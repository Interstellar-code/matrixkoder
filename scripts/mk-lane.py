#!/usr/bin/env python3
"""MatrixKoder lanes — computed names + a small pool of reusable worktree slots. Python 3 stdlib only.

  names   --run R --task T [--role build|fix|takeover|review] [--round N] [--slot N]
  acquire --run R (--task T | --tasks A,B,C) [--base REF] [--role ROLE] [--round N]
           (--tasks: one slot for the group, the lane fields written on every listed task)
  release --slot N --integration REF
  gc      [--keep K]
  list
  --self-test

Common: --data DIR (resolved exactly as mk-state.py does), --lock-timeout S (hidden, default 30).
Every command prints ONE JSON line. Exit 0 ok; 2 bad input, a git/herdr failure, or lanes.lock busy;
3 the slot is not reusable as it stands — dirty working tree, or commits on no branch (acquire and
release both refuse: no state change, no git command run); 4 all slots busy.

Nothing is ever aimed at a path this repo does not own: a stored slot keeps the path it was created
with, a slot path must be a registered worktree of this repo and never the main checkout, and every
ref argument must resolve to a commit here before it reaches git.

Why slots: one git worktree per task means pnpm install, a fresh .env copy and a fresh agent
folder-trust prompt for every task. A slot is a persistent checkout reused across tasks: acquire
takes the lowest free clean slot, release detaches it back to the integration branch. Names are
computed here, so no agent invents one. `lanes: copy: [...]` names gitignored paths a slot needs
(.env, generated types): they are copied from the main checkout on every acquire — a reused slot
gets only the targets it is missing, an existing target is never overwritten. Every acquire also
refreshes the lane's own tooling: stale top-level `.matrixkoder-*` reports move into
`.matrixkoder-old-<run>/` first (archived, never deleted), then the plugin's child-skills/mk-builder/
is copied to <slot>/.claude/skills/mk-builder (overwritten, so the version always matches the
plugin) with an executable git guard at <slot>/.matrixkoder-bin/git.

This tool belongs to the ORCHESTRATOR: it runs `git switch` inside a slot, only after the slot is
verified clean and never with force. Builders never run it. It owns <data>/lanes.json (outside runs/);
run state stays mk-state.py's alone — the task keys written there are best-effort (a failure is a
warning, never a failed acquire), the run id must be one mk-state knows (checked first, nothing is
created otherwise) and task ids are written in their stored case (mk-state matches ids by case).
Schema: docs/state-schema.md
"""
import argparse, contextlib, fcntl, fnmatch, hashlib, importlib.util, json, math, os, re
import shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True  # no stray __pycache__ next to the scripts

SCHEMA = 1
ROLES = ("build", "fix", "takeover", "review")
AGENT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")  # herdr's name limit: 32 chars, lowercase
ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")  # run/task ids after lowercasing
LOCKFILES = ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb",
             "uv.lock", "poetry.lock", "Cargo.lock")
# Ignored entries a slot may keep between tasks; anything else is a pause item, never discarded.
# `.claude/` AND `.claude/skills/mk-builder/` (both, never just one): with only the ignored skill
# dir inside it, git collapses the status entry to `!! .claude/`, which the dir pattern alone does
# not match; when other untracked .claude/ content makes git descend, the entry names the skill dir
# instead. Both entries, so both shapes read disposable.
# `.omc/` is not on this list. Ignored `.omc` state is disposable only through the path-segment
# rule in omc_ignored_ok: a `!!` entry whose path has a `.omc` segment, and only while that tree
# tracks nothing. A tracked `.omc/plans/*.md` (or any other non-`!!` line) still counts. Listing
# `.omc/` here would also swallow an ignored file inside a TRACKED `.omc/` (the 11g case).
DISPOSABLE = (".matrixkoder-*", ".env", ".env.*", "node_modules/", ".omo/", ".serena/",
              ".venv/", "__pycache__/", ".claude/", ".claude/skills/mk-builder/",
              ".omp/", ".omp/skills/mk-builder/", ".claude-plugin/types/", ".ruff_cache/")
# The ignored scratch release REMOVES from the slot before parking it (the lane cleanup the skill
# docs describe: `reference/lane-rules.md`, the release step of `skills/matrixkoder/SKILL.md` and
# `reference/loop.md`). Literal root-level names only — no globs — and each must also be on
# DISPOSABLE, so a slot whose only leftover is one of these reads clean and is freed. Everything
# else stays a pause item: any other ignored leftover still refuses.
SCRATCH = (".ruff_cache", ".claude-plugin/types")
# The build-cache dirs every package/venv path ends with: allowed as a path component at any depth,
# so a monorepo's packages/a/node_modules/ does not read as dirty. Everything else stays root-only.
ANY_DEPTH = ("node_modules/", "__pycache__/", ".venv/")
# A lanes_disposable entry is a literal name: no globs, no paths, no `..` — a wildcard would make
# every ignored file disposable and the next gc would delete the user's data.
DISPOSABLE_RE = re.compile(r"[A-Za-z0-9._-]+/?")
# A lanes.copy entry is a literal relative path: same reasoning (a glob would widen the clean
# check's allowlist, `..`/`.` would leave the main checkout) plus no absolute paths.
COPY_RE = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*/?")
LOCK_TIMEOUT = 30.0
GIT_TIMEOUT, HERDR_TIMEOUT = 120, 300
# The lane tooling acquire installs: the plugin's own mk-builder skill copy and its git guard,
# resolved from the script's own directory (the plugin root is its parent) so the copy works from
# any cwd. A missing source is a hard Fail, never a lane without its guard.
SKILL_SRC = os.path.join(os.path.dirname(HERE), "child-skills", "mk-builder")
GUARD_SRC = os.path.join(SKILL_SRC, "scripts", "git-guard")
# Top-level `.matrixkoder-*` files that are never stale. The brief is no longer copied into
# the lane (D7B: it arrives over the bus), so only the watch state stays. A brief left by an
# older acquire is archived with the other stale files.
LANE_KEEP_FILES = (".matrixkoder-watch.json", ".matrixkoder-watch.lock")
DEFAULTS = {"slots": 1, "pattern": "../{repo}-mk-{n}", "reuse": True, "cap": 3, "disposable": [],
            "copy": [], "integration_branch": "main"}


def _load(name, path):
    _spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mod)
    return mod


mk_state = _load("mk_state", os.path.join(HERE, "mk-state.py"))  # data dir, atomic write, timestamp
mkdb = _load("mkdb", os.path.join(HERE, "mkdb.py"))  # project store; opened only via open_project
mkpaths = _load("mkpaths", os.path.join(HERE, "mkpaths.py"))  # C1: home, data-dir order, paths

# lanes.json slot keys that have a lane_slots column. Anything else is data we must not drop.
SLOT_KEYS = ("path", "workspace", "workspace_id", "state", "run", "task", "branch", "lock_hash", "updated")
SLOT_COLS = ("slot", "lane_path", "state", "run", "task", "workspace", "workspace_id", "branch",
             "lock_hash", "updated")

# the yaml parser lives in mk-config.py so one file defines what a value means: a lane slot
# count and a settings row are read by the same scalar/split_flow/flow_map code
mk_config = _load("mk_config", os.path.join(HERE, "mk-config.py"))
scalar, split_flow, strip_comment, flow_map = (mk_config.scalar, mk_config.split_flow,
                                               mk_config.strip_comment, mk_config.flow_map)
KEY_RE = mk_config.KEY_RE


class Fail(Exception):
    def __init__(self, msg, code=2, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


# ---------- config: the project settings file, line-wise (no YAML dependency) ----------

def literal_names(items):
    """-> (accepted, rejected) lanes_disposable entries: literal names only, so the clean check's
    allowlist can never be widened into "every ignored file is disposable"."""
    ok, bad = [], []
    for x in items:
        name = scalar(x)
        if DISPOSABLE_RE.fullmatch(name) and ".." not in name:
            ok.append(name)
        elif name:
            bad.append(name)
    return ok, bad


def copy_paths(items):
    """-> (accepted, rejected) lanes.copy entries: literal relative paths only, so the copy source
    cannot leave the main checkout and the allowlist entry cannot become a glob."""
    ok, bad = [], []
    for x in items:
        p = x.strip() if isinstance(x, str) else ""
        if COPY_RE.fullmatch(p) and ".." not in p and p not in (".", "./"):
            ok.append(p)
        elif p:
            bad.append(p)
    return ok, bad


def settings_file(data):
    """The file load_config reads: `mkpaths.project_yaml(data)` — the C2 `<project>/project.yaml`
    for a migrated project, the legacy `<data>/matrixkoder.yaml` for an unmigrated one (Z24, the
    same path authority mk-config reads) — and that legacy file when the project file does not
    exist, so a migrated project whose project.yaml is gone falls back to its old config instead
    of silently reading DEFAULTS."""
    path = mkpaths.project_yaml(data)
    return path if os.path.exists(path) else mkpaths.legacy_yaml(data)


def load_config(data):
    """`  lanes: { slots, pattern, reuse, copy }` under repo, `max_build_lanes` (indent >= 2, any
    block) as the lane cap and one-line `lanes_disposable: [a, b]` on top of the clean-check
    allowlist, read from settings_file(data) — the project settings file, never a second copy of
    it. `cap` is exactly that `max_build_lanes`, so it equals mk-config's
    `routing_preferences.max_build_lanes` for the same project (Z24: `repo.lanes.slots` is the
    pool's ask, never the cap). DEFAULTS when absent or unreadable; `warnings` carries entries the
    lanes_disposable or copy checks rejected."""
    cfg = dict(DEFAULTS, disposable=[], warnings=[])
    try:
        with open(settings_file(data)) as f:
            lines = f.read().splitlines()
    except OSError:
        return cfg
    r = mk_config.row_for(lines, "repo.integration_branch")
    if r:
        _, cfg["integration_branch"] = mk_config.value_of(lines, r)
    for raw in lines:
        m = KEY_RE.match(raw)
        if not m:
            continue
        indent, key, rest = len(m.group(1)), m.group(2), m.group(3)
        if key == "lanes_disposable":  # a top-level one-line list, matched at any indent
            rest = strip_comment(rest).strip()
            if rest.startswith("[") and rest.endswith("]"):
                cfg["disposable"], bad = literal_names(split_flow(rest[1:-1]))
                cfg["warnings"] += [f"lanes_disposable: ignored {name!r} (not a literal name)" for name in bad]
            continue
        if indent < 2:  # the other two are indented block entries (repo.lanes, routing_preferences)
            continue
        if key == "max_build_lanes":
            with contextlib.suppress(ValueError):
                cfg["cap"] = int(scalar(rest))
        elif key == "lanes":
            block = flow_map(rest)
            if not block:  # a lane line we cannot read must never look like an absent one
                cfg["warnings"].append(f"lanes line unparsed, defaults kept: {raw.strip()[:120]}")
            with contextlib.suppress(ValueError, KeyError):
                cfg["slots"] = max(1, int(block["slots"]))
            if block.get("pattern"):
                cfg["pattern"] = block["pattern"]
            if block.get("reuse") in ("true", "false"):
                cfg["reuse"] = block["reuse"] == "true"
            if isinstance(block.get("copy"), list):
                cfg["copy"], bad = copy_paths(block["copy"])
                cfg["warnings"] += [f"lanes copy: ignored {p!r} (not a literal path)" for p in bad]
            elif "copy" in block:
                cfg["warnings"].append("lanes copy: expected a [list]; nothing copied")
    return cfg


# ---------- repo + data dir ----------

def git_toplevel():
    """The nearest .git above cwd, exactly like mk-state.resolve_data's walk (never $HOME itself)."""
    top = mkpaths.repo_top()
    if top is None:
        raise Fail("no git toplevel above cwd (the walk stops before $HOME); pass --data")
    return top


def main_checkout():
    """A linked worktree resolves to its main checkout (mk-state.main_worktree)."""
    return mkpaths.main_worktree(git_toplevel())


def slot_path(cfg, main, n):
    """A slot's absolute path: the pattern with {repo}/{n} filled in, against the main checkout.
    Without `{n}` every slot would land on the same directory, so that pattern is refused."""
    pattern = cfg["pattern"]
    if "{n}" not in pattern:
        raise Fail(f"lanes pattern {pattern!r} has no {{n}}: every slot would share one path")
    return os.path.normpath(os.path.join(
        main, pattern.replace("{repo}", os.path.basename(main)).replace("{n}", str(n))))


def workspace_name(main, n):
    return f"mk:{os.path.basename(main)}:{n}"


def linked_worktrees(main):
    """The realpaths git has registered as worktrees of this repo (the main checkout is one of them)."""
    r = git(["-C", main, "worktree", "list", "--porcelain"])
    return {os.path.realpath(l.split(" ", 1)[1]) for l in r.stdout.splitlines()
            if l.startswith("worktree ")}


def verify_slot_path(n, path, main):
    """A slot is a *linked* worktree of this repo and nothing else: never the main checkout, never a
    directory this repo does not know. Checked before every switch and every remove, so a re-aimed
    pattern or a foreign path can never be moved or deleted."""
    p, m = os.path.realpath(path), os.path.realpath(main)
    if p == m:
        raise Fail(f"slot {n} is the main checkout {path}, not a lane")
    if p not in linked_worktrees(main):
        raise Fail(f"slot {n} at {path} is not a registered worktree of {m}")


def inside_main(path, main):
    p, m = os.path.realpath(path), os.path.realpath(main)
    return p == m or p.startswith(m + os.sep)


# ---------- names ----------

def agent_name(task, role, rnd):
    """`<task>-<role>-r<round>` inside herdr's 32-char name limit: a task id starting with a digit
    gets a `t` prefix, `.` becomes `-`, and only the task part is shortened."""
    base = ("t" + task if task[0].isdigit() else task).replace(".", "-")
    suffix = f"-{role}-r{rnd}"
    room = 32 - len(suffix)
    if room < 1:
        raise Fail(f"role {role!r} leaves no room for the task part of an agent name")
    name = base[:room].rstrip("-") + suffix
    if not AGENT_RE.fullmatch(name):
        raise Fail(f"cannot build a herdr agent name from task {task!r} role {role!r}")
    return name


def slug(value, what):
    v = (value or "").lower()
    if not ID_RE.fullmatch(v):
        raise Fail(f"invalid {what} {value!r} (a-z0-9._- after lowercasing)")
    return v


def names(run, task, cfg=None, role="build", rnd=1, slot=1, main=None):
    """Every name a lane needs, computed once: the task branch, the integration branch, the slot
    path, the herdr workspace label and the agent name. Nothing here is invented anywhere else."""
    rid, tid = slug(run, "run id"), slug(task, "task id")
    if role not in ROLES:
        raise Fail(f"bad role {role!r}; one of: {' '.join(ROLES)}")
    if not isinstance(rnd, int) or rnd < 1:
        raise Fail(f"--round must be a positive integer, got {rnd!r}")
    if not isinstance(slot, int) or slot < 1:
        raise Fail(f"--slot must be a positive integer, got {slot!r}")
    top = main_checkout() if main is None else main
    return {"ok": True, "branch": f"mk/{rid}/{tid}", "integration": f"mk/{rid}/integration",
            "path": slot_path(cfg or DEFAULTS, top, slot), "workspace": workspace_name(top, slot),
            "agent": agent_name(tid, role, rnd)}


# ---------- lane_slots in the project DB (lanes.json only in file mode), one exclusive lock ----------

@contextlib.contextmanager
def locked(data, timeout):
    """Every read-modify-write of the registry holds this flock. Kept with the DB (L10.5): a BEGIN
    IMMEDIATE transaction covers only the final write, while acquire/release read the rows, run git
    and write later, so the transaction alone would not serialize two acquires. A holder that outlasts the timeout is
    an error, never a second writer racing the first."""
    with contextlib.suppress(OSError):
        os.makedirs(data, exist_ok=True)
    f = open(mkpaths.lanes_lock(data), "a")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:  # held by someone else: wait out the timeout
                if time.monotonic() >= deadline:
                    raise Fail("lanes.lock busy")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def read_lanes_file(data):
    """-> the registry from lanes.json. A missing or wrong-shaped file is an empty pool; corrupt
    JSON is an error (exit 2), because a pool we cannot read is not a pool we may quietly reset."""
    d = mk_state.read_json(mkpaths.lanes_file(data), None)
    if not isinstance(d, dict) or not isinstance(d.get("slots"), dict):
        return {"schema": SCHEMA, "slots": {}}
    d["schema"] = SCHEMA
    d["slots"] = {str(k): v for k, v in d["slots"].items() if isinstance(v, dict)}
    return d


def write_lanes_file(data, lanes):
    mk_state.write_json(mkpaths.lanes_file(data), {"schema": SCHEMA, "slots": lanes["slots"]})


def store_db(data):
    """The project store, or None when <data>/mk.db is absent. Absence is the self-hosting guard:
    no store code runs and no file is created. Opened only through mkdb.open_project."""
    if not os.path.isfile(mkpaths.project_db(data=data)):
        return None
    return mkdb.open_project(mkpaths.home(), data_dir=data)


def _cell(value):
    return None if value is None else str(value)


def row_to_rec(row):
    return {"path": row["lane_path"], "workspace": row["workspace"], "workspace_id": row["workspace_id"],
            "state": row["state"], "run": row["run"], "task": row["task"], "branch": row["branch"],
            "lock_hash": row["lock_hash"], "updated": row["updated"]}


def check_slot_keys(rec, slot):
    extra = sorted(set(rec) - set(SLOT_KEYS))
    if extra:
        raise Fail(f"slot {slot} has keys with no lane_slots column: {', '.join(extra)}")


def lanes_from_rows(rows):
    slots = {}
    for row in rows:
        rec = row_to_rec(row)
        check_slot_keys(rec, row["slot"])
        slots[str(row["slot"])] = rec
    return {"schema": SCHEMA, "slots": slots}


def read_rows(con):
    return con.execute("SELECT " + ", ".join(SLOT_COLS) + " FROM lane_slots ORDER BY slot").fetchall()


def write_rows(con, lanes):
    """Replace lane_slots with the in-memory pool. Caller holds the lanes lock and a transaction."""
    con.execute("DELETE FROM lane_slots")
    for key, rec in lanes["slots"].items():
        check_slot_keys(rec, key)
        con.execute(
            "INSERT INTO lane_slots(" + ", ".join(SLOT_COLS) + ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(key), rec.get("path"), _cell(rec.get("state")), _cell(rec.get("run")),
             _cell(rec.get("task")), _cell(rec.get("workspace")), _cell(rec.get("workspace_id")),
             _cell(rec.get("branch")), _cell(rec.get("lock_hash")), _cell(rec.get("updated"))))


def read_lanes(data):
    """The registry. With the project store the `lane_slots` rows are the only registry (L10.5):
    lanes.json is neither read nor written. A project with no mk.db is file mode: the file is its
    only store, and none is created."""
    con = store_db(data)
    if con is None:
        return read_lanes_file(data)
    try:
        return lanes_from_rows(read_rows(con))
    finally:
        con.close()


def write_lanes(data, lanes):
    """Commit the rows in one transaction (store), or write lanes.json (no mk.db: file mode)."""
    con = store_db(data)
    if con is None:
        write_lanes_file(data, lanes)
        return
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            write_rows(con, lanes)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def cmd_carry_legacy(a, cfg, data, main):
    """`carry-legacy [--dry-run]` (L10.5): the ONE-TIME carry of the pre-switch registry into the C2
    project DB. Sources: the legacy store's lane_slots (`project_db(legacy=True)`, opened read-only)
    and the legacy lanes.json. Only slots ABSENT from the target are inserted; a target row is newer
    and never overwritten (`skipped_newer`). Refused, nothing written, when any slot is busy in the
    sources or the target (quiesce). Idempotent; --dry-run writes nothing."""
    src_db, dst_db = mkpaths.project_db(data=data, legacy=True), mkpaths.project_db(data=data)
    if not dst_db or not os.path.isfile(dst_db):
        raise Fail("carry-legacy needs the project store (the project DB file is absent)")
    if os.path.realpath(src_db) == os.path.realpath(dst_db):
        return {"ok": True, "carried": [], "skipped_newer": [], "refused": [], "dry_run": bool(a.dry_run),
                "note": "legacy and project store are the same file: nothing to carry"}
    src = {}
    if os.path.isfile(src_db):
        ro = mkdb._connect_ro(src_db)
        try:
            if ro.execute("SELECT 1 FROM sqlite_master WHERE name='lane_slots'").fetchone():
                src = {r["slot"]: row_to_rec(r) for r in read_rows(ro)}
        finally:
            ro.close()
    for key, rec in read_lanes_file(data)["slots"].items():
        src.setdefault(str(key), rec)  # the store row wins over the file's, like the old import
    con = store_db(data)
    try:
        have = {r["slot"]: row_to_rec(r) for r in read_rows(con)}
        busy = sorted([f"source:{k}" for k, r in src.items() if r.get("state") == "busy"]
                      + [f"target:{k}" for k, r in have.items() if r.get("state") == "busy"])
        if busy:
            raise Fail("carry-legacy refused: busy slot(s) " + ", ".join(busy) + " (quiesce first)",
                       refused=busy, carried=[], skipped_newer=[])
        skipped = sorted(k for k in src if k in have)
        todo = sorted((k for k in src if k not in have), key=lambda k: (not k.lstrip("-").isdigit(), k))
        if todo and not a.dry_run:
            con.execute("BEGIN IMMEDIATE")
            try:
                for k in todo:
                    rec = src[k]
                    check_slot_keys(rec, k)
                    con.execute("INSERT INTO lane_slots(" + ", ".join(SLOT_COLS) + ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (str(k), rec.get("path"), _cell(rec.get("state")), _cell(rec.get("run")),
                                 _cell(rec.get("task")), _cell(rec.get("workspace")), _cell(rec.get("workspace_id")),
                                 _cell(rec.get("branch")), _cell(rec.get("lock_hash")), _cell(rec.get("updated"))))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
    finally:
        con.close()
    return {"ok": True, "carried": todo, "skipped_newer": skipped, "refused": [], "dry_run": bool(a.dry_run)}


def slot_numbers(lanes):
    return sorted(int(k) for k in lanes["slots"] if str(k).lstrip("-").isdigit())


def blank_slot(path, workspace):
    return {"path": path, "workspace": workspace, "workspace_id": None, "state": "free", "run": None,
            "task": None, "branch": None, "lock_hash": None, "updated": mk_state.now()}


def touch(rec, **kw):
    rec.update(kw)
    rec["updated"] = mk_state.now()


# ---------- git / herdr ----------

def run_cmd(cmd, timeout=GIT_TIMEOUT):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Fail(f"{cmd[0]} timed out after {timeout}s")
    except OSError as e:
        raise Fail(f"{cmd[0]}: {e}")


def git(args, timeout=GIT_TIMEOUT):
    r = run_cmd(["git", *args], timeout)
    if r.returncode != 0:
        raise Fail(f"git {' '.join(args[:4])} failed", stderr=(r.stderr or r.stdout).strip()[:400])
    return r


