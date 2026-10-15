#!/usr/bin/env python3
"""MatrixKoder SQLite stores — the ONLY module that opens a store read-write. Python 3 stdlib only.

  timeline --project P --run R --task T --json
  cards [--project P] --json
  projects --json
  plans --project P|--data D [--json]          # every plan row (C5, C8)
  plans --project P|--data D <id> --json       # one plan with its phases
  overview --json                            # the root's projects + plans: numbers and db_path (C3)
  run-snapshot --project P [--run R] --json    # the runs list, the active run and its tasks (L5)
  events --project P --run R [--task T] [--chan C] [--since N] --json   # the run's events (L5)

Common: --data DIR (the project data dir; until DM a project lives at <repo>/.matrixkoder/),
--home DIR (the MK home, default $MK_HOME or ~/.matrixkoder), --project SLUG.
Every command prints ONE JSON line. Errors: {"ok": false, "error": ...} exit 2;
a store newer than this code: exit 3.

Two stores. The root store <home>/mk.db holds the cross-project cards (C1). The project store
<data>/mk.db (or <home>/projects/<slug>/mk.db when only --project is given) holds that project's
plans, runs, tasks, events and change_log (C2). The project store is the truth: transition() is
the only status writer, and the root card is a copy written afterwards, only when sync_root=True
(default False — D3B turns it on).

Schema: docs/state-schema.md (store section).
"""
import argparse, contextlib, datetime, hashlib, importlib.util, json, multiprocessing, os, re, sqlite3, subprocess, sys, tempfile

sys.dont_write_bytecode = True

SCHEMA = 5
ROOT_SCHEMA = 2  # the root store migrates additively; project stores go to SCHEMA
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

# C1 (L3): mkpaths.py is the ONE path authority (home rule, data dir, project folder, store
# path). mkdb keeps its own check_home/check_slug refusals and _selftest_write_guard, and
# delegates the path RULES to mkpaths — loaded as a sibling by path, the way mk-bus.py does,
# because mkpaths imports nothing from the repo (no cycle).
_mkpaths_mod = None


