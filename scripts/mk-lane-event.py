#!/usr/bin/env python3
"""herdr pushes lane events; this script turns them into MatrixKoder verdicts (event-driven lanes).

Usage:
  mk-lane-event.py arm --lane DIR --pane ID --task T --run R --data DIR --phase P --commit-grep RX
                      [--report PATH] [--kind claude|codex|opencode|agy] [--prompt-marker TEXT] [--stall-min N]
  mk-lane-event.py arm --lane DIR --pane ID --tasks A,B --run R --data DIR --phase P --commit-grep RX
                      [...]   # a shared lane group: one lane serving several phases of one plan
  mk-lane-event.py event                (herdr plugin hook: reads HERDR_PLUGIN_EVENT + HERDR_PLUGIN_EVENT_JSON)
  mk-lane-event.py dispatch --pane ID --text TEXT [--timeout-ms 25000] [--role build|review]
                             [--kind build|fix|takeover] [--connect|--no-connect]
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
dispatch --kind fix|takeover (a fix/takeover round; default build) with --role build on session.fresh_session: new
(<data>/matrixkoder.yaml, else the schema default) first sends the pane's agent its new-session command — /new for
an OpenCode lane, /clear for a Claude lane, from the armed watch's kind — then waits (bounded) for the pane to go
idle and sends the prompt exactly as today. No armed watch kind, a kind without a known command, --kind build,
--role review, or fresh_session: same: one prompt, no extra herdr call. The reply carries fresh_session (the
command sent, or null).
"""
import fcntl, shlex, hashlib, importlib.util, json, os, re, shutil, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
WATCH = ".matrixkoder-watch.json"
LOCK = ".matrixkoder-watch.lock"
DISPATCH_RETRY_S = 5  # wait before dispatch's one automatic retry; $MK_DISPATCH_RETRY_S overrides
DISPATCH_RECHECK_S = 3  # wait before dispatch's one pane-text recheck; $MK_DISPATCH_RECHECK_S overrides
RETRY_PANE_STATES = ("idle", "done")  # pane sat still through a failed wait → the prompt was lost
FRESH_IDLE_WAIT_S = float(os.environ.get("MK_FRESH_IDLE_WAIT_S", "30"))  # bounded; $MK_FRESH_IDLE_WAIT_S shrinks it for a self-test
FRESH_IDLE_POLL_S = float(os.environ.get("MK_FRESH_IDLE_POLL_S", "0.5"))
# the doorbell wait is its own bound: a /new just before must finish before the line is typed
DOORBELL_IDLE_WAIT_S = float(os.environ.get("MK_DOORBELL_IDLE_WAIT_S", "15"))
DOORBELL_IDLE_POLL_S = float(os.environ.get("MK_DOORBELL_IDLE_POLL_S", "0.5"))
FRESH_CMDS = {"opencode": "/new", "claude": "/clear"}

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
    $MATRIXKODER_HOME are the same setting; when both are set they must agree."""
    mkh = os.environ.get("MK_HOME")
    mch = os.environ.get("MATRIXKODER_HOME")
    if mkh and mch:
        left = os.path.realpath(os.path.abspath(os.path.expanduser(mkh)))
        right = os.path.realpath(os.path.abspath(os.path.expanduser(mch)))
        if left != right:
            raise RuntimeError(f"MK_HOME ({mkh}) and MATRIXKODER_HOME ({mch}) differ; set one MK home")
    return os.path.expanduser(mkh or mch or "~/.matrixkoder")


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
        with open(os.path.join(mk_home(), "projects.json")) as f:
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
    """The new-session command for an agent kind: /new for OpenCode, /clear for Claude. None for a
    kind with no known command (or no kind at all): that round keeps today's single-prompt
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
        lines = mk_config.read_lines(os.path.join(data, "matrixkoder.yaml"))
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
    so a bus that has not moved the token keeps working."""
    override = os.environ.get("MK_BUS_TOKEN_FILE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    inside = os.path.join(lane, ".matrixkoder-bus.json")
    if os.path.isfile(inside):
        return inside
    home = os.environ.get("MK_BUS_DIR") or os.environ.get("MK_HOME") or os.environ.get("MATRIXKODER_HOME") or os.path.expanduser("~/.matrixkoder")
    digest = hashlib.sha256(os.path.realpath(lane).encode("utf-8")).hexdigest()
    return os.path.join(os.path.abspath(os.path.expanduser(home)), "bus-lanes", digest + ".json")


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
    """Stored ids of <task>'s children in <run>, [] when it has none or run state is unreadable."""
    try:
        with open(os.path.join(data, "runs", run, "run.json")) as f:
            tasks = json.load(f).get("tasks") or []
    except (OSError, ValueError):
        return []
    return [t.get("id") for t in tasks if isinstance(t, dict)
            and (t.get("parent") or "").lower() == str(task).lower()]


