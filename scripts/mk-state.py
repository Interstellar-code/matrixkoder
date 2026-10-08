#!/usr/bin/env python3
"""MatrixKoder run state — the ONLY writer of <data>/runs/**. Python 3 stdlib only.

  run init --id ID --feature F --branch B [--plan PATH]
  run list | run get [--id ID | ID] | run set [--id ID] [--next TEXT] [--status active|done]
  run stage N [--id ID] | run archive --id ID | run archive ID
  task add --id T --title TEXT [--wave N] [--deps A,RUN:B] [--owned glob,glob] [--parent P]
      ids are case-insensitive unique; deps store the real id case and a self-dep is refused;
      RUN:B is a task in another run of the same data dir; --parent onto merged/qa/done is refused;
      warns when an owned glob overlaps an unmerged task in any active run
  task set T key=value ... [--note TEXT] [--force]   keys: status agent model effort lane last_commit gates verdict
      status=dispatched|building needs every own AND ancestor dep merged/qa/done (--force overrides);
      status= on a parent is refused (it rolls up from children); a task whose deps are all met gets
      ready=true and a "deps met" event; an unreadable sibling run only produces a warnings entry
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
import argparse, contextlib, datetime, fcntl, fnmatch, json, math, os, re, socket, sys, tempfile

SCHEMA = 1
STATUSES = ("planned routed dispatched building verifying review fix merged qa done "
            "blocked quota stalled provider").split()
EVENT_TYPES = ("transition", "decision", "verdict", "quota", "note")
TASK_KEYS = ("status", "agent", "model", "effort", "lane", "last_commit", "gates", "verdict")
ROUND_KINDS = ("build", "fix", "takeover")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RECENT, BOARD_EVENTS = 10, 15
DEP_MET = ("merged", "qa", "done")
DEP_GATED = ("dispatched", "building")
SIDE = ("blocked", "provider", "quota", "stalled")  # roll-up precedence: the first present wins


class Fail(Exception):
    def __init__(self, msg, code=2, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


class Parser(argparse.ArgumentParser):
    def __init__(self, *a, **kw):
        kw.setdefault("allow_abbrev", False)  # no --fo/--b=evil prefix matching, anywhere
        super().__init__(*a, **kw)

    def error(self, message):
        raise Fail(message)


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def home():
    return os.path.expanduser(os.environ.get("MATRIXKODER_HOME") or "~/.matrixkoder")


def resolve_data(arg):
    """--data > $MATRIXKODER_DATA > <home>/projects.json entry for the repo > <main checkout>/.matrixkoder.
    The repo is the nearest .git above cwd (never $HOME itself); a linked worktree maps to its main checkout."""
    if arg:
        return os.path.abspath(os.path.expanduser(arg))
    if os.environ.get("MATRIXKODER_DATA"):
        return os.path.abspath(os.path.expanduser(os.environ["MATRIXKODER_DATA"]))
    d, stop = os.path.realpath(os.getcwd()), os.path.realpath(os.path.expanduser("~"))
    while not os.path.exists(os.path.join(d, ".git")):  # nearest repo wins; nested repos must pass --data
        if os.path.dirname(d) == d or d == stop:
            raise Fail("no git toplevel above cwd (the walk stops before $HOME); pass --data or set MATRIXKODER_DATA")
        d = os.path.dirname(d)
    if d == stop:
        raise Fail("cwd is $HOME's own repo, not a project; pass --data or set MATRIXKODER_DATA")
    top = main_worktree(d)
    try:  # the wizard's home mode: ~/.matrixkoder/projects.json {"<repo>": {"data_dir": ...}}
        with open(os.path.join(home(), "projects.json")) as f:
            projects = json.load(f)
        entry = projects.get(top) or projects.get(os.path.realpath(top))
        listed = entry.get("data_dir") if isinstance(entry, dict) else None
    except (OSError, ValueError, AttributeError):
        listed = None
    if isinstance(listed, str) and listed.strip():
        return os.path.normpath(os.path.join(top, os.path.expanduser(listed.strip())))
    return os.path.join(top, ".matrixkoder")


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

    def load(self, rid=None, task=None):
        if rid is None:
            active = [r["id"] for r in self.index() if r.get("status") == "active"]

            def owns(r):  # a corrupt sibling must not crash the pick
                try:
                    src = read_json(os.path.join(self.run_dir(r), "run.json"))
                except (OSError, ValueError):
                    return False
                return isinstance(src, dict) and any(
                    t.get("id") == task for t in src.get("tasks") or [] if isinstance(t, dict))
            if len(active) > 1 and task:  # several active runs: the one that owns the task wins
                owners = [r for r in active if owns(r)]
                active = owners if len(owners) == 1 else active
            if len(active) != 1:
                raise Fail(f"{len(active)} active runs {active}; pass --run/--id")
            rid = active[0]
        run = read_json(os.path.join(self.run_dir(check_id(rid, "run id")), "run.json"))
        if run is None:
            raise Fail(f"no run {rid!r} in {self.runs}")
        return run

    def save(self, run, task=None, by="orchestrator"):
        rollup(self, run, by)
        self.refresh_ready(run, by)
        run["updated"] = now()
        if task is not None:
            write_json(os.path.join(self.task_dir(run["id"], task["id"]), "task.json"), task)
        write_json(os.path.join(self.run_dir(run["id"]), "run.json"), run)
        idx = [r for r in self.index() if r["id"] != run["id"]]
        idx.append({k: run[k] for k in ("id", "feature", "status", "started", "ended")})
        write_json(self.index_path, sorted(idx, key=lambda r: r["started"]))
        self.render(run)

    def dep_status(self, run, dep):
        """Status of a dep ("T" in this run, "RUN:T" in another run of this data dir), None if unknown.
        Run and task ids match case-insensitively (APFS); a same-run RUN:T reads the in-memory run."""
        rid, _, tid = dep.rpartition(":")
        if rid.lower() in ("", run["id"].lower()):
            src = run
        else:
            try:
                src = read_json(os.path.join(self.run_dir(rid), "run.json")) or {}
            except (OSError, ValueError):
                return None  # unreadable dep target: unknown, so unmet — never fatal
        return next((t.get("status") for t in src.get("tasks") or []
                     if isinstance(t, dict) and str(t.get("id") or "").lower() == tid.lower()), None)

    def unmet(self, run, t):
        return [d for d in t.get("deps") or [] if self.dep_status(run, d) not in DEP_MET]

    def unmet_tree(self, run, t):
        """The task's own unmet deps plus every ancestor's (a parent's dep gates its children)."""
        out, p = list(self.unmet(run, t)), t.get("parent")
        seen = set()
        while p and p not in seen:
            seen.add(p)
            pt = next((x for x in run["tasks"] if x.get("id") == p), None)
            if pt is None:
                break
            out += [d for d in self.unmet(run, pt) if d not in out]
            p = pt.get("parent")
        return out

    def norm_dep(self, run, new_tid, dep):
        """Canonical form of a dep at task add: the stored case of the run id and task id (ids are
        case-insensitive). Unknown ids stay as typed; a self-dep (any case, same run) is rejected."""
        rid, _, did = dep.rpartition(":")
        check_id(did, "dep task id")
        if rid:
            check_id(rid, "dep run id")
        runs = {r["id"].lower(): r["id"] for r in self.index() if r.get("id")}
        if rid.lower() in runs:
            rid = runs[rid.lower()]
        same = rid.lower() in ("", run["id"].lower())
        if same and did.lower() == new_tid.lower():
            raise Fail(f"task {new_tid} cannot depend on itself")
        if same:
            src = run
        else:
            try:
                src = read_json(os.path.join(self.run_dir(rid), "run.json")) or {}
            except (OSError, ValueError):
                src = {}
        hit = next((t.get("id") for t in (src.get("tasks") or [])
                    if isinstance(t, dict) and str(t.get("id") or "").lower() == did.lower()), None)
        return (rid + ":" if rid else "") + (hit or did)

    def active_runs(self, run):
        """-> ([run, *other active runs], warnings): the given run first, the other active runs
        loaded. An unreadable or non-object sibling is skipped and named in warnings, never fatal."""
        others, warnings = [], []
        for r in self.index():
            if r.get("status") != "active" or r.get("id") in (None, run.get("id")):
                continue
            try:
                x = read_json(os.path.join(self.run_dir(r["id"]), "run.json"))
            except (OSError, ValueError) as e:
                warnings.append(f"active run {r['id']} unreadable, skipped: {type(e).__name__}")
                continue
            if isinstance(x, dict) and x:
                others.append(x)
        return [run] + others, warnings

    def refresh_ready(self, run, by="orchestrator"):
        """ready = has deps, not started, all deps met. A flip to ready logs a "deps met" event. -> changed?"""
        changed = False
        for t in run.get("tasks") or []:
            ready = bool(t.get("deps")) and t.get("status") in ("planned", "routed") and not self.unmet(run, t)
            if ready != bool(t.get("ready")):
                t["ready"], changed = ready, True
                if ready:
                    self.event(run, "note", f"deps met ({', '.join(map(str, t.get('deps') or []))}): "
                                "ready to dispatch", task=t.get("id"), by=by)
        return changed

    def refresh_others(self, run, by="orchestrator"):
        """Cross-run upkeep after a save that may change a status (task add and task set): refresh
        `ready` and the board of every other active run — a merge here can make their tasks ready,
        and their boards show "blocks" markers. An unreadable sibling is skipped and warned. -> warnings"""
        runs, warnings = self.active_runs(run)
        for o in runs[1:]:
            try:
                if self.refresh_ready(o, by):
                    self.save(o, by=by)
                else:
                    self.render(o)
            except (OSError, ValueError) as e:
                warnings.append(f"run {o.get('id')} not refreshed: {type(e).__name__}")
        return warnings

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
                "| Task | Title | Wave | Status | Deps | Agent | Model · effort | Round | Lane | Last commit | Verdict |",
                "|---|---|---|---|---|---|---|---|---|---|---|"]
        active, _ = self.active_runs(run)  # an unreadable sibling is skipped here; render stays ok
        for t, depth in tree(run["tasks"]):
            cell = lambda v: "—" if v in (None, "", []) else str(v).replace("|", "\\|")
            me = " · ".join(x for x in (t.get("model"), t.get("effort")) if x)
            deps = [f"{d} {'✓' if self.dep_status(run, d) in DEP_MET else '…'}" for d in t.get("deps") or []]
            deps += [f"blocks {o.get('id')}:{x.get('id')}" for o in active[1:] for x in o.get("tasks") or []
                     if x.get("id") and deps_include(x.get("deps"), f"{run['id']}:{t.get('id')}")
                     and t.get("status") not in DEP_MET]
            status = (t.get("status") or "?") + (" · ready" if t.get("ready") else "")
            out.append("| " + " | ".join(cell(v) for v in (
                "↳ " * depth + (t.get("id") or "?"), t.get("title"), t.get("wave"), status, ", ".join(deps),
                t.get("agent"), me, t.get("round"), t.get("lane"), t.get("last_commit"),
                t.get("verdict"))) + " |")
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


