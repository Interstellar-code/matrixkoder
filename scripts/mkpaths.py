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
Python 3 stdlib only; `mkdb.py` is deliberately NOT imported (mkpaths must stay importable
from mkdb once it delegates here in L3).
"""
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import sys

DEFAULT_HOME = "~/.matrixkoder"
ROOT_DB_NAME = "mk.db"
PROJECTS_JSON = "projects.json"
USAGE_LEDGER = "usage.json"
LANE_BUS_FILE = ".matrixkoder-bus.json"
BUS_LANES_DIR = "bus-lanes"
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


def root_db(arg=None):
    """The root overview store: <home>/mk.db (C3). A read never creates it."""
    return os.path.join(home(arg), ROOT_DB_NAME)


def bus_env_dir():
    """$MK_BUS_DIR: the bus test seam that pins a bus home without touching ~/.matrixkoder."""
    override = os.environ.get("MK_BUS_DIR")
    return os.path.abspath(os.path.expanduser(override)) if override else None


def root_home():
    """The home the root overview store lives in for the project-by-slug lookups below: the
    bus pins it with $MK_BUS_DIR (tests), everything else uses the MK home. No other caller
    ever sets $MK_BUS_DIR, so the two agree wherever it matters."""
    return bus_env_dir() or home()


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


def project_data_dir(slug):
    """The data_dir the root projects table gives this slug, or None. Never creates mk.db.

    mk-bus.store_for's rule: realpath(expanduser(recorded)). A relative data_dir therefore
    resolves against cwd, not the repo — the bus runs from the repo or a lane of it, and that
    is the bus's rule to keep (state-side `root_data_dir` resolves it against the repo top).
    """
    row = _query(os.path.join(root_home(), ROOT_DB_NAME),
                 "SELECT data_dir FROM projects WHERE slug=?", (slug,))
    if not row or not isinstance(row["data_dir"], str) or not row["data_dir"].strip():
        return None
    return os.path.realpath(os.path.expanduser(row["data_dir"].strip()))


def project_slug_of(data_dir):
    """The root projects.slug whose data_dir is this path, or None. Never creates mk.db.

    mk-bus.project_slug_for's read half: realpath on both sides, slug rule C11 (an invalid
    slug is skipped, so a matching row with a bad slug never wins). The bus keeps its own
    "no row yet → register a derived slug" write path.
    """
    real = os.path.realpath(data_dir)
    con = _open_root_ro(os.path.join(root_home(), ROOT_DB_NAME))
    if con is None:
        return None
    try:
        for row in con.execute("SELECT slug, data_dir FROM projects"):
            slug, recorded = row["slug"], row["data_dir"]
            if not isinstance(slug, str) or not SLUG_RE.match(slug):
                continue
            if recorded and os.path.realpath(os.path.expanduser(recorded)) == real:
                return slug
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return None


# ---- stores ----------------------------------------------------------------------------------

def project_db(slug=None, data=None):
    """The project store: <data>/mk.db. `data` wins over the slug's root-projects data_dir.

    Absent file means file mode; this never creates it. Returns None when a slug has no root
    projects row carrying a data_dir (the caller owns that error).
    """
    if data:
        return os.path.join(os.path.abspath(os.path.expanduser(data)), ROOT_DB_NAME)
    if not slug:
        raise Fail("project_db needs a slug or a data dir")
    data_dir_of_slug = project_data_dir(slug)
    if not data_dir_of_slug:
        return None
    return os.path.join(data_dir_of_slug, ROOT_DB_NAME)


# ---- run / plan / task folders (today: <data>/runs/**, <data>/plans/**) ----------------------

def runs_dir(data):
    return os.path.join(data, "runs")


def run_dir(data, run):
    return os.path.join(runs_dir(data), run)


def tasks_dir(data, run):
    return os.path.join(run_dir(data, run), "tasks")


def task_dir(data, run, task):
    return os.path.join(tasks_dir(data, run), task)


def run_doc(data, run):
    return os.path.join(run_dir(data, run), "run.json")


def task_doc(data, run, task):
    return os.path.join(task_dir(data, run, task), "task.json")


def events_doc(data, run):
    return os.path.join(run_dir(data, run), "events.jsonl")


def board_doc(data, run):
    return os.path.join(run_dir(data, run), "board.md")


def runs_index(data):
    return os.path.join(runs_dir(data), "index.json")


def plans_dir(data):
    return os.path.join(data, "plans")


def plan_dir(data, plan):
    return os.path.join(plans_dir(data), plan)


def plan_archives_dir(data):
    return os.path.join(plans_dir(data), "archive")


def plan_file(data, plan_id):
    """The plan markdown: <data>/plans/<plan_id>.md."""
    return os.path.join(plans_dir(data), f"{plan_id}.md")


def project_yaml(data):
    """<data>/matrixkoder.yaml — the repo settings file (L3 moves it under projects/<slug>/)."""
    return os.path.join(data, "matrixkoder.yaml")


def project_yaml_lock(data):
    """The flock beside matrixkoder.yaml: <data>/matrixkoder.yaml.lock."""
    return os.path.join(project_yaml(data) + ".lock")


def lanes_file(data):
    """<data>/lanes.json — the lane-slot registry (a project-level file, not lane data)."""
    return os.path.join(data, "lanes.json")


def lanes_lock(data):
    """The flock beside lanes.json: <data>/lanes.lock."""
    return os.path.join(data, "lanes.lock")


def runs_lock(data):
    """The one flock file for the data dir: <data>/runs/.lock."""
    return os.path.join(runs_dir(data), ".lock")


# ---- bus ------------------------------------------------------------------------------------

def bus_dir(data):
    """Where bus.json / bus.lock / bus-tokens.json / ring-queue.json live for this data dir.

    $MK_BUS_DIR wins. File mode (no <data>/mk.db): the data dir, exactly as before — the
    self-hosting guard. Store mode: the MK home, one bus for every project.
    """
    override = bus_env_dir()
    if override:
        return override
    if os.path.isfile(project_db(data=data)):
        return home()
    return data


def lane_token(lane, file_mode=False):
    """C6: the ONE lane-token rule for mk-bus, mk-lane-event and mk-builder.

    $MK_BUS_TOKEN_FILE wins (tests and a relocated home), else the file-mode token inside the
    lane (<lane>/.matrixkoder-bus.json) — when it is there, and in file mode even when it is
    not — else <home>/bus-lanes/<sha256(realpath(lane))>.json, mode 0600. The home is
    $MK_BUS_DIR when the bus is pinned to one, else $MK_HOME, else $MATRIXKODER_HOME,
    else ~/.matrixkoder.
    """
    override = os.environ.get("MK_BUS_TOKEN_FILE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    inside = os.path.join(lane, LANE_BUS_FILE)
    if file_mode or os.path.isfile(inside):
        return inside
    base = bus_env_dir() or home()
    digest = hashlib.sha256(os.path.realpath(lane).encode("utf-8")).hexdigest()
    return os.path.join(os.path.abspath(os.path.expanduser(base)), BUS_LANES_DIR, digest + ".json")


def ring_queue(data):
    return os.path.join(bus_dir(data), "ring-queue.json")


def bus_json(data):
    """<bus>/bus.json — pid, port, started, owner_heartbeat, token (0600)."""
    return os.path.join(bus_dir(data), "bus.json")


def bus_lock(data):
    return os.path.join(bus_dir(data), "bus.lock")


def bus_tokens(data):
    return os.path.join(bus_dir(data), "bus-tokens.json")


def bus_tokens_lock(data):
    return os.path.join(bus_dir(data), "bus-tokens.lock")


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
    """The orchestrator bus watch cursors: <data>/watch-cursors.json (0600)."""
    return os.path.join(data, "watch-cursors.json")


def nudge_state(data):
    """mk-lane-nudge's state file: the data dir when there is one, else the MK home."""
    return os.path.join(data or home(), "nudge-state.json")


