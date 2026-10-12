#!/usr/bin/env python3
"""MatrixKoder SQLite stores — the ONLY module that opens a store read-write. Python 3 stdlib only.

  timeline --project P --run R --task T --json
  cards [--project P] --json
  --self-test

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
import argparse, contextlib, datetime, hashlib, json, multiprocessing, os, re, sqlite3, subprocess, sys, tempfile

sys.dont_write_bytecode = True

SCHEMA = 3
ROOT_SCHEMA = 1  # open_root stamps v1 and never migrates the root store; project stores go to SCHEMA
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
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

ROOT_DDL = """
CREATE TABLE IF NOT EXISTS projects(
  slug TEXT PRIMARY KEY, name TEXT, repo TEXT, data_dir TEXT, mode TEXT, created TEXT, last_activity TEXT);
CREATE TABLE IF NOT EXISTS cards(
  project TEXT, run TEXT, task TEXT, parent TEXT, type TEXT, title TEXT, status TEXT, col TEXT,
  attention INTEGER, question TEXT, route_json TEXT, lane TEXT, round INTEGER, verdict TEXT,
  gates TEXT, updated TEXT, change_no INTEGER, PRIMARY KEY(project, run, task));
CREATE TABLE IF NOT EXISTS project_summary(
  project TEXT PRIMARY KEY, active_runs INTEGER, by_status_json TEXT, attention INTEGER,
  last_event TEXT, change_no INTEGER);
CREATE TABLE IF NOT EXISTS sync(project TEXT PRIMARY KEY, last_change_no INTEGER);
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
"""
PROJECT_DDL = """
CREATE TABLE IF NOT EXISTS plans(
  id TEXT PRIMARY KEY, version INTEGER, title TEXT, header_json TEXT, body_md TEXT,
  approved_at TEXT, archived_path TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, plan TEXT, plan_version INTEGER, branch TEXT, status TEXT, stage TEXT,
  next TEXT, started TEXT, ended TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  run TEXT, id TEXT, parent TEXT, type TEXT, title TEXT, acceptance TEXT, owned TEXT, deps TEXT,
  parallel INTEGER, route_json TEXT, status TEXT, prev_status TEXT, file_status TEXT, lane TEXT, slot TEXT, branch TEXT, pane TEXT,
  round INTEGER, verdict TEXT, updated TEXT, PRIMARY KEY(run, id));
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


def resolve_home(arg):
    home = os.path.abspath(os.path.expanduser(arg or os.environ.get("MK_HOME") or "~/.matrixkoder"))
    check_home(home)
    return home


def project_dir(home, slug=None, data_dir=None):
    """--data wins; else <home>/projects/<slug>. The slug is checked before the path exists.

    Both routes pass check_home before anything is created: a synced or networked home, and a
    --data dir that itself looks synced, are refused (C11).
    """
    if data_dir:
        path = os.path.abspath(os.path.expanduser(data_dir))
        check_home(path)
        return path
    if not slug:
        raise Fail("pass --project or --data")
    check_slug(slug)
    check_home(home)
    return os.path.join(home, "projects", slug)


def _connect(path):
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


def open_root(home, version=SCHEMA, migrations=()):
    """Open or create the root store <home>/mk.db (C1)."""
    check_home(home)
    return _migrate(_connect(os.path.join(home, "mk.db")), version, ((1, ROOT_DDL),) + tuple(migrations))


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


def open_project(home, slug=None, data_dir=None, version=SCHEMA, migrations=()):
    """Open or create the project store (C2). check_home runs before any directory is created."""
    path = os.path.join(project_dir(home, slug, data_dir), "mk.db")
    con = _migrate(_connect(path), version, ((1, PROJECT_DDL), (2, _v2), (3, _v3)) + tuple(migrations))
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


def _repair_cards(home, project, repairs, data_dir):
    """Write every mismatched card and advance the watermark in ONE root transaction.

    change_no is the task's own MAX(change_log), which can sit below the watermark the
    triggering transition already advanced. The watermark moves to at least that number and
    at least its current value, so a later catch_up still replays anything newer.
    """
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
        if sync_root:
            _upsert_card(home, project, run, task, row, to, col, attention, round, change_no, data_dir,
                         updated=stamped)
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
                  updated=None):
    """C5: the root copy, in its own transaction. A failure here leaves the project change durable."""
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
    return n