def tree(tasks):
    """-> [(task, depth)]: roots in insertion order, each followed by its children (indented on the
    board). A task without an id (a corrupt hand-edit) is listed but never walked into or through."""
    kids = {}
    for t in tasks:
        kids.setdefault(t.get("parent"), []).append(t)
    out, ids = [], {t["id"] for t in tasks if t.get("id")}

    def walk(t, d):
        out.append((t, d))
        if not t.get("id"):
            return
        for c in kids.get(t["id"], []):
            walk(c, d + 1)
    for t in tasks:
        if t.get("parent") not in ids:  # a root (or an orphan whose parent vanished)
            walk(t, 0)
    return out


def rollup(st, run, by):
    """A parent's status follows its children: a side state if any child is in one, else the least
    advanced child — except that once any child is past `routed` and not all are met, the parent is
    `building` (work started; a parent must not look less advanced than its children)."""
    order = {s: i for i, s in enumerate(STATUSES)}
    for t, _ in reversed(tree(run.get("tasks") or [])):  # leaves first, so nested parents see rolled-up children
        if not t.get("id"):
            continue
        kids = [c.get("status") for c in run["tasks"] if c.get("parent") == t["id"]]
        if not kids:
            continue
        new = next((s for s in SIDE if s in kids), None)
        if new is None:
            started = any(order.get(k, 0) > order["routed"] for k in kids)  # unknown status: least advanced
            new = "building" if started and not all(k in DEP_MET for k in kids) else min(
                kids, key=lambda s: order.get(s, 0))
        if new != t.get("status"):
            st.event(run, "transition", f"roll-up from children: {t.get('status')} → {new}", task=t["id"],
                     frm=t.get("status"), to=new, by=by)
            t["status"], t["updated"] = new, now()