def nudge_state_lock(data):
    """The flock beside the nudge state: <data or home>/nudge-state.lock."""
    return os.path.join(data or home(), "nudge-state.lock")


# ---- C2 target tree (unused until L3) --------------------------------------------------------

def target_projects_dir(arg=None):
    return os.path.join(home(arg), TARGET_PROJECTS_DIR)


def target_project_dir(slug, arg=None):
    return os.path.join(target_projects_dir(arg), slug)


def target_project_db(slug, arg=None):
    return os.path.join(target_project_dir(slug, arg), ROOT_DB_NAME)


def target_project_yaml(slug, arg=None):
    """Today's matrixkoder.yaml, under the project folder."""
    return os.path.join(target_project_dir(slug, arg), "project.yaml")


def target_watch_cursors(slug, arg=None):
    return os.path.join(target_project_dir(slug, arg), "watch-cursors.json")


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


def _seed_root(home_dir, slug, repo, data_dir_rel):
    """A root store with just the projects table the resolvers read (re-seedable).

    The store is created through mkdb.open_root: the single-writer guard (mkdb._single_writer)
    allows only mkdb.py to open SQLite read-write, and this is a fixture, so it borrows
    mkdb's own writer. mkpaths itself never writes a store — production code only ever calls
    _open_root_ro. mkdb is imported lazily, here in the self-test, because mkdb will delegate
    to this module in L3.
    """
    import sqlite3  # noqa: F401  (only the connect call is banned outside mkdb; none here)
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("mkdb", os.path.join(here, "mkdb.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    con = mod.open_root(home_dir)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS projects("
                    "slug TEXT PRIMARY KEY, name TEXT, repo TEXT, data_dir TEXT, mode TEXT,"
                    " created TEXT, last_activity TEXT)")
        con.execute("DELETE FROM projects")
        con.execute("INSERT INTO projects(slug, name, repo, data_dir, mode) VALUES (?,?,?,?,?)",
                    (slug, slug, repo, data_dir_rel, "home"))
        con.commit()
    finally:
        con.close()


def _clear_home_env():
    for key in ("MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA", "MK_BUS_DIR", "MK_BUS_TOKEN_FILE"):
        os.environ.pop(key, None)


def self_test():
    import shutil
    import tempfile

    root = tempfile.mkdtemp(prefix="mkpaths-st-")
    saved_env = {k: os.environ.pop(k, None)
                 for k in ("MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA", "MK_BUS_DIR",
                           "MK_BUS_TOKEN_FILE", "HOME")}
    saved_cwd = os.getcwd()
    failures = []

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
        _check(watch_cursors(data) == os.path.join(data, "watch-cursors.json"), watch_cursors(data))
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
        _check(project_db(slug="matrixkoder") == os.path.join(data, "mk.db"),
               project_db(slug="matrixkoder"))
        _check(project_db(data=data) == os.path.join(data, "mk.db"), project_db(data=data))
        _check(project_db(slug="nope") is None, project_db(slug="nope"))
        _check(project_slug_of(data) == "matrixkoder", project_slug_of(data))
        _check(project_slug_of(os.path.join(root, "nowhere")) is None, project_slug_of("x"))

    def t_bus(tmp_home, repo):
        os.environ["HOME"] = tmp_home
        _clear_home_env()
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = tmp_home
        os.chdir(repo)
        data = data_dir()
        # file mode: the data dir, as before
        _check(bus_dir(data) == data, bus_dir(data))
        _check(ring_queue(data) == os.path.join(data, "ring-queue.json"), ring_queue(data))
        # store mode: the MK home, one bus per home
        os.makedirs(data, exist_ok=True)
        open(os.path.join(data, "mk.db"), "w").close()
        _check(bus_dir(data) == tmp_home, bus_dir(data))
        # $MK_BUS_DIR overrides both
        pinned = os.path.join(root, "pinned-bus")
        os.environ["MK_BUS_DIR"] = pinned
        _check(bus_dir(data) == pinned, bus_dir(data))
        _check(ring_queue(data) == os.path.join(pinned, "ring-queue.json"), ring_queue(data))
        os.environ.pop("MK_BUS_DIR")
        # the lane token: one rule for mk-bus, mk-lane-event and mk-builder
        lane = os.path.join(root, "lane")
        os.makedirs(lane, exist_ok=True)
        digest = hashlib.sha256(os.path.realpath(lane).encode("utf-8")).hexdigest()
        _check(lane_token(lane) == os.path.join(tmp_home, BUS_LANES_DIR, digest + ".json"),
               lane_token(lane))
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

    def t_target(tmp_home):
        os.environ["HOME"] = tmp_home
        _clear_home_env()
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = tmp_home
        proj = os.path.join(tmp_home, "projects", "matrixkoder")
        _check(target_projects_dir() == os.path.join(tmp_home, "projects"), target_projects_dir())
        _check(target_project_dir("matrixkoder") == proj, target_project_dir("matrixkoder"))
        _check(target_project_db("matrixkoder") == os.path.join(proj, "mk.db"), "target db")
        _check(target_project_yaml("matrixkoder") == os.path.join(proj, "project.yaml"), "yaml")
        _check(target_watch_cursors("matrixkoder")
               == os.path.join(proj, "watch-cursors.json"), "cursors")
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
        t_data_dir(tmp_home, repo, worktree)
        t_folders(tmp_home, repo)
        t_project_db(tmp_home, repo)
        t_bus(tmp_home, repo)
        t_target(tmp_home)
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
