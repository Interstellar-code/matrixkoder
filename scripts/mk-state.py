#!/usr/bin/env python3
"""MatrixKoder run state — the ONLY writer of the project store. Python 3 stdlib only.

L4 (C4): the project DB is the ONLY source of truth — no run.json / index.json / task.json /
events.jsonl / board.md / run-doc artifact is written or read. The store is always on and is
created through mkdb.open_project on first use; `store enable` / `export-legacy` are kept as
no-op commands for callers. `render` prints the board text from the DB (stdout), never board.md.
Legacy projects whose runs live only in files migrate through mk-store-import.py.

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
      --status done (F.0) closes a run ONLY when nothing of it is open and every runnable plan
      gate passes. Open = any task or phase not in merged/qa/done; the refusal lists each open
      id with its status (`open`) and writes nothing. Then the plan's `[gates]` run in the run's
      integration checkout (the repo this run belongs to, resolved the way --data resolves its
      repo; never a hard-coded path): a plain string gate is `manual` — never run, never blocks;
      a `[gates.<name>]` table {cmd, expect = 0, timeout = 600} must exit `expect`, inside its
      timeout, with the 1-minute load average at or below 40 (above that every gate is
      `deferred` and blocks; the load reader is `load_avg()`). A gate whose cmd matches a project.yaml
      `banned_commands` entry is `banned`: blocks, never run. A failure names the gate, its
      result (`fail exit=N` | `timeout` | `banned` | `deferred load=X`) and the last 20 lines of its output,
      and writes nothing. The accepted reply lists every gate with its result (`gates`).
      `run set` takes no --force, and none is added: F.0 has no bypass. Reading the gates is
      best-effort like the plan update below (no plan link, a missing plan file or a plan-tool
      error reports `gate_error` instead of blocking).
  run stage N [--id ID] | run archive --id ID | run archive ID
  run backfill-fields [--run ID] — one-shot: fill the EMPTY acceptance/refs/owned/deps/type/route
      of this run's tasks from the approved plan (D1.5.5). The plan tool is the only reader of
      the plan file (C3). Links match case-insensitively: a phase item by plan_phase, a task
      item by plan_task, a subtask item by its id. Plan items with no run task are reported as
      unmatched; fields already set are never overwritten (idempotent). status/lane/round/
      verdict/slot/branch/workspace/pane are not touched, route_json/agent/model/effort are not
      touched (route_json comes from dispatch, D1.5.3). Writes through the normal save path, so
      the store mirror and the run-doc artifact update together; ONE summary note event.
  task add --id T --title TEXT [--wave N] [--deps A,RUN:B] [--owned glob,glob] (--parent P | --root)
      [--followup]
      ids are case-insensitive unique; deps store the real id case and a self-dep is refused;
      RUN:B is a task in another run of the same data dir; --parent onto merged/qa/done is
      refused unless --followup, which adds the task anyway and rolls that phase back to
      `building` with a "follow-up <id> added" note, so its status rolls up from its children
      again; warns when an owned glob overlaps an unmerged task in any active run
      (the new task's own --parent chain is not an overlap: a parent owns its children's globs)
  task set T key=value ... [--note TEXT] [--force]   keys: status agent model effort lane pane
      branch workspace slot last_commit gates verdict reviewer parent title acceptance owned deps refs
      (branch/workspace/slot are written by mk-lane.py acquire; reviewer comes from the plan's phase
      or task set; owned/deps/refs take comma lists, an empty value clears)
      title/acceptance/owned/deps/refs may be edited only while the task is planned or routed: on a
      started task the key is dropped (nothing written) and the reply carries a warning, exit 0 —
      the same rule plan sync applies; each edited key writes one decision event with old and new
      value plus the --note; deps are validated like task add (stored id case, a self-dep refused,
      an unknown target warned)
      parent=P re-parents T (P must exist in this run and may not be T or anything below T);
      parent= with an empty value makes T a root, i.e. a phase. T's type and the types under it
      are re-derived, and the old and the new parent roll up in the same save.
      status=dispatched|building needs every own AND ancestor dep merged/qa/done (--force overrides);
      status= on a parent fans out: every child is set (one transition each, the same --note; the
      parent itself is never set directly, it rolls up from the children); status aliases
      reviewing/merging/fixing are stored as review/merged/fix; a task whose deps are all met gets
      ready=true and a "deps met" event; an unreadable sibling run only produces a warnings entry
  task round T --kind build|fix|takeover|rework [--cause TEXT]
  task round --tasks C1,S1,P1 --kind ... — a lane_group in one round: the lead (first id) owns the one
      brief/report/review path (tasks/C1/brief-rN.md); the round number and kind are recorded on EVERY
      listed task, one event each. N is the highest current round of the listed tasks plus one. T, if
      given, must equal the first id. Every id must exist and share the lead's lane_group (a child
      task takes its parent's); else ok:false and nothing is written. Not with --undo.
      X3.1: the round's artifacts are store-authored — `task brief T --run R --file P | --stdin`
      stores the brief (kind brief, rev replaces) and rewrites the view file from it; a bus
      report/review lands as artifacts the same way (X2). `task round` still prints the paths
      of the three round files (recreated from the artifacts by `task export T --run R`, X3.3)
  task brief T --file P | --stdin — X3.1: store the CURRENT round's brief now; replace-per-round
  task export T — X3.3: write brief/report/review-rN.md back FROM the artifacts (all rounds),
      idempotent; a file whose content differs from the artifact is left and named, not clobbered
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
  plan sync [--run R] [--apply] [--from-file] — diff the run's plan file against its tasks and add or
      update the planned ones. Dry run by default (nothing written). On a live run — at least one
      task not merged/done — it REFUSES (the tasks are the source; `task set` edits are the way to
      change them) unless --from-file, which applies the file anyway. An all-terminal or task-less
      run is unchanged either way. A started task's plan diff is a warning, never a write.
  event [--task T] --type transition|decision|verdict|quota|note --text TEXT [--from S] [--to S]  (transition
      needs --to; --from/--to take the status aliases and store the canonical word)
  wait set --question TEXT --options a,b [--rec TEXT] | wait clear
  lock take RES --reason TEXT [--ttl-min 30] [--scope a,b] [--force-stale]
  lock release RES [--force] (refuses a lock taken by another --by) | lock check RES
  project register --slug S --repo R [--name N] — upsert the project in the root projects table
  render — print the board text from the project DB (stdout, never board.md)
  store enable [--data D] — L4 no-op kept for callers: the store is always on. It reports store on
      and imports nothing; the project store (mkpaths.project_folder: <home>/projects/<slug>/mk.db
      once migrated, else <data>/mk.db) is created through mkdb.open_project on first use.
  store status [--data D] — {"ok": true, "store": "on"}
  sync [--project SLUG] — copy every project change above sync.last_change_no into the root cards
      (idempotent).
  export-legacy --run R [--data D] — L4 no-op kept for callers: no file view is written, so there
      is nothing to rebuild; the store is the only record.
  --self-test

Common: --data DIR (else $MATRIXKODER_DATA, else the root projects table by repo path, else
<home>/projects.json, else <main checkout of cwd's git repo>/.matrixkoder; a linked worktree
resolves to its main checkout, a nested repo to itself), --by WHO (default orchestrator), --run ID for
task/event/wait/render (default: the single active run). Global dir (locks/) is the MK home — the
rule (one of the two home env names, never both disagreeing) lives in scripts/mkpaths.py (C1).
Every command prints ONE JSON line. Errors: {"ok": false, "error": ...} exit 2; lock refused: exit 1.
The project DB is the only truth (C4); run.json / index.json / task.json / events.jsonl / board.md
are never written. Legacy projects migrate through mk-store-import.py.
Schema: docs/state-schema.md
"""
import argparse, contextlib, datetime, fcntl, fnmatch, hashlib, importlib.util, json, math, os, re, shlex, shutil, signal, socket, subprocess, sys, tempfile, threading, time

SCHEMA = 1
STATUSES = ("planned routed dispatched building verifying review fix rework merged qa done "
            "blocked quota stalled provider").split()
STATUS_ALIASES = {"reviewing": "review", "merging": "merged", "fixing": "fix"}  # input aliases (F2.2)
EVENT_TYPES = ("transition", "decision", "verdict", "quota", "note")
TASK_KEYS = ("status", "agent", "model", "effort", "lane", "pane", "branch", "workspace", "slot",
             "last_commit", "gates", "verdict", "reviewer", "parent",  # parent= re-parents a task
             "title", "acceptance", "owned", "deps", "refs")  # T1: the plan text task set can edit
# T1: the keys that take a comma list (task add's --owned/--deps parsing; an empty value clears)
SET_LIST_KEYS = ("owned", "deps", "refs")
# T1: the keys only a task that has not started accepts. A started task (building, verifying,
# review, fix, merged, done, ...) keeps its text: the key is dropped and the reply warns —
# the same rule `plan sync` applies, one writer's worth of it.
SET_START_KEYS = ("title", "acceptance", "owned", "deps", "refs")
EDITABLE_WHILE = ("planned", "routed")
# F.2: the plan's gate fields (and the stored names they ride under) are never settable by task set
GATE_READONLY_KEYS = ("gate", "manual", "critical", "risk", "base_risk", "acceptance_lines")
ROUND_KINDS = ("build", "fix", "takeover", "rework")
# A new round must not inherit the previous round's terminal row. File mode never wrote
# status on `task round` (the row stays whatever it was); the store path maps each kind
# onto the status the board already uses for that kind, in the same commit as the bump.
ROUND_STATUS = {"build": "planned", "fix": "fix", "takeover": "building", "rework": "rework"}
# A terminal status is the one thing a new round must never inherit; a non-terminal one stays
# (demoting building -> planned would skew every later transition's "from" against the file path).
ROUND_TERMINAL = ("done", "verifying", "review", "merged", "blocked")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RECENT, BOARD_EVENTS = 10, 15
DEP_MET = ("merged", "qa", "done")
DEP_GATED = ("dispatched", "building")
SIDE = ("rework", "blocked", "provider", "quota", "stalled")  # roll-up precedence: the first present wins
# F.0: a run closes (`run set --status done`) only when nothing is open and every runnable plan
# gate passes. Closed = merged/qa/done (a task that reached the integration branch or was waived);
# every other status is open. A plan gate is a plain string (manual: never run, never blocks) or a
# table {cmd, expect = 0, timeout = 600} run in the repo's integration checkout.
CLOSED_STATUSES = ("merged", "qa", "done")
GATE_LOAD_MAX = 40.0        # 1-minute load average above this defers a gate: it blocks, never passes
GATE_TIMEOUT_DEFAULT = 600
GATE_OUTPUT_LINES = 20      # output lines a gate failure quotes
GATE_TAIL_BYTES = 4000      # bytes of a gate's output kept as its evidence tail
# `gate run` writes one evidence row per gate run; its result is one of these (the evidence table
# has no CHECK, so this tuple is the enum). pass -> green, fail exit=N -> red, invalid -> error.
EVIDENCE_RESULTS = ("green", "red", "timeout", "deferred", "error")
GATE_DUE_TASK_STATUSES = ("verifying", "review")  # --due: tasks in these statuses have their gates due

# The project store is opt-in. With the project store absent, nothing below runs.
# Z3: the root projection's slug is the project's OWN slug, resolved from the data dir through
# the root linkage (mkpaths.project_slug_of). This constant is only the fixture/fallback slug a
# self-test uses when a temp data dir has no root row; production never relies on it.
STORE_PROJECT_FALLBACK = "matrixkoder"
_DERIVED_SLUG = re.compile(r"^p[0-9a-f]{16}$")  # mk-bus/mk-state's own derivation (no root row yet)
STORE_RUN_DOC = "run-doc"      # artifact kind holding the run.json document (legacy fallback)
VIEW_CRASH = "MK_VIEW_CRASH"   # test seam: raise after the store commit, before the view rewrite
ROOT_CRASH = "MK_ROOT_CRASH"   # test seam: project commit lands, the root card upsert is skipped
# fields the tasks table has no column for; they live in the project DB's tasks.extra_json
# column (L4, C4: a new column of the existing project schema, never a duplicated field)
TASK_FILE_KEYS = ("wave", "ready", "workspace", "last_commit", "gates", "rounds", "created",
                  "plan_phase", "plan_task", "route", "lane_group", "reviewer", "refs", "deps",
                  # F.2 plan contract, copied from `mk-plan parse` and read-only after that (task set
                  # refuses them). `gate` is a list of {cmd, expect, timeout, lines} tables, the shape
                  # _gate_run already runs; `manual` is a line nobody runs. `base_risk` is the plan's
                  # `risk` under the name mkdb._task_risk reads (derived risk = base + open reasons).
                  "gate", "manual", "critical", "base_risk", "acceptance_lines")
# run-level fields the runs table has no column for; they live in the runs.extra_json column.
# `plan` (the file path) and `plan_id` ride here, not the runs.plan column: that column holds
# the plan id for a plan-tool run and the bare path for a legacy one, so it cannot carry both. `task_order` is the task-add order (the rows store parent-before-child, which is not that order).
RUN_FILE_KEYS = ("feature", "waiting", "recent", "plan_sha", "plan_status", "updated",
                 "plan_id", "plan", "fidelity", "final_gates")  # F.2: set only for a plan with the contract


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


# C1: every MK path (home rule, data-dir order, runs/plans folders) lives in scripts/mkpaths.py
# and nowhere else. mkpaths imports nothing from the repo, so there is no cycle.
mkpaths = _load_sibling("mkpaths")


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
    """One MK home rule (C16): $MK_HOME and $MATRIXKODER_HOME are the same setting; when both
    are set they must agree. The rule itself lives in scripts/mkpaths.py (C1) — this wrapper
    only turns its RuntimeError into this script's Fail, so the CLI stays one JSON line, exit 2."""
    try:
        return mkpaths.home()
    except mkpaths.Fail as e:
        raise Fail(str(e))


def _default_home():
    """True when neither $MK_HOME nor $MATRIXKODER_HOME is set: home() then resolves the
    default ~/.matrixkoder. Z8: only the DEFAULT home is protected from a throwaway data dir;
    an explicit home (tests, mk-e2e-layout, mk-livetest) keeps registering as it always did."""
    return not (os.environ.get("MK_HOME") or os.environ.get("MATRIXKODER_HOME"))


def _under_system_temp(path):
    """True when path's realpath is the system temp dir or under it (tempfile.gettempdir(),
    /tmp, /private/tmp — the same spellings mkdb's MK_SELFTEST guard accepts). Z8: a data dir
    here is a throwaway (a one-off --data, a test fixture), not a project a user pointed at."""
    real = os.path.realpath(path)
    for root in (tempfile.gettempdir(), "/tmp", "/private/tmp"):
        base = os.path.realpath(root)
        if real == base or real.startswith(base + os.sep):
            return True
    return False


def repo_top():
    """The main checkout of the nearest git repo above cwd, or None when there is none
    (mkpaths.repo_top)."""
    return mkpaths.repo_top()


def resolve_data(arg):
    """--data > $MATRIXKODER_DATA > root projects table > <home>/projects.json > <main checkout>/.matrixkoder.
    The repo is the nearest .git above cwd (never $HOME itself); a linked worktree maps to its main checkout.
    Reading the root table checks the file exists first, so a resolve never creates <home>/mk.db.
    The order itself is mkpaths.data_dir (C1); this wrapper converts its Fail for the CLI."""
    try:
        return mkpaths.data_dir(arg)
    except mkpaths.Fail as e:
        raise Fail(str(e))


def register_project(slug, repo, data_dir, name=None):
    """Write the project to the root projects table. L10.7: <home>/projects.json is no longer written
    (the resolver still READS it as the last-resort fallback for an old home). The slug is checked
    (C11) before the root store is opened."""
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


def main_worktree(top):
    """A linked worktree (.git is a file + commondir) shares the main checkout's data dir
    (mkpaths.main_worktree). Kept as a name: mk-lane.py calls it."""
    return mkpaths.main_worktree(top)


def check_id(v, what):
    if not v or not ID_RE.fullmatch(v):
        raise Fail(f"invalid {what} {v!r} (letters, digits, . _ -; max 64)")
    return v


def csv(v):
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _json_obj(text):
    """A JSON object from an extra_json column, or {} for NULL/bad/non-object text."""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def extras_json(obj):
    """The extra_json column value for a dict: compact JSON, or None when empty (a stored NULL
    keeps the same shape as a pre-L4 row and lets COALESCE-keep fire on a mirror write)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True) if obj else None


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
        self.runs = mkpaths.runs_dir(data)
        # plan sync --apply: events for this run id are counted and dropped (ONE summary event
        # comes after); other runs' upkeep events still write
        self.sink_run, self.sunk = None, 0
        # L4: the project DB is the only source of truth, always on. A project with no mk.db is
        # created through mkdb.open_project on first use, never through a file import.
        self.store_on = True
        self.pending = None
        self.pending_objs = {}  # run id -> run dict, every run this command saved (flush order)
        self._mkdb = None
        self._src = {}          # (run, task, kind) -> next live source-key counter
        self._appended = {}     # run id -> events.jsonl lines appended during this command

    @contextlib.contextmanager
    def locked(self):
        # ponytail: one flock for the whole data dir; per-run locks only if writers ever contend
        # L10.7: a migrated project's lock lives in its C2 folder, never in the legacy data dir
        lock_dir = mkpaths.project_folder(self.data)
        os.makedirs(mkpaths.runs_dir(lock_dir), exist_ok=True)
        with open(mkpaths.runs_lock(lock_dir), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def run_dir(self, rid):
        return mkpaths.run_dir(self.data, rid)

    def task_dir(self, rid, tid):
        return mkpaths.task_dir(self.data, rid, tid)

    def index(self):
        """L4: the run list from the runs table (was runs/index.json)."""
        con = self.store_open()
        try:
            rows = con.execute("SELECT id, plan, status, started, ended FROM runs").fetchall()
        finally:
            con.close()
        out = []
        for r in rows:
            run_x, _ = self.extras_read(r["id"])
            # R2 HIGH 2: a legacy run keeps its plan path in runs.plan, never in plan_id; fall
            # back to the column so `run list` does not report plan None for those runs.
            out.append({"id": r["id"], "feature": run_x.get("feature"), "status": r["status"],
                        "started": r["started"], "ended": r["ended"],
                        "plan": run_x.get("plan_id") or r["plan"]})
        return sorted(out, key=lambda x: x.get("started") or "")

    def load(self, rid=None, task=None):
        if rid is None:
            active = [r["id"] for r in self.index() if r.get("status") == "active"]

            def owns(r):  # a corrupt sibling must not crash the pick
                try:
                    src = self.load_store(r)
                except Fail:
                    return False
                return any(t.get("id") == task for t in src.get("tasks") or [] if isinstance(t, dict))
            if len(active) > 1 and task:  # several active runs: the one that owns the task wins
                owners = [r for r in active if owns(r)]
                active = owners if len(owners) == 1 else active
            if len(active) != 1:
                raise Fail(f"{len(active)} active runs {active}; pass --run/--id")
            rid = active[0]
        rid = check_id(rid, "run id")
        return self.load_store(rid)

    def save(self, run, task=None, by="orchestrator"):
        rollup(self, run, by)
        self.refresh_ready(run, by)
        run["updated"] = now()
        # every run this command saves is committed at flush, in the order saved (the file path
        # wrote each run.json as it went). refresh_others saves sibling runs too; a single pending
        # slot lost the triggering run's own commit.
        self.pending_objs[run["id"]] = run
        self.pending = lambda run=run: run  # resolved at flush, after later edits to this dict

    def dep_status(self, run, dep):
        """Status of a dep ("T" in this run, "RUN:T" in another run of this store), None if unknown.
        Run and task ids match case-insensitively (APFS); a same-run RUN:T reads the in-memory run.
        L4: a cross-run dep resolves from the tasks table, never runs/<other>/run.json. The run this
        command is saving (self.pending) is read in memory: refresh_others runs before the commit,
        exactly where the file path had already written that run's run.json."""
        rid, _, tid = dep.rpartition(":")
        src = None
        if rid.lower() in ("", run["id"].lower()):
            src = run
        else:
            pend = self.pending() if self.pending else None
            if pend and str(pend.get("id") or "").lower() == rid.lower():
                src = pend
        if src is not None:
            return next((t.get("status") for t in src.get("tasks") or []
                         if isinstance(t, dict) and str(t.get("id") or "").lower() == tid.lower()), None)
        con = self.store_open()
        try:
            row = con.execute("SELECT status FROM tasks WHERE lower(run)=lower(?) AND lower(id)=lower(?)",
                              (rid, tid)).fetchone()
        except Exception:
            return None
        finally:
            con.close()
        return row["status"] if row else None

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
            hit = next((t.get("id") for t in (src.get("tasks") or [])
                        if isinstance(t, dict) and str(t.get("id") or "").lower() == did.lower()), None)
        else:
            con = self.store_open()
            try:
                row = con.execute("SELECT id FROM tasks WHERE run=? AND lower(id)=lower(?)",
                                  (rid, did)).fetchone()
            except Exception:
                row = None
            finally:
                con.close()
            hit = row["id"] if row else None
        return (rid + ":" if rid else "") + (hit or did)

    def active_runs(self, run):
        """-> ([run, *other active runs], warnings): the given run first, the other active runs
        loaded from the store. An unreadable or wrong-shape sibling is skipped and named in
        warnings, never fatal. L4: no runs/<id>/run.json is read."""
        others, warnings = [], []
        for r in self.index():
            rid = r.get("id")
            if r.get("status") != "active" or rid in (None, run.get("id")):
                continue
            try:
                x = self.load_store(rid)
            except (Fail, OSError, ValueError) as e:
                warnings.append(f"active run {rid} unreadable, skipped: {type(e).__name__}")
                continue
            missing = [k for k in ("branch", "feature") if not x.get(k)]
            if missing:
                warnings.append(f"active run {rid} missing {', '.join(missing)}, skipped")
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
        # L4: no file view. The store commit writes the line; remember it so a crash before the
        # publish is repaired.
        self._appended.setdefault(run["id"], []).append(line)
        run["recent"] = (run.get("recent", []) + [ev])[-RECENT:]
        return ev

    def events(self, rid, n):
        """L4: the run's state events from the events table (was events.jsonl), in store order."""
        con = self.store_open()
        try:
            rows = con.execute(
                "SELECT id, run, task, t, chan, kind, from_id, to_id, text, meta_json"
                " FROM events WHERE run=? AND chan='state' ORDER BY id", (rid,)).fetchall()
        except Exception:
            return []
        finally:
            con.close()
        out = []
        for row in rows:  # a live row keeps its original bytes; a direct transition is rendered
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(self.event_line(row)))
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
        # L4: render returns the board text; it never writes board.md (cmd_render prints it).
        return "\n".join(out) + "\n"

    # ---------- project store (the only store; the project DB is the only truth, C4) ----------

    def db(self):
        if self._mkdb is None:
            self._mkdb = _mkdb()
        return self._mkdb

    def home_dir(self):
        """The REAL MK home (mkpaths.home(): --home > $MK_HOME > $MATRIXKODER_HOME > ~/.matrixkoder).

        Z3: the root projection (cards, sync, plan_summary) must land in the live root store
        (<home>/mk.db) so the Plan tab and `k` see it, not in a per-data-dir fake home. This
        creates NOTHING: mkdb.open_root makes <home>/mk.db when a root write needs it, and a
        self-test pins $MK_HOME/$MATRIXKODER_HOME to a temp dir (the mkdb MK_SELFTEST guard
        refuses a write under the real ~/.matrixkoder)."""
        return home()

    def project_slug(self):
        """This project's OWN slug for the root projection (its own row's slug, from the data dir).

        Read-only: never registers. A write path calls root_register() for that. A self-test pins
        MK_SELFTEST_FALLBACK_SLUG so its temp data dirs keep the fixture slug they assert on (and
        add no root row); production with no root row yet derives one exactly as mk-bus does. The
        project store itself resolves by data_dir (self.project_folder()), so this slug only keys
        the root projection (cards, sync, plan_summary).

        A root row may exist under a DERIVED alias (`p` + sha256, what mk-bus/mk-state create when
        the project has no row yet) as well as the project's canonical slug (registered by the
        wizard / mk-migrate / mk-state.register_project). The canonical one is the project's OWN
        slug — the Plan tab lists it — so it wins when both name this data dir."""
        slug = mkpaths.project_slug_of(self.data)
        if slug and not _DERIVED_SLUG.fullmatch(slug):
            return slug
        canonical = self._canonical_slug()
        if canonical:
            return canonical
        if slug:
            return slug  # a derived alias that exists now; root_register derives the same one
        return os.environ.get("MK_SELFTEST_FALLBACK_SLUG") or \
            "p" + hashlib.sha256(os.path.realpath(self.data).encode("utf-8")).hexdigest()[:16]

    def _canonical_slug(self):
        """The non-derived slug of a root row naming this data dir (its data_dir or db_path folder),
        or None. Read-only: mkdb.projects never creates the root store. A derived alias
        (`p`+sha256, mk-bus's own derivation) is skipped, so the project's canonical slug wins."""
        try:
            rows = self.db().projects(self.home_dir())
        except Exception:
            return None
        want = os.path.realpath(self.data)
        for r in rows:
            slug = r.get("slug")
            if not isinstance(slug, str) or _DERIVED_SLUG.fullmatch(slug):
                continue
            for raw in (r.get("data_dir"), r.get("db_path")):
                if not isinstance(raw, str) or not raw.strip():
                    continue
                p = os.path.realpath(os.path.expanduser(raw.strip()))
                if os.path.dirname(p) == want or p == want:
                    return slug
        return None

    def _skip_root_projection(self):
        """Z8: True when this data dir is a throwaway under the system temp dir AND the home is
        the default one. root_register then inserts no root row and root_write projects nothing,
        so a one-off `mk-state.py --data /tmp/x ...` cannot add a junk row to the user's real
        ~/.matrixkoder. An explicit MK_HOME/MATRIXKODER_HOME (tests, mk-e2e-layout, mk-livetest)
        or a data dir outside temp keeps registering exactly as before."""
        return _under_system_temp(self.data) and _default_home()

    def root_register(self):
        """Give this project its root row (the C3 linkage) if it has none, then return the slug.

        The root projection (and the Plan tab that reads it) hangs off a root `projects` row keyed
        by the slug. A project with no row yet — a brand-new project — is registered here, the same
        INSERT OR IGNORE mkpaths._register_project / mk-bus / mk-migrate use (same columns, same
        derivation). An existing row is left alone, so a migrated row's db_path and repo survive.
        A self-test fixture (MK_SELFTEST_FALLBACK_SLUG) is skipped: it needs no row and must add
        none. Z8: a throwaway temp data dir on the DEFAULT home registers nothing either — the
        slug is returned, but the user's real root store gains no row. Called from the write path
        only, never from a read."""
        slug = self.project_slug()
        if os.environ.get("MK_SELFTEST_FALLBACK_SLUG") or mkpaths.project_slug_of(self.data):
            return slug
        if self._skip_root_projection():
            return slug
        if self._canonical_slug():
            return self._canonical_slug()
        try:
            con = self.db().open_root(self.home_dir())
            try:
                con.execute("INSERT OR IGNORE INTO projects(slug, name, repo, data_dir, mode, db_path)"
                            " VALUES (?,?,?,?,?,NULL)",
                            (slug, slug, os.path.dirname(os.path.abspath(self.data)),
                             os.path.abspath(self.data), "home"))
                con.commit()
            finally:
                con.close()
        except self.db().Fail:
            raise  # a real refusal (a synced/unwritable home) must still fail the command
        except Exception:
            # a missing/unreadable/corrupt root store: register nothing. The project store is the
            # truth; the root projection is a derived rollup and must never abort a project write.
            pass
        return slug

    def root_write(self, fn, *a, **kw):
        """Run a ROOT-store projection write (a card upsert, catch_up, promote_ready, transition).

        Z3: the root projection is a derived rollup (the project store is the truth). A
        missing/unreadable/corrupt root store — or a corrupt ~/.matrixkoder/mk.db — must never
        abort a project write; the card is simply not written and `sync` heals it later. A real
        refusal still propagates: mkdb.Fail (a synced/unwritable home, a newer schema) names a
        condition the caller must see, so it is re-raised. Z8: nothing is projected for a
        throwaway temp data dir on the default home (see _skip_root_projection)."""
        if self._skip_root_projection():
            return None
        try:
            return fn(*a, **kw)
        except self.db().Fail:
            raise
        except Exception:
            return None

    def project_folder(self):
        """The folder to pass as `data_dir` (L3B): mkpaths.project_folder(self.data) — the C2
        project folder for a migrated project (a root `projects.db_path` set), else --data itself.

        After `mk-migrate --switch` the repo data dir is the project's LEGACY location; passing it
        as `data_dir` would open the old `<data>/mk.db` while the dashboard, mkdb and mk-plan read
        the new one — a silent split. Every project-store open and write goes through the folder
        this returns. Pre-migration it is `--data` exactly, so the store file, the fake home and
        the bytes are unchanged."""
        return mkpaths.project_folder(self.data)

    def store_open(self):
        con = self.db().open_project(self.home_dir(), self.project_slug(), data_dir=self.project_folder())
        return con

    def _run_doc_extras(self, rid):
        """(run_x, task_x) from the legacy run-doc artifact, or ({}, {}) when there is none.
        The pre-L4 migration route: the fields that used to ride the run.json document are read
        once so L4's first write migrates them into runs/tasks.extra_json instead of blanking them."""
        try:
            con = self.store_open()
        except Exception:
            return {}, {}
        try:
            doc = con.execute(
                "SELECT text FROM artifacts WHERE run=? AND kind=? AND task IS NULL"
                " ORDER BY id DESC LIMIT 1", (rid, STORE_RUN_DOC)).fetchone()
        except Exception:
            return {}, {}
        finally:
            con.close()
        if not doc:
            return {}, {}
        try:
            base = json.loads(doc["text"])
        except ValueError:
            return {}, {}
        if not isinstance(base, dict):
            return {}, {}
        run_x = {k: base[k] for k in RUN_FILE_KEYS if k in base}
        run_x["task_order"] = [t.get("id") for t in base.get("tasks") or []
                               if isinstance(t, dict) and t.get("id")]
        task_x = {t["id"]: {k: t[k] for k in TASK_FILE_KEYS if k in t}
                  for t in base.get("tasks") or [] if isinstance(t, dict) and t.get("id")}
        return run_x, task_x

    def extras_read(self, rid):
        """-> ({extras...}, {task_id: {extras...}}) for one run from runs.extra_json and the
        tasks' extra_json columns (L4, C4: a new column, never a duplicated field). A NULL column
        is an empty dict. A pre-L4 store (both NULL) falls back to the legacy run-doc artifact
        ONCE, so the first post-L4 write migrates the extras instead of blanking them."""
        con = self.store_open()
        try:
            rrow = con.execute("SELECT extra_json FROM runs WHERE id=?", (rid,)).fetchone()
            trows = con.execute("SELECT id, extra_json FROM tasks WHERE run=?", (rid,)).fetchall()
        finally:
            con.close()
        run_x = _json_obj(rrow["extra_json"] if rrow else None)
        task_x = {}
        for r in trows:
            data = _json_obj(r["extra_json"])
            if data:
                task_x[r["id"]] = data
        if not run_x and not task_x:
            run_x, task_x = self._run_doc_extras(rid)
        return run_x, task_x

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
        # D1.5/CLI: an unknown dep id is kept in the run (warned, never fatal — it may be added
        # later). import_history refuses such a dep, so the COLUMN carries only the deps that
        # resolve now; the full list rides tasks.extra_json and load_store restores it.
        in_run = {x.get("id") for x in run.get("tasks") or []}
        deps_col = [d for d in deps if d in in_run or self.dep_status(run, d) is not None]
        # the file view shows readiness as a flag on a planned task; the store shows it as the
        # status. Mapping the flag back stops the next import from demoting a ready task.
        status = t.get("status") or "planned"
        # a status mk-state never wrote (a hand-edited row) is not a column status: import_history
        # refuses it, so the mirror keeps the run readable by storing it as planned.
        if status not in STATUSES:
            status = "planned"
        # only a planned task becomes the kanban status `ready`. A routed task keeps `routed`
        # and the ready flag: mapping it to `ready` made the next load publish it as `planned`.
        if status == "planned" and t.get("ready"):
            status = "ready"
        return {"id": t["id"], "parent": t.get("parent"),
                "type": t.get("type") or task_type_of(run, t),
                "title": t.get("title") or "", "acceptance": t.get("acceptance"),
                "owned": ",".join(owned) if isinstance(owned, list) else (owned or ""),
                "deps": ",".join(deps_col) if isinstance(deps_col, list) else (deps_col or ""),
                "parallel": 1 if t.get("parallel") else 0,
                "route_json": _route_json(t),
                "status": status, "lane": t.get("lane"), "slot": t.get("slot"),
                "branch": t.get("branch"), "pane": t.get("pane"), "round": t.get("round") or 0,
                "verdict": t.get("verdict"), "updated": t.get("updated"),
                "extra_json": extras_json(self.task_extras(t))}

    def run_row(self, run):
        # runs.plan is the plan id (C5); the file path stays in runs.extra_json. Writing the path
        # here left a run started from plans/archive/<id>-v<n>.md unlinked from its plan.
        return {"id": run["id"], "plan": run.get("plan_id") or run.get("plan"), "plan_version": None,
                "branch": run.get("branch"), "status": run.get("status"),
                "stage": str(run.get("stage")) if run.get("stage") is not None else None,
                "next": run.get("next"), "started": run.get("started"), "ended": run.get("ended"),
                "extra_json": extras_json(self.run_extras(run))}

    def _round_artifacts(self, run, t):
        """brief/report/review of the task's current round — ingest list for import_history.

        The store, when it has the artifact, IS the source (C4): such a kind yields NOTHING
        here (re-importing a stored row under a synthetic src wrote a duplicate row per rev —
        R2 FIX HIGH). A kind with NO artifact row falls back to the task folder's round file:
        hand-written briefs (the orchestrator's flow today) and pre-X3 report/review files are
        ingested once, keyed on the file's digest (src as before X3), so a repeat import adds
        nothing. Returns one dict per ingested kind; [] when nothing to import."""
        if not self.store_on:
            return []
        n = t.get("round") or 0
        d = self.task_dir(run["id"], t["id"])
        con = self.store_open()
        try:
            have = {r["kind"]: r["rev"] for r in con.execute(
                "SELECT kind, MAX(rev) rev FROM artifacts WHERE run=? AND task=? AND round=?"
                " AND kind IN ('brief','report','review') GROUP BY kind",
                (run["id"], t["id"], n)).fetchall()}
        finally:
            con.close()
        arts = []
        for kind in ("brief", "report", "review"):
            if kind in have:
                continue  # the artifact is the source; re-importing it would duplicate the row
            fp = os.path.join(d, f"{kind}-r{n}.md")
            if not os.path.exists(fp):
                continue
            text = open(fp, encoding="utf-8").read()
            digest = hashlib.sha256(text.encode()).hexdigest()[:16]
            arts.append({"task": t["id"], "round": n, "kind": kind, "rev": 1, "text": text,
                         "created": t.get("updated") or now(),
                         "src": f"file:{run['id']}:{t['id']}:{kind}:{n}:{digest}"})
        return arts

    def doc_artifact(self, rid, task, kind, text, created):
        # src includes the text: import_history skips a src it already has, so a changed
        # document is a new row and the newest one (highest id) is the view
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        return {"task": task, "round": 0, "kind": kind, "rev": 1, "text": text,
                "created": created, "src": f"doc:{rid}:{task or ''}:{kind}:{digest}"}


    def store_round_files(self, run, t):
        """Store the current round's files now (task round, after the views exist). X3.1: when
        the artifact already holds the round's text, the view files are not re-read at all."""
        if not self.store_on:
            return []
        arts = self._round_artifacts(run, t)
        if arts:
            self.db().import_history(self.home_dir(), self.project_slug(), run["id"], artifacts=arts,
                                     data_dir=self.project_folder(),
                                     sync_root=not self._skip_root_projection())
        return [a["kind"] for a in arts]

    def flush(self):
        """Commit every run this command saved, in the order saved. A command that saves before it
        is finished (run init adds its tasks after the first save) therefore stores the final run.
        L4: there are no file views to publish — the store is the only truth, so publish/repair
        are gone."""
        runs = list(self.pending_objs.values())
        self.pending_objs = {}
        self.pending = None
        if not runs:
            return
        for run in runs:
            self.store_commit(run)
        # the store commit is done; this test seam raises here so a command can be interrupted
        # between the commit and whatever the caller does next.
        if os.environ.get(VIEW_CRASH) == "1":
            raise Fail("injected crash between the store commit and the view rewrite")

    def run_extras(self, run):
        """The run-level fields the runs table has no column for (RUN_FILE_KEYS), plus the task
        order, as a dict."""
        out = {k: run[k] for k in RUN_FILE_KEYS if k in run}
        out["task_order"] = [t["id"] for t in run.get("tasks") or [] if t.get("id")]
        return out

    def task_extras(self, t):
        """The task-level fields the tasks table has no column for (TASK_FILE_KEYS), as a dict."""
        return {k: t[k] for k in TASK_FILE_KEYS if k in t}

    def store_commit(self, run, task=None):
        """Mirror the result into the store. Status changes go ONLY through mkdb.transition() (one
        store event, one change_log row). Other events go through import_history. The run/task
        fields the tables have no column for ride runs.extra_json / tasks.extra_json (L4, C4)."""
        rid = run["id"]
        # Z3: a project with no root row yet gets one here (write path only), so its root
        # projection (cards, sync, plan_summary) is keyed by its own slug and the Plan tab sees it.
        self.root_register()
        lines = self._appended.pop(rid, [])
        events = []
        for line in lines:
            ev = json.loads(line.decode())
            events.append({"t": ev["t"], "chan": "state", "kind": ev["type"], "task": ev.get("task"),
                           "from_id": ev.get("from"), "to_id": ev.get("to"), "text": ev.get("text"),
                           "meta_json": json.dumps({"by": ev.get("by"), "line": line.decode()},
                                                   sort_keys=True, ensure_ascii=False),
                           "src": self.next_src(rid, ev.get("task"), ev["type"])})
        tasks = [self.task_row(run, t) for t in run.get("tasks") or [] if t.get("id")]
        arts = []
        for t in run.get("tasks") or []:
            if t.get("id") and t.get("round"):
                arts += self._round_artifacts(run, t)
        self.db().import_history(self.home_dir(), self.project_slug(), rid, run_row=self.run_row(run),
                                 tasks=tasks, events=events, artifacts=arts, data_dir=self.project_folder(),
                                 sync_root=not self._skip_root_projection())
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
                self.root_write(self.db().transition, self.home_dir(), self.project_slug(), rid, tid, to,
                                note=f"round {task.get('round')} ({kind}) reset",
                                round=task.get("round") or 0, sync_root=True,
                                data_dir=self.project_folder(), verdict=None)
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
            self.root_write(self.db().transition, self.home_dir(), self.project_slug(), rid,
                            ev["task"], ev["to_id"],
                            note=ev.get("text") or "", round=task.get("round") or 0,
                            sync_root=True, data_dir=self.project_folder())
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
        if run.get("fidelity") and os.environ.get(ROOT_CRASH) != "1":
            # F.3: mkdb.transition() rolls a parent up in the store on its own. A parent that
            # rollup() held un-closed (own gate without fresh evidence) is put back here.
            con = self.store_open()
            try:
                stored = {r["id"]: (r["status"] or "").lower() for r in
                          con.execute("SELECT id, status FROM tasks WHERE run=?", (rid,))}
            finally:
                con.close()
            for t, _ in reversed(tree(run.get("tasks") or [])):  # children first
                if t.get("id") and stored.get(t["id"]) in CLOSED_STATUSES and t.get("status") not in CLOSED_STATUSES:
                    self.root_write(self.db().transition, self.home_dir(), self.project_slug(), rid,
                                    t["id"], t["status"], note="held: own gate has no fresh green evidence",
                                    round=t.get("round") or 0, sync_root=True, data_dir=self.project_folder())
        if os.environ.get(ROOT_CRASH) != "1":
            # catch_up repairs a card whose status disagrees with the task row, including one
            # whose change_log sits below the watermark the transition above just advanced.
            # promote_ready then moves dependents and writes their cards in one root transaction.
            self.root_write(self.db().catch_up, self.home_dir(), self.project_slug(),
                            data_dir=self.project_folder())
            self.root_write(self.db().promote_ready, self.home_dir(), self.project_slug(), rid,
                            data_dir=self.project_folder(), sync_root=True)

    def store_transition(self, rid, tid, to, note, line=None):
        """One status change through mkdb.transition, root card included (C5)."""
        self.root_write(self.db().transition, self.home_dir(), self.project_slug(), rid, tid, to,
                        note=note, sync_root=os.environ.get(ROOT_CRASH) != "1",
                        data_dir=self.project_folder())

    def load_store(self, rid):
        """The store is the read source: runs and tasks tables (runs.extra_json / tasks.extra_json
        carry the fields with no column; C4). A status another writer set through mkdb.transition()
        wins over the stored extras."""
        con = self.store_open()
        try:
            row = con.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            if not row:
                raise Fail(f"no run {rid!r} in the store")
            tasks = con.execute("SELECT * FROM tasks WHERE run=? ORDER BY rowid", (rid,)).fetchall()
        finally:
            con.close()
        run_x = _json_obj(row["extra_json"])
        task_x = {}
        for t in tasks:
            data = _json_obj(t["extra_json"])
            if data:
                task_x[t["id"]] = data
        if not run_x and not task_x:
            # L4 migration route: a pre-L4 store kept these extras only in the run-doc artifact.
            # Read it once (read-only) so the first post-L4 write does not blank them; the next
            # write repopulates the columns and this fallback stops firing.
            run_x, task_x = self._run_doc_extras(rid)
        run = dict(run_x)
        # A pre-L4 store has NULL extra_json: the extras are absent, never fatal. runs.plan
        # carries the plan id for a plan-tool run (C5) or the bare path for a legacy one; the
        # extra_json separates them (plan_id + the file path).
        run.update({"id": row["id"], "branch": row["branch"],
                    "status": row["status"] or "active",
                    "stage": int(row["stage"]) if str(row["stage"] or "").isdigit() else (run_x.get("stage") or 0),
                    "next": row["next"], "started": row["started"], "ended": row["ended"]})
        if "plan" not in run_x:
            run["plan"] = row["plan"]  # legacy/hand-written row: the column is the only path we have
        if "plan_id" not in run_x and row["plan"]:
            # an extra_json-less row: runs.plan is the plan id only when it is not a file path
            run["plan_id"] = None if ("/" in row["plan"] or row["plan"].endswith(".md")) else row["plan"]
        # Task order is not a column: the run row's `task_order` list is the order the tasks were
        # added (the runs table stores parent-before-child, which is not that order).
        rows_by_id = {t["id"]: t for t in tasks}
        order = [i for i in (run_x.get("task_order") or []) if i in rows_by_id]
        seen = set(order)
        order += [t["id"] for t in tasks if t["id"] not in seen]  # a task added after the last commit
        out = []
        for tid in order:
            t = rows_by_id[tid]
            extras = dict(task_x.get(t["id"]) or {})
            extra = dict(extras)
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
            # D1.5.2: the store is the read source, so a truthy acceptance column overlays the
            # file view here; an empty/absent one (legacy tasks carry acceptance="") adds no key,
            # which keeps the first publish after `store enable` byte-identical to the imported
            # run.json. `type` is NOT overlaid for the same reason (it is derived per row, so a
            # legacy task would gain a key it never had); `task check` and the mirror derive it
            # from the tree instead (task_type_of).
            extra.update({
                "id": t["id"], "title": t["title"] or "", "parent": t["parent"],
                "type": t["type"] or task_type_of(run, {"id": t["id"], "parent": t["parent"]}),
                "owned": owned, "status": status,
                "lane": t["lane"], "slot": t["slot"], "branch": t["branch"], "pane": t["pane"],
                "round": t["round"] or 0, "verdict": t["verdict"], "updated": t["updated"],
                # R2 HIGH 1: every column task_row writes must be read back, or a load+save
                # resets it. parallel is a column, not an extra: bool(...) restores the flag.
                "parallel": bool(t["parallel"]),
                "ready": ready,
                "agent": route.get("agent"), "model": route.get("model"), "effort": route.get("effort"),
            })
            # the full dep list rides tasks.extra_json (an unknown dep cannot sit in the column);
            # a pre-L4 store with no extra_json falls back to the column's list.
            if "deps" not in extras:
                extra["deps"] = deps
            if t["acceptance"]:
                extra["acceptance"] = t["acceptance"]
            if route.get("route"):
                extra["route"] = route["route"]
            out.append(extra)
        run["tasks"] = out
        run.pop("task_order", None)
        run.setdefault("schema", SCHEMA)
        run.setdefault("feature", run_x.get("feature"))
        run.setdefault("waiting", run_x.get("waiting"))
        run.setdefault("recent", run_x.get("recent") or [])
        run.setdefault("plan_id", run_x.get("plan_id"))
        run.setdefault("plan_sha", run_x.get("plan_sha"))
        run.setdefault("plan_status", run_x.get("plan_status"))
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

    def repair(self):
        """L4: nothing to repair. Views no longer exist, so there is no lag to converge; the store
        is always current. Kept as the name the read commands call."""
        return

    def enable_store(self):
        """L4: there is no file mode to opt into — the store is always on, created through
        mkdb.open_project on first use. Kept as a command for callers: reports store on."""
        return {"ok": True, "store": "on", "imported": None}

    def export_legacy(self, rid):
        """L4: run.json / index.json / board.md / events.jsonl are no longer written, so there is
        nothing to export. Kept as a command for callers; the store is the only record."""
        if not self.load_store(rid):
            raise Fail(f"store has no run {rid}")
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
    child past `routed` and the least advanced child below `building`.
    `st._rollup_keep` names the ids this save sets on purpose (a re-opened follow-up phase): their
    rollback is the point of the command, so roll-up does not read their children over it."""
    order = {s: i for i, s in enumerate(STATUSES)}
    for t, _ in reversed(tree(run.get("tasks") or [])):  # leaves first, so nested parents see rolled-up children
        if not t.get("id") or t["id"] in (getattr(st, "_rollup_keep", None) or ()):
            continue
        kids = [c.get("status") for c in run["tasks"] if c.get("parent") == t["id"]]
        if not kids:
            continue
        new = next((s for s in SIDE if s in kids), None)
        if new is None:
            new = min(kids, key=lambda s: order.get(s, 0))  # unknown status: least advanced
            if any(order.get(k, 0) > order["routed"] for k in kids) and order.get(new, 0) < order["building"]:
                new = "building"
        if (new in CLOSED_STATUSES and new != t.get("status") and run.get("fidelity")
                and (t.get("gate") or t.get("manual")) and fidelity_missing(st, run, [t["id"]])):
            # F.3: a parent with its own gate/manual line never rolls up to a closed status unchecked
            new = t["status"] if t.get("status") not in CLOSED_STATUSES else "review"
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


def short_val(v, n=80):
    """One event line's worth of a task field: JSON for lists/None, cut at n chars (T1)."""
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n - 1] + "…"


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
    return mkpaths.lock_path(check_id(res, "lock resource").lower())


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
            con = st.store_open()
            try:
                exists = con.execute("SELECT 1 FROM runs WHERE id=?", (rid,)).fetchone()
            finally:
                con.close()
            if exists:
                raise Fail(f"run {rid} exists")
            cur = None
            if a.plan:  # the plan learns the run before the run learns its tasks (C5)
                plan_tool("set", a.plan, "--status", "running")
                plan_tool("add-run", a.plan, "--run", rid)
                cur = plan_tool("parse", a.plan)  # sha AFTER set/add-run: no board ⚠ at birth
            os.makedirs(mkpaths.tasks_dir(st.data, rid), exist_ok=True)
            run = {"schema": SCHEMA, "id": rid, "feature": a.feature, "branch": a.branch, "plan": a.plan,
                   "plan_id": cur.get("plan") if cur else None, "plan_sha": cur.get("sha") if cur else None,
                   "plan_status": cur.get("status") if cur else None,
                   "status": "active", "stage": 0, "next": None, "waiting": None,
                   "started": now(), "ended": None, "updated": None, "tasks": [], "recent": []}
            fid = plan_fidelity(cur) if cur else False
            if fid:  # F.2: the run carries the plan's gate contract; a pre-F plan sets nothing
                run["fidelity"], run["final_gates"] = True, list(cur.get("final_gates") or [])
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
                                plan_field_extra({"plan_phase": pid, "route": ph.get("route") or None,
                                                  "lane_group": ph.get("lane_group") or None,
                                                  "reviewer": ph.get("reviewer") or ""}, ph, "phase"))
                warnings += w
                for c in ph.get("tasks") or []:
                    _, w = add_task(st, run, str(c.get("id") or ""), c.get("title") or c.get("id"),
                                    ph.get("wave"), c.get("deps") or [], c.get("owned") or [], pid, a.by,
                                    plan_field_extra({"plan_task": c.get("id")}, c, "task", fid))
                    warnings += w
                    for s in c.get("subtasks") or []:
                        _, w = add_task(st, run, str(s.get("id") or ""), s.get("title") or s.get("id"),
                                        ph.get("wave"), s.get("deps") or [], s.get("owned") or [],
                                        str(c.get("id") or ""), a.by,
                                        plan_field_extra({}, s, "subtask", fid))
                        warnings += w
            st.flush()
        return {"ok": True, "run": run, **({"warnings": warnings} if warnings else {})}
    if a.action == "backfill-fields":
        with st.locked():
            st.repair()
            run = st.load(a.run)
            if not (run.get("plan_id") and run.get("plan")):
                raise Fail(f"run {run['id']} has no plan link (run init --plan)")
            return backfill_fields(st, run, a.by)
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
                # F.0: the open-work and plan-gate checks run BEFORE anything is written and
                # --force does not bypass them (no bypass in F.0). A refusal leaves the store
                # untouched.
                checks = close_checks(st, run) if a.status == "done" else {}
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
            reconcile_open(st, run)  # first: a failing reconcile leaves red evidence the gate would quote instead
            fidelity_gate(st, run, [t["id"] for t in run.get("tasks") or []
                                    if t.get("id") and t.get("status") in CLOSED_STATUSES], final=True)
            if st.store_on:
                archive_backup(st, run)  # before the status commit: a failed backup leaves the run active
            set_run_status(st, run, "archived")
        st.save(run)
        st.flush()
    out = {"ok": True, "run": run, **({"warning": warn} if warn else {})}
    if a.action == "set" and a.status == "done":
        if run["status"] == "done":
            out["bus_archive"] = archive_bus(st, run)
        out.update(checks)
        out.update(final_report(st, run))  # F.6: the closed run prints its final report
        out["promoted"] = promoted
        if perr:
            out["promote_error"] = perr
    return out