def _mkpaths():
    global _mkpaths_mod
    if _mkpaths_mod is None:
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location("mkpaths", os.path.join(here, "mkpaths.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _mkpaths_mod = mod
    return _mkpaths_mod
TYPES = ("phase", "task", "subtask")
# C4: the column a status lands in. Attention statuses keep their column and raise the flag.
COLUMNS = {"planned": "Backlog", "routed": "Backlog", "ready": "Ready",
           "dispatched": "Building", "building": "Building", "takeover": "Building",
           "verifying": "Verify", "review": "Review", "fix": "Fix",
           "merged": "Done", "qa": "Done", "done": "Done"}
ATTENTION = ("blocked", "stalled", "quota", "provider")
ROUTE_KEYS = ("agent", "model", "effort")
# shortcut: the synced-folder check is a path-string heuristic, not a mount probe. It catches the
# macOS iCloud container ("Mobile Documents"), the CloudStorage symlink tree, Dropbox, and anything
# under /Volumes (network shares and most external disks). It misses a custom sync root with none
# of those words, a bind mount, and a volume mounted somewhere other than /Volumes.
UNSAFE_HOME = ("mobile documents", "cloudstorage", "icloud", "dropbox")

# v2 (C3): projects gains db_path (set = migrated; L2's mk-migrate --switch writes it) and the
# plan_summary table. Additive only: db_path is added last so a fresh root and a migrated root
# reach the same column order, and no existing column or row is rewritten.
ROOT_DDL = """
CREATE TABLE IF NOT EXISTS projects(
  slug TEXT PRIMARY KEY, name TEXT, repo TEXT, data_dir TEXT, mode TEXT, created TEXT, last_activity TEXT,
  db_path TEXT);
CREATE TABLE IF NOT EXISTS cards(
  project TEXT, run TEXT, task TEXT, parent TEXT, type TEXT, title TEXT, status TEXT, col TEXT,
  attention INTEGER, question TEXT, route_json TEXT, lane TEXT, round INTEGER, verdict TEXT,
  gates TEXT, updated TEXT, change_no INTEGER, PRIMARY KEY(project, run, task));
CREATE TABLE IF NOT EXISTS project_summary(
  project TEXT PRIMARY KEY, active_runs INTEGER, by_status_json TEXT, attention INTEGER,
  last_event TEXT, change_no INTEGER);
CREATE TABLE IF NOT EXISTS sync(project TEXT PRIMARY KEY, last_change_no INTEGER);
-- C3: one row per plan of a project — the numbers and the linkage, never the rows themselves.
-- Column list is frozen: L2 codes against it. No task row, no event.
CREATE TABLE IF NOT EXISTS plan_summary(
  project TEXT, plan TEXT, title TEXT, status TEXT, counts_json TEXT, live_run TEXT, updated TEXT,
  PRIMARY KEY(project, plan));
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
"""
PROJECT_DDL = """
CREATE TABLE IF NOT EXISTS plans(
  id TEXT PRIMARY KEY, version INTEGER, title TEXT, header_json TEXT, body_md TEXT,
  approved_at TEXT, archived_path TEXT, status TEXT, created TEXT, design TEXT,
  integration TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, plan TEXT, plan_version INTEGER, branch TEXT, status TEXT, stage TEXT,
  next TEXT, started TEXT, ended TEXT, extra_json TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  run TEXT, id TEXT, parent TEXT, type TEXT, title TEXT, acceptance TEXT, owned TEXT, deps TEXT,
  parallel INTEGER, route_json TEXT, status TEXT, prev_status TEXT, file_status TEXT, lane TEXT, slot TEXT, branch TEXT, pane TEXT,
  round INTEGER, verdict TEXT, updated TEXT, extra_json TEXT, PRIMARY KEY(run, id));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, run TEXT, task TEXT, t TEXT, chan TEXT, kind TEXT, from_id TEXT,
  to_id TEXT, reply_to TEXT, text TEXT, artifact_id INTEGER, meta_json TEXT);
CREATE INDEX IF NOT EXISTS ev_task ON events(run, task, id);
CREATE TABLE IF NOT EXISTS artifacts(
  id INTEGER PRIMARY KEY, run TEXT, task TEXT, round INTEGER, kind TEXT, rev INTEGER, text TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS acks(run TEXT, msg TEXT, lane TEXT, level INTEGER, t TEXT, PRIMARY KEY(run, msg, lane));
CREATE TABLE IF NOT EXISTS lane_slots(
  slot TEXT PRIMARY KEY, lane_path TEXT, state TEXT, run TEXT, task TEXT,
  workspace TEXT, workspace_id TEXT, branch TEXT, lock_hash TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS waits(id INTEGER PRIMARY KEY, run TEXT, question TEXT, opened TEXT, cleared TEXT);
CREATE TABLE IF NOT EXISTS change_log(change_no INTEGER PRIMARY KEY, run TEXT, task TEXT, t TEXT);
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
"""
# v2: artifacts.src is the importer's stable source key. NULL on rows written by the live path.
# event_src mirrors meta_json.src so a repeat import is one indexed lookup, not a table scan.
# A step may be SQL or a callable: ALTER TABLE ADD COLUMN is not idempotent as SQL, so V2 is a
# function that adds the column only when PRAGMA table_info says it is missing.


class Fail(Exception):
    def __init__(self, msg, code=2):
        super().__init__(msg)
        self.code = code


class _Unset:
    """Sentinel: transition() leaves tasks.verdict unchanged unless the caller passes one."""


_UNSET = _Unset()


class Parser(argparse.ArgumentParser):
    def __init__(self, *a, **kw):
        kw.setdefault("allow_abbrev", False)
        super().__init__(*a, **kw)

    def error(self, message):
        raise Fail(message)


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def check_slug(slug):
    """C11: validate BEFORE any path is built from the slug."""
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise Fail(f"invalid slug {slug!r}: must match {SLUG_RE.pattern}")


def check_home(path):
    """Refuse a home that looks synced or networked. See UNSAFE_HOME for what this misses."""
    low = path.lower()
    if low.startswith("/volumes/") or any(mark in low for mark in UNSAFE_HOME):
        raise Fail(f"refusing a network or synced home: {path}")


def _real_home():
    """The real user MK home, independent of $MK_HOME/$MATRIXKODER_HOME and of $HOME.

    A self-test may override $HOME (mk-state does), so the password database wins;
    $HOME is only the fallback. Used by the MK_SELFTEST write guard (C16)."""
    try:
        import pwd
        base = pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        base = os.path.expanduser("~")
    return os.path.realpath(os.path.abspath(os.path.join(base, ".matrixkoder")))


def _selftest_write_guard(path):
    """Refuse a write open outside the temp tree while self-testing (C16).

    Under MK_SELFTEST=1 a writer may only touch the temp tree: the system temp
    dir ($TMPDIR/TEMP/TMP, tempfile.gettempdir()) or the pinned temp MK home
    ($MK_HOME/$MATRIXKODER_HOME). Anything under the real ~/.matrixkoder, or any
    other real path such as a repo's .matrixkoder, raises Fail naming the path.
    Read-only opens (_connect_ro) never call this.
    """
    if os.environ.get("MK_SELFTEST") != "1":
        return
    rp = os.path.realpath(os.path.abspath(path))
    real = _real_home()
    if rp == os.path.join(real, "mk.db") or rp.startswith(real + os.sep):
        raise Fail(f"refusing to write the real MK home {rp} under MK_SELFTEST=1 (C16)")
    roots = set()
    for key in ("TMPDIR", "TEMP", "TMP"):
        val = os.environ.get(key)
        if val:
            with contextlib.suppress(Exception):
                roots.add(os.path.realpath(os.path.abspath(val)))
    with contextlib.suppress(Exception):
        roots.add(os.path.realpath(os.path.abspath(tempfile.gettempdir())))
    for key in ("MK_HOME", "MATRIXKODER_HOME"):
        val = os.environ.get(key)
        if val:
            with contextlib.suppress(Exception):
                roots.add(os.path.realpath(os.path.abspath(os.path.expanduser(val))))
    if any(rp == root or rp.startswith(root + os.sep) for root in roots if root):
        return
    raise Fail(f"refusing to write {rp} under MK_SELFTEST=1: not under the temp dir (C16)")


def resolve_home(arg):
    """One MK home rule (C16): $MK_HOME and $MATRIXKODER_HOME are the same setting.

    Either name is accepted; when both are set they must resolve to the same
    directory, else Fail. An explicit --home arg wins over both. C1 (L3): the rule
    itself is mkpaths.home(); mkdb keeps its own check_home refusal on the result.
    """
    home = _mkpaths().home(arg)
    check_home(home)
    return home


def project_dir(home, slug=None, data_dir=None):
    """--data wins; else <home>/projects/<slug> — the C2 folder for a migrated project (C1, L3).

    The slug is checked before the path exists. Both routes pass check_home before
    anything is created: a synced or networked home, and a --data dir that itself
    looks synced, are refused (C11). The path RULE (migrated → C2 target, else
    today's answer) is mkpaths.project_dir's; mkdb keeps the refusals.
    """
    if data_dir:
        path = _mkpaths().project_dir(home_arg=home, data_dir=data_dir)
        check_home(path)
        return path
    if not slug:
        raise Fail("pass --project or --data")
    check_slug(slug)
    check_home(home)
    return _mkpaths().project_dir(home_arg=home, slug=slug)


def _connect(path):
    _selftest_write_guard(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _connect_ro(path):
    """Open an existing store read-only. Never creates the database file.

    mode=ro is the documented exception to the single-writer rule: a read must not migrate,
    stamp schema_version, or otherwise write. query_only is belt and braces on top of the URI.

    immutable=1 is used ONLY when no -wal exists: it tells SQLite the file never changes, so it
    ignores the WAL, which would hide commits a live writer has not checkpointed yet. With a WAL
    present the open is plain mode=ro, which reads the WAL. A mode=ro open of a WAL database needs
    the -shm file and creates it when it can; if the directory is not writable and -shm is missing,
    the failure is a named error, never a traceback.
    """
    wal = path + "-wal"
    if os.path.exists(wal) and os.path.getsize(wal) > 0:
        uri = "file:" + path + "?mode=ro"
    else:
        uri = "file:" + path + "?mode=ro&immutable=1"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.OperationalError as e:
        raise Fail(f"cannot open {path} read-only: {e}")
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA foreign_keys=ON")
    except sqlite3.OperationalError as e:
        con.close()
        raise Fail(f"cannot open {path} read-only: {e}")
    return con


def _schema_version(con):
    """The stamped version, or 0 when the store has no schema_version table (a pre-v1 file)."""
    row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
    if not row:
        return 0
    got = con.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return got["v"] or 0


def _open_read(path, version):
    """Open path for a read, or None when the file does not exist.

    A missing file returns None without creating anything. A store newer than this code is
    refused (exit 3), same as a writer. An older store is refused (exit 2) instead of migrated:
    a read must not write, and the caller is told to run a writer command.
    """
    if not os.path.isfile(path):
        return None
    con = _connect_ro(path)
    try:
        have = _schema_version(con)
    except Exception:
        con.close()
        raise
    if have > version:
        con.close()
        raise Fail(f"store schema v{have} is newer than this code (v{version}); upgrade mkdb.py", 3)
    if have < version:
        con.close()
        raise Fail(f"store schema v{have} needs migration to v{version}; run a writer command")
    return con


def _begin(con):
    con.execute("BEGIN IMMEDIATE")


def _change_log(con, run, task):
    return con.execute("INSERT INTO change_log(run, task, t) VALUES (?,?,?)", (run, task, now()))


def _migrate(con, version, migrations):
    """Apply the ordered migration list. A store newer than this code is refused (exit 3)."""
    con.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY)")
    row = con.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    have = row["v"] or 0
    if have > version:
        raise Fail(f"store schema v{have} is newer than this code (v{version}); upgrade mkdb.py", 3)
    for n, step in migrations:
        if n <= have or n > version:
            continue
        # executescript() commits on its own, so the version stamp is a second transaction:
        # a crash between them re-runs the migration, which is why every step is idempotent.
        # A step is either SQL or a callable(con); an ALTER TABLE ADD COLUMN must be a callable
        # that checks the column first, because the SQL is not safe to run twice.
        if callable(step):
            step(con)
            con.commit()
        else:
            con.executescript(step)
        _begin(con)
        try:
            con.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (n,))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return con


def open_root(home, version=ROOT_SCHEMA, migrations=()):
    """Open or create the root store <home>/mk.db (C1). Migrates additively: an existing v1
    root keeps every row and gains projects.db_path plus plan_summary (C3)."""
    check_home(home)
    _selftest_write_guard(os.path.join(home, "mk.db"))
    return _migrate(_connect(os.path.join(home, "mk.db")), version,
                    ((1, ROOT_DDL), (2, _root_v2)) + tuple(migrations))


def _root_v2(con):
    """The shipped root v2 step (C3): projects.db_path + the plan_summary table.

    Additive only — the column is added last (matching ROOT_DDL's order) and the table is
    created empty, so a v1 root keeps every row it had and reaches the same shape a fresh
    root gets from ROOT_DDL. Safe to run twice: the column is added only when table_info
    lacks it, and CREATE TABLE IF NOT EXISTS never touches an existing table.
    """
    cols = [r["name"] for r in con.execute("PRAGMA table_info(projects)")]
    if cols and "db_path" not in cols:
        con.execute("ALTER TABLE projects ADD COLUMN db_path TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS plan_summary(
      project TEXT, plan TEXT, title TEXT, status TEXT, counts_json TEXT, live_run TEXT, updated TEXT,
      PRIMARY KEY(project, plan))""")



def _ensure_prev_status(con):
    """tasks.prev_status holds the last non-attention status (C4). Added in place; schema stays v1."""
    cols = [r["name"] for r in con.execute("PRAGMA table_info(tasks)")]
    if cols and "prev_status" not in cols:
        con.execute("ALTER TABLE tasks ADD COLUMN prev_status TEXT")


def _ensure_file_status(con):
    """tasks.file_status is the published status (planned or routed) while status is the kanban
    status `ready`. Added in place, only when missing, so the step is safe to run twice."""
    cols = [r["name"] for r in con.execute("PRAGMA table_info(tasks)")]
    if cols and "file_status" not in cols:
        con.execute("ALTER TABLE tasks ADD COLUMN file_status TEXT")


def _v2(con):
    """The shipped v2 step. Safe to run twice: the column, the table and the index are guarded."""
    cols = [r["name"] for r in con.execute("PRAGMA table_info(artifacts)")]
    if cols and "src" not in cols:
        con.execute("ALTER TABLE artifacts ADD COLUMN src TEXT")
    con.execute("CREATE TABLE IF NOT EXISTS event_src(run TEXT, src TEXT, PRIMARY KEY(run, src))")
    con.execute("CREATE INDEX IF NOT EXISTS art_run_src ON artifacts(run, src)")


# Columns a lanes.json slot carries beyond the v1 lane_slots row (mk-lane.py slot record).
LANE_SLOT_COLS = ("workspace", "workspace_id", "branch", "lock_hash", "updated")


def _v3(con):
    """The shipped v3 step. Safe to run twice: each column is added only when table_info lacks it."""
    cols = [r["name"] for r in con.execute("PRAGMA table_info(lane_slots)")]
    if not cols:
        return
    for name in LANE_SLOT_COLS:
        if name not in cols:
            con.execute(f"ALTER TABLE lane_slots ADD COLUMN {name} TEXT")


# plans columns added by D1 (C1): status, created, design (a JSON array as text), integration.
PLAN_D1_COLS = ("status", "created", "design", "integration")


def _v4(con):
    """The shipped v4 step (D1.1, C1): plans gains status, created, design, integration.

    Safe to run twice: each column is added only when table_info lacks it. Additive only —
    no existing column or row changes; a fresh store reaches the same shape through PROJECT_DDL.
    """
    cols = [r["name"] for r in con.execute("PRAGMA table_info(plans)")]
    if not cols:
        return
    for name in PLAN_D1_COLS:
        if name not in cols:
            con.execute(f"ALTER TABLE plans ADD COLUMN {name} TEXT")


def _v5(con):
    """The shipped v5 step (L4, C4): runs and tasks gain extra_json, the TEXT column holding the
    fields the tables have no column for (run: feature/waiting/recent/plan_sha/plan_status/... ;
    task: wave/ready/workspace/gates/rounds/... ). Additive only, and safe to run twice: each
    column is added only when table_info lacks it, so a fresh store reaches the same shape
    through PROJECT_DDL and an existing v4 store keeps every row and gains the columns NULL.
    """
    for table in ("runs", "tasks"):
        cols = [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]
        if cols and "extra_json" not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN extra_json TEXT")


def open_project(home, slug=None, data_dir=None, version=SCHEMA, migrations=()):
    """Open or create the project store (C2). check_home runs before any directory is created."""
    path = os.path.join(project_dir(home, slug, data_dir), "mk.db")
    _selftest_write_guard(path)
    con = _migrate(_connect(path), version, ((1, PROJECT_DDL), (2, _v2), (3, _v3), (4, _v4), (5, _v5)) + tuple(migrations))
    _ensure_prev_status(con)
    _ensure_file_status(con)
    return con


def _deps_of(text):
    return [d.strip() for d in (text or "").split(",") if d.strip()]


def _dep_target(run, dep):
    """A dep is an id in the same plan, or <run>:<id> in the same project."""
    if ":" in dep:
        other, _, task = dep.partition(":")
        if not other or not task or ":" in task:
            raise Fail(f"bad dep {dep!r}: expected <run>:<id>")
        return other, task
    return run, dep


def _reaches(start, edges):
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        for nxt in edges.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _cycle_through(me, edges):
    """One simple directed cycle through me, or None. BFS with a parent map; always terminates."""
    parent = {me: None}
    queue = [me]
    found = None
    while queue:
        cur = queue.pop(0)
        for nxt in edges.get(cur, ()):
            if nxt == me and cur != me:
                found = cur
                break
            if nxt not in parent:
                parent[nxt] = cur
                queue.append(nxt)
        if found is not None:
            break
    if found is None:
        return None
    path = [me]
    cur = found
    while cur is not None:
        path.append(cur)
        cur = parent[cur]
    path.reverse()
    return path


def check_deps(con, run, task, deps):
    """Exist, no self-dep, no cycle. A cycle error names one simple path back to the task."""
    me = (run, task)
    for dep in _deps_of(deps):
        target = _dep_target(run, dep)
        if target == me:
            raise Fail(f"self-dep: {task} depends on itself")
        if not con.execute("SELECT 1 FROM tasks WHERE run=? AND id=?", target).fetchone():
            raise Fail(f"missing dep: {dep} is not a task in this project")
    edges = {}
    for row in con.execute("SELECT run, id, deps FROM tasks"):
        key = (row["run"], row["id"])
        edges[key] = [_dep_target(row["run"], d) for d in _deps_of(row["deps"])]
    edges[me] = [_dep_target(run, d) for d in _deps_of(deps)]
    path = _cycle_through(me, edges)
    if path:
        raise Fail("cycle: " + " -> ".join(f"{r}:{t}" for r, t in path))


def check_route(route_json):
    """route_json is {agent, model, effort}; empty (None or '') is the orchestrator's choice."""
    if route_json in (None, ""):
        return None
    try:
        route = json.loads(route_json)
    except (TypeError, ValueError):
        route = None
    if not isinstance(route, dict) or set(route) != set(ROUTE_KEYS):
        raise Fail('bad route_json: expected {"agent","model","effort"}, got %r' % (route_json,))
    return route_json


def column_of(status, prev_status=None):
    """C4: the kanban column, plus the attention flag for blocked/stalled/quota/provider.

    An attention status keeps the column of prev_status (the task's previous non-attention
    status). With no previous status the column is Backlog. column_of stays pure: the caller
    looks prev_status up.
    """
    if status in ATTENTION:
        if prev_status in COLUMNS:
            return COLUMNS[prev_status], 1
        return "Backlog", 1
    if status not in COLUMNS:
        raise Fail(f"unknown status {status!r}")
    return COLUMNS[status], 0


def _prev_status(con, run, task):
    """The task's previous non-attention status, stored on the row (NULL until the first one)."""
    row = con.execute("SELECT prev_status FROM tasks WHERE run=? AND id=?", (run, task)).fetchone()
    return row["prev_status"] if row else None


def _placed(row, status=None):
    """Column and attention for a task row. The one path transition() and catch_up() both use."""
    status = row["status"] if status is None else status
    prev = row["prev_status"] if status in ATTENTION else None
    return column_of(status, prev)


def _rollup(con, run, task):
    """The status a parent should hold, or None when it has no children.

    Any child in an attention status wins, first match in ATTENTION order (blocked, stalled,
    quota, provider): the parent takes that status, and transition() then keeps the parent's
    previous column and sets attention=1. Otherwise the parent takes the least-done column of
    its children's non-attention statuses.
    """
    kids = con.execute("SELECT status FROM tasks WHERE run=? AND parent=?", (run, task)).fetchall()
    if not kids:
        return None
    statuses = [k["status"] for k in kids]
    for flag in ATTENTION:
        if flag in statuses:
            return flag
    order = list(dict.fromkeys(COLUMNS.values()))
    cols = {column_of(s)[0]: s for s in statuses}
    return cols[min(cols, key=order.index)]


def add_task(con, run, task, type, title="", parent=None, deps="", route_json=None, status="planned"):
    """Insert one task. The dep check and the insert share one write transaction (C3)."""
    if type not in TYPES:
        raise Fail(f"bad type {type!r}: expected phase|task|subtask")
    check_route(route_json)
    _begin(con)
    try:
        if parent and not con.execute("SELECT 1 FROM tasks WHERE run=? AND id=?", (run, parent)).fetchone():
            raise Fail(f"missing parent: {parent}")
        check_deps(con, run, task, deps)
        con.execute(
            "INSERT INTO tasks(run, id, parent, type, title, deps, route_json, status, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (run, task, parent, type, title, deps, route_json or None, status, now()))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


# only these move to ready; a started task is never touched. `routed` stays the published
# status (file_status) so a later import does not turn it into `planned`.
READY_FROM = ("planned", "routed")
DEP_MET = ("merged", "qa", "done")


def _scope_hit(row_run, deps, run):
    """True when the task is in `run`, or one dep token names `<run>:<id>` exactly.

    A substring LIKE would let run `r` match the dep `other:ext` (the letters `r:`).
    """
    if row_run == run:
        return True
    for d in _deps_of(deps):
        try:
            if _dep_target(row_run, d)[0] == run:
                return True
        except Fail:
            continue
    return False


def _dep_met(con, run, dep):
    """True when the dep's task is merged/qa/done. A missing or cross-project dep is unmet."""
    other, tid = _dep_target(run, dep)
    row = con.execute("SELECT status FROM tasks WHERE run=? AND id=?", (other, tid)).fetchone()
    return bool(row) and row["status"] in DEP_MET


def promote_ready(home, project, run, changed=None, data_dir=None, sync_root=False):
    """Move dependents of `run`'s tasks to `ready` when every dep is met, and back to `planned`
    when a dep leaves the met set (a done -> fix regress), through transition().

    Scope: the triggering run, plus any task whose dep token names `<run>:<id>` exactly —
    never an unrelated old run, and never a substring (`other:ext` does not match run `r`).
    One connection for the scan, one snapshot of the dep statuses, so a long waiting chain
    costs one query per pass, not one per candidate. A task with no deps stays planned; a
    task that has started is never touched (its status is not in READY_FROM). Each move is
    ONE transaction: no event (the file view's "deps met" note is the one event), `updated`
    unchanged. `file_status` keeps `planned` or `routed` so the published view can restore it.
    Idempotent: a task already `ready` is not in READY_FROM, and a demoted task has an unmet
    dep, so a second pass moves nothing. When sync_root is set, every card that does not match
    its row is repaired in ONE root transaction at the end of the pass (a missing card is
    recorded, so a task with no change_log is visited once). -> ids moved."""
    con = open_project(home, project, data_dir)
    try:
        cand = con.execute(
            "SELECT run, id, deps, status, file_status FROM tasks"
            " WHERE status IN (?,?,?)",
            (*READY_FROM, "ready")).fetchall()
        rows = [r for r in cand if _scope_hit(r["run"], r["deps"], run)]
        if not rows:
            return []
        runs = {run}
        for r in rows:
            for d in _deps_of(r["deps"]):
                with contextlib.suppress(Fail):
                    runs.add(_dep_target(r["run"], d)[0])
        marks = {(x["run"], x["id"]): x["status"] for x in
                 con.execute("SELECT run, id, status FROM tasks WHERE run IN (%s)"
                             % ",".join("?" * len(runs)), tuple(sorted(runs)))}
        # change_no and the full row, read once, so the card repair does not reopen per task
        full = {}
        nos = {}
        if sync_root:
            for r in rows:
                key = (r["run"], r["id"])
                full[key] = con.execute("SELECT * FROM tasks WHERE run=? AND id=?", key).fetchone()
                nos[key] = con.execute(
                    "SELECT MAX(change_no) c FROM change_log WHERE run=? AND task=?", key).fetchone()["c"]
    finally:
        con.close()
    card_status = {}
    if sync_root:
        root = open_root(home)
        try:
            card_status = {(c["run"], c["task"]): c["status"] for c in
                           root.execute("SELECT run, task, status FROM cards WHERE project=?",
                                        (project,))}
        finally:
            root.close()
    moved = []
    repairs = []
    for r in rows:
        deps = _deps_of(r["deps"])
        if not deps:
            continue
        try:
            targets = [_dep_target(r["run"], d) for d in deps]
        except Fail:
            continue  # a malformed stored dep: skip this task, never fail the committed change
        met = all(marks.get(t) in DEP_MET for t in targets)
        published = r["file_status"] if r["file_status"] in READY_FROM else "planned"
        if r["status"] in READY_FROM and met:
            to = "ready"
        elif r["status"] == "ready" and not met:
            to = published
        else:
            to = None
        key = (r["run"], r["id"])
        status_now = r["status"]
        if to is not None:
            con = open_project(home, project, data_dir)
            try:
                cur = con.execute("SELECT status, file_status FROM tasks WHERE run=? AND id=?",
                                  key).fetchone()
            finally:
                con.close()
            if not cur or cur["status"] != r["status"]:
                continue  # moved between the scan and now: leave it to the next pass
            if to == "ready":
                file_status = cur["file_status"] if cur["file_status"] in READY_FROM else cur["status"]
            else:
                file_status = None  # demoted: the status itself is the published one again
            transition(home, project, r["run"], r["id"], to,
                       note="" if to == "ready" else "dep no longer met",
                       data_dir=data_dir, sync_root=False, event=False, keep_updated=True,
                       file_status=file_status)
            status_now = to
            if sync_root:
                con = open_project(home, project, data_dir)
                try:
                    full[key] = con.execute("SELECT * FROM tasks WHERE run=? AND id=?", key).fetchone()
                    nos[key] = con.execute(
                        "SELECT MAX(change_no) c FROM change_log WHERE run=? AND task=?",
                        key).fetchone()["c"]
                finally:
                    con.close()
            if to == "ready":
                moved.append(r["id"])
        if sync_root and card_status.get(key) != status_now:
            row = full.get(key)
            no = nos.get(key)
            if row and no:
                repairs.append((r, dict(row) if not isinstance(row, dict) else row, status_now, no))
            else:
                # no change_log, or no row: remember it so the next pass does not reopen it
                card_status[key] = status_now
    if repairs:
        _repair_cards(home, project, repairs, data_dir)
    return moved


ACTIVE_RUN_STATUSES = ("running", "active")  # mk-state writes 'active'; 'running' kept for older rows


def _summary_values(con):
    """C1 project_summary aggregates from an OPEN project connection: three aggregate
    statements, no per-task scan and no second connection.

    active_runs counts the runs the store holds as running/active; by_status_json is the
    sorted-keys JSON object of task-status counts over ALL tasks of the project (every run);
    attention reuses the card rule (column_of): a status in ATTENTION; last_event is the
    newest events.t of the project (NULL with no events). change_no is the caller's — the
    change just committed.
    """
    by, attention = {}, 0
    for r in con.execute("SELECT status, COUNT(*) c FROM tasks GROUP BY status"):
        by[r["status"]] = r["c"]
        if r["status"] in ATTENTION:
            attention += r["c"]
    active = con.execute(
        "SELECT COUNT(*) c FROM runs WHERE status IN (?,?)", ACTIVE_RUN_STATUSES).fetchone()["c"]
    last_event = con.execute("SELECT MAX(t) m FROM events").fetchone()["m"]
    return active, json.dumps(by, sort_keys=True), attention, last_event


def _write_summary(root, project, values, change_no):
    """The C1 summary upsert, INSIDE the caller's open root transaction (C5: it lands with
    the card). change_no never lowers: a replay carrying an older change keeps the stored one.
    """
    active, by_json, attention, last_event = values
    root.execute(
        "INSERT INTO project_summary(project, active_runs, by_status_json, attention, last_event, change_no)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(project) DO UPDATE SET active_runs=excluded.active_runs,"
        " by_status_json=excluded.by_status_json, attention=excluded.attention,"
        " last_event=excluded.last_event, change_no=MAX(project_summary.change_no, excluded.change_no)",
        (project, active, by_json, attention, last_event, change_no))

def _plan_summary_values(con):
    """C3: one plan_summary value tuple per plans row, from an OPEN project connection.

    The numbers are the ones plans() already reports for the same store — _plan_obj owns the
    counting rule (the newest run's tasks, phase-level rows excluded), so the root and the
    project reply can never disagree about a plan's counts. One pass over plans, no write.
    """
    out = []
    for row in con.execute("SELECT * FROM plans ORDER BY id"):
        obj = _plan_obj(con, row)
        out.append((row["id"], obj["title"], obj["status"],
                    json.dumps(obj["counts"], sort_keys=True), obj["live_run"]))
    return out


def _write_plan_summaries(root, project, values):
    """The C3 plan_summary upserts, INSIDE the caller's open root transaction.

    Same contract as _write_summary: the rows land with the card that triggered them (or in the
    caller's own transaction when sync_root is off), so a kill before the commit leaves the
    previous row and catch_up rebuilds it. counts_json is rewritten whole; no column is merged,
    because the row is a projection of the project store, never an independent fact.
    """
    if not values:
        return
    stamp = now()
    root.executemany(
        "INSERT INTO plan_summary(project, plan, title, status, counts_json, live_run, updated)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(project, plan) DO UPDATE SET title=excluded.title, status=excluded.status,"
        " counts_json=excluded.counts_json, live_run=excluded.live_run, updated=excluded.updated",
        [(project, plan, title, status, counts, live, stamp)
         for plan, title, status, counts, live in values])


def _write_plan_summaries_root(home, project, values):
    """The plan_summary batch in its own root transaction (the sync_root=False path).

    A no-op when the project store holds no plans row: a transition on a store with no plan
    has no plan_summary row to keep current, so it opens no root store, exactly as before.
    """
    if not values:
        return
    root = open_root(home)
    try:
        _begin(root)
        try:
            _write_plan_summaries(root, project, values)
            root.execute("COMMIT")
        except Exception:
            root.execute("ROLLBACK")
            raise
    finally:
        root.close()


def refresh_plan_summaries(home, project, data_dir=None):
    """Rebuild every plan_summary row of one project from its store (C3). The public entry for
    a writer that changed a plans row outside transition() — it recomputes and writes in one
    root transaction, and is what catch_up uses to heal a missed batch.

    Returns the number of plan rows written. A store with no plans row writes nothing.
    """
    con = open_project(home, project, data_dir)
    try:
        values = _plan_summary_values(con)
    finally:
        con.close()
    _write_plan_summaries_root(home, project, values)
    return len(values)


def _repair_cards(home, project, repairs, data_dir):
    """Write every mismatched card and advance the watermark in ONE root transaction.

    change_no is the task's own MAX(change_log), which can sit below the watermark the
    triggering transition already advanced. The watermark moves to at least that number and
    at least its current value, so a later catch_up still replays anything newer.
    The batch also writes the summary ONCE (not per card), in the same transaction, and the
    plan_summary rows (C3) in that same transaction.
    """
    con = open_project(home, project, data_dir)
    try:
        summary = _summary_values(con)
        plans = _plan_summary_values(con)
    finally:
        con.close()
    root = open_root(home)
    try:
        _begin(root)
        try:
            hi = 0
            for r, row, status, no in repairs:
                col, attention = _placed(row, status)
                root.execute(
                    "INSERT INTO cards(project, run, task, parent, type, title, status, col, attention,"
                    " route_json, lane, round, verdict, updated, change_no)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(project, run, task) DO UPDATE SET status=excluded.status, col=excluded.col,"
                    " attention=excluded.attention, round=excluded.round, verdict=excluded.verdict,"
                    " updated=excluded.updated,"
                    " change_no=MAX(cards.change_no, excluded.change_no)",
                    (project, r["run"], r["id"], row["parent"], row["type"], row["title"], status, col,
                     attention, row["route_json"], row["lane"], row["round"],
                     row["verdict"] if "verdict" in row.keys() else None, row["updated"], no))
                hi = max(hi, no)
            root.execute(
                "INSERT INTO sync(project, last_change_no) VALUES (?,?)"
                " ON CONFLICT(project) DO UPDATE SET last_change_no=MAX(sync.last_change_no, excluded.last_change_no)",
                (project, hi))
            _write_summary(root, project, summary, hi)
            _write_plan_summaries(root, project, plans)
            root.execute("COMMIT")
        except Exception:
            root.execute("ROLLBACK")
            raise
    finally:
        root.close()


def transition(home, project, run, task, to, note="", round=0, sync_root=False, data_dir=None, verdict=_UNSET,
                 event=True, keep_updated=False, file_status=_UNSET):
    """The ONLY status writer (C10). One project transaction, then the root card iff sync_root.

    verdict defaults to leaving the task's stored verdict unchanged. Pass a string or None to
    replace it; the root card (sync_root) and a later catch_up both copy that value.
    """
    con = open_project(home, project, data_dir)
    try:
        # The read and the write share one immediate transaction, so a concurrent transition
        # cannot change prev_status between the two.
        _begin(con)
        try:
            row = con.execute(
                "SELECT parent, type, title, route_json, lane, status, prev_status, file_status, verdict, updated"
                " FROM tasks WHERE run=? AND id=?", (run, task)).fetchone()
            if not row:
                raise Fail(f"no task {run}:{task}")
            # An attention status keeps the column of the previous non-attention status (C4).
            col, attention = _placed(row, to)
            kept = row["prev_status"] if to in ATTENTION else (to if to in COLUMNS else row["prev_status"])
            # verdict= is optional: a status-only transition leaves the stored verdict alone.
            verdict = row["verdict"] if verdict is _UNSET else verdict
            stamped = row["updated"] if (keep_updated and row["updated"]) else now()
            # file_status is the published status under a kanban `ready`. A real transition
            # (the user set a status) clears it; a promotion passes the status it replaced.
            if file_status is _UNSET:
                stored_file = None if event else row["file_status"] if "file_status" in row.keys() else None
            else:
                stored_file = file_status
            con.execute(
                "UPDATE tasks SET status=?, prev_status=?, file_status=?, round=?, verdict=?, updated=? WHERE run=? AND id=?",
                (to, kept, stored_file, round, verdict, stamped, run, task))
            row = dict(row)
            row["verdict"] = verdict
            if event:
                con.execute(
                    "INSERT INTO events(run, task, t, chan, kind, to_id, text) VALUES (?,?,?,?,?,?,?)",
                    (run, task, now(), "state", "transition", to, note))
            cur = _change_log(con, run, task)
            change_no = cur.lastrowid
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        # The child's card lands before the parent rollup, so a lower change_no cannot follow a
        # higher one into the root. sync.last_change_no is also monotonic inside the upsert.
        # The summary is computed from the just-committed rows on the same connection, then
        # written in the SAME root transaction as the card (C5): a kill before it leaves both
        # missing, and catch_up heals both. plan_summary (C3) rides in that same transaction, so
        # the root's plan numbers can never lag the status this transition just committed; with
        # sync_root off it lands in its own root transaction right after the project commit.
        plans = _plan_summary_values(con)
        if sync_root:
            _upsert_card(home, project, run, task, row, to, col, attention, round, change_no, data_dir,
                         updated=stamped, summary=_summary_values(con), plans=plans)
        elif plans:
            _write_plan_summaries_root(home, project, plans)
        rolled = _rollup(con, run, row["parent"]) if row["parent"] else None
        if rolled and rolled != con.execute("SELECT status FROM tasks WHERE run=? AND id=?",
                                            (run, row["parent"])).fetchone()["status"]:
            transition(home, project, run, row["parent"], rolled, note=f"rollup from {task}",
                       round=round, sync_root=sync_root, data_dir=data_dir)
    finally:
        con.close()
    # a task reaching merged/done can unblock its dependents. ready itself is not terminal,
    # so this cannot recurse.
    # a task reaching the met set can promote its dependents; one leaving it (done -> fix)
    # can demote them. `row["status"]` is the status before this transition.
    if to in DEP_MET or row["status"] in DEP_MET:
        promote_ready(home, project, run, changed=task, data_dir=data_dir, sync_root=sync_root)
    return change_no


def _upsert_card(home, project, run, task, row, status, col, attention, round, change_no, data_dir,
                  updated=None, summary=None, plans=None):
    """C5: the root copy, in its own transaction. A failure here leaves the project change durable.

    summary (C1 project_summary values from _summary_values) and plans (C3 plan_summary values
    from _plan_summary_values) are written in the SAME root transaction, so the card and both
    rollups always land together; None skips either (a caller that only repairs a card leaves
    the rollups to its own batch).
    """
    root = open_root(home)
    try:
        _begin(root)
        try:
            root.execute(
                "INSERT INTO cards(project, run, task, parent, type, title, status, col, attention,"
                " route_json, lane, round, verdict, updated, change_no)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(project, run, task) DO UPDATE SET status=excluded.status, col=excluded.col,"
                " attention=excluded.attention, round=excluded.round, verdict=excluded.verdict,"
                " updated=excluded.updated,"
                " change_no=MAX(cards.change_no, excluded.change_no)",
                (project, run, task, row["parent"], row["type"], row["title"], status, col, attention,
                 row["route_json"], row["lane"], round, row["verdict"] if "verdict" in row.keys() else None,
                 updated or now(), change_no))
            root.execute(
                "INSERT INTO sync(project, last_change_no) VALUES (?,?)"
                " ON CONFLICT(project) DO UPDATE SET last_change_no=MAX(sync.last_change_no, excluded.last_change_no)",
                (project, change_no))
            if summary is not None:
                _write_summary(root, project, summary, change_no)
            if plans is not None:
                _write_plan_summaries(root, project, plans)
            root.execute("COMMIT")
        except Exception:
            root.execute("ROLLBACK")
            raise
    finally:
        root.close()


def catch_up(home, project, data_dir=None):
    """Replay project changes the root has not seen yet (C5 sync.last_change_no).

    Also repairs a card whose status disagrees with the task row even when every change_log
    row for that task sits at or below the watermark: a kill between a transition and the
    card repair leaves exactly that. The repair uses the task's own MAX(change_no) and does
    not move the watermark backwards.

    C1: the summary is written ONCE after the replays (never per card), carrying the
    watermark as its change_no — so a store whose cards are current but whose summary is
    missing or stale (a kill between the card writes and this one) heals here too. The
    write is skipped when the stored row already equals the recomputed one, so a second
    catch_up over a synced store writes nothing.
    """
    con = open_project(home, project, data_dir)
    try:
        rows = con.execute("SELECT t.run, t.id, t.parent, t.type, t.title, t.route_json, t.lane,"
                           " t.status, t.prev_status, t.round, t.verdict, t.updated, c.change_no FROM change_log c"
                           " JOIN tasks t ON t.run=c.run AND t.id=c.task ORDER BY c.change_no").fetchall()
        tasks = con.execute("SELECT run, id, parent, type, title, route_json, lane, status,"
                            " prev_status, round, verdict, updated FROM tasks").fetchall()
        nos = {(r["run"], r["task"]): r["c"] for r in con.execute(
            "SELECT run, task, MAX(change_no) c FROM change_log GROUP BY run, task")}
        summary = _summary_values(con)
        plans = _plan_summary_values(con)
    finally:
        con.close()
    root = open_root(home)
    try:
        seen = root.execute("SELECT last_change_no FROM sync WHERE project=?", (project,)).fetchone()
        cards_now = {(c["run"], c["task"]): c["status"] for c in
                     root.execute("SELECT run, task, status FROM cards WHERE project=?", (project,))}
    finally:
        root.close()
    last = seen["last_change_no"] if seen else 0
    n = 0
    for r in rows:
        if r["change_no"] <= last:
            continue
        col, attention = _placed(r)
        _upsert_card(home, project, r["run"], r["id"], r, r["status"], col, attention,
                     r["round"], r["change_no"], data_dir, updated=r["updated"])
        cards_now[(r["run"], r["id"])] = r["status"]
        n += 1
    for t in tasks:
        key = (t["run"], t["id"])
        if cards_now.get(key) == t["status"]:
            continue
        no = nos.get(key)
        if not no:
            continue
        col, attention = _placed(t)
        _upsert_card(home, project, t["run"], t["id"], t, t["status"], col, attention,
                     t["round"], no, data_dir, updated=t["updated"])
        n += 1
    # C1/C3: the one rollup write. The compare-then-write reads happen outside a transaction
    # (single writer: nothing else moves the root between them), and the write only opens
    # one when a row is missing or differs, so the idempotent pass commits nothing. plan_summary
    # compares on the five value columns only — `updated` is the writer's stamp, so including it
    # would make every pass differ and break idempotence.
    root = open_root(home)
    try:
        seen = root.execute("SELECT last_change_no FROM sync WHERE project=?", (project,)).fetchone()
        have = root.execute(
            "SELECT active_runs, by_status_json, attention, last_event, change_no"
            " FROM project_summary WHERE project=?", (project,)).fetchone()
        want_no = seen["last_change_no"] if seen else 0
        want_plans = {p: (title, status, counts, live) for p, title, status, counts, live in plans}
        have_plans = {r["plan"]: (r["title"], r["status"], r["counts_json"], r["live_run"]) for r in
                      root.execute("SELECT plan, title, status, counts_json, live_run"
                                   " FROM plan_summary WHERE project=?", (project,))}
        if have is None or tuple(have) != (*summary, want_no) or have_plans != want_plans:
            _begin(root)
            try:
                if have is None or tuple(have) != (*summary, want_no):
                    _write_summary(root, project, summary, want_no)
                _write_plan_summaries(root, project, plans)
                root.execute("COMMIT")
            except Exception:
                root.execute("ROLLBACK")
                raise
    finally:
        root.close()
    return n


RUN_COLS = ("id", "plan", "plan_version", "branch", "status", "stage", "next", "started", "ended",
            "extra_json")
TASK_COLS = ("id", "parent", "type", "title", "acceptance", "owned", "deps", "parallel",
             "route_json", "status", "lane", "slot", "branch", "pane", "round", "verdict", "updated",
             "extra_json")
# D1.5.2: the columns the mirror (import_history's upsert) never blanks — the approved plan's
# acceptance/owned/deps/route_json/type survive a re-import whose file view carries no value
# for them (a run.json written before D1.5.1, or any task the file view does not populate).
# L4 adds extra_json: a mirror write whose file view carries no extras must keep the stored ones.
MERGE_KEEP = ("acceptance", "owned", "deps", "route_json", "type", "extra_json")
EVENT_COLS = ("t", "chan", "kind", "from_id", "to_id", "reply_to", "text", "meta_json", "task")
ARTIFACT_COLS = ("task", "round", "kind", "rev", "text", "created")


def _src_of(meta_json):
    """The importer's stable source key, stored as meta_json.src. Absent means a live event."""
    if not meta_json:
        return None
    try:
        meta = json.loads(meta_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    return meta.get("src")


def _with_src(meta_json, src):
    """Put src into meta_json without dropping the keys the importer already set."""
    if meta_json in (None, ""):
        meta = {}
    else:
        try:
            meta = json.loads(meta_json)
        except (TypeError, ValueError):
            raise Fail(f"bad meta_json: {meta_json!r}")
        if not isinstance(meta, dict):
            raise Fail(f"bad meta_json: expected an object, got {meta_json!r}")
    meta["src"] = src
    return json.dumps(meta, sort_keys=True, ensure_ascii=False)


def _parents_first(tasks):
    """Insert order: a parent before its children. A cycle among parents is named and refused."""
    by_id = {}
    for task in tasks:
        tid = task.get("id")
        if not tid or tid in by_id:
            raise Fail(f"bad task id {tid!r}")
        by_id[tid] = task
    edges = {tid: [t["parent"]] if t.get("parent") else [] for tid, t in by_id.items()}
    order, pending = [], set(by_id)
    while pending:
        ready = sorted(t for t in pending if all(p not in pending for p in edges[t]))
        if not ready:
            start = sorted(pending)[0]
            cycle = _cycle_through(start, {t: edges[t] for t in pending})
            raise Fail("cycle: " + " -> ".join(cycle or sorted(pending)))
        order.extend(ready)
        pending -= set(ready)
    return [by_id[t] for t in order]


def import_history(home, project, run, *, run_row=None, tasks=(), events=(), artifacts=(), data_dir=None):
    """Import one historical run with its original values (D1B). The only history writer.

    One BEGIN IMMEDIATE transaction for the whole call; any error rolls it back, so the store is
    unchanged. Idempotent: events and artifacts carry a stable source key (meta_json.src, and the
    artifacts.src column) and a repeat imports nothing. Tasks and the run row upsert. No card
    is written here — change_log rows are written so catch_up() can build the cards later.
    C3: the plan_summary rows ARE refreshed after the commit, in their own root transaction,
    because an import moves task statuses (and a run row, which live_run reads); a store with
    no plans row writes nothing. Returns counts of rows actually added.
    """
    con = open_project(home, project, data_dir)
    try:
        _begin(con)
        try:
            added = {"runs": 0, "tasks": 0, "events": 0, "artifacts": 0}
            if run_row is not None:
                row = {c: run_row.get(c) for c in RUN_COLS}
                if row["id"] != run:
                    raise Fail(f"run_row id {row['id']!r} != run {run!r}")
                before = con.execute("SELECT 1 FROM runs WHERE id=?", (run,)).fetchone()
                # L4 (L4.1c): a run_row that carries no extras must not blank the stored ones.
                # extra_json keeps the stored value when the incoming one is NULL, exactly the
                # MERGE_KEEP rule the task upsert below applies to its own columns.
                run_assign = [f"{c}=excluded.{c}" for c in RUN_COLS if c not in ("id", "extra_json")]
                run_assign += ["extra_json=COALESCE(excluded.extra_json, runs.extra_json)"]
                con.execute(
                    "INSERT INTO runs(" + ",".join(RUN_COLS) + ") VALUES (" + ",".join("?" * len(RUN_COLS)) + ")"
                    " ON CONFLICT(id) DO UPDATE SET " + ",".join(run_assign),
                    tuple(row[c] for c in RUN_COLS))
                if not before:
                    added["runs"] = 1
            ordered = _parents_first(list(tasks))
            # Deps are checked against the batch plus what is already stored, so a dep may name a
            # task later in the same batch. The graph is built once and extended as rows insert.
            known = {(r["run"], r["id"]): _deps_of(r["deps"])
                     for r in con.execute("SELECT run, id, deps FROM tasks")}
            batch_ids = {t["id"] for t in ordered}
            for t in ordered:
                if t.get("type") not in TYPES:
                    raise Fail(f"bad type {t.get('type')!r}: expected phase|task|subtask")
                check_route(t.get("route_json"))
                parent = t.get("parent")
                if parent and parent not in batch_ids and not con.execute(
                        "SELECT 1 FROM tasks WHERE run=? AND id=?", (run, parent)).fetchone():
                    raise Fail(f"missing parent: {parent}")
                me = (run, t["id"])
                deps = _deps_of(t.get("deps"))
                targets = []
                for dep in deps:
                    target = _dep_target(run, dep)
                    if target == me:
                        raise Fail(f"self-dep: {t['id']} depends on itself")
                    if target[0] == run and target[1] not in batch_ids and target not in known:
                        raise Fail(f"missing dep: {dep} is not a task in this project")
                    if target[0] != run and target not in known:
                        raise Fail(f"missing dep: {dep} is not a task in this project")
                    targets.append(target)
                known[me] = deps
                edges = {k: [_dep_target(k[0], d) for d in v] for k, v in known.items()}
                path = _cycle_through(me, edges)
                if path:
                    raise Fail("cycle: " + " -> ".join(f"{r_}:{t_}" for r_, t_ in path))
                status = t.get("status") or "planned"
                # An attention status keeps the column of the status it names as previous; the
                # importer has no earlier row, so prev_status is the status itself when it is a
                # column status and NULL otherwise (column_of then uses Backlog).
                prev = status if status in COLUMNS else None
                column_of(status, prev)  # refuse an unknown status before anything is written
                existed = con.execute("SELECT * FROM tasks WHERE run=? AND id=?", me).fetchone()
                # file_status is not an import column; the store owns it. A re-import keeps the
                # stored one when the incoming status is `ready` (the file view of a planned
                # task whose ready flag is set), and keeps the stored `ready` + file_status
                # when the incoming status IS the stored file_status (the file view of a
                # routed task the store already holds ready) — otherwise every ordinary write
                # would demote it to `routed` and let promote_ready re-promote it, two
                # change_log rows and two card writes each time. Any other status clears it:
                # the published status is then the status itself.
                keep_file = None
                if existed and existed["status"] == "ready" and status in READY_FROM \
                        and existed["file_status"] == status:
                    # the promotion stored `ready` as prev_status, which is not a column
                    # status; recomputing it here would differ and log a change every write
                    status, keep_file, prev = "ready", existed["file_status"], existed["prev_status"]
                elif existed and status == "ready" and existed["file_status"] in READY_FROM:
                    keep_file = existed["file_status"]
                cols = ["run"] + list(TASK_COLS) + ["prev_status", "file_status"]
                values = [run] + [t.get(c) for c in TASK_COLS] + [prev, keep_file]
                values[cols.index("status")] = status
                # D1.5.2: the mirror never blanks an approved-plan column. A live file view
                # that carries no value for acceptance/owned/deps/route_json/type keeps the
                # stored one (COALESCE); an empty string from the file view counts as absent
                # for the csv columns (the file view renders an empty list as ""), and an
                # empty list counts as absent in the row dict. Status/lane/round/verdict keep
                # the plain overwrite semantics.
                keep_cols = []
                for c in cols:
                    if c in ("run", "id", "status", "prev_status", "file_status"):
                        continue
                    if c not in MERGE_KEEP:
                        continue
                    # the file view's "no list" rides as ""; an omitted key reads as None
                    if values[cols.index(c)] in (None, ""):
                        keep_cols.append(c)
                assign = [f"{c}=excluded.{c}" for c in cols
                          if c not in ("run", "id") and c not in keep_cols]
                # NULLIF: the file view renders an empty list as "" for owned/deps and the
                # importer writes "" for a missing acceptance, so "" must read as "absent"
                # before COALESCE can keep the stored value.
                assign += [f"{c}=COALESCE(NULLIF(excluded.{c}, ''), tasks.{c})" for c in keep_cols]
                con.execute(
                    "INSERT INTO tasks(" + ",".join(cols) + ") VALUES (" + ",".join("?" * len(cols)) + ")"
                    " ON CONFLICT(run, id) DO UPDATE SET " + ",".join(assign),
                    tuple(values))
                if not existed:
                    added["tasks"] += 1
                    _change_log(con, run, t["id"])
                else:
                    # A re-import logs a change only when a column actually moved, so catch_up()
                    # rebuilds the card; identical data adds nothing, change_log included. A kept
                    # column compares its EFFECTIVE value (the stored one it did not blank), so a
                    # re-import that omits a field never logs a change either.
                    effective = list(values)
                    for c in keep_cols:
                        if effective[cols.index(c)] in (None, ""):
                            effective[cols.index(c)] = existed[c]
                    changed = any(existed[c] != v for c, v in zip(cols, effective))
                    if changed:
                        _change_log(con, run, t["id"])
            seen_src = set()
            for ev in events:
                src = ev.get("src")
                if not src:
                    raise Fail("event missing src")
                if src in seen_src:
                    raise Fail(f"duplicate src in one import: {src}")
                seen_src.add(src)
                meta = _with_src(ev.get("meta_json"), src)
                if con.execute("SELECT 1 FROM event_src WHERE run=? AND src=?", (run, src)).fetchone():
                    continue
                con.execute(
                    "INSERT INTO events(run, task, t, chan, kind, from_id, to_id, reply_to, text, meta_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (run, ev.get("task"), ev.get("t"), ev.get("chan"), ev.get("kind"),
                     ev.get("from_id"), ev.get("to_id"), ev.get("reply_to"), ev.get("text"), meta))
                con.execute("INSERT INTO event_src(run, src) VALUES (?,?)", (run, src))
                added["events"] += 1
            seen_art = set()
            for art in artifacts:
                src = art.get("src")
                if not src:
                    raise Fail("artifact missing src")
                if src in seen_art:
                    raise Fail(f"duplicate artifact src in one import: {src}")
                seen_art.add(src)
                if con.execute("SELECT 1 FROM artifacts WHERE run=? AND src=?", (run, src)).fetchone():
                    continue
                text = art.get("text")
                if not isinstance(text, str):
                    raise Fail(f"artifact text must be a string, got {type(text).__name__}")
                con.execute(
                    "INSERT INTO artifacts(run, task, round, kind, rev, text, created, src)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (run, art.get("task"), art.get("round"), art.get("kind"), art.get("rev"),
                     text, art.get("created"), src))
                added["artifacts"] += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        plans = _plan_summary_values(con)
    finally:
        con.close()
    _write_plan_summaries_root(home, project, plans)
    return added


# Live bus writers (D1D). change_log is written only when a task row changes — the same rule
# import_history uses — so these helpers do not write change_log: they never touch tasks.status.
LIVE_CHANS = ("state", "bus", "note")
ARTIFACT_MAX = 256 * 1024


def _require_run(con, run):
    if not isinstance(run, str) or not run:
        raise Fail(f"invalid run {run!r}")
    if not con.execute("SELECT 1 FROM runs WHERE id=?", (run,)).fetchone():
        raise Fail(f"no run {run}")


def _require_task(con, run, task):
    if task is None:
        return
    if not isinstance(task, str) or not task:
        raise Fail(f"invalid task {task!r}")
    if not con.execute("SELECT 1 FROM tasks WHERE run=? AND id=?", (run, task)).fetchone():
        raise Fail(f"no task {run}:{task}")


def append_event(home, project, run, *, task=None, chan, kind, from_id=None, to_id=None,
                 reply_to=None, text="", meta=None, artifact_id=None, t=None, data_dir=None):
    """Insert ONE live events row and return its id (the bus SSE Last-Event-ID).

    Does not touch tasks.status and does not write change_log (import_history's rule: a change_log
    row is written only when a task row changes). chan='state' with kind='transition' is refused —
    status changes go through transition() only (C10).
    """
    if chan not in LIVE_CHANS:
        raise Fail(f"unknown chan {chan!r}: expected state|bus|note")
    if chan == "state" and kind == "transition":
        raise Fail("chan='state' kind='transition' is refused; status changes go through transition()")
    if not isinstance(kind, str) or not kind:
        raise Fail(f"invalid kind {kind!r}")
    if not isinstance(text, str):
        raise Fail(f"event text must be a string, got {type(text).__name__}")
    if meta is None:
        meta_json = None
    elif isinstance(meta, dict):
        meta_json = json.dumps(meta, sort_keys=True, ensure_ascii=False)
    else:
        raise Fail(f"meta must be a dict or None, got {type(meta).__name__}")
    con = open_project(home, project, data_dir)
    try:
        _begin(con)
        try:
            _require_run(con, run)
            _require_task(con, run, task)
            if artifact_id is not None:
                art = con.execute("SELECT 1 FROM artifacts WHERE id=? AND run=?",
                                  (artifact_id, run)).fetchone()
                if not art:
                    raise Fail(f"no artifact {artifact_id} in run {run}")
            cur = con.execute(
                "INSERT INTO events(run, task, t, chan, kind, from_id, to_id, reply_to, text,"
                " artifact_id, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run, task, t or now(), chan, kind, from_id, to_id, reply_to, text,
                 artifact_id, meta_json))
            eid = cur.lastrowid
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return eid


