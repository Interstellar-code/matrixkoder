#!/usr/bin/env python3
"""MatrixKoder SQLite stores — the ONLY module that opens a store read-write. Python 3 stdlib only.

  timeline --project P --run R --task T --json
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

Two stores. The root store <home>/mk.db holds the projects linkage and plan_summary (C1, C3).
The project store <data>/mk.db (or <home>/projects/<slug>/mk.db when only --project is given)
holds that project's plans, runs, tasks, events and change_log (C2). The project store is the
truth: transition() is the only status writer, and the root plan_summary is a projection written
afterwards. L7.3 dropped the old root copies (`cards`, `project_summary`, `sync`) from every read
and write; root schema 3 (L10.9) drops the empty ones and a fresh root never creates them.

Schema: docs/state-schema.md (store section).
"""
import argparse, contextlib, datetime, hashlib, importlib.util, json, multiprocessing, os, re, shutil, sqlite3, subprocess, sys, tempfile

sys.dont_write_bytecode = True

SCHEMA = 7
ROOT_SCHEMA = 3  # the root store migrates additively; project stores go to SCHEMA
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
           "verifying": "Verify", "review": "Review", "fix": "Fix", "rework": "Fix",
           "merged": "Done", "qa": "Done", "done": "Done"}
ATTENTION = ("rework", "blocked", "stalled", "quota", "provider")
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
CREATE TABLE IF NOT EXISTS evidence(
  id INTEGER PRIMARY KEY, run TEXT, task TEXT, line TEXT, gate TEXT, result TEXT,
  tree_sha TEXT, by TEXT, ref TEXT, output_tail TEXT, output_sha256 TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS risk_reasons(
  id INTEGER PRIMARY KEY, run TEXT, task TEXT, kind TEXT, points INTEGER,
  source TEXT, opened TEXT, cleared TEXT, cleared_by TEXT);
CREATE TABLE IF NOT EXISTS reconcile_due(
  id INTEGER PRIMARY KEY, run TEXT, scope TEXT, created TEXT, done TEXT);
CREATE INDEX IF NOT EXISTS evidence_run_task ON evidence(run, task);
CREATE INDEX IF NOT EXISTS risk_reasons_run_task ON risk_reasons(run, task);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY, value TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS watch_cursors(
  key TEXT PRIMARY KEY, seq INTEGER NOT NULL, started TEXT, updated TEXT);
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
    if rp == _mkpaths().root_db_in(real) or rp.startswith(real + os.sep):
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


def project_dir(home, slug=None, data_dir=None, legacy=False):
    """--data wins; else <home>/projects/<slug> — the C2 folder for a migrated project (C1, L3).

    The slug is checked before the path exists. Both routes pass check_home before
    anything is created: a synced or networked home, and a --data dir that itself
    looks synced, are refused (C11). The path RULE (migrated → C2 target, else
    today's answer) is mkpaths.project_dir's; mkdb keeps the refusals.
    """
    if data_dir:
        path = _mkpaths().project_dir(home_arg=home, data_dir=data_dir, legacy=legacy)
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
    _selftest_write_guard(_mkpaths().root_db_in(home))
    return _migrate(_connect(_mkpaths().root_db_in(home)), version,
                    ((1, ROOT_DDL), (2, _root_v2), (3, _root_v3)) + tuple(migrations))


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



def _root_v3(con):
    """The shipped root v3 step (L10.9): drop cards, project_summary and sync, the pre-linkage
    copies of the project store. Additive in reverse: a table is dropped only when it holds no
    row, so a root that still has data in one keeps it (root-cleanup lists it as `keep`).
    Safe to run twice: DROP TABLE IF EXISTS."""
    for table in ROOT_DROPPED_TABLES:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists and con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0:
            con.execute(f"DROP TABLE {table}")


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

def _v6(con):
    """The shipped v6 step (F.1, phase F): evidence, risk_reasons, reconcile_due tables + indexes.

    Additive only — creates three empty tables and two indexes. No existing table, column or
    row changes; a v5 store keeps every row it had and gains the new tables. Safe to run twice:
    CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS are idempotent.
    """
    con.execute("""CREATE TABLE IF NOT EXISTS evidence(
      id INTEGER PRIMARY KEY, run TEXT, task TEXT, line TEXT, gate TEXT, result TEXT,
      tree_sha TEXT, by TEXT, ref TEXT, output_tail TEXT, output_sha256 TEXT, created TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS risk_reasons(
      id INTEGER PRIMARY KEY, run TEXT, task TEXT, kind TEXT, points INTEGER,
      source TEXT, opened TEXT, cleared TEXT, cleared_by TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS reconcile_due(
      id INTEGER PRIMARY KEY, run TEXT, scope TEXT, created TEXT, done TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS evidence_run_task ON evidence(run, task)")
    con.execute("CREATE INDEX IF NOT EXISTS risk_reasons_run_task ON risk_reasons(run, task)")


def _v7(con):
    """The v7 step (L10.2): the project `settings` table (key -> JSON value, a reset is a DELETE) and
    the `watch_cursors` table (the bus watch cursors, keyed `<run or *>|<task or *>`). Additive only
    and safe to run twice: two CREATE TABLE IF NOT EXISTS, no existing table, column or row changes.
    Nothing reads or writes them yet (L10.3 and L10.5 do); `lane_slots` stays the lane registry.
    """
    con.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS watch_cursors("
                "key TEXT PRIMARY KEY, seq INTEGER NOT NULL, started TEXT, updated TEXT)")


def open_project(home, slug=None, data_dir=None, version=SCHEMA, migrations=(), legacy=False):
    """Open or create the project store (C2). check_home runs before any directory is created.
    `legacy=True` opens the literal --data store of a migrated project (rollback fixtures only)."""
    path = _mkpaths().project_db_in(project_dir(home, slug, data_dir, legacy))
    _selftest_write_guard(path)
    con = _migrate(_connect(path), version, ((1, PROJECT_DDL), (2, _v2), (3, _v3), (4, _v4), (5, _v5), (6, _v6), (7, _v7)) + tuple(migrations))
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

    `rework` is special: it always maps to Fix (it is in ATTENTION but has its own column).
    """
    if status == "rework":
        return "Fix", 1
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


def _rollup(con, run, task):
    """The status a parent should hold, or None when it has no children.

    Any child in an attention status wins, first match in ATTENTION order (rework, blocked,
    stalled, quota, provider): the parent takes that status, and transition() then keeps the
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
    dep, so a second pass moves nothing. L7.3: the root card is gone, so the pass no longer
    repairs one; each move still refreshes the root plan_summary through transition().
    `sync_root` is kept for callers and drives nothing here any more. -> ids moved."""
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
    finally:
        con.close()
    moved = []
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
            if to == "ready":
                moved.append(r["id"])
    return moved


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

    Same contract as the card write it used to ride with (C5): the rows land with the commit
    that triggered them (or in the caller's own transaction when sync_root is off), so a kill
    before it leaves the previous rows and catch_up rebuilds them. counts_json is rewritten
    whole; no column is merged, because the row is a projection of the project store, never an
    independent fact.
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


def transition(home, project, run, task, to, note="", round=0, sync_root=False, data_dir=None, verdict=_UNSET,
                 event=True, keep_updated=False, file_status=_UNSET):
    """The ONLY status writer (C10). One project transaction, then the root plan_summary (C3).

    verdict defaults to leaving the task's stored verdict unchanged. Pass a string or None to
    replace it; L7.3 removed the root card that used to copy it.
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
        # L7.3: the root card and its sync watermark are gone. Each commit now refreshes the
        # root plan_summary (C3), so the root's plan numbers can never lag the status this
        # transition just committed. sync_root is kept for callers and drives nothing here.
        plans = _plan_summary_values(con)
        if plans:
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


def catch_up(home, project, data_dir=None):
    """L7.3: heal the root plan_summary against the project store, and nothing else.

    The root cards and the sync watermark are gone, so there is no change_log replay any more.
    What can still lag is the C3 plan_summary batch: a kill between the project commit and the
    root write (the MKDB_FAIL_BEFORE_ROOT window) leaves it stale. This recomputes the rows and
    writes only when a row is missing or differs, so a second call over a synced store writes
    nothing and returns 0. Returns the number of plan rows written.
    """
    con = open_project(home, project, data_dir)
    try:
        plans = _plan_summary_values(con)
    finally:
        con.close()
    if not plans:
        return 0
    root = open_root(home)
    try:
        have = {r["plan"]: (r["title"], r["status"], r["counts_json"], r["live_run"]) for r in
                root.execute("SELECT plan, title, status, counts_json, live_run"
                             " FROM plan_summary WHERE project=?", (project,))}
    finally:
        root.close()
    want = {p: (title, status, counts, live) for p, title, status, counts, live in plans}
    if have == want:
        return 0
    _write_plan_summaries_root(home, project, plans)
    return len(want)


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


def import_history(home, project, run, *, run_row=None, tasks=(), events=(), artifacts=(), data_dir=None,
                   sync_root=True):
    """Import one historical run with its original values (D1B). The only history writer.

    One BEGIN IMMEDIATE transaction for the whole call; any error rolls it back, so the store is
    unchanged. Idempotent: events and artifacts carry a stable source key (meta_json.src, and the
    artifacts.src column) and a repeat imports nothing. Tasks and the run row upsert. No card
    is written here — L7.3 removed the root cards, so nothing reads change_log for a replay.
    C3: the plan_summary rows ARE refreshed after the commit, in their own root transaction,
    because an import moves task statuses (and a run row, which live_run reads); a store with
    no plans row writes nothing. Returns counts of rows actually added.

    Z9: `sync_root=False` skips ONLY that root plan_summary refresh — the import itself is
    identical. Every other caller keeps the default, so behaviour is unchanged for it. mk-state
    passes it for a throwaway temp data dir on the DEFAULT home (Store._skip_root_projection),
    which must not leave a plan_summary row in the user's real ~/.matrixkoder.
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
    if sync_root:
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

    A missing store is an empty timeline: no file and no directory is created. L5: the reader
    opens the store _store_path resolves, so a migrated project's events are read (Z6).
    """
    path = _store_path(home, project, data_dir)
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


def projects(home):
    """Read the root store's projects, ordered by slug.

    A missing root store is an empty list: <home>/mk.db is not created.
    """
    check_home(home)
    root = _open_read(_mkpaths().root_db_in(home), ROOT_SCHEMA)
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

    Read-only like projects()/plans(): mode=ro, never migrates, never writes. A missing root
    store is an empty overview, not an error, and <home>/mk.db is not created. counts_json
    arrives as a parsed object ({} when NULL or unparsable), never as a JSON string.
    """
    check_home(home)
    root = _open_read(_mkpaths().root_db_in(home), ROOT_SCHEMA)
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

def _task_extra(con, run, task_id):
    """The parsed extra_json dict for one task, or {} when absent/unparsable."""
    row = con.execute("SELECT extra_json FROM tasks WHERE run=? AND id=?", (run, task_id)).fetchone()
    if not row or not row["extra_json"]:
        return {}
    try:
        got = json.loads(row["extra_json"])
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _open_reasons(con, run, task_id):
    """The task's OPEN risk_reasons as [{kind, points, source, opened}], ordered by id.

    points is coerced to a non-negative int (non-int, bool, or negative → 0).
    NULL source/opened → "" (F.8 drops non-string reasons).
    """
    rows = con.execute(
        "SELECT kind, points, source, opened FROM risk_reasons"
        " WHERE run=? AND task=? AND cleared IS NULL ORDER BY id",
        (run, task_id)).fetchall()
    out = []
    for r in rows:
        pts = r["points"]
        if not isinstance(pts, int) or isinstance(pts, bool) or pts < 0:
            pts = 0
        out.append({"kind": r["kind"], "points": pts,
                    "source": r["source"] or "", "opened": r["opened"] or ""})
    return out


def _task_risk(con, run, task_id):
    """Derived risk = min(5, base_risk + sum(points of OPEN risk_reasons)).

    Read-only: never stored, never settable by task set. base_risk defaults to 1.
    Returns an int in [1, 5].
    """
    return _risk_of(_task_extra(con, run, task_id), _open_reasons(con, run, task_id))


def _risk_of(extra, reasons):
    """min(5, base_risk + OPEN points) from an already-read extra dict and reasons list."""
    base = extra.get("base_risk", 1)
    if not isinstance(base, int) or isinstance(base, bool):
        base = 1
    base = max(1, min(5, base))
    return max(1, min(5, base + sum(r["points"] for r in reasons)))


def _phases_from_run(rows, header, run, con):
    """Phases with their tasks from one run's task rows, in row order (C5).

    A phase row is a type='phase' row or any root (no parent): run init writes the plan's
    phases as parentless task rows. Children of a phase are its tasks; deeper children
    (type='subtask' or any row under a task) nest under their parent task as subtasks.
    `wave` has no run column; it is cross-filled from header_json.phases by id.
    V1.5.1: every phase-level node carries the routing keys too — a round's lane tasks sit
    at phase level (root rows the live mirror may type 'phase' or 'task'), and their slot/
    pane/branch/route live on the row itself, null-defaulted when the row carries none.
    Z13: the ATTACH pass does not assume a parent row precedes its children. rowid order only
    holds at insert time (add_task, import_history's _parents_first); a re-parent (`task set
    <id> parent=<later row>`) points a child at a row with a HIGHER rowid, so grouping in one
    pass dropped such a task from its phase. Pass 1 builds every node, pass 2 attaches it,
    which keeps today's order: phases and tasks stay in rowid order.
    """
    hwave = {str(p.get("id")): p.get("wave") for p in header.get("phases") or []
             if isinstance(p, dict) and p.get("id")}
    ids = {r["id"] for r in rows}
    roots = {r["id"] for r in rows if not r["parent"] or r["parent"] not in ids}
    phases, nodes = {}, {}
    for r in rows:  # pass 1: one node per row; a root row is a phase, everything else a child
        if r["id"] in roots:
            ph = {"id": r["id"], "title": r["title"], "wave": hwave.get(r["id"]),
                  "deps": _deps_of(r["deps"]), "status": r["status"]}
            ph.update(_routing_of(r))
            ph["tasks"] = []
            phases[r["id"]] = ph
        else:
            nodes[r["id"]] = {"id": r["id"], "title": r["title"], "status": r["status"],
                              "parent": r["parent"], "type": r["type"], "round": r["round"],
                              "verdict": r["verdict"], **_routing_of(r), "subtasks": []}
            # F.1: risk fields from extra_json + risk_reasons
            extra = _task_extra(con, run, r["id"])
            reasons = _open_reasons(con, run, r["id"])
            nodes[r["id"]]["risk"] = _risk_of(extra, reasons)
            nodes[r["id"]]["critical"] = extra.get("critical") is True
            nodes[r["id"]]["open_reasons"] = reasons
    for r in rows:  # pass 2: attach in rowid order — the parent node already exists, any depth
        node = nodes.get(r["id"])
        if node is None:
            continue
        if r["parent"] in phases:
            phases[r["parent"]]["tasks"].append(node)
        else:
            parent = nodes.get(r["parent"])
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
                    "deps": list(d.get("deps") or []), "route": None, "subtasks": [],
                    "risk": 1, "critical": False, "open_reasons": []}

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
            phases = _phases_from_run(rows, header, newest, con)
    elif detail:
        phases = _phases_from_header(header)
    obj["counts"] = counts
    if detail:
        obj["phases"] = phases
    return obj


def plans(home, project=None, data_dir=None, plan_id=None):
    """Read the project store's plans (C5, C8). Read-only: mode=ro, never migrates, never writes.

    A missing store is an empty list, like timeline(). An unknown plan id is a Fail (exit 2),
    the same error shape as the other subcommands. L5: the store _store_path resolves is the
    one read, so a migrated project's plans are the C2 store's (Z6).
    """
    path = _store_path(home, project, data_dir)
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


def _extra_dict(text):
    """A parsed extra_json column as a dict; NULL, bad JSON or a non-object is {}."""
    try:
        got = json.loads(text or "")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _run_doc(con, run):
    """The run document of run as a dict: the newest run-doc artifact (pre-migration stores)
    overlaid with runs.extra_json and each tasks.extra_json (what mk-state writes now).

    mk-state stopped writing the artifact (bf55ee0): the file-only keys live in the extra_json
    columns, run keys on the run row and task keys on the task rows, task order in `task_order`.
    mk-state's own load_store reads them the same way; the columns are overlaid later, by
    _run_obj and _task_obj. A run with neither (a plain mkdb writer, an imported row set) is {}.
    """
    row = con.execute(
        "SELECT text FROM artifacts WHERE run=? AND kind=? AND task IS NULL ORDER BY id DESC LIMIT 1",
        (run, RUN_DOC)).fetchone()
    doc = _extra_dict(row["text"]) if row is not None else {}
    rrow = con.execute("SELECT extra_json FROM runs WHERE id=?", (run,)).fetchone()
    run_x = _extra_dict(rrow["extra_json"]) if rrow else {}
    task_x = {r["id"]: x for r in con.execute("SELECT id, extra_json FROM tasks WHERE run=?", (run,))
              if (x := _extra_dict(r["extra_json"]))}
    if not run_x and not task_x:
        return doc
    doc.update(run_x)
    tasks = {t["id"]: t for t in doc.get("tasks") or [] if isinstance(t, dict) and t.get("id")}
    order = [i for i in doc.pop("task_order", None) or [] if isinstance(i, str)]
    order += [i for i in tasks if i not in order]  # the artifact's own order, when task_order is absent
    order += [i for i in task_x if i not in order]
    doc["tasks"] = [{**tasks.get(i, {}), **task_x.get(i, {}), "id": i} for i in order]
    return doc


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
    """The store a READER opens for this project (L5, Z6). mkpaths.project_folder's rule: a
    migrated project's store is the C2 `<home>/projects/<slug>/mk.db` its root row points at,
    a pre-migration project's is `<data>/mk.db`. The DATA-literal join is not a reader rule:
    only writers keep it — `open_project` migrates whatever `--data` names, and mk-migrate's
    rollback addresses the legacy store through `mkpaths.project_db(data=…)` on purpose (it
    must read the pre-switch copy). A pane holding a data dir must follow the switch instead,
    or it would read the pre-switch copy that is still sitting in the data dir. Never creates
    anything."""
    folder = (_mkpaths().project_folder(data_dir, home_arg=home) if data_dir
              else project_dir(home, project))
    return _mkpaths().project_db_in(folder)


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


# --- L10.9 root cleanup: dry-run list, backup, apply, restore -----------------------------------
# The root home carries tables and files that later layouts replaced. `root-cleanup` lists them
# (the default, writes nothing); `--apply --backup-dir D` copies every item into
# D/root-cleanup-<stamp>/ with a sha256 manifest, verifies the copy, then removes exactly the
# listed items. `restore --from <backup>` puts the root DB and every removed file back.
ROOT_DROPPED_TABLES = ("cards", "project_summary", "sync")  # emptied by _root_v3, never created
ROOT_CLEANUP_TABLES = ROOT_DROPPED_TABLES + ("event_src",)  # event_src leaked into one real root
CLEANUP_BUS_FILES = ("bus-ensure.lock", "bus.lock", "bus-tokens.json", "bus-tokens.lock",
                     "ring-queue.json")  # pre-L6.1 home-root names; the bus/ folder replaced them
CLEANUP_MANIFEST = "manifest.json"
_DERIVED_SLUG_RE = re.compile(r"^p[0-9a-f]{16}$")  # a slug derived from a path, not a name


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_files(path):
    """Every file under path (sorted, relative), plus every dir (relative, '' = path itself)."""
    files, dirs = [], [""]
    for cur, dnames, fnames in os.walk(path):
        rel = os.path.relpath(cur, path)
        rel = "" if rel == "." else rel
        for d in dnames:
            dirs.append(os.path.join(rel, d))
        for f in fnames:
            files.append(os.path.join(rel, f))
    return sorted(files), sorted(dirs)


def _token_slugs(home):
    """The project slugs that bus tokens carry (slugs only, never a token), from bus/ and the
    pre-L6.1 home-root store."""
    slugs = {}
    for path in (os.path.join(home, "bus", "bus-tokens.json"), os.path.join(home, "bus-tokens.json")):
        try:
            with open(path) as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        entries = doc.items() if isinstance(doc, dict) else enumerate(doc) if isinstance(doc, list) else ()
        for key, e in entries:
            if isinstance(e, dict) and isinstance(e.get("project"), str):
                slugs.setdefault(e["project"], set()).add(e.get("token") or key)  # a token once, not per file
    return {slug: len(toks) for slug, toks in slugs.items()}


def _old_bus_alive(home):
    """True when a pre-L6.1 bus.json at the home root names a live pid: that bus still reads the
    old token and lane files, so none of them may go."""
    try:
        with open(os.path.join(home, "bus.json")) as fh:
            pid = (json.load(fh) or {}).get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, AttributeError):
        return False


def _flock_free(path):
    """True when nobody holds the flock on path (it is then an unused lock file)."""
    import fcntl
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _cleanup_projects(con, home, remove, merge, keep):
    """List the junk and alias rows of `projects`. Alias rows (same data dir or repo as another
    row) with a bus token are `merge`: kept, the token slug stays. Without a token and without a
    plan_summary row they are `remove`."""
    rows = [dict(r) for r in con.execute("SELECT * FROM projects ORDER BY slug")]
    if not rows:
        return
    named = {r[0] for r in con.execute("SELECT DISTINCT project FROM plan_summary")} \
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='plan_summary'").fetchone() else set()
    tokens = _token_slugs(home)
    groups = {}
    for r in rows:
        for key in (("data_dir", r.get("data_dir")), ("repo", r.get("repo"))):
            if key[1]:
                groups.setdefault(key, []).append(r["slug"])
    alias_of = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        by_slug = {r["slug"]: r for r in rows}
        canon = next((s for s in members if by_slug[s].get("repo") and not _DERIVED_SLUG_RE.match(s)),
                     members[0])
        for s in members:
            if s != canon:
                alias_of.setdefault(s, canon)
    for r in rows:
        slug = r["slug"]
        repo, data_dir = r.get("repo"), r.get("data_dir")
        if slug in alias_of:
            n = tokens.get(slug, 0)
            if n:
                merge.append({"kind": "projects-row", "target": slug, "canonical": alias_of[slug],
                              "reason": f"alias row: {n} bus token(s) carry this slug; keep it or "
                                        "drain and re-mint, never drop", "rows": 1})
            elif slug in named:
                keep.append({"kind": "projects-row", "target": slug, "rows": 1,
                             "reason": "alias row but a plan_summary row names it"})
            else:
                remove.append({"kind": "projects-row", "target": slug, "rows": 1,
                               "reason": f"alias of {alias_of[slug]}, no bus token carries it"})
            continue
        gone = not (repo and os.path.isdir(repo)) and not (data_dir and os.path.isdir(data_dir))
        if gone and r.get("db_path") is None and slug not in named and slug not in tokens:
            remove.append({"kind": "projects-row", "target": slug, "rows": 1,
                           "reason": "junk row: repo and data dir are gone, no db_path, "
                                     "no plan_summary row, no bus token"})


def _cleanup_files(home, remove, keep):
    bus_dir = os.path.join(home, "bus")
    old_alive = _old_bus_alive(home)

    def item(kind, rel, reason):
        full = os.path.join(home, rel)
        if os.path.isdir(full):
            files, _ = _tree_files(full)
            return {"kind": kind, "target": rel, "reason": reason, "rows": len(files)}
        return {"kind": kind, "target": rel, "reason": reason, "rows": 1,
                "bytes": os.path.getsize(full)}

    # old per-lane token files
    old_lanes = os.path.join(home, "bus-lanes")
    if os.path.isdir(old_lanes):
        files, _ = _tree_files(old_lanes)
        new_lanes = os.path.join(bus_dir, "lanes")
        missing = [f for f in files if not os.path.isfile(os.path.join(new_lanes, f))
                   or _sha256_file(os.path.join(new_lanes, f)) != _sha256_file(os.path.join(old_lanes, f))]
        if old_alive:
            keep.append({"kind": "dir", "target": "bus-lanes", "rows": len(files),
                         "reason": "keep: the pre-L6.1 bus is still running"})
        elif missing:
            keep.append({"kind": "dir", "target": "bus-lanes", "rows": len(files),
                         "reason": f"keep: bus/lanes holds {len(files) - len(missing)} of "
                                   f"{len(files)} lane files"})
        else:
            remove.append(item("dir", "bus-lanes", "bus/lanes holds the same lane files"))
    # .mk-home dirs: the home's own and one level into projects/<slug>/
    cands = [".mk-home"]
    pdir = os.path.join(home, "projects")
    if os.path.isdir(pdir):
        cands += [os.path.join("projects", d, ".mk-home") for d in sorted(os.listdir(pdir))]
    for rel in cands:
        full = os.path.join(home, rel)
        if os.path.isdir(full) and not os.path.islink(full):
            files, _ = _tree_files(full)
            if files:
                keep.append({"kind": "dir", "target": rel, "rows": len(files),
                             "reason": "keep: .mk-home holds files"})
            else:
                remove.append(item("dir", rel, "empty .mk-home dir"))
    # runs/.lock and an empty runs/
    runs = os.path.join(home, "runs")
    lock = os.path.join(runs, ".lock")
    if os.path.isdir(runs):
        left = [n for n in os.listdir(runs) if n != ".lock"]
        if os.path.isfile(lock) and os.path.getsize(lock) == 0 and _flock_free(lock):
            remove.append(item("file", os.path.join("runs", ".lock"), "unused empty flock file"))
            if not left:
                remove.append({"kind": "dir", "target": "runs", "rows": 0,
                               "reason": "empty after runs/.lock goes"})
        elif os.path.isfile(lock):
            keep.append({"kind": "file", "target": os.path.join("runs", ".lock"), "rows": 1,
                         "reason": "keep: not empty or a process holds the lock"})
        if not os.path.isfile(lock) and not left:
            remove.append({"kind": "dir", "target": "runs", "rows": 0, "reason": "empty runs dir"})
    # pre-L6.1 bus files at the home root
    new_tokens = None
    for name in CLEANUP_BUS_FILES:
        old = os.path.join(home, name)
        if not os.path.isfile(old):
            continue
        new = os.path.join(bus_dir, name)
        if old_alive:
            keep.append({"kind": "file", "target": name, "rows": 1,
                         "reason": "keep: the pre-L6.1 bus is still running"})
        elif not os.path.isfile(new):
            keep.append({"kind": "file", "target": name, "rows": 1,
                         "reason": f"keep: bus/{name} does not exist"})
        elif name == "bus-tokens.json":
            try:
                with open(old) as fh:
                    o = json.load(fh)
                with open(new) as fh:
                    new_tokens = json.load(fh)
                lost = [t for t in o if t not in new_tokens] if isinstance(o, dict) else ["?"]
            except (OSError, ValueError):
                lost = ["?"]
            if lost:
                keep.append({"kind": "file", "target": name, "rows": 1,
                             "reason": f"keep: {len(lost)} token(s) are only in the old file"})
            else:
                remove.append(item("file", name, "every token is in bus/bus-tokens.json"))
        else:
            remove.append(item("file", name, f"replaced by bus/{name}"))
    if os.path.isfile(os.path.join(home, "config.json")):
        keep.append({"kind": "file", "target": "config.json", "rows": 1,
                     "reason": "keep: waiting for L10.3 migrate-settings"})


def root_cleanup_plan(home):
    """The dry-run list: {remove, merge, keep, counts}. Read-only: the root DB opens mode=ro and
    no file is created or changed."""
    check_home(home)
    remove, merge, keep = [], [], []
    db = _mkpaths().root_db_in(home)
    if os.path.isfile(db):
        con = _connect_ro(db)
        try:
            for t in ROOT_CLEANUP_TABLES:
                if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                   (t,)).fetchone():
                    continue
                n = con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                if n:
                    keep.append({"kind": "table", "target": t, "rows": n, "reason": "keep: has rows"})
                else:
                    remove.append({"kind": "table", "target": t, "rows": 0,
                                   "reason": "empty table nothing reads or writes"})
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='projects'").fetchone():
                _cleanup_projects(con, home, remove, merge, keep)
        finally:
            con.close()
    _cleanup_files(home, remove, keep)
    counts = {}
    for it in remove:
        counts[it["reason"]] = counts.get(it["reason"], 0) + 1
    return {"ok": True, "home": home, "remove": remove, "merge": merge, "keep": keep, "counts": counts}


