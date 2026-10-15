#!/usr/bin/env python3
"""herdr pushes lane events; this script turns them into MatrixKoder verdicts (event-driven lanes).

Usage:
  mk-lane-event.py arm --lane DIR --pane ID --task T --run R --data DIR --phase P --commit-grep RX
                      [--report PATH] [--kind claude|codex|opencode|agy|omp] [--prompt-marker TEXT] [--stall-min N]
  mk-lane-event.py arm --lane DIR --pane ID --tasks A,B --run R --data DIR --phase P --commit-grep RX
                      [...]   # a shared lane group: one lane serving several phases of one plan
  mk-lane-event.py event                (herdr plugin hook: reads HERDR_PLUGIN_EVENT + HERDR_PLUGIN_EVENT_JSON)
  mk-lane-event.py dispatch --pane ID --text TEXT [--timeout-ms 25000] [--role build|review]
                             [--kind build|fix|takeover|rework] [--connect|--no-connect]
                             [--task T] [--agent A] [--model M] [--effort E] [--force]
  mk-lane-event.py sweep [--data DIR]
  mk-lane-event.py disarm --lane DIR
  mk-lane-event.py review-handoff --task T [--run R] [--data DIR] [--hold] [--min medium]
  mk-lane-event.py --self-test

arm writes <lane>/.matrixkoder-watch.json and records the pane on the task (`mk-state task set T
pane=<pane>`, best-effort: a task missing from run state still arms). When yaml bus.enabled is true
(the mk-config schema default) arm also runs mk-bus.py ensure then token for the primary task (the
token under the MK home, not in the lane; its url reported as bus:{ok,url}) and disarm runs mk-bus.py revoke —
a bus failure rides in the reply as bus:{ok:false,error} and never fails the command ($MK_BUS_CMD
overrides the script; bus.enabled:false runs no bus command at all). event is run by the herdr plugin
for EVERY pane status
change / exit in every workspace; it finds the armed lane for that pane (worktrees of the repos in
$MATRIXKODER_HOME/projects.json, default ~/.matrixkoder) and exits fast with {"ignored": ...} when
there is none. `working` only refreshes last_activity. `done`/`idle`/`blocked` run the same verdict
one lane-watch poll would make (commit subject + report + "<PHASE> DONE" token + lane-health check →
lane-watch's decide(), imported, never copied); a verdict is written to run state via lane-watch's
record() (which never moves a task past WATCHED), the pane gets a herdr sidebar label
(pane.report_metadata) and a toast (notification.show) — both best-effort — and `verdict`/`verdict_at`
land in the watch file so a repeated identical event is a no-op. pane.exited on an armed pane →
STALL "agent exited"; after any verdict an exit is just post-finish cleanup. A herdr ERROR probe is
not a verdict (the next event retries). Silence (crash, hang) produces no event at all — `sweep` is
the backstop: no file/commit/pane change for stall_min since last_activity → STALL by the same rules.
A build dispatch to an armed lane runs EVERY guard BEFORE anything is published, written or typed
(todo 130/R2): the task/slot mismatch refusal (below), the route write and the mandatory-field
check all run first, so a refused dispatch leaves the bus and the lane exactly as they were — it
publishes no brief to the bus, writes no <lane>.matrixkoder-brief.md and types no prompt. Order:
mismatch refusal, then (D1.5.3/D1.5.4) --agent/--model/--effort write the route to every armed task
via `mk-state task set <T> agent=.. model=.. effort=..` (one call per task, --by dispatch) —
without --agent the armed watch's kind is the agent and model/effort stay empty — and a
`mk-state task check <T>` on the lead task gates the dispatch: a missing mandatory field
(title/acceptance/parent-unless-phase/owned-when-briefed/agent-at-dispatch) refuses it
(ok:false, delivered:false, nothing published or typed) unless --force sends and the reply says
the check was forced. Only after the guards pass does the doorbell/publish run, and it publishes
the DISPATCHED task's brief — --task when it names a member of the armed group, else the lead
(todo 130/HIGH: a group lane's --task B must publish B's brief, never always the lead's).
todo 130 (group lanes): the mismatch refusal compares against the whole ARMED GROUP
(watch_tasks(w) = [A, B]) and the slot's recorded group (mk-lane.py acquire records "A,B"), so a
group lane dispatches with no --task or with --task <member>; only a --task outside the group, or
a slot whose acquire recorded a different group, is refused. A mk-state failure rides in the reply
as task_route_error / task_check_error and never fails the delivery verdict; every reply carries
the recorded route. --role review and unarmed panes skip all of it (status_unchanged covers review).
dispatch is the one-call prompt helper: `herdr agent prompt <pane> <text> --wait --timeout <ms>`
(binary from $MK_HERDR, default herdr). A timeout proves nothing either way — the text may already
be in flight — so the pane's own state decides delivery: pane list still `working` → delivered:true;
anything else (agent_prompt_stalled, idle, gone, list unreadable) → delivered:false with the observed
state and herdr's own error. The reply carries exactly what the subprocesses returned, never invented.
When the first attempt is not delivered while the pane just sits idle/done (the prompt was probably
lost — once seen on a pane that was `done` all along), dispatch waits DISPATCH_RETRY_S
($MK_DISPATCH_RETRY_S, default 5 s) and sends the prompt ONE more time; that second attempt decides
the verdict. When even that leaves the pane idle/done and about to be reported not delivered,
dispatch takes ONE last look after DISPATCH_RECHECK_S ($MK_DISPATCH_RECHECK_S, default 3 s): a fresh
pane list plus the pane text (lane-health's read, visible-screen fallback) — the prompt marker (the
first words of --text) found there means the prompt had landed after all, so delivered:true with
recovered:true. Every dispatch reply also carries attempts (1 or 2), herdr_error (herdr's words for the
attempt that did not deliver — kept even when a retry lands, the final attempt's when both fail) and
pane_entry (the freshest raw `herdr pane list` row for the pane, null when no observation names it).
A delivered build prompt (--role build, the default) also re-arms the board for the next round: every
armed task's children (or the task itself, when it has none) are set building via mk-state (script
from $MK_STATE_CMD, default scripts/mk-state.py) — with --tasks A,B one delivered dispatch arms the
whole lane group, so no phase's children are left behind; a failed write rides along in the JSON as
task_status_error and never changes the delivery verdict. --role review (reviewer panes) skips that
write entirely — the task under review stays at review — and the reply says status_unchanged:true. herdr runs one process per event with no ordering guarantee: every read/decide/write of a watch file
happens under an fcntl.flock on <lane>/.matrixkoder-watch.lock (a stable inode next to the atomically
replaced watch file). One JSON line per command.
--stall-min absent (arm, and the verdict fallback for a watch that records none): <data>/matrixkoder.yaml
session.stall_min, else the mk-config schema default.
dispatch --kind fix|takeover|rework (a fix/takeover/rework round; default build) with --role build on session.fresh_session: new
(<data>/matrixkoder.yaml, else the schema default) first sends the pane's agent its new-session command — /new for
an OpenCode or omp lane, /clear for a Claude lane, from the armed watch's kind — then waits (bounded) for the pane
to go idle and sends the prompt exactly as today. No armed watch kind, a kind without a known command, --kind
--role review, or fresh_session: same: one prompt, no extra herdr call. The reply carries fresh_session (the
command sent, or null).
"""
import contextlib, fcntl, hashlib, importlib.util, json, os, re, shlex, shutil, socket, sqlite3, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
WATCH = ".matrixkoder-watch.json"
LOCK = ".matrixkoder-watch.lock"
DISPATCH_RETRY_S = 5  # wait before dispatch's one automatic retry; $MK_DISPATCH_RETRY_S overrides
DISPATCH_RECHECK_S = 3  # wait before dispatch's one pane-text recheck; $MK_DISPATCH_RECHECK_S overrides
RETRY_PANE_STATES = ("idle", "done")  # pane sat still through a failed wait → the prompt was lost
# todo 130: a task that reached one of these is finished; dispatch never moves it back to building
TERMINAL_STATUSES = ("merged", "qa", "done")
FRESH_IDLE_WAIT_S = float(os.environ.get("MK_FRESH_IDLE_WAIT_S", "30"))  # bounded; $MK_FRESH_IDLE_WAIT_S shrinks it for a self-test
FRESH_IDLE_POLL_S = float(os.environ.get("MK_FRESH_IDLE_POLL_S", "0.5"))
# the doorbell wait is its own bound: a /new just before must finish before the line is typed
DOORBELL_IDLE_WAIT_S = float(os.environ.get("MK_DOORBELL_IDLE_WAIT_S", "15"))
DOORBELL_IDLE_POLL_S = float(os.environ.get("MK_DOORBELL_IDLE_POLL_S", "0.5"))
FRESH_CMDS = {"opencode": "/new", "claude": "/clear", "omp": "/new"}

sys.dont_write_bytecode = True  # no stray __pycache__ next to the scripts


def _load(name, path):
    _spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mod)
    return mod


lw = _load("lane_watch", os.path.join(HERE, "lane-watch.py"))  # verdict logic lives there — import it
lh = _load("lane_health", os.path.join(HERE, "lane-health.py"))
mk_config = _load("mk_config", os.path.join(HERE, "mk-config.py"))
mkdb = _load("mkdb", os.path.join(HERE, "mkdb.py"))  # R1.2 write-guard probe only
mkpaths = _load("mkpaths", os.path.join(HERE, "mkpaths.py"))  # C1/L0: ONE module for every MK path


def cfg(key, flag=None):
    """-> flag > <data>/matrixkoder.yaml > the mk-config schema default. One default, one home:
    a repo that never set the key gets the schema value, not a second literal here."""
    if flag is not None:
        return flag
    try:
        lines = mk_config.read_lines(mkpaths.project_yaml(mk_config.mk_state.resolve_data(None)))
    except Exception:  # outside a repo / unreadable data dir: the schema default, never a crash
        lines = []
    return mk_config.value_of(lines, mk_config.row_for(lines, key))[1]

# Note (fixed on main in 26f1af8): herdr agent read --source recent-unwrapped
# returns {"error":{"code":"agent_not_idle"}} while a full-screen agent works;
# read --source visible then.
_orig_pane_text = lh.pane_text