def append_ack(home, project, run, msg, lane, level, *, t=None, data_dir=None):
    """INSERT OR IGNORE into acks. Level never decreases. True when a row was inserted or upgraded."""
    if not isinstance(msg, str) or not msg:
        raise Fail(f"invalid msg {msg!r}")
    if not isinstance(lane, str) or not lane:
        raise Fail(f"invalid lane {lane!r}")
    if isinstance(level, bool) or not isinstance(level, int):
        raise Fail(f"invalid level {level!r}")
    con = open_project(home, project, data_dir)
    try:
        _begin(con)
        try:
            _require_run(con, run)
            row = con.execute("SELECT level FROM acks WHERE run=? AND msg=? AND lane=?",
                              (run, msg, lane)).fetchone()
            stamp = t or now()
            if row is None:
                con.execute("INSERT INTO acks(run, msg, lane, level, t) VALUES (?,?,?,?,?)",
                            (run, msg, lane, level, stamp))
                changed = True
            elif level > row["level"]:
                con.execute("UPDATE acks SET level=?, t=? WHERE run=? AND msg=? AND lane=?",
                            (level, stamp, run, msg, lane))
                changed = True
            else:
                changed = False
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return changed


def put_artifact(home, project, run, task, round, kind, text, *, event_id=None, data_dir=None):
    """Insert one live artifact (src stays NULL). rev is 1 + the highest rev for the key.

    When event_id is given, events.artifact_id is set on that event in the same transaction.
    Text larger than 256 KiB is refused. A failure after the insert rolls the whole transaction
    back, so neither the artifact nor the link is left behind.
    """
    if not isinstance(task, str) or not task:
        raise Fail(f"invalid task {task!r}")
    if isinstance(round, bool) or not isinstance(round, int):
        raise Fail(f"invalid round {round!r}")
    if not isinstance(kind, str) or not kind:
        raise Fail(f"invalid kind {kind!r}")
    if not isinstance(text, str):
        raise Fail(f"artifact text must be a string, got {type(text).__name__}")
    if len(text.encode("utf-8")) > ARTIFACT_MAX:
        raise Fail(f"artifact text exceeds {ARTIFACT_MAX} bytes")
    con = open_project(home, project, data_dir)
    try:
        _begin(con)
        try:
            _require_run(con, run)
            _require_task(con, run, task)
            if event_id is not None:
                ev = con.execute("SELECT 1 FROM events WHERE id=? AND run=?", (event_id, run)).fetchone()
                if not ev:
                    raise Fail(f"no event {event_id} in run {run}")
            top = con.execute(
                "SELECT MAX(rev) m FROM artifacts WHERE run=? AND task=? AND round=? AND kind=?",
                (run, task, round, kind)).fetchone()["m"]
            rev = 1 if top is None else top + 1
            cur = con.execute(
                "INSERT INTO artifacts(run, task, round, kind, rev, text, created, src)"
                " VALUES (?,?,?,?,?,?,?,NULL)",
                (run, task, round, kind, rev, text, now()))
            aid = cur.lastrowid
            if event_id is not None:
                con.execute("UPDATE events SET artifact_id=? WHERE id=? AND run=?",
                            (aid, event_id, run))
            if os.environ.get("MKDB_FAIL_AFTER_ARTIFACT"):
                raise Fail("injected failure after the artifact insert")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return {"id": aid, "rev": rev}


def timeline(home, project, run, task, data_dir=None):
    """SELECT … FROM events WHERE run=? AND task=? ORDER BY id.

    A missing store is an empty timeline: no file and no directory is created.
    """
    path = os.path.join(project_dir(home, project, data_dir), "mk.db")
    con = _open_read(path, SCHEMA)
    if con is None:
        return []
    try:
        return [dict(r) for r in con.execute(
            "SELECT id, run, task, t, chan, kind, to_id, text FROM events"
            " WHERE run=? AND task=? ORDER BY id", (run, task))]
    finally:
        con.close()


EVENT_COLS_READ = ("id", "run", "task", "t", "chan", "kind", "from_id", "to_id", "reply_to",
                   "text", "meta_json")


def events(home, project, run, data_dir=None, task=None, chan="state", since=None):
    """One run's events, in id order — the run-wide feed timeline() cannot serve (L5).

    timeline() needs --task and drops from_id/reply_to/meta_json; wake.ts polls the whole run
    with the events.id cursor, so this adds the two selectors it needs: --task narrows to one
    task and --since returns the rows past that id (the cursor, one indexed range scan).
    `chan` defaults to 'state' — the chan mk-state writes the run document and events.jsonl
    from; pass '' for every channel. Read-only like timeline(): a missing store is an empty
    list, no file and no directory is created. meta_json carries `by` and the source key; a
    direct mkdb.transition() leaves it NULL, which the reader maps to "orchestrator".
    """
    con = _open_read(_store_path(home, project, data_dir), SCHEMA)
    if con is None:
        return []
    where, params = ["run=?"], [run]
    if task:
        where.append("task=?")
        params.append(task)
    if chan:
        where.append("chan=?")
        params.append(chan)
    if since is not None:
        where.append("id>?")
        params.append(since)
    try:
        return [dict(r) for r in con.execute(
            f"SELECT {', '.join(EVENT_COLS_READ)} FROM events WHERE {' AND '.join(where)}"
            " ORDER BY id", params)]
    finally:
        con.close()


def _store_present(home, project, data_dir):
    """Whether this project's store file exists — the marker a read-only CLI reply carries.

    A stat, not an open: a reader that already holds the connection gets the flag from
    _store_open, and one that only needs the flag does not pay for a second open."""
    return os.path.isfile(_store_path(home, project, data_dir))


def cards(home, project=None):
    """Read the root store's cards, optionally one project.

    A missing root store is an empty card list: <home>/mk.db is not created.
    """
    check_home(home)
    root = _open_read(os.path.join(home, "mk.db"), ROOT_SCHEMA)
    if root is None:
        return []
    try:
        if project:
            rows = root.execute("SELECT * FROM cards WHERE project=? ORDER BY change_no", (project,))
        else:
            rows = root.execute("SELECT * FROM cards ORDER BY project, change_no")
        return [dict(r) for r in rows]
    finally:
        root.close()


def projects(home):
    """Read the root store's projects, ordered by slug.

    A missing root store is an empty list: <home>/mk.db is not created.
    """
    check_home(home)
    root = _open_read(os.path.join(home, "mk.db"), ROOT_SCHEMA)
    if root is None:
        return []
    try:
        return [dict(r) for r in root.execute("SELECT * FROM projects ORDER BY slug")]
    finally:
        root.close()


OVERVIEW_PROJECT_COLS = ("slug", "name", "repo", "data_dir", "mode", "db_path")
OVERVIEW_PLAN_COLS = ("project", "plan", "title", "status", "counts", "live_run", "updated")


def overview(home):
    """The root store's numbers and linkage (C3): every project with its db_path, every plan with
    its counts and live run. Nothing else — no card, no task, no event, no project store.

    Read-only like cards()/projects(): mode=ro, never migrates, never writes. A missing root
    store is an empty overview, not an error, and <home>/mk.db is not created. counts_json
    arrives as a parsed object ({} when NULL or unparsable), never as a JSON string.
    """
    check_home(home)
    root = _open_read(os.path.join(home, "mk.db"), ROOT_SCHEMA)
    if root is None:
        return {"ok": True, "projects": [], "plans": []}
    try:
        projs = [{c: r[c] for c in OVERVIEW_PROJECT_COLS} for r in
                 root.execute("SELECT " + ",".join(OVERVIEW_PROJECT_COLS) + " FROM projects ORDER BY slug")]
        plans_ = []
        for r in root.execute("SELECT " + ",".join(
                ("project", "plan", "title", "status", "counts_json", "live_run", "updated")) +
                " FROM plan_summary ORDER BY project, plan"):
            row = {c: r[c] for c in OVERVIEW_PLAN_COLS if c != "counts"}
            try:
                counts = json.loads(r["counts_json"] or "")
            except (TypeError, ValueError):
                counts = {}
            row["counts"] = counts if isinstance(counts, dict) else {}
            plans_.append(row)
        return {"ok": True, "projects": projs, "plans": plans_}
    finally:
        root.close()


def _runs_of(con, pid):
    """The runs of plan pid, newest first (C5).

    runs.plan holds the plan id on rows written after plan ids landed; older rows hold the
    plan file path — '<repo>/.matrixkoder/plans/<id>.md' relative or absolute — so a path
    whose leaf under a plans/ dir names the plan counts too. A plan id is a slug: no LIKE
    metacharacters reach the pattern.
    """
    rows = con.execute(
        "SELECT id, status, plan FROM runs WHERE plan = ? OR plan = 'plans/' || ? || '.md'"
        " OR plan LIKE '%/plans/' || ? || '.md' OR plan LIKE '%plans/archive/' || ? || '-v%.md'"
        " ORDER BY started DESC, rowid DESC",
        (pid, pid, pid, pid)).fetchall()
    # an archive copy plans/archive/<id>-v<n>.md links too; the regex keeps plan "a" off "a-v2-v1.md"
    arch = re.compile(r"(?:^|/)plans/archive/" + re.escape(pid) + r"-v\d+\.md$")
    return [r for r in rows if "plans/archive/" not in (r["plan"] or "") or arch.search(r["plan"])]


def _hdr_of(row):
    """The parsed header_json of a plans row, or {} when missing or not an object."""
    try:
        got = json.loads(row["header_json"] or "")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _design_of(row):
    """The design column as a list. NULL or unparsable text is [] (the reply type is a list)."""
    if not row["design"]:
        return []
    try:
        got = json.loads(row["design"])
    except (TypeError, ValueError):
        return []
    return got if isinstance(got, list) else []


def _col(row, name):
    """A column read that tolerates a row without it (a v3-shaped row, a narrower SELECT)."""
    return row[name] if name in row.keys() else None


def _slot_of(text):
    """tasks.slot is TEXT; the reply carries int when it parses, else null (V1.5.1)."""
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _route_of(text):
    """route_json as {agent, model, effort}: null when empty, unparsable, or not an object."""
    if not text:
        return None
    try:
        got = json.loads(text)
    except (TypeError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def _routing_of(r):
    """The per-task routing fields every task/subtask node in a plans detail reply carries."""
    return {"slot": _slot_of(_col(r, "slot")), "pane": _col(r, "pane"), "branch": _col(r, "branch"),
            "updated": _col(r, "updated"), "acceptance": _col(r, "acceptance"),
            "owned": _deps_of(_col(r, "owned")), "deps": _deps_of(_col(r, "deps")),
            "route": _route_of(_col(r, "route_json"))}


def _phases_from_run(rows, header):
    """Phases with their tasks from one run's task rows, in row order (C5).

    A phase row is a type='phase' row or any root (no parent): run init writes the plan's
    phases as parentless task rows. Children of a phase are its tasks; deeper children
    (type='subtask' or any row under a task) nest under their parent task as subtasks.
    `wave` has no run column; it is cross-filled from header_json.phases by id.
    V1.5.1: every phase-level node carries the routing keys too — a round's lane tasks sit
    at phase level (root rows the live mirror may type 'phase' or 'task'), and their slot/
    pane/branch/route live on the row itself, null-defaulted when the row carries none.
    """
    hwave = {str(p.get("id")): p.get("wave") for p in header.get("phases") or []
             if isinstance(p, dict) and p.get("id")}
    ids = {r["id"] for r in rows}
    phases, tasks, subs = {}, {}, {}
    for r in rows:  # add_task and import insert a parent before its children, so one pass is safe
        node = {"id": r["id"], "title": r["title"], "status": r["status"], "parent": r["parent"],
                "type": r["type"], "round": r["round"], "verdict": r["verdict"], **_routing_of(r),
                "subtasks": []}
        if not r["parent"] or r["parent"] not in ids:
            ph = {"id": r["id"], "title": r["title"], "wave": hwave.get(r["id"]),
                  "deps": _deps_of(r["deps"]), "status": r["status"]}
            ph.update(_routing_of(r))
            ph["tasks"] = []
            phases[r["id"]] = ph
        elif r["parent"] in phases:
            tasks[r["id"]] = node
            phases[r["parent"]]["tasks"].append(node)
        else:
            subs[r["id"]] = node
    for r in rows:  # second pass: a subtask nests under its parent node, any depth
        node = subs.get(r["id"])
        if not node:
            continue
        parent = tasks.get(r["parent"]) or subs.get(r["parent"])
        if parent:
            parent["subtasks"].append(node)
    return list(phases.values())


def _phases_from_header(header):
    """The same phase shape from header_json.phases, every status 'planned' (no run yet).

    Task/subtask nodes carry the same routing keys as _phases_from_run, defaulted: no run
    has routed anything yet, so slot/pane/branch/updated/acceptance/route are null and
    owned is []; deps comes from the header task dict when present (V1.5.1 shape parity).
    Phase-level nodes carry the same defaulted keys, parity with the run view.
    """
    out = []
    for ph in header.get("phases") or []:
        if not isinstance(ph, dict):
            continue
        pid = str(ph.get("id", ""))

        def _node(nid, d, parent, type_):
            return {"id": nid, "title": d.get("title", ""), "status": "planned", "parent": parent,
                    "type": type_, "round": None, "verdict": None, "slot": None, "pane": None,
                    "branch": None, "updated": None, "acceptance": None, "owned": [],
                    "deps": list(d.get("deps") or []), "route": None, "subtasks": []}

        tasks = []
        for t in ph.get("tasks") or []:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id", ""))
            task = _node(tid, t, pid, "task")
            task["subtasks"] = [_node(str(s.get("id", "")), s, tid, "subtask")
                                for s in t.get("subtasks") or [] if isinstance(s, dict)]
            tasks.append(task)
        out.append({"id": pid, "title": ph.get("title", ""), "wave": ph.get("wave"),
                    "deps": list(ph.get("deps") or []), "status": "planned", "slot": None,
                    "pane": None, "branch": None, "updated": None, "acceptance": None,
                    "owned": [], "route": None, "tasks": tasks})
    return out


def _integration_of(text):
    """mk-plan stores integration as a JSON array (one header key may hold one value); the pane
    reads one branch name. A plain string is passed through."""
    if isinstance(text, str) and text.startswith("["):
        try:
            got = json.loads(text)
        except ValueError:
            return text
        if isinstance(got, list):
            return ", ".join(str(x) for x in got) or None
    return text


def _plan_obj(con, row, detail=False):
    """One plans row as the C8 reply object. status/created fall back to header_json when the
    column is NULL (rows written before the v4 migration); design/integration have no fallback."""
    header = _hdr_of(row)
    obj = {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"] if row["status"] is not None else header.get("status"),
        "created": row["created"] if row["created"] is not None else header.get("created"),
        "design": _design_of(row),
        "integration": _integration_of(row["integration"]),
        "version": row["version"],
        "approved_at": row["approved_at"],
    }
    runs = _runs_of(con, row["id"])
    obj["runs"] = [r["id"] for r in runs]
    obj["live_run"] = next((r["id"] for r in runs if r["status"] == "active"), None)
    counts = {}
    phases = []
    if runs:
        newest = runs[0]["id"]
        rows = con.execute(
            "SELECT id, parent, type, title, status, deps, round, verdict, acceptance, owned,"
            " route_json, slot, branch, pane, updated FROM tasks"
            " WHERE run=? ORDER BY rowid", (newest,)).fetchall()
        ids = {r["id"] for r in rows}
        roots = {r["id"] for r in rows if not r["parent"] or r["parent"] not in ids}
        for r in rows:  # counts cover the run's tasks; phase-level (root) rows are not tasks
            if r["id"] not in roots:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
        if detail:
            phases = _phases_from_run(rows, header)
    elif detail:
        phases = _phases_from_header(header)
    obj["counts"] = counts
    if detail:
        obj["phases"] = phases
    return obj


def plans(home, project=None, data_dir=None, plan_id=None):
    """Read the project store's plans (C5, C8). Read-only: mode=ro, never migrates, never writes.

    A missing store is an empty list, like timeline/cards. An unknown plan id is a Fail (exit 2),
    the same error shape as the other subcommands.
    """
    path = os.path.join(project_dir(home, project, data_dir), "mk.db")
    con = _open_read(path, SCHEMA)
    if con is None:
        if plan_id:
            raise Fail(f"no plan {plan_id!r}: no store at {path}")
        return {"ok": True, "plans": []}
    try:
        if plan_id:
            row = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise Fail(f"no plan {plan_id!r} in {path}")
            return {"ok": True, **_plan_obj(con, row, detail=True)}
        rows = con.execute("SELECT * FROM plans ORDER BY id").fetchall()
        return {"ok": True, "plans": [_plan_obj(con, row) for row in rows]}
    finally:
        con.close()


# The run-doc artifact kind mk-state writes: the run.json document, minus the columns the runs
# and tasks tables carry. L5: run-snapshot reads the tables for the columns and this artifact for
# the file-only keys (feature, waiting, recent, and each task's wave/ready/gates/rounds/last_commit).
RUN_DOC = "run-doc"


def _run_doc(con, run):
    """The newest run-doc artifact of run as a dict, or {} when there is none (or it is not JSON).

    The artifact is a whole run.json written by mk-state on every store commit; mk-state's own
    load_store overlays the columns on top of it, and so does run_snapshot. A run with no
    artifact (a plain mkdb writer, an imported row set) still reads: the columns carry it.
    """
    row = con.execute(
        "SELECT text FROM artifacts WHERE run=? AND kind=? AND task IS NULL ORDER BY id DESC LIMIT 1",
        (run, RUN_DOC)).fetchone()
    if row is None:
        return {}
    try:
        got = json.loads(row["text"] or "")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _stage_of(row):
    """runs.stage is TEXT ('6'); the run document carries an int. A non-numeric column is 0."""
    text = str(row["stage"] or "")
    return int(text) if text.isdigit() else 0


def _published_status(row, ready):
    """The status the run document published. The store's `ready` is a kanban column, not a
    status: it publishes as file_status (`planned` or `routed`) plus ready=True. Every other
    status is the column's own word. load_store computes the pair exactly this way."""
    status = row["status"] or "planned"
    if status == "ready":
        return (row["file_status"] if row["file_status"] in ("planned", "routed") else "planned"), True
    return status, ready


def _task_obj(row, extra):
    """One task as the run document held it: the artifact dict overlaid with the table columns.

    The columns win (the store is the read source); the artifact supplies the file-only keys
    (wave, ready, gates, rounds, last_commit, reviewer, plan_phase). `type` is not overlaid,
    matching load_store: it is derived per row, so a legacy task keeps the spelling it was
    written with. acceptance is overlaid only when the column is non-empty, so a legacy task
    does not gain a key it never had.
    """
    route = _route_of(row["route_json"]) or {}
    obj = dict(extra)
    status, ready = _published_status(row, bool(obj.get("ready")))
    obj.update({
        "id": row["id"], "title": row["title"] or "", "parent": row["parent"],
        "deps": _deps_of(row["deps"]), "owned": _deps_of(row["owned"]), "status": status,
        "lane": row["lane"], "slot": row["slot"], "branch": row["branch"], "pane": row["pane"],
        "round": row["round"] or 0, "verdict": row["verdict"], "updated": row["updated"],
        "ready": ready,
        "agent": route.get("agent"), "model": route.get("model"), "effort": route.get("effort"),
    })
    if "type" not in obj:  # no run-doc artifact, or one that omits it: the column is the only source
        obj["type"] = row["type"] or "task"
    if row["acceptance"]:
        obj["acceptance"] = row["acceptance"]
    if route.get("route"):
        obj["route"] = route["route"]
    return obj


def _run_obj(con, row, doc):
    """One run as the run document held it, with its tasks in the document's order.

    runs.plan holds the plan id on rows mk-state wrote; the artifact's `plan` is the plan file
    path, and it wins when the column only repeated the id (load_store's rule). A task row the
    artifact does not list yet still appears, after the listed ones, in rowid order.
    """
    plan = row["plan"]
    if doc.get("plan_id") and plan == doc.get("plan_id") and doc.get("plan"):
        plan = doc["plan"]
    rows = con.execute("SELECT * FROM tasks WHERE run=? ORDER BY rowid", (row["id"],)).fetchall()
    by_id = {r["id"]: r for r in rows}
    extra = {t.get("id"): t for t in doc.get("tasks") or []
             if isinstance(t, dict) and t.get("id")}
    ordered, seen = [], set()
    for tid in extra:  # the artifact's order; the table inserts parent-before-child, a different one
        if tid in by_id and tid not in seen:
            ordered.append(by_id[tid])
            seen.add(tid)
    ordered += [r for r in rows if r["id"] not in seen]
    obj = dict(doc)
    obj.update({
        "id": row["id"], "plan": plan, "branch": row["branch"], "status": row["status"] or "active",
        "stage": _stage_of(row), "next": row["next"], "started": row["started"], "ended": row["ended"],
        "tasks": [_task_obj(r, extra.get(r["id"]) or {}) for r in ordered],
    })
    obj.setdefault("feature", None)
    obj.setdefault("waiting", None)
    obj.setdefault("recent", [])
    obj.setdefault("plan_id", None)
    return obj


def _store_path(home, project=None, data_dir=None):
    """The store a READER opens for this project (L5). mkpaths.project_folder's rule: a migrated
    project's store is the C2 `<home>/projects/<slug>/mk.db` its root row points at, a
    pre-migration project's is `<data>/mk.db`. The other readers here keep project_dir's
    --data-literal rule on purpose (mk-migrate's rollback addresses the legacy store through
    exactly that form); a pane holding a data dir must follow the switch instead, or it would
    read the pre-switch copy that is still sitting in the data dir. Never creates anything."""
    folder = (_mkpaths().project_folder(data_dir, home_arg=home) if data_dir
              else project_dir(home, project))
    return os.path.join(folder, "mk.db")


def _store_open(home, project, data_dir):
    """(con, store) for a READER of this project's store (L5). Never creates anything.

    `store` is the marker the readers report: False when the store file does not exist (a
    pre-migration project), True when it does. A missing store answers with exit 0 and an
    empty result, which a TypeScript reader cannot tell from a real store that holds no run —
    so without this marker the dashboard and wake would treat "no store" as "no active run"
    and never fall back to the file views."""
    path = _store_path(home, project, data_dir)
    con = _open_read(path, SCHEMA)
    return con, con is not None


def run_snapshot(home, project=None, run=None, data_dir=None):
    """The runs list plus one run's document — what run.json and runs/index.json held (L5).

    Read-only like plans(): mode=ro, never migrates, never writes. The reply carries `store`:
    False when the store is missing (an empty snapshot, no file and no directory created), True
    when it exists — including when it holds no run. A named run that is not there is a Fail
    (exit 2), the same error shape as the other subcommands. Without --run the run is the
    newest active one (runs.status='active', newest `started` first — the pane's own active-run
    order), or null when no run is active.
    """
    con, store = _store_open(home, project, data_dir)
    if con is None:
        if run:
            raise Fail(f"no run {run!r}: no store at {_store_path(home, project, data_dir)}")
        return {"ok": True, "store": False, "runs": [], "active": None, "run": None, "waits": []}
    try:
        runs, docs = [], {}
        for r in con.execute("SELECT id, plan, status, started, ended FROM runs"
                             " ORDER BY started, rowid"):
            doc = _run_doc(con, r["id"])
            docs[r["id"]] = doc
            runs.append({"id": r["id"], "feature": doc.get("feature"), "status": r["status"],
                         "started": r["started"], "ended": r["ended"], "plan": r["plan"]})
        active = next((r["id"] for r in reversed(runs) if r["status"] == "active"), None)
        want = run or active
        if run and not any(r["id"] == run for r in runs):
            raise Fail(f"no run {run!r} in {_store_path(home, project, data_dir)}")
        snapshot = {"ok": True, "store": store, "runs": runs, "active": active,
                    "run": None, "waits": []}
        if want is None:
            return snapshot
        row = con.execute("SELECT * FROM runs WHERE id=?", (want,)).fetchone()
        snapshot["run"] = _run_obj(con, row, docs[want])
        snapshot["waits"] = [dict(w) for w in con.execute(
            "SELECT id, run, question, opened, cleared FROM waits WHERE run=? ORDER BY id", (want,))]
        return snapshot
    finally:
        con.close()


def _cli(argv):
    # The common options are accepted before or after the subcommand. They are pulled out first,
    # so a subcommand never sees them and the two positions cannot disagree.
    common = Parser(prog="mkdb.py", add_help=False)
    common.add_argument("--data", help="project data dir (overrides --project)")
    common.add_argument("--home", help="MK home (default $MK_HOME or $MATRIXKODER_HOME or ~/.matrixkoder)")
    common.add_argument("--project", help="project slug")
    known, rest = common.parse_known_args(argv)
    p = Parser(prog="mkdb.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    tl = sub.add_parser("timeline")
    tl.add_argument("--run", required=True)
    tl.add_argument("--task", required=True)
    tl.add_argument("--json", action="store_true")
    cd = sub.add_parser("cards")
    cd.add_argument("--json", action="store_true")
    pj = sub.add_parser("projects")
    pj.add_argument("--json", action="store_true")
    ov = sub.add_parser("overview")
    ov.add_argument("--json", action="store_true")
    pl = sub.add_parser("plans")
    pl.add_argument("id", nargs="?", help="one plan id; omit for every plan")
    pl.add_argument("--json", action="store_true")
    rs = sub.add_parser("run-snapshot")
    rs.add_argument("--run", help="one run id; omit for the newest active run")
    rs.add_argument("--json", action="store_true")
    ev = sub.add_parser("events")
    ev.add_argument("--run", required=True)
    ev.add_argument("--task")
    ev.add_argument("--chan", default="state", help="channel; '' reads every channel")
    ev.add_argument("--since", type=int, help="only rows past this events.id (the wake cursor)")
    ev.add_argument("--json", action="store_true")
    im = sub.add_parser("import-history")
    im.add_argument("--run", required=True)
    im.add_argument("--json-file", required=True)
    args = p.parse_args(rest)
    for name in ("data", "home", "project"):
        setattr(args, name, getattr(known, name))
    home = resolve_home(args.home)
    if args.data:  # C11: refuse a synced or networked --data dir before any directory is created
        project_dir(home, data_dir=args.data)
    if args.cmd == "timeline":
        if not args.project:
            raise Fail("timeline needs --project")
        return {"ok": True, "events": timeline(home, args.project, args.run, args.task, args.data)}
    if args.cmd == "cards":
        return {"ok": True, "cards": cards(home, args.project)}
    if args.cmd == "projects":
        return {"ok": True, "projects": projects(home)}
    if args.cmd == "overview":  # the root store only: no --project, no --data, nothing opened
        return overview(home)
    if args.cmd == "plans":
        if not args.project and not args.data:
            raise Fail("plans needs --project or --data")
        return plans(home, args.project, args.data, args.id)
    if args.cmd == "run-snapshot":
        if not args.project and not args.data:
            raise Fail("run-snapshot needs --project or --data")
        return run_snapshot(home, args.project, args.run, args.data)
    if args.cmd == "events":
        if not args.project and not args.data:
            raise Fail("events needs --project or --data")
        return {"ok": True,
                "store": _store_present(home, args.project, args.data),
                "events": events(home, args.project, args.run, args.data, args.task,
                                 args.chan, args.since)}
    if args.cmd == "import-history":
        if not args.project:
            raise Fail("import-history needs --project")
        try:
            with open(args.json_file) as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise Fail(f"bad json file {args.json_file}: {e}")
        if not isinstance(doc, dict):
            raise Fail(f"bad json file {args.json_file}: expected an object")
        counts = import_history(home, args.project, args.run, run_row=doc.get("run_row"),
                                tasks=doc.get("tasks") or (), events=doc.get("events") or (),
                                artifacts=doc.get("artifacts") or (), data_dir=args.data)
        return {"ok": True, **counts}
    raise Fail("usage: mkdb.py timeline|cards|projects|overview|plans|run-snapshot|events"
               "|import-history --json  (or --self-test)")


def main(argv):
    out = _cli(argv)
    print(json.dumps(out))
    return 0


# --- self-test ----------------------------------------------------------------

V3_SQL = "CREATE TABLE IF NOT EXISTS mkdb_v3_probe(id INTEGER PRIMARY KEY);"  # test migration past the shipped steps, not shipped


def _check(cond, msg):
    if not cond:
        raise SystemExit(f"SELF-TEST FAIL: {msg}")


def _pragmas(con):
    return (con.execute("PRAGMA journal_mode").fetchone()[0],
            con.execute("PRAGMA busy_timeout").fetchone()[0],
            con.execute("PRAGMA foreign_keys").fetchone()[0])


def _flip_worker(home, data, slot, n):
    """Alternate one shared task between building and blocked."""
    os.environ.pop("MKDB_FAIL_BEFORE_ROOT", None)
    os.environ.pop("MKDB_FAIL_IN_TXN", None)
    for i in range(n):
        to = "blocked" if (i + slot) % 2 else "building"
        transition(home, "flip", "r", "shared", to, note=f"{slot}:{i}", sync_root=True, data_dir=data)


def _worker(home, data, slot, n, bad, sync=False, project="probe"):
    """One of 8 writer processes, each writing its own task row."""
    os.environ.pop("MKDB_FAIL_BEFORE_ROOT", None)
    os.environ.pop("MKDB_FAIL_IN_TXN", None)
    try:
        for i in range(n):
            if bad and i == n - 1:
                os.environ["MKDB_FAIL_IN_TXN"] = "1"
            transition(home, project, "r", f"w{slot}", "building", note=str(i),
                       sync_root=sync, data_dir=data)
    except Exception:
        if not bad:
            raise


def _cycle_worker(home, data, slot):
    """Each process adds one side of a mutual dependency. At most one side may commit."""
    con = open_project(home, data_dir=data)
    try:
        me, other = ("left", "right") if slot % 2 == 0 else ("right", "left")
        try:
            add_task(con, "r", f"{me}{slot}", "task", deps=f"{other}{slot ^ 1}")
        except Fail:
            return
    finally:
        con.close()


def _selftest_guard(home):
    """C16: under MK_SELFTEST=1 a write open outside the temp dir raises; ro reads work."""
    _check(os.environ.get("MK_SELFTEST") == "1", "guard case ran without MK_SELFTEST=1")
    real = _real_home()
    real_db = os.path.join(real, "mk.db")
    # the real resolved home path, not a made-up one: with both vars unset the
    # resolver must land on ~/.matrixkoder
    kept = {k: os.environ.get(k) for k in ("MK_HOME", "MATRIXKODER_HOME")}
    try:
        os.environ.pop("MK_HOME", None)
        os.environ.pop("MATRIXKODER_HOME", None)
        _check(resolve_home(None) == os.path.abspath(os.path.expanduser("~/.matrixkoder")),
               f"unset env did not resolve to the real home: {resolve_home(None)}")
    finally:
        for k, v in kept.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    sibling_data = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".matrixkoder")
    for label, fn in (
            ("root", lambda: open_root(real)),
            ("project-by-slug", lambda: open_project(real, "guard-probe")),
            ("project-by-data", lambda: open_project(home, data_dir=sibling_data))):
        try:
            con = fn()
        except Fail as e:
            _check("MK_SELFTEST=1" in str(e) and ("real" in str(e) or "temp dir" in str(e)),
                   f"guard error names neither the path nor the rule: {e}")
        else:
            con.close()
            raise SystemExit(f"SELF-TEST FAIL: the guard allowed a write open of {label}")
    # a read-only open of the same real path stays allowed
    con = _connect_ro(real_db)
    try:
        _check(con.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"] >= 0, "ro read failed")
    finally:
        con.close()
    # a write open under the temp dir works
    con = open_project(home, "guard-ok")
    try:
        _check(con.execute("SELECT 1").fetchone()[0] == 1, "temp write failed")
    finally:
        con.close()


def selftest_guard_probe(home):
    """Shared C16 probe for every changed script's --self-test (R1.2).

    Under MK_SELFTEST=1 a write open of the real home or of a real repo path
    raises; a mode=ro open of the real root works; a write open under the temp
    home works. Call it from a script's self_test with that suite's temp home.
    """
    _selftest_guard(home)


def _home_env_parity(tmp):
    """R1.1: with only one of the two vars set, mkdb and mk-state resolve the same temp home."""
    state_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mk-state.py")
    probe = ("import importlib.util, sys; "
             "spec = importlib.util.spec_from_file_location('mkdb_probe', sys.argv[1]); "
             "mkdb = importlib.util.module_from_spec(spec); spec.loader.exec_module(mkdb); "
             "spec2 = importlib.util.spec_from_file_location('mk_state_probe', sys.argv[2]); "
             "st = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(st); "
             "print(mkdb.resolve_home(None)); print(st.home())")
    for key in ("MK_HOME", "MATRIXKODER_HOME"):
        want = os.path.abspath(os.path.join(tmp, "parity-%s" % key))
        os.makedirs(want, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in ("MK_HOME", "MATRIXKODER_HOME")}
        env[key] = want
        env["MK_SELFTEST"] = "1"
        proc = subprocess.run([sys.executable, "-c", probe, os.path.abspath(__file__), state_path],
                              capture_output=True, text=True, env=env)
        _check(proc.returncode == 0, "%s-only probe failed: rc=%s %s"
               % (key, proc.returncode, proc.stderr[-500:]))
        lines = proc.stdout.strip().splitlines()
        _check(len(lines) == 2 and lines[0] == lines[1] == want,
               "%s-only homes differ: %s want %s" % (key, lines, want))


def self_test():
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home")
        saved = {k: os.environ.get(k) for k in ("MK_HOME", "MATRIXKODER_HOME", "MK_SELFTEST")}
        os.environ["MK_HOME"] = home
        os.environ["MATRIXKODER_HOME"] = home
        os.environ["MK_SELFTEST"] = "1"
        _stores(home)
        _guards(tmp, home)
        _migrations(home)
        _writers(home)
        _sync_monotonic(home)
        _attention(home)
        _flip(home)
        _cycle_race(home)
        _tree(home)
        _reads(home)
        _crashes(home)
        _import(home)
        _import_atomic(home)
        _import_dup_artifact(home)
        _import_cli(home)
        _import_reupdate(home)
        _import_cycle(home)
        _import_race(home)
        _import_keep_cols(home)
        _live_writers(home)
        _v1_upgrade(home)
        _lane_slots_v3(home)
        _plans_cols_v4(home)
        _extra_json_v5(home)
        _plans_query(home)
        _plans_detail(home)
        _card_verdict(home)
        _summary_transitions(home)
        _summary_regress(home)
        _summary_import(home)
        _summary_kill(home)
        _summary_idempotent(home)
        _summary_two_projects(home)
        _reads_create_nothing(home)
        _projects_read(home)
        _run_snapshot(home)
        _run_events(home)
        _run_snapshot_migrated(home)
        _run_snapshot_no_store(home)
        _run_snapshot_zero_runs(home)
        _task_type_default(home)
        _root_v2_migration(home, tmp)
        _plan_summary_transitions(home)
        _plan_summary_atomic(home)
        _overview_read(home)
        _overview_production_home(home, tmp)
        _single_writer()
        _ready(home)
        _selftest_guard(home)
        _home_env_parity(tmp)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print(json.dumps({"ok": True, "self_test": "pass"}))
    return 0


def _stores(home):
    root, proj = open_root(home), open_project(home, "demo")
    try:
        _check(_pragmas(root) == ("wal", 5000, 1), f"root pragmas {_pragmas(root)}")
        _check(_pragmas(proj) == ("wal", 5000, 1), f"project pragmas {_pragmas(proj)}")
        for table in ("projects", "cards", "project_summary", "sync", "schema_version"):
            _check(root.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone(), table)
        for table in ("plans", "runs", "tasks", "events", "artifacts", "acks", "lane_slots",
                      "waits", "change_log", "schema_version"):
            _check(proj.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone(), table)
    finally:
        root.close()
        proj.close()
    again = open_project(home, "demo")  # re-open is a no-op: still one version row
    try:
        _check(again.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == SCHEMA, "re-open")
        slot_cols = [r["name"] for r in again.execute("PRAGMA table_info(lane_slots)")]
        for name in LANE_SLOT_COLS:
            _check(name in slot_cols, f"fresh store missing {name}")
    finally:
        again.close()
    via_data = open_project(home, data_dir=os.path.join(home, "explicit"))
    via_data.close()
    _check(os.path.exists(os.path.join(home, "explicit", "mk.db")), "--data store")


def _guards(tmp, home):
    before = set(os.listdir(tmp))
    for slug in ("../x", "A", "", "a" * 42):
        try:
            project_dir(home, slug)
        except Fail:
            pass
        else:
            raise SystemExit(f"SELF-TEST FAIL: slug {slug!r} was accepted")
    _check(set(os.listdir(tmp)) == before, "a refused slug created a path")
    unsafe = (os.path.join(tmp, "CloudStorage", "iCloud"),
              os.path.join(tmp, "Mobile Documents", "x"),
              os.path.join(tmp, "Library", "CloudStorage", "iCloud", "mk"))
    for bad in unsafe:
        try:
            open_project(bad, slug="proj")
        except Fail:
            pass
        else:
            raise SystemExit(f"SELF-TEST FAIL: open_project accepted {bad!r}")
        _check(not os.path.exists(os.path.join(bad, "projects")), f"open_project created {bad}")
        try:
            open_project(home, data_dir=bad)
        except Fail:
            pass
        else:
            raise SystemExit(f"SELF-TEST FAIL: --data route accepted {bad!r}")
        _check(not os.path.exists(bad), f"--data created {bad}")
    # the CLI takes the same route and must exit non-zero without creating the dir
    cli_bad = os.path.join(tmp, "Mobile Documents", "cli")
    rc = subprocess.call([sys.executable, os.path.abspath(__file__), "--data", cli_bad,
                          "--home", home, "cards", "--json"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _check(rc != 0, f"CLI --data exited {rc}")
    _check(not os.path.exists(cli_bad), "CLI --data created a directory")


def _sync_monotonic(home):
    """Finding 1: a child rollup must not rewind sync.last_change_no or a card's change_no."""
    data = os.path.join(home, "syncm")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "phase", "phase")
        add_task(con, "r", "t1", "task", parent="phase")
    finally:
        con.close()
    transition(home, "syncm", "r", "t1", "done", sync_root=True, data_dir=data)
    root = open_root(home)
    try:
        last = root.execute("SELECT last_change_no FROM sync WHERE project='syncm'").fetchone()[0]
        _check(last == 2, f"sync.last_change_no rewound to {last}")
        nums = {r["task"]: r["change_no"] for r in root.execute(
            "SELECT task, change_no FROM cards WHERE project='syncm'")}
        _check(nums.get("t1") == 1 and nums.get("phase") == 2, f"card change_no {nums}")
    finally:
        root.close()
    # a later upsert carrying a lower change_no must not lower the watermark or the card
    con = open_project(home, data_dir=data)
    try:
        row = con.execute("SELECT parent, type, title, route_json, lane FROM tasks WHERE id='t1'").fetchone()
    finally:
        con.close()
    _upsert_card(home, "syncm", "r", "t1", row, "done", "Done", 0, 0, 1, data)
    root = open_root(home)
    try:
        last = root.execute("SELECT last_change_no FROM sync WHERE project='syncm'").fetchone()[0]
        card = root.execute("SELECT change_no FROM cards WHERE project='syncm' AND task='t1'").fetchone()[0]
        _check(last == 2 and card == 1, f"lower change_no moved sync={last} card={card}")
    finally:
        root.close()
    # 8 concurrent sync_root writers: the watermark equals MAX(change_log.change_no)
    wdata = os.path.join(home, "syncw")
    con = open_project(home, data_dir=wdata)
    try:
        for i in range(8):
            add_task(con, "r", f"w{i}", "task")
    finally:
        con.close()
    procs = []
    for i in range(8):
        proc = multiprocessing.get_context("fork").Process(
            target=_worker, args=(home, wdata, i, 5, False, True, "syncw"))
        proc.start()
        procs.append(proc)
    for proc in procs:
        proc.join(60)
        _check(proc.exitcode == 0, f"sync writer exit {proc.exitcode}")
    con = open_project(home, data_dir=wdata)
    try:
        top = con.execute("SELECT MAX(change_no) c FROM change_log").fetchone()["c"]
    finally:
        con.close()
    root = open_root(home)
    try:
        last = root.execute("SELECT last_change_no FROM sync WHERE project='syncw'").fetchone()[0]
        _check(last == top, f"sync.last_change_no {last} != MAX(change_log) {top}")
    finally:
        root.close()


def _attention(home):
    """Finding 4: blocked keeps the previous column; leaving it clears the flag. Through transition()."""
    data = os.path.join(home, "attn")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "phase", "phase")
        add_task(con, "r", "t", "task", parent="phase")
        add_task(con, "r", "fresh", "task")
    finally:
        con.close()
    transition(home, "attn", "r", "t", "building", sync_root=True, data_dir=data)
    transition(home, "attn", "r", "t", "blocked", sync_root=True, data_dir=data)
    got = {c["task"]: c for c in cards(home, "attn")}
    _check(got["t"]["col"] == "Building" and got["t"]["attention"] == 1, f"blocked card {got.get('t')}")
    _check(got["phase"]["status"] == "blocked" and got["phase"]["col"] == "Building"
           and got["phase"]["attention"] == 1, f"parent unflagged: {got.get('phase')}")
    con = open_project(home, data_dir=data)
    try:
        prow = con.execute("SELECT status, prev_status FROM tasks WHERE id='phase'").fetchone()
        _check(prow["status"] == "blocked" and prow["prev_status"] == "building", f"phase row {dict(prow)}")
        before = con.execute("SELECT COUNT(*) c FROM events WHERE task='phase'").fetchone()["c"]
    finally:
        con.close()
    transition(home, "attn", "r", "t", "blocked", note="again", sync_root=True, data_dir=data)
    con = open_project(home, data_dir=data)
    try:
        after = con.execute("SELECT COUNT(*) c FROM events WHERE task='phase'").fetchone()["c"]
        _check(after == before, f"parent re-transitioned: {before} -> {after}")
    finally:
        con.close()
    transition(home, "attn", "r", "t", "building", sync_root=True, data_dir=data)
    got = {c["task"]: c for c in cards(home, "attn")}
    _check(got["t"]["col"] == "Building" and got["t"]["attention"] == 0, f"attention stuck {got.get('t')}")
    _check(got["phase"]["status"] == "building" and got["phase"]["attention"] == 0,
           f"phase did not return: {got.get('phase')}")
    transition(home, "attn", "r", "fresh", "blocked", sync_root=True, data_dir=data)
    fresh = {c["task"]: c for c in cards(home, "attn")}["fresh"]
    _check(fresh["col"] == "Backlog" and fresh["attention"] == 1, f"no-history attention {fresh}")
    _check(column_of("blocked") == ("Backlog", 1), "column_of(blocked) without prev")
    _check(column_of("blocked", "building") == ("Building", 1), "column_of keeps prev column")
    # two children: the parent stays flagged until the blocked one clears
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "t2", "task", parent="phase")
    finally:
        con.close()
    transition(home, "attn", "r", "t", "blocked", sync_root=True, data_dir=data)
    transition(home, "attn", "r", "t2", "building", sync_root=True, data_dir=data)
    got = {c["task"]: c for c in cards(home, "attn")}
    _check(got["phase"]["status"] == "blocked" and got["phase"]["attention"] == 1,
           f"sibling cleared the flag: {got.get('phase')}")
    transition(home, "attn", "r", "t", "building", sync_root=True, data_dir=data)
    got = {c["task"]: c for c in cards(home, "attn")}
    _check(got["phase"]["status"] == "building" and got["phase"]["attention"] == 0,
           f"flag stuck after both clear: {got.get('phase')}")
    # three levels: subtask -> task -> phase
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "sub", "subtask", parent="t")
    finally:
        con.close()
    transition(home, "attn", "r", "sub", "building", sync_root=True, data_dir=data)
    transition(home, "attn", "r", "sub", "blocked", sync_root=True, data_dir=data)
    got = {c["task"]: c for c in cards(home, "attn")}
    _check(got["t"]["status"] == "blocked" and got["t"]["attention"] == 1, f"task not flagged {got.get('t')}")
    _check(got["phase"]["status"] == "blocked" and got["phase"]["col"] == "Building"
           and got["phase"]["attention"] == 1, f"phase not flagged {got.get('phase')}")
    # catch_up replays the attention column onto a fresh root, and leaves a plain task unchanged
    root = open_root(home)
    try:
        root.execute("DELETE FROM cards WHERE project='attn'")
        root.execute("DELETE FROM sync WHERE project='attn'")
        root.commit()
    finally:
        root.close()
    caught = catch_up(home, "attn", data)
    _check(caught > 0, "catch_up replayed nothing")
    got = {c["task"]: c for c in cards(home, "attn")}
    _check(got["sub"]["col"] == "Building" and got["sub"]["attention"] == 1, f"replayed sub {got.get('sub')}")
    _check(got["phase"]["col"] == "Building" and got["phase"]["attention"] == 1,
           f"replayed phase {got.get('phase')}")
    plain = dict(got["t2"])
    root = open_root(home)
    try:
        root.execute("DELETE FROM cards WHERE project='attn' AND task='t2'")
        root.execute("UPDATE sync SET last_change_no=0 WHERE project='attn'")
        root.commit()
    finally:
        root.close()
    catch_up(home, "attn", data)
    again = {c["task"]: c for c in cards(home, "attn")}["t2"]
    _check(again["status"] == plain["status"] and again["col"] == plain["col"]
           and again["attention"] == plain["attention"], f"plain card changed {plain} -> {again}")


