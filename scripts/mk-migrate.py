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
--v2-retire : (L10.Y) AFTER a verified --v2 switch of --slug S: MOVE (never delete) the old stores and the
              leftovers the v2 layout no longer holds into <home>/backups/retire-<stamp>/ with a manifest
              (path, size, sha256), drop the junk `projects` rows, and let `--v2-retire --rollback` put every
              byte back. See cmd_v2_retire.
--v2        : (L10.8) the whole-home move onto the v2 names, from BOTH source shapes: a v1 C2 home
              (`projects.db_path` set, store `projects/<slug>/mk.db`) and a pre-migration project
              (store `<repo>/.matrixkoder/mk.db`, no db_path). `--v2 --slug A[,B] [--dry-run]` copies each
              store with SQLite's backup API into the NEW-name file (mk-overview.db, mk-project.db) beside
              the old one, migrates the copy to the current schema, verifies it table by table, points
              `projects.db_path` at it, carries settings (mk-config migrate-settings), lane slots and watch
              cursors into the copy, and moves the leftover files into <home>/backups/migrate-<stamp>/.
              Never a bare rename and never a write to an old store. `--v2 --rollback` removes the new
              files and puts the leftovers back; the old files were never touched.

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
    return mkpaths.project_db(data=data, legacy=True)  # the old store, even after the switch (rollback)


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


def _open_ro_any_schema(path):
    """An existing store read-only whatever its schema (None when absent). A pre-migration store (hermes-switchui
    is schema 4) is exactly what a migration reads, and `mkdb._open_read` refuses an OLDER store ("run a writer
    command"), which made the quiesce check impossible. Only a store NEWER than this code is refused."""
    if not os.path.isfile(path):
        return None
    con = mkdb._connect_ro(path)
    try:
        have = mkdb._schema_version(con)
    except Exception:
        con.close()
        raise
    if have > mkdb.SCHEMA:
        con.close()
        raise Fail(f"store schema v{have} is newer than this code (v{mkdb.SCHEMA}); upgrade mkdb.py", 3)
    return con


def _registry_rows(data):
    """The lane registry as rows (slot, lane_path, state, run, task), or None when the registry is
    genuinely absent (no store, no `lane_slots` table, no lanes.json).

    Review R1 HIGH 2: a registry that exists but cannot be read is a C5 refusal, never a quiet
    "no lanes". Every read failure — a corrupt/non-SQLite store, a store at the wrong schema
    version, malformed lanes.json — becomes `Fail(..., 3)`; only true absence stays quiet."""
    db = src_db(data)
    if db and os.path.isfile(db):
        try:
            con = _open_ro_any_schema(db)
        except sqlite3.Error as e:
            raise Fail(f"lane registry unreadable (C5 refusal): {db}: {e}", 3)
        except Exception as e:  # mkdb.Fail: a store NEWER than this code
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
        # the same two SELECTs mk-bus's StoreAdapter runs, on a read-only open that accepts a pre-migration
        # (older-schema) store: StoreAdapter refuses one, and v2 must be able to quiesce a schema-4 project
        con = _open_ro_any_schema(src_db(data))
        if con is None:
            asks, answers = [], []
        else:
            try:
                asks = [dict(r) for r in con.execute(
                    "SELECT id, run, t FROM events WHERE chan='bus' AND kind='ask' ORDER BY id")]
                answers = [dict(r) for r in con.execute(
                    "SELECT reply_to FROM events WHERE chan='bus' AND kind='answer'"
                    " AND reply_to IS NOT NULL")]
            finally:
                con.close()
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
        dst = mkdb._connect(dst_path, create_beside_legacy=True)  # the deliberate create (L10.X)
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
    their only home). Both shapes are read; the column wins when it carries a value.

    A numbered-block plan has no `design` header key: it names its documents on the Goal block's
    `Sources:` line (templates/plan.md, block 3). That line is read out of the stored body, and it
    is used only when neither the column nor the header key named anything, so an old plan keeps
    the answer it always had. The paths are the line's backticked `...` spans, else its
    comma-separated items; `none` names nothing."""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(plans)")}
    if "design" in cols:
        row = con.execute("SELECT design FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row and row["design"]:
            try:
                got = json.loads(row["design"])
            except ValueError:
                got = None
            if isinstance(got, list) and got:
                return [x for x in got if isinstance(x, str) and x]
    hdr = _header(con, plan_id)
    old = [x for x in (hdr.get("design") or []) if isinstance(x, str) and x]
    return old or _sources_refs(hdr.get("body") or "")


SOURCES_LINE_RE = re.compile(r"^\s*-\s*Sources\s*:\s*(.*)$", re.M)
GOAL_BLOCK_RE = re.compile(r"^\+\+\+\s+3\s+Goal\b(.*?)^/\+{3}\s*$", re.M | re.S)


def _sources_refs(body):
    """The paths a Goal block's `- Sources:` line names, or [] when there is no such line.
    The block is found by its number (3 Goal), so a `Sources:` line quoted elsewhere does not
    count. `none` and links name no path under the checkout."""
    m = GOAL_BLOCK_RE.search(body or "")
    if not m:
        return []
    line = SOURCES_LINE_RE.search(m.group(1))
    if not line:
        return []
    text = line.group(1).strip()
    if not text or text.lower().startswith("none"):
        return []
    ticks = re.findall(r"`([^`]+)`", text)
    cands = ticks or [x.strip() for x in text.split(",")]
    return [c for c in (c.strip() for c in cands) if c and "://" not in c and " " not in c]


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
                      "dst": os.path.join(target_dir(home, slug), mkpaths.LANES_NAME)})
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
    dbname = os.path.basename(mkpaths.target_project_db(slug, home))  # the file the copy wrote (new or legacy name)
    for name in (dbname, dbname + "-wal", dbname + "-shm", mkpaths.PROJECT_YAML_NAME, mkpaths.CURSORS_NAME,
                 mkpaths.LANES_NAME, "plans"):
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


# ---------- v2: the whole-home move onto the v2 names (L10.8) ----------
#
# Both source shapes end in the same place: root `mk-overview.db`, project `mk-project.db`, settings as
# project DB rows, lane slots and watch cursors as rows, no leftover files in the project folder.
# The OLD files are never written: every new file is a SQLite-backup copy, so a rollback is "delete the
# new files, put the moved leftovers back" and the old tree is byte-identical to where it started.

V2_MARKER = ".migrate-v2.json"          # home-level bookkeeping (what v2 created / moved)
# Only state that now lives in the DB (lane slots, watch cursors) and lock files move away. project.yaml (and its
# .bak) is LIVE configuration: it is the lowest settings layer and holds keys migrate-settings does not carry
# (banned_commands, gates, baselines, agents, picks, helpers, qa, lanes_disposable), read by seven scripts.
LEFTOVER_RE = re.compile(r"^(lanes\.json|lanes\.lock|watch-cursors\.json|.+\.yaml\.lock)$")
SKIP_SUMS = {"schema_version"}          # a migration adds stamps: not user data
# the root tables a running process rewrites by itself (derived overview rows): they never block a rollback
ROOT_DERIVED = {"plan_summary", "project_summary", "sync", "models", "agents", "catalog_user", "schema_version"}


def _v2_marker_path(home):
    return os.path.join(home, V2_MARKER)


def _table_sums(path, ref=None):
    """{table: (rows, sha256)} read-only. With `ref` (another {table: [columns]}) the checksum covers only
    the columns the SOURCE had, so a migration that ADDS columns or tables does not read as a change."""
    con = open_ro(path)
    if con is None:
        return None
    try:
        out = {}
        for t in _tables(con):
            if t in SKIP_SUMS:
                continue
            cols = [r["name"] for r in con.execute(f"PRAGMA table_info({t})")]
            use = [c for c in (ref.get(t, cols) if ref else cols) if c in cols]
            h, n = hashlib.sha256(), 0
            for row in con.execute(f"SELECT {', '.join(use)} FROM {t} ORDER BY rowid"):
                n += 1
                h.update(repr(tuple(row)).encode())
            out[t] = (n, h.hexdigest(), cols)
        return out
    finally:
        con.close()


def _v2_compare(src, dst):
    """Problems between a source store and its migrated copy: every source table present, equal row count and
    equal checksum over the source's own columns. Tables the copy ADDED are fine."""
    a = _table_sums(src)
    if a is None:
        return [f"source store missing: {src}"]
    b = _table_sums(dst, ref={t: v[2] for t, v in a.items()})
    if b is None:
        return [f"copy missing: {dst}"]
    bad = []
    for t, (n, digest, _cols) in a.items():
        if t not in b:
            bad.append(f"{t}: table missing in the copy")
        elif b[t][0] != n:
            bad.append(f"{t}: {n} rows in the source, {b[t][0]} in the copy")
        elif b[t][1] != digest:
            bad.append(f"{t}: row checksum differs")
    return bad


def _file_sha(path):
    return _hash_file(path) if os.path.isfile(path) else None


def _v2_project(home, slug):
    """Plan one project: its shape, old and new store, folder and every root row that names it."""
    row = root_row(home, slug)
    if not row:
        raise Fail(f"no projects row for slug {slug!r}", 3)
    data = row.get("data_dir")
    if row.get("db_path"):
        shape, old = "c2", row["db_path"]
        tdir = os.path.dirname(old)
        data_for_registry = tdir
    else:
        if not data or not os.path.isdir(data):
            raise Fail(f"{slug}: no data dir at {data!r} to migrate from", 3)
        shape, old = "legacy", src_db(data)
        tdir = target_dir(home, slug)
        data_for_registry = data
    if not os.path.isfile(old):
        raise Fail(f"{slug}: no project store at {old}", 3)
    new = mkpaths.new_db_in(tdir, "project")
    if os.path.realpath(old) == os.path.realpath(new):
        raise Fail(f"{slug}: already on the v2 names ({new})", 3)
    if os.path.realpath(tdir) != os.path.realpath(target_dir(home, slug)):
        raise Fail(f"{slug}: its store folder {tdir} is not the C2 folder {target_dir(home, slug)}; refusing to guess", 3)
    return {"slug": slug, "shape": shape, "data": data, "old": old, "tdir": tdir, "new": new,
            "slugs": switch_slugs(home, slug, data) if data else [slug], "registry": data_for_registry,
            "leftovers": sorted(n for n in (os.listdir(tdir) if os.path.isdir(tdir) else ())
                                if os.path.isfile(os.path.join(tdir, n)) and LEFTOVER_RE.match(n)),
            "new_exists": os.path.exists(new)}


def _v2_plan(home, slugs):
    root_old = mkpaths.root_db_in(home)
    root_new = mkpaths.new_db_in(home, "root")
    if not os.path.isfile(root_old):
        raise Fail(f"no root store at {root_old}", 3)
    projects = [_v2_project(home, s) for s in slugs]
    return {"root_old": root_old, "root_new": root_new,
            "root_copy": os.path.realpath(root_old) != os.path.realpath(root_new),
            "root_new_exists": os.path.exists(root_new), "projects": projects}


