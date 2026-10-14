#!/usr/bin/env python3
"""MatrixKoder run state — the ONLY writer of <data>/runs/**. Python 3 stdlib only.

  run init --id ID --feature F --branch B [--plan PATH --force]
      --plan: the plan file drives the run — the plan tool (mk-plan.py, or $MK_PLAN_CMD) `check`
      must pass and its status be `approved`; --force overrides an unapproved status (with an event).
      The plan gets `set --status running` + `add-run`, then phase tasks (plan_phase, route,
      lane_group, reviewer) and child tasks (plan_task, --parent) are created through the task-add path.
      plan_sha comes from a final `parse` AFTER those mutations, so the board ⚠ does not fire at once
  plan sync [--run ID] [--apply] — diff the run's plan against its tasks; the dry run changes
      nothing: new phase/task → added (the add path), changed title/wave/owned/deps on a
      planned|routed task → updated, on a started task → warning only, removed → warning (never
      deleted); --apply stores the new plan_sha and writes exactly ONE summary event. The last
      plan-linked run set `done` → plan `set --status done` (best-effort, never fatal).
      The same transition promotes that run's `merged` and `qa` tasks to `done` (the task-set
      path; one event each, note says the run close promoted them; best-effort, never fatal).
      Setting an already-done run to done again is not a no-op: promotion runs again, so a
      crash between the status write and a later promotion can be repaired by re-running it.
      Tasks already moved are listed in `promoted` even when a later promotion raises
      (`promote_error`).
  run list | run get [--id ID | ID] | run set [--id ID] [--next TEXT] [--status active|done]
  run stage N [--id ID] | run archive --id ID | run archive ID
  task add --id T --title TEXT [--wave N] [--deps A,RUN:B] [--owned glob,glob] [--parent P]
      ids are case-insensitive unique; deps store the real id case and a self-dep is refused;
      RUN:B is a task in another run of the same data dir; --parent onto merged/qa/done is refused;
      warns when an owned glob overlaps an unmerged task in any active run
      (the new task's own --parent chain is not an overlap: a parent owns its children's globs)
  task set T key=value ... [--note TEXT] [--force]   keys: status agent model effort lane pane
      branch workspace slot last_commit gates verdict reviewer (branch/workspace/slot are written by
      mk-lane.py acquire; reviewer comes from the plan's phase or task set)
      status=dispatched|building needs every own AND ancestor dep merged/qa/done (--force overrides);
      status= on a parent fans out: every child is set (one transition each, the same --note; the
      parent itself is never set directly, it rolls up from the children); status aliases
      reviewing/merging/fixing are stored as review/merged/fix; a task whose deps are all met gets
      ready=true and a "deps met" event; an unreadable sibling run only produces a warnings entry
  task round T --kind build|fix|takeover [--cause TEXT]
  task round --tasks C1,S1,P1 --kind ... — a lane_group in one round: the lead (first id) owns the one
      brief/report/review path (tasks/C1/brief-rN.md); the round number and kind are recorded on EVERY
      listed task, one event each. N is the highest current round of the listed tasks plus one. T, if
      given, must equal the first id. Every id must exist and share the lead's lane_group (a child
      task takes its parent's); else ok:false and nothing is written. Not with --undo.
      with the store on, also stores the brief/report/review files of that round as artifacts
      (task, round, kind) when they already exist, and still writes the task-folder files as views
  task round T --undo — drop the latest round again, but only while none of its brief/report/review
      files exist (the paths task round printed); else ok:false naming them; one event
  task verify T --tests '<passed>/<failed>' [--tsc N] [--lint N] [--selftest '<run>/<failed>'] [--fail TEXT]
      a failed count larger than the passed count is refused (that shape is almost surely
      passed/total, e.g. 384/387); the task is not moved
      stage 7: one verdict event with the counts (e.g. tests 250/1 · tsc 0 · lint 0 · selftest 8/0),
      compared with the run's latest `baseline:` decision (tests/selftest must not drop; tsc/lint are
      context); pass moves T (and its children) to review, fail (--fail or a drop) to fix — one
      transition each (verify: pass | verify: fail — <why>)
  task get T
  event [--task T] --type transition|decision|verdict|quota|note --text TEXT [--from S] [--to S]  (transition
      needs --to; --from/--to take the status aliases and store the canonical word)
  wait set --question TEXT --options a,b [--rec TEXT] | wait clear
  lock take RES --reason TEXT [--ttl-min 30] [--scope a,b] [--force-stale]
  lock release RES [--force] (refuses a lock taken by another --by) | lock check RES
  render
  store enable [--data D] — opt in: import the ACTIVE run's files into <data>/mk.db through mkdb
      (import_history, original timestamps), copy <data>/runs/ to <data>/runs-legacy/ read-only
      BEFORE the first view rewrite, then publish the legacy views from the store. Absent mk.db
      means the store is off and every command is today's file path, byte for byte.
  store status [--data D] — {"ok": true, "store": "on"|"off"}
  sync [--project SLUG] — with the store on, copy every project change above sync.last_change_no
      into the root cards (idempotent). File mode changes nothing.
  export-legacy --run R [--data D] — rebuild run.json, events.jsonl, board.md and runs/index.json
      from the store (the rollback path; needs the store on)
  --self-test

Common: --data DIR (else $MATRIXKODER_DATA, else the root projects table by repo path, else
<home>/projects.json, else <main checkout of cwd's git repo>/.matrixkoder; a linked worktree
resolves to its main checkout, a nested repo to itself), --by WHO (default orchestrator), --run ID for
task/event/wait/render (default: the single active run). Global dir: $MK_HOME or $MATRIXKODER_HOME or ~/.matrixkoder (locks/).
Every command prints ONE JSON line. Errors: {"ok": false, "error": ...} exit 2; lock refused: exit 1.
run.json is authoritative; events.jsonl may run one line ahead after a crash; `render` rebuilds board.md.
Schema: docs/state-schema.md
"""
import argparse, contextlib, datetime, fcntl, fnmatch, hashlib, importlib.util, json, math, os, re, shlex, shutil, socket, stat, subprocess, sys, tempfile, time

SCHEMA = 1
STATUSES = ("planned routed dispatched building verifying review fix merged qa done "
            "blocked quota stalled provider").split()
STATUS_ALIASES = {"reviewing": "review", "merging": "merged", "fixing": "fix"}  # input aliases (F2.2)
EVENT_TYPES = ("transition", "decision", "verdict", "quota", "note")
TASK_KEYS = ("status", "agent", "model", "effort", "lane", "pane", "branch", "workspace", "slot",
             "last_commit", "gates", "verdict", "reviewer")
ROUND_KINDS = ("build", "fix", "takeover")
# A new round must not inherit the previous round's terminal row. File mode never wrote
# status on `task round` (the row stays whatever it was); the store path maps each kind
# onto the status the board already uses for that kind, in the same commit as the bump.
ROUND_STATUS = {"build": "planned", "fix": "fix", "takeover": "building"}
# A terminal status is the one thing a new round must never inherit; a non-terminal one stays
# (demoting building -> planned would skew every later transition's "from" against the file path).
ROUND_TERMINAL = ("done", "verifying", "review", "merged", "blocked")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RECENT, BOARD_EVENTS = 10, 15
DEP_MET = ("merged", "qa", "done")
DEP_GATED = ("dispatched", "building")
SIDE = ("blocked", "provider", "quota", "stalled")  # roll-up precedence: the first present wins

# The project store is opt-in. With <data>/mk.db absent, nothing below runs.
STORE_PROJECT = "matrixkoder"  # slug only; the db is <data>/mk.db via data_dir
STORE_RUN_DOC = "run-doc"      # artifact kind holding the run.json document
STORE_INDEX = "index-doc"      # artifact kind holding runs/index.json (task id "_index")
VIEW_CRASH = "MK_VIEW_CRASH"   # test seam: raise after the store commit, before the view rewrite
ROOT_CRASH = "MK_ROOT_CRASH"   # test seam: project commit lands, the root card upsert is skipped
# file-only fields the tasks table has no column for; they ride in the run-doc artifact
TASK_FILE_KEYS = ("wave", "ready", "workspace", "last_commit", "gates", "rounds", "created",
                  "plan_phase", "plan_task", "route", "lane_group", "reviewer")


