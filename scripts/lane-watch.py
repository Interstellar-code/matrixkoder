#!/usr/bin/env python3
"""Watch one lane until it finishes, fails, or the watch window ends — never silently.

Usage:
  lane-watch.py --lane DIR --agent NAME|PANE --commit-grep REGEX [--report PATH] [--phase P1]
                [--kind claude|codex|opencode|agy] [--prompt-marker TEXT]
                 [--then-send BRIEF_PATH] [--max-wait-min N] [--poll 20] [--stall-min N]
                 [--task T | --tasks A,B --data DIR [--run ID]] [--no-commit]
  lane-watch.py --self-test

Done = latest commit subject matches REGEX (searched in the bare subject, so `^feat(x)`-style anchors work; the
       older "<sha> <subject>" form, e.g. `^[0-9a-f]+ feat`, still matches) AND (report exists, if given) AND agent not working
       AND (with --phase) a "<PHASE> DONE" line in the pane since dispatch. Done wins over a stray
       limit / dev-server string in the same pane.
With --no-commit (a reviewer pane: nothing is committed, so there is no commit to match) the commit half
       drops out everywhere: --commit-grep is not required (a given one is ignored; without --no-commit it
       is), Done = (report exists, if given) AND agent not working AND the token, the no-token grace is
       report + agent idle/done, and STALL never lists a matching commit as missing.
"<PHASE> BLOCKED: <question>" in the pane → BLOCKED with the question (the later of DONE / BLOCKED wins).
A herdr ERROR is retried; the watcher exits ERROR after 3 in a row, or on any crash (still one JSON line).
Idle/done/working agent, not Done, and no file/commit/pane change for --stall-min since watch start → STALL
(lane-watch decides STALL itself; lane-health's file-only STALL is not used here).
Pane output that changed since the last poll (digits ignored, so a ticking timer is not activity — the
`↳ N toolcalls` counter is exempt: a changed count is activity) counts as activity too, and so does the
mere presence of a running subagent task line (a braille spinner + "Task —"): a builder running tests,
a revert/re-apply proof or a lead waiting on its subagents is not STALL. Finished "✓ … Task —" lines
alone are not activity — with only those left, the stall clock runs again.
The lane's .matrixkoder-status.json (written by the mk-builder skill's mk-builder.py) is the FIRST
channel: read_status() accepts it only under status-contract.md R27 (v <= 1; run and task equal the
armed watch's; the round and the reviewer role parsed from the --phase form "<T>[ REVIEW] R<n>";
updated_at >= the watch's armed_at; seq strictly monotonic per process and identity), and any miss —
absent, corrupt (never an exception, R29), stale or wrong identity — falls back to today's pane-text
path unchanged (R28). state=done IS the DONE token (R30: the commit and report checks stay; it overrides
only the pane-status and herdr-ERROR halves of the DONE branch) and state=blocked is BLOCKED with its
question (R32). A fresh working/subagent heartbeat (updated_at inside --stall-min) resets the idle
clock (R33; a subagent run is not a STALL — but only within --max-wait-min of the watch's armed_at,
R34). QUOTA / PROVIDER / DEV_SERVER stay pane-text detections (R35: an agent at a quota wall cannot
write a file). Every verdict's evidence ends with its source tag (R41): source=status (decided from
the accepted file), source=pane (the pane-text path, QUOTA/STALL/ERROR/WATCH_EXPIRED included),
source=grace (the no-token DONE rule).
With --then-send, on DONE it dispatches "Read and follow <BRIEF_PATH> exactly ..." to the same agent
via the herdr skill's herdr-ask.py (which refuses busy agents, so we only send once idle).
With --task and --data (both or neither), every exit writes the task status via mk-state (DONE→verifying,
QUOTA→quota, STALL→stalled, PROVIDER→provider, BLOCKED→blocked) when the target is still in a watched
status (dispatched building stalled quota provider blocked); otherwise, and for other exits, it only logs
an event. A parent task is never written — mk-state refuses status= on it ("status rolls up from
children") — every child gets the verdict instead, and each mk-state reply is checked: an ok:false error
always lands in this script's own JSON output, never dropped.
--tasks A,B is the shared-lane-group spelling of --task (one comma list, one pane): the verdict
reaches every listed task's children (or the task itself when it has none), exactly as --task does
for one. The two spellings are alternatives — pass one, with --data.
Exit states (one JSON line): DONE | DONE_SENT | QUOTA | PROVIDER | STALL | BLOCKED | DEV_SERVER | ERROR | WATCH_EXPIRED.
Run in the background (max_wait_min; foreground calls must stay under their 10-min timeout); re-arm on WATCH_EXPIRED.
--max-wait-min / --stall-min absent: <data>/matrixkoder.yaml session.max_wait_min / session.stall_min,
else the mk-config schema default.
"""
import importlib.util, json, math, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ASK = os.path.expanduser("~/.claude/skills/herdr/scripts/herdr-ask.py")
MK_STATE = os.path.join(HERE, "mk-state.py")
STATUS_MAP = {"DONE": "verifying", "DONE_SENT": "verifying", "QUOTA": "quota", "STALL": "stalled",
              "PROVIDER": "provider", "BLOCKED": "blocked"}
FAIL_STATES = ("QUOTA", "PROVIDER", "BLOCKED", "DEV_SERVER", "ERROR")
WATCHED = ("dispatched", "building", "stalled", "quota", "provider", "blocked")  # a watcher may move these only
ERROR_RETRIES = 3
TOKEN_GRACE_MIN = 3  # commit + report + agent idle this long (report + idle with --no-commit), but no DONE token: DONE anyway (lesson 39)
STATUS_FILE = ".matrixkoder-status.json"   # R1: <lane>/.matrixkoder-status.json, written by mk-builder.py (MB2)
STATUS_STATES = ("started", "working", "subagent", "done", "blocked")   # R12
PHASE_ROUND_RE = re.compile(r"^(\S+)( REVIEW)? R(\d+)$")   # R27.5 round + R27.6 reviewer marker
_seen_seq = {}   # R27.9: (lane, run, task, round, role) -> highest seq THIS process accepted

sys.dont_write_bytecode = True  # no stray __pycache__ next to the scripts
STORE_PROJECT = "matrixkoder"  # slug only; the db is <data>/mk.db via data_dir (mk-state's rule)
# D5A.3: a pane that is quiet is still working while a descendant runs one of these. The window
# after the child exits is stall_min * max(1, loadavg_1m / cpu_count): a busy machine gets a
# longer grace, an idle machine keeps today's stall_min. cpu_count 0 is treated as 1.
# A load spike must not stretch one stall_min without limit, and a leaked matching child
# must not refresh the grace forever (SR2). Factor and the total window share this cap.
CHILD_SCALE_CAP = 4.0
CHILD_PROGRESS_RE = re.compile(
    r"(?:^|/)(?:selftest\.sh|pytest|tsc)(?:\s|$)"
    r"|claude\s+plugin\s+test"
    r"|python3?\b.*--self-test",
    re.I)
_spec = importlib.util.spec_from_file_location("lane_health", os.path.join(HERE, "lane-health.py"))
lh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lh)
_cspec = importlib.util.spec_from_file_location("mk_config", os.path.join(HERE, "mk-config.py"))
mk_config = importlib.util.module_from_spec(_cspec)
_cspec.loader.exec_module(mk_config)


def cfg(key, flag=None):
    """-> flag > <data>/matrixkoder.yaml > the mk-config schema default. One default, one home:
    a repo that never set the key gets the schema value, not a second literal here."""
    if flag is not None:
        return flag
    try:
        lines = mk_config.read_lines(os.path.join(
            mk_config.mk_state.resolve_data(None), "matrixkoder.yaml"))
    except Exception:  # outside a repo / unreadable data dir: the schema default, never a crash
        lines = []
    return mk_config.value_of(lines, mk_config.row_for(lines, key))[1]


def opt(a, f, d=None):
    if f not in a:
        return d
    if a.index(f) + 1 >= len(a):
        sys.exit(f"{f} needs a value\n{__doc__}")
    return a[a.index(f) + 1]


def commit_subject(lane):
    return subprocess.run(["git", "-C", lane, "log", "-1", "--format=%h %s"], capture_output=True, text=True).stdout.strip()


def phase_tokens(text, phase):
    """-> (done, blocked_question); only the later of the two counts. Tokens must start a line (bullets
    allowed), so prose that merely mentions "reply with P1 DONE" does not count, and lines with a "<"
    placeholder (an echoed brief: "P1 BLOCKED: <question>") are ignored."""
    if not phase:
        return False, None
    p = re.escape(phase)
    blocked = list(re.finditer(rf"(?m)^\W*{p} BLOCKED:\s*([^<\n]+)$", text))
    done = list(re.finditer(rf"(?m)^\W*{p} DONE\b[^<\n]*$", text))
    if blocked and (not done or blocked[-1].start() > done[-1].start()):
        return False, blocked[-1].group(1).strip()
    return bool(done), None


TOOLCALLS_RE = re.compile(r"(↳\s*)(\d+)(\s*toolcalls)")  # the ↳ N toolcalls subagent counter, kept below
COUNT_AS_LETTERS = str.maketrans("0123456789", "abcdefghij")  # digits that survive the drop
SPINNERS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille glyphs a busy CLI spins through
RUNNING_TASK_RE = re.compile(rf"(?m)^[{SPINNERS}].*Task —")  # a running subagent task line (em dash)


def pane_fingerprint(text):
    """Pane text with digits dropped: spinners and elapsed-time counters change digits only. The
    `↳ N toolcalls` counter is exempt (its digits become letters): a changed count between polls is
    subagent activity, the `12m 48s` timer it sits next to still is not.
    ponytail: a redrawn spinner glyph already moves this; a spinner line whose glyph happens not to
    change between two polls is caught by running_task() instead (presence, not change)."""
    kept = TOOLCALLS_RE.sub(lambda m: m.group(1) + m.group(2).translate(COUNT_AS_LETTERS) + m.group(3), text)
    return re.sub(r"\d+", "", kept)


def running_task(text):
    """True while a running subagent task line is on screen — a braille spinner (⠋⠙⠹…) then
    "Task —": the subagent is actively working, so the line's presence alone is activity even when
    the glyph does not change between two polls. Finished "✓ … Task —" lines (no spinner) never count."""
    return bool(RUNNING_TASK_RE.search(text))


def store_on(data):
    """True only when the project store already exists. Never creates mk.db (self-hosting guard)."""
    return bool(data) and os.path.exists(os.path.join(data, "mk.db"))


def store_con(data):
    """The project store, opened only through mkdb.open_project. Caller closes it."""
    db = _load_mkdb()
    home = os.path.join(data, ".mk-home")
    os.makedirs(home, exist_ok=True)
    return db.open_project(home, STORE_PROJECT, data_dir=data)


_mkdb_mod = None