def _carry_lanes(con, lanes_json):
    """Insert the slots of a leftover lanes.json that the store does not hold yet (a store row is newer and
    never overwritten). -> the slots carried."""
    doc = _read_json(lanes_json) if os.path.isfile(lanes_json) else None
    slots = doc.get("slots") if isinstance(doc, dict) else None
    if not isinstance(slots, dict):
        return []
    have = {r[0] for r in con.execute("SELECT slot FROM lane_slots")}
    carried = []
    for k, rec in sorted(slots.items(), key=lambda kv: str(kv[0])):
        if str(k) in have or not isinstance(rec, dict):
            continue
        con.execute(
            "INSERT INTO lane_slots(slot, lane_path, state, run, task, workspace, workspace_id, branch,"
            " lock_hash, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(k), rec.get("path"), rec.get("state"), rec.get("run"), rec.get("task"), rec.get("workspace"),
             rec.get("workspace_id"), rec.get("branch"), rec.get("lock_hash"), rec.get("updated")))
        carried.append(str(k))
    return carried


def _carry_cursors(con, cursors_json):
    """Insert the watch cursors of a leftover watch-cursors.json that the store does not hold yet.
    The file maps `<run or *>|<task or *>` -> {seq, started?, updated?}. -> the keys carried."""
    doc = _read_json(cursors_json) if os.path.isfile(cursors_json) else None
    if not isinstance(doc, dict):
        return []
    have = {r[0] for r in con.execute("SELECT key FROM watch_cursors")}
    carried = []
    for key, rec in sorted(doc.items()):
        seq = rec.get("seq") if isinstance(rec, dict) else rec
        if key in have or not isinstance(seq, int) or isinstance(seq, bool):
            continue
        con.execute("INSERT INTO watch_cursors(key, seq, started, updated) VALUES (?,?,?,?)",
                    (key, seq, rec.get("started") if isinstance(rec, dict) else None,
                     rec.get("updated") if isinstance(rec, dict) else None))
        carried.append(key)
    return carried


def _carry_settings(home, tdir):
    """mk-config migrate-settings against the new store: yaml keys -> project rows, config.json -> mk-settings.yaml.
    The yaml is never edited. -> the reply."""
    env = dict(os.environ, MK_HOME=home, MATRIXKODER_HOME=home)
    r = subprocess.run([sys.executable, os.path.join(HERE, "mk-config.py"), "--data", tdir, "migrate-settings"],
                       capture_output=True, text=True, env=env)
    try:
        doc = json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        doc = {}
    if r.returncode != 0 or not doc.get("ok"):
        raise Fail(f"settings carry failed: {doc.get('error') or (r.stdout + r.stderr).strip()[-300:]}", 3)
    return doc


def _remove_files(paths):
    for p in paths:
        with contextlib.suppress(OSError):
            os.remove(p)


def _v2_undo_new(created):
    """Remove only files this run created (new stores + sidecars). Old files are never in this list."""
    for p in created:
        _remove_files([p, p + "-wal", p + "-shm"])


def _home_files(home):
    """The regular files directly in the MK home (not recursive): the snapshot that finds a file a carry step
    created there (mk-settings.yaml from config.json, and the lock file mk-config takes to write it)."""
    return {n for n in os.listdir(home) if os.path.isfile(os.path.join(home, n))}


def _note_home_created(home, before, created):
    """Add every home-level file that appeared since `before` to `created` (once), so an undo or a rollback
    removes it."""
    for n in sorted(_home_files(home) - before):
        path = os.path.join(home, n)
        if path not in created:
            created.append(path)


def _restore_home_files(restore):
    """Put back the exact bytes of a home-level file the carry step rewrote (copies kept in the backup), then drop
    the copy: after an undo nothing of this run stays in the home."""
    for copy, original in restore:
        if os.path.isfile(copy):
            shutil.copy2(copy, original)
            with contextlib.suppress(OSError):
                os.remove(copy)


def _prune_backup(backup):
    """Remove the (now empty) folders of this run's backup, and `backups/` itself when nothing else is in it.
    Never removes a file: a backup that still holds something is left as it is."""
    for d, _dirs, _files in sorted(os.walk(backup), key=lambda t: -len(t[0])):
        with contextlib.suppress(OSError):
            os.rmdir(d)
    with contextlib.suppress(OSError):
        os.rmdir(os.path.dirname(backup))


def cmd_v2(args):
    home = mkpaths.home(args.home)
    if args.rollback:
        return cmd_v2_rollback(home, args)
    slugs = [x for x in (args.slug or "").split(",") if x]
    if not slugs:
        raise Fail("--v2 needs --slug A[,B]: the projects to move onto the v2 names", 2)
    plan = _v2_plan(home, slugs)
    if os.path.exists(_v2_marker_path(home)):
        raise Fail(f"a v2 move is already recorded ({_v2_marker_path(home)}); --v2 --rollback first", 3)
    clash = [p["new"] for p in plan["projects"] if p["new_exists"]] + ([plan["root_new"]] if plan["root_new_exists"] and plan["root_copy"] else [])
    if clash:
        raise Fail("v2 target file(s) already exist, inspect or remove them: " + ", ".join(clash), 3)
    blocked = []
    for p in plan["projects"]:
        try:
            quiesce(p["registry"])
        except Fail as e:
            if not args.dry_run:
                raise
            blocked.append(f"{p['slug']}: {e}")  # a dry run still prints the plan and names what would refuse
    steps = []
    steps.append(f"copy root {plan['root_old']} -> {plan['root_new']} (SQLite backup API)" if plan["root_copy"]
                 else f"root already {plan['root_new']}")
    for p in plan["projects"]:
        steps.append(f"{p['slug']} [{p['shape']}]: copy {p['old']} -> {p['new']}, migrate the copy to schema {mkdb.SCHEMA}, verify table by table")
        steps.append(f"{p['slug']}: switch projects.db_path of {', '.join(p['slugs'])} to {p['new']}")
        steps.append(f"{p['slug']}: carry settings (mk-config migrate-settings), lane slots, watch cursors into the copy")
        extra = (" (a pre-migration project: its lanes and cursors are first copied into the C2 folder; its yaml becomes"
                 " the C2 settings file and STAYS there as live config)") if p["shape"] == "legacy" else ""
        steps.append(f"{p['slug']}: move {len(p['leftovers'])} leftover file(s) {p['leftovers']}{extra} to <home>/backups/migrate-<stamp>/{p['slug']}/ (the project settings file and its backup stay: live config)")
    if args.dry_run:
        for line in steps:
            print(line)
        for line in blocked:
            print(f"would refuse: {line}")
        return {"ok": True, "command": "v2", "dry_run": True, "home": home, "steps": steps, "would_refuse": blocked,
                "projects": [{k: p[k] for k in ("slug", "shape", "old", "new", "tdir", "slugs", "leftovers")}
                             for p in plan["projects"]],
                "root": {"old": plan["root_old"], "new": plan["root_new"]}}
    return _v2_apply(home, plan)


def _v2_apply(home, plan):
    stamp = now_stamp()
    backup = os.path.join(home, "backups", f"migrate-{stamp}")
    created, moved = [], []          # created: new store files; moved: (backup path, original path)
    restore = []                     # (backup copy, original) of a home-level file the carry step may rewrite
    home_before = _home_files(home)
    marker = {"home": home, "stamp": stamp, "backup": backup, "created_root": False, "projects": [],
              "old_sha": {}, "switched_at": None}
    with switch_lock(home):
        for p in plan["projects"]:
            quiesce(p["registry"])
        try:
            # 1. the project copies (the old stores are only read)
            for p in plan["projects"]:
                if p["shape"] == "legacy":
                    do_copy_verify(home, p["data"], p["slug"])  # backup-API copy + plan files + verify (root row exists: _v2_project)
                else:
                    copy_db(p["old"], p["new"])
                created.append(p["new"])
                marker["old_sha"][p["old"]] = _file_sha(p["old"])
            # 2. the root copy (becomes the root every resolver reads: the new name wins)
            if plan["root_copy"]:
                marker["old_sha"][plan["root_old"]] = _file_sha(plan["root_old"])
                copy_db(plan["root_old"], plan["root_new"])
                created.append(plan["root_new"])
                marker["created_root"] = True
            # 3. migrate the copies to the current schema, through mkdb (the only writer)
            mkdb.open_root(home).close()
            for p in plan["projects"]:
                mkdb.open_project(home, p["slug"]).close()
            # 4. verify: every source table, row count and checksum equal in the copy
            bad = []
            for p in plan["projects"]:
                bad += [f"{p['slug']} {x}" for x in _v2_compare(p["old"], p["new"])]
            if plan["root_copy"]:
                bad += [f"root {x}" for x in _v2_compare(plan["root_old"], plan["root_new"])]
            if bad:
                raise Fail("v2 verify failed: " + "; ".join(bad) + " — the new files were removed, the old ones are untouched", 3)
            # 5. switch: the NEW root's rows point at the new stores
            for p in plan["projects"]:
                set_db_path(home, p["slugs"], p["new"])
            # 6. carry settings, lane slots and watch cursors into the copies. migrate-settings may CREATE
            # <home>/mk-settings.yaml (from config.json) or rewrite an existing one: keep the exact old bytes
            # in the backup first, and note every home-level file it creates, so an undo / a rollback restores
            # the home byte for byte.
            ms = mkpaths.mk_settings(home)
            if os.path.isfile(ms):
                keep = os.path.join(backup, "_home", os.path.basename(ms))
                os.makedirs(os.path.dirname(keep), exist_ok=True)
                shutil.copy2(ms, keep)
                restore.append((keep, ms))
            carried = {}
            for p in plan["projects"]:
                settings = _carry_settings(home, p["tdir"])
                _note_home_created(home, home_before, created)
                con = mkdb.open_project(home, p["slug"])
                try:
                    lanes = _carry_lanes(con, mkpaths.legacy_lanes(p["tdir"]))
                    cursors = _carry_cursors(con, mkpaths.legacy_cursors(p["tdir"]))
                    con.commit()
                finally:
                    con.close()
                carried[p["slug"]] = {"settings": len(settings.get("carried") or []),
                                      "unknown_keys": settings.get("unknown_keys") or [],
                                      "comments": len(settings.get("comments") or []),
                                      "lanes": lanes, "cursors": cursors}
            # 7. the leftovers move into a named backup (never deleted)
            for p in plan["projects"]:
                # listed NOW, not at plan time: a pre-migration project's C2 folder only gets its copied
                # project.yaml / lanes.json / watch-cursors.json during step 1
                for name in sorted(n for n in os.listdir(p["tdir"]) if LEFTOVER_RE.match(n)):
                    src = os.path.join(p["tdir"], name)
                    if not os.path.isfile(src):
                        continue
                    dst = os.path.join(backup, p["slug"], name)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.move(src, dst)
                    moved.append((dst, src))
        except BaseException as e:
            # roll the partial v2 back: put moved files back, remove only what this run created. The old
            # stores and the old root were never written, so they are exactly as they started.
            for dst, src in reversed(moved):
                with contextlib.suppress(OSError):
                    shutil.move(dst, src)
            _note_home_created(home, home_before, created)
            _v2_undo_new(created)
            _restore_home_files(restore)
            _prune_backup(backup)
            for p in plan["projects"]:
                if p["shape"] == "legacy":
                    _remove_new_copy(home, p["slug"])  # the plan files do_copy_verify wrote into the C2 folder
            if isinstance(e, (Fail, KeyboardInterrupt, SystemExit)):
                raise
            raise Fail(f"v2 failed: {type(e).__name__}: {e} — the new files were removed, the old ones are untouched", 3)
        # 8. the record a rollback needs: what was created, moved, and the state of the new files now
        marker["switched_at"] = now_iso()
        marker["moved"] = [[a, b] for a, b in moved]
        marker["created"] = created
        marker["restore"] = [[a, b] for a, b in restore]
        for p in plan["projects"]:
            st = state_of(p["new"])
            marker["projects"].append({"slug": p["slug"], "slugs": p["slugs"], "old": p["old"], "new": p["new"],
                                       "shape": p["shape"], "tdir": p["tdir"], "counts": st[0], "digest": st[1]})
        marker["root_projects_digest"] = _root_projects_digest(plan["root_new"])
        with open(_v2_marker_path(home), "w") as f:
            json.dump(marker, f, indent=2, sort_keys=True)
    return {"ok": True, "command": "v2", "home": home, "backup": backup, "carried": carried,
            "projects": [{"slug": x["slug"], "shape": x["shape"], "new": x["new"], "counts": x["counts"]}
                         for x in marker["projects"]],
            "root": plan["root_new"], "moved": len(moved)}


