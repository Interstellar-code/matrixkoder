#!/usr/bin/env python3
"""MatrixKoder lanes — computed names + a small pool of reusable worktree slots. Python 3 stdlib only.

  names   --run R --task T [--role build|fix|takeover|review] [--round N] [--slot N]
  acquire --run R --task T --base REF [--role ROLE] [--round N]
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
computed here, so no agent invents one.

This tool belongs to the ORCHESTRATOR: it runs `git switch` inside a slot, only after the slot is
verified clean and never with force. Builders never run it. It owns <data>/lanes.json (outside runs/);
run state stays mk-state.py's alone — the task keys written there are best-effort (a failure is a
warning, never a failed acquire). Schema: docs/state-schema.md
"""
import argparse, contextlib, fcntl, fnmatch, hashlib, importlib.util, json, math, os, re
import subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True  # no stray __pycache__ next to the scripts

SCHEMA = 1
ROLES = ("build", "fix", "takeover", "review")
AGENT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")  # herdr's name limit: 32 chars, lowercase
ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")  # run/task ids after lowercasing
KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*)$")
LOCKFILES = ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb",
             "uv.lock", "poetry.lock", "Cargo.lock")
# Ignored entries a slot may keep between tasks; anything else is a pause item, never discarded.
DISPOSABLE = (".matrixkoder-*", ".env", ".env.*", "node_modules/", ".omc/", ".serena/", ".venv/",
              "__pycache__/")
# The build-cache dirs every package/venv path ends with: allowed as a path component at any depth,
# so a monorepo's packages/a/node_modules/ does not read as dirty. Everything else stays root-only.
ANY_DEPTH = ("node_modules/", "__pycache__/", ".venv/")
# A lanes_disposable entry is a literal name: no globs, no paths, no `..` — a wildcard would make
# every ignored file disposable and the next gc would delete the user's data.
DISPOSABLE_RE = re.compile(r"[A-Za-z0-9._-]+/?")
LOCK_TIMEOUT = 30.0
GIT_TIMEOUT, HERDR_TIMEOUT = 120, 300
DEFAULTS = {"slots": 1, "pattern": "../{repo}-mk-{n}", "reuse": True, "cap": 3, "disposable": []}


def _load(name, path):
    _spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mod)
    return mod


mk_state = _load("mk_state", os.path.join(HERE, "mk-state.py"))  # data dir, atomic write, timestamp


class Fail(Exception):
    def __init__(self, msg, code=2, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


# ---------- config: <data>/matrixkoder.yaml, line-wise (no YAML dependency) ----------

def scalar(v):
    """One YAML scalar: quoted or plain text up to a ` #` comment (hooks/guards.ts scalar())."""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return re.split(r"\s+#", v, maxsplit=1)[0].strip()


def split_flow(body):
    """Split a flow collection on the commas that are not inside quotes."""
    parts, cur, quote = [], "", None
    for ch in body:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch == ",":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p for p in parts if p.strip()]


def strip_comment(s):
    """Drop a trailing `# …` that is outside quotes. The wizard emits one on the lanes line, and
    without this the whole `{ … }` is unparseable and every value silently falls back to DEFAULTS."""
    quote = None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i]
    return s


def flow_map(body):
    body = strip_comment(body).strip()
    if not (body.startswith("{") and body.endswith("}")):
        return {}
    out = {}
    for item in split_flow(body[1:-1]):
        k, sep, v = item.partition(":")
        if sep:
            out[k.strip()] = scalar(v)
    return out


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


def load_config(data):
    """<data>/matrixkoder.yaml: `  lanes: { slots, pattern, reuse }` under repo, `max_build_lanes`
    (indent >= 2, any block) as the lane cap and one-line `lanes_disposable: [a, b]` on top of the
    clean-check allowlist. Defaults when absent or unreadable; `warnings` carries entries the
    lanes_disposable check rejected."""
    cfg = dict(DEFAULTS, disposable=[], warnings=[])
    try:
        with open(os.path.join(data, "matrixkoder.yaml")) as f:
            lines = f.read().splitlines()
    except OSError:
        return cfg
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
            with contextlib.suppress(ValueError, KeyError):
                cfg["slots"] = max(1, int(block["slots"]))
            if block.get("pattern"):
                cfg["pattern"] = block["pattern"]
            if block.get("reuse") in ("true", "false"):
                cfg["reuse"] = block["reuse"] == "true"
    cfg["cap"] = max(cfg["slots"], cfg["cap"])  # the pool never grows past the lane cap
    return cfg