def archive_bus(st, run):
    """L10.7: a run that just closed archives its bus rows to <plan folder>/runs/<run>/bus.jsonl
    (`mk-bus.py archive-run`: count-checked, rows deleted only after the file verifies). The status is
    already committed here, so a failure is LOUD (exit 2, naming the retry) and never swallowed.
    A run with no plan has no folder to hold the file: skipped, and the reply says so."""
    if not (run.get("plan_id") or run.get("plan")):
        return {"skipped": "run has no plan: no folder for bus.jsonl"}
    try:
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mk-bus.py"),
                            "archive-run", "--run", run["id"], "--data", st.data],
                           capture_output=True, text=True, timeout=120)
        doc = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        doc, r = {"error": f"{type(e).__name__}: {e}"}, None
    if r is None or r.returncode != 0 or not doc.get("ok"):
        why = doc.get("error") or (r.stderr if r else "").strip()[-300:] or "no output"
        raise Fail(f"run {run['id']} is done but its bus archive failed: {why} — retry: "
                   f"mk-bus.py archive-run --run {run['id']} --data {st.data}")
    return {k: doc.get(k) for k in ("path", "messages", "acks", "artifacts", "written")}


def final_report(st, run):
    """F.6: `mk-report.py final` for a run that just closed -> {"report": text} (or report_error).
    The checkout and tree are the ones the close gates used, so the report agrees with them.
    Best-effort: the close already happened, so a report failure is a field, never an error."""
    where, _ = integration_checkout(st)
    try:
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mk-report.py"),
                            "final", "--run", run["id"], "--data", st.data, "--where", where],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"report_error": f"{type(e).__name__}: {e}"}
    if r.returncode in (0, 1) and r.stdout.strip():
        return {"report": r.stdout.rstrip("\n")}
    return {"report_error": (r.stdout or r.stderr).strip()[-300:] or f"exit {r.returncode}"}


def archive_backup(st, run):
    """VACUUM INTO a new file, check it holds the run, then rename it into place.
    An existing <run>.db is kept: the new backup is <run>-<UTC timestamp>.db. A failure removes
    the temp file and raises, before the run is marked archived."""
    dest_dir = os.path.join(mkpaths.project_folder(st.data), "archive")  # L10.7: never the legacy dir of a migrated project
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
    fidelity_gate(st, run, [t["id"] for t in run.get("tasks") or []
                            if t.get("id") and t.get("status") in ("merged", "qa")])
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
        n += 1
        yield t["id"]


def open_work(run):
    """F.0: [{"id", "status"}] for every task AND phase of the run that is not closed
    (closed = merged/qa/done). The run's own task order, so the refusal reads like the board."""
    return [{"id": t["id"], "status": t.get("status")}
            for t in run.get("tasks") or []
            if isinstance(t, dict) and t.get("id") and t.get("status") not in CLOSED_STATUSES]


def load_avg():
    """The 1-minute load average, the deferral input (F.0). $MK_STATE_LOADAVG injects one, the
    same kind of test seam as MK_PROMOTE_FAIL_AFTER: it pins the reader's VALUE, never the
    module under test. A platform without getloadavg never defers."""
    override = os.environ.get("MK_STATE_LOADAVG")
    if override is not None:
        try:
            return float(override)
        except ValueError:
            raise Fail(f"bad MK_STATE_LOADAVG {override!r} (a number)")
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def integration_checkout(st):
    """The checkout a run's gates run in (F.0, todo 140): the repo this run belongs to.
    Order: (1) the root projects row that names THIS project's data dir (data_dir, or the folder
    of db_path) AND whose `repo` is a checkout — the registered repo wins, so `--data X` from an
    unrelated cwd still gates X's own repo; (2) the nearest git repo above cwd (mkpaths.repo_top;
    a lane's linked worktree maps to its main checkout); (3) a row with this project's slug;
    (4) the project folder. -> (dir, note). An auto-registered row records dirname(data_dir),
    which is not necessarily a git checkout, so a row only counts when `repo` has a .git. Never
    raises. It never switches a branch — the orchestrator keeps this checkout on the plan's
    integration branch, which is why a deferred or failing gate blocks instead of being retried
    elsewhere."""
    folder = os.path.realpath(st.project_folder())
    rows, slug = [], None
    with contextlib.suppress(Exception):
        rows = st.db().projects(st.home_dir()) or []
    with contextlib.suppress(Exception):
        slug = st.project_slug()

    def registered(want):
        for r in rows:
            repo = r.get("repo")
            if not isinstance(repo, str) or not repo.strip():
                continue
            if want is not None:
                hit = any(isinstance(v, str) and v.strip()
                          and os.path.realpath(os.path.expanduser(
                              os.path.dirname(v.strip()) if k == "db_path" else v.strip())) == want
                          for k, v in (("data_dir", r.get("data_dir")), ("db_path", r.get("db_path"))))
                if not hit:
                    continue
            elif not slug or r.get("slug") != slug:
                continue
            d = os.path.realpath(os.path.expanduser(repo.strip()))
            if os.path.exists(os.path.join(d, ".git")):  # a checkout, never a bare folder
                return d
        return None

    hit = registered(folder)
    if hit:
        return hit, ""
    top = repo_top()
    if top:
        return top, ""
    hit = registered(None)
    if hit:
        return hit, ""
    return st.project_folder(), "no git repo resolved for this run"


def _gate_tail(text, n=GATE_OUTPUT_LINES):
    """The last n lines of a gate's output (stdout+stderr), as the refusal quotes them."""
    return "\n".join((text or "").splitlines()[-n:])


def _read_tail(pipe, box):
    """Reader thread: drain a gate's merged output; keep only the last GATE_TAIL_BYTES and a
    sha256 of the whole stream, so a gate printing 1 GB holds ~64 KB in memory."""
    h, tail = hashlib.sha256(), bytearray()
    for chunk in iter(lambda: pipe.read(65536), b""):
        h.update(chunk)
        tail += chunk
        if len(tail) > GATE_TAIL_BYTES:
            del tail[:-GATE_TAIL_BYTES]
    box["sha"], box["tail"] = h.hexdigest(), bytes(tail)


