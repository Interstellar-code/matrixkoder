#!/usr/bin/env python3
"""MatrixKoder migration — move ONE project's MK data onto the C2 tree (plan storage-layout, L2).

  mk-migrate.py [--dry-run] [--switch] [--rollback] [--archive]
                [--slug S] [--data DIR] [--home DIR] [--repo DIR]
  mk-migrate.py --self-test

Default (no flag): quiesce, copy, verify. Leaves the C2 copy in place; `projects.db_path`
stays NULL, so every reader keeps the old path.
--switch    : the same copy+verify under the switch lock, then set `projects.db_path`.
--rollback  : undo a switch. Refuses when the NEW DB changed after the switch (or when the
              old DB changed), else carries the new DB back new->old with the same verify.
--archive   : AFTER the user confirms, tar the old <repo>/.matrixkoder into
              <MK home>/projects/<slug>/legacy/. Never deletes the old folder or the old DB.

Contracts (read-only here, they are the plan's):
  C2 — the target tree. Every target path comes from mkpaths.target_*.
  C3 — the root schema is frozen: this tool touches only `projects.db_path`.
  C5 — quiesce -> copy -> verify (under the switch lock) -> --switch -> checklist -> --archive.
       A failed verify removes the NEW copy only. No command deletes the old folder.
  C7 — never touch the live bus: lane state and the bus inbox are read through the existing
       readers (the lane registry, mk-bus's StoreAdapter), never by opening bus files.

This tool never runs SQLite read-write except through mkdb.open_root / mkdb.open_project /
mkdb._connect (mkdb is the single writer). It never copies a live DB by file copy: the whole
project DB moves through SQLite's backup API.
"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mkpaths = _load("mkpaths", os.path.join(HERE, "mkpaths.py"))  # C1: one path module
mkdb = _load("mkdb", os.path.join(HERE, "mkdb.py"))           # the only SQLite writer
mkbus = _load("mkbus", os.path.join(HERE, "mk-bus.py"))       # the bus's own reader


class Fail(Exception):
    def __init__(self, msg, code=2):
        super().__init__(msg)
        self.code = code


# Round files C2 keeps per task: brief / report / review / review-brief, round-scoped.
ROUND_RE = re.compile(r"^(?:brief|report|review|review-brief)-r\d+\.md$")
# Switch bookkeeping lives beside the C2 DB (the MK home, never the repo).
SWITCH_MARKER = ".migrate.json"


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def now_stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------- paths (all from mkpaths) ----------

def src_db(data):
    """<data>/mk.db — the project store, today's place."""
    return mkpaths.project_db(data=data)


def target_dir(home, slug):
    return mkpaths.target_project_dir(slug, home)


def switch_lock_path(home):
    """The one migrator lock, under the MK home (mkpaths.lock_path)."""
    return mkpaths.lock_path("migrate")


# ---------- the root row (C3: only projects.db_path is touched) ----------

def root_row(home, slug):
    """The root projects row for the slug, or None. A read never creates the root store."""
    for row in mkdb.projects(home):
        if row.get("slug") == slug:
            return row
    return None


def ensure_root_row(home, slug, data, repo):
    """The root projects row, registered when absent (the bus does the same on first use)."""
    row = root_row(home, slug)
    if row:
        return row
    con = mkdb.open_root(home)
    try:
        con.execute(
            "INSERT OR IGNORE INTO projects(slug, name, repo, data_dir, mode, created, last_activity)"
            " VALUES (?,?,?,?,?,?,?)",
            (slug, slug, repo, os.path.realpath(data), "home", now_iso(), now_iso()))
        con.commit()
    finally:
        con.close()
    return root_row(home, slug)


def set_db_path(home, slug, db_path):
    """--switch: set projects.db_path through mkdb.open_root (the C3 column; the L1 migration
    comment names mk-migrate as its writer). No other root column is touched."""
    con = mkdb.open_root(home)
    try:
        cur = con.execute("UPDATE projects SET db_path=?, last_activity=? WHERE slug=?",
                          (db_path, now_iso(), slug))
        con.commit()
        if cur.rowcount != 1:
            raise Fail(f"no projects row for slug {slug!r} to switch")
    finally:
        con.close()


def clear_db_path(home, slug):
    con = mkdb.open_root(home)
    try:
        con.execute("UPDATE projects SET db_path=NULL, last_activity=? WHERE slug=?",
                    (now_iso(), slug))
        con.commit()
    finally:
        con.close()


# ---------- quiesce (C5) ----------

def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def busy_lanes(data):
    """The lane registry's held slots for this data dir. Store mode reads `lane_slots` (rows are
    truth); file mode reads lanes.json. Read-only: never republishes the view."""
    db = src_db(data)
    out = []
    if db and os.path.isfile(db):
        con = mkdb._open_read(db, mkdb.SCHEMA)
        if con is None:
            return out
        try:
            if not _has_table(con, "lane_slots"):
                return out
            for row in con.execute("SELECT slot, lane_path, state, run, task FROM lane_slots"
                                   " WHERE state='busy' ORDER BY slot"):
                out.append(dict(row))
        finally:
            con.close()
        return out
    lanes = _read_json(mkpaths.lanes_file(data))
    if isinstance(lanes, dict) and isinstance(lanes.get("slots"), dict):
        for key, rec in lanes["slots"].items():
            if isinstance(rec, dict) and rec.get("state") == "busy":
                out.append({"slot": key, "lane_path": rec.get("path"), "state": "busy",
                            "run": rec.get("run"), "task": rec.get("task")})
    return out


def armed_lane_paths(data):
    """Slot worktrees holding `.matrixkoder-watch.json` — the watch file is what makes a lane
    armed (mk-lane-event's rule). Read-only; the registry supplies the paths."""
    db = src_db(data)
    paths = []
    if db and os.path.isfile(db):
        con = mkdb._open_read(db, mkdb.SCHEMA)
        if con is not None:
            try:
                if _has_table(con, "lane_slots"):
                    paths = [r["lane_path"] for r in con.execute(
                        "SELECT lane_path FROM lane_slots WHERE lane_path IS NOT NULL")]
            finally:
                con.close()
    else:
        lanes = _read_json(mkpaths.lanes_file(data))
        if isinstance(lanes, dict) and isinstance(lanes.get("slots"), dict):
            paths = [v.get("path") for v in lanes["slots"].values() if isinstance(v, dict)]
    return [p for p in paths if p and os.path.isfile(os.path.join(p, ".matrixkoder-watch.json"))]