def norm_glob(g):
    while g.startswith("./"):
        g = g[2:]
    return g


def glob_variants(g):
    """What one owned glob also covers: './' stripped; a literal path (or a dir/) also covers its
    subtree; '**/' also matches nothing (fnmatch has no '**', so src/**/*.ts must also try src/*.ts)."""
    g = norm_glob(g)
    vs = {g}
    if re.search(r"[*?\[]", g):
        vs.add(g.replace("**/", ""))
    else:
        vs.add(g.rstrip("/") + "/**")
    return vs


def globs_overlap(a, b):
    """Could two owned globs name the same file? fnmatch the variants both ways, else literal prefixes.
    ponytail: prefix heuristic over-warns (src/*.ts vs src/*.css); a real glob intersection if that gets noisy."""
    va, vb = glob_variants(a), glob_variants(b)
    if any(fnmatch.fnmatchcase(x, y) for x in va for y in vb) or any(
            fnmatch.fnmatchcase(x, y) for x in vb for y in va):
        return True
    na, nb = norm_glob(a), norm_glob(b)
    la, lb = (re.split(r"[*?\[]", g, maxsplit=1)[0] for g in (na, nb))
    wild = lambda g: bool(re.search(r"[*?\[]", g))
    return wild(na) and wild(nb) and (la.startswith(lb) or lb.startswith(la))