def mark_armed_building(pane):
    """dispatch delivered:true → the lane's tasks move again: the children of EVERY armed task
    (or the task itself when it has none) are set building via mk-state, so the board shows the
    lane working before the agent's first event lands — in a shared lane group one delivered
    prompt arms every listed phase (tonight's two-phase lane only moved the first phase's).
    No armed watch file → None, nothing else in the JSON. mk-state failures are returned, never
    swallowed and never fatal: the delivery already happened (a parent is never set directly —
    its status rolls up, so its children are set instead)."""
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
    common = ["--data", data, "--by", "dispatch"] + (["--run", run] if run else [])
    done, errors = [], []
    for t in targets:
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
    """Where bus.json lives, the same rule as mk-bus.bus_location: $MK_BUS_DIR, else the
    MK home when <data>/mk.db exists (store mode), else the data dir (file mode)."""
    override = os.environ.get("MK_BUS_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if data and os.path.isfile(os.path.join(data, "mk.db")):
        # One MK home rule (C16): mk_home() takes $MK_HOME or $MATRIXKODER_HOME.
        home = mk_home()
        return os.path.abspath(os.path.expanduser(home))
    return data


def brief_message(round_n, text):
    """The one brief format both publish_brief and review-handoff send, and the one
    connect reads: `round:<n>\nkind:brief\n<body>`."""
    return f"round:{round_n}\nkind:brief\n{text}"


def bus_is_up(data):
    """True only when a bus is already serving this data dir. Reads bus.json from the
    mk-bus location and probes /health; never calls ensure, so a down bus stays down."""
    if not data:
        return False
    bus_dir = bus_dir_for(data)
    try:
        with open(os.path.join(bus_dir, "bus.json")) as f:
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
    """The brief the orchestrator would hand the lane: the newest brief-rN.md for the task."""
    d = os.path.join(data, "runs", run, "tasks", task)
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


def publish_brief(data, run, task):
    """Send the task's current brief to the lane as the orchestrator. File mode uses kind
    info (the only kinds a file-mode bus accepts); store mode uses kind task. -> (ok, error)."""
    text, n = brief_body_for(data, run, task)
    if not text or not str(text).strip():
        return False, "no brief"
    body = brief_message(n, text)
    kind = "task" if os.path.isfile(os.path.join(data, "mk.db")) else "info"
    _j, err = run_bus(["send", "--data", data, "--run", run, "--task", task,
                       "--kind", kind, "--from", "orch", "--to", task, "--text", body])
    return err is None, err


def publish_review_brief(data, run, task):
    """Send the task's newest review-brief-rN.md to the lane as `round:<n>\nkind:review-brief\n<body>`
    (its own kind: the builder's connect never picks it up). Same send rules as publish_brief."""
    d = os.path.join(data, "runs", run, "tasks", task)
    best, best_n = None, -1
    for name in (os.listdir(d) if os.path.isdir(d) else []):
        m = re.fullmatch(r"review-brief-r(\d+)\.md", name)
        if m and int(m.group(1)) > best_n:
            best, best_n = os.path.join(d, name), int(m.group(1))
    if not best:
        return False, "no review brief"
    with open(best) as f:
        body = f"round:{best_n}\nkind:review-brief\n{f.read()}"
    kind = "task" if os.path.isfile(os.path.join(data, "mk.db")) else "info"
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


def connect_text(lane, data):
    """The one line a bus-up dispatch types: connect, plus the job-budget command.

    `--data <dir>` is in the line so the lane's first command works with no env (E1 reads
    it). The hint is a command prefix, so typing it is how the builder's shell gets the var.
    One line: the fake herdr and the prompt marker both keep only the first line.
    """
    skill = os.path.join(lane, ".claude", "skills", "mk-builder")
    n = selftest_jobs_hint(data) if data else "4"
    # MK_SELFTEST_JOBS=N stays in the production doorbell: it is the E6 job-budget hint
    # and E1 reads it from the typed line.
    data_arg = f" --data {shlex.quote(data)}" if data else ""
    return (f"MK_SELFTEST_JOBS={n} python3 {shlex.quote(skill)}/scripts/mk-builder.py connect{data_arg}")


def delivered_status(pane, role):
    """The board write a DELIVERED dispatch makes, by role. build (the default) →
    mark_armed_building: every armed task's children (or the task itself) go building. review →
    no write at all — the task under review stays at review, and status_unchanged:true in the
    reply says the verdict knowingly moved nothing."""
    if role == "review":
        return {"status_unchanged": True}
    return mark_armed_building(pane) or {}


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
    fix|takeover build dispatch on session.fresh_session: new first sends the pane's agent its
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
    connected, legacy_note = False, None
    if opt(a, "--role", "build") == "build":
        hit = find_armed(pane)
        wdata = (hit[1].get("data") or "") if hit else ""
        wrun, wtask = (hit[1].get("run"), hit[1].get("task")) if hit else (None, None)
        why = None
        if not hit:
            pass
        elif "--no-connect" in a:
            why = "--no-connect given"
        elif not bus_enabled(wdata):
            why = "bus.enabled is false"
        elif not bus_is_up(wdata):
            why = "bus is down"
        else:
            ok, publish_error = publish_brief(wdata, wrun, wtask)
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
                    alert_publish_failure(wdata, wrun, wtask, publish_error)
        if why:
            legacy_note = f"legacy prompt: {why}"
    elif opt(a, "--lane") and "--no-connect" not in a:
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
                text = connect_text(rlane, rdata) + " --role review"
                connected = True
            else:
                why = f"publish failed: {publish_error}"
                alert_publish_failure(rdata, rrun, rtask, publish_error or "no review brief")
        if why:
            legacy_note = f"legacy prompt: {why}"

    def reply(d, note=None):
        n = "; ".join(x for x in (legacy_note, note) if x)
        out({**d, "connect": connected, **({"note": n} if n else {})})

    role = opt(a, "--role", "build")
    if role not in ("build", "review"):
        fail("--role must be build or review")
    round_kind = opt(a, "--kind", "build")
    if round_kind not in ("build", "fix", "takeover"):
        fail("--kind must be build, fix or takeover")
    try:
        ms = int(opt(a, "--timeout-ms", "25000"))
    except ValueError as e:
        fail(f"--timeout-ms: {e}")
    # a fix/takeover round on fresh_session: new resets the agent's session first: the command,
    # then a bounded idle wait, then the prompt exactly as today. Everything else (build, review,
    # fresh_session: same, no armed kind, unknown kind) skips this and sends one prompt as before.
    fresh = (fresh_session_cmd(armed_kind(pane))
             if role == "build" and round_kind in ("fix", "takeover")
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
    attempts, fail_why = 1, None
    while True:
        kind, why, status, j = _prompt_attempt(pane, text, ms)
        if kind == "ok":
            reply({"ok": True, "delivered": True, "pane": pane, "agent_status": status, "timeout_ms": ms,
                 "attempts": attempts, "herdr_error": fail_why or why, "pane_entry": entry,
                 "fresh_session": fresh,
                 **({"publish_error": opt(a, "--publish-error")} if opt(a, "--publish-error") else {}),
                 **({"pane_list_error": pl_err} if pl_err else {}), **delivered_status(pane, role)})
            return
        fail_why = why
        if kind == "fail":  # herdr failed some other way: its own words, no delivery, no pane check
            reply({"ok": False, "delivered": False, "pane": pane, "timeout_ms": ms, "attempts": attempts,
                 "herdr_error": why, "error": why, "pane_entry": entry, "fresh_session": fresh})
            return
        # exc / wait: a timeout proves nothing either way — the pane's own state decides delivery
        st, entry, pl_err = pane_state(pane)
        if st == "working":  # the wait gave up, the agent did not: the prompt is still being delivered
            reply({"ok": True, "delivered": True, "pane_state": "working", "pane": pane, "timed_out": True,
                 "timeout_ms": ms, "attempts": attempts, "herdr_error": why,
                 "pane_entry": entry, "fresh_session": fresh, **delivered_status(pane, role)},
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
                     **({"pane_list_error": pl_err} if pl_err else {}), **delivered_status(pane, role)},
                      note="herdr wait timed out; the prompt had already landed")
                return
        reply({"ok": False, "delivered": False, "pane_state": st, "pane": pane, "timed_out": True,
             "timeout_ms": ms, "attempts": attempts, "herdr_error": why, "error": why,
             "pane_entry": entry, "fresh_session": fresh, **({"pane_list_error": pl_err} if pl_err else {})})
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
    d = os.path.join(data, "runs", run, "tasks", task)
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


def send_brief_over_bus(data, run, task, text, round_n):
    """The orchestrator sends the FIX brief to the builder. Store mode stores it as a
    brief artifact (connect reads that); file mode sends it as info. Never a lane-to-lane
    message: the sender is the orchestrator token."""
    body = brief_message(round_n, text)
    if os.path.isfile(os.path.join(data, "mk.db")):
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
    return os.path.join(data, "runs", run, "tasks", task, ".matrixkoder-handoff.json")


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


def filled_brief(data, run, task, round_n, review_path, fix):
    """A fix brief filled from the task, never the raw template.

    Repo, worktree, branch and sha come from the task's lane (git). Owned files come from
    the task. The commit subject, the report path and the test/rules lines are copied from
    the previous round's brief when it has them. The FIX section cites the review file and
    lists each finding.
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
    lines = [
        f"# {task} — FIX round {round_n}: review R{review_round_of(review_path) or ''}",
        "",
        f"Repo: this folder (worktree `{lane or '(no lane recorded)'}`, branch `{branch or '(none)'}`, at `{sha or '(none)'}`).",
        f"Plan phase task `{task}`. History: `{data}/runs/{run}/tasks/{task}/` (read-only). Do ONLY these fixes.",
        "",
        "## Files you own",
        owned_txt or "(none recorded on the task)",
        "Do NOT touch: everything else.",
        "",
        f"## FIX (review `{review_path}`)",
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
    path = opt(a, "--review") or review_path_for(data, run, task)
    if not path or not os.path.isfile(path):
        # the reviewer writes into its own lane; copy that back into the task folder
        found = lane_review_for(data, run, task)
        if found:
            # the lane file is copied into the task folder under the plain name, and the
            # brief cites the copy so it survives a lane reset
            dest_dir = os.path.join(data, "runs", run, "tasks", task)
            os.makedirs(dest_dir, exist_ok=True)
            n_found = review_round_of(found)
            path = os.path.join(dest_dir, f"review-r{n_found}.md")
            if not os.path.isfile(path):
                shutil.copyfile(found, path)
        else:
            lane = (task_record(data, run, task).get("lane") or "<lane>")
            fail(f"no review file for {task}: looked in "
                 f"{os.path.join(data, 'runs', run, 'tasks', task)} and "
                 f"{lane}/.matrixkoder-review-rN.md")
    n = review_round_of(path)
    if n is None:
        fail("review file is not review-rN.md")
    try:
        text = open(path).read()
    except OSError as e:
        fail(f"review unreadable: {e}")
    verdict = review_verdict(text)
    if verdict == "APPROVE":
        r = subprocess.run(
            [sys.executable, mk_state_cmd(), "--data", data, "--run", run,
             "task", "set", task, "verdict=approve", "--by", "review-handoff"],
            capture_output=True, text=True)
        out({"ok": True, "verdict": "APPROVE", "dispatched": False, "task": task,
             "recorded": r.returncode == 0})
        return
    if verdict != "REQUEST CHANGES":
        fail("review title has neither APPROVE nor REQUEST CHANGES")
    cur = task_round_of(data, run, task)
    if cur != n:
        out({"ok": True, "dispatched": False, "task": task, "round": cur,
             "reason": "already handed off" if cur > n else "review round is not the current round"})
        return
    findings = review_findings(text)
    if not findings:
        out({"ok": True, "dispatched": False, "task": task, "round": cur, "findings": 0,
             "reason": "no findings; held for the orchestrator"})
        return
    pending = hold_path(data, run, task)
    if hold:
        # Record the pending handoff and nothing else: no round, no brief, no send.
        blob = {"task": task, "run": run, "review": path, "round": n,
                "min": min_sev, "cause": opt(a, "--cause", "verification gap")}
        try:
            os.makedirs(os.path.dirname(pending), exist_ok=True)
            with open(pending, "w") as f:
                json.dump(blob, f)
        except OSError as e:
            out({"ok": False, "error": f"hold write failed: {e}", "task": task})
            return
        out({"ok": True, "verdict": "REQUEST CHANGES", "dispatched": False, "held": True,
             "task": task, "round": n, "findings": len(findings),
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
    fix = render_fix_section(findings, min_sev)
    brief_path = opened.get("brief")
    written = filled_brief(data, run, task, opened.get("round"), path, fix)
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
    out({"ok": True, "verdict": "REQUEST CHANGES", "dispatched": True, "task": task,
         "round": opened.get("round"), "kind": "fix", "brief": brief_path,
         "findings": len(findings), "required": sum(
             1 for f in findings if SEV_RANK[f["severity"]] <= SEV_RANK[min_sev]),
         "children": fixed, "sent": bool(sent and not send_err),
         "held": False, "wake": bool(wake and not wake_err),
         **({"send_error": send_err} if send_err else {}),
         **({"children_error": "; ".join(fix_err)} if fix_err else {}),
         **({"wake_error": wake_err} if wake_err else {})})


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
                 "recorded": True, "label": label, "toast": toast})


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
            mks("task", "add", "--id", tid, "--title", tid)
            mks("task", "set", tid, "status=building")

        def task_status(tid):
            run_ = json.load(open(os.path.join(data, "runs", "r1", "run.json")))
            return next(t["status"] for t in run_["tasks"] if t["id"].lower() == tid.lower())

        def events():
            with open(os.path.join(data, "runs", "r1", "events.jsonl")) as f:
                return [json.loads(x) for x in f if x.strip()]

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
                    "    dest = os.path.join(home, 'bus-lanes', digest + '.json')\n"
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
            run_ = json.load(open(os.path.join(data, "runs", "r1", "run.json")))
            return next(t for t in run_["tasks"] if t["id"].lower() == tid.lower())[key]

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
        mks("task", "add", "--id", "H8", "--title", "H8", "--run", "r1")
        mks("task", "add", "--id", "H8C1", "--title", "c1", "--parent", "H8", "--run", "r1")
        mks("task", "add", "--id", "H8C2", "--title", "c2", "--parent", "H8", "--run", "r1")
        arm("H8")

        def ms_calls():
            return open(ms_log).read().splitlines() if os.path.exists(ms_log) else []

        # (f) delivered:true, armed task has children → exactly the children set building
        set_canned("prompt_out.txt", '{"result": {"agent": {"agent_status": "idle"}}}\n')
        set_canned("prompt_err.txt", "")
        set_canned("prompt_rc.txt", "0")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go", extra={"MK_STATE_CMD": fake_ms})
        assert r["ok"] is True and r["delivered"] is True and r["building"] == ["H8C1", "H8C2"], r
        assert "task_status_error" not in r, r
        assert ms_calls() == [
            f"task set H8C1 status=building --data {data} --by dispatch --run r1",
            f"task set H8C2 status=building --data {data} --by dispatch --run r1"], ms_calls()
        # (g) delivered:true, armed task has NO children → the task itself
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H1"), "--text", "go", extra={"MK_STATE_CMD": fake_ms})
        assert r["ok"] is True and r["delivered"] is True and r["building"] == ["H1"], r
        assert ms_calls() == [f"task set H1 status=building --data {data} --by dispatch --run r1"], ms_calls()
        # (h) delivered:false → no mk-state call at all
        set_canned("prompt_out.txt", "")
        set_canned("prompt_err.txt", "herdr: no such pane wZ:gone")
        set_canned("prompt_rc.txt", "1")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go", extra={"MK_STATE_CMD": fake_ms})
        assert r["ok"] is False and r["delivered"] is False and "building" not in r, r
        assert ms_calls() == [], ms_calls()
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
        os.unlink(os.path.join(tmp, "ms_err.txt"))
        open(os.path.join(tmp, "ms_badjson.txt"), "w").write("x")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H8"), "--text", "go", extra={"MK_STATE_CMD": fake_ms})
        assert r["ok"] is True and r["delivered"] is True and r["building"] == [], r
        assert "not json" in r["task_status_error"], r
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
        assert ms_calls() == [f"task set H8C1 status=building --data {data} --by dispatch --run r1",
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
        mks("task", "add", "--id", "H_R", "--title", "rev", "--run", "r1")
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
        assert ms_calls() == [f"task set H_R status=building --data {data} --by dispatch --run r1"], ms_calls()
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
        assert ms_calls() == [f"task set H8C1 status=building --data {data} --by dispatch --run r1",
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
        assert ms_calls() == [], (r, ms_calls())
        # the marker does NOT appear in old pane text — the new portion (fake appends) carries
        # the marker and it is genuinely new since this dispatch started → recover
        os.unlink(os.path.join(tmp, "no_pane.txt"))
        open(pane_f, "w").write("older unrelated work\nold history line\n")
        open(ms_log, "w").write("")
        r = cli("dispatch", "--pane", pane_of("H8"),
                "--text", "Read and follow /this/lane/brief.md\nread the brief and continue",
                extra=fast)
        assert r["ok"] is True and r["delivered"] is True and r["recovered"] is True, r
        assert r["attempts"] == 2 and ms_calls() == [
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
        mks("task", "add", "--id", "H9", "--title", "g1", "--run", "r1")
        mks("task", "add", "--id", "H9C1", "--title", "c1", "--parent", "H9", "--run", "r1")
        mks("task", "add", "--id", "H10", "--title", "g2", "--run", "r1")
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
        assert ms_calls() == [f"task set H9C1 status=building --data {data} --by dispatch --run r1",
                              f"task set H10 status=building --data {data} --by dispatch --run r1"], ms_calls()
        # a verdict on a group lane records for every listed task: H9's children and childless H10
        g(lanes["H9"], "commit", "-q", "--allow-empty", "-m", "feat(p1): group core")
        open(pane_f, "w").write("> dispatch-h9\nall gates green\n⏺ P1 DONE\n")
        open(status_f, "w").write("done")
        r = event("done", tid="H9")
        assert r["verdict"] == "DONE" and r["recorded"], r
        st_ = json.load(open(os.path.join(data, "runs", "r1", "run.json")))["tasks"]
        got = {t["id"]: t["status"] for t in st_}
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
        mks("task", "add", "--id", "HB", "--title", "bus lane", "--run", "r1")
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
        mks("task", "add", "--id", "HG1", "--title", "g1", "--run", "r1")
        mks("task", "add", "--id", "HG2", "--title", "g2", "--run", "r1")
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
        mks_in(databus, "task", "add", "--id", "HO", "--title", "off", "--run", "r1")
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
        mks("task", "add", "--id", "HB2", "--title", "b2", "--run", "r1")
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
        # self-hosting guard: every case above ran with no store and created none
        assert not os.path.exists(os.path.join(data, "mk.db")), "an event created mk.db"
        mks("store", "enable")
        assert os.path.getsize(os.path.join(data, "mk.db")) > 0
        # store-first: no lane status file, the tasks row + report artifact decide -> DONE, source=store
        mks("task", "add", "--id", "S1", "--title", "store")
        mks("task", "set", "S1", "status=building")
        lanes["S1"] = os.path.join(tmp, "lane-s1")
        g(repo, "worktree", "add", "-q", "--detach", lanes["S1"])
        g(lanes["S1"], "commit", "-q", "--allow-empty", "-m", "feat(p1): store core")
        td = os.path.join(data, "runs", "r1", "tasks", "S1")
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
        mks("task", "add", "--id", "S2", "--title", "store2")
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
        mks("task", "add", "--id", "S3", "--title", "fallback")
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
        mks("task", "add", "--id", "S4", "--title", "sweep")
        mks("task", "set", "S4", "status=building")
        lanes["S4"] = os.path.join(tmp, "lane-s4")
        g(repo, "worktree", "add", "-q", "--detach", lanes["S4"])
        g(lanes["S4"], "commit", "-q", "--allow-empty", "-m", "feat(p1): sweep core")
        td = os.path.join(data, "runs", "r1", "tasks", "S4")
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
            mks("task", "add", "--id", "NB", "--title", "no brief")
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

        # (D7B R3) store mode: mk.db present, so bus.json lives in the MK home, not the data dir.
        # A stale <data>/bus.json must not win. A real store-mode bus, real acquire and arm.
        open(os.path.join(data, "mk.db"), "w").write("")
        _home = os.path.join(tmp, "mkhome")
        os.makedirs(_home, exist_ok=True)
        open(os.path.join(data, "bus.json"), "w").write(json.dumps({"port": 1}))
        # no MK_BUS_DIR: a present mk.db resolves to the MK home, never the stale data-dir file
        _saved_home = os.environ.get("MK_HOME")
        _saved_mhome = os.environ.get("MATRIXKODER_HOME")
        _saved_bdir = os.environ.get("MK_BUS_DIR")
        os.environ["MK_HOME"] = _home
        os.environ["MATRIXKODER_HOME"] = _home
        os.environ.pop("MK_BUS_DIR", None)
        try:
            assert bus_dir_for(data) == os.path.abspath(_home), bus_dir_for(data)
            assert bus_is_up(data) is False
            open(os.path.join(_home, "bus.json"), "w").write(
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
        os.unlink(os.path.join(data, "mk.db"))
        os.unlink(os.path.join(data, "bus.json"))

        # (D7B.2) review-handoff. The bus is the fake, so "sent" is the recorded argv.
        rev_dir = os.path.join(data, "runs", "r1", "tasks", "RH")
        os.makedirs(rev_dir, exist_ok=True)
        mks("task", "add", "--id", "RH", "--title", "handoff")
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
        mks("task", "add", "--id", "RN", "--title", "no previous brief")
        no_prev = filled_brief(data, "r1", "RN", 2, os.path.join(rev_dir, "review-r1.md"), "1. [HIGH] a:1 — p — f")
        assert _bc.placeholder_failures(no_prev) == [], _bc.placeholder_failures(no_prev)
        assert "Co-Authored-By: <" not in no_prev, no_prev
        calls = bus_calls()
        joined = " ".join(calls)
        assert joined.startswith("send ") and "--from orch" in joined and "--kind info" in joined \
            and "kind:brief" in joined, calls
        # APPROVE records the verdict and dispatches nothing
        mks("task", "add", "--id", "RA", "--title", "approve")
        mks("task", "round", "RA", "--kind", "build")
        ad = os.path.join(data, "runs", "r1", "tasks", "RA")
        open(os.path.join(ad, "review-r1.md"), "w").write("# RA review — APPROVE\n\n## Verified\n- ok\n")
        open(bus_log, "w").write("")
        r = cli("review-handoff", "--task", "RA", "--run", "r1", "--data", data)
        assert r["ok"] is True and r["verdict"] == "APPROVE" and r["dispatched"] is False, r
        assert bus_calls() == [], bus_calls()
        # --hold records the veto text and does not send the wake
        mks("task", "add", "--id", "HH", "--title", "hold")
        mks("task", "round", "HH", "--kind", "build")
        hd = os.path.join(data, "runs", "r1", "tasks", "HH")
        open(os.path.join(hd, "review-r1.md"), "w").write(
            "# HH review — REQUEST CHANGES\n\n## CRITICAL\n- x:1 — boom — stop\n")
        open(bus_log, "w").write("")
        r = cli("review-handoff", "--task", "HH", "--run", "r1", "--data", data, "--hold")
        assert r["ok"] is True and r["held"] is True and r["dispatched"] is False, r
        hh = next(t for t in json.load(open(os.path.join(data, "runs", "r1", "run.json")))["tasks"]
                  if t["id"] == "HH")
        assert hh["round"] == 1, hh  # nothing opened while held
        assert bus_calls() == [], bus_calls()
        # the second call releases: one round, one send
        r = cli("review-handoff", "--task", "HH", "--run", "r1", "--data", data)
        assert r["ok"] is True and r["dispatched"] is True and r["round"] == 2, r
        # a second run on the same review opens nothing more
        r = cli("review-handoff", "--task", "RH", "--run", "r1", "--data", data)
        assert r["ok"] is True and r["dispatched"] is False and r["reason"] == "already handed off", r
        # a review with no parseable findings is held, not opened
        mks("task", "add", "--id", "RZ", "--title", "empty")
        mks("task", "round", "RZ", "--kind", "build")
        zd = os.path.join(data, "runs", "r1", "tasks", "RZ")
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
        mks("task", "add", "--id", "RL", "--title", "lane review")
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
        open(prev, "w").write("## Commands (wait for each; ONE run each at the end)\n0. `B start`\n\n## Rules\n- Use the mk-builder skill.\n")
        block = commands_block_of(open(prev).read())
        assert block.startswith("## Commands") and "`B start`" in block, block
        # a task with neither names both places
        mks("task", "add", "--id", "RM", "--title", "missing review")
        mks("task", "round", "RM", "--kind", "build")
        r = cli("review-handoff", "--task", "RM", "--run", "r1", "--data", data, code=2)
        assert r["ok"] is False and "review-rN.md" in r["error"] and "RM" in r["error"], r
        # an odd id is refused
        r = cli("review-handoff", "--task", "../x", "--run", "r1", "--data", data, code=2)
        assert r["ok"] is False, r

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