def banned_commands(data):
    """The project's top-level `banned_commands:` from its project yaml (block or one-line flow list).
    A missing file or key is an empty list. ponytail: scalars only, no anchors or multi-line flows."""
    try:
        with open(mkpaths.project_yaml(data), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for i, line in enumerate(lines):
        m = re.match(r"banned_commands:\s*(.*?)\s*(#.*)?$", line)
        if not m:
            continue
        flow = m.group(1)
        if flow.startswith("["):
            items = re.findall(r"\"([^\"]*)\"|'([^']*)'|([^,\[\]\s][^,\]]*)", flow)
            return [a or b or c.strip() for a, b, c in items if a or b or c.strip()]
        for nxt in lines[i + 1:]:
            m2 = re.match(r"\s+-\s+(.*?)\s*(\s#.*)?$", nxt)
            if not m2:
                if nxt.strip() and not nxt.lstrip().startswith("#"):
                    break
                continue
            out.append(m2.group(1).strip("\"'"))
        return out
    return out


def _banned_hit(cmd, banned):
    """The first banned entry whose words occur as a contiguous word run in cmd (`npm run dev`
    hits `cd x && npm run dev`, not `npm run devtools`); shell separators count as spaces."""
    words = re.sub(r"[;&|()<>]", " ", cmd).split()
    for entry in banned:
        ew = entry.split()
        if ew and any(words[i:i + len(ew)] == ew for i in range(len(words) - len(ew) + 1)):
            return entry
    return None


def _gate_exec(g, cwd, banned=()):
    """Run ONE table gate in cwd -> (result, output tail text, sha256 of the full output).
    A cmd matching a `banned` entry (project.yaml banned_commands) is never run: result `banned`.
    The gate gets its own process group (start_new_session); on timeout the WHOLE group is
    SIGKILLed, so no child outlives the gate (todo 140; subprocess.run only killed the shell).
    stdout+stderr are merged into one stream. The tail is bounded to GATE_TAIL_BYTES."""
    cmd, expect, timeout = g.get("cmd"), g.get("expect", 0), g.get("timeout", GATE_TIMEOUT_DEFAULT)
    empty = hashlib.sha256(b"").hexdigest()
    if not isinstance(cmd, str) or not cmd.strip():
        return "invalid", "no cmd (mk-plan check refuses a table gate without one)", empty
    if type(expect) is not int:
        return "invalid", f"expect {expect!r} is not an int", empty
    if type(timeout) is not int:
        return "invalid", f"timeout {timeout!r} is not an int", empty
    hit = _banned_hit(cmd, banned)
    if hit:
        return "banned", f"banned command {hit!r} (project.yaml banned_commands); gate not run", empty
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except OSError as e:  # cwd gone, or the shell cannot start: a failure, never a pass
        return "fail exit=127", f"{type(e).__name__}: {e}", empty
    box = {"sha": empty, "tail": b""}
    reader = threading.Thread(target=_read_tail, args=(proc.stdout, box), daemon=True)
    reader.start()

    def kill_group():
        with contextlib.suppress(OSError):  # ProcessLookupError: the group is already gone
            os.killpg(proc.pid, signal.SIGKILL)

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_group()
        proc.wait()
    reader.join(5)
    if reader.is_alive():  # a straggler in the group still holds the pipe: end the group
        kill_group()
        reader.join(5)
    with contextlib.suppress(OSError):
        proc.stdout.close()
    out = box["tail"].decode("utf-8", "replace")
    if timed_out:
        return "timeout", out, box["sha"]
    if proc.returncode == expect:
        return "pass", out, box["sha"]
    return f"fail exit={proc.returncode}", out, box["sha"]


def _gate_run(g, cwd, banned=()):
    """F.0: run ONE table gate in cwd -> (result, output tail). Result vocabulary: `pass`,
    `fail exit=N`, `timeout`, `banned` (cmd matches banned_commands, never run), or `invalid` for a gate the plan tool would have refused (this
    reads `parse`, which does not check shape). A wrong exit code, a timeout and a malformed
    gate all block; only exit == expect passes. The tail is the last GATE_OUTPUT_LINES lines."""
    result, out, _sha = _gate_exec(g, cwd, banned)
    return result, "" if result == "pass" else _gate_tail(out)


def run_gates(st, gates):
    """F.0: run every runnable gate of the run's plan in its integration checkout.
    -> (results, failures, where, note). `results` names every gate as the reply lists it: a
    plain string gate is `manual` (never run, never blocks); a table gate is `pass`,
    `fail exit=N`, `timeout`, `banned` or `deferred load=X`. Deferral blocks: a 1-minute load average
    above GATE_LOAD_MAX means the machine is busy, so NO gate passes and none is run."""
    where, note = integration_checkout(st)
    load = load_avg()
    deferred = load > GATE_LOAD_MAX
    banned = banned_commands(st.data)
    results, failures = {}, []
    for name, g in gates.items():
        if not isinstance(g, dict):
            results[name] = "manual"
            continue
        result, out = (f"deferred load={load:g}", "deferred: load average above "
                       f"{GATE_LOAD_MAX:g}") if deferred else _gate_run(g, where, banned)
        results[name] = result
        if result != "pass":
            failures.append({"gate": name, "result": result, "output": out})
    return results, failures, where, note


def current_tree_sha(where):
    """The git tree id of HEAD in the checkout `where` (`git rev-parse HEAD^{tree}`), or None when
    it is not a git repo with a commit. A TREE id (not the commit id) so an amend or empty commit
    that changes no file keeps earlier green evidence current. Evidence rows store this; the
    'is the evidence for the current tip' comparison later calls this same function."""
    try:
        r = subprocess.run(["git", "-C", where, "rev-parse", "HEAD^{tree}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = r.stdout.strip()
    return sha if r.returncode == 0 and sha else None


def _evidence_result(result):
    """Map a _gate_exec result to the EVIDENCE_RESULTS enum."""
    if result == "pass":
        return "green"
    if result == "timeout":
        return "timeout"
    if result.startswith("deferred"):
        return "deferred"
    if result.startswith("fail exit="):
        return "red"
    return "error"


def _gate_line(g):
    """The evidence `line` of a gate: its `lines` (task gate) or `covers` (final gate), joined."""
    v = g.get("lines", g.get("covers"))
    if isinstance(v, (list, tuple)):
        v = ",".join(str(x) for x in v)
    return str(v) if v not in (None, "") else None


def gate_record(st, rid, tid, name, g, where, tree, load, by, ref):
    """Run ONE gate (or defer it above GATE_LOAD_MAX) and write its evidence row -> the reply row.
    The one code path `gate run` and `reconcile` both write evidence through."""
    if load > GATE_LOAD_MAX:
        result, tail, sha = (f"deferred load={load:g}", f"deferred: load average above {GATE_LOAD_MAX:g}", None)
    else:
        result, tail, sha = _gate_exec(g, where, banned_commands(st.data))
    enum = _evidence_result(result)
    with st.locked():
        con = st.store_open()
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "INSERT INTO evidence(run, task, line, gate, result, tree_sha, by, ref, output_tail,"
                " output_sha256, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rid, tid, _gate_line(g), name, enum, tree, by, ref, tail, sha, now()))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {"id": cur.lastrowid, "task": tid, "gate": name, "result": enum, "detail": result, "tree_sha": tree}


def cmd_gate(a, st):
    """`gate run <T> | --final | --due`: run gates in the integration checkout through the F.0
    runner and write ONE evidence row per gate run. <T> = the task's extra_json `gate` list
    ({cmd, expect, timeout, lines}); --final = the run's extra_json `final_gates` ({id, cmd,
    expect, every, timeout, needs, covers}; `needs` is not enforced here); --due = final gates
    with every == "merge" once any task is merged, plus the gates of tasks in
    GATE_DUE_TASK_STATUSES. Plan-level [gates] stay with `run set --status done`.
    Load above GATE_LOAD_MAX records `deferred` and runs nothing. Reply ok=true when the command ran;
    `all_green` says whether every gate was green."""
    if bool(a.task) + bool(a.final) + bool(a.due) != 1:
        raise Fail("gate run needs exactly one of <task>, --final, --due")
    with st.locked():
        st.repair()
        run = st.load(a.run, a.task)
        run_x, task_x = st.extras_read(run["id"])
        status = {t["id"]: t.get("status") for t in run.get("tasks") or [] if isinstance(t, dict)}
    if a.task:
        find_task(run, a.task)

    def task_jobs(tid):
        gl = (task_x.get(tid) or {}).get("gate")
        gl = gl if isinstance(gl, list) else []
        return [(tid, g.get("id") or f"{tid}#{i}", g) for i, g in enumerate(gl, 1) if isinstance(g, dict)]

    def final_jobs(only_merge=False):
        fl = run_x.get("final_gates")
        fl = fl if isinstance(fl, list) else []
        return [(None, g.get("id") or f"final#{i}", g) for i, g in enumerate(fl, 1)
                if isinstance(g, dict) and (not only_merge or g.get("every") == "merge")]

    if a.task:
        jobs = task_jobs(a.task)
    elif a.final:
        jobs = final_jobs()
    else:
        jobs = final_jobs(True) if "merged" in status.values() else []
        for tid, stt in status.items():
            if stt in GATE_DUE_TASK_STATUSES:
                jobs += task_jobs(tid)
    where, note = integration_checkout(st)
    load = load_avg()
    tree = current_tree_sha(where)
    rows = [gate_record(st, run["id"], tid, name, g, where, tree, load, getattr(a, "by", "orchestrator"), a.ref)
            for tid, name, g in jobs]
    out = {"ok": True, "evidence": rows, "where": where, "all_green": all(r["result"] == "green" for r in rows)}
    if note:
        out["note"] = note
    return out


def _evidence_con(st):
    """A store connection for evidence reads, or None when the run has no project store."""
    return st.store_open() if st.store_on else None


def fidelity_gate(st, run, task_ids, final=False):
    """F.3: the ONE check every closing path calls. In a fidelity run (run.fidelity, set by run
    init for a plan with the F.2 contract) each task in `task_ids` needs, for every gate of its
    `gate` list, a latest evidence row that is green AND whose tree_sha is the integration
    checkout's current tree (older evidence is STALE and does not count); a `manual` line needs a
    recorded green sign-off (gate 'manual', any tree). `final=True` adds every final gate.
    Legacy (non-fidelity) runs are exempt. Raises Fail naming each missing gate; writes nothing."""
    missing = fidelity_missing(st, run, task_ids, final)
    if missing:
        raise Fail("fidelity run " + run["id"] + " needs fresh green evidence (gate run / gate signoff): "
                   + "; ".join(missing), missing=missing)


def fidelity_missing(st, run, task_ids, final=False):
    """The list behind fidelity_gate: one text per gate lacking fresh green evidence ([] when the
    run is legacy or nothing is missing). rollup() calls it directly, since it must not raise."""
    if not run.get("fidelity"):
        return []
    byid = {t["id"]: t for t in run.get("tasks") or [] if t.get("id")}
    need = []  # (task or None, gate name)
    for tid in task_ids:
        t = byid.get(tid) or {}
        for i, g in enumerate(t.get("gate") or [], 1):
            need.append((tid, g.get("id") or f"{tid}#{i}"))
        if t.get("manual"):
            need.append((tid, "manual"))
    if final:
        need += [(None, g.get("id") or f"final#{i}") for i, g in enumerate(run.get("final_gates") or [], 1)]
    if not need:
        return []
    where, _ = integration_checkout(st)
    tree = current_tree_sha(where)
    con = _evidence_con(st)
    missing = []
    try:
        for tid, name in need:
            row = con.execute(
                "SELECT result, tree_sha FROM evidence WHERE run=? AND task IS ? AND gate=? ORDER BY id DESC LIMIT 1",
                (run["id"], tid, name)).fetchone() if con else None
            label = f"{tid or 'final'}:{name}"
            if row is None:
                missing.append(f"{label} no evidence")
            elif row["result"] != "green":
                missing.append(f"{label} latest evidence is {row['result']}")
            elif name != "manual" and (tree is None or row["tree_sha"] != tree):
                missing.append(f"{label} evidence is stale (tree {str(row['tree_sha'])[:8]} != {str(tree)[:8]})")
    finally:
        if con:
            con.close()
    return missing


def cmd_gate_signoff(a, st):
    """`gate signoff <T>`: record the human sign-off of a task's `manual` line as a green evidence
    row (gate 'manual'). The line is never run."""
    with st.locked():
        st.repair()
        run = st.load(a.run, a.task)
        t = find_task(run, a.task)
        if not t.get("manual"):
            raise Fail(f"task {t['id']} has no manual line to sign off")
        if not a.ref:
            raise Fail("gate signoff needs --ref (who/what signed off)")
        con = st.store_open()
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "INSERT INTO evidence(run, task, line, gate, result, tree_sha, by, ref, output_tail,"
                " output_sha256, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run["id"], t["id"], None, "manual", "green", current_tree_sha(integration_checkout(st)[0]),
                 getattr(a, "by", "orchestrator"), a.ref, t["manual"], None, now()))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {"ok": True, "evidence": cur.lastrowid, "task": t["id"]}


RECONCILE_KIND = "reconcile-inconclusive"


def reconcile_mark_due(st, run, t):
    """F.5: the phase (top-level ancestor of t) whose subtree is now all closed gets ONE
    reconcile_due row (scope = phase id, done NULL). A plain INSERT: no gate runs in the write that
    closes the phase. No row while one is already open for the scope; none in a legacy run."""
    if not run.get("fidelity"):
        return
    chain = ancestor_chain(run, t.get("parent"))
    root = chain[-1] if chain else t
    ids = {root["id"], *descendant_ids(root["id"], run["tasks"])}
    if any(x.get("status") not in CLOSED_STATUSES for x in run["tasks"] if x.get("id") in ids):
        return
    con = st.store_open()
    try:
        con.execute("BEGIN IMMEDIATE")
        if not con.execute("SELECT 1 FROM reconcile_due WHERE run=? AND scope=? AND done IS NULL",
                           (run["id"], root["id"])).fetchone():
            con.execute("INSERT INTO reconcile_due(run, scope, created) VALUES (?,?,?)",
                        (run["id"], root["id"], now()))
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def reconcile_open(st, run):
    """F.5: what stops a fidelity run from closing/archiving: tasks at `rework`, open risk_reasons
    rows, reconcile_due rows not done. Raises Fail naming them; legacy runs are exempt."""
    if not run.get("fidelity"):
        return
    parts = []
    rework = [t["id"] for t in run.get("tasks") or [] if t.get("id") and t.get("status") == "rework"]
    if rework:
        parts.append("task(s) at rework: " + ", ".join(rework))
    con = _evidence_con(st)
    try:
        if con:
            rr = [f"{r['task']}:{r['kind']}" for r in con.execute(
                "SELECT task, kind FROM risk_reasons WHERE run=? AND cleared IS NULL ORDER BY id", (run["id"],))]
            if rr:
                parts.append("open risk reason(s): " + ", ".join(rr))
            due = [r["scope"] for r in con.execute(
                "SELECT scope FROM reconcile_due WHERE run=? AND done IS NULL ORDER BY id", (run["id"],))]
            if due:
                parts.append("reconcile due for: " + ", ".join(due) + " (run `reconcile`)")
    finally:
        if con:
            con.close()
    if parts:
        raise Fail(f"run {run['id']} has open reconcile work: " + "; ".join(parts), reconcile=parts)


def cmd_reconcile(a, st):
    """`reconcile --run R [--phase P]` (F.5): run the due and STALE gates (final gates + task gates
    of closed tasks) through gate_record, so evidence is written exactly as `gate run` does. A red
    gate reruns once: red twice on the same tree_sha = reproducible, so the tasks it covers (a task
    gate: the task itself) go merged -> rework; red then green = flaky, nothing changes. timeout /
    error / deferred are inconclusive: an open risk row per covered task, no status change. The due
    rows of the scope are stamped done unless a task went to rework or an inconclusive gate has no
    task to hold its reason. A clean run changes nothing but evidence rows and the due stamp."""
    with st.locked():
        st.repair()
        run = st.load(a.run)
    out = {"ok": True, "ran": [], "green": [], "reworked": [], "inconclusive": [], "flaky": []}
    if not run.get("fidelity"):
        return {**out, "note": "legacy run: nothing to reconcile"}
    rid, tasks = run["id"], run.get("tasks") or []
    byid = {t["id"]: t for t in tasks if t.get("id")}
    if a.phase:
        find_task(run, a.phase)
    scope = {x for x in byid if not a.phase or x == a.phase or x in descendant_ids(a.phase, tasks)}
    con = _evidence_con(st)
    try:
        due = [r["scope"] for r in con.execute(
            "SELECT scope FROM reconcile_due WHERE run=? AND done IS NULL ORDER BY id", (rid,))
            if r["scope"] in scope]
    finally:
        con.close()
    where, _ = integration_checkout(st)
    tree, load = current_tree_sha(where), load_avg()

    def stale(tid, name):
        c = _evidence_con(st)
        try:
            r = c.execute("SELECT result, tree_sha FROM evidence WHERE run=? AND task IS ? AND gate=?"
                          " ORDER BY id DESC LIMIT 1", (rid, tid, name)).fetchone()
        finally:
            c.close()
        return r is None or r["result"] != "green" or tree is None or r["tree_sha"] != tree

    jobs = []  # (task or None, gate name, gate, tasks it covers)
    for tid in scope:
        if byid[tid].get("status") in CLOSED_STATUSES:
            jobs += [(tid, g.get("id") or f"{tid}#{i}", g, [tid])
                     for i, g in enumerate(byid[tid].get("gate") or [], 1)
                     if stale(tid, g.get("id") or f"{tid}#{i}")]
    for i, g in enumerate(run.get("final_gates") or [], 1):
        if due or stale(None, g.get("id") or f"final#{i}"):
            jobs.append((None, g.get("id") or f"final#{i}", g, [c for c in g.get("covers") or [] if c in byid]))
    by = getattr(a, "by", "orchestrator")
    rework, inconclusive, unheld = [], [], False
    for tid, name, g, covered in jobs:
        row = gate_record(st, rid, tid, name, g, where, tree, load, by, None)
        out["ran"].append(name)
        res = row["result"]
        if res == "red":  # one rerun on the same tree decides: red twice = reproducible
            row = gate_record(st, rid, tid, name, g, where, current_tree_sha(where), load, by, None)
            res = "reproducible" if row["result"] == "red" and row["tree_sha"] == tree else row["result"]
            if res == "green":
                out["flaky"].append(name)
                continue
        if res == "green":
            out["green"].append(name)
        elif res == "reproducible":
            rework += covered
        else:
            inconclusive += covered
            unheld = unheld or not covered
    with st.locked():
        st.repair()
        run = st.load(a.run)
        byid = {t["id"]: t for t in run["tasks"] if t.get("id")}
        for cid in dict.fromkeys(rework):  # a covered parent reworks its merged leaves
            for lid in leaf_descendants(cid, run["tasks"]):
                t = next((x for x in run["tasks"] if (x.get("id") or "").lower() == lid), None)
                if t and t["status"] == "merged":
                    st.event(run, "transition", "reconcile: gate failed twice on the same tree",
                             task=t["id"], frm="merged", to="rework", by=by)
                    t["status"], t["updated"] = "rework", now()
                    out["reworked"].append(t["id"])
        if out["reworked"]:
            st.save(run)
            st.flush()
        out["inconclusive"] = list(dict.fromkeys(inconclusive))
        c = st.store_open()
        try:
            c.execute("BEGIN IMMEDIATE")
            for tid in out["inconclusive"]:
                if not c.execute("SELECT 1 FROM risk_reasons WHERE run=? AND task=? AND kind=? AND cleared IS NULL",
                                 (rid, tid, RECONCILE_KIND)).fetchone():
                    c.execute("INSERT INTO risk_reasons(run, task, kind, points, source, opened)"
                              " VALUES (?,?,?,?,?,?)", (rid, tid, RECONCILE_KIND, 1, "reconcile", now()))
            if due and not rework and not unheld:
                c.execute("UPDATE reconcile_due SET done=? WHERE run=? AND done IS NULL AND scope IN (%s)"
                          % ",".join("?" * len(due)), (now(), rid, *due))
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            c.close()
    return out


def cmd_risk(a, st):
    """`risk add <T> --kind K --points N [--source S]` | `risk list [<T>]` | `risk clear <id>
    --evidence <id>`. Rows live in risk_reasons (F.1); the derived risk is mkdb._task_risk. A
    clear needs the id of a green evidence row of that task whose tree_sha is the current tree."""
    with st.locked():
        st.repair()
        run = st.load(a.run, a.task if a.action != "clear" else None)
        db = st.db()
        con = st.store_open()
        try:
            if a.action == "list":
                q, args = "SELECT * FROM risk_reasons WHERE run=?", [run["id"]]
                if a.task:
                    q, args = q + " AND task=?", args + [find_task(run, a.task)["id"]]
                rows = [dict(r) for r in con.execute(q + " ORDER BY id", args)]
                risks = {tid: db._task_risk(con, run["id"], tid) for tid in {r["task"] for r in rows}}
                return {"ok": True, "reasons": rows, "risk": risks}
            con.execute("BEGIN IMMEDIATE")
            if a.action == "add":
                t = find_task(run, a.task)
                if not a.kind or a.points is None or a.points < 1:
                    raise Fail("risk add needs --kind and --points >= 1")
                cur = con.execute("INSERT INTO risk_reasons(run, task, kind, points, source, opened)"
                                  " VALUES (?,?,?,?,?,?)", (run["id"], t["id"], a.kind, a.points, a.source, now()))
                out = {"ok": True, "id": cur.lastrowid, "task": t["id"]}
                tid = t["id"]
            else:  # clear
                r = con.execute("SELECT * FROM risk_reasons WHERE id=? AND run=?", (a.task, run["id"])).fetchone()
                if r is None or r["cleared"]:
                    raise Fail(f"risk reason {a.task!r} is not an open reason of run {run['id']}")
                tid = r["task"]
                if a.evidence is None:
                    raise Fail("risk clear needs --evidence <id> of a fresh green gate run")
                ev = con.execute("SELECT * FROM evidence WHERE id=? AND run=? AND task IS ?",
                                 (a.evidence, run["id"], tid)).fetchone()
                tree = current_tree_sha(integration_checkout(st)[0])
                if ev is None or ev["result"] != "green" or ev["gate"] == "manual" or ev["tree_sha"] != tree or tree is None:
                    raise Fail(f"evidence {a.evidence} is not a green gate run of {tid} on the current tree")
                con.execute("UPDATE risk_reasons SET cleared=?, cleared_by=? WHERE id=?",
                            (now(), f"evidence:{a.evidence}", r["id"]))
                out = {"ok": True, "cleared": r["id"], "task": tid}
            out["risk"] = db._task_risk(con, run["id"], tid)
            con.execute("COMMIT")
            return out
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def gate_refusal(failures, where):
    """The refusal text: each failing gate with its result and the last 20 lines of its output."""
    parts = [f"{len(failures)} plan gate(s) failed in {where}:"]
    for f in failures:
        parts.append(f"  gate {f['gate']}: {f['result']}")
        parts += [f"    {line}" for line in f["output"].splitlines()]
    return "\n".join(parts)


def close_checks(st, run):
    """F.0: everything `run set --status done` must clear, AFTER which the run may close.
    -> the reply keys to add. Raises Fail (having written nothing) when a task or phase of the
    run is still open, or when a runnable plan gate fails, times out or is deferred. A plain
    string gate is `manual`: it is reported and never blocks. A plan whose gates cannot be read
    (no plan link, a missing plan file, a plan-tool error) is not a close gate: the close keeps
    its best-effort plan behaviour, and gate_error says so."""
    opens = open_work(run)
    if opens:
        raise Fail(f"run {run['id']} is not closed: "
                   + ", ".join(f"{o['id']} ({o['status']})" for o in opens)
                   + " — close them (task set <id> status=done) or leave the run open",
                   open=opens)
    fidelity_gate(st, run, [t["id"] for t in run.get("tasks") or []
                            if t.get("id") and t.get("status") in CLOSED_STATUSES], final=True)
    reconcile_open(st, run)
    if not run.get("plan"):
        return {}
    try:
        cur = plan_tool("parse", st.plan_file(run))
    except Fail as e:
        return {"gate_error": f"plan gates not run: {e}"}
    gates = cur.get("gates") if isinstance(cur.get("gates"), dict) else {}
    if not gates:
        return {}
    results, failures, where, note = run_gates(st, gates)
    if failures:
        raise Fail(gate_refusal(failures, where), gates=results,
                   failed=[f["gate"] for f in failures])
    out = {"gates": results}
    if note:
        out["gate_error"] = note
    return out


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


def add_task(st, run, tid, title, wave, deps, owned, parent, by, extra=None, followup=False):
    """The ONE task-add path (`task add`, `run init --plan`, `plan sync --apply`): validate, append,
    save (rollup, ready, board, other runs included). `extra` rides on the new task (C4 plan links).
    `--followup` is the only way onto a merged/qa/done parent: it adds the child and rolls that
    phase back to `building`, so its status keeps rolling up from its children.
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
            if not followup:
                raise Fail(f"--parent onto {p['id']} ({p['status']}) is refused: status rolls up from "
                           "children (pass --followup to add a follow-up under a merged phase)")
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
    # D1.5.1: the plan fields ride the file view too, so run.json tasks carry what the store
    # has (acceptance/refs/type) and the mirror proves them instead of blanking them.
    t = {"id": tid, "title": title, "wave": wave, "deps": deps, "owned": owned, "parent": parent,
         "ready": False, "status": "planned", "agent": None, "model": None, "effort": None, "lane": None,
         "pane": None, "branch": None, "workspace": None, "slot": None,
         "last_commit": None, "gates": None, "verdict": None, "round": 0, "rounds": [],
         "created": now(), "updated": now(), **(extra or {})}
    run["tasks"].append(t)
    st.event(run, "transition", f"task added: {title}", task=tid, to="planned", by=by)
    reopen = p if (followup and parent and p["status"] in DEP_MET) else None
    if reopen is not None:  # a follow-up re-opens its phase: back to building, and it says so
        frm = reopen["status"]
        reopen["status"], reopen["updated"] = "building", now()
        st.event(run, "transition", f"follow-up {tid} added: {frm} → building", task=reopen["id"],
                 frm=frm, to="building", by=by)
        st.event(run, "note", f"follow-up {tid} added", task=reopen["id"], by=by)
        st._rollup_keep = {reopen["id"]}  # the rollback is the point: roll-up does not read it back
    try:
        st.save(run, t, by)
    finally:
        st._rollup_keep = None
    warnings += st.refresh_others(run, by)  # their boards show "blocks"; ready stays correct
    return t, warnings


def task_type_of(run, t, fresh=False):
    """D1.5.1/D1.5.2: the store column for one task — the plan's id when run init copied it
    (a [[phase]] row is a phase, a [[phase.task.subtask]] row is a subtask), else derived from
    the tree: no parent -> phase, a parent that is itself a phase -> task, deeper -> subtask.
    A task row never pretends to be a bare task when the plan said otherwise. `fresh` ignores the
    stored value and re-derives from the tree — what a re-parent needs for T and its subtree."""
    if not fresh and t.get("type") in ("phase", "task", "subtask"):
        return t["type"]
    parent = t.get("parent")
    if not parent:
        return "phase"
    p = next((x for x in run.get("tasks") or [] if x.get("id") == parent), None)
    if p is not None and not p.get("parent"):
        return "task"
    return "subtask"


def descendants_of(tasks, task_id):
    """-> {id, ...} every task below `task_id`, at any depth."""
    children_of = {}
    for x in tasks:
        p = (x.get("parent") or "").lower()
        if p:
            children_of.setdefault(p, []).append(x)
    out, frontier = set(), [task_id.lower()]
    while frontier:
        for c in children_of.get(frontier.pop(), []):
            out.add(c["id"])
            frontier.append(str(c["id"]).lower())
    return out


def reparent(st, run, t, new_parent, by):
    """`task set T parent=P` — move T under P; an empty value makes T a root, i.e. a phase.

    P must exist in this run, and it may not be T itself or anything below T (that would make a
    cycle). T's own type and every type under it are re-derived from the tree (task_type_of), so
    a root that becomes a child is a task and a task that becomes a root is a phase again. The
    move goes through the caller's normal save, so the old and the new parent roll up and the
    file view and the store agree.
    -> [task, ...] the tasks whose own task.json this move touched (T, old and new parent)."""
    below = descendants_of(run["tasks"], t["id"])
    new_id = None
    if new_parent not in (None, ""):
        p = next((x for x in run["tasks"] if str(x["id"]).lower() == str(new_parent).lower()), None)
        if p is None:
            raise Fail(f"no task {new_parent!r} in run {run['id']}")
        if p["id"] == t["id"]:
            raise Fail(f"task {t['id']} cannot be its own parent")
        if p["id"] in below:
            raise Fail(f"parent {p['id']} is under {t['id']}: the move would make a cycle")
        new_id = p["id"]
    old = t.get("parent")
    if new_id == old:
        return [t]
    t["parent"] = new_id
    for x in [t, *(y for y in run["tasks"] if y.get("id") in below)]:
        x["type"] = task_type_of(run, x, fresh=True)
    st.event(run, "note", f"parent: {old or 'root'} → {new_id or 'root'}", task=t["id"], by=by)
    touched = [t] + [x for x in run["tasks"] if x.get("id") in {old, new_id} and x is not t]
    return touched


def task_has_brief(st, run, t):
    """True when this task has a brief file for any round (tasks/<T>/brief-r<N>.md). A task that
    was already briefed owns files, so the check can require `owned` from that round on."""
    d = st.task_dir(run["id"], t["id"])
    try:
        return any(re.fullmatch(r"brief-r\d+\.md", name) for name in os.listdir(d))
    except OSError:
        return False


def task_check(st, run, t, at_dispatch=False):
    """D1.5.4: the mandatory-field check for one task -> {"ok", "missing": [...], ...}.

    Mandatory: title (every row); acceptance, parent and owned for a task (a phase is the
    parent of tasks, it carries no acceptance and needs no parent); owned once the task has a
    brief; and agent when the check runs at dispatch (--at-dispatch: the route dispatch records
    must be there before an agent is prompted). `type` is the plan's own, else derived from the
    tree (task_type_of) — a legacy run.json without the key is still classified, not skipped.
    Read-only: nothing is written and no status moves."""
    typ = task_type_of(run, t)
    missing = []
    if not (t.get("title") or "").strip():
        missing.append("title")
    if typ != "phase":
        if not (t.get("acceptance") or "").strip():
            missing.append("acceptance")
        if not t.get("parent"):
            missing.append("parent")
    if task_has_brief(st, run, t) and not (t.get("owned") or []):
        missing.append("owned")
    if at_dispatch and not (t.get("agent") or "").strip():
        missing.append("agent")
    return {"ok": not missing, "task": t["id"], "type": typ, "missing": missing,
            **({"at_dispatch": True} if at_dispatch else {})}


def plan_fidelity(cur):
    """F.2: True when the parsed plan carries the gate contract: any final gate, or any task or
    subtask with a gate, a manual line, critical, or a risk other than the default 1. A pre-F plan
    parses to all defaults and gets no flag. (An explicit `risk = 1` alone is not detectable.)"""
    if cur.get("final_gates"):
        return True
    for ph in cur.get("phases") or []:
        for c in ph.get("tasks") or []:
            for it in [c] + list(c.get("subtasks") or []):
                if it.get("gate") or it.get("manual") or it.get("critical") or it.get("risk", 1) != 1:
                    return True
    return False


def plan_field_extra(extra, src, kind, fid=False):
    """D1.5.1: the plan fields (acceptance/refs/type) a `run init --plan` / `plan sync --apply`
    add copies onto the new run.json task. The plan tool (C3, a subprocess) is the only reader
    of the plan file; mk-state consumes what its `parse` already returned. Phases carry no
    acceptance (the plan has no acceptance key there); their route stays a file field."""
    out = dict(extra or {})
    out["type"] = kind
    if kind in ("task", "subtask"):
        out["acceptance"] = (src.get("acceptance") or "") or None
        refs = src.get("refs") or []
        out["refs"] = list(refs) if isinstance(refs, list) else ([refs] if refs else [])
        if fid:  # F.2: the gate contract rides tasks.extra_json; a pre-F plan adds no key
            out.update({"gate": list(src.get("gate") or []), "manual": src.get("manual") or "",
                        "critical": bool(src.get("critical")), "base_risk": src.get("risk", 1),
                        "acceptance_lines": list(src.get("acceptance_lines") or [])})
    return out


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
            if a.root == bool(a.parent):  # F.11: a parent or an explicit --root, never both, never neither
                raise Fail("--parent and --root exclude each other" if a.root else
                           "task add needs --parent <id> to nest it, or --root to create a top-level phase")
            t, warnings = add_task(st, run, a.id, a.title, a.wave, csv(a.deps), csv(a.owned), a.parent,
                                   a.by, followup=a.followup)
            st.flush()
            return {"ok": True, "task": t, **({"warnings": warnings} if warnings else {})}
        if a.action == "check":
            return task_check(st, run, find_task(run, a.task), at_dispatch=getattr(a, "at_dispatch", False))
        if a.action == "brief":
            return task_brief(st, run, find_task(run, a.task), a)
        if a.action == "export":
            return task_export(st, run, find_task(run, a.task), a)
        t = find_task(run, a.task)
        cur = t["rounds"][-1] if t["rounds"] else None
        if a.action == "verify":
            return task_verify(st, run, t, a)
        if a.action == "set":
            changes = {}
            set_warnings = []  # T1: the started-task drops and the dep notes, merged into the reply
            for pair in a.pairs:
                k, sep, v = pair.partition("=")
                if k in GATE_READONLY_KEYS or (k.startswith(("gate", "risk")) and k != "gates"):
                    raise Fail(f"{k} is a plan gate field and is read-only: task set never edits "
                               "gate/manual/critical/risk; change it in the plan text and approve "
                               "a new version")
                if not sep or k not in TASK_KEYS:
                    raise Fail(f"bad pair {pair!r}; keys: {' '.join(TASK_KEYS)}")
                if k in SET_LIST_KEYS:  # T1: owned/deps/refs parse like task add's flags
                    changes[k] = csv(v) or None  # an empty value clears, like every other key
                else:
                    changes[k] = parse_value(v)
            if "status" in changes:
                # F2.2: the alias resolves before the check; the canonical word is what is stored
                changes["status"] = STATUS_ALIASES.get(changes["status"], changes["status"])
                if changes["status"] not in STATUSES:
                    raise Fail(f"bad status {changes['status']!r}; one of: {' '.join(STATUSES)}")
            # T1: a task that already started keeps the text its round ran under. The pair is
            # dropped, not refused: the reply carries the warning and exits 0.
            started = [k for k in SET_START_KEYS if k in changes]
            if started and t["status"] not in EDITABLE_WHILE:
                for k in started:
                    changes.pop(k)
                set_warnings.append(f"task {t['id']} is {t['status']} (started): "
                                    f"{', '.join(started)} changed; not updated")
            if changes.get("deps"):
                # the task add rule, on an edit: stored id case, a self-dep refused, an unknown
                # target warned (never fatal — it may be added later).
                fixed, missing = [], []
                for d in changes["deps"]:
                    nd = st.norm_dep(run, t["id"], d)
                    fixed.append(nd)
                    if st.dep_status(run, nd) is None:
                        missing.append(nd)
                changes["deps"] = fixed
                set_warnings += [f"dep {d} not found (yet)" for d in missing]
            olds = {k: t.get(k) for k in SET_START_KEYS if k in changes}  # for the decision events
            if "parent" in changes:
                reparent(st, run, t, changes.pop("parent"), a.by)
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
                if s in CLOSED_STATUSES:  # F.3: no leaf closes without fresh green evidence
                    fidelity_gate(st, run, [t["id"]] + descendant_ids(t["id"], run["tasks"]))
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
            if changes.get("status") in DEP_GATED and t["status"] not in DEP_GATED:
                waiting = st.unmet_tree(run, t)  # the task's own deps plus every ancestor's
                if waiting:
                    if not a.force:
                        raise Fail(f"task {t['id']} waits on {', '.join(waiting)} (not merged/qa/done); pass --force "
                                   "to dispatch anyway", unmet=waiting)
                    st.event(run, "decision", f"forced by {a.by}: task set {t['id']} status={changes['status']} "
                             f"(unmet: {', '.join(waiting)})", task=t["id"], by=a.by)
            frm = t["status"]
            if changes.get("status") in CLOSED_STATUSES and changes["status"] != frm:
                fidelity_gate(st, run, [t["id"]])  # F.3: before any write
            t.update(changes)
            # T1: one decision event per edited key, old and new value (short) plus the --note.
            for k, old in olds.items():
                new = changes[k]
                if old != new:  # a key set to its current value is not an edit: no event
                    st.event(run, "decision", f"task set {t['id']} {k}: {short_val(old)} → "
                             f"{short_val(new)}" + (f" — {a.note}" if a.note else ""),
                             task=t["id"], by=a.by)
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
            if a.kind == "rework" and any(x["status"] != "merged" for x in [t] + [
                    find_task(run, i) for i in csv(a.tasks)[1:]]):
                raise Fail("round --kind rework moves a merged task to rework; "
                           "every task of the round must be merged")
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
                if x is not t:  # save() below stores every task through flush()
                    x["updated"] = now()
            st._round_reset = [(x["id"], a.kind) for x in group]
        t["updated"] = now()
        st.save(run, t, a.by)
        warnings = (set_warnings + st.refresh_others(run, a.by)) if a.action == "set" else []
        st.flush()
        if a.action == "set" and (fanned or changes.get("status") in CLOSED_STATUSES) \
                and t["status"] in CLOSED_STATUSES:
            reconcile_mark_due(st, run, t)  # F.5: the phase's last close; a plain INSERT, no gate run
        if a.action == "round":
            d = st.task_dir(run["id"], t["id"])
            paths = {k: os.path.join(d, f"{k}-r{t['round']}.md") for k in ("brief", "report", "review")}
            stored = st.store_round_files(run, t)
            return {"ok": True, "task": t["id"], "round": t["round"], "kind": a.kind,
                    **({"tasks": csv(a.tasks)} if a.tasks else {}),
                    **({"stored": stored} if stored else {}), **paths}
        return {"ok": True, "task": t, **({"warnings": warnings} if warnings else {})}


def task_brief(st, run, t, a):
    """X3.1: author the round's brief into the project DB in ONE command — the store is the
    source (C4); the file was the hand-written detour today's flow used.

    Shape picked: `task brief T --run R --file PATH | --stdin`, a separate subcommand rather
    than a `task round` flag, because `task round` is a pure transition (it must stay runnable
    before any brief exists, and a lane re-runs `task brief` without re-rounding). Re-running
    for the round REPLACES the brief: mkdb.put_artifact inserts rev = last+1 for the key
    (run, task, round, kind), so exactly one newest row per round; no duplicate pile. The view
    file brief-rN.md is rewritten to match the artifact (the view follows the store; a hand
    edit to the view is named in the reply's `previous_view_body` before it is overwritten)."""
    if not st.store_on:
        raise Fail("task brief needs the project store on (C4)")
    if not t.get("rounds"):
        raise Fail(f"task {t['id']} has no round; 'task round {t['id']} --kind build' first")
    n = t["round"] or t["rounds"][-1]["n"]
    if a.file:
        if not os.path.exists(a.file):
            raise Fail(f"--file {a.file!r} does not exist")
        with open(a.file, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    con = st.store_open()
    try:
        row = con.execute(
            "SELECT text, rev FROM artifacts WHERE run=? AND task=? AND round=? AND kind='brief'"
            " ORDER BY rev DESC, id DESC LIMIT 1", (run["id"], t["id"], n)).fetchone()
    finally:
        con.close()
    # put_artifact replaces by rev (one newest row per (task, round, kind)); the same bytes
    # already stored: no rev bump, idempotent. The view file follows the artifact either way.
    # R2 FIX (CRITICAL): this is a PROJECT store write — put_artifact is called directly
    # (root_write is for the root projection and short-circuits to None under
    # _skip_root_projection, which made `task brief` fail on a temp/default-home data dir).
    if row and row["text"] == text:
        stored = {"id": None, "rev": row["rev"]}  # idempotent re-run: no rev bump
    else:
        stored = st.db().put_artifact(st.home_dir(), st.project_slug(), run["id"],
                                      t["id"], n, "brief", text,
                                      data_dir=st.project_folder())
    fp = os.path.join(st.task_dir(run["id"], t["id"]), f"brief-r{n}.md")
    out = {"ok": True, "task": t["id"], "round": n, "stored": True, "view": fp,
           "rev": stored["rev"]}
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    if os.path.exists(fp):
        with open(fp, encoding="utf-8") as f:
            old = f.read()
        if old != text:
            out["previous_view_body"] = old  # the hand edit, preserved in the reply before overwrite
    with open(fp, "w", encoding="utf-8") as f:
        f.write(text)
    return out


def task_export(st, run, t, a):
    """X3.3: write the task folder's round files FROM the artifacts — an audit view, for a
    bus-down lane. Every round of this task; a kind exports only when an artifact holds it.

    Idempotent: the same bytes may be rewritten (content cannot drift), but a differing file
    is left in place and NAMED — the export never clobbers a hand edit without saying so."""
    if not st.store_on:
        raise Fail("the project store is off; task export needs it")
    con = st.store_open()
    try:
        rows = con.execute(
            "SELECT kind, round, text FROM artifacts WHERE run=? AND task=?"
            " AND kind IN ('brief','report','review') ORDER BY round, kind, id",  # last wins
            (run["id"], t["id"])).fetchall()
    finally:
        con.close()
    newest = {}  # (round, round-canonical kind) -> text: the last row per (round, kind) wins
    for r in rows:
        newest[(r["round"], r["kind"])] = r["text"]
    d = st.task_dir(run["id"], t["id"])
    diff, written = [], []
    for (n, kind), text in sorted(newest.items()):
        if not n:
            continue  # round 0 is the run-doc shape, not a task-folder round file (X3.3 scope)
        fp = os.path.join(d, f"{kind}-r{n}.md")
        os.makedirs(d, exist_ok=True)
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as f:  # the audit view: differing file is not clobbered
                if f.read() != text:
                    diff.append(fp)
                    continue
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)
        written.append(fp)
    out = {"ok": True, "task": t["id"], "written": written,
           **({"differing": diff} if diff else {}),
           **({"empty": True} if not written and not diff else {})}
    if not written and not diff:
        out["why"] = "no brief/report/review artifacts for this task (or no rounds yet)"
    return out


def count_pair(v, flag):
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


def descendant_ids(task_id, tasks):
    """Every task id below task_id (children, grandchildren, ...), parents and leaves alike."""
    out, stack = [], [task_id.lower()]
    while stack:
        cur = stack.pop()
        for x in tasks:
            if (x.get("parent") or "").lower() == cur and x.get("id"):
                out.append(x["id"])
                stack.append(x["id"].lower())
    return out


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
    if (header.get("final_gates") or []) != (cur.get("final_gates") or []):
        diffs.append("final_gates")  # F.2: the header [[final_gate]] blocks
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
        # T1: the tasks are the source once a run is live. A plan sync rewrites title/acceptance/
        # owned/deps from the file, which silently drops `task set` edits — so it refuses while
        # any task is not terminal (merged/done), dry-run included. --from-file is the explicit
        # "the plan file wins" override; an all-terminal or empty run is unchanged.
        live = [t["id"] for t in run.get("tasks") or []
                if t.get("status") not in ("merged", "done")]
        if live and not getattr(a, "from_file", False):
            raise Fail("plan sync on a live run overwrites task edits made with task set; "
                       "change tasks with 'task set T title=… acceptance=… owned=… deps=…' "
                       "or pass --from-file", live=live)
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
        fid = plan_fidelity(cur)
        try:
            if fid:  # F.2: existing tasks keep their frozen gate fields; the run learns the contract
                run["fidelity"], run["final_gates"] = True, list(cur.get("final_gates") or [])
            for kind, src, ph in adds:
                if kind == "phase":
                    add_task(st, run, str(src.get("id") or ""), src.get("title") or src.get("id"),
                             src.get("wave"), src.get("deps") or [], src.get("owned") or [], None, a.by,
                             plan_field_extra({"plan_phase": src.get("id"), "route": src.get("route") or None,
                                               "lane_group": src.get("lane_group") or None,
                                               "reviewer": src.get("reviewer") or ""}, src, "phase"))
                else:
                    add_task(st, run, str(src.get("id") or ""), src.get("title") or src.get("id"),
                             ph.get("wave"), src.get("deps") or [], src.get("owned") or [],
                             str(ph.get("id") or ""), a.by,
                             plan_field_extra({"plan_task": src.get("id")}, src, "task", fid))
                    for s in src.get("subtasks") or []:
                        add_task(st, run, str(s.get("id") or ""), s.get("title") or s.get("id"),
                                 ph.get("wave"), s.get("deps") or [], s.get("owned") or [],
                                 str(src.get("id") or ""), a.by,
                                 plan_field_extra({}, s, "subtask", fid))
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
                              "final_gates": cur.get("final_gates") or [],
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


def backfill_fields(st, run, by):
    """D1.5.5: one-shot `run backfill-fields` — fill the EMPTY file-view plan fields
    (acceptance/refs/owned/deps/type/route) of this run's tasks from the approved plan.
    Self-contained: it re-derives everything from the plan tool's `parse` (C3: mk-plan.py is
    a subprocess, never imported) and from the run's own link columns (plan_phase/plan_task);
    it does not depend on the D1.5.1 helpers. Nothing else is written: status/lane/round/
    verdict/slot/branch/workspace/pane keep their values, agent/model/effort are not invented
    and route_json stays whatever dispatch (D1.5.3) put there. The writes go through the
    normal save path, so the store mirror and the run-doc artifact update together, and the
    field list lands on the file view where the D1.5.2 merge-keep then protects it."""
    cur = plan_tool("parse", st.plan_file(run))
    by_phase = {str(t.get("plan_phase")).lower(): t for t in run["tasks"] if t.get("plan_phase")}
    by_task = {str(t.get("plan_task")).lower(): t for t in run["tasks"] if t.get("plan_task")}
    by_id = {str(t.get("id")).lower(): t for t in run["tasks"] if t.get("id")}

    def _route_str(route):
        # the plan's route string or dict -> the file-view string form "" or
        # "<agent> <model> [<effort>]"; a dict contributes its string form, never route_json
        if route in (None, ""):
            return ""
        if isinstance(route, dict):
            return " ".join(str(route.get(k) or "") for k in ("agent", "model", "effort")
                            if route.get(k)).strip()
        return str(route)

    def _list(v):
        if v is None:
            return []
        return list(v) if isinstance(v, list) else [v]

    filled, unmatched, already_full, n_fields = {}, [], 0, 0
    items = []  # (kind, plan item, run task | None)
    for ph in cur.get("phases") or []:
        if not isinstance(ph, dict):
            continue
        pid = str(ph.get("id") or "")
        t = by_phase.get(pid.lower()) or by_id.get(pid.lower())
        items.append(("phase", ph, t))
        for c in ph.get("tasks") or []:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "")
            ct = by_task.get(cid.lower()) or by_id.get(cid.lower())
            items.append(("task", c, ct))
            for s in c.get("subtasks") or []:  # no plan_task link today: match by id only
                if not isinstance(s, dict):
                    continue
                sid = str(s.get("id") or "")
                items.append(("subtask", s, by_id.get(sid.lower())))
    for kind, src, t in items:
        if t is None:
            unmatched.append(src.get("id"))
            continue
        fields = []
        # a field is written only when the PLAN carries a value for it: an empty plan
        # acceptance/refs/owned/deps/route fills nothing (and counts nothing), so the reply
        # names the fields that actually moved and a second run finds no work left
        if kind in ("task", "subtask") and not t.get("acceptance") and (src.get("acceptance") or ""):
            t["acceptance"] = src["acceptance"]
            fields.append("acceptance")
        if kind in ("task", "subtask") and not t.get("refs") and _list(src.get("refs")):
            t["refs"] = _list(src.get("refs"))
            fields.append("refs")
        if not t.get("owned") and _list(src.get("owned")):
            t["owned"] = _list(src.get("owned"))
            fields.append("owned")
        if not t.get("deps") and _list(src.get("deps")):
            t["deps"] = _list(src.get("deps"))
            fields.append("deps")
        if not t.get("type"):
            t["type"] = kind
            fields.append("type")
        if not t.get("route") and _route_str(src.get("route")):
            t["route"] = _route_str(src.get("route"))
            fields.append("route")
        if fields:
            filled[t["id"]] = fields
            n_fields += len(fields)
        else:
            already_full += 1
    st.event(run, "note", f"backfill-fields: {len(filled)} tasks filled ({n_fields} fields)"
             + (f", {len(unmatched)} unmatched" if unmatched else ""), by=by)
    st.save(run)
    st.flush()
    return {"ok": True, "run": run["id"], "plan": run["plan_id"], "filled": filled,
            **({"unmatched": unmatched} if unmatched else {}), "already_full": already_full}


def cmd_render(a, st):
    with st.locked():
        st.repair()
        return {"ok": True, "board": st.render(st.load(a.run))}


def cmd_project(a, st):
    """`project register --slug S --repo R [--name N] [--data D]` (L10.7): the wizard's way into the
    root projects table (it used to write <home>/projects.json itself). One upsert, nothing else."""
    register_project(a.slug, a.repo, st.data, name=a.name)
    return {"ok": True, "slug": a.slug, "repo": os.path.realpath(a.repo), "data_dir": os.path.abspath(st.data)}


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
    project = a.project or st.root_register()
    # Z8: a throwaway temp data dir on the default home projects nothing (root_register already
    # added no row); copied stays 0 and catch_up is not called.
    copied = 0 if st._skip_root_projection() else \
        st.db().catch_up(st.home_dir(), project, data_dir=st.project_folder())
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
    for name in ("init", "list", "get", "set", "stage", "archive", "backfill-fields"):
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
    t.add_argument("--root", action="store_true", help="create a top-level phase (no --parent)")
    t.add_argument("--followup", action="store_true",
                   help="--parent may be a merged/qa/done phase: it rolls back to building")
    t = task.add_parser("set", parents=[data_p, run_p],
                        help="edit one task: status agent model effort lane pane branch workspace "
                             "slot last_commit gates verdict reviewer parent title acceptance owned "
                             "deps refs (owned/deps/refs are comma lists; title/acceptance/owned/"
                             "deps/refs edit a planned|routed task only)")
    t.add_argument("task"), t.add_argument("pairs", nargs="+"), t.add_argument("--note")
    t.add_argument("--force", action="store_true")
    t = task.add_parser("round", parents=[data_p, run_p])
    t.add_argument("task", nargs="?"), t.add_argument("--kind"), t.add_argument("--cause")
    t.add_argument("--tasks")  # lane_group round: LEAD,T2,T3; the task arg is optional then
    t.add_argument("--undo", action="store_true")  # drop the latest (empty) round; kind checked in cmd
    t = task.add_parser("get", parents=[data_p, run_p])
    t.add_argument("task")
    t = task.add_parser("check", parents=[data_p, run_p])
    t.add_argument("task")
    t.add_argument("--at-dispatch", action="store_true",
                   help="also require a recorded agent (the route dispatch writes)")
    t = task.add_parser("verify", parents=[data_p, run_p])
    t.add_argument("task")
    t.add_argument("--tests", required=True, metavar="<passed>/<failed>",
                   help="passed/failed counts; a failed count larger than the passed count is refused")
    t.add_argument("--tsc", type=int)
    t.add_argument("--lint", type=int)
    t.add_argument("--selftest")  # '<run>/<failed>'
    t.add_argument("--fail")
    t = task.add_parser("brief", parents=[data_p, run_p],
                        help="store the round's brief text as an artifact: --file PATH or --stdin")
    t.add_argument("task")
    _b = t.add_mutually_exclusive_group(required=True)
    _b.add_argument("--file", metavar="PATH")
    _b.add_argument("--stdin", action="store_true")
    t = task.add_parser("export", parents=[data_p, run_p],
                        help="write brief/report/review round files BACK from the artifacts (all rounds)")
    t.add_argument("task")
    for t in task.choices.values():
        t.set_defaults(func=cmd_task)

    pn = sub.add_parser("plan").add_subparsers(dest="action", required=True)
    ps = pn.add_parser("sync", parents=[data_p, run_p],
                       help="diff the run's plan file against its tasks; dry run unless --apply; "
                            "refuses on a live run unless --from-file")
    ps.add_argument("--apply", action="store_true")  # default: dry run, nothing written
    ps.add_argument("--from-file", action="store_true",
                    help="the plan file wins: sync even while the run has non-terminal tasks")
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

    g = sub.add_parser("gate").add_subparsers(dest="action", required=True)
    gr = g.add_parser("run", parents=[data_p, run_p])
    gr.add_argument("task", nargs="?"), gr.add_argument("--ref")
    gr.add_argument("--final", action="store_true"), gr.add_argument("--due", action="store_true")
    gr.set_defaults(func=cmd_gate)
    gs = g.add_parser("signoff", parents=[data_p, run_p])
    gs.add_argument("task"), gs.add_argument("--ref")
    gs.set_defaults(func=cmd_gate_signoff)
    rc = sub.add_parser("reconcile", parents=[data_p, run_p])
    rc.add_argument("--phase")
    rc.set_defaults(func=cmd_reconcile)
    rk = sub.add_parser("risk").add_subparsers(dest="action", required=True)
    for name in ("add", "list", "clear"):
        x = rk.add_parser(name, parents=[data_p, run_p])
        x.add_argument("task", nargs="?")  # clear: the risk reason id
        x.add_argument("--kind"), x.add_argument("--points", type=int), x.add_argument("--source")
        x.add_argument("--evidence", type=int)
        x.set_defaults(func=cmd_risk)

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
    pc = sub.add_parser("project").add_subparsers(dest="action", required=True)
    pr = pc.add_parser("register", parents=[data_p])
    pr.add_argument("--slug", required=True), pr.add_argument("--repo", required=True), pr.add_argument("--name")
    pr.set_defaults(func=cmd_project)
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
                      "gates": db.get("gates", {}), "final_gates": db.get("final_gates", []),
                      "phases": db.get("phases", []), **extra}))
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

