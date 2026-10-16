#!/usr/bin/env python3
"""Live-test harness for the mk-store gates. Python 3 stdlib only.

  setup [--slug SLUG]
      TEMP MK home (tempfile, never ~/.matrixkoder), a scratch git repo with one commit,
      and the root and project stores via scripts/mkdb.py. One JSON line with every path.
      Writes a marker file listing exactly what it created.
  teardown --home DIR
      Removes the setup root (its home and scratch repo) and nothing else. The marker
      only proves the root is ours: a random token in the marker must match a second
      file inside the root. A path outside that root, a symlink, or a missing --home
      is refused.
  scripted --gate G1|G2|G3 [--home DIR]
      Scripted rows up to and including that gate (G1; G2 = G1+G2; G3 = every row).
      One JSON line per row (pass / fail / PENDING) and a summary line. A row whose
      feature is absent, or whose matrix cell says pending, is PENDING, never pass.
      The summary is ok only when fail and PENDING are both 0. Exit 0 when ok, 1 when
      any row failed, 3 when nothing failed but some rows are PENDING.
  --self-test

Common: --home DIR. A home that resolves to ~/.matrixkoder, or that is not under the
system temp dir, exits non-zero before anything is created.
Every command prints ONE JSON line. Errors: {"ok": false, "error": ...} exit 2.
The matrix is docs/live-test-mk-store.md. Store access goes through scripts/mkdb.py only.
"""
import argparse, json, os, secrets, shutil, subprocess, sys, tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mkpaths  # noqa: E402  C1: every MK path
import mkdb  # the only store opener; this file opens no database itself

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MATRIX = os.path.join(ROOT, "docs", "live-test-mk-store.md")
MARKER = "mk-livetest-marker.json"
GATES = ("G1", "G2", "G3")
REAL_HOME = os.path.realpath(os.path.abspath(os.path.expanduser("~/.matrixkoder")))
TOKEN = "mk-livetest-token"
GATE_RANK = {"G1": 1, "G2": 2, "G3": 3}


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


def temp_root():
    return os.path.realpath(tempfile.gettempdir())


def refuse_home(path):
    """Hard refusal: the real MK home, or anything outside the system temp dir."""
    home = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if home == REAL_HOME or REAL_HOME.startswith(home + os.sep):
        raise Fail(f"refusing the real MK home: {home}")
    root = temp_root()
    if home != root and not home.startswith(root + os.sep):
        raise Fail(f"refusing a home outside the system temp dir: {home}")
    return home


def matrix_rows():
    """One dict per matrix row. The self-test parses this for C1..C13.

    Cells are split on '|'. A regex cannot tell an empty pending cell from the next
    cell, because both are just pipes.
    """
    rows = []
    with open(MATRIX, encoding="utf-8") as fh:
        for line in fh:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 5 or not cells[0].startswith("C"):
                continue
            rows.append({"id": cells[0], "contract": cells[1], "gate": cells[2],
                         "kind": cells[3], "pending": cells[4]})
    return rows


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


def _marker_path(home):
    return os.path.join(home, MARKER)


def _write_marker(root, home, repo):
    """The marker only proves this root is ours. A second file holds the same token."""
    token = secrets.token_hex(16)
    with open(_marker_path(home), "w", encoding="utf-8") as fh:
        json.dump({"home": home, "repo": repo, "root": root, "token": token}, fh)
    with open(os.path.join(root, TOKEN), "w", encoding="utf-8") as fh:
        fh.write(token)


def _read_marker(home):
    """Confirm the root is ours. The marker is not a list of paths to delete."""
    path = _marker_path(home)
    if not os.path.isfile(path) or os.path.islink(path):
        raise Fail(f"no marker at {path}; teardown only removes what setup created")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if os.path.realpath(data.get("home", "")) != os.path.realpath(home):
        raise Fail("marker home does not match; refusing")
    root = os.path.realpath(data.get("root", ""))
    token_path = os.path.join(root, TOKEN)
    if os.path.islink(token_path) or not os.path.isfile(token_path):
        raise Fail("setup token missing; refusing")
    with open(token_path, encoding="utf-8") as fh:
        if fh.read().strip() != data.get("token"):
            raise Fail("setup token does not match; refusing")
    return data


