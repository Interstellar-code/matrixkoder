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
import sqlite3
import stat
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


def switch_slugs(home, slug, data):
    """Every root slug the switch must move: the rows whose data_dir realpath IS this data
    dir, plus the --slug row (todo 117).

    One data dir can have TWO root rows — the canonical `--slug` row and the derived alias
    (`p`+sha256 of the data dir) mk-bus/mk-state register on first use, which every lane
    token and the bus resolve through. Switching only the slug row used to leave the alias
    row's db_path NULL, so the bus kept writing the OLD store. Rows that record ANOTHER
    data dir are never returned. A read never creates the root store."""
    real = os.path.realpath(os.path.abspath(data))
    out = []

    def add(one):
        if one and one not in out:
            out.append(one)

    for row in mkdb.projects(home):
        recorded = row.get("data_dir")
        if recorded and os.path.realpath(os.path.expanduser(recorded)) == real:
            add(row.get("slug"))
    add(slug)
    return out


def _slug_list(slugs):
    """One slug or many, deduped in order (the self-test passes a single slug)."""
    if isinstance(slugs, str):
        return [slugs] if slugs else []
    return list(dict.fromkeys(s for s in slugs if s))


def set_db_path(home, slugs, db_path):
    """--switch: set projects.db_path on EVERY row of the data dir through mkdb.open_root
    (the C3 column; the L1 migration comment names mk-migrate as its writer). No other root
    column is touched. One transaction, so a row that vanished (rowcount 0) leaves no row
    half-switched — the whole update is rolled back and the caller sees Fail."""
    slugs = _slug_list(slugs)
    if not slugs:
        raise Fail("no projects row to switch")
    con = mkdb.open_root(home)
    try:
        for one in slugs:
            cur = con.execute("UPDATE projects SET db_path=?, last_activity=? WHERE slug=?",
                              (db_path, now_iso(), one))
            if cur.rowcount != 1:
                raise Fail(f"no projects row for slug {one!r} to switch")
        con.commit()
    finally:
        con.close()


def clear_db_path(home, slugs):
    """--rollback: clear db_path on the same set of rows the switch had set."""
    slugs = _slug_list(slugs)
    if not slugs:
        raise Fail("no projects row to roll back")
    con = mkdb.open_root(home)
    try:
        for one in slugs:
            con.execute("UPDATE projects SET db_path=NULL, last_activity=? WHERE slug=?",
                        (now_iso(), one))
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


def _registry_rows(data):
    """The lane registry as rows (slot, lane_path, state, run, task), or None when the registry is
    genuinely absent (no store, no `lane_slots` table, no lanes.json).

    Review R1 HIGH 2: a registry that exists but cannot be read is a C5 refusal, never a quiet
    "no lanes". Every read failure — a corrupt/non-SQLite store, a store at the wrong schema
    version, malformed lanes.json — becomes `Fail(..., 3)`; only true absence stays quiet."""
    db = src_db(data)
    if db and os.path.isfile(db):
        try:
            con = mkdb._open_read(db, mkdb.SCHEMA)
        except sqlite3.Error as e:
            raise Fail(f"lane registry unreadable (C5 refusal): {db}: {e}", 3)
        except Exception as e:  # mkdb.Fail: newer or unmigrated schema
            raise Fail(f"lane registry unreadable (C5 refusal): {db}: {e}", 3)
        if con is None:
            raise Fail(f"lane registry unreadable (C5 refusal): cannot open {db}", 3)
        try:
            if not _has_table(con, "lane_slots"):
                return None
            return [dict(r) for r in con.execute(
                "SELECT slot, lane_path, state, run, task FROM lane_slots ORDER BY slot")]
        except Exception as e:
            raise Fail(f"lane registry unreadable (C5 refusal): {db}: {e}", 3)
        finally:
            con.close()
    path = mkpaths.lanes_file(data)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            lanes = json.load(f)
    except (OSError, ValueError) as e:
        raise Fail(f"lane registry unreadable (C5 refusal): {path}: {e}", 3)
    if not isinstance(lanes, dict):
        raise Fail(f"lane registry unreadable (C5 refusal): {path}: not a JSON object", 3)
    slots = lanes.get("slots")
    if slots is None:
        slots = {}
    # Review R2 LOW: a syntactically valid lanes.json whose `slots` is a list, string or number
    # would raise AttributeError on `.items()` — a traceback, not the C5 refusal. Anything but an
    # object (or an absent/null slots) is unreadable.
    if not isinstance(slots, dict):
        raise Fail(f"lane registry unreadable (C5 refusal): {path}: slots is not an object", 3)
    out = []
    for key, rec in slots.items():
        if not isinstance(rec, dict):
            continue
        out.append({"slot": key, "lane_path": rec.get("path"), "state": rec.get("state"),
                    "run": rec.get("run"), "task": rec.get("task")})
    return out


def busy_lanes(data):
    """The lane registry's held slots for this data dir. Store mode reads `lane_slots` (rows are
    truth); file mode reads lanes.json. Read-only: never republishes the view. An unreadable
    registry refuses through `_registry_rows`."""
    rows = _registry_rows(data) or []
    return [{"slot": r["slot"], "lane_path": r.get("lane_path"), "state": "busy",
             "run": r.get("run"), "task": r.get("task")}
            for r in rows if r.get("state") == "busy"]


def armed_lane_paths(data):
    """Slot worktrees holding `.matrixkoder-watch.json` — the watch file is what makes a lane
    armed (mk-lane-event's rule). Read-only; the registry supplies the paths."""
    rows = _registry_rows(data) or []
    paths = [r.get("lane_path") for r in rows if r.get("lane_path")]
    return [p for p in paths if os.path.isfile(os.path.join(p, ".matrixkoder-watch.json"))]


def pending_asks(data):
    """Unanswered asks in the project store, through mk-bus's own reader (never a bus file).
    This is the `/pending` rule: an ask with no answer naming it. An unreadable inbox is a C5
    refusal (review R1 HIGH 2), never a traceback and never "no asks"."""
    try:
        store = mkbus.StoreAdapter(data)
        asks = store.store_select(
            "SELECT id, run, t FROM events WHERE chan='bus' AND kind='ask' ORDER BY id")
        answers = store.store_select(
            "SELECT reply_to FROM events WHERE chan='bus' AND kind='answer'"
            " AND reply_to IS NOT NULL")
    except Exception as e:
        raise Fail(f"bus inbox unreadable: {e}", 3)
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

def _symlink_ref(real, linkname):
    """The canonical reference to a symlink's target, or None when the target is outside the
    given realpath base (or the link is unreadable). Used to compare a source symlink against the
    copied one WITHOUT following either: both sides are realpath-resolved, so an in-repo link
    compares equal and a link that escapes is named instead of silently dereferenced."""
    try:
        base = os.path.realpath(real)
        tgt = os.path.realpath(os.path.join(base, os.readlink(linkname)))
    except OSError:
        return None
    if tgt == base or tgt.startswith(base + os.sep):
        return tgt
    return None


def _hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_path(p):
    """The hash of one file, or of a symlink (its own target string, never followed), or of a
    special entry (a type tag). `os.walk(followlinks=False)` lists a symlink under `files` even
    when its target is a directory; hashing it with `_hash_file` would follow it, so this branches
    on `islink` first. A broken symlink and a fifo hash too, so a tree with one still verifies."""
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        return "link:" + os.readlink(p)
    if stat.S_ISREG(st.st_mode):
        return _hash_file(p)
    return "type:%o" % stat.S_IFMT(st.st_mode)


def _tree_hash(root):
    """sha256 over (relative path, entry hash) of every non-directory entry under root. Symlinks
    are hashed as symlinks (never followed), so a symlink that became a regular file — or vice
    versa — changes the digest. `os.walk(followlinks=False)` never descends into a directory
    symlink; the link itself is hashed, so a tree containing one verifies."""
    h = hashlib.sha256()
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            p = os.path.join(dirpath, name)
            h.update(os.path.relpath(p, root).encode())
            h.update(_hash_path(p).encode())
        for name in sorted(dirs):
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                h.update(os.path.relpath(p, root).encode())
                h.update(_hash_path(p).encode())
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