def pending_asks(data):
    """Unanswered asks in the project store, through mk-bus's own reader (never a bus file).
    This is the `/pending` rule: an ask with no answer naming it."""
    store = mkbus.StoreAdapter(data)
    asks = store.store_select(
        "SELECT id, run, t FROM events WHERE chan='bus' AND kind='ask' ORDER BY id")
    answers = store.store_select(
        "SELECT reply_to FROM events WHERE chan='bus' AND kind='answer' AND reply_to IS NOT NULL")
    answered = {a["reply_to"] for a in answers if a.get("reply_to")}
    return [a for a in asks if f"m-{a['id']}" not in answered]


def quiesce(data):
    """Refuse unless no lane is armed or working and the bus inbox is drained (C5)."""
    problems = []
    busy = busy_lanes(data)
    if busy:
        held = ", ".join(f"slot {b['slot']} ({b.get('run') or '-'}/{b.get('task') or '-'})" for b in busy)
        problems.append(f"{len(busy)} lane(s) busy: {held}")
    armed = armed_lane_paths(data)
    if armed:
        problems.append(f"{len(armed)} lane(s) armed: {', '.join(armed)}")
    pend = pending_asks(data)
    if pend:
        ids = ", ".join(f"m-{a['id']}" for a in pend[:5])
        problems.append(f"{len(pend)} unanswered ask(s) on the bus inbox: {ids}")
    if problems:
        raise Fail("not quiesced (C5): " + "; ".join(problems), 3)


# ---------- the store (C3 schema: counts + a row digest) ----------

