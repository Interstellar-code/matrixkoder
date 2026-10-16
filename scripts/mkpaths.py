"""MatrixKoder paths — ONE module for every MK path (C1, L0).

Nothing else in the repo implements the MK home rule, the data-dir order, the project DB
path, a plan/run/task folder, the lane-token path, the bus location or the watch-cursor
path. `scripts/mkdb.py` keeps its own home rule until L3 (it is L1's file in wave 1).

L0 changes no layout: every function here returns the path the old per-file copies
produced. The `target_*` functions describe the C2 tree
(`~/.matrixkoder/projects/<slug>/...`) and are unused until L3 flips the switch.

L0 folded these copies into this module:
    home rule   — mk-state.py:165, mk-bus.py:88, mk-plan.py:680, mk-lane-event.py:181,
                  lane-health.py:337, mk-lane-nudge.py:318, mk-usage.py:109,
                  mk-config.py:342, mk-store-import.py:71, mk-lane.py:333 (in store_db)
    data order  — mk-state.py:224, mk-plan.py:741, mk-bus.py:293, plus the repo walk in
                  mk-lane.py:195 (git_toplevel)
    lane token  — mk-bus.py:108, mk-lane-event.py:589, mk-builder.py:1955
    bus files   — mk-bus.py bus_location, mk-lane-event.py:831 bus_dir_for

L6.1 moves the bus files into <home>/bus/ (C6) and the watch cursors into the project folder.
The pre-L6.1 file of a moved path is READ ONCE and copied over (`_take_over`) when the new
one is missing — never deleted, never written — so a bus, an armed lane or a watch that was
started before the switch keeps working against the same bytes. Every WRITE lands at the new
path, because these getters answer with the new path. Python 3 stdlib only; `mkdb.py` is
deliberately NOT imported (mkpaths must stay importable from mkdb once it delegates here in L3).
"""
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile

DEFAULT_HOME = "~/.matrixkoder"


def _name(env, default):
    """A file-name constant: `default`, or $<env> when set. The override exists for the rename canary
    (c1-gate.py canary): proof that no caller spells a store/run file name outside this module."""
    v = os.environ.get(env)
    if v is None or v == "":
        return default
    if "/" in v or "\\" in v or v in (".", "..") or v.startswith("."):
        raise RuntimeError(f"{env}={v!r} is not a plain file name")
    return v


# L10.13: new homes name their stores mk-overview.db (root) and mk-project.db (project). A store that already
# exists under the legacy name `mk.db` keeps being used under that name until migrate v2 renames it (L10.8):
# the live home must not move under running processes. Rule, one place (`_resolve_db`): the new-name file if it
# exists, else an existing legacy `mk.db`, else (nothing on disk) the new name. Never rename, never create a
# new-name file beside an old one. LEGACY_DB_NAME is deliberately not overridable: it is the name on disk.
ROOT_DB_NAME = _name("MK_NAME_ROOT_DB", "mk-overview.db")
PROJECT_DB_NAME = _name("MK_NAME_PROJECT_DB", "mk-project.db")
LEGACY_DB_NAME = "mk.db"
PROJECTS_JSON = "projects.json"
USAGE_LEDGER = "usage.json"
MK_SETTINGS_NAME = _name("MK_NAME_SETTINGS", "mk-settings.yaml")
# L10.12a: the file names a caller joins onto a folder it already holds (no migrated lookup, no home rule)
DB_FILE_NAMES = tuple(n + suf for n in (ROOT_DB_NAME, PROJECT_DB_NAME, LEGACY_DB_NAME) for suf in ("", "-wal", "-shm"))
# ^ every store file a folder can hold (new root, new project, legacy), each with its WAL sidecars
LEGACY_YAML_NAME = _name("MK_NAME_LEGACY_YAML", "matrixkoder.yaml")
PROJECT_YAML_NAME = _name("MK_NAME_PROJECT_YAML", "project.yaml")
RUN_DOC_NAME = _name("MK_NAME_RUN_DOC", "run.json")
TASK_DOC_NAME = _name("MK_NAME_TASK_DOC", "task.json")
EVENTS_DOC_NAME = _name("MK_NAME_EVENTS_DOC", "events.jsonl")
BOARD_DOC_NAME = _name("MK_NAME_BOARD_DOC", "board.md")
VIEW_DOC_NAMES = (RUN_DOC_NAME, TASK_DOC_NAME, EVENTS_DOC_NAME, BOARD_DOC_NAME)  # the pre-C4 file views
LANES_NAME = _name("MK_NAME_LANES", "lanes.json")
CURSORS_NAME = _name("MK_NAME_CURSORS", "watch-cursors.json")
INBOX_DIR = "inbox"
LANE_BUS_FILE = ".matrixkoder-bus.json"
BUS_LANES_DIR = "bus-lanes"
# L6.1: the lane token files move with the bus into <bus dir>/lanes/ (C6). BUS_LANES_DIR
# stays the pre-L6.1 spelling: it is the read-once source a take-over copies from.
TARGET_LANES_DIR = "lanes"
# the project slug rule (C11), the same shape mk-bus and mkdb check
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

# C2 (the target tree; unused until L3 flips the switch)
TARGET_BUS_DIR = "bus"
TARGET_PROJECTS_DIR = "projects"
TARGET_PLANS_DIR = "plans"


class Fail(RuntimeError):
    """A path that cannot be resolved. A RuntimeError so the callers that raised RuntimeError
    keep raising one; the callers with their own Fail(Exception) convert it (see their wrappers)."""


def home(arg=None):
    """One MK home rule (C16), mkdb.resolve_home's: `--home` arg > $MK_HOME > $MATRIXKODER_HOME
    > ~/.matrixkoder. When both env names are set they must resolve to the same directory.

    The answer is abspath(expanduser(...)), mkdb.resolve_home's canonical form. The older
    mk-state/mk-plan/mk-lane-event/lane-health/mk-lane-nudge/mk-usage copies returned the
    expanduser value un-abspathed: identical for an absolute input, and a relative $MK_HOME
    is a config bug this refuses to guess at. `mkdb.check_home` (C11, synced/networked homes)
    stays with mkdb, so a home mk-state accepts is accepted here unchanged.
    """
    mkh = os.environ.get("MK_HOME")
    mch = os.environ.get("MATRIXKODER_HOME")
    if mkh and mch:
        left = os.path.realpath(os.path.abspath(os.path.expanduser(mkh)))
        right = os.path.realpath(os.path.abspath(os.path.expanduser(mch)))
        if left != right:
            raise Fail(f"MK_HOME ({mkh}) and MATRIXKODER_HOME ({mch}) differ; set one MK home")
    return os.path.abspath(os.path.expanduser(arg or mkh or mch or DEFAULT_HOME))


# ---- git repo discovery (part of the data-dir order) ----------------------------------------

def main_worktree(top):
    """A linked worktree (.git is a file + commondir) shares the main checkout's data dir."""
    try:
        with open(os.path.join(top, ".git")) as f:
            gitdir = f.read().split("gitdir:", 1)[1].strip()
        gitdir = os.path.join(top, gitdir)
        with open(os.path.join(gitdir, "commondir")) as f:
            common = os.path.normpath(os.path.join(gitdir, f.read().strip()))
    except (OSError, IndexError):  # .git is a directory, or a submodule (no commondir)
        return top
    return os.path.dirname(common) if os.path.basename(common) == ".git" else top


def repo_top():
    """The main checkout of the nearest git repo above cwd, or None when there is none.
    The walk stops before $HOME; a linked worktree maps to its main checkout."""
    d, stop = os.path.realpath(os.getcwd()), os.path.realpath(os.path.expanduser("~"))
    while not os.path.exists(os.path.join(d, ".git")):  # nearest repo wins; nested repos pass --data
        if os.path.dirname(d) == d or d == stop:
            return None
        d = os.path.dirname(d)
    if d == stop:
        return None
    return main_worktree(d)


# ---- the data-dir order ----------------------------------------------------------------------

def data_dir(arg=None, required=True):
    """The data-dir order (C1): --data > $MATRIXKODER_DATA > root projects table >
    <home>/projects.json > <main checkout>/.matrixkoder.

    The repo is the nearest .git above cwd (never $HOME itself); a linked worktree maps to its
    main checkout. Reading the root table checks the file exists first, so a resolve never
    creates <home>/mk.db. `required=False` returns None instead of failing when there is no
    repo above cwd (mk-plan: a plan file stays usable without a store).
    """
    if arg:
        return os.path.abspath(os.path.expanduser(arg))
    if os.environ.get("MATRIXKODER_DATA"):
        return os.path.abspath(os.path.expanduser(os.environ["MATRIXKODER_DATA"]))
    top = repo_top()
    if top is None:
        if required:
            raise Fail("no git toplevel above cwd (the walk stops before $HOME); "
                       "pass --data or set MATRIXKODER_DATA")
        return None
    listed = root_data_dir(top)
    if listed:
        return listed
    listed = projects_json_data(top)
    if listed:
        return listed
    return os.path.join(top, ".matrixkoder")


def _open_root_ro(path):
    """Open the root overview store read-only. Never creates the file; None when absent.

    Read with sqlite3 directly rather than through mkdb: mkdb will delegate to mkpaths in
    L3, so importing it here would be a cycle. mode=ro is the documented exception to the
    single-writer rule — a resolve must not migrate or stamp a store.
    """
    if not os.path.isfile(path):
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _query(path, sql, args=()):
    con = _open_root_ro(path)
    if con is None:
        return None
    try:
        return con.execute(sql, args).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()


SQLITE_MAGIC = b"SQLite format 3\x00"


def is_store_file(path):
    """True when `path` is a real SQLite store: a file that starts with the 16-byte header. A 0-byte or
    foreign file is not one — an empty new-name file must never win over the live store (L10.X)."""
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def _resolve_db(folder, kind):
    """The store file inside `folder` for kind 'root' | 'project' (L10.13): the new-name file when it is a real
    store, else an existing legacy `mk.db`, else (a fresh folder) the new name. When BOTH are real the new name
    wins: only a deliberate step (migrate v2, a fresh home) creates it, and the legacy file is then the frozen
    pre-migration copy. An empty or foreign new-name file is not a store (L10.X) and never hides `mk.db`.
    Read-only: nothing is created, renamed or moved."""
    new = os.path.join(folder, ROOT_DB_NAME if kind == "root" else PROJECT_DB_NAME)
    if is_store_file(new):
        return new
    old = os.path.join(folder, LEGACY_DB_NAME)
    return old if os.path.isfile(old) else new


def _kind_of(folder):
    """'root' when `folder` is the MK home (or the pinned bus home), else 'project'. Only a fresh folder needs
    this: an existing store is found by its own name."""
    try:
        real = os.path.realpath(os.path.abspath(folder))
        return "root" if real in {os.path.realpath(home()), os.path.realpath(root_home())} else "project"
    except (Fail, OSError):
        return "project"


def root_db(arg=None):
    """The root overview store: <home>/mk-overview.db on a new home, the existing <home>/mk.db on a home that
    has one (C3, L10.13). A read never creates it."""
    return _resolve_db(home(arg), "root")


def bus_env_dir():
    """$MK_BUS_DIR: the bus test seam that pins a bus home without touching ~/.matrixkoder."""
    override = os.environ.get("MK_BUS_DIR")
    return os.path.abspath(os.path.expanduser(override)) if override else None


def root_home():
    """The home the root overview store lives in for the project-by-slug lookups below: the
    bus pins it with $MK_BUS_DIR (tests), everything else uses the MK home. No other caller
    ever sets $MK_BUS_DIR, so the two agree wherever it matters."""
    return bus_env_dir() or home()


