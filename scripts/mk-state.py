#!/usr/bin/env python3
"""MatrixKoder run state — the ONLY writer of <data>/runs/**. Python 3 stdlib only.

  run init --id ID --feature F --branch B [--plan PATH]
  run list | run get [--id ID | ID] | run set [--id ID] [--next TEXT] [--status active|done]
  run stage N [--id ID] | run archive --id ID | run archive ID
  task add --id T --title TEXT [--wave N] [--deps A,B] [--owned glob,glob]   (ids are case-insensitive unique)
  task set T key=value ... [--note TEXT]     keys: status agent model effort lane last_commit gates verdict
  task round T --kind build|fix|takeover [--cause TEXT]
  task get T
  event [--task T] --type transition|decision|verdict|quota|note --text TEXT [--from S] [--to S]  (transition needs --to)
  wait set --question TEXT --options a,b [--rec TEXT] | wait clear
  lock take RES --reason TEXT [--ttl-min 30] [--scope a,b] [--force-stale]
  lock release RES [--force] (refuses a lock taken by another --by) | lock check RES
  render
  --self-test

Common: --data DIR (else $MATRIXKODER_DATA, else <main checkout of cwd's git repo>/.matrixkoder; a linked worktree
resolves to its main checkout, a nested repo to itself), --by WHO (default orchestrator), --run ID for
task/event/wait/render (default: the single active run). Global dir: $MATRIXKODER_HOME or ~/.matrixkoder (locks/).
Every command prints ONE JSON line. Errors: {"ok": false, "error": ...} exit 2; lock refused: exit 1.
run.json is authoritative; events.jsonl may run one line ahead after a crash; `render` rebuilds board.md.
Schema: docs/state-schema.md
"""
import argparse, contextlib, datetime, fcntl, json, math, os, re, socket, sys, tempfile

SCHEMA = 1
STATUSES = ("planned routed dispatched building verifying review fix merged qa done "
            "blocked quota stalled provider").split()
EVENT_TYPES = ("transition", "decision", "verdict", "quota", "note")
TASK_KEYS = ("status", "agent", "model", "effort", "lane", "last_commit", "gates", "verdict")
ROUND_KINDS = ("build", "fix", "takeover")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RECENT, BOARD_EVENTS = 10, 15


