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
"""
import argparse
import contextlib
import datetime
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
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
ASK_ID_RE = re.compile(r"^m-(\d+)$")  # B15 Table 1: ids are `m-<seq>`

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


def cmd_done(a):
    """R18: the report file at the lane root AND a readable HEAD; R20: final per identity, so the
    legality table refuses every second write. The report is checked first, so the error is
    deterministic."""
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
    return ({"ok": True, "seq": obj["seq"], "state": obj["state"], "commit": sha},
            token_for(obj, "DONE"))


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


class BusHttp(Exception):
    """The bus answered with an HTTP error status (`code`, server `error` text)."""

    def __init__(self, code, error):
        super().__init__(f"{code}: {error}")
        self.code, self.error = code, error


def bus_http_error(e):
    """The brief's client error shape: `{"ok": false, "error": "<status>: <server error>"}`."""
    return f"{e.code}: {e.error}"


def bus_lane_config(lane):
    """B24: `<lane>/.matrixkoder-bus.json` `{v, url, token, run, task}`, written by
    `mk-bus.py token`. None when absent or unusable — the caller's down-fallback."""
    try:
        with open(os.path.join(lane, BUS_LANE_FILE)) as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or not obj.get("url") or not obj.get("token"):
        return None
    return obj


def bus_read_cursor(lane):
    """The last seq this lane has seen (`<lane>/.matrixkoder-bus-cursor`, lane-only). A missing
    or corrupt cursor reads as 0 — at-least-once, never a crash."""
    try:
        with open(os.path.join(lane, BUS_CURSOR_FILE)) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def bus_write_cursor(lane, seq):
    with open(os.path.join(lane, BUS_CURSOR_FILE), "w") as f:
        f.write(f"{int(seq)}\n")


def bus_call(cfg, method, path, body=None, timeout=BUS_CONNECT_TIMEOUT):
    """One HTTP call to the bus (B4-B8: loopback, Bearer token, strict Host). BusDown on any
    connection failure, BusHttp on any HTTP error status — never a traceback."""
    base = (cfg.get("url") or "").rstrip("/")
    parsed = urllib.parse.urlparse(base)
    host = parsed.hostname or "127.0.0.1"
    headers = {"Authorization": f"Bearer {cfg['token']}",
               "Host": f"{host}:{parsed.port}" if parsed.port else host}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error = e.reason
        with contextlib.suppress(OSError, ValueError):
            error = json.loads(e.read().decode("utf-8")).get("error") or e.reason
        raise BusHttp(e.code, error) from None
    except (urllib.error.URLError, TimeoutError, ConnectionRefusedError, OSError, ValueError) as e:
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


def bus_wait_chunk(cfg, after, chunk):
    """One B11 long-poll chunk: the `/health` probe carries the 2 s connect budget (a refused
    loopback connect fails instantly; a hung server surfaces here, not after the chunk), then
    `GET /wait` with a read window that spans the chunk."""
    bus_call(cfg, "GET", "/health")
    qs = urllib.parse.urlencode({"after": int(after), "timeout": f"{chunk:g}"})
    return bus_call(cfg, "GET", f"/wait?{qs}", timeout=chunk + 10.0)


def bus_poll_answer(cfg, ask_id, after, deadline):
    """Long-poll `GET /wait` in chunks of at most 90 s until the `answer` whose `reply_to` is
    this ask arrives, skipping other messages addressed to the lane, or until `deadline`.
    Returns the answer message or None; raises BusDown/BusHttp to the caller."""
    while True:
        remaining = deadline - time.time()
        if remaining < 1.0:  # /wait clamps to >= 1 s server-side; a shorter chunk would overshoot
            return None
        res = bus_wait_chunk(cfg, after, min(BUS_WAIT_CHUNK_MAX, remaining))
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
    status_resume(lane)
    print(answer["text"])
    raise Handled(0)


def cmd_inbox(a):
    """BB: print the messages addressed to this lane after the cursor, one JSON line each,
    ack each at level 3, advance the cursor. Empty prints nothing and exits 0."""
    lane = lane_dir(a)
    cfg = bus_lane_config(lane)
    if cfg is None:
        raise Fail("bus down", code=4)
    after = bus_read_cursor(lane)
    while True:
        try:
            res = bus_wait_chunk(cfg, after, 1.0)
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
    past the inbox cursor. Best effort — a 2 s budget, silent whenever the bus is unusable,
    never a failure, never a cursor move."""
    cfg = bus_lane_config(lane)
    if cfg is None:
        return None
    try:
        after, n = bus_read_cursor(lane), 0
        while True:
            res = bus_wait_chunk(cfg, after, 1.0)
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
                       "ask, wait, inbox, ack, install, measure)")
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
        assert res.get("started") is True and res.get("pid") and res.get("port"), res
        BUS_SERVERS.append((res["pid"], data))
        BUS_SERVER_CACHE[key] = (data, res["pid"], res["port"])
    return BUS_SERVER_CACHE[key]


def bus_lane(root, lane_name, task, data):
    """A lane dir whose `.matrixkoder-bus.json` is written by the real `mk-bus.py token`."""
    lane = mklane(root, lane_name)
    bus_cli("token", "--lane", lane, "--task", task, "--run", BUS_RUN, "--data", data)
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
