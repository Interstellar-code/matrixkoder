#!/usr/bin/env python3
"""herdr pushes lane events; this script turns them into MatrixKoder verdicts (event-driven lanes).

Usage:
  mk-lane-event.py arm --lane DIR --pane ID --task T --run R --data DIR --phase P --commit-grep RX
                      [--report PATH] [--kind claude|codex|opencode|agy] [--prompt-marker TEXT] [--stall-min 15]
  mk-lane-event.py event                (herdr plugin hook: reads HERDR_PLUGIN_EVENT + HERDR_PLUGIN_EVENT_JSON)
  mk-lane-event.py dispatch --pane ID --text TEXT [--timeout-ms 25000]
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
herdr runs one process per event with no ordering guarantee: every read/decide/write of a watch file
happens under an fcntl.flock on <lane>/.matrixkoder-watch.lock (a stable inode next to the atomically
replaced watch file). One JSON line per command.
"""
import fcntl, hashlib, importlib.util, json, os, re, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
WATCH = ".matrixkoder-watch.json"
LOCK = ".matrixkoder-watch.lock"

sys.dont_write_bytecode = True  # no stray __pycache__ next to the scripts


def _load(name, path):
    _spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mod)
    return mod


lw = _load("lane_watch", os.path.join(HERE, "lane-watch.py"))  # verdict logic lives there — import it
lh = _load("lane_health", os.path.join(HERE, "lane-health.py"))

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
    stall = float(w.get("stall_min") or 15)
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
    stamp the watch file. A failed mk-state write stamps nothing, so the next event retries."""
    rec = lw.record({"state": state, "commit": subj, "evidence": evidence,
                     "agent_status": (h or {}).get("agent_status")}, w["task"], w["data"], w.get("run"))
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
    r = subprocess.run([sys.executable, lw.MK_STATE, "task", "set", task, f"pane={pane}", *common],
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error": (r.stderr or r.stdout).strip()[-200:]}


def herdr_bin():
    """The herdr binary dispatch runs; $MK_HERDR lets a self-test inject a fake (mk-lane.py's rule)."""
    return os.environ.get("MK_HERDR") or "herdr"


def _jload(s):
    s = (s or "").strip()
    if not s.startswith("{"):
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def pane_state(pane):
    """(agent_status of <pane> from `herdr pane list`, error). A failed / malformed list is an
    error, never a guessed state — the caller reports delivered:false with both."""
    try:
        r = subprocess.run([herdr_bin(), "pane", "list"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"{type(e).__name__}: {e}"[:200]
    panes = (lambda j: j.get("result", {}).get("panes") if isinstance(j, dict) else None)(_jload(r.stdout))
    if r.returncode != 0 or not isinstance(panes, list):
        return None, (r.stderr or r.stdout or "").strip()[-200:] or f"exit {r.returncode}"
    row = next((p for p in panes if isinstance(p, dict) and p.get("pane_id") == pane), None)
    return (row or {}).get("agent_status"), None


def cmd_dispatch(a):
    """Prompt a pane's agent and check delivery in one call. Success → delivered:true. A timeout
    (herdr's error, or the wait settling on agent_prompt_stalled) → the pane's own state decides:
    still working → delivered:true; anything else → delivered:false. Other failures → ok:false.
    Completed checks exit 0 whatever the verdict; only usage errors exit 2."""
    pane, text = opt(a, "--pane"), opt(a, "--text")
    if not (pane and text):
        fail("dispatch needs --pane --text")
    try:
        ms = int(opt(a, "--timeout-ms", "25000"))
    except ValueError as e:
        fail(f"--timeout-ms: {e}")
    try:
        r = subprocess.run([herdr_bin(), "agent", "prompt", pane, text, "--wait", "--timeout", str(ms)],
                           capture_output=True, text=True, timeout=ms / 1000 + 30)
    except (OSError, subprocess.TimeoutExpired) as e:
        out({"ok": False, "delivered": False, "pane": pane, "timeout_ms": ms,
             "error": f"{type(e).__name__}: {e}"[:300]})
        return
    blob, err = (r.stdout or "").strip(), (r.stderr or "").strip()
    j = _jload(blob)
    jerr = j.get("error") if isinstance(j, dict) else None  # herdr can print {"error": ...} and exit 0
    agent = j.get("result", {}).get("agent") if isinstance(j, dict) else None
    status = agent.get("agent_status") if isinstance(agent, dict) else None
    why = "; ".join(x for x in ((jerr or {}).get("code") if isinstance(jerr, dict) else None, err, blob)
                    if x)[:300] or f"herdr exited {r.returncode} with no output"
    if r.returncode == 0 and isinstance(j, dict) and "result" in j and jerr is None \
            and status != "agent_prompt_stalled":
        out({"ok": True, "delivered": True, "pane": pane, "agent_status": status, "timeout_ms": ms})
    elif status == "agent_prompt_stalled" or "timeout" in why.lower() or "timed out" in why.lower():
        st, pl_err = pane_state(pane)
        base = {"pane": pane, "timed_out": True, "timeout_ms": ms, "herdr_error": why}
        if st == "working":  # the wait gave up, the agent did not: the prompt is still being delivered
            out({"ok": True, "delivered": True, "pane_state": "working", **base})
        else:
            out({"ok": False, "delivered": False, "pane_state": st, "error": why,
                 **({"pane_list_error": pl_err} if pl_err else {}), **base})
    else:
        out({"ok": False, "delivered": False, "pane": pane, "timeout_ms": ms, "error": why})


def cmd_arm(a):
    lane, pane, task = opt(a, "--lane"), opt(a, "--pane"), opt(a, "--task")
    run, data, phase, grep = opt(a, "--run"), opt(a, "--data"), opt(a, "--phase"), opt(a, "--commit-grep")
    if not all((lane, pane, task, run, data, phase, grep)):
        fail("arm needs --lane --pane --task --run --data --phase --commit-grep")
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
        stall = float(opt(a, "--stall-min", "15"))
    except ValueError as e:
        fail(f"--stall-min: {e}")
    lane = os.path.abspath(lane)
    ensure_lane_exclude(lane)
    now = time.time()
    w = {"lane": lane, "pane": pane, "task": task, "run": run, "data": os.path.abspath(data),
         "phase": phase, "commit_grep": grep, "report": opt(a, "--report"), "kind": kind,
         "prompt_marker": opt(a, "--prompt-marker"), "stall_min": stall,
         "armed_at": now, "last_activity": now, "last_pane_fp": None, "verdict": None, "verdict_at": None}
    with locked(lane):
        write_watch(lane, w)
    rec = record_pane(task, pane, w["data"], run)
    out({"ok": True, "lane": lane, "pane": pane, "watch": os.path.join(lane, WATCH), "armed_at": now,
         **({} if rec.get("ok") else {"task_pane_error": rec.get("error", "mk-state task set failed")})})


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
                    "    open(os.path.join(base, 'calls.log'), 'a').write(' '.join(a) + chr(10))\n"
                    "    sys.stdout.write(open(os.path.join(base, 'prompt_out.txt')).read())\n"
                    "    sys.stderr.write(open(os.path.join(base, 'prompt_err.txt')).read())\n"
                    "    sys.exit(int(open(os.path.join(base, 'prompt_rc.txt')).read().strip() or '0'))\n"
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