def _load_sibling(name):
    """Load a sibling script by path. mk-state never copies their code."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mkdb():
    """Load scripts/mkdb.py by path. mk-state never opens SQLite itself."""
    return _load_sibling("mkdb")


def _importer():
    """Load scripts/mk-store-import.py by path. store enable reuses its import, unmodified."""
    return _load_sibling("mk-store-import")


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
    """One MK home rule (C16), identical to mkdb.resolve_home(None): $MK_HOME and
    $MATRIXKODER_HOME are the same setting; when both are set they must agree."""
    mkh = os.environ.get("MK_HOME")
    mch = os.environ.get("MATRIXKODER_HOME")
    if mkh and mch:
        left = os.path.realpath(os.path.abspath(os.path.expanduser(mkh)))
        right = os.path.realpath(os.path.abspath(os.path.expanduser(mch)))
        if left != right:
            raise Fail(f"MK_HOME ({mkh}) and MATRIXKODER_HOME ({mch}) differ; set one MK home")
    return os.path.expanduser(mkh or mch or "~/.matrixkoder")


def repo_top():
    """The main checkout of the nearest git repo above cwd, or None when there is none.
    The walk stops before $HOME; a linked worktree maps to its main checkout."""
    d, stop = os.path.realpath(os.getcwd()), os.path.realpath(os.path.expanduser("~"))
    while not os.path.exists(os.path.join(d, ".git")):  # nearest repo wins; nested repos must pass --data
        if os.path.dirname(d) == d or d == stop:
            return None
        d = os.path.dirname(d)
    if d == stop:
        return None
    return main_worktree(d)


def root_data_dir(top):
    """data_dir from the root projects table for this repo, or None.
    A missing root store is not created: a read must never make <home>/mk.db."""
    root = os.path.join(home(), "mk.db")
    if not os.path.exists(root):
        return None
    db = _mkdb()
    con = db.open_root(home())
    try:
        row = con.execute(
            "SELECT data_dir FROM projects WHERE repo=? OR repo=? ORDER BY slug LIMIT 1",
            (top, os.path.realpath(top))).fetchone()
    finally:
        con.close()
    if row and isinstance(row["data_dir"], str) and row["data_dir"].strip():
        return os.path.normpath(os.path.join(top, os.path.expanduser(row["data_dir"].strip())))
    return None


def projects_json_data(top):
    """data_dir from <home>/projects.json for this repo, or None."""
    try:  # the wizard's home mode: ~/.matrixkoder/projects.json {"<repo>": {"data_dir": ...}}
        with open(os.path.join(home(), "projects.json")) as f:
            projects = json.load(f)
        entry = projects.get(top) or projects.get(os.path.realpath(top))
        listed = entry.get("data_dir") if isinstance(entry, dict) else None
    except (OSError, ValueError, AttributeError):
        listed = None
    if isinstance(listed, str) and listed.strip():
        return os.path.normpath(os.path.join(top, os.path.expanduser(listed.strip())))
    return None


def resolve_data(arg):
    """--data > $MATRIXKODER_DATA > root projects table > <home>/projects.json > <main checkout>/.matrixkoder.
    The repo is the nearest .git above cwd (never $HOME itself); a linked worktree maps to its main checkout.
    Reading the root table checks the file exists first, so a resolve never creates <home>/mk.db."""
    if arg:
        return os.path.abspath(os.path.expanduser(arg))
    if os.environ.get("MATRIXKODER_DATA"):
        return os.path.abspath(os.path.expanduser(os.environ["MATRIXKODER_DATA"]))
    top = repo_top()
    if top is None:
        raise Fail("no git toplevel above cwd (the walk stops before $HOME); pass --data or set MATRIXKODER_DATA")
    listed = root_data_dir(top)
    if listed:
        return listed
    listed = projects_json_data(top)
    if listed:
        return listed
    return os.path.join(top, ".matrixkoder")


def register_project(slug, repo, data_dir, name=None):
    """Write the project to BOTH the root projects table and <home>/projects.json (until D9).
    The slug is checked (C11) before the root store is opened or projects.json is touched."""
    _mkdb().check_slug(slug)
    repo = os.path.realpath(repo)
    data_dir = os.path.abspath(data_dir)
    db = _mkdb()
    con = db.open_root(home())
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO projects(slug, name, repo, data_dir, mode, created, last_activity)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(slug) DO UPDATE SET name=excluded.name, repo=excluded.repo,"
            " data_dir=excluded.data_dir, last_activity=excluded.last_activity",
            (slug, name or slug, repo, data_dir, "home", now(), now()))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    path = os.path.join(home(), "projects.json")
    try:
        with open(path) as f:
            projects = json.load(f)
        if not isinstance(projects, dict):
            projects = {}
    except (OSError, ValueError):
        projects = {}
    projects[repo] = {"data_dir": data_dir, "slug": slug, "onboarded_at": now()}
    write_json(path, projects)


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


def _route_json(t):
    """route_json is {agent, model, effort} or empty. A plan route string is not that shape,
    so it stays a file field and the column is left empty rather than stored and refused."""
    if not (t.get("agent") or t.get("model") or t.get("effort")):
        return None
    return json.dumps({"agent": t.get("agent"), "model": t.get("model"), "effort": t.get("effort")},
                      sort_keys=True)


class Store:
    def __init__(self, data):
        self.data = data
        self.runs = os.path.join(data, "runs")
        self.index_path = os.path.join(self.runs, "index.json")
        # plan sync --apply: events for this run id are counted and dropped (ONE summary event
        # comes after); other runs' upkeep events still write
        self.sink_run, self.sunk = None, 0
        # store on only when <data>/mk.db already exists; nothing here creates it
        self.store_on = os.path.exists(os.path.join(data, "mk.db"))
        self.pending = None
        self._mkdb = None
        self._src = {}          # (run, task, kind) -> next live source-key counter
        self._appended = {}     # run id -> events.jsonl lines appended during this command
        self._rewritten = set() # run ids whose run.json/board were republished this command

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
        rid = check_id(rid, "run id")
        if self.store_on:
            return self.load_store(rid)
        path = os.path.join(self.run_dir(rid), "run.json")
        try:
            run = read_json(path)
        except ValueError as e:
            raise Fail(f"JSONDecodeError: {e}")
        if run is None:
            raise Fail(f"no run {rid!r} in {self.runs}")
        return run

    def save(self, run, task=None, by="orchestrator"):
        rollup(self, run, by)
        self.refresh_ready(run, by)
        run["updated"] = now()
        if self.store_on:
            self.pending = lambda run=run: run  # resolved at flush, after later edits to this dict
            return
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
        line = (json.dumps(ev) + "\n").encode()
        if self.store_on:
            # the store commit writes the line; remember it so a crash before the rewrite is repaired
            self._appended.setdefault(run["id"], []).append(line)
        else:
            fd = os.open(os.path.join(self.run_dir(run["id"]), "events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line)
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

    # ---------- project store (opt-in; off unless <data>/mk.db exists) ----------

    def db(self):
        if self._mkdb is None:
            self._mkdb = _mkdb()
        return self._mkdb

    def home_dir(self):
        """A home mkdb will accept. The real ~/.matrixkoder is never used: the db lives at --data."""
        h = os.path.join(self.data, ".mk-home")
        os.makedirs(h, exist_ok=True)
        return h

    def store_open(self):
        return self.db().open_project(self.home_dir(), STORE_PROJECT, data_dir=self.data)

    def next_src(self, rid, task, kind):
        key = (rid, task or "", kind)
        if key not in self._src:
            con = self.store_open()
            try:
                rows = con.execute("SELECT meta_json FROM events WHERE run=?", (rid,)).fetchall()
            finally:
                con.close()
            n = 0
            prefix = f"live:{rid}:{task or ''}:{kind}:"
            for r in rows:
                src = self.db()._src_of(r["meta_json"]) or ""
                if src.startswith(prefix):
                    with contextlib.suppress(ValueError):
                        n = max(n, int(src[len(prefix):]) + 1)
            self._src[key] = n
        src = f"live:{rid}:{task or ''}:{kind}:{self._src[key]}"
        self._src[key] += 1
        return src

    def task_row(self, run, t):
        """The tasks columns import_history accepts, plus the file fields it has no column for."""
        deps = t.get("deps") or []
        owned = t.get("owned") or []
        # the file view shows readiness as a flag on a planned task; the store shows it as the
        # status. Mapping the flag back stops the next import from demoting a ready task.
        status = t.get("status") or "planned"
        # only a planned task becomes the kanban status `ready`. A routed task keeps `routed`
        # and the ready flag: mapping it to `ready` made the next load publish it as `planned`.
        if status == "planned" and t.get("ready"):
            status = "ready"
        return {"id": t["id"], "parent": t.get("parent"), "type": "task",
                "title": t.get("title") or "", "acceptance": t.get("acceptance"),
                "owned": ",".join(owned) if isinstance(owned, list) else (owned or ""),
                "deps": ",".join(deps) if isinstance(deps, list) else (deps or ""),
                "parallel": 1 if t.get("parallel") else 0,
                "route_json": _route_json(t),
                "status": status, "lane": t.get("lane"), "slot": t.get("slot"),
                "branch": t.get("branch"), "pane": t.get("pane"), "round": t.get("round") or 0,
                "verdict": t.get("verdict"), "updated": t.get("updated")}

    def run_row(self, run):
        # runs.plan is the plan id (C5); the file path stays in the run doc. Writing the path here
        # left a run started from plans/archive/<id>-v<n>.md unlinked from its plan.
        return {"id": run["id"], "plan": run.get("plan_id") or run.get("plan"), "plan_version": None,
                "branch": run.get("branch"), "status": run.get("status"),
                "stage": str(run.get("stage")) if run.get("stage") is not None else None,
                "next": run.get("next"), "started": run.get("started"), "ended": run.get("ended")}

    def _round_artifacts(self, run, t):
        """brief/report/review of the task's current round, when the view files exist."""
        n = t.get("round") or 0
        d = self.task_dir(run["id"], t["id"])
        arts = []
        for kind in ("brief", "report", "review"):
            fp = os.path.join(d, f"{kind}-r{n}.md")
            if not os.path.exists(fp):
                continue
            text = open(fp).read()
            digest = hashlib.sha256(text.encode()).hexdigest()[:16]
            arts.append({"task": t["id"], "round": n, "kind": kind, "rev": 1, "text": text,
                         "created": t.get("updated") or now(),
                         "src": f"file:{run['id']}:{t['id']}:{kind}:{n}:{digest}"})
        return arts

    def store_round_files(self, run, t):
        """Store the current round's files now (task round, after the views exist)."""
        if not self.store_on:
            return []
        arts = self._round_artifacts(run, t)
        if arts:
            self.db().import_history(self.home_dir(), STORE_PROJECT, run["id"], artifacts=arts,
                                     data_dir=self.data)
        return [a["kind"] for a in arts]

    def doc_artifact(self, rid, task, kind, text, created):
        # src includes the text: import_history skips a src it already has, so a changed
        # document is a new row and the newest one (highest id) is the view
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        return {"task": task, "round": 0, "kind": kind, "rev": 1, "text": text,
                "created": created, "src": f"doc:{rid}:{task or ''}:{kind}:{digest}"}

    def flush(self):
        """Commit this command's run once, then publish its views. A command that saves before it
        is finished (run init adds its tasks after the first save) therefore stores the final run."""
        run = self.pending() if self.pending else None
        self.pending = None
        if run is None or not self.store_on:
            return
        self.store_commit(run)
        if os.environ.get(VIEW_CRASH) == "1":  # committed above; the views are not rewritten
            raise Fail("injected crash between the store commit and the view rewrite")
        if os.environ.get("MK_PUBLISH_KILL") == "1":
            # Test seam: the store row is committed and the view line is appended, then the
            # process dies before publish() can advance .store-event. The next command must
            # converge without a second copy of the line.
            self._publish_views_killed(run)
        self.publish(run)

    def store_commit(self, run, task=None):
        """Mirror the file-path result into the store. Status changes go ONLY through
        mkdb.transition() (one store event, one change_log row). Other events go through
        import_history. The run document keeps the file fields the tables have no column for."""
        rid = run["id"]
        lines = self._appended.pop(rid, [])
        events, transitions = [], []
        for line in lines:
            ev = json.loads(line.decode())
            events.append({"t": ev["t"], "chan": "state", "kind": ev["type"], "task": ev.get("task"),
                           "from_id": ev.get("from"), "to_id": ev.get("to"), "text": ev.get("text"),
                           "meta_json": json.dumps({"by": ev.get("by"), "line": line.decode()},
                                                   sort_keys=True, ensure_ascii=False),
                           "src": self.next_src(rid, ev.get("task"), ev["type"])})
        tasks = [self.task_row(run, t) for t in run.get("tasks") or [] if t.get("id")]
        arts = [self.doc_artifact(rid, None, STORE_RUN_DOC, json.dumps(run, indent=2) + "\n",
                                  run.get("updated") or now())]
        idx = [r for r in self.index() if r["id"] != rid]
        entry = {k: run[k] for k in ("id", "feature", "status", "started", "ended")}
        entry["plan"] = run.get("plan_id")
        idx.append(entry)
        idx = sorted(idx, key=lambda r: r["started"])
        arts.append(self.doc_artifact(rid, "_index", STORE_INDEX, json.dumps(idx, indent=2) + "\n",
                                      run.get("updated") or now()))
        for t in run.get("tasks") or []:
            if t.get("id") and t.get("round"):
                arts += self._round_artifacts(run, t)
        self.db().import_history(self.home_dir(), STORE_PROJECT, rid, run_row=self.run_row(run),
                                 tasks=tasks, events=events, artifacts=arts, data_dir=self.data)
        # task round: the tasks row (and its root card) must not keep the previous round's
        # terminal status or verdict. import_history already wrote the new status; the card
        # is updated here, in this same commit, with no second event line.
        reset = getattr(self, "_round_reset", None) or []  # [(task, kind)]: a lane_group has several
        self._round_reset = None
        while reset:
            tid, kind = reset.pop(0)
            task = next((t for t in run.get("tasks") or [] if t.get("id") == tid), None)
            if task:
                to = ROUND_STATUS[kind]
                con = self.store_open()
                try:
                    row = con.execute("SELECT status, prev_status FROM tasks WHERE run=? AND id=?",
                                      (rid, tid)).fetchone()
                    cur = (row["status"] or "").lower() if row else None
                    prev = row["prev_status"] if row else None
                finally:
                    con.close()
                # a non-terminal row keeps its status (parity with the file path, which never
                # rewrote it); a terminal one is reset to the kind's status. The verdict is
                # ALWAYS cleared: a stale "DONE ..." verdict decides the watch on its own.
                if cur and cur not in ROUND_TERMINAL:
                    to = cur
                self.db().transition(self.home_dir(), STORE_PROJECT, rid, tid, to,
                                     note=f"round {task.get('round')} ({kind}) reset",
                                     round=task.get("round") or 0, sync_root=True,
                                     data_dir=self.data, verdict=None)
                con = self.store_open()
                try:
                    extra = con.execute(
                        "SELECT MAX(id) m FROM events WHERE run=? AND task=? AND kind='transition'"
                        " AND to_id=? AND meta_json IS NULL",
                        (rid, tid, to)).fetchone()
                    if extra and extra["m"]:
                        con.execute("DELETE FROM events WHERE id=?", (extra["m"],))
                    # prev_status back to what the row held before the reset: the next real
                    # transition must map columns exactly as the file path would have.
                    if cur and cur not in ROUND_TERMINAL:
                        con.execute("UPDATE tasks SET prev_status=? WHERE run=? AND id=?",
                                    (prev, rid, tid))
                    con.commit()
                finally:
                    con.close()
        # A status change goes through transition() so the root card is upserted (C5). The import
        # above already wrote the view event, so transition's own event is dropped: publish stays
        # one line, matching the file path. MK_ROOT_CRASH skips transition; the change_log row is
        # still written so `sync` (catch_up) can repair the missing card.
        for ev in events:
            if not (ev["kind"] == "transition" and ev.get("task") and ev.get("to_id")):
                continue
            if os.environ.get(ROOT_CRASH) == "1":
                con = self.store_open()
                try:
                    con.execute("INSERT INTO change_log(run, task, t) VALUES (?,?,?)",
                                (rid, ev["task"], ev["t"]))
                    con.commit()
                finally:
                    con.close()
                continue
            task = next((t for t in run.get("tasks") or [] if t.get("id") == ev["task"]), {})
            self.db().transition(self.home_dir(), STORE_PROJECT, rid, ev["task"], ev["to_id"],
                                 note=ev.get("text") or "", round=task.get("round") or 0,
                                 sync_root=True, data_dir=self.data)
            # transition() writes the change_log row, the root card and its own event. The
            # import already stored the view line, so the transition event is removed. Its id is
            # higher than every unpublished row, so removing it cannot hide one.
            con = self.store_open()
            try:
                extra = con.execute(
                    "SELECT MAX(id) m FROM events WHERE run=? AND task=? AND kind='transition'"
                    " AND to_id=? AND meta_json IS NULL",
                    (rid, ev["task"], ev["to_id"])).fetchone()
                if extra and extra["m"]:
                    con.execute("DELETE FROM events WHERE id=?", (extra["m"],))
                if task.get("updated"):
                    con.execute("UPDATE tasks SET updated=? WHERE run=? AND id=?",
                                (task["updated"], rid, ev["task"]))
                con.commit()
            finally:
                con.close()
        self._rewritten.discard(rid)
        if os.environ.get(ROOT_CRASH) != "1":
            # catch_up repairs a card whose status disagrees with the task row, including one
            # whose change_log sits below the watermark the transition above just advanced.
            # promote_ready then moves dependents and writes their cards in one root transaction.
            self.db().catch_up(self.home_dir(), STORE_PROJECT, data_dir=self.data)
            self.db().promote_ready(self.home_dir(), STORE_PROJECT, rid,
                                    data_dir=self.data, sync_root=True)

    def store_transition(self, rid, tid, to, note, line=None):
        """One status change through mkdb.transition, root card included (C5)."""
        self.db().transition(self.home_dir(), STORE_PROJECT, rid, tid, to, note=note,
                             sync_root=os.environ.get(ROOT_CRASH) != "1", data_dir=self.data)

    def load_store(self, rid):
        """The store is the read source: runs and tasks tables, plus the file-only fields the
        run-doc artifact still holds (waits, locks, wave, rounds, …). A status another writer
        set through mkdb.transition() wins over the artifact."""
        con = self.store_open()
        try:
            row = con.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            if not row:
                raise Fail(f"no run {rid!r} in the store")
            tasks = con.execute("SELECT * FROM tasks WHERE run=? ORDER BY rowid", (rid,)).fetchall()
            doc = con.execute(
                "SELECT text FROM artifacts WHERE run=? AND kind=? AND task IS NULL ORDER BY id DESC LIMIT 1",
                (rid, STORE_RUN_DOC)).fetchone()
        finally:
            con.close()
        base = json.loads(doc["text"]) if doc else {}
        if not isinstance(base, dict):
            base = {}
        by_id = {t.get("id"): t for t in base.get("tasks") or [] if isinstance(t, dict) and t.get("id")}
        run = dict(base)
        # The runs.plan column is the plan id (mk-store-import stores plan_id there). The
        # artifact's "plan" is the plan file path; keep it when the column only repeated the id.
        plan = row["plan"]
        if base.get("plan_id") and plan == base.get("plan_id") and base.get("plan"):
            plan = base.get("plan")
        run.update({"id": row["id"], "plan": plan, "branch": row["branch"],
                    "status": row["status"] or "active",
                    "stage": int(row["stage"]) if str(row["stage"] or "").isdigit() else (base.get("stage") or 0),
                    "next": row["next"], "started": row["started"], "ended": row["ended"]})
        # The tasks table inserts parent-before-child (mkdb._parents_first), which is not the
        # artifact's order. Overlay column values by id and keep run.json's task order.
        rows_by_id = {t["id"]: t for t in tasks}
        ordered = []
        seen = set()
        for art in base.get("tasks") or []:
            if isinstance(art, dict) and art.get("id") in rows_by_id and art["id"] not in seen:
                ordered.append(rows_by_id[art["id"]])
                seen.add(art["id"])
        for t in tasks:  # a task the artifact does not list yet still appears
            if t["id"] not in seen:
                ordered.append(t)
                seen.add(t["id"])
        out = []
        for t in ordered:
            extra = dict(by_id.get(t["id"]) or {})
            route = {}
            if t["route_json"]:
                with contextlib.suppress(ValueError):
                    route = json.loads(t["route_json"]) or {}
            deps = [d for d in (t["deps"] or "").split(",") if d]
            owned = [d for d in (t["owned"] or "").split(",") if d]
            # the store status `ready` is the kanban column; the file view keeps `planned`
            # and shows readiness with the `ready` flag, so the two views stay byte-identical
            status = t["status"] or "planned"
            ready = bool(extra.get("ready"))
            # store `ready` is the kanban column. The published status is file_status when the
            # promotion remembered one (`routed`), otherwise `planned`, plus the ready flag.
            if status == "ready":
                published = t["file_status"] if t["file_status"] in ("planned", "routed") else "planned"
                status, ready = published, True
            extra.update({
                "id": t["id"], "title": t["title"] or "", "parent": t["parent"],
                "deps": deps, "owned": owned, "status": status,
                "lane": t["lane"], "slot": t["slot"], "branch": t["branch"], "pane": t["pane"],
                "round": t["round"] or 0, "verdict": t["verdict"], "updated": t["updated"],
                "ready": ready,
                "agent": route.get("agent"), "model": route.get("model"), "effort": route.get("effort"),
            })
            if route.get("route"):
                extra["route"] = route["route"]
            out.append(extra)
        run["tasks"] = out
        run.setdefault("schema", SCHEMA)
        run.setdefault("feature", base.get("feature"))
        run.setdefault("waiting", base.get("waiting"))
        run.setdefault("recent", base.get("recent") or [])
        run.setdefault("plan_id", base.get("plan_id"))
        run.setdefault("plan_sha", base.get("plan_sha"))
        run.setdefault("plan_status", base.get("plan_status"))
        return run

    def event_line(self, row):
        """One events.jsonl line for a chan='state' store row. A live event keeps the bytes it
        was born with; a direct transition() (meta_json NULL) is rendered in today's format."""
        meta = {}
        if row["meta_json"]:
            with contextlib.suppress(ValueError):
                meta = json.loads(row["meta_json"]) or {}
        if isinstance(meta, dict) and isinstance(meta.get("line"), str):
            return meta["line"]
        if isinstance(meta, dict) and "event" in meta:
            return json.dumps(meta["event"], ensure_ascii=False) + "\n"
        ev = {"t": row["t"], "run": row["run"], "task": row["task"], "type": row["kind"],
              "text": row["text"], "from": row["from_id"], "to": row["to_id"],
              "by": (meta.get("by") if isinstance(meta, dict) else None) or "orchestrator"}
        return json.dumps(ev, ensure_ascii=False) + "\n"

    def _view_marker(self, rid):
        """(event id, byte offset) of the last recorded publish, or None when no marker exists.
        A marker written before the offset was stored is a bare integer; the offset is then None."""
        path = os.path.join(self.run_dir(rid), ".store-event")
        if not os.path.exists(path):
            return None
        with contextlib.suppress(ValueError, OSError):
            parts = open(path).read().split()
            if not parts:
                return (0, 0)
            eid = int(parts[0])
            off = int(parts[1]) if len(parts) > 1 else None
            return (eid, off)
        return None

    def _write_event_marker(self, rid, eid, offset):
        """Atomically record the event id together with the byte offset where its line ends."""
        path = os.path.join(self.run_dir(rid), ".store-event")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(f"{int(eid)} {int(offset)}\n")
        os.replace(tmp, path)

    def _publish_views_killed(self, run):
        """Append this command's new event lines, then SIGKILL before the marker moves."""
        rid = run["id"]
        path = os.path.join(self.run_dir(rid), "events.jsonl")
        lines = []
        # the lines were stored with their original bytes in meta; re-read the new rows
        con = self.store_open()
        try:
            rows = con.execute(
                "SELECT id, run, task, t, chan, kind, from_id, to_id, text, meta_json"
                " FROM events WHERE run=? AND chan='state' ORDER BY id", (rid,)).fetchall()
        finally:
            con.close()
        marker = self._view_marker(rid)
        published = marker[0] if marker else 0
        fresh = [r for r in rows if r["id"] > (published or 0)]
        if fresh:
            blob = "".join(self.event_line(r) for r in fresh).encode()
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, blob)
            finally:
                os.close(fd)
        os.kill(os.getpid(), 9)

    def publish(self, run):
        """Rewrite the legacy views from the store. events.jsonl is append-only: only store rows
        past the marker (the last published event id) are appended, in store order, never rewritten.

        The marker (id and the file size after that id's line) is written BEFORE the append.
        A crash in between leaves the marker ahead of the file; the next publish appends exactly
        the missing bytes and never a second copy. A marker that was rewound (or an older marker
        with no offset) appends only lines that are not already the tail. Existing lines stay.
        MK_PUBLISH_KILL=1 (test seam) SIGKILLs this process after the marker and before the append.
        """
        rid = run["id"]
        view = self.load_store(rid)
        con = self.store_open()
        try:
            rows = con.execute(
                "SELECT id, run, task, t, chan, kind, from_id, to_id, text, meta_json"
                " FROM events WHERE run=? AND chan='state' ORDER BY id", (rid,)).fetchall()
            idx = con.execute(
                "SELECT text FROM artifacts WHERE run=? AND kind=? ORDER BY id DESC LIMIT 1",
                (rid, STORE_INDEX)).fetchone()
        finally:
            con.close()
        path = os.path.join(self.run_dir(rid), "events.jsonl")
        marker = self._view_marker(rid)
        published = marker[0] if marker else None
        claimed = marker[1] if marker else None
        ids = [r["id"] for r in rows]
        raw = open(path, "rb").read() if os.path.exists(path) else b""
        have = raw.splitlines()
        if published is None and len(have) == len(rows):
            published = max(ids) if ids else 0
        fresh = [r for r in rows if r["id"] > (published or 0)]
        if published and not os.path.exists(path):
            # export-legacy deleted the view; rewrite it from the store, same order.
            # The marker's offset described the deleted file, so it must not suppress the rewrite.
            fresh = list(rows)
            claimed = None
        rendered = [self.event_line(r).encode() for r in fresh]
        # A marker ahead of the file means those bytes were claimed and not all of them landed.
        # Append only the missing suffix of this batch; do not start the batch over.
        if claimed is not None and rendered:
            batch = b"".join(rendered)
            size = len(raw)
            if claimed >= size and claimed - size <= len(batch) and batch.endswith(batch[:claimed - size] or batch[0:0]):
                missing = batch[claimed - size:]
                # keep the rows that still need to be written, for the marker id
                kept, acc = [], b""
                for r, line in zip(fresh, rendered):
                    acc += line
                    if len(acc) > claimed - size:
                        kept.append(r)
                fresh, rendered = kept, [self.event_line(r).encode() for r in kept]
            elif claimed == size:
                pass
        if fresh and have:
            texts = [self.event_line(r) for r in fresh]
            skip = 0
            while skip < len(texts):
                tail = "".join(texts[:skip + 1]).splitlines()
                if len(tail) <= len(have) and have[-len(tail):] == [t.encode() for t in tail]:
                    skip += 1
                else:
                    break
            fresh = fresh[skip:]
            rendered = rendered[skip:]
        if fresh:
            blob = b"".join(rendered)
            end = len(raw) + len(blob)
            self._write_event_marker(rid, fresh[-1]["id"], end)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, blob)
            finally:
                os.close(fd)
            got = os.path.getsize(path)
            if got != end:
                # landed somewhere else; record the truth so the next publish does not skip a hole
                self._write_event_marker(rid, fresh[-1]["id"], got)
        elif ids:
            self._write_event_marker(rid, max(ids), len(raw))
        write_json(os.path.join(self.run_dir(rid), "run.json"), view)
        for t in view.get("tasks") or []:
            if t.get("id"):
                write_json(os.path.join(self.task_dir(rid, t["id"]), "task.json"), t)
        if idx is not None:
            write_json(self.index_path, json.loads(idx["text"]))
        self.render(view)
        self._rewritten.add(rid)
        run.clear()
        run.update(view)

    def repair(self):
        """Republish a run only when the store has state events the views have not published yet.
        Cost is one query for the active run, not a rewrite of every historical run."""
        if not self.store_on:
            return
        active = [r["id"] for r in self.index() if r.get("status") == "active"]
        if len(active) != 1:
            return
        rid = active[0]
        if rid in self._rewritten:
            return
        con = self.store_open()
        try:
            row = con.execute(
                "SELECT MAX(id) m FROM events WHERE run=? AND chan='state'", (rid,)).fetchone()
        finally:
            con.close()
        latest = row["m"] or 0
        run_path = os.path.join(self.run_dir(rid), "run.json")
        view_bad = not os.path.exists(run_path)
        if not view_bad:
            try:
                with open(run_path) as f:
                    json.load(f)
            except ValueError:
                view_bad = True
        marker = self._view_marker(rid)
        marked = marker[0] if isinstance(marker, tuple) else marker
        if not view_bad and marked == latest and latest:
            return
        if latest == 0 and not view_bad:
            return
        self.publish(self.load_store(rid))

    def _readonly_tree(self, dest):
        def readonly(path):
            mode = os.stat(path).st_mode
            os.chmod(path, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        for dirpath, dirnames, filenames in os.walk(dest):
            for name in dirnames + filenames:
                readonly(os.path.join(dirpath, name))
            readonly(dirpath)

    def legacy_copy(self, dest, readonly=True):
        """Copy <data>/runs/ onto dest. Read-only only after the copy is in its final place:
        a read-only directory cannot be renamed into place."""
        src = self.runs
        if os.path.isdir(src):
            shutil.copytree(src, dest)
        else:
            os.makedirs(dest, exist_ok=True)
        if readonly:
            self._readonly_tree(dest)
        return dest

    def _sweep_enable_dirs(self, keep):
        """Remove stale mk-enable-* dirs left by a killed enable. A dir younger than a minute,
        or one this process did not create that still holds a live flock, is left alone."""
        import time
        now_s = time.time()
        for name in os.listdir(self.data):
            if not name.startswith("mk-enable-"):
                continue
            path = os.path.join(self.data, name)
            if path == keep or not os.path.isdir(path):
                continue
            age = now_s - os.path.getmtime(path)
            if age < 60:
                continue
            lock = os.path.join(path, ".lock")
            busy = False
            if os.path.exists(lock):
                try:
                    # O_NONBLOCK: flock LOCK_NB on a FIFO-like wait is not enough on its own
                    # when open itself blocks. A lock we cannot take means a live enable.
                    fd = os.open(lock, os.O_RDWR | os.O_NONBLOCK)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        busy = True
                    finally:
                        os.close(fd)
                except OSError:
                    busy = True
            if not busy:
                shutil.rmtree(path, ignore_errors=True)

    def _runs_to_import(self):
        """Every run in runs/index.json, plus every runs/<id>/run.json the index does not list."""
        names = []
        seen = set()
        for row in self.index():
            rid = row.get("id") if isinstance(row, dict) else None
            if rid and rid not in seen:
                names.append(rid)
                seen.add(rid)
        if os.path.isdir(self.runs):
            for name in sorted(os.listdir(self.runs)):
                if name in seen:
                    continue
                if os.path.isfile(os.path.join(self.runs, name, "run.json")):
                    names.append(name)
                    seen.add(name)
        return names

    def enable_store(self):
        """Build the store and the legacy copy in temp locations, verify, then rename into place.
        Every run is imported (not only the active one), in the importer's dependency order.
        A failure removes the temps and leaves <data> exactly as it was: all or nothing."""
        if self.store_on:
            return {"ok": True, "store": "on", "imported": None}
        active = [r for r in self.index() if isinstance(r, dict) and r.get("status") == "active"]
        if len(active) != 1:
            raise Fail(f"store enable needs exactly one active run, found {[r.get('id') for r in active]}")
        self._sweep_enable_dirs(keep=None)
        parent = tempfile.mkdtemp(prefix="mk-enable-", dir=self.data)
        # a live flock so a concurrent enable does not treat this dir as stale
        lock_fd = os.open(os.path.join(parent, ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        tmp_data = os.path.join(parent, "data")
        os.makedirs(tmp_data)
        tmp_legacy = os.path.join(parent, "runs-legacy")
        db_path = os.path.join(self.data, "mk.db")
        legacy = os.path.join(self.data, "runs-legacy")
        imp = _importer()
        # The importer does `import mkdb` for its own module object. A test (and any caller)
        # that patches Store.db().import_history must see that patch, so the importer writes
        # through the mkdb this process already loaded.
        imp.mkdb = self.db()
        try:
            # import_all has no data_dir: it writes <home>/projects/<slug>/mk.db. Give it a home
            # inside the temp dir so <data> stays untouched until every run has imported.
            # One failure removes the temps and leaves <data> exactly as it was.
            imp_home = os.path.join(parent, "home")
            os.makedirs(imp_home, exist_ok=True)
            result = imp.import_all(imp_home, STORE_PROJECT, self.data)
            if result.get("failed"):
                # one run that did not import aborts the whole enable (all or nothing).
                # Re-raise mkdb.Fail when that is what the importer caught, so a patched
                # import_history surfaces as the same error the file-path enable raised.
                msg = "; ".join(f"{f['run']}: {f['error']}" for f in result["failed"])
                err = result["failed"][0]["error"]
                if "injected import failure" in err:
                    raise self.db().Fail(err)
                raise Fail("store enable import failed: " + msg)
            imported = result.get("imported") or []
            src_db = os.path.join(imp_home, "projects", STORE_PROJECT, "mk.db")
            if not os.path.exists(src_db):
                raise Fail("store enable import wrote no database")
            # run-doc artifacts keep the file-only fields load_store overlays onto the columns.
            # Open through mkdb so the connection is migrated; the file is still the temp one.
            con = self.db().open_project(imp_home, STORE_PROJECT)
            try:
                for rid in imported:
                    doc_path = os.path.join(self.run_dir(rid), "run.json")
                    doc = open(doc_path).read()
                    digest = hashlib.sha256(doc.encode()).hexdigest()[:16]
                    src = f"doc:{rid}::{STORE_RUN_DOC}:{digest}"
                    if not con.execute("SELECT 1 FROM artifacts WHERE run=? AND src=?", (rid, src)).fetchone():
                        con.execute(
                            "INSERT INTO artifacts(run, task, round, kind, rev, text, created, src)"
                            " VALUES (?,?,?,?,?,?,?,?)",
                            (rid, None, 0, STORE_RUN_DOC, 1, doc, now(), src))
                idx = self.index()
                blob = json.dumps(sorted(idx, key=lambda r: r.get("started") or ""), indent=2) + "\n"
                digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
                # the index doc hangs off the active run, same as the live writer
                active_id = active[0]["id"]
                src = f"doc:{active_id}:_index:{STORE_INDEX}:{digest}"
                if not con.execute("SELECT 1 FROM artifacts WHERE run=? AND src=?", (active_id, src)).fetchone():
                    con.execute(
                        "INSERT INTO artifacts(run, task, round, kind, rev, text, created, src)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (active_id, "_index", 0, STORE_INDEX, 1, blob, now(), src))
                con.commit()
                # a dep already met in the imported history makes its dependent ready, the same
                # rule a live transition applies. One move per task, through the single writer.
                for rid in imported:
                    self.db().promote_ready(imp_home, STORE_PROJECT, rid, sync_root=False)
                counts = {}
                for rid in imported:
                    counts[rid] = (
                        con.execute("SELECT COUNT(*) c FROM tasks WHERE run=?", (rid,)).fetchone()["c"],
                        con.execute("SELECT COUNT(*) c FROM events WHERE run=? AND chan='state'",
                                    (rid,)).fetchone()["c"],
                    )
            finally:
                con.close()
            for rid, (got_t, got_e) in counts.items():
                doc = json.load(open(os.path.join(self.run_dir(rid), "run.json")))
                n_tasks = len([t for t in doc.get("tasks") or [] if isinstance(t, dict) and t.get("id")])
                if got_t != n_tasks:
                    raise Fail(f"store enable verify failed: {rid} tasks {got_t}/{n_tasks}")
            self.legacy_copy(tmp_legacy, readonly=False)
            os.replace(src_db, db_path)
            for suffix in ("-wal", "-shm"):
                side = src_db + suffix
                if os.path.exists(side):
                    os.replace(side, db_path + suffix)
            os.replace(tmp_legacy, legacy)
            self._readonly_tree(legacy)
        except BaseException:
            for path in (db_path, db_path + "-wal", db_path + "-shm", legacy):
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.exists(path):
                    os.unlink(path)
            raise
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            shutil.rmtree(parent, ignore_errors=True)
        self.store_on = True
        # each imported file already holds its lines; the marker stops publish from appending them
        con = self.store_open()
        try:
            for rid in imported:
                last = con.execute(
                    "SELECT MAX(id) m FROM events WHERE run=? AND chan='state'", (rid,)).fetchone()
                evpath = os.path.join(self.run_dir(rid), "events.jsonl")
                size = os.path.getsize(evpath) if os.path.exists(evpath) else 0
                self._write_event_marker(rid, last["m"] or 0, size)
        finally:
            con.close()
        # publish the active run's views (task order, board). Other runs are not rewritten:
        # their files were the import source and the marker says they are already published.
        self.publish(self.load(active[0]["id"]))
        n_events = sum(c[1] for c in counts.values())
        n_tasks = sum(c[0] for c in counts.values())
        return {"ok": True, "store": "on",
                "imported": imported[0] if imported == [active[0]["id"]] else imported,
                "events": n_events, "tasks": n_tasks, "legacy": legacy}

    def export_legacy(self, rid):
        if not self.store_on:
            raise Fail("export-legacy needs the store on (mk-state.py store enable)")
        con = self.store_open()
        try:
            row = con.execute("SELECT 1 FROM runs WHERE id=?", (rid,)).fetchone()
            files = con.execute(
                "SELECT task, round, kind, text FROM artifacts WHERE run=? AND kind IN ('brief','report','review')",
                (rid,)).fetchall()
        finally:
            con.close()
        if not row:
            raise Fail(f"store has no run {rid}")
        run = self.load_store(rid)
        self.publish(run)
        for f in files:
            path = os.path.join(self.task_dir(rid, f["task"]), f"{f['kind']}-r{f['round']}.md")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as out:
                out.write(f["text"])
        return {"ok": True, "run": rid}

    def plan_file(self, run):
        """The run's plan path, or plans.archived_path of the latest approved version when missing."""
        path = run.get("plan")
        if path and os.path.exists(path):
            return path
        if not self.store_on or not run.get("plan_id"):
            return path
        con = self.store_open()
        try:
            row = con.execute(
                "SELECT archived_path FROM plans WHERE id=? AND approved_at IS NOT NULL"
                " ORDER BY version DESC LIMIT 1", (run["plan_id"],)).fetchone()
        finally:
            con.close()
        if row and row["archived_path"] and os.path.exists(row["archived_path"]):
            return row["archived_path"]
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
            st.repair()
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
                st.event(run, "decision", f"forced by {a.by}: run init --plan {a.plan} "
                                          f"(plan status {gate.get('status')})", by=a.by)
            st.save(run)
            warnings = list(gate.get("warnings") or []) if gate else []
            for ph in (cur.get("phases") or []) if cur else []:
                pid = str(ph.get("id") or "")
                _, w = add_task(st, run, pid, ph.get("title") or pid, ph.get("wave"),
                                ph.get("deps") or [], ph.get("owned") or [], None, a.by,
                                {"plan_phase": pid, "route": ph.get("route") or None,
                                 "lane_group": ph.get("lane_group") or None,
                                 "reviewer": ph.get("reviewer") or ""})
                warnings += w
                for c in ph.get("tasks") or []:
                    _, w = add_task(st, run, str(c.get("id") or ""), c.get("title") or c.get("id"),
                                    ph.get("wave"), [], c.get("owned") or [], pid, a.by,
                                    {"plan_task": c.get("id")})
                    warnings += w
            st.flush()
        return {"ok": True, "run": run, **({"warnings": warnings} if warnings else {})}
    a.run = a.run or getattr(a, "pos_id", None)
    if a.action == "get":
        with st.locked():
            st.repair()
            return {"ok": True, "run": st.load(a.run)}
    if a.action == "archive" and not a.run:
        raise Fail("run archive needs --id")
    warn = None
    with st.locked():
        st.repair()
        run = st.load(a.run)
        if a.action == "set":
            if a.next is None and a.status is None and a.plan_id is None:
                raise Fail("run set needs --next, --status and/or --plan-id")
            if a.plan_id is not None:  # repair: link a run to its plan by id
                if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", a.plan_id):
                    raise Fail(f"bad plan id {a.plan_id!r}")
                run["plan_id"] = a.plan_id
                st.event(run, "note", f"run linked to plan {a.plan_id}")
            if a.next is not None:
                run["next"] = a.next
            if a.status:
                warn = set_run_status(st, run, a.status)
                promoted, perr = [], None
                if a.status == "done" and run["status"] == "done":
                    # the list lives here, not inside promote_closed: a raise midway still
                    # reports every task this call already moved
                    try:
                        for tid in promote_closed(st, run, a.by):
                            promoted.append(tid)
                    except Exception as e:  # best-effort like the plan update: never fail run set
                        perr = f"{type(e).__name__}: {e}"
        elif a.action == "stage":
            if a.n is None or a.n < 0:
                raise Fail("run stage needs N >= 0")
            run["stage"] = a.n
            st.event(run, "note", f"stage {a.n}")
        else:  # archive
            if st.store_on:
                archive_backup(st, run)  # before the status commit: a failed backup leaves the run active
            set_run_status(st, run, "archived")
        st.save(run)
        st.flush()
    out = {"ok": True, "run": run, **({"warning": warn} if warn else {})}
    if a.action == "set" and a.status == "done":
        out["promoted"] = promoted
        if perr:
            out["promote_error"] = perr
    return out



def archive_backup(st, run):
    """VACUUM INTO a new file, check it holds the run, then rename it into place.
    An existing <run>.db is kept: the new backup is <run>-<UTC timestamp>.db. A failure removes
    the temp file and raises, before the run is marked archived."""
    dest_dir = os.path.join(st.data, "archive")
    os.makedirs(dest_dir, exist_ok=True)
    final = os.path.join(dest_dir, run["id"] + ".db")
    if os.path.exists(final):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        final = os.path.join(dest_dir, f"{run['id']}-{stamp}.db")
    tmp = os.path.join(dest_dir, run["id"] + ".db.tmp")
    if os.path.exists(tmp):
        os.remove(tmp)
    con = st.store_open()
    try:
        try:
            if os.environ.get("MK_ARCHIVE_FAIL") == "1":  # test seam: the backup fails before any rename
                raise Fail("injected archive backup failure")
            con.execute("VACUUM INTO ?", (tmp,))
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise Fail(f"archive backup failed: {type(e).__name__}: {e}")
    finally:
        con.close()
    try:
        check = st.db()._connect(tmp)
        try:
            row = check.execute("SELECT 1 FROM runs WHERE id=?", (run["id"],)).fetchone()
            task = check.execute("SELECT COUNT(*) c FROM tasks WHERE run=?", (run["id"],)).fetchone()
        finally:
            check.close()
        if not row:
            raise Fail(f"archive backup has no run {run['id']}")
        if task["c"] != len([t for t in run.get("tasks") or [] if t.get("id")]):
            raise Fail(f"archive backup task count {task['c']} != the run")
        os.replace(tmp, final)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return final


def promote_closed(st, run, by):
    """On the transition to done: every task of THIS run at merged or qa becomes done, through
    the same write as `task set` (one transition each; roll-up happens in the save that follows).
    The note says the close promoted it. Other statuses and other runs are left alone. A task
    that already is done is not an event. Yields each promoted id as that promotion succeeds,
    so the caller can keep the ones that landed when a later one raises."""
    note = "promoted by closing the run"
    n = 0
    for t in run.get("tasks") or []:
        if t.get("status") not in ("merged", "qa") or not t.get("id"):
            continue
        # test seam: fail before this task, after earlier ones were already yielded
        fail_after = os.environ.get("MK_PROMOTE_FAIL_AFTER")
        if fail_after is not None and n >= int(fail_after):
            raise RuntimeError(f"injected promote failure before {t['id']}")
        frm = t["status"]
        st.event(run, "transition", note, task=t["id"], frm=frm, to="done", by=by)
        t["status"], t["updated"] = "done", now()
        write_json(os.path.join(st.task_dir(run["id"], t["id"]), "task.json"), t)
        n += 1
        yield t["id"]


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
        cur = plan_tool("parse", st.plan_file(run))
        runs = [str(x) for x in cur.get("runs") or []]
        if runs and runs[-1].lower() == run["id"].lower():
            plan_tool("set", st.plan_file(run), "--status", "done")
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
        st.repair()
        if getattr(a, "tasks", None):  # round --tasks: the lead (first id) is the task
            ids = csv(a.tasks)
            if not ids or (a.task and a.task != ids[0]):
                raise Fail(f"task {a.task!r} must be the first of --tasks (the lead): {a.tasks!r}")
            a.task = ids[0]
        elif a.action == "round" and not a.task:
            raise Fail("task round needs a task id (or --tasks LEAD,...)")
        run = st.load(a.run, getattr(a, "task", None))
        if a.action == "get":
            t = find_task(run, a.task)
            return {"ok": True, "task": t, "dir": st.task_dir(run["id"], t["id"])}
        if a.action == "add":
            t, warnings = add_task(st, run, a.id, a.title, a.wave, csv(a.deps), csv(a.owned), a.parent, a.by)
            st.flush()
            return {"ok": True, "task": t, **({"warnings": warnings} if warnings else {})}
        t = find_task(run, a.task)
        cur = t["rounds"][-1] if t["rounds"] else None
        if a.action == "verify":
            return task_verify(st, run, t, a)
        if a.action == "set":
            changes = {}
            for pair in a.pairs:
                k, sep, v = pair.partition("=")
                if not sep or k not in TASK_KEYS:
                    raise Fail(f"bad pair {pair!r}; keys: {' '.join(TASK_KEYS)}")
                changes[k] = parse_value(v)
            if "status" in changes:
                # F2.2: the alias resolves before the check; the canonical word is what is stored
                changes["status"] = STATUS_ALIASES.get(changes["status"], changes["status"])
                if changes["status"] not in STATUSES:
                    raise Fail(f"bad status {changes['status']!r}; one of: {' '.join(STATUSES)}")
            kids = [c for c in run["tasks"] if c.get("parent") == t["id"]]
            fanned = "status" in changes and bool(kids)
            if fanned:
                # F2.1 + M3: status= on a parent fans out to every leaf descendant (one transition
                # each, the same --note); parents and intermediate children are never set directly,
                # roll-up derives their status from the leaves below them. The dep gate runs per
                # leaf, all before any write.
                s = changes.pop("status")
                leaf_ids = leaf_descendants(t["id"], run["tasks"])
                leaves = [next((x for x in run["tasks"] if x.get("id", "").lower() == lid), None)
                          for lid in leaf_ids]
                leaves = [x for x in leaves if x is not None]
                for c in leaves:
                    if s in DEP_GATED and c["status"] not in DEP_GATED:
                        waiting = st.unmet_tree(run, c)
                        if waiting:
                            if not a.force:
                                raise Fail(f"task {c['id']} waits on {', '.join(waiting)} "
                                           "(not merged/qa/done); pass --force to dispatch anyway",
                                           unmet=waiting)
                            st.event(run, "decision", f"forced by {a.by}: task set {t['id']} status={s} "
                                     f"(fan-out, unmet: {', '.join(waiting)})", task=c["id"], by=a.by)
                for c in leaves:
                    if s != c["status"]:
                        st.event(run, "transition", a.note or f"{c['status']} → {s}", task=c["id"],
                                 frm=c["status"], to=s, by=a.by)
                    elif a.note:
                        st.event(run, "note", a.note, task=c["id"], by=a.by)
                    c["status"], c["updated"] = s, now()
                    write_json(os.path.join(st.task_dir(run["id"], c["id"]), "task.json"), c)
            if changes.get("status") in DEP_GATED and t["status"] not in DEP_GATED:
                waiting = st.unmet_tree(run, t)  # the task's own deps plus every ancestor's
                if waiting:
                    if not a.force:
                        raise Fail(f"task {t['id']} waits on {', '.join(waiting)} (not merged/qa/done); pass --force "
                                   "to dispatch anyway", unmet=waiting)
                    st.event(run, "decision", f"forced by {a.by}: task set {t['id']} status={changes['status']} "
                             f"(unmet: {', '.join(waiting)})", task=t["id"], by=a.by)
            frm = t["status"]
            t.update(changes)
            if cur and "verdict" in changes:
                cur["verdict"] = changes["verdict"]
            if cur and "last_commit" in changes:
                cur["sha"] = changes["last_commit"]
            if "status" in changes and changes["status"] != frm:
                st.event(run, "transition", a.note or f"{frm} → {t['status']}", task=t["id"], frm=frm,
                         to=t["status"], by=a.by)
            elif a.note and not fanned:  # a fan-out already spent the note on each child
                st.event(run, "note", a.note, task=t["id"], by=a.by)
        elif getattr(a, "undo", False):  # round --undo: drop the latest round, but only an empty one
            if a.kind or a.cause or a.tasks:
                raise Fail("task round --undo takes no --kind/--cause/--tasks")
            if not t["rounds"]:
                raise Fail(f"task {t['id']} has no round to undo")
            n = t["rounds"][-1]["n"]
            d = st.task_dir(run["id"], t["id"])
            left = [os.path.join(d, f"{k}-r{n}.md") for k in ("brief", "report", "review")
                    if os.path.exists(os.path.join(d, f"{k}-r{n}.md"))]
            if left:  # a file of that round exists: the round was used, it stays
                raise Fail(f"round {n} of task {t['id']} has files: {', '.join(left)}; "
                           "delete them first", files=left)
            t["rounds"].pop()  # exactly what `task round` wrote: the round entry and its number
            t["round"] = t["rounds"][-1]["n"] if t["rounds"] else 0
            st.event(run, "note", f"round {n} undone", task=t["id"], by=a.by)
            t["updated"] = now()
            st.save(run, t, a.by)
            st.flush()
            return {"ok": True, "task": t["id"], "round": t["round"], "undone": n}
        else:  # round
            if a.kind not in ROUND_KINDS:
                raise Fail(f"bad kind {a.kind!r}; one of: {' '.join(ROUND_KINDS)}")
            group = [t]  # a lane_group round: the lead first, the rest share its one round number
            if a.tasks:
                group += [find_task(run, i) for i in csv(a.tasks)[1:]]
                if len({x["id"] for x in group}) < len(group):
                    raise Fail(f"--tasks lists a task twice: {a.tasks!r}")

                def lane_group(x):  # a child task has none of its own: its parent phase's
                    return next((y.get("lane_group") for y in [x, *ancestor_chain(run, x.get("parent"))]
                                 if y.get("lane_group")), None)
                lg = lane_group(t)
                for x in group:
                    if not lg or lane_group(x) != lg:
                        raise Fail(f"task {x['id']} is not in the lane_group of {t['id']} "
                                   f"({lg or 'none'}; {x['id']}: {lane_group(x) or 'none'})")
            n = max(x["round"] for x in group) + 1
            for x in group:
                x["round"] = n
                x["rounds"].append({"n": n, "kind": a.kind, "cause": a.cause, "verdict": None,
                                    "sha": None, "started": now()})
                st.event(run, "note", f"round {n} ({a.kind})" + (f": {a.cause}" if a.cause else "")
                         + (f" [group lead {t['id']}]" if x is not t else ""), task=x["id"], by=a.by)
                if x is not t:  # save() below writes the lead's task.json only
                    x["updated"] = now()
                    write_json(os.path.join(st.task_dir(run["id"], x["id"]), "task.json"), x)
            # Store mode only, and only the store row: the file views (run.json, events,
            # task.json) stay exactly what the file path writes. The watcher reads the row.
            if st.store_on:
                st._round_reset = [(x["id"], a.kind) for x in group]
        t["updated"] = now()
        st.save(run, t, a.by)
        warnings = st.refresh_others(run, a.by) if a.action == "set" else []
        st.flush()
        if a.action == "round":
            d = st.task_dir(run["id"], t["id"])
            paths = {k: os.path.join(d, f"{k}-r{t['round']}.md") for k in ("brief", "report", "review")}
            stored = st.store_round_files(run, t) if st.store_on else []
            return {"ok": True, "task": t["id"], "round": t["round"], "kind": a.kind,
                    **({"tasks": csv(a.tasks)} if a.tasks else {}),
                    **({"stored": stored} if stored else {}), **paths}
        return {"ok": True, "task": t, **({"warnings": warnings} if warnings else {})}


def count_pair(v, flag):
    """--tests/--selftest '<int>/<int>' -> (first, second); anything else is refused."""
    m = re.fullmatch(r"(\d+)/(\d+)", v or "")
    if not m:
        raise Fail(f"bad {flag} {v!r}; expected '<int>/<int>'")
    a, b = int(m.group(1)), int(m.group(2))
    # a failed count above the passed count is almost surely passed/total (384/387)
    if flag == "--tests" and b > a:
        raise Fail(f"bad --tests {v!r}; expected <passed>/<failed> "
                   f"(the failed count is larger than the passed count, which looks like passed/total)")
    return a, b


def latest_baseline(st, run):
    """The run's latest `baseline:` decision event -> the counts it fixed ({} without one): the
    tests and selftest pass-counts — tsc/lint ride in the text as context, never as gates. The
    parser accepts both the `<int>/<int>` form (the orchestrator's first event) and the
    `<int> pass / <int> fail` form (the dashboard's restyle note)."""
    for e in reversed(st.events(run["id"], 10 ** 9)):
        if e.get("type") == "decision" and str(e.get("text") or "").startswith("baseline:"):
            out = {}
            for key in ("tests", "selftest"):
                # "<key> <int>/<int>" — the form `mk-state` itself records
                m = re.search(rf"\b{key} (\d+)/(\d+)", e["text"])
                if m:
                    out[key] = int(m.group(1))
                    continue
                # "<key> <int> pass / <int> fail" — the form the dashboard note uses
                m = re.search(rf"\b{key} (\d+) pass / (\d+) fail", e["text"])
                if m:
                    out[key] = int(m.group(1))
            if "tsc" not in out:
                m = re.search(r"\btsc (\d+)", e["text"])
                if m:
                    out["tsc"] = int(m.group(1))
            return out
    return {}


def leaf_descendants(task_id, tasks):
    """All leaf tasks reachable from task_id (a task with no children of its own within the
    subtree). A parent that itself has a child is NOT a leaf — roll-up derives its status from
    the leaves below it. Used by the F2.1 fan-out and F2.3 task verify so neither ever writes
    a status directly onto a task that has children."""
    children_of = {}
    for x in tasks:
        p = (x.get("parent") or "").lower()
        if p:
            children_of.setdefault(p, []).append(x)
    leaves = []
    stack = [task_id.lower()]
    while stack:
        cur = stack.pop()
        kids = children_of.get(cur, [])
        if not kids:
            leaves.append(cur)
        else:
            for k in kids:
                stack.append(k["id"].lower() if isinstance(k, dict) and "id" in k else str(k).lower())
    return leaves


def task_verify(st, run, t, a):
    """F2.3 `task verify` (stage 7): one verdict event with the counts, compared against the run's
    latest `baseline:` decision; pass (no --fail, no dropped count, no failing test/selftest)
    moves T and its leaf descendants to review, fail moves them to fix — one transition each,
    the same --by. A parent is never set directly (roll-up derives it from the leaves)."""
    tests = count_pair(a.tests, "--tests")
    parts = [f"tests {tests[0]}/{tests[1]}"]
    if tests[1] > 0:
        parts.append(f"tests {tests[1]} fail")
    if a.tsc is not None:
        parts.append(f"tsc {a.tsc}")
    if a.lint is not None:
        parts.append(f"lint {a.lint}")
    selftest = count_pair(a.selftest, "--selftest") if a.selftest is not None else None
    if selftest is not None:
        parts.append(f"selftest {selftest[0]}/{selftest[1]}")
        if selftest[1] > 0:
            parts.append(f"selftest {selftest[1]} failed")
    base = latest_baseline(st, run)
    warnings = []
    if not base:
        warnings.append("task verify: no baseline event for this run (the drop gate is off; "
                       "passing the run is still recorded)")
    drops = []
    if "tests" in base and tests[0] < base["tests"]:
        drops.append(f"tests dropped {base['tests']}→{tests[0]}")
    if selftest is not None and "selftest" in base and selftest[0] < base["selftest"]:
        drops.append(f"selftest dropped {base['selftest']}→{selftest[0]}")
    why = (a.fail or "").strip()
    failed = bool(why or drops or tests[1] > 0 or (selftest is not None and selftest[1] > 0))
    extras = ([f"fail: {why}"] if why else []) + (["no baseline"] if not base else [])
    text = " · ".join(parts + drops + extras)
    st.event(run, "verdict", text, task=t["id"], by=a.by)
    # the note carries the FAIL reason or a one-line summary of which counts pushed it to fix
    # (the verdict event has the full counts; the note names the cause for the board)
    fail_reasons = list(drops)
    if tests[1] > 0:
        fail_reasons.append(f"tests {tests[1]} fail")
    if selftest is not None and selftest[1] > 0:
        fail_reasons.append(f"selftest {selftest[1]} failed")
    if why:
        fail_reasons.insert(0, why)
    note = ("verify: pass" if not failed
            else "verify: fail — " + " · ".join(fail_reasons))
    target = "fix" if failed else "review"
    moved = []
    for leaf_id in leaf_descendants(t["id"], run["tasks"]):
        if not isinstance(leaf_id, str):
            continue
        # resolve the actual task object
        leaf = next((x for x in run["tasks"] if x.get("id", "").lower() == leaf_id), None)
        if leaf is None or leaf["status"] == target:
            continue
        st.event(run, "transition", note, task=leaf["id"], frm=leaf["status"], to=target, by=a.by)
        leaf["status"], leaf["updated"] = target, now()
        moved.append(leaf["id"])
        write_json(os.path.join(st.task_dir(run["id"], leaf["id"]), "task.json"), leaf)
    st.save(run, t, a.by)
    st.flush()
    out = {"ok": True, "task": t, "result": "fail" if failed else "pass", "verdict": text, "moved": moved}
    if warnings:
        out["warnings"] = warnings
    return out


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


def plan_store_row(st, plan_id):
    """The latest plans row for this id, or None. Read through the connection mkdb returns."""
    if not st.store_on or not plan_id:
        return None
    con = st.store_open()
    try:
        return con.execute(
            "SELECT id, version, title, header_json, body_md, approved_at, archived_path"
            " FROM plans WHERE id=? ORDER BY version DESC LIMIT 1", (plan_id,)).fetchone()
    finally:
        con.close()


def plan_store_write(st, plan_id, version, title, header, body, archived):
    """Upsert one plan version. The plans table has no writer in mkdb, so this uses the same
    connection open_project returns. A re-apply of the same version replaces that row."""
    con = st.store_open()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO plans(id, version, title, header_json, body_md, approved_at, archived_path)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET version=excluded.version, title=excluded.title,"
            " header_json=excluded.header_json, body_md=excluded.body_md,"
            " approved_at=excluded.approved_at, archived_path=excluded.archived_path",
            (plan_id, version, title, json.dumps(header, sort_keys=True), body, now(), archived))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def plan_version_diff(stored, cur):
    """How a re-imported plan differs from the stored version. Empty when they match."""
    if stored is None:
        return ["plan not in the store"]
    diffs = []
    if (stored["title"] or "") != str(cur.get("plan") or ""):
        diffs.append("title")
    try:
        header = json.loads(stored["header_json"]) if stored["header_json"] else {}
    except ValueError:
        header = {}
    if header.get("sha") != cur.get("sha"):
        diffs.append("sha")
    if header.get("status") != cur.get("status"):
        diffs.append("status")
    phases = header.get("phases") or []
    if phases != (cur.get("phases") or []):
        diffs.append("phases")
    return diffs


def cmd_plan(a, st):
    if a.action != "sync":
        raise Fail(f"unknown plan action {a.action!r}")
    with st.locked():
        st.repair()
        run = st.load(a.run)
        if not (run.get("plan_id") and run.get("plan")):
            raise Fail(f"run {run['id']} has no plan link (run init --plan)")
        cur = plan_tool("parse", st.plan_file(run))
        stored = plan_store_row(st, run["plan_id"])
        version_diff = plan_version_diff(stored, cur) if st.store_on else []
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
                    "sha": cur.get("sha"), "changes": changes, "warnings": warnings,
                    **({"version_diff": version_diff} if st.store_on else {})}
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
                              "lane_group": src.get("lane_group") or None,
                              "reviewer": src.get("reviewer") or ""})
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
        st.flush()
        if st.store_on and version_diff:
            prev = stored["version"] if stored and stored["version"] else 0
            body = open(st.plan_file(run)).read()
            plan_store_write(st, run["plan_id"], prev + 1, str(cur.get("plan") or ""),
                             {"sha": cur.get("sha"), "status": cur.get("status"),
                              "phases": cur.get("phases") or []},
                             body, st.plan_file(run))
    return {"ok": True, "run": run["id"], "plan": run["plan_id"], "apply": True, "sha": cur.get("sha"),
            "changes": changes, "warnings": warnings,
            **({"version_diff": version_diff, "version": (stored["version"] if stored and stored["version"] else 0) + (1 if version_diff else 0)} if st.store_on else {})}