def _flip(home):
    """8 processes alternate one task between building and blocked; column and log stay consistent."""
    data = os.path.join(home, "flip")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "shared", "task")
    finally:
        con.close()
    transition(home, "flip", "r", "shared", "building", sync_root=True, data_dir=data)
    procs = []
    for i in range(8):
        proc = multiprocessing.get_context("fork").Process(target=_flip_worker, args=(home, data, i, 10))
        proc.start()
        procs.append(proc)
    for proc in procs:
        proc.join(120)
        _check(proc.exitcode == 0, f"flip writer exit {proc.exitcode}")
    con = open_project(home, data_dir=data)
    try:
        row = con.execute("SELECT status, prev_status FROM tasks WHERE id='shared'").fetchone()
        nums = [r[0] for r in con.execute("SELECT change_no FROM change_log ORDER BY change_no")]
    finally:
        con.close()
    card = {c["task"]: c for c in cards(home, "flip")}["shared"]
    _check(row["prev_status"] == "building", f"prev_status lost: {dict(row)}")
    _check(card["col"] == "Building", f"column fell to {card['col']} for a task that was building")
    if row["status"] == "blocked":
        _check(card["attention"] == 1, "blocked without attention")
    else:
        _check(row["status"] == "building" and card["attention"] == 0, f"status {row['status']}")
    _check(nums == list(range(1, len(nums) + 1)), "change_no not gap-free")


def _cycle_race(home):
    """Finding 2: a branched cycle is named and returns. Finding 5: concurrent adds stay acyclic."""
    data = os.path.join(home, "cyc")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "A", "task")
        add_task(con, "r", "B", "task", deps="A")
        con.execute("UPDATE tasks SET deps='B,me' WHERE id='A'")
        con.execute("COMMIT")
        try:
            add_task(con, "r", "me", "task", deps="A")
        except Fail as e:
            _check("cycle" in str(e) and "r:me" in str(e), f"cycle not named: {e}")
        else:
            raise SystemExit("SELF-TEST FAIL: branched cycle accepted")
    finally:
        con.close()
    rdata = os.path.join(home, "cycr")
    open_project(home, data_dir=rdata).close()
    procs = []
    for i in range(8):
        proc = multiprocessing.get_context("fork").Process(target=_cycle_worker, args=(home, rdata, i))
        proc.start()
        procs.append(proc)
    for proc in procs:
        proc.join(60)
        _check(proc.exitcode == 0, f"cycle racer exit {proc.exitcode}")
    con = open_project(home, data_dir=rdata)
    try:
        edges = {}
        for row in con.execute("SELECT run, id, deps FROM tasks"):
            edges[(row["run"], row["id"])] = [_dep_target(row["run"], d) for d in _deps_of(row["deps"])]
        for node in list(edges):
            _check(node not in _reaches(node, edges), f"cycle survived: {node}")
    finally:
        con.close()


def _migrations(home):
    """Finding 1: the REAL v2 step, applied without its stamp, must succeed on the next open."""
    v1 = os.path.join(home, "v2crash")
    con = open_project(home, data_dir=v1, version=1, migrations=())
    try:
        add_task(con, "r", "kept", "task", title="keep me")
    finally:
        con.close()
    bare = sqlite3.connect(os.path.join(v1, "mk.db"))
    bare.row_factory = sqlite3.Row
    _v2(bare)  # the crash: the step landed, the schema_version stamp did not
    bare.commit()
    bare.close()
    reopened = open_project(home, data_dir=v1)
    try:
        _check(reopened.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] == SCHEMA,
               "v2 not stamped after the crash")
        _check(reopened.execute("SELECT title FROM tasks WHERE id='kept'").fetchone()["title"] == "keep me",
               "v2 crash lost a row")
        _check(reopened.execute("SELECT 1 FROM sqlite_master WHERE name='art_run_src'").fetchone(),
               "art_run_src missing")
    finally:
        reopened.close()
    again = open_project(home, data_dir=v1)
    third = open_project(home, data_dir=v1)
    try:
        _check(again.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == SCHEMA, "reopen grew")
        _check(third.execute("SELECT title FROM tasks WHERE id='kept'").fetchone()["title"] == "keep me",
               "second reopen lost a row")
    finally:
        again.close()
        third.close()
    data = os.path.join(home, "mig")
    first = open_project(home, data_dir=data, version=SCHEMA + 1, migrations=((SCHEMA + 1, V3_SQL),))
    _check(first.execute("SELECT 1 FROM sqlite_master WHERE name='mkdb_v3_probe'").fetchone(), "v2")
    first.close()
    second = open_project(home, data_dir=data, version=SCHEMA + 1, migrations=((SCHEMA + 1, V3_SQL),))
    _check(second.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == SCHEMA + 1, "idempotent")
    second.close()
    # Finding 8: a crash between executescript and the version stamp re-runs safely.
    con = sqlite3.connect(os.path.join(data, "mk.db"))
    con.execute(f"DELETE FROM schema_version WHERE version={SCHEMA + 1}")
    con.commit()
    con.close()
    third = open_project(home, data_dir=data, version=SCHEMA + 1, migrations=((SCHEMA + 1, V3_SQL),))
    _check(third.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == SCHEMA + 1, "re-stamp")
    _check(third.execute("SELECT COUNT(*) c FROM mkdb_v3_probe").fetchone()["c"] == 0, "probe grew")
    third.close()
    try:
        open_project(home, data_dir=data, version=1)
    except Fail as e:
        _check(e.code == 3 and "newer" in str(e), f"newer-version error: {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: a newer store was opened")


def _writers(home):
    data = os.path.join(home, "writers")
    con = open_project(home, data_dir=data)
    try:
        for i in range(8):
            add_task(con, "r", f"w{i}", "task")  # one row per writer process
    finally:
        con.close()
    n, procs = 25, []
    for i in range(8):
        proc = multiprocessing.get_context("fork").Process(target=_worker, args=(home, data, i, n, False))
        proc.start()
        procs.append(proc)
    for proc in procs:
        proc.join(60)
        _check(proc.exitcode == 0, f"writer {proc.pid} exit {proc.exitcode}")
    con = open_project(home, data_dir=data)
    try:
        count = con.execute("SELECT COUNT(*) c FROM change_log").fetchone()["c"]
        _check(count == 8 * n, f"rows {count} != {8 * n}")
        nums = [r[0] for r in con.execute("SELECT change_no FROM change_log ORDER BY change_no")]
        _check(nums == list(range(1, 8 * n + 1)), "change_no not gap-free")
    finally:
        con.close()


def _tree(home):
    data = os.path.join(home, "tree")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "phase", "phase")
        add_task(con, "r", "task", "task", parent="phase", deps="phase")
        add_task(con, "other", "ext", "task")
        add_task(con, "r", "sub", "subtask", parent="task", deps="task,other:ext")
        for who, deps, blame in (("bad", "nope", "missing"), ("sub", "sub", "self-dep"),
                                 ("phase", "task", "cycle")):
            try:
                add_task(con, "r", who, "task", parent="phase", deps=deps)
            except Fail as e:
                _check(blame in str(e), f"expected {blame} in {e}")
                if blame == "cycle":
                    _check("r:phase -> r:task -> r:phase" in str(e), f"cycle not named: {e}")
            else:
                raise SystemExit(f"SELF-TEST FAIL: deps {deps!r} accepted")
        try:
            add_task(con, "r", "routed", "task", route_json='{"agent":"x"}')
        except Fail:
            pass
        else:
            raise SystemExit("SELF-TEST FAIL: bad route_json accepted")
        add_task(con, "r", "routed", "task", route_json='{"agent":"opencode","model":"auto","effort":"low"}')
    finally:
        con.close()
    transition(home, "tree", "r", "sub", "done", data_dir=data)
    con = open_project(home, data_dir=data)
    try:
        got = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM tasks WHERE run='r'")}
        _check(got["sub"] == "done" and got["task"] == "done" and got["phase"] == "done", f"rollup {got}")
    finally:
        con.close()


def _reads(home):
    data = os.path.join(home, "reads")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "t", "task")
    finally:
        con.close()
    transition(home, "reads", "r", "t", "building", note="one", data_dir=data)
    transition(home, "reads", "r", "t", "done", note="two", data_dir=data)
    ids = [e["id"] for e in timeline(home, "reads", "r", "t", data)]
    _check(ids == sorted(ids) and len(ids) == 2, f"timeline {ids}")
    _check(cards(home, "reads") == [], "cards must be empty when sync_root is off")
    transition(home, "reads", "r", "t", "done", note="synced", sync_root=True, data_dir=data)
    got = cards(home, "reads")
    _check(len(got) == 1 and got[0]["change_no"] == 3, f"cards {got}")


def _crashes(home):
    data = os.path.join(home, "crash")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "t", "task")
    finally:
        con.close()
    os.environ["MKDB_FAIL_BEFORE_ROOT"] = "1"
    try:
        try:
            transition(home, "crash", "r", "t", "building", sync_root=True, data_dir=data)
        except Fail:
            pass
        else:
            raise SystemExit("SELF-TEST FAIL: the injected root failure did not fire")
        con = open_project(home, data_dir=data)
        try:
            _check(con.execute("SELECT status FROM tasks WHERE id='t'").fetchone()["status"] == "building",
                   "project change lost")
        finally:
            con.close()
        _check(cards(home, "crash") == [], "root card written despite the failure")
        os.environ.pop("MKDB_FAIL_BEFORE_ROOT", None)
        caught = catch_up(home, "crash", data)
        _check(caught == 1 and len(cards(home, "crash")) == 1, "catch-up did not repair the card")
        os.environ["MKDB_FAIL_IN_TXN"] = "1"
        try:
            transition(home, "crash", "r", "t", "done", data_dir=data)
        except Fail:
            pass
        else:
            raise SystemExit("SELF-TEST FAIL: the injected txn failure did not fire")
        con = open_project(home, data_dir=data)
        try:
            _check(con.execute("SELECT status FROM tasks WHERE id='t'").fetchone()["status"] == "building",
                   "a failed transaction left a change")
            _check(con.execute("SELECT COUNT(*) c FROM change_log").fetchone()["c"] == 1, "change_log grew")
        finally:
            con.close()
    finally:
        os.environ.pop("MKDB_FAIL_BEFORE_ROOT", None)
        os.environ.pop("MKDB_FAIL_IN_TXN", None)



def _checksum(con):
    """A stable digest of the rows an import can touch, so idempotence is more than a count."""
    h = hashlib.sha256()
    for table in ("runs", "tasks", "events", "artifacts", "change_log", "event_src"):
        for row in con.execute(f"SELECT * FROM {table}"):
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def _sample():
    """One small historical run: parent + child, an attention task, past timestamps, two artifacts."""
    run_row = {"id": "hist", "plan": "p", "plan_version": 1, "branch": "b", "status": "done",
               "stage": None, "next": None, "started": "2020-01-01T00:00:00+00:00",
               "ended": "2020-01-02T00:00:00+00:00"}
    tasks = [
        {"id": "child", "parent": "parent", "type": "task", "title": "child", "deps": "later",
         "status": "done", "round": 2, "verdict": "pass", "updated": "2020-01-01T01:00:00+00:00"},
        {"id": "parent", "type": "phase", "title": "parent", "status": "done",
         "updated": "2020-01-01T02:00:00+00:00"},
        {"id": "later", "type": "task", "title": "later", "status": "blocked", "round": 4,
         "verdict": None, "updated": "2020-01-01T03:00:00+00:00"},
    ]
    events = [
        {"src": "e1", "task": "child", "t": "2020-01-01T00:00:01+00:00", "chan": "state",
         "kind": "transition", "text": "started"},
        {"src": "e2", "task": "child", "t": "2020-01-01T00:00:02+00:00", "chan": "state",
         "kind": "note", "text": "a note"},
        {"src": "e3", "task": "child", "t": "2020-01-01T00:00:03+00:00", "chan": "state",
         "kind": "verdict", "text": "pass"},
        {"src": "e4", "task": "child", "t": "2020-01-01T00:00:04+00:00", "chan": "state",
         "kind": "decision", "text": "ship it"},
        {"src": "e5", "task": "child", "t": "2020-01-01T00:00:05+00:00", "chan": "bus",
         "kind": "ask", "text": "question?"},
    ]
    artifacts = [
        {"src": "a1", "task": "child", "round": 2, "kind": "report", "rev": 1,
         "text": "caf\u00e9 \u2014 no newline", "created": "2020-01-01T00:00:06+00:00"},
        {"src": "a2", "task": "child", "round": 2, "kind": "brief", "rev": 1,
         "text": "plain\n", "created": "2020-01-01T00:00:07+00:00"},
    ]
    return run_row, tasks, events, artifacts