def herdr_bin():
    return os.environ.get("MK_HERDR") or "herdr"


def herdr_json_id(text):
    """-> (workspace_id, warning): the id herdr's create response actually carries —
    result.workspace.workspace_id, else result.worktree.open_workspace_id. The top-level `id` is
    the RPC name ("cli:worktree:create"), never a workspace id. A response without any id on any
    JSON line -> (None, warning): the slot exists, only the id capture failed."""
    for line in reversed([x for x in text.splitlines() if x.strip()]):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        result = d.get("result") if isinstance(d.get("result"), dict) else {}
        ws = d.get("workspace") if isinstance(d.get("workspace"), dict) else None
        rws = result.get("workspace") if isinstance(result.get("workspace"), dict) else None
        rwt = result.get("worktree") if isinstance(result.get("worktree"), dict) else None
        for cand in (rws.get("workspace_id") if rws else None,
                     rwt.get("open_workspace_id") if rwt else None,
                     result.get("workspace_id"), d.get("workspace_id"),
                     ws.get("id") if ws else None, rws.get("id") if rws else None):
            if cand:
                return str(cand), None
    return None, "herdr worktree create printed no workspace id " \
                 "(want result.workspace.workspace_id or result.worktree.open_workspace_id)"


STALE_WS_ID = "cli:worktree:create"  # herdr's RPC id: an old id capture stored it as a workspace id


def herdr_list_workspace_id(main, path):
    """-> (workspace_id | None, warning | None): the open_workspace_id herdr's `worktree list`
    carries for <path> (--cwd keeps the list to this repo). A path herdr does not know — a
    plain-git slot — is (None, None): the caller leaves the stored id as is. Only a herdr that
    fails or answers nothing usable is a warning, the same best-effort rule as the create side."""
    r = run_cmd([herdr_bin(), "worktree", "list", "--cwd", main], HERDR_TIMEOUT)
    if r.returncode != 0:
        why = (r.stderr or r.stdout).strip()[:150] or f"exit {r.returncode}"
        return None, f"herdr worktree list failed: {why}"
    for line in reversed([x for x in r.stdout.splitlines() if x.strip()]):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        result = d.get("result") if isinstance(d.get("result"), dict) else {}
        wts = result.get("worktrees")
        if not isinstance(wts, list):
            continue
        for w in wts:  # the id for THIS slot's path, never whatever entry happens to be first
            if not isinstance(w, dict) or not w.get("path"):
                continue
            if os.path.normpath(w["path"]) == os.path.normpath(path) \
                    or os.path.realpath(w["path"]) == os.path.realpath(path):
                wid = w.get("open_workspace_id") or w.get("workspace_id")
                return (str(wid), None) if wid else (None, None)
        return None, None  # a list without this path: unknown to herdr, not an error
    return None, "herdr worktree list printed no worktree list"


def herdr_pane_agent(path, workspace_id, new_name):
    """-> {"pane_agent": ..., "pane_agent_note": ...} | {}: the first live agent herdr lists in this
    slot (cwd is the slot path or inside it, or its workspace is the slot's) whose name is not the
    new round's — a leftover from an earlier round. Report only. Any herdr failure, timeout, bad
    JSON or exit != 0 is {}: acquire's reply stays exactly what it was without this check."""
    if herdr_bin() == "none":
        return {}
    try:
        r = run_cmd([herdr_bin(), "agent", "list"], 3)
        if r.returncode != 0:
            return {}
        real = os.path.realpath(path)
        for line in reversed([x for x in r.stdout.splitlines() if x.strip()]):
            try:
                d = json.loads(line)
                agents = d["result"]["agents"]
            except (ValueError, KeyError, TypeError):
                continue
            for e in agents:
                if not isinstance(e, dict) or e.get("name") == new_name:
                    continue
                cwd = os.path.realpath(e["cwd"]) if isinstance(e.get("cwd"), str) and e["cwd"] else None
                if (cwd is not None and (cwd == real or cwd.startswith(real + os.sep))) or \
                        (workspace_id not in (None, STALE_WS_ID) and e.get("workspace_id") == workspace_id):
                    return {"pane_agent": {"name": e.get("name"), "agent": e.get("agent"),
                                           "status": e.get("agent_status"), "pane_id": e.get("pane_id")},
                            "pane_agent_note": "an agent from an earlier round is running here; "
                                               "start the round in a fresh session"}
            return {}
    except (Fail, TypeError, AttributeError):
        pass
    return {}


def ensure_lane_exclude(lane):
    """`.matrixkoder-*`, `.omo/` and the installed skill dir into the slot's info/exclude (same
    pattern as mk-lane-event.py): lane reports, agent state and the skill install never read as
    untracked in a slot. Only missing patterns are appended, so calling it on every acquire stays
    idempotent — a reused slot created by an older version catches up on the new entries."""
    with contextlib.suppress(OSError):
        r = run_cmd(["git", "-C", lane, "rev-parse", "--git-path", "info/exclude"], 5)
        if r.returncode == 0 and r.stdout.strip():
            ep = r.stdout.strip()
            ep = ep if os.path.isabs(ep) else os.path.join(lane, ep)
            if os.path.exists(os.path.dirname(ep)):
                cur = open(ep).read() if os.path.exists(ep) else ""
                add = [e for e in (".matrixkoder-*", ".omo/", ".claude/skills/mk-builder/",
                                    ".omp/", ".omp/skills/mk-builder/")
                       if e not in cur]
                if add:
                    with open(ep, "a") as f:
                        f.write("".join(e + "\n" for e in add))


# ---------- the clean check (shared by acquire, release and gc) ----------

def dirs_named(path, name):
    """-> the `<…>/name` directory prefixes of a path, e.g. `lambda/node_modules`."""
    parts = path.strip().rstrip("/").split("/")
    return ["/".join(parts[:i + 1]) for i in range(len(parts) - 1) if parts[i] == name]


def omc_ignored_ok(path, has_tracked):
    """An ignored (`!!`) entry whose path has a `.omc` segment is not dirty. Builder OMC hooks
    write `.omc/project-memory.json`, `.omc/sessions/`, `.omc/state/` and nested `src/…/.omc/`
    that gitignore hides. Only while that `.omc` tree tracks nothing: a tracked `.omc/plans/`
    makes an ignored sibling inside it user data. A non-`!!` line never reaches here, so a
    modified tracked plan still counts. `.omc/state/` (HUD cache) is judged on its own subtree,
    so tracked plans beside it do not make it user data. This check never deletes."""
    parts = path.strip().rstrip("/").split("/")
    if ".omc" not in parts:
        return False
    i = parts.index(".omc")
    omc = "/".join(parts[:i + (2 if parts[i + 1:i + 2] == ["state"] else 1)])
    return has_tracked is None or not has_tracked(omc)


def disposable_ok(path, patterns, has_tracked=None):
    """Is an ignored entry disposable?

    Ignored `.omc` state is the extra case (omc_ignored_ok): a path segment, not an allowlist
    name, and still only when that tree is untracked.

    A trailing-`/` allowlist entry covers its whole subtree — at the root, or at any depth when it is
    one of ANY_DEPTH — while a name must match the path itself at the root (fnmatch, so
    `.matrixkoder-*` and `.env.*` are globs). A directory entry is only disposable while the repo
    tracks nothing inside it, at EVERY level: a vendored `node_modules/` or `.venv/` is source, and an
    ignored file inside it is user data, not a build cache. `has_tracked(dir)` answers that question;
    without it a directory match is taken at face value."""
    if omc_ignored_ok(path, has_tracked):
        return True
    raw = path.strip()
    p = raw.rstrip("/")
    for pat in patterns:
        if not pat.endswith("/"):
            if p == pat or fnmatch.fnmatchcase(p, pat) or fnmatch.fnmatchcase(p + "/*", pat):
                return True
            continue
        name = pat[:-1]
        if p == name or p.startswith(pat):  # the allowlisted dir itself, or something inside it
            if has_tracked is None or not has_tracked(name):
                return True
        elif pat in ANY_DEPTH and f"/{name}/" in f"/{p}/":
            inner = dirs_named(p, name)  # packages/a/node_modules/, scripts/__pycache__/, …
            if has_tracked is None or not any(has_tracked(d) for d in inner):
                return True
    return False


def clean_state(path, cfg):
    """-> (clean, dirty lines): `git status --porcelain=v1 -z --ignored` in the slot may list nothing
    but ignored entries on the allowlist, and no allowlisted directory may hold tracked files. Read-only:
    it never repairs, only reports.

    `-z` is what makes the check trustworthy: porcelain v1 C-quotes a path with a space, a non-ASCII
    byte or a leading `:`, and a quoted path matches nothing and reads as untracked. The flags are on
    the command line on purpose — `status.showUntrackedFiles=no` in the user's config would otherwise
    hide `??` lines, and this check is the only thing standing between a hidden untracked file and
    `git worktree remove`.

    `--no-optional-locks`: this is a read-only poll that runs on every status and acquire, and
    without the flag git may take the index lock for it. A poll must never block a real
    `git add` in the same worktree.

    `--ignored=matching` (not the default `traditional`): traditional COLLAPSES a directory whose
    every entry is ignored into one `!! .claude-plugin/` record, so the allowlist entry that names
    the real leftover (`.claude-plugin/types/`) never matched and the slot read dirty forever —
    the exact refusal the Z21 brief saw on slots 1 and 4. matching reports the entry the ignore
    rule actually names, which is what the allowlist is spelled in."""
    r = run_cmd(["git", "--no-optional-locks", "-C", path, "status", "--porcelain=v1", "-z",
                 "--ignored=matching", "--untracked-files=normal", "--ignore-submodules=none"], 60)
    if r.returncode != 0:
        raise Fail(f"git status in {path} failed", stderr=(r.stderr or r.stdout).strip()[:400])
    allow = tuple(DISPOSABLE) + tuple(cfg["disposable"]) + tuple(cfg["copy"])
    cache = {}

    def has_tracked(rel):
        """-> does this repo track files under <rel>? --literal-pathspecs so a dir named `:(z)` or
        `:x` is a path and not pathspec magic; a non-zero exit counts as tracked (fail closed)."""
        if rel not in cache:
            t = run_cmd(["git", "-C", path, "--literal-pathspecs", "ls-files", "-z", "--", rel], 30)
            cache[rel] = t.returncode != 0 or bool(t.stdout.strip())
        return cache[rel]

    dirty = []
    for rec in r.stdout.split("\0"):  # NUL-separated records: paths arrive exactly as on disk
        if not rec:
            continue
        if not (rec.startswith("!! ") and disposable_ok(rec[3:], allow, has_tracked)):
            dirty.append(rec)
    return not dirty, dirty


def scratch_names(path):
    """-> the SCRATCH entries that EXIST in this slot and that git reports as ignored. Literal
    root-level names only, and `git check-ignore` (not a parsed `git status`) is what decides:
    status COLLAPSES a directory whose every entry is ignored to `!! .claude-plugin/`, so the
    entry the brief saw is only visible per name. check-ignore consults the index the same way
    status does — a TRACKED `.ruff_cache/` or `.claude-plugin/types/` is not reported ignored and
    is therefore never removed."""
    out = []
    for name in SCRATCH:
        if not os.path.lexists(os.path.join(path, name)):
            continue
        # no --literal-pathspecs here: check-ignore refuses it ("pathspec magic not supported").
        # The name comes from the SCRATCH tuple, never from user input, so there is nothing to
        # escape; a `:` in a SCRATCH entry would be a code change, not data.
        r = run_cmd(["git", "-C", path, "check-ignore", "-q", "--", name], 30)
        if r.returncode == 0:
            out.append(name)
    return out


def clear_scratch(path):
    """Remove the slot's ignored scratch — the SDK types `claude plugin test` writes into
    `.claude-plugin/types/` and the linter's `.ruff_cache` — and return the removed names. Called
    only by release, and only AFTER the clean check passed, so a slot that refuses loses nothing.
    One path per name: a file or a link goes by the link, a directory goes whole, and anything
    resolving outside the slot is left alone."""
    slot_real = os.path.realpath(path)
    gone = []
    for name in scratch_names(path):
        target = os.path.join(path, name)
        if os.path.commonpath([os.path.realpath(target), slot_real]) != slot_real:
            continue        # a name that resolves outside the slot is not ours to remove
        if os.path.islink(target) or os.path.isfile(target):
            os.unlink(target)
        elif os.path.isdir(target):
            shutil.rmtree(target)
        else:
            continue
        gone.append(name)
    return gone


def stray_commits(path):
    """The commits HEAD has that no branch, tag or remote ref reaches. A free slot sits detached, so a
    late builder commit there is orphaned by the next switch — it survives only in the reflog."""
    r = run_cmd(["git", "-C", path, "rev-list", "HEAD", "--not", "--branches", "--tags", "--remotes"], 60)
    if r.returncode != 0:
        raise Fail(f"git rev-list in {path} failed", stderr=(r.stderr or r.stdout).strip()[:400])
    return r.stdout.split()


def require_clean(n, rec, cfg):
    """Refuse before any git command runs and before any state is written (exit 3)."""
    ok, dirty = clean_state(rec.get("path") or "", cfg)
    if not ok:
        raise Fail(f"slot {n} dirty", code=3, dirty=dirty[:20])


def require_no_stray(n, path):
    """No switch may orphan a commit: check it right before every switch (exit 3)."""
    shas = stray_commits(path)
    if shas:
        raise Fail(f"slot {n} has {len(shas)} commit(s) on no branch", code=3, unreachable=shas[:5])


# ---------- ref arguments: untrusted input reaches git as argv ----------

def check_ref(main, ref, what):
    """-> the commit sha a ref argument resolves to. Rejects a leading `-` (option injection) and
    anything that is not a commit here, then the sha is what git commands get."""
    if not ref or ref.startswith("-"):
        raise Fail(f"{what} {ref!r} must be a ref, not an option")
    r = run_cmd(["git", "-C", main, "rev-parse", "--verify", "--quiet", "--end-of-options",
                 f"{ref}^{{commit}}"])
    if r.returncode != 0 or not r.stdout.strip():
        raise Fail(f"{what} {ref!r} does not resolve to a commit in {os.path.basename(main)}")
    return r.stdout.strip()


def try_ref(main, ref):
    """-> the commit sha, or None when the ref is gone (an already-deleted branch is not an error)."""
    r = run_cmd(["git", "-C", main, "rev-parse", "--verify", "--quiet", "--end-of-options",
                 f"{ref}^{{commit}}"])
    return r.stdout.strip() if r.returncode == 0 else None


def branch_ref(main, ref, what="--integration"):
    """-> (name, sha) for a ref that must be an existing local branch. `--integration` names where
    the work is merged, so a raw sha, a tag or the task branch itself is refused: as its own
    "integration" the is-ancestor test below would pass with nothing else holding the commits."""
    if not ref or ref.startswith("-"):
        raise Fail(f"{what} {ref!r} must be a ref, not an option")
    r = run_cmd(["git", "-C", main, "rev-parse", "--verify", "--quiet", "--symbolic-full-name",
                 "--end-of-options", ref])
    full = r.stdout.strip()
    if r.returncode != 0 or not full.startswith("refs/heads/"):
        raise Fail(f"{what} {ref!r} must be an existing local branch (refs/heads/…), not a sha or tag")
    # A case-insensitive filesystem (macOS, core.ignorecase) resolves a loose ref by any case and
    # rev-parse echoes the alias back, so `MK/R1/T2` would pass as the task branch `mk/r1/t2`. Only
    # the real ref list knows the stored spelling.
    real = run_cmd(["git", "-C", main, "for-each-ref", "--format=%(refname)", "--", full])
    if real.returncode != 0 or real.stdout.strip() != full:
        raise Fail(f"{what} {ref!r} is not the stored spelling of a local branch "
                   f"(no ref named {full!r})")
    return full[len("refs/heads/"):], check_ref(main, full, what)


def branch_holder(main, name, own_path=""):
    """-> the worktree path that has refs/heads/<name> checked out, other than `own_path` (our own
    slot, which we have already detached). update-ref -d does not know about worktrees: without this
    the delete would leave that checkout on an unborn branch."""
    path = None
    for line in git(["-C", main, "worktree", "list", "--porcelain"]).stdout.splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1]
        elif line.strip() == f"branch refs/heads/{name}" and os.path.realpath(path or "") != os.path.realpath(own_path or ""):
            return path
    return None


def check_branch(main, name, what="branch name"):
    if not name or name.startswith("-"):
        raise Fail(f"{what} {name!r} must be a ref, not an option")
    r = run_cmd(["git", "-C", main, "check-ref-format", "--branch", name])
    if r.returncode != 0:
        raise Fail(f"{what} {name!r} is not a valid git branch name")
    return name


# ---------- lockfile hash: does the slot still need an install? ----------

def lock_hash(path):
    """sha256 of the first lockfile present in the slot; None when the repo has none."""
    for name in LOCKFILES:
        p = os.path.join(path, name)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
    return None


def store_snapshot(data, run=None):
    """-> mkdb.run_snapshot's answer for the project store, or None when the store cannot
    answer: no mk.db (a pre-migration project), an unreadable/newer one, or — with `run` named —
    a run the store does not know. Read-only (mode=ro) like every other mkdb reader: it never
    creates, migrates or writes mk.db, and the file's existence is the gate, exactly as store_db.
    The gate is mkpaths.project_folder's — the same store run_snapshot opens — so a migrated
    project is gated on its C2 store, never on the pre-switch copy left in the data dir."""
    try:
        if not data or not os.path.isfile(mkpaths.db_in(mkpaths.project_folder(data, home_arg=mkpaths.home()))):
            return None
        return mkdb.run_snapshot(mkpaths.home(), None, run, data_dir=data)
    except Exception:  # a store we cannot read must not fail an acquire: the file view answers
        return None


def run_ids(data):
    """-> the run ids mk-state knows, the same list `run list` prints. The project store's runs
    table is the source; <data>/runs/index.json is only the pre-L4 fallback a project with no
    store (or a store that has not imported its runs yet) still needs."""
    snap = store_snapshot(data)
    if snap is not None and snap["runs"]:
        return [r["id"] for r in snap["runs"]]
    idx = mk_state.read_json(mkpaths.runs_index(data), [])
    if not isinstance(idx, list):
        return []
    return [r.get("id") for r in idx if isinstance(r, dict) and r.get("id")]


def resolve_run(data, run):
    """-> the stored run id for `run`, matched case-insensitively (ids are case-insensitive, run
    dirs are not on every filesystem). None when neither the store nor the pre-L4 index knows such
    a run — cmd_acquire refuses on it, and the mk-state write below must use the stored spelling."""
    for rid in run_ids(data):
        if str(rid).lower() == run.lower():
            return rid
    return None


def resolve_task(data, rid, task):
    """-> the task id as stored in run `rid` (mk-state matches task ids by case, so the write must
    use the stored spelling). The store's task list for that run is the source; the pre-L4
    <data>/runs/<rid>/run.json is the fallback a project with no store still needs. Falls back to
    `task` as typed when neither can be read: the mk-state write stays best-effort, exactly as
    before."""
    snap = store_snapshot(data, rid)
    if snap is not None:  # the store knows this run: its task list is the answer, no file read
        for t in (snap.get("run") or {}).get("tasks") or []:
            if isinstance(t, dict) and str(t.get("id") or "").lower() == task.lower():
                return t["id"]
        return task
    try:
        run = mk_state.read_json(mkpaths.run_doc(data, rid), None)
    except (OSError, ValueError):
        return task
    for t in (run.get("tasks") if isinstance(run, dict) else None) or []:
        if isinstance(t, dict) and str(t.get("id") or "").lower() == task.lower():
            return t["id"]
    return task


def set_task(data, run, task, rec, n):
    """`mk-state task set` for lane/branch/workspace/slot. Best-effort: lanes.json is the truth about
    slots, so a failure here is a warning on an otherwise successful acquire, never a failed one."""
    r = run_cmd([sys.executable, os.path.join(HERE, "mk-state.py"), "--data", data, "--run", run,
                  "task", "set", task, f"lane={rec['path']}", f"branch={rec['branch']}",
                  f"workspace={rec['workspace']}", f"slot={n}"], 60)
    return None if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200] or f"exit {r.returncode}"


# ---------- commands ----------

def cmd_names(a, cfg, data, main):
    return with_warnings(names(a.run, a.task, cfg, a.role, a.round, a.slot, main), cfg)


def pick_slot(lanes, cfg):
    """-> (n, created): reuse the lowest free slot whose path exists; while the pool is below the cap,
    create the lowest free slot whose path is gone, or the lowest unused number."""
    slots = lanes["slots"]
    free = [n for n in slot_numbers(lanes) if slots[str(n)].get("state") != "busy"]
    for n in free:
        if os.path.isdir(slots[str(n)].get("path") or ""):
            return n, False
    if len(slots) < cfg["cap"]:
        gone = [n for n in free if not os.path.isdir(slots[str(n)].get("path") or "")]
        if gone:
            return gone[0], True
        n = 1
        while str(n) in slots:
            n += 1
        return n, True
    raise Fail(f"all {cfg['cap']} slots busy", code=4)


def create_slot(rec, main, base_sha):
    """Create the slot's worktree: herdr (labelled workspace) or plain git, both from the sha
    check_ref resolved — the raw `--base` never reaches herdr either. The record is written by the
    caller after this returns, so a failure leaves no slot behind. -> None, or a warning when herdr
    answered without a usable workspace id (the slot itself was created)."""
    bin_ = herdr_bin()
    warn = None
    if bin_ == "none":
        rec["workspace_id"] = None  # a recreated slot has no herdr workspace, let alone a stale id
        git(["-C", main, "worktree", "add", "-b", rec["branch"], rec["path"], base_sha])
    else:
        r = run_cmd([bin_, "worktree", "create", "--cwd", main, "--branch", rec["branch"],
                     "--base", base_sha, "--path", rec["path"], "--label", rec["workspace"],
                     "--no-focus"], HERDR_TIMEOUT)
        if r.returncode != 0:
            raise Fail("herdr worktree create failed", stderr=(r.stderr or r.stdout).strip()[:400])
        rec["workspace_id"], warn = herdr_json_id(r.stdout)
    ensure_lane_exclude(rec["path"])
    return warn


def tracked_in(main, rel):
    """-> does the main checkout track <rel> (the path itself, or anything under it)? Fail closed:
    any git error counts as tracked, so a broken repo never smuggles files into a slot (the same
    rule as clean_state's has_tracked)."""
    t = run_cmd(["git", "-C", main, "--literal-pathspecs", "ls-files", "--error-unmatch", "-z", "--", rel], 30)
    if t.returncode == 0:
        return True
    if t.returncode == 1:  # --error-unmatch: nothing tracked under <rel>
        return False
    return True  # any other git error: fail closed