def deps_include(deps, ref):
    """Is ref in deps? Ids are case-insensitive, so match that way (a legacy dep may store wrong case)."""
    return ref.lower() in [str(d).lower() for d in deps or []]


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
        run = st.load(a.run, getattr(a, "task", None))
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
            deps, owned, warnings = csv(a.deps), csv(a.owned), []
            deps = [st.norm_dep(run, tid, d) for d in deps]  # stored id case; a self-dep fails here
            for d in deps:
                if st.dep_status(run, d) is None:
                    warnings.append(f"dep {d} not found (yet)")
            if a.parent:
                p = find_task(run, a.parent)
                if p["status"] in DEP_MET:
                    raise Fail(f"--parent onto {p['id']} ({p['status']}) is refused: status rolls up from children")
            active, _ = st.active_runs(run)
            for o in active:
                for x in o.get("tasks") or []:
                    ref = x.get("id") if o.get("id") == run.get("id") else f"{o.get('id')}:{x.get('id')}"
                    if not x.get("id") or x.get("status") in DEP_MET or ref in deps:
                        continue
                    hits = [f"{g} ~ {h}" for g in owned for h in x.get("owned") or [] if globs_overlap(g, h)]
                    if hits:
                        warnings.append(f"owned overlaps {ref} ({x.get('status')}): {', '.join(hits)}; "
                                        f"consider --deps {ref}")
            t = {"id": tid, "title": a.title, "wave": a.wave, "deps": deps, "owned": owned, "parent": a.parent,
                 "ready": False, "status": "planned", "agent": None, "model": None, "effort": None, "lane": None,
                 "last_commit": None, "gates": None, "verdict": None, "round": 0, "rounds": [],
                 "created": now(), "updated": now()}
            run["tasks"].append(t)
            st.event(run, "transition", f"task added: {a.title}", task=tid, to="planned", by=a.by)
            st.save(run, t, a.by)
            warnings += st.refresh_others(run, a.by)  # their boards show "blocks"; ready stays correct
            return {"ok": True, "task": t, **({"warnings": warnings} if warnings else {})}
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
            if "status" in changes and any(c.get("parent") == t["id"] for c in run["tasks"]):
                raise Fail(f"task {t['id']} has children: status rolls up from children "
                           "(set the children, or --parent elsewhere)")
            if changes.get("status") in DEP_GATED and t["status"] not in DEP_GATED and not a.force:
                waiting = st.unmet_tree(run, t)  # the task's own deps plus every ancestor's
                if waiting:
                    raise Fail(f"task {t['id']} waits on {', '.join(waiting)} (not merged/qa/done); pass --force "
                               "to dispatch anyway", unmet=waiting)
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
        st.save(run, t, a.by)
        warnings = st.refresh_others(run, a.by) if a.action == "set" else []
        if a.action == "round":
            d = st.task_dir(run["id"], t["id"])
            return {"ok": True, "task": t["id"], "round": t["round"], "kind": a.kind,
                    **{k: os.path.join(d, f"{k}-r{t['round']}.md") for k in ("brief", "report", "review")}}
        return {"ok": True, "task": t, **({"warnings": warnings} if warnings else {})}


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
        run = st.load(a.run, a.task)
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
    t.add_argument("--deps"), t.add_argument("--owned"), t.add_argument("--parent")
    t = task.add_parser("set", parents=[data_p, run_p])
    t.add_argument("task"), t.add_argument("pairs", nargs="+"), t.add_argument("--note")
    t.add_argument("--force", action="store_true")
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
        assert mk("task", "get", "T1")["task"]["id"] == "T1"  # only r1 owns T1
        assert mk("event", "--task", "T1", "--type", "note", "--text", "owner pick")["ok"]
        assert mk("run", "get", "--id", "r1")["run"]["recent"][-1]["text"] == "owner pick"
        mk("task", "add", "--id", "T1", "--title", "Clash", "--wave", "1", "--run", "r2")
        assert "active runs" in mk("task", "get", "T1", code=2)["error"]  # both own T1: still ambiguous
        assert "active runs" in mk("run", "get", code=2)["error"]
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

        # home mode: ~/.matrixkoder/projects.json names the repo's data dir; env still beats it
        repo2 = os.path.join(tmp, "repo2")
        os.makedirs(os.path.join(repo2, ".git"))
        os.makedirs(os.path.join(repo2, "sub"))
        homedata = os.path.join(tmp, "homedata")
        os.makedirs(os.path.join(tmp, "home"), exist_ok=True)
        with open(os.path.join(tmp, "home", "projects.json"), "w") as f:
            json.dump({os.path.realpath(repo2): {"data_dir": homedata, "onboarded_at": "x"}}, f)
        mk("run", "init", "--id", "h1", "--feature", "F", "--branch", "b", data_arg=False, cwd=os.path.join(repo2, "sub"))
        assert os.path.exists(os.path.join(homedata, "runs", "h1", "run.json"))
        assert not os.path.exists(os.path.join(repo2, ".matrixkoder"))
        mk("run", "init", "--id", "h2", "--feature", "F", "--branch", "b", data_arg=False, e=envd, cwd=repo2)
        assert os.path.exists(os.path.join(tmp, "envdata", "runs", "h2", "run.json"))  # env beats projects.json
        wt2 = os.path.join(repo2, ".git", "worktrees", "lane")
        os.makedirs(wt2)
        open(os.path.join(wt2, "commondir"), "w").write("../..\n")
        lane2 = os.path.join(tmp, "repo2-lane")
        os.makedirs(lane2)
        open(os.path.join(lane2, ".git"), "w").write(f"gitdir: {wt2}\n")
        mk("run", "init", "--id", "h3", "--feature", "F", "--branch", "b", data_arg=False, cwd=lane2)  # keyed by the MAIN checkout
        assert os.path.exists(os.path.join(homedata, "runs", "h3", "run.json"))
        # a dotfiles repo at $HOME is never project data
        fake_home = os.path.join(tmp, "fakehome")
        os.makedirs(os.path.join(fake_home, ".git"))
        os.makedirs(os.path.join(fake_home, "proj"))
        out = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "list"], cwd=os.path.join(fake_home, "proj"),
                             env=dict(env, HOME=fake_home), capture_output=True, text=True)
        assert out.returncode != 0 and "$HOME" in (out.stdout + out.stderr), out

        # global --data/--by/--run also parse before the subcommand (after: every mk above)
        mk("--data", data, "run", "init", "--id", "f1", "--feature", "F", "--branch", "b", data_arg=False)
        assert os.path.exists(os.path.join(data, "runs", "f1", "run.json"))
        front = mk("--data", data, "--by", "front", "--run", "r1", "event", "--type", "note",
                   "--text", "flags before the subcommand", data_arg=False)["event"]
        assert front["by"] == "front"  # subparser SUPPRESS keeps the earlier --by
        assert mk("--data", data, "--run", "r1", "task", "get", "T1", data_arg=False)["task"]["id"] == "T1"

        # deps, cross-run deps, parent roll-up, owned overlap — own data dir
        d2 = ["--data", os.path.join(tmp, "deps")]
        dk = lambda *x, code=0: mk(*x, *d2, code=code, data_arg=False)
        dk("run", "init", "--id", "ra", "--feature", "A", "--branch", "a")
        dk("run", "init", "--id", "rb", "--feature", "B", "--branch", "b")
        dk("task", "add", "--id", "R", "--title", "Reasoning", "--owned", "src/server/hermes-api.ts", "--run", "ra")
        dk("task", "add", "--id", "X", "--title", "Base", "--run", "rb")
        # deps normalise to the stored id case (ids are case-insensitive); a self-dep is refused
        assert dk("task", "add", "--id", "N1", "--title", "n", "--deps", "x", "--run", "rb")["task"]["deps"] == ["X"]
        assert dk("task", "add", "--id", "N2", "--title", "n", "--deps", "ra:r", "--run", "rb")["task"]["deps"] == ["ra:R"]
        assert dk("task", "add", "--id", "N3", "--title", "n", "--deps", "RA:R", "--run", "rb")["task"]["deps"] == ["ra:R"]
        for selfdep in ("Z", "z", "RB:Z", "rb:z"):
            assert dk("task", "add", "--id", "Z", "--title", "self", "--deps", selfdep,
                      "--run", "rb", code=2)["ok"] is False
        assert dk("task", "add", "--id", "S", "--title", "same-run dep", "--deps", "RA:R",
                  "--run", "ra")["task"]["deps"] == ["ra:R"]
        w = dk("task", "add", "--id", "A", "--title", "Archive", "--owned", "src/server/*.ts", "--run", "rb")
        assert any("ra:R" in x and "--deps ra:R" in x for x in w["warnings"]), w  # overlap across runs
        w = dk("task", "add", "--id", "A2", "--title", "Archive 2", "--owned", "src/server/*.ts",
               "--deps", "ra:R,X", "--run", "rb")
        assert not any("ra:R" in x for x in w.get("warnings", [])), w  # dep already declared: no warning
        assert "dep ra:Q not found" in dk("task", "add", "--id", "A3", "--title", "t", "--deps", "ra:Q",
                                           "--run", "rb")["warnings"][0]
        assert globs_overlap("src/a.ts", "src/*.ts") and not globs_overlap("src/a.ts", "src/b.ts")
        assert not globs_overlap("docs/*", "src/*")
        for a, b in (("src/**/*.ts", "src/a.ts"), ("**/*.ts", "a.ts"), ("src/server", "src/server/*.ts"),
                     ("src/", "src/a.ts"), ("./src/a.ts", "src/a.ts")):
            assert globs_overlap(a, b), (a, b)  # dirs, ** and ./ all cover their files
        # gate: unmet deps block dispatch, --force overrides
        e = dk("task", "set", "A2", "status=dispatched", "--run", "rb", code=2)
        assert "waits on ra:R, X" in e["error"] and e["unmet"] == ["ra:R", "X"], e
        board_a = open(os.path.join(tmp, "deps", "runs", "ra", "board.md")).read()
        assert "blocks rb:A2" in board_a, board_a  # shown on the dependency's board too
        dk("task", "set", "X", "status=merged", "--run", "rb")
        assert dk("task", "get", "A2", "--run", "rb")["task"]["ready"] is False  # ra:R still open
        dk("task", "set", "R", "status=merged", "--run", "ra")  # cross-run: flips rb:A2 ready
        assert dk("task", "get", "S", "--run", "ra")["task"]["ready"] is True  # same-run ra:R, in-memory
        t = dk("task", "get", "A2", "--run", "rb")["task"]
        assert t["ready"] is True, t
        rb = dk("run", "get", "--id", "rb")["run"]
        assert any(x["task"] == "A2" and "deps met" in x["text"] for x in rb["recent"])
        board_b = open(os.path.join(tmp, "deps", "runs", "rb", "board.md")).read()
        assert "ra:R ✓" in board_b and "planned · ready" in board_b, board_b
        dk("task", "set", "A2", "status=dispatched", "--run", "rb")
        assert dk("task", "get", "A2", "--run", "rb")["task"]["ready"] is False
        dk("task", "set", "A3", "status=building", "--force", "--run", "rb")
        # parent roll-up: a side state wins, else building while any child is past routed and not
        # all are met, else the least advanced child; nested parents too
        dk("task", "add", "--id", "P", "--title", "Parent", "--run", "rb")
        dk("task", "add", "--id", "C1", "--title", "Child 1", "--parent", "P", "--run", "rb")
        dk("task", "add", "--id", "C2", "--title", "Child 2", "--parent", "P", "--run", "rb")
        dk("task", "add", "--id", "G", "--title", "Grandchild", "--parent", "C2", "--run", "rb")
        assert dk("task", "add", "--id", "C3", "--title", "t", "--parent", "NOPE", "--run", "rb", code=2)["ok"] is False
        dk("task", "set", "C1", "status=merged", "--run", "rb")
        assert dk("task", "get", "P", "--run", "rb")["task"]["status"] == "building"  # not all children met
        dk("task", "set", "G", "status=building", "--run", "rb")
        assert dk("task", "get", "C2", "--run", "rb")["task"]["status"] == "building"
        assert dk("task", "get", "P", "--run", "rb")["task"]["status"] == "building"
        dk("task", "set", "C1", "status=stalled", "--run", "rb")
        assert dk("task", "get", "P", "--run", "rb")["task"]["status"] == "stalled"
        board_b = open(os.path.join(tmp, "deps", "runs", "rb", "board.md")).read().splitlines()
        rows = [x.split("|")[1].strip() for x in board_b if x.startswith("| ") and "Task" not in x]
        assert rows[-4:] == ["P", "↳ C1", "↳ C2", "↳ ↳ G"], rows

        # an ancestor's unmet deps gate the child's dispatch; a parent's status is never manual
        dk("task", "add", "--id", "P2", "--title", "Parent 2", "--deps", "A3", "--run", "rb")
        dk("task", "add", "--id", "C4", "--title", "Child 4", "--parent", "P2", "--run", "rb")
        e = dk("task", "set", "C4", "status=dispatched", "--run", "rb", code=2)
        assert "waits on A3" in e["error"] and e["unmet"] == ["A3"], e
        assert dk("task", "set", "C4", "status=dispatched", "--force", "--run", "rb")["ok"]
        for parent in ("P", "P2", "C2"):
            e = dk("task", "set", parent, "status=building", "--run", "rb", code=2)
            assert "status rolls up from children" in e["error"], e

        # --parent onto merged/qa/done is refused; other runs' ready stays correct after an add
        dk("task", "add", "--id", "Q2", "--title", "q2", "--run", "rb")
        dk("task", "add", "--id", "D2", "--title", "waits on rb:Q2", "--deps", "rb:Q2", "--run", "ra")
        dk("task", "set", "Q2", "status=merged", "--run", "rb")
        assert dk("task", "get", "D2", "--run", "ra")["task"]["ready"] is True
        e = dk("task", "add", "--id", "Q2C", "--title", "child", "--parent", "Q2", "--run", "rb", code=2)
        assert "merged" in e["error"] and "rolls up" in e["error"], e
        assert dk("task", "get", "Q2", "--run", "rb")["task"]["status"] == "merged"
        assert dk("task", "get", "D2", "--run", "ra")["task"]["ready"] is True  # no stale ready
        dk("task", "add", "--id", "Q3", "--title", "q3", "--run", "rb")
        dk("task", "set", "Q3", "status=building", "--run", "rb")
        dk("task", "add", "--id", "D3", "--title", "waits on rb:Q3", "--deps", "rb:Q3", "--run", "ra")
        assert dk("task", "get", "D3", "--run", "ra")["task"]["ready"] is False
        dk("task", "add", "--id", "Q3C", "--title", "child", "--parent", "Q3", "--run", "rb")
        assert dk("task", "get", "Q3", "--run", "rb")["task"]["status"] == "planned"  # rolled back by the child
        assert dk("task", "get", "D3", "--run", "ra")["task"]["ready"] is False  # refreshed, still correct

        # building + planned rolls up to building; an unknown child status never crashes the roll-up
        dk("task", "add", "--id", "P3", "--title", "p3", "--run", "rb")
        dk("task", "add", "--id", "K1", "--title", "k1", "--parent", "P3", "--run", "rb")
        dk("task", "add", "--id", "K2", "--title", "k2", "--parent", "P3", "--run", "rb")
        dk("task", "set", "K1", "status=building", "--run", "rb")
        assert dk("task", "get", "P3", "--run", "rb")["task"]["status"] == "building"
        runjson = os.path.join(tmp, "deps", "runs", "rb", "run.json")
        run = json.load(open(runjson))
        next(t for t in run["tasks"] if t["id"] == "K2")["status"] = "weird"  # a status mk-state never wrote
        write_json(runjson, run)
        assert dk("task", "set", "K1", "status=verifying", "--run", "rb")["ok"]

        # an unreadable or malformed OTHER active run never breaks the healthy one — own data dir
        d3 = ["--data", os.path.join(tmp, "sick")]
        sk = lambda *x, code=0: mk(*x, *d3, code=code, data_arg=False)
        sk("run", "init", "--id", "good", "--feature", "G", "--branch", "g")
        sk("run", "init", "--id", "sick", "--feature", "S", "--branch", "s")
        sk("task", "add", "--id", "T", "--title", "t", "--run", "good")
        sick = os.path.join(tmp, "sick", "runs", "sick", "run.json")
        keep = open(sick).read()
        open(sick, "w").write('{"id": "sick", ')  # truncated JSON
        out = sk("task", "set", "T", "status=routed")  # owner pick skips the sick sibling
        assert out["ok"] is True and any("sick" in w for w in out.get("warnings", [])), out
        assert sk("task", "get", "T")["task"]["status"] == "routed"  # later commands still work
        assert sk("render", "--run", "good")["ok"]
        open(sick, "w").write(json.dumps({**json.loads(keep), "tasks": [{"title": "ghost", "deps": ["good:T"]}]}))
        assert sk("render", "--run", "good")["ok"]  # a sibling task without id/status: no crash
        assert sk("task", "add", "--id", "N", "--title", "n", "--owned", "src/*.ts", "--run", "good")["ok"]
        # no prefix matching anywhere: abbreviated flags are unknown arguments
        for bad in (["task", "set", "T", "--fo", "status=building"], ["task", "set", "T", "--b=evil"],
                    ["--dat", os.path.join(tmp, "sick"), "run", "list"]):
            assert sk(*bad, code=2)["ok"] is False, bad
        print("mk-state self-test ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        sys.exit(main(sys.argv[1:]))
