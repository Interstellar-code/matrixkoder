#!/usr/bin/env python3
"""End-to-end layout test for the C2 storage tree (plan storage-layout, L9).

  run [--slug S] [--keep]   Build a TEMP MK home + a scratch git repo, drive the full MK
                            cycle through the PRODUCTION tools only, migrate one project,
                            and print ONE JSON line describing the tree.
  check --home DIR          Walk that tree and audit every C2-C6 rule. Writes tree.txt and
                            audit.md into the temp root. Exit 0 clean, 1 on any violation.
  show --home DIR           Print the tree and the MK_HOME to point /mk at.
  audit [--home DIR]        L10.14: the v2 layout + DB audit of a REAL home (default ~/.matrixkoder),
                            strictly read-only: every store is read from a private copy. Prints
                            ONE JSON line {tree, tables, schema, rules}; exit 0 clean, 1 drift.
  teardown --home DIR       Remove only what `run` created (a marker proves the root).
  --self-test               The mutation cases: each rule is broken on a tiny fixture tree
                            and `check` must fail, naming the offending path.

Safety (scripts/mk-livetest.py's rule, reused by import, never copied): the MK home is
refused before anything is created unless it is under the system temp dir, and
~/.matrixkoder is refused outright. Every subprocess gets MK_HOME and MATRIXKODER_HOME in
its OWN env dict; this process never mutates os.environ.

No mock of the code under test: every state change goes through scripts/mk-state.py,
mk-plan.py, mkdb.py, mk-migrate.py, mk-config.py and mk-bus.py.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(name, path):
    """A script by path: the hyphen in mk-state.py keeps it off the import path."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mkpaths = _load("mkpaths", os.path.join(HERE, "mkpaths.py"))  # C1: the one path module
mkdb = _load("mkdb", os.path.join(HERE, "mkdb.py"))           # the only SQLite writer
mkstate = _load("mk_state", os.path.join(HERE, "mk-state.py"))
livetest = _load("mk_livetest", os.path.join(HERE, "mk-livetest.py"))

MARKER = "mk-e2e-layout-marker.json"
TOKEN = "mk-e2e-layout-token"
PREFIX = "mk-e2e-layout-"
FAIL = getattr(livetest, "Fail", RuntimeError)

# The C4 file views (plan storage-layout L410). L4 (mk-state store-only) removes the
# writers; until it merges these are still produced, so `check` reports them, never hides
# them (see the report's "rules that fail on today's main").
FILE_VIEWS = mkpaths.VIEW_DOC_NAMES
BUS_ARCHIVE_SUBCOMMANDS = ("archive-run",)   # L6.3; absent on main until it merges

POLL_STEP = 0.02


# --------------------------------------------------------------------------- paths

def refuse_home(path):
    """Hard refusal before anything is created: the real MK home, or anything outside the
    system temp dir. mk-livetest.py:61-69, reused rather than re-implemented."""
    return livetest.refuse_home(path)

def _check_root(root):
    """The run root: a real directory named mk-e2e-layout-* directly under the temp dir
    (mk-livetest._check_root's shape, this script's prefix). Never the temp dir itself,
    the root, or the real home."""
    root = os.path.realpath(root)
    parent, name = os.path.split(root)
    if parent != temp_root() or not name.startswith(PREFIX):
        raise FAIL(f"refusing a run root that is not {PREFIX}* under the temp dir: {root}")
    if os.path.islink(root) or not os.path.isdir(root):
        raise FAIL(f"refusing a run root that is not a real directory: {root}")
    if root in (temp_root(), os.path.realpath("/"), os.path.realpath(os.path.expanduser("~"))):
        raise FAIL(f"refusing to remove {root}")
    return root


def temp_root():
    return livetest.temp_root()


def _inside(root, path):
    """True only when path is strictly inside root. Symlinks never count as inside."""
    root, path = os.path.realpath(root), os.path.realpath(path)
    return os.path.commonpath([root, path]) == root and path != root


def _marker_path(home):
    return os.path.join(home, MARKER)


def _write_marker(root, home, repo):
    """The marker proves the root is ours only when its token matches a second file in it."""
    import secrets
    token = secrets.token_hex(16)
    with open(_marker_path(home), "w", encoding="utf-8") as fh:
        json.dump({"home": home, "repos": list(repo), "root": root, "token": token}, fh)
    with open(os.path.join(root, TOKEN), "w", encoding="utf-8") as fh:
        fh.write(token)


def _read_marker(home):
    path = _marker_path(home)
    if not os.path.isfile(path) or os.path.islink(path):
        raise FAIL(f"no marker at {path}; teardown only removes what run created")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if os.path.realpath(data.get("home", "")) != os.path.realpath(home):
        raise FAIL("marker home does not match; refusing")
    root = os.path.realpath(data.get("root", ""))
    token_path = os.path.join(root, TOKEN)
    if os.path.islink(token_path) or not os.path.isfile(token_path):
        raise FAIL("run token missing; refusing")
    with open(token_path, encoding="utf-8") as fh:
        if fh.read() != data.get("token"):
            raise FAIL("run token does not match the marker; refusing")
    return data


def _walk(root):
    """Every path under root, relative, sorted, excluding the marker's own two files."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if rel in (MARKER, TOKEN):
                continue
            out.append(rel)
    return sorted(out)


def _git(repo, *args):
    """One git call in the scratch repo. The stderr is kept so a failure is diagnosable."""
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode:
        raise FAIL(f"git {' '.join(args)} in {repo}: rc={proc.returncode} "
                   f"err={proc.stderr.strip()[:400]}")


def _digest(path):
    """sha256 of a file's bytes, or None when it is absent."""
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _store_digest(db_path):
    """The store's bytes plus its WAL, so a write that only reached the WAL still shows."""
    return {p: _digest(db_path + p) for p in ("", "-wal", "-shm")}


# an audit read of a quiescent tree: immutable=1 never creates a -shm/-wal, so a rule's read cannot
# change the bytes R11 compares
RO_AUDIT = "?mode=ro&immutable=1"


def _tables(db_path):
    con = sqlite3.connect("file:" + db_path + RO_AUDIT, uri=True)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _rows(db_path, sql):
    con = sqlite3.connect("file:" + db_path + RO_AUDIT, uri=True)
    try:
        return [tuple(r) for r in con.execute(sql)]
    finally:
        con.close()


# the root tables root schema 3 dropped (L10.9): none may come back
ROOT_DROPPED = ("cards", "project_summary", "sync", "event_src")


def _columns(db_path, table):
    con = sqlite3.connect("file:" + db_path + RO_AUDIT, uri=True)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def _wait_until(fn, seconds, what):
    """A bounded wait on a condition: one deadline, short polls, never an open loop."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(POLL_STEP)
    return fn()


def _default_home():
    """The USER's real home (C16), NOT this process's: the password database wins
    (mkdb._real_home copies it), $HOME is the fallback, so a temp-HOME test still
    names the real default home it must never write."""
    try:
        import pwd
        base = pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        base = os.path.expanduser("~")
    return os.path.realpath(os.path.abspath(os.path.join(base, ".matrixkoder")))

# The temp roots R15 treats as the caller's own throwaway tree. macOS is why there are four:
# tempfile.gettempdir() is /var/folders/... while every STORED path is a realpath
# (/private/var/folders/...), so the literal spellings alone matched nothing; /tmp and
# /private/tmp name that other spelling of the temp tree.
TEMP_ROOTS = tuple(sorted({r.rstrip("/") for r in (
    tempfile.gettempdir(), os.path.realpath(tempfile.gettempdir()), "/tmp", "/private/tmp")}))


def _temp_row(value):
    """Does a stored `data_dir`/`db_path` point inside a temp root? The comparison is on a
    PATH BOUNDARY, so `/tmp` never matches `/tmpfoo` — a real project folder."""
    return bool(value) and any(value == root or value.startswith(root + "/") for root in TEMP_ROOTS)


def _real_projects_rows():
    """The real home's temp-pointing projects rows, read-only, as 'slug -> data'
    strings. Snapshot for R15: only rows the run ADDED are a leak."""
    real = _default_home()
    db = mkpaths.db_in(real)
    if not os.path.isfile(db):
        return []
    con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "projects" not in tables:
            return []
        return sorted(
            f"{slug} -> {data or dbp}" for slug, data, dbp in con.execute(
                "SELECT slug, data_dir, db_path FROM projects")
            if _temp_row(data) or _temp_row(dbp))
    finally:
        con.close()


def mk_env(home, data=None, extra=None):
    """A child env dict with BOTH home names (mkpaths.home(), mkpaths.py:59-76), the
    data dir and MK_SELFTEST=1. Every write the child makes is refused outside the
    temp tree by mkdb's C16 guard (mkdb.py:170-200). This process's own os.environ
    is NOT read again here: do_run pins the parent once, at :185's setter."""
    env = dict(os.environ)
    env["MK_HOME"] = home
    env["MATRIXKODER_HOME"] = home
    env["MK_SELFTEST"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in ("MK_BUS_DIR", "MK_BUS_TOKEN_FILE", "MATRIXKODER_DATA"):
        env.pop(key, None)
    if data:
        env["MATRIXKODER_DATA"] = data
    if extra:
        env.update(extra)
    return env


def _pin_parent_env(home):
    """Pin the PARENT's own env to the temp home BEFORE any in-process mkdb/mk-state
    call resolves the DEFAULT home (mkpaths.home() -> ~/.matrixkoder, mkpaths.py:76):
    that is the leak that put the `e2e` row in the real root store. The pins stay for
    the process's whole life; the guard flag arms mkdb's C16 write refusal."""
    os.environ["MK_HOME"] = home
    os.environ["MATRIXKODER_HOME"] = home
    os.environ["MK_SELFTEST"] = "1"


def _guard_refuses_real_home():
    """C16, case (c): a planted UNPINNED write to the REAL default home is refused
    by mkdb's own guard, in-process, before it can exist. Proves the guard fires
    even when the caller has cleared the pins. MK_SELFTEST stays armed; only the
    HOME pins are cleared."""
    real = _default_home()
    kept = {k: os.environ.get(k) for k in ("MK_HOME", "MATRIXKODER_HOME", "MK_SELFTEST")}
    try:
        os.environ["MK_SELFTEST"] = "1"
        os.environ.pop("MK_HOME", None)
        os.environ.pop("MATRIXKODER_HOME", None)
        try:
            mkdb.open_root(real).close()
        except Exception:
            # refused: the case under test. Exception, not BaseException: mkdb.Fail is a subclass,
            # but a KeyboardInterrupt/SystemExit from the open must NOT read as "the guard refused".
            return
        raise FAIL(f"the guard ALLOWED a write into the real default home {real}")
    finally:
        for k, v in kept.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _last_json(text):
    for line in reversed((text or "").strip().splitlines()):
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(line)
    return {}


def run_tool(script, *args, home, data=None, timeout=120, ok=(0,), extra_env=None):
    """One PRODUCTION script, one JSON line. Never a mock: this is the code under test."""
    argv = [sys.executable, os.path.join(HERE, script), *[str(a) for a in args]]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          env=mk_env(home, data, extra_env))
    out = _last_json(proc.stdout)
    if proc.returncode not in ok or not out.get("ok"):
        raise FAIL(f"{script} {' '.join(str(a) for a in args)}: rc={proc.returncode} "
                   f"out={json.dumps(out)[:400]} err={proc.stderr.strip()[:400]}")
    return out


# --------------------------------------------------------------------------- the plan

PLAN_TEMPLATE = """+++
plan = "{plan}"
status = "draft"
created = {created}
runs = []
design = ["research.md"]
integration = "mk/{plan}/integration"

[gates]
test = "python3 scripts/mk-e2e-layout.py --self-test"
typecheck = "python3 -m py_compile scripts/mk-e2e-layout.py"
selftest = "python3 scripts/mk-e2e-layout.py --self-test"
validate = "claude plugin validate ."

[[phase]]
id = "P1"
title = "first phase of the mock feature"
wave = 1
deps = []
owned = ["scripts/a.py"]
forbidden = []
route = ""
reviewer = ""
size = "standard"
risk = "reversible"
review = "light"
qa = []
commit = "feat(a): the first phase"
subagents = false
lane_group = ""

  [[phase.task]]
  id = "P1.1"
  title = "first task of the mock feature"
  acceptance = "scripts/a.py exists and its self-test passes"
  subagent = false
  owned = []

  [[phase.task]]
  id = "P1.2"
  title = "second task of the mock feature"
  acceptance = "scripts/b.py exists and its self-test passes"
  subagent = false
  owned = []

[[phase]]
id = "P2"
title = "second phase of the mock feature"
wave = 2
deps = ["P1"]
owned = ["scripts/c.py"]
forbidden = []
route = ""
reviewer = ""
size = "standard"
risk = "reversible"
review = "light"
qa = []
commit = "feat(c): the second phase"
subagents = false
lane_group = ""

  [[phase.task]]
  id = "P2.1"
  title = "third task of the mock feature"
  acceptance = "scripts/c.py exists and its self-test passes"
  subagent = false
  owned = []
+++
"""