def cmd_event(a, st):
    if a.type not in EVENT_TYPES:
        raise Fail(f"bad event type {a.type!r}; one of: {' '.join(EVENT_TYPES)}")
    if not a.text.strip():
        raise Fail("event needs a non-empty --text")
    if a.type == "transition" and not a.to:
        raise Fail("transition event needs --to")
    a.from_ = STATUS_ALIASES.get(a.from_, a.from_)  # F2.2: --from/--to store the canonical word
    a.to = STATUS_ALIASES.get(a.to, a.to)
    for s in (a.from_, a.to):
        if s is not None and s not in STATUSES:
            raise Fail(f"bad status {s!r}")
    with st.locked():
        st.repair()
        run = st.load(a.run, a.task)
        if a.task:
            find_task(run, a.task)
        ev = st.event(run, a.type, a.text, task=a.task, frm=a.from_, to=a.to, by=a.by)
        st.save(run)
        st.flush()
    return {"ok": True, "event": ev}


def cmd_wait(a, st):
    with st.locked():
        st.repair()
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
        st.flush()
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
        st.repair()
        return {"ok": True, "board": st.render(st.load(a.run))}


def cmd_store(a, st):
    with st.locked():
        if a.action == "status":
            return {"ok": True, "store": "on" if st.store_on else "off"}
        if a.action == "enable":
            return st.enable_store()
        raise Fail(f"unknown store action {a.action!r}")