def _has_table(con, name):
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _tables(con):
    return [r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def db_state(con):
    """(counts, digest) over every table: the verify compares BOTH, so a rewrite that keeps the
    count still fails. Rows are read in rowid order, so the digest is deterministic."""
    counts, h = {}, hashlib.sha256()
    for t in _tables(con):
        n = 0
        for row in con.execute(f"SELECT * FROM {t} ORDER BY rowid"):
            n += 1
            h.update(repr(tuple(row)).encode())
        counts[t] = n
        h.update(t.encode())
    return counts, h.hexdigest()


def open_ro(path):
    """An existing store read-only, or None. Never creates and never migrates."""
    if not os.path.isfile(path):
        return None
    return mkdb._connect_ro(path)


def state_of(path):
    con = open_ro(path)
    if con is None:
        return None
    try:
        return db_state(con)
    finally:
        con.close()


def copy_db(src_path, dst_path):
    """The WHOLE project DB, through SQLite's backup API (never a file copy while open)."""
    src = mkdb._connect_ro(src_path)
    try:
        dst = mkdb._connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


# ---------- files ----------

def _hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_hash(root):
    """sha256 over (relative path, file hash) of every file under root."""
    h = hashlib.sha256()
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            p = os.path.join(dirpath, name)
            h.update(os.path.relpath(p, root).encode())
            h.update(_hash_file(p).encode())
    return h.hexdigest()


def _header(con, plan_id):
    row = con.execute("SELECT header_json FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row or not row["header_json"]:
        return {}
    try:
        got = json.loads(row["header_json"])
        return got if isinstance(got, dict) else {}
    except ValueError:
        return {}


def live_plans(con):
    """Plan ids with an active run (C5: only live plans move their files)."""
    if not _has_table(con, "plans"):
        return []
    return [r["id"] for r in con.execute(
        "SELECT DISTINCT p.id FROM plans p JOIN runs r ON r.plan=p.id"
        " WHERE r.status IN ('running','active') ORDER BY p.id")]


def plan_moves(data, home, slug, con, pid):
    """Every file of ONE live plan: plan.md, assets (the plan's own dir plus its design refs),
    its archived versions, and the round files of every task of every run of the plan."""
    out = []
    tdir = mkpaths.target_plan_dir(slug, pid, home)
    pf = mkpaths.plan_file(data, pid)
    if os.path.isfile(pf):
        out.append({"label": f"plans/{pid}/plan.md", "kind": "file", "src": pf,
                    "dst": os.path.join(tdir, "plan.md")})
    assets = mkpaths.plan_dir(data, pid)
    if os.path.isdir(assets):
        out.append({"label": f"plans/{pid}/assets/", "kind": "tree", "src": assets,
                    "dst": os.path.join(tdir, "assets")})
    repo = os.path.dirname(os.path.abspath(data))
    for rel in (_header(con, pid).get("design") or []):
        if not isinstance(rel, str) or not rel:
            continue
        src = os.path.join(repo, rel)
        if os.path.isfile(src):
            out.append({"label": f"plans/{pid}/assets/{os.path.basename(rel)}", "kind": "file",
                        "src": src, "dst": os.path.join(tdir, "assets", os.path.basename(rel))})
    arch_dir = mkpaths.plan_archives_dir(data)
    if os.path.isdir(arch_dir):
        for name in sorted(os.listdir(arch_dir)):
            if name.startswith(pid) and name.endswith(".md"):
                out.append({"label": f"plans/{pid}/archive/{name}", "kind": "file",
                            "src": os.path.join(arch_dir, name),
                            "dst": os.path.join(tdir, "archive", name)})
    if _has_table(con, "runs") and _has_table(con, "tasks"):
        for run in con.execute("SELECT id FROM runs WHERE plan=? ORDER BY id", (pid,)):
            rid = run["id"]
            for task in con.execute("SELECT id FROM tasks WHERE run=? ORDER BY id", (rid,)):
                tid = task["id"]
                td = mkpaths.task_dir(data, rid, tid)
                if not os.path.isdir(td):
                    continue
                for name in sorted(os.listdir(td)):
                    if ROUND_RE.match(name):
                        out.append({"label": f"plans/{pid}/runs/{rid}/{tid}/{name}", "kind": "file",
                                    "src": os.path.join(td, name),
                                    "dst": os.path.join(
                                        mkpaths.target_task_dir(slug, pid, rid, tid, home), name)})
    return out


def build_moves(data, home, slug, con):
    """Every non-DB move: project.yaml, watch-cursors.json, the lanes registry file (only when
    the store carries no rows) and every live plan's files. The DB itself moves via copy_db."""
    moves = []
    yaml = mkpaths.project_yaml(data)
    if os.path.isfile(yaml):
        moves.append({"label": "project.yaml", "kind": "file", "src": yaml,
                      "dst": mkpaths.target_project_yaml(slug, home)})
    wc = mkpaths.watch_cursors(data)
    if os.path.isfile(wc):
        moves.append({"label": "watch-cursors.json", "kind": "file", "src": wc,
                      "dst": mkpaths.target_watch_cursors(slug, home)})
    lanes_rows = 0
    if _has_table(con, "lane_slots"):
        lanes_rows = con.execute("SELECT COUNT(*) c FROM lane_slots").fetchone()["c"]
    lanes_json = mkpaths.lanes_file(data)
    if not lanes_rows and os.path.isfile(lanes_json):
        moves.append({"label": "lanes.json", "kind": "file", "src": lanes_json,
                      "dst": os.path.join(target_dir(home, slug), "lanes.json")})
    for pid in live_plans(con):
        moves += plan_moves(data, home, slug, con, pid)
    return moves


def copy_moves(moves):
    """Copy every move; return [(move, sha)] so the verify can re-hash both sides."""
    done = []
    for m in moves:
        if m["kind"] == "file":
            os.makedirs(os.path.dirname(m["dst"]), exist_ok=True)
            shutil.copy2(m["src"], m["dst"])
            done.append((m, _hash_file(m["src"])))
        else:
            shutil.copytree(m["src"], m["dst"], dirs_exist_ok=True)
            done.append((m, None))
    return done


def verify_moves(done):
    """Labels whose bytes moved on either side since the copy (a write landed during the copy)."""
    bad = []
    for m, want in done:
        if m["kind"] == "file":
            if _hash_file(m["src"]) != want or _hash_file(m["dst"]) != want:
                bad.append(m["label"])
        else:
            if _tree_hash(m["src"]) != _tree_hash(m["dst"]):
                bad.append(m["label"])
    return bad


def _after_copy_hook():
    """Test seam (never set in normal use): run a command between the copy and the verify, so a
    write landing during the copy can be injected."""
    cmd = os.environ.get("MK_MIGRATE_AFTER_COPY")
    if cmd:
        subprocess.run(cmd, shell=True, check=False)


# ---------- the copy + verify unit ----------

def _prepare(args):
    data = os.path.abspath(os.path.expanduser(args.data)) if args.data else mkpaths.data_dir()
    if not os.path.isdir(data):
        raise Fail(f"no data dir at {data}")
    home = mkpaths.home(args.home)
    slug = args.slug or mkpaths.project_slug_of(data)
    if not slug:
        raise Fail("no root projects row names this data dir; pass --slug to register one", 3)
    repo = args.repo or os.path.dirname(data)
    return home, data, slug, repo


def do_copy_verify(home, data, slug, dry_run=False):
    """Quiesce, copy, verify. Returns a summary dict. The caller holds the switch lock (or is a
    dry run: then nothing is written). A failed verify removes the NEW copy only.

    An existing C2 copy that already matches the source is reused (the copy-then-switch workflow):
    it is verified, never re-copied over. A copy that does NOT match refuses."""
    source = src_db(data)
    if source is None or not os.path.isfile(source):
        raise Fail(f"no project store at {source}")
    dest = mkpaths.target_project_db(slug, home)
    con = open_ro(source)
    if con is None:
        raise Fail(f"cannot read {source}")
    try:
        moves = build_moves(data, home, slug, con)
        src_counts, src_digest = db_state(con)
    finally:
        con.close()
    existing = os.path.exists(dest)

    if dry_run:
        for m in moves:
            print(f"{'verify' if existing else 'copy'} {m['src']} -> {m['dst']}")
        print(f"{'verify' if existing else 'copy'} {source} -> {dest}"
              f" (SQLite backup API, {sum(src_counts.values())} rows)")
        return {"ok": True, "command": "copy", "dry_run": True, "slug": slug, "data": data,
                "target": dest, "moves": len(moves) + 1, "db_counts": src_counts}

    if existing:
        # a prior copy is present: verify it, never overwrite it silently.
        done = [(m, _hash_file(m["src"]) if m["kind"] == "file" else None) for m in moves]
        bad = verify_moves(done)
        dst_counts, dst_digest = state_of(dest)
        if dst_counts != src_counts or dst_digest != src_digest:
            bad.append("mk.db")
        if bad:
            raise Fail("an existing C2 copy does not match the source: "
                       + "; ".join(sorted(set(bad))) + f" — inspect or remove {dest} and retry", 3)
        return {"ok": True, "command": "copy", "reused": True, "slug": slug, "data": data,
                "target": dest, "moves": len(moves) + 1, "db_counts": src_counts,
                "db_digest": src_digest}

    copy_db(source, dest)
    done = copy_moves(moves)
    _after_copy_hook()
    # verify: the DB (counts AND digest) and every copied file, under the caller's lock.
    dst_counts, dst_digest = state_of(dest)
    bad = verify_moves(done)
    if dst_counts != src_counts or dst_digest != src_digest:
        bad.append("mk.db")
    # re-read the source to catch a write that landed during the copy
    now_counts, now_digest = state_of(source)
    if now_counts != src_counts or now_digest != src_digest:
        bad.append("mk.db (the source changed during the copy)")
    if bad:
        _remove_new_copy(home, slug)
        raise Fail("verify failed: " + "; ".join(sorted(set(bad))) + " — the NEW copy was removed,"
                   " the old data is untouched", 3)
    return {"ok": True, "command": "copy", "slug": slug, "data": data, "target": dest,
            "moves": len(moves) + 1, "db_counts": src_counts, "db_digest": src_digest}


def _remove_new_copy(home, slug):
    """Remove ONLY what the C2 copy owns: its DB and the files a copy writes. `legacy/` (a user
    archive) and the switch marker are never removed. The old folder and old DB are untouched."""
    tdir = target_dir(home, slug)
    dbname = mkpaths.ROOT_DB_NAME
    for name in (dbname, dbname + "-wal", dbname + "-shm", "project.yaml", "watch-cursors.json",
                 "lanes.json", "plans"):
        p = os.path.join(tdir, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            with contextlib.suppress(OSError):
                os.remove(p)


# ---------- commands ----------

@contextlib.contextmanager
def switch_lock(home):
    """The one migrator lock under the MK home: a second migrator is refused, never queued."""
    path = switch_lock_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Fail(f"another migrator holds the switch lock ({path})", 3)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def cmd_copy(args):
    home, data, slug, repo = _prepare(args)
    if args.dry_run:
        quiesce(data)
        return do_copy_verify(home, data, slug, dry_run=True)
    ensure_root_row(home, slug, data, repo)
    with switch_lock(home):
        quiesce(data)
        return do_copy_verify(home, data, slug)


def cmd_switch(args):
    home, data, slug, repo = _prepare(args)
    if args.dry_run:
        quiesce(data)
        out = do_copy_verify(home, data, slug, dry_run=True)
        print(f"switch {slug}: set projects.db_path = {mkpaths.target_project_db(slug, home)}")
        out["command"] = "switch"
        return out
    ensure_root_row(home, slug, data, repo)
    row = root_row(home, slug)
    if row and row.get("db_path"):
        raise Fail(f"already migrated: projects.db_path = {row['db_path']}", 3)
    with switch_lock(home):
        quiesce(data)
        out = do_copy_verify(home, data, slug)
        dest = mkpaths.target_project_db(slug, home)
        before = src_db(data)
        old_state = state_of(before)
        new_state = state_of(dest)
        set_db_path(home, slug, dest)
        marker = {"slug": slug, "data_dir": os.path.realpath(data), "db_path": dest,
                  "old_digest": old_state[1], "old_counts": old_state[0],
                  "new_digest": new_state[1], "new_counts": new_state[0],
                  "switched_at": now_iso()}
        _write_marker(home, slug, marker)
    out["command"] = "switch"
    out["db_path"] = dest
    return out


def cmd_rollback(args):
    home, data, slug, repo = _prepare(args)
    row = root_row(home, slug)
    if not row or not row.get("db_path"):
        raise Fail("nothing to roll back: projects.db_path is not set", 3)
    marker = _read_marker(home, slug)
    if not marker:
        raise Fail(f"no switch record at {os.path.join(target_dir(home, slug), SWITCH_MARKER)};"
                   " refusing to guess what to roll back", 3)
    if args.dry_run:
        print(f"rollback {slug}: carry {row['db_path']} back to {src_db(data)}, clear db_path")
        return {"ok": True, "command": "rollback", "dry_run": True, "slug": slug,
                "from": row["db_path"], "to": src_db(data)}
    dest = mkpaths.target_project_db(slug, home)
    new_state = state_of(dest)
    old_state = state_of(src_db(data))
    # C5: refuse when the NEW DB moved after the switch; a carry-back would lose that write.
    if new_state is None:
        raise Fail(f"the new store is gone: {dest}", 3)
    if new_state[1] != marker.get("new_digest"):
        raise Fail("the new DB changed after the switch; refusing to roll back (its writes would be"
                   " lost). Re-run without changes, or keep the new layout", 3)
    # the old DB is the destination of the carry-back: a change there would be overwritten.
    if old_state is not None and old_state[1] != marker.get("old_digest"):
        raise Fail("the OLD DB changed after the switch; refusing to overwrite it", 3)
    with switch_lock(home):
        copy_db(dest, src_db(data))
        back = state_of(src_db(data))
        if back is None or back != new_state:
            raise Fail("rollback verify failed: the carried-back DB does not match the new DB", 3)
        clear_db_path(home, slug)
        _remove_new_copy(home, slug)
    return {"ok": True, "command": "rollback", "slug": slug, "from": dest, "to": src_db(data),
            "counts": new_state[0]}


def cmd_archive(args):
    home, data, slug, repo = _prepare(args)
    row = root_row(home, slug)
    if not row or not row.get("db_path"):
        raise Fail("refusing to archive before the switch: run --switch first", 3)
    legacy = os.path.join(target_dir(home, slug), "legacy")
    out = os.path.join(legacy, f"{slug}-{now_stamp()}.tar.gz")
    if args.dry_run:
        print(f"archive {data} -> {out} (the old folder stays)")
        return {"ok": True, "command": "archive", "dry_run": True, "slug": slug, "tar": out}
    os.makedirs(legacy, exist_ok=True)
    with tarfile.open(out, "w:gz") as tf:
        tf.add(data, arcname=os.path.basename(data.rstrip(os.sep)))
    members = count_tar_files(out)
    on_disk = sum(len(files) for _, _, files in os.walk(data))
    if members != on_disk:
        raise Fail(f"archive incomplete: {members} files in {out}, {on_disk} on disk", 3)
    # the old folder and the old DB are never deleted here.
    if not os.path.isdir(data) or not os.path.isfile(src_db(data)):
        raise Fail("the old data folder or DB vanished during --archive", 3)
    return {"ok": True, "command": "archive", "slug": slug, "tar": out, "files": members}


def count_tar_files(path):
    """Regular-file members in a tar, with the handle closed."""
    with tarfile.open(path) as tf:
        return sum(1 for m in tf if m.isfile())


def _write_marker(home, slug, marker):
    path = os.path.join(target_dir(home, slug), SWITCH_MARKER)
    with open(path, "w") as f:
        json.dump(marker, f, indent=2, sort_keys=True)


def _read_marker(home, slug):
    return _read_json(os.path.join(target_dir(home, slug), SWITCH_MARKER))


# ---------- cli ----------

class Parser(argparse.ArgumentParser):
    def __init__(self, *a, **kw):
        kw.setdefault("allow_abbrev", False)
        super().__init__(*a, **kw)


def build_parser():
    p = Parser(prog="mk-migrate.py", description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="print every move; write nothing")
    p.add_argument("--switch", action="store_true", help="copy+verify under the lock, then set projects.db_path")
    p.add_argument("--rollback", action="store_true", help="undo a switch (refuses on new writes)")
    p.add_argument("--archive", action="store_true", help="tar the old <repo>/.matrixkoder under the MK home")
    p.add_argument("--slug", help="project slug (registered in the root when absent)")
    p.add_argument("--data", help="the project data dir (default: the data-dir order)")
    p.add_argument("--home", help="the MK home (default $MK_HOME / ~/.matrixkoder)")
    p.add_argument("--repo", help="the repo path for the root row (default: dirname of --data)")
    return p


def main(argv):
    if argv == ["--self-test"]:
        return self_test()
    args = build_parser().parse_args(argv)
    try:
        if args.archive:
            out = cmd_archive(args)
        elif args.rollback:
            out = cmd_rollback(args)
        elif args.switch:
            out = cmd_switch(args)
        else:
            out = cmd_copy(args)
    except Fail as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return e.code
    print(json.dumps(out, sort_keys=True))
    return 0


# ---------- self-test ----------

def _check(cond, msg):
    if not cond:
        raise SystemExit(f"SELF-TEST FAIL: {msg}")


SYNTHETIC_PLAN = b"""+++
plan = "fixture"
status = "running"
created = 2026-10-14
runs = ["fixture-r1"]
design = []
integration = "main"

[[phase]]
id = "P1"
title = "one phase"
wave = 1
deps = []
owned = []
forbidden = []
route = ""
reviewer = ""
size = "standard"
risk = "reversible"
review = "light"
qa = []
commit = "feat: p1"
subagents = false
lane_group = ""

  [[phase.task]]
  id = "P1.1"
  title = "one task"
  acceptance = "it works"
  subagent = false
  owned = []
+++
"""


def _plan_fixture():
    """One REAL small plan file (its bytes) when the main checkout's plans/archive is reachable,
    else a synthetic one with the same header shape."""
    cands = []
    env = os.environ.get("MK_MIGRATE_PLAN_FIXTURE")
    if env:
        cands.append(env)
    try:
        top = mkpaths.repo_top()
    except Exception:
        top = None
    if top:
        cands.append(os.path.join(top, ".matrixkoder", "plans", "archive"))
    for cand in cands:
        if os.path.isdir(cand):
            files = [n for n in os.listdir(cand) if n.endswith(".md")]
            if files:
                name = min(files, key=lambda n: os.path.getsize(os.path.join(cand, n)))
                with open(os.path.join(cand, name), "rb") as f:
                    return f.read()
    return SYNTHETIC_PLAN


def _seed(home, data, repo, *, busy=False, ask=False):
    """A temp project store through the production mkdb open path, two plans (one live, one done),
    tasks, round files, a watch-cursors file and an archived plan version."""
    os.makedirs(data, exist_ok=True)
    root = mkdb.open_root(home)
    try:
        root.execute("INSERT OR IGNORE INTO projects(slug, name, repo, data_dir, mode, created,"
                     " last_activity) VALUES (?,?,?,?,?,?,?)",
                     ("fixture", "Fixture", repo, os.path.realpath(data), "home", now_iso(), now_iso()))
        root.commit()
    finally:
        root.close()
    con = mkdb.open_project(home, "fixture", data_dir=data)
    try:
        con.execute("INSERT INTO plans(id, version, title, status, header_json) VALUES"
                    " ('live', 1, 'Live', 'running', ?)",
                    (json.dumps({"design": [".matrixkoder/plans/live-research.md"]}),))
        con.execute("INSERT INTO plans(id, version, title, status, header_json) VALUES"
                    " ('done', 1, 'Done', 'done', '{}')")
        con.execute("INSERT INTO runs(id, plan, status, started) VALUES ('r1', 'live', 'active', 't')")
        con.execute("INSERT INTO runs(id, plan, status, started) VALUES ('r0', 'done', 'done', 't')")
        con.execute("INSERT INTO lane_slots(slot, lane_path, state, run, task) VALUES"
                    " ('1', ?, ?, 'r1', 'P1.1')",
                    (os.path.join(data, "..", "lane1"), "busy" if busy else "free"))
        con.commit()
    finally:
        con.close()
    mkdb.add_task(mkdb.open_project(home, "fixture", data_dir=data), "r1", "P1.1", "task", title="one")
    if ask:
        mkdb.append_event(home, "fixture", "r1", task="P1.1", chan="bus", kind="ask",
                          from_id="orch", to_id="P1.1", text="q", data_dir=data)
    # files: plan file, archive version, design ref, round files, watch-cursors
    os.makedirs(os.path.join(data, "plans", "archive"), exist_ok=True)
    plan_bytes = _plan_fixture()
    with open(os.path.join(data, "plans", "live.md"), "wb") as f:
        f.write(plan_bytes)
    with open(os.path.join(data, "plans", "archive", "live-v1.md"), "wb") as f:
        f.write(plan_bytes)
    with open(os.path.join(data, "plans", "live-research.md"), "wb") as f:
        f.write(b"# research\n")
    td = os.path.join(data, "runs", "r1", "tasks", "P1.1")
    os.makedirs(td, exist_ok=True)
    for name, body in (("brief-r1.md", b"# brief\n"), ("report-r1.md", b"# report\n"),
                       ("review-r1.md", b"# review\n"), ("task.json", b'{"id":"P1.1"}')):
        with open(os.path.join(td, name), "wb") as f:
            f.write(body)
    with open(os.path.join(data, "watch-cursors.json"), "w") as f:
        json.dump({"r1|*": {"seq": 1, "started": None}}, f)
    with open(os.path.join(data, "matrixkoder.yaml"), "w") as f:
        f.write("data_dir: .matrixkoder/\n")
    return plan_bytes


def _cli(home, args, env=None):
    e = dict(os.environ)
    e.update({"MK_HOME": home, "MATRIXKODER_HOME": home, "MK_SELFTEST": "1"})
    if env:
        e.update(env)
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), *args],
                          capture_output=True, text=True, env=e)
    return proc.returncode, proc.stdout, proc.stderr