def d15_self_test(tmp, env, pd, penv):
    """D1.5 — run init copies the plan fields, the store mirror keeps them, the mandatory-field
    check lists what is missing, and `run backfill-fields` fills a stripped run from the plan.

    Own data dir (the fixture plan tool through $MK_PLAN_CMD, like the plan block above), so the
    run.json / store round trip is exercised end to end and every assertion reads a real file."""
    # Z3: the root projection lives in ONE real home (H) per suite, not a per-data-dir fake one.
    H = os.environ["MK_HOME"]
    plan = {"plan": "d15", "status": "approved", "runs": [], "phases": [
        {"id": "DP", "title": "Phase", "wave": 1, "deps": [], "owned": ["scripts/a.py"], "route": "",
         "reviewer": "", "size": "standard", "risk": "reversible", "review": "light", "qa": [],
         "commit": "", "subagents": False, "lane_group": "", "tasks": [
             {"id": "DP.1", "title": "Child", "acceptance": "child done", "refs": ["scripts/a.py:1"],
              "subagent": False, "owned": ["scripts/a.py"], "deps": [], "parallel": False,
              "subtasks": [{"id": "DP.1.1", "title": "Leaf", "acceptance": "leaf done", "refs": [],
                            "owned": ["scripts/a.py"], "deps": [], "parallel": False, "route": ""}]}]},
        {"id": "DQ", "title": "Other", "wave": 2, "deps": ["DP"], "owned": [], "route": "",
         "reviewer": "", "size": "standard", "risk": "reversible", "review": "light", "qa": [],
         "commit": "", "subagents": False, "lane_group": "", "tasks": [
             {"id": "DQ.1", "title": "Other child", "acceptance": "other done", "refs": [],
              "subagent": False, "owned": [], "deps": [], "parallel": False}]}]}
    pfile = os.path.join(tmp, "d15plan.json")
    json.dump(plan, open(pfile, "w"))

    def pk(*x, code=0):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *x, "--data", pd],
                           capture_output=True, text=True, env=penv)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (x, r.stdout, r.stderr)
        assert r.returncode == code, (x, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    # --- D1.5.1: run init --plan copies acceptance/refs/type (and the subtask becomes a row)
    out = pk("run", "init", "--id", "d1", "--feature", "D15", "--branch", "b", "--plan", pfile)
    tasks = {t["id"]: t for t in out["run"]["tasks"]}
    assert set(tasks) == {"DP", "DP.1", "DP.1.1", "DQ", "DQ.1"}, sorted(tasks)
    assert tasks["DP"]["type"] == "phase" and "acceptance" not in tasks["DP"], tasks["DP"]
    assert tasks["DP.1"]["type"] == "task" and tasks["DP.1"]["acceptance"] == "child done", tasks["DP.1"]
    assert tasks["DP.1"]["refs"] == ["scripts/a.py:1"] and tasks["DP.1"]["owned"] == ["scripts/a.py"], tasks["DP.1"]
    assert tasks["DP.1.1"]["type"] == "subtask" and tasks["DP.1.1"]["parent"] == "DP.1", tasks["DP.1.1"]
    assert tasks["DP.1.1"]["acceptance"] == "leaf done", tasks["DP.1.1"]
    assert tasks["DQ"]["deps"] == ["DP"] and tasks["DQ"]["type"] == "phase", tasks["DQ"]
    # task check reads the same fields: the subtask has everything; a phase needs no acceptance
    assert pk("task", "check", "DP.1.1", "--run", "d1")["ok"] is True
    assert pk("task", "check", "DP", "--run", "d1")["ok"] is True

    # --- D1.5.4: task check lists the missing mandatory fields
    pk("task", "add", "--root", "--id", "DBAD", "--title", "", "--run", "d1", code=2)  # title is mandatory at add
    pk("task", "add", "--id", "DBAD", "--title", "no acceptance", "--parent", "DP", "--run", "d1")
    chk = pk("task", "check", "DBAD", "--run", "d1")
    assert chk["ok"] is False and "acceptance" in chk["missing"], chk
    assert "title" not in chk["missing"] and chk["type"] == "task", chk  # a child of a phase is a task
    chk = pk("task", "check", "DBAD", "--run", "d1", "--at-dispatch")
    assert chk["ok"] is False and "agent" in chk["missing"] and chk["at_dispatch"] is True, chk
    pk("task", "set", "DBAD", "agent=opencode")
    assert pk("task", "check", "DBAD", "--run", "d1", "--at-dispatch")["missing"] == ["acceptance"]
    # a briefed task must own files (the check reads the brief-rN.md on disk)
    pk("task", "add", "--id", "DCH", "--title", "child", "--parent", "DP", "--run", "d1")
    chk = pk("task", "check", "DCH", "--run", "d1")
    assert chk["ok"] is False and chk["type"] == "task" and "acceptance" in chk["missing"], chk
    assert "owned" not in chk["missing"], chk  # no brief yet -> owned is not required
    os.makedirs(os.path.join(pd, "runs", "d1", "tasks", "DCH"), exist_ok=True)
    open(os.path.join(pd, "runs", "d1", "tasks", "DCH", "brief-r1.md"), "w").write("# b\n")
    assert "owned" in pk("task", "check", "DCH", "--run", "d1")["missing"]  # briefed -> owned
    # a parentless row is a phase: no acceptance and no parent are required of it
    pk("task", "add", "--root", "--id", "DPH", "--title", "phase-ish", "--run", "d1")
    assert pk("task", "check", "DPH", "--run", "d1") == {"ok": True, "task": "DPH", "type": "phase",
                                                        "missing": []}
    assert "no task" in pk("task", "check", "NOPE", "--run", "d1", code=2)["error"]

    # --- D1.5.5: backfill-fields fills a stripped run from the plan, twice is a no-op
    def strip_run(run_id):
        """L4: no run.json. Blank the plan fields on the tasks rows (and refs/route extras in
        tasks.extra_json), exactly the file view a legacy run had before backfill."""
        con = _mkdb().open_project(H, STORE_PROJECT_FALLBACK, data_dir=pd)
        try:
            for tid in [r["id"] for r in con.execute("SELECT id FROM tasks WHERE run=?", (run_id,))]:
                extra = _json_obj(con.execute("SELECT extra_json FROM tasks WHERE run=? AND id=?",
                                              (run_id, tid)).fetchone()["extra_json"])
                for k in ("refs", "route", "type"):
                    extra.pop(k, None)
                con.execute("UPDATE tasks SET acceptance='', owned='', deps='', route_json=NULL,"
                            " extra_json=? WHERE run=? AND id=?",
                            (extras_json(extra), run_id, tid))
            con.commit()
        finally:
            con.close()

    def task_rows(run_id):
        con = _mkdb().open_project(H, STORE_PROJECT_FALLBACK, data_dir=pd)
        try:
            out = {}
            for r in con.execute("SELECT id, type, acceptance, owned, deps, extra_json FROM tasks"
                                 " WHERE run=?", (run_id,)):
                extra = _json_obj(r["extra_json"])
                out[r["id"]] = {"type": r["type"], "acceptance": r["acceptance"],
                                "owned": [x for x in (r["owned"] or "").split(",") if x],
                                "deps": [x for x in (r["deps"] or "").split(",") if x],
                                "refs": extra.get("refs"), "route": extra.get("route")}
            return out
        finally:
            con.close()

    strip_run("d1")
    bf = pk("run", "backfill-fields", "--run", "d1")
    assert bf["ok"] and "DP.1" in bf["filled"], bf
    bt = task_rows("d1")
    assert bt["DP.1"]["acceptance"] == "child done" and bt["DP.1"]["type"] == "task", bt["DP.1"]
    assert bt["DP.1.1"]["type"] == "subtask" and bt["DP"]["type"] == "phase", bt["DP.1.1"]
    assert bt["DP"]["owned"] == ["scripts/a.py"] and bt["DP"]["deps"] == [], bt["DP"]  # DQ dep is on DQ
    assert bt["DQ"]["deps"] == ["DP"] and bt["DQ"]["owned"] == [], bt["DQ"]
    again = pk("run", "backfill-fields", "--run", "d1")
    assert again["filled"] == {} and again["already_full"] >= 5, again  # idempotent
    # a plan-less run is refused
    pk("run", "init", "--id", "d2", "--feature", "no plan", "--branch", "b")
    e = pk("run", "backfill-fields", "--run", "d2", code=2)
    assert "no plan link" in e["error"], e

    # --- D1.5.2 (store): run init --plan into a store; the rows keep the fields after three
    # unrelated task-set writes, and the phase/subtask types land as phase/task/subtask.
    sd = os.path.join(tmp, "d15store")

    def sk(*x, code=0):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *x, "--data", sd],
                           capture_output=True, text=True, env=penv)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1 and r.returncode == code, (x, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    sk("run", "init", "--id", "s", "--feature", "S", "--branch", "b")
    sk("task", "add", "--root", "--id", "SX", "--title", "x")
    sk("store", "enable")
    json.dump(plan, open(pfile, "w"))  # the fixture plan tool set it `running` above: re-seed
    sk("run", "init", "--id", "s2", "--feature", "Store plan", "--branch", "b2", "--plan", pfile)
    for i in range(3):  # three unrelated writes: none carries the plan fields
        sk("task", "set", "DP.1", "pane=w%d:p" % i, "--run", "s2")
    con = _mkdb().open_project(H, STORE_PROJECT_FALLBACK, data_dir=sd)
    try:
        rows = {r["id"]: r for r in con.execute(
            "SELECT id, type, acceptance, owned, deps FROM tasks WHERE run='s2'")}
    finally:
        con.close()
    assert rows["DP"]["type"] == "phase", dict(rows["DP"])
    assert rows["DP.1"]["type"] == "task" and rows["DP.1"]["acceptance"] == "child done", dict(rows["DP.1"])
    assert rows["DP.1.1"]["type"] == "subtask", dict(rows["DP.1.1"])
    assert rows["DP.1"]["owned"] == "scripts/a.py", dict(rows["DP.1"])
    assert rows["DQ"]["deps"] == "DP", dict(rows["DQ"])

    # --- T1: task set edits reach the store row and task get (the plan text is not file-only)
    sk("task", "set", "DP.1", "title=Edited child", "acceptance=edited outcome",
       "owned=scripts/b.py,scripts/c.py", "deps=DP", "--run", "s2")
    con = _mkdb().open_project(H, STORE_PROJECT_FALLBACK, data_dir=sd)
    try:
        row = con.execute("SELECT title, acceptance, owned, deps FROM tasks"
                          " WHERE run='s2' AND id='DP.1'").fetchone()
    finally:
        con.close()
    assert row["title"] == "Edited child" and row["acceptance"] == "edited outcome", dict(row)
    assert row["owned"] == "scripts/b.py,scripts/c.py" and row["deps"] == "DP", dict(row)
    got = sk("task", "get", "DP.1", "--run", "s2")["task"]
    assert got["title"] == "Edited child" and got["acceptance"] == "edited outcome", got
    assert got["owned"] == ["scripts/b.py", "scripts/c.py"] and got["deps"] == ["DP"], got

    # --- F.2 (later round): run init / plan sync copy the gate contract; task set cannot touch it
    def f2_task(tid, **gate_fields):
        return {"id": tid, "title": tid, "acceptance": "a", "refs": [], "subagent": False, "owned": [],
                "deps": [], "parallel": False, "acceptance_lines": [{"id": "A1", "text": "a"}],
                "gate": [], "manual": "", "critical": False, "risk": 1, **gate_fields}

    def f2_plan(plan_id, tasks, final_gates=()):
        return {"plan": plan_id, "status": "approved", "runs": [], "final_gates": list(final_gates),
                "phases": [{"id": "FP", "title": "Phase", "wave": 1, "deps": [], "owned": [], "route": "",
                            "reviewer": "", "size": "standard", "risk": "reversible", "review": "light",
                            "qa": [], "commit": "", "subagents": False, "lane_group": "", "tasks": tasks}]}

    def run_extra(run_id):
        con = _mkdb().open_project(H, STORE_PROJECT_FALLBACK, data_dir=pd)
        try:
            return _json_obj(con.execute("SELECT extra_json FROM runs WHERE id=?", (run_id,)).fetchone()["extra_json"])
        finally:
            con.close()

    gate = {"cmd": "true", "expect": 0, "timeout": 30, "lines": ["A1"]}
    fg = {"id": "FG1", "cmd": "true", "expect": 0, "every": "merge", "timeout": 600, "needs": [],
          "covers": ["FP.1"]}
    f2file = os.path.join(tmp, "f2plan.json")
    json.dump(f2_plan("f2gate", [f2_task("FP.1", gate=[gate], critical=True, risk=4),
                                 f2_task("FP.2", manual="read it by eye")], [fg]), open(f2file, "w"))
    out = pk("run", "init", "--id", "f2g", "--feature", "F2", "--branch", "b", "--plan", f2file)
    ft = {t["id"]: t for t in out["run"]["tasks"]}
    assert ft["FP.1"]["gate"] == [gate] and ft["FP.1"]["critical"] is True, ft["FP.1"]
    assert ft["FP.1"]["base_risk"] == 4 and ft["FP.1"]["manual"] == "", ft["FP.1"]
    assert ft["FP.1"]["acceptance_lines"] == [{"id": "A1", "text": "a"}], ft["FP.1"]
    assert ft["FP.2"]["manual"] == "read it by eye" and ft["FP.2"]["gate"] == [], ft["FP.2"]
    assert out["run"]["fidelity"] is True and out["run"]["final_gates"] == [fg], out["run"]
    # the store keeps all of it: runs.extra_json.fidelity + final_gates, tasks.extra_json per task
    rx = run_extra("f2g")
    assert rx.get("fidelity") is True and rx.get("final_gates") == [fg], rx
    stored = pk("task", "get", "FP.1", "--run", "f2g")["task"]
    assert stored["gate"] == [gate] and stored["base_risk"] == 4 and stored["critical"] is True, stored
    # the stored gate is the table shape the F.0 runner executes (one runner), a manual line is a
    # plain string that run_gates never runs
    assert _gate_run(stored["gate"][0], tmp)[0] == "pass"
    assert not isinstance(ft["FP.2"]["manual"], dict)
    # task set refuses every gate field, clearly, and writes nothing
    for pair in ("risk=2", "gate=x", "manual=x", "critical=false", "base_risk=1", "acceptance_lines=x", "risk_note=x"):
        e = pk("task", "set", "FP.1", pair, "--run", "f2g", code=2)
        assert e["ok"] is False and "read-only" in e["error"], (pair, e)
    after = pk("task", "get", "FP.1", "--run", "f2g")["task"]
    assert after["base_risk"] == 4 and after["critical"] is True and after["gate"] == [gate], after
    pk("task", "set", "FP.1", "gates=test=pass", "--run", "f2g")  # the existing `gates` result key still sets
    # a pre-F plan (all defaults) sets no fidelity flag and no gate keys
    pre = os.path.join(tmp, "f2pre.json")
    json.dump(f2_plan("f2pre", [f2_task("FP.1")]), open(pre, "w"))
    out = pk("run", "init", "--id", "f2p", "--feature", "pre", "--branch", "b", "--plan", pre)
    pt = {t["id"]: t for t in out["run"]["tasks"]}
    assert "fidelity" not in out["run"] and "final_gates" not in out["run"], out["run"]
    assert "fidelity" not in run_extra("f2p"), run_extra("f2p")
    assert not ({"gate", "manual", "critical", "base_risk", "acceptance_lines"} & set(pt["FP.1"])), pt["FP.1"]
    # plan sync --apply: the plan grows the contract; the run learns it and the new task carries it
    json.dump(f2_plan("f2pre", [f2_task("FP.1"), f2_task("FP.3", gate=[gate], risk=3)], [fg]), open(pre, "w"))
    out = pk("plan", "sync", "--run", "f2p", "--apply", "--from-file")
    assert out["ok"] is True, out
    rx = run_extra("f2p")
    assert rx.get("fidelity") is True and rx.get("final_gates") == [fg], rx
    st3 = pk("task", "get", "FP.3", "--run", "f2p")["task"]
    assert st3["gate"] == [gate] and st3["base_risk"] == 3, st3


def self_test():
    import shutil, subprocess
    tmp = tempfile.mkdtemp(prefix="mk-state-test-")
    tmp_home = os.path.join(tmp, "home")
    os.environ["MK_HOME"] = tmp_home
    os.environ["MATRIXKODER_HOME"] = tmp_home
    os.environ["MK_SELFTEST"] = "1"
    # Z3: every fixture root projection is keyed by this one slug, so the assertions below keep
    # the spelling they had under the old <data>/.mk-home. It also tells mk-state NOT to register
    # a root row for a temp data dir (a self-test must add none).
    os.environ["MK_SELFTEST_FALLBACK_SLUG"] = STORE_PROJECT_FALLBACK
    H = tmp_home  # the ONE real home the root projection lands in
    env = dict(os.environ, MATRIXKODER_HOME=tmp_home, MK_HOME=tmp_home, MK_SELFTEST="1",
               MK_SELFTEST_FALLBACK_SLUG=STORE_PROJECT_FALLBACK)
    env.pop("MATRIXKODER_DATA", None)
    data = os.path.join(tmp, "data")

    def mk(*args, code=0, cwd=None, e=None, data_arg=True):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, *(["--data", data] if data_arg else [])],
                           capture_output=True, text=True, env=e or env, cwd=cwd or tmp)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    # L4: the project DB is the only source of truth. Every former file view reads from it.
    DB = _mkdb()

    def q(datadir, sql, args=()):
        """One query against a data dir's project DB; None-safe helpers over it."""
        con = DB.open_project(H, STORE_PROJECT_FALLBACK, data_dir=datadir)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def q1(datadir, sql, args=()):
        rows = q(datadir, sql, args)
        return rows[0] if rows else None

    def events_of(datadir, rid):
        """The run's state events, as the events table holds them (was events.jsonl)."""
        con = DB.open_project(H, STORE_PROJECT_FALLBACK, data_dir=datadir)
        try:
            rows = con.execute(
                "SELECT id, run, task, t, chan, kind, from_id, to_id, reply_to, text, meta_json"
                " FROM events WHERE run=? ORDER BY id", (rid,)).fetchall()
        finally:
            con.close()
        out = []
        for row in rows:
            meta = {}
            if row["meta_json"]:
                with contextlib.suppress(ValueError):
                    meta = json.loads(row["meta_json"]) or {}
            if isinstance(meta, dict) and isinstance(meta.get("line"), str):
                out.append(json.loads(meta["line"]))
            else:
                out.append({"t": row["t"], "run": row["run"], "task": row["task"],
                            "type": row["kind"], "text": row["text"], "from": row["from_id"],
                            "to": row["to_id"],
                            "by": (meta.get("by") if isinstance(meta, dict) else None) or "orchestrator"})
        return out

    def n_events(datadir, rid):
        return q1(datadir, "SELECT COUNT(*) c FROM events WHERE run=?", (rid,))["c"]

    def close_all(datadir, rid, e=None):
        """F.0: put every OPEN task of a run at `done` through the real task-set path, so a
        `run set --status done` clears the open-work gate. -> the ids it moved."""
        moved = []
        for r in q(datadir, "SELECT id, status FROM tasks WHERE run=? ORDER BY rowid", (rid,)):
            if r["status"] in CLOSED_STATUSES:
                continue
            mk("task", "set", r["id"], "status=done", "--run", rid, "--data", datadir,
               data_arg=False, e=e or env)
            moved.append(r["id"])
        return moved

    try:
        # Z8: a throwaway data dir under the system temp dir must not register a root row in the
        # DEFAULT home. root_register inserts a projects row on the write path (store_commit); that
        # is right for a real project and for an explicit MK_HOME, and wrong for a temp data dir on
        # the default home, where home_dir() is the user's real ~/.matrixkoder. The harness pins
        # MK_SELFTEST_FALLBACK_SLUG (a different skip), so these cases drop that pin.
        import sqlite3 as _z8sq

        def _root_counts(home_dir):
            dbp = os.path.join(home_dir, "mk.db")
            if not os.path.exists(dbp):
                return 0, 0
            con = _z8sq.connect(dbp)
            try:
                nproj = con.execute("SELECT COUNT(*) c FROM projects").fetchone()[0]
                try:
                    nplan = con.execute("SELECT COUNT(*) c FROM plan_summary").fetchone()[0]
                except _z8sq.OperationalError:
                    nplan = 0
            finally:
                con.close()
            return nproj, nplan

        def _bare_env(home_dir):
            """Default-home env: HOME pinned, both home names and the fallback slug unset."""
            e = {k: v for k, v in env.items()
                 if k not in ("MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA",
                              "MK_SELFTEST_FALLBACK_SLUG")}
            e["HOME"] = home_dir
            e["MK_SELFTEST"] = "1"
            return e

        def _status_write(data_dir, run_env):
            """run init + task add + task set status=building. The status write reaches
            store_commit -> root_register (and plan_summary, once a plan exists)."""
            script = os.path.abspath(__file__)
            for args in (
                ["run", "init", "--id", "z8", "--feature", "Z", "--branch", "bz",
                 "--data", data_dir],
                ["task", "add", "--root", "--id", "Z1", "--title", "t", "--run", "z8", "--data", data_dir],
                ["task", "set", "Z1", "status=building", "--run", "z8", "--data", data_dir],
            ):
                r = subprocess.run([sys.executable, script, *args],
                                   capture_output=True, text=True, env=run_env)
                assert r.returncode == 0, (args, r.returncode, r.stdout, r.stderr)
                assert len(r.stdout.strip().splitlines()) == 1, (args, r.stdout, r.stderr)
            assert json.loads(r.stdout)["task"]["status"] == "building"

        # (a) temp data dir + default home: the status write succeeds, and the default home gains
        # neither a projects row nor a plan_summary row.
        a_home = os.path.join(tmp, "z8-default-home")
        a_data = os.path.join(tempfile.gettempdir(), "z8-data-" + os.path.basename(tmp))
        os.makedirs(a_home, exist_ok=True)
        os.makedirs(a_data, exist_ok=True)
        assert os.path.realpath(a_data).startswith(
            os.path.realpath(tempfile.gettempdir()) + os.sep)
        _status_write(a_data, _bare_env(a_home))
        a_root = os.path.join(a_home, ".matrixkoder")
        assert _root_counts(a_root) == (0, 0), _root_counts(a_root)
        assert os.path.exists(os.path.join(a_data, "mk.db")), "the project write itself must land"

        # Z9: the SAME store WITH a plans row. Z8 only proved the status write leaves no projects
        # row; the root projection mkdb rebuilds on that path is the C3 plan_summary of import_history
        # (mkdb.py:~1108), which Z8's guard does not cover — store_commit still called it directly.
        # The fix threads sync_root=not Store._skip_root_projection() through import_history, so the
        # (a) case now holds for the root plan_summary too, and the (b) case keeps every other
        # caller's behaviour byte for byte.
        def _plan_dir(data_dir):
            """The project store mkpaths picks for a data dir (its own folder until migrated)."""
            return os.path.join(data_dir, "mk.db")

        def _z9(which):
            h = os.path.join(tmp, "z9-default-home-" + which)
            d = os.path.join(tempfile.gettempdir(),
                             "z9-data-" + which + "-" + os.path.basename(tmp))
            os.makedirs(h, exist_ok=True)
            os.makedirs(d, exist_ok=True)
            return h, d

        def _plan_in_store(data_dir, plan_id):
            """One plans row in the project store — the C3 state a tool such as mk-plan leaves."""
            script_local = os.path.abspath(__file__)
            code = (
                "import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('mkstate_plan', %r)\n"
                "m = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(m)\n"
                "st = m.Store(sys.argv[1])\n"
                "m.plan_store_write(st, %r, 1, 'Z9 plan', {'plan': %r, 'status': 'approved'},\n"
                "                 '# Z9 plan\\n', None)\n"
                "con = st.store_open()\n"
                "print(con.execute('SELECT COUNT(*) c FROM plans').fetchone()['c'])\n"
            ) % (script_local, plan_id, plan_id)
            r = subprocess.run([sys.executable, "-c", code, data_dir],
                               capture_output=True, text=True)
            assert r.returncode == 0, ("plan write failed", r.returncode, r.stderr[-400:])
            assert r.stdout.strip() == "1", ("plans rows", r.stdout, r.stderr[-400:])

        # Z9 (a): temp data dir + default home + A PLAN. The status write must land in the project
        # store and leave the default home's plan_summary empty.
        z9a_home, z9a_data = _z9("a")
        _plan_in_store(z9a_data, "z9plan")
        _status_write(z9a_data, _bare_env(z9a_home))
        z9a_root = os.path.join(z9a_home, ".matrixkoder")
        assert _root_counts(z9a_root) == (0, 0), (
            "a temp data dir with a plan wrote into the default home: " + repr(_root_counts(z9a_root)))
        z9a_con = _z8sq.connect(_plan_dir(z9a_data))
        try:
            z9a_events = z9a_con.execute("SELECT COUNT(*) c FROM events WHERE kind='transition'"
                                         " AND to_id='building'").fetchone()[0]
        finally:
            z9a_con.close()
        assert z9a_events == 1, "the status write itself was lost (events=%s)" % z9a_events

        # Z9 (b): the identical setup with an explicit MK_HOME registers as today: the root row and
        # the plan_summary rows for the plan are both written.
        z9b_home, z9b_data = _z9("b")
        z9b_env = _bare_env(z9b_home)
        z9b_env["MK_HOME"] = z9b_home
        z9b_env["MATRIXKODER_HOME"] = z9b_home
        _plan_in_store(z9b_data, "z9plan")
        _status_write(z9b_data, z9b_env)
        assert _root_counts(z9b_home)[0] == 1, _root_counts(z9b_home)
        assert _root_counts(z9b_home)[1] >= 1, (
            "an explicit MK_HOME lost the root plan_summary written today: " + repr(_root_counts(z9b_home)))

        # (b) temp data dir + explicit MK_HOME: registers as today (one projects row; a plan-less
        # write never writes a plan_summary row).
        b_home = os.path.join(tmp, "z8-explicit-home")
        b_data = os.path.join(tempfile.gettempdir(), "z8-data-b-" + os.path.basename(tmp))
        os.makedirs(b_home, exist_ok=True)
        os.makedirs(b_data, exist_ok=True)
        b_env = _bare_env(os.path.join(tmp, "z8-unused-home"))
        b_env["MK_HOME"] = b_home
        b_env["MATRIXKODER_HOME"] = b_home
        _status_write(b_data, b_env)
        assert _root_counts(b_home)[0] == 1, _root_counts(b_home)
        assert _root_counts(b_home)[1] == 0, "a plan-less write must not invent a plan_summary row"

        # (c) non-temp data dir + default home: registers as today. A temp HOME is inside the
        # system temp dir on macOS, so a real outside-temp data dir cannot be paired with a default
        # home MK_SELFTEST accepts. Drive the predicate with a fake temp root: the data dir is not
        # under it, the home is still the default (~/.matrixkoder of HOME).
        saved_tmp = {k: os.environ.get(k) for k in ("TMPDIR", "TEMP", "TMP")}
        fake_tmp = os.path.join(tmp, "z8-fake-temp")
        os.makedirs(fake_tmp, exist_ok=True)
        c_home = os.path.join(tmp, "z8-default-home-c")
        c_data = os.path.join(tmp, "z8-outside-temp")  # under the real temp dir, not the fake one
        os.makedirs(c_home, exist_ok=True)
        os.makedirs(c_data, exist_ok=True)
        c_env = _bare_env(c_home)
        # the MK_SELFTEST guard refuses any write outside the (faked) temp root; case (c) writes
        # to a temp HOME on purpose, so the guard is not needed here.
        c_env.pop("MK_SELFTEST", None)
        c_env["TMPDIR"] = c_env["TEMP"] = c_env["TMP"] = fake_tmp
        for k in ("TMPDIR", "TEMP", "TMP"):
            os.environ[k] = fake_tmp
        try:
            assert not os.path.realpath(c_data).startswith(os.path.realpath(fake_tmp) + os.sep)
            _status_write(c_data, c_env)
        finally:
            for k, v in saved_tmp.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        assert _root_counts(os.path.join(c_home, ".matrixkoder"))[0] == 1, \
            _root_counts(os.path.join(c_home, ".matrixkoder"))

        rdir = os.path.join(data, "runs", "r1")
        # run init --plan now runs the plan tool (check/approved) — plans/f.md does not exist and
        # no MK_PLAN_CMD is set here, so r1 is a plan-less run; plan links get their own block below
        out = mk("run", "init", "--id", "r1", "--feature", "Feat", "--branch", "feat/x")
        assert out["ok"] and out["run"]["status"] == "active"
        # L4: the DB is the record; no run.json / events.jsonl / board.md is written
        assert q1(data, "SELECT 1 x FROM runs WHERE id='r1'"), "no runs row for r1"
        assert n_events(data, "r1") >= 1, "no events rows for r1"
        for f in ("run.json", "events.jsonl", "board.md", "index.json"):
            assert not os.path.exists(os.path.join(rdir, f)), f
        assert not os.path.exists(os.path.join(data, "runs", "index.json"))
        assert mk("run", "init", "--id", "r1", "--feature", "F", "--branch", "b", code=2)["ok"] is False
        assert mk("run", "init", "--id", "../evil", "--feature", "F", "--branch", "b", code=2)["ok"] is False
        assert [r["id"] for r in mk("run", "list")["runs"]] == ["r1"]

        mk("task", "add", "--root", "--id", "T1", "--title", "State core", "--wave", "1")
        t2 = mk("task", "add", "--root", "--id", "T2", "--title", "Usage", "--wave", "2", "--deps", "T1", "--owned", "a/*,b.py")["task"]
        assert t2["deps"] == ["T1"] and t2["owned"] == ["a/*", "b.py"] and t2["status"] == "planned"
        assert mk("task", "add", "--root", "--id", "T1", "--title", "dup", code=2)["ok"] is False
        # F.11 lint: a task needs --parent or --root; the refusal names the fix; both together are refused
        err = mk("task", "add", "--id", "L1", "--title", "orphan", code=2)["error"]
        assert "--parent" in err and "--root" in err, err
        assert "exclude" in mk("task", "add", "--id", "L1", "--title", "both", "--parent", "T1", "--root", code=2)["error"]
        assert t2["parent"] is None  # --root works (T1/T2 above); --parent works (the DP/DCH and Z cases below)
        assert q1(data, "SELECT 1 x FROM tasks WHERE run='r1' AND id='T2'"), "no T2 row"

        assert "bad status" in mk("task", "set", "T1", "status=bogus", code=2)["error"]
        assert mk("task", "set", "T1", "colour=red", code=2)["ok"] is False
        t1 = mk("task", "set", "T1", "status=routed", "agent=codex", "model=gpt-5", "effort=high",
                'gates={"test":"pass"}', "--by", "lane-watch")["task"]
        assert t1["status"] == "routed" and t1["gates"] == {"test": "pass"}
        # L4: the store is always on; a round bump never touches status or verdict
        before = mk("task", "get", "T1")["task"]
        r1 = mk("task", "round", "T1", "--kind", "build")
        assert r1["round"] == 1 and r1["brief"].endswith(os.path.join("tasks", "T1", "brief-r1.md"))
        after = mk("task", "get", "T1")["task"]
        assert after["status"] == before["status"] == "routed" and after["verdict"] == before["verdict"], after
        assert os.path.exists(os.path.join(data, "mk.db")), "the store is always on"
        assert q1(data, "SELECT round FROM tasks WHERE run='r1' AND id='T1'")["round"] == 1
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
        os.makedirs(os.path.dirname(brief3), exist_ok=True)  # L4: no task.json to create the dir
        open(brief3, "w").write("# brief\n")
        e = mk("task", "round", "T1", "--undo", code=2)
        assert e["ok"] is False and e["files"] == [brief3] and brief3 in e["error"], e
        assert mk("task", "get", "T1")["task"]["round"] == 3  # refused: the round stays
        os.unlink(brief3)
        n_ev = n_events(data, "r1")
        out = mk("task", "round", "T1", "--undo")
        assert out["ok"] and out["round"] == 2 and out["undone"] == 3, out
        evs = events_of(data, "r1")
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
        board = mk("render")["board"]   # L4: render returns the board text, no board.md
        assert "mk/r1/t1 · slot 1 · mk:repo:1" in board, board  # the lane cell names the slot
        for k in ("branch", "workspace", "slot"):
            assert mk("task", "set", "T1", f"{k}=")["task"][k] is None  # empty clears, like lane=

        # T1: task set edits the plan text of a task that has not started. Own data dir: the r1
        # fixture above must stay exactly T1/T2 for the blocks below.
        td = os.path.join(tmp, "t1set")

        def tk(*x, code=0):
            r = subprocess.run([sys.executable, os.path.abspath(__file__), *x, "--data", td],
                               capture_output=True, text=True, env=env)
            lines = r.stdout.strip().splitlines()
            assert len(lines) == 1 and r.returncode == code, (x, r.returncode, r.stdout, r.stderr)
            return json.loads(lines[0])

        tk("run", "init", "--id", "r1", "--feature", "T1", "--branch", "b")
        tk("task", "add", "--root", "--id", "T1", "--title", "Base")
        assert {"title", "acceptance", "owned", "deps", "refs"} <= set(TASK_KEYS)
        tk("task", "add", "--root", "--id", "E1", "--title", "Old title", "--owned", "a.py")
        e1 = tk("task", "set", "E1", "title=New title", "acceptance=the outcome",
                "owned=src/x.py,src/y.py", "refs=docs/a.md:10,scripts/b.py", "--note", "brief gap")
        assert e1["task"]["title"] == "New title" and e1["task"]["acceptance"] == "the outcome"
        assert e1["task"]["owned"] == ["src/x.py", "src/y.py"], e1["task"]
        assert e1["task"]["refs"] == ["docs/a.md:10", "scripts/b.py"], e1["task"]
        got = tk("task", "get", "E1")["task"]  # the file view reads the new values back
        assert got["title"] == "New title" and got["owned"] == ["src/x.py", "src/y.py"]
        assert got["refs"] == ["docs/a.md:10", "scripts/b.py"] and got["acceptance"] == "the outcome"
        # one decision event per changed key, old and new (short), plus the --note
        evs = events_of(td, "r1")
        dec = [x for x in evs if x["type"] == "decision" and x.get("task") == "E1"]
        assert len(dec) == 4, dec
        assert "task set E1 title: Old title → New title" in dec[0]["text"], dec[0]
        assert "brief gap" in dec[0]["text"], dec[0]  # the --note rides the decision event
        assert "task set E1 owned:" in dec[2]["text"] and "task set E1 refs:" in dec[3]["text"], dec
        # deps are validated like task add: stored id case, unknown warned, self-dep refused
        w = tk("task", "set", "E1", "deps=t1,NOPE1")["warnings"]
        assert any("dep NOPE1 not found (yet)" in x for x in w), w
        assert tk("task", "get", "E1")["task"]["deps"] == ["T1", "NOPE1"]  # unknown stays, warned
        assert "cannot depend on itself" in tk("task", "set", "E1", "deps=E1", code=2)["error"]
        assert tk("task", "get", "E1")["task"]["deps"] == ["T1", "NOPE1"]  # the refusal wrote nothing
        assert tk("task", "set", "E1", "owned=")["task"]["owned"] is None  # empty clears, like lane=
        # a started task keeps its text: the key is dropped (nothing written), warned, exit 0
        tk("task", "set", "E1", "owned=a.py")
        tk("task", "set", "E1", "status=building", "--force")  # E1 depends on T1: unmet, forced
        out = tk("task", "set", "E1", "title=After start", "owned=z.py", "agent=codex")
        assert out["task"]["title"] == "New title" and out["task"]["owned"] == ["a.py"], out["task"]
        assert out["task"]["agent"] == "codex", out["task"]  # a non-text key still writes
        warns = out["warnings"]
        assert any("task E1 is building (started): title, owned changed; not updated" in x
                   for x in warns), warns
        assert tk("task", "get", "E1")["task"]["title"] == "New title"  # nothing was written
        # back to planned: the plan text is editable again
        tk("task", "set", "E1", "status=planned")
        assert tk("task", "set", "E1", "title=Editable again")["task"]["title"] == "Editable again"
        plain = mk("render")["board"]  # the lane cell is back to the bare path
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
        assert q1(data, "SELECT extra_json FROM runs WHERE id='r1'")["extra_json"].find("Merge now?") >= 0
        assert mk("wait", "clear")["waiting"] is None

        events = events_of(data, "r1")
        tr = [e for e in events if e["type"] == "transition" and e["task"] == "T1" and e["from"]]
        assert tr and tr[0]["from"] == "planned" and tr[0]["to"] == "routed" and tr[0]["by"] == "lane-watch"
        run = mk("run", "get")["run"]
        assert len(events) >= RECENT and run["recent"] == events[-RECENT:], (len(events), len(run["recent"]))
        assert [t["id"] for t in run["tasks"]] == ["T1", "T2"]
        t1row = q1(data, "SELECT * FROM tasks WHERE run='r1' AND id='T1'")
        assert t1row["id"] == run["tasks"][0]["id"] and run["tasks"][0]["title"] == t1row["title"]

        for i in range(6):  # push the log past the board's 15-event window
            mk("event", "--type", "note", "--text", f"filler {i}")
        assert mk("run", "get")["run"]["recent"][-1]["text"] == "filler 5"
        txt = mk("render")["board"]   # L4: the board text, not a path
        assert "| T1 |" in txt and "| T2 |" in txt and "route codex high" in txt
        assert len([x for x in txt.splitlines() if x.startswith("- ")]) == BOARD_EVENTS

        # default-run selection: ambiguous with two active runs, fine again after archive
        mk("run", "init", "--id", "r2", "--feature", "Other", "--branch", "b2")
        assert mk("task", "get", "T1")["task"]["id"] == "T1"  # only r1 owns T1
        assert mk("event", "--task", "T1", "--type", "note", "--text", "owner pick")["ok"]
        assert mk("run", "get", "--id", "r1")["run"]["recent"][-1]["text"] == "owner pick"
        mk("task", "add", "--root", "--id", "T1", "--title", "Clash", "--wave", "1", "--run", "r2")
        assert "active runs" in mk("task", "get", "T1", code=2)["error"]  # both own T1: still ambiguous
        assert "active runs" in mk("run", "get", code=2)["error"]
        assert mk("task", "get", "T1", "--run", "r1")["task"]["id"] == "T1"
        assert mk("run", "archive", "--id", "r2")["run"]["status"] == "archived"
        assert {r["id"]: r["status"] for r in mk("run", "list")["runs"]} == {"r1": "active", "r2": "archived"}
        assert mk("run", "archive", code=2)["ok"] is False
        assert mk("task", "get", "T1")["task"]["id"] == "T1"
        # F.0: a run with open work cannot close; the refusal names each open task, and
        # closing (merged/qa/done) lets it through
        o = mk("run", "set", "--status", "done", code=2)
        assert o["ok"] is False and "T1 (routed)" in o["error"] and "T2 (planned)" in o["error"], o
        assert [x["id"] for x in o["open"]] == ["T1", "T2"], o
        assert mk("run", "get")["run"]["status"] == "active"  # nothing was written
        mk("task", "set", "T1", "status=done")
        mk("task", "set", "T2", "status=done")
        assert mk("run", "set", "--status", "done")["run"]["ended"]
        assert mk("run", "set", "--id", "r1", "--status", "active")["run"]["ended"] is None  # reopen clears ended
        assert mk("run", "get", "r1")["run"]["id"] == "r1" and mk("run", "get", "--id", "r1")["run"]["id"] == "r1"

        # review r1: case-insensitive task ids, input validation, one JSON line on corrupt files
        assert "exists as T1" in mk("task", "add", "--root", "--id", "t1", "--title", "lower", code=2)["error"]
        assert mk("task", "get", "T1")["task"]["title"] == "State core"
        assert mk("event", "--type", "note", "--text", "  ", code=2)["ok"] is False
        assert mk("event", "--type", "transition", "--text", "x", code=2)["ok"] is False
        assert mk("run", "stage", "-1", code=2)["ok"] is False
        for ttl in ("nan", "inf", "-5"):
            assert mk("lock", "take", "x", "--reason", "r", "--ttl-min", ttl, code=2)["ok"] is False
        for f in ("run.json", "events.jsonl", "board.md", os.path.join("tasks", "T1", "task.json")):
            assert not os.path.exists(os.path.join(rdir, f)), f  # L4 writes no view file
        # a torn event row in the store never crashes render (was a torn events.jsonl line)
        con = DB.open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
        try:
            con.execute("INSERT INTO events(run, task, t, chan, kind, text, meta_json)"
                        " VALUES ('r1', NULL, 't', 'state', 'note', 'torn', '{\"line\": \"{\\\"t\\\": \\\"torn')")
            con.commit()
        finally:
            con.close()
        assert mk("render")["ok"]
        # a corrupt run doc: the runs row is intact, the run still loads one JSON line
        assert mk("run", "get", "--id", "r2")["run"]["id"] == "r2"

        # concurrent writers: flock serialises read-modify-write, no lost event or update
        procs = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "task", "set", "T2", f"agent=a{i}",
                                   "--note", f"race {i}", "--data", data], env=env, stdout=subprocess.PIPE, text=True)
                 for i in range(10)]
        assert all(p.wait() == 0 for p in procs)
        races = q1(data, "SELECT COUNT(*) c FROM events WHERE run='r1' AND text LIKE 'race %'")["c"]
        assert races == 10 and mk("task", "get", "T2")["task"]["agent"] in {f"a{i}" for i in range(10)}
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
        assert q1(os.path.join(repo, ".matrixkoder"), "SELECT 1 x FROM runs WHERE id='g1'"), "g1 not in the repo store"
        envd = dict(env, MATRIXKODER_DATA=os.path.join(tmp, "envdata"))
        mk("run", "init", "--id", "e1", "--feature", "F", "--branch", "b", data_arg=False, e=envd, cwd=repo)
        assert q1(os.path.join(tmp, "envdata"), "SELECT 1 x FROM runs WHERE id='e1'"), "e1 not in envdata"
        mk("run", "init", "--id", "d1", "--feature", "F", "--branch", "b", e=envd, cwd=repo)
        assert q1(data, "SELECT 1 x FROM runs WHERE id='d1'"), "d1 not in --data store"
        # a linked worktree (.git file + commondir) resolves to the main checkout's data dir
        wt_git = os.path.join(repo, ".git", "worktrees", "lane")
        os.makedirs(wt_git)
        open(os.path.join(wt_git, "commondir"), "w").write("../..\n")
        lane = os.path.join(tmp, "repo-worktree-lane")
        os.makedirs(lane)
        open(os.path.join(lane, ".git"), "w").write(f"gitdir: {wt_git}\n")
        mk("run", "init", "--id", "w1", "--feature", "F", "--branch", "b", data_arg=False, cwd=lane)
        assert q1(os.path.join(repo, ".matrixkoder"), "SELECT 1 x FROM runs WHERE id='w1'"), "w1 not in main store"
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
        assert q1(homedata, "SELECT 1 x FROM runs WHERE id='h1'"), "h1 not in homedata"
        assert not os.path.exists(os.path.join(repo2, ".matrixkoder"))
        mk("run", "init", "--id", "h2", "--feature", "F", "--branch", "b", data_arg=False, e=envd, cwd=repo2)
        assert q1(os.path.join(tmp, "envdata"), "SELECT 1 x FROM runs WHERE id='h2'"), "env did not beat projects.json"
        wt2 = os.path.join(repo2, ".git", "worktrees", "lane")
        os.makedirs(wt2)
        open(os.path.join(wt2, "commondir"), "w").write("../..\n")
        lane2 = os.path.join(tmp, "repo2-lane")
        os.makedirs(lane2)
        open(os.path.join(lane2, ".git"), "w").write(f"gitdir: {wt2}\n")
        mk("run", "init", "--id", "h3", "--feature", "F", "--branch", "b", data_arg=False, cwd=lane2)  # keyed by the MAIN checkout
        assert q1(homedata, "SELECT 1 x FROM runs WHERE id='h3'"), "h3 not keyed by the main checkout"
        # a dotfiles repo at $HOME is never project data
        fake_home = os.path.join(tmp, "fakehome")
        os.makedirs(os.path.join(fake_home, ".git"))
        os.makedirs(os.path.join(fake_home, "proj"))
        out = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "list"], cwd=os.path.join(fake_home, "proj"),
                             env=dict(env, HOME=fake_home), capture_output=True, text=True)
        assert out.returncode != 0 and "$HOME" in (out.stdout + out.stderr), out

        # global --data/--by/--run also parse before the subcommand (after: every mk above)
        mk("--data", data, "run", "init", "--id", "f1", "--feature", "F", "--branch", "b", data_arg=False)
        assert q1(data, "SELECT 1 x FROM runs WHERE id='f1'"), "f1 not in the --data store"
        front = mk("--data", data, "--by", "front", "--run", "r1", "event", "--type", "note",
                   "--text", "flags before the subcommand", data_arg=False)["event"]
        assert front["by"] == "front"  # subparser SUPPRESS keeps the earlier --by
        assert mk("--data", data, "--run", "r1", "task", "get", "T1", data_arg=False)["task"]["id"] == "T1"

        # deps, cross-run deps, parent roll-up, owned overlap — own data dir
        d2 = ["--data", os.path.join(tmp, "deps")]
        dk = lambda *x, code=0: mk(*x, *d2, code=code, data_arg=False)
        dk("run", "init", "--id", "ra", "--feature", "A", "--branch", "a")
        dk("run", "init", "--id", "rb", "--feature", "B", "--branch", "b")
        dk("task", "add", "--root", "--id", "R", "--title", "Reasoning", "--owned", "src/server/hermes-api.ts", "--run", "ra")
        dk("task", "add", "--root", "--id", "X", "--title", "Base", "--run", "rb")
        # deps normalise to the stored id case (ids are case-insensitive); a self-dep is refused
        assert dk("task", "add", "--root", "--id", "N1", "--title", "n", "--deps", "x", "--run", "rb")["task"]["deps"] == ["X"]
        assert dk("task", "add", "--root", "--id", "N2", "--title", "n", "--deps", "ra:r", "--run", "rb")["task"]["deps"] == ["ra:R"]
        assert dk("task", "add", "--root", "--id", "N3", "--title", "n", "--deps", "RA:R", "--run", "rb")["task"]["deps"] == ["ra:R"]
        for selfdep in ("Z", "z", "RB:Z", "rb:z"):
            assert dk("task", "add", "--root", "--id", "Z", "--title", "self", "--deps", selfdep,
                      "--run", "rb", code=2)["ok"] is False
        assert dk("task", "add", "--root", "--id", "S", "--title", "same-run dep", "--deps", "RA:R",
                  "--run", "ra")["task"]["deps"] == ["ra:R"]
        w = dk("task", "add", "--root", "--id", "A", "--title", "Archive", "--owned", "src/server/*.ts", "--run", "rb")
        assert any("ra:R" in x and "--deps ra:R" in x for x in w["warnings"]), w  # overlap across runs
        w = dk("task", "add", "--root", "--id", "A2", "--title", "Archive 2", "--owned", "src/server/*.ts",
               "--deps", "ra:R,X", "--run", "rb")
        assert not any("ra:R" in x for x in w.get("warnings", [])), w  # dep already declared: no warning
        assert "dep ra:Q not found" in dk("task", "add", "--root", "--id", "A3", "--title", "t", "--deps", "ra:Q",
                                           "--run", "rb")["warnings"][0]
        assert globs_overlap("src/a.ts", "src/*.ts") and not globs_overlap("src/a.ts", "src/b.ts")
        assert not globs_overlap("docs/*", "src/*")
        for a, b in (("src/**/*.ts", "src/a.ts"), ("**/*.ts", "a.ts"), ("src/server", "src/server/*.ts"),
                     ("src/", "src/a.ts"), ("./src/a.ts", "src/a.ts")):
            assert globs_overlap(a, b), (a, b)  # dirs, ** and ./ all cover their files
        # gate: unmet deps block dispatch, --force overrides
        ddeps = d2[1]
        e = dk("task", "set", "A2", "status=dispatched", "--run", "rb", code=2)
        assert "waits on ra:R, X" in e["error"] and e["unmet"] == ["ra:R", "X"], e
        board_a = dk("render", "--run", "ra")["board"]   # L4: board text, not a file
        assert "blocks rb:A2" in board_a, board_a  # shown on the dependency's board too
        dk("task", "set", "X", "status=merged", "--run", "rb")
        assert dk("task", "get", "A2", "--run", "rb")["task"]["ready"] is False  # ra:R still open
        dk("task", "set", "R", "status=merged", "--run", "ra")  # cross-run: flips rb:A2 ready
        assert dk("task", "get", "S", "--run", "ra")["task"]["ready"] is True  # same-run ra:R, in-memory
        t = dk("task", "get", "A2", "--run", "rb")["task"]
        assert t["ready"] is True, t
        rb = dk("run", "get", "--id", "rb")["run"]
        assert any(x["task"] == "A2" and "deps met" in x["text"] for x in rb["recent"])
        board_b = dk("render", "--run", "rb")["board"]
        assert "ra:R ✓" in board_b and "planned · ready" in board_b, board_b
        dk("task", "set", "A2", "status=dispatched", "--run", "rb")
        assert dk("task", "get", "A2", "--run", "rb")["task"]["ready"] is False
        dk("task", "set", "A3", "status=building", "--force", "--run", "rb")
        # parent roll-up: a side state wins, else building while any child is past routed and not
        # all are met, else the least advanced child; nested parents too
        dk("task", "add", "--root", "--id", "P", "--title", "Parent", "--run", "rb")
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
        board_b = dk("render", "--run", "rb")["board"].splitlines()
        rows = [x.split("|")[1].strip() for x in board_b if x.startswith("| ") and "Task" not in x]
        assert rows[-4:] == ["P", "↳ C1", "↳ C2", "↳ ↳ G"], rows

        # an ancestor's unmet deps gate the child's dispatch; a parent's status= fans out (F2.1)
        dk("task", "add", "--root", "--id", "P2", "--title", "Parent 2", "--deps", "A3", "--run", "rb")
        dk("task", "add", "--id", "C4", "--title", "Child 4", "--parent", "P2", "--run", "rb")
        e = dk("task", "set", "C4", "status=dispatched", "--run", "rb", code=2)
        assert "waits on A3" in e["error"] and e["unmet"] == ["A3"], e
        assert dk("task", "set", "C4", "status=dispatched", "--force", "--run", "rb")["ok"]
        # F2.1: status= on a parent fans out to every child (never written on the parent directly);
        # the dep gate applies per child — an unmet one refuses the whole fan-out before any write
        dk("task", "add", "--root", "--id", "PG", "--title", "Parent gated", "--deps", "A3", "--run", "rb")
        dk("task", "add", "--id", "CG", "--title", "Child gated", "--parent", "PG", "--run", "rb")
        e = dk("task", "set", "PG", "status=building", "--run", "rb", code=2)  # CG behind PG's dep A3
        assert "waits on A3" in e["error"] and e["unmet"] == ["A3"], e
        dk("task", "set", "P2", "status=review", "--run", "rb")  # review carries no gate: plain fan-out
        assert dk("task", "get", "C4", "--run", "rb")["task"]["status"] == "review"
        assert dk("task", "get", "P2", "--run", "rb")["task"]["status"] == "review"  # rolled up

        # --force past the dep gate: ONE decision event naming --by (default: orchestrator)
        dk("task", "add", "--root", "--id", "F1", "--title", "forced", "--deps", "A3", "--run", "rb")
        dk("task", "add", "--root", "--id", "F2", "--title", "forced too", "--deps", "A3", "--run", "rb")
        dec = lambda: [x for x in events_of(ddeps, "rb") if x["type"] == "decision"]
        n_dec = len(dec())
        e = dk("task", "set", "F1", "status=dispatched", "--run", "rb", code=2)  # unforced: still refused
        assert "waits on A3" in e["error"] and e["unmet"] == ["A3"] and len(dec()) == n_dec, e
        out = dk("task", "set", "F1", "status=dispatched", "--force", "--by", "user", "--run", "rb")
        assert out["ok"] and out["task"]["status"] == "dispatched", out
        d = dec()[n_dec:]
        assert len(d) == 1 and d[0]["by"] == "user" and d[0]["task"] == "F1", d  # exactly one decision
        assert d[0]["text"] == "forced by user: task set F1 status=dispatched (unmet: A3)", d
        assert any(x["type"] == "transition" and x["task"] == "F1" and x["to"] == "dispatched"
                   for x in events_of(ddeps, "rb"))  # the usual transition event stays
        dk("task", "set", "F2", "status=building", "--force", "--run", "rb")  # no --by → orchestrator
        d = dec()[n_dec:]
        assert len(d) == 2 and d[1]["by"] == "orchestrator", d
        assert d[1]["text"] == "forced by orchestrator: task set F2 status=building (unmet: A3)", d

        # R2-L2: a parent fan-out that is --force'd past the dep gate writes the unmet list into
        # the decision text (the old code missed the f-prefix and wrote the literal
        # "{', '.join(waiting)}" into the event). Parent has dep A3 (unmet); the forced
        # fan-out produces a decision event whose text names the unmet dep.
        dk("task", "add", "--root", "--id", "RFG", "--title", "r2-l2 fan-out gated", "--deps", "A3", "--run", "rb")
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
        dk("task", "add", "--root", "--id", "Q2", "--title", "q2", "--run", "rb")
        dk("task", "add", "--root", "--id", "D2", "--title", "waits on rb:Q2", "--deps", "rb:Q2", "--run", "ra")
        dk("task", "set", "Q2", "status=merged", "--run", "rb")
        assert dk("task", "get", "D2", "--run", "ra")["task"]["ready"] is True
        e = dk("task", "add", "--id", "Q2C", "--title", "child", "--parent", "Q2", "--run", "rb", code=2)
        assert "merged" in e["error"] and "rolls up" in e["error"], e
        assert dk("task", "get", "Q2", "--run", "rb")["task"]["status"] == "merged"
        assert dk("task", "get", "D2", "--run", "ra")["task"]["ready"] is True  # no stale ready
        dk("task", "add", "--root", "--id", "Q3", "--title", "q3", "--run", "rb")
        dk("task", "set", "Q3", "status=building", "--run", "rb")
        dk("task", "add", "--root", "--id", "D3", "--title", "waits on rb:Q3", "--deps", "rb:Q3", "--run", "ra")
        assert dk("task", "get", "D3", "--run", "ra")["task"]["ready"] is False
        dk("task", "add", "--id", "Q3C", "--title", "child", "--parent", "Q3", "--run", "rb")
        assert dk("task", "get", "Q3", "--run", "rb")["task"]["status"] == "planned"  # rolled back by the child
        assert dk("task", "get", "D3", "--run", "ra")["task"]["ready"] is False  # refreshed, still correct

        # F2: a follow-up under a merged phase, and `task set T parent=P` re-parenting
        dk("task", "add", "--root", "--id", "FU", "--title", "Follow-up phase", "--run", "rb")
        dk("task", "add", "--id", "FU.1", "--title", "first child", "--parent", "FU", "--run", "rb")
        dk("task", "set", "FU.1", "status=merged", "--run", "rb")
        assert dk("task", "get", "FU", "--run", "rb")["task"]["status"] == "merged"
        # (a) without --followup the merged phase still refuses the add, and nothing was written
        e = dk("task", "add", "--id", "FU.2", "--title", "follow-up", "--parent", "FU", "--run", "rb", code=2)
        assert "merged" in e["error"] and "rolls up" in e["error"], e
        assert dk("task", "get", "FU", "--run", "rb")["task"]["status"] == "merged"
        assert not any(x["id"] == "FU.2" for x in dk("run", "get", "--id", "rb")["run"]["tasks"])
        # (b) with --followup the task lands under the phase, which rolls back to building
        t = dk("task", "add", "--id", "FU.2", "--title", "follow-up", "--parent", "FU",
               "--followup", "--run", "rb")["task"]
        assert t["parent"] == "FU" and t["status"] == "planned", t
        assert dk("task", "get", "FU", "--run", "rb")["task"]["status"] == "building"
        fu_ev = events_of(ddeps, "rb")
        assert any(x["task"] == "FU" and "follow-up FU.2 added" in x["text"] for x in fu_ev), fu_ev[-3:]
        dk("task", "set", "FU.2", "status=merged", "--run", "rb")
        assert dk("task", "get", "FU", "--run", "rb")["task"]["status"] == "merged"  # rolled up again
        # --followup on a phase that is not merged is a plain --parent (no rollback, no note)
        dk("task", "add", "--id", "FU.3", "--title", "open child", "--parent", "Q3", "--followup", "--run", "rb")
        assert dk("task", "get", "Q3", "--run", "rb")["task"]["status"] == "planned"

        # (c) a root with children moves under a phase: T is re-typed and so is everything below
        dk("task", "add", "--root", "--id", "RP", "--title", "movable", "--run", "rb")
        dk("task", "add", "--id", "RP.1", "--title", "under it", "--parent", "RP", "--run", "rb")
        ty = lambda i: dk("task", "check", i, "--run", "rb")["type"]  # task_type_of, the store's own word
        stored = lambda: {x["id"]: x.get("type") for x in dk("run", "get", "--id", "rb")["run"]["tasks"]}
        assert (ty("RP"), ty("RP.1")) == ("phase", "task"), (ty("RP"), ty("RP.1"))
        moved = dk("task", "set", "RP", "parent=Q3", "--run", "rb")["task"]
        assert moved["parent"] == "Q3" and moved["type"] == "task", moved
        assert (ty("RP"), ty("RP.1")) == ("task", "subtask"), (ty("RP"), ty("RP.1"))
        assert (stored()["RP"], stored()["RP.1"]) == ("task", "subtask"), stored()  # re-derived, then stored
        # the new parent rolls up from its new child
        assert dk("task", "get", "Q3", "--run", "rb")["task"]["status"] == "planned"
        # (e) an empty value makes T a root again, i.e. a phase, and its children follow
        root = dk("task", "set", "RP", "parent=", "--run", "rb")["task"]
        assert root["parent"] is None and root["type"] == "phase", root
        assert (ty("RP"), ty("RP.1")) == ("phase", "task"), (ty("RP"), ty("RP.1"))
        # (d) a cycle (T's own child), an unknown parent and T itself are refused; nothing moves
        for bad, why in (("RP.1", "cycle"), ("NOPE", "no task"), ("RP", "own parent")):
            e = dk("task", "set", "RP", f"parent={bad}", "--run", "rb", code=2)
            assert e["ok"] is False and why in e["error"], (bad, e)
        assert dk("task", "get", "RP", "--run", "rb")["task"]["parent"] is None

        # owned overlap: a child's globs inside its parent's are the plan, not a collision
        overlap_refs = lambda w: [x.split("owned overlaps ", 1)[1].split(" ")[0]
                                  for x in w.get("warnings", []) if x.startswith("owned overlaps ")]
        dk("task", "add", "--root", "--id", "OW", "--title", "Owner", "--owned", "scripts/*.py", "--run", "rb")
        w = dk("task", "add", "--id", "OWC", "--title", "child", "--parent", "OW",
               "--owned", "scripts/mk-state.py", "--run", "rb")
        assert not overlap_refs(w), w
        dk("task", "add", "--id", "OWM", "--title", "mid", "--parent", "OW", "--run", "rb")
        w = dk("task", "add", "--id", "OWG", "--title", "grandchild", "--parent", "OWM",
               "--owned", "scripts/mk-state.py", "--run", "rb")
        assert not ({"OW", "OWM"} & set(overlap_refs(w))), w  # ancestors skipped...
        assert "OWC" in overlap_refs(w), w  # ...a cousin branch sharing the glob still warns
        dk("task", "add", "--root", "--id", "OWR", "--title", "same id, other run", "--owned", "scripts/*.py", "--run", "ra")
        w = dk("task", "add", "--root", "--id", "OWX", "--title", "unrelated", "--owned", "scripts/*.py", "--run", "rb")
        assert "ra:OWR" in overlap_refs(w), w  # cross-run overlap is still reported
        dk("task", "add", "--root", "--id", "OWS", "--title", "sibling", "--owned", "scripts/lane-*.py", "--run", "rb")
        w = dk("task", "add", "--root", "--id", "OWY", "--title", "another sibling", "--owned", "scripts/lane-*.py", "--run", "rb")
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
            dk("task", "add", "--root", "--id", pid, "--title", pid, "--run", "rb")
            dk("task", "add", "--id", f"{pid}C1", "--title", "c1", "--parent", pid, "--run", "rb")
            dk("task", "set", f"{pid}C1", f"status={s1}", "--run", "rb")
            dk("task", "add", "--id", f"{pid}C2", "--title", "c2", "--parent", pid, "--run", "rb")
            dk("task", "set", f"{pid}C2", f"status={s2}", "--run", "rb")
            assert dk("task", "get", pid, "--run", "rb")["task"]["status"] == want, (pid, want)

        # building + planned rolls up to building; an unknown child status never crashes the roll-up
        dk("task", "add", "--root", "--id", "P3", "--title", "p3", "--run", "rb")
        dk("task", "add", "--id", "K1", "--title", "k1", "--parent", "P3", "--run", "rb")
        dk("task", "add", "--id", "K2", "--title", "k2", "--parent", "P3", "--run", "rb")
        dk("task", "set", "K1", "status=building", "--run", "rb")
        got = dk("task", "get", "P3", "--run", "rb")["task"]["status"]
        assert got == "building", got
        # a status mk-state never wrote: written straight into the tasks row (was a run.json edit)
        con = DB.open_project(H, STORE_PROJECT_FALLBACK, data_dir=ddeps)
        try:
            con.execute("UPDATE tasks SET status='weird' WHERE run='rb' AND id='K2'")
            con.commit()
        finally:
            con.close()
        assert dk("task", "set", "K1", "status=verifying", "--run", "rb")["ok"]

        # an unreadable or malformed OTHER active run never breaks the healthy one — own data dir
        d3 = ["--data", os.path.join(tmp, "sick")]
        sk = lambda *x, code=0: mk(*x, *d3, code=code, data_arg=False)
        sk("run", "init", "--id", "good", "--feature", "G", "--branch", "g")
        sk("run", "init", "--id", "sick", "--feature", "S", "--branch", "s")
        sk("task", "add", "--root", "--id", "T", "--title", "t", "--run", "good")
        # L4: a sibling runs row is what load_store reads; a malformed one is a warning, not a crash
        ksick = os.path.join(tmp, "sick")
        con = DB.open_project(H, STORE_PROJECT_FALLBACK, data_dir=ksick)
        try:
            keep_branch = con.execute("SELECT branch FROM runs WHERE id='sick'").fetchone()["branch"]
        finally:
            con.close()

        def set_sick(sql, args=()):
            c = DB.open_project(H, STORE_PROJECT_FALLBACK, data_dir=ksick)
            try:
                c.execute(sql, args)
                c.commit()
            finally:
                c.close()

        set_sick("UPDATE runs SET branch=NULL WHERE id='sick'")  # a sibling missing a required field
        out = sk("task", "set", "T", "status=routed")  # owner pick skips the sick sibling
        assert out["ok"] is True and any("sick" in w for w in out.get("warnings", [])), out
        assert sk("task", "get", "T")["task"]["status"] == "routed"  # later commands still work
        assert sk("render", "--run", "good")["ok"]
        for _ in range(2):  # render + task set both walk the sibling; both must warn, not crash
            out = sk("render", "--run", "good")
            assert out["ok"] is True, out
            out = sk("task", "set", "T", "status=building")
            assert out["ok"] is True and any("sick" in w for w in out.get("warnings", [])), out
            assert sk("task", "get", "T")["task"]["status"] == "building"
        set_sick("UPDATE runs SET branch=? WHERE id='sick'", (keep_branch,))
        # a sibling task whose dep names the healthy run must not crash render or commands there
        sk("task", "add", "--root", "--id", "ghost", "--title", "ghost", "--deps", "good:T", "--run", "sick")
        assert sk("render", "--run", "good")["ok"]
        assert sk("task", "set", "T", "status=review")["ok"]
        assert sk("task", "get", "T")["task"]["status"] == "review"
        # a dep whose target run has no row: unmet with a warning, no traceback
        assert sk("task", "add", "--root", "--id", "BD", "--title", "bad dep", "--deps", "baddep:X",
                  "--run", "good")["warnings"][0].startswith("dep baddep:X not found"), "baddep dep"
        assert sk("task", "add", "--root", "--id", "N", "--title", "n", "--owned", "src/*.ts", "--run", "good")["ok"]

        # F2.1/F2.2/F2.3: parent status fan-out, status aliases, task verify — own data dir
        f2 = ["--data", os.path.join(tmp, "f2")]
        fk = lambda *x, code=0: mk(*x, *f2, code=code, data_arg=False)
        fk("run", "init", "--id", "fr", "--feature", "F2 fixes", "--branch", "b")
        fd2 = os.path.join(tmp, "f2")
        fevs = lambda: events_of(fd2, "fr")

        # F2.1: a 3-child parent — status= fans out, one transition per child with the same --note,
        # the parent itself is never set directly (roll-up derives it from the children)
        fk("task", "add", "--root", "--id", "PF", "--title", "fan-out parent")
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
        fk("task", "add", "--root", "--id", "AL", "--title", "aliases")
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
        fk("task", "add", "--root", "--id", "VT", "--title", "verify target")
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
        fk("task", "add", "--root", "--id", "VP", "--title", "verify parent")
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
        fk2("task", "add", "--root", "--id", "VT2", "--title", "verify no baseline")
        out = fk2("task", "verify", "VT2", "--tests", "240/5", "--selftest", "8/3")
        assert out["result"] == "fail", out  # tests fail → still fix even without baseline
        assert out.get("warnings") and "no baseline" in " ".join(out["warnings"]), out
        verd = [e for e in events_of(fd2, "fr2") if e.get("type") == "verdict"][-1]
        assert "no baseline" in verd["text"], verd

        # F2.3 M3: descendants — verify reaches leaves, never writes on a parent
        fk("task", "add", "--root", "--id", "VGP", "--title", "verify grand", "--run", "fr")
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
        fk("task", "add", "--root", "--id", "FGP", "--title", "fan grand", "--run", "fr")
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
        assert q1(pd, "SELECT 1 x FROM tasks WHERE run='pr1' AND id='P1.1'"), "no P1.1 row"
        assert q1(pd, "SELECT plan FROM runs WHERE id='pr1'")["plan"] == "tplan"
        board = pk("render", "--run", "pr1")["board"]
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
        n_ev = n_events(pd, "lg1")
        for bad, why in ((("--tasks", "C1,NOPE"), "NOPE"), (("--tasks", "C1,X1"), "lane_group"),
                         (("--tasks", "C1,C1"), "twice"), (("--tasks", "S1,C1", "C1"), "first of --tasks"),
                         (("--tasks", "C1,S1", "--undo"), "takes no")):
            e = pk("task", "round", *bad, "--kind", "build", "--run", "lg1", code=2)
            assert e["ok"] is False and why in e["error"], (bad, e)
        assert n_events(pd, "lg1") == n_ev  # every refusal wrote nothing
        assert pk("task", "get", "C1", "--run", "lg1")["task"]["round"] == 0
        out = pk("task", "round", "--tasks", "C1,S1,P1,C1.1", "--kind", "build", "--run", "lg1")
        assert out["round"] == 2 and out["tasks"] == ["C1", "S1", "P1", "C1.1"], out  # 1 + the max (S1's)
        assert out["brief"].endswith(os.path.join("tasks", "C1", "brief-r2.md")), out  # lead's folder only
        for tid in ("C1", "S1", "P1", "C1.1"):
            tt = pk("task", "get", tid, "--run", "lg1")["task"]
            assert tt["round"] == 2 and tt["rounds"][-1]["n"] == 2 and tt["rounds"][-1]["kind"] == "build", tt
            assert q1(pd, "SELECT round FROM tasks WHERE run='lg1' AND id=?", (tid,))["round"] == 2, tid
        evs = [x for x in events_of(pd, "lg1")][n_ev:]
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
        evs = events_of(pd, "pf1")
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
        board = pk("render", "--run", "pr1")["board"]
        assert "⚠ plan file changed since run start" in board, board  # the file moved under the run
        # L4: snapshot the DB (runs row + tasks rows + event count); a dry run must leave it unchanged
        snap_row = q1(pd, "SELECT * FROM runs WHERE id='pr1'")
        snap_tasks = {r["id"]: dict(r) for r in q(pd, "SELECT * FROM tasks WHERE run='pr1'")}
        snap_ev = n_events(pd, "pr1")
        out = pk("plan", "sync", "--from-file")
        assert out["apply"] is False
        assert dict(q1(pd, "SELECT * FROM runs WHERE id='pr1'")) == dict(snap_row)
        assert {r["id"]: dict(r) for r in q(pd, "SELECT * FROM tasks WHERE run='pr1'")} == snap_tasks
        assert n_events(pd, "pr1") == snap_ev  # the dry run wrote nothing
        assert any("phase P2" in c and "title" in c for c in out["changes"]), out["changes"]
        assert any("phase P3" in c and "new" in c for c in out["changes"])
        assert any("task P3.1" in c and "new" in c for c in out["changes"])
        wtxt = out["warnings"]
        assert any(w.startswith("phase P1 is building") for w in wtxt), wtxt
        assert any(w.startswith("task P1.1 is building") for w in wtxt), wtxt
        assert any("P1.2" in w and "removed" in w and "kept" in w for w in wtxt), wtxt

        # --apply: adds through the add path, updates in place, ONE event, new sha stored
        n_ev = n_events(pd, "pr1")
        out = pk("plan", "sync", "--apply", "--from-file")
        assert out["apply"] is True
        newev = events_of(pd, "pr1")[n_ev:]
        assert len(newev) == 1 and newev[0]["type"] == "note" and "plan sync" in newev[0]["text"], newev
        run = pk("run", "get", "--id", "pr1")["run"]
        t = {x["id"]: x for x in run["tasks"]}
        assert "P3" in t and t["P3"]["plan_phase"] == "P3" and t["P3"]["lane_group"] == "B"
        assert t["P3"]["reviewer"] == "agy gemini-3.8-flash"  # the sync add path stores it too
        assert "P3.1" in t and t["P3.1"]["parent"] == "P3" and t["P3.1"]["plan_task"] == "P3.1"
        assert q1(pd, "SELECT 1 x FROM tasks WHERE run='pr1' AND id='P3.1'"), "no P3.1 row"
        assert t["P2"]["title"] == "Phase two v2"  # planned: updated
        assert t["P1"]["title"] == "Phase one" and t["P1.1"]["title"] == "First"  # started: untouched
        assert "P1.2" in t  # removed from the plan: never deleted
        assert run["plan_sha"] == file_sha(pfile)
        board = pk("render", "--run", "pr1")["board"]
        assert "⚠" not in board and "Phase two v2" in board, board

        # T1: on a live run plan sync refuses (dry run and --apply), --from-file overrides,
        # an all-terminal run is unchanged
        db = json.load(open(pfile))
        db["phases"][1]["title"] = "Phase two v3"  # a diff a sync would write
        putdb(db)
        before_tasks = {r["id"]: dict(r) for r in q(pd, "SELECT * FROM tasks WHERE run='pr1'")}
        e = pk("plan", "sync", code=2)
        assert "plan sync on a live run overwrites task edits made with task set" in e["error"], e
        assert "or pass --from-file" in e["error"], e
        assert {r["id"]: dict(r) for r in q(pd, "SELECT * FROM tasks WHERE run='pr1'")} == before_tasks, "the refusal wrote nothing"
        e = pk("plan", "sync", "--apply", code=2)
        assert "live run" in e["error"] and {r["id"]: dict(r) for r in
               q(pd, "SELECT * FROM tasks WHERE run='pr1'")} == before_tasks, e
        out = pk("plan", "sync", "--from-file")  # the explicit override still dry-runs first
        assert out["apply"] is False, out
        assert any("phase P2" in c and "title" in c for c in out["changes"]), out["changes"]
        # every task terminal: a plain sync runs again
        tiny = {"plan": "tiny", "status": "approved", "runs": [], "phases": [
            {"id": "Z1", "title": "Solo", "wave": 1, "deps": [], "owned": [], "route": "",
             "reviewer": "", "size": "standard", "risk": "reversible", "review": "light",
             "qa": [], "commit": "feat: z", "subagents": False, "lane_group": "",
             "tasks": [{"id": "Z1.1", "title": "only child", "acceptance": "z", "refs": [],
                        "subagent": False, "owned": [], "deps": [], "parallel": False}]}]}
        tfile = os.path.join(tmp, "tinyplan.json")
        json.dump(tiny, open(tfile, "w"))
        pk("run", "init", "--id", "pr5", "--feature", "Tiny", "--branch", "b5", "--plan", tfile)
        pk("task", "set", "Z1.1", "status=done", "--run", "pr5")  # the roll-up makes Z1 done too
        tiny["phases"][0]["title"] = "Solo v2"
        json.dump(tiny, open(tfile, "w"))
        out = pk("plan", "sync", "--run", "pr5")  # no live task: allowed without --from-file
        assert out["ok"] and out["changes"] == [], out  # the existing started-task rule, unchanged
        assert any("phase Z1 is done (started)" in w for w in out["warnings"]), out["warnings"]

        # last run done → plan done; a non-last run finishing leaves the plan alone.
        # F.0: the close needs both runs closed of work, so their tasks go to done first.
        putdb({**json.load(open(pfile)), "status": "running"})
        pk("run", "init", "--id", "pr3", "--feature", "F3", "--branch", "b3", "--plan", pfile, "--force")
        assert json.load(open(pfile))["runs"] == ["pr1", "pf1", "pr3"]
        o = pk("run", "set", "--id", "pr1", "--status", "done", code=2)
        assert o["ok"] is False and "not closed" in o["error"], o
        assert close_all(pd, "pr1") and close_all(pd, "pr3"), "the fixture runs had no open task"
        pk("run", "set", "--id", "pr1", "--status", "done")
        assert json.load(open(pfile))["status"] == "running"  # pr1 is not the last run
        assert pk("run", "get", "--id", "pr1")["run"]["plan_status"] == "running"
        pk("run", "set", "--id", "pr3", "--status", "done")
        assert json.load(open(pfile))["status"] == "done"  # the last run finished
        r3 = pk("run", "get", "--id", "pr3")["run"]
        assert r3["plan_status"] == "done"
        evs = events_of(pd, "pr3")
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
        assert q1(pd, "SELECT 1 x FROM runs WHERE id='pb1'") is None  # not in the runs table
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
        board = pk("render", "--run", "pr1")["board"]
        assert "⚠" not in board and "plan: `tplan`" in board, board  # unreadable file: no ⚠, no crash

        # legacy path-only plan link keeps the plain path line; no plan at all → no line
        lg = os.path.join(tmp, "legacy")
        mk("run", "init", "--id", "l1", "--feature", "L", "--branch", "b", "--data", lg,
           data_arg=False, e=env)
        board = mk("render", "--data", lg, data_arg=False, e=env)["board"]
        assert "plan:" not in board, board
        # L4: the legacy file path lives in runs.extra_json (runs.plan holds the plan id);
        # set it there, then the board carries the plain path line.
        lcon = DB.open_project(H, STORE_PROJECT_FALLBACK, data_dir=lg)
        try:
            row = lcon.execute("SELECT extra_json FROM runs WHERE id='l1'").fetchone()
            data = _json_obj(row["extra_json"])
            data["plan"] = "plans/legacy.md"
            lcon.execute("UPDATE runs SET extra_json=? WHERE id='l1'",
                         (json.dumps(data, sort_keys=True),))
            lcon.commit()
        finally:
            lcon.close()
        board = mk("render", "--data", lg, data_arg=False, e=env)["board"]
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
            pm("task", "add", "--root", "--id", tid, "--title", tid, "--run", "close")
            if stt != "planned":
                pm("task", "set", tid, f"status={stt}", "--run", "close")
        pm("task", "add", "--root", "--id", "PAR", "--title", "parent", "--run", "close")
        pm("task", "add", "--id", "C1", "--title", "c1", "--parent", "PAR", "--run", "close")
        pm("task", "add", "--id", "C2", "--title", "c2", "--parent", "PAR", "--run", "close")
        pm("task", "set", "C1", "status=merged", "--run", "close")
        pm("task", "set", "C2", "status=qa", "--run", "close")
        # roll-up already lifted PAR to merged; put it back so the close promotes the
        # children and the following save rolls the parent up to done (not a direct set)
        pdata = os.path.join(tmp, "promote")
        con = DB.open_project(H, STORE_PROJECT_FALLBACK, data_dir=pdata)
        try:
            par = con.execute("SELECT status FROM tasks WHERE run='close' AND id='PAR'").fetchone()
            assert par["status"] == "merged", dict(par)
            con.execute("UPDATE tasks SET status='building' WHERE run='close' AND id='PAR'")
            con.commit()
        finally:
            con.close()
        pm("task", "add", "--root", "--id", "OM", "--title", "other merged", "--run", "other")
        pm("task", "set", "OM", "status=merged", "--run", "other")
        n_ev = n_events(pdata, "close")
        # F.0: B/K/P are open and so is the parent PAR (whose children are already merged/qa but
        # which the SQL above put back to building), so the close is refused naming each of them
        # and the exit-2 reply carries no promotion at all — nothing of the run moved
        e = pm("run", "set", "--id", "close", "--status", "done", code=2)
        assert e["ok"] is False and "not closed" in e["error"], e
        assert [x["id"] for x in e["open"]] == ["B", "K", "P", "PAR"], e
        assert n_events(pdata, "close") == n_ev, "the open-work refusal wrote an event"
        assert pm("task", "get", "Q", "--run", "close")["task"]["status"] == "qa"
        # every task closed through the real task-set path. PAR is a phase and is never set
        # directly: closing the other tasks saves the run, and the roll-up lifts PAR off the
        # children it already has at merged/qa.
        for tid in ("B", "K", "P"):
            pm("task", "set", tid, "status=done", "--run", "close")
        assert pm("task", "get", "PAR", "--run", "close")["task"]["status"] in CLOSED_STATUSES
        out = pm("run", "set", "--id", "close", "--status", "done")
        assert out["ok"] and out["run"]["status"] == "done", out
        # the roll-up from B/K/P's save already lifted PAR back to merged, so the close
        # promotes it too (its children are merged/qa)
        assert out["promoted"] == ["M", "Q", "PAR", "C1", "C2"], out
        assert "promote_error" not in out, out
        got = {t["id"]: t["status"] for t in pm("run", "get", "--id", "close")["run"]["tasks"]}
        assert got["M"] == "done" and got["Q"] == "done", got
        assert got["B"] == "done" and got["K"] == "done" and got["P"] == "done", got
        assert got["PAR"] == "done" and got["C1"] == "done" and got["C2"] == "done", got
        assert pm("task", "get", "OM", "--run", "other")["task"]["status"] == "merged"
        newev = events_of(pdata, "close")[n_ev:]
        promo = [e for e in newev if e.get("type") == "transition" and e.get("to") == "done"
                 and e.get("text") == "promoted by closing the run"]
        assert [e["task"] for e in promo] == ["M", "Q", "PAR", "C1", "C2"], newev
        assert all(e["from"] in ("merged", "qa") for e in promo), promo
        again = pm("run", "set", "--id", "close", "--status", "done")
        assert again["ok"] and again["promoted"] == [], again
        assert "promote_error" not in again
        tail = events_of(pdata, "close")[n_ev + len(newev):]
        assert not [e for e in tail if e.get("to") == "done"], tail  # no second promotion

        # K2.2: a raise on the 2nd promotion still reports the task already moved.
        # The list is built by the caller; MK_PROMOTE_FAIL_AFTER=1 fails inside promote_closed.
        pm("run", "init", "--id", "half", "--feature", "Half", "--branch", "h")
        pm("task", "add", "--root", "--id", "H1", "--title", "first", "--run", "half")
        pm("task", "add", "--root", "--id", "H2", "--title", "second", "--run", "half")
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
        # F.6: the closed run prints its final report (a legacy run renders: NONE / MANUAL rows)
        assert rest["report"].startswith("FINAL REPORT run half"), rest.get("report") or rest
        assert "## Corrective actions" in rest["report"] and "Evidence (SELECT" in rest["report"], rest["report"]
        assert "promote_error" not in rest

        # F0-BLOCK-BEGIN — F.0: `run set --status done` is refused while work is open or a gate fails
        f0data = os.path.join(tmp, "f0")
        f0repo = os.path.join(tmp, "f0repo")
        os.makedirs(os.path.join(f0repo, ".git"))
        f0tool = os.path.join(tmp, "f0-planfix.py")
        with open(f0tool, "w") as f:
            f.write(PLAN_FIXTURE)
        f0env = dict(env, MK_STATE_LOADAVG="0", MK_PLAN_CMD=" ".join([sys.executable, f0tool]))
        f0plan = os.path.join(tmp, "f0-plan.json")
        # the gate's cwd must be THIS project's checkout: only it holds the marker
        open(os.path.join(f0repo, "gate-marker.txt"), "w").write("here\n")

        def f0(*args, code=0, e=None):
            return mk(*args, "--data", f0data, code=code, data_arg=False, e=e or f0env)

        def f0put(gates):
            json.dump({"plan": "f0plan", "status": "approved", "runs": ["f0"], "gates": gates,
                       "phases": []}, open(f0plan, "w"), indent=1)

        def f0store():
            """The f0 run's store slice: nothing here may move on a refusal."""
            con = DB.open_project(H, STORE_PROJECT_FALLBACK, data_dir=f0data)
            try:
                return {"run": dict(con.execute("SELECT * FROM runs WHERE id='f0'").fetchone()),
                        "tasks": {r["id"]: dict(r) for r in
                                  con.execute("SELECT * FROM tasks WHERE run='f0'")},
                        "events": con.execute("SELECT COUNT(*) c FROM events WHERE run='f0'").fetchone()["c"],
                        "log": con.execute("SELECT COUNT(*) c FROM change_log WHERE run='f0'").fetchone()["c"]}
            finally:
                con.close()

        # the gates run in THIS project's checkout: the root projects row naming this data dir
        # (the real resolution, not a hard-coded path)
        con = DB.open_root(H)
        try:
            con.execute("INSERT OR REPLACE INTO projects(slug, name, repo, data_dir, mode, db_path)"
                        " VALUES ('f0proj','f0proj',?,?,'home',NULL)", (f0repo, f0data))
            con.commit()
        finally:
            con.close()

        # 1. one planned task: refused, naming it with its status; the store is untouched
        f0put({"manual": "echo manual"})
        f0("run", "init", "--id", "f0", "--feature", "F0", "--branch", "b", "--plan", f0plan)
        f0("task", "add", "--root", "--id", "F0.1", "--title", "open", "--run", "f0")
        before = f0store()
        e = f0("run", "set", "--id", "f0", "--status", "done", code=2)
        assert e["ok"] is False, e
        assert "not closed" in e["error"] and "F0.1 (planned)" in e["error"], e
        assert [o["id"] for o in e["open"]] == ["F0.1"] and e["open"][0]["status"] == "planned", e
        assert f0store() == before, "the open-work refusal wrote something"
        # 5. no bypass: run set has no --force, so the flag is not even parsed
        e = f0("run", "set", "--id", "f0", "--status", "done", "--force", code=2)
        assert e["ok"] is False and "unrecognized arguments" in e["error"], e
        assert f0store() == before, "the --force attempt wrote something"

        # 2a. every task closed, a table gate exits non-zero: refused with its exit code and the
        # last 20 lines of its output
        f0("task", "set", "F0.1", "status=merged", "--run", "f0")
        noisy = "i=1; while [ $i -le 30 ]; do echo line$i; i=$((i+1)); done; false"
        f0put({"manual": "echo manual", "noisy": {"cmd": noisy}})
        snap = f0store()
        e = f0("run", "set", "--id", "f0", "--status", "done", code=2)
        assert e["ok"] is False and "noisy: fail exit=1" in e["error"], e
        assert e["gates"]["noisy"] == "fail exit=1" and e["failed"] == ["noisy"], e
        assert e["gates"]["manual"] == "manual", e  # a string gate is reported, never run
        assert "    line11" in e["error"] and "    line30" in e["error"], e
        assert "line10" not in e["error"], e  # exactly the last 20 lines
        assert f0store() == snap, "the gate refusal wrote something"

        # 2b. timeout
        f0put({"slow": {"cmd": "sleep 5", "timeout": 1}})
        e = f0("run", "set", "--id", "f0", "--status", "done", code=2)
        assert e["ok"] is False and "slow: timeout" in e["error"], e
        assert e["gates"]["slow"] == "timeout", e
        assert f0store() == snap, "the timeout refusal wrote something"

        # 2c. deferral: a 1-minute load average above 40 blocks and no gate is run at all
        f0put({"ok": {"cmd": "true"}, "manual": "echo manual"})
        loaded = dict(f0env, MK_STATE_LOADAVG="41")
        e = f0("run", "set", "--id", "f0", "--status", "done", code=2, e=loaded)
        assert e["ok"] is False and "deferred load=41" in e["error"], e
        assert e["gates"] == {"ok": "deferred load=41", "manual": "manual"}, e
        assert f0store() == snap, "the deferral wrote something"

        # 3. every task closed and the gate passes (load 40 is not above 40): the run closes, and
        # the reply lists each gate with its result. The gate's own cwd is asserted with the
        # marker file only this project's checkout carries.
        f0put({"ok": {"cmd": "test -f gate-marker.txt"}, "ok2": {"cmd": "true"}, "manual": "echo hi"})
        out = f0("run", "set", "--id", "f0", "--status", "done", e=dict(f0env, MK_STATE_LOADAVG="40"))
        assert out["ok"] is True and out["run"]["status"] == "done", out
        assert out["gates"] == {"ok": "pass", "ok2": "pass", "manual": "manual"}, out
        assert out["promoted"] == ["F0.1"], out
        assert "gate_error" not in out, out

        # reopening is not gated: --status active never runs a gate
        f0put({"ok": {"cmd": "false"}})
        out = f0("run", "set", "--id", "f0", "--status", "active")
        assert out["ok"] is True and out["run"]["status"] == "active", out
        assert "gates" not in out, out
        # F0-BLOCK-END

        # F.1: rework status — rollup, close refusal, DEP_MET exclusion
        rwdata = os.path.join(tmp, "rework")
        def rw(*args, code=0):
            return mk(*args, "--data", rwdata, code=code, data_arg=False)
        rw("run", "init", "--id", "rw", "--feature", "RW", "--branch", "b")
        rw("task", "add", "--root", "--id", "P", "--title", "phase", "--run", "rw")
        rw("task", "add", "--id", "A", "--title", "merged", "--run", "rw", "--parent", "P")
        rw("task", "add", "--id", "B", "--title", "rework", "--run", "rw", "--parent", "P")
        rw("task", "set", "A", "status=merged", "--run", "rw")
        rw("task", "set", "B", "status=rework", "--run", "rw")
        # The phase rolls up to rework
        out = rw("task", "get", "P", "--run", "rw")
        assert out["task"]["status"] == "rework", f"phase status {out['task']['status']} != rework"
        # run set --status done refuses: B is rework (not in CLOSED_STATUSES)
        out = rw("run", "set", "--id", "rw", "--status", "done", code=2)
        assert out["ok"] is False, out
        assert "not closed" in out["error"], out
        assert any(o["id"] == "B" and o["status"] == "rework" for o in out.get("open", [])), out
        # rework is NOT in DEP_MET — a dependent cannot dispatch
        assert "rework" not in CLOSED_STATUSES, "rework in CLOSED_STATUSES"
        # Fix B, phase rolls back to merged, run closes
        rw("task", "set", "B", "status=merged", "--run", "rw")
        out = rw("task", "get", "P", "--run", "rw")
        assert out["task"]["status"] == "merged", f"phase status {out['task']['status']} != merged"
        out = rw("run", "set", "--id", "rw", "--status", "done")
        assert out["ok"] is True and out["run"]["status"] == "done", out

        d15_self_test(tmp, env, pd, penv)
        print("mk-state self-test ok")
        store_self_test(tmp, env)
        print("mk-state store self-test ok")
        migrated_store_self_test(tmp, env)
        print("mk-state migrated-store self-test ok")
        no_file_views_self_test(tmp, env)
        print("mk-state no-file-views self-test ok")
        cross_run_dep_from_db_self_test(tmp, env)
        print("mk-state cross-run-dep self-test ok")
        no_field_blanked_self_test(tmp, env)
        print("mk-state no-field-blanked self-test ok")
        r2_fixes_self_test(tmp, env)
        print("mk-state r2-fixes self-test ok")
        x31_r2_fixes_self_test(tmp, env)
        print("mk-state x31-r2-fixes self-test ok")
        gate_run_self_test(tmp, env)
        stray_writers_self_test(tmp, env)
        print("mk-state stray-writers self-test ok")
        fidelity_self_test(tmp, env)
        rework_round_self_test(tmp, env)
        lessons_lint_self_test()
        reconcile_self_test(tmp, env)
        print("mk-state gate-run self-test ok")
        # R1.2: the mkdb write guard refuses the real home from this suite too.
        _mkdb().selftest_guard_probe(tmp_home)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def reconcile_self_test(tmp, env):
    """F.5: `reconcile`. The phase's last close writes a reconcile_due row (no gate run in that
    write); reconcile reruns due and stale gates, maps a final-gate failure to its covers via
    `covers`, moves merged -> rework only on a failure that repeats on the same tree, records
    inconclusive (timeout / deferred) as an open risk reason; run done / archive are refused while a
    rework task, an open reason or a due row remains; a legacy run writes and refuses nothing.
    Every case FAILS on c97e8c5 (no `reconcile` command, no due rows, no refusal)."""
    H = env["MK_HOME"]
    DB = _mkdb()
    repo, data = os.path.join(tmp, "rc-repo"), os.path.join(tmp, "rc-data")
    os.makedirs(repo)
    subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
    open(os.path.join(repo, "f.txt"), "w").write("0")
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c0"],
                   check=True, capture_output=True)
    con = DB.open_root(H)
    try:
        con.execute("INSERT OR REPLACE INTO projects(slug, name, repo, data_dir, mode, db_path)"
                    " VALUES ('rcproj','rcproj',?,?,'home',NULL)", (repo, data))
        con.commit()
    finally:
        con.close()

    def rc(*args, code=0, e=None):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, "--data", data],
                           capture_output=True, text=True, env=e or dict(env, MK_STATE_LOADAVG="0"),
                           cwd=tmp, timeout=120)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    def sql(q, *args, write=False):
        c = DB.open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
        try:
            rows = [tuple(r) for r in c.execute(q, args)]
            if write:
                c.commit()
            return rows
        finally:
            c.close()

    def setx(table, where, key, val):
        cur = json.loads(sql(f"SELECT extra_json FROM {table} WHERE {where}")[0][0] or "{}")
        cur[key] = val
        sql(f"UPDATE {table} SET extra_json=? WHERE {where}", json.dumps(cur), write=True)

    def final(rid, cmd, covers, **kw):
        setx("runs", f"id='{rid}'", "final_gates",
             [{"id": "fin", "cmd": cmd, "every": "merge", "covers": covers, **kw}])

    def mkrun(rid, tasks, fidelity=True):
        rc("run", "init", "--id", rid, "--feature", rid, "--branch", "b")
        for tid, parent in tasks:
            rc("task", "add", "--id", tid, "--title", tid, "--run", rid, *(["--parent", parent] if parent else ["--root"]))
            setx("tasks", f"run='{rid}' AND id='{tid}'", "gate", [{"cmd": "true"}])
        if fidelity:
            setx("runs", f"id='{rid}'", "fidelity", True)

    def merge(rid, *tids):
        for tid in tids:
            rc("gate", "run", tid, "--run", rid)
            rc("task", "set", tid, "status=merged", "--run", rid)

    def status(rid, tid):
        return rc("task", "get", tid, "--run", rid)["task"]["status"]

    def due(rid):
        return sql("SELECT scope, done IS NULL FROM reconcile_due WHERE run=? ORDER BY id", rid)

    def refused(out, *words):
        assert out["ok"] is False and all(w in out["error"] for w in words), out

    # 1. a final gate that is always red: its covered task goes to rework, B stays merged
    mkrun("c1", [("A", None), ("B", None)])
    final("c1", "false", ["A"])
    merge("c1", "A", "B")
    assert due("c1") == [("A", 1), ("B", 1)], due("c1")
    assert status("c1", "A") == "merged"  # the close ran no final gate
    out = rc("reconcile", "--run", "c1")
    assert out["reworked"] == ["A"] and out["flaky"] == [] and out["inconclusive"] == [], out
    assert status("c1", "A") == "rework" and status("c1", "B") == "merged"
    refused(rc("run", "set", "--id", "c1", "--status", "done", code=2), "A")
    refused(rc("run", "archive", "--id", "c1", code=2), "rework", "A")
    # the fix: gate green, the task re-merged (its evidence is still fresh), reconcile again
    final("c1", "true", ["A"])
    rc("task", "set", "A", "status=merged", "--run", "c1")
    out = rc("reconcile", "--run", "c1")
    assert out["reworked"] == [] and "fin" in out["green"], out
    assert all(d == 0 for _, d in due("c1")), due("c1")
    assert rc("run", "set", "--id", "c1", "--status", "done")["run"]["status"] == "done"

    # 2. red once, then green on the same tree: flaky, nothing changes
    mark = os.path.join(tmp, "rc-flaky-mark")
    mkrun("c2", [("A", None)])
    final("c2", f"test -e {mark} || {{ touch {mark}; exit 1; }}", ["A"])
    merge("c2", "A")
    out = rc("reconcile", "--run", "c2")
    assert out["flaky"] == ["fin"] and out["reworked"] == [] and status("c2", "A") == "merged", out
    assert sql("SELECT result FROM evidence WHERE run='c2' AND gate='fin' ORDER BY id") == [("red",), ("green",)]
    assert rc("run", "set", "--id", "c2", "--status", "done")["run"]["status"] == "done"

    # 3. a passing reconcile changes no status and no risk row; it adds evidence and stamps the row
    mkrun("c3", [("A", None), ("B", None)])
    final("c3", "true", ["A", "B"])
    merge("c3", "A", "B")
    before = (sql("SELECT id, status FROM tasks WHERE run='c3' ORDER BY id"),
              sql("SELECT * FROM risk_reasons WHERE run='c3'"), sql("SELECT COUNT(*) FROM evidence WHERE run='c3'"))
    out = rc("reconcile", "--run", "c3")
    assert out["green"] == ["fin"] and not out["reworked"] and not out["inconclusive"] and not out["flaky"], out
    assert before[:2] == (sql("SELECT id, status FROM tasks WHERE run='c3' ORDER BY id"),
                          sql("SELECT * FROM risk_reasons WHERE run='c3'")), "reconcile changed state"
    assert sql("SELECT COUNT(*) FROM evidence WHERE run='c3'")[0][0] == before[2][0][0] + 1
    assert all(d == 0 for _, d in due("c3"))
    again = rc("reconcile", "--run", "c3")  # nothing due, nothing stale
    assert again["ran"] == [], again

    # 4. timeout and deferred are inconclusive: a risk reason, no status change, close refused
    mkrun("c4", [("A", None)])
    final("c4", "sleep 5", ["A"], timeout=1)
    merge("c4", "A")
    out = rc("reconcile", "--run", "c4")
    assert out["inconclusive"] == ["A"] and out["reworked"] == [], out
    assert status("c4", "A") == "merged"
    assert sql("SELECT kind, points FROM risk_reasons WHERE run='c4' AND cleared IS NULL") == [("reconcile-inconclusive", 1)]
    assert rc("reconcile", "--run", "c4")["inconclusive"] == ["A"]  # a second run opens no second reason
    assert len(sql("SELECT id FROM risk_reasons WHERE run='c4'")) == 1
    final("c4", "true", ["A"])
    rc("gate", "run", "--final", "--run", "c4")  # fresh green evidence; only the open reason is left
    refused(rc("run", "set", "--id", "c4", "--status", "done", code=2), "reconcile-inconclusive")
    refused(rc("run", "archive", "--id", "c4", code=2), "reconcile-inconclusive")
    ev = rc("gate", "run", "A", "--run", "c4")["evidence"][0]["id"]
    rid = sql("SELECT id FROM risk_reasons WHERE run='c4'")[0][0]
    rc("risk", "clear", str(rid), "--evidence", str(ev), "--run", "c4")
    assert rc("run", "set", "--id", "c4", "--status", "done")["run"]["status"] == "done"
    mkrun("c4d", [("A", None)])
    final("c4d", "true", ["A"])
    merge("c4d", "A")
    out = rc("reconcile", "--run", "c4d", e=dict(env, MK_STATE_LOADAVG="41"))
    assert out["inconclusive"] == ["A"] and status("c4d", "A") == "merged", out
    assert sql("SELECT result FROM evidence WHERE run='c4d' AND gate='fin'") == [("deferred",)]
    refused(rc("run", "archive", "--id", "c4d", code=2), "reconcile-inconclusive")

    # 5. the due row: written by the phase's LAST close only, once; a legacy run writes none
    mkrun("c5", [("P", None), ("K1", "P"), ("K2", "P")])
    final("c5", "true", ["P"])
    merge("c5", "K1")
    assert due("c5") == [], due("c5")
    rc("gate", "run", "P", "--run", "c5")  # the phase's own gate, so the roll-up may close it
    merge("c5", "K2")
    assert due("c5") == [("P", 1)], due("c5")
    rc("task", "set", "K2", "status=merged", "--run", "c5", "--note", "again")
    assert len(due("c5")) == 1, due("c5")
    rc("gate", "run", "--final", "--run", "c5")  # fresh evidence; only the due row is left
    refused(rc("run", "set", "--id", "c5", "--status", "done", code=2), "reconcile due", "P")
    refused(rc("run", "archive", "--id", "c5", code=2), "reconcile due", "P")
    rc("reconcile", "--run", "c5", "--phase", "P")
    assert due("c5") == [("P", 0)], due("c5")
    assert rc("run", "archive", "--id", "c5")["run"]["status"] == "archived"
    mkrun("c6", [("L", None)], fidelity=False)
    final("c6", "false", ["L"])
    rc("task", "set", "L", "status=merged", "--run", "c6")
    assert due("c6") == []
    out = rc("reconcile", "--run", "c6")
    assert out["ran"] == [] and status("c6", "L") == "merged", out
    assert rc("run", "set", "--id", "c6", "--status", "done")["run"]["status"] == "done"


