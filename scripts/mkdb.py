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
import argparse, datetime, json, multiprocessing, os, re, sqlite3, subprocess, sys, tempfile

sys.dont_write_bytecode = True

SCHEMA = 1
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
  parallel INTEGER, route_json TEXT, status TEXT, prev_status TEXT, lane TEXT, slot TEXT, branch TEXT, pane TEXT,
  round INTEGER, verdict TEXT, updated TEXT, PRIMARY KEY(run, id));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, run TEXT, task TEXT, t TEXT, chan TEXT, kind TEXT, from_id TEXT,
  to_id TEXT, reply_to TEXT, text TEXT, artifact_id INTEGER, meta_json TEXT);
CREATE INDEX IF NOT EXISTS ev_task ON events(run, task, id);
CREATE TABLE IF NOT EXISTS artifacts(
  id INTEGER PRIMARY KEY, run TEXT, task TEXT, round INTEGER, kind TEXT, rev INTEGER, text TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS acks(run TEXT, msg TEXT, lane TEXT, level INTEGER, t TEXT, PRIMARY KEY(run, msg, lane));
CREATE TABLE IF NOT EXISTS lane_slots(slot TEXT PRIMARY KEY, lane_path TEXT, state TEXT, run TEXT, task TEXT);
CREATE TABLE IF NOT EXISTS waits(id INTEGER PRIMARY KEY, run TEXT, question TEXT, opened TEXT, cleared TEXT);
CREATE TABLE IF NOT EXISTS change_log(change_no INTEGER PRIMARY KEY, run TEXT, task TEXT, t TEXT);
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
"""


class Fail(Exception):
    def __init__(self, msg, code=2):
        super().__init__(msg)
        self.code = code


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
    for n, sql in migrations:
        if n <= have or n > version:
            continue
        # executescript() commits on its own, so the version stamp is a second transaction:
        # a crash between them re-runs the migration, which is why every migration is idempotent.
        con.executescript(sql)
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


def open_project(home, slug=None, data_dir=None, version=SCHEMA, migrations=()):
    """Open or create the project store (C2). check_home runs before any directory is created."""
    path = os.path.join(project_dir(home, slug, data_dir), "mk.db")
    con = _migrate(_connect(path), version, ((1, PROJECT_DDL),) + tuple(migrations))
    _ensure_prev_status(con)
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


def _rollup(con, run, task):
    """A parent's status follows its children: attention wins, else the least-done column.

    An attention child keeps the parent on its own previous non-attention status when it has
    one, so a blocked child does not drag a building parent back to Backlog.
    """
    kids = con.execute("SELECT status, prev_status FROM tasks WHERE run=? AND parent=?",
                       (run, task)).fetchall()
    if not kids:
        return None
    statuses = [k["status"] for k in kids]
    for flag in ATTENTION:
        if flag in statuses:
            row = con.execute("SELECT prev_status FROM tasks WHERE run=? AND id=?",
                              (run, task)).fetchone()
            if row and row["prev_status"] in COLUMNS:
                return row["prev_status"]
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


def transition(home, project, run, task, to, note="", round=0, sync_root=False, data_dir=None):
    """The ONLY status writer (C10). One project transaction, then the root card iff sync_root."""
    con = open_project(home, project, data_dir)
    try:
        row = con.execute(
            "SELECT parent, type, title, route_json, lane, status, prev_status FROM tasks"
            " WHERE run=? AND id=?", (run, task)).fetchone()
        if not row:
            raise Fail(f"no task {run}:{task}")
        # An attention status keeps the column of the previous non-attention status (C4).
        col, attention = column_of(to, row["prev_status"] if to in ATTENTION else None)
        kept = row["prev_status"] if to in ATTENTION else (to if to in COLUMNS else row["prev_status"])
        _begin(con)
        try:
            con.execute(
                "UPDATE tasks SET status=?, prev_status=?, round=?, updated=? WHERE run=? AND id=?",
                (to, kept, round, now(), run, task))
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
            _upsert_card(home, project, run, task, row, to, col, attention, round, change_no, data_dir)
        rolled = _rollup(con, run, row["parent"]) if row["parent"] else None
        if rolled and rolled != con.execute("SELECT status FROM tasks WHERE run=? AND id=?",
                                            (run, row["parent"])).fetchone()["status"]:
            transition(home, project, run, row["parent"], rolled, note=f"rollup from {task}",
                       round=round, sync_root=sync_root, data_dir=data_dir)
    finally:
        con.close()
    return change_no


def _upsert_card(home, project, run, task, row, status, col, attention, round, change_no, data_dir):
    """C5: the root copy, in its own transaction. A failure here leaves the project change durable."""
    root = open_root(home)
    try:
        _begin(root)
        try:
            root.execute(
                "INSERT INTO cards(project, run, task, parent, type, title, status, col, attention,"
                " route_json, lane, round, updated, change_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(project, run, task) DO UPDATE SET status=excluded.status, col=excluded.col,"
                " attention=excluded.attention, round=excluded.round, updated=excluded.updated,"
                " change_no=MAX(cards.change_no, excluded.change_no)",
                (project, run, task, row["parent"], row["type"], row["title"], status, col, attention,
                 row["route_json"], row["lane"], round, now(), change_no))
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
    """Replay project changes the root has not seen yet (C5 sync.last_change_no)."""
    con = open_project(home, project, data_dir)
    try:
        rows = con.execute("SELECT t.run, t.id, t.parent, t.type, t.title, t.route_json, t.lane,"
                           " t.status, t.round, c.change_no FROM change_log c JOIN tasks t"
                           " ON t.run=c.run AND t.id=c.task ORDER BY c.change_no").fetchall()
    finally:
        con.close()
    root = open_root(home)
    try:
        seen = root.execute("SELECT last_change_no FROM sync WHERE project=?", (project,)).fetchone()
    finally:
        root.close()
    last = seen["last_change_no"] if seen else 0
    for r in rows:
        if r["change_no"] <= last:
            continue
        col, attention = column_of(r["status"])
        _upsert_card(home, project, r["run"], r["id"], r, r["status"], col, attention,
                     r["round"], r["change_no"], data_dir)
    return len([r for r in rows if r["change_no"] > last])


def timeline(home, project, run, task, data_dir=None):
    """SELECT … FROM events WHERE run=? AND task=? ORDER BY id."""
    con = open_project(home, project, data_dir)
    try:
        return [dict(r) for r in con.execute(
            "SELECT id, run, task, t, chan, kind, to_id, text FROM events"
            " WHERE run=? AND task=? ORDER BY id", (run, task))]
    finally:
        con.close()


def cards(home, project=None):
    """Read the root store's cards, optionally one project."""
    root = open_root(home)
    try:
        if project:
            rows = root.execute("SELECT * FROM cards WHERE project=? ORDER BY change_no", (project,))
        else:
            rows = root.execute("SELECT * FROM cards ORDER BY project, change_no")
        return [dict(r) for r in rows]
    finally:
        root.close()


def _cli(argv):
    p = Parser(prog="mkdb.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", help="project data dir (overrides --project)")
    p.add_argument("--home", help="MK home (default $MK_HOME or ~/.matrixkoder)")
    p.add_argument("--project", help="project slug")
    sub = p.add_subparsers(dest="cmd")
    tl = sub.add_parser("timeline")
    tl.add_argument("--run", required=True)
    tl.add_argument("--task", required=True)
    tl.add_argument("--json", action="store_true")
    cd = sub.add_parser("cards")
    cd.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    home = resolve_home(args.home)
    if args.data:  # C11: refuse a synced or networked --data dir before any directory is created
        project_dir(home, data_dir=args.data)
    if args.cmd == "timeline":
        if not args.project:
            raise Fail("timeline needs --project")
        return {"ok": True, "events": timeline(home, args.project, args.run, args.task, args.data)}
    if args.cmd == "cards":
        return {"ok": True, "cards": cards(home, args.project)}
    raise Fail("usage: mkdb.py timeline|cards --json  (or --self-test)")


def main(argv):
    out = _cli(argv)
    print(json.dumps(out))
    return 0


# --- self-test ----------------------------------------------------------------

V2_SQL = "CREATE TABLE IF NOT EXISTS mkdb_v2_probe(id INTEGER PRIMARY KEY);"  # test migration, not shipped


def _check(cond, msg):
    if not cond:
        raise SystemExit(f"SELF-TEST FAIL: {msg}")


def _pragmas(con):
    return (con.execute("PRAGMA journal_mode").fetchone()[0],
            con.execute("PRAGMA busy_timeout").fetchone()[0],
            con.execute("PRAGMA foreign_keys").fetchone()[0])


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
        _cycle_race(home)
        _tree(home)
        _reads(home)
        _crashes(home)
        _single_writer()
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
        _check(again.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == 1, "re-open")
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
    _check(got["phase"]["col"] == "Building", f"parent dragged off its column: {got.get('phase')}")
    transition(home, "attn", "r", "t", "building", sync_root=True, data_dir=data)
    got = {c["task"]: c for c in cards(home, "attn")}
    _check(got["t"]["col"] == "Building" and got["t"]["attention"] == 0, f"attention stuck {got.get('t')}")
    transition(home, "attn", "r", "fresh", "blocked", sync_root=True, data_dir=data)
    fresh = {c["task"]: c for c in cards(home, "attn")}["fresh"]
    _check(fresh["col"] == "Backlog" and fresh["attention"] == 1, f"no-history attention {fresh}")
    _check(column_of("blocked") == ("Backlog", 1), "column_of(blocked) without prev")
    _check(column_of("blocked", "building") == ("Building", 1), "column_of keeps prev column")


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
    data = os.path.join(home, "mig")
    first = open_project(home, data_dir=data, version=2, migrations=((2, V2_SQL),))
    _check(first.execute("SELECT 1 FROM sqlite_master WHERE name='mkdb_v2_probe'").fetchone(), "v2")
    first.close()
    second = open_project(home, data_dir=data, version=2, migrations=((2, V2_SQL),))
    _check(second.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == 2, "idempotent")
    second.close()
    # Finding 8: a crash between executescript and the version stamp re-runs safely.
    con = sqlite3.connect(os.path.join(data, "mk.db"))
    con.execute("DELETE FROM schema_version WHERE version=2")
    con.commit()
    con.close()
    third = open_project(home, data_dir=data, version=2, migrations=((2, V2_SQL),))
    _check(third.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == 2, "re-stamp")
    _check(third.execute("SELECT COUNT(*) c FROM mkdb_v2_probe").fetchone()["c"] == 0, "probe grew")
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
