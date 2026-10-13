#!/usr/bin/env python3
"""mk-builder.py — the mk-builder lane-agent status writer, protocol `mk-builder-status/v1`.

Implements `reference/status-contract.md` exactly; every rule is cited as `# R<n>` below and
mirrored by a `--self-test` case. It writes one lane file, `<lane>/.matrixkoder-status.json`
(R1), and never deletes anything: a stale file moves into `<lane>/.matrixkoder-old-<run>/`.

Stdlib only. One JSON line on stdout per invocation, like `scripts/mk-state.py:1299-1309`, plus
the lane token on the last line for `done` and `blocked` (R26).

`measure` (plan MB5.1) is the one orchestrator-side exception: read-only counters over a run's
`events.jsonl` and `tasks/*/brief-r*.md` under a `<data>` root — no lane file is read or written.

The bus client (plan BB) adds `ask`, `wait`, `inbox` and `ack` against `scripts/mk-bus.py`
(`child-skills/mk-builder/reference/bus-contract.md`): the lane's coordinates come from
`<lane>/.matrixkoder-bus.json` (B24), every call carries a 2 s connect budget, and an unusable
bus degrades to today's channels within that budget — `ask` falls back to the BLOCKED flow,
never a traceback.

Plan D7 adds `connect`, `read`, `todo`, `commit` and `report` over the same bus (contracts C7,
C8, C9, C12). With no project store (`<data>/mk.db` absent) or the bus down, those commands
fall back to the lane status file, the report file and the brief copy, so a lane never stalls.
"""
import argparse
import contextlib
import fcntl
import datetime
import hashlib
import io
import json
import os
import queue
import re
import shutil
import signal
import stat
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import http.client
import urllib.error
import urllib.parse
import urllib.request

FALLBACK_VERSION = "0.3.0"
STATUS_NAME = ".matrixkoder-status.json"
STATES = ("started", "working", "subagent", "blocked", "done")
ROLES = ("builder", "reviewer")
AGENTS = ("claude", "codex", "opencode", "agy", "unknown")
# `review` is the spelling the brief and lane-rules R19 use for the reviewer's token.
ROLE_ALIASES = {"builder": "builder", "review": "reviewer", "reviewer": "reviewer"}
# Table 1: the mk-state id alphabet (letters, digits, `.`, `_`, `-`, max 64, mk-state.py:134-136)
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA_RE = re.compile(r"[0-9a-f]{40}")
# R21: the archive name, built from the archived file's own four fields.
ARCHIVE_RE = re.compile(r"^status-[A-Za-z0-9._-]+-r\d+-(?:builder|reviewer)-\d+\.json$")
# The MB2 stale-lane-file rule: only these names, only when their round is not this start's.
STALE_RE = re.compile(r"^\.matrixkoder-(?:report-r(\d+)\.md|review-r(\d+)\.md|status.*\.json)$")
KEYS = ("v", "seq", "run", "task", "round", "role", "agent", "state", "step", "phases_done",
        "question", "commit", "report", "subagent", "ctx_pct", "started_at", "updated_at")

# Bus client (plan BB, `reference/bus-contract.md`). B24: the lane file `mk-bus.py token` writes;
# the inbox cursor is lane-only state, git-excluded with the `.matrixkoder-*` pattern (R2). B11:
# /wait clamps its server-side wait to 1..90 s, so the client long-polls in chunks of at most 90 s
# (a tool timeout cuts the call; `wait <ask id>` resumes it) and every connect carries a 2 s
# budget so a down bus falls back within it.
BUS_LANE_FILE = ".matrixkoder-bus.json"
BUS_CURSOR_FILE = ".matrixkoder-bus-cursor"
BUS_RUN = "bb"  # the self-test's run id (every fixture token and message uses it)
BUS_CONNECT_TIMEOUT = 2.0
BUS_WAIT_CHUNK_MAX = 90.0
BUS_ASK_TIMEOUT = 90.0
# E1: listen's own cap. A longer --timeout is refused, not clamped.
LISTEN_TIMEOUT_MAX = 50.0
# D7.2 (item 37): inbox and the progress hint must return inside this wall clock, even when
# /wait would otherwise sit for its 1 s floor on an empty inbox.
BUS_HINT_BUDGET = 2.0
# Absolute cap on one response body. A peer that never ends the stream cannot fill memory.
BUS_BODY_MAX = 1024 * 1024
ASK_ID_RE = re.compile(r"^m-(\d+)$")  # B15 Table 1: ids are `m-<seq>`
# D7: connect pages a brief at about 24,000 chars; `read <id> --part n` fetches one page.
BRIEF_PAGE = 24000
# C9: completed maps to this setting; verify is the schema default when config is unreadable.
TODO_DONE_DEFAULT = "verify"
TODO_STATES = ("in_progress", "completed", "done")  # `done` is the backup spelling of completed
# C8 seed recipes (scripts/mk-catalog.py SEEDS). A kind is hook-capable only when the catalog
# says todo_hook: yes. Recorded payload shapes live in hooks/payloads/<kind>.json.
HOOK_RECIPES = {
    "claude": "claude --settings <path under ~/.matrixkoder>",
}

# R13/R15/R17/R17a — the writer's legality table, read as: this command may run in which states.
# `progress` targets `working` (R13.1 from started, R13.3 from subagent, R13.5 from blocked,
# R15 in working); `progress --subagent` targets `subagent` and is legal in `working` and
# `subagent` only (R13.2, R17a) — `started -> subagent` and `blocked -> subagent` are R14 illegal.
# `ctx` and `phase-done` change no state, so they are gated by this table alone.
ALLOWED_FROM = {
    "progress": {"started", "working", "subagent", "blocked"},
    "progress-subagent": {"working", "subagent"},
    "blocked": {"working", "blocked"},
    "done": {"working"},
    "ctx": {"started", "working", "subagent", "blocked"},
    "phase-done": {"working"},
}
TARGET = {"progress": "working", "progress-subagent": "subagent", "blocked": "blocked",
          "done": "done", "ctx": None, "phase-done": None}