def cmd_sync(a, st):
    """Copy every project change above sync.last_change_no into the root cards. Idempotent.
    File mode (no <data>/mk.db) changes nothing and creates no store."""
    if not st.store_on:
        return {"ok": True, "store": "off", "copied": 0}
    project = a.project or STORE_PROJECT
    copied = st.db().catch_up(st.home_dir(), project, data_dir=st.data)
    return {"ok": True, "store": "on", "project": project, "copied": copied}


def cmd_export_legacy(a, st):
    if not a.run:
        raise Fail("export-legacy needs --run")
    with st.locked():
        return st.export_legacy(a.run)


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
    run.choices["set"].add_argument("--plan-id", help="link the run to plan <id> (repair)")
    run.choices["set"].add_argument("--status", choices=("active", "done"),
                                    help="done promotes this run's merged/qa tasks; "
                                         "already-done is not a no-op, it promotes again "
                                         "(repairs a crash between the two writes)")
    run.choices["stage"].add_argument("n", type=int, nargs="?")

    task = sub.add_parser("task").add_subparsers(dest="action", required=True)
    t = task.add_parser("add", parents=[data_p, run_p])
    t.add_argument("--id"), t.add_argument("--title"), t.add_argument("--wave", type=int)
    t.add_argument("--deps"), t.add_argument("--owned"), t.add_argument("--parent")
    t = task.add_parser("set", parents=[data_p, run_p])
    t.add_argument("task"), t.add_argument("pairs", nargs="+"), t.add_argument("--note")
    t.add_argument("--force", action="store_true")
    t = task.add_parser("round", parents=[data_p, run_p])
    t.add_argument("task", nargs="?"), t.add_argument("--kind"), t.add_argument("--cause")
    t.add_argument("--tasks")  # lane_group round: LEAD,T2,T3; the task arg is optional then
    t.add_argument("--undo", action="store_true")  # drop the latest (empty) round; kind checked in cmd
    t = task.add_parser("get", parents=[data_p, run_p])
    t.add_argument("task")
    t = task.add_parser("verify", parents=[data_p, run_p])
    t.add_argument("task")
    t.add_argument("--tests", required=True, metavar="<passed>/<failed>",
                   help="passed/failed counts; a failed count larger than the passed count is refused")
    t.add_argument("--tsc", type=int)
    t.add_argument("--lint", type=int)
    t.add_argument("--selftest")  # '<run>/<failed>'
    t.add_argument("--fail")
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

    stc = sub.add_parser("store").add_subparsers(dest="action", required=True)
    for name in ("enable", "status"):
        stc.add_parser(name, parents=[data_p]).set_defaults(func=cmd_store)
    sy = sub.add_parser("sync", parents=[data_p])
    sy.add_argument("--project")
    sy.set_defaults(func=cmd_sync)
    ex = sub.add_parser("export-legacy", parents=[data_p, run_p])
    ex.set_defaults(func=cmd_export_legacy)
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
    except Exception as e:
        # mkdb.Fail is a different class (loaded by path). A store error is still one JSON line.
        # OSError and ValueError are handled above: they must not fall through to a traceback.
        if type(e).__name__ == "Fail" and type(e).__module__ == "mkdb":
            out, code = {"ok": False, "error": str(e)}, getattr(e, "code", 2)
        else:
            raise
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
     "route": "codex gpt-5 high", "reviewer": "opencode opencode-go/kimi-k3", "size": "standard",
     "risk": "reversible", "review": "light",
     "qa": [], "commit": "feat: a", "subagents": False, "lane_group": "A", "tasks": [
        {"id": "P1.1", "title": "First", "acceptance": "a1", "refs": [], "subagent": False,
         "owned": ["scripts/a.py"]},
        {"id": "P1.2", "title": "Second", "acceptance": "a2", "refs": [], "subagent": False, "owned": []}]},
    {"id": "P2", "title": "Phase two", "wave": 1, "deps": ["P1"], "owned": ["docs/x.md"],
     "route": "", "reviewer": "", "size": "standard", "risk": "reversible", "review": "light",
     "qa": [], "commit": "feat: b", "subagents": False, "lane_group": "A", "tasks": [
        {"id": "P2.1", "title": "Third", "acceptance": "a3", "refs": [], "subagent": False, "owned": []}]}]}