# ---------- repo + data dir ----------

def git_toplevel():
    """The nearest .git above cwd, exactly like mk-state.resolve_data's walk (never $HOME itself)."""
    d, stop = os.path.realpath(os.getcwd()), os.path.realpath(os.path.expanduser("~"))
    while not os.path.exists(os.path.join(d, ".git")):
        if os.path.dirname(d) == d or d == stop:
            raise Fail("no git toplevel above cwd (the walk stops before $HOME); pass --data")
        d = os.path.dirname(d)
    return d


def main_checkout():
    """A linked worktree resolves to its main checkout (mk-state.main_worktree)."""
    return mk_state.main_worktree(git_toplevel())


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


# ---------- <data>/lanes.json, one exclusive lock ----------

@contextlib.contextmanager
def locked(data, timeout):
    """Every read-modify-write of lanes.json holds this flock; a holder that outlasts the timeout is
    an error, never a second writer racing the first."""
    with contextlib.suppress(OSError):
        os.makedirs(data, exist_ok=True)
    f = open(os.path.join(data, "lanes.lock"), "a")
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


def read_lanes(data):
    """-> the registry. A missing or wrong-shaped file is an empty pool; corrupt JSON is an error
    (exit 2), because a pool we cannot read is not a pool we may quietly reset."""
    d = mk_state.read_json(os.path.join(data, "lanes.json"), None)
    if not isinstance(d, dict) or not isinstance(d.get("slots"), dict):
        return {"schema": SCHEMA, "slots": {}}
    d["schema"] = SCHEMA
    d["slots"] = {str(k): v for k, v in d["slots"].items() if isinstance(v, dict)}
    return d


def write_lanes(data, lanes):
    mk_state.write_json(os.path.join(data, "lanes.json"), {"schema": SCHEMA, "slots": lanes["slots"]})


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
    """`workspace_id` from herdr's JSON output (its own id when nested); None when it prints none."""
    for line in reversed([x for x in text.splitlines() if x.strip()]):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        ws = d.get("workspace")
        for cand in (d.get("workspace_id"), ws.get("id") if isinstance(ws, dict) else None, d.get("id")):
            if cand:
                return str(cand)
    return None


def ensure_lane_exclude(lane):
    """`.matrixkoder-*` into the slot's info/exclude (same pattern as mk-lane-event.py)."""
    with contextlib.suppress(OSError):
        r = run_cmd(["git", "-C", lane, "rev-parse", "--git-path", "info/exclude"], 5)
        if r.returncode == 0 and r.stdout.strip():
            ep = r.stdout.strip()
            ep = ep if os.path.isabs(ep) else os.path.join(lane, ep)
            if os.path.exists(os.path.dirname(ep)):
                cur = open(ep).read() if os.path.exists(ep) else ""
                if ".matrixkoder-*" not in cur:
                    with open(ep, "a") as f:
                        f.write(".matrixkoder-*\n")


# ---------- the clean check (shared by acquire, release and gc) ----------

def disposable_ok(path, patterns):
    """Is an ignored entry disposable? A trailing-`/` entry covers its whole subtree — at the root, or
    at any depth when it is one of ANY_DEPTH — while a name must match the path itself at the root
    (fnmatch, so `.matrixkoder-*` and `.env.*` are globs)."""
    p = path.strip().rstrip("/")
    for pat in patterns:
        if pat.endswith("/"):
            name, root = pat[:-1], pat
            if p == name or p.startswith(root):
                return True
            if pat in ANY_DEPTH and f"/{name}/" in f"/{p}/":
                return True  # packages/a/node_modules/, scripts/__pycache__/, …
        elif p == pat or fnmatch.fnmatchcase(p, pat) or fnmatch.fnmatchcase(p + "/*", pat):
            return True
    return False


