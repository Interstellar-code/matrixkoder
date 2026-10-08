#!/usr/bin/env python3
"""MatrixKoder run state — the ONLY writer of <data>/runs/**. Python 3 stdlib only.

  run init --id ID --feature F --branch B [--plan PATH --force]
      --plan: the plan file drives the run — the plan tool (mk-plan.py, or $MK_PLAN_CMD) `check`
      must pass and its status be `approved`; --force overrides an unapproved status (with an event).
      The plan gets `set --status running` + `add-run`, then phase tasks (plan_phase, route,
      lane_group) and child tasks (plan_task, --parent) are created through the task-add path.
      plan_sha comes from a final `parse` AFTER those mutations, so the board ⚠ does not fire at once
  plan sync [--run ID] [--apply] — diff the run's plan against its tasks; the dry run changes
      nothing: new phase/task → added (the add path), changed title/wave/owned/deps on a
      planned|routed task → updated, on a started task → warning only, removed → warning (never
      deleted); --apply stores the new plan_sha and writes exactly ONE summary event. The last
      plan-linked run set `done` → plan `set --status done` (best-effort, never fatal)
  run list | run get [--id ID | ID] | run set [--id ID] [--next TEXT] [--status active|done]
  run stage N [--id ID] | run archive --id ID | run archive ID
  task add --id T --title TEXT [--wave N] [--deps A,RUN:B] [--owned glob,glob] [--parent P]
      ids are case-insensitive unique; deps store the real id case and a self-dep is refused;
      RUN:B is a task in another run of the same data dir; --parent onto merged/qa/done is refused;
      warns when an owned glob overlaps an unmerged task in any active run
      (the new task's own --parent chain is not an overlap: a parent owns its children's globs)
  task set T key=value ... [--note TEXT] [--force]   keys: status agent model effort lane pane
      branch workspace slot last_commit gates verdict (branch/workspace/slot are written by mk-lane.py acquire)
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
import argparse, contextlib, datetime, fcntl, fnmatch, hashlib, json, math, os, re, shlex, socket, subprocess, sys, tempfile

SCHEMA = 1
STATUSES = ("planned routed dispatched building verifying review fix merged qa done "
            "blocked quota stalled provider").split()
EVENT_TYPES = ("transition", "decision", "verdict", "quota", "note")
TASK_KEYS = ("status", "agent", "model", "effort", "lane", "pane", "branch", "workspace", "slot",
             "last_commit", "gates", "verdict")
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
        # plan sync --apply: events for this run id are counted and dropped (ONE summary event
        # comes after); other runs' upkeep events still write
        self.sink_run, self.sunk = None, 0

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
        entry = {k: run[k] for k in ("id", "feature", "status", "started", "ended")}
        entry["plan"] = run.get("plan_id")  # C4: the plan id, null for plan-less and legacy runs
        idx.append(entry)
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
                src = read_json(os.path.join(self.run_dir(rid), "run.json"))
            except (OSError, ValueError):
                return None  # unreadable dep target: unknown, so unmet — never fatal
            if not isinstance(src, dict):
                return None  # valid JSON but not an object: unknown too
        return next((t.get("status") for t in src.get("tasks") or []
                     if isinstance(t, dict) and str(t.get("id") or "").lower() == tid.lower()), None)

    def unmet(self, run, t):
        return [d for d in t.get("deps") or [] if self.dep_status(run, d) not in DEP_MET]

    def unmet_tree(self, run, t):
        """The task's own unmet deps plus every ancestor's (a parent's dep gates its children)."""
        out = list(self.unmet(run, t))
        for pt in ancestor_chain(run, t.get("parent")):
            out += [d for d in self.unmet(run, pt) if d not in out]
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
                src = read_json(os.path.join(self.run_dir(rid), "run.json"))
            except (OSError, ValueError):
                src = {}
            if not isinstance(src, dict):  # valid JSON but not an object: dep stays unknown
                src = {}
        hit = next((t.get("id") for t in (src.get("tasks") or [])
                    if isinstance(t, dict) and str(t.get("id") or "").lower() == did.lower()), None)
        return (rid + ":" if rid else "") + (hit or did)

    def active_runs(self, run):
        """-> ([run, *other active runs], warnings): the given run first, the other active runs
        loaded. An unreadable or wrong-shape sibling (missing id/branch/feature/updated, `tasks`
        not a list of objects) is skipped and named in warnings, never fatal."""
        others, warnings = [], []
        for r in self.index():
            rid = r.get("id")
            if r.get("status") != "active" or rid in (None, run.get("id")):
                continue
            try:
                x = read_json(os.path.join(self.run_dir(rid), "run.json"))
            except (OSError, ValueError) as e:
                warnings.append(f"active run {rid} unreadable, skipped: {type(e).__name__}")
                continue
            if not isinstance(x, dict) or not x.get("id"):
                warnings.append(f"active run {rid} run.json is not a usable run (no id), skipped")
                continue
            missing = [k for k in ("branch", "feature", "updated") if not x.get(k)]
            if missing:
                warnings.append(f"active run {rid} run.json missing {', '.join(missing)}, skipped")
                continue
            if not isinstance(x.get("tasks"), list) or not all(isinstance(t, dict) for t in x["tasks"]):
                warnings.append(f"active run {rid} has a malformed tasks list, skipped")
                continue
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
        and their boards show "blocks" markers. An unusable sibling is skipped and warned. -> warnings"""
        # ponytail: every add/set re-renders every other active run, each render re-reading them
        # all — ~N² in active runs; a per-command {rid: run} cache when that ever shows up
        runs, warnings = self.active_runs(run)
        for o in runs[1:]:
            try:
                if self.refresh_ready(o, by):
                    self.save(o, by=by)
                else:
                    self.render(o)
            except Exception as e:  # a wrong-shape sibling (e.g. missing branch) is a warning, never a traceback
                warnings.append(f"run {o.get('id')} not refreshed: {type(e).__name__}")
        return warnings

    def event(self, run, type_, text, task=None, frm=None, to=None, by="orchestrator"):
        ev = {"t": now(), "run": run["id"], "task": task, "type": type_, "text": text,
              "from": frm, "to": to, "by": by}
        if self.sink_run is not None and self.sink_run == run.get("id"):
            self.sunk += 1  # plan sync --apply: the reused add/update paths stay silent
            return ev
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
        if run.get("plan_id"):  # plan-linked run: id · status · sha (+ ⚠ when the file changed)
            sha = run.get("plan_sha")
            out.append(f"plan: `{run['plan_id']}` · {run.get('plan_status') or '—'}"
                       + (f" · sha `{sha[:12]}`" if sha else ""))
            cur = file_sha(run.get("plan"))  # plain read + hashlib; never the plan tool in render
            if cur and sha and cur != sha:  # unreadable file: no warning, no crash
                out.append(f"⚠ plan file changed since run start (stored `{sha[:12]}`)")
        elif run.get("plan"):  # legacy path-only link (runs created before the plan tool)
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
            # mk-lane.py's slot keys ride the lane cell; unset keys must leave the row byte-identical
            lane = " · ".join(x for x in (t.get("lane"), t.get("branch"),
                                           f"slot {t['slot']}" if t.get("slot") else None,
                                           t.get("workspace")) if x)
            out.append("| " + " | ".join(cell(v) for v in (
                "↳ " * depth + (t.get("id") or "?"), t.get("title"), t.get("wave"), status, ", ".join(deps),
                t.get("agent"), me, t.get("round"), lane, t.get("last_commit"),
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
    advanced child — only ever raised to `building` (never lowered) when work has started: any
    child past `routed` and the least advanced child below `building`."""
    order = {s: i for i, s in enumerate(STATUSES)}
    for t, _ in reversed(tree(run.get("tasks") or [])):  # leaves first, so nested parents see rolled-up children
        if not t.get("id"):
            continue
        kids = [c.get("status") for c in run["tasks"] if c.get("parent") == t["id"]]
        if not kids:
            continue
        new = next((s for s in SIDE if s in kids), None)
        if new is None:
            new = min(kids, key=lambda s: order.get(s, 0))  # unknown status: least advanced
            if any(order.get(k, 0) > order["routed"] for k in kids) and order.get(new, 0) < order["building"]:
                new = "building"
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


def ancestor_chain(run, parent):
    """-> [task, ...] the --parent chain above `parent`, nearest first; [] without a parent.
    Missing or cyclic parents stop the walk (a corrupt parent must not hang a command)."""
    out, p, seen = [], parent, set()
    while p and p not in seen:
        seen.add(p)
        pt = next((x for x in run["tasks"] if x.get("id") == p), None)
        if pt is None:
            break
        out.append(pt)
        p = pt.get("parent")
    return out


def parse_value(v):
    if v == "":
        return None
    if v[:1] in "{[":
        try:
            return json.loads(v)
        except json.JSONDecodeError as e:
            raise Fail(f"bad JSON value {v!r}: {e}")
    return v


# ---------- plan tool (C3: mk-plan.py only ever as a subprocess, never imported) ----------

def plan_tool(*args):
    """Run mk-plan.py (or $MK_PLAN_CMD, shlex-split) and return its one JSON line (C2). -> the dict
    on ok:true, else Fail carrying the tool's errors. The board render never calls this (a plain
    file read there); `run set --status done` treats a failure as best-effort."""
    cmd = (shlex.split(os.environ["MK_PLAN_CMD"]) if os.environ.get("MK_PLAN_CMD") else
           [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mk-plan.py")])
    try:
        r = subprocess.run([*cmd, *args], capture_output=True, text=True, timeout=60)
    except OSError as e:  # missing script, not executable
        raise Fail(f"plan tool cannot run: {type(e).__name__}: {e}")
    except subprocess.TimeoutExpired:
        raise Fail(f"mk-plan {' '.join(args)}: timed out")
    out = None
    for line in reversed((r.stdout or "").strip().splitlines()):  # the JSON line is the last one
        with contextlib.suppress(json.JSONDecodeError):
            out = json.loads(line)
            break
    if r.returncode == 0 and isinstance(out, dict) and out.get("ok"):
        return out
    detail = out.get("errors") or out.get("error") if isinstance(out, dict) else None
    if isinstance(detail, list):
        detail = "; ".join(str(x) for x in detail)
    raise Fail(f"mk-plan {' '.join(args)}: "
               f"{detail or (r.stdout + r.stderr).strip()[:200] or f'exit {r.returncode}'}")


def file_sha(path):
    """sha256 of a file's bytes, None when unreadable — the board render must never crash on it."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (OSError, TypeError):
        return None


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
        gate = None  # C5: check + the approved gate run BEFORE the lock, before any local write
        if a.plan:
            gate = plan_tool("check", a.plan)
            if gate.get("status") != "approved" and not a.force:
                raise Fail(f"plan {gate.get('plan') or a.plan} is {gate.get('status')!r}, not approved; "
                           "approve it (mk-plan.py set --status approved) or pass --force")
            plan_preflight(st, plan_tool("parse", a.plan), rid)
        with st.locked():
            if os.path.exists(os.path.join(st.run_dir(rid), "run.json")):
                raise Fail(f"run {rid} exists")
            cur = None
            if a.plan:  # the plan learns the run before the run learns its tasks (C5)
                plan_tool("set", a.plan, "--status", "running")
                plan_tool("add-run", a.plan, "--run", rid)
                cur = plan_tool("parse", a.plan)  # sha AFTER set/add-run: no board ⚠ at birth
            os.makedirs(os.path.join(st.run_dir(rid), "tasks"), exist_ok=True)
            run = {"schema": SCHEMA, "id": rid, "feature": a.feature, "branch": a.branch, "plan": a.plan,
                   "plan_id": cur.get("plan") if cur else None, "plan_sha": cur.get("sha") if cur else None,
                   "plan_status": cur.get("status") if cur else None,
                   "status": "active", "stage": 0, "next": None, "waiting": None,
                   "started": now(), "ended": None, "updated": None, "tasks": [], "recent": []}
            st.event(run, "note", f"run init: {a.feature} on {a.branch}")
            if cur and gate.get("status") != "approved":
                st.event(run, "note", f"plan {run['plan_id']} was {gate.get('status')!r}, not approved; "
                                      "run init forced with --force")
            st.save(run)
            warnings = list(gate.get("warnings") or []) if gate else []
            for ph in (cur.get("phases") or []) if cur else []:
                pid = str(ph.get("id") or "")
                _, w = add_task(st, run, pid, ph.get("title") or pid, ph.get("wave"),
                                ph.get("deps") or [], ph.get("owned") or [], None, a.by,
                                {"plan_phase": pid, "route": ph.get("route") or None,
                                 "lane_group": ph.get("lane_group") or None})
                warnings += w
                for c in ph.get("tasks") or []:
                    _, w = add_task(st, run, str(c.get("id") or ""), c.get("title") or c.get("id"),
                                    ph.get("wave"), [], c.get("owned") or [], pid, a.by,
                                    {"plan_task": c.get("id")})
                    warnings += w
        return {"ok": True, "run": run, **({"warnings": warnings} if warnings else {})}
    a.run = a.run or getattr(a, "pos_id", None)
    if a.action == "get":
        return {"ok": True, "run": st.load(a.run)}
    if a.action == "archive" and not a.run:
        raise Fail("run archive needs --id")
    warn = None
    with st.locked():
        run = st.load(a.run)
        if a.action == "set":
            if a.next is None and a.status is None:
                raise Fail("run set needs --next and/or --status")
            if a.next is not None:
                run["next"] = a.next
            if a.status:
                warn = set_run_status(st, run, a.status)
        elif a.action == "stage":
            if a.n is None or a.n < 0:
                raise Fail("run stage needs N >= 0")
            run["stage"] = a.n
            st.event(run, "note", f"stage {a.n}")
        else:  # archive
            set_run_status(st, run, "archived")
        st.save(run)
    return {"ok": True, "run": run, **({"warning": warn} if warn else {})}


def set_run_status(st, run, status):
    if status != run["status"]:
        st.event(run, "note", f"run {run['status']} → {status}")
        run["status"] = status
        run["ended"] = None if status == "active" else (run.get("ended") or now())
        if status == "done" and run.get("plan_id") and run.get("plan"):
            return plan_done_if_last(st, run)  # C5: best-effort, never fatal
    return None


def plan_done_if_last(st, run):
    """The LAST run id in the plan's runs list finished → the plan goes `done` (C5). Best-effort:
    a plan-tool error (missing plan file, mk-plan error JSON) never fails the `run set` around it —
    the message rides back as the command's "warning". Only Fail is caught; anything else is a real
    bug and still raises."""
    try:
        cur = plan_tool("parse", run["plan"])
        runs = [str(x) for x in cur.get("runs") or []]
        if runs and runs[-1].lower() == run["id"].lower():
            plan_tool("set", run["plan"], "--status", "done")
            run["plan_status"] = "done"
            st.event(run, "note", f"plan {run['plan_id']} done (last run finished)")
    except Fail as e:
        return f"plan {run.get('plan_id')} not marked done: {e}"
    return None


def plan_preflight(st, cur, rid):
    """Everything `run init --plan` is about to write, checked the way the task-add path would
    build it — BEFORE the lock, so an unbuildable plan leaves no run dir, no index entry and a
    byte-identical plan file (a bad id can no longer half-create a run after `set`/`add-run`).
    Same id rule as `task add`, unique ids across the run, plan-shaped deps/owned, self-deps."""
    phases = cur.get("phases") or []
    if not isinstance(phases, list):
        raise Fail("plan phases is not a list")
    ids = []
    for ph in phases:
        if not isinstance(ph, dict):
            raise Fail("plan phase is not an object")
        pid = str(ph.get("id") or "")
        check_id(pid, "task id")
        ids.append(pid)
        tasks = ph.get("tasks") or []
        if not isinstance(tasks, list) or not all(isinstance(c, dict) for c in tasks):
            raise Fail(f"phase {pid}: tasks is not a list")
        for c in tasks:
            cid = str(c.get("id") or "")
            check_id(cid, "task id")
            ids.append(cid)
    seen = set()
    for tid in ids:  # the add path's clash check, run before any task is written
        if tid.lower() in seen:
            raise Fail(f"task {tid} exists")
        seen.add(tid.lower())
    for ph in phases:
        pid = str(ph.get("id") or "")
        deps = ph.get("deps") or []
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise Fail(f"phase {pid}: deps must be a list of ids")
        for d in deps:
            st.norm_dep({"id": rid}, pid, d)  # stored id case; a self-dep fails here too
        plan_owned(ph.get("owned"), f"phase {pid}")
        for c in ph.get("tasks") or []:
            plan_owned(c.get("owned"), f"task {c.get('id')}")


def plan_owned(owned, who):
    if owned is None:
        return []
    if not isinstance(owned, list) or not all(isinstance(x, str) for x in owned):
        raise Fail(f"{who}: owned must be a list of paths")
    return owned


def add_task(st, run, tid, title, wave, deps, owned, parent, by, extra=None):
    """The ONE task-add path (`task add`, `run init --plan`, `plan sync --apply`): validate, append,
    save (rollup, ready, board, other runs included). `extra` rides on the new task (C4 plan links).
    -> (task, warnings)"""
    tid = check_id(tid, "task id")
    clash = [t["id"] for t in run["tasks"] if t["id"].lower() == tid.lower()]
    if clash:  # case-insensitive: task folders share one path on APFS
        raise Fail(f"task {tid} exists" + (f" as {clash[0]}" if clash[0] != tid else ""))
    if not title:
        raise Fail("task add needs --title")
    deps, owned, warnings = list(deps or []), list(owned or []), []
    deps = [st.norm_dep(run, tid, d) for d in deps]  # stored id case; a self-dep fails here
    for d in deps:
        if st.dep_status(run, d) is None:
            warnings.append(f"dep {d} not found (yet)")
    if parent:
        p = find_task(run, parent)
        if p["status"] in DEP_MET:
            raise Fail(f"--parent onto {p['id']} ({p['status']}) is refused: status rolls up from children")
    active, _ = st.active_runs(run)
    own = {x["id"] for x in ancestor_chain(run, parent)}  # our own parent chain: not a collision
    for o in active:
        same = o.get("id") == run.get("id")
        for x in o.get("tasks") or []:
            ref = x.get("id") if same else f"{o.get('id')}:{x.get('id')}"
            if not x.get("id") or x.get("status") in DEP_MET or ref in deps:
                continue
            if same and x.get("id") in own:
                continue  # a parent owns its children's globs; overlapping them is the plan
            hits = [f"{g} ~ {h}" for g in owned for h in x.get("owned") or [] if globs_overlap(g, h)]
            if hits:
                warnings.append(f"owned overlaps {ref} ({x.get('status')}): {', '.join(hits)}; "
                                f"consider --deps {ref}")
    t = {"id": tid, "title": title, "wave": wave, "deps": deps, "owned": owned, "parent": parent,
         "ready": False, "status": "planned", "agent": None, "model": None, "effort": None, "lane": None,
         "pane": None, "branch": None, "workspace": None, "slot": None,
         "last_commit": None, "gates": None, "verdict": None, "round": 0, "rounds": [],
         "created": now(), "updated": now(), **(extra or {})}
    run["tasks"].append(t)
    st.event(run, "transition", f"task added: {title}", task=tid, to="planned", by=by)
    st.save(run, t, by)
    warnings += st.refresh_others(run, by)  # their boards show "blocks"; ready stays correct
    return t, warnings


def cmd_task(a, st):
    with st.locked():
        run = st.load(a.run, getattr(a, "task", None))
        if a.action == "get":
            t = find_task(run, a.task)
            return {"ok": True, "task": t, "dir": st.task_dir(run["id"], t["id"])}
        if a.action == "add":
            t, warnings = add_task(st, run, a.id, a.title, a.wave, csv(a.deps), csv(a.owned), a.parent, a.by)
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


def plan_field_diffs(t, src, child):
    """Which of title/wave/owned/deps differ between a stored task and its plan entry (P2.2).
    Deps compare case-insensitively (ids do); owned compares exactly (paths)."""
    d = []
    if (t.get("title") or "") != str(src.get("title") or ""):
        d.append("title")
    if not child and t.get("wave") != src.get("wave"):
        d.append("wave")
    if [str(x).lower() for x in t.get("deps") or []] != [str(x).lower() for x in src.get("deps") or []]:
        d.append("deps")
    if [str(x) for x in t.get("owned") or []] != [str(x) for x in src.get("owned") or []]:
        d.append("owned")
    return d


def cmd_plan(a, st):
    if a.action != "sync":
        raise Fail(f"unknown plan action {a.action!r}")
    with st.locked():
        run = st.load(a.run)
        if not (run.get("plan_id") and run.get("plan")):
            raise Fail(f"run {run['id']} has no plan link (run init --plan)")
        cur = plan_tool("parse", run["plan"])
        changes, warnings, adds, updates = [], [], [], []
        ph_by = {str(t.get("plan_phase")).lower(): t for t in run["tasks"] if t.get("plan_phase")}
        ck_by = {str(t.get("plan_task")).lower(): t for t in run["tasks"] if t.get("plan_task")}
        for ph in cur.get("phases") or []:
            pid = str(ph.get("id") or "")
            t = ph_by.get(pid.lower())
            if t is None:
                adds.append(("phase", ph, None))
                changes.append(f"phase {pid}: new → add")
            if t is not None:
                d = plan_field_diffs(t, ph, False)
                if d:
                    if t.get("status") in ("planned", "routed"):
                        updates.append((t, ph, d, False))
                        changes.append(f"phase {pid}: {', '.join(d)} changed → update")
                    else:
                        warnings.append(f"phase {pid} is {t.get('status')} (started): "
                                        f"{', '.join(d)} changed in the plan; not updated")
            for c in ph.get("tasks") or []:
                cid = str(c.get("id") or "")
                ct = ck_by.get(cid.lower())
                if ct is None:
                    adds.append(("task", c, ph))
                    changes.append(f"task {cid}: new → add")
                else:
                    d = plan_field_diffs(ct, c, True)
                    if d:
                        if ct.get("status") in ("planned", "routed"):
                            updates.append((ct, c, d, True))
                            changes.append(f"task {cid}: {', '.join(d)} changed → update")
                        else:
                            warnings.append(f"task {cid} is {ct.get('status')} (started): "
                                            f"{', '.join(d)} changed in the plan; not updated")
        seen_ph = {str(ph.get("id")).lower() for ph in cur.get("phases") or []}
        seen_ck = {str(c.get("id")).lower() for ph in cur.get("phases") or [] for c in ph.get("tasks") or []}
        for t in run["tasks"]:  # removed from the plan: warned, NEVER deleted
            if t.get("plan_phase") and str(t["plan_phase"]).lower() not in seen_ph:
                warnings.append(f"phase {t['plan_phase']} removed from the plan; task {t['id']} kept")
            if t.get("plan_task") and str(t["plan_task"]).lower() not in seen_ck:
                warnings.append(f"task {t['plan_task']} removed from the plan; task {t['id']} kept")
        if not a.apply:
            return {"ok": True, "run": run["id"], "plan": run["plan_id"], "apply": False,
                    "sha": cur.get("sha"), "changes": changes, "warnings": warnings}
        have = {t["id"].lower() for t in run["tasks"]}  # pre-flight: fail before anything is written
        for _, src, _ in adds:
            tid = str(src.get("id") or "")
            if not tid or tid.lower() in have:
                raise Fail(f"plan adds task {tid!r} but a task with that id exists; rename first")
        for t, src, d, child in updates:
            if not child and "deps" in d:  # a self-dep in the new deps fails before any write
                [st.norm_dep(run, t["id"], x) for x in src.get("deps") or []]
        st.sink_run, st.sunk = run["id"], 0
        try:
            for kind, src, ph in adds:
                if kind == "phase":
                    add_task(st, run, str(src.get("id") or ""), src.get("title") or src.get("id"),
                             src.get("wave"), src.get("deps") or [], src.get("owned") or [], None, a.by,
                             {"plan_phase": src.get("id"), "route": src.get("route") or None,
                              "lane_group": src.get("lane_group") or None})
                else:
                    add_task(st, run, str(src.get("id") or ""), src.get("title") or src.get("id"),
                             ph.get("wave"), [], src.get("owned") or [], str(ph.get("id") or ""), a.by,
                             {"plan_task": src.get("id")})
            for t, src, d, child in updates:
                t["title"] = str(src.get("title") or t["title"])
                if not child:
                    if "wave" in d:
                        t["wave"] = src.get("wave")
                    if "deps" in d:
                        t["deps"] = [st.norm_dep(run, t["id"], x) for x in src.get("deps") or []]
                if "owned" in d:
                    t["owned"] = list(src.get("owned") or [])
                t["updated"] = now()
                st.save(run, t, a.by)
            run["plan_sha"] = cur.get("sha")
            run["plan_status"] = cur.get("status")
            st.save(run)
        finally:
            sunk = st.sunk
            st.sink_run, st.sunk = None, 0
        st.event(run, "note", f"plan sync: {len(adds)} added, {len(updates)} updated"
                 + (f", {sunk} task events suppressed" if sunk else ""))
        st.save(run)
    return {"ok": True, "run": run["id"], "plan": run["plan_id"], "apply": True, "sha": cur.get("sha"),
            "changes": changes, "warnings": warnings}


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
    r.add_argument("--force", action="store_true")  # run init --plan: init an unapproved plan
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

    pn = sub.add_parser("plan").add_subparsers(dest="action", required=True)
    ps = pn.add_parser("sync", parents=[data_p, run_p])
    ps.add_argument("--apply", action="store_true")  # default: dry run, nothing written
    ps.set_defaults(func=cmd_plan)

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

# The C2 surface of mk-plan.py as a fixture over a JSON "plan file" — the self-test must not need
# P1's real code (it may not exist yet in this worktree).
PLAN_FIXTURE = '''#!/usr/bin/env python3
import hashlib, json, sys
cmd, path = sys.argv[1], sys.argv[2]
try:
    db = json.load(open(path))
except OSError:
    print(json.dumps({"ok": False, "errors": [f"plan file not found: {path}"]})); sys.exit(3)
if cmd in ("parse", "check"):
    if cmd == "check" and db.get("check_error"):
        print(json.dumps({"ok": False, "errors": ["line 4: " + db["check_error"]]})); sys.exit(2)
    extra = {"warnings": db.get("warnings", [])} if cmd == "check" else {}
    print(json.dumps({"ok": True, "plan": db.get("plan"), "status": db.get("status"),
                      "runs": db.get("runs", []), "sha": hashlib.sha256(open(path, "rb").read()).hexdigest(),
                      "gates": {}, "phases": db.get("phases", []), **extra}))
elif cmd == "set":
    db["status"] = sys.argv[sys.argv.index("--status") + 1]
    json.dump(db, open(path, "w"), indent=1)
    print(json.dumps({"ok": True, "status": db["status"]}))
elif cmd == "add-run":
    db.setdefault("runs", []).append(sys.argv[sys.argv.index("--run") + 1])
    json.dump(db, open(path, "w"), indent=1)
    print(json.dumps({"ok": True, "runs": db["runs"]}))
else:
    print(json.dumps({"ok": False, "errors": [f"bad command {cmd}"]})); sys.exit(2)
'''

# a plan with 2 phases and 3 tasks (P2.1 makes the third child task)
PLAN_BASE = {"plan": "tplan", "status": "approved", "runs": [], "phases": [
    {"id": "P1", "title": "Phase one", "wave": 1, "deps": [], "owned": ["scripts/a.py"],
     "route": "codex gpt-5 high", "size": "standard", "risk": "reversible", "review": "light",
     "qa": [], "commit": "feat: a", "subagents": False, "lane_group": "A", "tasks": [
        {"id": "P1.1", "title": "First", "acceptance": "a1", "refs": [], "subagent": False,
         "owned": ["scripts/a.py"]},
        {"id": "P1.2", "title": "Second", "acceptance": "a2", "refs": [], "subagent": False, "owned": []}]},
    {"id": "P2", "title": "Phase two", "wave": 1, "deps": ["P1"], "owned": ["docs/x.md"],
     "route": "", "size": "standard", "risk": "reversible", "review": "light", "qa": [],
     "commit": "feat: b", "subagents": False, "lane_group": "A", "tasks": [
        {"id": "P2.1", "title": "Third", "acceptance": "a3", "refs": [], "subagent": False, "owned": []}]}]}

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
        # run init --plan now runs the plan tool (check/approved) — plans/f.md does not exist and
        # no MK_PLAN_CMD is set here, so r1 is a plan-less run; plan links get their own block below
        out = mk("run", "init", "--id", "r1", "--feature", "Feat", "--branch", "feat/x")
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

        # pane: the lane's herdr pane id (arm writes it) is a settable task key
        assert "pane" in TASK_KEYS
        tp = mk("task", "set", "T1", "pane=wK:p1")["task"]
        assert tp["pane"] == "wK:p1"
        assert mk("task", "get", "T1")["task"]["pane"] == "wK:p1"
        assert mk("task", "set", "T1", "pane=")["task"]["pane"] is None  # empty clears, like lane=
        assert mk("task", "set", "T1", "pan=wK:p1", code=2)["ok"] is False  # still a closed key set
        assert mk("task", "get", "T1")["task"]["pane"] is None
        assert mk("task", "set", "T1", "pane=wK:p1")["task"]["pane"] == "wK:p1"

        # branch/workspace/slot: the lane a task holds, written by mk-lane.py acquire
        assert {"branch", "workspace", "slot"} <= set(TASK_KEYS)
        tl = mk("task", "set", "T1", "branch=mk/r1/t1", "workspace=mk:repo:1", "slot=1")["task"]
        assert (tl["branch"], tl["workspace"], tl["slot"]) == ("mk/r1/t1", "mk:repo:1", "1"), tl
        assert mk("task", "get", "T1")["task"]["branch"] == "mk/r1/t1"
        board = open(mk("render")["board"]).read()
        assert "mk/r1/t1 · slot 1 · mk:repo:1" in board, board  # the lane cell names the slot
        for k in ("branch", "workspace", "slot"):
            assert mk("task", "set", "T1", f"{k}=")["task"][k] is None  # empty clears, like lane=
        plain = open(mk("render")["board"]).read()  # the lane cell is back to the bare path
        assert "| T1 |" in plain and "mk/r1/t1" not in plain, plain
        for bad in ("bran=mk/r1/t1", "workspac=mk:repo:1", "slotx=1"):
            assert mk("task", "set", "T1", bad, code=2)["ok"] is False  # still a closed key set
        assert mk("task", "set", "T1", "branch=mk/r1/t1", "workspace=mk:repo:1", "slot=1")["ok"]

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

        # owned overlap: a child's globs inside its parent's are the plan, not a collision
        overlap_refs = lambda w: [x.split("owned overlaps ", 1)[1].split(" ")[0]
                                  for x in w.get("warnings", []) if x.startswith("owned overlaps ")]
        dk("task", "add", "--id", "OW", "--title", "Owner", "--owned", "scripts/*.py", "--run", "rb")
        w = dk("task", "add", "--id", "OWC", "--title", "child", "--parent", "OW",
               "--owned", "scripts/mk-state.py", "--run", "rb")
        assert not overlap_refs(w), w
        dk("task", "add", "--id", "OWM", "--title", "mid", "--parent", "OW", "--run", "rb")
        w = dk("task", "add", "--id", "OWG", "--title", "grandchild", "--parent", "OWM",
               "--owned", "scripts/mk-state.py", "--run", "rb")
        assert not ({"OW", "OWM"} & set(overlap_refs(w))), w  # ancestors skipped...
        assert "OWC" in overlap_refs(w), w  # ...a cousin branch sharing the glob still warns
        dk("task", "add", "--id", "OWR", "--title", "same id, other run", "--owned", "scripts/*.py", "--run", "ra")
        w = dk("task", "add", "--id", "OWX", "--title", "unrelated", "--owned", "scripts/*.py", "--run", "rb")
        assert "ra:OWR" in overlap_refs(w), w  # cross-run overlap is still reported
        dk("task", "add", "--id", "OWS", "--title", "sibling", "--owned", "scripts/lane-*.py", "--run", "rb")
        w = dk("task", "add", "--id", "OWY", "--title", "another sibling", "--owned", "scripts/lane-*.py", "--run", "rb")
        assert "OWS" in overlap_refs(w), w  # a real same-run collision is untouched

        # roll-up only raises to building, never lowers; verify every rule in the acceptance
        cases = (
            ("P4", ("review", "review"), "review"),
            ("P5", ("verifying", "review"), "verifying"),
            ("P6", ("fix", "merged"), "fix"),
            ("P7", ("building", "planned"), "building"),
            ("P8", ("planned", "planned"), "planned"),
        )
        for pid, (s1, s2), want in cases:
            dk("task", "add", "--id", pid, "--title", pid, "--run", "rb")
            dk("task", "add", "--id", f"{pid}C1", "--title", "c1", "--parent", pid, "--run", "rb")
            dk("task", "set", f"{pid}C1", f"status={s1}", "--run", "rb")
            dk("task", "add", "--id", f"{pid}C2", "--title", "c2", "--parent", pid, "--run", "rb")
            dk("task", "set", f"{pid}C2", f"status={s2}", "--run", "rb")
            assert dk("task", "get", pid, "--run", "rb")["task"]["status"] == want, (pid, want)

        # building + planned rolls up to building; an unknown child status never crashes the roll-up
        dk("task", "add", "--id", "P3", "--title", "p3", "--run", "rb")
        dk("task", "add", "--id", "K1", "--title", "k1", "--parent", "P3", "--run", "rb")
        dk("task", "add", "--id", "K2", "--title", "k2", "--parent", "P3", "--run", "rb")
        dk("task", "set", "K1", "status=building", "--run", "rb")
        got = dk("task", "get", "P3", "--run", "rb")["task"]["status"]
        assert got == "building", got
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
        keep = json.loads(open(sick).read())
        open(sick, "w").write('{"id": "sick", ')  # truncated JSON
        out = sk("task", "set", "T", "status=routed")  # owner pick skips the sick sibling
        assert out["ok"] is True and any("sick" in w for w in out.get("warnings", [])), out
        assert sk("task", "get", "T")["task"]["status"] == "routed"  # later commands still work
        assert sk("render", "--run", "good")["ok"]
        for shape in (
            {"id": "sick", "tasks": [5]},
            {"id": "sick", "tasks": {"a": 1}},
            {**keep, "branch": None},
            {**keep, "feature": None},
            {**keep, "updated": None},
        ):
            open(sick, "w").write(json.dumps(shape))
            # render walks active_runs; task set also walks refresh_others — both must warn, not crash
            out = sk("render", "--run", "good")
            assert out["ok"] is True, (shape, out)
            out = sk("task", "set", "T", "status=building")
            assert out["ok"] is True and any("sick" in w for w in out.get("warnings", [])), (shape, out)
            assert sk("task", "get", "T")["task"]["status"] == "building"
        # a sibling task missing id/status must not crash render or commands in the healthy run
        open(sick, "w").write(json.dumps({**keep, "tasks": [{"title": "ghost", "deps": ["good:T"]}]}))
        assert sk("render", "--run", "good")["ok"]
        assert sk("task", "set", "T", "status=review")["ok"]
        assert sk("task", "get", "T")["task"]["status"] == "review"
        # a dep-target run.json that is valid JSON but not an object → unmet with a warning, no traceback
        badrun = os.path.join(tmp, "sick", "runs", "baddep", "run.json")
        os.makedirs(os.path.dirname(badrun), exist_ok=True)
        write_json(badrun, [1])
        assert sk("task", "add", "--id", "BD", "--title", "bad dep", "--deps", "baddep:X",
                  "--run", "good")["warnings"][0].startswith("dep baddep:X not found"), open(sick).read()
        assert sk("task", "add", "--id", "N", "--title", "n", "--owned", "src/*.ts", "--run", "good")["ok"]
        # plans: run init --plan, plan sync, link fields, board ⚠, last-run done — own data dir,
        # the fixture plan tool in $MK_PLAN_CMD (never P1's real code)
        fix = os.path.join(tmp, "mkplan-fix.py")
        with open(fix, "w") as f:
            f.write(PLAN_FIXTURE)
        penv = dict(env, MK_PLAN_CMD=" ".join([sys.executable, fix]))
        pd = os.path.join(tmp, "plans")
        pk = lambda *x, code=0: mk(*x, "--data", pd, code=code, data_arg=False, e=penv)
        pfile = os.path.join(tmp, "tplan.json")
        putdb = lambda db: json.dump(db, open(pfile, "w"), indent=1)

        putdb({**PLAN_BASE, "status": "draft"})
        e = pk("run", "init", "--id", "pr1", "--feature", "P", "--branch", "b", "--plan", pfile, code=2)
        assert "not approved" in e["error"] and not os.path.exists(os.path.join(pd, "runs", "pr1")), e
        putdb({**PLAN_BASE, "status": "approved", "check_error": "dep cycle"})
        e = pk("run", "init", "--id", "pr1", "--feature", "P", "--branch", "b", "--plan", pfile,
               "--force", code=2)
        assert "dep cycle" in e["error"], e  # a failing check is refused even with --force
        putdb({**PLAN_BASE, "status": "approved"})
        out = pk("run", "init", "--id", "pr1", "--feature", "Plans", "--branch", "mk/plans", "--plan", pfile)
        run = out["run"]
        assert run["plan_id"] == "tplan" and run["plan_status"] == "running"
        assert run["plan_sha"] == file_sha(pfile)  # from the parse AFTER set/add-run: no ⚠ at birth
        db = json.load(open(pfile))
        assert db["status"] == "running" and db["runs"] == ["pr1"]
        assert [t["id"] for t in run["tasks"]] == ["P1", "P1.1", "P1.2", "P2", "P2.1"], run["tasks"]
        t = {x["id"]: x for x in run["tasks"]}
        assert t["P1"]["plan_phase"] == "P1" and t["P1"]["route"] == "codex gpt-5 high"
        assert t["P1"]["lane_group"] == "A" and t["P1"]["parent"] is None
        assert t["P2"]["deps"] == ["P1"] and t["P2"]["plan_phase"] == "P2"
        assert t["P1.1"]["plan_task"] == "P1.1" and t["P1.1"]["parent"] == "P1"
        assert t["P1.1"]["owned"] == ["scripts/a.py"] and t["P2.1"]["parent"] == "P2"
        assert os.path.exists(os.path.join(pd, "runs", "pr1", "tasks", "P1.1", "task.json"))
        idx = {r["id"]: r for r in json.load(open(os.path.join(pd, "runs", "index.json")))}
        assert idx["pr1"]["plan"] == "tplan"
        board = open(os.path.join(pd, "runs", "pr1", "board.md")).read()
        assert "plan: `tplan` · running · sha `" in board and "⚠" not in board, board

        # plan-less runs and tasks carry no plan links (C4), in run.json and the index
        r1run = mk("run", "get", "--id", "r1")["run"]
        assert r1run["plan_id"] is None and r1run["plan_sha"] is None and r1run["plan_status"] is None
        assert all(x.get("plan_phase") is None and x.get("plan_task") is None for x in r1run["tasks"])
        assert {r["id"]: r for r in mk("run", "list")["runs"]}["r1"]["plan"] is None

        # --force init on an unapproved plan: allowed, and an event says so
        db["status"] = "draft"
        putdb(db)
        assert pk("run", "init", "--id", "pf1", "--feature", "F", "--branch", "b", "--plan", pfile,
                  "--force")["ok"] is True
        evs = [json.loads(x) for x in open(os.path.join(pd, "runs", "pf1", "events.jsonl"))]
        assert any("not approved" in x["text"] and "--force" in x["text"] for x in evs), evs
        pk("run", "archive", "--id", "pf1")

        # a plan tool that cannot even run: refused before any local write
        e = mk("run", "init", "--id", "px", "--feature", "F", "--branch", "b", "--plan", pfile, code=2,
               e=dict(env, MK_PLAN_CMD=os.path.join(tmp, "nope.py")))
        assert "cannot run" in e["error"] and not os.path.exists(os.path.join(data, "runs", "px")), e
        # plan sync on a plan-less run is refused before the plan tool is called
        assert "no plan link" in mk("plan", "sync", "--run", "r1", code=2)["error"]

        # plan sync: dry run changes nothing; P1.1 started → P1 rolled up to building
        pk("task", "set", "P1.1", "status=building")
        db = json.load(open(pfile))
        db["phases"][0]["title"] = "Phase one v2"  # started (building): warning only
        db["phases"][0]["tasks"] = [x for x in db["phases"][0]["tasks"] if x["id"] != "P1.2"]  # P1.2 gone
        db["phases"][0]["tasks"][0]["title"] = "First v2"  # started (building): warning only
        db["phases"][1]["title"] = "Phase two v2"  # planned: update
        db["phases"].append({"id": "P3", "title": "Phase three", "wave": 2, "deps": ["P2"], "owned": [],
                             "route": "", "size": "standard", "risk": "reversible", "review": "light",
                             "qa": [], "commit": "feat: c", "subagents": False, "lane_group": "B",
                             "tasks": [{"id": "P3.1", "title": "New one", "acceptance": "a4", "refs": [],
                                        "subagent": False, "owned": []}]})
        putdb(db)
        runjson = os.path.join(pd, "runs", "pr1", "run.json")
        boardp = os.path.join(pd, "runs", "pr1", "board.md")
        board = open(pk("render")["board"]).read()
        assert "⚠ plan file changed since run start" in board, board  # the file moved under the run
        before_run, before_board = open(runjson).read(), open(boardp).read()
        out = pk("plan", "sync")
        assert out["apply"] is False and open(runjson).read() == before_run
        assert open(boardp).read() == before_board  # the dry run does not even re-render
        assert any("phase P2" in c and "title" in c for c in out["changes"]), out["changes"]
        assert any("phase P3" in c and "new" in c for c in out["changes"])
        assert any("task P3.1" in c and "new" in c for c in out["changes"])
        wtxt = out["warnings"]
        assert any(w.startswith("phase P1 is building") for w in wtxt), wtxt
        assert any(w.startswith("task P1.1 is building") for w in wtxt), wtxt
        assert any("P1.2" in w and "removed" in w and "kept" in w for w in wtxt), wtxt

        # --apply: adds through the add path, updates in place, ONE event, new sha stored
        evpath = os.path.join(pd, "runs", "pr1", "events.jsonl")
        n_ev = len(open(evpath).readlines())
        out = pk("plan", "sync", "--apply")
        assert out["apply"] is True
        newev = [json.loads(x) for x in open(evpath)][n_ev:]
        assert len(newev) == 1 and newev[0]["type"] == "note" and "plan sync" in newev[0]["text"], newev
        run = pk("run", "get", "--id", "pr1")["run"]
        t = {x["id"]: x for x in run["tasks"]}
        assert "P3" in t and t["P3"]["plan_phase"] == "P3" and t["P3"]["lane_group"] == "B"
        assert "P3.1" in t and t["P3.1"]["parent"] == "P3" and t["P3.1"]["plan_task"] == "P3.1"
        assert os.path.exists(os.path.join(pd, "runs", "pr1", "tasks", "P3.1", "task.json"))
        assert t["P2"]["title"] == "Phase two v2"  # planned: updated
        assert t["P1"]["title"] == "Phase one" and t["P1.1"]["title"] == "First"  # started: untouched
        assert "P1.2" in t  # removed from the plan: never deleted
        assert run["plan_sha"] == file_sha(pfile)
        board = open(pk("render")["board"]).read()
        assert "⚠" not in board and "Phase two v2" in board, board

        # last run done → plan done; a non-last run finishing leaves the plan alone
        putdb({**json.load(open(pfile)), "status": "running"})
        pk("run", "init", "--id", "pr3", "--feature", "F3", "--branch", "b3", "--plan", pfile, "--force")
        assert json.load(open(pfile))["runs"] == ["pr1", "pf1", "pr3"]
        pk("run", "set", "--id", "pr1", "--status", "done")
        assert json.load(open(pfile))["status"] == "running"  # pr1 is not the last run
        assert pk("run", "get", "--id", "pr1")["run"]["plan_status"] == "running"
        pk("run", "set", "--id", "pr3", "--status", "done")
        assert json.load(open(pfile))["status"] == "done"  # the last run finished
        r3 = pk("run", "get", "--id", "pr3")["run"]
        assert r3["plan_status"] == "done"
        evs = [json.loads(x) for x in open(os.path.join(pd, "runs", "pr3", "events.jsonl"))]
        assert any("plan tplan done" in x["text"] for x in evs), evs

        # an unbuildable plan: refused before the plan file, the run dir or the index move
        putdb({**PLAN_BASE, "status": "approved", "phases": [
            {**PLAN_BASE["phases"][0], "tasks": [
                {"id": "P1.1", "title": "ok", "acceptance": "a", "refs": [], "subagent": False,
                 "owned": []},
                {"id": "bad id", "title": "x", "acceptance": "a", "refs": [], "subagent": False,
                 "owned": []}]}]})
        pbytes = open(pfile, "rb").read()
        e = pk("run", "init", "--id", "pb1", "--feature", "F", "--branch", "b", "--plan", pfile, code=2)
        assert "invalid task id 'bad id'" in e["error"], e
        assert open(pfile, "rb").read() == pbytes  # the plan header is untouched
        assert not os.path.exists(os.path.join(pd, "runs", "pb1"))  # no run dir
        assert "pb1" not in {r["id"] for r in json.load(open(os.path.join(pd, "runs", "index.json")))}
        putdb({**PLAN_BASE, "status": "approved"})  # the same run id now builds
        out = pk("run", "init", "--id", "pb1", "--feature", "F", "--branch", "b", "--plan", pfile)
        assert [t["id"] for t in out["run"]["tasks"]] == ["P1", "P1.1", "P1.2", "P2", "P2.1"]
        pk("run", "archive", "--id", "pb1")

        # best-effort: a missing plan file never fails run set done or render
        pfile2 = os.path.join(tmp, "tplan2.json")
        json.dump({"plan": "t2", "status": "running", "runs": [], "phases": []}, open(pfile2, "w"))
        pk("run", "init", "--id", "pr4", "--feature", "F", "--branch", "b", "--plan", pfile2, "--force")
        os.unlink(pfile2)
        out = pk("run", "set", "--id", "pr4", "--status", "done")
        assert out["ok"] is True and out["run"]["status"] == "done", out
        assert out["run"]["plan_status"] == "running"  # unchanged: the parse failed
        assert "not marked done" in out.get("warning", ""), out  # said out loud, not swallowed
        os.unlink(pfile)
        board = open(pk("render", "--run", "pr1")["board"]).read()
        assert "⚠" not in board and "plan: `tplan`" in board, board  # unreadable file: no ⚠, no crash

        # legacy path-only plan link keeps the plain path line; no plan at all → no line
        lg = os.path.join(tmp, "legacy")
        mk("run", "init", "--id", "l1", "--feature", "L", "--branch", "b", "--data", lg,
           data_arg=False, e=env)
        lb = os.path.join(lg, "runs", "l1", "board.md")
        assert "plan:" not in open(lb).read()
        lr = os.path.join(lg, "runs", "l1", "run.json")
        r = json.load(open(lr))
        r["plan"] = "plans/legacy.md"
        write_json(lr, r)
        board = open(mk("render", "--data", lg, data_arg=False, e=env)["board"]).read()
        assert "plan: `plans/legacy.md`" in board and "sha" not in board.split("\n")[2], board

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
