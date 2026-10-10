#!/usr/bin/env python3
"""herdr pushes lane events; this script turns them into MatrixKoder verdicts (event-driven lanes).

Usage:
  mk-lane-event.py arm --lane DIR --pane ID --task T --run R --data DIR --phase P --commit-grep RX
                      [--report PATH] [--kind claude|codex|opencode|agy] [--prompt-marker TEXT] [--stall-min N]
  mk-lane-event.py arm --lane DIR --pane ID --tasks A,B --run R --data DIR --phase P --commit-grep RX
                      [...]   # a shared lane group: one lane serving several phases of one plan
  mk-lane-event.py event                (herdr plugin hook: reads HERDR_PLUGIN_EVENT + HERDR_PLUGIN_EVENT_JSON)
  mk-lane-event.py dispatch --pane ID --text TEXT [--timeout-ms 25000] [--role build|review]
                             [--kind build|fix|takeover]
  mk-lane-event.py sweep [--data DIR]
  mk-lane-event.py disarm --lane DIR
  mk-lane-event.py --self-test

arm writes <lane>/.matrixkoder-watch.json and records the pane on the task (`mk-state task set T
pane=<pane>`, best-effort: a task missing from run state still arms). event is run by the herdr plugin
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
import fcntl, hashlib, importlib.util, json, os, re, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
WATCH = ".matrixkoder-watch.json"
LOCK = ".matrixkoder-watch.lock"
DISPATCH_RETRY_S = 5  # wait before dispatch's one automatic retry; $MK_DISPATCH_RETRY_S overrides
DISPATCH_RECHECK_S = 3  # wait before dispatch's one pane-text recheck; $MK_DISPATCH_RECHECK_S overrides
RETRY_PANE_STATES = ("idle", "done")  # pane sat still through a failed wait → the prompt was lost
FRESH_IDLE_WAIT_S = 30  # bounded wait for a fresh session's pane to go idle; never an unbounded wait
FRESH_IDLE_POLL_S = 0.5
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
    return os.path.expanduser(os.environ.get("MATRIXKODER_HOME") or "~/.matrixkoder")


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
    the last observation refreshes last_activity (the first observation does not: dispatch counts)."""
    stall = float(w.get("stall_min") or cfg("session.stall_min"))
    rx = re.compile(w.get("commit_grep") or ".*")
    report = w.get("report")
    report_ok = not report or os.path.exists(os.path.join(w["lane"], report))
    subj = lw.commit_subject(w["lane"])
    since = float(w.get("last_activity") or w.get("armed_at") or time.time())
    h = lh.check(w["pane"], w["lane"], w.get("kind"), stall, since, w.get("prompt_marker"))
    fp = pane_fp(h.get("text", ""))
    if w.get("last_pane_fp") is not None and fp != w["last_pane_fp"]:
        # subj/report_ok are computed before the second lh.check on purpose (pane activity only moves the idle clock)
        w["last_activity"] = time.time()
        h = lh.check(w["pane"], w["lane"], w.get("kind"), stall, w["last_activity"], w.get("prompt_marker"))
    w["last_pane_fp"] = fp
    state, ev = lw.decide(h, subj, rx, report_ok, h.get("text", ""), w.get("phase"), stall)
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
    the task itself when it has none) — the same list dispatch's building marks use."""
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
    """Bounded wait until <pane> is idle/done, or the wait budget ends — never an unbounded wait."""
    deadline = time.time() + budget_s
    while True:
        st, _row, _err = pane_state(pane)
        if st in RETRY_PANE_STATES or time.time() >= deadline:
            return st
        time.sleep(poll_s)