def clean_state(path, cfg):
    """-> (clean, dirty lines): `git status --porcelain --ignored` in the slot may list nothing but
    ignored entries on the allowlist. Read-only: it never repairs, only reports.
    The untracked/submodule flags are given on the command line on purpose: `status.showUntrackedFiles`
    and `diff.ignoreSubmodules` in the user's config would otherwise hide `??` lines, and this check
    is the only thing standing between a hidden untracked file and `git worktree remove`."""
    r = run_cmd(["git", "-C", path, "status", "--porcelain", "--ignored", "--untracked-files=normal",
                 "--ignore-submodules=none"], 60)
    if r.returncode != 0:
        raise Fail(f"git status in {path} failed", stderr=(r.stderr or r.stdout).strip()[:400])
    allow = tuple(DISPOSABLE) + tuple(cfg["disposable"])
    dirty = [ln for ln in r.stdout.splitlines()
             if not (ln.startswith("!! ") and disposable_ok(ln[3:], allow))]
    return not dirty, dirty


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


def set_task(data, run, task, rec, n):
    """`mk-state task set` for lane/branch/workspace/slot. Best-effort: lanes.json is the truth about
    slots, so a failure here is a warning on an otherwise successful acquire, never a failed one."""
    r = run_cmd([sys.executable, os.path.join(HERE, "mk-state.py"), "--data", data, "--run", run,
                  "task", "set", task, f"lane={rec['path']}", f"branch={rec['branch']}",
                  f"workspace={rec['workspace']}", f"slot={n}"], 60)
    return None if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200] or f"exit {r.returncode}"


# ---------- commands ----------

def cmd_names(a, cfg, data, main):
    return names(a.run, a.task, cfg, a.role, a.round, a.slot, main)


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


def create_slot(rec, main, base_sha, base_ref):
    """Create the slot's worktree: herdr (labelled workspace) or plain git. git is given the resolved
    sha; herdr gets the ref the caller wrote, which check_ref already resolved and proved a commit.
    The record is written by the caller after this returns, so a failure leaves no slot behind."""
    bin_ = herdr_bin()
    if bin_ == "none":
        rec["workspace_id"] = None  # a recreated slot has no herdr workspace, let alone a stale id
        git(["-C", main, "worktree", "add", "-b", rec["branch"], rec["path"], base_sha])
    else:
        r = run_cmd([bin_, "worktree", "create", "--cwd", main, "--branch", rec["branch"],
                     "--base", base_ref, "--path", rec["path"], "--label", rec["workspace"],
                     "--no-focus"], HERDR_TIMEOUT)
        if r.returncode != 0:
            raise Fail("herdr worktree create failed", stderr=(r.stderr or r.stdout).strip()[:400])
        rec["workspace_id"] = herdr_json_id(r.stdout)
    ensure_lane_exclude(rec["path"])


def remove_slot(rec, main):
    """Drop the slot's worktree: herdr (which also removes the ignored files) or plain git. Never
    forced. -> None on success, the reason on failure."""
    bin_ = herdr_bin()
    if bin_ != "none" and rec.get("workspace_id"):
        r = run_cmd([bin_, "worktree", "remove", "--workspace", rec["workspace_id"]], HERDR_TIMEOUT)
        return None if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200] or f"exit {r.returncode}"
    git(["-C", main, "worktree", "remove", rec["path"]])
    return None


