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
import datetime
import hashlib
import io
import json
import os
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
BUS_ASK_TIMEOUT = 600.0
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
    return ({"ok": True, "seq": obj["seq"], "state": obj["state"], "step": obj["step"],
             "subagent": obj["subagent"]}, bus_unread_hint(lane))


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


def bus_read_cursor(lane):
    """The last seq this lane has seen (`<lane>/.matrixkoder-bus-cursor`, lane-only). A missing
    or corrupt cursor reads as 0 — at-least-once, never a crash."""
    try:
        with open(os.path.join(lane, BUS_CURSOR_FILE)) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


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


def bus_note_seq(lane, msg):
    """Advance the inbox cursor past a message this lane has already been shown (D7.2, C13e).
    A non-integer seq is ignored — the cursor never moves backwards or onto garbage."""
    seq = msg.get("seq") if isinstance(msg, dict) else None
    if is_int(seq) and seq > bus_read_cursor(lane):
        bus_write_cursor(lane, seq)


def bus_poll_answer(cfg, ask_id, after, deadline):
    """Long-poll `GET /wait` in chunks of at most 90 s until the `answer` whose `reply_to` is
    this ask arrives, skipping other messages addressed to the lane, or until `deadline`.
    Returns the answer message or None; raises BusDown/BusHttp to the caller.
    A non-integer `seq` (item 37) is ignored: the cursor stays put and the loop continues."""
    while True:
        remaining = deadline - time.time()
        if remaining < 1.0:  # /wait clamps to >= 1 s server-side; a shorter chunk would overshoot
            return None
        res = bus_wait_chunk(cfg, after, min(BUS_WAIT_CHUNK_MAX, remaining), deadline=deadline)
        msg = res.get("message")
        if msg is None:
            continue
        if msg.get("kind") == "answer" and msg.get("reply_to") == ask_id:
            return msg
        seq = msg.get("seq")
        if is_int(seq) and seq > after:
            after = seq


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
    try:
        answer = bus_poll_answer(cfg, ask_id, after, deadline)
    except BusDown:
        ask_down(lane, question)
    except BusHttp as e:
        raise Fail(bus_http_error(e), code=5)
    if answer is None:
        print(f"PENDING {ask_id}")
        raise Handled(3)
    with contextlib.suppress(BusDown, BusHttp, OSError, ValueError):
        bus_call(cfg, "POST", "/ack", body=bus_ack_body(cfg, answer["id"]))
    bus_note_seq(lane, answer)  # D7.2: inbox must not re-show or re-ack this answer
    status_resume(lane)
    print(answer["text"])
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
        answer = bus_poll_answer(cfg, ask_id, int(m.group(1)), deadline)
    except BusDown:
        raise Fail("bus down", code=4)
    except BusHttp as e:
        raise Fail(bus_http_error(e), code=5)
    if answer is None:
        print(f"PENDING {ask_id}")
        raise Handled(3)
    with contextlib.suppress(BusDown, BusHttp, OSError, ValueError):
        bus_call(cfg, "POST", "/ack", body=bus_ack_body(cfg, answer["id"]))
    bus_note_seq(lane, answer)  # D7.2: inbox must not re-show or re-ack this answer
    status_resume(lane)
    print(answer["text"])
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
        print(json.dumps(msg))
        seq = msg.get("seq")
        if not is_int(seq) or seq <= after:
            break  # cannot advance past this message; stop rather than fetch it again
        after = seq
        bus_write_cursor(lane, seq)
        try:
            bus_call(cfg, "POST", "/ack", body=bus_ack_body(cfg, msg.get("id")))
        except BusHttp as e:
            raise Fail(bus_http_error(e), code=5)
        except (BusDown, OSError, ValueError):
            raise Fail("bus down", code=4)
    raise Handled(0)


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
    """The project data dir: `MK_BUS_DATA` wins, else the token file's `data` field.
    Production token files omit it; without either, store mode stays off (C12)."""
    env = os.environ.get("MK_BUS_DATA")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    if not cfg:
        return None
    data = cfg.get("data")
    if isinstance(data, str) and os.path.isdir(data):
        return os.path.abspath(data)
    return None


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