def _root_projects_digest(root_path):
    con = open_ro(root_path)
    if con is None:
        return None
    try:
        h = hashlib.sha256()
        for row in con.execute("SELECT slug, data_dir, db_path FROM projects ORDER BY slug"):
            h.update(repr(tuple(row)).encode())
        return h.hexdigest()
    finally:
        con.close()


def cmd_v2_rollback(home, args):
    """Undo --v2: refuse when a NEW project store (or the root's project rows) changed since the switch;
    else put the leftovers back and remove the new files. The old files are not written."""
    mpath = _v2_marker_path(home)
    marker = _read_json(mpath) if os.path.isfile(mpath) else None
    if not marker:
        raise Fail(f"no v2 record at {mpath}: nothing to roll back", 3)
    if os.path.exists(_retire_marker_path(home)):
        raise Fail(f"a v2-retire is recorded ({_retire_marker_path(home)}); run --v2-retire --rollback first", 3)
    lines = [f"remove {p}" for p in marker.get("created", [])]
    lines += [f"restore {dst} -> {src}" for dst, src in marker.get("moved", [])]
    lines += [f"restore {copy} -> {orig} (rewritten by the settings carry)" for copy, orig in marker.get("restore", [])]
    if args.dry_run:
        for line in lines:
            print(line)
        return {"ok": True, "command": "v2-rollback", "dry_run": True, "steps": lines}
    with switch_lock(home):
        for p in marker.get("projects", []):
            st = state_of(p["new"])
            if st is None:
                raise Fail(f"the new store is gone: {p['new']}", 3)
            if st[1] != p["digest"]:
                raise Fail(f"the new store {p['new']} changed after the v2 switch; refusing to roll back (its writes"
                           " would be lost)", 3)
        root_new = mkpaths.new_db_in(home, "root")
        if marker.get("created_root") and os.path.isfile(root_new):
            if _root_projects_digest(root_new) != marker.get("root_projects_digest"):
                raise Fail("the new root's project rows changed after the v2 switch; refusing to roll back", 3)
        for dst, src in marker.get("moved", []):
            if os.path.exists(src):
                raise Fail(f"cannot restore {src}: it exists again (inspect it, then retry)", 3)
        for dst, src in marker.get("moved", []):
            if os.path.isfile(dst):
                os.makedirs(os.path.dirname(src), exist_ok=True)
                shutil.move(dst, src)
        _v2_undo_new(marker.get("created", []))
        _restore_home_files(marker.get("restore", []))
        if marker.get("backup"):
            _prune_backup(marker["backup"])
        for p in marker.get("projects", []):
            if p.get("shape") == "legacy":
                _remove_new_copy(home, p["slug"])  # the C2 folder a pre-migration project did not have before
        old_changed = [p for p, sha in (marker.get("old_sha") or {}).items() if _file_sha(p) != sha]
        os.remove(mpath)
    return {"ok": True, "command": "v2-rollback", "home": home, "removed": marker.get("created", []),
            "restored": len(marker.get("moved", [])), "old_files_changed": old_changed}


# ---------- v2-retire: move the old stores and leftovers into a named backup (L10.Y) ----------
#
# After `--v2` has switched a project, the OLD root/project stores, the stale view files and a few home-level
# leftovers still sit in the live tree and make the layout audit fail. Retire MOVES them (shutil.move on one
# filesystem is a rename; nothing is ever deleted) into <home>/backups/retire-<stamp>/, keeping their relative
# paths, writes a manifest (path, size, sha256) and a marker, and `--v2-retire --rollback` puts every byte back.

RETIRE_MARKER = ".retire-v2.json"
RETIRE_VIEW_NAMES = set(mkpaths.VIEW_DOC_NAMES) | {"index.json"}
TEMP_PREFIXES = ("/private/tmp/", "/tmp/", "/private/var/folders/", "/var/folders/")


def _retire_marker_path(home):
    return os.path.join(home, RETIRE_MARKER)


def _sizes(path):
    """[(rel-to-path, size, sha256)] for a file (rel '') or every file under a folder, symlinks as symlinks."""
    if os.path.isfile(path) or os.path.islink(path):
        return [("", os.lstat(path).st_size, _hash_path(path))]
    out = []
    for d, dirs, files in os.walk(path):
        dirs.sort()
        for n in sorted(files):
            full = os.path.join(d, n)
            out.append((os.path.relpath(full, path), os.lstat(full).st_size, _hash_path(full)))
    return out


def _config_json_carried(home):
    """True when <home>/config.json can go: it holds ONLY keys migrate-settings carries and mk-settings.yaml has
    every one of them with the same value. -> (carried?, reason)."""
    cj = os.path.join(home, "config.json")
    doc = _read_json(cj)
    if not isinstance(doc, dict):
        return False, "config.json is unreadable"
    flat = {}

    def walk(prefix, node):
        for k, v in node.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                walk(key, v)
            else:
                flat[key] = v

    walk("", doc)
    carried_keys = {"dashboard.auto_open", "dashboard.chip", "dashboard.toasts", "usage.poll_min"}
    extra = sorted(k for k in flat if k not in carried_keys)
    if extra:
        return False, f"config.json holds keys migrate-settings does not carry: {extra}"
    have = {}
    try:
        with open(mkpaths.mk_settings(home)) as f:
            for line in f:
                m = re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*):[ \t]*(.*)$", line.rstrip("\n"))
                if m:
                    try:
                        have[m.group(1)] = json.loads(m.group(2))
                    except ValueError:
                        have[m.group(1)] = m.group(2).strip().strip("\"'")
    except OSError:
        return False, "mk-settings.yaml is missing"
    missing = sorted(k for k, v in flat.items() if have.get(k) != v)
    if missing:
        return False, f"mk-settings.yaml lacks or differs on {missing}"
    return True, "carried"


def _lock_held(path):
    """True when another process holds this flock (or it cannot be opened): a held lock is never retired."""
    try:
        f = open(path, "a")
    except OSError:
        return True
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
    return False


def _tokens_name(home, slug):
    """True when any bus token file under the home names this slug (a row with tokens is never junk)."""
    for p in (os.path.join(home, "bus-tokens.json"), os.path.join(home, "bus", "bus-tokens.json")):
        try:
            with open(p) as f:
                if slug in f.read():
                    return True
        except OSError:
            continue
    return False


def _junk_rows(home, root_path, keep):
    """Root `projects` rows that are test debris: no db_path, a temp-dir data_dir that no longer exists, and no bus
    token naming the slug. Rows in `keep` (the alias, hermes, anything with db_path) are never junk."""
    con = open_ro(root_path)
    if con is None:
        return []
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM projects")]
    finally:
        con.close()
    out = []
    for r in rows:
        d = r.get("data_dir") or ""
        if r["slug"] in keep or r.get("db_path") or not d.startswith(TEMP_PREFIXES) or os.path.exists(d):
            continue
        if _tokens_name(home, r["slug"]):
            continue
        out.append(r)
    return out


def _retire_plan(home, slug):
    """What retire would move for this project, and the junk rows it would drop. Refuses unless the switch is
    verified: the v2 marker names the slug, the root row points at the new store, both new stores open."""
    marker = _read_json(_v2_marker_path(home)) if os.path.isfile(_v2_marker_path(home)) else None
    entry = next((p for p in (marker or {}).get("projects", []) if p.get("slug") == slug), None)
    if not entry:
        raise Fail(f"no verified v2 switch recorded for {slug!r} ({_v2_marker_path(home)}); run --v2 first", 3)
    new_proj, new_root = entry["new"], mkpaths.new_db_in(home, "root")
    for label, path in (("root", new_root), ("project", new_proj)):
        if state_of(path) is None:
            raise Fail(f"the new {label} store does not open: {path}", 3)
    row = root_row(home, slug)
    if not row or os.path.realpath(row.get("db_path") or "") != os.path.realpath(new_proj):
        raise Fail(f"projects.db_path of {slug!r} is not the new store; refusing to retire", 3)
    if os.path.exists(_retire_marker_path(home)):
        raise Fail(f"a retire is already recorded ({_retire_marker_path(home)}); --v2-retire --rollback first", 3)
    tdir = os.path.dirname(new_proj)
    items = []      # {"rel": path relative to home, "why": rule}

    def add(path, why):
        if os.path.lexists(path):
            rel = os.path.relpath(path, home)
            if not any(i["rel"] == rel for i in items):
                items.append({"rel": rel, "why": why})

    legacy_root = mkpaths.legacy_db_in(home)
    if os.path.realpath(legacy_root) != os.path.realpath(new_root):
        for suf in ("", "-wal", "-shm"):
            add(legacy_root + suf, "old root store beside the new one")
    legacy_proj = mkpaths.legacy_db_in(tdir)
    if os.path.realpath(legacy_proj) != os.path.realpath(new_proj):
        for suf in ("", "-wal", "-shm"):
            add(legacy_proj + suf, "old project store beside the new one")
    for n in sorted(os.listdir(home)):
        if n.startswith(mkpaths.LEGACY_DB_NAME + ".bak-"):
            add(os.path.join(home, n), "old root store backup")
    # stale run view files under the project's plans/**/runs/ and the pre-C2 projects/<slug>/runs folder
    for d, dirs, files in os.walk(os.path.join(tdir, mkpaths.TARGET_PLANS_DIR)):
        dirs.sort()
        if "runs" in d.split(os.sep):
            for n in sorted(files):
                if n in RETIRE_VIEW_NAMES:
                    add(os.path.join(d, n), "stale run view file")
    add(os.path.join(tdir, "runs"), "old runs folder (pre-C2)")
    # a stray store with no projects row
    rows_all = mkdb.projects(home)
    slugs_all = {r.get("slug") for r in rows_all}
    pdir = os.path.join(home, mkpaths.TARGET_PROJECTS_DIR)
    for n in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        folder = os.path.join(pdir, n)
        owned = n in slugs_all or any((r.get("db_path") or "").startswith(folder + os.sep) for r in rows_all)
        if not owned and os.path.isdir(folder) and any(
                os.path.isfile(os.path.join(folder, x)) for x in (mkpaths.LEGACY_DB_NAME, mkpaths.PROJECT_DB_NAME)):
            add(folder, "project folder with a store and no projects row")
        # stray lock files in a project folder (lanes.lock now lives under <home>/locks)
        if os.path.isdir(folder) and os.path.relpath(folder, home) not in [i["rel"] for i in items]:
            for x in sorted(os.listdir(folder)):
                if re.match(r"^(.+\.yaml\.lock|lanes\.lock)$", x) and os.path.isfile(os.path.join(folder, x)):
                    add(os.path.join(folder, x), "stray lock file in a project folder")
    # home-level leftovers
    notes = []
    if os.path.isfile(os.path.join(home, "config.json")):
        ok_, why_ = _config_json_carried(home)
        if ok_:
            add(os.path.join(home, "config.json"), "retired UI toggles, carried into mk-settings.yaml")
        else:
            notes.append(f"config.json stays: {why_}")
    add(os.path.join(home, mkpaths.PROJECTS_JSON), "retired home-mode registry (the root rows hold it)")
    ldir = os.path.join(home, "locks")
    for n in sorted(os.listdir(ldir)) if os.path.isdir(ldir) else []:
        full = os.path.join(ldir, n)
        if n.endswith(".lock") and os.path.isfile(full) and n != os.path.basename(mkpaths.lock_path("migrate")):
            if _lock_held(full):
                notes.append(f"locks/{n} stays: held by a process")
            else:
                add(full, "lock file no process holds")
    keep = {slug} | set(entry.get("slugs") or []) | {r["slug"] for r in rows_all if r.get("db_path")}
    junk = _junk_rows(home, new_root, keep)
    return {"items": items, "junk": junk, "notes": notes, "tdir": tdir, "new_root": new_root, "entry": entry}