def cmd_acquire(a, cfg, data, main):
    nms = names(a.run, a.task, cfg, a.role, a.round, 1, main)  # validates run/task before the lock
    base = check_ref(main, a.base, "--base")
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
        if created:
            if os.path.lexists(rec["path"]):
                raise Fail(f"slot path {rec['path']} exists and is not a registered slot")
            create_slot(rec, main, base, a.base)
        else:
            verify_slot_path(n, rec["path"], main)
            require_clean(n, rec, cfg)  # before any git command runs
            require_no_stray(n, rec["path"])
            git(["-C", rec["path"], "switch", "--detach", base])
            git(["-C", rec["path"], "switch", "-c", rec["branch"]])  # an existing branch fails here
        h = lock_hash(rec["path"])
        install_needed = created or h != rec.get("lock_hash") or rec.get("lock_hash") is None
        touch(rec, state="busy", run=a.run, task=a.task, lock_hash=h)
        write_lanes(data, lanes)
        out = {"ok": True, "slot": n, "path": rec["path"], "workspace": rec["workspace"],
               "workspace_id": rec["workspace_id"], "branch": rec["branch"],
               "agent": nms["agent"], "created": created, "install_needed": install_needed}
        if warning := set_task(data, a.run, a.task, rec, n):
            out["warning"] = f"mk-state task set: {warning}"
        return with_warnings(out, cfg)


def cmd_release(a, cfg, data, main):
    integration = check_ref(main, a.integration, "--integration")
    with locked(data, a.lock_timeout):
        lanes = read_lanes(data)
        rec = lanes["slots"].get(str(a.slot))
        if rec is None:
            raise Fail(f"no slot {a.slot}")
        verify_slot_path(a.slot, rec["path"], main)
        require_clean(a.slot, rec, cfg)
        require_no_stray(a.slot, rec["path"])
        git(["-C", rec["path"], "switch", "--detach", integration])
        branch, warning, deleted = rec.get("branch"), None, None
        if branch:
            # merged-ness is judged against the integration branch, not the main checkout's HEAD, and
            # update-ref -d <ref> <sha> is a compare-and-swap that refuses if the branch moved
            sha = try_ref(main, branch)
            if sha is None:
                deleted, warning = False, f"branch {branch} already gone"
            elif run_cmd(["git", "-C", main, "merge-base", "--is-ancestor", sha,
                          integration]).returncode == 0:
                r = run_cmd(["git", "-C", main, "update-ref", "-d", f"refs/heads/{branch}", sha])
                deleted = r.returncode == 0
                if not deleted:
                    warning = f"branch {branch} moved while releasing, kept"
            else:
                deleted = False
                warning = f"branch {branch} not merged into {a.integration}, kept"  # never -D
        touch(rec, state="free", run=None, task=None, branch=None)
        out = {"ok": True, "slot": a.slot, "path": rec["path"], "state": "free",
               "integration": a.integration, "branch": branch, "branch_deleted": deleted}
        if not cfg["reuse"]:  # one-shot slots: the worktree goes with the release
            try:
                why = remove_slot(rec, main)
            except Fail as e:
                why = e.extra.get("stderr") or str(e)
            if why:
                warning = f"{warning}; slot kept: {why}" if warning else f"slot kept: {why}"
            else:
                lanes["slots"].pop(str(a.slot), None)
        write_lanes(data, lanes)
        return with_warnings({**out, **({"warning": warning} if warning else {})}, cfg)


def with_warnings(out, cfg):
    """Config problems (a rejected lanes_disposable entry) travel with every ok answer: the caller
    must see that its allowlist is narrower than it asked for."""
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
                why = remove_slot(rec, main)
            except Fail as e:
                why = e.extra.get("stderr") or str(e)
            if why:
                skipped.append({"slot": n, "reason": f"remove failed: {why}"})
                continue
            lanes["slots"].pop(str(n))
            removed.append(n)
        write_lanes(data, lanes)
        return with_warnings({"ok": True, "removed": removed, "skipped": skipped}, cfg)