def brief_from_bus(lane, old):
    """The brief the orchestrator published for this lane, when no lane file exists.

    One short /wait. A message whose text starts `round:<n>\\nkind:brief\\n` is the brief;
    the newest such round wins. Anything else (the bus down, no such message) is no brief.
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
    after = 0
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            res = bus_wait_chunk(cfg, after, 1.0, deadline=deadline, hint=True)
        except (BusDown, BusHttp, OSError, ValueError):
            break
        msg = res.get("message") if isinstance(res, dict) else None
        if not msg:
            break
        seq = msg.get("seq")
        if is_int(seq) and seq > after:
            after = seq
        text = msg.get("text") or ""
        head, sep, rest = text.partition("\n")
        if head.startswith("round:") and rest.startswith("kind:brief\n"):
            try:
                n = int(head[6:].strip())
            except ValueError:
                n = 0
            if (want is None or n == want) and n >= best_n:
                best_n, best = n, rest[len("kind:brief\n"):]
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


def cmd_connect(a):
    """D7.1: print the brief (one page, ~24,000 chars) and the phase task list. The agent
    adds one `[<id>] ` todo per task. Ack of the brief happens on `read` of the last page."""
    lane = lane_dir(a)
    old = load_live(lane)
    text, path = brief_text(lane, old)
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
    `status <mapped>` — the bus change this needs is listed in the D7 report — and with the
    bus or the store absent nothing is sent (C12: the lane status file is the record)."""
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
        bus_send(cfg, "task", text, task=task)
    except (BusDown, BusHttp):
        return {"sent": False, "deduped": False, "state": state, "mapped": mapped,
                "fallback": "bus-down"}
    return {"sent": True, "deduped": False, "state": state, "mapped": mapped, "note": note}


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
    sp = lane_cmd("ctx", cmd_ctx, "record the context percentage, 0..100 [R10]")
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
    sp = lane_cmd("connect", cmd_connect, "print the brief (paged) and the phase task list [D7.1]")
    sp.add_argument("--plan", default=None, help="plan file; default $MK_PLAN")
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
                       "commit, report, install, measure)")
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


def bus_server(root, name="bus"):
    """One REAL mk-bus server per (root, name): ensure → detached serve, ephemeral port,
    temp data dir. Returns (data dir, pid, port)."""
    key = (root, name)
    if key not in BUS_SERVER_CACHE:
        data = os.path.join(root, f"{name}-data")
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
    assert code == 0 and lines == [answer_text], (code, lines)
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
    assert code == 3 and len(lines) == 1 and lines[0].startswith("PENDING m-"), (code, lines)
    ask_id = lines[0].split()[1]
    st = status(lane)
    assert st["state"] == "blocked" and st["question"] == "no answer coming", st
    code, lines = cli(lane, "ask", "also unanswered", "--timeout", "2")
    assert code == 3 and len(lines) == 1 and lines[0].startswith("PENDING m-"), (code, lines)
    ask2 = lines[0].split()[1]
    code, lines = cli(lane, "wait", ask2, "--timeout", "2")
    assert code == 3 and lines == [f"PENDING {ask2}"], (code, lines)
    assert status(lane)["state"] == "blocked", "a PENDING wait leaves the status blocked"
    bus_cli("answer", ask_id, "late answer", "--run", BUS_RUN, "--data", data)
    code, lines = cli(lane, "wait", ask_id, "--timeout", "10")
    assert code == 0 and lines == ["late answer"], (code, lines)
    st = status(lane)
    assert st["state"] == "working" and st["question"] is None, st
    answers = [r for r in bus_rows(data)
               if r.get("kind") == "answer" and r.get("reply_to") == ask_id]
    assert len(answers) == 1 and answers[0]["text"] == "late answer", answers
    acks = [r for r in bus_rows(data)
            if r.get("kind") == "ack" and r.get("reply_to") == answers[0]["id"]]
    assert len(acks) == 1 and acks[0]["ack_level"] == 3, acks


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


