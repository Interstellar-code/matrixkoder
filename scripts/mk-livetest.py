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
            "root_db": os.path.join(home, "mk.db"), "project_db": os.path.join(data, "mk.db"),
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


def teardown(home):
    """Remove the setup root, and nothing the marker lists. The marker only confirms it."""
    if not home:
        raise Fail("teardown needs --home")
    home = refuse_home(home)
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
    # planned is the insert status; transition the rest, syncing the root so cards can see it.
    for status, col, attention in want[1:]:
        mkdb.transition(ctx["home"], ctx["slug"], ctx["run"], "col-" + ctx["run"], status, sync_root=True, data_dir=ctx["data"])
        got = mkdb.cards(ctx["home"], ctx["slug"])
        card = next((c for c in got if c["task"] == "col-" + ctx["run"]), None)
        if not card or card["col"] != col or card["attention"] != attention:
            raise Fail(f"{status}: card {card}")


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
            capture_output=True, text=True)
        _check(proc.returncode == 3, f"G1 with PENDING rows exited {proc.returncode}")
        lines = [json.loads(l) for l in proc.stdout.strip().splitlines()]
        summary = lines[-1]
        by_id = {l["row"]: l for l in lines if "row" in l}
        _check(any(l.get("status") == "pass" for l in lines), "no scripted row passed")
        pending_ids = {r["id"] for r in matrix_rows() if r["pending"] and r["kind"] == "scripted"
                       and GATE_RANK[r["gate"]] <= GATE_RANK["G1"]}
        _check(pending_ids, "matrix has no pending G1 row")
        for rid in pending_ids:
            _check(by_id.get(rid, {}).get("status") == "PENDING", f"{rid} was not PENDING")
        _check(summary.get("ok") is False, "PENDING gate reported ok")
        _check(summary["summary"]["PENDING"] == len(pending_ids), "PENDING count mismatch")
        g1 = [r["id"] for r in _select("G1", matrix_rows())]
        g2 = [r["id"] for r in _select("G2", matrix_rows())]
        g3 = [r["id"] for r in _select("G3", matrix_rows())]
        _check(set(g1) <= set(g2) <= set(g3), "gates are not nested")
        _check(len(g3) == sum(1 for r in matrix_rows() if r["kind"] == "scripted"),
               "G3 is not the full scripted matrix")
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