_CLOCK = {"t": None}  # the parity test pins now() so the two paths share one clock


def _frozen_now():
    return _CLOCK["t"] or now()


def store_self_test(tmp, env):
    """D3A: the store is opt-in. Off = no mk.db and byte-identical files; on = views from the store."""

    H = os.environ["MK_HOME"]  # Z3: the one real home (root projection)
    import shutil as _sh
    data = os.path.join(tmp, "store")

    def mk(*args, code=0, e=None, stdin_text=None):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, "--data", data],
                           capture_output=True, text=True, env=e or env,
                           input=stdin_text)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    # L4: the store is ALWAYS on. `store enable` is kept as a command but imports nothing, and
    # `store status` reports on. No file view exists to snapshot or to publish.
    assert mk("store", "status")["store"] == "on"
    mk("run", "init", "--id", "s1", "--feature", "Store", "--branch", "b")
    mk("task", "add", "--root", "--id", "T1", "--title", "One")
    mk("task", "set", "T1", "status=routed")
    mk("event", "--task", "T1", "--type", "note", "--text", "hello")
    assert os.path.exists(os.path.join(data, "mk.db")), "the store is always on: mk.db must exist"
    assert not os.path.exists(os.path.join(data, "runs-legacy"))
    for rel in ("runs/s1/run.json", "runs/s1/events.jsonl", "runs/s1/board.md", "runs/index.json"):
        assert not os.path.exists(os.path.join(data, rel)), rel  # no view file is written

    # `store enable` is a no-op now: it reports on and imports nothing.
    out = mk("store", "enable")
    assert out["store"] == "on", out
    assert mk("store", "status")["store"] == "on"

    db = _mkdb()
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        n_ev = con.execute("SELECT COUNT(*) c FROM events WHERE run='s1' AND chan='state'").fetchone()["c"]
        n_tasks = con.execute("SELECT COUNT(*) c FROM tasks WHERE run='s1'").fetchone()["c"]
        statuses = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM tasks WHERE run='s1'")}
    finally:
        con.close()
    assert n_tasks == 1 and statuses == {"T1": "routed"}, (n_tasks, statuses)
    assert n_ev >= 3, n_ev  # init note + task added + merged

    # store on: the same script against a second data dir; both are DB-only now (no file views)
    twin = os.path.join(tmp, "twin")
    global now
    real_now, now = now, _frozen_now

    script = [
        ("run", "init", "--id", "s2", "--feature", "Twin", "--branch", "b2"),
        ("task", "add", "--root", "--id", "A", "--title", "Alpha", "--run", "s2"),
        ("task", "add", "--root", "--id", "B", "--title", "Beta", "--deps", "A", "--run", "s2"),
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

    def rows_of(d, rid):
        con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=d)
        try:
            tasks = {r["id"]: dict(r) for r in con.execute("SELECT * FROM tasks WHERE run=?", (rid,))}
            events = [dict(r) for r in con.execute(
                "SELECT task, kind, from_id, to_id, text FROM events WHERE run=? ORDER BY id", (rid,))]
            runx = con.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            return tasks, events, dict(runx)
        finally:
            con.close()

    # both stores hold the same s2 rows (the twin also holds s1, so compare the s2 slice)
    at, ae, ar = rows_of(data, "s2")
    bt, be, br = rows_of(twin, "s2")
    assert set(at) == set(bt), (set(at), set(bt))
    for tid in at:
        for k in ("status", "title", "deps", "owned", "round", "parent"):
            assert at[tid][k] == bt[tid][k], (tid, k, at[tid][k], bt[tid][k])
    assert [(e["task"], e["kind"], e["text"]) for e in ae] == \
           [(e["task"], e["kind"], e["text"]) for e in be], (ae, be)
    assert (ar["status"], ar["feature"] if "feature" in ar else None) == \
           (br["status"], br["feature"] if "feature" in br else None), (ar, br)
    for rel in ("runs/s2/run.json", "runs/s2/events.jsonl", "runs/s2/board.md", "runs/index.json"):
        assert not os.path.exists(os.path.join(data, rel)), rel
        assert not os.path.exists(os.path.join(twin, rel)), rel

    # every event row is unique and the log only grows (both stores are DB-only now)
    def ev_count(d, rid):
        con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=d)
        try:
            return con.execute("SELECT COUNT(*) c FROM events WHERE run=?", (rid,)).fetchone()["c"]
        finally:
            con.close()

    def q1_dir(d, sql, args=()):
        con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=d)
        try:
            return con.execute(sql, args).fetchone()
        finally:
            con.close()

    def events_rows(d, rid):
        """The run's state events as their JSON lines (was events.jsonl). A direct transition
        (meta_json NULL) renders in today's format, exactly as Store.event_line does."""
        con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=d)
        try:
            out = []
            for r in con.execute("SELECT run, task, t, kind, from_id, to_id, text, meta_json"
                                 " FROM events WHERE run=? ORDER BY id", (rid,)):
                meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
                if isinstance(meta.get("line"), str):
                    out.append(json.loads(meta["line"]))
                else:
                    out.append({"t": r["t"], "run": r["run"], "task": r["task"], "type": r["kind"],
                                "text": r["text"], "from": r["from_id"], "to": r["to_id"],
                                "by": meta.get("by") or "orchestrator"})
            return out
        finally:
            con.close()

    counts = [ev_count(data, "s2")]
    now = _frozen_now
    _CLOCK["t"] = "2000-01-01T00:00:00+00:00"
    grow = (("event", "--type", "note", "--text", "one more", "--run", "s2"),
            ("task", "set", "B", "status=routed", "--run", "s2"))
    try:
        for step in grow:
            with contextlib.redirect_stdout(open(os.devnull, "w")):
                assert main([*step, "--data", data]) == 0, step
            counts.append(ev_count(data, "s2"))
        assert counts == sorted(counts) and len(set(counts)) == len(counts), counts
        for step in grow:
            with contextlib.redirect_stdout(open(os.devnull, "w")):
                assert main([*step, "--data", twin]) == 0, step
        assert ev_count(data, "s2") == ev_count(twin, "s2")  # the same rows on both
    finally:
        now = real_now
        _CLOCK["t"] = None

    # a status change goes through transition(): change_log grew and the event is in the store
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        before = con.execute("SELECT COUNT(*) c FROM change_log WHERE run='s2'").fetchone()["c"]
    finally:
        con.close()
    mk("task", "set", "B", "status=building", "--note", "start", "--run", "s2")
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        after = con.execute("SELECT COUNT(*) c FROM change_log WHERE run='s2'").fetchone()["c"]
        hit = con.execute("SELECT 1 FROM events WHERE run='s2' AND task='B' AND kind='transition'"
                          " AND to_id='building'").fetchone()
    finally:
        con.close()
    assert after > before, (before, after)
    assert hit, "transition event missing from the store"

    # X3.1: `task brief` authors the round's brief into the store in ONE command; re-running
    # REPLACES it (rev+1 for the same task+round, no duplicate pile) and the view file follows
    # the artifact. Same-bytes re-run is idempotent (no rev bump).
    paths = mk("task", "round", "B", "--kind", "fix", "--cause", "gap", "--run", "s2")
    brief_file = os.path.join(tmp, "brief-file.md")
    with open(brief_file, "w") as f:
        f.write("brief body v1\n")
    b1 = mk("task", "brief", "B", "--file", brief_file)
    assert b1["round"] == paths["round"] and b1["stored"] is True and b1["rev"] == 1, b1
    assert os.path.exists(paths["brief"]) and open(paths["brief"]).read() == "brief body v1\n"
    with open(brief_file, "w") as f:
        f.write("brief body v2\n")
    b2 = mk("task", "brief", "B", "--file", brief_file)
    assert b2["rev"] == 2 and b2["previous_view_body"] == "brief body v1\n", b2
    b3 = mk("task", "brief", "B", "--file", brief_file)
    assert b3["rev"] == 2, b3  # same bytes: no rev bump
    stdin_body = mk("task", "brief", "B", "--stdin", stdin_text="stdin brief\n")
    assert stdin_body["rev"] == 3, stdin_body
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        n_brief = con.execute(
            "SELECT COUNT(*) c FROM artifacts WHERE run='s2' AND task='B' AND kind='brief'"
        ).fetchone()["c"]
    finally:
        con.close()
    assert n_brief == 3, n_brief  # three revisions, one newest row each; no duplicate pile
    # X3.3: export writes the round files back FROM the artifacts (all rounds)
    os.unlink(paths["brief"])
    ex = mk("task", "export", "B")
    assert ex["ok"] and paths["brief"] in ex["written"], ex
    with open(paths["brief"]) as f:
        assert f.read() == "stdin brief\n"
    ex2 = mk("task", "export", "B")
    assert ex2["ok"] and not ex2.get("differing"), ex2  # idempotent: same bytes rewritten, no drift

    # L4: export-legacy is a no-op (no file views exist to rebuild)
    saved_rows = rows_of(data, "s2")
    mk("export-legacy", "--run", "s2")
    assert rows_of(data, "s2") == saved_rows  # nothing changed
    for rel in ("runs/s2/run.json", "runs/s2/events.jsonl", "runs/s2/board.md", "runs/index.json"):
        assert not os.path.exists(os.path.join(data, rel)), rel  # still no view file

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
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        con.execute("INSERT INTO plans(id, version, title, approved_at, archived_path) VALUES (?,?,?,?,?)",
                    ("gp", 1, "gp", "2026-01-01T00:00:00+00:00", archived))
        con.commit()
    finally:
        con.close()
    out = mk("plan", "sync", "--run", "s3", "--from-file", e=e2)  # parses through the archived path, not the missing one
    assert out["ok"] and out["plan"] == "gp", out

    # L4: a crash after the store commit leaves no file view to lag behind; the event is in the
    # store and the next command adds its own without duplicating the first.
    crashed = dict(env, MK_VIEW_CRASH="1")
    bad = mk("event", "--type", "note", "--text", "crash me", "--run", "s2", code=2, e=crashed)
    assert "injected crash" in bad["error"], bad
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        stored = con.execute("SELECT text FROM events WHERE run='s2' AND text='crash me'").fetchone()
    finally:
        con.close()
    assert stored, "the store lost the event committed before the crash"
    mk("event", "--type", "note", "--text", "after", "--run", "s2")
    texts = [e["text"] for e in rows_of(data, "s2")[1]]
    assert texts.count("crash me") == 1 and texts.count("after") == 1, texts[-4:]

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
        once(["task", "add", "--root", "--id", f"R{i}", "--title", f"r{i}", "--run", "rr"], race)
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
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=race)
    try:
        rows = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM tasks WHERE run='rr'")}
    finally:
        con.close()
    assert all(rows[f"R{i}"] == "building" for i in range(20)), rows

    # CRITICAL review :246 — a direct transition survives the next task set
    keep = os.path.join(tmp, "keep")
    once(["run", "init", "--id", "k1", "--feature", "K", "--branch", "bk"], keep)
    once(["task", "add", "--root", "--id", "T1", "--title", "One", "--run", "k1"], keep)
    once(["task", "add", "--root", "--id", "T2", "--title", "Two", "--run", "k1"], keep)
    once(["store", "enable"], keep)
    db.transition(H, STORE_PROJECT_FALLBACK, "k1", "T1", "building", data_dir=keep)
    once(["task", "set", "T2", "status=building", "--run", "k1"], keep)
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=keep)
    try:
        st1 = con.execute("SELECT status FROM tasks WHERE run='k1' AND id='T1'").fetchone()["status"]
    finally:
        con.close()
    assert st1 == "building", st1
    # L4: the file view is gone; the read source is the store, which holds the direct transition
    assert once(["task", "get", "T1", "--run", "k1"], keep)["task"]["status"] == "building"

    # HIGH review :563 — exactly one transition event, and a direct transition is in the store
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=keep)
    try:
        count = con.execute("SELECT COUNT(*) c FROM events WHERE run='k1' AND task='T2'"
                            " AND kind='transition' AND to_id='building'").fetchone()["c"]
    finally:
        con.close()
    assert count == 1, count
    once(["run", "get", "--id", "k1"], keep)  # a read picks up the direct transition
    lines = [e for e in events_rows(keep, "k1")]
    state_lines = [x for x in lines if x.get("task") == "T1" and x.get("to") == "building"]
    assert len(state_lines) == 1, lines
    tl = db.timeline(H, STORE_PROJECT_FALLBACK, "k1", "T1", data_dir=keep)
    store_t1 = [x for x in lines if x.get("task") == "T1"]
    assert len(tl) == len(store_t1), (len(tl), len(store_t1), tl, store_t1)

    # HIGH review :676 — a failed import leaves <data> unchanged (no runs-legacy view tree)
    boom = os.path.join(tmp, "boom")
    once(["run", "init", "--id", "b1", "--feature", "B", "--branch", "bb"], boom)
    st = Store(boom)
    real_db = st.db()
    real_import = real_db.import_history
    def fail_import(*a, **k):
        raise real_db.Fail("injected import failure")
    # L4: enable_store is a no-op, so the injectable path is a real commit; drive it directly.
    real_db.import_history = fail_import
    try:
        st.task_row({"id": "b1", "tasks": []}, {"id": "X", "title": "x"})
        real_db.import_history(st.home_dir(), STORE_PROJECT_FALLBACK, "b1", data_dir=boom,
                               run_row={"id": "b1", "status": "active"},
                               tasks=[st.task_row({"id": "b1", "tasks": []}, {"id": "X", "title": "x"})])
        raise AssertionError("the injected import failure did not raise")
    except real_db.Fail as e:
        assert "injected" in str(e), e
    finally:
        real_db.import_history = real_import
    assert not os.path.exists(os.path.join(boom, "runs-legacy")), "runs-legacy left behind"

    # HIGH review :733 — a direct transition is visible through the store after the next read
    once(["run", "get", "--id", "k1"], keep)
    db.transition(H, STORE_PROJECT_FALLBACK, "k1", "T1", "review", data_dir=keep)
    once(["export-legacy", "--run", "k1"], keep)  # no-op under L4
    assert once(["task", "get", "T1", "--run", "k1"], keep)["task"]["status"] == "review"

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
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=keep)
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

    # L4: a corrupt task row does not crash a write; the store still moves the task (no file view
    # to be torn). The runs/task.json-is-a-directory fault injected by the old file path is gone.
    corrupt = os.path.join(tmp, "corrupt")
    once(["run", "init", "--id", "c1", "--feature", "C", "--branch", "bc"], corrupt)
    once(["task", "add", "--root", "--id", "T", "--title", "Tee", "--run", "c1"], corrupt)
    cc = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=corrupt)
    try:
        cc.execute("UPDATE tasks SET route_json='{not json' WHERE run='c1' AND id='T'")
        cc.commit()
    finally:
        cc.close()
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "task", "set", "T",
                        "status=building", "--run", "c1", "--data", corrupt],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr
    body = json.loads(r.stdout.strip().splitlines()[0])
    assert body["ok"] is True, body
    cc = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=corrupt)
    try:
        assert cc.execute("SELECT status FROM tasks WHERE run='c1' AND id='T'").fetchone()["status"] == "building"
    finally:
        cc.close()
    # a missing view never existed to repair: the store is the read source
    assert once(["run", "get", "--id", "c1"], corrupt)["run"]["id"] == "c1"
    for rel in ("runs/c1/run.json", "runs/c1/board.md", "runs/c1/events.jsonl"):
        assert not os.path.exists(os.path.join(corrupt, rel)), rel

    # L7.3: the root card and sync watermark are gone, so this checks the surviving facts —
    # the project store is the truth whether or not the root write runs, and `sync` (catch_up)
    # is idempotent and creates no root card table rows.
    root = os.path.join(tmp, "d3b-home")
    d3 = os.path.join(tmp, "d3b")
    d3env = dict(env, MATRIXKODER_HOME=root, MK_HOME=root)
    once(["run", "init", "--id", "d1", "--feature", "D", "--branch", "bd"], d3)
    once(["task", "add", "--root", "--id", "T1", "--title", "Rooted", "--run", "d1"], d3)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "task", "set", "T1",
                        "status=building", "--run", "d1", "--data", d3],
                       capture_output=True, text=True, env=d3env)
    assert r.returncode == 0, r.stdout
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=d3)
    try:
        assert con.execute("SELECT status FROM tasks WHERE run='d1' AND id='T1'").fetchone()["status"] \
            == "building"
    finally:
        con.close()
    killed = dict(d3env, MK_ROOT_CRASH="1")
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "task", "set", "T1",
                        "status=review", "--run", "d1", "--data", d3],
                       capture_output=True, text=True, env=killed)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=d3)
    try:
        proj = con.execute("SELECT status FROM tasks WHERE run='d1' AND id='T1'").fetchone()["status"]
    finally:
        con.close()
    assert proj == "review", proj  # the project commit landed, root write or not
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "sync", "--data", d3],
                       capture_output=True, text=True, env=d3env)
    assert r.returncode == 0, r.stdout
    assert json.loads(r.stdout)["copied"] == 0, r.stdout  # plan_summary already current
    rootdb = db.open_root(root)
    try:
        for table in ("cards", "project_summary", "sync"):  # root schema 3: not created at all
            assert not rootdb.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone(), table
    finally:
        rootdb.close()

    # L4: sync finds the store always on — it copies any un-synced changes and creates no view
    off = os.path.join(tmp, "d3b-off")
    once(["run", "init", "--id", "o1", "--feature", "O", "--branch", "bo"], off)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "sync", "--data", off],
                       capture_output=True, text=True, env=d3env)
    assert r.returncode == 0 and json.loads(r.stdout)["store"] == "on", r.stdout
    assert os.path.exists(os.path.join(off, "mk.db"))  # the store is always on
    for rel in ("runs/o1/run.json", "runs/index.json"):
        assert not os.path.exists(os.path.join(off, rel)), rel
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
    assert q1_dir(from_json, "SELECT 1 x FROM runs WHERE id='j1'"), "j1 not in from-json"
    # Z3: a WRITE maintains the root projection in the REAL home (the old code wrote a fake
    # <data>/.mk-home instead), so run init creates <home>/mk.db here. A read (the `bare` case
    # above) still creates nothing.
    # L7.3 retired the root cards writes that used to create <home>/mk.db on a bare write: with no
    # plan there is no plan_summary row to write, so the root file is only created by a registration
    # or a plan. What must still hold: no fake home inside the data dir.
    assert not os.path.exists(os.path.join(from_json, ".mk-home")), "a fake home must not be created"
    # L10.7: register writes the root table ONLY; the stale projects.json is read, never written
    pj_path = os.path.join(reg_home, "projects.json")
    pj_stat = (os.stat(pj_path).st_mtime_ns, os.stat(pj_path).st_size, open(pj_path, "rb").read())
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
    # the CLI form the wizard calls: root row yes, projects.json no
    cli_home = os.path.join(tmp, "cli-reg-home")
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "project", "register", "--slug", "clireg",
                        "--repo", reg_repo, "--data", from_root], capture_output=True, text=True,
                       env=dict(env, MATRIXKODER_HOME=cli_home, MK_HOME=cli_home))
    assert r.returncode == 0 and json.loads(r.stdout)["slug"] == "clireg", (r.stdout, r.stderr)
    assert os.path.isfile(mkpaths.root_db(cli_home)) and not os.path.exists(os.path.join(cli_home, "projects.json"))
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
    assert (os.stat(pj_path).st_mtime_ns, os.stat(pj_path).st_size, open(pj_path, "rb").read()) == pj_stat, \
        "register_project wrote projects.json"
    assert mkpaths.root_data_dir(os.path.realpath(reg_repo)) == os.path.abspath(from_root)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "init", "--id", "rt1",
                        "--feature", "R", "--branch", "br"], cwd=reg_repo,
                       capture_output=True, text=True, env=dict(env, MATRIXKODER_HOME=reg_home, MK_HOME=reg_home))
    assert r.returncode == 0, r.stdout
    assert q1_dir(from_root, "SELECT 1 x FROM runs WHERE id='rt1'"), "rt1 not in from-root"
    env_over = dict(env, MATRIXKODER_HOME=reg_home, MK_HOME=reg_home, MATRIXKODER_DATA=os.path.join(tmp, "env-over"))
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "init", "--id", "ev1",
                        "--feature", "E", "--branch", "be"], cwd=reg_repo,
                       capture_output=True, text=True, env=env_over)
    assert r.returncode == 0, r.stdout
    assert q1_dir(os.path.join(tmp, "env-over"), "SELECT 1 x FROM runs WHERE id='ev1'"), "ev1 not in env-over"

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
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=d3)
    try:
        con.execute("INSERT INTO plans(id, version, title, header_json, body_md, approved_at)"
                    " VALUES (?,?,?,?,?,?)",
                    ("tplan", 1, "tplan", json.dumps({"sha": "old", "status": "running", "phases": []}),
                     "{}", now()))
        con.commit()
    finally:
        con.close()
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "plan", "sync", "--run", "p1",
                        "--from-file", "--data", d3], capture_output=True, text=True, env=penv)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    dry = json.loads(r.stdout)
    assert dry["apply"] is False and "sha" in dry["version_diff"], dry
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=d3)
    try:
        still = con.execute("SELECT version FROM plans WHERE id='tplan'").fetchone()["version"]
    finally:
        con.close()
    assert still == 1  # dry run wrote nothing
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "plan", "sync", "--run", "p1",
                        "--apply", "--from-file", "--data", d3], capture_output=True, text=True, env=penv)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    applied = json.loads(r.stdout)
    assert applied["version"] == 2, applied
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=d3)
    try:
        row = con.execute("SELECT version, header_json FROM plans WHERE id='tplan'").fetchone()
    finally:
        con.close()
    assert row["version"] == 2 and json.loads(row["header_json"])["sha"] == applied["sha"]
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "plan", "sync", "--run", "p1",
                        "--from-file", "--data", d3], capture_output=True, text=True, env=penv)
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
    def k2_status():
        c = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=d3)
        try:
            return c.execute("SELECT status FROM tasks WHERE run='d1' AND id='T1'").fetchone()["status"]
        finally:
            c.close()

    status_before = k2_status()
    kept = open(backup, "rb").read()
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "archive", "--id", "d1",
                        "--data", d3], capture_output=True, text=True,
                       env=dict(d3env, MK_ARCHIVE_FAIL="1"))
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    body = json.loads(r.stdout)
    assert body["ok"] is False and "backup" in body["error"], body
    assert open(backup, "rb").read() == kept
    assert not os.path.exists(os.path.join(d3, "archive", "d1.db.tmp"))
    assert k2_status() == status_before

    # K2 store on: --tests 384/387 refused (task stays, no event); 350/0 still passes;
    # a raise on the 2nd promotion still lists the 1st.
    k2 = os.path.join(tmp, "k2on")
    k2env = dict(env, MATRIXKODER_HOME=os.path.join(tmp, "k2on-home"), MK_HOME=os.path.join(tmp, "k2on-home"))
    once(["run", "init", "--id", "k2", "--feature", "K2", "--branch", "bk"], k2, k2env)
    once(["task", "add", "--root", "--id", "V", "--title", "verify", "--run", "k2"], k2, k2env)
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
    assert not any("384/387" in e.get("text", "") for e in events_rows(k2, "k2"))
    passed = once(["task", "verify", "V", "--tests", "350/0", "--run", "k2"], k2, k2env)
    assert passed["result"] == "pass" and passed["task"]["status"] == "review", passed
    once(["task", "add", "--root", "--id", "A", "--title", "a", "--run", "k2"], k2, k2env)
    once(["task", "add", "--root", "--id", "B", "--title", "b", "--run", "k2"], k2, k2env)
    once(["task", "set", "A", "status=merged", "--run", "k2"], k2, k2env)
    once(["task", "set", "B", "status=qa", "--run", "k2"], k2, k2env)
    # F.0: V is at review (open), so the close is refused naming it first
    bad = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "set", "--id", "k2",
                          "--status", "done", "--data", k2], capture_output=True, text=True, env=k2env)
    assert bad.returncode == 2, (bad.returncode, bad.stdout, bad.stderr)
    bad = json.loads(bad.stdout.strip())
    assert bad["ok"] is False and "V (review)" in bad["error"], bad
    once(["task", "set", "V", "status=done", "--run", "k2"], k2, k2env)
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
        once(["task", "add", "--root", "--id", f"P{i}", "--title", f"p{i}", "--run", "pr"], par, parenv)
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
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=par)
    try:
        proj = {r["id"]: r["status"] for r in con.execute(
            "SELECT id, status FROM tasks WHERE run='pr'")}
    finally:
        con.close()
    assert set(proj) == {f"P{i}" for i in range(20)}, proj
    # L7.3: the root card copy is gone, so there is nothing to compare the project store against.

    # D3C: store enable imports every run, not only the active one
    many = os.path.join(tmp, "many")
    once(["run", "init", "--id", "r1", "--feature", "F", "--branch", "b1"], many)
    once(["run", "init", "--id", "r2", "--feature", "G", "--branch", "b2"], many)
    once(["run", "set", "--id", "r2", "--status", "done"], many)
    before = {}
    for rid in ("r1", "r2"):
        before[rid] = once(["run", "get", "--id", rid], many)["run"]
    out = once(["store", "enable"], many)
    assert out["store"] == "on" and out["imported"] is None, out  # L4: a no-op
    got_r2 = once(["run", "get", "--id", "r2"], many)
    assert got_r2["ok"] and got_r2["run"]["id"] == "r2" and got_r2["run"]["status"] == "done", got_r2
    for rid in ("r1", "r2"):
        after = once(["run", "get", "--id", rid], many)["run"]
        assert after == before[rid], rid  # the store already held every run

    # L4: an event row appears exactly once (there is no events.jsonl marker to rewind)
    rew = os.path.join(tmp, "rew")
    once(["run", "init", "--id", "s1", "--feature", "S", "--branch", "bs"], rew)
    once(["task", "add", "--root", "--id", "A1", "--title", "a", "--run", "s1"], rew)
    once(["event", "--run", "s1", "--type", "note", "--text", "grow1"], rew)
    once(["event", "--run", "s1", "--type", "note", "--text", "after"], rew)
    texts = [e["text"] for e in events_rows(rew, "s1")]
    assert texts.count("grow1") == 1, texts.count("grow1")
    assert texts.count("after") == 1

    # L4: MK_PUBLISH_KILL (the old view-append seam) no longer applies — there is no view to
    # append. The store commit is the only write, so each event lands exactly once.
    kills = os.path.join(tmp, "kills")
    once(["run", "init", "--id", "s1", "--feature", "S", "--branch", "bs"], kills)
    once(["task", "add", "--root", "--id", "A1", "--title", "a", "--run", "s1"], kills)
    script = os.path.abspath(__file__)
    for trial in range(5):
        before_n = ev_count(kills, "s1")
        once(["event", "--run", "s1", "--type", "note", "--text", f"ev{trial}"], kills)
        after = [e["text"] for e in events_rows(kills, "s1")]
        assert after.count(f"ev{trial}") == 1, (trial, after.count(f"ev{trial}"))
        assert ev_count(kills, "s1") == before_n + 1, (trial, ev_count(kills, "s1"), before_n)

    # L4: task order is the runs.extra_json `task_order` list, so it survives every write
    order = os.path.join(tmp, "order")
    once(["run", "init", "--id", "o1", "--feature", "O", "--branch", "bo"], order)
    once(["task", "add", "--root", "--id", "BA", "--title", "ba", "--run", "o1"], order)
    once(["task", "add", "--id", "BA.1", "--title", "ba1", "--parent", "BA", "--run", "o1"], order)
    once(["task", "add", "--id", "BA.2", "--title", "ba2", "--parent", "BA", "--run", "o1"], order)
    once(["task", "add", "--root", "--id", "BB", "--title", "bb", "--run", "o1"], order)
    pre_ids = [t["id"] for t in once(["run", "get", "--id", "o1"], order)["run"]["tasks"]]
    assert pre_ids == ["BA", "BA.1", "BA.2", "BB"], pre_ids
    once(["task", "set", "BB", "status=building", "--run", "o1"], order)  # an ordinary write
    post_ids = [t["id"] for t in once(["run", "get", "--id", "o1"], order)["run"]["tasks"]]
    assert post_ids == ["BA", "BA.1", "BA.2", "BB"], post_ids
    for rel in ("runs/o1/run.json", "runs/o1/board.md", "runs/index.json"):
        assert not os.path.exists(os.path.join(order, rel)), rel

    # L4: `store enable` no longer sweeps mk-enable-* dirs (the enable path is gone; the store is
    # always on, so no such dir is ever created). Any stray dir is simply left alone.
    stale = os.path.join(tmp, "stale")
    once(["run", "init", "--id", "s1", "--feature", "S", "--branch", "bs"], stale)
    old = os.path.join(stale, "mk-enable-old")
    os.makedirs(old)
    once(["store", "enable"], stale)  # a no-op: it must not touch an unrelated dir
    assert os.path.exists(old), "a no-op enable removed an unrelated dir"

    # M2 R2: `ready` survives the next ordinary write, and a regress demotes it.
    # Through the real CLI, not mkdb.promote_ready directly.
    rd = os.path.join(tmp, "ready")
    once(["run", "init", "--id", "rr", "--feature", "Ready", "--branch", "br"], rd)
    once(["store", "enable"], rd)
    once(["task", "add", "--root", "--id", "A", "--title", "dep", "--run", "rr"], rd)
    once(["task", "add", "--root", "--id", "B", "--title", "needs A", "--deps", "A", "--run", "rr"], rd)
    once(["task", "add", "--root", "--id", "Z", "--title", "unrelated", "--run", "rr"], rd)
    once(["task", "set", "A", "status=done", "--run", "rr"], rd)

    def store_status(tid):
        c = _mkdb().open_project(H, STORE_PROJECT_FALLBACK, data_dir=rd)
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
    # L7.3: the root card copy is gone; the ready flag is a project-store fact (checked above).
    # a regress demotes: A done -> fix puts B back to planned
    once(["task", "set", "A", "status=fix", "--run", "rr"], rd)
    assert store_status("B") == "planned", "a regressed dep left B " + store_status("B")
    view = once(["task", "get", "B", "--run", "rr"], rd)["task"]
    assert view["ready"] is False, view

    # M2 R3: a routed task whose deps are met stays routed, in both orders.
    rt = os.path.join(tmp, "routed")
    once(["run", "init", "--id", "rr", "--feature", "Routed", "--branch", "br"], rt)
    once(["store", "enable"], rt)
    once(["task", "add", "--root", "--id", "A", "--title", "dep", "--run", "rr"], rt)
    once(["task", "add", "--root", "--id", "B", "--title", "needs A", "--deps", "A", "--run", "rr"], rt)
    once(["task", "add", "--root", "--id", "Z", "--title", "unrelated", "--run", "rr"], rt)
    once(["task", "set", "A", "status=done", "--run", "rr"], rt)
    got = once(["task", "set", "B", "status=routed", "--run", "rr"], rt)
    assert got["task"]["status"] == "routed" and got["task"]["ready"] is True, got["task"]
    once(["task", "set", "Z", "status=building", "--run", "rr"], rt)
    view = once(["task", "get", "B", "--run", "rr"], rt)["task"]
    assert view["status"] == "routed" and view["ready"] is True, view

    rt2 = os.path.join(tmp, "routed2")
    once(["run", "init", "--id", "rr", "--feature", "Routed", "--branch", "br"], rt2)
    once(["store", "enable"], rt2)
    once(["task", "add", "--root", "--id", "A", "--title", "dep", "--run", "rr"], rt2)
    once(["task", "add", "--root", "--id", "B", "--title", "needs A", "--deps", "A", "--run", "rr"], rt2)
    once(["task", "set", "B", "status=routed", "--run", "rr"], rt2)
    once(["task", "set", "A", "status=done", "--run", "rr"], rt2)
    view = once(["task", "get", "B", "--run", "rr"], rt2)["task"]
    assert view["status"] == "routed" and view["ready"] is True, view

    # M2 R4: a routed task with met deps is not demoted and re-promoted on every write.
    # Three unrelated writes after the promotion add no change_log row for B.
    ch = os.path.join(tmp, "churn")
    once(["run", "init", "--id", "rr", "--feature", "Churn", "--branch", "br"], ch)
    once(["store", "enable"], ch)
    once(["task", "add", "--root", "--id", "A", "--title", "dep", "--run", "rr"], ch)
    once(["task", "add", "--root", "--id", "B", "--title", "needs A", "--deps", "A", "--run", "rr"], ch)
    once(["task", "add", "--root", "--id", "Z", "--title", "unrelated", "--run", "rr"], ch)
    once(["task", "set", "A", "status=done", "--run", "rr"], ch)
    once(["task", "set", "B", "status=routed", "--run", "rr"], ch)

    def b_changes():
        c = _mkdb().open_project(H, STORE_PROJECT_FALLBACK, data_dir=ch)
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

    # F2: a follow-up and a re-parent reach the store, so `mkdb plans` nests the moved task
    # under its new parent and both parents roll up there too.
    rp = os.path.join(tmp, "reparent")
    once(["run", "init", "--id", "r1", "--feature", "F2", "--branch", "br"], rp)
    once(["store", "enable"], rp)
    con = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=rp)
    try:
        con.execute("INSERT INTO plans(id, version, title, approved_at, archived_path) VALUES (?,?,?,?,?)",
                    ("f2plan", 1, "F2", "2026-01-01T00:00:00+00:00", os.path.join(tmp, "f2plan-v1.md")))
        con.commit()
    finally:
        con.close()
    once(["run", "set", "--id", "r1", "--plan-id", "f2plan"], rp)
    once(["task", "add", "--root", "--id", "P1", "--title", "phase", "--run", "r1"], rp)
    once(["task", "add", "--root", "--id", "P2", "--title", "other phase", "--run", "r1"], rp)
    once(["task", "add", "--root", "--id", "T1", "--title", "movable", "--run", "r1"], rp)
    once(["task", "set", "T1", "parent=P1", "--run", "r1"], rp)

    def f2_rows():
        c = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=rp)
        try:
            return {r["id"]: (r["parent"], r["type"], r["status"]) for r in
                    c.execute("SELECT id, parent, type, status FROM tasks WHERE run='r1'")}
        finally:
            c.close()

    def f2_phases():
        got = db.plans(H, STORE_PROJECT_FALLBACK, data_dir=rp, plan_id="f2plan")
        return {p["id"]: [x["id"] for x in p["tasks"]] for p in got["phases"]}

    assert f2_rows()["T1"][:2] == ("P1", "task"), f2_rows()  # the store row carries parent and type
    assert f2_phases() == {"P1": ["T1"], "P2": []}, f2_phases()  # nested under the new parent
    once(["task", "set", "T1", "status=merged", "--run", "r1"], rp)
    assert f2_rows()["P1"][2] == "merged", f2_rows()  # the new parent rolls up from its new child
    once(["task", "set", "T1", "parent=", "--run", "r1"], rp)
    assert f2_rows()["T1"][:2] == (None, "phase"), f2_rows()  # a root again, and a phase
    assert f2_phases() == {"P1": [], "P2": [], "T1": []}, f2_phases()

    # a follow-up under the merged phase P2 lands there and rolls it back to building
    once(["task", "set", "T1", "status=merged", "--run", "r1"], rp)
    once(["task", "set", "P2", "status=merged", "--run", "r1"], rp)
    once(["task", "add", "--id", "T2", "--title", "follow-up", "--parent", "P2", "--followup",
          "--run", "r1"], rp)
    assert f2_rows()["P2"][2] == "building", f2_rows()
    assert f2_phases() == {"P1": [], "P2": ["T2"], "T1": []}, f2_phases()