def cmd_v2_retire(args):
    home = mkpaths.home(args.home)
    if args.rollback:
        return cmd_v2_retire_rollback(home, args)
    if not args.slug or "," in args.slug:
        raise Fail("--v2-retire needs exactly one --slug S", 2)
    plan = _retire_plan(home, args.slug)
    lines = [f"move {i['rel']}  ({i['why']})" for i in plan["items"]]
    lines += [f"drop projects row {r['slug']} ({r.get('data_dir')}): temp path gone, no db_path, no token" for r in plan["junk"]]
    if args.dry_run:
        for line in lines + [f"note: {n}" for n in plan["notes"]]:
            print(line)
        return {"ok": True, "command": "v2-retire", "dry_run": True, "home": home, "slug": args.slug,
                "moves": [i["rel"] for i in plan["items"]], "junk_rows": [r["slug"] for r in plan["junk"]],
                "notes": plan["notes"]}
    stamp = now_stamp()
    backup = os.path.join(home, "backups", f"retire-{stamp}")
    manifest = {"slug": args.slug, "stamp": stamp, "backup": backup, "items": [], "rows": plan["junk"]}
    done = []
    with switch_lock(home):
        plan = _retire_plan(home, args.slug)       # re-read under the lock: what is there NOW is what moves
        try:
            for i in plan["items"]:
                src, dst = os.path.join(home, i["rel"]), os.path.join(backup, i["rel"])
                files = _sizes(src)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                done.append((dst, src))
                manifest["items"].append({"rel": i["rel"], "why": i["why"], "files": files,
                                          "is_dir": os.path.isdir(dst) and not os.path.islink(dst)})
            if plan["junk"]:
                con = mkdb.open_root(home)
                try:
                    for r in plan["junk"]:
                        con.execute("DELETE FROM projects WHERE slug=? AND db_path IS NULL", (r["slug"],))
                    con.commit()
                finally:
                    con.close()
            manifest["rows"] = plan["junk"]
            os.makedirs(backup, exist_ok=True)
            with open(os.path.join(backup, "manifest.json"), "w") as f:
                json.dump(manifest, f, indent=2, sort_keys=True, default=str)
            with open(_retire_marker_path(home), "w") as f:
                json.dump({"slug": args.slug, "stamp": stamp, "backup": backup,
                           "manifest": os.path.join(backup, "manifest.json")}, f, indent=2)
        except BaseException as e:
            for dst, src in reversed(done):
                with contextlib.suppress(OSError):
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    shutil.move(dst, src)
            if plan["junk"]:
                _reinsert_rows(home, plan["junk"])
            with contextlib.suppress(OSError):
                os.remove(_retire_marker_path(home))
            _prune_backup(backup)
            if isinstance(e, (Fail, KeyboardInterrupt, SystemExit)):
                raise
            raise Fail(f"v2-retire failed: {type(e).__name__}: {e} — everything was put back", 3)
    return {"ok": True, "command": "v2-retire", "home": home, "slug": args.slug, "backup": backup,
            "moved": len(manifest["items"]), "junk_rows": [r["slug"] for r in plan["junk"]], "notes": plan["notes"]}


def _reinsert_rows(home, rows):
    con = mkdb.open_root(home)
    try:
        for r in rows:
            cols = [c for c in r if c in {x[1] for x in con.execute("PRAGMA table_info(projects)")}]
            con.execute(f"INSERT OR IGNORE INTO projects({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                        [r[c] for c in cols])
        con.commit()
    finally:
        con.close()


def cmd_v2_retire_rollback(home, args):
    """Put every retired byte back. Refuses when a moved file changed in the backup (sha mismatch), when a target
    path is occupied again, or when the manifest is missing; nothing is half-restored on a refusal."""
    mpath = _retire_marker_path(home)
    marker = _read_json(mpath) if os.path.isfile(mpath) else None
    if not marker:
        raise Fail(f"no retire record at {mpath}: nothing to roll back", 3)
    manifest = _read_json(marker["manifest"])
    if not manifest:
        raise Fail(f"the retire manifest is unreadable: {marker['manifest']}", 3)
    backup = manifest["backup"]
    lines = [f"restore {i['rel']}" for i in manifest["items"]] + [f"re-insert projects row {r['slug']}" for r in manifest.get("rows", [])]
    if args.dry_run:
        for line in lines:
            print(line)
        return {"ok": True, "command": "v2-retire-rollback", "dry_run": True, "steps": lines}
    with switch_lock(home):
        for i in manifest["items"]:
            dst, src = os.path.join(backup, i["rel"]), os.path.join(home, i["rel"])
            if not os.path.lexists(dst):
                raise Fail(f"the backup copy is gone: {dst}", 3)
            if os.path.lexists(src):
                raise Fail(f"cannot restore {i['rel']}: the path exists again (inspect it, then retry)", 3)
            now = {(a, b, c) for a, b, c in _sizes(dst)}
            if now != {tuple(f) for f in i["files"]}:
                raise Fail(f"the backup copy of {i['rel']} differs from the manifest; refusing to restore", 3)
        for i in manifest["items"]:
            src = os.path.join(home, i["rel"])
            os.makedirs(os.path.dirname(src), exist_ok=True)
            shutil.move(os.path.join(backup, i["rel"]), src)
        if manifest.get("rows"):
            _reinsert_rows(home, manifest["rows"])
        os.remove(mpath)
        with contextlib.suppress(OSError):
            os.remove(os.path.join(backup, "manifest.json"))
        _prune_backup(backup)
    return {"ok": True, "command": "v2-retire-rollback", "home": home, "restored": len(manifest["items"]),
            "rows": len(manifest.get("rows", []))}


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
    p.add_argument("--v2-retire", dest="v2_retire", action="store_true",
                   help="after --v2: move the old stores and leftovers into backups/retire-<stamp>/ (--slug S; --rollback undoes)")
    p.add_argument("--v2", action="store_true", help="move onto the v2 names (mk-overview.db, mk-project.db); --slug A[,B]")
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
        if args.v2_retire:
            out = cmd_v2_retire(args)
        elif args.v2:
            out = cmd_v2(args)
        elif args.archive:
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


SYNTHETIC_PLAN = """+++ 1 Top block — plan identity [toml]
plan = "fixture"
status = "running"
created = 2026-10-14
runs = ["fixture-r1"]
integration = "main"
/+++

+++ 2 Summary
# fixture — the plan the migrate self-test copies

## Summary
- Phases: 1 · P1: one phase
- Method: single lane · subagents off
- Lanes: P1 (own lane)
- Delivery: main · pushed: no
- Gates: test
/+++

+++ 3 Goal
## Goal
- Done when:
  - G1 — the plan's files are copied to the C2 tree
- Why: the self-test needs a plan file of the shipped shape
- From: the self-test
- Sources: none
/+++

+++ 5 Phases
## Phases
/+++

+++ 5.1 P1 — one phase
### P1 — one phase
- Goal: one task exists
- Serves: G1
- Needs first: nothing
- Build order: P1.1
- Hazards: none
- Carries: none
/+++

+++ 5.1.0 P1 settings [toml]
id = "P1"
title = "one phase"
wave = 1
owned = []
commit = "feat: p1"
subagents = false
/+++

+++ 5.1.1 P1.1 — one task [toml]
id = "P1.1"
title = "one task"
acceptance = ["it works"]
risk = 1
/+++
""".encode("utf-8")


Z15_PLAN = """+++ 1 Top block — plan identity [toml]
plan = "z15plan"
status = "draft"
created = 2026-10-15
runs = []
integration = "main"
/+++

+++ 2 Summary
# z15plan — the live plan of the Z15 case

## Summary
- Phases: 1 — P1: one phase
- Method: single lane · subagents off
- Lanes: P1 (own lane)
- Delivery: main · pushed: no
- Gates: test
/+++

+++ 3 Goal
## Goal
- Done when:
  - G1 — the plan folder holds plan.md, assets/ and archive/ after the switch
- Why: the Z15 case runs a live plan through the switch
- From: the self-test
- Sources: `z15-research.md`
/+++

+++ 4 Design / context
## Design / context
### What is wrong today
1. none, this is new
### Shape of the change
One task; nothing else changes.
### Contracts
none
### Test plan
T1 — the switch carries the plan folder — integration — `scripts/mk-migrate.py --self-test` — written by P1.1
### Assumptions and dependencies
none
### Rollout and undo
R1 — ships by merge; undo = revert the merge — P1.1
### Settings and limits
S1 — most lanes at once — 3 — mk-config.py list --json — 2026-10-15
### Out of scope
Nothing.
/+++

+++ 5 Phases
## Phases
/+++

+++ 5.1 P1 — one phase
### P1 — one phase
- Goal: one task exists and is done
- Serves: G1
- Needs first: nothing
- Build order: P1.1
- Hazards: none
- Carries: T1, R1
/+++

+++ 5.1.0 P1 settings [toml]
id = "P1"
title = "one phase"
wave = 1
owned = []
commit = "feat: p1"
subagents = false
/+++

+++ 5.1.1 P1.1 — one task [toml]
id = "P1.1"
title = "one task"
covers = ["T1", "R1"]
acceptance = ["it works"]
risk = 2
/+++

+++ 6 Gates
## Gates
- test — proves G1 — the test gate passes
/+++

+++ 6.0 Gate settings [toml]
use = ["test"]
/+++

+++ 7 Risks
## Risks
/+++

+++ 8 Open questions
## Open questions
/+++

+++ 9 Log
## Log
- 2026-10-15 — drafted — the live plan of the Z15 case — user
/+++
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
                       ("review-r1.md", b"# review\n"), (mkpaths.TASK_DOC_NAME, b'{"id":"P1.1"}')):
        with open(os.path.join(td, name), "wb") as f:
            f.write(body)
    with open(mkpaths.legacy_cursors(data), "w") as f:
        json.dump({"r1|*": {"seq": 1, "started": None}}, f)
    with open(mkpaths.legacy_yaml(data), "w") as f:
        f.write("data_dir: .matrixkoder/\n")
    return plan_bytes


class _as_home:
    """Point this process's MK home at `home` for a block: mkpaths picks mk-overview.db for a fresh
    ROOT only when the folder IS the process's home, so a second temp home read in-process needs it."""
    def __init__(self, home):
        self.home = home

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in ("MK_HOME", "MATRIXKODER_HOME")}
        os.environ.update({"MK_HOME": self.home, "MATRIXKODER_HOME": self.home})

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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