def _backup_db(src, dst):
    """Copy a SQLite file with the backup API (consistent even with a WAL), read-only source."""
    ro = _connect_ro(src)
    try:
        out = sqlite3.connect(dst)
        try:
            ro.backup(out)
        finally:
            out.close()
    finally:
        ro.close()


def _copy_into(home, rel, bdir, manifest):
    """Copy one removal target (file or dir tree) into bdir/files/<rel>; record each file."""
    full = os.path.join(home, rel)
    if os.path.isdir(full):
        files, dirs = _tree_files(full)
        for d in dirs:
            os.makedirs(os.path.join(bdir, "files", rel, d), exist_ok=True)
            nd = os.path.normpath(os.path.join(rel, d))
            if nd not in manifest["dirs"]:
                manifest["dirs"].append(nd)
        names = [os.path.join(rel, f) for f in files]
    else:
        names = [rel]
    seen = {f["path"] for f in manifest["files"]}
    for name in names:
        if name in seen:
            continue
        dst = os.path.join(bdir, "files", name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(home, name), dst)
        manifest["files"].append({"path": name, "size": os.path.getsize(dst), "sha256": _sha256_file(dst),
                                  "mode": os.stat(dst).st_mode & 0o7777})


def root_cleanup_apply(home, backup_dir):
    """Back up, verify, then remove exactly the dry-run list. -> the reply."""
    if not backup_dir:
        raise Fail("--apply needs --backup-dir")
    check_home(home)
    plan = root_cleanup_plan(home)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bdir = os.path.join(os.path.abspath(os.path.expanduser(backup_dir)), f"root-cleanup-{stamp}")
    _selftest_write_guard(_mkpaths().db_in(bdir))
    for it in plan["remove"]:
        _selftest_write_guard(os.path.join(home, it["target"]))
    if os.path.exists(bdir):
        raise Fail(f"backup folder {bdir} exists; wait a second and retry")
    os.makedirs(bdir, mode=0o700)  # it holds copies of token files
    manifest = {"home": home, "stamp": stamp, "db": None, "files": [], "dirs": [], "plan": plan}
    db = _mkpaths().root_db_in(home)
    if os.path.isfile(db):
        # the backup copy is named after the SOURCE file (mk.db today, mk-overview.db on a new home), so the
        # backup folder says which file it holds and root_restore puts it back under that same name (L10.13)
        name = os.path.basename(db)
        _backup_db(db, os.path.join(bdir, name))
        manifest["db"] = {"path": name, "size": os.path.getsize(os.path.join(bdir, name)),
                          "sha256": _sha256_file(os.path.join(bdir, name))}
    for it in plan["remove"]:
        if it["kind"] in ("file", "dir"):
            _copy_into(home, it["target"], bdir, manifest)
    with open(os.path.join(bdir, CLEANUP_MANIFEST), "w") as fh:
        json.dump(manifest, fh, indent=1)
    bad = _verify_backup(bdir)
    if bad:
        raise Fail(f"backup verification failed, nothing was removed: {bad}")
    removed = {"tables": [], "projects": [], "files": []}
    drops = [it["target"] for it in plan["remove"] if it["kind"] == "table"]
    junk = [it["target"] for it in plan["remove"] if it["kind"] == "projects-row"]
    if os.path.isfile(db):
        root = open_root(home)  # a v2 root migrates here; _root_v3 drops the empty orphans itself
        try:
            for t in drops:
                n = root.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()[0] if root.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() else 0
                if n:
                    raise Fail(f"table {t} gained {n} row(s) since the list; not dropped")
                root.execute(f"DROP TABLE IF EXISTS {t}")
                removed["tables"].append(t)
            _begin(root)
            try:
                for slug in junk:
                    root.execute("DELETE FROM projects WHERE slug=? AND db_path IS NULL", (slug,))
                    removed["projects"].append(slug)
                root.execute("COMMIT")
            except Exception:
                root.execute("ROLLBACK")
                raise
        finally:
            root.close()
    for it in plan["remove"]:
        if it["kind"] == "file":
            os.remove(os.path.join(home, it["target"]))
            removed["files"].append(it["target"])
    # dirs deepest first: their files are listed in the manifest, remove them, then the dirs
    for it in plan["remove"]:
        if it["kind"] == "dir":
            base = os.path.join(home, it["target"])
            if os.path.isdir(base):
                files, dirs = _tree_files(base)
                for f in files:
                    os.remove(os.path.join(base, f))
                for d in sorted(dirs, key=len, reverse=True):
                    os.rmdir(os.path.join(base, d) if d else base)
            removed["files"].append(it["target"] + "/")
    return {"ok": True, "applied": True, "backup": bdir, "removed": removed}