def copy_into_slot(cfg, main, lane):
    """The lanes.copy entries from the main checkout into the slot — on every acquire, not only a
    fresh one: the worktree has the tracked files, not the gitignored ones a build needs (.env,
    generated types). A target that already exists in the slot is NEVER overwritten: a reused slot
    keeps its own copy, only a missing target is filled. And only UNTRACKED content is copied — a
    tracked source would put repo files in the slot's status — and only literal in-checkout paths:
    absolute paths, `..` components and symlinks resolving outside the main checkout are skipped.
    -> warnings; a missing, tracked or unreadable source is reported, never fatal."""
    warns = []
    for rel in cfg["copy"]:
        src, dst = os.path.join(main, rel), os.path.join(lane, rel)
        try:
            if os.path.isabs(rel) or ".." in rel.split("/"):
                raise ValueError("not a literal relative path")
            if os.path.commonpath([os.path.realpath(src), main]) != main:
                raise ValueError("resolves outside the main checkout")
            if tracked_in(main, rel):
                warns.append(f"lanes copy: {rel!r} is tracked in {os.path.basename(main)}, skipped")
                continue
            if os.path.lexists(dst):
                continue  # never overwrite: a target already in the slot stays exactly as it is
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            elif os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            else:
                warns.append(f"lanes copy: {rel!r} not in {os.path.basename(main)}, skipped")
        except (OSError, shutil.Error, ValueError) as e:
            warns.append(f"lanes copy: {rel!r}: {type(e).__name__}: {e}"[:200])
    return warns


def archive_stale_lane_files(lane, run):
    """Every top-level `.matrixkoder-*` FILE except the watch state moves into
    <lane>/.matrixkoder-old-<run>/ before the install writes its own: a report from an earlier
    round would otherwise sit next to the fresh ones and read as this round's. Moved (shutil.move),
    never deleted; directories (.matrixkoder-bin/, an older archive) stay put; a name that already
    exists in the archive keeps both under a numeric suffix. Deterministic: names are visited
    sorted, and with nothing stale no archive dir appears at all. -> the archive dir, or None."""
    if not os.path.isdir(lane):
        return None  # a herdr that answered ok but created no worktree: nothing to archive
    old = os.path.join(lane, f".matrixkoder-old-{run}")
    for name in sorted(os.listdir(lane)):
        p = os.path.join(lane, name)
        if not name.startswith(".matrixkoder-") or name in LANE_KEEP_FILES or not os.path.isfile(p):
            continue
        os.makedirs(old, exist_ok=True)
        dst, i = os.path.join(old, name), 1
        while os.path.exists(dst):
            i += 1
            dst = os.path.join(old, f"{name}.{i}")
        shutil.move(p, dst)
    return old if os.path.isdir(old) else None


def user_omp_skill():
    """The user-level omp skill DIRECTORY acquire compares against (the real
    ~/.omp/agent/skills/mk-builder), or None when there is none. Read-only: acquire never writes
    here. $MK_OMP_USER_SKILLS redirects the comparison at a stand-in (the self-test's temp dir), so
    a test never reads or touches the real ~/.omp; it may name the skills dir or the skill itself.
    """
    d = os.environ.get("MK_OMP_USER_SKILLS")
    if d:
        # a skills dir is the parent; the skill dir is named `mk-builder` either way
        return os.path.join(d, "mk-builder") if os.path.basename(d) != "mk-builder" else d
    return os.path.join(os.path.expanduser("~"), ".omp", "agent", "skills", "mk-builder")


def omp_user_skill_mismatch():
    """A warning string when the user-level ~/.omp copy of mk-builder differs from the plugin's,
    else None. The project `.omp/skills` copy is what an omp lane loads, so the user copy is only
    ever reported, never overwritten — an acquire never writes outside its own slot."""
    got = os.path.join(user_omp_skill(), "SKILL.md")
    want = os.path.join(SKILL_SRC, "SKILL.md")
    if not os.path.isfile(got) or not os.path.isfile(want):
        return None
    try:
        if open(got, "rb").read() == open(want, "rb").read():
            return None
    except OSError:
        return None
    return (f"user-level omp skill {got} differs from the plugin copy {want}: version "
            f"mismatch, left untouched (an omp lane reads the slot's .omp/skills copy instead)")


def install_lane_skill(lane):
    """The mk-builder skill copy and its git guard into the slot, on EVERY acquire: overwrite, not
    merge — an existing install is always this tool's own untracked, excluded, disposable dir, and
    a wholesale replace is the only way the slot's version always matches the plugin. A missing
    source is a hard Fail (a lane silently missing its guard is worse than a refused acquire) and
    leaves no half-installed skill dir behind.
    TWO installs, because a lane's agent decides where it reads skills: `.claude/skills/mk-builder`
    for a Claude/OpenCode lane and `.omp/skills/mk-builder` for an omp lane (omp's native project
    skill dir, its loader joining `<cwd>/.omp` with `skills`). Both are overwritten, both are
    git-excluded, and both hold the same bytes.
    -> (claude skill dir, omp skill dir, guard path), all absolute."""
    skill = os.path.join(lane, ".claude", "skills", "mk-builder")
    omp_skill = os.path.join(lane, ".omp", "skills", "mk-builder")
    guard = os.path.join(lane, ".matrixkoder-bin", "git")
    if not os.path.isdir(lane):
        # a herdr that answered ok but created no worktree: report the intended destinations,
        # install nothing (creating the path implicitly would poison the next pick_slot), and
        # let the acquire through exactly as before — the next real acquire installs for real.
        return skill, omp_skill, guard
    if not os.path.isdir(SKILL_SRC):
        raise Fail(f"mk-builder skill source {SKILL_SRC} not found in the plugin")
    if not os.path.isfile(GUARD_SRC):
        raise Fail(f"git-guard source {GUARD_SRC} not found in the plugin")
    slot_real = os.path.realpath(lane)

    def inside_slot(p_):
        try:
            return os.path.commonpath([os.path.realpath(p_), slot_real]) == slot_real
        except ValueError:  # realpath outside this mount: never ours
            return False

    def fresh(dst):
        if os.path.islink(dst) or os.path.isfile(dst):
            os.unlink(dst)  # a link or a stray file goes by the link only; never followed
        elif os.path.isdir(dst):
            if not inside_slot(dst):
                raise Fail(f"mk-builder skill path {dst} resolves outside the slot, refused")
            shutil.rmtree(dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        return dst

    done = []
    try:
        for dst in (skill, omp_skill):
            shutil.copytree(SKILL_SRC, fresh(dst))
            done.append(dst)
        os.makedirs(os.path.join(lane, ".matrixkoder-bin"), exist_ok=True)
        guard = os.path.join(lane, ".matrixkoder-bin", "git")
        shutil.copy2(GUARD_SRC, guard)
        os.chmod(guard, 0o755)  # copy2 keeps the source mode; chmod guarantees the executable bit
    except (OSError, shutil.Error) as e:
        for dst in done:  # no partial install left behind, either copy
            if not os.path.islink(dst) and inside_slot(dst):
                shutil.rmtree(dst, ignore_errors=True)
        raise Fail(f"mk-builder skill install into {lane} failed: {type(e).__name__}: {e}"[:400])
    return skill, omp_skill, guard


def remove_slot(n, rec, main):
    """Drop the slot's worktree: herdr (which also removes the ignored files) or plain git. Never
    forced, and never with commits only this slot's HEAD knows about: `git worktree remove` deletes the
    worktree's logs/HEAD, so a stray commit would go from reflog-only to dangling. That check raises
    (exit 3); gc turns it into a skip, release lets it through. -> None on success, else the reason."""
    require_no_stray(n, rec["path"])
    bin_ = herdr_bin()
    if bin_ != "none" and rec.get("workspace_id"):
        r = run_cmd([bin_, "worktree", "remove", "--workspace", rec["workspace_id"]], HERDR_TIMEOUT)
        return None if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200] or f"exit {r.returncode}"
    git(["-C", main, "worktree", "remove", rec["path"]])
    return None


def acquire_tasks(a):
    """-> the task id list behind --task / --tasks (a group is one slot; ids keep their case,
    only branch and agent names are lowercased)."""
    if a.task and a.tasks:
        raise Fail("--task and --tasks are mutually exclusive")
    tasks = [a.task] if a.task else (a.tasks.split(",") if a.tasks else [])
    if not tasks or not all(tasks):
        raise Fail("acquire needs --task T or --tasks A,B (no empty entries)")
    for t in tasks:
        slug(t, "task id")
    return tasks


def cmd_acquire(a, cfg, data, main):
    tasks = acquire_tasks(a)
    nms = names(a.run, tasks[0], cfg, a.role, a.round, 1, main)  # validates run/task before the lock
    rid = resolve_run(data, a.run)  # ids are case-insensitive; the write needs the stored one
    if rid is None:  # refuse before anything is created: lanes.json, worktree, branch all untouched
        known = run_ids(data)
        raise Fail(f"unknown run {a.run!r} (mk-state knows: {', '.join(map(str, known)) or 'no runs'})",
                   known_runs=known)
    base_ref = a.base if a.base is not None else cfg.get("integration_branch", "main")
    base = check_ref(main, base_ref, "--base")
    check_branch(main, nms["branch"], "task branch name")
    with locked(data, a.lock_timeout):
        lanes = read_lanes(data)
        n, created = pick_slot(lanes, cfg)
        want = slot_path(cfg, main, n)
        if inside_main(want, main):
            raise Fail(f"slot path {want} is the main checkout or inside it")
        rec = lanes["slots"].get(str(n))
        if rec is not None and os.path.normpath(rec.get("path") or "") != want:
            raise Fail(f"slot {n} is at {rec.get('path')}, pattern now gives {want}")
        if rec is None:
            rec = lanes["slots"][str(n)] = blank_slot(want, workspace_name(main, n))
        rec.setdefault("workspace_id", None)
        rec["workspace"] = workspace_name(main, n)  # rec["path"] is never reassigned: a slot stays put
        rec["branch"] = nms["branch"]
        warns = []
        create_warn = None
        if created:
            if os.path.lexists(rec["path"]):
                raise Fail(f"slot path {rec['path']} exists and is not a registered slot")
            create_warn = create_slot(rec, main, base)
        else:
            verify_slot_path(n, rec["path"], main)
            require_clean(n, rec, cfg)  # before any git command runs
            require_no_stray(n, rec["path"])
            git(["-C", rec["path"], "switch", "--detach", base])
            git(["-C", rec["path"], "switch", "-c", rec["branch"]])  # an existing branch fails here
        # the lane's own tooling refreshes on EVERY acquire, fresh slot or reused: the exclude
        # entries first (a reused slot created by an older version never got the new ones), then
        # the stale-report archive — before the install, so old reports never sit next to the new
        # — then the skill copy + guard, overwritten so the version always matches the plugin.
        ensure_lane_exclude(rec["path"])
        archive_stale_lane_files(rec["path"], rid)
        skill_dst, omp_dst, guard_dst = install_lane_skill(rec["path"])
        if (mm := omp_user_skill_mismatch()):  # user-level copy drifts: reported, never overwritten
            warns.append(mm)
        # lanes.copy runs on EVERY acquire: a fresh slot gets everything, a reused slot only the
        # targets it is missing — an existing target is never overwritten (copy_into_slot).
        warns += copy_into_slot(cfg, main, rec["path"])
        # a stale registry id (null, or herdr's RPC name an old capture stored) is repaired from
        # herdr's own list before the record is written; a path herdr does not know stays as is
        if rec.get("workspace_id") in (None, STALE_WS_ID) and herdr_bin() != "none":
            wid, warn = herdr_list_workspace_id(main, rec["path"])
            if wid:
                rec["workspace_id"] = wid
                create_warn = None
            elif warn:
                warns.append(warn)
        if create_warn:
            warns.append(create_warn)
        h = lock_hash(rec["path"])
        install_needed = created or h != rec.get("lock_hash") or rec.get("lock_hash") is None
        touch(rec, state="busy", run=a.run, task=a.task or a.tasks, lock_hash=h)
        write_lanes(data, lanes)
        out = {"ok": True, "slot": n, "path": rec["path"], "workspace": rec["workspace"],
               "workspace_id": rec["workspace_id"], "branch": rec["branch"],
               "agent": nms["agent"], "created": created, "install_needed": install_needed,
               "skill_installed": skill_dst, "omp_skill_installed": omp_dst,
               "guard_path": guard_dst}
        out.update(herdr_pane_agent(rec["path"], rec["workspace_id"], nms["agent"]))
        if len(tasks) > 1:
            out["tasks"] = tasks
        for t in tasks:
            if warning := set_task(data, rid, resolve_task(data, rid, t), rec, n):
                warns.append((f"task {t}: " if len(tasks) > 1 else "") + f"mk-state task set: {warning}")
        if warns:
            out["warning"] = "; ".join(warns)
        return with_warnings(out, cfg)


def cmd_release(a, cfg, data, main):
    iname, integration = branch_ref(main, a.integration, "--integration")
    with locked(data, a.lock_timeout):
        lanes = read_lanes(data)
        rec = lanes["slots"].get(str(a.slot))
        if rec is None:
            raise Fail(f"no slot {a.slot}")
        task_branch = rec.get("branch")
        task_tip = try_ref(main, f"refs/heads/{task_branch}") if task_branch else None
        # the branch the work is merged into must be a real branch and must not be this slot's own
        # task branch: as --integration it would be its own ancestor, so the delete below would leave
        # the slot's HEAD as the only ref on those commits. A raw task sha never gets here at all —
        # branch_ref already refused anything that is not refs/heads/<name>.
        if task_branch and iname == task_branch:
            raise Fail(f"--integration {a.integration!r} is the task branch of slot {a.slot}; "
                       "pass the integration branch")
        verify_slot_path(a.slot, rec["path"], main)
        require_clean(a.slot, rec, cfg)  # BEFORE any cleanup: a slot that refuses keeps its bytes
        require_no_stray(a.slot, rec["path"])
        git(["-C", rec["path"], "switch", "--detach", integration])
        branch, warning, deleted = task_branch, None, None
        if branch:
            # merged-ness is judged against the integration branch, not the main checkout's HEAD, and
            # update-ref -d <ref> <sha> is a compare-and-swap that refuses if the branch moved
            sha = task_tip
            holder = branch_holder(main, branch, rec["path"])
            if sha is None:
                deleted, warning = False, f"branch {branch} already gone"
            elif holder:
                deleted = False
                warning = f"branch {branch} checked out at {holder}, kept"
            elif run_cmd(["git", "-C", main, "merge-base", "--is-ancestor", sha,
                          integration]).returncode == 0:
                r = run_cmd(["git", "-C", main, "update-ref", "-d", f"refs/heads/{branch}", sha])
                deleted = r.returncode == 0
                if not deleted:
                    warning = f"branch {branch} moved while releasing, kept"
            else:
                deleted = False
                warning = f"branch {branch} not merged into {iname}, kept"  # never -D
        # the documented lane cleanup (lane-rules §6): the ignored scratch a slot ACCUMULATES —
        # `.ruff_cache`, the SDK types `claude plugin test` writes into `.claude-plugin/types/` —
        # goes before the slot is parked. Only after the clean check passed, so a dirty slot the
        # user must look at loses nothing.
        scratch = clear_scratch(rec["path"])
        touch(rec, state="free", run=None, task=None, branch=None)
        out = {"ok": True, "slot": a.slot, "path": rec["path"], "state": "free",
               "integration": a.integration, "branch": branch, "branch_deleted": deleted,
               "scratch_removed": scratch}
        if not cfg["reuse"]:  # one-shot slots: the worktree goes with the release
            try:
                why = remove_slot(a.slot, rec, main)
            except Fail as e:
                if "unreachable" in e.extra:
                    raise  # commits only this slot knows about: exit 3, not a "kept the slot" warning
                why = e.extra.get("stderr") or str(e)
            if why:
                warning = f"{warning}; slot kept: {why}" if warning else f"slot kept: {why}"
            else:
                lanes["slots"].pop(str(a.slot), None)
        write_lanes(data, lanes)
        return with_warnings({**out, **({"warning": warning} if warning else {})}, cfg)


def with_warnings(out, cfg):
    """Config problems (a rejected lanes_disposable entry, a `lanes:` line we could not parse) travel
    with every ok answer: the caller must see that its config is not what it asked for."""
    return {**out, "warnings": list(cfg["warnings"])} if cfg["warnings"] else out


def cmd_gc(a, cfg, data, main):
    keep = max(1, a.keep)
    with locked(data, a.lock_timeout):
        lanes = read_lanes(data)
        survivors = set(slot_numbers(lanes)[:keep])  # the lowest K that exist, not the numbers <= K
        removed, skipped = [], []
        for n in slot_numbers(lanes):
            if n in survivors:
                continue
            rec = lanes["slots"][str(n)]
            if rec.get("state") == "busy":
                skipped.append({"slot": n, "reason": "busy"})
                continue
            if not os.path.isdir(rec.get("path") or ""):
                skipped.append({"slot": n, "reason": "path gone"})
                continue
            try:
                verify_slot_path(n, rec["path"], main)
            except Fail as e:
                skipped.append({"slot": n, "reason": str(e)})
                continue
            ok, dirty = clean_state(rec["path"], cfg)
            if not ok:
                skipped.append({"slot": n, "reason": f"dirty: {dirty[0]}"})
                continue
            try:
                why = remove_slot(n, rec, main)
            except Fail as e:
                if "unreachable" in e.extra:  # a refusal, not a failed removal: name the commits
                    skipped.append({"slot": n, "reason": str(e), "unreachable": e.extra["unreachable"]})
                    continue
                why = e.extra.get("stderr") or str(e)
            if why:
                skipped.append({"slot": n, "reason": f"remove failed: {why}"})
                continue
            lanes["slots"].pop(str(n))
            removed.append(n)
        write_lanes(data, lanes)
        return with_warnings({"ok": True, "removed": removed, "skipped": skipped}, cfg)


def cmd_list(a, cfg, data, main):
    lanes = read_lanes(data)  # read-only: no lock, no state change (a held lock must stay readable)
    out = []
    for n in slot_numbers(lanes):
        rec = lanes["slots"][str(n)]
        live = None  # a path that is gone is neither clean nor dirty
        if os.path.isdir(rec.get("path") or ""):
            live = clean_state(rec["path"], cfg)[0]
        out.append({"slot": n, "path": rec.get("path"), "workspace": rec.get("workspace"),
                    "workspace_id": rec.get("workspace_id"), "state": rec.get("state"),
                    "run": rec.get("run"), "task": rec.get("task"), "branch": rec.get("branch"),
                    "clean": live, "updated": rec.get("updated")})
    return with_warnings({"ok": True, "slots": out, "cap": cfg["cap"]}, cfg)


# ---------- status: lane facts pulled without asking the agent (read-only) ----------

TUI_CHROME = set("┃╹╻▀▄█▌▐░▒▓⬝■□▪ │─┌┐└┘├┤┬┴┼═║╔╗╚╝\t")
CTX_RE = re.compile(r"\d[\d.]*[KMkm]?\s*\((\d{1,3})%\)")
HERDR_STATUS_TIMEOUT = 5


def parse_ctx_pct(text):
    """-> int | None: the last `465.4K (44%)` footer figure in a pane read."""
    found = CTX_RE.findall(text or "")
    return int(found[-1]) if found else None


def strip_tui(text, n):
    """-> the last n non-blank pane lines with TUI chrome gone: lines made only of box/bar glyphs
    drop, a leading `┃ ` goes."""
    out = []
    for line in (text or "").splitlines():
        if not line.strip() or all(c in TUI_CHROME for c in line):
            continue
        line = line.rstrip()
        s = line.lstrip()
        out.append(s[1:].strip() if s.startswith("┃") else line.strip())
    return out[-n:] if n > 0 else []


def _json_lines(text):
    for line in reversed([x for x in (text or "").splitlines() if x.strip()]):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            return d
    return {}


def _status_git(path, base):
    out = {"commits_ahead": None, "commits": [], "diff_shortstat": None, "modified": [],
           "head_subject": None, "head_age_min": None, "git_error": None}
    try:
        r = git(["-C", path, "log", "--oneline", f"{base}..HEAD"])
        lines = [x for x in r.stdout.splitlines() if x.strip()]
        out["commits_ahead"], out["commits"] = len(lines), lines[:10]
        out["diff_shortstat"] = git(["-C", path, "diff", "--shortstat", "HEAD"]).stdout.strip() or None
        mods = [x for x in git(["-C", path, "status", "--porcelain"]).stdout.splitlines()
                if x.strip() and not os.path.basename(x[3:].strip().strip('"')).startswith(".matrixkoder-")]
        out["modified"] = mods[:20]
        h = git(["-C", path, "log", "-1", "--format=%ct%x09%s"]).stdout.strip().split("\t", 1)
        if len(h) == 2 and h[0].isdigit():
            out["head_subject"], out["head_age_min"] = h[1], round((time.time() - int(h[0])) / 60, 1)
    except Fail as e:
        out["git_error"] = str(e)
    return out


def _status_file(path):
    out = {"agent_state": None, "round": None, "role": None, "step": None, "step_age_min": None,
           "ctx_pct": None, "report_present": None}
    try:
        with open(os.path.join(path, ".matrixkoder-status.json")) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except (OSError, ValueError):
        d = {}
    out.update(agent_state=d.get("state"), round=d.get("round"), role=d.get("role"), step=d.get("step"),
               ctx_pct=d.get("ctx_pct"))
    if isinstance(d.get("updated_at"), (int, float)):
        out["step_age_min"] = round((time.time() - d["updated_at"]) / 60, 1)
    if isinstance(d.get("round"), int):
        out["report_present"] = os.path.isfile(os.path.join(path, f".matrixkoder-report-r{d['round']}.md"))
    return out


def _status_task(data, run, task):
    out = {"task_status": None, "task_model": None, "task_effort": None, "task_pane": None,
           "children": None, "task_error": None}
    if not task:
        return out
    cmd = [sys.executable, os.path.join(HERE, "mk-state.py"), "--data", data]
    if run:
        cmd += ["--run", run]
    try:
        r = run_cmd(cmd + ["task", "get", task], GIT_TIMEOUT)
        d = _json_lines(r.stdout)
        t = d.get("task")
        if not d.get("ok") or not isinstance(t, dict):
            raise Fail(d.get("error") or (r.stderr or "task get failed").strip()[:200])
        out.update(task_status=t.get("status"), task_model=t.get("model"), task_effort=t.get("effort"),
                   task_pane=t.get("pane"))
        kids = d.get("children") or t.get("children") or t.get("subtasks")
        if isinstance(kids, list):
            out["children"] = [{"id": k.get("id"), "status": k.get("status")} if isinstance(k, dict) else k
                               for k in kids]
    except Fail as e:
        out["task_error"] = str(e)
    return out


