#!/usr/bin/env python3
"""MatrixKoder loopback message bus — SSE / HTTP pubsub under flock. Python 3 stdlib only.

Subcommands:
  serve [--data DIR] [--run ID] [--port PORT] [--prefer-port PORT]
      Runs foreground loopback HTTP/SSE server. Holds <data>/bus.lock exclusively (0600),
      writes <data>/bus.json {pid, port, started, owner_heartbeat, token} (0600), registers token in
      <data>/bus-tokens.json (0600). Rebuilds state from <data>/runs/<run>/bus.jsonl.
      Self-exits when owner_heartbeat is older than bus.owner_timeout_min
      (<data>/matrixkoder.yaml, clamped to [5, 120] min, default 30; $MK_BUS_HEARTBEAT_TIMEOUT_SEC
      overrides) or when bus.json is missing. --prefer-port tries that port first, then falls
      back to an OS-assigned one (ensure passes the dead bus's port so armed lanes keep their url).
  ensure [--data DIR] [--run ID] [--port PORT]
      Ensures a detached server process is running. Idempotent. A recorded pid is only
      trusted when /health names this data dir (a refused port means pid reuse): else
      bus.json is stale and the bus respawns. Returns
      {"ok": true, "pid": PID, "port": PORT, "started": BOOL}.
  heartbeat [--data DIR] [--run ID]
      Updates owner_heartbeat in <data>/bus.json. If the server is dead, restarts it (the
      ensure path). Prints {"ok":true,"restarted":bool,"pid","port"}.
  token --lane DIR --task T [--run ID] [--data DIR]
      Mints bearer token, writes <lane>/.matrixkoder-bus.json (0600) with
      {"v": 1, "url": "http://127.0.0.1:<port>", "token": TOKEN, "run": RUN, "task": TASK},
      and registers in <data>/bus-tokens.json (0600). The reserved task id "orch" is refused.
  revoke --lane DIR [--data DIR]
      Removes <lane>/.matrixkoder-bus.json and invalidates token.
  send --run ID --task T --kind KIND --text TEXT [--from S] [--to S] [--reply-to ID] [--data DIR]
      Sends a message via POST /send. A lane-token ask also marks its task blocked in run state
      (mk-state task set <T> status=blocked --note "bus ask <id>: <text>" --by bus, once per
      (run, ask id) — ids are per run); that reply carries state_updated/mk_state_error, and an
      mk-state failure never fails the POST.
  answer MSG_ID TEXT [--run ID] [--data DIR]
      Sends answer message with reply_to=MSG_ID. Also runs
      scripts/mk-state.py --data <data> --run ID task set <T> status=building --note "bus answer".
  pending [--run ID] [--data DIR]
      Lists asks without an answer where reply_to == ask.id.
  tail [--run ID] [--n N] [--lane L] [--after SEQ] [--data DIR]
      Lists messages from bus.jsonl.
  watch [--run ID] [--task T] [--after ID|SEQ] [--replay] [--max-messages N] [--data DIR]
      Streams bus messages, one line each. Keeps the last printed seq per (run, task) in
      <data>/watch-cursors.json (0600): a new watch resumes after it. An explicit --after wins
      over the cursor; --replay ignores it and starts from 0; the first watch ever replays history.
  archive-run [--run ID] [--data DIR]
      L6.3 (C6): archive ONE closed run's bus messages, acks and bus artifacts to
      <run dir>/bus.jsonl (mkpaths-resolved), count-check the file against the rows, then
      delete the rows in one transaction. Refuses a run that is not closed (run status
      done|archived and every task done|merged|blocked). The seq high-water mark is kept
      above the archived range, so no later message reuses a deleted seq. A second call
      is a no-op. A mismatch deletes nothing and removes the partial file.
  --self-test
      Comprehensive self-test of server, round trip, auth, replay, lifecycle, security, error handling.

Every command prints ONE JSON line. Errors: {"ok": false, "error": ...} exit 2.
"""
import argparse, contextlib, datetime, fcntl, hashlib, hmac, importlib.util, io, json, math, os, re, secrets, shutil, signal, socket, stat, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
RING_ACT = "then carry out what it delivers (brief, round, nudge) without asking; ask only over the bus for a real decision."

# `watch` (E2 4): the bus must stay down this long before watch gives up with
# exit 4. One blip, a dropped socket or a server restart resumes instead.
WATCH_DOWN_SEC = 60.0
WATCH_RETRY_SEC = 0.5
WATCH_READ_SEC = 30.0

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# File mode (no project store): the original four kinds, 64 KiB body.
# Store mode adds C7 kinds. report/review bodies page up to 128 KiB.
FILE_KINDS = ("ask", "answer", "info", "ack")
STORE_KINDS = FILE_KINDS + ("task", "status", "commit", "report", "review", "nudge")
FILE_TEXT_MAX = 65536
STORE_TEXT_MAX = 131072
PAGE_BYTES = 65536
# X2.3: a `report`/`review` body above STORE_TEXT_MAX is not refused — the bus pages it at
# PAGE_BYTES (the `\x1e` separator the contract already names) and stores the WHOLE body as
# one artifact. The ceiling is the artifact writer's own (mkdb.ARTIFACT_MAX), so a body the
# store cannot hold is still refused rather than half-written.
PAGED_TEXT_MAX = 256 * 1024
SEP = "\x1e"
NONEMPTY_KINDS = ("ask", "answer", "info", "task", "status", "commit", "report", "review", "nudge")
# status values transition() already accepts (COLUMNS plus attention).
STATUS_VALUES = (
    "planned", "routed", "ready", "dispatched", "building", "takeover",
    "verifying", "review", "fix", "merged", "qa", "done",
    "blocked", "stalled", "quota", "provider", "paused",
)


def _load_mkpaths():
    """scripts/mkpaths.py, the ONE module for every MK path (C1, L0): loaded as a sibling
    by path, the way _load_mkdb loads mkdb (each script stands alone). mkpaths imports
    nothing from the repo, so there is no cycle. Cached: it is stateless by design."""
    global _mkpaths
    if _mkpaths is None:
        spec = importlib.util.spec_from_file_location("mkpaths", os.path.join(HERE, "mkpaths.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _mkpaths = mod
    return _mkpaths


_mkpaths = None


def _paths(name, *args, **kwargs):
    """One mkpaths call by function name. mkpaths.Fail is a RuntimeError, this module's is
    its own (exit 2 / one JSON line), so the two are converted here — one place."""
    mod = _load_mkpaths()
    try:
        return getattr(mod, name)(*args, **kwargs)
    except mod.Fail as e:
        raise Fail(str(e))


def project_store_path(data_dir):
    """The project store lives at <data>/mk.db. Absent means file mode."""
    return _paths("project_db", data=data_dir)


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def mk_home():
    """MK home: $MK_HOME or $MATRIXKODER_HOME (both set must agree), else ~/.matrixkoder.

    One MK home rule (C16), identical to mkdb.resolve_home(None): either name is
    accepted; when both are set they must resolve to the same directory.
    The rule itself lives in mkpaths (L0): this stays the mk-bus spelling of it."""
    return _paths("home")


def root_bus_dir(home=None):
    """Where the one-per-home bus lives: <home>/bus.json and <home>/bus.lock."""
    return home or mk_home()


def lane_token_path(lane_dir, home=None):
    """C6: the lane token file is outside the worktree.

    ~/.matrixkoder/bus-lanes/<sha256(realpath(lane))>.json, mode 0600.
    $MK_BUS_TOKEN_FILE overrides the path (tests and a relocated home).
    The rule itself lives in mkpaths (L0), the same one for mk-bus,
    mk-lane-event and mk-builder."""
    return _paths("lane_token", lane_dir)


def _load_mkdb():
    path = os.path.join(HERE, "mkdb.py")
    spec = importlib.util.spec_from_file_location("mkdb", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def store_mode(data_dir):
    """Self-hosting guard: store code runs only when the project store already exists."""
    return os.path.isfile(project_store_path(data_dir))


RING_QUEUE_MAX = 100
RING_MAX_AGE = 3600
# Agent kinds that take typed input into a running turn (a ring needs no idle wait).
STEER_KINDS_DEFAULT = frozenset({"claude", "codex"})
PANE_RE = re.compile(r"^[A-Za-z0-9_:.-]+$")


class StoreAdapter:
    """The only place mk-bus talks to the project store.

    Wired to mkdb.append_event / append_ack / put_artifact / transition once those
    helpers exist (D1D). Until then every method raises, and store_mode stays false
    unless a store file is already present — callers must not reach here in file mode.
    """

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self._mkdb = None

    def _mod(self):
        if self._mkdb is None:
            path = os.path.join(HERE, "mkdb.py")
            spec = importlib.util.spec_from_file_location("mkdb", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._mkdb = mod
        return self._mkdb

    def store_append_event(self, run, *, task=None, kind, from_id=None, to_id=None,
                            reply_to=None, text="", meta=None, artifact_id=None, t=None, chan="bus"):
        fn = getattr(self._mod(), "append_event", None)
        if fn is None:
            raise Fail("store writer append_event is not available")
        # Test-only: MK_BUS_APPEND_FAIL makes the next append raise after the
        # caller has already transitioned, so the 500 path is reachable.
        flag_path = os.path.join(self.data_dir, "append-fail")
        if os.path.isfile(flag_path):
            os.remove(flag_path)
            raise RuntimeError("injected append failure")
        return fn(
            self.data_dir, None, run, task=task, chan=chan, kind=kind,
            from_id=from_id, to_id=to_id, reply_to=reply_to, text=text,
            meta=meta, artifact_id=artifact_id, t=t, data_dir=self.data_dir,
        )

    def store_append_ack(self, run, msg, lane, level, *, t=None):
        fn = getattr(self._mod(), "append_ack", None)
        if fn is None:
            raise Fail("store writer append_ack is not available")
        return fn(self.data_dir, None, run, msg, lane, level, t=t, data_dir=self.data_dir)

    def store_put_artifact(self, run, task, round_n, kind, text, *, event_id=None):
        fn = getattr(self._mod(), "put_artifact", None)
        if fn is None:
            raise Fail("store writer put_artifact is not available")
        return fn(
            self.data_dir, None, run, task, round_n, kind, text,
            event_id=event_id, data_dir=self.data_dir,
        )

    def store_transition(self, run, task, to, note="", round_n=0):
        fn = getattr(self._mod(), "transition", None)
        if fn is None:
            raise Fail("store writer transition is not available")
        # Test-only, the twin of the append-fail flag above: a one-shot marker in the data
        # dir makes the NEXT transition raise after the caller has already appended the
        # message, so the "the POST succeeded, the side-write did not, its error rides in
        # the reply" path is reachable without a real mk-state failure (L4 moved this write
        # off mk-state and into mkdb, so $MK_STATE_CMD can no longer fail it).
        flag_path = os.path.join(self.data_dir, "transition-fail")
        if os.path.isfile(flag_path):
            os.remove(flag_path)
            raise RuntimeError("injected transition failure")
        return fn(
            self.data_dir, None, run, task, to, note=note, round=round_n,
            sync_root=False, data_dir=self.data_dir,
        )

    def store_select(self, sql, params=()):
        """SELECT only, on a real read-only open (mkdb._open_read: mode=ro, no migration, no write).

        No store file yet means no rows. A locked, newer or unmigrated store raises Fail.
        """
        mod = self._mod()
        path = _load_mkpaths().db_in(mod.project_dir(self.data_dir, None, self.data_dir, True))
        try:
            con = mod._open_read(path, mod.SCHEMA)
        except Exception as e:
            raise Fail("store read failed: %s" % (e,))
        if con is None:
            return []
        try:
            return [dict(r) for r in con.execute(sql, params)]
        except Exception as e:
            raise Fail("store read failed: %s" % (e,))
        finally:
            con.close()


def is_valid_id(val):
    """C2 identifier alphabet: `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` and never contains `..`
    (contract B18: "no `..`, no `/`"). The `..` check matters even though the regex
    allows dots, because a run id like `evil..run` would otherwise slip past validation
    and reach path joins / token scoping."""
    return bool(
        val
        and isinstance(val, str)
        and ID_RE.match(val)
        and ".." not in val
    )


def page_body(text, size=PAGE_BYTES):
    """X2.3: split a `report`/`review` body into `size`-byte pages joined by the SEP separator
    the contract names. A body that already fits one page is returned unchanged, so the wire
    shape of every ordinary message is untouched; a bigger one travels paged and is reassembled
    whole (SEP stripped) into its artifact."""
    parts, cur, n = [], [], 0
    for ch in text:
        b = len(ch.encode("utf-8"))
        if cur and n + b > size:
            parts.append("".join(cur))
            cur, n = [], 0
        cur.append(ch)
        n += b
    parts.append("".join(cur))
    if len(parts) <= 1:
        return text
    return SEP.join(parts)


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


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path, obj, mode=0o600):
    """Atomic write with explicit permissions: temp file in same dir, fchmod, fsync, os.replace."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            os.chmod(path, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return default


def out(obj):
    print(json.dumps(obj, separators=(",", ":")))
    sys.stdout.flush()


def resolve_data(arg=None):
    """The data-dir order (C1): --data > $MATRIXKODER_DATA > root projects table >
    <home>/projects.json > <main checkout>/.matrixkoder. mkpaths owns it (L0): the bus
    now uses the FULL order, as the state side does."""
    return _paths("data_dir", arg)


def bus_location(data_dir):
    """Where bus.json / bus.lock / bus-tokens.json live for this data dir.

    File mode (no <data>/mk.db): the data dir, exactly as before — the
    self-hosting guard. Store mode: the MK home, one bus for every project.
    $MK_BUS_DIR overrides (tests pin a temp home without touching ~/.matrixkoder).
    The rule itself lives in mkpaths (L0)."""
    return _paths("bus_dir", data_dir)


def live_bus_dir(data_dir):
    """The directory whose bus.json describes the bus actually serving this data dir.

    mkpaths resolves the L6.1 bus/ folder under the MK home the moment a project
    store exists. A data dir that gains its store while a file-mode bus is still
    running — mk-state, driven by an `answer`, creates that store — would move every
    bus file to a bus/ folder that holds none of this bus's tokens, so a client
    answered "bus down" (or reached the ROOT bus, which carries its own token set and
    refuses this project's) and a freshly minted lane token was written where no bus
    reads it (HTTP 401 on the lane's next request).

    A bus.json in the data dir is a file-mode bus started for exactly this data dir,
    so while its pid is alive that bus is the one serving it and wins — including when
    a root bus is running, whose bus/ folder also holds a bus.json. A dead pid is a
    leftover from before a switch and the resolution stands.
    """
    new_dir = bus_location(data_dir)
    if new_dir == data_dir:
        return new_dir
    file_json = os.path.join(data_dir, "bus.json")
    if not os.path.isfile(file_json):
        return new_dir
    if is_pid_alive((read_json(file_json, {}) or {}).get("pid")):
        return data_dir
    return new_dir


def project_slug_for(data_dir):
    """The root projects.slug whose data_dir is this path, or None in file mode.

    Validated against the root projects table (slug rule C11) BEFORE any
    project store is opened. A store with no matching root row is refused.
    The root store is <home>/mk.db; $MK_BUS_DIR is that home in tests. The read
    half is mkpaths.project_slug_of (L0); the "no row yet → register a derived
    slug" write half stays here, it is the bus's own write.
    """
    if not store_mode(data_dir):
        return None
    slug = _paths("project_slug_of", data_dir)
    if slug:
        return slug
    mod = _load_mkdb()
    home = _paths("root_home")  # $MK_BUS_DIR when the bus is pinned, else the MK home
    # No root row yet. Register this store so it joins the root bus: a present
    # mk.db is store mode (task/status/report), and file mode would refuse
    # those kinds. The slug is derived from the data dir path.
    real = os.path.realpath(data_dir)
    slug = "p" + hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]
    try:
        con = mod.open_root(home)
        try:
            con.execute(
                "INSERT OR IGNORE INTO projects(slug, name, repo, data_dir, mode, created) VALUES (?,?,?,?,?,?)",
                (slug, slug, "", real, "managed", now_iso()),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        return None
    return slug


def is_pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user (pid reused after our
        # server exited): treat as not our process.
        return False
    except OSError:
        return False
    # A zombie still answers kill(pid, 0). Reap it; it is dead.
    try:
        wpid, _status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except ChildProcessError:
        pass  # not our child: kill(pid, 0) already said it exists
    except OSError:
        pass
    return True


def stop_process(pid, timeout=5.0):
    if not is_pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _load_mk_config():
    """mk-config from the sibling script (a path load, never an import — each script stands alone),
    or None when that fails: a broken sibling install must leave the bus running on its defaults,
    not dead on an import error."""
    try:
        spec = importlib.util.spec_from_file_location("mk_config", os.path.join(HERE, "mk-config.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


_mk_config = _load_mk_config()


def owner_timeout_s(data_dir):
    """Seconds a silent owner may keep the bus alive: $MK_BUS_HEARTBEAT_TIMEOUT_SEC (test hook,
    unchanged precedence) > bus.owner_timeout_min from <data_dir>/matrixkoder.yaml (mk-config
    helpers; clamped to the schema's [5, 120] minutes, then ×60) > the schema default 30 min.
    Any failure to read the setting falls back to 1800.0 s, exactly the old hard-coded default."""
    env = os.environ.get("MK_BUS_HEARTBEAT_TIMEOUT_SEC")
    if env is not None:
        try:
            return float(env)
        except ValueError:
            pass  # an unparseable hook falls through to the settings rather than crash the monitor
    if _mk_config is not None:
        try:
            lines = _mk_config.read_lines(_paths("project_yaml", data_dir))
            row = _mk_config.row_for(lines, "bus.owner_timeout_min")
            minutes = _mk_config.value_of(lines, row)[1]
            return float(max(5, min(120, minutes))) * 60.0
        except Exception:
            pass
    return 1800.0


def _mk_state_task_set(data_dir, run, task, status, note, by="orchestrator"):
    """The ONE mk-state write the bus makes (cmd_answer's unblock and the lane-ask block both go
    through here): `task set <T> status=<s> --note <n> --by <by>`, $MK_STATE_CMD overridable for a
    self-test. -> (state_updated, mk_state_error): a failure is returned, never raised — the
    caller rode it along in its reply instead of failing the command that triggered the write."""
    mk_state_cmd = os.environ.get("MK_STATE_CMD") or os.path.join(HERE, "mk-state.py")
    cmd = [sys.executable, mk_state_cmd, "--data", data_dir, "--run", run, "task", "set", task,
           f"status={status}", "--note", note, "--by", by]
    try:
        child_env = os.environ.copy()
        # MK_BUS_DIR is the root-bus home. mk-state must not inherit it: its
        # own home resolution would then open the root store as a project db.
        child_env.pop("MK_BUS_DIR", None)
        child_env.pop("MK_BUS_TOKEN_FILE", None)
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, env=child_env)
        if p.returncode == 0:
            return True, None
        err = p.stderr.strip() or p.stdout.strip()
        # A store task with no rounds list makes mk-state raise KeyError. The
        # bus already stored the message; the side-write must not fail it.
        if "KeyError" in err:
            return False, None
        return False, err
    except Exception as e:
        return False, str(e)


# Todo 67: the words a lane `task` message with head `status <word>` may announce -> (task status
# it lands on, statuses it may start from). A lane moves its own task along the builder path
# only: it never sets done/merged (only the orchestrator does), so `done` caps at `verifying`,
# and a late or stale message after review, merged, qa, done, blocked, stalled, quota or
# provider changes nothing (mkdb.transition has no legal-transition check of its own).
_LANE_BUILD_FROM = ("planned", "ready", "fix", "building")
_LANE_VERIFY_FROM = ("building",)
LANE_STATUS_WORDS = {
    "in_progress": ("building", _LANE_BUILD_FROM),
    "verify": ("verifying", _LANE_VERIFY_FROM),
    "verifying": ("verifying", _LANE_VERIFY_FROM),
    "done": ("verifying", _LANE_VERIFY_FROM),
}


def _store_task_row(store, run, task):
    rows = store.store_select("SELECT status, round FROM tasks WHERE run=? AND id=?", (run, task))
    return rows[0] if rows else None


def _store_move(store, run, task, to, note, allowed_from=None):
    """Store-mode status write for the lane paths (todo status word, ask block, answer unblock).
    -> (state_updated, reason). Never raises: the message is already delivered, a refusal rides
    in the reply. `allowed_from` limits the move to a task in one of those statuses; any other
    status is left alone and the reason says why. A task already at `to` is a quiet no-op.
    The read and the write are two transactions (mkdb.transition takes no expected status and
    is not an owned file), so an orchestrator move inside that few-ms window can be overwritten."""
    try:
        row = _store_task_row(store, run, task)
        if row is None:
            return False, f"no task {run}:{task} in the store"
        if row["status"] == to:
            return False, None
        if allowed_from and row["status"] not in allowed_from:
            return False, f"task {task} is {row['status']}, {to} only from {'|'.join(allowed_from)}"
        store.store_transition(run, task, to, note=note, round_n=row.get("round") or 0)
        return True, None
    except Exception as e:
        return False, f"{to} refused: {e}"


def lane_status_word(text):
    """`status verify\\nround: 1\\n...` -> the word (a LANE_STATUS_WORDS key), else None."""
    head = (text or "").partition("\n")[0].strip()
    word = head[7:].strip() if head.startswith("status ") else ""
    return word if word in LANE_STATUS_WORDS else None


# A lane message body may carry `todo in_progress|completed|done <id>` lines (mk-builder todo).
# They move the lane's own subtasks, never another task.
LANE_TODO_RE = re.compile(r"(?m)^[ \t]*todo[ \t]+(in_progress|completed|done)[ \t]+(\S+)[ \t]*$")
_TODO_START_FROM = ("planned", "routed", "dispatched", "ready", "fix")


def _is_descendant(store, run, tid, root):
    """True when `tid` is a child, at any depth, of `root` in this run (parent chain, depth-capped)."""
    cur = tid
    for _ in range(20):
        rows = store.store_select("SELECT parent FROM tasks WHERE run=? AND id=?", (run, cur))
        cur = rows[0]["parent"] if rows else None
        if not cur:
            return False
        if cur == root:
            return True
    return False


def apply_lane_todos(store, run, task, text):
    """Move the lane's subtasks named by `todo <state> <id>` lines -> (updated ids, ignored ids).
    in_progress -> building; completed|done -> verifying (a lane never sets done; todo_done_status is ignored here). An id that is
    not a descendant of the token's task is ignored: a lane never moves another task.

    Every line is applied in order, so an id named twice ends in its LAST state; the returned
    list names each id once (review LOW), so a lane that moves one id twice does not report it twice."""
    updated, ignored = [], []
    for state, tid in LANE_TODO_RE.findall(text or ""):
        try:
            ours = tid != task and _is_descendant(store, run, tid, task)
        except Exception:
            ours = False
        if not ours:
            ignored.append(tid)
            continue
        if state == "in_progress":
            upd, _ = _store_move(store, run, tid, "building", f"bus todo {state}", allowed_from=_TODO_START_FROM)
        else:
            upd, _ = _store_move(store, run, tid, "verifying", f"bus todo {state}",
                                 allowed_from=("building",))
        if upd and tid not in updated:
            updated.append(tid)
    return updated, ignored


def matches_lane(msg, lane):
    if not lane or lane == "*":
        return True
    to_val = msg.get("to")
    if to_val:
        return to_val == lane or to_val in ("*", "all")
    task_val = msg.get("task")
    return task_val == lane


class BusState:
    def __init__(self, data_dir, default_run=None, bus_dir=None):
        self.project_dir = data_dir
        # bus_dir is where tokens and the pid file live. File mode: the data
        # dir. Root bus: the MK home, while messages still go to project_dir.
        self.data_dir = bus_dir or data_dir
        self.default_run = default_run
        # RLock: append_message/record_ack/get_pending hold self.lock and call
        # get_run_state, which also takes self.lock. A plain Lock would self-deadlock.
        self.lock = threading.RLock()
        # Notified on every appended message: the all-runs /events stream waits on it.
        self.any_cond = threading.Condition(self.lock)
        self.tokens = {}
        self.tokens_mtime = 0
        # Bus-file paths, pinned on first use (see _bus_file). mkpaths re-decides
        # bus_dir per call and a project store appearing mid-process would move a
        # live server's files under it, so this cache is the file-side twin of
        # use_store below.
        self._bus_files = {}
        # Decided once at process start. A store that appears mid-process is ignored
        # so a file-mode server never starts writing the store halfway through.
        self.use_store = store_mode(self.project_dir)
        self.store = StoreAdapter(self.project_dir) if self.use_store else None
        self.project = None
        self.runs = {}
        # (run, ask id) pairs whose task was already marked blocked (the /send lane-ask
        # path): guards the "once per ask" mk-state write, thread-safe under the same lock
        # as every other state change. Ask ids are per run ("m-1" exists in every run),
        # so the key carries the run too — an id-only key would let run A's m-1 suppress
        # run B's m-1 blocked-marking on the same bus (review MEDIUM 2).
        self.marked_asks = set()
        self.rung = set()
        self._ring_queue = []
        # Ring state: _ring_lock guards the queue, rung, in-flight ids and the
        # pending note events; _drain_lock serialises drains (one worker, plus
        # tests that call _drain_rings directly); _ring_wake wakes the worker.
        self._ring_lock = threading.RLock()
        self._drain_lock = threading.Lock()
        self._ring_wake = threading.Event()
        self._ring_inflight = set()
        self._ring_notes = []
        self._ring_skipped = set()
        # Message ids whose Esc has already been sent (review LOW: one Esc per message id, so a
        # ring retry of the same message cannot interrupt a second turn). Same lock as the queue.
        self._ring_esc = set()
        # Doorbell drain interval. A queued ring is delivered when the lane
        # goes idle even if no later message arrives to call _drain_rings.
        # Override with MK_BUS_RING_INTERVAL_SEC (seconds, minimum 0.05).
        self.ring_interval = 5.0
        try:
            self.ring_interval = max(0.05, float(os.environ.get("MK_BUS_RING_INTERVAL_SEC", "5")))
        except (TypeError, ValueError):
            self.ring_interval = 5.0
        # A project that gained its store while a bus was running keeps its tokens in
        # the data dir; this bus now resolves them elsewhere. One copy, before the first
        # read, so a restart after the flip does not 401 the tokens the old bus minted.
        self._adopt_file_mode_tokens()

    def _bus_file(self, name):
        """One bus-file path from mkpaths (bus_json / bus_tokens / bus_tokens_lock /
        ring_queue ...), resolved against this bus's project dir and then PINNED for
        this server's lifetime.

        mkpaths resolves bus_dir per call, and its rule flips the moment a project
        store exists: file mode keeps the data dir, store mode moves the files to the
        MK home's bus/ folder. A live server must not have its token file, ring queue
        or lock move under it, because the files it reads and writes were placed at the
        serve-time location by the very process it serves: mk-state, driven by an
        `answer`, creates <data>/mk.db while the bus runs, which moved a file-mode
        bus's tokens to a bus/ folder that holds none of its tokens, so every lane
        token answered 401 on a bus that was alive and delivering (Z4). First use is
        serve time (retire_orch_tokens + add_token in cmd_serve), so the pinned path is
        the one the bus registered its own tokens in.

        A BusState assembled with __new__ — a self-test stub — carries neither
        project_dir nor _bus_files, so both are read defensively and such a state
        resolves per call as before."""
        base = getattr(self, "project_dir", None) or self.data_dir
        pinned = getattr(self, "_bus_files", None)
        if pinned is None:
            return _paths(name, base)
        path = pinned.get(name)
        if path is None:
            path = _paths(name, base)
            pinned[name] = path
        return path

    def _adopt_file_mode_tokens(self):
        """C6 read-once for a project that gained its store while a bus was running.

        A bus started in file mode keeps its tokens in the data dir. When the project
        then gains a store (mk-state, driven by an `answer`), this bus resolves its
        token file to the MK home's bus/ folder — where the pre-L6.1 file sits at the
        home root, so the file-mode data-dir file is not one mkpaths' take-over sees
        and every lane token minted before the switch answers 401. Copy that file over
        once (atomic, 0600), never delete it: the rule L6.1 states for a moved path,
        applied to the move a file-mode bus makes.
        """
        new_path = self._bus_file("bus_tokens")
        old_path = os.path.join(self.project_dir, "bus-tokens.json")
        if old_path == new_path or os.path.exists(new_path) or not os.path.isfile(old_path):
            return
        try:
            with open(old_path, "rb") as f:
                blob = f.read()
        except OSError:
            return
        if not blob:
            return
        try:
            tokens = json.loads(blob.decode("utf-8"))
        except Exception:
            return
        if not isinstance(tokens, dict):
            return
        # A store-mode bus resolves every token's project (resolve_ctx refuses a token
        # with none) and a file-mode token had no project because there was no store.
        # This bus serves exactly this project, so the adopted rows name it.
        try:
            slug = project_slug_for(self.project_dir)
        except Exception:
            slug = None
        if slug:
            for entry in tokens.values():
                if isinstance(entry, dict) and not entry.get("project"):
                    entry["project"] = slug
        payload = json.dumps(tokens).encode("utf-8")
        dst_dir = os.path.dirname(new_path)
        fd = tmp = None
        try:
            os.makedirs(dst_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=dst_dir, prefix=".tokens-adopt-")
            with os.fdopen(fd, "wb") as f:
                fd = None  # closed by fdopen on every path out
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, new_path)
            tmp = None  # installed
        except OSError:
            if fd is not None:
                os.close(fd)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def mark_ask_once(self, run, ask_id):
        """True exactly once per (run, ask id): the caller may run its one blocked-marking write."""
        with self.lock:
            key = (run, ask_id)
            if key in self.marked_asks:
                return False
            self.marked_asks.add(key)
            return True

    def get_token(self, token_str):
        if not token_str or not isinstance(token_str, str):
            return None
        tokens_path = self._bus_file("bus_tokens")
        try:
            mtime = os.path.getmtime(tokens_path)
        except OSError:
            mtime = 0
        with self.lock:
            if mtime != self.tokens_mtime or not self.tokens:
                self.tokens = read_json(tokens_path, default={})
                self.tokens_mtime = mtime
            # Constant-time comparison across stored tokens. Compare encoded
            # bytes: hmac.compare_digest raises TypeError on non-ASCII input,
            # which must surface as 401, not a 500.
            try:
                token_bytes = token_str.encode("utf-8")
            except UnicodeEncodeError:
                return None
            for stored_tok, tok_info in self.tokens.items():
                try:
                    stored_bytes = stored_tok.encode("utf-8")
                except (UnicodeEncodeError, AttributeError):
                    continue
                if hmac.compare_digest(stored_bytes, token_bytes):
                    return tok_info
            return None

    def add_token(self, token_str, run, task, lane, role="lane", project=None):
        lock_path = self._bus_file("bus_tokens_lock")
        tokens_path = self._bus_file("bus_tokens")
        os.makedirs(self.data_dir, exist_ok=True)
        lfd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(lfd, 0o600)
        fcntl.flock(lfd, fcntl.LOCK_EX)
        try:
            tokens = read_json(tokens_path, default={})
            tokens[token_str] = {
                "token": token_str,
                "role": role,
                "run": run or "",
                "task": task or None,
                "lane": lane or None,
                "project": project,
                "created_at": now_iso()
            }
            write_json(tokens_path, tokens, mode=0o600)
            with self.lock:
                self.tokens = tokens
                self.tokens_mtime = os.path.getmtime(tokens_path)
        finally:
            fcntl.flock(lfd, fcntl.LOCK_UN)
            os.close(lfd)

    def retire_orch_tokens(self):
        """Drop every role=="orch" entry from <data>/bus-tokens.json: each serve
        start registers a fresh orchestrator token, and without retirement every
        orch token since the data dir's birth stayed valid forever (review LOW 4).
        Same bus-tokens.lock + write_json 0600 pattern as add_token/cmd_revoke;
        lane tokens are untouched."""
        lock_path = self._bus_file("bus_tokens_lock")
        tokens_path = self._bus_file("bus_tokens")
        os.makedirs(self.data_dir, exist_ok=True)
        lfd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(lfd, 0o600)
        fcntl.flock(lfd, fcntl.LOCK_EX)
        try:
            tokens = read_json(tokens_path, default={})
            if not isinstance(tokens, dict):
                tokens = {}
            kept = {k: v for k, v in tokens.items()
                    if not (isinstance(v, dict) and v.get("role") == "orch")}
            write_json(tokens_path, kept, mode=0o600)
            with self.lock:
                self.tokens = kept
                self.tokens_mtime = os.path.getmtime(tokens_path)
        finally:
            fcntl.flock(lfd, fcntl.LOCK_UN)
            os.close(lfd)

    def get_run_state(self, run, project_dir=None):
        if not run:
            run = self.default_run or "default"
        if not is_valid_id(run):
            raise Fail("invalid run id")

        # Store mode keys the cache by (project slug, run). Two projects can
        # share a run id; a run-only key would mix their messages. The dir is
        # kept so a file-mode server (one project) stays keyed as before.
        if self.use_store and project_dir:
            try:
                slug = project_slug_for(project_dir)
            except Fail:
                slug = None
            key = ((slug or project_dir), run)
        else:
            key = (run, project_dir or self.project_dir)
        with self.lock:
            if key in self.runs:
                return self.runs[key]

            base = project_dir or self.project_dir
            jsonl_path = os.path.join(_paths("run_dir", base, run), "bus.jsonl")
            messages = []
            by_id = {}
            max_seq = 0
            if self.use_store:
                loaded = self._load_store_run(run, store=StoreAdapter(base) if base != self.project_dir else self.store)
                messages, by_id, max_seq = loaded
            elif os.path.exists(jsonl_path):
                # Per-line tolerance: the file may be hand-edited between runs,
                # so one bad line (invalid UTF-8, non-int seq, non-hashable
                # reply_to) is skipped instead of failing replay for the run.
                with open(jsonl_path, "rb") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    try:
                        for raw in f:
                            line = raw.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line.decode("utf-8"))
                            except Exception:
                                continue
                            if not isinstance(obj, dict) or "id" not in obj or "seq" not in obj:
                                continue
                            # A non-str (or empty) id would poison by_id with an
                            # unhashable key and TypeError every later request for
                            # the run (review LOW 3, hand-edited files only): skip
                            # the whole line, id AND seq.
                            if not isinstance(obj.get("id"), str) or not obj.get("id"):
                                continue
                            seq = obj.get("seq")
                            if not isinstance(seq, int) or isinstance(seq, bool):
                                continue
                            if seq > max_seq:
                                max_seq = seq
                            if obj.get("kind") == "ack":
                                target_id = obj.get("reply_to")
                                level = obj.get("ack_level", 1)
                                if not isinstance(level, int) or isinstance(level, bool):
                                    level = 1
                                if isinstance(target_id, str) and target_id in by_id:
                                    cur = by_id[target_id].get("ack_level", 1)
                                    if not isinstance(cur, int) or isinstance(cur, bool) or cur < 1:
                                        cur = 1
                                    by_id[target_id]["ack_level"] = max(cur, level)
                            else:
                                messages.append(obj)
                                by_id[obj["id"]] = obj
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            rstate = {
                "run": run,
                "messages": messages,
                "by_id": by_id,
                "next_seq": max_seq + 1,
                "cond": threading.Condition(self.lock)
            }
            self.runs[key] = rstate
            return rstate

    def _load_store_run(self, run, store=None):
        """Rebuild one run from events chan=bus. SELECT only. Ids are row ids."""
        store = store or self.store
        rows = store.store_select(
            "SELECT id, run, task, t, kind, from_id, to_id, reply_to, text, artifact_id, meta_json"
            " FROM events WHERE chan=? AND run=? ORDER BY id",
            ("bus", run),
        )
        acks = store.store_select(
            "SELECT msg, lane, level FROM acks WHERE run=?",
            (run,),
        )
        ack_of = {}
        for a in acks:
            ack_of[a["msg"]] = max(ack_of.get(a["msg"], 1), int(a["level"] or 1))
        messages, by_id, max_seq = [], {}, 0
        for row in rows:
            eid = int(row["id"])
            if eid > max_seq:
                max_seq = eid
            meta = {}
            if row.get("meta_json"):
                try:
                    meta = json.loads(row["meta_json"])
                except (TypeError, ValueError):
                    meta = {}
            if not isinstance(meta, dict):
                meta = {}
            # Wire id is always m-<event row id>. meta.bus_id is ignored: older
            # rows stored the placeholder "pending", which collided in by_id.
            msg_id = f"m-{eid}"
            if row.get("kind") == "ack":
                target = row.get("reply_to")
                level = meta.get("ack_level", 1)
                if not isinstance(level, int) or isinstance(level, bool):
                    level = 1
                if isinstance(target, str) and target in by_id:
                    cur = by_id[target].get("ack_level", 1)
                    by_id[target]["ack_level"] = max(cur, level)
                continue
            msg = {
                "seq": eid,
                "id": msg_id,
                "ts": row.get("t") or "",
                "run": run,
                "task": row.get("task"),
                "from": row.get("from_id") or "",
                "to": row.get("to_id") or "",
                "kind": row.get("kind"),
                "reply_to": row.get("reply_to"),
                "text": row.get("text") or "",
                "ack_level": ack_of.get(msg_id, 1),
                "event_id": eid,
            }
            if row.get("artifact_id") is not None:
                msg["artifact_id"] = row["artifact_id"]
            messages.append(msg)
            by_id[msg["id"]] = msg
        return messages, by_id, max_seq

    def _append_jsonl(self, run_dir, line):
        """Append one JSON line to <run_dir>/bus.jsonl under flock.

        Message text may carry sensitive content, so the log is created 0600
        and <data>/runs and <data>/runs/<run> are 0700 (enforced even when
        they were created earlier with looser modes)."""
        os.makedirs(run_dir, exist_ok=True)
        os.chmod(run_dir, 0o700)
        with contextlib.suppress(OSError):
            os.chmod(os.path.dirname(run_dir), 0o700)
        jsonl_path = os.path.join(run_dir, "bus.jsonl")

        # Open with lock, ensuring previous line had newline. O_APPEND keeps
        # every write at EOF regardless of interleaved readers.
        fd = os.open(jsonl_path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                size = os.fstat(fd).st_size
                if size > 0 and os.pread(fd, 1, size - 1) != b"\n":
                    os.write(fd, b"\n")
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def append_message(self, run, task, from_val, to_val, kind, text, reply_to, project=None, urgent=False):
        if not is_valid_id(run):
            raise Fail("invalid run id")
        if task and not is_valid_id(task):
            raise Fail("invalid task id")

        store, project_dir = self.store_for(project)
        ring = None
        with self.lock:
            rstate = self.get_run_state(run, project_dir)
            if kind == "answer":
                # Authoritative duplicate-answer check under the lock: the early
                # check in do_POST runs unlocked, so two simultaneous answers to
                # one ask could both pass it (review LOW 5). append_message is
                # the one serialization point for writes — exactly one lands.
                for m in rstate["messages"]:
                    if m.get("kind") == "answer" and m.get("reply_to") == reply_to:
                        raise Fail("ask already answered")
            ts = now_iso()
            if self.use_store:
                # Row id is the wire seq and the SSE Last-Event-ID. The wire id
                # is derived as m-<row id> on load; meta does not store it.
                eid = int(store.store_append_event(
                    run, task=task or None, kind=kind, from_id=from_val, to_id=to_val,
                    reply_to=reply_to or None, text=text, t=ts,
                ))
                seq = eid
                msg_id = f"m-{seq}"
                rstate["next_seq"] = max(rstate["next_seq"], seq + 1)
                msg = {
                    "seq": seq, "id": msg_id, "ts": ts, "run": run,
                    "task": task or None, "from": from_val, "to": to_val,
                    "kind": kind, "reply_to": reply_to or None, "text": text,
                    "ack_level": 1, "event_id": seq, "project": project,
                }
            else:
                seq = rstate["next_seq"]
                rstate["next_seq"] += 1
                msg_id = f"m-{seq}"
                msg = {
                    "seq": seq, "id": msg_id, "ts": ts, "run": run,
                    "task": task or None, "from": from_val, "to": to_val,
                    "kind": kind, "reply_to": reply_to or None, "text": text,
                    "ack_level": 1,
                }
                run_dir = _paths("run_dir", project_dir, run)
                line = json.dumps(msg, separators=(",", ":")) + "\n"
                self._append_jsonl(run_dir, line)

            rstate["messages"].append(msg)
            rstate["by_id"][msg_id] = msg
            rstate["cond"].notify_all()
            self.any_cond.notify_all()
            ring = dict(msg, urgent=True) if urgent else msg
        if ring is not None:
            try:
                self._maybe_ring(ring)
            except Exception as e:
                sys.stderr.write("ring failed: %r\n" % (e,))
        return msg



    def store_for(self, project):
        """The store of a named project on the root bus.

        `project` is checked against the root projects table (slug rule) before
        the store is opened. File mode has no projects and ignores the name.
        An empty slug is refused in store mode: only the explicit host project
        name maps to the host store. There is no default-project fallback.

        The project DB path itself is resolved by mkpaths.project_db (L0): one
        rule for the project store, root row lookup included. A slug with no
        root row carrying a data_dir has no store, so it is an unknown project.

        The bus's OWN project resolves the same way (L6.1, todo 108): a host
        started with a legacy `--data` follows `mk-migrate --switch` instead of
        writing the legacy store — see `_resolve_own_store`.
        """
        if not self.use_store:
            return self.store, self.project_dir
        if not isinstance(project, str) or project == "" or not SLUG_RE.match(project):
            raise Fail("invalid project slug")
        if project == self.project:
            # todo 108: the host project resolves like every other one, through
            # mkpaths, so `mk-migrate --switch` moves this bus's own store too.
            return self._resolve_own_store()
        db = _paths("project_db", slug=project)
        if not db:
            raise Fail("unknown project")
        data_dir = os.path.dirname(db)
        return StoreAdapter(data_dir), data_dir

    def _resolve_own_store(self):
        """The host project's (store, folder), re-resolved through mkpaths on every call.

        A bus started with a legacy `--data` kept writing the legacy store after the project
        switched (todo 108). The store now resolves through `mkpaths.project_db(slug)`, whose
        slug form follows the root row's `db_path` (L2's switch marker) and is memoised by the
        root store's stamp, so a switch is picked up on the next own-project lookup — no
        restart, and one stat on the hot path. Pre-migration the resolve answers exactly
        today's store and folder, so nothing moves.

        The moved run's cached state goes with the store: it holds the pre-switch messages and
        seq counter, and the switched store already carries them (mk-migrate copies the project
        DB whole), so a reload rebuilds both from one place.
        """
        project = self.project
        try:
            db = _paths("project_db", slug=project)
        except Fail:
            db = None
        if not db or os.path.realpath(db) == os.path.realpath(
                _load_mkpaths().db_in(self.project_dir)):
            return self.store, self.project_dir  # no root row, or still today's store
        folder = os.path.dirname(db)
        with self.lock:
            stale = [key for key in self.runs
                     if isinstance(key, tuple) and key[0] in (project, self.project_dir)]
            for key in stale:
                del self.runs[key]
            self.project_dir = folder
            self.store = StoreAdapter(folder)
        sys.stderr.write("bus: own project %s moved to %s\n" % (project, folder))
        return self.store, folder

    def _maybe_ring(self, msg):
        """C13b: queue a doorbell for an unsolicited message. Never runs herdr.

        A solicited answer (one whose reply_to names an ask) needs no ring.
        A nudge, an info, or an answer with no waiter does. This only records
        the message in the ring queue and wakes the ring worker: every herdr
        call runs on that worker, so a slow herdr cannot block a request. The
        worker types the fixed line when herdr says the lane is idle. A working
        lane depends on the agent kind (`agent` in herdr's agent list):

          kind                     working lane
          claude, codex (STEER)    rung at once: typed input joins the running turn
          opencode, agy, other     stays queued until idle
          any kind, urgent message Esc (send-keys) first, then the doorbell

        STEER kinds default to claude,codex; $MK_BUS_STEER_KINDS (comma list,
        empty = none) overrides. `urgent` comes from `send --urgent` and is honoured only from the
        orchestrator — the one sender allowed to interrupt a lane's turn. Esc interrupts the turn,
        so it is for real redirects only, and it goes out once per message id: a ring retry of the
        same message must not interrupt a second turn. One ring per message id, recorded as an
        event chan=bus kind=ring. With the bus down this method is never
        called, so nothing rings.
        """
        if not self.use_store:
            return
        kind = msg.get("kind")
        if kind not in ("nudge", "info", "answer"):
            return
        if kind == "answer" and msg.get("reply_to"):
            return  # a waiter is already blocked on this answer
        to_id = msg.get("to")
        if not to_id or to_id == "orch":
            return
        # `urgent` is the orchestrator's alone: only the orchestrator may interrupt a working
        # lane's turn. `from` is the sender identity the token fixed — a lane token's is its own
        # task id, never "orch" — so another sender's urgent is still queued and delivered, and
        # reaches the drain un-urgent.
        if msg.get("urgent") and msg.get("from") != "orch":
            msg = dict(msg, urgent=False)
        self._ring_enqueue(msg)

    def _ring_enqueue(self, msg):
        """Add to the ring queue under the ring lock (once per id), then wake the worker."""
        with self._ring_lock:
            mid = msg["id"]
            if mid in self.rung or mid in self._ring_inflight or any(q["id"] == mid for q in self._ring_queue):
                return
            self._ring_queue.append(dict(msg, queued_at=msg.get("queued_at") or time.time()))
            self._ring_cap_locked()
        self._ring_wake.set()

    def _ring_cap_locked(self):
        """Hold the newest RING_QUEUE_MAX rows; each dropped row gets one ring-dropped note."""
        while len(self._ring_queue) > RING_QUEUE_MAX:
            old = self._ring_queue.pop(0)
            self._ring_notes.append((old, "ring-dropped", "queue over %d rows" % RING_QUEUE_MAX))

    def _flush_ring_notes(self):
        """Write queued note events (chan=note). Worker thread only."""
        with self._ring_lock:
            notes, self._ring_notes = self._ring_notes, []
        for msg, kind, text, *extra in notes:
            try:
                store, _ = self.store_for(msg.get("project"))
                store.store_append_event(
                    msg["run"], task=None, kind=kind, from_id="bus", to_id=msg.get("to"),
                    reply_to=str(msg["id"]), text=text, chan="note",
                    meta=dict(extra[0] if extra else {}, msg=msg["id"]),
                )
            except Exception as e:
                sys.stderr.write("ring note failed: %r\n" % (e,))

    def _drain_rings(self):
        """Ring every queued message whose lane is idle. Runs on the ring worker.

        The whole queue is CLAIMED under the ring lock before any subprocess
        runs (moved out of the queue, ids marked in-flight), so a second drain
        or a second sender cannot ring the same message. One `herdr agent list`
        serves the whole queue.
        """
        with self._drain_lock:
            self._flush_ring_notes()
            now = time.time()
            with self._ring_lock:
                batch = []
                for m in self._ring_queue:
                    if m["id"] in self.rung:
                        continue
                    if now - m.get("queued_at", now) > RING_MAX_AGE:
                        self._ring_notes.append((m, "ring-dropped", "expired after 1 hour"))
                        continue
                    batch.append(m)
                self._ring_queue = []
                self._ring_inflight = {m["id"] for m in batch}
            self._save_ring_queue(batch)  # a kill mid-herdr-call must not lose the claimed rows
            still = []
            try:
                agents = self._herdr_agents() if batch else []
                for msg in batch:
                    try:
                        self._drain_one(msg, agents, still)
                    except Exception as e:
                        sys.stderr.write("ring failed: %r\n" % (e,))
                        self._ring_raised(msg, still)
            finally:
                with self._ring_lock:
                    self._ring_queue = still + self._ring_queue
                    self._ring_inflight = set()
                    self._ring_cap_locked()
            self._save_ring_queue()
            self._flush_ring_notes()

    def _ring_raised(self, msg, still):
        """A claimed message whose drain raised: retry once on the next tick, then give up with a note."""
        attempts = msg["_attempts"] = msg.get("_attempts", 0) + 1
        final = attempts >= 2
        with self._ring_lock:
            self._ring_notes.append((
                msg, "ring-failed",
                "ring raised (attempt %d%s)" % (attempts, ", giving up" if final else ""),
                {"final": final},
            ))
            if final:
                self.rung.add(msg["id"])  # never queued again
            else:
                self.rung.discard(msg["id"])
        if not final:
            still.append(msg)

    def _drain_one(self, msg, agents, still):
        """One claimed message: ring it, keep it queued (append to `still`), or drop it."""
        if not agents:
            still.append(msg)  # herdr missing or failing: try again next tick
            return
        hit = self._pick_pane(msg, agents)
        if hit == "ambiguous":
            with self._ring_lock:
                first = msg["id"] not in self._ring_skipped
                self._ring_skipped.add(msg["id"])
                if first:
                    self._ring_notes.append((msg, "ring-skipped", "ambiguous pane"))
            still.append(msg)
            return
        if not hit:
            return  # no pane for this lane: nothing to ring
        agent, pane = hit
        status = agent.get("agent_status")
        if status == "working":
            if msg.get("urgent"):
                # One Esc per message id: a ring retry of the same message must not interrupt a
                # second turn (the first Esc already ended the one it was aimed at).
                with self._ring_lock:
                    esc_done = msg["id"] in self._ring_esc
                if not esc_done and not self._send_esc(pane):
                    still.append(msg)  # Esc failed: stay queued rather than type into a busy turn
                    return
                with self._ring_lock:
                    self._ring_esc.add(msg["id"])
            elif not self._steers(agent):
                still.append(msg)  # queue until idle
                return
        if status not in ("idle", "done", "working"):  # working only gets here when steered or urgent
            return  # unknown state: do not ring; herdr reports a finished turn as "done"
        with self._ring_lock:
            self.rung.add(msg["id"])
        if self._ring(msg, pane):
            return
        attempts = msg["_attempts"] = msg.get("_attempts", 0) + 1
        final = attempts >= 2
        with self._ring_lock:
            self._ring_notes.append((
                msg, "ring-failed",
                "herdr prompt failed (attempt %d%s)" % (attempts, ", giving up" if final else ""),
                {"final": final},
            ))
            if not final:
                self.rung.discard(msg["id"])
        if not final:
            still.append(msg)  # one retry on the next tick
        return

    @staticmethod
    def _steers(agent):
        env = os.environ.get("MK_BUS_STEER_KINDS")
        kinds = STEER_KINDS_DEFAULT if env is None else {k.strip() for k in env.split(",") if k.strip()}
        return agent.get("agent") in kinds

    def _send_esc(self, pane):
        """Interrupt a working lane's turn: `herdr agent send-keys <pane> esc` (fake seam: $MK_BUS_HERDR). True on exit 0."""
        if not pane or pane.startswith("-") or not PANE_RE.match(pane):
            return False
        fake = os.environ.get("MK_BUS_HERDR")
        if not fake and shutil.which("herdr") is None:
            return False
        argv = [sys.executable, fake, "send-keys", pane, "esc"] if fake else ["herdr", "agent", "send-keys", pane, "esc"]
        try:
            return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False

    def _lane_worktrees(self, run, project, task):
        """Every lane worktree path a token for (run, project, task) was minted for.

        Store mode keeps tokens in <bus dir>/bus-tokens.json, which records the
        lane directory per token. The worktree path is how a real pane is
        identified (herdr agents carry `cwd`), so the ring can find the lane's
        pane without the orchestrator passing a pane id. The token must match
        the message's run AND project AND task: the same task id in another run
        or project is another lane. A task can still hold more than one live
        token (re-armed in a fresh worktree), so all of them are candidates.
        """
        try:
            tokens = read_json(self._bus_file("bus_tokens"), default={})
        except Exception:
            return set()
        lanes = set()
        for v in (tokens or {}).values():
            if (isinstance(v, dict) and v.get("task") == task and v.get("lane")
                    and v.get("run") == run and (not project or v.get("project") == project)):
                lanes.add(os.path.realpath(v["lane"]))
        return lanes

    def _herdr_agents(self):
        """Every agent dict from the real `herdr agent list`.

        $MK_BUS_HERDR stays the test seam: a program printing the same JSON,
        invoked exactly as today. With it unset the REAL `herdr` binary on
        PATH is called. Missing binary, non-zero exit, bad JSON or a timeout
        (3 s) all return [] — no ring and no crash (the bus-down rule's twin).
        """
        fake = os.environ.get("MK_BUS_HERDR")
        if fake:
            argv = [sys.executable, fake]
        else:
            if shutil.which("herdr") is None:
                return []
            argv = ["herdr", "agent", "list"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        if data.get("error") and "result" not in data:
            return []
        agents = ((data.get("result") or {}).get("agents"))
        if agents is None:
            agents = data.get("agents")
        return agents if isinstance(agents, list) else []

    @staticmethod
    def _pane_id(agent):
        return str(agent.get("pane_id") or agent.get("pane") or agent.get("name") or "")

    def _pick_pane(self, msg, agents):
        """(agent, pane) of the lane agent for the message, "ambiguous", or None.

        Match order: the agent whose cwd IS a lane worktree of this (run,
        project, task) (the real-lane case, todo 66a / F4), then the agent
        named after the task, then the old `cwd.endswith("/" + task)` guess
        (kept for the fake). Two or more agents in the winning tier (builder
        and reviewer share the lane cwd) are told apart by the lane's armed
        agent name, then by the pane recorded in the lane's
        .matrixkoder-watch.json; if that does not decide, "ambiguous" and the
        caller does not ring.
        """
        task = msg.get("to")
        lanes = self._lane_worktrees(msg.get("run"), msg.get("project"), task)
        valid = [a for a in agents if isinstance(a, dict)]
        by_cwd = [a for a in valid if lanes and a.get("cwd") and os.path.realpath(a["cwd"]) in lanes]
        by_name = [a for a in valid if (a.get("name") or "") == task]
        by_suffix = [a for a in valid if (a.get("cwd") or "").endswith("/" + task)]
        for tier in (by_cwd, by_name, by_suffix):
            if len(tier) == 1:
                return tier[0], self._pane_id(tier[0])
            if tier:
                return self._disambiguate(tier, lanes)
        return None

    def _disambiguate(self, tier, lanes):
        armed = watch_pane = None
        tokens = read_json(self._bus_file("bus_tokens"), default={})
        here = {os.path.realpath(a["cwd"]) for a in tier if a.get("cwd")} & lanes
        for lane in here:
            watch = read_json(os.path.join(lane, ".matrixkoder-watch.json"), default={})
            if isinstance(watch, dict):
                armed = armed or watch.get("agent")
                watch_pane = watch_pane or watch.get("pane")
        for v in (tokens or {}).values():
            if isinstance(v, dict) and v.get("lane") and os.path.realpath(v["lane"]) in here:
                armed = armed or v.get("agent")
        if armed:
            named = [a for a in tier if a.get("name") == armed]
            if len(named) == 1:
                return named[0], self._pane_id(named[0])
        if watch_pane:
            known = [a for a in tier if self._pane_id(a) == watch_pane and a.get("agent_status") is not None
                     and a.get("cwd") and os.path.realpath(a["cwd"]) in lanes]
            if len(known) == 1:
                return known[0], self._pane_id(known[0])
        return "ambiguous"

    def _ring(self, msg, pane):
        """Record the ring BEFORE typing it; True when herdr typed it.

        A crash between the two leaves kind=ring with meta typed=false. On
        restart that row is queued and rung once the lane is idle. A row with
        typed=true is never typed again. MK_BUS_RING_KILL, when set to a path,
        is a test seam: the process exits after the record and before the type.
        A retry (msg carries `_ring_eid`) does not record a second ring row.
        """
        skill = os.environ.get("MK_BUS_SKILL_DIR") or os.path.join(HERE, "..", "child-skills", "mk-builder")
        line = "Run: python3 %s/scripts/mk-builder.py inbox %s" % (os.path.abspath(skill), RING_ACT)
        store, _ = self.store_for(msg.get("project"))
        eid = msg.get("_ring_eid")
        if eid is None:
            eid = msg["_ring_eid"] = store.store_append_event(
                msg["run"], task=None, kind="ring",
                from_id="bus", to_id=msg.get("to"), reply_to=msg["id"], text=line,
                meta={"typed": False, "msg": msg["id"]},
            )
            kill = os.environ.get("MK_BUS_RING_KILL")
            if kill:
                try:
                    open(kill, "w").write(str(msg.get("id")))
                except OSError:
                    pass
                os._exit(86)
        return self._type_ring(msg, line, eid, store, pane)

    def _type_ring(self, msg, line, eid, store, pane):
        """Type the fixed doorbell line into the lane's pane; True when herdr returned 0.

        One ring path: the test seam (`$MK_BUS_HERDR`, invoked as today) or the
        REAL `herdr agent prompt <pane> <line>` (5 s). The line is fixed — a
        message body never becomes typed text (C13a/C13b). The pane id comes
        from herdr, but it is still checked (`herdr agent prompt --help` does
        not document `--`, so the id must not read as an option). Only a typed
        ring gets the sibling row kind=ring-typed that restart treats as done;
        a failure is the caller's ring-failed note. Never raises on herdr.
        """
        if not pane or pane.startswith("-") or not PANE_RE.match(pane):
            return False
        try:
            fake = os.environ.get("MK_BUS_HERDR")
            argv = [sys.executable, fake, "prompt", pane, line] if fake else ["herdr", "agent", "prompt", pane, line]
            if not fake and shutil.which("herdr") is None:
                return False
            # DEVNULL, not inherited: a bus started by `ensure` has a dead stdout pipe, and herdr's reply would SIGPIPE it (-13)
            if subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode != 0:
                return False
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False
        store.store_append_event(
            msg["run"], task=None, kind="ring-typed",
            from_id="bus", to_id=msg.get("to"), reply_to=str(msg["id"]), text=str(eid or ""),
            meta={"typed": True, "msg": msg["id"]},
        )
        return True

    def _ring_queue_path(self):
        return self._bus_file("ring_queue")

    def _save_ring_queue(self, claimed=()):
        """Persist queued rings so a restart can drain them once the lane is idle.

        The queue is messages that arrived while the lane was working, plus the
        batch a drain has claimed and not finished. It is not the ring event
        itself; _ring still records that before typing. restore_rings reads it.
        """
        if not self.use_store:
            return
        with self._ring_lock:
            snapshot = list(claimed) + list(self._ring_queue)
        try:
            write_json(self._ring_queue_path(), snapshot, mode=0o600)
        except OSError as e:
            sys.stderr.write("ring queue save failed: %r\n" % (e,))

    def _load_ring_queue(self):
        """Rows saved by _save_ring_queue; anything unreadable is no rows."""
        saved = read_json(self._ring_queue_path(), default=[])
        return [m for m in saved if isinstance(m, dict) and m.get("id")] if isinstance(saved, list) else []

    def restore_rings(self):
        """On startup: a recorded-but-untyped ring is queued (never typed here); a typed one never again.

        The idle check stays in _drain_rings, so a ring recorded just before a
        crash is not typed into an agent that is working now.
        """
        if not self.use_store:
            return
        rows = self.store.store_select(
            "SELECT id, run, kind, to_id, reply_to, text, meta_json FROM events"
            " WHERE kind IN ('ring','ring-typed','ring-failed') ORDER BY id",
            (),
        )
        done = set()
        pending = []
        for row in rows:
            meta = {}
            if row.get("meta_json"):
                try:
                    meta = json.loads(row["meta_json"])
                except (TypeError, ValueError):
                    meta = {}
            if not isinstance(meta, dict):
                meta = {}
            mid = row.get("reply_to") or meta.get("msg")
            if row.get("kind") == "ring-typed" or (row.get("kind") == "ring-failed" and meta.get("final")):
                done.add(mid)
            elif row.get("kind") == "ring" and mid:
                pending.append((mid, row))
        self.rung |= {d for d in done if d}
        # A saved row carries the whole message and its queued_at; a store `ring` row
        # (recorded, not typed) adds the event id so the retry does not record a second.
        saved = {m["id"]: m for m in self._load_ring_queue()}
        now = time.time()
        for mid, row in pending:
            if mid in done:
                continue
            msg = saved.pop(mid, None) or {
                "id": mid, "run": row.get("run"), "to": row.get("to_id"), "project": self.project,
            }
            msg["_ring_eid"] = row.get("id")
            self._ring_enqueue(msg)
        for mid, msg in saved.items():
            if mid in done:
                continue
            if now - msg.get("queued_at", now) > RING_MAX_AGE:
                with self._ring_lock:
                    self._ring_notes.append((msg, "ring-dropped", "expired after 1 hour"))
                continue
            self._ring_enqueue(msg)

    def ring_worker(self, stop_event):
        """Run every herdr call: drain the doorbell queue when woken and on a fixed interval.

        _maybe_ring only queues and wakes this thread. The interval delivers a
        ring that was queued while the lane was working, after the lane
        becomes idle and no further message shows up.
        """
        while not stop_event.is_set():
            self._ring_wake.wait(self.ring_interval)
            self._ring_wake.clear()
            if stop_event.is_set():
                break
            try:
                self._drain_rings()
            except Exception as e:
                sys.stderr.write("ring drain failed: %r\n" % (e,))

    def record_ack(self, run, target_id, ack_level, from_val, to_val="orch", project=None, project_dir=None):
        if not is_valid_id(run):
            raise Fail("invalid run id")

        store = self.store
        if self.use_store:
            store, project_dir = self.store_for(project)
        else:
            project_dir = project_dir or self.project_dir
        with self.lock:
            rstate = self.get_run_state(run, project_dir)
            target_msg = rstate["by_id"].get(target_id)
            target_task = target_msg.get("task") if target_msg else None

            ts = now_iso()
            if self.use_store:
                lane = from_val or "client"
                # C13e / todo F7: ONE ack row per (run, msg, lane). The acks
                # table dedupes itself in mkdb, but every call also wrote a
                # kind=ack EVENT row, which is what a replay turned into two
                # ack rows for one answer (m-4/m-5, m-9/m-10). A repeat at the
                # same or a lower level is ignored: no event, no write. A
                # genuine upgrade (1 -> 3) still records.
                already = store.store_select(
                    "SELECT level FROM acks WHERE run=? AND msg=? AND lane=?",
                    (run, target_id, lane),
                )
                prior = 0
                if already:
                    try:
                        prior = int(already[0]["level"] or 0)
                    except (TypeError, ValueError):
                        prior = 0
                if prior >= ack_level:
                    if target_msg:
                        target_msg["ack_level"] = max(target_msg.get("ack_level", 1), prior)
                    return False
                eid = int(store.store_append_event(
                    run, task=target_task, kind="ack",
                    from_id=from_val or "client", to_id=to_val or "orch",
                    reply_to=target_id, text=f"ack {ack_level}", t=ts,
                    meta={"ack_level": ack_level},
                ))
                store.store_append_ack(run, target_id, lane, ack_level, t=ts)
                seq = eid
                rstate["next_seq"] = max(rstate["next_seq"], seq + 1)
            else:
                seq = rstate["next_seq"]
                rstate["next_seq"] += 1
                ack_id = f"m-{seq}"
                ack_rec = {
                    "seq": seq, "id": ack_id, "ts": ts, "run": run,
                    "task": target_task, "from": from_val or "client",
                    "to": to_val or "orch", "kind": "ack", "reply_to": target_id,
                    "text": f"ack {ack_level}", "ack_level": ack_level,
                }
                run_dir = _paths("run_dir", project_dir, run)
                line = json.dumps(ack_rec, separators=(",", ":")) + "\n"
                self._append_jsonl(run_dir, line)

            if target_msg:
                target_msg["ack_level"] = max(target_msg.get("ack_level", 1), ack_level)

            rstate["cond"].notify_all()
            return True

    def validate_store_kind(self, kind, task, text):
        """Checks that must pass before any store write for status/report/review.

        A failure here stores nothing and streams nothing.
        """
        if kind == "status":
            if not task:
                raise Fail("status requires a task")
            to = (text or "").strip()
            if to not in STATUS_VALUES:
                raise Fail(f"invalid status {to!r}")
            return
        if not task:
            raise Fail(f"{kind} requires a task")
        body = (text or "").replace(SEP, "")
        # X2.3: a `report`/`review` body above STORE_TEXT_MAX is paged, not refused — the whole
        # text still goes into ONE artifact, so the ceiling is the artifact writer's. Every
        # other store kind keeps the 128 KiB contract.
        ceiling = PAGED_TEXT_MAX if kind in ("report", "review") else STORE_TEXT_MAX
        if len(body.encode("utf-8")) > ceiling:
            raise Fail(f"text exceeds {ceiling // 1024} KiB")

    def apply_store_kind(self, msg, store=None):
        """status -> transition(); report/review -> artifact linked to the bus event.

        Called only in store mode, after the bus event row exists, and only
        after validate_store_kind and (for status) a successful transition().
        """
        kind = msg["kind"]
        run, task = msg["run"], msg.get("task")
        if kind == "status":
            return {"change_no": msg.get("change_no")}
        if not task:
            raise Fail(f"{kind} requires a task")
        body = msg["text"].replace(SEP, "")
        ceiling = PAGED_TEXT_MAX if kind in ("report", "review") else STORE_TEXT_MAX
        if len(body.encode("utf-8")) > ceiling:
            raise Fail(f"text exceeds {ceiling // 1024} KiB")
        round_n = 0
        head, sep, rest = body.partition("\n")
        if head.startswith("round:"):
            try:
                round_n = int(head[6:].strip())
            except ValueError:
                round_n = 0
            body = rest
        try:
            art = (store or self.store).store_put_artifact(
                run, task, round_n, kind, body, event_id=msg.get("event_id"),
            )
        except Exception as e:
            raise Fail(str(e))
        msg["artifact_id"] = art["id"]
        msg["rev"] = art["rev"]
        return {"artifact": art}

    def get_pending(self, run, project_dir=None):
        base = project_dir or self.project_dir
        with self.lock:
            if run:
                if not is_valid_id(run):
                    raise Fail("invalid run id")
                runs_to_check = [self.get_run_state(run, base)]
            else:
                runs_dir = _paths("runs_dir", base)
                names = set()
                if os.path.exists(runs_dir):
                    for d in os.listdir(runs_dir):
                        if os.path.isdir(os.path.join(runs_dir, d)) and is_valid_id(d):
                            names.add(d)
                runs_to_check = [self.get_run_state(r, base) for r in names]

            pending = []
            for rstate in runs_to_check:
                # Hand-edited lines may carry a non-hashable reply_to; skip those.
                answered = {
                    m["reply_to"] for m in rstate["messages"]
                    if m.get("kind") == "answer"
                    and isinstance(m.get("reply_to"), str)
                }
                for m in rstate["messages"]:
                    if m.get("kind") == "ask" and m["id"] not in answered:
                        pending.append(m)
            return pending


class RequestHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.request.settimeout(30.0)

    def log_message(self, format, *args):
        pass

    def send_json(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, status, error_msg):
        self.send_json(status, {"ok": False, "error": error_msg})

    def lane_can_ack(self, tok, target_msg):
        """One shared check for every ack path (POST /ack and POST /send with
        kind=ack): a lane token can only ack messages addressed to its own lane
        (`to == tok.task` or `task == tok.task`). Orchestrator tokens always may."""
        if tok.get("role") == "orch":
            return True
        tok_task = tok.get("task")
        return target_msg.get("to") == tok_task or target_msg.get("task") == tok_task

    def do_OPTIONS(self):
        # B7: CORS preflight is rejected with JSON, never answered with CORS
        # headers (BaseHTTPRequestHandler would otherwise answer 501 HTML).
        self.send_error_json(403, "browser origins rejected")

    # B25: PUT/DELETE/PATCH would otherwise fall through to the stdlib 501
    # HTML page — errors are JSON on this bus. Constant 405, no auth path.
    def do_PUT(self):
        self.send_error_json(405, "method not allowed")

    def do_DELETE(self):
        self.send_error_json(405, "method not allowed")

    def do_PATCH(self):
        self.send_error_json(405, "method not allowed")

    def do_HEAD(self):
        # Same 405, but HEAD must never carry a body: send the status and
        # JSON headers like send_json does, skipping the body write.
        self.send_response(405)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def validate_request(self):
        if "Origin" in self.headers:
            self.send_error_json(403, "browser origins rejected")
            return False

        host = self.headers.get("Host", "")
        host_name = host.split(":")[0].lower() if host else ""
        if host_name not in ("127.0.0.1", "localhost"):
            self.send_error_json(400, "invalid host header")
            return False

        return True

    def check_auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth or not auth.startswith("Bearer "):
            self.send_error_json(401, "unauthorized")
            return None
        tok_str = auth[len("Bearer "):].strip()
        tok = self.server.bus_state.get_token(tok_str)
        if not tok:
            self.send_error_json(401, "unauthorized")
            return None
        return tok

    def resolve_ctx(self, tok, asked=None):
        """ONE project context from the token, used by every later read or write.

        Store-mode tokens must name a project that is in the root projects
        table. A query or body project that disagrees is a 403 and changes
        nothing. File mode has no project and keeps the server's data dir.
        Returns (project, project_dir, store) or None after sending the error.
        """
        bus = self.server.bus_state
        if not bus.use_store:
            return (None, bus.project_dir, None)
        project = tok.get("project")
        if not isinstance(project, str) or not project:
            self.send_error_json(403, "forbidden: token has no project")
            return None
        # An explicit empty slug is not "use the token's project". store_for
        # refuses "" (no default-project fallback); do that here too so the
        # write never lands.
        if asked == "":
            self.send_error_json(403, "invalid project slug")
            return None
        if asked is not None and asked != project:
            self.send_error_json(403, "forbidden: project mismatch")
            return None
        try:
            store, project_dir = bus.store_for(project)
        except Fail as e:
            self.send_error_json(403, str(e))
            return None
        return (project, project_dir, store)

    def do_GET(self):
        try:
            if not self.validate_request():
                return

            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

            if path == "/health":
                uptime = time.time() - getattr(self.server, "started_at", time.time())
                # "data" lets ensure IDENTIFY our server: a live pid alone can
                # be pid reuse, and a reused pid serving another data dir must
                # not be trusted (review LOW 1). Loopback-only, same user.
                self.send_json(200, {
                    "ok": True,
                    "status": "healthy",
                    "pid": os.getpid(),
                    "uptime_sec": round(uptime, 3),
                    "data": self.server.bus_state.data_dir,
                    "project_dir": self.server.bus_state.project_dir,
                    "project": self.server.bus_state.project,
                })
                return

            tok = self.check_auth()
            if not tok:
                return

            is_orch = tok.get("role") == "orch"

            if path == "/events":
                # Disable socket timeout for long-lived SSE stream
                self.request.settimeout(None)

                # `run=*` is the orchestrator's "every run of my project" stream
                # (watch without --run). Store mode only: event ids are project-wide
                # there, so one Last-Event-ID covers all runs.
                all_runs = is_orch and params.get("run") == "*"
                if all_runs and not self.server.bus_state.use_store:
                    self.send_error_json(400, "run * needs the project store")
                    return
                if params.get("run") and not all_runs and not is_valid_id(params["run"]):
                    self.send_error_json(400, "invalid run id")
                    return
                if params.get("lane") and params["lane"] != "*" and not is_valid_id(params["lane"]):
                    self.send_error_json(400, "invalid task id")
                    return
                if params.get("task") and not is_valid_id(params["task"]):
                    self.send_error_json(400, "invalid task id")
                    return

                if is_orch:
                    lane = params.get("lane") or params.get("task") or ""
                    run = params.get("run") or tok.get("run") or self.server.bus_state.default_run
                else:
                    lane = tok.get("task") or ""
                    run = tok.get("run") or ""
                    if params.get("lane") and params.get("lane") != lane:
                        self.send_error_json(403, "forbidden: lane mismatch")
                        return
                    if params.get("run") and params.get("run") != run:
                        self.send_error_json(403, "forbidden: run mismatch")
                        return

                if not all_runs and not is_valid_id(run):
                    self.send_error_json(400, "invalid run id")
                    return

                ctx = self.resolve_ctx(tok, params.get("project"))
                if ctx is None:
                    return
                _project, project_dir, store = ctx

                last_id_str = self.headers.get("Last-Event-ID", "").strip()
                if last_id_str.startswith("m-"):
                    try:
                        last_seq = int(last_id_str[2:])
                    except ValueError:
                        last_seq = 0
                elif last_id_str.isdigit():
                    last_seq = int(last_id_str)
                else:
                    last_seq = 0

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                bus = self.server.bus_state
                if all_runs:
                    cond = bus.any_cond
                else:
                    rstate = bus.get_run_state(run, project_dir)
                    cond = rstate["cond"]

                run_ids = []  # run=* only: grows when a new run id appears

                def fresh(last):
                    """Messages after `last` for the lane, oldest first; holds `cond`'s lock."""
                    if all_runs:
                        states = [bus.get_run_state(r, project_dir) for r in run_ids]
                    else:
                        states = [rstate]
                    got = [m for st in states for m in st["messages"] if m["seq"] > last and matches_lane(m, lane)]
                    got.sort(key=lambda m: m["seq"])
                    return got

                while True:
                    to_send = []
                    for attempt in (0, 1):
                        if all_runs:
                            # The run list is read once per wake, outside bus.lock.
                            run_ids.extend(r["id"] for r in store.store_select("SELECT id FROM runs") if r["id"] not in run_ids)
                        with cond:
                            for m in fresh(last_seq):
                                to_send.append(dict(m))
                                m["ack_level"] = max(m.get("ack_level", 1), 2)
                                last_seq = m["seq"]
                            if not to_send and not attempt:
                                cond.wait(timeout=15.0)
                        if to_send or attempt:
                            break

                    # The lock is released before the socket write. A slow SSE
                    # client must not hold the run lock or a store connection.
                    if to_send:
                        for m in to_send:
                            sse_id = m.get("event_id", m["seq"])
                            ev = f"id: {sse_id}\nevent: message\ndata: {json.dumps(m)}\n\n"
                            try:
                                self.wfile.write(ev.encode("utf-8"))
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError, OSError):
                                return
                    else:
                        # Keepalive comment
                        try:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            return

            elif path == "/wait":
                if params.get("run") and not is_valid_id(params["run"]):
                    self.send_error_json(400, "invalid run id")
                    return
                if params.get("lane") and params["lane"] != "*" and not is_valid_id(params["lane"]):
                    self.send_error_json(400, "invalid task id")
                    return
                if params.get("task") and not is_valid_id(params["task"]):
                    self.send_error_json(400, "invalid task id")
                    return

                if is_orch:
                    lane = params.get("lane") or params.get("task") or ""
                    run = params.get("run") or tok.get("run") or self.server.bus_state.default_run
                else:
                    lane = tok.get("task") or ""
                    run = tok.get("run") or ""
                    if params.get("lane") and params.get("lane") != lane:
                        self.send_error_json(403, "forbidden: lane mismatch")
                        return
                    if params.get("run") and params.get("run") != run:
                        self.send_error_json(403, "forbidden: run mismatch")
                        return

                if not is_valid_id(run):
                    self.send_error_json(400, "invalid run id")
                    return

                ctx = self.resolve_ctx(tok, params.get("project"))
                if ctx is None:
                    return
                _project, project_dir, _store = ctx

                after_str = params.get("after", "0").strip()
                if after_str.startswith("m-"):
                    try:
                        after_seq = int(after_str[2:])
                    except ValueError:
                        after_seq = 0
                elif after_str.isdigit():
                    after_seq = int(after_str)
                else:
                    after_seq = 0

                try:
                    timeout = float(params.get("timeout", "30"))
                    if not math.isfinite(timeout):
                        timeout = 30.0
                except (ValueError, TypeError):
                    timeout = 30.0
                # B11/B18: clamp strictly to 1 <= timeout <= 90.
                timeout = min(max(timeout, 1.0), 90.0)

                deadline = time.time() + timeout
                rstate = self.server.bus_state.get_run_state(run, project_dir)

                # Build the payload under the lock, send after releasing it:
                # rstate["cond"] wraps the global bus_state lock, and writing
                # to a stalled socket while holding it would block appends.
                response = None
                with rstate["cond"]:
                    while response is None:
                        matching = None
                        for m in rstate["messages"]:
                            if m["seq"] > after_seq and matches_lane(m, lane):
                                matching = m
                                break
                        if matching:
                            matching["ack_level"] = max(matching.get("ack_level", 1), 2)
                            snap = dict(matching)
                            response = {"ok": True, "message": snap, "messages": [snap]}
                        else:
                            remaining = deadline - time.time()
                            if remaining <= 0:
                                response = {"ok": True, "message": None, "messages": []}
                            else:
                                rstate["cond"].wait(timeout=min(remaining, 0.5))
                self.send_json(200, response)

            elif path == "/pending":
                if not is_orch:
                    self.send_error_json(403, "forbidden: lane tokens cannot read pending asks")
                    return

                run = params.get("run")
                if run == "*":
                    run = None
                if run and not is_valid_id(run):
                    self.send_error_json(400, "invalid run id")
                    return

                ctx = self.resolve_ctx(tok, params.get("project"))
                if ctx is None:
                    return
                _project, project_dir, _store = ctx
                pending = self.server.bus_state.get_pending(run, project_dir)
                # B14: response echoes the queried run (or "*" when unscoped).
                self.send_json(200, {"ok": True, "run": run if run else "*", "pending": pending})

            else:
                self.send_error_json(404, "not found")
        except Exception:
            self.send_error_json(500, "internal server error")

    def do_POST(self):
        try:
            if not self.validate_request():
                return

            tok = self.check_auth()
            if not tok:
                return

            # C6: the root token is read-only on EVERY write, before the body
            # is parsed. role "root" is not an orchestrator and not a lane.
            if tok.get("role") == "root":
                self.send_error_json(403, "forbidden: root token is read-only")
                return

            is_orch = tok.get("role") == "orch"

            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            try:
                content_len = int(self.headers.get("Content-Length", 0))
            except ValueError:
                content_len = 0

            limit = PAGED_TEXT_MAX + 4096 if self.server.bus_state.use_store else 65536
            if content_len > limit:
                self.send_error_json(413, "payload too large")
                return

            body_bytes = self.rfile.read(content_len) if content_len > 0 else b"{}"
            try:
                body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            except Exception:
                self.send_error_json(400, "invalid json")
                return

            if not isinstance(body, dict):
                self.send_error_json(400, "request body must be a JSON object")
                return

            if path == "/send":
                if "run" in body:
                    if not is_valid_id(body.get("run")):
                        self.send_error_json(400, "invalid run id")
                        return
                if "task" in body and body.get("task"):
                    if not is_valid_id(body.get("task")):
                        self.send_error_json(400, "invalid task id")
                        return
                if "from" in body and body.get("from") and body.get("from") != "orch":
                    if not is_valid_id(body.get("from")):
                        self.send_error_json(400, "invalid task id")
                        return
                if "to" in body and body.get("to") and body.get("to") != "orch":
                    if not is_valid_id(body.get("to")):
                        self.send_error_json(400, "invalid task id")
                        return

                kind = body.get("kind")
                allowed = STORE_KINDS if self.server.bus_state.use_store else FILE_KINDS
                if kind not in allowed:
                    self.send_error_json(400, "invalid kind")
                    return

                text = body.get("text")
                if text is None or not isinstance(text, str):
                    self.send_error_json(400, "text must be a string")
                    return
                try:
                    text_len = len(text.encode("utf-8"))
                except UnicodeEncodeError:
                    # json.loads happily builds a lone surrogate from a \ud800
                    # escape; it can never encode to UTF-8 (the jsonl append
                    # would raise) -> reject at the door as 400, never a 500.
                    self.send_error_json(400, "text is not valid UTF-8")
                    return
                # X2.3: a `report`/`review` body above STORE_TEXT_MAX is paged by the bus, not
                # refused; every other kind keeps the store's 128 KiB ceiling.
                paged_kind = kind in ("report", "review")
                text_max = (PAGED_TEXT_MAX if paged_kind else STORE_TEXT_MAX) \
                    if self.server.bus_state.use_store else FILE_TEXT_MAX
                if text_len > text_max:
                    self.send_error_json(
                        400,
                        "text exceeds 64 KiB" if text_max == FILE_TEXT_MAX
                        else ("text exceeds 256 KiB" if paged_kind else "text exceeds 128 KiB"))
                    return
                # X2.3: an oversized `report`/`review` body travels PAGED — split at PAGE_BYTES
                # and joined with the contract's SEP separator — and the store strips the
                # separators, so the artifact is the whole body. A body inside one page keeps
                # the plain wire shape every other message uses.
                if paged_kind and text_len > PAGE_BYTES:
                    text = page_body(text)
                # B18: text is non-empty for ask/answer/info; empty is allowed
                # only for ack.
                if kind in NONEMPTY_KINDS and not text:
                    self.send_error_json(400, "text must be non-empty")
                    return

                urgent = body.get("urgent", False)
                if not isinstance(urgent, bool):
                    self.send_error_json(400, "urgent must be a boolean")
                    return
                reply_to = body.get("reply_to")
                if reply_to is not None and not isinstance(reply_to, str):
                    self.send_error_json(400, "reply_to must be a string or null")
                    return
                # B18: reply_to MUST be null for ask/info; required for ack and
                # answer. For ack/answer it must name an existing message in the
                # target run (checked after run resolution below, because the
                # run determines which bus.jsonl to look in).
                if kind in ("ask", "info", "task", "status", "commit", "report", "review", "nudge") and reply_to is not None:
                    self.send_error_json(400, "reply_to must be null for ask/info")
                    return
                if kind == "ack" and not reply_to:
                    self.send_error_json(400, "reply_to required for ack")
                    return
                # B13: ack_level is a strict integer 1..3 (bool is not an int
                # here even though True == 1 in Python).
                ack_level = None
                if kind == "ack":
                    ack_level = body.get("ack_level", 3)
                    if not isinstance(ack_level, int) or isinstance(ack_level, bool) or ack_level not in (1, 2, 3):
                        self.send_error_json(400, "invalid ack_level")
                        return

                if not is_orch:
                    # Lane token scoping
                    tok_project = tok.get("project")
                    asked = body.get("project") or tok_project
                    if tok_project and asked and tok_project != asked:
                        self.send_error_json(403, "forbidden: project mismatch")
                        return
                    if kind == "answer":
                        self.send_error_json(403, "lane tokens cannot send answers")
                        return
                    if kind == "status":
                        self.send_error_json(403, "lane tokens cannot set status")
                        return

                    tok_run = tok.get("run")
                    tok_task = tok.get("task")

                    if "run" in body and body["run"] != tok_run:
                        self.send_error_json(403, "forbidden: run mismatch")
                        return
                    if "task" in body and body["task"] != tok_task:
                        self.send_error_json(403, "forbidden: task mismatch")
                        return
                    if "from" in body and body["from"] != tok_task:
                        self.send_error_json(403, "forbidden: from mismatch")
                        return
                    if "to" in body and body["to"] != "orch":
                        self.send_error_json(403, "forbidden: lane to must be orch")
                        return

                    run = tok_run
                    task = tok_task
                    from_val = tok_task
                    to_val = "orch"
                else:
                    # Orchestrator token: scoped to one project.
                    tok_project = tok.get("project")
                    asked = body.get("project") or tok_project
                    if tok_project and asked and tok_project != asked:
                        self.send_error_json(403, "forbidden: project mismatch")
                        return
                    # m-16 (E2 6): an orchestrator /send MUST name its run.
                    # Falling back to the token's run or the server default
                    # silently filed the message under the wrong run, so the
                    # reply looked fine while the lane never saw it. A lane
                    # token is different: its run comes from the token.
                    if not body.get("run"):
                        self.send_error_json(400, "missing run")
                        return
                    run = body.get("run") or tok.get("run") or self.server.bus_state.default_run
                    task = body.get("task")
                    from_val = body.get("from") or "orch"
                    to_val = body.get("to") or task or "orch"

                if not run or run == "*":
                    self.send_error_json(400, "missing run")
                    return

                if not is_valid_id(run):
                    self.send_error_json(400, "invalid run id")
                    return
                if task and not is_valid_id(task):
                    self.send_error_json(400, "invalid task id")
                    return

                ctx = self.resolve_ctx(tok, body.get("project"))
                if ctx is None:
                    return
                project, project_dir, store = ctx
                rstate = self.server.bus_state.get_run_state(run, project_dir)

                # Validate reply_to for answer
                if kind == "answer":
                    if not reply_to:
                        self.send_error_json(400, "reply_to required for answer")
                        return
                    target_msg = rstate["by_id"].get(reply_to)
                    if not target_msg:
                        self.send_error_json(400, "reply_to message not found")
                        return
                    if target_msg.get("kind") != "ask":
                        self.send_error_json(400, "reply_to must name an ask message")
                        return
                    if project and target_msg.get("project") and target_msg.get("project") != project:
                        self.send_error_json(403, "forbidden: project mismatch")
                        return
                    # Check if already answered
                    for m in rstate["messages"]:
                        if m.get("kind") == "answer" and m.get("reply_to") == reply_to:
                            self.send_error_json(400, "ask already answered")
                            return

                # Validate reply_to for ack
                if kind == "ack":
                    if reply_to not in rstate["by_id"]:
                        self.send_error_json(400, "reply_to message not found")
                        return
                    # A lane token can only ack messages addressed to its own
                    # lane — the same check POST /ack applies (one shared rule).
                    if not is_orch and not self.lane_can_ack(tok, rstate["by_id"][reply_to]):
                        self.send_error_json(403, "cannot ack messages for other lanes")
                        return
                    # Ack records are never messages: persist an ack record like
                    # POST /ack does, so live state and replayed state agree.
                    recorded = self.server.bus_state.record_ack(
                        run=run,
                        target_id=reply_to,
                        ack_level=ack_level,
                        from_val=from_val,
                        to_val=to_val,
                        project=project,
                        project_dir=project_dir,
                    )
                    target_msg = rstate["by_id"][reply_to]
                    self.send_json(200, {
                        "ok": True,
                        "id": reply_to,
                        "ack_level": target_msg.get("ack_level"),
                        "ack_recorded": True,
                        "duplicate": not recorded,
                    })
                    return

                # append_message re-checks "ask already answered" under the
                # lock: a losing racer surfaces here as Fail -> 400, never a
                # 500 from the handler's blanket except.
                bus = self.server.bus_state
                change_no = None
                if bus.use_store and kind in ("status", "report", "review"):
                    # Validate, and for status transition, BEFORE any bus event.
                    # A failure returns 400 with nothing stored and nothing streamed.
                    try:
                        bus.validate_store_kind(kind, task, text)
                        if kind == "status":
                            change_no = store.store_transition(
                                run, task, (text or "").strip(), note="bus status",
                            )
                    except Fail as e:
                        self.send_error_json(400, str(e))
                        return
                    except Exception as e:
                        self.send_error_json(400, str(e))
                        return
                try:
                    msg = bus.append_message(
                        run=run, task=task, from_val=from_val, to_val=to_val,
                        kind=kind, text=text, reply_to=reply_to,
                        project=project, urgent=urgent,
                    )
                except Fail as e:
                    # Transition already committed: the state change is the truth.
                    # Do not report ok; the next status resend is idempotent.
                    if change_no is not None:
                        sys.stderr.write(f"bus status event append failed after transition: {e}\n")
                        self.send_error_json(500, "status applied but the bus event was not stored")
                        return
                    self.send_error_json(400, str(e))
                    return
                except Exception as e:
                    if change_no is not None:
                        sys.stderr.write(f"bus status event append failed after transition: {e}\n")
                        self.send_error_json(500, "status applied but the bus event was not stored")
                        return
                    raise
                if change_no is not None:
                    msg["change_no"] = change_no
                if bus.use_store and kind in ("status", "report", "review"):
                    try:
                        extra = bus.apply_store_kind(msg, store=store)
                    except Fail as e:
                        if kind == "status":
                            sys.stderr.write(f"bus status follow-up failed after transition: {e}\n")
                            self.send_error_json(500, "status applied but the bus event was not stored")
                            return
                        self.send_error_json(400, str(e))
                        return
                    self.send_json(200, {"ok": True, "message": msg, **extra})
                    return
                if kind == "ask" and not is_orch:
                    # A lane ask means the lane is parked on the orchestrator: set its task
                    # blocked so the dashboard wake surfaces it (answer sets building again).
                    # append_message has returned, so no lock is held here; the once-per-ask-id
                    # guard is the bus state's set; an mk-state failure rides in the reply and
                    # never fails the POST (the ask itself was already delivered).
                    ask_reply = {"state_updated": False, "mk_state_error": None}
                    if self.server.bus_state.mark_ask_once(run, msg["id"]):
                        note = f"bus ask {msg['id']}: {text[:80]}"
                        if bus.use_store:
                            # Todo 68d: the store is the state here; only a building task blocks.
                            ask_reply["state_updated"], ask_reply["mk_state_error"] = _store_move(
                                store, run, task, "blocked", note, allowed_from=("building",))
                        elif not store_mode(bus.project_dir):
                            # File mode has no state to move (L4 removed the file views), and
                            # mk-state's store-always-on behaviour would CREATE the project
                            # store the bus resolves its own files through — moving a live
                            # bus's token file so every lane token answers 401 (Z4). A
                            # project that gains a store moves through the branch above.
                            pass
                        else:
                            ask_reply["state_updated"], ask_reply["mk_state_error"] = _mk_state_task_set(
                                self.server.bus_state.data_dir, run, task, "blocked", note, by="bus")
                    self.send_json(200, {"ok": True, "message": msg, **ask_reply})
                elif bus.use_store and kind == "answer" and task:
                    # Todo 68d: the answer puts a blocked task back to building (file mode does
                    # it in cmd_answer through mk-state).
                    upd, err = _store_move(store, run, task, "building", "bus answer", allowed_from=("blocked",))
                    if err and "only from blocked" in err:
                        err = None  # not blocked: nothing to undo, not a failure
                    self.send_json(200, {"ok": True, "message": msg, "state_updated": upd, "mk_state_error": err})
                elif bus.use_store and kind == "task" and not is_orch and task and (
                        lane_status_word(text) or LANE_TODO_RE.search(text)):
                    # Todo 67: a lane `task` message "status in_progress|verify|done" moves its own
                    # task (the token fixes `task`) along the builder path; the message stays, so
                    # the dedupe key still reads it. Not a legal move -> 200 and state_ignored.
                    word = lane_status_word(text)
                    reply = {"ok": True, "message": msg}
                    if word:
                        to, from_ok = LANE_STATUS_WORDS[word]
                        upd, why = _store_move(store, run, task, to, f"bus status {word}", allowed_from=from_ok)
                        reply.update(state_updated=upd, mk_state_error=None)
                        if why:
                            reply["state_ignored"] = why
                    if LANE_TODO_RE.search(text):
                        reply["todo_updated"], ignored = apply_lane_todos(store, run, task, text)
                        if ignored:
                            reply["todo_ignored"] = ignored
                    self.send_json(200, reply)
                else:
                    self.send_json(200, {"ok": True, "message": msg})

            elif path == "/ack":
                target_id = body.get("id") or body.get("reply_to")
                if not target_id or not isinstance(target_id, str):
                    self.send_error_json(400, "missing id")
                    return

                ack_level = body.get("ack_level", 3)
                # Strict integer: bool passes isinstance(int) but True == 1
                # would silently persist as JSON true.
                if not isinstance(ack_level, int) or isinstance(ack_level, bool) or ack_level not in (1, 2, 3):
                    self.send_error_json(400, "invalid ack_level")
                    return

                if "run" in body:
                    if not is_valid_id(body.get("run")):
                        self.send_error_json(400, "invalid run id")
                        return

                if not is_orch:
                    tok_run = tok.get("run")
                    if "run" in body and body["run"] != tok_run:
                        self.send_error_json(403, "forbidden: run mismatch")
                        return
                    run = tok_run
                    from_val = tok.get("task")
                else:
                    run = body.get("run") or tok.get("run")
                    if run == "*":
                        run = None
                    from_val = "orch"

                if not run:
                    self.send_error_json(400, "missing run")
                    return
                if not is_valid_id(run):
                    self.send_error_json(400, "invalid run id")
                    return

                ctx = self.resolve_ctx(tok, body.get("project"))
                if ctx is None:
                    return
                project, project_dir, _store = ctx
                rstate = self.server.bus_state.get_run_state(run, project_dir)
                target_msg = rstate["by_id"].get(target_id)
                if not target_msg:
                    self.send_error_json(400, "message not found")
                    return

                # One shared rule with POST /send kind=ack (lane_can_ack):
                # a lane token can only ack messages addressed to its own
                # lane; orchestrator tokens always may.
                if not self.lane_can_ack(tok, target_msg):
                    self.send_error_json(403, "cannot ack messages for other lanes")
                    return

                recorded = self.server.bus_state.record_ack(
                    run=run,
                    target_id=target_id,
                    ack_level=ack_level,
                    from_val=from_val,
                    to_val=target_msg.get("from") or "orch",
                    project=project,
                    project_dir=project_dir,
                )
                self.send_json(200, {"ok": True, "id": target_id, "ack_level": ack_level,
                                     "duplicate": not recorded})

            else:
                self.send_error_json(404, "not found")
        except Exception:
            self.send_error_json(500, "internal server error")


class BusServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass


def heartbeat_monitor(server, bus_dir, data_dir, stop_event):
    check_interval = float(os.environ.get("MK_BUS_HEARTBEAT_CHECK_SEC", "60.0"))
    timeout = owner_timeout_s(bus_dir)  # env hook > bus.owner_timeout_min > schema default 30 min
    bus_json_path = _paths("bus_json", data_dir)

    while not stop_event.wait(check_interval):
        try:
            if not os.path.exists(bus_json_path):
                # Missing bus.json counts as stale -> self exit
                threading.Thread(target=server.shutdown, daemon=True).start()
                break
            bdata = read_json(bus_json_path)
            if not bdata:
                continue
            # Root bus: projects maps slug -> last heartbeat. The process
            # exits only when NO project has heartbeated within the timeout.
            # File mode has no projects map; owner_heartbeat is the one clock.
            projects = bdata.get("projects")
            if isinstance(projects, dict) and projects:
                latest = 0.0
                for v in projects.values():
                    try:
                        latest = max(latest, float(v))
                    except (TypeError, ValueError):
                        pass
                hb = latest
            else:
                hb = float(bdata.get("owner_heartbeat", 0))
            if time.time() - hb > timeout:
                try:
                    if bdata.get("pid") == os.getpid():
                        os.remove(bus_json_path)
                except OSError:
                    pass
                threading.Thread(target=server.shutdown, daemon=True).start()
                break
        except Exception:
            pass


def cmd_serve(args):
    data_dir = resolve_data(getattr(args, "data", None))
    os.makedirs(data_dir, exist_ok=True)
    # Store mode: the lock and the pid file live in the MK home, shared by
    # every project. File mode keeps them in the data dir.
    bus_dir = bus_location(data_dir)
    os.makedirs(bus_dir, exist_ok=True)
    lock_path = _paths("bus_lock", data_dir)
    bus_json_path = _paths("bus_json", data_dir)

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(lock_fd, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        out({"ok": False, "error": "bus already running"})
        sys.exit(2)

    port = getattr(args, "port", 0) or 0
    prefer = getattr(args, "prefer_port", 0) or 0
    server = None
    if not port and prefer:
        with contextlib.suppress(OSError):
            server = BusServer(("127.0.0.1", prefer), RequestHandler)
    if server is None:
        server = BusServer(("127.0.0.1", port), RequestHandler)
    server.started_at = time.time()
    server.bus_state = BusState(data_dir, getattr(args, "run", None), bus_dir=bus_dir)
    actual_port = server.server_address[1]

    orch_token = secrets.token_urlsafe(32)
    # One live orchestrator identity per data dir: retire earlier orch tokens
    # BEFORE registering the fresh one (review LOW 4), so the previous
    # orchestrator's token answers 401 after a restart instead of living
    # forever.
    server.bus_state.retire_orch_tokens()
    server.bus_state.add_token(orch_token, getattr(args, "run", "*") or "*", "orch", "orch", role="orch", project=(project_slug_for(data_dir) if bus_dir != data_dir else None))

    project = None
    if bus_dir != data_dir:
        project = project_slug_for(data_dir)
    server.bus_state.project = project
    bus_data = {
        "pid": os.getpid(),
        "port": actual_port,
        "started": server.started_at,
        "owner_heartbeat": server.started_at,
        "token": orch_token,
    }
    if project:
        bus_data["project"] = project
        bus_data["projects"] = {project: server.started_at}
    write_json(bus_json_path, bus_data, mode=0o600)

    stop_event = threading.Event()
    hb_thread = threading.Thread(
        target=heartbeat_monitor,
        args=(server, bus_dir, data_dir, stop_event),
        daemon=True
    )
    hb_thread.start()
    try:
        server.bus_state.restore_rings()
    except Exception as e:
        sys.stderr.write("ring restore failed: %r\n" % (e,))
    ring_thread = threading.Thread(
        target=server.bus_state.ring_worker, args=(stop_event,), daemon=True,
    )
    ring_thread.start()

    def handle_signal(sig, frame):
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        out({"ok": True, "pid": os.getpid(), "port": actual_port})
        server.serve_forever()
    finally:
        stop_event.set()
        try:
            server.server_close()
        except Exception:
            pass
        try:
            if os.path.exists(bus_json_path):
                d = read_json(bus_json_path)
                # A second serve that lost the lock must not delete the live
                # bus.json on its way out. Only the process that published it
                # removes it, and only while it still owns the lock.
                if d and d.get("pid") == os.getpid():
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        os.remove(bus_json_path)
                    except (BlockingIOError, OSError):
                        pass
        except OSError:
            pass
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except Exception:
            pass
    sys.exit(0)


def cmd_ensure(args):
    data_dir = resolve_data(getattr(args, "data", None))
    os.makedirs(data_dir, exist_ok=True)
    bus_dir = bus_location(data_dir)
    os.makedirs(bus_dir, exist_ok=True)
    ensure_lock_path = os.path.join(bus_dir, "bus-ensure.lock")
    bus_json_path = _paths("bus_json", data_dir)

    # Serialize ensure calls with dedicated flock
    efd = os.open(ensure_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(efd, 0o600)
    fcntl.flock(efd, fcntl.LOCK_EX)
    try:
        # Check if server is already running. The recorded pid alone proves
        # nothing: after a SIGKILL or a reboot the OS may have recycled it for
        # any same-user process, and ensure would fail forever on a stale
        # bus.json (review LOW 1). /health now names the data dir it serves,
        # so the probe must IDENTIFY our server, not just find a live pid. A
        # REFUSED connection also means pid reuse: our server, even SIGSTOPed,
        # keeps its listening socket (the kernel completes the handshake from
        # the backlog; only the response stalls), so refusal ≠ timeout. A
        # timeout or any other probe error on a live pid keeps the old rule:
        # the server holds bus.lock and a respawn would just exit — retry to
        # the deadline, then fail without touching bus.json (review LOW 5,
        # self-test 10b).
        if os.path.exists(bus_json_path):
            bdata = read_json(bus_json_path)
            pid = (bdata or {}).get("pid")
            port = (bdata or {}).get("port")
            if pid and port and is_pid_alive(pid):
                # This bus is ours when it serves the project THIS data dir names. After
                # `mk-migrate --switch` the bus follows the root row's db_path and reports the
                # C2 folder, so a caller still holding the legacy dir must recognise it
                # instead of tearing a healthy bus down (L6.1, todo 108).
                mine = {os.path.realpath(data_dir),
                        os.path.realpath(_paths("project_folder", data_dir))}
                stale = False
                probe_deadline = time.time() + 5.0
                while time.time() < probe_deadline:
                    refused = False
                    try:
                        req = urllib.request.Request(
                            f"http://127.0.0.1:{port}/health",
                            headers={"Host": f"127.0.0.1:{port}"}
                        )
                        with urllib.request.urlopen(req, timeout=1.0) as resp:
                            if resp.status == 200:
                                h = json.loads(resp.read().decode("utf-8"))
                                if isinstance(h, dict) and h.get("project_dir") in mine:
                                    # Healthy and provably ours: refresh heartbeat.
                                    bdata["owner_heartbeat"] = time.time()
                                    proj = getattr(args, "_project", None)
                                    if proj:
                                        projects = bdata.get("projects")
                                        if not isinstance(projects, dict):
                                            projects = {}
                                        projects[proj] = time.time()
                                        bdata["projects"] = projects
                                    write_json(bus_json_path, bdata, mode=0o600)
                                    res = {"ok": True, "pid": pid, "port": port, "started": False}
                                    out(res)
                                    return res
                            # 200 from a different data dir: another mk-bus
                            # owns this pid (pid reuse across data dirs) —
                            # bus.json is stale, respawn ours.
                            stale = True
                    except urllib.error.URLError as e:
                        # urllib wraps refusal as URLError(reason=ConnectionRefusedError);
                        # http.client can also raise it bare.
                        refused = isinstance(getattr(e, "reason", None), ConnectionRefusedError)
                    except ConnectionRefusedError:
                        refused = True
                    except Exception:
                        pass
                    if stale or refused or not is_pid_alive(pid):
                        # Stale (reused pid), nothing listening, or the pid
                        # died under our probe: all fall through to
                        # remove + respawn.
                        break
                    time.sleep(0.2)
                else:
                    # pid alive but /health never answered within the retry
                    # window. The server holds bus.lock, so a new serve would
                    # just exit; refuse instead of destroying live state.
                    raise Fail("bus process alive but not answering health probes")

        # Not running (no bus.json, unreadable bus.json, dead pid, or a stale
        # bus.json whose pid was reused): start a detached serve process
        old_port = None
        if os.path.exists(bus_json_path):
            old_bdata = read_json(bus_json_path)
            old_port = old_bdata.get("port") if isinstance(old_bdata, dict) else None
            old_pid = old_bdata.get("pid") if isinstance(old_bdata, dict) else None
            # A live root bus publishes this file for every project. Deleting
            # it while that pid is alive makes the next ensure blind. Only a
            # dead pid's file is stale.
            if not (isinstance(old_pid, int) and is_pid_alive(old_pid)):
                with contextlib.suppress(OSError):
                    os.remove(bus_json_path)

        cmd = [sys.executable, os.path.abspath(__file__), "serve", "--data", data_dir]
        if getattr(args, "run", None):
            cmd.extend(["--run", args.run])
        if getattr(args, "port", None):
            cmd.extend(["--port", str(args.port)])
        elif type(old_port) is int and 1024 <= old_port <= 65535:  # bus.json is untrusted: bool/huge ports would crash serve
            # armed lanes hold the old url; reuse its port when free
            cmd.extend(["--prefer-port", str(old_port)])

        p = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )

        deadline = time.time() + 10.0
        while time.time() < deadline:
            # A root bus is one process for every project. A second serve
            # exits with "bus already running"; that is the live bus, not a
            # failed start. Adopt it.
            if p.poll() is not None:
                try:
                    line = (p.stdout.read() or b"").decode("utf-8", "replace")
                except Exception:
                    line = ""
                if "bus already running" in line and os.path.exists(bus_json_path):
                    bdata = read_json(bus_json_path) or {}
                    pid, port = bdata.get("pid"), bdata.get("port")
                    if pid and port and is_pid_alive(pid):
                        proj = project_slug_for(data_dir)
                        if proj:
                            projects = bdata.get("projects")
                            if not isinstance(projects, dict):
                                projects = {}
                            projects[proj] = time.time()
                            bdata["projects"] = projects
                            bdata["owner_heartbeat"] = time.time()
                            write_json(bus_json_path, bdata, mode=0o600)
                        res = {"ok": True, "pid": pid, "port": port, "started": False}
                        out(res)
                        return res
            if os.path.exists(bus_json_path):
                try:
                    bdata = read_json(bus_json_path)
                    pid = bdata.get("pid")
                    port = bdata.get("port")
                    if pid and port:
                        req = urllib.request.Request(
                            f"http://127.0.0.1:{port}/health",
                            headers={"Host": f"127.0.0.1:{port}"}
                        )
                        with urllib.request.urlopen(req, timeout=1.0) as resp:
                            if resp.status == 200:
                                started = (pid == p.pid)
                                res = {"ok": True, "pid": pid, "port": port, "started": started}
                                out(res)
                                return res
                except Exception:
                    pass
            time.sleep(0.05)

        raise Fail("server failed to start within timeout")
    finally:
        fcntl.flock(efd, fcntl.LOCK_UN)
        os.close(efd)


def cmd_heartbeat(args):
    data_dir = resolve_data(getattr(args, "data", None))
    bus_dir = bus_location(data_dir)
    bus_json_path = _paths("bus_json", data_dir)
    # A project heartbeat. In root-bus mode the file records one timestamp per
    # project; the process exits only when EVERY project is past the timeout.
    project = None
    if bus_dir != data_dir:
        project = project_slug_for(data_dir)
        args._project = project
    # A missing or pid-less bus.json is a dead bus, not a hard failure:
    # ensure below restarts it (todo 38). Only refuse when there is nothing
    # ensure can restart, which there isn't — ensure creates the file.
    bdata = read_json(bus_json_path) if os.path.exists(bus_json_path) else None
    if bdata is not None and not isinstance(bdata, dict):
        raise Fail("bus down")

    # A SIGKILLed server leaves bus.json behind, so a bare timestamp refresh kept a dead
    # bus "alive" forever (todo 38). ensure probes /health under its lock, refreshes
    # owner_heartbeat when healthy and respawns (replaying bus.jsonl) when not.
    with contextlib.redirect_stdout(io.StringIO()):  # ensure prints its own line; we print one
        ens = cmd_ensure(args)
    res = {"ok": True, "restarted": ens["started"], "pid": ens["pid"], "port": ens["port"]}
    out(res)
    return res


def _mirror_legacy_tokens(data_dir, entry):
    """R2 HIGH 2: an adopted pre-L6.1 bus still validates tokens against the LEGACY
    <home>/bus-tokens.json, so a token minted only under bus/ would 401 on it until the
    bus restarts. When the legacy bus is alive (legacy bus.json, live pid) the same entry
    is merged into the legacy file under the legacy lock, with the write pattern the old
    bus's own writers use — so the running bus accepts the new token at once. When no
    legacy bus is alive nothing is written at any legacy path (no legacy file is ever
    created by L6.1 code). Returns True when the mirror happened."""
    legacy_json = _paths("legacy_bus_json", data_dir)
    if not os.path.exists(legacy_json):
        return False
    bdata = read_json(legacy_json)
    pid = (bdata or {}).get("pid")
    if not is_pid_alive(pid):
        return False
    lock_path = _paths("legacy_bus_tokens_lock", data_dir)
    tokens_path = _paths("legacy_bus_tokens", data_dir)
    lfd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(lfd, 0o600)
    fcntl.flock(lfd, fcntl.LOCK_EX)
    try:
        tokens = read_json(tokens_path, default={})
        if not isinstance(tokens, dict):
            tokens = {}
        tokens[entry["token"]] = entry
        write_json(tokens_path, tokens, mode=0o600)
    finally:
        fcntl.flock(lfd, fcntl.LOCK_UN)
        os.close(lfd)
    return True


def cmd_token(args):
    data_dir = resolve_data(getattr(args, "data", None))
    # Same realpath lane_token_path hashes, so revoke matches a symlinked lane.
    lane_dir = os.path.realpath(args.lane)
    os.makedirs(lane_dir, exist_ok=True)

    if not is_valid_id(args.task):
        raise Fail("invalid task id")
    # Reserved sender identity: a lane token must never carry it (review LOW 2)
    if args.task == "orch":
        raise Fail("task id 'orch' is reserved")
    run_val = getattr(args, "run", "") or ""
    if run_val and not is_valid_id(run_val):
        raise Fail("invalid run id")
    round_val = getattr(args, "round", 0) or 0
    if not isinstance(round_val, int) or isinstance(round_val, bool) or round_val < 0:
        raise Fail("invalid round")

    # The url this lane is told must be the LIVE bus's: the same resolution
    # _get_client_conn and _orch_token use, so a store appearing mid-flight cannot
    # hand a lane the root bus's port (or port 0) while the token lands in the
    # file-mode bus still serving this data dir (Z4, review HIGH).
    bus_json_path = os.path.join(live_bus_dir(data_dir), "bus.json")
    port = None
    if os.path.exists(bus_json_path):
        d = read_json(bus_json_path)
        if d:
            port = d.get("port")
    if not port:
        port = 0

    tok = secrets.token_urlsafe(32)
    # C6: outside the worktree. File mode (no store) keeps the lane file
    # where mk-builder.py still reads it; store mode uses the home.
    if store_mode(data_dir) or os.environ.get("MK_BUS_TOKEN_FILE"):
        lane_file = _paths("lane_token", lane_dir)
    else:
        lane_file = _paths("lane_token", lane_dir, file_mode=True)
    os.makedirs(os.path.dirname(lane_file), exist_ok=True)
    # "v": 1 matches the contract B24 lane-file example (protocol version).
    # todo 71 / E2 (1): `data` is the data dir, so a fresh lane can find the
    # store with no MK_BUS_DATA in its env. Round fields are recorded so the
    # doorbell's first command (`connect`) knows run/task/round. Existing
    # fields are only ADDED to: the lane reader (E1 mk-builder.py) keeps
    # working against an older token file, and token scope is unchanged.
    payload = {
        "v": 1,
        "url": f"http://127.0.0.1:{port}",
        "token": tok,
        "run": run_val,
        "task": args.task,
        "data": data_dir,
    }
    if round_val:
        payload["round"] = round_val  # never 0: an unarmed token omits it, connect reads the brief

    write_json(lane_file, payload, mode=0o600)

    # The token must land in the file the bus actually reads: the live bus's dir,
    # not the resolution a store appearing mid-flight would name (Z4).
    bus_dir = live_bus_dir(data_dir)
    lock_path = os.path.join(bus_dir, "bus-tokens.lock")
    tokens_path = os.path.join(bus_dir, "bus-tokens.json")
    entry = {
        "project": project_slug_for(data_dir) if store_mode(data_dir) else None,
        "token": tok,
        "role": "lane",
        "run": run_val,
        "task": args.task,
        "lane": lane_dir,
        "created_at": now_iso()
    }
    lfd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(lfd, 0o600)
    fcntl.flock(lfd, fcntl.LOCK_EX)
    try:
        tokens = read_json(tokens_path, default={})
        tokens[tok] = entry
        write_json(tokens_path, tokens, mode=0o600)
    finally:
        fcntl.flock(lfd, fcntl.LOCK_UN)
        os.close(lfd)
    # While a pre-L6.1 bus is still alive it reads the legacy tokens file, so the mint
    # lands in both places (R2 HIGH 2); with no old bus alive only the new path is written.
    _mirror_legacy_tokens(data_dir, entry)

    res = {"ok": True, "token": tok, "lane": lane_dir, "task": args.task, "run": run_val}
    out(res)
    return res


def cmd_revoke(args):
    data_dir = resolve_data(getattr(args, "data", None))
    lane_dir = os.path.realpath(args.lane)
    lane_file = lane_token_path(lane_dir) if (store_mode(data_dir) or os.environ.get("MK_BUS_TOKEN_FILE")) else os.path.join(lane_dir, ".matrixkoder-bus.json")
    revoked_token = None
    removed = False
    if os.path.exists(lane_file):
        try:
            d = read_json(lane_file, {})
            revoked_token = d.get("token")
        except Exception:
            pass
        try:
            os.remove(lane_file)
            removed = True
        except OSError:
            pass

    bus_dir = live_bus_dir(data_dir)
    lock_path = os.path.join(bus_dir, "bus-tokens.lock")
    tokens_path = os.path.join(bus_dir, "bus-tokens.json")
    lfd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(lfd, 0o600)
    fcntl.flock(lfd, fcntl.LOCK_EX)
    try:
        tokens = read_json(tokens_path, default={})
        new_tokens = {}
        for k, v in tokens.items():
            if k == revoked_token:
                continue
            if v.get("lane") == lane_dir:
                continue
            new_tokens[k] = v
        write_json(tokens_path, new_tokens, mode=0o600)
    finally:
        fcntl.flock(lfd, fcntl.LOCK_UN)
        os.close(lfd)

    res = {"ok": True, "removed": removed}
    out(res)
    return res


def _get_client_conn(data_dir):
    bus_json_path = os.path.join(live_bus_dir(data_dir), "bus.json")
    if not os.path.exists(bus_json_path):
        raise Fail("bus down")
    bdata = read_json(bus_json_path)
    if not bdata or "port" not in bdata:
        raise Fail("bus down")
    port = bdata["port"]
    token = _orch_token(data_dir)
    if not token:
        lane_file = os.path.join(os.getcwd(), ".matrixkoder-bus.json")
        if os.path.exists(lane_file):
            token = read_json(lane_file, {}).get("token")
    if not token:
        raise Fail("bus down")
    return port, token


def cmd_send(args):
    data_dir = resolve_data(getattr(args, "data", None))
    if not is_valid_id(args.run):
        raise Fail("invalid run id")
    if getattr(args, "task", None) and not is_valid_id(args.task):
        raise Fail("invalid task id")

    port, token = _get_client_conn(data_dir)

    payload = {
        "run": args.run,
        "task": getattr(args, "task", None),
        "kind": args.kind,
        "text": args.text,
        "from": getattr(args, "from_sender", None) or "orch",
        "to": getattr(args, "to_recipient", None) or getattr(args, "task", None) or "orch",
        "reply_to": getattr(args, "reply_to", None)
    }
    if getattr(args, "urgent", False):
        payload["urgent"] = True
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Host": f"127.0.0.1:{port}"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            out(res)
            return res
    except urllib.error.HTTPError as e:
        err_msg = ""
        try:
            err_msg = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            err_msg = e.reason
        raise Fail(f"HTTP {e.code}: {err_msg}")
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError):
        raise Fail("bus down")


def _store_run_messages(data_dir, run):
    """One run's messages from the PROJECT STORE (todo 68a, E2 (2)).

    SELECT only, through StoreAdapter (mkdb is the single writer). This is the
    same read `pending` uses server-side (`BusState._load_store_run`), reached
    without a server: `cmd_answer` and `cmd_tail` are orchestrator-side CLIs
    that run while the store holds the messages and no bus.jsonl exists.
    Returns [] when the dir is not a registered project (file mode).
    """
    store = StoreAdapter(data_dir)
    rows = store.store_select(
        "SELECT id, run, task, t, kind, from_id, to_id, reply_to, text, artifact_id, meta_json"
        " FROM events WHERE chan=? AND run=? ORDER BY id",
        ("bus", run),
    )
    ack_rows = store.store_select(
        "SELECT msg, lane, level FROM acks WHERE run=?", (run,)
    )
    ack_of = {}
    for a in ack_rows:
        try:
            ack_of[a["msg"]] = max(ack_of.get(a["msg"], 1), int(a["level"] or 1))
        except (TypeError, ValueError):
            continue
    messages = []
    for row in rows:
        eid = int(row["id"])
        meta = {}
        if row.get("meta_json"):
            try:
                meta = json.loads(row["meta_json"])
            except (TypeError, ValueError):
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if row.get("kind") == "ack":
            continue  # ack records are not messages
        msg = {
            "seq": eid,
            "id": f"m-{eid}",
            "ts": row.get("t") or "",
            "run": run,
            "task": row.get("task"),
            "from": row.get("from_id") or "",
            "to": row.get("to_id") or "",
            "kind": row.get("kind"),
            "reply_to": row.get("reply_to"),
            "text": row.get("text") or "",
            "ack_level": ack_of.get(f"m-{eid}", 1),
            "event_id": eid,
        }
        if row.get("artifact_id") is not None:
            msg["artifact_id"] = row["artifact_id"]
        messages.append(msg)
    return messages


def _known_runs(data_dir):
    """Run ids from `<data>/runs/<run>` directories and, in store mode, the project store."""
    runs_dir = _paths("runs_dir", data_dir)
    runs = []
    if os.path.isdir(runs_dir):
        runs = [d for d in sorted(os.listdir(runs_dir)) if os.path.isdir(os.path.join(runs_dir, d)) and is_valid_id(d)]
    # The project store path is resolved by mkpaths only (L0): one rule for the
    # project DB, whether the caller holds a slug or the data dir.
    db = _paths("project_db", data=data_dir)
    if db and os.path.isfile(db):
        for r in StoreAdapter(os.path.dirname(db)).store_select("SELECT id FROM runs"):
            if is_valid_id(r.get("id")) and r["id"] not in runs:
                runs.append(r["id"])
    return runs


def _read_run_messages(data_dir, run):
    """Every message of one run: the project store in store mode, else bus.jsonl.

    E2 (2): `answer` and `tail` must see a store-mode run, where the messages
    live in mk.db and `<data>/runs/<run>/bus.jsonl` is absent. File mode keeps
    reading bus.jsonl exactly as before.
    """
    db = _paths("project_db", data=data_dir)
    if db and os.path.isfile(db):
        msgs = _store_run_messages(os.path.dirname(db), run)
        if msgs:
            return msgs
    jsonl = os.path.join(_paths("run_dir", data_dir, run), "bus.jsonl")
    messages = []
    if not os.path.exists(jsonl):
        return messages
    try:
        with open(jsonl, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if isinstance(m, dict) and m.get("kind") != "ack":
                    messages.append(m)
    except OSError:
        return messages
    return messages


# --- L6.3 run-end archive (C6) -------------------------------------------------------------------
#
# A closed run's bus rows leave the project store: every bus message (events
# chan='bus'), every ack and every bus artifact of the run is written to the
# run's bus.jsonl under mkpaths (one JSON object per line), the file is
# count-checked against the rows, and ONLY THEN are the rows deleted in one
# transaction. Any mismatch or error deletes nothing and removes the file.

# Test-only seam (never set in normal use): drop 1-based line N from the lines
# about to be written, so the count check fails. Self-test case (c) uses it.
ARCHIVE_DROP_LINE_ENV = "MK_BUS_ARCHIVE_DROP_LINE"

# Column order of the SELECTs the readers above use; the canonical row JSON of
# one archived row is these columns in this order, tight separators.
ARCHIVE_MSG_COLS = ("id", "run", "task", "t", "chan", "kind", "from_id", "to_id",
                    "reply_to", "text", "artifact_id", "meta_json")
ARCHIVE_ACK_COLS = ("run", "msg", "lane", "level", "t")
ARCHIVE_ART_COLS = ("id", "run", "task", "round", "kind", "rev", "text", "created", "src")
ARCHIVE_GROUPS = (("msg", ARCHIVE_MSG_COLS), ("ack", ARCHIVE_ACK_COLS), ("artifact", ARCHIVE_ART_COLS))

# mk-state's run statuses are active|done|archived; done and archived both close
# a run (run set --status done, run archive).
ARCHIVE_CLOSED_STATUSES = ("done", "archived")
# A task that cannot still be working when the run closes. This is NOT mk-state's
# ROUND_TERMINAL: a round reset may legitimately inherit `review`/`verifying`,
# but for a run end those two mean a reviewer or verifier lane is live or
# scheduled — its messages, reports and reviews would be deleted out from under
# it. Refusing costs one retry once the task lands; archiving early loses data.
ARCHIVE_TASK_TERMINAL = ("done", "merged", "blocked")

# mk-state's slug-only project name; the --data form below wins, so it is a label.
_ARCHIVE_STORE_PROJECT = "matrixkoder"

# events.id is the wire seq and the SSE Last-Event-ID. The column is a plain
# INTEGER PRIMARY KEY: SQLite hands out max(id)+1 over the rows PRESENT in the
# table, so deleting the top seqs of a run would let the next message reuse a seq
# a live watch / SSE / lane /wait cursor has already passed — that reader would
# then skip every new message that lands on a reused seq. One guard row per
# archived run pins the high-water mark instead: chan='seq' is a chan no reader
# of bus rows selects, and the run id fails is_valid_id, so no run-scoped reader
# (dashboard Events, mk-state, mk-builder task history) ever sees it either.
ARCHIVE_SEQ_RUN = "_seq-guard"
ARCHIVE_SEQ_CHAN = "seq"
ARCHIVE_SEQ_KIND = "guard"


def _archive_canonical(cols, row):
    """One row as its canonical JSON: the SELECT column order, tight separators."""
    return json.dumps([row[c] for c in cols], separators=(",", ":"), ensure_ascii=False)


def _archive_digest(rows_by_group):
    """sha256 over every canonical row of the archive, in file order, newline-fed."""
    h = hashlib.sha256()
    for _name, cols, rows in rows_by_group:
        for row in rows:
            h.update(_archive_canonical(cols, row).encode("utf-8"))
            h.update(b"\n")
    return h.hexdigest()


def _switched_slug_of(data_dir):
    """The root slug whose projects row has SWITCHED this data dir (db_path set), or None.

    One data dir can be named by several root rows: the bus's own project_slug_for
    REGISTERS a derived `p<hash>` row for a dir it has no row for, and that row stays
    after the project is switched under its real slug (L9's audit tree holds exactly
    that pair, both naming <repo>/.matrixkoder). mkpaths resolves a data dir two ways
    and BOTH can land on the unswitched row: project_slug_of returns the FIRST root
    match (the derived row is inserted first), and migrated_slug's map is last-write-wins
    (the derived row is inserted last), so it then finds no db_path and answers None.

    The row that carries db_path — C3's migrated marker — is the one whose store every
    reader of this project must use; a db_path-less row never wins over it. Read-only:
    this is the read half of project_slug_for and registers nothing.
    """
    try:
        mod = _load_mkdb()
        home = _paths("root_home")
        con = mod.open_root(home)
    except Exception:
        return None
    real = os.path.realpath(data_dir)
    try:
        for row in con.execute("SELECT slug, data_dir, db_path FROM projects"):
            slug, recorded, db_path = row[0], row[1], row[2]
            if not isinstance(slug, str) or not slug or not isinstance(db_path, str) or not db_path.strip():
                continue
            if recorded and os.path.realpath(os.path.expanduser(str(recorded))) == real:
                return slug
    except Exception:
        return None
    finally:
        con.close()
    return None


def _own_project_store(data_dir):
    """(store, folder, slug) of the project this data dir names: a switched project's
    rows live in the C2 project folder (L6.1, todo 108), and mkpaths.project_db(slug)
    is the one rule that follows the switch. Pre-migration the answer is exactly
    <data>/mk.db, with a None slug.

    The slug comes from _switched_slug_of — the row that has actually switched this
    dir. The plain resolutions can hand back the unswitched row when a derived row
    names the same dir, which made archive-run read the LEGACY store of a migrated
    project: a closed run answered `not closed (status active)` and a live run's rows
    would have been deleted from the wrong store. See _switched_slug_of for why.
    """
    slug = _switched_slug_of(data_dir)
    if slug:
        try:
            db = _paths("project_db", slug=slug)
        except Fail:
            db = None
        if db and os.path.isfile(db):
            folder = os.path.dirname(db)
            if os.path.realpath(folder) != os.path.realpath(data_dir):
                return StoreAdapter(folder), folder, slug
    return StoreAdapter(data_dir), data_dir, None


def _archive_rows(store, run):
    """(messages, acks, artifacts) of ONE run. SELECT only, through StoreAdapter.

    Messages are events chan='bus' (the row id IS the wire seq).

    Bus artifacts are the rows THAT A BUS MESSAGE CARRIES: the events.artifact_id
    link mkdb.put_artifact writes for the event it was stored for. src IS NULL is
    NOT the discriminator: mk-lane-event and mk-builder use the very same live
    writer (put_artifact, src NULL) to store a task's brief, review-brief and
    review in the project DB, and those rows are not bus traffic — archiving a run
    must never take them. Each revision the bus put also carries its own event
    link, so a revision a later one superseded is archived with the rest instead
    of being orphaned. An importer row (src set) is mk-state's run-doc/index data
    and is unreferenced either way.
    """
    msgs = store.store_select(
        "SELECT id, run, task, t, chan, kind, from_id, to_id, reply_to, text, artifact_id, meta_json"
        " FROM events WHERE chan=? AND run=? ORDER BY id",
        ("bus", run),
    )
    acks = store.store_select(
        "SELECT run, msg, lane, level, t FROM acks WHERE run=? ORDER BY msg, lane",
        (run,),
    )
    arts = store.store_select(
        "SELECT id, run, task, round, kind, rev, text, created, src FROM artifacts"
        " WHERE run=? AND id IN (SELECT artifact_id FROM events"
        " WHERE chan=? AND run=? AND artifact_id IS NOT NULL) ORDER BY id",
        (run, "bus", run),
    )
    return msgs, acks, arts


def _archive_groups(msgs, acks, arts):
    """[(name, cols, rows)] in file order: messages, acks, artifacts."""
    return [("msg", ARCHIVE_MSG_COLS, msgs),
            ("ack", ARCHIVE_ACK_COLS, acks),
            ("artifact", ARCHIVE_ART_COLS, arts)]


def _archive_state(groups):
    """The DB-side (counts, seq range, digest) the written file must carry."""
    counts = {name: len(rows) for name, _cols, rows in groups}
    seqs = [int(r["id"]) for r in groups[0][2]]
    return counts, (min(seqs), max(seqs)) if seqs else (None, None), _archive_digest(groups)


def _archive_lines(groups):
    """One JSON object per archived row, in file order.

    Each line carries its own sha256 of the canonical row, so the count check
    can re-derive every digest from the file alone."""
    lines = []
    for name, cols, rows in groups:
        for row in rows:
            canon = _archive_canonical(cols, row)
            lines.append(json.dumps(
                {"type": name, "sha256": hashlib.sha256(canon.encode("utf-8")).hexdigest(),
                 "row": {c: row[c] for c in cols}},
                separators=(",", ":"), ensure_ascii=False))
    return lines


def _archive_write_file(path, lines):
    """Write the archive file atomically (temp in the same dir, fsync, replace), 0600.

    The one seam a test wraps (or shrinks through ARCHIVE_DROP_LINE_ENV) to
    force a count mismatch without reaching into the finished file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    drop = os.environ.get(ARCHIVE_DROP_LINE_ENV)
    if drop:
        try:
            n = int(drop)
        except ValueError:
            n = 0
        if 1 <= n <= len(lines):
            lines = lines[:n - 1] + lines[n:]
    fd, tmp = tempfile.mkstemp(prefix=".bus.jsonl.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _archive_verify_file(path, groups):
    """Count-check the written file against the rows it was written from.

    Re-reads the file and recomputes FROM THE FILE the per-group counts, the seq
    range and the aggregate digest, and every line's own sha256. A truncated,
    reordered or edited file fails here: the caller deletes nothing."""
    cols_of = {name: cols for name, cols, _rows in groups}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = [ln for ln in f.read().split("\n") if ln]
    except OSError as e:
        raise Fail(f"archive file unreadable after the write: {e}")
    file_rows = {name: [] for name, _cols, _rows in groups}
    for ln in raw:
        try:
            obj = json.loads(ln)
        except ValueError:
            raise Fail(f"archive file holds a non-JSON line: {ln[:120]}")
        name = obj.get("type") if isinstance(obj, dict) else None
        row = obj.get("row") if isinstance(obj, dict) else None
        if name not in cols_of or not isinstance(row, dict):
            raise Fail(f"archive file holds a malformed line: {ln[:120]}")
        cols = cols_of[name]
        if any(c not in row for c in cols):
            raise Fail(f"archive file line is missing columns: {ln[:120]}")
        canon = json.dumps([row[c] for c in cols], separators=(",", ":"), ensure_ascii=False)
        if hashlib.sha256(canon.encode("utf-8")).hexdigest() != obj.get("sha256"):
            raise Fail("archive file line digest mismatch: the file changed after the write")
        file_rows[name].append(row)
    counts = {name: len(rows) for name, rows in file_rows.items()}
    want_counts = {name: len(rows) for name, _cols, rows in groups}
    if counts != want_counts:
        raise Fail(f"archive count mismatch: the file holds {counts}, the rows were {want_counts}")
    seqs = [int(r["id"]) for r in file_rows["msg"]]
    want_seqs = [int(r["id"]) for r in groups[0][2]]
    got_range = (min(seqs), max(seqs)) if seqs else (None, None)
    want_range = (min(want_seqs), max(want_seqs)) if want_seqs else (None, None)
    if got_range != want_range:
        raise Fail(f"archive seq range mismatch: the file holds {got_range}, the rows were {want_range}")
    digest = _archive_digest([(name, cols_of[name], file_rows[name]) for name, _cols, _rows in groups])
    if digest != _archive_digest(groups):
        raise Fail("archive sha256 mismatch: the file rows do not hash to the store rows")


def _archive_refuse_unless_closed(store, run):
    """(plan, status) of a CLOSED run; Fail before anything is written otherwise.

    Closed means the run row says so (done or archived) AND every task of the run
    is terminal: an active run, or one with a live task, is never archived."""
    rows = store.store_select("SELECT id, plan, status FROM runs WHERE id=?", (run,))
    if not rows:
        raise Fail(f"unknown run {run!r}: no run row in the project store")
    status = (rows[0].get("status") or "").strip().lower()
    if status not in ARCHIVE_CLOSED_STATUSES:
        raise Fail(f"run {run} is not closed (status {status or 'unset'}): refusing to archive")
    tasks = store.store_select("SELECT id, status FROM tasks WHERE run=?", (run,))
    open_tasks = [t.get("id") for t in tasks
                  if (t.get("status") or "").strip().lower() not in ARCHIVE_TASK_TERMINAL]
    if open_tasks:
        raise Fail(f"run {run} has non-terminal tasks {open_tasks}: refusing to archive")
    return rows[0].get("plan"), status


def _archive_hold_seq(con, seq_max, last_t, run):
    """Pin the seq high-water mark above the archived range, in the same transaction.

    Called AFTER the run's bus rows are gone and before COMMIT, so it rolls back
    with them. `seq_max` is the highest archived message id; when some other row
    already sits above it the counter cannot reuse the archived range and no
    guard row is written. `last_t` keeps the guard's own timestamp the archived
    run's, so MAX(t) readers (cards/project_summary) see no change."""
    if not seq_max:
        return
    top = con.execute("SELECT MAX(id) AS m FROM events").fetchone()["m"]
    if top is not None and int(top) >= int(seq_max):
        return
    con.execute(
        "INSERT INTO events(id, run, task, t, chan, kind, from_id, to_id, reply_to,"
        " text, artifact_id, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(seq_max), ARCHIVE_SEQ_RUN, None, last_t or "", ARCHIVE_SEQ_CHAN,
         ARCHIVE_SEQ_KIND, None, None, None, run, None, None))


def _archive_delete(mod, folder, run, want):
    """Delete the run's bus rows in ONE transaction, re-checked inside it.

    The count check runs again under BEGIN IMMEDIATE: a row that landed between
    the read and the delete changes the state, and the archive refuses rather
    than delete a row the file never carried. Artifacts go first: their ids come
    from the bus events, so the events must still be there when they are read."""
    con = mod.open_project(mk_home(), _ARCHIVE_STORE_PROJECT, data_dir=folder)
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            msgs, acks, arts = _archive_rows(StoreAdapter(folder), run)
            now = _archive_state(_archive_groups(msgs, acks, arts))
            if now != want:
                raise Fail("the run's rows changed while the archive was written: nothing deleted")
            art_ids = [(r["id"],) for r in arts]
            if art_ids:
                con.executemany("DELETE FROM artifacts WHERE id=?", art_ids)
            con.execute("DELETE FROM events WHERE chan=? AND run=?", ("bus", run))
            con.execute("DELETE FROM acks WHERE run=?", (run,))
            _archive_hold_seq(con, want[1][1], msgs[-1]["t"] if msgs else None, run)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def archive_run(data_dir, run):
    """L6.3 (C6): archive ONE closed run's bus rows to bus.jsonl, then delete them.

    Refuses an active run before anything is written. A count mismatch or any
    error deletes nothing and removes the partial file. A second call finds
    nothing to archive and does nothing: no empty second file, and an archive
    file that already exists is left alone. Returns the reply dict.

    A bus process that is serving this data dir while the archive runs keeps the
    run's messages in its own cache until it restarts: the CLI holds no channel
    into a live serve loop, so a /pending or /events?replay=true against THAT
    process answers from memory. Watch, /wait, cmd_pending and every fresh serve
    read the store and see the archive. Restart the bus of an archived run to
    drop the cache."""
    if not is_valid_id(run):
        raise Fail("invalid run id")
    if not store_mode(data_dir):
        raise Fail("archive-run needs the project store: file mode holds no bus rows")
    store, folder, slug = _own_project_store(data_dir)
    plan, _status = _archive_refuse_unless_closed(store, run)
    msgs, acks, arts = _archive_rows(store, run)
    groups = _archive_groups(msgs, acks, arts)
    counts, seq_range, digest = _archive_state(groups)
    # The archive belongs beside the run it holds: the FOLDER of the store it was
    # read from, not a slug. A slug can name an alias row (`p<hash>` registered for
    # a dir it has no row for) and building the path from that slug creates a stray
    # project folder beside the real one; `folder` IS the db_path folder
    # _own_project_store resolved, the same tree mkpaths' target rule names.
    if slug:
        path = os.path.join(folder, _load_mkpaths().TARGET_PLANS_DIR, plan, "runs", run, "bus.jsonl")
    else:
        path = os.path.join(_paths("run_dir", data_dir, run), "bus.jsonl")
    if not any(counts.values()):
        # Idempotent: the rows are already gone. An archive file that already
        # exists with matching content is left alone; no empty second file.
        return {"ok": True, "op": "archive-run", "run": run, "plan": plan, "path": path,
                "messages": 0, "acks": 0, "artifacts": 0, "seq_min": None, "seq_max": None,
                "sha256": None, "written": False, "archived": False}
    if os.path.exists(path):
        raise Fail(f"archive file already exists while rows remain: {path} — refusing to overwrite it")
    _archive_write_file(path, _archive_lines(groups))
    try:
        _archive_verify_file(path, groups)
    except BaseException:
        # The partial file never survives a failed check.
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    try:
        _archive_delete(_load_mkdb(), folder, run, (counts, seq_range, digest))
    except BaseException:
        # Nothing was deleted, but a file on disk while the rows remain would
        # refuse every later attempt. Clear it so the next archive can retry.
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return {"ok": True, "op": "archive-run", "run": run, "plan": plan, "path": path,
            "messages": counts["msg"], "acks": counts["ack"], "artifacts": counts["artifact"],
            "seq_min": seq_range[0], "seq_max": seq_range[1], "sha256": digest,
            "written": True, "archived": True}


def cmd_archive_run(args):
    """C6: the run-end archive CLI. See archive_run for the contract."""
    data_dir = resolve_data(getattr(args, "data", None))
    res = archive_run(data_dir, getattr(args, "run", "") or "")
    out(res)
    return res


def _copy_private(src, dst):
    """Copy one file byte for byte to dst, 0600, atomically (temp file, fsync, replace)."""
    d = os.path.dirname(dst)
    os.makedirs(d, exist_ok=True)
    with open(src, "rb") as f:
        blob = f.read()
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".carry-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def carry_root(home, dry_run=False):
    """L10.G: move the pre-L6.1 root bus files into bus/ and the old copies into a named backup.

    Every token of <home>/bus-tokens.json that bus/bus-tokens.json lacks is merged in (an entry
    in both places keeps the bus/ value, never overwritten); every <home>/bus-lanes/<sha>.json
    that bus/lanes/ lacks is copied byte for byte, 0600 (a name in both keeps the bus/ file).
    Only after both are verified are the root bus-tokens.json, bus-tokens.lock and bus-lanes/
    MOVED to <home>/backups/bus-root-<stamp>/ — never deleted. A bus whose pid sits in the root
    bus.json is still running against the root files: refused. --dry-run lists, writes nothing.
    Tokens are never printed; the reply counts them and names the projects of the carried ones."""
    home = os.path.abspath(home)
    bus = _paths("target_bus_dir", home)
    root_tokens = os.path.join(home, "bus-tokens.json")
    root_lock = os.path.join(home, "bus-tokens.lock")
    root_lanes = os.path.join(home, "bus-lanes")
    running = is_pid_alive((read_json(os.path.join(home, "bus.json")) or {}).get("pid"))
    if running and not dry_run:
        raise Fail("a bus is still running against the root bus files (pid in <home>/bus.json); stop it first")
    res = {"ok": True, "op": "carry-root", "dry_run": bool(dry_run), "home": home, "bus_dir": bus,
           "bus_running_on_root": running,
           "tokens": {"root": 0, "carried": 0, "same": 0, "bus_wins": 0, "carried_projects": {}},
           "lanes": {"root": 0, "carried": 0, "same": 0, "bus_wins": 0}, "moves": []}
    tokens_path = os.path.join(bus, "bus-tokens.json")
    root = {}
    if os.path.isfile(root_tokens):
        root = read_json(root_tokens)
        if not isinstance(root, dict):
            raise Fail("root bus-tokens.json is unreadable; nothing moved")
    t = res["tokens"]
    t["root"] = len(root)
    cur = read_json(tokens_path, default={}) or {}
    for tok, entry in root.items():
        if tok not in cur:
            t["carried"] += 1
            proj = str((entry or {}).get("project") or "?") if isinstance(entry, dict) else "?"
            t["carried_projects"][proj] = t["carried_projects"].get(proj, 0) + 1
        elif cur[tok] == entry:
            t["same"] += 1
        else:
            t["bus_wins"] += 1
    lane_ops = []
    names = sorted(n for n in os.listdir(root_lanes) if os.path.isfile(os.path.join(root_lanes, n))) \
        if os.path.isdir(root_lanes) else []
    res["lanes"]["root"] = len(names)
    for n in names:
        src, dst = os.path.join(root_lanes, n), os.path.join(bus, "lanes", n)
        if not os.path.exists(dst):
            res["lanes"]["carried"] += 1
            lane_ops.append((src, dst))
        elif open(src, "rb").read() == open(dst, "rb").read():
            res["lanes"]["same"] += 1
        else:
            res["lanes"]["bus_wins"] += 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(home, "backups", "bus-root-" + stamp)
    n = 1
    while os.path.exists(backup):
        n += 1
        backup = os.path.join(home, "backups", f"bus-root-{stamp}-{n}")
    movers = [p for p in (root_tokens, root_lock, root_lanes) if os.path.lexists(p)]
    res["moves"] = [{"from": p, "to": os.path.join(backup, os.path.basename(p))} for p in movers]
    res["backup"] = backup if movers else None
    if dry_run:
        return res
    if t["carried"]:
        os.makedirs(bus, exist_ok=True)
        lfd = os.open(os.path.join(bus, "bus-tokens.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(lfd, 0o600)
        fcntl.flock(lfd, fcntl.LOCK_EX)
        try:
            cur = read_json(tokens_path, default={}) or {}
            for tok, entry in root.items():
                cur.setdefault(tok, entry)
            write_json(tokens_path, cur, mode=0o600)
        finally:
            fcntl.flock(lfd, fcntl.LOCK_UN)
            os.close(lfd)
    for src, dst in lane_ops:
        _copy_private(src, dst)
    # verify before anything moves: every root token and lane file is now answered from bus/
    after = read_json(tokens_path, default={}) or {}
    lost = [k for k in root if k not in after]
    if lost:
        raise Fail(f"{len(lost)} root tokens missing from bus/bus-tokens.json after the merge; nothing moved")
    for src, dst in lane_ops:
        if not os.path.isfile(dst) or open(src, "rb").read() != open(dst, "rb").read():
            raise Fail(f"lane file {os.path.basename(dst)} did not copy intact; nothing moved")
    if movers:
        os.makedirs(backup, exist_ok=True)
        for p in movers:
            os.rename(p, os.path.join(backup, os.path.basename(p)))
    return res


def cmd_carry_root(args):
    """L10.G: the carry-root CLI. See carry_root for the contract."""
    res = carry_root(mk_home(), dry_run=getattr(args, "dry_run", False))
    out(res)
    return res


def cmd_answer(args):
    data_dir = resolve_data(getattr(args, "data", None))
    port, token = _get_client_conn(data_dir)

    target_run = getattr(args, "run", None)
    if target_run and not is_valid_id(target_run):
        raise Fail("invalid run id")
    runs_to_search = [target_run] if target_run else _known_runs(data_dir)

    # Message ids are per run ("m-1" exists in every run), so without --run
    # the id can match in several runs. Collect every match instead of
    # answering whichever run os.listdir happens to return first (review
    # MEDIUM 2), then refuse when it is ambiguous.
    matches = []
    for r in runs_to_search:
        for m in _read_run_messages(data_dir, r):
            if m.get("id") == args.msg_id:
                matches.append((r, m))
                break

    if not matches:
        raise Fail(f"message not found: {args.msg_id}")
    if not target_run and len(matches) > 1:
        raise Fail("ambiguous message id, pass --run")
    target_run, target_msg = matches[0]

    if target_msg.get("kind") != "ask":
        raise Fail(f"message {args.msg_id} is not an ask")
    if not target_run or not is_valid_id(target_run):
        raise Fail(f"cannot find valid run for message {args.msg_id}")

    task = target_msg.get("task") if target_msg else None
    to_recipient = target_msg.get("from") if target_msg else "client"

    payload = {
        "run": target_run,
        "task": task,
        "from": "orch",
        "to": to_recipient,
        "kind": "answer",
        "text": args.text,
        "reply_to": args.msg_id
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Host": f"127.0.0.1:{port}"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            send_res = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_msg = ""
        try:
            err_msg = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            err_msg = e.reason
        raise Fail(f"HTTP {e.code}: {err_msg}")
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError):
        raise Fail("bus down")

    state_updated = False
    mk_state_error = None
    if "state_updated" in send_res:
        # Store mode: the bus already moved the task through the store.
        state_updated, mk_state_error = send_res["state_updated"], send_res.get("mk_state_error")
    elif task:
        # File mode has no state to move (L4 removed the file views). Calling mk-state here
        # would CREATE the project store this bus resolves its files through, which moves a
        # live bus's token file and answers every lane token 401 (Z4).
        if store_mode(data_dir):
            state_updated, mk_state_error = _mk_state_task_set(data_dir, target_run, task,
                                                               "building", "bus answer")

    res = {
        "ok": True,
        "message": send_res.get("message"),
        "state_updated": state_updated,
        "mk_state_error": mk_state_error
    }
    out(res)
    return res


def cmd_pending(args):
    data_dir = resolve_data(getattr(args, "data", None))
    run = getattr(args, "run", "") or ""
    if run and not is_valid_id(run):
        raise Fail("invalid run id")

    port, token = _get_client_conn(data_dir)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/pending?run={run}",
        headers={
            "Authorization": f"Bearer {token}",
            "Host": f"127.0.0.1:{port}"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            out(res)
            return res
    except urllib.error.HTTPError as e:
        err_msg = ""
        try:
            err_msg = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            err_msg = e.reason
        raise Fail(f"HTTP {e.code}: {err_msg}")
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError):
        raise Fail("bus down")


def cmd_tail(args):
    data_dir = resolve_data(getattr(args, "data", None))
    run = getattr(args, "run", None)
    if not run:
        runs = _known_runs(data_dir)
        if len(runs) == 1:
            run = runs[0]
    if not run or not is_valid_id(run):
        raise Fail("missing or invalid --run")

    lane_filter = getattr(args, "lane", None)
    after_seq = getattr(args, "after", 0) or 0
    n = getattr(args, "n", 10) or 10

    # E2 (2): store mode reads the project store; file mode reads bus.jsonl.
    messages = []
    for msg in _read_run_messages(data_dir, run):
        if after_seq and msg.get("seq", 0) <= after_seq:
            continue
        if lane_filter and not matches_lane(msg, lane_filter):
            continue
        messages.append(msg)
    res = {"ok": True, "messages": messages[-n:] if n > 0 else messages}
    out(res)
    return res



def _watch_line(m):
    """One MSG line per message: `MSG <id> <run> <task> <kind> <from>-><to>: <text>`.

    The text is untrusted quoted data (C13a): collapsed to one line and cut to
    200 characters so a pasted brief cannot break the stream or flood the pane.
    """
    text = re.sub(r"\s+", " ", str(m.get("text") or "")).strip()
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)[:200]
    return "MSG %s %s %s %s %s->%s: %s" % (
        m.get("id", ""), m.get("run", ""), m.get("task") or "-", m.get("kind", ""),
        m.get("from", ""), m.get("to", ""), text,
    )


def _orch_token(data_dir):
    """The orchestrator token for THIS data dir's project. The root bus is shared by every
    repo on the machine and bus.json's token belongs to the project that started it; an
    orchestrator of another project uses (or mints) its own project-scoped orch token in
    bus-tokens.json, so its messages land in its own store and run, never the host's."""
    # The live bus's dir, not the resolution a store appearing mid-flight names: the
    # token must be one the bus that is actually running accepts (Z4).
    base = live_bus_dir(data_dir)
    bus_json = read_json(os.path.join(base, "bus.json")) or {}
    slug = project_slug_for(data_dir)
    if bus_json.get("token") and (slug is None or bus_json.get("project") in (None, slug)):
        return bus_json["token"]
    tp = os.path.join(base, "bus-tokens.json")
    tl = os.path.join(base, "bus-tokens.lock")
    lfd = os.open(tl, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lfd, fcntl.LOCK_EX)
        tokens = read_json(tp, default={}) or {}
        for tok, v in tokens.items():
            if isinstance(v, dict) and v.get("role") == "orch" and v.get("project") == slug:
                return tok
        if slug is None:
            return None
        tok = secrets.token_urlsafe(32)
        tokens[tok] = {"token": tok, "role": "orch", "run": "*", "task": "", "lane": "",
                       "project": slug, "created_at": now_iso()}
        write_json(tp, tokens, mode=0o600)
        return tok
    finally:
        fcntl.flock(lfd, fcntl.LOCK_UN)
        os.close(lfd)


def _cursor_seq(saved):
    """The seq of one saved cursor value: an old plain int, or {"seq": n, ...}; else 0."""
    if isinstance(saved, dict):
        saved = saved.get("seq")
    return saved if isinstance(saved, int) and not isinstance(saved, bool) and saved > 0 else 0


def cursor_get(data_dir, key):
    """L10.5: the watch cursor for `key` from the project DB's watch_cursors table. No row yet: the
    ONE-TIME import takes the max seq of the C2-folder watch-cursors.json and the legacy one (read
    only, neither is ever written again) and stores it. A store that cannot be read or written
    (a pre-schema-7 store, a locked one) answers 0: a replay, never a lost message."""
    try:
        mod = StoreAdapter(data_dir)._mod()
        con = mod._open_read(project_store_path(data_dir), mod.SCHEMA)
        row = con.execute("SELECT seq FROM watch_cursors WHERE key=?", (key,)).fetchone() if con else None
        if con:
            con.close()
        if row:
            return int(row["seq"])
        folder = _paths("project_folder", data_dir)
        seqs = [_cursor_seq((read_json(os.path.join(d, os.path.basename(_paths("target_watch_cursors", "x"))), default={}) or {}).get(key))
                for d in {folder, data_dir}]
        seq = max(seqs)
        if seq:
            cursor_put(data_dir, key, seq)
        return seq
    except Exception:
        return 0


def cursor_row(data_dir, key):
    """The stored seq of one watch_cursors row, or None (no row, no store): a read, nothing imported."""
    mod = StoreAdapter(data_dir)._mod()
    con = mod._open_read(project_store_path(data_dir), mod.SCHEMA)
    try:
        row = con.execute("SELECT seq FROM watch_cursors WHERE key=?", (key,)).fetchone() if con else None
    finally:
        if con:
            con.close()
    return row["seq"] if row else None


def cursor_put(data_dir, key, seq):
    """Upsert one watch cursor into watch_cursors (the store's own write path; mk-bus owns this one)."""
    mod = StoreAdapter(data_dir)._mod()
    con = mod.open_project(mod.resolve_home(None), data_dir=data_dir)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO watch_cursors(key, seq, started, updated) VALUES (?,?,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET seq=excluded.seq, updated=excluded.updated",
                    (key, int(seq), now_iso(), now_iso()))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def cmd_watch(args):
    """E2 (4): the orchestrator's stream. One flushed line per bus message.

    Runs under the Claude Code Monitor tool so every lane message reaches the
    main session as a notification. After a drop or a bus RESTART it resumes
    with Last-Event-ID and re-reads bus.json for the new port. Exit 4 only
    when the bus stays down for WATCH_DOWN_SEC (60 s), never on one blip.
    Without --run it streams every run of the project (`run=*`; in file mode
    the bus's own run, since `run=*` needs the project store). A 4xx from
    the bus prints the error on one line and exits 5, as ask/wait/inbox do.

    The last printed seq is kept in the project DB's watch_cursors table (file mode:
    <data>/watch-cursors.json) under "<run or *>|<task or *>", so a new watch does not replay what the last one
    printed. Start point: explicit --after, else the cursor, else 0 (first arm);
    --replay skips the cursor. Seq is the number in the message id (m-12 -> 12).
    """
    data_dir = resolve_data(getattr(args, "data", None))
    run = getattr(args, "run", "") or ""
    task = getattr(args, "task", "") or ""
    if run and not is_valid_id(run):
        raise Fail("invalid run id")
    if task and not is_valid_id(task):
        raise Fail("invalid task id")
    after = getattr(args, "after", None)
    cursor_key = "%s|%s" % (run or "*", task or "*")
    # L10.5: with the project store the cursor is a watch_cursors row, and no watch-cursors.json is
    # read or written (the first watch imports the old files once, inside cursor_get). File mode
    # (no mk.db) keeps the file.
    cursor_path = None if store_mode(data_dir) else _paths("watch_cursors", data_dir)
    # File mode: a new run in the same data dir restarts seq from 1 and restarts the bus, so a cursor
    # is trusted only for the bus start stamp it was written under (a mismatch replays: a duplicate,
    # never a lost message). Store mode seqs never reset, so no stamp is kept or checked there.
    file_mode = not store_mode(data_dir)

    def bus_json_path():
        # The live bus's bus.json, not the resolution a store appearing mid-flight
        # names: the token comes from _orch_token (live_bus_dir), so the port and the
        # started stamp must come from the same bus or watch 401s / reports BUS DOWN.
        return os.path.join(live_bus_dir(data_dir), "bus.json")

    def bus_started():
        try:
            return (read_json(bus_json_path()) or {}).get("started")
        except Exception:
            return None
    cur_started = bus_started() if file_mode else None
    if after is None or after == "":
        after = 0
        if not getattr(args, "replay", False):
            cur = read_json(cursor_path, default={}) if cursor_path else {}
            saved = cur.get(cursor_key) if isinstance(cur, dict) else None
            if cursor_path is None:
                saved = cursor_get(data_dir, cursor_key)
            if isinstance(saved, int):  # an old plain-integer value: no stamp
                saved = {"seq": saved, "started": None}
            if (isinstance(saved, dict) and isinstance(saved.get("seq"), int) and saved["seq"] > 0
                    and (not file_mode or (cur_started is not None and saved.get("started") == cur_started))):
                after = saved["seq"]
    last_seq = int(str(after)[2:]) if str(after).startswith("m-") and str(after)[2:].isdigit() else int(after or 0)
    max_messages = getattr(args, "max_messages", 0) or 0

    down_since = None
    printed = 0
    while True:
        port = None
        try:
            bdata = read_json(bus_json_path())
            if bdata and bdata.get("port"):
                port = int(bdata["port"])
                if file_mode:
                    cur_started = bdata.get("started")
        except Exception:
            port = None
        token = _orch_token(data_dir)

        if not port or not token:
            # Bus down or not started yet: keep trying for the window, then 4.
            if down_since is None:
                down_since = time.time()
            if time.time() - down_since >= WATCH_DOWN_SEC:
                sys.stderr.write("BUS DOWN: watch gave up after %ds\n" % WATCH_DOWN_SEC)
                return 4
            time.sleep(WATCH_RETRY_SEC)
            continue

        try:
            # `run=*` needs the project store; a file-mode bus answers for its own run.
            qs = urllib.parse.urlencode({"lane": "*", **({"run": run or "*"} if run or store_mode(data_dir) else {})})
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/events?{qs}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Last-Event-ID": f"m-{last_seq}",
                    "Host": f"127.0.0.1:{port}",
                },
            )
            with urllib.request.urlopen(req, timeout=WATCH_READ_SEC) as resp:
                down_since = None  # the bus answered: the window restarts
                buf = ""
                while True:
                    chunk = resp.readline()
                    if not chunk:
                        raise ConnectionError("stream closed")
                    buf += chunk.decode("utf-8", "replace")
                    while "\n\n" in buf:
                        block, buf = buf.split("\n\n", 1)
                        data_lines = [ln[5:].strip() for ln in block.splitlines() if ln.startswith("data:")]
                        if not data_lines:
                            continue  # keepalive comment
                        try:
                            m = json.loads("".join(data_lines))
                        except ValueError:
                            continue
                        if not isinstance(m, dict):
                            continue
                        if task and m.get("task") != task:
                            continue
                        try:
                            last_seq = max(last_seq, int(m.get("seq") or 0))
                        except (TypeError, ValueError):
                            pass
                        print(_watch_line(m), flush=True)
                        # shortcut: unlocked read-modify-write, two watches of one data dir can
                        # lose a cursor update (replays a few lines); lock if that ever bites.
                        if cursor_path is None:
                            try:
                                cursor_put(data_dir, cursor_key, last_seq)
                            except Exception as e:  # a cursor that cannot be saved replays, never loses
                                sys.stderr.write("watch cursor not saved: %s\n" % (e,))
                        else:
                            cursors = read_json(cursor_path, default={})
                            cursors = cursors if isinstance(cursors, dict) else {}
                            cursors[cursor_key] = {"seq": last_seq, "started": cur_started}
                            write_json(cursor_path, cursors)
                        printed += 1
                        if max_messages and printed >= max_messages:
                            return 0
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                # The bus is up and refused the request: retrying cannot help.
                try:
                    reason = json.loads(e.read().decode("utf-8")).get("error", "")
                except Exception:
                    reason = e.reason
                sys.stderr.write("HTTP %d: %s\n" % (e.code, re.sub(r"\s+", " ", str(reason))))
                return 5
        except (urllib.error.URLError, ConnectionError, ConnectionRefusedError,
                TimeoutError, socket.timeout, OSError, ValueError):
            # A drop or a restart: resume from last_seq, re-reading bus.json
            # for the new port on the next turn of the loop.
            pass
        if down_since is None:
            down_since = time.time()
        if time.time() - down_since >= WATCH_DOWN_SEC:
            sys.stderr.write("BUS DOWN: watch gave up after %ds\n" % WATCH_DOWN_SEC)
            return 4
        time.sleep(WATCH_RETRY_SEC)


def cmd_metrics(args):
    """C13f: one JSON line of bus metrics for a run, from the project store.

    Without --run: a `runs` array with the same object for every active run."""
    data_dir = resolve_data(getattr(args, "data", None))
    run = args.run
    if run is not None and not is_valid_id(run):
        raise Fail("invalid run id")
    if not store_mode(data_dir):
        res = _file_metrics(data_dir, run)
        out(res)
        return res
    store = StoreAdapter(data_dir)
    if run is None:
        active = [r["id"] for r in store.store_select("SELECT id FROM runs WHERE status=? ORDER BY id", ("active",))
                  if is_valid_id(r.get("id")) and not store._mod().is_bugs_run(r.get("id"))]  # bugs-tab
        res = {"ok": True, "run": "*", "runs": [_run_metrics(store, r) for r in active]}
        out(res)
        return res
    res = _run_metrics(store, run)
    out(res)
    return res


def _file_metrics(data_dir, run):
    """File mode (no project store): counts from bus.jsonl via the shared reader.

    Without --run: the run in this lane's bus token file, else the data dir's only run."""
    if run is None:
        run = (read_json(os.path.join(os.getcwd(), ".matrixkoder-bus.json"), {}) or {}).get("run")
        if not is_valid_id(run):
            runs = _known_runs(data_dir)
            run = runs[0] if len(runs) == 1 else None
        if not run:
            raise Fail("missing --run")
    messages = _read_run_messages(data_dir, run)
    by_kind, last = {}, {}
    for m in messages:
        by_kind[m.get("kind")] = by_kind.get(m.get("kind"), 0) + 1
        lane = m.get("task") or m.get("from")
        if lane and m.get("ts"):
            last[lane] = max(last.get(lane, ""), m["ts"])
    answered = {m["reply_to"] for m in messages
                if m.get("kind") == "answer" and isinstance(m.get("reply_to"), str)}
    pending = sum(1 for m in messages if m.get("kind") == "ask" and m.get("id") not in answered)
    return {"ok": True, "mode": "file", "run": run, "messages_by_kind": by_kind,
            "last_message_by_lane": last, "pending_asks": pending}


def _run_metrics(store, run):
    events = store.store_select(
        "SELECT id, t, kind, reply_to, text, meta_json FROM events WHERE chan=? AND run=? ORDER BY id",
        ("bus", run),
    )
    acks = store.store_select("SELECT msg, level, t FROM acks WHERE run=?", (run,))
    ack3 = {}
    for a in acks:
        if int(a["level"] or 0) >= 3:
            ack3[a["msg"]] = a["t"]
    lat = []
    for e in events:
        wid = "m-%s" % e["id"]
        if e["kind"] == "answer" and wid in ack3:
            lat.append(_secs(ack3[wid]) - _secs(e["t"]))
    sent = [e for e in events if e["kind"] in ("ask", "info", "nudge", "answer")]
    obeyed = sum(1 for e in sent if ("m-%s" % e["id"]) in ack3)
    failed = 0
    for e in events:
        meta = {}
        if e.get("meta_json"):
            try:
                meta = json.loads(e["meta_json"])
            except ValueError:
                meta = {}
        if isinstance(meta, dict) and (meta.get("delivered") is False or meta.get("attempts") == 2):
            failed += 1
    answered = set(e["reply_to"] for e in events if e["kind"] == "answer")
    failed += sum(1 for e in events if e["kind"] == "ask" and ("m-%s" % e["id"]) not in answered)
    false_stall = 0
    try:
        rows = store.store_select(
            "SELECT task, note, t FROM change_log WHERE run=? ORDER BY change_no", (run,),
        )
    except Exception:
        rows = []
    by_task = {}
    for r in rows:
        by_task.setdefault(r["task"], []).append(r)
    for seq in by_task.values():
        for i, r in enumerate(seq):
            if "stalled" not in (r.get("note") or ""):
                continue
            if i + 1 < len(seq) and _secs(seq[i + 1]["t"]) - _secs(r["t"]) <= 600:
                false_stall += 1

    def pct(xs, q):
        if not xs:
            return None
        xs = sorted(xs)
        k = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
        return round(xs[k], 3)

    res = {
        "ok": True, "mode": "store", "run": run,
        "latency_p50": pct(lat, 0.50), "latency_p95": pct(lat, 0.95),
        "failed_deliveries": failed,
        "obedience": (round(obeyed / len(sent), 3) if sent else None),
        "false_stall": false_stall,
    }
    return res


def _secs(ts):
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return datetime.datetime.fromisoformat(str(ts)).timestamp()
    except ValueError:
        return 0.0


def _mkdb_helpers_ready():
    path = os.path.join(HERE, "mkdb.py")
    spec = importlib.util.spec_from_file_location("mkdb_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    needed = ("append_event", "append_ack", "put_artifact", "transition", "open_project")
    return mod if all(hasattr(mod, n) for n in needed) else None


def _store_self_test():
    """Store-mode cases. Skipped until D1D's helpers are importable.

    Each case fails on the file-mode bus: a present mk.db is ignored, so a
    report never becomes an artifact and a restart cannot replay from the store.
    """
    mod = _mkdb_helpers_ready()
    if mod is None:
        print("store-mode self-test skipped: mkdb live writers not present")
        return
    # Implemented in the same function once the helpers land. The skip line is
    # the R2 gate that this file does not call them before they exist.
    _store_self_test_body(mod)


def _layout_self_test():
    """L6.1 (C6/C7): the bus on the C2 layout, exercised on the production path.

    One temp $HOME with MK_HOME, MATRIXKODER_HOME, MATRIXKODER_DATA, MK_BUS_DIR and
    MK_BUS_TOKEN_FILE all UNSET, so mkpaths.home() answers $HOME/.matrixkoder exactly as a
    real machine resolves it. Three projects share ONE bus: a MIGRATED one (L2's real
    `mk-migrate --switch`), a PRE-MIGRATION one beside it, and the bus's OWN project, which
    is pre-migration when the bus starts and switches while the bus runs. Real CLI, real
    HTTP, real migration tool:

      1. every bus file lands in <home>/.matrixkoder/bus/ and nowhere else;
      2. a message sent to each project lands in that project's own store;
      3. after the switch of the bus's OWN project the next message lands in the NEW
         project DB and not in the legacy one, with no restart (todo 108);
      4. the watch cursor of the switched project is written to the project folder, and
         the pre-migration file is read once and carried over;
      5. a bus, its tokens and an armed lane's token file published at the pre-L6.1 paths
         are ADOPTED — copied, never deleted — and keep working: one bus for the home, and
         the lane's token still authenticates a message.
    """
    import shutil

    mod = _mkdb_helpers_ready()
    if mod is None:
        print("layout self-test skipped: mkdb live writers not present")
        return
    tmp = tempfile.mkdtemp(prefix="mk-bus-layout-")
    pids = []
    saved = {k: os.environ.get(k) for k in
             ("HOME", "MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA", "MK_BUS_DIR",
              "MK_BUS_TOKEN_FILE", "MK_SELFTEST")}
    home_dir = os.path.join(tmp, "home")
    os.makedirs(home_dir, exist_ok=True)
    for key in ("MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA", "MK_BUS_DIR",
                "MK_BUS_TOKEN_FILE"):
        os.environ.pop(key, None)   # the production path: no pin resolves the home
    os.environ["HOME"] = home_dir
    os.environ["MK_SELFTEST"] = "1"  # mkdb's C16 guard: writes stay in the temp tree
    mkh = os.path.join(home_dir, ".matrixkoder")
    bus_dir = os.path.join(mkh, "bus")

    def cli(args, check=True):
        p = subprocess.run([sys.executable, os.path.abspath(__file__)] + args,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = [x.strip() for x in p.stdout.strip().splitlines() if x.strip()]
        last = json.loads(lines[-1]) if lines else {}
        if check and p.returncode != 0:
            raise AssertionError(f"CLI {args} rc={p.returncode}: {p.stderr.strip()[-300:]}")
        return p.returncode, last, p.stderr

    def texts(db_path):
        con = mod._connect_ro(db_path)
        try:
            return [r[0] for r in con.execute(
                "SELECT text FROM events WHERE chan='bus' ORDER BY id")]
        finally:
            con.close()

    def project(slug, repo_name):
        """A registered project with a run and a task: root row + project store."""
        repo = os.path.join(tmp, repo_name)
        data = os.path.join(repo, ".matrixkoder")
        os.makedirs(data, exist_ok=True)
        con = mod.open_root(mkh)
        try:
            con.execute("INSERT OR IGNORE INTO projects(slug, name, repo, data_dir, mode, db_path)"
                        " VALUES (?,?,?,?,?,NULL)",
                        (slug, slug, os.path.realpath(repo), os.path.realpath(data), "home"))
            con.commit()
        finally:
            con.close()
        con = mod.open_project(mkh, slug, data_dir=data)
        try:
            con.execute("INSERT OR IGNORE INTO runs(id, status) VALUES ('r1', 'active')")
            con.commit()
            mod.add_task(con, "r1", "T1", "task", title="one")
        finally:
            con.close()
        return data

    def switch(slug, data, repo_name):
        """L2's real migration tool, on the temp store, with the bus left running."""
        p = subprocess.run([sys.executable, os.path.join(HERE, "mk-migrate.py"), "--switch",
                            "--slug", slug, "--data", data, "--home", mkh,
                            "--repo", os.path.join(tmp, repo_name)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise AssertionError(f"mk-migrate --switch {slug}: "
                                 f"{p.stdout[-200:]} {p.stderr[-200:]}")

    def serve_lines():
        out = subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True,
                             text=True, timeout=10).stdout
        return [line.strip() for line in out.splitlines()
                if "mk-bus.py" in line and "serve" in line and home_dir in line]

    def one_bus(what, timeout=10.0):
        """Exactly one serve for this home. Bounded: `ensure`'s adopt path spawns a child that
        exits with "bus already running" the moment it loses the lock, so a count taken
        mid-adopt sees it. A second LONG-LIVED bus is what this refuses."""
        deadline = time.time() + timeout
        seen = serve_lines()
        while time.time() < deadline:
            seen = serve_lines()
            if len(seen) <= 1:
                return len(seen)
            time.sleep(0.1)
        raise AssertionError(f"{what}: {len(seen)} buses for one home: {seen}")

    def stop_bus():
        path = os.path.join(bus_dir, "bus.json")
        if not os.path.isfile(path):
            return
        pid = (read_json(path) or {}).get("pid")
        if isinstance(pid, int) and pid > 1:
            stop_process(pid)
        deadline = time.time() + 5.0
        while time.time() < deadline and os.path.isfile(path):
            time.sleep(0.05)

    try:
        # ---- fixtures: one migrated, one pre-migration, one still on the legacy layout ----
        migs_data = project("migs", "migs-repo")
        switch("migs", migs_data, "migs-repo")
        migs_folder = os.path.join(mkh, "projects", "migs")
        assert os.path.isfile(_load_mkpaths().db_in(migs_folder)), \
            "the switched store is not in the C2 project folder"
        plain_data = project("plain", "plain-repo")
        own_data = project("own", "own-repo")

        # ---- 1. one bus, started on the migrated project's folder -------------------------
        rc, res, err = cli(["ensure", "--run", "r1", "--data", migs_folder])
        assert rc == 0 and res.get("started") is True, (res, err)
        pids.append(res["pid"])
        first_pid = res["pid"]
        for name in ("bus.json", "bus-tokens.json", "bus-tokens.lock", "bus.lock",
                     "bus-ensure.lock"):
            assert os.path.isfile(os.path.join(bus_dir, name)), f"{name} is not in <home>/bus/"
        assert oct(os.stat(os.path.join(bus_dir, "bus.json")).st_mode & 0o777) == "0o600", \
            "bus.json is not 0600"
        for legacy in ("bus.json", "bus-tokens.json", "bus-tokens.lock", "bus.lock",
                       "bus-ensure.lock", "ring-queue.json", "bus-lanes"):
            assert not os.path.exists(os.path.join(mkh, legacy)), \
                f"{legacy} is still at the pre-L6.1 home root"
        assert not os.path.exists(os.path.join(migs_folder, "bus.json")), \
            "store mode wrote a per-project bus.json"

        # ---- 2. one message per project, each in its own store -----------------------------
        lane = os.path.join(tmp, "lane")
        os.makedirs(lane, exist_ok=True)
        rc, tok, _ = cli(["token", "--lane", lane, "--task", "T1", "--run", "r1",
                          "--data", migs_folder])
        assert rc == 0 and tok.get("ok"), tok
        lanes_dir = os.path.join(bus_dir, "lanes")
        assert os.path.isdir(lanes_dir) and os.listdir(lanes_dir), \
            "the lane token file is not under <bus dir>/lanes/"
        lane_tok_file = os.path.join(lanes_dir, os.listdir(lanes_dir)[0])
        assert oct(os.stat(lane_tok_file).st_mode & 0o777) == "0o600", "lane token is not 0600"
        cfg = read_json(lane_tok_file)
        base, host = cfg["url"], cfg["url"].split("//", 1)[1]

        def post(body, token=None):
            req = urllib.request.Request(
                base + "/send", data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + (token or cfg["token"]),
                         "Host": host}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                try:
                    return e.code, json.loads(raw)
                except ValueError:
                    return e.code, {"error": raw}

        st, out = post({"run": "r1", "task": "T1", "kind": "info", "text": "for migs"})
        assert st == 200 and out.get("message", {}).get("project") == "migs", (st, out)
        for slug, data, text in (("plain", plain_data, "for plain"),
                                 ("own", own_data, "before switch")):
            rc, res, err = cli(["send", "--data", data, "--run", "r1", "--task", "T1",
                                "--kind", "info", "--from", "orch", "--to", "T1", "--text", text])
            assert rc == 0 and res.get("ok"), (res, err)
            assert res["message"]["project"] == slug, res
        migs_texts = texts(_load_mkpaths().db_in(migs_folder))
        plain_texts = texts(_load_mkpaths().db_in(plain_data))
        assert "for migs" in migs_texts and "for plain" not in migs_texts, migs_texts
        assert "for plain" in plain_texts and "for migs" not in plain_texts, plain_texts

        # ---- 3. the bus's OWN project switches while the bus runs (todo 108) ---------------
        assert "before switch" in texts(_load_mkpaths().db_in(own_data)), \
            "the pre-switch message must be in the legacy store"
        switch("own", own_data, "own-repo")   # the bus keeps running through the switch
        own_folder = os.path.join(mkh, "projects", "own")
        rc, res, err = cli(["send", "--data", own_data, "--run", "r1", "--task", "T1",
                            "--kind", "info", "--from", "orch", "--to", "T1",
                            "--text", "after switch"])
        assert rc == 0 and res.get("ok"), (res, err)
        new_texts = texts(_load_mkpaths().db_in(own_folder))
        old_texts = texts(_load_mkpaths().db_in(own_data))
        assert "before switch" in new_texts, "the switch did not carry the pre-switch message"
        assert "after switch" in new_texts, f"the new project DB missed the message: {new_texts}"
        assert "after switch" not in old_texts, \
            f"the legacy store was written after the switch: {old_texts}"
        # the bus followed without a restart, and an ensure sees the SAME bus, never a second
        rc, again, err = cli(["ensure", "--run", "r1", "--data", migs_folder])
        assert rc == 0 and again.get("started") is False and again.get("pid") == first_pid, \
            (again, err)
        assert one_bus("after the own-project switch") == 1

        # ---- 4. the watch cursor lives in the project folder; the old one is carried over --
        # L10.5: the cursor is a watch_cursors row; the first watch imports the MAX of the legacy file
        # (seq 1) and the C2-folder file (seq 0) once, and neither file is written again.
        legacy_cursor = _load_mkpaths().legacy_cursors(own_data)
        write_json(legacy_cursor, {"r1|*": {"seq": 1}}, mode=0o600)
        folder_cursor = os.path.join(own_folder, os.path.basename(_paths("target_watch_cursors", "x")))
        write_json(folder_cursor, {"r1|*": {"seq": 0}}, mode=0o600)
        folder_bytes = open(folder_cursor, "rb").read()
        assert cursor_row(own_data, "r1|*") is None
        w = subprocess.run([sys.executable, os.path.abspath(__file__), "watch", "--data",
                            own_data, "--run", "r1", "--max-messages", "1"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        seen = [x for x in w.stdout.strip().splitlines() if x.strip()]
        assert len(seen) == 1 and "after switch" in seen[0], (w.stdout, w.stderr)
        assert cursor_row(own_data, "r1|*") == 2, cursor_row(own_data, "r1|*")
        assert open(folder_cursor, "rb").read() == folder_bytes, "watch wrote the C2 cursor file"
        assert os.path.isfile(legacy_cursor), "the legacy cursor file must survive the import"
        assert read_json(legacy_cursor) == {"r1|*": {"seq": 1}}, "watch wrote the legacy cursor file"
        # a restarted watch resumes from the table alone, with both files gone
        os.remove(legacy_cursor), os.remove(folder_cursor)
        assert cursor_get(own_data, "r1|*") == 2

        # ---- 5. a pre-L6.1 bus is adopted, never duplicated ---------------------------------
        # SIGKILL, not a stop: a killed server leaves its bus.json behind, which is exactly
        # the pre-L6.1 state the take-over has to adopt (a clean stop removes the file).
        dead = first_pid
        os.kill(dead, signal.SIGKILL)
        deadline = time.time() + 5.0
        while time.time() < deadline and is_pid_alive(dead):
            time.sleep(0.05)
        assert not is_pid_alive(dead), "the killed bus is still alive"
        assert os.path.isfile(os.path.join(bus_dir, "bus.json")), "a killed bus leaves bus.json"
        for name in ("bus.json", "bus-tokens.json", "bus-tokens.lock", "bus.lock",
                     "bus-ensure.lock"):
            shutil.copy2(os.path.join(bus_dir, name), os.path.join(mkh, name))
            os.remove(os.path.join(bus_dir, name))
        os.makedirs(os.path.join(mkh, "bus-lanes"), exist_ok=True)
        shutil.copy2(lane_tok_file, os.path.join(mkh, "bus-lanes",
                                                 os.path.basename(lane_tok_file)))
        os.remove(lane_tok_file)
        rc, res2, err = cli(["ensure", "--run", "r1", "--data", migs_folder])
        assert rc == 0 and res2.get("started") is True, (res2, err)
        pids.append(res2["pid"])
        assert isinstance(res2.get("port"), int) and res2["port"] > 0, res2
        assert os.path.isfile(os.path.join(bus_dir, "bus.json")), "the new bus.json is missing"
        assert one_bus("after the take-over") == 1
        for legacy in ("bus.json", "bus-tokens.json", "bus-tokens.lock", "bus-lanes"):
            assert os.path.exists(os.path.join(mkh, legacy)), \
                f"the legacy {legacy} was deleted by the take-over"
        adopted_path = _paths("lane_token", lane)
        assert adopted_path == lane_tok_file, adopted_path
        assert (read_json(adopted_path) or {}).get("token") == cfg["token"], \
            "the armed lane's token was not carried over"
        st, out = post({"run": "r1", "task": "T1", "kind": "info", "text": "adopted token"},
                       token=cfg["token"])
        assert st == 200, (st, out)
        assert "adopted token" in texts(_load_mkpaths().db_in(migs_folder)), texts(
            _load_mkpaths().db_in(migs_folder))
    finally:
        stop_bus()
        for p in pids:
            stop_process(p)
        for line in subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True,
                                   text=True, timeout=10).stdout.splitlines():
            if "mk-bus.py" in line and "serve" in line and home_dir in line:
                head = line.strip().split(None, 1)[0]
                if head.isdigit():
                    with contextlib.suppress(OSError):
                        os.kill(int(head), signal.SIGKILL)
        shutil.rmtree(tmp, ignore_errors=True)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _legacy_bus_token_self_test():
    """R2 (review HIGH 2): an ADOPTED pre-L6.1 bus still validates tokens against the
    LEGACY <home>/bus-tokens.json, so a token minted only under bus/ answers 401 on that
    bus until it restarts.

    One temp $HOME with every MK pin unset (the production resolution) and a store-mode
    project. A legacy bus is alive — its pid sits in the pre-L6.1 bus.json at the home
    root — and `token` must then write the mint BOTH at the new path and into the legacy
    file under the legacy lock, so the running old bus accepts it at once. With NO legacy
    bus alive the mint must create no legacy path at all: no bus.json, no bus-tokens.json,
    no lock, no bus-lanes/."""
    import shutil

    mod = _mkdb_helpers_ready()
    if mod is None:
        print("legacy-bus-token self-test skipped: mkdb live writers not present")
        return
    tmp = tempfile.mkdtemp(prefix="mk-bus-legacy-token-")
    pids = []
    saved = {k: os.environ.get(k) for k in
             ("HOME", "MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA", "MK_BUS_DIR",
              "MK_BUS_TOKEN_FILE", "MK_SELFTEST")}

    def cli(args, home):
        p = subprocess.run([sys.executable, os.path.abspath(__file__)] + args,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           env=dict(os.environ, HOME=home))
        lines = [x.strip() for x in p.stdout.strip().splitlines() if x.strip()]
        last = json.loads(lines[-1]) if lines else {}
        if p.returncode != 0:
            raise AssertionError(f"CLI {args} rc={p.returncode}: {p.stderr.strip()[-300:]}")
        return last

    def scrub(home):
        for key in ("MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA", "MK_BUS_DIR",
                    "MK_BUS_TOKEN_FILE"):
            os.environ.pop(key, None)   # the production path: no pin resolves the home
        os.environ["HOME"] = home
        os.environ["MK_SELFTEST"] = "1"  # mkdb's C16 guard: writes stay in the temp tree

    def seed(home, name):
        """A store-mode project: mkdb creates the project store the mint resolves to."""
        mkh = os.path.join(home, ".matrixkoder")
        os.makedirs(mkh, exist_ok=True)
        data = os.path.join(tmp, name, "data")
        os.makedirs(data, exist_ok=True)
        mod.open_project(mkh, "matrixkoder", data_dir=data).close()
        return mkh, data

    try:
        # ---- a legacy bus is ALIVE: its pid sits in the pre-L6.1 bus.json --------------
        home = os.path.join(tmp, "home")
        os.makedirs(home, exist_ok=True)
        scrub(home)
        mkh, data = seed(home, "r1")
        legacy_bus_json = os.path.join(mkh, "bus.json")           # pre-L6.1: the home root
        legacy_bus_tokens = os.path.join(mkh, "bus-tokens.json")  # pre-L6.1: the home root
        proc = subprocess.Popen(["sleep", "120"])
        pids.append(proc.pid)
        write_json(legacy_bus_json, {"pid": proc.pid, "port": 1, "token": "t"}, mode=0o600)
        write_json(legacy_bus_tokens, {}, mode=0o600)

        lane = os.path.join(tmp, "lane")
        os.makedirs(lane, exist_ok=True)
        res = cli(["token", "--data", data, "--lane", lane, "--task", "T9", "--run", "r1"], home)
        assert res.get("ok"), res
        new_tokens = read_json(_paths("bus_tokens", data), {}) or {}
        assert res["token"] in new_tokens, "the mint is not in <home>/bus/bus-tokens.json"
        legacy = read_json(legacy_bus_tokens, {}) or {}
        assert res["token"] in legacy, \
            "HIGH 2: the minted token is missing from the legacy bus-tokens.json the " \
            "adopted pre-L6.1 bus still validates against"

        # ---- no legacy bus alive: NO legacy file may appear -----------------------------
        home2 = os.path.join(tmp, "home2")
        os.makedirs(home2, exist_ok=True)
        scrub(home2)
        mkh2, data2 = seed(home2, "r2")
        lane2 = os.path.join(tmp, "lane2")
        os.makedirs(lane2, exist_ok=True)
        res2 = cli(["token", "--data", data2, "--lane", lane2, "--task", "T9",
                    "--run", "r1"], home2)
        assert res2.get("ok"), res2
        for name in ("bus.json", "bus-tokens.json", "bus-tokens.lock", "bus-lanes"):
            assert not os.path.exists(os.path.join(mkh2, name)), \
                f"token created the legacy {os.path.join(mkh2, name)} with no old bus alive"
        new2 = read_json(_paths("bus_tokens", data2), {}) or {}
        assert res2["token"] in new2, "the mint is not in <home2>/bus/bus-tokens.json"
    finally:
        for pid in pids:
            stop_process(pid)
        shutil.rmtree(tmp, ignore_errors=True)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _store_self_test_body(mod):
    """Real store round-trip. Runs only after append_event exists.

    File-mode self_test (the 31 checks above) is not touched. This body is
    reached only when _mkdb_helpers_ready sees append_event.
    """
    import shutil

    tmp = tempfile.mkdtemp(prefix="mk-bus-store-")
    pids = []
    # One temp MK home under every name (C16): the spawned serve and its
    # mk-state children resolve $MK_HOME/$MATRIXKODER_HOME here, never real.
    store_home = os.path.join(tmp, "home")
    os.makedirs(store_home, exist_ok=True)
    store_saved = {k: os.environ.get(k) for k in ("MK_BUS_DIR", "MK_HOME", "MATRIXKODER_HOME", "MK_SELFTEST")}
    os.environ["MK_BUS_DIR"] = store_home
    os.environ["MK_HOME"] = store_home
    os.environ["MATRIXKODER_HOME"] = store_home
    os.environ["MK_SELFTEST"] = "1"
    # The real herdr is never called from a test: drop every PATH dir that holds one.
    # Ring cases put their own fake `herdr` first.
    orig_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(
        d for d in orig_path.split(os.pathsep) if not os.path.exists(os.path.join(d, "herdr")))

    def post(port, token, body, timeout=10.0):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/send",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "Host": f"127.0.0.1:{port}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {"error": raw}
            return e.code, parsed

    def spawn_prefer(data_dir, run, prefer):
        return spawn(data_dir, run, prefer)

    def spawn(data_dir, run, prefer=None, dead_stdout=False):
        # dead_stdout: stdout is a pipe whose read end is closed once the bus is healthy, as after `ensure`
        argv = [sys.executable, os.path.abspath(__file__), "serve", "--data", data_dir]
        if run:
            argv += ["--run", run]
        if prefer:
            argv += ["--prefer-port", str(prefer)]
        proc = subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=subprocess.PIPE if dead_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pids.append(proc.pid)
        bus_json = os.path.join(os.environ.get("MK_BUS_DIR", data_dir), "bus.json")
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if os.path.exists(bus_json):
                bdata = read_json(bus_json) or {}
                pid, port = bdata.get("pid"), bdata.get("port")
                if pid and port and pid == proc.pid and is_pid_alive(pid):
                    try:
                        req = urllib.request.Request(
                            f"http://127.0.0.1:{port}/health",
                            headers={"Host": f"127.0.0.1:{port}"},
                        )
                        with urllib.request.urlopen(req, timeout=1.0) as resp:
                            body = json.loads(resp.read().decode())
                            if resp.status == 200 and body.get("pid") == proc.pid:
                                if dead_stdout:
                                    proc.stdout.close()
                                return pid, port
                    except Exception:
                        pass
            if proc.poll() is not None:
                raise AssertionError(f"serve exited {proc.returncode} before health")
            time.sleep(0.05)
        raise AssertionError("store serve did not become healthy")

    def kill(pid):
        stop_process(pid)
        deadline = time.time() + 5.0
        while time.time() < deadline and is_pid_alive(pid):
            time.sleep(0.05)

    def sse_replay(port, token, last_id, expect):
        """Read SSE until `expect` message events have arrived, then close.

        The stream stays open (keepalive comments), so a bare read would hang.
        lane=* with the orchestrator token: a lane token only sees messages
        addressed to that lane, and an ask is addressed to orch.
        """
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/events?lane=*",
            headers={
                "Authorization": f"Bearer {token}",
                "Last-Event-ID": str(last_id),
                "Host": f"127.0.0.1:{port}",
            },
        )
        events = []
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                buf = ""
                while len(events) < expect:
                    chunk = resp.readline()
                    if not chunk:
                        break
                    buf += chunk.decode("utf-8")
                    while "\n\n" in buf:
                        block, buf = buf.split("\n\n", 1)
                        data_lines = [ln[5:].strip() for ln in block.splitlines() if ln.startswith("data:")]
                        if data_lines:
                            events.append(json.loads("".join(data_lines)))
        except (TimeoutError, urllib.error.URLError, socket.timeout):
            if expect < 50:
                raise
        return events


    def mint_orch(data_dir):
        # serve retires every orch token on startup, so mint after each spawn.
        orch_tok = secrets.token_urlsafe(32)
        tok_dir = os.environ.get("MK_BUS_DIR", data_dir)
        tokens_path = os.path.join(tok_dir, "bus-tokens.json")
        lock_path = os.path.join(tok_dir, "bus-tokens.lock")
        lfd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lfd, fcntl.LOCK_EX)
            tokens = read_json(tokens_path, default={})
            tokens[orch_tok] = {
                "token": orch_tok, "role": "orch", "run": "r1", "task": "",
                "lane": "", "project": "proj", "created_at": now_iso(),
            }
            write_json(tokens_path, tokens, mode=0o600)
        finally:
            fcntl.flock(lfd, fcntl.LOCK_UN)
            os.close(lfd)
        return orch_tok

    try:
        data = os.path.join(tmp, "data")
        os.makedirs(data, exist_ok=True)
        # Root bus: a store is used only when the data dir is a registered
        # project. The test home is temp under every name, never the real one.
        home = store_home
        os.environ["MK_BUS_DIR"] = home
        os.environ["MK_HOME"] = home
        os.environ["MATRIXKODER_HOME"] = home
        root = mod.open_root(home)
        try:
            root.execute(
                "INSERT INTO projects(slug, name, repo, data_dir, mode, created) VALUES (?,?,?,?,?,?)",
                ("proj", "proj", "", data, "managed", "t"),
            )
            root.commit()
        finally:
            root.close()
        # Public API only. open_project creates mk.db; add_task inserts the task
        # transition() looks up. A runs row is not required by transition().
        # append_event refuses an unknown run. import_history is the public run writer.
        mod.import_history(data, None, "r1", run_row={"id": "r1", "status": "active"}, data_dir=data)
        con = mod.open_project(data, data_dir=data)
        try:
            mod.add_task(con, "r1", "T1", "task", title="store self-test")
            mod.add_task(con, "r1", "T2", "task", title="working lane")
            mod.add_task(con, "r1", "T9", "task", title="real herdr lane")
        finally:
            con.close()
        assert os.path.isfile(_load_mkpaths().db_in(data)), "open_project did not create mk.db"

        lane = os.path.join(tmp, "lane")
        os.makedirs(lane, exist_ok=True)
        pid, port = spawn(data, "r1")
        orch = mint_orch(data)
        rc_tok = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "token",
             "--lane", lane, "--task", "T1", "--run", "r1", "--data", data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert rc_tok.returncode == 0, rc_tok.stderr
        token = json.loads(rc_tok.stdout.strip().splitlines()[-1])["token"]

        code, ask = post(port, token, {"kind": "ask", "text": "store ask"})
        assert code == 200 and ask["ok"] is True, ask
        ask_id = ask["message"]["id"]
        code, ans = post(port, orch, {
            "run": "r1", "task": "T1", "kind": "answer", "text": "store answer",
            "from": "orch", "to": "T1", "reply_to": ask_id,
        })
        assert code == 200 and ans["ok"] is True, ans
        code, rep = post(port, token, {"kind": "report", "text": "round:1\nfirst report"})
        assert code == 200 and rep["ok"] is True, rep
        assert rep.get("artifact", {}).get("rev") == 1, rep

        rows = mod.timeline(data, None, "r1", "T1", data_dir=data)
        kinds = [r["kind"] for r in rows]
        assert "ask" in kinds and "answer" in kinds and "report" in kinds, kinds
        sent_texts = {"store ask", "store answer", "round:1\nfirst report"}
        got_texts = {r["text"] for r in rows}
        assert sent_texts <= got_texts, (sent_texts, got_texts)

        sent_seqs = [ask["message"]["seq"], ans["message"]["seq"], rep["message"]["seq"]]
        kill(pid)
        pid2, port2 = spawn(data, "r1")
        orch = mint_orch(data)
        first = min(sent_seqs)
        replayed = sse_replay(port2, orch, first - 1, len(sent_seqs))
        replay_seqs = [m["seq"] for m in replayed]
        assert replay_seqs == sent_seqs, (replay_seqs, sent_seqs)
        assert [m["text"] for m in replayed] == [
            "store ask", "store answer", "round:1\nfirst report",
        ], replayed
        live_ids = [ask["message"]["id"], ans["message"]["id"], rep["message"]["id"]]
        assert [m["id"] for m in replayed] == live_ids, (replayed, live_ids)
        assert all(i == f"m-{s}" for i, s in zip(live_ids, sent_seqs)), (live_ids, sent_seqs)
        assert len(replay_seqs) == len(set(replay_seqs)), replay_seqs
        # Answer a replayed ask and ack a replayed message; the level survives another restart.
        replayed_ask = next(m for m in replayed if m["kind"] == "ask")
        code, ans2 = post(port2, orch, {
            "run": "r1", "task": "T1", "kind": "answer", "text": "answered after restart",
            "from": "orch", "to": "T1", "reply_to": replayed_ask["id"],
        })
        # The original ask was already answered before the restart.
        assert code == 400, (code, ans2)
        code, ask2 = post(port2, token, {"kind": "ask", "text": "ask after restart"})
        assert code == 200 and ask2["message"]["id"].startswith("m-"), ask2
        kill(pid2)
        pid3, port3 = spawn(data, "r1")
        orch = mint_orch(data)
        replayed2 = sse_replay(port3, orch, 0, 99)
        again = next(m for m in replayed2 if m["id"] == ask2["message"]["id"])
        assert again["id"] == ask2["message"]["id"]
        code, ans3 = post(port3, orch, {
            "run": "r1", "task": "T1", "kind": "answer", "text": "answer of replayed ask",
            "from": "orch", "to": "T1", "reply_to": again["id"],
        })
        assert code == 200 and ans3["ok"] is True, ans3
        code, acked = post(port3, orch, {
            "run": "r1", "task": "T1", "kind": "ack", "text": "",
            "from": "orch", "to": "T1", "reply_to": again["id"], "ack_level": 3,
        })
        assert code == 200 and acked.get("ack_level") == 3, acked
        kill(pid3)
        pid2, port2 = spawn(data, "r1")
        orch = mint_orch(data)
        replayed3 = sse_replay(port2, orch, 0, 99)
        persisted = next(m for m in replayed3 if m["id"] == again["id"])
        assert persisted["ack_level"] == 3, persisted
        assert persisted["id"] == f"m-{persisted['seq']}", persisted

        code, rep2 = post(port2, token, {"kind": "report", "text": "round:1\nsecond report"})
        assert code == 200 and rep2.get("artifact", {}).get("rev") == 2, rep2

        before = mod.open_project(data, data_dir=data)
        try:
            n_before = before.execute("SELECT COUNT(*) c FROM change_log").fetchone()["c"]
            status_before = before.execute(
                "SELECT status FROM tasks WHERE run=? AND id=?", ("r1", "T1")
            ).fetchone()["status"]
        finally:
            before.close()
        code, st = post(port2, orch, {
            "run": "r1", "task": "T1", "kind": "status", "text": "building",
            "from": "orch", "to": "T1",
        })
        assert code == 200 and st["ok"] is True, st
        after = mod.open_project(data, data_dir=data)
        try:
            nums = [r["change_no"] for r in after.execute("SELECT change_no FROM change_log")]
            status_now = after.execute(
                "SELECT status FROM tasks WHERE run=? AND id=?", ("r1", "T1")
            ).fetchone()["status"]
        finally:
            after.close()
        assert len(nums) == n_before + 1, (n_before, nums)
        assert status_now == "building", status_now

        ev_before = mod.open_project(data, data_dir=data)
        try:
            n_events = ev_before.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        finally:
            ev_before.close()
        # Connected client: a rejected status must not appear on the stream.
        sse_box = {"events": [], "err": None, "stop": False}
        def _listen():
            req = urllib.request.Request(
                f"http://127.0.0.1:{port2}/events?lane=*",
                headers={
                    "Authorization": f"Bearer {orch}",
                    "Last-Event-ID": "999999999",
                    "Host": f"127.0.0.1:{port2}",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    buf = ""
                    while not sse_box["stop"]:
                        chunk = resp.readline()
                        if not chunk:
                            break
                        buf += chunk.decode("utf-8")
                        while "\n\n" in buf:
                            block, buf = buf.split("\n\n", 1)
                            data_lines = [ln[5:].strip() for ln in block.splitlines() if ln.startswith("data:")]
                            if data_lines:
                                sse_box["events"].append(json.loads("".join(data_lines)))
            except Exception as e:
                if not sse_box["stop"]:
                    sse_box["err"] = repr(e)
        listener = threading.Thread(target=_listen, daemon=True)
        listener.start()
        time.sleep(0.2)
        code, bad = post(port2, orch, {
            "run": "r1", "task": "T1", "kind": "status", "text": "not-a-status",
            "from": "orch", "to": "T1",
        })
        assert code == 400, (code, bad)
        # Rejected transition: task exists but the move is not a known status,
        # and a second case uses a status word transition() itself refuses if any.
        code, bad2 = post(port2, orch, {
            "run": "r1", "task": "NO-SUCH", "kind": "status", "text": "building",
            "from": "orch", "to": "NO-SUCH",
        })
        assert code == 400, (code, bad2)
        ev_after = mod.open_project(data, data_dir=data)
        try:
            n_events_after = ev_after.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        finally:
            ev_after.close()
        assert n_events_after == n_events, (n_events, n_events_after)
        sse_box["stop"] = True
        listener.join(timeout=3)
        assert sse_box["events"] == [], sse_box
        still = mod.open_project(data, data_dir=data)
        try:
            unchanged = still.execute(
                "SELECT status FROM tasks WHERE run=? AND id=?", ("r1", "T1")
            ).fetchone()["status"]
        finally:
            still.close()
        assert unchanged == "building", unchanged
        assert status_before == "planned", status_before

        # One-shot: the live serve child raises on the next store_append_event.
        open(os.path.join(data, "append-fail"), "w").close()
        code, inj = post(port2, orch, {
            "run": "r1", "task": "T1", "kind": "status", "text": "review",
            "from": "orch", "to": "T1",
        })
        assert code == 500, (code, inj)
        # The transition is the truth even though the bus event was not stored.
        chk = mod.open_project(data, data_dir=data)
        try:
            st_now = chk.execute(
                "SELECT status FROM tasks WHERE run=? AND id=?", ("r1", "T1")
            ).fetchone()["status"]
            n_bus = chk.execute(
                "SELECT COUNT(*) c FROM events WHERE chan=? AND kind=?", ("bus", "status")
            ).fetchone()["c"]
        finally:
            chk.close()
        assert st_now == "review", st_now
        # The injected failure must not have added a bus status row for this send.
        # (the earlier successful "building" status is the only bus status row)
        assert n_bus == 1, n_bus

        big = "a" * 100000
        code, big_res = post(port2, token, {"kind": "report", "text": "round:2\n" + big}, timeout=20.0)
        assert code == 200 and big_res["ok"] is True, (code, big_res.get("error"))
        # X2.3: a body above 128 KiB is PAGED, not refused — the whole text still lands as the
        # round's artifact (2 revisions above), and the reply carries it. Only a body past the
        # artifact writer's own ceiling (mkdb.ARTIFACT_MAX) is an error.
        over = "b" * (STORE_TEXT_MAX + 1)
        code, over_res = post(port2, token,
                              {"kind": "report", "text": "round:3\n" + over}, timeout=20.0)
        assert code == 200 and over_res["ok"] is True, (code, over_res.get("error"))
        assert over_res["artifact"]["rev"] == 1, over_res
        con = mod.open_project(data, data_dir=data)
        try:
            row = con.execute(
                "SELECT length(text) n FROM artifacts WHERE run='r1' AND task='T1'"
                " AND kind='report' AND round=3").fetchone()
        finally:
            con.close()
        assert row and row["n"] == STORE_TEXT_MAX + 1, row
        too_big = "c" * (PAGED_TEXT_MAX + 1)
        code, too_big_res = post(port2, token,
                                 {"kind": "report", "text": too_big}, timeout=20.0)
        assert code == 400, (code, too_big_res)

        data_b = os.path.join(tmp, "data-b")
        os.makedirs(data_b, exist_ok=True)
        root_b = mod.open_root(home)
        try:
            root_b.execute(
                "INSERT INTO projects(slug, name, repo, data_dir, mode, created) VALUES (?,?,?,?,?,?)",
                ("projb", "projb", "", data_b, "managed", "t"),
            )
            root_b.commit()
        finally:
            root_b.close()
        warm = mod.open_project(data_b, data_dir=data_b)
        warm.close()
        mod.import_history(data_b, None, "r1", run_row={"id": "r1", "status": "active"}, data_dir=data_b)
        con_b = mod.open_project(data_b, data_dir=data_b)
        try:
            mod.add_task(con_b, "r1", "T1", "task")
        finally:
            con_b.close()
        # One root bus serves both projects, so B is not a second server.
        code, cross = post(port2, token, {"kind": "ask", "text": "cross", "project": "projb"})
        assert code in (401, 403), (code, cross)
        # --- D6B.1: two projects, one bus, no cross-project read or write ---
        assert os.path.isfile(os.path.join(home, "bus.json")), "root bus.json missing"
        assert not os.path.isfile(os.path.join(data, "bus.json")), "store mode wrote a per-project bus.json"
        lane_file_old = os.path.join(lane, ".matrixkoder-bus.json")
        assert not os.path.isfile(lane_file_old), "token file still inside the lane"
        # L6.1: <bus dir>/lanes/<sha>.json. store_home is MK_BUS_DIR, which IS the bus dir.
        lanes_dir = os.path.join(home, "lanes")
        assert os.path.isdir(lanes_dir) and os.listdir(lanes_dir), "token file not under lanes/"
        tok_path = os.path.join(lanes_dir, os.listdir(lanes_dir)[0])
        assert oct(os.stat(tok_path).st_mode & 0o777) == "0o600", oct(os.stat(tok_path).st_mode)
        # orchestrator token of project A cannot write project B
        code, cross2 = post(port2, orch, {"run": "r1", "task": "T1", "kind": "info", "text": "from A", "from": "orch", "to": "T1", "project": "projb"})
        assert code == 403, (code, cross2)
        # killing A's host heartbeat does not drop B while B heartbeats
        bjson = read_json(os.path.join(home, "bus.json"))
        bjson["projects"]["proj"] = time.time() - 10**7
        bjson["projects"]["projb"] = time.time()
        write_json(os.path.join(home, "bus.json"), bjson, mode=0o600)
        time.sleep(0.3)
        req = urllib.request.Request("http://127.0.0.1:%d/health" % port2, headers={"Host": "127.0.0.1:%d" % port2})
        with urllib.request.urlopen(req, timeout=2) as resp:
            assert resp.status == 200

        # --- D6B R2: same run id and task id in both projects ---
        def _table_hash(data_dir):
            import hashlib
            con = mod.open_project(data_dir, data_dir=data_dir)
            try:
                blob = []
                for table in ("events", "acks", "artifacts", "tasks"):
                    rows = [tuple(r) for r in con.execute("SELECT * FROM %s ORDER BY 1" % table)]
                    blob.append(repr(rows))
            finally:
                con.close()
            return hashlib.sha256("\n".join(blob).encode()).hexdigest()

        def _http(method, path, token, body=None, timeout=3.0):
            data_b = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:%d%s" % (port2, path),
                data=data_b,
                headers={
                    "Authorization": "Bearer " + token,
                    "Host": "127.0.0.1:%d" % port2,
                    "Content-Type": "application/json",
                },
                method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode()
                    return resp.status, json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = {"error": raw}
                return e.code, parsed

        # Distinct bodies under the SAME run id and task id.
        code, sec_a = post(port2, orch, {"run": "r1", "task": "T1", "kind": "info", "text": "SECRET FOR A", "from": "orch", "to": "T1", "project": "proj"})
        assert code == 200, (code, sec_a)
        lane_b = os.path.join(tmp, "lane-b")
        os.makedirs(lane_b, exist_ok=True)
        rc_b = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "token", "--lane", lane_b, "--task", "T1", "--run", "r1", "--data", data_b],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert rc_b.returncode == 0, rc_b.stderr
        token_b = json.loads(rc_b.stdout.strip().splitlines()[-1])["token"]
        orch_b = secrets.token_urlsafe(32)
        tp = os.path.join(home, "bus-tokens.json")
        tl = os.path.join(home, "bus-tokens.lock")
        lfd = os.open(tl, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lfd, fcntl.LOCK_EX)
            toks = read_json(tp, default={})
            toks[orch_b] = {"token": orch_b, "role": "orch", "run": "r1", "task": "", "lane": "", "project": "projb", "created_at": now_iso()}
            write_json(tp, toks, mode=0o600)
        finally:
            fcntl.flock(lfd, fcntl.LOCK_UN)
            os.close(lfd)
        code, sec_b = post(port2, orch_b, {"run": "r1", "task": "T1", "kind": "ask", "text": "SECRET FOR B", "from": "orch", "to": "T1", "project": "projb"})
        assert code == 200, (code, sec_b)
        # the shared bus belongs to project A: the orchestrator CLI of project B uses B's own
        # orch token, so its send lands in B's store and run, never in A's (live bug 2026-10-13)
        assert _orch_token(data_b) == orch_b, "B's CLI must pick B's orch token, not bus.json's"
        assert _orch_token(data) != orch_b
        rc_cli = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "send", "--data", data_b, "--run", "r1", "--task", "T1",
             "--kind", "task", "--from", "orch", "--to", "T1", "--text", "B CLI SEND"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        res_cli = json.loads(rc_cli.stdout.strip().splitlines()[-1])
        assert rc_cli.returncode == 0 and res_cli["ok"] and res_cli["message"]["project"] == "projb", (rc_cli.stderr, res_cli)
        hash_before = _table_hash(data)
        con = mod.open_project(data, data_dir=data)
        try:
            a_status_before = con.execute("SELECT status FROM tasks WHERE run=? AND id=?", ("r1", "T1")).fetchone()["status"]
        finally:
            con.close()

        # B's lane SSE sees only B. Read a few events, then close; the stream stays open.
        req = urllib.request.Request(
            "http://127.0.0.1:%d/events?run=r1&lane=T1" % port2,
            headers={"Authorization": "Bearer " + token_b, "Host": "127.0.0.1:%d" % port2, "Last-Event-ID": "0"},
        )
        seen = []
        try:
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                buf = ""
                while len(seen) < 5:
                    chunk = resp.readline()
                    if not chunk:
                        break
                    buf += chunk.decode()
                    while "\n\n" in buf:
                        block, buf = buf.split("\n\n", 1)
                        data_lines = [ln[5:].strip() for ln in block.splitlines() if ln.startswith("data:")]
                        if data_lines:
                            seen.append(json.loads("".join(data_lines)))
        except Exception:
            pass
        texts = [m.get("text") for m in seen]
        assert "SECRET FOR B" in texts, texts
        assert "SECRET FOR A" not in texts, texts
        assert all(m.get("project") in (None, "projb") for m in seen), seen

        st, waited = _http("GET", "/wait?run=r1&lane=T1&timeout=1", token_b)
        assert st == 200, (st, waited)
        wmsg = (waited or {}).get("message") or {}
        assert wmsg.get("text") != "SECRET FOR A", waited
        assert "SECRET FOR A" not in json.dumps(waited), waited

        st, pend = _http("GET", "/pending?run=r1", orch_b)
        assert st == 200, (st, pend)
        ptexts = [m.get("text") for m in pend.get("pending") or []]
        assert "SECRET FOR A" not in ptexts, pend
        assert "SECRET FOR B" in ptexts, pend

        # B cannot ack, answer, or transition A's message / task.
        a_id = sec_a["message"]["id"]
        st, hij = _http("POST", "/ack", token_b, {"run": "r1", "id": a_id, "ack_level": 3, "project": "proj"})
        assert st == 403, (st, hij)
        st, hij = _http("POST", "/ack", token_b, {"run": "r1", "id": a_id, "ack_level": 3})
        assert st in (400, 403), (st, hij)
        st, hij = _http("POST", "/send", orch_b, {"run": "r1", "task": "T1", "kind": "answer", "text": "hijacked answer", "from": "orch", "to": "T1", "reply_to": a_id, "project": "projb"})
        assert st in (400, 403), (st, hij)
        st, hij = post(port2, orch_b, {"run": "r1", "task": "T1", "kind": "status", "text": "building", "from": "orch", "to": "T1", "project": "projb"})
        assert st == 200, (st, hij)
        # B's own task moved; A's did not.
        con = mod.open_project(data, data_dir=data)
        try:
            a_status = con.execute("SELECT status FROM tasks WHERE run=? AND id=?", ("r1", "T1")).fetchone()["status"]
        finally:
            con.close()
        con = mod.open_project(data_b, data_dir=data_b)
        try:
            b_status = con.execute("SELECT status FROM tasks WHERE run=? AND id=?", ("r1", "T1")).fetchone()["status"]
        finally:
            con.close()
        assert a_status == a_status_before, (a_status_before, a_status)
        assert b_status == "building", b_status
        assert _table_hash(data) == hash_before, "project A store changed during B activity"

        # Root token is read-only on every write, before the body matters.
        root_tok = secrets.token_urlsafe(32)
        lfd = os.open(tl, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lfd, fcntl.LOCK_EX)
            toks = read_json(tp, default={})
            toks[root_tok] = {"token": root_tok, "role": "root", "run": "r1", "task": "T1", "lane": "", "project": "proj", "created_at": now_iso()}
            write_json(tp, toks, mode=0o600)
        finally:
            fcntl.flock(lfd, fcntl.LOCK_UN)
            os.close(lfd)
        for kind in ("ack", "ask", "info", "nudge", "status", "report"):
            st, body = post(port2, root_tok, {"run": "r1", "task": "T1", "kind": kind, "text": "nope", "from": "T1", "to": "orch"})
            assert st == 403, (kind, st, body)
        st, body = _http("POST", "/ack", root_tok, {"run": "r1", "id": a_id, "ack_level": 3})
        assert st == 403, (st, body)
        assert _table_hash(data) == hash_before, "root token wrote project A"

        # --- D6B.2 doorbell with a fake herdr ---
        # agent_status comes from <fake>.status (idle|working), not from the
        # rang log. Default idle when the file is missing.
        fake = os.path.join(tmp, "fake-herdr.py")
        with open(fake, "w") as fh:
            fh.write("import json, sys\n")
            fh.write("log = sys.argv[0] + '.rang'\n")
            fh.write("if len(sys.argv) > 1 and sys.argv[1] == 'prompt':\n")
            fh.write("    with open(log, 'a') as f:\n")
            fh.write("        f.write(sys.argv[2] + ' ' + sys.argv[3] + chr(10))\n")
            fh.write("    print(json.dumps({'result': {'ok': True}}), flush=True)\n")
            fh.write("    sys.exit(0)\n")
            fh.write("if len(sys.argv) > 1 and sys.argv[1] == 'send-keys':\n")
            fh.write("    with open(log, 'a') as f:\n")
            fh.write("        f.write(sys.argv[2] + ' ' + sys.argv[3] + chr(10))\n")
            fh.write("    sys.exit(0)\n")
            fh.write("import os\n")
            fh.write("sf = sys.argv[0] + '.status'\n")
            fh.write("status = open(sf).read().strip() if os.path.exists(sf) else 'idle'\n")
            fh.write("kf = sys.argv[0] + '.kind'\n")
            fh.write("agent = {'agent': open(kf).read().strip()} if os.path.exists(kf) else {}\n")
            fh.write("print(json.dumps({'result': {'agents': [\n")
            fh.write("    dict({'name': 'T1', 'agent_status': status, 'cwd': '/x/T1'}, **agent)]}}))\n")
        os.chmod(fake, 0o755)

        def write_status(text):
            with open(fake + ".status", "w") as sf:
                sf.write(text)

        def inbox_count():
            if not os.path.isfile(fake + ".rang"):
                return 0
            return open(fake + ".rang").read().count("inbox")

        def remint():
            tok = secrets.token_urlsafe(32)
            tp = os.path.join(home, "bus-tokens.json")
            tl = os.path.join(home, "bus-tokens.lock")
            lfd = os.open(tl, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lfd, fcntl.LOCK_EX)
                toks = read_json(tp, default={})
                toks[tok] = {"token": tok, "role": "orch", "run": "r1", "task": "", "lane": "", "project": "proj", "created_at": now_iso()}
                write_json(tp, toks, mode=0o600)
            finally:
                fcntl.flock(lfd, fcntl.LOCK_UN)
                os.close(lfd)
            return tok

        def event_count(kind):
            rings = mod.open_project(data, data_dir=data)
            try:
                return rings.execute("SELECT COUNT(*) c FROM events WHERE kind=?", (kind,)).fetchone()["c"]
            finally:
                rings.close()

        os.environ["MK_BUS_HERDR"] = fake
        os.environ["MK_BUS_SKILL_DIR"] = os.path.join(tmp, "skill")
        os.environ["MK_BUS_RING_INTERVAL_SEC"] = "0.2"
        write_status("idle")
        kill(pid2)
        # Messages queued in the earlier no-herdr phases now survive a restart; this
        # block counts rings from a clean queue.
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(home, "ring-queue.json"))
        pid2, port2 = spawn(data, "r1")
        orch = mint_orch(data)
        btok = remint()
        code, nudge = post(port2, btok, {"run": "r1", "task": "T1", "kind": "nudge", "text": "look", "from": "orch", "to": "T1", "project": "proj"})
        assert code == 200, (code, nudge)
        time.sleep(0.3)
        rang = open(fake + ".rang").read() if os.path.isfile(fake + ".rang") else ""
        assert "T1" in rang and "mk-builder.py inbox" in rang, rang
        # 151a: the ring line carries the act-without-asking sentence (the fake herdr keeps the first line only)
        assert RING_ACT in rang, rang
        # Second nudge while working must not ring.
        write_status("working")
        code, work = post(port2, btok, {"run": "r1", "task": "T1", "kind": "nudge", "text": "busy", "from": "orch", "to": "T1", "project": "proj"})
        assert code == 200, (code, work)
        rang2 = open(fake + ".rang").read()
        assert rang2.count("mk-builder.py inbox") == 1, rang2
        assert event_count("ring") == 1

        # FIX 3: a ring queued while working drains once the lane goes idle,
        # with no further message. Same server: a restart drops the in-memory queue.
        write_status("working")
        before3 = inbox_count()
        code, q = post(port2, btok, {"run": "r1", "task": "T1", "kind": "nudge", "text": "queued", "from": "orch", "to": "T1", "project": "proj"})
        assert code == 200, (code, q)
        time.sleep(0.4)
        assert inbox_count() == before3, (inbox_count(), before3)
        write_status("idle")
        time.sleep(1.0)
        rang3 = open(fake + ".rang").read()
        assert inbox_count() == before3 + 2, rang3
        assert "T1" in rang3 and "mk-builder.py inbox" in rang3

        # F2: a bus whose stdout reader is gone (started by `ensure`) still types its ring.
        # The fake herdr writes a reply, so an inherited dead pipe would SIGPIPE it (-13).
        write_status("idle")
        kill(pid2)
        pid2, port2 = spawn(data, "r1", dead_stdout=True)
        btok = remint()
        typed_f2, failed_f2 = event_count("ring-typed"), event_count("ring-failed")
        code, f2 = post(port2, btok, {"run": "r1", "task": "T1", "kind": "nudge", "text": "dead stdout", "from": "orch", "to": "T1", "project": "proj"})
        assert code == 200, (code, f2)
        deadline = time.time() + 5.0
        while time.time() < deadline and event_count("ring-typed") == typed_f2 and event_count("ring-failed") == failed_f2:
            time.sleep(0.05)
        assert event_count("ring-typed") == typed_f2 + 1, (event_count("ring-typed"), typed_f2)
        assert event_count("ring-failed") == failed_f2, (event_count("ring-failed"), failed_f2)

        # FIX 4a: ring is recorded before typing. A kill between the two
        # leaves kind=ring and no rang line; restart types it once; a second
        # restart does not type it again.
        write_status("idle")
        os.environ["MK_BUS_RING_KILL"] = os.path.join(tmp, "kill-seam")
        kill(pid2)
        pid2, port2 = spawn(data, "r1")
        btok = remint()
        before4 = inbox_count()
        rings_before = event_count("ring")
        try:
            post(port2, btok, {"run": "r1", "task": "T1", "kind": "nudge", "text": "die", "from": "orch", "to": "T1", "project": "proj"}, timeout=2.0)
        except Exception:
            pass
        deadline = time.time() + 5.0
        while time.time() < deadline and is_pid_alive(pid2):
            time.sleep(0.05)
        # The ring runs after the POST returns, so give the seam a moment.
        deadline = time.time() + 3.0
        while time.time() < deadline and is_pid_alive(pid2):
            time.sleep(0.1)
        assert not is_pid_alive(pid2), "ring-kill seam did not exit"
        assert event_count("ring") >= rings_before + 1
        # Earlier rings in this test were typed. This seam's ring must not be.
        seam_id = open(os.environ["MK_BUS_RING_KILL"]).read().strip()
        con = mod.open_project(data, data_dir=data)
        try:
            typed_this = con.execute(
                "SELECT COUNT(*) c FROM events WHERE kind='ring-typed' AND reply_to=?",
                (seam_id,),
            ).fetchone()["c"]
        finally:
            con.close()
        assert typed_this == 0, typed_this
        assert inbox_count() == before4, (inbox_count(), before4)
        del os.environ["MK_BUS_RING_KILL"]
        pid2, port2 = spawn(data, "r1")
        time.sleep(0.5)
        assert inbox_count() == before4 + 1, open(fake + ".rang").read()
        assert event_count("ring-typed") >= 1
        kill(pid2)
        pid2, port2 = spawn(data, "r1")
        time.sleep(0.5)
        assert inbox_count() == before4 + 1, open(fake + ".rang").read()

        # Steer kinds: a working claude rings at once, a working opencode stays queued, an urgent
        # message to a working opencode sends Esc and then rings, an empty steer list queues claude.
        def rang_lines():
            return open(fake + ".rang").read().splitlines() if os.path.isfile(fake + ".rang") else []

        def wait_inbox(n, secs=3.0):
            end = time.time() + secs
            while time.time() < end and inbox_count() < n:
                time.sleep(0.05)
            return inbox_count()

        def nudge(text, **extra):
            return post(port2, remint(), dict({"run": "r1", "task": "T1", "kind": "nudge", "text": text,
                                               "from": "orch", "to": "T1", "project": "proj"}, **extra))

        write_status("idle")  # drain whatever earlier cases left queued
        time.sleep(1.0)
        with open(fake + ".kind", "w") as kf:
            kf.write("claude")
        write_status("working")
        n0 = inbox_count()
        assert nudge("steer claude")[0] == 200
        assert wait_inbox(n0 + 1) == n0 + 1, rang_lines()
        assert not any(l.endswith(" esc") for l in rang_lines()), rang_lines()
        with open(fake + ".kind", "w") as kf:
            kf.write("opencode")
        assert nudge("queue opencode")[0] == 200
        time.sleep(1.0)
        assert inbox_count() == n0 + 1, rang_lines()
        code_u, bad_u = nudge("bad urgent", urgent="yes")
        assert code_u == 400 and "urgent" in bad_u.get("error", ""), (code_u, bad_u)
        n_lines = len(rang_lines())
        assert nudge("urgent opencode", urgent=True)[0] == 200
        assert wait_inbox(n0 + 2) == n0 + 2, rang_lines()
        new = rang_lines()[n_lines:]
        assert new[0] == "T1 esc" and "inbox" in new[1], new
        write_status("idle")  # the queued non-urgent message rings now
        assert wait_inbox(n0 + 3) == n0 + 3, rang_lines()
        kill(pid2)
        os.environ["MK_BUS_STEER_KINDS"] = ""
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(home, "ring-queue.json"))
        pid2, port2 = spawn(data, "r1")
        with open(fake + ".kind", "w") as kf:
            kf.write("claude")
        write_status("working")
        n1 = inbox_count()
        assert nudge("claude, steer none")[0] == 200
        time.sleep(1.0)
        assert inbox_count() == n1, rang_lines()
        write_status("idle")
        assert wait_inbox(n1 + 1) == n1 + 1, rang_lines()
        kill(pid2)
        del os.environ["MK_BUS_STEER_KINDS"]
        os.remove(fake + ".kind")
        pid2, port2 = spawn(data, "r1")

        # FIX 4b: an explicit empty project slug is 403 and changes nothing.
        st = mod.open_project(data, data_dir=data)
        try:
            status_before = st.execute("SELECT status FROM tasks WHERE run=? AND id=?", ("r1", "T1")).fetchone()["status"]
        finally:
            st.close()
        btok = remint()
        code, empty = post(port2, btok, {"run": "r1", "task": "T1", "kind": "nudge", "text": "no project", "from": "orch", "to": "T1", "project": ""})
        assert code == 403, (code, empty)
        st = mod.open_project(data, data_dir=data)
        try:
            status_after = st.execute("SELECT status FROM tasks WHERE run=? AND id=?", ("r1", "T1")).fetchone()["status"]
        finally:
            st.close()
        assert status_after == status_before, (status_before, status_after)

        # FIX 4c: token file is written atomically, mode 0600, with token/run/task.
        lane_new = os.path.join(tmp, "lane-fix4c")
        os.makedirs(lane_new, exist_ok=True)
        rc_tok = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "token", "--lane", lane_new, "--task", "T2", "--run", "r1", "--data", data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert rc_tok.returncode == 0, rc_tok.stderr
        tok_file = lane_token_path(lane_new)
        assert (os.stat(tok_file).st_mode & 0o777) == 0o600, oct(os.stat(tok_file).st_mode)
        payload = json.loads(open(tok_file).read())
        assert set(("token", "run", "task")).issubset(payload) and payload["run"] == "r1" and payload["task"] == "T2", payload

        # A symlink lane and its real directory hash to the same token file.
        lane_real = os.path.join(tmp, "lane-real")
        lane_link = os.path.join(tmp, "lane-link")
        os.makedirs(lane_real, exist_ok=True)
        os.symlink(lane_real, lane_link)
        rc_link = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "token", "--lane", lane_link, "--task", "T1", "--run", "r1", "--data", data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert rc_link.returncode == 0, rc_link.stderr
        link_file = lane_token_path(lane_link)
        assert os.path.isfile(link_file) and os.path.realpath(lane_link) == os.path.realpath(lane_real)
        rc_rev = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "revoke", "--lane", lane_link, "--data", data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert rc_rev.returncode == 0, rc_rev.stderr
        assert not os.path.exists(link_file), link_file

        os.environ.pop("MK_BUS_HERDR", None)
        os.environ.pop("MK_BUS_RING_INTERVAL_SEC", None)
        os.environ.pop("MK_BUS_RING_KILL", None)

        # ============ E2 (1) token carries data + round ============
        lane_e2 = os.path.join(tmp, "lane-e2")
        os.makedirs(lane_e2, exist_ok=True)
        rc_tok = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "token",
             "--lane", lane_e2, "--task", "T9", "--run", "r1", "--round", "2",
             "--data", data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert rc_tok.returncode == 0, rc_tok.stderr
        e2_payload = json.loads(open(lane_token_path(lane_e2)).read())
        # todo 71: a fresh lane finds the store with no MK_BUS_DATA in its env.
        # Both sides compared as realpath: on macOS /var/folders is a symlink to
        # /private/var/folders, so the strings need not be byte-identical.
        assert e2_payload.get("data") and os.path.realpath(e2_payload["data"]) == os.path.realpath(data), e2_payload
        assert store_mode(e2_payload["data"]), e2_payload
        assert e2_payload.get("round") == 2, e2_payload
        # Only ADDED: the pre-existing fields and their values are unchanged.
        assert e2_payload.get("v") == 1 and e2_payload.get("run") == "r1", e2_payload
        assert e2_payload.get("task") == "T9" and e2_payload.get("token"), e2_payload
        assert (os.stat(lane_token_path(lane_e2)).st_mode & 0o777) == 0o600

        # ============ E2 (2) answer + tail read the project store ============
        # todo 68a: these two read mk.db, not runs/<run>/bus.jsonl, which does
        # not exist for a store run.
        assert store_mode(data), "store-mode fixtures expected"
        assert not os.path.exists(os.path.join(data, "runs", "r1", "bus.jsonl")), \
            "fixture must be store-only: a bus.jsonl here would mask the fix"
        rc_ask = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "send",
             "--run", "r1", "--task", "T1", "--kind", "ask",
             "--text", "store mode ask", "--from", "T1", "--to", "orch", "--data", data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert rc_ask.returncode == 0, (rc_ask.stdout, rc_ask.stderr)
        store_ask_id = json.loads(rc_ask.stdout.strip().splitlines()[-1])["message"]["id"]

        rc_ans = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "answer", store_ask_id,
             "answered from the store", "--run", "r1", "--data", data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert rc_ans.returncode == 0, (
            f"answer {store_ask_id} failed in store mode: {rc_ans.stdout}{rc_ans.stderr}"
        )
        ans_res = json.loads(rc_ans.stdout.strip().splitlines()[-1])
        # cmd_answer echoes the server's message record; its reply_to must be
        # the ask, which is what proves the store lookup found the right ask.
        assert ans_res["ok"] is True, ans_res
        assert ans_res["message"]["reply_to"] == store_ask_id, (ans_res, store_ask_id)
        assert ans_res["message"]["kind"] == "answer", ans_res

        rc_tail = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "tail", "--run", "r1",
             "--n", "100", "--data", data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert rc_tail.returncode == 0, rc_tail.stderr
        tail_res = json.loads(rc_tail.stdout.strip().splitlines()[-1])
        tail_texts = [m.get("text") for m in tail_res["messages"]]
        assert "store mode ask" in tail_texts, tail_res
        assert "answered from the store" in tail_texts, tail_res

        # ============ L0 (3): the project DB path is resolved ONLY through mkpaths ============
        # R13 proof: patch the loaded mkpaths module's project_db so the project's store
        # answers to a different directory, then show that both the server-side
        # store_for and the CLI-side _known_runs follow it. On the pre-L0 code each read
        # the root projects table (or joined <data>/mk.db) itself, so the patch was
        # ignored and this block fails.
        proof = os.path.join(tmp, "l0-proof")
        proj_dir = os.path.join(proof, "proj")
        else_dir = os.path.join(proof, "elsewhere")
        os.makedirs(proj_dir, exist_ok=True)
        os.makedirs(else_dir, exist_ok=True)
        # an empty project store in `<proj>`: store mode on, no run rows, no runs/ dir
        _c = mod.open_project(proj_dir, data_dir=proj_dir)
        _c.close()
        # the store `<proj>` is patched to: a real store holding run rL0
        mod.import_history(else_dir, None, "rL0", run_row={"id": "rL0", "status": "active"},
                           data_dir=else_dir)

        real_project_db, real = _load_mkpaths().project_db, os.path.realpath(proj_dir)

        def patched_project_db(slug=None, data=None):
            if slug == "proj" or (data and os.path.realpath(data) == real):
                return _load_mkpaths().db_in(else_dir)
            return real_project_db(slug=slug, data=data)

        mkpaths = _load_mkpaths()
        mkpaths.project_db = patched_project_db
        try:
            server = BusState(proj_dir, "r1", bus_dir=home)
            assert server.use_store, "the proof needs a store-mode project dir"
            adapter, named = server.store_for("proj")
            assert named == else_dir, (named, else_dir)
            # the patched store is the one opened: its runs/, read through the adapter
            assert adapter.store_select("SELECT id FROM runs") == [{"id": "rL0"}], adapter.data_dir
            # _known_runs reads the store the patch names, not <data>/mk.db
            assert _known_runs(proj_dir) == ["rL0"], _known_runs(proj_dir)
            # a slug with no root row still has no store -> "unknown project"
            try:
                server.store_for("no-such-project")
                raise AssertionError("a slug with no root row must be an unknown project")
            except Fail as e:
                assert str(e) == "unknown project", str(e)
        finally:
            _load_mkpaths().project_db = real_project_db
        # unpatched, both read the project's own store again: no run rows there, and the
        # registered project's store is the host one (realpath: tmp is under /var).
        server = BusState(proj_dir, "r1", bus_dir=home)
        assert _known_runs(proj_dir) == [], _known_runs(proj_dir)
        assert server.store_for("proj")[1] == os.path.realpath(data), server.store_for("proj")[1]

        # ============ E2 (6) /send without run is 400 (m-16) ============
        btok = remint()
        code, norun = post(port2, btok, {"task": "T1", "kind": "nudge", "text": "no run", "project": "proj"})
        assert code == 400, (code, norun)
        assert "run" in (norun.get("error") or ""), norun

        # ============ E2 (5) one ack row per (run, msg, lane) ============
        # todo F7 / C13e: a second ack of the same message by the same lane
        # records nothing (ask + inbox + progress all acked one answer).
        code, ask_e2 = post(port2, btok, {
            "run": "r1", "task": "T1", "kind": "ask", "text": "ack once",
            "from": "orch", "to": "T1", "project": "proj",
        })
        assert code == 200, (code, ask_e2)
        ack_target = ask_e2["message"]["id"]
        acks_db = mod.open_project(data, data_dir=data)
        try:
            base_rows = acks_db.execute(
                "SELECT COUNT(*) c FROM events WHERE kind='ack' AND reply_to=?", (ack_target,)
            ).fetchone()["c"]
        finally:
            acks_db.close()
        assert base_rows == 0, base_rows
        for _ in range(2):
            code, acked = post(port2, btok, {
                "run": "r1", "task": "T1", "kind": "ack", "text": "",
                "from": "orch", "to": "T1", "reply_to": ack_target, "ack_level": 3,
            })
            assert code == 200, (code, acked)
        acks_db = mod.open_project(data, data_dir=data)
        try:
            after_rows = acks_db.execute(
                "SELECT COUNT(*) c FROM events WHERE kind='ack' AND reply_to=?", (ack_target,)
            ).fetchone()["c"]
            ack_table = acks_db.execute(
                "SELECT COUNT(*) c FROM acks WHERE run=? AND msg=? AND lane=?",
                ("r1", ack_target, "orch"),
            ).fetchone()["c"]
        finally:
            acks_db.close()
        assert after_rows == 1, (after_rows, "a second ack must not add a row")
        assert ack_table == 1, ack_table

        # ============ E2 (3) real herdr on PATH, no $MK_BUS_HERDR ============
        # The adapter calls the real `herdr agent list` / `herdr agent prompt`.
        # herdr is the ONLY mock: a program on PATH that records its calls.
        assert "MK_BUS_HERDR" not in os.environ, "real-adapter cases need the seam unset"
        bindir = os.path.join(tmp, "realherdr-bin")
        os.makedirs(bindir, exist_ok=True)
        herdr_bin = os.path.join(bindir, "herdr")
        calls_log = os.path.join(tmp, "herdr-calls.log")
        agents_json = os.path.join(tmp, "herdr-agents.json")
        # Fixture captured from the real command's shape: `herdr agent list`
        # prints {"result": {"agents": [...]}}, the same shape herdr_agents()
        # parses (see mk-lane-nudge.py).
        with open(agents_json, "w") as fh:
            json.dump({"result": {"agents": [
                {"pane_id": "wE:p1", "agent_status": "idle",
                 "name": "other", "cwd": os.path.join(tmp, "elsewhere")},
                {"pane_id": "wE:p2", "agent_status": "idle",
                 "name": "e2-t9-build-r1", "cwd": lane_e2},
            ]}}, fh)
        with open(herdr_bin, "w") as fh:
            fh.write("#!" + sys.executable + "\n")
            fh.write("import json, os, sys\n")
            fh.write("log = os.environ['MK_BUS_E2_HERDR_LOG']\n")
            fh.write("cmd = ' '.join(sys.argv[1:3])\n")
            fh.write("import time\n")
            fh.write("with open(log, 'a') as f:\n")
            fh.write("    f.write(cmd + ' ' + ' '.join(sys.argv[3:]) + chr(10))\n")
            fh.write("if cmd == 'agent list':\n")
            fh.write("    if os.path.exists(log + '.sleep'):\n")
            fh.write("        time.sleep(float(open(log + '.sleep').read()))\n")
            fh.write("    sys.stdout.write(open(os.environ['MK_BUS_E2_HERDR_JSON']).read())\n")
            fh.write("    sys.exit(0)\n")
            fh.write("sys.exit(1 if os.path.exists(log + '.promptfail') else 0)\n")
        os.chmod(herdr_bin, 0o755)
        os.environ["MK_BUS_E2_HERDR_LOG"] = calls_log
        os.environ["MK_BUS_E2_HERDR_JSON"] = agents_json
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = bindir + os.pathsep + old_path

        def herdr_calls():
            if not os.path.isfile(calls_log):
                return []
            return [ln for ln in open(calls_log).read().splitlines() if ln.strip()]

        def prompt_calls():
            return [c for c in herdr_calls() if c.startswith("agent prompt")]

        # The lane token for lane_e2 is what maps task T1 to the worktree, and
        # the worktree cwd is what finds pane wE:p2.
        os.environ["MK_BUS_RING_INTERVAL_SEC"] = "0.2"
        kill(pid2)
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(home, "ring-queue.json"))  # rows from the no-herdr phases above
        pid2, port2 = spawn(data, "r1")
        btok = remint()
        os.environ.pop("MK_BUS_RING_INTERVAL_SEC", None)
        assert shutil.which("herdr") == herdr_bin, shutil.which("herdr")
        code, ring_e2 = post(port2, btok, {
            "run": "r1", "task": "T9", "kind": "nudge", "text": "ring me",
            "from": "orch", "to": "T9", "project": "proj",
        })
        assert code == 200, (code, ring_e2)
        deadline = time.time() + 10.0
        while time.time() < deadline and not prompt_calls():
            time.sleep(0.1)
        rings = prompt_calls()
        assert len(rings) == 1, rings
        assert rings[0].startswith("agent prompt wE:p2 "), rings
        # C13a: the ring line is fixed, never the message body.
        assert "ring me" not in rings[0], rings
        assert "mk-builder.py inbox" in rings[0] and RING_ACT in rings[0], rings

        # A working agent queues: no second prompt while the lane is busy.
        with open(agents_json, "w") as fh:
            json.dump({"result": {"agents": [
                {"pane_id": "wE:p2", "agent_status": "working",
                 "name": "e2-t9-build-r1", "cwd": lane_e2},
            ]}}, fh)
        before_busy = len(prompt_calls())
        code, busy_e2 = post(port2, btok, {
            "run": "r1", "task": "T9", "kind": "nudge", "text": "busy now",
            "from": "orch", "to": "T9", "project": "proj",
        })
        assert code == 200, (code, busy_e2)
        time.sleep(1.0)
        assert len(prompt_calls()) == before_busy, prompt_calls()
        # ... and it goes idle, so the queued ring drains on the next tick.
        with open(agents_json, "w") as fh:
            json.dump({"result": {"agents": [
                {"pane_id": "wE:p2", "agent_status": "idle",
                 "name": "e2-t9-build-r1", "cwd": lane_e2},
            ]}}, fh)
        deadline = time.time() + 15.0
        while time.time() < deadline and len(prompt_calls()) <= before_busy:
            time.sleep(0.2)
        assert len(prompt_calls()) == before_busy + 1, prompt_calls()

        # ============ E2 R3: ring on the worker, claimed once, one pane, no real herdr ============
        def ag(pane, status, name, cwd):
            return {"pane_id": pane, "agent_status": status, "name": name, "cwd": cwd}

        def set_agents(*agents):
            with open(agents_json, "w") as fh:
                json.dump({"result": {"agents": list(agents)}}, fh)

        def list_calls():
            return [c for c in herdr_calls() if c.startswith("agent list")]

        def wait_prompts(n, secs=15.0):
            deadline = time.time() + secs
            while time.time() < deadline and len(prompt_calls()) < n:
                time.sleep(0.1)

        def send_to(text, to="T9"):
            return post(port2, btok, {"run": "r1", "task": "T9", "kind": "nudge", "text": text,
                                      "from": "orch", "to": to, "project": "proj"})

        p2 = ag("wE:p2", "idle", "e2-t9-build-r1", lane_e2)
        sleep_file = calls_log + ".sleep"

        # A blocked agent is not rung (only idle is) and the message is not kept.
        set_agents(dict(p2, agent_status="blocked"))
        n0 = len(prompt_calls())
        assert send_to("blocked lane")[0] == 200
        time.sleep(0.8)
        assert len(prompt_calls()) == n0, prompt_calls()

        # herdr reports a lane whose turn just finished as `done`: it rings, exactly once.
        set_agents(dict(p2, agent_status="done"))
        assert send_to("done lane")[0] == 200
        wait_prompts(n0 + 1)
        time.sleep(0.8)
        assert len(prompt_calls()) == n0 + 1, ("a done lane rings once", prompt_calls()[n0:])

        # (2) A herdr that hangs for 20 s must not hold /send; (1) two concurrent
        # sends plus the ticks type each message exactly once.
        set_agents(p2)
        n0 = len(prompt_calls())
        with open(sleep_file, "w") as fh:
            fh.write("20")
        t0 = time.time()
        code, _slow = send_to("slow herdr")
        assert code == 200, (code, _slow)
        assert time.time() - t0 < 1.0, "/send waited on herdr: %.2fs" % (time.time() - t0)
        os.remove(sleep_file)
        senders = [threading.Thread(target=send_to, args=("concurrent %d" % i,)) for i in range(2)]
        for th in senders:
            th.start()
        for th in senders:
            th.join()
        wait_prompts(n0 + 3)
        time.sleep(1.0)
        assert len(prompt_calls()) == n0 + 3, ("each message typed exactly once", prompt_calls()[n0:])

        # (3) Two panes in the lane cwd: no guess, one ring-skipped note, no prompt.
        p3 = ag("wE:p3", "idle", "rev", lane_e2)
        set_agents(p3, p2)
        n0 = len(prompt_calls())
        skipped0 = event_count("ring-skipped")
        assert send_to("two panes")[0] == 200
        time.sleep(1.0)
        assert len(prompt_calls()) == n0, prompt_calls()[n0:]
        assert event_count("ring-skipped") == skipped0 + 1, event_count("ring-skipped")
        # The pane recorded in the lane's watch file decides; the queued message rings.
        watch_file = os.path.join(lane_e2, ".matrixkoder-watch.json")
        write_json(watch_file, {"pane": "wE:p2"})
        wait_prompts(n0 + 1)
        time.sleep(0.6)
        assert len(prompt_calls()) == n0 + 1, prompt_calls()[n0:]
        assert prompt_calls()[-1].startswith("agent prompt wE:p2 "), prompt_calls()[-1]
        # The armed agent's name beats the recorded pane.
        write_json(watch_file, {"pane": "wE:p2", "agent": "rev"})
        assert send_to("armed name")[0] == 200
        wait_prompts(n0 + 2)
        time.sleep(0.6)
        assert len(prompt_calls()) == n0 + 2, prompt_calls()[n0:]
        assert prompt_calls()[-1].startswith("agent prompt wE:p3 "), prompt_calls()[-1]
        os.remove(watch_file)

        # (4) The same task id in another run owns another pane; it is never rung.
        lane_r2 = os.path.join(tmp, "lane-r2")
        os.makedirs(lane_r2, exist_ok=True)
        other_tok = secrets.token_urlsafe(32)
        tokens_path = os.path.join(home, "bus-tokens.json")
        toks = read_json(tokens_path, default={})
        toks[other_tok] = {"token": other_tok, "role": "lane", "run": "r2", "task": "T9",
                           "lane": lane_r2, "project": "proj", "created_at": now_iso()}
        write_json(tokens_path, toks, mode=0o600)
        set_agents(ag("wE:p9", "idle", "r2-t9", lane_r2), p2)
        n0 = len(prompt_calls())
        assert send_to("same task, other run")[0] == 200
        wait_prompts(n0 + 1)
        time.sleep(0.6)
        assert [c.split(" ")[2] for c in prompt_calls()[n0:]] == ["wE:p2"], prompt_calls()[n0:]

        # (6) Six queued messages for a working lane cost one `agent list` per
        # tick (the old drain spawned one per message per tick), then ring once each.
        set_agents(dict(p2, agent_status="working"))
        l0 = len(list_calls())
        n0 = len(prompt_calls())
        for i in range(6):
            assert send_to("queued %d" % i)[0] == 200
        time.sleep(1.2)
        used = len(list_calls()) - l0
        assert used <= 14, ("list calls for 6 queued messages", used)
        assert len(prompt_calls()) == n0
        set_agents(p2)
        wait_prompts(n0 + 6)
        time.sleep(0.6)
        assert len(prompt_calls()) == n0 + 6, prompt_calls()[n0:]

        # (6) The queue holds 100 rows: the oldest are dropped, one note each.
        set_agents(ag("wE:p8", "working", "T8", "/x/T8"), p2)
        dropped0 = event_count("ring-dropped")
        for i in range(105):
            assert send_to("cap %d" % i, to="T8")[0] == 200
        deadline = time.time() + 15.0
        while time.time() < deadline and event_count("ring-dropped") < dropped0 + 5:
            time.sleep(0.1)
        time.sleep(0.6)
        assert event_count("ring-dropped") == dropped0 + 5, event_count("ring-dropped") - dropped0
        assert len(read_json(os.path.join(home, "ring-queue.json"), default=[])) == 100
        set_agents(p2)  # T8 has no pane any more: the queue is dropped, nothing is typed
        n0 = len(prompt_calls())
        time.sleep(0.8)
        assert len(prompt_calls()) == n0
        assert read_json(os.path.join(home, "ring-queue.json"), default=[]) == []

        # (9) A failing `herdr agent prompt` is ring-failed, retried once, then given up;
        # it is never ring-typed.
        with open(calls_log + ".promptfail", "w") as fh:
            fh.write("1")
        n0, failed0, typed0, ring0 = len(prompt_calls()), event_count("ring-failed"), event_count("ring-typed"), event_count("ring")
        assert send_to("prompt fails")[0] == 200
        time.sleep(1.5)
        os.remove(calls_log + ".promptfail")
        assert len(prompt_calls()) == n0 + 2, prompt_calls()[n0:]
        assert event_count("ring-failed") == failed0 + 2
        assert event_count("ring-typed") == typed0
        assert event_count("ring") == ring0 + 1
        # A pane id that reads as an option is refused before herdr is called.
        assert BusState._type_ring(None, {"run": "r1", "id": "m-0"}, "line", 1, None, "-x") is False

        def esc_calls():
            return [c for c in herdr_calls() if c.startswith("agent send-keys")]

        # (10) `urgent` Escs once per message id: a ring retry of the same message must not
        # interrupt a second turn, while a second id gets its own Esc.
        def esc_once_proof(senders, status="idle"):
            """-> (esc count, ring count, urgent flags queued): each sender's urgent message is
            queued once by the real `_maybe_ring`, then drained twice by the real `_drain_one`
            — the first attempt and the retry the ring queue uses. herdr is the only mock:
            `_send_esc` and `_ring` become counters. Two ids per sender, so a retry is
            distinguishable from a new message."""
            bus = BusState(data, "r1", bus_dir=home)
            bus.project = "proj"
            escs, typed, queued = [], [], []
            bus._send_esc = lambda pane: (escs.append(pane) or True)
            bus._ring = lambda msg, pane: (typed.append(msg["id"]) or True)
            bus._pick_pane = lambda msg, agents: (agents[0], "wE:p2")
            agents = [{"pane_id": "wE:p2", "agent_status": status, "name": "e2-t9-build-r1",
                       "cwd": lane_e2}]
            base = {"run": "r1", "task": "T9", "to": "T9", "kind": "nudge", "urgent": True,
                    "project": "proj", "queued_at": time.time()}
            for i, sender in enumerate(senders):
                for j in (1, 2):  # two ids: a retry is never a second interrupt
                    bus._maybe_ring(dict(base, id=f"m-esc-{i}-{j}", **{"from": sender}))
                    row = bus._ring_queue.pop()   # the row this send queued
                    queued.append(bool(row.get("urgent")))
                    bus._drain_one(row, agents, [])   # the attempt
                    bus._drain_one(row, agents, [])   # its retry, same id
            return len(escs), len(typed), queued

        # (10) Two orchestrator ids on a WORKING lane, each retried once: four drains and two
        # Escs — a ring retry of the same message never interrupts a second turn, while the
        # second id interrupts its own.
        assert esc_once_proof(["orch"], "working") == (2, 4, [True, True]), \
            esc_once_proof(["orch"], "working")

        # (11) `urgent` is honoured only from the orchestrator. On a WORKING lane the lane's
        # two ids are still delivered and still queued, but un-urgent: they wait for idle
        # instead of Escing, so the run Escs exactly twice, for the orchestrator alone.
        assert esc_once_proof(["orch", "T9"], "working") == (2, 4, [True, True, False, False]), \
            esc_once_proof(["orch", "T9"], "working")

        def send_from(sender, text, **extra):
            return post(port2, btok, {"run": "r1", "task": "T9", "kind": "nudge", "text": text,
                                      "from": sender, "to": "T9", "project": "proj",
                                      "urgent": True, **extra})

        # End to end over the real server, both senders on the same working pane.
        set_agents(dict(p2, agent_status="working"))
        n_e, n_a = len(esc_calls()), len(list_calls())
        code, lane_urgent_msg = send_from("T9", "lane urgent")
        assert code == 200 and lane_urgent_msg["message"]["kind"] == "nudge", lane_urgent_msg
        # Count-based: wait until the drain carrying this message has asked herdr for the agent
        # list (one `agent list` more than before), so the ring has made its decision.
        deadline = time.time() + 10.0
        while time.time() < deadline and len(list_calls()) < n_a + 1:
            time.sleep(0.1)
        time.sleep(0.5)
        assert len(esc_calls()) == n_e, ("a non-orch urgent must not Esc", esc_calls()[n_e:])
        # The orchestrator's urgent on the same working lane does Esc.
        assert send_from("orch", "orch urgent")[0] == 200
        deadline = time.time() + 10.0
        while time.time() < deadline and len(esc_calls()) < n_e + 1:
            time.sleep(0.1)
        assert len(esc_calls()) == n_e + 1, esc_calls()[n_e:]
        # The lane's message is still queued (it waits for idle); the orchestrator's already
        # rang. Drain the lane's, so nothing this block sent can ring into a later case's
        # prompt count. Count-based: wait for the ring queue file to empty.
        qp = os.path.join(home, "ring-queue.json")
        set_agents(p2)
        deadline = time.time() + 15.0
        while time.time() < deadline and read_json(qp, default=[]):
            time.sleep(0.1)
        assert read_json(qp, default=[]) == [], "ring queue left dirty"

        # (5) A ring recorded but not typed (bus killed in between) is queued on restart,
        # not typed into an agent that is working now; it rings once the agent is idle.
        kill(pid2)
        os.environ["MK_BUS_RING_KILL"] = os.path.join(tmp, "kill-seam-r3")
        os.environ["MK_BUS_RING_INTERVAL_SEC"] = "0.2"
        pid2, port2 = spawn(data, "r1")
        os.environ.pop("MK_BUS_RING_KILL", None)
        btok = remint()
        set_agents(p2)
        n0 = len(prompt_calls())
        try:
            send_to("die before typing")
        except Exception:
            pass
        deadline = time.time() + 8.0
        while time.time() < deadline and is_pid_alive(pid2):
            time.sleep(0.1)
        assert not is_pid_alive(pid2), "ring-kill seam did not exit"
        assert len(prompt_calls()) == n0, prompt_calls()[n0:]
        set_agents(dict(p2, agent_status="working"))
        pid2, port2 = spawn(data, "r1")
        btok = remint()
        time.sleep(1.0)
        assert len(prompt_calls()) == n0, ("restore typed into a working agent", prompt_calls()[n0:])
        set_agents(p2)
        wait_prompts(n0 + 1)
        time.sleep(0.6)
        assert len(prompt_calls()) == n0 + 1, prompt_calls()[n0:]

        # E2 R4 (1): a message queued for a working lane has no `ring` row yet; it must
        # survive a kill -9 through ring-queue.json and ring exactly once after restart.
        os.environ["MK_BUS_RING_INTERVAL_SEC"] = "0.2"
        kill(pid2)
        pid2, port2 = spawn(data, "r1")
        btok = remint()
        set_agents(dict(p2, agent_status="working"))
        n0 = len(prompt_calls())
        code, queued_kill = send_to("queued across kill -9")
        assert code == 200, (code, queued_kill)
        queued_id = queued_kill["message"]["id"]
        queue_file = os.path.join(home, "ring-queue.json")
        deadline = time.time() + 5.0
        while time.time() < deadline and not any(m.get("id") == queued_id for m in read_json(queue_file, default=[])):
            time.sleep(0.1)
        assert any(m.get("id") == queued_id for m in read_json(queue_file, default=[])), "queue file lacks the row"
        os.kill(pid2, signal.SIGKILL)
        deadline = time.time() + 5.0
        while time.time() < deadline and is_pid_alive(pid2):
            time.sleep(0.05)
        assert not is_pid_alive(pid2), pid2
        pid2, port2 = spawn(data, "r1")
        btok = remint()
        time.sleep(1.0)
        assert len(prompt_calls()) == n0, ("restore typed into a working agent", prompt_calls()[n0:])
        set_agents(p2)
        wait_prompts(n0 + 1)
        time.sleep(0.6)
        assert len(prompt_calls()) == n0 + 1, ("queued row rings once after kill -9", prompt_calls()[n0:])

        # E2 R4 (2): a drain that raises (the append-fail seam hits the ring row) puts the
        # claimed message back; it rings on the next tick, with one non-final ring-failed note.
        set_agents(dict(p2, agent_status="working"))
        n0, failed0, ring0 = len(prompt_calls()), event_count("ring-failed"), event_count("ring")
        assert send_to("drain raises")[0] == 200
        time.sleep(0.6)
        flag = os.path.join(data, "append-fail")
        open(flag, "w").close()
        set_agents(p2)
        wait_prompts(n0 + 1)
        time.sleep(0.6)
        assert not os.path.exists(flag), "the seam was not consumed by the drain"
        assert len(prompt_calls()) == n0 + 1, ("a raised drain is retried once", prompt_calls()[n0:])
        assert event_count("ring-failed") == failed0 + 1, event_count("ring-failed") - failed0
        assert event_count("ring") == ring0 + 1, event_count("ring") - ring0
        os.environ.pop("MK_BUS_RING_INTERVAL_SEC", None)
        # The attempt counter bounds it: attempt 1 re-queues, attempt 2 gives up and keeps the id rung.
        stub = BusState.__new__(BusState)
        stub._ring_lock, stub._ring_notes, stub.rung = threading.Lock(), [], set()
        raised_msg, raised_still = {"id": "m-raised"}, []
        stub._ring_raised(raised_msg, raised_still)
        assert raised_still == [raised_msg] and "m-raised" not in stub.rung, (raised_still, stub.rung)
        assert stub._ring_notes[-1][1:2] == ("ring-failed",) and stub._ring_notes[-1][3] == {"final": False}
        raised_still.clear()
        stub._ring_raised(raised_msg, raised_still)
        assert raised_still == [] and "m-raised" in stub.rung, (raised_still, stub.rung)
        assert stub._ring_notes[-1][3] == {"final": True}, stub._ring_notes[-1]

        # E2 R4 (4b): the pane recorded in the watch file counts only if that agent's cwd is the lane worktree.
        lane_w = os.path.join(tmp, "lane-w")
        os.makedirs(lane_w, exist_ok=True)
        write_json(os.path.join(lane_w, ".matrixkoder-watch.json"), {"pane": "wE:p6"})
        stub.data_dir = home
        lanes_w = {os.path.realpath(lane_w)}
        tier_w = [ag("wE:p5", "idle", "a", lane_w), ag("wE:p6", "idle", "b", os.path.join(tmp, "elsewhere"))]
        assert stub._disambiguate(tier_w, lanes_w) == "ambiguous", "a reused pane id in another cwd was accepted"
        tier_w[1]["cwd"] = lane_w
        assert stub._disambiguate(tier_w, lanes_w)[1] == "wE:p6"

        # herdr missing entirely: no ring, no crash, the bus still serves.
        os.environ["PATH"] = os.path.join(tmp, "emptybin")
        os.makedirs(os.path.join(tmp, "emptybin"), exist_ok=True)
        assert shutil.which("herdr") is None
        code, noherdr = post(port2, btok, {
            "run": "r1", "task": "T9", "kind": "nudge", "text": "no herdr",
            "from": "orch", "to": "T9", "project": "proj",
        })
        assert code == 200, (code, noherdr)
        time.sleep(0.5)
        code, still_ok = post(port2, btok, {
            "run": "r1", "task": "T9", "kind": "info", "text": "still alive",
            "from": "orch", "to": "T9", "project": "proj",
        })
        assert code == 200, (code, still_ok)
        os.environ["PATH"] = old_path

        # ============ E2 (4) watch: live, then across kill -9 + restart ============
        kill(pid2)
        pid2, port2 = spawn(data, "r1")
        orch_w = mint_orch(data)
        # The store already holds history (earlier blocks): watch replays from
        # --after, so a baseline message fixes the point watch starts after and
        # the test counts only the messages sent below.
        code, base_w = post(port2, orch_w, {
            "run": "r1", "task": "T9", "kind": "info", "text": "watch baseline",
            "from": "orch", "to": "T9",
        })
        assert code == 200, (code, base_w)
        base_id = base_w["message"]["id"]
        watch = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "watch",
             "--run", "r1", "--data", data, "--max-messages", "6",
             "--after", base_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        pids.append(watch.pid)
        watch_lines = []

        def read_watch(n, secs=20.0):
            deadline = time.time() + secs
            while time.time() < deadline and len(watch_lines) < n:
                ln = watch.stdout.readline()
                if not ln:
                    break
                if ln.strip():
                    watch_lines.append(ln.strip())

        try:
            for i in range(3):
                code, wmsg = post(port2, orch_w, {
                    "run": "r1", "task": "T9", "kind": "info",
                    "text": f"watch message {i}", "from": "orch", "to": "T9",
                })
                assert code == 200, (code, wmsg)
            read_watch(3)
            assert len(watch_lines) == 3, watch_lines
            for i, ln in enumerate(watch_lines):
                assert ln.startswith("MSG m-"), ln
                assert " r1 T9 info orch->T9: " in ln, ln
                assert f"watch message {i}" in ln, ln
            assert "watch baseline" not in "".join(watch_lines), watch_lines
            first_ids = [ln.split()[1] for ln in watch_lines]

            # kill -9 the bus (no clean shutdown), restart ON THE SAME PORT.
            # The SAME watch process must resume with Last-Event-ID: no replay
            # of the first 3, and all 3 new messages arrive.
            os.kill(pid2, signal.SIGKILL)
            deadline = time.time() + 5.0
            while time.time() < deadline and is_pid_alive(pid2):
                time.sleep(0.05)
            assert not is_pid_alive(pid2), pid2
            assert watch.poll() is None, "watch must outlive the bus"
            pid_old, port_old = pid2, port2
            pid2, port2 = spawn_prefer(data, "r1", port_old)
            assert pid2 != pid_old and port2 == port_old, (pid2, port2, pid_old, port_old)
            orch_w = mint_orch(data)
            for i in range(3):
                code, w2 = post(port2, orch_w, {
                    "run": "r1", "task": "T9", "kind": "info",
                    "text": f"after restart {i}", "from": "orch", "to": "T9",
                })
                assert code == 200, (code, w2)
            read_watch(6)
            assert len(watch_lines) == 6, watch_lines
            assert watch.wait(timeout=10) == 0, watch.returncode
            for i, ln in enumerate(watch_lines[3:]):
                assert f"after restart {i}" in ln, ln
            ids = [ln.split()[1] for ln in watch_lines]
            assert len(set(ids)) == 6, ("a restart must not replay", ids)
        finally:
            if watch.poll() is None:
                stop_process(watch.pid)
                watch.wait(timeout=10)

        # Decision 1 (L10.5): watch keeps a per-(run, task) cursor in the project DB's watch_cursors
        # table, never in a file. The watch above (--after, run r1, no task) printed 6 lines, so the
        # cursor holds "r1|*".
        cur_path = os.path.join(data, os.path.basename(_paths("target_watch_cursors", "x")))
        assert not os.path.exists(cur_path), "a store-mode watch wrote watch-cursors.json"
        cur_seq = cursor_row(data, "r1|*")
        assert cur_seq == int(ids[-1][2:]), (cur_seq, ids[-1])
        cur_new = []
        for i in range(2):
            code, cm = post(port2, orch_w, {"run": "r1", "task": "T9", "kind": "info",
                                            "text": f"cursor message {i}", "from": "orch", "to": "T9"})
            assert code == 200, (code, cm)
            cur_new.append(cm["message"]["id"])
        watch_cmd = [sys.executable, os.path.abspath(__file__), "watch", "--run", "r1", "--data", data]
        # No --after: resumes after the cursor, so only the two new messages, no history replay.
        res_cur = subprocess.run(watch_cmd + ["--max-messages", "2"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, timeout=20)
        assert res_cur.returncode == 0, (res_cur.returncode, res_cur.stderr)
        assert [ln.split()[1] for ln in res_cur.stdout.splitlines()] == cur_new, res_cur.stdout
        assert cursor_row(data, "r1|*") == int(cur_new[-1][2:]), cursor_row(data, "r1|*")
        assert not os.path.exists(cur_path)
        # A different filter has its own key: a task watch with no cursor replays from the start.
        res_tk = subprocess.run(watch_cmd + ["--task", "T9", "--max-messages", "1"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=20)
        assert res_tk.returncode == 0, (res_tk.returncode, res_tk.stderr)
        assert res_tk.stdout.split()[1] not in cur_new, res_tk.stdout
        assert cursor_row(data, "r1|T9") is not None
        # --replay ignores the cursor and starts from 0: the oldest message, not a new one.
        res_rp = subprocess.run(watch_cmd + ["--replay", "--max-messages", "1"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=20)
        assert res_rp.returncode == 0, (res_rp.returncode, res_rp.stderr)
        assert int(res_rp.stdout.split()[1][2:]) < int(base_id[2:]), (res_rp.stdout, base_id)
        # An explicit --after wins over the cursor (here: replay the second new message only).
        res_af = subprocess.run(watch_cmd + ["--after", cur_new[0], "--max-messages", "1"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=20)
        assert res_af.stdout.split()[1] == cur_new[1], res_af.stdout
        # A cursor row at seq 0 is no cursor: replay, never a crash.
        for junk in (0,):
            cursor_put(data, "r1|*", junk)
            res_junk = subprocess.run(watch_cmd + ["--max-messages", "1"], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, timeout=20)
            assert res_junk.returncode == 0, (junk, res_junk.returncode, res_junk.stderr)
            assert int(res_junk.stdout.split()[1][2:]) < int(base_id[2:]), (junk, res_junk.stdout)

        # A multi-line body collapses to ONE line, cut to 200 chars (C13a).
        noisy = "first line\nsecond\tline " + ("y" * 400)
        code, noised = post(port2, orch_w, {
            "run": "r1", "task": "T9", "kind": "info", "text": noisy,
            "from": "orch", "to": "T9",
        })
        assert code == 200, (code, noised)
        noisy_line = _watch_line(noised["message"])
        assert "\n" not in noisy_line, repr(noisy_line)
        assert len(noisy_line.split(": ", 1)[1]) <= 200, len(noisy_line)
        assert noisy_line.endswith("y"), noisy_line[-40:]

        # E2 R3: _watch_line drops control characters and prints "-" for a null task.
        ctl_line = _watch_line({"id": "m-1", "run": "r1", "task": None, "kind": "info",
                                "from": "a", "to": "b", "text": "x\x1b[31mred\x00\x07 end"})
        assert not re.search(r"[\x00-\x1f\x7f]", ctl_line), repr(ctl_line)
        assert ctl_line.startswith("MSG m-1 r1 - info a->b: "), ctl_line

        # E2 R3: store reads are real read-only opens. A write is refused, a missing
        # store is empty, and a store that cannot be read is an error, not [].
        ro = StoreAdapter(data)
        assert ro.store_select("SELECT id FROM runs ORDER BY id")[0]["id"] == "r1"
        try:
            ro.store_select("CREATE TABLE ro_must_fail(a)")
            raise AssertionError("store_select wrote to the store")
        except Fail:
            pass
        empty_dir = os.path.join(tmp, "no-store")
        os.makedirs(empty_dir)
        assert StoreAdapter(empty_dir).store_select("SELECT 1") == []
        assert not os.path.exists(_load_mkpaths().db_in(empty_dir))
        broken_dir = os.path.join(tmp, "broken-store")
        os.makedirs(broken_dir)
        with open(_load_mkpaths().db_in(broken_dir), "w") as fh:
            fh.write("this is not a database")
        try:
            StoreAdapter(broken_dir).store_select("SELECT 1")
            raise AssertionError("an unreadable store returned rows")
        except Fail:
            pass
        # answer/tail run lookup: runs/<x> directories no longer hide the store's runs.
        os.makedirs(os.path.join(data, "runs", "rdisk"), exist_ok=True)
        assert {"rdisk", "r1"} <= set(_known_runs(data)), _known_runs(data)

        # E2 R3 (7): a bus started WITHOUT --run; `watch` without --run streams every run.
        mod.import_history(data, None, "r2", run_row={"id": "r2", "status": "active"}, data_dir=data)
        con = mod.open_project(data, data_dir=data)
        try:
            mod.add_task(con, "r2", "T9", "task", title="second run")
        finally:
            con.close()
        kill(pid2)
        pid2, port2 = spawn(data, None)
        orch_nr = mint_orch(data)
        code, base_nr = post(port2, orch_nr, {"run": "r1", "task": "T9", "kind": "info",
                                              "text": "no-run baseline", "from": "orch", "to": "T9"})
        assert code == 200, (code, base_nr)
        watch_nr = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "watch", "--data", data,
             "--task", "T9", "--max-messages", "2", "--after", base_nr["message"]["id"]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        pids.append(watch_nr.pid)
        try:
            for run_nr in ("r1", "r2"):
                code, sent_nr = post(port2, orch_nr, {"run": run_nr, "task": "T9", "kind": "info",
                                                      "text": "from " + run_nr, "from": "orch", "to": "T9"})
                assert code == 200, (code, sent_nr)
            out_nr, err_nr = watch_nr.communicate(timeout=20)
        finally:
            if watch_nr.poll() is None:
                stop_process(watch_nr.pid)
                watch_nr.wait(timeout=10)
        assert watch_nr.returncode == 0, (watch_nr.returncode, err_nr)
        runs_nr = sorted(ln.split()[2] for ln in out_nr.splitlines())
        assert runs_nr == ["r1", "r2"], out_nr

        # E2 R3 (7): a 4xx from /events is not "bus down": one error line and exit 5, at once.
        from http.server import HTTPServer

        class Refuse(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"ok": False, "error": "invalid run id"}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        refuser = HTTPServer(("127.0.0.1", 0), Refuse)
        threading.Thread(target=refuser.serve_forever, daemon=True).start()
        fake_home = os.path.join(tmp, "fake-bus-home")
        os.makedirs(fake_home)
        write_json(os.path.join(fake_home, "bus.json"), {"port": refuser.server_address[1], "token": "t" * 20}, mode=0o600)
        t0 = time.time()
        rc_4xx = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "watch", "--data", data],
            env=dict(os.environ, MK_BUS_DIR=fake_home),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
        )
        refuser.shutdown()
        assert rc_4xx.returncode == 5, (rc_4xx.returncode, rc_4xx.stderr)
        assert rc_4xx.stderr.strip() == "HTTP 400: invalid run id", rc_4xx.stderr
        assert rc_4xx.stdout == "" and time.time() - t0 < 10.0

        # E2 R4 (4c): a 401 is a 4xx too: the real bus refuses a stale token, watch exits 5 at once.
        stale_home = os.path.join(tmp, "stale-bus-home")
        os.makedirs(stale_home)
        write_json(os.path.join(stale_home, "bus.json"), {"port": port2, "token": "stale-" + "t" * 20}, mode=0o600)
        t0 = time.time()
        rc_401 = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "watch", "--data", data],
            env=dict(os.environ, MK_BUS_DIR=stale_home),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
        )
        assert rc_401.returncode == 5, (rc_401.returncode, rc_401.stderr)
        assert rc_401.stderr.strip().startswith("HTTP 401: "), rc_401.stderr
        assert rc_401.stdout == "" and time.time() - t0 < 10.0

        # E2 R4 (4c): `run=*` stays inside the token's project. projb (made above) holds
        # its own run r1; the same run name in two projects is kept apart.
        tok_b = secrets.token_urlsafe(32)
        tokens_path_b = os.path.join(home, "bus-tokens.json")
        toks_b = read_json(tokens_path_b, default={})
        toks_b[tok_b] = {"token": tok_b, "role": "orch", "run": "r1", "task": "", "lane": "",
                         "project": "projb", "created_at": now_iso()}
        write_json(tokens_path_b, toks_b, mode=0o600)
        assert post(port2, tok_b, {"run": "r1", "task": "T1", "kind": "info", "text": "star probe projb",
                                   "from": "orch", "to": "T1", "project": "projb"})[0] == 200
        assert post(port2, orch_nr, {"run": "r1", "task": "T9", "kind": "info", "text": "star probe proj",
                                     "from": "orch", "to": "T9"})[0] == 200

        def star_texts(token):
            """Text of every message `run=*` replays for this token (read until the stream idles)."""
            req = urllib.request.Request(
                f"http://127.0.0.1:{port2}/events?lane=*&run=*",
                headers={"Authorization": f"Bearer {token}", "Last-Event-ID": "m-0",
                         "Host": f"127.0.0.1:{port2}"},
            )
            seen = []
            try:
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    while True:
                        ln = resp.readline()
                        if not ln:
                            break
                        if ln.startswith(b"data:"):
                            seen.append(json.loads(ln[5:].decode("utf-8")).get("text"))
            except (TimeoutError, socket.timeout, urllib.error.URLError):
                pass
            return seen

        texts_a, texts_b = star_texts(orch_nr), star_texts(tok_b)
        assert "star probe proj" in texts_a and "star probe projb" not in texts_a, texts_a[-5:]
        assert "star probe projb" in texts_b and "star probe proj" not in texts_b, texts_b[-5:]

        # E2 R4 (3): a FILE-mode bus (no mk.db) started with --run: `run=*` is a 400 there, and
        # `watch` without --run follows the bus's own run.
        fm_home = os.path.join(tmp, "fm-home")
        fm_data = os.path.join(tmp, "fm-data")
        os.makedirs(fm_home)
        os.makedirs(fm_data)
        assert not store_mode(fm_data)
        saved_bus_dir = os.environ["MK_BUS_DIR"]
        os.environ["MK_BUS_DIR"] = fm_home
        pid_fm = None
        try:
            pid_fm, port_fm = spawn(fm_data, "r1")
            tok_fm = read_json(os.path.join(fm_home, "bus.json"))["token"]
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port_fm}/events?lane=*&run=*",
                    headers={"Authorization": f"Bearer {tok_fm}", "Host": f"127.0.0.1:{port_fm}"}), timeout=5.0)
                raise AssertionError("file-mode run=* was served")
            except urllib.error.HTTPError as e:
                assert e.code == 400 and b"project store" in e.read(), e.code
            watch_fm = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "watch", "--data", fm_data, "--max-messages", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
            pids.append(watch_fm.pid)
            try:
                code, sent_fm = post(port_fm, tok_fm, {"run": "r1", "task": "T1", "kind": "info",
                                                       "text": "file mode", "from": "orch", "to": "T1"})
                assert code == 200, (code, sent_fm)
                out_fm, err_fm = watch_fm.communicate(timeout=20)
            finally:
                if watch_fm.poll() is None:
                    stop_process(watch_fm.pid)
                    watch_fm.wait(timeout=10)
            assert watch_fm.returncode == 0, (watch_fm.returncode, err_fm)
            assert " r1 T1 info orch->T1: file mode" in out_fm, out_fm
            # File-mode cursor: valid only for the bus start stamp it was written under. A new run in
            # the same data dir restarts seq from 1 and restarts the bus, so a stale cursor must replay.
            fm_cur = _load_mkpaths().legacy_cursors(fm_data)
            fm_started = read_json(os.path.join(fm_home, "bus.json"))["started"]
            assert read_json(fm_cur)["*|*"] == {"seq": 1, "started": fm_started}, read_json(fm_cur)
            fm_watch = [sys.executable, os.path.abspath(__file__), "watch", "--data", fm_data, "--max-messages", "1"]

            def fm_first():
                r = subprocess.run(fm_watch, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
                assert r.returncode == 0, (r.returncode, r.stderr)
                return r.stdout.split()[1]
            assert post(port_fm, tok_fm, {"run": "r1", "task": "T1", "kind": "info", "text": "file mode 2",
                                          "from": "orch", "to": "T1"})[0] == 200
            assert fm_first() == "m-2"  # same bus start stamp: resumes after the cursor
            for stale in ({"seq": 50, "started": fm_started - 1000.0}, 50, {"seq": 50, "started": None}):
                write_json(fm_cur, {"*|*": stale})
                assert fm_first() == "m-1", stale  # another bus start, or no stamp: replay, no lost message
        finally:
            if pid_fm:
                kill(pid_fm)
            os.environ["MK_BUS_DIR"] = saved_bus_dir

        os.environ.pop("MK_BUS_E2_HERDR_LOG", None)
        os.environ.pop("MK_BUS_E2_HERDR_JSON", None)

        rc_m = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "metrics", "--run", "r1", "--data", data],
            stdout=subprocess.PIPE, text=True,
        )
        assert rc_m.returncode == 0, rc_m.stdout
        metrics = json.loads(rc_m.stdout.strip().splitlines()[-1])
        assert metrics["ok"] is True and "latency_p50" in metrics and "obedience" in metrics and "false_stall" in metrics, metrics
        # metrics without --run: every active run, each equal to its --run output; the
        # permanent bugs run is no active run (bugs-tab)
        con = mod.open_project(data, data_dir=data)
        try:
            con.execute("INSERT INTO runs(id, plan, branch, status, started)"
                        " VALUES ('bugs', NULL, 'main', 'active', '2026-01-01T00:00:00+00:00')")
            con.commit()
        finally:
            con.close()
        rc_ma = subprocess.run([sys.executable, os.path.abspath(__file__), "metrics", "--data", data],
                               stdout=subprocess.PIPE, text=True)
        assert rc_ma.returncode == 0, rc_ma.stdout
        all_m = json.loads(rc_ma.stdout.strip().splitlines()[-1])
        assert all_m["ok"] is True and [m["run"] for m in all_m["runs"]] == ["r1", "r2"], all_m
        assert all_m["runs"][0] == metrics, (all_m["runs"][0], metrics)
        rc_m2 = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "metrics", "--run", "r2", "--data", data],
            stdout=subprocess.PIPE, text=True,
        )
        assert all_m["runs"][1] == json.loads(rc_m2.stdout.strip().splitlines()[-1]), all_m["runs"][1]

        # item 8: file mode (no mk.db) degrades to counts from bus.jsonl instead of erroring
        fdata = os.path.join(tmp, "fm-metrics")
        os.makedirs(os.path.join(fdata, "runs", "rf"))
        with open(os.path.join(fdata, "runs", "rf", "bus.jsonl"), "w", encoding="utf-8") as f:
            for i, (kind, task, ts, rt) in enumerate([
                ("info", "T1", "2026-10-13T10:00:00Z", None), ("ask", "T1", "2026-10-13T10:01:00Z", None),
                ("ask", "T2", "2026-10-13T10:02:00Z", None), ("answer", "T1", "2026-10-13T10:03:00Z", "m-2"),
            ], 1):
                f.write(json.dumps({"seq": i, "id": "m-%d" % i, "ts": ts, "run": "rf", "task": task,
                                    "from": task, "to": "orch", "kind": kind, "reply_to": rt, "text": "x"}) + "\n")
        for extra in (["--run", "rf"], []):  # without --run: the data dir's only run
            rc_f = subprocess.run([sys.executable, os.path.abspath(__file__), "metrics", "--data", fdata] + extra,
                                  stdout=subprocess.PIPE, text=True, cwd=fdata)
            assert rc_f.returncode == 0, rc_f.stdout
            fm = json.loads(rc_f.stdout.strip().splitlines()[-1])
            assert fm == {"ok": True, "mode": "file", "run": "rf",
                          "messages_by_kind": {"info": 1, "ask": 2, "answer": 1},
                          "last_message_by_lane": {"T1": "2026-10-13T10:03:00Z", "T2": "2026-10-13T10:02:00Z"},
                          "pending_asks": 1}, fm
        assert metrics["mode"] == "store", metrics

        # no direct database-module import text anywhere in this script
        src = open(os.path.abspath(__file__), encoding="utf-8").read()
        assert ("sq" + "lite3") not in src, "mk-bus.py mentions the database module"


        kill(pid2)
        _lane_state_self_test(mod)
        # R1.2: the write guard refuses the real home here too, in store mode.
        mod.selftest_guard_probe(store_home)
    finally:
        os.environ["PATH"] = orig_path
        for k, v in store_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for p in pids:
            stop_process(p)
        shutil.rmtree(tmp, ignore_errors=True)


def _lane_state_self_test(mod):
    """E8 (todos 67, 68d) on the REAL bus in store mode, on a temp home: a lane `task`
    message `status verify|done|in_progress` moves the lane's own task, an ask from a building
    task blocks it and the answer (HTTP and CLI) puts it back, an ask from a planned task still
    posts with a note. Mock-free: real serve, real store, real token and answer commands."""
    import shutil

    tmp = tempfile.mkdtemp(prefix="mk-bus-e8-")
    pids = []
    saved = {k: os.environ.get(k) for k in ("MK_BUS_DIR", "MK_HOME", "MATRIXKODER_HOME")}
    home = os.path.join(tmp, "home")
    data = os.path.join(tmp, "data")
    os.makedirs(home)
    os.makedirs(data)
    for k in saved:
        os.environ[k] = home

    def call(port, token, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                     "Host": f"127.0.0.1:{port}"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")

    def status_of(task):
        con = mod.open_project(data, data_dir=data)
        try:
            return con.execute("SELECT status FROM tasks WHERE run='r1' AND id=?", (task,)).fetchone()["status"]
        finally:
            con.close()

    def set_status(task, to):
        con = mod.open_project(data, data_dir=data)
        try:
            con.execute("UPDATE tasks SET status=? WHERE run='r1' AND id=?", (to, task))
            con.commit()
        finally:
            con.close()

    lane_tokens = {}

    def lane_token(task):
        if task in lane_tokens:
            return lane_tokens[task]
        lane = os.path.join(tmp, "lane-" + task)
        os.makedirs(lane)
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "token", "--lane", lane,
                            "--task", task, "--run", "r1", "--data", data],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert r.returncode == 0, r.stderr
        lane_tokens[task] = json.loads(r.stdout.strip().splitlines()[-1])["token"]
        return lane_tokens[task]

    try:
        root = mod.open_root(home)
        try:
            root.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, created) VALUES (?,?,?,?,?,?)",
                         ("proj", "proj", "", data, "managed", "t"))
            root.commit()
        finally:
            root.close()
        mod.import_history(data, None, "r1", run_row={"id": "r1", "status": "active"}, data_dir=data)
        con = mod.open_project(data, data_dir=data)
        try:
            for t in ("S1", "S2", "S3", "L1", "A1", "A2"):
                mod.add_task(con, "r1", t, "task", title="e8 " + t)
            for t, parent in (("S1a", "S1"), ("S1b", "S1a"), ("S1c", "S1"), ("S2x", "S2")):
                mod.add_task(con, "r1", t, "task", title="e8 " + t, parent=parent)
        finally:
            con.close()
        proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve", "--data", data, "--run", "r1"],
                                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pids.append(proc.pid)
        port = None
        deadline = time.time() + 15.0
        while time.time() < deadline and port is None:
            b = read_json(os.path.join(home, "bus.json")) or {}
            if b.get("pid") == proc.pid and b.get("port"):
                port = b["port"]
            elif proc.poll() is not None:
                raise AssertionError(f"serve exited {proc.returncode}")
            else:
                time.sleep(0.05)
        assert port, "e8 serve did not publish bus.json"
        orch = secrets.token_urlsafe(32)
        toks = read_json(os.path.join(home, "bus-tokens.json"), default={})
        toks[orch] = {"token": orch, "role": "orch", "run": "r1", "task": "", "lane": "",
                      "project": "proj", "created_at": now_iso()}
        write_json(os.path.join(home, "bus-tokens.json"), toks, mode=0o600)

        def lane_word(task, word):
            return call(port, lane_token(task), "/send", {"kind": "task", "text": f"status {word}\nround: 1\ntodo done X"})

        def entered(task, to):
            con = mod.open_project(data, data_dir=data)
            try:
                return con.execute("SELECT COUNT(*) c FROM events WHERE chan='state' AND kind='transition' "
                                   "AND run='r1' AND task=? AND to_id=?", (task, to)).fetchone()["c"]
            finally:
                con.close()

        # Todo 67: the lane words move the lane's own task along the builder path; `done` caps at
        # verifying (only the orchestrator sets done); the same word again moves nothing twice.
        for task, steps in (("S1", (("in_progress", "building"),)),
                            ("S2", (("in_progress", "building"), ("verify", "verifying"))),
                            ("S3", (("in_progress", "building"), ("done", "verifying")))):
            for word, want in steps:
                code, res = lane_word(task, word)
                assert code == 200 and res.get("state_updated") is True and res.get("mk_state_error") is None, (word, res)
                assert "state_ignored" not in res and status_of(task) == want, (word, status_of(task), res)
        code, res = lane_word("S2", "verify")
        assert code == 200 and res.get("state_updated") is False and "state_ignored" not in res, res
        assert entered("S2", "building") == 1 and entered("S2", "verifying") == 1
        tok_s1 = lane_token("S1")
        code, res = call(port, tok_s1, "/send", {"kind": "task", "text": "status bogus\nround: 1\n"})
        assert code == 200 and "state_updated" not in res and status_of("S1") == "building", res
        code, res = call(port, tok_s1, "/send", {"kind": "task", "task": "S2", "text": "status done\nround: 1\n"})
        assert code == 403 and status_of("S2") == "verifying", (code, res)

        # Part 5: `todo <state> <id>` lines move the lane's own subtasks (any depth), never another task.
        code, res = call(port, tok_s1, "/send", {"kind": "task", "text": "status in_progress\nround: 1\ntodo in_progress S1a\ntodo in_progress S1b"})
        assert code == 200 and res.get("todo_updated") == ["S1a", "S1b"], res
        assert status_of("S1a") == "building" and status_of("S1b") == "building", (status_of("S1a"), status_of("S1b"))
        code, res = call(port, tok_s1, "/send", {"kind": "task", "text": "round: 1\ntodo done S1b\ntodo done S2x\ntodo done S2"})
        assert code == 200 and res.get("todo_updated") == ["S1b"] and res.get("todo_ignored") == ["S2x", "S2"], res
        assert status_of("S1b") == "verifying" and status_of("S2x") == "planned" and status_of("S2") == "verifying", (status_of("S1b"), status_of("S2x"))
        with open(_load_mkpaths().legacy_yaml(data), "w") as yf:
            yf.write("todo_done_status: done\n")  # a lane still stops at verifying
        assert status_of("S1a") == "verifying", status_of("S1a")  # rolled up from its only child
        code, res = call(port, tok_s1, "/send", {"kind": "task", "text": "todo done S1c"})  # never started: no move
        assert code == 200 and res.get("todo_updated") == [] and status_of("S1c") == "planned", res
        code, res = call(port, tok_s1, "/send", {"kind": "task", "text": "todo in_progress S1c\ntodo completed S1c"})
        # One entry per id (review LOW): both lines are applied in order, so S1c ends in its
        # LAST state, and the reply names it once.
        assert code == 200 and res.get("todo_updated") == ["S1c"] and status_of("S1c") == "verifying", res
        con = mod.open_project(data, data_dir=data)
        try:
            for t in ("S1d", "S1e"):
                mod.add_task(con, "r1", t, "task", title="e8 " + t, parent="S1")
        finally:
            con.close()
        code, res = call(port, tok_s1, "/send", {"kind": "task", "text": "todo in_progress S1d\ntodo in_progress S1e\ntodo done S1d"})
        assert code == 200 and res.get("todo_updated") == ["S1d", "S1e"], res  # distinct ids untouched
        assert status_of("S1d") == "verifying" and status_of("S1e") == "building", (status_of("S1d"), status_of("S1e"))

        # A late or stale lane word never overwrites a state the orchestrator moved on to, and
        # `done` from a lane never reaches done or merged (R2 HIGH).
        for state in ("review", "merged", "qa", "done", "blocked", "stalled", "quota", "provider", "verifying"):
            code, st = call(port, orch, "/send", {"run": "r1", "task": "L1", "kind": "status", "text": state,
                                                  "from": "orch", "to": "L1"})
            assert code == 200, (state, st)
            for word in ("in_progress", "done", "verify"):
                code, res = lane_word("L1", word)
                if state == "verifying" and word in ("done", "verify"):
                    assert res.get("state_updated") is False and "state_ignored" not in res, (state, word, res)
                    continue
                assert code == 200 and res.get("state_updated") is False and res.get("state_ignored"), (state, word, res)
                assert status_of("L1") == state, (state, word, status_of("L1"))
        code, st = call(port, orch, "/send", {"run": "r1", "task": "L1", "kind": "status", "text": "fix", "from": "orch", "to": "L1"})
        code, res = lane_word("L1", "done")  # fix -> verifying is not a lane move
        assert res.get("state_updated") is False and res.get("state_ignored") and status_of("L1") == "fix", res
        code, res = lane_word("L1", "in_progress")
        assert res.get("state_updated") is True and status_of("L1") == "building", res
        code, res = lane_word("L1", "done")
        assert res.get("state_updated") is True and status_of("L1") == "verifying", res
        assert entered("L1", "done") == 1 and entered("L1", "merged") == 1  # the orchestrator's, not the lane's

        # Todo 68d: a building task is blocked by its ask and unblocked by the answer.
        set_status("A1", "building")
        tok_a1 = lane_token("A1")
        code, ask = call(port, tok_a1, "/send", {"kind": "ask", "text": "which fixture?"})
        assert code == 200 and ask.get("state_updated") is True and ask.get("mk_state_error") is None, ask
        assert status_of("A1") == "blocked", status_of("A1")
        code, ans = call(port, orch, "/send", {"run": "r1", "task": "A1", "kind": "answer", "text": "the schema one",
                                               "from": "orch", "to": "A1", "reply_to": ask["message"]["id"]})
        assert code == 200 and ans.get("state_updated") is True and ans.get("mk_state_error") is None, ans
        assert status_of("A1") == "building", status_of("A1")
        # the answer CLI takes the same store path (no mk-state, no second write)
        code, ask2 = call(port, tok_a1, "/send", {"kind": "ask", "text": "and the report?"})
        assert code == 200 and ask2.get("state_updated") is True and status_of("A1") == "blocked", ask2
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "answer", ask2["message"]["id"], "in the lane",
                            "--run", "r1", "--data", data], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out_ans = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
        assert r.returncode == 0 and out_ans.get("state_updated") is True and out_ans.get("mk_state_error") is None, (r.stdout, r.stderr)
        assert status_of("A1") == "building", status_of("A1")
        # a planned task: the ask still posts, nothing moves, the note says why
        code, ask3 = call(port, lane_token("A2"), "/send", {"kind": "ask", "text": "planned lane ask"})
        assert code == 200 and ask3["ok"] is True and ask3.get("state_updated") is False, ask3
        assert "planned" in (ask3.get("mk_state_error") or "") and "building" in ask3["mk_state_error"], ask3
        assert status_of("A2") == "planned"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for p in pids:
            stop_process(p)
        shutil.rmtree(tmp, ignore_errors=True)


def _load_script_module(name, path):
    """One script loaded by absolute path through importlib — the way _load_mkpaths and
    _load_mkdb load their siblings (each script stands alone)."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The home env names every MK script honours (mkpaths._clear_home_env's set).
_ARCHIVE_HOME_KEYS = ("MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA", "MK_BUS_DIR",
                      "MK_BUS_TOKEN_FILE")
def _z14_seed_project(home, slug, data, run_id, migrated, plan="alpha", task="T1"):
    """One project store for the Z14 path cases, through mkdb's own writers.

    A migrated project's store is the C2 one (target_project_db), and an EMPTY
    legacy <data>/mk.db stays on disk — store_mode's guard only checks the file,
    and _own_project_store must be the one that resolves the folder. Pre-migration
    the store IS <data>/mk.db and nothing else exists. Alias rows have no mkdb
    writer; the caller INSERTs them into the root directly (the fixture convention)."""
    mod = _load_mkdb()
    mp = _load_mkpaths()
    store_dir = mp.target_project_dir(slug) if migrated else data
    os.makedirs(store_dir, exist_ok=True)
    if migrated:
        # an EMPTY legacy store at <data>/mk.db: store_mode only checks the file
        mod.open_project(home, None, data).close()
        if not os.path.isfile(_load_mkpaths().db_in(store_dir)):
            mod.open_project(home, slug, None).close()
    con = mod.open_project(home, slug if migrated else None, store_dir)
    try:
        con.execute("INSERT OR REPLACE INTO runs(id, plan, plan_version, branch, status,"
                    " stage, next, started, ended, extra_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                    (run_id, plan, 1, "mk/%s/%s" % (plan, run_id), "done", "1", None,
                     "2026-10-12T09:00:00+00:00", "2026-10-12T15:00:00+00:00"))
        con.execute(
            "INSERT OR REPLACE INTO tasks(run, id, parent, type, title, acceptance, owned,"
            " deps, parallel, route_json, status, prev_status, file_status, lane, slot,"
            " branch, pane, round, verdict, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, task, None, "task", task, None, None, "", None, None,
             "done", None, None, None, None, None, None, 0, None, None))
        con.commit()
    finally:
        con.close()
    mod.append_event(home, slug, run_id, task=task, chan="bus", kind="info",
                     text="the archived message", from_id="orch", to_id=task,
                     data_dir=store_dir, t="2026-10-12T10:00:00+00:00")
    return store_dir


def _z14_path_self_test():
    """Z14: the archive FILE path is derived from the db_path FOLDER, never a slug.

    The BUGGY main (e83e6e9) builds `path = os.path.join(_paths("target_run_dir",
    slug, plan, run), "bus.jsonl")` from the slug _own_project_store returned, and
    with a derived ALIAS row (`p<hash>` of a data dir it had no row for, LEFT behind
    by project_slug_for after the real slug took over) that slug CAN win the read:
    the buggy build then CREATES a stray projects/p<hash>/plans/... tree beside the
    real project folder and names its bus.jsonl there. The FIX derives the path
    from the db_path FOLDER (`<folder>/plans/<plan>/runs/<run>/bus.jsonl`), so no
    alias slug can ever name the folder:

      (a) the alias row: the archive still lands at projects/<slug>/plans/<plan>/
          runs/<run>/bus.jsonl and no projects/p66677a607f481f16/ folder exists
          afterwards. Fails on the buggy main (the alias folder IS created);
      (b) a SINGLE migrated row — no-op: same folder, no stray beside it;
      (c) PRE-migration (db_path NULL or '') — unchanged: <data>/runs/<run>/bus.jsonl
          and no projects/ tree at all.

    Fails on a tree without archive-run (exit 2, "unknown command")."""
    def cli(args, env):
        p = subprocess.run([sys.executable, os.path.abspath(__file__)] + args,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           env=env, timeout=120)
        lines = [ln.strip() for ln in p.stdout.strip().splitlines() if ln.strip()]
        try:
            doc = json.loads(lines[-1]) if lines else {}
        except ValueError:
            doc = {}
        return p.returncode, doc, (p.stderr or "") + (p.stdout or "")

    tmp = tempfile.mkdtemp(prefix="mk-bus-z14-path-")
    saved = {k: os.environ.get(k) for k in _ARCHIVE_HOME_KEYS}
    try:
        for tag, slug, data_name, alias_row, migrated, db_value in (
                ("(a) alias slug never names the archive folder",
                 "e2e", "repo-a", True, True, "db"),
                ("(b) single migrated row (no-op)", "mig", "repo-b", False, True, "db"),
                ("(c) pre-migration (db_path NULL)", "premig", "repo-c", False, False, None)):
            home = os.path.join(tmp, "home-" + slug)
            data = os.path.join(tmp, data_name, ".matrixkoder")
            os.makedirs(home)
            os.makedirs(data)
            # pin the process env BEFORE seeding: _z14_seed_project resolves mp.home(),
            # which must be the temp home (and never the real one under MK_SELFTEST)
            for _k in _ARCHIVE_HOME_KEYS:
                os.environ.pop(_k, None)
            os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = os.environ["MK_BUS_DIR"] = home
            os.environ["MK_SELFTEST"] = "1"
            db_path = _load_mkpaths().db_in(os.path.join(home, "projects", slug)) if migrated else None
            con = _load_mkdb().open_root(home)
            try:
                # the derived alias row FIRST: project_slug_for registers it before
                # the real row exists, and it stays after the switch takes over
                if alias_row:
                    con.execute(
                        "INSERT INTO projects(slug, name, repo, data_dir, mode, created,"
                        " last_activity, db_path) VALUES (?,?,?,?,?,?,?,?)",
                        ("p66677a607f481f16", slug + "-alias", None, data, "home",
                         "2026-10-12T09:00:00+00:00", "2026-10-12T09:00:00+00:00", db_path))
                con.execute(
                    "INSERT INTO projects(slug, name, repo, data_dir, mode, created,"
                    " last_activity, db_path) VALUES (?,?,?,?,?,?,?,?)",
                    (slug, slug, None, data, "home", "2026-10-12T09:00:00+00:00",
                     "2026-10-12T09:00:00+00:00", db_path))
                con.commit()
            finally:
                con.close()
            store_dir = _z14_seed_project(home, slug, data, "rZ", migrated)
            env = {k: v for k, v in os.environ.items() if k not in _ARCHIVE_HOME_KEYS}
            env["MK_HOME"] = env["MATRIXKODER_HOME"] = env["MK_BUS_DIR"] = home
            env["MK_SELFTEST"] = "1"
            rc, res, blob = cli(["archive-run", "--data", data, "--run", "rZ"], env)
            assert rc == 0 and res.get("ok") is True and res.get("written") is True, \
                (rc, res, blob)
            want = (os.path.join(store_dir, "plans", res["plan"], "runs", "rZ", "bus.jsonl")
                    if migrated else os.path.join(data, "runs", "rZ", "bus.jsonl"))
            assert res["path"] == want, (res["path"], want)
            assert os.path.isfile(want), want
            projects_dir = os.path.join(home, "projects")
            listing = sorted(os.listdir(projects_dir)) if os.path.isdir(projects_dir) else []
            assert listing == ([slug] if migrated else []), listing
            if alias_row:
                alias_dir = os.path.join(projects_dir, "p66677a607f481f16")
                assert not os.path.exists(alias_dir), "the ALIAS folder was created"
            print("pass mk-bus.py: z14 archive-path %s" % tag)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


class _ArchiveFixture:
    """One temp project for the L6.3 archive-run cases: every path through mkpaths, every
    row through mkdb's live writers, a real bus on a temp home. Same shape as the file's
    other store-mode self-tests, kept local so the cases stand alone in this file."""

    def __init__(self, tmp, slug, home_env=True, plan="alpha"):
        self.mod = _mkdb_helpers_ready()
        self.mp = _load_mkpaths()
        self._slug, self._plan = slug, plan
        self._saved = {k: os.environ.get(k) for k in _ARCHIVE_HOME_KEYS + ("HOME",)}
        self._procs = {}
        home_root = os.path.join(tmp, "home")
        # the MK home is <tmp>/home when MK_HOME pins it, else the production
        # $HOME/.matrixkoder under a temp $HOME
        home = home_root if home_env else os.path.join(home_root, ".matrixkoder")
        repo, data = os.path.join(tmp, "repo-" + slug), os.path.join(tmp, "repo-" + slug, ".matrixkoder")
        os.makedirs(home, exist_ok=True)
        os.makedirs(data, exist_ok=True)
        self.tmp, self.home, self.repo, self.data = tmp, home, repo, data
        self._env = {k: v for k, v in os.environ.items() if k not in _ARCHIVE_HOME_KEYS}
        self._env["MK_SELFTEST"] = "1"
        self._env["HOME"] = home_root
        if home_env:
            self._env["MK_HOME"] = self._env["MATRIXKODER_HOME"] = self._env["MK_BUS_DIR"] = home
        # the same env in process, so mkpaths/mkdb resolve this temp home here too
        for key in _ARCHIVE_HOME_KEYS:
            os.environ.pop(key, None)
        os.environ["HOME"] = home_root
        os.environ["MK_SELFTEST"] = "1"
        if home_env:
            os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = home
            os.environ["MK_BUS_DIR"] = home
        # the root projects row (db_path NULL): no mkdb helper owns it, the repo's own
        # fixture convention (mkpaths._seed_root)
        con = self.mod.open_root(home)
        try:
            con.execute(
                "INSERT OR IGNORE INTO projects(slug, name, repo, data_dir, mode, created,"
                " last_activity) VALUES (?,?,?,?,?,?,?)",
                (slug, slug, os.path.realpath(repo), os.path.realpath(data), "home",
                 "2026-10-12T09:00:00+00:00", "2026-10-12T09:00:00+00:00"))
            con.commit()
        finally:
            con.close()
        self.mod.open_project(home, slug, data_dir=data).close()

    # ---- paths ---------------------------------------------------------------------------------
    @property
    def slug(self):
        return self._slug

    @property
    def plan(self):
        return self._plan

    def env(self):
        """A fresh child env for CLI subprocesses (MK_SELFTEST plus this fixture's home)."""
        return dict(self._env)

    def folder(self):
        """The project folder every writer and reader must use: <data> before the switch,
        the C2 project folder after it — mkpaths.project_folder's one rule, the same
        _own_project_store and run_dir follow. Never hand-built."""
        return _paths("project_folder", self.data)

    def store_path(self):
        return _load_mkpaths().db_in(self.folder())

    def paths(self):
        """The paths the cases assert against, every one resolved through mkpaths."""
        return {"home": self.home, "data": self.data, "store": self.store_path(),
                "project_folder": self.folder()}

    # ---- rows ----------------------------------------------------------------------------------
    def add_run(self, run_id, status="active", tasks=()):
        """One import_history call: the run row plus its task rows (a task spec may be a
        plain id or a dict with status)."""
        specs = []
        for t in tasks:
            spec = {"id": t, "type": "task", "title": t} if isinstance(t, str) else dict(t)
            spec.setdefault("type", "task")
            spec.setdefault("title", spec["id"])
            spec.setdefault("status", "planned")
            specs.append(spec)
        run_row = {"id": run_id, "plan": self._plan, "plan_version": 1,
                   "branch": "mk/%s/%s" % (self._plan, run_id), "status": status, "stage": "1",
                   "next": None, "started": "2026-10-12T09:00:00+00:00",
                   "ended": None if status == "active" else "2026-10-12T15:00:00+00:00"}
        self.mod.import_history(self.home, self._slug, run_id, run_row=run_row, tasks=specs,
                                data_dir=self.folder())
        return run_id

    def set_run_status(self, run_id, status):
        con = self.mod.open_project(self.home, self._slug, data_dir=self.folder())
        try:
            con.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
            con.commit()
        finally:
            con.close()
        return status

    def event(self, run, *, chan="bus", kind, task=None, from_id=None, to_id=None,
              reply_to=None, text="", meta=None, artifact_id=None, t=None):
        """mkdb.append_event -> the new event id; the message id is 'm-<id>'."""
        return self.mod.append_event(self.home, self._slug, run, task=task, chan=chan,
                                     kind=kind, from_id=from_id, to_id=to_id,
                                     reply_to=reply_to, text=text, meta=meta,
                                     artifact_id=artifact_id, t=t, data_dir=self.folder())

    def ack(self, run, msg, lane, level=2, t=None):
        return self.mod.append_ack(self.home, self._slug, run, msg, lane, level, t=t,
                                   data_dir=self.folder())

    def artifact(self, run, task, round_n, kind, text, event_id=None):
        return self.mod.put_artifact(self.home, self._slug, run, task, round_n, kind, text,
                                     event_id=event_id, data_dir=self.folder())

    def import_artifacts(self, run, specs):
        """Importer-path artifacts (src SET) through mkdb's history writer — the same writer
        mk-state's store-enable path uses, so the rows carry src exactly like production.
        -> the ids of the rows written (a repeat adds nothing)."""
        before = set(self.imported_artifact_ids(run))
        self.mod.import_history(self.home, self._slug, run, artifacts=list(specs),
                                data_dir=self.folder())
        return [i for i in self.imported_artifact_ids(run) if i not in before]

    def imported_artifact_ids(self, run_id):
        """The artifact ids archive-run must LEAVE: the importer's rows of this run (src set
        — mk-state's run-doc/index artifacts carry it). The discriminator that proves the
        live-path filter."""
        con = self.mod._open_read(self.store_path(), self.mod.SCHEMA)
        if con is None:
            return []
        try:
            return [r["id"] for r in con.execute(
                "SELECT id FROM artifacts WHERE run=? AND src IS NOT NULL ORDER BY id", (run_id,))]
        finally:
            con.close()

    # ---- the C2 layout --------------------------------------------------------------------------
    def migrate(self):
        """The real mk-migrate --switch: the project joins the C2 tree and every mkpaths
        resolve follows — the store the writers above use, the store archive-run reads
        (_own_project_store follows db_path) and the bus.jsonl path (run_dir). Call it
        AFTER the last store write: the switch copies the store, so a pre-switch write is
        what lands in the C2 store."""
        if not os.path.isdir(os.path.join(self.repo, ".git")):
            subprocess.run(["git", "init", "-q", self.repo], check=True, env=self.env(),
                           capture_output=True)
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "mk-migrate.py"), "--switch", "--slug", self._slug,
             "--data", self.data, "--home", self.home, "--repo", self.repo],
            capture_output=True, text=True, env=self.env(), timeout=300)
        if proc.returncode != 0:
            raise AssertionError("mk-migrate --switch failed rc=%s:\n%s\n%s"
                                 % (proc.returncode, proc.stdout, proc.stderr))
        self.mp._MIGRATED_MEMO = None  # a re-seeded root: drop the memo this process holds
        self.mp._RUN_PLAN_MEMO = None
        return self.paths()

    # ---- the real bus ---------------------------------------------------------------------------
    def start_bus(self, run):
        """The real `serve --data <data> --run <run>`: spawn, poll bus.json for this pid's
        port, then /health. No fixed sleeps — every wait is a bounded poll."""
        argv = [sys.executable, os.path.join(HERE, "mk-bus.py"), "serve",
                "--data", self.data, "--run", run]
        err_path = os.path.join(self.tmp, "serve-%s-%s.stderr" % (self._slug, run))
        with open(err_path, "wb") as err:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=err,
                                    env=self.env(), start_new_session=True)
        self._procs[proc.pid] = (proc, err_path)
        try:
            port = self._wait_bus_json(proc.pid)
            self._wait_healthy(port)
        except BaseException:
            self.stop_bus(proc.pid)
            raise
        return proc.pid, port

    def _wait_bus_json(self, pid, timeout=30.0):
        path = _paths("bus_json", self.data)
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            entry = self._procs.get(pid)
            if entry is not None and entry[0].poll() is not None:
                raise AssertionError("serve exited %s before publishing its port (stderr=%s)"
                                     % (entry[0].returncode, self._stderr_tail(pid)))
            try:
                with open(path) as f:
                    doc = json.load(f)
                last = doc
                if isinstance(doc, dict) and doc.get("pid") == pid and isinstance(doc.get("port"), int):
                    return doc["port"]
            except (OSError, ValueError):
                pass
            time.sleep(0.02)
        raise AssertionError("serve pid %s never published its port (bus.json=%r, stderr=%s)"
                             % (pid, last, self._stderr_tail(pid)))

    def _wait_healthy(self, port, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=2) as r:
                    if json.loads(r.read().decode()).get("status") == "healthy":
                        return
            except (urllib.error.URLError, ValueError, OSError):
                pass
            time.sleep(0.02)  # a poll, never a fixed sleep: the deadline above bounds it
        raise AssertionError("bus on port %d never became healthy" % port)

    def _stderr_tail(self, pid, limit=2000):
        entry = self._procs.get(pid)
        if not entry:
            return "<gone>"
        try:
            with open(entry[1]) as f:
                return f.read()[-limit:]
        except OSError:
            return "<unreadable>"

    def mint_orch(self):
        """The live orch (server) token from bus-tokens.json: serve retires every earlier
        orch entry on start, so exactly one role=="orch" token is valid."""
        with open(_paths("bus_tokens", self.data)) as f:
            tokens = json.load(f)
        orch = [tok for tok, v in tokens.items()
                if isinstance(v, dict) and v.get("role") == "orch"]
        if len(orch) != 1:
            raise AssertionError("expected exactly one orch token, got %r" % (sorted(orch),))
        return orch[0]

    def lane_token(self, run, task):
        """The real `token` CLI for a lane worktree (<tmp>/lane-<run>-<task>) -> the bearer
        token; the lane's token file lands at mkpaths.lane_token(lane)."""
        lane = os.path.join(self.tmp, "lane-%s-%s-%s" % (self._slug, run, task))
        os.makedirs(lane, exist_ok=True)
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "mk-bus.py"), "token", "--lane", lane,
             "--task", task, "--run", run, "--data", self.data],
            capture_output=True, text=True, env=self.env(), timeout=60)
        if proc.returncode != 0:
            raise AssertionError("token failed rc=%s: %s %s"
                                 % (proc.returncode, proc.stdout, proc.stderr))
        return read_json(_paths("lane_token", lane))["token"]

    def post(self, port, token, body, path="/send"):
        """A real HTTP POST against the running bus -> (status, reply-dict)."""
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (port, path),
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer %s" % token,
                     "Host": "127.0.0.1:%d" % port})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw}

    def stop_bus(self, pid):
        """Terminate one pid, escalate once, bounded poll until dead (never a fixed wait).
        Liveness goes through Popen.poll(): a serve spawned here is this process's child,
        so a killed one lingers as a zombie that kill(pid, 0) still reports as alive."""
        entry = self._procs.get(pid)
        proc = entry[0] if entry else None

        def alive():
            if proc is not None:
                return proc.poll() is None
            try:
                os.kill(pid, 0)
                return True  # not ours to reap: trust the signal
            except OSError:
                return False

        if not alive():
            self._forget(pid)
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except OSError:
                break
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and alive():
                time.sleep(0.02)
            if not alive():
                break
        if alive():
            raise AssertionError("bus pid %s did not stop (stderr=%s)"
                                 % (pid, self._stderr_tail(pid)))
        self._forget(pid)

    def _forget(self, pid):
        self._procs.pop(pid, None)

    def cleanup(self):
        """Reap every bus this fixture started, restore the env it pinned, and remove the
        fixture's own tree. The shared <tmp>/home is left to the caller's outer cleanup."""
        for pid in list(self._procs):
            try:
                self.stop_bus(pid)
            except AssertionError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        for proc, _ in list(self._procs.values()):
            if proc.poll() is None:
                proc.kill()
        self._procs = {}
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        for name in sorted(os.listdir(self.tmp)):
            if name == "home" or not name.startswith(("repo-" + self._slug, "lane-" + self._slug,
                                                      "serve-" + self._slug)):
                continue
            shutil.rmtree(os.path.join(self.tmp, name), ignore_errors=True)


def _archive_fixture(tmp, slug, home_env=True):
    """One temp project for the L6.3 cases (see _ArchiveFixture)."""
    return _ArchiveFixture(tmp, slug, home_env=home_env)


def _archive_run_self_test():
    """L6.3 (C6) archive-run, on the REAL bus and the REAL project store.

    Seven cases. Each fails on a tree without `archive-run`, where the CLI exits 2
    with "unknown command archive-run":

      (a) a closed run is archived ON THE SWITCHED C2 LAYOUT (the fixture runs the real
          mk-migrate --switch): the file lands at the mkpaths run dir — <project folder>/
          plans/<plan>/runs/<run>/bus.jsonl, never hand-built — its line count is this run's
          messages+acks+artifacts read from the store BEFORE the call, the counts, the seq
          range and the sha256 on stdout match a recomputation from those rows AND from the
          file itself, and those rows are gone afterwards while the run row, its tasks, its
          chan='state' events, its importer artifacts (src SET), its LIVE-PATH artifacts
          (brief / review-brief / review, src NULL — task data, not bus traffic) and another
          run's rows are not; every revision of a bus report is archived and none of those
          decoys is; a message sent on a live run still lands above the archived seq range;
      (b) an ACTIVE run is refused before anything is written, and so is a closed run with a
          non-terminal task — including `review` and `verifying`, where a reviewer or verifier
          lane is still live: exit 2, the reason named, no file, the rows untouched. The other
          closed spelling (archived) is accepted;
      (c) a dropped archive line fails the count check: nothing is deleted and the partial
          file is removed — through the writer seam wrapped in process, and through the CLI
          with the documented drop-line env;
      (d) a cursor that points at a deleted seq resumes at the NEXT existing seq: the watch
          stream, the SSE stream and the lane /wait path never replay what was already seen,
          never reset to 0 and never error;
      (e) a second archive of the same run is a no-op: exit 0, nothing written, the file's
          bytes unchanged — on a temp $HOME with MK_HOME/MATRIXKODER_HOME/MATRIXKODER_DATA
          unset;
      (f) an archived run's seqs are never reused: the run held the highest seqs, they are
          gone from the table, and the live run's next message still lands above them (the
          high-water guard row is the only events row left holding the range, invisible to
          every run-scoped, chan='bus' and bus-kind reader);
      (g) a message that lands between the count check and the delete makes the transaction
          re-check refuse: nothing is deleted, the archive file does not survive, and the
          retry archives the run whole, that message included.
    """
    import shutil

    if _mkdb_helpers_ready() is None:
        print("archive-run self-test skipped: mkdb live writers not present")
        return
    bus_py = os.path.abspath(__file__)
    tmp = tempfile.mkdtemp(prefix="mk-bus-archive-")
    # The canonical row JSON of one archived row is the SELECT column order, tight
    # separators. Recomputed here from the store so the digest checks below are independent
    # of the code under test rather than a call back into it.
    cols_of = {
        "msg": ("id", "run", "task", "t", "chan", "kind", "from_id", "to_id",
                "reply_to", "text", "artifact_id", "meta_json"),
        "ack": ("run", "msg", "lane", "level", "t"),
        "artifact": ("id", "run", "task", "round", "kind", "rev", "text", "created", "src"),
    }
    sql_of = {
        "msg": "SELECT %s FROM events WHERE chan='bus' AND run=? ORDER BY id" % ", ".join(cols_of["msg"]),
        "ack": "SELECT %s FROM acks WHERE run=? ORDER BY msg, lane" % ", ".join(cols_of["ack"]),
        # bus artifacts are the rows a BUS MESSAGE carries: the events.artifact_id
        # link put_artifact writes for the event it was stored for. src IS NULL is
        # NOT the filter — mk-lane-event and mk-builder use the same live writer
        # (src NULL) for a task's brief / review-brief / review, and those rows
        # are not bus traffic and must survive. An importer row (src set) is
        # mk-state's run-doc/index data and is unreferenced either way.
        # one binding: the subquery correlates on the run the outer row names
        "artifact": ("SELECT %s FROM artifacts WHERE run=? AND id IN"
                     " (SELECT artifact_id FROM events WHERE chan='bus'"
                     " AND run=artifacts.run AND artifact_id IS NOT NULL) ORDER BY id"
                     % ", ".join(cols_of["artifact"])),
    }

    def rows_of(f, run):
        """One run's (msg, ack, artifact) rows straight from the store every reader uses, in
        archive order. f.folder() is <data> before a switch and the C2 project folder after,
        the one rule mkpaths.project_folder gives."""
        folder = f.folder()
        con = f.mod.open_project(folder, data_dir=folder)
        try:
            return {name: [dict(r) for r in con.execute(sql, (run,))]
                    for name, sql in sql_of.items()}
        finally:
            con.close()

    def canonical(name, row):
        return json.dumps([row[c] for c in cols_of[name]], separators=(",", ":"), ensure_ascii=False)

    def digest_of(rows_by_group):
        h = hashlib.sha256()
        for name in ("msg", "ack", "artifact"):
            for row in rows_by_group[name]:
                h.update(canonical(name, row).encode("utf-8"))
                h.update(b"\n")
        return h.hexdigest()

    def digest_of_file(path):
        """(aggregate digest, lines) FROM THE FILE, checking every line's own sha256 too."""
        with open(path, "r", encoding="utf-8") as fh:
            objs = [json.loads(ln) for ln in fh.read().split("\n") if ln]
        h = hashlib.sha256()
        for name in ("msg", "ack", "artifact"):
            for obj in [o for o in objs if o["type"] == name]:
                canon = canonical(name, obj["row"])
                assert obj["sha256"] == hashlib.sha256(canon.encode("utf-8")).hexdigest(), \
                    "archive-run: a %s line's own sha256 does not match its row" % name
                h.update(canon.encode("utf-8"))
                h.update(b"\n")
        return h.hexdigest(), objs

    def cli(args, env=None, timeout=60):
        p = subprocess.run([sys.executable, bus_py] + args, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, env=env, timeout=timeout)
        lines = [ln.strip() for ln in p.stdout.strip().splitlines() if ln.strip()]
        try:
            doc = json.loads(lines[-1]) if lines else {}
        except ValueError:
            doc = {}
        # A refusal rides in the one JSON line on stdout (the file-mode Fail convention),
        # so the blob is what the reason assertions search.
        return p.returncode, doc, (p.stderr or "") + (p.stdout or "")

    def set_statuses(f, run, run_status=None, tasks=()):
        folder = f.folder()
        con = f.mod.open_project(folder, data_dir=folder)
        try:
            if run_status is not None:
                con.execute("UPDATE runs SET status=? WHERE id=?", (run_status, run))
            for task, status in tasks:
                con.execute("UPDATE tasks SET status=? WHERE run=? AND id=?", (status, run, task))
            con.commit()
        finally:
            con.close()

    def scalar(f, sql, args=()):
        folder = f.folder()
        con = f.mod.open_project(folder, data_dir=folder)
        try:
            return con.execute(sql, args).fetchone()[0]
        finally:
            con.close()

    def get(port, path, token=None, timeout=5.0, last_event_id=None):
        """One real GET against the running bus (the /wait and /events paths)."""
        headers = {"Host": "127.0.0.1:%d" % port}
        if token:
            headers["Authorization"] = "Bearer " + token
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")

    def ask(f, port, token, run, task, text, to="orch"):
        """One ask over the real bus: an ask never rings, so no ring row can appear later."""
        code, msg = f.post(port, token, {"run": run, "task": task, "kind": "ask", "text": text,
                                         "from": task, "to": to})
        assert code == 200 and msg.get("ok") is True, (code, msg)
        return msg["message"]

    def case(name):
        print("pass mk-bus.py: archive-run " + name)

    try:
        # ---- (a) a closed run is archived; a live run keeps sending ---------------------------
        f = _archive_fixture(tmp, "proj")
        f.add_run("rA", status="active", tasks=("T1",))
        f.add_run("rB", status="active", tasks=("T1",))
        pid, port = f.start_bus("rA")
        orch = f.mint_orch()
        tokA, tokB = f.lane_token("rA", "T1"), f.lane_token("rB", "T1")
        askmsg = ask(f, port, tokA, "rA", "T1", "which fixture?")
        code, ans = f.post(port, orch, {"run": "rA", "task": "T1", "kind": "answer",
                                        "text": "the schema one", "from": "orch", "to": "T1",
                                        "reply_to": askmsg["id"]})
        assert code == 200 and ans["ok"] is True, (code, ans)
        code, acked = f.post(port, tokA, {"run": "rA", "task": "T1", "kind": "ack", "text": "",
                                          "from": "T1", "to": "orch",
                                          "reply_to": ans["message"]["id"], "ack_level": 3})
        assert code == 200 and acked.get("ack_recorded") is True, (code, acked)
        code, rep = f.post(port, tokA, {"run": "rA", "task": "T1", "kind": "report",
                                        "text": "round:1\nthe report body", "from": "T1", "to": "orch"})
        assert code == 200 and rep.get("artifact", {}).get("rev") == 1, (code, rep)
        # a second report for the same (task, round, kind) SUPERSEDES it: rev 2, its own
        # event link. Both revisions are bus rows and both must be archived, not orphaned.
        code, rep2 = f.post(port, tokA, {"run": "rA", "task": "T1", "kind": "report",
                                        "text": "round:1\nthe report body, revised",
                                        "from": "T1", "to": "orch"})
        assert code == 200 and rep2.get("artifact", {}).get("rev") == 2, (code, rep2)
        # LIVE-PATH decoys: mk-lane-event and mk-builder store a task's brief,
        # review-brief and review through the very same live writer put_artifact
        # (src NULL) the bus uses. They are task data, not bus traffic: an archive
        # that took them would delete the brief and review of the run's task.
        decoys = {kind: f.artifact("rA", "T1", 1, kind, "the %s body" % kind)
                  for kind in ("brief", "review-brief", "review")}
        # a state event of the same run is not bus data: it must survive the archive
        code, st = f.post(port, orch, {"run": "rA", "task": "T1", "kind": "status", "text": "done",
                                       "from": "orch", "to": "T1"})
        assert code == 200 and st["ok"] is True, (code, st)
        # the live run keeps a message: it must survive the archive, and answering it drains
        # the inbox so the switch below can quiesce
        ask_b = ask(f, port, tokB, "rB", "T1", "run B before the archive")
        code, ans_b = f.post(port, orch, {"run": "rB", "task": "T1", "kind": "answer",
                                          "text": "answered before the archive", "from": "orch",
                                          "to": "T1", "reply_to": ask_b["id"]})
        assert code == 200 and ans_b["ok"] is True, (code, ans_b)
        set_statuses(f, "rA", run_status="done", tasks=(("T1", "done"),))
        want, want_b = rows_of(f, "rA"), rows_of(f, "rB")
        assert want["msg"] and want["ack"] and want["artifact"], \
            "case (a) needs messages, an ack and a bus artifact: %s" % {k: len(v) for k, v in want.items()}
        state_before = scalar(f, "SELECT COUNT(*) FROM events WHERE run=? AND chan<>'bus'", ("rA",))
        tasks_before = scalar(f, "SELECT COUNT(*) FROM tasks WHERE run=?", ("rA",))
        assert state_before >= 1, "the status send must have left a state event behind"
        # IMPORTER artifacts (src set, written through mkdb's history writer) are mk-state's
        # run-doc and index data, not bus traffic: they are the discriminator that proves the
        # live-path artifact filter
        imported = f.import_artifacts("rA", [
            {"src": "run-doc-rA", "task": None, "round": 0, "kind": "run-doc", "rev": 1,
             "text": "the run document", "created": "2026-10-12T09:00:00+00:00"},
            {"src": "index-rA", "task": None, "round": 0, "kind": "index", "rev": 1,
             "text": "the runs index", "created": "2026-10-12T09:00:01+00:00"}])
        assert imported, "the importer rows must land"
        # the real C2 switch (L2's mk-migrate --switch): the archive must land in the project
        # folder's plans/<plan>/runs/<run>, resolved through mkpaths and never hand-built
        f.stop_bus(pid)
        f.migrate()
        path = os.path.join(_paths("run_dir", f.data, "rA"), "bus.jsonl")
        # the mkpaths answer above is the assertion under test; this is the documented C2
        # shape it must carry, so a switch that never happened cannot pass the case
        assert f.folder() != f.data, (f.folder(), f.data)
        assert path == os.path.join(f.paths()["project_folder"], "plans", f.plan, "runs", "rA",
                                    "bus.jsonl"), path

        rc, res, blob = cli(["archive-run", "--data", f.data, "--run", "rA"], env=f.env())
        assert rc == 0 and res.get("ok") is True, (rc, res, blob)
        assert res["op"] == "archive-run" and res["run"] == "rA" and res["written"] is True, res
        assert res["path"] == path, (res["path"], path)
        assert os.path.isfile(path), path
        assert oct(os.stat(path).st_mode & 0o777) == "0o600", oct(os.stat(path).st_mode & 0o777)
        assert (res["messages"], res["acks"], res["artifacts"]) == \
            tuple(len(want[n]) for n in ("msg", "ack", "artifact")), \
            (res, {k: len(v) for k, v in want.items()})
        ids = [r["id"] for r in want["msg"]]
        assert (res["seq_min"], res["seq_max"]) == (min(ids), max(ids)), (res, ids)
        assert res["sha256"] == digest_of(want), (res["sha256"], digest_of(want))
        got, objs = digest_of_file(path)
        assert got == res["sha256"], (got, res["sha256"])
        assert len(objs) == len(ids) + len(want["ack"]) + len(want["artifact"]), len(objs)
        assert [o["row"]["id"] for o in objs if o["type"] == "msg"] == ids, "archived message order"
        art_ids = [o["row"]["id"] for o in objs if o["type"] == "artifact"]
        assert not (set(imported) & set(art_ids)), "an importer artifact row must not be archived"
        # the archived artifacts are the run's BUS revisions only: every revision of the
        # report, and none of the live-path brief / review-brief / review rows the same
        # live writer stored for the run's task (the CRITICAL a src-based filter hit)
        art_rows = [o["row"] for o in objs if o["type"] == "artifact"]
        assert {r["kind"] for r in art_rows} == {"report"}, art_rows
        assert sorted(r["rev"] for r in art_rows) == [1, 2], art_rows
        for kind, art in decoys.items():
            assert scalar(f, "SELECT COUNT(*) FROM artifacts WHERE id=?", (art["id"],)) == 1, \
                "archive-run took the run's live-path %s artifact (%s)" % (kind, art)
        # the rows are gone; the run, its tasks, its STATE events and the importer rows are not
        assert rows_of(f, "rA") == {"msg": [], "ack": [], "artifact": []}, "archived rows must be gone"
        assert f.imported_artifact_ids("rA") == imported, \
            "the importer rows (src set) must survive: %s -> %s" % (imported, f.imported_artifact_ids("rA"))
        assert rows_of(f, "rB") == want_b, "another run's rows must survive this run's archive"
        assert scalar(f, "SELECT COUNT(*) FROM events WHERE run=? AND chan<>'bus'", ("rA",)) == state_before
        assert scalar(f, "SELECT COUNT(*) FROM tasks WHERE run=?", ("rA",)) == tasks_before
        assert scalar(f, "SELECT status FROM runs WHERE id=?", ("rA",)) == "done"
        # a live run still takes messages on the real bus, above the archived seq range. The
        # bus restarts after the switch, so its own store resolves to the C2 project folder.
        pid, port = f.start_bus("rB")
        live = ask(f, port, f.lane_token("rB", "T1"), "rB", "T1", "run B after the archive")
        assert live["seq"] > res["seq_max"], (live, res)
        assert scalar(f, "SELECT COUNT(*) FROM events WHERE id=? AND chan='bus' AND run='rB'",
                      (live["seq"],)) == 1, "the post-archive send did not land"
        f.stop_bus(pid)
        case("(a) closed run archived: file, counts, digest, seq range, rows gone, live run sends")
        f.cleanup()

        # ---- (b) an active run, and a closed run with a live task, are refused ----------------
        f = _archive_fixture(tmp, "projb")
        f.add_run("rA", status="active", tasks=("T1",))
        pid, port = f.start_bus("rA")
        tokA = f.lane_token("rA", "T1")
        for text in ("one", "two"):
            ask(f, port, tokA, "rA", "T1", text)
        before = rows_of(f, "rA")
        path = os.path.join(_paths("run_dir", f.data, "rA"), "bus.jsonl")

        rc, res, blob = cli(["archive-run", "--data", f.data, "--run", "rA"], env=f.env())
        assert rc != 0 and res.get("ok") is False, (rc, res, blob)
        assert "not closed" in blob and "active" in blob, blob
        assert not os.path.exists(path), "a refused archive wrote a file"
        assert rows_of(f, "rA") == before, "a refused archive deleted rows"

        # closed, but the task is still live: refused with the task named
        set_statuses(f, "rA", run_status="done")
        rc, res, blob = cli(["archive-run", "--data", f.data, "--run", "rA"], env=f.env())
        assert rc != 0 and "non-terminal" in blob and "T1" in blob, (rc, res, blob)
        assert not os.path.exists(path), "a refused archive wrote a file"
        assert rows_of(f, "rA") == before, "a refused archive deleted rows"

        # `review` and `verifying` are LIVE for a run end: a reviewer or verifier lane is
        # working the task and its messages are still bus traffic. mk-state's ROUND_TERMINAL
        # treats them as terminal for a round RESET; a closure that archive'd under them
        # would delete the messages of a lane that is still running.
        for live_status in ("review", "verifying"):
            set_statuses(f, "rA", run_status="done", tasks=(("T1", live_status),))
            rc, res, blob = cli(["archive-run", "--data", f.data, "--run", "rA"], env=f.env())
            assert rc != 0 and "non-terminal" in blob and "T1" in blob, (live_status, rc, res, blob)
            assert not os.path.exists(path), "a refused archive wrote a file"
            assert rows_of(f, "rA") == before, "a refused archive deleted rows"

        # the other closed spelling is accepted once every task is terminal
        set_statuses(f, "rA", run_status="archived", tasks=(("T1", "merged"),))
        rc, res, blob = cli(["archive-run", "--data", f.data, "--run", "rA"], env=f.env())
        assert rc == 0 and res.get("ok") is True and res["written"] is True, (rc, res, blob)
        assert res["messages"] == len(before["msg"]), (res, len(before["msg"]))
        assert rows_of(f, "rA") == {"msg": [], "ack": [], "artifact": []}, "archived rows must be gone"
        f.stop_bus(pid)
        case("(b) active run and live tasks refused before any write; archived accepted")
        f.cleanup()

        # ---- (c) a dropped archive line fails the count check ---------------------------------
        f = _archive_fixture(tmp, "projc")
        f.add_run("rA", status="active", tasks=("T1",))
        pid, port = f.start_bus("rA")
        orch = f.mint_orch()
        for i in range(3):
            ask(f, port, orch, "rA", "T1", "ask %d" % i)
        set_statuses(f, "rA", run_status="done", tasks=(("T1", "done"),))
        before = rows_of(f, "rA")
        path = os.path.join(_paths("run_dir", f.data, "rA"), "bus.jsonl")
        run_dir = os.path.dirname(path)
        # THE SEAM: mk-bus.py loaded by absolute path, its writer step wrapped so ONE line is
        # dropped before the file is written. Nothing is reached into after the fact.
        under_test = _load_script_module("mk_bus_archive_case", bus_py)
        written = []
        orig_writer = under_test._archive_write_file

        def dropping_writer(path_, lines):
            written.append(len(lines))
            return orig_writer(path_, lines[:-1])

        under_test._archive_write_file = dropping_writer
        try:
            try:
                under_test.archive_run(f.data, "rA")
            except under_test.Fail as e:
                reason = str(e)
            else:
                raise AssertionError("archive-run (c): a file one line short of the rows was accepted")
        finally:
            under_test._archive_write_file = orig_writer
        assert "mismatch" in reason, reason
        assert written == [sum(len(before[n]) for n in ("msg", "ack", "artifact"))], written
        assert not os.path.exists(path), "the partial file survived the failed check"
        if os.path.isdir(run_dir):
            assert os.listdir(run_dir) == [], os.listdir(run_dir)
        assert rows_of(f, "rA") == before, "a failed check deleted rows"

        # the same property through the CLI, on the documented drop-line env
        env = f.env()
        env[under_test.ARCHIVE_DROP_LINE_ENV] = "2"
        rc, res, blob = cli(["archive-run", "--data", f.data, "--run", "rA"], env=env)
        assert rc != 0 and "mismatch" in blob, (rc, res, blob)
        assert not os.path.exists(path), "the CLI left a partial file behind"
        assert rows_of(f, "rA") == before, "the CLI's failed check deleted rows"
        f.stop_bus(pid)
        case("(c) dropped archive line: count check fails, nothing deleted, no partial file")
        f.cleanup()

        # ---- (d) a cursor on a deleted seq resumes at the NEXT existing seq -------------------
        f = _archive_fixture(tmp, "projd")
        f.add_run("rB", status="active", tasks=("T1",))
        f.add_run("rA", status="active", tasks=("T1",))
        pid, port = f.start_bus("rA")
        tokA, tokB = f.lane_token("rA", "T1"), f.lane_token("rB", "T1")
        orch = f.mint_orch()
        # run B first, and the LOW seqs are what a reader has ALREADY SEEN: an ask and its
        # answer, both below the cursor this case sets. The answer is addressed to the lane,
        # so a reader that reset to 0 would hand back an already-seen message.
        ask1 = ask(f, port, tokB, "rB", "T1", "run B one")
        code, ans1 = f.post(port, orch, {"run": "rB", "task": "T1", "kind": "answer",
                                         "text": "answered one", "from": "orch", "to": "T1",
                                         "reply_to": ask1["id"]})
        assert code == 200 and ans1["ok"] is True, (code, ans1)
        keep = [ask1["seq"], int(ans1["message"]["seq"])]
        # then the closed run: its seqs are the HIGHEST so far, and they are the deleted ones
        doomed = [ask(f, port, tokA, "rA", "T1", "run A one")["seq"],
                  ask(f, port, tokA, "rA", "T1", "run A two")["seq"]]
        assert doomed[0] > keep[-1], (keep, doomed)
        # a message of the live run sits ABOVE the doomed range and is still unshown: that is
        # the next existing seq every reader below must land on
        ask2 = ask(f, port, tokB, "rB", "T1", "run B two")
        next_seq = ask2["seq"]
        assert next_seq > doomed[-1], (next_seq, doomed)
        set_statuses(f, "rA", run_status="done", tasks=(("T1", "done"),))
        rc, res, blob = cli(["archive-run", "--data", f.data, "--run", "rA"], env=f.env())
        assert rc == 0 and res["written"] is True, (rc, res, blob)
        assert (res["seq_min"], res["seq_max"]) == (doomed[0], doomed[-1]), (res, doomed)
        # the bus rebuilds its run state from the store: every reader below sees the
        # post-archive rows, never the deleted ones
        f.stop_bus(pid)
        pid, port = f.start_bus("rB")
        tokB = f.lane_token("rB", "T1")
        orch = f.mint_orch()
        # a solicited answer on the live run, addressed to the lane, above the deleted range
        code, live = f.post(port, orch, {"run": "rB", "task": "T1", "kind": "answer",
                                         "text": "answered after the archive", "from": "orch",
                                         "to": "T1", "reply_to": ask2["id"]})
        assert code == 200 and live["ok"] is True, (code, live)
        live_seq = int(live["message"]["seq"])
        assert live_seq > doomed[-1], (live_seq, doomed)
        # every cursor below sits ON a deleted seq
        cursor = doomed[-1]

        # 1. the watch cursor sits on a deleted seq: the next watch prints the NEXT existing
        #    message and nothing it had already passed (a reset to 0 would replay both)
        cursor_put(f.data, "*|*", cursor)
        w = subprocess.run([sys.executable, bus_py, "watch", "--data", f.data, "--max-messages", "2"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           env=f.env(), timeout=30)
        assert w.returncode == 0, (w.returncode, w.stderr)
        seen = [ln.split()[1] for ln in w.stdout.splitlines() if ln.strip()]
        assert seen == ["m-%d" % next_seq, "m-%d" % live_seq], (seen, next_seq, live_seq)

        # 2. the SSE stream (what watch, mk-builder's listen and every lane stream use) with
        #    Last-Event-ID on a deleted seq: the events are the next existing ones, in order
        req = urllib.request.Request(
            "http://127.0.0.1:%d/events?lane=*&run=*" % port,
            headers={"Authorization": "Bearer " + orch, "Last-Event-ID": "m-%d" % cursor,
                     "Host": "127.0.0.1:%d" % port})
        sse_ids = []
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                while len(sse_ids) < 2:
                    ln = resp.readline()
                    if not ln:
                        break
                    if ln.startswith(b"id: "):
                        sse_ids.append(ln[4:].strip().decode())
        except (TimeoutError, socket.timeout, urllib.error.URLError):
            pass
        assert sse_ids == [str(next_seq), str(live_seq)], (sse_ids, next_seq, live_seq)

        # 3. the lane's /wait path (mk-builder's inbox long-poll) after the same deleted seq:
        #    the first message above the cursor for this lane, never an already-seen one
        status, body = get(port, "/wait?after=%d&timeout=2" % cursor, token=tokB, timeout=6.0)
        assert status == 200, (status, body)
        doc = json.loads(body)
        assert doc["ok"] is True and int(doc["message"]["seq"]) == live_seq, (doc, live_seq)
        assert doc["message"]["seq"] > cursor, (doc, cursor)
        f.stop_bus(pid)
        case("(d) cursor on a deleted seq resumes at the next existing seq (watch, SSE, /wait)")
        f.cleanup()

        # ---- (e) a second archive is a no-op, on a temp $HOME with the MK_* names unset --------
        f = _archive_fixture(tmp, "proje", home_env=False)
        child = f.env()
        for k in ("MK_HOME", "MATRIXKODER_HOME", "MATRIXKODER_DATA"):
            assert k not in child, "case (e) must run with %s unset" % k
        assert child.get("HOME") == os.path.join(f.tmp, "home"), child.get("HOME")
        f.add_run("rA", status="done", tasks=({"id": "T1", "status": "merged"},))
        for i in range(3):
            f.event("rA", kind="ask", task="T1", from_id="T1", to_id="orch", text="ask %d" % i)
        eid = f.event("rA", kind="report", task="T1", from_id="T1", to_id="orch", text="round:1\nreport")
        f.ack("rA", "m-%d" % eid, "T1", 2)
        f.artifact("rA", "T1", 1, "report", "the report body", event_id=eid)
        # an importer row (src set) is not bus data: it stays behind and is not archived
        imported = f.import_artifacts("rA", [
            {"src": "run-doc-rA-e", "task": None, "round": 0, "kind": "run-doc", "rev": 1,
             "text": "the run document", "created": "2026-10-12T09:00:00+00:00"}])
        assert imported, "the importer row must land"
        want = rows_of(f, "rA")
        assert want["msg"] and want["ack"] and want["artifact"], \
            {k: len(v) for k, v in want.items()}
        path = os.path.join(_paths("run_dir", f.data, "rA"), "bus.jsonl")

        rc, res, blob = cli(["archive-run", "--data", f.data, "--run", "rA"], env=child)
        assert rc == 0 and res.get("ok") is True and res["written"] is True, (rc, res, blob)
        assert res["path"] == path, (res["path"], path)
        assert res["messages"] == len(want["msg"]) and res["acks"] == len(want["ack"]), res
        assert res["artifacts"] == len(want["artifact"]), res
        assert res["sha256"] == digest_of(want), (res["sha256"], digest_of(want))
        before_bytes = open(path, "rb").read()

        # the second call: no empty second file, no error, the bytes untouched
        rc2, res2, blob2 = cli(["archive-run", "--data", f.data, "--run", "rA"], env=child)
        assert rc2 == 0 and res2.get("ok") is True, (rc2, res2, blob2)
        assert res2["written"] is False, res2
        assert (res2["messages"], res2["acks"], res2["artifacts"]) == (0, 0, 0), res2
        assert res2["path"] == path, res2
        assert open(path, "rb").read() == before_bytes, "the second archive rewrote the file"
        assert rows_of(f, "rA") == {"msg": [], "ack": [], "artifact": []}, "the second archive deleted rows"
        assert f.imported_artifact_ids("rA") == imported, "the second archive deleted importer rows"
        assert os.listdir(os.path.dirname(path)) == ["bus.jsonl"], os.listdir(os.path.dirname(path))
        case("(e) second archive is a no-op on a temp $HOME with the MK_* names unset")
        f.cleanup()

        # ---- (f) an archived run's seqs are never handed out again ----------------------------
        f = _archive_fixture(tmp, "projf")
        f.add_run("rA", status="active", tasks=("T1",))
        f.add_run("rB", status="active", tasks=("T1",))
        pid, port = f.start_bus("rA")
        orch = f.mint_orch()
        # the closed run takes the HIGHEST seqs and nothing is sent after them: its rows are
        # the top of the events table, so a plain max(id)+1 would hand them straight back out
        # and a cursor past the archived range would skip every message that landed on them
        doomed = [ask(f, port, orch, "rA", "T1", "run A one")["seq"],
                  ask(f, port, orch, "rA", "T1", "run A two")["seq"]]
        set_statuses(f, "rA", run_status="done", tasks=(("T1", "done"),))
        rc, res, blob = cli(["archive-run", "--data", f.data, "--run", "rA"], env=f.env())
        assert rc == 0 and res["written"] is True, (rc, res, blob)
        assert (res["seq_min"], res["seq_max"]) == (doomed[0], doomed[-1]), (res, doomed)
        assert rows_of(f, "rA") == {"msg": [], "ack": [], "artifact": []}, rows_of(f, "rA")
        # the live run sends next, on the real bus: the seq must sit ABOVE the archived range,
        # never inside it
        f.stop_bus(pid)
        pid, port = f.start_bus("rB")
        live = ask(f, port, f.mint_orch(), "rB", "T1", "run B after the archive")
        # never a reused seq: without the high-water guard this is doomed[0] or doomed[1],
        # and the send above is the top row of the table (the guard held it until then)
        assert live["seq"] > doomed[-1], (live["seq"], doomed)
        # one guard row pins the high-water mark; nothing that scopes by run, chan='bus' or
        # a bus kind ever sees it
        assert scalar(f, "SELECT COUNT(*) FROM events WHERE chan=?", ("seq",)) == 1
        assert scalar(f, "SELECT MAX(id) FROM events") == live["seq"]
        assert scalar(f, "SELECT COUNT(*) FROM events WHERE run=?", ("rA",)) == 0
        # the store row of the live message is that seq: the run's rows are the bus rows
        assert rows_of(f, "rB")["msg"][-1]["id"] == live["seq"], rows_of(f, "rB")
        f.stop_bus(pid)
        case("(f) no seq reuse: the live run's message lands above the archived range")
        f.cleanup()

        # ---- (g) a delete that fails after the write clears the file; the retry works -------
        f = _archive_fixture(tmp, "projg")
        f.add_run("rA", status="active", tasks=("T1",))
        pid, port = f.start_bus("rA")
        orch = f.mint_orch()
        for i in range(2):
            ask(f, port, orch, "rA", "T1", "ask %d" % i)
        set_statuses(f, "rA", run_status="done", tasks=(("T1", "done"),))
        before = rows_of(f, "rA")
        path = os.path.join(_paths("run_dir", f.data, "rA"), "bus.jsonl")
        # THE SEAM again: a message of the run landing between the count check and the delete.
        # The transaction re-check must refuse, and the archive must not leave its file on
        # disk with the rows still there — that combination refuses every later attempt.
        under_test = _load_script_module("mk_bus_archive_case_g", bus_py)
        orig_writer = under_test._archive_write_file
        late_text = "a late answer that arrived after the count check"

        def late_message_writer(path_, lines):
            orig_writer(path_, lines)
            f.event("rA", kind="info", task="T1", from_id="T1", to_id="orch", text=late_text)

        under_test._archive_write_file = late_message_writer
        try:
            try:
                under_test.archive_run(f.data, "rA")
            except under_test.Fail as e:
                reason = str(e)
            else:
                raise AssertionError("archive-run (g): a late message was archived away")
        finally:
            under_test._archive_write_file = orig_writer
        assert "changed" in reason, reason
        assert not os.path.exists(path), "a failed delete left the archive file behind"
        after = rows_of(f, "rA")
        assert len(after["msg"]) == len(before["msg"]) + 1, (len(after["msg"]), len(before["msg"]))
        assert after["msg"][-1]["text"] == late_text, after["msg"][-1]
        assert after["ack"] == before["ack"] and after["artifact"] == before["artifact"], after
        # with the file cleared, the retry archives the run whole, the late message included
        rc, res, blob = cli(["archive-run", "--data", f.data, "--run", "rA"], env=f.env())
        assert rc == 0 and res["written"] is True, (rc, res, blob)
        assert res["messages"] == len(after["msg"]), (res, after["msg"])
        assert rows_of(f, "rA") == {"msg": [], "ack": [], "artifact": []}, rows_of(f, "rA")
        f.stop_bus(pid)
        case("(g) failed delete clears the file; the retry archives the run whole")
        f.cleanup()

        # ---- (h) archive-run on a MIGRATED project named by its LEGACY data dir ---------------
        # Z4 add-on: the switch copies the store to the C2 tree and sets projects.db_path,
        # and a post-switch write closes the run there — so the legacy <data>/mk.db still
        # says `active` while the C2 store says `archived`. archive-run must read the C2
        # store and write the archive into the C2 PLAN FOLDER, not the legacy runs dir, and
        # must never refuse with the legacy store's status.
        f = _archive_fixture(tmp, "projh")
        f.add_run("rA", status="active", tasks=("T1",))
        pid, port = f.start_bus("rA")
        orch = f.mint_orch()
        tok = f.lane_token("rA", "T1")
        askmsg = ask(f, port, tok, "rA", "T1", "which fixture?")
        code, ans = f.post(port, orch, {"run": "rA", "task": "T1", "kind": "answer",
                                        "text": "the schema one", "from": "orch", "to": "T1",
                                        "reply_to": askmsg["id"]})
        assert code == 200 and ans["ok"] is True, (code, ans)
        code, acked = f.post(port, tok, {"run": "rA", "task": "T1", "kind": "ack", "text": "",
                                         "from": "T1", "to": "orch",
                                         "reply_to": ans["message"]["id"], "ack_level": 3})
        assert code == 200 and acked.get("ack_recorded") is True, (code, acked)
        code, rep = f.post(port, tok, {"run": "rA", "task": "T1", "kind": "report",
                                       "text": "round:1\nthe report body", "from": "T1",
                                       "to": "orch"})
        assert code == 200 and rep.get("artifact", {}).get("rev") == 1, (code, rep)
        # the task is terminal before the switch (a copied task row); the RUN status stays
        # active in the legacy store and is closed only in the C2 one after the switch
        set_statuses(f, "rA", run_status="active", tasks=(("T1", "done"),))
        # the inbox is drained, so the switch can quiesce
        f.stop_bus(pid)
        f.migrate()  # the real mk-migrate --switch: db_path set, the store copied to C2
        # a write that lands ONLY in the new store: the run is closed there, while the legacy
        # copy the switch left behind still says active
        set_statuses(f, "rA", run_status="archived")
        # the C2 store the archive must read, addressed by shape: the fixture's own reader
        # (`folder()`/`store_path()`) follows mkpaths' data-dir form, which the shadow row
        # below breaks — that is the bug this case pins, so it is never used as the oracle.
        c2_dir = os.path.join(f.paths()["home"], "projects", f.slug)
        c2_store = StoreAdapter(c2_dir)

        def rows_of_c2(run_id):
            msgs = c2_store.store_select(
                "SELECT id, run, task, t, chan, kind, from_id, to_id, reply_to, text,"
                " artifact_id, meta_json FROM events WHERE chan=? AND run=? ORDER BY id",
                ("bus", run_id))
            acks = c2_store.store_select(
                "SELECT run, msg, lane, level, t FROM acks WHERE run=? ORDER BY msg, lane",
                (run_id,))
            arts = c2_store.store_select(
                "SELECT id, run, task, round, kind, rev, text, created, src FROM artifacts"
                " WHERE run=? AND id IN (SELECT artifact_id FROM events"
                " WHERE chan=? AND run=? AND artifact_id IS NOT NULL) ORDER BY id",
                (run_id, "bus", run_id))
            return {"msg": msgs, "ack": acks, "artifact": arts}

        want_h = rows_of_c2("rA")
        assert want_h["msg"] and want_h["ack"] and want_h["artifact"], \
            {k: len(v) for k, v in want_h.items()}
        # the L9 shadow row: the bus's project_slug_for registered a derived slug for this
        # data dir when it found no row (db_path NULL), and it still names <data>. A reader
        # that takes the first root match archives the LEGACY store.
        con_root = f.mod.open_root(f.paths()["home"])
        try:
            con_root.execute(
                "INSERT OR IGNORE INTO projects(slug, name, repo, data_dir, mode, created,"
                " last_activity) VALUES (?,?,?,?,?,?,?)",
                ("p" + __import__("hashlib").sha256(
                    os.path.realpath(f.data).encode("utf-8")).hexdigest()[:16],
                 "shadow", "", os.path.realpath(f.data), "managed",
                 "2026-10-12T09:00:00+00:00", "2026-10-12T09:00:00+00:00"))
            con_root.commit()
            shadowed = [r[0] for r in con_root.execute(
                "SELECT slug FROM projects WHERE data_dir=? ORDER BY rowid",
                (os.path.realpath(f.data),))]
        finally:
            con_root.close()
        assert len(shadowed) >= 2 and shadowed[0].startswith("p"), shadowed
        # the legacy store still says active — the trap this case pins
        legacy_rows = StoreAdapter(f.data).store_select(
            "SELECT status FROM runs WHERE id=?", ("rA",))
        assert legacy_rows and legacy_rows[0]["status"] == "active", legacy_rows
        want_path_h = os.path.join(c2_dir, "plans", f.plan, "runs", "rA", "bus.jsonl")
        rc, res, blob = cli(["archive-run", "--data", f.data, "--run", "rA"], env=f.env())
        assert rc == 0 and res.get("ok") is True and res["written"] is True, (rc, res, blob)
        assert res["path"] == want_path_h, (res["path"], want_path_h)
        assert os.path.isfile(want_path_h), want_path_h
        assert res["messages"] == len(want_h["msg"]) and res["artifacts"] == len(want_h["artifact"]), res
        assert res["acks"] == len(want_h["ack"]), res
        assert rows_of_c2("rA") == {"msg": [], "ack": [], "artifact": []}, rows_of_c2("rA")
        assert not os.path.exists(os.path.join(f.data, "runs", "rA", "bus.jsonl")), \
            "the archive must not land in the legacy runs dir"
        case("(h) archive-run reads the migrated store, not the shadowed legacy one")
        f.cleanup()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _carry_root_self_test():
    """L10.G: carry-root on a temp home. A root-only token and lane file land under bus/ and
    resolve through the production readers; an entry in both places keeps the bus/ value;
    --dry-run changes nothing; a running root bus refuses; the root files end in the named
    backup, moved not deleted; a second run is a no-op."""
    tmp = tempfile.mkdtemp(prefix="mk-bus-carry-")
    try:
        home = os.path.join(tmp, "home")
        bus = os.path.join(home, "bus")
        lane = os.path.join(tmp, "lane")
        os.makedirs(lane)
        digest = hashlib.sha256(os.path.realpath(lane).encode("utf-8")).hexdigest()
        both = hashlib.sha256(b"both").hexdigest()
        write_json(os.path.join(home, "bus-tokens.json"), {
            "root-only": {"project": "pa", "role": "lane", "lane": lane},
            "both": {"project": "old"}}, mode=0o600)
        open(os.path.join(home, "bus-tokens.lock"), "w").close()
        write_json(os.path.join(home, "bus-lanes", digest + ".json"), {"token": "root-only"}, mode=0o600)
        write_json(os.path.join(home, "bus-lanes", both + ".json"), {"token": "old"}, mode=0o600)
        write_json(os.path.join(bus, "bus-tokens.json"), {"both": {"project": "new"}, "live": {"project": "pb"}})
        write_json(os.path.join(bus, "lanes", both + ".json"), {"token": "new"}, mode=0o600)

        def snap():
            return sorted((os.path.join(d, f), open(os.path.join(d, f), "rb").read())
                          for d, _, fs in os.walk(home) for f in fs)

        def carry(*extra, check=True):
            env = dict(os.environ, MK_HOME=home, MATRIXKODER_HOME=home)
            env.pop("MK_BUS_DIR", None)
            p = subprocess.run([sys.executable, os.path.abspath(__file__), "carry-root", *extra],
                               capture_output=True, text=True, env=env)
            doc = json.loads(p.stdout.strip().splitlines()[-1])
            assert (p.returncode == 0) == check, f"carry-root {extra}: rc {p.returncode} {doc}"
            return doc

        before = snap()
        dry = carry("--dry-run")
        assert dry["dry_run"] and snap() == before, "carry-root --dry-run wrote something"
        assert dry["tokens"]["carried"] == 1 and dry["tokens"]["bus_wins"] == 1, dry["tokens"]
        assert dry["lanes"]["carried"] == 1 and dry["lanes"]["bus_wins"] == 1, dry["lanes"]
        assert len(dry["moves"]) == 3 and dry["tokens"]["carried_projects"] == {"pa": 1}, dry
        # a bus still running against the root files: refused, nothing written
        write_json(os.path.join(home, "bus.json"), {"pid": os.getpid()})
        busy = snap()
        refused = carry(check=False)
        assert "still running" in refused["error"] and snap() == busy, refused
        os.unlink(os.path.join(home, "bus.json"))
        real = carry()
        assert real["tokens"]["carried"] == 1 and real["lanes"]["carried"] == 1, real
        merged = read_json(os.path.join(bus, "bus-tokens.json"))
        assert merged["root-only"]["project"] == "pa" and merged["live"] == {"project": "pb"}, "merge lost a token"
        assert merged["both"] == {"project": "new"}, "an entry in both places must keep the bus/ value"
        assert read_json(os.path.join(bus, "lanes", both + ".json")) == {"token": "new"}, "bus/ lane file overwritten"
        # the production readers answer from bus/ now, with the root files gone
        env_keys = ("MK_HOME", "MATRIXKODER_HOME", "MK_BUS_DIR", "MK_BUS_TOKEN_FILE")
        saved = {k: os.environ.pop(k, None) for k in env_keys}
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = home
        try:
            tok_file = lane_token_path(lane)
            assert tok_file == os.path.join(bus, "lanes", digest + ".json"), tok_file
            assert read_json(tok_file) == {"token": "root-only"}, "carried lane token does not resolve"
            assert stat.S_IMODE(os.stat(tok_file).st_mode) == 0o600, "carried lane file is not 0600"
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
        names = sorted(os.listdir(home))
        assert "bus-tokens.json" not in names and "bus-lanes" not in names and "bus-tokens.lock" not in names, names
        kept = os.path.join(real["backup"])
        assert os.path.basename(kept).startswith("bus-root-") and os.path.dirname(kept) == os.path.join(home, "backups")
        assert read_json(os.path.join(kept, "bus-tokens.json"))["both"] == {"project": "old"}, "backup lost the old file"
        assert os.path.isfile(os.path.join(kept, "bus-lanes", digest + ".json")) and os.path.isfile(os.path.join(kept, "bus-tokens.lock"))
        again = carry()
        assert again["moves"] == [] and again["tokens"]["root"] == 0 and again["backup"] is None, again
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def self_test():
    tmp = tempfile.mkdtemp(prefix="mk-bus-test-")
    pids = []
    # One temp MK home under both home names (C16): children that read $MK_HOME
    # or $MATRIXKODER_HOME land here, never in ~/.matrixkoder. $MK_BUS_DIR stays
    # unset: a file-mode bus keeps bus.json in its data dir.
    st_home = os.path.join(tmp, "home")
    os.makedirs(st_home, exist_ok=True)
    st_saved = {k: os.environ.get(k) for k in ("MK_HOME", "MATRIXKODER_HOME", "MK_SELFTEST")}
    os.environ["MK_HOME"] = st_home
    os.environ["MATRIXKODER_HOME"] = st_home
    os.environ["MK_SELFTEST"] = "1"

    def cli(args_list, check=True):
        cmd = [sys.executable, os.path.abspath(__file__)] + args_list
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and p.returncode != 0:
            raise AssertionError(f"CLI failed (rc={p.returncode}): {' '.join(cmd)}\nstderr: {p.stderr}\nstdout: {p.stdout}")
        lines = [line.strip() for line in p.stdout.strip().splitlines() if line.strip()]
        last_json = json.loads(lines[-1]) if lines else {}
        return p.returncode, last_json, p.stderr

    def poll_until(fn, timeout=10.0, step=0.05):
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = fn()
            if res:
                return res
            time.sleep(step)
        raise AssertionError(f"poll_until timed out after {timeout}s")

    def wait_pid_dead(popen, timeout=10.0):
        # Poll Popen.poll(): it reaps the child, so a self-exited serve is reported
        # dead. os.kill(pid, 0) alone would keep succeeding on the zombie.
        deadline = time.time() + timeout
        while time.time() < deadline:
            if popen.poll() is not None:
                return popen.returncode
            time.sleep(0.05)
        raise AssertionError(f"pid {popen.pid} still alive after {timeout}s")

    # B31: the summary counts the checks that actually ran — never a literal.
    checks = []

    def done(name):
        checks.append(name)

    try:
        data = os.path.join(tmp, "data")
        lane1 = os.path.join(tmp, "lane1")
        os.makedirs(data, exist_ok=True)
        os.makedirs(lane1, exist_ok=True)

        # 1. ensure twice -> one process (second returns started: false, same pid)
        rc, res1, _ = cli(["ensure", "--data", data, "--run", "r1"])
        assert rc == 0 and res1["ok"] is True and res1["started"] is True, res1
        pid1 = res1["pid"]
        port1 = res1["port"]
        pids.append(pid1)
        assert pid1 > 0 and port1 > 0

        # Health endpoint unauthenticated probe
        req_health = urllib.request.Request(f"http://127.0.0.1:{port1}/health", headers={"Host": f"127.0.0.1:{port1}"})
        with urllib.request.urlopen(req_health, timeout=2.0) as resp:
            h_data = json.loads(resp.read().decode("utf-8"))
            assert h_data["ok"] is True and h_data["status"] == "healthy" and h_data["pid"] == pid1
            assert "uptime_sec" in h_data

        # Ensure idempotence
        rc, res2, _ = cli(["ensure", "--data", data, "--run", "r1"])
        assert rc == 0 and res2["ok"] is True and res2["started"] is False, res2
        assert res2["pid"] == pid1 and res2["port"] == port1
        done("ensure idempotence + /health")

        # 1a. /health names its data dir (LOW 1 support): ensure must be able
        # to tell OUR bus from a reused pid serving a different data dir.
        req_hd = urllib.request.Request(f"http://127.0.0.1:{port1}/health", headers={"Host": f"127.0.0.1:{port1}"})
        with urllib.request.urlopen(req_hd, timeout=2.0) as resp:
            hd = json.loads(resp.read().decode("utf-8"))
            assert hd.get("data") == data, hd
        done("health reports its data dir")

        # 1b. Concurrent ensure race (LOW finding 9): N callers serialize on
        # <data>/bus-ensure.lock; exactly one reports started:true, the rest
        # started:false, all agree on the same pid/port.
        data_race = os.path.join(tmp, "data_race")
        os.makedirs(data_race, exist_ok=True)
        race_procs = [
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "ensure", "--data", data_race, "--run", "rr"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            for _ in range(4)
        ]
        race_results = []
        for rp in race_procs:
            o, e = rp.communicate(timeout=30)
            assert rp.returncode == 0, f"concurrent ensure failed rc={rp.returncode}: {e}"
            race_results.append(json.loads(o.strip().splitlines()[-1]))
        pids.append(race_results[0]["pid"])
        started_count = sum(1 for r in race_results if r["started"] is True)
        assert started_count == 1, f"expected exactly 1 started:true, got {started_count}: {race_results}"
        assert all(r["pid"] == race_results[0]["pid"] for r in race_results), race_results
        assert all(r["port"] == race_results[0]["port"] for r in race_results), race_results
        done("concurrent ensure race")

        # 2. Token generation and state file permissions 0600
        rc, res_tok, _ = cli(["token", "--lane", lane1, "--task", "T1", "--run", "r1", "--data", data])
        assert rc == 0 and res_tok["ok"] is True, res_tok
        token1 = res_tok["token"]
        lane_bus_json = os.path.join(lane1, ".matrixkoder-bus.json")
        assert os.path.exists(lane_bus_json)
        assert (os.stat(lane_bus_json).st_mode & 0o777) == 0o600
        lane_payload = read_json(lane_bus_json, {})
        assert lane_payload.get("v") == 1, f"B24 lane file must carry v:1, got {lane_payload}"
        assert lane_payload.get("task") == "T1" and lane_payload.get("run") == "r1"

        # Reserved identity 'orch' is refused for --task (review LOW 2)
        rc, res_orch, _ = cli(["token", "--lane", lane1, "--task", "orch", "--run", "r1", "--data", data], check=False)
        assert rc != 0 and res_orch.get("error") == "task id 'orch' is reserved", res_orch

        # Assert server state files are 0600
        for fname in ("bus.json", "bus-tokens.json", "bus.lock", "bus-ensure.lock", "bus-tokens.lock"):
            fpath = os.path.join(data, fname)
            assert os.path.exists(fpath), f"{fname} missing"
            mode = os.stat(fpath).st_mode & 0o777
            assert mode == 0o600, f"{fname} has mode {oct(mode)}, expected 0600"
        done("token minting + 0600 perms + v:1 lane file")

        # 3. Path traversal security checks (HIGH finding 1): run/task with '..' or '/' -> 400
        for bad_id in ("../../evil", "foo/bar", "evil..run", "bad\0id", ""):
            req_trav = urllib.request.Request(
                f"http://127.0.0.1:{port1}/send",
                data=json.dumps({"run": bad_id, "kind": "ask", "text": "probe"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
                method="POST"
            )
            try:
                with urllib.request.urlopen(req_trav):
                    raise AssertionError(f"Expected 400 for path traversal id {bad_id}")
            except urllib.error.HTTPError as e:
                assert e.code == 400, f"Expected 400 for {bad_id}, got {e.code}"
        assert not os.path.exists(os.path.join(tmp, "evil")), "Directory created outside data dir!"
        done("path traversal rejection")

        # 4. Token scoping security checks (HIGH finding 2): lane token restrictions
        # (a) Lane token cannot send answer -> 403
        req_ans_lane = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            data=json.dumps({"run": "r1", "task": "T1", "kind": "answer", "text": "self answer", "reply_to": "m-1"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_ans_lane):
                raise AssertionError("Expected 403 for lane token sending answer")
        except urllib.error.HTTPError as e:
            assert e.code == 403, f"Expected 403, got {e.code}"

        # (b) Lane token cannot send to another lane (to != "orch") -> 403
        req_to_lane = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            data=json.dumps({"run": "r1", "task": "T1", "to": "T2", "kind": "info", "text": "peer"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_to_lane):
                raise AssertionError("Expected 403 for lane token sending to peer")
        except urllib.error.HTTPError as e:
            assert e.code == 403, f"Expected 403, got {e.code}"

        # (c) Lane token cannot override run/task/from -> 403
        req_forged = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            data=json.dumps({"run": "r2", "task": "T1", "kind": "ask", "text": "forged run"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_forged):
                raise AssertionError("Expected 403 for lane token overriding run")
        except urllib.error.HTTPError as e:
            assert e.code == 403, f"Expected 403, got {e.code}"

        # (d) Lane token cannot read /pending -> 403
        req_pend_lane = urllib.request.Request(
            f"http://127.0.0.1:{port1}/pending",
            headers={"Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"}
        )
        try:
            with urllib.request.urlopen(req_pend_lane):
                raise AssertionError("Expected 403 for lane token reading /pending")
        except urllib.error.HTTPError as e:
            assert e.code == 403, f"Expected 403, got {e.code}"

        # (e) Lane token cannot stream or long-poll another lane or run -> 403
        for scope_url in (
            f"/events?lane=T2",
            f"/events?run=r2",
            f"/wait?lane=T2&timeout=1",
            f"/wait?run=r2&timeout=1",
        ):
            req_scope = urllib.request.Request(
                f"http://127.0.0.1:{port1}{scope_url}",
                headers={"Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"}
            )
            try:
                with urllib.request.urlopen(req_scope, timeout=5.0):
                    raise AssertionError(f"Expected 403 for {scope_url}")
            except urllib.error.HTTPError as e:
                assert e.code == 403, f"Expected 403 for {scope_url}, got {e.code}"
        done("lane token scoping (send/events/wait/pending)")

        # 5. Auth validation: wrong token & absent token -> 401, bus.jsonl unchanged
        jsonl_path = os.path.join(data, "runs", "r1", "bus.jsonl")
        count_before = len(open(jsonl_path).readlines()) if os.path.exists(jsonl_path) else 0

        req_wrong = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            data=b'{"run":"r1","task":"T1","kind":"ask","text":"bad"}',
            headers={"Content-Type": "application/json", "Authorization": "Bearer bad_token", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_wrong):
                raise AssertionError("Expected 401 on bad token")
        except urllib.error.HTTPError as e:
            assert e.code == 401, f"Expected 401, got {e.code}"

        req_absent = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            data=b'{"run":"r1","task":"T1","kind":"ask","text":"bad"}',
            headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_absent):
                raise AssertionError("Expected 401 on absent token")
        except urllib.error.HTTPError as e:
            assert e.code == 401, f"Expected 401, got {e.code}"

        # Non-ASCII bearer (review LOW 1): byte comparison must yield 401,
        # never a TypeError -> 500.
        req_nonascii = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            data=b'{"run":"r1","task":"T1","kind":"ask","text":"bad"}',
            headers={"Content-Type": "application/json", "Authorization": "Bearer é", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_nonascii):
                raise AssertionError("Expected 401 on non-ASCII bearer")
        except urllib.error.HTTPError as e:
            assert e.code == 401, f"Expected 401 for non-ASCII bearer, got {e.code}"

        count_after = len(open(jsonl_path).readlines()) if os.path.exists(jsonl_path) else 0
        assert count_after == count_before, "bus.jsonl modified after 401 requests"
        done("auth 401 (wrong/absent/non-ASCII) + no writes")

        # 6. Input validation per B18 (MEDIUM finding 5)
        # Non-dict body -> 400
        req_non_dict = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            data=b'[1, 2, 3]',
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_non_dict):
                raise AssertionError("Expected 400 for non-dict body")
        except urllib.error.HTTPError as e:
            assert e.code == 400

        # Invalid kind -> 400
        req_bad_kind = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            data=b'{"kind": "bogus", "text": "hi"}',
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_bad_kind):
                raise AssertionError("Expected 400 for bad kind")
        except urllib.error.HTTPError as e:
            assert e.code == 400

        # ack kind without reply_to -> 400 (B18: reply_to required for ack)
        req_ack_no_reply = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            data=b'{"run": "r1", "kind": "ack", "text": ""}',
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_ack_no_reply):
                raise AssertionError("Expected 400 for ack without reply_to")
        except urllib.error.HTTPError as e:
            assert e.code == 400, f"Expected 400 for ack without reply_to, got {e.code}"

        # B18 strict: empty text for ask -> 400; reply_to on ask -> 400
        for bad_body in (
            b'{"run":"r1","task":"T1","kind":"ask","text":""}',
            b'{"run":"r1","task":"T1","kind":"ask","text":"hi","reply_to":"m-1"}',
            b'{"run":"r1","task":"T1","kind":"info","text":"hi","reply_to":"m-1"}',
        ):
            req_b18 = urllib.request.Request(
                f"http://127.0.0.1:{port1}/send",
                data=bad_body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
                method="POST"
            )
            try:
                with urllib.request.urlopen(req_b18):
                    raise AssertionError(f"Expected 400 for B18 violation: {bad_body!r}")
            except urllib.error.HTTPError as e:
                assert e.code == 400, f"Expected 400 for {bad_body!r}, got {e.code}"

        # B13 strict integer: bool ack_level refused on /send and /ack
        req_ack_bool = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            data=b'{"run":"r1","kind":"ack","text":"","reply_to":"m-1","ack_level":true}',
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_ack_bool):
                raise AssertionError("Expected 400 for bool ack_level via /send")
        except urllib.error.HTTPError as e:
            assert e.code == 400, f"Expected 400 for bool ack_level via /send, got {e.code}"

        req_ack_bool2 = urllib.request.Request(
            f"http://127.0.0.1:{port1}/ack",
            data=b'{"run":"r1","id":"m-1","ack_level":true}',
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_ack_bool2):
                raise AssertionError("Expected 400 for bool ack_level via /ack")
        except urllib.error.HTTPError as e:
            assert e.code == 400, f"Expected 400 for bool ack_level via /ack, got {e.code}"

        # 413 over real HTTP: body above the 64 KiB limit
        req_big = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            data=b'{"run":"r1","task":"T1","kind":"info","text":"' + b"x" * 70000 + b'"}',
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_big):
                raise AssertionError("Expected 413 for oversized body")
        except urllib.error.HTTPError as e:
            assert e.code == 413, f"Expected 413, got {e.code}"

        # B6 Host header enforcement over real HTTP
        req_bad_host = urllib.request.Request(
            f"http://127.0.0.1:{port1}/health",
            headers={"Host": "evil.com"}
        )
        try:
            with urllib.request.urlopen(req_bad_host):
                raise AssertionError("Expected 400 for external Host header")
        except urllib.error.HTTPError as e:
            assert e.code == 400, f"Expected 400 for bad Host, got {e.code}"
            assert json.loads(e.read().decode("utf-8"))["error"] == "invalid host header"

        # B7 Origin rejection over real HTTP
        req_origin = urllib.request.Request(
            f"http://127.0.0.1:{port1}/health",
            headers={"Host": f"127.0.0.1:{port1}", "Origin": "http://evil.example"}
        )
        try:
            with urllib.request.urlopen(req_origin):
                raise AssertionError("Expected 403 for Origin header")
        except urllib.error.HTTPError as e:
            assert e.code == 403, f"Expected 403 for Origin, got {e.code}"
            assert json.loads(e.read().decode("utf-8"))["error"] == "browser origins rejected"

        # B7 CORS preflight: OPTIONS -> 403 JSON (never 501 HTML)
        req_options = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            headers={"Host": f"127.0.0.1:{port1}", "Content-Type": "application/json"},
            method="OPTIONS"
        )
        try:
            with urllib.request.urlopen(req_options):
                raise AssertionError("Expected 403 for OPTIONS preflight")
        except urllib.error.HTTPError as e:
            assert e.code == 403, f"Expected 403 for OPTIONS, got {e.code}"
            opt_body = json.loads(e.read().decode("utf-8"))
            assert opt_body == {"ok": False, "error": "browser origins rejected"}, opt_body

        # /wait timeout clamp over real HTTP (B11/B18): 0.2 clamps up to 1.0
        t0_fin = time.time()
        req_fin_wait = urllib.request.Request(
            f"http://127.0.0.1:{port1}/wait?lane=T1&after=0&timeout=0.2",
            headers={"Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"}
        )
        with urllib.request.urlopen(req_fin_wait, timeout=5.0) as resp:
            fin_res = json.loads(resp.read().decode("utf-8"))
            assert fin_res["ok"] is True and fin_res["message"] is None, fin_res
        dt_fin = time.time() - t0_fin
        assert dt_fin >= 0.9, f"timeout=0.2 must clamp to 1.0 s, returned after {dt_fin:.2f}s"
        done("input validation + 413 + host/origin/OPTIONS + wait clamp")

        # 7. ask -> answer round trip (< 2s) with mk-state unblock and honest acks
        rc, send_ask, _ = cli(["send", "--run", "r1", "--task", "T1", "--kind", "ask", "--text", "how to proceed?", "--from", "T1", "--to", "orch", "--data", data])
        assert rc == 0 and send_ask["ok"] is True, send_ask
        ask_msg = send_ask["message"]
        assert ask_msg["seq"] == 1 and ask_msg["id"] == "m-1" and ask_msg["kind"] == "ask"
        assert ask_msg["ack_level"] == 1

        # Check pending (via orch CLI) — B14: response echoes the run id
        rc, pend, _ = cli(["pending", "--run", "r1", "--data", data])
        assert rc == 0 and len(pend["pending"]) == 1 and pend["pending"][0]["id"] == "m-1", pend
        assert pend.get("run") == "r1", f"B14: pending response must echo run, got {pend}"

        # Client blocking wait in thread
        wait_result = {}

        def do_wait():
            req_w = urllib.request.Request(
                f"http://127.0.0.1:{port1}/wait?lane=T1&after=1&timeout=5",
                headers={"Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"}
            )
            t_start = time.time()
            with urllib.request.urlopen(req_w, timeout=5.0) as resp:
                data_str = resp.read().decode("utf-8")
                wait_result["res"] = json.loads(data_str)
                wait_result["dt"] = time.time() - t_start

        th = threading.Thread(target=do_wait)
        th.start()
        time.sleep(0.1)

        # Orch answers
        rc, send_ans, _ = cli(["answer", "m-1", "proceed with caution", "--run", "r1", "--data", data])
        assert rc == 0 and send_ans["ok"] is True, send_ans
        assert "state_updated" in send_ans
        ans_msg = send_ans["message"]
        assert ans_msg["kind"] == "answer" and ans_msg["reply_to"] == "m-1"

        th.join(timeout=3.0)
        assert not th.is_alive(), "wait thread did not terminate"
        dt = wait_result.get("dt", 99)
        assert dt < 2.0, f"Blocking wait took {dt:.3f}s >= 2.0s"
        assert wait_result["res"]["ok"] is True
        recv_msg = wait_result["res"]["message"]
        assert recv_msg["id"] == ans_msg["id"] and recv_msg["text"] == "proceed with caution"

        # Client sends ack for answer
        req_ack = urllib.request.Request(
            f"http://127.0.0.1:{port1}/ack",
            data=json.dumps({"id": ans_msg["id"], "ack_level": 3, "run": "r1"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        with urllib.request.urlopen(req_ack, timeout=2.0) as resp:
            ack_res = json.loads(resp.read().decode("utf-8"))
        assert ack_res["ok"] is True

        # B18: ack kind with a reply_to that names no existing message -> 400
        req_ack_bad_reply = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            data=json.dumps({"run": "r1", "task": "T1", "kind": "ack", "text": "", "reply_to": "m-9999", "from": "T1", "to": "orch"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_ack_bad_reply):
                raise AssertionError("Expected 400 for ack with unknown reply_to")
        except urllib.error.HTTPError as e:
            assert e.code == 400, f"Expected 400 for ack with unknown reply_to, got {e.code}"
        done("ask->answer round trip + honest acks")

        # 7c. Z4: a store appearing mid-flight must not move a live bus's files.
        # `answer` runs mk-state, which creates <data>/mk.db while the server above
        # is still running; mkpaths then resolves this bus's token file into the MK
        # home's bus/ folder, which holds none of the tokens registered here. The bus
        # pins its file paths at serve time (the file-side twin of use_store), so the
        # lane token above keeps answering instead of 401 on a live, delivering bus.
        data_flip = os.path.join(tmp, "data_flip")
        os.makedirs(data_flip, exist_ok=True)
        rc, res_flip, _ = cli(["ensure", "--data", data_flip, "--run", "rf"])
        assert rc == 0 and res_flip["ok"] is True, res_flip
        pids.append(res_flip["pid"])
        port_flip = res_flip["port"]
        lane_flip = os.path.join(tmp, "lane_flip")
        os.makedirs(lane_flip, exist_ok=True)
        rc, res_flip_tok, _ = cli(["token", "--lane", lane_flip, "--task", "T1", "--run", "rf",
                                   "--data", data_flip])
        assert rc == 0 and res_flip_tok["ok"] is True, res_flip_tok
        token_flip = res_flip_tok["token"]
        with urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port_flip}/wait?lane=T1&after=0&timeout=1",
                headers={"Authorization": f"Bearer {token_flip}",
                         "Host": f"127.0.0.1:{port_flip}"}), timeout=5.0) as resp:
            assert json.loads(resp.read().decode("utf-8"))["ok"] is True, "flip bus: token before"
        # The flip: a project store appears under the data dir mid-flight, exactly
        # what mk-state does for this bus when a lane's ask is answered.
        with open(_load_mkpaths().db_in(data_flip), "wb") as f:
            f.write(b"")
        with urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port_flip}/wait?lane=T1&after=0&timeout=1",
                headers={"Authorization": f"Bearer {token_flip}",
                         "Host": f"127.0.0.1:{port_flip}"}), timeout=5.0) as resp:
            flip_ok = json.loads(resp.read().decode("utf-8"))["ok"]
        assert flip_ok is True, "a mid-flight store must not move the bus's token file (401)"
        # a lane ask + its ack, straight over HTTP (the CLI resolves the bus through
        # bus.json, which the flip legitimately moves): both must still answer 200
        req_flip_ask = urllib.request.Request(
            f"http://127.0.0.1:{port_flip}/send",
            data=json.dumps({"run": "rf", "task": "T1", "kind": "ask", "text": "still mine?",
                             "from": "T1", "to": "orch"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token_flip}",
                     "Host": f"127.0.0.1:{port_flip}"},
            method="POST"
        )
        with urllib.request.urlopen(req_flip_ask, timeout=5.0) as resp:
            flip_ask = json.loads(resp.read().decode("utf-8"))
        assert flip_ask["ok"] is True, flip_ask
        req_flip_ack = urllib.request.Request(
            f"http://127.0.0.1:{port_flip}/ack",
            data=json.dumps({"id": flip_ask["message"]["id"], "ack_level": 3, "run": "rf"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token_flip}",
                     "Host": f"127.0.0.1:{port_flip}"},
            method="POST"
        )
        with urllib.request.urlopen(req_flip_ack, timeout=5.0) as resp:
            flip_ack = json.loads(resp.read().decode("utf-8"))
        assert flip_ack["ok"] is True, flip_ack
        # The freshly minted half of the same review (HIGH): a token minted AFTER the
        # flip must name the LIVE bus's port. Before the fix, bus.json had already moved,
        # so this url was http://127.0.0.1:0 (no root bus alive) or the ROOT bus's port
        # — and the token itself was written where the live file-mode bus does not look,
        # so every request with it answered 401. Read by path, because that is the file a
        # lane would dial.
        rc, res_flip_tok2, _ = cli(["token", "--lane", lane_flip, "--task", "T2", "--run", "rf",
                                    "--data", data_flip])
        assert rc == 0 and res_flip_tok2["ok"] is True, res_flip_tok2
        flip_cfg2 = read_json(os.path.join(lane_flip, ".matrixkoder-bus.json"))
        assert flip_cfg2 and flip_cfg2["url"] == f"http://127.0.0.1:{port_flip}", flip_cfg2
        assert flip_cfg2["token"] == res_flip_tok2["token"], flip_cfg2
        req_flip_fresh = urllib.request.Request(
            f"http://127.0.0.1:{port_flip}/send",
            data=json.dumps({"run": "rf", "task": "T2", "kind": "ask", "text": "minted after the flip",
                             "from": "T2", "to": "orch"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {flip_cfg2['token']}",
                     "Host": f"127.0.0.1:{port_flip}"},
            method="POST"
        )
        with urllib.request.urlopen(req_flip_fresh, timeout=10.0) as resp:
            flip_fresh = json.loads(resp.read().decode("utf-8"))
        assert flip_fresh["ok"] is True, flip_fresh
        req_flip_fresh_ack = urllib.request.Request(
            f"http://127.0.0.1:{port_flip}/ack",
            data=json.dumps({"id": flip_fresh["message"]["id"], "ack_level": 3, "run": "rf"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {flip_cfg2['token']}",
                     "Host": f"127.0.0.1:{port_flip}"},
            method="POST"
        )
        with urllib.request.urlopen(req_flip_fresh_ack, timeout=10.0) as resp:
            flip_fresh_ack = json.loads(resp.read().decode("utf-8"))
        assert flip_fresh_ack["ok"] is True, flip_fresh_ack
        done("a store appearing mid-flight never moves the bus's token file (old or freshly minted)")

        # 7b. Cross-lane ack forgery refused on EVERY ack path (MEDIUM 1):
        # T1 must not ack T2's messages, via /send kind=ack AND via /ack.
        lane2 = os.path.join(tmp, "lane2")
        os.makedirs(lane2, exist_ok=True)
        rc, res_tok2, _ = cli(["token", "--lane", lane2, "--task", "T2", "--run", "r1", "--data", data])
        assert rc == 0 and res_tok2["ok"] is True, res_tok2
        token2 = res_tok2["token"]

        rc, res_ask_t2, _ = cli(["send", "--run", "r1", "--task", "T2", "--kind", "ask", "--text", "T2 question", "--from", "T2", "--to", "orch", "--data", data])
        assert rc == 0 and res_ask_t2["ok"] is True, res_ask_t2
        t2_ask_id = res_ask_t2["message"]["id"]
        t2_ask_seq = res_ask_t2["message"]["seq"]

        lines_before = len(open(jsonl_path).readlines())
        req_cross_ack = urllib.request.Request(
            f"http://127.0.0.1:{port1}/send",
            data=json.dumps({"kind": "ack", "text": "", "reply_to": t2_ask_id}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_cross_ack):
                raise AssertionError("Expected 403 for T1 acking T2's ask via /send")
        except urllib.error.HTTPError as e:
            assert e.code == 403, f"Expected 403 for cross-lane ack via /send, got {e.code}"

        req_cross_ack2 = urllib.request.Request(
            f"http://127.0.0.1:{port1}/ack",
            data=json.dumps({"id": t2_ask_id, "run": "r1"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_cross_ack2):
                raise AssertionError("Expected 403 for T1 acking T2's ask via /ack")
        except urllib.error.HTTPError as e:
            assert e.code == 403, f"Expected 403 for cross-lane ack via /ack, got {e.code}"

        assert len(open(jsonl_path).readlines()) == lines_before, "403 acks must write nothing"
        done("cross-lane ack refused on /send and /ack")

        # 7c. NaN /wait timeout over real HTTP (B11): non-finite falls back to
        # a finite 30 s wait, proven by the request staying open until a
        # matching message arrives (no error, no instant empty return).
        nan_result = {}

        def do_nan_wait():
            req_nan = urllib.request.Request(
                f"http://127.0.0.1:{port1}/wait?lane=T1&after={t2_ask_seq}&timeout=nan",
                headers={"Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"}
            )
            with urllib.request.urlopen(req_nan, timeout=10.0) as resp:
                nan_result["res"] = json.loads(resp.read().decode("utf-8"))

        th_nan = threading.Thread(target=do_nan_wait)
        th_nan.start()
        time.sleep(0.4)
        assert th_nan.is_alive(), "timeout=nan must not return instantly or error"

        rc, send_info, _ = cli(["send", "--run", "r1", "--task", "T1", "--kind", "info", "--text", "update", "--from", "orch", "--to", "T1", "--data", data])
        assert rc == 0 and send_info["ok"] is True, send_info
        info_id = send_info["message"]["id"]
        # todo 86: the CLI passes every kind to the server (dispatch sends kind task in store
        # mode); only the server decides, so a file-mode bus refuses it there, not argparse
        rc_k, res_k, err_k = cli(["send", "--run", "r1", "--task", "T1", "--kind", "task", "--text", "b",
                                  "--from", "orch", "--to", "T1", "--data", data], check=False)
        assert "invalid choice" not in err_k and rc_k != 0 and "invalid kind" in json.dumps(res_k) + err_k, (rc_k, res_k, err_k)

        th_nan.join(timeout=5.0)
        assert not th_nan.is_alive(), "nan wait thread did not terminate"
        assert nan_result["res"]["ok"] is True, nan_result
        assert nan_result["res"]["message"]["id"] == info_id, nan_result
        done("NaN /wait over real HTTP")

        # Verify bus.jsonl contains ask, answer, ack lines with typed fields
        lines = [json.loads(line.strip()) for line in open(jsonl_path).readlines() if line.strip()]
        kinds = [l["kind"] for l in lines]
        assert "ask" in kinds and "answer" in kinds and "ack" in kinds, kinds
        for l in lines:
            for k in ("seq", "id", "ts", "run", "task", "from", "to", "kind", "reply_to", "text", "ack_level"):
                assert k in l, f"Field {k} missing in line {l}"
            assert l["ts"].endswith("Z"), f"Timestamp not UTC Z: {l['ts']}"

        # LOW 7: the message log is 0600 and the runs dirs are 0700
        assert (os.stat(jsonl_path).st_mode & 0o777) == 0o600, "bus.jsonl must be 0600"
        assert (os.stat(os.path.join(data, "runs")).st_mode & 0o777) == 0o700, "runs/ must be 0700"
        assert (os.stat(os.path.join(data, "runs", "r1")).st_mode & 0o777) == 0o700, "runs/r1 must be 0700"
        done("bus.jsonl fields, UTC Z, 0600/0700 modes")

        # 8. Server restart and replay via Last-Event-ID (the 7c info message
        # addressed to T1 gives the lane something to replay with seq > 2)
        stop_process(pid1)
        assert not is_pid_alive(pid1)

        rc, res_restart, _ = cli(["ensure", "--data", data, "--run", "r1"])
        assert rc == 0 and res_restart["ok"] is True and res_restart["started"] is True
        pid2 = res_restart["pid"]
        port2 = res_restart["port"]
        pids.append(pid2)

        req_sse = urllib.request.Request(
            f"http://127.0.0.1:{port2}/events?lane=T1",
            headers={"Authorization": f"Bearer {token1}", "Last-Event-ID": "2", "Host": f"127.0.0.1:{port2}"}
        )
        with urllib.request.urlopen(req_sse, timeout=3.0) as resp:
            assert resp.headers.get("Content-Type") == "text/event-stream; charset=utf-8", \
                f"Wrong SSE Content-Type: {resp.headers.get('Content-Type')}"
            first_line = resp.readline().decode("utf-8")
            assert first_line.startswith("id: "), f"Expected id: ..., got {first_line!r}"
            ev_id = int(first_line.split(":")[1].strip())
            assert ev_id > 2, f"Expected event id > 2, got {ev_id}"
        done("SSE replay via Last-Event-ID + headers")

        # 8b. Lane ack via /send records an ack, never a message (MEDIUM 1):
        # the ack record is absent from messages/by_id live, and live state
        # equals replayed state after a restart.
        req_lane_ack = urllib.request.Request(
            f"http://127.0.0.1:{port2}/send",
            data=json.dumps({"kind": "ack", "text": "", "reply_to": ans_msg["id"]}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port2}"},
            method="POST"
        )
        with urllib.request.urlopen(req_lane_ack, timeout=2.0) as resp:
            lane_ack_res = json.loads(resp.read().decode("utf-8"))
        assert lane_ack_res["ok"] is True and lane_ack_res["ack_recorded"] is True, lane_ack_res
        assert lane_ack_res["id"] == ans_msg["id"], lane_ack_res

        last_line = json.loads(open(jsonl_path).readlines()[-1].strip())
        assert last_line["kind"] == "ack" and last_line["reply_to"] == ans_msg["id"], last_line
        ack_rec_id = last_line["id"]

        req_addr_ack = urllib.request.Request(
            f"http://127.0.0.1:{port2}/send",
            data=json.dumps({"kind": "ack", "text": "", "reply_to": ack_rec_id}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port2}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_addr_ack):
                raise AssertionError("ack record id must not be addressable live")
        except urllib.error.HTTPError as e:
            assert e.code == 400, f"Expected 400 addressing ack record live, got {e.code}"

        req_wait_live = urllib.request.Request(
            f"http://127.0.0.1:{port2}/wait?lane=T1&after=0&timeout=1",
            headers={"Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port2}"}
        )
        with urllib.request.urlopen(req_wait_live, timeout=5.0) as resp:
            wait_live = json.loads(resp.read().decode("utf-8"))
        assert wait_live["message"]["id"] == ans_msg["id"], wait_live

        rc, pend_live, _ = cli(["pending", "--run", "r1", "--data", data])
        assert rc == 0 and [p["id"] for p in pend_live["pending"]] == [t2_ask_id], pend_live

        # Restart: replayed state must equal the live state observed above
        stop_process(pid2)
        assert not is_pid_alive(pid2)
        rc, res_restart2, _ = cli(["ensure", "--data", data, "--run", "r1"])
        assert rc == 0 and res_restart2["ok"] is True and res_restart2["started"] is True
        pid3 = res_restart2["pid"]
        port3 = res_restart2["port"]
        pids.append(pid3)

        req_addr_ack2 = urllib.request.Request(
            f"http://127.0.0.1:{port3}/send",
            data=json.dumps({"kind": "ack", "text": "", "reply_to": ack_rec_id}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port3}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_addr_ack2):
                raise AssertionError("ack record id must not be addressable after replay")
        except urllib.error.HTTPError as e:
            assert e.code == 400, f"Expected 400 addressing ack record after replay, got {e.code}"

        req_wait_replay = urllib.request.Request(
            f"http://127.0.0.1:{port3}/wait?lane=T1&after=0&timeout=1",
            headers={"Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port3}"}
        )
        with urllib.request.urlopen(req_wait_replay, timeout=5.0) as resp:
            wait_replay = json.loads(resp.read().decode("utf-8"))
        assert wait_replay["message"]["id"] == wait_live["message"]["id"], wait_replay

        rc, pend_replay, _ = cli(["pending", "--run", "r1", "--data", data])
        assert rc == 0 and [p["id"] for p in pend_replay["pending"]] == [p["id"] for p in pend_live["pending"]], pend_replay
        done("lane ack via /send is a record, live == replayed")

        stop_process(pid3)
        assert not is_pid_alive(pid3)

        # 8c. Replay tolerance for a hand-edited line whose id is not a str
        # (LOW 3): the line is skipped (id AND seq) and the run still serves
        # its REAL messages instead of TypeError-500ing every request.
        poison = '{"seq": 99, "id": [1], "kind": "info", "run": "r1", "task": "T1", "from": "x", "to": "T1", "text": "poison", "reply_to": null, "ack_level": 1, "ts": "2026-01-01T00:00:00Z"}\n'
        with open(jsonl_path, "a") as f:
            f.write(poison)
        rc, res_ens_poison, _ = cli(["ensure", "--data", data, "--run", "r1"])
        assert rc == 0 and res_ens_poison["ok"] is True and res_ens_poison["started"] is True, res_ens_poison
        pids.append(res_ens_poison["pid"])
        port_p = res_ens_poison["port"]
        req_wp = urllib.request.Request(
            f"http://127.0.0.1:{port_p}/wait?lane=T1&after=0&timeout=1",
            headers={"Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port_p}"}
        )
        with urllib.request.urlopen(req_wp, timeout=5.0) as resp:
            wp = json.loads(resp.read().decode("utf-8"))
        assert wp["ok"] is True and wp["message"]["id"] == ans_msg["id"], wp
        rc, pend_p, _ = cli(["pending", "--run", "r1", "--data", data])
        assert rc == 0 and [p["id"] for p in pend_p["pending"]] == [t2_ask_id], pend_p
        stop_process(res_ens_poison["pid"])
        assert not is_pid_alive(res_ens_poison["pid"])
        # Restore the log so later sections replay a pristine r1.
        lines_p = open(jsonl_path).readlines()
        assert lines_p[-1] == poison, "poison line must still be the last line"
        with open(jsonl_path, "w") as f:
            f.writelines(lines_p[:-1])
        done("replay skips non-str id lines")

        # 9. Stale owner_heartbeat and missing bus.json reaper (LOW finding 9)
        env_hb = os.environ.copy()
        env_hb["MK_BUS_HEARTBEAT_CHECK_SEC"] = "0.05"
        env_hb["MK_BUS_HEARTBEAT_TIMEOUT_SEC"] = "600.0"  # Large timeout initially so we control trigger
        p_hb = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "serve", "--data", data, "--run", "r1"],
            env=env_hb,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        pids.append(p_hb.pid)

        bus_json_path = os.path.join(data, "bus.json")
        poll_until(lambda: os.path.exists(bus_json_path))

        # Write stale heartbeat
        bdata = read_json(bus_json_path)
        bdata["owner_heartbeat"] = time.time() - 3600.0
        write_json(bus_json_path, bdata, mode=0o600)

        wait_pid_dead(p_hb)
        # The monitor removes bus.json before triggering shutdown; poll for the
        # removal too so a slow remove under load cannot flake the assertion.
        poll_until(lambda: not os.path.exists(bus_json_path))

        # Missing bus.json test
        p_hb2 = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "serve", "--data", data, "--run", "r1"],
            env=env_hb,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        pids.append(p_hb2.pid)
        poll_until(lambda: os.path.exists(bus_json_path))
        os.remove(bus_json_path)
        wait_pid_dead(p_hb2)
        done("stale heartbeat + missing bus.json reap")

        # 10. Client error mapping (MEDIUM finding 7): 401/403/400 map to HTTP status; only down is "bus down"
        # Server down test
        rc, res_down, err = cli(["send", "--run", "r1", "--task", "T1", "--kind", "ask", "--text", "hi", "--data", data], check=False)
        assert rc != 0
        assert res_down == {"ok": False, "error": "bus down"}, res_down
        done("server down -> bus down")

        # 10b. ensure vs a live but unresponsive server (LOW 5): a failed
        # health probe on a LIVE pid must not delete bus.json or respawn a
        # second serve; the bus recovers once the server responds again.
        data_slow = os.path.join(tmp, "data_slow")
        os.makedirs(data_slow, exist_ok=True)
        rc, res_slow, _ = cli(["ensure", "--data", data_slow, "--run", "rs"])
        assert rc == 0 and res_slow["ok"] is True and res_slow["started"] is True, res_slow
        pid_slow = res_slow["pid"]
        pids.append(pid_slow)

        os.kill(pid_slow, signal.SIGSTOP)
        try:
            rc, res_probe_slow, _ = cli(["ensure", "--data", data_slow, "--run", "rs"], check=False)
            assert rc != 0, "ensure must fail, not respawn, against a live unresponsive server"
            assert res_probe_slow.get("ok") is False and "not answering" in res_probe_slow.get("error", ""), res_probe_slow
            bslow = read_json(os.path.join(data_slow, "bus.json"))
            assert bslow and bslow.get("pid") == pid_slow, "bus.json must be kept while the pid is alive"
        finally:
            os.kill(pid_slow, signal.SIGCONT)

        rc, res_slow2, _ = cli(["ensure", "--data", data_slow, "--run", "rs"])
        assert rc == 0 and res_slow2["ok"] is True and res_slow2["started"] is False, res_slow2
        assert res_slow2["pid"] == pid_slow and res_slow2["port"] == res_slow["port"], res_slow2
        stop_process(pid_slow)
        done("ensure keeps live-but-unresponsive bus")

        # 10c. Stale bus.json whose pid is a live NON-listening process
        # (LOW 1): pid reuse (here a sleeper) must be told from our stopped
        # server — connection refused means nothing of ours listens there,
        # so bus.json is stale: ensure removes it and respawns a real bus.
        data_stale = os.path.join(tmp, "data_stale")
        os.makedirs(data_stale, exist_ok=True)
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        pids.append(sleeper.pid)
        write_json(os.path.join(data_stale, "bus.json"),
                   {"pid": sleeper.pid, "port": 1, "started": time.time(),
                    "owner_heartbeat": time.time(), "token": "stale"}, mode=0o600)
        rc, res_stale, _ = cli(["ensure", "--data", data_stale, "--run", "rst"])
        assert rc == 0 and res_stale["ok"] is True and res_stale["started"] is True, res_stale
        pids.append(res_stale["pid"])
        b_stale = read_json(os.path.join(data_stale, "bus.json"))
        assert b_stale["pid"] == res_stale["pid"] and b_stale["pid"] != sleeper.pid, b_stale
        req_sh = urllib.request.Request(
            f"http://127.0.0.1:{res_stale['port']}/health",
            headers={"Host": f"127.0.0.1:{res_stale['port']}"}
        )
        with urllib.request.urlopen(req_sh, timeout=2.0) as resp:
            sh = json.loads(resp.read().decode("utf-8"))
        assert sh["ok"] is True and sh["data"] == data_stale, sh
        stop_process(res_stale["pid"])
        assert not is_pid_alive(res_stale["pid"])
        stop_process(sleeper.pid)
        # The sleeper is a direct child of this process: reap it, else the
        # zombie keeps answering os.kill(pid, 0) (see wait_pid_dead).
        wait_pid_dead(sleeper)
        done("stale pid (refused) -> bus.json replaced, real respawn")

        # 10d. Orchestrator tokens retire at serve start (LOW 4): a restart
        # drops every earlier role=="orch" entry; the previous orchestrator
        # token answers 401 and exactly one orch entry remains.
        data_orch = os.path.join(tmp, "data_orch")
        os.makedirs(data_orch, exist_ok=True)
        rc, res_o1, _ = cli(["ensure", "--data", data_orch, "--run", "ro"])
        assert rc == 0 and res_o1["ok"] is True and res_o1["started"] is True, res_o1
        pids.append(res_o1["pid"])
        old_orch_tok = read_json(os.path.join(data_orch, "bus.json"))["token"]
        stop_process(res_o1["pid"])
        assert not is_pid_alive(res_o1["pid"])
        rc, res_o2, _ = cli(["ensure", "--data", data_orch, "--run", "ro"])
        assert rc == 0 and res_o2["ok"] is True and res_o2["started"] is True, res_o2
        pids.append(res_o2["pid"])
        req_old = urllib.request.Request(
            f"http://127.0.0.1:{res_o2['port']}/pending",
            headers={"Authorization": f"Bearer {old_orch_tok}", "Host": f"127.0.0.1:{res_o2['port']}"}
        )
        try:
            with urllib.request.urlopen(req_old, timeout=2.0):
                raise AssertionError("Expected 401 for the retired orch token")
        except urllib.error.HTTPError as e:
            assert e.code == 401, f"Expected 401 for retired orch token, got {e.code}"
        toks_o = read_json(os.path.join(data_orch, "bus-tokens.json"), {})
        orch_entries = [v for v in toks_o.values() if isinstance(v, dict) and v.get("role") == "orch"]
        assert len(orch_entries) == 1, orch_entries
        new_orch_tok = read_json(os.path.join(data_orch, "bus.json"))["token"]
        assert new_orch_tok != old_orch_tok
        req_new = urllib.request.Request(
            f"http://127.0.0.1:{res_o2['port']}/pending",
            headers={"Authorization": f"Bearer {new_orch_tok}", "Host": f"127.0.0.1:{res_o2['port']}"}
        )
        with urllib.request.urlopen(req_new, timeout=2.0) as resp:
            assert json.loads(resp.read().decode("utf-8"))["ok"] is True
        done("orch token retirement on restart (old 401, one left)")

        # 11. mk-state integration with state_updated report
        data_state = os.path.join(tmp, "data_state")
        os.makedirs(data_state, exist_ok=True)
        mk_state = os.path.join(HERE, "mk-state.py")

        subprocess.run([sys.executable, mk_state, "--data", data_state, "run", "init", "--id", "r2", "--feature", "f", "--branch", "b", "--force"], check=True, capture_output=True)
        subprocess.run([sys.executable, mk_state, "--data", data_state, "--run", "r2", "task", "add", "--root", "--id", "T2", "--title", "task two"], check=True, capture_output=True)
        subprocess.run([sys.executable, mk_state, "--data", data_state, "--run", "r2", "task", "set", "T2", "status=blocked"], check=True, capture_output=True)

        rc, res_ens_st, _ = cli(["ensure", "--data", data_state, "--run", "r2"])
        assert rc == 0 and res_ens_st["ok"] is True
        pids.append(res_ens_st["pid"])

        rc, res_ask2, _ = cli(["send", "--run", "r2", "--task", "T2", "--kind", "ask", "--text", "how to build", "--from", "T2", "--to", "orch", "--data", data_state])
        assert rc == 0 and res_ask2["ok"] is True
        ask2_id = res_ask2["message"]["id"]

        rc, res_ans2, _ = cli(["answer", ask2_id, "build like this", "--run", "r2", "--data", data_state])
        assert rc == 0 and res_ans2["ok"] is True
        assert res_ans2["state_updated"] is True and res_ans2["mk_state_error"] is None

        p_get2 = subprocess.run([sys.executable, mk_state, "--data", data_state, "--run", "r2", "task", "get", "T2"], stdout=subprocess.PIPE, text=True, check=True)
        assert json.loads(p_get2.stdout)["task"]["status"] == "building"
        done("mk-state integration (answer -> building)")

        # 11b. Ambiguous message id refused (MEDIUM 2): ids are per run in file mode,
        # so "m-1" exists in both runs and without --run the CLI must fail instead of
        # answering whichever run os.listdir returned first. Store mode is not a
        # counter-example: ids are project-wide there, so no id can collide, which is
        # why this case runs its own file-mode project with two hand-seeded runs.
        data_amb = os.path.join(tmp, "data_amb")
        os.makedirs(data_amb, exist_ok=True)
        for run_id, task_id in (("ar1", "A1"), ("ar2", "A2")):
            amb_run_dir = _paths("run_dir", data_amb, run_id)
            os.makedirs(amb_run_dir, exist_ok=True)
            with open(os.path.join(amb_run_dir, "bus.jsonl"), "w") as f:
                f.write(json.dumps({"seq": 1, "id": "m-1", "ts": "2026-10-15T00:00:00Z",
                                    "run": run_id, "task": task_id, "from": task_id, "to": "orch",
                                    "kind": "ask", "reply_to": None, "text": "ask " + run_id,
                                    "ack_level": 1}) + "\n")
        rc, res_ens_amb, _ = cli(["ensure", "--data", data_amb, "--run", "ar1"])
        assert rc == 0 and res_ens_amb["ok"] is True, res_ens_amb
        pids.append(res_ens_amb["pid"])

        rc, res_amb, _ = cli(["answer", "m-1", "which one?", "--data", data_amb], check=False)
        assert rc != 0, "ambiguous answer without --run must fail"
        assert res_amb == {"ok": False, "error": "ambiguous message id, pass --run"}, res_amb

        # --run disambiguates: ar2's ask is answered, ar1's stays open
        rc, res_dis, _ = cli(["answer", "m-1", "answered via run", "--run", "ar2", "--data", data_amb])
        assert rc == 0 and res_dis["ok"] is True, res_dis
        assert res_dis["message"]["reply_to"] == "m-1" and res_dis["message"]["run"] == "ar2", res_dis

        rc, res_r1, _ = cli(["answer", "m-1", "nope", "--run", "ar1", "--data", data_amb], check=False)
        assert rc == 0 and res_r1.get("ok") is True and res_r1["message"]["run"] == "ar1", res_r1

        rc, res_again, _ = cli(["answer", "m-1", "nope", "--run", "ar1", "--data", data_amb], check=False)
        assert rc != 0 and "already answered" in res_again.get("error", ""), res_again
        done("ambiguous answer refused, --run disambiguates")

        # 11c. A lane ask over real HTTP marks its task blocked (once per ask id, --by bus) via
        # the one shared mk-state helper; the reply carries state_updated/mk_state_error; the
        # answer restores building through the same helper.
        data_ask = os.path.join(tmp, "data_ask")
        os.makedirs(data_ask, exist_ok=True)
        subprocess.run([sys.executable, mk_state, "--data", data_ask, "run", "init", "--id", "r4", "--feature", "f", "--branch", "b", "--force"], check=True, capture_output=True)
        subprocess.run([sys.executable, mk_state, "--data", data_ask, "--run", "r4", "task", "add", "--root", "--id", "T4", "--title", "ask lane"], check=True, capture_output=True)
        subprocess.run([sys.executable, mk_state, "--data", data_ask, "--run", "r4", "task", "set", "T4", "status=building"], check=True, capture_output=True)

        rc, res_ens_ask, _ = cli(["ensure", "--data", data_ask, "--run", "r4"])
        assert rc == 0 and res_ens_ask["ok"] is True, res_ens_ask
        pids.append(res_ens_ask["pid"])
        port_ask = res_ens_ask["port"]

        lane_ask = os.path.join(tmp, "lane_ask")
        os.makedirs(lane_ask, exist_ok=True)
        rc, res_tok_ask, _ = cli(["token", "--lane", lane_ask, "--task", "T4", "--run", "r4", "--data", data_ask])
        assert rc == 0 and res_tok_ask["ok"] is True, res_tok_ask
        tok_ask = res_tok_ask["token"]

        def post_lane_ask(text):
            req_ask = urllib.request.Request(
                f"http://127.0.0.1:{port_ask}/send",
                data=json.dumps({"kind": "ask", "text": text}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok_ask}", "Host": f"127.0.0.1:{port_ask}"},
                method="POST"
            )
            with urllib.request.urlopen(req_ask, timeout=5.0) as resp:
                return json.loads(resp.read().decode("utf-8"))

        def r4_task():
            """T4's store row: the status now, plus prev_status — the status the last move
            came from. A move into an attention status keeps the one it replaced (C4), so
            `building` here IS that transition's `from`."""
            rows = StoreAdapter(data_ask).store_select(
                "SELECT status, prev_status FROM tasks WHERE run=? AND id=?", ("r4", "T4"))
            return rows[0] if rows else {}

        def r4_status():
            """T4's status from the store's tasks table (C4). mkdb.transition is the only
            writer of it (C10); L4 stopped writing runs/<run>/run.json, so the file read this
            case used to do now returns None."""
            return r4_task().get("status")

        def r4_bus_transitions():
            """The moves the BUS made on this task, from the store's own events rows.

            A store-mode task move is mkdb.transition: it writes one
            events(chan='state', kind='transition', to_id=<to>, text=<note>) row and leaves
            the status it came from in tasks.prev_status. The note of a lane ask is
            `bus ask <id>: <text>`, so these rows are the store-mode twin of the old
            events.jsonl `transition` + `by == 'bus'` view — the same moves, the same note,
            the same order, read from the only source of truth."""
            return [r for r in StoreAdapter(data_ask).store_select(
                "SELECT task, to_id, text, t FROM events"
                " WHERE chan=? AND kind=? AND run=? AND text LIKE ? ORDER BY id",
                ("state", "transition", "r4", "bus ask %"))]

        ask1 = post_lane_ask("which fixture should I load?")
        assert ask1["ok"] is True and ask1["state_updated"] is True and ask1["mk_state_error"] is None, ask1
        ask1_id = ask1["message"]["id"]
        assert r4_status() == "blocked"
        tr1 = r4_bus_transitions()
        assert len(tr1) == 1 and tr1[0]["text"].startswith(f"bus ask {ask1_id}: which fixture"), tr1
        assert tr1[0]["to_id"] == "blocked" and r4_task()["prev_status"] == "building", tr1

        rc, res_ans_ask, _ = cli(["answer", ask1_id, "load the schema one", "--run", "r4", "--data", data_ask])
        assert rc == 0 and res_ans_ask["ok"] is True, res_ans_ask
        assert res_ans_ask["state_updated"] is True and res_ans_ask["mk_state_error"] is None, res_ans_ask
        assert r4_status() == "building"

        ask2 = post_lane_ask("and where is the report written?")
        assert ask2["ok"] is True and ask2["state_updated"] is True and ask2["mk_state_error"] is None, ask2
        ask2_id = ask2["message"]["id"]
        assert r4_status() == "blocked"
        tr2 = r4_bus_transitions()
        assert len(tr2) == 2 and tr2[1]["text"].startswith(f"bus ask {ask2_id}: and where is the report"), tr2
        assert tr2[1]["to_id"] == "blocked" and r4_task()["prev_status"] == "building", tr2
        done("lane ask marks blocked (--by bus), answer restores building")

        # 11d. The task side-write failing on the ask path: the POST still succeeds, the
        # failure rides in the reply, and the run's state is untouched. Since L4 the store
        # is the only state (C4), so this write is mkdb.transition and $MK_STATE_CMD can no
        # longer fail it: the one-shot transition-fail marker is the failure seam, the twin
        # of the append-fail flag the store writers already carry.
        data_askf = os.path.join(tmp, "data_askfail")
        os.makedirs(data_askf, exist_ok=True)
        subprocess.run([sys.executable, mk_state, "--data", data_askf, "run", "init", "--id", "r5", "--feature", "f", "--branch", "b", "--force"], check=True, capture_output=True)
        subprocess.run([sys.executable, mk_state, "--data", data_askf, "--run", "r5", "task", "add", "--root", "--id", "T5", "--title", "ask fail lane"], check=True, capture_output=True)
        subprocess.run([sys.executable, mk_state, "--data", data_askf, "--run", "r5", "task", "set", "T5", "status=building"], check=True, capture_output=True)

        r5_store = StoreAdapter(data_askf)

        def r5_state():
            """T5's store row plus r5's transition rows, the state the failed side-write must
            leave exactly as it was. runs/r5/run.json is gone since L4, so a byte read of it
            is no longer a proof of anything — the store's own rows are."""
            row = r5_store.store_select(
                "SELECT status, prev_status FROM tasks WHERE run=? AND id=?", ("r5", "T5"))
            moves = r5_store.store_select(
                "SELECT task, to_id, text, t FROM events WHERE chan=? AND kind=? AND run=?",
                ("state", "transition", "r5"))
            return (row[0] if row else None, moves)

        before = r5_state()
        # The seam, armed before the serve starts: this bus's first transition raises.
        open(os.path.join(data_askf, "transition-fail"), "w").close()
        try:
            rc, res_ens_f, _ = cli(["ensure", "--data", data_askf, "--run", "r5"])
            assert rc == 0 and res_ens_f["ok"] is True, res_ens_f
            pids.append(res_ens_f["pid"])
            lane_askf = os.path.join(tmp, "lane_askfail")
            os.makedirs(lane_askf, exist_ok=True)
            rc, res_tok_f, _ = cli(["token", "--lane", lane_askf, "--task", "T5", "--run", "r5", "--data", data_askf])
            assert rc == 0 and res_tok_f["ok"] is True, res_tok_f
            req_askf = urllib.request.Request(
                f"http://127.0.0.1:{res_ens_f['port']}/send",
                data=json.dumps({"kind": "ask", "text": "does this fail?"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {res_tok_f['token']}", "Host": f"127.0.0.1:{res_ens_f['port']}"},
                method="POST"
            )
            with urllib.request.urlopen(req_askf, timeout=5.0) as resp:
                assert resp.status == 200
                askf = json.loads(resp.read().decode("utf-8"))
            assert askf["ok"] is True and askf["state_updated"] is False, askf
            assert askf["mk_state_error"] and "injected transition failure" in askf["mk_state_error"], askf
            assert r5_state() == before, "the task row and its transitions must be untouched when the side-write fails"
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(os.path.join(data_askf, "transition-fail"))
        done("lane ask survives a failed side-write (200 + error carried, state untouched)")

        # 11e. owner_timeout_s: env hook > yaml bus.owner_timeout_min (clamped to the schema's
        # [5, 120]) > 1800.0 s default. Asserted directly on the helper: fast, no waiting.
        data_ot = os.path.join(tmp, "data_ot")
        os.makedirs(data_ot, exist_ok=True)
        with open(_load_mkpaths().legacy_yaml(data_ot), "w") as f:
            f.write("bus:\n  owner_timeout_min: 5\n")
        assert owner_timeout_s(data_ot) == 300.0
        assert owner_timeout_s(data_ask) == 1800.0  # no yaml there
        os.environ["MK_BUS_HEARTBEAT_TIMEOUT_SEC"] = "7"
        try:
            assert owner_timeout_s(data_ot) == 7.0 and owner_timeout_s(data_ask) == 7.0
        finally:
            os.environ.pop("MK_BUS_HEARTBEAT_TIMEOUT_SEC", None)
        with open(_load_mkpaths().legacy_yaml(data_ot), "w") as f:
            f.write("bus:\n  owner_timeout_min: 999\n")
        assert owner_timeout_s(data_ot) == 120 * 60.0  # clamped up to the schema max
        done("owner_timeout_s: env > yaml (clamped) > 1800 s default")

        # 11f. Store mode makes ids project-wide (one counter per project store), but the
        # once-per-ask guard must still key (run, id), not id alone (review MEDIUM 2): two
        # runs on ONE bus, each lane's ask blocks its own task, and a second ask in the
        # same run is a new (run, id) pair that blocks again. Also pins the argparse
        # side of the "--" terminator (review MEDIUM 1): option-shaped answer texts reach
        # mk-bus.py only after "--" and are stored verbatim, flags not hijacked.
        data_2r = os.path.join(tmp, "data_2runs")
        os.makedirs(data_2r, exist_ok=True)
        for rid, tid in (("r6", "T6"), ("r7", "T7")):
            subprocess.run([sys.executable, mk_state, "--data", data_2r, "run", "init", "--id", rid, "--feature", "f", "--branch", "b", "--force"], check=True, capture_output=True)
            subprocess.run([sys.executable, mk_state, "--data", data_2r, "--run", rid, "task", "add", "--root", "--id", tid, "--title", f"ask lane {rid}"], check=True, capture_output=True)
            subprocess.run([sys.executable, mk_state, "--data", data_2r, "--run", rid, "task", "set", tid, "status=building"], check=True, capture_output=True)

        rc, res_ens_2r, _ = cli(["ensure", "--data", data_2r, "--run", "r6"])
        assert rc == 0 and res_ens_2r["ok"] is True, res_ens_2r
        pids.append(res_ens_2r["pid"])
        port_2r = res_ens_2r["port"]

        def post_ask_2r(tok, text):
            req_2r = urllib.request.Request(
                f"http://127.0.0.1:{port_2r}/send",
                data=json.dumps({"kind": "ask", "text": text}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}", "Host": f"127.0.0.1:{port_2r}"},
                method="POST"
            )
            with urllib.request.urlopen(req_2r, timeout=5.0) as resp:
                return json.loads(resp.read().decode("utf-8"))

        def status_2r(rid, tid):
            """One (run, task) status from the store's tasks table (C4). L4 stopped writing
            runs/<run>/run.json, and a file view could never hold two runs' tasks in one
            project anyway: the store's ids are project-wide, so the two runs of this case
            share one message counter (m-1 in r6, m-2 in r7) and one tasks table."""
            rows = StoreAdapter(data_2r).store_select(
                "SELECT status FROM tasks WHERE run=? AND id=?", (rid, tid))
            return rows[0]["status"] if rows else None

        toks_2r = {}
        for rid, tid in (("r6", "T6"), ("r7", "T7")):
            lane_2r = os.path.join(tmp, f"lane_2r_{rid}")
            os.makedirs(lane_2r, exist_ok=True)
            rc, res_tok_2r, _ = cli(["token", "--lane", lane_2r, "--task", tid, "--run", rid, "--data", data_2r])
            assert rc == 0 and res_tok_2r["ok"] is True, res_tok_2r
            toks_2r[rid] = res_tok_2r["token"]

        ask_2r_a = post_ask_2r(toks_2r["r6"], "first run question")
        ask_2r_b = post_ask_2r(toks_2r["r7"], "second run question")
        # Store mode: one counter per project store. Both ids are therefore whatever
        # this project store's next two ids are — the case does not pin a number (the
        # data dir is fresh but a shared project store can already hold rows), it pins
        # that each lane's ask blocks ITS task: the (run, id) guard, not an id-only one.
        assert ask_2r_a["message"]["id"] != ask_2r_b["message"]["id"], (ask_2r_a, ask_2r_b)
        assert ask_2r_a["state_updated"] is True and ask_2r_a["mk_state_error"] is None, ask_2r_a
        # an id-only guard answered state_updated=False here (r6's ask had already marked)
        assert ask_2r_b["state_updated"] is True and ask_2r_b["mk_state_error"] is None, ask_2r_b
        assert status_2r("r6", "T6") == "blocked"
        assert status_2r("r7", "T7") == "blocked"  # and T7 stayed building on the id-only guard

        rc, res_2r_ev, _ = cli(["answer", "--data", data_2r, "--run", "r6", "--", ask_2r_a["message"]["id"], "--run=evil"])
        assert rc == 0 and res_2r_ev["ok"] is True, res_2r_ev
        assert res_2r_ev["message"]["text"] == "--run=evil", res_2r_ev  # verbatim, not swallowed
        assert res_2r_ev["message"]["run"] == "r6", res_2r_ev  # --run not hijacked by the text

        # A third ask in the SAME run is another (run, id) pair, so the once-per-ask guard
        # marks r6's task again: its id is a fresh one, not either ask's or that answer's.
        ask_2r_c = post_ask_2r(toks_2r["r6"], "third question")
        ask_2r_c_id = ask_2r_c["message"]["id"]
        assert ask_2r_c_id not in ("m-1", ask_2r_b["message"]["id"], res_2r_ev["message"]["id"]), \
            ask_2r_c_id
        assert ask_2r_c_id != ask_2r_a["message"]["id"], ask_2r_c_id
        assert status_2r("r6", "T6") == "blocked"  # once per ask, not once per run
        rc, res_2r_x, _ = cli(["answer", "--data", data_2r, "--run", "r6", "--", ask_2r_c_id, "-x"])
        assert rc == 0 and res_2r_x["ok"] is True, res_2r_x
        assert res_2r_x["message"]["text"] == "-x" and res_2r_x["message"]["run"] == "r6", res_2r_x
        done("ask-once keyed (run, id): each run's ask blocks its own task; answer -- stores text verbatim")

        # 12. Unsupported methods answer JSON 405, never the stdlib 501 HTML
        # page (LOW 5); HEAD sends the JSON headers with no body.
        data_misc = os.path.join(tmp, "data_misc")
        os.makedirs(data_misc, exist_ok=True)
        rc, res_m, _ = cli(["ensure", "--data", data_misc, "--run", "rm"])
        assert rc == 0 and res_m["ok"] is True and res_m["started"] is True, res_m
        pids.append(res_m["pid"])
        port_m = res_m["port"]
        for verb in ("PUT", "DELETE", "PATCH"):
            req_m = urllib.request.Request(
                f"http://127.0.0.1:{port_m}/send",
                data=b"{}", headers={"Host": f"127.0.0.1:{port_m}"}, method=verb
            )
            try:
                with urllib.request.urlopen(req_m, timeout=2.0):
                    raise AssertionError(f"Expected 405 for {verb}")
            except urllib.error.HTTPError as e:
                assert e.code == 405, f"Expected 405 for {verb}, got {e.code}"
                assert json.loads(e.read().decode("utf-8")) == {"ok": False, "error": "method not allowed"}
        req_head = urllib.request.Request(
            f"http://127.0.0.1:{port_m}/health", headers={"Host": f"127.0.0.1:{port_m}"}, method="HEAD"
        )
        try:
            with urllib.request.urlopen(req_head, timeout=2.0):
                raise AssertionError("Expected 405 for HEAD")
        except urllib.error.HTTPError as e:
            assert e.code == 405, f"Expected 405 for HEAD, got {e.code}"
            assert e.read() == b""
        done("PUT/DELETE/PATCH/HEAD -> JSON 405")

        # 12b. A lone surrogate in text is a 400, not a 500, and writes
        # nothing (LOW 2): json.loads happily builds it from the \ud800
        # escape, so the UTF-8 length check is the gate.
        orch_m = read_json(os.path.join(data_misc, "bus.json"))["token"]
        jsonl_m = os.path.join(data_misc, "runs", "rm", "bus.jsonl")
        cnt_m_before = len(open(jsonl_m).readlines()) if os.path.exists(jsonl_m) else 0
        req_sur = urllib.request.Request(
            f"http://127.0.0.1:{port_m}/send",
            data=b'{"run":"rm","kind":"info","text":"\\ud800"}',
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {orch_m}", "Host": f"127.0.0.1:{port_m}"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_sur, timeout=2.0):
                raise AssertionError("Expected 400 for lone surrogate text")
        except urllib.error.HTTPError as e:
            assert e.code == 400, f"Expected 400 for lone surrogate, got {e.code}"
            assert json.loads(e.read().decode("utf-8"))["error"] == "text is not valid UTF-8"
        cnt_m_after = len(open(jsonl_m).readlines()) if os.path.exists(jsonl_m) else 0
        assert cnt_m_after == cnt_m_before, "bus.jsonl must be unchanged after surrogate 400"
        done("lone surrogate text -> 400, no write")

        # 12c. Two concurrent answers to one ask (LOW 5): the duplicate
        # check inside append_message runs under the lock, so exactly one
        # lands (200) and the loser surfaces as 400, never a 500.
        req_ask_m = urllib.request.Request(
            f"http://127.0.0.1:{port_m}/send",
            data=json.dumps({"run": "rm", "kind": "ask", "text": "race ask"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {orch_m}", "Host": f"127.0.0.1:{port_m}"},
            method="POST"
        )
        with urllib.request.urlopen(req_ask_m, timeout=2.0) as resp:
            ask_m = json.loads(resp.read().decode("utf-8"))["message"]
        race_results = []

        def post_race_answer(tag):
            req_a = urllib.request.Request(
                f"http://127.0.0.1:{port_m}/send",
                data=json.dumps({"run": "rm", "kind": "answer", "text": f"answer {tag}", "reply_to": ask_m["id"]}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {orch_m}", "Host": f"127.0.0.1:{port_m}"},
                method="POST"
            )
            try:
                with urllib.request.urlopen(req_a, timeout=5.0) as resp:
                    race_results.append((resp.status, None))
            except urllib.error.HTTPError as e:
                race_results.append((e.code, json.loads(e.read().decode("utf-8")).get("error")))

        race_threads = [threading.Thread(target=post_race_answer, args=(i,)) for i in range(2)]
        for t in race_threads:
            t.start()
        for t in race_threads:
            t.join(timeout=10.0)
        assert all(not t.is_alive() for t in race_threads), "race threads did not terminate"
        codes_m = sorted(r[0] for r in race_results)
        assert codes_m == [200, 400], race_results
        for code_m, err_m in race_results:
            if code_m == 400:
                assert err_m == "ask already answered", race_results
        assert len(open(jsonl_m).readlines()) == cnt_m_before + 2, "exactly ask + one answer appended"
        done("duplicate answer race -> one 200, one 400")

        # 13. heartbeat restarts a SIGKILLed bus (todo 38) and replays its pending ask
        data_hb = os.path.join(tmp, "data_hb")
        rc, res_hb0, _ = cli(["ensure", "--data", data_hb, "--run", "rh"])
        assert rc == 0 and res_hb0["started"] is True
        pids.append(res_hb0["pid"])
        rc, res_hb_ask, _ = cli(["send", "--run", "rh", "--task", "T1", "--kind", "ask", "--text", "survive?", "--from", "T1", "--to", "orch", "--data", data_hb])
        ask_hb = res_hb_ask["message"]["id"]
        rc, res_hb_ok, _ = cli(["heartbeat", "--data", data_hb, "--run", "rh"])
        assert res_hb_ok["ok"] is True and res_hb_ok["restarted"] is False, res_hb_ok
        os.kill(res_hb0["pid"], signal.SIGKILL)
        assert poll_until(lambda: not is_pid_alive(res_hb0["pid"]), timeout=5.0)
        rc, res_hb, _ = cli(["heartbeat", "--data", data_hb, "--run", "rh"])
        assert res_hb["ok"] is True and res_hb["restarted"] is True and res_hb["pid"] != res_hb0["pid"], res_hb
        pids.append(res_hb["pid"])
        with urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{res_hb['port']}/health", headers={"Host": f"127.0.0.1:{res_hb['port']}"}), timeout=2.0) as resp:
            assert json.loads(resp.read().decode("utf-8"))["data"] == data_hb
        rc, pend_hb, _ = cli(["pending", "--run", "rh", "--data", data_hb])
        assert [p["id"] for p in pend_hb["pending"]] == [ask_hb], pend_hb
        done("heartbeat restarts a killed bus; pending ask replayed")

        # 14. restart keeps the old port so armed lanes' url still works
        data_pp = os.path.join(tmp, "data_pp")
        lane_pp = os.path.join(tmp, "lane_pp")
        os.makedirs(lane_pp)
        rc, res_pp0, _ = cli(["ensure", "--data", data_pp, "--run", "rp"])
        assert rc == 0 and res_pp0["started"] is True, res_pp0
        pids.append(res_pp0["pid"])
        rc, _tk, _ = cli(["token", "--lane", lane_pp, "--task", "T1", "--run", "rp", "--data", data_pp])
        assert rc == 0, _tk
        os.kill(res_pp0["pid"], signal.SIGKILL)
        assert poll_until(lambda: not is_pid_alive(res_pp0["pid"]), timeout=5.0)
        rc, res_pp1, _ = cli(["heartbeat", "--data", data_pp, "--run", "rp"])
        assert res_pp1["restarted"] is True and res_pp1["port"] == res_pp0["port"], (res_pp0, res_pp1)
        pids.append(res_pp1["pid"])
        lane_cfg = read_json(os.path.join(lane_pp, ".matrixkoder-bus.json"))
        lane_host = lane_cfg["url"].split("//", 1)[1]
        with urllib.request.urlopen(urllib.request.Request(
                f"{lane_cfg['url']}/wait?lane=T1&timeout=1", headers={"Authorization": f"Bearer {lane_cfg['token']}", "Host": lane_host}), timeout=2.0) as resp:
            assert json.loads(resp.read().decode("utf-8"))["ok"] is True
        # old port taken by another listener -> restart still works, on a new port
        os.kill(res_pp1["pid"], signal.SIGKILL)
        assert poll_until(lambda: not is_pid_alive(res_pp1["pid"]), timeout=5.0)
        squat = socket.socket()
        squat.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # TIME_WAIT from the earlier /wait
        try:
            squat.bind(("127.0.0.1", res_pp1["port"]))
            squat.listen(1)
            rc, res_pp2, _ = cli(["heartbeat", "--data", data_pp, "--run", "rp"])
            assert res_pp2["ok"] is True and res_pp2["restarted"] is True and res_pp2["port"] != res_pp1["port"], res_pp2
            pids.append(res_pp2["pid"])
        finally:
            squat.close()
        done("restart reuses the old port; falls back to a new one when it is taken")

    finally:
        for p in pids:
            stop_process(p)
        with contextlib.suppress(OSError):
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        for k, v in st_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    _store_self_test()
    _layout_self_test()
    _legacy_bus_token_self_test()
    _archive_run_self_test()
    _z14_path_self_test()
    _carry_root_self_test()
    print("pass mk-bus.py: mk-bus self-test ok")
    print(json.dumps({"ok": True, "tests": len(checks), "passed": len(checks)}))
    return 0


def main():
    if "--self-test" in sys.argv:
        try:
            return self_test()
        except AssertionError as e:
            print(f"FAIL mk-bus.py: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"FAIL mk-bus.py: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)

    parser = Parser(prog="mk-bus.py", description="MatrixKoder message bus CLI")
    subparsers = parser.add_subparsers(dest="cmd")

    p_serve = subparsers.add_parser("serve")
    p_serve.add_argument("--data", help="MatrixKoder data dir")
    p_serve.add_argument("--run", help="Run id")
    p_serve.add_argument("--port", type=int, default=0, help="Port to bind (default 0)")
    p_serve.add_argument("--prefer-port", type=int, default=0, help="Try this port first, else fall back to 0")

    p_ensure = subparsers.add_parser("ensure")
    p_ensure.add_argument("--data", help="MatrixKoder data dir")
    p_ensure.add_argument("--run", help="Run id")
    p_ensure.add_argument("--port", type=int, default=0, help="Port to bind if starting")

    p_hb = subparsers.add_parser("heartbeat")
    p_hb.add_argument("--data", help="MatrixKoder data dir")
    p_hb.add_argument("--run", help="Run id (passed to a restarted server)")

    p_token = subparsers.add_parser("token")
    p_token.add_argument("--lane", required=True, help="Lane worktree directory")
    p_token.add_argument("--task", required=True, help="Task identifier")
    p_token.add_argument("--run", default="", help="Run identifier")
    p_token.add_argument("--round", type=int, default=0, help="Round number recorded in the lane token")
    p_token.add_argument("--data", help="MatrixKoder data dir")

    p_revoke = subparsers.add_parser("revoke")
    p_revoke.add_argument("--lane", required=True, help="Lane worktree directory")
    p_revoke.add_argument("--data", help="MatrixKoder data dir")

    p_send = subparsers.add_parser("send")
    p_send.add_argument("--run", required=True, help="Run identifier")
    p_send.add_argument("--task", help="Task identifier")
    # every kind the server knows; the server refuses a store kind on a file-mode bus
    p_send.add_argument("--kind", required=True, choices=list(STORE_KINDS), help="Message kind")
    p_send.add_argument("--text", required=True, help="Message text")
    p_send.add_argument("--from", dest="from_sender", help="Sender identifier")
    p_send.add_argument("--to", dest="to_recipient", help="Recipient identifier")
    p_send.add_argument("--reply-to", dest="reply_to", help="Replied message id")
    p_send.add_argument("--urgent", action="store_true",
                        help="ring a working lane of any kind now: Esc interrupts its turn, then the doorbell")
    p_send.add_argument("--data", help="MatrixKoder data dir")

    p_ans = subparsers.add_parser("answer")
    p_ans.add_argument("msg_id", help="Ask message id to answer")
    p_ans.add_argument("text", help="Answer text")
    p_ans.add_argument("--run", help="Run identifier")
    p_ans.add_argument("--data", help="MatrixKoder data dir")

    p_pend = subparsers.add_parser("pending")
    p_pend.add_argument("--run", default="", help="Run identifier")
    p_pend.add_argument("--data", help="MatrixKoder data dir")

    p_tail = subparsers.add_parser("tail")
    p_metrics = subparsers.add_parser("metrics")
    p_metrics.add_argument("--run")
    p_metrics.add_argument("--data")
    p_metrics.set_defaults(func=cmd_metrics)
    p_tail.add_argument("--run", help="Run identifier")
    p_tail.add_argument("--lane", help="Lane filter")
    p_tail.add_argument("--after", type=int, default=0, help="After seq filter")
    p_tail.add_argument("--n", type=int, default=10, help="Number of messages (default 10)")
    p_tail.add_argument("--data", help="MatrixKoder data dir")

    p_watch = subparsers.add_parser("watch")
    p_watch.add_argument("--run", default="", help="Only this run id")
    p_watch.add_argument("--task", default="", help="Only this task id")
    p_watch.add_argument("--after", default=None, help="Resume after this message id or seq (wins over the saved cursor)")
    p_watch.add_argument("--replay", action="store_true", help="Ignore the saved cursor and replay from the start")
    p_watch.add_argument("--max-messages", type=int, default=0, help="Exit after N lines (0 = never)")
    p_watch.add_argument("--data", help="MatrixKoder data dir")

    p_arch = subparsers.add_parser("archive-run")
    p_arch.add_argument("--run", required=True, help="Run identifier (must be closed)")
    p_arch.add_argument("--data", help="MatrixKoder data dir")

    p_carry = subparsers.add_parser("carry-root")
    p_carry.add_argument("--dry-run", action="store_true", help="List every move, write nothing")

    try:
        args = parser.parse_args()
        if not args.cmd:
            parser.print_help()
            sys.exit(2)

        dispatch = {
            "serve": cmd_serve,
            "ensure": cmd_ensure,
            "heartbeat": cmd_heartbeat,
            "token": cmd_token,
            "revoke": cmd_revoke,
            "send": cmd_send,
            "answer": cmd_answer,
            "pending": cmd_pending,
            "tail": cmd_tail,
            "watch": cmd_watch,
            "metrics": cmd_metrics,
            "archive-run": cmd_archive_run,
            "carry-root": cmd_carry_root,
        }
        fn = dispatch.get(args.cmd)
        if fn:
            rc = fn(args)
            if isinstance(rc, int) and not isinstance(rc, bool) and rc:
                sys.exit(rc)  # watch: 4 bus down, 5 refused
        else:
            raise Fail(f"unknown command {args.cmd}")
    except Fail as f:
        out({"ok": False, "error": str(f)})
        sys.exit(f.code)
    except Exception as e:
        out({"ok": False, "error": f"{type(e).__name__}: {e}"})
        sys.exit(2)


if __name__ == "__main__":
    main()