RUN_COLS = ("id", "plan", "plan_version", "branch", "status", "stage", "next", "started", "ended")
TASK_COLS = ("id", "parent", "type", "title", "acceptance", "owned", "deps", "parallel",
             "route_json", "status", "lane", "slot", "branch", "pane", "round", "verdict", "updated")
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
    artifacts.src column) and a repeat imports nothing. Tasks and the run row upsert. sync_root
    stays off — change_log rows are written so catch_up() can build the cards later, and nothing
    here touches the root store. Returns counts of rows actually added.
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
                con.execute(
                    "INSERT INTO runs(" + ",".join(RUN_COLS) + ") VALUES (" + ",".join("?" * len(RUN_COLS)) + ")"
                    " ON CONFLICT(id) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in RUN_COLS if c != "id"),
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
                con.execute(
                    "INSERT INTO tasks(" + ",".join(cols) + ") VALUES (" + ",".join("?" * len(cols)) + ")"
                    " ON CONFLICT(run, id) DO UPDATE SET " + ",".join(
                        f"{c}=excluded.{c}" for c in cols if c not in ("run", "id")),
                    tuple(values))
                if not existed:
                    added["tasks"] += 1
                    _change_log(con, run, t["id"])
                else:
                    # A re-import logs a change only when a column actually moved, so catch_up()
                    # rebuilds the card; identical data adds nothing, change_log included.
                    changed = any(existed[c] != v for c, v in zip(cols, values))
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
    finally:
        con.close()
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


def _cli(argv):
    # The common options are accepted before or after the subcommand. They are pulled out first,
    # so a subcommand never sees them and the two positions cannot disagree.
    common = Parser(prog="mkdb.py", add_help=False)
    common.add_argument("--data", help="project data dir (overrides --project)")
    common.add_argument("--home", help="MK home (default $MK_HOME or ~/.matrixkoder)")
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
    raise Fail("usage: mkdb.py timeline|cards|import-history --json  (or --self-test)")


def main(argv):
    out = _cli(argv)
    print(json.dumps(out))
    return 0


# --- self-test ----------------------------------------------------------------

V3_SQL = "CREATE TABLE IF NOT EXISTS mkdb_v3_probe(id INTEGER PRIMARY KEY);"  # test migration past shipped v3, not shipped


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


def self_test():
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home")
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
        _live_writers(home)
        _v1_upgrade(home)
        _lane_slots_v3(home)
        _card_verdict(home)
        _reads_create_nothing(home)
        _single_writer()
        _ready(home)
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
        _check(again.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == 3, "re-open")
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
        _check(reopened.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] == 3,
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
        _check(again.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == 3, "reopen grew")
        _check(third.execute("SELECT title FROM tasks WHERE id='kept'").fetchone()["title"] == "keep me",
               "second reopen lost a row")
    finally:
        again.close()
        third.close()
    data = os.path.join(home, "mig")
    first = open_project(home, data_dir=data, version=4, migrations=((4, V3_SQL),))
    _check(first.execute("SELECT 1 FROM sqlite_master WHERE name='mkdb_v3_probe'").fetchone(), "v2")
    first.close()
    second = open_project(home, data_dir=data, version=4, migrations=((4, V3_SQL),))
    _check(second.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == 4, "idempotent")
    second.close()
    # Finding 8: a crash between executescript and the version stamp re-runs safely.
    con = sqlite3.connect(os.path.join(data, "mk.db"))
    con.execute("DELETE FROM schema_version WHERE version=4")
    con.commit()
    con.close()
    third = open_project(home, data_dir=data, version=4, migrations=((4, V3_SQL),))
    _check(third.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == 4, "re-stamp")
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
        _check(reopened.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] == 3,
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
        _check(again.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == 3, "v3 reopen grew")
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
        _check(upgraded.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] == 3, "not v3")
        _check("src" in [r["name"] for r in upgraded.execute("PRAGMA table_info(artifacts)")], "src missing")
        row = upgraded.execute("SELECT title FROM tasks WHERE id='kept'").fetchone()
        _check(row["title"] == "keep me", f"row lost: {dict(row) if row else None}")
    finally:
        upgraded.close()


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