def no_file_views_self_test(tmp, env):
    """L4.1a: the project store is the only source of truth. After ANY mk-state command no
    run.json / index.json / task.json / events.jsonl / board.md is written (and no run-doc /
    index-doc artifact). FAILS on main, where every command rewrites the file views and the
    store stays off until `store enable` — a command L4 drops, so this helper never calls it."""

    H = os.environ["MK_HOME"]  # Z3: the one real home (root projection)
    data = os.path.join(tmp, "no-file-views")

    def mk(*args, code=0, e=None):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, "--data", data],
                           capture_output=True, text=True, env=e or env)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    VIEWS = ("run.json", "task.json", "events.jsonl", "board.md", "index.json")

    def view_roots():
        roots = [os.path.join(data, "runs")]              # today: <data>/runs/**
        c2 = os.path.join(H, "projects")   # migrated C2 folder
        if os.path.isdir(c2):
            roots.append(c2)
        return roots

    def no_views(cmd):
        for root in view_roots():
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    if name in VIEWS:
                        raise AssertionError("L4.1a: `mk-state %s` wrote the file view %s"
                                             % (" ".join(cmd), os.path.join(dirpath, name)))

    def step(*args, e=None, code=0):
        out = mk(*args, code=code, e=e)
        no_views(args)  # the invariant is checked after EVERY command
        return out

    plan = os.path.join(tmp, "nfv-plan.json")
    with open(plan, "w") as fh:
        json.dump({"plan": "nfvplan", "status": "approved", "runs": [], "phases": [
            {"id": "P1", "title": "Phase one", "wave": 1, "deps": [], "owned": ["scripts/a.py"],
             "route": "", "reviewer": "", "size": "standard", "risk": "reversible", "review": "light",
             "qa": [], "commit": "", "subagents": False, "lane_group": "", "tasks": [
                 {"id": "P1.1", "title": "First", "acceptance": "a1", "refs": [], "subagent": False,
                  "owned": ["scripts/a.py"]}]}]}, fh)
    fixture = os.path.join(tmp, "nfv-planfix.py")
    with open(fixture, "w") as fh:
        fh.write(PLAN_FIXTURE)
    os.chmod(fixture, 0o755)
    e2 = dict(env, MK_PLAN_CMD=f"{sys.executable} {fixture}")

    step("run", "init", "--id", "nfv", "--feature", "NoFileViews", "--branch", "b",
         "--plan", plan, "--force", e=e2)
    step("task", "add", "--root", "--id", "X1", "--title", "extra", "--run", "nfv")
    step("task", "set", "X1", "status=routed", "--run", "nfv")
    step("task", "set", "X1", "status=building", "--note", "start", "--run", "nfv")
    step("event", "--task", "X1", "--type", "note", "--text", "hello", "--run", "nfv")
    step("task", "round", "X1", "--kind", "build", "--run", "nfv")
    step("task", "verify", "X1", "--tests", "100/0", "--tsc", "0", "--lint", "0",
         "--selftest", "8/0", "--run", "nfv")
    step("run", "set", "--id", "nfv", "--next", "dispatch the next task")
    step("plan", "sync", "--run", "nfv", "--apply", "--from-file", e=e2)
    # F.0: P1, P1.1 and X1 are still open, so the close is refused naming them
    e = step("run", "set", "--id", "nfv", "--status", "done", code=2)
    assert "not closed" in e["error"] and "X1 (review)" in e["error"], e
    for tid in ("P1", "P1.1", "X1"):
        step("task", "set", tid, "status=done", "--run", "nfv")  # the real close path
    step("run", "set", "--id", "nfv", "--status", "done")  # "run close"

    con = _mkdb().open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        run_row = con.execute("SELECT id, status FROM runs WHERE id='nfv'").fetchone()
        assert run_row is not None, "L4.1a: the store holds no run row for nfv"
        assert run_row["status"] == "done", dict(run_row)
        tasks = {r["id"]: r["status"] for r in
                 con.execute("SELECT id, status FROM tasks WHERE run='nfv'")}
        assert {"P1", "P1.1", "X1"} <= set(tasks), tasks
        assert tasks["X1"] == "done", tasks  # F.0: task verify passed -> review, then the close path moved it
        n_ev = con.execute("SELECT COUNT(*) c FROM events WHERE run='nfv'").fetchone()["c"]
        assert n_ev > 0, n_ev
        n_log = con.execute("SELECT COUNT(*) c FROM change_log WHERE run='nfv'").fetchone()["c"]
        assert n_log > 0, n_log
        n_doc = con.execute("SELECT COUNT(*) c FROM artifacts WHERE run='nfv'"
                            " AND kind IN ('run-doc','index-doc')").fetchone()["c"]
        assert n_doc == 0, "L4.1a: %d run-doc/index-doc artifacts were written" % n_doc
    finally:
        con.close()


