#!/usr/bin/env python3
"""Find stranded builders and send the standard nudge.

Usage: mk-lane-nudge.py scan [--data D] [--json] [--idle-min N]
       mk-lane-nudge.py nudge [--data D] [--apply] [--idle-min N]
       mk-lane-nudge.py --self-test

One JSON line per call. scan classifies every BUSY slot that has an armed pane
(.matrixkoder-watch.json in the lane). nudge prints the standard nudge for each
stranded or waiting-on-job lane and, ONLY with --apply, sends it through
`python3 scripts/mk-lane-event.py dispatch --pane <pane> --text <nudge>`.
Without --apply it is a dry run and sends nothing.

Classes: working | waiting-on-job | stranded | finished | idle-no-work.
Never nudges a working or finished lane, never a reviewer pane (agent name
ending -review-* , or a pane that is not the pane the task's watch recorded),
and never more than once per lane per 10 minutes. State is <data>/nudge-state.json,
written atomically (temp file + rename).

A missing herdr or ps, or malformed herdr output, is a named error in the JSON
and exit 0 with an empty lanes list — never a crash, never a non-zero exit.

Tests never touch the real ~/.matrixkoder. When MK_NUDGE_LANES_JSON is set it
is a path to a JSON file used INSTEAD of `mk-lane.py list` (read only when
set). The slots come from `mk-lane.py list`, i.e. the project DB's lane_slots rows
(L10.5): this script never reads lanes.json. Tests put a fake `herdr`, a fake `ps` and a fake `mk-lane-event.py`
first on PATH.
"""
import fcntl, json, os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
import importlib.util

_pspec = importlib.util.spec_from_file_location("mkpaths", os.path.join(HERE, "mkpaths.py"))
mkpaths = importlib.util.module_from_spec(_pspec)
_pspec.loader.exec_module(mkpaths)

RATE_S = 600
IDLE_MIN_DEFAULT = 3
PANE_TAIL = 40
WATCH = ".matrixkoder-watch.json"
STATE_NAME = "nudge-state.json"
LOCK_NAME = "nudge-state.lock"
LOCK_WAIT_S = 5.0

# Same idea as lane-watch child_progress, self-contained (do not import it).
CHILD_RE = re.compile(
    r"(?:^|/)(?:selftest\.sh|pytest|tsc)(?:\s|$)"
    r"|claude\s+plugin\s+test"
    r"|python3?\b.*--self-test",
    re.I)
WAIT_LATER_RE = re.compile(
    r"i['\u2019]ll wait|i will wait|will read|when the job finishes|until the job"
    r"|wait for the exit|wait for it to finish|end(?:ed|ing) (?:my |your )?turn",
    re.I)
DONE_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9]* R\d+ DONE\b")
REVIEW_NAME_RE = re.compile(r"-review-", re.I)


def out(obj):
    print(json.dumps(obj, default=str))


def named_error(name, detail):
    return {"ok": True, "error": name, "detail": str(detail)[:300], "lanes": []}


def lanes_from_list(data):
    """Slots from `mk-lane.py list`. MK_NUDGE_LANES_JSON, when set, is a path to a
    JSON file read INSTEAD of spawning mk-lane.py — tests only, and only when set."""
    override = os.environ.get("MK_NUDGE_LANES_JSON")
    if override:
        with open(override) as f:
            doc = json.load(f)
        slots = doc.get("slots") if isinstance(doc, dict) else doc
        if not isinstance(slots, list):
            raise ValueError("MK_NUDGE_LANES_JSON has no slots list")
        return slots
    cmd = [sys.executable, os.path.join(HERE, "mk-lane.py"), "list"]
    if data:
        cmd += ["--data", data]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "mk-lane.py list failed").strip()[:300])
    doc = json.loads(r.stdout)
    slots = doc.get("slots") if isinstance(doc, dict) else None
    if not isinstance(slots, list):
        raise ValueError("mk-lane.py list returned no slots list")
    return slots


def herdr_agents():
    """`herdr agent list` -> list of agent dicts. Missing binary or bad JSON raises."""
    if shutil.which("herdr") is None:
        raise FileNotFoundError("herdr-missing")
    r = subprocess.run(["herdr", "agent", "list"], capture_output=True, text=True, timeout=10)
    text = (r.stdout or "").strip()
    if not text:
        raise ValueError("herdr-malformed: empty output")
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ValueError("herdr-malformed: %s" % e)
    if isinstance(doc, dict) and doc.get("error") and "result" not in doc:
        raise ValueError("herdr-error: %s" % doc.get("error"))
    agents = None
    if isinstance(doc, dict):
        res = doc.get("result")
        if isinstance(res, dict):
            agents = res.get("agents")
        elif isinstance(doc.get("agents"), list):
            agents = doc["agents"]
    elif isinstance(doc, list):
        agents = doc
    if not isinstance(agents, list):
        raise ValueError("herdr-malformed: no agents list")
    return agents