def db_in(folder):
    """The store file of a folder the caller already resolved (no home rule, no migrated lookup:
    `project_db` and `root_db` are the resolving forms). An existing file keeps its name (see `_resolve_db`);
    a fresh folder gets mk-overview.db when it is the MK home, else mk-project.db."""
    return _resolve_db(folder, _kind_of(folder))


def root_db_in(folder):
    """The root store file of `folder` (kind explicit: no guess from the MK home). Existing files keep their name."""
    return _resolve_db(folder, "root")


def project_db_in(folder):
    """The project store file of `folder` (kind explicit). Existing files keep their name."""
    return _resolve_db(folder, "project")


def db_files_in(folder):
    """The store file of `folder` and its two WAL sidecars."""
    db = db_in(folder)
    return [db, db + "-wal", db + "-shm"]


def legacy_db_in(folder):
    """<folder>/mk.db, literally: the OLD name, for mk-migrate to name the file it migrates away from."""
    return os.path.join(folder, LEGACY_DB_NAME)


def new_db_in(folder, kind):
    """The NEW-name store path of `folder` for kind 'root' | 'project', whether or not it exists: the
    target a migration writes to."""
    if kind not in ("root", "project"):
        raise Fail(f"new_db_in: kind is root or project, not {kind!r}")
    return os.path.join(folder, ROOT_DB_NAME if kind == "root" else PROJECT_DB_NAME)


def legacy_yaml(folder):
    """<folder>/matrixkoder.yaml: the pre-C2 settings file name, literal (`project_yaml` resolves)."""
    return os.path.join(folder, LEGACY_YAML_NAME)


def project_yaml_in(folder):
    """<folder>/project.yaml: the C2 settings file of a project folder the caller already resolved."""
    return os.path.join(folder, PROJECT_YAML_NAME)


def legacy_lanes(folder):
    """<folder>/lanes.json, literal (`lanes_file` is the data-dir form; same bytes)."""
    return lanes_file(folder)


def legacy_run_dir(data, run):
    """<data>/runs/<run>, literal: where the pre-C4 file views live (`run_dir` follows a switch)."""
    return os.path.join(runs_dir(data), run)


def run_doc_in(run_folder):
    return os.path.join(run_folder, RUN_DOC_NAME)


def task_doc_in(task_folder):
    return os.path.join(task_folder, TASK_DOC_NAME)


def events_doc_in(run_folder):
    return os.path.join(run_folder, EVENTS_DOC_NAME)


def board_doc_in(run_folder):
    return os.path.join(run_folder, BOARD_DOC_NAME)


def legacy_cursors(folder):
    """<folder>/watch-cursors.json, literal: the pre-L6.1 cursor file, never the take-over form."""
    return os.path.join(folder, CURSORS_NAME)


def mk_settings(arg=None):
    """The global settings file: <home>/mk-settings.yaml (L10.3 reads and writes it). A pure path."""
    return os.path.join(home(arg), MK_SETTINGS_NAME)


def inbox_dir(arg=None, plan_id=None):
    """<home>/inbox/, or <home>/inbox/<plan-id>/ — unassigned plans and input docs (L10.6 imports
    from it). A pure path: nothing is created. A plan id with a separator or `..` is refused."""
    base = os.path.join(home(arg), INBOX_DIR)
    if plan_id is None:
        return base
    if (not isinstance(plan_id, str) or not plan_id or plan_id in (".", "..") or "/" in plan_id
            or "\\" in plan_id or "\0" in plan_id):
        raise Fail(f"bad plan id {plan_id!r}")
    return os.path.join(base, plan_id)


def projects_json(arg=None):
    """The wizard's home-mode registry: <home>/projects.json."""
    return os.path.join(home(arg), PROJECTS_JSON)


def _repo_row(top, arg=None):
    """The root projects row for this repo (repo or realpath(repo)), or None."""
    return _query(root_db(arg),
                  "SELECT data_dir FROM projects WHERE repo=? OR repo=? ORDER BY slug LIMIT 1",
                  (top, os.path.realpath(top)))


def root_data_dir(top, arg=None):
    """data_dir from the root projects table for this repo, or None.
    A missing root store is not created: a read must never make <home>/mk.db."""
    row = _repo_row(top, arg)
    if row and isinstance(row["data_dir"], str) and row["data_dir"].strip():
        return os.path.normpath(os.path.join(top, os.path.expanduser(row["data_dir"].strip())))
    return None


def projects_json_data(top, arg=None):
    """data_dir from <home>/projects.json for this repo, or None."""
    try:  # the wizard's home mode: ~/.matrixkoder/projects.json {"<repo>": {"data_dir": ...}}
        with open(projects_json(arg)) as f:
            projects = json.load(f)
        entry = projects.get(top) or projects.get(os.path.realpath(top))
        listed = entry.get("data_dir") if isinstance(entry, dict) else None
    except (OSError, ValueError, AttributeError):
        listed = None
    if isinstance(listed, str) and listed.strip():
        return os.path.normpath(os.path.join(top, os.path.expanduser(listed.strip())))
    return None


def project_data_dir(slug, root=None):
    """The data_dir the root projects table gives this slug, or None. Never creates mk.db.

    mk-bus.store_for's rule: realpath(expanduser(recorded)). A relative data_dir therefore
    resolves against cwd, not the repo — the bus runs from the repo or a lane of it, and that
    is the bus's rule to keep (state-side `root_data_dir` resolves it against the repo top).
    `root` names the root store's home (default: `root_home()`), so a caller with an explicit
    --home reads that store rather than the ambient one.
    """
    row = _query(_resolve_db(root or root_home(), "root"),
                 "SELECT data_dir FROM projects WHERE slug=?", (slug,))
    if not row or not isinstance(row["data_dir"], str) or not row["data_dir"].strip():
        return None
    return os.path.realpath(os.path.expanduser(row["data_dir"].strip()))


def _row_rank(slug, db_path, root_dir):
    """How canonical a root row is for its data dir: 2, 1 or 0 (Z22: the one rule, shared).

    2 = db_path folder is `<root>/projects/<this row's slug>` (the C2 rule's own shape);
    1 = db_path set (C3's migrated marker); 0 = neither. Callers keep the FIRST row of the
    highest rank, so row order never picks the answer. `root_dir` is the realpath'd root home.
    """
    if not (isinstance(db_path, str) and db_path.strip()):
        return 0
    folder = os.path.dirname(os.path.expanduser(db_path.strip()))
    if os.path.realpath(os.path.abspath(folder)) == os.path.join(root_dir, TARGET_PROJECTS_DIR, slug):
        return 2
    return 1


def project_slug_of(data_dir, root=None):
    """The root projects.slug whose data_dir OR migrated db_path folder is this path, or None.

    mk-bus.project_slug_for's read half: realpath on both sides, slug rule C11 (an invalid
    slug is skipped, so a matching row with a bad slug never wins). After a switch a project has
    TWO known locations — the legacy data_dir still recorded on the row, and the C2 folder that
    holds its store (dirname of db_path) — and the bus is launched with either one, so both must
    resolve to the same slug (C7: the bus follows the switch). Never creates mk.db.
    `root` as in `project_data_dir`.

    Z19: one data dir can be named by SEVERAL rows — the bus's own project_slug_for registers
    a derived `p<hash>` alias for a dir it has no row for (mk-bus.py:3322-3328 documents the
    same problem), and that row STAYS after the project switches under its real slug. Row order
    must not choose the answer. Among the matching rows the canonical one wins: the row whose
    db_path folder is `<root>/projects/<that row's slug>` (the C2 rule's own shape); else
    any matching row that has db_path set (C3's migrated marker); else the first match.
    """
    real = os.path.realpath(data_dir)
    root_dir = os.path.realpath(os.path.abspath(os.path.expanduser(root or root_home())))
    con = _open_root_ro(_resolve_db(root_dir, "root"))
    if con is None:
        return None
    try:
        best, best_rank = None, -1
        for row in con.execute("SELECT slug, data_dir, db_path FROM projects"):
            slug, recorded, db_path = row["slug"], row["data_dir"], row["db_path"]
            if not isinstance(slug, str) or not SLUG_RE.match(slug):
                continue
            match = ((recorded and isinstance(recorded, str) and recorded.strip()
                      and os.path.realpath(os.path.expanduser(recorded.strip())) == real)
                     or (isinstance(db_path, str) and db_path.strip()
                         and os.path.realpath(os.path.dirname(os.path.expanduser(db_path.strip()))) == real))
            if not match:
                continue
            rank = _row_rank(slug, db_path, root_dir)
            if rank > best_rank:  # strict: the first row of the best rank wins
                best, best_rank = slug, rank
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return best


# ---- migration: today's layout vs the C2 target (L3) ----------------------------------------

def _root_signature(root=None):
    """A cheap change stamp for the root store: (path, mtime_ns, size) or None.

    A resolve runs on hot paths (every run/plan/task folder build), so the migrated lookup below
    is memoised against this stamp instead of hitting SQLite each call. A switch rewrites the root
    mk.db (mk-migrate's UPDATE), which moves mtime/size and drops the memo.
    """
    path = _resolve_db(root or root_home(), "root")
    try:
        st = os.stat(path)
    except OSError:
        return path, None, None
    return path, st.st_mtime_ns, st.st_size


_MIGRATED_MEMO = None  # (signature, {slug: db_path}, {data_dir: slug})


def _migrated_maps(root=None):
    """({slug: db_path}, {realpath(data_dir): slug}) for the root store, memoised by its stamp.

    db_path set is C3's migrated marker. A missing or unreadable root yields two empty maps; this
    never creates a store and never raises, so a resolve can fall back to today's path.
    """
    global _MIGRATED_MEMO
    sig = _root_signature(root)
    if _MIGRATED_MEMO is not None and _MIGRATED_MEMO[0] == sig:
        return _MIGRATED_MEMO[1], _MIGRATED_MEMO[2]
    by_slug, by_data, rank = {}, {}, {}
    root_dir = os.path.realpath(os.path.abspath(os.path.expanduser(root or root_home())))
    con = _open_root_ro(sig[0])
    if con is not None:
        try:
            for row in con.execute("SELECT slug, data_dir, db_path FROM projects"):
                slug, data_dir, db_path = row["slug"], row["data_dir"], row["db_path"]
                if not isinstance(slug, str) or not SLUG_RE.match(slug):
                    continue
                if isinstance(db_path, str) and db_path.strip():
                    by_slug[slug] = db_path.strip()
                if isinstance(data_dir, str) and data_dir.strip():
                    key = os.path.realpath(os.path.expanduser(data_dir.strip()))
                    r = _row_rank(slug, db_path, root_dir)
                    if r > rank.get(key, -1):  # Z22: canonical row wins, whatever the row order
                        by_data[key], rank[key] = slug, r
        except sqlite3.Error:
            by_slug, by_data = {}, {}
        finally:
            con.close()
    _MIGRATED_MEMO = (sig, by_slug, by_data)
    return by_slug, by_data


def db_path_of(slug=None, data=None, root=None):
    """The root projects.db_path for this project, or None (pre-migration).

    Either form names the project: a slug directly, a data dir through the root row's data_dir.
    A missing root, a missing row or an unreadable root is None — never an exception, so every
    caller keeps today's path for a pre-migration project (C3).
    """
    by_slug, by_data = _migrated_maps(root)
    if not slug and data:
        slug = by_data.get(os.path.realpath(os.path.expanduser(data)))
    return by_slug.get(slug) if slug else None