def _import(home):
    """Round trip plus idempotence: original t/chan/kind, byte-identical text, zero rows the second time."""
    data = os.path.join(home, "imp")
    run_row, tasks, events, artifacts = _sample()
    counts = import_history(home, "imp", "hist", run_row=run_row, tasks=tasks, events=events,
                            artifacts=artifacts, data_dir=data)
    _check(counts == {"runs": 1, "tasks": 3, "events": 5, "artifacts": 2}, f"counts {counts}")
    got = timeline(home, "imp", "hist", "child", data)
    _check([e["kind"] for e in got] == ["transition", "note", "verdict", "decision", "ask"], f"kinds {got}")
    _check([e["t"] for e in got] == [e["t"] for e in events], "timestamps rewritten")
    _check([e["chan"] for e in got] == ["state", "state", "state", "state", "bus"], f"chans {got}")
    _check([e["text"] for e in got] == [e["text"] for e in events], "text changed")
    _check("now()" not in "".join(e["t"] for e in got), "a timestamp looks like now()")
    con = open_project(home, data_dir=data)
    try:
        rows = {r["id"]: r for r in con.execute("SELECT * FROM tasks")}
        _check(rows["later"]["status"] == "blocked" and rows["later"]["prev_status"] is None,
               f"attention row {dict(rows['later'])}")
        _check(column_of(rows["later"]["status"], rows["later"]["prev_status"]) == ("Backlog", 1),
               "attention column")
        _check(rows["child"]["status"] == "done" and rows["child"]["round"] == 2
               and rows["child"]["verdict"] == "pass" and rows["child"]["parent"] == "parent",
               f"child {dict(rows['child'])}")
        texts = [r["text"] for r in con.execute("SELECT text FROM artifacts ORDER BY src")]
        _check(hashlib.sha256(texts[0].encode()).hexdigest()
               == hashlib.sha256("caf\u00e9 \u2014 no newline".encode()).hexdigest(), "unicode text")
        _check(texts[1] == "plain\n" and not texts[0].endswith("\n"), f"newline changed {texts!r}")
        _check(con.execute("SELECT COUNT(*) c FROM change_log").fetchone()["c"] == 3, "change_log")
        digest = _checksum(con)
        n_events = con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    finally:
        con.close()
    _check(cards(home, "imp") == [], "import wrote the root store")
    again = import_history(home, "imp", "hist", run_row=run_row, tasks=tasks, events=events,
                           artifacts=artifacts, data_dir=data)
    _check(again == {"runs": 0, "tasks": 0, "events": 0, "artifacts": 0}, f"second import {again}")
    con = open_project(home, data_dir=data)
    try:
        _check(_checksum(con) == digest, "second import changed a row")
        _check(con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == n_events, "events grew")
    finally:
        con.close()


def _import_dup_artifact(home):
    """Finding 2: two artifacts with one src in a batch are refused and nothing is written."""
    data = os.path.join(home, "dupa")
    try:
        import_history(home, "dupa", "hist",
                       tasks=[{"id": "t", "type": "task", "status": "planned"}],
                       artifacts=[{"src": "same", "task": "t", "round": 1, "kind": "report", "rev": 1,
                                   "text": "one", "created": "2020-01-01T00:00:00+00:00"},
                                  {"src": "same", "task": "t", "round": 1, "kind": "report", "rev": 2,
                                   "text": "two", "created": "2020-01-01T00:00:01+00:00"}],
                       data_dir=data)
    except Fail as e:
        _check("duplicate artifact src" in str(e), f"unexpected error {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: a duplicate artifact src was imported")
    con = open_project(home, data_dir=data)
    try:
        for table in ("tasks", "artifacts", "change_log"):
            _check(con.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0,
                   f"{table} written despite the duplicate src")
    finally:
        con.close()


def _import_cli(home):
    """Finding 3: a bad JSON file is a JSON error, and options work after the subcommand."""
    data = os.path.join(home, "cli")
    base = [sys.executable, os.path.abspath(__file__), "--home", home, "--data", data]
    def run(args, payload=None):
        path = None
        if payload is not None:
            path = os.path.join(home, "in.json")
            with open(path, "w") as fh:
                fh.write(payload)
        proc = subprocess.run(base + args + (["--json-file", path] if path else []),
                              capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr
    rc, out, err = run(["import-history", "--project", "cli", "--run", "hist"], payload="[1, 2]")
    _check(rc != 0 and "Traceback" not in err and '"ok": false' in out, f"non-object: rc={rc} out={out!r} err={err!r}")
    rc, out, err = run(["import-history", "--project", "cli", "--run", "hist"], payload="{")
    _check(rc != 0 and "Traceback" not in err and '"ok": false' in out, f"bad json: rc={rc} out={out!r} err={err!r}")
    rc, out, err = run(["import-history", "--project", "cli", "--run", "hist", "--json-file",
                        os.path.join(home, "missing.json")])
    _check(rc != 0 and "Traceback" not in err and '"ok": false' in out, f"missing: rc={rc} out={out!r} err={err!r}")
    good = json.dumps({"run_row": {"id": "hist", "status": "done"},
                       "tasks": [{"id": "t", "type": "task", "status": "done"}]})
    rc, out, err = run(["import-history", "--run", "hist", "--project", "cli"], payload=good)
    _check(rc == 0 and '"tasks": 1' in out, f"options after subcommand: rc={rc} out={out!r} err={err!r}")


def _import_reupdate(home):
    """Finding 6: a re-import logs a change only when a column changed."""
    data = os.path.join(home, "reup")
    task = {"id": "t", "type": "task", "status": "planned", "title": "one"}
    import_history(home, "reup", "hist", tasks=[task], data_dir=data)
    con = open_project(home, data_dir=data)
    try:
        before = con.execute("SELECT COUNT(*) c FROM change_log").fetchone()["c"]
    finally:
        con.close()
    same = import_history(home, "reup", "hist", tasks=[dict(task)], data_dir=data)
    _check(same["tasks"] == 0, f"identical re-import counted a task: {same}")
    con = open_project(home, data_dir=data)
    try:
        _check(con.execute("SELECT COUNT(*) c FROM change_log").fetchone()["c"] == before,
               "identical re-import wrote change_log")
    finally:
        con.close()
    changed = dict(task, title="two")
    import_history(home, "reup", "hist", tasks=[changed], data_dir=data)
    con = open_project(home, data_dir=data)
    try:
        after = con.execute("SELECT COUNT(*) c FROM change_log").fetchone()["c"]
        _check(after == before + 1, f"changed re-import logged {after - before}")
        _check(con.execute("SELECT title FROM tasks WHERE id='t'").fetchone()["title"] == "two",
               "update did not land")
    finally:
        con.close()


def _import_keep_cols(home):
    """D1.5.2: the mirror never blanks a column the file view carries no value for.

    The approved plan's acceptance/owned/deps/route_json/type survive a re-import that omits
    them (NULL, or "" for the csv columns — what the file view renders an empty list as),
    while a real value always wins and a status change still overwrites.
    """
    data = os.path.join(home, "keepcols")
    full = {"id": "t", "parent": None, "type": "phase", "title": "one", "acceptance": "the outcome",
            "owned": "scripts/a.py", "deps": "other", "parallel": 0,
            "route_json": '{"agent": "opencode", "effort": "", "model": "m"}', "status": "building"}
    # the dep target exists first: a batch is checked parent/dep before its target
    import_history(home, "keepcols", "hist", run_row={"id": "hist", "status": "active"},
                   tasks=[{"id": "other", "type": "task", "title": "other", "status": "done"}],
                   data_dir=data)
    import_history(home, "keepcols", "hist", tasks=[full], data_dir=data)
    bare = {"id": "t", "parent": None, "type": "phase", "title": "one", "acceptance": None,
            "owned": "", "deps": "", "parallel": 0, "route_json": None, "status": "routed"}
    import_history(home, "keepcols", "hist", tasks=[bare], data_dir=data)
    con = open_project(home, data_dir=data)
    try:
        row = con.execute("SELECT * FROM tasks WHERE run='hist' AND id='t'").fetchone()
        _check(row["acceptance"] == "the outcome", f"acceptance blanked: {row['acceptance']!r}")
        _check(row["owned"] == "scripts/a.py", f"owned blanked: {row['owned']!r}")
        _check(row["deps"] == "other", f"deps blanked: {row['deps']!r}")
        # type is always present (the mirror derives it), so it overwrites like any real value
        _check(row["type"] == "phase", f"the mirror's type did not land: {row['type']!r}")
        _check(row["route_json"] == full["route_json"], f"route_json blanked: {row['route_json']!r}")
        _check(row["status"] == "routed", f"status did not overwrite: {row['status']!r}")  # status still does
        # a re-import that omits the kept columns logs no change at all
        n_log = con.execute("SELECT MAX(change_no) c FROM change_log WHERE run='hist' AND task='t'").fetchone()["c"]
        import_history(home, "keepcols", "hist", tasks=[dict(bare)], data_dir=data)
        con = open_project(home, data_dir=data)
        try:
            again = con.execute("SELECT MAX(change_no) c FROM change_log"
                                " WHERE run='hist' AND task='t'").fetchone()["c"]
            _check(again == n_log, f"an omitted-only re-import logged a change: {n_log} -> {again}")
        finally:
            con.close()
        con = open_project(home, data_dir=data)
        try:
            row = con.execute("SELECT * FROM tasks WHERE run='hist' AND id='t'").fetchone()
            _check(row["acceptance"] == "the outcome" and row["owned"] == "scripts/a.py",
                   f"the kept columns did not survive an identical re-import: {row['acceptance']!r}")
        finally:
            con.close()
        # a real value always wins (deps stays as it is — a real one is below)
        fresh = dict(bare, acceptance="new outcome", owned="scripts/b.py", deps="other")
        import_history(home, "keepcols", "hist", tasks=[fresh], data_dir=data)
        con = open_project(home, data_dir=data)
        try:
            row = con.execute("SELECT * FROM tasks WHERE run='hist' AND id='t'").fetchone()
            _check(row["acceptance"] == "new outcome" and row["owned"] == "scripts/b.py",
                   f"a real value did not win: {row['acceptance']!r} {row['owned']!r}")
            _check(row["route_json"] == full["route_json"], "route_json lost after a real acceptance")
            _check(row["type"] == "phase", "type lost after a real acceptance")
            _check(con.execute("SELECT COUNT(*) c FROM tasks WHERE run='hist'").fetchone()["c"] == 2,
                   "the keep-case inserted a second row")
            n_log = con.execute("SELECT MAX(change_no) c FROM change_log"
                                " WHERE run='hist' AND task='t'").fetchone()["c"]
        finally:
            con.close()
        # owned/deps OMITTED (None, not "") keep the stored lists too, and log no change
        omitted = {k: v for k, v in fresh.items() if k not in ("owned", "deps")}
        import_history(home, "keepcols", "hist", tasks=[omitted], data_dir=data)
        con = open_project(home, data_dir=data)
        try:
            row = con.execute("SELECT * FROM tasks WHERE run='hist' AND id='t'").fetchone()
            _check(row["owned"] == "scripts/b.py" and row["deps"] == "other",
                   f"omitted owned/deps blanked: {row['owned']!r} {row['deps']!r}")
            again = con.execute("SELECT MAX(change_no) c FROM change_log"
                                " WHERE run='hist' AND task='t'").fetchone()["c"]
            _check(again == n_log, f"an omitted owned/deps logged a change: {n_log} -> {again}")
        finally:
            con.close()
    finally:
        con.close()


def _import_atomic(home):
    """A bad dep in the middle of the batch rolls the whole call back."""
    data = os.path.join(home, "atom")
    run_row, tasks, events, artifacts = _sample()
    tasks = tasks + [{"id": "bad", "type": "task", "title": "bad", "deps": "nope", "status": "planned"}]
    try:
        import_history(home, "atom", "hist", run_row=run_row, tasks=tasks, events=events,
                       artifacts=artifacts, data_dir=data)
    except Fail as e:
        _check("missing dep" in str(e), f"unexpected error {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: a bad dep was imported")
    con = open_project(home, data_dir=data)
    try:
        for table in ("runs", "tasks", "events", "artifacts", "change_log"):
            _check(con.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0,
                   f"{table} not rolled back")
    finally:
        con.close()


def _import_cycle(home):
    """A cycle inside the batch is refused and named; a dep on a later task in the batch is fine."""
    data = os.path.join(home, "icyc")
    try:
        import_history(home, "icyc", "hist", tasks=[
            {"id": "a", "type": "task", "deps": "b", "status": "planned"},
            {"id": "b", "type": "task", "deps": "a", "status": "planned"}], data_dir=data)
    except Fail as e:
        _check("cycle" in str(e) and "hist:a" in str(e) and "hist:b" in str(e), f"cycle not named: {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: a batch cycle was imported")
    con = open_project(home, data_dir=data)
    try:
        _check(con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 0, "cycle left rows")
    finally:
        con.close()
    # _sample's child depends on `later`, which comes after it in the batch
    run_row, tasks, events, artifacts = _sample()
    counts = import_history(home, "icyc", "hist", run_row=run_row, tasks=tasks, data_dir=data)
    _check(counts["tasks"] == 3, f"forward dep refused: {counts}")


def _import_worker(home, data, slot):
    os.environ.pop("MKDB_FAIL_BEFORE_ROOT", None)
    os.environ.pop("MKDB_FAIL_IN_TXN", None)
    import_history(home, "race", f"r{slot}",
                   run_row={"id": f"r{slot}", "status": "done"},
                   tasks=[{"id": "t", "type": "task", "status": "done"}],
                   events=[{"src": f"s{slot}", "task": "t", "t": "2020-01-01T00:00:00+00:00",
                            "chan": "state", "kind": "note", "text": str(slot)}],
                   data_dir=data)


def _import_race(home):
    """4 processes import different runs into one project store; counts add up, nobody is locked out."""
    data = os.path.join(home, "race")
    open_project(home, data_dir=data).close()
    procs = []
    for i in range(4):
        proc = multiprocessing.get_context("fork").Process(target=_import_worker, args=(home, data, i))
        proc.start()
        procs.append(proc)
    for proc in procs:
        proc.join(60)
        _check(proc.exitcode == 0, f"import racer exit {proc.exitcode}")
    con = open_project(home, data_dir=data)
    try:
        _check(con.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"] == 4, "runs lost")
        _check(con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 4, "tasks lost")
        _check(con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 4, "events lost")
    finally:
        con.close()


def _lane_slots_v3(home):
    """A v2 store with a populated lane_slots row migrates to v3 and keeps the row.

    A crash after the REAL v3 step and before the version stamp reopens cleanly.
    A fresh store already has the columns; opening twice more changes nothing.
    """
    data = os.path.join(home, "slots-v2")
    os.makedirs(data, exist_ok=True)
    # The base CREATE TABLE already has the v3 columns, so a real v2 file is built here:
    # the pre-v3 lane_slots shape, stamped at version 2, with one busy row.
    bare = sqlite3.connect(os.path.join(data, "mk.db"))
    bare.executescript("""
        CREATE TABLE lane_slots(slot TEXT PRIMARY KEY, lane_path TEXT, state TEXT, run TEXT, task TEXT);
        CREATE TABLE schema_version(version INTEGER PRIMARY KEY);
        INSERT INTO schema_version(version) VALUES (1);
        INSERT INTO schema_version(version) VALUES (2);
        INSERT INTO lane_slots(slot, lane_path, state, run, task) VALUES ('1', '/tmp/lane', 'busy', 'r1', 'D5B');
    """)
    bare.commit()
    names = [r[1] for r in bare.execute("PRAGMA table_info(lane_slots)")]
    bare.close()
    _check("workspace" not in names, "v2 fixture already had workspace")
    bare = sqlite3.connect(os.path.join(data, "mk.db"))
    bare.row_factory = sqlite3.Row
    _v3(bare)  # the crash: the step landed, the schema_version stamp did not
    bare.commit()
    bare.close()
    reopened = open_project(home, data_dir=data)
    try:
        _check(reopened.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] == SCHEMA,
               "v3 not stamped after the crash")
        row = reopened.execute("SELECT lane_path, state, run, task, workspace, branch FROM lane_slots WHERE slot='1'").fetchone()
        _check(row["lane_path"] == "/tmp/lane" and row["state"] == "busy" and row["run"] == "r1"
               and row["task"] == "D5B", f"v3 crash lost the slot: {dict(row) if row else None}")
        _check(row["workspace"] is None and row["branch"] is None, "v3 filled old columns")
    finally:
        reopened.close()
    again = open_project(home, data_dir=data)
    third = open_project(home, data_dir=data)
    try:
        _check(again.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == SCHEMA, "v3 reopen grew")
        _check(third.execute("SELECT task FROM lane_slots WHERE slot='1'").fetchone()["task"] == "D5B",
               "second reopen lost the slot")
        names = [r["name"] for r in third.execute("PRAGMA table_info(lane_slots)")]
        for name in LANE_SLOT_COLS:
            _check(names.count(name) == 1, f"{name} missing or duplicated")
    finally:
        again.close()
        third.close()


def _v1_upgrade(home):
    """A store created at v1 is upgraded in place and keeps the rows it already had."""
    data = os.path.join(home, "v1")
    con = open_project(home, data_dir=data, version=1, migrations=())
    try:
        add_task(con, "r", "kept", "task", title="keep me")
        _check("src" not in [r["name"] for r in con.execute("PRAGMA table_info(artifacts)")],
               "v1 store already had src")
    finally:
        con.close()
    upgraded = open_project(home, data_dir=data)
    try:
        _check(upgraded.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] == SCHEMA, "not at the current schema")
        _check("src" in [r["name"] for r in upgraded.execute("PRAGMA table_info(artifacts)")], "src missing")
        row = upgraded.execute("SELECT title FROM tasks WHERE id='kept'").fetchone()
        _check(row["title"] == "keep me", f"row lost: {dict(row) if row else None}")
    finally:
        upgraded.close()


def _seed_plans(home, name):
    """One store for the plans query/detail cases (C5, C8).

    Plan alpha: two runs — r1 older and done (the bare relative plans/ path), r2 newest and
    active (the absolute path form) — plus a same-prefix decoy run that must not match, and
    the newest run carries a phase, tasks in four statuses, a subtask and a parentless row.
    Plan beta: no run, NULL status/created columns (the header fallback) and the full phase
    shape in header_json. plans and runs rows are plain SQL (no writer API for them yet);
    the inserts commit before the first add_task (both take BEGIN IMMEDIATE).
    """
    data = os.path.join(home, name)
    hdr_a = json.dumps({"status": "header-draft", "created": "header-created",
                        "phases": [{"id": "ph1", "wave": 1}, {"id": "ph2", "wave": 2}]})
    hdr_b = json.dumps({
        "status": "draft", "created": "2026-02-02T00:00:00Z", "phases": [
            {"id": "ph1", "title": "Phase one", "wave": 1, "deps": [], "tasks": [
                {"id": "a1", "title": "Task A1", "subtasks": [{"id": "s1", "title": "Sub S1"}]}]},
            {"id": "ph2", "title": "Phase two", "wave": 2, "deps": ["ph1"], "tasks": [
                {"id": "a2", "title": "Task A2", "deps": ["a1"], "subtasks": []}]}]})
    con = open_project(home, data_dir=data)
    try:
        _begin(con)
        con.execute("INSERT INTO plans(id, version, title, header_json, approved_at, status,"
                    " created, design, integration) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("alpha", 3, "Alpha", hdr_a, "2026-01-01T00:00:00Z", "approved",
                     "2026-01-01T00:00:00Z", json.dumps(["d1", "d2"]), "int-branch"))
        con.execute("INSERT INTO plans(id, version, title, header_json) VALUES (?,?,?,?)",
                    ("beta", 2, "Beta", hdr_b))
        con.execute("INSERT INTO runs(id, plan, status, started) VALUES (?,?,?,?)",
                    ("r1", "plans/alpha.md", "done", "2026-01-03T00:00:00Z"))
        con.execute("INSERT INTO runs(id, plan, status, started) VALUES (?,?,?,?)",
                    ("r2", "/repo/.matrixkoder/plans/alpha.md", "active", "2026-01-04T00:00:00Z"))
        con.execute("INSERT INTO runs(id, plan, status, started) VALUES (?,?,?,?)",
                    ("zz", "plans/alpha2.md", "active", "2026-01-09T00:00:00Z"))
        con.execute("COMMIT")
        add_task(con, "r2", "ph1", "phase", title="Phase one")
        add_task(con, "r2", "a1", "task", title="Task A1", parent="ph1", status="planned")
        add_task(con, "r2", "a1s", "subtask", title="Sub S1", parent="a1", status="planned")
        add_task(con, "r2", "a2", "task", title="Task A2", parent="ph1", status="building")
        add_task(con, "r2", "a3", "task", title="Task A3", parent="ph1", status="done")
        add_task(con, "r2", "a4", "task", title="Task A4", parent="ph1", status="blocked")
        add_task(con, "r2", "loner", "task", title="root task row")  # parentless: phase-level
        add_task(con, "r1", "old-ph", "phase")  # an older run must not reach alpha's counts
        add_task(con, "r1", "old-t", "task", parent="old-ph", status="done")
    finally:
        con.close()
    return data


def _plans_cols_v4(home):
    """A v3 store with a pre-v4 plans row migrates: the D1 columns land and the row survives;
    a reopen stamps nothing new; a fresh store reaches the very same plans shape."""
    data = os.path.join(home, "plans-v3")
    os.makedirs(data, exist_ok=True)
    # a real v3 file, built like _lane_slots_v3's fixture: the pre-v4 7-column plans
    # shape, stamped 1..3, with one row
    bare = sqlite3.connect(os.path.join(data, "mk.db"))
    bare.executescript("""
        CREATE TABLE plans(id TEXT PRIMARY KEY, version INTEGER, title TEXT, header_json TEXT,
          body_md TEXT, approved_at TEXT, archived_path TEXT);
        CREATE TABLE schema_version(version INTEGER PRIMARY KEY);
        INSERT INTO schema_version(version) VALUES (1);
        INSERT INTO schema_version(version) VALUES (2);
        INSERT INTO schema_version(version) VALUES (3);
        INSERT INTO plans(id, version, title, header_json) VALUES ('old', 2, 'Old plan', '{}');
    """)
    bare.commit()
    bare.close()
    migrated = open_project(home, data_dir=data)
    try:
        cols = [(r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"])
                for r in migrated.execute("PRAGMA table_info(plans)")]
        names = [c[0] for c in cols]
        for name in PLAN_D1_COLS:
            _check(names.count(name) == 1, f"v4 left {name} missing or duplicated: {names}")
        _check(migrated.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"]
               == SCHEMA, "v4 not stamped")
        row = migrated.execute("SELECT title, header_json, status, created, design, integration"
                               " FROM plans WHERE id='old'").fetchone()
        _check(row["title"] == "Old plan" and row["header_json"] == "{}"
               and all(row[n] is None for n in PLAN_D1_COLS),
               f"v3 row lost: {dict(row) if row else None}")
    finally:
        migrated.close()
    again = open_project(home, data_dir=data)  # a reopen is a no-op: no new stamp, no new column
    try:
        _check(again.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == SCHEMA,
               "v4 reopen stamped again")
        names = [r["name"] for r in again.execute("PRAGMA table_info(plans)")]
        _check(sorted(names) == sorted(c[0] for c in cols), f"reopen changed plans columns: {names}")
    finally:
        again.close()
    fresh = open_project(home, data_dir=os.path.join(home, "plans-fresh"))
    try:
        fresh_cols = [(r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"])
                      for r in fresh.execute("PRAGMA table_info(plans)")]
    finally:
        fresh.close()
    _check(fresh_cols == cols, f"fresh and migrated plans shapes differ: {fresh_cols} vs {cols}")


def _extra_json_v5(home):
    """A v4 store with runs and tasks rows migrates to v5: extra_json lands on both tables and
    every row survives; a reopen stamps nothing new; a fresh store reaches the same shape.
    The grant for L4 (m-5862) asks exactly this case."""
    data = os.path.join(home, "extras-v4")
    os.makedirs(data, exist_ok=True)
    # a real v4 file: the pre-v5 runs/tasks shapes (no extra_json), stamped 1..4, with rows
    bare = sqlite3.connect(os.path.join(data, "mk.db"))
    bare.executescript("""
        CREATE TABLE runs(id TEXT PRIMARY KEY, plan TEXT, plan_version INTEGER, branch TEXT,
          status TEXT, stage TEXT, next TEXT, started TEXT, ended TEXT);
        CREATE TABLE tasks(run TEXT, id TEXT, parent TEXT, type TEXT, title TEXT, acceptance TEXT,
          owned TEXT, deps TEXT, parallel INTEGER, route_json TEXT, status TEXT, prev_status TEXT,
          file_status TEXT, lane TEXT, slot TEXT, branch TEXT, pane TEXT, round INTEGER,
          verdict TEXT, updated TEXT, PRIMARY KEY(run, id));
        CREATE TABLE schema_version(version INTEGER PRIMARY KEY);
        INSERT INTO schema_version(version) VALUES (1);
        INSERT INTO schema_version(version) VALUES (2);
        INSERT INTO schema_version(version) VALUES (3);
        INSERT INTO schema_version(version) VALUES (4);
        INSERT INTO runs(id, plan, status, started) VALUES ('rv', 'plans/x.md', 'active', '2026-01-01');
        INSERT INTO tasks(run, id, parent, type, title, status, round) VALUES
          ('rv', 'T1', NULL, 'task', 'kept', 'building', 2);
    """)
    bare.commit()
    bare.close()
    migrated = open_project(home, data_dir=data)
    try:
        for table in ("runs", "tasks"):
            names = [r["name"] for r in migrated.execute(f"PRAGMA table_info({table})")]
            _check(names.count("extra_json") == 1, f"v5 left {table}.extra_json wrong: {names}")
        _check(migrated.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"]
               == SCHEMA, "v5 not stamped")
        row = migrated.execute("SELECT title, status, round, extra_json FROM tasks WHERE id='T1'").fetchone()
        _check(row["title"] == "kept" and row["status"] == "building" and row["round"] == 2
               and row["extra_json"] is None, f"v4 task row lost: {dict(row) if row else None}")
        rrow = migrated.execute("SELECT plan, status, extra_json FROM runs WHERE id='rv'").fetchone()
        _check(rrow["plan"] == "plans/x.md" and rrow["status"] == "active" and rrow["extra_json"] is None,
               f"v4 run row lost: {dict(rrow) if rrow else None}")
    finally:
        migrated.close()
    again = open_project(home, data_dir=data)  # a reopen is a no-op: no new stamp, no new column
    try:
        _check(again.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == SCHEMA,
               "v5 reopen stamped again")
        for table in ("runs", "tasks"):
            names = [r["name"] for r in again.execute(f"PRAGMA table_info({table})")]
            _check(names.count("extra_json") == 1, f"v5 reopen changed {table}: {names}")
    finally:
        again.close()
    fresh_data = os.path.join(home, "extras-fresh")
    fresh = open_project(home, data_dir=fresh_data)
    try:
        for table in ("runs", "tasks"):
            fresh_names = [r["name"] for r in fresh.execute(f"PRAGMA table_info({table})")]
            ref = open_project(home, data_dir=data)
            try:
                ref_names = [r["name"] for r in ref.execute(f"PRAGMA table_info({table})")]
            finally:
                ref.close()
            _check(fresh_names == ref_names, f"fresh and migrated {table} shapes differ: {fresh_names} vs {ref_names}")
    finally:
        fresh.close()


def _plans_query(home):
    """plans() lists both plans: runs match three plan forms, the newest run feeds counts and
    live_run, root rows are not counted, and a header-only plan falls back and stays empty."""
    data = _seed_plans(home, "plans-q")
    got = plans(home, data_dir=data)
    _check(got["ok"] is True and [p["id"] for p in got["plans"]] == ["alpha", "beta"],
           f"plan order {[p['id'] for p in got['plans']]}")
    a, b = got["plans"]
    _check(set(a) == {"id", "title", "status", "created", "design", "integration", "version",
                      "approved_at", "runs", "live_run", "counts"}, f"list keys {sorted(a)}")
    _check("phases" not in a and "phases" not in b, "the list reply carries phases")
    _check(a["status"] == "approved" and a["created"] == "2026-01-01T00:00:00Z"
           and a["design"] == ["d1", "d2"] and a["integration"] == "int-branch"
           and a["version"] == 3 and a["approved_at"] == "2026-01-01T00:00:00Z",
           f"alpha columns {a}")
    _check(a["runs"] == ["r2", "r1"], f"alpha runs {a['runs']}")  # newest first, decoy zz misses
    _check(a["live_run"] == "r2", f"alpha live_run {a['live_run']}")
    # a run started from the archive copy links too; "alpha" never claims plan "alpha-v2"'s archive
    con = open_project(home, data_dir=data)
    try:
        con.execute("INSERT INTO runs(id, plan, status, started) VALUES (?,?,?,?)",
                    ("ra", ".matrixkoder/plans/archive/alpha-v1.md", "done", "2026-01-02T00:00:00Z"))
        con.execute("INSERT INTO runs(id, plan, status, started) VALUES (?,?,?,?)",
                    ("rx", "plans/archive/alpha-v2-v1.md", "done", "2026-01-02T00:00:00Z"))
        con.commit()
        _check([r["id"] for r in _runs_of(con, "alpha")] == ["r2", "r1", "ra"],
               f"archive-path runs {[r['id'] for r in _runs_of(con, 'alpha')]}")
        con.execute("DELETE FROM runs WHERE id IN ('ra', 'rx')")
        con.commit()
    finally:
        con.close()
    _check(a["counts"] == {"planned": 2, "building": 1, "done": 1, "blocked": 1},
           f"alpha counts {a['counts']}")  # ph1 and loner are roots; old-t is another run
    _check(b["status"] == "draft" and b["created"] == "2026-02-02T00:00:00Z",
           f"beta header fallback {b}")  # columns NULL, values from header_json
    _check(b["design"] == [] and b["integration"] is None, f"beta design/integration {b}")
    # mk-plan writes integration as a JSON array text; the reply carries the branch name
    _check(_integration_of('["main"]') == "main" and _integration_of("int-branch") == "int-branch"
           and _integration_of(None) is None, "integration array text")
    _check(b["runs"] == [] and b["live_run"] is None and b["counts"] == {}, f"beta no-run {b}")
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), "plans", "--home", home,
                           "--data", data, "--json"], capture_output=True, text=True)
    _check(proc.returncode == 0 and "Traceback" not in proc.stderr,
           f"plans cli rc={proc.returncode} err={proc.stderr[-300:]}")
    cli = json.loads(proc.stdout)
    _check(cli["ok"] is True and [p["id"] for p in cli["plans"]] == ["alpha", "beta"]
           and cli["plans"][0]["counts"] == a["counts"] and cli["plans"][1]["live_run"] is None,
           f"plans cli reply {cli}")