def _verify_backup(bdir):
    """-> list of problems (empty when every manifest entry matches its sha256 and size)."""
    try:
        with open(os.path.join(bdir, CLEANUP_MANIFEST)) as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as e:
        return [f"manifest unreadable: {e}"]
    bad = []
    db = manifest.get("db")
    if db:
        p = os.path.join(bdir, db["path"])
        if not os.path.isfile(p) or _sha256_file(p) != db["sha256"]:
            bad.append("mk.db copy does not match its sha256")
        else:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    bad.append("mk.db copy fails integrity_check")
            finally:
                con.close()
    for f in manifest.get("files", ()):
        p = os.path.join(bdir, "files", f["path"])
        if not os.path.isfile(p) or os.path.getsize(p) != f["size"] or _sha256_file(p) != f["sha256"]:
            bad.append(f"{f['path']} does not match its sha256")
    return bad


def root_restore(home, bdir):
    """Put the root DB and every removed file back from a root-cleanup-<stamp> folder, verified
    against its manifest first. The DB being replaced is saved beside the backup first."""
    check_home(home)
    bdir = os.path.abspath(os.path.expanduser(bdir))
    bad = _verify_backup(bdir)
    if bad:
        raise Fail(f"backup does not verify, nothing restored: {bad}")
    with open(os.path.join(bdir, CLEANUP_MANIFEST)) as fh:
        manifest = json.load(fh)
    for f in manifest["files"]:
        dst = os.path.join(home, f["path"])
        _selftest_write_guard(dst)
        if os.path.exists(dst) and _sha256_file(dst) != f["sha256"]:
            raise Fail(f"refusing to overwrite {dst}: it exists and differs from the backup")
    restored = []
    db = manifest.get("db")
    if db:
        target = os.path.join(home, db["path"])  # the name the backup recorded
        _selftest_write_guard(target)
        if os.path.isfile(target):
            _backup_db(target, os.path.join(bdir, "pre-restore-" + db["path"]))
        tmp = target + ".restore"
        shutil.copy2(os.path.join(bdir, db["path"]), tmp)
        for side in ("-wal", "-shm"):
            if os.path.exists(target + side):
                os.remove(target + side)
        os.replace(tmp, target)
        restored.append(db["path"])
    for d in manifest.get("dirs", ()):
        os.makedirs(os.path.join(home, d), exist_ok=True)
    for f in manifest["files"]:
        dst = os.path.join(home, f["path"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.exists(dst):
            shutil.copy2(os.path.join(bdir, "files", f["path"]), dst)
        restored.append(f["path"])
    return {"ok": True, "restored": restored, "from": bdir}
# --- end L10.9 root cleanup ---


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
    rc = sub.add_parser("root-cleanup")
    rc.add_argument("--dry-run", action="store_true")
    rc.add_argument("--apply", action="store_true")
    rc.add_argument("--backup-dir")
    rs2 = sub.add_parser("restore")
    rs2.add_argument("--from", dest="src", required=True)
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
    if args.cmd == "root-cleanup":
        if args.apply and args.dry_run:
            raise Fail("root-cleanup takes --dry-run or --apply, not both")
        if args.apply:
            return root_cleanup_apply(home, args.backup_dir)
        return {**root_cleanup_plan(home), "dry_run": True}
    if args.cmd == "restore":
        return root_restore(home, args.src)
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
    raise Fail("usage: mkdb.py timeline|projects|overview|plans|run-snapshot|events|root-cleanup"
               "|restore|import-history --json  (or --self-test)")


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


def _worker(home, data, slot, n, bad, project="probe"):
    """One of 8 writer processes, each writing its own task row."""
    os.environ.pop("MKDB_FAIL_BEFORE_ROOT", None)
    os.environ.pop("MKDB_FAIL_IN_TXN", None)
    try:
        for i in range(n):
            if bad and i == n - 1:
                os.environ["MKDB_FAIL_IN_TXN"] = "1"
            transition(home, project, "r", f"w{slot}", "building", note=str(i),
                       sync_root=False, data_dir=data)
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
    real_db = _mkpaths().db_in(real)
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


def _schema6_v6(home):
    """F.1: v6 migration — a fresh store has evidence/risk_reasons/reconcile_due at schema 6;
    a v5 store built by old code upgrades to 6 with every row of every table kept; running
    the upgrade twice changes nothing."""
    data = os.path.join(home, "schema6-v5")
    os.makedirs(data, exist_ok=True)
    # Build the v5 store with the real old chain (stops before _v6): event_src, artifacts.src, art_run_src
    bare = open_project(home, data_dir=data, version=5)
    _check(bare.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] == 5, "v5 store is not at 5")
    _check("src" in [c[1] for c in bare.execute("PRAGMA table_info(artifacts)")]
           and bare.execute("SELECT 1 FROM sqlite_master WHERE name IN ('event_src','art_run_src')").fetchone(),
           "v5 chain store lacks artifacts.src / event_src / art_run_src")
    bare.execute("INSERT INTO runs(id, plan, status, started) VALUES ('r1','p1','active','2026-01-01')")
    bare.execute("INSERT INTO tasks(run, id, parent, type, title, status, updated)"
                 " VALUES ('r1','T1',NULL,'phase','Phase 1','planned','2026-01-01')")
    bare.execute("INSERT INTO tasks(run, id, parent, type, title, status, updated)"
                 " VALUES ('r1','T2','T1','task','Task 2','merged','2026-01-01')")
    bare.execute("INSERT INTO events(run, task, t, chan, kind, text)"
                 " VALUES ('r1','T1','2026-01-01','state','transition','added')")
    bare.execute("INSERT INTO events(run, task, t, chan, kind, text)"
                 " VALUES ('r1','T2','2026-01-02','state','transition','merged')")
    bare.execute("INSERT INTO artifacts(run, task, round, kind, rev, text, created)"
                 " VALUES ('r1','T1',0,'brief',1,'hello','2026-01-01')")
    bare.execute("INSERT INTO change_log(run, task, t) VALUES ('r1','T1','2026-01-01')")
    bare.commit()
    bare.close()

    # Open with the v6 chain — migrates v5 to v6 (v7 has its own test)
    con = open_project(home, data_dir=data, version=6)
    try:
        # Schema is 6
        v = con.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"]
        _check(v == 6, f"schema version is {v}, expected 6")
        # Three new tables exist
        for tbl in ("evidence", "risk_reasons", "reconcile_due"):
            row = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone()
            _check(row is not None, f"table {tbl} missing after v6 migration")
        # Indexes exist
        for idx in ("evidence_run_task", "risk_reasons_run_task"):
            row = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (idx,)).fetchone()
            _check(row is not None, f"index {idx} missing after v6 migration")
        # Every pre-existing row is kept
        runs_n = con.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
        tasks_n = con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
        events_n = con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        artifacts_n = con.execute("SELECT COUNT(*) c FROM artifacts").fetchone()["c"]
        change_n = con.execute("SELECT COUNT(*) c FROM change_log").fetchone()["c"]
        _check(runs_n == 1, f"runs count {runs_n} != 1")
        _check(tasks_n == 2, f"tasks count {tasks_n} != 2")
        _check(events_n == 2, f"events count {events_n} != 2")
        _check(artifacts_n == 1, f"artifacts count {artifacts_n} != 1")
        _check(change_n == 1, f"change_log count {change_n} != 1")
        # Content spot-check
        t2 = con.execute("SELECT status FROM tasks WHERE run='r1' AND id='T2'").fetchone()
        _check(t2["status"] == "merged", f"T2 status {t2['status']} != merged")
    finally:
        con.close()

    # Idempotent: open again, nothing changes
    con = open_project(home, data_dir=data, version=6)
    try:
        v2 = con.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"]
        _check(v2 == 6, f"second open schema {v2} != 6")
        tasks_n2 = con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
        _check(tasks_n2 == 2, f"second open tasks count {tasks_n2} != 2")
    finally:
        con.close()

    # A fresh store also has the tables at schema 6
    fresh_data = os.path.join(home, "schema6-fresh")
    con = open_project(home, data_dir=fresh_data, version=6)
    try:
        v3 = con.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"]
        _check(v3 == 6, f"fresh store schema {v3} != 6")
        for tbl in ("evidence", "risk_reasons", "reconcile_due"):
            row = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone()
            _check(row is not None, f"fresh store table {tbl} missing")
    finally:
        con.close()

    # An old reader (version=5) refuses the v6 store
    try:
        _open_read(_mkpaths().db_in(data), 5)
    except Fail as e:
        _check(e.code == 3, f"old reader exit code {e.code} != 3")
        _check("newer than this code" in str(e), f"old reader message: {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: old reader did not refuse v6 store")


def _explicit_home_names(home):
    """L10.13: a FRESH explicit --home that is not the process's MK home still names its stores by kind: the root
    store mk-overview.db, a project store mk-project.db. An existing mk.db keeps its name and is never joined by a
    new-name file. open_root / open_project / projects / overview must agree with mkpaths.root_db."""
    mp = _mkpaths()
    other = os.path.join(home, "explicit-home")  # not MK_HOME (the self-test home is `home`)
    con = open_root(other)
    con.close()
    _check(os.path.isfile(os.path.join(other, "mk-overview.db")) and not os.path.exists(os.path.join(other, "mk-project.db")),
           f"fresh explicit home root store: {sorted(os.listdir(other))}")
    _check(mp.root_db(other) == os.path.join(other, "mk-overview.db"), mp.root_db(other))
    open_project(other, "px").close()
    _check(os.path.isfile(os.path.join(other, "projects", "px", "mk-project.db")),
           f"fresh project store: {sorted(os.listdir(os.path.join(other, 'projects', 'px')))}")
    _check(projects(other) == [] and overview(other) is not None, "readers see the fresh explicit home")
    # an explicit home that already holds the legacy root keeps it: no new-name file appears beside it
    old = os.path.join(home, "explicit-old")
    os.makedirs(old)
    con = _connect(os.path.join(old, "mk.db"))
    con.close()
    open_root(old).close()
    _check(sorted(os.listdir(old)) == ["mk.db"] or sorted(os.listdir(old)) == ["mk.db", "mk.db-shm", "mk.db-wal"],
           f"a legacy root store gained a sibling: {sorted(os.listdir(old))}")


def _store_sums(con):
    """(row count, sha256 of every row) per pre-existing table: the 'nothing lost' proof."""
    out = {}
    for tbl in ("runs", "tasks", "events", "artifacts", "change_log", "lane_slots", "evidence"):
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
            continue
        rows = sorted(repr(tuple(r)) for r in con.execute(f"SELECT * FROM {tbl}"))
        out[tbl] = (len(rows), hashlib.sha256("\n".join(rows).encode()).hexdigest())
    return out


def _schema7_v7(home):
    """L10.2: a fresh store has settings + watch_cursors at schema 7; a v5 and a v6 store upgrade in
    place with every row of every old table unchanged (count + checksum); a second open changes
    nothing; the tables take the rows L10.3 / L10.5 will write; an old reader refuses the v7 store."""
    want = {"settings": ["key", "value", "updated"], "watch_cursors": ["key", "seq", "started", "updated"]}

    def shape(con):
        for tbl, cols in want.items():
            _check([c[1] for c in con.execute(f"PRAGMA table_info({tbl})")] == cols, f"{tbl} columns")

    fresh = open_project(home, data_dir=os.path.join(home, "schema7-fresh"))
    try:
        _check(fresh.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] == 7, "fresh store not at 7")
        shape(fresh)
    finally:
        fresh.close()
    for old in (5, 6):
        data = os.path.join(home, f"schema7-v{old}")
        con = open_project(home, data_dir=data, version=old)
        _check(con.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] == old, f"not at {old}")
        # PROJECT_DDL (a fresh build) already carries the v7 tables; drop them so this store has the
        # shape real v{old} code left behind, and the upgrade has to create them
        con.execute("DROP TABLE settings")
        con.execute("DROP TABLE watch_cursors")
        _check(not con.execute("SELECT 1 FROM sqlite_master WHERE name IN ('settings','watch_cursors')").fetchone(),
               f"a v{old} store still has the v7 tables")
        con.execute("INSERT INTO runs(id, plan, status, started) VALUES ('r1','p1','active','2026-01-01')")
        con.execute("INSERT INTO tasks(run, id, parent, type, title, status, updated)"
                    " VALUES ('r1','T1',NULL,'phase','Phase 1','planned','2026-01-01')")
        con.execute("INSERT INTO events(run, task, t, chan, kind, text) VALUES ('r1','T1','2026-01-01','state','transition','added')")
        con.execute("INSERT INTO artifacts(run, task, round, kind, rev, text, created)"
                    " VALUES ('r1','T1',0,'brief',1,'hello','2026-01-01')")
        con.execute("INSERT INTO change_log(run, task, t) VALUES ('r1','T1','2026-01-01')")
        con.execute("INSERT INTO lane_slots(slot, lane_path, state) VALUES ('1','/x/lane1','free')")
        if old == 6:
            con.execute("INSERT INTO evidence(run, task, line, gate, result, created)"
                        " VALUES ('r1','T1','a1','g','GREEN','2026-01-01')")
        con.commit()
        before = _store_sums(con)
        con.close()
        con = open_project(home, data_dir=data)
        try:
            _check(con.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] == 7, f"v{old} not upgraded to 7")
            shape(con)
            _check(_store_sums(con) == before, f"v{old} upgrade changed a row: {_store_sums(con)} vs {before}")
        finally:
            con.close()
        con = open_project(home, data_dir=data)  # second open: nothing changes
        try:
            _check(con.execute("SELECT COUNT(*) c FROM schema_version WHERE version=7").fetchone()["c"] == 1, "v7 stamped twice")
            _check(_store_sums(con) == before, f"v{old} second open changed a row")
            con.execute("INSERT INTO settings(key, value, updated) VALUES ('lanes.default_agent','\"claude\"','2026-01-02')")
            con.execute("INSERT INTO watch_cursors(key, seq, started, updated) VALUES ('r1|*', 3, '2026-01-02', '2026-01-02')")
            con.commit()
            _check(con.execute("SELECT value FROM settings").fetchone()["value"] == '"claude"', "settings row")
            _check(con.execute("SELECT seq FROM watch_cursors").fetchone()["seq"] == 3, "watch_cursors row")
        finally:
            con.close()
    # the migration step alone is idempotent on a store that already has the tables
    con = open_project(home, data_dir=os.path.join(home, "schema7-fresh"))
    try:
        _v7(con)
        _v7(con)
        shape(con)
    finally:
        con.close()
    try:
        _open_read(_mkpaths().db_in(os.path.join(home, "schema7-fresh")), 6)
    except Fail as e:
        _check(e.code == 3 and "newer than this code" in str(e), f"old reader message: {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: a v6 reader did not refuse the v7 store")