def _pane_text_fallback(agent, lines=300):
    t = _orig_pane_text(agent, lines)
    if not t:
        try:
            r = subprocess.run([os.environ.get("HERDR_BIN_PATH") or "herdr", "agent", "read", agent,
                                "--source", "visible", "--lines", str(lines)],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and not r.stdout.lstrip().startswith('{"error"'):
                return r.stdout
        except Exception:
            pass
    return t


lh.pane_text = _pane_text_fallback


def ensure_lane_exclude(lane):
    try:
        r = subprocess.run(["git", "-C", lane, "rev-parse", "--git-path", "info/exclude"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            ep = r.stdout.strip()
            if not os.path.isabs(ep):
                ep = os.path.join(lane, ep)
            if os.path.exists(os.path.dirname(ep)):
                cur = open(ep).read() if os.path.exists(ep) else ""
                if ".matrixkoder-*" not in cur:
                    with open(ep, "a") as f:
                        f.write(".matrixkoder-*\n")
    except Exception:
        pass


def out(d):
    print(json.dumps(d))


def fail(msg, code=2):
    out({"ok": False, "error": msg})
    sys.exit(code)


def opt(a, f, d=None):
    if f not in a:
        return d
    if a.index(f) + 1 >= len(a):
        sys.exit(f"{f} needs a value\n{__doc__}")
    return a[a.index(f) + 1]


# ---- discovery: pane → armed lane (worktrees of the repos MK knows) ------------------------------

def mk_home():
    """One MK home rule (C16), identical to mkdb.resolve_home(None): $MK_HOME and
    $MATRIXKODER_HOME are the same setting; when both are set they must agree.
    The rule itself lives in mkpaths (L0); this stays the mk-lane-event spelling of it."""
    return mkpaths.home()


def worktrees(repo):
    try:
        r = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    return [l.split(" ", 1)[1] for l in r.stdout.splitlines() if l.startswith("worktree ")]


def read_watch(lane):
    try:
        with open(os.path.join(lane, WATCH)) as f:
            w = json.load(f)
        return w if isinstance(w, dict) and w.get("pane") and w.get("lane") else None
    except (OSError, ValueError):
        return None


def armed_lanes(data=None):
    """(worktree, watch) for every worktree of every repo in <home>/projects.json that holds a watch
    file; --data filters to one data dir. Missing/invalid projects.json → nothing (the fast path:
    this runs for every pane event on the machine)."""
    try:
        with open(mkpaths.projects_json()) as f:
            projects = json.load(f)
        keys = [k for k in projects if isinstance(k, str)] if isinstance(projects, dict) else []
    except (OSError, ValueError):
        keys = []
    found = []
    for repo in keys:
        for wt in worktrees(repo):
            w = read_watch(wt)
            if not w:
                continue
            w["lane"] = wt  # where the file was found is the truth, not the stored path
            if data is None or os.path.realpath(w.get("data") or "") == os.path.realpath(data):
                found.append((wt, w))
    return found


def find_armed(pane):
    """The armed lane for a pane: the freshest without a verdict, else the freshest with one."""
    open_, done_ = [], []
    for wt, w in armed_lanes():
        if w.get("pane") == pane:
            (done_ if w.get("verdict") else open_).append((wt, w))
    pool = open_ or done_
    return max(pool, key=lambda p: p[1].get("armed_at") or 0, default=None)


# ---- watch file: flock + atomic replace -----------------------------------------------------------

class locked:
    """Exclusive flock on the lane's lock file — herdr gives events no ordering."""

    def __init__(self, lane):
        self.path = os.path.join(lane, LOCK)

    def __enter__(self):
        self.f = open(self.path, "a+")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *e):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def write_watch(lane, w):
    fd, tmp = tempfile.mkstemp(dir=lane, prefix=".watch-")
    with os.fdopen(fd, "w") as f:
        json.dump(w, f, indent=2)
    os.replace(tmp, os.path.join(lane, WATCH))


# ---- one lane-watch poll, against an armed lane ----------------------------------------------------

def pane_fp(text):
    """Pane fingerprint: lane-watch's digit-stripped text, hashed into the watch file."""
    return hashlib.sha256(lw.pane_fingerprint(text or "").encode()).hexdigest()[:16]


def verdict_for(w):
    """The inputs lane-watch's watch() builds each poll — health (agent get + pane text since the
    dispatch marker), commit subject, report existence — fed through decide(). New pane output since
    the last observation refreshes last_activity (the first observation does not: dispatch counts).
    The lane's .matrixkoder-status.json is read FIRST (R27, R42: the caller — cmd_event/cmd_sweep —
    already holds the per-lane flock, so no second locking scheme); an accepted working/subagent
    heartbeat moves the idle mark (R33, subagent only within max_wait_min of armed_at, R34), and
    decide() gets st + armed (R30/R32) and returns source-tagged evidence (R41)."""
    stall = float(w.get("stall_min") or cfg("session.stall_min"))
    rx = re.compile(w.get("commit_grep") or ".*")
    report = w.get("report")
    report_ok = not report or os.path.exists(os.path.join(w["lane"], report))
    subj = lw.commit_subject(w["lane"])
    armed = {"lane": w["lane"], "run": w.get("run"), "task": w.get("task"), "phase": w.get("phase"),
             "armed_at": float(w.get("armed_at") or 0.0),
             "max_wait_min": float(cfg("session.max_wait_min")),
             "data": w.get("data")}
    if not report_ok and lw.store_report_ok(w.get("data"), armed, report):
        report_ok = True  # report artifact in the store; the file is absent
    st = lw.read_status(w["lane"], armed)   # R27/R28; R42: inside the caller's per-lane flock
    since = float(w.get("last_activity") or w.get("armed_at") or time.time())
    if lw.status_activity(st, stall, armed):
        # R33: the reader moves the lane's idle-clock mark to the file's write (never backwards)
        since = max(since, float(st["updated_at"]))
        w["last_activity"] = since
    h = lh.check(w["pane"], w["lane"], w.get("kind"), stall, since, w.get("prompt_marker"))
    fp = pane_fp(h.get("text", ""))
    if w.get("last_pane_fp") is not None and fp != w["last_pane_fp"]:
        # subj/report_ok are computed before the second lh.check on purpose (pane activity only moves the idle clock)
        w["last_activity"] = time.time()
        h = lh.check(w["pane"], w["lane"], w.get("kind"), stall, w["last_activity"], w.get("prompt_marker"))
    w["last_pane_fp"] = fp
    state, ev = lw.decide(h, subj, rx, report_ok, h.get("text", ""), w.get("phase"), stall, st, armed)
    if st and st.get("source") == "store" and ev and ev[-1] == "source=status":
        ev = ev[:-1] + ["source=store"]  # decided from the tasks row, not the status file
    return h, subj, state, ev


def overrides(old, new):
    """May `new` replace the verdict already in the watch file? A repeated identical event is a
    no-op; DONE is final (later events are noise); anything softer can still be upgraded (a stalled
    or blocked lane can finish). pane.exited is handled by the caller: it never overrides."""
    if old is None:
        return True
    return old != new and old != "DONE"


# ---- herdr side effects: pane label + toast (best-effort, never fail the command) ------------------

def socket_call(method, params, timeout=5.0):
    """One newline-JSON request over the herdr socket ({"id","method","params"} → {"id","result"})."""
    path = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
    rid = "mk-%d" % time.time_ns()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall((json.dumps({"id": rid, "method": method, "params": params}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n", 1)[0].decode(errors="replace").strip()
        return json.loads(line) if line else {"error": "empty reply"}


def label_pane(pane, task, status):
    """herdr sidebar label via pane.report_metadata (it has no CLI wrapper → raw socket)."""
    try:
        r = socket_call("pane.report_metadata", {"pane_id": pane, "source": "matrixkoder",
                                                 "state_labels": {"mk": f"{task} · {status}"}})
        return "error" not in r
    except Exception:
        return False


def toast_pane(task, state, status):
    try:
        r = subprocess.run([os.environ.get("HERDR_BIN_PATH") or "herdr", "notification", "show",
                            "MatrixKoder", "--body", f"{task} {state} → {status}"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def apply_verdict(w, state, subj, evidence, h):
    """Record via lane-watch's record() (verdict → mk-state status, WATCHED guard), label + toast,
    stamp the watch file. A failed mk-state write stamps nothing, so the next event retries. A
    group lane arms every listed task, so the verdict is recorded for all of them (children, or
    the task itself when it has none) — the same list dispatch's building marks use. R41: every
    verdict event/note written here carries source=status|pane|grace — decide()'s evidence ends
    with it; evidence from any other caller (the pane-exit STALL) is a pane detection and gets
    source=pane appended."""
    evidence = list(evidence or [])
    if not any(str(e).startswith("source=") for e in evidence):   # R41: pane detections (R35)
        evidence = evidence + ["source=pane"]
    # source is one of store|status|pane|grace (D5A.2); decide() already ends with one of them
    rec = lw.record({"state": state, "commit": subj, "evidence": evidence,
                     "agent_status": (h or {}).get("agent_status")}, watch_tasks(w), w["data"],
                    w.get("run"))
    if not rec.get("ok"):
        return rec, False, False, None
    if state == "DONE":
        missing = ingest_report(w)  # F.4: a claimed test absent from the branch opens claim_mismatch
        if missing:
            rec = dict(rec, claim_mismatch=missing)
    status = lw.STATUS_MAP.get(state, state.lower())
    label = label_pane(w["pane"], w["task"], status)
    toast = toast_pane(w["task"], state, status)
    w["verdict"], w["verdict_at"] = state, time.time()
    write_watch(w["lane"], w)
    return rec, label, toast, status


# ---- commands ---------------------------------------------------------------------------------------

def record_pane(task, pane, data, run=None):
    """`mk-state task set T pane=<pane>`: the lane's herdr pane id on the task, so the dashboard and
    the dashboard's lane rows name the pane without re-reading the watch files. Best-effort: the watch
    file is what makes a lane armed, so a task that is not in run state yet must not stop the arm."""
    common = ["--data", data, "--by", "lane-watch"] + (["--run", run] if run else [])
    r = subprocess.run([sys.executable, mk_state_cmd(), "task", "set", task, f"pane={pane}", *common],
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error": (r.stderr or r.stdout).strip()[-200:]}


def herdr_bin():
    """The herdr binary dispatch runs; $MK_HERDR lets a self-test inject a fake (mk-lane.py's rule)."""
    return os.environ.get("MK_HERDR") or "herdr"


def retry_wait_s():
    """Seconds dispatch sleeps before its ONE automatic retry ($MK_DISPATCH_RETRY_S overrides the
    DISPATCH_RETRY_S default — read at call time so a self-test can run it at 0)."""
    raw = os.environ.get("MK_DISPATCH_RETRY_S")
    try:
        return max(0.0, float(raw)) if raw is not None else float(DISPATCH_RETRY_S)
    except ValueError:
        return float(DISPATCH_RETRY_S)


def recheck_wait_s():
    """Seconds dispatch sleeps before its ONE pane-text recheck ($MK_DISPATCH_RECHECK_S overrides
    the DISPATCH_RECHECK_S default — read at call time like the retry wait, so a self-test can run
    it at 0)."""
    raw = os.environ.get("MK_DISPATCH_RECHECK_S")
    try:
        return max(0.0, float(raw)) if raw is not None else float(DISPATCH_RECHECK_S)
    except ValueError:
        return float(DISPATCH_RECHECK_S)


def prompt_marker(text):
    """The marker dispatch searches the pane text for on its recheck: the FIRST LINE of --text,
    trimmed, capped at 60 chars. The first line carries the meaningful prefix (paths, ids, the
    command's verb) — the loose "first word" form matched every "Read …" sibling-lane prompt and
    turned real delivery failures into recovered ones. The 60-char cap keeps the substring search
    bounded (long paths would otherwise produce expensive near-unique markers)."""
    line = (text or "").strip().split("\n", 1)[0].strip()
    return line[:60]


def pane_text_snapshot(pane):
    """The pane's text right now, via lane-health's read with the visible-screen fallback. Any
    read failure returns '' (a snapshot is best-effort: the comparison below tolerates it)."""
    try:
        return lh.pane_text(pane) or ""
    except Exception:
        return ""


def marker_in_new_pane_text(pane, marker, before):
    """True when the marker appears in pane text that was NOT in `before` — the since_dispatch
    check. Compared by occurrence COUNT (not slice) so a 300-line sliding window cannot hide
    a freshly landed marker: the bottom of the screen is the new text, the top scrolls off,
    and a slice like `after[len(before):]` returns nothing when the window slides. An empty
    marker, an empty `before` AND `after`, or a failed `before` read proves nothing (False)
    — a read failure means we cannot tell old from new, so we cannot prove the dispatch
    landed and we must not recover it."""
    if not marker:
        return False
    after = pane_text_snapshot(pane)
    if not before and not after:
        return False
    # a failed `before` read (empty string) cannot prove the marker is new: treat as "cannot prove"
    if not before:
        return False
    return after.count(marker) > before.count(marker)


def marker_on_pane(pane, marker):
    """Backwards-compat wrapper used by the test contract asserts (and lane-health's check):
    the marker is visible anywhere in the pane's current text. Production code uses
    marker_in_new_pane_text to scope the search to text that arrived since this dispatch started."""
    if not marker:
        return False
    try:
        return marker in (lh.pane_text(pane) or "")
    except Exception:
        return False


def _jload(s):
    s = (s or "").strip()
    if not s.startswith("{"):
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def agent_in_pane(pane):
    """True when `herdr agent list` shows an agent in <pane>; False when it shows none or herdr
    cannot say (missing binary, failure, bad JSON, the 3 s bound) — the caller never hears an error."""
    try:
        r = subprocess.run([herdr_bin(), "agent", "list"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    j = _jload(r.stdout) if r.returncode == 0 else None
    agents = ((j or {}).get("result") or {}).get("agents")
    return isinstance(agents, list) and any(isinstance(x, dict) and x.get("pane_id") == pane for x in agents)


def pane_lacks_agent(pane):
    """True only when `herdr agent list` answers with a list and <pane> is not in it. herdr
    failing or answering oddly is "cannot say" (False), so an unknown never blocks a dispatch."""
    try:
        r = subprocess.run([herdr_bin(), "agent", "list"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    j = _jload(r.stdout) if r.returncode == 0 else None
    agents = ((j or {}).get("result") or {}).get("agents") if isinstance(j, dict) else None
    return isinstance(agents, list) and not any(isinstance(x, dict) and x.get("pane_id") == pane for x in agents)


def pane_state(pane):
    """(agent_status, raw list row, error) of <pane> from `herdr pane list`. The row is the exact
    dict the list named the pane with (None when it did not). A failed / malformed list is an
    error, never a guessed state — the caller reports delivered:false with the error."""
    try:
        r = subprocess.run([herdr_bin(), "pane", "list"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, None, f"{type(e).__name__}: {e}"[:200]
    panes = (lambda j: j.get("result", {}).get("panes") if isinstance(j, dict) else None)(_jload(r.stdout))
    if r.returncode != 0 or not isinstance(panes, list):
        return None, None, (r.stderr or r.stdout or "").strip()[-200:] or f"exit {r.returncode}"
    row = next((p for p in panes if isinstance(p, dict) and p.get("pane_id") == pane), None)
    return (row or {}).get("agent_status"), row, None


def fresh_session_cmd(kind):
    """The new-session command for an agent kind: /new for OpenCode and for omp, /clear for Claude.
    None for a kind with no known command (or no kind at all): that round keeps today's single-prompt
    behaviour — guessing a slash command an agent does not support would be worse than skipping."""
    return FRESH_CMDS.get(kind or "")


def armed_kind(pane):
    """The agent kind the armed watch for <pane> records (arm --kind), or None when not armed."""
    hit = find_armed(pane)
    return hit[1].get("kind") if hit else None


def wait_idle(pane, budget_s=FRESH_IDLE_WAIT_S, poll_s=FRESH_IDLE_POLL_S):
    """-> (status, why) after a bounded wait for <pane> to go idle/done — never an unbounded wait.
    A budget that runs out reports that, so a caller can refuse to act on an unconfirmed pane."""
    deadline = time.time() + budget_s
    while True:
        st, _row, err = pane_state(pane)
        if st in RETRY_PANE_STATES:
            return st, None
        if time.time() >= deadline:
            return st, f"pane still {st or 'unreadable'} after {budget_s:g}s"
        time.sleep(poll_s)


def mk_state_cmd():
    """The mk-state script this file's run-state writes go through (always a subprocess, never an
    import); $MK_STATE_CMD lets a self-test inject a fake (mk-lane.py's rule, like $MK_HERDR)."""
    return os.environ.get("MK_STATE_CMD") or lw.MK_STATE


def bus_cmd():
    """The bus script arm/disarm wire the lane through (a subprocess, like every mk-state call);
    $MK_BUS_CMD lets a self-test inject a fake (the same rule as $MK_STATE_CMD)."""
    return os.environ.get("MK_BUS_CMD") or os.path.join(HERE, "mk-bus.py")


def bus_enabled(data):
    """bus.enabled from <data>/matrixkoder.yaml (mk-config helpers: one parser, one default).
    arm knows its --data, so this never resolves ambient. Missing/unreadable yaml → the schema
    default (true): a repo that never wrote the key still gets the bus."""
    try:
        lines = mk_config.read_lines(mkpaths.project_yaml(data))
        return bool(mk_config.value_of(lines, mk_config.row_for(lines, "bus.enabled"))[1])
    except Exception:  # never crash arm over a setting read
        return True


def run_bus(args_list):
    """One mk-bus command; -> (json, error). Exit 0 with a JSON ok:true line gives (json, None);
    every other outcome (non-zero exit, bad/absent JSON, ok:false, a timeout, an exec failure)
    gives the command's own error as one line. The bus must never fail arm/disarm, so nothing
    here raises."""
    try:
        r = subprocess.run([sys.executable, bus_cmd(), *args_list],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:  # missing script, timeout, …: bus trouble, never arm trouble
        return None, f"{type(e).__name__}: {e}"[:200]
    j = None
    try:
        j = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        j = None
    if r.returncode == 0 and isinstance(j, dict) and j.get("ok"):
        return j, None
    if isinstance(j, dict) and isinstance(j.get("error"), str) and j["error"].strip():
        err = j["error"].strip()
    else:
        tail = (r.stderr or r.stdout or "").strip()
        err = tail.splitlines()[-1] if tail else f"exit {r.returncode}"
    return j, err[:200]


def lane_token_file(lane):
    """Where the lane token lives. D6B moved it out of the worktree:
    <home>/bus-lanes/<sha256(realpath(lane))>.json. $MK_BUS_TOKEN_FILE overrides (tests).
    File mode still writes <lane>/.matrixkoder-bus.json; that path wins when the file is there,
    so a bus that has not moved the token keeps working.
    The rule itself is mkpaths.lane_token (C6, L0): the same one mk-bus and mk-builder use.

    L6.1 moves the file to <home>/bus/lanes/<hash>.json inside mkpaths. Between L6.1 and this
    caller's next token write the two layouts can both exist, so a missing NEW file falls back
    to the OLD one instead of reporting the lane as busless (C7: the bus path never breaks)."""
    path = mkpaths.lane_token(lane)
    if os.path.exists(path):
        return path
    base = mkpaths.bus_env_dir() or mkpaths.home()
    old = os.path.join(os.path.abspath(os.path.expanduser(base)), "bus-lanes",
                       hashlib.sha256(os.path.realpath(lane).encode("utf-8")).hexdigest() + ".json")
    return old if old != path and os.path.exists(old) else path


def store_present(data):
    """The self-hosting guard: the project store exists for this data dir (C4)."""
    return bool(data) and os.path.isfile(mkpaths.project_db(data=data))


def store_rows(data, sql, params=()):
    """SELECT-only against the project store, read-only URI so this file can never write it.
    None when there is no store (a pre-migration project keeps the file view as the truth),
    [] when the store answers with no rows."""
    if not store_present(data):
        return None
    uri = "file:" + mkpaths.project_db(data=data) + "?mode=ro"
    con = None
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)  # uri ends in mode=ro: read-only
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, params)]
    except sqlite3.Error:
        # An unreadable or corrupt store file must not take the caller down: the file
        # view stays the fallback.
        return None
    finally:
        if con is not None:
            con.close()


def store_artifact(data, run, task, kind):
    """Every (round, text) the store holds for one artifact kind of one task, oldest round
    first. None when the store does not have the RUN (no store, or a run nothing imported):
    the caller then falls back to the file view. [] means the store knows the run and holds
    no artifact of that kind."""
    runs = store_rows(data, "SELECT id FROM runs WHERE id=?", (run,))
    if not runs:
        return None
    rows = store_rows(
        data,
        "SELECT round, text FROM artifacts WHERE run=? AND task=? AND kind=? ORDER BY round, id",
        (run, task, kind))
    if rows is None:
        return None
    return [(int(r.get("round") or 0), r.get("text") or "") for r in rows]


def newest_stored_artifact(data, run, task, kind):
    """The newest NON-EMPTY (round, text) the store holds for that kind, or None. An empty
    text never wins: the file view is checked next."""
    got = store_artifact(data, run, task, kind)
    for n, text in reversed(got or []):
        if text.strip():
            return n, text
    return None


def arm_bus(lane, task, run, data):
    """arm's bus wiring: ensure the server, then mint the lane token. The token file is
    outside the lane (D6B); on full success report its url. token only runs after a good
    ensure, so a dead bus costs one command, and every failure lands in the reply.
    arm itself never writes .matrixkoder-brief.md or .matrixkoder-bus.json into the lane."""
    _j, err = run_bus(["ensure", "--data", data, "--run", run])
    if err:
        return {"ok": False, "error": err}
    _j, err = run_bus(["token", "--lane", lane, "--task", task, "--run", run, "--data", data])
    if err:
        return {"ok": False, "error": err}
    try:
        with open(lane_token_file(lane)) as f:
            url = json.load(f).get("url")
    except (OSError, ValueError):
        return {"ok": False, "error": "lane bus file unreadable"}
    return ({"ok": True, "url": url} if url
            else {"ok": False, "error": "lane bus file has no url"})


def disarm_bus(lane, data):
    """disarm's bus wiring: revoke the lane token (--data only when the watch named one). Like
    arm_bus, a failure is reported in the reply, never fatal to the disarm."""
    _j, err = run_bus(["revoke", "--lane", lane, *(["--data", data] if data else [])])
    return {"ok": True} if err is None else {"ok": False, "error": err}


def watch_tasks(w):
    """The tasks a watch file's lane serves: the armed group list (arm --tasks A,B), else the
    single --task id. The primary id stays in w["task"] for every single-task consumer."""
    group = [str(t) for t in (w.get("tasks") or []) if str(t)]
    return group or ([w["task"]] if w.get("task") else [])


def child_ids(task, data, run):
    """Stored ids of <task>'s children in <run>, [] when it has none or run state is unreadable.
    C4: the store's tasks rows are the source; run.json is the fallback for a project that
    has not been imported (file mode) or whose store does not know the run."""
    rows = store_rows(data, "SELECT id, parent FROM tasks WHERE run=?", (run,))
    if rows is not None:
        return [r.get("id") for r in rows if r.get("id")
                and (r.get("parent") or "").lower() == str(task).lower()]
    try:
        with open(mkpaths.run_doc(data, run)) as f:
            tasks = json.load(f).get("tasks") or []
    except (OSError, ValueError):
        return []
    return [t.get("id") for t in tasks if isinstance(t, dict)
            and (t.get("parent") or "").lower() == str(task).lower()]


def slot_acquired_task(data, slot):
    """The task `mk-lane.py acquire` recorded for <slot>: the lane registry's row. C4: the
    store's `lane_slots` rows are the truth; lanes.json is the fallback for a project with no
    store. -> (task, error): task None when the slot is free/unknown, error names a read
    failure (a registry we cannot read is refused, never assumed to match)."""
    if slot in (None, ""):
        return None, "no slot recorded on the task"
    rows = store_rows(data, "SELECT slot, task, state FROM lane_slots WHERE slot=?", (str(slot),))
    if rows is None:  # no store: the published view
        try:
            with open(mkpaths.lanes_file(data)) as f:
                doc = json.load(f)
            rec = (doc.get("slots") or {}).get(str(slot))
        except (OSError, ValueError) as e:
            return None, f"lane registry unreadable: {type(e).__name__}"
        return ((rec or {}).get("task") or None), (None if rec else f"slot {slot} not in lanes.json")
    if not rows:
        return None, f"slot {slot} not in the lane registry"
    r = rows[0]
    if (r.get("state") or "") != "busy":
        return None, f"slot {slot} is {r.get('state') or 'free'}, not busy"
    return (r.get("task") or None), None


def _task_group(value):
    """A comma string as a task list: "A,B" → ["A", "B"]; "" → []. `mk-lane.py acquire` records
    a group lane's slot task as the raw --tasks string, so the comparison below splits it."""
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


def dispatched_task(w, named):
    """The task a dispatch delivers for: --task when it names a member of the armed group (the
    group's own spelling), else the lead task. todo 130/HIGH: the brief published on the bus and
    copied into a bus-down lane must be the DISPATCHED task's, never always the lead's."""
    group = watch_tasks(w)
    if named:
        for t in group:
            if str(t).lower() == str(named).lower():
                return t
    return group[0] if group else (w or {}).get("task")


def dispatch_task_mismatch(w, named, data, run):
    """todo 130: dispatch must refuse when the pane's armed task is not the task
    `mk-lane.py acquire` recorded for that slot — the bug that published the previous task's
    brief after a slot was reused. -> error string, or None when they agree / the registry
    cannot be read (an unknown never blocks a dispatch).

    GROUP LANES: a lane armed `arm --tasks A,B` has the armed GROUP watch_tasks(w) = [A, B],
    while acquire records the slot's task as "A,B". So --task must name a member of the armed
    group (never compare it to the lead alone), and the slot's recorded group (split on `,`)
    must equal the armed group — case- and order-insensitively. Comparing single ids rejected
    every group dispatch, and every `--task B` on a group lane.

    Reads the store directly (one read-only SELECT): a child mk-state call here would show up
    in a caller's fake-$MK_STATE_CMD log, and this is only a read."""
    group = watch_tasks(w)
    if not group:
        return None
    if named and str(named).lower() not in {t.lower() for t in group}:
        return (f"dispatch names task {named} but the pane's armed tasks are {', '.join(group)}")
    if not (data and run):
        return None
    rows = store_rows(data, "SELECT slot FROM tasks WHERE run=? AND lower(id)=lower(?)",
                      (run, str(group[0])))
    slot = rows[0].get("slot") if rows else None
    acquired, err = slot_acquired_task(data, slot)
    if err:
        return None  # an unreadable registry is "cannot say": never blocks a dispatch
    if not acquired:
        return None
    if {t.lower() for t in _task_group(acquired)} != {t.lower() for t in group}:
        return (f"the pane's armed tasks {', '.join(group)} are not the tasks {acquired} "
                f"acquire recorded for slot {slot}")
    return None


def mk_state_json(args_list, timeout=30):
    """One mk-state command; -> (parsed JSON line, None) or (None, error). Never raises: a
    failed or unreadable mk-state is the caller's error field, never this helper's exception.
    A NON-ZERO exit with a JSON line still parses the line (mk-state prints its own
    {"ok": false, "error": ...} and exits 2) — the caller reads ok off it; only an
    unparseable line is an error here."""
    try:
        r = subprocess.run([sys.executable, mk_state_cmd(), *args_list],
                           capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # missing script, timeout, …: reported, never raised
        return None, f"{type(e).__name__}: {e}"[:200]
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), None
    except (json.JSONDecodeError, IndexError):
        # an argparse usage error exits 2 BEFORE main's JSON handlers: the line is on
        # stderr. Surface it, so a `task check` mk-state predates is reported, not fatal.
        tail = (r.stderr or r.stdout).strip()
        return None, (tail.splitlines()[-1] if tail else f"exit {r.returncode}")[:200]


def record_route(w, agent=None, model=None, effort=None):
    """D1.5.3: a build dispatch records the route on every armed task itself (a group lane
    writes it to each listed task — no children expansion, the lane is the build unit).
    --agent/--model/--effort name the route; without --agent the armed watch's kind is the
    agent and model/effort stay empty. Only the GIVEN fields are written: an absent flag
    leaves the task's value alone (a re-dispatch without --model keeps the model), while an
    explicitly empty one clears it (mk-state's `k=` rule). The write happens BEFORE the
    prompt is typed, so a refused check below can still stop everything, and a mk-state
    failure rides in the reply as task_route_error — never fatal to the delivery verdict.
    -> the reply's route dict ({"agent","model","effort"}, each None when empty), plus an
    error string or None. No armed watch (or no data / no tasks) → (None, None)."""
    data = w.get("data")
    group = watch_tasks(w)
    if not (data and group):
        return None, None
    run = w.get("run")
    route = {"agent": agent or w.get("kind") or None, "model": model or None, "effort": effort or None}
    # the route write covers the given keys only: an absent flag leaves the task alone;
    # an explicitly empty one clears it; the watch kind always supplies the agent
    pairs = [("agent", route["agent"])] if route["agent"] else []
    pairs += [("model", model)] if model is not None else []
    pairs += [("effort", effort)] if effort is not None else []
    if not pairs:
        return route, None  # nothing to write (no flag, no watch kind): the route stays empty
    common = ["--data", data, "--by", "dispatch"] + (["--run", run] if run else [])
    errors = []
    for t in group:
        j, err = mk_state_json(["task", "set", t, *[f"{k}={v}" for k, v in pairs], *common])
        if j is None or not isinstance(j, dict) or not j.get("ok"):
            errors.append(f"{t}: {err or (j or {}).get('error', 'mk-state task set failed')}")
    return route, ("; ".join(errors)[:300] if errors else None)


def task_check(w, task):
    """D1.5.4: the mandatory-field check on the armed task before anything is typed:
    `mk-state task check <T> --data <watch data> [--run <watch run>]` -> (ok, missing, error).
    Read-only. No data/task in the watch → (True, [], None): nothing to check. An mk-state
    that cannot answer the check — an ok:false line with no missing list (an argparse usage
    error from a build predating `task check`, or any refusal of the command itself) — is
    treated as PASSING (True, [], error): the gate stops an incomplete task, not a dispatch
    over a version mismatch; the error still rides in the reply as task_check_error."""
    data = w.get("data")
    if not (data and task):
        return True, [], None
    run = w.get("run")
    j, err = mk_state_json(["task", "check", task, "--data", data]
                           + (["--run", run] if run else []))
    if not isinstance(j, dict):
        return True, [], err or "mk-state task check unavailable"  # unparseable: never gates
    if "ok" not in j:  # an ok-less line: mk-state could not answer the check at all
        return True, [], err or (j.get("error") or "mk-state task check gave no verdict")
    missing = j.get("missing")
    has_missing = isinstance(missing, list)
    missing = [str(x) for x in missing] if has_missing else []
    ok = bool(j.get("ok"))
    # an ok:false line that is NOT a check verdict (no missing key at all — mk-state refused
    # the command itself, e.g. an argparse usage error from a build predating `task check`):
    # reported, never gates — the gate stops an incomplete task, not a version mismatch
    if not ok and not has_missing:
        return True, [], j.get("error") or err or "mk-state task check gave no verdict"
    return ok, missing, None if ok else (j.get("error") or None)


def mark_armed_building(pane, rework=False):
    """dispatch delivered:true → the lane's tasks move again: the children of EVERY armed task
    (or the task itself when it has none) are set building via mk-state, so the board shows the
    lane working before the agent's first event lands — in a shared lane group one delivered
    prompt arms every listed phase (tonight's two-phase lane only moved the first phase's).
    No armed watch file → None, nothing else in the JSON. mk-state failures are returned, never
    swallowed and never fatal: the delivery already happened (a parent is never set directly —
    its status rolls up, so its children are set instead).
    todo 130: a finished target is NEVER demoted. When a re-delivered prompt lands on a lane
    whose task already reached merged/qa/done (L7.3, Z3, Z5 were set back to building this way),
    the write is skipped and the reply names the target in skipped_terminal."""
    hit = find_armed(pane)
    if not hit:
        return None
    w = hit[1]
    data = w.get("data")
    group = watch_tasks(w)
    if not (data and group):
        return None
    run = w.get("run")
    targets, seen = [], set()
    for t in group:
        kids = child_ids(t, data, run) if run else []
        for kid in (kids or [t]):
            if str(kid).lower() not in seen:
                seen.add(str(kid).lower())
                targets.append(kid)
    # todo 130: the statuses as the store holds them, one read for the whole target list.
    # Store-only: with no store (or an unreadable one) nothing is known, so nothing is
    # skipped and no extra mk-state call is made.
    statuses = {}
    if run:
        rows = store_rows(data, "SELECT id, status FROM tasks WHERE run=?", (run,))
        if rows is not None:
            statuses = {str(r.get("id") or "").lower(): (r.get("status") or "") for r in rows}
    common = ["--data", data, "--by", "dispatch"] + (["--run", run] if run else [])
    done, skipped, errors, reopened = [], [], [], []
    for t in targets:
        cur = statuses.get(str(t).lower())
        if cur in TERMINAL_STATUSES and not rework:
            skipped.append(t)  # a finished task stays finished: never moved back to building
            continue
        if cur in TERMINAL_STATUSES:
            reopened.append(t)  # F.4: a rework dispatch is the one exception to the merged guard
        r = subprocess.run([sys.executable, mk_state_cmd(), "task", "set", t, "status=building", *common],
                           capture_output=True, text=True)
        try:
            j = json.loads(r.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            j = None
        if r.returncode != 0 or not isinstance(j, dict) or not j.get("ok"):
            errors.append(f"{t}: {(r.stderr or r.stdout).strip()[-200:] or f'exit {r.returncode}'}")
        else:
            done.append(t)
    res = {"building": done}
    if skipped:
        res["skipped_terminal"] = skipped
    if reopened:
        res["reopened"] = reopened
    if errors:
        res["task_status_error"] = "; ".join(errors)[:300]
    return res


def selftest_jobs_total(data):
    """session.selftest_jobs_total from mk-config.py, looked up by the HERE path so the
    cwd does not matter. Bounded at 5 s. A failure, or a value outside 1..16 (the same
    range scripts/selftest.sh accepts), uses 4."""
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "mk-config.py"), "get",
             "session.selftest_jobs_total", "--data", data],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            j = json.loads(r.stdout.strip().splitlines()[-1])
            v = j.get("value")
            if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 16:
                return v
    except Exception:
        pass
    return 4


def selftest_jobs_hint(data, active=None):
    """The per-lane self-test job budget: max(1, total // active_lanes).

    An existing MK_SELFTEST_JOBS in the environment wins and is returned as-is.
    active overrides the armed-lane count (tests); otherwise it is the number of
    armed lanes for this data dir, at least 1. The result is a number, never a
    model or effort choice.
    """
    env = os.environ.get("MK_SELFTEST_JOBS")
    if env and env.strip():
        return env.strip()
    total = selftest_jobs_total(data)
    if active is None:
        active = len(armed_lanes(data)) or 1
    active = active or 1
    return str(max(1, total // active))


def bus_dir_for(data):
    """Where bus.json lives, the same rule as mk-bus.bus_location — mkpaths.bus_dir (L0):
    $MK_BUS_DIR, else the MK home when <data>/mk.db exists (store mode), else the data dir
    (file mode)."""
    if not data:
        return data  # no data dir at all: file mode has nothing to resolve a bus for
    return mkpaths.bus_dir(data)


def brief_message(round_n, text):
    """The one brief format both publish_brief and review-handoff send, and the one
    connect reads: `round:<n>\nkind:brief\n<body>`."""
    return f"round:{round_n}\nkind:brief\n{text}"


def bus_is_up(data):
    """True only when a bus is already serving this data dir. Reads bus.json from the
    mk-bus location and probes /health; never calls ensure, so a down bus stays down."""
    if not data:
        return False
    try:
        with open(mkpaths.bus_json(data)) as f:
            info = json.load(f)
        port = int(info.get("port"))
    except (OSError, ValueError, TypeError):
        return False
    try:
        r = subprocess.run(
            [sys.executable, "-c",
             "import json,sys,urllib.request\n"
             "body=urllib.request.urlopen(sys.argv[1], timeout=2).read()\n"
             "j=json.loads(body)\n"
             "sys.exit(0 if isinstance(j, dict) and j.get('ok') and j.get('status')=='healthy' else 1)",
             f"http://127.0.0.1:{port}/health"],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    return r.returncode == 0


def brief_body_for(data, run, task):
    """The brief the orchestrator would hand the lane: the newest brief for the task.
    C4: the store's `brief` artifact wins; the task folder's brief-rN.md is the fallback for
    a pre-migration project or a round nothing stored yet."""
    got = newest_stored_artifact(data, run, task, "brief")
    if got:
        return got[1], got[0]
    d = mkpaths.task_dir(data, run, task)
    best, best_n = None, -1
    try:
        names = os.listdir(d)
    except OSError:
        return None, None
    for name in names:
        m = re.fullmatch(r"brief-r(\d+)\.md", name)
        if m and int(m.group(1)) > best_n:
            best, best_n = os.path.join(d, name), int(m.group(1))
    if not best:
        return None, None
    try:
        return open(best).read(), best_n
    except OSError:
        return None, None


def write_lane_brief(lane, text):
    """The bus-down fallback copy: <lane>/.matrixkoder-brief.md, written atomically like the
    watch file. The ONLY writer of that path (the docs say "bus-down-fallback-only"; until now
    the orchestrator did it by hand, so a bus-down lane had no brief at all). Returns the path,
    or None when there is nothing to write / the lane is not a directory."""
    if not (lane and text and str(text).strip()) or not os.path.isdir(lane):
        return None
    try:
        fd, tmp = tempfile.mkstemp(dir=lane, prefix=".brief-")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        dest = os.path.join(lane, ".matrixkoder-brief.md")
        os.replace(tmp, dest)
        return dest
    except OSError:
        return None


def publish_brief(data, run, task):
    """Send the task's current brief to the lane as the orchestrator. File mode uses kind
    info (the only kinds a file-mode bus accepts); store mode uses kind task. -> (ok, error)."""
    text, n = brief_body_for(data, run, task)
    if not text or not str(text).strip():
        return False, "no brief"
    body = brief_message(n, text)
    kind = "task" if os.path.isfile(mkpaths.project_db(data=data)) else "info"
    _j, err = run_bus(["send", "--data", data, "--run", run, "--task", task,
                       "--kind", kind, "--from", "orch", "--to", task, "--text", body])
    return err is None, err


def publish_review_brief(data, run, task):
    """Send the task's newest review brief to the lane as `round:<n>\nkind:review-brief\n<body>`
    (its own kind: the builder's connect never picks it up). Same send rules as publish_brief.
    C4: the store's `review-brief` artifact wins; review-brief-rN.md in the task folder is the
    fallback for a pre-migration project."""
    got = newest_stored_artifact(data, run, task, "review-brief")
    if got:
        body = f"round:{got[0]}\nkind:review-brief\n{got[1]}"
    else:
        d = mkpaths.task_dir(data, run, task)
        best, best_n = None, -1
        for name in (os.listdir(d) if os.path.isdir(d) else []):
            m = re.fullmatch(r"review-brief-r(\d+)\.md", name)
            if m and int(m.group(1)) > best_n:
                best, best_n = os.path.join(d, name), int(m.group(1))
        if not best:
            return False, "no review brief"
        with open(best) as f:
            body = f"round:{best_n}\nkind:review-brief\n{f.read()}"
    kind = "task" if store_present(data) else "info"
    _j, err = run_bus(["send", "--data", data, "--run", run, "--task", task,
                       "--kind", kind, "--from", "orch", "--to", task, "--text", body])
    return err is None, err


def alert_publish_failure(data, run, task, error):
    """A brief that did not reach the bus must not hide in the dispatch reply: post an info
    message to the orchestrator (the stage 6 watch shows it) and a run event (the board shows
    it). Best effort: a failure here never changes the dispatch."""
    text = f"BRIEF NOT ON BUS: {task} brief publish failed ({error}); the full prompt was typed instead"
    run_bus(["send", "--data", data, "--run", run, "--task", task, "--kind", "info",
             "--from", "dispatch", "--to", "orch", "--text", text])
    try:
        subprocess.run([sys.executable, mk_state_cmd(), "--data", data, "--run", run, "event",
                        "--task", task, "--type", "note", "--text", text],
                       capture_output=True, text=True, timeout=30)
    except Exception:
        pass


DOORBELL_ACT = "then carry out what it delivers (brief, round, nudge) without asking; ask only over the bus for a real decision."


def connect_text(lane, data, role_arg=""):
    """The one line a bus-up dispatch types: connect, plus the job-budget command.

    `--data <dir>` is in the line so the lane's first command works with no env (E1 reads
    it). The hint is a command prefix, so typing it is how the builder's shell gets the var.
    One line: the fake herdr and the prompt marker both keep only the first line. The closing
    sentence tells the lane to act on what connect delivers; role_arg goes before it.
    """
    skill = os.path.join(lane, ".claude", "skills", "mk-builder")
    n = selftest_jobs_hint(data) if data else "4"
    # MK_SELFTEST_JOBS=N stays in the production doorbell: it is the E6 job-budget hint
    # and E1 reads it from the typed line.
    data_arg = f" --data {shlex.quote(data)}" if data else ""
    return (f"MK_SELFTEST_JOBS={n} python3 {shlex.quote(skill)}/scripts/mk-builder.py connect{data_arg}{role_arg} {DOORBELL_ACT}")


def delivered_status(pane, role, rework=False):
    """The board write a DELIVERED dispatch makes, by role. build (the default) →
    mark_armed_building: every armed task's children (or the task itself) go building. review →
    no write at all — the task under review stays at review, and status_unchanged:true in the
    reply says the verdict knowingly moved nothing."""
    if role == "review":
        return {"status_unchanged": True}
    return mark_armed_building(pane, rework) or {}


def _prompt_attempt(pane, text, ms):
    """One `herdr agent prompt --wait --timeout <ms>` call → (kind, why, agent_status, parsed reply).
    kind: "ok" = the wait result itself says the prompt landed; "exc" = the subprocess died or hung;
    "wait" = herdr timed out or settled on agent_prompt_stalled — a timeout proves nothing either
    way, the pane's own state must decide; "fail" = any other herdr failure. why is herdr's own
    words (or the exception's), never empty."""
    try:
        r = subprocess.run([herdr_bin(), "agent", "prompt", pane, text, "--wait", "--timeout", str(ms)],
                           capture_output=True, text=True, timeout=ms / 1000 + 30)
    except (OSError, subprocess.TimeoutExpired) as e:
        # the subprocess died, the agent may not have: the pane's own state decides, exactly
        # like a herdr timeout
        return "exc", f"{type(e).__name__}: {e}"[:300], None, None
    blob, err = (r.stdout or "").strip(), (r.stderr or "").strip()
    j = _jload(blob)
    jerr = j.get("error") if isinstance(j, dict) else None  # herdr can print {"error": ...} and exit 0
    agent = j.get("result", {}).get("agent") if isinstance(j, dict) else None
    status = agent.get("agent_status") if isinstance(agent, dict) else None
    why = "; ".join(x for x in ((jerr or {}).get("code") if isinstance(jerr, dict) else None, err, blob)
                    if x)[:300] or f"herdr exited {r.returncode} with no output"
    if r.returncode == 0 and isinstance(j, dict) and "result" in j and jerr is None \
            and status != "agent_prompt_stalled":
        return "ok", why, status, j
    if status == "agent_prompt_stalled" or "timeout" in why.lower() or "timed out" in why.lower():
        return "wait", why, status, j
    return "fail", why, status, j


def cmd_dispatch(a):
    """Prompt a pane's agent and check delivery in one call. Success → delivered:true. A timeout
    (herdr's error, the subprocess dying, or the wait settling on agent_prompt_stalled) → the pane's
    own state decides: still working → delivered:true; anything else → delivered:false — and when
    the pane just sits idle/done through a failed wait (the prompt was lost; once seen on a pane
    that was done all along) dispatch waits DISPATCH_RETRY_S and sends the prompt ONE more time,
    the second attempt deciding the verdict and never retrying again. When even that leaves the
    pane idle/done, one recheck after DISPATCH_RECHECK_S — a fresh pane list plus the pane text —
    searches for the prompt marker (the first words of --text): found → delivered:true with
    recovered:true (herdr's wait gave up while the prompt had already landed). A build dispatch to
    an armed lane with a live bus types the connect doorbell instead of --text (--connect says so;
    --no-connect forces the full prompt); every reply carries connect:true|false, and note
    "legacy prompt: <why>" when the lane is armed and the full prompt went anyway. --role review
    (reviewer panes) delivers without the board write: no mark_armed_building, the reply says
    status_unchanged:true instead. Every reply carries attempts (1 or 2), herdr_error (herdr's
    words for the attempt that did not deliver — the first attempt's when a retry lands, the final
    attempt's when both fail, the reply itself when nothing failed) and pane_entry (the freshest
    raw `herdr pane list` row for the pane, null when no observation names it). A delivered reply
    never carries error: herdr's timeout words stay in herdr_error and note says what happened. A --kind
    fix|takeover|rework build dispatch on session.fresh_session: new first sends the pane's agent its
    new-session command (/new OpenCode, /clear Claude — the armed watch's kind names the agent)
    and waits (bounded) for the pane to go idle; the reply's fresh_session names the command sent,
    or null. A reset herdr refuses outright, or a pane that never goes idle, ends the round with
    ok:false "fresh session not confirmed" and NO task prompt — but a reset that times out
    (wait/exc, which start no model turn) is delivered as soon as the pane reads idle, like every
    other uncertain prompt here. Other failures → ok:false. Completed checks exit 0 whatever the
    verdict; only usage errors exit 2."""
    pane, text = opt(a, "--pane"), opt(a, "--text")
    if not (pane and text):
        fail("dispatch needs --pane --text")
    # The doorbell (bus on): the brief and the token are not in the lane, so the typed
    # prompt is only the connect doorbell. A build dispatch to an armed lane with a live
    # bus rings it by default (--connect says so explicitly); --no-connect keeps the
    # caller's full prompt unchanged, which is also the bus-down path and every dispatch
    # that predates the doorbell. The reply says which one was typed: connect, and a note
    # when the lane is armed but the full prompt went anyway.
    # The doorbell replaces the prompt ONLY when the bus is actually up and the brief
    # was published. A yaml flag is not enough: a bus that is enabled but down, or a
    # publish that fails, keeps today's full prompt (C12).
    connected, legacy_note, lane_brief = False, None, None
    role = opt(a, "--role", "build")
    if role not in ("build", "review"):
        fail("--role must be build or review")
    round_kind = opt(a, "--kind", "build")
    if round_kind not in ("build", "fix", "takeover", "rework"):
        fail("--kind must be build, fix, takeover or rework")
    try:
        ms = int(opt(a, "--timeout-ms", "25000"))
    except ValueError as e:
        fail(f"--timeout-ms: {e}")

    def reply(d, note=None):
        n = "; ".join(x for x in (legacy_note, note) if x)
        out({**d, "connect": connected, **({"lane_brief": lane_brief} if lane_brief else {}),
             **({"note": n} if n else {})})

    # todo 130/R2: EVERY guard runs BEFORE anything is published, written or typed. A refused
    # dispatch (a task/slot mismatch, --task outside the armed group, or the mandatory-field
    # check) leaves the bus and the lane exactly as they were — the live bug this round fixes
    # published the stale lead's brief before the mismatch was even checked.
    hit = find_armed(pane) if role == "build" else None
    route = route_err = None
    check = None
    check_unavailable = None
    disp_task = None
    if hit:
        w = hit[1]
        mismatch = dispatch_task_mismatch(w, opt(a, "--task"), w.get("data"), w.get("run"))
        if mismatch and "--force" not in a:
            reply({"ok": False, "delivered": False, "pane": pane,
                   "armed_task": w.get("task"), "error": mismatch})
            return
        # HIGH: publish/copy the DISPATCHED task's brief, never always the lead's: --task names
        # the round being handed off, so its brief is the one that belongs on the bus.
        disp_task = dispatched_task(w, opt(a, "--task"))
        route, route_err = record_route(
            w, agent=opt(a, "--agent"), model=opt(a, "--model"), effort=opt(a, "--effort"))
        ok, missing, chk_err = task_check(w, w.get("task"))
        if chk_err and ok:
            check_unavailable = chk_err  # reported on every reply; never gates
        if not ok and "--force" not in a:
            # Refused: NOTHING is typed — no herdr prompt call at all, no fresh-session reset.
            reply({"ok": False, "delivered": False, "pane": pane,
                   "check": {"ok": False, "missing": missing},
                   "error": f"task check failed: missing {', '.join(missing) or chk_err or 'fields'}",
                   **({"route": route} if route else {}),
                   **({"task_route_error": route_err} if route_err else {}),
                   **({"task_check_error": chk_err} if chk_err else {})})
            return
        if not ok:
            check = {"ok": False, "missing": missing, "forced": True}
    # The doorbell (bus on): the brief and the token are not in the lane, so the typed
    # prompt is only the connect doorbell. A build dispatch to an armed lane with a live
    # bus rings it by default (--connect says so explicitly); --no-connect keeps the
    # caller's full prompt unchanged, which is also the bus-down path and every dispatch
    # that predates the doorbell. The reply says which one was typed: connect, and a note
    # when the lane is armed but the full prompt went anyway.
    # The doorbell replaces the prompt ONLY when the bus is actually up and the brief
    # was published. A yaml flag is not enough: a bus that is enabled but down, or a
    # publish that fails, keeps today's full prompt (C12). It runs only AFTER the guards
    # above: a refused dispatch publishes nothing (todo 130/R2).
    if role == "build" and hit:
        wdata = hit[1].get("data") or ""
        wrun = hit[1].get("run")
        why = None
        if "--no-connect" in a:
            why = "--no-connect given"
        elif not bus_enabled(wdata):
            why = "bus.enabled is false"
        elif not bus_is_up(wdata):
            why = "bus is down"
        else:
            ok, publish_error = publish_brief(wdata, wrun, disp_task)
            if ok:
                # A /new sent just before must not merge with the typed line (todo 74):
                # wait, bounded, until the pane agent is idle or done. A pane that is
                # still busy after the wait keeps the full prompt and is not typed into.
                ist, iwhy = wait_idle(pane, budget_s=float(os.environ.get('MK_DOORBELL_IDLE_WAIT_S', str(DOORBELL_IDLE_WAIT_S))), poll_s=float(os.environ.get('MK_DOORBELL_IDLE_POLL_S', str(DOORBELL_IDLE_POLL_S))))
                if ist in RETRY_PANE_STATES:
                    text = connect_text(hit[1].get("lane") or "", wdata)
                    connected = True
                else:
                    # a busy pane gets nothing typed: not the doorbell, not the full
                    # prompt. The caller retries later.
                    out({"ok": False, "delivered": False, "pane": pane,
                         "publish_error": f"pane not idle: {ist or iwhy}"})
                    sys.exit(1)
            else:
                why = f"publish failed: {publish_error}"
                if publish_error:
                    # the full prompt is kept; the reply says why the doorbell was not typed
                    a = list(a) + ["--publish-error", publish_error]
                    alert_publish_failure(wdata, wrun, disp_task, publish_error)
        if why:
            legacy_note = f"legacy prompt: {why}"
            # A2/X3.2: the bus-down fallback copies the DB brief into the lane. Only here —
            # when the bus is up the lane reads the brief over the bus (connect) and no copy
            # is written (docs: "bus-down-fallback-only").
            btext, _bn = brief_body_for(wdata, wrun, disp_task)
            lane_brief = write_lane_brief(hit[1].get("lane") or "", btext)
    elif role == "review" and opt(a, "--lane") and "--no-connect" not in a:
        # review over the bus: the reviewer pane shares the builder's lane, so the lane's
        # watch file names the run/task; the review brief goes as its own kind and the pane
        # gets `connect --role review`. No --lane keeps the typed review prompt (old callers).
        rlane = opt(a, "--lane")
        if pane_lacks_agent(pane):
            # todo 88: publishing first left one review-brief copy on the bus per failed retry
            out({"ok": False, "delivered": False, "pane": pane, "connect": False,
                 "error": f"no agent in pane {pane}; start the reviewer first (nothing published)"})
            return
        w = read_watch(rlane) or {}
        rdata, rrun, rtask = w.get("data") or "", w.get("run"), w.get("task")
        why = None
        if not (rdata and rrun and rtask):
            why = f"no armed watch in {rlane}"
        elif not bus_enabled(rdata):
            why = "bus.enabled is false"
        elif not bus_is_up(rdata):
            why = "bus is down"
        else:
            ok, publish_error = publish_review_brief(rdata, rrun, rtask)
            if ok:
                text = connect_text(rlane, rdata, " --role review")
                connected = True
            else:
                why = f"publish failed: {publish_error}"
                alert_publish_failure(rdata, rrun, rtask, publish_error or "no review brief")
        if why:
            legacy_note = f"legacy prompt: {why}"

    # a fix/takeover round on fresh_session: new resets the agent's session first: the command,
    # then a bounded idle wait, then the prompt exactly as today. Everything else (build, review,
    # fresh_session: same, no armed kind, unknown kind) skips this and sends one prompt as before.
    fresh = (fresh_session_cmd(armed_kind(pane))
             if role == "build" and round_kind in ("fix", "takeover", "rework")
             and cfg("session.fresh_session") == "new" else None)
    if fresh:
        # The reset gates the round, but only a CONFIRMED failure blocks it: `wait`/`exc` mean
        # "proves nothing, the pane's state decides" everywhere else in this function, and `/clear`
        # and `/new` start no model turn, so herdr may well time out on a reset that worked. A pane
        # that ends idle/done is the confirmation; anything else is not.
        rkind, rwhy, rstatus, _rj = _prompt_attempt(pane, fresh, ms)
        ist, iwhy = wait_idle(pane)
        if rkind == "fail" or ist not in RETRY_PANE_STATES:
            reply({"ok": False, "delivered": False, "pane": pane, "fresh_session": fresh,
                 "error": "fresh session not confirmed",
                 "fresh_session_error": rwhy if rkind == "fail" else (iwhy or "pane never went idle"),
                 "fresh_session_kind": rkind, "agent_status": rstatus})
            return
    # the pane's raw list row before any prompt: the ok/fail arms report it as pane_entry; every
    # not-delivering exc/wait pass below replaces it with its own deciding observation
    _, entry, pl_err = pane_state(pane)
    # since_dispatch capture: the pane text RIGHT NOW, before any herdr call. The recheck only
    # counts the prompt marker when it appears in pane text that arrived AFTER this snapshot —
    # an old "Read and follow …" sitting in pane history from a sibling lane must not recover
    pane_text_before = pane_text_snapshot(pane)
    # D1.5.3/D1.5.4 ride on every reply that gets past the gate: the recorded route (the
    # write happened above, before anything was typed) and the check result (a forced
    # failure says so). An unarmed pane carries neither.
    carried = {**({"route": route} if route else {}),
               **({"task_route_error": route_err} if route_err else {}),
               **({"check": check} if check else {}),
               **({"task_check_error": check_unavailable} if check_unavailable else {})}
    attempts, fail_why = 1, None
    while True:
        kind, why, status, j = _prompt_attempt(pane, text, ms)
        if kind == "ok":
            reply({"ok": True, "delivered": True, "pane": pane, "agent_status": status, "timeout_ms": ms,
                 "attempts": attempts, "herdr_error": fail_why or why, "pane_entry": entry,
                 "fresh_session": fresh,
                 **({"publish_error": opt(a, "--publish-error")} if opt(a, "--publish-error") else {}),
                 **({"pane_list_error": pl_err} if pl_err else {}), **delivered_status(pane, role, round_kind == "rework"),
                 **carried})
            return
        fail_why = why
        if kind == "fail":  # herdr failed some other way: its own words, no delivery, no pane check
            reply({"ok": False, "delivered": False, "pane": pane, "timeout_ms": ms, "attempts": attempts,
                 "herdr_error": why, "error": why, "pane_entry": entry, "fresh_session": fresh, **carried})
            return
        # exc / wait: a timeout proves nothing either way — the pane's own state decides delivery
        st, entry, pl_err = pane_state(pane)
        if st == "working":  # the wait gave up, the agent did not: the prompt is still being delivered
            reply({"ok": True, "delivered": True, "pane_state": "working", "pane": pane, "timed_out": True,
                 "timeout_ms": ms, "attempts": attempts, "herdr_error": why,
                 "pane_entry": entry, "fresh_session": fresh, **delivered_status(pane, role, round_kind == "rework"), **carried},
                  note="herdr status wait timed out; the pane is working")
            return
        if attempts == 1 and st in RETRY_PANE_STATES:  # idle/done: the prompt was lost — resend once
            time.sleep(retry_wait_s())
            attempts = 2
            continue
        if st in RETRY_PANE_STATES:
            # idle/done through a failed wait, about to report not delivered: ONE recheck, after
            # DISPATCH_RECHECK_S — a fresh pane list plus the pane text. The prompt marker visible
            # in NEW pane text (since_dispatch) means herdr's wait gave up while the prompt had
            # already landed → delivered:true, recovered:true says why
            time.sleep(recheck_wait_s())
            st, entry, pl_err = pane_state(pane)
            if marker_in_new_pane_text(pane, prompt_marker(text), pane_text_before):
                reply({"ok": True, "delivered": True, "recovered": True, "pane_state": st, "pane": pane,
                     "timed_out": True, "timeout_ms": ms, "attempts": attempts, "herdr_error": why,
                     "pane_entry": entry, "fresh_session": fresh,
                     **({"pane_list_error": pl_err} if pl_err else {}), **delivered_status(pane, role, round_kind == "rework"),
                     **carried},
                      note="herdr wait timed out; the prompt had already landed")
                return
        reply({"ok": False, "delivered": False, "pane_state": st, "pane": pane, "timed_out": True,
             "timeout_ms": ms, "attempts": attempts, "herdr_error": why, "error": why,
             "pane_entry": entry, "fresh_session": fresh, **({"pane_list_error": pl_err} if pl_err else {}),
             **carried})
        return


def cmd_arm(a):
    lane, pane = opt(a, "--lane"), opt(a, "--pane")
    task, tasks = opt(a, "--task"), opt(a, "--tasks")
    run, data, phase, grep = opt(a, "--run"), opt(a, "--data"), opt(a, "--phase"), opt(a, "--commit-grep")
    group = [t.strip() for t in (tasks or "").split(",") if t.strip()]
    if task and group:
        fail("arm takes --task (one task) or --tasks (a comma list), not both")
    if not (lane and pane and run and data and phase and grep) or not (task or group):
        fail("arm needs --lane --pane --task|--tasks --run --data --phase --commit-grep")
    if not os.path.exists(os.path.join(lane, ".git")):  # dir (main checkout) or file (linked worktree)
        fail(f"{lane} is not a git worktree (no .git)")
    try:
        re.compile(grep)
    except re.error as e:
        fail(f"--commit-grep does not compile: {e}")
    kind = opt(a, "--kind")
    if kind and kind not in lh.KINDS:
        fail(f"--kind must be one of {' '.join(lh.KINDS)}")
    try:
        stall = float(cfg("session.stall_min", opt(a, "--stall-min")))
    except ValueError as e:
        fail(f"--stall-min: {e}")
    lane = os.path.abspath(lane)
    ensure_lane_exclude(lane)
    now = time.time()
    listed = [task] if task else group  # a group lane: the pane lands on every listed task
    w = {"lane": lane, "pane": pane, "task": listed[0], "run": run, "data": os.path.abspath(data),
         "phase": phase, "commit_grep": grep, "report": opt(a, "--report"), "kind": kind,
         "prompt_marker": opt(a, "--prompt-marker"), "stall_min": stall,
         "armed_at": now, "last_activity": now, "last_pane_fp": None, "verdict": None, "verdict_at": None,
         **({"tasks": group} if group else {})}
    with locked(lane):
        write_watch(lane, w)
    errs = []
    for t in listed:  # best-effort, exactly one write per task, exactly as for a single --task
        rec = record_pane(t, pane, w["data"], run)
        if not rec.get("ok"):
            errs.append(f"{t}: {rec.get('error', 'mk-state task set failed')}")
    # the bus wiring rides along: bus.enabled false → no command at all; any failure is reported,
    # never fatal — the lane is armed with or without the bus
    bus = (arm_bus(lane, listed[0], run, w["data"]) if bus_enabled(w["data"])
           else {"ok": True, "skipped": "bus.enabled is false"})
    # the git guard is a PATH export that only reaches agents started after it: one already running
    # in the pane never has it (false); no agent yet is unknown (null) — never claimed true
    running = agent_in_pane(pane)
    guard = ({"guard_active": False,
              "guard_note": "an agent was already running when armed; the git guard on PATH only reaches "
                            f"agents started after the export — restart the agent after exporting "
                            f"{lane}/.matrixkoder-bin, or rely on the brief's banned-git list"}
             if running else {"guard_active": None})
    out({"ok": True, "lane": lane, "pane": pane, "watch": os.path.join(lane, WATCH), "armed_at": now,
         "bus": bus, **guard, **({"task_pane_error": "; ".join(errs)} if errs else {})})


def cmd_disarm(a):
    lane = opt(a, "--lane")
    if not lane:
        fail("disarm needs --lane")
    wf = os.path.join(lane, WATCH)
    with locked(lane):
        w = read_watch(lane)
        removed = os.path.exists(wf)
        if removed:
            os.remove(wf)
        wdata = (w or {}).get("data")
    enabled = (bus_enabled(wdata) if wdata  # the watch names the data dir its bus lives in
               else bool(cfg("bus.enabled")))  # no watch (or no data in it): the ambient resolve
    # Spec-literal: "bus.enabled: false -> neither runs". So a bus.enabled flipped to
    # false between arm and disarm leaves the lane token unrevoked on the bus (review
    # LOW) — accepted: the token only authenticates against this loopback bus, and the
    # owner-timeout shutdown is the backstop that eventually kills it with the server.
    bus = (disarm_bus(lane, wdata) if enabled
           else {"ok": True, "skipped": "bus.enabled is false"})
    out({"ok": True, "lane": lane, "removed": removed, "bus": bus})


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SEV_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
SEV_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


def review_verdict(text):
    """The verdict token after the last em dash of the title line.

    `templates/review.md` titles end `— APPROVE` or `— REQUEST CHANGES`. A title that
    merely mentions both words (the unfilled template, or "APPROVE (no REQUEST CHANGES
    needed)") matches neither.
    """
    head = (text or "").splitlines()[0] if text else ""
    tail = head.rsplit("\u2014", 1)[-1] if "\u2014" in head else head.rsplit(" - ", 1)[-1]
    token = tail.strip().rstrip(".").upper()
    if token == "REQUEST CHANGES":
        return "REQUEST CHANGES"
    if token == "APPROVE":
        return "APPROVE"
    return ""


def review_findings(text):
    """Findings under the exact single-severity headings of templates/review.md.

    A heading is `## CRITICAL` and nothing else: the template's own
    `## CRITICAL / HIGH / MEDIUM / LOW` line is not a heading. A finding starts with
    `- ` or `1. `; following indented lines stay with it. `###` headings and any other
    `##` heading end the section.
    """
    out, sev, cur = [], None, None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        m = re.fullmatch(r"##[ \t]+(CRITICAL|HIGH|MEDIUM|LOW)", line.strip())
        if m:
            if cur:
                out.append(cur)
                cur = None
            sev = m.group(1)
            continue
        if line.startswith("#"):
            if cur:
                out.append(cur)
                cur = None
            sev = None
            continue
        if not sev:
            continue
        bullet = re.match(r"^(?:- |\d+\. )(.*)$", line)
        if bullet:
            if cur:
                out.append(cur)
            cur = {"severity": sev, "text": bullet.group(1).strip()}
            continue
        if cur and (line.startswith(" ") or line.startswith("\t")) and line.strip():
            cur["text"] += " " + line.strip()
    if cur:
        out.append(cur)
    return out


def render_fix_section(findings, min_sev):
    """The FIX section of templates/brief.md, from the findings at or above min_sev.

    Findings below the minimum are listed and marked as not required.
    """
    floor = SEV_RANK.get(min_sev, SEV_RANK["LOW"])
    lines = ["## FIX (only for review findings)",
             "Findings: the review file — cite their numbers in the report."]
    n = 0
    for f in findings:
        if SEV_RANK[f["severity"]] <= floor:
            n += 1
            lines.append(f"{n}. [{f['severity']}] {f['text']}")
    listed = [f for f in findings if SEV_RANK[f["severity"]] > floor]
    if listed:
        lines.append("")
        lines.append(f"Listed, not required (--min {min_sev}):")
        for f in listed:
            lines.append(f"- [{f['severity']}] {f['text']}")
    if n == 0 and not listed:
        lines.append("(no findings)")
    return "\n".join(lines) + "\n"



def lane_review_for(data, run, task):
    """The newest `<lane>/.matrixkoder-review-rN.md`, or None. The lane is the task's."""
    lane = task_record(data, run, task).get("lane")
    if not lane or not os.path.isdir(lane):
        return None
    best, best_n = None, -1
    try:
        names = os.listdir(lane)
    except OSError:
        return None
    for name in names:
        m = re.fullmatch(r"\.matrixkoder-review-r(\d+)\.md", name)
        if m and int(m.group(1)) > best_n:
            best, best_n = os.path.join(lane, name), int(m.group(1))
    return best


def review_path_for(data, run, task):
    """The newest review-rN.md for the task, or None."""
    d = mkpaths.task_dir(data, run, task)
    best, best_n = None, -1
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for name in names:
        m = re.fullmatch(r"review-r(\d+)\.md", name)
        if m and int(m.group(1)) > best_n:
            best, best_n = os.path.join(d, name), int(m.group(1))
    return best


def newest_review(data, run, task):
    """todo 114: the newest ROUND wins, whatever its source. -> (round, text, ref, src).

    A stored round-N-1 `review` artifact must not shadow the lane's newer
    .matrixkoder-review-rN.md — that bug reported round N "already handed off" while the
    reviewer's fresh review sat unread in the lane. Candidates: the store's `review` artifact
    (C4), the task folder's review-rN.md and the lane's .matrixkoder-review-rN.md. Ties go to
    the store. `ref` names where the text came from: the task folder's copy of that round when
    it exists (the brief cites it), else the lane file, else the DB location — never a file of
    another round. `src` is the lane file to copy in when the text came from there, else None.
    """
    cands = []  # (round, store-first rank, text, task-folder path, lane file)
    got = newest_stored_artifact(data, run, task, "review")
    n_file, text_file = None, None
    path = review_path_for(data, run, task)
    if path:
        n_file = review_round_of(path)
        try:
            text_file = open(path).read()
        except OSError:
            n_file, text_file = None, None
    if got:
        cands.append((got[0], 0, got[1], path if n_file == got[0] else None, None))
    if n_file is not None:
        cands.append((n_file, 1, text_file, path, None))
    lane_path = lane_review_for(data, run, task)
    if lane_path:
        n_lane = review_round_of(lane_path)
        try:
            text_lane = open(lane_path).read()
        except OSError:
            text_lane = None
        if n_lane is not None and text_lane is not None:
            cands.append((n_lane, 2, text_lane, path if n_file == n_lane else None, lane_path))
    if not cands:
        return None, None, None, None
    n, _rank, text, cited, src = max(cands, key=lambda c: (c[0], -c[1]))
    ref = cited or src or f"project DB review artifact (round {n}, task {task})"
    return n, text, ref, src


def review_body_for(data, run, task):
    """The newest review for the task as (round, text, ref), or (None, None, None). See
    newest_review: the newest ROUND wins whatever its source (todo 114)."""
    n, text, ref, _src = newest_review(data, run, task)
    return n, text, ref


def send_brief_over_bus(data, run, task, text, round_n):
    """The orchestrator sends the FIX brief to the builder. Store mode stores it as a
    brief artifact (connect reads that); file mode sends it as info. Never a lane-to-lane
    message: the sender is the orchestrator token."""
    body = brief_message(round_n, text)
    if os.path.isfile(mkpaths.project_db(data=data)):
        return run_bus(["send", "--data", data, "--run", run, "--task", task,
                        "--kind", "task", "--from", "orch", "--to", task, "--text", body])
    return run_bus(["send", "--data", data, "--run", run, "--task", task,
                    "--kind", "info", "--from", "orch", "--to", task, "--text", body])


def set_children_fix(data, run, task):
    """The task's children (or the task itself) go to status fix. Failures are reported."""
    kids = child_ids(task, data, run)
    targets = kids or [task]
    done, errors = [], []
    for t in targets:
        r = subprocess.run(
            [sys.executable, mk_state_cmd(), "--data", data, "--run", run,
             "task", "set", t, "status=fix", "--by", "review-handoff"],
            capture_output=True, text=True)
        try:
            j = json.loads(r.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            j = None
        if r.returncode != 0 or not isinstance(j, dict) or not j.get("ok"):
            errors.append(f"{t}: {(r.stderr or r.stdout).strip()[-160:] or f'exit {r.returncode}'}")
        else:
            done.append(t)
    return done, errors


def wake_orchestrator(data, run, task, round_n, hold):
    """A short veto window for the orchestrator: one info message, held when --hold is set.
    There is no pane here; the message is the wake."""
    text = (f"review-handoff {task} round {round_n}: fix opened"
            + ("; hold for veto" if hold else ""))
    return run_bus(["send", "--data", data, "--run", run, "--task", task,
                    "--kind", "info", "--from", "orch", "--to", "orch", "--text", text])


def task_round_of(data, run, task):
    """The task's current round, or 0 when the task cannot be read."""
    r = subprocess.run(
        [sys.executable, mk_state_cmd(), "--data", data, "--run", run, "task", "get", task],
        capture_output=True, text=True)
    try:
        j = json.loads(r.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return 0
    if r.returncode != 0 or not isinstance(j, dict) or not j.get("ok"):
        return 0
    return int((j.get("task") or {}).get("round") or 0)


def review_round_of(path):
    """The N in review-rN.md or .matrixkoder-review-rN.md, or None."""
    base = os.path.basename(path or "")
    m = re.fullmatch(r"(?:\.matrixkoder-)?review-r(\d+)\.md", base)
    return int(m.group(1)) if m else None


def hold_path(data, run, task):
    return os.path.join(mkpaths.task_dir(data, run, task), ".matrixkoder-handoff.json")


def task_record(data, run, task):
    """The task dict from `mk-state task get`, or {} when it cannot be read."""
    r = subprocess.run(
        [sys.executable, mk_state_cmd(), "--data", data, "--run", run, "task", "get", task],
        capture_output=True, text=True)
    try:
        j = json.loads(r.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {}
    return j.get("task") or {} if isinstance(j, dict) and j.get("ok") else {}


def git_out(repo, *args):
    """One line of git output from <repo>, or '' when git cannot answer."""
    if not repo:
        return ""
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


# F.4: the orchestrator-side risk reasons this tool opens, with their points (mk-state `risk add`
# is the only writer; a builder command never calls it)
RISK_POINTS = {"review_changes": 2, "medium_parked": 1, "claim_mismatch": 2, "no_evidence": 1}


def open_risk(data, run, task, kind, source):
    """Open one risk reason through `mk-state risk add`. The same open kind+source on the task
    is not added twice. -> (opened, error)."""
    base = [sys.executable, mk_state_cmd(), "--data", data, "--run", run]
    listed = subprocess.run(base + ["risk", "list", task], capture_output=True, text=True).stdout.strip().splitlines()
    lst = _jload(listed[-1] if listed else "") or {}
    for x in lst.get("reasons") or []:
        if x.get("kind") == kind and (x.get("source") or "") == source and not x.get("cleared"):
            return False, None
    r = subprocess.run(base + ["risk", "add", task, "--kind", kind, "--points", str(RISK_POINTS[kind]),
                               "--source", source], capture_output=True, text=True)
    lines = r.stdout.strip().splitlines()
    j = _jload(lines[-1] if lines else "")
    if r.returncode == 0 and isinstance(j, dict) and j.get("ok"):
        return True, None
    return False, ((j or {}).get("error") or r.stderr.strip()[-160:] or f"exit {r.returncode}")


def review_acceptance(text):
    """The rows of the review's `## Acceptance` table as [{criterion, verdict}]. A row counts
    when its verdict cell is exactly MET, NOT MET or UNVERIFIED (the template's placeholder
    row and the header do not). [] when the section or its rows are missing."""
    m = re.search(r"(?m)^##[ \t]+Acceptance[ \t]*$", text or "")
    if not m:
        return []
    body = text[m.end():]
    nxt = re.search(r"(?m)^##[ \t]", body)
    rows = []
    for line in (body[:nxt.start()] if nxt else body).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.strip().startswith("|") and len(cells) >= 3 and cells[2].upper() in ("MET", "NOT MET", "UNVERIFIED"):
            rows.append({"criterion": cells[1], "verdict": cells[2].upper()})
    return rows


# The same claim rule is in mk-builder.py `done` (a lane script, so it cannot import this one):
# a backticked test file in a report clause (split on `;`, `. `, newline) that says it was added/new/wrote/created is a claim.
CLAIM_TEST_RE = re.compile(r"`([\w./-]*(?:test_[\w.-]+\.py|[\w.-]*_test\.py|[\w.-]+\.test\.tsx?))`")
CLAIM_VERB_RE = re.compile(r"\b(added|adds|new|wrote|writes|created|creates)\b", re.I)


def claimed_tests(report):
    out = []
    for clause in re.split(r";|\. |\n", report or ""):
        if CLAIM_VERB_RE.search(clause):
            out += [t for t in CLAIM_TEST_RE.findall(clause) if t not in out]
    return out


def claim_in(test, files):
    """A claimed test matches a changed path when it equals it, is a path suffix of it, or equals
    its basename (a bare `plans-map.test.ts`). Same rule as mk-builder.py `done`."""
    return any(f == test or f.endswith("/" + test) or os.path.basename(f) == os.path.basename(test)
               and "/" not in test for f in files)


def branch_files(lane):
    """Files the lane's commits changed since the merge-base with main (or master); None when
    git cannot say, so an unknown never reads as a mismatch."""
    for base in ("main", "master"):
        mb = git_out(lane, "merge-base", "HEAD", base)
        if mb:
            try:
                r = subprocess.run(["git", "-C", lane, "diff", "--name-only", f"{mb}..HEAD"],
                                   capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                return None
            return set(r.stdout.splitlines()) if r.returncode == 0 else None  # a git error is unknown, not empty
    return None


def ingest_report(w):
    """F.4: when a lane finishes, the report's claimed tests must be in the branch diff; a claimed
    test that is absent opens a claim_mismatch reason. Best-effort: never raises. -> missing list."""
    try:
        data, run, task, lane = w.get("data"), w.get("run"), w.get("task"), w.get("lane")
        if not (data and run and task and lane):
            return []
        text, rnd = None, "?"
        path = os.path.join(lane, w.get("report") or "")
        if w.get("report") and os.path.isfile(path):
            text = open(path).read()
            m = re.search(r"-r(\d+)\.md$", os.path.basename(path))
            if m:
                rnd = m.group(1)
        else:
            got = newest_stored_artifact(data, run, task, "report")
            if got:
                rnd, text = got
        files = branch_files(lane)
        if not text or files is None:
            return []
        missing = [t for t in claimed_tests(text) if not claim_in(t, files)]
        if missing:
            open_risk(data, run, task, "claim_mismatch", f"report R{rnd}: {', '.join(missing)}"[:200])
        return missing
    except Exception:
        return []


def rules_block_of(text):
    """The ## Rules section of a brief, header included, or '' when there is none."""
    lines = (text or "").splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == "## Rules"), None)
    if start is None:
        return ""
    out = []
    for line in lines[start:]:
        if out and line.startswith("## "):
            break
        out.append(line)
    return "\n".join(out).strip()


def commands_block_of(text):
    """The ## Commands section of a brief, header included, or '' when there is none."""
    lines = (text or "").splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("## Commands")), None)
    if start is None:
        return ""
    out = []
    for line in lines[start:]:
        if out and line.startswith("## "):
            break
        out.append(line)
    return "\n".join(out).strip()


def risk_gates_section(data, run, task, rec):
    """F.4: the brief's `## Risk and gates` section from the store: the derived risk with its open
    reasons (`mk-state risk list`) and the task's gates, so a fix or rework brief shows both."""
    listed = subprocess.run([sys.executable, mk_state_cmd(), "--data", data, "--run", run, "risk", "list", task],
                            capture_output=True, text=True).stdout.strip().splitlines()
    j = _jload(listed[-1] if listed else "") or {}
    reasons = [x for x in j.get("reasons") or [] if not x.get("cleared")]
    risk = (j.get("risk") or {}).get(task) or rec.get("risk") or 1
    why = "; ".join(f"{x.get('kind')} +{x.get('points')} ({x.get('source') or '-'})" for x in reasons)
    gates = rec.get("gates")
    if isinstance(gates, (list, tuple)):
        gate_lines = [f"- {g}" for g in gates] or ["- none"]
    elif isinstance(gates, dict):
        gate_lines = [f"- {k}: {v}" for k, v in gates.items()] or ["- none"]
    else:
        gate_lines = [f"- {gates}" if gates else "- none"]
    return "\n".join(["## Risk and gates", f"- Risk: {risk}" + (f" — {why}" if why else ", no open reasons"),
                      "- Gates:", *["  " + g for g in gate_lines],
                      "- Risk is the orchestrator's: a builder never writes a risk reason.", ""])


def filled_brief(data, run, task, round_n, review_ref, fix, review_n=None):
    """A fix brief filled from the task, never the raw template.

    Repo, worktree, branch and sha come from the task's lane (git). Owned files come from
    the task. The commit subject, the report path and the test/rules lines are copied from
    the previous round's brief when it has them. The FIX section cites the review (`review_ref`,
    a file path or the DB location) and lists each finding. `review_n` is the review's round
    when `review_ref` is not a filename to parse.
    """
    rec = task_record(data, run, task)
    lane = rec.get("lane") or rec.get("workspace") or ""
    branch = rec.get("branch") or (git_out(lane, "rev-parse", "--abbrev-ref", "HEAD") if lane else "")
    sha = git_out(lane, "rev-parse", "--short", "HEAD") if lane else ""
    owned = rec.get("owned") or []
    owned_txt = ", ".join(owned) if isinstance(owned, list) else str(owned)
    prev_text, prev_n = brief_body_for(data, run, task)
    prev_text = prev_text or ""
    report = ""
    m = re.search(r"(?m)^- Report: write the report to `([^`]+)`", prev_text)
    if not m:
        m = re.search(r"(?m)^Report: (.+)$", prev_text)
    if m:
        report = m.group(1).strip().strip("`")
    commit = ""
    m = re.search(r"(?m)^- Commit: `([^`]+)`", prev_text)
    if m:
        commit = m.group(1).strip()
    rules = rules_block_of(prev_text)
    commands = commands_block_of(prev_text)
    # the co-author is filled, never left as <agent/model>: the previous brief's commit
    # line names it, else the task's agent record
    coauthor = ""
    cm = re.search(r"Co-Authored-By:\s*(\S[^\n]*?)\.?\s*$", prev_text)
    if cm and "<agent/model>" not in cm.group(1):
        coauthor = cm.group(1).strip().rstrip(".")
    if not coauthor:
        agent = rec.get("agent") or "the builder"
        coauthor = f"{agent} <noreply@matrixkoder.local>"
    # a fix round does not reuse the previous subject unchanged
    if commit:
        rest = re.sub(r"^[a-z]+\([^)]*\):\s*", "", commit)
        commit = f"fix({task.lower()}): {rest}"
    review_n = review_n if review_n is not None else review_round_of(review_ref)
    lines = [
        f"# {task} — FIX round {round_n}: review R{review_n if review_n is not None else ''}",
        "",
        f"Repo: this folder (worktree `{lane or '(no lane recorded)'}`, branch `{branch or '(none)'}`, at `{sha or '(none)'}`).",
        f"Plan phase task `{task}`. History: `{data}/runs/{run}/tasks/{task}/` (read-only). Do ONLY these fixes.",
        "",
        "## Files you own",
        owned_txt or "(none recorded on the task)",
        "Do NOT touch: everything else.",
        "",
        risk_gates_section(data, run, task, rec),
        f"## FIX (review `{review_ref}`)",
        fix.strip(),
        "",
    ]
    if commands:
        lines += [commands, ""]
    if rules:
        lines += [rules, ""]
    # append Commit/Report only when the copied blocks do not already carry them
    copied = "\n".join(x for x in (commands, rules) if x)
    if not re.search(r"(?m)^- Commit:", copied):
        lines.append(f"- Commit: `{commit or 'fix: review findings'}` subject, blank line, "
                     f"`Co-Authored-By: {coauthor}`.")
    if not re.search(r"(?m)^- Report:", copied):
        lines.append(f"- Report: write the report to `{report or '(set by the orchestrator)'}` "
                     f"(cite the review findings).")
    lines.append("")
    return "\n".join(lines)


def compose_brief(brief_path, fix):
    """Append the FIX section to the brief that already exists. A missing brief starts
    from templates/brief.md. Never replaces a brief the orchestrator already wrote."""
    existing = ""
    if brief_path and os.path.isfile(brief_path):
        try:
            existing = open(brief_path).read()
        except OSError:
            existing = ""
    if not existing.strip():
        tmpl = os.path.join(HERE, "..", "skills", "matrixkoder", "templates", "brief.md")
        try:
            existing = open(os.path.normpath(tmpl)).read()
        except OSError:
            existing = ""
    body = existing.rstrip("\n")
    if "## FIX (only for review findings)" in body:
        body = body.split("## FIX (only for review findings)", 1)[0].rstrip("\n")
    return (body + "\n\n" + fix) if body else fix


def cmd_review_handoff(a):
    """REQUEST CHANGES opens the fix round and sends the FIX brief to the builder through
    the orchestrator. APPROVE only records the verdict. The reviewer never messages the builder.

    Idempotent: acts only when the review's N equals the task's current round and no fix
    round for that N exists yet. --hold records the pending handoff and nothing else; the
    next call without --hold releases it.
    """
    task = opt(a, "--task")
    if not task or not ID_RE.fullmatch(task):
        fail("review-handoff needs a valid --task id")
    data = opt(a, "--data") or cfg_data()
    run = opt(a, "--run")
    if not run or not ID_RE.fullmatch(run):
        fail("review-handoff needs a valid --run id")
    hold = "--hold" in a
    min_sev = (opt(a, "--min", "LOW") or "LOW").upper()
    if min_sev not in SEV_RANK:
        fail("--min must be one of CRITICAL HIGH MEDIUM LOW")
    path = opt(a, "--review")
    if path:
        n = review_round_of(path)
        if n is None:
            fail("review file is not review-rN.md")
        try:
            text, ref = open(path).read(), path
        except OSError as e:
            fail(f"review unreadable: {e}")
    else:
        # todo 114: the newest ROUND wins, whatever its source — a stored review of round
        # N-1 must not shadow the lane's fresh .matrixkoder-review-rN.md and report round N
        # "already handed off".
        n, text, ref, src = newest_review(data, run, task)
        if text is None:
            lane = (task_record(data, run, task).get("lane") or "<lane>")
            fail(f"no review file for {task}: looked in "
                 f"{mkpaths.task_dir(data, run, task)} and "
                 f"{lane}/.matrixkoder-review-rN.md")
        if src:
            # the reviewer writes into its own lane; copy that back into the task folder
            # under the plain name, and the brief cites the copy so it survives a lane reset
            dest_dir = mkpaths.task_dir(data, run, task)
            os.makedirs(dest_dir, exist_ok=True)
            path = os.path.join(dest_dir, f"review-r{n}.md")
            if not os.path.isfile(path):
                shutil.copyfile(src, path)
            ref = path
    verdict = review_verdict(text)
    # F.4: the acceptance table is part of a review; one without it is flagged on every reply
    rows = review_acceptance(text)
    table = {"acceptance_table": bool(rows),
             **({} if rows else {"flag": "review has no acceptance table"})}
    findings_all = review_findings(text)
    if verdict == "APPROVE":
        r = subprocess.run(
            [sys.executable, mk_state_cmd(), "--data", data, "--run", run,
             "task", "set", task, "verdict=approve", "--by", "review-handoff"],
            capture_output=True, text=True)
        wanted = []
        if any(x["verdict"] == "UNVERIFIED" for x in rows):
            wanted.append("no_evidence")  # APPROVE over an UNVERIFIED row is approval without evidence
        if any(f["severity"] == "MEDIUM" for f in findings_all):
            wanted.append("medium_parked")  # an APPROVE parks every MEDIUM it lists
        risk = open_risks(data, run, task, wanted, f"review R{n}")
        out({"ok": True, "verdict": "APPROVE", "dispatched": False, "task": task,
             "recorded": r.returncode == 0, **table, **risk})
        return
    if verdict != "REQUEST CHANGES":
        fail("review title has neither APPROVE nor REQUEST CHANGES")
    cur = task_round_of(data, run, task)
    if cur != n:
        out({"ok": True, "dispatched": False, "task": task, "round": cur, **table,
             "reason": "already handed off" if cur > n else "review round is not the current round"})
        return
    findings = findings_all
    if not findings:
        out({"ok": True, "dispatched": False, "task": task, "round": cur, "findings": 0, **table,
             "reason": "no findings; held for the orchestrator"})
        return
    pending = hold_path(data, run, task)
    if hold:
        # Record the pending handoff and nothing else: no round, no brief, no send.
        blob = {"task": task, "run": run, "review": ref, "round": n,
                "min": min_sev, "cause": opt(a, "--cause", "verification gap")}
        try:
            os.makedirs(os.path.dirname(pending), exist_ok=True)
            with open(pending, "w") as f:
                json.dump(blob, f)
        except OSError as e:
            out({"ok": False, "error": f"hold write failed: {e}", "task": task})
            return
        out({"ok": True, "verdict": "REQUEST CHANGES", "dispatched": False, "held": True,
             "task": task, "round": n, "findings": len(findings), **table,
             "reason": "held for veto"})
        return
    if os.path.isfile(pending):
        try:
            os.unlink(pending)
        except OSError:
            pass
    cause = opt(a, "--cause", "verification gap")
    rnd = subprocess.run(
        [sys.executable, mk_state_cmd(), "--data", data, "--run", run,
         "task", "round", task, "--kind", "fix", "--cause", cause, "--by", "review-handoff"],
        capture_output=True, text=True)
    try:
        opened = json.loads(rnd.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        opened = {"ok": False, "error": (rnd.stderr or rnd.stdout).strip()[-200:]}
    if rnd.returncode != 0 or not opened.get("ok"):
        out({"ok": False, "error": opened.get("error") or "task round failed", "task": task})
        return
    # F.4: opened before the brief is filled, so the brief shows them: changes were requested → review_changes; MEDIUMs below --min are parked → medium_parked
    floor = SEV_RANK[min_sev]
    wanted = ["review_changes"]
    if any(f["severity"] == "MEDIUM" and SEV_RANK["MEDIUM"] > floor for f in findings):
        wanted.append("medium_parked")
    risk = open_risks(data, run, task, wanted, f"review R{n}")
    fix = render_fix_section(findings, min_sev)
    brief_path = opened.get("brief")
    written = filled_brief(data, run, task, opened.get("round"), ref, fix, n)
    if brief_path:
        try:
            with open(brief_path, "w") as f:
                f.write(written)
        except OSError as e:
            out({"ok": False, "error": f"brief write failed: {e}", "task": task})
            return
    sent, send_err = send_brief_over_bus(data, run, task, written, opened.get("round"))
    fixed, fix_err = set_children_fix(data, run, task)
    wake, wake_err = wake_orchestrator(data, run, task, opened.get("round"), False)
    out({"ok": True, "verdict": "REQUEST CHANGES", "dispatched": True, "task": task, **table, **risk,
         "round": opened.get("round"), "kind": "fix", "brief": brief_path,
         "findings": len(findings), "required": sum(
             1 for f in findings if SEV_RANK[f["severity"]] <= SEV_RANK[min_sev]),
         "children": fixed, "sent": bool(sent and not send_err),
         "held": False, "wake": bool(wake and not wake_err),
         **({"send_error": send_err} if send_err else {}),
         **({"children_error": "; ".join(fix_err)} if fix_err else {}),
         **({"wake_error": wake_err} if wake_err else {})})


def open_risks(data, run, task, kinds, source):
    """open_risk for each kind -> {"risk_opened": [...]} plus risk_error when one failed."""
    opened, errs = [], []
    for k in kinds:
        ok, err = open_risk(data, run, task, k, source)
        if ok:
            opened.append(k)
        if err:
            errs.append(f"{k}: {err}")
    return {"risk_opened": opened, **({"risk_error": "; ".join(errs)} if errs else {})}


def cfg_data():
    """The ambient data dir, for a review-handoff that passed no --data."""
    try:
        return mk_config.mk_state.resolve_data(None)
    except Exception:
        return os.path.abspath(".matrixkoder")


def cmd_event():
    ev = os.environ.get("HERDR_PLUGIN_EVENT") or ""
    try:
        j = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
    except ValueError:
        j = {}
    data = j.get("data") if isinstance(j.get("data"), dict) else (j if isinstance(j, dict) else {})
    pane = data.get("pane_id")
    if not pane:
        out({"ok": True, "ignored": "event without pane_id"})
        return
    exited = "exit" in ev or j.get("event") == "pane_exited" or data.get("type") == "pane_exited"
    hit = find_armed(pane)
    if not hit:
        out({"ok": True, "ignored": "not an armed lane"})  # most panes on the machine end here
        return
    lane = hit[0]
    with locked(lane):
        w = read_watch(lane)
        if not w:
            out({"ok": True, "ignored": "not an armed lane"})
            return
        if w.get("verdict") == "DONE":  # finished stays finished; later events are noise
            out({"ok": True, "pane": pane, "lane": lane, "verdict": "DONE", "noop": True})
            return
        if exited:
            if w.get("verdict"):  # exit after a verdict is post-finish cleanup, not a stall
                out({"ok": True, "pane": pane, "lane": lane, "verdict": w["verdict"], "noop": True})
                return
            rec, label, toast, status = apply_verdict(w, "STALL", lw.commit_subject(lane),
                                                      ["agent exited"], None)
            if not rec.get("ok"):
                out({"ok": False, "pane": pane, "lane": lane, "error": f"mk-state: {rec.get('error')}"})
                sys.exit(2)
            out({"ok": True, "pane": pane, "lane": lane, "verdict": "STALL", "status": status,
                 "recorded": True, "label": label, "toast": toast})
            return
        if data.get("agent_status") == "working":
            w["last_activity"] = time.time()  # the agent is alive; that is all this proves
            write_watch(lane, w)
            out({"ok": True, "pane": pane, "lane": lane, "touched": True})
            return
        h, subj, state, evidence = verdict_for(w)
        if state == "ERROR":  # one bad probe is not a verdict; the next event or sweep retries
            write_watch(lane, w)
            out({"ok": True, "pane": pane, "lane": lane, "error": "; ".join(h.get("evidence") or [])[:200]})
        elif state is None:  # a finished turn is not a finished task: keep watching
            write_watch(lane, w)
            out({"ok": True, "pane": pane, "lane": lane, "verdict": None,
                 "note": "no verdict: turn ended, task not finished"})
        elif not overrides(w.get("verdict"), state):
            write_watch(lane, w)
            out({"ok": True, "pane": pane, "lane": lane, "verdict": state, "noop": True})
        else:
            rec, label, toast, status = apply_verdict(w, state, subj, evidence, h)
            if not rec.get("ok"):
                out({"ok": False, "pane": pane, "lane": lane, "verdict": state,
                     "error": f"mk-state: {rec.get('error')}"})
                sys.exit(2)
            out({"ok": True, "pane": pane, "lane": lane, "verdict": state, "status": status,
                 "recorded": True, "label": label, "toast": toast,
                 **({"claim_mismatch": rec["claim_mismatch"]} if rec.get("claim_mismatch") else {})})


def dirty_files(lane):
    """Lines of `git status --porcelain` in the lane (agent-independent alive signal); None when git fails."""
    try:
        r = subprocess.run(["git", "-C", lane, "status", "--porcelain"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return len(r.stdout.splitlines()) if r.returncode == 0 else None


def cmd_sweep(a):
    data = opt(a, "--data")
    checked, skipped, verdicts, lanes = 0, 0, [], []
    for wt, _ in armed_lanes(data and os.path.abspath(data)):
        if not os.path.isdir(wt):
            continue
        with locked(wt):
            w = read_watch(wt)
            if not w:
                continue
            if w.get("verdict"):  # already decided; the orchestrator disarms when it has acted
                skipped += 1
                continue
            checked += 1
            dirty = dirty_files(wt)
            lanes.append({"lane": wt, "task": w.get("task"), "dirty_files": dirty})
            try:
                h, subj, state, evidence = verdict_for(w)
            except Exception as e:
                verdicts.append({"lane": wt, "task": w.get("task"), "error": str(e)[:120]})
                continue
            if state == "ERROR":  # dead pane / herdr down: retried on the next sweep
                write_watch(wt, w)
                continue
            if state is None:
                write_watch(wt, w)
                continue
            rec, label, toast, status = apply_verdict(w, state, subj, evidence, h)
            entry = {"lane": wt, "task": w.get("task"), "verdict": state,
                     "status": status, "recorded": rec.get("ok", False)}
            if state == "STALL" and dirty:
                entry["alive_hint"] = f"the lane has {dirty} modified files; it may be working in a subagent"
            verdicts.append(entry)
    out({"ok": True, "checked": checked, "skipped": skipped, "lanes": lanes, "verdicts": verdicts})


def self_test():
    import shutil, socketserver, tempfile, threading
    # overrides(): the no-op / recovery rules for repeated and late events
    assert overrides(None, "STALL") and overrides(None, "DONE")
    assert overrides("STALL", "STALL") is False and overrides("DONE", "DONE") is False
    assert overrides("DONE", "STALL") is False and overrides("STALL", "DONE")
    assert overrides("QUOTA", "BLOCKED") and overrides("BLOCKED", "DONE")
    assert pane_fp("Working (12s) esc") == pane_fp("Working (73s) esc")  # timers are not activity
    assert pane_fp("$ vitest run") != pane_fp("$ vitest run\n 10 failed")
    # dispatch retry contract: the default wait is 5 s, read from $MK_DISPATCH_RETRY_S at call time
    assert RETRY_PANE_STATES == ("idle", "done") and DISPATCH_RETRY_S == 5
    assert retry_wait_s() == 5.0
    # dispatch recheck contract: the default wait is 3 s, read from $MK_DISPATCH_RECHECK_S at call
    # time; the marker is --text's first line (up to the first newline), capped at 60 chars
    assert DISPATCH_RECHECK_S == 3
    assert recheck_wait_s() == 3.0
    assert prompt_marker("Read brief.md\nthen stop") == "Read brief.md"
    assert prompt_marker("  go  ") == "go" and prompt_marker("go\nthink again") == "go"
    assert prompt_marker("") == "" and prompt_marker(" \n ") == ""
    assert prompt_marker("a" * 200) == "a" * 60  # capped at 60 chars

    # no flag and no yaml: the stall default arm/verdict_for now use is the mk-config schema's
    empty = tempfile.mkdtemp(prefix="mk-event-cfg-")
    old_md = os.environ.get("MATRIXKODER_DATA")
    os.environ["MATRIXKODER_DATA"] = empty
    try:
        assert cfg("session.stall_min") == mk_config.value_of(
            [], mk_config.row_for([], "session.stall_min"))[1], cfg("session.stall_min")
        assert float({}.get("stall_min") or cfg("session.stall_min")) == 15.0  # verdict_for's fallback
        assert cfg("session.stall_min", "9") == "9"  # a given flag wins
    finally:
        if old_md is None:
            os.environ.pop("MATRIXKODER_DATA", None)
        else:
            os.environ["MATRIXKODER_DATA"] = old_md
        shutil.rmtree(empty, ignore_errors=True)

    tmp = tempfile.mkdtemp(prefix="mk-lane-event-test-")
    home, data = os.path.join(tmp, "home"), os.path.join(tmp, "data")
    os.makedirs(home)
    # One temp MK home under every name (C16): mk-bus children read $MK_BUS_DIR/$MK_HOME.
    os.environ["MK_HOME"] = home
    os.environ["MATRIXKODER_HOME"] = home
    os.environ["MK_BUS_DIR"] = home
    os.environ["MK_SELFTEST"] = "1"
    # every dispatch resolves the data dir through cfg(); pin it so no case reads a real config
    restore_data = mk_config.pin_tmp_data(tmp)
    # never drop this pop: a test run inside a herdr pane inherits the REAL herdr via this env var
    os.environ.pop("HERDR_BIN_PATH", None)
    try:
        # a real repo + real linked worktree lanes, registered like the wizard does
        repo = os.path.join(tmp, "repo")
        subprocess.run(["git", "init", "-q", repo], check=True)
        gid = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

        def g(dir_, *args, dates=None, code=0):
            env = dict(os.environ, **gid, **(dates or {}))
            r = subprocess.run(["git", "-C", dir_, *args], capture_output=True, text=True, env=env)
            assert r.returncode == code, (args, r.stdout, r.stderr)

        g(repo, "commit", "-q", "--allow-empty", "-m", "chore: init")
        ex_dir = os.path.join(repo, ".git", "info")
        os.makedirs(ex_dir, exist_ok=True)
        with open(os.path.join(ex_dir, "exclude"), "a") as f:
            f.write(".matrixkoder-*\n")
        tasks = ("H1", "H2", "H3", "H4", "H5", "H6", "H7", "T1", "T2")  # T1/T2: MB3.2 status-file cases
        lanes = {}
        for tid in tasks:
            lanes[tid] = os.path.join(tmp, "lane-" + tid.lower())
            g(repo, "worktree", "add", "-q", "--detach", lanes[tid])
        json.dump({repo: {"data_dir": data}}, open(os.path.join(home, "projects.json"), "w"))

        # run state through the real mk-state
        MK = os.path.join(HERE, "mk-state.py")

        def mks(*args):
            r = subprocess.run([sys.executable, MK, *args, "--data", data], capture_output=True, text=True)
            assert r.returncode == 0, r.stdout + r.stderr

        mks("run", "init", "--id", "r1", "--feature", "F", "--branch", "b")
        for tid in tasks:
            mks("task", "add", "--root", "--id", tid, "--title", tid)
            mks("task", "set", tid, "status=building")

        # L4 (C4): there is no runs/<run>/run.json or events.jsonl any more — the store's
        # tasks/events rows are the record. These harness readers open it read-only.
        def store_read(sql, params=()):
            path = os.path.join(data, "mk.db")
            if not os.path.isfile(path):
                return []
            con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
            con.row_factory = sqlite3.Row
            try:
                return [dict(r) for r in con.execute(sql, params)]
            finally:
                con.close()

        def task_status(tid):
            rows = store_read("SELECT status FROM tasks WHERE run='r1' AND lower(id)=lower(?)", (tid,))
            return rows[0]["status"] if rows else None

        def events():
            out = []
            for r in store_read("SELECT task, kind, from_id, to_id, text, meta_json FROM events"
                                " WHERE run='r1' AND chan='state' ORDER BY id"):
                by = None
                if r.get("meta_json"):
                    with contextlib.suppress(ValueError):
                        by = json.loads(r["meta_json"]).get("by")
                out.append({"task": r["task"], "type": r["kind"], "from": r["from_id"],
                            "to": r["to_id"], "text": r["text"], "by": by})
            return out

        def transitions():
            return [e for e in events() if e.get("type") == "transition" and e.get("from")]

        def watch_of(tid):
            return json.load(open(os.path.join(lanes[tid], WATCH)))

        def patch_watch(tid, **kw):
            w = watch_of(tid)
            w.update(kw)
            json.dump(w, open(os.path.join(lanes[tid], WATCH), "w"), indent=2)

        def wstat(tid, state, updated_at, **kw):
            # a contract-valid v1 status file (status-contract.md Table 1) into the lane; kw overrides
            d = {"v": 1, "seq": 1, "run": "r1", "task": tid, "round": 1, "role": "builder",
                 "agent": "opencode", "state": state, "step": "selftest", "phases_done": [],
                 "question": None, "commit": None, "report": "report.json", "subagent": None,
                 "ctx_pct": None, "started_at": updated_at, "updated_at": updated_at}
            d.update(kw)
            json.dump(d, open(os.path.join(lanes[tid], ".matrixkoder-status.json"), "w"))

        # fake herdr on PATH: agent get/read answer from files, notification calls are recorded
        bin_ = os.path.join(tmp, "bin")
        os.makedirs(bin_)
        pane_f, status_f, notes_f, sock_log = (os.path.join(tmp, x) for x in
                                               ("pane.txt", "status.txt", "notes.log", "sock.log"))
        open(status_f, "w").write("idle")
        open(pane_f, "w").write("")
        with open(os.path.join(bin_, "herdr"), "w") as f:
            f.write("#!/usr/bin/env python3\n"
                    "import json, os, sys\n"
                    "a = sys.argv[1:]\n"
                    "base = os.environ['FAKE_HERDR_DIR']\n"
                    "if a[:2] == ['agent', 'get']:\n"
                    "    st = open(os.path.join(base, 'status.txt')).read().strip() or 'idle'\n"
                    "    print(json.dumps({'result': {'agent': {'agent_status': st}}}))\n"
                    "elif a[:2] == ['agent', 'read']:\n"
                    "    sys.stdout.write(open(os.path.join(base, 'pane.txt')).read())\n"
                    "elif a[:2] == ['notification', 'show']:\n"
                    "    open(os.path.join(base, 'notes.log'), 'a').write(' '.join(a[2:]) + chr(10))\n"
                    "    print(json.dumps({'result': {'ok': True}}))\n"
                    "elif a[:2] == ['agent', 'prompt']:\n"
                    "    log = os.path.join(base, 'calls.log')\n"
                    "    open(log, 'a').write(' '.join(a) + chr(10))\n"
                    "    # every prompt call appends the prompt text to pane.txt for the since_dispatch\n"
                    "    # recheck. `no_pane.txt` suppresses the append for tests that need the pane\n"
                    "    # text to stay static across attempts.\n"
                    "    no_pane = os.path.join(base, 'no_pane.txt')\n"
                    "    if not os.path.exists(no_pane):\n"
                    "        ptxt = a[3] if len(a) > 3 else ''\n"
                    "        if ptxt:\n"
                    "            with open(os.path.join(base, 'pane.txt'), 'a') as _pf:\n"
                    "                _pf.write(ptxt + chr(10))\n"
                    "    hang = os.path.join(base, 'hang.txt')\n"
                    "    if os.path.exists(hang):\n"
                    "        import time; time.sleep(int(open(hang).read().strip()))\n"
                    "    sfx = ''\n"
                    "    q = os.path.join(base, 'prompt_q.txt')\n"
                    "    if os.path.exists(q):\n"
                    "        toks = [t.strip() for t in open(q).read().split()]\n"
                    "        if toks:\n"
                    "            sfx = '' if toks[0] == '-' else toks[0]\n"
                    "            open(q, 'w').write(' '.join(toks[1:]))\n"
                    "    def canned(nm):\n"
                    "        p = os.path.join(base, nm + sfx + '.txt')\n"
                    "        return open(p).read() if os.path.exists(p) else open(os.path.join(base, nm + '.txt')).read()\n"
                    "    sys.stdout.write(canned('prompt_out'))\n"
                    "    sys.stderr.write(canned('prompt_err'))\n"
                    "    sys.exit(int(canned('prompt_rc').strip() or '0'))\n"
                    "elif a[:2] == ['pane', 'list']:\n"
                    "    open(os.path.join(base, 'calls.log'), 'a').write(' '.join(a) + chr(10))\n"
                    "    sfx = ''\n"
                    "    q = os.path.join(base, 'pane_list_q.txt')\n"
                    "    if os.path.exists(q):\n"
                    "        toks = [t.strip() for t in open(q).read().split()]\n"
                    "        if toks:\n"
                    "            sfx = '' if toks[0] == '-' else toks[0]\n"
                    "            open(q, 'w').write(' '.join(toks[1:]))\n"
                    "    nm = os.path.join(base, 'pane_list_out' + sfx + '.txt')\n"
                    "    if not os.path.exists(nm):\n"
                    "        nm = os.path.join(base, 'pane_list_out.txt')\n"
                    "    sys.stdout.write(open(nm).read())\n"
                    "    sys.exit(0)\n"
                    "elif a[:2] == ['agent', 'list'] and os.path.exists(os.path.join(base, 'agent_list_out.txt')):\n"
                    "    sys.stdout.write(open(os.path.join(base, 'agent_list_out.txt')).read())\n"
                    "    sys.exit(int(open(os.path.join(base, 'agent_list_rc.txt')).read().strip() or '0')"
                    " if os.path.exists(os.path.join(base, 'agent_list_rc.txt')) else 0)\n"
                    "else:\n"
                    "    print(json.dumps({'result': {}}))\n")
        os.chmod(os.path.join(bin_, "herdr"), 0o755)
        os.environ["FAKE_HERDR_DIR"] = tmp
        # canned dispatch replies (dispatch tests rewrite these per case)
        open(os.path.join(tmp, "prompt_out.txt"), "w").write('{"result": {"agent": {"agent_status": "idle"}}}\n')
        open(os.path.join(tmp, "prompt_err.txt"), "w").write("")
        open(os.path.join(tmp, "prompt_rc.txt"), "w").write("0")
        open(os.path.join(tmp, "pane_list_out.txt"), "w").write('{"result": {"panes": []}}\n')
        # fake mk-bus on $MK_BUS_CMD (like the fake mk-state below): records its argv, answers
        # from canned bus_<cmd>_rc/out.txt files, and token's default success writes the lane's
        # .matrixkoder-bus.json with a url exactly like the real one — so no test in THIS file
        # ever starts a real bus server.
        bus_log = os.path.join(tmp, "buscalls.log")
        with open(os.path.join(bin_, "mk-bus"), "w") as f:
            f.write("#!/usr/bin/env python3\n"
                    "import json, os, sys\n"
                    "a = sys.argv[1:]\n"
                    "base = os.environ['FAKE_MK_BUS_DIR']\n"
                    "open(os.path.join(base, 'buscalls.log'), 'a').write(' '.join(a) + chr(10))\n"
                    "cmd = a[0] if a else ''\n"
                    "rc, out = 0, '{\"ok\": true}'\n"
                    "p_rc = os.path.join(base, 'bus_' + cmd + '_rc.txt')\n"
                    "p_out = os.path.join(base, 'bus_' + cmd + '_out.txt')\n"
                    "if os.path.exists(p_rc):\n"
                    "    rc = int(open(p_rc).read().strip() or '0')\n"
                    "if os.path.exists(p_out):\n"
                    "    out = open(p_out).read()\n"
                    "elif cmd == 'token' and '--lane' in a:\n"
                    "    import hashlib\n"
                    "    lane = a[a.index('--lane') + 1]\n"
                    "    home = os.environ.get('MK_HOME') or os.environ.get('MATRIXKODER_HOME') or os.path.expanduser('~/.matrixkoder')\n"
                    "    digest = hashlib.sha256(os.path.realpath(lane).encode('utf-8')).hexdigest()\n"
                    # the bus dir this self-test pins with $MK_BUS_DIR is that dir itself, so the
                    # lane token lands in <pin>/lanes/ — mkpaths' rule (C6, L6.4), not the old root
                    "    dest = os.path.join(home, 'lanes', digest + '.json')\n"
                    "    os.makedirs(os.path.dirname(dest), exist_ok=True)\n"
                    "    with open(dest, 'w') as lf:\n"
                    "        json.dump({'v': 1, 'url': 'http://127.0.0.1:9999', 'token': 'fake'}, lf)\n"
                    "    out = '{\"ok\": true, \"token\": \"fake\"}'\n"
                    "sys.stdout.write(out + chr(10))\n"
                    "sys.exit(rc)\n")
        os.chmod(os.path.join(bin_, "mk-bus"), 0o755)
        os.environ["FAKE_MK_BUS_DIR"] = tmp
        os.environ["MK_BUS_CMD"] = os.path.join(bin_, "mk-bus")
        # fake herdr socket: records pane.report_metadata, answers {"ok": true}
        import socketserver

        class Rec(socketserver.StreamRequestHandler):
            def handle(self):
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    try:
                        req = json.loads(line)
                    except ValueError:
                        continue
                    with open(sock_log, "a") as f:
                        f.write(line.decode())
                    self.wfile.write(json.dumps({"id": req.get("id") or "",
                                                 "result": {"ok": True}}).encode() + b"\n")

        sock_path = os.path.join(tmp, "herdr.sock")
        srv = socketserver.UnixStreamServer(sock_path, Rec)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ["HERDR_SOCKET_PATH"] = sock_path

        def cli(*args, code=0, extra=None):
            env = dict(os.environ, PATH=bin_ + os.pathsep + os.environ["PATH"], **(extra or {}))
            r = subprocess.run([sys.executable, os.path.abspath(__file__), *args], env=env,
                               capture_output=True, text=True)
            assert r.returncode == code, f"{args}: rc={r.returncode}\n{r.stdout}\n{r.stderr}"
            lines = [x for x in r.stdout.strip().splitlines() if x]
            assert len(lines) == 1, f"{args}: expected one JSON line, got {r.stdout!r}"
            return json.loads(lines[0])

        def pane_of(tid):
            return "wD:" + tid.lower()

        def arm(tid, grep=r"feat\(p1\)", phase="P1", **kw):
            args = ["arm", "--lane", lanes[tid], "--pane", pane_of(tid), "--task", tid, "--run", "r1",
                    "--data", data, "--phase", phase, "--commit-grep", grep]
            for k, v in kw.items():
                args += ["--" + k.replace("_", "-"), str(v)]
            return cli(*args)

        def event(status=None, tid="H1", exited=False, pane=None):
            kind = "pane_exited" if exited else "pane_agent_status_changed"
            d = {"type": kind, "pane_id": pane or pane_of(tid), "workspace_id": "wD"}
            if status:
                d["agent_status"] = status
            return cli("event", extra={
                "HERDR_PLUGIN_EVENT": "pane.exited" if exited else "pane.agent_status_changed",
                "HERDR_PLUGIN_EVENT_JSON": json.dumps({"event": kind, "data": d})})

        # arm: validation, then the watch file
        plain = os.path.join(tmp, "plain")
        os.makedirs(plain)
        r = cli("arm", "--lane", plain, "--pane", "wX:p1", "--task", "H1", "--run", "r1",
                "--data", data, "--phase", "P1", "--commit-grep", "x", code=2)
        assert r["ok"] is False and "worktree" in r["error"], r
        r = cli("arm", "--lane", lanes["H1"], "--pane", pane_of("H1"), "--task", "H1", "--run", "r1",
                "--data", data, "--phase", "P1", "--commit-grep", "(", code=2)
        assert r["ok"] is False and "compile" in r["error"], r
        r = cli("arm", "--lane", lanes["H1"], "--pane", pane_of("H1"), "--task", "H1",
                "--data", data, "--phase", "P1", "--commit-grep", "x", code=2)  # --run missing
        assert r["ok"] is False, r

        # event for a pane with no armed lane: ignored, exit 0, nothing written anywhere
        r = event("done", pane="wZ:p9")
        assert r == {"ok": True, "ignored": "not an armed lane"}, r
        assert armed_lanes() == []

        # H1 done: matching commit + report + phase token → verifying, label + toast attempted
        g(lanes["H1"], "commit", "-q", "--allow-empty", "-m", "feat(p1): state core")
        open(os.path.join(lanes["H1"], "report.json"), "w").write("{}")
        open(pane_f, "w").write("> dispatch-h1\nall gates green\n⏺ P1 DONE\n")
        open(status_f, "w").write("done")
        a = arm("H1", report="report.json", prompt_marker="dispatch-h1")
        assert a["ok"] and a["watch"] == os.path.join(lanes["H1"], WATCH)
        assert "task_pane_error" not in a, a
        w = watch_of("H1")
        assert (w["pane"], w["task"], w["run"], w["phase"], w["data"]) == (pane_of("H1"), "H1", "r1", "P1", data)
        assert w["commit_grep"] == r"feat\(p1\)" and w["verdict"] is None and w["armed_at"] and w["last_activity"]
        # (G1) arm reports the git guard: no agent in the pane yet = unknown (null), never true
        assert a["guard_active"] is None and "guard_note" not in a, a
        al_out, al_rc = os.path.join(tmp, "agent_list_out.txt"), os.path.join(tmp, "agent_list_rc.txt")
        open(al_out, "w").write(json.dumps({"result": {"agents": [
            {"agent": "opencode", "agent_status": "idle", "pane_id": pane_of("H1")}]}}) + "\n")
        a2 = arm("H1", report="report.json", prompt_marker="dispatch-h1")
        assert a2["guard_active"] is False, a2
        assert f"{lanes['H1']}/.matrixkoder-bin" in a2["guard_note"] and "already running" in a2["guard_note"], a2
        open(al_out, "w").write(json.dumps({"result": {"agents": [
            {"agent": "opencode", "agent_status": "idle", "pane_id": "wD:elsewhere"}]}}) + "\n")
        assert arm("H1", report="report.json", prompt_marker="dispatch-h1")["guard_active"] is None
        open(al_out, "w").write("not json\n")  # a herdr that cannot say stays unknown, arm still ok
        assert arm("H1", report="report.json", prompt_marker="dispatch-h1")["guard_active"] is None
        open(al_rc, "w").write("1")
        open(al_out, "w").write(json.dumps({"result": {"agents": [{"pane_id": pane_of("H1")}]}}) + "\n")
        assert arm("H1", report="report.json", prompt_marker="dispatch-h1")["guard_active"] is None
        os.unlink(al_out)
        os.unlink(al_rc)
        # arm records the lane's pane on the task, so the board names it without reading the watch file
        def task_field(tid, key):
            # a direct read-only store read, never a child mk-state: a case with $MK_STATE_CMD
            # faked would otherwise see this probe in its call log
            rows = store_read("SELECT * FROM tasks WHERE run='r1' AND lower(id)=lower(?)", (tid,))
            assert rows, f"task {tid} not in the store"
            val = rows[0][key]
            return json.loads(val) if key in ("deps", "owned", "refs") and isinstance(val, str) else val

        assert task_field("H1", "pane") == pane_of("H1")
        r = event("done", tid="H1")
        assert r["verdict"] == "DONE" and r["status"] == "verifying" and r["recorded"], r
        assert r["label"] is True and r["toast"] is True, r
        assert task_status("H1") == "verifying"
        w = watch_of("H1")
        assert w["verdict"] == "DONE" and w["verdict_at"]
        assert any("H1 DONE → verifying" in l for l in open(notes_f).read().splitlines())
        reqs = [json.loads(l) for l in open(sock_log).read().splitlines()]
        md = [q for q in reqs if q.get("method") == "pane.report_metadata"]
        assert md and md[-1]["params"] == {"pane_id": pane_of("H1"), "source": "matrixkoder",
                                           "state_labels": {"mk": "H1 · verifying"}}, md
        assert ("building", "verifying", "lane-watch") in [(t["from"], t["to"], t["by"]) for t in transitions()]
        # the same event again: no second transition, no re-label, no re-toast
        ntr, vat, notes_len = len(transitions()), w["verdict_at"], len(open(notes_f).read().splitlines())
        r = event("done", tid="H1")
        assert r.get("noop") is True and task_status("H1") == "verifying" and len(transitions()) == ntr
        assert watch_of("H1")["verdict_at"] == vat and len(open(notes_f).read().splitlines()) == notes_len

        # H2: done without the commit → no verdict (turn ended, task not finished) ...
        g(lanes["H2"], "commit", "-q", "--allow-empty", "-m", "chore: wip")
        open(pane_f, "w").write("⏺ P1 DONE\n")
        open(status_f, "w").write("done")
        arm("H2")
        r = event("done", tid="H2")
        assert r["ok"] and r["verdict"] is None and "not finished" in r["note"], r
        assert watch_of("H2")["verdict"] is None and task_status("H2") == "building"
        # ... then the phase BLOCKED token → blocked
        open(pane_f, "w").write("● P1 BLOCKED: which schema version?\n")
        open(status_f, "w").write("blocked")
        r = event("blocked", tid="H2")
        assert r["verdict"] == "BLOCKED" and r["status"] == "blocked" and task_status("H2") == "blocked", r
        assert ("building", "blocked") in [(t["from"], t["to"]) for t in transitions()]
        assert any("P1 BLOCKED: which schema version?" in e.get("text", "") for e in events())

        # H3: working only moves last_activity
        g(lanes["H3"], "commit", "-q", "--allow-empty", "-m", "chore: wip")
        open(status_f, "w").write("working")
        arm("H3")
        t0 = watch_of("H3")["last_activity"]
        time.sleep(0.05)
        r = event("working", tid="H3")
        assert r["touched"] is True and watch_of("H3")["last_activity"] > t0
        assert watch_of("H3")["verdict"] is None and task_status("H3") == "building"

        # H4: pane.exited → stalled "agent exited"; a second exit is a no-op
        arm("H4")
        r = event(tid="H4", exited=True)
        assert r["verdict"] == "STALL" and r["status"] == "stalled" and task_status("H4") == "stalled", r
        assert any("agent exited" in e.get("text", "") for e in events())
        # R41 guard: the pane-exit STALL note carries a source (apply_verdict appended source=pane;
        # H4 is the only lane-watch STALL so far — the sweep STALLs come later)
        assert all("source=pane" in e.get("text", "") for e in events()
                   if e.get("text", "").startswith("lane-watch STALL"))
        r = event(tid="H4", exited=True)
        assert r.get("noop") is True

        # sweep: STALL only after stall_min of silence; a lane with a verdict is skipped
        old = f"{int(time.time() - 300)} +0000"
        g(lanes["H5"], "commit", "-q", "--allow-empty", "-m", "chore: wip",
          dates={"GIT_COMMITTER_DATE": old, "GIT_AUTHOR_DATE": old})
        for i in (1, 2):  # H5 has two modified files, old enough not to count as activity: the alive hint
            wip = os.path.join(lanes["H5"], f"wip{i}.txt")
            open(wip, "w").write("x")
            os.utime(wip, (time.time() - 300, time.time() - 300))
        arm("H5", stall_min=1)
        patch_watch("H5", last_activity=time.time() - 90)  # silence since 90 s ago
        g(lanes["H6"], "commit", "-q", "--allow-empty", "-m", "chore: fresh")
        arm("H6", stall_min=1)
        open(status_f, "w").write("idle")
        open(pane_f, "w").write("quiet\n")
        r = cli("sweep")
        assert r["ok"] and r["checked"] == 3 and r["skipped"] == 3, r  # H3 H5 H6 checked, H1 H2 H4 skipped
        v = {x["task"]: x for x in r["verdicts"]}
        assert set(v) == {"H5"} and v["H5"]["verdict"] == "STALL" and v["H5"]["status"] == "stalled", r
        dirty = {x["task"]: x["dirty_files"] for x in r["lanes"]}
        assert set(dirty) == {"H3", "H5", "H6"} and dirty["H5"] == 2 and dirty["H6"] == 0, dirty
        assert v["H5"]["alive_hint"] == "the lane has 2 modified files; it may be working in a subagent", v["H5"]
        assert task_status("H5") == "stalled" and watch_of("H6")["verdict"] is None
        assert task_status("H6") == "building" and task_status("H3") == "building"
        ntr = len(transitions())
        r = cli("sweep")  # H5 has a verdict now: skipped, no double STALL
        assert r["checked"] == 2 and r["skipped"] == 4 and not r["verdicts"] and len(transitions()) == ntr, r
        r = cli("sweep", "--data", data)  # the --data filter reaches the same lanes
        assert r["ok"] and r["checked"] + r["skipped"] == 6, r

        # H7: a task already in review is never moved back by a late event
        g(lanes["H7"], "commit", "-q", "--allow-empty", "-m", "chore: late",
          dates={"GIT_COMMITTER_DATE": old, "GIT_AUTHOR_DATE": old})
        arm("H7", stall_min=1)
        mks("task", "set", "H7", "status=review")
        patch_watch("H7", last_activity=time.time() - 90)
        r = event("idle", tid="H7")
        assert r["verdict"] == "STALL" and task_status("H7") == "review", r
        assert watch_of("H7")["verdict"] == "STALL"
        assert any("status review kept" in e.get("text", "") for e in events())

        # (MB3.2) the status-file channel: verdict_for reads <lane>/.matrixkoder-status.json first
        # (R27) inside the caller's per-lane flock (R42), and every verdict note carries a source (R41)
        # case 1: pane shows NO token, the status file says done -> DONE from the file (R30), source=status
        g(lanes["T1"], "commit", "-q", "--allow-empty", "-m", "feat(p1): status core")
        open(os.path.join(lanes["T1"], "report.json"), "w").write("{}")
        open(pane_f, "w").write("> dispatch-t1\nwork finished, gates green\n")
        open(status_f, "w").write("done")
        arm("T1", phase="T1 R1", report="report.json", prompt_marker="dispatch-t1")
        wstat("T1", "done", time.time(), commit="0123456789abcdef0123456789abcdef01234567")
        r = event("done", tid="T1")
        assert r["verdict"] == "DONE" and r["status"] == "verifying" and r["recorded"], r
        assert task_status("T1") == "verifying" and watch_of("T1")["verdict"] == "DONE"
        assert "source=status" in [t for t in transitions() if t.get("task") == "T1"][-1]["text"]
        # case 2: the same lane, a stale file (armed_at later than updated_at, R27.7) -> no verdict
        mks("task", "set", "T1", "status=building")
        patch_watch("T1", verdict=None, verdict_at=None, armed_at=time.time() + 60)
        r = event("done", tid="T1")
        assert r.get("verdict") is None and "no verdict" in r.get("note", ""), r
        assert task_status("T1") == "building"
        armed = {"lane": lanes["T1"], "run": "r1", "task": "T1", "phase": "T1 R1",
                 "armed_at": time.time() + 60, "max_wait_min": 30}
        assert lw.read_status(lanes["T1"], armed) is None  # R27.7 directly: updated_at < armed_at
        patch_watch("T1", armed_at=time.time())  # later cases use their own lanes; leave this one sane
        # case 3: a v=99 file the reader must ignore whole (R39) -> the pane token is the only channel
        g(lanes["T2"], "commit", "-q", "--allow-empty", "-m", "feat(p1): pane core")
        open(os.path.join(lanes["T2"], "report.json"), "w").write("{}")
        open(pane_f, "w").write("> dispatch-t2\nall gates green\n⏺ T2 R1 DONE\n")
        open(status_f, "w").write("done")
        arm("T2", phase="T2 R1", report="report.json", prompt_marker="dispatch-t2")
        wstat("T2", "done", time.time(), v=99, commit="0123456789abcdef0123456789abcdef01234567")
        r = event("done", tid="T2")
        assert r["verdict"] == "DONE" and r["status"] == "verifying" and r["recorded"], r
        t2 = [t for t in transitions() if t.get("task") == "T2"][-1]["text"]
        assert "source=pane" in t2 and "source=status" not in t2, t2
        assert task_status("T2") == "verifying"

        # dispatch: prompt + delivery check in one call, against the fake herdr ($MK_HERDR)
        calls = os.path.join(tmp, "calls.log")

        def set_canned(fname, text):
            open(os.path.join(tmp, fname), "w").write(text)

        def last_call():
            return open(calls).read().splitlines()[-1]

        # (a) the wait settles → delivered:true; the argv herdr saw is exactly the spec's
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "idle"}}}\n')
        set_canned("prompt_err.txt", "")
        set_canned("prompt_rc.txt", "0")
        r = cli("dispatch", "--pane", pane_of("H1"), "--text", "Read brief.md", extra={"MK_HERDR": ""})
        assert r["ok"] is True and r["delivered"] is True and r["agent_status"] == "idle", r
        assert r["timeout_ms"] == 25000, r
        assert last_call() == f"agent prompt {pane_of('H1')} Read brief.md --wait --timeout 25000", last_call()
        r = cli("dispatch", "--pane", pane_of("H1"), "--text", "go", "--timeout-ms", "1234",
                extra={"MK_HERDR": os.path.join(bin_, "herdr")})
        assert r["ok"] and last_call().endswith("--timeout 1234"), last_call()
        # (b) timeout + pane list shows working → delivered:true (the prompt is still in flight)
        set_canned("prompt_out.txt", '{"error": {"code": "timeout", "message": "timed out waiting"}}\n')
        set_canned("prompt_rc.txt", "1")
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [
            {"pane_id": pane_of("H1"), "agent_status": "working"}]}}) + "\n")
        r = cli("dispatch", "--pane", pane_of("H1"), "--text", "go")
        assert r["ok"] is True and r["delivered"] is True and r["pane_state"] == "working", r
        assert r["timed_out"] is True and "timeout" in r["herdr_error"], r
        # (G1) delivered: no error field, herdr's words in herdr_error, the note says why
        assert "error" not in r and r["note"].endswith("herdr status wait timed out; the pane is working"), r
        # (c) timeout + agent_prompt_stalled (or the pane gone from the list) → delivered:false
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [
            {"pane_id": pane_of("H1"), "agent_status": "agent_prompt_stalled"}]}}) + "\n")
        r = cli("dispatch", "--pane", pane_of("H1"), "--text", "go")
        assert r["ok"] is False and r["delivered"] is False, r
        assert r["pane_state"] == "agent_prompt_stalled" and "timeout" in r["error"], r
        set_canned("pane_list_out.txt", '{"result": {"panes": []}}\n')
        r = cli("dispatch", "--pane", pane_of("H1"), "--text", "go")
        assert r["ok"] is False and r["delivered"] is False and r["pane_state"] is None, r
        # a stall reported as the wait result (rc 0) takes the same pane-list path
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "agent_prompt_stalled"}}}\n')
        set_canned("prompt_rc.txt", "0")
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [
            {"pane_id": pane_of("H1"), "agent_status": "working"}]}}) + "\n")
        r = cli("dispatch", "--pane", pane_of("H1"), "--text", "go")
        assert r["ok"] is True and r["delivered"] is True and r["timed_out"] is True, r
        # (d) any other failure → ok:false carrying herdr's own words; usage errors stay exit 2
        set_canned("prompt_out.txt", "")
        set_canned("prompt_err.txt", "herdr: no such pane wZ:gone")
        set_canned("prompt_rc.txt", "1")
        r = cli("dispatch", "--pane", "wZ:gone", "--text", "go")
        assert r["ok"] is False and r["delivered"] is False and "no such pane" in r["error"], r
        assert cli("dispatch", "--pane", "wD:h1", code=2)["ok"] is False  # --text missing
        # (e) the subprocess itself times out (a hanging herdr): the pane's state still decides
        open(os.path.join(tmp, "hang.txt"), "w").write("35")
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [
            {"pane_id": pane_of("H1"), "agent_status": "working"}]}}) + "\n")
        r = cli("dispatch", "--pane", pane_of("H1"), "--text", "go", "--timeout-ms", "100")
        assert r["ok"] is True and r["delivered"] is True and r["timed_out"] is True, r
        assert r["pane_state"] == "working", r
        os.unlink(os.path.join(tmp, "hang.txt"))
        # a herdr that cannot even run: the same pane-state check, not working → not delivered
        open(os.path.join(tmp, "noexec"), "w").write("not a binary\n")
        set_canned("pane_list_out.txt", '{"result": {"panes": []}}\n')
        r = cli("dispatch", "--pane", pane_of("H1"), "--text", "go",
                extra={"MK_HERDR": os.path.join(tmp, "noexec")})
        assert r["ok"] is False and r["delivered"] is False and r["timed_out"] is True, r
        assert "PermissionError" in r["error"], r

        # (F5) a delivered prompt moves the armed task forward: its children (or the task itself,
        # childless) go building via mk-state. The fake ($MK_STATE_CMD) records every call and can
        # be made to fail; delivered:false must not call mk-state at all.
        ms_log = os.path.join(tmp, "mscalls.log")
        with open(os.path.join(bin_, "mk-state"), "w") as f:
            f.write("#!/usr/bin/env python3\n"
                    "import json, os, sys\n"
                    "base = os.environ['FAKE_MK_STATE_DIR']\n"
                    "open(os.path.join(base, 'mscalls.log'), 'a').write(' '.join(sys.argv[1:]) + chr(10))\n"
                    # D1.5.4: the dispatch's `task check` is answered like a passing check
                    # (an ok:false + missing list when ms_failcheck.txt exists)
                    "if sys.argv[1:3] == ['task', 'check']:\n"
                    "    if os.path.exists(os.path.join(base, 'ms_failcheck.txt')):\n"
                    "        print(json.dumps({'ok': False, 'missing': ['acceptance']}))\n"
                    "    else:\n"
                    "        print(json.dumps({'ok': True, 'missing': []}))\n"
                    "    sys.exit(0)\n"
                    "if os.path.exists(os.path.join(base, 'ms_badjson.txt')):\n"
                    "    sys.stdout.write('not json at all' + chr(10))\n"
                    "    sys.exit(0)\n"
                    "if os.path.exists(os.path.join(base, 'ms_err.txt')):\n"
                    "    sys.stderr.write(open(os.path.join(base, 'ms_err.txt')).read())\n"
                    "    sys.exit(1)\n"
                    "print(json.dumps({'ok': True}))\n")
        os.chmod(os.path.join(bin_, "mk-state"), 0o755)
        os.environ["FAKE_MK_STATE_DIR"] = tmp
        fake_ms = os.path.join(bin_, "mk-state")

        # an armed task that HAS children: H8 with H8C1 + H8C2
        lanes["H8"] = os.path.join(tmp, "lane-h8")
        g(repo, "worktree", "add", "-q", "--detach", lanes["H8"])
        mks("task", "add", "--root", "--id", "H8", "--title", "H8", "--run", "r1")
        mks("task", "add", "--id", "H8C1", "--title", "c1", "--parent", "H8", "--run", "r1")
        mks("task", "add", "--id", "H8C2", "--title", "c2", "--parent", "H8", "--run", "r1")
        arm("H8")

        def ms_calls():
            return open(ms_log).read().splitlines() if os.path.exists(ms_log) else []

        def building_calls():
            # D1.5.3/D1.5.4 add a route write and a `task check` around every build dispatch
            # to an armed lane; these pre-D1.5 cases assert the board writes exactly, so they
            # filter the log to the set-status writes (the check line has no status= pair).
            return [l for l in ms_calls() if l.startswith("task set ") and "status=" in l]

        # (f) delivered:true, armed task has children → exactly the children set building
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "idle"}}}\n')
        set_canned("prompt_err.txt", "")
        set_canned("prompt_rc.txt", "0")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go", extra={"MK_STATE_CMD": fake_ms})
        assert r["ok"] is True and r["delivered"] is True and r["building"] == ["H8C1", "H8C2"], r
        assert "task_status_error" not in r, r
        # the D1.5.4 check ran once, on the armed task, before the prompt
        assert ms_calls() == [f"task check H8 --data {data} --run r1",
                              f"task set H8C1 status=building --data {data} --by dispatch --run r1",
                              f"task set H8C2 status=building --data {data} --by dispatch --run r1"], ms_calls()
        assert building_calls() == [
            f"task set H8C1 status=building --data {data} --by dispatch --run r1",
            f"task set H8C2 status=building --data {data} --by dispatch --run r1"], ms_calls()
        # (g) delivered:true, armed task has NO children → the task itself
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H1"), "--text", "go", extra={"MK_STATE_CMD": fake_ms})
        assert r["ok"] is True and r["delivered"] is True and r["building"] == ["H1"], r
        assert ms_calls() == [f"task check H1 --data {data} --run r1",
                              f"task set H1 status=building --data {data} --by dispatch --run r1"], ms_calls()
        assert building_calls() == [f"task set H1 status=building --data {data} --by dispatch --run r1"], ms_calls()
        # (h) delivered:false → no mk-state call at all
        set_canned("prompt_out.txt", "")
        set_canned("prompt_err.txt", "herdr: no such pane wZ:gone")
        set_canned("prompt_rc.txt", "1")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go", extra={"MK_STATE_CMD": fake_ms})
        assert r["ok"] is False and r["delivered"] is False and "building" not in r, r
        # a NOT-delivered dispatch passed the check (it ran, once, on the armed task) but
        # never reached the board write — delivered:false calls no task set at all
        assert ms_calls() == [f"task check H8 --data {data} --run r1"], ms_calls()
        assert building_calls() == [], ms_calls()
        # (i) a failing mk-state (nonzero exit, or bad JSON) → the error rides in the JSON,
        # delivery verdict unchanged
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "idle"}}}\n')
        set_canned("prompt_err.txt", "")
        set_canned("prompt_rc.txt", "0")
        open(os.path.join(tmp, "ms_err.txt"), "w").write("mk-state: run state locked\n")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go", extra={"MK_STATE_CMD": fake_ms})
        assert r["ok"] is True and r["delivered"] is True and r["building"] == [], r
        assert "run state locked" in r["task_status_error"] and "H8C1" in r["task_status_error"], r
        assert building_calls() == [f"task set H8C1 status=building --data {data} --by dispatch --run r1",
                                    f"task set H8C2 status=building --data {data} --by dispatch --run r1"], ms_calls()
        os.unlink(os.path.join(tmp, "ms_err.txt"))
        open(os.path.join(tmp, "ms_badjson.txt"), "w").write("x")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go", extra={"MK_STATE_CMD": fake_ms})
        assert r["ok"] is True and r["delivered"] is True and r["building"] == [], r
        assert "not json" in r["task_status_error"], r
        assert building_calls() == [f"task set H8C1 status=building --data {data} --by dispatch --run r1",
                                    f"task set H8C2 status=building --data {data} --by dispatch --run r1"], ms_calls()
        os.unlink(os.path.join(tmp, "ms_badjson.txt"))

        # (H2.1) one automatic retry: not delivered + pane idle/done → wait, send once more; every
        # reply carries attempts, herdr_error and the raw pane row. prompt_q.txt scripts the fake's
        # prompt replies per call ("-" = the base canned files, "2" = the *2 files, consumed in
        # order); $MK_DISPATCH_RETRY_S runs the wait at 0 s instead of the real 5, and
        # $MK_DISPATCH_RECHECK_S does the same for the recheck's 3.
        def prompt_calls():
            return [l for l in open(calls).read().splitlines() if l.startswith("agent prompt")]

        fast = {"MK_DISPATCH_RETRY_S": "0", "MK_DISPATCH_RECHECK_S": "0", "MK_STATE_CMD": fake_ms}
        done_row = {"pane_id": pane_of("H8"), "agent_status": "done", "workspace_id": "wD"}
        set_canned("prompt_out.txt", '{"error": {"code": "timeout", "message": "timed out waiting"}}\n')
        set_canned("prompt_rc.txt", "1")
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [done_row]}}) + "\n")
        # (a) attempt 1 times out on a done pane; the retry lands via its own wait result →
        # delivered:true, attempts:2, first attempt's words in herdr_error, done row in pane_entry,
        # mark_armed_building exactly once (the two child writes, no more)
        set_canned("prompt_out2.txt", '{"result": {"agent": {"agent_status": "working"}}}\n')
        set_canned("prompt_rc2.txt", "0")
        set_canned("prompt_q.txt", "- 2")
        open(ms_log, "w").write("")
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go", extra=fast)
        assert r["ok"] is True and r["delivered"] is True and r["attempts"] == 2, r
        assert r["agent_status"] == "working" and "timeout" in r["herdr_error"], r
        assert r["pane_entry"] == done_row, r
        assert len(prompt_calls()) - n0 == 2, prompt_calls()
        assert building_calls() == [f"task set H8C1 status=building --data {data} --by dispatch --run r1",
                                    f"task set H8C2 status=building --data {data} --by dispatch --run r1"], ms_calls()
        # (b) the pane never becomes working (done through both attempts) but the prompt text
        # DID land in pane.txt (the fake herdr appends on every prompt call). The count check
        # sees the marker count go up and recovers — the prompt was delivered even though the
        # pane state didn't transition to working. delivered:true, recovered:true.
        set_canned("prompt_q.txt", "- -")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go", extra=fast)
        assert r["ok"] is True and r["delivered"] is True and r["recovered"] is True and r["attempts"] == 2, r
        assert r["pane_state"] == "done" and r["pane_entry"] == done_row, r
        assert "timeout" in r["herdr_error"] and "error" not in r and r["timed_out"] is True, r
        assert r["note"].endswith("herdr wait timed out; the prompt had already landed"), r
        # (c) pane state null — missing from a readable list, or the list unreadable → NO retry
        set_canned("pane_list_out.txt", '{"result": {"panes": []}}\n')
        set_canned("prompt_q.txt", "- 2")  # a scripted second reply that must never be sent
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go", extra=fast)
        assert r["ok"] is False and r["delivered"] is False and r["attempts"] == 1, r
        assert r["pane_state"] is None and r["pane_entry"] is None, r
        assert "pane_list_error" not in r and len(prompt_calls()) - n0 == 1, r
        set_canned("pane_list_out.txt", "not json\n")  # unreadable list: same verdict, error carried
        set_canned("prompt_q.txt", "- 2")
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go", extra=fast)
        assert r["ok"] is False and r["delivered"] is False and r["attempts"] == 1, r
        assert r["pane_state"] is None and r["pane_entry"] is None and "pane_list_error" in r, r
        # (d) a delivered first attempt: attempts stays 1, the raw fields are still there
        idle_row = {"pane_id": pane_of("H1"), "agent_status": "idle", "workspace_id": "wD"}
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "idle"}}}\n')
        set_canned("prompt_rc.txt", "0")
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [idle_row]}}) + "\n")
        os.unlink(os.path.join(tmp, "prompt_q.txt"))
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H1"), "--text", "go")
        assert r["ok"] is True and r["delivered"] is True and r["attempts"] == 1, r
        assert r["pane_entry"] == idle_row and r["herdr_error"], r
        assert len(prompt_calls()) - n0 == 1, prompt_calls()
        for f_ in ("prompt_out2.txt", "prompt_rc2.txt"):
            os.unlink(os.path.join(tmp, f_))

        # (F3.1) dispatch --role review: delivered WITHOUT the board write — the task under review
        # stays at review, real mk-state is never called, the reply says status_unchanged:true
        lanes["H_R"] = os.path.join(tmp, "lane-hr")
        g(repo, "worktree", "add", "-q", "--detach", lanes["H_R"])
        mks("task", "add", "--root", "--id", "H_R", "--title", "rev", "--run", "r1")
        mks("task", "set", "H_R", "status=review")
        arm("H_R")
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "idle"}}}\n')
        set_canned("prompt_err.txt", "")
        set_canned("prompt_rc.txt", "0")
        r = cli("dispatch", "--pane", pane_of("H_R"), "--text", "do review", "--role", "review")
        assert r["ok"] is True and r["delivered"] is True and r["status_unchanged"] is True, r  # real mk-state: any write shows
        assert "task_status_error" not in r and "building" not in r, r
        assert task_status("H_R") == "review", task_status("H_R")  # STILL review, not building
        # the same armed lane, default --role build, fake mk-state: the write IS made — the two
        # paths differ exactly in the mk-state call review skips
        mks("task", "set", "H_R", "status=review")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H_R"), "--text", "do review",
                extra={"MK_STATE_CMD": fake_ms})
        assert r["ok"] is True and r["delivered"] is True and "status_unchanged" not in r, r
        assert r["building"] == ["H_R"], r  # childless task → itself
        assert ms_calls() == [f"task check H_R --data {data} --run r1",
                              f"task set H_R status=building --data {data} --by dispatch --run r1"], ms_calls()
        assert building_calls() == [f"task set H_R status=building --data {data} --by dispatch --run r1"], ms_calls()
        assert cli("dispatch", "--pane", pane_of("H_R"), "--text", "x", "--role", "boss",
                   code=2)["ok"] is False  # --role takes exactly build|review

        # (F3.2) stall + pane idle/done: ONE recheck (fresh pane list + pane text) before
        # delivered:false — the prompt marker (first words of --text) in the pane text means it
        # landed after all (herdr's wait gave up on a pane that was done all along)
        def pane_list_calls():
            return [l for l in open(calls).read().splitlines() if l == "pane list"]

        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "agent_prompt_stalled"}}}\n')
        set_canned("prompt_err.txt", "")
        set_canned("prompt_rc.txt", "0")
        idle_row8 = {"pane_id": pane_of("H8"), "agent_status": "idle", "workspace_id": "wD"}
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [idle_row8]}}) + "\n")
        # pane text BEFORE the dispatch does NOT contain the dispatch marker; the fake herdr
        # appends the prompt text on every prompt call, so the new portion in pane.txt after
        # both stalled attempts carries the marker — since_dispatch sees it as new and recovers
        open(pane_f, "w").write("> older unrelated work\nold history line\n")
        set_canned("prompt_q.txt", "- -")  # both attempts settle on agent_prompt_stalled
        open(ms_log, "w").write("")
        nl0 = len(pane_list_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "do the review", extra=fast)
        assert r["ok"] is True and r["delivered"] is True and r["recovered"] is True, r
        assert r["attempts"] == 2 and r["pane_state"] == "idle" and r["pane_entry"] == idle_row8, r
        assert len(pane_list_calls()) - nl0 == 4, pane_list_calls()  # pre-read + 2 deciding + recheck
        assert building_calls() == [f"task set H8C1 status=building --data {data} --by dispatch --run r1",
                                    f"task set H8C2 status=building --data {data} --by dispatch --run r1"], ms_calls()
        # pane working → the existing working path decides on its own check, no recheck happens
        set_canned("prompt_out.txt", '{"error": {"code": "timeout", "message": "timed out waiting"}}\n')
        set_canned("prompt_rc.txt", "1")
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [
            {"pane_id": pane_of("H8"), "agent_status": "working"}]}}) + "\n")
        nl0 = len(pane_list_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go", extra=fast)
        assert r["ok"] is True and r["delivered"] is True and r["pane_state"] == "working", r
        assert "recovered" not in r and r["attempts"] == 1, r
        assert len(pane_list_calls()) - nl0 == 2, pane_list_calls()  # pre-read + deciding, no recheck

        # (H2) since_dispatch: the recheck only counts the marker when it appears in pane text
        # that is NEW since this dispatch started — the first line of --text that was ALREADY
        # in the pane (an earlier dispatch's prompt, an old log line) must NOT recover.
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "agent_prompt_stalled"}}}\n')
        set_canned("prompt_rc.txt", "0")
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [idle_row8]}}) + "\n")
        # pane text BEFORE this dispatch starts already contains the exact marker. `no_pane.txt`
        # suppresses the fake's append so the count does not go up — the marker is genuinely
        # old (a sibling lane's earlier prompt, an old log line) and must not recover.
        open(pane_f, "w").write("Read and follow /this/lane/brief.md\nold tail line\n")
        open(os.path.join(tmp, "no_pane.txt"), "w").write("")
        set_canned("prompt_q.txt", "- -")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H8"),
                "--text", "Read and follow /this/lane/brief.md\nread the brief and continue",
                extra=fast)
        assert r["ok"] is False and r["delivered"] is False, r
        assert "recovered" not in r and r["attempts"] == 2, r
        assert building_calls() == [] and r.get("check", {}).get("ok") is not False, (r, ms_calls())
        # the marker does NOT appear in old pane text — the new portion (fake appends) carries
        # the marker and it is genuinely new since this dispatch started → recover
        os.unlink(os.path.join(tmp, "no_pane.txt"))
        open(pane_f, "w").write("older unrelated work\nold history line\n")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H8"),
                "--text", "Read and follow /this/lane/brief.md\nread the brief and continue",
                extra=fast)
        assert r["ok"] is True and r["delivered"] is True and r["recovered"] is True, r
        assert r["attempts"] == 2 and building_calls() == [
            f"task set H8C1 status=building --data {data} --by dispatch --run r1",
            f"task set H8C2 status=building --data {data} --by dispatch --run r1"], ms_calls()
        os.unlink(os.path.join(tmp, "prompt_q.txt"))

        # (R2-M3) sliding pane: a full 300-line window where the prompt lands at the bottom.
        # The old slice-based check returned False (the slice from the old length was empty
        # after the window scrolled). The count check sees the marker count go up (the fake
        # herdr appends the full --text on every prompt call) and recovers.
        marker = "Read and follow /this/lane/brief.md"
        open(pane_f, "w").write("\n".join(f"filler line {i}" for i in range(300)) + "\n")
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "agent_prompt_stalled"}}}\n')
        set_canned("prompt_rc.txt", "0")
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [idle_row8]}}) + "\n")
        set_canned("prompt_q.txt", "- -")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H8"),
                "--text", f"{marker}\nread the brief and continue",
                extra=fast)
        assert r["ok"] is True and r["delivered"] is True and r["recovered"] is True, (
            "full-window pane: the prompt marker count went up, recovered", r)
        os.unlink(os.path.join(tmp, "prompt_q.txt"))

        # (S1.4) --kind fix|takeover on session.fresh_session: new sends the pane's agent its
        # new-session command BEFORE the task text (the armed watch's kind names the agent:
        # claude -> /clear, opencode -> /new); build, --role review and fresh_session: same
        # send only the task text. $MATRIXKODER_DATA pins each case's yaml; the fixture says
        # fresh_session: new.
        datanew = os.path.join(tmp, "data-new")
        os.makedirs(datanew)
        shutil.copy(os.path.join(HERE, "fixtures", "matrixkoder.yaml"),
                    os.path.join(datanew, "matrixkoder.yaml"))
        datasame = os.path.join(tmp, "data-same")
        os.makedirs(datasame)
        with open(os.path.join(datasame, "matrixkoder.yaml"), "w") as f:
            f.write("session:\n  fresh_session: same\n")
        dataempty = os.path.join(tmp, "data-empty")
        os.makedirs(dataempty)
        fnew = dict(fast, MATRIXKODER_DATA=datanew)
        fsame = dict(fast, MATRIXKODER_DATA=datasame)
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "idle"}}}\n')
        set_canned("prompt_err.txt", "")
        set_canned("prompt_rc.txt", "0")
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [
            {"pane_id": pane_of("H8"), "agent_status": "idle"}]}}) + "\n")
        patch_watch("H8", kind="claude")
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "do the fix", "--kind", "fix", extra=fnew)
        assert r["ok"] is True and r["delivered"] is True and r["fresh_session"] == "/clear", r
        assert prompt_calls()[n0:] == [
            f"agent prompt {pane_of('H8')} /clear --wait --timeout 25000",
            f"agent prompt {pane_of('H8')} do the fix --wait --timeout 25000"], prompt_calls()[n0:]
        # a takeover round on an OpenCode lane: /new before the text
        patch_watch("H8", kind="opencode")
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "take over", "--kind", "takeover",
                extra=fnew)
        assert r["ok"] is True and r["delivered"] is True and r["fresh_session"] == "/new", r
        got = prompt_calls()[n0:]
        assert got[0] == f"agent prompt {pane_of('H8')} /new --wait --timeout 25000" and \
            got[1] == f"agent prompt {pane_of('H8')} take over --wait --timeout 25000", got
        # an omp lane: its own /new before the text (same command, a different kind's agent)
        patch_watch("H8", kind="omp")
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "omp round", "--kind", "fix", extra=fnew)
        assert r["ok"] is True and r["delivered"] is True and r["fresh_session"] == "/new", r
        assert prompt_calls()[n0:] == [
            f"agent prompt {pane_of('H8')} /new --wait --timeout 25000",
            f"agent prompt {pane_of('H8')} omp round --wait --timeout 25000"], prompt_calls()[n0:]
        # a build round (the default kind): one prompt, exactly as today
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go build", extra=fnew)
        assert r["ok"] is True and r["delivered"] is True and r["fresh_session"] is None, r
        assert prompt_calls()[n0:] == [
            f"agent prompt {pane_of('H8')} go build --wait --timeout 25000"], prompt_calls()[n0:]
        # fresh_session: same — a fix round still sends only the task text
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go fix", "--kind", "fix", extra=fsame)
        assert r["ok"] is True and r["delivered"] is True and r["fresh_session"] is None, r
        assert prompt_calls()[n0:] == [
            f"agent prompt {pane_of('H8')} go fix --wait --timeout 25000"], prompt_calls()[n0:]
        # --role review never resets, even on a fix round with fresh_session: new
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "review it", "--kind", "fix",
                "--role", "review", extra=fnew)
        assert r["ok"] is True and r["delivered"] is True and r["fresh_session"] is None and \
            r["status_unchanged"] is True, r
        assert prompt_calls()[n0:] == [
            f"agent prompt {pane_of('H8')} review it --wait --timeout 25000"], prompt_calls()[n0:]
        # no yaml at all: the schema default is new, so the fix round still resets first
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "fix again", "--kind", "fix",
                extra=dict(fast, MATRIXKODER_DATA=dataempty))
        assert r["ok"] is True and r["delivered"] is True and r["fresh_session"] == "/new", r
        assert prompt_calls()[n0] == f"agent prompt {pane_of('H8')} /new --wait --timeout 25000"
        assert cli("dispatch", "--pane", pane_of("H8"), "--text", "x", "--kind", "boss",
                   code=2)["ok"] is False  # --kind takes exactly build|fix|takeover

        # (R2 LOW 1) the reset gates the round: a reset herdr refused, or a pane that never goes
        # idle after it, must NOT be followed by the task prompt
        patch_watch("H8", kind="claude")
        set_canned("prompt_out.txt", "")
        set_canned("prompt_err.txt", "herdr: pane wZ:gone is gone")
        set_canned("prompt_rc.txt", "1")
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "fix it", "--kind", "fix", extra=fnew)
        assert r["ok"] is False and r["delivered"] is False, r
        assert r["error"] == "fresh session not confirmed" and r["fresh_session"] == "/clear", r
        assert "is gone" in r["fresh_session_error"], r
        assert prompt_calls()[n0:] == [
            f"agent prompt {pane_of('H8')} /clear --wait --timeout 25000"], prompt_calls()[n0:]
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "idle"}}}\n')
        set_canned("prompt_err.txt", "")
        set_canned("prompt_rc.txt", "0")
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [
            {"pane_id": pane_of("H8"), "agent_status": "working"}]}}) + "\n")
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "fix it too", "--kind", "fix",
                extra=dict(fnew, MK_FRESH_IDLE_WAIT_S="1", MK_FRESH_IDLE_POLL_S="0.1"))
        assert r["ok"] is False and r["delivered"] is False, r
        assert r["error"] == "fresh session not confirmed" and "still working" in \
            r["fresh_session_error"], r
        assert prompt_calls()[n0:] == [
            f"agent prompt {pane_of('H8')} /clear --wait --timeout 25000"], prompt_calls()[n0:]
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [
            {"pane_id": pane_of("H8"), "agent_status": "idle"}]}}) + "\n")
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "fix it again", "--kind", "fix",
                extra=fnew)  # the gate is not sticky: the next round resets and prompts again
        assert r["ok"] is True and r["delivered"] is True and r["fresh_session"] == "/clear", r

        # (R3 MEDIUM 2) a reset that TIMES OUT is not a refusal: /clear and /new start no model
        # turn, so herdr may time out on a reset that worked. An idle pane confirms it.
        # the fake reads prompt_out<sfx>.txt, so the suffix queue must name the file it will look for
        set_canned("prompt_out2.txt", json.dumps(
            {"error": {"code": "agent_prompt_stalled"}, "result": {"agent": {
                "agent_status": "agent_prompt_stalled"}}}) + "\n")
        set_canned("prompt_rc2.txt", "0")
        set_canned("prompt_q.txt", "2")  # only the /clear stalls; the task prompt settles
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "fix after a timeout", "--kind", "fix",
                extra=fnew)
        assert r["ok"] is True and r["delivered"] is True and r["fresh_session"] == "/clear", r
        assert prompt_calls()[n0:] == [
            f"agent prompt {pane_of('H8')} /clear --wait --timeout 25000",
            f"agent prompt {pane_of('H8')} fix after a timeout --wait --timeout 25000"], \
            prompt_calls()[n0:]
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "idle"}}}\n')

        # (J1.1) --tasks arms a lane GROUP: the watch file lists every task, and one delivered
        # dispatch sets the children of EVERY listed task (or the task itself when it has none)
        # building — tonight's two-phase lane only moved the first phase's children.
        lanes["H9"] = os.path.join(tmp, "lane-h9")
        g(repo, "worktree", "add", "-q", "--detach", lanes["H9"])
        mks("task", "add", "--root", "--id", "H9", "--title", "g1", "--run", "r1")
        mks("task", "add", "--id", "H9C1", "--title", "c1", "--parent", "H9", "--run", "r1")
        mks("task", "add", "--root", "--id", "H10", "--title", "g2", "--run", "r1")
        mks("task", "set", "H9C1", "status=building")  # H9 rolls up from its child (a parent takes no status)
        mks("task", "set", "H10", "status=building")
        r = cli("arm", "--lane", lanes["H9"], "--pane", pane_of("H9"), "--tasks", "H9,H10",
                "--run", "r1", "--data", data, "--phase", "P1", "--commit-grep", r"feat\(p1\)")
        assert r["ok"] and "task_pane_error" not in r, r
        wg = watch_of("H9")
        assert wg["tasks"] == ["H9", "H10"] and wg["task"] == "H9", wg  # the group, primary kept
        assert task_field("H10", "pane") == pane_of("H9")  # the pane lands on every listed task
        assert cli("arm", "--lane", lanes["H9"], "--pane", pane_of("H9"), "--task", "H9", "--tasks",
                   "H9,H10", "--run", "r1", "--data", data, "--phase", "P1",
                   "--commit-grep", "x", code=2)["ok"] is False  # two spellings: pass one
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "idle"}}}\n')
        set_canned("prompt_err.txt", "")
        set_canned("prompt_rc.txt", "0")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H9"), "--text", "go", extra={"MK_STATE_CMD": fake_ms})
        assert r["ok"] is True and r["delivered"] is True and r["building"] == ["H9C1", "H10"], r
        # the D1.5.4 check runs on the LEAD task only (H9); the board writes fan out as before
        assert ms_calls() == [f"task check H9 --data {data} --run r1",
                              f"task set H9C1 status=building --data {data} --by dispatch --run r1",
                              f"task set H10 status=building --data {data} --by dispatch --run r1"], ms_calls()
        assert building_calls() == [f"task set H9C1 status=building --data {data} --by dispatch --run r1",
                                    f"task set H10 status=building --data {data} --by dispatch --run r1"], ms_calls()
        # a verdict on a group lane records for every listed task: H9's children and childless H10
        g(lanes["H9"], "commit", "-q", "--allow-empty", "-m", "feat(p1): group core")
        open(pane_f, "w").write("> dispatch-h9\nall gates green\n⏺ P1 DONE\n")
        open(status_f, "w").write("done")
        r = event("done", tid="H9")
        assert r["verdict"] == "DONE" and r["recorded"], r
        got = {t["id"]: t["status"] for t in store_read(
            "SELECT id, status FROM tasks WHERE run='r1'")}
        assert got["H9C1"] == "verifying" and got["H10"] == "verifying", got
        assert got["H9"] == "verifying", got  # rolled up from its child

        # disarm: removes the watch file, the lane stops matching events
        r = cli("disarm", "--lane", lanes["H3"])
        assert r["ok"] and r["removed"] is True and not os.path.exists(os.path.join(lanes["H3"], WATCH))
        assert os.path.exists(os.path.join(lanes["H3"], LOCK))  # lock file stays in place
        r = cli("disarm", "--lane", lanes["H3"])
        assert r["ok"] and r["removed"] is False
        r = event("working", tid="H3")
        assert r == {"ok": True, "ignored": "not an armed lane"}, r

        # (BUS) arm/disarm wire the lane into the bus through the fake ($MK_BUS_CMD): argv
        # recorded to buscalls.log, canned failures per subcommand. No real server, ever.
        def bus_calls():
            return open(bus_log).read().splitlines() if os.path.exists(bus_log) else []

        # (a) default (no bus yaml): ensure then token with exactly this argv; the reply reports
        # the lane file's url
        open(bus_log, "w").write("")
        lanes["HB"] = os.path.join(tmp, "lane-hb")
        g(repo, "worktree", "add", "-q", "--detach", lanes["HB"])
        mks("task", "add", "--root", "--id", "HB", "--title", "bus lane", "--run", "r1")
        r = cli("arm", "--lane", lanes["HB"], "--pane", pane_of("HB"), "--task", "HB", "--run", "r1",
                "--data", data, "--phase", "P1", "--commit-grep", "x")
        assert r["ok"] is True and r["bus"] == {"ok": True, "url": "http://127.0.0.1:9999"}, r
        assert bus_calls() == [f"ensure --data {data} --run r1",
                               f"token --lane {lanes['HB']} --task HB --run r1 --data {data}"], bus_calls()
        assert json.load(open(lane_token_file(lanes["HB"])))["url"] == \
            "http://127.0.0.1:9999"
        # the token is not a coordination file inside the lane
        assert not os.path.isfile(os.path.join(lanes["HB"], ".matrixkoder-bus.json"))
        # (b) a group arm (--tasks A,B): token's --task is the FIRST listed task
        open(bus_log, "w").write("")
        lanes["HG"] = os.path.join(tmp, "lane-hg")
        g(repo, "worktree", "add", "-q", "--detach", lanes["HG"])
        mks("task", "add", "--root", "--id", "HG1", "--title", "g1", "--run", "r1")
        mks("task", "add", "--root", "--id", "HG2", "--title", "g2", "--run", "r1")
        r = cli("arm", "--lane", lanes["HG"], "--pane", pane_of("HG"), "--tasks", "HG1,HG2",
                "--run", "r1", "--data", data, "--phase", "P1", "--commit-grep", "x")
        assert r["ok"] is True and r["bus"]["ok"] is True and "task_pane_error" not in r, r
        assert bus_calls() == [f"ensure --data {data} --run r1",
                               f"token --lane {lanes['HG']} --task HG1 --run r1 --data {data}"], bus_calls()
        # (c) a data dir whose yaml says bus.enabled false: no bus command at all, arm or disarm
        databus = os.path.join(tmp, "data-busoff")
        os.makedirs(databus)
        with open(os.path.join(databus, "matrixkoder.yaml"), "w") as f:
            f.write("bus:\n  enabled: false\n")

        def mks_in(dir_, *args):
            r_ = subprocess.run([sys.executable, MK, *args, "--data", dir_],
                                capture_output=True, text=True)
            assert r_.returncode == 0, r_.stdout + r_.stderr

        mks_in(databus, "run", "init", "--id", "r1", "--feature", "F", "--branch", "b")
        mks_in(databus, "task", "add", "--root", "--id", "HO", "--title", "off", "--run", "r1")
        open(bus_log, "w").write("")
        lanes["HO"] = os.path.join(tmp, "lane-ho")
        g(repo, "worktree", "add", "-q", "--detach", lanes["HO"])
        r = cli("arm", "--lane", lanes["HO"], "--pane", pane_of("HO"), "--task", "HO", "--run", "r1",
                "--data", databus, "--phase", "P1", "--commit-grep", "x")
        assert r["ok"] is True and "task_pane_error" not in r and \
            r["bus"] == {"ok": True, "skipped": "bus.enabled is false"}, r
        assert bus_calls() == [], bus_calls()
        r = cli("disarm", "--lane", lanes["HO"])
        assert r["ok"] is True and r["removed"] is True and \
            r["bus"] == {"ok": True, "skipped": "bus.enabled is false"}, r
        assert bus_calls() == [], bus_calls()
        # (d) ensure fails (exit 2, {"ok": false, "error": "boom"}) → arm still ok:true,
        # bus.ok:false carrying "boom", and token was NOT called
        open(os.path.join(tmp, "bus_ensure_rc.txt"), "w").write("2")
        open(os.path.join(tmp, "bus_ensure_out.txt"), "w").write('{"ok": false, "error": "boom"}\n')
        open(bus_log, "w").write("")
        r = cli("arm", "--lane", lanes["HB"], "--pane", pane_of("HB"), "--task", "HB", "--run", "r1",
                "--data", data, "--phase", "P2", "--commit-grep", "x")
        assert r["ok"] is True and r["bus"]["ok"] is False and "boom" in r["bus"]["error"], r
        assert bus_calls() == [f"ensure --data {data} --run r1"], bus_calls()
        os.unlink(os.path.join(tmp, "bus_ensure_rc.txt"))
        os.unlink(os.path.join(tmp, "bus_ensure_out.txt"))
        # (e) token fails after a good ensure → bus.ok:false, arm still ok:true
        open(os.path.join(tmp, "bus_token_rc.txt"), "w").write("2")
        open(os.path.join(tmp, "bus_token_out.txt"), "w").write('{"ok": false, "error": "no token"}\n')
        r = cli("arm", "--lane", lanes["HB"], "--pane", pane_of("HB"), "--task", "HB", "--run", "r1",
                "--data", data, "--phase", "P2", "--commit-grep", "x")
        assert r["ok"] is True and r["bus"]["ok"] is False and "no token" in r["bus"]["error"], r
        os.unlink(os.path.join(tmp, "bus_token_rc.txt"))
        os.unlink(os.path.join(tmp, "bus_token_out.txt"))
        # (f) disarm on an armed lane → revoke --lane … --data <watch's data>; no watch →
        # revoke --lane only (ambient bus.enabled, no yaml → true); a failing revoke → disarm
        # still ok:true, bus.ok:false
        open(bus_log, "w").write("")
        r = cli("disarm", "--lane", lanes["HB"])
        assert r["ok"] is True and r["removed"] is True and r["bus"] == {"ok": True}, r
        assert bus_calls() == [f"revoke --lane {lanes['HB']} --data {data}"], bus_calls()
        open(bus_log, "w").write("")
        r = cli("disarm", "--lane", lanes["HB"])
        assert r["ok"] is True and r["removed"] is False and r["bus"] == {"ok": True}, r
        assert bus_calls() == [f"revoke --lane {lanes['HB']}"], bus_calls()
        lanes["HB2"] = os.path.join(tmp, "lane-hb2")
        g(repo, "worktree", "add", "-q", "--detach", lanes["HB2"])
        mks("task", "add", "--root", "--id", "HB2", "--title", "b2", "--run", "r1")
        r = cli("arm", "--lane", lanes["HB2"], "--pane", pane_of("HB2"), "--task", "HB2",
                "--run", "r1", "--data", data, "--phase", "P1", "--commit-grep", "x")
        assert r["ok"] is True and r["bus"]["ok"] is True, r
        open(os.path.join(tmp, "bus_revoke_rc.txt"), "w").write("2")
        open(os.path.join(tmp, "bus_revoke_out.txt"), "w").write('{"ok": false, "error": "revoke boom"}\n')
        r = cli("disarm", "--lane", lanes["HB2"])
        assert r["ok"] is True and r["removed"] is True and r["bus"]["ok"] is False and \
            "revoke boom" in r["bus"]["error"], r
        os.unlink(os.path.join(tmp, "bus_revoke_rc.txt"))
        os.unlink(os.path.join(tmp, "bus_revoke_out.txt"))

        # --- the project store (D5A.2): verdict_for reads the store inside the per-lane lock ---
        # L4 (C4): the store is always on — every mk-state write above created and used mk.db
        # (no file views, no `store enable` gate any more).
        assert os.path.getsize(os.path.join(data, "mk.db")) > 0
        # store-first: no lane status file, the tasks row + report artifact decide -> DONE, source=store
        mks("task", "add", "--root", "--id", "S1", "--title", "store")
        mks("task", "set", "S1", "status=building")
        lanes["S1"] = os.path.join(tmp, "lane-s1")
        g(repo, "worktree", "add", "-q", "--detach", lanes["S1"])
        g(lanes["S1"], "commit", "-q", "--allow-empty", "-m", "feat(p1): store core")
        td = os.path.join(data, "runs", "r1", "tasks", "S1")
        os.makedirs(td, exist_ok=True)
        open(os.path.join(td, "report-r1.md"), "w").write("# report\nstore\n")
        mks("task", "round", "S1", "--kind", "build")
        os.unlink(os.path.join(td, "report-r1.md"))   # only the artifact remains
        mks("task", "set", "S1", "status=verifying")
        open(pane_f, "w").write("> dispatch-s1\nquiet, no token\n")
        open(status_f, "w").write("done")
        arm("S1", phase="S1 R1", report="report.json", prompt_marker="dispatch-s1")
        assert not os.path.exists(os.path.join(lanes["S1"], ".matrixkoder-status.json"))
        r = event("done", tid="S1")
        assert r["verdict"] == "DONE" and r["recorded"], r
        s1 = [e for e in events() if e.get("task") == "S1" and "lane-watch" in (e.get("text") or "")][-1]["text"]
        assert "source=store" in s1 and "source=status" not in s1, s1
        # DONE still needs the commit: a store row of verifying with a non-matching subject is
        # never DONE. decide() is what verdict_for feeds; the row comes from the store.
        mks("task", "add", "--root", "--id", "S2", "--title", "store2")
        mks("task", "set", "S2", "status=building")
        mks("task", "round", "S2", "--kind", "build")
        mks("task", "set", "S2", "status=verifying")
        armed = {"run": "r1", "task": "S2", "phase": "S2 R1", "data": data}
        st = lw.read_status(lanes["H1"], armed)  # the lane dir is irrelevant: the row is the channel
        assert st and st["source"] == "store" and st["state"] == "done", st
        h = {"state": "OK", "agent_status": "done", "last_change_min": 5}
        state, ev = lw.decide(h, "abc chore: wip", re.compile("nomatch"), True, "", "S2 R1", 1, st, armed)
        assert state == "STALL" and ev[-1] == "source=pane", (state, ev)

        # fallback: the row is round 2, so phase S3 R1 has no row -> the status file decides
        mks("task", "add", "--root", "--id", "S3", "--title", "fallback")
        mks("task", "set", "S3", "status=building")
        lanes["S3"] = os.path.join(tmp, "lane-s3")
        g(repo, "worktree", "add", "-q", "--detach", lanes["S3"])
        g(lanes["S3"], "commit", "-q", "--allow-empty", "-m", "feat(p1): file core")
        open(os.path.join(lanes["S3"], "report.json"), "w").write("{}")
        mks("task", "round", "S3", "--kind", "build")
        mks("task", "round", "S3", "--kind", "fix")   # round 2: phase S3 R1 has no store row
        mks("task", "set", "S3", "status=verifying")
        open(pane_f, "w").write("> dispatch-s3\nquiet, no token\n")
        open(status_f, "w").write("done")
        arm("S3", phase="S3 R1", report="report.json", prompt_marker="dispatch-s3")
        wstat("S3", "done", time.time() + 5)
        r = event("done", tid="S3")
        assert r["verdict"] == "DONE" and r["recorded"], r
        s3 = [e for e in events() if e.get("task") == "S3" and "lane-watch" in (e.get("text") or "")][-1]["text"]
        assert "source=status" in s3 and "source=store" not in s3, s3
        # sweep reads the store too: a quiet lane whose row says done is DONE, source=store
        mks("task", "add", "--root", "--id", "S4", "--title", "sweep")
        mks("task", "set", "S4", "status=building")
        lanes["S4"] = os.path.join(tmp, "lane-s4")
        g(repo, "worktree", "add", "-q", "--detach", lanes["S4"])
        g(lanes["S4"], "commit", "-q", "--allow-empty", "-m", "feat(p1): sweep core")
        td = os.path.join(data, "runs", "r1", "tasks", "S4")
        os.makedirs(td, exist_ok=True)
        open(os.path.join(td, "report-r1.md"), "w").write("# report\n")
        mks("task", "round", "S4", "--kind", "build")
        os.unlink(os.path.join(td, "report-r1.md"))
        mks("task", "set", "S4", "status=verifying")
        open(pane_f, "w").write("quiet\n")
        open(status_f, "w").write("idle")
        arm("S4", phase="S4 R1", report="report.json")
        r = cli("sweep", "--data", data)
        v = {x["task"]: x for x in r["verdicts"]}
        assert v.get("S4", {}).get("verdict") == "DONE", r
        assert "source=store" in [e for e in events() if e.get("task") == "S4" and "lane-watch" in (e.get("text") or "")][-1]["text"]

        # (D7B) the job hint calls the real function. The lookup uses the HERE path,
        # so the cwd does not matter, and the arithmetic goes through selftest_jobs_hint.
        os.environ.pop("MK_SELFTEST_JOBS", None)
        saved_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            away = selftest_jobs_total(data)
            os.chdir(os.path.join(tmp, "repo"))
            home = selftest_jobs_total(data)
        finally:
            os.chdir(saved_cwd)
        assert away == home and 1 <= away <= 16, (away, home)
        for total, active, expect in ((4, 1, "4"), (4, 2, "2"), (4, 5, "1"),
                                      (8, 1, "8"), (8, 2, "4"), (8, 5, "1")):
            real_total = selftest_jobs_total(data)
            got = selftest_jobs_hint(data, active=active)
            assert got == str(max(1, real_total // active)), (got, real_total, active)
            assert str(max(1, total // (active or 1))) == expect
        os.environ["MK_SELFTEST_JOBS"] = "7"
        try:
            assert selftest_jobs_hint(data, active=1) == "7"
        finally:
            os.environ.pop("MK_SELFTEST_JOBS", None)
        # a value outside 1..16 is refused by the same bound selftest.sh uses
        # a value outside 1..16 is refused: the lookup is stubbed to print each one
        real_here = HERE
        for bad in (17, 0, -1, "abc"):
            os.environ.pop("MK_SELFTEST_JOBS", None)
            globals()["HERE"] = tmp
            open(os.path.join(tmp, "mk-config.py"), "w").write(
                "import json,sys\nprint(json.dumps({'ok':True,'value':%s}))\n" % json.dumps(bad))
            assert selftest_jobs_total(data) == 4, bad
        globals()["HERE"] = real_here

        # (D7B) the bus is down here (the fake never listens), so --connect keeps the prompt
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H1"), "--text", "the full prompt stays",
                "--connect")
        assert r["delivered"] is True, r
        assert prompt_calls()[n0:][-1].endswith("the full prompt stays --wait --timeout 25000"), \
            prompt_calls()[n0:]
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H1"), "--text", "the full prompt stays")
        assert prompt_calls()[n0:][-1].endswith("the full prompt stays --wait --timeout 25000"), \
            prompt_calls()[n0:]
        # (G1) the reply names the path: no doorbell, and why, on an armed lane whose bus is down
        assert r["connect"] is False and r["note"] == "legacy prompt: bus is down", r

        # (D7B R3) bus up: a real /health plus bus.json. The suite's bus is the fake, so
        # "published" is the recorded send argv. No brief -> the full prompt and the reply names
        # the error. A brief -> the doorbell. A send the fake refuses -> the full prompt again.
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class _H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok": true, "status": "healthy"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a):
                pass
        _srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=_srv.serve_forever, daemon=True).start()
        # store enable earlier left mk.db. The bus home is $MK_HOME (mk-bus.root_bus_dir),
        # not MATRIXKODER_HOME. Pin it for this block, then write bus.json AFTER arm:
        # arm's ensure must not be the writer.
        _saved_bus_home = os.environ.get("MK_HOME")
        _saved_bus_mhome = os.environ.get("MATRIXKODER_HOME")
        _bus_home = os.path.join(tmp, "home")
        os.environ["MK_HOME"] = _bus_home
        os.environ["MATRIXKODER_HOME"] = _bus_home
        try:
            _bus_dir = bus_dir_for(data)
            assert _bus_dir == os.path.abspath(_bus_home), _bus_dir
            os.makedirs(_bus_dir, exist_ok=True)
            # owned: the brief written below makes `task check` require it (D1.5.4) — a briefed task
            # that owns no file is exactly what that check refuses, so the fixture declares one
            mks("task", "add", "--root", "--id", "NB", "--title", "no brief", "--owned", "scripts/nb.py")
            lanes["NB"] = os.path.join(tmp, "lane-nb")
            g(repo, "worktree", "add", "-q", "--detach", lanes["NB"])
            arm("NB")
            open(os.path.join(_bus_dir, "bus.json"), "w").write(
                json.dumps({"port": _srv.server_address[1]}))
            assert bus_is_up(data) is True, (_bus_dir, open(os.path.join(_bus_dir, "bus.json")).read())
            _td = os.path.join(data, "runs", "r1", "tasks", "NB")
            os.makedirs(_td, exist_ok=True)
            open(bus_log, "w").write("")
            n0 = len(prompt_calls())
            r = cli("dispatch", "--pane", pane_of("NB"), "--text", "the full prompt stays", "--connect")
            assert r["delivered"] is True and r.get("publish_error"), r
            assert prompt_calls()[n0:][-1].endswith("the full prompt stays --wait --timeout 25000"), \
                prompt_calls()[n0:]
            open(os.path.join(_td, "brief-r1.md"), "w").write("# the brief\nhello lane\n")
            open(os.path.join(tmp, "pane_list_out.txt"), "w").write(json.dumps(
                {"result": {"panes": [{"pane_id": pane_of("NB"), "agent_status": "idle"}]}}) + "\n")
            open(bus_log, "w").write("")
            n0 = len(prompt_calls())
            r = cli("dispatch", "--pane", pane_of("NB"), "--text", "the full prompt stays", "--connect",
                    extra={"MK_DOORBELL_IDLE_WAIT_S": "5", "MK_DOORBELL_IDLE_POLL_S": "0.05"})
            assert r["delivered"] is True and "publish_error" not in r, r
            _typed = prompt_calls()[n0:][-1]
            assert "mk-builder.py connect" in _typed and "the full prompt stays" not in _typed, _typed
            # E3: the doorbell carries --data, so the lane's first command needs no env
            assert f"--data {data}" in _typed, _typed
            # 151a: the doorbell ends with the act-without-asking sentence, on a single line
            # (prompt_calls keeps the first line only, so the sentence surviving proves one line)
            assert DOORBELL_ACT in _typed, _typed
            # (G1) no flag: a build dispatch to an armed lane with a live bus rings the doorbell
            assert r["connect"] is True and "note" not in r, r
            n0 = len(prompt_calls())
            r = cli("dispatch", "--pane", pane_of("NB"), "--text", "the full prompt stays",
                    extra={"MK_DOORBELL_IDLE_WAIT_S": "5", "MK_DOORBELL_IDLE_POLL_S": "0.05"})
            assert r["delivered"] is True and r["connect"] is True, r
            assert "mk-builder.py connect" in prompt_calls()[n0:][-1], prompt_calls()[n0:]
            # (G1) --no-connect forces the legacy prompt and the reply says so
            n0 = len(prompt_calls())
            r = cli("dispatch", "--pane", pane_of("NB"), "--text", "the full prompt stays", "--no-connect")
            assert r["delivered"] is True and r["connect"] is False, r
            assert r["note"] == "legacy prompt: --no-connect given", r
            assert prompt_calls()[n0:][-1].endswith("the full prompt stays --wait --timeout 25000"), \
                prompt_calls()[n0:]
            # (G1) --role review never rings the doorbell, and gets no legacy note
            n0 = len(prompt_calls())
            r = cli("dispatch", "--pane", pane_of("NB"), "--text", "the full prompt stays", "--role", "review")
            assert r["delivered"] is True and r["connect"] is False and "note" not in r, r
            assert prompt_calls()[n0:][-1].endswith("the full prompt stays --wait --timeout 25000"), \
                prompt_calls()[n0:]
            # review over the bus: --lane names the builder's armed lane; with no review brief the
            # typed prompt stays and the failure is posted for the watch
            open(os.path.join(tmp, "bus_log.txt"), "a").close()
            n0, b0 = len(prompt_calls()), len(bus_calls())
            r = cli("dispatch", "--pane", pane_of("NB"), "--text", "the full prompt stays", "--role", "review",
                    "--lane", lanes["NB"])
            assert r["delivered"] is True and r["connect"] is False, r
            assert r["note"] == "legacy prompt: publish failed: no review brief", r
            assert any("BRIEF NOT ON BUS" in c for c in bus_calls()[b0:]), bus_calls()[b0:]
            # with a review brief it is published as its own kind and the pane gets connect --role review
            open(os.path.join(_td, "review-brief-r1.md"), "w").write("# NB review brief\n")
            # todo 88: herdr lists agents and none sits in the pane → refused, nothing published
            open(os.path.join(tmp, "agent_list_out.txt"), "w").write(json.dumps(
                {"result": {"agents": [{"pane_id": "elsewhere"}]}}) + "\n")
            n0, b0 = len(prompt_calls()), len(bus_calls())
            r = cli("dispatch", "--pane", pane_of("NB"), "--text", "the full prompt stays", "--role", "review",
                    "--lane", lanes["NB"])
            assert r["ok"] is False and "no agent in pane" in r["error"], r
            assert bus_calls()[b0:] == [] and prompt_calls()[n0:] == [], (bus_calls()[b0:], prompt_calls()[n0:])
            os.unlink(os.path.join(tmp, "agent_list_out.txt"))
            n0, b0 = len(prompt_calls()), len(bus_calls())
            r = cli("dispatch", "--pane", pane_of("NB"), "--text", "the full prompt stays", "--role", "review",
                    "--lane", lanes["NB"])
            assert r["delivered"] is True and r["connect"] is True and r.get("status_unchanged") is True, r
            assert "kind:review-brief" in " ".join(bus_calls()[b0:]) and "NB review brief" in " ".join(bus_calls()[b0:]), bus_calls()[b0:]
            assert "connect --data" in prompt_calls()[n0:][-1] and "--role review" in prompt_calls()[n0:][-1], \
                prompt_calls()[n0:]
            os.unlink(os.path.join(_td, "review-brief-r1.md"))
            # E3: the doorbell waits for idle. The pane starts working; the first pane list
            # says so, the second says idle. The typed line comes after the idle observation.
            open(os.path.join(tmp, "pane_list_q.txt"), "w").write("1 2")
            open(os.path.join(tmp, "pane_list_out1.txt"), "w").write(json.dumps(
                {"result": {"panes": [{"pane_id": pane_of("NB"), "agent_status": "working"}]}}) + "\n")
            open(os.path.join(tmp, "pane_list_out2.txt"), "w").write(json.dumps(
                {"result": {"panes": [{"pane_id": pane_of("NB"), "agent_status": "idle"}]}}) + "\n")
            open(os.path.join(tmp, "calls.log"), "w").write("")
            r = cli("dispatch", "--pane", pane_of("NB"), "--text", "the full prompt stays", "--connect",
                    extra={"MK_DOORBELL_IDLE_WAIT_S": "5", "MK_DOORBELL_IDLE_POLL_S": "0.05"})
            assert r["delivered"] is True, r
            log = open(os.path.join(tmp, "calls.log")).read().splitlines()
            lists = [i for i, c in enumerate(log) if c.startswith("pane list")]
            prompts = [i for i, c in enumerate(log) if "agent prompt" in c]
            assert lists and prompts and min(lists) < min(prompts), log
            assert any("connect --data" in c for c in log), log
            # E3 R2: a pane that stays working is never typed the doorbell
            open(os.path.join(tmp, "pane_list_q.txt"), "w").write("")
            open(os.path.join(tmp, "pane_list_out.txt"), "w").write(json.dumps(
                {"result": {"panes": [{"pane_id": pane_of("NB"), "agent_status": "working"}]}}) + "\n")
            open(os.path.join(tmp, "calls.log"), "w").write("")
            n0 = len(prompt_calls())
            r = cli("dispatch", "--pane", pane_of("NB"), "--text", "the full prompt stays", "--connect",
                    extra={"MK_DOORBELL_IDLE_WAIT_S": "0.3", "MK_DOORBELL_IDLE_POLL_S": "0.1"}, code=1)
            # nothing at all is typed into a pane that stays working
            assert r["delivered"] is False and "pane not idle" in r.get("publish_error", ""), r
            assert prompt_calls()[n0:] == [], prompt_calls()[n0:]
            # E3 R2: lane and data are quoted, so a space or metacharacters stay one argument
            odd = os.path.join(tmp, "data dir;$(touch x)")
            os.makedirs(odd, exist_ok=True)
            open(os.path.join(odd, "mk.db"), "w").write("")
            open(os.path.join(tmp, "pane_list_out.txt"), "w").write(json.dumps(
                {"result": {"panes": [{"pane_id": pane_of("NB"), "agent_status": "idle"}]}}) + "\n")
            n0 = len(prompt_calls())
            r = cli("dispatch", "--pane", pane_of("NB"), "--text", "the full prompt stays", "--connect",
                    extra={"MK_DOORBELL_IDLE_WAIT_S": "0.3"})
            # the armed watch still points at the plain data dir; quote the builder directly
            quoted = connect_text(os.path.join(tmp, "lane name"), odd)
            assert "';$(touch x)'" in quoted or "data dir;" in quoted, quoted
            assert "'" in quoted and "$(touch x)" in quoted, quoted
            assert quoted.endswith(DOORBELL_ACT) and "\n" not in quoted, quoted
            assert connect_text("l", "", " --role review").endswith(" --role review " + DOORBELL_ACT)
            # mk.db is already present (store enable, above), so the send is kind task, not info
            assert any("--kind task" in c and "kind:brief" in c for c in [" ".join(bus_calls())]), bus_calls()
            # a refused send keeps the full prompt and names the error
            open(os.path.join(tmp, "bus_send_rc.txt"), "w").write("2")
            open(os.path.join(tmp, "bus_send_out.txt"), "w").write('{"ok": false, "error": "too big"}\n')
            open(bus_log, "w").write("")
            n0 = len(prompt_calls())
            r = cli("dispatch", "--pane", pane_of("NB"), "--text", "the full prompt stays", "--connect")
            assert r.get("publish_error") == "too big", r
            assert prompt_calls()[n0:][-1].endswith("the full prompt stays --wait --timeout 25000"), \
                prompt_calls()[n0:]
            assert r["connect"] is False and r["note"] == "legacy prompt: publish failed: too big", r
            # the failure is also posted for the stage 6 watch, not only named in the reply
            alerts = [c for c in bus_calls() if "--kind info" in c and "BRIEF NOT ON BUS" in c]
            assert alerts and "--to orch" in alerts[-1] and "too big" in alerts[-1], bus_calls()
            os.unlink(os.path.join(tmp, "bus_send_rc.txt"))
            os.unlink(os.path.join(tmp, "bus_send_out.txt"))
            os.unlink(os.path.join(_bus_dir, "bus.json"))
        finally:
            if _saved_bus_home is None:
                os.environ.pop("MK_HOME", None)
            else:
                os.environ["MK_HOME"] = _saved_bus_home
            if _saved_bus_mhome is None:
                os.environ.pop("MATRIXKODER_HOME", None)
            else:
                os.environ["MATRIXKODER_HOME"] = _saved_bus_mhome

        # (D1.5.3/D1.5.4) the dispatch route write + the mandatory-field check, against the
        # REAL mk-state (no fake: the route lands in run.json, and in mk.db's route_json now
        # that store enable above turned the store on). The check gates the dispatch BEFORE
        # anything is typed: a failing task refuses with ok:false/delivered:false, no herdr
        # prompt call, no fresh-session reset; --force sends and the reply says it was forced.
        # A passing task gets the route on it (and on every listed task of a group lane).
        def task_get(tid):
            r_ = subprocess.run([sys.executable, MK, "task", "get", tid, "--data", data,
                                 "--run", "r1"], capture_output=True, text=True)
            assert r_.returncode == 0, r_.stdout + r_.stderr
            return json.loads(r_.stdout.strip().splitlines()[-1])["task"]

        def route_json_of(tid):
            import sqlite3
            # read-only (mode=ro): the single-writer rule lets only mkdb.py open SQLite for
            # writing (mkdb.py _single_writer), and this helper only reads a column
            con = sqlite3.connect("file:%s?mode=ro" % os.path.join(data, "mk.db"), uri=True)
            try:
                row = con.execute("SELECT route_json FROM tasks WHERE run=? AND id=?",
                                  ("r1", tid)).fetchone()
            finally:
                con.close()
            return row[0] if row else None

        # a passing task: title + acceptance + parent set, no brief file (so owned is not
        # required), status building so the dispatch's board write has something to move
        mks("task", "add", "--root", "--id", "DP1", "--title", "Phase one", "--run", "r1")
        mks("task", "add", "--id", "DOK", "--title", "ok task", "--parent", "DP1", "--run", "r1")
        mks("task", "set", "DOK", "status=building")
        # the store is on here, so `task check` reads the tasks row: plant the acceptance the
        # way `mk-plan approve` writes it (through mkdb, the one writer), then confirm via
        # THIS dispatch module's own task_check (real mk-state) that the check passes
        _c = mkdb.open_project(os.path.join(data, ".mk-home"), "matrixkoder", data_dir=data)
        try:
            _c.execute("UPDATE tasks SET acceptance=? WHERE run='r1' AND id='DOK'",
                       ("the check passes",))
            _c.commit()
        finally:
            _c.close()
        ok_chk, missing_chk, _err = task_check({"data": data, "run": "r1"}, "DOK")
        assert ok_chk is True and missing_chk == [], (ok_chk, missing_chk)
        lanes["DOK"] = os.path.join(tmp, "lane-dok")
        g(repo, "worktree", "add", "-q", "--detach", lanes["DOK"])
        mks("task", "round", "DOK", "--kind", "build")  # a dispatched task has a round
        arm("DOK", phase="DOK R1", kind="opencode")
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "idle"}}}\n')
        set_canned("prompt_err.txt", "")
        set_canned("prompt_rc.txt", "0")
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [
            {"pane_id": pane_of("DOK"), "agent_status": "idle"}]}}) + "\n")
        # (a) --agent/--model/--effort: delivered, the route is on the task (file + store) and
        # the reply carries it
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("DOK"), "--text", "go build",
                "--agent", "opencode", "--model", "m", "--effort", "high")
        assert r["ok"] is True and r["delivered"] is True, r
        assert r["route"] == {"agent": "opencode", "model": "m", "effort": "high"}, r
        got = task_get("DOK")
        assert (got["agent"], got["model"], got["effort"]) == ("opencode", "m", "high"), got
        assert json.loads(route_json_of("DOK")) == {"agent": "opencode", "model": "m",
                                                   "effort": "high"}, route_json_of("DOK")
        assert len(prompt_calls()) - n0 == 1, prompt_calls()[n0:]
        # (b) no --agent: the armed watch's kind is the agent, model/effort stay empty.
        # The route write only names the non-empty fields, so `model=`/`effort=` are NOT
        # sent — the flags were not given — and the previous round's values stay on the
        # task. The reply's route is what THIS dispatch recorded.
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("DOK"), "--text", "go again")
        assert r["ok"] is True and r["delivered"] is True, r
        assert r["route"] == {"agent": "opencode", "model": None, "effort": None}, r
        got = task_get("DOK")
        assert got["agent"] == "opencode", got  # the watch kind overwrote the agent
        assert json.loads(route_json_of("DOK"))["agent"] == "opencode", route_json_of("DOK")
        assert len(prompt_calls()) - n0 == 1, prompt_calls()[n0:]
        # (b2) explicit empty model/effort flags DO clear them (the flags were given)
        r = cli("dispatch", "--pane", pane_of("DOK"), "--text", "go again",
                "--model", "", "--effort", "")
        assert r["ok"] is True and r["delivered"] is True, r
        assert r["route"] == {"agent": "opencode", "model": None, "effort": None}, r
        got = task_get("DOK")
        assert got["agent"] == "opencode" and got["model"] is None and got["effort"] is None, got
        assert json.loads(route_json_of("DOK")) == {"agent": "opencode", "model": None,
                                                   "effort": None}, route_json_of("DOK")
        # (c) a FAILING task (task add sets no acceptance): the dispatch is refused — nothing
        # typed at all (no herdr prompt call, no fresh-session reset), the reply carries the
        # missing fields, exit 0 (a completed check)
        mks("task", "add", "--id", "DBAD", "--title", "bad task", "--parent", "DP1", "--run", "r1")
        mks("task", "round", "DBAD", "--kind", "build")
        lanes["DBAD"] = os.path.join(tmp, "lane-dbad")
        g(repo, "worktree", "add", "-q", "--detach", lanes["DBAD"])
        arm("DBAD", phase="DBAD R1", kind="claude")
        # the failing task's check verdict, through this module's own task_check (real
        # mk-state): task add sets no acceptance, so the check must refuse it
        ok_bad, missing_bad, _err2 = task_check({"data": data, "run": "r1"}, "DBAD")
        assert ok_bad is False and "acceptance" in missing_bad, (ok_bad, missing_bad)
        n0 = len(prompt_calls())
        n_calls0 = len(open(calls).read().splitlines()) if os.path.exists(calls) else 0
        r = cli("dispatch", "--pane", pane_of("DBAD"), "--text", "go bad", "--kind", "fix")
        assert r["ok"] is False and r["delivered"] is False, r
        assert r["check"] == {"ok": False, "missing": missing_bad}, r
        assert "task check failed" in r["error"] and "acceptance" in r["error"], r
        assert r["route"] == {"agent": "claude", "model": None, "effort": None}, r  # written first
        assert prompt_calls()[n0:] == [], prompt_calls()[n0:]  # NO herdr prompt call at all
        n_calls1 = len(open(calls).read().splitlines()) if os.path.exists(calls) else 0
        assert n_calls1 - n_calls0 == 0, "a refused dispatch made a herdr call"  # no reset either
        # (d) the same failing task with --force: delivered, and the reply says it was forced
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("DBAD"), "--text", "go bad forced", "--force")
        assert r["ok"] is True and r["delivered"] is True, r
        assert r["check"] == {"ok": False, "missing": missing_bad, "forced": True}, r
        assert len(prompt_calls()) - n0 == 1, prompt_calls()[n0:]
        # (e) --role review skips the check entirely: the failing task is not gated, and no
        # route is written. The stored route_json is STICKY (D1.5.2's merge-keep keeps the
        # column when the file view's route is empty — it cannot tell "cleared" from "never
        # set"), so the proof that review wrote nothing is that the route is UNCHANGED plus
        # the reply carrying neither `check` nor `route`.
        mks("task", "set", "DBAD", "agent=", "model=", "effort=")
        before_review = route_json_of("DBAD")
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("DBAD"), "--text", "review it", "--role", "review")
        assert r["ok"] is True and r["delivered"] is True and r["status_unchanged"] is True, r
        assert "check" not in r and "route" not in r, r
        assert "task_check_error" not in r, r
        assert len(prompt_calls()) - n0 == 1, prompt_calls()[n0:]
        assert route_json_of("DBAD") == before_review, (before_review, route_json_of("DBAD"))
        # (f) an unarmed pane dispatch: no armed watch to check or route, so the prompt goes
        # out exactly as today (the fake herdr settles) and the reply carries neither
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", "wZ:free", "--text", "anyone there")
        assert r["ok"] is True and r["delivered"] is True, r
        assert "check" not in r and "route" not in r and "task_check_error" not in r, r
        assert len(prompt_calls()) - n0 == 1, prompt_calls()[n0:]
        # (f2) a pre-`task check` mk-state ($MK_STATE_CMD fake: argparse's usage error on
        # stderr, exit 2): the dispatch is NOT gated — it delivers, and the reply carries
        # the mk-state error as task_check_error, never failing the verdict
        fake_old = ("#!/usr/bin/env python3\n"
                    "import sys\n"
                    "sys.stderr.write(\"mk-state.py task: error: argument action: "
                    "invalid choice: 'check'...\\n\")\n"
                    "sys.exit(2)\n")
        with open(os.path.join(bin_, "mk-state-old"), "w") as f_:
            f_.write(fake_old)
        os.chmod(os.path.join(bin_, "mk-state-old"), 0o755)
        lane_old = os.path.join(tmp, "lane-dold")
        lanes["DOLD"] = lane_old
        g(repo, "worktree", "add", "-q", "--detach", lane_old)
        mks("task", "add", "--id", "DOLD", "--title", "old mk-state", "--parent", "DP1", "--run", "r1")
        arm("DOLD", phase="DOLD R1", kind="opencode")
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("DOLD"), "--text", "go old",
                extra={"MK_STATE_CMD": os.path.join(bin_, "mk-state-old")})
        assert r["ok"] is True and r["delivered"] is True, r
        assert "task_check_error" in r and "invalid choice" in r["task_check_error"], r
        assert "check" not in r, r  # a passed-but-unavailable check is not a check verdict
        assert len(prompt_calls()) - n0 == 1, prompt_calls()[n0:]
        # (g) a group lane (--tasks): the route write lands on EVERY listed task, and the
        # check runs on the lead task only
        mks("task", "add", "--id", "DG1", "--title", "g1", "--parent", "DP1", "--run", "r1")
        mks("task", "add", "--root", "--id", "DG2", "--title", "g2", "--run", "r1")
        mks("task", "round", "DG1", "--kind", "build")
        for tid in ("DG1", "DG2"):
            # the store is the read source: plant the acceptance the way `mk-plan approve`
            # writes it, through mkdb (the one writer)
            _c = mkdb.open_project(os.path.join(data, ".mk-home"), "matrixkoder", data_dir=data)
            try:
                _c.execute("UPDATE tasks SET acceptance=? WHERE run='r1' AND id=?", ("group ok", tid))
                _c.commit()
            finally:
                _c.close()
        lanes["DG"] = os.path.join(tmp, "lane-dg")
        g(repo, "worktree", "add", "-q", "--detach", lanes["DG"])
        r = cli("arm", "--lane", lanes["DG"], "--pane", pane_of("DG"), "--tasks", "DG1,DG2",
                "--run", "r1", "--data", data, "--phase", "P1", "--commit-grep", r"feat\(p1\)")
        assert r["ok"] and "task_pane_error" not in r, r
        r = cli("dispatch", "--pane", pane_of("DG"), "--text", "go group",
                "--agent", "codex", "--model", "gpt-5", "--effort", "low")
        assert r["ok"] is True and r["delivered"] is True, r
        assert r["route"] == {"agent": "codex", "model": "gpt-5", "effort": "low"}, r
        for tid in ("DG1", "DG2"):
            got = task_get(tid)
            assert (got["agent"], got["model"], got["effort"]) == ("codex", "gpt-5", "low"), (tid, got)
            assert json.loads(route_json_of(tid))["agent"] == "codex", (tid, route_json_of(tid))

        # (D7B R3) store mode: mk.db present, so bus.json lives in the MK home, not the data dir.
        # A stale <data>/bus.json must not win. A real store-mode bus, real acquire and arm.
        # L4 (C4): <data>/mk.db is the real store now, not an empty marker. It must survive
        # this case (deleting it would take run r1 with it — there is no file view to fall
        # back on). The stale <data>/bus.json below is the point: bus.json resolves to the
        # MK home when the store is present, never the leftover data-dir file.
        _home = os.path.join(tmp, "mkhome")
        os.makedirs(_home, exist_ok=True)
        open(os.path.join(data, "bus.json"), "w").write(json.dumps({"port": 1}))
        _saved_home = os.environ.get("MK_HOME")
        _saved_mhome = os.environ.get("MATRIXKODER_HOME")
        _saved_bdir = os.environ.get("MK_BUS_DIR")
        os.environ["MK_HOME"] = _home
        os.environ["MATRIXKODER_HOME"] = _home
        os.environ.pop("MK_BUS_DIR", None)
        try:
            assert os.path.isfile(os.path.join(data, "mk.db")), "the store must exist here"
            assert bus_dir_for(data) == mkpaths.target_bus_dir(), bus_dir_for(data)
            assert bus_is_up(data) is False
            os.makedirs(mkpaths.target_bus_dir(), exist_ok=True)
            open(mkpaths.bus_json(data), "w").write(
                json.dumps({"port": _srv.server_address[1]}))
            assert bus_is_up(data) is True  # the home bus, not the stale data-dir file
        finally:
            if _saved_home is None:
                os.environ.pop("MK_HOME", None)
            else:
                os.environ["MK_HOME"] = _saved_home
            if _saved_mhome is None:
                os.environ.pop("MATRIXKODER_HOME", None)
            else:
                os.environ["MATRIXKODER_HOME"] = _saved_mhome
            if _saved_bdir is None:
                os.environ.pop("MK_BUS_DIR", None)
            else:
                os.environ["MK_BUS_DIR"] = _saved_bdir
        _srv.shutdown()
        os.unlink(os.path.join(data, "bus.json"))

        # (D7B.2) review-handoff. The bus is the fake, so "sent" is the recorded argv.
        rev_dir = os.path.join(data, "runs", "r1", "tasks", "RH")
        os.makedirs(rev_dir, exist_ok=True)
        mks("task", "add", "--root", "--id", "RH", "--title", "handoff")
        mks("task", "round", "RH", "--kind", "build")
        open(os.path.join(rev_dir, "review-r1.md"), "w").write(
            "# RH review — REQUEST CHANGES\n\n## HIGH\n"
            "- scripts/a.py:10 — leak — close it\n\n## LOW\n"
            "- scripts/b.py:2 — nit — tidy\n")
        open(bus_log, "w").write("")
        r = cli("review-handoff", "--task", "RH", "--run", "r1", "--data", data, "--min", "medium")
        assert r["ok"] is True and r["verdict"] == "REQUEST CHANGES" and r["dispatched"] is True, r
        assert r["round"] == 2 and r["required"] == 1, r
        brief = open(r["brief"]).read()
        assert "## FIX" in brief and "[HIGH]" in brief and "not required" in brief and "[LOW]" in brief, brief
        # E3: the brief is filled from the task, never the raw template
        assert "<PHASE>" not in brief and "<path>" not in brief, brief
        assert "scripts/a.py:10" in brief and "review-r1.md" in brief, brief
        # E3 R2: the generated brief passes brief-check, with and without a previous brief
        import importlib.util as _iu
        _spec = _iu.spec_from_file_location("briefcheck", os.path.join(HERE, "brief-check.py"))
        _bc = _iu.module_from_spec(_spec)
        _spec.loader.exec_module(_bc)
        assert _bc.placeholder_failures(brief) == [], _bc.placeholder_failures(brief)
        mks("task", "add", "--root", "--id", "RN", "--title", "no previous brief")
        no_prev = filled_brief(data, "r1", "RN", 2, os.path.join(rev_dir, "review-r1.md"), "1. [HIGH] a:1 — p — f")
        assert _bc.placeholder_failures(no_prev) == [], _bc.placeholder_failures(no_prev)
        assert "Co-Authored-By: <" not in no_prev, no_prev
        bcalls = bus_calls()
        joined = " ".join(bcalls)
        assert joined.startswith("send ") and "--from orch" in joined and "--kind info" in joined \
            and "kind:brief" in joined, bcalls
        # APPROVE records the verdict and dispatches nothing
        mks("task", "add", "--root", "--id", "RA", "--title", "approve")
        mks("task", "round", "RA", "--kind", "build")
        ad = os.path.join(data, "runs", "r1", "tasks", "RA")
        os.makedirs(ad, exist_ok=True)
        open(os.path.join(ad, "review-r1.md"), "w").write("# RA review — APPROVE\n\n## Verified\n- ok\n")
        open(bus_log, "w").write("")
        r = cli("review-handoff", "--task", "RA", "--run", "r1", "--data", data)
        assert r["ok"] is True and r["verdict"] == "APPROVE" and r["dispatched"] is False, r
        assert bus_calls() == [], bus_calls()
        # --hold records the veto text and does not send the wake
        mks("task", "add", "--root", "--id", "HH", "--title", "hold")
        mks("task", "round", "HH", "--kind", "build")
        hd = os.path.join(data, "runs", "r1", "tasks", "HH")
        os.makedirs(hd, exist_ok=True)
        open(os.path.join(hd, "review-r1.md"), "w").write(
            "# HH review — REQUEST CHANGES\n\n## CRITICAL\n- x:1 — boom — stop\n")
        open(bus_log, "w").write("")
        r = cli("review-handoff", "--task", "HH", "--run", "r1", "--data", data, "--hold")
        assert r["ok"] is True and r["held"] is True and r["dispatched"] is False, r
        hh = store_read("SELECT id, round FROM tasks WHERE run='r1' AND id='HH'")[0]
        assert hh["round"] == 1, hh  # nothing opened while held
        assert bus_calls() == [], bus_calls()
        # the second call releases: one round, one send
        r = cli("review-handoff", "--task", "HH", "--run", "r1", "--data", data)
        assert r["ok"] is True and r["dispatched"] is True and r["round"] == 2, r
        # a second run on the same review opens nothing more
        r = cli("review-handoff", "--task", "RH", "--run", "r1", "--data", data)
        assert r["ok"] is True and r["dispatched"] is False and r["reason"] == "already handed off", r
        # a review with no parseable findings is held, not opened
        mks("task", "add", "--root", "--id", "RZ", "--title", "empty")
        mks("task", "round", "RZ", "--kind", "build")
        zd = os.path.join(data, "runs", "r1", "tasks", "RZ")
        os.makedirs(zd, exist_ok=True)
        open(os.path.join(zd, "review-r1.md"), "w").write(
            "# RZ review — REQUEST CHANGES\n\nno headings here\n")
        r = cli("review-handoff", "--task", "RZ", "--run", "r1", "--data", data)
        assert r["ok"] is True and r["dispatched"] is False and r["findings"] == 0, r
        # the template's own combined heading is not a severity heading
        assert review_findings("## CRITICAL / HIGH / MEDIUM / LOW\n- a:1 — p — f\n") == []
        # a wrapped bullet keeps its continuation
        got = review_findings("## HIGH\n- a:1 — p —\n  the rest of it\n")
        assert got == [{"severity": "HIGH", "text": "a:1 — p — the rest of it"}], got
        # E3: the review lives in the lane, not the task folder. It is copied in and used.
        mks("task", "add", "--root", "--id", "RL", "--title", "lane review")
        mks("task", "round", "RL", "--kind", "build")
        lane_rl = os.path.join(tmp, "lane-rl")
        os.makedirs(lane_rl, exist_ok=True)
        mks("task", "set", "RL", "lane=" + lane_rl)
        open(os.path.join(lane_rl, ".matrixkoder-review-r1.md"), "w").write(
            "# RL review — REQUEST CHANGES\n\n## MEDIUM\n- scripts/c.py:4 — gap — cover it\n")
        r = cli("review-handoff", "--task", "RL", "--run", "r1", "--data", data)
        assert r["ok"] is True and r["dispatched"] is True, r
        # E3 R2: the lane review is copied into the task folder and the brief cites the copy
        copied = os.path.join(data, "runs", "r1", "tasks", "RL", "review-r1.md")
        assert os.path.isfile(copied), copied
        got = open(r["brief"]).read()
        assert "scripts/c.py:4" in got and copied in got and "<PHASE>" not in got, got
        # the real Commands heading (longer than the old exact text) is copied
        prev = os.path.join(data, "runs", "r1", "tasks", "RL", "brief-r1.md")
        os.makedirs(os.path.dirname(prev), exist_ok=True)
        open(prev, "w").write("## Commands (wait for each; ONE run each at the end)\n0. `B start`\n\n## Rules\n- Use the mk-builder skill.\n")
        block = commands_block_of(open(prev).read())
        assert block.startswith("## Commands") and "`B start`" in block, block
        # a task with neither names both places
        mks("task", "add", "--root", "--id", "RM", "--title", "missing review")
        mks("task", "round", "RM", "--kind", "build")
        r = cli("review-handoff", "--task", "RM", "--run", "r1", "--data", data, code=2)
        assert r["ok"] is False and "review-rN.md" in r["error"] and "RM" in r["error"], r
        # an odd id is refused
        r = cli("review-handoff", "--task", "../x", "--run", "r1", "--data", data, code=2)
        assert r["ok"] is False, r

        # --- F.4: review-handoff opens risk reasons and checks the acceptance table
        def reasons(tid):
            return sorted((x["kind"], x["points"], x["source"]) for x in store_read(
                "SELECT kind, points, source FROM risk_reasons WHERE run='r1' AND task=? AND cleared IS NULL", (tid,)))
        table_ok = ("\n## Acceptance\n| # | Criterion | Verdict | Evidence |\n|---|---|---|---|\n"
                    "| 1 | a | MET | x:1 |\n| 2 | b | UNVERIFIED | no run |\n")
        assert review_acceptance(table_ok) == [{"criterion": "a", "verdict": "MET"},
                                               {"criterion": "b", "verdict": "UNVERIFIED"}]
        tmpl = open(os.path.join(HERE, "..", "skills", "matrixkoder", "templates", "review.md")).read()
        assert review_acceptance(tmpl) == [], "the template's placeholder row is not a verdict"
        assert review_acceptance("# t\n\n## HIGH\n- a\n") == []
        # APPROVE with an UNVERIFIED row -> no_evidence; the table is present, so no flag
        mks("task", "add", "--root", "--id", "F4A", "--title", "approve unverified")
        mks("task", "round", "F4A", "--kind", "build")
        fd = os.path.join(data, "runs", "r1", "tasks", "F4A")
        os.makedirs(fd, exist_ok=True)
        open(os.path.join(fd, "review-r1.md"), "w").write("# F4A review — APPROVE\n" + table_ok)
        r = cli("review-handoff", "--task", "F4A", "--run", "r1", "--data", data)
        assert r["verdict"] == "APPROVE" and r["acceptance_table"] is True and "flag" not in r, r
        assert r["risk_opened"] == ["no_evidence"], r
        assert reasons("F4A") == [("no_evidence", 1, "review R1")], reasons("F4A")
        # a repeat does not add the reason twice
        cli("review-handoff", "--task", "F4A", "--run", "r1", "--data", data)
        assert len(reasons("F4A")) == 1, reasons("F4A")
        # APPROVE, every row MET, a MEDIUM listed: medium_parked only
        mks("task", "add", "--root", "--id", "F4B", "--title", "approve met")
        mks("task", "round", "F4B", "--kind", "build")
        fd = os.path.join(data, "runs", "r1", "tasks", "F4B")
        os.makedirs(fd, exist_ok=True)
        open(os.path.join(fd, "review-r1.md"), "w").write(
            "# F4B review — APPROVE\n\n## MEDIUM\n- a:1 — gap — later\n"
            "\n## Acceptance\n| # | Criterion | Verdict | Evidence |\n|---|---|---|---|\n| 1 | a | MET | x |\n")
        r = cli("review-handoff", "--task", "F4B", "--run", "r1", "--data", data)
        assert r["risk_opened"] == ["medium_parked"], r
        assert [x[0] for x in reasons("F4B")] == ["medium_parked"], reasons("F4B")
        # REQUEST CHANGES, --min high: review_changes + the parked MEDIUM; the fix brief shows risk + gates
        mks("task", "add", "--root", "--id", "F4C", "--title", "changes")
        mks("task", "round", "F4C", "--kind", "build")
        fd = os.path.join(data, "runs", "r1", "tasks", "F4C")
        os.makedirs(fd, exist_ok=True)
        open(os.path.join(fd, "review-r1.md"), "w").write(
            "# F4C review — REQUEST CHANGES\n\n## HIGH\n- a:1 — leak — close\n\n## MEDIUM\n- b:2 — gap — later\n")
        r = cli("review-handoff", "--task", "F4C", "--run", "r1", "--data", data, "--min", "high")
        assert r["dispatched"] is True and sorted(r["risk_opened"]) == ["medium_parked", "review_changes"], r
        assert r["acceptance_table"] is False and r["flag"] == "review has no acceptance table", r
        assert [x[0] for x in reasons("F4C")] == ["medium_parked", "review_changes"], reasons("F4C")
        got = open(r["brief"]).read()
        assert "## Risk and gates" in got and "review_changes +2" in got and "Gates:" in got, got
        # claim_mismatch: a test the report says it added, absent from the branch diff
        crepo = os.path.join(tmp, "claim-repo")
        subprocess.run(["git", "init", "-q", "-b", "main", crepo], check=True)
        g(crepo, "commit", "-q", "--allow-empty", "-m", "init")
        g(crepo, "switch", "-q", "-c", "work")
        os.makedirs(os.path.join(crepo, "tests"))
        open(os.path.join(crepo, "tests", "a_test.py"), "w").write("x = 1\n")
        g(crepo, "add", "-A")
        g(crepo, "commit", "-q", "-m", "feat: a")
        assert branch_files(crepo) == {"tests/a_test.py"}, branch_files(crepo)
        rep = "Added `tests/a_test.py` and a new `tests/b_test.py`; ran `tests/old_test.py` unchanged.\n"
        assert claimed_tests(rep) == ["tests/a_test.py", "tests/b_test.py"], claimed_tests(rep)
        mks("task", "add", "--root", "--id", "F4D", "--title", "claims")
        mks("task", "round", "F4D", "--kind", "build")
        open(os.path.join(crepo, ".matrixkoder-report-r1.md"), "w").write(rep)
        w4 = {"data": data, "run": "r1", "task": "F4D", "lane": crepo, "report": ".matrixkoder-report-r1.md"}
        assert ingest_report(w4) == ["tests/b_test.py"], ingest_report(w4)
        assert reasons("F4D") == [("claim_mismatch", 2, "report R1: tests/b_test.py")], reasons("F4D")
        assert len(reasons("F4D")) == 1 and ingest_report(w4) == ["tests/b_test.py"] and len(reasons("F4D")) == 1
        # F.4 R2: bare-basename and path-suffix claims match; the round comes from report-rN.md
        assert claim_in("b_test.py", {"tests/b_test.py"}) and claim_in("tests/b_test.py", {"x/tests/b_test.py"})
        assert not claim_in("c_test.py", {"tests/b_test.py"})
        mks("task", "add", "--root", "--id", "F4F", "--title", "round name")
        mks("task", "round", "F4F", "--kind", "build")
        open(os.path.join(crepo, ".matrixkoder-report-r2.md"), "w").write("Added `tests/zz_test.py`.\n")
        assert ingest_report(dict(w4, task="F4F", report=".matrixkoder-report-r2.md")) == ["tests/zz_test.py"]
        assert reasons("F4F") == [("claim_mismatch", 2, "report R2: tests/zz_test.py")], reasons("F4F")
        # a git error on the diff is unknown: nothing is claimed missing
        _bf = globals()["branch_files"]
        globals()["branch_files"] = lambda lane_: None
        try:
            assert ingest_report(dict(w4, task="F4F", report=".matrixkoder-report-r2.md")) == []
        finally:
            globals()["branch_files"] = _bf
        # a report whose claimed tests are all in the diff opens nothing
        mks("task", "add", "--root", "--id", "F4E", "--title", "honest")
        mks("task", "round", "F4E", "--kind", "build")
        open(os.path.join(crepo, ".matrixkoder-report-r1.md"), "w").write("Added `tests/a_test.py`.\n")
        assert ingest_report(dict(w4, task="F4E")) == [] and reasons("F4E") == []

        # --- C4/L6.2: briefs, review briefs, reviews and child ids come from the project DB,
        # and the file view stays the fallback. Its own data dir, because the D7B.3 case above
        # removed <data>/mk.db and the first `store enable` left a read-only runs-legacy there.
        ddata = os.path.join(tmp, "dbdata")

        def mkd(*args):
            r = subprocess.run([sys.executable, MK, *args, "--data", ddata],
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stdout + r.stderr

        mkd("run", "init", "--id", "r1", "--feature", "DB", "--branch", "b")
        mkd("task", "add", "--root", "--id", "DB", "--title", "db reads")
        mkd("task", "add", "--id", "DB.1", "--title", "db child", "--parent", "DB")
        mkd("store", "enable")
        s_home = os.path.join(ddata, ".mk-home")   # mk-state's own store home for this data dir
        assert sorted(child_ids("DB", ddata, "r1")) == ["DB.1"], child_ids("DB", ddata, "r1")
        db_dir = os.path.join(ddata, "runs", "r1", "tasks", "DB")
        os.makedirs(db_dir, exist_ok=True)
        # (a) the brief and review brief are served from the DB with NO file on disk
        _c = mkdb.open_project(s_home, "matrixkoder", data_dir=ddata)
        try:
            assert _c.execute("SELECT 1 FROM runs WHERE id='r1'").fetchone(), "no run r1 in the store"
            mkdb.put_artifact(s_home, "matrixkoder", "r1", "DB", 1, "brief",
                              "# DB brief\nfrom the store\n", data_dir=ddata)
            mkdb.put_artifact(s_home, "matrixkoder", "r1", "DB", 1, "review-brief",
                              "# DB review brief\nfrom the store\n", data_dir=ddata)
        finally:
            _c.close()
        assert not os.path.exists(os.path.join(db_dir, "brief-r1.md")), "no file should exist yet"
        got_text, got_n = brief_body_for(ddata, "r1", "DB")
        assert got_n == 1 and "from the store" in got_text, (got_n, got_text)
        open(bus_log, "w").write("")
        ok, err = publish_review_brief(ddata, "r1", "DB")
        assert ok and err is None and "DB review brief" in open(bus_log).read(), (ok, err)
        # (b) the file fallback: a task the store knows but holds no brief for
        open(os.path.join(db_dir, "brief-r1.md"), "w").write("# file brief\nfallback\n")
        got_text, got_n = brief_body_for(ddata, "r1", "DB")
        assert got_n == 1 and "from the store" in got_text, "the DB must still win"
        mkd("task", "add", "--root", "--id", "FB", "--title", "fallback")
        fb_dir = os.path.join(ddata, "runs", "r1", "tasks", "FB")
        os.makedirs(fb_dir, exist_ok=True)
        open(os.path.join(fb_dir, "brief-r2.md"), "w").write("# file brief only\n")
        got_text, got_n = brief_body_for(ddata, "r1", "FB")
        assert got_n == 2 and "file brief only" in got_text, (got_n, got_text)
        # no store at all: the file view is the only truth (a pre-migration project)
        _nod = os.path.join(tmp, "nostore")
        shutil.copytree(os.path.join(ddata, "runs"), os.path.join(_nod, "runs"))
        assert store_rows(_nod, "SELECT id FROM runs WHERE id=?", ("r1",)) is None
        got_text, got_n = brief_body_for(_nod, "r1", "FB")
        assert got_n == 2 and "file brief only" in got_text, (got_n, got_text)
        # an UNREADABLE store file is not a crash: store_rows returns None (its connect is inside
        # the try, like its execute) and every reader falls back to the file view. Without that,
        # sqlite3.OperationalError escaped dispatch over a store file no one could read.
        _bad = os.path.join(tmp, "badstore")
        shutil.copytree(os.path.join(ddata, "runs"), os.path.join(_bad, "runs"))
        # a pre-migration file view: L4 writes no run.json, but the fallback path still reads
        # one, so the fixture must supply it (this is the case that proves the fallback works)
        open(mkpaths.run_doc(_bad, "r1"), "w").write(json.dumps(
            {"id": "r1", "tasks": [{"id": "DB", "parent": None}, {"id": "DB.1", "parent": "DB"}]}))
        bad_db = mkpaths.project_db(data=_bad)
        open(bad_db, "w").close()
        os.chmod(bad_db, 0o000)
        try:
            assert store_present(_bad), "an unreadable store is still a store"
            assert store_rows(_bad, "SELECT id FROM runs WHERE id=?", ("r1",)) is None
            got_text, got_n = brief_body_for(_bad, "r1", "FB")
            assert got_n == 2 and "file brief only" in got_text, (got_n, got_text)
            # dispatch's own read path publishes that same file brief
            open(bus_log, "w").write("")
            ok, err = publish_brief(_bad, "r1", "FB")
            assert ok and err is None and "file brief only" in open(bus_log).read(), (ok, err)
            # child ids come from run.json, not from a store nobody can open
            assert child_ids("DB", _bad, "r1") == ["DB.1"], child_ids("DB", _bad, "r1")
        finally:
            os.chmod(bad_db, 0o600)
        # (c) the review is served from the DB, with the review round and no file
        mkd("task", "round", "DB", "--kind", "build")
        _c = mkdb.open_project(s_home, "matrixkoder", data_dir=ddata)
        try:
            mkdb.put_artifact(s_home, "matrixkoder", "r1", "DB", 1, "review",
                              "# DB review — REQUEST CHANGES\n\n## HIGH\n"
                              "- scripts/x.py:9 — leak — close it\n", data_dir=ddata)
        finally:
            _c.close()
        assert not os.path.exists(os.path.join(db_dir, "review-r1.md"))
        rn, rtext, rref = review_body_for(ddata, "r1", "DB")
        assert rn == 1 and "scripts/x.py:9" in rtext and "project DB" in rref, (rn, rtext, rref)
        # a full handoff off the DB review: the FIX brief cites the DB location, not a file
        open(bus_log, "w").write("")
        r = cli("review-handoff", "--task", "DB", "--run", "r1", "--data", ddata)
        assert r["ok"] is True and r["dispatched"] is True and r["round"] == 2, r
        got = open(r["brief"]).read()
        assert "scripts/x.py:9" in got and "project DB" in got, got
        assert "<PHASE>" not in got and "<path>" not in got, got
        # (d) the token path: mkpaths decides. A token still at the pre-L6.1 <home>/bus-lanes
        # is carried over to the bus dir's lanes/ once it is there (C6, L6.4). Both paths come
        # from mkpaths' own constants and its bus-dir rule — the OLD one from the
        # `BUS_LANES_DIR` the take-over copies from — so the old-layout fallback is tested,
        # never a spelling copied here.
        lane_tok = os.path.join(tmp, "lane-token")
        os.makedirs(lane_tok, exist_ok=True)
        base = os.path.abspath(os.path.expanduser(mkpaths.bus_env_dir() or mkpaths.home()))
        bus = base if mkpaths.bus_env_dir() else os.path.join(base, mkpaths.TARGET_BUS_DIR)
        digest = hashlib.sha256(os.path.realpath(lane_tok).encode("utf-8")).hexdigest()
        old_tok = os.path.join(base, mkpaths.BUS_LANES_DIR, digest + ".json")
        new_tok = os.path.join(bus, mkpaths.TARGET_LANES_DIR, digest + ".json")
        os.makedirs(os.path.dirname(old_tok), exist_ok=True)
        with open(old_tok, "w") as f:
            json.dump({"v": 1, "url": "http://127.0.0.1:9999", "token": "old"}, f)
        try:
            assert not os.path.exists(new_tok)
            assert lane_token_file(lane_tok) == new_tok, (
                "the old-layout token must still be found", lane_token_file(lane_tok), new_tok)
            assert json.load(open(new_tok))["token"] == "old", json.load(open(new_tok))
            assert os.path.isfile(old_tok), "the legacy lane token must survive the take-over"
            with open(new_tok, "w") as f:
                json.dump({"v": 1, "url": "http://127.0.0.1:9998", "token": "new"}, f)
            assert lane_token_file(lane_tok) == new_tok, (lane_token_file(lane_tok), new_tok)
            os.unlink(new_tok)
            # an empty old file carries nothing over, so the resolver answers the old path
            os.truncate(old_tok, 0)
            assert lane_token_file(lane_tok) == old_tok, (lane_token_file(lane_tok), old_tok)
        finally:
            for path in (old_tok, new_tok):
                if os.path.exists(path):
                    os.unlink(path)

        # --- X3.2: todo 114 (newest review ROUND wins whatever its source), todo 130
        # (mark_armed_building never demotes a finished task; dispatch refuses a task/slot
        # mismatch), and the bus-down lane brief copy. Its own data dir, so the DB cases
        # above cannot leak state into these.
        xdata = os.path.join(tmp, "x32data")
        os.makedirs(xdata)

        def mkx(*args):
            r = subprocess.run([sys.executable, MK, *args, "--data", xdata],
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stdout + r.stderr
            return json.loads(r.stdout.strip().splitlines()[-1])

        mkx("run", "init", "--id", "r1", "--feature", "X32", "--branch", "b")
        mkx("store", "enable")

        # (todo 114) the store holds round 1's review; the lane has a NEWER round 2. The
        # round-2 review must win — the bug returned round 1 and reported "already handed off".
        mkx("task", "add", "--root", "--id", "R14", "--title", "round wins")
        mkx("task", "round", "R14", "--kind", "build")
        s_home14 = os.path.join(xdata, ".mk-home")
        _c = mkdb.open_project(s_home14, "matrixkoder", data_dir=xdata)
        try:
            mkdb.put_artifact(s_home14, "matrixkoder", "r1", "R14", 1, "review",
                              "# R14 review — REQUEST CHANGES\n\n## HIGH\n- a:1 — old — stale\n",
                              data_dir=xdata)
        finally:
            _c.close()
        lane14 = os.path.join(tmp, "lane-r14")
        os.makedirs(lane14, exist_ok=True)
        mkx("task", "set", "R14", "lane=" + lane14)
        mkx("task", "set", "R14", "slot=1")
        mkx("task", "round", "R14", "--kind", "fix", "--cause", "verification gap")  # now round 2
        assert task_round_of(xdata, "r1", "R14") == 2, "fixture: R14 must be at round 2"
        open(os.path.join(lane14, ".matrixkoder-review-r2.md"), "w").write(
            "# R14 review — REQUEST CHANGES\n\n## MEDIUM\n- b:2 — new — fresh\n")
        n14, text14, ref14 = review_body_for(xdata, "r1", "R14")
        assert n14 == 2 and "fresh" in text14 and "stale" not in text14, (n14, text14, ref14)
        # the FIX handoff opens off round 2, not round 1, and copies the lane file in
        r = cli("review-handoff", "--task", "R14", "--run", "r1", "--data", xdata)
        assert r["ok"] is True and r["dispatched"] is True and r["round"] == 3, r
        got = open(r["brief"]).read()
        assert "b:2" in got and "a:1" not in got, got
        copied14 = os.path.join(xdata, "runs", "r1", "tasks", "R14", "review-r2.md")
        assert os.path.isfile(copied14), copied14
        # the store's OLDER review must not shadow the newer lane file: the same call again
        # ("already handed off" is the correct answer now that round 3 IS open)
        r = cli("review-handoff", "--task", "R14", "--run", "r1", "--data", xdata)
        assert r["ok"] is True and r["dispatched"] is False and r["reason"] == "already handed off", r
        # the store still wins a tie: a round-2 artifact written after the file is the same
        # round, and the round is what decides
        n14b, _t14b, _r14b = review_body_for(xdata, "r1", "R14")
        assert n14b == 2, n14b

        # (todo 130a) mark_armed_building never demotes a merged/done/qa task. H31 is merged
        # in the store; H32 is building. Both are armed on one lane; a delivered dispatch may
        # only move H32.
        mkx("task", "add", "--root", "--id", "H31", "--title", "finished")
        mkx("task", "add", "--root", "--id", "H32", "--title", "open")
        mkx("task", "set", "H31", "status=merged")
        mkx("task", "set", "H32", "status=building")
        lane31 = os.path.join(tmp, "lane-h31")
        g(repo, "worktree", "add", "-q", "--detach", lane31)
        lanes["H31"] = lane31
        mkx("task", "set", "H31", "pane=" + pane_of("H31"))
        mkx("task", "set", "H32", "pane=" + pane_of("H31"))
        r = cli("arm", "--lane", lane31, "--pane", pane_of("H31"), "--tasks", "H31,H32",
                "--run", "r1", "--data", xdata, "--phase", "P1", "--commit-grep", r"feat\(p1\)")
        assert r["ok"] and "task_pane_error" not in r, r
        res31 = mark_armed_building(pane_of("H31"))
        assert res31 is not None and res31.get("skipped_terminal") == ["H31"], res31
        assert "H32" in res31["building"] and "H31" not in res31["building"], res31
        assert mkx("task", "get", "H31")["task"]["status"] == "merged", "H31 was demoted"
        assert mkx("task", "get", "H32")["task"]["status"] == "building", "H32 not moved"
        # an all-terminal lane is a no-op: nothing in building, everything named skipped
        mkx("task", "set", "H31", "status=done")
        mkx("task", "set", "H32", "status=qa")
        res31b = mark_armed_building(pane_of("H31"))
        assert res31b["building"] == [] and sorted(res31b["skipped_terminal"]) == ["H31", "H32"], res31b
        mkx("task", "set", "H31", "status=merged")
        # F.4: a rework dispatch is the one exception to the merged guard: the merged task moves
        # to building and the reply names it in reopened
        res31r = mark_armed_building(pane_of("H31"), True)
        assert sorted(res31r["reopened"]) == ["H31", "H32"] and "H31" in res31r["building"], res31r  # H32 is qa here
        assert "skipped_terminal" not in res31r, res31r
        assert mkx("task", "get", "H31")["task"]["status"] == "building", "rework did not reopen H31"
        mkx("task", "set", "H31", "status=merged")

        # (todo 130b) dispatch refuses when --task names a task OUTSIDE the pane's armed group,
        # and when the slot's acquired group disagrees. Nothing is typed. The lane above is
        # armed `--tasks H31,H32`, so H32 is a MEMBER of the armed group and a --task H32 is a
        # legitimate group-lane dispatch — the old assertion here that it must fail was the
        # group-lane bug itself (R2).
        # The bus is pinned down (an empty MK_BUS_DIR) so the legacy path decides: no doorbell.
        nobus = os.path.join(tmp, "nobus")
        os.makedirs(nobus, exist_ok=True)
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "idle"}}}\n')
        set_canned("prompt_rc.txt", "0")
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H31"), "--text", "go", "--task", "H99",
                extra={"MK_BUS_DIR": nobus})
        assert r["ok"] is False and r["delivered"] is False and "H99" in r["error"], r
        assert len(prompt_calls()) == n0, "a mismatched dispatch typed a prompt"
        # F.4: --kind rework is accepted and is the merged guard's one exception: the merged task
        # H31 moves to building; an unknown kind is still a usage error
        r = cli("dispatch", "--pane", pane_of("H31"), "--text", "go", "--task", "H31", "--kind", "rework",
                extra={"MK_BUS_DIR": nobus, "MK_DISPATCH_RETRY_S": "0", "MK_DISPATCH_RECHECK_S": "0"})
        assert r["ok"] is True and r["delivered"] is True and r.get("reopened") == ["H31"], r
        assert mkx("task", "get", "H31")["task"]["status"] == "building", "rework dispatch did not reopen H31"
        mkx("task", "set", "H31", "status=merged")
        r = cli("dispatch", "--pane", pane_of("H31"), "--text", "go", "--kind", "nap", code=2)
        assert r["ok"] is False and "rework" in r["error"], r
        # the slot registry disagrees: slot 1 belongs to a DIFFERENT task than the armed one
        mkx("task", "set", "H31", "slot=1")
        _c = mkdb.open_project(s_home14, "matrixkoder", data_dir=xdata)
        try:
            _c.execute("DELETE FROM lane_slots")
            _c.execute("INSERT INTO lane_slots(slot, lane_path, state, run, task)"
                       " VALUES ('1', ?, 'busy', 'r1', 'OTHER')", (lane31,))
            _c.commit()
        finally:
            _c.close()
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H31"), "--text", "go", extra={"MK_BUS_DIR": nobus})
        assert r["ok"] is False and r["delivered"] is False and "OTHER" in r["error"], r
        assert len(prompt_calls()) == n0, "a slot-mismatched dispatch typed a prompt"
        # --force is the documented escape hatch: the dispatch goes through
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H31"), "--text", "go", "--force",
                extra={"MK_BUS_DIR": nobus})
        assert r["ok"] is True and r["delivered"] is True, r
        assert len(prompt_calls()) == n0 + 1, prompt_calls()[n0:]
        # a free slot is not a mismatch: the registry has no task to disagree with
        _c = mkdb.open_project(s_home14, "matrixkoder", data_dir=xdata)
        try:
            _c.execute("UPDATE lane_slots SET state='free', task=NULL")
            _c.commit()
        finally:
            _c.close()
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H31"), "--text", "go", extra={"MK_BUS_DIR": nobus})
        assert r["ok"] is True and r["delivered"] is True, r
        assert len(prompt_calls()) == n0 + 1, prompt_calls()[n0:]

        # (R2 a/b/c) the R2 fixes: a refused dispatch publishes NOTHING and writes nothing, a
        # group lane dispatches with or without --task and publishes the DISPATCHED task's
        # brief, and a merged lead is never demoted by a delivered dispatch. Distinct briefs on
        # H31 (lead, merged) and H32 (member) let the published text name the task.
        _c = mkdb.open_project(s_home14, "matrixkoder", data_dir=xdata)
        try:
            for _tid in ("H31", "H32"):
                _c.execute("DELETE FROM artifacts WHERE run='r1' AND task=? AND kind='brief'", (_tid,))
            _c.commit()
        finally:
            _c.close()
        mkdb.put_artifact(s_home14, "matrixkoder", "r1", "H31", 1, "brief",
                          "# H31 brief\nLEADTEXT\n", data_dir=xdata)
        mkdb.put_artifact(s_home14, "matrixkoder", "r1", "H32", 1, "brief",
                          "# H32 brief\nSECONDTEXT\n", data_dir=xdata)
        lane_brief31 = os.path.join(lane31, ".matrixkoder-brief.md")
        # the 130a case above leaves H31=merged and H32=qa; the merged-led lane needs H32 open
        # (building) so a delivered dispatch has a non-terminal member to arm.
        mkx("task", "set", "H32", "status=building")
        # (a) bus DOWN: the mismatched dispatch publishes nothing, writes no lane brief, types no
        # prompt — the guards run before write_lane_brief (the live wrong-brief bug).
        for p_ in (lane_brief31,):
            if os.path.exists(p_):
                os.unlink(p_)
        n0 = len(prompt_calls())
        r = cli("dispatch", "--pane", pane_of("H31"), "--text", "go", "--task", "H99",
                extra={"MK_BUS_DIR": nobus})
        assert r["ok"] is False and "H99" in r["error"], r
        assert not os.path.exists(lane_brief31), "a refused dispatch wrote the lane brief"
        assert len(prompt_calls()) == n0, prompt_calls()[n0:]
        # (a) bus UP: a pinned $MK_BUS_DIR with a real /health and the fake mk-bus that records
        # every argv; the refused dispatch sends nothing to the bus.
        from http.server import ThreadingHTTPServer as _THS
        class _HB(BaseHTTPRequestHandler):
            def do_GET(self):
                b = b'{"ok": true, "status": "healthy"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            def log_message(self, *a):
                pass
        buspin = os.path.join(tmp, "buspin")
        os.makedirs(buspin, exist_ok=True)
        # the doorbell waits for the pane to read idle before it types, so the fake list must
        # name this pane idle (the canned list from earlier cases names other panes)
        set_canned("pane_list_out.txt", json.dumps({"result": {"panes": [
            {"pane_id": pane_of("H31"), "agent_status": "idle"}]}}) + "\n")
        _bsrv = _THS(("127.0.0.1", 0), _HB)
        threading.Thread(target=_bsrv.serve_forever, daemon=True).start()
        open(os.path.join(buspin, "bus.json"), "w").write(
            json.dumps({"port": _bsrv.server_address[1]}))
        try:
            n0, b0 = len(prompt_calls()), len(bus_calls())
            r = cli("dispatch", "--pane", pane_of("H31"), "--text", "go", "--task", "H99",
                    extra={"MK_BUS_DIR": buspin})
            assert r["ok"] is False and "H99" in r["error"], r
            assert bus_calls()[b0:] == [], bus_calls()[b0:]
            assert not os.path.exists(lane_brief31), "a refused dispatch wrote the lane brief"
            assert len(prompt_calls()) == n0, prompt_calls()[n0:]
            # (b) a GROUP lane: no --task publishes the LEAD's brief; --task H32 publishes H32's
            n0, b0 = len(prompt_calls()), len(bus_calls())
            r = cli("dispatch", "--pane", pane_of("H31"), "--text", "go", extra={"MK_BUS_DIR": buspin})
            assert r["ok"] is True and r["delivered"] is True, r
            assert "LEADTEXT" in " ".join(bus_calls()[b0:]) and "SECONDTEXT" not in " ".join(bus_calls()[b0:]), \
                bus_calls()[b0:]
            assert len(prompt_calls()) == n0 + 1, prompt_calls()[n0:]
            n0, b0 = len(prompt_calls()), len(bus_calls())
            r = cli("dispatch", "--pane", pane_of("H31"), "--text", "go", "--task", "H32",
                    extra={"MK_BUS_DIR": buspin})
            assert r["ok"] is True and r["delivered"] is True, r
            assert "SECONDTEXT" in " ".join(bus_calls()[b0:]) and "LEADTEXT" not in " ".join(bus_calls()[b0:]), \
                bus_calls()[b0:]
            assert len(prompt_calls()) == n0 + 1, prompt_calls()[n0:]
            # (c) the merged LEAD is never demoted by those delivered dispatches
            assert mkx("task", "get", "H31")["task"]["status"] == "merged", "the merged lead was demoted"
            assert mkx("task", "get", "H32")["task"]["status"] == "building", "the member was not building"
        finally:
            _bsrv.shutdown()
            _bsrv.server_close()
        # (b, bus-down) the same group lane falls back to the DISPATCHED task's brief file
        if os.path.exists(lane_brief31):
            os.unlink(lane_brief31)
        r = cli("dispatch", "--pane", pane_of("H31"), "--text", "go", "--task", "H32",
                extra={"MK_BUS_DIR": nobus})
        assert r["ok"] is True and r.get("lane_brief"), r
        assert "SECONDTEXT" in open(lane_brief31).read(), open(lane_brief31).read()
        if os.path.exists(lane_brief31):
            os.unlink(lane_brief31)

        # (A2/X3.2) the bus-down fallback copies the brief into the lane. The bus is pinned
        # down; the store holds a brief artifact for the lane's lead task (dispatch publishes
        # for the watch's lead), so the legacy path writes <lane>/.matrixkoder-brief.md.
        _c = mkdb.open_project(s_home14, "matrixkoder", data_dir=xdata)
        try:
            mkdb.put_artifact(s_home14, "matrixkoder", "r1", "H31", 1, "brief",
                              "# H31 brief\nfrom the store, into the lane\n", data_dir=xdata)
        finally:
            _c.close()
        assert not os.path.exists(os.path.join(lane31, ".matrixkoder-brief.md"))
        r = cli("dispatch", "--pane", pane_of("H31"), "--text", "the full prompt stays",
                extra={"MK_BUS_DIR": nobus})
        assert r["ok"] is True and r["connect"] is False, r
        assert r.get("lane_brief") and os.path.realpath(r["lane_brief"]) == \
            os.path.realpath(os.path.join(lane31, ".matrixkoder-brief.md")), r
        assert "into the lane" in open(os.path.join(lane31, ".matrixkoder-brief.md")).read()
        # the copy falls back to the task folder's brief when the store holds none (C4's
        # fallback, kept): delete the artifact and give the lead task a brief-rN.md file
        os.unlink(os.path.join(lane31, ".matrixkoder-brief.md"))
        fb_dir = os.path.join(xdata, "runs", "r1", "tasks", "H31")
        os.makedirs(fb_dir, exist_ok=True)
        open(os.path.join(fb_dir, "brief-r1.md"), "w").write("# file brief\nfallback into lane\n")
        _c = mkdb.open_project(s_home14, "matrixkoder", data_dir=xdata)
        try:
            # a briefed task must own files; a started task drops `owned` through task set,
            # so plant it the way mk-plan approve writes it — through mkdb, the one writer
            _c.execute("UPDATE tasks SET owned=? WHERE run='r1' AND id='H31'", ("scripts/h31.py",))
            _c.execute("DELETE FROM artifacts WHERE run='r1' AND task='H31' AND kind='brief'")
            _c.commit()
        finally:
            _c.close()
        r = cli("dispatch", "--pane", pane_of("H31"), "--text", "the full prompt stays",
                extra={"MK_BUS_DIR": nobus})
        assert r["ok"] is True and r.get("lane_brief"), r
        assert "fallback into lane" in open(os.path.join(lane31, ".matrixkoder-brief.md")).read()
        os.unlink(os.path.join(lane31, ".matrixkoder-brief.md"))

        srv.shutdown()
        srv.server_close()
        # R1.2: the mkdb write guard refuses the real home from this suite too.
        mkdb.selftest_guard_probe(os.environ["MK_HOME"])
    finally:
        restore_data()
        shutil.rmtree(tmp, ignore_errors=True)
    print("mk-lane-event self-test ok")


def main():
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    cmd, rest = (a[0], a[1:]) if a else ("", [])
    try:
        if cmd == "arm":
            return cmd_arm(rest)
        if cmd == "event":
            return cmd_event()
        if cmd == "dispatch":
            return cmd_dispatch(rest)
        if cmd == "sweep":
            return cmd_sweep(rest)
        if cmd == "disarm":
            return cmd_disarm(rest)
        if cmd == "review-handoff":
            return cmd_review_handoff(rest)
    except SystemExit:
        raise
    except Exception as e:  # never die silently: one JSON line with the failure
        out({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
        sys.exit(2)
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