def write_plan(repo, plan_id):
    """The plan file, written from skills/matrixkoder/templates/plan.md's shape, plus the
    design ref assets/ will carry."""
    path = os.path.join(repo, f"{plan_id}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(PLAN_TEMPLATE.format(plan=plan_id, created="2026-10-15"))
    with open(os.path.join(repo, "research.md"), "w", encoding="utf-8") as fh:
        fh.write("# research\n\nThe design note the plan's assets/ carries.\n")
    return path


# --------------------------------------------------------------------------- the bus

class TempBus:
    """The REAL mk-bus.py server on the temp home. The message texts are byte-for-byte the
    mk-builder shapes, so the audit counts what a real lane would have posted."""

    def __init__(self, home, data):
        self.home, self.data = home, data
        self.pid = self.port = None
        self.files_up = []      # the bus files observed while it was up

    def cli(self, *args, timeout=60, ok=(0,)):
        return run_tool("mk-bus.py", *args, home=self.home, data=self.data,
                        timeout=timeout, ok=ok)

    def start(self):
        """`ensure` spawns the detached `serve`; bus.json lands in the C2 bus folder."""
        info = self.cli("ensure", "--run", "", timeout=90)
        self.pid, self.port = info.get("pid"), info.get("port")
        if not self.pid or not self.port:
            raise FAIL(f"bus did not start: {info}")
        if not self.wait_for_quiesce(30):
            raise FAIL("bus did not answer within 30s")
        # What the bus wrote WHILE UP. bus.json is removed when the bus exits, so the audit
        # asks "where did these live?" rather than "are they still there?".
        bus_dir = mkpaths.target_bus_dir(self.home)
        self.files_up = sorted(n for n in os.listdir(bus_dir)
                               if os.path.isfile(os.path.join(bus_dir, n))) \
            if os.path.isdir(bus_dir) else []
        return info

    def wait_for_quiesce(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.cli("pending", "--run", "", timeout=20)
                return True
            except FAIL:
                time.sleep(POLL_STEP)
        return False

    def stop(self):
        """SIGTERM, bounded poll, SIGKILL — mk-bus.py's own stop_process shape
        (scripts/mk-bus.py:386-401)."""
        if self.pid is None:
            return
        pid = self.pid
        self.pid = None
        import signal
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with contextlib.suppress(ChildProcessError, OSError):
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    return
            time.sleep(POLL_STEP)
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, 0)

    def post(self, *, kind, frm, to, run, task, text, reply_to=None):
        args = ["send", "--run", run, "--task", task, "--kind", kind, "--text", text,
                "--from", frm, "--to", to]
        if reply_to:
            args += ["--reply-to", reply_to]
        res = self.cli(*args, timeout=30)
        mid = (res.get("message") or {}).get("id") or res.get("id")
        if not mid:
            raise FAIL(f"no message id in send reply: {res}")
        return str(mid)

    def lane_cycle(self, *, run, task, lane, report, round_n=1, verdict="APPROVE", findings=0,
                   review_path=".matrixkoder-review-r1.md", commit_sha="0" * 40):
        """One simulated lane writer through the real mk-builder sequence."""
        ids = {}
        ids["connect"] = self.post(kind="info", frm=task, to="orch", run=run, task=task,
                                   text=f"lane connected {task} R{round_n}")
        ids["progress"] = self.post(kind="info", frm=task, to="orch", run=run, task=task,
                                    text=f"progress: {lane}: starting round {round_n}")
        ids["status"] = self.post(kind="task", frm=task, to="orch", run=run, task=task,
                                  text=f"status in_progress\nround: {round_n}\nworking in {lane}")
        ids["commit"] = self.post(kind="commit", frm=task, to="orch", run=run, task=task,
                                  text=f"round: {round_n}\n{commit_sha}")
        ids["report"] = self.post(kind="report", frm=task, to="orch", run=run, task=task,
                                  text=f"round: {round_n}\n{report}")
        # the bus takes reply_to only on ask/answer (mk-bus.py send, the reply_to guard):
        # every other kind must post with reply_to null
        ids["review"] = self.post(kind="review", frm=f"{task}-reviewer", to=task,
                                  run=run, task=task,
                                  text=f"round: {round_n}\nverdict: {verdict} "
                                       f"findings: {findings} path: {review_path}")
        ids["ids"] = [ids[k] for k in ("connect", "progress", "status", "commit",
                                       "report", "review")]
        return ids

    def archive_run(self, run):
        """L6.3's run-end archive, when it is on the branch. Conditional so the script works
        before and after the merge; the caller records which way it went."""
        return run_tool("mk-bus.py", "archive-run", "--run", run, home=self.home,
                        data=self.data, timeout=60, ok=(0, 2, 3))

    def has_archive_run(self):
        try:
            out = subprocess.run([sys.executable, os.path.join(HERE, "mk-bus.py"), "--help"],
                                 capture_output=True, text=True, timeout=30,
                                 env=mk_env(self.home, self.data))
        except (OSError, subprocess.SubprocessError):
            return False
        return all(name in out.stdout for name in BUS_ARCHIVE_SUBCOMMANDS)


# --------------------------------------------------------------------------- run

def do_run(slug="e2e", keep=False):
    """Build the temp tree and drive the whole cycle. Returns the JSON payload printed."""
    mkdb.check_slug(slug)
    # realpath: refuse_home and mkpaths both realpath, so a symlinked temp dir would make
    # every relative path in the audit come out as ../..
    # Z17: snapshot the real home's projects rows BEFORE the run; R15 faults only on
    # junk this run added (pre-existing junk is the user's call, listed, never a leak).
    _real_before = _real_projects_rows()
    parent = os.path.realpath(tempfile.mkdtemp(prefix=PREFIX))
    home = refuse_home(os.path.join(parent, "home"))
    os.makedirs(home)
    repo = os.path.join(parent, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=e2e@example.invalid", "-c", "user.name=e2e",
         "commit", "-q", "--allow-empty", "-m", "scratch")
    plan_id = f"{slug}-demo"
    plan_path = write_plan(repo, plan_id)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=e2e@example.invalid", "-c", "user.name=e2e",
         "commit", "-q", "-m", "plan")

    # Project A is REPO mode (data = <repo>/.matrixkoder): only then does C5 have a real
    # legacy store to move and freeze. Project B (below) is home mode and never switched.
    data = os.path.join(repo, ".matrixkoder")
    os.makedirs(os.path.join(data, "plans"), exist_ok=True)
    os.makedirs(os.path.join(data, "runs"), exist_ok=True)

    # (b) /mk setup equivalent, through the Python tools (mk-livetest.py:125-147).
    # Every tool call in this function goes through `tool`, so the temp home AND the data dir
    # travel in each child's env. mk-state shells out to mk-plan with no --data, and that
    # child resolves the data dir from $MATRIXKODER_DATA — unset, the plan tool would
    # resolve against the CWD and touch the real lane store.
    def tool(script, *args, data=data, **kw):
        return run_tool(script, *args, home=home, data=data, **kw)

    tool("mk-config.py", "list", "--json", "--data", data, timeout=60)
    # Z17: pin THIS process's env before the in-process registrations below (mkstate.register_
    # project -> mk-state.py:258's mkdb.open_root(home()), and _register at :663) resolve
    # mkpaths.home(): without the pins they resolve the REAL ~/.matrixkoder and write the
    # e2e row into the user's root store (the leak this task fixes).
    _pin_parent_env(home)
    mkstate.register_project(slug, repo, data, name="e2e")
    mkdb.open_project(home, slug, data_dir=data).close()

    # (c) the plan: import, approve, then the run.
    tool("mk-plan.py", "import", plan_path, "--data", data, "--home", home)
    tool("mk-plan.py", "approve", plan_path, "--data", data, "--home", home)
    run = f"{plan_id}-r1"
    tool("mk-state.py", "run", "init", "--data", data, "--by", "orch", "--id", run,
         "--feature", plan_id, "--branch", f"mk/{plan_id}/lane", "--plan", plan_path)

    # (e) the second project, left on the pre-migration layout: a data dir with the project
    # store at <data>/mk.db, never switched. It has its OWN clean scratch repo, which is what
    # R04 audits — C5 forbids any script from deleting a migrated project's old folder, so
    # "no .matrixkoder in the repo" can only hold for a project that never had one.
    slug_b = f"{slug}b"
    scratch = os.path.join(parent, "scratch")
    os.makedirs(scratch)
    _git(scratch, "init", "-q")
    _git(scratch, "-c", "user.email=e2e@example.invalid", "-c", "user.name=e2e",
         "commit", "-q", "--allow-empty", "-m", "scratch")
    data_b = os.path.join(parent, "legacy", slug_b)
    os.makedirs(data_b, exist_ok=True)
    mkdb.open_project(home, slug_b, data_dir=data_b).close()
    _register(home, slug_b, scratch, data_b)
    tool("mk-state.py", "run", "init", "--data", data_b, "--by", "orch",
         "--id", f"{plan_id}-legacy", "--feature", plan_id, "--branch",
         f"mk/{plan_id}/legacy", data=data_b)

    # (d) the tasks and the simulated lane writers on a REAL bus.
    bus = TempBus(home, data)
    bus.start()
    lane_msgs, lane_dirs, has_archive = {}, [], False
    try:
        for task, lane in (("P1.1", "lane-p11"), ("P1.2", "lane-p12"), ("P2.1", "lane-p21")):
            lane_dir = os.path.join(home, lane)
            os.makedirs(lane_dir, exist_ok=True)
            lane_dirs.append(lane_dir)
            tool("mk-bus.py", "token", "--lane", lane_dir, "--task", task, "--run", run,
                 data=data)
            tool("mk-state.py", "task", "set", task, "status=building", "--data", data,
                 "--by", "orch", "--run", run)
            lane_msgs[task] = bus.lane_cycle(
                run=run, task=task, lane=lane_dir,
                report=f"# {task} report\n\n[done] everything the acceptance names\n",
                verdict="REQUEST_CHANGES" if task == "P1.1" else "APPROVE",
                findings=1 if task == "P1.1" else 0)
            tool("mk-state.py", "task", "set", task, "verdict=" +
                 ("REQUEST_CHANGES" if task == "P1.1" else "APPROVE"),
                 "--data", data, "--by", "orch", "--run", run)
            if task == "P1.1":
                # a fix round: the reviewer asked for one, so the task takes round 2.
                tool("mk-state.py", "task", "round", task, "--kind", "fix", "--data", data,
                     "--by", "orch", "--run", run, "--cause", "review findings")
                lane_msgs[task]["fix"] = bus.lane_cycle(
                    run=run, task=task, lane=lane_dir,
                    report=f"# {task} fix report\n\n[done] the review finding is fixed\n",
                    round_n=2, verdict="APPROVE", findings=0)
            tool("mk-state.py", "task", "set", task, "status=done", "--data", data,
                 "--by", "orch", "--run", run)
        # (f) migrate while the run is still ACTIVE: C5 moves the files of live plans, so
        # switching here is what puts the run tree under the C2 plan folder at all — and it
        # is the state the bus archive (C6) then writes into.
        legacy_db = mkpaths.db_in(data)
        legacy_before = _store_digest(legacy_db)
        # the dry-run's own move list is kept: it is the evidence for what C5 did and did not
        # carry into the C2 plan folder
        dry = tool("mk-migrate.py", "--dry-run", "--slug", slug, "--data", data,
                   "--home", home, "--repo", repo, timeout=180)
        # the dry-run's own tally and skip list: the evidence for what C5 did and did not
        # carry into the C2 plan folder
        moves = dry.get("moves")
        skips = sorted(str(s) for s in (dry.get("skips") or []))
        tool("mk-migrate.py", "--switch", "--slug", slug, "--data", data, "--home", home,
             "--repo", repo, timeout=180)
        new_db = mkpaths.target_project_db(slug, home)
        # the bus follows the switch: the same data dir now resolves to the NEW store
        archived_before = _bus_row_counts(new_db, run)
        # The run end is where the C2 tree gets its last write. On today's main it can fail
        # because the switch left no runs/<run>/ folder under the C2 plan dir (round files
        # are store artifacts today, so mk-migrate has nothing to copy). That is a REAL gap,
        # so it is recorded and audited, never hidden by creating the folder here.
        run_end_error = ""
        try:
            tool("mk-state.py", "run", "set", "--status", "done", "--id", run, "--data", data,
                 "--by", "orch")
            has_archive = bus.has_archive_run()
            archive = bus.archive_run(run) if has_archive else None
        except FAIL as e:
            run_end_error = str(e)
            has_archive, archive = False, None
        archived_after = _bus_row_counts(new_db, run)
        before_writes = _store_digest(new_db)
        # a post-switch write: it must land in the NEW store, never in the legacy one
        try:
            tool("mk-state.py", "task", "set", "P2.1", "status=merged", "--data", data,
                 "--by", "orch", "--run", run)
        except FAIL as e:
            run_end_error = run_end_error or str(e)
        legacy_after = _store_digest(legacy_db)
    finally:
        bus.stop()

    # The manifest is what `check` reads: the tree itself proves the layout, but the bus
    # counts and the store digests can only be captured while the run is being driven.
    manifest = {
        "repo": repo, "scratch_repo": scratch,
        "plans": [[slug, plan_id]], "runs": [[slug, plan_id, run]],
        "lanes": lane_dirs, "data_of": {slug: data, slug_b: data_b},
        "pre_migration_slug": slug_b, "legacy_db": {slug: legacy_db},
        "legacy_digest_after": {slug: legacy_after}, "new_store_before": {slug: before_writes},
        # only meaningful once L6.3's archive-run exists; otherwise there is nothing to count
        "archived_counts": ({f"{slug}/{plan_id}/{run}": archived_before} if has_archive else {}),
        "bus_rows_before_archive": archived_before, "bus_rows_after_archive": archived_after,
        "archive_run_available": has_archive, "run_end_error": run_end_error,
        "bus_files_up": bus.files_up, "migrate_moves": moves, "migrate_skips": skips,
        "real_home_junk_before": _real_before, "real_home_junk_after": _real_projects_rows(),
    }
    with open(os.path.join(home, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)

    _write_marker(parent, home, [repo, scratch])
    payload = {
        "scratch_repo": scratch,
        "ok": True, "root": parent, "home": home, "repo": repo,
        "slug": slug, "slug_pre_migration": slug_b, "pre_migration_data": data_b,
        "plan": plan_id, "plan_file": plan_path, "run": run,
        "data": data, "new_project_db": new_db, "legacy_project_db": legacy_db,
        "root_db": mkpaths.db_in(home),
        "messages": sum(len(v["ids"]) for v in lane_msgs.values()),
        "bus_archive": bool(archive and archive.get("ok")),
        "legacy_unchanged": legacy_before == legacy_after,
        "new_store_written": before_writes != _store_digest(new_db),
        "run_end_error": run_end_error,
        "kept": True,
        "real_home_junk_before": _real_before,
        "real_home_junk_after": _real_projects_rows(),
    }
    if not keep:
        print(json.dumps(payload))
        teardown(home, quiet=True)
        return payload
    print(json.dumps(payload))
    return payload


def _bus_row_counts(db_path, run=None):
    """The C6 archived set in one store: bus messages + acks + bus artifacts of the run.
    Read-only; the count is exactly what leaves the store when the run is archived."""
    if not os.path.isfile(db_path):
        return 0
    con = sqlite3.connect("file:" + db_path + RO_AUDIT, uri=True)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # the run filter goes last on every query, so each WHERE is written in full here
        run_filter = " AND run=?" if run else ""
        args = (run,) if run else ()
        total = 0
        for table, sql in (
            ("events", "SELECT COUNT(*) FROM events WHERE chan='bus'" + run_filter),
            ("acks", "SELECT COUNT(*) FROM acks" + (" WHERE run=?" if run else "")),
            ("artifacts", "SELECT COUNT(*) FROM artifacts WHERE kind IN"
                          " ('bus','brief','report','review','review-brief')" + run_filter),
        ):
            if table in tables:
                total += con.execute(sql, args).fetchone()[0]
        return total
    finally:
        con.close()


def _register(home, slug, repo, data):
    """The root projects row, through mkdb (the one writer)."""
    con = mkdb.open_root(home)
    try:
        mkdb._begin(con)
        con.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, created)"
                    " VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(slug) DO UPDATE SET name=excluded.name, repo=excluded.repo,"
                    " data_dir=excluded.data_dir",
                    (slug, slug, os.path.realpath(repo), os.path.abspath(data), "home", "e2e"))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