def _design_refs(con, plan_id):
    """The plan's design/asset references (paths under the checkout). The `design` COLUMN holds
    them from D1.1 on; a store without the column keeps them in `header_json` (C3: the header was
    their only home). Both shapes are read; the column wins when it carries a value."""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(plans)")}
    if "design" in cols:
        row = con.execute("SELECT design FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row and row["design"]:
            try:
                got = json.loads(row["design"])
            except ValueError:
                got = None
            if isinstance(got, list):
                return [x for x in got if isinstance(x, str) and x]
    return [x for x in (_header(con, plan_id).get("design") or []) if isinstance(x, str) and x]


def live_plans(con):
    """Plan ids with an active run (C5: only live plans move their files)."""
    if not _has_table(con, "plans"):
        return []
    return [r["id"] for r in con.execute(
        "SELECT DISTINCT p.id FROM plans p JOIN runs r ON r.plan=p.id"
        " WHERE r.status IN ('running','active') ORDER BY p.id")]


def _safe_design_ref(repo, rel):
    """A design reference resolved strictly inside `repo`, or None (review R1 HIGH 1).

    `os.path.join` discards the base for an absolute `rel`, and `..` escapes it: either would
    copy a file from outside the repo into the C2 store. Both sides go through `realpath`, so a
    symlink cannot escape either (a symlinked repo prefix is fine). Only a path strictly inside
    the repo is accepted; the caller lists the skipped ones on `--dry-run`."""
    if not isinstance(rel, str) or not rel or os.path.isabs(rel):
        return None
    base = os.path.realpath(repo)
    src = os.path.realpath(os.path.join(base, rel))
    if src != base and not src.startswith(base + os.sep):
        return None
    return src


def _newest_archive(archive_dir, pid):
    """The newest archived version `<archive_dir>/<pid>-v<n>.md`, or None. The version is read as
    an integer, so v10 beats v9."""
    if not os.path.isdir(archive_dir):
        return None
    best = None
    for name in sorted(os.listdir(archive_dir)):
        m = re.fullmatch(re.escape(pid) + r"-v(\d+)\.md", name)
        if m and (best is None or int(m.group(1)) > best[0]):
            best = (int(m.group(1)), os.path.join(archive_dir, name))
    return best[1] if best else None


def _approved_plan_file(data, con, pid):
    """The plan's approved bytes (C5), in the order the contract names them: the file the row's
    `archived_path` records (the newest version approve wrote), the newest archived version beside
    the plan, then the plan file the current layout keeps (`<data>/plans/<id>.md`). None when the
    plan has no markdown anywhere — the plan folder then still gets its empty `assets/`."""
    row = con.execute("SELECT archived_path FROM plans WHERE id=?", (pid,)).fetchone()
    for src in (row["archived_path"] if row else None,
                _newest_archive(mkpaths.plan_archives_dir(data), pid),
                mkpaths.plan_file(data, pid)):
        if src and os.path.isfile(src):
            return src
    return None


def _asset_sources(data, pid):
    """Every pre-switch folder holding this plan's assets (C2): the plan's own dir
    (`<data>/plans/<id>/` today) and the sibling `<id>-assets/` or `assets/` dir beside the plan
    file. A missing one is simply absent. All of them land in the one `assets/` folder."""
    plans = mkpaths.plans_dir(data)
    out = []
    for path in (mkpaths.plan_dir(data, pid), os.path.join(plans, f"{pid}-assets"),
                 os.path.join(plans, "assets")):
        if path not in out and os.path.isdir(path):
            out.append(path)
    return out


def plan_moves(data, home, slug, con, pid, skips=None):
    """Every file of ONE live plan: plan.md, assets (the plan's own dir, a sibling assets dir and
    its design refs), its archived versions, and the round files of every task of every run of the
    plan. The plan folder always gets a `plan.md` and an `assets/`, even when the plan was approved
    before the switch and left its file in the checkout — that is what R02 audits (C2). A design
    ref that escapes the repo is skipped and its label appended to `skips`."""
    out = []
    tdir = mkpaths.target_plan_dir(slug, pid, home)
    pf = _approved_plan_file(data, con, pid)
    if pf:
        out.append({"label": f"plans/{pid}/plan.md", "kind": "file", "src": pf,
                    "dst": os.path.join(tdir, "plan.md")})
    adir = os.path.join(tdir, "assets")
    for src in _asset_sources(data, pid):
        out.append({"label": f"plans/{pid}/assets/ [{os.path.basename(src)}]", "kind": "tree",
                    "src": src, "dst": adir})
    # C2: the plan folder carries plan.md, assets/ and archive/ even when the plan has no asset
    # file and no archived version of its own (an empty folder, never a missing one — that is what
    # the e2e audit's R02 checks). `mkdir` moves are created, never hashed.
    for name in ("assets", "archive"):
        out.append({"label": f"plans/{pid}/{name}/", "kind": "mkdir",
                    "dst": os.path.join(tdir, name)})
    repo = os.path.dirname(os.path.abspath(data))
    for rel in _design_refs(con, pid):
        src = _safe_design_ref(repo, rel)
        if src is None:
            if skips is not None:
                skips.append(f"plans/{pid}/assets/{rel!r} (outside the repo)")
            continue
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


def build_moves(data, home, slug, con, skips=None):
    """Every non-DB move: project.yaml, watch-cursors.json, the lanes registry file (only when
    the store carries no rows) and every live plan's files. The DB itself moves via copy_db.
    Design refs that escape the repo are appended to `skips` (reported by --dry-run)."""
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
        moves += plan_moves(data, home, slug, con, pid, skips=skips)
    return moves


def copy_moves(moves, repo):
    """Copy every move; return [(move, sha)] so the verify can re-hash both sides. A symlink is
    copied AS a symlink (`symlinks=True`), never dereferenced: a link whose target leaves the repo
    must not pull that target's bytes into the C2 store. A link that escapes is the caller's
    `Fail`, raised before the copy, so no partial tree is written. `repo` is the same base
    `_safe_design_ref` resolves a design ref against (the project's checkout)."""
    done = []
    for m in moves:
        if m["kind"] == "mkdir":
            os.makedirs(m["dst"], exist_ok=True)
            done.append((m, None))
        elif m["kind"] == "file":
            os.makedirs(os.path.dirname(m["dst"]), exist_ok=True)
            shutil.copy2(m["src"], m["dst"])
            done.append((m, _hash_file(m["src"])))
        else:
            for dirpath, dirs, files in os.walk(m["src"]):
                dirs.sort()
                for name in sorted(files) + sorted(dirs):
                    link = os.path.join(dirpath, name)
                    if not os.path.islink(link):
                        continue
                    if _symlink_ref(repo, link) is None:
                        raise Fail(f"assets symlink escapes the repo: {link} -> "
                                   f"{os.readlink(link)}; remove it or point it inside the repo",
                                   3)
            shutil.copytree(m["src"], m["dst"], dirs_exist_ok=True, symlinks=True)
            done.append((m, None))
    return done


def verify_moves(done):
    """Labels whose bytes moved on either side since the copy (a write landed during the copy).
    A `mkdir` move writes no bytes: it is checked as "the folder exists", never re-hashed."""
    bad = []
    for m, want in done:
        if m["kind"] == "mkdir":
            if not os.path.isdir(m["dst"]):
                bad.append(m["label"])
        elif m["kind"] == "file":
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


def _after_verify_hook():
    """Test seam (never set in normal use): run a command after do_copy_verify returns and before
    cmd_switch re-reads both stores under the lock, so a write landing in that window can be
    injected (review R1 HIGH 3)."""
    cmd = os.environ.get("MK_MIGRATE_AFTER_VERIFY")
    if cmd:
        subprocess.run(cmd, shell=True, check=False)


def _after_tar_hook():
    """Test seam (never set in normal use): run a command after the archive tarball is written and
    before the completeness recount, so a file the tar missed can be injected (M1.2b)."""
    cmd = os.environ.get("MK_MIGRATE_AFTER_TAR")
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


def existing_copy_problem(home, data, slug, moves, src_counts, src_digest, dest):
    """Non-empty label list when an EXISTING C2 copy does not match the source, else []. Shared by
    the real path and `--dry-run`, so a dry run predicts the same refusal the copy would give
    (M1.1a's rule, extended to the existing-copy case). Never follows a symlink: a source file
    entry hashes with `_hash_path`, which returns the link's target string for a link."""
    done = [(m, _hash_path(m["src"]) if m["kind"] == "file" else None) for m in moves]
    bad = verify_moves(done)
    dst_counts, dst_digest = state_of(dest)
    if dst_counts != src_counts or dst_digest != src_digest:
        bad.append("mk.db")
    return bad


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
    skips = []
    try:
        moves = build_moves(data, home, slug, con, skips=skips)
        src_counts, src_digest = db_state(con)
    finally:
        con.close()
    existing = os.path.exists(dest)

    if dry_run:
        # M1.1a rule, extended: a dry run reports what the real command would do. If an existing
        # C2 copy can never verify, the real copy would refuse — so refuse here too, before
        # printing any plan, instead of advertising a copy that cannot converge.
        if existing:
            bad = existing_copy_problem(home, data, slug, moves, src_counts, src_digest, dest)
            if bad:
                raise Fail("an existing C2 copy does not match the source: "
                           + "; ".join(sorted(set(bad))) + f" — inspect or remove {dest} and retry",
                           3)
        for m in moves:
            if m["kind"] == "mkdir":
                print(f"{'verify' if existing else 'mkdir'} {m['dst']}")
                continue
            print(f"{'verify' if existing else 'copy'} {m['src']} -> {m['dst']}")
        for label in skips:
            print(f"skip {label}: outside the repo")
        print(f"{'verify' if existing else 'copy'} {source} -> {dest}"
              f" (SQLite backup API, {sum(src_counts.values())} rows)")
        return {"ok": True, "command": "copy", "dry_run": True, "slug": slug, "data": data,
                "target": dest, "moves": len(moves) + 1, "skips": skips, "db_counts": src_counts}

    if existing:
        # a prior copy is present: verify it, never overwrite it silently.
        bad = existing_copy_problem(home, data, slug, moves, src_counts, src_digest, dest)
        if bad:
            raise Fail("an existing C2 copy does not match the source: "
                       + "; ".join(sorted(set(bad))) + f" — inspect or remove {dest} and retry", 3)
        return {"ok": True, "command": "copy", "reused": True, "slug": slug, "data": data,
                "target": dest, "moves": len(moves) + 1, "db_counts": src_counts,
                "db_digest": src_digest}

    copy_db(source, dest)
    # `plan_moves` resolves design refs against dirname(data) (its own rule); the assets-symlink
    # guard uses the same base, so an in-repo link is kept and a link out of the checkout refuses.
    repo = os.path.dirname(os.path.abspath(data))
    # A partial copy must never be left behind: ANY failure between here and a passing verify —
    # an unreadable file, a fifo, a symlink that escapes the repo, a copy_moves raise — removes the
    # NEW copy and becomes one JSON `Fail`, so no later retry sees a half-written C2 store and no
    # future `--dry-run` advertises a copy that cannot verify. The old data is untouched.
    try:
        done = copy_moves(moves, repo)
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
            raise Fail("verify failed: " + "; ".join(sorted(set(bad)))
                       + " — the NEW copy was removed, the old data is untouched", 3)
    except BaseException as e:
        # one place removes the partial copy for every failure kind; Fail (the verify's own
        # refusal) and a user interrupt pass through unchanged, anything else becomes a clean
        # one-line refusal. Never re-raise a bare traceback from a half-written store.
        _remove_new_copy(home, slug)
        if isinstance(e, (Fail, KeyboardInterrupt, SystemExit)):
            raise
        raise Fail(f"copy failed: {type(e).__name__}: {e} — the NEW copy was removed, the old data"
                   " is untouched", 3)
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
    # Review R2 MEDIUM: the already-migrated refusal runs BEFORE the --dry-run branch, so a dry
    # run on a switched project exits 3 exactly like the real command. This check only reads
    # (`root_row`, never `ensure_root_row`): a dry run still writes nothing.
    row = root_row(home, slug)
    if row and row.get("db_path"):
        raise Fail(f"already migrated: projects.db_path = {row['db_path']}; use --rollback", 3)
    if args.dry_run:
        quiesce(data)
        return do_copy_verify(home, data, slug, dry_run=True)
    ensure_root_row(home, slug, data, repo)
    with switch_lock(home):
        quiesce(data)
        return do_copy_verify(home, data, slug)


def cmd_switch(args):
    home, data, slug, repo = _prepare(args)
    # Review R2 MEDIUM: the same refusal order as cmd_copy — before the --dry-run branch, and a
    # dry run must not register a root row (`ensure_root_row` writes).
    row = root_row(home, slug)
    if row and row.get("db_path"):
        raise Fail(f"already migrated: projects.db_path = {row['db_path']}", 3)
    if args.dry_run:
        quiesce(data)
        out = do_copy_verify(home, data, slug, dry_run=True)
        dest = mkpaths.target_project_db(slug, home)
        for one in switch_slugs(home, slug, data):
            print(f"switch {one}: set projects.db_path = {dest}")
        out["command"] = "switch"
        return out
    ensure_root_row(home, slug, data, repo)
    slugs = switch_slugs(home, slug, data)
    with switch_lock(home):
        quiesce(data)
        out = do_copy_verify(home, data, slug)
        _after_verify_hook()
        dest = mkpaths.target_project_db(slug, home)
        before = src_db(data)
        old_state = state_of(before)
        new_state = state_of(dest)
        # Review R1 HIGH 3: re-assert equality under the lock, right before the switch. A write
        # that landed on the source after the copy leaves the two stores different; switching
        # then would record mismatched digests. No switch has happened yet, so the NEW copy is
        # removed and projects.db_path stays NULL.
        if old_state is None or new_state is None or old_state != new_state:
            _remove_new_copy(home, slug)
            raise Fail("verify under switch lock failed: source and target stores differ", 3)
        set_db_path(home, slugs, dest)
        marker = {"slug": slug, "data_dir": os.path.realpath(data), "db_path": dest,
                  "slugs": slugs,
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
    # Todo 117: the marker names every slug the switch moved. A marker written before that
    # (scalar `slug` only) falls back to the data-dir match, which resolves the same rows.
    recorded = marker.get("slugs")
    slugs = [s for s in recorded if isinstance(s, str)] if isinstance(recorded, list) else []
    if not slugs:
        slugs = switch_slugs(home, slug, data)
    if args.dry_run:
        print(f"rollback {slug}: carry {row['db_path']} back to {src_db(data)}, clear db_path")
        for one in slugs:
            print(f"rollback {one}: clear projects.db_path")
        return {"ok": True, "command": "rollback", "dry_run": True, "slug": slug,
                "from": row["db_path"], "to": src_db(data)}
    dest = mkpaths.target_project_db(slug, home)
    # Review R1 LOW: the old/new state reads and the refusal checks run INSIDE the switch lock.
    # Reading them before the lock left a window where a concurrent migrator (or a writer) could
    # change either store between the check and the carry-back.
    with switch_lock(home):
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
        copy_db(dest, src_db(data))
        back = state_of(src_db(data))
        if back is None or back != new_state:
            raise Fail("rollback verify failed: the carried-back DB does not match the new DB", 3)
        clear_db_path(home, slugs)
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
    # Review R1 MEDIUM: on ANY failure the partial tarball is removed; the old folder and DB are
    # never touched. tarfile stores a symlink as a symlink (dereference is False by default), so
    # it never follows one out of the folder and never reads its target's bytes.
    try:
        with tarfile.open(out, "w:gz") as tf:
            tf.add(data, arcname=os.path.basename(data.rstrip(os.sep)))
        _after_tar_hook()
        members = count_tar_files(out)
        on_disk = count_disk_entries(data)
        if members != on_disk:
            raise Fail(f"archive incomplete: {members} files in {out}, {on_disk} on disk", 3)
        # the old folder and the old DB are never deleted here.
        if not os.path.isdir(data) or not os.path.isfile(src_db(data)):
            raise Fail("the old data folder or DB vanished during --archive", 3)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(out)
        raise
    return {"ok": True, "command": "archive", "slug": slug, "tar": out, "files": members}


def count_tar_files(path):
    """Every non-directory member in a tar, with the handle closed.

    Review R1 MEDIUM: `isfile()` is False for a symlink (and for a fifo), yet `os.walk` lists a
    symlink among the files it descends. Counting only regular files made ANY symlink in the data
    dir fail the completeness check. Directories are traversed, never counted; everything else
    (regular file, symlink, fifo) counts — matching `count_disk_entries`."""
    with tarfile.open(path) as tf:
        return sum(1 for m in tf if not m.isdir())


def count_disk_entries(root):
    """Every non-directory entry under root, the disk side of `count_tar_files`.

    `os.walk(followlinks=False)` lists a symlink to a file among `files` and a symlink to a
    directory among `dirs` (and never descends into it); a fifo among `files`. Directories are not
    counted; every symlink is, whatever its target, so a symlink that points inside, outside or at
    a directory still balances against its tar member."""
    n = 0
    for dirpath, dirs, files in os.walk(root):
        n += len(files)
        n += sum(1 for name in dirs if os.path.islink(os.path.join(dirpath, name)))
    return n


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


Z15_PLAN = """+++
plan = "z15plan"
status = "draft"
created = 2026-10-15
runs = []
design = ["z15-research.md"]
integration = "main"

[gates]
test = "true"

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

# z15plan — the live plan of the Z15 case
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
    con = mkdb.open_project(home, "fixture", data_dir=data)
    try:
        mkdb.add_task(con, "r1", "P1.1", "task", title="one")
    finally:
        con.close()
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


def _tool(script, home, *args, env=None):
    """One SIBLING production script (mk-plan.py, mk-state.py), one JSON line — the same env
    `_cli` pins, so a temp home and a temp data dir travel in the child's own env."""
    e = dict(os.environ)
    e.update({"MK_HOME": home, "MATRIXKODER_HOME": home, "MK_SELFTEST": "1"})
    if env:
        e.update(env)
    proc = subprocess.run([sys.executable, os.path.join(HERE, script), *[str(a) for a in args]],
                          capture_output=True, text=True, env=e)
    return proc.returncode, proc.stdout, proc.stderr


def _last_json(text):
    for line in reversed([ln for ln in text.splitlines() if ln.strip()]):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def alias_slug(data):
    """The derived root alias mk-bus registers for a data dir: `p` + sha256[0:16] of its
    realpath (mk-bus.py:356-359). Every lane token and the bus resolve the project through
    that row, so a switch that skips it leaves the bus on the OLD store (Z7, todo 117)."""
    return "p" + hashlib.sha256(os.path.realpath(data).encode()).hexdigest()[:16]


def _root_rows_set(home, rows):
    """Insert root rows through the same open_root path _seed uses (no fixture-only writes)."""
    con = mkdb.open_root(home)
    try:
        for slug, data_dir in rows:
            con.execute(
                "INSERT OR IGNORE INTO projects(slug, name, repo, data_dir, mode, created,"
                " last_activity) VALUES (?,?,?,?,?,?,?)",
                (slug, slug, os.path.dirname(os.path.realpath(data_dir)),
                 os.path.realpath(data_dir), "home", now_iso(), now_iso()))
        con.commit()
    finally:
        con.close()


def _alias_root_row(home, data, repo):
    """A SECOND root row for the same data dir, exactly as mk-bus registers its alias."""
    _root_rows_set(home, [(alias_slug(data), data)])


def _other_root_row(home, data, repo):
    """A root row for a DIFFERENT data dir: the set the switch must never touch."""
    _root_rows_set(home, [("otherproj", data)])


def self_test():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="mk-migrate-test-"))
    saved = {k: os.environ.get(k) for k in
             ("MK_HOME", "MATRIXKODER_HOME", "MK_SELFTEST", "MK_MIGRATE_AFTER_COPY",
              "MK_MIGRATE_AFTER_VERIFY", "MK_MIGRATE_AFTER_TAR")}
    home = os.path.join(tmp, "home")
    repo = os.path.join(tmp, "repo")
    data = os.path.join(repo, ".matrixkoder")
    os.environ.update({"MK_HOME": home, "MATRIXKODER_HOME": home, "MK_SELFTEST": "1"})
    os.environ.pop("MK_MIGRATE_AFTER_COPY", None)
    os.environ.pop("MK_MIGRATE_AFTER_VERIFY", None)
    os.environ.pop("MK_MIGRATE_AFTER_TAR", None)
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

        # 10. review R1 HIGH 2: an unreadable lane registry or inbox REFUSES quiesce (never a
        # quiet "no lanes", never a traceback). Each failure mode gets its own temp data dir.
        # (a) corrupt lanes.json (file mode: no mk.db)
        d = os.path.join(tmp, "badlanes")
        os.makedirs(d, exist_ok=True)
        with open(mkpaths.lanes_file(d), "w") as f:
            f.write('{"corrupt')
        try:
            _registry_rows(d)
            raise SystemExit("SELF-TEST FAIL: a corrupt lanes.json did not refuse")
        except Fail as e:
            _check(e.code == 3 and "unreadable" in str(e), f"corrupt lanes.json: {e}")
        try:
            quiesce(d)
            raise SystemExit("SELF-TEST FAIL: quiesce passed with a corrupt lanes.json")
        except Fail as e:
            _check(e.code == 3 and "lane registry unreadable" in str(e), f"quiesce: {e}")
        ok("quiesce-corrupt-lanes")

        # (b) a non-SQLite mk.db: a named refusal, no traceback
        d = os.path.join(tmp, "notadb")
        os.makedirs(d, exist_ok=True)
        with open(src_db(d), "wb") as f:
            f.write(b"this is not a sqlite database at all" * 4)
        try:
            _registry_rows(d)
            raise SystemExit("SELF-TEST FAIL: a non-SQLite mk.db did not refuse")
        except Fail as e:
            _check(e.code == 3 and "lane registry unreadable" in str(e), f"non-SQLite mk.db: {e}")
        ok("quiesce-non-sqlite-db")

        # (c) a store at the wrong schema version
        d = os.path.join(tmp, "badschema")
        hm = os.path.join(tmp, "badschema-home")
        os.makedirs(d, exist_ok=True)
        con = mkdb.open_project(hm, "bad", data_dir=d)
        con.execute("INSERT INTO schema_version(version) VALUES (999)")
        con.commit()
        con.close()
        try:
            _registry_rows(d)
            raise SystemExit("SELF-TEST FAIL: a newer schema version did not refuse")
        except Fail as e:
            _check(e.code == 3 and "lane registry unreadable" in str(e), f"wrong schema: {e}")
        ok("quiesce-wrong-schema")

        # (d) an unreadable inbox (the ask read) refuses, never "no asks"
        try:
            pending_asks(os.path.join(tmp, "notadb"))
            raise SystemExit("SELF-TEST FAIL: an unreadable inbox did not refuse")
        except Fail as e:
            _check(e.code == 3 and "bus inbox unreadable" in str(e), f"unreadable inbox: {e}")
        ok("quiesce-unreadable-inbox")

        # 10b. review R1 HIGH 1: a plan `design` entry that escapes the repo is skipped; the
        # in-repo entry still copies. /etc/hosts, ../../x and a symlink out of the repo.
        d = os.path.join(tmp, "esc-repo", ".matrixkoder")
        hm = os.path.join(tmp, "esc-home")
        rp = os.path.dirname(d)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(rp, "inside.md"), "w") as f:
            f.write("# inside\n")
        os.symlink("/etc/hosts", os.path.join(rp, "escape-link"))
        design = ["inside.md", "/etc/hosts", "../../x", "escape-link"]
        root = mkdb.open_root(hm)
        root.execute("INSERT OR IGNORE INTO projects(slug, name, repo, data_dir, mode, created,"
                     " last_activity) VALUES ('esc','esc',?,?,?,?,?)",
                     (rp, os.path.realpath(d), "home", now_iso(), now_iso()))
        root.commit()
        root.close()
        con = mkdb.open_project(hm, "esc", data_dir=d)
        con.execute("INSERT INTO plans(id, version, title, status, header_json) VALUES"
                    " ('live', 1, 'L', 'running', ?)", (json.dumps({"design": design}),))
        con.execute("INSERT INTO runs(id, plan, status, started) VALUES ('r1', 'live', 'active', 't')")
        con.commit()
        con.close()
        rc, out, err = _cli(hm, ["--dry-run", "--slug", "esc", "--data", d])
        _check(rc == 0, f"design dry-run rc={rc} {err[-400:]}")
        _check(out.count("skip ") >= 3, f"escaped design refs were not reported: {out!r}")
        _check("inside.md" in out, f"the in-repo design ref was skipped: {out!r}")
        rc, out, err = _cli(hm, ["--slug", "esc", "--data", d])
        _check(rc == 0, f"design copy rc={rc} {err[-400:]}")
        assets = os.path.join(mkpaths.target_plan_dir("esc", "live", hm), "assets")
        _check(os.path.isfile(os.path.join(assets, "inside.md")),
               "the in-repo design ref was not copied")
        _check(not os.path.exists(os.path.join(assets, "hosts")),
               "an escaped design ref (/etc/hosts) was copied")
        _check(not os.path.exists(os.path.join(assets, "x")),
               "an escaped design ref (../..) was copied")
        ok("design-ref-escape-skipped")

        # 10c. review R1 HIGH 3: a write to the source AFTER do_copy_verify but before the state
        # read makes the switch refuse; db_path stays NULL, no marker, the NEW copy is removed.
        shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
        clear_db_path(home, "fixture")
        pre_events = state_of(src_db(data))[0]["events"]
        inject = ("import importlib.util, os; "
                  "s = importlib.util.spec_from_file_location('m', os.path.join(%r, 'mkdb.py')); "
                  "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                  "m.append_event(os.environ['MK_MIGRATE_HOME'], 'fixture', 'r1', task='P1.1',"
                  " chan='bus', kind='info', from_id='orch', to_id='P1.1', text='post-verify', data_dir=%r)"
                  % (HERE, data))
        rc, out, err = _cli(home, ["--switch", "--slug", "fixture", "--data", data],
                            env={"MK_MIGRATE_AFTER_VERIFY": f"{sys.executable} -c {json.dumps(inject)}",
                                 "MK_MIGRATE_HOME": home})
        _check(rc == 3, f"a post-verify write did not refuse the switch (rc={rc}) {out[-200:]}")
        _check("source and target stores differ" in (_last_json(out) or {}).get("error", ""),
               f"wrong refusal: {out[-200:]}")
        _check(root_row(home, "fixture")["db_path"] is None, "the refused switch set db_path")
        _check(not os.path.exists(os.path.join(target_dir(home, "fixture"), SWITCH_MARKER)),
               "the refused switch wrote a marker")
        _check(not os.path.exists(mkpaths.target_project_db("fixture", home)),
               "the refused switch left the NEW copy behind")
        _check(state_of(src_db(data))[0]["events"] == pre_events + 1,
               "the refused switch disturbed the old DB")
        ok("switch-source-target-must-match")

        # 10d. review R1 guard: a plain copy on an ALREADY switched project refuses, before any
        # copy or _remove_new_copy, and leaves the live C2 store byte-identical. (Switch it back
        # first, write to the new DB, then read the hash across the refused copy.)
        shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
        clear_db_path(home, "fixture")
        _check(_cli(home, ["--switch", "--slug", "fixture", "--data", data])[0] == 0,
               "setup switch failed")
        switched_db = mkpaths.target_project_db("fixture", home)
        mkdb.append_event(home, "fixture", "r1", task="P1.1", chan="bus", kind="info",
                          from_id="orch", to_id="P1.1", text="post-switch",
                          data_dir=dest_dir_of(switched_db))
        live_hash = _hash_file(switched_db)
        rc, out, err = _cli(home, ["--slug", "fixture", "--data", data])
        _check(rc == 3 and "already migrated" in (_last_json(out) or {}).get("error", ""),
               f"copy on a switched project did not refuse (rc={rc}) {out[-200:]}")
        _check(_hash_file(switched_db) == live_hash, "the refused copy changed the live C2 store")
        _check(root_row(home, "fixture")["db_path"] == switched_db, "the refused copy cleared db_path")
        ok("copy-refuses-after-switch")
        shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
        clear_db_path(home, "fixture")

        # 10e. M1.1a (review R2 MEDIUM): the already-migrated refusal runs BEFORE the --dry-run
        # branch in cmd_copy and cmd_switch, so a dry run on a switched project exits 3 exactly
        # like the real command — and (the dry-run invariant) writes nothing at all.
        shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
        clear_db_path(home, "fixture")
        _check(_cli(home, ["--switch", "--slug", "fixture", "--data", data])[0] == 0,
               "setup switch for dry-run refusal failed")
        for extra in ([], ["--switch"]):
            before = _tree_hash(tmp)
            rc, out, err = _cli(home, ["--dry-run", *extra, "--slug", "fixture", "--data", data])
            _check(rc == 3, f"dry-run{extra} on a switched project did not refuse (rc={rc}) {out[-200:]}")
            _check("already migrated" in (_last_json(out) or {}).get("error", ""),
                   f"dry-run{extra} wrong refusal: {out[-200:]}")
            _check("SQLite backup API" not in out, f"dry-run{extra} still planned a copy: {out!r}")
            _check(_tree_hash(tmp) == before, f"dry-run{extra} on a switched project wrote something")
        ok("dry-run-refuses-after-switch")
        shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
        clear_db_path(home, "fixture")

        # 10f. M1.1b (review R2 LOW): a lanes.json whose `slots` is a list, string or number is a
        # C5 refusal, never an AttributeError. An absent/null slots still reads as no lanes.
        for label, slots in (("list", [1, 2]), ("string", "nope"), ("number", 7)):
            d = os.path.join(tmp, f"slots-{label}")
            os.makedirs(d, exist_ok=True)
            with open(mkpaths.lanes_file(d), "w") as f:
                json.dump({"schema": 1, "slots": slots}, f)
            try:
                _registry_rows(d)
                raise SystemExit(f"SELF-TEST FAIL: slots={label} did not refuse")
            except Fail as e:
                _check(e.code == 3 and "slots is not an object" in str(e),
                       f"slots={label} refusal: {e}")
            try:
                quiesce(d)
                raise SystemExit(f"SELF-TEST FAIL: quiesce passed with slots={label}")
            except Fail as e:
                _check(e.code == 3 and "lane registry unreadable" in str(e),
                       f"quiesce slots={label}: {e}")
        d = os.path.join(tmp, "slots-absent")
        os.makedirs(d, exist_ok=True)
        with open(mkpaths.lanes_file(d), "w") as f:
            json.dump({"schema": 1}, f)  # no `slots` key: an empty registry, not a refusal
        _check(_registry_rows(d) == [], "an absent slots key must read as no lanes")
        with open(mkpaths.lanes_file(d), "w") as f:
            json.dump({"schema": 1, "slots": None}, f)
        _check(_registry_rows(d) == [], "a null slots must read as no lanes")
        ok("registry-slots-not-an-object")

        # 10g. M1.2b (review R1 MEDIUM): a symlink inside the data dir is archived AS a symlink
        # (never followed, never escaping the folder) and counted by the completeness check. On
        # ANY failure the partial tarball is removed; the old folder stays.
        sym_repo = os.path.join(tmp, "sym-repo")
        sym_data = os.path.join(sym_repo, ".matrixkoder")
        sym_home = os.path.join(tmp, "sym-home")
        _seed(sym_home, sym_data, sym_repo)
        os.symlink("/etc/hosts", os.path.join(sym_data, "outside-link"))
        os.makedirs(os.path.join(sym_data, "subdir"), exist_ok=True)
        os.symlink("subdir", os.path.join(sym_data, "dirlink"))
        rc, _, err = _cli(sym_home, ["--switch", "--slug", "fixture", "--data", sym_data])
        _check(rc == 0, f"sym setup switch failed: {err[-200:]}")
        legacy = os.path.join(target_dir(sym_home, "fixture"), "legacy")
        # the failure path FIRST: a file the tar missed makes the recount refuse AND the partial
        # tarball is removed. (First, so the same-second tarball name cannot collide with the
        # success case below — `now_stamp` is second-resolution.)
        inject = "import sys; open(sys.argv[1], 'w').write('x')"
        late = os.path.join(sym_data, "late.txt")
        rc, out, err = _cli(sym_home, ["--archive", "--slug", "fixture", "--data", sym_data],
                            env={"MK_MIGRATE_AFTER_TAR":
                                 f"{sys.executable} -c {json.dumps(inject)} {late}"})
        _check(rc == 3, f"an incomplete archive did not refuse (rc={rc}) {out[-200:]}")
        _check("archive incomplete" in (_last_json(out) or {}).get("error", ""),
               f"wrong archive refusal: {out[-200:]}")
        _check(sorted(os.listdir(legacy)) == [],
               f"the partial tarball was left behind: {os.listdir(legacy)}")
        os.remove(os.path.join(sym_data, "late.txt"))
        # now the success path: the symlink is archived AS a symlink (never followed), the
        # recount balances, and the old folder is unchanged.
        # the switch's own state reads create mk.db-wal/-shm in the source dir: take the
        # unchanged-across-archive baseline now, after the switch (test 7 does the same).
        sym_old_files = _tree_hash(sym_data)
        rc, out, err = _cli(sym_home, ["--archive", "--slug", "fixture", "--data", sym_data])
        _check(rc == 0, f"archive with symlinks rc={rc} {out[-300:]} {err[-300:]}")
        tar_path = _last_json(out)["tar"]
        _check(_last_json(out)["files"] == count_disk_entries(sym_data),
               f"the symlink count did not balance: {out[-200:]}")
        with tarfile.open(tar_path) as tf:
            got = {m.name: m for m in tf}
        link = next((m for n, m in got.items() if n.endswith("outside-link")), None)
        _check(link is not None, "the symlink was not archived at all")
        _check(link.issym() and link.linkname == "/etc/hosts",
               f"the symlink was not stored as a symlink: {link}")
        _check(_tree_hash(sym_data) == sym_old_files, "--archive changed the old folder")
        _check(os.path.isdir(sym_data) and os.path.isfile(src_db(sym_data)),
               "--archive removed the old data or DB")
        ok("archive-symlinks-and-partial-cleanup")

        # 10h. M1.2a (review R1 LOW): cmd_rollback reads the old and new state INSIDE switch_lock.
        # Proof: hold the migrator lock AND make the new DB diverge from the switch marker. The new
        # code refuses on the LOCK before reading any state; the old code read state first and
        # reported the divergence. The refusal names the lock, not the DB.
        shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
        clear_db_path(home, "fixture")
        _check(_cli(home, ["--switch", "--slug", "fixture", "--data", data])[0] == 0,
               "setup switch for the rollback-lock case failed")
        locked_db = mkpaths.target_project_db("fixture", home)
        mkdb.append_event(home, "fixture", "r1", task="P1.1", chan="bus", kind="info",
                          from_id="orch", to_id="P1.1", text="diverge", data_dir=dest_dir_of(locked_db))
        lp = switch_lock_path(home)
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        holder = open(lp, "a")
        _fcntl.flock(holder, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        try:
            rc, out, err = _cli(home, ["--rollback", "--slug", "fixture", "--data", data])
            msg = (_last_json(out) or {}).get("error", "")
            _check(rc == 3 and "switch lock" in msg,
                   f"rollback read state before taking the lock: {msg!r}")
        finally:
            _fcntl.flock(holder, _fcntl.LOCK_UN)
            holder.close()
        ok("rollback-takes-lock-before-state")
        shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
        clear_db_path(home, "fixture")

        # 10i. M1.3-S1 (helper finding, HIGH): a copy that raises mid-way must not leave a partial
        # C2 copy behind. An unreadable round file makes copy_moves raise; the new code removes the
        # NEW copy and prints one JSON refusal. On 179fa58 the raise escaped with a traceback, the
        # partial C2 store stayed, and a later --dry-run reported ok:true.
        shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
        clear_db_path(home, "fixture")
        locked = os.path.join(data, "runs", "r1", "tasks", "P1.1", "brief-r1.md")
        os.chmod(locked, 0o000)
        try:
            rc, out, err = _cli(home, ["--slug", "fixture", "--data", data])
            _check(rc == 3, f"an unreadable file did not refuse cleanly (rc={rc}) {err[-200:]}")
            _check("copy failed" in (_last_json(out) or {}).get("error", ""),
                   f"wrong copy refusal: {out[-200:]}")
            _check(not os.path.exists(mkpaths.target_project_db("fixture", home)),
                   "the partial C2 copy was left behind")
            # and a dry run must not advertise a copy that cannot verify
            rc2, out2, _ = _cli(home, ["--dry-run", "--slug", "fixture", "--data", data])
            _check(rc2 == 0 and (_last_json(out2) or {}).get("ok") is True,
                   f"dry-run after a clean failure should still plan: rc={rc2} {out2[-120:]}")
        finally:
            os.chmod(locked, 0o644)
        ok("partial-copy-removed-on-failure")

        # 10j. M1.3-S2 (helper finding, MEDIUM): a symlink inside a plan's assets tree that points
        # OUTSIDE the repo is refused, never dereferenced — its target's bytes must not enter the
        # C2 store. On 179fa58 copytree followed it and stored a regular file.
        shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
        clear_db_path(home, "fixture")
        outside = os.path.join(tmp, "outside-assets")
        os.makedirs(outside, exist_ok=True)
        secret = os.path.join(outside, "leak.txt")
        with open(secret, "w") as f:
            f.write("SECRET-CONTENT")
        pdir = mkpaths.plan_dir(data, "live")
        os.makedirs(pdir, exist_ok=True)
        os.symlink(secret, os.path.join(pdir, "leak-link"))
        rc, out, err = _cli(home, ["--slug", "fixture", "--data", data])
        _check(rc == 3, f"an out-of-repo assets symlink was not refused (rc={rc}) {out[-200:]}")
        _check("escapes the repo" in (_last_json(out) or {}).get("error", ""),
               f"wrong symlink refusal: {out[-200:]}")
        _check(not os.path.exists(mkpaths.target_project_db("fixture", home))
               and not os.path.exists(os.path.join(target_dir(home, "fixture"), "plans")),
               "the refused copy left a partial C2 tree")
        # an IN-repo symlink still copies, as a symlink. (The design ref `live-research.md` is
        # dropped here: this seed's design entry lands in the SAME assets dst the tree copies from a
        # different src, a pre-existing false verify refused below and not part of M1 — reported,
        # not fixed.)
        os.remove(os.path.join(pdir, "leak-link"))
        con = mkdb.open_project(home, "fixture", data_dir=data)
        con.execute("UPDATE plans SET header_json='{}' WHERE id='live'")
        con.commit()
        con.close()
        inside_target = os.path.join(os.path.dirname(data), "inside-target.txt")
        with open(inside_target, "w") as f:
            f.write("inside")
        os.symlink(inside_target, os.path.join(pdir, "in-link"))
        rc, out, err = _cli(home, ["--slug", "fixture", "--data", data])
        _check(rc == 0, f"an in-repo assets symlink was refused (rc={rc}) {out[-200:]}")
        dst_link = os.path.join(
            mkpaths.target_plan_dir("fixture", "live", home), "assets", "in-link")
        _check(os.path.islink(dst_link), f"the in-repo symlink was dereferenced: {dst_link}")
        os.remove(os.path.join(pdir, "in-link"))
        ok("assets-symlink-escape-refused")

        # 11. mutation proofs (R13): each guard is shown load-bearing. A deliberately broken copy
        # of this file (recall disabled / quiesce disabled / old-folder delete enabled / switch
        # equality skipped) is run and shown to change the outcome — so the guard test is proven
        # to catch the bad version.
        _mutation_recount(home, data, tmp)
        ok("mutation-recount")
        _mutation_quiesce(home, data, tmp)
        ok("mutation-quiesce")
        _mutation_delete(home, data, tmp)
        ok("mutation-delete-old")
        _mutation_switch_cmp(home, data, tmp)
        ok("mutation-switch-cmp")

        # 12. Z7 (todo 117): one data dir can have TWO root rows — the canonical --slug row and
        # the derived alias (`p`+sha256 of the data dir) mk-bus/mk-state register on first use,
        # which every lane token and the bus resolve through. A `WHERE slug=?` switch left the
        # alias row's db_path NULL, so the bus kept writing the OLD store after the switch.
        # Each case FAILS on ee0d2a9 (proof in the report): the alias row stayed NULL.
        _alias_root_row(home, data, repo)
        other_data = os.path.join(tmp, "other-repo", ".matrixkoder")
        os.makedirs(other_data, exist_ok=True)
        _other_root_row(home, other_data, os.path.join(tmp, "other-repo"))
        slugs = switch_slugs(home, "fixture", data)
        _check(slugs[0] == "fixture" and alias_slug(data) in slugs and len(slugs) == 2,
               f"switch_slugs missed a row of this data dir: {slugs}")
        ok("switch-slugs-enumerates-the-data-dir")

        # 12a. --dry-run names every row it would change. It writes nothing: db_path stays NULL
        # and the C2 tree is not created. (A whole-tree hash is not used here: a subprocess read
        # of the root store leaves SQLite's -shm/-wal beside it, which is not a write of ours.)
        rc, out, err = _cli(home, ["--dry-run", "--switch", "--slug", "fixture", "--data", data])
        _check(rc == 0, f"dry-run switch rc={rc} {err[-300:]}")
        _check("switch fixture: set projects.db_path = " in out,
               f"the dry run did not name the slug row: {out!r}")
        _check(f"switch {alias_slug(data)}: set projects.db_path = " in out,
               f"the dry run did not name the alias row: {out!r}")
        _check(root_row(home, "fixture")["db_path"] is None
               and root_row(home, alias_slug(data))["db_path"] is None,
               "the dry-run switch changed a root row")
        _check(not os.path.exists(mkpaths.target_project_dir("fixture", home)),
               "the dry-run switch created the C2 tree")
        ok("dry-run-switch-names-every-row")

        # 12b. the real switch sets db_path on BOTH rows of this data dir; the third row, whose
        # data_dir is a DIFFERENT directory, is never touched.
        rc, out, err = _cli(home, ["--switch", "--slug", "fixture", "--data", data])
        _check(rc == 0, f"switch rc={rc} {err[-400:]}")
        _check(root_row(home, "fixture")["db_path"] == mkpaths.target_project_db("fixture", home),
               "the slug row did not switch")
        _check(root_row(home, alias_slug(data))["db_path"] == mkpaths.target_project_db("fixture", home),
               "the alias row did not switch (the bug: the bus keeps the OLD store)")
        _check(root_row(home, "otherproj")["db_path"] is None,
               "a row of another data dir was switched")
        marker = _read_marker(home, "fixture") or {}
        _check(sorted(marker.get("slugs") or []) == sorted(slugs),
               f"the marker did not record the switched slugs: {marker.get('slugs')}")
        ok("switch-sets-db-path-on-every-row")

        # 12c. --rollback clears db_path on the same set, and names it in the dry run.
        rc, out, err = _cli(home, ["--dry-run", "--rollback", "--slug", "fixture", "--data", data])
        _check(rc == 0, f"dry-run rollback rc={rc} {err[-300:]}")
        _check(f"rollback {alias_slug(data)}: clear projects.db_path" in out,
               f"the rollback dry run did not name the alias row: {out!r}")
        rc, out, err = _cli(home, ["--rollback", "--slug", "fixture", "--data", data])
        _check(rc == 0, f"rollback rc={rc} {err[-400:]}")
        _check(root_row(home, "fixture")["db_path"] is None, "the slug row was not rolled back")
        _check(root_row(home, alias_slug(data))["db_path"] is None,
               "the alias row was not rolled back (a rollback would leave the bus on the new store)")
        _check(root_row(home, "otherproj")["db_path"] is None,
               "a row of another data dir was rolled back")
        ok("rollback-clears-every-row")

        # 12d. a marker written before the slugs list (scalar `slug` only) still rolls back every
        # row of the data dir, through the data-dir match.
        shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
        clear_db_path(home, "fixture")
        rc, out, err = _cli(home, ["--switch", "--slug", "fixture", "--data", data])
        _check(rc == 0, f"the setup switch for the old-marker case failed (rc={rc}) {err[-300:]}")
        old_marker = _read_marker(home, "fixture") or {}
        old_marker.pop("slugs", None)
        _write_marker(home, "fixture", old_marker)
        rc, out, err = _cli(home, ["--rollback", "--slug", "fixture", "--data", data])
        _check(rc == 0, f"rollback over an old marker failed (rc={rc}) {err[-300:]}")
        _check(root_row(home, alias_slug(data))["db_path"] is None,
               "an old marker left the alias row switched")
        _check(root_row(home, "otherproj")["db_path"] is None,
               "an old marker rolled back another data dir")
        clear_db_path(home, [alias_slug(data), "fixture", "otherproj"])
        ok("rollback-old-marker-covers-every-row")

        # 12e. Z7 (helper finding, MEDIUM): a --dry-run --rollback over a LIVE switched tree
        # (every row of the home's data dir already migrated) must change nothing: the same
        # "dry run writes nothing" invariant case 2 asserts on the fresh layout, asserted on the
        # switched one — the real layout the live run operated on.
        z7_repo = os.path.join(tmp, "z7-repo")
        z7_data = os.path.join(z7_repo, ".matrixkoder")
        z7_home = os.path.join(tmp, "z7-home")
        _seed(z7_home, z7_data, z7_repo)
        z7_alias = "p" + hashlib.sha256(os.path.realpath(z7_data).encode()).hexdigest()[:16]
        _root_rows_set(z7_home, [(z7_alias, z7_data)])
        rc, out, err = _cli(z7_home, ["--switch", "--slug", "fixture", "--data", z7_data])
        _check(rc == 0, f"the 12e setup switch failed (rc={rc}) {err[-300:]}")
        rc, out, err = _cli(z7_home, ["--dry-run", "--rollback", "--slug", "fixture",
                                      "--data", z7_data])
        _check(rc == 0, f"the 12e dry-run rollback failed (rc={rc}) {err[-300:]}")
        _check(root_row(z7_home, "fixture")["db_path"] ==
               mkpaths.target_project_db("fixture", z7_home),
               "the 12e dry-run rollback cleared db_path")
        _check(root_row(z7_home, z7_alias)["db_path"] ==
               mkpaths.target_project_db("fixture", z7_home),
               "the 12e dry-run rollback cleared the alias row")
        shutil.rmtree(mkpaths.target_project_dir("fixture", z7_home), ignore_errors=True)
        clear_db_path(z7_home, [z7_alias, "fixture"])
        ok("dry-run-names-only-what-it-would-change")

        # 13. Z15 (todo: R02): a plan approved BEFORE the switch must still land in its plan
        # folder with a plan.md and an assets/ — the C2 tree the e2e audit's R02 checks. Each
        # case FAILS on e83e6e9 (proof in the report): the plan folder held only archive/.
        z15_repo = os.path.join(tmp, "z15-repo")
        z15_data = os.path.join(z15_repo, ".matrixkoder")
        z15_home = os.path.join(tmp, "z15-home")
        os.makedirs(os.path.join(z15_data, "plans"), exist_ok=True)
        os.makedirs(z15_home, exist_ok=True)
        subprocess.run(["git", "init", "-q", z15_repo], check=False)
        # the design doc the plan header lists; it rides into assets/ (C2)
        with open(os.path.join(z15_repo, "z15-research.md"), "w") as f:
            f.write("# the design note assets/ carries\n")
        z15_plan = os.path.join(z15_repo, "z15plan.md")
        with open(z15_plan, "w") as f:
            f.write(Z15_PLAN)
        mkdb.open_project(z15_home, "fixture", data_dir=z15_data).close()
        _root_rows_set(z15_home, [("fixture", z15_data)])
        # import + approve BEFORE the switch, then start a run so the plan is live
        rc, out, err = _tool("mk-plan.py", z15_home, "import", z15_plan,
                             "--data", z15_data, "--home", z15_home)
        _check(rc == 0, f"Z15 import rc={rc} {out[-300:]}")
        rc, out, err = _tool("mk-plan.py", z15_home, "approve", z15_plan,
                             "--data", z15_data, "--home", z15_home)
        _check(rc == 0, f"Z15 approve rc={rc} {out[-300:]}")
        rc, out, err = _tool("mk-state.py", z15_home, "run", "init", "--data", z15_data,
                             "--by", "orch", "--id", "z15plan-r1", "--feature", "z15plan",
                             "--branch", "mk/z15plan/lane", "--plan", z15_plan,
                             env={"MATRIXKODER_DATA": z15_data})
        _check(rc == 0, f"Z15 run init rc={rc} {out[-300:]}")
        # BEFORE the switch the plan folder does not exist at all (the gap this task closes)
        z15_pdir = mkpaths.target_plan_dir("fixture", "z15plan", z15_home)
        _check(not os.path.exists(z15_pdir), "Z15: the C2 plan folder already existed before the switch")
        z15_old = _tree_hash(z15_data)
        # (a) switch: plan.md (the approved bytes), assets/ (the design ref) and archive/
        rc, out, err = _cli(z15_home, ["--switch", "--slug", "fixture", "--data", z15_data,
                                       "--repo", z15_repo])
        _check(rc == 0, f"Z15 switch rc={rc} {out[-400:]}")
        z15_md = os.path.join(z15_pdir, "plan.md")
        _check(os.path.isfile(z15_md), f"Z15: no plan.md in the plan folder ({z15_pdir})")
        _check(os.path.isdir(os.path.join(z15_pdir, "assets")),
               "Z15: the plan folder has no assets/")
        _check(os.path.isdir(os.path.join(z15_pdir, "archive")),
               "Z15: the plan folder has no archive/")
        with open(z15_md, "rb") as f:
            z15_md_bytes = f.read()
        with open(os.path.join(z15_pdir, "archive", "z15plan-v1.md"), "rb") as f:
            z15_arch_bytes = f.read()
        _check(z15_md_bytes == z15_arch_bytes,
               "Z15: plan.md is not the approved version's bytes")
        _check(b'status = "approved"' in z15_md_bytes,
               "Z15: plan.md does not carry the approved status")
        _check(os.path.isfile(os.path.join(z15_pdir, "assets", "z15-research.md")),
               "Z15: the design ref was not carried into assets/")
        _check(_tree_hash(z15_data) == z15_old,
               "Z15: the switch changed the old data dir (copy, never move)")
        ok("z15-live-plan-folder-after-switch")

        # (b) rollback removes what the copy wrote: plan.md, assets/ and archive/ go with it.
        _check(os.path.isfile(z15_md) and os.path.isdir(os.path.join(z15_pdir, "assets")),
               "Z15: the copied plan folder is not there to roll back")
        rc, out, err = _cli(z15_home, ["--rollback", "--slug", "fixture", "--data", z15_data])
        _check(rc == 0, f"Z15 rollback rc={rc} {out[-400:]}")
        _check(not os.path.exists(z15_pdir),
               f"Z15: rollback left the plan folder behind ({z15_pdir})")
        _check(os.path.isfile(os.path.join(z15_data, "mk.db")),
               "Z15: rollback removed the OLD store")
        ok("z15-rollback-removes-the-plan-folder")

        # 13c. a plan with NO asset file at all still gets an empty assets/ (C2: the folder is
        # part of the tree, not a side effect of having an asset).
        z15b_repo = os.path.join(tmp, "z15b-repo")
        z15b_data = os.path.join(z15b_repo, ".matrixkoder")
        z15b_home = os.path.join(tmp, "z15b-home")
        os.makedirs(os.path.join(z15b_data, "plans"), exist_ok=True)
        os.makedirs(z15b_home, exist_ok=True)
        subprocess.run(["git", "init", "-q", z15b_repo], check=False)
        z15b_plan = os.path.join(z15b_repo, "z15bplan.md")
        with open(z15b_plan, "w") as f:
            f.write(Z15_PLAN.replace('design = ["z15-research.md"]', "design = []")
                    .replace('plan = "z15plan"', 'plan = "z15bplan"'))
        mkdb.open_project(z15b_home, "fixture", data_dir=z15b_data).close()
        _root_rows_set(z15b_home, [("fixture", z15b_data)])
        rc, out, err = _tool("mk-plan.py", z15b_home, "import", z15b_plan,
                             "--data", z15b_data, "--home", z15b_home)
        _check(rc == 0, f"Z15c import rc={rc} {out[-300:]}")
        rc, out, err = _tool("mk-plan.py", z15b_home, "approve", z15b_plan,
                             "--data", z15b_data, "--home", z15b_home)
        _check(rc == 0, f"Z15c approve rc={rc} {out[-300:]}")
        rc, out, err = _tool("mk-state.py", z15b_home, "run", "init", "--data", z15b_data,
                             "--by", "orch", "--id", "z15bplan-r1", "--feature", "z15bplan",
                             "--branch", "mk/z15bplan/lane", "--plan", z15b_plan,
                             env={"MATRIXKODER_DATA": z15b_data})
        _check(rc == 0, f"Z15c run init rc={rc} {out[-300:]}")
        rc, out, err = _cli(z15b_home, ["--switch", "--slug", "fixture", "--data", z15b_data,
                                        "--repo", z15b_repo])
        _check(rc == 0, f"Z15c switch rc={rc} {out[-400:]}")
        z15b_pdir = mkpaths.target_plan_dir("fixture", "z15bplan", z15b_home)
        _check(os.path.isdir(os.path.join(z15b_pdir, "assets")),
               "Z15c: a plan with no asset file got no assets/ folder")
        _check(os.listdir(os.path.join(z15b_pdir, "assets")) == [],
               "Z15c: assets/ should be empty when the plan has no asset")
        _check(os.path.isfile(os.path.join(z15b_pdir, "plan.md")),
               "Z15c: the plan folder has no plan.md")
        ok("z15-empty-assets-folder")

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


def _mutation_switch_cmp(home, data, tmp):
    """(4) skip the source==target check under the switch lock: with the real file a post-verify
    write refuses the switch (db_path stays NULL); with the broken file the switch commits a
    mismatched copy anyway. The difference IS the proof (review R1 HIGH 3)."""
    sandbox = os.path.join(tmp, "mut-switch")
    mutant = _mutant([
        ("        if old_state is None or new_state is None or old_state != new_state:\n"
         "            _remove_new_copy(home, slug)\n"
         '            raise Fail("verify under switch lock failed: source and target stores differ", 3)',
         "        pass  # the mutation: source==target check removed"),
    ], sandbox)
    inject = ("import importlib.util, os; "
              "s = importlib.util.spec_from_file_location('m', os.path.join(os.path.dirname(%r), 'mkdb.py')); "
              "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); "
              "m.append_event(os.environ['MK_MIGRATE_HOME'], 'fixture', 'r1', task='P1.1',"
              " chan='bus', kind='info', from_id='orch', to_id='P1.1', text='post-v', data_dir=%r)"
              % (mutant, data))
    env = {"MK_MIGRATE_AFTER_VERIFY": f"{sys.executable} -c {json.dumps(inject)}",
           "MK_MIGRATE_HOME": home}
    shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
    clear_db_path(home, "fixture")
    rc_real, out_real, _ = _run_mutant(os.path.abspath(__file__), home,
                                       ["--switch", "--slug", "fixture", "--data", data], env=env)
    _check(rc_real == 3, f"mutation-switch-cmp: the real file did not refuse (rc={rc_real})")
    _check(root_row(home, "fixture")["db_path"] is None,
           "mutation-switch-cmp: the real file switched anyway")
    shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
    clear_db_path(home, "fixture")
    rc_broken, out_broken, _ = _run_mutant(mutant, home,
                                           ["--switch", "--slug", "fixture", "--data", data], env=env)
    _check(rc_broken == 0, f"mutation-switch-cmp: the broken version still refused (rc={rc_broken})")
    _check(root_row(home, "fixture")["db_path"],
           "mutation-switch-cmp: the broken version did not switch — the guard is not the thing tested")
    shutil.rmtree(mkpaths.target_project_dir("fixture", home), ignore_errors=True)
    clear_db_path(home, "fixture")


def _mutation_recount(home, data, tmp):
    """(1) skip the recount ('if False'): with the real file a write during the copy is caught and
    the NEW copy removed; with the broken file the copy lands anyway. The difference IS the proof."""
    sandbox = os.path.join(tmp, "mut-recount")
    mutant = _mutant([
        ('if dst_counts != src_counts or dst_digest != src_digest:\n            bad.append("mk.db")',
         'if False:\n            bad.append("mk.db")'),
        ('if now_counts != src_counts or now_digest != src_digest:\n            bad.append("mk.db (the source changed during the copy)")',
         'if False:\n            bad.append("mk.db (the source changed during the copy)")'),
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