def _derived_risk(home):
    """F.1: derived risk = min(5, base_risk + sum(OPEN reason points)); plans --json carries
    risk, critical, open_reasons."""
    data = os.path.join(home, "risk-v6")
    con = open_project(home, data_dir=data)
    try:
        con.execute("INSERT INTO plans(id, version, title, header_json)"
                    " VALUES ('rp',1,'Risk plan','{}')")
        con.execute("INSERT INTO runs(id, plan, status, started)"
                    " VALUES ('rr','rp','active','2026-01-01T00:00:00Z')")
        con.commit()
        add_task(con, "rr", "P1", "phase", title="phase")
        add_task(con, "rr", "R1", "task", title="low risk", parent="P1")
        add_task(con, "rr", "R2", "task", title="medium risk", parent="P1")
        add_task(con, "rr", "R3", "task", title="capped risk", parent="P1")
        add_task(con, "rr", "R4", "task", title="cleared risk", parent="P1")
        con.execute("UPDATE tasks SET extra_json='{\"base_risk\":2,\"critical\":true}'"
                    " WHERE run='rr' AND id='R2'")
        con.execute("UPDATE tasks SET extra_json='{\"base_risk\":3,\"critical\":false}'"
                    " WHERE run='rr' AND id='R3'")
        con.execute("UPDATE tasks SET extra_json='{\"base_risk\":1}'"
                    " WHERE run='rr' AND id='R4'")
        con.commit()
        # R1: base 1, no reasons → risk 1
        # R2: base 2, one open reason +2 → risk 4
        con.execute("INSERT INTO risk_reasons(run, task, kind, points, source, opened)"
                    " VALUES ('rr','R2','review_changes',2,'review R1','2026-01-01')")
        # R3: base 3, two open reasons +2+1 → capped at 5
        con.execute("INSERT INTO risk_reasons(run, task, kind, points, source, opened)"
                    " VALUES ('rr','R3','gate_red',2,'gate run','2026-01-01')")
        con.execute("INSERT INTO risk_reasons(run, task, kind, points, source, opened)"
                    " VALUES ('rr','R3','no_evidence',1,'merge','2026-01-01')")
        # R4: base 1, one open +2, one cleared +3 → risk 3 (only open counts)
        con.execute("INSERT INTO risk_reasons(run, task, kind, points, source, opened, cleared, cleared_by)"
                    " VALUES ('rr','R4','review_changes',2,'review R1','2026-01-01',NULL,NULL)")
        con.execute("INSERT INTO risk_reasons(run, task, kind, points, source, opened, cleared, cleared_by)"
                    " VALUES ('rr','R4','gate_red',3,'gate run','2026-01-01','2026-01-02','42')")
        con.commit()

        # Verify through _task_risk
        _check(_task_risk(con, "rr", "R1") == 1, f"R1 risk {_task_risk(con, 'rr', 'R1')} != 1")
        _check(_task_risk(con, "rr", "R2") == 4, f"R2 risk {_task_risk(con, 'rr', 'R2')} != 4")
        _check(_task_risk(con, "rr", "R3") == 5, f"R3 risk {_task_risk(con, 'rr', 'R3')} != 5")
        _check(_task_risk(con, "rr", "R4") == 3, f"R4 risk {_task_risk(con, 'rr', 'R4')} != 3")

        # Verify open_reasons
        r2_reasons = _open_reasons(con, "rr", "R2")
        _check(len(r2_reasons) == 1 and r2_reasons[0]["kind"] == "review_changes"
               and r2_reasons[0]["points"] == 2, f"R2 reasons {r2_reasons}")
        r4_reasons = _open_reasons(con, "rr", "R4")
        _check(len(r4_reasons) == 1 and r4_reasons[0]["points"] == 2,
               f"R4 open reasons {r4_reasons}")
        # R2: bad points coerced to 0
        con.execute("INSERT INTO risk_reasons(run, task, kind, points, source, opened)"
                    " VALUES ('rr','R1','text_points','x','bad','2026-01-01')")
        con.execute("INSERT INTO risk_reasons(run, task, kind, points, source, opened)"
                    " VALUES ('rr','R1','neg_points',-4,'bad','2026-01-01')")
        con.execute("INSERT INTO risk_reasons(run, task, kind, points, source, opened)"
                    " VALUES ('rr','R1','real_points',1.5,'bad','2026-01-01')")
        con.execute("INSERT INTO risk_reasons(run, task, kind, points, source, opened)"
                    " VALUES ('rr','R1','str_points','true','bad','2026-01-01')")
        # NULL source/opened → ""
        con.execute("INSERT INTO risk_reasons(run, task, kind, points, source, opened)"
                    " VALUES ('rr','R1','null_fields',0,NULL,NULL)")
        con.commit()
        _check(_task_risk(con, "rr", "R1") == 1, f"R1 risk with bad points {_task_risk(con, 'rr', 'R1')} != 1")
        r1_reasons = _open_reasons(con, "rr", "R1")
        _check(len(r1_reasons) == 5, f"R1 should have 5 open reasons, got {len(r1_reasons)}")
        for reason in r1_reasons:
            _check(reason["points"] == 0, f"bad points coerced to {reason['points']} != 0")
        null_reason = [r for r in r1_reasons if r["kind"] == "null_fields"][0]
        _check(null_reason["source"] == "" and null_reason["opened"] == "",
               f"NULL source/opened not coerced: {null_reason}")
    finally:
        con.close()

    # plans --json carries risk, critical, open_reasons
    got = plans(home, data_dir=data, plan_id="rp")
    tasks = got["phases"][0]["tasks"] if got["phases"] else []
    by_id = {t["id"]: t for t in tasks}
    _check(by_id["R1"]["risk"] == 1, f"R1 plans risk {by_id['R1']['risk']} != 1")
    _check(by_id["R1"]["critical"] is False, f"R1 critical {by_id['R1']['critical']} != False")
    _check(len(by_id["R1"]["open_reasons"]) == 5, f"R1 open_reasons {by_id['R1']['open_reasons']} should have 5")
    _check(by_id["R2"]["risk"] == 4, f"R2 plans risk {by_id['R2']['risk']} != 4")
    _check(by_id["R2"]["critical"] is True, f"R2 critical {by_id['R2']['critical']} != True")
    _check(len(by_id["R2"]["open_reasons"]) == 1, f"R2 plans open_reasons {by_id['R2']['open_reasons']}")
    _check(by_id["R3"]["risk"] == 5, f"R3 plans risk {by_id['R3']['risk']} != 5")
    _check(by_id["R4"]["risk"] == 3, f"R4 plans risk {by_id['R4']['risk']} != 3")
    # Existing keys unchanged
    _check("id" in by_id["R1"] and "title" in by_id["R1"] and "status" in by_id["R1"],
           "existing task keys missing")
    # R2: critical as string "false" → False (is True, not bool())
    con2 = open_project(home, data_dir=data)
    try:
        con2.execute("UPDATE tasks SET extra_json='{\"base_risk\":1,\"critical\":\"false\"}'"
                     " WHERE run='rr' AND id='R1'")
        con2.commit()
    finally:
        con2.close()
    got2 = plans(home, data_dir=data, plan_id="rp")
    tasks2 = got2["phases"][0]["tasks"] if got2["phases"] else []
    by_id2 = {t["id"]: t for t in tasks2}
    _check(by_id2["R1"]["critical"] is False,
           f"critical string 'false' → {by_id2['R1']['critical']!r} (should be False)")
    # critical as string "true" → False (is True, not truthy)
    con3 = open_project(home, data_dir=data)
    try:
        con3.execute("UPDATE tasks SET extra_json='{\"base_risk\":1,\"critical\":\"true\"}'"
                     " WHERE run='rr' AND id='R1'")
        con3.commit()
    finally:
        con3.close()
    got3 = plans(home, data_dir=data, plan_id="rp")
    tasks3 = got3["phases"][0]["tasks"] if got3["phases"] else []
    by_id3 = {t["id"]: t for t in tasks3}
    _check(by_id3["R1"]["critical"] is False,
           f"critical string 'true' → {by_id3['R1']['critical']!r} (should be False)")
    # critical as int 1 → False (is True)
    con4 = open_project(home, data_dir=data)
    try:
        con4.execute("UPDATE tasks SET extra_json='{\"base_risk\":1,\"critical\":1}'"
                     " WHERE run='rr' AND id='R1'")
        con4.commit()
    finally:
        con4.close()
    got4 = plans(home, data_dir=data, plan_id="rp")
    tasks4 = got4["phases"][0]["tasks"] if got4["phases"] else []
    by_id4 = {t["id"]: t for t in tasks4}
    _check(by_id4["R1"]["critical"] is False,
           f"critical int 1 → {by_id4['R1']['critical']!r} (should be False)")



def _rework_rollup(home):
    """F.1: a phase with one rework child and merged siblings rolls up to rework; rework is in
    COLUMNS (-> Fix) and ATTENTION (card raises the flag); rework is NOT in DEP_MET."""
    data = os.path.join(home, "rework-v6")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "rw", "P1", "phase", title="Phase 1")
        add_task(con, "rw", "A", "task", title="merged task", parent="P1", status="merged")
        add_task(con, "rw", "B", "task", title="rework task", parent="P1", status="rework")
        con.commit()
    finally:
        con.close()

    # transition B to rework — the rollup should lift P1 to rework
    transition(home, "matrixkoder", "rw", "B", "rework", note="found wrong", data_dir=data)
    con = open_project(home, data_dir=data)
    try:
        p1 = con.execute("SELECT status FROM tasks WHERE run='rw' AND id='P1'").fetchone()
        _check(p1["status"] == "rework", f"P1 status {p1['status']} != rework")
        a = con.execute("SELECT status FROM tasks WHERE run='rw' AND id='A'").fetchone()
        _check(a["status"] == "merged", f"A status {a['status']} != merged")
        b = con.execute("SELECT status FROM tasks WHERE run='rw' AND id='B'").fetchone()
        _check(b["status"] == "rework", f"B status {b['status']} != rework")
        # rework is in COLUMNS -> Fix column
        _check(COLUMNS["rework"] == "Fix", f"COLUMNS[rework] = {COLUMNS['rework']} != Fix")
        # rework is in ATTENTION
        _check("rework" in ATTENTION, "rework not in ATTENTION")
        # rework is NOT in DEP_MET
        _check("rework" not in DEP_MET, "rework in DEP_MET")
        # R2: column_of('rework') always returns ("Fix", 1) regardless of prev_status
        _check(column_of("rework", "merged") == ("Fix", 1),
               f"column_of('rework','merged') = {column_of('rework', 'merged')} != ('Fix', 1)")
        _check(column_of("rework", "review") == ("Fix", 1),
               f"column_of('rework','review') = {column_of('rework', 'review')} != ('Fix', 1)")
        _check(column_of("rework", None) == ("Fix", 1),
               f"column_of('rework',None) = {column_of('rework', None)} != ('Fix', 1)")
        _check(column_of("rework", "blocked") == ("Fix", 1),
               f"column_of('rework','blocked') = {column_of('rework', 'blocked')} != ('Fix', 1)")
    finally:
        con.close()

    # Now clear B back to merged — P1 should roll up to merged
    transition(home, "matrixkoder", "rw", "B", "merged", note="fixed", data_dir=data)
    con = open_project(home, data_dir=data)
    try:
        p1 = con.execute("SELECT status FROM tasks WHERE run='rw' AND id='P1'").fetchone()
        _check(p1["status"] == "merged", f"P1 status {p1['status']} != merged")
    finally:
        con.close()

    # Two children: one rework, one blocked — rework wins (first in ATTENTION)
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "rw2", "P2", "phase", title="Phase 2")
        add_task(con, "rw2", "C", "task", title="blocked task", parent="P2")
        add_task(con, "rw2", "D", "task", title="rework task 2", parent="P2")
        con.commit()
    finally:
        con.close()
    transition(home, "matrixkoder", "rw2", "C", "blocked", note="blocked", data_dir=data)
    transition(home, "matrixkoder", "rw2", "D", "rework", note="found wrong", data_dir=data)
    con = open_project(home, data_dir=data)
    try:
        p2 = con.execute("SELECT status FROM tasks WHERE run='rw2' AND id='P2'").fetchone()
        _check(p2["status"] == "rework", f"P2 status {p2['status']} != rework (rework should win over blocked)")
    finally:
        con.close()
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
        _import_sync_root(home)
        _live_writers(home)
        _v1_upgrade(home)
        _lane_slots_v3(home)
        _plans_cols_v4(home)
        _extra_json_v5(home)
        _plans_query(home)
        _plans_detail(home)
        _plans_reparent(home)
        _card_verdict(home)
        _summary_idempotent(home)
        _summary_two_projects(home)
        _orphan_root_untouched(home)
        _orphan_root_absent(home)
        _cards_subcommand_gone(home)
        _root_cleanup(home)
        _reads_create_nothing(home)
        _projects_read(home)
        _run_snapshot(home)
        _run_snapshot_extra_json(home)
        _run_events(home)
        _run_snapshot_migrated(home)
        _run_snapshot_no_store(home)
        _run_snapshot_zero_runs(home)
        _plans_timeline_migrated(home)
        _plans_timeline_premigration(home)
        _plans_timeline_no_store(home)
        _task_type_default(home)
        _root_v2_migration(home, tmp)
        _plan_summary_transitions(home)
        _plan_summary_atomic(home)
        _overview_read(home)
        _overview_production_home(home, tmp)
        _single_writer()
        _ready(home)
        _schema6_v6(home)
        _schema7_v7(home)
        _explicit_home_names(home)
        _derived_risk(home)
        _rework_rollup(home)
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
        for table in ("projects", "plan_summary", "schema_version"):
            _check(root.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone(), table)
        for table in ROOT_DROPPED_TABLES:  # root schema 3: a fresh root never creates them
            _check(not root.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone(),
                   f"a fresh root created {table}")
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
    _check(os.path.exists(_mkpaths().db_in(os.path.join(home, "explicit"))), "--data store")


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
                          "--home", home, "projects", "--json"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _check(rc != 0, f"CLI --data exited {rc}")
    _check(not os.path.exists(cli_bad), "CLI --data created a directory")