def cross_run_dep_from_db_self_test(tmp, env):
    """L4.1b: a <run>:<id> dep resolves from the project DB, not from runs/<other>/run.json.
    FAILS on main: with no store, dep_status reads runs/ra/run.json; delete it and the dep is
    unknown, so the dependent never becomes ready."""

    H = os.environ["MK_HOME"]  # Z3: the one real home (root projection)
    data = os.path.join(tmp, "xdep")

    def mk(*args, code=0):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, "--data", data],
                           capture_output=True, text=True, env=env)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    mk("run", "init", "--id", "ra", "--feature", "A", "--branch", "a")
    mk("run", "init", "--id", "rb", "--feature", "B", "--branch", "b")
    mk("task", "add", "--root", "--id", "R", "--title", "Reasoning", "--run", "ra")
    mk("task", "add", "--root", "--id", "A2", "--title", "Waits", "--deps", "ra:R", "--run", "rb")
    assert mk("task", "get", "A2", "--run", "rb")["task"]["ready"] is False
    # remove any file view the other run might still have: the DB is the only source now
    for name in ("run.json", "task.json"):
        p = os.path.join(data, "runs", "ra", name)
        if os.path.exists(p):
            os.remove(p)
    taskjson = os.path.join(data, "runs", "ra", "tasks", "R", "task.json")
    if os.path.exists(taskjson):
        os.remove(taskjson)
    mk("task", "set", "R", "status=merged", "--run", "ra")   # writes ra only
    assert mk("task", "get", "A2", "--run", "rb")["task"]["ready"] is True, \
        "cross-run dep did not resolve from the DB"
    con = _mkdb().open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        assert con.execute("SELECT status FROM tasks WHERE run='ra' AND id='R'").fetchone()["status"] == "merged"
    finally:
        con.close()


def no_field_blanked_self_test(tmp, env):
    """L4.1c: a status write must not reset agent/model/lane/owned/deps/acceptance/title, nor the
    artifact-only fields wave/gates/rounds/last_commit/workspace. FAILS on main if the store
    overlay drops them (or, pre-L4, never stores them at all)."""

    H = os.environ["MK_HOME"]  # Z3: the one real home (root projection)
    data = os.path.join(tmp, "keepfields")

    def mk(*args, code=0):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, "--data", data],
                           capture_output=True, text=True, env=env)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    mk("run", "init", "--id", "kf", "--feature", "Keep", "--branch", "b")
    mk("task", "add", "--root", "--id", "T", "--title", "Keep me", "--wave", "3", "--owned", "a.py,b.py", "--run", "kf")
    mk("task", "set", "T", "status=routed", "--run", "kf")
    mk("task", "set", "T", "agent=codex", "model=gpt-5", "effort=high", "lane=/tmp/lane",
       "workspace=mk:repo:1", "branch=mk/kf/t", "slot=1", "gates={\"test\":\"pass\"}",
       "last_commit=abc1234", "acceptance=the outcome", "--run", "kf")
    mk("task", "round", "T", "--kind", "build", "--run", "kf")
    before = mk("task", "get", "T", "--run", "kf")["task"]
    keep_keys = ("agent", "model", "effort", "lane", "workspace", "branch", "slot", "gates",
                 "acceptance", "owned", "title", "wave", "round", "rounds", "last_commit")
    snap = {k: before.get(k) for k in keep_keys}
    assert snap["gates"] == {"test": "pass"} and snap["round"] == 1, snap
    mk("task", "set", "T", "status=building", "--note", "advance", "--run", "kf")
    after = mk("task", "get", "T", "--run", "kf")["task"]
    assert after["status"] == "building", after
    for k in keep_keys:
        assert after.get(k) == snap[k], (k, snap[k], after.get(k))
    con = _mkdb().open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        row = con.execute("SELECT route_json, status FROM tasks WHERE run='kf' AND id='T'").fetchone()
        assert row["status"] == "building"
        assert json.loads(row["route_json"])["agent"] == "codex"
    finally:
        con.close()


def r2_fixes_self_test(tmp, env):
    """R2 review fixes: (1) a load+save round trip keeps every stored task column, `parallel`
    included; (2) `run list` reads a legacy run's plan path from runs.plan and its feature from
    the run-doc extras on a pre-L4 store; (3) dep_status matches a cross-run id case-insensitively;
    (4) the importer's TASK_FILE_KEYS carries deps. (1) and (2) FAIL on bf55ee0."""

    H = os.environ["MK_HOME"]  # Z3: the one real home (root projection)
    data = os.path.join(tmp, "r2fix")
    home = H

    def mk(*args, code=0):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, "--data", data],
                           capture_output=True, text=True, env=env)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    db = _mkdb()
    # (1) parallel round trip: set the column, load, save through an ordinary write, read it back
    mk("run", "init", "--id", "par", "--feature", "P", "--branch", "b")
    mk("task", "add", "--root", "--id", "PT", "--title", "par", "--run", "par")
    con = db.open_project(home, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        con.execute("UPDATE tasks SET parallel=1 WHERE run='par' AND id='PT'")
        con.commit()
    finally:
        con.close()
    assert mk("task", "get", "PT", "--run", "par")["task"].get("parallel") is True, \
        "load_store dropped the parallel column"
    mk("task", "set", "PT", "status=building", "--run", "par")  # an ordinary load+save
    con = db.open_project(home, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        assert con.execute("SELECT parallel FROM tasks WHERE run='par' AND id='PT'").fetchone()["parallel"] == 1, \
            "a load+save reset tasks.parallel to 0"
    finally:
        con.close()
    # every other column task_row writes must round-trip too: compare the stored row to the load
    con = db.open_project(home, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        stored = dict(con.execute("SELECT * FROM tasks WHERE run='par' AND id='PT'").fetchone())
    finally:
        con.close()
    loaded = mk("task", "get", "PT", "--run", "par")["task"]
    for col in ("id", "parent", "type", "title", "status", "lane", "slot", "branch", "pane",
                "round", "verdict", "updated"):
        got = loaded.get(col)
        assert got == (stored[col] if col != "round" else (stored[col] or 0)), (col, got, stored[col])

    # (2) legacy run list: a run-doc artifact + runs.plan only (no extra_json, no plan_id)
    mk("run", "init", "--id", "leg", "--feature", "Legacy", "--branch", "b2")
    con = db.open_project(home, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        # a pre-L4 store keeps the run-level extras only in the run-doc artifact (the old
        # mk-state wrote it); write one directly, then blank the columns exactly as L4 sees them
        con.execute("INSERT INTO artifacts(run, task, round, kind, rev, text, created)"
                    " VALUES ('leg', NULL, 0, 'run-doc', 1, ?, '2020-01-01T00:00:00+00:00')",
                    (json.dumps({"id": "leg", "feature": "LegacyFromDoc", "plan": "plans/legacy.md",
                                 "tasks": [{"id": "LG", "title": "lg", "status": "planned"}]}),))
        con.execute("UPDATE runs SET plan='plans/legacy.md', extra_json=NULL WHERE id='leg'")
        con.execute("UPDATE tasks SET extra_json=NULL WHERE run='leg'")
        con.commit()
    finally:
        con.close()
    rows = {r["id"]: r for r in mk("run", "list")["runs"]}
    assert rows["leg"]["plan"] == "plans/legacy.md", rows["leg"]
    assert rows["leg"]["feature"] == "LegacyFromDoc", rows["leg"]  # from the run-doc extras fallback

    # (3) dep_status matches a cross-run id case-insensitively
    mk("run", "init", "--id", "ra", "--feature", "A", "--branch", "a")
    mk("task", "add", "--root", "--id", "RT", "--title", "dep", "--run", "ra")
    mk("run", "init", "--id", "rb", "--feature", "B", "--branch", "b")
    mk("task", "add", "--root", "--id", "B2", "--title", "waits", "--deps", "RA:RT", "--run", "rb")
    mk("task", "set", "RT", "status=merged", "--run", "ra")
    mk("task", "set", "B2", "status=building", "--run", "rb")
    con = db.open_project(home, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        assert con.execute("SELECT status FROM tasks WHERE run='rb' AND id='B2'").fetchone()["status"] == "building", \
            "an upper-case run id in a dep did not resolve"
    finally:
        con.close()


def stray_writers_self_test(tmp, env):
    """L10.7: a full run cycle on a MIGRATED project (root `projects.db_path` set) writes only the
    project DB. The legacy store, projects.json and the legacy data dir keep their mtime/size, no
    run-folder copy (run.json / events.jsonl / board.md / task.json) appears anywhere, and
    `run set --status done` archives the run's bus rows to <plan folder>/runs/<run>/bus.jsonl.
    FAILS on e506cd6: register_project rewrote projects.json, and nothing archived the bus."""
    home, repo = os.path.join(tmp, "sw-home"), os.path.join(tmp, "sw-repo")
    legacy, slug = os.path.join(repo, ".matrixkoder"), "sw-proj"
    os.makedirs(os.path.join(repo, ".git"))
    os.makedirs(legacy)
    e = {k: v for k, v in env.items() if k != "MK_SELFTEST_FALLBACK_SLUG"}
    e.update(MK_HOME=home, MATRIXKODER_HOME=home, MK_SELFTEST="1", MK_STATE_LOADAVG="0")
    db, paths = _mkdb(), mkpaths
    c2 = os.path.join(home, "projects", slug)
    lc = db.open_project(home, data_dir=legacy, legacy=True)  # the pre-switch store
    lc.close()
    write_json(os.path.join(home, "projects.json"), {os.path.realpath(repo): {"data_dir": legacy, "slug": slug}})
    root = db.open_root(home)
    try:
        root.execute("INSERT OR REPLACE INTO projects(slug, name, repo, data_dir, mode, db_path) VALUES (?,?,?,?,?,?)",
                     (slug, slug, os.path.realpath(repo), os.path.realpath(legacy), "home",
                      os.path.join(c2, os.path.basename(paths.project_db(data=legacy, legacy=True)))))
        root.commit()
    finally:
        root.close()
    db.open_project(home, data_dir=legacy).close()  # the C2 store the switch made

    def snap():
        out = {}
        for base in (legacy, os.path.join(home, "projects.json")):
            for dirpath, _d, files in (os.walk(base) if os.path.isdir(base) else [(os.path.dirname(base), [], [os.path.basename(base)])]):
                for f in files:
                    st = os.stat(os.path.join(dirpath, f))
                    out[os.path.join(dirpath, f)] = (st.st_mtime_ns, st.st_size)
        return out
    before = snap()
    assert any(k.endswith("projects.json") for k in before) and any(k.endswith(".db") for k in before), before

    def run(*args, code=0):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, "--data", legacy],
                           capture_output=True, text=True, env=e, cwd=repo)
        lines = r.stdout.strip().splitlines()
        assert lines and r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[-1])

    run("run", "init", "--id", "swr", "--feature", "F", "--branch", "b")
    run("run", "set", "--run", "swr", "--plan-id", "swplan")
    run("task", "add", "--root", "--id", "T1", "--title", "one", "--run", "swr")
    for st_ in ("routed", "building", "verifying", "review", "merged"):
        run("task", "set", "T1", f"status={st_}", "--run", "swr")
    ev = db.append_event(home, None, "swr", task="T1", chan="bus", kind="info", from_id="orch", to_id="T1",
                         text="hello bus", data_dir=legacy)
    assert ev, ev
    out = run("run", "set", "--run", "swr", "--status", "done")
    arch = out.get("bus_archive") or {}
    want = os.path.join(c2, "plans", "swplan", "runs", "swr", "bus.jsonl")
    assert arch.get("path") == want and arch.get("messages") == 1 and os.path.isfile(want), out
    assert "hello bus" in open(want).read(), open(want).read()
    # the legacy store, projects.json and the legacy dir are untouched
    assert snap() == before, {k: (before.get(k), v) for k, v in snap().items() if before.get(k) != v}
    # no run-folder copy anywhere under the home or the repo
    copies = {os.path.basename(x) for x in (mkpaths.run_doc("d", "r"), mkpaths.board_doc("d", "r"), mkpaths.task_doc("d", "r", "t"),
                                            mkpaths.events_doc("d", "r"))}
    bad = [os.path.join(d, f) for base in (home, repo) for d, _x, fs in os.walk(base) for f in fs
           if f in copies or (f == "bus.jsonl" and os.path.join(d, f) != want)]
    assert not bad, bad
    # a failing archive is loud: the retry hint, a non-zero exit, never a silent done
    run("run", "init", "--id", "swr2", "--feature", "F", "--branch", "b2")
    run("run", "set", "--run", "swr2", "--plan-id", "swplan")
    os.makedirs(os.path.join(c2, "plans", "swplan", "runs", "swr2"))
    write_json(os.path.join(c2, "plans", "swplan", "runs", "swr2", "bus.jsonl"), {"x": 1})
    db.append_event(home, None, "swr2", chan="bus", kind="info", from_id="orch", to_id="x", text="m", data_dir=legacy)
    bad_out = run("run", "set", "--run", "swr2", "--status", "done", code=2)
    assert "bus archive failed" in bad_out.get("error", "") and "mk-bus.py archive-run" in bad_out["error"], bad_out


def gate_run_self_test(tmp, env):
    """`gate run`: evidence rows (enum result, tree_sha), a timeout that kills the whole process
    group, load deferral, a bounded output tail with the full-stream sha256, and the registered
    repo winning over the cwd repo. Every case FAILS on 7ae641f (no `gate` command, and
    subprocess.run leaves the timed-out gate's children alive)."""
    H = env["MK_HOME"]  # the home the child processes use (os.environ drifts in earlier cases)
    DB = _mkdb()
    repo, other, data = (os.path.join(tmp, n) for n in ("gr-repo", "gr-other", "gr-data"))
    for d in (repo, other):
        os.makedirs(d)
        subprocess.run(["git", "init", "-q", d], check=True)
        open(os.path.join(d, "f.txt"), "w").write(d)
        subprocess.run(["git", "-C", d, "add", "."], check=True)
        subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"],
                       check=True)
    open(os.path.join(repo, "marker.txt"), "w").write("here\n")  # only the registered repo has it
    tree = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD^{tree}"], capture_output=True,
                          text=True, check=True).stdout.strip()
    con = DB.open_root(H)
    try:
        con.execute("INSERT OR REPLACE INTO projects(slug, name, repo, data_dir, mode, db_path)"
                    " VALUES ('grproj','grproj',?,?,'home',NULL)", (repo, data))
        con.commit()
    finally:
        con.close()

    def gr(*args, code=0, e=None):
        # cwd is a DIFFERENT git repo: only the registered row can point the gate at `repo`
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, "--data", data],
                           capture_output=True, text=True, env=e or dict(env, MK_STATE_LOADAVG="0"), cwd=other, timeout=120)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    def evidence():
        c = DB.open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
        try:
            return [dict(r) for r in c.execute("SELECT * FROM evidence ORDER BY id")]
        finally:
            c.close()

    pidfile = os.path.join(tmp, "gr-child.pid")
    gr("run", "init", "--id", "gr", "--feature", "GR", "--branch", "b")
    gr("task", "add", "--root", "--id", "G1", "--title", "gated", "--run", "gr")
    def setx(table, where, key, val):
        """Merge one key into a row's extra_json (what helper A's `gate` key does), keeping the rest."""
        c = DB.open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
        try:
            cur = json.loads(c.execute(f"SELECT extra_json FROM {table} WHERE {where}").fetchone()[0] or "{}")
            cur[key] = val
            c.execute(f"UPDATE {table} SET extra_json=? WHERE {where}", (json.dumps(cur),))
            c.commit()
        finally:
            c.close()

    g1 = "run='gr' AND id='G1'"
    setx("tasks", g1, "gate", [
        {"cmd": "test -f marker.txt", "lines": ["L1", "L2"]},
        {"cmd": "exit 3"},
        {"cmd": f"sleep 60 & echo $! > {pidfile}; wait", "timeout": 1},
        {"cmd": "yes abcdefghi | head -c 1048576", "id": "big"}])
    setx("runs", "id='gr'", "final_gates", [
        {"id": "fin", "cmd": "true", "every": "merge", "covers": ["L1"]},
        {"id": "fin2", "cmd": "true", "every": "end"}])

    out = gr("gate", "run", "G1", "--run", "gr", "--ref", "abc")
    assert out["ok"] is True and out["all_green"] is False, out
    assert os.path.realpath(out["where"]) == os.path.realpath(repo), out  # registered repo, not cwd
    rows = evidence()
    assert [r["result"] for r in rows] == ["green", "red", "timeout", "green"], rows
    assert all(r["result"] in EVIDENCE_RESULTS for r in rows)
    assert all(r["tree_sha"] == tree and r["run"] == "gr" and r["task"] == "G1" for r in rows), rows
    assert rows[0]["line"] == "L1,L2" and rows[0]["by"] == "orchestrator" and rows[0]["ref"] == "abc", rows[0]
    assert rows[0]["gate"] == "G1#1" and rows[3]["gate"] == "big", rows
    assert rows[1]["output_sha256"] == hashlib.sha256(b"").hexdigest(), rows[1]
    # timeout kills the whole process group: the gate's `sleep` child is dead
    child = int(open(pidfile).read())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child, 9)
        raise AssertionError("the timed-out gate's child process survived")
    # bounded tail + full-stream sha256
    full = (b"abcdefghi\n" * 200000)[:1048576]
    big = rows[3]
    assert 0 < len(big["output_tail"].encode()) <= GATE_TAIL_BYTES, len(big["output_tail"])
    assert full.endswith(big["output_tail"].encode()), "the tail is not the end of the output"
    assert big["output_sha256"] == hashlib.sha256(full).hexdigest(), big["output_sha256"]
    # the same kill/bound through the F.0 runner directly (its contract is unchanged)
    res, tail = _gate_run({"cmd": f"sleep 60 & echo $! > {pidfile}; wait", "timeout": 1}, repo)
    assert res == "timeout" and tail == "", (res, tail)
    with contextlib.suppress(ProcessLookupError):
        os.kill(int(open(pidfile).read()), 0)
        raise AssertionError("_gate_run left the child alive")
    assert _gate_run({"cmd": "echo hi; exit 2"}, repo) == ("fail exit=2", "hi")

    # F.3b: banned_commands (project.yaml) — a matching gate is `banned`, never run, and blocks
    bmark = os.path.join(tmp, "gr-banned-ran")
    assert banned_commands(os.path.join(tmp, "no-such-data")) == []  # no yaml: no ban
    assert _gate_run({"cmd": f"touch {bmark}"}, repo, ["touch"]) [0] == "banned" and not os.path.exists(bmark)
    assert _gate_run({"cmd": f"cd {repo} && touch {bmark}"}, repo, ["cd"])[0] == "banned"
    assert _gate_run({"cmd": "npm run devtools"}, repo, ["npm run dev"])[0] != "banned"  # word match
    assert _banned_hit("cd x && npm run dev", ["npm run dev"]) == "npm run dev"
    assert _gate_run({"cmd": "echo ok"}, repo, ["touch"]) == ("pass", "")  # unbanned unchanged
    bdata = os.path.join(tmp, "bdata")
    os.makedirs(bdata)
    with open(os.path.join(bdata, "matrixkoder.yaml"), "w") as f:
        f.write("banned_commands:\n  - npm run dev   # servers\n  - \"touch\"\nqa:\n  start: x\n")
    assert banned_commands(bdata) == ["npm run dev", "touch"], banned_commands(bdata)
    with open(os.path.join(bdata, "matrixkoder.yaml"), "w") as f:
        f.write("banned_commands: [\"npm run dev\", 'touch']\n")
    assert banned_commands(bdata) == ["npm run dev", "touch"]
    with open(os.path.join(bdata, "matrixkoder.yaml"), "w") as f:
        f.write("banned_commands: []\n")
    assert banned_commands(bdata) == []

    # deferred under load: a deferred row, nothing run
    before = len(evidence())
    marker = os.path.join(tmp, "gr-ran")
    task_gates = [{"cmd": f"touch {marker}"}]
    final_gates = [{"id": "fin", "cmd": "true", "every": "merge", "covers": ["L1"]},
                   {"id": "fin2", "cmd": "true", "every": "end"}]

    def regate():  # a task/run write rewrites extra_json from the file keys; helper A makes these keys ride
        setx("tasks", g1, "gate", task_gates)
        setx("runs", "id='gr'", "final_gates", final_gates)
    regate()
    out = gr("gate", "run", "G1", "--run", "gr", e=dict(env, MK_STATE_LOADAVG="41"))
    rows = evidence()
    assert len(rows) == before + 1 and rows[-1]["result"] == "deferred" and not os.path.exists(marker), rows[-1]
    assert out["all_green"] is False, out

    # F.3b through the CLI: a banned task gate records an error row (detail banned) and never runs;
    # lifting the ban lets the same gate run; no project yaml means no ban
    ypath = mkpaths.project_yaml(data)
    assert not os.path.exists(ypath), ypath
    out = gr("gate", "run", "G1", "--run", "gr")
    assert os.path.exists(marker) and out["all_green"] is True, out  # no yaml: unbanned
    os.remove(marker)
    with open(ypath, "w") as f:
        f.write("banned_commands:\n  - touch\n")
    before = len(evidence())
    out = gr("gate", "run", "G1", "--run", "gr")
    assert out["evidence"][0]["detail"] == "banned" and out["all_green"] is False, out
    assert not os.path.exists(marker) and len(evidence()) == before + 1, "banned gate ran"
    assert "banned command 'touch'" in evidence()[-1]["output_tail"], evidence()[-1]
    os.remove(ypath)

    # --final runs every final gate; --due runs the merge gates only once a task is merged
    out = gr("gate", "run", "--final", "--run", "gr")
    rows = evidence()
    assert [r["gate"] for r in rows[-2:]] == ["fin", "fin2"] and rows[-1]["task"] is None, rows[-2:]
    assert rows[-2]["line"] == "L1" and out["all_green"] is True, rows[-2:]
    assert gr("gate", "run", "--due", "--run", "gr")["evidence"] == []
    gr("task", "set", "G1", "status=review", "--run", "gr")
    regate()
    due = gr("gate", "run", "--due", "--run", "gr")
    assert [r["gate"] for r in due["evidence"]] == ["G1#1"], due
    gr("task", "set", "G1", "status=merged", "--run", "gr")
    regate()
    assert [r["gate"] for r in gr("gate", "run", "--due", "--run", "gr")["evidence"]] == ["fin"]
    # exactly one of <task> | --final | --due
    assert gr("gate", "run", "--run", "gr", code=2)["ok"] is False
    assert gr("gate", "run", "G1", "--final", "--run", "gr", code=2)["ok"] is False