def cmd_list(a, cfg, data, main):
    lanes = read_lanes(data)  # read-only: no lock, no state change
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
        x.add_argument("--run", required=True), x.add_argument("--task", required=True)
        x.add_argument("--role", default="build", choices=ROLES)
        x.add_argument("--round", type=int, default=1)

    x = sub.add_parser("names", parents=[dp])
    lane_args(x)
    x.add_argument("--slot", type=int, default=1)
    x.set_defaults(func=cmd_names)
    x = sub.add_parser("acquire", parents=[dp])
    lane_args(x)
    x.add_argument("--base", required=True)
    x.set_defaults(func=cmd_acquire)
    x = sub.add_parser("release", parents=[dp])
    x.add_argument("--slot", type=int, required=True), x.add_argument("--integration", required=True)
    x.set_defaults(func=cmd_release)
    x = sub.add_parser("gc", parents=[dp])
    x.add_argument("--keep", type=int, default=1)
    x.set_defaults(func=cmd_gc)
    sub.add_parser("list", parents=[dp]).set_defaults(func=cmd_list)
    return p


def main(argv):
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
    print(json.dumps(out))
    return code


# ---------- self-test: real git in temp repos, temp data dir, never the real ~/.matrixkoder ----------

def self_test():
    import shutil
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="mk-lane-test-"))  # realpath: paths are reported that way
    env = dict(os.environ, MATRIXKODER_HOME=os.path.join(tmp, "home"), MK_HERDR="none")
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

    def run_py(script, args, code=0):
        r = subprocess.run([sys.executable, script, *args, "--data", data], capture_output=True,
                           text=True, env=env, cwd=repo)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    def mk(*args, code=0):
        return run_py(self_path, list(args), code)

    def st(*args, code=0):
        return run_py(os.path.join(HERE, "mk-state.py"), list(args), code)

    def write(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def yaml_cap(slots, cap, pattern="../{repo}-mk-{n}", disposable=None):
        with open(os.path.join(data, "matrixkoder.yaml"), "w") as f:
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
        write(os.path.join(repo, ".gitignore"), "node_modules/\n__pycache__/\n.venv/\n.omc/\nprod.sqlite\n")
        write(os.path.join(repo, "pkg", "a", "src", "app.js"), "// tracked, so git lists the\n"
              "                        // ignored dir inside pkg/ instead of collapsing to `?? pkg/`\n")
        write(os.path.join(repo, "scripts", "tool.py"), "# tracked\n")
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
        write(os.path.join(data, "matrixkoder.yaml"),
              'repo:\n  lanes: { slots: 2, pattern: "../{repo}-lanes-{n}", reuse: true }   '
              "# reusable worktree slots; the cap is max_build_lanes\n")
        assert load_config(data) == {**DEFAULTS, "slots": 2, "pattern": "../{repo}-lanes-{n}",
                                     "cap": 3, "disposable": [], "warnings": []}, load_config(data)
        yaml_cap(2, 2, "../{repo}-lanes-{n}", "build")
        assert load_config(data) == {**DEFAULTS, "slots": 2, "pattern": "../{repo}-lanes-{n}",
                                     "cap": 2, "disposable": ["build"], "warnings": []}, load_config(data)
        yaml_cap(1, 3)
        assert load_config(data)["cap"] == 3, load_config(data)  # max(slots, max_build_lanes)
        yaml_cap(4, 2)
        assert load_config(data)["cap"] == 4, load_config(data)
        write(os.path.join(data, "matrixkoder.yaml"),
              "repo:\n  lanes: { slots: 1, pattern: ../{repo}-mk-{n}, reuse: false }\n")
        assert load_config(data) == {**DEFAULTS, "slots": 1, "reuse": False,
                                     "warnings": []}, load_config(data)
        # a wildcard disposable entry is refused, so it can never widen the allowlist to "everything"
        write(os.path.join(data, "matrixkoder.yaml"),
              'repo:\n  lanes: { slots: 1, pattern: "../{repo}-mk-{n}", reuse: true }\n'
              'lanes_disposable: ["*", "../secrets", "*.db", "build", "dist/"]\n')
        cfg = load_config(data)
        assert cfg["disposable"] == ["build", "dist/"], cfg
        assert len(cfg["warnings"]) == 3, cfg
        yaml_cap(1, 1)

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
        write(os.path.join(data, "matrixkoder.yaml"),
              'repo:\n  lanes: { slots: 1, pattern: "../{repo}", reuse: true }\n')
        assert "{n}" in mk("names", "--run", "r1", "--task", "t1", code=2)["error"]
        yaml_cap(1, 1)

        st("run", "init", "--id", "r1", "--feature", "Lanes", "--branch", "mk/r1/integration")
        st("task", "add", "--id", "t1", "--title", "first", "--run", "r1")

        # 2 first acquire creates slot 1 on the task branch and records the task keys
        a1 = mk("acquire", "--run", "r1", "--task", "t1", "--base", "main")
        assert a1 == {"ok": True, "slot": 1, "path": slot1, "workspace": "mk:repo:1",
                      "workspace_id": None, "branch": "mk/r1/t1", "agent": "t1-build-r1",
                      "created": True, "install_needed": True}, a1
        assert os.path.isdir(slot1)
        assert g("-C", slot1, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "mk/r1/t1"
        assert mk("list")["slots"][0]["state"] == "busy"
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
                       "branch_deleted": True}, rel
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
        rec = mk_state.read_json(os.path.join(data, "lanes.json"))["slots"]["1"]
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
        # an ignored node_modules/ or .omc/ is disposable, not dirty
        os.makedirs(os.path.join(slot1, "node_modules", "pkg"), exist_ok=True)
        os.makedirs(os.path.join(slot1, ".omc", "sessions"), exist_ok=True)
        write(os.path.join(slot1, "node_modules", "pkg", "i.js"), "x\n")
        write(os.path.join(slot1, ".omc", "sessions", "s.json"), "{}\n")
        write(os.path.join(slot1, ".matrixkoder-report-r1.md"), "report\n")
        assert mk("list")["slots"][0]["clean"] is True
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
        assert "mk/r1/t5" not in open(os.path.join(data, "lanes.json")).read()  # only in the branch ref

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

        # 12 the pattern can never re-aim a slot, and a slot path is always ours to touch
        other = os.path.join(tmp, "other-1")
        g("worktree", "add", "-q", other, "-b", "userwork", "main")
        write(os.path.join(other, "mine.txt"), "the user's own work\n")
        g("-C", other, "add", "-A"), g("-C", other, "commit", "-qm", "user work")
        write(os.path.join(data, "matrixkoder.yaml"),
              'repo:\n  lanes: { slots: 2, pattern: "{repo}-lanes/{n}", reuse: true }\n')
        bad = mk("acquire", "--run", "r1", "--task", "tb", "--base", "main", code=2)
        assert bad["ok"] is False and "inside it" in bad["error"], bad
        assert main_head() == "main", main_head()  # the main checkout was never switched
        write(os.path.join(data, "matrixkoder.yaml"),
              'repo:\n  lanes: { slots: 2, pattern: "../{repo}", reuse: true }\n')
        bad = mk("acquire", "--run", "r1", "--task", "tb", "--base", "main", code=2)
        assert bad["ok"] is False and "{n}" in bad["error"], bad
        assert main_head() == "main", main_head()
        write(os.path.join(data, "matrixkoder.yaml"),
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

        # 13 a rejected lanes_disposable entry narrows the allowlist and is reported
        write(os.path.join(data, "matrixkoder.yaml"),
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
        before = open(os.path.join(data, "lanes.json")).read()
        hold = open(os.path.join(data, "lanes.lock"), "a")
        fcntl.flock(hold, fcntl.LOCK_EX)
        try:
            assert mk("acquire", "--run", "r1", "--task", "td", "--base", "main", "--lock-timeout", "1",
                      code=2)["error"] == "lanes.lock busy"
            assert open(os.path.join(data, "lanes.json")).read() == before  # no state change
            assert mk("list")["slots"][1]["task"] == "tc"  # the registry stays readable while locked
        finally:
            fcntl.flock(hold, fcntl.LOCK_UN)
            hold.close()
        assert mk("acquire", "--run", "r1", "--task", "td", "--base", "main",
                  code=4)["error"] == "all 2 slots busy"
        assert main_head() == "main"  # this test never moved the user's main checkout
        print("mk-lane self-test ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        sys.exit(main(sys.argv[1:]))