def mk_state_cmd():
    """The mk-state script this file's run-state writes go through (always a subprocess, never an
    import); $MK_STATE_CMD lets a self-test inject a fake (mk-lane.py's rule, like $MK_HERDR)."""
    return os.environ.get("MK_STATE_CMD") or lw.MK_STATE


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
    recovered:true (herdr's wait gave up while the prompt had already landed). --role review
    (reviewer panes) delivers without the board write: no mark_armed_building, the reply says
    status_unchanged:true instead. Every reply carries attempts (1 or 2), herdr_error (herdr's
    words for the attempt that did not deliver — the first attempt's when a retry lands, the final
    attempt's when both fail, the reply itself when nothing failed) and pane_entry (the freshest
    raw `herdr pane list` row for the pane, null when no observation names it). A --kind
    fix|takeover build dispatch on session.fresh_session: new first sends the pane's agent its
    new-session command (/new OpenCode, /clear Claude — the armed watch's kind names the agent)
    and waits (bounded) for the pane to go idle; the reply's fresh_session names the command sent,
    or null. Other failures → ok:false. Completed checks exit 0 whatever the verdict; only usage
    errors exit 2."""
    pane, text = opt(a, "--pane"), opt(a, "--text")
    if not (pane and text):
        fail("dispatch needs --pane --text")
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
        _prompt_attempt(pane, fresh, ms)  # the reset itself; its outcome does not gate the round
        wait_idle(pane)
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
            out({"ok": True, "delivered": True, "pane": pane, "agent_status": status, "timeout_ms": ms,
                 "attempts": attempts, "herdr_error": fail_why or why, "pane_entry": entry,
                 "fresh_session": fresh,
                 **({"pane_list_error": pl_err} if pl_err else {}), **delivered_status(pane, role)})
            return
        fail_why = why
        if kind == "fail":  # herdr failed some other way: its own words, no delivery, no pane check
            out({"ok": False, "delivered": False, "pane": pane, "timeout_ms": ms, "attempts": attempts,
                 "herdr_error": why, "error": why, "pane_entry": entry, "fresh_session": fresh})
            return
        # exc / wait: a timeout proves nothing either way — the pane's own state decides delivery
        st, entry, pl_err = pane_state(pane)
        if st == "working":  # the wait gave up, the agent did not: the prompt is still being delivered
            out({"ok": True, "delivered": True, "pane_state": "working", "pane": pane, "timed_out": True,
                 "timeout_ms": ms, "attempts": attempts, "herdr_error": why, "error": why,
                 "pane_entry": entry, "fresh_session": fresh, **delivered_status(pane, role)})
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
                out({"ok": True, "delivered": True, "recovered": True, "pane_state": st, "pane": pane,
                     "timed_out": True, "timeout_ms": ms, "attempts": attempts, "herdr_error": why,
                     "error": why, "pane_entry": entry, "fresh_session": fresh,
                     **({"pane_list_error": pl_err} if pl_err else {}), **delivered_status(pane, role)})
                return
        out({"ok": False, "delivered": False, "pane_state": st, "pane": pane, "timed_out": True,
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
    out({"ok": True, "lane": lane, "pane": pane, "watch": os.path.join(lane, WATCH), "armed_at": now,
         **({"task_pane_error": "; ".join(errs)} if errs else {})})


def cmd_disarm(a):
    lane = opt(a, "--lane")
    if not lane:
        fail("disarm needs --lane")
    wf = os.path.join(lane, WATCH)
    with locked(lane):
        removed = os.path.exists(wf)
        if removed:
            os.remove(wf)
    out({"ok": True, "lane": lane, "removed": removed})


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


def cmd_sweep(a):
    data = opt(a, "--data")
    checked, skipped, verdicts = 0, 0, []
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
            verdicts.append({"lane": wt, "task": w.get("task"), "verdict": state,
                             "status": status, "recorded": rec.get("ok", False)})
    out({"ok": True, "checked": checked, "skipped": skipped, "verdicts": verdicts})


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
    os.environ["MATRIXKODER_HOME"] = home
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
        tasks = ("H1", "H2", "H3", "H4", "H5", "H6", "H7")
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
                    "    sys.stdout.write(open(os.path.join(base, 'pane_list_out.txt')).read())\n"
                    "    sys.exit(0)\n"
                    "else:\n"
                    "    print(json.dumps({'result': {}}))\n")
        os.chmod(os.path.join(bin_, "herdr"), 0o755)
        os.environ["FAKE_HERDR_DIR"] = tmp
        # canned dispatch replies (dispatch tests rewrite these per case)
        open(os.path.join(tmp, "prompt_out.txt"), "w").write('{"result": {"agent": {"agent_status": "idle"}}}\n')
        open(os.path.join(tmp, "prompt_err.txt"), "w").write("")
        open(os.path.join(tmp, "prompt_rc.txt"), "w").write("0")
        open(os.path.join(tmp, "pane_list_out.txt"), "w").write('{"result": {"panes": []}}\n')
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
        r = event(tid="H4", exited=True)
        assert r.get("noop") is True

        # sweep: STALL only after stall_min of silence; a lane with a verdict is skipped
        old = f"{int(time.time() - 300)} +0000"
        g(lanes["H5"], "commit", "-q", "--allow-empty", "-m", "chore: wip",
          dates={"GIT_COMMITTER_DATE": old, "GIT_AUTHOR_DATE": old})
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
        assert "timeout" in r["herdr_error"] and "timeout" in r["error"] and r["timed_out"] is True, r
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

        srv.shutdown()
        srv.server_close()
    finally:
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
    except SystemExit:
        raise
    except Exception as e:  # never die silently: one JSON line with the failure
        out({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
        sys.exit(2)
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