def _rows(home, data, run="r"):
    """The task rows of one run as {id: row} — the project-store replacement for the old root
    card read in the self-tests (L7.3). The card view is derived with column_of()."""
    con = open_project(home, data_dir=data)
    try:
        return {r["id"]: dict(r) for r in
                con.execute("SELECT id, status, prev_status FROM tasks WHERE run=?", (run,))}
    finally:
        con.close()


def _view(rows, tid):
    """The kanban column and attention flag a task shows, from its stored row."""
    r = rows[tid]
    col, attention = column_of(r["status"], r["prev_status"])
    return {"status": r["status"], "col": col, "attention": attention}


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
    got = {t: _view(_rows(home, data), t) for t in ("t", "phase")}
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
    got = {t: _view(_rows(home, data), t) for t in ("t", "phase")}
    _check(got["t"]["col"] == "Building" and got["t"]["attention"] == 0, f"attention stuck {got.get('t')}")
    _check(got["phase"]["status"] == "building" and got["phase"]["attention"] == 0,
           f"phase did not return: {got.get('phase')}")
    transition(home, "attn", "r", "fresh", "blocked", sync_root=True, data_dir=data)
    fresh = _view(_rows(home, data), "fresh")
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
    got = {t: _view(_rows(home, data), t) for t in ("phase",)}
    _check(got["phase"]["status"] == "blocked" and got["phase"]["attention"] == 1,
           f"sibling cleared the flag: {got.get('phase')}")
    transition(home, "attn", "r", "t", "building", sync_root=True, data_dir=data)
    got = {t: _view(_rows(home, data), t) for t in ("phase",)}
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
    got = {t: _view(_rows(home, data), t) for t in ("t", "phase")}
    _check(got["t"]["status"] == "blocked" and got["t"]["attention"] == 1, f"task not flagged {got.get('t')}")
    _check(got["phase"]["status"] == "blocked" and got["phase"]["col"] == "Building"
           and got["phase"]["attention"] == 1, f"phase not flagged {got.get('phase')}")
    # L7.3: the flag is a stored fact, so a reload re-derives it — no root copy to replay.
    again = {t: _view(_rows(home, data), t) for t in ("sub", "t2")}
    _check(again["sub"]["col"] == "Building" and again["sub"]["attention"] == 1,
           f"reloaded sub {again.get('sub')}")
    plain = again["t2"]
    _check(plain["status"] == "building" and plain["attention"] == 0, f"plain task changed {plain}")


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
    col, attention = column_of(row["status"], row["prev_status"])
    _check(row["prev_status"] == "building", f"prev_status lost: {dict(row)}")
    _check(col == "Building", f"column fell to {col} for a task that was building")
    if row["status"] == "blocked":
        _check(attention == 1, "blocked without attention")
    else:
        _check(row["status"] == "building" and attention == 0, f"status {row['status']}")
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
        _check(_reaches('a', {'a': ['b'], 'b': ['c']}) == {'b', 'c'}, "_reaches doesn't traverse")
        _check(_reaches('a', {'a': ['a']}) == {'a'}, "_reaches doesn't catch self-cycle")
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
    bare = sqlite3.connect(_mkpaths().db_in(v1))
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
    con = sqlite3.connect(_mkpaths().db_in(data))
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
    # L7.3: a synced write leaves the root card tables untouched — they never gain a row.
    transition(home, "reads", "r", "t", "done", note="synced", sync_root=True, data_dir=data)
    root = open_root(home)
    try:
        for table in ROOT_DROPPED_TABLES:  # not there at all, so nothing can have gained a row
            _check(not root.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone(),
                   f"{table} came back on a synced write")
    finally:
        root.close()


def _crashes(home):
    """Injected failures: a root-write failure leaves the project change durable, an in-project
    failure leaves nothing. L7.3: the pre-root hook now guards the plan_summary write, so the
    store needs a plans row for it to fire."""
    data = _seed_plan_summary_store(home, "crash")
    _add_plan_summary_tasks(data)
    os.environ["MKDB_FAIL_BEFORE_ROOT"] = "1"
    try:
        try:
            transition(home, "crash", "r-alpha", "ALPHA1", "building", sync_root=True, data_dir=data)
        except Fail:
            pass
        else:
            raise SystemExit("SELF-TEST FAIL: the injected root failure did not fire")
        con = open_project(home, data_dir=data)
        try:
            _check(con.execute("SELECT status FROM tasks WHERE id='ALPHA1'").fetchone()["status"]
                   == "building", "project change lost")
        finally:
            con.close()
        os.environ.pop("MKDB_FAIL_BEFORE_ROOT", None)
        os.environ["MKDB_FAIL_IN_TXN"] = "1"
        before = _counts_log(data)
        try:
            transition(home, "crash", "r-alpha", "ALPHA1", "done", sync_root=True, data_dir=data)
        except Fail:
            pass
        else:
            raise SystemExit("SELF-TEST FAIL: the injected txn failure did not fire")
        con = open_project(home, data_dir=data)
        try:
            _check(con.execute("SELECT status FROM tasks WHERE id='ALPHA1'").fetchone()["status"]
                   == "building", "a failed transaction left a change")
        finally:
            con.close()
        _check(_counts_log(data) == before, "change_log grew")
    finally:
        os.environ.pop("MKDB_FAIL_BEFORE_ROOT", None)
        os.environ.pop("MKDB_FAIL_IN_TXN", None)


def _counts_log(data):
    """The change_log row count of one store — a small crash-test probe."""
    con = open_project(os.path.dirname(data), data_dir=data)
    try:
        return con.execute("SELECT COUNT(*) c FROM change_log").fetchone()["c"]
    finally:
        con.close()



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


def _import_sync_root(home):
    """Z9: `sync_root=False` imports everything and refreshes no root plan_summary; the default
    call keeps writing them.

    A throwaway temp data dir on the DEFAULT home must leave nothing in the user's real root, and
    the only root write an import makes is the plan_summary batch, so the caller needs a switch
    that changes nothing else. The reload case proves the projection is reachable again through
    the default, so `sync_root=False` is a gate and not a dead parameter.
    """
    data = _seed_plan_summary_store(home, "z9sync")
    _add_plan_summary_tasks(data)
    run_row, tasks, events, artifacts = _sample()
    con = open_project(home, data_dir=data)
    try:
        _check(con.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] > 0,
               "_import_sync_root: no plans row: the case proves nothing")
    finally:
        con.close()
    counts = import_history(home, "z9sync", "hist", run_row=run_row, tasks=tasks, events=events,
                            artifacts=artifacts, data_dir=data, sync_root=False)
    _check(counts["tasks"] == 3 and counts["events"] == 5 and counts["artifacts"] == 2,
           f"_import_sync_root: sync_root=False imported nothing: {counts}")
    con = open_project(home, data_dir=data)
    try:
        n_tasks = con.execute("SELECT COUNT(*) c FROM tasks WHERE run='hist'").fetchone()["c"]
        n_ev = con.execute("SELECT COUNT(*) c FROM events WHERE run='hist'").fetchone()["c"]
    finally:
        con.close()
    _check(n_tasks == 3 and n_ev == 5, "_import_sync_root: the store lost the imported rows")
    rows = [r for r in root_rows(home) if r["project"] == "z9sync"]
    _check(rows == [], f"_import_sync_root: sync_root=False still wrote plan_summary rows {rows}")
    # the DEFAULT is unchanged: the same import shape writes the rows back
    again = import_history(home, "z9sync", "hist2", run_row=dict(run_row, id="hist2"), tasks=tasks,
                           events=events, artifacts=artifacts, data_dir=data)
    _check(again["runs"] == 1 and again["tasks"] == 3, f"_import_sync_root: second import {again}")
    rows = [r for r in root_rows(home) if r["project"] == "z9sync"]
    _check(len(rows) >= 1, f"_import_sync_root: the default import wrote no plan_summary row {rows}")
    for plan in ("alpha", "beta"):
        got = json.loads(_stored_plan(home, "z9sync", plan)["counts_json"])
        want = _plan_recount(home, data, plan)
        _check(got == want, f"_import_sync_root: {plan} counts {got} != project DB {want}")


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
    bare = sqlite3.connect(_mkpaths().db_in(data))
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
    bare = sqlite3.connect(_mkpaths().db_in(data))
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
    bare = sqlite3.connect(_mkpaths().db_in(data))
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
    bare = sqlite3.connect(_mkpaths().db_in(data))
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
                 "branch", "updated", "acceptance", "owned", "deps", "route", "subtasks",
                 "risk", "critical", "open_reasons"}
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


def _plans_reparent(home):
    """Z13: grouping attaches by parent, never by row visit order.

    rowid order only holds at insert time (add_task, import_history's _parents_first);
    `task set <id> parent=<later row>` points a row at a parent inserted AFTER it. Case a: a
    phase row created after its two tasks (the live store's Z1..Z11 under a later Z). Case b:
    a subtask re-parented under a task that is itself re-parented under a later phase — the
    one-pass grouping read the whole chain as subtasks and dropped it from the tree.
    """
    data = os.path.join(home, "plans-rp")
    hdr = json.dumps({"status": "running", "created": "2026-03-01T00:00:00Z",
                      "phases": [{"id": "P", "wave": 1}]})
    con = open_project(home, data_dir=data)
    try:
        _begin(con)
        for plan, run in (("rp-a", "rr-a"), ("rp-b", "rr-b")):
            con.execute("INSERT INTO plans(id, version, title, header_json) VALUES (?,?,?,?)",
                        (plan, 1, plan, hdr))
            con.execute("INSERT INTO runs(id, plan, status, started) VALUES (?,?,?,?)",
                        (run, "plans/" + plan + ".md", "active", "2026-03-01T00:00:00Z"))
        con.execute("COMMIT")
        add_task(con, "rr-a", "T1", "task", title="One")
        add_task(con, "rr-a", "T2", "task", title="Two")
        add_task(con, "rr-a", "P", "phase", title="Late phase")  # rowid AFTER its children
        add_task(con, "rr-b", "S", "subtask", title="Deep child")
        add_task(con, "rr-b", "B", "task", title="Later task")  # rowid after S
        add_task(con, "rr-b", "P2", "phase", title="Latest phase")  # rowid after B
        con.execute("UPDATE tasks SET parent='P' WHERE run='rr-a' AND id IN ('T1','T2')")
        con.execute("UPDATE tasks SET parent='B' WHERE run='rr-b' AND id='S'")
        con.execute("UPDATE tasks SET parent='P2' WHERE run='rr-b' AND id='B'")
        con.commit()  # a re-parent, not an insert: the row orders above are the store's truth
    finally:
        con.close()
    got = plans(home, data_dir=data, plan_id="rp-a")
    pids = [p["id"] for p in got["phases"]]
    _check(pids == ["P"], f"reparent phases {pids}")  # T1/T2 are no phase card of their own
    _check([t["id"] for t in got["phases"][0]["tasks"]] == ["T1", "T2"],
           f"reparent phase tasks {[t['id'] for t in got['phases'][0]['tasks']]}")
    _check(got["counts"] == {"planned": 2}, f"reparent counts {got['counts']}")
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), "plans", "rp-a", "--home",
                           home, "--data", data, "--json"], capture_output=True, text=True)
    _check(proc.returncode == 0 and "Traceback" not in proc.stderr,
           f"reparent cli rc={proc.returncode} err={proc.stderr[-300:]}")
    cli = json.loads(proc.stdout)
    _check([p["id"] for p in cli["phases"]] == ["P"]
           and [t["id"] for t in cli["phases"][0]["tasks"]] == ["T1", "T2"],
           f"reparent cli phases {cli['phases']}")
    got = plans(home, data_dir=data, plan_id="rp-b")
    pids = [p["id"] for p in got["phases"]]
    _check(pids == ["P2"], f"reparent nested phases {pids}")
    _check([t["id"] for t in got["phases"][0]["tasks"]] == ["B"],
           f"reparent nested task {got['phases'][0]['tasks']}")
    b = got["phases"][0]["tasks"][0]
    _check([s["id"] for s in b["subtasks"]] == ["S"] and b["subtasks"][0]["subtasks"] == [],
           f"reparent nested subtask {b['subtasks']}")


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
    """A transition that changes the verdict stores it on the task row (the root card is gone)."""
    data = os.path.join(home, "verdict")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "t", "task")
        con.execute("UPDATE tasks SET verdict=? WHERE run=? AND id=?", ("APPROVE", "r", "t"))
        con.commit()
    finally:
        con.close()
    transition(home, "verdict", "r", "t", "review", round=1, sync_root=True, data_dir=data)
    con = open_project(home, data_dir=data)
    try:
        row = con.execute("SELECT verdict, status FROM tasks WHERE id='t'").fetchone()
        _check(row["verdict"] == "APPROVE", f"import-style verdict {dict(row)}")
    finally:
        con.close()
    transition(home, "verdict", "r", "t", "done", round=2, verdict="REQUEST CHANGES",
               sync_root=True, data_dir=data)
    con = open_project(home, data_dir=data)
    try:
        row = con.execute("SELECT verdict, status FROM tasks WHERE id='t'").fetchone()
        _check(row["verdict"] == "REQUEST CHANGES" and row["status"] == "done",
               f"transition verdict {dict(row)}")
    finally:
        con.close()


# --- S1 summary self-tests: extract this whole region for the old-code probe ---
def _plan_summary_snapshot(home, project):
    """Every plan_summary row of one project as {plan: (title, status, counts_json, live_run)}."""
    root = open_root(home)
    try:
        return {r["plan"]: (r["title"], r["status"], r["counts_json"], r["live_run"]) for r in
                root.execute("SELECT plan, title, status, counts_json, live_run"
                             " FROM plan_summary WHERE project=?", (project,))}
    finally:
        root.close()


def _summary_idempotent(home):
    """A second catch_up over an already-synced store writes nothing (C3 plan_summary heal).

    One root connection stays open across both calls: PRAGMA data_version on it moves only
    when another connection commits to the file, so it doubles the row checks. If data_version
    ever proves unusable here, the row checks below are the idempotence contract on their own.
    """
    data = _seed_plan_summary_store(home, "psid")
    _add_plan_summary_tasks(data)
    # a root write that fails leaves plan_summary stale (the project change is durable), so the
    # first catch_up has real work to do; catch_up only heals, it never runs on the happy path.
    os.environ["MKDB_FAIL_BEFORE_ROOT"] = "1"
    try:
        try:
            transition(home, "psi", "r-alpha", "ALPHA1", "building", sync_root=False, data_dir=data)
        except Fail:
            pass
        else:
            raise SystemExit("SELF-TEST FAIL: _summary_idempotent: the injected failure did not fire")
    finally:
        os.environ.pop("MKDB_FAIL_BEFORE_ROOT", None)
    _check(not _plan_summary_snapshot(home, "psi"), "_summary_idempotent: root not left stale")
    caught = catch_up(home, "psi", data_dir=data)
    _check(caught >= 1, f"_summary_idempotent: first catch_up wrote nothing (n={caught})")
    root = open_root(home)
    try:
        dv = root.execute("PRAGMA data_version").fetchone()[0]
        before = _plan_summary_snapshot(home, "psi")
        _check(before, "_summary_idempotent: no plan_summary rows after catch_up")
        again = catch_up(home, "psi", data_dir=data)
        _check(again == 0, f"_summary_idempotent: second catch_up wrote {again}")
        dv2 = root.execute("PRAGMA data_version").fetchone()[0]
        after = _plan_summary_snapshot(home, "psi")
        _check(dv == dv2, f"_summary_idempotent: data_version moved {dv} -> {dv2}")
        _check(before == after, f"_summary_idempotent: rows changed {before} -> {after}")
    finally:
        root.close()