def _status_herdr(path, workspace_id, pane, lines):
    out = {"pane": pane, "agent": None, "agent_status": None, "pane_tail": None, "ctx_pct_pane": None,
           "herdr_error": None}
    if herdr_bin() == "none":
        out["herdr_error"] = "herdr disabled (MK_HERDR=none)"
        return out
    try:
        if not pane:
            r = run_cmd([herdr_bin(), "agent", "list"], HERDR_STATUS_TIMEOUT)
            real = os.path.realpath(path)
            for e in (_json_lines(r.stdout).get("result") or {}).get("agents") or []:
                cwd = os.path.realpath(e["cwd"]) if isinstance(e, dict) and e.get("cwd") else None
                if isinstance(e, dict) and e.get("pane_id") and (
                        (cwd and (cwd == real or cwd.startswith(real + os.sep)))
                        or (workspace_id not in (None, STALE_WS_ID) and e.get("workspace_id") == workspace_id)):
                    pane = out["pane"] = e["pane_id"]
                    break
        if not pane:
            out["herdr_error"] = "no pane found for this lane"
            return out
        r = run_cmd([herdr_bin(), "agent", "get", pane], HERDR_STATUS_TIMEOUT)
        a = ((_json_lines(r.stdout).get("result") or {}).get("agent")) or {}
        out["agent"], out["agent_status"] = a.get("agent"), a.get("agent_status")
        r = run_cmd([herdr_bin(), "agent", "read", pane, "--lines", str(max(lines * 3, 30))], HERDR_STATUS_TIMEOUT)
        if r.returncode == 0:
            out["pane_tail"], out["ctx_pct_pane"] = strip_tui(r.stdout, lines), parse_ctx_pct(r.stdout)
        else:
            out["herdr_error"] = (r.stderr or r.stdout).strip()[:150] or f"herdr read exit {r.returncode}"
    except (Fail, TypeError, AttributeError, KeyError) as e:
        out["herdr_error"] = str(e)
    return out


def cmd_status(a, cfg, data, main):
    lanes = read_lanes(data)  # read-only, like cmd_list
    rows = []
    for n in slot_numbers(lanes):
        rec = lanes["slots"][str(n)]
        if a.slot is not None and n != a.slot:
            continue
        if a.task and rec.get("task") != a.task:
            continue
        if a.run and rec.get("run") != a.run:
            continue
        if a.slot is None and not a.task and rec.get("state") != "busy":
            continue
        path = rec.get("path") or ""
        row = {"slot": n, "path": path, "run": rec.get("run"), "task": rec.get("task"),
               "branch": rec.get("branch"), "state": rec.get("state")}
        row.update(_status_git(path, rec.get("base") or "main") if os.path.isdir(path)
                   else {"git_error": "lane path missing"})
        row.update(_status_file(path))
        row.update(_status_task(data, rec.get("run"), rec.get("task")))
        row.update(_status_herdr(path, rec.get("workspace_id"), row["task_pane"], a.lines))
        if row["ctx_pct"] is None:
            row["ctx_pct"] = row["ctx_pct_pane"]
        rows.append(row)
    return {"ok": True, "lanes": rows}


def fmt_status(rows):
    def age(m):
        return "?" if m is None else f"{m:g}m ago"
    out = []
    for r in rows:
        out.append(f"== slot {r['slot']} {r['state']}  run {r['run']}  task {r['task']}  branch {r['branch']}")
        out.append(f"   path {r['path']}")
        out.append(f"   task: {r['task_status']} {r['task_model']} {r['task_effort']}"
                   + (f"  children {r['children']}" if r.get("children") else "")
                   + (f"  [task_error: {r['task_error']}]" if r.get("task_error") else ""))
        out.append(f"   agent: {r['agent']} {r['agent_status']}  ctx {r['ctx_pct']}%  pane {r['pane']}"
                   + (f"  [herdr: {r['herdr_error']}]" if r.get("herdr_error") else ""))
        out.append(f"   status file: {r['agent_state']} round {r['round']} {r['role']}  step "
                   f"\"{r['step']}\" {age(r['step_age_min'])}  report {r['report_present']}")
        if r.get("git_error"):
            out.append(f"   git: {r['git_error']}")
        else:
            out.append(f"   git: {r['commits_ahead']} ahead; uncommitted {r['diff_shortstat'] or 'none'}; "
                       f"HEAD \"{r['head_subject']}\" {age(r['head_age_min'])}")
            for c in r["commits"][:3]:
                out.append(f"     + {c}")
            if r["modified"]:
                out.append("   modified: " + ", ".join(m.strip() for m in r["modified"][:8])
                           + (f" (+{len(r['modified']) - 8})" if len(r["modified"]) > 8 else ""))
        for t in r.get("pane_tail") or []:
            out.append(f"   | {t}")
    return "\n".join(out) if out else "no busy lanes"


# ---------- cli ----------

class Parser(argparse.ArgumentParser):
    def __init__(self, *a, **kw):
        kw.setdefault("allow_abbrev", False)  # no --dat/--lock prefix matching, anywhere
        super().__init__(*a, **kw)

    def error(self, message):
        raise Fail(message)