def setup(slug="lt"):
    """Temp home, scratch repo with one commit, both stores. Returns the JSON payload."""
    mkdb.check_slug(slug)
    parent = tempfile.mkdtemp(prefix="mk-livetest-")
    home = refuse_home(os.path.join(parent, "home"))
    os.makedirs(home)
    repo = os.path.join(parent, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "lt0@example.com")
    _git(repo, "config", "user.name", "lt0")
    with open(os.path.join(repo, "README"), "w", encoding="utf-8") as fh:
        fh.write("scratch\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "scratch")
    data = os.path.join(home, "projects", slug)
    # Stores come from mkdb, which checks the slug before it builds a path.
    root = mkdb.open_root(home)
    root.close()
    proj = mkdb.open_project(home, slug)
    proj.close()
    _write_marker(parent, home, repo)
    return {"ok": True, "home": home, "repo": repo, "data": data, "slug": slug,
            "root_db": mkpaths.root_db_in(home), "project_db": mkpaths.project_db_in(data),
            "marker": _marker_path(home)}


def _inside(root, path):
    """True only when path is strictly inside root. Symlinks never count as inside."""
    root, path = os.path.realpath(root), os.path.realpath(path)
    return os.path.commonpath([root, path]) == root and path != root


def _unlink(path):
    """Remove a symlink as a link. Never follow it."""
    if os.path.islink(path):
        os.unlink(path)
        return True
    return False


def _check_root(root):
    """The setup root: a real directory named mk-livetest-* directly under the temp dir."""
    root = os.path.realpath(root)
    parent, name = os.path.split(root)
    if parent != temp_root() or not name.startswith("mk-livetest-"):
        raise Fail(f"refusing a setup root that is not mk-livetest-* under the temp dir: {root}")
    if os.path.islink(root) or not os.path.isdir(root):
        raise Fail(f"refusing a setup root that is not a real directory: {root}")
    if root in (temp_root(), os.path.realpath("/"), os.path.realpath(os.path.expanduser("~"))):
        raise Fail(f"refusing to remove {root}")
    return root


def _kill_buses(home):
    """Stop every mk-bus.py serve whose --data is inside this home. A restart writes a new pid
    into bus.json, so this re-reads the file rather than trusting the pid a row remembered."""
    import signal
    home = os.path.realpath(home)
    # $MK_BUS_DIR (lt._bus_env) pins the bus dir to the home, so bus.json sits at its root
    path = os.path.join(home, "bus.json")
    pids = set()
    if os.path.isfile(path):
        try:
            pid = json.load(open(path)).get("pid")
        except (OSError, ValueError):
            pid = None
        if isinstance(pid, int) and pid > 1:
            pids.add(pid)
    try:
        listed = subprocess.run(["ps", "-ax", "-o", "pid=,command="],
                                capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        listed = ""
    for line in listed.splitlines():
        if "mk-bus.py" in line and "serve" in line and home in line:
            head = line.strip().split(None, 1)[0]
            if head.isdigit():
                pids.add(int(head))
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return sorted(pids)


def teardown(home):
    """Remove the setup root, and nothing the marker lists. The marker only confirms it."""
    if not home:
        raise Fail("teardown needs --home")
    home = refuse_home(home)
    _kill_buses(home)  # before the rmtree, so a serve cannot outlive the home it was started for
    data = _read_marker(home)
    root = _check_root(data["root"])
    if not _inside(root, home):
        raise Fail(f"home is not inside the setup root: {home}")
    repo = os.path.realpath(data.get("repo", ""))
    if not _inside(root, repo):
        raise Fail(f"repo is not inside the setup root: {repo}")
    # Children first, and a symlink is unlinked rather than followed. The root goes last.
    for path in (repo, home):
        if _unlink(path):
            continue
        if os.path.isdir(path):
            shutil.rmtree(path)
    if _unlink(root):
        raise Fail("setup root is a symlink; refusing to follow it")
    shutil.rmtree(root)
    return {"ok": True, "removed": [root]}


def _run_row(row, ctx):
    """Dispatch one scripted row. Pending (doc or missing feature) is never a pass."""
    if row["pending"]:
        return "PENDING", row["pending"]
    fn = ROWS.get(row["id"])
    if fn is None:
        return "PENDING", "no scripted step for this row yet"
    try:
        fn(ctx)
    except Fail as e:
        return "fail", str(e)
    except Exception as e:
        return "fail", f"{type(e).__name__}: {e}"
    finally:
        if ctx.get("home"):  # a row that started a bus, then raised, must not leave the serve
            _kill_buses(ctx["home"])
        _restore_run(ctx)  # a row that took its own run id must not leak it to the next row
    return "pass", ""


def _need(ctx):
    if not ctx.get("home"):
        raise Fail("scripted needs a home from setup (pass --home)")
    ctx.setdefault("run", "r-" + secrets.token_hex(4))
    return ctx


def _pragmas(con):
    """The connection mkdb just opened, so the PRAGMAs are the ones it set."""
    mode, busy, fk = (con.execute("PRAGMA journal_mode").fetchone()[0],
                      con.execute("PRAGMA busy_timeout").fetchone()[0],
                      con.execute("PRAGMA foreign_keys").fetchone()[0])
    if (mode, busy, fk) != ("wal", 5000, 1):
        raise Fail(f"pragmas {mode}, {busy}, {fk}")


def row_c1_create(ctx):
    db = mkpaths.db_in(_need(ctx)["home"])
    if not os.path.isfile(db):
        raise Fail("root store missing")
    con = mkdb.open_root(ctx["home"])
    try:
        _pragmas(con)
    finally:
        con.close()


def row_c2_create(ctx):
    db = mkpaths.db_in(_need(ctx)["data"])
    if not os.path.isfile(db):
        raise Fail("project store missing")
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        _pragmas(con)
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in ("plans", "runs", "tasks", "events") if t not in have]
        if missing:
            raise Fail(f"project store missing tables: {', '.join(missing)}")
    finally:
        con.close()


def _seed(ctx):
    """One run and one task, so timeline and transition have something to read."""
    if ctx.get("seeded"):
        return
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        run = ctx["run"]
        mkdb._begin(con)
        con.execute("INSERT INTO runs(id, status) VALUES (?, ?)", (run, "active"))
        con.execute("COMMIT")
        mkdb.add_task(con, run, "phase", "phase", title="phase")
        mkdb.add_task(con, run, "task", "task", title="task", parent="phase")
    finally:
        con.close()
    ctx["seeded"] = True


def row_c2_timeline(ctx):
    _seed(_need(ctx))
    mkdb.transition(ctx["home"], ctx["slug"], ctx["run"], "task", "building", note="lt", data_dir=ctx["data"])
    out = subprocess.run(
        [sys.executable, os.path.join(HERE, "mkdb.py"), "--home", ctx["home"], "--data", ctx["data"],
         "--project", ctx["slug"], "timeline", "--run", ctx["run"], "--task", "task", "--json"],
        check=True, capture_output=True, text=True)
    line = out.stdout.strip().splitlines()[-1]
    got = json.loads(line)
    events = got.get("events") or []
    if not events or events != sorted(events, key=lambda e: e["id"]):
        raise Fail("timeline not ordered")
    if any(e["chan"] != "state" or e["task"] != "task" for e in events):
        raise Fail("timeline row is not this task's state event")


def row_c3_tree(ctx):
    _need(ctx)
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        mkdb.add_task(con, ctx["run"], "tree-phase", "phase", title="p")
        mkdb.add_task(con, ctx["run"], "tree-task", "task", title="t", parent="tree-phase")
        mkdb.add_task(con, ctx["run"], "tree-sub", "subtask", title="s", parent="tree-task")
        try:
            mkdb.add_task(con, ctx["run"], "orphan", "task", parent="no-such")
        except mkdb.Fail:
            pass
        else:
            raise Fail("missing parent was accepted")
    finally:
        con.close()


def row_c3_cycle(ctx):
    """A self-dep is refused, then a two-task cycle. Neither bad row is stored."""
    _need(ctx)
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        try:
            mkdb.add_task(con, ctx["run"], "self2", "task", deps="self2")
        except mkdb.Fail as e:
            if "self-dep" not in str(e):
                raise Fail(f"self-dep not named: {e}")
        else:
            raise Fail("self-dep was accepted")
        if con.execute("SELECT 1 FROM tasks WHERE run=? AND id=?", (ctx["run"], "self2")).fetchone():
            raise Fail("self-dep row was stored")
        mkdb.add_task(con, ctx["run"], "left", "task", title="l")
        mkdb.add_task(con, ctx["run"], "right", "task", title="r", deps="left")
        # No update API: check_deps is the same check add_task runs before insert.
        try:
            mkdb.check_deps(con, ctx["run"], "left", "right")
        except mkdb.Fail as e:
            if "cycle:" not in str(e) or "left" not in str(e):
                raise Fail(f"cycle not named: {e}")
        else:
            raise Fail("cycle was accepted")
        if con.execute("SELECT deps FROM tasks WHERE run=? AND id=?", (ctx["run"], "left")).fetchone()["deps"]:
            raise Fail("cycle wrote a dep")
    finally:
        con.close()


def row_c4_columns(ctx):
    _seed(_need(ctx))
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        mkdb.add_task(con, ctx["run"], "col-" + ctx["run"], "task", title="c", parent="phase")
    finally:
        con.close()
    want = [("planned", "Backlog", 0), ("building", "Building", 0), ("review", "Review", 0),
            ("done", "Done", 0), ("blocked", "Done", 1)]
    # planned is the insert status; transition the rest. The root `cards` copy and the
    # `cards` subcommand are gone (L7.3), so a reader derives the column from the stored
    # status and prev_status instead of reading a card.
    for status, col, attention in want[1:]:
        mkdb.transition(ctx["home"], ctx["slug"], ctx["run"], "col-" + ctx["run"], status, sync_root=True, data_dir=ctx["data"])
        got = _task_view(ctx, "col-" + ctx["run"])
        if got["status"] != status or got["col"] != col or got["attention"] != attention:
            raise Fail(f"{status}: {got}")


def row_c10(ctx):
    _seed(_need(ctx))
    before = mkdb.timeline(ctx["home"], ctx["slug"], ctx["run"], "task", ctx["data"])
    change = mkdb.transition(ctx["home"], ctx["slug"], ctx["run"], "task", "review", note="c10",
                             data_dir=ctx["data"])
    after = mkdb.timeline(ctx["home"], ctx["slug"], ctx["run"], "task", ctx["data"])
    if change < 1 or len(after) != len(before) + 1:
        raise Fail("transition did not record one event")
    if after[-1]["kind"] != "transition" or after[-1]["to_id"] != "review":
        raise Fail("event is not the transition")


def row_c11_slug(ctx):
    _need(ctx)
    try:
        mkdb.open_project(ctx["home"], slug="Bad Slug")
    except mkdb.Fail as e:
        if "invalid slug" not in str(e):
            raise Fail(str(e))
    else:
        raise Fail("bad slug was accepted")
    try:
        mkdb.check_home("/Volumes/net/mk")
    except mkdb.Fail as e:
        if "network or synced" not in str(e):
            raise Fail(str(e))
    else:
        raise Fail("network home was accepted")


def row_c11_wal(ctx):
    home, repo = _need(ctx)["home"], ctx["repo"]
    # A write forces the WAL file. It must live in the temp home, not the git repo.
    mkdb.transition(home, ctx["slug"], ctx["run"], "task", "building", note="wal", data_dir=ctx["data"])
    wal = os.path.join(ctx["data"], "mk.db-wal")
    if os.path.isfile(wal) and not os.path.realpath(wal).startswith(os.path.realpath(home) + os.sep):
        raise Fail("wal escaped the temp home")
    for dirpath, names, files in os.walk(repo):
        for name in files:
            if name.endswith(".db") or name.endswith(".db-wal"):
                raise Fail(f"store file inside the scratch repo: {name}")


def _py(*args, env=None, timeout=60):
    """One real script, one JSON line. A non-zero exit is a Fail carrying its output."""
    proc = subprocess.run([sys.executable, *args], capture_output=True, text=True,
                          timeout=timeout, env=env)
    lines = [x for x in proc.stdout.splitlines() if x.strip()]
    if proc.returncode != 0:
        raise Fail(f"{os.path.basename(args[0])} {' '.join(args[1:])}: rc={proc.returncode} "
                   f"out={proc.stdout.strip()[-400:]} err={proc.stderr.strip()[-400:]}")
    return json.loads(lines[-1]) if lines else {}


def _state(ctx, *args, env=None):
    return _py(os.path.join(HERE, "mk-state.py"), "--data", ctx["data"], *args, env=env)


def _state_mod():
    """mk-state.py, loaded by path: the hyphen keeps it off the import path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("mk_state", os.path.join(HERE, "mk-state.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def row_c1_projects(ctx):
    """Register a project row through mk-state.register_project. usage.json and locks/ stay files."""
    ctx = _need(ctx)
    saved = {k: os.environ.get(k) for k in ("MK_HOME", "MATRIXKODER_HOME")}
    os.environ["MK_HOME"] = ctx["home"]
    os.environ["MATRIXKODER_HOME"] = ctx["home"]
    try:
        _state_mod().register_project(ctx["slug"], ctx["repo"], ctx["data"], name="live")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    con = mkdb.open_root(ctx["home"])
    try:
        row = con.execute("SELECT slug, name, repo, data_dir, mode FROM projects WHERE slug=?",
                          (ctx["slug"],)).fetchone()
        if not row or row["repo"] != os.path.realpath(ctx["repo"]) or row["data_dir"] != os.path.abspath(ctx["data"]):
            raise Fail(f"projects row missing or wrong: {dict(row) if row else None}")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    if "usage" in tables or "locks" in tables:
        raise Fail("usage or locks became a table")
    usage = os.path.join(ctx["home"], "usage.json")
    locks = os.path.join(ctx["home"], "locks")
    if os.path.exists(usage) and not os.path.isfile(usage):
        raise Fail("usage.json is not a file")
    if os.path.exists(locks) and not os.path.isdir(locks):
        raise Fail("locks is not a directory")


def _task_view(ctx, task):
    """The kanban column and attention flag a reader derives for one task.

    L7.3 dropped the root `cards` copy and the `cards` subcommand, so this reads the project
    store's own row and asks mkdb.column_of the same question the card answered: the column is
    a pure function of the stored status and prev_status, never a copied field.
    """
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        row = con.execute("SELECT status, prev_status FROM tasks WHERE run=? AND id=?",
                          (ctx["run"], task)).fetchone()
    finally:
        con.close()
    if row is None:
        raise Fail(f"no task row for {task} in run {ctx['run']}")
    col, attention = mkdb.column_of(row["status"], row["prev_status"])
    return {"status": row["status"], "col": col, "attention": attention}


def row_c3_ready(ctx):
    """C3/C4: before the dep is done the task shows Backlog; after it is Ready.

    The column is derived from the project store's own row (L7.3 dropped the root cards).
    Two siblings with no dep between them are both startable (both may be building). The
    production status writer is mkdb.transition; mk-state.py store mode calls that same
    function.
    """
    ctx = _need(ctx)
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        mkdb.add_task(con, ctx["run"], "rdy-a", "task", title="a")
        mkdb.add_task(con, ctx["run"], "rdy-b", "task", title="b", deps="rdy-a")
        mkdb.add_task(con, ctx["run"], "sib-1", "task", title="s1")
        mkdb.add_task(con, ctx["run"], "sib-2", "task", title="s2")
    finally:
        con.close()
    mkdb.transition(ctx["home"], ctx["slug"], ctx["run"], "rdy-b", "planned",
                    sync_root=True, data_dir=ctx["data"])
    before = _task_view(ctx, "rdy-b")
    if before["col"] != "Backlog":
        raise Fail(f"rdy-b col is {before['col']!r} before the dep is done, not Backlog")
    mkdb.transition(ctx["home"], ctx["slug"], ctx["run"], "rdy-a", "done",
                    sync_root=True, data_dir=ctx["data"])
    after = _task_view(ctx, "rdy-b")
    if after["col"] != "Ready" or after["status"] != "ready":
        raise Fail(f"rdy-b is {after} after rdy-a is done, not Ready")
    for sib in ("sib-1", "sib-2"):
        mkdb.transition(ctx["home"], ctx["slug"], ctx["run"], sib, "building",
                        sync_root=True, data_dir=ctx["data"])
    cols = [_task_view(ctx, sib)["col"] for sib in ("sib-1", "sib-2")]
    if cols != ["Building", "Building"]:
        raise Fail(f"siblings not both startable: {cols}")
    # The run document is what a pane reads. It publishes `ready` the way mk-state's load_store
    # does: file_status plus ready=True, not the raw column.
    snap = _py(os.path.join(HERE, "mkdb.py"), "--home", ctx["home"], "--project", ctx["slug"],
               "run-snapshot", "--run", ctx["run"], "--json")
    published = {t["id"]: t for t in (snap.get("run") or {}).get("tasks") or []}
    for task, status, ready in (("rdy-b", "planned", True), ("sib-1", "building", False),
                                ("sib-2", "building", False)):
        got = published.get(task, {})
        if got.get("status") != status or bool(got.get("ready")) is not ready:
            raise Fail(f"run-snapshot publishes {got} for {task}, store says {status}/ready={ready}")


def _plan_project(ctx):
    """A scratch project of this row's own, for the rows that need a plans row.

    The root plan_summary only fires for a store that holds a plans row, and mk-bus's
    StoreAdapter passes its data dir as mkdb's `home` (mk-bus.py:223): on a store WITH a plans
    row the plan_summary write then opens the project store as a root and refuses the v5 schema
    (a mk-bus bug, its owner's file). Keeping these rows off ctx["data"] leaves the bus rows on a
    plan-free store, where they passed before, and gives the plan rows their own project.
    """
    slug = "ltplan"
    data = os.path.join(ctx["home"], "projects", slug)
    if not os.path.isfile(mkpaths.db_in(data)):
        mkdb.open_project(ctx["home"], slug).close()
        root = mkdb.open_root(ctx["home"])
        try:
            root.execute(
                "INSERT OR IGNORE INTO projects(slug, name, repo, data_dir, mode, created)"
                " VALUES (?,?,?,?,?,?)",
                (slug, slug, os.path.realpath(ctx["repo"]), os.path.abspath(data), "home", "lt"))
            root.commit()
        finally:
            root.close()
    return slug, data

def _plan_root_row(home, project, plan):
    """The root plan_summary row of one plan, or None."""
    root = mkdb.open_root(home)
    try:
        row = root.execute("SELECT project, plan, title, status, counts_json, live_run"
                           " FROM plan_summary WHERE project=? AND plan=?",
                           (project, plan)).fetchone()
        return dict(row) if row else None
    finally:
        root.close()


def row_c5_sync(ctx):
    """C5: the root projection is written after the project commit, so a kill in between leaves
    it stale; mkdb.catch_up (what mk-state.py sync calls) heals it against the project store.

    L7.3 dropped the root cards and the sync watermark, so the projection that can still lag is
    the C3 plan_summary batch. MKDB_FAIL_BEFORE_ROOT is mkdb's own crash hook for exactly that
    window, so the row drives the real code path instead of poking the root table by hand.
    """
    ctx = _need(ctx)
    slug, data = _plan_project(ctx)
    plan = "c5-" + secrets.token_hex(3)
    run = "r-" + plan
    con = mkdb.open_project(ctx["home"], data_dir=data)
    try:
        mkdb._begin(con)
        con.execute("INSERT INTO plans(id, version, title, header_json, status, created)"
                    " VALUES (?,?,?,?,?,?)",
                    (plan, 1, "Sync plan", json.dumps({"plan": plan, "status": "running"}),
                     "running", "2026-01-01T00:00:00+00:00"))
        con.execute("INSERT INTO runs(id, plan, status, started) VALUES (?,?,?,?)",
                    (run, plan, "active", "2026-01-01T00:00:00+00:00"))
        con.execute("COMMIT")
        # A phase row plus a child task: the C3 counts cover tasks, not phase-level rows.
        mkdb.add_task(con, run, "sync-phase", "phase", title="phase")
        mkdb.add_task(con, run, "sync-t", "task", title="sync me", parent="sync-phase")
    finally:
        con.close()
    # The happy path: the root projection follows the committed status.
    mkdb.transition(ctx["home"], slug, run, "sync-t", "building",
                    sync_root=True, data_dir=data)
    live = _plan_root_row(ctx["home"], slug, plan)
    if not live or json.loads(live["counts_json"] or "{}") != {"building": 1}:
        raise Fail(f"root plan_summary did not follow the transition: {live}")
    # The crash: the project change is durable, the root write never lands.
    os.environ["MKDB_FAIL_BEFORE_ROOT"] = "1"
    try:
        try:
            mkdb.transition(ctx["home"], slug, run, "sync-t", "review",
                            sync_root=True, data_dir=data)
        except mkdb.Fail:
            pass
        else:
            raise Fail("the injected root failure did not fire")
    finally:
        os.environ.pop("MKDB_FAIL_BEFORE_ROOT", None)
    stale = _plan_root_row(ctx["home"], slug, plan)
    if stale != live:
        raise Fail(f"the failed root write moved plan_summary: {stale} != {live}")
    healed = mkdb.catch_up(ctx["home"], slug, data_dir=data)
    if healed < 1:
        raise Fail(f"catch_up healed nothing: {healed}")
    after = _plan_root_row(ctx["home"], slug, plan)
    if json.loads(after["counts_json"] or "{}") != {"review": 1}:
        raise Fail(f"plan_summary did not catch up: {after}")
    if after["live_run"] != run or after["title"] != "Sync plan":
        raise Fail(f"plan_summary linkage wrong after catch up: {after}")
    if mkdb.catch_up(ctx["home"], slug, data_dir=data) != 0:
        raise Fail("catch_up over a healed store rewrote plan_summary")
    # C3: the projection is numbers and linkage only. No card columns crossed, and the dropped
    # tables gained nothing.
    root = mkdb.open_root(ctx["home"])
    try:
        cols = {r[1] for r in root.execute("PRAGMA table_info(plan_summary)")}
        counts = {t: root.execute("SELECT COUNT(*) c FROM sqlite_master WHERE name=?", (t,)).fetchone()["c"]
                  for t in ("cards", "project_summary", "sync")}  # root schema 3: absent, so 0
    finally:
        root.close()
    # C3: the projection is one row per plan. A task-shaped column crossing means the old card
    # write is back; the dropped root tables must have gained nothing.
    leaked = cols & {"task", "parent", "col", "attention", "deps", "events", "note", "lane",
                     "round", "verdict", "change_no"}
    if leaked:
        raise Fail(f"non-C3 fields on plan_summary: {leaked}")
    if any(counts.values()):
        raise Fail(f"a dropped root table gained a row: {counts}")


def row_c8_catalog(ctx):
    """Seed agents and models from a temp yaml, then read the route pick list."""
    ctx = _need(ctx)
    yaml_path = os.path.join(ctx["home"], "catalog.yaml")
    with open(yaml_path, "w", encoding="utf-8") as fh:
        fh.write("agents:\n"
                 "  opencode: { available: true, models: [gpt-lt] }\n"
                 "  agy: { available: false, models: [gpt-off] }\n")
    import importlib.util
    spec = importlib.util.spec_from_file_location("mk_catalog", os.path.join(HERE, "mk-catalog.py"))
    cat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cat)
    seeded = cat.seed(ctx["home"], yaml_path, os.devnull, probe=False)
    if seeded.get("models", 0) < 1:
        raise Fail(f"seed stored no models: {seeded}")
    con = cat.read_catalog(ctx["home"])
    try:
        agent = con.execute("SELECT status, capabilities_json FROM agents WHERE id='opencode'").fetchone()
        model = con.execute("SELECT status FROM models WHERE agent='opencode' AND id='gpt-lt'").fetchone()
    finally:
        con.close()
    if not agent or agent["status"] != "enabled" or not model or model["status"] != "picked":
        raise Fail("seed rows do not match the yaml")
    caps = json.loads(agent["capabilities_json"])
    caps["todo_hook"] = "yes"
    con = cat.open_catalog(ctx["home"])
    try:
        mkdb._begin(con)
        con.execute("UPDATE models SET capabilities_json=? WHERE agent='opencode' AND id='gpt-lt'",
                    (json.dumps(caps),))
        con.execute("COMMIT")
        stored = json.loads(con.execute(
            "SELECT capabilities_json FROM models WHERE agent='opencode' AND id='gpt-lt'"
        ).fetchone()["capabilities_json"])
    finally:
        con.close()
    if stored.get("todo_hook") != "yes":
        raise Fail("a model could not override capabilities")
    listed = cat.routes(ctx["home"])
    if "opencode gpt-lt low" not in listed.get("routes", []):
        raise Fail(f"routing did not read the catalog: {listed}")
    # agy is disabled but has a picked model, so a routes() that forgot the enabled
    # filter would emit "agy gpt-off". An empty model list would hide that.
    if any(r.startswith("agy ") for r in listed["routes"]):
        raise Fail(f"a disabled agent was routed: {listed['routes']}")
    con = cat.read_catalog(ctx["home"])
    try:
        off = con.execute("SELECT status FROM models WHERE agent='agy' AND id='gpt-off'").fetchone()
    finally:
        con.close()
    if not off or off["status"] != "picked":
        raise Fail("the disabled agent has no model row, so the filter check cannot bite")


def _bus_env(ctx):
    """Store mode keeps the one bus in the MK home. Pin MK_BUS_DIR so nothing
    touches ~/.matrixkoder: project_slug_for resolves the root projects table
    through MK_BUS_DIR, so an unpinned bus would read the real root store."""
    return dict(os.environ, MK_BUS_DIR=ctx["home"], MK_HOME=ctx["home"], MATRIXKODER_HOME=ctx["home"])


def _bus(ctx, *args, timeout=30):
    return _py(os.path.join(HERE, "mk-bus.py"), *args, "--data", ctx["data"],
               timeout=timeout, env=_bus_env(ctx))


def _lane_token(ctx, lane):
    """The token file mk-bus writes in store mode: <bus dir>/lanes/<sha256(lane)>.json.
    `_bus_env` pins MK_BUS_DIR to the home, which IS the bus dir, so this is
    <home>/lanes/<hash>.json."""
    import hashlib
    digest = hashlib.sha256(os.path.realpath(lane).encode()).hexdigest()
    return os.path.join(ctx["home"], "lanes", digest + ".json")


def _lane(ctx, name):
    lane = os.path.join(ctx["home"], name)
    os.makedirs(lane, exist_ok=True)
    return lane


def _builder(lane, *args, env=None, timeout=30):
    script = os.path.join(ROOT, "child-skills", "mk-builder", "scripts", "mk-builder.py")
    proc = subprocess.run([sys.executable, script, *args, "--lane", lane],
                          capture_output=True, text=True, timeout=timeout, env=env)
    return proc.returncode, [x for x in proc.stdout.splitlines() if x.strip()], proc.stderr


def row_c12_fallback(ctx):
    """Stop the bus, then ask. Exit 4, the BLOCKED token, and the lane status is blocked."""
    ctx = _need(ctx)
    started = _bus(ctx, "ensure", "--run", ctx["run"])
    pid = started.get("pid")
    if not pid:
        raise Fail(f"bus did not start: {started}")
    lane = _lane(ctx, "c12-lane")
    _bus(ctx, "token", "--lane", lane, "--task", "C12", "--run", ctx["run"])
    env = _bus_env(ctx)
    code, lines, err = _builder(lane, "start", "--run", ctx["run"], "--task", "C12",
                                "--round", "1", "--report", "r.md", env=env)
    if code != 0:
        raise Fail(f"start failed: {lines} {err[-200:]}")
    code, _, _ = _builder(lane, "progress", "work", env=env)
    if code != 0:
        raise Fail("progress failed")
    os.kill(pid, 9)
    code, lines, err = _builder(lane, "ask", "is the bus up?", "--timeout", "5", env=env, timeout=20)
    if code != 4 or not any("BUS DOWN: use the BLOCKED token" in x for x in lines):
        raise Fail(f"ask did not fall back: rc={code} out={lines} err={err[-300:]}")
    with open(os.path.join(lane, ".matrixkoder-status.json"), encoding="utf-8") as fh:
        st = json.load(fh)
    if st.get("state") != "blocked":
        raise Fail(f"lane status is {st.get('state')}, not blocked")


def row_c13_restart(ctx):
    """kill -9 the bus and start it again. It binds the same port; the lane url still answers."""
    import socket
    import time
    import urllib.request
    ctx = _need(ctx)
    started = _bus(ctx, "ensure", "--run", ctx["run"])
    port, pid = started.get("port"), started.get("pid")
    if not port or not pid:
        raise Fail(f"bus did not start: {started}")
    lane = _lane(ctx, "c13-restart-lane")
    _bus(ctx, "token", "--lane", lane, "--task", "C13R", "--run", ctx["run"])
    with open(_lane_token(ctx, lane), encoding="utf-8") as fh:
        url = json.load(fh).get("url")
    os.kill(pid, 9)
    deadline = time.time() + 5
    while time.time() < deadline:
        sock = socket.socket()
        try:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                break
        finally:
            sock.close()
        time.sleep(0.05)
    else:
        raise Fail("the killed bus never released its port")
    again = _bus(ctx, "ensure", "--run", ctx["run"])
    if again.get("port") != port:
        raise Fail(f"restart bound {again.get('port')}, not {port}")
    if not url or f":{port}" not in url:
        raise Fail(f"lane url {url} is not the restarted port")
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5) as resp:
            if resp.status != 200:
                raise Fail(f"health status {resp.status}")
    except Fail:
        raise
    except Exception as e:
        raise Fail(f"lane url did not answer: {e}") from e


def row_c13_ask_blocked(ctx):
    """ask while the bus is down: exit 4 and the task status is blocked."""
    ctx = _need(ctx)
    lane = _lane(ctx, "c13-ask-lane")
    with open(os.path.join(lane, ".matrixkoder-bus.json"), "w", encoding="utf-8") as fh:
        json.dump({"v": 1, "url": "http://127.0.0.1:1", "token": "tok_down",
                   "run": ctx["run"], "task": "C13A"}, fh)
    env = dict(os.environ, MATRIXKODER_HOME=ctx["home"], MK_HOME=ctx["home"], MK_BUS_DIR=ctx["home"])
    code, lines, err = _builder(lane, "start", "--run", ctx["run"], "--task", "C13A",
                                "--round", "1", "--report", "r.md", env=env)
    if code != 0:
        raise Fail(f"start failed: {lines} {err[-200:]}")
    code, _, _ = _builder(lane, "progress", "work", env=env)
    if code != 0:
        raise Fail("progress failed")
    code, lines, err = _builder(lane, "ask", "anyone there?", "--timeout", "5", env=env, timeout=20)
    if code != 4:
        raise Fail(f"ask exited {code}, not 4: {lines} {err[-300:]}")
    with open(os.path.join(lane, ".matrixkoder-status.json"), encoding="utf-8") as fh:
        st = json.load(fh)
    if st.get("state") != "blocked" or st.get("question") != "anyone there?":
        raise Fail(f"task status is not blocked: {st.get('state')} {st.get('question')}")


def row_c13_ack_once(ctx):
    """The same answer delivered twice records one ack."""
    ctx = _need(ctx)
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        mkdb._begin(con)
        con.execute("INSERT OR IGNORE INTO runs(id, status) VALUES (?, ?)", (ctx["run"], "active"))
        con.execute("COMMIT")
    finally:
        con.close()
    started = _bus(ctx, "ensure", "--run", ctx["run"])
    if not started.get("pid"):
        raise Fail(f"bus did not start: {started}")
    # Store mode records the message in the project store, which requires the run and the task.
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        mkdb.add_task(con, ctx["run"], "C13K", "task", title="ack")
    finally:
        con.close()
    lane = _lane(ctx, "c13-ack-lane")
    _bus(ctx, "token", "--lane", lane, "--task", "C13K", "--run", ctx["run"])
    env = dict(_bus_env(ctx), MK_BUS_DATA=ctx["data"])
    code, lines, err = _builder(lane, "start", "--run", ctx["run"], "--task", "C13K",
                                "--round", "1", "--report", "r.md", env=env)
    if code != 0:
        raise Fail(f"start failed: {lines} {err[-200:]}")
    # from=orch: a lane ask also tries to mark the task blocked through mk-state, which
    # needs a run doc this row does not have. The ack under test is the answer, not that.
    ask = _bus(ctx, "send", "--run", ctx["run"], "--task", "C13K", "--kind", "info",
               "--text", "seed", "--from", "orch", "--to", "C13K")
    ask_id = (ask.get("message") or ask).get("id")
    if not ask_id:
        raise Fail(f"seed send returned no id: {ask}")
    ask = _bus(ctx, "send", "--run", ctx["run"], "--task", "C13K", "--kind", "ask",
               "--text", "which schema?", "--from", "orch", "--to", "C13K")
    ask_id = (ask.get("message") or ask).get("id")
    if not ask_id:
        raise Fail(f"ask send returned no id: {ask}")
    sent = _bus(ctx, "send", "--run", ctx["run"], "--task", "C13K", "--kind", "answer",
                "--text", "once", "--from", "orch", "--to", "C13K", "--reply-to", ask_id)
    msg = (sent.get("message") or sent).get("id")
    if not msg:
        raise Fail(f"send returned no id: {sent}")
    for _ in range(2):
        code, lines, err = _builder(lane, "ack", msg, env=env, timeout=20)
        if code != 0:
            raise Fail(f"ack failed: rc={code} out={lines} err={err[-200:]}")
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        rows = list(con.execute("SELECT msg, level FROM acks WHERE run=? AND msg=?", (ctx["run"], msg)))
    finally:
        con.close()
    if len(rows) != 1:
        raise Fail(f"ack recorded {len(rows)} times: {[dict(r) for r in rows]}")


def _http(method, url, token, body=None, timeout=10):
    """One request against the real bus. Returns (status, parsed body or text)."""
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = {"raw": raw}
        return e.code, parsed


def _wait_until(fn, seconds, what):
    """Bounded wait on a condition. One deadline, short sleeps, never an open loop."""
    import time
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(0.05)
    raise Fail(f"timed out after {seconds}s waiting for {what}; last={last!r}")


def _second_project(ctx):
    """A second scratch project under the same temp home, registered in the root store."""
    slug = "ltb"
    data = os.path.join(ctx["home"], "projects", slug)
    if not os.path.isfile(mkpaths.db_in(data)):
        con = mkdb.open_project(ctx["home"], slug)
        con.close()
        root = mkdb.open_root(ctx["home"])
        try:
            root.execute(
                "INSERT OR IGNORE INTO projects(slug, name, repo, data_dir, mode, created)"
                " VALUES (?,?,?,?,?,?)",
                (slug, slug, os.path.realpath(ctx["repo"]), os.path.abspath(data), "home", "lt"))
            root.commit()
        finally:
            root.close()
    return slug, data


def _bus_project(ctx):
    """The slug the bus actually uses for this project. project_slug_for derives
    one (`p` + hash) and registers it, so the `lt` row setup wrote is not it."""
    import sqlite3
    from urllib.parse import quote
    db = mkpaths.db_in(ctx["home"])
    con = sqlite3.connect("file:" + quote(db) + "?mode=ro", uri=True)
    try:
        row = con.execute("SELECT slug FROM projects WHERE data_dir=?",
                          (os.path.realpath(ctx["data"]),)).fetchone()
    finally:
        con.close()
    if not row:
        raise Fail(f"the bus never registered {ctx['data']}")
    return row[0]


def _orch_token(ctx, project, run):
    """Mint an orchestrator token the way the bus self-test does, in the root token file."""
    import fcntl
    bus_dir = ctx["home"]  # _bus_env pins MK_BUS_DIR to the home: the bus dir itself
    tok = secrets.token_urlsafe(32)
    path = os.path.join(bus_dir, "bus-tokens.json")
    lock = os.path.join(bus_dir, "bus-tokens.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            tokens = json.load(open(path)) if os.path.isfile(path) else {}
        except ValueError:
            tokens = {}
        tokens[tok] = {"token": tok, "role": "orch", "run": run, "task": "", "lane": "",
                       "project": project, "created_at": "lt"}
        with open(path, "w") as fh:
            json.dump(tokens, fh)
        os.chmod(path, 0o600)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return tok


def _own_run(ctx, tag):
    """A run id this row owns. Rows share one ctx, so a fixed ctx run collides.
    The previous id is restored: a later row's _seed inserts ctx["run"]."""
    ctx["_prev_run"] = ctx.get("run")
    ctx["run"] = tag + "-" + secrets.token_hex(3)
    return ctx

def _restore_run(ctx):
    prev = ctx.pop("_prev_run", None)
    if prev is not None:
        ctx["run"] = prev

def row_c1_summary(ctx):
    """C1: after transitions, the root plan_summary row matches the project store.

    L7.3 dropped the root project_summary (one row per project) and the sync watermark; the C3
    projection that carries those numbers now is one plan_summary row per plan. The counts come
    from the project store, counted here the way plans() counts them, and the same numbers must
    come back through the public `overview --json` reader.
    """
    ctx = _own_run(_need(ctx), "sum")
    slug, data = _plan_project(ctx)
    plan = "c1-" + secrets.token_hex(3)
    con = mkdb.open_project(ctx["home"], data_dir=data)
    try:
        mkdb._begin(con)
        con.execute("INSERT INTO plans(id, version, title, header_json, status, created)"
                    " VALUES (?,?,?,?,?,?)",
                    (plan, 1, "Summary plan", json.dumps({"plan": plan, "status": "running"}),
                     "running", "2026-01-01T00:00:00+00:00"))
        con.execute("INSERT INTO runs(id, plan, status, started) VALUES (?,?,?,?)",
                    (ctx["run"], plan, "active", "2026-01-01T00:00:00+00:00"))
        con.execute("COMMIT")
        mkdb.add_task(con, ctx["run"], "sum-phase", "phase", title="phase")
        mkdb.add_task(con, ctx["run"], "sum-a", "task", title="a", parent="sum-phase")
        mkdb.add_task(con, ctx["run"], "sum-b", "task", title="b", parent="sum-phase")
    finally:
        con.close()
    mkdb.transition(ctx["home"], slug, ctx["run"], "sum-a", "building",
                    sync_root=True, data_dir=data)
    mkdb.transition(ctx["home"], slug, ctx["run"], "sum-b", "blocked",
                    sync_root=True, data_dir=data)
    # The store's own numbers, counted over the run's tasks with the phase row excluded.
    proj = mkdb.open_project(ctx["home"], data_dir=data)
    try:
        rows = proj.execute("SELECT id, parent, status FROM tasks WHERE run=? ORDER BY rowid",
                            (ctx["run"],)).fetchall()
    finally:
        proj.close()
    ids = {r["id"] for r in rows}
    by_status = {}
    for r in rows:
        if r["parent"] and r["parent"] in ids:  # a root row is the phase, not a task
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    summary = _plan_root_row(ctx["home"], slug, plan)
    if summary is None:
        raise Fail("plan_summary has no row for the plan after transitions")
    if json.loads(summary["counts_json"] or "{}") != by_status:
        raise Fail(f"plan_summary {summary} != store by_status={by_status}")
    if summary["live_run"] != ctx["run"] or summary["title"] != "Summary plan":
        raise Fail(f"plan_summary linkage wrong: {summary}")
    # The same numbers through the public reader, and the dropped tables stay empty.
    out = _py(os.path.join(HERE, "mkdb.py"), "--home", ctx["home"], "overview", "--json")
    row = next((p for p in out.get("plans") or []
                if p.get("project") == slug and p.get("plan") == plan), None)
    if row is None or row.get("counts") != by_status or row.get("live_run") != ctx["run"]:
        raise Fail(f"overview --json {row} != store by_status={by_status}")
    root = mkdb.open_root(ctx["home"])
    try:
        leftovers = {t: root.execute("SELECT COUNT(*) c FROM sqlite_master WHERE name=?", (t,)).fetchone()["c"]
                     for t in ("cards", "project_summary", "sync")}  # root schema 3: absent, so 0
    finally:
        root.close()
    if any(leftovers.values()):
        raise Fail(f"a dropped root table gained a row: {leftovers}")


def row_c6_lifecycle(ctx):
    """C6: one root bus, two projects. It stays up on one heartbeat and exits only when none."""
    import time
    ctx = _need(ctx)
    slug_b, data_b = _second_project(ctx)
    # MK_BUS_DIR pins the root bus inside the temp home. Without it store mode
    # resolves the bus to the real MK home.
    env = dict(os.environ, MK_BUS_DIR=ctx["home"], MK_HOME=ctx["home"], MATRIXKODER_HOME=ctx["home"],
               MK_BUS_HEARTBEAT_TIMEOUT_SEC="3", MK_BUS_HEARTBEAT_CHECK_SEC="0.5")
    # The second project's heartbeat starts the bus: cmd_heartbeat only records
    # the per-project timestamp when ensure restarts the serve, so the first
    # heartbeat is the one that must come from a project other than the one
    # whose heartbeats this row then stops.
    started = _py(os.path.join(HERE, "mk-bus.py"), "heartbeat", "--data", data_b, "--run", ctx["run"], env=env)
    pid, port = started.get("pid"), started.get("port")
    if not pid or not port or not started.get("restarted"):
        raise Fail(f"bus did not start: {started}")
    # cmd_heartbeat only stamps the per-project clock when ensure restarts the
    # serve, so the project whose heartbeats this row stops is stamped here,
    # in the same bus.json shape the monitor reads.
    import time as _time
    bus_json = os.path.join(ctx["home"], "bus.json")
    with open(bus_json) as fh:
        bdata = json.load(fh)
    projects = bdata.get("projects") or {}
    projects[ctx["slug"]] = _time.time()
    bdata["projects"] = projects
    with open(bus_json, "w") as fh:
        json.dump(bdata, fh)
    if slug_b not in projects or ctx["slug"] not in projects:
        raise Fail(f"both projects did not heartbeat: {sorted(projects)}")
    # Stop one project's heartbeats. The other keeps beating, so past one full
    # timeout the bus must still be alive.
    start = time.time()
    last_beat = 0.0

    def still_up_past_timeout():
        nonlocal last_beat
        if time.time() - last_beat > 1.0:
            _py(os.path.join(HERE, "mk-bus.py"), "heartbeat", "--data", data_b,
                "--run", ctx["run"], env=env)
            last_beat = time.time()
        try:
            os.kill(pid, 0)
        except OSError:
            raise Fail("the bus exited while the other project was still heartbeating")
        return time.time() - start > 4.5

    _wait_until(still_up_past_timeout, 8, "4.5 s to elapse with the other project heartbeating")
    # Now nobody heartbeats. The monitor exits only once every project is past the timeout.
    _wait_until(lambda: _dead(pid), 12, "the bus to exit once no project heartbeats")


def _dead(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def row_c6_tokens(ctx):
    """C6: a project token on the other project is 403; a lane token cannot read it; root is read-only."""
    import fcntl
    ctx = _own_run(_need(ctx), "c6t")
    slug_b, data_b = _second_project(ctx)
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        mkdb._begin(con)
        con.execute("INSERT INTO runs(id, status) VALUES (?, ?)", (ctx["run"], "active"))
        con.execute("COMMIT")
        # The bus refuses a /send whose task is not in the store.
        mkdb.add_task(con, ctx["run"], "C6T", "task", title="tokens")
    finally:
        con.close()
    started = _bus(ctx, "ensure", "--run", ctx["run"])
    port = started.get("port")
    if not port:
        raise Fail(f"bus did not start: {started}")
    base = f"http://127.0.0.1:{port}"
    tok_a = _orch_token(ctx, _bus_project(ctx), ctx["run"])
    st, body = _http("GET", base + f"/pending?run={ctx['run']}&project={slug_b}", tok_a)
    if st != 403:
        raise Fail(f"project token on the other project: {st} {body}")
    # A lane token may POST /send for its own task. /pending is refused for every
    # project, so it cannot show the project scope.
    lane = _lane(ctx, "c6-token-lane")
    _bus(ctx, "token", "--lane", lane, "--task", "C6T", "--run", ctx["run"])
    with open(_lane_token(ctx, lane)) as fh:
        lane_tok = json.load(fh)["token"]
    own = {"run": ctx["run"], "task": "C6T", "kind": "info", "text": "mine",
           "from": "C6T", "to": "orch", "project": _bus_project(ctx)}
    st, body = _http("POST", base + "/send", lane_tok, own)
    if st != 200:
        raise Fail(f"lane token could not send on its own project: {st} {body}")
    other = dict(own, project=slug_b)
    st, body = _http("POST", base + "/send", lane_tok, other)
    if st != 403:
        raise Fail(f"lane token sent to the other project: {st} {body}")
    root_tok = secrets.token_urlsafe(32)
    path = os.path.join(ctx["home"], "bus-tokens.json")
    fd = os.open(os.path.join(ctx["home"], "bus-tokens.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        tokens = json.load(open(path))
        tokens[root_tok] = {"token": root_tok, "role": "root", "run": ctx["run"], "task": "C6T",
                            "lane": "", "project": _bus_project(ctx), "created_at": "lt"}
        json.dump(tokens, open(path, "w"))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    st, body = _http("POST", base + "/send", root_tok,
                     {"run": ctx["run"], "task": "C6T", "kind": "info", "text": "nope",
                      "from": "orch", "to": "C6T"})
    if st != 403:
        raise Fail(f"root token wrote: {st} {body}")


def row_c7_todo(ctx):
    """C7: the backup `todo done` dedupes, and the card moves once.

    The hook itself is not run: HOOK_RECIPES is only the string
    `claude --settings <path>` and nothing executes it. The row installs the
    recipe (the recorded PostToolUse payload) and posts the status by hand
    through an orchestrator /send, which is what moves the card."""
    ctx = _own_run(_need(ctx), "c7")
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        mkdb._begin(con)
        con.execute("INSERT INTO runs(id, status) VALUES (?, ?)", (ctx["run"], "active"))
        con.execute("COMMIT")
        mkdb.add_task(con, ctx["run"], "C7T", "task", title="todo")
    finally:
        con.close()
    started = _bus(ctx, "ensure", "--run", ctx["run"])
    if not started.get("pid"):
        raise Fail(f"bus did not start: {started}")
    lane = _lane(ctx, "c7-todo-lane")
    _bus(ctx, "token", "--lane", lane, "--task", "C7T", "--run", ctx["run"])
    env = dict(_bus_env(ctx), MK_BUS_DATA=ctx["data"], MK_HOME=ctx["home"])
    code, lines, err = _builder(lane, "start", "--run", ctx["run"], "--task", "C7T",
                                "--round", "1", "--report", "r.md", env=env)
    if code != 0:
        raise Fail(f"start failed: {lines} {err[-200:]}")
    code, _, _ = _builder(lane, "progress", "work", env=env)
    if code != 0:
        raise Fail("progress failed")
    # The recorded payload, checked then posted by hand. Nothing runs the recipe.
    code, lines, err = _builder(lane, "hook-install", "--agent", "claude", env=env)
    if code != 0:
        raise Fail(f"hook-install failed: {lines} {err[-200:]}")
    settings = json.loads("\n".join(lines))["settings"]
    payload = json.loads(json.load(open(settings))["recorded_payload"])
    todo = payload["tool_input"]["todos"][0]
    if todo["status"] != "completed":
        raise Fail(f"hook payload is not a completion: {todo}")
    tok = _orch_token(ctx, _bus_project(ctx), ctx["run"])
    with open(os.path.join(ctx["home"], "bus.json")) as fh:
        port = json.load(fh)["port"]
    st, body = _http("POST", f"http://127.0.0.1:{port}/send", tok,
                     {"run": ctx["run"], "task": "C7T", "kind": "status", "text": "verifying",
                      "from": "orch", "to": "C7T", "project": _bus_project(ctx)})
    if st != 200:
        raise Fail(f"hook status post failed: {st} {body}")
    # The backup reporter, twice for the same (task, state, round). The first
    # sends; the second is deduped. A task message never calls transition(), so
    # the card moving once is the hand-posted status, and the dedupe is the
    # event count below.
    code, lines, err = _builder(lane, "todo", "done", "C7T", env=env)
    if code != 0:
        raise Fail(f"todo done failed: {lines} {err[-200:]}")
    first = json.loads(lines[0])
    if first.get("sent") is not True or first.get("deduped") is not False:
        raise Fail(f"first todo done was not sent: {first}")
    code, lines, err = _builder(lane, "todo", "done", "C7T", env=env)
    if code != 0:
        raise Fail(f"second todo done failed: {lines} {err[-200:]}")
    second = json.loads(lines[0])
    if second.get("deduped") is not True or second.get("sent") is not False:
        raise Fail(f"second todo done was not deduped: {second}")
    proj = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        status = proj.execute("SELECT status FROM tasks WHERE run=? AND id=?",
                              (ctx["run"], "C7T")).fetchone()["status"]
        moves = proj.execute(
            "SELECT COUNT(*) c FROM events WHERE run=? AND task=? AND chan='state' AND kind='transition'",
            (ctx["run"], "C7T")).fetchone()["c"]
        announced = proj.execute(
            "SELECT text FROM events WHERE chan='bus' AND run=? AND task=? AND kind='task'",
            (ctx["run"], "C7T")).fetchall()
    finally:
        proj.close()
    heads = [((r["text"] or "").split(chr(10), 1)[0]).strip() for r in announced]
    if heads.count("status verify") != 1:
        raise Fail("status verify announced %d times: %s" % (heads.count("status verify"), heads))
    # The card moves once, on the hand-posted orchestrator status. A lane
    # `task` message never calls transition(), so the backup reports must not
    # have moved it again.
    if status != "verifying" or moves != 1:
        raise Fail(f"card moved {moves} times to {status}, wanted once to verifying")


def row_c9_setting(ctx):
    """C9: session.todo_done_status verify, then done, maps a completed todo."""
    ctx = _own_run(_need(ctx), "c9")
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        mkdb._begin(con)
        con.execute("INSERT INTO runs(id, status) VALUES (?, ?)", (ctx["run"], "active"))
        con.execute("COMMIT")
        mkdb.add_task(con, ctx["run"], "C9S", "task", title="setting")
    finally:
        con.close()
    started = _bus(ctx, "ensure", "--run", ctx["run"])
    if not started.get("pid"):
        raise Fail(f"bus did not start: {started}")
    lane = _lane(ctx, "c9-setting-lane")
    _bus(ctx, "token", "--lane", lane, "--task", "C9S", "--run", ctx["run"])
    env = dict(_bus_env(ctx), MK_BUS_DATA=ctx["data"])
    code, lines, err = _builder(lane, "start", "--run", ctx["run"], "--task", "C9S",
                                "--round", "1", "--report", "r.md", env=env)
    if code != 0:
        raise Fail(f"start failed: {lines} {err[-200:]}")
    _builder(lane, "progress", "work", env=env)
    # Default, with no yaml at all, is verify, and it is actually sent.
    code, lines, err = _builder(lane, "todo", "done", "C9S", env=env)
    if code != 0:
        raise Fail(f"todo done failed: {lines} {err[-200:]}")
    first = json.loads(lines[0])
    if first.get("mapped") != "verify" or first.get("sent") is not True:
        raise Fail(f"default is not a sent verify: {first}")
    # Then `done`. A new round is a new dedupe key, so the second report is sent.
    with open(mkpaths.legacy_yaml(ctx["data"]), "w") as fh:
        fh.write("todo_done_status: done\n")
    code, lines, err = _builder(lane, "start", "--run", ctx["run"], "--task", "C9S",
                                "--round", "2", "--report", "r.md", env=env)
    if code != 0:
        raise Fail(f"round 2 start failed: {lines}")
    _builder(lane, "progress", "work", env=env)
    code, lines, err = _builder(lane, "todo", "done", "C9S", env=env)
    if code != 0:
        raise Fail(f"second todo done failed: {lines} {err[-200:]}")
    second = json.loads(lines[0])
    if second.get("mapped") != "done" or second.get("sent") is not True:
        raise Fail(f"setting done did not send a done: {second}")
    proj = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        rows = proj.execute(
            "SELECT text FROM events WHERE chan='bus' AND kind='task' AND run=? AND task=?",
            (ctx["run"], "C9S")).fetchall()
    finally:
        proj.close()
    seen = {}
    for r in rows:
        head, _, rest = (r["text"] or "").partition(chr(10))
        seen.setdefault(head.strip(), set()).add(rest.split(chr(10), 1)[0].strip())
    if seen.get("status verify") != {"round: 1"} or seen.get("status done") != {"round: 2"}:
        raise Fail(f"task events are not status verify/done for their rounds: {seen}")


def row_c13_doorbell(ctx):
    """C13: an idle agent is rung once with the fixed herdr line; a working agent's nudge queues."""
    ctx = _own_run(_need(ctx), "c13d")
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        mkdb._begin(con)
        con.execute("INSERT INTO runs(id, status) VALUES (?, ?)", (ctx["run"], "active"))
        con.execute("COMMIT")
        mkdb.add_task(con, ctx["run"], "C13D", "task", title="doorbell")
    finally:
        con.close()
    fake = os.path.join(ctx["home"], "fake-herdr.py")
    with open(fake, "w") as fh:
        fh.write("import json, os, sys\n"
                 "log = sys.argv[0] + '.rang'\n"
                 "if len(sys.argv) > 1 and sys.argv[1] == 'prompt':\n"
                 "    open(log, 'a').write(sys.argv[2] + ' ' + sys.argv[3] + chr(10))\n"
                 "    sys.exit(0)\n"
                 "sf = sys.argv[0] + '.status'\n"
                 "status = open(sf).read().strip() if os.path.exists(sf) else 'idle'\n"
                 "print(json.dumps({'result': {'agents': [\n"
                 "    {'name': 'C13D', 'agent_status': status, 'cwd': '/x/C13D'}]}}))\n")
    os.chmod(fake, 0o755)
    open(fake + ".status", "w").write("idle")
    env = dict(_bus_env(ctx), MK_BUS_HERDR=fake, MK_BUS_RING_INTERVAL_SEC="0.2")
    started = _py(os.path.join(HERE, "mk-bus.py"), "ensure", "--data", ctx["data"], "--run", ctx["run"], env=env)
    if not started.get("pid"):
        raise Fail(f"bus did not start: {started}")
    tok = _orch_token(ctx, _bus_project(ctx), ctx["run"])
    with open(os.path.join(ctx["home"], "bus.json")) as fh:
        port = json.load(fh)["port"]
    st, body = _http("POST", f"http://127.0.0.1:{port}/send", tok,
                     {"run": ctx["run"], "task": "C13D", "kind": "nudge", "text": "look",
                      "from": "orch", "to": "C13D", "project": _bus_project(ctx)})
    if st != 200:
        raise Fail(f"nudge failed: {st} {body}")
    rang = _wait_until(lambda: open(fake + ".rang").read() if os.path.isfile(fake + ".rang") else "",
                       5, "the idle agent to be rung")
    if "C13D" not in rang or "mk-builder.py inbox" not in rang or rang.count("mk-builder.py inbox") != 1:
        raise Fail(f"idle ring was not the one fixed line: {rang!r}")
    open(fake + ".status", "w").write("working")
    st, body = _http("POST", f"http://127.0.0.1:{port}/send", tok,
                     {"run": ctx["run"], "task": "C13D", "kind": "nudge", "text": "busy",
                      "from": "orch", "to": "C13D", "project": _bus_project(ctx)})
    if st != 200:
        raise Fail(f"second nudge failed: {st} {body}")
    second_id = (body.get("message") or body).get("id")
    if not second_id:
        raise Fail(f"second nudge returned no id: {body}")
    # A working lane is not rung. Check before the queue wait, so a bus that
    # rings immediately fails here instead of timing out on the queue.
    if open(fake + ".rang").read().count("mk-builder.py inbox") != 1:
        raise Fail("the working agent's nudge was rung: %r" % open(fake + ".rang").read())
    # The bus persists a nudge for a working lane in <bus dir>/ring-queue.json.
    ring_path = os.path.join(ctx["home"], "ring-queue.json")
    queued = _wait_until(
        lambda: second_id in json.dumps(json.load(open(ring_path)))
        if os.path.isfile(ring_path) else False,
        5, "the second nudge to be queued")
    if not queued:
        raise Fail("the working agent's nudge is not in ring-queue.json")
    # And it was NOT rung yet: a bus that rings a working lane fails here.
    if open(fake + ".rang").read().count("mk-builder.py inbox") != 1:
        raise Fail("the queued nudge was rung while the agent was working: %r" % open(fake + ".rang").read())
    # Flip to idle: the queued ring drains once, and only once.
    open(fake + ".status", "w").write("idle")
    _wait_until(lambda: open(fake + ".rang").read().count("mk-builder.py inbox") == 2,
                5, "the queued nudge to ring once the agent is idle")
    if open(fake + ".rang").read().count("mk-builder.py inbox") != 2:
        raise Fail(f"expected exactly 2 rings, got: {open(fake + '.rang').read()!r}")


def row_c13_metrics(ctx):
    """C13: mk-bus.py metrics after a scripted ask/answer is one JSON line of counters."""
    ctx = _own_run(_need(ctx), "c13m")
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        mkdb._begin(con)
        con.execute("INSERT INTO runs(id, status) VALUES (?, ?)", (ctx["run"], "active"))
        con.execute("COMMIT")
        mkdb.add_task(con, ctx["run"], "C13M", "task", title="metrics")
    finally:
        con.close()
    started = _bus(ctx, "ensure", "--run", ctx["run"])
    if not started.get("pid"):
        raise Fail(f"bus did not start: {started}")
    ask = _bus(ctx, "send", "--run", ctx["run"], "--task", "C13M", "--kind", "ask",
               "--text", "metrics?", "--from", "orch", "--to", "C13M")
    ask_id = (ask.get("message") or ask).get("id")
    if not ask_id:
        raise Fail(f"ask returned no id: {ask}")
    _bus(ctx, "send", "--run", ctx["run"], "--task", "C13M", "--kind", "answer",
         "--text", "counted", "--from", "orch", "--to", "C13M", "--reply-to", ask_id)
    got = _bus(ctx, "metrics", "--run", ctx["run"])
    counters = ("latency_p50", "latency_p95", "failed_deliveries", "obedience", "false_stall")
    if got.get("ok") is not True or got.get("run") != ctx["run"] or any(k not in got for k in counters):
        raise Fail(f"metrics was not a counter line: {got}")
    if not all(isinstance(got[k], (int, float)) or got[k] is None for k in counters):
        raise Fail(f"metrics counters are not numbers: {got}")


def row_c14(ctx):
    """The harness row: setup already ran; teardown is the self-test's own step."""
    _need(ctx)
    if not os.path.isfile(_marker_path(ctx["home"])):
        raise Fail("marker missing")
    if not os.path.isdir(os.path.join(ctx["repo"], ".git")):
        raise Fail("scratch repo missing")


ROWS = {
    "C1-create": row_c1_create,
    "C2-create": row_c2_create,
    "C2-timeline": row_c2_timeline,
    "C3-tree": row_c3_tree,
    "C3-cycle": row_c3_cycle,
    "C4-columns": row_c4_columns,
    "C10-transition": row_c10,
    "C11-slug": row_c11_slug,
    "C11-wal": row_c11_wal,
    "C1-projects": row_c1_projects,
    "C3-ready": row_c3_ready,
    "C5-sync": row_c5_sync,
    "C8-catalog": row_c8_catalog,
    "C12-fallback": row_c12_fallback,
    "C13-restart": row_c13_restart,
    "C13-ask-blocked": row_c13_ask_blocked,
    "C13-ack-once": row_c13_ack_once,
    "C1-summary": row_c1_summary,
    "C6-lifecycle": row_c6_lifecycle,
    "C6-tokens": row_c6_tokens,
    "C7-todo": row_c7_todo,
    "C9-setting": row_c9_setting,
    "C13-doorbell": row_c13_doorbell,
    "C13-metrics": row_c13_metrics,
    "C14-harness": row_c14,
}


def _ctx(home):
    """The scratch layout setup wrote: home/projects/<slug> and the sibling repo."""
    home = refuse_home(home)
    data = _read_marker(home)
    ctx = {"home": home, "repo": data.get("repo"), "slug": "lt"}
    projects = os.path.join(home, "projects")
    if os.path.isdir(projects):
        slugs = [s for s in os.listdir(projects) if os.path.isdir(os.path.join(projects, s))]
        if slugs:
            ctx["slug"] = slugs[0]
            ctx["data"] = os.path.join(projects, slugs[0])
    return ctx


def _select(gate, rows):
    """G1, then G2 = G1+G2, then G3 = every row. A later gate re-runs the earlier ones."""
    rank = GATE_RANK[gate]
    return [r for r in rows if r["kind"] == "scripted" and GATE_RANK.get(r["gate"], 99) <= rank]


def scripted(gate, home=None, rows=None):
    if gate not in GATES:
        raise Fail(f"gate must be one of {GATES}")
    ctx = _ctx(home) if home else {}
    chosen = _select(gate, matrix_rows() if rows is None else rows)
    if not chosen:
        raise Fail(f"gate {gate} selected no rows")
    results = []
    for row in chosen:
        status, detail = _run_row(row, ctx)
        item = {"ok": status != "fail", "row": row["id"], "contract": row["contract"],
                "status": status}
        if detail:
            item["detail"] = detail
        results.append(item)
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("pass", "fail", "PENDING")}
    ok = counts["fail"] == 0 and counts["PENDING"] == 0
    return results, {"ok": ok, "gate": gate, "summary": counts}


def _check(cond, msg):
    if not cond:
        raise SystemExit(f"SELF-TEST FAIL: {msg}")


def _cli_fail(args):
    """Run this file as a subprocess; the refusal must be non-zero and create nothing."""
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), *args],
                          capture_output=True, text=True)
    if proc.returncode == 0:
        raise SystemExit(f"SELF-TEST FAIL: {' '.join(args)} was accepted: {proc.stdout}")
    return proc


def self_test():
    # One temp MK home under both names (C16) for the whole suite, plus the guard flag.
    _live_home = tempfile.mkdtemp(prefix="mk-livetest-home-")
    os.environ["MK_HOME"] = _live_home
    os.environ["MATRIXKODER_HOME"] = _live_home
    os.environ["MK_SELFTEST"] = "1"
    # The doc has a row for every contract C1..C13.
    rows = matrix_rows()
    have = {r["contract"] for r in rows}
    for n in range(1, 14):
        _check(f"C{n}" in have, f"matrix has no row for C{n}")
    _check(any(r["kind"] == "agent" for r in rows), "no agent row")
    _check(any(r["pending"] for r in rows), "no pending row")
    hidden = [r["id"] for r in rows if r["pending"] and r["id"] in ROWS]
    _check(not hidden, f"pending row also has a step, so a failure would be hidden: {hidden}")

    # Real home, and a non-temp path, are refused before anything is created.
    before = set(os.listdir(REAL_HOME)) if os.path.isdir(REAL_HOME) else set()
    _cli_fail(["--home", REAL_HOME, "setup"])
    _cli_fail(["--home", os.path.expanduser("~/.matrixkoder"), "scripted", "--gate", "G1"])
    scratch = os.path.join(ROOT, ".matrixkoder-livetest-probe")
    _check(not os.path.exists(scratch), "probe path already exists")
    _cli_fail(["--home", scratch, "setup"])
    _check(not os.path.exists(scratch), "refused setup created a directory")
    if os.path.isdir(REAL_HOME):
        _check(set(os.listdir(REAL_HOME)) == before, "real home was modified")

    # teardown refuses a path it did not create.
    alien = tempfile.mkdtemp(prefix="mk-livetest-alien-")
    try:
        proc = _cli_fail(["teardown", "--home", alien])
        _check("marker" in proc.stdout or "marker" in proc.stderr, proc.stdout + proc.stderr)
        _check(os.path.isdir(alien), "teardown removed a path it did not create")
    finally:
        if os.path.isdir(alien):
            shutil.rmtree(alien)

    # setup, one scripted row passes, an unimplemented row is PENDING, teardown leaves nothing.
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), "setup"],
                          check=True, capture_output=True, text=True)
    made = json.loads(proc.stdout.strip().splitlines()[-1])
    home, repo = made["home"], made["repo"]
    _check(os.path.isfile(made["root_db"]) and os.path.isfile(made["project_db"]), "stores missing")
    _check(os.path.isdir(os.path.join(repo, ".git")), "scratch repo missing")
    try:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--home", home, "scripted", "--gate", "G1"],
            capture_output=True, text=True)
        lines = [json.loads(l) for l in proc.stdout.strip().splitlines()]
        summary = lines[-1]
        by_id = {l["row"]: l for l in lines if "row" in l}
        _check(any(l.get("status") == "pass" for l in lines), "no scripted row passed")
        pending_ids = {r["id"] for r in matrix_rows() if r["pending"] and r["kind"] == "scripted"
                       and GATE_RANK[r["gate"]] <= GATE_RANK["G1"]}
        # Exit follows the matrix: 3 only while a G1 scripted row is still pending.
        # A row with a step is never pending (checked above), so a failing step exits 1.
        want = 3 if pending_ids else (1 if summary["summary"]["fail"] else 0)
        _check(proc.returncode == want, f"G1 exited {proc.returncode}, wanted {want}")
        for rid in pending_ids:
            _check(by_id.get(rid, {}).get("status") == "PENDING", f"{rid} was not PENDING")
        _check(summary["summary"]["PENDING"] == len(pending_ids), "PENDING count mismatch")
        if pending_ids:
            _check(summary.get("ok") is False, "PENDING gate reported ok")
        g1 = [r["id"] for r in _select("G1", matrix_rows())]
        g2 = [r["id"] for r in _select("G2", matrix_rows())]
        g3 = [r["id"] for r in _select("G3", matrix_rows())]
        _check(set(g1) <= set(g2) <= set(g3), "gates are not nested")
        _check(len(g3) == sum(1 for r in matrix_rows() if r["kind"] == "scripted"),
               "G3 is not the full scripted matrix")
        # The PENDING count follows the matrix, not a fixed number: the G2 scripted
        # rows grew steps, and the two agent rows (C7-connect, C13-untrusted) stay
        # pending. Exit 3 only while nothing failed.
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--home", home, "scripted", "--gate", "G2"],
            capture_output=True, text=True)
        g2_lines = [json.loads(l) for l in proc.stdout.strip().splitlines()]
        g2_summary = g2_lines[-1]
        g2_pending = {r["id"] for r in matrix_rows() if r["pending"] and r["kind"] == "scripted"
                      and GATE_RANK[r["gate"]] <= GATE_RANK["G2"]}
        _check(g2_summary["summary"]["PENDING"] == len(g2_pending),
               f"G2 PENDING {g2_summary['summary']['PENDING']} != matrix {len(g2_pending)}")
        # G2 is green once S1 writes project_summary. The invariant is that ok
        # is true exactly when nothing failed and nothing is pending.
        summary = g2_summary["summary"]
        _check(g2_summary.get("ok") is (summary["fail"] == 0 and summary["PENDING"] == 0),
               f"G2 ok={g2_summary.get('ok')} for fail={summary['fail']} PENDING={summary['PENDING']}")
        _check(all(l.get("status") != "pass" or not l.get("detail", "").startswith("pending")
                   for l in g2_lines if "row" in l), "a pending row was scored pass")
        want = 0 if summary["fail"] == 0 and summary["PENDING"] == 0 else (
            1 if summary["fail"] else 3)
        _check(proc.returncode == want, f"G2 exited {proc.returncode}, wanted {want}")
    finally:
        subprocess.run([sys.executable, os.path.abspath(__file__), "teardown", "--home", home],
                       check=True, capture_output=True, text=True)
    _check(not os.path.exists(home), "temp home survived teardown")
    _check(not os.path.exists(repo), "scratch repo survived teardown")
    parent = os.path.dirname(home)
    _check(not os.path.exists(parent), "temp parent survived teardown")

    # Injected rows: absent feature is PENDING (ok false), a raising row is fail, a clean set is ok.
    def boom(ctx):
        raise RuntimeError("boom")
    def fine(ctx):
        return None
    absent = [{"id": "X-absent", "contract": "C1", "gate": "G1", "kind": "scripted", "pending": "pending: DX"}]
    _, summary = scripted("G1", rows=absent)
    _check(summary["ok"] is False and summary["summary"]["PENDING"] == 1, "absent row was not PENDING")
    failing = [{"id": "X-boom", "contract": "C1", "gate": "G1", "kind": "scripted", "pending": ""}]
    saved_rows = dict(ROWS)
    ROWS["X-boom"] = boom
    try:
        _, summary = scripted("G1", rows=failing)
        _check(summary["ok"] is False and summary["summary"]["fail"] == 1,
               f"a gate with a failing row reported ok: {summary}")
    finally:
        ROWS.clear()
        ROWS.update(saved_rows)
    saved = dict(ROWS)
    try:
        ROWS["X-boom"] = boom
        ROWS["X-fine"] = fine
        bad = [{"id": "X-boom", "contract": "C1", "gate": "G1", "kind": "scripted", "pending": ""}]
        _, summary = scripted("G1", rows=bad)
        _check(summary["summary"]["fail"] == 1 and summary["ok"] is False, "raising row was not fail")
        good = [{"id": "X-fine", "contract": "C1", "gate": "G1", "kind": "scripted", "pending": ""}]
        _, summary = scripted("G1", rows=good)
        _check(summary["ok"] is True and summary["summary"]["pass"] == 1, "clean set was not ok")
    finally:
        ROWS.clear()
        ROWS.update(saved)

    # A tampered marker cannot delete a file outside the setup root.
    victim_dir = tempfile.mkdtemp(prefix="mk-livetest-victim-")
    victim = os.path.join(victim_dir, "victim_probe.txt")
    with open(victim, "w", encoding="utf-8") as fh:
        fh.write("sensitive")
    try:
        fake = tempfile.mkdtemp(prefix="mk-livetest-fake-")
        h = os.path.join(fake, "home")
        os.makedirs(h)
        with open(os.path.join(h, MARKER), "w", encoding="utf-8") as fh:
            json.dump({"home": h, "created": [victim, h]}, fh)
        proc = _cli_fail(["teardown", "--home", h])
        _check(os.path.isfile(victim), "tampered marker deleted the victim")
        _check("token" in proc.stdout or "token" in proc.stderr, proc.stdout + proc.stderr)
        for bad_root in (temp_root(), "/", os.path.expanduser("~")):
            link = os.path.join(fake, "link")
            if os.path.lexists(link):
                os.unlink(link)
            os.symlink(bad_root, link)
            proc = _cli_fail(["teardown", "--home", link])
            _check(os.path.exists(bad_root), f"teardown followed a symlink to {bad_root}")
    finally:
        shutil.rmtree(victim_dir)
        if os.path.isdir(fake):
            shutil.rmtree(fake)

    # teardown with no --home is a clean error, and --home is accepted on either side.
    proc = _cli_fail(["teardown"])
    _check("teardown needs --home" in proc.stdout, proc.stdout)
    _check("Traceback" not in proc.stderr, proc.stderr)
    proc = _cli_fail(["--home", REAL_HOME, "teardown"])
    _check("refusing the real MK home" in proc.stdout, proc.stdout)
    _check("Traceback" not in proc.stderr, proc.stderr)

    # This file never opens a store itself, read-write or otherwise.
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    needle = "sqlite3" + ".connect("
    _check(not any(line.lstrip().startswith(needle) for line in src.splitlines()),
           "database connect call in the harness")
    # R1.2: the mkdb write guard refuses the real home from this suite too.
    mkdb.selftest_guard_probe(_live_home)
    print("SELF-TEST PASS")