V2_YAML = ("# my own note\nsession:\n  stall_min: 17\nmystery_key: keep-me\nbanned_commands: [\"git push --force\", \"rm -rf\"]\n"
           "lanes_disposable: [\".omo/\"]\n")     # keys migrate-settings does NOT carry: the yaml must stay live
V2_CONFIG_JSON = {"dashboard": {"chip": True}, "usage": {"poll_min": 7}}   # the retired UI toggles -> mk-settings.yaml
V2_PRE_SETTINGS = "lanes.default_agent: claude\n"                         # a mk-settings.yaml that already exists
V2_NO_FILL = ("settings", "schema_version")      # tables the source fixture leaves empty on purpose


def _v2_fill(con):
    """One row in every project-store table a source holds (direct INSERTs through an open mkdb connection)."""
    con.execute("INSERT INTO plans(id, version, title, status, header_json) VALUES ('pa', 1, 'A', 'running', '{}')")
    con.execute("INSERT INTO runs(id, plan, status, started) VALUES ('ra', 'pa', 'active', 't0')")
    con.execute("INSERT INTO tasks(run, id, type, title, status) VALUES ('ra', 'T1', 'task', 'one', 'todo')")
    con.execute("INSERT INTO events(run, task, t, chan, kind, from_id, to_id, text)"
                " VALUES ('ra', 'T1', 't1', 'bus', 'info', 'orch', 'T1', 'hello')")
    con.execute("INSERT INTO artifacts(run, task, round, kind, rev, text, created)"
                " VALUES ('ra', 'T1', 1, 'brief', 1, '# brief', 't2')")
    con.execute("INSERT INTO acks(run, msg, lane, level, t) VALUES ('ra', 'm-1', 'L1', 1, 't3')")
    con.execute("INSERT OR REPLACE INTO change_log(change_no, run, task, t) VALUES (900, 'ra', 'T1', 't4')")
    con.execute("INSERT INTO waits(run, question, opened) VALUES ('ra', 'why?', 't5')")
    con.execute("INSERT OR REPLACE INTO lane_slots(slot, lane_path, state, run, task) VALUES ('1', '/x/lane1', 'free', 'ra', 'T1')")
    con.execute("INSERT OR REPLACE INTO event_src(run, src) VALUES ('ra', 'src-1')")
    for t in ("evidence", "risk_reasons", "reconcile_due", "watch_cursors"):
        if con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone():
            if t == "evidence":
                con.execute("INSERT INTO evidence(run, task, line, gate, result) VALUES ('ra', 'T1', 'l', 'g', 'pass')")
            elif t == "risk_reasons":
                con.execute("INSERT INTO risk_reasons(run, task, kind, points, opened) VALUES ('ra', 'T1', 'k', 1, 't6')")
            elif t == "reconcile_due":
                con.execute("INSERT INTO reconcile_due(run, scope, created) VALUES ('ra', 's', 't7')")
            else:
                con.execute("INSERT INTO watch_cursors(key, seq, started, updated) VALUES ('ra|*', 3, NULL, NULL)")
    con.commit()


def _v2_rename_to_legacy(path_new, folder):
    """Give a closed store the OLD file name (a closed WAL store leaves no sidecar; drop strays)."""
    os.replace(path_new, mkpaths.legacy_db_in(folder))
    for suffix in ("-wal", "-shm"):
        with contextlib.suppress(OSError):
            os.remove(path_new + suffix)


def _v2_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _v2_fixture_c2(tmp):
    """Shape 1: a v1 C2 home. Root and project stores under the LEGACY name, root row has db_path set,
    leftovers in the project folder, a frozen copy in <repo>/.matrixkoder."""
    home, repo = os.path.join(tmp, "home"), os.path.join(tmp, "repo")
    data, slug = os.path.join(repo, ".matrixkoder"), "v2c2"
    tdir = os.path.join(home, "projects", slug)
    os.makedirs(data)
    os.makedirs(tdir)
    with _as_home(home):
        root = mkdb.open_root(home)
        root.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, created, last_activity, db_path)"
                     " VALUES (?,?,?,?,?,?,?,?)", (slug, slug, repo, os.path.realpath(data), "home", "c", "c",
                                                  mkpaths.legacy_db_in(tdir)))
        root.execute("INSERT INTO plan_summary(project, plan, title, status, counts_json, live_run, updated)"
                     " VALUES (?, 'pa', 'A', 'running', '{}', 'ra', 'u')", (slug,))
        root.commit()
        root.close()
        con = mkdb.open_project(home, slug)
        _v2_fill(con)
        con.close()
        newp = mkpaths.new_db_in(tdir, "project")
        _v2_rename_to_legacy(newp, tdir)
        _v2_rename_to_legacy(mkpaths.new_db_in(home, "root"), home)
    shutil.copyfile(mkpaths.legacy_db_in(tdir), mkpaths.legacy_db_in(data))      # the frozen pre-switch copy
    _v2_write(mkpaths.legacy_yaml(data), V2_YAML)
    _v2_write(mkpaths.legacy_lanes(tdir), json.dumps({"slots": {"1": {"path": "/x/lane1", "state": "free"},
                                                              "2": {"path": "/x/lane2", "state": "free", "branch": "b2"}}}))
    _v2_write(mkpaths.legacy_cursors(tdir), json.dumps({"ra|*": {"seq": 3}, "rb|T9": {"seq": 8}}))
    _v2_write(os.path.join(home, "config.json"), json.dumps(V2_CONFIG_JSON))     # the carry CREATES mk-settings.yaml
    _v2_write(mkpaths.project_yaml_in(tdir), V2_YAML)
    _v2_write(mkpaths.project_yaml_in(tdir) + ".bak", "# older\n")
    _v2_write(mkpaths.project_yaml_in(tdir) + ".lock", "")
    _v2_write(mkpaths.legacy_yaml(tdir) + ".lock", "")
    _v2_write(os.path.join(tdir, "lanes.lock"), "")
    return {"home": home, "repo": repo, "data": data, "slug": slug, "tdir": tdir, "shape": "c2",
            "old_proj": mkpaths.legacy_db_in(tdir), "old_root": mkpaths.legacy_db_in(home),
            "leftovers": [mkpaths.LANES_NAME, "lanes.lock", mkpaths.CURSORS_NAME,
                          mkpaths.PROJECT_YAML_NAME + ".lock", mkpaths.LEGACY_YAML_NAME + ".lock"],
            "stay": [mkpaths.PROJECT_YAML_NAME, mkpaths.PROJECT_YAML_NAME + ".bak"], "settings_before": None,
            "lane": "2", "cursor": "rb|T9"}


def _v2_fixture_legacy(tmp):
    """Shape 2: a pre-migration project. Schema-4 store `<repo>/.matrixkoder/<legacy name>`, matrixkoder.yaml,
    lanes.json and watch-cursors.json beside it, root row with db_path NULL, root store under the LEGACY name."""
    home, repo = os.path.join(tmp, "home"), os.path.join(tmp, "repo")
    data, slug = os.path.join(repo, ".matrixkoder"), "v2leg"
    os.makedirs(data)
    with _as_home(home):
        root = mkdb.open_root(home)
        root.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, created, last_activity)"
                     " VALUES (?,?,?,?,?,?,?)", (slug, slug, repo, os.path.realpath(data), "home", "c", "c"))
        root.commit()
        root.close()
        con = mkdb.open_project(home, slug, data_dir=data, version=4)
        _v2_fill(con)
        con.close()
        _v2_rename_to_legacy(mkpaths.new_db_in(data, "project"), data)
        _v2_rename_to_legacy(mkpaths.new_db_in(home, "root"), home)
    _v2_write(mkpaths.legacy_yaml(data), V2_YAML)
    _v2_write(mkpaths.legacy_lanes(data), json.dumps({"slots": {"1": {"path": "/x/lane1", "state": "free"},
                                                              "2": {"path": "/x/lane2", "state": "free", "branch": "b2"}}}))
    _v2_write(mkpaths.legacy_cursors(data), json.dumps({"ra|*": {"seq": 3}, "rb|T9": {"seq": 8}}))
    _v2_write(os.path.join(home, "config.json"), json.dumps(V2_CONFIG_JSON))
    _v2_write(mkpaths.mk_settings(home), V2_PRE_SETTINGS)    # the carry REWRITES an existing mk-settings.yaml
    return {"home": home, "repo": repo, "data": data, "slug": slug, "tdir": os.path.join(home, "projects", slug),
            "shape": "legacy", "old_proj": mkpaths.legacy_db_in(data), "old_root": mkpaths.legacy_db_in(home),
            "leftovers": [mkpaths.CURSORS_NAME], "stay": [mkpaths.PROJECT_YAML_NAME],
            "settings_before": V2_PRE_SETTINGS, "lane": None, "cursor": "rb|T9"}


def _v2_hash(*roots):
    """_tree_hash over folders, minus the migrator's own lock file (created by any locked run, never removed) and the empty WAL sidecars a read leaves."""
    for r in roots:
        with contextlib.suppress(OSError):
            os.remove(os.path.join(r, "locks", "migrate.lock"))
        # a read-only open of a WAL store leaves an EMPTY -wal and a -shm beside it (SQLite, not a write): drop those
        for d, _dirs, files in os.walk(r):
            for n in files:
                if n.endswith("-wal") and os.path.getsize(os.path.join(d, n)) == 0:
                    for suffix in ("-wal", "-shm"):
                        with contextlib.suppress(OSError):
                            os.remove(os.path.join(d, n[:-4] + suffix))
    return [_tree_hash(r) for r in roots]


def _v2_args(fx, *extra):
    return ["--v2", "--home", fx["home"], *extra]