def build_parser():
    top = Parser(add_help=False)
    top.add_argument("--data")
    top.add_argument("--lock-timeout", type=float, default=LOCK_TIMEOUT, help=argparse.SUPPRESS)
    p = Parser(prog="mk-lane.py", parents=[top])
    sub = p.add_subparsers(dest="cmd", required=True)
    # subparser copies SUPPRESS so a flag given before the subcommand is never clobbered
    dp = Parser(add_help=False)
    dp.add_argument("--data", default=argparse.SUPPRESS)
    dp.add_argument("--lock-timeout", type=float, default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    def lane_args(x):
        x.add_argument("--run", required=True)
        x.add_argument("--role", default="build", choices=ROLES)
        x.add_argument("--round", type=int, default=1)

    x = sub.add_parser("names", parents=[dp])
    lane_args(x)
    x.add_argument("--task", required=True)
    x.add_argument("--slot", type=int, default=1)
    x.set_defaults(func=cmd_names)
    x = sub.add_parser("acquire", parents=[dp])
    lane_args(x)
    x.add_argument("--task", help="one task; or --tasks A,B,C for a group on one slot")
    x.add_argument("--tasks", help="comma-separated task group: one slot, lane fields on every task")
    x.add_argument("--base", required=False, default=None)
    x.set_defaults(func=cmd_acquire)
    x = sub.add_parser("release", parents=[dp])
    x.add_argument("--slot", type=int, required=True), x.add_argument("--integration", required=True)
    x.set_defaults(func=cmd_release)
    x = sub.add_parser("gc", parents=[dp])
    x.add_argument("--keep", type=int, default=1)
    x.set_defaults(func=cmd_gc)
    sub.add_parser("list", parents=[dp]).set_defaults(func=cmd_list)
    x = sub.add_parser("carry-legacy", parents=[dp])
    x.add_argument("--dry-run", action="store_true")
    x.set_defaults(func=cmd_carry_legacy)
    x = sub.add_parser("status", parents=[dp])
    x.add_argument("--slot", type=int), x.add_argument("--task"), x.add_argument("--run")
    x.add_argument("--lines", type=int, default=12), x.add_argument("--json", action="store_true")
    x.set_defaults(func=cmd_status)
    return p


def main(argv):
    a = None
    try:
        a = build_parser().parse_args(argv)
        if not math.isfinite(a.lock_timeout) or a.lock_timeout < 0:
            raise Fail("--lock-timeout must be a finite number >= 0")
        data = mk_state.resolve_data(a.data)
        out, code = a.func(a, load_config(data), data, main_checkout()), 0
    except Fail as e:
        out, code = {"ok": False, "error": str(e), **e.extra}, e.code
    except (OSError, ValueError) as e:  # unreadable file, corrupt JSON: still one JSON line
        out, code = {"ok": False, "error": f"{type(e).__name__}: {e}"}, 2
    if code == 0 and out.get("ok") and getattr(a, "cmd", "") == "status" and not a.json:
        print(fmt_status(out["lanes"]))
        return 0
    print(json.dumps(out))
    return code


# ---------- self-test: real git in temp repos, temp data dir, never the real ~/.matrixkoder ----------

def self_test():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="mk-lane-test-"))  # realpath: paths are reported that way
    lane_home = os.path.join(tmp, "home")
    os.environ["MK_HOME"] = lane_home
    os.environ["MATRIXKODER_HOME"] = lane_home
    os.environ["MK_SELFTEST"] = "1"
    # MK_OMP_USER_SKILLS points the user-level skill comparison at a temp stand-in (empty until 6c
    # fills it), so no case ever reads the machine's real ~/.omp copy: a version drift there must
    # not decide this test's outcome.
    user_skills = os.path.join(tmp, "user-omp-skills")
    env = dict(os.environ, MATRIXKODER_HOME=lane_home, MK_HOME=lane_home, MK_SELFTEST="1",
               MK_HERDR="none", MK_OMP_USER_SKILLS=user_skills)
    env.pop("MATRIXKODER_DATA", None)
    data, repo = os.path.join(tmp, "data"), os.path.join(tmp, "repo")
    os.makedirs(data)
    os.makedirs(repo)
    self_path = os.path.abspath(__file__)

    def g(*args, cwd=repo, check=True):
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if check:
            assert r.returncode == 0, (args, r.stderr)
        return r

    def run_py(script, args, code=0, e=None):
        r = subprocess.run([sys.executable, script, *args, "--data", data], capture_output=True,
                           text=True, env=e or env, cwd=repo)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    def mk(*args, code=0, e=None):
        return run_py(self_path, list(args), code, e)

    def st(*args, code=0):
        return run_py(os.path.join(HERE, "mk-state.py"), list(args), code)

    def write(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def stable(obj):
        """The reply without its conditional keys. `warning` (a drifted user-level skill copy) and
        `warnings` (a lanes: line we could not parse) appear only when the machine says so; the
        assertions below are about the STABLE keys, so neither may flip a comparison."""
        return {k: v for k, v in obj.items() if k not in ("warning", "warnings")}

    def yaml_cap(slots, cap, pattern="../{repo}-mk-{n}", disposable=None):
        with open(mkpaths.legacy_yaml(data), "w") as f:
            f.write(f'repo:\n  lanes: {{ slots: {slots}, pattern: "{pattern}", reuse: true }}\n'
                    f"routing_preferences:\n  max_build_lanes: {cap}\n")
            if disposable:
                f.write(f"lanes_disposable: [{disposable}]\n")

    try:
        # a real repo: main + the integration branch in its own checkout, one lockfile, and ignored
        # node_modules/ __pycache__/ .venv/ .omc/ prod.sqlite. Main stays on `main` for the whole test,
        # like the real orchestrator: merges land in the integration checkout.
        g("init", "-q", "-b", "main")
        g("config", "user.email", "mk-lane@test")
        g("config", "user.name", "mk-lane self-test")
        write(os.path.join(repo, "README.md"), "base\n")
        write(os.path.join(repo, "pnpm-lock.yaml"), "lock v1\n")
        # `.venv/` is anchored at the root so a *tracked* vendor/.venv/ can exist for the 11c case
        write(os.path.join(repo, ".gitignore"),
              "node_modules/\n__pycache__/\n/.venv/\n.omc/\nprod.sqlite\n.claude/\n")
        write(os.path.join(repo, "pkg", "a", "src", "app.js"), "// tracked, so git lists the\n"
              "                        // ignored dir inside pkg/ instead of collapsing to `?? pkg/`\n")
        write(os.path.join(repo, "scripts", "tool.py"), "# tracked\n")
        # tracked beside the nested `.omc` the 8b case writes, so git names `!! src/x/.omc/`
        # instead of collapsing the whole tree to `!! src/`
        write(os.path.join(repo, "src", "x", "keep.txt"), "tracked neighbour\n")
        g("add", "-A"), g("commit", "-qm", "base"), g("branch", "mk/r1/integration")
        integ = os.path.join(tmp, "integ")  # the integration checkout, not a slot
        g("worktree", "add", "-q", integ, "mk/r1/integration")
        slot1, slot2 = os.path.join(tmp, "repo-mk-1"), os.path.join(tmp, "repo-mk-2")

        def merge_integration(branch):
            return g("-C", integ, "merge", "-q", "--ff-only", branch)

        def main_head():
            return g("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

        # config: L3's shape (with its trailing comment), defaults, cap math, disposable validation
        assert load_config(data) == {**DEFAULTS, "warnings": []}, load_config(data)  # no yaml at all
        write(mkpaths.legacy_yaml(data),
              'repo:\n  lanes: { slots: 2, pattern: "../{repo}-lanes-{n}", reuse: true }   '
              "# reusable worktree slots; the cap is max_build_lanes\n")
        assert load_config(data) == {**DEFAULTS, "slots": 2, "pattern": "../{repo}-lanes-{n}",
                                     "cap": 3, "disposable": [], "warnings": []}, load_config(data)
        yaml_cap(2, 2, "../{repo}-lanes-{n}", "build")
        assert load_config(data) == {**DEFAULTS, "slots": 2, "pattern": "../{repo}-lanes-{n}",
                                     "cap": 2, "disposable": ["build"], "warnings": []}, load_config(data)
        yaml_cap(1, 3)
        assert load_config(data)["cap"] == 3, load_config(data)  # the cap is max_build_lanes
        yaml_cap(4, 2)  # a bigger pool ask never raises the cap: mk-config's value wins (Z24)
        assert load_config(data)["cap"] == 2, load_config(data)
        write(mkpaths.legacy_yaml(data),
              "repo:\n  lanes: { slots: 1, pattern: ../{repo}-mk-{n}, reuse: false }\n")
        assert load_config(data) == {**DEFAULTS, "slots": 1, "reuse": False,
                                     "warnings": []}, load_config(data)
        # a wildcard disposable entry is refused, so it can never widen the allowlist to "everything"
        write(mkpaths.legacy_yaml(data),
              'repo:\n  lanes: { slots: 1, pattern: "../{repo}-mk-{n}", reuse: true }\n'
              'lanes_disposable: ["*", "../secrets", "*.db", "build", "dist/"]\n')
        cfg = load_config(data)
        assert cfg["disposable"] == ["build", "dist/"], cfg
        assert len(cfg["warnings"]) == 3, cfg
        # lanes.copy: a flow list inside the lanes map; only literal relative paths are accepted
        write(mkpaths.legacy_yaml(data),
              'repo:\n  lanes: { slots: 2, pattern: "../{repo}-mk-{n}", reuse: true, '
              'copy: [".claude-plugin/types", ".env"] }\n')
        cfg = load_config(data)
        assert cfg["copy"] == [".claude-plugin/types", ".env"] and cfg["warnings"] == [], cfg
        write(mkpaths.legacy_yaml(data),
              'repo:\n  lanes: { slots: 1, pattern: "../{repo}-mk-{n}", reuse: true, '
              'copy: ["*", "../secrets", "/abs", ".", "ok-dir/"] }\n')
        cfg = load_config(data)
        assert cfg["copy"] == ["ok-dir/"] and len(cfg["warnings"]) == 4, cfg
        yaml_cap(1, 1)

        # Z24: the lane cap comes from the project settings file — the C2 project.yaml a migrated
        # root row points at — never from the legacy matrixkoder.yaml beside the old data dir, and
        # never raised by repo.lanes.slots. Both data forms of a migrated project must answer the
        # value mk-config reports for routing_preferences.max_build_lanes.
        slug, legacy = "cap-proj", os.path.join(tmp, "cap-legacy")
        c2 = os.path.join(lane_home, "projects", slug)
        write(mkpaths.project_yaml_in(c2),
              "repo:\n  lanes: { slots: 5, pattern: ../{repo}-mk-{n}, reuse: true }\n"
              "routing_preferences:\n  max_build_lanes: 4\n")
        write(mkpaths.legacy_yaml(legacy),
              "repo:\n  lanes: { slots: 1, pattern: ../{repo}-mk-{n}, reuse: true }\n"
              "routing_preferences:\n  max_build_lanes: 5\n")
        root = mkdb.open_root(lane_home)  # C3's migrated marker is this row's db_path
        try:
            root.execute("CREATE TABLE IF NOT EXISTS projects("
                         "slug TEXT PRIMARY KEY, name TEXT, repo TEXT, data_dir TEXT, mode TEXT,"
                         " created TEXT, last_activity TEXT, db_path TEXT)")
            root.execute("INSERT OR REPLACE INTO projects(slug, name, repo, data_dir, mode, db_path)"
                         " VALUES (?,?,?,?,?,?)",
                         # repo: a path of its own, so data_dir(None)'s repo lookup never finds this row
                         (slug, slug, os.path.join(tmp, "cap-repo"), legacy, "home",
                          mkpaths.db_in(c2)))
            root.commit()
        finally:
            root.close()
        mkpaths._MIGRATED_MEMO = None  # a new root row: drop the memo this process holds
        for d in (legacy, c2):  # the legacy data dir, and the C2 project folder as --data
            cap = load_config(d)["cap"]
            r = subprocess.run([sys.executable, os.path.join(HERE, "mk-config.py"), "--data", d,
                                "get", "routing_preferences.max_build_lanes"],
                               capture_output=True, text=True, env=env, cwd=repo)
            assert (cap, r.returncode) == (4, 0), (d, cap, r.stdout, r.stderr)
            assert json.loads(r.stdout)["value"] == cap, (d, cap, r.stdout)
        # unmigrated: no root row, so the legacy file in the data dir is still the config
        plain = os.path.join(tmp, "cap-plain")
        write(mkpaths.legacy_yaml(plain),
              "repo:\n  lanes: { slots: 1, pattern: ../{repo}-mk-{n}, reuse: true }\n"
              "routing_preferences:\n  max_build_lanes: 6\n")
        assert load_config(plain)["cap"] == 6, load_config(plain)
        # a migrated project whose project.yaml is gone falls back to its legacy file, not DEFAULTS
        os.unlink(mkpaths.project_yaml_in(c2))
        assert load_config(legacy)["cap"] == 5, load_config(legacy)

        # 1 names: lowercased, digit-prefixed agent, the 32-char limit, integration branch
        n = mk("names", "--run", "R1", "--task", "T1")
        assert n == {"ok": True, "branch": "mk/r1/t1", "integration": "mk/r1/integration",
                     "path": slot1, "workspace": "mk:repo:1", "agent": "t1-build-r1"}, n
        assert mk("names", "--run", "r1", "--task", "2A")["agent"] == "t2a-build-r1"
        assert mk("names", "--run", "r1", "--task", "t.1", "--role", "fix",
                  "--round", "3")["agent"] == "t-1-fix-r3"
        big = mk("names", "--run", "r1", "--task", "a" * 40)
        assert len(big["agent"]) <= 32 and AGENT_RE.fullmatch(big["agent"]), big
        assert big["agent"].endswith("-build-r1"), big
        assert mk("names", "--run", "r1", "--task", "x", "--slot", "2")["workspace"] == "mk:repo:2"
        # a pattern without {n} would put every slot on one path: refused before anything is computed
        write(mkpaths.legacy_yaml(data),
              'repo:\n  lanes: { slots: 1, pattern: "../{repo}", reuse: true }\n')
        assert "{n}" in mk("names", "--run", "r1", "--task", "t1", code=2)["error"]
        yaml_cap(1, 1)

        st("run", "init", "--id", "r1", "--feature", "Lanes", "--branch", "mk/r1/integration")
        st("task", "add", "--root", "--id", "t1", "--title", "first", "--run", "r1")

        # 2 first acquire creates slot 1 on the task branch and records the task keys
        omp_slot_skill = os.path.join(slot1, ".omp/skills/mk-builder")
        a1 = mk("acquire", "--run", "r1", "--task", "t1", "--base", "main")
        # the STABLE keys, compared exactly: `warning`/`warnings` are conditional (a drifted user
        # skill copy, a config line we could not parse) and must never enter this assertion
        assert stable(a1) == {"ok": True, "slot": 1, "path": slot1, "workspace": "mk:repo:1",
                             "workspace_id": None, "branch": "mk/r1/t1", "agent": "t1-build-r1",
                             "created": True, "install_needed": True,
                             "skill_installed": os.path.join(slot1, ".claude/skills/mk-builder"),
                             "omp_skill_installed": omp_slot_skill,
                             "guard_path": os.path.join(slot1, ".matrixkoder-bin/git")}, a1
        assert open(os.path.join(omp_slot_skill, "SKILL.md"), "rb").read() == \
            open(os.path.join(os.path.dirname(HERE), "child-skills", "mk-builder", "SKILL.md"),
                 "rb").read()  # an omp lane reads .omp/skills; it holds the plugin's bytes
        assert os.path.isdir(slot1)
        assert g("-C", slot1, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "mk/r1/t1"
        assert mk("list")["slots"][0]["state"] == "busy"
        # the slot's info/exclude carries the lane entries (.omo/ is not in .gitignore at all),
        # including the installed skill dir — without it the install would read as untracked
        ep = g("-C", slot1, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        ep = ep if os.path.isabs(ep) else os.path.join(slot1, ep)
        excl = open(ep).read()
        assert ".matrixkoder-*" in excl and ".omo/" in excl
        assert ".claude/skills/mk-builder/" in excl and ".omp/skills/mk-builder/" in excl
        t = st("task", "get", "t1")["task"]
        assert (t["lane"], t["branch"], t["workspace"], t["slot"]) == (slot1, "mk/r1/t1", "mk:repo:1", "1"), t

        # 3 cap 1 with the only slot busy
        assert mk("acquire", "--run", "r1", "--task", "t2", "--base", "main",
                  code=4)["error"] == "all 1 slots busy"

        # 4 merge the task branch into the integration branch (main stays on main), then release
        write(os.path.join(slot1, "work.md"), "work\n")
        g("-C", slot1, "add", "-A"), g("-C", slot1, "commit", "-qm", "work")
        merge_integration("mk/r1/t1")
        head = g("-C", integ, "rev-parse", "HEAD").stdout.strip()
        rel = mk("release", "--slot", "1", "--integration", "mk/r1/integration")
        assert rel == {"ok": True, "slot": 1, "path": slot1, "state": "free",
                       "integration": "mk/r1/integration", "branch": "mk/r1/t1",
                       "branch_deleted": True, "scratch_removed": []}, rel
        assert main_head() == "main"  # merged-ness judged against integration, not this HEAD
        assert g("rev-parse", "--verify", "--quiet", "refs/heads/mk/r1/t1",
                 check=False).returncode != 0  # the task branch is gone
        assert g("-C", slot1, "rev-parse", "HEAD").stdout.strip() == head  # detached at integration
        assert g("-C", slot1, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0
        assert mk("list")["slots"][0]["state"] == "free"

        # 5 the same slot is reused; the lockfile hash decides install_needed
        a2 = mk("acquire", "--run", "r1", "--task", "t3", "--base", "mk/r1/integration")
        assert (a2["slot"], a2["path"], a2["created"], a2["install_needed"]) == (1, slot1, False, False), a2
        write(os.path.join(slot1, "work.md"), "work\nmore\n")
        g("-C", slot1, "commit", "-aqm", "more"), merge_integration("mk/r1/t3")
        write(os.path.join(integ, "pnpm-lock.yaml"), "lock v2\n")
        g("-C", integ, "commit", "-aqm", "lock")
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["branch_deleted"]
        a3 = mk("acquire", "--run", "r1", "--task", "t4", "--base", "mk/r1/integration")
        assert (a3["created"], a3["install_needed"]) == (False, True), a3  # lockfile changed on the base

        # 6 a dirty slot is refused by acquire and release, with no state or HEAD change
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["branch_deleted"]
        head = g("-C", slot1, "rev-parse", "HEAD").stdout.strip()
        write(os.path.join(slot1, "scratch.txt"), "untracked\n")
        d = mk("acquire", "--run", "r1", "--task", "t5", "--base", "main", code=3)
        assert d["error"] == "slot 1 dirty" and "?? scratch.txt" in d["dirty"], d
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration",
                  code=3)["error"] == "slot 1 dirty"
        assert mk("list")["slots"][0]["clean"] is False
        assert g("-C", slot1, "rev-parse", "HEAD").stdout.strip() == head  # HEAD untouched
        assert g("-C", slot1, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0  # still detached
        assert g("rev-parse", "--verify", "--quiet", "refs/heads/mk/r1/t5",
                 check=False).returncode != 0  # no branch was created
        rec = mk("list")["slots"][0]  # L10.5: the rows are the registry; no lanes.json exists
        assert (rec["state"], rec["branch"], rec["task"]) == ("free", None, None), rec
        # `status.showUntrackedFiles=no` in the user's config must not hide it: the flags win
        g("config", "status.showUntrackedFiles", "no")
        os.unlink(os.path.join(slot1, "scratch.txt"))
        write(os.path.join(slot1, "hidden.txt"), "untracked, config-hidden\n")
        assert mk("list")["slots"][0]["clean"] is False
        assert mk("acquire", "--run", "r1", "--task", "t5", "--base", "main",
                  code=3)["dirty"] == ["?? hidden.txt"]
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration",
                  code=3)["error"] == "slot 1 dirty"
        assert mk("list")["slots"][0]["state"] == "free"  # no state change
        g("config", "--unset", "status.showUntrackedFiles")
        os.unlink(os.path.join(slot1, "hidden.txt"))
        # an ignored node_modules/ or .omc/ is disposable, not dirty; .omo/ is ignored only through
        # the exclude entry written at slot creation, and disposable the same way
        os.makedirs(os.path.join(slot1, "node_modules", "pkg"), exist_ok=True)
        os.makedirs(os.path.join(slot1, ".omc", "sessions"), exist_ok=True)
        os.makedirs(os.path.join(slot1, ".omo", "state"), exist_ok=True)
        write(os.path.join(slot1, "node_modules", "pkg", "i.js"), "x\n")
        write(os.path.join(slot1, ".omc", "sessions", "s.json"), "{}\n")
        write(os.path.join(slot1, ".omo", "state", "s.json"), "{}\n")
        write(os.path.join(slot1, ".matrixkoder-report-r1.md"), "report\n")
        assert mk("list")["slots"][0]["clean"] is True
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        assert mk("acquire", "--run", "r1", "--task", "t5", "--base", "mk/r1/integration")["ok"]
        # (that acquire also archived the report file from the lines above — its own old dir)
        assert os.listdir(os.path.join(slot1, ".matrixkoder-old-r1")) == [".matrixkoder-report-r1.md"]

        # 6b the acceptance case, on a REUSED free slot: four stale top-level reports plus the
        # brief and watch state; acquire must archive the four into .matrixkoder-old-r1/ (moved,
        # never deleted) and install skill + guard. The stale files are `.matrixkoder-*` — ignored
        # via the exclude, disposable — so require_clean never refuses the slot.
        shutil.rmtree(os.path.join(slot1, ".matrixkoder-old-r1"))  # reset the fixture from above
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        stale_files = {".matrixkoder-report-r1.md": "stale report\n",
                       ".matrixkoder-review-r1.md": "stale review\n",
                       ".matrixkoder-status.json": '{"stale": true}\n',
                       ".matrixkoder-old-code.patch": "--- stale\n"}
        for name, body in stale_files.items():
            write(os.path.join(slot1, name), body)
        write(os.path.join(slot1, ".matrixkoder-brief.md"), "# brief\n")
        write(os.path.join(slot1, ".matrixkoder-watch.json"), '{"watch": 1}\n')
        write(os.path.join(slot1, ".matrixkoder-watch.lock"), "")
        old1 = os.path.join(slot1, ".matrixkoder-old-r1")
        skill_dst = os.path.join(slot1, ".claude", "skills", "mk-builder")
        omp_dst = os.path.join(slot1, ".omp", "skills", "mk-builder")
        guard_dst = os.path.join(slot1, ".matrixkoder-bin", "git")
        src_skill = os.path.join(os.path.dirname(HERE), "child-skills", "mk-builder")
        st("task", "add", "--root", "--id", "t6a", "--title", "archive case a", "--run", "r1")
        acq1 = mk("acquire", "--run", "r1", "--task", "t6a", "--base", "main")
        assert (acq1["ok"], acq1["skill_installed"], acq1["omp_skill_installed"],
                acq1["guard_path"]) == (True, skill_dst, omp_dst, guard_dst), acq1
        # the four stale files plus the brief (no longer kept) sit in the archive, byte-identical;
        # only the watch state remains at top level among `.matrixkoder-*` FILES
        # (the archive and .matrixkoder-bin/ are dirs)
        archived = dict(stale_files)
        archived[".matrixkoder-brief.md"] = "# brief\n"
        assert sorted(os.listdir(old1)) == sorted(archived), os.listdir(old1)
        for name, body in archived.items():
            assert open(os.path.join(old1, name), "rb").read() == body.encode(), name
        top_files = sorted(x for x in os.listdir(slot1) if x.startswith(".matrixkoder-")
                           and os.path.isfile(os.path.join(slot1, x)))
        assert top_files == [".matrixkoder-watch.json", ".matrixkoder-watch.lock"], top_files
        assert not os.path.isfile(os.path.join(slot1, ".matrixkoder-brief.md"))
        # the install matches the plugin source, and the guard is executable
        assert open(os.path.join(skill_dst, "SKILL.md"), "rb").read() == \
            open(os.path.join(src_skill, "SKILL.md"), "rb").read()
        assert open(guard_dst, "rb").read() == \
            open(os.path.join(src_skill, "scripts", "git-guard"), "rb").read()
        assert os.access(guard_dst, os.X_OK)
        # release still passes its clean check with the installed skill dir + guard present
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        # a second acquire on the clean slot is idempotent: same fields, byte-identical install,
        # and no duplicate archive content (nothing stale was left to move a second time)
        st("task", "add", "--root", "--id", "t6b", "--title", "archive case b", "--run", "r1")
        acq2 = mk("acquire", "--run", "r1", "--task", "t6b", "--base", "main")
        assert (acq2["ok"], acq2["skill_installed"], acq2["omp_skill_installed"],
                acq2["guard_path"]) == (True, skill_dst, omp_dst, guard_dst), acq2
        assert sorted(os.listdir(old1)) == sorted(archived), os.listdir(old1)
        assert open(os.path.join(skill_dst, "SKILL.md"), "rb").read() == \
            open(os.path.join(src_skill, "SKILL.md"), "rb").read()
        assert os.access(guard_dst, os.X_OK)
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        # back on t5 at the integration tip, the state case 8 below expects
        assert mk("acquire", "--run", "r1", "--task", "t5", "--base", "mk/r1/integration")["ok"]
        # 6c (C4) the user-level ~/.omp copy is COMPARED, never written: a drifted copy rides in
        # the acquire warning and is left exactly as it was; a matching one says nothing.
        usr = os.path.join(tmp, "user-omp-skills", "mk-builder")
        os.makedirs(usr)
        write(os.path.join(usr, "SKILL.md"), "a stale user-level copy\n")
        uenv = dict(env, MK_OMP_USER_SKILLS=os.path.join(tmp, "user-omp-skills"))
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        st("task", "add", "--root", "--id", "t6c", "--title", "user skill mismatch", "--run", "r1")
        acq3 = mk("acquire", "--run", "r1", "--task", "t6c", "--base", "mk/r1/integration", e=uenv)
        assert "mismatch" in acq3.get("warning", "") and usr in acq3["warning"], acq3
        assert open(os.path.join(usr, "SKILL.md")).read() == "a stale user-level copy\n"  # untouched
        write(os.path.join(usr, "SKILL.md"), open(os.path.join(src_skill, "SKILL.md")).read())
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        st("task", "add", "--root", "--id", "t6d", "--title", "user skill in sync", "--run", "r1")
        acq4 = mk("acquire", "--run", "r1", "--task", "t6d", "--base", "mk/r1/integration", e=uenv)
        assert "mismatch" not in acq4.get("warning", ""), acq4
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        assert mk("acquire", "--run", "r1", "--task", "t5", "--base", "mk/r1/integration")["ok"]

        # 7 a nested build-cache dir is disposable at any depth; the same names at the root stay
        # root-only for everything else (a nested .env would be dirty)
        os.makedirs(os.path.join(slot1, "pkg", "a", "node_modules", "dep"), exist_ok=True)
        os.makedirs(os.path.join(slot1, "scripts", "__pycache__"), exist_ok=True)
        os.makedirs(os.path.join(slot1, ".venv", "lib"), exist_ok=True)
        write(os.path.join(slot1, "pkg", "a", "node_modules", "dep", "i.js"), "x\n")
        write(os.path.join(slot1, "scripts", "__pycache__", "tool.pyc"), "x\n")
        write(os.path.join(slot1, ".venv", "lib", "p.txt"), "x\n")
        assert mk("list")["slots"][0]["clean"] is True
        os.makedirs(os.path.join(slot1, "pkg", "a", ".env.d"), exist_ok=True)
        write(os.path.join(slot1, "pkg", "a", ".env.local"), "SECRET=1\n")
        assert mk("list")["slots"][0]["clean"] is False  # .env* is root-only
        shutil.rmtree(os.path.join(slot1, "pkg", "a", ".env.d"))
        os.unlink(os.path.join(slot1, "pkg", "a", ".env.local"))
        assert mk("list")["slots"][0]["clean"] is True

        # 8 releasing an unmerged branch keeps it, with a warning, and still frees the slot
        write(os.path.join(slot1, "work.md"), "work\nmore\nunmerged\n")
        g("-C", slot1, "commit", "-aqm", "unmerged")
        rel = mk("release", "--slot", "1", "--integration", "mk/r1/integration")
        assert rel["warning"] == "branch mk/r1/t5 not merged into mk/r1/integration, kept", rel
        assert rel["branch_deleted"] is False and rel["state"] == "free", rel
        assert mk("list")["slots"][0]["state"] == "free"
        assert g("rev-parse", "--verify", "--quiet", "refs/heads/mk/r1/t5").returncode == 0
        assert "mk/r1/t5" not in json.dumps(mk("list"))  # only in the branch ref
        assert not os.path.exists(mkpaths.lanes_file(data)), "a store-backed lane command wrote the lanes file"

        # 8b (L1.1) git-ignored `.omc` state alone is not dirt, at the root and nested, and the
        # check never deletes it. A modified TRACKED `.omc/plans/x.md` still refuses, and so does
        # any other untracked file. The repo ignores `.omc/` wholesale, so the tracked plan is
        # force-added on the integration branch (`git add -f`, here only). Slot 1 stays free.
        write(os.path.join(integ, ".omc", "plans", "x.md"), "tracked plan\n")
        g("-C", integ, "add", "-f", ".omc/plans/x.md")
        g("-C", integ, "commit", "-qm", "track an omc plan")
        omc_root = os.path.join(slot1, ".omc")
        omc_nested = os.path.join(slot1, "src", "x", ".omc")
        for rel in ("project-memory.json", os.path.join("sessions", "s.json"),
                    os.path.join("state", "x")):
            write(os.path.join(omc_root, rel), "{}\n")
            write(os.path.join(omc_nested, rel), "{}\n")
        # the slot still sits on the pre-plan tip, so nothing under .omc/ is tracked here: clean
        assert mk("list")["slots"][0]["clean"] is True, mk("list")
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        for rel in ("project-memory.json", os.path.join("sessions", "s.json"),
                    os.path.join("state", "x")):
            assert os.path.isfile(os.path.join(omc_root, rel)), rel  # never deleted
            assert os.path.isfile(os.path.join(omc_nested, rel)), rel
        # the release switched HEAD to the tip that tracks `.omc/plans/x.md`: ignored siblings
        # inside a tracked .omc tree are user data, not state, so the slot reads dirty again
        assert mk("list")["slots"][0]["clean"] is False, mk("list")
        for rel in ("project-memory.json", os.path.join("sessions", "s.json"),
                    os.path.join("state", "x")):
            os.unlink(os.path.join(omc_root, rel))
            os.unlink(os.path.join(omc_nested, rel))
        os.removedirs(os.path.join(omc_root, "sessions"))
        os.removedirs(os.path.join(omc_root, "state"))
        os.removedirs(os.path.join(omc_nested, "sessions"))
        os.removedirs(os.path.join(omc_nested, "state"))
        assert mk("list")["slots"][0]["clean"] is True, mk("list")
        # a modified tracked plan still refuses, with the ignored state long gone
        write(os.path.join(omc_root, "plans", "x.md"), "tracked plan\nedited\n")
        d = mk("release", "--slot", "1", "--integration", "mk/r1/integration", code=3)
        assert d["error"] == "slot 1 dirty", d
        assert any(".omc/plans/x.md" in line and not line.startswith("!!") for line in d["dirty"]), d
        write(os.path.join(omc_root, "plans", "x.md"), "tracked plan\n")
        # an ordinary untracked file beside nothing else still refuses
        write(os.path.join(slot1, "scratch-omc.txt"), "untracked\n")
        d = mk("release", "--slot", "1", "--integration", "mk/r1/integration", code=3)
        assert d["error"] == "slot 1 dirty" and "?? scratch-omc.txt" in d["dirty"], d
        os.unlink(os.path.join(slot1, "scratch-omc.txt"))
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]

        # 8c (Z21) release removes the slot's ignored scratch: the SDK types `claude plugin test`
        # writes into `.claude-plugin/types/` and the linter's `.ruff_cache`. Both used to read
        # `slot N dirty` (`!! .claude-plugin/types/`), so such a slot was never releasable — the
        # exact refusal seen on slots 1 and 4 on 2026-10-15.
        write(os.path.join(integ, ".gitignore"),
              "node_modules/\n__pycache__/\n/.venv/\n.omc/\nprod.sqlite\n"
              ".claude-plugin/types/\n.ruff_cache/\n")
        g("-C", integ, "add", ".gitignore"), g("-C", integ, "commit", "-qm", "ignore the scratch dirs")
        assert mk("acquire", "--run", "r1", "--task", "t8c", "--base", "mk/r1/integration")["slot"] == 1
        write(os.path.join(slot1, ".claude-plugin", "types", "x.d.ts"), "export type X = number\n")
        write(os.path.join(slot1, ".ruff_cache", "cache.db"), "x\n")
        assert mk("list")["slots"][0]["clean"] is True, mk("list")   # both are disposable
        rel = mk("release", "--slot", "1", "--integration", "mk/r1/integration")
        assert rel["ok"] and sorted(rel["scratch_removed"]) == [".claude-plugin/types", ".ruff_cache"], rel
        assert not os.path.exists(os.path.join(slot1, ".claude-plugin", "types"))
        assert not os.path.exists(os.path.join(slot1, ".ruff_cache"))
        assert os.path.isdir(os.path.join(slot1, ".claude-plugin"))  # only the scratch leaf went

        # 8d any OTHER ignored leftover still refuses, and the release that refuses deletes nothing
        write(os.path.join(integ, ".gitignore"),
              "node_modules/\n__pycache__/\n/.venv/\n.omc/\nprod.sqlite\n"
              ".claude-plugin/types/\n.ruff_cache/\nbuild-out/\n")
        g("-C", integ, "add", ".gitignore"), g("-C", integ, "commit", "-qm", "ignore build-out")
        # re-acquire so the slot sits on the tip that ignores build-out/ (the release above parked
        # it on the older tip, where the folder would read `??` — a different rule than this case's)
        st("task", "add", "--root", "--id", "t8d", "--title", "other ignored leftover", "--run", "r1")
        assert mk("acquire", "--run", "r1", "--task", "t8d", "--base", "mk/r1/integration")["slot"] == 1
        write(os.path.join(slot1, "build-out", "b.js"), "x\n")
        write(os.path.join(slot1, ".ruff_cache", "cache.db"), "x\n")
        assert mk("list")["slots"][0]["clean"] is False  # build-out/ is not on the cleanup list
        d = mk("release", "--slot", "1", "--integration", "mk/r1/integration", code=3)
        assert d["error"] == "slot 1 dirty" and d["dirty"] == ["!! build-out/"], d
        assert os.path.isfile(os.path.join(slot1, ".ruff_cache", "cache.db"))  # left alone while dirty
        shutil.rmtree(os.path.join(slot1, "build-out"))
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        assert not os.path.exists(os.path.join(slot1, ".ruff_cache"))
        # back to the 8b world for the cases below
        write(os.path.join(integ, ".gitignore"),
              "node_modules/\n__pycache__/\n/.venv/\n.omc/\nprod.sqlite\n")
        g("-C", integ, "add", ".gitignore"), g("-C", integ, "commit", "-qm", "restore the ignores")

        # 9 a commit on the free detached slot is not orphaned by the next switch
        write(os.path.join(slot1, "stray.md"), "late builder commit\n")
        g("-C", slot1, "add", "-A"), g("-C", slot1, "commit", "-qm", "stray")
        stray = g("-C", slot1, "rev-parse", "HEAD").stdout.strip()
        s = mk("acquire", "--run", "r1", "--task", "t9", "--base", "main", code=3)
        assert s["unreachable"] == [stray] and "on no branch" in s["error"], s
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration",
                  code=3)["unreachable"] == [stray]
        assert g("-C", slot1, "rev-parse", "HEAD").stdout.strip() == stray  # the commit is still there
        assert mk("list")["slots"][0]["state"] == "free"
        g("-C", slot1, "branch", "mk/r1/rescued", stray)  # give the commit a ref, as a human would
        assert mk("acquire", "--run", "r1", "--task", "t9", "--base", "main")["slot"] == 1

        # 10 ref arguments are untrusted: an option never reaches git
        assert mk("acquire", "--run", "r1", "--task", "ta", "--base=--orphan",
                  code=2)["error"].startswith("--base '--orphan' must be a ref")
        assert "expected one argument" in mk("acquire", "--run", "r1", "--task", "ta", "--base",
                                             "--orphan", code=2)["error"]
        assert mk("acquire", "--run", "r1", "--task", "ta", "--base", "nosuchref",
                  code=2)["error"].startswith("--base 'nosuchref' does not resolve")
        assert mk("release", "--slot", "1", "--integration=--force",
                  code=2)["error"].startswith("--integration '--force' must be a ref")
        assert mk("list")["slots"][0]["task"] == "t9"  # none of them changed anything
        assert check_branch(repo, "mk/r1/t1") == "mk/r1/t1"
        for bad in ("--force", "mk/r1/ t1", "mk//r1", "x.lock/../y"):
            try:
                check_branch(repo, bad)
                raise AssertionError(f"check_branch accepted {bad!r}")
            except Fail:
                pass

        # 11 with cap 2: gc keeps the lowest slot, removes a free clean extra one, skips a dirty one
        mk("release", "--slot", "1", "--integration", "mk/r1/integration")
        yaml_cap(2, 2)
        assert mk("acquire", "--run", "r1", "--task", "t6", "--base", "main")["slot"] == 1
        a2 = mk("acquire", "--run", "r1", "--task", "t7", "--base", "main")
        assert (a2["slot"], a2["path"], a2["created"]) == (2, slot2, True), a2
        assert mk("acquire", "--run", "r1", "--task", "t8", "--base", "main",
                  code=4)["error"] == "all 2 slots busy"
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        assert mk("release", "--slot", "2", "--integration", "mk/r1/integration")["ok"]
        write(os.path.join(slot2, "junk.txt"), "x\n")
        g("config", "status.showUntrackedFiles", "no")  # gc must not be blinded by the config either
        out = mk("gc", "--keep", "1")
        assert out == {"ok": True, "removed": [],
                       "skipped": [{"slot": 2, "reason": "dirty: ?? junk.txt"}]}, out
        assert os.path.isdir(slot1) and os.path.isdir(slot2)  # never removed, only reported
        assert os.path.exists(os.path.join(slot2, "junk.txt"))  # and the file is still there
        g("config", "--unset", "status.showUntrackedFiles")
        os.unlink(os.path.join(slot2, "junk.txt"))
        assert mk("gc", "--keep", "1")["removed"] == [2]
        assert os.path.isdir(slot1) and not os.path.exists(slot2)  # the lowest slot survives
        assert [s["slot"] for s in mk("list")["slots"]] == [1]
        assert mk("gc", "--keep", "0")["removed"] == []  # --keep is clamped to 1

        # 11b gc must not remove a slot whose detached HEAD holds commits no ref reaches: the removal
        # deletes the worktree's logs/HEAD, so the commit would go from reflog-only to dangling
        assert mk("acquire", "--run", "r1", "--task", "ta", "--base", "main")["slot"] == 1
        assert mk("acquire", "--run", "r1", "--task", "tb", "--base", "main")["slot"] == 2
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        assert mk("release", "--slot", "2", "--integration", "mk/r1/integration")["ok"]
        write(os.path.join(slot2, "late.md"), "a late builder commit\n")
        g("-C", slot2, "add", "-A"), g("-C", slot2, "commit", "-qm", "late")
        late = g("-C", slot2, "rev-parse", "HEAD").stdout.strip()
        assert mk("list")["slots"][1]["clean"] is True  # the working tree is clean…
        out = mk("gc", "--keep", "1")
        assert out["removed"] == [] and [x["slot"] for x in out["skipped"]] == [2], out
        assert out["skipped"][0]["unreachable"] == [late], out
        assert out["skipped"][0]["reason"] == "slot 2 has 1 commit(s) on no branch", out
        assert os.path.isdir(slot2) and g("-C", slot2, "rev-parse", "HEAD").stdout.strip() == late
        assert g("for-each-ref", "--contains", late, "--format=%(refname)").stdout.strip() == ""
        # one ref is all it takes, exactly as for a switch
        g("-C", slot2, "branch", "mk/r1/late", late)
        assert mk("gc", "--keep", "1")["removed"] == [2]
        assert g("rev-parse", "--verify", "--quiet", "refs/heads/mk/r1/late").returncode == 0

        # 11c a tracked build-cache dir is source: an ignored file inside it is user data. `.venv/`
        # is not in this repo's .gitignore, so it can be tracked without force-adding — the guard is
        # the same ANY_DEPTH + tracked-files rule the reviewer's vendored node_modules/ hits
        write(os.path.join(integ, "vendor", ".venv", "x.js"), "// vendored\n")
        g("-C", integ, "add", "vendor"), g("-C", integ, "commit", "-qm", "vendor")
        assert mk("acquire", "--run", "r1", "--task", "tc", "--base", "mk/r1/integration")["slot"] == 1
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        vendored = os.path.join(slot1, "vendor", ".venv")
        write(os.path.join(vendored, "prod.sqlite"), "data\n")
        status = g("-C", slot1, "status", "--porcelain", "--ignored").stdout.strip()
        assert "!! vendor/.venv/prod.sqlite" in status.splitlines(), status  # a file, not the dir
        assert mk("list")["slots"][0]["clean"] is False  # not a disposable build cache
        assert mk("gc", "--keep", "1")["removed"] == []
        assert os.path.exists(os.path.join(vendored, "prod.sqlite"))
        assert mk("acquire", "--run", "r1", "--task", "tc", "--base", "main",
                  code=3)["error"] == "slot 1 dirty"
        os.unlink(os.path.join(vendored, "prod.sqlite"))
        assert mk("list")["slots"][0]["clean"] is True

        # 11d --integration must be a real branch, and never the slot's own task branch: as its own
        # "integration" the is-ancestor test would pass with the slot's HEAD as the only ref left
        assert mk("acquire", "--run", "r1", "--task", "td", "--base", "mk/r1/integration")["slot"] == 1
        tip = g("rev-parse", "refs/heads/mk/r1/td").stdout.strip()
        write(os.path.join(slot1, "work.md"), "work\n")
        g("-C", slot1, "add", "-A"), g("-C", slot1, "commit", "-qm", "work"), merge_integration("mk/r1/td")
        for bad, why in (("mk/r1/td", "is the task branch"), (tip, "must be an existing local branch"),
                         ("nosuchbranch", "must be an existing local branch")):
            r = mk("release", "--slot", "1", "--integration", bad, code=2)
            assert why in r["error"], (bad, r)
        assert mk("list")["slots"][0]["state"] == "busy"  # nothing changed
        assert g("rev-parse", "--verify", "--quiet", "refs/heads/mk/r1/td").returncode == 0
        # a tag of the same name must not shadow the branch: the delete is judged on refs/heads/<b>
        g("tag", "mk/r1/td", "main")  # a tag pointing at the *base*, not the task tip
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["branch_deleted"]
        assert g("rev-parse", "--verify", "--quiet", "refs/heads/mk/r1/td", check=False).returncode != 0
        g("tag", "-d", "mk/r1/td")

        # 11e a branch checked out in another worktree is kept, not deleted out from under it: git
        # refuses a second `worktree add` on a live branch, so slot 2's HEAD is pointed at it directly
        # (the state `worktree add --force` would leave, reached here without a forced add)
        assert mk("acquire", "--run", "r1", "--task", "te", "--base", "mk/r1/integration")["slot"] == 1
        write(os.path.join(slot1, "review.md"), "work a reviewer still has open\n")
        g("-C", slot1, "add", "-A"), g("-C", slot1, "commit", "-qm", "work"), merge_integration("mk/r1/te")
        assert mk("acquire", "--run", "r1", "--task", "tf", "--base", "main")["slot"] == 2
        g("-C", slot2, "symbolic-ref", "HEAD", "refs/heads/mk/r1/te")
        assert "branch refs/heads/mk/r1/te" in g("worktree", "list", "--porcelain").stdout
        rel = mk("release", "--slot", "1", "--integration", "mk/r1/integration")
        assert rel["branch_deleted"] is False, rel
        assert rel["warning"] == f"branch mk/r1/te checked out at {slot2}, kept", rel
        assert g("rev-parse", "--verify", "--quiet", "refs/heads/mk/r1/te").returncode == 0
        g("-C", slot2, "symbolic-ref", "HEAD", "refs/heads/mk/r1/tf")  # that checkout is gone again
        assert mk("list")["slots"][0]["state"] == "free"  # and a normal delete still works (step 4)
        assert mk("release", "--slot", "2", "--integration", "mk/r1/integration")["ok"]
        assert mk("gc", "--keep", "1")["removed"] == [2]  # back to a single slot

        # 11f a lanes: line we cannot read is reported, never silently the defaults
        for bad in ('  lanes: { slots: 1, pattern: "../{repo}-mk-{n}", reuse: true }#c\n',
                    "  lanes: { slots: 1, pattern: ../bob's-mk-{n}, reuse: true }   # slots\n"):
            write(mkpaths.legacy_yaml(data), bad)
            w = mk("list")
            assert any("lanes line unparsed" in x for x in w.get("warnings", [])), (bad, w)
            assert any("lanes line unparsed" in x for x in mk("names", "--run", "r1",
                                                              "--task", "t1").get("warnings", []))
            assert w["cap"] == DEFAULTS["cap"] and w["slots"][0]["path"] == slot1  # defaults kept
        yaml_cap(1, 1)
        assert "warnings" not in mk("list")

        # 11g an ignored file inside a TRACKED allowlist directory, in all seven shapes the review
        # reproduces: at the root, under a name with a space, non-ASCII, and two that read as
        # pathspec magic. The ignore rules are narrowed to prod.sqlite so each dir can hold a tracked
        # file — adding one under an ignored dir would need a forced add, which this file never uses.
        shapes = ["node_modules", ".venv", ".omc", "my dir/node_modules", "café/node_modules",
                  ":x/node_modules", ":(z)/node_modules"]
        for junk in ("node_modules", ".omc", ".omo", "pkg/a/node_modules", ".venv",
                     "scripts/__pycache__", ".matrixkoder-report-r1.md"):  # ignored fixtures only
            victim = os.path.join(slot1, junk)
            if os.path.isdir(victim):
                shutil.rmtree(victim)
            elif os.path.exists(victim):
                os.unlink(victim)
        yaml_cap(2, 2)
        write(os.path.join(integ, ".gitignore"), "prod.sqlite\n")
        # 8b force-added `.omc/plans/x.md` under a wholesale `.omc/` ignore; narrowing the ignore
        # would otherwise leave that file deleted in this commit
        write(os.path.join(integ, ".omc", "plans", "x.md"), "tracked plan\n")
        for rel in shapes:
            write(os.path.join(integ, rel, "x.js"), "// tracked\n")
        g("-C", integ, "add", "-A"), g("-C", integ, "commit", "-qm", "tracked cache dirs")
        for rel in shapes:  # ignored, so untracked: the fixture is the file only
            write(os.path.join(integ, rel, "prod.sqlite"), "data\n")
        # slot 1 still carries the 8b deletion of `.omc/plans/x.md` in its index; restore it
        write(os.path.join(slot1, ".omc", "plans", "x.md"), "tracked plan\n")
        g("-C", slot1, "add", "-f", ".omc/plans/x.md")
        assert mk("acquire", "--run", "r1", "--task", "tg", "--base", "mk/r1/integration")["slot"] == 1
        assert mk("acquire", "--run", "r1", "--task", "th", "--base", "mk/r1/integration")["slot"] == 2
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        assert mk("release", "--slot", "2", "--integration", "mk/r1/integration")["ok"]
        for slot in (slot1, slot2):  # ignored inside a tracked dir: the shape the review lost data on
            for rel in shapes:
                write(os.path.join(slot, rel, "prod.sqlite"), "data\n")
        assert all(not s["clean"] for s in mk("list")["slots"]), mk("list")
        out = mk("gc", "--keep", "1")
        assert out["removed"] == [] and [x["slot"] for x in out["skipped"]] == [2], out
        assert out["skipped"][0]["reason"].startswith("dirty: !! ") \
            and "prod.sqlite" in out["skipped"][0]["reason"], out
        d = mk("acquire", "--run", "r1", "--task", "ti", "--base", "mk/r1/integration", code=3)
        assert d["error"] == "slot 1 dirty", d
        for rel in shapes:  # every one of them is named as dirty, unquoted, and still on disk
            assert f"!! {rel}/prod.sqlite" in d["dirty"], (rel, d["dirty"])
            for slot in (slot1, slot2):
                assert os.path.exists(os.path.join(slot, rel, "prod.sqlite")), (slot, rel)
        for slot in (slot1, slot2):  # clean up the ignored fixtures
            for rel in shapes:
                os.unlink(os.path.join(slot, rel, "prod.sqlite"))
        write(os.path.join(integ, ".gitignore"), "node_modules/\n__pycache__/\n/.venv/\n.omc/\nprod.sqlite\n")
        g("-C", integ, "add", ".gitignore"), g("-C", integ, "commit", "-qm", "full ignores")
        assert mk("acquire", "--run", "r1", "--task", "ti", "--base", "mk/r1/integration")["ok"]
        # the plain case must stay clean: an untracked node_modules/ is still a disposable cache dir
        write(os.path.join(slot1, "pkg", "node_modules", "dep", "i.js"), "x\n")
        assert mk("list")["slots"][0]["clean"] is True, mk("list")
        shutil.rmtree(os.path.join(slot1, "pkg", "node_modules"))
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        assert mk("gc", "--keep", "1")["removed"] == [2]  # back to one slot for the rest
        yaml_cap(1, 1)

        # 11h ref names are compared by their stored spelling: on a case-insensitive filesystem
        # `--integration MK/R1/TJ` resolves to the loose refs/heads/mk/r1/tj and rev-parse echoes the
        # alias back, so a name comparison alone would accept it as the task branch
        assert mk("acquire", "--run", "r1", "--task", "tj", "--base", "mk/r1/integration")["slot"] == 1
        write(os.path.join(slot1, "alias.md"), "work\n")
        g("-C", slot1, "add", "-A"), g("-C", slot1, "commit", "-qm", "work"), merge_integration("mk/r1/tj")
        alias = mk("release", "--slot", "1", "--integration", "MK/R1/TJ", code=2)
        assert alias["ok"] is False and "branch" in alias["error"], alias
        assert g("rev-parse", "--verify", "--quiet", "refs/heads/mk/r1/tj").returncode == 0
        assert mk("list")["slots"][0]["state"] == "busy"  # nothing happened
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["branch_deleted"]

        # 12 the pattern can never re-aim a slot, and a slot path is always ours to touch
        other = os.path.join(tmp, "other-1")
        g("worktree", "add", "-q", other, "-b", "userwork", "main")
        write(os.path.join(other, "mine.txt"), "the user's own work\n")
        g("-C", other, "add", "-A"), g("-C", other, "commit", "-qm", "user work")
        write(mkpaths.legacy_yaml(data),
              'repo:\n  lanes: { slots: 2, pattern: "{repo}-lanes/{n}", reuse: true }\n')
        bad = mk("acquire", "--run", "r1", "--task", "tb", "--base", "main", code=2)
        assert bad["ok"] is False and "inside it" in bad["error"], bad
        assert main_head() == "main", main_head()  # the main checkout was never switched
        write(mkpaths.legacy_yaml(data),
              'repo:\n  lanes: { slots: 2, pattern: "../{repo}", reuse: true }\n')
        bad = mk("acquire", "--run", "r1", "--task", "tb", "--base", "main", code=2)
        assert bad["ok"] is False and "{n}" in bad["error"], bad
        assert main_head() == "main", main_head()
        write(mkpaths.legacy_yaml(data),
              'repo:\n  lanes: { slots: 2, pattern: "../other-{n}", reuse: true }\n')
        bad = mk("acquire", "--run", "r1", "--task", "tb", "--base", "main", code=2)
        assert bad["ok"] is False and "pattern now gives" in bad["error"], bad
        assert mk("list")["slots"][0]["path"] == slot1  # the stored path is not rewritten
        assert g("-C", other, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "userwork"
        yaml_cap(2, 2)
        assert mk("acquire", "--run", "r1", "--task", "tb", "--base", "main")["slot"] == 1
        os.makedirs(slot2)  # a directory that is not a slot: git would accept an empty one
        bad = mk("acquire", "--run", "r1", "--task", "tc", "--base", "main", code=2)
        assert bad["ok"] is False and "exists and is not a registered slot" in bad["error"], bad
        assert os.path.isdir(slot2) and not os.listdir(slot2)  # refused, never taken over or emptied
        os.rmdir(slot2)
        assert mk("acquire", "--run", "r1", "--task", "tc", "--base", "main")["slot"] == 2
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]
        # the same guard gc uses: a slot path must be a worktree of THIS repo, never the main checkout
        for path, want in ((os.path.join(tmp, "not-a-worktree"), "not a registered worktree"),
                           (repo, "is the main checkout")):
            try:
                verify_slot_path(9, path, repo)
                raise AssertionError(f"verify_slot_path accepted {path}")
            except Fail as e:
                assert want in str(e), e

        # 12b (L1.2) a symlinked `.claude/skills/mk-builder` pointing outside the slot is
        # unlinked, never followed: the sentinel outside survives, the lane gets a real skill dir.
        # Slot 1 is the free one here (released just above); slot 2 stays busy.
        outside = os.path.join(tmp, "outside-skill")
        os.makedirs(outside)
        write(os.path.join(outside, "SENTINEL"), "do not delete\n")
        link = os.path.join(slot1, ".claude", "skills", "mk-builder")
        # the slot's HEAD predates the `.claude/` gitignore line, so exclude it here or the
        # symlink reads as `?? .claude/` and acquire refuses before the install runs
        ep = g("-C", slot1, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        ep = ep if os.path.isabs(ep) else os.path.join(slot1, ep)
        with open(ep, "a") as f:
            f.write(".claude/\n")
        if os.path.lexists(link):
            if os.path.islink(link) or os.path.isfile(link):
                os.unlink(link)
            else:
                shutil.rmtree(link)
        os.symlink(outside, link)
        st("task", "add", "--root", "--id", "t12b", "--title", "skill symlink", "--run", "r1")
        acq = mk("acquire", "--run", "r1", "--task", "t12b", "--base", "main")
        assert acq["slot"] == 1 and acq["ok"], acq
        assert not os.path.islink(link) and os.path.isdir(link)
        assert open(os.path.join(outside, "SENTINEL")).read() == "do not delete\n"
        assert open(os.path.join(link, "SKILL.md"), "rb").read() == \
            open(os.path.join(os.path.dirname(HERE), "child-skills", "mk-builder", "SKILL.md"), "rb").read()
        assert mk("release", "--slot", "1", "--integration", "mk/r1/integration")["ok"]

        # 13 a rejected lanes_disposable entry narrows the allowlist and is reported
        write(mkpaths.legacy_yaml(data),
              'repo:\n  lanes: { slots: 2, pattern: "../{repo}-mk-{n}", reuse: true }\n'
              'lanes_disposable: ["*"]\n')
        assert mk("release", "--slot", "2", "--integration", "mk/r1/integration")["ok"]
        write(os.path.join(slot2, "prod.sqlite"), "ignored, but not disposable\n")
        w = mk("list")
        assert len(w["warnings"]) == 1 and "not a literal name" in w["warnings"][0], w
        assert next(x for x in w["slots"] if x["slot"] == 2)["clean"] is False  # the wildcard lost
        out = mk("gc", "--keep", "1")
        assert out["removed"] == [] and os.path.exists(os.path.join(slot2, "prod.sqlite")), out
        os.unlink(os.path.join(slot2, "prod.sqlite"))
        yaml_cap(2, 2)
        assert mk("acquire", "--run", "r1", "--task", "tb", "--base", "main")["ok"]

        # bad input never changes state; an existing branch leaves the slot detached and still free
        for bad in (("names", "--run", "r1"), ("names", "--run", "r1", "--task", "t/1"),
                    ("acquire", "--run", "r1", "--task", "t/1", "--base", "main"),
                    ("release", "--slot", "9", "--integration", "main"), ("gc", "--keep", "abc"),
                    ("--lock-timeout", "nan", "gc")):
            assert mk(*bad, code=2)["ok"] is False, bad
        for free in ("1", "2"):
            assert mk("release", "--slot", free, "--integration", "mk/r1/integration")["ok"]
        dup = mk("acquire", "--run", "r1", "--task", "t5", "--base", "main", code=2)  # mk/r1/t5 kept
        assert dup["ok"] is False and "stderr" in dup and "switch" in dup["error"], dup
        assert mk("list")["slots"][0]["state"] == "free"
        assert g("-C", slot1, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0  # detached

        # 14 lanes.lock: a holder outlasts a short timeout, and nothing changes
        assert mk("acquire", "--run", "r1", "--task", "t9", "--base", "main")["slot"] == 1
        assert mk("acquire", "--run", "r1", "--task", "tc", "--base", "main")["slot"] == 2
        before = json.dumps(mk("list")["slots"])
        hold = open(os.path.join(data, "lanes.lock"), "a")
        fcntl.flock(hold, fcntl.LOCK_EX)
        try:
            assert mk("acquire", "--run", "r1", "--task", "td", "--base", "main", "--lock-timeout", "1",
                      code=2)["error"] == "lanes.lock busy"
            assert json.dumps(mk("list")["slots"]) == before  # no state change
            assert mk("list")["slots"][1]["task"] == "tc"  # the registry stays readable while locked
        finally:
            fcntl.flock(hold, fcntl.LOCK_UN)
            hold.close()
        assert mk("acquire", "--run", "r1", "--task", "td", "--base", "main",
                  code=4)["error"] == "all 2 slots busy"
        assert main_head() == "main"  # this test never moved the user's main checkout

        # 15 fixes from the first live run: unknown run ids, task groups, task-id case,
        # slot copies, and the real herdr workspace id

        # an unknown run id is refused before anything is created: no slot, no branch, no lanes entry
        before = json.dumps(mk("list")["slots"])
        bad = mk("acquire", "--run", "plan-link", "--task", "t10", "--base", "main", code=2)
        assert bad["ok"] is False and "plan-link" in bad["error"] and "r1" in bad["error"], bad
        assert bad["known_runs"] == ["r1"], bad
        assert json.dumps(mk("list")["slots"]) == before  # nothing created
        assert g("rev-parse", "--verify", "--quiet", "refs/heads/mk/r1/plan-link",
                 check=False).returncode != 0

        # a task group: one slot, the lane fields on every listed task
        yaml_cap(3, 3)
        st("task", "add", "--root", "--id", "t10", "--title", "ten", "--run", "r1")
        st("task", "add", "--root", "--id", "t11", "--title", "eleven", "--run", "r1")
        grp = mk("acquire", "--run", "r1", "--tasks", "t10,t11", "--base", "main")
        assert (grp["ok"], grp["slot"], grp["created"], grp["tasks"]) == (True, 3, True, ["t10", "t11"]), grp
        slot3 = os.path.join(tmp, "repo-mk-3")
        for t in ("t10", "t11"):
            tt = st("task", "get", t)["task"]
            assert (tt["lane"], tt["branch"], tt["workspace"], tt["slot"]) == (slot3, "mk/r1/t10", "mk:repo:3", "3"), tt
        assert mk("acquire", "--run", "r1", "--task", "t12", "--tasks", "t12", "--base", "main",
                  code=2)["error"] == "--task and --tasks are mutually exclusive"
        assert mk("acquire", "--run", "r1", "--tasks", "t12,,t13", "--base", "main",
                  code=2)["error"] == "acquire needs --task T or --tasks A,B (no empty entries)"

        # task ids keep their case for the mk-state write; only branch and agent names are lowercased
        st("task", "add", "--root", "--id", "T2", "--title", "upper", "--run", "r1")
        yaml_cap(4, 4)
        up = mk("acquire", "--run", "r1", "--task", "T2", "--base", "main")
        assert (up["slot"], up["agent"], up["branch"]) == (4, "t2-build-r1", "mk/r1/t2"), up
        tt = st("task", "get", "T2")["task"]
        assert (tt["lane"], tt["branch"], tt["slot"]) == (os.path.join(tmp, "repo-mk-4"), "mk/r1/t2", "4"), tt
        # the lowercase spelling addresses the same stored task: no second, lowercase task appears
        merge_integration("mk/r1/t2")  # frees the branch name for the lowercase re-acquire
        mk("release", "--slot", "4", "--integration", "mk/r1/integration")
        yaml_cap(5, 5)
        low = mk("acquire", "--run", "r1", "--task", "t2", "--base", "main")
        assert low["slot"] == 4, low  # the lowest free slot, path gone → recreated
        assert "mk-state task set" not in low.get("warning", ""), low  # the stored-case id was written
        # L4 (C4): the store is the record — no runs/<rid>/run.json to read, so the task list
        # comes from the same snapshot resolve_task uses
        ids = [t["id"] for t in ((store_snapshot(data, "r1") or {}).get("run") or {}).get("tasks") or []]
        assert ids.count("T2") == 1 and "t2" not in ids, ids
        tt = st("task", "get", "T2")["task"]
        assert (tt["lane"], tt["slot"]) == (os.path.join(tmp, "repo-mk-4"), "4"), tt

        # lanes.copy: only untracked paths travel — a tracked source is skipped, and what does
        # land in the slot is on the clean-check allowlist (the slot stays clean; .env is
        # gitignored, as in any real project — that is the point of lanes.copy)
        write(os.path.join(repo, "src", "app.js"), "// tracked source\n")
        write(os.path.join(repo, ".gitignore"),
              "node_modules/\n__pycache__/\n/.venv/\n.omc/\nprod.sqlite\n.env\n")
        g("add", "src", ".gitignore"), g("commit", "-qm", "tracked src; .env ignored")
        write(os.path.join(repo, ".env"), "TOKEN=copy\n")
        write(mkpaths.legacy_yaml(data),
              'repo:\n  lanes: { slots: 5, pattern: "../{repo}-mk-{n}", reuse: true, '
              'copy: ["README.md", "src", ".env"] }\n'
              'routing_preferences:\n  max_build_lanes: 5\n')
        st("task", "add", "--root", "--id", "t14", "--title", "fourteen", "--run", "r1")
        cp = mk("acquire", "--run", "r1", "--task", "t14", "--base", "main")
        assert (cp["ok"], cp["slot"], cp["created"]) == (True, 5, True), cp
        slot5 = os.path.join(tmp, "repo-mk-5")
        # untracked, so only the copy brings it into the slot; README.md/src are already there
        # from the worktree checkout (tracked) and were skipped by the copy
        assert open(os.path.join(slot5, ".env")).read() == "TOKEN=copy\n"
        assert cp["warning"].count("lanes copy") == 2, cp  # README.md and src are tracked
        assert "README.md" in cp["warning"] and "'src'" in cp["warning"], cp
        assert next(s for s in mk("list")["slots"] if s["slot"] == 5)["clean"] is True
        # absolute, `..` and symlink-escape sources are skipped before any copy
        os.symlink(tmp, os.path.join(repo, "escape"))
        warns = copy_into_slot({"copy": ["escape", "/abs", "../up"]}, repo, slot5)
        assert len(warns) == 3 and all("lanes copy" in w for w in warns), warns
        assert not os.path.exists(os.path.join(slot5, "escape"))
        os.unlink(os.path.join(repo, "escape"))

        # the workspace id herdr's create response actually carries: the captured real shape
        captured = ('{"id":"cli:worktree:create","result":{"workspace":{"workspace_id":"wW","label":"mk:repo:7"},'
                    '"worktree":{"branch":"mk/r1/t15","open_workspace_id":"wW","path":"repo-mk-7"}}}')
        assert herdr_json_id(captured) == ("wW", None)
        assert herdr_json_id('{"id":"cli:worktree:create","result":{"worktree":{"open_workspace_id":"wX"}}}') == ("wX", None)
        noid = herdr_json_id('{"id":"cli:worktree:create","result":{}}')
        assert noid[0] is None and noid[1]  # the slot exists; only the id capture failed
        # ...and through acquire with a fake herdr: the stored id is the workspace one, never the RPC name
        fake = os.path.join(tmp, "fake-herdr")
        write(fake, "#!/usr/bin/env python3\n"
                    "import sys\n"
                    "sys.stdout.write('{\"id\":\"cli:worktree:create\",\"result\":{\"workspace\":{\"workspace_id\":\"wW\"},"
                    "\"worktree\":{\"open_workspace_id\":\"wW\"}}}')\n")
        os.chmod(fake, 0o755)
        yaml_cap(6, 6)
        st("task", "add", "--root", "--id", "t15", "--title", "fifteen", "--run", "r1")
        h = mk("acquire", "--run", "r1", "--task", "t15", "--base", "main", e=dict(env, MK_HERDR=fake))
        assert (h["ok"], h["slot"], h["workspace_id"]) == (True, 6, "wW"), h
        assert next(s for s in mk("list")["slots"] if s["slot"] == 6)["workspace_id"] == "wW"
        lanes = read_lanes(data)  # the fake made no worktree: drop the record again
        lanes["slots"].pop("6", None)
        write_lanes(data, lanes)

        # 16 F4 on a REUSED slot: copies fill only missing targets, a stale id is repaired
        # from herdr's list — the same rules as on create, nothing existing ever overwritten

        # a reused slot dir + a fake config: a missing target is copied, an existing one is kept
        # as is, and the tracked/escape refusals are exactly the create path's
        reused = os.path.join(tmp, "reused-slot")
        os.makedirs(reused)
        write(os.path.join(reused, ".env"), "TOKEN=old\n")
        write(os.path.join(repo, "gen", "t.d.ts"), "export type T = number\n")
        os.symlink(tmp, os.path.join(repo, "escape"))
        rw = copy_into_slot({"copy": [".env", "gen", "escape", "/abs", "../up"]}, repo, reused)
        assert open(os.path.join(reused, ".env")).read() == "TOKEN=old\n", rw  # never overwritten
        assert open(os.path.join(reused, "gen", "t.d.ts")).read() == "export type T = number\n", rw
        assert len(rw) == 3 and all("lanes copy" in w for w in rw), rw  # escape, /abs, ../up refuse
        assert not os.path.exists(os.path.join(reused, "escape")), rw
        os.unlink(os.path.join(repo, "escape"))

        # ...and through acquire: slot 5 is reused (its path exists), loses its .env, and gets
        # main's current one back; the second reuse keeps the slot's own newer .env untouched
        write(mkpaths.legacy_yaml(data),
              'repo:\n  lanes: { slots: 5, pattern: "../{repo}-mk-{n}", reuse: true, '
              'copy: ["README.md", "src", ".env"] }\n'
              'routing_preferences:\n  max_build_lanes: 5\n')
        st("task", "add", "--root", "--id", "t16", "--title", "sixteen", "--run", "r1")
        # integration must ignore .env too: release parks the slot there, and there the copied
        # .env would read `??` untracked — dirty — instead of ignored-and-disposable
        write(os.path.join(integ, ".gitignore"),
              "node_modules/\n__pycache__/\n/.venv/\n.omc/\nprod.sqlite\n.env\n")
        g("-C", integ, "add", ".gitignore"), g("-C", integ, "commit", "-qm", "ignore .env")
        assert mk("release", "--slot", "5", "--integration", "mk/r1/integration")["ok"]
        os.unlink(os.path.join(slot5, ".env"))
        write(os.path.join(repo, ".env"), "TOKEN=fresh\n")
        ru = mk("acquire", "--run", "r1", "--task", "t16", "--base", "main")
        assert (ru["ok"], ru["slot"], ru["created"]) == (True, 5, False), ru
        assert open(os.path.join(slot5, ".env")).read() == "TOKEN=fresh\n", ru  # missing → copied
        assert ru["warning"].count("lanes copy") == 2, ru  # tracked README.md/src still refuse
        assert next(s for s in mk("list")["slots"] if s["slot"] == 5)["clean"] is True
        st("task", "add", "--root", "--id", "t17", "--title", "seventeen", "--run", "r1")
        assert mk("release", "--slot", "5", "--integration", "mk/r1/integration")["ok"]
        write(os.path.join(repo, ".env"), "TOKEN=newer\n")
        keep = mk("acquire", "--run", "r1", "--task", "t17", "--base", "main")
        assert (keep["ok"], keep["slot"], keep["created"]) == (True, 5, False), keep
        assert open(os.path.join(slot5, ".env")).read() == "TOKEN=fresh\n", keep  # kept, not replaced
        yaml_cap(5, 5)

        # a stale registry id becomes the open_workspace_id for THIS slot's path (wL5, not wL1);
        # a null id on a path herdr's list does not know stays null, and the acquire still succeeds
        def fake_list_herdr(path_ids):
            fake = os.path.join(tmp, "fake-herdr-" + "-".join(w for _, w in path_ids))
            wts = ",".join(f"{{'path': {p!r}, 'open_workspace_id': {w!r}}}" for p, w in path_ids)
            write(fake, "#!/usr/bin/env python3\n"
                        "import json\n"
                        f"print(json.dumps({{'id': 'cli:worktree:list', "
                        f"'result': {{'worktrees': [{wts}]}}}}))\n")
            os.chmod(fake, 0o755)
            return fake

        assert mk("release", "--slot", "5", "--integration", "mk/r1/integration")["ok"]
        lanes = read_lanes(data)
        lanes["slots"]["5"]["workspace_id"] = "cli:worktree:create"  # the stale capture
        write_lanes(data, lanes)
        st("task", "add", "--root", "--id", "t18", "--title", "eighteen", "--run", "r1")
        ws = mk("acquire", "--run", "r1", "--task", "t18", "--base", "main",
                e=dict(env, MK_HERDR=fake_list_herdr([(slot1, "wL1"), (slot5, "wL5")])))
        assert (ws["ok"], ws["slot"], ws["workspace_id"]) == (True, 5, "wL5"), ws
        assert next(s for s in mk("list")["slots"] if s["slot"] == 5)["workspace_id"] == "wL5"
        assert mk("release", "--slot", "5", "--integration", "mk/r1/integration")["ok"]
        lanes = read_lanes(data)
        lanes["slots"]["5"]["workspace_id"] = None
        write_lanes(data, lanes)
        st("task", "add", "--root", "--id", "t19", "--title", "nineteen", "--run", "r1")
        unk = mk("acquire", "--run", "r1", "--task", "t19", "--base", "main",
                 e=dict(env, MK_HERDR=fake_list_herdr([(slot1, "wL1")])))
        assert (unk["ok"], unk["slot"], unk["workspace_id"]) == (True, 5, None), unk
        assert "herdr worktree list" not in unk.get("warning", ""), unk  # unknown path: left silently

        # acquire reports a leftover agent from herdr's `agent list` (report only): one whose cwd is
        # in the slot, or whose workspace is the slot's, under another name than the new round's
        def pane_acquire(tid, body, rc=0):
            assert mk("release", "--slot", "5", "--integration", "mk/r1/integration")["ok"]
            st("task", "add", "--root", "--id", tid, "--title", tid, "--run", "r1")
            fk = os.path.join(tmp, "fake-agents-" + tid)
            write(fk, "#!/usr/bin/env python3\nimport sys\n"
                      f"sys.stdout.write({body!r})\nsys.exit({rc})\n")
            os.chmod(fk, 0o755)
            return mk("acquire", "--run", "r1", "--task", tid, "--base", "main", e=dict(env, MK_HERDR=fk))

        def agents_json(*es):
            return json.dumps({"id": "cli:agent:list", "result": {"agents": [
                dict({"agent": "claude", "agent_status": "done", "pane_id": "wP5:p1"}, **e) for e in es]}}) + "\n"

        lanes = read_lanes(data)
        lanes["slots"]["5"]["workspace_id"] = "wP5"
        write_lanes(data, lanes)
        pa = pane_acquire("pa1", agents_json({"cwd": os.path.join(slot5, "sub"), "name": "old-build-r1",
                                              "workspace_id": "wX"}))
        assert (pa["ok"], pa["slot"], pa["created"]) == (True, 5, False), pa
        assert pa["pane_agent"] == {"name": "old-build-r1", "agent": "claude", "status": "done",
                                    "pane_id": "wP5:p1"}, pa
        assert "fresh session" in pa["pane_agent_note"], pa
        pa = pane_acquire("pa2", agents_json({"cwd": "/nowhere", "workspace_id": "wP5"}))  # by workspace; no name
        assert pa["ok"] and pa["pane_agent"]["name"] is None, pa
        pa = pane_acquire("pa3", agents_json({"cwd": slot5, "name": "pa3-build-r1"}))  # same name: this round's own
        assert pa["ok"] and "pane_agent" not in pa and "pane_agent_note" not in pa, pa
        pa = pane_acquire("pa4", agents_json({"cwd": "/nowhere", "name": "x", "workspace_id": "wOther"}))
        assert pa["ok"] and "pane_agent" not in pa, pa  # an agent elsewhere
        pa = pane_acquire("pa5", agents_json({"cwd": slot5, "name": "x"}), rc=1)  # herdr exit 1
        assert pa["ok"] and "pane_agent" not in pa and "warning" not in pa, pa
        pa = pane_acquire("pa6", "not json\n")
        assert pa["ok"] and "pane_agent" not in pa and "warning" not in pa, pa
        lanes = read_lanes(data)
        lanes["slots"]["5"]["workspace_id"] = None
        write_lanes(data, lanes)

        # 12 acquire without --base uses repo.integration_branch from the yaml; an explicit --base
        # still wins; a bad yaml value still fails check_ref
        assert mk("release", "--slot", "5", "--integration", "mk/r1/integration")["ok"]
        write(mkpaths.legacy_yaml(data),
              'repo:\n  integration_branch: mk/r1/integration\n  lanes: { slots: 5, reuse: true }\n')
        st("task", "add", "--root", "--id", "t20", "--title", "twenty", "--run", "r1")
        acq_nobase = mk("acquire", "--run", "r1", "--task", "t20")
        assert (acq_nobase["ok"], acq_nobase["slot"]) == (True, 5), acq_nobase
        integ_sha = g("rev-parse", "--verify", "mk/r1/integration^{commit}").stdout.strip()
        main_sha = g("rev-parse", "--verify", "main^{commit}").stdout.strip()
        assert integ_sha != main_sha  # integration branch has distinct commits from main
        assert g("-C", slot5, "rev-parse", "HEAD^{commit}").stdout.strip() == integ_sha

        # an explicit --base still wins
        assert mk("release", "--slot", "5", "--integration", "mk/r1/integration")["ok"]
        st("task", "add", "--root", "--id", "t21", "--title", "twenty-one", "--run", "r1")
        acq_exp = mk("acquire", "--run", "r1", "--task", "t21", "--base", "main")
        assert (acq_exp["ok"], acq_exp["slot"]) == (True, 5), acq_exp
        assert g("-C", slot5, "rev-parse", "HEAD^{commit}").stdout.strip() == main_sha

        # a bad yaml value still fails check_ref
        assert mk("release", "--slot", "5", "--integration", "mk/r1/integration")["ok"]
        write(mkpaths.legacy_yaml(data),
              'repo:\n  integration_branch: nosuchbranch\n  lanes: { slots: 5, reuse: true }\n')
        st("task", "add", "--root", "--id", "t22", "--title", "twenty-two", "--run", "r1")
        bad_yaml = mk("acquire", "--run", "r1", "--task", "t22", code=2)
        assert "does not resolve to a commit" in bad_yaml["error"]
        assert bad_yaml["ok"] is False

        # absent from yaml falls back to schema default "main"
        write(mkpaths.legacy_yaml(data),
              'repo:\n  lanes: { slots: 5, reuse: true }\n')
        st("task", "add", "--root", "--id", "t23", "--title", "twenty-three", "--run", "r1")
        acq_dflt = mk("acquire", "--run", "r1", "--task", "t23")
        assert (acq_dflt["ok"], acq_dflt["slot"]) == (True, 5), acq_dflt
        assert g("-C", slot5, "rev-parse", "HEAD^{commit}").stdout.strip() == main_sha
        assert mk("release", "--slot", "5", "--integration", "mk/r1/integration")["ok"]

        # ---- store parity (D5B): the same script against two independent stores. L4 (C4) dropped
        # file mode, so there is no "file dir without mk.db" left to compare against: mk-state
        # writes a store into every data dir it is asked about, and what this replays is that two
        # separate stores answer the same commands with the same lane semantics. ----

        def norm(obj):
            """Outputs equal except paths: temp dirs differ between the two data dirs."""
            if isinstance(obj, dict):
                return {k: norm(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [norm(v) for v in obj]
            if isinstance(obj, str) and (obj.startswith(tmp) or "/repo-mk-" in obj or obj.endswith("/repo")):
                return "<path>"
            return obj

        # the scripted sequence both modes replay: same commands, same order
        file_script = [
            ["list"],
            ["acquire", "--run", "r1", "--task", "s1", "--base", "main"],
            ["list"],
            ["release", "--slot", "1", "--integration", "mk/r1/integration"],
            ["list"],
            ["gc", "--keep", "1"],
            ["list"],
        ]
        # distinct patterns: the two acquires must not fight over one worktree path
        def parity_yaml(tag):
            return (f'repo:\n  lanes: {{ slots: 2, pattern: "../{{repo}}-{tag}-{{n}}", reuse: true }}\n'
                    "routing_preferences:\n  max_build_lanes: 2\n")

        # Two independent store-backed dirs, seeded from the same data dir so they start equal
        # (L4: every data dir carries a store; there is no storeless side to compare against).
        # lanes.json is the one thing left out, so each side builds its own slot pool.
        import shutil as _sh

        def seed(ddir, tag):
            os.makedirs(ddir)
            for name in os.listdir(data):
                if name == mkpaths.LANES_NAME:
                    continue
                src, dst = os.path.join(data, name), os.path.join(ddir, name)
                if os.path.isdir(src):
                    _sh.copytree(src, dst)
                else:
                    _sh.copy2(src, dst)
            write(mkpaths.legacy_yaml(ddir), parity_yaml(tag))
            # the copied store carries the source run's slot rows: empty the pool so each side
            # builds its own, exactly as the old file-mode side started with no lanes.json
            con = mkdb.open_project(mkpaths.home(), data_dir=ddir)
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute("DELETE FROM lane_slots")
                con.execute("COMMIT")
            finally:
                con.close()

        store_data = os.path.join(tmp, "data-store")
        seed(store_data, "store")
        # open the store the only allowed way, under a temp home, so the file exists before commands
        home = os.path.join(tmp, "mk-home")
        os.makedirs(home)
        env_store = dict(env, MK_HOME=home, MATRIXKODER_HOME=home)
        con = mkdb.open_project(home, data_dir=store_data)
        con.close()
        assert os.path.isfile(mkpaths.db_in(store_data))

        def mk_at(ddir, eenv, *args, code=0):
            r = subprocess.run([sys.executable, self_path, *args, "--data", ddir], capture_output=True,
                               text=True, env=eenv, cwd=repo)
            lines = r.stdout.strip().splitlines()
            assert len(lines) == 1, (args, r.stdout, r.stderr)
            assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
            return json.loads(lines[0])

        # parity: two independent stores, same commands, outputs equal except paths
        file_data = os.path.join(tmp, "data-file")
        seed(file_data, "file")
        def mapped(args, task):
            return [task if a == "s1" else a for a in args]

        for args in file_script:
            # release/gc need a slot that exists. Distinct task ids so the two modes do not
            # both try to create the branch mk/r1/s1. Outputs compared after normalising paths.
            fa = mk_at(file_data, env, *mapped(args, "s1"), code=0)
            sa = mk_at(store_data, env_store, *mapped(args, "s2"), code=0)
            # slot semantics are the comparable part: same keys, same non-path values (each store
            # owns its own task rows, so the task-set warnings are never compared)
            def core(obj):
                if isinstance(obj, dict) and "slots" in obj:
                    return {"cap": obj.get("cap"), "slots": [
                        {k: norm(s[k]) for k in ("slot", "state", "clean")} for s in obj["slots"]]}
                if isinstance(obj, dict):
                    keep = ("ok", "slot", "state", "created", "install_needed", "branch_deleted",
                            "removed", "skipped")
                    return {k: norm(obj[k]) for k in keep if k in obj}
                return norm(obj)
            assert core(fa) == core(sa), (args, fa, sa)
        assert os.path.isfile(mkpaths.db_in(file_data))  # both sides are stores now

        # exit 4 when every slot is busy — its own data dir, so no slot path from the parity pattern
        # cap 1: one acquire succeeds, the next is exit 4. Need a known task.
        st("task", "add", "--root", "--id", "storeA", "--title", "store A", "--run", "r1")
        st("task", "add", "--root", "--id", "storeB", "--title", "store B", "--run", "r1")
        st("task", "add", "--root", "--id", "parA", "--title", "par A", "--run", "r1")
        st("task", "add", "--root", "--id", "parB", "--title", "par B", "--run", "r1")

        def seed_cap(ddir, yaml_text):
            """A fresh store-backed dir carrying the run r1 mk-state just wrote, with an empty lane
            pool. L4: the run lives in mk.db, so a runs/ copy alone would leave it unknown."""
            _sh.rmtree(ddir, ignore_errors=True)
            os.makedirs(ddir)
            for name in os.listdir(data):
                if name == mkpaths.LANES_NAME:
                    continue
                src, dst = os.path.join(data, name), os.path.join(ddir, name)
                if os.path.isdir(src):
                    _sh.copytree(src, dst)
                else:
                    _sh.copy2(src, dst)
            write(mkpaths.legacy_yaml(ddir), yaml_text)
            con = mkdb.open_project(mkpaths.home(), data_dir=ddir)
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute("DELETE FROM lane_slots")
                con.execute("COMMIT")
            finally:
                con.close()

        cap_data = os.path.join(tmp, "data-cap")
        seed_cap(cap_data, 'repo:\n  lanes: { slots: 1, pattern: "../{repo}-cap-{n}", reuse: true }\n'
                           "routing_preferences:\n  max_build_lanes: 1\n")
        store_data_cap = cap_data
        one = mk_at(store_data_cap, env_store, "acquire", "--run", "r1", "--task", "storeA", "--base", "main")
        assert one["ok"] and one["slot"] == 1, one
        busy = mk_at(store_data_cap, env_store, "acquire", "--run", "r1", "--task", "storeB",
                     "--base", "main", code=4)
        assert busy["ok"] is False and "busy" in busy["error"], busy
        mk_at(store_data_cap, env_store, "release", "--slot", "1", "--integration", "mk/r1/integration")

        # two parallel acquires never get the same slot (cap 2), own data dir
        par_data = os.path.join(tmp, "data-par")
        seed_cap(par_data, 'repo:\n  lanes: { slots: 2, pattern: "../{repo}-par-{n}", reuse: true }\n'
                           "routing_preferences:\n  max_build_lanes: 2\n")
        env_store_par = dict(env_store)
        import threading
        got = {}

        def grab(task, key):
            r = subprocess.run([sys.executable, self_path, "acquire", "--run", "r1", "--task", task,
                                "--base", "main", "--data", par_data], capture_output=True,
                               text=True, env=env_store_par, cwd=repo)
            got[key] = (r.returncode, r.stdout.strip())

        t1 = threading.Thread(target=grab, args=("parA", "a"))
        t2 = threading.Thread(target=grab, args=("parB", "b"))
        t1.start(); t2.start(); t1.join(); t2.join()
        outs = []
        for key in ("a", "b"):
            code, line = got[key]
            assert code == 0, (key, code, line)
            outs.append(json.loads(line))
        assert outs[0]["slot"] != outs[1]["slot"], outs
        for o in outs:
            mk_at(par_data, env_store, "release", "--slot", str(o["slot"]),
                  "--integration", "mk/r1/integration")

        # L10.5: the registry is the project DB only. A store-backed dir never gets a lanes.json, and
        # carry-legacy copies a pre-switch registry once: it fills only ABSENT slots, never overwrites a
        # newer C2 row, refuses while any slot is busy, and --dry-run writes nothing.
        cl_slug, cl_legacy = "carry-proj", os.path.join(tmp, "carry-legacy")
        cl_c2 = os.path.join(home, "projects", cl_slug)
        os.makedirs(cl_legacy)

        def slot_row(slot, state, path, updated, run=None, task=None):
            return (slot, path, state, run, task, "mk:repo:" + slot, None, None, None, updated)
        ins = "INSERT INTO lane_slots(" + ", ".join(SLOT_COLS) + ") VALUES (?,?,?,?,?,?,?,?,?,?)"
        lc = mkdb.open_project(home, data_dir=cl_legacy, legacy=True)  # the unmigrated, literal store
        try:
            lc.execute("BEGIN IMMEDIATE")
            lc.execute(ins, slot_row("1", "free", slot1, "2026-10-01T00:00:00Z"))
            lc.execute(ins, slot_row("2", "busy", slot2, "2026-10-01T00:00:01Z", "r1", "oldtask"))
            lc.execute("COMMIT")
        finally:
            lc.close()
        mk_state.write_json(mkpaths.lanes_file(cl_legacy), {"schema": 1, "slots": {
            "3": {"path": slot1 + "-3", "workspace": "mk:repo:3", "workspace_id": None, "state": "free",
                  "run": None, "task": None, "branch": None, "lock_hash": None, "updated": "2026-10-01T00:00:02Z"}}})
        legacy_json = open(mkpaths.lanes_file(cl_legacy), "rb").read()
        root = mkdb.open_root(home)
        try:
            root.execute("CREATE TABLE IF NOT EXISTS projects("
                         "slug TEXT PRIMARY KEY, name TEXT, repo TEXT, data_dir TEXT, mode TEXT,"
                         " created TEXT, last_activity TEXT, db_path TEXT)")
            root.execute("INSERT OR REPLACE INTO projects(slug, name, repo, data_dir, mode, db_path)"
                         " VALUES (?,?,?,?,?,?)",
                         (cl_slug, cl_slug, os.path.join(tmp, "carry-repo"), cl_legacy, "home",
                          os.path.join(cl_c2, os.path.basename(mkpaths.project_db(data=cl_legacy, legacy=True)))))
            root.commit()
        finally:
            root.close()
        mkpaths._MIGRATED_MEMO = None
        cc = mkdb.open_project(home, data_dir=cl_legacy)  # now the C2 store
        try:
            cc.execute("BEGIN IMMEDIATE")
            cc.execute(ins, slot_row("1", "free", slot1 + "-new", "2026-10-16T10:00:00Z"))  # the NEWER registry
            cc.execute("COMMIT")
        finally:
            cc.close()

        def c2_rows():
            con = mkdb.open_project(home, data_dir=cl_legacy)
            try:
                return {r["slot"]: dict(r) for r in con.execute("SELECT * FROM lane_slots")}
            finally:
                con.close()
        before_rows = c2_rows()
        # refused while a slot is busy in the legacy store: nothing written, even with the rest absent
        ref = mk_at(cl_legacy, env_store, "carry-legacy", code=2)
        assert ref["ok"] is False and ref["refused"] == ["source:2"] and c2_rows() == before_rows, ref
        lc = mkdb.open_project(home, data_dir=cl_legacy, legacy=True)
        try:
            lc.execute("BEGIN IMMEDIATE")
            lc.execute("UPDATE lane_slots SET state='free', run=NULL, task=NULL WHERE slot='2'")
            lc.execute("COMMIT")
        finally:
            lc.close()
        # a busy slot in the target refuses too
        cc = mkdb.open_project(home, data_dir=cl_legacy)
        try:
            cc.execute("BEGIN IMMEDIATE")
            cc.execute("UPDATE lane_slots SET state='busy' WHERE slot='1'")
            cc.execute("COMMIT")
        finally:
            cc.close()
        ref = mk_at(cl_legacy, env_store, "carry-legacy", code=2)
        assert ref["refused"] == ["target:1"], ref
        cc = mkdb.open_project(home, data_dir=cl_legacy)
        try:
            cc.execute("BEGIN IMMEDIATE")
            cc.execute("UPDATE lane_slots SET state='free' WHERE slot='1'")
            cc.execute("COMMIT")
        finally:
            cc.close()
        before_rows = c2_rows()
        # --dry-run: reports, writes nothing
        dry = mk_at(cl_legacy, env_store, "carry-legacy", "--dry-run")
        assert dry["dry_run"] is True and dry["carried"] == ["2", "3"] and dry["skipped_newer"] == ["1"], dry
        assert c2_rows() == before_rows
        # the real carry: slots 2 (legacy store) and 3 (legacy lanes.json) arrive, newer slot 1 is untouched
        got = mk_at(cl_legacy, env_store, "carry-legacy")
        assert got["carried"] == ["2", "3"] and got["skipped_newer"] == ["1"] and got["refused"] == [], got
        rows = c2_rows()
        assert sorted(rows) == ["1", "2", "3"] and rows["1"] == before_rows["1"], rows
        assert rows["1"]["lane_path"] == slot1 + "-new" and rows["2"]["lane_path"] == slot2, rows
        assert rows["3"]["lane_path"] == slot1 + "-3", rows
        # idempotent: a second carry changes nothing
        again = mk_at(cl_legacy, env_store, "carry-legacy")
        assert again["carried"] == [] and again["skipped_newer"] == ["1", "2", "3"], again
        assert c2_rows() == rows
        # the legacy file is read, never written; no lanes.json appeared in the C2 folder
        assert open(mkpaths.lanes_file(cl_legacy), "rb").read() == legacy_json
        assert not os.path.exists(mkpaths.lanes_file(cl_c2))
        # acquire/release/status work off the table alone
        listed = mk_at(cl_legacy, env_store, "list")
        assert [s["slot"] for s in listed["slots"]] == [1, 2, 3], listed
        assert open(mkpaths.lanes_file(cl_legacy), "rb").read() == legacy_json
        mkpaths._MIGRATED_MEMO = None
        # L4 (C4): there is no file-mode data dir left — every dir mk-state is asked about gets a
        # store, so `data` above carries mk.db like any other, and that is the expected state
        assert os.path.isfile(mkpaths.db_in(data))

        # L1 in store mode: ignored `.omc` state alone leaves the slot releasable, a modified
        # tracked plan still refuses, and the outside skill symlink is not followed.
        st("task", "add", "--root", "--id", "omcs", "--title", "omc store", "--run", "r1")
        st("task", "add", "--root", "--id", "omcs2", "--title", "omc store 2", "--run", "r1")
        omc_data = os.path.join(tmp, "data-omc-store")
        seed_cap(omc_data, 'repo:\n  lanes: { slots: 2, pattern: "../{repo}-omc-{n}", reuse: true }\n'
                           "routing_preferences:\n  max_build_lanes: 2\n")
        got = mk_at(omc_data, env_store, "acquire", "--run", "r1", "--task", "omcs", "--base", "main")
        assert got["ok"] and got["slot"] == 1, got
        omc_slot = got["path"]
        for rel in ("project-memory.json", os.path.join("sessions", "s.json"),
                    os.path.join("state", "x")):
            write(os.path.join(omc_slot, ".omc", rel), "{}\n")
        # the base fixture already tracks src/x/keep.txt, so the nested .omc is named
        write(os.path.join(omc_slot, "src", "x", ".omc", "state", "x"), "x\n")
        assert mk_at(omc_data, env_store, "list")["slots"][0]["clean"] is True
        assert mk_at(omc_data, env_store, "release", "--slot", "1",
                     "--integration", "mk/r1/integration")["ok"]
        assert os.path.isfile(os.path.join(omc_slot, ".omc", "project-memory.json"))
        # the release switched onto the tip that tracks `.omc/plans/x.md`, so the ignored
        # siblings inside that tracked tree are user data again: clear them, then a modified
        # tracked plan still refuses
        for rel in ("project-memory.json", os.path.join("sessions", "s.json"),
                    os.path.join("state", "x")):
            os.unlink(os.path.join(omc_slot, ".omc", rel))
        os.removedirs(os.path.join(omc_slot, ".omc", "sessions"))
        os.removedirs(os.path.join(omc_slot, ".omc", "state"))
        write(os.path.join(omc_slot, ".omc", "plans", "x.md"), "tracked plan\nedited\n")
        refused = mk_at(omc_data, env_store, "release", "--slot", "1",
                        "--integration", "mk/r1/integration", code=3)
        assert refused["error"] == "slot 1 dirty", refused
        assert any(".omc/plans/x.md" in line and not line.startswith("!!") for line in refused["dirty"]), refused
        write(os.path.join(omc_slot, ".omc", "plans", "x.md"), "tracked plan\n")
        assert mk_at(omc_data, env_store, "release", "--slot", "1",
                     "--integration", "mk/r1/integration")["ok"]
        outside_s = os.path.join(tmp, "outside-skill-store")
        os.makedirs(outside_s)
        write(os.path.join(outside_s, "SENTINEL"), "do not delete\n")
        link_s = os.path.join(omc_slot, ".claude", "skills", "mk-builder")
        if os.path.lexists(link_s):
            if os.path.islink(link_s) or os.path.isfile(link_s):
                os.unlink(link_s)
            else:
                shutil.rmtree(link_s)
        os.symlink(outside_s, link_s)
        # L4: the run lives in the store, so the new task must be written there (a runs/ copy would
        # leave 'syms' unknown); the existing lane pool and its slot 1 registration are kept
        r = subprocess.run([sys.executable, os.path.join(HERE, "mk-state.py"), "--data", omc_data,
                            "task", "add", "--root", "--id", "syms", "--title", "symlink store", "--run", "r1"],
                           capture_output=True, text=True, env=env, cwd=repo)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        got = mk_at(omc_data, env_store, "acquire", "--run", "r1", "--task", "syms", "--base", "main")
        assert got["ok"] and got["slot"] == 1, got
        assert not os.path.islink(link_s) and os.path.isdir(link_s)
        assert open(os.path.join(outside_s, "SENTINEL")).read() == "do not delete\n"
        assert mk_at(omc_data, env_store, "release", "--slot", "1",
                     "--integration", "mk/r1/integration")["ok"]

        # L1 (.omc/state): the HUD cache is disposable even while `.omc/plans/` is tracked, at any
        # depth; an ignored sibling in the tracked plans and a modified tracked file stay dirty.
        hud = os.path.join(tmp, "hud-repo")
        os.makedirs(os.path.join(hud, ".omc", "plans"))
        gh = lambda *a: g("-c", "user.name=t", "-c", "user.email=t@t", *a, cwd=hud)
        gh("init", "-q")
        write(os.path.join(hud, ".gitignore"), ".omc/state/\n.omc/plans/x.md\n")
        write(os.path.join(hud, ".omc", "plans", "p.md"), "plan\n")
        gh("add", ".gitignore", ".omc/plans/p.md")
        gh("commit", "-q", "-m", "init")
        hud_cfg = {"disposable": [], "copy": []}
        write(os.path.join(hud, ".omc", "state", "hud.json"), "{}\n")
        write(os.path.join(hud, ".omc", "state", "sessions", "x", "hud-state.json"), "{}\n")
        assert clean_state(hud, hud_cfg)[0], clean_state(hud, hud_cfg)
        write(os.path.join(hud, ".omc", "plans", "x.md"), "ignored sibling\n")
        ok, dirty = clean_state(hud, hud_cfg)
        assert not ok and dirty == ["!! .omc/plans/x.md"], dirty
        os.unlink(os.path.join(hud, ".omc", "plans", "x.md"))
        write(os.path.join(hud, ".omc", "plans", "p.md"), "plan\nedited\n")
        ok, dirty = clean_state(hud, hud_cfg)
        assert not ok and any(".omc/plans/p.md" in line for line in dirty), dirty

        # A status poll must never take the index lock: the argv git is asked for carries
        # --no-optional-locks. A repo of its own, so the assertion does not depend on what the
        # cases above left behind, and the runner is injected so the assertion is on that argv
        # rather than on whether this git build happens to lock anything.
        lock_repo = os.path.join(tmp, "lock-repo")
        os.makedirs(lock_repo)
        gl = lambda *a: g(*a, cwd=lock_repo)
        gl("init", "-q")
        seen_argv = []
        real_run_cmd = globals()["run_cmd"]
        try:
            globals()["run_cmd"] = lambda cmd, timeout=GIT_TIMEOUT: (
                seen_argv.append(list(cmd)) or real_run_cmd(cmd, timeout))
            assert clean_state(lock_repo, {"disposable": [], "copy": []})[0]
        finally:
            globals()["run_cmd"] = real_run_cmd
        status_argv = [a for a in seen_argv if "status" in a]
        assert status_argv and all("--no-optional-locks" in a for a in status_argv), status_argv

        # status: facts from git + status file + herdr, no agent asked (pure helpers, then real git)
        assert parse_ctx_pct("x 465.4K (44%) · $1\n 12K (7%)") == 7 and parse_ctx_pct("nothing") is None
        assert strip_tui("  ┃\n  ┃  hello\n  ╹▀▀▀▀\n   ■■■⬝⬝ \nplain  \n\n", 12) == ["hello", "plain"]
        assert strip_tui("a\nb\nc", 2) == ["b", "c"]
        sdata, sl = os.path.join(tmp, "sdata"), os.path.join(tmp, "status-lane")
        os.makedirs(sdata)
        g("worktree", "add", "-q", "-b", "mk/s1/t1", sl, "main")
        write(os.path.join(sl, "new.txt"), "ahead\n"), g("add", "new.txt", cwd=sl)
        g("commit", "-qm", "lane commit", cwd=sl)
        write(os.path.join(sl, "README.md"), "base\nedited\n")           # uncommitted, tracked
        write(os.path.join(sl, ".matrixkoder-scratch"), "x\n")            # scratch: excluded
        write(os.path.join(sl, ".matrixkoder-status.json"), json.dumps(
            {"state": "working", "round": 1, "role": "builder", "step": "S1.2 did it", "ctx_pct": None,
             "updated_at": time.time() - 180}))
        write(os.path.join(sl, ".matrixkoder-report-r1.md"), "r\n")
        # L10.5: the registry rows are the fixture (a store created later no longer imports lanes.json)
        sc = mkdb.open_project(lane_home, data_dir=sdata)
        try:
            sc.execute("BEGIN IMMEDIATE")
            for rec in (("1", sl, "busy", "s1", "t1", "w", "wS", "mk/s1/t1", None, "x"),
                        ("2", os.path.join(tmp, "gone"), "busy", "s1", "t2", "w", None, None, None, "x"),
                        ("3", sl, "free", None, None, "w", None, None, None, "x")):
                sc.execute(ins, rec)
            sc.execute("COMMIT")
        finally:
            sc.close()
        sfake = os.path.join(tmp, "fake-status-herdr")
        write(sfake, "#!/usr/bin/env python3\nimport json, sys\n"
              "if sys.argv[2] == 'list':\n"
              "    print(json.dumps({'result': {'agents': [{'pane_id': 'wS:p1', 'cwd': SLOT, 'workspace_id': 'wS'}]}}))\n"
              "elif sys.argv[2] == 'get':\n"
              "    print(json.dumps({'result': {'agent': {'agent': 'opencode', 'agent_status': 'working'}}}))\n"
              "else:\n"
              "    print('  ┃  doing it\\n  ╹▀▀▀\\n  ■■⬝⬝ esc interrupt   470.9K (45%) · $1')\n".replace("SLOT", repr(sl)))
        os.chmod(sfake, 0o755)
        senv = dict(env, MK_HERDR=sfake)
        got = subprocess.run([sys.executable, self_path, "status", "--json", "--data", sdata],
                             capture_output=True, text=True, env=senv, cwd=repo)
        assert got.returncode == 0, got
        sj = json.loads(got.stdout)
        assert sj["ok"] and [r["slot"] for r in sj["lanes"]] == [1, 2], sj  # busy slots only
        r1, r2 = sj["lanes"]
        assert r1["commits_ahead"] == 1 and r1["modified"] == [" M README.md"], r1
        assert r1["step"] == "S1.2 did it" and 2.5 < r1["step_age_min"] < 4, r1
        assert r1["report_present"] is True and r1["agent_state"] == "working", r1
        assert r1["agent"] == "opencode" and r1["agent_status"] == "working" and r1["pane"] == "wS:p1", r1
        assert r1["pane_tail"] == ["doing it", "esc interrupt   470.9K (45%) · $1"] or r1["pane_tail"][0] == "doing it", r1
        assert r1["ctx_pct"] == 45 and r1["task_error"], r1  # no such run in sdata: error field, not a crash
        assert r2["git_error"] and r2["step"] is None and r2["round"] is None, r2  # missing path + status file
        os.unlink(os.path.join(sl, ".matrixkoder-status.json"))
        got = subprocess.run([sys.executable, self_path, "status", "--slot", "1", "--data", sdata],
                             capture_output=True, text=True, env=dict(env, MK_HERDR="none"), cwd=repo)
        assert got.returncode == 0 and "slot 1" in got.stdout and "herdr disabled" in got.stdout, got
        got = subprocess.run([sys.executable, self_path, "status", "--slot", "1", "--json", "--data", sdata],
                             capture_output=True, text=True, env=dict(env, MK_HERDR="none"), cwd=repo)
        nf = json.loads(got.stdout)
        assert nf["ok"] and nf["lanes"][0]["step"] is None and nf["lanes"][0]["commits_ahead"] == 1, nf

        # ---- L5: run/task resolution reads the project store; the run files are the fallback ----
        # A store-backed project, built the way mk-state builds one: the files first, then
        # `store enable` imports them and republishes the views. Every resolution assertion below
        # holds with those file views present AND with <data>/runs deleted outright — the post-L4
        # world, where mk-state writes none of them.
        rsd = os.path.join(tmp, "data-resolve-store")
        os.makedirs(rsd)
        write(mkpaths.legacy_yaml(rsd),
              'repo:\n  lanes: { slots: 1, pattern: "../{repo}-res-{n}", reuse: true }\n')

        def st_at(ddir, *args, code=0):
            r = subprocess.run([sys.executable, os.path.join(HERE, "mk-state.py"), *args,
                                "--data", ddir], capture_output=True, text=True, env=env, cwd=repo)
            assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
            return json.loads(r.stdout.strip()) if r.stdout.strip() else {}

        # a run id is slug-shaped (lowercase): `store enable` refuses any other
        st_at(rsd, "run", "init", "--id", "rs1", "--feature", "F", "--branch", "b")
        st_at(rsd, "task", "add", "--root", "--id", "Rt1", "--title", "one")
        st_at(rsd, "task", "add", "--root", "--id", "Rt2", "--title", "two")
        st_at(rsd, "task", "add", "--root", "--id", "Rt3", "--title", "three")
        st_at(rsd, "store", "enable")
        assert os.path.isfile(mkpaths.db_in(rsd)), "store enable wrote no mk.db"

        def resolutions(ddir):
            """Every answer run_ids/resolve_run/resolve_task give for the store fixture, as one dict."""
            return {"ids": run_ids(ddir),
                    "exact": resolve_run(ddir, "rs1"), "upper": resolve_run(ddir, "RS1"),
                    "mixed": resolve_run(ddir, "Rs1"), "unknown": resolve_run(ddir, "nope"),
                    "task_exact": resolve_task(ddir, "rs1", "Rt1"),
                    "task_upper": resolve_task(ddir, "rs1", "RT1"),
                    "task_lower": resolve_task(ddir, "rs1", "rt1"),
                    "task_other": resolve_task(ddir, "rs1", "RT2"),
                    "task_unknown": resolve_task(ddir, "rs1", "nope"),
                    "task_no_run": resolve_task(ddir, "nosuch", "rt1")}

        want = {"ids": ["rs1"], "exact": "rs1", "upper": "rs1", "mixed": "rs1", "unknown": None,
                "task_exact": "Rt1", "task_upper": "Rt1", "task_lower": "Rt1", "task_other": "Rt2",
                "task_unknown": "nope", "task_no_run": "rt1"}
        assert os.path.isfile(mkpaths.db_in(rsd)), "store enable wrote no mk.db"
        # (b) L4 (C4): store enable writes NO run.json / index.json — the store is the only
        # record. The resolutions below are read straight off it.
        assert not os.path.exists(mkpaths.run_doc(rsd, "rs1"))
        assert not os.path.exists(mkpaths.runs_index(rsd))
        assert resolutions(rsd) == want, resolutions(rsd)
        # (d) an unknown run still resolves to None, and acquire refuses naming the store's runs
        assert resolve_run(rsd, "nope") is None
        bad = mk_at(rsd, env, "acquire", "--run", "nope", "--task", "Rt1", "--base", "main", code=2)
        assert bad["ok"] is False and "unknown run 'nope'" in bad["error"], bad
        assert bad["known_runs"] == ["rs1"], bad
        assert not os.path.exists(mkpaths.legacy_lanes(rsd)), "a refused acquire wrote lanes.json"
        # (c) the whole acquire off the store, with both ids typed in the wrong case: the writes
        # must carry the stored spelling, which only a store-backed resolve can produce
        acq = mk_at(rsd, env, "acquire", "--run", "RS1", "--task", "rt1", "--base", "main")
        assert acq["ok"] and acq["slot"] == 1, acq
        assert "mk-state task set" not in (acq.get("warning") or ""), acq
        con = mkdb.open_project(lane_home, data_dir=rsd)
        try:
            row = dict(con.execute("SELECT id, lane, slot FROM tasks"
                                   " WHERE run='rs1' AND id='Rt1'").fetchone())
        finally:
            con.close()
        assert row["id"] == "Rt1" and row["lane"] and row["slot"] == "1", row
        assert mk_at(rsd, env, "release", "--slot", "1",
                     "--integration", "mk/r1/integration")["ok"]
        # (a) L4 (C4): mk-state writes no run.json / index.json, so there is nothing for a reader
        # to fall back to — the store is the only record, and the resolutions hold anyway. (If a
        # legacy file view ever exists, delete it: the store must answer without it.)
        for _p in (mkpaths.runs_index(rsd), mkpaths.run_doc(rsd, "rs1")):
            if os.path.exists(_p):
                os.unlink(_p)
        assert not os.path.exists(mkpaths.run_doc(rsd, "rs1"))
        assert not os.path.exists(mkpaths.runs_index(rsd))
        assert resolutions(rsd) == want, resolutions(rsd)
        # and an acquire still resolves both ids off the store with neither file present
        acq2 = mk_at(rsd, env, "acquire", "--run", "Rs1", "--task", "rt2", "--base", "main")
        assert acq2["ok"] and acq2["slot"] == 1, acq2
        assert "mk-state task set" not in (acq2.get("warning") or ""), acq2
        con = mkdb.open_project(lane_home, data_dir=rsd)
        try:
            row = dict(con.execute("SELECT id, lane, slot FROM tasks"
                                   " WHERE run='rs1' AND id='Rt2'").fetchone())
        finally:
            con.close()
        assert row["id"] == "Rt2" and row["lane"] and row["slot"] == "1", row
        assert mk_at(rsd, env, "release", "--slot", "1",
                     "--integration", "mk/r1/integration")["ok"]
        # the same, with no MK home in the environment at all: a temp $HOME and nothing set
        bare = {k: v for k, v in env.items()
                if k not in ("MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA")}
        bare["HOME"] = os.path.join(tmp, "bare-home")
        os.makedirs(bare["HOME"])
        assert not os.path.exists(os.path.join(bare["HOME"], ".matrixkoder"))
        got = subprocess.run([sys.executable, self_path, "acquire", "--run", "Rs1", "--task", "RT3",
                              "--base", "main", "--data", rsd], capture_output=True, text=True,
                             env=bare, cwd=repo)
        assert got.returncode == 0 and json.loads(got.stdout)["ok"], (got.returncode, got.stdout,
                                                                       got.stderr)
        assert not os.path.exists(os.path.join(bare["HOME"], ".matrixkoder")), "a read created a home"
        r = subprocess.run([sys.executable, self_path, "release", "--slot", "1", "--integration",
                            "mk/r1/integration", "--data", rsd], capture_output=True, text=True,
                           env=bare, cwd=repo)
        assert r.returncode == 0 and json.loads(r.stdout)["ok"], (r.returncode, r.stdout, r.stderr)

        # (e) a pre-migration project: no store at all, so the run files are still the source.
        # L4 (C4) made mk-state store-always, so this fixture writes the legacy file views by hand
        # — that is the one shape a storeless project can have now.
        nsd = os.path.join(tmp, "data-resolve-file")
        os.makedirs(os.path.join(nsd, "runs", "Fs1"))
        write(os.path.join(nsd, "runs", "index.json"), json.dumps([{"id": "Fs1"}]))
        write(mkpaths.run_doc_in(mkpaths.legacy_run_dir(nsd, "Fs1")),
              json.dumps({"schema": 1, "id": "Fs1", "tasks": [{"id": "Ft1"}]}))
        assert not os.path.exists(mkpaths.db_in(nsd))
        assert store_snapshot(nsd) is None
        assert run_ids(nsd) == ["Fs1"], run_ids(nsd)
        assert resolve_run(nsd, "fS1") == "Fs1" and resolve_run(nsd, "nope") is None
        assert resolve_task(nsd, "Fs1", "FT1") == "Ft1", resolve_task(nsd, "Fs1", "FT1")
        assert resolve_task(nsd, "Nosuch", "ft1") == "ft1"
        assert resolve_task(nsd, "Fs1", "nope") == "nope"
        # with no store the ids come from index.json alone: a run.json that is gone is still known
        os.unlink(mkpaths.run_doc(nsd, "Fs1"))
        assert run_ids(nsd) == ["Fs1"], run_ids(nsd)
        assert resolve_run(nsd, "Fs1") == "Fs1" and resolve_task(nsd, "Fs1", "ft1") == "ft1"

        print("mk-lane self-test ok")
        # R1.2: the mkdb write guard refuses the real home from this suite too.
        mkdb.selftest_guard_probe(lane_home)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        sys.exit(main(sys.argv[1:]))