def _plans_detail(home):
    """plans(<id>) returns the phases: built from the newest run (waves cross-filled from
    header_json, subtasks nested) or all 'planned' from header_json with no run; an unknown
    id is a Fail, and the CLI turns it into ok:false exit 2."""
    data = _seed_plans(home, "plans-d")
    # V1.5.1: task/subtask nodes carry the routing columns. add_task has no slot/pane/branch/
    # acceptance/owned parameters (they are lane-orchestrator writes), so the seeded rows are
    # updated in place: a1 fully routed, a2 garbage route_json, a3/a4 left empty, loner a
    # parentless task row (the live store's D1.5-style round root) routed like a task.
    con = open_project(home, data_dir=data)
    try:
        con.execute(
            "UPDATE tasks SET slot='3', pane='mk:0.1', branch='mk/r2/v1',"
            " updated='2026-01-05T00:00:00Z', acceptance='ac: a1 green', owned='a2, a3',"
            " deps='a3', route_json=? WHERE run='r2' AND id='a1'",
            (json.dumps({"agent": "opencode", "model": "zai-coding-plan/glm-5.3",
                         "effort": "high"}),))
        con.execute("UPDATE tasks SET route_json='{oops' WHERE run='r2' AND id='a2'")
        con.execute("UPDATE tasks SET slot='7', pane='w1:p2', route_json=?"
                    " WHERE run='r2' AND id='loner'",
                    (json.dumps({"agent": "mk-lane", "model": None, "effort": "low"}),))
        con.commit()
    finally:
        con.close()
    got = plans(home, data_dir=data, plan_id="alpha")
    _check(got["ok"] is True and got["id"] == "alpha", f"detail id {got.get('id')}")
    _check(set(got) == {"ok", "id", "title", "status", "created", "design", "integration",
                        "version", "approved_at", "runs", "live_run", "counts", "phases"},
           f"detail keys {sorted(got)}")
    ids = [p["id"] for p in got["phases"]]
    _check(ids == ["ph1", "loner"], f"detail phases {ids}")  # row order; header ph2 never ran
    ph1, loner = got["phases"]
    phase_keys = {"id", "title", "wave", "deps", "status", "tasks", "slot", "pane", "branch",
                  "updated", "acceptance", "owned", "route"}
    _check(ph1["wave"] == 1 and ph1["title"] == "Phase one" and ph1["status"] == "planned"
           and ph1["deps"] == [] and set(ph1) == phase_keys and ph1["slot"] is None
           and ph1["route"] is None, f"ph1 {ph1}")  # wave from header_json; routing null-defaulted
    _check([t["id"] for t in ph1["tasks"]] == ["a1", "a2", "a3", "a4"], f"ph1 tasks {ph1['tasks']}")
    task_keys = {"id", "title", "status", "parent", "type", "round", "verdict", "slot", "pane",
                 "branch", "updated", "acceptance", "owned", "deps", "route", "subtasks"}
    a1, a2, a3, a4 = ph1["tasks"]
    _check(a1["parent"] == "ph1" and a1["type"] == "task" and a1["status"] == "planned"
           and set(a1) == task_keys, f"a1 {a1}")
    _check(a1["slot"] == 3 and a1["pane"] == "mk:0.1" and a1["branch"] == "mk/r2/v1"
           and a1["updated"] == "2026-01-05T00:00:00Z" and a1["acceptance"] == "ac: a1 green",
           f"a1 routing {a1}")  # slot is TEXT '3' in the row; the reply carries int 3
    _check(a1["owned"] == ["a2", "a3"] and a1["deps"] == ["a3"], f"a1 owned/deps {a1}")
    _check(a1["route"] == {"agent": "opencode", "model": "zai-coding-plan/glm-5.3",
                           "effort": "high"}, f"a1 route {a1['route']}")
    _check(a2["route"] is None, f"garbage route_json must read as null: {a2}")  # '{oops'
    _check(a3["route"] is None and a3["slot"] is None and a3["owned"] == [] and a3["deps"] == [],
           f"a3 defaults {a3}")  # empty route_json and no routing columns set
    a1s = a1["subtasks"][0]
    _check([s["id"] for s in a1["subtasks"]] == ["a1s"]
           and a1s["parent"] == "a1" and a1s["type"] == "subtask" and set(a1s) == task_keys
           and a1s["slot"] is None and a1s["route"] is None and a1s["subtasks"] == [],
           f"a1 subtasks {a1['subtasks']}")
    # a parentless task row renders at phase level with the same key set: routing keys on,
    # phase keys (wave/tasks) on, and no task-only keys (parent/type/round/verdict)
    _check(loner["wave"] is None and loner["tasks"] == [] and set(loner) == phase_keys,
           f"root task row as phase {loner}")
    _check(loner["slot"] == 7 and loner["pane"] == "w1:p2"
           and loner["route"] == {"agent": "mk-lane", "model": None, "effort": "low"},
           f"root task row routing {loner}")
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), "plans", "beta", "--home",
                           home, "--data", data, "--json"], capture_output=True, text=True)
    _check(proc.returncode == 0 and "Traceback" not in proc.stderr,
           f"beta detail cli rc={proc.returncode} err={proc.stderr[-300:]}")
    beta = json.loads(proc.stdout)
    _check(beta["ok"] is True and beta["runs"] == [] and beta["counts"] == {}
           and beta["live_run"] is None, f"beta detail {beta}")
    _check([p["id"] for p in beta["phases"]] == ["ph1", "ph2"], f"beta phases {beta['phases']}")
    ph1b, ph2b = beta["phases"]
    _check(ph1b["wave"] == 1 and ph2b["wave"] == 2 and ph2b["deps"] == ["ph1"]
           and ph1b["deps"] == [], f"beta waves/deps {beta['phases']}")
    _check(all(p["status"] == t["status"] == s["status"] == "planned"
               for p in beta["phases"] for t in p["tasks"] for s in t["subtasks"]),
           f"beta statuses not all planned: {beta['phases']}")
    s1 = ph1b["tasks"][0]["subtasks"][0]
    _check(s1["id"] == "s1" and s1["parent"] == "a1" and s1["type"] == "subtask"
           and ph1b["tasks"][0]["status"] == "planned", f"beta subtask {s1}")
    # V1.5.1 shape parity in the header fallback: same task_keys, all routing fields defaulted
    bt = ph1b["tasks"][0]
    _check(set(bt) == task_keys and bt["slot"] is None and bt["pane"] is None
           and bt["branch"] is None and bt["updated"] is None and bt["acceptance"] is None
           and bt["owned"] == [] and bt["deps"] == [] and bt["route"] is None,
           f"beta header task shape {bt}")
    _check(set(s1) == task_keys and s1["slot"] is None and s1["route"] is None
           and s1["deps"] == [], f"beta header subtask shape {s1}")
    _check(set(ph1b) == phase_keys and ph1b["slot"] is None and ph1b["route"] is None,
           f"beta header phase shape {ph1b}")
    _check(ph2b["tasks"][0]["deps"] == ["a1"],
           f"beta header task deps {ph2b['tasks'][0]}")  # deps from the header task dict
    try:
        plans(home, data_dir=data, plan_id="nope")
    except Fail:
        pass
    else:
        raise SystemExit("SELF-TEST FAIL: plans() accepted an unknown id")
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), "plans", "nope", "--home",
                           home, "--data", data, "--json"], capture_output=True, text=True)
    _check(proc.returncode == 2 and "Traceback" not in proc.stderr,
           f"unknown id rc={proc.returncode} err={proc.stderr[-300:]}")
    _check(json.loads(proc.stdout)["ok"] is False, f"unknown id out {proc.stdout!r}")


def _seed_run(home, name, task="t"):
    data = os.path.join(home, name)
    con = open_project(home, data_dir=data)
    try:
        _begin(con)
        con.execute("INSERT INTO runs(id, status) VALUES ('r', 'active')")
        con.execute("COMMIT")
        add_task(con, "r", task, "task")
    finally:
        con.close()
    return data


def _live_event_worker(home, data, slot, n):
    for i in range(n):
        append_event(home, "live", "r", task="t", chan="bus", kind="info",
                     text=f"{slot}:{i}", data_dir=data)


def _live_writers(home):
    data = _seed_run(home, "live")
    a = append_event(home, "live", "r", task="t", chan="bus", kind="info",
                     from_id="a", to_id="b", reply_to="0", text="hello",
                     meta={"k": 1}, t="2020-01-01T00:00:00+00:00", data_dir=data)
    b = append_event(home, "live", "r", chan="note", kind="note", text="run-level", data_dir=data)
    _check(b == a + 1, f"ids not increasing: {a}, {b}")
    con = open_project(home, data_dir=data)
    try:
        row = dict(con.execute("SELECT * FROM events WHERE id=?", (a,)).fetchone())
        _check(row["chan"] == "bus" and row["kind"] == "info" and row["text"] == "hello", row)
        _check(row["from_id"] == "a" and row["to_id"] == "b" and row["reply_to"] == "0", row)
        _check(json.loads(row["meta_json"]) == {"k": 1}, row["meta_json"])
        _check(row["t"] == "2020-01-01T00:00:00+00:00", row["t"])
        clog = con.execute("SELECT COUNT(*) c FROM change_log").fetchone()["c"]
        status = con.execute("SELECT status FROM tasks WHERE id='t'").fetchone()["status"]
        # add_task writes no change_log; append_event must not add one or move status.
        _check(clog == 0 and status == "planned", f"live event touched status or change_log {clog} {status}")
    finally:
        con.close()
    for chan, kind, blame in (("state", "transition", "transition()"), ("nope", "info", "unknown chan")):
        try:
            append_event(home, "live", "r", task="t", chan=chan, kind=kind, data_dir=data)
        except Fail as e:
            _check(blame in str(e), f"expected {blame} in {e}")
        else:
            raise SystemExit(f"SELF-TEST FAIL: chan={chan} kind={kind} accepted")
    for kwargs, blame in (({"run": "missing"}, "no run"), ({"task": "missing"}, "no task")):
        try:
            append_event(home, "live", kwargs.get("run", "r"), task=kwargs.get("task", "t"),
                         chan="bus", kind="info", data_dir=data)
        except Fail as e:
            _check(blame in str(e), f"expected {blame} in {e}")
        else:
            raise SystemExit(f"SELF-TEST FAIL: {blame} accepted")
    _check(append_ack(home, "live", "r", "m1", "lane", 1, data_dir=data) is True, "ack insert")
    _check(append_ack(home, "live", "r", "m1", "lane", 1, data_dir=data) is False, "ack not idempotent")
    _check(append_ack(home, "live", "r", "m1", "lane", 0, data_dir=data) is False, "ack level decreased")
    _check(append_ack(home, "live", "r", "m1", "lane", 2, data_dir=data) is True, "ack upgrade")
    con = open_project(home, data_dir=data)
    try:
        level = con.execute("SELECT level FROM acks WHERE msg='m1'").fetchone()["level"]
        _check(level == 2, f"ack level {level}")
    finally:
        con.close()
    try:
        append_ack(home, "live", "nope", "m", "lane", 1, data_dir=data)
    except Fail as e:
        _check("no run" in str(e), str(e))
    else:
        raise SystemExit("SELF-TEST FAIL: ack for unknown run accepted")
    one = put_artifact(home, "live", "r", "t", 1, "report", "v1", data_dir=data)
    two = put_artifact(home, "live", "r", "t", 1, "report", "v2", data_dir=data)
    three = put_artifact(home, "live", "r", "t", 1, "report", "v3", data_dir=data)
    other = put_artifact(home, "live", "r", "t", 2, "report", "other", data_dir=data)
    _check([one["rev"], two["rev"], three["rev"], other["rev"]] == [1, 2, 3, 1],
           f"revs {[one, two, three, other]}")
    ev = append_event(home, "live", "r", task="t", chan="bus", kind="report", text="body", data_dir=data)
    linked = put_artifact(home, "live", "r", "t", 1, "report", "linked", event_id=ev, data_dir=data)
    con = open_project(home, data_dir=data)
    try:
        got = con.execute("SELECT artifact_id FROM events WHERE id=?", (ev,)).fetchone()["artifact_id"]
        src = con.execute("SELECT src FROM artifacts WHERE id=?", (linked["id"],)).fetchone()["src"]
        _check(got == linked["id"] and src is None, f"link {got} src {src}")
        n_art = con.execute("SELECT COUNT(*) c FROM artifacts").fetchone()["c"]
    finally:
        con.close()
    os.environ["MKDB_FAIL_AFTER_ARTIFACT"] = "1"
    try:
        try:
            put_artifact(home, "live", "r", "t", 1, "report", "boom", event_id=ev, data_dir=data)
        except Fail as e:
            _check("injected" in str(e), str(e))
        else:
            raise SystemExit("SELF-TEST FAIL: injected artifact failure did not raise")
    finally:
        os.environ.pop("MKDB_FAIL_AFTER_ARTIFACT", None)
    con = open_project(home, data_dir=data)
    try:
        after = con.execute("SELECT COUNT(*) c FROM artifacts").fetchone()["c"]
        still = con.execute("SELECT artifact_id FROM events WHERE id=?", (ev,)).fetchone()["artifact_id"]
        _check(after == n_art and still == linked["id"], f"partial write artifacts {after} link {still}")
    finally:
        con.close()
    big = "x" * ARTIFACT_MAX
    ok = put_artifact(home, "live", "r", "t", 3, "report", big, data_dir=data)
    _check(ok["rev"] == 1, f"256 KiB rev {ok}")
    try:
        put_artifact(home, "live", "r", "t", 3, "report", big + "y", data_dir=data)
    except Fail as e:
        _check("256" in str(e) or str(ARTIFACT_MAX) in str(e), str(e))
    else:
        raise SystemExit("SELF-TEST FAIL: oversized artifact accepted")
    procs = []
    for slot in (0, 1):
        proc = multiprocessing.get_context("fork").Process(
            target=_live_event_worker, args=(home, data, slot, 50))
        proc.start()
        procs.append(proc)
    for proc in procs:
        proc.join(120)
        _check(proc.exitcode == 0, f"live writer exit {proc.exitcode}")
    con = open_project(home, data_dir=data)
    try:
        ids = [r[0] for r in con.execute(
            "SELECT id FROM events WHERE chan='bus' AND kind='info' AND text LIKE '%:%'")]
        _check(len(ids) == 100 and len(set(ids)) == 100, f"raced ids {len(ids)} distinct {len(set(ids))}")
    finally:
        con.close()
    lines = [e["text"] for e in timeline(home, "live", "r", "t", data) if e["chan"] == "bus"]
    _check(lines[0] == "hello" and "0:0" in lines and "1:49" in lines, f"timeline {lines[:3]}…{lines[-1:]}")
    _check(lines.index("0:0") < lines.index("0:49"), "timeline not in id order")



def _card_verdict(home):
    """A transition that changes the verdict updates the root card; catch_up copies it too."""
    data = os.path.join(home, "verdict")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "t", "task")
        con.execute("UPDATE tasks SET verdict=? WHERE run=? AND id=?", ("APPROVE", "r", "t"))
        con.commit()
    finally:
        con.close()
    transition(home, "verdict", "r", "t", "review", round=1, sync_root=True, data_dir=data)
    got = cards(home, "verdict")
    _check(len(got) == 1 and got[0]["verdict"] == "APPROVE", f"import-style verdict {got}")
    transition(home, "verdict", "r", "t", "done", round=2, verdict="REQUEST CHANGES",
               sync_root=True, data_dir=data)
    got = cards(home, "verdict")
    _check(len(got) == 1 and got[0]["verdict"] == "REQUEST CHANGES" and got[0]["status"] == "done",
           f"transition verdict {got}")
    con = open_project(home, data_dir=data)
    try:
        stored = con.execute("SELECT verdict FROM tasks WHERE id='t'").fetchone()["verdict"]
        _check(stored == "REQUEST CHANGES", stored)
        root = open_root(home)
        try:
            root.execute("UPDATE cards SET verdict=NULL WHERE project='verdict' AND task='t'")
            root.execute("UPDATE sync SET last_change_no=0 WHERE project='verdict'")
            root.commit()
        finally:
            root.close()
    finally:
        con.close()
    caught = catch_up(home, "verdict", data)
    got = cards(home, "verdict")
    _check(caught >= 1 and got[0]["verdict"] == "REQUEST CHANGES", f"catch_up verdict {got} n={caught}")


# --- S1 summary self-tests: extract this whole region for the old-code probe ---
def _summary_row(root, project):
    """The stored project_summary row as a plain tuple, or None while the writer is absent."""
    r = root.execute(
        "SELECT active_runs, by_status_json, attention, last_event, change_no"
        " FROM project_summary WHERE project=?", (project,)).fetchone()
    return tuple(r) if r else None


def _summary_recount(home, data_dir):
    """Independent recount of the summary contract, computed here with fresh SQL (C1).

    active_runs = runs with status IN ('running','active'); by_status_json = json.dumps of
    {status: count} over every task, sort_keys=True; attention = tasks in blocked/stalled/
    quota/provider; last_event = MAX(t) over events, NULL with no events. No writer helper
    is reused: the recount must not inherit the bug it polices.
    """
    con = open_project(home, data_dir=data_dir)
    try:
        counts = {r["status"]: r["c"] for r in
                  con.execute("SELECT status, COUNT(*) c FROM tasks GROUP BY status")}
        active = con.execute(
            "SELECT COUNT(*) c FROM runs WHERE status IN ('running','active')").fetchone()["c"]
        last = con.execute("SELECT MAX(t) FROM events").fetchone()[0]
    finally:
        con.close()
    return (active, json.dumps(counts, sort_keys=True),
            sum(n for s, n in counts.items() if s in ATTENTION), last)


def _summary_import_sample():
    """One small historical run: a done task, an attention task, two past-timestamped events."""
    run_row = {"id": "hist", "plan": "p", "plan_version": 1, "branch": "b", "status": "done",
               "stage": None, "next": None, "started": "2021-01-01T00:00:00+00:00",
               "ended": "2021-01-02T00:00:00+00:00"}
    tasks = [
        {"id": "a", "type": "task", "title": "a", "status": "done",
         "updated": "2021-01-01T01:00:00+00:00"},
        {"id": "b", "type": "task", "title": "b", "status": "quota",
         "updated": "2021-01-01T02:00:00+00:00"},
    ]
    events = [
        {"src": "s1", "task": "a", "t": "2021-01-01T00:00:01+00:00", "chan": "state",
         "kind": "transition", "text": "started"},
        {"src": "s2", "task": "b", "t": "2021-01-01T00:00:09+00:00", "chan": "bus",
         "kind": "note", "text": "hit the quota"},
    ]
    return run_row, tasks, events


def _summary_transitions(home):
    """Transitions across two seeded runs: the row equals an independent recount (C1).

    One run is 'active', one 'done', so active_runs must count only the first; the blocked
    task must raise attention; change_no must equal the sync watermark — the last change
    the root has seen for the project.
    """
    data = os.path.join(home, "sumtr")
    con = open_project(home, data_dir=data)
    try:
        _begin(con)
        con.execute("INSERT INTO runs(id, status) VALUES ('r1', 'active')")
        con.execute("INSERT INTO runs(id, status) VALUES ('r2', 'done')")
        con.execute("COMMIT")
        add_task(con, "r1", "t1", "task")
        add_task(con, "r1", "t2", "task")
        add_task(con, "r2", "t3", "task")
        add_task(con, "r2", "t4", "task")
    finally:
        con.close()
    transition(home, "sumtr", "r1", "t1", "building", sync_root=True, data_dir=data)
    transition(home, "sumtr", "r1", "t2", "blocked", sync_root=True, data_dir=data)
    transition(home, "sumtr", "r2", "t3", "done", sync_root=True, data_dir=data)
    transition(home, "sumtr", "r2", "t4", "review", sync_root=True, data_dir=data)
    root = open_root(home)
    try:
        row = _summary_row(root, "sumtr")
        _check(row is not None, "_summary_transitions: no project_summary row for 'sumtr'")
        expect = _summary_recount(home, data)
        _check(row[:4] == expect, f"_summary_transitions: row {row[:4]} != recount {expect}")
        last = root.execute("SELECT last_change_no FROM sync WHERE project='sumtr'").fetchone()
        _check(last and row[4] == last[0],
               f"_summary_transitions: change_no {row[4]} != watermark {last[0] if last else None}")
    finally:
        root.close()


def _summary_regress(home):
    """done -> fix rewrites the summary: by_status_json differs and matches the recount again."""
    data = os.path.join(home, "sumre")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "tmain", "task")
        add_task(con, "r", "tdep", "task", deps="tmain")
    finally:
        con.close()
    transition(home, "sumre", "r", "tmain", "done", sync_root=True, data_dir=data)
    root = open_root(home)
    try:
        row = _summary_row(root, "sumre")
        _check(row is not None, "_summary_regress: no project_summary row for 'sumre'")
        old_json, old_no = row[1], row[4]
    finally:
        root.close()
    transition(home, "sumre", "r", "tmain", "fix", sync_root=True, data_dir=data)
    root = open_root(home)
    try:
        row = _summary_row(root, "sumre")
        expect = _summary_recount(home, data)
        _check(row[:4] == expect, f"_summary_regress: row {row[:4]} != recount {expect}")
        _check(row[1] != old_json, "_summary_regress: done->fix left by_status_json unchanged")
        last = root.execute("SELECT last_change_no FROM sync WHERE project='sumre'").fetchone()
        _check(last and row[4] == last[0] and row[4] >= old_no,
               f"_summary_regress: change_no {row[4]} vs watermark {last[0] if last else None},"
               f" previously {old_no}")
    finally:
        root.close()


def _summary_import(home):
    """An imported run: catch_up builds the summary; last_event is the newest imported t."""
    data = os.path.join(home, "sumim")
    run_row, tasks, events = _summary_import_sample()
    import_history(home, "sumim", "hist", run_row=run_row, tasks=tasks, events=events, data_dir=data)
    caught = catch_up(home, "sumim", data_dir=data)
    _check(caught >= 1, f"_summary_import: catch_up replayed nothing (n={caught})")
    root = open_root(home)
    try:
        row = _summary_row(root, "sumim")
        _check(row is not None, "_summary_import: no project_summary row for 'sumim'")
        expect = _summary_recount(home, data)
        _check(row[:4] == expect, f"_summary_import: row {row[:4]} != recount {expect}")
        newest = max(e["t"] for e in events)
        _check(row[3] == newest, f"_summary_import: last_event {row[3]} != max imported t {newest}")
    finally:
        root.close()


def _summary_kill(home):
    """A kill between the two writes: the project moved, the root got neither card nor
    summary; catch_up heals both and the healed row equals the recount."""
    data = os.path.join(home, "sumki")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "t", "task")
    finally:
        con.close()
    os.environ["MKDB_FAIL_BEFORE_ROOT"] = "1"
    try:
        try:
            transition(home, "sumki", "r", "t", "building", sync_root=True, data_dir=data)
        except Fail:
            pass
        else:
            raise SystemExit("SELF-TEST FAIL: _summary_kill: the injected root failure did not fire")
        con = open_project(home, data_dir=data)
        try:
            _check(con.execute("SELECT status FROM tasks WHERE id='t'").fetchone()["status"]
                   == "building", "_summary_kill: project change lost")
        finally:
            con.close()
        root = open_root(home)
        try:
            _check(cards(home, "sumki") == [], "_summary_kill: root card written despite the failure")
            _check(_summary_row(root, "sumki") is None,
                   "_summary_kill: summary written despite the failure")
        finally:
            root.close()
        os.environ.pop("MKDB_FAIL_BEFORE_ROOT", None)
        caught = catch_up(home, "sumki", data_dir=data)
        _check(caught >= 1 and len(cards(home, "sumki")) == 1,
               f"_summary_kill: catch_up did not heal the card (n={caught})")
        root = open_root(home)
        try:
            row = _summary_row(root, "sumki")
            _check(row is not None, "_summary_kill: no project_summary row after catch_up")
            expect = _summary_recount(home, data)
            _check(row[:4] == expect, f"_summary_kill: row {row[:4]} != recount {expect}")
        finally:
            root.close()
    finally:
        os.environ.pop("MKDB_FAIL_BEFORE_ROOT", None)


def _summary_idempotent(home):
    """A second catch_up over an already-synced store writes nothing.

    One root connection stays open across both calls: PRAGMA data_version on it moves only
    when another connection commits to the file, so it doubles the row/watermark/cards
    checks. If data_version ever proves unusable here, those three checks below are the
    idempotence contract on their own.
    """
    data = os.path.join(home, "sumid")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "t1", "task")
        add_task(con, "r", "t2", "task")
    finally:
        con.close()
    transition(home, "sumid", "r", "t1", "building", sync_root=True, data_dir=data)
    transition(home, "sumid", "r", "t2", "done", sync_root=True, data_dir=data)
    # one change the root has not seen, so the first catch_up has real work to do
    transition(home, "sumid", "r", "t1", "review", sync_root=False, data_dir=data)
    caught = catch_up(home, "sumid", data_dir=data)
    _check(caught >= 1, f"_summary_idempotent: first catch_up replayed nothing (n={caught})")
    root = open_root(home)
    try:
        row = _summary_row(root, "sumid")
        _check(row is not None, "_summary_idempotent: no project_summary row for 'sumid'")
        dv = root.execute("PRAGMA data_version").fetchone()[0]
        before = [tuple(c) for c in root.execute(
            "SELECT run, task, status, change_no FROM cards WHERE project='sumid'"
            " ORDER BY run, task")]
        wm = root.execute("SELECT last_change_no FROM sync WHERE project='sumid'").fetchone()[0]
        again = catch_up(home, "sumid", data_dir=data)
        _check(again == 0, f"_summary_idempotent: second catch_up replayed {again}")
        dv2 = root.execute("PRAGMA data_version").fetchone()[0]
        row2 = _summary_row(root, "sumid")
        after = [tuple(c) for c in root.execute(
            "SELECT run, task, status, change_no FROM cards WHERE project='sumid'"
            " ORDER BY run, task")]
        wm2 = root.execute("SELECT last_change_no FROM sync WHERE project='sumid'").fetchone()[0]
        _check(dv == dv2, f"_summary_idempotent: data_version moved {dv} -> {dv2}")
        _check(row == row2, f"_summary_idempotent: row changed {row} -> {row2}")
        _check(before == after, "_summary_idempotent: cards changed")
        _check(wm == wm2, f"_summary_idempotent: watermark moved {wm} -> {wm2}")
    finally:
        root.close()