def _v2_rehearse(fx):
    home, repo, slug, tdir = fx["home"], fx["repo"], fx["slug"], fx["tdir"]
    tag = f"v2-{fx['shape']}"
    start = _v2_hash(home, repo)
    # (1) dry run: prints the steps, changes nothing
    rc, out, err = _cli(home, _v2_args(fx, "--slug", slug, "--dry-run"))
    _check(rc == 0 and "copy" in out and "carry settings" in out, f"{tag} dry-run rc={rc} {out[-300:]} {err[-300:]}")
    _check(_v2_hash(home, repo) == start, f"{tag}: the dry run changed the tree")
    old_sha = {p: _hash_file(p) for p in (fx["old_proj"], fx["old_root"])}
    # the leftovers' bytes: the c2 ones in the project folder, the legacy ones in the data dir (the copy puts them in tdir)
    src_folder = tdir if fx["shape"] == "c2" else fx["data"]
    orig = {}
    for n in fx["leftovers"]:
        with open(os.path.join(src_folder, n), "rb") as f:
            orig[n] = f.read()
    # the live config: project.yaml (c2: in the C2 folder; legacy: the repo's matrixkoder.yaml becomes it) must
    # survive with its bytes, and a key migrate-settings does not carry must read the same value before and after
    stay_src = {n: os.path.join(src_folder, mkpaths.LEGACY_YAML_NAME if (fx["shape"] == "legacy" and n == mkpaths.PROJECT_YAML_NAME) else n)
                for n in fx["stay"]}
    stay_body = {}
    for n, path in stay_src.items():
        with open(path, "rb") as f:
            stay_body[n] = f.read()
    banned_before = [ln for ln in stay_body[mkpaths.PROJECT_YAML_NAME].decode().splitlines() if ln.startswith("banned_commands")]
    _check(len(banned_before) == 1, f"{tag}: fixture has no banned_commands line")
    # (2) apply
    rc, out, err = _cli(home, _v2_args(fx, "--slug", slug))
    _check(rc == 0, f"{tag} apply rc={rc} {out[-500:]} {err[-300:]}")
    new_proj, new_root = mkpaths.new_db_in(tdir, "project"), mkpaths.new_db_in(home, "root")
    _check(os.path.isfile(new_proj) and os.path.isfile(new_root), f"{tag}: new stores missing")
    for p, sha in old_sha.items():
        _check(_hash_file(p) == sha, f"{tag}: old store {p} changed")
    with _as_home(home):
        _check(mkpaths.root_db(home) == new_root, f"{tag}: root_db is {mkpaths.root_db(home)}")
        _check(mkpaths.project_db(slug=slug) == new_proj, f"{tag}: project_db is {mkpaths.project_db(slug=slug)}")
    bad = _v2_compare(fx["old_proj"], new_proj)
    # lane_slots, watch_cursors and settings legitimately gain the carried rows; every other table is equal
    # a legacy store that already holds lane rows wins over lanes.json (do_copy_verify's rule): no slot is carried
    carried_tables = ["lane_slots", "settings", "watch_cursors"] if fx["lane"] else ["settings", "watch_cursors"]
    _check(sorted(x.split(":")[0] for x in bad) == carried_tables and all("rows in the source" in x for x in bad),
           f"{tag}: compare problems {bad}")
    old_n = _table_sums(fx["old_proj"])
    new_n = _table_sums(new_proj)
    for t, (n, _d, _c) in old_n.items():
        _check(new_n[t][0] == n + (1 if t in carried_tables else 0), f"{tag}: {t} rows {n} -> {new_n[t][0]}")
    for t in ("runs", "tasks", "events", "artifacts", "acks", "change_log", "plans", "waits", "event_src"):
        _check(new_n[t][0] >= 1, f"{tag}: {t} empty in the copy")
    rbad = [x for x in _v2_compare(fx["old_root"], new_root) if not x.startswith("projects:")]
    _check(rbad == [], f"{tag}: root compare {rbad}")
    con = open_ro(new_proj)
    try:
        _check(con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == mkdb.SCHEMA, f"{tag}: schema")
        got = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}
        _check(any(k.endswith("stall_min") and json.loads(v) == 17 for k, v in got.items()), f"{tag}: settings {got}")
        _check(fx["lane"] is None or con.execute("SELECT 1 FROM lane_slots WHERE slot=?", (fx["lane"],)).fetchone(),
               f"{tag}: lane not carried")
        _check(con.execute("SELECT 1 FROM watch_cursors WHERE key=?", (fx["cursor"],)).fetchone(), f"{tag}: cursor not carried")
    finally:
        con.close()
    rcon = open_ro(new_root)
    try:
        paths = {r["db_path"] for r in rcon.execute("SELECT db_path FROM projects WHERE data_dir=?",
                                                    (os.path.realpath(fx["data"]),))}
    finally:
        rcon.close()
    _check(paths == {new_proj}, f"{tag}: root db_path {paths}")
    # project.yaml and its .bak STAY in the C2 folder (live config); banned_commands reads the same through mk-config
    for n, body in stay_body.items():
        with open(os.path.join(tdir, n), "rb") as f:
            _check(f.read() == body, f"{tag}: {n} in the project folder differs from the source")
    with _as_home(home):
        yaml_now = mkpaths.project_yaml(fx["data"])
    _check(yaml_now == os.path.join(tdir, mkpaths.PROJECT_YAML_NAME), f"{tag}: project_yaml resolves to {yaml_now}")
    listed = subprocess.run([sys.executable, os.path.join(HERE, "mk-config.py"), "--data", fx["data"], "list", "--json"],
                            capture_output=True, text=True, env=dict(os.environ, MK_HOME=home, MATRIXKODER_HOME=home, MK_SELFTEST="1"))
    _check(listed.returncode == 0 and json.loads(listed.stdout.strip().splitlines()[-1])["file"] == yaml_now,
           f"{tag}: mk-config reads {listed.stdout[-200:]} {listed.stderr[-200:]}")
    with open(yaml_now) as f:
        banned_after = [ln for ln in f.read().splitlines() if ln.startswith("banned_commands")]
    _check(banned_after == banned_before, f"{tag}: banned_commands {banned_before} -> {banned_after}")
    # <home>/mk-settings.yaml: created by the carry (c2) or rewritten (legacy); config.json is left in place
    ms = mkpaths.mk_settings(home)
    _check(os.path.isfile(ms) and "dashboard.chip" in open(ms).read(), f"{tag}: mk-settings.yaml not written by the carry")
    _check(os.path.isfile(os.path.join(home, "config.json")), f"{tag}: config.json removed")
    bdirs = [os.path.join(home, "backups", d, slug) for d in sorted(os.listdir(os.path.join(home, "backups")))
             if d.startswith("migrate-") and os.path.isdir(os.path.join(home, "backups", d, slug))]
    _check(len(bdirs) == 1, f"{tag}: backup dirs {bdirs}")
    for n, body in orig.items():
        _check(not os.path.exists(os.path.join(tdir, n)), f"{tag}: leftover {n} still in the project folder")
        with open(os.path.join(bdirs[0], n), "rb") as f:
            _check(f.read() == body, f"{tag}: backup {n} differs")
    # (3) a second --v2 refuses
    rc, out, _ = _cli(home, _v2_args(fx, "--slug", slug))
    _check(rc == 3 and os.path.isfile(_v2_marker_path(home)), f"{tag}: second v2 rc={rc} {out[-200:]}")
    # (4) rollback dry run changes nothing
    mid = _v2_hash(home, repo)
    rc, out, _ = _cli(home, _v2_args(fx, "--rollback", "--dry-run"))
    _check(rc == 0 and "remove" in out and _v2_hash(home, repo) == mid, f"{tag}: rollback dry-run rc={rc}")
    # (5) rollback: byte-identical to the start
    rc, out, err = _cli(home, _v2_args(fx, "--rollback"))
    _check(rc == 0, f"{tag}: rollback rc={rc} {out[-400:]} {err[-300:]}")
    _check(not os.path.exists(new_proj) and not os.path.exists(new_root), f"{tag}: new files survive rollback")
    _check(_v2_hash(home, repo) == start, f"{tag}: the rollback did not restore the tree")
    if fx["settings_before"] is None:
        _check(not os.path.exists(mkpaths.mk_settings(home)), f"{tag}: rollback left a created mk-settings.yaml")
    else:
        with open(mkpaths.mk_settings(home)) as f:
            _check(f.read() == fx["settings_before"], f"{tag}: rollback did not restore the old mk-settings.yaml")
    # (6) re-apply, write to the NEW store, rollback refuses and removes nothing
    rc, out, err = _cli(home, _v2_args(fx, "--slug", slug))
    _check(rc == 0, f"{tag}: re-apply rc={rc} {out[-300:]}")
    with _as_home(home):
        con = mkdb.open_project(home, slug)
        con.execute("INSERT INTO acks(run, msg, lane, level, t) VALUES ('ra', 'm-late', 'L1', 1, 'late')")
        con.commit()
        con.close()
    rc, out, _ = _cli(home, _v2_args(fx, "--rollback"))
    _check(rc == 3 and "changed after" in out, f"{tag}: rollback after a write rc={rc} {out[-200:]}")
    _check(os.path.isfile(new_proj) and os.path.isfile(new_root) and os.path.isfile(_v2_marker_path(home)),
           f"{tag}: the refused rollback removed something")
    return tag


RETIRE_SLUG = "matrixkoder"
RETIRE_JUNK = ("zz-junk-1", "zz-junk-2", "zz-junk-3")