def lessons_lint_self_test():
    """F.11: every lessons.md row names what enforces its rule (a script path or `prose only`),
    and at most 20 rows may be prose only."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "matrixkoder", "reference", "lessons.md")
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")]
            for ln in open(path, encoding="utf-8").read().splitlines() if ln.startswith("|")]
    assert rows[0][-1] == "Enforced by", rows[0]
    data = [r for r in rows[2:] if r[0]]
    assert data and all(len(r) >= len(rows[0]) and r[-1] for r in data)  # >=: a cell may hold `a|b`, [r[0] for r in data if not r[-1]]
    prose = [r[0] for r in data if r[-1] == "prose only"]
    assert len(prose) <= 20, f"{len(prose)} lessons are prose only (cap 20): {prose}"


def rework_round_self_test(tmp, env):
    """F.4 needs: `task round --kind rework` moves a MERGED task to rework and is refused on any
    other status. Both cases fail on c97e8c5 (the kind does not exist there)."""
    data = os.path.join(tmp, "rw-data")

    def rw(*args, code=0):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, "--data", data],
                           capture_output=True, text=True, env=dict(env, MK_STATE_LOADAVG="0"),
                           cwd=tmp, timeout=120)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1 and r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    rw("run", "init", "--id", "rw", "--feature", "rw", "--branch", "b")
    rw("task", "add", "--id", "W", "--title", "w", "--run", "rw", "--root")
    bad = rw("task", "round", "W", "--kind", "rework", "--run", "rw", code=2)
    assert bad["ok"] is False and "merged" in bad["error"], bad  # planned: refused
    rw("task", "set", "W", "status=merged", "--run", "rw")
    out = rw("task", "round", "W", "--kind", "rework", "--run", "rw")
    assert out["ok"] is True and out["kind"] == "rework", out
    assert rw("task", "get", "W", "--run", "rw")["task"]["status"] == "rework"


def fidelity_self_test(tmp, env):
    """F.3: ONE fidelity_gate on every closing path (task set, fan-out, promote_closed, run set
    --status done, run archive), stale evidence does not count, manual lines need a sign-off, a
    legacy run is exempt, task set refuses risk keys, risk clear needs a fresh green evidence id.
    Every case FAILS on 7ae641f (no gate/risk commands, no fidelity check)."""
    H = env["MK_HOME"]
    DB = _mkdb()
    repo, data = os.path.join(tmp, "fi-repo"), os.path.join(tmp, "fi-data")
    os.makedirs(repo)

    def git(*args):
        return subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def bump(n):  # a new commit = a new tree = every earlier evidence is stale
        open(os.path.join(repo, "f.txt"), "w").write(str(n))
        git("add", "."), git("commit", "-qm", f"c{n}")

    git("init", "-q")
    bump(0)
    con = DB.open_root(H)
    try:
        con.execute("INSERT OR REPLACE INTO projects(slug, name, repo, data_dir, mode, db_path)"
                    " VALUES ('fiproj','fiproj',?,?,'home',NULL)", (repo, data))
        con.commit()
    finally:
        con.close()

    def fi(*args, code=0, e=None):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, "--data", data],
                           capture_output=True, text=True, env=e or dict(env, MK_STATE_LOADAVG="0"), cwd=tmp, timeout=120)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    def setx(table, where, key, val):
        c = DB.open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
        try:
            cur = json.loads(c.execute(f"SELECT extra_json FROM {table} WHERE {where}").fetchone()[0] or "{}")
            cur[key] = val
            c.execute(f"UPDATE {table} SET extra_json=? WHERE {where}", (json.dumps(cur),))
            c.commit()
        finally:
            c.close()

    GATE = [{"cmd": "true"}]
    FIN = [{"id": "fin", "cmd": "true", "every": "merge", "covers": ["T"]}]

    def mkrun(rid, fidelity=True, tasks=(), final=True):
        fi("run", "init", "--id", rid, "--feature", rid, "--branch", "b")
        for tid, parent in tasks:
            fi("task", "add", "--id", tid, "--title", tid, "--run", rid, *(["--parent", parent] if parent else ["--root"]))
            if not parent or True:
                setx("tasks", f"run='{rid}' AND id='{tid}'", "gate", GATE)
        if fidelity:
            setx("runs", f"id='{rid}'", "fidelity", True)
        if final:
            setx("runs", f"id='{rid}'", "final_gates", FIN)

    def refused(out, *words):
        assert out["ok"] is False and all(w in out["error"] for w in words), out
        return out

    def evidence_id(*args):
        out = fi("gate", "run", *args)
        assert out["all_green"] is True, out
        return out["evidence"][0]["id"]

    # 1. task set to a closed status
    mkrun("f1", tasks=[("A", None), ("B", None), ("M", None)], final=False)
    refused(fi("task", "set", "A", "status=done", "--run", "f1", code=2), "fidelity", "A:A#1 no evidence")
    ea = evidence_id("A", "--run", "f1")
    fi("task", "set", "A", "status=done", "--run", "f1")
    # stale: evidence from before a new commit does not count
    evidence_id("B", "--run", "f1")
    bump(1)
    refused(fi("task", "set", "B", "status=merged", "--run", "f1", code=2), "stale")
    evidence_id("B", "--run", "f1")
    fi("task", "set", "B", "status=merged", "--run", "f1")
    # a red latest row does not count either
    setx("tasks", "run='f1' AND id='M'", "gate", [{"cmd": "false"}])
    fi("gate", "run", "M", "--run", "f1")
    refused(fi("task", "set", "M", "status=done", "--run", "f1", code=2), "red")
    # manual line: never run, clears only on a recorded sign-off
    setx("tasks", "run='f1' AND id='M'", "gate", [])
    setx("tasks", "run='f1' AND id='M'", "manual", "eyeball it")
    refused(fi("task", "set", "M", "status=done", "--run", "f1", code=2), "M:manual no evidence")
    refused(fi("gate", "signoff", "M", "--run", "f1", code=2), "--ref")
    fi("gate", "signoff", "M", "--ref", "user", "--run", "f1")
    fi("task", "set", "M", "status=done", "--run", "f1")
    # a non-closing status needs no evidence
    mkrun("f1b", tasks=[("N", None)], final=False)
    fi("task", "set", "N", "status=building", "--run", "f1b")

    # 2. parent fan-out: the parent's OWN gate counts too (R1 only checked the leaves)
    mkrun("f2", tasks=[("P", None), ("K1", "P"), ("K2", "P")], final=False)
    refused(fi("task", "set", "P", "status=done", "--run", "f2", code=2), "K1:K1#1", "K2:K2#1", "P:P#1")
    fi("gate", "run", "K1", "--run", "f2"), fi("gate", "run", "K2", "--run", "f2")
    refused(fi("task", "set", "P", "status=done", "--run", "f2", code=2), "P:P#1 no evidence")
    fi("gate", "run", "P", "--run", "f2")
    assert fi("task", "set", "P", "status=done", "--run", "f2")["ok"] is True
    # an intermediate gated task of a 3-level tree is checked too
    mkrun("f2b", tasks=[("GP", None), ("M", "GP"), ("L", "M")], final=False)
    fi("gate", "run", "L", "--run", "f2b"), fi("gate", "run", "GP", "--run", "f2b")
    refused(fi("task", "set", "GP", "status=done", "--run", "f2b", code=2), "M:M#1 no evidence")
    fi("gate", "run", "M", "--run", "f2b")
    assert fi("task", "set", "GP", "status=done", "--run", "f2b")["ok"] is True

    # 2c. automatic roll-up: a gated parent never rolls up to a closed status without its evidence
    def status_of(rid, tid):
        return fi("task", "get", tid, "--run", rid)["task"]["status"]

    mkrun("f2r", tasks=[("RP", None), ("RK", "RP")], final=False)
    fi("gate", "run", "RK", "--run", "f2r")
    fi("task", "set", "RK", "status=done", "--run", "f2r")
    assert status_of("f2r", "RK") == "done" and status_of("f2r", "RP") not in CLOSED_STATUSES, status_of("f2r", "RP")
    fi("gate", "run", "RP", "--run", "f2r")
    fi("task", "set", "RK", "status=done", "--run", "f2r", "--note", "resave")  # any save rolls up again
    assert status_of("f2r", "RP") == "done", status_of("f2r", "RP")
    # a legacy run still rolls up exactly as before
    mkrun("f2l", fidelity=False, tasks=[("LP", None), ("LK", "LP")], final=False)
    fi("task", "set", "LK", "status=done", "--run", "f2l")
    assert status_of("f2l", "LP") == "done", status_of("f2l", "LP")

    # 3. run set --status done (open work first, then final gates, then stale tasks)
    mkrun("f3", tasks=[("X", None)])
    fi("gate", "run", "X", "--run", "f3")
    fi("task", "set", "X", "status=merged", "--run", "f3")
    bump(2)  # X's evidence is now stale
    refused(fi("run", "set", "--id", "f3", "--status", "done", code=2), "X:X#1 evidence is stale", "final:fin no evidence")
    fi("gate", "run", "X", "--run", "f3"), fi("gate", "run", "--final", "--run", "f3")
    refused(fi("run", "set", "--id", "f3", "--status", "done", code=2), "reconcile due", "X")  # F.5
    fi("reconcile", "--run", "f3")
    out = fi("run", "set", "--id", "f3", "--status", "done")
    assert out["run"]["status"] == "done" and out["promoted"] == ["X"], out

    # 4. run archive
    mkrun("f4")
    refused(fi("run", "archive", "--id", "f4", code=2), "final:fin no evidence")
    fi("gate", "run", "--final", "--run", "f4")
    assert fi("run", "archive", "--id", "f4")["run"]["status"] == "archived"

    # 5. promote_closed called directly (run set done swallows its errors after the status write)
    mkrun("f5", tasks=[("Y", None)], final=False)
    fi("gate", "run", "Y", "--run", "f5")
    fi("task", "set", "Y", "status=merged", "--run", "f5")
    bump(3)
    old = os.environ.copy()
    os.environ.update(env)
    try:
        st = Store(data)
        run = st.load("f5")
        try:
            list(promote_closed(st, run, "t"))
        except Fail as e:
            assert "Y:Y#1 evidence is stale" in str(e), e
        else:
            raise AssertionError("promote_closed promoted a task on stale evidence")
        assert run["tasks"][0]["status"] == "merged", run["tasks"]
    finally:
        os.environ.clear(), os.environ.update(old)

    # 6. a legacy (non-fidelity) run is exempt, gates or not
    mkrun("f6", fidelity=False, tasks=[("L", None)], final=True)
    fi("task", "set", "L", "status=done", "--run", "f6")
    assert fi("run", "set", "--id", "f6", "--status", "done")["run"]["status"] == "done"

    # 7. task set refuses every risk/gate key
    for k in ("risk=2", "base_risk=1", "gate=x", "critical=true", "manual=x"):
        refused(fi("task", "set", "A", k, "--run", "f1", code=2), "read-only")

    # 8. risk add / list / clear
    mkrun("f8", tasks=[("R", None)], final=False)
    rid = fi("risk", "add", "R", "--kind", "touches-auth", "--points", "2", "--run", "f8")
    assert rid["risk"] == 3, rid
    lst = fi("risk", "list", "R", "--run", "f8")
    assert [r["kind"] for r in lst["reasons"]] == ["touches-auth"] and lst["risk"] == {"R": 3}, lst
    refused(fi("risk", "clear", str(rid["id"]), "--run", "f8", code=2), "--evidence")
    refused(fi("risk", "clear", str(rid["id"]), "--evidence", "99999", "--run", "f8", code=2), "not a green gate run")
    refused(fi("risk", "clear", str(rid["id"]), "--evidence", str(ea), "--run", "f8", code=2), "not a green gate run")  # other task's
    er = evidence_id("R", "--run", "f8")
    bump(4)  # stale
    refused(fi("risk", "clear", str(rid["id"]), "--evidence", str(er), "--run", "f8", code=2), "not a green gate run")
    er = evidence_id("R", "--run", "f8")
    out = fi("risk", "clear", str(rid["id"]), "--evidence", str(er), "--run", "f8")
    assert out["risk"] == 1, out
    refused(fi("risk", "clear", str(rid["id"]), "--evidence", str(er), "--run", "f8", code=2), "not an open")


def x31_r2_fixes_self_test(tmp, env):
    """X3.1 R2 review fixes: (a) `task brief` stores on a temp data dir with the DEFAULT home
    (root_write short-circuited on 96fdbc3 -> nothing landed) and the default home gains no
    root projection; (b) a later store_commit re-importing the STORE-authored brief duplicated
    each rev row (2 revs -> 4 rows on 96fdbc3); (c) a hand-written brief file is ingested once
    and idempotently on later commits. All three FAIL on 96fdbc3."""

    H = os.environ["MK_HOME"]  # Z3: the one real home (root projection)
    db = _mkdb()

    # R2 scratch runner: x31_a_fails=1 marks a KNOWN-BUG run (the base 96fdbc3). A failing
    # base case stops its own run short so the other cases still get a fresh outcome.
    A_BUG = os.environ.get("x31_a_fails") == "1"
    RESULTS = {}

    def data_con(datadir):
        """One open_project connection for a data dir, caller closes (or use q_art)."""
        return db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=datadir)

    def q_art(datadir, sql, args=()):
        con = data_con(datadir)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def bare_env(home_dir):
        """Default-home env: HOME pinned, both home names and the fallback slug unset."""
        e = {k: v for k, v in env.items()
             if k not in ("MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA",
                          "MK_SELFTEST_FALLBACK_SLUG")}
        e["HOME"] = home_dir
        e["MK_SELFTEST"] = "1"
        return e

    # (a) temp data dir + default home (no MK_HOME/MATRIXKODER_HOME): run init, task add,
    # task round, task brief --file must store a kind='brief' artifact, byte-equal body,
    # ok/rev as asserted. FAILS on 96fdbc3: task_brief wrote through root_write, which
    # short-circuits to None under _skip_root_projection, so the reply was
    # {"ok": false, "error": "could not store the brief artifact"} and the project store
    # stayed empty. The guard is REAL_HOME-aware, so all writes here land under $HOME.
    def mk(data_dir, *args, code=0, e=None, stdin_text=None):
        """One CLI invocation against one data dir; -> its one JSON line.
        Zero extra args runs --self-test (a bare invocation needs one arg)."""
        assert args, "mk() needs at least one CLI argument"
        r = subprocess.run([sys.executable, os.path.abspath(__file__),
                            *args, "--data", data_dir],
                           capture_output=True, text=True, env=e or env,
                           input=stdin_text)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    def raw(data_dir, *args, e=None):
        """A raw subprocess.run (no JSON shape check) for the expected-failure case."""
        return subprocess.run([sys.executable, os.path.abspath(__file__),
                               *args, "--data", data_dir],
                              capture_output=True, text=True, env=e or env)

    a_home = os.path.join(tmp, "x31-default-home")
    a_data = os.path.join(tempfile.gettempdir(), "x31-data-" + os.path.basename(tmp))
    os.makedirs(a_home, exist_ok=True)
    os.makedirs(a_data, exist_ok=True)
    assert os.path.realpath(a_data).startswith(
        os.path.realpath(tempfile.gettempdir()) + os.sep)
    b_path = os.path.join(tmp, "x31-a-brief.md")
    with open(b_path, "w") as f:
        f.write("X31 R2 brief, round 1, default home\n")

    def a_mk(*args, code=0):
        return mk(a_data, *args, code=code, e=bare_env(a_home))
    assert not os.path.exists(os.path.join(a_home, ".matrixkoder")), \
        "the default home already has a root projection; the case wants a clean default home"
    a_mk("run", "init", "--id", "x31a", "--feature", "X31", "--branch", "ba")
    a_mk("task", "add", "--root", "--id", "T", "--title", "brief me", "--run", "x31a")
    a_mk("task", "round", "T", "--kind", "build", "--run", "x31a")
    try:
        b1 = a_mk("task", "brief", "T", "--file", b_path)
    except AssertionError as e:
        RESULTS["a"] = f"FAIL: task brief refused: {e!r}"
        if A_BUG:  # expected on the base; the other cases follow
            return RESULTS
        raise
    assert b1["ok"] is True and b1["round"] == 1 and b1["stored"] is True and b1["rev"] == 1, b1
    RESULTS["a"] = "ok"
    assert os.path.exists(b1["view"]) and open(b1["view"]).read() == open(b_path).read()
    rows = q_art(a_data, "SELECT text, rev FROM artifacts WHERE run='x31a' AND task='T'"
                         " AND round=1 AND kind='brief'")
    assert len(rows) == 1 and rows[0]["rev"] == 1 and rows[0]["text"] == open(b_path).read(), rows
    assert not os.path.exists(os.path.join(a_home, ".matrixkoder")), (  # no root projection
        "a temp data dir wrote a root projection into the default home")

    # (b) duplicate import: two brief revisions (v1 then v2 via --file), then one `task set`
    # and two `event --type note` writes, each reaching store_commit. Exactly TWO brief rows
    # (one per rev), the newest rev is 2 and holds the v2 bytes. FAILS on 96fdbc3: store_commit
    # walked _round_artifacts, which handed the STORE row back with a synthetic per-commit src,
    # so every later commit inserted one row per rev again (2 revs -> 4).
    b_data = os.path.join(tmp, "x31-dup")
    mk(b_data, "run", "init", "--id", "x31b", "--feature", "B", "--branch", "bb", e=env)
    mk(b_data, "task", "add", "--root", "--id", "B", "--title", "dup", "--run", "x31b", e=env)
    def b2_mk(*args, code=0):
        return mk(b_data, *args, e=env, code=code)
    v1 = os.path.join(tmp, "x31-b-v1.md")
    with open(v1, "w") as f:
        f.write("dup brief v1\n")
    paths = b2_mk("task", "round", "B", "--kind", "build", "--run", "x31b")
    b2_mk("task", "brief", "B", "--file", v1)
    with open(v1, "w") as f:
        f.write("dup brief v2\n")
    b2_mk("task", "brief", "B", "--file", v1)                     # second revision: rev 2
    b2_mk("task", "set", "B", "status=building", "--run", "x31b")  # a later store_commit
    b2_mk("event", "--run", "x31b", "--type", "note", "--text", "e1")
    b2_mk("event", "--run", "x31b", "--type", "note", "--text", "e2")
    rows = q_art(b_data, "SELECT rev, text FROM artifacts WHERE run='x31b' AND task='B'"
                         " AND kind='brief' ORDER BY rev")
    newest = rows[-1] if rows else None
    reason = None
    if len(rows) != 2:
        reason = f"expected exactly 2 brief rows (one per rev), got {len(rows)}: {rows!r}"
    elif [r["rev"] for r in rows] != [1, 2]:
        reason = f"revs are not [1, 2]: {rows!r}"
    elif newest["rev"] != 2 or newest["text"] != "dup brief v2\n":
        reason = f"newest is not rev 2 with the v2 bytes: {rows!r}"
    if reason:
        if A_BUG:
            RESULTS["b"] = f"FAIL: {reason}"  # expected on the base: recorded and continued
            return RESULTS
        raise AssertionError(f"x31 R2 (b): {reason}")
    RESULTS["b"] = "ok"

    # (c) hand-written brief file with no artifact rows: ONE row after ONE store_commit
    # (ingested once off the task folder), still ONE after a second commit (idempotent).
    # FAILS on 96fdbc3 (duplicate import, like (b)) and on any rev where the ingest src
    # changed per commit.
    c_data = os.path.join(tmp, "x31-file-ingest")
    def c_mk(*args, code=0):
        return mk(c_data, *args, e=env, code=code)
    c_mk("run", "init", "--id", "x31c", "--feature", "C", "--branch", "bc")
    c_mk("task", "add", "--root", "--id", "C1", "--title", "c", "--run", "x31c")
    c_con = data_con(c_data)
    try:
        c_con.execute("UPDATE tasks SET round=1 WHERE run='x31c' AND id='C1'")
        c_con.commit()
    finally:
        c_con.close()
    c_brief_dir = os.path.join(c_data, "runs", "x31c", "tasks", "C1")
    os.makedirs(c_brief_dir, exist_ok=True)
    with open(os.path.join(c_brief_dir, "brief-r1.md"), "w") as f:
        f.write("hand-written brief, ingest-once\n")
    c_mk("task", "set", "C1", "status=building", "--run", "x31c")  # commit 1: ingests once
    con = data_con(c_data)
    try:
        n = con.execute("SELECT COUNT(*) c FROM artifacts WHERE run='x31c' AND task='C1'"
                        " AND kind='brief'").fetchone()["c"]
    finally:
        con.close()
    if n != 1:
        if A_BUG:
            RESULTS["c"] = f"FAIL: after the first commit expected ONE brief row, got {n}"
            return RESULTS
        raise AssertionError(f"x31 R2 (c): after the first commit expected ONE hand-written"
                             f" brief artifact row, got {n}")
    c_mk("task", "set", "C1", "status=review", "--run", "x31c")    # commit 2: still ONE
    con = data_con(c_data)
    try:
        n2 = con.execute("SELECT COUNT(*) c FROM artifacts WHERE run='x31c' AND task='C1'"
                         " AND kind='brief'").fetchone()["c"]
    finally:
        con.close()
    if n2 != 1:
        if A_BUG:
            RESULTS["c"] = f"FAIL: after the second commit expected ONE brief row, got {n2}"
            return RESULTS
        raise AssertionError(f"x31 R2 (c): after the second commit the hand-written brief was"
                             f" re-imported; expected ONE row, got {n2}")
    con = data_con(c_data)
    try:
        got = con.execute("SELECT text FROM artifacts WHERE run='x31c' AND task='C1'"
                          " AND kind='brief' LIMIT 1").fetchall()
    finally:
        con.close()
    assert got[0]["text"] == "hand-written brief, ingest-once\n", got
    RESULTS["c"] = "ok"
    print("x31-r2 scratch: %s" % RESULTS)
    # a KNOWN-BUG run still signals the failure it proved (x31_a_fails was set only when a
    # case was recorded FAIL above); a clean run reaches the normal return.
    return RESULTS


def migrated_store_self_test(tmp, env):
    """L3B: after `mk-migrate --switch`, mk-state opens the project store in the C2 folder.

    Every store open/write goes through mkpaths.project_folder(self.data): the migrated
    <home>/projects/<slug>/mk.db, not the legacy <data>/mk.db the repo data dir still names.
    Pre-migration, a missing/corrupt/old-schema root, and a home that resolves only from a temp
    $HOME all fall back to <data>/mk.db and never raise.
    """

    H = os.environ["MK_HOME"]  # Z3: the one real home (root projection)
    import hashlib as _h
    import sqlite3 as _sq
    here = os.path.dirname(os.path.abspath(__file__))

    def run(args, e, code=0, cwd=None):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args],
                           capture_output=True, text=True, env=e, cwd=cwd)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    def sha(path):
        return _h.sha256(open(path, "rb").read()).hexdigest() if os.path.exists(path) else None

    def migrated_env(home):
        e = dict(env)
        e.pop("MATRIXKODER_DATA", None)
        e.update(MK_HOME=home, MATRIXKODER_HOME=home)
        return e

    db = _mkdb()
    # case (a): a project migrated by L2's real tool. Writes must land in the NEW store and
    # leave the OLD <d>/mk.db byte-identical.
    base = os.path.join(tmp, "l3b")
    home = os.path.join(base, "home")
    repo = os.path.join(base, "repo")
    data = os.path.join(repo, ".matrixkoder")
    os.makedirs(repo, exist_ok=True)
    subprocess.run(["git", "init", "-q", repo], check=False)
    m = migrated_env(home)
    d = ["--data", data]
    # a real project store through mk-state itself (store enable), so the run-doc artifact and
    # every column the reader needs are present before the switch
    run(["run", "init", "--id", "r1", "--feature", "F", "--branch", "b1", *d], m, cwd=repo)
    run(["task", "add", "--root", "--id", "P1.1", "--title", "one", "--run", "r1", *d], m, cwd=repo)
    run(["store", "enable", *d], m, cwd=repo)
    old_db = os.path.join(data, "mk.db")
    new_db = mkpaths.target_project_db("fixture", home)
    assert os.path.exists(old_db), "mk-state store enable wrote no <data>/mk.db"
    rc = subprocess.run([sys.executable, os.path.join(here, "mk-migrate.py"),
                         "--switch", "--slug", "fixture", "--data", data, "--home", home],
                        capture_output=True, text=True, env=m)
    assert rc.returncode == 0, (rc.stdout, rc.stderr)
    assert os.path.exists(new_db), "switch wrote no new store"
    old_sha = sha(old_db)

    assert run(["store", "status", *d], m)["store"] == "on"  # the probe follows the C2 folder
    run(["task", "set", "P1.1", "status=building", "--run", "r1", *d], m)
    run(["event", "--task", "P1.1", "--type", "note", "--text", "after switch", "--run", "r1", *d], m)
    run(["run", "set", "--id", "r1", "--next", "dispatch the next task", *d], m)
    assert run(["sync", "--project", "fixture", *d], m)["ok"]
    # the rows are in the NEW store
    con = db.open_project(home, "fixture", data_dir=mkpaths.target_project_dir("fixture", home))
    try:
        status = con.execute("SELECT status FROM tasks WHERE run='r1' AND id='P1.1'").fetchone()["status"]
        notes = con.execute("SELECT COUNT(*) c FROM events WHERE run='r1' AND text='after switch'").fetchone()["c"]
        nxt = con.execute("SELECT next FROM runs WHERE id='r1'").fetchone()["next"]
    finally:
        con.close()
    assert status == "building" and notes == 1 and nxt == "dispatch the next task", (status, notes, nxt)
    # the OLD store never moved: no row landed there, and its bytes are unchanged
    assert sha(old_db) == old_sha, "the legacy <data>/mk.db changed after the switch"
    old = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=data)
    try:
        assert old.execute("SELECT status FROM tasks WHERE run='r1' AND id='P1.1'").fetchone()["status"] == "planned"
        assert old.execute("SELECT COUNT(*) c FROM events WHERE run='r1' AND text='after switch'").fetchone()["c"] == 0
    finally:
        old.close()

    # case (b): a pre-migration project. Writes stay in <d>/mk.db and no projects/ folder appears.
    bhome = os.path.join(base, "bhome")
    brepo = os.path.join(base, "brepo")
    bdata = os.path.join(brepo, ".matrixkoder")
    os.makedirs(brepo, exist_ok=True)
    subprocess.run(["git", "init", "-q", brepo], check=False)
    bm = migrated_env(bhome)
    run(["run", "init", "--id", "b1", "--feature", "B", "--branch", "bb", "--data", bdata], bm, cwd=brepo)
    run(["task", "add", "--root", "--id", "BT", "--title", "t", "--run", "b1", "--data", bdata], bm, cwd=brepo)
    run(["store", "enable", "--data", bdata], bm, cwd=brepo)
    out = run(["task", "set", "BT", "status=building", "--run", "b1", "--data", bdata], bm, cwd=brepo)
    assert out["task"]["status"] == "building", out
    assert os.path.exists(os.path.join(bdata, "mk.db")), "pre-migration write left the legacy store"
    assert not os.path.exists(os.path.join(bhome, "projects")), "pre-migration created a projects/ folder"
    bcon = db.open_project(H, STORE_PROJECT_FALLBACK, data_dir=bdata)
    try:
        assert bcon.execute("SELECT status FROM tasks WHERE run='b1' AND id='BT'").fetchone()["status"] == "building"
    finally:
        bcon.close()

    # case (c1): the root store is missing, and $MK_HOME/$MATRIXKODER_HOME are unset — the home
    # resolves from a temp $HOME only. The write falls back to <d>/mk.db and never raises.
    chome = os.path.join(base, "chome")
    crepo = os.path.join(base, "crepo")
    cdata = os.path.join(crepo, ".matrixkoder")
    os.makedirs(crepo, exist_ok=True)
    subprocess.run(["git", "init", "-q", crepo], check=False)
    ce = {k: v for k, v in env.items() if k not in ("MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA")}
    ce["HOME"] = chome
    os.makedirs(chome, exist_ok=True)
    run(["run", "init", "--id", "c1", "--feature", "C", "--branch", "cc", "--data", cdata], ce, cwd=crepo)
    run(["task", "add", "--root", "--id", "CT", "--title", "t", "--run", "c1", "--data", cdata], ce, cwd=crepo)
    run(["store", "enable", "--data", cdata], ce, cwd=crepo)
    assert run(["task", "set", "CT", "status=building", "--run", "c1", "--data", cdata],
               ce, cwd=crepo)["task"]["status"] == "building"
    assert os.path.exists(os.path.join(cdata, "mk.db")), "a missing root lost the fallback store"

    # case (c2): a corrupt root store must not raise — fall back to <d>/mk.db.
    korrupt = os.path.join(base, "korrupt-home")
    kdata = os.path.join(base, "krepo", ".matrixkoder")
    os.makedirs(os.path.dirname(kdata), exist_ok=True)
    os.makedirs(korrupt, exist_ok=True)
    with open(os.path.join(korrupt, "mk.db"), "wb") as f:
        f.write(b"this is not a sqlite database")
    km = migrated_env(korrupt)
    run(["run", "init", "--id", "k1", "--feature", "K", "--branch", "kk", "--data", kdata], km)
    run(["task", "add", "--root", "--id", "KT", "--title", "t", "--run", "k1", "--data", kdata], km)
    run(["store", "enable", "--data", kdata], km)
    assert run(["task", "set", "KT", "status=building", "--run", "k1", "--data", kdata],
               km)["task"]["status"] == "building"
    assert os.path.exists(os.path.join(kdata, "mk.db")), "a corrupt root lost the fallback store"

    # case (c3): a root store at an OLDER schema (no projects.db_path) — same safe fallback.
    ohome = os.path.join(base, "old-home")
    odata = os.path.join(base, "orepo", ".matrixkoder")
    os.makedirs(os.path.dirname(odata), exist_ok=True)
    os.makedirs(ohome, exist_ok=True)
    old_root = _sq.connect(os.path.join(ohome, "mk.db"))
    try:
        old_root.execute("CREATE TABLE projects(slug TEXT, name TEXT, repo TEXT, data_dir TEXT)")
        old_root.execute("INSERT INTO projects(slug, name, repo, data_dir) VALUES (?,?,?,?)",
                         ("matrixkoder", "M", "/r", odata))
        old_root.commit()
    finally:
        old_root.close()
    om = migrated_env(ohome)
    run(["run", "init", "--id", "o1", "--feature", "O", "--branch", "oo", "--data", odata], om)
    run(["task", "add", "--root", "--id", "OT", "--title", "t", "--run", "o1", "--data", odata], om)
    run(["store", "enable", "--data", odata], om)
    assert run(["task", "set", "OT", "status=building", "--run", "o1", "--data", odata],
               om)["task"]["status"] == "building"
    assert os.path.exists(os.path.join(odata, "mk.db")), "an old-schema root lost the fallback store"
    assert not os.path.exists(os.path.join(ohome, "projects")), "an old-schema root created a projects/ folder"


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        sys.exit(main(sys.argv[1:]))