def _load_mkdb():
    global _mkdb_mod
    if _mkdb_mod is None:
        spec = importlib.util.spec_from_file_location("mkdb", os.path.join(HERE, "mkdb.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _mkdb_mod = mod
    return _mkdb_mod


# A terminal row from the previous round must not decide this watch (SR2).
STORE_TERMINAL = ("done", "verifying", "review", "merged", "blocked")


def _row_epoch(updated):
    """mkdb stores ISO-8601 text; an epoch float also parses. Unparseable is 0."""
    try:
        return float(updated)
    except (TypeError, ValueError):
        try:
            import datetime
            return datetime.datetime.fromisoformat(str(updated)).timestamp()
        except (TypeError, ValueError):
            return 0.0


def store_status_row(data, armed):
    """The tasks row for this armed round when the store is on and the row exists, else None.
    A miss (store off, no row, wrong round) is the status-file fallback, never an exception.
    A read failure is not a miss: it returns {"store_error": "<why>"} so the verdict can name
    it. A terminal row (done/verifying/review/merged/blocked) whose updated is not after the
    watch's armed_at is the previous round's outcome and is ignored."""
    if not store_on(data) or not armed or not armed.get("run") or not armed.get("task"):
        return None
    m = PHASE_ROUND_RE.match(str(armed.get("phase") or ""))
    if not m:
        return None
    rnd = int(m.group(3))
    try:
        con = store_con(data)
        try:
            row = con.execute(
                "SELECT id, status, round, verdict, updated FROM tasks WHERE run=? AND id=?",
                (armed["run"], armed["task"])).fetchone()
        finally:
            con.close()
    except Exception as e:
        return {"store_error": f"{type(e).__name__}: {e}"}
    if not row or row["round"] != rnd:
        return None
    status = (row["status"] or "").lower()
    # A terminal row whose updated is not after armed_at is the previous round's outcome
    # (task round bumps the round but the row keeps its old timestamp). A row written in
    # the same second as the arm still counts: ISO timestamps have whole seconds.
    armed_at = armed.get("armed_at")
    # 30s, not 1s: a loaded machine can take well over a second between the status
    # write and the arm, and ISO timestamps are whole seconds. A row from the PREVIOUS
    # round is minutes-to-hours older, so 30s still rejects it.
    if (armed_at is not None and status in STORE_TERMINAL
            and _row_epoch(row["updated"]) + 30.0 <= float(armed_at)):
        return None
    return row


def store_report_ok(data, armed, report):
    """With the store on, a report artifact for this task+round satisfies the report check.
    Store off, or no such artifact: the caller keeps the file check. None = not decided here."""
    if not report or not store_on(data) or not armed or not armed.get("run") or not armed.get("task"):
        return None
    m = PHASE_ROUND_RE.match(str(armed.get("phase") or ""))
    rnd = int(m.group(3)) if m else None
    try:
        con = store_con(data)
        try:
            q = ("SELECT 1 FROM artifacts WHERE run=? AND task=? AND kind='report'"
                 + (" AND round=?" if rnd is not None else "") + " LIMIT 1")
            args = (armed["run"], armed["task"]) + ((rnd,) if rnd is not None else ())
            hit = con.execute(q, args).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    return True if hit else None


def child_progress(pane_pid, now=None):
    """D5A.3: (active, scale). active when a descendant of the pane pid runs a test/build
    command (selftest.sh, `claude plugin test`, pytest, `python3 ... --self-test`, tsc).
    scale = max(1, loadavg_1m / cpu_count): the grace after the child exits is stall_min*scale.
    (False, 1.0) when ps cannot be read — a read failure never holds a stall off."""
    if not pane_pid:
        return False, 1.0
    try:
        r = subprocess.run(["ps", "-ax", "-o", "pid=,ppid=,command="],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return False, 1.0
    except Exception:
        return False, 1.0
    kids, cmd = {}, {}
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        pid, ppid = parts[0], parts[1]
        kids.setdefault(ppid, []).append(pid)
        cmd[pid] = parts[2] if len(parts) > 2 else ""
    stack, seen, hit = list(kids.get(str(pane_pid), [])), set(), False
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if CHILD_PROGRESS_RE.search(cmd.get(cur, "")):
            hit = True
            break
        stack.extend(kids.get(cur, []))
    try:
        load1 = os.getloadavg()[0]
        cpus = os.cpu_count() or 1
    except Exception:
        load1, cpus = 0.0, 1
    return hit, min(CHILD_SCALE_CAP, max(1.0, float(load1) / float(cpus or 1)))


def read_status(lane, armed):
    """The lane's .matrixkoder-status.json when it passes reader acceptance (status-contract.md
    R27, in order), else None -> today's pane-text path unchanged (R28). With the project store
    on, a tasks row for this run/task/round is preferred and shaped like the status file
    (state done/blocked from the row); no row for the round falls back to this file. No exception
    ever escapes
    (R29): a missing file, corrupt JSON or a wrong-typed field is a miss, never a crash. Unknown
    fields are ignored (R38). The only file this watcher ever gains; the caller passes the result
    to decide() as st."""
    row = store_status_row((armed or {}).get("data"), armed)
    if isinstance(row, dict) and row.get("store_error"):
        # A failed store read is a named miss, not silence and not the pane path: decide()
        # keeps watching (source=status) instead of grace-DONE or STALL on a quiet lane.
        return {"v": 1, "seq": None, "run": (armed or {}).get("run"),
                "task": (armed or {}).get("task"), "round": None, "role": "builder",
                "state": "store_error", "question": None, "updated_at": 0.0,
                "source": "status", "store_error": row["store_error"]}
    if row is not None:
        # the store row for this round wins; the status file is not read. seq stays None so the
        # row never enters _seen_seq (that counter belongs to the file channel only).
        status = (row["status"] or "").lower()
        verdict = row["verdict"] or ""
        fresh = True
        if status == "blocked" or verdict.upper().startswith("BLOCKED"):
            state, question = "blocked", verdict or "blocked"
        elif status in ("verifying", "review", "merged", "done") or verdict.upper().startswith("DONE"):
            state, question = "done", None
        else:
            # a non-terminal row is not a heartbeat: updated_at 0 so it never resets the idle clock
            state, question, fresh = "started", None, False
        upd = _row_epoch(row["updated"]) if fresh else 0.0
        return {"v": 1, "seq": None, "run": armed.get("run"), "task": armed.get("task"),
                "round": row["round"], "role": "builder", "state": state, "question": question,
                "updated_at": upd, "source": "store"}
    try:
        with open(os.path.join(lane, STATUS_FILE)) as f:   # R27.1: <lane>/.matrixkoder-status.json
            st = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict):
        return None   # R27.1: a JSON object
    v = st.get("v")
    if not isinstance(v, int) or isinstance(v, bool) or v > 1:
        return None   # R27.2: an int the reader supports; a newer v ignores the WHOLE file (R39)
    if st.get("run") != armed.get("run"):
        return None   # R27.3: an armed watch without a run never accepts (None != a string)
    if st.get("task") != armed.get("task"):
        return None   # R27.4: the watch's task (the group lead, not another member)
    m = PHASE_ROUND_RE.match(str(armed.get("phase") or ""))   # R27.5/R27.6: the round + reviewer marker
    rnd = st.get("round")
    if (not m or m.group(1) != armed.get("task")
            or not isinstance(rnd, int) or isinstance(rnd, bool) or rnd != int(m.group(3))):
        return None   # R27.5: a parse failure, task mismatch or a non-matching round is not accepted
    if st.get("role") != ("reviewer" if m.group(2) else "builder"):
        return None   # R27.6: the REVIEW marker arms the reviewer role, and nothing else does
    upd = st.get("updated_at")
    if (not isinstance(upd, (int, float)) or isinstance(upd, bool)
            or upd < float(armed.get("armed_at") or 0.0)):
        return None   # R27.7: full-precision epoch seconds on both sides (R7), never rounded
    if st.get("state") not in STATUS_STATES:
        return None   # R27.8: R12's five values
    seq = st.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return None   # R27.9: type + lower bound (Table 1)
    ident = (lane, st.get("run"), st.get("task"), rnd, st.get("role"))
    if ident in _seen_seq and seq <= _seen_seq[ident]:
        return None   # R27.9: rewound or replayed — never the newer truth a reader should act on
    _seen_seq[ident] = seq
    return st


CHECK_HOLD_MAX_S = 4 * 3600  # a check older than this no longer holds DONE/STALL


def check_hold(lane, state, ev):
    """A check the lane started with `mk-builder.py check` and whose supervisor pid is alive keeps
    the lane busy: no STALL and no DONE verdict. A dead pid (a crash that left no clear) counts as
    finished, so a stall is never hidden forever. Reads the file directly: it needs no R27 acceptance."""
    if state not in ("DONE", "STALL"):
        return state, ev
    try:
        with open(os.path.join(lane, STATUS_FILE)) as f:
            checks = json.load(f).get("checks")
        for c in (checks or {}).values():
            pid, started = c.get("pid"), c.get("started")
            if isinstance(started, (int, float)) and time.time() - started > CHECK_HOLD_MAX_S:
                continue  # over the cap: a recycled pid must not hold the lane forever
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError):
                    continue  # gone, or not ours (a recycled pid)
                return None, []
    except (OSError, ValueError, AttributeError):
        pass
    return state, ev


def status_activity(st, stall_min, armed):
    """R33/R34: an accepted working/subagent file whose updated_at is inside stall_min is activity
    for this poll — a writer that stops moving lets the clock run out. The comparison is in SECONDS
    on both sides (R33): stall_min is minutes, so it is multiplied by 60. state=subagent counts only
    while now - armed_at <= max_wait_min * 60 (R34) — the WATCH's armed_at, never the file's
    started_at; past that the pane spinner/toolcall detection alone keeps the lane alive. The 30.0
    literal is the last-resort echo of the schema default (session.max_wait_min = 30); callers
    that resolve cfg() pass the real value in armed."""
    if not st or st.get("state") not in ("working", "subagent"):
        return False
    now = time.time()
    if now - float(st.get("updated_at") or 0) >= float(stall_min) * 60:
        return False   # R33: the heartbeat is stale — not activity
    if st["state"] == "subagent":
        if now - float((armed or {}).get("armed_at") or 0) > \
                float((armed or {}).get("max_wait_min") or 30.0) * 60:
            return False   # R34: subagent is honoured only up to max_wait_min of the watch's armed_at
    return True


def decide(h, subj, rx, report_ok, text, phase, stall_min, st=None, armed=None):
    """One poll's verdict -> (state | None to keep watching, evidence list). Pure: no herdr, no git.
    rx None = --no-commit (a reviewer pane: no commit to match): any commit counts as matching, no
    commit evidence is reported, and STALL never lists a matching commit as missing. st = a status
    file accepted by read_status (R27) or None; a file that missed any R27 condition fell back to
    this pane-text path unchanged (R28). Every verdict's evidence ends with its source (R41):
    status = decided from the accepted file, pane = the pane-text path, grace = the no-token rule."""
    if st and st.get("state") == "store_error":
        # The store could not be read. Do not fall through to the pane/grace path: a quiet
        # idle lane must not become DONE or STALL because of a locked or corrupt mk.db.
        why = st.get("store_error") or "unknown"
        return None, [f"store_error: {why}", "source=status"]
    done_tok, question = phase_tokens(text, phase)
    commit_ok = rx is None or bool(rx.search(subj.partition(" ")[2]) or rx.search(subj))  # subj = "<sha> <subject>"
    commit_ev = [] if rx is None else [f"commit {subj}"]
    status = h.get("agent_status")
    f_done = bool(st) and st.get("state") == "done"        # R30: the file IS the DONE token
    f_blocked = bool(st) and st.get("state") == "blocked"  # R32
    # R30: an accepted done file overrides ONLY the pane-status ("working") and herdr-ERROR halves
    # of this branch; the commit and report checks stay exactly as today (commit_ok/report_ok feed it)
    if commit_ok and report_ok and (f_done or (h["state"] != "ERROR" and status != "working"
                                               and (done_tok or not phase))):
        if f_done and (st.get("source") != "store" or (commit_ok and report_ok)):
            # R31 rows 1-2: the accepted file decides. A store row (D5A.1) decides the same way
            # but ONLY with the commit and the report artifact — DONE still needs both — and its
            # tag is source=store with no file seq.
            if st.get("source") == "store":
                return "DONE", commit_ev + ["store row done", "source=store"]
            return "DONE", commit_ev + [f"status file done (seq {st['seq']})", "source=status"]
        return "DONE", commit_ev + ([f"{phase} DONE"] if phase else []) + ["source=pane"]
    if question:
        return "BLOCKED", [f"{phase} BLOCKED: {question}", "source=pane"]
    if f_blocked:   # R32: the file's question, the pane path's shape
        if st.get("source") == "store":
            return "BLOCKED", [f"store row blocked: {st['question']}", "source=store"]
        return "BLOCKED", [f"status file blocked: {st['question']}", "source=status"]
    idle = h.get("last_change_min")
    if (h["state"] != "ERROR" and commit_ok and report_ok and status in ("idle", "done") and phase
            and idle is not None and idle >= min(TOKEN_GRACE_MIN, stall_min)):
        # builders (Claude especially) end with a summary and forget the token; the work is there, so the
        # orchestrator verifies it rather than waiting for WATCH_EXPIRED. R30 overrides nothing here:
        # a non-terminal file state never touches the grace (R31's table gives the file precedence
        # only for done/blocked).
        got = "commit + report" if rx is not None else "report"
        return "DONE", commit_ev + [f"no {phase} DONE token ({got}, agent {status} {idle} min)",
                                    "source=grace"]
    if status_activity(st, stall_min, armed):
        # R33/R34: an accepted working/subagent file with updated_at inside stall_min (subagent only
        # within max_wait_min of armed_at) is activity for THIS poll; no STALL. Past that the existing
        # pane spinner/toolcall detection alone keeps the lane alive (R34).
        return None, []
    if h["state"] in FAIL_STATES:
        return h["state"], (h.get("evidence") or []) + ["source=pane"]   # R41: pane detections (R35)
    if status == "working" and idle is not None and idle >= stall_min:
        return "STALL", [f"working but no file/commit/pane change for {idle} min", "source=pane"]
    if status in ("idle", "done") and idle is not None and idle >= stall_min:
        missing = [n for n, ok in (("matching commit", commit_ok), ("report", report_ok),
                                   (f"{phase} DONE token", done_tok or not phase)) if not ok]
        return "STALL", [f"agent {status}, no file/commit change for {idle} min, missing: {', '.join(missing)}",
                         "source=pane"]
    return None, []


def _leaves(ts, task):
    """Leaf descendants of task, in ts insertion order. ts items have id + parent."""
    pid = next(str(t["id"]) for t in ts if str(t["id"]).lower() == task.lower())
    parent_of = lambda tid: [str(c["id"]) for c in ts if c.get("parent") == tid]
    kids, stack, seen = [], [pid], {pid}
    while stack:  # a cyclic hand-edit must not hang this
        cur = stack.pop()
        next_ = [k for k in parent_of(cur) if k not in seen]
        seen.update(next_)
        stack.extend(next_)
        if cur != pid and not next_:
            kids.append(cur)  # a leaf descendant: it can hold a status
    return [str(t["id"]) for t in ts if str(t["id"]) in kids]


def child_tasks(task, data, run=None):
    """Stored ids of <task>'s status-holding descendants — every child, recursing past children
    that are parents themselves (mk-state refuses status= on ANY parent: "status rolls up from
    children"; intermediate parents get the verdict by roll-up, leaves by the write). [] for a
    leaf, an unknown task, an unreadable or ambiguous run: the mk-state write itself then decides,
    exactly as before. With the project store on, the tasks table is the tree (parent column);
    otherwise the run.json files. Mirrors mk-state's run pick: --run names the run; else the
    single active run; with several, the one that owns the task (ids match case-insensitively)."""
    if store_on(data):
        try:
            con = store_con(data)
            try:
                if run:
                    rows = con.execute("SELECT id, parent FROM tasks WHERE run=?", (run,)).fetchall()
                    if not any(str(r["id"]).lower() == task.lower() for r in rows):
                        return []
                else:
                    actives = [r["id"] for r in con.execute(
                        "SELECT id FROM runs WHERE status='active'").fetchall()]
                    owners = []
                    for rid in actives:
                        rs = con.execute("SELECT id, parent FROM tasks WHERE run=?", (rid,)).fetchall()
                        if any(str(r["id"]).lower() == task.lower() for r in rs):
                            owners.append(rs)
                    if len(owners) != 1:
                        return []
                    rows = owners[0]
            finally:
                con.close()
        except Exception:
            return []
        return _leaves([{"id": r["id"], "parent": r["parent"]} for r in rows], task)
    runs = os.path.join(data, "runs")

    def load(rid):
        try:
            with open(os.path.join(runs, rid, "run.json")) as f:
                j = json.load(f)
            return j if isinstance(j, dict) else None
        except (OSError, ValueError):
            return None

    def owns(j):
        return isinstance(j.get("tasks"), list) and any(
            isinstance(t, dict) and str(t.get("id") or "").lower() == task.lower() for t in j["tasks"])

    if run:
        src = load(run)
        if src is None or not owns(src):
            return []
    else:
        try:
            with open(os.path.join(runs, "index.json")) as f:
                actives = [r["id"] for r in json.load(f)
                           if isinstance(r, dict) and r.get("status") == "active" and r.get("id")]
        except (OSError, ValueError):
            return []
        owners = [j for j in (load(r) for r in actives) if j and owns(j)]
        if len(owners) != 1:
            return []
        src = owners[0]
    ts = [t for t in src["tasks"] if isinstance(t, dict) and t.get("id")]
    return _leaves(ts, task)  # run.json insertion order


def record(out, task, data, run=None):
    """Write the exit into run state via mk-state: every child when <task> is a parent (mk-state
    refuses status= on a parent — "status rolls up from children" — so the children carry the verdict
    and the parent only rolls up), else the task itself. <task> may also be a LIST of ids (a shared
    lane group, --tasks A,B): the verdict reaches every listed task's children — one delivered
    dispatch marks them all building, one verdict marks them all done — and a task listed twice
    (or two listed tasks sharing a child) is written once. -> mk-state's reply for one target, or
    {ok, children, results[, error]} for several; every ok:false reply is reported, never dropped."""
    note = f"lane-watch {out['state']}: " + "; ".join(out.get("evidence") or [out.get("commit") or ""])
    common = ["--data", data, "--by", "lane-watch"] + (["--run", run] if run else [])
    status = STATUS_MAP.get(out["state"])
    ids = [task] if isinstance(task, str) else [str(t) for t in task]
    targets, seen = [], set()
    for tid in ids:
        for c in (child_tasks(tid, data, run) or [tid]):
            if c.lower() not in seen:
                seen.add(c.lower())
                targets.append(c)
    results = []
    for t in targets:
        t_note, t_status = note, status
        if t_status:
            r = subprocess.run([sys.executable, MK_STATE, "task", "get", t, *common], capture_output=True, text=True)
            try:
                cur = json.loads(r.stdout.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                cur = {"ok": False}
            if cur.get("ok") and cur["task"]["status"] not in WATCHED:  # late watcher: never overwrite review/merged/...
                t_note += f" (status {cur['task']['status']} kept)"
                t_status = None
        if t_status:
            pairs = [f"status={t_status}"]
            if out["state"].startswith("DONE") and out.get("commit"):
                pairs.append(f"last_commit={out['commit'].split()[0]}")
            cmd = ["task", "set", t, *pairs, "--note", t_note[:300]]
        else:
            cmd = ["event", "--task", t, "--type", "note", "--text", t_note[:300]]
        r = subprocess.run([sys.executable, MK_STATE, *cmd, *common], capture_output=True, text=True)
        try:
            results.append(json.loads(r.stdout.strip().splitlines()[-1]))
        except (json.JSONDecodeError, IndexError):
            results.append({"ok": False, "error": (r.stderr or r.stdout).strip()[-200:]})
    if len(results) == 1:
        return results[0]
    bad = [f"{t}: {x.get('error') or 'mk-state replied ok:false without an error'}"
           for t, x in zip(targets, results) if not x.get("ok")]
    res = {"ok": not bad, "children": targets, "results": results}
    if bad:
        res["error"] = "; ".join(bad)[:300]
    return res


FOOTER_LINES = 8  # the limit banner lives in the pane footer; the transcript above it is not a banner


def pane_footer(text, lines=FOOTER_LINES):
    """The last <lines> non-empty pane lines. The real limit banner is printed in the footer
    (OpenCode's "Free usage exceeded … [retrying in …]" row); the same phrase in the transcript
    above it is the agent TALKING about a limit — a brief line, a test fixture, a quoted banner —
    and must not wake a watcher (tonight at 04:00 J1.3's own test text on screen did)."""
    return "\n".join([l for l in text.splitlines() if l.strip()][-lines:])


def quota_beats_stall(agent, kind):
    """lane-health's quota detection over the pane FOOTER — the marker-scoped text a poll
    classifies can cut a limit banner out: when the agent later echoes its dispatch prompt (a
    `cat brief` line, a re-printed instruction), since_dispatch's rfind starts after the LAST
    echo and the banner above it is scoped away — so before STALL is ever decided, the footer
    (last FOOTER_LINES non-empty lines) goes through lane-health (imported, the way these
    scripts share code) and a hit records the usage ledger with its reset time. The transcript
    is deliberately NOT searched: a banner the agent merely quotes is not a banner.
    -> ("QUOTA", evidence) or (None, [])."""
    text = pane_footer(lh.pane_text(agent))
    # the same banner filter classify uses: a quota word inside a test log in the footer
    # is not a wall, so it must not beat a stall or write the ledger
    text = "\n".join(lh.banner_lines(text))
    hits = lh.quota_hits(text)
    if not hits:
        return None, []
    try:
        lh.record_quota(lh.default_ledger(), kind or agent, text)
    except Exception as e:  # a ledger write never hides the verdict itself
        return "QUOTA", hits[-2:] + [f"ledger write failed: {type(e).__name__}: {e}"[:200], "source=pane"]
    until, window = lh.parse_reset(text)
    return "QUOTA", hits[-2:] + ([f"exhausted_until {until} ({window})"] if until
                                   else ["exhausted_until unknown"]) + ["source=pane"]   # R41 (R35)


def child_grace_end(child_first, child_seen_until, running, stall_min, scale, max_wait, now):
    """End of the child-progress grace, or None when a STALL is not suppressed.

    While the child still runs, the window stays open until max(4*stall_min, the watch's
    max-wait) after the child was FIRST seen. After it exits, only stall_min*scale remains
    (scale <= 4), never past that same bound. A leaked child cannot hold a lane forever;
    a real stall fires once the bound passes. Pure: the caller passes the clock."""
    if child_seen_until is None:
        return None
    alive_until = child_first + max(stall_min * 60 * CHILD_SCALE_CAP, max_wait)
    end = alive_until if running else min(child_seen_until + stall_min * 60 * scale, alive_until)
    return end if now < end else None


def watch(a):
    no_commit = "--no-commit" in a
    lane, agent, grep = opt(a, "--lane"), opt(a, "--agent"), opt(a, "--commit-grep")
    report, then_send, phase = opt(a, "--report"), opt(a, "--then-send"), opt(a, "--phase")
    kind, marker = opt(a, "--kind"), opt(a, "--prompt-marker")
    max_wait = float(cfg("session.max_wait_min", opt(a, "--max-wait-min"))) * 60
    poll = float(opt(a, "--poll", "20"))
    stall_min = float(cfg("session.stall_min", opt(a, "--stall-min")))
    rx = None if no_commit else re.compile(grep)  # --no-commit: no commit to match; decide() counts rx None as ok
    start, errors = time.time(), 0
    active, last_fp = start, None  # last time the pane showed new output
    # D5A.3: last epoch a test/build descendant of the pane was seen. After it exits,
    # grace = stall_min * max(1, loadavg_1m / cpu_count), measured when the child was last seen running.
    child_seen_until, child_scale, child_first = 0.0, 1.0, 0.0
    t_one, t_list = opt(a, "--task"), opt(a, "--tasks")
    # the armed watch read_status() accepts against (R27): run/task/phase-round/role + armed_at;
    # a watch without --run, --task or a "<T>[ REVIEW] R<n>" phase never accepts a file (R27.3-5)
    armed = {"lane": lane, "run": opt(a, "--run"),
             "task": t_one or (t_list.split(",")[0].strip() if t_list else None),
             "phase": phase, "armed_at": start, "max_wait_min": max_wait / 60,
             "data": opt(a, "--data")}
    while True:
        subj = commit_subject(lane)
        # math.inf: lane-health judges only files/commits; STALL is decided in decide() with pane activity
        h = lh.check(agent, lane, kind, math.inf, active, marker)
        text = h.get("text", "")
        fp = pane_fingerprint(text)
        # pane moved, or a subagent task line (spinner + "Task —") is running: the lane is alive, so
        # idle time counts from now; only when no task line remains does the stall clock run again
        if last_fp is not None and (fp != last_fp or running_task(text)):
            active = time.time()
            h = lh.check(agent, lane, kind, math.inf, active, marker)
        last_fp = fp
        st = read_status(lane, armed)
        if status_activity(st, stall_min, armed):
            # R33: the reader moves the lane's idle-clock mark to the file's write (never backwards)
            active = max(active, float(st["updated_at"]))
        report_ok = not report or os.path.exists(os.path.join(lane, report))
        if not report_ok and store_report_ok(opt(a, "--data"), armed, report):
            report_ok = True  # the report artifact is in the store; the file is absent
        state, ev = decide(h, subj, rx, report_ok, h.get("text", ""), phase, stall_min, st, armed)
        state, ev = check_hold(lane, state, ev)
        # pane pid: herdr status if present, else MK_PANE_PID (tests). No herdr call of our own.
        pane_pid = os.environ.get("MK_PANE_PID") or h.get("pane_pid")
        running, scale = child_progress(pane_pid)
        if running:
            now_c = time.time()
            if not child_first:
                child_first = now_c
            child_seen_until, child_scale = now_c, scale
        if state == "STALL" and child_grace_end(
                child_first, child_seen_until, running, stall_min, child_scale, max_wait, time.time()):
            state = None  # quiet pane, but the test/build child (or its grace) is still in play
        errors = errors + 1 if state == "ERROR" else 0
        if state == "ERROR" and errors < ERROR_RETRIES and time.time() - start <= max_wait:
            time.sleep(poll)
            continue
        if state == "STALL":  # quota beats stall: the whole pane text is checked for a limit banner
            qstate, qev = quota_beats_stall(agent, kind)
            if qstate:
                state, ev = qstate, qev
        if state == "DONE" and then_send:
            msg = f"Read and follow {then_send} exactly. Work autonomously until complete, then reply with the DONE token it names."
            r = subprocess.run([sys.executable, ASK, agent, msg, "--no-wait"], capture_output=True, text=True)
            return {"state": "DONE_SENT" if r.returncode == 0 else "ERROR", "commit": subj, "evidence": ev,
                    "sent": then_send, "ask_rc": r.returncode, "ask_out": (r.stdout + r.stderr).strip()[-200:]}
        if state:
            return {"state": state, "commit": subj, "evidence": ev, "agent_status": h.get("agent_status")}
        if time.time() - start > max_wait:
            return {"state": "WATCH_EXPIRED", "commit": subj, "agent_status": h.get("agent_status"),
                    "last_change_min": h.get("last_change_min"),
                    "evidence": ["watch window ended", "source=pane"]}   # R41: a pane detection
        time.sleep(poll)


def self_test():
    # Load-proof windows (W1): one trivial spawn is timed and every stall window
    # below is multiplied by it, so a loaded machine still gets several polls
    # inside max-wait. The ratio stays, so a stall still fires inside the window.
    _t0 = time.time()
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    # named _spawn_scale, not _scale: self_test later rebinds `_hit, _scale` from
    # child_progress, and a win() after that point must still see the spawn scale
    _spawn_scale = max(1.0, (time.time() - _t0) / 0.05)

    def win(stall, wait):
        # last_change_min rounds UP to the next 0.1 min, so one slow poll reads as
        # 0.2. The floor sits above that. Below scale ~4 the floor wins, so every
        # pair comes out 0.25/0.75 (a 2:1 pair becomes 1:3); a stall still fires
        # inside the window and a live pane still outlives it.
        return f"{max(0.25, stall * _spawn_scale):.4f}", f"{max(0.75, wait * _spawn_scale):.4f}"


    rx = re.compile(r"^\w+ feat\(p1\)")
    ok = {"state": "OK", "evidence": [], "agent_status": "idle", "last_change_min": 1}
    good, other = "abc123 feat(p1): state core", "def456 chore: wip"
    # DONE without --phase: commit + report + not working
    assert decide(ok, good, rx, True, "", None, 10)[0] == "DONE"
    assert decide({**ok, "agent_status": "working"}, good, rx, True, "", None, 10)[0] is None
    assert decide(ok, good, rx, False, "", None, 10)[0] is None
    # a ^-anchored regex matches the bare subject (lesson 36); an unanchored one is unaffected
    assert decide(ok, good, re.compile(r"^feat\(p1\): state"), True, "", None, 10)[0] == "DONE"
    assert decide(ok, other, re.compile(r"^feat\(p1\)"), True, "", None, 10)[0] is None
    assert decide(ok, good, re.compile(r"state core"), True, "", None, 10)[0] == "DONE"
    # with --phase the DONE token is also required, at line start only
    assert decide(ok, good, rx, True, "I will reply with P1 DONE when finished", "P1", 10)[0] is None
    assert decide(ok, good, rx, True, "all gates green\n⏺ P1 DONE\n", "P1", 10) == \
        ("DONE", [f"commit {good}", "P1 DONE", "source=pane"])
    assert decide(ok, other, rx, True, "P1 DONE", "P1", 10)[0] is None
    assert decide(ok, good, rx, True, "P2 DONE", "P1", 10)[0] is None
    # BLOCKED token wins and carries the question
    st, ev = decide({**ok, "agent_status": "working"}, other, rx, True, "● P1 BLOCKED: which schema version?\n", "P1", 10)
    assert st == "BLOCKED" and ev == ["P1 BLOCKED: which schema version?", "source=pane"]
    # placeholder lines from an echoed brief never count; the later of DONE / BLOCKED wins
    assert decide(ok, other, rx, True, "Reply P1 BLOCKED: <question> if stuck", "P1", 10)[0] is None
    assert phase_tokens("P1 BLOCKED: <question>\nP1 DONE <sha>\n", "P1") == (False, None)
    assert phase_tokens("P1 BLOCKED: which port?\nok, 3001\nP1 DONE\n", "P1") == (True, None)
    assert phase_tokens("P1 DONE\nP1 BLOCKED: tests now fail?\n", "P1") == (False, "tests now fail?")
    # a finished lane is DONE even if a stray limit / dev-server string is on screen; ERROR never means DONE
    for s_ in ("QUOTA", "DEV_SERVER", "PROVIDER"):
        assert decide({**ok, "state": s_, "evidence": ["x"]}, good, rx, True, "P1 DONE", "P1", 10)[0] == "DONE"
    assert decide({**ok, "state": "ERROR", "evidence": ["x"]}, good, rx, True, "", None, 10)[0] == "ERROR"
    # health failures pass through
    for s in FAIL_STATES:
        assert decide({**ok, "state": s, "evidence": ["x"]}, other, rx, True, "", None, 10) == (s, ["x", "source=pane"])
    # working agent: STALL only from lane-watch's own idle (files/commits/pane); lane-health's STALL is ignored
    assert decide({**ok, "state": "STALL", "agent_status": "working"}, other, rx, True, "", None, 10)[0] is None
    st, ev = decide({**ok, "agent_status": "working", "last_change_min": 11}, other, rx, True, "", None, 10)
    assert st == "STALL" and "pane" in ev[0]
    # new STALL: idle/done, no matching commit, no change for stall_min since watch start
    st, ev = decide({**ok, "last_change_min": 11}, other, rx, True, "", None, 10)
    assert st == "STALL" and "matching commit" in ev[0]
    assert decide({**ok, "agent_status": "done", "last_change_min": 11}, other, rx, True, "", None, 10)[0] == "STALL"
    assert decide({**ok, "last_change_min": 9}, other, rx, True, "", None, 10)[0] is None
    # commit + report + idle, no token: DONE after the grace (lesson 39), never before; no report: still STALL
    st, ev = decide({**ok, "last_change_min": 4}, good, rx, True, "", "P1", 10)
    assert st == "DONE" and "no P1 DONE token" in ev[1], (st, ev)
    assert decide({**ok, "last_change_min": 2}, good, rx, True, "", "P1", 10)[0] is None
    assert decide({**ok, "agent_status": "working", "last_change_min": 4}, good, rx, True, "", "P1", 10)[0] is None
    st, ev = decide({**ok, "last_change_min": 11}, good, rx, False, "", "P1", 10)
    assert st == "STALL" and "report" in ev[0] and "matching commit" not in ev[0]
    # a live check holds DONE and STALL; a dead pid does not
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _ck:
        def _hold(pid):
            with open(os.path.join(_ck, STATUS_FILE), "w") as f:
                json.dump({"checks": {"c": {"pid": pid}}}, f)
            return check_hold(_ck, "STALL", ["x"]), check_hold(_ck, "DONE", ["y"])
        assert _hold(os.getpid()) == ((None, []), (None, []))
        dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
        assert _hold(dead.pid) == (("STALL", ["x"]), ("DONE", ["y"]))
        with open(os.path.join(_ck, STATUS_FILE), "w") as f:
            json.dump({"checks": {"c": {"pid": os.getpid(), "started": time.time() - CHECK_HOLD_MAX_S - 60}}}, f)
        assert check_hold(_ck, "STALL", ["x"]) == ("STALL", ["x"])  # live pid, over the cap
        assert check_hold(os.path.join(_ck, "nope"), "STALL", ["x"]) == ("STALL", ["x"])
        assert check_hold(_ck, "BLOCKED", ["z"]) == ("BLOCKED", ["z"])
    # --no-commit (rx None, a reviewer pane): no commit to match — any subject is fine, no commit evidence
    assert decide(ok, other, None, True, "P1 DONE", "P1", 10) == ("DONE", ["P1 DONE", "source=pane"])
    assert decide(ok, other, None, True, "I will reply with P1 DONE", "P1", 10)[0] is None  # token still required
    assert decide({**ok, "agent_status": "working"}, other, None, True, "P1 DONE", "P1", 10)[0] is None
    assert decide(ok, other, None, False, "P1 DONE", "P1", 10)[0] is None  # report still required
    # the no-token grace loses its commit half too: report + idle 3 min is DONE, never before
    st, ev = decide({**ok, "last_change_min": 4}, other, None, True, "", "P1", 10)
    assert st == "DONE" and ev == ["no P1 DONE token (report, agent idle 4 min)", "source=grace"], (st, ev)
    assert decide({**ok, "last_change_min": 2}, other, None, True, "", "P1", 10)[0] is None
    assert decide({**ok, "agent_status": "working", "last_change_min": 4}, other, None, True, "", "P1", 10)[0] is None
    # idle STALL never lists a matching commit as missing in this mode (report gone, so the grace cannot fire)
    st, ev = decide({**ok, "last_change_min": 11}, other, None, False, "P1 DONE", "P1", 10)
    assert st == "STALL" and "report" in ev[0] and "matching commit" not in ev[0]
    # pane activity: digit-only changes (timers) are not activity; new output lines are
    assert pane_fingerprint("Working (12s) esc to interrupt") == pane_fingerprint("Working (73s) esc to interrupt")
    assert pane_fingerprint("$ vitest run\n") != pane_fingerprint("$ vitest run\n 10 failed\n")
    # the ↳ N toolcalls counter is activity (a changed count moves the fingerprint); its timer is not
    assert pane_fingerprint("⠼ General Task — E2\n↳ 49 toolcalls · 12m 48s\n") != \
        pane_fingerprint("⠼ General Task — E2\n↳ 50 toolcalls · 12m 48s\n")
    assert pane_fingerprint("⠼ General Task — E2\n↳ 49 toolcalls · 12m 48s\n") == \
        pane_fingerprint("⠼ General Task — E2\n↳ 49 toolcalls · 13m 5s\n")
    # a running task line (spinner + "Task —") is activity by presence; finished ✓ lines are not
    assert running_task("⠼ General Task — E2 reviewer key, quiet optional\n")
    assert running_task("⠋ Task — x") and running_task("⠏Task — x")
    assert not running_task("✓ General Task — E2 reviewer key\n")
    assert not running_task("some Task — mentioned mid-prose\n") and not running_task("↳ 49 toolcalls\n")
    # status map covers every mapped exit with a valid mk-state status
    sp = importlib.util.spec_from_file_location("mk_state", MK_STATE)
    mk = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(mk)
    assert set(STATUS_MAP.values()) <= set(mk.STATUSES)
    assert STATUS_MAP["DONE"] == "verifying" and STATUS_MAP["STALL"] == "stalled"

    # --- the status-file channel (status-contract.md R27-R41): read_status() acceptance + decide(st, armed) ---
    import shutil, tempfile
    stmp = tempfile.mkdtemp(prefix="lane-watch-status-")  # fixture lanes only, never a real .matrixkoder/
    try:
        t0 = time.time() - 120  # the watch armed 2 min ago

        def wstat(dir_, **kw):
            """One contract-valid v1 status file (Table 1 fields) into dir_, kw overriding; -> path."""
            j = {"v": 1, "seq": 5, "run": "r1", "task": "P1", "round": 1, "role": "builder",
                 "agent": "opencode", "state": "working", "step": "start", "phases_done": [],
                 "question": None, "commit": None, "report": ".matrixkoder-report-r1.md",
                 "subagent": None, "ctx_pct": None, "started_at": t0, "updated_at": t0 + 1}
            j.update(kw)
            p = os.path.join(dir_, STATUS_FILE)
            with open(p, "w") as f:
                json.dump(j, f)
            return p

        def mklane(name):  # a fresh lane dir per case: _seen_seq identities never leak across cases
            d = os.path.join(stmp, name)
            os.makedirs(d, exist_ok=True)
            return d

        armed = {"lane": stmp, "run": "r1", "task": "P1", "phase": "P1 R1",
                 "armed_at": t0, "max_wait_min": 30}

        # done file + commit + report and no pane token -> DONE, source=status (R30/R31 row 2)
        ln = mklane("done")
        wstat(ln, state="done", seq=5)
        st = read_status(ln, armed)
        assert st and st["state"] == "done" and st["seq"] == 5, st
        assert decide(ok, good, rx, True, "", "P1 R1", 10, st, armed) == \
            ("DONE", [f"commit {good}", "status file done (seq 5)", "source=status"])
        # R30: the done file overrides the pane-status and herdr-ERROR halves ONLY; commit + report stay
        assert decide({**ok, "agent_status": "working"}, good, rx, True, "", "P1 R1", 10, st, armed)[0] == "DONE"
        assert decide({**ok, "state": "ERROR", "evidence": ["x"]}, good, rx, True, "", "P1 R1", 10,
                      st, armed) == ("DONE", [f"commit {good}", "status file done (seq 5)", "source=status"])
        assert decide(ok, other, rx, True, "", "P1 R1", 10, st, armed)[0] is None  # commit mismatch: no DONE
        assert decide(ok, good, rx, False, "", "P1 R1", 10, st, armed)[0] is None   # report missing: no DONE
        # the reviewer form (--no-commit, rx None): phase "D2 REVIEW R1" arms role=reviewer (R27.6)
        armed_rev = {"lane": stmp, "run": "r1", "task": "D2", "phase": "D2 REVIEW R1",
                     "armed_at": t0, "max_wait_min": 30}
        ln = mklane("done-rev")
        wstat(ln, task="D2", role="reviewer", state="done")
        st_rev = read_status(ln, armed_rev)
        assert st_rev and st_rev["role"] == "reviewer", st_rev
        assert decide(ok, other, None, True, "", "D2 REVIEW R1", 10, st_rev, armed_rev) == \
            ("DONE", ["status file done (seq 5)", "source=status"])  # no commit evidence when rx is None

        # a status file of another round -> not DONE (R27.5): the round comes from the phase form
        ln = mklane("round2")
        wstat(ln, round=2)
        assert read_status(ln, armed) is None
        assert decide(ok, good, rx, True, "", "P1 R1", 10, None, armed)[0] is None

        # updated_at before armed_at -> ignored (R27.7)
        ln = mklane("stale")
        wstat(ln, updated_at=t0 - 60)
        assert read_status(ln, armed) is None
        assert decide(ok, good, rx, True, "", "P1 R1", 10, None, armed)[0] is None

        # v=99 -> the whole file is ignored (R39); the pane-text path still works (source=pane)
        ln = mklane("v99")
        wstat(ln, v=99, state="done")
        assert read_status(ln, armed) is None
        assert decide(ok, good, rx, True, "all gates green\n⏺ P1 R1 DONE\n", "P1 R1", 10, None, armed) == \
            ("DONE", [f"commit {good}", "P1 R1 DONE", "source=pane"])

        # blocked file -> BLOCKED with its question, source=status (R32)
        ln = mklane("blocked")
        wstat(ln, state="blocked", seq=6, question="Which fixture format is the acceptance target?")
        st = read_status(ln, armed)
        assert st and st["state"] == "blocked", st
        assert decide(ok, other, rx, True, "", "P1 R1", 10, st, armed) == \
            ("BLOCKED", ["status file blocked: Which fixture format is the acceptance target?", "source=status"])

        # subagent heartbeat 14 min old with stall_min 15 -> no STALL; 16 min old -> STALL (R33/R34)
        now = time.time()
        st14 = {"v": 1, "seq": 9, "run": "r1", "task": "P1", "round": 1, "role": "builder",
                "state": "subagent", "updated_at": now - 14 * 60,
                "subagent": {"task": "S1.2", "since": now - 14 * 60}}
        h16 = {**ok, "agent_status": "working", "last_change_min": 16}
        assert decide(h16, other, rx, True, "", "P1 R1", 15, st14, armed) == (None, [])  # a heartbeat is activity
        st16 = {**st14, "updated_at": now - 16 * 60, "subagent": {"task": "S1.2", "since": now - 16 * 60}}
        st, ev = decide(h16, other, rx, True, "", "P1 R1", 15, st16, armed)
        assert st == "STALL" and ev[-1] == "source=pane", (st, ev)  # stale heartbeat: the clock ran out
        # R34: a subagent run past max_wait_min of the WATCH's armed_at is not activity either
        armed_max = {**armed, "armed_at": now - 31 * 60}
        assert status_activity(st14, 15, armed_max) is False

        # QUOTA in the pane with a done file -> DONE, finished wins (R30 first branch; R31 row 1)
        ln = mklane("quota")
        wstat(ln, state="done")
        st = read_status(ln, armed)
        assert st and st["state"] == "done", st
        stq, evq = decide({**ok, "state": "QUOTA", "evidence": ["x"]}, good, rx, True, "P1 R1 DONE",
                          "P1 R1", 10, st, armed)
        assert stq == "DONE" and evq[-1] == "source=status", (stq, evq)

        # corrupt JSON -> the pane-text path, no exception (R29)
        ln = mklane("corrupt")
        with open(os.path.join(ln, STATUS_FILE), "wb") as f:
            f.write(b"not json{{{")
        assert read_status(ln, armed) is None
        assert decide(ok, good, rx, True, "⏺ P1 R1 DONE\n", "P1 R1", 10, None, armed) == \
            ("DONE", [f"commit {good}", "P1 R1 DONE", "source=pane"])

        # grace DONE -> source=grace (the no-token rule, TOKEN_GRACE_MIN)
        st, ev = decide({**ok, "last_change_min": 4}, good, rx, True, "", "P1 R1", 10, None, armed)
        assert st == "DONE" and ev == [f"commit {good}",
                                       "no P1 R1 DONE token (commit + report, agent idle 4 min)",
                                       "source=grace"], (st, ev)

        # R27.9: seq is strictly monotonic per process and identity — a rewound or replayed file is stale
        ln = mklane("mono")
        wstat(ln, seq=5)
        st = read_status(ln, armed)
        assert st and st["seq"] == 5
        assert read_status(ln, armed) is None            # the SAME file again: not strictly greater
        wstat(ln, seq=3)
        assert read_status(ln, armed) is None            # rewound: never the newer truth
        wstat(ln, seq=6)
        st = read_status(ln, armed)
        assert st and st["seq"] == 6                      # a new write is accepted
        armed_r2 = {**armed, "phase": "P1 R2"}
        wstat(ln, seq=1, round=2)
        st = read_status(ln, armed_r2)
        assert st and st["seq"] == 1                      # new identity: no comparison

        # R27.5/R27.6: the round and the reviewer role come from the phase form, and nothing else
        ln = mklane("role-rev")
        wstat(ln, task="D2", role="builder")
        assert read_status(ln, armed_rev) is None        # a REVIEW phase accepts role=reviewer only
        ln = mklane("role-builder")
        wstat(ln, role="reviewer")
        assert read_status(ln, armed) is None           # a plain phase accepts role=builder only
        ln = mklane("nophase")
        wstat(ln)
        assert read_status(ln, {**armed, "phase": "P1"}) is None   # no R<n> tail: no round, no acceptance
        assert read_status(ln, {**armed, "phase": "P2 R1"}) is None  # group 1 must equal the watch's task
        ln = mklane("otherrun")
        wstat(ln, run="r2")
        assert read_status(ln, armed) is None            # R27.3: another run's file is not ours

        # R38: a reader ignores every field it does not know — never rejects for an unknown field
        ln = mklane("extra")
        wstat(ln, proto="mk-builder-status/v1", msg_id=None, last_read_id="b7")
        assert read_status(ln, armed) is not None
    finally:
        shutil.rmtree(stmp, ignore_errors=True)

    # record(): writes status + transition event through mk-state, in a temp data dir
    import shutil, tempfile
    tmp = tempfile.mkdtemp(prefix="lane-watch-test-")
    watch_home = os.path.join(tmp, "home")
    os.environ["MK_HOME"] = watch_home
    os.environ["MATRIXKODER_HOME"] = watch_home
    os.environ["MK_SELFTEST"] = "1"
    data = os.path.join(tmp, "data")
    restore_data = mk_config.pin_tmp_data(tmp)  # cfg() resolves the data dir: keep it in temp
    try:
        def mks(*args):
            r = subprocess.run([sys.executable, MK_STATE, *args, "--data", data], capture_output=True, text=True)
            assert r.returncode == 0, r.stdout + r.stderr
        mks("run", "init", "--id", "r1", "--feature", "F", "--branch", "b")
        mks("task", "add", "--id", "P1", "--title", "State core")
        mks("task", "set", "P1", "status=building")
        r = record({"state": "STALL", "commit": other, "evidence": ["agent idle 11 min"]}, "P1", data)
        assert r["ok"] and r["task"]["status"] == "stalled", r
        r = record({"state": "DONE", "commit": good, "evidence": ["commit x"]}, "P1", data)
        assert r["task"]["status"] == "verifying" and r["task"]["last_commit"] == "abc123"
        r = record({"state": "WATCH_EXPIRED", "commit": good}, "P1", data)
        assert r["ok"] and r["event"]["type"] == "note"
        evs = [json.loads(x) for x in open(os.path.join(data, "runs", "r1", "events.jsonl"))]
        tr = [(e["from"], e["to"], e["by"]) for e in evs if e["type"] == "transition" and e["from"]]
        assert ("building", "stalled", "lane-watch") in tr and ("stalled", "verifying", "lane-watch") in tr
        assert "lane-watch STALL: agent idle 11 min" in [e["text"] for e in evs]
        assert record({"state": "DONE", "commit": good}, "NOPE", data)["ok"] is False
        # late watcher: a task already in review keeps its status, the exit is only noted
        mks("task", "set", "P1", "status=review")
        r = record({"state": "STALL", "commit": other, "evidence": ["late"]}, "P1", data)
        assert r["ok"] and "event" in r and "status review kept" in r["event"]["text"]
        assert json.load(open(os.path.join(data, "runs", "r1", "run.json")))["tasks"][0]["status"] == "review"

        # a parent task: mk-state refuses status= on it ("status rolls up from children"), so the
        # verdict goes to every child, never the parent (which only rolls up)
        mks("task", "add", "--id", "P2", "--title", "group")
        for c in ("C1", "C2", "C3"):
            mks("task", "add", "--id", c, "--title", c, "--parent", "P2")
            mks("task", "set", c, "status=building")

        def statuses():
            run_ = json.load(open(os.path.join(data, "runs", "r1", "run.json")))
            return {t["id"]: t for t in run_["tasks"]}

        r = record({"state": "DONE", "commit": good, "evidence": ["commit x"]}, "P2", data)
        assert r["ok"] and r["children"] == ["C1", "C2", "C3"], r
        st = statuses()
        assert [st[c]["status"] for c in ("C1", "C2", "C3")] == ["verifying"] * 3, st
        assert st["C1"]["last_commit"] == "abc123" and st["P2"]["status"] == "verifying", st
        for c in ("C1", "C2", "C3"):  # QUOTA / STALL / BLOCKED move all children the same way
            mks("task", "set", c, "status=building")
        r = record({"state": "QUOTA", "evidence": ["usage limit"]}, "P2", data)
        assert r["ok"], r
        st = statuses()
        assert [st[c]["status"] for c in ("C1", "C2", "C3")] == ["quota"] * 3 and st["P2"]["status"] == "quota", st
        # a child past the watched set keeps its status (late watcher, per child); the others move
        for c, s in (("C1", "building"), ("C2", "review"), ("C3", "building")):
            mks("task", "set", c, f"status={s}")
        r = record({"state": "STALL", "commit": other, "evidence": ["agent idle 11 min"]}, "P2", data)
        assert r["ok"], r
        st = statuses()
        assert (st["C1"]["status"], st["C2"]["status"], st["C3"]["status"]) == \
            ("stalled", "review", "stalled"), st
        evs2 = [json.loads(x) for x in open(os.path.join(data, "runs", "r1", "events.jsonl"))]
        assert any(e.get("task") == "C2" and "status review kept" in e.get("text", "") for e in evs2)
        # nested groups: a child that is itself a parent never gets the write either — the verdict
        # lands on the subtree's leaves, intermediate parents roll up
        mks("task", "add", "--id", "P5", "--title", "nested")
        mks("task", "add", "--id", "C4", "--title", "mid", "--parent", "P5")
        mks("task", "add", "--id", "G1", "--title", "grandchild", "--parent", "C4")
        mks("task", "add", "--id", "C5", "--title", "leaf", "--parent", "P5")
        mks("task", "set", "G1", "status=building")
        mks("task", "set", "C5", "status=building")
        r = record({"state": "DONE", "commit": good, "evidence": ["commit x"]}, "P5", data)
        assert r["ok"] and r["children"] == ["G1", "C5"], r
        st = statuses()
        assert st["G1"]["status"] == "verifying" and st["C5"]["status"] == "verifying", st
        assert st["C4"]["status"] == "verifying" and st["P5"]["status"] == "verifying", st  # rolled up

        # a lane GROUP (--tasks QG1,QG2): one verdict reaches every listed task's children, the
        # parents only roll up — the same list one delivered dispatch marks building
        mks("task", "add", "--id", "QG1", "--title", "group one")
        for c in ("A1", "A2"):
            mks("task", "add", "--id", c, "--title", c, "--parent", "QG1")
            mks("task", "set", c, "status=building")
        mks("task", "add", "--id", "QG2", "--title", "group two")
        for c in ("B1", "B2"):
            mks("task", "add", "--id", c, "--title", c, "--parent", "QG2")
            mks("task", "set", c, "status=building")
        r = record({"state": "DONE", "commit": good, "evidence": ["commit x"]}, ["QG1", "QG2"], data)
        assert r["ok"] and r["children"] == ["A1", "A2", "B1", "B2"], r
        assert [x["task"]["status"] for x in r["results"]] == ["verifying"] * 4, r
        st = statuses()
        assert [st[c]["status"] for c in ("A1", "A2", "B1", "B2")] == ["verifying"] * 4, st
        assert st["QG1"]["status"] == "verifying" and st["QG2"]["status"] == "verifying", st
        # a leaf listed alongside a parent goes itself; a child past the watched set still keeps
        # its status, exactly as for a single --task
        for c in ("A1", "A2", "B1", "B2"):
            mks("task", "set", c, "status=building")
        mks("task", "set", "P1", "status=building")
        mks("task", "set", "B2", "status=review")
        r = record({"state": "STALL", "commit": other, "evidence": ["agent idle 11 min"]},
                   ["QG1", "P1", "QG2"], data)
        assert r["ok"] and r["children"] == ["A1", "A2", "P1", "B1", "B2"], r
        st = statuses()
        assert (st["A1"]["status"], st["B2"]["status"], st["P1"]["status"]) == ("stalled", "review", "stalled"), st
        # an unknown id in the group is reported, the healthy ones still got the verdict
        r = record({"state": "DONE", "commit": good, "evidence": ["x"]}, ["QG1", "NOPE"], data)
        assert r["ok"] is False and "NOPE" in r["error"], r

        # one child's write fails (its task.json is a directory): ok is False and the error
        # names the child — never dropped, and the healthy child still got the verdict
        mks("task", "add", "--id", "P6", "--title", "errors")
        mks("task", "add", "--id", "E1", "--title", "e1", "--parent", "P6")
        mks("task", "add", "--id", "E2", "--title", "e2", "--parent", "P6")
        mks("task", "set", "E1", "status=building")
        mks("task", "set", "E2", "status=building")
        e2json = os.path.join(data, "runs", "r1", "tasks", "E2", "task.json")
        os.unlink(e2json)
        os.makedirs(e2json)  # unwritable: the write fails, the error is reported
        r = record({"state": "DONE", "commit": good, "evidence": ["commit x"]}, "P6", data)
        assert r["ok"] is False and r["children"] == ["E1", "E2"], r
        assert r["error"].startswith("E2:") and "IsADirectoryError" in r["error"], r
        st = statuses()
        assert st["E1"]["status"] == "verifying", st  # the healthy child still got the verdict
        assert st["E2"]["status"] == "building", st  # the failing child kept its status

        # CLI end to end with a fake herdr on PATH and a real git lane
        bin_ = os.path.join(tmp, "bin")
        os.makedirs(bin_)
        pane = os.path.join(tmp, "pane.txt")
        with open(os.path.join(bin_, "herdr"), "w") as f:
            f.write('#!/bin/sh\nif [ "$2" = get ]; then echo \'{"result":{"agent":{"agent_status":"idle"}}}\'; '
                    f'else cat "{pane}"; fi\n')
        os.chmod(os.path.join(bin_, "herdr"), 0o755)
        env = dict(os.environ, PATH=bin_ + os.pathsep + os.environ["PATH"])
        lane = os.path.join(tmp, "lane")
        git = ["git", "-C", lane, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", lane], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "feat(p1): state core"], check=True)
        mks("task", "add", "--id", "P3", "--title", "cli")
        mks("task", "set", "P3", "status=building")

        def cli(*args, code=0):
            # env was snapshotted before the store existed; the child must see the CURRENT
            # temp home under both names or its store reads look at the real home
            e = dict(env, MATRIXKODER_HOME=os.environ["MATRIXKODER_HOME"],
                     MK_HOME=os.environ["MATRIXKODER_HOME"], MK_SELFTEST="1")
            r = subprocess.run([sys.executable, __file__, "--lane", lane, "--agent", "w1", *args], env=e,
                               capture_output=True, text=True)
            assert r.returncode == code, r.stdout + r.stderr
            return json.loads(r.stdout.strip().splitlines()[-1]) if code == 0 else r
        open(pane, "w").write("old: Usage limit reached\n> Read and follow brief-r1.md exactly.\nall green\n⏺ P1 DONE\n")
        out = cli("--commit-grep", r"feat\(p1\)", "--phase", "P1", "--kind", "codex", "--prompt-marker",
                  "Read and follow brief-r1.md", "--poll", "0.1", "--task", "P3", "--data", data)
        assert out["state"] == "DONE" and out["state_written"] is True, out
        assert json.load(open(os.path.join(data, "runs", "r1", "run.json")))["tasks"][-1]["status"] == "verifying"
        # CLI end to end on a parent: the verdict reaches its children through the same record()
        mks("task", "add", "--id", "P4", "--title", "cli group")
        for c in ("D1", "D2", "D3"):
            mks("task", "add", "--id", c, "--title", c, "--parent", "P4")
            mks("task", "set", c, "status=building")
        out = cli("--commit-grep", r"feat\(p1\)", "--phase", "P1", "--kind", "codex", "--prompt-marker",
                  "Read and follow brief-r1.md", "--poll", "0.1", "--task", "P4", "--data", data)
        assert out["state"] == "DONE" and out["state_written"] is True, out
        st = statuses()
        assert [st[c]["status"] for c in ("D1", "D2", "D3")] == ["verifying"] * 3, st
        assert st["P4"]["status"] == "verifying", st
        # --tasks A,B end to end: one pane, one verdict, both groups' children
        open(pane, "w").write("> Read and follow brief-r1.md\nall gates green\n⏺ P1 DONE\n")
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "feat(p1): group cli"], check=True)
        for c in ("A1", "A2", "B1", "B2"):
            mks("task", "set", c, "status=building")
        out = cli("--commit-grep", r"feat\(p1\)", "--phase", "P1", "--kind", "codex", "--prompt-marker",
                  "Read and follow brief-r1.md", "--poll", "0.1", "--tasks", "QG1,QG2", "--data", data)
        assert out["state"] == "DONE" and out["state_written"] is True, out
        st = statuses()
        assert [st[c]["status"] for c in ("A1", "A2", "B1", "B2")] == ["verifying"] * 4, st
        assert cli("--commit-grep", "x", "--tasks", "QG1,QG2", code=1).returncode == 1  # --tasks without --data
        cli("--commit-grep", "x", "--poll", "0.1", "--task", "P3", "--tasks", "QG1", "--data", data, code=1)
        out = cli("--commit-grep", "(", "--poll", "0.1", "--task", "P3", "--data", data)  # bad regex → ERROR
        assert out["state"] == "ERROR" and "missing )" in out["evidence"][0] and out["state_written"] is True, out
        assert cli("--commit-grep", "x", "--task", "P3", code=1).returncode == 1  # --task without --data
        # --no-commit (a reviewer pane): --commit-grep is not required and the commit subject is irrelevant
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "docs: merge lane notes"], check=True)
        open(pane, "w").write("> Read and follow review-brief-r1.md\nreport written, gates green\n⏺ D2 REVIEW R1 DONE\n")
        open(os.path.join(lane, "review-r1.md"), "w").write("# review r1\nship it\n")
        out = cli("--no-commit", "--report", "review-r1.md", "--phase", "D2 REVIEW R1", "--kind", "codex",
                  "--prompt-marker", "Read and follow review-brief-r1.md", "--poll", "0.1",
                  "--task", "P3", "--data", data)
        assert out["state"] == "DONE" and out["evidence"] == ["D2 REVIEW R1 DONE", "source=pane"], out
        assert out["state_written"] is True, out
        # without --no-commit, --commit-grep stays required: usage error, exit 2
        r = subprocess.run([sys.executable, __file__, "--lane", lane, "--agent", "w1", "--report", "review-r1.md",
                            "--phase", "D2 REVIEW R1"], env=env, capture_output=True, text=True)
        assert r.returncode == 2 and "Usage" in r.stderr, (r.returncode, r.stderr[:200])
        # working agent, no file/commit change: a pane that keeps moving is never STALL (lesson 37); a frozen one is
        work_bin = os.path.join(tmp, "workbin")
        os.makedirs(work_bin)
        grow = os.path.join(tmp, "grow.txt")
        cnt = os.path.join(tmp, "cnt.txt")
        open(grow, "w").write("")
        with open(os.path.join(work_bin, "herdr"), "w") as f:
            f.write('#!/bin/sh\nif [ "$2" = get ]; then echo \'{"result":{"agent":{"agent_status":"working"}}}\'; '
                    # like real herdr: a working full-screen agent refuses history reads; only --source visible
                    'elif [ "$5" != visible ]; then echo \'{"error":{"code":"agent_not_idle"}}\'; '
                    f'else echo "> Read and follow brief-r3.md"; cat "{grow}"; '
                    f'if [ -n "$MK_GROW" ]; then echo thinking >> "{grow}"; fi; '
                    # MK_COUNT: only the ↳ N toolcalls counter moves between polls, nothing else does
                    f'if [ -n "$MK_COUNT" ]; then n=$(cat "{cnt}" 2>/dev/null || echo 49); '
                    f'echo "↳ $n toolcalls · 12m 48s"; echo $((n + 1)) > "{cnt}"; fi; fi\n')
        os.chmod(os.path.join(work_bin, "herdr"), 0o755)
        for grows, want in ((True, "WATCH_EXPIRED"), (False, "STALL")):
            e2 = dict(env, PATH=work_bin + os.pathsep + os.environ["PATH"], **({"MK_GROW": "1"} if grows else {}))
            # the wider arguments only differ once the spawn scale passes ~4; below that
            # win() returns the same 0.25/0.75 floor for both. The frozen pane keeps the
            # smaller arguments so a stall still fires inside its window.
            stall, wait = win(0.06, 0.18) if grows else win(0.02, 0.06)
            r = subprocess.run([sys.executable, __file__, "--lane", lane, "--agent", "w1", "--commit-grep", "nomatch",
                                "--prompt-marker", "Read and follow brief-r3.md", "--poll", "0.1",
                                "--stall-min", stall, "--max-wait-min", wait], env=e2, capture_output=True, text=True)
            out = json.loads(r.stdout.strip().splitlines()[-1])
            assert out["state"] == want, (grows, out)

        # D5A.3: a quiet pane whose process tree runs a test/build command is not STALL until
        # stall_min * scale after that child exits. The child is this interpreter with --self-test
        # on its command line (CHILD_PROGRESS_RE); it is the pane pid. Never a server.
        active0, scale0 = child_progress(None)
        assert active0 is False and scale0 >= 1.0, (active0, scale0)
        # a descendant running a matching command is seen; the pane pid itself is not.
        # The watch-level suppression under load is proven by child_grace_end on an
        # injected clock (below), not by this wall-clock watch: ps under heavy load
        # outruns any fixed window.
        script = os.path.join(lane, "selftest.sh")
        wrap = os.path.join(lane, "wrap.py")
        open(script, "w").write("import time\ntime.sleep(30)\n")
        open(wrap, "w").write("import subprocess,sys\nsubprocess.run([sys.executable, sys.argv[1]])\n")
        child = subprocess.Popen([sys.executable, wrap, script])
        try:
            time.sleep(0.3)
            assert child_progress(child.pid)[0] is True, (child.pid, child.poll())
        finally:
            child.kill()
            child.wait(timeout=5)
        assert child_progress(child.pid)[0] is False, child.pid

        # grace elapsed with the child: this watch is a fresh process (child_seen_until unset)
        r = subprocess.run([sys.executable, __file__, "--lane", lane, "--agent", "w1",
                            "--commit-grep", "nomatch", "--prompt-marker", "Read and follow brief-r3.md",
                            "--poll", "0.1", "--stall-min", win(0.02, 0.06)[0], "--max-wait-min", win(0.02, 0.06)[1]],
                           env=dict(env, PATH=work_bin + os.pathsep + os.environ["PATH"]),
                           capture_output=True, text=True)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        assert out["state"] == "STALL", out  # child gone, grace elapsed: pane-text STALL as before

        _stall, _wait = win(0.02, 0.06)

        def probe(pane_txt, want, stall=_stall, wait=_wait, **extra):
            """One watch run against the working-agent fake; assert its exit state."""
            open(grow, "w").write(pane_txt)
            e3 = dict(env, PATH=work_bin + os.pathsep + os.environ["PATH"], **extra)
            r = subprocess.run([sys.executable, __file__, "--lane", lane, "--agent", "w1", "--commit-grep", "nomatch",
                                "--prompt-marker", "Read and follow brief-r3.md", "--poll", "0.1",
                                "--stall-min", stall, "--max-wait-min", wait], env=e3, capture_output=True, text=True)
            out = json.loads(r.stdout.strip().splitlines()[-1])
            assert out["state"] == want, (pane_txt or extra, out)

        # tonight's false STALL: a pane whose only life is a running subagent task line (spinner +
        # "Task —", text frozen between polls so the fingerprint never moves) is activity; a pane
        # with only finished "✓ … Task —" lines is not; a lone ticking ↳ N toolcalls counter is
        # the task line counts only once a poll has seen it. Its arguments are larger, but
        # below scale ~4 win() floors both to the same 0.25/0.75, so the window is only
        # actually wider under load.
        probe("⠼ General Task — E2 reviewer key, quiet optional\n↳ 49 toolcalls · 12m 48s\n", "WATCH_EXPIRED",
              *win(0.06, 0.18))
        probe("✓ General Task — E2 reviewer key\n", "STALL")
        probe("", "WATCH_EXPIRED", *win(0.06, 0.18), MK_COUNT="1")
        # lane-health CLI: QUOTA keyed by kind into $MATRIXKODER_HOME/usage.json, scope reported
        open(pane, "w").write("> Read and follow brief-r2.md\n■ You've hit your usage limit. Try again in 2 hours.\n")
        r = subprocess.run([sys.executable, os.path.join(HERE, "lane-health.py"), "--agent", "w1", "--lane", lane,
                            "--kind", "codex", "--prompt-marker", "Read and follow brief-r2.md"],
                           env=env, capture_output=True, text=True)
        h = json.loads(r.stdout)
        assert h["state"] == "QUOTA" and h["scope"] == "marker" and h["agent_status"] == "idle", h
        led = json.load(open(os.path.join(tmp, "home", "usage.json")))["agents"]["codex"]
        assert led["exhausted_window"] == "5h" and led["exhausted_guess"] is False
        # J1.3: quota beats STALL — tonight's OpenCode line sits above the marker's last echo (the
        # agent re-printed its prompt), so the marker-scoped text cannot see it: STALL is never
        # decided until the pane FOOTER (K1.1) went through lane-health's quota detection.
        mks("task", "set", "P3", "status=building")
        open(pane, "w").write(
            "> Read and follow brief-r2.md\n"
            "Free usage exceeded, subscribe to Go [retrying in 22h 40m attempt #1]\n"
            "$ cat brief-r2.md\n"
            "Read and follow brief-r2.md exactly. Work autonomously until complete.\n")
        out = cli("--commit-grep", "nomatch", "--kind", "opencode", "--prompt-marker",
                  "Read and follow brief-r2.md", "--poll", "0.1", "--stall-min", win(0.03, 0.06)[0],
                  "--max-wait-min", win(0.03, 0.06)[1], "--task", "P3", "--data", data)
        assert out["state"] == "QUOTA", out  # not STALL: the banner above the echo still counts
        assert "Free usage exceeded" in out["evidence"][0], out
        assert any(e.startswith("exhausted_until ") for e in out["evidence"]), out
        assert out["state_written"] is True, out
        assert statuses()["P3"]["status"] == "quota", statuses()
        ocled = json.load(open(os.path.join(tmp, "home", "usage.json")))["agents"]["opencode"]
        assert ocled["exhausted_source"] == "pane-text" and ocled["exhausted_window"] == "weekly", ocled
        assert ocled["exhausted_guess"] is False, ocled
        # K1.1: the same phrase in the TRANSCRIPT (the agent quoting a banner — a brief line, a
        # test fixture) with an ordinary footer below it is not a banner: no QUOTA, no re-record.
        # The marker echo at the end keeps it out of the poll's scoped text too, so decide()
        # itself says STALL and only the footer check could (wrongly) override it.
        oc_ev = ocled["exhausted_observed_at"]
        open(pane, "w").write(
            "> Read and follow brief-r2.md\n"
            "Free usage exceeded, subscribe to Go [retrying in 22h 40m attempt #1]\n"
            "$ pytest tests/ -q\n" + "".join(f"tests/test_{i}.py ....\n" for i in range(1, 11)) +
            "10 passed in 4.21s\n$ cat brief-r2.md\n"
            "Read and follow brief-r2.md exactly. Work autonomously until complete.\n")
        mks("task", "set", "P3", "status=building")
        out = cli("--commit-grep", "nomatch", "--kind", "opencode", "--prompt-marker",
                  "Read and follow brief-r2.md", "--poll", "0.1", "--stall-min", win(0.03, 0.06)[0],
                  "--max-wait-min", win(0.03, 0.06)[1], "--task", "P3", "--data", data)
        assert out["state"] == "STALL" and out["evidence"][0].startswith("agent idle"), out
        assert json.load(open(os.path.join(tmp, "home", "usage.json")))["agents"]["opencode"][
            "exhausted_observed_at"] == oc_ev, "a quoted banner must not re-record the ledger"
        # without a quota line on screen the STALL path is unchanged (no override, no ledger entry)
        oc_ev = ocled["exhausted_observed_at"]
        open(pane, "w").write("> Read and follow brief-r2.md\nworking, then silence\n")
        mks("task", "set", "P3", "status=building")
        out = cli("--commit-grep", "nomatch", "--kind", "opencode", "--prompt-marker",
                  "Read and follow brief-r2.md", "--poll", "0.1", "--stall-min", win(0.03, 0.06)[0],
                  "--max-wait-min", win(0.03, 0.06)[1], "--task", "P3", "--data", data)
        assert out["state"] == "STALL" and out["evidence"][0].startswith("agent idle"), out
        assert json.load(open(os.path.join(tmp, "home", "usage.json")))["agents"]["opencode"][
            "exhausted_observed_at"] == oc_ev  # untouched: no quota line, no re-record

        # --- the project store (D5A.1): reads go through mkdb; files are the fallback --------
        # self-hosting guard: every run above was the file path; none of them created a store
        assert not os.path.exists(os.path.join(data, "mk.db")), "a watch created mk.db"
        out = cli("--commit-grep", "nomatch", "--poll", "0.1", "--stall-min", win(0.02, 0.06)[0],
                  "--max-wait-min", win(0.02, 0.06)[1], "--task", "P3", "--data", data)
        assert out["state"] == "STALL" and out["state_written"], out  # file path, still works
        assert not os.path.exists(os.path.join(data, "mk.db")), "file-path run created mk.db"
        # undo the E2 fixture (task.json is a directory) BEFORE enabling: store enable republishes
        # every view, and a directory there fails the publish
        shutil.rmtree(os.path.join(data, "runs", "r1", "tasks", "E2"), ignore_errors=True)
        os.makedirs(os.path.join(data, "runs", "r1", "tasks", "E2"))
        r = subprocess.run([sys.executable, MK_STATE, "store", "enable", "--data", data],
                           capture_output=True, text=True)
        assert r.returncode == 0 and os.path.getsize(os.path.join(data, "mk.db")) > 0, (r.stdout, r.stderr, os.listdir(data))

        # store-first: no status file in the lane, the tasks row IS the channel -> DONE, source=store
        mks("task", "add", "--id", "S4", "--title", "store")
        mks("task", "add", "--id", "SK1", "--title", "k1", "--parent", "S4")  # child_tasks tree
        mks("task", "set", "SK1", "status=building")
        # the report lives in the TASK dir: task round stores whatever report-r<n>.md is there
        td = os.path.join(data, "runs", "r1", "tasks", "S4")
        open(os.path.join(td, "report-r1.md"), "w").write("# report r1\nstore test\n")
        mks("task", "round", "S4", "--kind", "build")   # round 1 + report artifact stored
        os.unlink(os.path.join(td, "report-r1.md"))     # the file is GONE: only the artifact remains
        _c = store_con(data)
        _arts = [dict(r) for r in _c.execute("SELECT task, round, kind FROM artifacts")]
        _c.close()
        assert any(a["task"] == "S4" and a["kind"] == "report" and a["round"] == 1 for a in _arts), _arts
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "feat(s4): store read"], check=True)
        mks("task", "set", "S4", "status=verifying")    # the row a finished builder leaves behind
        assert not os.path.exists(os.path.join(lane, STATUS_FILE))
        _armed = {"run": "r1", "task": "S4", "phase": "S4 R1", "data": data}
        assert store_on(data), data
        assert store_report_ok(data, _armed, "report-s1.md") is True, "report artifact not seen"
        _row = store_status_row(data, _armed)
        assert _row and _row["status"] == "verifying" and _row["round"] == 1, dict(_row) if _row else None
        open(pane, "w").write("> Read and follow brief-s1.md\nquiet, no token\n")
        out = cli("--commit-grep", r"feat\(s4\)", "--report", "report-s1.md", "--phase", "S4 R1", "--run", "r1",
                  "--kind", "codex", "--prompt-marker", "Read and follow brief-s1.md",
                  "--poll", "0.1", "--task", "S4", "--data", data)
        assert out["state"] == "DONE" and out["state_written"], out
        assert out["evidence"][-1] == "source=store" and "store row done" in out["evidence"], out
        # the verdict reached the CHILD through the store tree (child_tasks read tasks.parent)
        assert statuses()["SK1"]["status"] == "verifying", statuses()
        # DONE still needs the commit: no matching commit -> never DONE
        mks("task", "add", "--id", "S5", "--title", "store2")
        mks("task", "set", "S5", "status=building")
        mks("task", "round", "S5", "--kind", "build")
        mks("task", "set", "S5", "status=verifying")
        open(pane, "w").write("> Read and follow brief-s1.md\nquiet, no token\n")
        out = cli("--commit-grep", "nomatch", "--report", "report-s1.md", "--phase", "S5 R1", "--run", "r1",
                  "--kind", "codex", "--prompt-marker", "Read and follow brief-s1.md",
                  "--poll", "0.1", "--stall-min", win(0.02, 0.06)[0], "--max-wait-min", win(0.02, 0.06)[1],
                  "--task", "S5", "--data", data)
        assert out["state"] == "STALL" and "matching commit" in out["evidence"][0], out
        # ... and no report artifact for the round -> the file check fails -> STALL lists report
        mks("task", "add", "--id", "S7", "--title", "store3")
        mks("task", "set", "S7", "status=building")
        mks("task", "round", "S7", "--kind", "build")   # round file never existed: no artifact
        mks("task", "set", "S7", "status=verifying")
        out = cli("--commit-grep", r"feat\(s4\)", "--report", "report-s1.md", "--phase", "S7 R1", "--run", "r1",
                  "--kind", "codex", "--prompt-marker", "Read and follow brief-s1.md",
                  "--poll", "0.1", "--stall-min", win(0.02, 0.06)[0], "--max-wait-min", win(0.02, 0.06)[1],
                  "--task", "S7", "--data", data)
        assert out["state"] == "STALL" and "report" in out["evidence"][0], out
        # fallback: the row is round 2, so phase S8 R1 has no row -> the status file decides
        mks("task", "add", "--id", "S8", "--title", "fallback")
        mks("task", "set", "S8", "status=building")
        mks("task", "round", "S8", "--kind", "build")
        mks("task", "round", "S8", "--kind", "fix")     # round 2 now
        mks("task", "set", "S8", "status=verifying")
        j = {"v": 1, "seq": 3, "run": "r1", "task": "S8", "round": 1, "role": "builder",
             "agent": "opencode", "state": "done", "step": "selftest", "phases_done": [],
             "question": None, "commit": None, "report": "report-s1.md", "subagent": None,
             "ctx_pct": None, "started_at": time.time(), "updated_at": time.time() + 5}
        json.dump(j, open(os.path.join(lane, STATUS_FILE), "w"))
        open(os.path.join(lane, "report-s1.md"), "w").write("# report\n")  # file fallback: no round-1 artifact
        out = cli("--commit-grep", r"feat\(s4\)", "--report", "report-s1.md", "--phase", "S8 R1", "--run", "r1",
                  "--kind", "codex", "--prompt-marker", "Read and follow brief-s1.md",
                  "--poll", "0.1", "--task", "S8", "--data", data)
        assert out["state"] == "DONE" and out["evidence"][-1] == "source=status", out
        assert "status file done (seq 3)" in out["evidence"], out
        os.unlink(os.path.join(lane, STATUS_FILE))

        # W1: task round with the store on starts the new round non-terminal and clears verdict,
        # in the tasks row the watcher reads (the file view is unchanged on purpose).
        def w1row():
            c = store_con(data)
            try:
                return dict(c.execute(
                    "SELECT status, round, verdict FROM tasks WHERE run='r1' AND id='W1T'").fetchone())
            finally:
                c.close()
        mks("task", "add", "--id", "W1T", "--title", "round reset")
        mks("task", "set", "W1T", "status=done", "verdict=OLD")
        mks("task", "round", "W1T", "--kind", "build")
        got = w1row()
        assert got["status"] == "planned" and got["verdict"] is None and got["round"] == 1, got
        mks("task", "set", "W1T", "status=done", "verdict=OLD")
        mks("task", "round", "W1T", "--kind", "fix")
        got = w1row()
        assert got["status"] == "fix" and got["verdict"] is None and got["round"] == 2, got
        mks("task", "set", "W1T", "status=done", "verdict=OLD")
        mks("task", "round", "W1T", "--kind", "takeover")
        got = w1row()
        assert got["status"] == "building" and got["verdict"] is None and got["round"] == 3, got
        # a terminal row older than armed_at is the previous round's outcome: ignored
        mks("task", "set", "W1T", "status=done", "verdict=OLD")
        _c = store_con(data)
        _upd = _c.execute("SELECT updated FROM tasks WHERE id='W1T'").fetchone()["updated"]
        _c.close()
        stale = store_status_row(data, {"run": "r1", "task": "W1T", "phase": "W1T R3",
                                         "armed_at": _row_epoch(_upd) + 120, "data": data})
        assert stale is None, dict(stale) if stale else stale
        # a terminal row written after armed_at still decides
        fresh = store_status_row(data, {"run": "r1", "task": "W1T", "phase": "W1T R3",
                                         "armed_at": _row_epoch(_upd) - 5, "data": data})
        assert fresh and fresh["status"] == "done", dict(fresh) if fresh else fresh

        # W1: a store read failure is a named reason, never a silent None, and decide keeps watching
        dbp = os.path.join(data, "mk.db")
        os.rename(dbp, dbp + ".w1bak")
        os.mkdir(dbp)
        try:
            err = store_status_row(data, {"run": "r1", "task": "W1T", "phase": "W1T R3", "data": data})
            assert isinstance(err, dict) and str(err.get("store_error", "")).startswith("store_error") is False
            assert "store_error" in err and err["store_error"], err
            st_err = read_status(lane, {"run": "r1", "task": "W1T", "phase": "W1T R3",
                                         "armed_at": 0, "data": data})
            assert st_err["state"] == "store_error" and st_err["source"] == "status", st_err
            quiet = {"state": "OK", "evidence": [], "agent_status": "idle", "last_change_min": 30}
            dstate, dev = decide(quiet, "abc feat(p1): x", re.compile("feat"), True, "",
                                 "W1T R3", 10, st_err, {"armed_at": 0})
            assert dstate is None and dev[-1] == "source=status" and dev[0].startswith("store_error:"), dev
        finally:
            os.rmdir(dbp)
            os.rename(dbp + ".w1bak", dbp)

        # W1: the child-progress scale never exceeds 4
        _hit, _scale = child_progress(os.getpid())
        assert _scale <= CHILD_SCALE_CAP, _scale
        # the cap itself, on an injected clock (no wall time, so load cannot flake it).
        stall, mx = 15.0, 30 * 60.0
        cap = max(stall * 60 * CHILD_SCALE_CAP, mx)
        assert child_grace_end(0.0, 0.0, True, stall, 4.0, mx, cap - 1) is not None
        assert child_grace_end(0.0, 0.0, True, stall, 100.0, mx, cap + 1) is None
        assert child_grace_end(0.0, 100.0, False, stall, 4.0, mx, 100 + stall * 60 - 1) is not None
        assert child_grace_end(0.0, 100.0, False, stall, 100.0, mx, 100 + stall * 60 * CHILD_SCALE_CAP + 1) is None
        assert child_grace_end(0.0, None, False, stall, 1.0, mx, 0) is None

        # no flag and no yaml: the stall/max_wait defaults this script now uses are the schema's
        empty = os.path.join(tmp, "empty-data")
        os.makedirs(empty)
        old_md = os.environ.get("MATRIXKODER_DATA")
        os.environ["MATRIXKODER_DATA"] = empty
        try:
            assert cfg("session.stall_min") == mk_config.value_of(
                [], mk_config.row_for([], "session.stall_min"))[1], cfg("session.stall_min")
            assert cfg("session.max_wait_min") == mk_config.value_of(
                [], mk_config.row_for([], "session.max_wait_min"))[1]
            assert cfg("session.stall_min", "9") == "9"  # a given flag wins
        finally:
            if old_md is None:
                os.environ.pop("MATRIXKODER_DATA", None)
            else:
                os.environ["MATRIXKODER_DATA"] = old_md
    finally:
        restore_data()
        shutil.rmtree(tmp, ignore_errors=True)
    # A2: the filter is what suppresses it, so the test drives quota_beats_stall itself.
    # A quota phrase inside a bar-drawn command in the footer must not beat a stall;
    # a real wall in the footer must. Removing the banner_lines line fails the first assert.
    log = "\n".join(["  \u2503", "  \u2503  $ pytest",
                      "  \u2503  Usage limit reached for 5 hour resets in 3h", "  \u2503"])
    wall = "Usage limit reached for 5 hour. resets in 3h"
    real_pane = lh.pane_text
    try:
        lh.pane_text = lambda agent, lines=300: log
        assert quota_beats_stall("w1", "opencode") == (None, []), "a test log must not beat a stall"
        lh.pane_text = lambda agent, lines=300: wall
        state, ev = quota_beats_stall("w1", "opencode")
        assert state == "QUOTA", (state, ev)
    finally:
        lh.pane_text = real_pane

    # R1.2: the mkdb write guard refuses the real home from this suite too.
    _load_mkdb().selftest_guard_probe(watch_home)
    print("lane-watch self-test ok")