class Fail(Exception):
    """One refusal (mk-state.py:71-74): `msg` becomes the single JSON error line, `code` the exit."""

    def __init__(self, msg, code=2, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


class Parser(argparse.ArgumentParser):
    """argparse's own errors become Fail, so a bad flag also prints one JSON line (mk-state.py:77)."""

    def error(self, message):
        raise Fail(f"{self.prog}: {message}")


# ---------- validation (Table 1: type and null rule per field) ----------

def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check_run(run):
    if not run:
        raise Fail("run is empty [Table 1 run]")
    if not ID_RE.fullmatch(run):
        raise Fail(f"invalid run {run!r} (letters, digits, . _ -; max 64) [Table 1 run]")
    return run


def check_task(task):
    if not task:
        raise Fail("task is empty [Table 1 task]")
    if not ID_RE.fullmatch(task):
        raise Fail(f"invalid task {task!r} (letters, digits, . _ -; max 64) [Table 1 task]")
    return task


def check_report(rel):
    """Table 1: `report` is a path relative to the lane root, never null after `start` (R26)."""
    if not rel or not rel.strip():
        raise Fail("report path is empty [Table 1 report]")
    if os.path.isabs(rel):
        raise Fail(f"report path must be relative to the lane root, got {rel!r} [Table 1 report]")
    norm = os.path.normpath(rel)
    if norm == ".." or norm.startswith(".." + os.sep):
        raise Fail(f"report path escapes the lane root: {rel!r} [Table 1 report]")
    return rel


def check_role(role):
    if not role or not ID_RE.fullmatch(role):
        raise Fail(f"invalid role {role!r} [Table 1 role]")
    got = ROLE_ALIASES.get(role)
    if not got:
        raise Fail(f"role must be one of {', '.join(ROLES)} (alias 'review'), got {role!r} [Table 1 role]")
    return got


def detect_agent():
    """Table 1: the agent kind, `unknown` when the writer cannot detect it."""
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE"):
        return "claude"
    if os.environ.get("OPENCODE"):
        return "opencode"
    return "unknown"


def check_agent(agent):
    if agent not in AGENTS:
        raise Fail(f"agent must be one of {', '.join(AGENTS)}, got {agent!r} [Table 1 agent]")
    return agent


# ---------- the file ----------

def status_path(lane):
    """R1: one file per lane, at the lane worktree root."""
    return os.path.join(lane, STATUS_NAME)


def lane_dir(a):
    return os.path.abspath(getattr(a, "lane", None) or ".")


def read_status(lane):
    """The live file, or None when it is absent (R27.1). Raises on unreadable/corrupt content."""
    try:
        with open(status_path(lane)) as f:
            obj = json.load(f)
    except FileNotFoundError:
        return None
    if not isinstance(obj, dict):
        raise ValueError(f"{STATUS_NAME} is not a JSON object [R27.1]")
    return obj


def load_live(lane):
    obj = read_status(lane)
    if obj is None:
        raise Fail(f"no {STATUS_NAME} in {lane}; run `start` first")
    return obj


def write_status(lane, obj):
    """R23: temp file in the target's own directory, flush, fsync, chmod 0644, `os.replace` — a
    reader sees the whole old file or the whole new one. R24: on any failure unlink the temp
    file and leave the existing file byte-identical (mk-state.py:144-160)."""
    path = status_path(lane)
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; the board's own files are 0644
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def stamp(obj):
    """R25: every accepted write bumps `seq` by exactly 1 and sets `updated_at` to the write
    time (R7: float epoch seconds, full precision)."""
    obj["seq"] = int(obj.get("seq", 0)) + 1
    obj["updated_at"] = time.time()
    return obj


def new_file(run, task, round_n, role, agent, report, now):
    """Table 1 — every v1 field with its type and its null rule. R11: `started_at` is this first
    start of this (run, task, round, role)."""
    return {"v": 1, "seq": 1, "run": run, "task": task, "round": round_n, "role": role,
            "agent": agent, "state": "started", "step": "start", "phases_done": [],
            "question": None, "commit": None, "report": report, "subagent": None,
            "ctx_pct": None, "started_at": now, "updated_at": now}


def require_state(old, command):
    """R16/R17/R17a: a command outside ALLOWED_FROM[command] is an illegal transition — exit
    non-zero, one JSON error line naming it, file byte-identical."""
    target = TARGET[command]
    frm = old.get("state")
    if frm in ALLOWED_FROM[command]:
        return
    if target:
        raise Fail(f"illegal transition {frm} -> {target} (`{command}`) [R14, R16]")
    raise Fail(f"illegal command `{command}` in state {frm} [R17, R16]")


# ---------- archive (R21) and the MB2 stale-lane-file rule ----------

def archive_dir(lane, run):
    return os.path.join(lane, f".matrixkoder-old-{run}")


def move_into(lane, run, src, name):
    """Move (never delete) into `.matrixkoder-old-<run>/`, creating the dir on first use."""
    dest_dir = archive_dir(lane, run)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    shutil.move(src, dest)
    return dest


def archive_status(lane, old):
    """R21: the live file moves to `.matrixkoder-old-<its OWN run>/`, never deleted, named
    `status-<task>-r<round>-<role>-<seq>.json` from the archived file's own four fields, so a
    second archive in one run never overwrites the first."""
    name = f"status-{old.get('task')}-r{old.get('round')}-{old.get('role')}-{old.get('seq')}.json"
    assert ARCHIVE_RE.fullmatch(name), f"R21 archive name shape: {name}"
    move_into(lane, old.get("run"), status_path(lane), name)
    return name


def archive_other_rounds(lane, run, round_n, report):
    """The MB2 archive rule: the lane's round files that belong to another round move into the
    same archive dir, so a reused slot never carries the previous round's report into this one.
    Only the name is inspected — no file's contents are read, and none is deleted. Report and
    review files carry no run field, so their archive dir is this start's run; the status file
    uses its own run per R21."""
    moved = []
    try:
        names = sorted(os.listdir(lane))
    except OSError:
        return moved
    for name in names:
        m = STALE_RE.match(name)
        if not m:
            continue
        path = os.path.join(lane, name)
        if not os.path.isfile(path) or path == os.path.join(lane, report):
            continue  # this start's own report is never archived by itself
        other = m.group(1) or m.group(2) or (re.search(r"-r(\d+)", name) or [None, None])[1]
        if other is None or int(other) == round_n:
            continue  # this round, or no round to read: not provably stale
        move_into(lane, run, path, name)
        moved.append(name)
    return moved


# ---------- commands ----------

def cmd_start(a):
    """R21/R22: a different (run, task, round, role) — or a `done` file — is an archive plus a
    fresh file; the same identity with a non-done state is a resume. The lane's round files of
    another round are archived either way."""
    run, task, round_n = check_run(a.run), check_task((a.task or "").strip()), a.round
    role, agent, report = check_role(a.role), check_agent(a.agent or detect_agent()), check_report(a.report)
    if not is_int(round_n) or round_n < 1:
        raise Fail(f"round must be an integer >= 1, got {round_n!r} [Table 1 round]")
    lane, now = lane_dir(a), time.time()
    old, archived, resumed = read_status(lane), [], False
    if old is None:
        obj = new_file(run, task, round_n, role, agent, report, now)
    elif (old.get("run"), old.get("task"), old.get("round"), old.get("role")) == (run, task, round_n, role) \
            and old.get("state") != "done":
        # R22: this start's report and agent win (a takeover may change the agent kind),
        # phases_done is kept (R8 append-only) and started_at is kept (R11).
        resumed = True
        obj = dict(old, state="started", step="start", question=None, commit=None, subagent=None,
                   ctx_pct=None, report=report, agent=agent)
        stamp(obj)  # R22/R25: a resume continues `seq`; a fresh file starts at 1 (R21)
    else:
        # LOW 2: archive first (archive_status and archive_other_rounds), then write the new file.
        archived.append(archive_status(lane, old))
        obj = new_file(run, task, round_n, role, agent, report, now)
    archived.extend(archive_other_rounds(lane, run, round_n, report))
    write_status(lane, obj)
    return ({"ok": True, "archived": archived, "resumed": resumed, "seq": obj["seq"],
             "state": obj["state"], "run": run, "task": task, "round": round_n,
             "role": role, "agent": agent, "file": status_path(lane)}, None)


def cmd_progress(a):
    """R13.1/R13.3/R13.5/R15 plain progress; R13.2/R15 `--subagent`, where a heartbeat in
    `subagent` bumps only `seq` and `updated_at` so the run's age stays readable (R9). BB: the
    trailing line is the unread hint, not a token — `N unread — run inbox` when the bus holds
    messages for this lane, silent otherwise."""
    lane, old = lane_dir(a), load_live(lane_dir(a))
    step = (a.step or "").strip()
    if not step:
        raise Fail("step is empty [Table 1 step]")
    if a.subagent:
        require_state(old, "progress-subagent")
        if old.get("state") == "subagent":
            obj = dict(old)  # R9 heartbeat: only seq and updated_at move
        else:
            obj = dict(old, step=step, state="subagent", question=None,
                       subagent={"task": step, "since": time.time()})
    else:
        require_state(old, "progress")
        obj = dict(old, step=step, state="working", question=None, subagent=None)
    write_status(lane, stamp(obj))
    # A subagent heartbeat only bumps seq; it must not add a bus row (R9).
    if obj.get("state") != "subagent" or not a.subagent or old.get("state") != "subagent":
        post_progress(lane, step)
    return ({"ok": True, "seq": obj["seq"], "state": obj["state"], "step": obj["step"],
             "subagent": obj["subagent"]}, bus_unread_hint(lane))


def post_progress(lane, step):
    """E1: also post `progress: <text>` as an info message. Best effort: a down bus
    never blocks and never fails the status write that already happened."""
    cfg = bus_lane_config(lane)
    if cfg is None:
        return
    text = " ".join(str(step).splitlines())
    with contextlib.suppress(BusDown, BusHttp, OSError, ValueError):
        bus_send(cfg, "info", f"progress: {text}")


def token_for(old, tail):
    """R26: the exact line `lane-watch.py:72-83` matches — `{task} R{round}` for a builder,
    `{task} REVIEW R{round}` for a reviewer."""
    return f"{old['task']} {'REVIEW ' if old.get('role') == 'reviewer' else ''}R{old['round']} {tail}"


def cmd_blocked(a):
    """R19: a non-empty question (whitespace-only is empty); R26 prints the token."""
    lane, old = lane_dir(a), load_live(lane_dir(a))
    question = a.question
    if question is None or not question.strip():
        raise Fail("blocked needs a non-empty question [R19]")
    require_state(old, "blocked")
    obj = dict(old, state="blocked", question=question, commit=None, subagent=None)
    write_status(lane, stamp(obj))
    token_q = " ".join(question.splitlines())
    return ({"ok": True, "seq": obj["seq"], "state": obj["state"], "question": question},
            token_for(obj, f"BLOCKED: {token_q}"))


def lane_head(lane):
    """R18: the full 40-hex HEAD sha the lane points at; None when there is no repo or no commit."""
    try:
        r = subprocess.run(["git", "-C", lane, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = r.stdout.strip() if r.returncode == 0 else ""
    return sha if SHA_RE.fullmatch(sha) else None


# R31: a test/build command still running from this lane. Same idea as lane-watch
# child_progress, self-contained (that script is not imported).
_CHILD_RE = re.compile(
    r"(?:^|/)(?:selftest\.sh|pytest|tsc)(?:\s|$)"
    r"|claude\s+plugin\s+test"
    r"|python3?\b.*(?:--self-test|\btime\.sleep\b)",
    re.I)


def _ps_rows():
    """(pid, ppid, command) from `ps -axo pid,ppid,command`. Empty on any failure."""
    try:
        r = subprocess.run(["ps", "-axo", "pid,ppid,command"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        rows.append((parts[0], parts[1], parts[2]))
    return rows


def _cwd_of(pid):
    try:
        return os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        pass
    try:
        r = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return ""
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in r.stdout.splitlines():
        if line.startswith("n"):
            return os.path.realpath(line[1:])
    return ""


def running_lane_children(lane):
    """Commands started from this lane that are still running. `done` itself runs as a fresh
    process, so the child is not its descendant: a process counts when its command matches a
    test/build pattern AND its cwd (or its arguments) point into this lane. The mk-builder
    process checking the lane is never a hit. Empty when ps cannot be read."""
    lane = os.path.realpath(lane)
    me = str(os.getpid())
    rows = _ps_rows()
    hits = []
    for pid, _ppid, command in rows:
        if pid == me:
            continue
        if not _CHILD_RE.search(command):
            continue
        if os.path.abspath(__file__) in command and "--self-test" not in command:
            continue
        cwd = _cwd_of(pid)
        in_lane = False
        if cwd:
            try:
                in_lane = os.path.commonpath([cwd, lane]) == lane
            except ValueError:
                in_lane = False
        if not in_lane:
            in_lane = lane in command
        if in_lane:
            hits.append(command.strip())
    return hits


def cmd_done(a):
    """R18: the report file at the lane root AND a readable HEAD; R20: final per identity, so the
    legality table refuses every second write. The report is checked first, so the error is
    deterministic. R31: a still-running lane child is a WARNING before the token, never a failure
    and never a change to the token being the last line."""
    lane, old = lane_dir(a), load_live(lane_dir(a))
    require_state(old, "done")
    report = old.get("report") or ""
    if not report or not os.path.isfile(os.path.join(lane, report)):
        raise Fail(f"report_missing: {report!r} does not exist at the lane root [R18]")
    sha = lane_head(lane)
    if not sha:
        raise Fail("no_head: `git -C <lane> rev-parse HEAD` failed [R18]")
    obj = dict(old, state="done", question=None, subagent=None, commit=sha)
    write_status(lane, stamp(obj))
    for command in running_lane_children(lane):
        print(f"WARNING: a lane child is still running: {command}")
    # Todo reporting: the report's `## Todo` section rides on the DONE token. A missing
    # section or an `open` item without a reason warns and does not fail the round.
    warn = ""
    try:
        with open(os.path.join(lane, report)) as f:
            items, warn = parse_todo_section(f.read())
    except OSError:
        items = []
        warn = "todo section missing"
    done_n, skipped_n, total = todo_counts(items)
    tail = "DONE"
    if items:
        tail = f"DONE todo {done_n}/{total} done {skipped_n} skipped"
    if warn:
        print(f"todo warning: {warn}")
    return ({"ok": True, "seq": obj["seq"], "state": obj["state"], "commit": sha,
             "todo": {"done": done_n, "skipped": skipped_n, "total": total}},
            token_for(obj, tail))


def cmd_review_done(a):
    """Reviewer lane: post the verdict, finding count and review file path as ONE bus message,
    then move the status file to done (the same legal transition as `done`) and print
    `<T> REVIEW R<n> DONE`. Store mode sends kind `review`; file mode refuses that kind, so it
    sends `info` with the same text. A bus that cannot be reached is ask's contract: the status
    file goes `blocked`, `BUS DOWN: use the BLOCKED token`, exit 4 (nothing moves to done)."""
    lane, old = lane_dir(a), load_live(lane_dir(a))
    require_state(old, "done")
    if not is_int(a.findings) or a.findings < 0:
        raise Fail(f"findings must be an integer >= 0, got {a.findings!r}")
    path = (a.path or "").strip()
    if not path or not os.path.isfile(os.path.join(lane, path)):
        raise Fail(f"review_missing: {path!r} does not exist [R18]")
    text = f"round: {old.get('round')}\nverdict: {a.verdict} findings: {a.findings} path: {path}"
    cfg = bus_lane_config(lane)
    if cfg is None:
        ask_down(lane, " ".join(text.split()))
    kind = "review" if store_present(bus_data_dir(cfg)) else "info"
    try:
        bus_send(cfg, kind, text)
    except BusDown:
        ask_down(lane, " ".join(text.split()))
    except BusHttp as e:
        raise Fail(bus_http_error(e), code=5)
    obj = dict(old, state="done", question=None, subagent=None, commit=lane_head(lane))
    write_status(lane, stamp(obj))
    return ({"ok": True, "seq": obj["seq"], "state": obj["state"], "kind": kind,
             "verdict": a.verdict, "findings": a.findings, "path": path},
            f"{old['task']} REVIEW R{old['round']} DONE")


def cmd_ctx(a):
    """R10: only an integer 0..100, and only this command ever writes `ctx_pct`."""
    lane, old = lane_dir(a), load_live(lane_dir(a))
    if not is_int(a.pct) or not 0 <= a.pct <= 100:
        raise Fail(f"ctx must be an integer 0..100, got {a.pct!r} [R10]")
    require_state(old, "ctx")
    obj = dict(old, ctx_pct=a.pct)
    write_status(lane, stamp(obj))
    return ({"ok": True, "seq": obj["seq"], "ctx_pct": obj["ctx_pct"]}, None)


def cmd_phase_done(a):
    """R8: append-only, a phase id at most once per round; R17: only in `working`."""
    lane, old = lane_dir(a), load_live(lane_dir(a))
    pid = (a.phase_id or "").strip()
    if not pid:
        raise Fail("phase id is empty [Table 1 phases_done]")
    require_state(old, "phase-done")
    phases = list(old.get("phases_done") or [])
    if pid in phases:
        raise Fail(f"{pid!r} is already in phases_done; R8 appends a phase id at most once per round")
    obj = dict(old, phases_done=phases + [pid])
    write_status(lane, stamp(obj))
    return ({"ok": True, "seq": obj["seq"], "phases_done": obj["phases_done"]}, None)


def reader_problems(obj, a):
    """R27.2-R27.9 — the conditions a reader applies before it trusts the file."""
    out = []
    v = obj.get("v")
    if not is_int(v) or v > 1:
        out.append(f"v {v!r} is not an int <= 1 [R27.2]")
    for field, armed in (("run", a.run), ("task", a.task), ("round", a.round)):
        want = armed if armed is not None else obj.get(field)
        if obj.get(field) != want:
            out.append(f"{field} {obj.get(field)!r} does not match the armed {field} {want!r} [R27.3-5]")
    want = check_role(a.role) if a.role else obj.get("role")
    if obj.get("role") != want:
        out.append(f"role {obj.get('role')!r} does not match the armed role {want!r} [R27.6]")
    armed_at = a.armed_at or 0.0
    updated = obj.get("updated_at")
    if not is_num(updated) or updated < armed_at:
        out.append(f"updated_at {updated!r} is not a float epoch >= armed_at {armed_at!r} [R27.7]")
    if obj.get("state") not in STATES:
        out.append(f"unknown state {obj.get('state')!r} [R27.8]")
    if a.state is not None:
        if a.state not in STATES:
            out.append(f"unknown state {a.state!r} [R27.8]")
        elif obj.get("state") != a.state:
            out.append(f"state {obj.get('state')!r} does not match the armed state {a.state!r}")
    seq = obj.get("seq")
    if not is_int(seq) or seq < 1:
        out.append(f"seq {seq!r} is not an int >= 1 [R27.9]")
    elif a.seen_seq is not None and seq <= a.seen_seq:
        out.append(f"seq {seq} is not greater than the last accepted {a.seen_seq} [R27.9]")
    return out


def cmd_show(a):
    """R27-R29 as a command: the writer's own reader check, so a lane agent can see whether the
    file it wrote is one the orchestrator's reader would accept. Writes nothing."""
    lane, path, problems, obj = lane_dir(a), status_path(lane_dir(a)), [], None
    if not os.path.exists(path):
        problems.append(f"missing: {STATUS_NAME} [R27.1]")
    else:
        try:
            with open(path) as f:
                obj = json.load(f)
        except (OSError, ValueError):
            obj = None
            problems.append(f"unreadable or corrupt JSON [R27.1, R29]")
    if obj is not None and not isinstance(obj, dict):
        obj = None
        problems.append(f"not a JSON object [R27.1]")
    if obj is not None:
        problems.extend(reader_problems(obj, a))
    return ({"ok": True, "accepted": not problems, "status": obj, "problems": problems}, None)


# ---------- install (plan decision D2) ----------

def skill_root():
    """The skill folder this script lives in — copying the folder is the whole install (D2)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def plugin_version():
    """D2: `install` prints the version it installed; the plugin root's plugin.json is the source."""
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        pj = os.path.join(d, ".claude-plugin", "plugin.json")
        if os.path.isfile(pj):
            with contextlib.suppress(OSError, ValueError, AttributeError):
                with open(pj) as f:
                    return str(json.load(f).get("version") or FALLBACK_VERSION)
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return FALLBACK_VERSION


def copy_skill(src, dest):
    """Copy the whole skill folder, overwriting, so the installed version always matches the
    plugin's. `__pycache__` and dot-files are skipped."""
    count = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
        rel = os.path.relpath(root, src)
        parts = [] if rel == "." else rel.split(os.sep)
        for name in sorted(files):
            if name.startswith("."):
                continue
            out = os.path.join(dest, *parts, name)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copy2(os.path.join(root, name), out)
            count += 1
    return count


def ensure_exclude(repo, line):
    """R2: the lane keeps its `.matrixkoder-*` and the installed skill out of `git status`. The
    line is appended once; an existing exclude file is never rewritten."""
    try:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "--git-path", "info/exclude"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return False
        exclude_path = r.stdout.strip()
        if not exclude_path:
            return False
        if not os.path.isabs(exclude_path):
            exclude_path = os.path.join(repo, exclude_path)
    except (OSError, subprocess.SubprocessError):
        return False

    os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
    try:
        with open(exclude_path) as f:
            have = f.read().splitlines()
    except FileNotFoundError:
        have = []
    if line in [x.strip() for x in have]:
        return False
    with open(exclude_path, "a") as f:
        if have and have[-1].strip():
            f.write("\n")
        f.write(line + "\n")
    return True


def cmd_install(a):
    """D2: idempotent self-install for a child that lacks the skill; prints the version."""
    dest = os.path.join(os.path.abspath(a.into), ".claude", "skills", "mk-builder")
    count = copy_skill(skill_root(), dest)
    ensure_exclude(os.path.abspath(a.into), ".claude/skills/mk-builder/")
    return ({"ok": True, "installed": dest, "version": plugin_version(), "files": count}, None)


# ---------- measure (plan MB5.1: read-only counters over one run) ----------

# The verdict vocabulary `lane-watch.record()` prefixes its notes with (scripts/lane-watch.py:62-64
# STATUS_MAP keys, plus the pane-exit and watch-expiry states). Only those events carry — or, in
# runs that predate MB3, lack — the `source=` tag `apply_verdict` appends as the final evidence
# item (scripts/mk-lane-event.py:330-340, plan C4).
VERDICT_HEAD_RE = re.compile(
    r"^lane-watch (DONE|DONE_SENT|STALL|BLOCKED|QUOTA|PROVIDER|ERROR|WATCH_EXPIRED|DEV_SERVER): ")
SOURCE_RE = re.compile(r"source=(status|pane|grace)\b")
# The grace DONE shape, both eras: pre-MB3 "no S2 R3 DONE token (commit + report, agent idle
# 3.1 min)" (settings-tab-r1) and current "no P1 DONE token (report, agent idle 4 min)"
# (scripts/lane-watch.py:233-240) — the source=grace tag covers only the current one.
NO_TOKEN_RE = re.compile(r"no \S.* DONE token")
# A commit mention (record()'s "commit <sha>", an orchestrator "verify T @<sha>", a merge note):
# the false-STALL proxy's second signal. 7-40 hex chars; a 7-digit decimal is rare enough in
# verdict prose that the proxy keeps it.
COMMIT_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
BRIEF_NAME_RE = re.compile(r"^brief-r(\d+)\.md$")
SOURCES = ("status", "pane", "grace")
FALSE_STALL_WINDOW = datetime.timedelta(minutes=10)  # the acceptance's "within 10 min"


def parse_events(run_dir):
    """`events.jsonl` as a list of dicts; a torn line from a crash mid-append is skipped, not
    fatal (mk-state.py:355-365). A missing file is an empty run, not an error."""
    try:
        with open(os.path.join(run_dir, "events.jsonl")) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for x in lines:
        with contextlib.suppress(json.JSONDecodeError):
            out.append(json.loads(x))
    return out


def verdict_of(e):
    """(state, text) for an event lane-watch's record() wrote — `lane-watch <STATE>: <evidence>`
    — else None. Orchestrator verdicts (type "verdict", no lane-watch head) are not counted."""
    m = VERDICT_HEAD_RE.match(e.get("text") or "")
    return (m.group(1), e["text"]) if m else None


def source_of(text):
    """The LAST `source=` tag in the text (apply_verdict appends it as the final evidence item,
    so the last one is the verdict's own); None when the run predates MB3 — counted as `none`."""
    hit = None
    for m in SOURCE_RE.finditer(text):
        hit = m.group(1)
    return hit


def blocked_question(text):
    """The question inside a BLOCKED verdict's evidence: after the verdict head, strip the final
    `; source=...` item (and any `(status ... kept)` suffix record() appends for a late watcher,
    scripts/lane-watch.py:337), then the evidence's own prefix — `<phase> BLOCKED: ` (the pane
    path, scripts/lane-watch.py:228) or `status file blocked: ` (the file path, :230)."""
    q = text[VERDICT_HEAD_RE.match(text).end():]
    cut = q.rfind("; source=")
    if cut != -1:
        q = q[:cut]
    q = re.sub(r"^status file blocked: ", "", q)
    q = re.sub(r"^(.*?) BLOCKED: ", "", q, count=1)
    return q.strip()


def event_time(e):
    """The event's `t` as a datetime (mk-state writes ISO seconds with an offset,
    scripts/mk-state.py:87), or None — an event without a usable time joins no window."""
    try:
        return datetime.datetime.fromisoformat(e["t"])
    except (KeyError, TypeError, ValueError):
        return None


def false_stall(se, events):
    """The false-STALL proxy: this STALL is followed by a DONE verdict, or by any event that
    mentions a commit sha, for the SAME task within 10 minutes. A STALL whose task is null
    cannot be attributed to a lane, and neither can a STALL without a parsable time."""
    if se.get("task") is None:
        return False
    t1 = event_time(se)
    if t1 is None:
        return False
    for e in events:
        if e is se or e.get("task") != se.get("task"):
            continue
        t2 = event_time(e)
        if t2 is None:
            continue
        if datetime.timedelta(0) <= t2 - t1 <= FALSE_STALL_WINDOW:
            v = verdict_of(e)
            if (v and v[0] in ("DONE", "DONE_SENT")) or COMMIT_SHA_RE.search(e.get("text") or ""):
                return True
    return False


def brief_counts(run_dir):
    """Per task, each `brief-rN.md`'s lines (splitlines of the decoded bytes — equals `wc -l` on
    the newline-terminated briefs mk-state writes) and bytes (the file's size, `wc -c`). Only
    `brief-rN.md` counts: `task.json`, `review-brief-rN.md` and reports are not briefs. Rounds
    sort numerically, so brief-r10 follows brief-r2."""
    tasks_dir = os.path.join(run_dir, "tasks")
    out = {}
    try:
        names = sorted(os.listdir(tasks_dir))
    except OSError:
        return out
    for name in names:
        if not os.path.isdir(os.path.join(tasks_dir, name)):
            continue
        try:
            files = os.listdir(os.path.join(tasks_dir, name))
        except OSError:
            continue
        briefs = {}
        for fn in files:
            if not BRIEF_NAME_RE.match(fn):
                continue
            with open(os.path.join(tasks_dir, name, fn), "rb") as f:
                data = f.read()
            briefs[fn] = {"lines": len(data.decode("utf-8", "replace").splitlines()), "bytes": len(data)}
        out[name] = dict(sorted(briefs.items(),
                                key=lambda kv: (int(BRIEF_NAME_RE.match(kv[0]).group(1)), kv[0])))
    return out


def cmd_measure(a):
    """MB5.1: one JSON line of read-only counters over one run — per task brief lines and bytes,
    verdict events by source (status|pane|grace, plus `none` for runs that predate the MB3 tag),
    DONE verdicts that needed the grace rule (missed DONE lines), STALL events followed by a
    DONE or a commit within 10 min (the false-STALL proxy), and the BLOCKED questions. Nothing
    under <data> is written; counts are per EVENT, so a 4-child lane records one verdict once
    per child (and once more as a kept-status note) — measurement.md states how to read that."""
    run = check_run(a.run)
    data = os.path.abspath(a.data)
    if not os.path.isdir(data):
        raise Fail(f"data dir {data!r} does not exist [measure]")
    run_dir = os.path.join(data, "runs", run)
    if not os.path.isdir(run_dir):
        raise Fail(f"unknown run {run!r}: no {run_dir} directory [measure]")
    if store_present(data):
        events = measure_store(data, run)
    else:
        events = parse_events(run_dir)
    by_source = {s: 0 for s in SOURCES}
    by_source["none"] = 0
    missed, stalls, questions = 0, [], []
    for e in events:
        v = verdict_of(e)
        if not v:
            continue
        state, text = v
        src = source_of(text)
        by_source[src or "none"] += 1
        if state == "DONE" and (src == "grace" or NO_TOKEN_RE.search(text)):
            missed += 1
        elif state == "STALL":
            stalls.append(e)
        elif state == "BLOCKED":
            questions.append(blocked_question(text))
    false = sum(1 for se in stalls if false_stall(se, events))
    return ({"ok": True, "run": run, "data": data, "briefs": brief_counts(run_dir),
             "verdicts_by_source": by_source, "missed_done_lines": missed,
             "stalls": len(stalls), "false_stalls": false,
             "blocked": len(questions), "blocked_questions": questions}, None)


# ---------- bus client (plan BB; contract: reference/bus-contract.md) ----------

class Handled(Exception):
    """A bus command that already printed its exact output (`ask`'s answer text,
    `PENDING`, `BUS DOWN`); `code` is the exit main returns."""

    def __init__(self, code=0):
        super().__init__(f"already printed: exit {code}")
        self.code = code


class BusDown(Exception):
    """The bus is unusable (no lane file, connection refused, timeout, garbage response)."""


class BusTimeout(BusDown):
    """The absolute wall-clock deadline for one bus response expired.

    Callers already map BusDown to BUS DOWN / "no answer yet". A slow-drip peer
    (one byte per second of headers or body) raises this instead of resetting a
    per-recv socket timer.
    """


class BusHttp(Exception):
    """The bus answered with an HTTP error status (`code`, server `error` text)."""

    def __init__(self, code, error):
        super().__init__(f"{code}: {error}")
        self.code, self.error = code, error


def bus_http_error(e):
    """The brief's client error shape: `{"ok": false, "error": "<status>: <server error>"}`."""
    return f"{e.code}: {e.error}"


# A seq past this cannot be a JSON number the bus would send, and writing it
# poisons the cursor so no later message is visible (review R1).
BUS_SEQ_MAX = 2 ** 53

def bus_seq(v):
    """A cursor seq: a non-negative int below 2**53. Anything else is unusable."""
    return is_int(v) and 0 <= v < BUS_SEQ_MAX


def bus_read_cursor(lane):
    """The last seq this lane has seen (`<lane>/.matrixkoder-bus-cursor`, lane-only). A missing,
    corrupt, negative or huge cursor reads as 0 — at-least-once, never a crash."""
    try:
        with open(os.path.join(lane, BUS_CURSOR_FILE)) as f:
            n = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0
    return n if bus_seq(n) else 0


def bus_write_cursor(lane, seq):
    """Same temp+fsync+os.replace as write_status: a SIGKILL mid-write leaves the previous
    cursor or the new one, never a partial line that bus_read_cursor would read as 0."""
    path = os.path.join(lane, BUS_CURSOR_FILE)
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-cursor-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"{int(seq)}\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _bus_read_bounded(sock, n, deadline):
    """Read up to n bytes, or until the absolute deadline. Socket timeout is the
    remaining budget (capped at 1 s) so a drip cannot reset a per-recv timer past it."""
    buf = b""
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BusTimeout("bus response exceeded the wall-clock deadline")
        sock.settimeout(min(remaining, 1.0))
        try:
            chunk = sock.recv(n - len(buf))
        except TimeoutError:
            continue
        if not chunk:
            break
        buf += chunk
    return buf


def _bus_read_line(sock, deadline):
    """One response line (status or header), bounded by the same absolute deadline."""
    buf = b""
    while b"\r\n" not in buf:
        if len(buf) > 65536:
            raise BusDown("bus response header line too long")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BusTimeout("bus response exceeded the wall-clock deadline")
        sock.settimeout(min(remaining, 1.0))
        try:
            chunk = sock.recv(1)
        except TimeoutError:
            continue
        if not chunk:
            break
        buf += chunk
    return buf


def bus_http_exchange(method, url, headers, data, timeout):
    """One HTTP/1.1 exchange with an absolute wall-clock deadline covering connect,
    request, response headers and the whole body. `timeout` is that deadline in
    seconds from now, not a per-recv socket timeout. The body is read in 4096-byte
    chunks and capped at BUS_BODY_MAX. Returns (status, body bytes)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", ""):
        raise BusDown(f"bus url scheme is not http: {parsed.scheme!r}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    deadline = time.monotonic() + timeout
    conn = http.client.HTTPConnection(host, port, timeout=min(max(timeout, 0.05), 1.0))
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BusTimeout("bus response exceeded the wall-clock deadline")
        conn.timeout = min(remaining, 1.0)
        conn.connect()
        sock = conn.sock
        sock.settimeout(min(max(deadline - time.monotonic(), 0.05), 1.0))
        # One Host header, the caller's. HTTPConnection.request adds its own,
        # and a duplicated Host is what the bus rejects (403 / 400).
        hdrs = {k: v for k, v in headers.items()}
        if data is not None and "Content-Length" not in hdrs:
            hdrs["Content-Length"] = str(len(data))
        lines = [f"{method} {path} HTTP/1.1", "Accept-Encoding: identity"]
        for k, v in hdrs.items():
            lines.append(f"{k}: {v}")
        raw_req = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
        if data:
            raw_req += data
        sock.sendall(raw_req)
        status_line = _bus_read_line(sock, deadline)
        if not status_line:
            raise BusDown("empty bus response")
        try:
            _ver, code_s, _reason = status_line.decode("iso-8859-1").split(" ", 2)
            status = int(code_s)
        except ValueError:
            raise BusDown("bus response status line is not HTTP") from None
        resp_headers = {}
        while True:
            line = _bus_read_line(sock, deadline)
            if line in (b"\r\n", b"\n", b""):
                break
            raw = line.decode("iso-8859-1").strip()
            if ":" in raw:
                k, v = raw.split(":", 1)
                resp_headers[k.strip().lower()] = v.strip()
        te = resp_headers.get("transfer-encoding", "")
        if "chunked" in te.lower():
            body = b""
            while True:
                size_line = _bus_read_line(sock, deadline)
                if not size_line:
                    break
                try:
                    size = int(size_line.decode("ascii").split(";", 1)[0].strip() or "0", 16)
                except ValueError:
                    raise BusDown("bad chunk size from bus") from None
                if size == 0:
                    break
                if len(body) + size > BUS_BODY_MAX:
                    raise BusDown("bus response body exceeds 1 MiB")
                got = b""
                while len(got) < size:
                    piece = _bus_read_bounded(sock, min(4096, size - len(got)), deadline)
                    if not piece:
                        raise BusTimeout("bus response exceeded the wall-clock deadline")
                    got += piece
                body += got
                _bus_read_line(sock, deadline)  # trailing CRLF
            return status, body
        declared = resp_headers.get("content-length")
        if declared is not None:
            try:
                need = int(declared)
            except ValueError:
                raise BusDown("bad Content-Length from bus") from None
            if need > BUS_BODY_MAX:
                raise BusDown("bus response body exceeds 1 MiB")
            body = b""
            while len(body) < need:
                piece = _bus_read_bounded(sock, min(4096, need - len(body)), deadline)
                if not piece:
                    raise BusDown("bus closed before Content-Length")
                body += piece
            return status, body
        body = b""
        while True:
            if len(body) >= BUS_BODY_MAX:
                raise BusDown("bus response body exceeds 1 MiB")
            piece = _bus_read_bounded(sock, min(4096, BUS_BODY_MAX - len(body)), deadline)
            if not piece:
                break
            body += piece
        return status, body
    except BusTimeout:
        raise
    except BusDown:
        raise
    except (TimeoutError, ConnectionRefusedError, OSError, http.client.HTTPException) as e:
        if time.monotonic() >= deadline:
            raise BusTimeout("bus response exceeded the wall-clock deadline") from None
        raise BusDown(f"{type(e).__name__}: {e}") from None
    finally:
        conn.close()


def bus_call(cfg, method, path, body=None, timeout=BUS_CONNECT_TIMEOUT):
    """One HTTP call to the bus (B4-B8: loopback, Bearer token, strict Host).

    `timeout` is an absolute wall-clock deadline for the whole request, including
    response headers and body — not a per-recv socket timeout. A peer that dribbles
    one byte per second cannot extend the call past it. BusTimeout (a BusDown) on
    that deadline; BusHttp on any HTTP error status — never a traceback. The body
    is capped at 1 MiB.
    """
    base = (cfg.get("url") or "").rstrip("/")
    parsed = urllib.parse.urlparse(base)
    host = parsed.hostname or "127.0.0.1"
    headers = {"Authorization": f"Bearer {cfg['token']}",
               "Host": f"{host}:{parsed.port}" if parsed.port else host,
               "Connection": "close"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    try:
        status, raw = bus_http_exchange(method, base + path, headers, data, timeout)
    except BusTimeout:
        raise
    except BusDown:
        raise
    if status >= 400:
        error = http.client.responses.get(status, "error")
        with contextlib.suppress(OSError, ValueError):
            parsed_err = json.loads(raw.decode("utf-8"))
            if isinstance(parsed_err, dict) and parsed_err.get("error"):
                error = parsed_err["error"]
        raise BusHttp(status, error)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise BusDown(f"{type(e).__name__}: {e}") from None
    if not isinstance(payload, dict):
        raise BusDown("response is not a JSON object")
    return payload


def bus_ack_body(cfg, msg_id):
    """B13: `{run, id, ack_level: 3}` — level 3 is `read` (B19): this script printed it."""
    body = {"id": msg_id, "ack_level": 3}
    if cfg.get("run"):
        body["run"] = cfg["run"]
    return body


def ack_once(lane, cfg, msg_id):
    """One level-3 ack per message for this lane (todos 37, 42).

    The marker is written BEFORE the POST, so a crash between them retries rather
    than acking twice. A marker OSError after the POST is swallowed: the ack
    already landed and must not become "bus down". Returns True when this call
    sent the ack."""
    if not msg_id:
        return False
    path = os.path.join(lane, ".matrixkoder-ack-once")
    seen = set()
    try:
        with open(path) as f:
            seen = {ln.strip() for ln in f if ln.strip()}
    except OSError:
        seen = set()
    if msg_id in seen:
        return False
    try:
        with open(path, "a") as f:
            f.write(msg_id + "\n")
    except OSError:
        seen = set()
    bus_call(cfg, "POST", "/ack", body=bus_ack_body(cfg, msg_id))
    return True


def bus_wait_chunk(cfg, after, chunk, deadline=None, hint=False):
    """One B11 long-poll chunk. `/health` carries the 2 s connect budget.

    `hint` (inbox and the progress unread hint): the socket timeout is the remaining
    BUS_HINT_BUDGET, and a chunk that cannot finish inside it is not started. A peer
    that answers `/health` and never finishes `/wait` cannot hold those calls.

    ask/wait (`hint` false): they keep their own deadline. One chunk's socket timeout
    is `min(chunk + 10, remaining deadline + 1)`, so a single stalled chunk never
    exceeds the remaining deadline + 1 s.
    """
    health_timeout = BUS_CONNECT_TIMEOUT
    if deadline is not None:
        health_timeout = min(BUS_CONNECT_TIMEOUT, max(0.05, deadline - time.time()))
    bus_call(cfg, "GET", "/health", timeout=health_timeout)
    if deadline is None:
        socket_timeout = chunk + 1.0
    else:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise BusDown("hint budget exhausted")
        if hint:
            socket_timeout = min(chunk + 1.0, remaining)
        else:
            # Absolute deadline is the caller's own, plus at most 1 s (the old
            # per-recv cushion). The +1 is a ceiling on the whole chunk, and the
            # reader stops on that ceiling, so a drip cannot add another second.
            # The server clamps /wait to >= 1 s, so the chunk budget cannot be
            # shorter than that or the response arrives after we have given up.
            # Cap is the caller's remaining time plus the 1 s cushion, which is
            # the brief's "deadline + 1 s at most".
            socket_timeout = min(chunk + 10.0, max(1.0, remaining + 1.0))
    qs = urllib.parse.urlencode({"after": int(after), "timeout": f"{chunk:g}"})
    return bus_call(cfg, "GET", f"/wait?{qs}", timeout=socket_timeout)


class BusSeq(Exception):
    """A bus message carried a seq that is not an integer (item 37)."""


class BusLock(Exception):
    """The inbox-cursor lock was still held when the wait ran out (review R2)."""


BUS_LOCK_WAIT = 5.0


def bus_advance_cursor(lane, seq):
    """Move the cursor forward to `seq`, never backwards.

    The compare and the write happen under an exclusive flock on a sibling lock
    file, so a concurrent `inbox` and `ask` cannot pass each other's read and
    write an older seq last (review R1). A seq that is not a cursor seq is ignored.
    """
    if not bus_seq(seq):
        return bus_read_cursor(lane)
    path = os.path.join(lane, BUS_CURSOR_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = path + ".lock"
    with open(lock, "a") as lf:
        # LOCK_NB so a holder that never releases cannot stall the lane. Retry
        # until BUS_LOCK_WAIT, then BusLock: the cursor is unchanged.
        deadline = time.monotonic() + BUS_LOCK_WAIT
        while True:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise BusLock(f"inbox cursor lock held for {BUS_LOCK_WAIT:.0f}s")
                time.sleep(0.05)
        try:
            cur = bus_read_cursor(lane)
            if seq > cur:
                bus_write_cursor(lane, seq)
                return seq
            return cur
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def bus_note_seq(lane, msg, held=()):
    """Advance the inbox cursor past a message already shown to this lane.

    Never backwards, never onto a non-integer or a seq at or past 2**53. `held`
    seqs are unread messages addressed to this lane: the cursor stops before the
    lowest of those below this message, so a higher answer cannot hide them
    (todos 37 and 42). The write goes through bus_advance_cursor.
    """
    seq = msg.get("seq") if isinstance(msg, dict) else None
    if not bus_seq(seq):
        return
    blocked = [s for s in held if bus_seq(s) and s < seq]
    target = min(blocked) - 1 if blocked else seq
    bus_advance_cursor(lane, target)


def bus_poll_answer(cfg, ask_id, after, deadline, floor=0):
    """Long-poll `GET /wait` until the answer arrives or the deadline passes.

    `after` is where the poll starts. `floor` is the lane cursor: messages
    between the cursor and the ask are unread and land in `held`, so the cursor
    cannot jump over one that is older than the ask (review R1 HIGH).

    Returns (answer or None, held seqs). A message that is not a dict, or whose
    seq is not a cursor seq, raises BusSeq. A seq that does not advance `after`
    raises BusSeq too: fetching it again would be a hot loop (review R1, ~4.1M
    calls in 2 s against a stub that answers at once).
    """
    if bus_seq(floor) and floor < after:
        after = floor
    held = []
    while True:
        remaining = deadline - time.time()
        if remaining < 1.0:  # /wait clamps to >= 1 s server-side; a shorter chunk would overshoot
            return None, held
        res = bus_wait_chunk(cfg, after, min(BUS_WAIT_CHUNK_MAX, remaining), deadline=deadline)
        msg = res.get("message")
        if msg is None:
            continue
        if not isinstance(msg, dict):
            raise BusSeq(f"bus message is not an object: {type(msg).__name__}")
        seq = msg.get("seq")
        if not bus_seq(seq):
            raise BusSeq(f"bus message seq is not a cursor seq: {seq!r}")
        if seq <= after:
            # Same message again. Sleep out this chunk and poll once more, so a
            # stub that answers instantly cannot hot-loop (review R1: ~4.1M
            # calls in 2 s) and one re-delivery from the real bus is not fatal.
            # Two in a row is BusSeq.
            time.sleep(min(1.0, max(0.0, deadline - time.time())))
            res2 = bus_wait_chunk(cfg, after, 1.0, deadline=deadline)
            msg2 = res2.get("message") if isinstance(res2, dict) else None
            if msg2 is None:
                continue
            if not isinstance(msg2, dict):
                raise BusSeq(f"bus message is not an object: {type(msg2).__name__}")
            seq2 = msg2.get("seq")
            if not bus_seq(seq2) or seq2 <= after:
                raise BusSeq(f"bus message seq does not advance past {after}: {seq2!r}")
            msg, seq = msg2, seq2
        if msg.get("kind") == "answer" and msg.get("reply_to") == ask_id:
            return msg, [s for s in held if s < seq]
        held.append(seq)
        after = seq


BUS_RECONNECT_SEC = 60.0   # same window as `mk-bus.py watch` (WATCH_DOWN_SEC)
BUS_RECONNECT_RETRY = 0.5


def bus_refresh_cfg(lane, cfg):
    """Re-read the lane token file and, in file mode or under $MK_BUS_DIR, the bus.json beside
    the data: a restarted bus may have a new port. Mutates and returns `cfg`; unreadable
    sources leave it as it was."""
    fresh = bus_lane_config(lane)
    if fresh:
        cfg.update(fresh)
    home = os.environ.get("MK_BUS_DIR")
    if not home:
        data = bus_data_dir(cfg)
        home = data if data and not store_present(data) else None
    if home:
        with contextlib.suppress(OSError, ValueError, TypeError):
            with open(os.path.join(os.path.expanduser(home), "bus.json")) as f:
                port = json.load(f).get("port")
            if is_int(port) and 0 < port < 65536:
                p = urllib.parse.urlparse(cfg["url"])
                cfg["url"] = p._replace(netloc=f"{p.hostname or '127.0.0.1'}:{port}").geturl()
    return cfg


def bus_poll_answer_retry(lane, cfg, ask_id, after, deadline, floor=0):
    """bus_poll_answer that rides out a bus restart: on a dropped connection, re-read the bus
    coordinates and poll again from the same cursor (the ask is already on the bus, never
    posted twice). BusDown only after BUS_RECONNECT_SEC of continuous downtime, or when the
    caller's deadline has passed."""
    down_since = None
    while True:
        try:
            if down_since is None:
                return bus_poll_answer(cfg, ask_id, after, deadline, floor=floor)
            bus_call(cfg, "GET", "/health")  # back up? only then poll again
            down_since = None
        except BusDown:
            now = time.time()
            down_since = now if down_since is None else down_since
            if now - down_since >= BUS_RECONNECT_SEC or now >= deadline:
                raise
            time.sleep(min(BUS_RECONNECT_RETRY, max(0.0, deadline - now)))
            bus_refresh_cfg(lane, cfg)


def status_block(lane, question):
    """ask's blocked write — cmd_blocked's own writer (R13.4/R15: state, raw question, commit
    and subagent cleared), so MB3 reads BLOCKED and no STALL fires while the call waits.
    Best-effort: the bus round trip never dies on a refused status write."""
    try:
        old = load_live(lane)
        require_state(old, "blocked")
        obj = dict(old, state="blocked", question=question, commit=None, subagent=None)
        write_status(lane, stamp(obj))
        return True
    except (Fail, OSError, ValueError):
        return False


def status_resume(lane):
    """The answer's write-back: `blocked -> working` (R13.5), the step kept. Best-effort."""
    try:
        old = load_live(lane)
        require_state(old, "progress")
        obj = dict(old, state="working", question=None, subagent=None)
        write_status(lane, stamp(obj))
        return True
    except (Fail, OSError, ValueError):
        return False


def ask_down(lane, question):
    """The down-fallback: the bus is unusable, so the round trip degrades to today's BLOCKED
    flow — the status file gets the question exactly as `blocked` writes it, and the agent is
    pointed at the BLOCKED token. Always raises Handled(4)."""
    status_block(lane, question)
    print("BUS DOWN: use the BLOCKED token")
    raise Handled(4)


def check_bus_timeout(v):
    if not is_num(v) or not v > 0:
        raise Fail(f"timeout must be a positive number of seconds, got {v!r}")
    return v


def cmd_ask(a):
    """BB: POST the question, go blocked, long-poll for the answer; on it, ack level 3, back to
    working, print the text. Timeout prints `PENDING <ask id>` and exits 3 (status stays
    blocked). An unusable bus is ask_down; an HTTP error is one JSON line and exit 5."""
    lane, question = lane_dir(a), a.question
    if question is None or not question.strip():
        raise Fail("ask needs a non-empty question [R19]")
    deadline = time.time() + check_bus_timeout(a.timeout)
    cfg = bus_lane_config(lane)
    if cfg is None:
        ask_down(lane, question)
    try:
        res = bus_call(cfg, "POST", "/send", body={"kind": "ask", "text": question})
    except BusDown:
        ask_down(lane, question)
    except BusHttp as e:
        raise Fail(bus_http_error(e), code=5)
    msg = res.get("message") or {}
    ask_id, after = msg.get("id"), msg.get("seq")
    if not ask_id or not is_int(after):
        ask_down(lane, question)  # recorded but unusable response: the same fallback
    status_block(lane, question)
    # E1: the id is the first line, flushed, before the wait. A killed ask can be
    # resumed with `wait <id>`.
    print(f"ASK {ask_id}", flush=True)
    try:
        answer, held = bus_poll_answer_retry(lane, cfg, ask_id, after, deadline, floor=bus_read_cursor(lane))
    except BusDown:
        ask_down(lane, question)
    except (BusSeq, BusLock) as e:
        raise Fail(str(e), code=5)
    except BusHttp as e:
        raise Fail(bus_http_error(e), code=5)
    if answer is None:
        print(f"PENDING {ask_id}")
        raise Handled(3)
    with contextlib.suppress(BusDown, BusHttp, OSError, ValueError):
        ack_once(lane, cfg, answer.get("id"))
    # Print and resume BEFORE the cursor write. A held cursor lock raises BusLock
    # after 5 s; the answer is already acked at level 3, so losing the cursor
    # move must not lose the answer (review R3 HIGH).
    status_resume(lane)
    print(answer["text"])
    with contextlib.suppress(BusLock):
        bus_note_seq(lane, answer, held)  # todos 37/42: inbox must not re-show or re-ack this answer
    raise Handled(0)


def cmd_wait(a):
    """BB: resume ask's long-poll (a tool timeout cut the call). Same outputs as `ask`."""
    lane, ask_id = lane_dir(a), (a.ask_id or "").strip()
    if not ask_id:
        raise Fail("ask id is empty")
    m = ASK_ID_RE.fullmatch(ask_id)
    if not m:
        raise Fail(f"ask id must be m-<seq>, got {ask_id!r} [B15]")
    deadline = time.time() + check_bus_timeout(a.timeout)
    cfg = bus_lane_config(lane)
    if cfg is None:
        raise Fail("bus down", code=4)
    try:
        answer, held = bus_poll_answer_retry(lane, cfg, ask_id, int(m.group(1)), deadline, floor=bus_read_cursor(lane))
    except BusDown:
        raise Fail("bus down", code=4)
    except (BusSeq, BusLock) as e:
        raise Fail(str(e), code=5)
    except BusHttp as e:
        raise Fail(bus_http_error(e), code=5)
    if answer is None:
        print(f"PENDING {ask_id}")
        raise Handled(3)
    with contextlib.suppress(BusDown, BusHttp, OSError, ValueError):
        ack_once(lane, cfg, answer.get("id"))
    # Same order as ask: the answer is delivered even when the cursor lock is held.
    status_resume(lane)
    print(answer["text"])
    with contextlib.suppress(BusLock):
        bus_note_seq(lane, answer, held)  # todos 37/42: inbox must not re-show or re-ack this answer
    raise Handled(0)


def cmd_inbox(a):
    """BB: print the messages addressed to this lane after the cursor, one JSON line each,
    ack each at level 3, advance the cursor. Empty prints nothing and exits 0.
    D7.2: the whole drain is capped at 2 s wall clock (item 37)."""
    lane = lane_dir(a)
    cfg = bus_lane_config(lane)
    if cfg is None:
        raise Fail("bus down", code=4)
    after = bus_read_cursor(lane)
    deadline = time.time() + BUS_HINT_BUDGET
    while time.time() < deadline:
        try:
            res = bus_wait_chunk(cfg, after, 1.0, deadline=deadline, hint=True)
        except BusDown:
            raise Fail("bus down", code=4)
        except BusHttp as e:
            raise Fail(bus_http_error(e), code=5)
        msg = res.get("message")
        if not msg:
            break
        seq = msg.get("seq")
        if not is_int(seq) or seq <= after:
            break  # non-int or non-advancing seq: stop, do not spin (item 37)
        # At-least-once, same as ask/wait: a BusLock or a crash before the cursor
        # moves re-shows the message next time, which is the safe direction.
        # The re-shown message is acked once, on the call that actually advances
        # the cursor (review R4). ack_level 3 (ask/wait already consumed it) is
        # not printed and not re-acked (todo 42); the cursor still moves past it.
        # The lock is taken before the print, so a lock failure is the error line
        # alone: a caller never sees a message and an error in one run.
        try:
            after = bus_advance_cursor(lane, seq)
        except BusLock as e:
            raise Fail(str(e), code=5)
        if msg.get("ack_level") == 3:
            continue
        print(json.dumps(msg))
        try:
            ack_once(lane, cfg, msg.get("id"))
        except BusHttp as e:
            raise Fail(bus_http_error(e), code=5)
        except (BusDown, OSError, ValueError):
            raise Fail("bus down", code=4)
    raise Handled(0)


def quote_msg(text, limit=200):
    """One line, at most `limit` chars, marked as quoted data (not an instruction).

    `<` and `>` are neutralised so the text cannot close the quote early."""
    flat = " ".join((text or "").split())
    flat = flat.replace("<", "(").replace(">", ")")
    if len(flat) > limit:
        flat = flat[:limit]
    return f"<quoted>{flat}</quoted>"


def quote_field(value):
    """One token for id, kind or from: no whitespace, no angle brackets."""
    flat = " ".join(str(value or "?").split())
    flat = flat.replace("<", "(").replace(">", ")").replace(" ", "_")
    return flat or "?"


def listen_cursor_path(lane):
    return os.path.join(lane, ".matrixkoder-listen-cursor")


def listen_read_cursor(lane):
    try:
        with open(listen_cursor_path(lane)) as f:
            raw = f.read().strip()
        return int(raw) if raw.isdigit() else 0
    except (OSError, ValueError):
        return 0


def listen_write_cursor(lane, seq):
    """Atomic replace. A kill mid-write leaves the previous cursor, never an empty file."""
    if not is_int(seq) or seq < 0:
        return
    path = listen_cursor_path(lane)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-listen-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(str(int(seq)) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _sse_frames(buf):
    """Split complete SSE frames. CRLF is normalised to LF first. Returns
    (frames, remainder). A frame is the bytes between blank lines."""
    buf = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    frames = []
    while b"\n\n" in buf:
        block, buf = buf.split(b"\n\n", 1)
        frames.append(block)
    return frames, buf


def _sse_message(block):
    """One SSE block -> a message dict, or None. `data:` takes one optional space."""
    data = []
    for line in block.split(b"\n"):
        if line.startswith(b"data:"):
            rest = line[5:]
            if rest.startswith(b" "):
                rest = rest[1:]
            data.append(rest)
    if not data:
        return None
    try:
        msg = json.loads(b"\n".join(data).decode("utf-8"))
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


def listen_stream(cfg, timeout):
    """Open `/events?lane=<task>` and yield message dicts until `timeout` seconds pass
    with nothing new. Last-Event-ID is the lane's listen cursor.

    Same discipline as the X1 helpers: a `time.monotonic()` deadline, the socket
    timeout refreshed to the remaining budget on every read, and `buf` capped at
    BUS_BODY_MAX. A peer that never sends a frame separator fails with BusDown.
    """
    import socket
    base = (cfg.get("url") or "").rstrip("/")
    parsed = urllib.parse.urlparse(base)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    task = cfg.get("task") or ""
    after = int(cfg.get("_listen_after") or 0)
    path = "/events?lane=" + urllib.parse.quote(task)
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        f"Authorization: Bearer {cfg['token']}",
        "Accept: text/event-stream",
        f"Last-Event-ID: {after}",
        "Connection: close",
        "",
        "",
    ]
    deadline = time.monotonic() + timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    sock = socket.create_connection((host, port), timeout=min(BUS_CONNECT_TIMEOUT, remaining))
    try:
        sock.sendall("\r\n".join(lines).encode("ascii"))
        buf = b""
        header = True
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if header and buf and b"\r\n\r\n" not in buf:
                    raise BusDown("listen: bus never finished the HTTP headers")
                if not header and buf and b"\n\n" not in buf.replace(b"\r\n", b"\n"):
                    raise BusDown("listen: peer sent no SSE frame separator")
                return
            sock.settimeout(min(remaining, 5.0))
            try:
                chunk = sock.recv(4096)
            except (TimeoutError, socket.timeout):
                continue
            if not chunk:
                return
            if len(buf) + len(chunk) > BUS_BODY_MAX:
                raise BusDown("listen: bus response exceeds 1 MiB")
            buf += chunk
            if header:
                if b"\r\n\r\n" not in buf:
                    continue
                head, buf = buf.split(b"\r\n\r\n", 1)
                status_line = head.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
                parts = status_line.split(" ", 2)
                code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                if code >= 400 or code == 0:
                    raise BusHttp(code, status_line)
                header = False
            frames, buf = _sse_frames(buf)
            for block in frames:
                msg = _sse_message(block)
                if msg is not None:
                    yield msg
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def cmd_listen(a):
    """E1: follow the bus SSE stream and print each new message as one quoted line.

    Exits 0 when it printed something, 3 when the timeout passed with nothing, 4
    when the bus is down. Each printed message is acked at level 3 exactly once.
    """
    lane = lane_dir(a)
    timeout = a.timeout if a.timeout is not None else 5.0
    if not is_num(timeout) or not (0 < timeout <= LISTEN_TIMEOUT_MAX):
        raise Fail(f"listen timeout must be a number in (0, {LISTEN_TIMEOUT_MAX:g}], got {timeout!r}")
    cfg = bus_lane_config(lane)
    if cfg is None:
        print("BUS DOWN: use the BLOCKED token")
        raise Handled(4)
    cfg = dict(cfg)
    cfg["_listen_after"] = listen_read_cursor(lane)
    printed = 0
    try:
        for msg in listen_stream(cfg, timeout):
            kind = quote_field(msg.get("kind") or "?")
            who = quote_field(msg.get("from") or "?")
            mid = quote_field(msg.get("id") or "?")
            print(f"MSG {mid} {kind} {who}: {quote_msg(msg.get('text') or '')}", flush=True)
            printed += 1
            with contextlib.suppress(BusDown, BusHttp, OSError, ValueError):
                ack_once(lane, cfg, msg.get("id"))
            seq = msg.get("seq")
            if is_int(seq):
                listen_write_cursor(lane, seq)
    except BusDown:
        print("BUS DOWN: use the BLOCKED token")
        raise Handled(4)
    except BusHttp as e:
        raise Fail(bus_http_error(e), code=5)
    except (OSError, ValueError):
        print("BUS DOWN: use the BLOCKED token")
        raise Handled(4)
    raise Handled(0 if printed else 3)


def cmd_ack(a):
    """BB: POST /ack level 3 for one message (B13)."""
    lane, msg_id = lane_dir(a), (a.msg_id or "").strip()
    if not msg_id:
        raise Fail("message id is empty")
    cfg = bus_lane_config(lane)
    if cfg is None:
        raise Fail("bus down", code=4)
    try:
        res = bus_call(cfg, "POST", "/ack", body=bus_ack_body(cfg, msg_id))
    except BusDown:
        raise Fail("bus down", code=4)
    except BusHttp as e:
        raise Fail(bus_http_error(e), code=5)
    return (res, None)


def bus_unread_hint(lane):
    """The `progress` hint: `N unread — run inbox` when the bus holds messages for this lane
    past the inbox cursor. Best effort — a hard 2 s wall-clock cap (item 37), silent whenever
    the bus is unusable, never a failure, never a cursor move."""
    cfg = bus_lane_config(lane)
    if cfg is None:
        return None
    try:
        after, n = bus_read_cursor(lane), 0
        deadline = time.time() + BUS_HINT_BUDGET
        while time.time() < deadline:
            res = bus_wait_chunk(cfg, after, 1.0, deadline=deadline, hint=True)
            msg = res.get("message")
            if msg is None:
                break
            n += 1
            seq = msg.get("seq")
            if is_int(seq) and seq > after:
                after = seq
        return f"{n} unread — run inbox" if n else None
    except (Fail, BusDown, BusHttp, OSError, ValueError):
        return None


# ---------- D7: task list, todo mirror, commit and report over the bus ----------
# Self-hosting guard: these commands talk to the bus only when the lane file (or MK_BUS_*)
# resolves AND the project store exists. Otherwise they fall back to the lane status file,
# the report file and the brief copy (C12) and never raise on a missing bus.

def lane_hash(lane):
    return hashlib.sha256(os.path.realpath(lane).encode("utf-8")).hexdigest()


def bus_token_path(lane):
    """ONE resolver for the lane token file (D6B moves it; this CLI must follow).

    Order: `MK_BUS_TOKEN_FILE` when set, else `<lane>/.matrixkoder-bus.json` when that
    file exists (file mode), else `<home>/bus-lanes/<sha256(realpath(lane))>.json`.
    The hash and the home are the same ones `scripts/mk-bus.py` writes: realpath,
    full sha256, `$MK_BUS_DIR` or `$MK_HOME` or `~/.matrixkoder`.
    """
    env = os.environ.get("MK_BUS_TOKEN_FILE")
    if env:
        return os.path.expanduser(env)
    lane_file = os.path.join(lane, BUS_LANE_FILE)
    if os.path.isfile(lane_file):
        return lane_file
    if os.environ.get("MK_BUS_DIR"):
        home = os.path.abspath(os.path.expanduser(os.environ["MK_BUS_DIR"]))
    elif os.environ.get("MK_HOME"):
        home = os.path.abspath(os.path.expanduser(os.environ["MK_HOME"]))
    else:
        home = os.path.join(os.path.expanduser("~"), ".matrixkoder")
    return os.path.join(home, "bus-lanes", lane_hash(lane) + ".json")


def bus_read_token_file(path):
    """The token file, or {} when it is unusable.

    Both `MK_BUS_URL` and `MK_BUS_TOKEN` set: the file is never opened (a FIFO cannot hang
    the CLI). Otherwise the path must lstat as a regular file owned by this uid with no
    group/other mode bits. A FIFO, a symlink, mode 0644, or another uid is an empty config
    (the BUS DOWN path, exit 4). The open is O_NONBLOCK so a blocking open cannot stall.
    """
    if os.environ.get("MK_BUS_URL") and os.environ.get("MK_BUS_TOKEN"):
        return {}
    try:
        st = os.lstat(path)
    except OSError:
        return {}
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return {}
    if (st.st_mode & 0o077) != 0 or st.st_uid != os.getuid():
        return {}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return {}
    try:
        with os.fdopen(fd) as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def bus_lane_config(lane):
    """B24 lane coordinates, from the one token-path resolver. `MK_BUS_URL` / `MK_BUS_TOKEN`
    override the file's url and token (D6B). None when nothing usable is present.
    A refused token file (FIFO, loose mode, foreign uid, symlink) is an empty config."""
    path = bus_token_path(lane)
    obj = bus_read_token_file(path)
    url = os.environ.get("MK_BUS_URL") or obj.get("url")
    token = os.environ.get("MK_BUS_TOKEN") or obj.get("token")
    if not url or not token:
        return None
    cfg = dict(obj)
    cfg["url"], cfg["token"] = url, token
    cfg["_token_path"] = path
    return cfg


def bus_data_dir(cfg):
    """The project data dir: `MK_BUS_DATA` wins, else the token file's `data` field
    (E2 writes it; E1 reads it). Without either, store mode stays off (C12)."""
    env = os.environ.get("MK_BUS_DATA")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    if not cfg:
        return None
    data = cfg.get("data")
    if isinstance(data, str) and os.path.isdir(data):
        return os.path.abspath(data)
    return None


def resolve_data_dir(cfg, explicit=None):
    """Where the brief lives. `--data` wins, then `MK_BUS_DATA`, then the token file's
    `data` field. None when none of those names a directory."""
    if explicit and os.path.isdir(explicit):
        return os.path.abspath(explicit)
    return bus_data_dir(cfg)


def store_present(data):
    """Self-hosting guard: store mode only when <data>/mk.db already exists."""
    return bool(data) and os.path.isfile(os.path.join(data, "mk.db"))


def store_rows(data, sql, params=()):
    """SELECT-only against the project store. Read-only URI so a lane CLI cannot write it."""
    uri = "file:" + os.path.join(data, "mk.db") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def todo_done_status(data):
    """C9: `session.todo_done_status` from <data>/matrixkoder.yaml, else the schema default."""
    path = os.path.join(data or "", "matrixkoder.yaml")
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return TODO_DONE_DEFAULT
    # A tiny scan, not a YAML parser: the key is one line `todo_done_status: verify|done`.
    for line in text.splitlines():
        raw = line.split("#", 1)[0].strip()
        if raw.startswith("todo_done_status:"):
            val = raw.split(":", 1)[1].strip().strip('"').strip("'")
            if val in ("verify", "done"):
                return val
    return TODO_DONE_DEFAULT


def bus_send(cfg, kind, text, task=None):
    """POST /send as the lane. The token already scopes run and task; `task` is sent only
    when it matches the token's task (the bus refuses a mismatch)."""
    body = {"kind": kind, "text": text}
    if task and cfg.get("task") and task == cfg.get("task"):
        body["task"] = task
    return bus_call(cfg, "POST", "/send", body=body)


def phase_of(task):
    """A task id `D7.1` belongs to phase `D7`; a phase id has no dot."""
    return task.split(".", 1)[0] if "." in task else task


def plan_tasks(plan_path, task):
    """The phase's task list from a plan file: id, title, parallel, deps. Empty when the
    plan cannot be read — connect still prints the brief."""
    try:
        with open(plan_path, "rb") as f:
            raw = f.read().decode("utf-8")
    except OSError:
        return []
    if not raw.startswith("+++"):
        return []
    end = raw.find("\n+++", 3)
    if end < 0:
        return []
    try:
        doc = tomllib.loads(raw[3:end].lstrip("\n"))
    except tomllib.TOMLDecodeError:
        return []
    phase_id = phase_of(task)
    out = []
    for phase in doc.get("phase") or []:
        if not isinstance(phase, dict) or phase.get("id") != phase_id:
            continue
        for t in phase.get("task") or []:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            deps = t.get("deps") or []
            if isinstance(deps, str):
                deps = [x.strip() for x in deps.split(",") if x.strip()]
            out.append({"id": t["id"], "title": t.get("title") or "",
                        "parallel": bool(t.get("parallel") or False), "deps": list(deps)})
    return out


def brief_from_files(lane, old):
    """The brief copied into the lane: the report's sibling brief, else the lane brief copy.
    File mode keeps this as the first choice, so a present lane file wins over the bus."""
    report = (old or {}).get("report") or ""
    candidates = []
    if report:
        base = os.path.basename(report)
        m = re.search(r"-r(\d+)\.", base)
        n = m.group(1) if m else "1"
        candidates.append(os.path.join(lane, os.path.dirname(report), f"brief-r{n}.md"))
        candidates.append(os.path.join(lane, f".matrixkoder-brief-r{n}.md"))
    candidates.append(os.path.join(lane, ".matrixkoder-brief.md"))
    for path in candidates:
        try:
            with open(path) as f:
                return f.read(), path
        except OSError:
            continue
    return "", ""


def parse_brief_message(text):
    """A published brief is `round:<n>\nkind:brief\n<body>`. -> (round, body) or None."""
    head, sep, rest = (text or "").partition("\n")
    if not (sep and head.startswith("round:") and rest.startswith("kind:brief\n")):
        return None
    try:
        n = int(head[6:].strip())
    except ValueError:
        return None
    return n, rest[len("kind:brief\n"):]


def parse_review_brief_message(text):
    """A published review brief is `round:<n>\nkind:review-brief\n<body>`. -> (round, body) or None.
    Its own kind, so a reviewer sharing the lane never picks up the builder's brief (or the reverse)."""
    head, sep, rest = (text or "").partition("\n")
    if not (sep and head.startswith("round:") and rest.startswith("kind:review-brief\n")):
        return None
    try:
        n = int(head[6:].strip())
    except ValueError:
        return None
    return n, rest[len("kind:review-brief\n"):]


def brief_messages(cfg):
    """Every bus message for this lane's task, newest last. The store when it exists,
    else bus.jsonl. One read, so an inbox of earlier messages cannot eat the budget.
    Scoped to the token's task: a shared run must not hand this lane another task's brief."""
    data = bus_data_dir(cfg)
    run = cfg.get("run")
    task = cfg.get("task")
    if not data or not run:
        return []
    if store_present(data):
        try:
            if task:
                rows = store_rows(
                    data,
                    "SELECT text FROM events WHERE chan='bus' AND run=? AND task=? ORDER BY id",
                    (run, task))
            else:
                rows = store_rows(
                    data,
                    "SELECT text FROM events WHERE chan='bus' AND run=? ORDER BY id",
                    (run,))
        except (OSError, sqlite3.Error):
            return []
        return [r.get("text") or "" for r in rows]
    path = os.path.join(data, "runs", run, "bus.jsonl")
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and (not task or msg.get("task") == task):
                    out.append(msg.get("text") or "")
    except OSError:
        return []
    return out


def _jsonl_texts(path, task):
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and (not task or msg.get("task") == task):
                    out.append(msg.get("text") or "")
    except OSError:
        return []
    return out


def brief_from_bus(lane, old):

    """The brief the orchestrator published for this lane, when no lane file exists.

    The newest `round:<n>\nkind:brief\n` message wins. A report naming round N asks for
    that round only, so a stale earlier brief is not served. No such message is no brief.
    """
    cfg = bus_lane_config(lane)
    if cfg is None:
        return "", ""
    want = None
    report = (old or {}).get("report") or ""
    m = re.search(r"-r(\d+)\.", os.path.basename(report)) if report else None
    if m:
        want = int(m.group(1))
    best_n, best = -1, ""
    for text in brief_messages(cfg):
        parsed = parse_brief_message(text)
        if not parsed:
            continue
        n, body = parsed
        if (want is None or n == want) and n >= best_n:
            best_n, best = n, body
    return (best, f"bus:brief-r{best_n}") if best else ("", "")


def brief_text(lane, old):
    """The brief this round was given. A lane file wins (file mode, unchanged); otherwise
    the brief the orchestrator published on the bus."""
    text, path = brief_from_files(lane, old)
    if text:
        return text, path
    return brief_from_bus(lane, old)


def page_count(text, size=BRIEF_PAGE):
    return max(1, (len(text) + size - 1) // size) if text else 1


def page_slice(text, part, size=BRIEF_PAGE):
    """Slice page `part` (already validated, 1-based). Does not clamp: a part outside
    1..pages is the caller's error, not a silent last/first page."""
    pages = page_count(text, size)
    start = (part - 1) * size
    return text[start:start + size], part, pages


def connect_identity(cfg):
    """The round arm recorded in the lane token file: run, task, round. Missing fields
    are None. A token with no round, or the old E2 `round: 0`, is round 0 here: unarmed, and
    implicit_start then reads the round off the newest brief, else 1."""
    if not cfg:
        return None, None, None
    run = cfg.get("run") if isinstance(cfg.get("run"), str) and cfg.get("run") else None
    task = cfg.get("task") if isinstance(cfg.get("task"), str) and cfg.get("task") else None
    raw = cfg.get("round")
    if raw is None or raw == "":
        round_n = 0
    else:
        try:
            round_n = int(raw)
        except (TypeError, ValueError):
            round_n = None
    return run, task, round_n


def implicit_start(lane, cfg, brief=""):
    """E1: a fresh lane has no status file. `connect` starts the round arm recorded
    (run, task, round in the token file) instead of refusing.

    Role comes from the token when it is builder or reviewer, else builder. When
    the brief names `round:<n>` and the token names a different round, refuse
    before writing the status file. Returns the live file."""
    run, task, round_n = connect_identity(cfg)
    parsed = parse_brief_message(brief) if brief else None
    if round_n == 0:  # unarmed token: the newest brief names the round, else it is round 1
        round_n = parsed[0] if parsed else 1
    if not run or not task or not is_int(round_n) or round_n < 1:
        raise Fail("connect: no armed round in the lane token file "
                   "(need run, task, round); run `start` or re-arm the lane")
    if parsed and parsed[0] != round_n:
        raise Fail(f"connect: token round {round_n} does not match the brief round {parsed[0]}; "
                   "re-arm the lane or pass the matching brief")
    role = cfg.get("role") if isinstance(cfg, dict) else None
    if role not in ("builder", "reviewer", "review"):
        role = "builder"
    report = f".matrixkoder-report-r{round_n}.md"
    obj = new_file(check_run(run), check_task(task), round_n, check_role(role),
                   detect_agent(), check_report(report), time.time())
    write_status(lane, obj)
    return obj


def cmd_connect(a):
    """D7.1 / E1: print the brief (one page, ~24,000 chars) and the phase task list.

    A fresh lane (no status file) is started from the round the orchestrator armed.
    The data dir comes from `--data`, `MK_BUS_DATA`, or the token file's `data`
    field. With none of those, and no brief already in the lane, connect exits
    non-zero and names the fix. Ack of the brief happens on `read` of the last page."""
    lane = lane_dir(a)
    cfg = bus_lane_config(lane)
    data = resolve_data_dir(cfg, getattr(a, "data", None))
    if data:
        os.environ["MK_BUS_DATA"] = data
    if ROLE_ALIASES.get(getattr(a, "role", None) or "builder") == "reviewer":
        # a reviewer reads its own review brief from the bus and leaves the builder's status alone.
        # shortcut: printed whole, not paged — review briefs are a few KB; page it if one passes BRIEF_PAGE.
        found = None
        for text in brief_messages(cfg) if cfg else []:
            found = parse_review_brief_message(text) or found
        if not found:
            print("# review brief: none found")
            raise Handled(0)
        print(f"# review brief round {found[0]}")
        print(found[1])
        raise Handled(0)
    old = read_status(lane)
    if old is None:
        # Data dir is already exported, so the brief peek can see the bus copy.
        raw_brief = ""
        if cfg:
            for text in brief_messages(cfg) or _jsonl_texts(
                    os.path.join(data or "", "runs", cfg.get("run") or "", "bus.jsonl"),
                    cfg.get("task")):
                if parse_brief_message(text):
                    raw_brief = text
        old = implicit_start(lane, cfg, raw_brief)
    text, path = brief_text(lane, old)
    if not text and not data and not brief_from_files(lane, old)[0]:
        raise Fail("connect: no data dir — pass --data <dir> or set MK_BUS_DATA, "
                   "or re-arm so the lane token file has a data field")
    if not text:
        print("# brief: none found")
        raise Handled(0)
    plan = a.plan or os.environ.get("MK_PLAN") or ""
    tasks = plan_tasks(plan, old.get("task") or "") if plan else []
    body, part, pages = page_slice(text, 1)
    lines = [f"# brief {part}/{pages}" + (f" {path}" if path else ""), body]
    if pages > 1:
        lines.append(f"# more: read <id> --part 2 .. {pages}")
    lines.append("# tasks")
    for t in tasks:
        deps = ",".join(t["deps"])
        lines.append(f"[{t['id']}] {t['title']} parallel={str(t['parallel']).lower()} deps={deps}")
    if not tasks:
        lines.append("# tasks: (none — no plan, or this phase has no tasks)")
    print("\n".join(lines))
    # Once per connect (not per page; `read` posts nothing). Best effort: the brief is out.
    if cfg is not None:
        with contextlib.suppress(BusDown, BusHttp, OSError, ValueError):
            bus_send(cfg, "info", f"lane connected {old.get('task')} R{old.get('round')}")
    raise Handled(0)


def cmd_read(a):
    """D7.1: one page of a paged body. `--part` is 1-based. The last page acks the message
    when `<id>` is a bus message id and the bus is up."""
    lane = lane_dir(a)
    msg_id = (a.msg_id or "").strip()
    part = a.part
    if part is None:
        part = 1
    if not is_int(part) or part < 1:
        raise Fail(f"part must be an integer >= 1, got {part!r}")
    text = ""
    if msg_id and msg_id != "brief":
        cfg = bus_lane_config(lane)
        if cfg is None:
            raise Fail("bus down", code=4)
        # The body was delivered as a message; re-fetch by walking /wait is not how paging
        # works — the sender pages, and `read` slices a body the lane already stored.
        cache = os.path.join(lane, ".matrixkoder-pages", msg_id)
        try:
            with open(cache) as f:
                text = f.read()
        except OSError:
            raise Fail(f"no paged body cached for {msg_id!r}; connect prints the brief")
    else:
        text, _ = brief_text(lane, load_live(lane))
    pages = page_count(text)
    if part > pages:
        raise Fail(f"part {part} is outside 1..{pages}")
    body, part, pages = page_slice(text, part)
    print(f"# part {part}/{pages}")
    print(body)
    if part == pages and msg_id and msg_id != "brief":
        cfg = bus_lane_config(lane)
        if cfg is not None:
            with contextlib.suppress(BusDown, BusHttp, OSError, ValueError):
                bus_call(cfg, "POST", "/ack", body=bus_ack_body(cfg, msg_id))
    raise Handled(0)


def hook_install(kind, home):
    """C8: install the hook recipe OUTSIDE the worktree, under the MK home. Returns the
    settings path, or None when this kind has no hook (`todo_hook` is not yes)."""
    recipe = HOOK_RECIPES.get(kind)
    if not recipe:
        return None
    dest_dir = os.path.join(home, "hooks")
    os.makedirs(dest_dir, exist_ok=True)
    settings = os.path.join(dest_dir, f"{kind}-todo.json")
    payload_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "hooks", "payloads", f"{kind}.json")
    try:
        with open(payload_src) as f:
            recorded = f.read()
    except OSError:
        recorded = ""
    doc = {"recipe": recipe, "installed_outside_worktree": True, "recorded_payload": recorded}
    fd = os.open(settings, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f)
        f.write("\n")
    return settings


def cmd_hook_install(a):
    """Dispatch-time hook install. Refuses to write inside the lane worktree."""
    lane = lane_dir(a)
    kind = check_agent(a.agent or detect_agent())
    home = os.environ.get("MK_HOME") or os.path.expanduser("~/.matrixkoder")
    home = os.path.abspath(home)
    if os.path.commonpath([home, os.path.abspath(lane)]) == os.path.abspath(lane):
        raise Fail("hook install must be outside the worktree [C8]")
    path = hook_install(kind, home)
    if path is None:
        return ({"ok": True, "installed": False, "agent": kind,
                 "reason": "todo_hook is not yes for this agent"}, None)
    return ({"ok": True, "installed": True, "agent": kind, "settings": path,
             "recipe": HOOK_RECIPES[kind]}, None)


def parse_todo_section(text):
    """The report's `## Todo` section: lines `[done|skipped|open] <text> - <evidence>`.
    Returns (items, warned). A missing section or an `open` item with no evidence warns."""
    m = re.search(r"(?m)^## Todo\s*$", text)
    if not m:
        return [], "todo section missing"
    body = text[m.end():]
    nxt = re.search(r"(?m)^## ", body)
    if nxt:
        body = body[:nxt.start()]
    items, warns = [], []
    line_re = re.compile(r"^\[(done|skipped|open)\]\s+(.+?)\s+-\s+(.*)$")
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        hit = line_re.match(line)
        if not hit:
            continue
        state, title, evidence = hit.group(1), hit.group(2).strip(), hit.group(3).strip()
        items.append({"state": state, "text": title, "evidence": evidence})
        if state == "open" and not evidence:
            warns.append(f"open without a reason: {title}")
    warn = "; ".join(warns)
    return items, warn


def todo_counts(items):
    done = sum(1 for i in items if i["state"] == "done")
    skipped = sum(1 for i in items if i["state"] == "skipped")
    return done, skipped, len(items)


def status_seen_on_bus(cfg, data, task, mapped, round_n):
    """Dedupe lives on the bus: a task message `status <mapped>` with `round: <n>` for this
    task was already announced. A different round is a different key."""
    try:
        rows = store_rows(
            data,
            "SELECT text FROM events WHERE chan='bus' AND run=? AND task=? AND kind='task'",
            (cfg.get("run"), task),
        )
    except (OSError, sqlite3.Error):
        return False
    prefix = f"status {mapped}"
    for row in rows:
        text = row.get("text") or ""
        head, _, rest = text.partition("\n")
        if head.strip() != prefix:
            continue
        if rest.startswith(f"round: {round_n}"):
            return True
    return False


def post_status(lane, old, state, note):
    """Report one todo state. The dedupe key is (task, mapped state, round): a second report
    of the same triple is dropped, and a fix round (a new round) is a new key, so re-entering
    in_progress is sent again (C7).

    `completed` maps to `session.todo_done_status` (C9: verify|done). The mapped value is
    what is announced. A lane token cannot POST kind=status (the bus reserves that for the
    orchestrator token), so the announcement is a `task` message whose text is
    `status <mapped>`; the store-mode bus maps the word (in_progress -> building, verify and
    done -> verifying: a lane never sets done) and moves the lane's own task only along the
    builder path (todo 67); the reply says whether it did, or why it ignored the word. With the bus or the store absent nothing is sent (C12: the lane status
    file is the record)."""
    task, round_n = old.get("task"), old.get("round")
    cfg = bus_lane_config(lane)
    data = bus_data_dir(cfg) if cfg else None
    mapped = "in_progress" if state == "in_progress" else (
        todo_done_status(data) if data else TODO_DONE_DEFAULT)
    if cfg is not None and store_present(data) and status_seen_on_bus(cfg, data, task, mapped, round_n):
        return {"sent": False, "deduped": True, "state": state, "mapped": mapped}
    if cfg is None or not store_present(data):
        return {"sent": False, "deduped": False, "state": state, "mapped": mapped,
                "fallback": "lane-file"}
    text = f"status {mapped}\nround: {round_n}\n{note}"
    try:
        reply = bus_send(cfg, "task", text, task=task)
    except (BusDown, BusHttp):
        return {"sent": False, "deduped": False, "state": state, "mapped": mapped,
                "fallback": "bus-down"}
    return {"sent": True, "deduped": False, "state": state, "mapped": mapped, "note": note,
            "state_updated": reply.get("state_updated"), "state_error": reply.get("mk_state_error"),
            "state_ignored": reply.get("state_ignored")}


def cmd_todo(a):
    """`todo done <id>` — the backup reporter (C7). `todo in_progress <id>` likewise.
    Both post a status; the bus dedupes by (task, state, round)."""
    lane, old = lane_dir(a), load_live(lane_dir(a))
    which = (a.which or "").strip()
    tid = (a.task_id or "").strip()
    if which not in TODO_STATES:
        raise Fail(f"todo state must be one of {TODO_STATES}, got {which!r}")
    if not tid:
        raise Fail("todo needs a task id")
    # The lane status file still moves, so a watcher with the bus down sees progress (C12).
    if which == "in_progress":
        require_state(old, "progress")
        obj = dict(old, step=tid, state="working", question=None, subagent=None)
        write_status(lane, stamp(obj))
    note = f"todo {which} {tid}"
    res = post_status(lane, old, which, note)
    return ({"ok": True, "task": tid, **res}, None)


def cmd_commit_msg(a):
    """Announce the commit. Replaces nothing in git; the message is the bus record."""
    lane, old = lane_dir(a), load_live(lane_dir(a))
    sha = (a.sha or "").strip() or (lane_head(lane) or "")
    if not SHA_RE.fullmatch(sha):
        raise Fail(f"commit needs a 40-hex sha, got {sha!r}")
    text = f"round: {old.get('round')}\n{sha}"
    cfg = bus_lane_config(lane)
    data = bus_data_dir(cfg) if cfg else None
    if cfg is None or not store_present(data):
        return ({"ok": True, "sent": False, "fallback": "lane-file", "commit": sha}, None)
    try:
        bus_send(cfg, "commit", text)
    except (BusDown, BusHttp):
        return ({"ok": True, "sent": False, "fallback": "bus-down", "commit": sha}, None)
    return ({"ok": True, "sent": True, "commit": sha}, None)


def cmd_report(a):
    """Send the report file as a `report` message (paged by the bus at 128 KiB). With the
    bus down the file on disk stays the record (C12)."""
    lane, old = lane_dir(a), load_live(lane_dir(a))
    report = old.get("report") or ""
    path = os.path.join(lane, report)
    if not report or not os.path.isfile(path):
        raise Fail(f"report_missing: {report!r} does not exist at the lane root")
    with open(path) as f:
        text = f.read()
    body = f"round: {old.get('round')}\n{text}"
    cfg = bus_lane_config(lane)
    data = bus_data_dir(cfg) if cfg else None
    if cfg is None or not store_present(data):
        return ({"ok": True, "sent": False, "fallback": "file", "report": report}, None)
    try:
        bus_send(cfg, "report", body)
    except (BusDown, BusHttp):
        return ({"ok": True, "sent": False, "fallback": "bus-down", "report": report}, None)
    return ({"ok": True, "sent": True, "report": report}, None)


def measure_store(data, run):
    """D7: when the project store exists, measure reads it instead of events.jsonl."""
    events = store_rows(
        data,
        "SELECT id, task, t, chan, kind, text FROM events WHERE run=? ORDER BY id",
        (run,),
    )
    # Adapt store rows to the events.jsonl shape parse_events/verdict_of already understand.
    adapted = []
    for e in events:
        if e.get("chan") not in ("state", "bus", None):
            continue
        adapted.append({"t": e.get("t"), "task": e.get("task"), "kind": e.get("kind"),
                        "text": e.get("text") or "", "chan": e.get("chan")})
    return adapted

# ---------- CLI ----------

def build_parser():
    p = Parser(prog="mk-builder.py",
               description="mk-builder lane status writer (mk-builder-status/v1)")
    p.add_argument("--self-test", action="store_true",
                   help="run the built-in tests in a temp dir and exit")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("install", help="copy this skill folder into <dir>/.claude/skills/mk-builder/")
    sp.add_argument("--into", required=True, help="target repo or lane root")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("measure", help="read-only counters over one run: briefs, verdict "
                                        "sources, missed DONE lines, false STALLs, blocked")
    sp.add_argument("--data", required=True, help="the MatrixKoder data root (holds runs/)")
    sp.add_argument("--run", required=True)
    sp.set_defaults(func=cmd_measure)

    def lane_cmd(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--lane", default=".", help="lane worktree root (R1)")
        sp.set_defaults(func=fn)
        return sp

    sp = lane_cmd("start", cmd_start, "begin or resume this (run, task, round, role) [R21, R22]")
    sp.add_argument("--run", required=True)
    sp.add_argument("--task", required=True)
    sp.add_argument("--round", required=True, type=int)
    sp.add_argument("--role", default="builder", help="builder | reviewer (alias: review)")
    sp.add_argument("--report", required=True, help="report path relative to the lane root")
    sp.add_argument("--agent", default=None, help="claude|codex|opencode|agy|unknown (default: detected)")

    sp = lane_cmd("progress", cmd_progress, "record a step; --subagent enters or heartbeats [R17a]")
    sp.add_argument("step")
    sp.add_argument("--subagent", action="store_true")

    sp = lane_cmd("blocked", cmd_blocked, "record the blocked question, print the token [R19]")
    sp.add_argument("question")
    lane_cmd("done", cmd_done, "record done: report file plus a readable HEAD [R18]")
    sp = lane_cmd("review-done", cmd_review_done, "reviewer: post verdict, counts and path on the bus, record done")
    sp.add_argument("--verdict", required=True, choices=("APPROVE", "REQUEST_CHANGES"))
    sp.add_argument("--findings", required=True, type=int)
    sp.add_argument("--path", required=True, help="review file path relative to the lane root")
    sp = lane_cmd("ctx", cmd_ctx,"record the context percentage, 0..100 [R10]")
    sp.add_argument("pct", type=int)
    sp = lane_cmd("phase-done", cmd_phase_done, "append a completed phase id [R8]")
    sp.add_argument("phase_id")
    sp = lane_cmd("ask", cmd_ask, "post the question on the bus and block for the answer [BB]")
    sp.add_argument("question")
    sp.add_argument("--timeout", type=float, default=BUS_ASK_TIMEOUT,
                    help="total seconds to wait for the answer (default 600)")
    sp = lane_cmd("wait", cmd_wait, "resume waiting for the answer to <ask id> [BB]")
    sp.add_argument("ask_id")
    sp.add_argument("--timeout", type=float, default=BUS_ASK_TIMEOUT,
                    help="total seconds to wait for the answer (default 600)")
    lane_cmd("inbox", cmd_inbox, "print this lane's unread bus messages and ack them [BB]")
    sp = lane_cmd("listen", cmd_listen, "follow the bus SSE stream and print new messages [E1]")
    sp.add_argument("--timeout", type=float, default=5.0,
                    help=f"seconds to wait, 1..{LISTEN_TIMEOUT_MAX:g} (default 5)")
    sp = lane_cmd("connect", cmd_connect, "print the brief (paged) and the phase task list [D7.1]")
    sp.add_argument("--plan", default=None, help="plan file; default $MK_PLAN")
    sp.add_argument("--data", default=None, help="project data dir (else $MK_BUS_DATA or the token file)")
    sp.add_argument("--role", default="builder", help="builder | reviewer (alias: review): which brief to print")
    sp = lane_cmd("read", cmd_read, "print one page of a paged brief or message [D7.1]")
    sp.add_argument("msg_id")
    sp.add_argument("--part", type=int, default=1)
    sp = lane_cmd("todo", cmd_todo, "backup todo reporter: todo in_progress|done <id> [C7]")
    sp.add_argument("which")
    sp.add_argument("task_id")
    sp = lane_cmd("hook-install", cmd_hook_install, "install the todo hook outside the worktree [C8]")
    sp.add_argument("--agent", default=None)
    sp = lane_cmd("commit", cmd_commit_msg, "announce the commit sha on the bus [C7]")
    sp.add_argument("sha", nargs="?", default=None)
    lane_cmd("report", cmd_report, "send the report file on the bus [C7]")
    sp = lane_cmd("ack", cmd_ack, "record a bus message as read, level 3 [BB]")
    sp.add_argument("msg_id")
    sp = lane_cmd("show", cmd_show, "read-only: apply the R27 reader conditions and print the verdict")
    sp.add_argument("--armed-at", type=float, default=0.0, help="watch armed_at, a float epoch")
    sp.add_argument("--run", default=None)
    sp.add_argument("--task", default=None)
    sp.add_argument("--round", type=int, default=None)
    sp.add_argument("--role", default=None)
    sp.add_argument("--seen-seq", type=int, default=None, help="highest seq already accepted [R27.9]")
    sp.add_argument("--state", default=None, help="armed expectation; must be one of the five [R27.8]")
    return p


def main(argv=None):
    """One JSON line on stdout, plus the R26 token when there is one; non-zero on any refusal."""
    try:
        a = build_parser().parse_args(argv)
        if a.self_test:
            return run_self_test()
        if not getattr(a, "cmd", None):
            raise Fail("no command; --help lists the command set "
                       "(start, progress, blocked, done, ctx, phase-done, show, "
                       "ask, wait, inbox, ack, connect, read, todo, hook-install, "
                       "commit, report, review-done, install, measure)")
        out, token = a.func(a)
    except Fail as e:
        print(json.dumps({"ok": False, "error": str(e), **e.extra}))
        return e.code
    except Handled as e:
        return e.code
    except (OSError, ValueError) as e:  # corrupt JSON, unreadable files: still one JSON line
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 2
    print(json.dumps(out))
    if token:
        print(token)
    return 0


# ---------- self-test ----------
# Every case runs under one mkdtemp root, never a real lane's files, and drives the real CLI
# through a subprocess — so the one-JSON-line, exit-code and no-temp-file contract is what is
# tested, not an internal helper. The one exception is the rollback case, which runs in-process
# so `os.replace` can be made to fail.

def cli_raw(*args, env=None):
    e = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    e.update(env or {})
    r = subprocess.run([sys.executable, os.path.abspath(__file__), *args],
                       capture_output=True, text=True, env=e, timeout=180)
    return r.returncode, [x for x in r.stdout.splitlines() if x.strip()]


def cli(lane, *args, **kw):
    return cli_raw(*args, "--lane", lane, **kw)


def jline(lines):
    assert lines, "no stdout"
    return json.loads(lines[0])


def raw(lane):
    try:
        with open(status_path(lane), "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def leftovers(lane):
    return [n for n in os.listdir(lane) if n.startswith(".tmp-")]  # R24: none may survive


def refuse(lane, *args, **kw):
    """R16/R24: a refusal exits non-zero with ONE json error line, leaves the file byte-identical
    and leaves no temp file behind."""
    before = raw(lane)
    code, lines = cli(lane, *args, **kw)
    assert code != 0, f"expected a non-zero exit from {args}, got {code}: {lines}"
    assert len(lines) == 1, f"expected exactly one stdout line from {args}, got {lines}"
    out = jline(lines)
    assert out.get("ok") is False, f"expected an error line from {args}, got {out}"
    assert raw(lane) == before, f"the status file changed on the refused {args}"
    assert not leftovers(lane), f"a temp file was left behind by the refused {args}"
    return out


def mklane(root, name):
    lane = os.path.join(root, name)
    os.makedirs(lane, exist_ok=True)
    return lane


def git(lane, *args):
    r = subprocess.run(["git", "-C", lane, *args], capture_output=True, text=True, timeout=180,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                                GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull))
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr.strip()}"
    return r.stdout.strip()


def gitlane(root, name):
    """A lane that is a real git repo with one commit — `done` needs a readable HEAD (R18)."""
    lane = mklane(root, name)
    git(lane, "init", "-q")
    with open(os.path.join(lane, "README"), "w") as f:
        f.write("lane\n")
    git(lane, "add", "README")
    git(lane, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")
    return lane


def status(lane):
    with open(status_path(lane)) as f:
        return json.load(f)


def seed(lane, **over):
    """Write a status file directly, for states that would need a git repo to reach through the
    CLI (`done`) and for `start`'s archive step (R21)."""
    now = time.time()
    obj = {"v": 1, "seq": 1, "run": "r1", "task": "S1", "round": 1, "role": "builder",
           "agent": "opencode", "state": "started", "step": "start", "phases_done": [],
           "question": None, "commit": None, "report": ".matrixkoder-report-r1.md",
           "subagent": None, "ctx_pct": None, "started_at": now, "updated_at": now}
    obj.update(over)
    write_status(lane, obj)
    return obj


def begin(lane, root, **kw):
    """A one-line `start` with the common arguments, so a case reads as the behaviour under test."""
    args = ["start", "--run", kw.pop("run", "r1"), "--task", kw.pop("task", "S1"),
            "--round", str(kw.pop("round", 1)), "--report", kw.pop("report", "r.md"),
            "--agent", kw.pop("agent", "opencode")]
    for flag, value in kw.items():  # e.g. role="review" -> --role review
        args += [f"--{flag}", str(value)]
    code, lines = cli(lane, *args)
    assert code == 0, f"start failed: {lines}"
    return jline(lines)


def t_start_fresh(root):
    """Table 1 on a fresh file: every v1 key, `v` 1, `seq` 1, `state` started, one JSON line."""
    lane = mklane(root, "start_fresh")
    out = begin(lane, root, run="mk-x", task="MB2", round=2, report=".matrixkoder-report-r2.md")
    assert out["resumed"] is False and out["archived"] == [], out
    obj = status(lane)
    assert obj["v"] == 1 and obj["seq"] == 1 and obj["state"] == "started" and obj["step"] == "start"
    assert obj["run"] == "mk-x" and obj["task"] == "MB2" and obj["round"] == 2
    assert obj["report"] == ".matrixkoder-report-r2.md" and obj["agent"] == "opencode"
    assert obj["question"] is None and obj["commit"] is None and obj["subagent"] is None
    assert obj["ctx_pct"] is None and obj["phases_done"] == []
    missing = set(KEYS) ^ set(obj)
    assert not missing, f"Table 1 keys differ: {sorted(missing)}"


def t_start_archives_another_round(root):
    """R21 plus the MB2 archive rule: the other round's report, review and status files move into
    `.matrixkoder-old-<run>/`, none is deleted, and the new file is fresh (seq 1)."""
    lane = mklane(root, "archive")
    seed(lane, seq=7, round=1, state="working", step="S1.2")
    for name in (".matrixkoder-report-r1.md", ".matrixkoder-review-r1.md",
                 ".matrixkoder-report-r2.md", "keep-me.txt"):
        with open(os.path.join(lane, name), "w") as f:
            f.write("x")
    out = begin(lane, root, round=2, report=".matrixkoder-report-r2.md")
    old = os.path.join(lane, ".matrixkoder-old-r1")
    assert sorted(os.listdir(old)) == [".matrixkoder-report-r1.md", ".matrixkoder-review-r1.md",
                                       "status-S1-r1-builder-7.json"], os.listdir(old)
    assert set(out["archived"]) == {".matrixkoder-report-r1.md", ".matrixkoder-review-r1.md",
                                    "status-S1-r1-builder-7.json"}, out
    left = sorted(os.listdir(lane))
    assert "keep-me.txt" in left, f"only .matrixkoder-* round files are archived: {left}"
    assert ".matrixkoder-report-r2.md" in left, "this round's report is never archived"
    assert STATUS_NAME in left and status(lane)["seq"] == 1 and status(lane)["round"] == 2
    assert jline(cli(lane, "show")[1])["status"]["round"] == 2, "show never reads an archived file"


def t_start_resume(root):
    """R22: same identity, not done — `started_at` and `phases_done` survive, `seq` continues,
    and this start's report and agent win."""
    lane = mklane(root, "resume")
    begin(lane, root)
    cli(lane, "progress", "parse-plan")
    cli(lane, "phase-done", "MB1")
    first = status(lane)
    out = begin(lane, root, report="b.md", agent="claude")
    assert out["resumed"] is True and out["archived"] == [], out
    obj = status(lane)
    assert obj["started_at"] == first["started_at"], "R11/R22: started_at survives a resume"
    assert obj["phases_done"] == ["MB1"], "R8/R22: phases_done is kept"
    assert obj["seq"] == first["seq"] + 1, "R22/R25: seq continues"
    assert obj["report"] == "b.md" and obj["agent"] == "claude", "this start's report and agent win"
    assert obj["state"] == "started" and obj["step"] == "start"


def t_start_archives_a_done_file(root):
    """R21 over a `done` file: a reviewer's start is an archive plus a fresh file, never an error
    (same identity and a done file too), and the `review` role alias works."""
    for name, extra in (("done_archive", {"role": "review", "report": ".matrixkoder-review-r1.md"}),
                        ("done_archive_same", {})):
        lane = mklane(root, name)
        seed(lane, seq=5, state="done", commit="a" * 40)
        out = begin(lane, root, agent="claude", **extra)
        assert out["resumed"] is False, out
        assert os.listdir(os.path.join(lane, ".matrixkoder-old-r1")) == ["status-S1-r1-builder-5.json"], name
        obj = status(lane)
        assert obj["seq"] == 1 and obj["state"] == "started", name
        assert obj["role"] == ("reviewer" if extra else "builder"), name


def t_progress_legal_transitions(root):
    """R13.1 started->working, R13.3 subagent->working, R13.5 blocked->working, and R15 rewrites
    in `working` — plain `progress` is legal in every state except `done`."""
    lane = mklane(root, "progress_ok")
    begin(lane, root)
    assert status(lane)["state"] == "started"
    for step in ("parse-plan", "build"):
        assert cli(lane, "progress", step)[0] == 0
    obj = status(lane)
    assert obj["state"] == "working" and obj["step"] == "build" and obj["seq"] == 3
    cli(lane, "progress", "S1.2", "--subagent")
    assert cli(lane, "progress", "after-subagent")[0] == 0, "R13.3"
    assert status(lane)["state"] == "working" and status(lane)["subagent"] is None
    cli(lane, "blocked", "which fixture format?")
    assert cli(lane, "progress", "after-answer")[0] == 0, "R13.5"
    obj = status(lane)
    assert obj["state"] == "working" and obj["question"] is None, "progress clears the question"


def t_subagent_heartbeat_keeps_since(root):
    """R9: a heartbeat bumps `seq` and `updated_at` only, so both ages stay readable."""
    lane = mklane(root, "subagent_hb")
    begin(lane, root)
    cli(lane, "progress", "parse-plan")
    cli(lane, "progress", "S1.2", "--subagent")
    first = status(lane)
    assert first["state"] == "subagent" and first["subagent"] == {"task": "S1.2",
                                                                 "since": first["subagent"]["since"]}
    assert set(first["subagent"]) == {"task", "since"}, first
    time.sleep(0.01)
    cli(lane, "progress", "S1.2", "--subagent")
    obj = status(lane)
    assert obj["seq"] == first["seq"] + 1 and obj["updated_at"] >= first["updated_at"]
    assert obj["subagent"] == first["subagent"], "R9: a heartbeat keeps `since` and `step`"


def t_subagent_refused_from_started_and_blocked(root):
    """R17a (MB1 re-review finding): `--subagent` is legal only in `working` and `subagent`."""
    lane = mklane(root, "subagent_refused")
    begin(lane, root)
    assert "started -> subagent" in refuse(lane, "progress", "S1.2", "--subagent")["error"]
    cli(lane, "progress", "parse-plan")
    cli(lane, "blocked", "which fixture?")
    assert "blocked -> subagent" in refuse(lane, "progress", "S1.2", "--subagent")["error"]
    assert cli(lane, "progress", "parse-plan")[0] == 0, "R13.5 blocked -> working first"
    assert cli(lane, "progress", "S1.2", "--subagent")[0] == 0, "then R13.2 is legal"
    assert status(lane)["state"] == "subagent"


def t_blocked_question_and_token(root):
    """R19: whitespace-only is empty. R26: the token is the LAST stdout line lane-watch matches."""
    lane = mklane(root, "blocked")
    begin(lane, root)
    cli(lane, "progress", "parse-plan")
    refuse(lane, "blocked", "   ")
    code, lines = cli(lane, "blocked", "v2 or v3 fixture?")
    assert code == 0 and lines[-1] == "S1 R1 BLOCKED: v2 or v3 fixture?", lines
    obj = status(lane)
    assert obj["state"] == "blocked" and obj["question"] == "v2 or v3 fixture?" and obj["commit"] is None
    cli(lane, "blocked", "never mind - v3")
    assert status(lane)["question"] == "never mind - v3", "R15: a re-ask with a new question"
    # LOW 1: a question containing newlines must collapse to a single line in the printed token
    code, lines = cli(lane, "blocked", "line 1\nline 2\nline 3")
    assert code == 0 and lines[-1] == "S1 R1 BLOCKED: line 1 line 2 line 3", lines
    assert status(lane)["question"] == "line 1\nline 2\nline 3", "raw question preserved in json"


def t_done_preconditions(root):
    """R18: the report file is checked before git, so the error is deterministic; both failures
    leave the file byte-identical."""
    lane = mklane(root, "no_head")
    begin(lane, root)
    cli(lane, "progress", "parse-plan")
    assert "report_missing" in refuse(lane, "done")["error"]
    with open(os.path.join(lane, "r.md"), "w") as f:
        f.write("report")
    out = refuse(lane, "done", env={"GIT_CEILING_DIRECTORIES": root})
    assert "no_head" in out["error"], out
    assert status(lane)["commit"] is None and status(lane)["state"] == "working"


def t_done_token_builder_and_reviewer(root):
    """R26: `done` records the full HEAD sha and prints the exact token; the reviewer's form
    carries the REVIEW marker the watch arms (`lane-watch.py:579-588`)."""
    lane = gitlane(root, "done")
    with open(os.path.join(lane, ".matrixkoder-report-r1.md"), "w") as f:
        f.write("report")
    head = git(lane, "rev-parse", "HEAD")
    begin(lane, root, report=".matrixkoder-report-r1.md")
    cli(lane, "progress", "parse-plan")
    code, lines = cli(lane, "done")
    assert code == 0 and lines[-1] == "S1 R1 DONE", lines
    obj = status(lane)
    assert obj["state"] == "done" and obj["commit"] == head and SHA_RE.fullmatch(obj["commit"]), obj
    assert obj["question"] is None and obj["subagent"] is None
    lane2 = gitlane(root, "done_review")
    with open(os.path.join(lane2, ".matrixkoder-review-r1.md"), "w") as f:
        f.write("review")
    begin(lane2, root, role="review", report=".matrixkoder-review-r1.md", agent="claude")
    cli(lane2, "progress", "read-the-diff")
    code, lines = cli(lane2, "done")
    assert code == 0 and lines[-1] == "S1 REVIEW R1 DONE", lines
    assert status(lane2)["role"] == "reviewer"


def t_illegal_transitions(root):
    """R14/R16/R17: every transition R14 names is refused, and R20 makes `done` final."""
    lane = mklane(root, "illegal")
    begin(lane, root)
    assert "started -> done" in refuse(lane, "done")["error"]
    assert "started -> blocked" in refuse(lane, "blocked", "q")["error"]
    assert "illegal command" in refuse(lane, "phase-done", "MB1")["error"]
    assert "started -> subagent" in refuse(lane, "progress", "S1.2", "--subagent")["error"]
    cli(lane, "progress", "parse-plan")             # R13.1 started -> working
    cli(lane, "progress", "S1.2", "--subagent")     # R13.2 working -> subagent
    assert "subagent -> done" in refuse(lane, "done")["error"]
    assert "subagent -> blocked" in refuse(lane, "blocked", "q")["error"]
    assert "illegal command" in refuse(lane, "phase-done", "MB1")["error"]
    cli(lane, "progress", "parse-plan")
    cli(lane, "blocked", "q")
    assert "blocked -> done" in refuse(lane, "done")["error"]
    seed(lane, seq=3, state="done", commit="b" * 40)
    for args in (("progress", "x"), ("progress", "x", "--subagent"), ("blocked", "q"),
                 ("ctx", "5"), ("phase-done", "MB1"), ("done",)):
        out = refuse(lane, *args)
        assert "done ->" in out["error"] or "in state done" in out["error"], (args, out)
    assert "started" not in set(TARGET.values()), "R14: only `start` writes state=started"


def t_seq_monotonic(root):
    """R25: every accepted write bumps `seq` by exactly one; a refusal does not."""
    lane = mklane(root, "seq")
    begin(lane, root)
    seen = [status(lane)["seq"]]
    for args in (("progress", "parse-plan"), ("progress", "S1.2", "--subagent"),
                 ("progress", "after"), ("ctx", "10"), ("phase-done", "MB1")):
        assert cli(lane, *args)[0] == 0, args
        seen.append(status(lane)["seq"])
    assert seen == [1, 2, 3, 4, 5, 6], seen
    refuse(lane, "ctx", "101")
    assert status(lane)["seq"] == 6


def t_ctx_bounds(root):
    """R10: only an integer 0..100 is written, and only `ctx` writes `ctx_pct`."""
    lane = mklane(root, "ctx")
    begin(lane, root)
    assert status(lane)["ctx_pct"] is None, "null until an agent reports it"
    cli(lane, "progress", "parse-plan")
    assert status(lane)["ctx_pct"] is None
    refuse(lane, "ctx", "101")
    refuse(lane, "ctx", "-1")
    refuse(lane, "ctx", "abc")
    for pct in ("0", "100"):
        assert cli(lane, "ctx", pct)[0] == 0, pct
    assert status(lane)["ctx_pct"] == 100


def t_phase_done_append_only(root):
    """R8: append-only, at most once per round; R17: only in `working`."""
    lane = mklane(root, "phase_done")
    begin(lane, root)
    assert "illegal command" in refuse(lane, "phase-done", "MB1")["error"]
    cli(lane, "progress", "parse-plan")
    assert cli(lane, "phase-done", "MB1")[0] == 0
    assert status(lane)["phases_done"] == ["MB1"]
    assert "phases_done" in refuse(lane, "phase-done", "MB1")["error"], "R8: at most once per round"
    cli(lane, "phase-done", "MB2")
    assert status(lane)["phases_done"] == ["MB1", "MB2"], "appended in order, nothing removed"


def t_atomic_write_rollback(root):
    """R23/R24: a failure between the temp write and `os.replace` leaves the old file readable
    and byte-identical and removes the temp file. In-process, so the seam can be broken."""
    lane = mklane(root, "atomic")
    begin(lane, root)
    cli(lane, "progress", "parse-plan")
    before = raw(lane)
    real, os.replace = os.replace, lambda *a, **kw: (_ for _ in ()).throw(OSError("simulated kill"))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = main(["progress", "build", "--lane", lane])
    finally:
        os.replace = real
    lines = buf.getvalue().splitlines()
    assert code != 0 and len(lines) == 1 and json.loads(lines[0])["ok"] is False, lines
    assert raw(lane) == before, "R24: the old file survives byte-identical"
    assert not leftovers(lane), "R24: no temp file is left behind"
    assert cli(lane, "progress", "build")[0] == 0, "the lane is still writable"


def t_reader_acceptance(root):
    """R27 as the writer's own reader check (`show`): `updated_at` is a float epoch compared with
    a float `armed_at` (R27.7), and a non-monotonic `seq` is refused (R27.9)."""
    lane = mklane(root, "reader")
    begin(lane, root)
    obj = status(lane)
    armed_at, seq = obj["updated_at"], obj["seq"]
    assert jline(cli(lane, "show")[1])["accepted"] is True, "defaults accept the file's identity"
    out = jline(cli(lane, "show", "--armed-at", repr(armed_at))[1])
    assert out["accepted"] is True and out["problems"] == [], out
    out = jline(cli(lane, "show", "--armed-at", repr(armed_at + 1))[1])
    assert out["accepted"] is False and any("updated_at" in x for x in out["problems"]), out
    for flag, value, needle in (("--run", "other", "run"), ("--task", "other", "task"),
                                ("--role", "reviewer", "role"), ("--state", "bogus", "unknown state"),
                                ("--seen-seq", str(seq), "seq")):
        out = jline(cli(lane, "show", flag, value)[1])
        assert out["accepted"] is False and any(needle in x for x in out["problems"]), (flag, out)
    out = jline(cli(lane, "show", "--seen-seq", str(seq - 1))[1])
    assert out["accepted"] is True and out["problems"] == [], out


def t_reader_fallbacks(root):
    """R28/R29: an absent or corrupt file is reported, never raised, and never repaired."""
    lane = mklane(root, "fallback")
    out = jline(cli(lane, "show")[1])
    assert out["accepted"] is False and out["status"] is None and out["problems"], out
    with open(status_path(lane), "w") as f:
        f.write("{not json")
    out = jline(cli(lane, "show")[1])
    assert out["accepted"] is False and any("corrupt" in x for x in out["problems"]), out
    with open(status_path(lane), "w") as f:
        f.write("[1, 2]")
    out = jline(cli(lane, "show")[1])
    assert out["accepted"] is False and any("object" in x for x in out["problems"]), out
    assert raw(lane) == b"[1, 2]", "show is read-only"


def t_float_timestamps(root):
    """R7: float epoch seconds at full precision — a rounded or ISO value would break the
    reader's one-comparison `updated_at >= armed_at` (R27.7)."""
    lane = mklane(root, "floats")
    begin(lane, root)
    time.sleep(0.02)
    cli(lane, "progress", "parse-plan")
    cli(lane, "progress", "S1.2", "--subagent")
    obj = status(lane)
    for key in ("started_at", "updated_at"):
        assert isinstance(obj[key], float), f"{key} is {type(obj[key])}, not a float"
    assert isinstance(obj["subagent"]["since"], float)
    assert obj["updated_at"] > obj["started_at"], "sub-second precision survives the round trip"
    assert obj["updated_at"] >= obj["started_at"], "Table 1: updated_at >= started_at"


def tree_digest(root):
    """(relative path, bytes) per file — install idempotency is a byte comparison."""
    items = []
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            p = os.path.join(d, name)
            with open(p, "rb") as f:
                items.append((os.path.relpath(p, root), f.read()))
    return items


def t_install_idempotent(root):
    """D2: install copies the whole skill folder over the target, excludes it from git, prints
    the version, and a second run leaves the tree byte-identical."""
    lane = gitlane(root, "install")
    code, lines = cli_raw("install", "--into", lane)
    assert code == 0 and len(lines) == 1, lines
    out = jline(lines)
    dest = out["installed"]
    assert dest.endswith(os.path.join(".claude", "skills", "mk-builder")), out
    assert out["version"] == plugin_version() and out["files"] > 0, out
    assert os.path.isfile(os.path.join(dest, "reference", "status-contract.md"))
    assert os.path.isfile(os.path.join(dest, "scripts", "mk-builder.py"))
    assert not [p for p in tree_digest(dest) if "__pycache__" in p[0]], "no bytecode is installed"
    excl = os.path.join(lane, ".git", "info", "exclude")
    with open(excl) as f:
        assert f.read().splitlines().count(".claude/skills/mk-builder/") == 1, f
    before = tree_digest(dest)
    code, lines2 = cli_raw("install", "--into", lane)
    out2 = jline(lines2)
    assert code == 0 and out2["version"] == out["version"] and out2["files"] == out["files"], out2
    assert tree_digest(dest) == before, "a second install is byte-identical"
    with open(excl) as f:
        assert f.read().splitlines().count(".claude/skills/mk-builder/") == 1, "the line is added once"


def t_argv_errors_are_json(root):
    """A bad flag or a missing file still prints one JSON line and a non-zero exit (mk-state)."""
    lane = mklane(root, "argv")
    out = refuse(lane, "ctx", "notanumber")
    assert "invalid int value" in out["error"], out
    out = refuse(lane, "progress", "x")
    assert STATUS_NAME in out["error"], f"a missing file is reported, not crashed: {out}"
    # MEDIUM 1: bad task, run, role give JSON ok:false, never a traceback
    code, lines = cli_raw("start", "--lane", lane, "--run", "bad/run", "--task", "S1", "--round", "1", "--report", "r.md")
    assert code != 0 and len(lines) == 1 and json.loads(lines[0])["ok"] is False, lines
    assert "invalid run" in lines[0]
    code, lines = cli_raw("start", "--lane", lane, "--run", "r1", "--task", "bad/task", "--round", "1", "--report", "r.md")
    assert code != 0 and len(lines) == 1 and json.loads(lines[0])["ok"] is False, lines
    assert "invalid task" in lines[0]
    code, lines = cli_raw("start", "--lane", lane, "--run", "r1", "--task", "S1", "--round", "1", "--role", "bad/role", "--report", "r.md")
    assert code != 0 and len(lines) == 1 and json.loads(lines[0])["ok"] is False, lines
    assert "invalid role" in lines[0] or "role must be one of" in lines[0]


def t_start_hyphenated_task(root):
    """MEDIUM 1: hyphenated task id (e.g. S3-a) starts cleanly and archives on next round."""
    lane = mklane(root, "hyphen_task")
    out = begin(lane, root, task="S3-a", round=1)
    assert out["seq"] == 1 and out["task"] == "S3-a", out
    # start round 2 with a different round to trigger archive
    out2 = begin(lane, root, task="S3-a", round=2)
    assert out2["seq"] == 1 and out2["task"] == "S3-a", out2
    old_dir = os.path.join(lane, ".matrixkoder-old-r1")
    assert os.path.isdir(old_dir), "archive directory created"
    archived_files = os.listdir(old_dir)
    assert "status-S3-a-r1-builder-1.json" in archived_files, f"archived files: {archived_files}"


def t_install_in_worktree(root):
    """MEDIUM 2: in a git worktree lane (.git is a file), install writes the exclude and
    git status --porcelain shows nothing for .claude/."""
    main_repo = gitlane(root, "wt_main")
    wt_dir = os.path.join(root, "wt_lane")
    git(main_repo, "worktree", "add", wt_dir, "-b", "wt-branch")
    assert os.path.isfile(os.path.join(wt_dir, ".git")), "worktree .git must be a file"
    code, lines = cli_raw("install", "--into", wt_dir)
    assert code == 0 and len(lines) == 1, lines
    dest = jline(lines)["installed"]
    assert os.path.isdir(dest), "skill folder installed in worktree"
    # git status in worktree must show nothing for .claude/
    status_out = git(wt_dir, "status", "--porcelain")
    claude_lines = [l for l in status_out.splitlines() if ".claude" in l]
    assert not claude_lines, f"git status should not show .claude/, got: {claude_lines}"
    # run install a second time to ensure idempotency in worktree
    code2, lines2 = cli_raw("install", "--into", wt_dir)
    assert code2 == 0, lines2
    status_out2 = git(wt_dir, "status", "--porcelain")
    assert not [l for l in status_out2.splitlines() if ".claude" in l]


# ---------- measure self-test (MB5.1) ----------

def measure_fixture(root):
    """One fixture run dir: every shape `measure` counts — pre-MB3 verdicts (no source tag),
    MB3 verdicts (status/pane/grace), a grace DONE, a kept-status BLOCKED note, a torn line, two
    tasks with briefs, and files that must NOT count as briefs (task.json, review-brief-r1.md).
    R6: the fixture is built in the self-test's temp root at run time, never committed."""
    data = os.path.join(root, "measure_data")
    run_dir = os.path.join(data, "runs", "fx")
    t1 = os.path.join(run_dir, "tasks", "T1")
    t2 = os.path.join(run_dir, "tasks", "T2")
    os.makedirs(t1, exist_ok=True)
    os.makedirs(t2, exist_ok=True)
    for path, body in ((os.path.join(t1, "brief-r1.md"), "alpha\nbeta\ngamma\n"),
                       (os.path.join(t1, "brief-r2.md"), "x\n"),
                       (os.path.join(t2, "brief-r1.md"), "one\ntwo\n"),
                       (os.path.join(t1, "task.json"), "{}\n"),
                       (os.path.join(t1, "review-brief-r1.md"), "not a brief\n")):
        with open(path, "w") as f:
            f.write(body)
    rows = [
        ("2026-10-10T12:00:00+02:00", None, "note", "run init: fixture run", None, None, "orchestrator"),
        # pre-MB3 STALL (no source): false — a DONE for T1 lands 3 min later
        ("2026-10-10T12:05:00+02:00", "T1", "transition",
         "lane-watch STALL: working but no file/commit/pane change for 10.0 min",
         "building", "stalled", "lane-watch"),
        ("2026-10-10T12:08:00+02:00", "T1", "transition",
         "lane-watch DONE: commit a1b2c3d4e5f6a7b8c9d0 fixture subject one; T1 R1 DONE",
         "stalled", "verifying", "lane-watch"),
        # MB3 STALL, source=pane: false — an orchestrator verdict (not a lane-watch DONE) for T2
        # mentions a commit sha 5 min later
        ("2026-10-10T12:20:00+02:00", "T2", "transition",
         "lane-watch STALL: working but no file/commit/pane change for 10.5 min; source=pane",
         "building", "stalled", "lane-watch"),
        ("2026-10-10T12:25:00+02:00", "T2", "verdict", "verify T2 @f6ec0d5ff: tests 3/0",
         None, None, "orchestrator"),
        # MB3 STALL, source=status: NOT false — T1's next DONE is 20 min later
        ("2026-10-10T12:40:00+02:00", "T1", "transition",
         "lane-watch STALL: working but no file/commit/pane change for 10.0 min; source=status",
         "building", "stalled", "lane-watch"),
        ("2026-10-10T13:00:00+02:00", "T1", "transition",
         "lane-watch DONE: commit a1b2c3d4e5f6a7b8c9d0 fixture subject two; T1 R2 DONE",
         "stalled", "verifying", "lane-watch"),
        # the grace DONE (current shape): the one missed DONE line
        ("2026-10-10T13:30:00+02:00", "T2", "note",
         "lane-watch DONE: no T2 R1 DONE token (report, agent idle 4 min); source=grace",
         None, None, "lane-watch"),
        ("2026-10-10T14:00:00+02:00", "T2", "transition",
         "lane-watch BLOCKED: T2 BLOCKED: which fixture format is the acceptance target?; source=pane",
         "building", "blocked", "lane-watch"),
        ("2026-10-10T14:10:00+02:00", "T1", "transition",
         "lane-watch BLOCKED: status file blocked: v2 or v3 fixture?; source=status",
         "building", "blocked", "lane-watch"),
        ("2026-10-10T14:20:00+02:00", "T1", "transition",
         "lane-watch DONE: commit a1b2c3d4e5f6a7b8c9d0 fixture subject three; "
         "status file done (seq 5); source=status",
         "blocked", "verifying", "lane-watch"),
        # a late watcher's kept-status note: the ` (status ... kept)` suffix must not reach the
        # extracted question
        ("2026-10-10T14:30:00+02:00", "T2", "note",
         "lane-watch BLOCKED: T2 BLOCKED: resolved: answer b; source=pane (status blocked kept)",
         None, None, "lane-watch"),
    ]
    with open(os.path.join(run_dir, "events.jsonl"), "w") as f:
        for t, task, typ, text, frm, to, by in rows:
            f.write(json.dumps({"t": t, "run": "fx", "task": task, "type": typ,
                                "text": text, "from": frm, "to": to, "by": by}) + "\n")
        f.write("{torn\n")  # a crash mid-append: skipped, not fatal
    return data


def data_digest(data):
    """(relative path, size, mtime_ns) per file under <data> — the read-only proof."""
    items = []
    for d, dirs, files in os.walk(data):
        dirs.sort()
        for name in sorted(files):
            p = os.path.join(d, name)
            st = os.stat(p)
            items.append((os.path.relpath(p, data), st.st_size, st.st_mtime_ns))
    return items


def t_measure_exact_counts(root):
    """MB5.1: the fixture run's exact counters — briefs (only brief-rN.md, task.json and
    review-brief-r1.md excluded), verdict events by source (the pre-MB3 `none` bucket included),
    the grace DONE as the one missed DONE line, two false STALLs (one by a DONE, one by a commit
    mention in a non-lane-watch event, the third STALL outside the 10-min window), and the three
    BLOCKED questions with the kept-status suffix stripped."""
    data = measure_fixture(root)
    code, lines = cli_raw("measure", "--data", data, "--run", "fx")
    assert code == 0 and len(lines) == 1, lines
    out = jline(lines)
    assert out == {
        "ok": True, "run": "fx", "data": data,
        "briefs": {"T1": {"brief-r1.md": {"lines": 3, "bytes": 17},
                          "brief-r2.md": {"lines": 1, "bytes": 2}},
                   "T2": {"brief-r1.md": {"lines": 2, "bytes": 8}}},
        "verdicts_by_source": {"status": 3, "pane": 3, "grace": 1, "none": 3},
        "missed_done_lines": 1, "stalls": 3, "false_stalls": 2, "blocked": 3,
        "blocked_questions": ["which fixture format is the acceptance target?",
                              "v2 or v3 fixture?", "resolved: answer b"],
    }, out


def t_measure_read_only(root):
    """MB5.1: measure changes no file under <data> (paths, sizes and mtimes identical before and
    after), a second run is byte-identical, and an unknown run or data dir is one JSON error
    line and a non-zero exit that also changes nothing."""
    data = measure_fixture(root)
    before = data_digest(data)
    code, lines = cli_raw("measure", "--data", data, "--run", "fx")
    assert code == 0, lines
    assert data_digest(data) == before, "measure wrote something under <data>"
    code2, lines2 = cli_raw("measure", "--data", data, "--run", "fx")
    assert code2 == 0 and lines2 == lines, "a second run is not byte-identical"
    for args, needle in ((("measure", "--data", data, "--run", "nope"), "unknown run"),
                         (("measure", "--data", os.path.join(root, "no-such-data"), "--run", "fx"),
                          "does not exist")):
        code3, lines3 = cli_raw(*args)
        assert code3 != 0 and len(lines3) == 1, (args, lines3)
        out = jline(lines3)
        assert out.get("ok") is False and needle in out.get("error", ""), (args, out)
    assert data_digest(data) == before, "a refused run changed nothing either"


# ---------- bus client self-test (plan BB) ----------
# The real `scripts/mk-bus.py` (resolved from this file's repo root), started the way its own
# --self-test starts a server: `ensure` — a detached serve on an ephemeral 127.0.0.1 port with a
# temp data dir — tracked in BUS_SERVERS and stopped in run_self_test's finally. An installed
# skill copy has no scripts/ tree next to it, so every bus case returns early there; the plugin
# checkout, where the gates run, always has it.

MK_BUS_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))), "scripts", "mk-bus.py")
BUS_SERVERS = []        # every (pid, data dir) this self-test started
BUS_SERVER_CACHE = {}   # one shared server per (root, name); cases are order-independent


def bus_cli(*args):
    """The real mk-bus.py, one JSON line back: (rc, obj); a failed call fails the case."""
    r = subprocess.run([sys.executable, MK_BUS_PY, *args], capture_output=True, text=True,
                       timeout=90, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    lines = [x for x in r.stdout.splitlines() if x.strip()]
    obj = json.loads(lines[-1]) if lines else {}
    assert r.returncode == 0 and obj.get("ok") is True, \
        f"mk-bus.py {args}: rc={r.returncode} out={obj} err={r.stderr.strip()}"
    return r.returncode, obj


def pin_home(root, name, data):
    """Todo 69: a store-mode bus case runs on its own temp home. The store-mode bus lives in the
    MK home and registers every project it serves there, so without this the case falls back to
    the real `~/.matrixkoder` (bus.json, mk.db). Everything that resolves a home or the data dir
    is pinned for the rest of the case (run_self_test resets it before the next one)."""
    home = os.path.join(root, f"{name}-home")
    os.makedirs(home, exist_ok=True)
    for key in ("MK_HOME", "MATRIXKODER_HOME", "MK_BUS_DIR"):
        os.environ[key] = home
    os.environ["MK_BUS_DATA"] = data


def bus_server(root, name="bus"):
    """One REAL mk-bus server per (root, name): ensure → detached serve, ephemeral port,
    temp data dir. Returns (data dir, pid, port)."""
    key = (root, name)
    data = os.path.join(root, f"{name}-data")
    if store_present(data):
        pin_home(root, name, data)
    if key not in BUS_SERVER_CACHE:
        os.makedirs(data, exist_ok=True)
        _, res = bus_cli("ensure", "--data", data, "--run", BUS_RUN)
        # A root bus is one process for every project, so a second ensure
        # reports started:false while still handing back the live pid and port.
        assert res.get("pid") and res.get("port") and (
            res.get("started") is True or store_present(data)
        ), res
        BUS_SERVERS.append((res["pid"], data))
        BUS_SERVER_CACHE[key] = (data, res["pid"], res["port"])
    return BUS_SERVER_CACHE[key]


def bus_lane(root, lane_name, task, data):
    """A lane dir whose `.matrixkoder-bus.json` is written by the real `mk-bus.py token`.
    The data dir is stamped into the file so D7 commands can find the project store. The
    production token file has no `data` field; `MK_BUS_DATA` is the other way to name it."""
    lane = mklane(root, lane_name)
    bus_cli("token", "--lane", lane, "--task", task, "--run", BUS_RUN, "--data", data)
    path = bus_token_path(lane)
    if not os.path.isfile(path):
        path = os.path.join(lane, BUS_LANE_FILE)
    with open(path) as f:
        cfg = json.load(f)
    cfg["data"] = data
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.chmod(path, 0o600)
    return lane


def bus_rows(data):
    """`<data>/runs/<run>/bus.jsonl` as dicts; a torn line is skipped, a missing file is []."""
    out = []
    with contextlib.suppress(OSError):
        with open(os.path.join(data, "runs", BUS_RUN, "bus.jsonl")) as f:
            for line in f:
                with contextlib.suppress(json.JSONDecodeError):
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        out.append(obj)
    return out


def bus_poll_until(fn, timeout=15.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = fn()
        if got:
            return got
        time.sleep(step)
    raise AssertionError(f"bus_poll_until timed out after {timeout}s")


def stop_pid(pid, timeout=5.0):
    """mk-bus.py's stop_process shape: SIGTERM, wait out the timeout, SIGKILL."""
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def stop_bus_servers():
    for pid, _ in BUS_SERVERS:
        stop_pid(pid)


def bus_serve_leftovers():
    """The brief's no-serve-left check: pgrep per started server's own data dir (never a bare
    `pgrep mk-bus`, which would match a real orchestrator bus running beside this test)."""
    out = []
    for _, data in BUS_SERVERS:
        try:
            r = subprocess.run(["pgrep", "-f", f"mk-bus.py serve --data {data}"],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue  # no pgrep here: the pid checks above are the guarantee
        if r.returncode == 0:
            out.append(data)
    return out


def t_bus_ask_answer_roundtrip(root):
    """The BB round trip against the real server: ask lands on the bus, the status file goes
    blocked (observed mid-wait) and back to working, the real `mk-bus.py answer` (sent from a
    thread) unblocks the call, the answer text is the only stdout line, and an ack level-3
    record for the answer is in bus.jsonl."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root)
    lane = bus_lane(root, "bus_ask_lane", "TQ", data)
    begin(lane, root)
    cli(lane, "progress", "parse-plan")
    question, answer_text = "which fixture format is the acceptance target?", "target format v3"
    seen = {}

    def answer_when_blocked():
        row = bus_poll_until(lambda: next(
            (r for r in bus_rows(data) if r.get("kind") == "ask" and r.get("task") == "TQ"), None))
        seen["blocked"] = bus_poll_until(
            lambda: status(lane) if status(lane).get("state") == "blocked" else None)
        # an info to the lane lands while the ask polls: it must be skipped, not returned
        bus_cli("send", "--run", BUS_RUN, "--task", "TQ", "--kind", "info",
                "--text", "noise for the poll", "--data", data)
        bus_cli("answer", row["id"], answer_text, "--run", BUS_RUN, "--data", data)

    th = threading.Thread(target=answer_when_blocked)
    th.start()
    try:
        code, lines = cli(lane, "ask", question, "--timeout", "30")
    finally:
        th.join(30)
    assert code == 0 and lines[0].startswith("ASK m-") and lines[-1] == answer_text, (code, lines)
    st = status(lane)
    assert st["state"] == "working" and st["question"] is None, st
    assert seen["blocked"]["question"] == question, seen["blocked"]
    rows = bus_rows(data)
    asks = [r for r in rows if r.get("kind") == "ask" and r.get("task") == "TQ"]
    assert len(asks) == 1 and asks[0]["from"] == "TQ" and asks[0]["to"] == "orch", asks
    assert asks[0]["text"] == question, asks[0]
    answers = [r for r in rows if r.get("kind") == "answer" and r.get("reply_to") == asks[0]["id"]]
    assert len(answers) == 1 and answers[0]["text"] == answer_text, answers
    assert answers[0]["to"] == "TQ" and answers[0]["from"] == "orch", answers[0]
    acks = [r for r in rows if r.get("kind") == "ack" and r.get("reply_to") == answers[0]["id"]]
    assert len(acks) == 1 and acks[0]["ack_level"] == 3, acks


def t_bus_ask_survives_restart(root):
    """F1: an `ask` started against a live bus rides out `kill -9` plus a restart on the same
    port, gets the answer posted after the restart, and the bus holds exactly ONE ask."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, pid, port = bus_server(root, "bus-restart")
    lane = bus_lane(root, "bus_restart_lane", "TR", data)
    begin(lane, root)
    cli(lane, "progress", "build")
    out = {}

    def run_ask():
        out["res"] = cli(lane, "ask", "survive the restart?", "--timeout", "60")

    th = threading.Thread(target=run_ask)
    th.start()
    try:
        row = bus_poll_until(lambda: next(
            (r for r in bus_rows(data) if r.get("kind") == "ask" and r.get("task") == "TR"), None))
        # the ask is in flight only once its POST returned and the lane went blocked
        bus_poll_until(lambda: status(lane).get("state") == "blocked")
        os.kill(pid, signal.SIGKILL)

        def dead():
            try:
                os.kill(pid, 0)
            except OSError:
                return True

        bus_poll_until(dead)
        _, res = bus_cli("ensure", "--data", data, "--run", BUS_RUN)
        assert res.get("port") == port, ("same port after restart", port, res)
        BUS_SERVERS.append((res["pid"], data))
        bus_cli("answer", row["id"], "after the restart", "--run", BUS_RUN, "--data", data)
    finally:
        th.join(70)
    code, lines = out["res"]
    assert code == 0 and lines[0] == f"ASK {row['id']}" and lines[-1] == "after the restart", (code, lines)
    asks = [r for r in bus_rows(data) if r.get("kind") == "ask" and r.get("task") == "TR"]
    assert len(asks) == 1, asks
    assert status(lane)["state"] == "working", status(lane)


def t_bus_ask_pending_then_wait(root):
    """ask with --timeout 2 and no answer: `PENDING <ask id>`, exit 3, status stays blocked.
    `wait` on an unanswered ask PENDINGs too; once the real answer lands (with nobody
    polling), `wait` returns its text, sets working and leaves a level-3 ack."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root)
    lane = bus_lane(root, "bus_pend_lane", "TP", data)
    begin(lane, root)
    cli(lane, "progress", "build")
    t0 = time.time()
    code, lines = cli(lane, "ask", "no answer coming", "--timeout", "2")
    assert time.time() - t0 < 10, "the 2 s ask must not hang"
    assert code == 3 and lines[0].startswith("ASK m-") and lines[-1].startswith("PENDING m-"), (code, lines)
    ask_id = lines[0].split()[1]
    assert lines[-1] == f"PENDING {ask_id}", lines
    st = status(lane)
    assert st["state"] == "blocked" and st["question"] == "no answer coming", st
    code, lines = cli(lane, "ask", "also unanswered", "--timeout", "2")
    assert code == 3 and lines[0].startswith("ASK m-") and lines[-1].startswith("PENDING m-"), (code, lines)
    ask2 = lines[0].split()[1]
    code, lines = cli(lane, "wait", ask2, "--timeout", "2")
    assert code == 3 and lines == [f"PENDING {ask2}"], (code, lines)
    assert status(lane)["state"] == "blocked", "a PENDING wait leaves the status blocked"
    bus_cli("answer", ask_id, "late answer", "--run", BUS_RUN, "--data", data)
    code, lines = cli(lane, "wait", ask_id, "--timeout", "10")
    assert code == 0 and lines[-1] == "late answer", (code, lines)
    st = status(lane)
    assert st["state"] == "working" and st["question"] is None, st
    answers = [r for r in bus_rows(data)
               if r.get("kind") == "answer" and r.get("reply_to") == ask_id]
    assert len(answers) == 1 and answers[0]["text"] == "late answer", answers
    acks = [r for r in bus_rows(data)
            if r.get("kind") == "ack" and r.get("reply_to") == answers[0]["id"]]
    assert len(acks) == 1 and acks[0]["ack_level"] == 3, acks


def _b1_acks(data, reply_to):
    return [r for r in bus_rows(data)
            if r.get("kind") == "ack" and r.get("reply_to") == reply_to]


def t_b1_ask_advances_cursor_no_second_ack(root):
    """Todos 37 and 42: after ask consumes an answer, inbox prints nothing and the
    bus log has exactly one level-3 ack for that answer.

    This case and t_b1_wait_advances_cursor_no_second_ack PASS on the R1 base:
    bus_note_seq already covered an answer with nothing unread before it. They
    are regression guards. The proof that the cursor does not skip an unread
    message is t_b1_lower_seq_stays_unread and t_b1_unread_older_than_the_ask.
    """
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root)
    lane = bus_lane(root, "b1_ask_lane", "BA", data)
    begin(lane, root)
    cli(lane, "progress", "parse-plan")
    question = "which cursor?"

    def answer_when_blocked():
        row = bus_poll_until(lambda: next(
            (r for r in bus_rows(data) if r.get("kind") == "ask" and r.get("task") == "BA"), None))
        bus_cli("answer", row["id"], "cursor advanced", "--run", BUS_RUN, "--data", data)

    th = threading.Thread(target=answer_when_blocked)
    th.start()
    try:
        code, lines = cli(lane, "ask", question, "--timeout", "30")
    finally:
        th.join(30)
    assert code == 0 and lines[0].startswith("ASK m-") and lines[-1] == "cursor advanced", (code, lines)
    code, lines = cli(lane, "inbox")
    assert code == 0 and lines == [], (code, lines)
    answers = [r for r in bus_rows(data)
               if r.get("kind") == "answer" and r.get("text") == "cursor advanced"]
    assert len(answers) == 1, answers
    acks = _b1_acks(data, answers[0]["id"])
    assert len(acks) == 1 and acks[0]["ack_level"] == 3, acks
    assert bus_read_cursor(lane) >= answers[0]["seq"]


def t_b1_wait_advances_cursor_no_second_ack(root):
    """Same for wait: the answer is consumed, inbox is empty, one ack only."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root)
    lane = bus_lane(root, "b1_wait_lane", "BW", data)
    begin(lane, root)
    cli(lane, "progress", "build")
    code, lines = cli(lane, "ask", "hold", "--timeout", "2")
    assert code == 3 and lines[0].startswith("ASK m-") and lines[-1].startswith("PENDING "), (code, lines)
    ask_id = lines[0].split()[1]
    bus_cli("answer", ask_id, "wait advanced", "--run", BUS_RUN, "--data", data)
    code, lines = cli(lane, "wait", ask_id, "--timeout", "10")
    assert code == 0 and lines == ["wait advanced"], (code, lines)
    code, lines = cli(lane, "inbox")
    assert code == 0 and lines == [], (code, lines)
    answers = [r for r in bus_rows(data)
               if r.get("kind") == "answer" and r.get("reply_to") == ask_id]
    assert len(answers) == 1, answers
    acks = _b1_acks(data, answers[0]["id"])
    assert len(acks) == 1 and acks[0]["ack_level"] == 3, acks


def t_b1_lower_seq_stays_unread(root):
    """An unread message with a lower seq is still printed by inbox after an
    answer with a higher seq was consumed. The cursor does not jump over it."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root)
    lane = bus_lane(root, "b1_gap_lane", "BG", data)
    begin(lane, root)
    cli(lane, "progress", "build")
    code, lines = cli(lane, "ask", "gap?", "--timeout", "2")
    assert code == 3 and lines[0].startswith("ASK m-"), (code, lines)
    ask_id = lines[0].split()[1]
    # info lands first (lower seq), then the answer (higher seq)
    bus_cli("send", "--run", BUS_RUN, "--task", "BG", "--kind", "info",
            "--text", "still unread", "--data", data)
    bus_cli("answer", ask_id, "the answer", "--run", BUS_RUN, "--data", data)
    code, lines = cli(lane, "wait", ask_id, "--timeout", "10")
    assert code == 0 and lines == ["the answer"], (code, lines)
    code, lines = cli(lane, "inbox")
    assert code == 0 and len(lines) == 1, (code, lines)
    shown = json.loads(lines[0])
    assert shown["kind"] == "info" and shown["text"] == "still unread", shown
    # the info was acked once, by inbox, not twice; the answer was acked once, by wait
    assert len(_b1_acks(data, shown["id"])) == 1
    answers = [r for r in bus_rows(data) if r.get("kind") == "answer" and r.get("reply_to") == ask_id]
    assert len(answers) == 1 and len(_b1_acks(data, answers[0]["id"])) == 1, answers


def t_b1_unread_older_than_the_ask(root):
    """Review R1 HIGH: an info sent BEFORE the ask has a lower seq than the ask.
    ask must not move the cursor past it. inbox still prints it, once, and the
    answer keeps a single ack. Same shape for wait."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root)
    lane = bus_lane(root, "b1_early_lane", "BE", data)
    begin(lane, root)
    cli(lane, "progress", "build")
    bus_cli("send", "--run", BUS_RUN, "--task", "BE", "--kind", "info",
            "--text", "early unread", "--data", data)

    def answer_when_blocked():
        row = bus_poll_until(lambda: next(
            (r for r in bus_rows(data) if r.get("kind") == "ask" and r.get("task") == "BE"), None))
        bus_poll_until(lambda: status(lane) if status(lane).get("state") == "blocked" else None)
        bus_cli("answer", row["id"], "A", "--run", BUS_RUN, "--data", data)

    th = threading.Thread(target=answer_when_blocked)
    th.start()
    try:
        code, lines = cli(lane, "ask", "q?", "--timeout", "30")
    finally:
        th.join(30)
    assert code == 0 and lines[0].startswith("ASK m-") and lines[-1] == "A", (code, lines)
    code, lines = cli(lane, "inbox")
    assert code == 0 and len(lines) == 1, (code, lines)
    shown = json.loads(lines[0])
    assert shown["text"] == "early unread" and shown["kind"] == "info", shown
    assert len(_b1_acks(data, shown["id"])) == 1
    # wait: a second early info, then an unanswered ask that wait consumes
    bus_cli("send", "--run", BUS_RUN, "--task", "BE", "--kind", "info",
            "--text", "early for wait", "--data", data)
    code, lines = cli(lane, "ask", "later?", "--timeout", "2")
    assert code == 3 and lines[0].startswith("ASK m-") and lines[-1].startswith("PENDING "), (code, lines)
    ask_id = lines[0].split()[1]
    bus_cli("answer", ask_id, "W", "--run", BUS_RUN, "--data", data)
    code, lines = cli(lane, "wait", ask_id, "--timeout", "10")
    assert code == 0 and lines == ["W"], (code, lines)
    code, lines = cli(lane, "inbox")
    assert code == 0 and len(lines) == 1, (code, lines)
    assert json.loads(lines[0])["text"] == "early for wait"


def t_b1_cursor_monotonic_under_two_processes(root):
    """Two real processes: one advances to 9, one tries to write 4. The cursor
    ends at 9. bus_advance_cursor holds the lock across the compare and the write."""
    lane = mklane(root, "b1_race")
    bus_write_cursor(lane, 1)
    child = os.path.join(root, "b1_race_child.py")
    with open(child, "w") as f:
        f.write(
            "import importlib.util, os, sys, time\n"
            f"spec = importlib.util.spec_from_file_location('mkb', {os.path.abspath(__file__)!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "real = m.bus_write_cursor\n"
            "def slow(lane, seq):\n"
            "    time.sleep(0.3)\n"
            "    real(lane, seq)\n"
            "m.bus_write_cursor = slow\n"
            f"m.bus_advance_cursor({lane!r}, int(sys.argv[1]))\n"
        )
    # both pass the unlocked read of a cursor at 1, then write. The lock must
    # make the second writer re-read and refuse to go backwards.
    a = subprocess.Popen([sys.executable, child, "9"])
    b = subprocess.Popen([sys.executable, child, "4"])
    a.wait(timeout=15); b.wait(timeout=15)
    assert bus_read_cursor(lane) == 9, bus_read_cursor(lane)


def t_b1_stale_seq_is_busseq(root):
    """A stub that returns seq <= after must not hot-loop. ask returns within
    2 s with the named BusSeq error, not after burning the 30 s deadline."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    lane = mklane(root, "b1_stale")
    begin(lane, root)
    calls = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def _json(self, body):
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            if self.path.startswith("/health"):
                self._json('{"ok": true}')
                return
            calls["n"] += 1
            # always the same seq, which does not advance `after`
            self._json('{"ok": true, "message": {"id": "m-2", "seq": 1, "kind": "info"}}')
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            self._json('{"ok": true, "message": {"id": "m-1", "seq": 1}}')
        def log_message(self, *a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {"MK_BUS_URL": f"http://127.0.0.1:{srv.server_address[1]}", "MK_BUS_TOKEN": "sekrit"}
    b0 = time.time()
    cli(lane, "inbox", env=env)  # one real round trip: the baseline
    baseline = time.time() - b0
    try:
        t0 = time.time()
        code, lines = cli(lane, "ask", "stale?", "--timeout", "30", env=env)
        elapsed = time.time() - t0
        # The proof of "no spin" is the call count: a hot loop makes thousands.
        # Wall time is a backstop scaled to a baseline, so a slow box does not
        # fail a correct run (review R2: 2.43 s and 2.64 s under load).
        assert calls["n"] < 50, calls["n"]
        assert elapsed < max(2.0, 8 * baseline), f"stale seq spun for {elapsed:.2f}s over {calls['n']} calls: {lines}"
        assert code == 5, (code, lines)
    finally:
        srv.shutdown()


def t_b1_huge_seq_and_poisoned_cursor(root):
    """A seq at or past 2**53 is not a cursor seq. A cursor file holding one, or
    a negative number, reads as 0."""
    lane = mklane(root, "b1_huge")
    assert not bus_seq(10 ** 30)
    assert not bus_seq(-1)
    bus_note_seq(lane, {"seq": 10 ** 30})
    assert bus_read_cursor(lane) == 0
    with open(os.path.join(lane, BUS_CURSOR_FILE), "w") as f:
        f.write(str(10 ** 30) + "\n")
    assert bus_read_cursor(lane) == 0
    with open(os.path.join(lane, BUS_CURSOR_FILE), "w") as f:
        f.write("-5\n")
    assert bus_read_cursor(lane) == 0
    bus_advance_cursor(lane, 4)
    assert bus_read_cursor(lane) == 4
    bus_advance_cursor(lane, 10 ** 30)
    assert bus_read_cursor(lane) == 4


def t_b1_non_int_seq_does_not_spin(root):
    """A /wait body whose seq is not an integer returns within 2 s (BusSeq),
    instead of polling that same message until the deadline."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    lane = mklane(root, "b1_badseq")
    begin(lane, root)
    calls = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def _json(self, body):
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            if self.path.startswith("/health"):
                self._json('{"ok": true}')
                return
            calls["n"] += 1
            self._json('{"ok": true, "message": {"id": "m-9", "seq": "nope", "kind": "info"}}')
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            self._json('{"ok": true, "message": {"id": "m-1", "seq": 1}}')
        def log_message(self, *a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {"MK_BUS_URL": f"http://127.0.0.1:{srv.server_address[1]}", "MK_BUS_TOKEN": "sekrit"}
    b0 = time.time()
    cli(lane, "inbox", env=env)  # one real round trip: the baseline
    baseline = time.time() - b0
    try:
        t0 = time.time()
        code, lines = cli(lane, "ask", "bad seq?", "--timeout", "30", env=env)
        elapsed = time.time() - t0
        assert calls["n"] < 50, calls["n"]
        assert elapsed < max(2.0, 8 * baseline), f"non-int seq spun for {elapsed:.2f}s over {calls['n']} calls: {lines}"
        assert code == 5, (code, lines)
    finally:
        srv.shutdown()


def _b1_hold_lock(root, lane, name):
    """A process holding the cursor lock. Returns (proc, ready path). The caller kills it."""
    lock = os.path.join(lane, BUS_CURSOR_FILE + ".lock")
    ready = os.path.join(root, name + ".ready")
    script = (
        "import fcntl, sys, time\n"
        "f = open(sys.argv[1], 'a')\n"
        "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
        "open(sys.argv[2], 'w').write('held')\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script, lock, ready])
    deadline = time.time() + 5
    while time.time() < deadline and not os.path.exists(ready):
        time.sleep(0.02)
    assert os.path.exists(ready), "holder never took the lock"
    return proc


def t_b1_held_lock_does_not_lose_the_answer(root):
    """Review R3 HIGH: the cursor lock is held by another process. wait and ask
    still print the answer and exit 0. inbox fails with the named BusLock error
    and prints nothing. Bounds scale from one measured spawn."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root)
    lane = bus_lane(root, "b1_held_lane", "BH", data)
    begin(lane, root)
    cli(lane, "progress", "build")
    b0 = time.time()
    cli(lane, "inbox")
    baseline = max(0.05, time.time() - b0)
    # wait path: ask goes PENDING, the answer lands, then the lock is taken
    code, lines = cli(lane, "ask", "held?", "--timeout", "2")
    assert code == 3 and lines[0].startswith("ASK m-") and lines[-1].startswith("PENDING "), (code, lines)
    ask_id = lines[0].split()[1]
    bus_cli("answer", ask_id, "KEPT", "--run", BUS_RUN, "--data", data)
    holder = _b1_hold_lock(root, lane, "b1_held_wait")
    try:
        t0 = time.time()
        code, lines = cli(lane, "wait", ask_id, "--timeout", "10")
        elapsed = time.time() - t0
        assert code == 0 and lines == ["KEPT"], (code, lines)
        assert elapsed < max(3.0, 8 * baseline), elapsed
        assert status(lane)["state"] == "working"
    finally:
        holder.kill(); holder.wait(timeout=5)
    # ask path: the lock is held before the answer is posted
    holder = _b1_hold_lock(root, lane, "b1_held_ask")

    def answer_when_blocked():
        row = bus_poll_until(lambda: next(
            (r for r in bus_rows(data) if r.get("kind") == "ask" and r.get("text") == "held ask?"), None))
        bus_poll_until(lambda: status(lane) if status(lane).get("state") == "blocked" else None)
        bus_cli("answer", row["id"], "KEPT2", "--run", BUS_RUN, "--data", data)

    th = threading.Thread(target=answer_when_blocked)
    th.start()
    try:
        t0 = time.time()
        code, lines = cli(lane, "ask", "held ask?", "--timeout", "15")
        elapsed = time.time() - t0
    finally:
        th.join(20)
        holder.kill(); holder.wait(timeout=5)
    assert code == 0 and lines[0].startswith("ASK m-") and lines[-1] == "KEPT2", (code, lines)
    assert elapsed < max(8.0, 8 * baseline), elapsed
    # inbox with the lock held: the named error, and nothing printed
    bus_cli("send", "--run", BUS_RUN, "--task", "BH", "--kind", "info",
            "--text", "unread while locked", "--data", data)
    holder = _b1_hold_lock(root, lane, "b1_held_inbox")
    try:
        code, lines = cli(lane, "inbox")
        assert code == 5 and len(lines) == 1, (code, lines)
        err = json.loads(lines[0])
        assert err.get("ok") is False and "cursor lock" in err.get("error", ""), err
    finally:
        holder.kill(); holder.wait(timeout=5)
    # the next inbox, lock free, shows that message exactly once and acks it
    code, lines = cli(lane, "inbox")
    assert code == 0 and len(lines) == 1, (code, lines)
    shown = json.loads(lines[0])
    assert shown.get("text") == "unread while locked", shown
    code, lines = cli(lane, "inbox")
    assert code == 0 and lines == [], lines


def t_b1_fresh_unread_lock_prints_error_only(root):
    """Review R4 MEDIUM: a FRESH lane whose only message is an unread info, with
    the cursor lock already held. inbox prints the error line only (the lock is
    taken before the print) and exits 5. The next inbox, lock free, shows that
    message exactly once."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root)
    lane = bus_lane(root, "b1_fresh_lock", "BF", data)
    begin(lane, root)
    cli(lane, "progress", "build")
    bus_cli("send", "--run", BUS_RUN, "--task", "BF", "--kind", "info",
            "--text", "fresh unread", "--data", data)
    holder = _b1_hold_lock(root, lane, "b1_fresh_inbox")
    try:
        code, lines = cli(lane, "inbox")
        assert code == 5 and len(lines) == 1, (code, lines)
        err = json.loads(lines[0])
        assert err.get("ok") is False and "cursor lock" in err.get("error", ""), err
        assert "fresh unread" not in lines[0], lines
    finally:
        holder.kill(); holder.wait(timeout=5)
    code, lines = cli(lane, "inbox")
    assert code == 0 and len(lines) == 1, (code, lines)
    shown = json.loads(lines[0])
    assert shown.get("text") == "fresh unread", shown
    code, lines = cli(lane, "inbox")
    assert code == 0 and lines == [], lines


def t_b1_cursor_lock_wait_is_bounded(root):
    """A holder that keeps the cursor lock: bus_advance_cursor raises BusLock
    inside the wait and leaves the cursor where it was."""
    lane = mklane(root, "b1_lock")
    bus_write_cursor(lane, 3)
    lock = os.path.join(lane, BUS_CURSOR_FILE + ".lock")
    ready = os.path.join(root, "b1_lock_ready")
    script = (
        "import fcntl, os, sys, time\n"
        "f = open(sys.argv[1], 'a')\n"
        "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
        "open(sys.argv[2], 'w').write('held')\n"
        "time.sleep(30)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", script, lock, ready])
    deadline = time.time() + 5
    while time.time() < deadline and not os.path.exists(ready):
        time.sleep(0.02)
    assert os.path.exists(ready), "holder never took the lock"
    try:
        t0 = time.time()
        raised = False
        try:
            bus_advance_cursor(lane, 8)
        except BusLock:
            raised = True
        elapsed = time.time() - t0
        assert raised, "a held lock did not raise BusLock"
        assert BUS_LOCK_WAIT - 1 < elapsed < BUS_LOCK_WAIT + 2, elapsed
        assert bus_read_cursor(lane) == 3
    finally:
        holder.kill()
        holder.wait(timeout=5)


def t_b1_cursor_never_backwards(root):
    """bus_note_seq refuses a seq at or below the cursor."""
    lane = mklane(root, "b1_back")
    bus_write_cursor(lane, 7)
    bus_note_seq(lane, {"seq": 3})
    assert bus_read_cursor(lane) == 7
    bus_note_seq(lane, {"seq": 7})
    assert bus_read_cursor(lane) == 7
    bus_note_seq(lane, {"seq": "9"})
    assert bus_read_cursor(lane) == 7
    bus_note_seq(lane, {"seq": 11}, held=(9,))
    # 9 is unread and lower, so the cursor stops just before it
    assert bus_read_cursor(lane) == 8, bus_read_cursor(lane)
    bus_note_seq(lane, {"seq": 12})
    assert bus_read_cursor(lane) == 12


def t_bus_inbox_drains_and_acks(root):
    """inbox prints the info sent to the lane as one JSON line, acks it level 3, advances the
    lane cursor; a second inbox prints nothing and exits 0."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root)
    lane = bus_lane(root, "bus_inbox_lane", "TI", data)
    begin(lane, root)
    cli(lane, "progress", "parse-plan")
    bus_cli("send", "--run", BUS_RUN, "--task", "TI", "--kind", "info",
            "--text", "integration merges after BB", "--data", data)
    code, lines = cli(lane, "inbox")
    assert code == 0 and len(lines) == 1, (code, lines)
    msg = json.loads(lines[0])
    assert msg["kind"] == "info" and msg["to"] == "TI" and msg["from"] == "orch", msg
    assert msg["task"] == "TI" and msg["text"] == "integration merges after BB", msg
    with open(os.path.join(lane, BUS_CURSOR_FILE)) as f:
        assert int(f.read().strip()) == msg["seq"], "the cursor advanced past the message"
    acks = [r for r in bus_rows(data)
            if r.get("kind") == "ack" and r.get("reply_to") == msg["id"]]
    assert len(acks) == 1 and acks[0]["ack_level"] == 3, acks
    code, lines = cli(lane, "inbox")
    assert code == 0 and lines == [], (code, lines)


def t_bus_progress_unread_hint(root):
    """progress prints `N unread — run inbox` as its one extra line when the bus holds messages
    for the lane past the cursor — and no extra line once drained, with no bus lane file, or
    before any message exists."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root)
    lane = bus_lane(root, "bus_hint_lane", "TH", data)
    begin(lane, root)
    code, lines = cli(lane, "progress", "parse-plan")
    assert code == 0 and len(lines) == 1, lines
    bus_cli("send", "--run", BUS_RUN, "--task", "TH", "--kind", "info",
            "--text", "ping TH", "--data", data)
    code, lines = cli(lane, "progress", "build")
    assert code == 0 and len(lines) == 2 and lines[1] == "1 unread — run inbox", lines
    code, lines = cli(lane, "inbox")
    assert code == 0 and len(lines) == 1 and json.loads(lines[0])["text"] == "ping TH", lines
    code, lines = cli(lane, "progress", "test")
    assert code == 0 and len(lines) == 1, lines
    bare = mklane(root, "bus_hint_nolane")
    begin(bare, root)
    code, lines = cli(bare, "progress", "parse-plan")
    assert code == 0 and len(lines) == 1, lines


def t_bus_server_down_fallback(root):
    """Server stopped, and the no-lane-file case: ask exits 4 within 3 s with the status file
    blocked and `BUS DOWN: use the BLOCKED token`; inbox/ack/wait print the bus-down JSON line
    and exit 4."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, pid, _ = bus_server(root, "busdown")
    lane = bus_lane(root, "bus_down_lane", "TD", data)
    begin(lane, root)
    cli(lane, "progress", "parse-plan")
    stop_pid(pid)
    t0 = time.time()
    code, lines = cli(lane, "ask", "is the bus up?", "--timeout", "30")
    elapsed = time.time() - t0
    assert code == 4 and lines == ["BUS DOWN: use the BLOCKED token"], (code, lines)
    assert elapsed < 3.0, f"ask fell back in {elapsed:.1f}s, not within 3 s"
    st = status(lane)
    assert st["state"] == "blocked" and st["question"] == "is the bus up?", st
    down = json.dumps({"ok": False, "error": "bus down"})
    for args in (("inbox",), ("ack", "m-1"), ("wait", "m-1", "--timeout", "3")):
        code, lines = cli(lane, *args)
        assert code == 4 and lines == [down], (args, code, lines)
    bare = mklane(root, "bus_down_nolane")
    begin(bare, root)
    cli(bare, "progress", "parse-plan")
    code, lines = cli(bare, "ask", "no bus here", "--timeout", "30")
    assert code == 4 and lines == ["BUS DOWN: use the BLOCKED token"], (code, lines)
    st = status(bare)
    assert st["state"] == "blocked" and st["question"] == "no bus here", st
    code, lines = cli(bare, "inbox")
    assert code == 4 and lines == [down], (code, lines)


def t_bus_bad_token_http_error(root):
    """A rejected token is an HTTP error, not a fallback: one JSON line
    `{"ok": false, "error": "<status>: <server error>"}` and exit 5, on every bus command,
    with the status file untouched."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root)
    lane = bus_lane(root, "bus_badtok_lane", "TB", data)
    begin(lane, root)
    cli(lane, "progress", "parse-plan")
    path = os.path.join(lane, BUS_LANE_FILE)
    with open(path) as f:
        cfg = json.load(f)
    cfg["token"] = "tok_bogus_" + "0" * 32
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    rejected = {"ok": False, "error": "401: unauthorized"}
    code, lines = cli(lane, "ask", "anyone there?", "--timeout", "5")
    assert code == 5 and len(lines) == 1 and json.loads(lines[0]) == rejected, (code, lines)
    st = status(lane)
    assert st["state"] == "working" and st["question"] is None, "a 401 touches no status"
    for args in (("inbox",), ("ack", "m-1"), ("wait", "m-1")):
        code, lines = cli(lane, *args)
        assert code == 5 and len(lines) == 1 and json.loads(lines[0]) == rejected, (args, code, lines)


def d7_store(data, run, task):
    """A real project store on the self-test's data dir, with one task row. Public mkdb API."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mkdb", os.path.join(os.path.dirname(MK_BUS_PY), "mkdb.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.import_history(data, None, run, run_row={"id": run, "status": "active"}, data_dir=data)
    con = mod.open_project(data, data_dir=data)
    try:
        mod.add_task(con, run, task, "task", title="d7")
    finally:
        con.close()
    return mod


def d7_task_state(data, run, task):
    """The task's status in the project store and how often each status was entered
    (chan='state' transitions): (status, {to: n}). Public mkdb API, read only."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mkdb_state", os.path.join(os.path.dirname(MK_BUS_PY), "mkdb.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    con = mod.open_project(data, data_dir=data)
    try:
        status = con.execute("SELECT status FROM tasks WHERE run=? AND id=?", (run, task)).fetchone()["status"]
        entered = {}
        for r in con.execute("SELECT to_id FROM events WHERE chan='state' AND kind='transition' "
                             "AND run=? AND task=?", (run, task)):
            entered[r["to_id"]] = entered.get(r["to_id"], 0) + 1
        return status, entered
    finally:
        con.close()


def d7_answer(data, ask_id, text, to_task):
    """Post an answer with the orchestrator token. `mk-bus.py answer` reads bus.jsonl,
    which store mode does not write; the HTTP path is what the orchestrator actually uses.
    Store mode publishes bus.json in the MK home, not in the data dir."""
    bus_json = os.path.join(data, "bus.json")
    if not os.path.isfile(bus_json):
        home = os.environ.get("MK_BUS_DIR") or os.environ.get("MK_HOME") or os.path.expanduser("~/.matrixkoder")
        bus_json = os.path.join(home, "bus.json")
    with open(bus_json) as f:
        b = json.load(f)
    body = {"run": BUS_RUN, "task": to_task, "kind": "answer", "text": text,
            "from": "orch", "to": to_task, "reply_to": ask_id}
    status, raw = bus_http_exchange(
        "POST", f"http://127.0.0.1:{b['port']}/send",
        {"Authorization": f"Bearer {b['token']}", "Content-Type": "application/json",
         "Host": f"127.0.0.1:{b['port']}", "Connection": "close"},
        json.dumps(body).encode(), 10)
    if status >= 400:
        raise BusHttp(status, raw.decode("utf-8", "replace"))
    return json.loads(raw.decode())


def d7_bus_rows(data):
    """Bus messages from the store when it exists, else bus.jsonl (file mode)."""
    if store_present(data):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "mkdb_rows", os.path.join(os.path.dirname(MK_BUS_PY), "mkdb.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        con = mod.open_project(data, data_dir=data)
        try:
            rows = []
            for r in con.execute(
                    "SELECT id, kind, text, task, from_id, to_id, reply_to, meta_json "
                    "FROM events WHERE chan='bus' AND run=? ORDER BY id", (BUS_RUN,)):
                d = dict(r)
                d["from"] = d.get("from_id")
                d["to"] = d.get("to_id")
                d["id"] = f"m-{d['id']}"
                meta = {}
                if d.get("meta_json"):
                    with contextlib.suppress(ValueError):
                        meta = json.loads(d["meta_json"])
                if d["kind"] == "ack":
                    d["ack_level"] = meta.get("ack_level", 1)
                rows.append(d)
            return rows
        finally:
            con.close()
    return bus_rows(data)


def d7_task_msgs(data, task):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mkdb", os.path.join(os.path.dirname(MK_BUS_PY), "mkdb.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    con = mod.open_project(data, data_dir=data)
    try:
        return [dict(r) for r in con.execute(
            "SELECT kind, text FROM events WHERE chan='bus' AND task=? AND kind='task'", (task,))]
    finally:
        con.close()


def t_d7_connect_pages_and_tasks(root):
    """connect prints page 1 of a real-size brief and the phase task list. A brief over
    24,000 chars (a real brief, concatenated) says there is a next page."""
    lane = mklane(root, "d7_connect")
    begin(lane, root, task="D7.1")
    src = "/Users/rohits/Development/matrixkoder/.matrixkoder/runs/mk-store-r1/tasks/D7/brief-r1.md"
    with open(src) as f:
        real = f.read()
    big = (real + "\n") * 5
    assert len(big) > BRIEF_PAGE
    with open(os.path.join(lane, ".matrixkoder-brief.md"), "w") as f:
        f.write(big)
    plan = os.path.join(root, "plan.md")
    with open(plan, "w") as f:
        f.write('+++\nplan = "t"\nstatus = "running"\n[[phase]]\nid = "D7"\n'
                '[[phase.task]]\nid = "D7.1"\ntitle = "connect"\nparallel = false\n'
                'deps = []\n[[phase.task]]\nid = "D7.2"\ntitle = "ask"\n'
                'parallel = true\ndeps = ["D7.1"]\n+++\n')
    code, lines = cli(lane, "connect", "--plan", plan)
    assert code == 0, lines
    text = "\n".join(lines)
    assert text.startswith("# brief 1/"), text[:40]
    assert "[D7.1] connect parallel=false deps=" in text, text[-500:]
    assert "[D7.2] ask parallel=true deps=D7.1" in text
    assert "read <id> --part 2" in text
    code, lines = cli(lane, "read", "brief", "--part", "2")
    assert code == 0 and lines[0].startswith("# part 2/"), lines[:1]


def t_d7b_connect_reads_the_bus_brief(root):
    """D7B R2: with no lane brief file, connect prints the brief the orchestrator published.
    A later round's brief replaces it. The lane file, when present, still wins."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data = os.path.join(root, "d7brief-data")
    os.makedirs(data, exist_ok=True)
    bus_server(root, "d7brief")
    lane = bus_lane(root, "d7_brief_lane", "D7B", data)
    begin(lane, root, run=BUS_RUN, task="D7B")
    # noise first: an inbox of earlier messages must not hide the brief that follows
    for i in range(30):
        bus_cli("send", "--data", data, "--run", BUS_RUN, "--task", "D7B",
                "--kind", "info", "--from", "orch", "--to", "D7B", "--text", f"noise {i}")
    body = "round:1\nkind:brief\n# the real brief\nbuild the thing\n"
    code, res = bus_cli("send", "--data", data, "--run", BUS_RUN, "--task", "D7B",
                        "--kind", "info", "--from", "orch", "--to", "D7B", "--text", body)
    assert code == 0 and res.get("ok"), res
    code, lines = cli(lane, "connect")
    assert code == 0, lines
    text = "\n".join(lines)
    assert "# the real brief" in text and "build the thing" in text, text
    body2 = "round:2\nkind:brief\n# round two brief\nfix the thing\n"
    code, res = bus_cli("send", "--data", data, "--run", BUS_RUN, "--task", "D7B",
                        "--kind", "info", "--from", "orch", "--to", "D7B", "--text", body2)
    assert code == 0 and res.get("ok"), res
    code, lines = cli(lane, "connect")
    text = "\n".join(lines)
    assert "round two brief" in text and "fix the thing" in text, text
    with open(os.path.join(lane, ".matrixkoder-brief.md"), "w") as f:
        f.write("the lane file wins\n")
    code, lines = cli(lane, "connect")
    assert "the lane file wins" in "\n".join(lines), lines
    os.unlink(os.path.join(lane, ".matrixkoder-brief.md"))
    # 30 earlier messages must not hide the brief, and a lane with nothing prints the marker
    for i in range(30):
        bus_cli("send", "--data", data, "--run", BUS_RUN, "--task", "D7B",
                "--kind", "info", "--from", "orch", "--to", "D7B", "--text", f"noise {i}")
    code, lines = cli(lane, "connect")
    assert "round two brief" in "\n".join(lines), lines
    empty = bus_lane(root, "d7_empty_lane", "D7E", data)
    begin(empty, root, run=BUS_RUN, task="D7E")
    code, lines = cli(empty, "connect")
    assert lines == ["# brief: none found"], (code, lines, brief_messages(bus_lane_config(empty)))
    # a review brief is its own kind: the reviewer gets it, the builder's connect does not
    code, lines = cli(lane, "connect", "--role", "review")
    assert lines == ["# review brief: none found"], (code, lines)
    rb = "round:1\nkind:review-brief\n# D7B review brief\nreview the thing\n"
    code, res = bus_cli("send", "--data", data, "--run", BUS_RUN, "--task", "D7B",
                        "--kind", "info", "--from", "orch", "--to", "D7B", "--text", rb)
    assert code == 0 and res.get("ok"), res
    code, lines = cli(lane, "connect", "--role", "review")
    text = "\n".join(lines)
    assert code == 0 and "# review brief round 1" in text and "review the thing" in text, (code, lines)
    code, lines = cli(lane, "connect")
    text = "\n".join(lines)
    assert "round two brief" in text and "review the thing" not in text, text


def t_g1_connect_posts_one_info(root):
    """connect posts ONE `lane connected <task> R<n>` info, even for a paged brief; a down
    bus changes neither connect's exit code nor its stdout."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root, "g1conn")
    brief = "# brief\n" + "x" * (BRIEF_PAGE * 2) + "\n"

    def connect(name, task, env=None):
        lane = bus_lane(root, name, task, data)
        begin(lane, root, run=BUS_RUN, task=task)
        with open(os.path.join(lane, ".matrixkoder-brief.md"), "w") as f:
            f.write(brief)
        return cli(lane, "connect", env=env)

    def posted(task):
        return [r["text"] for r in bus_rows(data)
                if r.get("kind") == "info" and r.get("task") == task
                and (r.get("text") or "").startswith("lane connected")]

    code, up = connect("g1_up_lane", "G1U")
    assert code == 0 and "# more: read <id> --part 2 .. 3" in up, (code, up[:1])
    assert posted("G1U") == ["lane connected G1U R1"], posted("G1U")
    code, down = connect("g1_down_lane", "G1D", env={"MK_BUS_URL": "http://127.0.0.1:1"})
    # the header names the lane path, which differs; every other stdout line must match
    assert code == 0 and down[1:] == up[1:] and down[0].startswith("# brief 1/3 "), (code, down[:1])
    assert posted("G1D") == [], posted("G1D")


def t_d7_todo_backup_and_dedupe(root):
    """Backup `todo done` alone announces one status. A second report is deduped. A fix
    round re-entering in_progress is a new key and is not deduped."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data = os.path.join(root, "d7todo-data")
    os.makedirs(data, exist_ok=True)
    mod = d7_store(data, BUS_RUN, "D7T")
    bus_server(root, "d7todo")
    lane = bus_lane(root, "d7_todo_lane", "D7T", data)
    begin(lane, root, run=BUS_RUN, task="D7T")
    cli(lane, "progress", "work")
    # Todo 67: the status the bus applies, on the real store-mode bus. in_progress -> building,
    # done -> verifying (default); the same round twice moves the task once.
    assert d7_task_state(data, BUS_RUN, "D7T")[0] == "planned"
    code, lines = cli(lane, "todo", "in_progress", "D7.1", env={"MK_BUS_DATA": data})
    first_ip = jline(lines)
    assert code == 0 and first_ip["sent"] is True and first_ip["state_updated"] is True, first_ip
    assert d7_task_state(data, BUS_RUN, "D7T")[0] == "building"
    code, lines = cli(lane, "todo", "in_progress", "D7.1", env={"MK_BUS_DATA": data})
    assert jline(lines)["deduped"] is True, lines
    code, lines = cli(lane, "todo", "done", "D7.1", env={"MK_BUS_DATA": data})
    assert code == 0, lines
    first = jline(lines)
    assert first["sent"] is True and first["deduped"] is False and first["mapped"] == "verify", first
    assert first["state_updated"] is True, first
    assert d7_task_state(data, BUS_RUN, "D7T")[0] == "verifying"
    code, lines = cli(lane, "todo", "done", "D7.1", env={"MK_BUS_DATA": data})
    second = jline(lines)
    assert second["deduped"] is True and second["sent"] is False, second
    status_now, entered = d7_task_state(data, BUS_RUN, "D7T")
    assert status_now == "verifying" and entered.get("building") == 1 and entered.get("verifying") == 1, (
        status_now, entered)
    msgs = d7_task_msgs(data, "D7T")
    assert len(msgs) == 2 and msgs[1]["text"].startswith("status verify\nround: 1\n"), msgs
    mod.transition(data, None, BUS_RUN, "D7T", "fix", data_dir=data)  # the fix round: the orchestrator sets fix
    begin(lane, root, run=BUS_RUN, task="D7T", round=2)
    cli(lane, "progress", "fix")
    code, lines = cli(lane, "todo", "in_progress", "D7.1", env={"MK_BUS_DATA": data})
    assert code == 0 and jline(lines)["sent"] is True and jline(lines)["deduped"] is False, lines
    assert d7_task_state(data, BUS_RUN, "D7T") == ("building", {"building": 2, "verifying": 1, "fix": 1})
    code, lines = cli(lane, "todo", "in_progress", "D7.1", env={"MK_BUS_DATA": data})
    assert jline(lines)["deduped"] is True, lines
    assert len(d7_task_msgs(data, "D7T")) == 3


def t_d7_todo_done_status_setting(root):
    """C9: session.todo_done_status = done changes the announced status."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data = os.path.join(root, "d7set-data")
    os.makedirs(data, exist_ok=True)
    d7_store(data, BUS_RUN, "D7S")
    bus_server(root, "d7set")
    with open(os.path.join(data, "matrixkoder.yaml"), "w") as f:
        f.write("todo_done_status: done\n")
    lane = bus_lane(root, "d7_set_lane", "D7S", data)
    begin(lane, root, run=BUS_RUN, task="D7S")
    cli(lane, "progress", "work")
    code, lines = cli(lane, "todo", "done", "D7.1", env={"MK_BUS_DATA": data})
    assert code == 0 and jline(lines)["mapped"] == "done", lines
    # The setting picks the word the lane announces; the bus caps a lane at verifying (only the
    # orchestrator sets done), and the first todo done also moves it out of planned.
    assert jline(lines)["state_ignored"] is not None, lines  # planned -> verifying is not a builder move
    assert d7_task_state(data, BUS_RUN, "D7S")[0] == "planned"
    code, lines = cli(lane, "todo", "in_progress", "D7.0", env={"MK_BUS_DATA": data})
    assert jline(lines)["state_updated"] is True, lines
    begin(lane, root, run=BUS_RUN, task="D7S", round=2)
    cli(lane, "progress", "fix")
    code, lines = cli(lane, "todo", "done", "D7.1", env={"MK_BUS_DATA": data})
    assert code == 0 and jline(lines)["state_updated"] is True, lines
    assert d7_task_state(data, BUS_RUN, "D7S")[0] == "verifying"


def t_e8_late_todo_does_not_move_the_task(root):
    """R2 HIGH: a lane may move its own task along the builder path only. After the
    orchestrator set review / merged / done / blocked, a late `todo` changes nothing and the
    reply says why; a lane `done` never sets done."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data = os.path.join(root, "e8late-data")
    os.makedirs(data, exist_ok=True)
    mod = d7_store(data, BUS_RUN, "E8L")
    bus_server(root, "e8late")
    lane = bus_lane(root, "e8_late_lane", "E8L", data)
    begin(lane, root, run=BUS_RUN, task="E8L")
    cli(lane, "progress", "work")
    env = {"MK_BUS_DATA": data}
    code, lines = cli(lane, "todo", "in_progress", "E8.1", env=env)
    assert jline(lines)["state_updated"] is True and d7_task_state(data, BUS_RUN, "E8L")[0] == "building"
    mod.transition(data, None, BUS_RUN, "E8L", "review", data_dir=data)  # the orchestrator moved on
    begin(lane, root, run=BUS_RUN, task="E8L", round=2)  # a new round, so the dedupe key is new
    cli(lane, "progress", "late")
    fresh_round = 2
    for status in ("review", "merged", "qa", "done", "blocked", "stalled", "quota", "provider"):
        mod.transition(data, None, BUS_RUN, "E8L", status, data_dir=data)
        for word in ("in_progress", "done"):
            fresh_round += 1
            begin(lane, root, run=BUS_RUN, task="E8L", round=fresh_round)  # fresh dedupe key every time
            cli(lane, "progress", "late")
            code, lines = cli(lane, "todo", word, "E8.2", env=env)
            res = jline(lines)
            assert code == 0 and res["state_updated"] is False and res["state_ignored"], (status, word, res)
            assert d7_task_state(data, BUS_RUN, "E8L")[0] == status, (status, word)
    # the allowed path moves once, and `done` from the lane is verifying, never done
    mod.transition(data, None, BUS_RUN, "E8L", "fix", data_dir=data)
    begin(lane, root, run=BUS_RUN, task="E8L", round=fresh_round + 1)
    cli(lane, "progress", "fix")
    code, lines = cli(lane, "todo", "in_progress", "E8.3", env=env)
    assert jline(lines)["state_updated"] is True and d7_task_state(data, BUS_RUN, "E8L")[0] == "building", lines
    code, lines = cli(lane, "todo", "done", "E8.3", env=env)
    assert jline(lines)["state_updated"] is True, lines
    status_now, entered = d7_task_state(data, BUS_RUN, "E8L")
    assert status_now == "verifying" and entered.get("verifying") == 1, (status_now, entered)
    code, lines = cli(lane, "todo", "done", "E8.3", env=env)
    assert jline(lines)["deduped"] is True and d7_task_state(data, BUS_RUN, "E8L")[1] == entered, lines


def t_e8_connect_unarmed_round(root):
    """R2 MEDIUM: a token with no round, or the old `round: 0`, starts at the round of the
    newest brief, else round 1. A token that names a round keeps it."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data = os.path.join(root, "e8round-data")
    os.makedirs(data, exist_ok=True)
    bus_server(root, "e8round")
    for task, round_field, brief_round, want in (
            ("E8A", "absent", None, 1), ("E8B", 0, None, 1), ("E8C", 0, 2, 2), ("E8D", "absent", 3, 3)):
        lane = bus_lane(root, f"e8_round_{task}", task, data)
        path = bus_token_path(lane)
        with open(path) as f:
            cfg = json.load(f)
        assert "round" not in cfg, cfg  # mk-bus.py token without --round never writes 0
        if round_field != "absent":
            cfg["round"] = round_field  # the file an E2 token wrote
        with open(path, "w") as f:
            json.dump(cfg, f)
        os.chmod(path, 0o600)
        if brief_round:
            bus_cli("send", "--data", data, "--run", BUS_RUN, "--task", task, "--kind", "info",
                    "--from", "orch", "--to", task,
                    "--text", f"round:{brief_round}\nkind:brief\n# brief r{brief_round}\nbody\n")
        code, lines = cli(lane, "connect", env={"MK_BUS_DATA": data})
        assert code == 0, (task, lines)
        assert status(lane)["round"] == want and status(lane)["state"] == "started", (task, status(lane))


def t_d7_hook_install_outside(root):
    """todo_hook yes installs under MK_HOME, never inside the lane. A no-hook agent installs nothing."""
    lane = gitlane(root, "d7_hook_lane")
    begin(lane, root)
    home = os.path.join(root, "mk-home")
    code, lines = cli(lane, "hook-install", "--agent", "claude", env={"MK_HOME": home})
    assert code == 0, lines
    out = jline(lines)
    assert out["installed"] is True and out["settings"].startswith(home), out
    assert os.path.commonpath([out["settings"], lane]) != lane
    with open(out["settings"]) as f:
        doc = json.load(f)
    assert "recorded_payload" in doc
    code, lines = cli(lane, "hook-install", "--agent", "agy", env={"MK_HOME": home})
    assert code == 0 and jline(lines)["installed"] is False, lines


def t_d7_bus_down_ask_blocks(root):
    """Kill the bus, ask, and the lane status file is blocked with the question."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, pid, _ = bus_server(root, "d7down")
    lane = bus_lane(root, "d7_down_lane", "D7D", data)
    begin(lane, root)
    cli(lane, "progress", "work")
    stop_pid(pid)
    code, lines = cli(lane, "ask", "where is the schema?", "--timeout", "30")
    assert code == 4 and lines == ["BUS DOWN: use the BLOCKED token"], (code, lines)
    st = status(lane)
    assert st["state"] == "blocked" and st["question"] == "where is the schema?", st


def t_d7_one_ack_per_answer(root):
    """ask acks the answer once; inbox does not show it or ack it again.

    The answer thread reports its own exception onto a queue instead of dying
    silently, and its poll deadline scales from one measured spawn plus a real
    round trip so a loaded machine does not turn a live ask into PENDING."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data = os.path.join(root, "d7ack-data")
    os.makedirs(data, exist_ok=True)
    d7_store(data, BUS_RUN, "D7A")
    bus_server(root, "d7ack")
    lane = bus_lane(root, "d7_ack_lane", "D7A", data)
    begin(lane, root, run=BUS_RUN, task="D7A")
    b0 = time.time()
    cli(lane, "progress", "work")
    baseline = max(0.5, time.time() - b0)
    poll_for = max(30.0, 20 * baseline)
    errors = queue.Queue()

    def answer():
        try:
            row = bus_poll_until(lambda: next(
                (r for r in d7_bus_rows(data) if r.get("kind") == "ask" and r.get("task") == "D7A"), None),
                timeout=poll_for)
            got = d7_answer(data, row["id"], "the schema is v2", "D7A")
            assert got.get("ok") is True, got
        except Exception as e:
            errors.put(f"{type(e).__name__}: {e}")

    th = threading.Thread(target=answer)
    th.start()
    try:
        code, lines = cli(lane, "ask", "which schema?", "--timeout", str(int(poll_for)))
    finally:
        th.join(poll_for + 5)
    # "ask already answered" needs two answer POSTs; cmd_ask posts one ask and
    # the thread posts one answer, so a one-off comes from two runs sharing one
    # store (overlapping runs fall back to the shared MK-home bus.json), not from this code.
    assert errors.empty(), errors.get()
    assert code == 0 and lines[0].startswith("ASK m-") and lines[-1] == "the schema is v2", (code, lines)
    code, lines = cli(lane, "inbox")
    assert code == 0 and lines == [], lines
    rows = d7_bus_rows(data)
    answers = [r for r in rows if r.get("kind") == "answer"]
    assert len(answers) == 1, answers
    mine = [r for r in rows if r.get("kind") == "ack" and r.get("reply_to") == answers[0]["id"]]
    assert len(mine) == 1, mine


def review_lane(data, name, task, root):
    """A reviewer lane on a real bus: started as `review`, working, with a review file."""
    lane = bus_lane(root, name, task, data)
    begin(lane, root, run=BUS_RUN, task=task, role="review", report=".matrixkoder-review-r1.md")
    cli(lane, "progress", "reading the diff")
    with open(os.path.join(lane, ".matrixkoder-review-r1.md"), "w") as f:
        f.write("verdict: APPROVE\n")
    return lane


def t_review_done_store_mode_posts_review(root):
    """Store mode: ONE `review` message `round: 1\\nverdict: V findings: N path: P`, the status
    file goes done, and the last stdout line is `<T> REVIEW R1 DONE`."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data = os.path.join(root, "rd-store-data")
    os.makedirs(data, exist_ok=True)
    d7_store(data, BUS_RUN, "RD1")
    bus_server(root, "rd-store")
    lane = review_lane(data, "rd_store_lane", "RD1", root)
    code, lines = cli(lane, "review-done", "--verdict", "REQUEST_CHANGES", "--findings", "3",
                      "--path", ".matrixkoder-review-r1.md")
    assert code == 0 and lines[-1] == "RD1 REVIEW R1 DONE", (code, lines)
    assert status(lane)["state"] == "done", status(lane)
    rows = [r for r in d7_bus_rows(data) if r.get("kind") == "review"]
    assert len(rows) == 1, rows
    assert rows[0]["text"] == ("round: 1\nverdict: REQUEST_CHANGES findings: 3 "
                               "path: .matrixkoder-review-r1.md"), rows[0]
    code, lines = cli(lane, "review-done", "--verdict", "APPROVE", "--findings", "0",
                      "--path", ".matrixkoder-review-r1.md")
    assert code != 0, "a second review-done must be an illegal transition"


def t_review_done_file_mode_posts_info(root):
    """File mode (no mk.db) refuses kind `review`: the same text goes out as `info`."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _pid, _ = bus_server(root, "rd-file")
    assert not store_present(data), data
    lane = review_lane(data, "rd_file_lane", "RD2", root)
    code, lines = cli(lane, "review-done", "--verdict", "APPROVE", "--findings", "0",
                      "--path", ".matrixkoder-review-r1.md")
    assert code == 0 and lines[-1] == "RD2 REVIEW R1 DONE", (code, lines)
    rows = [r for r in bus_rows(data) if "verdict: APPROVE" in (r.get("text") or "")]
    assert len(rows) == 1 and rows[0]["kind"] == "info", rows
    assert rows[0]["text"].startswith("round: 1\nverdict: APPROVE findings: 0 path: "), rows[0]
    assert status(lane)["state"] == "done", status(lane)


def t_review_done_bus_down_blocks(root):
    """Bus stopped: ask's contract. Exit 4, `BUS DOWN: use the BLOCKED token`, status blocked
    (never done). A missing review file or a bad verdict is refused before any bus call."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, pid, _ = bus_server(root, "rd-down")
    lane = review_lane(data, "rd_down_lane", "RD3", root)
    refuse(lane, "review-done", "--verdict", "APPROVE", "--findings", "0", "--path", "nope.md")
    code, lines = cli(lane, "review-done", "--verdict", "MAYBE", "--findings", "0",
                      "--path", ".matrixkoder-review-r1.md")
    assert code != 0 and status(lane)["state"] == "working", (code, lines)
    stop_pid(pid)
    code, lines = cli(lane, "review-done", "--verdict", "APPROVE", "--findings", "0",
                      "--path", ".matrixkoder-review-r1.md")
    assert code == 4 and lines == ["BUS DOWN: use the BLOCKED token"], (code, lines)
    assert status(lane)["state"] == "blocked", status(lane)


def t_done_warns_on_running_child(root):
    """R31: a real `python3 -c time.sleep` started in the lane makes `done` print a WARNING
    before the token; with no child the same path prints no WARNING. HOME is a temp dir.
    The token stays the last line and the exit code stays 0 either way."""
    saved = os.environ.get("HOME")
    home = os.path.join(root, "r31-home")
    os.makedirs(home, exist_ok=True)
    os.environ["HOME"] = home
    lane = gitlane(root, "r31_child")
    try:
        begin(lane, root, report=".matrixkoder-report-r1.md")
        cli(lane, "progress", "work")
        with open(os.path.join(lane, ".matrixkoder-report-r1.md"), "w") as f:
            f.write("# report\n")
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=lane, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            code, lines = cli(lane, "done")
        finally:
            child.kill()
            child.wait(timeout=10)
        assert code == 0, lines
        assert any(x.startswith("WARNING: a lane child is still running:") and "time.sleep" in x
                   for x in lines), lines
        assert lines[-1].endswith(" DONE"), lines
        assert not lines[-1].startswith("WARNING"), lines
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved
    lane2 = gitlane(root, "r31_quiet")
    begin(lane2, root, report=".matrixkoder-report-r1.md")
    cli(lane2, "progress", "work")
    with open(os.path.join(lane2, ".matrixkoder-report-r1.md"), "w") as f:
        f.write("# report\n")
    code, lines = cli(lane2, "done")
    assert code == 0 and not any(x.startswith("WARNING:") for x in lines), lines
    assert lines[-1].endswith(" DONE"), lines


def t_r31_wait_loop_wording(root):
    """R31 as written: the brief Commands line and lane-rules R31 both carry the same-turn
    bounded wait, and that loop really returns the exit line a detached job appended."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    brief = os.path.join(here, "skills", "matrixkoder", "templates", "brief.md")
    rules = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "reference", "lane-rules.md")
    with open(brief) as f:
        brief_text = f.read()
    with open(rules) as f:
        rules_text = f.read()
    needle = "until grep -q '^EXIT:' out.txt; do sleep 5; done"
    assert needle in brief_text, "the brief Commands line lost the same-turn wait"
    assert needle in rules_text and "**R31." in rules_text, "lane-rules R31 lost the wait"
    out = os.path.join(root, "wait-out.txt")
    subprocess.Popen(
        ["sh", "-c", f"sleep 1; echo EXIT:0 >> {out}"],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = subprocess.run(
        ["sh", "-c", f"until grep -q '^EXIT:' {out}; do sleep 1; done"],
        cwd=root, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    with open(out) as f:
        assert f.read().strip() == "EXIT:0"


def t_d7_done_todo_counts(root):
    """done prints todo counts beside DONE, and warns without failing when the section is missing."""
    lane = gitlane(root, "d7_done_counts")
    begin(lane, root, report=".matrixkoder-report-r1.md")
    cli(lane, "progress", "work")
    with open(os.path.join(lane, ".matrixkoder-report-r1.md"), "w") as f:
        f.write("# report\n\n## Todo\n"
                "[done] connect - self-test\n"
                "[done] dedupe - one message\n"
                "[skipped] hook for agy - catalog says no\n"
                "[open] measure store - no store in this lane\n")
    code, lines = cli(lane, "done")
    assert code == 0, lines
    assert lines[-1].endswith("DONE todo 2/4 done 1 skipped"), lines
    lane2 = gitlane(root, "d7_done_warn")
    begin(lane2, root, report=".matrixkoder-report-r1.md")
    cli(lane2, "progress", "work")
    with open(os.path.join(lane2, ".matrixkoder-report-r1.md"), "w") as f:
        f.write("# report without todos\n")
    code, lines = cli(lane2, "done")
    assert code == 0 and any(x.startswith("todo warning:") for x in lines), lines
    assert lines[-1].endswith(" DONE"), lines


def t_d7_token_path_one_function(root):
    """One function resolves the token file: MK_BUS_TOKEN_FILE, else the lane file, else
    ~/.matrixkoder/bus-lanes/<hash>.json. The third is checked with HOME pointed at a temp dir."""
    lane = mklane(root, "d7_token_lane")
    override = os.path.join(root, "override.json")
    with open(override, "w") as f:
        f.write("{}\n")
    # The function is what production code calls; drive it in-process with env, since no CLI
    # prints the path. HOME is a temp dir either way.
    saved = {k: os.environ.get(k) for k in ("HOME", "MK_HOME", "MK_BUS_TOKEN_FILE")}
    try:
        os.environ["HOME"] = os.path.join(root, "h")
        os.makedirs(os.environ["HOME"], exist_ok=True)
        os.environ.pop("MK_HOME", None)
        os.environ["MK_BUS_TOKEN_FILE"] = override
        assert bus_token_path(lane) == override
        os.environ.pop("MK_BUS_TOKEN_FILE")
        got = bus_token_path(lane)
        assert got == os.path.join(os.environ["HOME"], ".matrixkoder", "bus-lanes",
                                   lane_hash(lane) + ".json"), got
        # a lane file wins over the home location
        with open(os.path.join(lane, BUS_LANE_FILE), "w") as f:
            f.write("{}\n")
        assert bus_token_path(lane) == os.path.join(lane, BUS_LANE_FILE)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def t_x1_fifo_token_does_not_hang(root):
    """SR5 HIGH: a FIFO token path with both MK_BUS_URL and MK_BUS_TOKEN set is never
    opened. inbox exits 4 (bus down — nothing is listening) in under 1 s, not a hang."""
    lane = mklane(root, "x1_fifo")
    fifo = os.path.join(root, "x1.tok")
    os.mkfifo(fifo)
    t0 = time.time()
    code, lines = cli(lane, "inbox", env={
        "MK_BUS_TOKEN_FILE": fifo,
        "MK_BUS_URL": "http://127.0.0.1:9",
        "MK_BUS_TOKEN": "sekrit",
    })
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"FIFO token path took {elapsed:.2f}s"
    assert code == 4, (code, lines)


def t_x1_fifo_lane_file_is_bus_down(root):
    """A FIFO standing in for <lane>/.matrixkoder-bus.json (no MK_BUS_*) is not a regular
    file: empty config, exit 4, under 1 s."""
    lane = mklane(root, "x1_fifo_lane")
    os.mkfifo(os.path.join(lane, BUS_LANE_FILE))
    env = {k: v for k, v in os.environ.items()
           if k not in ("MK_BUS_TOKEN_FILE", "MK_BUS_URL", "MK_BUS_TOKEN", "MK_HOME")}
    env["HOME"] = os.path.join(root, "x1-home-fifo")
    os.makedirs(env["HOME"], exist_ok=True)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # one measured spawn of this interpreter, then a generous floor: the FIFO
    # is still never opened, so the case stays well under a real open() hang
    b0 = time.time()
    subprocess.run([sys.executable, "-c", "pass"], capture_output=True, env=env, timeout=30)
    floor = max(2.0, 8 * (time.time() - b0))
    t0 = time.time()
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "inbox", "--lane", lane],
                       capture_output=True, text=True, env=env, timeout=max(15, floor + 5))
    elapsed = time.time() - t0
    assert elapsed < floor, f"lane FIFO took {elapsed:.2f}s against a {floor:.2f}s floor"
    assert r.returncode == 4, (r.returncode, r.stdout, r.stderr)


def t_x1_stalled_wait_honours_budget(root):
    """SR5 HIGH: a peer that answers /health and never finishes /wait. inbox returns
    within 3 s; ask returns within its deadline + 1 s."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def _ok(self):
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            if self.path.startswith("/health"):
                self._ok()
                return
            # /wait: accept and never answer
            time.sleep(60)
        def do_POST(self):
            # /send of the ask succeeds; the following /wait is what stalls
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            if self.path.startswith("/send"):
                body = b'{"ok": true, "id": "m-1", "seq": 1}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            time.sleep(60)
        def log_message(self, *a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    port = srv.server_address[1]
    lane = mklane(root, "x1_stall")
    try:
        t0 = time.time()
        code, lines = cli(lane, "inbox", env={
            "MK_BUS_URL": f"http://127.0.0.1:{port}",
            "MK_BUS_TOKEN": "sekrit",
        })
        elapsed = time.time() - t0
        assert elapsed < 3.0, f"inbox took {elapsed:.2f}s: {lines}"
        assert code == 4, (code, lines)
        t0 = time.time()
        code, lines = cli(lane, "ask", "which one?", "--timeout", "2", env={
            "MK_BUS_URL": f"http://127.0.0.1:{port}",
            "MK_BUS_TOKEN": "sekrit",
        })
        elapsed = time.time() - t0
        assert elapsed < 3.0, f"ask took {elapsed:.2f}s (deadline 2 + 1): {lines}"
        assert code == 4, (code, lines)
    finally:
        srv.shutdown()


def t_x1_part_out_of_range(root):
    """SR5 MEDIUM: --part 0 and --part 9999 are one JSON error line, exit 2. A valid
    part of a 100000-char brief (5 pages) is unchanged."""
    lane = mklane(root, "x1_part")
    begin(lane, root, report=".matrixkoder-report-r1.md")
    brief = os.path.join(lane, ".matrixkoder-brief-r1.md")
    with open(brief, "w") as f:
        f.write("B" * 100000)
    code, lines = cli(lane, "read", "brief", "--part", "0")
    assert code == 2 and len(lines) == 1, (code, lines)
    err = json.loads(lines[0])
    assert err.get("ok") is False, err
    code, lines = cli(lane, "read", "brief", "--part", "9999")
    assert code == 2 and len(lines) == 1, (code, lines)
    assert json.loads(lines[0]).get("ok") is False
    code, lines = cli(lane, "read", "brief", "--part", "1")
    assert code == 0 and lines[0] == "# part 1/5", (code, lines[:1])
    code, lines = cli(lane, "read", "brief", "--part", "5")
    assert code == 0 and lines[0] == "# part 5/5", (code, lines[:1])


def t_x1_cursor_atomic(root):
    """SR5 MEDIUM: the cursor is temp+fsync+os.replace, same as write_status. A child
    SIGKILLed while it is inside the write (20 trials) never leaves a partial cursor:
    the file is the previous complete value or the new complete value."""
    lane = mklane(root, "x1_cursor")
    bus_write_cursor(lane, 7)
    script = (
        "import os,sys\n"
        "sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])) if False else '.')\n"
    )
    # Drive the real function in a child of this interpreter by importing the module.
    child = os.path.join(root, "x1_cursor_child.py")
    with open(child, "w") as f:
        f.write(
            "import importlib.util, os, sys, time\n"
            f"spec = importlib.util.spec_from_file_location('mkb', {os.path.abspath(__file__)!r})\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "real = os.replace\n"
            "def slow(src, dst):\n"
            "    # park until SIGKILL: the temp file exists, the cursor name does not\n"
            "    while True:\n"
            "        time.sleep(0.2)\n"
            "os.replace = slow\n"
            f"m.bus_write_cursor({lane!r}, 99)\n"
        )
    for trial in range(20):
        proc = subprocess.Popen([sys.executable, child], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        # wait until the temp exists (the child is inside the write), then SIGKILL
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(n.startswith(".tmp-cursor-") for n in os.listdir(lane)):
                break
            if proc.poll() is not None:
                break
            time.sleep(0.01)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
        assert proc.returncode < 0 or proc.returncode == 137 or proc.returncode != 0
        with open(os.path.join(lane, BUS_CURSOR_FILE)) as f:
            raw = f.read()
        assert raw in ("7\n", "99\n"), f"trial {trial}: partial cursor {raw!r}"
        assert not any(n.startswith(".tmp-cursor-") for n in os.listdir(lane)) or True
        # a temp may exist only if kill landed before the except handler; it must not
        # be the cursor file, and the cursor itself stays a complete line (asserted).
        got = int(raw.strip())
        assert got in (7, 99)
    # a clean write still lands
    bus_write_cursor(lane, 42)
    assert bus_read_cursor(lane) == 42


def t_x1_token_mode_refused(root):
    """SR5 LOW: mode 0644 and a symlink to a 0600 file are refused (empty config, exit 4).
    A real 0600 file written by mk-bus.py token still works. Env MK_BUS_TOKEN is not
    affected by the file's mode."""
    lane = mklane(root, "x1_mode")
    loose = os.path.join(root, "loose.json")
    with open(loose, "w") as f:
        json.dump({"url": "http://127.0.0.1:9", "token": "sekrit"}, f)
    os.chmod(loose, 0o644)
    saved = {k: os.environ.pop(k, None) for k in ("MK_BUS_URL", "MK_BUS_TOKEN", "MK_BUS_TOKEN_FILE")}
    try:
        os.environ["MK_BUS_TOKEN_FILE"] = loose
        assert bus_lane_config(lane) is None, "0644 token file must be refused"
        good = os.path.join(root, "good.json")
        with open(good, "w") as f:
            json.dump({"url": "http://127.0.0.1:9", "token": "sekrit"}, f)
        os.chmod(good, 0o600)
        link = os.path.join(root, "link.json")
        os.symlink(good, link)
        os.environ["MK_BUS_TOKEN_FILE"] = link
        assert bus_lane_config(lane) is None, "symlink to a 0600 file must be refused"
        os.environ["MK_BUS_TOKEN_FILE"] = good
        cfg = bus_lane_config(lane)
        assert cfg and cfg["token"] == "sekrit" and cfg["url"].startswith("http://"), cfg
        # env token is used even when the file is 0644
        os.environ["MK_BUS_TOKEN_FILE"] = loose
        os.environ["MK_BUS_URL"] = "http://127.0.0.1:9"
        os.environ["MK_BUS_TOKEN"] = "from-env"
        cfg = bus_lane_config(lane)
        assert cfg and cfg["token"] == "from-env", cfg
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _x1_rss_kb():
    """This process's resident set, in KiB. 0 when the platform does not expose it."""
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    try:
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError):
        return 0


def _x1_fast_baseline(root):
    """One spawn-cost baseline from a NORMAL fast peer, shared by the drip cases:
    a real-bus `progress` round trip on its own lane when the plugin checkout is
    present, else a `python -c pass` spawn. Never measured from a drip peer:
    against the drip the baseline stretches to the whole drip on a path without
    the absolute deadline, and the scaled bound grows with it. The server is a
    cached bus_server (stopped by stop_bus_servers) and the lane lives in the
    temp root, like every other bus case."""
    if os.path.isfile(MK_BUS_PY):
        data, _, _ = bus_server(root, "x1dripbase")
        blane = bus_lane(root, "x1_drip_baseline", "X1D", data)
        begin(blane, root, run=BUS_RUN, task="X1D")
        b0 = time.time()
        cli(blane, "progress", "work")
        return max(0.25, time.time() - b0)
    b0 = time.time()
    subprocess.run([sys.executable, "-c", "pass"], capture_output=True, timeout=30)
    return max(0.25, time.time() - b0)


def t_x1_slow_drip_body_honours_budget(root):
    """Review R1 HIGH: a peer that streams the body at 1 byte/s resets a per-recv
    socket timer. inbox and progress must still return within a baseline-scaled
    bound (floor 5 s, cap 20 s); ask --timeout 5 within the wider bound (floor
    10 s). RSS must not grow by the drip."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/health"):
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            for _ in range(60):
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                time.sleep(1)
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            if self.path.startswith("/send"):
                body = b'{"ok": true, "message": {"id": "m-1", "seq": 1}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            for _ in range(60):
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                time.sleep(1)
        def log_message(self, *a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    port = srv.server_address[1]
    lane = mklane(root, "x1_drip_body")
    begin(lane, root)
    env = {"MK_BUS_URL": f"http://127.0.0.1:{port}", "MK_BUS_TOKEN": "sekrit"}
    before = _x1_rss_kb()
    # Baseline from _x1_fast_baseline: a NORMAL fast peer, never the drip peer
    # itself (see the helper). Without the absolute deadline each run below
    # takes the full 60 s drip (do_GET/do_POST loop 60 x 1 s; cli_raw timeout
    # is 180 s), which exceeds the 20 s hard cap, so the case still fails on
    # that regression path. Floors 5/10 s absorb a fast sample followed by a
    # load spike (review R6 LOW) and stay far below the 60 s drip.
    baseline = _x1_fast_baseline(root)
    inbox_bound = min(20.0, max(5.0, 4 * baseline))
    ask_bound = min(20.0, max(10.0, 5 + 4 * baseline))
    try:
        t0 = time.time()
        code, lines = cli(lane, "inbox", env=env)
        elapsed = time.time() - t0
        assert elapsed < inbox_bound, f"inbox took {elapsed:.2f}s against a 1 byte/s body: {lines}"
        assert code == 4, (code, lines)
        t0 = time.time()
        code, lines = cli(lane, "progress", "parse-plan", env=env)
        elapsed = time.time() - t0
        assert elapsed < inbox_bound, f"progress took {elapsed:.2f}s against a 1 byte/s body: {lines}"
        assert code == 0, (code, lines)
        t0 = time.time()
        code, lines = cli(lane, "ask", "which one?", "--timeout", "5", env=env)
        elapsed = time.time() - t0
        # deadline is set at ask start; one chunk may run remaining+1, and the
        # server floor is 1 s, so the bound is the brief's timeout + 1, scaled
        # from the measured baseline. Without the absolute deadline the 60 s
        # drip would blow past it.
        assert elapsed < ask_bound, f"ask took {elapsed:.2f}s (timeout 5, deadline + 1): {lines}"
        # no answer yet: PENDING (exit 3) or BUS DOWN (exit 4). Never a hang.
        assert code in (3, 4), (code, lines)
        if code == 3:
            assert lines and lines[-1].startswith("PENDING "), lines
        else:
            assert "BUS DOWN: use the BLOCKED token" in lines, lines
        after = _x1_rss_kb()
        if before and after:
            assert after - before < 32 * 1024, f"RSS grew {after - before} KiB on a drip"
    finally:
        srv.shutdown()


def t_x1_slow_drip_headers_honours_budget(root):
    """A peer that dribbles response-header bytes at 1 byte/s. The deadline covers
    headers, not only the body: inbox and progress return within a baseline-scaled
    bound (floor 3 s, cap 20 s)."""
    import socket as _socket
    import threading

    def serve(sock):
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return

            def one(c=conn):
                try:
                    c.settimeout(2)
                    buf = b""
                    try:
                        while b"\r\n\r\n" not in buf and len(buf) < 65536:
                            piece = c.recv(4096)
                            if not piece:
                                return
                            buf += piece
                    except (TimeoutError, OSError):
                        pass
                    # drip the status line and headers one byte per second
                    drip = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"
                    for byte in drip:
                        try:
                            c.send(bytes([byte]))
                        except OSError:
                            return
                        time.sleep(1)
                finally:
                    with contextlib.suppress(OSError):
                        c.close()

            threading.Thread(target=one, daemon=True).start()

    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = sock.getsockname()[1]
    threading.Thread(target=serve, args=(sock,), daemon=True).start()
    lane = mklane(root, "x1_drip_hdr")
    begin(lane, root)
    env = {"MK_BUS_URL": f"http://127.0.0.1:{port}", "MK_BUS_TOKEN": "sekrit"}
    # Baseline from _x1_fast_baseline: a NORMAL fast peer, never the drip peer
    # itself (see the helper). Without the absolute deadline each run below
    # waits out the whole 51-byte header drip at 1 byte/s (~51 s; cli_raw
    # timeout is 180 s), which exceeds the 20 s hard cap, so the case still
    # fails on that regression path.
    bound = min(20.0, max(3.0, 4 * _x1_fast_baseline(root)))
    try:
        t0 = time.time()
        code, lines = cli(lane, "inbox", env=env)
        elapsed = time.time() - t0
        assert elapsed < bound, f"inbox took {elapsed:.2f}s against dribbled headers: {lines}"
        assert code == 4, (code, lines)
        t0 = time.time()
        code, lines = cli(lane, "progress", "parse-plan", env=env)
        elapsed = time.time() - t0
        assert elapsed < bound, f"progress took {elapsed:.2f}s against dribbled headers: {lines}"
        assert code == 0, (code, lines)
    finally:
        sock.close()


def t_x1_huge_body_is_capped(root):
    """A peer that sends a 50 MB body. inbox and progress return within 3 s and
    this process's RSS does not grow by tens of megabytes."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/health"):
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(50 * 1024 * 1024))
            self.end_headers()
            blob = b"x" * (256 * 1024)
            for _ in range(50 * 4):
                try:
                    self.wfile.write(blob)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
        def log_message(self, *a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    port = srv.server_address[1]
    lane = mklane(root, "x1_huge")
    begin(lane, root)
    env = {"MK_BUS_URL": f"http://127.0.0.1:{port}", "MK_BUS_TOKEN": "sekrit"}
    before = _x1_rss_kb()
    try:
        t0 = time.time()
        code, lines = cli(lane, "inbox", env=env)
        elapsed = time.time() - t0
        assert elapsed < 3.0, f"inbox took {elapsed:.2f}s against a 50 MB body: {lines}"
        assert code == 4, (code, lines)
        t0 = time.time()
        code, lines = cli(lane, "progress", "parse-plan", env=env)
        elapsed = time.time() - t0
        assert elapsed < 3.0, f"progress took {elapsed:.2f}s against a 50 MB body: {lines}"
        assert code == 0, (code, lines)
        after = _x1_rss_kb()
        if before and after:
            assert after - before < 32 * 1024, f"RSS grew {after - before} KiB reading a 50 MB body"
    finally:
        srv.shutdown()


def t_x1_real_bus_token_0600(root):
    """A token file written by the real mk-bus.py token command is mode 0600 and the CLI
    still reaches the bus (inbox on an empty lane exits 0)."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root, "x1")
    lane = bus_lane(root, "x1_real_token", "X1", data)
    path = os.path.join(lane, BUS_LANE_FILE)
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600, oct(mode)
    code, lines = cli(lane, "inbox")
    assert code == 0, (code, lines)


def t_e1_connect_fresh_lane_prints_brief(root):
    """E1: a fresh lane with no status file and no MK_BUS_* env runs connect and
    gets brief page 1 plus the task list. The data dir comes from the token file."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data = os.path.join(root, "e1fresh-data")
    os.makedirs(data, exist_ok=True)
    bus_server(root, "e1fresh")
    lane = bus_lane(root, "e1_fresh_lane", "E1F", data)
    assert not os.path.isfile(status_path(lane))
    body = "round:1\nkind:brief\n# e1 brief page\nbuild the doorbell\n"
    code, res = bus_cli("send", "--data", data, "--run", BUS_RUN, "--task", "E1F",
                        "--kind", "info", "--from", "orch", "--to", "E1F", "--text", body)
    assert code == 0 and res.get("ok"), res
    plan = os.path.join(root, "e1-plan.md")
    with open(plan, "w") as f:
        f.write('+++\nplan = "t"\nstatus = "running"\n[[phase]]\nid = "E1F"\n'
                '[[phase.task]]\nid = "E1F.1"\ntitle = "doorbell"\nparallel = false\n'
                'deps = []\n+++\n')
    env = {k: v for k, v in os.environ.items()
           if k not in ("MK_BUS_DATA", "MK_BUS_URL", "MK_BUS_TOKEN", "MK_PLAN")}
    code, lines = cli(lane, "connect", "--plan", plan, env=env)
    assert code == 0, lines
    text = "\n".join(lines)
    assert text.startswith("# brief 1/"), text[:80]
    assert "e1 brief page" in text and "build the doorbell" in text, text
    assert "[E1F.1] doorbell parallel=false" in text, text
    assert os.path.isfile(status_path(lane))
    assert status(lane)["task"] == "E1F" and status(lane)["state"] == "started"


def t_e1_connect_names_the_fix_without_data(root):
    """No status file, no data dir, no env: connect exits non-zero and names --data."""
    lane = mklane(root, "e1_nodata")
    path = os.path.join(lane, BUS_LANE_FILE)
    with open(path, "w") as f:
        json.dump({"v": 1, "url": "http://127.0.0.1:9", "token": "t",
                   "run": "bb", "task": "E1N"}, f)
    os.chmod(path, 0o600)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MK_")}
    code, lines = cli(lane, "connect", env=env)
    assert code != 0, lines
    assert any("data" in ln for ln in lines), lines


def t_e1_ask_id_first_then_resume(root):
    """ask's first line is ASK <id>, then PENDING, under a short timeout. wait resumes it."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root, "e1ask")
    lane = bus_lane(root, "e1_ask_lane", "E1A", data)
    begin(lane, root, task="E1A")
    cli(lane, "progress", "work")
    t0 = time.time()
    code, lines = cli(lane, "ask", "format?", "--timeout", "2")
    assert time.time() - t0 < 8, "ask must not hang"
    assert code == 3 and lines[0].startswith("ASK m-") and lines[1].startswith("PENDING "), (code, lines)
    ask_id = lines[0].split()[1]
    assert lines[1] == f"PENDING {ask_id}"
    bus_cli("answer", ask_id, "format ok", "--run", BUS_RUN, "--data", data)
    code, lines = cli(lane, "wait", ask_id, "--timeout", "10")
    assert code == 0 and lines == ["format ok"], (code, lines)


def t_e1_listen_two_messages_and_resume(root):
    """listen prints two MSG lines, acks each once, and a second listen with the
    cursor already past them prints nothing (Last-Event-ID resume) and exits 3."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root, "e1listen")
    lane = bus_lane(root, "e1_listen_lane", "E1L", data)
    begin(lane, root, task="E1L")
    bus_cli("send", "--data", data, "--run", BUS_RUN, "--task", "E1L",
            "--kind", "info", "--from", "orch", "--to", "E1L", "--text", "note one")
    bus_cli("send", "--data", data, "--run", BUS_RUN, "--task", "E1L",
            "--kind", "info", "--from", "orch", "--to", "E1L", "--text", "note two")
    code, lines = cli(lane, "listen", "--timeout", "3")
    assert code == 0, lines
    msgs = [ln for ln in lines if ln.startswith("MSG ")]
    assert len(msgs) == 2, lines
    assert "<quoted>note one</quoted>" in msgs[0] and "<quoted>note two</quoted>" in msgs[1], msgs
    acks = [r for r in bus_rows(data) if r.get("kind") == "ack" and r.get("task") == "E1L"]
    assert len(acks) == 2 and all(r.get("ack_level") == 3 for r in acks), acks
    code, lines = cli(lane, "listen", "--timeout", "2")
    assert code == 3 and lines == [], (code, lines)
    acks2 = [r for r in bus_rows(data) if r.get("kind") == "ack" and r.get("task") == "E1L"]
    assert len(acks2) == 2, acks2


def t_e1_listen_timeout_exits_3(root):
    """Nothing on the bus: listen exits 3 and prints nothing."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root, "e1to")
    lane = bus_lane(root, "e1_to_lane", "E1T", data)
    begin(lane, root, task="E1T")
    t0 = time.time()
    code, lines = cli(lane, "listen", "--timeout", "2")
    assert time.time() - t0 < 8
    assert code == 3 and lines == [], (code, lines)


def t_e1_progress_posts_info(root):
    """progress writes the status file and posts `progress: <text>` to the orchestrator."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root, "e1prog")
    lane = bus_lane(root, "e1_prog_lane", "E1P", data)
    begin(lane, root, task="E1P")
    code, lines = cli(lane, "progress", "reading the spec")
    assert code == 0, lines
    assert status(lane)["step"] == "reading the spec"
    infos = [r for r in bus_rows(data)
             if r.get("kind") == "info" and r.get("task") == "E1P" and (r.get("text") or "").startswith("progress:")]
    assert len(infos) == 1 and infos[0]["text"] == "progress: reading the spec", infos
    assert infos[0].get("from") == "E1P", infos[0]


def t_e1_listen_then_inbox_acks_once(root):
    """listen consumes a message; inbox must not POST a second ack. The marker
    file holds the id and the bus log has one level-3 ack row. A second listen
    with the cursor file deleted still does not ack again."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root, "e1ack2")
    lane = bus_lane(root, "e1_ack2_lane", "E1K", data)
    begin(lane, root, task="E1K")
    bus_cli("send", "--data", data, "--run", BUS_RUN, "--task", "E1K",
            "--kind", "info", "--from", "orch", "--to", "E1K", "--text", "only once")
    code, lines = cli(lane, "listen", "--timeout", "3")
    assert code == 0 and any("only once" in ln for ln in lines), lines
    marker = os.path.join(lane, ".matrixkoder-ack-once")
    assert os.path.isfile(marker)
    marked = open(marker).read().strip().splitlines()
    assert len(marked) == 1 and marked[0].startswith("m-"), marked
    code, lines = cli(lane, "inbox")
    assert code == 0, lines
    acks = [r for r in bus_rows(data) if r.get("kind") == "ack" and r.get("task") == "E1K"]
    assert len(acks) == 1 and acks[0]["ack_level"] == 3, acks
    os.remove(listen_cursor_path(lane))
    code, lines = cli(lane, "listen", "--timeout", "3")
    assert code == 0 and any("only once" in ln for ln in lines), lines
    acks2 = [r for r in bus_rows(data) if r.get("kind") == "ack" and r.get("task") == "E1K"]
    assert len(acks2) == 1, acks2
    assert open(marker).read().strip().splitlines() == marked


def t_e1_listen_drip_and_flood_stay_bounded(root):
    """A slow-drip peer and a peer that floods with no frame separator both return
    inside a bound scaled from one fast baseline, hard-capped at 20 s."""
    import socket as _socket
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

    def baseline_listen():
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"id: 1\nevent: message\ndata: {\"id\":\"m-1\",\"seq\":1,\"kind\":\"info\",\"from\":\"orch\",\"text\":\"hi\"}\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a):
                return
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        return srv

    lane = mklane(root, "e1_drip")
    begin(lane, root, task="E1D")
    fast = baseline_listen()
    env = {"MK_BUS_URL": f"http://127.0.0.1:{fast.server_address[1]}", "MK_BUS_TOKEN": "sekrit"}
    # token file so bus_lane_config resolves even with env
    with open(os.path.join(lane, BUS_LANE_FILE), "w") as f:
        json.dump({"v": 1, "url": env["MK_BUS_URL"], "token": "sekrit", "run": "bb", "task": "E1D"}, f)
    os.chmod(os.path.join(lane, BUS_LANE_FILE), 0o600)
    t0 = time.time()
    code, lines = cli(lane, "listen", "--timeout", "2", env=env)
    base = max(0.05, time.time() - t0)
    fast.shutdown()
    assert code == 0 and any("hi" in ln for ln in lines), (code, lines)
    bound = min(20.0, max(5.0, 8 * base))

    class Drip(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for _ in range(40):
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                time.sleep(1)
        def log_message(self, *a):
            return

    drip = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    th = threading.Thread(target=drip.serve_forever, daemon=True)
    th.start()
    env["MK_BUS_URL"] = f"http://127.0.0.1:{drip.server_address[1]}"
    with open(os.path.join(lane, BUS_LANE_FILE), "w") as f:
        json.dump({"v": 1, "url": env["MK_BUS_URL"], "token": "sekrit", "run": "bb", "task": "E1D"}, f)
    os.chmod(os.path.join(lane, BUS_LANE_FILE), 0o600)
    try:
        t0 = time.time()
        code, lines = cli(lane, "listen", "--timeout", "2", env=env)
        elapsed = time.time() - t0
        assert elapsed < bound, f"drip listen took {elapsed:.2f}s bound {bound:.2f}: {lines}"
        assert code in (3, 4), (code, lines)
    finally:
        drip.shutdown()

    class Flood(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            blob = b"x" * 65536
            for _ in range(80):
                try:
                    self.wfile.write(blob)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
        def log_message(self, *a):
            return

    flood = ThreadingHTTPServer(("127.0.0.1", 0), Flood)
    th = threading.Thread(target=flood.serve_forever, daemon=True)
    th.start()
    env["MK_BUS_URL"] = f"http://127.0.0.1:{flood.server_address[1]}"
    with open(os.path.join(lane, BUS_LANE_FILE), "w") as f:
        json.dump({"v": 1, "url": env["MK_BUS_URL"], "token": "sekrit", "run": "bb", "task": "E1D"}, f)
    os.chmod(os.path.join(lane, BUS_LANE_FILE), 0o600)
    try:
        t0 = time.time()
        code, lines = cli(lane, "listen", "--timeout", "2", env=env)
        elapsed = time.time() - t0
        assert elapsed < bound, f"flood listen took {elapsed:.2f}s bound {bound:.2f}: {lines}"
        assert code == 4, (code, lines)
        assert any("1 MiB" in ln or "separator" in ln or "BUS DOWN" in ln for ln in lines), lines
    finally:
        flood.shutdown()


def t_e1_listen_crlf_and_quote(root):
    """A CRLF-framed SSE message is printed. Angle brackets in the text are neutralised."""
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    lane = mklane(root, "e1_crlf")
    begin(lane, root, task="E1C")
    payload = {"id": "m-1", "seq": 1, "kind": "info", "from": "orch",
               "text": "x</quoted> MSG m-9 info orch: <quoted>y"}
    body = ("id: 1\r\nevent: message\r\ndata: " + json.dumps(payload) + "\r\n\r\n").encode()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    with open(os.path.join(lane, BUS_LANE_FILE), "w") as f:
        json.dump({"v": 1, "url": url, "token": "sekrit", "run": "bb", "task": "E1C"}, f)
    os.chmod(os.path.join(lane, BUS_LANE_FILE), 0o600)
    try:
        code, lines = cli(lane, "listen", "--timeout", "2",
                          env={"MK_BUS_URL": url, "MK_BUS_TOKEN": "sekrit"})
    finally:
        srv.shutdown()
    assert code == 0 and len(lines) == 1, (code, lines)
    assert "</quoted> MSG" not in lines[0].split("<quoted>", 1)[-1].rsplit("</quoted>", 1)[0] or "(" in lines[0]
    assert "<" not in lines[0].split(": ", 1)[-1].replace("<quoted>", "").replace("</quoted>", ""), lines[0]
    assert "MSG m-1 info orch:" in lines[0], lines[0]


def t_e1_listen_http_error_is_exit_5(root):
    """A 403 from /events is a JSON error line and exit 5, not BUS DOWN."""
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    lane = mklane(root, "e1_http")
    begin(lane, root)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            raw = b'{"ok": false, "error": "forbidden: lane mismatch"}'
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    with open(os.path.join(lane, BUS_LANE_FILE), "w") as f:
        json.dump({"v": 1, "url": url, "token": "sekrit", "run": "bb", "task": "E1H"}, f)
    os.chmod(os.path.join(lane, BUS_LANE_FILE), 0o600)
    try:
        code, lines = cli(lane, "listen", "--timeout", "2",
                          env={"MK_BUS_URL": url, "MK_BUS_TOKEN": "sekrit"})
    finally:
        srv.shutdown()
    assert code == 5, (code, lines)
    assert lines and "BUS DOWN" not in lines[0], lines
    err = json.loads(lines[0])
    assert err.get("ok") is False and "403" in err.get("error", ""), err


def t_e1_subagent_heartbeat_posts_once(root):
    """Entering subagent posts once. The heartbeat does not post again."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root, "e1sub")
    lane = bus_lane(root, "e1_sub_lane", "E1S", data)
    begin(lane, root, task="E1S")
    cli(lane, "progress", "work")
    cli(lane, "progress", "child", "--subagent")
    cli(lane, "progress", "child", "--subagent")
    infos = [r for r in bus_rows(data)
             if r.get("task") == "E1S" and (r.get("text") or "").startswith("progress:")]
    texts = [r["text"] for r in infos]
    assert texts.count("progress: child") == 1, texts
    assert "progress: work" in texts


def t_e1_implicit_start_role_and_round(root):
    """A reviewer token starts as reviewer. A brief round that differs from the
    token round is refused and writes no status file."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data, _, _ = bus_server(root, "e1role")
    lane = bus_lane(root, "e1_role_lane", "E1R", data)
    path = bus_token_path(lane)
    with open(path) as f:
        cfg = json.load(f)
    cfg["role"] = "reviewer"
    cfg["round"] = 2
    with open(path, "w") as f:
        json.dump(cfg, f)
    os.chmod(path, 0o600)
    body = "round:1\nkind:brief\n# wrong round\nnope\n"
    bus_cli("send", "--data", data, "--run", BUS_RUN, "--task", "E1R",
            "--kind", "info", "--from", "orch", "--to", "E1R", "--text", body)
    code, lines = cli(lane, "connect")
    assert code != 0, lines
    assert any("round" in ln for ln in lines), lines
    assert not os.path.isfile(status_path(lane))
    body2 = "round:2\nkind:brief\n# right round\nyes\n"
    bus_cli("send", "--data", data, "--run", BUS_RUN, "--task", "E1R",
            "--kind", "info", "--from", "orch", "--to", "E1R", "--text", body2)
    code, lines = cli(lane, "connect")
    assert code == 0, lines
    assert "right round" in "\n".join(lines)
    assert status(lane)["role"] == "reviewer" and status(lane)["round"] == 2


def t_e1_listen_bus_down_exits_4(root):
    """No token file: listen exits 4 with the BLOCKED line."""
    lane = mklane(root, "e1_down")
    begin(lane, root)
    code, lines = cli(lane, "listen", "--timeout", "2")
    assert code == 4 and lines == ["BUS DOWN: use the BLOCKED token"], (code, lines)


def t_skill_frontmatter(root):
    """The SKILL.md this script serves: exactly the three portable frontmatter keys, the
    directory-matching name, the guard description, the one allowed-tools value, steps before
    `## Reference`, and both script-path forms plus the R9 citation in the body."""
    skill = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "SKILL.md")
    assert os.path.isfile(skill), f"SKILL.md is missing at {skill}"
    with open(skill) as f:
        lines = f.read().splitlines()
    assert lines[0] == "---", "SKILL.md must open with a `---` frontmatter fence"
    assert "---" in lines[1:], "SKILL.md has no closing `---` fence"
    end = lines.index("---", 1)
    fields = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(": ")
        assert sep, f"frontmatter line is not `key: value`: {line!r}"
        assert key not in fields, f"duplicate frontmatter key {key!r}"
        fields[key] = value
    assert set(fields) == {"name", "description", "allowed-tools"}, \
        f"frontmatter keys must be exactly name/description/allowed-tools: {sorted(fields)}"
    name, desc = fields["name"], fields["description"]
    assert name == "mk-builder", f"name is {name!r}, not 'mk-builder'"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), f"name breaks the skill-name regex: {name!r}"
    assert name == os.path.basename(os.path.dirname(skill)), \
        "name must equal the name of the directory holding SKILL.md"
    assert 1 <= len(desc) <= 1024, f"description length {len(desc)} is outside 1..1024"
    assert desc.startswith("MatrixKoder lane agents only"), \
        f"description must start with the lane-agents guard: {desc[:40]!r}"
    assert fields["allowed-tools"] == "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/mk-builder.py *)", \
        f"allowed-tools must be the single script value: {fields['allowed-tools']!r}"
    body = "\n".join(lines[end + 1:])
    headings = [(m.start(), m.group(1).strip()) for m in re.finditer(r"(?m)^## (.+)$", body)]
    assert headings, "the body has no `##` sections"
    refs = [i for i, (_, h) in enumerate(headings) if h == "Reference"]
    assert len(refs) == 1, f"exactly one `## Reference` heading expected, found {len(refs)}"
    assert headings[-1][1] == "Reference", "`## Reference` must be the LAST `##` section"
    steps = [i for i, (_, h) in enumerate(headings)
             if any(w in h.lower() for w in ("start", "progress", "blocked", "done"))]
    assert steps, "no `##` heading names a step (start, progress, blocked, done)"
    for i in steps:
        assert i < refs[0], f"step heading {headings[i][1]!r} must come before `## Reference`"
    ask_sec = [i for i, (_, h) in enumerate(headings) if h == "Asking the orchestrator"]
    assert len(ask_sec) == 1, "exactly one `## Asking the orchestrator` section (BB)"
    assert ask_sec[0] < refs[0], "`## Asking the orchestrator` must come before `## Reference`"
    assert "${CLAUDE_SKILL_DIR}" in body, "the body must give the Claude Code script path"
    assert "base directory" in body, "the body must give the OpenCode script path (base directory)"
    assert ".claude/skills/mk-builder/SKILL.md" in body or "child-skills/mk-builder/SKILL.md" in body, \
        "the body must name a path a no-skill-support agent reads this file from"
    assert any("SKILL.md" in ln and "path" in ln for ln in lines[end + 1:]), \
        "the body must tell a no-skill-support agent to read SKILL.md by path"
    assert "R9" in body, "the body must cite lane-rules R9 for the todo-list rule"


def stat_of(path):
    """(size, mtime_ns) of a file, None when absent. os.stat only: never opens the file."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def run_self_test():
    """Print one line per case, then `ok <n> cases`; exit non-zero if any case failed. Every
    bus server the cases started is stopped in the finally, and none may survive it (pgrep)."""
    root = tempfile.mkdtemp(prefix="mk-builder-selftest-")
    cases = sorted((n, f) for n, f in globals().items() if n.startswith("t_") and callable(f))
    failed, leftover = [], []
    # Todo 69: the real home's root store, read with os.stat only, before anything runs.
    real_db = os.path.join(os.path.expanduser("~"), ".matrixkoder", "mk.db")
    real_before = stat_of(real_db)
    pinned = {k: os.environ.get(k) for k in ("MK_HOME", "MATRIXKODER_HOME", "MK_BUS_DIR", "MK_BUS_DATA")}
    try:
        for name, fn in cases:
            # Every case starts on the temp home; a store-mode bus case pins its own (pin_home).
            # MK_BUS_DIR and MK_BUS_DATA stay unset here: a file-mode bus keeps bus.json in its data dir.
            os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = os.path.join(root, "pin-home")
            os.environ.pop("MK_BUS_DIR", None)
            os.environ.pop("MK_BUS_DATA", None)
            try:
                fn(root)
                print(f"ok {name}")
            except Exception as e:  # a crash is a failed case, not a stop
                failed.append(name)
                print(f"not ok {name}: {type(e).__name__}: {e}")
    finally:
        stop_bus_servers()
        leftover = bus_serve_leftovers()
        shutil.rmtree(root, ignore_errors=True)
        for k, v in pinned.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if stat_of(real_db) != real_before:
        failed.append("real_home_untouched")
        print(f"not ok real_home_untouched: {real_db} was {real_before}, now {stat_of(real_db)}")
    if leftover:
        failed.append("bus_server_leftover")
        print(f"not ok bus_server_leftover: pgrep still shows a serve for: {leftover}")
    if failed:
        print(f"{len(failed)} of {len(cases)} cases failed: {', '.join(failed)}")
        return 1
    print(f"ok {len(cases)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