def _retire_fixture(tmp, config=None):
    """ONE home mirroring the real layout: `matrixkoder` (shape c2, v1 names, plus its alias row and alias folder) and
    `hermes` (shape legacy, db_path NULL, store + yaml in its repo), root debris and a frozen repo copy."""
    home, repo, repo2 = os.path.join(tmp, "home"), os.path.join(tmp, "repo"), os.path.join(tmp, "repo2")
    data, data2, slug = os.path.join(repo, ".matrixkoder"), os.path.join(repo2, ".matrixkoder"), RETIRE_SLUG
    tdir, keepdir = os.path.join(home, "projects", slug), os.path.join(tmp, "keepdir")
    alias = alias_slug(data)
    for d in (data, data2, tdir, keepdir):
        os.makedirs(d)
    with _as_home(home):
        root = mkdb.open_root(home)
        ins = ("INSERT INTO projects(slug, name, repo, data_dir, mode, created, last_activity, db_path)"
               " VALUES (?,?,?,?,?,?,?,?)")
        proj_db = mkpaths.legacy_db_in(tdir)
        root.execute(ins, (slug, slug, repo, os.path.realpath(data), "home", "c", "c", proj_db))
        root.execute(ins, (alias, alias, repo, os.path.realpath(data), "managed", "c", "c", proj_db))
        root.execute(ins, ("hermes", "hermes", repo2, os.path.realpath(data2), "home", "c", "c", None))
        for n, d in [(j, f"/private/tmp/{j}-gone") for j in RETIRE_JUNK] + [("zz-keep", keepdir)]:
            root.execute(ins, (n, n, d, d, "home", "c", "c", None))
        root.commit()
        con = mkdb.open_project(home, slug)
        _v2_fill(con)
        for pid, title, status, counts_json, live_run in mkdb._plan_summary_values(con):
            root.execute("INSERT INTO plan_summary(project, plan, title, status, counts_json, live_run, updated)"
                         " VALUES (?,?,?,?,?,?,?)", (slug, pid, title, status, counts_json, live_run, "u"))
        root.commit()
        root.close()
        con.close()
        _v2_rename_to_legacy(mkpaths.new_db_in(tdir, "project"), tdir)
        h = mkdb.open_project(home, "hermes", data_dir=data2, version=4)
        _v2_fill(h)
        h.close()
        _v2_rename_to_legacy(mkpaths.new_db_in(data2, "project"), data2)
        _v2_rename_to_legacy(mkpaths.new_db_in(home, "root"), home)
    shutil.copyfile(mkpaths.legacy_db_in(tdir), mkpaths.legacy_db_in(data))      # the frozen pre-switch copy
    _v2_write(mkpaths.legacy_yaml(data), V2_YAML)
    _v2_write(mkpaths.legacy_yaml(data2), V2_YAML)
    for n, body in ((mkpaths.legacy_lanes(tdir), json.dumps({"slots": {"1": {"path": "/x/lane1", "state": "free"}}})),
                    (mkpaths.legacy_cursors(tdir), json.dumps({"ra|*": {"seq": 3}})),
                    (os.path.join(tdir, "lanes.lock"), ""), (mkpaths.project_yaml_in(tdir), V2_YAML),
                    (mkpaths.project_yaml_in(tdir) + ".bak", "# older\n"), (mkpaths.project_yaml_in(tdir) + ".lock", ""),
                    (mkpaths.legacy_yaml(tdir) + ".lock", "")):
        _v2_write(n, body)
    # stale run view files, and the pre-C2 runs folder with its flock and one brief
    for n in (mkpaths.TASK_DOC_NAME, mkpaths.BOARD_DOC_NAME, mkpaths.EVENTS_DOC_NAME, mkpaths.RUN_DOC_NAME):
        _v2_write(os.path.join(tdir, "plans", "pa", "runs", "ra", n), f"stale {n}\n")
    _v2_write(os.path.join(tdir, "runs", ".lock"), "")
    _v2_write(os.path.join(tdir, "runs", "ra", "T1", "brief-r1.md"), "# old brief\n")
    # the alias folder: only config
    _v2_write(mkpaths.project_yaml_in(os.path.join(home, "projects", alias)), V2_YAML)
    _v2_write(mkpaths.project_yaml_in(os.path.join(home, "projects", alias)) + ".bak", "# alias older\n")
    _v2_write(mkpaths.legacy_yaml(os.path.join(home, "projects", alias)) + ".lock", "")
    # a stray store with no projects row, home leftovers, locks
    _v2_write(mkpaths.legacy_db_in(os.path.join(home, "projects", "e2e")), "stray store")
    _v2_write(os.path.join(home, mkpaths.LEGACY_DB_NAME + ".bak-20261013"), "bak")
    _v2_write(os.path.join(home, "config.json"), json.dumps(config or V2_CONFIG_JSON))
    _v2_write(os.path.join(home, mkpaths.PROJECTS_JSON), "{}")
    _v2_write(os.path.join(home, "locks", "x.lock"), "")
    # the old root bus files carry-root moves: one token naming the project, one lane token file
    _v2_write(os.path.join(home, "bus-tokens.json"), json.dumps({"tk1": {"project": slug, "token": "x", "role": "orch"}}))
    _v2_write(os.path.join(home, "bus-lanes", hashlib.sha256(b"lane1").hexdigest() + ".json"),
              json.dumps({"v": 1, "token": "x", "data": data}))
    return {"home": home, "repo": repo, "repo2": repo2, "data": data, "data2": data2, "slug": slug, "alias": alias,
            "tdir": tdir, "keepdir": keepdir, "shape": "retire"}


def _row_of(root_path, slug):
    con = open_ro(root_path)
    try:
        r = con.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def _slugs_of(root_path):
    con = open_ro(root_path)
    try:
        return {r["slug"]: dict(r) for r in con.execute("SELECT * FROM projects")}
    finally:
        con.close()


def _hash_skip(home, repo, repo2, skip):
    """_v2_hash-clean trees of home/repo/repo2, hashed again minus every home-relative path starting with a `skip` entry."""
    _v2_hash(home, repo, repo2)
    out = []
    for root in (home, repo, repo2):
        h = hashlib.sha256()
        for d, dirs, files in os.walk(root):
            dirs.sort()
            for n in sorted(files):
                p = os.path.join(d, n)
                rel = os.path.relpath(p, root)
                if root == home and any(rel.startswith(s) for s in skip):
                    continue
                h.update(rel.encode() + _hash_path(p).encode())
        out.append(h.hexdigest())
    return out


def _retire_rehearse(fx):
    home, repo, repo2, slug, alias, tdir, data = (fx[k] for k in ("home", "repo", "repo2", "slug", "alias", "tdir", "data"))
    tag = "v2-retire"
    new_root, new_proj = mkpaths.new_db_in(home, "root"), mkpaths.new_db_in(tdir, "project")
    old_root, old_proj = mkpaths.legacy_db_in(home), mkpaths.legacy_db_in(tdir)

    def retire(*a):
        return _cli(home, ["--v2-retire", "--home", home, *a])

    def doc(out):
        return _last_json(out) or {}

    start = _v2_hash(home, repo, repo2)
    # carry-root's moves and the archive tarball are not undone by --v2 --rollback: compare the rest
    v2_skip = ("backups", "bus", "mk-settings.yaml", os.path.join("projects", slug, "legacy"))
    start_skip = _hash_skip(home, repo, repo2, v2_skip)
    old_sha = {p: _hash_file(p) for p in (old_proj, old_root)}
    # (0) retire before --v2 refuses, nothing moves
    rc, out, _ = retire("--slug", slug)
    _check(rc == 3 and "no verified v2 switch" in out and _v2_hash(home, repo, repo2) == start, f"{tag}: retire before v2 rc={rc} {out[-200:]}")
    hermes_hash, hermes_row = _v2_hash(repo2)[0], _row_of(old_root, "hermes")
    _check(hermes_row and hermes_row["db_path"] is None, f"{tag}: hermes fixture row {hermes_row}")
    # (1) --v2 --slug matrixkoder (the alias row moves with it); hermes untouched
    rc, out, err = _cli(home, ["--v2", "--home", home, "--slug", slug])
    _check(rc == 0, f"{tag}: v2 rc={rc} {out[-500:]} {err[-300:]}")
    rows = _slugs_of(new_root)
    _check(rows[slug]["db_path"] == new_proj and rows[alias]["db_path"] == new_proj, f"{tag}: db_path after v2 {rows[slug]['db_path']} {rows[alias]['db_path']}")
    _check({k: rows["hermes"][k] for k in hermes_row} == hermes_row, f"{tag}: the hermes row changed by v2")
    # (2) carry-root as the real subprocess, on the temp home
    env = dict(os.environ, MK_HOME=home, MATRIXKODER_HOME=home, MK_SELFTEST="1")
    env.pop("MK_BUS_DIR", None)
    p = subprocess.run([sys.executable, os.path.join(HERE, "mk-bus.py"), "carry-root"], capture_output=True, text=True, env=env)
    cdoc = _last_json(p.stdout) or {}
    _check(p.returncode == 0 and cdoc.get("tokens", {}).get("carried") == 1 and cdoc.get("lanes", {}).get("carried") == 1,
           f"{tag}: carry-root rc={p.returncode} {p.stdout[-300:]} {p.stderr[-200:]}")
    _check(not os.path.exists(os.path.join(home, "bus-tokens.json")) and not os.path.exists(os.path.join(home, mkpaths.BUS_LANES_DIR)),
           f"{tag}: carry-root left the root bus files")
    # what the migrated tree gets back later: a tool recreates a stray yaml lock in the project folder
    _v2_write(os.path.join(tdir, "other.yaml.lock"), "")
    want = [os.path.relpath(old_root, home), os.path.relpath(old_proj, home), mkpaths.LEGACY_DB_NAME + ".bak-20261013",
            "config.json", mkpaths.PROJECTS_JSON, os.path.join("locks", "x.lock"), os.path.join("projects", "e2e"),
            os.path.join("projects", slug, "runs"), os.path.join("projects", slug, "other.yaml.lock"),
            os.path.join("projects", alias, mkpaths.LEGACY_YAML_NAME + ".lock")]
    want += [os.path.join("projects", slug, "plans", "pa", "runs", "ra", n)
             for n in (mkpaths.TASK_DOC_NAME, mkpaths.BOARD_DOC_NAME, mkpaths.EVENTS_DOC_NAME, mkpaths.RUN_DOC_NAME)]
    pre = {rel: _sizes(os.path.join(home, rel)) for rel in want}
    live_stay = [os.path.join("projects", slug, mkpaths.PROJECT_YAML_NAME), os.path.join("projects", slug, mkpaths.PROJECT_YAML_NAME + ".bak"),
                 os.path.join("projects", alias, mkpaths.PROJECT_YAML_NAME), os.path.join("projects", alias, mkpaths.PROJECT_YAML_NAME + ".bak")]
    stay_pre = {rel: _hash_file(os.path.join(home, rel)) for rel in live_stay}
    before = _v2_hash(home, repo, repo2)
    skip = (os.path.relpath(new_root, home), os.path.join("projects", slug, "legacy"))
    before_skip, rows_pre = _hash_skip(home, repo, repo2, skip), _slugs_of(new_root)
    # (3) dry run: changes nothing, lists the items
    rc, out, err = retire("--slug", slug, "--dry-run")
    d = doc(out)
    _check(rc == 0 and d.get("dry_run") and sorted(d["moves"]) == sorted(want), f"{tag}: dry-run rc={rc} {sorted(set(d.get('moves', [])) ^ set(want))} {err[-200:]}")
    _check(sorted(d["junk_rows"]) == sorted(RETIRE_JUNK), f"{tag}: dry-run junk rows {d['junk_rows']}")
    _check(_v2_hash(home, repo, repo2) == before, f"{tag}: the dry run changed the tree")
    _check(not os.path.exists(os.path.join(home, RETIRE_MARKER)), f"{tag}: the dry run wrote a marker")
    # (4) apply
    rc, out, err = retire("--slug", slug)
    d = doc(out)
    _check(rc == 0 and d.get("moved") == len(want), f"{tag}: retire rc={rc} {out[-400:]} {err[-300:]}")
    backup = d["backup"]
    _check(os.path.dirname(backup) == os.path.join(home, "backups") and os.path.basename(backup).startswith("retire-"), f"{tag}: backup {backup}")
    with open(os.path.join(backup, "manifest.json")) as f:
        man = json.load(f)
    mani = {i["rel"]: [tuple(x) for x in i["files"]] for i in man["items"]}
    for rel in want:
        _check(not os.path.lexists(os.path.join(home, rel)), f"{tag}: {rel} is still in the live tree")
        _check(_sizes(os.path.join(backup, rel)) == pre[rel] and mani.get(rel) == pre[rel], f"{tag}: backup/manifest of {rel} differ from the original")
    _check(_hash_file(os.path.join(backup, os.path.relpath(old_root, home))) == old_sha[old_root]
           and _hash_file(os.path.join(backup, os.path.relpath(old_proj, home))) == old_sha[old_proj], f"{tag}: old stores' sha256")
    _check(os.path.isfile(new_root) and os.path.isfile(new_proj) and os.path.isfile(os.path.join(home, RETIRE_MARKER)), f"{tag}: new stores or marker missing")
    for rel, sha in stay_pre.items():
        _check(os.path.isfile(os.path.join(home, rel)) and _hash_file(os.path.join(home, rel)) == sha, f"{tag}: {rel} did not stay")
    rows = _slugs_of(new_root)
    _check(not set(RETIRE_JUNK) & set(rows) and {"zz-keep", "hermes", slug, alias} <= set(rows), f"{tag}: rows after retire {sorted(rows)}")
    _check(sorted(r["slug"] for r in man["rows"]) == sorted(RETIRE_JUNK), f"{tag}: manifest rows")
    _check(rows[alias]["db_path"] == new_proj and rows[alias]["mode"] == "managed", f"{tag}: alias row {rows[alias]}")
    # (5) refusals while retired
    mid = _v2_hash(home, repo, repo2)
    rc, out, _ = retire("--slug", slug)
    _check(rc == 3 and "already recorded" in out and _v2_hash(home, repo, repo2) == mid, f"{tag}: second retire rc={rc} {out[-200:]}")
    rc, out, _ = _cli(home, ["--v2", "--home", home, "--rollback"])
    _check(rc == 3 and "v2-retire is recorded" in out and _v2_hash(home, repo, repo2) == mid, f"{tag}: v2 rollback while retired rc={rc} {out[-200:]}")
    # (6) the project archive tarball, then the layout audit
    rc, out, err = _cli(home, ["--archive", "--home", home, "--slug", slug, "--data", data])
    _check(rc == 0, f"{tag}: archive rc={rc} {out[-300:]} {err[-200:]}")
    _check(any(n.endswith(".tar.gz") for n in os.listdir(os.path.join(tdir, "legacy"))), f"{tag}: no tarball")
    audited = _v2_hash(home, repo, repo2)
    ap = subprocess.run([sys.executable, os.path.join(HERE, "mk-e2e-layout.py"), "audit", "--home", home], capture_output=True, text=True, env=env)
    bad = [r for r in (_last_json(ap.stdout) or {}).get("rules", []) if not r.get("pass")]
    _check(ap.returncode == 0 and not bad, f"{tag}: audit rc={ap.returncode} failed={bad} {ap.stderr[-300:]}")
    _check(_v2_hash(home, repo, repo2) == audited, f"{tag}: the audit changed the tree")
    # (7) lanes: a migrated project's registry lock lives under <home>/locks, never in the project folder
    lane = _load("mklane", os.path.join(HERE, "mk-lane.py"))
    with _as_home(home):
        lock = mkpaths.lanes_lock(data)
        _check(os.path.dirname(lock) == os.path.join(home, "locks"), f"{tag}: lanes_lock {lock}")
        for _ in range(2):         # an acquire and a release each take it
            with lane.locked(data, 2):
                pass
    _check(os.path.isfile(lock) and not os.path.exists(os.path.join(tdir, "lanes.lock")), f"{tag}: lanes.lock location")
    os.remove(lock)
    # (8) a changed backup copy blocks the rollback, nothing moves; fixing the copy unblocks it
    victim = os.path.join(backup, "config.json")
    with open(victim, "rb") as f:
        good = f.read()
    with open(victim, "wb") as f:
        f.write(good + b" ")
    held = _v2_hash(home, repo, repo2)
    rc, out, _ = retire("--rollback")
    _check(rc == 3 and "differs from the manifest" in out and _v2_hash(home, repo, repo2) == held
           and os.path.isfile(os.path.join(home, RETIRE_MARKER)), f"{tag}: altered backup rc={rc} {out[-200:]}")
    with open(victim, "wb") as f:
        f.write(good)
    held = _v2_hash(home, repo, repo2)
    # (9) retire rollback: the tree equals the state just before retire. The new root is compared by its rows (the
    # rollback re-inserts the dropped ones, which rewrites the file), the archive tarball is not part of the retire.
    rc, out, err = retire("--rollback", "--dry-run")
    _check(rc == 0 and "restore config.json" in out and _v2_hash(home, repo, repo2) == held, f"{tag}: rollback dry run rc={rc}")
    rc, out, err = retire("--rollback")
    _check(rc == 0 and doc(out).get("rows") == len(RETIRE_JUNK), f"{tag}: rollback rc={rc} {out[-300:]} {err[-200:]}")
    _check(_hash_skip(home, repo, repo2, skip) == before_skip, f"{tag}: the retire rollback did not restore the tree")
    _check(_slugs_of(new_root) == rows_pre, f"{tag}: root rows after the retire rollback differ")
    _check(not os.path.exists(os.path.join(home, RETIRE_MARKER)) and not [n for n in os.listdir(os.path.join(home, "backups")) if n.startswith("retire-")],
           f"{tag}: marker or backup left after the rollback")
    # (10) --v2 --rollback works again; carry-root's moves, the tarball and the retired-then-restored stray lock are
    # outside what it undoes, so the stray lock is removed and the rest is compared with those paths skipped
    os.remove(os.path.join(tdir, "other.yaml.lock"))
    rc, out, err = _cli(home, ["--v2", "--home", home, "--rollback"])
    _check(rc == 0, f"{tag}: v2 rollback rc={rc} {out[-400:]} {err[-300:]}")
    _check(not os.path.exists(new_root) and not os.path.exists(new_proj) and not os.path.exists(mkpaths.mk_settings(home)), f"{tag}: new files survive the v2 rollback")
    _check(_hash_skip(home, repo, repo2, v2_skip) == start_skip, f"{tag}: the v2 rollback did not restore the start (carry-root's moves and the tarball excluded)")
    # (11) hermes: the whole sequence never touched its store, yaml or root row
    _check(_v2_hash(repo2)[0] == hermes_hash, f"{tag}: the hermes folder changed")
    _check(_row_of(old_root, "hermes") == hermes_row, f"{tag}: the hermes root row changed")
    return tag