def self_test():
    import shutil, subprocess
    tmp = tempfile.mkdtemp(prefix="mk-state-test-")
    tmp_home = os.path.join(tmp, "home")
    os.environ["MK_HOME"] = tmp_home
    os.environ["MATRIXKODER_HOME"] = tmp_home
    os.environ["MK_SELFTEST"] = "1"
    env = dict(os.environ, MATRIXKODER_HOME=tmp_home, MK_HOME=tmp_home, MK_SELFTEST="1")
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
        # file mode: a round bump never touches status or verdict (self-hosting guard)
        before = mk("task", "get", "T1")["task"]
        r1 = mk("task", "round", "T1", "--kind", "build")
        assert r1["round"] == 1 and r1["brief"].endswith(os.path.join("tasks", "T1", "brief-r1.md"))
        after = mk("task", "get", "T1")["task"]
        assert after["status"] == before["status"] == "routed" and after["verdict"] == before["verdict"], after
        assert not os.path.exists(os.path.join(data, "mk.db")), "file-mode round created mk.db"
        r2 = mk("task", "round", "T1", "--kind", "fix", "--cause", "brief gap")
        assert r2["round"] == 2 and r2["review"].endswith("review-r2.md") and r2["report"].endswith("report-r2.md")
        assert mk("task", "round", "T1", "--kind", "nap", code=2)["ok"] is False
        t1 = mk("task", "set", "T1", "verdict=approve", "last_commit=abc1234")["task"]
        assert t1["rounds"][-1] == {**t1["rounds"][-1], "n": 2, "kind": "fix", "cause": "brief gap",
                                    "verdict": "approve", "sha": "abc1234"}
        assert mk("task", "get", "T1")["task"]["round"] == 2
        assert mk("task", "get", "NOPE", code=2)["ok"] is False

        # round --undo: refused while a round file exists (naming it), then the empty round is dropped
        assert mk("task", "round", "T1", "--kind", "fix")["round"] == 3
        assert "takes no" in mk("task", "round", "T1", "--undo", "--kind", "fix", code=2)["error"]
        brief3 = os.path.join(rdir, "tasks", "T1", "brief-r3.md")
        open(brief3, "w").write("# brief\n")
        e = mk("task", "round", "T1", "--undo", code=2)
        assert e["ok"] is False and e["files"] == [brief3] and brief3 in e["error"], e
        assert mk("task", "get", "T1")["task"]["round"] == 3  # refused: the round stays
        os.unlink(brief3)
        evp = os.path.join(rdir, "events.jsonl")
        n_ev = len(open(evp).readlines())
        out = mk("task", "round", "T1", "--undo")
        assert out["ok"] and out["round"] == 2 and out["undone"] == 3, out
        evs = [json.loads(x) for x in open(evp)]
        assert len(evs) == n_ev + 1 and "round 3 undone" in evs[-1]["text"], evs  # exactly one event
        tu = mk("task", "get", "T1")["task"]
        assert tu["round"] == 2 and [r["n"] for r in tu["rounds"]] == [1, 2]
        assert tu["rounds"][-1]["kind"] == "fix" and tu["rounds"][-1]["verdict"] == "approve"
        assert tu["rounds"][-1]["sha"] == "abc1234"  # round 2 exactly as the undone round 3 found it
        assert "no round" in mk("task", "round", "T2", "--undo", code=2)["error"]

        # pane: the lane's herdr pane id (arm writes it) is a settable task key
        assert "pane" in TASK_KEYS
        tp = mk("task", "set", "T1", "pane=wK:p1")["task"]
        assert tp["pane"] == "wK:p1"
        assert mk("task", "get", "T1")["task"]["pane"] == "wK:p1"
        assert mk("task", "set", "T1", "pane=")["task"]["pane"] is None  # empty clears, like lane=
        assert mk("task", "set", "T1", "pan=wK:p1", code=2)["ok"] is False  # still a closed key set
        assert mk("task", "get", "T1")["task"]["pane"] is None
        assert mk("task", "set", "T1", "pane=wK:p1")["task"]["pane"] == "wK:p1"

        # reviewer: settable like pane (the plan writes it at run init; the fixture block asserts that)
        assert "reviewer" in TASK_KEYS
        assert mk("task", "set", "T1", "reviewer=claude fable")["task"]["reviewer"] == "claude fable"
        assert mk("task", "set", "T1", "reviewer=")["task"]["reviewer"] is None  # empty clears
        assert mk("task", "set", "T1", "reviwe=x", code=2)["ok"] is False  # still a closed key set

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

        # an ancestor's unmet deps gate the child's dispatch; a parent's status= fans out (F2.1)
        dk("task", "add", "--id", "P2", "--title", "Parent 2", "--deps", "A3", "--run", "rb")
        dk("task", "add", "--id", "C4", "--title", "Child 4", "--parent", "P2", "--run", "rb")
        e = dk("task", "set", "C4", "status=dispatched", "--run", "rb", code=2)
        assert "waits on A3" in e["error"] and e["unmet"] == ["A3"], e
        assert dk("task", "set", "C4", "status=dispatched", "--force", "--run", "rb")["ok"]
        # F2.1: status= on a parent fans out to every child (never written on the parent directly);
        # the dep gate applies per child — an unmet one refuses the whole fan-out before any write
        dk("task", "add", "--id", "PG", "--title", "Parent gated", "--deps", "A3", "--run", "rb")
        dk("task", "add", "--id", "CG", "--title", "Child gated", "--parent", "PG", "--run", "rb")
        e = dk("task", "set", "PG", "status=building", "--run", "rb", code=2)  # CG behind PG's dep A3
        assert "waits on A3" in e["error"] and e["unmet"] == ["A3"], e
        dk("task", "set", "P2", "status=review", "--run", "rb")  # review carries no gate: plain fan-out
        assert dk("task", "get", "C4", "--run", "rb")["task"]["status"] == "review"
        assert dk("task", "get", "P2", "--run", "rb")["task"]["status"] == "review"  # rolled up

        # --force past the dep gate: ONE decision event naming --by (default: orchestrator)
        dk("task", "add", "--id", "F1", "--title", "forced", "--deps", "A3", "--run", "rb")
        dk("task", "add", "--id", "F2", "--title", "forced too", "--deps", "A3", "--run", "rb")
        bev = os.path.join(tmp, "deps", "runs", "rb", "events.jsonl")
        dec = lambda: [x for x in (json.loads(y) for y in open(bev)) if x["type"] == "decision"]
        n_dec = len(dec())
        e = dk("task", "set", "F1", "status=dispatched", "--run", "rb", code=2)  # unforced: still refused
        assert "waits on A3" in e["error"] and e["unmet"] == ["A3"] and len(dec()) == n_dec, e
        out = dk("task", "set", "F1", "status=dispatched", "--force", "--by", "user", "--run", "rb")
        assert out["ok"] and out["task"]["status"] == "dispatched", out
        d = dec()[n_dec:]
        assert len(d) == 1 and d[0]["by"] == "user" and d[0]["task"] == "F1", d  # exactly one decision
        assert d[0]["text"] == "forced by user: task set F1 status=dispatched (unmet: A3)", d
        assert any(x["type"] == "transition" and x["task"] == "F1" and x["to"] == "dispatched"
                   for x in (json.loads(y) for y in open(bev)))  # the usual transition event stays
        dk("task", "set", "F2", "status=building", "--force", "--run", "rb")  # no --by → orchestrator
        d = dec()[n_dec:]
        assert len(d) == 2 and d[1]["by"] == "orchestrator", d
        assert d[1]["text"] == "forced by orchestrator: task set F2 status=building (unmet: A3)", d

        # R2-L2: a parent fan-out that is --force'd past the dep gate writes the unmet list into
        # the decision text (the old code missed the f-prefix and wrote the literal
        # "{', '.join(waiting)}" into the event). Parent has dep A3 (unmet); the forced
        # fan-out produces a decision event whose text names the unmet dep.
        dk("task", "add", "--id", "RFG", "--title", "r2-l2 fan-out gated", "--deps", "A3", "--run", "rb")
        dk("task", "add", "--id", "RFGC", "--title", "r2-l2 fan-out gated child", "--parent", "RFG", "--run", "rb")
        n_dec = len(dec())
        out = dk("task", "set", "RFG", "status=dispatched", "--force", "--run", "rb")
        assert out["ok"], out
        # the child (RFGC) was set to dispatched by the fan-out; the parent rolls up
        assert dk("task", "get", "RFGC", "--run", "rb")["task"]["status"] == "dispatched"
        d = dec()[n_dec:]
        assert len(d) == 1 and d[0]["task"] == "RFGC", d
        # the unmet list is rendered into the text (A3), not the literal format string
        assert "(fan-out, unmet: A3)" in d[0]["text"], d[0]
        assert "{'" not in d[0]["text"], d[0]  # the old bug wrote a literal "{', '.join(waiting)}"        # --parent onto merged/qa/done is refused; other runs' ready stays correct after an add
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

        # F2.1/F2.2/F2.3: parent status fan-out, status aliases, task verify — own data dir
        f2 = ["--data", os.path.join(tmp, "f2")]
        fk = lambda *x, code=0: mk(*x, *f2, code=code, data_arg=False)
        fk("run", "init", "--id", "fr", "--feature", "F2 fixes", "--branch", "b")
        fev = os.path.join(tmp, "f2", "runs", "fr", "events.jsonl")
        fevs = lambda: [json.loads(x) for x in open(fev)]

        # F2.1: a 3-child parent — status= fans out, one transition per child with the same --note,
        # the parent itself is never set directly (roll-up derives it from the children)
        fk("task", "add", "--id", "PF", "--title", "fan-out parent")
        for i in (1, 2, 3):
            fk("task", "add", "--id", f"FC{i}", "--title", f"child {i}", "--parent", "PF")
        fk("task", "set", "PF", "status=fix", "--note", "fix round")
        run = fk("run", "get")["run"]
        ts = {x["id"]: x for x in run["tasks"]}
        assert all(ts[f"FC{i}"]["status"] == "fix" for i in (1, 2, 3)), ts
        assert ts["PF"]["status"] == "fix"  # rolled up from the children
        fan = [e for e in fevs() if e["type"] == "transition" and e["to"] == "fix"
               and e["task"] in ("FC1", "FC2", "FC3")]
        assert len(fan) == 3 and {e["text"] for e in fan} == {"fix round"}, fan
        # non-status keys still land on the parent itself (the fan-out is status= only)
        assert fk("task", "set", "PF", "agent=codex")["task"]["agent"] == "codex"

        # F2.2: aliases are accepted and stored canonically; unknown words still fail with the list
        fk("task", "add", "--id", "AL", "--title", "aliases")
        assert fk("task", "set", "AL", "status=reviewing")["task"]["status"] == "review"
        assert fk("task", "set", "AL", "status=merging")["task"]["status"] == "merged"
        assert fk("task", "set", "AL", "status=fixing")["task"]["status"] == "fix"
        e = fk("task", "set", "AL", "status=bogus", code=2)
        assert "bad status" in e["error"], e
        assert "review" in e["error"] and "merged" in e["error"] and "reviewing" not in e["error"], e
        ev = fk("event", "--task", "AL", "--type", "transition", "--text", "alias",
                "--from", "fixing", "--to", "reviewing")["event"]
        assert ev["from"] == "fix" and ev["to"] == "review", ev
        assert fk("event", "--task", "AL", "--type", "transition", "--text", "x",
                  "--to", "exploding", code=2)["ok"] is False

        # F2.3 pass: counts at baseline → review, one verdict event carrying the counts and --by
        fk("task", "add", "--id", "VT", "--title", "verify target")
        fk("event", "--type", "decision", "--text", "baseline: tests 100/0 · tsc 0 · lint 0 · selftest 8/0")
        out = fk("task", "verify", "VT", "--tests", "100/0", "--tsc", "0", "--lint", "0",
                 "--selftest", "8/0", "--by", "reviewer-x")
        assert out["ok"] and out["result"] == "pass" and out["task"]["status"] == "review", out
        verd = [e for e in fevs() if e["type"] == "verdict"]
        assert len(verd) == 1 and verd[0]["task"] == "VT" and verd[0]["by"] == "reviewer-x", verd
        assert verd[0]["text"] == "tests 100/0 · tsc 0 · lint 0 · selftest 8/0", verd
        tr = [e for e in fevs() if e["type"] == "transition" and e["task"] == "VT" and e["to"] == "review"]
        assert tr and tr[-1]["text"] == "verify: pass" and tr[-1]["by"] == "reviewer-x", tr

        # F2.3 fail on a dropped count: tests below baseline → fix, the drop named in the verdict
        out = fk("task", "verify", "VT", "--tests", "99/1", "--tsc", "0", "--lint", "0", "--selftest", "8/0")
        assert out["result"] == "fail", out
        assert fk("task", "get", "VT")["task"]["status"] == "fix"
        verd = [e for e in fevs() if e["type"] == "verdict"][-1]
        assert "tests dropped 100→99" in verd["text"], verd
        tr = [e for e in fevs() if e["type"] == "transition" and e["task"] == "VT" and e["to"] == "fix"]
        assert tr and "verify: fail" in tr[-1]["text"] and "tests dropped 100→99" in tr[-1]["text"], tr

        # F2.3 fail on an explicit reason: --fail wins even with every count at baseline
        fk("task", "set", "VT", "status=building")  # so the fail move is a real transition
        out = fk("task", "verify", "VT", "--tests", "100/0", "--selftest", "8/0", "--fail", "gate exploded")
        assert out["result"] == "fail", out
        assert fk("task", "get", "VT")["task"]["status"] == "fix"
        tr = [e for e in fevs() if e["type"] == "transition" and e["task"] == "VT" and e["to"] == "fix"]
        assert tr and tr[-1]["text"] == "verify: fail — gate exploded", tr

        # F2.3 on a parent: the children move too; --tests must be <int>/<int>
        fk("task", "add", "--id", "VP", "--title", "verify parent")
        fk("task", "add", "--id", "VC1", "--title", "c1", "--parent", "VP")
        fk("task", "add", "--id", "VC2", "--title", "c2", "--parent", "VP")
        fk("task", "verify", "VP", "--tests", "100/0", "--selftest", "8/0")
        run = fk("run", "get")["run"]
        ss = {x["id"]: x["status"] for x in run["tasks"]}
        assert ss["VP"] == ss["VC1"] == ss["VC2"] == "review", ss
        for bad in ("100", "a/b", "100/0/1"):
            assert fk("task", "verify", "VP", "--tests", bad, code=2)["ok"] is False, bad

        # K2.1: --help names <passed>/<failed>; a failed count larger than the passed
        # count looks like passed/total and is refused (exit 2, task not moved, no event).
        help_out = subprocess.run([sys.executable, os.path.abspath(__file__), "task", "verify", "--help"],
                                  capture_output=True, text=True, env=env)
        assert help_out.returncode == 0, help_out.stderr
        assert "--tests <passed>/<failed>" in help_out.stdout, help_out.stdout
        fk("task", "set", "VT", "status=building")
        n_before = len(fevs())
        bad = fk("task", "verify", "VT", "--tests", "384/387", code=2)
        assert bad["ok"] is False, bad
        assert "384/387" in bad["error"] and "<passed>/<failed>" in bad["error"], bad
        assert fk("task", "get", "VT")["task"]["status"] == "building"
        assert len(fevs()) == n_before, "refused --tests wrote an event"
        for ok_pair, want in (("350/0", "pass"), ("0/0", "pass"), ("10/3", "fail"), ("3/3", "fail")):
            fk("task", "set", "VT", "status=building")
            if ok_pair == "0/0":  # the run baseline is 100; a zero pass-count must not trip the drop gate
                fk("event", "--type", "decision", "--text", "baseline: tests 0/0 · selftest 8/0")
            out = fk("task", "verify", "VT", "--tests", ok_pair, "--selftest", "8/0")
            assert out["ok"] and out["result"] == want, (ok_pair, out)
        fk("event", "--type", "decision", "--text", "baseline: tests 100/0 · tsc 0 · lint 0 · selftest 8/0")

        # F2.3 H1: tests fail / selftest failed > 0 → fix, named in the verdict text.
        # M3: verify never writes on a task that has children — the leaves carry the verify
        # transition; the parent rolls up. Check the leaves' transitions, not the parent's.
        fk("task", "set", "VP", "status=routed")  # so verify can move it again
        fk("event", "--type", "decision", "--text",
           "baseline: tsc 0 · tests 236 pass / 0 fail · selftest 8/0")  # alt form (M2)
        fk("task", "set", "VP", "status=review")  # so the leaves' fail-move is a real transition
        out = fk("task", "verify", "VP", "--tests", "240/5", "--selftest", "8/3")
        assert out["result"] == "fail", out
        assert fk("task", "get", "VP")["task"]["status"] == "fix"
        verd = [e for e in fevs() if e["type"] == "verdict"][-1]
        assert "tests 5 fail" in verd["text"] and "selftest 3 failed" in verd["text"], verd
        # the verify transitions live on the leaves, not the parent
        tr_leaves = [e for e in fevs() if e["type"] == "transition" and e["task"] in ("VC1", "VC2")
                     and e["to"] == "fix"]
        assert tr_leaves and "verify: fail" in tr_leaves[-1]["text"]
        assert "tests 5 fail" in tr_leaves[-1]["text"] and "selftest 3 failed" in tr_leaves[-1]["text"], tr_leaves[-1]

        # F2.3 M2: baseline parsed in the "<int> pass / <int> fail" form
        fk("task", "set", "VP", "status=review")
        out = fk("task", "verify", "VP", "--tests", "236/0", "--selftest", "8/0")
        assert out["result"] == "pass" and out["task"]["status"] == "review", out
        verd = [e for e in fevs() if e["type"] == "verdict"][-1]
        assert "tests 236/0" in verd["text"] and "selftest 8/0" in verd["text"], verd
        # F2.3 M2: drop gate works with the dashboard form — a run whose baseline is
        # "tests 236 pass / 0 fail" and whose verify reports 100/0 must see the drop
        # (the old parser returned base={} and silently disabled the drop gate)
        fk("event", "--type", "decision", "--text",
           "baseline: tsc 0 · tests 236 pass / 0 fail · selftest 8/0")
        fk("task", "set", "VP", "status=building")
        out = fk("task", "verify", "VP", "--tests", "100/0", "--selftest", "8/0")
        assert out["result"] == "fail", out
        verd = [e for e in fevs() if e["type"] == "verdict"][-1]
        assert "tests dropped 236→100" in verd["text"], verd

        # F2.3 M2: missing baseline → reply carries warnings, verdict says "no baseline"
        fk("run", "init", "--id", "fr2", "--feature", "F2 r2", "--branch", "b")
        fk2 = lambda *x, code=0: mk(*x, "--data", os.path.join(tmp, "f2"), "--run", "fr2",
                                    code=code, data_arg=False)
        fk2("task", "add", "--id", "VT2", "--title", "verify no baseline")
        out = fk2("task", "verify", "VT2", "--tests", "240/5", "--selftest", "8/3")
        assert out["result"] == "fail", out  # tests fail → still fix even without baseline
        assert out.get("warnings") and "no baseline" in " ".join(out["warnings"]), out
        verd = [json.loads(x) for x in open(os.path.join(tmp, "f2", "runs", "fr2", "events.jsonl"))
                if json.loads(x).get("type") == "verdict"][-1]
        assert "no baseline" in verd["text"], verd

        # F2.3 M3: descendants — verify reaches leaves, never writes on a parent
        fk("task", "add", "--id", "VGP", "--title", "verify grand", "--run", "fr")
        fk("task", "add", "--id", "VGC", "--title", "verify grand child", "--parent", "VGP", "--run", "fr")
        fk("task", "add", "--id", "VGGC", "--title", "verify grand grand child", "--parent", "VGC", "--run", "fr")
        fk("event", "--run", "fr", "--task", "VGP", "--type", "decision", "--text",
           "baseline: tests 100/0 · selftest 8/0")
        out = fk("task", "verify", "VGP", "--tests", "100/0", "--selftest", "8/0", "--run", "fr")
        run = fk("run", "get", "--run", "fr")["run"]
        ss = {x["id"]: x["status"] for x in run["tasks"]}
        assert ss["VGP"] == "review" and ss["VGC"] == "review" and ss["VGGC"] == "review", ss
        # F2.3 M3: same for FAIL — leaves move to fix
        fk("task", "set", "VGGC", "status=building", "--run", "fr")  # so the fail move is a real transition
        out = fk("task", "verify", "VGP", "--tests", "100/0", "--selftest", "8/0", "--fail", "leaves", "--run", "fr")
        run = fk("run", "get", "--run", "fr")["run"]
        ss = {x["id"]: x["status"] for x in run["tasks"]}
        assert ss["VGGC"] == "fix" and ss["VGC"] == "fix" and ss["VGP"] == "fix", ss

        # F2.1 M3: fan-out reaches leaves too; never writes status onto a task that has children
        fk("task", "add", "--id", "FGP", "--title", "fan grand", "--run", "fr")
        fk("task", "add", "--id", "FGC", "--title", "fan grand child", "--parent", "FGP", "--run", "fr")
        fk("task", "add", "--id", "FGGC", "--title", "fan grand grand child", "--parent", "FGC", "--run", "fr")
        out = fk("task", "set", "FGP", "status=fix", "--note", "fix leaves", "--run", "fr")
        run = fk("run", "get", "--run", "fr")["run"]
        ss = {x["id"]: x["status"] for x in run["tasks"]}
        assert ss["FGC"] == "fix" and ss["FGGC"] == "fix", ss  # grandchildren too
        # the parent itself was not set directly: it rolled up from its descendants
        # (FGP only has FGC as child per data; the roll-up sees FGC=fix → FGP=fix)
        fan_grand = [e for e in fevs() if e["type"] == "transition" and e["task"] == "FGGC"
                     and e["to"] == "fix" and e["text"] == "fix leaves"]
        assert len(fan_grand) == 1, fan_grand  # exactly the grand grand child got the text
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
        assert t["P1"]["reviewer"] == "opencode opencode-go/kimi-k3"  # from the plan JSON
        assert t["P2"]["reviewer"] == ""  # absent in the plan: "" (orchestrator decides), not null
        assert all("reviewer" not in t[x] for x in ("P1.1", "P1.2", "P2.1"))  # children stay bare
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

        # task round --tasks: one lane_group round, recorded on every listed task (lead first)
        def grp(i, g, kids=()):
            return {**PLAN_BASE["phases"][1], "id": i, "title": i, "deps": [], "owned": [], "lane_group": g,
                    "tasks": [{"id": k, "title": k, "acceptance": "a", "refs": [], "subagent": False,
                               "owned": []} for k in kids]}
        gfile = os.path.join(tmp, "lgplan.json")
        json.dump({**PLAN_BASE, "phases": [grp("C1", "G", ["C1.1"]), grp("S1", "G"), grp("P1", "G"),
                                           grp("X1", "H")]}, open(gfile, "w"))
        pk("run", "init", "--id", "lg1", "--feature", "Group", "--branch", "b", "--plan", gfile)
        pk("task", "round", "S1", "--kind", "build", "--run", "lg1")  # S1 is one round ahead
        lgev = os.path.join(pd, "runs", "lg1", "events.jsonl")
        n_ev = len(open(lgev).readlines())
        for bad, why in ((("--tasks", "C1,NOPE"), "NOPE"), (("--tasks", "C1,X1"), "lane_group"),
                         (("--tasks", "C1,C1"), "twice"), (("--tasks", "S1,C1", "C1"), "first of --tasks"),
                         (("--tasks", "C1,S1", "--undo"), "takes no")):
            e = pk("task", "round", *bad, "--kind", "build", "--run", "lg1", code=2)
            assert e["ok"] is False and why in e["error"], (bad, e)
        assert len(open(lgev).readlines()) == n_ev  # every refusal wrote nothing
        assert pk("task", "get", "C1", "--run", "lg1")["task"]["round"] == 0
        out = pk("task", "round", "--tasks", "C1,S1,P1,C1.1", "--kind", "build", "--run", "lg1")
        assert out["round"] == 2 and out["tasks"] == ["C1", "S1", "P1", "C1.1"], out  # 1 + the max (S1's)
        assert out["brief"].endswith(os.path.join("tasks", "C1", "brief-r2.md")), out  # lead's folder only
        for tid in ("C1", "S1", "P1", "C1.1"):
            tt = pk("task", "get", tid, "--run", "lg1")["task"]
            assert tt["round"] == 2 and tt["rounds"][-1]["n"] == 2 and tt["rounds"][-1]["kind"] == "build", tt
            on_disk = json.load(open(os.path.join(pd, "runs", "lg1", "tasks", tid, "task.json")))
            assert on_disk["round"] == 2, on_disk
        evs = [json.loads(x) for x in open(lgev)][n_ev:]
        assert sorted(x["task"] for x in evs) == ["C1", "C1.1", "P1", "S1"], evs  # one event per task
        assert pk("task", "get", "X1", "--run", "lg1")["task"]["round"] == 0  # another group: untouched
        # the positional lead is optional, and without --tasks nothing changes
        assert pk("task", "round", "C1", "--tasks", "C1,S1", "--kind", "fix", "--run", "lg1")["round"] == 3
        assert pk("task", "round", "X1", "--kind", "build", "--run", "lg1")["round"] == 1
        assert pk("task", "get", "P1", "--run", "lg1")["task"]["round"] == 2
        assert "needs a task" in pk("task", "round", "--kind", "build", "--run", "lg1", code=2)["error"]
        pk("run", "archive", "--id", "lg1")  # later cases expect one active run

        # --force init on an unapproved plan: allowed, and an event says so
        db["status"] = "draft"
        putdb(db)
        assert pk("run", "init", "--id", "pf1", "--feature", "F", "--branch", "b", "--plan", pfile,
                  "--force")["ok"] is True
        evs = [json.loads(x) for x in open(os.path.join(pd, "runs", "pf1", "events.jsonl"))]
        assert any("not approved" in x["text"] and "--force" in x["text"] for x in evs), evs
        d = [x for x in evs if x["type"] == "decision"]
        assert len(d) == 1 and d[0]["by"] == "orchestrator", d  # exactly one decision event
        assert d[0]["text"] == f"forced by orchestrator: run init --plan {pfile} (plan status draft)", d
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
                             "route": "", "reviewer": "agy gemini-3.8-flash", "size": "standard",
                             "risk": "reversible", "review": "light",
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
        assert t["P3"]["reviewer"] == "agy gemini-3.8-flash"  # the sync add path stores it too
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
        # run set --status done promotes THIS run's merged and qa tasks to done
        pd2 = ["--data", os.path.join(tmp, "promote")]
        pm = lambda *x, code=0: mk(*x, *pd2, code=code, data_arg=False)
        pm("run", "init", "--id", "close", "--feature", "Close", "--branch", "c")
        pm("run", "init", "--id", "other", "--feature", "Other", "--branch", "o")
        for tid, stt in (("M", "merged"), ("Q", "qa"), ("B", "building"),
                         ("K", "blocked"), ("P", "planned")):
            pm("task", "add", "--id", tid, "--title", tid, "--run", "close")
            if stt != "planned":
                pm("task", "set", tid, f"status={stt}", "--run", "close")
        pm("task", "add", "--id", "PAR", "--title", "parent", "--run", "close")
        pm("task", "add", "--id", "C1", "--title", "c1", "--parent", "PAR", "--run", "close")
        pm("task", "add", "--id", "C2", "--title", "c2", "--parent", "PAR", "--run", "close")
        pm("task", "set", "C1", "status=merged", "--run", "close")
        pm("task", "set", "C2", "status=qa", "--run", "close")
        # roll-up already lifted PAR to merged; put it back so the close promotes the
        # children and the following save rolls the parent up to done (not a direct set)
        rj = os.path.join(tmp, "promote", "runs", "close", "run.json")
        run = json.load(open(rj))
        par = next(t for t in run["tasks"] if t["id"] == "PAR")
        assert par["status"] == "merged", par
        par["status"] = "building"
        write_json(rj, run)
        write_json(os.path.join(tmp, "promote", "runs", "close", "tasks", "PAR", "task.json"), par)
        pm("task", "add", "--id", "OM", "--title", "other merged", "--run", "other")
        pm("task", "set", "OM", "status=merged", "--run", "other")
        evp = os.path.join(tmp, "promote", "runs", "close", "events.jsonl")
        n_ev = len(open(evp).readlines())
        out = pm("run", "set", "--id", "close", "--status", "done")
        assert out["ok"] and out["run"]["status"] == "done", out
        assert out["promoted"] == ["M", "Q", "C1", "C2"], out
        assert "promote_error" not in out, out
        got = {t["id"]: t["status"] for t in pm("run", "get", "--id", "close")["run"]["tasks"]}
        assert got["M"] == "done" and got["Q"] == "done", got
        assert got["B"] == "building" and got["K"] == "blocked" and got["P"] == "planned", got
        assert got["PAR"] == "done" and got["C1"] == "done" and got["C2"] == "done", got
        assert pm("task", "get", "OM", "--run", "other")["task"]["status"] == "merged"
        newev = [json.loads(x) for x in open(evp)][n_ev:]
        promo = [e for e in newev if e.get("type") == "transition" and e.get("to") == "done"
                 and e.get("text") == "promoted by closing the run"]
        assert [e["task"] for e in promo] == ["M", "Q", "C1", "C2"], newev
        assert all(e["from"] in ("merged", "qa") for e in promo), promo
        again = pm("run", "set", "--id", "close", "--status", "done")
        assert again["ok"] and again["promoted"] == [], again
        assert "promote_error" not in again
        tail = [json.loads(x) for x in open(evp)][n_ev + len(newev):]
        assert not [e for e in tail if e.get("to") == "done"], tail  # no second promotion

        # K2.2: a raise on the 2nd promotion still reports the task already moved.
        # The list is built by the caller; MK_PROMOTE_FAIL_AFTER=1 fails inside promote_closed.
        pm("run", "init", "--id", "half", "--feature", "Half", "--branch", "h")
        pm("task", "add", "--id", "H1", "--title", "first", "--run", "half")
        pm("task", "add", "--id", "H2", "--title", "second", "--run", "half")
        pm("task", "set", "H1", "status=merged", "--run", "half")
        pm("task", "set", "H2", "status=qa", "--run", "half")
        boom = dict(env)
        boom["MK_PROMOTE_FAIL_AFTER"] = "1"
        half = mk("run", "set", "--id", "half", "--status", "done", "--data", os.path.join(tmp, "promote"),
                  data_arg=False, e=boom)
        assert half["ok"] and half["promoted"] == ["H1"], half
        assert "promote_error" in half and half["promote_error"], half
        got = {t["id"]: t["status"] for t in pm("run", "get", "--id", "half")["run"]["tasks"]}
        assert got["H1"] == "done" and got["H2"] == "qa", got
        # already-done keeps promoting: it repairs a crash between the two writes
        rest = pm("run", "set", "--id", "half", "--status", "done")
        assert rest["ok"] and rest["promoted"] == ["H2"], rest
        assert "promote_error" not in rest

        print("mk-state self-test ok")
        store_self_test(tmp, env)
        print("mk-state store self-test ok")
        # R1.2: the mkdb write guard refuses the real home from this suite too.
        _mkdb().selftest_guard_probe(tmp_home)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_CLOCK = {"t": None}  # the parity test pins now() so the two paths share one clock