# --------------------------------------------------------------------------- check

class Rule:
    """One audit rule: its id, the contract it enforces, and the check itself."""

    def __init__(self, rid, contract, what, fn):
        self.id, self.contract, self.what, self.fn = rid, contract, what, fn

    def run(self, ctx):
        """(ok, detail, [offending paths])."""
        try:
            return self.fn(ctx)
        except Exception as e:                     # a broken assertion is a violation, not a crash
            return False, f"{type(e).__name__}: {e}", [ctx.get("home", "?")]


def _rel(ctx, path):
    try:
        return os.path.relpath(path, ctx["root"])
    except ValueError:
        return str(path)


def build_rules():
    """The rule list, in contract order. Each returns (ok, detail, offending paths)."""

    def r01(ctx):
        # C2: exactly two DB kinds. Under the whole temp root that is the root store, one
        # store per data dir, and the pre-migration project's own <data>/mk.db. Any other
        # mk.db would be a third kind. A "project" = one data dir (realpath); its store =
        # the root rows' db_path. All rows of one data dir must agree on db_path.
        # C5: the legacy <data>/mk.db of a migrated project is the frozen pre-switch copy
        # until --archive runs, so it is expected too.
        found = sorted(_rel(ctx, os.path.join(ctx["root"], p)) for p in _all_mkdb(ctx["root"]))
        want = {_rel(ctx, mkpaths.db_in(ctx["home"]))}
        by_data = {}
        for slug in ctx["known"]:
            data = ctx["data_of"].get(slug)
            if data:
                by_data.setdefault(os.path.realpath(data), []).append(slug)
        bad = []
        for data, slugs in sorted(by_data.items()):
            paths = {ctx["db_paths"].get(s) for s in slugs}
            paths.discard(None)
            if len(paths) > 1:
                bad.append(f"data dir {data} has disagreeing db_paths: {sorted(paths)}")
                continue
            if len(paths) == 1:
                want.add(_rel(ctx, paths.pop()))
                want.add(_rel(ctx, mkpaths.db_in(data)))  # legacy frozen copy
            else:
                want.add(_rel(ctx, mkpaths.db_in(data)))
        stray = sorted(p for p in found if p not in want)
        missing = sorted(p for p in want if p not in found)
        ok = not stray and not missing and not bad
        return ok, f"{len(found)} mk.db under the temp root: {found}" \
            + (f"; disagreeing {bad}" if bad else "") \
            + (f"; stray {stray}" if stray else "") \
            + (f"; missing {missing}" if missing else ""), stray or missing or bad

    def r02(ctx):
        # A migrated plan lives under the C2 project folder; a pre-migration one under its
        # own data dir. Resolved from ctx, not through mkpaths' env-dependent helpers, so the
        # rule reads the same tree whether or not MK_HOME is set in this process.
        missing = []
        dirs = []
        for slug, data, plan in ctx["plans"]:
            pdir = (mkpaths.target_plan_dir(slug, plan, ctx["home"]) if slug in ctx["slugs"]
                    else mkpaths.plan_dir(data, plan))
            dirs.append(pdir)
            for entry in ("plan.md", "assets", "archive"):
                if not os.path.exists(os.path.join(pdir, entry)):
                    missing.append(f"{slug}:{_rel(ctx, pdir)}/{entry}")
        return not missing, f"plan dirs {[_rel(ctx, p) for p in dirs]}" + \
            (f"; missing {missing}" if missing else ""), missing

    def r03(ctx):
        missing = []
        for slug, plan, run in ctx["runs"]:
            path = os.path.join(mkpaths.target_run_dir(slug, plan, run, ctx["home"]), "bus.jsonl")
            if not os.path.isfile(path):
                missing.append(_rel(ctx, path))
                continue
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    with contextlib.suppress(json.JSONDecodeError):
                        json.loads(line)
                        continue
                    missing.append(f"{_rel(ctx, path)}:{n} (not a JSON object)")
        if not ctx["runs"]:
            return True, "no runs to check"
        return not missing, "bus.jsonl per closed run" + \
            (f"; missing/bad {missing}" if missing else ""), missing

    def r04(ctx):
        # C2: the clean scratch repo holds no .matrixkoder at all. Project A's repo keeps
        # its OLD folder on purpose (C5: no script deletes it) — what must hold there is
        # that nothing new was written into it, which R11 checks byte for byte.
        strays = [e for e in _walk(ctx["scratch_repo"])
                  if os.path.basename(e) == ".matrixkoder"]
        return not strays, f"{len(strays)} .matrixkoder in {ctx['scratch_repo']}" + \
            (f": {strays}" if strays else ""), strays

    def r05(ctx):
        strays = []
        for rel in _walk(ctx["root"]):
            base = os.path.basename(rel)
            if base in FILE_VIEWS or (base == "index.json"
                                      and os.path.basename(os.path.dirname(rel)) == "runs"):
                strays.append(rel)
        return not strays, f"{len(strays)} C4 file views under the temp root" + \
            (f": {sorted(strays)[:20]}" if strays else ""), strays

    def r06(ctx):
        # The root's plan_summary counts must equal the project store's own counts for the
        # same plan. R14 owns whether a row exists at all; this rule owns the numbers in it,
        # so the two never fail for the same reason. Both counts are in the detail, so a
        # vacuous pass shows up in the report instead of hiding.
        over = run_tool("mkdb.py", "overview", "--json", home=ctx["home"], timeout=60)
        rows = {(r["project"], r["plan"]): r for r in (over.get("plans") or [])}
        bad, plans = [], 0
        for slug, data in sorted(ctx["data_of"].items()):
            live = ctx["live_dir"].get(slug, data)
            if not live or not os.path.isfile(mkpaths.db_in(live)):
                continue
            mine = run_tool("mkdb.py", "plans", "--json", "--data", live,
                            home=ctx["home"], timeout=60)
            for plan in mine.get("plans") or []:
                plans += 1
                row = rows.get((slug, plan["id"]))
                if row is None:
                    continue        # R14 owns a missing row
                if (row.get("counts") or {}) != (plan.get("counts") or {}):
                    bad.append(f"plan_summary[{slug}/{plan['id']}] "
                               f"{row.get('counts')} != {plan.get('counts')}")
        return not bad, f"{plans} plans, {len(rows)} plan_summary rows" + \
            (f"; wrong {bad}" if bad else ""), bad

    def r07(ctx):
        bad = []
        for slug, plan, run in ctx["runs"]:
            path = os.path.join(mkpaths.target_run_dir(slug, plan, run, ctx["home"]), "bus.jsonl")
            if not os.path.isfile(path):
                continue        # R03 owns absence; this rule owns the count of what is there
            with open(path, encoding="utf-8") as fh:
                lines = sum(1 for line in fh if line.strip())
            want = ctx["archived_counts"].get(f"{slug}/{plan}/{run}")
            if want is not None and lines != want:
                bad.append(f"{_rel(ctx, path)} has {lines} lines, {want} archived")
        return not bad, "bus.jsonl counts" + (f"; wrong {bad}" if bad else ""), bad

    def r08(ctx):
        bad = []
        bus_dir = os.path.join(ctx["home"], "bus")
        lanes = os.path.join(bus_dir, "lanes")
        for lane in ctx["lanes"]:
            digest = hashlib.sha256(os.path.realpath(lane).encode("utf-8")).hexdigest()
            want = os.path.join(lanes, digest + ".json")
            if not os.path.isfile(want):
                bad.append(f"no token at {_rel(ctx, want)}")
                continue
            mode = os.stat(want).st_mode & 0o777
            if mode != 0o600:
                bad.append(f"{_rel(ctx, want)} mode {oct(mode)} != 0600")
            inside = os.path.join(lane, ".matrixkoder-bus.json")
            if os.path.exists(inside):
                bad.append(f"lane-local token {_rel(ctx, inside)}")
        if os.path.isdir(os.path.join(ctx["home"], "bus-lanes")):
            bad.append("bus-lanes/ at the home root (the pre-L6.1 token folder)")
        return not bad, f"{len(ctx['lanes'])} lane tokens under {_rel(ctx, lanes)}" + \
            (f"; wrong {bad}" if bad else ""), bad

    def r09(ctx):
        # Where each bus file lived while the bus was UP. bus.json and the lock files are
        # removed when the bus exits, so only the files meant to survive are required to
        # still be there; the rest only had to be under bus/ in the first place.
        bus_dir, bad = os.path.join(ctx["home"], "bus"), []
        seen = ctx["bus_files_up"] or ["bus.json", "bus-tokens.json"]
        for name in seen:
            if name == "bus.json" or name.endswith(".lock"):
                continue        # ephemeral: the bus deletes these when it exits
            if not os.path.isfile(os.path.join(bus_dir, name)):
                bad.append(f"bus/{name} (seen while the bus was up, gone now)")
        for name in list(seen) + ["ring-queue.json"]:
            if os.path.exists(os.path.join(ctx["home"], name)):
                bad.append(f"{name} at the home root (pre-L6.1 spelling)")
        # bus-lanes/ (the pre-L6.1 token folder) is R08's business, not this rule's
        if os.path.isdir(os.path.join(bus_dir, "bus-lanes")):
            bad.append("bus/bus-lanes/ (the token folder belongs one level up, C6)")
        return not bad, f"bus files {seen} under bus/" + \
            (f"; wrong {bad}" if bad else ""), bad

    def r10(ctx):
        # L10.5: the bus watch cursors are rows of the project DB's `watch_cursors` table; no
        # watch-cursors.json exists anywhere under the temp root, in the C2 folder or a data dir.
        bad = []
        for rel in _walk(ctx["root"]):
            if os.path.basename(rel) == mkpaths.CURSORS_NAME:
                bad.append(f"{rel} (cursors are watch_cursors rows, never a file)")
        rows = 0
        for slug in ctx["slugs"]:
            db = ctx["db_paths"].get(slug)
            if not db or not os.path.isfile(db):
                continue
            if "watch_cursors" not in _tables(db):
                bad.append(f"{_rel(ctx, db)} has no watch_cursors table")
                continue
            for key, seq in _rows(db, "SELECT key, seq FROM watch_cursors"):
                rows += 1
                if "|" not in str(key) or not isinstance(seq, int) or seq < 0:
                    bad.append(f"{_rel(ctx, db)} watch_cursors row {key!r}={seq!r} is malformed")
        return not bad, f"watch cursors in the project DB ({rows} rows)" + \
            (f"; wrong {bad}" if bad else ""), bad

    def r11(ctx):
        # One entry per data dir: the NEW store is the db_path the root rows agree on
        # (R01 enforces agreement), the legacy store is <data>/mk.db, frozen until --archive.
        bad = []
        by_data = {}
        for slug in ctx["migrated"]:
            data = ctx["data_of"].get(slug)
            if data:
                by_data.setdefault(os.path.realpath(data), []).append(slug)
        for data, slugs in sorted(by_data.items()):
            new_db = ctx["db_paths"].get(slugs[0])
            if not new_db or not os.path.isfile(new_db):
                bad.append(f"no NEW store at {_rel(ctx, new_db or mkpaths.db_in(data))}")
                continue
            if _store_digest(new_db) == ctx["new_store_before"].get(slugs[0]):
                bad.append(f"{_rel(ctx, new_db)} did not change after the switch")
            for slug in slugs:
                legacy = ctx["legacy_digest_after"].get(slug)
                if legacy is not None and _store_digest(ctx["legacy_db"][slug]) != legacy:
                    bad.append(f"legacy store {_rel(ctx, ctx['legacy_db'][slug])} changed")
        return not bad, f"{len(by_data)} migrated projects" + \
            (f"; wrong {bad}" if bad else ""), bad

    def r12(ctx):
        root_db = mkpaths.db_in(ctx["home"])
        bad = []
        tables = _tables(root_db)
        for name in ("tasks", "events"):
            if name in tables:
                bad.append(f"root mk.db has a {name} table (C3: root is numbers only)")
        # L10.9: root schema 3 dropped the sync-era tables
        for name in ROOT_DROPPED:
            if name in tables:
                bad.append(f"root mk.db still has the dropped {name} table (schema 3)")
        have = _rows(root_db, "SELECT MAX(version) FROM schema_version") if "schema_version" in tables else [(None,)]
        if have[0][0] != mkdb.ROOT_SCHEMA:
            bad.append(f"root schema_version {have[0][0]} != {mkdb.ROOT_SCHEMA}")
        want = ["project", "plan", "title", "status", "counts_json", "live_run", "updated"]
        got = _columns(root_db, "plan_summary")
        if got != want:
            bad.append(f"plan_summary columns {got} != {want}")
        return not bad, f"root schema: {len(tables)} tables" + (f"; wrong {bad}" if bad else ""), bad

    def r13(ctx):
        bad = []
        for rel in _walk(ctx["home"]):
            base = os.path.basename(rel)
            if base in (mkpaths.ROOT_DB_NAME,) or base.endswith(("-wal", "-shm")):
                continue
            if os.path.splitext(base)[1] in (".db", ".sqlite", ".sqlite3"):
                # the one whitelist: `mk-state run archive`'s VACUUM INTO backup
                if os.path.basename(os.path.dirname(rel)) == "archive":
                    continue
                bad.append(rel)
        return not bad, "no per-plan or bus DB" + (f"; stray {bad}" if bad else ""), bad

    def r14(ctx):
        over = run_tool("mkdb.py", "overview", "--json", home=ctx["home"], timeout=60)
        rows = {(r["project"], r["plan"]): r for r in (over.get("plans") or [])}
        bad = []
        for slug, _, plan in ctx["plans"]:
            row = rows.get((slug, plan))
            if row is None:
                bad.append(f"no plan_summary row for {slug}/{plan}")
                continue
            if not row.get("counts"):
                bad.append(f"plan_summary[{slug}/{plan}] has no counts")
        return not bad, f"{len(rows)} plan rows in the root overview" + \
            (f"; wrong {bad}" if bad else ""), bad

    def r15(ctx):
        # C16 (Z17): the CALLER's real home never GAINS a temp-pointing projects row.
        # Compare the after snapshot (from the payload) against the before one:
        # pre-existing junk is the user's call, listed in the payload, never a new leak.
        after = ctx.get("real_home_junk_after")
        if after is None:
            return True, "no after snapshot (older manifest): skipped", []
        before = set(ctx.get("real_home_junk_before") or [])
        bad = sorted(s for s in after if s not in before)
        return not bad, "no NEW temp-pointing row in the real home" \
            + (f"; new junk {bad}" if bad else ""), bad


    def r16(ctx):
        # L10.5: the lane registry is the `lane_slots` table of the project DB. No lanes.json (or its
        # flock file) exists after a run cycle, in the C2 folder, a data dir or anywhere else.
        bad = [f"{rel} (the registry is lane_slots rows, never a file)" for rel in _walk(ctx["root"])
               if os.path.basename(rel) in (mkpaths.LANES_NAME, "lanes.lock")]
        for slug in ctx["slugs"]:
            db = ctx["db_paths"].get(slug)
            if db and os.path.isfile(db) and "lane_slots" not in _tables(db):
                bad.append(f"{_rel(ctx, db)} has no lane_slots table")
        return not bad, "the lane registry is lane_slots only" + (f"; wrong {bad}" if bad else ""), bad

    def r17(ctx):
        # L10.3: settings live in the project DB's `settings` table and the global
        # <home>/mk-settings.yaml. The ONE yaml a migrated project folder may hold is the
        # `project.yaml` onboarding creates (mk-config reads it as the lowest layer); the legacy
        # matrixkoder.yaml name, a project.yaml anywhere else and a mk-settings.yaml outside the
        # home root are all violations.
        bad = []
        folders = {os.path.realpath(mkpaths.target_project_dir(s, ctx["home"])) for s in ctx["slugs"]}
        for rel in _walk(ctx["root"]):
            base, full = os.path.basename(rel), os.path.realpath(os.path.join(ctx["root"], rel))
            here = os.path.dirname(full)
            if base == mkpaths.PROJECT_YAML_NAME and here not in folders:
                bad.append(f"{rel} (project.yaml belongs only in a migrated project folder)")
            elif base == "matrixkoder.yaml" and here in folders:
                bad.append(f"{rel} (the legacy settings name inside a migrated project folder)")
            elif base == "mk-settings.yaml" and here != os.path.realpath(ctx["home"]):
                bad.append(f"{rel} (the global settings file lives at the home root)")
        for slug in ctx["slugs"]:
            db = ctx["db_paths"].get(slug)
            if db and os.path.isfile(db) and "settings" not in _tables(db):
                bad.append(f"{_rel(ctx, db)} has no settings table")
        return not bad, "settings in the project DB + mk-settings.yaml" + (f"; wrong {bad}" if bad else ""), bad

    return [
        Rule("R01", "C2", "exactly the root mk.db and one mk.db per project", r01),
        Rule("R02", "C2", "the plan folder holds plan.md, assets/ and archive/", r02),
        Rule("R03", "C6", "the closed run has runs/<run>/bus.jsonl", r03),
        Rule("R04", "C2", "the scratch repo holds no .matrixkoder", r04),
        Rule("R05", "C4", "no run.json/task.json/events.jsonl/board.md/index.json anywhere", r05),
        Rule("R06", "C3", "root plan_summary counts equal the project DB counts", r06),
        Rule("R07", "C6", "bus.jsonl line count equals the archived messages", r07),
        Rule("R08", "C6", "lane tokens live at bus/lanes/<sha256(lane)>.json, 0600", r08),
        Rule("R09", "C6", "the bus files live in bus/ and nowhere else", r09),
        Rule("R10", "C6", "watch cursors are watch_cursors rows in the project DB, no watch-cursors.json", r10),
        Rule("R11", "C3,C5", "post-switch writes land in the NEW store, the legacy one is frozen",
             r11),
        Rule("R12", "C3", "the root store carries numbers and linkage only", r12),
        Rule("R13", "C2", "no per-plan DB and no bus DB", r13),
        Rule("R14", "C3", "the root overview alone carries every plan row the Plan tab needs", r14),
        Rule("R15", "C16", "the caller's REAL home never gains an e2e row", r15),
        Rule("R16", "C6", "the lane registry is lane_slots rows, no lanes.json anywhere", r16),
        Rule("R17", "C8", "settings live in the project DB and mk-settings.yaml; at most project.yaml", r17),
    ]