class Fail(Exception):
    def __init__(self, msg, code=2, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(message)


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def home():
    return os.path.expanduser(os.environ.get("MATRIXKODER_HOME") or "~/.matrixkoder")


def resolve_data(arg):
    if arg:
        return os.path.abspath(os.path.expanduser(arg))
    if os.environ.get("MATRIXKODER_DATA"):
        return os.path.abspath(os.path.expanduser(os.environ["MATRIXKODER_DATA"]))
    d = os.getcwd()
    while not os.path.exists(os.path.join(d, ".git")):  # nearest repo wins; nested repos must pass --data
        if os.path.dirname(d) == d:
            raise Fail("no git toplevel above cwd; pass --data or set MATRIXKODER_DATA")
        d = os.path.dirname(d)
    return os.path.join(main_worktree(d), ".matrixkoder")


def main_worktree(top):
    """A linked worktree (.git is a file + commondir) shares the main checkout's data dir."""
    try:
        with open(os.path.join(top, ".git")) as f:
            gitdir = f.read().split("gitdir:", 1)[1].strip()
        gitdir = os.path.join(top, gitdir)
        with open(os.path.join(gitdir, "commondir")) as f:
            common = os.path.normpath(os.path.join(gitdir, f.read().strip()))
    except (OSError, IndexError):  # .git is a directory, or a submodule (no commondir)
        return top
    return os.path.dirname(common) if os.path.basename(common) == ".git" else top


def check_id(v, what):
    if not v or not ID_RE.fullmatch(v):
        raise Fail(f"invalid {what} {v!r} (letters, digits, . _ -; max 64)")
    return v


def csv(v):
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def write_json(path, obj):
    """Atomic: write a temp file in the same dir, fsync, os.replace. Readers never see a partial file."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; match board.md / events.jsonl
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


class Store:
    def __init__(self, data):
        self.data = data
        self.runs = os.path.join(data, "runs")
        self.index_path = os.path.join(self.runs, "index.json")

    @contextlib.contextmanager
    def locked(self):
        # ponytail: one flock for the whole data dir; per-run locks only if writers ever contend
        os.makedirs(self.runs, exist_ok=True)
        with open(os.path.join(self.runs, ".lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def run_dir(self, rid):
        return os.path.join(self.runs, rid)

    def task_dir(self, rid, tid):
        return os.path.join(self.runs, rid, "tasks", tid)

    def index(self):
        return read_json(self.index_path, [])

    def load(self, rid=None):
        if rid is None:
            active = [r["id"] for r in self.index() if r.get("status") == "active"]
            if len(active) != 1:
                raise Fail(f"{len(active)} active runs {active}; pass --run/--id")
            rid = active[0]
        run = read_json(os.path.join(self.run_dir(check_id(rid, "run id")), "run.json"))
        if run is None:
            raise Fail(f"no run {rid!r} in {self.runs}")
        return run

    def save(self, run, task=None):
        run["updated"] = now()
        if task is not None:
            write_json(os.path.join(self.task_dir(run["id"], task["id"]), "task.json"), task)
        write_json(os.path.join(self.run_dir(run["id"]), "run.json"), run)
        idx = [r for r in self.index() if r["id"] != run["id"]]
        idx.append({k: run[k] for k in ("id", "feature", "status", "started", "ended")})
        write_json(self.index_path, sorted(idx, key=lambda r: r["started"]))
        self.render(run)

    def event(self, run, type_, text, task=None, frm=None, to=None, by="orchestrator"):
        ev = {"t": now(), "run": run["id"], "task": task, "type": type_, "text": text,
              "from": frm, "to": to, "by": by}
        fd = os.open(os.path.join(self.run_dir(run["id"]), "events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(ev) + "\n").encode())
        finally:
            os.close(fd)
        run["recent"] = (run.get("recent", []) + [ev])[-RECENT:]
        return ev

    def events(self, rid, n):
        try:
            with open(os.path.join(self.run_dir(rid), "events.jsonl")) as f:
                lines = f.read().splitlines()  # ponytail: reads whole log; tail-seek if logs reach MBs
        except FileNotFoundError:
            return []
        out = []
        for x in lines:  # a torn line from a crash mid-append is skipped, not fatal
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(x))
        return out[-n:]

    def render(self, run):
        w = run.get("waiting")
        out = [f"# {run['id']} — {run['feature']}", "",
               f"branch `{run['branch']}` · stage {run['stage']} · {run['status']} · updated {run['updated']}"]
        if run.get("plan"):
            out.append(f"plan: `{run['plan']}`")
        out += ["", f"**Next:** {run.get('next') or '—'}"]
        if w:
            out.append(f"**Waiting on you:** {w['question']} — options: {' | '.join(w['options'])}"
                       + (f" — rec: {w['rec']}" if w.get("rec") else ""))
        out += ["", "## Tasks", "",
                "| Task | Title | Wave | Status | Agent | Model · effort | Round | Lane | Last commit | Verdict |",
                "|---|---|---|---|---|---|---|---|---|---|"]
        for t in run["tasks"]:
            cell = lambda v: "—" if v in (None, "", []) else str(v).replace("|", "\\|")
            me = " · ".join(x for x in (t.get("model"), t.get("effort")) if x)
            out.append("| " + " | ".join(cell(v) for v in (
                t["id"], t["title"], t["wave"], t["status"], t["agent"], me, t["round"], t["lane"],
                t["last_commit"], t["verdict"])) + " |")
        out += ["", f"## Recent events (last {BOARD_EVENTS})", ""]
        for e in self.events(run["id"], BOARD_EVENTS):
            move = f"{e['from'] or ''} → {e['to']}".strip() if e.get("to") else ""
            text = "" if e["text"] == move else e["text"]
            parts = [e["t"], e["task"] or "run", e["type"], move, text]
            out.append("- " + " · ".join(p for p in parts if p) + f" ({e['by']})")
        path = os.path.join(self.run_dir(run["id"]), "board.md")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, path)
        return path


def find_task(run, tid):
    for t in run["tasks"]:
        if t["id"] == tid:
            return t
    raise Fail(f"no task {tid!r} in run {run['id']}")


def parse_value(v):
    if v == "":
        return None
    if v[:1] in "{[":
        try:
            return json.loads(v)
        except json.JSONDecodeError as e:
            raise Fail(f"bad JSON value {v!r}: {e}")
    return v


# ---------- locks (global, cross-session) ----------

def lock_path(res):
    # lower-case file name: APFS is case-insensitive, so "DB" and "db" must be the same lock
    return os.path.join(home(), "locks", check_id(res, "lock resource").lower() + ".lock")


def lock_state(rec):
    if rec is None:
        return "free"
    try:
        expired = datetime.datetime.fromisoformat(rec["expires_at"]) <= datetime.datetime.now().astimezone()
    except (KeyError, TypeError, ValueError):
        return "held"  # unreadable expiry: never assume stale
    return "stale" if expired else "held"


def lock_take(a):
    path = lock_path(a.resource)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    t = datetime.datetime.now().astimezone()
    rec = {"resource": a.resource, "reason": a.reason, "scope": csv(a.scope), "by": a.by,
           "pid": os.getpid(), "host": socket.gethostname(), "taken_at": t.isoformat(timespec="seconds"),
           "expires_at": (t + datetime.timedelta(minutes=a.ttl_min)).isoformat(timespec="seconds")}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.chmod(tmp, 0o644)
    try:
        for attempt in (0, 1, 2):
            try:
                os.link(tmp, path)  # exclusive create (EEXIST if held) with complete content, never half-written
                return {"ok": True, "state": "held", "lock": rec, "path": path}
            except FileExistsError:
                cur = read_json(path)
                state = lock_state(cur)
                if state == "free" and attempt < 2:  # released between link and read: try again
                    continue
                if state == "stale" and a.force_stale and attempt == 0:
                    # ponytail: two simultaneous --force-stale callers can both win; forcing is a deliberate act
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(path)
                    continue
                raise Fail(f"lock {a.resource} is {state}" + (" (use --force-stale)" if state == "stale" else ""),
                           code=1, state=state, lock=cur)
    finally:
        os.unlink(tmp)


# ---------- commands ----------

def cmd_run(a, st):
    if a.action == "list":
        return {"ok": True, "runs": st.index()}
    if a.action == "init":
        rid = check_id(a.run, "run id")
        if not (a.feature and a.branch):
            raise Fail("run init needs --id --feature --branch")
        with st.locked():
            if os.path.exists(os.path.join(st.run_dir(rid), "run.json")):
                raise Fail(f"run {rid} exists")
            os.makedirs(os.path.join(st.run_dir(rid), "tasks"), exist_ok=True)
            run = {"schema": SCHEMA, "id": rid, "feature": a.feature, "branch": a.branch, "plan": a.plan,
                   "status": "active", "stage": 0, "next": None, "waiting": None,
                   "started": now(), "ended": None, "updated": None, "tasks": [], "recent": []}
            st.event(run, "note", f"run init: {a.feature} on {a.branch}")
            st.save(run)
        return {"ok": True, "run": run}
    a.run = a.run or getattr(a, "pos_id", None)
    if a.action == "get":
        return {"ok": True, "run": st.load(a.run)}
    if a.action == "archive" and not a.run:
        raise Fail("run archive needs --id")
    with st.locked():
        run = st.load(a.run)
        if a.action == "set":
            if a.next is None and a.status is None:
                raise Fail("run set needs --next and/or --status")
            if a.next is not None:
                run["next"] = a.next
            if a.status:
                set_run_status(st, run, a.status)
        elif a.action == "stage":
            if a.n is None or a.n < 0:
                raise Fail("run stage needs N >= 0")
            run["stage"] = a.n
            st.event(run, "note", f"stage {a.n}")
        else:  # archive
            set_run_status(st, run, "archived")
        st.save(run)
    return {"ok": True, "run": run}


def set_run_status(st, run, status):
    if status != run["status"]:
        st.event(run, "note", f"run {run['status']} → {status}")
        run["status"] = status
        run["ended"] = None if status == "active" else (run.get("ended") or now())


def cmd_task(a, st):
    with st.locked():
        run = st.load(a.run)
        if a.action == "get":
            t = find_task(run, a.task)
            return {"ok": True, "task": t, "dir": st.task_dir(run["id"], t["id"])}
        if a.action == "add":
            tid = check_id(a.id, "task id")
            clash = [t["id"] for t in run["tasks"] if t["id"].lower() == tid.lower()]
            if clash:  # case-insensitive: task folders share one path on APFS
                raise Fail(f"task {tid} exists" + (f" as {clash[0]}" if clash[0] != tid else ""))
            if not a.title:
                raise Fail("task add needs --title")
            t = {"id": tid, "title": a.title, "wave": a.wave, "deps": csv(a.deps), "owned": csv(a.owned),
                 "status": "planned", "agent": None, "model": None, "effort": None, "lane": None,
                 "last_commit": None, "gates": None, "verdict": None, "round": 0, "rounds": [],
                 "created": now(), "updated": now()}
            run["tasks"].append(t)
            st.event(run, "transition", f"task added: {a.title}", task=tid, to="planned", by=a.by)
            st.save(run, t)
            return {"ok": True, "task": t}
        t = find_task(run, a.task)
        cur = t["rounds"][-1] if t["rounds"] else None
        if a.action == "set":
            changes = {}
            for pair in a.pairs:
                k, sep, v = pair.partition("=")
                if not sep or k not in TASK_KEYS:
                    raise Fail(f"bad pair {pair!r}; keys: {' '.join(TASK_KEYS)}")
                changes[k] = parse_value(v)
            if "status" in changes and changes["status"] not in STATUSES:
                raise Fail(f"bad status {changes['status']!r}; one of: {' '.join(STATUSES)}")
            frm = t["status"]
            t.update(changes)
            if cur and "verdict" in changes:
                cur["verdict"] = changes["verdict"]
            if cur and "last_commit" in changes:
                cur["sha"] = changes["last_commit"]
            if "status" in changes and changes["status"] != frm:
                st.event(run, "transition", a.note or f"{frm} → {t['status']}", task=t["id"], frm=frm,
                         to=t["status"], by=a.by)
            elif a.note:
                st.event(run, "note", a.note, task=t["id"], by=a.by)
        else:  # round
            if a.kind not in ROUND_KINDS:
                raise Fail(f"bad kind {a.kind!r}; one of: {' '.join(ROUND_KINDS)}")
            n = t["round"] + 1
            t["round"] = n
            t["rounds"].append({"n": n, "kind": a.kind, "cause": a.cause, "verdict": None, "sha": None,
                                "started": now()})
            st.event(run, "note", f"round {n} ({a.kind})" + (f": {a.cause}" if a.cause else ""),
                     task=t["id"], by=a.by)
        t["updated"] = now()
        st.save(run, t)
        if a.action == "round":
            d = st.task_dir(run["id"], t["id"])
            return {"ok": True, "task": t["id"], "round": t["round"], "kind": a.kind,
                    **{k: os.path.join(d, f"{k}-r{t['round']}.md") for k in ("brief", "report", "review")}}
        return {"ok": True, "task": t}


def cmd_event(a, st):
    if a.type not in EVENT_TYPES:
        raise Fail(f"bad event type {a.type!r}; one of: {' '.join(EVENT_TYPES)}")
    if not a.text.strip():
        raise Fail("event needs a non-empty --text")
    if a.type == "transition" and not a.to:
        raise Fail("transition event needs --to")
    for s in (a.from_, a.to):
        if s is not None and s not in STATUSES:
            raise Fail(f"bad status {s!r}")
    with st.locked():
        run = st.load(a.run)
        if a.task:
            find_task(run, a.task)
        ev = st.event(run, a.type, a.text, task=a.task, frm=a.from_, to=a.to, by=a.by)
        st.save(run)
    return {"ok": True, "event": ev}


def cmd_wait(a, st):
    with st.locked():
        run = st.load(a.run)
        if a.action == "set":
            if not a.question:
                raise Fail("wait set needs --question")
            run["waiting"] = {"question": a.question, "options": csv(a.options), "rec": a.rec, "since": now()}
            st.event(run, "note", f"waiting on user: {a.question}", by=a.by)
        else:
            run["waiting"] = None
            st.event(run, "note", "wait cleared", by=a.by)
        st.save(run)
    return {"ok": True, "waiting": run["waiting"]}


def cmd_lock(a, st):
    if a.action == "take":
        if not a.reason:
            raise Fail("lock take needs --reason")
        if not (math.isfinite(a.ttl_min) and a.ttl_min >= 0):
            raise Fail("--ttl-min must be a finite number >= 0")
        return lock_take(a)
    path = lock_path(a.resource)
    if a.action == "release":
        cur = read_json(path)
        if cur is None:
            return {"ok": True, "released": False}
        if cur.get("by") != a.by and not a.force:
            raise Fail(f"lock {a.resource} is held by {cur.get('by')!r}, not {a.by!r} (use --force)",
                       code=1, state=lock_state(cur), lock=cur)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        return {"ok": True, "released": True}
    cur = read_json(path)
    return {"ok": True, "state": lock_state(cur), "lock": cur}


def cmd_render(a, st):
    with st.locked():
        return {"ok": True, "board": st.render(st.load(a.run))}


def build_parser():
    # --data/--by/--run parse before AND after the subcommand: top-level copies keep
    # the defaults, subparser copies SUPPRESS so they never clobber an earlier value.
    top_p = Parser(add_help=False)
    top_p.add_argument("--data")
    top_p.add_argument("--by", default="orchestrator")
    top_p.add_argument("--run")
    data_p = Parser(add_help=False)
    data_p.add_argument("--data", default=argparse.SUPPRESS)
    data_p.add_argument("--by", default=argparse.SUPPRESS)
    run_p = Parser(add_help=False)
    run_p.add_argument("--run", default=argparse.SUPPRESS)
    p = Parser(prog="mk-state.py", parents=[top_p])
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run").add_subparsers(dest="action", required=True)
    for name in ("init", "list", "get", "set", "stage", "archive"):
        r = run.add_parser(name, parents=[data_p])
        r.add_argument("--id", "--run", dest="run", default=argparse.SUPPRESS)
        r.set_defaults(func=cmd_run)
    for name in ("get", "archive"):  # `run get r1` works as well as `run get --id r1`
        run.choices[name].add_argument("pos_id", nargs="?")
    r = run.choices["init"]
    r.add_argument("--feature"), r.add_argument("--branch"), r.add_argument("--plan")
    run.choices["set"].add_argument("--next")
    run.choices["set"].add_argument("--status", choices=("active", "done"))
    run.choices["stage"].add_argument("n", type=int, nargs="?")

    task = sub.add_parser("task").add_subparsers(dest="action", required=True)
    t = task.add_parser("add", parents=[data_p, run_p])
    t.add_argument("--id"), t.add_argument("--title"), t.add_argument("--wave", type=int)
    t.add_argument("--deps"), t.add_argument("--owned")
    t = task.add_parser("set", parents=[data_p, run_p])
    t.add_argument("task"), t.add_argument("pairs", nargs="+"), t.add_argument("--note")
    t = task.add_parser("round", parents=[data_p, run_p])
    t.add_argument("task"), t.add_argument("--kind", required=True), t.add_argument("--cause")
    t = task.add_parser("get", parents=[data_p, run_p])
    t.add_argument("task")
    for t in task.choices.values():
        t.set_defaults(func=cmd_task)

    e = sub.add_parser("event", parents=[data_p, run_p])
    e.add_argument("--task"), e.add_argument("--type", required=True), e.add_argument("--text", required=True)
    e.add_argument("--from", dest="from_"), e.add_argument("--to")
    e.set_defaults(func=cmd_event)

    w = sub.add_parser("wait").add_subparsers(dest="action", required=True)
    ws = w.add_parser("set", parents=[data_p, run_p])
    ws.add_argument("--question"), ws.add_argument("--options"), ws.add_argument("--rec")
    w.add_parser("clear", parents=[data_p, run_p])
    for x in w.choices.values():
        x.set_defaults(func=cmd_wait)

    lk = sub.add_parser("lock").add_subparsers(dest="action", required=True)
    for name in ("take", "release", "check"):
        x = lk.add_parser(name, parents=[data_p])
        x.add_argument("resource")
        x.set_defaults(func=cmd_lock)
    x = lk.choices["take"]
    x.add_argument("--reason"), x.add_argument("--ttl-min", type=float, default=30), x.add_argument("--scope")
    x.add_argument("--force-stale", action="store_true")
    lk.choices["release"].add_argument("--force", action="store_true")

    sub.add_parser("render", parents=[data_p, run_p]).set_defaults(func=cmd_render)
    return p


def main(argv):
    try:
        a = build_parser().parse_args(argv)
        st = None if a.cmd == "lock" else Store(resolve_data(a.data))
        out, code = a.func(a, st), 0
    except Fail as e:
        out, code = {"ok": False, "error": str(e), **e.extra}, e.code
    except (OSError, ValueError) as e:  # corrupt JSON (ValueError), unreadable files: still one JSON line
        out, code = {"ok": False, "error": f"{type(e).__name__}: {e}"}, 2
    print(json.dumps(out))
    return code


# ---------- self-test ----------

def self_test():
    import shutil, subprocess
    tmp = tempfile.mkdtemp(prefix="mk-state-test-")
    env = dict(os.environ, MATRIXKODER_HOME=os.path.join(tmp, "home"))
    env.pop("MATRIXKODER_DATA", None)
    data = os.path.join(tmp, "data")

    def mk(*args, code=0, cwd=None, e=None, data_arg=True):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, *(["--data", data] if data_arg else [])],
                           capture_output=True, text=True, env=e or env, cwd=cwd or tmp)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    try:
        rdir = os.path.join(data, "runs", "r1")
        out = mk("run", "init", "--id", "r1", "--feature", "Feat", "--branch", "feat/x", "--plan", "plans/f.md")
        assert out["ok"] and out["run"]["status"] == "active"
        for f in ("run.json", "events.jsonl", "board.md"):
            assert os.path.exists(os.path.join(rdir, f)), f
        assert mk("run", "init", "--id", "r1", "--feature", "F", "--branch", "b", code=2)["ok"] is False
        assert mk("run", "init", "--id", "../evil", "--feature", "F", "--branch", "b", code=2)["ok"] is False
        assert [r["id"] for r in mk("run", "list")["runs"]] == ["r1"]

        mk("task", "add", "--id", "T1", "--title", "State core", "--wave", "1")
        t2 = mk("task", "add", "--id", "T2", "--title", "Usage", "--wave", "2", "--deps", "T1", "--owned", "a/*,b.py")["task"]
        assert t2["deps"] == ["T1"] and t2["owned"] == ["a/*", "b.py"] and t2["status"] == "planned"
        assert mk("task", "add", "--id", "T1", "--title", "dup", code=2)["ok"] is False
        assert os.path.exists(os.path.join(rdir, "tasks", "T2", "task.json"))

        assert "bad status" in mk("task", "set", "T1", "status=bogus", code=2)["error"]
        assert mk("task", "set", "T1", "colour=red", code=2)["ok"] is False
        t1 = mk("task", "set", "T1", "status=routed", "agent=codex", "model=gpt-5", "effort=high",
                'gates={"test":"pass"}', "--by", "lane-watch")["task"]
        assert t1["status"] == "routed" and t1["gates"] == {"test": "pass"}
        r1 = mk("task", "round", "T1", "--kind", "build")
        assert r1["round"] == 1 and r1["brief"].endswith(os.path.join("tasks", "T1", "brief-r1.md"))
        r2 = mk("task", "round", "T1", "--kind", "fix", "--cause", "brief gap")
        assert r2["round"] == 2 and r2["review"].endswith("review-r2.md") and r2["report"].endswith("report-r2.md")
        assert mk("task", "round", "T1", "--kind", "nap", code=2)["ok"] is False
        t1 = mk("task", "set", "T1", "verdict=approve", "last_commit=abc1234")["task"]
        assert t1["rounds"][-1] == {**t1["rounds"][-1], "n": 2, "kind": "fix", "cause": "brief gap",
                                    "verdict": "approve", "sha": "abc1234"}
        assert mk("task", "get", "T1")["task"]["round"] == 2
        assert mk("task", "get", "NOPE", code=2)["ok"] is False

        ev = mk("event", "--task", "T1", "--type", "decision", "--text", "route codex high")["event"]
        assert ev["type"] == "decision" and ev["task"] == "T1"
        assert mk("event", "--type", "gossip", "--text", "x", code=2)["ok"] is False
        assert mk("event", "--task", "T9", "--type", "note", "--text", "x", code=2)["ok"] is False
        assert mk("event", "--type", "transition", "--text", "x", "--from", "building", "--to", "zzz", code=2)["ok"] is False

        w = mk("wait", "set", "--question", "Merge now?", "--options", "yes,no", "--rec", "yes")["waiting"]
        assert w["options"] == ["yes", "no"] and w["rec"] == "yes"
        mk("run", "set", "--next", "dispatch T2")
        mk("run", "stage", "3")
        run = mk("run", "get")["run"]
        assert run["next"] == "dispatch T2" and run["stage"] == 3 and run["waiting"]["question"] == "Merge now?"
        assert "Merge now?" in open(os.path.join(rdir, "board.md")).read()
        assert mk("wait", "clear")["waiting"] is None

        events = [json.loads(x) for x in open(os.path.join(rdir, "events.jsonl"))]
        tr = [e for e in events if e["type"] == "transition" and e["task"] == "T1" and e["from"]]
        assert tr and tr[0]["from"] == "planned" and tr[0]["to"] == "routed" and tr[0]["by"] == "lane-watch"
        run = mk("run", "get")["run"]
        assert len(events) >= RECENT and run["recent"] == events[-RECENT:], (len(events), len(run["recent"]))
        assert [t["id"] for t in run["tasks"]] == ["T1", "T2"]
        assert read_json(os.path.join(rdir, "tasks", "T1", "task.json")) == run["tasks"][0]

        for i in range(6):  # push the log past the board's 15-event window
            mk("event", "--type", "note", "--text", f"filler {i}")
        assert mk("run", "get")["run"]["recent"][-1]["text"] == "filler 5"
        board = mk("render")["board"]
        txt = open(board).read()
        assert "| T1 |" in txt and "| T2 |" in txt and "route codex high" in txt
        assert len([x for x in txt.splitlines() if x.startswith("- ")]) == BOARD_EVENTS

        # default-run selection: ambiguous with two active runs, fine again after archive
        mk("run", "init", "--id", "r2", "--feature", "Other", "--branch", "b2")
        assert "active runs" in mk("task", "get", "T1", code=2)["error"]
        assert mk("task", "get", "T1", "--run", "r1")["task"]["id"] == "T1"
        assert mk("run", "archive", "--id", "r2")["run"]["status"] == "archived"
        assert {r["id"]: r["status"] for r in mk("run", "list")["runs"]} == {"r1": "active", "r2": "archived"}
        assert mk("run", "archive", code=2)["ok"] is False
        assert mk("task", "get", "T1")["task"]["id"] == "T1"
        assert mk("run", "set", "--status", "done")["run"]["ended"]
        assert mk("run", "set", "--id", "r1", "--status", "active")["run"]["ended"] is None  # reopen clears ended
        assert mk("run", "get", "r1")["run"]["id"] == "r1" and mk("run", "get", "--id", "r1")["run"]["id"] == "r1"

        # review r1: case-insensitive task ids, input validation, one JSON line on corrupt files
        assert "exists as T1" in mk("task", "add", "--id", "t1", "--title", "lower", code=2)["error"]
        assert mk("task", "get", "T1")["task"]["title"] == "State core"
        assert mk("event", "--type", "note", "--text", "  ", code=2)["ok"] is False
        assert mk("event", "--type", "transition", "--text", "x", code=2)["ok"] is False
        assert mk("run", "stage", "-1", code=2)["ok"] is False
        for ttl in ("nan", "inf", "-5"):
            assert mk("lock", "take", "x", "--reason", "r", "--ttl-min", ttl, code=2)["ok"] is False
        for f in ("run.json", "events.jsonl", "board.md", os.path.join("tasks", "T1", "task.json")):
            assert os.stat(os.path.join(rdir, f)).st_mode & 0o777 == 0o644, f
        with open(os.path.join(rdir, "events.jsonl"), "a") as f:
            f.write('{"t": "torn')  # crash mid-append
        assert mk("render")["ok"]
        with open(os.path.join(rdir, "events.jsonl"), "a") as f:
            f.write("\n")
        bad = os.path.join(data, "runs", "r2", "run.json")
        good = open(bad).read()
        open(bad, "w").write("{not json")
        assert "JSONDecodeError" in mk("run", "get", "--id", "r2", code=2)["error"]
        open(bad, "w").write(good)

        # concurrent writers: flock serialises read-modify-write, no lost event or update
        procs = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "task", "set", "T2", f"agent=a{i}",
                                   "--note", f"race {i}", "--data", data], env=env, stdout=subprocess.PIPE, text=True)
                 for i in range(10)]
        assert all(p.wait() == 0 for p in procs)
        races = [json.loads(x) for x in open(os.path.join(rdir, "events.jsonl")) if '"race ' in x]
        assert len(races) == 10 and mk("task", "get", "T2")["task"]["agent"] in {f"a{i}" for i in range(10)}
        assert len([e for e in mk("run", "get")["run"]["recent"] if e["text"].startswith("race ")]) == 10

        # locks live in MATRIXKODER_HOME, exclusive, expire to stale
        assert mk("lock", "take", "dev-server", "--reason", "QA", "--scope", "restart,deploy")["state"] == "held"
        assert os.path.exists(os.path.join(tmp, "home", "locks", "dev-server.lock"))
        held = mk("lock", "take", "dev-server", "--reason", "other", code=1)
        assert held["state"] == "held" and held["lock"]["reason"] == "QA" and held["lock"]["scope"] == ["restart", "deploy"]
        assert mk("lock", "check", "dev-server")["state"] == "held"
        assert mk("lock", "release", "dev-server")["released"] is True
        assert mk("lock", "check", "dev-server")["state"] == "free"
        assert mk("lock", "release", "dev-server")["released"] is False
        mk("lock", "take", "db", "--reason", "migrate", "--ttl-min", "0")
        assert mk("lock", "check", "db")["state"] == "stale"
        assert mk("lock", "take", "db", "--reason", "again", code=1)["state"] == "stale"
        assert mk("lock", "take", "db", "--reason", "again", "--force-stale")["lock"]["reason"] == "again"
        assert mk("lock", "check", "db")["state"] == "held"
        assert mk("lock", "take", "../etc", "--reason", "x", code=2)["ok"] is False
        assert mk("lock", "take", "DB", "--reason", "case", code=1)["state"] == "held"  # same file as "db"
        other = mk("lock", "release", "db", "--by", "lane-watch", code=1)
        assert other["ok"] is False and other["lock"]["by"] == "orchestrator"
        assert mk("lock", "release", "db", "--by", "lane-watch", "--force")["released"] is True
        mk("lock", "take", "web", "--reason", "x")
        assert os.stat(os.path.join(tmp, "home", "locks", "web.lock")).st_mode & 0o777 == 0o644

        # atomic write: a failing write leaves the old file intact and no temp file behind
        p = os.path.join(tmp, "atomic", "x.json")
        write_json(p, {"a": 1})
        try:
            write_json(p, {"bad": object()})
            raise AssertionError("expected TypeError")
        except TypeError:
            pass
        assert read_json(p) == {"a": 1} and os.listdir(os.path.dirname(p)) == ["x.json"]
        for root, _, files in os.walk(data):
            assert not [f for f in files if f.startswith(".tmp-") or f.endswith(".tmp")], (root, files)

        # data dir resolution: --data > $MATRIXKODER_DATA > <git toplevel>/.matrixkoder
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        os.makedirs(os.path.join(repo, "sub", "deep"))
        mk("run", "init", "--id", "g1", "--feature", "F", "--branch", "b", data_arg=False, cwd=os.path.join(repo, "sub", "deep"))
        assert os.path.exists(os.path.join(repo, ".matrixkoder", "runs", "g1", "run.json"))
        envd = dict(env, MATRIXKODER_DATA=os.path.join(tmp, "envdata"))
        mk("run", "init", "--id", "e1", "--feature", "F", "--branch", "b", data_arg=False, e=envd, cwd=repo)
        assert os.path.exists(os.path.join(tmp, "envdata", "runs", "e1", "run.json"))
        mk("run", "init", "--id", "d1", "--feature", "F", "--branch", "b", e=envd, cwd=repo)
        assert os.path.exists(os.path.join(rdir, "..", "d1", "run.json"))
        # a linked worktree (.git file + commondir) resolves to the main checkout's data dir
        wt_git = os.path.join(repo, ".git", "worktrees", "lane")
        os.makedirs(wt_git)
        open(os.path.join(wt_git, "commondir"), "w").write("../..\n")
        lane = os.path.join(tmp, "repo-worktree-lane")
        os.makedirs(lane)
        open(os.path.join(lane, ".git"), "w").write(f"gitdir: {wt_git}\n")
        mk("run", "init", "--id", "w1", "--feature", "F", "--branch", "b", data_arg=False, cwd=lane)
        assert os.path.exists(os.path.join(repo, ".matrixkoder", "runs", "w1", "run.json"))
        assert not os.path.exists(os.path.join(lane, ".matrixkoder"))

        # global --data/--by/--run also parse before the subcommand (after: every mk above)
        mk("--data", data, "run", "init", "--id", "f1", "--feature", "F", "--branch", "b", data_arg=False)
        assert os.path.exists(os.path.join(data, "runs", "f1", "run.json"))
        front = mk("--data", data, "--by", "front", "--run", "r1", "event", "--type", "note",
                   "--text", "flags before the subcommand", data_arg=False)["event"]
        assert front["by"] == "front"  # subparser SUPPRESS keeps the earlier --by
        assert mk("--data", data, "--run", "r1", "task", "get", "T1", data_arg=False)["task"]["id"] == "T1"
        print("mk-state self-test ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        sys.exit(main(sys.argv[1:]))