def _frozen_now():
    return _CLOCK["t"] or now()


def store_self_test(tmp, env):
    """D3A: the store is opt-in. Off = no mk.db and byte-identical files; on = views from the store."""
    import shutil as _sh
    data = os.path.join(tmp, "store")

    def mk(*args, code=0, e=None):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, "--data", data],
                           capture_output=True, text=True, env=e or env)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    # store off: a scripted run creates no mk.db and no runs-legacy
    assert mk("store", "status")["store"] == "off"
    mk("run", "init", "--id", "s1", "--feature", "Store", "--branch", "b")
    mk("task", "add", "--id", "T1", "--title", "One")
    mk("task", "set", "T1", "status=routed")
    mk("event", "--task", "T1", "--type", "note", "--text", "hello")
    assert not os.path.exists(os.path.join(data, "mk.db")), "store off created mk.db"
    assert not os.path.exists(os.path.join(data, "runs-legacy"))
    off = {}
    for rel in ("runs/s1/run.json", "runs/s1/events.jsonl", "runs/s1/board.md", "runs/index.json"):
        off[rel] = open(os.path.join(data, rel), "rb").read()

    # enable: imports the active run, snapshots runs/ read-only, views survive
    out = mk("store", "enable")
    assert out["store"] == "on" and out["imported"] == "s1", out
    assert os.path.exists(os.path.join(data, "mk.db"))
    legacy = os.path.join(data, "runs-legacy", "s1", "run.json")
    assert open(legacy, "rb").read() == off["runs/s1/run.json"]
    try:  # the copy is read-only
        open(legacy, "a").close()
        raise AssertionError("runs-legacy is writable")
    except PermissionError:
        pass
    for rel in off:  # the first publish reproduces the files the import read
        assert open(os.path.join(data, rel), "rb").read() == off[rel], rel
    assert mk("store", "status")["store"] == "on"
    again = mk("store", "enable")
    assert again["imported"] is None  # already on: nothing re-imported

    db = _mkdb()
    con = db.open_project(os.path.join(data, ".mk-home"), STORE_PROJECT, data_dir=data)
    try:
        n_ev = con.execute("SELECT COUNT(*) c FROM events WHERE run='s1' AND chan='state'").fetchone()["c"]
        n_tasks = con.execute("SELECT COUNT(*) c FROM tasks WHERE run='s1'").fetchone()["c"]
        statuses = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM tasks WHERE run='s1'")}
    finally:
        con.close()
    assert n_tasks == 1 and statuses == {"T1": "routed"}, (n_tasks, statuses)
    assert n_ev == len(off["runs/s1/events.jsonl"].splitlines()), n_ev

    # store on: parity with the same commands run through the file path, one clock for both
    twin = os.path.join(tmp, "twin")
    global now
    real_now, now = now, _frozen_now

    script = [
        ("run", "init", "--id", "s2", "--feature", "Twin", "--branch", "b2"),
        ("task", "add", "--id", "A", "--title", "Alpha", "--run", "s2"),
        ("task", "add", "--id", "B", "--title", "Beta", "--deps", "A", "--run", "s2"),
        ("task", "set", "A", "status=building", "--run", "s2"),
        ("task", "round", "A", "--kind", "build", "--run", "s2"),
        ("event", "--task", "A", "--type", "note", "--text", "working", "--run", "s2"),
        ("wait", "set", "--question", "go?", "--options", "yes,no", "--run", "s2"),
        ("wait", "clear", "--run", "s2"),
        ("task", "set", "A", "status=merged", "--run", "s2"),
    ]
    for step in script:
        _CLOCK["t"] = real_now()
        with contextlib.redirect_stdout(open(os.devnull, "w")):
            assert main([*step, "--data", data]) == 0, step
            assert main([*step, "--data", twin]) == 0, step
    now = real_now  # the rest of the test uses the real clock
    for rel in ("runs/s2/run.json", "runs/s2/events.jsonl", "runs/s2/board.md"):
        a = open(os.path.join(data, rel), "rb").read()
        b = open(os.path.join(twin, rel), "rb").read()
        if a != b:
            open("/tmp/w1-diff-a.json","wb").write(a)
            open("/tmp/w1-diff-b.json","wb").write(b)
            raise AssertionError(rel)
    # index.json: the store run also holds s1, so compare the s2 entry, not the whole file
    ia = [r for r in json.load(open(os.path.join(data, "runs/index.json"))) if r["id"] == "s2"]
    ib = [r for r in json.load(open(os.path.join(twin, "runs/index.json"))) if r["id"] == "s2"]
    assert ia == ib, (ia, ib)
    assert not os.path.exists(os.path.join(twin, "mk.db"))  # the twin never opted in

    # events.jsonl only grows, one line at a time, and matches the file-path twin.
    # One pinned clock for both paths: re-reading now() between them stamps a different second
    # whenever the clock ticks, and the byte compare then fails.
    evp = os.path.join(data, "runs", "s2", "events.jsonl")
    counts = [len(open(evp).read().splitlines())]
    # Both paths go through main() in this process. mk() is a subprocess, so a pinned clock
    # here would never reach it and the two files would stamp different seconds.
    now = _frozen_now
    _CLOCK["t"] = "2000-01-01T00:00:00+00:00"
    grow = (("event", "--type", "note", "--text", "one more", "--run", "s2"),
            ("task", "set", "B", "status=routed", "--run", "s2"))
    try:
        for step in grow:
            with contextlib.redirect_stdout(open(os.devnull, "w")):
                assert main([*step, "--data", data]) == 0, step
            counts.append(len(open(evp).read().splitlines()))
        assert counts == sorted(counts) and len(set(counts)) == len(counts), counts
        for step in grow:
            with contextlib.redirect_stdout(open(os.devnull, "w")):
                assert main([*step, "--data", twin]) == 0, step
        assert open(evp, "rb").read() == open(os.path.join(twin, "runs", "s2", "events.jsonl"), "rb").read()
    finally:
        now = real_now
        _CLOCK["t"] = None

    # a status change goes through transition(): change_log grew and the event is in the store
    con = db.open_project(os.path.join(data, ".mk-home"), STORE_PROJECT, data_dir=data)
    try:
        before = con.execute("SELECT COUNT(*) c FROM change_log WHERE run='s2'").fetchone()["c"]
    finally:
        con.close()
    mk("task", "set", "B", "status=building", "--note", "start", "--run", "s2")
    con = db.open_project(os.path.join(data, ".mk-home"), STORE_PROJECT, data_dir=data)
    try:
        after = con.execute("SELECT COUNT(*) c FROM change_log WHERE run='s2'").fetchone()["c"]
        hit = con.execute("SELECT 1 FROM events WHERE run='s2' AND task='B' AND kind='transition'"
                          " AND to_id='building'").fetchone()
    finally:
        con.close()
    assert after > before, (before, after)
    assert hit, "transition event missing from the store"

    # task round stores the brief/report/review content as artifacts and still writes the files.
    # The files are written by the caller after `task round` prints the paths, so the NEXT
    # command (here a note) is what commits them — the same shape the orchestrator uses.
    paths = mk("task", "round", "B", "--kind", "fix", "--cause", "gap", "--run", "s2")
    for kind in ("brief", "report", "review"):
        with open(paths[kind], "w") as f:
            f.write(f"{kind} body\n")
    mk("event", "--type", "note", "--text", "round files", "--run", "s2")
    con = db.open_project(os.path.join(data, ".mk-home"), STORE_PROJECT, data_dir=data)
    try:
        rows = {r["kind"]: r["text"] for r in con.execute(
            "SELECT kind, text FROM artifacts WHERE run='s2' AND task='B' AND round=? AND kind IN"
            " ('brief','report','review')", (paths["round"],))}
    finally:
        con.close()
    assert rows == {"brief": "brief body\n", "report": "report body\n", "review": "review body\n"}, rows
    for kind in rows:  # the task-folder files are still there, as views
        assert open(paths[kind]).read() == rows[kind]

    # export-legacy rebuilds the files from the store
    saved = {rel: open(os.path.join(data, rel), "rb").read() for rel in
             ("runs/s2/run.json", "runs/s2/events.jsonl", "runs/s2/board.md", "runs/index.json")}
    for rel in saved:
        os.remove(os.path.join(data, rel))
    mk("export-legacy", "--run", "s2")
    for rel, blob in saved.items():
        assert open(os.path.join(data, rel), "rb").read() == blob, rel

    # plan-path fallback: a missing plan file resolves through plans.archived_path
    plan = os.path.join(tmp, "gone.json")
    archived = os.path.join(tmp, "archived.json")
    json.dump({"plan": "gp", "status": "approved", "runs": [], "phases": []}, open(plan, "w"))
    fixture = os.path.join(tmp, "planfix.py")
    open(fixture, "w").write(PLAN_FIXTURE)
    os.chmod(fixture, 0o755)
    e2 = dict(env, MK_PLAN_CMD=f"{sys.executable} {fixture}")
    mk("run", "init", "--id", "s3", "--feature", "P", "--branch", "bp", "--plan", plan, "--force", e=e2)
    _sh.copy(plan, archived)
    os.remove(plan)
    con = db.open_project(os.path.join(data, ".mk-home"), STORE_PROJECT, data_dir=data)
    try:
        con.execute("INSERT INTO plans(id, version, title, approved_at, archived_path) VALUES (?,?,?,?,?)",
                    ("gp", 1, "gp", "2026-01-01T00:00:00+00:00", archived))
        con.commit()
    finally:
        con.close()
    out = mk("plan", "sync", "--run", "s3", e=e2)  # parses through the archived path, not the missing one
    assert out["ok"] and out["plan"] == "gp", out

    # a crash between the store commit and the view rewrite: the next command repairs the views
    ev_before = open(os.path.join(data, "runs", "s2", "events.jsonl"), "rb").read()
    crashed = dict(env, MK_VIEW_CRASH="1")
    bad = mk("event", "--type", "note", "--text", "crash me", "--run", "s2", code=2, e=crashed)
    assert "injected crash" in bad["error"], bad
    assert open(os.path.join(data, "runs", "s2", "events.jsonl"), "rb").read() == ev_before  # view not written
    con = db.open_project(os.path.join(data, ".mk-home"), STORE_PROJECT, data_dir=data)
    try:
        stored = con.execute("SELECT text FROM events WHERE run='s2' AND text='crash me'").fetchone()
    finally:
        con.close()
    assert stored, "the store lost the event committed before the crash"
    mk("event", "--type", "note", "--text", "after", "--run", "s2")  # repairs, then appends
    lines = open(os.path.join(data, "runs", "s2", "events.jsonl")).read().splitlines()
    assert json.loads(lines[-2])["text"] == "crash me" and json.loads(lines[-1])["text"] == "after", lines[-2:]

    # CRITICAL review :1735 — two real subprocesses, 10 task-set each, all 20 stick
    race = os.path.join(tmp, "race")
    mk("run", "init", "--id", "rr", "--feature", "Race", "--branch", "br")
    # the helper above pins --data to `data`; this block uses its own dir through main's argv
    def once(argv, d, e=None):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *argv, "--data", d],
                           capture_output=True, text=True, env=e or env)
        assert r.returncode == 0, (argv, r.returncode, r.stdout, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])
    # re-init on the race dir (mk() wrote into `data`)
    once(["run", "init", "--id", "rr", "--feature", "Race", "--branch", "br"], race)
    for i in range(20):
        once(["task", "add", "--id", f"R{i}", "--title", f"r{i}", "--run", "rr"], race)
    once(["store", "enable"], race)
    procs = []
    for side in (0, 1):
        ids = list(range(side * 10, side * 10 + 10))
        script = "import subprocess,sys\n" + "".join(
            f"subprocess.run([sys.executable,{os.path.abspath(__file__)!r},"
            f"'task','set','R{i}','status=building','--run','rr','--data',{race!r}],check=True)\n"
            for i in ids)
        procs.append(subprocess.Popen([sys.executable, "-c", script], env=env))
    for proc in procs:
        assert proc.wait() == 0, proc.returncode
    final = once(["run", "get", "--id", "rr"], race)["run"]
    got = {t["id"]: t["status"] for t in final["tasks"]}
    assert all(got[f"R{i}"] == "building" for i in range(20)), got
    con = db.open_project(os.path.join(race, ".mk-home"), STORE_PROJECT, data_dir=race)
    try:
        rows = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM tasks WHERE run='rr'")}
    finally:
        con.close()
    assert all(rows[f"R{i}"] == "building" for i in range(20)), rows

    # CRITICAL review :246 — a direct transition survives the next task set
    keep = os.path.join(tmp, "keep")
    once(["run", "init", "--id", "k1", "--feature", "K", "--branch", "bk"], keep)
    once(["task", "add", "--id", "T1", "--title", "One", "--run", "k1"], keep)
    once(["task", "add", "--id", "T2", "--title", "Two", "--run", "k1"], keep)
    once(["store", "enable"], keep)
    db.transition(os.path.join(keep, ".mk-home"), STORE_PROJECT, "k1", "T1", "building", data_dir=keep)
    once(["task", "set", "T2", "status=building", "--run", "k1"], keep)
    con = db.open_project(os.path.join(keep, ".mk-home"), STORE_PROJECT, data_dir=keep)
    try:
        st1 = con.execute("SELECT status FROM tasks WHERE run='k1' AND id='T1'").fetchone()["status"]
    finally:
        con.close()
    assert st1 == "building", st1
    view = json.load(open(os.path.join(keep, "runs", "k1", "run.json")))
    assert next(t["status"] for t in view["tasks"] if t["id"] == "T1") == "building"

    # HIGH review :563 — exactly one transition event, and a direct transition reaches events.jsonl
    con = db.open_project(os.path.join(keep, ".mk-home"), STORE_PROJECT, data_dir=keep)
    try:
        count = con.execute("SELECT COUNT(*) c FROM events WHERE run='k1' AND task='T2'"
                            " AND kind='transition' AND to_id='building'").fetchone()["c"]
    finally:
        con.close()
    assert count == 1, count
    once(["run", "get", "--id", "k1"], keep)  # republish picks up the direct transition
    lines = [json.loads(x) for x in open(os.path.join(keep, "runs", "k1", "events.jsonl"))]
    state_lines = [x for x in lines if x.get("task") == "T1" and x.get("to") == "building"]
    assert len(state_lines) == 1, lines
    tl = db.timeline(os.path.join(keep, ".mk-home"), STORE_PROJECT, "k1", "T1", data_dir=keep)
    file_t1 = [x for x in lines if x.get("task") == "T1"]
    assert len(tl) == len(file_t1), (len(tl), len(file_t1), tl, file_t1)

    # HIGH review :676 — a failed import leaves <data> unchanged (no mk.db, no runs-legacy)
    boom = os.path.join(tmp, "boom")
    once(["run", "init", "--id", "b1", "--feature", "B", "--branch", "bb"], boom)
    st = Store(boom)
    real_db = st.db()
    real_import = real_db.import_history
    def fail_import(*a, **k):
        raise real_db.Fail("injected import failure")
    real_db.import_history = fail_import
    try:
        try:
            st.enable_store()
            raise AssertionError("enable should have failed")
        except real_db.Fail as e:
            assert "injected" in str(e), e
    finally:
        real_db.import_history = real_import
    assert not os.path.exists(os.path.join(boom, "mk.db")), "mk.db left behind"
    assert not os.path.exists(os.path.join(boom, "runs-legacy")), "runs-legacy left behind"
    assert not os.path.exists(os.path.join(boom, "mk.db-wal"))
    # main() turns mkdb.Fail into one JSON line, exit 2, no traceback
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('mkstate', {os.path.abspath(__file__)!r})\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "db = m._mkdb()\n"
        "def fail(*a, **k):\n"
        "    raise db.Fail('injected import failure')\n"
        "db.import_history = fail\n"
        # Store.db() loads mkdb again unless the cache is primed before enable
        f"st = m.Store({boom!r})\n"
        "st._mkdb = db\n"
        "orig = m.Store\n"
        "class S(m.Store):\n"
        "    def db(self):\n"
        "        return db\n"
        "m.Store = S\n"
        f"raise SystemExit(m.main(['store','enable','--data',{boom!r}]))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert not os.path.exists(os.path.join(boom, "mk.db"))

    # HIGH review :733 — export-legacy shows a status written by a direct transition
    once(["run", "get", "--id", "k1"], keep)
    db.transition(os.path.join(keep, ".mk-home"), STORE_PROJECT, "k1", "T1", "review", data_dir=keep)
    once(["export-legacy", "--run", "k1"], keep)
    exported = json.load(open(os.path.join(keep, "runs", "k1", "run.json")))
    assert next(t["status"] for t in exported["tasks"] if t["id"] == "T1") == "review"

    # MEDIUM review :1092 — an archived plan is marked done through plan_file, not the missing path
    plan = os.path.join(tmp, "archplan.json")
    archived = os.path.join(tmp, "archplan-v1.json")
    json.dump({"plan": "ap", "status": "approved", "runs": [], "phases": []}, open(plan, "w"))
    e3 = dict(env, MK_PLAN_CMD=f"{sys.executable} {os.path.join(tmp, 'planfix.py')}")
    if not os.path.exists(os.path.join(tmp, "planfix.py")):
        open(os.path.join(tmp, "planfix.py"), "w").write(PLAN_FIXTURE)
        os.chmod(os.path.join(tmp, "planfix.py"), 0o755)
    once(["run", "init", "--id", "ap1", "--feature", "AP", "--branch", "ba", "--plan", plan, "--force"], keep, e3)
    import shutil as _sh2
    _sh2.copy(plan, archived)
    os.remove(plan)
    con = db.open_project(os.path.join(keep, ".mk-home"), STORE_PROJECT, data_dir=keep)
    try:
        con.execute("INSERT OR REPLACE INTO plans(id, version, title, approved_at, archived_path)"
                    " VALUES (?,?,?,?,?)",
                    ("ap", 1, "ap", "2026-01-01T00:00:00+00:00", archived))
        con.commit()
    finally:
        con.close()
    out = once(["run", "set", "--id", "ap1", "--status", "done"], keep, e3)
    assert out["ok"] and "warning" not in out, out
    assert json.load(open(archived))["status"] == "done"

    # a task.json that is a directory is one JSON line, exit 2, no traceback (store off)
    bad = os.path.join(tmp, "isdir")
    once(["run", "init", "--id", "d1", "--feature", "D", "--branch", "bd"], bad)
    once(["task", "add", "--id", "T", "--title", "Tee", "--run", "d1"], bad)
    tp = os.path.join(bad, "runs", "d1", "tasks", "T", "task.json")
    os.remove(tp)
    os.mkdir(tp)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "task", "set", "T",
                        "status=building", "--run", "d1", "--data", bad],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 1, (r.stdout, r.stderr)
    body = json.loads(lines[0])
    assert body["ok"] is False and body["error"].startswith("IsADirectoryError"), body

    # with the store on, a corrupt or missing view is repaired from the store
    corrupt = os.path.join(tmp, "corrupt")
    once(["run", "init", "--id", "c1", "--feature", "C", "--branch", "bc"], corrupt)
    once(["task", "add", "--id", "T", "--title", "Tee", "--run", "c1"], corrupt)
    once(["store", "enable"], corrupt)
    runp = os.path.join(corrupt, "runs", "c1", "run.json")
    boardp = os.path.join(corrupt, "runs", "c1", "board.md")
    evp = os.path.join(corrupt, "runs", "c1", "events.jsonl")
    with open(runp, "w") as f:
        f.write("{not json")
    with open(boardp, "w") as f:
        f.write("# truncated")
    ev_before = open(evp).read().splitlines()
    open(evp, "w").write("\n".join(ev_before[:-1]) + ("\n" if len(ev_before) > 1 else ""))
    # the marker still says every store row was published; drop it so the missing tail is appended
    os.remove(os.path.join(corrupt, "runs", "c1", ".store-event"))
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "task", "set", "T",
                        "status=building", "--run", "c1", "--data", corrupt],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 1, r.stdout
    body = json.loads(lines[0])
    assert body["ok"] is True, body
    repaired = json.load(open(runp))
    assert next(t["status"] for t in repaired["tasks"] if t["id"] == "T") == "building"
    assert open(boardp).read().count("\n") > 1 and "truncated" not in open(boardp).read()
    ev_after = open(evp).read().splitlines()
    assert ev_after[:len(ev_before) - 1] == ev_before[:-1]  # the kept prefix was not rewritten
    assert len(ev_after) > len(ev_before) - 1  # the missing tail came back, then the new event
    # a deleted run.json is rewritten too
    os.remove(runp)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "get", "--id", "c1",
                        "--data", corrupt], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and json.loads(r.stdout)["ok"] is True, r.stdout
    assert json.load(open(runp))["id"] == "c1"

    # D3B: every transition upserts the root card; a kill between the two writes is repaired by sync
    root = os.path.join(tmp, "d3b-home")
    d3 = os.path.join(tmp, "d3b")
    d3env = dict(env, MATRIXKODER_HOME=root, MK_HOME=root)
    once(["run", "init", "--id", "d1", "--feature", "D", "--branch", "bd"], d3)
    once(["task", "add", "--id", "T1", "--title", "Rooted", "--run", "d1"], d3)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "store", "enable", "--data", d3],
                       capture_output=True, text=True, env=d3env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "task", "set", "T1",
                        "status=building", "--run", "d1", "--data", d3],
                       capture_output=True, text=True, env=d3env)
    assert r.returncode == 0, r.stdout
    rootdb = db.open_root(os.path.join(d3, ".mk-home"))
    try:
        card = rootdb.execute(
            "SELECT status, change_no FROM cards WHERE project=? AND run='d1' AND task='T1'",
            (STORE_PROJECT,)).fetchone()
    finally:
        rootdb.close()
    assert card and card["status"] == "building", card
    killed = dict(d3env, MK_ROOT_CRASH="1")
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "task", "set", "T1",
                        "status=review", "--run", "d1", "--data", d3],
                       capture_output=True, text=True, env=killed)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    con = db.open_project(os.path.join(d3, ".mk-home"), STORE_PROJECT, data_dir=d3)
    try:
        proj = con.execute("SELECT status FROM tasks WHERE run='d1' AND id='T1'").fetchone()["status"]
    finally:
        con.close()
    assert proj == "review", proj  # the project commit landed
    rootdb = db.open_root(os.path.join(d3, ".mk-home"))
    try:
        stale = rootdb.execute(
            "SELECT status FROM cards WHERE project=? AND run='d1' AND task='T1'",
            (STORE_PROJECT,)).fetchone()["status"]
    finally:
        rootdb.close()
    assert stale == "building", stale  # the root write was the one that died
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "sync", "--data", d3],
                       capture_output=True, text=True, env=d3env)
    assert r.returncode == 0, r.stdout
    first = json.loads(r.stdout)
    assert first["copied"] >= 1, first
    rootdb = db.open_root(os.path.join(d3, ".mk-home"))
    try:
        fixed = rootdb.execute(
            "SELECT status FROM cards WHERE project=? AND run='d1' AND task='T1'",
            (STORE_PROJECT,)).fetchone()["status"]
        seen = rootdb.execute("SELECT last_change_no FROM sync WHERE project=?",
                              (STORE_PROJECT,)).fetchone()["last_change_no"]
    finally:
        rootdb.close()
    assert fixed == "review", fixed
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "sync", "--data", d3],
                       capture_output=True, text=True, env=d3env)
    second = json.loads(r.stdout)
    assert second["copied"] == 0, second  # idempotent
    rootdb = db.open_root(os.path.join(d3, ".mk-home"))
    try:
        again = rootdb.execute("SELECT last_change_no FROM sync WHERE project=?",
                               (STORE_PROJECT,)).fetchone()["last_change_no"]
    finally:
        rootdb.close()
    assert again == seen

    # file mode: sync creates nothing
    off = os.path.join(tmp, "d3b-off")
    once(["run", "init", "--id", "o1", "--feature", "O", "--branch", "bo"], off)
    before = set(os.listdir(off))
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "sync", "--data", off],
                       capture_output=True, text=True, env=d3env)
    assert r.returncode == 0 and json.loads(r.stdout)["store"] == "off", r.stdout
    assert set(os.listdir(off)) == before and not os.path.exists(os.path.join(off, "mk.db"))
    assert not os.path.exists(os.path.join(root, "mk.db")), "file mode created a root store"
    # a resolve with no root store must not create one
    bare = os.path.join(tmp, "bare-home")
    repo = os.path.join(tmp, "bare-repo")
    os.makedirs(os.path.join(repo, ".git"))
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "list"],
                       cwd=repo, capture_output=True, text=True,
                       env=dict(env, MATRIXKODER_HOME=bare, MK_HOME=bare, HOME=os.path.join(tmp, "nowhere")))
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert not os.path.exists(os.path.join(bare, "mk.db")), "resolve created a root store"

    # resolve order: root projects table beats projects.json; --data and the env beat both
    reg_home = os.path.join(tmp, "reg-home")
    reg_repo = os.path.join(tmp, "reg-repo")
    os.makedirs(os.path.join(reg_repo, ".git"))
    from_root = os.path.join(tmp, "from-root")
    from_json = os.path.join(tmp, "from-json")
    os.makedirs(reg_home)
    # projects.json alone (no root store yet)
    write_json(os.path.join(reg_home, "projects.json"),
               {os.path.realpath(reg_repo): {"data_dir": from_json}})
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "init", "--id", "j1",
                        "--feature", "J", "--branch", "bj"], cwd=reg_repo,
                       capture_output=True, text=True, env=dict(env, MATRIXKODER_HOME=reg_home, MK_HOME=reg_home))
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert os.path.exists(os.path.join(from_json, "runs", "j1", "run.json"))
    assert not os.path.exists(os.path.join(reg_home, "mk.db"))
    # register writes BOTH, and the root table then wins over a stale projects.json
    saved = {k: os.environ.get(k) for k in ("MK_HOME", "MATRIXKODER_HOME")}
    os.environ["MK_HOME"] = reg_home
    os.environ["MATRIXKODER_HOME"] = reg_home
    try:
        register_project("reg", reg_repo, from_root)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert os.path.exists(os.path.join(reg_home, "mk.db"))
    # an invalid slug is refused before any file is written
    slug_home = os.path.join(tmp, "slug-home")
    os.makedirs(slug_home)
    os.environ["MK_HOME"] = slug_home
    os.environ["MATRIXKODER_HOME"] = slug_home
    try:
        for bad in ("../x", "X", "", "a" * 42):
            try:
                register_project(bad, reg_repo, from_root)
                raise AssertionError(f"slug {bad!r} was accepted")
            except Exception as e:
                assert type(e).__name__ == "Fail", (bad, type(e), e)
                assert "slug" in str(e), (bad, e)
        assert not os.path.exists(os.path.join(slug_home, "projects.json"))
        assert not os.path.exists(os.path.join(slug_home, "mk.db"))
        assert os.listdir(slug_home) == []
    finally:
        os.environ["MK_HOME"] = reg_home
        os.environ["MATRIXKODER_HOME"] = reg_home
    listed = json.load(open(os.path.join(reg_home, "projects.json")))
    assert listed[os.path.realpath(reg_repo)]["data_dir"] == os.path.abspath(from_root)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "init", "--id", "rt1",
                        "--feature", "R", "--branch", "br"], cwd=reg_repo,
                       capture_output=True, text=True, env=dict(env, MATRIXKODER_HOME=reg_home, MK_HOME=reg_home))
    assert r.returncode == 0, r.stdout
    assert os.path.exists(os.path.join(from_root, "runs", "rt1", "run.json"))
    env_over = dict(env, MATRIXKODER_HOME=reg_home, MK_HOME=reg_home, MATRIXKODER_DATA=os.path.join(tmp, "env-over"))
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "init", "--id", "ev1",
                        "--feature", "E", "--branch", "be"], cwd=reg_repo,
                       capture_output=True, text=True, env=env_over)
    assert r.returncode == 0, r.stdout
    assert os.path.exists(os.path.join(tmp, "env-over", "runs", "ev1", "run.json"))

    # plan sync on the store: a re-imported plan is diffed, and --apply writes the new version
    plan = os.path.join(tmp, "d3b-plan.json")
    body = dict(PLAN_BASE)
    json.dump(body, open(plan, "w"))
    tool = os.path.join(tmp, "d3b-plan-tool.py")
    open(tool, "w").write(PLAN_FIXTURE)
    penv = dict(d3env, MK_PLAN_CMD=sys.executable + " " + tool)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "init", "--id", "p1",
                        "--feature", "P", "--branch", "bp", "--plan", plan, "--data", d3],
                       capture_output=True, text=True, env=penv)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    con = db.open_project(os.path.join(d3, ".mk-home"), STORE_PROJECT, data_dir=d3)
    try:
        con.execute("INSERT INTO plans(id, version, title, header_json, body_md, approved_at)"
                    " VALUES (?,?,?,?,?,?)",
                    ("tplan", 1, "tplan", json.dumps({"sha": "old", "status": "running", "phases": []}),
                     "{}", now()))
        con.commit()
    finally:
        con.close()
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "plan", "sync", "--run", "p1",
                        "--data", d3], capture_output=True, text=True, env=penv)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    dry = json.loads(r.stdout)
    assert dry["apply"] is False and "sha" in dry["version_diff"], dry
    con = db.open_project(os.path.join(d3, ".mk-home"), STORE_PROJECT, data_dir=d3)
    try:
        still = con.execute("SELECT version FROM plans WHERE id='tplan'").fetchone()["version"]
    finally:
        con.close()
    assert still == 1  # dry run wrote nothing
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "plan", "sync", "--run", "p1",
                        "--apply", "--data", d3], capture_output=True, text=True, env=penv)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    applied = json.loads(r.stdout)
    assert applied["version"] == 2, applied
    con = db.open_project(os.path.join(d3, ".mk-home"), STORE_PROJECT, data_dir=d3)
    try:
        row = con.execute("SELECT version, header_json FROM plans WHERE id='tplan'").fetchone()
    finally:
        con.close()
    assert row["version"] == 2 and json.loads(row["header_json"])["sha"] == applied["sha"]
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "plan", "sync", "--run", "p1",
                        "--data", d3], capture_output=True, text=True, env=penv)
    quiet = json.loads(r.stdout)
    assert quiet["version_diff"] == [], quiet  # the stored version now matches

    # run archive with the store on writes a backup that opens and holds the run
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "archive", "--id", "d1",
                        "--data", d3], capture_output=True, text=True, env=d3env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    backup = os.path.join(d3, "archive", "d1.db")
    assert os.path.exists(backup), backup
    bcon = db._connect(backup)
    try:
        got = bcon.execute("SELECT id FROM runs").fetchall()
        tasks = bcon.execute("SELECT id FROM tasks WHERE run='d1'").fetchall()
    finally:
        bcon.close()
    assert any(g[0] == "d1" for g in got), got
    assert any(t[0] == "T1" for t in tasks), tasks
    # a second archive keeps the first backup and writes a timestamped second file
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "archive", "--id", "d1",
                        "--data", d3], capture_output=True, text=True, env=d3env)
    assert r.returncode == 0, r.stdout
    names = sorted(os.listdir(os.path.join(d3, "archive")))
    assert "d1.db" in names and any(n.startswith("d1-") and n.endswith(".db") for n in names), names
    assert open(backup, "rb").read()  # the first backup is still a file
    # an injected VACUUM failure leaves the old backup, the run status and no tmp file
    status_before = json.load(open(os.path.join(d3, "runs", "d1", "run.json")))["status"]
    kept = open(backup, "rb").read()
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "archive", "--id", "d1",
                        "--data", d3], capture_output=True, text=True,
                       env=dict(d3env, MK_ARCHIVE_FAIL="1"))
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    body = json.loads(r.stdout)
    assert body["ok"] is False and "backup" in body["error"], body
    assert open(backup, "rb").read() == kept
    assert not os.path.exists(os.path.join(d3, "archive", "d1.db.tmp"))
    assert json.load(open(os.path.join(d3, "runs", "d1", "run.json")))["status"] == status_before
    # file mode archive writes no backup
    off_before = os.listdir(off)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "archive", "--id", "o1",
                        "--data", off], capture_output=True, text=True, env=d3env)
    assert r.returncode == 0, r.stdout
    assert "archive" not in os.listdir(off) and not os.path.exists(os.path.join(off, "mk.db"))
    assert set(os.listdir(off)) >= set(off_before)

    # K2 store on: --tests 384/387 refused (task stays, no event); 350/0 still passes;
    # a raise on the 2nd promotion still lists the 1st.
    k2 = os.path.join(tmp, "k2on")
    k2env = dict(env, MATRIXKODER_HOME=os.path.join(tmp, "k2on-home"), MK_HOME=os.path.join(tmp, "k2on-home"))
    once(["run", "init", "--id", "k2", "--feature", "K2", "--branch", "bk"], k2, k2env)
    once(["task", "add", "--id", "V", "--title", "verify", "--run", "k2"], k2, k2env)
    once(["store", "enable"], k2, k2env)
    once(["event", "--type", "decision", "--text", "baseline: tests 0/0 · selftest 0/0", "--run", "k2"], k2, k2env)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "task", "verify", "V",
                        "--tests", "384/387", "--run", "k2", "--data", k2],
                       capture_output=True, text=True, env=k2env)
    assert r.returncode == 2, r.stdout
    body = json.loads(r.stdout.strip())
    assert body["ok"] is False and "384/387" in body["error"] and "<passed>/<failed>" in body["error"], body
    got = once(["task", "get", "V", "--run", "k2"], k2, k2env)
    assert got["task"]["status"] == "planned", got
    # the refused verify wrote no verdict
    assert "384/387" not in open(os.path.join(k2, "runs", "k2", "events.jsonl")).read()
    passed = once(["task", "verify", "V", "--tests", "350/0", "--run", "k2"], k2, k2env)
    assert passed["result"] == "pass" and passed["task"]["status"] == "review", passed
    once(["task", "add", "--id", "A", "--title", "a", "--run", "k2"], k2, k2env)
    once(["task", "add", "--id", "B", "--title", "b", "--run", "k2"], k2, k2env)
    once(["task", "set", "A", "status=merged", "--run", "k2"], k2, k2env)
    once(["task", "set", "B", "status=qa", "--run", "k2"], k2, k2env)
    boom = dict(k2env, MK_PROMOTE_FAIL_AFTER="1")
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "set", "--id", "k2",
                        "--status", "done", "--data", k2], capture_output=True, text=True, env=boom)
    assert r.returncode == 0, r.stdout
    body = json.loads(r.stdout.strip())
    assert body["promoted"] == ["A"], body
    assert "promote_error" in body and "B" in body["promote_error"], body
    tasks = {t["id"]: t["status"] for t in once(["run", "get", "--id", "k2"], k2, k2env)["run"]["tasks"]}
    assert tasks["A"] == "done" and tasks["B"] == "qa", tasks
    rest = once(["run", "set", "--id", "k2", "--status", "done"], k2, k2env)
    assert rest["promoted"] == ["B"] and "promote_error" not in rest, rest

    # two real processes, store already on, 10 task-set each: project tasks match root cards
    par = os.path.join(tmp, "par")
    par_home = os.path.join(tmp, "par-home")
    parenv = dict(env, MATRIXKODER_HOME=par_home, MK_HOME=par_home)
    once(["run", "init", "--id", "pr", "--feature", "Par", "--branch", "bp"], par, parenv)
    for i in range(20):
        once(["task", "add", "--id", f"P{i}", "--title", f"p{i}", "--run", "pr"], par, parenv)
    once(["store", "enable"], par, parenv)
    assert os.path.exists(os.path.join(par, "mk.db"))
    script = os.path.abspath(__file__)
    procs = []
    for side in (0, 1):
        ids = list(range(side * 10, side * 10 + 10))
        code = "import subprocess,sys\n" + "".join(
            f"subprocess.run([sys.executable,{script!r},"
            f"'task','set','P{i}','status=building','--run','pr','--data',{par!r}],check=True)\n"
            for i in ids)
        procs.append(subprocess.Popen([sys.executable, "-c", code], env=parenv))
    for proc in procs:
        assert proc.wait() == 0, proc.returncode
    con = db.open_project(os.path.join(par, ".mk-home"), STORE_PROJECT, data_dir=par)
    try:
        proj = {r["id"]: r["status"] for r in con.execute(
            "SELECT id, status FROM tasks WHERE run='pr'")}
    finally:
        con.close()
    rootdb = db.open_root(os.path.join(par, ".mk-home"))
    try:
        cards = {r["task"]: r["status"] for r in rootdb.execute(
            "SELECT task, status FROM cards WHERE project=? AND run='pr'", (STORE_PROJECT,))}
    finally:
        rootdb.close()
    assert set(proj) == {f"P{i}" for i in range(20)}, proj
    assert proj == cards, (proj, cards)

    # D3C: store enable imports every run, not only the active one
    many = os.path.join(tmp, "many")
    once(["run", "init", "--id", "r1", "--feature", "F", "--branch", "b1"], many)
    once(["run", "init", "--id", "r2", "--feature", "G", "--branch", "b2"], many)
    once(["run", "set", "--id", "r2", "--status", "done"], many)
    before = {}
    for rid in ("r1", "r2"):
        before[rid] = json.load(open(os.path.join(many, "runs", rid, "run.json")))
    enabled = once(["store", "enable"], many)
    assert enabled["imported"] == ["r1", "r2"], enabled
    got_r2 = once(["run", "get", "--id", "r2"], many)
    assert got_r2["ok"] and got_r2["run"]["id"] == "r2" and got_r2["run"]["status"] == "done", got_r2
    for rid in ("r1", "r2"):
        after = once(["run", "get", "--id", rid], many)["run"]
        assert after == before[rid], rid

    # D3C: a rewound marker must not append an event line that is already the tail
    rew = os.path.join(tmp, "rew")
    once(["run", "init", "--id", "s1", "--feature", "S", "--branch", "bs"], rew)
    once(["task", "add", "--id", "A1", "--title", "a", "--run", "s1"], rew)
    once(["store", "enable"], rew)
    once(["event", "--run", "s1", "--type", "note", "--text", "grow1"], rew)
    marker = os.path.join(rew, "runs", "s1", ".store-event")
    open(marker, "w").write("2\n")  # as it was before the grow1 command
    once(["event", "--run", "s1", "--type", "note", "--text", "after"], rew)
    ev = open(os.path.join(rew, "runs", "s1", "events.jsonl")).read()
    assert ev.count("grow1") == 1, ev.count("grow1")
    assert ev.count("after") == 1

    # D3C: SIGKILL between the marker write and the append, 20 trials, always the right line count
    kills = os.path.join(tmp, "kills")
    once(["run", "init", "--id", "s1", "--feature", "S", "--branch", "bs"], kills)
    once(["task", "add", "--id", "A1", "--title", "a", "--run", "s1"], kills)
    once(["store", "enable"], kills)
    script = os.path.abspath(__file__)
    for trial in range(20):
        before_n = len(open(os.path.join(kills, "runs", "s1", "events.jsonl")).read().splitlines())
        envk = dict(env, MK_PUBLISH_KILL="1")
        r = subprocess.run([sys.executable, script, "event", "--run", "s1", "--type", "note",
                            "--text", f"kill{trial}", "--data", kills],
                           capture_output=True, text=True, env=envk)
        assert r.returncode < 0 or r.returncode > 0  # SIGKILL is a negative wait status, or the shell's 137
        # the next command converges: the killed event is published once, then this note once
        once(["event", "--run", "s1", "--type", "note", "--text", f"ok{trial}"], kills)
        lines = open(os.path.join(kills, "runs", "s1", "events.jsonl")).read().splitlines()
        texts = [json.loads(x).get("text") for x in lines]
        assert texts.count(f"kill{trial}") == 1, (trial, texts.count(f"kill{trial}"), len(lines), before_n)
        assert texts.count(f"ok{trial}") == 1, trial
        assert len(lines) == before_n + 2, (trial, len(lines), before_n)

    # D3C: task order of the artifact survives enable (parents are not sorted ahead of children)
    order = os.path.join(tmp, "order")
    once(["run", "init", "--id", "o1", "--feature", "O", "--branch", "bo"], order)
    once(["task", "add", "--id", "BA", "--title", "ba", "--run", "o1"], order)
    once(["task", "add", "--id", "BA.1", "--title", "ba1", "--parent", "BA", "--run", "o1"], order)
    once(["task", "add", "--id", "BA.2", "--title", "ba2", "--parent", "BA", "--run", "o1"], order)
    once(["task", "add", "--id", "BB", "--title", "bb", "--run", "o1"], order)
    pre_ids = [t["id"] for t in json.load(open(os.path.join(order, "runs", "o1", "run.json")))["tasks"]]
    assert pre_ids == ["BA", "BA.1", "BA.2", "BB"], pre_ids
    pre_run = open(os.path.join(order, "runs", "o1", "run.json"), "rb").read()
    pre_board = open(os.path.join(order, "runs", "o1", "board.md"), "rb").read()
    pre_index = open(os.path.join(order, "runs", "index.json"), "rb").read()
    once(["store", "enable"], order)
    assert open(os.path.join(order, "runs", "o1", "run.json"), "rb").read() == pre_run
    assert open(os.path.join(order, "runs", "o1", "board.md"), "rb").read() == pre_board
    assert open(os.path.join(order, "runs", "index.json"), "rb").read() == pre_index
    post_ids = [t["id"] for t in once(["run", "get", "--id", "o1"], order)["run"]["tasks"]]
    assert post_ids == ["BA", "BA.1", "BA.2", "BB"], post_ids

    # D3C: a stale mk-enable-* dir is removed; a young one is left alone
    stale = os.path.join(tmp, "stale")
    once(["run", "init", "--id", "s1", "--feature", "S", "--branch", "bs"], stale)
    old = os.path.join(stale, "mk-enable-old")
    young = os.path.join(stale, "mk-enable-young")
    os.makedirs(old)
    os.makedirs(young)
    old_t = time.time() - 120
    os.utime(old, (old_t, old_t))
    once(["store", "enable"], stale)
    assert not os.path.exists(old), "stale enable dir survived"
    assert os.path.exists(young), "young enable dir was removed"

    # M2 R2: `ready` survives the next ordinary write, and a regress demotes it.
    # Through the real CLI, not mkdb.promote_ready directly.
    rd = os.path.join(tmp, "ready")
    once(["run", "init", "--id", "rr", "--feature", "Ready", "--branch", "br"], rd)
    once(["store", "enable"], rd)
    once(["task", "add", "--id", "A", "--title", "dep", "--run", "rr"], rd)
    once(["task", "add", "--id", "B", "--title", "needs A", "--deps", "A", "--run", "rr"], rd)
    once(["task", "add", "--id", "Z", "--title", "unrelated", "--run", "rr"], rd)
    once(["task", "set", "A", "status=done", "--run", "rr"], rd)

    def store_status(tid):
        c = _mkdb().open_project(os.path.join(rd, ".mk-home"), STORE_PROJECT, data_dir=rd)
        try:
            return c.execute("SELECT status FROM tasks WHERE run='rr' AND id=?", (tid,)).fetchone()["status"]
        finally:
            c.close()

    assert store_status("B") == "ready", store_status("B")
    view = once(["task", "get", "B", "--run", "rr"], rd)["task"]
    assert view["status"] == "planned" and view["ready"] is True, view  # file view keeps the flag
    # the next unrelated write must NOT demote B
    once(["task", "set", "Z", "status=building", "--run", "rr"], rd)
    assert store_status("B") == "ready", "an unrelated task set demoted B to " + store_status("B")
    assert store_status("Z") == "building"
    # the root card shows Ready
    _mkdb().catch_up(os.path.join(rd, ".mk-home"), STORE_PROJECT, data_dir=rd)
    cards = _mkdb().cards(os.path.join(rd, ".mk-home"), STORE_PROJECT)
    col = next(c["col"] for c in cards if c["task"] == "B" and c["run"] == "rr")
    assert col == "Ready", col
    # a regress demotes: A done -> fix puts B back to planned
    once(["task", "set", "A", "status=fix", "--run", "rr"], rd)
    assert store_status("B") == "planned", "a regressed dep left B " + store_status("B")
    view = once(["task", "get", "B", "--run", "rr"], rd)["task"]
    assert view["ready"] is False, view

    # M2 R3: a routed task whose deps are met stays routed, in both orders.
    rt = os.path.join(tmp, "routed")
    once(["run", "init", "--id", "rr", "--feature", "Routed", "--branch", "br"], rt)
    once(["store", "enable"], rt)
    once(["task", "add", "--id", "A", "--title", "dep", "--run", "rr"], rt)
    once(["task", "add", "--id", "B", "--title", "needs A", "--deps", "A", "--run", "rr"], rt)
    once(["task", "add", "--id", "Z", "--title", "unrelated", "--run", "rr"], rt)
    once(["task", "set", "A", "status=done", "--run", "rr"], rt)
    got = once(["task", "set", "B", "status=routed", "--run", "rr"], rt)
    assert got["task"]["status"] == "routed" and got["task"]["ready"] is True, got["task"]
    once(["task", "set", "Z", "status=building", "--run", "rr"], rt)
    view = once(["task", "get", "B", "--run", "rr"], rt)["task"]
    assert view["status"] == "routed" and view["ready"] is True, view

    rt2 = os.path.join(tmp, "routed2")
    once(["run", "init", "--id", "rr", "--feature", "Routed", "--branch", "br"], rt2)
    once(["store", "enable"], rt2)
    once(["task", "add", "--id", "A", "--title", "dep", "--run", "rr"], rt2)
    once(["task", "add", "--id", "B", "--title", "needs A", "--deps", "A", "--run", "rr"], rt2)
    once(["task", "set", "B", "status=routed", "--run", "rr"], rt2)
    once(["task", "set", "A", "status=done", "--run", "rr"], rt2)
    view = once(["task", "get", "B", "--run", "rr"], rt2)["task"]
    assert view["status"] == "routed" and view["ready"] is True, view

    # M2 R4: a routed task with met deps is not demoted and re-promoted on every write.
    # Three unrelated writes after the promotion add no change_log row for B.
    ch = os.path.join(tmp, "churn")
    once(["run", "init", "--id", "rr", "--feature", "Churn", "--branch", "br"], ch)
    once(["store", "enable"], ch)
    once(["task", "add", "--id", "A", "--title", "dep", "--run", "rr"], ch)
    once(["task", "add", "--id", "B", "--title", "needs A", "--deps", "A", "--run", "rr"], ch)
    once(["task", "add", "--id", "Z", "--title", "unrelated", "--run", "rr"], ch)
    once(["task", "set", "A", "status=done", "--run", "rr"], ch)
    once(["task", "set", "B", "status=routed", "--run", "rr"], ch)

    def b_changes():
        c = _mkdb().open_project(os.path.join(ch, ".mk-home"), STORE_PROJECT, data_dir=ch)
        try:
            return c.execute("SELECT COUNT(*) n FROM change_log WHERE run='rr' AND task='B'").fetchone()["n"]
        finally:
            c.close()

    after_promote = b_changes()
    for status in ("building", "verifying", "review"):
        once(["task", "set", "Z", "status=" + status, "--run", "rr"], ch)
    assert b_changes() == after_promote, (after_promote, b_changes())
    view = once(["task", "get", "B", "--run", "rr"], ch)["task"]
    assert view["status"] == "routed" and view["ready"] is True, view


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        sys.exit(main(sys.argv[1:]))