def migrated_slug(data=None, slug=None, root=None):
    """The slug of a migrated project named by `data` or `slug`, else None. db_path set = migrated.

    `data` is either form of a migrated project: the legacy data_dir on the root row, or the C2
    project folder (dirname of that row's db_path), so run/task folder helpers answer the same for both.
    """
    by_slug, by_data = _migrated_maps(root)
    if not slug and data:
        real = os.path.realpath(os.path.expanduser(data))
        slug = by_data.get(real)
        if not slug:
            # several rows can share one db_path (Z19's p<hash> alias): the one named like the folder wins
            hits = [s for s, db in by_slug.items()
                    if os.path.realpath(os.path.dirname(os.path.expanduser(db))) == real]
            slug = os.path.basename(real) if os.path.basename(real) in hits else next(iter(hits), None)
    return slug if slug in by_slug else None


def project_folder(data, home_arg=None):
    """The folder a caller holding a data dir should use: the C2 project folder for a migrated
    project, else the data dir itself.

    mk-plan holds the repo's data dir (its `--data`), which after a switch is the project's LEGACY
    location; the store, archives and round files must land in the C2 folder once the root row has
    db_path set. mkdb.project_dir/open_project keep their `--data`-literal rule (mk-migrate's
    rollback addresses the legacy store through exactly that form), so a writer resolves the folder
    here instead. Never creates anything.
    """
    slug = migrated_slug(data=data, root=home_arg)
    return target_project_dir(slug, home_arg) if slug else data


# ---- stores ----------------------------------------------------------------------------------

def project_db(slug=None, data=None, legacy=False):
    """The project store (C2/C3). A migrated project's store is its root `db_path`; else <data>/mk.db.

    Both forms follow a switch (L10.1): a root `projects.db_path` set is L2's marker, so the slug
    form AND the data form (the project's old `<repo>/.matrixkoder`, or its C2 folder) resolve the
    NEW store. `legacy=True` asks for the literal `<data>/mk.db` — mk-migrate's rollback reads the
    source store through it (its copy-back must not follow the switch). An unmigrated project is
    `<data>/mk.db` either way. Absent file means file mode; this never creates it. Returns None
    when a slug has no root projects row carrying a data_dir (the caller owns that error).
    """
    if data:
        data = os.path.abspath(os.path.expanduser(data))
        migrated = None if legacy else migrated_slug(data=data)
        return db_path_of(slug=migrated) if migrated else _resolve_db(data, "project")
    if not slug:
        raise Fail("project_db needs a slug or a data dir")
    target = db_path_of(slug=slug)
    if target:
        return target
    data_dir_of_slug = project_data_dir(slug)
    if not data_dir_of_slug:
        return None
    return _resolve_db(data_dir_of_slug, "project")


def project_dir(home_arg=None, slug=None, data_dir=None, legacy=False):
    """The project's folder (C2): <home>/projects/<slug> once migrated, else today's answer.

    Both forms follow a switch (L10.1) — a root `projects.db_path` set (L2's marker) resolves to
    the C2 folder, whether the caller names the slug or the project's old `--data` dir. `legacy=True`
    keeps the literal `--data` folder (mk-migrate's rollback addresses the LEGACY <data>/mk.db
    through it). A pre-migration project is byte-for-byte today's path. Never creates anything.
    """
    if data_dir:
        data_dir = os.path.abspath(os.path.expanduser(data_dir))
        migrated = None if legacy else migrated_slug(data=data_dir, root=home_arg)
        return target_project_dir(migrated, home_arg) if migrated else data_dir
    if not slug:
        raise Fail("pass --project or --data")
    if db_path_of(slug=slug, root=home_arg):
        return target_project_dir(slug, home_arg)
    return os.path.join(home(home_arg), TARGET_PROJECTS_DIR, slug)


# ---- run / plan / task folders (today: <data>/runs/**, <data>/plans/**) ----------------------
#
# Migrated projects (root db_path set) live in the C2 tree: `<home>/projects/<slug>/plans/<plan>/
# runs/<run>/<task>/`. A run's folder needs its plan id, which only the project store records, so
# `run_dir` reads it from there (read-only, memoised by the store's stamp) and falls back to
# today's path when the plan cannot be resolved — a resolve never raises and never writes.

_RUN_PLAN_MEMO = None  # (db path, mtime_ns, size, {run: plan})


def _project_runs(db, run):
    """`runs.plan` for one run from the project store at `db`, or None. Never raises."""
    global _RUN_PLAN_MEMO
    try:
        st = os.stat(db)
        sig = (db, st.st_mtime_ns, st.st_size)
    except OSError:
        sig = (db, None, None)
    if _RUN_PLAN_MEMO is not None and _RUN_PLAN_MEMO[0] == sig:
        return _RUN_PLAN_MEMO[1].get(run)
    plans = {}
    con = _open_root_ro(db)
    if con is not None:
        try:
            for row in con.execute("SELECT id, plan FROM runs"):
                if isinstance(row["id"], str) and isinstance(row["plan"], str):
                    plans[row["id"]] = row["plan"]
        except sqlite3.Error:
            plans = {}
        finally:
            con.close()
    _RUN_PLAN_MEMO = (sig, plans)
    return plans.get(run)


def _run_plan(data, run):
    """The plan id of a run in a migrated project named by `data`, else None.

    A run is only reachable in the C2 tree once its project is migrated and the store names its
    plan; anything else (pre-migration, unknown run, unreadable store) is None, so the caller
    keeps today's path.
    """
    slug = migrated_slug(data=data)
    if not slug:
        return None
    plan = _project_runs(project_db(slug=slug), run)
    return plan or None


def runs_dir(data):
    return os.path.join(data, "runs")


def run_dir(data, run):
    """<data>/runs/<run>, or the C2 <project>/plans/<plan>/runs/<run> for a migrated project."""
    plan = _run_plan(data, run)
    if plan:
        return target_run_dir(migrated_slug(data=data), plan, run)
    return os.path.join(runs_dir(data), run)


def tasks_dir(data, run):
    return os.path.join(run_dir(data, run), "tasks")


def task_dir(data, run, task):
    """<data>/runs/<run>/tasks/<task>, or its C2 twin for a migrated project."""
    plan = _run_plan(data, run)
    if plan:
        return target_task_dir(migrated_slug(data=data), plan, run, task)
    return os.path.join(tasks_dir(data, run), task)


def run_doc(data, run):
    return os.path.join(run_dir(data, run), RUN_DOC_NAME)


def task_doc(data, run, task):
    return os.path.join(task_dir(data, run, task), TASK_DOC_NAME)


def events_doc(data, run):
    return os.path.join(run_dir(data, run), EVENTS_DOC_NAME)


def board_doc(data, run):
    return os.path.join(run_dir(data, run), BOARD_DOC_NAME)


def runs_index(data):
    return os.path.join(runs_dir(data), "index.json")


def plans_dir(data):
    return os.path.join(data, "plans")


def plan_dir(data, plan):
    """<data>/plans/<plan> today; the C2 <project>/plans/<plan> for a migrated project."""
    slug = migrated_slug(data=data)
    if slug:
        return target_plan_dir(slug, plan)
    return os.path.join(plans_dir(data), plan)


def plan_archives_dir(data, plan=None):
    """The plan-version folder. Migrated and named: <project>/plans/<plan>/archive (C2).
    Otherwise today's <data>/plans/archive — a migrated project with no plan id cannot be
    placed, so it keeps the old answer rather than guess."""
    slug = migrated_slug(data=data) if plan else None
    if slug:
        return os.path.join(target_plan_dir(slug, plan), "archive")
    return os.path.join(plans_dir(data), "archive")


def plan_file(data, plan_id):
    """The plan markdown: <data>/plans/<plan_id>.md today, or the C2 <project>/plans/<id>/plan.md."""
    slug = migrated_slug(data=data)
    if slug:
        return os.path.join(target_plan_dir(slug, plan_id), "plan.md")
    return os.path.join(plans_dir(data), f"{plan_id}.md")


def project_yaml(data):
    """<data>/matrixkoder.yaml today; the C2 <project>/project.yaml for a migrated project."""
    slug = migrated_slug(data=data)
    if slug:
        return target_project_yaml(slug)
    return os.path.join(data, LEGACY_YAML_NAME)


def project_yaml_lock(data):
    """The flock beside matrixkoder.yaml: <data>/matrixkoder.yaml.lock."""
    return os.path.join(project_yaml(data) + ".lock")


def lanes_file(data):
    """<data>/lanes.json — the lane-slot registry (a project-level file, not lane data)."""
    return os.path.join(data, LANES_NAME)


def lanes_lock(data):
    """The flock beside lanes.json: <data>/lanes.lock."""
    return os.path.join(data, "lanes.lock")


def runs_lock(data):
    """The one flock file for the data dir: <data>/runs/.lock."""
    return os.path.join(runs_dir(data), ".lock")


# ---- bus ------------------------------------------------------------------------------------

def bus_dir(data):
    """Where bus.json / bus.lock / bus-tokens.json / ring-queue.json / lanes/ live for this
    data dir (C6, L6.1).

    $MK_BUS_DIR wins: the test seam pins the bus home, and the bus files live directly in
    it, so a pinned bus keeps today's layout. File mode (no <data>/mk.db): the data dir,
    exactly as before — the self-hosting guard. Store mode with no pin: the MK home's
    `bus/` folder, one bus for every project (`~/.matrixkoder/bus/`; the pre-L6.1 location
    was the home root itself, read once).
    """
    override = bus_env_dir()
    if override:
        return override
    if os.path.isfile(project_db(data=data)):
        return target_bus_dir()
    return data


def _legacy_bus_dir(data):
    """Where the bus files lived before L6.1: $MK_BUS_DIR, else the home root in store mode,
    else the data dir. The read-once source of every bus-scoped file below — a caller that
    still finds its data there copies it over and never deletes it (C6)."""
    override = bus_env_dir()
    if override:
        return override
    if os.path.isfile(project_db(data=data)):
        return home()
    return data


def _take_over(new_path, old_path):
    """The read-once rule for a moved file (C6, L6.1).

    Read the NEW place. When it is missing and the OLD one holds data, copy that data over
    first, atomically and 0600, and return the new path. The old file is never deleted and
    never written, so a downgrade, a second reader and the rollback all still see it. An
    empty or unreadable old file is ignored (nothing to carry over), and a failed copy falls
    back to reading the old file, which holds the same bytes. Idempotent: once the new file
    exists this is one stat.
    """
    if not old_path or old_path == new_path or os.path.exists(new_path):
        return new_path
    try:
        with open(old_path, "rb") as f:
            blob = f.read()
    except OSError:
        return new_path
    if not blob:
        return new_path
    dst_dir = os.path.dirname(new_path)
    fd = None
    tmp = None
    try:
        os.makedirs(dst_dir, exist_ok=True)
        # A UNIQUE temp name per copier (review HIGH 1): the old fixed
        # `new_path + ".take-over"` was shared by every copier, so one racer's
        # os.replace deleted another's temp mid-write — installing a truncated
        # 0-byte file, or raising FileNotFoundError so callers diverged (one
        # answered new_path, another old_path). mkstemp opens 0600; the chmod
        # only makes that explicit.
        fd, tmp = tempfile.mkstemp(dir=dst_dir, prefix=".take-over-")
        with os.fdopen(fd, "wb") as f:
            fd = None  # closed by fdopen on every path out of the with
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, new_path)
        tmp = None  # installed: nothing to clean up
    except OSError:
        if fd is not None:
            os.close(fd)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return old_path
    return new_path