def _emit(payload):
    print(json.dumps(payload))


def _parser():
    """--home is one option, accepted before or after the subcommand.

    Each subparser redefines it with default SUPPRESS, so a value given on the parent
    is not overwritten by the subparser's default of None.
    """
    p = Parser(prog="mk-livetest.py", description=__doc__,
               formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", help="MK home; must be under the system temp dir, never ~/.matrixkoder")
    sub = p.add_subparsers(dest="cmd")
    su = sub.add_parser("setup")
    su.add_argument("--slug", default="lt")
    su.add_argument("--home", default=argparse.SUPPRESS)
    td = sub.add_parser("teardown")
    td.add_argument("--home", default=argparse.SUPPRESS)
    sc = sub.add_parser("scripted")
    sc.add_argument("--gate", required=True)
    sc.add_argument("--home", default=argparse.SUPPRESS)
    return p


def _exit_for(summary):
    """0 when ok, 1 when a row failed, 3 when nothing failed but some rows are PENDING."""
    if summary["summary"]["fail"]:
        return 1
    if summary["summary"]["PENDING"]:
        return 3
    return 0


def main(argv):
    if argv == ["--self-test"]:
        self_test()
        return 0
    args = _parser().parse_args(argv)
    home = getattr(args, "home", None)
    # Refuse a bad --home before any command creates anything, on every subcommand.
    if home:
        refuse_home(home)
    if args.cmd == "setup":
        _emit(setup(args.slug))
        return 0
    if args.cmd == "teardown":
        _emit(teardown(home))
        return 0
    if args.cmd == "scripted":
        results, summary = scripted(args.gate, home)
        for item in results:
            _emit(item)
        _emit(summary)
        return _exit_for(summary)
    raise Fail("usage: mk-livetest.py setup|teardown|scripted  (or --self-test)")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Fail as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(e.code)