def _summary_two_projects(home):
    """Two projects under one root: each project's plan_summary matches its own store, and the
    rows differ — no project's plan numbers leak into the other's."""
    d1 = _seed_plan_summary_store(home, "psp1")
    _add_plan_summary_tasks(d1)
    transition(home, "psp1", "r-alpha", "ALPHA1", "building", sync_root=True, data_dir=d1)
    d2 = _seed_plan_summary_store(home, "psp2")
    _add_plan_summary_tasks(d2)
    transition(home, "psp2", "r-alpha", "ALPHA1", "done", sync_root=True, data_dir=d2)
    transition(home, "psp2", "r-beta", "BETA1", "stalled", sync_root=True, data_dir=d2)
    r1, r2 = _plan_summary_snapshot(home, "psp1"), _plan_summary_snapshot(home, "psp2")
    _check(r1 and r2, f"_summary_two_projects: missing rows {r1} / {r2}")
    for plan in ("alpha", "beta"):
        got1 = json.loads(r1[plan][2])
        _check(got1 == _plan_recount(home, d1, plan),
               f"_summary_two_projects: psp1 {plan} counts {got1} != store")
        got2 = json.loads(r2[plan][2])
        _check(got2 == _plan_recount(home, d2, plan),
               f"_summary_two_projects: psp2 {plan} counts {got2} != store")
    _check(r1["alpha"] != r2["alpha"], "_summary_two_projects: the two projects share one row")
# --- end S1 summary self-tests ---


# --- L7.3 orphan-root self-tests: extract this whole region for the old-code probe ---
# C3 (L7.3): cards, project_summary and sync are the pre-linkage copies of the project store.
# After the drop nothing in mkdb reads or writes them; a status write leaves them exactly as
# they were, and a root that never had them still serves every path that stays.
# The pre-L10.9 shape of the three tables, for fixtures that need a root which still holds rows in
# them (a fresh root no longer creates them).
OLD_ROOT_ORPHAN_DDL = """
CREATE TABLE IF NOT EXISTS cards(
  project TEXT, run TEXT, task TEXT, parent TEXT, type TEXT, title TEXT, status TEXT, col TEXT,
  attention INTEGER, question TEXT, route_json TEXT, lane TEXT, round INTEGER, verdict TEXT,
  gates TEXT, updated TEXT, change_no INTEGER, PRIMARY KEY(project, run, task));
CREATE TABLE IF NOT EXISTS project_summary(
  project TEXT PRIMARY KEY, active_runs INTEGER, by_status_json TEXT, attention INTEGER,
  last_event TEXT, change_no INTEGER);
CREATE TABLE IF NOT EXISTS sync(project TEXT PRIMARY KEY, last_change_no INTEGER);
"""