def _bus_path(data, name):
    """One bus-scoped file: the L6.1 path under bus_dir, read-once from the pre-L6.1 one."""
    return _take_over(os.path.join(bus_dir(data), name),
                     os.path.join(_legacy_bus_dir(data), name))


def lane_token(lane, file_mode=False):
    """C6: the ONE lane-token rule for mk-bus, mk-lane-event and mk-builder.

    $MK_BUS_TOKEN_FILE wins (tests and a relocated home), else the file-mode token inside the
    lane (<lane>/.matrixkoder-bus.json) — when it is there, and in file mode even when it is
    not — else <bus dir>/lanes/<sha256(realpath(lane))>.json, mode 0600. With the bus pinned
    by $MK_BUS_DIR that file lives in it, else under $MK_HOME, else $MATRIXKODER_HOME, else
    ~/.matrixkoder — L6.1 puts the bus under ~/.matrixkoder/bus/, so the home form is
    ~/.matrixkoder/bus/lanes/<hash>.json. The pre-L6.1 <home>/bus-lanes/<hash>.json is read
    once and copied over, never deleted, so an armed lane keeps its token across the switch.
    """
    override = os.environ.get("MK_BUS_TOKEN_FILE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    inside = os.path.join(lane, LANE_BUS_FILE)
    if file_mode or os.path.isfile(inside):
        return inside
    base = os.path.abspath(os.path.expanduser(bus_env_dir() or home()))
    digest = hashlib.sha256(os.path.realpath(lane).encode("utf-8")).hexdigest()
    # the token file lives in the bus dir, exactly like bus.json: the $MK_BUS_DIR pin is
    # that dir, and with no pin the bus dir is the home's bus/ folder (C6, L6.1)
    bus = base if bus_env_dir() else os.path.join(base, TARGET_BUS_DIR)
    return _take_over(os.path.join(bus, TARGET_LANES_DIR, digest + ".json"),
                      os.path.join(base, BUS_LANES_DIR, digest + ".json"))


def ring_queue(data):
    return _bus_path(data, "ring-queue.json")


def bus_json(data):
    """<bus>/bus.json — pid, port, started, owner_heartbeat, token (0600)."""
    return _bus_path(data, "bus.json")


def bus_lock(data):
    return _bus_path(data, "bus.lock")


def bus_tokens(data):
    return _bus_path(data, "bus-tokens.json")


def bus_tokens_lock(data):
    return _bus_path(data, "bus-tokens.lock")


def legacy_bus_json(data):
    """The pre-L6.1 bus.json, at the legacy bus dir: its live pid proves an OLD bus is
    still running (R2 HIGH 2) — the one signal that tokens minted at the new path must
    ALSO land in the legacy bus-tokens.json the old bus still validates against."""
    return os.path.join(_legacy_bus_dir(data), "bus.json")


def legacy_bus_tokens(data):
    """The pre-L6.1 bus-tokens.json an adopted old bus reads its tokens from."""
    return os.path.join(_legacy_bus_dir(data), "bus-tokens.json")


def legacy_bus_tokens_lock(data):
    """The flock beside legacy_bus_tokens — the old bus's writers take this one."""
    return os.path.join(_legacy_bus_dir(data), "bus-tokens.lock")


# ---- home-level files -----------------------------------------------------------------------

def usage_ledger():
    """<home>/usage.json — the one ledger mk-usage.py writes and lane-health.py marks."""
    return os.path.join(home(), USAGE_LEDGER)


def usage_lock():
    """The flock beside usage.json: <home>/usage.json.lock — the one lock both writers take."""
    return os.path.join(home(), USAGE_LEDGER + ".lock")


def lock_path(res):
    """<home>/locks/<res lowercased>.lock — mk-state's per-resource lock file."""
    return os.path.join(home(), "locks", res.lower() + ".lock")


def watch_cursors(data):
    """The orchestrator bus watch cursors (0600): projects/<slug>/watch-cursors.json for a
    migrated project (C6, mkpaths.project_folder), else today's <data>/watch-cursors.json.
    The pre-migration file is read once and copied over, never deleted."""
    return _take_over(os.path.join(project_folder(data), CURSORS_NAME),
                      os.path.join(data, CURSORS_NAME))


def nudge_state(data):
    """mk-lane-nudge's state file: the data dir when there is one, else the MK home."""
    return os.path.join(data or home(), "nudge-state.json")


def nudge_state_lock(data):
    """The flock beside the nudge state: <data or home>/nudge-state.lock."""
    return os.path.join(data or home(), "nudge-state.lock")


# ---- C2 target tree (the bus entries below are live from L6.1) ------------------------------

def target_projects_dir(arg=None):
    return os.path.join(home(arg), TARGET_PROJECTS_DIR)


def target_project_dir(slug, arg=None):
    return os.path.join(target_projects_dir(arg), slug)


def target_project_db(slug, arg=None):
    """The C2 project store: an existing file keeps its name, a fresh project gets mk-project.db."""
    return _resolve_db(target_project_dir(slug, arg), "project")


def target_project_yaml(slug, arg=None):
    """Today's matrixkoder.yaml, under the project folder."""
    return os.path.join(target_project_dir(slug, arg), PROJECT_YAML_NAME)


def target_watch_cursors(slug, arg=None):
    return os.path.join(target_project_dir(slug, arg), CURSORS_NAME)


def target_bus_dir(arg=None):
    return os.path.join(home(arg), TARGET_BUS_DIR)


def target_lane_token(lane, arg=None):
    digest = hashlib.sha256(os.path.realpath(lane).encode("utf-8")).hexdigest()
    return os.path.join(target_bus_dir(arg), "lanes", digest + ".json")


def target_ring_queue(arg=None):
    return os.path.join(target_bus_dir(arg), "ring-queue.json")


def target_plan_dir(slug, plan, arg=None):
    return os.path.join(target_project_dir(slug, arg), TARGET_PLANS_DIR, plan)


def target_run_dir(slug, plan, run, arg=None):
    return os.path.join(target_plan_dir(slug, plan, arg), "runs", run)


def target_task_dir(slug, plan, run, task, arg=None):
    return os.path.join(target_run_dir(slug, plan, run, arg), task)


# ---- self-test -------------------------------------------------------------------------------

def _check(cond, msg):
    if not cond:
        raise Fail(f"self-test: {msg}")



def _write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(doc, f)


def _read_json(path):
    with open(path) as f:
        return json.load(f)


def _seed_root(home_dir, slug, repo, data_dir_rel, db_path=None):
    """A root store with just the projects table the resolvers read (re-seedable).

    The store is created through mkdb.open_root: the single-writer guard (mkdb._single_writer)
    allows only mkdb.py to open SQLite read-write, and this is a fixture, so it borrows
    mkdb's own writer. mkpaths itself never writes a store — production code only ever calls
    _open_root_ro. mkdb is imported lazily, here in the self-test, because mkdb will delegate
    to this module in L3. `db_path` set is the C3 migrated marker; only the fixture writes it.
    """
    import sqlite3  # noqa: F401  (only the connect call is banned outside mkdb; none here)
    global _MIGRATED_MEMO
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("mkdb", os.path.join(here, "mkdb.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    con = mod.open_root(home_dir)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS projects("
                    "slug TEXT PRIMARY KEY, name TEXT, repo TEXT, data_dir TEXT, mode TEXT,"
                    " created TEXT, last_activity TEXT, db_path TEXT)")
        con.execute("DELETE FROM projects")
        con.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path)"
                    " VALUES (?,?,?,?,?,?)", (slug, slug, repo, data_dir_rel, "home", db_path))
        con.commit()
    finally:
        con.close()
    _MIGRATED_MEMO = None  # a re-seed changed the root; drop the memo this process may hold