def _all_mkdb(home):
    out = []
    for rel in _walk(home):
        if os.path.basename(rel) == mkpaths.ROOT_DB_NAME:
            out.append(os.path.join(home, rel))
    return sorted(out)


def build_context(home):
    """Everything the rules read, resolved from the tree itself plus the run's own record."""
    home = refuse_home(home)
    manifest = {}
    path = os.path.join(home, "manifest.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    projects_dir = os.path.join(home, "projects")
    over = {"projects": [], "plans": []}
    with contextlib.suppress(Exception):
        over = run_tool("mkdb.py", "overview", "--json", home=home, timeout=60)
    # every project the root knows about: the C2 ones (db_path set) and the pre-migration
    # ones (db_path NULL) must both be visible to the audit. The C2 set comes from the root
    # table and NOT from a directory listing — an unregistered folder under projects/ is
    # exactly the stray R01 must catch.
    known = {r["slug"]: r for r in (over.get("projects") or [])}
    data_of = {s: known[s]["data_dir"] for s in known if known[s].get("data_dir")}
    data_of.update(manifest.get("data_of") or {})
    migrated = sorted(s for s, r in known.items() if r.get("db_path"))
    plans = [(slug, data_of.get(slug, os.path.join(projects_dir, slug)), plan)
             for slug, plan in manifest.get("plans", [])]
    runs = [(slug, plan, run) for slug, plan, run in manifest.get("runs", [])]
    root = os.path.dirname(home)
    # where each project's LIVE store is: the C2 folder once migrated, its own data dir
    # before. `data_of` stays the registered data_dir, which for a migrated project is the
    # frozen legacy location.
    live_dir = {s: (mkpaths.target_project_dir(s, home) if s in migrated else data_of.get(s))
                for s in known}
    return {
        "root": root, "home": home,
        "repo": manifest.get("repo") or os.path.join(root, "repo"),
        "scratch_repo": manifest.get("scratch_repo") or os.path.join(root, "scratch"),
        "slugs": migrated, "known": sorted(known), "migrated": migrated,
        "plans": plans, "runs": runs, "lanes": sorted(str(p) for p in manifest.get("lanes", [])),
        "data_of": data_of, "live_dir": live_dir,
        "db_paths": {s: r.get("db_path") for s, r in known.items() if r.get("db_path")},
        "bus_files_up": manifest.get("bus_files_up") or [],
        "archived_counts": manifest.get("archived_counts") or {},
        "legacy_db": manifest.get("legacy_db") or {},
        "legacy_digest_after": manifest.get("legacy_digest_after") or {},
        "new_store_before": manifest.get("new_store_before") or {},
        "real_home_junk_before": manifest.get("real_home_junk_before"),
        "real_home_junk_after": manifest.get("real_home_junk_after"),
    }


def do_check(home, quiet=False):
    """Walk the temp tree, run every rule, write tree.txt + audit.md. Exit 0 or 1."""
    ctx = build_context(home)
    results = []
    for rule in build_rules():
        ok, detail, paths = rule.run(ctx)
        results.append({"id": rule.id, "contract": rule.contract, "what": rule.what,
                        "ok": bool(ok), "detail": detail, "paths": paths})
    failed = [r for r in results if not r["ok"]]

    tree_path = os.path.join(ctx["root"], "tree.txt")
    with open(tree_path, "w", encoding="utf-8") as fh:
        for rel in _walk(ctx["root"]):
            fh.write(rel + "\n")
    audit_path = os.path.join(ctx["root"], "audit.md")
    with open(audit_path, "w", encoding="utf-8") as fh:
        fh.write("# Storage-layout audit\n\n")
        fh.write(f"MK home: `{ctx['home']}`\nScratch repo: `{ctx['repo']}`\n\n")
        fh.write(f"**{len(results) - len(failed)}/{len(results)} rules pass.**\n\n")
        fh.write("| rule | contract | result | what it checks | detail |\n")
        fh.write("|---|---|---|---|---|\n")
        for r in results:
            fh.write(f"| {r['id']} | {r['contract']} | {'PASS' if r['ok'] else 'FAIL'} | "
                     f"{r['what']} | {r['detail']} |\n")
        if failed:
            fh.write("\n## Offending paths\n\n")
            for r in failed:
                fh.write(f"- **{r['id']}** ({r['contract']}): {r['what']}\n")
                for p in r["paths"] or ["(none named)"]:
                    fh.write(f"  - `{p}`\n")
    if not quiet:
        for r in results:
            print(f"{'PASS' if r['ok'] else 'FAIL'} {r['id']} [{r['contract']}] {r['what']}"
                  + ("" if r["ok"] else f"\n     {r['detail']}"))
        print(f"{len(results) - len(failed)}/{len(results)} rules pass; "
              f"tree: {tree_path}; audit: {audit_path}")
    return 1 if failed else 0


def do_show(home):
    """Print the tree and the MK_HOME to point /mk at."""
    ctx = build_context(home)
    print(f"MK_HOME={ctx['home']}")
    print(f"  /mk against this home shows the mock project: "
          f"{', '.join(ctx['slugs']) or '(none)'}")
    print(f"scratch repo: {ctx['repo']}")
    print()
    for rel in _walk(ctx["root"]):
        print(rel)
    return 0


def teardown(home, quiet=False):
    """Remove the run root and nothing else. The marker proves the root is ours."""
    if not home:
        raise FAIL("teardown needs --home")
    home = refuse_home(home)
    livetest._kill_buses(home)
    data = _read_marker(home)
    # Z17: the run root is this script's OWN prefix, never livetest's mk-livetest-*
    # (that mismatch made teardown dead code for its own run roots).
    root = _check_root(data["root"])
    if not _inside(root, home):
        raise FAIL(f"home is not inside the run root: {home}")
    repos = [os.path.realpath(p) for p in (data.get("repos") or [])]
    for repo in repos:
        if not _inside(root, repo):
            raise FAIL(f"repo is not inside the run root: {repo}")
    for path in (*repos, home):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.exists(path) or os.path.islink(path):
            os.unlink(path)
    if os.path.isdir(root):
        shutil.rmtree(root)
    if not quiet:
        print(json.dumps({"ok": True, "removed": root}))
    return 0


# --------------------------------------------------------------------------- audit (L10.14)

# The names a v2 MK home may hold at each level. Anything else is drift and is NAMED, never ignored.
# Legitimate on a real home today (L10.14 R2): config.json (kept until migrate-settings), bus-tokens.json and
# bus-lanes/ (kept by root-cleanup for the old token place), projects.json (still READ as the last fallback),
# backups/ (named backups by design, never scanned inside) and mk.db.bak-* (handled by prefix below).
AUDIT_HOME_FILES = {mkpaths.ROOT_DB_NAME, mkpaths.ROOT_DB_NAME + "-wal", mkpaths.ROOT_DB_NAME + "-shm",
                    mkpaths.MK_SETTINGS_NAME, "usage.json", "usage.json.lock", "config.json",
                    "bus-tokens.json", mkpaths.PROJECTS_JSON}
AUDIT_HOME_DIRS = {mkpaths.TARGET_BUS_DIR, "locks", mkpaths.TARGET_PROJECTS_DIR, "inbox", "backups",
                   mkpaths.BUS_LANES_DIR}
AUDIT_BAK_PREFIX = mkpaths.ROOT_DB_NAME + ".bak-"
# `.migrate.json` is mk-migrate's marker; `runs/` holds mk-state's per-project flock (runs/.lock)
AUDIT_PROJECT_FILES = {mkpaths.ROOT_DB_NAME, mkpaths.ROOT_DB_NAME + "-wal", mkpaths.ROOT_DB_NAME + "-shm",
                       mkpaths.PROJECT_YAML_NAME, mkpaths.PROJECT_YAML_NAME + ".lock", mkpaths.PROJECT_YAML_NAME + ".bak",
                       ".migrate.json"}
AUDIT_PROJECT_DIRS = {mkpaths.TARGET_PLANS_DIR, "archive", "runs"}
AUDIT_PLAN_ENTRIES = {"plan.md", "assets", "archive", "runs"}
# the root store also carries mk-catalog's C8 tables (agents, catalog_user, models): the catalog owns them
AUDIT_ROOT_CATALOG_TABLES = {"agents", "catalog_user", "models"}   # optional: allowed when present, never required
AUDIT_BUS_FILES = {"bus.json", "bus-tokens.json", "ring-queue.json", "bus.lock", "bus-ensure.lock",
                   "bus-tokens.lock"}
LANE_TOKEN_RE = re.compile(r"^[0-9a-f]{64}\.json$")
# names that must not exist ANYWHERE under a v2 home: the retired file views, registry and cursor files
AUDIT_FORBIDDEN = set(mkpaths.VIEW_DOC_NAMES) | {mkpaths.LANES_NAME, mkpaths.CURSORS_NAME, "lanes.lock",
                                                  "projects.json", "index.json"}
AUDIT_TREE_DEPTH = 4


def _audit_scan(path):
    """(files, dirs) names of one folder, symlinks counted as files (never followed)."""
    files, dirs = set(), set()
    with os.scandir(path) as it:
        for e in it:
            (dirs if e.is_dir(follow_symlinks=False) else files).add(e.name)
    return files, dirs


def _audit_names(home, rel, ok_files, ok_dirs, bad):
    """Every entry of home/rel must be a known file or folder name; the rest is drift."""
    path = os.path.join(home, rel) if rel else home
    files, dirs = _audit_scan(path)
    for n in sorted(files - ok_files):
        bad.append(f"{os.path.join(rel, n)} (unexpected file in {rel or 'the home root'})")
    for n in sorted(dirs - ok_dirs):
        bad.append(f"{os.path.join(rel, n)}/ (unexpected folder in {rel or 'the home root'})")
    return files, dirs


def _audit_tree_rules(home):
    """-> (tree list, A01 names issues, A08 forbidden-anywhere issues)."""
    harness = os.path.isfile(_marker_path(home))     # a `run`-built tree also holds its marker + manifest
    extra = {MARKER, "manifest.json"} if harness else set()
    names, forbidden = [], []
    top_files, top_scan_dirs = _audit_scan(home)
    lane_dirs = {n for n in top_scan_dirs if n.startswith("lane-")} if harness else set()  # `run`'s lane fixtures
    baks = {n for n in top_files if n.startswith(AUDIT_BAK_PREFIX)}
    _, top_dirs = _audit_names(home, "", AUDIT_HOME_FILES | extra | baks, AUDIT_HOME_DIRS | lane_dirs, names)
    proj_root = mkpaths.TARGET_PROJECTS_DIR
    if proj_root in top_dirs:
        files, dirs = _audit_scan(os.path.join(home, proj_root))
        for n in sorted(files):
            names.append(f"{proj_root}/{n} (a file where only project folders belong)")
        for slug in sorted(dirs):
            rel = f"{proj_root}/{slug}"
            _, pdirs = _audit_names(home, rel, AUDIT_PROJECT_FILES, AUDIT_PROJECT_DIRS, names)
            if mkpaths.TARGET_PLANS_DIR in pdirs:
                pl_files, pl_dirs = _audit_scan(os.path.join(home, rel, mkpaths.TARGET_PLANS_DIR))
                for n in sorted(pl_files):
                    names.append(f"{rel}/plans/{n} (a file where only plan folders belong)")
                for plan in sorted(pl_dirs):
                    # a plan folder also holds the plan's own design / input documents (any file); only a
                    # stray FOLDER name is drift
                    plan_files = _audit_scan(os.path.join(home, rel, "plans", plan))[0]
                    _audit_names(home, f"{rel}/plans/{plan}", plan_files, AUDIT_PLAN_ENTRIES - {"plan.md"}, names)
    if mkpaths.TARGET_BUS_DIR in top_dirs:
        bus = mkpaths.TARGET_BUS_DIR
        _, bdirs = _audit_names(home, bus, AUDIT_BUS_FILES, {mkpaths.TARGET_LANES_DIR}, names)
        if mkpaths.TARGET_LANES_DIR in bdirs:
            lfiles, ldirs = _audit_scan(os.path.join(home, bus, mkpaths.TARGET_LANES_DIR))
            for n in sorted(lfiles):
                if not LANE_TOKEN_RE.match(n):
                    names.append(f"{bus}/lanes/{n} (not a <sha256>.json lane token)")
            for n in sorted(ldirs):
                names.append(f"{bus}/lanes/{n}/ (a folder where only lane tokens belong)")
    tree = []
    for dirpath, dirnames, filenames in os.walk(home):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, home)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        walked = sorted(dirnames) + sorted(filenames)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d != "backups"]   # named backups: anything goes, never scanned inside
        for n in walked:
            rel = n if rel_dir == "." else os.path.join(rel_dir, n)
            if depth < AUDIT_TREE_DEPTH:
                tree.append(rel + ("/" if n in dirnames else ""))
            low = n.lower()
            is_db = low.endswith((".db", ".sqlite", ".sqlite3"))
            ok_db = (n == mkpaths.ROOT_DB_NAME and (rel_dir == "." or os.path.dirname(rel_dir) == proj_root)
                     and rel_dir.count(os.sep) <= 1) or os.path.basename(rel_dir) == "archive"
            allowed_root = rel_dir == "." and (n == mkpaths.PROJECTS_JSON or n.startswith(AUDIT_BAK_PREFIX))
            if (n in AUDIT_FORBIDDEN and n not in dirnames and not allowed_root) or (is_db and not ok_db and not allowed_root):
                forbidden.append(rel)
    return tree, names, sorted(set(forbidden))


