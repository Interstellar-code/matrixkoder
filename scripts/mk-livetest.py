#!/usr/bin/env python3
"""Live-test harness for the mk-store gates. Python 3 stdlib only.

  setup [--slug SLUG]
      TEMP MK home (tempfile, never ~/.matrixkoder), a scratch git repo with one commit,
      and the root and project stores via scripts/mkdb.py. One JSON line with every path.
      Writes a marker file listing exactly what it created.
  teardown --home DIR
      Removes only what that marker lists. Refuses anything else: the path must be the
      temp home the marker names.
  scripted --gate G1|G2|G3 [--home DIR]
      Every matrix row for that gate that needs no AI. One JSON line per row
      (pass / fail / PENDING) and a summary line. A row whose feature is absent, or
      whose matrix cell says pending, is PENDING, never pass.
  --self-test

Common: --home DIR. A home that resolves to ~/.matrixkoder, or that is not under the
system temp dir, exits non-zero before anything is created.
Every command prints ONE JSON line. Errors: {"ok": false, "error": ...} exit 2.
The matrix is docs/live-test-mk-store.md. Store access goes through scripts/mkdb.py only.
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mkdb  # the only store opener; this file never calls sqlite3.connect

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MATRIX = os.path.join(ROOT, "docs", "live-test-mk-store.md")
MARKER = "mk-livetest-marker.json"
GATES = ("G1", "G2", "G3")
REAL_HOME = os.path.abspath(os.path.expanduser("~/.matrixkoder"))
ROW_RE = re.compile(r"^\| (C\d+[\w-]*) \| (C\d+) \| (G[123]) \| (scripted|agent) \| ([^|]*?) \|")


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


def _write_marker(home, created):
    with open(_marker_path(home), "w", encoding="utf-8") as fh:
        json.dump({"home": home, "created": created}, fh)


def _read_marker(home):
    path = _marker_path(home)
    if not os.path.isfile(path):
        raise Fail(f"no marker at {path}; teardown only removes what setup created")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if os.path.realpath(data.get("home", "")) != os.path.realpath(home):
        raise Fail("marker home does not match; refusing")
    return data


def setup(slug="lt"):
    """Temp home, scratch repo with one commit, both stores. Returns the JSON payload."""
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
    created = [home, repo, os.path.join(home, "mk.db"), os.path.join(data, "mk.db")]
    _write_marker(home, created)
    return {"ok": True, "home": home, "repo": repo, "data": data, "slug": slug,
            "root_db": os.path.join(home, "mk.db"), "project_db": os.path.join(data, "mk.db"),
            "marker": _marker_path(home)}


def _rm(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def teardown(home):
    """Remove only the paths the marker lists, longest first, then the marker itself."""
    home = refuse_home(home)
    data = _read_marker(home)
    listed = [os.path.realpath(p) for p in data["created"]]
    root = temp_root()
    for path in listed:
        if path != root and not path.startswith(root + os.sep):
            raise Fail(f"marker lists a path outside the temp dir: {path}")
        if path == REAL_HOME or path.startswith(REAL_HOME + os.sep):
            raise Fail(f"marker lists the real home: {path}")
    # The marker's home directory goes last so a partial failure can be retried.
    for path in sorted(listed, key=len, reverse=True):
        if os.path.lexists(path):
            _rm(path)
    parent = os.path.dirname(home)
    if os.path.isdir(parent) and os.path.basename(parent).startswith("mk-livetest-"):
        shutil.rmtree(parent)
    return {"ok": True, "removed": listed}


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
    return "pass", ""


def _need(ctx):
    if not ctx.get("home"):
        raise Fail("scripted needs a home from setup (pass --home)")
    return ctx


def _pragmas(con):
    """The connection mkdb just opened, so the PRAGMAs are the ones it set."""
    mode, busy, fk = (con.execute("PRAGMA journal_mode").fetchone()[0],
                      con.execute("PRAGMA busy_timeout").fetchone()[0],
                      con.execute("PRAGMA foreign_keys").fetchone()[0])
    if (mode, busy, fk) != ("wal", 5000, 1):
        raise Fail(f"pragmas {mode}, {busy}, {fk}")


def row_c1_create(ctx):
    db = os.path.join(_need(ctx)["home"], "mk.db")
    if not os.path.isfile(db):
        raise Fail("root store missing")
    con = mkdb.open_root(ctx["home"])
    try:
        _pragmas(con)
    finally:
        con.close()


def row_c2_create(ctx):
    db = os.path.join(_need(ctx)["data"], "mk.db")
    if not os.path.isfile(db):
        raise Fail("project store missing")
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        _pragmas(con)
    finally:
        con.close()


def _seed(ctx):
    """One run and one task, so timeline and transition have something to read."""
    if ctx.get("seeded"):
        return
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        mkdb._begin(con)
        con.execute("INSERT INTO runs(id, status) VALUES (?, ?)", ("r", "active"))
        con.execute("COMMIT")
        mkdb.add_task(con, "r", "phase", "phase", title="phase")
        mkdb.add_task(con, "r", "task", "task", title="task", parent="phase")
    finally:
        con.close()
    ctx["seeded"] = True


def row_c2_timeline(ctx):
    _seed(_need(ctx))
    mkdb.transition(ctx["home"], ctx["slug"], "r", "task", "building", note="lt", data_dir=ctx["data"])
    out = subprocess.run(
        [sys.executable, os.path.join(HERE, "mkdb.py"), "--home", ctx["home"], "--data", ctx["data"],
         "--project", ctx["slug"], "timeline", "--run", "r", "--task", "task", "--json"],
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
        mkdb.add_task(con, "r", "tree-phase", "phase", title="p")
        mkdb.add_task(con, "r", "tree-task", "task", title="t", parent="tree-phase")
        mkdb.add_task(con, "r", "tree-sub", "subtask", title="s", parent="tree-task")
        try:
            mkdb.add_task(con, "r", "orphan", "task", parent="no-such")
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
            mkdb.add_task(con, "r", "self2", "task", deps="self2")
        except mkdb.Fail as e:
            if "self-dep" not in str(e):
                raise Fail(f"self-dep not named: {e}")
        else:
            raise Fail("self-dep was accepted")
        if con.execute("SELECT 1 FROM tasks WHERE run=? AND id=?", ("r", "self2")).fetchone():
            raise Fail("self-dep row was stored")
        mkdb.add_task(con, "r", "left", "task", title="l")
        mkdb.add_task(con, "r", "right", "task", title="r", deps="left")
        # No update API: check_deps is the same check add_task runs before insert.
        try:
            mkdb.check_deps(con, "r", "left", "right")
        except mkdb.Fail as e:
            if "cycle:" not in str(e) or "left" not in str(e):
                raise Fail(f"cycle not named: {e}")
        else:
            raise Fail("cycle was accepted")
        if con.execute("SELECT deps FROM tasks WHERE run=? AND id=?", ("r", "left")).fetchone()["deps"]:
            raise Fail("cycle wrote a dep")
    finally:
        con.close()


def row_c4_columns(ctx):
    _seed(_need(ctx))
    con = mkdb.open_project(ctx["home"], data_dir=ctx["data"])
    try:
        mkdb.add_task(con, "r", "col", "task", title="c", parent="phase")
    finally:
        con.close()
    want = [("planned", "Backlog", 0), ("building", "Building", 0), ("review", "Review", 0),
            ("done", "Done", 0), ("blocked", "Done", 1)]
    # planned is the insert status; transition the rest, syncing the root so cards can see it.
    for status, col, attention in want[1:]:
        mkdb.transition(ctx["home"], ctx["slug"], "r", "col", status, sync_root=True, data_dir=ctx["data"])
        got = mkdb.cards(ctx["home"], ctx["slug"])
        card = next((c for c in got if c["task"] == "col"), None)
        if not card or card["col"] != col or card["attention"] != attention:
            raise Fail(f"{status}: card {card}")


def row_c10(ctx):
    _seed(_need(ctx))
    before = mkdb.timeline(ctx["home"], ctx["slug"], "r", "task", ctx["data"])
    change = mkdb.transition(ctx["home"], ctx["slug"], "r", "task", "review", note="c10",
                             data_dir=ctx["data"])
    after = mkdb.timeline(ctx["home"], ctx["slug"], "r", "task", ctx["data"])
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
    mkdb.transition(home, ctx["slug"], "r", "task", "building", note="wal", data_dir=ctx["data"])
    wal = os.path.join(ctx["data"], "mk.db-wal")
    if os.path.isfile(wal) and not os.path.realpath(wal).startswith(os.path.realpath(home) + os.sep):
        raise Fail("wal escaped the temp home")
    for dirpath, names, files in os.walk(repo):
        for name in files:
            if name.endswith(".db") or name.endswith(".db-wal"):
                raise Fail(f"store file inside the scratch repo: {name}")


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
    "C14-harness": row_c14,
}


def scripted(gate, home=None):
    if gate not in GATES:
        raise Fail(f"gate must be one of {GATES}")
    ctx = {}
    if home:
        home = refuse_home(home)
        data = _read_marker(home)
        ctx = {"home": home, "slug": "lt"}
        # The marker lists the repo; the project data dir is the one store under projects/.
        for path in data["created"]:
            if path.rstrip("/").endswith("/repo") or os.path.basename(path) == "repo":
                ctx["repo"] = path
        projects = os.path.join(home, "projects")
        if os.path.isdir(projects):
            slugs = [s for s in os.listdir(projects) if os.path.isdir(os.path.join(projects, s))]
            if slugs:
                ctx["slug"] = slugs[0]
                ctx["data"] = os.path.join(projects, slugs[0])
                ctx["repo"] = ctx.get("repo")
    results = []
    for row in matrix_rows():
        if row["gate"] != gate or row["kind"] != "scripted":
            continue
        status, detail = _run_row(row, ctx)
        item = {"ok": status != "fail", "row": row["id"], "contract": row["contract"],
                "status": status}
        if detail:
            item["detail"] = detail
        results.append(item)
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("pass", "fail", "PENDING")}
    return results, {"ok": counts["fail"] == 0, "gate": gate, "summary": counts}


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
    # The doc has a row for every contract C1..C13.
    rows = matrix_rows()
    have = {r["contract"] for r in rows}
    for n in range(1, 14):
        _check(f"C{n}" in have, f"matrix has no row for C{n}")
    _check(any(r["kind"] == "agent" for r in rows), "no agent row")
    _check(any(r["pending"] for r in rows), "no pending row")

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
            check=True, capture_output=True, text=True)
        lines = [json.loads(l) for l in proc.stdout.strip().splitlines()]
        summary = lines[-1]
        by_id = {l["row"]: l for l in lines if "row" in l}
        _check(any(l.get("status") == "pass" for l in lines), "no scripted row passed")
        pending = [l for l in lines if l.get("status") == "PENDING"]
        _check(pending, "unimplemented row was not PENDING")
        _check(all(l["status"] != "pass" for l in pending), "PENDING row counted as pass")
        _check(summary.get("summary", {}).get("PENDING", 0) == len(pending), "summary mismatch")
        _check("C8-catalog" in by_id and by_id["C8-catalog"]["status"] == "PENDING",
               "C8-catalog (no catalog yet) was not PENDING")
    finally:
        subprocess.run([sys.executable, os.path.abspath(__file__), "teardown", "--home", home],
                       check=True, capture_output=True, text=True)
    _check(not os.path.exists(home), "temp home survived teardown")
    _check(not os.path.exists(repo), "scratch repo survived teardown")
    parent = os.path.dirname(home)
    _check(not os.path.exists(parent), "temp parent survived teardown")

    # This file never opens a store itself, read-write or otherwise.
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    _check(not any(line.lstrip().startswith("sqlite3.connect(") for line in src.splitlines()),
           "sqlite3.connect in the harness")
    print("SELF-TEST PASS")


def _emit(payload):
    print(json.dumps(payload))


def main(argv):
    if argv == ["--self-test"]:
        self_test()
        return 0
    p = Parser(prog="mk-livetest.py", description=__doc__,
               formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", help="MK home; must be under the system temp dir, never ~/.matrixkoder")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("setup")
    td = sub.add_parser("teardown")
    td.add_argument("--home")
    sc = sub.add_parser("scripted")
    sc.add_argument("--gate", required=True)
    args = p.parse_args(argv)
    # Refuse a bad --home before any command creates anything.
    if args.home:
        refuse_home(args.home)
    if args.cmd == "setup":
        _emit(setup())
        return 0
    if args.cmd == "teardown":
        home = args.home or getattr(args, "home", None)
        # teardown's own --home lands on the subparser; the parent --home is args too only
        # when given before the command. Accept either position.
        _emit(teardown(args.home))
        return 0
    if args.cmd == "scripted":
        results, summary = scripted(args.gate, args.home)
        for item in results:
            _emit(item)
        _emit(summary)
        return 0 if summary["ok"] else 1
    raise Fail("usage: mk-livetest.py setup|teardown|scripted  (or --self-test)")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Fail as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(e.code)