def _root_orphan_dump(home):
    """Every row of the three orphan tables as one canonical text block.

    Rows are read in rowid order and rendered with sort_keys, so a rewritten value, an inserted
    row or a moved row shows up as a difference. `updated` is the writer's own stamp, so a table
    nothing writes renders byte-identically.
    """
    root = open_root(home)
    try:
        out = []
        for table in ROOT_DROPPED_TABLES:
            rows = [list(r) for r in root.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            out.append(table + "=" + json.dumps(rows, sort_keys=True))
        return "\n".join(out)
    finally:
        root.close()


def _copy_store(src_dir, dst_dir):
    """Copy a root store byte for byte into dst_dir.

    mk.db and a non-empty -wal travel; -shm is left behind, because SQLite rebuilds it from the
    WAL on the next open (a stale copy of it would be the one way to read a wrong state).
    """
    live = os.path.basename(_mkpaths().root_db_in(src_dir))  # a ROOT store: kind explicit, not guessed from the home
    for name in (live, live + "-wal"):
        src = os.path.join(src_dir, name)
        if not os.path.exists(src) or not os.path.getsize(src):
            continue
        os.makedirs(dst_dir, exist_ok=True)
        with open(src, "rb") as fh:
            blob = fh.read()
        with open(os.path.join(dst_dir, name), "wb") as fh:
            fh.write(blob)


def _seed_orphan_root(home, name):
    """A root shaped like a live one: a projects row and a row in each orphan table.

    The rows go in as direct SQL — the pre-linkage writers are exactly what L7.3 removes, so the
    fixture cannot borrow them. sync carries a LOW watermark and the card belongs to another
    project, so a writer that still touches these tables has to move them.
    """
    live = os.path.join(home, name)
    root = open_root(live)
    try:
        root.executescript(OLD_ROOT_ORPHAN_DDL)  # a fresh root has no such tables any more
        root.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, created,"
                     " last_activity, db_path) VALUES (?,?,?,?,?,?,?,?)",
                     ("legacy", "Legacy", "/r/legacy", "/d/legacy", "local", "t1", "t2", None))
        root.execute("INSERT INTO cards(project, run, task, parent, type, title, status, col,"
                     " attention, updated, change_no) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     ("legacy", "r", "t", None, "task", "Kept", "done", "Done", 0, "t3", 7))
        root.execute("INSERT INTO sync(project, last_change_no) VALUES ('legacy', 7)")
        root.execute("INSERT INTO project_summary(project, active_runs, by_status_json, attention,"
                     " last_event, change_no) VALUES (?,?,?,?,?,?)",
                     ("legacy", 1, json.dumps({"done": 1}), 0, "t3", 7))
        root.commit()
        root.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        root.close()
    return live


def _orphan_root_untouched(home):
    """L7.3 (C3): a status write leaves cards/project_summary/sync alone.

    Fresh root: the three tables are not there at all (root schema 3 never creates them) and a
    transition(sync_root=True) does not bring them back — their only writer is gone, and
    plan_summary, the rollup that stays, moves instead. A root that already
    holds rows, copied into a temp home exactly as a live root would be, keeps them
    byte-identical. Both halves FAIL on the pre-L7.3 code, which writes a card, a summary and a
    watermark on every sync_root move.
    """
    data = _seed_plan_summary_store(home, "oruntu")
    _add_plan_summary_tasks(data)

    fresh = os.path.join(home, "oruntu-fresh")
    open_root(fresh).close()
    transition(fresh, "oruntu", "r-alpha", "ALPHA1", "building", sync_root=True, data_dir=data)
    root = open_root(fresh)
    try:
        made = [t for t in ROOT_DROPPED_TABLES if root.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]
        _check(made == [], f"_orphan_root_untouched: a fresh root has the dropped tables: {made}")
        _check(root.execute("SELECT COUNT(*) c FROM plan_summary").fetchone()["c"] >= 1,
               "_orphan_root_untouched: the transition did not update plan_summary")
    finally:
        root.close()

    live = _seed_orphan_root(home, "oruntu-live")
    copy = os.path.join(home, "oruntu-copy")
    _copy_store(live, copy)
    before = _root_orphan_dump(copy)
    transition(copy, "oruntu", "r-alpha", "ALPHA2", "building", sync_root=True, data_dir=data)
    after = _root_orphan_dump(copy)
    _check(after == before,
           "_orphan_root_untouched: a status write moved the copied root's dropped tables\n"
           f"  before: {before}\n  after:  {after}")


def _orphan_root_absent(home):
    """L7.3 (C3): a root with NO cards/project_summary/sync still serves transition, overview
    and plans.

    The three tables are dropped on a temp root — the shape a root built after the drop has, or
    one whose pre-linkage copies are already gone. None of these paths may read, need or recreate
    them. The transition runs with the default sync_root=False, since the card path is exactly
    what the drop removes; plans() and overview() must answer from plan_summary alone.
    """
    sub = os.path.join(home, "orunta-root")
    data = _seed_plan_summary_store(home, "orunta")
    _add_plan_summary_tasks(data)
    root = open_root(sub)
    try:
        for table in ROOT_DROPPED_TABLES:
            root.execute(f"DROP TABLE IF EXISTS {table}")
        root.commit()
    finally:
        root.close()
    transition(sub, "orunta", "r-alpha", "ALPHA1", "building", data_dir=data)
    got = overview(sub)
    _check(got["ok"] is True and len(got["plans"]) == 2,
           f"_orphan_root_absent: overview without the dropped tables: {got}")
    listed = plans(sub, "orunta", data)
    _check(listed["ok"] is True and len(listed["plans"]) == 2,
           f"_orphan_root_absent: plans without the dropped tables: {listed}")
    root = open_root(sub)
    try:
        back = [t for t in ROOT_DROPPED_TABLES if root.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]
        _check(back == [], f"_orphan_root_absent: a dropped table came back: {back}")
    finally:
        root.close()


def _cards_subcommand_gone(home):
    """L7.3 (C3): mkdb.py cards is gone — the subcommand exits 2 through argparse's own error
    path, the same as any unknown command, while a read that stays still answers 0.

    FAILS on the pre-L7.3 code, where cards is a real subcommand and exits 0.
    """
    gone = subprocess.run([sys.executable, __file__, "--home", home, "cards", "--json"],
                          capture_output=True, text=True)
    _check(gone.returncode == 2,
           f"_cards_subcommand_gone: cards exited {gone.returncode}, wanted 2"
           f" (stdout {gone.stdout.strip()!r}, stderr {gone.stderr.strip()[-200:]!r})")
    kept = subprocess.run([sys.executable, __file__, "--home", home, "overview", "--json"],
                          capture_output=True, text=True)
    _check(kept.returncode == 0,
           f"_cards_subcommand_gone: overview exited {kept.returncode}"
           f": {kept.stderr.strip()[-200:]}")
# --- end L7.3 orphan-root self-tests ---


# --- L10.9 root-cleanup self-tests ---
def _rc_write(path, text="", mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    if mode:
        os.chmod(path, mode)


def _rc_home(base, name, old_bus_tokens=None, lanes_match=True):
    """A temp root home shaped like a live one, at root schema 2: an empty cards/project_summary/
    event_src, a sync table WITH a row, the projects rows of every cleanup kind, and the old bus
    files beside their bus/ replacements. -> (home, repo, tokens dict)."""
    home = os.path.join(base, name)
    repo = os.path.join(base, name + "-repo")
    os.makedirs(os.path.join(repo, ".matrixkoder"), exist_ok=True)
    con = open_root(home, version=2)  # a v2 root: apply migrates it to v3
    try:
        con.executescript(OLD_ROOT_ORPHAN_DDL + "CREATE TABLE IF NOT EXISTS event_src(run TEXT, src TEXT,"
                          " PRIMARY KEY(run, src));")
        con.execute("INSERT INTO sync(project, last_change_no) VALUES ('keepme', 3)")
        gone = os.path.join(base, "gone-" + name)  # a path that does not exist
        db_path = _mkpaths().db_in(os.path.join(home, "projects", "main"))
        data_main = os.path.join(repo, ".matrixkoder")
        rows = [("main", "Main", repo, data_main, "local", "t", "t", db_path),
                ("pa11a5a11a5a11a5a", "alias with token", "", data_main, "local", "t", "t", db_path),
                ("pb22b5b22b5b22b5b", "alias no token", "", data_main, "local", "t", "t", None),
                ("junk", "gone", gone, gone + "/.matrixkoder", "local", "t", "t", None),
                ("planned", "has a plan row", gone + "-2", gone + "-2/.matrixkoder", "local", "t", "t", None)]
        con.executemany("INSERT INTO projects(slug,name,repo,data_dir,mode,created,last_activity,db_path)"
                        " VALUES (?,?,?,?,?,?,?,?)", rows)
        con.execute("INSERT INTO plan_summary(project,plan,title,status,counts_json,live_run,updated)"
                    " VALUES ('main','pl','T','approved','{\"done\":1}',NULL,'t')")
        con.execute("INSERT INTO plan_summary(project,plan,title,status,counts_json,live_run,updated)"
                    " VALUES ('planned','pl2','T2','draft','{}',NULL,'t')")
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    tokens = {"tok-main-1": {"project": "main", "token": "tok-main-1"},
              "tok-alias-1": {"project": "pa11a5a11a5a11a5a", "token": "tok-alias-1"}}
    new_tokens = dict(tokens)
    _rc_write(os.path.join(home, "bus", "bus-tokens.json"), json.dumps(new_tokens), 0o600)
    old = dict(tokens) if old_bus_tokens is None else old_bus_tokens
    _rc_write(os.path.join(home, "bus-tokens.json"), json.dumps(old), 0o600)
    for name_ in ("bus-ensure.lock", "bus.lock", "bus-tokens.lock"):
        _rc_write(os.path.join(home, name_))
        _rc_write(os.path.join(home, "bus", name_))
    _rc_write(os.path.join(home, "ring-queue.json"), "[]", 0o600)
    _rc_write(os.path.join(home, "bus", "ring-queue.json"), "[]", 0o600)
    _rc_write(os.path.join(home, "bus-lanes", "l1.json"), '{"token": "tok-main-1"}', 0o600)
    _rc_write(os.path.join(home, "bus-lanes", "l2.json"), '{"token": "tok-alias-1"}', 0o600)
    _rc_write(os.path.join(home, "bus", "lanes", "l1.json"), '{"token": "tok-main-1"}', 0o600)
    if lanes_match:
        _rc_write(os.path.join(home, "bus", "lanes", "l2.json"), '{"token": "tok-alias-1"}', 0o600)
    os.makedirs(os.path.join(home, ".mk-home"))
    _rc_write(os.path.join(home, "runs", ".lock"))
    _rc_write(os.path.join(home, "config.json"), '{"a": 1}')
    return home, repo, tokens


def _rc_listing(home):
    """Every path under home with size, mode and sha256 (dirs by name): the 'nothing changed'
    proof for a dry run and the 'back byte for byte' proof for a restore. mk.db sidecars count."""
    out = {}
    for cur, dnames, fnames in os.walk(home):
        for d in dnames:
            out[os.path.relpath(os.path.join(cur, d), home)] = "dir"
        for f in fnames:
            p = os.path.join(cur, f)
            out[os.path.relpath(p, home)] = (os.path.getsize(p), os.stat(p).st_mode & 0o7777, _sha256_file(p))
    return out


def _rc_dump(home):
    """Every row of every table of the root DB, read-only, as one comparable dict."""
    con = _connect_ro(_mkpaths().root_db_in(home))
    try:
        out = {}
        for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            out[r[0]] = [list(x) for x in con.execute(f"SELECT * FROM {r[0]} ORDER BY rowid")]
        return out
    finally:
        con.close()


def _rc_reads(home):
    """The overview read paths, straight from the root DB: projects that carry a db_path, and every
    plan_summary row. Apply must leave both exactly as they were."""
    con = _connect_ro(_mkpaths().root_db_in(home))
    try:
        return ([list(r) for r in con.execute("SELECT * FROM projects WHERE db_path IS NOT NULL ORDER BY slug")],
                [list(r) for r in con.execute("SELECT * FROM plan_summary ORDER BY project, plan")])
    finally:
        con.close()


def _root_cleanup(home):
    """L10.9: the dry-run list, apply with a backup, and restore, all on temp homes."""
    base = os.path.join(home, "rc")
    os.makedirs(base, exist_ok=True)
    h, repo, tokens = _rc_home(base, "h1")
    want_remove = {("table", "cards"), ("table", "project_summary"), ("table", "event_src"),
                   ("projects-row", "pb22b5b22b5b22b5b"), ("projects-row", "junk"),
                   ("dir", "bus-lanes"), ("dir", ".mk-home"), ("file", "runs/.lock"), ("dir", "runs"),
                   ("file", "bus-ensure.lock"), ("file", "bus.lock"), ("file", "bus-tokens.json"),
                   ("file", "bus-tokens.lock"), ("file", "ring-queue.json")}

    # 1. the dry run writes nothing and lists exactly the removable items
    before_tree, before_dump = _rc_listing(h), _rc_dump(h)
    plan = root_cleanup_plan(h)
    cli = subprocess.run([sys.executable, __file__, "--home", h, "root-cleanup", "--dry-run"],
                         capture_output=True, text=True)
    _check(cli.returncode == 0, f"_root_cleanup: dry-run CLI exited {cli.returncode}: {cli.stderr[-300:]}")
    _check(_rc_listing(h) == before_tree and _rc_dump(h) == before_dump,
           "_root_cleanup: the dry run changed the home")
    got = {(i["kind"], i["target"]) for i in plan["remove"]}
    _check(got == want_remove, f"_root_cleanup: remove list {sorted(got ^ want_remove)}")
    cli_plan = json.loads(cli.stdout)
    _check(cli_plan["dry_run"] is True and {(i["kind"], i["target"]) for i in cli_plan["remove"]} == want_remove,
           "_root_cleanup: the CLI dry run lists something else")
    _check([m["target"] for m in plan["merge"]] == ["pa11a5a11a5a11a5a"] and plan["merge"][0]["canonical"] == "main",
           f"_root_cleanup: alias row with a token is not a merge: {plan['merge']}")
    keeps = {(i["kind"], i["target"]): i["reason"] for i in plan["keep"]}
    _check(keeps.get(("table", "sync"), "").startswith("keep: has rows"), f"_root_cleanup: sync {keeps}")
    _check(("projects-row", "planned") not in keeps and ("projects-row", "planned") not in got,
           "_root_cleanup: the row a plan_summary row names was touched")
    _check(keeps.get(("file", "config.json"), "").endswith("L10.3 migrate-settings"), f"_root_cleanup: {keeps}")
    _check(all(i["reason"] and "rows" in i for i in plan["remove"]) and sum(plan["counts"].values()) == len(plan["remove"]),
           "_root_cleanup: items lack a reason/rows, or counts do not add up")
    _check("tok-main-1" not in cli.stdout and "tok-alias-1" not in cli.stdout, "_root_cleanup: a token was printed")
    # a missing root DB lists files only and creates nothing
    empty = os.path.join(base, "empty")
    os.makedirs(empty)
    _check(root_cleanup_plan(empty)["remove"] == [] and not os.path.exists(_mkpaths().root_db_in(empty)),
           "_root_cleanup: a home with no root DB")

    # 2. apply is refused without --backup-dir, and --dry-run with --apply is a usage error
    for args in (["--apply"], ["--apply", "--dry-run", "--backup-dir", os.path.join(base, "bk")]):
        r = subprocess.run([sys.executable, __file__, "--home", h, "root-cleanup", *args],
                           capture_output=True, text=True)
        _check(r.returncode == 2 and _rc_listing(h) == before_tree, f"_root_cleanup: {args} -> {r.returncode}")

    # 3. apply: backup, verify, then exactly the listed items go
    reads_before = _rc_reads(h)
    bk = os.path.join(base, "bk")
    res = root_cleanup_apply(h, bk)
    bdir = res["backup"]
    _check(os.path.basename(bdir).startswith("root-cleanup-") and _verify_backup(bdir) == [],
           f"_root_cleanup: backup {_verify_backup(bdir)}")
    manifest = json.load(open(os.path.join(bdir, CLEANUP_MANIFEST)))
    _check(manifest["db"]["sha256"] == _sha256_file(os.path.join(bdir, manifest["db"]["path"])) and manifest["files"],
           "_root_cleanup: manifest")
    for kind, target in want_remove:
        if kind in ("file", "dir"):
            _check(not os.path.exists(os.path.join(h, target)), f"_root_cleanup: {target} is still there")
    kept = ["config.json", "bus/bus-tokens.json", "bus/lanes/l1.json", "bus/ring-queue.json"]
    _check(all(os.path.exists(os.path.join(h, k)) for k in kept), "_root_cleanup: a kept file went")
    after = _rc_dump(h)
    _check(all(t not in after for t in ("cards", "project_summary", "event_src")) and after["sync"] == [["keepme", 3]],
           f"_root_cleanup: tables after apply {sorted(after)}")
    slugs = {r[0] for r in after["projects"]}
    _check(slugs == {"main", "pa11a5a11a5a11a5a", "planned"}, f"_root_cleanup: projects after apply {slugs}")
    _check(_rc_reads(h) == reads_before, "_root_cleanup: apply changed the overview read paths")
    ov = overview(h)  # the real reader, now that the root is at schema 3
    _check(ov["ok"] and {p["plan"] for p in ov["plans"]} == {"pl", "pl2"}, f"_root_cleanup: overview {ov}")
    _check(_schema_version(_connect_ro(_mkpaths().root_db_in(h))) == 3, "_root_cleanup: root not at schema 3")
    # the live tokens still resolve: the bus/ store is untouched and holds every token slug
    _check(set(json.load(open(os.path.join(h, "bus", "bus-tokens.json")))) == set(tokens),
           "_root_cleanup: a live token is gone from bus/bus-tokens.json")
    again = root_cleanup_plan(h)
    _check(again["remove"] == [], f"_root_cleanup: a second dry run still lists {again['remove']}")

    # 4. restore puts every item back byte for byte (the DB logically and by the backup's sha)
    rr = root_restore(h, bdir)
    _check(manifest["db"]["path"] in rr["restored"], "_root_cleanup: restore did not restore the DB")
    _check(_sha256_file(_mkpaths().root_db_in(h)) == manifest["db"]["sha256"], "_root_cleanup: restored DB sha")
    _check(_rc_dump(h) == before_dump, "_root_cleanup: restored DB rows differ")
    now_tree = _rc_listing(h)
    for rel, v in before_tree.items():
        if rel.startswith(manifest["db"]["path"]):
            continue
        _check(now_tree.get(rel) == v, f"_root_cleanup: restore differs at {rel}: {now_tree.get(rel)} != {v}")
    _check(os.path.isfile(os.path.join(bdir, "pre-restore-" + manifest["db"]["path"])), "_root_cleanup: pre-restore copy missing")
    # restoring over a changed file is refused, nothing half-restored
    root_cleanup_apply(h, bk + "2")
    _rc_write(os.path.join(h, "ring-queue.json"), "[1]")
    r2 = [d for d in os.listdir(bk + "2")][0]
    try:
        root_restore(h, os.path.join(bk + "2", r2))
    except Fail as e:
        _check("refusing to overwrite" in str(e), f"_root_cleanup: {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: restore overwrote a changed file")
    # a corrupted backup is caught before anything is removed or restored
    victim = os.path.join(bdir, "files", "bus.lock")
    _rc_write(victim, "tampered")
    _check(_verify_backup(bdir) != [], "_root_cleanup: a tampered backup verified")
    try:
        root_restore(h, bdir)
    except Fail as e:
        _check("does not verify" in str(e), f"_root_cleanup: {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: restore used a tampered backup")

    # 5. keep rules: a token only in the old file, a lane file missing from bus/lanes, a running
    #    old bus, an .mk-home with a file, and a table with rows are all kept
    h2, _repo, _t = _rc_home(base, "h2", old_bus_tokens={"tok-old-only": {"project": "main"}}, lanes_match=False)
    _rc_write(os.path.join(h2, ".mk-home", "x"), "data")
    p2 = root_cleanup_plan(h2)
    k2 = {(i["kind"], i["target"]): i["reason"] for i in p2["keep"]}
    r2 = {(i["kind"], i["target"]) for i in p2["remove"]}
    _check("only in the old file" in k2.get(("file", "bus-tokens.json"), ""), f"_root_cleanup: {k2}")
    _check("holds 1 of 2" in k2.get(("dir", "bus-lanes"), ""), f"_root_cleanup: {k2}")
    _check("holds files" in k2.get(("dir", ".mk-home"), ""), f"_root_cleanup: {k2}")
    _check(("file", "bus-tokens.json") not in r2 and ("dir", "bus-lanes") not in r2 and (".mk-home" not in {t for _, t in r2}),
           "_root_cleanup: a kept item is also listed for removal")
    _rc_write(os.path.join(h2, "bus.json"), json.dumps({"pid": os.getpid()}))  # an old bus that is alive
    p3 = root_cleanup_plan(h2)
    _check(not [i for i in p3["remove"] if i["kind"] in ("file", "dir") and i["target"] in CLEANUP_BUS_FILES + ("bus-lanes",)]
           and "still running" in {(i["target"]): i["reason"] for i in p3["keep"]}.get("bus.lock", ""),
           f"_root_cleanup: a running old bus does not keep its files: {p3['remove']}")
    # a table that gains a row between the list and the apply is not dropped
    h3, _r, _t = _rc_home(base, "h3")
    con = open_root(h3, version=2)
    try:
        con.execute("INSERT INTO cards(project, run, task) VALUES ('p','r','t')")
        con.commit()
    finally:
        con.close()
    _check(("table", "cards") not in {(i["kind"], i["target"]) for i in root_cleanup_plan(h3)["remove"]},
           "_root_cleanup: a table with a row is listed for removal")
    res3 = root_cleanup_apply(h3, os.path.join(base, "bk3"))
    _check("cards" not in res3["removed"]["tables"] and "cards" in _rc_dump(h3), "_root_cleanup: a table with rows was dropped")
    # root schema 3 on its own: a v2 root migrates and loses only the EMPTY dropped tables
    h4, _r, _t = _rc_home(base, "h4")
    open_root(h4).close()
    d4 = _rc_dump(h4)
    _check("cards" not in d4 and "project_summary" not in d4 and d4["sync"] == [["keepme", 3]],
           f"_root_cleanup: _root_v3 dropped/kept the wrong tables: {sorted(d4)}")
# --- end L10.9 root-cleanup self-tests ---


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
    db = _mkpaths().root_db_in(old_home)
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
    ref = _open_read(_mkpaths().root_db_in(old_home), ROOT_SCHEMA)  # already migrated; read-only
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
    _check(not os.path.exists(_mkpaths().root_db_in(bare)), "overview created mk.db")

    # a read must not write or migrate: hash the store, read, hash again
    db = _mkpaths().db_in(seeded)
    before = {n: _file_hash(os.path.join(seeded, n)) for n in _mkpaths().DB_FILE_NAMES}
    overview(seeded)
    after = {n: _file_hash(os.path.join(seeded, n)) for n in _mkpaths().DB_FILE_NAMES}
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
    _check(os.path.isfile(_mkpaths().root_db(mkh)), "the production path created no root store")
    ran = subprocess.run(base + ["overview", "--json"], env=env, capture_output=True, text=True)
    _check(ran.returncode == 0, f"overview exited {ran.returncode}: {ran.stderr.strip()[-300:]}")
    got = json.loads(ran.stdout)
    _check([p["slug"] for p in got["projects"]] == ["prod"], f"overview projects {got['projects']}")
    _check(got["projects"][0]["db_path"] is None, f"db_path {got['projects'][0]['db_path']!r}")
    _check(len(got["plans"]) == 1 and got["plans"][0]["plan"] == "p", f"overview plans {got['plans']}")
    _check(got["plans"][0]["counts"] == {"done": 1}, f"overview counts {got['plans'][0]['counts']}")
    _check(got["plans"][0]["live_run"] == "r", f"overview live_run {got['plans'][0]['live_run']!r}")
    _check(os.path.realpath(_mkpaths().root_db(mkh)).startswith(os.path.realpath(fake_home) + os.sep),
           f"the store landed outside the temp $HOME: {_mkpaths().root_db(mkh)}")


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
    """Y1: projects and timeline never create a store, and a read never writes or migrates one."""
    empty = os.path.join(home, "empty-read")
    os.makedirs(empty)
    before = _listing(empty)
    got = json.loads(subprocess.check_output(
        [sys.executable, __file__, "--home", empty, "projects", "--json"], text=True))
    _check(got == {"ok": True, "projects": []}, f"empty projects {got}")
    got = json.loads(subprocess.check_output(
        [sys.executable, __file__, "--home", empty, "--project", "ghost",
         "timeline", "--run", "r", "--task", "t", "--json"], text=True))
    _check(got == {"ok": True, "events": []}, f"empty timeline {got}")
    _check(_listing(empty) == before == [], f"empty home grew: {_listing(empty)}")
    _check(projects(empty) == [] and overview(empty)["plans"] == [] and timeline(empty, "ghost", "r", "t") == [],
           "api read created a store")
    _check(_listing(empty) == [], "api read created a file")

    data = os.path.join(home, "ro-read")
    con = open_project(home, data_dir=data)
    try:
        add_task(con, "r", "t", "task", title="kept")
    finally:
        con.close()
    transition(home, "ignored", "r", "t", "building", note="hello", data_dir=data)
    db = _mkpaths().db_in(data)
    hashes = {name: _file_hash(os.path.join(data, name)) for name in _mkpaths().DB_FILE_NAMES}
    got = timeline(home, "ignored", "r", "t", data)
    _check(any(e["text"] == "hello" for e in got), f"ro timeline {got}")
    after = {name: _file_hash(os.path.join(data, name)) for name in _mkpaths().DB_FILE_NAMES}
    _check(after == hashes, f"read wrote the store: {hashes} -> {after}")

    old = os.path.join(home, "ro-old")
    con = open_project(home, data_dir=old, version=1, migrations=())
    try:
        add_task(con, "r", "t", "task", title="old")
    finally:
        con.close()
    old_db = _mkpaths().db_in(old)
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
    bare = sqlite3.connect(_mkpaths().root_db_in(root_old))
    bare.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
    bare.commit()
    bare.close()
    root_hash = _file_hash(_mkpaths().db_in(root_old))
    try:
        overview(root_old)
    except Fail as e:
        _check(e.code == 2 and "needs migration" in str(e), f"old root error: {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: an unstamped root store was read without a migration error")
    _check(_file_hash(_mkpaths().db_in(root_old)) == root_hash, "reading an old root wrote it")
    _check(not os.path.exists(os.path.join(root_old, "mk.db-wal")), "reading an old root created a wal")

    # Y1 R2: a commit sitting in the WAL, not yet checkpointed, must be visible to a read.
    # The reviewer's repro: wal_autocheckpoint=0, one committed insert, then timeline.
    wal = os.path.join(home, "ro-wal")
    con = open_project(home, data_dir=wal)
    try:
        add_task(con, "r", "t", "task", title="initial")
    finally:
        con.close()
    wal_db = _mkpaths().db_in(wal)
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
    held_db = _mkpaths().db_in(held)
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
    _check(not os.path.exists(_mkpaths().root_db_in(bare)), "projects created mk.db")


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
    names = _mkpaths().DB_FILE_NAMES
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

    names = _mkpaths().DB_FILE_NAMES
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
                         " VALUES (?,'demo','',?,'home',?)", (slug, data, _mkpaths().db_in(c2)))
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
    _check(_store_path(home, data_dir=data) == _mkpaths().db_in(c2),
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
    _check(_store_path(home, data_dir=plain) == _mkpaths().db_in(plain),
           "a pre-migration project lost its own store path")
    _check(run_snapshot(home, data_dir=plain)["active"] == "p", "the pre-migration read failed")


def _run_snapshot_extra_json(home):
    """run-snapshot() reads the file-only keys off runs.extra_json and tasks.extra_json (L10.T).

    mk-state stopped writing the run-doc artifact (bf55ee0): feature, waiting, recent and the plan
    link live on the run row's extra_json, wave/gates/rounds/last_commit/reviewer on each task
    row's. A run with an artifact-less store and extras still reads them; a run with neither reads
    its defaults (feature null, no waiting, empty recent) and never errors.
    """
    data = os.path.join(home, "snap-extra")
    con = open_project(home, data_dir=data)
    try:
        run_x = {"feature": "Extra lane", "plan_id": "p-x", "plan_sha": "f00d", "plan_status": "running",
                 "waiting": {"question": "Ship it?", "options": ["yes", "no"], "rec": "yes"},
                 "recent": [{"t": "2026-02-01T01:00:00+00:00", "run": "r-x", "task": "X1",
                             "type": "note", "text": "from extras", "by": "lane-watch"}],
                 "task_order": ["X2", "X1"]}
        con.execute("INSERT INTO runs(id, plan, branch, status, stage, next, started, extra_json)"
                    " VALUES ('r-x','p-x','mk/x','active','3','merge X','2026-02-01T00:00:00+00:00',?)",
                    (json.dumps(run_x),))
        con.execute("INSERT INTO runs(id, plan, branch, status, stage, next, started)"
                    " VALUES ('r-n','p-n','mk/n','done','1','none','2026-01-01T00:00:00+00:00')")
        con.commit()  # add_task opens its own transaction
        add_task(con, "r-x", "X1", "task", title="One")
        add_task(con, "r-x", "X2", "task", title="Two", deps="X1")
        con.execute("UPDATE tasks SET extra_json=? WHERE run='r-x' AND id='X1'", (json.dumps(
            {"wave": 2, "gates": {"tests": "green"}, "last_commit": "1de932a", "reviewer": "agy",
             "rounds": [{"n": 1, "kind": "build", "sha": "1de932a"}]}),))
        con.execute("UPDATE tasks SET extra_json=? WHERE run='r-x' AND id='X2'", (json.dumps(
            {"wave": 3, "gates": {}, "last_commit": None, "reviewer": "opencode", "rounds": []}),))
        con.commit()
        add_task(con, "r-n", "N1", "task", title="Bare")
        con.commit()
        _check(con.execute("SELECT COUNT(*) FROM artifacts WHERE kind='run-doc'").fetchone()[0] == 0,
               "the fixture holds a run-doc artifact")
    finally:
        con.close()

    got = run_snapshot(home, data_dir=data, run="r-x")
    run = got["run"]
    _check(got["runs"][1]["feature"] == "Extra lane", f"index feature from extra_json: {got['runs']}")
    _check(run["feature"] == "Extra lane", f"feature {run['feature']!r}")
    _check(run["waiting"]["question"] == "Ship it?" and run["waiting"]["options"] == ["yes", "no"],
           f"waiting {run['waiting']}")
    _check(len(run["recent"]) == 1 and run["recent"][0]["text"] == "from extras", f"recent {run['recent']}")
    _check(run["plan_id"] == "p-x" and run["plan_sha"] == "f00d" and run["plan_status"] == "running",
           f"plan link {run}")
    _check("task_order" not in run, f"task_order leaked into the run: {run}")
    _check([t["id"] for t in run["tasks"]] == ["X2", "X1"], f"task order {[t['id'] for t in run['tasks']]}")
    x1, x2 = run["tasks"][1], run["tasks"][0]
    _check(x1["wave"] == 2 and x1["gates"] == {"tests": "green"} and x1["last_commit"] == "1de932a"
           and x1["reviewer"] == "agy" and x1["rounds"][0]["sha"] == "1de932a", f"X1 extras {x1}")
    _check(x2["wave"] == 3 and x2["reviewer"] == "opencode" and x2["rounds"] == []
           and x2["last_commit"] is None and x2["deps"] == ["X1"], f"X2 extras {x2}")

    # no artifact and no extras: the defaults, no error
    bare = run_snapshot(home, data_dir=data, run="r-n")["run"]
    _check(bare["feature"] is None and bare["waiting"] is None and bare["recent"] == []
           and bare["plan_id"] is None, f"bare run defaults {bare}")
    _check([t["id"] for t in bare["tasks"]] == ["N1"] and bare["tasks"][0]["title"] == "Bare"
           and "wave" not in bare["tasks"][0], f"bare tasks {bare['tasks']}")


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
    _check(not os.path.exists(_mkpaths().db_in(missing)), "fixture has a store")

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
    _check(not os.path.exists(_mkpaths().db_in(missing)), "the no-store read created a store")


def _plans_timeline_migrated(home):
    """plans() and timeline() follow the switch to the C2 store, like run_snapshot() (Z6).

    The _run_snapshot_migrated fixture with plan rows: a migrated project whose pre-switch copy
    is still sitting in the data dir, holding DIFFERENT plan, task and event rows. A pane holding
    the data dir reads the root row's db_path — the live store — or mk-plan, the dashboard and
    wake would publish the stale plan, the stale task status and the stale events of a project
    that moved to the C2 tree long ago.
    """
    data = os.path.join(home, "plans-mig", "data")   # the legacy location, post-switch
    slug = "demo2"
    c2 = os.path.join(home, "projects", slug)        # the C2 project folder
    os.makedirs(data, exist_ok=True)
    os.makedirs(c2, exist_ok=True)
    live = open_project(home, data_dir=c2)           # the live store, in the C2 folder
    stale = open_project(home, data_dir=data)        # the pre-switch copy, left in the data dir
    try:
        live.execute("INSERT INTO plans(id, version, title, header_json)"
                     " VALUES ('p1',1,'Live plan','{}')")
        live.execute("INSERT INTO runs(id, plan, status, started)"
                     " VALUES ('r-live','p1','active','2026-03-01T00:00:00Z')")
        live.commit()
        add_task(live, "r-live", "ph1", "phase", title="Phase one")
        add_task(live, "r-live", "t1", "task", title="Live task", parent="ph1", status="merged")
        stale.execute("INSERT INTO plans(id, version, title, header_json)"
                      " VALUES ('p1',1,'Stale plan','{}')")
        stale.execute("INSERT INTO runs(id, plan, status, started)"
                      " VALUES ('r-stale','p1','active','2026-03-02T00:00:00Z')")
        stale.commit()
        add_task(stale, "r-stale", "ph1", "phase", title="Phase one")
        add_task(stale, "r-stale", "t1", "task", title="Stale task", parent="ph1", status="ready")
        for con, run, text in ((live, "r-live", "live merged"), (stale, "r-stale", "stale ready")):
            con.execute("INSERT INTO events(run, task, t, chan, kind, text)"
                        " VALUES (?,?,?,?,?,?)",
                        (run, "t1", "2026-03-03T00:00:00Z", "state", "transition", text))
            con.commit()
        # the root row: this project's live store is db_path (L2's switch marker)
        root = open_root(home)
        try:
            root.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path)"
                         " VALUES (?,'demo','',?,'home',?)", (slug, data, _mkpaths().db_in(c2)))
            root.commit()
        finally:
            root.close()
    finally:
        live.close()
        stale.close()

    # plans(): the detail reply is the live plan and the live task's merged status
    got = plans(home, data_dir=data, plan_id="p1")
    _check(got["ok"] is True and got["id"] == "p1", f"plans detail id {got.get('id')}")
    _check(got["title"] == "Live plan", f"plans read the pre-switch store: {got['title']}")
    t1 = got["phases"][0]["tasks"][0]
    _check(t1["id"] == "t1" and t1["status"] == "merged", f"plans task {t1}")
    _check(got["counts"] == {"merged": 1}, f"plans counts {got['counts']}")
    _check(got["runs"] == ["r-live"] and got["live_run"] == "r-live", f"plans runs {got['runs']}")
    # the list reply follows the same store: one plan, counted off the live run
    listing = plans(home, data_dir=data)
    _check([p["id"] for p in listing["plans"]] == ["p1"], f"plans list {listing}")
    _check(listing["plans"][0]["title"] == "Live plan"
           and listing["plans"][0]["counts"] == {"merged": 1},
           f"plans list entry {listing['plans'][0]}")
    # timeline(): the live store's events, and none of the stale copy's
    feed = timeline(home, slug, "r-live", "t1", data_dir=data)
    _check([e["text"] for e in feed] == ["live merged"], f"timeline {feed}")
    _check(timeline(home, slug, "r-stale", "t1", data_dir=data) == [],
           "timeline read the pre-switch store in the data dir")
    # the CLI reaches the same store through --data (--project only names the project)
    _check(_cli(["plans", "p1", "--json", "--home", home, "--project", slug,
                 "--data", data]) == got, "cli plans differs from the api")
    _check(_cli(["timeline", "--json", "--home", home, "--project", slug, "--data", data,
                 "--run", "r-live", "--task", "t1"]) == {"ok": True, "events": feed},
           "cli timeline differs from the api")
    # and a read still leaves the pre-switch copy untouched: no write, no migration
    names = _mkpaths().DB_FILE_NAMES
    before = {n: _file_hash(os.path.join(data, n)) for n in names}
    plans(home, data_dir=data)
    timeline(home, slug, "r-live", "t1", data_dir=data)
    after = {n: _file_hash(os.path.join(data, n)) for n in names}
    _check(after == before, f"the reader wrote the pre-switch store: {before} -> {after}")