def _summary_two_projects(home):
    """Two data-dir projects under one root: each row matches its own recount, and the
    rows differ — no project's summary leaks into the other's."""
    p1, p2 = os.path.join(home, "p1data"), os.path.join(home, "p2data")
    con = open_project(home, data_dir=p1)
    try:
        add_task(con, "r", "a1", "task")
        add_task(con, "r", "a2", "task")
    finally:
        con.close()
    con = open_project(home, data_dir=p2)
    try:
        add_task(con, "r", "b1", "task")
        add_task(con, "r", "b2", "task")
        add_task(con, "r", "b3", "task")
    finally:
        con.close()
    transition(home, "sump1", "r", "a1", "building", sync_root=True, data_dir=p1)
    transition(home, "sump1", "r", "a2", "blocked", sync_root=True, data_dir=p1)
    transition(home, "sump2", "r", "b1", "done", sync_root=True, data_dir=p2)
    transition(home, "sump2", "r", "b2", "review", sync_root=True, data_dir=p2)
    transition(home, "sump2", "r", "b3", "stalled", sync_root=True, data_dir=p2)
    root = open_root(home)
    try:
        r1, r2 = _summary_row(root, "sump1"), _summary_row(root, "sump2")
        _check(r1 is not None, "_summary_two_projects: no project_summary row for 'sump1'")
        _check(r2 is not None, "_summary_two_projects: no project_summary row for 'sump2'")
        e1, e2 = _summary_recount(home, p1), _summary_recount(home, p2)
        _check(r1[:4] == e1, f"_summary_two_projects: sump1 row {r1[:4]} != recount {e1}")
        _check(r2[:4] == e2, f"_summary_two_projects: sump2 row {r2[:4]} != recount {e2}")
        _check(r1 != r2, "_summary_two_projects: the two projects share one summary row")
    finally:
        root.close()
# --- end S1 summary self-tests ---


# --- L1 plan_summary self-tests: extract this whole region for the old-code probe ---
PLAN_SUMMARY_COLS = ("project", "plan", "title", "status", "counts_json", "live_run", "updated")
PROJECTS_V2_COLS = ("slug", "name", "repo", "data_dir", "mode", "created", "last_activity", "db_path")


def _cols(con, table):
    """The column names of one table, in stored order (PRAGMA table_info)."""
    return tuple(r["name"] for r in con.execute(f"PRAGMA table_info({table})"))


def _plan_recount(home, data_dir, plan):
    """An independent recount of one plan's counts, computed here with fresh SQL (C3).

    Counts are the tasks of the plan's newest run, phase-level (parentless) rows excluded —
    the rule plans() documents. No writer helper is reused: the recount must not inherit the
    bug it polices.
    """
    con = open_project(home, data_dir=data_dir)
    try:
        run = con.execute("SELECT id FROM runs WHERE plan=? ORDER BY started DESC, rowid DESC",
                          (plan,)).fetchone()
        if run is None:
            return {}
        rows = con.execute("SELECT id, parent, status FROM tasks WHERE run=? ORDER BY rowid",
                           (run["id"],)).fetchall()
    finally:
        con.close()
    ids = {r["id"] for r in rows}
    counts = {}
    for r in rows:
        if r["parent"] and r["parent"] in ids:  # a child; a root row is a phase, not a task
            counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


def _stored_plan(home, project, plan):
    """The stored plan_summary row as a plain dict, or None while the writer is absent."""
    root = open_root(home)
    try:
        r = root.execute(
            "SELECT " + ",".join(PLAN_SUMMARY_COLS) + " FROM plan_summary WHERE project=? AND plan=?",
            (project, plan)).fetchone()
    finally:
        root.close()
    return dict(r) if r else None


def _seed_plan_summary_store(home, name):
    """Two plans, one active run each, three child tasks under a phase row per plan.

    The plans rows and runs go in as the store's own shape (mk-plan's approved row, the run row
    import_history writes); every STATUS after this is moved through transition().
    """
    data = os.path.join(home, name)
    con = open_project(home, data_dir=data)
    try:
        for plan, title in (("alpha", "Alpha"), ("beta", "Beta")):
            run = "r-" + plan
            _begin(con)
            try:
                con.execute("INSERT INTO plans(id, version, title, header_json, status, created)"
                            " VALUES (?,?,?,?,?,?)",
                            (plan, 1, title, json.dumps({"plan": plan, "status": "running"}),
                             "running", "2026-01-01T00:00:00+00:00"))
                con.execute("INSERT INTO runs(id, plan, status, started) VALUES (?,?,?,?)",
                            (run, plan, "active", "2026-01-01T00:00:00+00:00"))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
    finally:
        con.close()
    return data


def _add_plan_summary_tasks(data):
    """One phase row plus three task rows per plan, through add_task (the store's own writer)."""
    con = open_project(os.path.dirname(data), data_dir=data)
    try:
        for plan in ("alpha", "beta"):
            run = "r-" + plan
            add_task(con, run, plan.upper(), "phase", title=plan.title())
            for i in (1, 2, 3):
                add_task(con, run, f"{plan.upper()}{i}", "task", title=f"{plan} {i}",
                         parent=plan.upper())
    finally:
        con.close()



def _repo_root():
    """The worktree root that holds scripts/mkdb.py: the nearest ancestor of this file with a
    .git entry, so the old-code probe (which runs the file from a temp copy) finds the same one."""
    path = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    raise SystemExit("SELF-TEST FAIL: no git worktree above scripts/mkdb.py")



def _root_v2_migration(home, tmp):
    """A root migrated from the SHIPPED v1 schema ends at the same schema as a fresh one.

    The v1 root is built by running the OLD mkdb.py (git show 6cbba59:scripts/mkdb.py) against
    a temp home, not by a hand-written CREATE, so the shape under test is the one users have.
    Every row the old file wrote must survive the migration (review level: data-loss).
    """
    repo = _repo_root()
    old_path = os.path.join(tmp, "old_mkdb.py")
    src = subprocess.run(["git", "-C", repo, "show", "6cbba59:scripts/mkdb.py"],
                         capture_output=True, text=True)
    _check(src.returncode == 0, f"git show 6cbba59:scripts/mkdb.py failed: {src.stderr.strip()}")
    with open(old_path, "w") as fh:
        fh.write(src.stdout)
    old_home = os.path.join(home, "migrated-root")
    driver = os.path.join(tmp, "old_root_driver.py")
    with open(driver, "w") as fh:
        fh.write(
            "import importlib.util, json, os, sys\n"
            "spec = importlib.util.spec_from_file_location('old_mkdb', sys.argv[1])\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "home = sys.argv[2]\n"
            "root = m.open_root(home)\n"
            "root.execute('INSERT INTO projects(slug, name, repo, data_dir, mode, created,"
            " last_activity) VALUES (?,?,?,?,?,?,?)',\n"
            "            ('legacy', 'Legacy', '/r/legacy', '/d/legacy', 'local', 't1', 't2'))\n"
            "root.execute('INSERT INTO cards(project, run, task, parent, type, title, status, col,"
            " attention, updated, change_no) VALUES (?,?,?,?,?,?,?,?,?,?,?)',\n"
            "            ('legacy', 'r', 't', None, 'task', 'Kept', 'done', 'Done', 0, 't3', 7))\n"
            "root.commit(); root.close()\n"
            "print(json.dumps({'projects': m.projects(home)}))\n")
    ran = subprocess.run([sys.executable, driver, old_path, old_home], capture_output=True, text=True)
    _check(ran.returncode == 0, f"the old mkdb.py did not build a root: {ran.stderr.strip()[-400:]}")
    db = os.path.join(old_home, "mk.db")
    _check(os.path.isfile(db), "the old mkdb.py created no root store")

    migrated = open_root(old_home)
    try:
        _check(_cols(migrated, "projects") == PROJECTS_V2_COLS,
               f"migrated projects columns {_cols(migrated, 'projects')} != {PROJECTS_V2_COLS}")
        _check(_cols(migrated, "plan_summary") == PLAN_SUMMARY_COLS,
               f"migrated plan_summary columns {_cols(migrated, 'plan_summary')}")
        # data-loss: the v1 rows are still there, and db_path starts NULL (set = migrated)
        row = migrated.execute("SELECT slug, repo, db_path FROM projects WHERE slug='legacy'").fetchone()
        _check(row is not None and row["repo"] == "/r/legacy" and row["db_path"] is None,
               f"the migration lost or rewrote the projects row: {dict(row) if row else None}")
        card = migrated.execute("SELECT title, status, change_no FROM cards WHERE project='legacy'").fetchone()
        _check(card is not None and card["title"] == "Kept" and card["change_no"] == 7,
               f"the migration lost a card row: {dict(card) if card else None}")
        _check(migrated.execute("SELECT COUNT(*) c FROM plan_summary").fetchone()["c"] == 0,
               "the migration invented plan_summary rows")
        _check(migrated.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] == ROOT_SCHEMA,
               "the migrated root is not stamped at the current root schema")
    finally:
        migrated.close()

    fresh_home = os.path.join(home, "fresh-root")
    fresh = open_root(fresh_home)
    ref = _open_read(os.path.join(old_home, "mk.db"), ROOT_SCHEMA)  # already migrated; read-only
    try:
        _check(ref is not None, "the migrated root vanished after the writer closed it")
        for table in ("projects", "plan_summary"):
            got, want = _cols(fresh, table), _cols(ref, table)
            _check(got == want, f"fresh {table} {got} != migrated {want}")
    finally:
        fresh.close()
        ref.close()

    # a second open must not stamp again, add a column again or grow the table
    again = open_root(old_home)
    try:
        _check(again.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == ROOT_SCHEMA,
               "the second open stamped the root schema again")
        _check(_cols(again, "projects") == PROJECTS_V2_COLS and
               _cols(again, "plan_summary") == PLAN_SUMMARY_COLS,
               "the second open changed the root schema")
        _check(again.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"] == 1,
               "the second open duplicated a projects row")
    finally:
        again.close()


def _plan_summary_transitions(home):
    """20 real transitions over two plans: plan_summary.counts_json equals the project DB's own
    counts after EVERY one (C3). Half the moves carry sync_root (the card's transaction), half
    do not (their own root transaction), so both writer paths are covered.
    """
    data = _seed_plan_summary_store(home, "plansum")
    _add_plan_summary_tasks(data)
    steps = ("building", "review", "done", "fix", "verifying")
    moves = 0
    for i in range(20):
        plan = "alpha" if i % 2 == 0 else "beta"
        task = f"{plan.upper()}{(i // 2) % 3 + 1}"
        transition(home, "ps", "r-" + plan, task, steps[i % len(steps)], note=f"move {i}",
                   sync_root=(i % 4 < 2), data_dir=data)
        moves += 1
        for checked in ("alpha", "beta"):
            row = _stored_plan(home, "ps", checked)
            _check(row is not None, f"_plan_summary_transitions: no plan_summary row for {checked}")
            want = _plan_recount(home, data, checked)
            got = json.loads(row["counts_json"])
            _check(got == want,
                   f"_plan_summary_transitions: {checked} counts {got} != project DB {want}"
                   f" after move {i + 1}")
            _check(row["title"] == checked.title() and row["status"] == "running",
                   f"_plan_summary_transitions: {checked} title/status {row['title']!r}/"
                   f"{row['status']!r}")
            _check(row["live_run"] == "r-" + checked,
                   f"_plan_summary_transitions: {checked} live_run {row['live_run']!r}")
            _check(row["updated"], f"_plan_summary_transitions: {checked} has no updated stamp")
    _check(moves == 20, f"_plan_summary_transitions: drove {moves} transitions, wanted 20")
    # both plans of this project, and no other project's rows leaked into it
    rows = {(r["project"], r["plan"]) for r in root_rows(home)}
    _check(("ps", "alpha") in rows and ("ps", "beta") in rows, f"plan_summary rows {rows}")
    _check(all(r["counts_json"] not in ("", None) for r in root_rows(home)),
           "a plan_summary row has empty counts")


def root_rows(home):
    """Every stored plan_summary row, as dicts."""
    root = open_root(home)
    try:
        return [dict(r) for r in root.execute("SELECT " + ",".join(PLAN_SUMMARY_COLS)
                                               + " FROM plan_summary")]
    finally:
        root.close()


def _plan_summary_atomic(home):
    """A transition that fails leaves plan_summary exactly as it was (C3 atomicity).

    Two windows: the failure INSIDE the project transaction (nothing commits, so the root must
    not move either), and the failure just BEFORE the root upsert (the project change is durable,
    so plan_summary must still hold the pre-transition numbers until catch_up heals it).
    """
    data = _seed_plan_summary_store(home, "planatom")
    _add_plan_summary_tasks(data)
    transition(home, "atom", "r-alpha", "ALPHA1", "building", sync_root=True, data_dir=data)
    before = {p: _stored_plan(home, "atom", p) for p in ("alpha", "beta")}
    _check(all(before.values()), f"_plan_summary_atomic: no seeded rows {before}")

    os.environ["MKDB_FAIL_IN_TXN"] = "1"
    try:
        try:
            transition(home, "atom", "r-alpha", "ALPHA1", "done", sync_root=True, data_dir=data)
        except Fail:
            pass
        else:
            raise SystemExit("SELF-TEST FAIL: the injected in-transaction failure did not fire")
    finally:
        os.environ.pop("MKDB_FAIL_IN_TXN", None)
    after = {p: _stored_plan(home, "atom", p) for p in ("alpha", "beta")}
    _check(after == before, f"_plan_summary_atomic: a rolled-back transition moved plan_summary")
    _check(json.loads(after["alpha"]["counts_json"]) == _plan_recount(home, data, "alpha"),
           "_plan_summary_atomic: the store and plan_summary disagree after a rollback")

    os.environ["MKDB_FAIL_BEFORE_ROOT"] = "1"
    try:
        try:
            transition(home, "atom", "r-beta", "BETA1", "review", sync_root=True, data_dir=data)
        except Fail:
            pass
        else:
            raise SystemExit("SELF-TEST FAIL: the injected root failure did not fire")
    finally:
        os.environ.pop("MKDB_FAIL_BEFORE_ROOT", None)
    held = {p: _stored_plan(home, "atom", p) for p in ("alpha", "beta")}
    _check(held == before, f"_plan_summary_atomic: the root failure moved plan_summary")
    # the project change is durable, and catch_up heals the root to match it
    caught = catch_up(home, "atom", data)
    _check(caught >= 1, f"_plan_summary_atomic: catch_up replayed nothing (n={caught})")
    healed = {p: _stored_plan(home, "atom", p) for p in ("alpha", "beta")}
    _check(healed != before, "_plan_summary_atomic: catch_up left plan_summary stale")
    for plan in ("alpha", "beta"):
        _check(json.loads(healed[plan]["counts_json"]) == _plan_recount(home, data, plan),
               f"_plan_summary_atomic: healed {plan} counts do not match the project DB")


def _overview_read(home):
    """overview() lists projects and plans with numbers and db_path — and only those keys (C3).

    Read-only like cards()/projects(): a missing root store is an empty overview at exit 0 and
    creates no file, in the API and through the CLI subprocess.
    """
    seeded = os.path.join(home, "ov-seeded")
    root = open_root(seeded)
    try:
        root.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, created, last_activity,"
                     " db_path) VALUES (?,?,?,?,?,?,?,?)",
                     ("migrated", "Migrated", "/r/m", "/d/m", "local", "t1", "t2",
                      "/home/u/.matrixkoder/projects/migrated"))
        root.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, created, last_activity,"
 " db_path) VALUES (?,?,?,?,?,?,?,?)",
                     ("fresh", "Fresh", "/r/f", "/d/f", "local", "t1", "t2", None))
        root.execute("INSERT INTO plan_summary(" + ",".join(PLAN_SUMMARY_COLS) +
                     ") VALUES (?,?,?,?,?,?,?)",
                     ("migrated", "alpha", "Alpha", "running", json.dumps({"done": 2, "planned": 1}),
                      "r-alpha", "2026-01-02T00:00:00+00:00"))
        root.commit()
    finally:
        root.close()

    got = overview(seeded)
    _check(got["ok"] is True, f"overview ok flag {got['ok']}")
    _check([p["slug"] for p in got["projects"]] == ["fresh", "migrated"],
           f"overview project order {[p['slug'] for p in got['projects']]}")
    for p in got["projects"]:
        _check(set(p) == set(OVERVIEW_PROJECT_COLS), f"overview project keys {sorted(p)}")
    _check(got["projects"][0]["db_path"] is None, "an unmigrated project carries a db_path")
    _check(got["projects"][1]["db_path"].endswith("projects/migrated"),
           f"db_path {got['projects'][1]['db_path']!r}")
    _check(len(got["plans"]) == 1, f"overview plans {got['plans']}")
    row = got["plans"][0]
    _check(set(row) == set(OVERVIEW_PLAN_COLS), f"overview plan keys {sorted(row)}")
    _check(row["counts"] == {"done": 2, "planned": 1}, f"overview counts {row['counts']}")
    _check(isinstance(row["counts"], dict), f"overview counts are not an object: {row['counts']!r}")
    _check(row["live_run"] == "r-alpha" and row["status"] == "running", f"overview plan {row}")
    # the key set is the contract: a card, a task or an event must never ride along
    _check(not ({"cards", "tasks", "events", "runs", "project_summary"} & set(got)),
           f"overview carries more than projects and plans: {sorted(got)}")
    cli = _cli(["overview", "--json", "--home", seeded])
    _check(cli == got, f"cli overview differs from the api: {cli}")

    missing = os.path.join(home, "ov-missing")
    _check(not os.path.exists(missing), "fixture dir already exists")
    _check(overview(missing) == {"ok": True, "projects": [], "plans": []},
           f"overview on a missing root {overview(missing)}")
    _check(_cli(["overview", "--json", "--home", missing]) ==
           {"ok": True, "projects": [], "plans": []}, "cli overview on a missing root")
    _check(not os.path.exists(missing), "overview created a missing home")

    bare = os.path.join(home, "ov-bare")
    os.makedirs(bare)
    ran = subprocess.run([sys.executable, __file__, "--home", bare, "overview", "--json"],
                         capture_output=True, text=True)
    _check(ran.returncode == 0, f"overview on an empty home exited {ran.returncode}: {ran.stderr[-300:]}")
    _check(json.loads(ran.stdout) == {"ok": True, "projects": [], "plans": []},
           f"overview stdout {ran.stdout.strip()}")
    _check(not os.path.exists(os.path.join(bare, "mk.db")), "overview created mk.db")

    # a read must not write or migrate: hash the store, read, hash again
    db = os.path.join(seeded, "mk.db")
    before = {n: _file_hash(os.path.join(seeded, n)) for n in ("mk.db", "mk.db-wal", "mk.db-shm")}
    overview(seeded)
    after = {n: _file_hash(os.path.join(seeded, n)) for n in ("mk.db", "mk.db-wal", "mk.db-shm")}
    _check(after == before, f"overview wrote the root store: {before} -> {after}")


def _overview_production_home(home, tmp):
    """The same numbers through the PRODUCTION open path: a temp $HOME with $MK_HOME and
    $MATRIXKODER_HOME unset, so mkdb's own home rule picks <temp $HOME>/.matrixkoder.

    A real copy of the real store shape (root projects row + plan_summary row written by the
    production writers) read back by `mkdb.py overview --json` as its own subprocess. Nothing
    under the real ~/.matrixkoder is read or written: the child gets HOME=temp and MK_SELFTEST=1.
    """
    fake_home = os.path.join(tmp, "prod-home")
    os.makedirs(fake_home)
    mkh = os.path.join(fake_home, ".matrixkoder")
    env = {k: v for k, v in os.environ.items() if k not in ("MK_HOME", "MATRIXKODER_HOME")}
    env["HOME"] = fake_home
    env["MK_SELFTEST"] = "1"
    env["TMPDIR"] = tmp  # the C16 write guard pins the temp tree; the fake $HOME is inside it
    base = [sys.executable, __file__, "--home", mkh, "--data", os.path.join(fake_home, "data")]
    # the production writers first: a plans row and a run through the store's own paths
    made = subprocess.run([sys.executable, "-c",
                           "import importlib.util, sys\n"
                           "spec = importlib.util.spec_from_file_location('mkdb', sys.argv[1])\n"
                           "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                           "home, data = sys.argv[2], sys.argv[3]\n"
                           "con = m.open_project(home, 'prod', data_dir=data)\n"
                           "m._begin(con)\n"
                           "con.execute(\"INSERT INTO plans(id, version, title, header_json, status,"
                           " created) VALUES ('p', 1, 'Prod', '{}', 'running', '2026-01-01')\")\n"
                           "con.execute(\"INSERT INTO runs(id, plan, status, started)"
                           " VALUES ('r', 'p', 'active', '2026-01-01')\")\n"
                           "con.execute('COMMIT'); con.close()\n"
                           "m.add_task(m.open_project(home, 'prod', data_dir=data), 'r', 'ph',"
                           " 'phase', title='P')\n"
                           "m.add_task(m.open_project(home, 'prod', data_dir=data), 'r', 't1',"
                           " 'task', title='One', parent='ph')\n"
                           "m.transition(home, 'prod', 'r', 't1', 'done', sync_root=True,"
                           " data_dir=data)\n"
                           "root = m.open_root(home)\n"
                           "root.execute(\"INSERT INTO projects(slug, name, repo, data_dir, mode,"
                           " created, last_activity, db_path) VALUES ('prod','P','/r','/d','local',"
                           "'t1','t2', NULL)\")\n"
                           "root.commit(); root.close()\n"
                           "print('ok')\n",
                           __file__, mkh, os.path.join(fake_home, "data")],
                          env=env, capture_output=True, text=True)
    _check(made.returncode == 0 and made.stdout.strip().endswith("ok"),
           f"the production open path failed: {made.stderr.strip()[-400:]}")
    _check(os.path.isfile(os.path.join(mkh, "mk.db")), "the production path created no root store")
    ran = subprocess.run(base + ["overview", "--json"], env=env, capture_output=True, text=True)
    _check(ran.returncode == 0, f"overview exited {ran.returncode}: {ran.stderr.strip()[-300:]}")
    got = json.loads(ran.stdout)
    _check([p["slug"] for p in got["projects"]] == ["prod"], f"overview projects {got['projects']}")
    _check(got["projects"][0]["db_path"] is None, f"db_path {got['projects'][0]['db_path']!r}")
    _check(len(got["plans"]) == 1 and got["plans"][0]["plan"] == "p", f"overview plans {got['plans']}")
    _check(got["plans"][0]["counts"] == {"done": 1}, f"overview counts {got['plans'][0]['counts']}")
    _check(got["plans"][0]["live_run"] == "r", f"overview live_run {got['plans'][0]['live_run']!r}")
    _check(os.path.realpath(os.path.join(mkh, "mk.db")).startswith(os.path.realpath(fake_home) + os.sep),
           f"the store landed outside the temp $HOME: {os.path.join(mkh, 'mk.db')}")


# --- end L1 plan_summary self-tests ---


# --- end L1 plan_summary self-tests ---


def _listing(path):
    """Every path under path, relative, so a read that creates anything is visible."""
    found = []
    for dirpath, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(dirs + files):
            found.append(os.path.relpath(os.path.join(dirpath, name), path))
    return sorted(found)


def _file_hash(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest() if os.path.exists(path) else None


def _reads_create_nothing(home):
    """Y1: cards and timeline never create a store, and a read never writes or migrates one."""
    empty = os.path.join(home, "empty-read")
    os.makedirs(empty)
    before = _listing(empty)
    got = json.loads(subprocess.check_output(
        [sys.executable, __file__, "--home", empty, "cards", "--json"], text=True))
    _check(got == {"ok": True, "cards": []}, f"empty cards {got}")
    got = json.loads(subprocess.check_output(
        [sys.executable, __file__, "--home", empty, "--project", "ghost",
         "timeline", "--run", "r", "--task", "t", "--json"], text=True))
    _check(got == {"ok": True, "events": []}, f"empty timeline {got}")
    _check(_listing(empty) == before == [], f"empty home grew: {_listing(empty)}")
    _check(cards(empty) == [] and timeline(empty, "ghost", "r", "t") == [], "api read created a store")
    _check(_listing(empty) == [], "api read created a file")

    data = os.path.join(home, "ro-read")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "t", "task", title="kept")
    finally:
        con.close()
    transition(home, "ignored", "r", "t", "building", note="hello", data_dir=data)
    db = os.path.join(data, "mk.db")
    hashes = {name: _file_hash(os.path.join(data, name)) for name in ("mk.db", "mk.db-wal", "mk.db-shm")}
    got = timeline(home, "ignored", "r", "t", data)
    _check(any(e["text"] == "hello" for e in got), f"ro timeline {got}")
    after = {name: _file_hash(os.path.join(data, name)) for name in ("mk.db", "mk.db-wal", "mk.db-shm")}
    _check(after == hashes, f"read wrote the store: {hashes} -> {after}")

    old = os.path.join(home, "ro-old")
    con = open_project(home, data_dir=old, version=1, migrations=())
    try:
        add_task(con, "r", "t", "task", title="old")
    finally:
        con.close()
    old_db = os.path.join(old, "mk.db")
    old_hash = _file_hash(old_db)
    try:
        timeline(home, "ignored", "r", "t", old)
    except Fail as e:
        _check(e.code == 2 and "needs migration" in str(e), f"old-store error: {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: an old store was read without a migration error")
    _check(_file_hash(old_db) == old_hash, "reading an old store wrote it")
    _check(not os.path.exists(old_db + "-wal"), "reading an old store created a wal")

    root_old = os.path.join(home, "ro-root-old")
    os.makedirs(root_old)
    bare = sqlite3.connect(os.path.join(root_old, "mk.db"))
    bare.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
    bare.commit()
    bare.close()
    root_hash = _file_hash(os.path.join(root_old, "mk.db"))
    try:
        cards(root_old)
    except Fail as e:
        _check(e.code == 2 and "needs migration" in str(e), f"old root error: {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: an unstamped root store was read without a migration error")
    _check(_file_hash(os.path.join(root_old, "mk.db")) == root_hash, "reading an old root wrote it")
    _check(not os.path.exists(os.path.join(root_old, "mk.db-wal")), "reading an old root created a wal")

    # Y1 R2: a commit sitting in the WAL, not yet checkpointed, must be visible to a read.
    # The reviewer's repro: wal_autocheckpoint=0, one committed insert, then timeline.
    wal = os.path.join(home, "ro-wal")
    con = open_project(home, data_dir=wal)
    try:
        add_task(con, "r", "t", "task", title="initial")
    finally:
        con.close()
    wal_db = os.path.join(wal, "mk.db")
    wcon = sqlite3.connect(wal_db)
    wcon.execute("PRAGMA wal_autocheckpoint=0")
    wcon.execute("INSERT INTO events(run, task, t, chan, kind, text) VALUES(?,?,?,?,?,?)",
                 ("r", "t", "1000", "state", "note", "uncheckpointed_wal_event"))
    wcon.commit()
    _check(os.path.getsize(wal_db + "-wal") > 0, "the repro did not leave a WAL")
    before = {n: _file_hash(wal_db + s) for n, s in (("db", ""), ("wal", "-wal"))}
    shm_existed = os.path.exists(wal_db + "-shm")
    got = timeline(home, "p", "r", "t", wal)
    _check(any(e["text"] == "uncheckpointed_wal_event" for e in got),
           f"timeline missed the uncheckpointed WAL event: {[e['text'] for e in got]}")
    after = {n: _file_hash(wal_db + s) for n, s in (("db", ""), ("wal", "-wal"))}
    # The -shm file is the WAL index, not data: SQLite rewrites it when a reader attaches, so its
    # hash is not stable. The database and the WAL frames themselves must not change, and a read
    # must not create the -shm when the writer did not already have one.
    _check(after == before, f"reading a live WAL changed the data: {before} -> {after}")
    _check(os.path.exists(wal_db + "-shm") == shm_existed, "the read created or removed the -shm")
    wcon.close()

    # A writer holding an uncommitted transaction must not hang or crash the read.
    held = os.path.join(home, "ro-held")
    con = open_project(home, data_dir=held)
    try:
        add_task(con, "r", "t", "task", title="held")
    finally:
        con.close()
    held_db = os.path.join(held, "mk.db")
    holder = sqlite3.connect(held_db, timeout=5.0)
    holder.execute("PRAGMA busy_timeout=5000")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO events(run, task, t, chan, kind, text) VALUES(?,?,?,?,?,?)",
                   ("r", "t", "1000", "state", "note", "not_committed"))
    got = timeline(home, "p", "r", "t", held)
    _check(all(e["text"] != "not_committed" for e in got), "a read saw an uncommitted insert")
    holder.execute("ROLLBACK")
    holder.close()