def _last_json(text):
    for line in reversed([ln for ln in text.splitlines() if ln.strip()]):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def self_test():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="mk-migrate-test-"))
    saved = {k: os.environ.get(k) for k in
             ("MK_HOME", "MATRIXKODER_HOME", "MK_SELFTEST", "MK_MIGRATE_AFTER_COPY")}
    home = os.path.join(tmp, "home")
    repo = os.path.join(tmp, "repo")
    data = os.path.join(repo, ".matrixkoder")
    os.environ.update({"MK_HOME": home, "MATRIXKODER_HOME": home, "MK_SELFTEST": "1"})
    os.environ.pop("MK_MIGRATE_AFTER_COPY", None)
    checks = []

    def ok(name):
        checks.append(name)

    try:
        os.makedirs(repo, exist_ok=True)
        subprocess.run(["git", "init", "-q", repo], check=False)
        _seed(home, data, repo)

        # 0. R1.2: the mkdb write guard refuses the real home from this suite too.
        mkdb.selftest_guard_probe(home)
        ok("selftest-guard")

        # 1. quiesce: clean store passes; a busy lane and an unanswered ask each refuse.
        quiesce(data)
        ok("quiesce-clean")
        con = mkdb.open_project(home, "fixture", data_dir=data)
        con.execute("UPDATE lane_slots SET state='busy' WHERE slot='1'")
        con.commit()
        con.close()
        try:
            quiesce(data)
            raise SystemExit("SELF-TEST FAIL: quiesce passed with a busy lane")
        except Fail as e:
            _check(e.code == 3 and "lane" in str(e), f"busy-lane refusal: {e}")
        ok("quiesce-busy-lane")
        con = mkdb.open_project(home, "fixture", data_dir=data)
        con.execute("UPDATE lane_slots SET state='free' WHERE slot='1'")
        con.commit()
        con.close()
        mkdb.append_event(home, "fixture", "r1", task="P1.1", chan="bus", kind="ask",
                          from_id="orch", to_id="P1.1", text="q", data_dir=data)
        try:
            quiesce(data)
            raise SystemExit("SELF-TEST FAIL: quiesce passed with an unanswered ask")
        except Fail as e:
            _check(e.code == 3 and "ask" in str(e), f"ask refusal: {e}")
        ok("quiesce-undrained-inbox")
        # an armed lane works through its watch file even with a free registry slot
        lane_dir = os.path.join(tmp, "lane-armed")
        os.makedirs(lane_dir, exist_ok=True)
        with open(os.path.join(lane_dir, ".matrixkoder-watch.json"), "w") as f:
            json.dump({"lane": lane_dir, "task": "P1.1", "run": "r1"}, f)
        con = mkdb.open_project(home, "fixture", data_dir=data)
        con.execute("UPDATE lane_slots SET lane_path=? WHERE slot='1'", (lane_dir,))
        con.commit()
        con.close()
        try:
            quiesce(data)
            raise SystemExit("SELF-TEST FAIL: quiesce passed with an armed lane")
        except Fail as e:
            _check(e.code == 3 and "armed" in str(e), f"armed-lane refusal: {e}")
        ok("quiesce-armed-lane")
        # restore: an unarmed free slot and a drained inbox
        os.remove(os.path.join(lane_dir, ".matrixkoder-watch.json"))
        con = mkdb.open_project(home, "fixture", data_dir=data)
        con.execute("UPDATE lane_slots SET lane_path=? WHERE slot='1'",
                    (os.path.join(data, "..", "lane1"),))
        con.commit()
        con.close()
        mkdb.append_event(home, "fixture", "r1", task="P1.1", chan="bus", kind="answer",
                          from_id="P1.1", to_id="orch", reply_to="m-1", text="a", data_dir=data)
        quiesce(data)
        ok("quiesce-drained")

        # 2. --dry-run writes nothing (hash the whole temp tree before and after).
        before = _tree_hash(tmp)
        rc, out, err = _cli(home, ["--dry-run", "--slug", "fixture", "--data", data])
        _check(rc == 0, f"dry-run rc={rc} {err[-400:]}")
        _check("copy " in out and "SQLite backup API" in out, f"dry-run printed no moves: {out!r}")
        _check(_tree_hash(tmp) == before, "dry-run wrote something")
        ok("dry-run-writes-nothing")

        # 3. a plain run copies + verifies, and the old data stays byte-identical.
        old_db = _hash_file(src_db(data))
        old_files = _tree_hash(data)
        rc, out, err = _cli(home, ["--slug", "fixture", "--data", data])
        _check(rc == 0, f"copy rc={rc} {err[-400:]}")
        dest_db = mkpaths.target_project_db("fixture", home)
        _check(os.path.isfile(dest_db), "the C2 store was not created")
        sc, sd = state_of(src_db(data)), state_of(dest_db)
        _check(sc == sd, f"copied store differs: {sc} != {sd}")
        _check(os.path.isfile(mkpaths.target_project_yaml("fixture", home)), "project.yaml not copied")
        _check(os.path.isfile(mkpaths.target_watch_cursors("fixture", home)), "watch-cursors not copied")
        _check(os.path.isfile(os.path.join(mkpaths.target_plan_dir("fixture", "live", home), "plan.md")),
               "the live plan.md was not copied")
        _check(not os.path.exists(mkpaths.target_plan_dir("fixture", "done", home)),
               "a non-live plan's files moved (C5: live plans only)")
        rt = mkpaths.target_task_dir("fixture", "live", "r1", "P1.1", home)
        _check(os.path.isfile(os.path.join(rt, "brief-r1.md")), "brief-r1.md not copied")
        _check(not os.path.exists(os.path.join(rt, "task.json")), "task.json should not move (C2)")
        _check(_hash_file(src_db(data)) == old_db, "the copy changed the old DB")
        _check(_tree_hash(data) == old_files, "the copy changed the old data dir")
        _check(os.path.isfile(mkpaths.target_plan_dir("fixture", "live", home) + "/archive/live-v1.md"),
               "the archived plan version was not copied")
        _check(os.path.isfile(mkpaths.target_plan_dir("fixture", "live", home) + "/assets/live-research.md"),
               "the design ref was not copied into assets/")
        ok("copy-verify-old-untouched")

        # 4. switch: db_path set; a second switch refuses.
        rc, out, err = _cli(home, ["--switch", "--slug", "fixture", "--data", data])
        _check(rc == 0, f"switch rc={rc} {err[-400:]}")
        _check(root_row(home, "fixture")["db_path"] == dest_db, "db_path was not set")
        rc, _, _ = _cli(home, ["--switch", "--slug", "fixture", "--data", data])
        _check(rc == 3, f"a second switch did not refuse (rc={rc})")
        ok("switch-sets-db-path")

        # 5. rollback with a write after the switch refuses; the write is NOT lost.
        mkdb.append_event(home, "fixture", "r1", task="P1.1", chan="bus", kind="info",
                          from_id="orch", to_id="P1.1", text="late write", data_dir=dest_dir_of(dest_db))
        rc, _, _ = _cli(home, ["--rollback", "--slug", "fixture", "--data", data])
        _check(rc == 3, f"rollback did not refuse after a write (rc={rc})")
        _check(state_of(dest_db)[0]["events"] >= 3, "the new DB lost the late write")
        ok("rollback-refuses-lost-write")

        # 6. a clean rollback carries the new DB back, clears db_path, and loses nothing.
        mkdb.append_event(home, "fixture", "r1", task="P1.1", chan="bus", kind="info",
                          from_id="orch", to_id="P1.1", text="carry me", data_dir=dest_dir_of(dest_db))
        # re-mark the switch point so the carry-back is allowed to proceed
        _write_marker(home, "fixture", {**_read_marker(home, "fixture"),
                                        "new_digest": state_of(dest_db)[1],
                                        "new_counts": state_of(dest_db)[0]})
        carried_counts = state_of(dest_db)[0]
        rc, out, err = _cli(home, ["--rollback", "--slug", "fixture", "--data", data])
        _check(rc == 0, f"clean rollback rc={rc} {err[-400:]}")
        _check(root_row(home, "fixture")["db_path"] is None, "rollback left db_path set")
        _check(state_of(src_db(data))[0]["events"] == carried_counts["events"],
               "rollback lost a write")
        ok("rollback-carries-back")
        # the old folder and the old DB still exist and are unchanged (nothing deletes them)
        _check(os.path.isdir(data) and os.path.isfile(src_db(data)), "rollback removed the old data")
        ok("rollback-keeps-old")

        # 7. --archive: tar under the MK home, never in the repo; the old folder stays unchanged.
        rc, _, _ = _cli(home, ["--switch", "--slug", "fixture", "--data", data])
        _check(rc == 0, "re-switch before archive failed")
        files_before = _tree_hash(data)
        rc, out, err = _cli(home, ["--archive", "--slug", "fixture", "--data", data])
        _check(rc == 0, f"archive rc={rc} {err[-400:]}")
        tar_path = _last_json(out)["tar"]
        _check(tar_path.startswith(mkpaths.target_project_dir("fixture", home)),
               f"the tar is not under the MK home: {tar_path}")
        _check(repo not in tar_path, f"the tar landed in the repo: {tar_path}")
        _check(os.path.isfile(tar_path), "no tar was written")
        _check(os.path.isdir(data) and os.path.isfile(src_db(data)), "--archive removed the old data")
        _check(_tree_hash(data) == files_before, "--archive changed the old folder")
        ok("archive-under-home-keeps-old")

        # 8. a write landing DURING the copy is caught; only the NEW copy is removed.
        shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
        clear_db_path(home, "fixture")
        pre_events = state_of(src_db(data))[0]["events"]
        inject = ("import importlib.util, os, sys; "
                  "spec = importlib.util.spec_from_file_location('mkdb_i', %r); "
                  "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                  "m.append_event(os.environ['MK_MIGRATE_HOME'], 'fixture', 'r1', task='P1.1', "
                  "chan='bus', kind='info', from_id='orch', to_id='P1.1', text='mid', data_dir=%r)"
                  % (os.path.join(HERE, "mkdb.py"), data))
        rc, out, err = _cli(home, ["--slug", "fixture", "--data", data],
                            env={"MK_MIGRATE_AFTER_COPY": f"{sys.executable} -c {json.dumps(inject)}",
                                 "MK_MIGRATE_HOME": home})
        _check(rc == 3, f"a write during the copy was not caught (rc={rc}) {out[-200:]}")
        _check(_last_json(out) and "verify failed" in _last_json(out).get("error", ""),
               f"wrong refusal: {out[-200:]}")
        _check(not os.path.exists(mkpaths.target_project_db("fixture", home)),
               "the failed verify left the NEW copy behind")
        # the old DB keeps every row: the injection landed and nothing was rewound or removed.
        _check(state_of(src_db(data))[0]["events"] == pre_events + 1,
               "the failed verify disturbed the old DB")
        ok("write-during-copy-caught")

        # 9. the switch lock: a held lock refuses a second migrator.
        import fcntl as _fcntl
        lp = switch_lock_path(home)
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        lf = open(lp, "a")
        _fcntl.flock(lf, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        try:
            rc, _, _ = _cli(home, ["--switch", "--slug", "fixture", "--data", data])
            _check(rc == 3, f"a second migrator was not refused (rc={rc})")
        finally:
            _fcntl.flock(lf, _fcntl.LOCK_UN)
            lf.close()
        ok("switch-lock-refuses-second")

        # 9b. counts re-checked under the switch lock: a mismatch refuses the switch and db_path
        # stays NULL. (The mutation is the same mid-copy seam, now through --switch.)
        shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
        clear_db_path(home, "fixture")
        inject = ("import importlib.util, os; "
                  "s = importlib.util.spec_from_file_location('m', os.path.join(%r, 'mkdb.py')); "
                  "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                  "m.append_event(os.environ['MK_MIGRATE_HOME'], 'fixture', 'r1', task='P1.1',"
                  " chan='bus', kind='info', from_id='orch', to_id='P1.1', text='mid-switch', data_dir=%r)"
                  % (HERE, data))
        rc, out, err = _cli(home, ["--switch", "--slug", "fixture", "--data", data],
                            env={"MK_MIGRATE_AFTER_COPY": f"{sys.executable} -c {json.dumps(inject)}",
                                 "MK_MIGRATE_HOME": home})
        _check(rc == 3, f"a switch with a mismatch was not refused (rc={rc})")
        _check(root_row(home, "fixture")["db_path"] is None,
               "the refused switch set projects.db_path anyway")
        _check(not os.path.exists(mkpaths.target_project_db("fixture", home)),
               "the refused switch left the NEW copy behind")
        ok("switch-refuses-mismatch")

        # 10. mutation proofs (R13): each guard is shown load-bearing. A deliberately broken copy
        # of this file (recall disabled / quiesce disabled / old-folder delete enabled) is run and
        # shown to change the outcome — so the guard test is proven to catch the bad version.
        _mutation_recount(home, data, tmp)
        ok("mutation-recount")
        _mutation_quiesce(home, data, tmp)
        ok("mutation-quiesce")
        _mutation_delete(home, data, tmp)
        ok("mutation-delete-old")

    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"pass mk-migrate.py: {len(checks)} checks")
    print(json.dumps({"ok": True, "tests": len(checks), "passed": len(checks)}))
    return 0


def dest_dir_of(db_path):
    return os.path.dirname(db_path)


def _mutant(pairs, sandbox):
    """Write a deliberately broken copy of this file into a sandbox and return its path. The three
    dependency modules are copied beside it, so the mutant resolves them from its own HERE."""
    os.makedirs(sandbox, exist_ok=True)
    for dep in ("mkpaths.py", "mkdb.py", "mk-bus.py"):
        shutil.copy2(os.path.join(HERE, dep), os.path.join(sandbox, dep))
    with open(os.path.abspath(__file__)) as f:
        src = f.read()
    for old, new in pairs:
        _check(old in src, f"mutation anchor not found: {old[:60]!r}")
        src = src.replace(old, new, 1)
    path = os.path.join(sandbox, "mk-migrate-mutant.py")
    with open(path, "w") as f:
        f.write(src)
    return path


def _run_mutant(path, home, args, env=None):
    e = dict(os.environ)
    e.update({"MK_HOME": home, "MATRIXKODER_HOME": home, "MK_SELFTEST": "1"})
    if env:
        e.update(env)
    proc = subprocess.run([sys.executable, path, *args], capture_output=True, text=True, env=e)
    return proc.returncode, proc.stdout, proc.stderr


def _mutation_recount(home, data, tmp):
    """(1) skip the recount ('if False'): with the real file a write during the copy is caught and
    the NEW copy removed; with the broken file the copy lands anyway. The difference IS the proof."""
    sandbox = os.path.join(tmp, "mut-recount")
    mutant = _mutant([
        ('if dst_counts != src_counts or dst_digest != src_digest:\n            bad.append("mk.db")',
         'if False:\n            bad.append("mk.db")'),
        ('if now_counts != src_counts or now_digest != src_digest:\n        bad.append("mk.db (the source changed during the copy)")',
         'if False:\n        bad.append("mk.db (the source changed during the copy)")'),
    ], sandbox)
    shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
    clear_db_path(home, "fixture")
    inject = ("import importlib.util, os; "
              "s = importlib.util.spec_from_file_location('m', os.path.join(os.path.dirname(%r), 'mkdb.py')); "
              "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); "
              "m.append_event(os.environ['MK_MIGRATE_HOME'], 'fixture', 'r1', task='P1.1',"
              " chan='bus', kind='info', from_id='orch', to_id='P1.1', text='mid', data_dir=%r)"
              % (mutant, data))
    env = {"MK_MIGRATE_AFTER_COPY": f"{sys.executable} -c {json.dumps(inject)}",
           "MK_MIGRATE_HOME": home}
    # the real file refuses (rc=3, the NEW copy removed); the broken recount leaves the copy.
    rc_real, _, _ = _run_mutant(os.path.abspath(__file__), home,
                                ["--slug", "fixture", "--data", data], env=env)
    _check(rc_real == 3, f"mutation-recount: the real file did not refuse (rc={rc_real})")
    _check(not os.path.exists(mkpaths.target_project_db("fixture", home)),
           "mutation-recount: the real file left the bad copy behind")
    shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
    rc_broken, out_broken, _ = _run_mutant(mutant, home, ["--slug", "fixture", "--data", data], env=env)
    _check(rc_broken == 0, f"mutation-recount: the broken version still refused (rc={rc_broken})")
    _check(os.path.exists(mkpaths.target_project_db("fixture", home)),
           "mutation-recount: the broken version did not leave the bad copy — the guard is not the"
           " thing being tested")
    shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)


def _mutation_quiesce(home, data, tmp):
    """(2) skip the quiesce ('pass'): with the real file a busy lane refuses the copy; with the
    broken file the copy runs anyway."""
    sandbox = os.path.join(tmp, "mut-quiesce")
    mutant = _mutant([
        ("    with switch_lock(home):\n        quiesce(data)\n        out = do_copy_verify(home, data, slug)",
         "    with switch_lock(home):\n        pass  # quiesce removed\n        out = do_copy_verify(home, data, slug)"),
    ], sandbox)
    shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
    clear_db_path(home, "fixture")
    con = mkdb.open_project(home, "fixture", data_dir=data)
    con.execute("UPDATE lane_slots SET state='busy' WHERE slot='1'")
    con.commit()
    con.close()
    try:
        rc_real, _, _ = _run_mutant(os.path.abspath(__file__), home, ["--switch", "--slug", "fixture", "--data", data])
        _check(rc_real == 3, f"mutation-quiesce: the real file did not refuse (rc={rc_real})")
        rc_broken, _, _ = _run_mutant(mutant, home, ["--switch", "--slug", "fixture", "--data", data])
        _check(rc_broken == 0, f"mutation-quiesce: the broken version did not run (rc={rc_broken})")
        _check(os.path.exists(mkpaths.target_project_db("fixture", home)),
               "mutation-quiesce: the broken version copied nothing")
    finally:
        con = mkdb.open_project(home, "fixture", data_dir=data)
        con.execute("UPDATE lane_slots SET state='free' WHERE slot='1'")
        con.commit()
        con.close()
        shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
        clear_db_path(home, "fixture")


def _mutation_delete(home, data, tmp):
    """(3) delete the old folder: a broken copy removes the old data dir after the copy. Our
    invariant (the old folder and DB survive every command) is what catches it."""
    sandbox = os.path.join(tmp, "mut-delete")
    mutant = _mutant([
        ("    with switch_lock(home):\n        quiesce(data)\n        return do_copy_verify(home, data, slug)",
         "    with switch_lock(home):\n        quiesce(data)\n        out = do_copy_verify(home, data, slug)\n"
         "        shutil.rmtree(data, ignore_errors=True)  # the mutation: delete the old folder\n"
         "        return out"),
    ], sandbox)
    shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
    clear_db_path(home, "fixture")
    fresh = os.path.join(tmp, "del-data")
    _seed(os.path.join(tmp, "del-home"), fresh, repo=os.path.join(tmp, "del-repo"))
    rc, _, err = _run_mutant(mutant, os.path.join(tmp, "del-home"), ["--slug", "fixture", "--data", fresh])
    _check(rc == 0, f"mutation-delete: the broken version failed (rc={rc}) {err[-200:]}")
    _check(not os.path.isdir(fresh),
           "mutation-delete: the broken version did NOT delete the old folder — the check is not"
           " load-bearing")
    # and the real file leaves it: the invariant the mutation violates.
    _check(os.path.isdir(data) and os.path.isfile(src_db(data)),
           "mutation-delete: the old folder was removed by the real run")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Fail as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(e.code)