def _audit_snapshot(home):
    """{relative path: (mtime_ns, size)} of every file under home: the read-only proof."""
    out = {}
    for dirpath, _dirs, files in os.walk(home):
        for n in files:
            full = os.path.join(dirpath, n)
            with contextlib.suppress(OSError):
                st = os.lstat(full)
                out[os.path.relpath(full, home)] = (st.st_mtime_ns, st.st_size)
    return out


def _audit_copy(db, tmp, n):
    """A private copy of a store (+ its WAL) to read: the original is never opened, so no -shm or
    checkpoint can touch it. A copy is a point-in-time view; a live writer mid-commit can tear it."""
    dst = os.path.join(tmp, f"{n}.db")
    shutil.copy2(db, dst)
    if os.path.isfile(db + "-wal"):
        shutil.copy2(db + "-wal", dst + "-wal")
    con = sqlite3.connect(dst)
    con.row_factory = sqlite3.Row
    return con


def _audit_tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not r[0].startswith("sqlite_")}


def _audit_version(con):
    if "schema_version" not in _audit_tables(con):
        return None
    return con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]


def do_audit(home=None):
    """The v2 layout + DB audit of a REAL home, strictly read-only: every store is read from a
    private copy, nothing under `home` is created, changed or deleted. -> the report dict."""
    home = os.path.realpath(home or _default_home())
    if not os.path.isdir(home):
        raise FAIL(f"no MK home at {home}")
    before = _audit_snapshot(home)
    rules, tables_out, schema_out = [], {}, {}
    tmp = tempfile.mkdtemp(prefix=PREFIX + "audit-")
    try:
        def rel(p):
            r = os.path.relpath(p, home)
            return p if r.startswith("..") else r

        def add(rid, what, bad, summary, info=()):
            # `paths` keeps every offender; the one-line detail names the first ten. `info` items are
            # facts that do NOT fail the rule (history, alias folders), listed in `info` and the detail.
            more = f" (+{len(bad) - 10} more)" if len(bad) > 10 else ""
            rules.append({"id": rid, "what": what, "pass": not bad, "paths": bad, "info": list(info),
                          "detail": summary + (f"; wrong {bad[:10]}{more}" if bad else "")
                          + (f"; info: {'; '.join(info)}" if info else "")})

        tree, names, forbidden = _audit_tree_rules(home)
        add("A01", "only known files and folders at each level of the tree", names,
            f"{len(tree)} tree entries, {len(names)} unexpected")
        add("A08", "no run view file, lanes.json, watch-cursors.json, projects.json or stray DB anywhere",
            forbidden, f"{len(forbidden)} forbidden")
        # the table sets a fresh production store has: the audit's reference, never a copy of the real home
        fresh = os.path.join(tmp, "fresh")
        fresh_root = mkdb.open_root(fresh)
        want_root = _audit_tables(fresh_root)
        fresh_root.close()
        fresh_proj = mkdb.open_project(fresh, "audit-fresh")
        want_proj = _audit_tables(fresh_proj)
        fresh_proj.close()
        root_db = mkpaths.db_in(home)
        bad_t, bad_v, bad_link, bad_over, bad_cnt, bad_tok = [], [], [], [], [], []
        info_link, info_tok = [], []
        root = None
        if os.path.isfile(root_db):
            root = _audit_copy(root_db, tmp, "root")
        else:
            bad_link.append("mk.db (no root store at the home root)")
        slugs, rows = set(), []
        if root is not None:
            tables_out["mk.db"] = sorted(_audit_tables(root))
            schema_out["mk.db"] = _audit_version(root)
            if "projects" in tables_out["mk.db"]:
                rows = [dict(r) for r in root.execute("SELECT slug, data_dir, db_path FROM projects")]
            slugs = {r["slug"] for r in rows}
        # every project store: those the root rows name, plus any projects/<x>/mk.db on disk
        stores = {}
        for r in rows:
            if r["db_path"]:
                stores.setdefault(os.path.realpath(r["db_path"]), []).append(r["slug"])
        proj_dir = os.path.join(home, mkpaths.TARGET_PROJECTS_DIR)
        folders = sorted(os.listdir(proj_dir)) if os.path.isdir(proj_dir) else []
        for d in folders:
            db = mkpaths.db_in(os.path.join(proj_dir, d))
            if os.path.isfile(db):
                stores.setdefault(os.path.realpath(db), [])
        for r in rows:
            if r["db_path"] and not os.path.isfile(r["db_path"]):
                bad_link.append(f"projects[{r['slug']}] db_path {rel(r['db_path'])} does not exist")
        row_dbs = {os.path.realpath(r["db_path"]) for r in rows if r["db_path"]}
        for d in folders:
            full = os.path.realpath(os.path.join(proj_dir, d))
            if os.path.isdir(full) and os.path.realpath(mkpaths.db_in(full)) not in row_dbs:
                if os.path.isfile(mkpaths.db_in(full)):
                    bad_link.append(f"{mkpaths.TARGET_PROJECTS_DIR}/{d} has no projects row (holds a stray mk.db)")
                else:
                    # no row and no store: the root-cleanup alias folder, kept while bus tokens carry its slug
                    info_link.append(f"{mkpaths.TARGET_PROJECTS_DIR}/{d} has no projects row and no store "
                                     "(alias folder kept for bus tokens): WARN, not a failure")
        proj_cons = {}
        for i, db in enumerate(sorted(stores)):
            if not os.path.isfile(db):
                continue
            name = rel(db)
            con = _audit_copy(db, tmp, f"p{i}")
            have = _audit_tables(con)
            tables_out[name] = sorted(have)
            schema_out[name] = _audit_version(con)
            if db not in row_dbs:
                con.close()          # a store no projects row owns is A04's finding alone: A02/A03 audit owned stores
                continue
            proj_cons[db] = con
            for t in sorted(want_proj - have):
                bad_t.append(f"{name} is missing table {t}")
            for t in sorted(have - want_proj):
                bad_t.append(f"{name} has extra table {t}")
            if schema_out[name] != mkdb.SCHEMA:
                bad_v.append(f"{name} schema_version {schema_out[name]} != {mkdb.SCHEMA}")
        if root is not None:
            have = set(tables_out["mk.db"])
            for t in sorted(want_root - have):
                bad_t.append(f"mk.db is missing table {t}")
            for t in sorted(have - want_root - AUDIT_ROOT_CATALOG_TABLES):
                bad_t.append(f"mk.db has extra table {t}")
            if schema_out["mk.db"] != mkdb.ROOT_SCHEMA:
                bad_v.append(f"mk.db schema_version {schema_out['mk.db']} != {mkdb.ROOT_SCHEMA}")
        add("A02", "each DB has exactly the table set of a fresh store", bad_t,
            f"{len(tables_out)} DBs")
        add("A03", "each DB carries the current schema_version", bad_v,
            ", ".join(f"{k}=v{v}" for k, v in sorted(schema_out.items())))
        add("A04", "every projects.db_path exists and every project folder has a row", bad_link,
            f"{len(rows)} projects rows, {len(folders)} project folders", info_link)
        # overview rows <-> project plans, and the counts in them
        plans_of, counts_of = {}, {}
        for db, con in proj_cons.items():
            if "plans" in _audit_tables(con) and "tasks" in _audit_tables(con):
                with contextlib.suppress(Exception):
                    for pid, _t, _s, counts_json, _lr in mkdb._plan_summary_values(con):
                        plans_of.setdefault(db, set()).add(pid)
                        counts_of[(db, pid)] = json.loads(counts_json or "{}")
        summary = []
        if root is not None and "plan_summary" in tables_out["mk.db"]:
            summary = [dict(r) for r in root.execute("SELECT project, plan, counts_json FROM plan_summary")]
        db_of_slug = {r["slug"]: os.path.realpath(r["db_path"]) for r in rows if r["db_path"]}
        seen = set()
        for r in summary:
            key = (r["project"], r["plan"])
            seen.add(key)
            if r["project"] not in slugs:
                bad_over.append(f"plan_summary row {r['project']}/{r['plan']} has no projects row")
                continue
            db = db_of_slug.get(r["project"])
            if db is None or db not in proj_cons:
                continue                                  # pre-migration project: A04 owns its store
            if r["plan"] not in plans_of.get(db, set()):
                bad_over.append(f"plan_summary row {r['project']}/{r['plan']} names a plan its project DB lacks")
                continue
            try:
                got = json.loads(r["counts_json"] or "{}")
            except ValueError:
                got = None
            if got != counts_of.get((db, r["plan"])):
                bad_cnt.append(f"plan_summary[{r['project']}/{r['plan']}] {got} != {counts_of.get((db, r['plan']))}")
        for slug, db in sorted(db_of_slug.items()):
            for pid in sorted(plans_of.get(db, set())):
                if (slug, pid) not in seen and not any(s != slug and db_of_slug.get(s) == db and (s, pid) in seen for s in slugs):
                    bad_over.append(f"plan {slug}/{pid} has no plan_summary row")
        add("A05", "overview rows and project plans agree (no orphan row, no missing row)", bad_over,
            f"{len(summary)} plan_summary rows")
        add("A06", "plan_summary counts equal the project DB's task counts", bad_cnt,
            f"{len(summary)} rows compared")
        # bus tokens: only the project slug / data dir of each is read, no secret is kept or printed
        data_dirs = {os.path.realpath(r["data_dir"]) for r in rows if r["data_dir"]}
        data_dirs |= {os.path.dirname(os.path.realpath(r["db_path"])) for r in rows if r["db_path"]}
        tok = os.path.join(home, mkpaths.TARGET_BUS_DIR, "bus-tokens.json")
        n_tok, stale_lane, gone_lane = 0, 0, 0
        if os.path.isfile(tok):
            try:
                with open(tok, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except ValueError:
                doc = None
                bad_tok.append("bus/bus-tokens.json is not valid JSON")
            unknown = {}
            for rec in (doc or {}).values() if isinstance(doc, dict) else []:
                n_tok += 1
                proj = rec.get("project") if isinstance(rec, dict) else None
                if proj not in slugs:
                    unknown[proj] = unknown.get(proj, 0) + 1
            # tokens of slugs with no row are HISTORY (temp test projects) unless the slug names a live
            # project folder: that folder has no row, which is real drift
            live = {d for d in folders if os.path.isdir(os.path.join(proj_dir, d))}
            for proj, n in sorted(unknown.items(), key=lambda kv: str(kv[0])):
                if proj in live:
                    bad_tok.append(f"bus/bus-tokens.json: {n} token(s) name slug {proj!r}, a live project folder with no projects row")
            hist = sum(n for k, n in unknown.items() if k not in live)
            if hist:
                info_tok.append(f"{hist} historical bus token(s) name {sum(1 for k in unknown if k not in live)} slug(s) "
                                "with no projects row and no folder (temp test projects)")
        lanes_dir = os.path.join(home, mkpaths.TARGET_BUS_DIR, mkpaths.TARGET_LANES_DIR)
        for n in sorted(os.listdir(lanes_dir)) if os.path.isdir(lanes_dir) else []:
            if not LANE_TOKEN_RE.match(n):
                continue
            n_tok += 1
            try:
                with open(os.path.join(lanes_dir, n), encoding="utf-8") as fh:
                    data = (json.load(fh) or {}).get("data")
            except (ValueError, AttributeError):
                data = None
            if not data or os.path.realpath(data) in data_dirs:
                continue
            if os.path.isdir(data):
                stale_lane = stale_lane + 1            # a data dir that exists on disk and no row owns
            else:
                gone_lane = gone_lane + 1              # its data dir is gone: history
        if stale_lane:
            bad_tok.append(f"bus/lanes/: {stale_lane} lane token(s) point at an existing data dir no projects row owns")
        if gone_lane:
            info_tok.append(f"{gone_lane} lane token(s) point at a data dir that no longer exists (history)")
        add("A07", "every bus token's project resolves to a projects row", bad_tok,
            f"{n_tok} tokens checked", info_tok)
        for con in [root, *proj_cons.values()]:
            if con is not None:
                con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    after = _audit_snapshot(home)
    # a live writer touches the -wal/-shm of its store: those are not the audit's doing, every other file is watched
    changed = sorted(k for k in set(before) | set(after)
                     if before.get(k) != after.get(k) and not k.endswith(("-wal", "-shm")))
    rules.sort(key=lambda r: r["id"])
    return {"ok": all(r["pass"] for r in rules), "home": home, "tree": tree, "tables": tables_out,
            "schema": schema_out, "rules": rules, "home_changed_during_audit": changed}


def do_audit_cmd(home=None):
    report = do_audit(home)
    print(json.dumps(report))
    return 0 if report["ok"] else 1


# --------------------------------------------------------------------------- self-test

def _fixture(ctx_skel):
    """A tiny C2 tree on disk: the root store, two project stores, one plan folder with a
    bus.jsonl, one lane token, one watch cursor. No server, no bus."""
    # realpath: refuse_home and mkpaths both realpath, so a symlinked temp dir would make
    # every relative path in the audit come out as ../..
    root = os.path.realpath(tempfile.mkdtemp(prefix=PREFIX + "fix-"))
    home = os.path.join(root, "home")
    os.makedirs(home)
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    scratch = os.path.join(root, "scratch")
    os.makedirs(scratch)
    _git(scratch, "init", "-q")
    slug, slug_b, plan, run = "fix", "fixb", "fix-plan", "fix-run"
    data = os.path.join(home, "projects", slug)
    os.makedirs(data)
    data_b = os.path.join(root, "legacy", slug_b)     # pre-migration: store stays at <data>
    os.makedirs(data_b)
    con = mkdb.open_root(home)
    try:
        mkdb._begin(con)
        con.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path, created)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (slug, slug, os.path.realpath(repo), data, "home",
                     mkpaths.db_in(data), "e2e"))
        con.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path, created)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (slug_b, slug_b, os.path.realpath(scratch), data_b, "home", None, "e2e"))
        con.execute("COMMIT")
    finally:
        con.close()
    proj = mkdb.open_project(home, slug)
    try:
        proj.execute("INSERT INTO plans(id, version, title, approved_at, archived_path, status)"
                     " VALUES (?,?,?,?,?,?)",
                     (plan, 1, "Fix plan", "2026-10-15", "plan-v1.md", "approved"))
        proj.execute("INSERT INTO runs(id, plan, status) VALUES (?,?,?)", (run, plan, "done"))
        proj.execute("INSERT INTO tasks(run, id, type, title, status) VALUES (?,?,?,?,?)",
                     (run, "P1", "phase", "Fix phase", "done"))
        proj.execute("INSERT INTO tasks(run, id, parent, type, title, status)"
                     " VALUES (?,?,?,?,?,?)", (run, "P1.1", "P1", "task", "Fix task", "done"))
        proj.commit()
    finally:
        proj.close()
    mkdb.open_project(home, slug_b, data_dir=data_b).close()
    # the root projection comes from the same production writer the real tools use, so the
    # fixture's two sides agree by construction, not by a hand-typed count
    mkdb.catch_up(home, slug, data_dir=data)
    pdir = mkpaths.target_plan_dir(slug, plan, home)
    os.makedirs(os.path.join(pdir, "assets"))
    os.makedirs(os.path.join(pdir, "archive"))
    with open(os.path.join(pdir, "plan.md"), "w", encoding="utf-8") as fh:
        fh.write("# fix plan\n")
    rdir = mkpaths.target_run_dir(slug, plan, run, home)
    os.makedirs(rdir)
    with open(os.path.join(rdir, "bus.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "m-1", "kind": "info", "seq": 1}) + "\n")
        fh.write(json.dumps({"id": "m-2", "kind": "commit", "seq": 2}) + "\n")
    bus = mkpaths.target_bus_dir(home)
    os.makedirs(os.path.join(bus, "lanes"))
    for name in ("bus.json", "bus-tokens.json", "ring-queue.json"):
        with open(os.path.join(bus, name), "w", encoding="utf-8") as fh:
            json.dump({"ok": True}, fh)
    lane = os.path.join(home, "lane-fix")
    os.makedirs(lane)
    with open(mkpaths.target_lane_token(lane, home), "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    os.chmod(mkpaths.target_lane_token(lane, home), 0o600)
    # L10.5: the cursor is a watch_cursors row of the project DB, and the registry holds one lane
    # slot row; no watch-cursors.json / lanes.json is ever written
    store = mkdb.open_project(home, slug, data_dir=data)
    try:
        store.execute("BEGIN IMMEDIATE")
        store.execute("INSERT INTO watch_cursors(key, seq, started, updated) VALUES (?,?,?,?)",
                      (f"{run}|*", 2, "2026-10-16T00:00:00Z", "2026-10-16T00:00:01Z"))
        store.execute("INSERT INTO lane_slots(slot, lane_path, state) VALUES (?,?,?)", ("1", lane, "free"))
        store.execute("INSERT INTO settings(key, value, updated) VALUES (?,?,?)",
                      ("lanes.default_agent", '"claude"', "2026-10-16T00:00:00Z"))
        store.execute("COMMIT")
    finally:
        store.close()
    with open(mkpaths.project_yaml_in(data), "w", encoding="utf-8") as fh:
        fh.write("# onboarding's project.yaml: the one yaml a migrated project folder may hold\n")
    with open(mkpaths.mk_settings(home), "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    manifest = {
        "repo": repo, "scratch_repo": scratch,
        "plans": [[slug, plan]], "runs": [[slug, plan, run]],
        "lanes": [lane], "data_of": {slug: data, slug_b: data_b},
        "pre_migration_slug": slug_b,
        "archived_counts": {f"{slug}/{plan}/{run}": 2},
        "legacy_db": {slug: os.path.join(data, "legacy.db")},
        "legacy_digest_after": {}, "new_store_before": {},
        "bus_files_up": ["bus.json", "bus-tokens.json", "ring-queue.json"],
    }
    with open(os.path.join(home, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return {"root": root, "home": home, "repo": repo, "scratch": scratch,
            "slug": slug, "slug_b": slug_b, "plan": plan, "run": run,
            "data": data, "data_b": data_b, "lane": lane}


def _sql(f, sql):
    """One write on the fixture's project store, through the one SQLite writer."""
    con = mkdb.open_project(f["home"], f["slug"], data_dir=f["data"])
    try:
        con.execute(sql)
        con.commit()
    finally:
        con.close()


def _drop_table(f, table):
    _sql(f, f"DROP TABLE {table}")


def _audit_fixture():
    """The clean fixture shaped like a real v2 home: no manifest, no lane folder, a real bus-tokens
    file and a lane token that resolves to its project's data dir."""
    f = _fixture(None)
    os.unlink(os.path.join(f["home"], "manifest.json"))
    shutil.rmtree(f["lane"], ignore_errors=True)
    bus = mkpaths.target_bus_dir(f["home"])
    with open(os.path.join(bus, "bus-tokens.json"), "w", encoding="utf-8") as fh:
        json.dump({"tk1": {"project": f["slug"], "token": "x", "role": "orch", "run": f["run"],
                           "task": None, "lane": None, "created_at": "2026-10-16T00:00:00Z"}}, fh)
    lane_token = mkpaths.target_lane_token(f["lane"], f["home"])
    with open(lane_token, "w", encoding="utf-8") as fh:
        json.dump({"v": 1, "url": "http://127.0.0.1:1", "token": "x", "run": f["run"], "task": "T1",
                   "data": f["data"]}, fh)
    os.chmod(lane_token, 0o600)
    return f


def _audit_mutations():
    """(rule id, what was broken, token the failure must name, mutator): one per drift kind."""
    out = []

    def m(rid, name, token):
        def deco(fn):
            out.append((rid, name, token, fn))
            return fn
        return deco

    @m("A04", "a stray mk.db in a project folder with no row", "ghost")
    def _(f):
        os.makedirs(os.path.join(f["home"], "projects", "ghost"))
        with open(os.path.join(f["home"], "projects", "ghost", "mk.db"), "wb") as fh:
            fh.write(b"")

    @m("A01", "an extra file at the home root", "notes.txt")
    def _(f):
        with open(os.path.join(f["home"], "notes.txt"), "w") as fh:
            fh.write("x")

    @m("A02", "a missing table in the project DB", "watch_cursors")
    def _(f):
        _drop_table(f, "watch_cursors")

    @m("A02", "an extra table in the project DB", "junk_table")
    def _(f):
        _sql(f, "CREATE TABLE junk_table(a)")

    @m("A03", "a wrong schema version in the project DB", "99")
    def _(f):
        _sql(f, "INSERT INTO schema_version(version) VALUES (99)")

    @m("A05", "an orphan plan_summary row for an unknown project", "ghostproj/ghostplan")
    def _(f):
        con = mkdb.open_root(f["home"])
        try:
            con.execute("INSERT INTO plan_summary(project, plan, title, status, counts_json, live_run, updated)"
                        " VALUES ('ghostproj','ghostplan','t','approved','{}',NULL,'x')")
            con.commit()
        finally:
            con.close()

    @m("A07", "a bus token naming the slug of a live project folder with no row", "ghostslug")
    def _(f):
        os.makedirs(os.path.join(f["home"], "projects", "ghostslug"))      # no store: A04 only warns
        with open(os.path.join(mkpaths.target_bus_dir(f["home"]), "bus-tokens.json"), "w", encoding="utf-8") as fh:
            json.dump({"tk9": {"project": "ghostslug", "token": "x"}}, fh)

    @m("A07", "a lane token pointing at an existing data dir no row owns", "lane token(s)")
    def _(f):
        orphan = os.path.join(f["root"], "orphan-data")
        os.makedirs(orphan)
        token = mkpaths.target_lane_token(os.path.join(f["home"], "lane-orphan"), f["home"])
        with open(token, "w", encoding="utf-8") as fh:
            json.dump({"v": 1, "url": "http://127.0.0.1:1", "token": "x", "run": "r", "task": "T", "data": orphan}, fh)

    @m("A06", "a plan_summary count that disagrees with the project DB", "plan_summary[")
    def _(f):
        con = mkdb.open_root(f["home"])
        try:
            con.execute("UPDATE plan_summary SET counts_json='{\"done\": 999}'")
            con.commit()
        finally:
            con.close()

    @m("A08", "a lanes.json deep in a plan folder", "lanes.json")
    def _(f):
        with open(os.path.join(mkpaths.target_plan_dir(f["slug"], f["plan"], f["home"]), "runs", "lanes.json"), "w") as fh:
            fh.write("{}")

    return out


def _audit_exit(home):
    with contextlib.redirect_stdout(open(os.devnull, "w")):
        return do_audit_cmd(home)


def audit_self_test():
    """The clean fixture passes every audit rule and the audit leaves every file under the home
    byte- and mtime-identical; each drift kind fails its own rule and names the offending path."""
    f = _audit_fixture()
    try:
        shot = _audit_snapshot(f["home"])
        rep = do_audit(f["home"])
        _check(rep["ok"], f"the clean fixture fails the audit: {[r for r in rep['rules'] if not r['pass']]}")
        _check(_audit_snapshot(f["home"]) == shot, "the audit changed a file under the home")
        _check(rep["home_changed_during_audit"] == [], f"home changed: {rep['home_changed_during_audit']}")
        _check(_audit_exit(f["home"]) == 0, "audit exit code on a clean home is not 0")
        _check({r["id"] for r in rep["rules"]} == {f"A0{i}" for i in range(1, 9)}, "rule ids drifted")
        _check("mk.db" in rep["tables"] and "projects/fix/mk.db" in rep["tables"], f"tables: {list(rep['tables'])}")
        _check(any(t.startswith("projects/fix/") for t in rep["tree"]), "tree lacks the project folder")
    finally:
        shutil.rmtree(f["root"], ignore_errors=True)
    # L10.14 R2: a HEALTHY real home carries entries a fresh v2 home does not, and none of them is drift
    f = _audit_fixture()
    try:
        home = f["home"]
        for name in ("config.json", "projects.json"):
            with open(os.path.join(home, name), "w") as fh:
                fh.write("{}")
        with open(os.path.join(home, "mk.db.bak-20261013"), "wb") as fh:
            fh.write(b"bak")
        os.makedirs(os.path.join(home, "bus-lanes"))
        os.makedirs(os.path.join(home, "backups", "root-pre-x"))
        for rel in ("root-pre-x/mk.db", "old.db", "root-pre-x/projects.json", "root-pre-x/lanes.json"):
            with open(os.path.join(home, "backups", rel), "w") as fh:
                fh.write("x")
        with open(os.path.join(home, "bus-tokens.json"), "w") as fh:
            fh.write("{}")
        with open(os.path.join(mkpaths.target_project_dir(f["slug"], home), "project.yaml.bak"), "w") as fh:
            fh.write("x")
        alias = os.path.join(home, "projects", "pdeadbeef")            # no row, no store: an alias folder
        os.makedirs(alias)
        with open(os.path.join(alias, "project.yaml"), "w") as fh:
            fh.write("x: 1\n")
        tok = os.path.join(mkpaths.target_bus_dir(home), "bus-tokens.json")
        with open(tok, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["old1"] = {"project": "p0123456789abcdef", "token": "x"}   # history of a temp test project
        with open(tok, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        gone = mkpaths.target_lane_token(os.path.join(home, "lane-gone"), home)
        with open(gone, "w", encoding="utf-8") as fh:
            json.dump({"v": 1, "url": "http://127.0.0.1:1", "token": "x", "run": "r", "task": "T",
                       "data": os.path.join(f["root"], "deleted-data")}, fh)
        rep = do_audit(home)
        _check(rep["ok"], f"a healthy home with the legitimate extras fails: {[r for r in rep['rules'] if not r['pass']]}")
        by = {r["id"]: r for r in rep["rules"]}
        _check(by["A04"]["info"] and "alias" in by["A04"]["detail"], f"the alias folder is not reported as a warning: {by['A04']}")
        _check(len(by["A07"]["info"]) == 2, f"history tokens are not counted as info: {by['A07']['info']}")
        # a live writer touching a -wal/-shm during the audit is not a change the audit made
        _check(do_audit(home)["home_changed_during_audit"] == [], "home_changed_during_audit is not empty on a quiet home")
    finally:
        shutil.rmtree(f["root"], ignore_errors=True)
    for rid, name, token, mutate in _audit_mutations():
        f = _audit_fixture()
        try:
            mutate(f)
            shot = _audit_snapshot(f["home"])
            rep = do_audit(f["home"])
            _check(_audit_snapshot(f["home"]) == shot, f"{rid} ({name}): the audit changed a file")
            failed = {r["id"]: r for r in rep["rules"] if not r["pass"]}
            _check(rid in failed, f"{rid} ({name}): not failed (failed: {sorted(failed)})")
            _check(not rep["ok"], f"{rid} ({name}): ok despite drift")
            text = " ".join(" ".join(r["paths"]) + " " + r["detail"] for r in failed.values())
            _check(token in text, f"{rid} ({name}): the failure does not name {token!r}: {text[:300]}")
            _check(_audit_exit(f["home"]) == 1, f"{rid} ({name}): exit code is not 1")
        finally:
            shutil.rmtree(f["root"], ignore_errors=True)


def _mutations(_unused):
    """Each mutation breaks exactly one rule. The tuple is (rule id, what was broken, the
    exact token the failure must name, the mutator) — so a rule that stopped naming the
    offending path fails the self-test instead of quietly passing."""
    out = []

    def m(rid, name, token):
        def deco(fn):
            out.append((rid, name, token, fn))
            return fn
        return deco

    @m("R01", "a stray mk.db under the temp root", "fix-stray/mk.db")
    def _(f):
        stray = mkpaths.db_in(os.path.join(f["home"], "projects", f["slug"] + "-stray"))
        os.makedirs(os.path.dirname(stray))
        with open(stray, "w", encoding="utf-8") as fh:
            fh.write("not a store")

    @m("R02", "a plan folder with no archive/", "archive")
    def _(f):
        shutil.rmtree(os.path.join(mkpaths.target_plan_dir(f["slug"], f["plan"], f["home"]),
                                   "archive"))

    @m("R03", "a missing bus.jsonl", "bus.jsonl")
    def _(f):
        os.unlink(os.path.join(mkpaths.target_run_dir(f["slug"], f["plan"], f["run"], f["home"]),
                               "bus.jsonl"))

    @m("R04", "a .matrixkoder in the scratch repo", ".matrixkoder")
    def _(f):
        os.makedirs(os.path.join(f["scratch"], ".matrixkoder", "plans"))

    @m("R05", "a stray run.json", "run.json")
    def _(f):
        d = os.path.join(f["home"], "projects", f["slug"], "runs", f["run"])
        os.makedirs(d, exist_ok=True)
        with open(mkpaths.run_doc_in(d), "w", encoding="utf-8") as fh:
            fh.write("{}")

    @m("R06", "a wrong plan_summary count", "plan_summary")
    def _(f):
        con = mkdb.open_root(f["home"])  # the one SQLite writer (single-writer rule)
        try:
            con.execute("UPDATE plan_summary SET counts_json=? WHERE plan=?",
                        (json.dumps({"done": 99}), f["plan"]))
            con.commit()
        finally:
            con.close()

    @m("R07", "a bus.jsonl line count that does not match the archive", "archived")
    def _(f):
        with open(os.path.join(mkpaths.target_run_dir(f["slug"], f["plan"], f["run"], f["home"]),
                               "bus.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "m-3", "kind": "info", "seq": 3}) + "\n")

    @m("R08", "a lane token at the old path", "bus-lanes")
    def _(f):
        tok = mkpaths.target_lane_token(f["lane"], f["home"])
        old = os.path.join(f["home"], "bus-lanes")      # the pre-L6.1 spelling
        os.makedirs(old, exist_ok=True)
        with open(os.path.join(old, os.path.basename(tok)), "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.unlink(tok)

    @m("R09", "a bus file at the pre-L6.1 home-root path", "home root")
    def _(f):
        with open(os.path.join(f["home"], "bus.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")

    @m("R10", "watch cursors in a data dir instead of the project folder", "watch-cursors.json")
    def _(f):
        # a pre-migration project's cursors must stay in its data dir, never appear as a
        # second file anywhere under the tree
        legacy = mkpaths.legacy_cursors(f["data_b"])
        with open(legacy, "w", encoding="utf-8") as fh:
            json.dump({"fix-run|*": {"seq": 9}}, fh)
        os.chmod(legacy, 0o600)

    @m("R10", "a watch-cursors.json in the C2 project folder", "watch-cursors.json")
    def _(f):
        path = os.path.join(mkpaths.target_project_dir(f["slug"], f["home"]), mkpaths.CURSORS_NAME)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"fix-run|*": {"seq": 9}}, fh)

    @m("R10", "a watch_cursors table missing from the project DB", "no watch_cursors table")
    def _(f):
        _drop_table(f, "watch_cursors")

    @m("R10", "a malformed watch_cursors row", "malformed")
    def _(f):
        _sql(f, "INSERT INTO watch_cursors(key, seq) VALUES ('nokey', -3)")

    @m("R16", "a lanes.json in the C2 project folder", "lanes.json")
    def _(f):
        path = os.path.join(mkpaths.target_project_dir(f["slug"], f["home"]), mkpaths.LANES_NAME)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "slots": {}}, fh)

    @m("R16", "a lanes.json in a legacy data dir", "lanes.json")
    def _(f):
        with open(os.path.join(f["data_b"], mkpaths.LANES_NAME), "w", encoding="utf-8") as fh:
            fh.write("{}")

    @m("R16", "a lane_slots table missing from the project DB", "no lane_slots table")
    def _(f):
        _drop_table(f, "lane_slots")

    @m("R17", "a legacy matrixkoder.yaml inside a migrated project folder", "matrixkoder.yaml")
    def _(f):
        with open(os.path.join(mkpaths.target_project_dir(f["slug"], f["home"]), "matrixkoder.yaml"), "w") as fh:
            fh.write("data_dir: x\n")

    @m("R17", "a project.yaml in the scratch repo", "project.yaml")
    def _(f):
        with open(os.path.join(f["scratch"], mkpaths.PROJECT_YAML_NAME), "w") as fh:
            fh.write("x: 1\n")

    @m("R17", "a mk-settings.yaml outside the home root", "mk-settings.yaml")
    def _(f):
        with open(os.path.join(f["repo"], "mk-settings.yaml"), "w") as fh:
            fh.write("{}\n")

    @m("R17", "a settings table missing from the project DB", "no settings table")
    def _(f):
        _drop_table(f, "settings")

    @m("R12", "a dropped sync-era table back in the root store", "project_summary")
    def _(f):
        con = mkdb.open_root(f["home"])
        try:
            con.execute("CREATE TABLE project_summary(project TEXT)")
            con.commit()
        finally:
            con.close()

    @m("R11", "a post-switch write that never reached the NEW store", "did not change")
    def _(f):
        con = mkdb.open_root(f["home"])
        try:
            row = con.execute("SELECT db_path FROM projects WHERE slug=?",
                              (f["slug"],)).fetchone()
        finally:
            con.close()
        manifest = os.path.join(f["home"], "manifest.json")
        with open(manifest, encoding="utf-8") as fh:
            man = json.load(fh)
        man["new_store_before"] = {f["slug"]: _store_digest(row["db_path"])}
        with open(manifest, "w", encoding="utf-8") as fh:
            json.dump(man, fh)

    @m("R12", "a tasks table in the root store", "tasks")
    def _(f):
        con = mkdb.open_root(f["home"])  # the one SQLite writer (single-writer rule)
        try:
            con.execute("CREATE TABLE tasks(run TEXT, id TEXT)")
            con.commit()
        finally:
            con.close()

    @m("R13", "a per-plan DB", "plan.sqlite3")
    def _(f):
        pdir = mkpaths.target_plan_dir(f["slug"], f["plan"], f["home"])
        with open(os.path.join(pdir, "plan.sqlite3"), "w", encoding="utf-8") as fh:
            fh.write("x")

    @m("R14", "a missing plan_summary row", "no plan_summary row")
    def _(f):
        con = mkdb.open_root(f["home"])  # the one SQLite writer (single-writer rule)
        try:
            con.execute("DELETE FROM plan_summary")
            con.commit()
        finally:
            con.close()


    @m("R01", "disagreeing db_paths for one data dir", "disagreeing")
    def _(f):
        con = mkdb.open_root(f["home"])
        try:
            con.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path, created)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (f["slug"] + "-alias", f["slug"] + "-alias", os.path.realpath(f["repo"]),
                         f["data"], "home", mkpaths.db_in(os.path.join(f["home"], "projects", f["slug"] + "-alias")),
                         "e2e"))
            con.commit()
        finally:
            con.close()

    @m("R01", "a missing second project store", "fixb")
    def _(f):
        os.unlink(mkpaths.db_in(f["data_b"]))
    return out


def self_test():
    """The clean fixture passes; each mutation makes exactly its own rule fail, by name."""
    for rid, name, token, mutate in _mutations(None):
        # a fresh fixture per case: exactly one thing is broken at a time
        f = _fixture(None)
        try:
            mutate(f)
            _check(do_check(f["home"], quiet=True) == 1, f"{rid} ({name}): check did not fail")
            results = {r["id"]: r for r in _results_of(f["home"])}
            _check(not results[rid]["ok"], f"{rid} ({name}): the rule passed anyway")
            others = [k for k, v in results.items() if k != rid and not v["ok"]]
            _check(not others, f"{rid} ({name}) also broke {others}")
            named = " ".join(results[rid]["paths"] or []) + " " + results[rid]["detail"]
            _check(token in named,
                   f"{rid} ({name}): the failure does not name {token!r}: {named[:300]}")
        finally:
            shutil.rmtree(f["root"], ignore_errors=True)
    # the clean fixture passes every rule
    f = _fixture(None)
    try:
        broken = [r["id"] for r in _results_of(f["home"]) if not r["ok"]]
        _check(not broken, f"the clean fixture fails {broken}")
    finally:
        shutil.rmtree(f["root"], ignore_errors=True)
    # (a) alias + canonical rows with one db_path: R01 and R11 must PASS, not fail
    f = _fixture(None)
    try:
        con = mkdb.open_root(f["home"])
        try:
            row = con.execute("SELECT db_path FROM projects WHERE slug=?",
                              (f["slug"],)).fetchone()
            con.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path, created)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (f["slug"] + "-alias", f["slug"] + "-alias", os.path.realpath(f["repo"]),
                         f["data"], "home", row["db_path"], "e2e"))
            con.commit()
        finally:
            con.close()
        results = {r["id"]: r for r in _results_of(f["home"])}
        _check(results["R01"]["ok"],
               f"alias row with same db_path makes R01 fail: {results['R01']['detail']}")
        _check(results["R11"]["ok"],
               f"alias row with same db_path makes R11 fail: {results['R11']['detail']}")
    finally:
        shutil.rmtree(f["root"], ignore_errors=True)
    # R15 (Z21): the temp-row detector sees the REALPATH spelling of the temp tree. On macOS
    # tempfile.gettempdir() is /var/folders/<xx>/<hash>/T while every STORED path is a realpath
    # (/private/var/folders/...), so `startswith(tempfile.gettempdir())` matched nothing and R15
    # never fired. /tmpfoo is a real folder, not a temp root: the match is on a path boundary.
    f = _fixture(None)
    try:
        tmp_lit, tmp_real = tempfile.gettempdir(), os.path.realpath(tempfile.gettempdir())
        rows = [("pf1", tmp_real + "/x", None),          # the realpath spelling: the bug
                ("pf2", "/private/tmp/x", None),         # /tmp's own realpath spelling
                ("pf3", "/tmp/x", None),                 # the literal /tmp spelling
                ("pf4", tmp_lit + "/x", None),           # gettempdir() spelled out
                ("pf5", "/tmpfoo/x", None),              # NOT a temp root
                ("pf6", None, mkpaths.db_in("/private/tmp/y"))]   # db_path counts, not only data_dir
        con = mkdb.open_root(f["home"])    # the one SQLite writer (single-writer rule)
        try:
            for slug, data, dbp in rows:
                con.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path, created)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (slug, slug, os.path.realpath(f["repo"]), data, "home", dbp, "e2e"))
            con.commit()
        finally:
            con.close()
        # the detector resolves the REAL home through _default_home(); patch the MODULE global at
        # the fixture's home so the case exercises _real_projects_rows itself, not a predicate
        # stand-in. globals() because a plain assignment would make _default_home local to
        # self_test and shadow every use of it in this function.
        def _fixture_home():
            return f["home"]

        saved_home = globals()["_default_home"]
        globals()["_default_home"] = _fixture_home
        try:
            got = _real_projects_rows()
        finally:
            globals()["_default_home"] = saved_home
        _check(not any(g.startswith("pf5 ") for g in got), f"/tmpfoo matched a temp root: {got}")
        for slug in ("pf1", "pf2", "pf3", "pf4", "pf6"):
            _check(any(g.startswith(slug + " ->") for g in got), f"{slug} not read as a temp row: {got}")
        # the fixture's own rows are realpath temp paths — the exact shape the old rule missed
        _check(any(g.startswith("fix ->") for g in got),
               f"a realpath temp row went unseen: {got}")
        # the boundary rule itself, spelled out: a prefix is not a match
        _check(_temp_row("/tmp") and _temp_row("/tmp/x") and _temp_row("/private/tmp/x")
               and _temp_row(tmp_real + "/x") and _temp_row(tmp_lit + "/x")
               and not _temp_row("/tmpfoo") and not _temp_row("/tmpfoo/x")
               and not _temp_row(tmp_real + "x")     # the boundary: .../T is not .../Tx
               and not _temp_row(None) and not _temp_row(""), "the temp-root boundary rule drifted")
    finally:
        shutil.rmtree(f["root"], ignore_errors=True)
    # Z21: the refusal catch is `except Exception`, so a KeyboardInterrupt from the open is NOT
    # swallowed as "the guard refused" — only mkdb.Fail (an Exception subclass) takes that path.
    real_open_root = mkdb.open_root

    def _interrupted(home, *a, **kw):
        raise KeyboardInterrupt

    mkdb.open_root = _interrupted
    try:
        raised = False
        try:
            _guard_refuses_real_home()
        except KeyboardInterrupt:
            raised = True
        _check(raised, "a KeyboardInterrupt from the open was swallowed as a refused write")
    finally:
        mkdb.open_root = real_open_root
    # the refusal, before anything is created
    _cli_fail(["--home", os.path.expanduser("~/.matrixkoder"), "check"])
    _cli_fail(["--home", os.path.join(ROOT, ".matrixkoder-e2e-probe"), "run"])
    _check(not os.path.exists(os.path.join(ROOT, ".matrixkoder-e2e-probe")),
           "a refused run created a directory")
    # C16 case (c): a planted UNPINNED write to the REAL default home is refused, in-process.
    _guard_refuses_real_home()
    # clean teardown: a run root named mk-e2e-layout-* is removable after the same prefix
    # (Z17: livetest._check_root demanded mk-livetest-* here, so teardown was dead code).
    root = os.path.realpath(tempfile.mkdtemp(prefix=PREFIX))
    home = os.path.join(root, "home")
    os.makedirs(home)
    _write_marker(root, home, [])
    teardown(home, quiet=True)
    _check(not os.path.exists(root), "teardown left the run root behind")
    audit_self_test()
    print("SELF-TEST PASS")
    return 0




def _results_of(home):
    """(rule id, result) pairs for a home, without writing the report files twice."""
    ctx = build_context(home)
    out = []
    for rule in build_rules():
        ok, detail, paths = rule.run(ctx)
        out.append({"id": rule.id, "contract": rule.contract, "what": rule.what,
                    "ok": bool(ok), "detail": detail, "paths": paths})
    return out


def _check(cond, msg):
    if not cond:
        raise AssertionError(f"self-test: {msg}")


def _cli_fail(argv):
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), *argv],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode == 0:
        raise AssertionError(f"self-test: {argv} was accepted")
    if "Traceback" in proc.stderr:
        raise AssertionError(f"self-test: {argv} crashed: {proc.stderr[-300:]}")
    return proc