def _plans_timeline_premigration(home):
    """A pre-migration project keeps <data>/mk.db for plans() and timeline() (Z6).

    The reader follows the switch only when the root row says so. A row that is registered but
    never switched (db_path NULL) must not move plans() or timeline() off the store in the
    project's own data dir, or a project that has not migrated loses its plans and events.
    """
    data = os.path.join(home, "plans-plain")
    con = open_project(home, data_dir=data)
    try:
        con.execute("INSERT INTO plans(id, version, title, header_json)"
                    " VALUES ('p1',1,'Plain plan','{}')")
        con.execute("INSERT INTO runs(id, plan, status, started)"
                    " VALUES ('r','p1','active','2026-03-01T00:00:00Z')")
        con.commit()
        add_task(con, "r", "ph1", "phase", title="Phase one")
        add_task(con, "r", "t1", "task", title="Plain task", parent="ph1", status="ready")
        con.execute("INSERT INTO events(run, task, t, chan, kind, text)"
                    " VALUES ('r','t1','2026-03-03T00:00:00Z','state','transition','plain ready')")
        con.commit()
    finally:
        con.close()
    # registered, never switched: the row is there and its db_path is NULL
    root = open_root(home)
    try:
        root.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path)"
                     " VALUES ('plain','Plain','',?,'home',NULL)", (data,))
        root.commit()
    finally:
        root.close()

    _check(_store_path(home, data_dir=data) == _mkpaths().db_in(data),
           f"a pre-migration project lost its own store: {_store_path(home, data_dir=data)}")
    got = plans(home, data_dir=data, plan_id="p1")
    _check(got["title"] == "Plain plan", f"pre-migration plans {got}")
    _check(got["phases"][0]["tasks"][0]["status"] == "ready", f"pre-migration task {got}")
    _check(got["counts"] == {"ready": 1} and got["live_run"] == "r",
           f"pre-migration counts {got['counts']}")
    feed = timeline(home, "plain", "r", "t1", data_dir=data)
    _check([e["text"] for e in feed] == ["plain ready"], f"pre-migration timeline {feed}")
    _check(_cli(["plans", "p1", "--json", "--home", home, "--project", "plain",
                 "--data", data]) == got, "cli plans on a pre-migration project")
    _check(_cli(["timeline", "--json", "--home", home, "--project", "plain", "--data", data,
                 "--run", "r", "--task", "t1"]) == {"ok": True, "events": feed},
           "cli timeline on a pre-migration project")


def _plans_timeline_no_store(home):
    """plans() and timeline() on a project with NO store answer as they always have (Z6).

    plans() answers an empty list (exit 0), a named plan a Fail (exit 2) and timeline() an empty
    list. Neither answer may create the data dir, the store or a WAL on the way, or a pane
    pointed at a fresh project would materialise an empty store and read it as a real one.
    """
    missing = os.path.join(home, "plans-none")
    _check(not os.path.exists(missing), "fixture dir already exists")
    _check(plans(home, data_dir=missing) == {"ok": True, "plans": []},
           f"plans on a missing store: {plans(home, data_dir=missing)}")
    try:
        plans(home, data_dir=missing, plan_id="p1")
    except Fail as e:
        _check(e.code == 2 and "no store" in str(e), f"a named plan on a missing store: {e}")
    else:
        raise SystemExit("SELF-TEST FAIL: plans named a plan in a store that does not exist")
    _check(timeline(home, "ghost", "r", "t", data_dir=missing) == [],
           f"timeline on a missing store: {timeline(home, 'ghost', 'r', 't', missing)}")
    _check(_cli(["plans", "--json", "--home", home, "--data", missing]) == {"ok": True, "plans": []},
           "cli plans on a missing store")
    _check(_cli(["timeline", "--json", "--home", home, "--project", "ghost", "--data", missing,
                 "--run", "r", "--task", "t"]) == {"ok": True, "events": []},
           "cli timeline on a missing store")
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), "plans", "p1", "--json",
                           "--home", home, "--data", missing], capture_output=True, text=True)
    _check(proc.returncode == 2 and json.loads(proc.stdout)["ok"] is False,
           f"cli named plan on a missing store rc={proc.returncode}: {proc.stdout!r}")

    # an existing but empty data dir gains nothing either
    empty = os.path.join(home, "plans-empty")
    os.makedirs(empty)
    _check(plans(home, data_dir=empty) == {"ok": True, "plans": []}, "plans on an empty data dir")
    _check(timeline(home, "ghost", "r", "t", data_dir=empty) == [], "timeline on an empty data dir")
    _check(_cli(["plans", "--json", "--home", home, "--data", empty]) == {"ok": True, "plans": []},
           "cli plans on an empty data dir")
    _check(os.listdir(empty) == [], f"the empty data dir grew: {os.listdir(empty)}")
    _check(not os.path.exists(missing), f"the missing-store read created {missing}")


def _run_snapshot_zero_runs(home):
    """A real store that holds no run is store=true — not the no-store answer (L5).

    The two replies differ in exactly one key, so this is what keeps the file fallback honest:
    a project that has a store must never read as one that has none.
    """
    data = os.path.join(home, "zero-runs")
    con = open_project(home, data_dir=data)    # a real store, no rows
    con.close()
    _check(os.path.exists(_mkpaths().db_in(data)), "the empty store was not created")

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
            if any("sqlite3.connect" in line and "mode=ro" not in line and "RO_AUDIT" not in line for line in fh):
                offenders.append(name)
    _check(offenders == [], f"read-write sqlite outside mkdb.py: {offenders}")


# failure injection for the crash tests — env so the worker processes see it too.
# L7.3: the pre-root hook now guards the plan_summary root write (the card write is gone).
_real_plan_root, _real_change_log = _write_plan_summaries_root, _change_log


def _guarded_plan_root(*a, **k):
    if os.environ.get("MKDB_FAIL_BEFORE_ROOT"):
        raise Fail("injected failure before the root upsert")
    return _real_plan_root(*a, **k)


def _guarded_change_log(con, run, task):
    if os.environ.get("MKDB_FAIL_IN_TXN"):
        raise Fail("injected failure inside the project transaction")
    return _real_change_log(con, run, task)


_write_plan_summaries_root, _change_log = _guarded_plan_root, _guarded_change_log


if __name__ == "__main__":
    try:
        sys.exit(self_test() if sys.argv[1:] == ["--self-test"] else main(sys.argv[1:]))
    except Fail as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(e.code)