def _retire_config_case(tmp):
    """config.json holds a key mk-settings.yaml cannot carry: retire leaves it, and says why."""
    fx = _retire_fixture(tmp, config={**V2_CONFIG_JSON, "other_toggle": 1})
    home, slug = fx["home"], fx["slug"]
    rc, out, err = _cli(home, ["--v2", "--home", home, "--slug", slug])
    _check(rc == 0, f"retire-config: v2 rc={rc} {out[-300:]} {err[-200:]}")
    rc, out, _ = _cli(home, ["--v2-retire", "--home", home, "--slug", slug, "--dry-run"])
    d = _last_json(out) or {}
    _check(rc == 0 and "config.json" not in d.get("moves", []) and any("other_toggle" in n for n in d.get("notes", [])), f"retire-config: dry run {out[-300:]}")
    rc, out, _ = _cli(home, ["--v2-retire", "--home", home, "--slug", slug])
    _check(rc == 0 and os.path.isfile(os.path.join(home, "config.json")), f"retire-config: retire rc={rc} {out[-200:]}")
    _check(not os.path.exists(os.path.join(_last_json(out)["backup"], "config.json")), "retire-config: config.json was moved")
    return "v2-retire-config-kept"


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
        _check(not os.path.exists(mkpaths.task_doc_in(rt)), "task.json should not move (C2)")
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
        # L10.1: while switched the data form follows to the C2 store, and rollback still reads the old one
        _check(mkpaths.project_db(data=data) == dest_db, "the data form did not follow the switch")
        _check(src_db(data) == mkpaths.db_in(data), "rollback source is not the legacy store")
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
        _z7_env = _as_home(z7_home)
        _z7_env.__enter__()
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
        _z7_env.__exit__()
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
                             "--branch", "mk/z15plan/lane", "--plan", "z15plan",
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
        _check(os.path.isfile(mkpaths.db_in(z15_data)),
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
            f.write(Z15_PLAN.replace("- Sources: `z15-research.md`", "- Sources: none")
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
                             "--branch", "mk/z15bplan/lane", "--plan", "z15bplan",
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

        # L10.8 v2 rehearsal: a whole home onto mk-overview.db / mk-project.db, from both source shapes
        for make in (_v2_fixture_c2, _v2_fixture_legacy):
            v2tmp = tempfile.mkdtemp(prefix="mk-v2-", dir=tmp)
            ok(_v2_rehearse(make(v2tmp)))
            shutil.rmtree(v2tmp, ignore_errors=True)

        # failure injection (real inputs): a pre-existing target; a lanes.json the carry cannot insert
        v2tmp = tempfile.mkdtemp(prefix="mk-v2-", dir=tmp)
        fx = _v2_fixture_c2(v2tmp)
        _v2_write(mkpaths.new_db_in(fx["tdir"], "project"), "not a store")
        start = _v2_hash(fx["home"], fx["repo"])
        rc, out, _ = _cli(fx["home"], _v2_args(fx, "--slug", fx["slug"]))
        _check(rc == 3 and "already exist" in out, f"v2 pre-existing target rc={rc} {out[-200:]}")
        _check(_v2_hash(fx["home"], fx["repo"]) == start, "v2 pre-existing target: the tree changed")
        _check(not os.path.exists(mkpaths.new_db_in(fx["home"], "root")), "v2 pre-existing target: a root was written")
        os.remove(mkpaths.new_db_in(fx["tdir"], "project"))
        _v2_write(mkpaths.legacy_lanes(fx["tdir"]), json.dumps({"slots": {"3": {"path": ["not", "a", "scalar"]}}}))
        start = _v2_hash(fx["home"], fx["repo"])
        rc, out, _ = _cli(fx["home"], _v2_args(fx, "--slug", fx["slug"]))
        _check(rc == 3 and "v2 failed" in out, f"v2 carry failure rc={rc} {out[-300:]}")
        _check(_v2_hash(fx["home"], fx["repo"]) == start, "v2 carry failure: the tree changed")
        _check(not os.path.exists(mkpaths.new_db_in(fx["home"], "root"))
               and not os.path.exists(mkpaths.new_db_in(fx["tdir"], "project"))
               and not os.path.exists(_v2_marker_path(fx["home"])), "v2 carry failure: a new file is left")
        ok("v2-failure-injection")

        # refusals: a busy lane, and no --slug
        with _as_home(fx["home"]):
            con = mkdb.open_project(fx["home"], fx["slug"])
            con.execute("UPDATE lane_slots SET state='busy' WHERE slot='1'")
            con.commit()
            con.close()
        start = _v2_hash(fx["home"], fx["repo"])
        rc, out, err = _cli(fx["home"], _v2_args(fx, "--slug", fx["slug"]))
        _check(rc == 3 and "not quiesced" in out + err, f"v2 busy lane rc={rc} {out[-200:]}")
        rc2, _, _ = _cli(fx["home"], _v2_args(fx))
        _check(rc2 == 2, f"v2 without --slug rc={rc2}")
        _check(_v2_hash(fx["home"], fx["repo"]) == start, "v2 refusals: the tree changed")
        ok("v2-refusals")
        shutil.rmtree(v2tmp, ignore_errors=True)

        # L10.Y: the whole sequence on a home that mirrors the real layout, then the config.json refusal case
        v2tmp = tempfile.mkdtemp(prefix="mk-v2-", dir=tmp)
        ok(_retire_rehearse(_retire_fixture(v2tmp)))
        shutil.rmtree(v2tmp, ignore_errors=True)
        v2tmp = tempfile.mkdtemp(prefix="mk-v2-", dir=tmp)
        ok(_retire_config_case(v2tmp))
        shutil.rmtree(v2tmp, ignore_errors=True)

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
    con = mkdb.open_project(home, "fixture", data_dir=data, legacy=True)
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
        con = mkdb.open_project(home, "fixture", data_dir=data, legacy=True)
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