def _register_project(tmp_home, slug, repo, data):
    """Register a project's root row (db_path NULL) and create its store. Insert-only: an
    existing row is left alone, so a migrated row is never cleared. Never touches other rows."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("mkdb", os.path.join(here, "mkdb.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    os.makedirs(data, exist_ok=True)
    con = mod.open_root(tmp_home)
    try:
        con.execute("INSERT OR IGNORE INTO projects(slug, name, repo, data_dir, mode, db_path)"
                    " VALUES (?,?,?,?,?,NULL)",
                    (slug, slug, os.path.realpath(repo), os.path.realpath(data), "home"))
        con.commit()
    finally:
        con.close()
    pcon = mod.open_project(tmp_home, slug, data_dir=data)
    try:
        pcon.execute("INSERT OR REPLACE INTO runs(id, plan, status) VALUES ('r1','p1','active')")
        pcon.commit()
    finally:
        pcon.close()
    mod.add_task(mod.open_project(tmp_home, slug, data_dir=data), "r1", "T1", "task", title="one")
    return data


def _migrate_fixture(tmp_home, repo, slug):
    """Register a project then run L2's real `mk-migrate.py --switch` on it. Returns the data dir.

    The migrated fixture is built through the production migration tool, never hand-written SQL:
    the switch sets projects.db_path, which is exactly the C3 marker these resolvers read. A
    minimal store (no plans, no runs, no lanes) is enough — quiesce passes on it.
    """
    import subprocess
    global _MIGRATED_MEMO
    data = os.path.join(repo, ".matrixkoder")
    os.makedirs(data, exist_ok=True)
    if not os.path.isdir(os.path.join(repo, ".git")):
        subprocess.run(["git", "init", "-q", repo], check=False)
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("mkdb", os.path.join(here, "mkdb.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    con = mod.open_root(tmp_home)
    try:
        con.execute("INSERT OR REPLACE INTO projects(slug, name, repo, data_dir, mode, db_path)"
                    " VALUES (?,?,?,?,?,NULL)", (slug, slug, repo, os.path.realpath(data), "home"))
        con.commit()
    finally:
        con.close()
    mod.open_project(tmp_home, slug, data_dir=data).close()
    proc = subprocess.run(
        [sys.executable, os.path.join(here, "mk-migrate.py"), "--switch", "--slug", slug,
         "--data", data, "--home", tmp_home, "--repo", repo],
        capture_output=True, text=True, env=dict(os.environ, MK_SELFTEST="1"))
    _check(proc.returncode == 0, f"mk-migrate --switch failed: {proc.stdout}{proc.stderr}")
    # a run → plan mapping in the switched store: run_dir/task_dir read it to place the C2 tree,
    # and the bus's store-kind writes (review/report artifacts) need the run's task row to exist
    con = mod.open_project(tmp_home, slug)
    try:
        con.execute("INSERT OR REPLACE INTO plans(id, version, title, status) VALUES ('p1',1,'P1','running')")
        con.execute("INSERT OR REPLACE INTO runs(id, plan, status) VALUES ('r1','p1','active')")
        con.commit()
    finally:
        con.close()
    mod.add_task(mod.open_project(tmp_home, slug), "r1", "T1", "task", title="one")
    _MIGRATED_MEMO = None
    return data


def _clear_home_env():
    for key in ("MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA", "MK_BUS_DIR", "MK_BUS_TOKEN_FILE"):
        os.environ.pop(key, None)


def _bus_gate(tmp_home, repo, migrated_data, plain_data):
    """C7: one temp bus (its own MK_BUS_DIR = tmp_home) serves a MIGRATED and a PRE-MIGRATION
    project at once: connect, inbox, status, commit, report over the bus for both, plus a
    reviewer's review-done for the migrated one. Drives the real `mk-builder.py` (present in the
    plugin checkout where gates run); skips when it is absent (an installed lane copy has no
    scripts/ tree). Returns None when skipped, else a check string."""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    builder = os.path.join(os.path.dirname(here), "child-skills", "mk-builder", "scripts",
                           "mk-builder.py")
    if not os.path.isfile(builder):
        return None
    bus = os.path.join(here, "mk-bus.py")
    env = dict(os.environ, MK_HOME=tmp_home, MATRIXKODER_HOME=tmp_home, MK_BUS_DIR=tmp_home,
               MK_SELFTEST="1")

    def run(args, cwd=None, timeout=60):
        return subprocess.run([sys.executable, *args], capture_output=True, text=True,
                              env=env, cwd=cwd, timeout=timeout)

    def jline(text):
        for line in reversed([x for x in text.splitlines() if x.strip()]):
            try:
                return json.loads(line)
            except ValueError:
                continue
        return {}

    # start the one bus on the migrated project's RESOLVED data dir. Post-switch that is the C2
    # folder (mkpaths.project_folder); the bus is launched by whoever holds the data dir, and
    # L6.1 is the phase that moves the bus itself. The pre-migration project resolves through its
    # token's slug either way, so one bus serves both.
    bus_data = project_folder(migrated_data)
    started = run([bus, "ensure", "--run", "r1", "--data", bus_data])
    _check(started.returncode == 0, f"bus ensure failed: {started.stdout}{started.stderr}")
    try:
        for slug, data in (("migs", migrated_data), ("plain", plain_data)):
            lane = os.path.join(tmp_home, f"gate-{slug}-lane")
            os.makedirs(lane, exist_ok=True)
            tok = run([bus, "token", "--lane", lane, "--task", "T1", "--run", "r1", "--data", data])
            _check(tok.returncode == 0 and jline(tok.stdout).get("ok"), f"{slug} token: {tok.stdout}")
            rep = os.path.join(lane, ".matrixkoder-report-r1.md")
            with open(rep, "w") as f:
                f.write("# report\n")
            conn = run([builder, "connect", "--lane", lane, "--data", data])
            _check(conn.returncode == 0, f"{slug} connect rc={conn.returncode}: {conn.stderr[-300:]}")
            st = run([builder, "start", "--run", "r1", "--task", "T1", "--round", "1",
                      "--role", "builder", "--report", ".matrixkoder-report-r1.md", "--lane", lane])
            _check(st.returncode == 0, f"{slug} start: {st.stdout}{st.stderr}")
            inbox = run([builder, "inbox", "--lane", lane])
            _check(inbox.returncode == 0, f"{slug} inbox rc={inbox.returncode}: {inbox.stderr[-200:]}")
            prog = run([builder, "progress", "working", "--lane", lane])
            _check(prog.returncode == 0, f"{slug} progress: {prog.stdout}{prog.stderr}")
            show = run([builder, "show", "--lane", lane])
            _check(jline(show.stdout).get("ok"), f"{slug} show: {show.stdout}")
            # commit: a real HEAD sha in the lane (the git guard is not on this PATH)
            subprocess.run(["git", "init", "-q", lane], check=False)
            subprocess.run(["git", "-C", lane, "config", "user.email", "g@example.com"], check=False)
            subprocess.run(["git", "-C", lane, "config", "user.name", "g"], check=False)
            subprocess.run(["git", "-C", lane, "add", "-A"], check=False)
            subprocess.run(["git", "-C", lane, "commit", "-q", "-m", "x"], check=False)
            sha = subprocess.run(["git", "-C", lane, "rev-parse", "HEAD"], capture_output=True,
                                 text=True).stdout.strip()
            cm = run([builder, "commit", sha, "--lane", lane])
            _check(jline(cm.stdout).get("ok"), f"{slug} commit: {cm.stdout}{cm.stderr}")
            rp = run([builder, "report", "--lane", lane])
            _check(jline(rp.stdout).get("ok"), f"{slug} report: {rp.stdout}{rp.stderr}")
        # a reviewer's review-done on the migrated project: start as the reviewer role first
        rlane = os.path.join(tmp_home, "gate-review-lane")
        os.makedirs(rlane, exist_ok=True)
        rrep = os.path.join(rlane, ".matrixkoder-review-r1.md")
        with open(rrep, "w") as f:
            f.write("# review\n")
        run([bus, "token", "--lane", rlane, "--task", "T1", "--run", "r1", "--data", migrated_data])
        conn = run([builder, "connect", "--lane", rlane, "--data", migrated_data])
        _check(conn.returncode == 0, f"review connect rc={conn.returncode}: {conn.stderr[-200:]}")
        rst = run([builder, "start", "--run", "r1", "--task", "T1", "--round", "1",
                   "--role", "review", "--report", ".matrixkoder-review-r1.md", "--lane", rlane])
        _check(rst.returncode == 0, f"review start: {rst.stdout}{rst.stderr}")
        rpr = run([builder, "progress", "reviewing", "--lane", rlane])
        _check(rpr.returncode == 0, f"review progress: {rpr.stdout}{rpr.stderr}")
        rd = run([builder, "review-done", "--verdict", "APPROVE", "--findings", "0",
                  "--path", ".matrixkoder-review-r1.md", "--lane", rlane])
        _check(rd.returncode == 0, f"review-done rc={rd.returncode}: {rd.stdout}{rd.stderr}")
    finally:
        bj = os.path.join(tmp_home, "bus.json")
        if os.path.isfile(bj):
            try:
                pid = (json.load(open(bj)) or {}).get("pid")
                if pid:
                    os.kill(pid, 9)
            except (OSError, ValueError):
                pass
    return "mixed-layout bus gate ok"


def self_test():
    import shutil
    import tempfile

    root = tempfile.mkdtemp(prefix="mkpaths-st-")
    saved_env = {k: os.environ.pop(k, None)
                 for k in ("MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA", "MK_BUS_DIR",
                           "MK_BUS_TOKEN_FILE", "HOME")}
    saved_cwd = os.getcwd()
    failures = []

    def t_l10(tmp_home):
        """L10.1: the two new pure paths (no mkdir, no write) and their id check."""
        _clear_home_env()
        os.environ["MK_HOME"] = tmp_home
        _check(mk_settings() == os.path.join(tmp_home, MK_SETTINGS_NAME), mk_settings())
        _check(mk_settings(tmp_home) == mk_settings(), mk_settings(tmp_home))
        _check(inbox_dir() == os.path.join(tmp_home, "inbox"), inbox_dir())
        _check(inbox_dir(plan_id="p1") == os.path.join(tmp_home, "inbox", "p1"), inbox_dir(plan_id="p1"))
        _check(inbox_dir(tmp_home, "p1") == inbox_dir(plan_id="p1"), "explicit home")
        _check(not os.path.exists(os.path.join(tmp_home, "inbox")), "inbox_dir must not create the folder")
        for bad in ("..", ".", "", "a/b", "../x", "a\\b", "a\0b"):
            try:
                inbox_dir(plan_id=bad)
            except Fail:
                continue
            raise Fail(f"self-test: inbox_dir accepted plan id {bad!r}")
        _clear_home_env()

    def t_db_names(tmp_home):
        """L10.13: the four states of a home / project folder, the kind rule, the legacy and NEW forms, and the
        rule that a read never creates, renames or moves a file."""
        _clear_home_env()
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = tmp_home
        base = os.path.join(root, "dbnames")

        def touch(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()

        def store(path):
            """A file that IS a store as far as the resolver can tell: the 16-byte SQLite header (L10.X)."""
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(SQLITE_MAGIC + b"\0" * 84)

        def listing(d):
            return sorted(os.listdir(d)) if os.path.isdir(d) else []

        # the names themselves
        _check((ROOT_DB_NAME, PROJECT_DB_NAME, LEGACY_DB_NAME) == ("mk-overview.db", "mk-project.db", "mk.db"), "names")
        _check(set(DB_FILE_NAMES) == {n + x for n in ("mk-overview.db", "mk-project.db", "mk.db") for x in ("", "-wal", "-shm")}, DB_FILE_NAMES)
        for kind, fn, new in (("root", lambda d: _resolve_db(d, "root"), "mk-overview.db"),
                              ("project", lambda d: _resolve_db(d, "project"), "mk-project.db")):
            d = os.path.join(base, kind + "-fresh")  # fresh (no folder, no file): the NEW name
            _check(fn(d) == os.path.join(d, new), fn(d))
            d = os.path.join(base, kind + "-old")  # old only: the existing mk.db keeps resolving
            touch(os.path.join(d, "mk.db"))
            _check(fn(d) == os.path.join(d, "mk.db"), fn(d))
            _check(listing(d) == ["mk.db"], listing(d))  # nothing created beside it
            d = os.path.join(base, kind + "-new")  # new only
            store(os.path.join(d, new))
            _check(fn(d) == os.path.join(d, new), fn(d))
            d = os.path.join(base, kind + "-both")  # both real: the new name wins (only a deliberate step makes it)
            store(os.path.join(d, "mk.db"))
            store(os.path.join(d, new))
            _check(fn(d) == os.path.join(d, new), fn(d))
            _check(listing(d) == sorted(["mk.db", new]), listing(d))
            # L10.X: an EMPTY or foreign new-name file beside mk.db is no store and never hides it
            d = os.path.join(base, kind + "-empty-new")
            touch(os.path.join(d, "mk.db"))
            touch(os.path.join(d, new))
            _check(fn(d) == os.path.join(d, "mk.db"), f"{kind}: a 0-byte {new} hijacked the store: {fn(d)}")
            with open(os.path.join(d, new), "wb") as f:
                f.write(b"not a sqlite file at all")
            _check(fn(d) == os.path.join(d, "mk.db"), f"{kind}: a foreign {new} hijacked the store: {fn(d)}")
            _check(listing(d) == sorted(["mk.db", new]), listing(d))  # the resolver deleted nothing
            d = os.path.join(base, kind + "-empty-new-alone")  # no mk.db: the (empty) new name stays the answer
            touch(os.path.join(d, new))
            _check(fn(d) == os.path.join(d, new), fn(d))
        # the project file must not be mistaken for the root one, and the other way round
        d = os.path.join(base, "cross")
        store(os.path.join(d, "mk-project.db"))
        _check(_resolve_db(d, "root") == os.path.join(d, "mk-overview.db"), _resolve_db(d, "root"))
        # the explicit-kind forms never guess: a root dir that is NOT the process home is still a root store
        other = os.path.join(base, "other-home")
        _check(root_db_in(other) == os.path.join(other, "mk-overview.db") and project_db_in(other) == os.path.join(other, "mk-project.db"), "explicit kinds")
        _check(db_in(other) == os.path.join(other, "mk-project.db"), "db_in guesses project for a non-home folder")
        touch(os.path.join(other, "mk.db"))
        _check(root_db_in(other) == project_db_in(other) == os.path.join(other, "mk.db"), "existing legacy store found by either kind")
        # kind rule for db_in: the MK home is root, any other folder is project
        _check(db_in(tmp_home) == os.path.join(tmp_home, "mk-overview.db"), db_in(tmp_home))
        _check(root_db() == db_in(tmp_home) == root_db(tmp_home), root_db())
        _check(db_in(os.path.join(base, "elsewhere")) == os.path.join(base, "elsewhere", "mk-project.db"), "project kind")
        touch(os.path.join(tmp_home, "mk.db"))  # an existing legacy root store keeps being the root store
        _check(root_db() == db_in(tmp_home) == os.path.join(tmp_home, "mk.db"), root_db())
        _check(db_files_in(tmp_home) == [os.path.join(tmp_home, n) for n in ("mk.db", "mk.db-wal", "mk.db-shm")], db_files_in(tmp_home))
        os.remove(os.path.join(tmp_home, "mk.db"))
        # project_db / target_project_db follow the same rule
        pd = os.path.join(base, "projdata")
        _check(project_db(data=pd) == os.path.join(pd, "mk-project.db") == project_db(data=pd, legacy=True), project_db(data=pd))
        touch(os.path.join(pd, "mk.db"))
        _check(project_db(data=pd) == os.path.join(pd, "mk.db") == project_db(data=pd, legacy=True), project_db(data=pd))
        _check(target_project_db("slug1") == os.path.join(tmp_home, "projects", "slug1", "mk-project.db"), target_project_db("slug1"))
        touch(os.path.join(tmp_home, "projects", "slug2", "mk.db"))
        _check(target_project_db("slug2") == os.path.join(tmp_home, "projects", "slug2", "mk.db"), target_project_db("slug2"))
        # mk-migrate's forms: the OLD name literally, the NEW name whether or not it exists
        _check(legacy_db_in(pd) == os.path.join(pd, "mk.db"), legacy_db_in(pd))
        _check(new_db_in(pd, "project") == os.path.join(pd, "mk-project.db"), new_db_in(pd, "project"))
        _check(new_db_in(tmp_home, "root") == os.path.join(tmp_home, "mk-overview.db"), new_db_in(tmp_home, "root"))
        try:
            new_db_in(pd, "bogus")
        except Fail:
            pass
        else:
            raise Fail("self-test: new_db_in accepted a bogus kind")
        # the overrides still rename the new names (the canary), and a bad override is refused
        _check(_name("MK_NAME_SELFTEST_ONLY", "x.db") == "x.db", "unset env = default")  # an env name the canary never sets
        os.environ["MK_NAME_SELFTEST_ONLY"] = "y.db"
        _check(_name("MK_NAME_SELFTEST_ONLY", "x.db") == "y.db", "override")
        os.environ["MK_NAME_SELFTEST_ONLY"] = "../evil.db"
        try:
            _name("MK_NAME_SELFTEST_ONLY", "x.db")
        except RuntimeError:
            pass
        else:
            raise Fail("self-test: a path-like name override was accepted")
        os.environ.pop("MK_NAME_SELFTEST_ONLY")
        _clear_home_env()

    def t_names():
        """L10.12a: the folder-literal helpers join exactly what the call sites used to spell."""
        d = os.path.join(root, "names")
        _check(db_in(d) == os.path.join(d, PROJECT_DB_NAME), db_in(d))  # a fresh non-home folder: the project name
        _check(db_files_in(d) == [os.path.join(d, n) for n in (PROJECT_DB_NAME, PROJECT_DB_NAME + "-wal", PROJECT_DB_NAME + "-shm")], db_files_in(d))
        _check(legacy_yaml(d) == os.path.join(d, LEGACY_YAML_NAME), legacy_yaml(d))
        _check(project_yaml_in(d) == os.path.join(d, PROJECT_YAML_NAME), project_yaml_in(d))
        _check(legacy_lanes(d) == os.path.join(d, LANES_NAME) == lanes_file(d), legacy_lanes(d))
        _check(legacy_cursors(d) == os.path.join(d, CURSORS_NAME), legacy_cursors(d))
        r, t = os.path.join(d, "runs", "r1"), os.path.join(d, "runs", "r1", "tasks", "T")
        _check(legacy_run_dir(d, "r1") == r, legacy_run_dir(d, "r1"))
        _check(run_doc_in(r) == os.path.join(r, RUN_DOC_NAME) and events_doc_in(r) == os.path.join(r, EVENTS_DOC_NAME)
               and board_doc_in(r) == os.path.join(r, BOARD_DOC_NAME) and task_doc_in(t) == os.path.join(t, TASK_DOC_NAME), "docs")
        _check(VIEW_DOC_NAMES == (RUN_DOC_NAME, TASK_DOC_NAME, EVENTS_DOC_NAME, BOARD_DOC_NAME), VIEW_DOC_NAMES)
        _check((LANES_NAME, CURSORS_NAME) == (LANES_NAME, CURSORS_NAME), "names")
        _check(not os.path.exists(d), "a path helper created a folder")

    def t_home(tmp_home):
        os.environ["HOME"] = tmp_home
        _clear_home_env()
        # the real production path: both names unset, resolved against $HOME
        _check(home() == os.path.join(tmp_home, ".matrixkoder"), home())
        # either name, one home
        os.environ["MK_HOME"] = tmp_home
        _check(home() == tmp_home, home())
        os.environ.pop("MK_HOME")
        os.environ["MATRIXKODER_HOME"] = tmp_home
        _check(home() == tmp_home, home())
        # both set, one home: accepted, as mkdb.resolve_home accepts it
        os.environ["MK_HOME"] = tmp_home
        _check(home() == tmp_home, home())
        # the same directory spelled two ways is still one home (realpath comparison)
        os.environ["MK_HOME"] = os.path.join(tmp_home, "a", "..")
        _check(home() == tmp_home, home())
        # an explicit arg wins over both names
        _check(home(os.path.join(root, "arg-home")) == os.path.join(root, "arg-home"), "arg")
        # and a disagreement is refused, as resolve_home refuses it
        os.environ["MK_HOME"] = os.path.join(root, "one")
        os.environ["MATRIXKODER_HOME"] = os.path.join(root, "two")
        try:
            home()
            failures.append("disagreeing home names must fail")
        except Fail as e:
            _check("differ; set one MK home" in str(e), str(e))

    def t_data_dir(tmp_home, repo, worktree):
        os.environ["HOME"] = tmp_home
        _clear_home_env()
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = tmp_home
        explicit, env_data = os.path.join(root, "explicit"), os.path.join(root, "envdata")
        os.environ["MATRIXKODER_DATA"] = env_data
        # 1. --data wins over the env
        _check(data_dir(explicit) == explicit, data_dir(explicit))
        # 2. the env beats every repo lookup
        os.chdir(repo)
        _check(data_dir() == env_data, data_dir())
        os.environ.pop("MATRIXKODER_DATA")
        # 3. a linked worktree maps to the MAIN checkout's data dir
        os.chdir(worktree)
        _check(data_dir() == os.path.join(repo, ".matrixkoder"), data_dir())
        _check(data_dir(required=False) == os.path.join(repo, ".matrixkoder"), "required=False")
        # 4. the root projects table maps the repo to its data dir
        _seed_root(tmp_home, "matrixkoder", repo, "custom/mk")
        _check(data_dir() == os.path.normpath(os.path.join(repo, "custom/mk")), data_dir())
        # 5. projects.json maps it too; the root table wins over the file
        _write_json(os.path.join(tmp_home, "projects.json"), {repo: {"data_dir": "from-json"}})
        _check(data_dir() == os.path.normpath(os.path.join(repo, "custom/mk")), "root table first")
        # an empty root data_dir falls through to <home>/projects.json
        _seed_root(tmp_home, "matrixkoder", repo, "")
        _check(data_dir() == os.path.normpath(os.path.join(repo, "from-json")), data_dir())
        # 6. no repo above cwd: Fail, or None when required=False
        os.chdir(tmp_home)
        try:
            data_dir()
            failures.append("no repo above cwd must fail")
        except Fail as e:
            _check("no git toplevel" in str(e), str(e))
        _check(data_dir(required=False) is None, data_dir(required=False))

    def t_folders(tmp_home, repo):
        os.environ["HOME"] = tmp_home
        _clear_home_env()
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = tmp_home
        data = os.path.join(repo, ".matrixkoder")
        _check(runs_dir(data) == os.path.join(data, "runs"), runs_dir(data))
        _check(run_dir(data, "r1") == os.path.join(data, "runs", "r1"), run_dir(data, "r1"))
        _check(tasks_dir(data, "r1") == os.path.join(data, "runs", "r1", "tasks"), tasks_dir(data, "r1"))
        _check(task_dir(data, "r1", "T1") == os.path.join(data, "runs", "r1", "tasks", "T1"),
               task_dir(data, "r1", "T1"))
        _check(plan_dir(data, "p1") == os.path.join(data, "plans", "p1"), plan_dir(data, "p1"))
        _check(plan_archives_dir(data) == os.path.join(data, "plans", "archive"),
               plan_archives_dir(data))
        _check(watch_cursors(data) == os.path.join(data, CURSORS_NAME), watch_cursors(data))
        _check(usage_ledger() == os.path.join(tmp_home, "usage.json"), usage_ledger())
        _check(nudge_state(data) == os.path.join(data, "nudge-state.json"), nudge_state(data))
        os.environ["MK_HOME"] = tmp_home
        _check(nudge_state(None) == os.path.join(tmp_home, "nudge-state.json"), nudge_state(None))

    def t_project_db(tmp_home, repo):
        os.environ["HOME"] = tmp_home
        _clear_home_env()
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = tmp_home
        # the bus resolves a data_dir recorded relative in the root table against cwd: keep
        # that rule (mk-bus.store_for's), and prove it by running from the repo, as the bus does
        os.chdir(repo)
        _seed_root(tmp_home, "matrixkoder", repo, ".matrixkoder")
        data = os.path.realpath(os.path.join(repo, ".matrixkoder"))
        _check(project_db(slug="matrixkoder") == os.path.join(data, PROJECT_DB_NAME),
               project_db(slug="matrixkoder"))
        _check(project_db(data=data) == os.path.join(data, PROJECT_DB_NAME), project_db(data=data))
        _check(project_db(slug="nope") is None, project_db(slug="nope"))
        _check(project_slug_of(data) == "matrixkoder", project_slug_of(data))
        _check(project_slug_of(os.path.join(root, "nowhere")) is None, project_slug_of("x"))

    def t_canonical(tmp_home, repo):
        """Z19: one data dir named by several root rows — the canonical row wins: the row
        whose db_path folder is <home>/projects/<that row's slug>; else any matching row
        with db_path set; else the first match (an unmigrated dir keeps today's rule)."""
        os.environ["HOME"] = tmp_home
        _clear_home_env()
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = tmp_home
        os.chdir(repo)
        data = os.path.realpath(os.path.join(repo, ".matrixkoder"))
        canon = os.path.join(tmp_home, "projects", "matrixkoder")
        spec = importlib.util.spec_from_file_location(
            "mkdb", os.path.join(os.path.dirname(os.path.abspath(__file__)), "mkdb.py"))
        mkdb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mkdb)
        # case 1: the alias row first, the canonical row last; only the canonical row is
        # migrated, and both name the same data dir — the slug must not follow the alias.
        _seed_root(tmp_home, "pdceb35c892fc86bb", repo, ".matrixkoder")
        con = mkdb.open_root(tmp_home)
        try:
            con.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path)"
                        " VALUES (?,?,?,?,?,?)",
                        ("matrixkoder", "matrixkoder", repo, ".matrixkoder", "home",
                         os.path.join(canon, ROOT_DB_NAME)))
            con.commit()
        finally:
            con.close()
        _check(project_slug_of(data) == "matrixkoder", project_slug_of(data))
        _check(project_folder(data) == canon, project_folder(data))
        # case 2: the same two rows in reverse order — row order must not decide the answer.
        _seed_root(tmp_home, "matrixkoder", repo, ".matrixkoder", os.path.join(canon, ROOT_DB_NAME))
        con = mkdb.open_root(tmp_home)
        try:
            con.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path)"
                        " VALUES (?,?,?,?,?,NULL)",
                        ("pdceb35c892fc86bb", "pdceb35c892fc86bb", repo, ".matrixkoder", "home"))
            con.commit()
        finally:
            con.close()
        _check(project_slug_of(data) == "matrixkoder", project_slug_of(data))
        _check(target_project_yaml("matrixkoder") == os.path.join(canon, PROJECT_YAML_NAME),
               target_project_yaml("matrixkoder"))
        _check(target_run_dir("matrixkoder", "p1", "r1")
               == os.path.join(canon, "plans", "p1", "runs", "r1"),
               target_run_dir("matrixkoder", "p1", "r1"))
        # case 3: for that data dir the C2 targets and the project folder resolve under
        # projects/<canonical>/ — target_* are slug-driven, project_folder follows the switch.
        _seed_root(tmp_home, "pdceb35c892fc86bb", repo, ".matrixkoder")
        con = mkdb.open_root(tmp_home)
        try:
            con.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path)"
                        " VALUES (?,?,?,?,?,?)",
                        ("matrixkoder", "matrixkoder", repo, ".matrixkoder", "home",
                         os.path.join(canon, ROOT_DB_NAME)))
            con.commit()
        finally:
            con.close()
        _check(target_project_yaml("matrixkoder") == os.path.join(canon, PROJECT_YAML_NAME),
               target_project_yaml("matrixkoder"))
        _check(target_run_dir("matrixkoder", "p1", "r1")
               == os.path.join(canon, "plans", "p1", "runs", "r1"),
               target_run_dir("matrixkoder", "p1", "r1"))
        _check(project_folder(data) == canon, project_folder(data))
        # case 4: no row carries db_path — an unmigrated dir keeps today's FIRST-MATCH rule.
        _seed_root(tmp_home, "pdceb35c892fc86bb", repo, ".matrixkoder")
        con = mkdb.open_root(tmp_home)
        try:
            con.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path)"
                        " VALUES (?,?,?,?,?,NULL)",
                        ("matrixkoder", "matrixkoder", repo, ".matrixkoder", "home"))
            con.commit()
        finally:
            con.close()
        _check(project_slug_of(data) == "pdceb35c892fc86bb", project_slug_of(data))

    def t_canonical_folder(tmp_home, repo):
        """Z22: two root rows share a data dir (alias, no db_path; canonical, db_path under
        projects/matrixkoder). project_folder/project_yaml must follow the canonical row in
        EITHER row order; with no db_path anywhere the data dir itself is the folder."""
        global _MIGRATED_MEMO
        os.environ["HOME"] = tmp_home
        _clear_home_env()
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = tmp_home
        os.chdir(repo)
        data = os.path.realpath(os.path.join(repo, ".matrixkoder"))
        canon = os.path.join(tmp_home, "projects", "matrixkoder")
        spec = importlib.util.spec_from_file_location(
            "mkdb", os.path.join(os.path.dirname(os.path.abspath(__file__)), "mkdb.py"))
        mkdb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mkdb)

        def add(slug, db_path):
            con = mkdb.open_root(tmp_home)
            try:
                con.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path)"
                            " VALUES (?,?,?,?,?,?)",
                            (slug, slug, repo, ".matrixkoder", "home", db_path))
                con.commit()
            finally:
                con.close()
            _MIGRATED_MEMO = None  # same-process reseed may keep the root stamp; drop the memo

        def answers():
            return (project_folder(data), project_yaml(data), project_yaml_lock(data))

        want = (canon, os.path.join(canon, PROJECT_YAML_NAME),
                os.path.join(canon, PROJECT_YAML_NAME) + ".lock")
        # (a) canonical row FIRST, alias row last — the old last-row-wins by_data picked the alias
        _seed_root(tmp_home, "matrixkoder", repo, ".matrixkoder", os.path.join(canon, ROOT_DB_NAME))
        add("pdceb35c892fc86bb", None)
        _check(answers() == want, "canonical first: %r" % (answers(),))
        # (b) alias row FIRST, canonical last — identical answers
        _seed_root(tmp_home, "pdceb35c892fc86bb", repo, ".matrixkoder")
        add("matrixkoder", os.path.join(canon, ROOT_DB_NAME))
        _check(answers() == want, "alias first: %r" % (answers(),))
        # (c) unmigrated: two rows, no db_path — the data dir stays the folder
        _seed_root(tmp_home, "pdceb35c892fc86bb", repo, ".matrixkoder")
        add("matrixkoder", None)
        _check(project_folder(data) == data, project_folder(data))
        _check(project_yaml(data) == os.path.join(data, LEGACY_YAML_NAME), project_yaml(data))

    def t_bus(tmp_home, repo):
        os.environ["HOME"] = tmp_home
        _clear_home_env()
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = tmp_home
        os.chdir(repo)
        data = data_dir()
        # file mode: the data dir, as before
        _check(bus_dir(data) == data, bus_dir(data))
        _check(ring_queue(data) == os.path.join(data, "ring-queue.json"), ring_queue(data))
        _check(bus_json(data) == os.path.join(data, "bus.json"), bus_json(data))
        # store mode: the MK home's bus/ folder (C6, L6.1), one bus for every project
        os.makedirs(data, exist_ok=True)
        open(os.path.join(data, PROJECT_DB_NAME), "w").close()
        _check(bus_dir(data) == os.path.join(tmp_home, "bus"), bus_dir(data))
        _check(bus_json(data) == os.path.join(tmp_home, "bus", "bus.json"), bus_json(data))
        _check(bus_tokens(data) == os.path.join(tmp_home, "bus", "bus-tokens.json"), bus_tokens(data))
        _check(bus_tokens_lock(data) == os.path.join(tmp_home, "bus", "bus-tokens.lock"),
               bus_tokens_lock(data))
        _check(bus_lock(data) == os.path.join(tmp_home, "bus", "bus.lock"), bus_lock(data))
        _check(ring_queue(data) == os.path.join(tmp_home, "bus", "ring-queue.json"), ring_queue(data))
        # the pre-L6.1 files are read once and copied over, never deleted or rewritten
        legacy_bus = os.path.join(tmp_home, "bus.json")
        _write_json(legacy_bus, {"pid": 7, "port": 1234, "token": "t" * 20})
        _write_json(os.path.join(tmp_home, "bus-tokens.json"), {"tok": {"role": "orch"}})
        _write_json(os.path.join(tmp_home, "ring-queue.json"), [{"id": "m-1"}])
        got = bus_json(data)
        _check(got == os.path.join(tmp_home, "bus", "bus.json"), got)
        _check(_read_json(got) == {"pid": 7, "port": 1234, "token": "t" * 20}, _read_json(got))
        _check(_read_json(bus_tokens(data)) == {"tok": {"role": "orch"}}, bus_tokens(data))
        _check(_read_json(ring_queue(data)) == [{"id": "m-1"}], ring_queue(data))
        _check(os.path.isfile(legacy_bus), "the legacy bus.json must survive a take-over")
        # a second read never re-copies: the new file wins and the legacy bytes stay put
        _write_json(legacy_bus, {"pid": 99})
        _check(_read_json(bus_json(data)) == {"pid": 7, "port": 1234, "token": "t" * 20},
               "the new file must win once it exists")
        # $MK_BUS_DIR overrides both: the seam pins the bus dir itself, files inside it
        pinned = os.path.join(root, "pinned-bus")
        os.environ["MK_BUS_DIR"] = pinned
        _check(bus_dir(data) == pinned, bus_dir(data))
        _check(ring_queue(data) == os.path.join(pinned, "ring-queue.json"), ring_queue(data))
        _check(bus_json(data) == os.path.join(pinned, "bus.json"), bus_json(data))
        os.environ.pop("MK_BUS_DIR")
        # the lane token: one rule for mk-bus, mk-lane-event and mk-builder
        lane = os.path.join(root, "lane")
        os.makedirs(lane, exist_ok=True)
        digest = hashlib.sha256(os.path.realpath(lane).encode("utf-8")).hexdigest()
        _check(lane_token(lane) == os.path.join(tmp_home, "bus", "lanes", digest + ".json"),
               lane_token(lane))
        # an armed lane's pre-L6.1 token file is read once and copied over
        new_tok = os.path.join(tmp_home, "bus", "lanes", digest + ".json")
        old_tok = os.path.join(tmp_home, BUS_LANES_DIR, digest + ".json")
        _write_json(old_tok, {"token": "armed"})
        _write_json(new_tok, {"token": "placeholder"})
        os.remove(new_tok)
        _check(_read_json(lane_token(lane)) == {"token": "armed"}, _read_json(lane_token(lane)))
        _check(os.path.isfile(old_tok), "the legacy lane token must survive")
        # file mode keeps the token inside the lane; a file there wins even in store mode
        inside = os.path.join(lane, LANE_BUS_FILE)
        open(inside, "w").close()
        _check(lane_token(lane) == inside, lane_token(lane))
        _check(lane_token(lane, file_mode=True) == inside, lane_token(lane, file_mode=True))
        os.unlink(inside)
        _check(lane_token(lane, file_mode=True) == inside, "file mode")
        # $MK_BUS_TOKEN_FILE overrides the whole path
        os.environ["MK_BUS_TOKEN_FILE"] = os.path.join(root, "tok.json")
        _check(lane_token(lane) == os.path.join(root, "tok.json"), lane_token(lane))
        os.environ.pop("MK_BUS_TOKEN_FILE")
        _check(target_lane_token(lane) == os.path.join(tmp_home, "bus", "lanes", digest + ".json"),
               target_lane_token(lane))
        # the lane token follows the bus dir: with $MK_BUS_DIR it is directly in the pin, and
        # the pre-L6.1 <pin>/bus-lanes/<hash>.json is read once and copied over
        os.environ["MK_BUS_DIR"] = pinned
        _check(lane_token(lane) == os.path.join(pinned, "lanes", digest + ".json"),
               lane_token(lane))
        _write_json(os.path.join(pinned, "bus-lanes", digest + ".json"), {"token": "pinned"})
        _check(_read_json(lane_token(lane)) == {"token": "pinned"}, lane_token(lane))
        os.environ.pop("MK_BUS_DIR")

    def t_take_over_race():
        """C6 (review HIGH 1): the read-once copy behind every bus / lane-token /
        watch-cursor getter must be race-safe. Several processes take the SAME file over
        at once — two lanes arming against one bus, an orchestrator watch beside a bus
        command — and the pre-fix FIXED scratch name let one racer's os.replace delete
        another's temp mid-write: either a truncated file was installed at the new path,
        or one copier answered old_path while the rest answered new_path. The scratch copy
        is therefore per copier. Twelve threads per round on a fresh target, thirty
        rounds: no exception, one identical answer, the old bytes intact, nothing left
        behind. The rule reads no env, so this case needs no home fixture."""
        import threading

        blob = b'{"k": "' + b"x" * 200000 + b'"}'
        fails, answers = [], []
        base = os.path.join(root, "take-over-race")
        bus_sub = os.path.join(base, "bus")
        old_path = os.path.join(base, "bus-tokens.json")
        new_path = os.path.join(bus_sub, "bus-tokens.json")

        def copier(i):
            barrier.wait(timeout=30)
            try:
                answers.append(_take_over(new_path, old_path))
            except OSError as e:
                fails.append(type(e).__name__)

        try:
            os.makedirs(base, exist_ok=True)
            with open(old_path, "wb") as f:
                f.write(blob)
            for rnd in range(30):
                shutil.rmtree(bus_sub, ignore_errors=True)
                fails.clear()
                answers.clear()
                barrier = threading.Barrier(12)
                threads = [threading.Thread(target=copier, args=(i,)) for i in range(12)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(60)
                _check(not fails, "round %d: copiers raised %s" % (rnd, sorted(set(fails))))
                _check(len(answers) == 12 and all(a == new_path for a in answers),
                       "round %d: every copier must answer the new path, got %s"
                       % (rnd, sorted({str(a) for a in answers})))
                with open(new_path, "rb") as f:
                    _check(f.read() == blob, "round %d: the new file must hold the old bytes" % rnd)
                _check(oct(os.stat(new_path).st_mode & 0o777) == "0o600",
                       "round %d: mode %s" % (rnd, oct(os.stat(new_path).st_mode & 0o777)))
                _check(sorted(os.listdir(bus_sub)) == ["bus-tokens.json"],
                       "round %d: a scratch copy was left behind: %s"
                       % (rnd, os.listdir(bus_sub)))
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def t_migrated(tmp_home):
        """C1/C3 (L3): a migrated project (root db_path set by L2's mk-migrate --switch) resolves
        every path to the C2 target; a pre-migration project beside it keeps today's paths, and
        a missing root / unknown slug falls back without raising."""
        global _MIGRATED_MEMO
        _clear_home_env()
        os.environ["HOME"] = tmp_home
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = tmp_home
        repo = os.path.join(root, "mig-repo")
        os.makedirs(repo, exist_ok=True)
        data = _migrate_fixture(tmp_home, repo, "migs")
        proj = os.path.join(tmp_home, "projects", "migs")
        # slug form: every path follows the switch
        _check(project_dir(home_arg=tmp_home, slug="migs") == proj, project_dir(home_arg=tmp_home, slug="migs"))
        _check(project_db(slug="migs") == os.path.join(proj, PROJECT_DB_NAME), project_db(slug="migs"))
        _check(project_yaml(data) == os.path.join(proj, PROJECT_YAML_NAME), project_yaml(data))
        _check(plan_dir(data, "p1") == os.path.join(proj, "plans", "p1"), plan_dir(data, "p1"))
        _check(plan_file(data, "p1") == os.path.join(proj, "plans", "p1", "plan.md"), plan_file(data, "p1"))
        _check(plan_archives_dir(data, "p1") == os.path.join(proj, "plans", "p1", "archive"),
               plan_archives_dir(data, "p1"))
        _check(project_folder(data) == proj, project_folder(data))
        # L10.1: the --data form of a MIGRATED project resolves to its C2 folder (fails on the old
        # literal rule); the old folder only on request, which is what mk-migrate's rollback asks for
        _check(project_dir(home_arg=tmp_home, data_dir=data) == proj, project_dir(home_arg=tmp_home, data_dir=data))
        _check(project_dir(home_arg=tmp_home, data_dir=data, legacy=True) == data,
               project_dir(home_arg=tmp_home, data_dir=data, legacy=True))
        _check(project_dir(home_arg=tmp_home, data_dir=proj) == proj, "C2 folder maps to itself")
        # the run/task family resolves under the plan the store records for the run
        _check(run_dir(data, "r1") == os.path.join(proj, "plans", "p1", "runs", "r1"),
               run_dir(data, "r1"))
        _check(task_dir(data, "r1", "T1") == os.path.join(proj, "plans", "p1", "runs", "r1", "T1"),
               task_dir(data, "r1", "T1"))
        # Z23: the C2 project folder as --data answers the same as the legacy form; an unknown run,
        # an unmigrated folder and a non-project folder keep today's <data>/runs/** paths
        _check(run_dir(proj, "r1") == run_dir(data, "r1"), run_dir(proj, "r1"))
        _check(task_dir(proj, "r1", "T1") == task_dir(data, "r1", "T1"), task_dir(proj, "r1", "T1"))
        _check(migrated_slug(data=proj) == "migs", migrated_slug(data=proj))
        _check(task_dir(proj, "nope", "T1") == os.path.join(proj, "runs", "nope", "tasks", "T1"),
               task_dir(proj, "nope", "T1"))
        # a p<hash> alias row ahead of the real row shares the db_path (live store today): the row
        # named like the folder still wins
        import sqlite3 as _sq
        rcon = _sq.connect(os.path.join(tmp_home, ROOT_DB_NAME))
        cols = "name, repo, data_dir, mode, db_path"
        try:
            rcon.execute(f"INSERT INTO projects(slug, {cols}) SELECT 'pzz', {cols} FROM projects WHERE slug='migs'")
            rcon.execute("DELETE FROM projects WHERE slug='migs'")
            rcon.execute(f"INSERT INTO projects(slug, {cols}) SELECT 'migs', {cols} FROM projects WHERE slug='pzz'")
            rcon.commit()
        finally:
            rcon.close()
        _check(migrated_slug(data=proj) == "migs", migrated_slug(data=proj))
        _check(task_dir(proj, "r1", "T1") == task_dir(data, "r1", "T1"), task_dir(proj, "r1", "T1"))
        for plain in (os.path.join(tmp_home, "projects", "unmigrated"), os.path.join(root, "elsewhere")):
            _check(migrated_slug(data=plain) is None, migrated_slug(data=plain))
            _check(task_dir(plain, "r1", "T1") == os.path.join(plain, "runs", "r1", "tasks", "T1"),
                   task_dir(plain, "r1", "T1"))
        # L10.1: the data form of project_db follows the switch to the C2 store; legacy=True is the
        # literal <data>/mk.db that mk-migrate's rollback reads (its copy-back must not follow)
        _check(project_db(data=data) == os.path.join(proj, PROJECT_DB_NAME), project_db(data=data))
        _check(project_db(data=data) == project_db(slug="migs"), "data form equals slug form")
        _check(project_db(data=data, legacy=True) == os.path.join(data, PROJECT_DB_NAME), project_db(data=data, legacy=True))
        # an unmigrated project is byte-for-byte unchanged, legacy flag or not
        for plain in (os.path.join(root, "elsewhere"), os.path.join(tmp_home, "projects", "unmigrated")):
            _check(project_db(data=plain) == os.path.join(plain, PROJECT_DB_NAME) == project_db(data=plain, legacy=True),
                   project_db(data=plain))
            _check(project_dir(home_arg=tmp_home, data_dir=plain) == plain, project_dir(home_arg=tmp_home, data_dir=plain))
        # C6 (L6.1): the watch cursors move into the project folder with the project; the
        # pre-migration file is read once and copied over, never deleted
        _check(watch_cursors(data) == os.path.join(proj, CURSORS_NAME), watch_cursors(data))
        _write_json(os.path.join(proj, CURSORS_NAME), {"r1|*": {"seq": 3}})
        _write_json(os.path.join(data, CURSORS_NAME), {"r1|*": {"seq": 9}})
        os.remove(os.path.join(proj, CURSORS_NAME))
        _check(_read_json(watch_cursors(data)) == {"r1|*": {"seq": 9}}, watch_cursors(data))
        _check(os.path.isfile(os.path.join(data, CURSORS_NAME)),
               "the legacy cursor file must survive a take-over")

        # a pre-migration project side by side keeps every path today's
        other_repo = os.path.join(root, "plain-repo")
        other_data = os.path.join(other_repo, ".matrixkoder")
        os.makedirs(other_data, exist_ok=True)
        # C7 gate: one temp bus serves the migrated project AND this pre-migration one.
        # The plain project needs its own root row (db_path NULL) and its <data>/mk.db, so the
        # bus's store_for resolves it to today's store. Insert-only: never clear the migrated row.
        _register_project(tmp_home, "plain", other_repo, other_data)
        gate = _bus_gate(tmp_home, other_repo, data, other_data)
        if gate:
            _check(gate == "mixed-layout bus gate ok", gate)
        _check(project_yaml(other_data) == os.path.join(other_data, LEGACY_YAML_NAME),
               project_yaml(other_data))
        _check(plan_file(other_data, "p1") == os.path.join(other_data, "plans", "p1.md"),
               plan_file(other_data, "p1"))
        _check(plan_archives_dir(other_data, "p1") == os.path.join(other_data, "plans", "archive"),
               plan_archives_dir(other_data, "p1"))
        _check(run_dir(other_data, "r1") == os.path.join(other_data, "runs", "r1"), run_dir(other_data, "r1"))
        _check(task_dir(other_data, "r1", "T1") == os.path.join(other_data, "runs", "r1", "tasks", "T1"),
               task_dir(other_data, "r1", "T1"))
        # an unknown slug and a missing root fall back, never raise
        _check(project_db(slug="nope") is None, "unknown slug")
        missing = os.path.join(root, "no-such-home")
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = missing
        _MIGRATED_MEMO = None
        _check(project_yaml(other_data) == os.path.join(other_data, LEGACY_YAML_NAME), "no root")
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = tmp_home
        _MIGRATED_MEMO = None

    def t_target(tmp_home):
        _clear_home_env()
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = tmp_home
        proj = os.path.join(tmp_home, "projects", "matrixkoder")
        _check(target_projects_dir() == os.path.join(tmp_home, "projects"), target_projects_dir())
        _check(target_project_dir("matrixkoder") == proj, target_project_dir("matrixkoder"))
        _check(target_project_db("matrixkoder") == os.path.join(proj, PROJECT_DB_NAME), "target db")
        _check(target_project_yaml("matrixkoder") == os.path.join(proj, PROJECT_YAML_NAME), "yaml")
        _check(target_watch_cursors("matrixkoder")
               == os.path.join(proj, CURSORS_NAME), "cursors")
        _check(target_bus_dir() == os.path.join(tmp_home, "bus"), target_bus_dir())
        _check(target_plan_dir("matrixkoder", "p1") == os.path.join(proj, "plans", "p1"), "plan")
        _check(target_run_dir("matrixkoder", "p1", "r1")
               == os.path.join(proj, "plans", "p1", "runs", "r1"), "run")
        _check(target_task_dir("matrixkoder", "p1", "r1", "T1")
               == os.path.join(proj, "plans", "p1", "runs", "r1", "T1"), "task")

    try:
        tmp_home = os.path.join(root, "home")
        os.makedirs(tmp_home, exist_ok=True)
        repo = os.path.join(root, "repo")
        os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
        # a linked worktree: .git is a file pointing at <repo>/.git/worktrees/lane
        wt_git = os.path.join(repo, ".git", "worktrees", "lane")
        os.makedirs(wt_git, exist_ok=True)
        with open(os.path.join(wt_git, "commondir"), "w") as f:
            f.write("../..\n")
        worktree = os.path.join(root, "lane")
        os.makedirs(worktree, exist_ok=True)
        with open(os.path.join(worktree, ".git"), "w") as f:
            f.write("gitdir: " + wt_git + "\n")
        t_home(tmp_home)
        t_l10(tmp_home)
        t_names()
        t_db_names(tmp_home)
        t_data_dir(tmp_home, repo, worktree)
        t_folders(tmp_home, repo)
        t_project_db(tmp_home, repo)
        t_canonical(tmp_home, repo)
        t_canonical_folder(tmp_home, repo)
        t_bus(tmp_home, repo)
        t_take_over_race()
        t_target(tmp_home)
        t_migrated(tmp_home)
    except Fail as e:
        failures.append(str(e))
    finally:
        os.chdir(saved_cwd)
        for key, val in saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        shutil.rmtree(root, ignore_errors=True)
    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("mkpaths self-test: ok")
    return 0


def main(argv):
    if argv == ["--self-test"]:
        return self_test()
    print(__doc__.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