# --------------------------------------------------------------------------- CLI

class Parser(argparse.ArgumentParser):
    def __init__(self, *a, **kw):
        kw.setdefault("allow_abbrev", False)
        super().__init__(*a, **kw)

    def error(self, message):
        raise FAIL(message)


def _parser():
    p = Parser(prog="mk-e2e-layout.py", description=__doc__,
                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", help="MK home; must be under the system temp dir, never ~/.matrixkoder")
    sub = p.add_subparsers(dest="cmd")
    rn = sub.add_parser("run")
    rn.add_argument("--slug", default="e2e")
    rn.add_argument("--keep", action="store_true", help="leave the tree for `show`/`check`")
    rn.add_argument("--home", default=argparse.SUPPRESS)
    ck = sub.add_parser("check")
    ck.add_argument("--home", default=argparse.SUPPRESS)
    sh = sub.add_parser("show")
    sh.add_argument("--home", default=argparse.SUPPRESS)
    au = sub.add_parser("audit")
    au.add_argument("--home", default=argparse.SUPPRESS)
    td = sub.add_parser("teardown")
    td.add_argument("--home", default=argparse.SUPPRESS)
    return p


def main(argv):
    if argv == ["--self-test"]:
        try:
            return self_test()
        except (AssertionError, FAIL) as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
    args = _parser().parse_args(argv)
    home = getattr(args, "home", None)
    try:
        if args.cmd == "run":
            if home:
                # `run` makes its OWN temp home; accepting one would be the one way to point
                # this tool at a real ~/.matrixkoder, so it is refused, not ignored.
                raise FAIL(f"run creates its own temp home; refusing --home {home}")
            do_run(args.slug, keep=args.keep)
            return 0
        if args.cmd == "check":
            if not home:
                raise FAIL("check needs --home (from a `run --keep` line)")
            return do_check(home)
        if args.cmd == "show":
            if not home:
                raise FAIL("show needs --home")
            return do_show(home)
        if args.cmd == "audit":
            return do_audit_cmd(home)       # no --home: the user's real home, read-only
        if args.cmd == "teardown":
            return teardown(home)
        raise FAIL("usage: mk-e2e-layout.py run [--keep] | check --home D | show --home D | "
                   "audit [--home D] | teardown --home D | --self-test")
    except FAIL as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return getattr(e, "code", 2)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))