def _projects_read(home):
    """projects() lists the root store and never creates one."""
    seeded = os.path.join(home, "proj-read")
    root = open_root(seeded)
    try:
        root.execute(
            "INSERT INTO projects(slug, name, repo, data_dir, mode, created, last_activity)"
            " VALUES (?,?,?,?,?,?,?)",
            ("beta", "Beta", "/r/beta", "/d/beta", "local", "t1", "t2"))
        root.execute(
            "INSERT INTO projects(slug, name, repo, data_dir, mode, created, last_activity)"
            " VALUES (?,?,?,?,?,?,?)",
            ("alpha", "Alpha", "/r/alpha", "/d/alpha", "local", "t1", "t2"))
        root.commit()
    finally:
        root.close()
    got = projects(seeded)
    _check([r["slug"] for r in got] == ["alpha", "beta"], f"order {got}")
    _check(all({"slug", "repo", "data_dir"} <= set(r) for r in got), f"columns {got}")
    _check(got[0]["repo"] == "/r/alpha" and got[0]["data_dir"] == "/d/alpha", f"alpha {got[0]}")
    cli = _cli(["projects", "--json", "--home", seeded])
    _check(cli["ok"] is True and [r["slug"] for r in cli["projects"]] == ["alpha", "beta"], f"cli {cli}")

    missing = os.path.join(home, "proj-missing-dir")
    _check(not os.path.exists(missing), "fixture dir already exists")
    _check(projects(missing) == [], "projects() created a missing home")
    cli = _cli(["projects", "--json", "--home", missing])
    _check(cli == {"ok": True, "projects": []}, f"missing cli {cli}")
    _check(not os.path.exists(missing), "projects created a directory")

    bare = os.path.join(home, "proj-no-db")
    os.makedirs(bare)
    _check(projects(bare) == [], "projects() created mk.db")
    cli = _cli(["projects", "--json", "--home", bare])
    _check(cli == {"ok": True, "projects": []}, f"bare cli {cli}")
    _check(not os.path.exists(os.path.join(bare, "mk.db")), "projects created mk.db")


def _run_snapshot(home):
    """run-snapshot() reads the runs list and one run's document off the store (L5).

    The columns win over the run-doc artifact (that is the store's read rule), the artifact's
    file-only keys survive, the newest active run is the default subject, a missing store is an
    empty snapshot that creates nothing, an unknown run is a Fail, and a read never writes.
    """
    data = os.path.join(home, "snap")
    con = open_project(home, data_dir=data)
    try:
        con.execute("INSERT INTO runs(id, plan, branch, status, stage, next, started, ended)"
                    " VALUES ('r-old','p-old','main','done','3','all merged',"
                    "'2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00')")
        con.execute("INSERT INTO runs(id, plan, branch, status, stage, next, started)"
                    " VALUES ('r-a','p-a','mk/a','active','7','merge L5','2026-01-03T00:00:00+00:00')")
        con.execute("INSERT INTO runs(id, plan, branch, status, stage, next, started)"
                    " VALUES ('r-b','p-b','mk/b','active','1','start T1','2026-01-04T00:00:00+00:00')")
        con.commit()  # add_task opens its own transaction
        add_task(con, "r-a", "P1", "phase", title="Phase one")
        add_task(con, "r-a", "T1", "task", title="One", parent="P1",
                 route_json=json.dumps({"agent": "omp", "model": "Space Bunny", "effort": "high"}))
        add_task(con, "r-a", "T2", "task", title="Two", parent="P1", deps="T1")
        add_task(con, "r-b", "B1", "task", title="Bee")
        # the columns mk-state would have written: the kanban column, the published status, a route
        con.execute("UPDATE tasks SET status='ready', file_status='routed', lane='/wt/t1',"
                    " slot='2', branch='mk/a/t1', pane='w1P:pN', round=2, verdict='APPROVE',"
                    " owned='scripts/x.py', acceptance='it works' WHERE run='r-a' AND id='T1'")
        # the run document: feature/waiting/recent are file-only, T2 carries wave + last_commit
        doc = {"id": "r-a", "feature": "Snapshot lane", "plan": ".matrixkoder/plans/p-a.md",
               "plan_id": "p-a", "plan_sha": "abc123", "plan_status": "running", "status": "active",
               "stage": 7, "next": "merge L5", "waiting": {"question": "Merge now?",
               "options": ["yes", "no"], "rec": "yes", "since": "2026-01-03T01:00:00+00:00"},
               "started": "2026-01-03T00:00:00+00:00", "ended": None,
               "updated": "2026-01-03T02:00:00+00:00",
               "recent": [{"t": "2026-01-03T01:30:00+00:00", "run": "r-a", "task": "T1",
                           "type": "note", "text": "gates green", "from": None, "to": None,
                           "by": "lane-watch"}],
               "tasks": [{"id": "T1", "type": "task", "wave": 2, "last_commit": "1de932a",
                          "ready": True, "reviewer": "agy", "created": "2026-01-03T00:05:00+00:00",
                          "rounds": [{"n": 2, "kind": "build", "sha": "1de932a"}]},
                         {"id": "P1", "type": "phase", "wave": 1, "last_commit": None},
                         {"id": "T2", "type": "task", "wave": 2, "status": "building",
                          "last_commit": "beeface"}]}
        con.execute("INSERT INTO artifacts(run, task, round, kind, rev, text, created)"
                    " VALUES ('r-a',NULL,0,'run-doc',1,?,'2026-01-03T02:00:00+00:00')",
                    (json.dumps(doc),))
        con.execute("INSERT INTO artifacts(run, task, round, kind, rev, text, created)"
                    " VALUES ('r-b',NULL,0,'run-doc',1,?,'2026-01-04T01:00:00+00:00')",
                    (json.dumps({"id": "r-b", "feature": "Second lane"}),))
        con.execute("INSERT INTO waits(run, question, opened) VALUES"
                    " ('r-a','Merge now?','2026-01-03T01:00:00+00:00')")
        con.commit()
    finally:
        con.close()

    got = run_snapshot(home, data_dir=data)
    _check(got["ok"] is True, f"snapshot ok flag {got['ok']}")
    _check([r["id"] for r in got["runs"]] == ["r-old", "r-a", "r-b"],
           f"snapshot run order {[r['id'] for r in got['runs']]}")
    _check(got["runs"][1] == {"id": "r-a", "feature": "Snapshot lane", "status": "active",
                             "started": "2026-01-03T00:00:00+00:00", "ended": None, "plan": "p-a"},
           f"snapshot index entry {got['runs'][1]}")
    _check(got["runs"][0]["feature"] is None, "a run with no run-doc invented a feature")
    _check(got["active"] == "r-b", f"active is {got['active']}, newest started wins")
    run = got["run"]
    _check(run["id"] == "r-b" and run["feature"] == "Second lane", f"default subject {run['id']}")
    # `type` is the artifact's when the run document carries one; a task the document does not
    # list falls back to its own column, and an empty column to "task" — never a missing key
    _check(run["tasks"][0].get("type") == "task", f"task type default: {run['tasks'][0]}")

    got = run_snapshot(home, data_dir=data, run="r-a")
    run = got["run"]
    _check(got["active"] == "r-b" and run["id"] == "r-a", f"named run {run['id']}")
    # the run columns win, the artifact supplies the file-only keys
    _check(run["stage"] == 7 and isinstance(run["stage"], int), f"stage is {run['stage']!r}")
    _check(run["next"] == "merge L5" and run["branch"] == "mk/a" and run["status"] == "active",
           f"run columns {run}")
    _check(run["plan"] == ".matrixkoder/plans/p-a.md", f"the plan path lost to the column: {run['plan']}")
    _check(run["plan_id"] == "p-a" and run["plan_sha"] == "abc123", f"plan link {run}")
    _check(run["waiting"]["question"] == "Merge now?" and run["waiting"]["options"] == ["yes", "no"],
           f"waiting {run['waiting']}")
    _check(len(run["recent"]) == 1 and run["recent"][0]["text"] == "gates green", f"recent {run['recent']}")
    _check(run["updated"] == "2026-01-03T02:00:00+00:00", f"updated {run['updated']}")
    # the document's task order, not the table's parent-before-child one
    _check([t["id"] for t in run["tasks"]] == ["T1", "P1", "T2"],
           f"task order {[t['id'] for t in run['tasks']]}")
    t1 = run["tasks"][0]
    _check(t1["title"] == "One" and t1["parent"] == "P1" and t1["type"] == "task", f"T1 {t1}")
    _check(t1["agent"] == "omp" and t1["model"] == "Space Bunny" and t1["effort"] == "high",
           f"T1 route {t1}")
    _check(t1["lane"] == "/wt/t1" and t1["slot"] == "2" and t1["branch"] == "mk/a/t1"
           and t1["pane"] == "w1P:pN", f"T1 lane {t1}")
    _check(t1["owned"] == ["scripts/x.py"] and t1["acceptance"] == "it works", f"T1 owned {t1}")
    _check(t1["round"] == 2 and t1["verdict"] == "APPROVE", f"T1 round/verdict {t1}")
    # the store's `ready` publishes as `routed` plus the ready flag, never as a status
    _check(t1["status"] == "routed" and t1["ready"] is True, f"T1 status {t1['status']}/{t1['ready']}")
    _check(t1["wave"] == 2 and t1["last_commit"] == "1de932a", f"T1 file-only keys {t1}")
    _check(t1["reviewer"] == "agy" and t1["rounds"][0]["sha"] == "1de932a", f"T1 rounds {t1}")
    t2 = run["tasks"][2]
    _check(t2["deps"] == ["T1"] and t2["status"] == "planned" and t2["ready"] is False, f"T2 {t2}")
    _check(t2["last_commit"] == "beeface" and t2["agent"] is None, f"T2 {t2}")
    # the column beat the document's stale `building` above; merged beats it the same way
    con = open_project(home, data_dir=data)
    try:
        con.execute("UPDATE tasks SET status='merged' WHERE run='r-a' AND id='T2'")
        con.commit()
    finally:
        con.close()
    t2 = run_snapshot(home, data_dir=data, run="r-a")["run"]["tasks"][2]
    _check(t2["status"] == "merged", f"the artifact won over the column: {t2['status']}")
    # waits come from the waits table, for the selected run only
    _check(got["waits"] == [{"id": 1, "run": "r-a", "question": "Merge now?",
                             "opened": "2026-01-03T01:00:00+00:00", "cleared": None}],
           f"waits {got['waits']}")
    _check(run_snapshot(home, data_dir=data, run="r-b")["waits"] == [], "another run's waits leaked")

    cli = _cli(["run-snapshot", "--json", "--home", home, "--data", data, "--run", "r-a"])
    _check(cli == run_snapshot(home, data_dir=data, run="r-a"), "cli run-snapshot differs from the api")
    try:
        run_snapshot(home, data_dir=data, run="nope")
    except Fail as e:
        _check(e.code == 2 and "no run" in str(e), f"unknown run error: {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: run-snapshot accepted an unknown run")

    missing = os.path.join(home, "snap-missing")
    _check(not os.path.exists(missing), "fixture dir already exists")
    empty = {"ok": True, "store": False, "runs": [], "active": None, "run": None, "waits": []}
    _check(run_snapshot(home, data_dir=missing) == empty,
           f"a missing store is not an empty snapshot: {run_snapshot(home, data_dir=missing)}")
    _check(_cli(["run-snapshot", "--json", "--home", home, "--data", missing]) == empty,
           "cli run-snapshot on a missing store")
    _check(not os.path.exists(missing), "run-snapshot created a missing data dir")
    try:
        run_snapshot(home, data_dir=missing, run="r-a")
    except Fail as e:
        _check(e.code == 2 and "no store" in str(e), f"missing-store error: {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: run-snapshot named a run in a store that does not exist")
    _check(not os.path.exists(missing), "the refused read created a data dir")

    # a read must not write or migrate: hash the store, read, hash again
    names = ("mk.db", "mk.db-wal", "mk.db-shm")
    before = {n: _file_hash(os.path.join(data, n)) for n in names}
    run_snapshot(home, data_dir=data, run="r-a")
    after = {n: _file_hash(os.path.join(data, n)) for n in names}
    _check(after == before, f"run-snapshot wrote the store: {before} -> {after}")


def _run_events(home):
    """events() is the run-wide feed timeline() cannot serve (L5): id order, chan='state' by
    default, --task and --since as the two selectors wake.ts needs (the events.id cursor).

    meta_json survives (it carries `by`, and a direct transition() leaves it NULL — the reader
    maps that to the orchestrator), a missing store is an empty list, and a read writes nothing.
    """
    data = os.path.join(home, "events")
    con = open_project(home, data_dir=data)
    try:
        con.execute("INSERT INTO runs(id, branch, status, stage, started)"
                    " VALUES ('r','main','active','0','2026-01-01T00:00:00+00:00')")
        con.commit()  # add_task opens its own transaction
        add_task(con, "r", "T1", "task", title="one")
        add_task(con, "r", "T2", "task", title="two")
        transition(home, "demo", "r", "T1", "building", note="picked up", data_dir=data)
        eid = transition(home, "demo", "r", "T2", "blocked", note="needs a decision", data_dir=data)
        # a bus-channel row the default chan must not return, and the --chan '' escape hatch
        # a live row: chan='bus' (the default --chan state must not return it), meta carrying `by`
        append_event(home, "demo", "r", task="T1", chan="bus", kind="info", text="hi",
                     meta={"by": "mk-builder"}, data_dir=data)
    finally:
        con.close()

    got = events(home, "demo", "r", data_dir=data)
    kinds = [(g["task"], g["kind"]) for g in got]
    _check([g["id"] for g in got] == sorted(g["id"] for g in got), f"events not in id order {got}")
    _check(all(g["chan"] == "state" for g in got), f"a bus row leaked into chan=state: {got}")
    _check(kinds[-1] == ("T2", "transition") and got[-1]["id"] == eid,
           f"the newest transition is missing: {kinds}")
    _check(got[-1]["to_id"] == "blocked" and got[-1]["text"] == "needs a decision",
           f"transition fields {got[-1]}")
    # a direct transition() writes no meta_json — a reader maps NULL to "orchestrator", which is
    # exactly how the file view renders such a row. A live event's `by` rides in meta_json, and
    # the bus row below carries it.
    _check(got[-1]["meta_json"] is None, f"a direct transition grew meta_json: {got[-1]}")
    _check(any(g["meta_json"] and "mk-builder" in g["meta_json"]
               for g in events(home, "demo", "r", data_dir=data, chan="")),
           "the live event's meta_json (the `by` a wake reads) did not survive")
    _check(set(EVENT_COLS_READ) <= set(got[-1]), f"reply columns {sorted(got[-1])}")
    # --task narrows, --since is the cursor
    _check([g["task"] for g in events(home, "demo", "r", data_dir=data, task="T1")] == ["T1"],
           "task selector")
    _check([g["id"] for g in events(home, "demo", "r", data_dir=data, since=eid)] == [],
           "since returned a consumed row")
    _check(len(events(home, "demo", "r", data_dir=data, since=got[0]["id"])) == len(got) - 1,
           "since did not return exactly the rows past it")
    # another run's rows never leak
    _check(events(home, "demo", "other", data_dir=data) == [], "another run's events leaked")
    # every channel on request
    every = events(home, "demo", "r", data_dir=data, chan="")
    _check(len(every) == len(got) + 1 and any(g["chan"] == "bus" for g in every),
           f"chan='' did not read every channel: {[g['chan'] for g in every]}")

    cli = _cli(["events", "--json", "--home", home, "--data", data, "--run", "r"])
    _check(cli == {"ok": True, "store": True, "events": got}, f"cli events differs from the api: {cli}")
    missing = os.path.join(home, "events-missing")
    _check(events(home, "demo", "r", data_dir=missing) == [], "a missing store is not an empty feed")
    _check(not os.path.exists(missing), "events created a missing data dir")

    names = ("mk.db", "mk.db-wal", "mk.db-shm")
    before = {n: _file_hash(os.path.join(data, n)) for n in names}
    events(home, "demo", "r", data_dir=data)
    after = {n: _file_hash(os.path.join(data, n)) for n in names}
    _check(after == before, f"events wrote the store: {before} -> {after}")


def _run_snapshot_migrated(home):
    """A reader holding a data dir follows the switch to the C2 store (L5).

    mk-migrate's rollback deliberately addresses the pre-switch store by the data dir's literal
    path, and that copy stays behind in the data dir. A pane holding the data dir must NOT read
    it: the root row's db_path is the live store. mkpaths.project_folder is the rule; this proves
    run_snapshot and events use it and that the stale copy is ignored.
    """
    data = os.path.join(home, "mig", "data")          # the legacy location, post-switch
    slug = "demo"
    c2 = os.path.join(home, "projects", slug)          # the C2 project folder
    os.makedirs(data, exist_ok=True)
    os.makedirs(c2, exist_ok=True)
    live = open_project(home, data_dir=c2)             # write the live store through the C2 folder
    stale = open_project(home, data_dir=data)          # and a DIFFERENT store in the legacy dir
    try:
        live.execute("INSERT INTO runs(id, branch, status, stage, started)"
                     " VALUES ('live','main','active','3','2026-02-02T00:00:00+00:00')")
        live.commit()
        add_task(live, "live", "A1", "task", title="live task")
        stale.execute("INSERT INTO runs(id, branch, status, stage, started)"
                      " VALUES ('stale','main','active','9','2026-02-03T00:00:00+00:00')")
        stale.commit()
        add_task(stale, "stale", "B1", "task", title="stale task")
        # the root row: this project's live store is db_path (L2's switch marker)
        root = open_root(home)
        try:
            root.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path)"
                         " VALUES (?,'demo','',?,'home',?)", (slug, data, os.path.join(c2, "mk.db")))
            root.commit()
        finally:
            root.close()
    finally:
        live.close()
        stale.close()

    got = run_snapshot(home, data_dir=data)
    _check([r["id"] for r in got["runs"]] == ["live"],
           f"the reader followed the stale pre-switch store: {[r['id'] for r in got['runs']]}")
    _check(got["active"] == "live" and got["run"]["stage"] == 3, f"live run {got['active']}")
    _check([t["id"] for t in got["run"]["tasks"]] == ["A1"], f"live tasks {got['run']['tasks']}")
    _check(events(home, "demo", "live", data_dir=data) == [],
           "events read a store with no rows for the live run")
    # --data wins over --project (the existing mkdb rule), and the slug form reaches the same store
    _check(run_snapshot(home, project=slug, data_dir=data) == got,
           "--project and --data disagree about the store")
    _check(_store_path(home, data_dir=data) == os.path.join(c2, "mk.db"),
           f"the store path is {_store_path(home, data_dir=data)}")
    # a pre-migration project (no root row) keeps today's literal <data>/mk.db
    plain = os.path.join(home, "plain")
    plain_db = open_project(home, data_dir=plain)
    try:
        plain_db.execute("INSERT INTO runs(id, branch, status, stage, started)"
                         " VALUES ('p','main','active','1','2026-02-04T00:00:00+00:00')")
        plain_db.commit()
    finally:
        plain_db.close()
    _check(_store_path(home, data_dir=plain) == os.path.join(plain, "mk.db"),
           "a pre-migration project lost its own store path")
    _check(run_snapshot(home, data_dir=plain)["active"] == "p", "the pre-migration read failed")


def _run_snapshot_no_store(home):
    """A project with NO store answers store=false, and the read creates nothing (L5).

    The reply is otherwise identical to a real store that holds no run — an empty runs list, an
    empty feed, exit 0 — and hooks/dashboard.tsx readStoreSnapshot/loadRuns and wake.ts
    activeRunId could not tell the two apart, so their file fallback never ran. `store` is the
    marker: false when the store file is absent, true when it is there (empty or not). The
    events reply carries the same key, and nothing under the data path is created.
    """
    missing = os.path.join(home, "no-store")
    os.makedirs(missing)                       # a real, empty data dir: no mk.db in it
    before = sorted(os.listdir(missing))
    _check(before == [], f"the fresh data dir is not empty: {before}")
    _check(not os.path.exists(os.path.join(missing, "mk.db")), "fixture has a store")

    got = run_snapshot(home, data_dir=missing)
    _check(got == {"ok": True, "store": False, "runs": [], "active": None, "run": None, "waits": []},
           f"the no-store reply is not marked store=false: {got}")
    _check(got["store"] is False and got["ok"] is True and got["runs"] == [], f"no-store keys {got}")
    _check(_cli(["run-snapshot", "--json", "--home", home, "--data", missing]) == got,
           "cli run-snapshot on a missing store")
    # the same marker on the events reply, so wake.ts can tell "no store" from "no event"
    feed = _cli(["events", "--json", "--home", home, "--data", missing, "--run", "r"])
    _check(feed.get("store") is False and feed["events"] == [], f"no-store events reply {feed}")
    # and the read stays read-only: no file, no directory, under the path it was pointed at
    _check(sorted(os.listdir(missing)) == before, f"the no-store read created a file: {before}")
    _check(not os.path.exists(os.path.join(missing, "mk.db")), "the no-store read created a store")


def _run_snapshot_zero_runs(home):
    """A real store that holds no run is store=true — not the no-store answer (L5).

    The two replies differ in exactly one key, so this is what keeps the file fallback honest:
    a project that has a store must never read as one that has none.
    """
    data = os.path.join(home, "zero-runs")
    con = open_project(home, data_dir=data)    # a real store, no rows
    con.close()
    _check(os.path.exists(os.path.join(data, "mk.db")), "the empty store was not created")

    got = run_snapshot(home, data_dir=data)
    _check(got["store"] is True, f"an existing store is not marked store=true: {got}")
    _check(got == {"ok": True, "store": True, "runs": [], "active": None, "run": None, "waits": []},
           f"the zero-run reply is wrong: {got}")
    _check(_cli(["run-snapshot", "--json", "--home", home, "--data", data]) == got,
           "cli run-snapshot on a store with no run")


def _task_type_default(home):
    """A task row with an empty type and no run-doc entry still reads as type 'task' (L5).

    The artifact's `type` is still the spelling of record — this only covers the row that has
    none of its own: without a default the reader published a key-less task and the dashboard's
    task grouping dropped it.
    """
    data = os.path.join(home, "empty-type")
    con = open_project(home, data_dir=data)
    try:
        con.execute("INSERT INTO runs(id, branch, status, stage, started)"
                    " VALUES ('r','main','active','0','2026-03-01T00:00:00+00:00')")
        con.commit()
        # a row written by an older writer: the type column is empty and there is no run-doc
        con.execute("INSERT INTO tasks(run, id, type, title, status, updated)"
                    " VALUES ('r','T1','','legacy row','planned','2026-03-01T00:00:00+00:00')")
        con.commit()
    finally:
        con.close()

    task = run_snapshot(home, data_dir=data)["run"]["tasks"][0]
    _check(task["id"] == "T1" and task["type"] == "task", f"the empty type read as {task}")


def _ready(home):
    """A task whose deps are all merged/done becomes ready; a regress demotes it; the move is
    one transaction with no event and no `updated` rewrite; the scan stays inside the run."""
    data = os.path.join(home, "ready")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "rdy-a", "task", title="a")
        add_task(con, "r", "rdy-b", "task", title="b", deps="rdy-a")
        add_task(con, "r", "nodep", "task", title="no deps")
        add_task(con, "r", "d1", "task")
        add_task(con, "r", "d2", "task")
        add_task(con, "r", "diamond", "task", deps="d1,d2")
        add_task(con, "other", "ext", "task")
        add_task(con, "r", "cross", "task", deps="other:ext")
        add_task(con, "r", "started", "task", deps="rdy-a", status="building")
        # an unrelated OLD run whose dep happens to be met: a transition in r must not touch it
        add_task(con, "old", "gone", "task", status="done")
        add_task(con, "old", "waiter", "task", deps="gone")
        add_task(con, "old", "far", "task", deps="other:ext")
        # a met cross-run dep whose run id is a substring of another run id (`r` inside nothing,
        # but `other:ext` contains the letters of a short run). Run id `r` must not match it.
        add_task(con, "old", "w", "task", deps="other:ext")
        add_task(con, "r", "routed", "task", deps="rdy-a", status="routed")
    finally:
        con.close()

    def row(run, task, col="status"):
        c = open_project(home, data_dir=data)
        try:
            return c.execute(f"SELECT {col} FROM tasks WHERE run=? AND id=?", (run, task)).fetchone()[0]
        finally:
            c.close()

    def counts():
        c = open_project(home, data_dir=data)
        try:
            return (c.execute("SELECT COUNT(*) c FROM change_log").fetchone()["c"],
                    c.execute("SELECT COUNT(*) c FROM events").fetchone()["c"])
        finally:
            c.close()

    _check(row("r", "rdy-b") == "planned", "rdy-b not planned before its dep is done")
    stamped = row("r", "rdy-b", "updated")
    transition(home, "matrixkoder", "r", "rdy-a", "done", data_dir=data)
    _check(row("r", "rdy-b") == "ready", f"rdy-b is {row('r', 'rdy-b')} after rdy-a is done")
    _check(row("r", "rdy-b", "updated") == stamped, "the promotion moved tasks.updated")
    _check(row("r", "nodep") == "planned", "a task with no deps moved")
    _check(row("r", "started") == "building", "a started task was reset")
    _check(row("r", "diamond") == "planned", "diamond ready after one dep")
    transition(home, "matrixkoder", "r", "d1", "done", data_dir=data)
    _check(row("r", "diamond") == "planned", "diamond ready after only d1")
    transition(home, "matrixkoder", "r", "d2", "merged", data_dir=data)
    _check(row("r", "diamond") == "ready", "diamond not ready after both deps")
    _check(row("r", "cross") == "planned", "cross-run dep counted before it was done")
    _check(row("old", "waiter") == "planned" and row("old", "far") == "planned",
           "a transition in run r promoted tasks in the unrelated old run")
    _check(row("old", "w") == "planned", "run r matched the dep other:ext by substring")
    _check(row("r", "routed") == "ready", "a routed task whose dep is met did not become ready")
    _check(row("r", "routed", "file_status") == "routed",
           "promoting a routed task forgot the published status")
    transition(home, "matrixkoder", "other", "ext", "done", data_dir=data)
    _check(row("r", "cross") == "ready", "cross-run dep did not promote")
    _check(row("old", "w") == "ready", "a met cross-run dep did not promote old:w")
    _check(row("old", "far") == "ready", "a met cross-run dep did not promote old:far")
    # no promotion wrote a transition event: the file view's "deps met" note is the one event
    c = open_project(home, data_dir=data)
    try:
        n = c.execute("SELECT COUNT(*) c FROM events WHERE kind='transition' AND to_id='ready'").fetchone()["c"]
    finally:
        c.close()
    _check(n == 0, f"a promotion wrote {n} transition events")
    # idempotent: a second pass moves nothing, writes no change_log row and no event
    before = counts()
    moved = promote_ready(home, "matrixkoder", "r", data_dir=data)
    _check(moved == [], f"second pass moved {moved}")
    _check(counts() == before, "second pass wrote a change_log row or an event")
    # a dep that regresses demotes the dependent (file mode flips the ready flag the same way)
    stamped_b = row("r", "rdy-b", "updated")
    transition(home, "matrixkoder", "r", "rdy-a", "fix", data_dir=data)
    _check(row("r", "rdy-b") == "planned", f"a regressed dep left rdy-b {row('r', 'rdy-b')}")
    _check(row("r", "routed") == "routed", f"a regressed dep left the routed task {row('r', 'routed')}")
    _check(row("r", "rdy-b", "updated") == stamped_b, "the demotion moved tasks.updated")
    # back again: done -> ready, so the pair is reversible
    transition(home, "matrixkoder", "r", "rdy-a", "done", data_dir=data)
    _check(row("r", "rdy-b") == "ready", "a re-met dep did not re-promote")


def _single_writer():
    """No script but mkdb.py may open SQLite read-write (research §8)."""
    offenders = []
    here = os.path.dirname(os.path.abspath(__file__))
    for name in sorted(os.listdir(here)):
        if not name.endswith(".py") or name == "mkdb.py":
            continue
        with open(os.path.join(here, name)) as fh:
            if any("sqlite3.connect" in line and "mode=ro" not in line for line in fh):
                offenders.append(name)
    _check(offenders == [], f"read-write sqlite outside mkdb.py: {offenders}")


# failure injection for the crash tests — env so the worker processes see it too
_real_upsert, _real_change_log = _upsert_card, _change_log


def _guarded_upsert(*a, **k):
    if os.environ.get("MKDB_FAIL_BEFORE_ROOT"):
        raise Fail("injected failure before the root upsert")
    return _real_upsert(*a, **k)


def _guarded_change_log(con, run, task):
    if os.environ.get("MKDB_FAIL_IN_TXN"):
        raise Fail("injected failure inside the project transaction")
    return _real_change_log(con, run, task)


_upsert_card, _change_log = _guarded_upsert, _guarded_change_log


if __name__ == "__main__":
    try:
        sys.exit(self_test() if sys.argv[1:] == ["--self-test"] else main(sys.argv[1:]))
    except Fail as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(e.code)