def main():
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    if not (opt(a, "--lane") and opt(a, "--agent")) or ("--no-commit" not in a and not opt(a, "--commit-grep")):
        sys.stderr.write(__doc__)  # usage: --commit-grep is required unless --no-commit (a reviewer pane)
        sys.exit(2)
    if opt(a, "--kind") and opt(a, "--kind") not in lh.KINDS:
        sys.exit(__doc__)
    task, tasks, data = opt(a, "--task"), opt(a, "--tasks"), opt(a, "--data")
    if task and tasks:
        sys.exit("--task and --tasks are two spellings of the same thing; pass one\n" + __doc__)
    if bool(task or tasks) != bool(data):
        sys.exit("--task and --data go together (state is written only with both)\n" + __doc__)
    try:
        out = watch(a)
    except Exception as e:  # never die silently: missing herdr/git, bad regex, bad number → ERROR + state note
        out = {"state": "ERROR", "evidence": [f"{type(e).__name__}: {e}"[:200], "source=pane"]}   # R41
    if task or tasks:
        r = record(out, task or [t.strip() for t in tasks.split(",") if t.strip()], data, opt(a, "--run"))
        out["state_written"] = r.get("ok", False)
        if not r.get("ok"):
            out["state_error"] = r.get("error")
    print(json.dumps(out))


if __name__ == "__main__":
    main()