def pane_text(pane):
    """Last PANE_TAIL lines of `herdr pane read <pane>`. A failed read is empty."""
    if not pane or shutil.which("herdr") is None:
        return ""
    try:
        r = subprocess.run(["herdr", "pane", "read", pane],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return ""
    return "\n".join((r.stdout or "").splitlines()[-PANE_TAIL:])


def ps_rows():
    """`ps -axo pid,ppid,command` rows as (pid, ppid, command). Missing ps raises."""
    if shutil.which("ps") is None:
        raise FileNotFoundError("ps-missing")
    r = subprocess.run(["ps", "-axo", "pid,ppid,command"],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError("ps-failed")
    rows = []
    for line in (r.stdout or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        rows.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return rows


def _ancestor_pids(rows):
    """PIDs of this process and every parent visible in the ps table."""
    by_pid = {}
    for pid, ppid, cmd in rows:
        by_pid[str(pid)] = str(ppid)
    pids = set()
    cur = str(os.getpid())
    seen = set()
    while cur and cur not in seen and cur not in ("0", "1"):
        seen.add(cur)
        pids.add(cur)
        cur = by_pid.get(cur) or (str(os.getppid()) if cur == str(os.getpid()) else None)
        if cur == str(os.getpid()):
            break
    pids.add(str(os.getpid()))
    return pids


def lane_in_cmd(cmd, lane):
    """True when the lane path is a whole path token, not a prefix of a longer lane.

    `/…/matrixkoder-mk-1` must not match `/…/matrixkoder-mk-10/scripts/selftest.sh`.
    A hit is the lane path followed by `/`, end of string, or a non-path character.
    """
    needle = (lane or "").rstrip("/")
    if not needle or needle not in cmd:
        return False
    i = 0
    while True:
        j = cmd.find(needle, i)
        if j < 0:
            return False
        after = cmd[j + len(needle):j + len(needle) + 1]
        if after == "" or after == "/" or after not in "/._-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
            return True
        i = j + 1


def matching_children(rows, lane):
    """Commands whose arguments point into the lane path AND look like a test/build.

    Excludes this process and its parent chain (a `--self-test` running inside the
    lane must not count as the lane's own job).
    """
    if not lane:
        return []
    skip = _ancestor_pids(rows)
    out_rows = []
    for pid, ppid, cmd in rows:
        if str(pid) in skip:
            continue
        if lane_in_cmd(cmd, lane) and CHILD_RE.search(cmd):
            out_rows.append({"pid": pid, "ppid": ppid, "command": cmd[:200]})
    return out_rows


def read_watch(lane):
    try:
        with open(os.path.join(lane, WATCH)) as f:
            w = json.load(f)
    except (OSError, ValueError):
        return None
    return w if isinstance(w, dict) and w.get("pane") else None


def agent_for(agents, pane):
    for a in agents:
        if isinstance(a, dict) and str(a.get("pane_id")) == str(pane):
            return a
    return None


def idle_minutes(agent, now):
    """Minutes the pane has held its current status. herdr gives no timestamp, so
    MK_NUDGE_SEQ_EPOCH (tests) maps state_change_seq to an epoch. With no clock an
    idle/done pane counts as old enough; the other stranded gates still have to hold."""
    raw = os.environ.get("MK_NUDGE_SEQ_EPOCH")
    seq = (agent or {}).get("state_change_seq")
    if raw and isinstance(seq, int) and not isinstance(seq, bool):
        try:
            base = json.loads(raw)
            epoch = float(base.get(str(seq), 0) or 0)
        except (ValueError, TypeError):
            epoch = 0.0
        if epoch:
            return max(0.0, (now - epoch) / 60.0)
    return float(IDLE_MIN_DEFAULT) + 1.0


def commit_since(lane, armed_at, grep):
    """True when HEAD is newer than armed_at AND its subject matches the armed grep."""
    git_dir = os.path.join(lane or "", ".git")
    if not lane or not (os.path.isdir(git_dir) or os.path.isfile(git_dir)):
        return False
    try:
        r = subprocess.run(["git", "-C", lane, "log", "-1", "--format=%ct%n%s"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    lines = (r.stdout or "").splitlines()
    if len(lines) < 2:
        return False
    try:
        ts = float(lines[0])
    except ValueError:
        return False
    if ts < float(armed_at or 0):
        return False
    if grep:
        try:
            if not re.search(grep, lines[1]):
                return False
        except re.error:
            return False
    return True


def report_present(watch):
    path = (watch or {}).get("report")
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def is_reviewer(agent, watch):
    """A reviewer pane: the agent name ends in the -review- marker, or herdr's pane
    for this agent is not the pane the task's watch recorded."""
    name = str((agent or {}).get("name") or "")
    if REVIEW_NAME_RE.search(name):
        return True
    pane = str((watch or {}).get("pane") or "")
    recorded = str((agent or {}).get("pane_id") or "")
    if recorded and pane and recorded != pane:
        return True
    return False


def classify(status, idle_min, child_running, commit, report, text, idle_need):
    """working | waiting-on-job | stranded | finished | idle-no-work."""
    st = (status or "unknown").lower()
    if st == "working":
        return "working"
    done_token = bool(DONE_TOKEN_RE.search(text or ""))
    if commit and report and done_token:
        return "finished"
    if st in ("idle", "done"):
        if child_running:
            return "waiting-on-job"
        waited = bool(WAIT_LATER_RE.search(text or ""))
        if idle_min >= idle_need and not commit and not report and waited:
            return "stranded"
        return "idle-no-work"
    return "idle-no-work"


def nudge_text(task, rnd):
    token = "%s R%s" % (task, rnd)
    return (
        "%s nudge: your turn ended while a job you started may still be running; "
        "nothing wakes you when it finishes. NOW, in this turn: wait with "
        "'until grep -q ^EXIT: <your result file>; do sleep 5; done; tail -n 8 <file>' "
        "(bounded 20 minutes), read the numbers, then continue the brief. "
        "Never end a turn while a job you started is running. "
        "The last line of your reply must be %s DONE" % (token, token)
    )


def round_of(watch):
    m = re.search(r"R(\d+)", str((watch or {}).get("phase") or ""))
    return int(m.group(1)) if m else 1


def _mk_home():
    """One MK home rule (C16): $MK_HOME and $MATRIXKODER_HOME are the same setting; when both
    are set they must agree. The rule itself is scripts/mkpaths.home (C1); this copy raised a
    plain RuntimeError and keeps raising one for its callers."""
    try:
        return mkpaths.home()
    except mkpaths.Fail as e:
        raise RuntimeError(str(e))


def state_path(data):
    return mkpaths.nudge_state(data)


def load_state(path):
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {"nudged": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("nudged"), dict):
        return {"nudged": {}}
    return doc


def save_state(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".nudge-state-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def rate_ok(doc, lane, now):
    """False while the last nudge is younger than RATE_S.

    A timestamp in the future (clock skew) is treated as expired so one bad clock
    cannot silence a lane forever. The caller records a warning when that happens.
    """
    try:
        last = float((doc.get("nudged") or {}).get(lane))
    except (TypeError, ValueError):
        return True
    if last > now:
        return True
    return (now - last) >= RATE_S


def future_state(doc, lane, now):
    try:
        last = float((doc.get("nudged") or {}).get(lane))
    except (TypeError, ValueError):
        return False
    return last > now


def error_name(exc):
    msg = str(exc)
    if isinstance(exc, FileNotFoundError):
        return "herdr-missing" if "herdr" in msg else "ps-missing"
    for name in ("herdr-malformed", "herdr-error", "herdr-missing", "ps-missing"):
        if msg.startswith(name):
            return name
    return "herdr-error"


def scan(data, idle_min, now=None):
    if now is None:
        now = float(os.environ["MK_NUDGE_NOW"]) if os.environ.get("MK_NUDGE_NOW") else time.time()
    try:
        slots = lanes_from_list(data)
    except Exception as e:
        return named_error("lanes-error", e)
    try:
        agents = herdr_agents()
    except Exception as e:
        return named_error(error_name(e), e)
    try:
        rows = ps_rows()
    except FileNotFoundError as e:
        return named_error("ps-missing", e)
    except Exception as e:
        return named_error("ps-error", e)

    pane_counts = {}
    for slot in slots:
        if not isinstance(slot, dict) or str(slot.get("state") or "").lower() != "busy":
            continue
        watch = read_watch(slot.get("path") or "")
        if not watch:
            continue
        pane = str(watch.get("pane") or "")
        if pane:
            pane_counts[pane] = pane_counts.get(pane, 0) + 1

    lanes = []
    for slot in slots:
        if not isinstance(slot, dict) or str(slot.get("state") or "").lower() != "busy":
            continue
        lane = slot.get("path") or ""
        watch = read_watch(lane)
        if not watch:
            continue
        pane = watch.get("pane")
        agent = agent_for(agents, pane) or {}
        status = str(agent.get("agent_status") or "unknown")
        kids = matching_children(rows, lane)
        text = pane_text(pane) if status.lower() in ("idle", "done", "unknown") else ""
        commit = commit_since(lane, watch.get("armed_at") or 0, watch.get("commit_grep"))
        report = report_present(watch)
        klass = classify(status, idle_minutes(agent, now), bool(kids),
                         commit, report, text, idle_min)
        if pane and pane_counts.get(str(pane), 0) > 1:
            klass = "ambiguous-pane"
        lanes.append({
            "slot": slot.get("slot"), "lane": lane,
            "task": watch.get("task") or slot.get("task"),
            "run": watch.get("run") or slot.get("run"),
            "pane": pane, "agent_status": status, "agent_name": agent.get("name"),
            "reviewer": is_reviewer(agent, watch), "child_running": bool(kids),
            "commit": commit, "report": report, "class": klass,
        })
    return {"ok": True, "lanes": lanes, "idle_min": idle_min}


def dispatch(pane, text):
    """The only send path: mk-lane-event.py dispatch. A stub of that name first on
    PATH wins (tests); otherwise the sibling script next to this file."""
    stub = shutil.which("mk-lane-event.py")
    cmd = ([stub] if stub else [sys.executable, os.path.join(HERE, "mk-lane-event.py")])
    cmd += ["dispatch", "--pane", pane, "--text", text]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stdout or r.stderr or "").strip()[:300]


def lock_path_for(data):
    return mkpaths.nudge_state_lock(data)


class NudgeLock:
    """Exclusive flock on <data>/nudge-state.lock covering read, rate check, send, write.

    Non-blocking try first, then a bounded wait of LOCK_WAIT_S. If another nudge
    still holds it, the caller exits 0 with skipped "another nudge is running"
    and sends nothing.
    """

    def __init__(self, data):
        self.path = lock_path_for(data)
        self.fd = None
        self.busy = False

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fd = open(self.path, "a+")
        try:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self
        except OSError:
            pass
        deadline = time.monotonic() + LOCK_WAIT_S
        while time.monotonic() < deadline:
            try:
                fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                time.sleep(0.05)
        self.busy = True
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self.fd.close()


def nudge(data, apply, idle_min, now=None):
    if now is None:
        now = float(os.environ["MK_NUDGE_NOW"]) if os.environ.get("MK_NUDGE_NOW") else time.time()
    # The lock wraps read-state, the rate check, dispatch AND write-state so two
    # processes cannot both send for one lane. scan() itself only reads.
    if apply:
        lock = NudgeLock(data)
        lock.__enter__()
        if lock.busy:
            scanned = scan(data, idle_min, now)
            scanned["dry_run"] = False
            scanned["sent"] = []
            scanned["ok"] = True
            scanned["skipped"] = "another nudge is running"
            for lane in scanned.get("lanes") or []:
                lane["nudge"] = None
                lane["sent"] = False
                lane["skipped"] = "another nudge is running"
            lock.__exit__(None, None, None)
            return scanned
        try:
            return _nudge_locked(data, apply, idle_min, now)
        finally:
            lock.__exit__(None, None, None)
    return _nudge_locked(data, apply, idle_min, now)


def _nudge_locked(data, apply, idle_min, now):
    scanned = scan(data, idle_min, now)
    scanned["dry_run"] = not apply
    if scanned.get("error"):
        scanned["sent"] = []
        return scanned
    path = state_path(data)
    doc = load_state(path)
    sent = []
    warnings = []
    for lane in scanned["lanes"]:
        lane["nudge"] = None
        lane["sent"] = False
        lane["skipped"] = None
        if lane["class"] == "ambiguous-pane":
            lane["skipped"] = "ambiguous-pane"
            continue
        if lane["class"] not in ("stranded", "waiting-on-job"):
            continue
        if lane["reviewer"]:
            lane["skipped"] = "reviewer"
            continue
        text = nudge_text(lane.get("task") or "TASK", round_of(read_watch(lane["lane"])))
        lane["nudge"] = text
        if future_state(doc, lane["lane"], now):
            lane["warning"] = "future-dated state timestamp; treating as expired"
            warnings.append({"lane": lane["lane"], "warning": lane["warning"]})
        if not rate_ok(doc, lane["lane"], now):
            lane["skipped"] = "rate-limit"
            continue
        if not apply:
            lane["skipped"] = "dry-run"
            continue
        ok, detail = dispatch(lane["pane"], text)
        lane["sent"] = ok
        lane["dispatch"] = detail
        if ok:
            doc.setdefault("nudged", {})[lane["lane"]] = now
            sent.append({"pane": lane["pane"], "lane": lane["lane"]})
        else:
            lane["skipped"] = "dispatch-failed"
    if apply and sent:
        save_state(path, doc)
    if warnings:
        scanned["warnings"] = warnings
    scanned["sent"] = sent
    return scanned


def opt(argv, flag, default=None):
    if flag not in argv:
        return default
    i = argv.index(flag)
    if i + 1 >= len(argv):
        sys.exit("%s needs a value" % flag)
    return argv[i + 1]


def main(argv):
    if "--self-test" in argv:
        return self_test()
    cmd = next((a for a in argv if not a.startswith("-")), None)
    if cmd not in ("scan", "nudge"):
        sys.exit(__doc__)
    data = opt(argv, "--data")
    try:
        idle = float(opt(argv, "--idle-min", str(IDLE_MIN_DEFAULT)))
    except ValueError:
        sys.exit("--idle-min needs a number")
    out(scan(data, idle) if cmd == "scan" else nudge(data, "--apply" in argv, idle))
    return 0


def write_exec(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)


HERDR_STUB = (
    "#!" + sys.executable + "\n"
    "import os, sys\n"
    "cmd = \" \".join(sys.argv[1:3])\n"
    "if cmd == \"agent list\":\n"
    "    sys.stdout.write(open(os.environ[\"MK_NUDGE_HERDR_JSON\"]).read())\n"
    "elif cmd == \"pane read\":\n"
    "    f = os.path.join(os.environ[\"MK_NUDGE_ROOT\"], \"pane-%s.txt\" % sys.argv[3])\n"
    "    if os.path.exists(f):\n"
    "        sys.stdout.write(open(f).read())\n"
    "else:\n"
    "    sys.stderr.write(\"unexpected herdr \" + cmd)\n"
    "    sys.exit(3)\n"
)

PS_STUB = (
    "#!" + sys.executable + "\n"
    "import os, sys\n"
    "sys.stdout.write(open(os.environ[\"MK_NUDGE_PS_FILE\"]).read())\n"
)

DISPATCH_STUB = (
    "#!" + sys.executable + "\n"
    "import os, sys\n"
    "pane = sys.argv[sys.argv.index(\"--pane\") + 1]\n"
    "text = sys.argv[sys.argv.index(\"--text\") + 1]\n"
    "fh = open(os.environ[\"MK_NUDGE_SENT_LOG\"], \"a\")\n"
    "fh.write(pane + \" \" + \" \".join(text.splitlines()) + chr(10))\n"
    "fh.close()\n"
    "sys.stdout.write('{\"ok\": true}' + chr(10))\n"
)

def self_test():
    fails = []
    # One temp MK home under both names (C16) for the whole suite.
    nudge_home = tempfile.mkdtemp(prefix="mk-nudge-home-")
    os.environ["MK_HOME"] = nudge_home
    os.environ["MATRIXKODER_HOME"] = nudge_home
    os.environ["MK_SELFTEST"] = "1"

    def check(name, cond):
        if not cond:
            fails.append(name)

    t_wait = "I'll wait for the EXIT line, then read the log when the job finishes."
    t_done = "All gates passed.\nN2 R1 DONE"
    check("class-working", classify("working", 10, True, False, False, t_wait, 3) == "working")
    check("class-waiting", classify("idle", 10, True, False, False, "", 3) == "waiting-on-job")
    check("class-waiting-done-pane", classify("done", 1, True, False, False, "", 3) == "waiting-on-job")
    check("class-stranded", classify("idle", 5, False, False, False, t_wait, 3) == "stranded")
    check("class-stranded-too-fresh", classify("idle", 1, False, False, False, t_wait, 3) == "idle-no-work")
    check("class-finished", classify("done", 30, False, True, True, t_done, 3) == "finished")
    check("class-finished-beats-child", classify("idle", 30, True, True, True, t_done, 3) == "finished")
    check("class-idle", classify("idle", 30, False, False, False, "reading the brief", 3) == "idle-no-work")
    check("class-no-commit-not-finished", classify("done", 30, False, False, True, t_done, 3) == "idle-no-work")
    check("class-unknown", classify("blocked", 30, False, False, False, t_wait, 3) == "idle-no-work")
    text = nudge_text("N2", 1)
    check("nudge-text", text.endswith("N2 R1 DONE") and "until grep -q ^EXIT:" in text)
    check("child-re-hit", bool(CHILD_RE.search("bash /lane/scripts/selftest.sh")))
    check("child-re-miss", not CHILD_RE.search("python3 scripts/mk-lane.py list"))
    check("reviewer-name", is_reviewer({"name": "d6b-review-r2", "pane_id": "wV:pE"}, {"pane": "wV:pE"}))
    check("builder-not-reviewer", not is_reviewer({"name": "d7s-fix-r2", "pane_id": "wV:p9"}, {"pane": "wV:p9"}))
    check("reviewer-unrecorded", is_reviewer({"name": "stray", "pane_id": "wZ:p1"}, {"pane": "wZ:p9"}))

    root = tempfile.mkdtemp(prefix="mk-nudge-selftest-")
    try:
        bin_dir = os.path.join(root, "bin")
        os.makedirs(bin_dir)
        data = os.path.join(root, "data")
        os.makedirs(data)
        lanes_file = os.path.join(root, "lane-list.json")  # a `mk-lane.py list` fixture, not the registry
        herdr_file = os.path.join(root, "herdr.json")
        ps_file = os.path.join(root, "ps.txt")
        sent_log = os.path.join(root, "sent.log")

        def make_lane(name, pane, task, phase, grep, armed_at, report_body=None, commit=None):
            lane = os.path.join(root, "lanes", name)
            os.makedirs(os.path.join(lane, "repo"))
            subprocess.run(["git", "init", "-q", lane], check=True)
            subprocess.run(["git", "-C", lane, "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-C", lane, "config", "user.name", "t"], check=True)
            open(os.path.join(lane, "repo", "f"), "w").write("x\n")
            subprocess.run(["git", "-C", lane, "add", "repo/f"], check=True)
            env = dict(os.environ)
            subject = "chore: start"
            if commit:
                env["GIT_AUTHOR_DATE"] = commit
                env["GIT_COMMITTER_DATE"] = commit
                subject = "feat(lanes): landed"
            subprocess.run(["git", "-C", lane, "commit", "-qm", subject], env=env, check=True)
            watch = {
                "lane": lane, "pane": pane, "task": task, "run": "r1", "data": data,
                "phase": phase, "commit_grep": grep,
                "report": os.path.join(lane, ".matrixkoder-report-r1.md"),
                "armed_at": armed_at,
            }
            json.dump(watch, open(os.path.join(lane, WATCH), "w"))
            if report_body is not None:
                open(watch["report"], "w").write(report_body)
            return lane

        now = 1800000000.0
        old = now - 3600
        L = {}
        L["working"] = make_lane("working", "w1:p1", "A1", "A1 R1", "^feat", old)
        L["waiting"] = make_lane("waiting", "w1:p2", "B1", "B1 R1", "^feat", old)
        L["stranded"] = make_lane("stranded", "w1:p3", "C1", "C1 R1", "^feat", old)
        L["finished"] = make_lane("finished", "w1:p4", "D1", "D1 R1", r"^feat\(lanes\)", old,
                                  report_body="done\n", commit=str(int(now - 60)))
        L["idle"] = make_lane("idle", "w1:p5", "E1", "E1 R1", "^feat", old)
        L["review"] = make_lane("review", "w1:p6", "F1", "F1 R1", "^feat", old)
        L["fresh"] = make_lane("fresh", "w1:p7", "G1", "G1 R1", "^feat", old)
        L["free"] = make_lane("free", "w1:p8", "H1", "H1 R1", "^feat", old)

        slots = [{"slot": i + 1, "path": lane, "state": "free" if key == "free" else "busy",
                  "run": "r1", "task": key} for i, (key, lane) in enumerate(L.items())]
        json.dump({"ok": True, "slots": slots}, open(lanes_file, "w"))

        spec = [
            ("w1:p1", "working", "a1-build-r1", "working", 1),
            ("w1:p2", "idle", "b1-build-r1", "waiting", 2),
            ("w1:p3", "done", "c1-build-r1", "stranded", 3),
            ("w1:p4", "done", "d1-build-r1", "finished", 4),
            ("w1:p5", "idle", "e1-build-r1", "idle", 5),
            ("w1:p6", "idle", "f1-review-r1", "review", 6),
            ("w1:p7", "idle", "g1-build-r1", "fresh", 7),
        ]
        agents = [{"pane_id": p, "agent_status": st, "name": nm, "cwd": L[k], "state_change_seq": seq}
                  for p, st, nm, k, seq in spec]
        json.dump({"id": "cli:agent:list", "result": {"agents": agents}}, open(herdr_file, "w"))
        seq_epoch = {str(i): now - 1800 for i in range(1, 7)}
        seq_epoch["7"] = now - 60

        # pane text taken from the shape of a real stranded OpenCode pane (wW:p8):
        # the builder said it would wait, then the turn ended.
        open(os.path.join(root, "pane-w1:p3.txt"), "w").write(
            "The suite buffers its report until the end, so there is nothing to read yet.\n"
            "I'll wait for the EXIT line and read it when the job finishes.\n")
        open(os.path.join(root, "pane-w1:p4.txt"), "w").write("Gates green.\nD1 R1 DONE\n")
        open(os.path.join(root, "pane-w1:p6.txt"), "w").write("I'll wait for the suite.\n")
        open(os.path.join(root, "pane-w1:p7.txt"), "w").write("I'll wait for the EXIT line.\n")
        open(os.path.join(root, "pane-w1:p5.txt"), "w").write("Reading the plan.\n")

        open(ps_file, "w").write("\n".join([
            "  PID  PPID COMMAND",
            "100 1 python3 %s/scripts/selftest.sh" % L["waiting"],
            "101 1 python3 %s/scripts/thing.py --self-test" % L["working"],
            "102 1 /usr/sbin/syslogd",
            "",
        ]))
        write_exec(os.path.join(bin_dir, "herdr"), HERDR_STUB)
        write_exec(os.path.join(bin_dir, "ps"), PS_STUB)
        write_exec(os.path.join(bin_dir, "mk-lane-event.py"), DISPATCH_STUB)

        env = dict(os.environ)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        env.update({"MK_NUDGE_NOW": str(now), 
            "MK_NUDGE_LANES_JSON": lanes_file, "MK_NUDGE_HERDR_JSON": herdr_file,
            "MK_NUDGE_PS_FILE": ps_file, "MK_NUDGE_ROOT": root, "MK_NUDGE_SENT_LOG": sent_log,
            "MK_NUDGE_SEQ_EPOCH": json.dumps(seq_epoch), "MATRIXKODER_HOME": data, "MK_HOME": data,
        })

        def run(args):
            r = subprocess.run([sys.executable, os.path.abspath(__file__)] + args,
                               capture_output=True, text=True, env=env, timeout=60)
            lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
            try:
                doc = json.loads(lines[-1]) if lines else {"_raw": r.stdout, "_err": r.stderr}
            except ValueError:
                doc = {"_raw": r.stdout, "_err": r.stderr}
            return r.returncode, doc

        code, doc = run(["scan", "--data", data, "--json"])
        by = {x["task"]: x["class"] for x in doc.get("lanes", [])} if isinstance(doc.get("lanes"), list) else {}
        check("scan-exit", code == 0 and doc.get("ok") is True)
        check("scan-working", by.get("A1") == "working")
        check("scan-waiting", by.get("B1") == "waiting-on-job")
        check("scan-stranded", by.get("C1") == "stranded")
        check("scan-finished", by.get("D1") == "finished")
        check("scan-idle", by.get("E1") == "idle-no-work")
        check("scan-review-class", by.get("F1") == "stranded")
        check("scan-fresh", by.get("G1") == "idle-no-work")
        check("scan-free-skipped", "H1" not in by)

        code, doc = run(["nudge", "--data", data])
        check("dry-exit", code == 0 and doc.get("dry_run") is True)
        check("dry-sent-empty", doc.get("sent") == [])
        check("dry-no-log", not os.path.exists(sent_log))
        nudged = {x["task"]: x for x in doc.get("lanes", [])}
        check("dry-text-stranded", str((nudged.get("C1") or {}).get("nudge") or "").endswith("C1 R1 DONE"))
        check("dry-waiting-text", bool((nudged.get("B1") or {}).get("nudge")))
        check("dry-working-silent", (nudged.get("A1") or {}).get("nudge") is None)
        check("dry-finished-silent", (nudged.get("D1") or {}).get("nudge") is None)
        check("dry-review-skipped", (nudged.get("F1") or {}).get("skipped") == "reviewer")

        code, doc = run(["nudge", "--data", data, "--apply"])
        sent_panes = sorted(s["pane"] for s in doc.get("sent", []))
        check("apply-exit", code == 0)
        check("apply-exactly", sent_panes == ["w1:p2", "w1:p3"])
        log = open(sent_log).read() if os.path.exists(sent_log) else ""
        check("apply-log-two", log.count("\n") == 2)
        check("apply-state-written", os.path.isfile(os.path.join(data, STATE_NAME)))

        code, doc = run(["nudge", "--data", data, "--apply"])
        skipped = {x["task"]: x.get("skipped") for x in doc.get("lanes", [])}
        check("rate-sent-empty", doc.get("sent") == [])
        check("rate-stranded", skipped.get("C1") == "rate-limit")
        check("rate-waiting", skipped.get("B1") == "rate-limit")
        check("rate-log-still-two", os.path.exists(sent_log) and open(sent_log).read().count("\n") == 2)

        open(herdr_file, "w").write("this is not json\n")
        code, doc = run(["scan", "--data", data])
        check("malformed-exit", code == 0 and doc.get("ok") is True)
        check("malformed-name", doc.get("error") == "herdr-malformed")
        check("malformed-empty", doc.get("lanes") == [])

        os.rename(os.path.join(bin_dir, "herdr"), os.path.join(bin_dir, "herdr.off"))
        env["PATH"] = bin_dir
        code, doc = run(["scan", "--data", data])
        check("missing-herdr-exit", code == 0)
        check("missing-herdr-name", doc.get("error") == "herdr-missing")
        check("missing-herdr-empty", doc.get("lanes") == [])
        os.rename(os.path.join(bin_dir, "herdr.off"), os.path.join(bin_dir, "herdr"))
        env["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

        saved_ps = os.path.join(bin_dir, "ps")
        os.rename(saved_ps, saved_ps + ".off")
        # a renamed stub falls through to /bin/ps, so the missing case needs ps gone from PATH
        env["PATH"] = os.pathsep.join(
            d for d in env["PATH"].split(os.pathsep) if d and not os.path.exists(os.path.join(d, "ps")))
        json.dump({"id": "cli:agent:list", "result": {"agents": agents}}, open(herdr_file, "w"))
        code, doc = run(["scan", "--data", data])
        check("missing-ps-exit", code == 0)
        check("missing-ps-name", doc.get("error") == "ps-missing")
        check("missing-ps-empty", doc.get("lanes") == [])
        os.rename(saved_ps + ".off", saved_ps)
        env["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

        home = os.path.expanduser("~/.matrixkoder")
        check("no-real-home", not data.startswith(home) and not lanes_file.startswith(home))

        # prefix collision: lane .../matrixkoder-mk-1 must not see mk-10's job
        base = os.path.join(root, "lanes")
        mk1 = os.path.join(base, "matrixkoder-mk-1")
        mk10 = os.path.join(base, "matrixkoder-mk-10")
        rows_pref = [
            ("200", "1", "python3 %s/scripts/selftest.sh" % mk10),
            ("201", "1", "python3 %s/scripts/thing.py --self-test" % mk1),
            (str(os.getpid()), "1", "python3 %s/scripts/mk-lane-nudge.py --self-test" % mk1),
        ]
        hit1 = matching_children(rows_pref, mk1)
        hit10 = matching_children(rows_pref, mk10)
        check("prefix-mk1-not-mk10", [c["pid"] for c in hit1] == ["201"])
        check("prefix-mk10-own", [c["pid"] for c in hit10] == ["200"])
        check("self-pid-excluded", all(c["pid"] != str(os.getpid()) for c in hit1))
        # parent chain: a parent whose command also matches is not a child job
        parent = str(os.getppid())
        rows_par = [
            (parent, "1", "python3 %s/scripts/selftest.sh" % mk1),
            ("303", parent, "python3 %s/scripts/selftest.sh" % mk1),
        ]
        check("parent-excluded", [c["pid"] for c in matching_children(rows_par, mk1)] == ["303"])

        # future-dated state timestamp is expired, with a warning field
        future_doc = {"nudged": {L["stranded"]: now + 99999}}
        check("future-rate-ok", rate_ok(future_doc, L["stranded"], now) is True)
        save_state(os.path.join(data, STATE_NAME), future_doc)
        if os.path.exists(sent_log):
            os.remove(sent_log)
        code, doc = run(["nudge", "--data", data, "--apply"])
        warned = doc.get("warnings") or []
        lanes = doc.get("lanes") if isinstance(doc.get("lanes"), list) else []
        c1 = next((x for x in lanes if x.get("task") == "C1"), None)
        if c1 is None:
            fails.append("future-shape:" + str(doc)[:400])
        else:
            check("future-sent", any(s.get("pane") == "w1:p3" for s in doc.get("sent", [])))
            check("future-warning", "future-dated" in str(c1.get("warning")) and bool(warned))

        # two lanes sharing one pane: class ambiguous-pane, zero dispatches
        share_a = make_lane("share-a", "w1:p9", "S1", "S1 R1", "^feat", old)
        share_b = make_lane("share-b", "w1:p9", "S2", "S2 R1", "^feat", old)
        slots2 = [
            {"slot": 1, "path": share_a, "state": "busy", "run": "r1", "task": "S1"},
            {"slot": 2, "path": share_b, "state": "busy", "run": "r1", "task": "S2"},
        ]
        lanes2 = os.path.join(root, "lanes-share.json")
        json.dump({"ok": True, "slots": slots2}, open(lanes2, "w"))
        agents2 = [
            {"pane_id": "w1:p9", "agent_status": "idle", "name": "s1-build-r1",
             "cwd": share_a, "state_change_seq": 1},
        ]
        herdr2 = os.path.join(root, "herdr-share.json")
        json.dump({"id": "cli:agent:list", "result": {"agents": agents2}}, open(herdr2, "w"))
        open(os.path.join(root, "pane-w1:p9.txt"), "w").write("I'll wait for the EXIT line.\n")
        env["MK_NUDGE_LANES_JSON"] = lanes2
        env["MK_NUDGE_HERDR_JSON"] = herdr2
        env["MK_NUDGE_SEQ_EPOCH"] = json.dumps({"1": now - 1800})
        data2 = os.path.join(root, "data-share")
        os.makedirs(data2)
        env["MATRIXKODER_HOME"] = data2
        env["MK_HOME"] = data2
        if os.path.exists(sent_log):
            os.remove(sent_log)
        code, doc = run(["scan", "--data", data2])
        classes = sorted(x["class"] for x in doc.get("lanes", []))
        check("share-class", classes == ["ambiguous-pane", "ambiguous-pane"])
        code, doc = run(["nudge", "--data", data2, "--apply"])
        check("share-sent-zero", doc.get("sent") == [])
        check("share-skipped", all(x.get("skipped") == "ambiguous-pane" for x in doc.get("lanes", [])))
        check("share-no-log", not os.path.exists(sent_log))

        # two REAL processes, fake dispatch that records calls and sleeps:
        # exactly one dispatch per stranded lane
        slow = os.path.join(bin_dir, "mk-lane-event.py")
        write_exec(slow, (
            "#!" + sys.executable + "\n"
            "import os, sys, time\n"
            "time.sleep(0.4)\n"
            "pane = sys.argv[sys.argv.index('--pane') + 1]\n"
            "fh = open(os.environ['MK_NUDGE_SENT_LOG'], 'a')\n"
            "fh.write(pane + chr(10))\n"
            "fh.close()\n"
            "sys.stdout.write('{\"ok\": true}' + chr(10))\n"
        ))
        lone = make_lane("lone", "w1:p3", "C1", "C1 R1", "^feat", old)
        # reuse stranded lane path already armed; point lanes json back
        env["MK_NUDGE_LANES_JSON"] = lanes_file
        env["MK_NUDGE_HERDR_JSON"] = herdr_file
        env["MK_NUDGE_SEQ_EPOCH"] = json.dumps(seq_epoch)
        data3 = os.path.join(root, "data-race")
        os.makedirs(data3)
        env["MATRIXKODER_HOME"] = data3
        env["MK_HOME"] = data3
        sent2 = os.path.join(root, "sent-race.log")
        env["MK_NUDGE_SENT_LOG"] = sent2
        procs = []
        for _ in range(2):
            procs.append(subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "nudge", "--data", data3, "--apply"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env))
        outs = [pr.communicate(timeout=30) for pr in procs]
        codes = [pr.returncode for pr in procs]
        log2 = open(sent2).read() if os.path.exists(sent2) else ""
        panes = [ln.strip() for ln in log2.splitlines() if ln.strip()]
        check("race-exit", codes == [0, 0])
        check("race-one-each", sorted(panes) == ["w1:p2", "w1:p3"])
        # restore PATH stubs for anything after (none)
        env["MK_NUDGE_SENT_LOG"] = sent_log
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if fails:
        out({"ok": False, "failed": fails})
        return 1
    # R1.2: the mkdb write guard refuses the real home from this suite too.
    import importlib.util as _iu
    _spec = _iu.spec_from_file_location("mkdb", os.path.join(HERE, "mkdb.py"))
    _mod = _iu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _mod.selftest_guard_probe(nudge_home)
    shutil.rmtree(nudge_home, ignore_errors=True)
    print("mk-lane-nudge self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