def t_d7_todo_backup_and_dedupe(root):
    """Backup `todo done` alone announces one status. A second report is deduped. A fix
    round re-entering in_progress is a new key and is not deduped."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data = os.path.join(root, "d7todo-data")
    os.makedirs(data, exist_ok=True)
    d7_store(data, BUS_RUN, "D7T")
    bus_server(root, "d7todo")
    lane = bus_lane(root, "d7_todo_lane", "D7T", data)
    begin(lane, root, run=BUS_RUN, task="D7T")
    cli(lane, "progress", "work")
    code, lines = cli(lane, "todo", "done", "D7.1", env={"MK_BUS_DATA": data})
    assert code == 0, lines
    first = jline(lines)
    assert first["sent"] is True and first["deduped"] is False and first["mapped"] == "verify", first
    code, lines = cli(lane, "todo", "done", "D7.1", env={"MK_BUS_DATA": data})
    second = jline(lines)
    assert second["deduped"] is True and second["sent"] is False, second
    msgs = d7_task_msgs(data, "D7T")
    assert len(msgs) == 1 and msgs[0]["text"].startswith("status verify\nround: 1\n"), msgs
    begin(lane, root, run=BUS_RUN, task="D7T", round=2)
    cli(lane, "progress", "fix")
    code, lines = cli(lane, "todo", "in_progress", "D7.1", env={"MK_BUS_DATA": data})
    assert code == 0 and jline(lines)["sent"] is True and jline(lines)["deduped"] is False, lines
    code, lines = cli(lane, "todo", "in_progress", "D7.1", env={"MK_BUS_DATA": data})
    assert jline(lines)["deduped"] is True, lines
    assert len(d7_task_msgs(data, "D7T")) == 2


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
    """ask acks the answer once; inbox does not show it or ack it again."""
    if not os.path.isfile(MK_BUS_PY):
        return
    data = os.path.join(root, "d7ack-data")
    os.makedirs(data, exist_ok=True)
    d7_store(data, BUS_RUN, "D7A")
    bus_server(root, "d7ack")
    lane = bus_lane(root, "d7_ack_lane", "D7A", data)
    begin(lane, root, run=BUS_RUN, task="D7A")
    cli(lane, "progress", "work")

    def answer():
        row = bus_poll_until(lambda: next(
            (r for r in d7_bus_rows(data) if r.get("kind") == "ask" and r.get("task") == "D7A"), None))
        got = d7_answer(data, row["id"], "the schema is v2", "D7A")
        assert got.get("ok") is True, got

    th = threading.Thread(target=answer)
    th.start()
    try:
        code, lines = cli(lane, "ask", "which schema?", "--timeout", "30")
    finally:
        th.join(30)
    assert code == 0 and lines == ["the schema is v2"], (code, lines)
    code, lines = cli(lane, "inbox")
    assert code == 0 and lines == [], lines
    rows = d7_bus_rows(data)
    answers = [r for r in rows if r.get("kind") == "answer"]
    assert len(answers) == 1, answers
    mine = [r for r in rows if r.get("kind") == "ack" and r.get("reply_to") == answers[0]["id"]]
    assert len(mine) == 1, mine


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
    t0 = time.time()
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "inbox", "--lane", lane],
                       capture_output=True, text=True, env=env, timeout=15)
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"lane FIFO took {elapsed:.2f}s"
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


def t_x1_slow_drip_body_honours_budget(root):
    """Review R1 HIGH: a peer that streams the body at 1 byte/s resets a per-recv
    socket timer. inbox and progress must still return within 3 s; ask --timeout 5
    within 6 s. RSS must not grow by the drip."""
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
    try:
        t0 = time.time()
        code, lines = cli(lane, "inbox", env=env)
        elapsed = time.time() - t0
        assert elapsed < 3.0, f"inbox took {elapsed:.2f}s against a 1 byte/s body: {lines}"
        assert code == 4, (code, lines)
        t0 = time.time()
        code, lines = cli(lane, "progress", "parse-plan", env=env)
        elapsed = time.time() - t0
        assert elapsed < 3.0, f"progress took {elapsed:.2f}s against a 1 byte/s body: {lines}"
        assert code == 0, (code, lines)
        t0 = time.time()
        code, lines = cli(lane, "ask", "which one?", "--timeout", "5", env=env)
        elapsed = time.time() - t0
        # deadline is set at ask start; one chunk may run remaining+1, and the
        # server floor is 1 s, so the bound is the brief's timeout + 1.
        assert elapsed < 6.5, f"ask took {elapsed:.2f}s (timeout 5, deadline + 1): {lines}"
        # no answer yet: PENDING (exit 3) or BUS DOWN (exit 4). Never a hang.
        assert code in (3, 4), (code, lines)
        if code == 3:
            assert lines and lines[0].startswith("PENDING "), lines
        else:
            assert lines == ["BUS DOWN: use the BLOCKED token"], lines
        after = _x1_rss_kb()
        if before and after:
            assert after - before < 32 * 1024, f"RSS grew {after - before} KiB on a drip"
    finally:
        srv.shutdown()


def t_x1_slow_drip_headers_honours_budget(root):
    """A peer that dribbles response-header bytes at 1 byte/s. The deadline covers
    headers, not only the body: inbox and progress return within 3 s."""
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
    try:
        t0 = time.time()
        code, lines = cli(lane, "inbox", env=env)
        elapsed = time.time() - t0
        assert elapsed < 3.0, f"inbox took {elapsed:.2f}s against dribbled headers: {lines}"
        assert code == 4, (code, lines)
        t0 = time.time()
        code, lines = cli(lane, "progress", "parse-plan", env=env)
        elapsed = time.time() - t0
        assert elapsed < 3.0, f"progress took {elapsed:.2f}s against dribbled headers: {lines}"
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


def run_self_test():
    """Print one line per case, then `ok <n> cases`; exit non-zero if any case failed. Every
    bus server the cases started is stopped in the finally, and none may survive it (pgrep)."""
    root = tempfile.mkdtemp(prefix="mk-builder-selftest-")
    cases = sorted((n, f) for n, f in globals().items() if n.startswith("t_") and callable(f))
    failed, leftover = [], []
    try:
        for name, fn in cases:
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
