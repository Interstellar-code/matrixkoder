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
  --self-test
      Comprehensive self-test of server, round trip, auth, replay, lifecycle, security, error handling.

Every command prints ONE JSON line. Errors: {"ok": false, "error": ...} exit 2.
"""
import argparse, contextlib, datetime, fcntl, hashlib, hmac, importlib.util, io, json, math, os, re, secrets, shutil, signal, socket, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))

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
NONEMPTY_KINDS = ("ask", "answer", "info", "task", "status", "commit", "report", "review", "nudge")
# status values transition() already accepts (COLUMNS plus attention).
STATUS_VALUES = (
    "planned", "routed", "ready", "dispatched", "building", "takeover",
    "verifying", "review", "fix", "merged", "qa", "done",
    "blocked", "stalled", "quota", "provider",
)


def project_store_path(data_dir):
    """The project store lives at <data>/mk.db. Absent means file mode."""
    return os.path.join(data_dir, "mk.db")


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def mk_home():
    """MK home: $MK_HOME, else ~/.matrixkoder. Never the project data dir."""
    return os.path.abspath(os.path.expanduser(os.environ.get("MK_HOME") or "~/.matrixkoder"))


def root_bus_dir(home=None):
    """Where the one-per-home bus lives: <home>/bus.json and <home>/bus.lock."""
    return home or mk_home()


def lane_token_path(lane_dir, home=None):
    """C6: the lane token file is outside the worktree.

    ~/.matrixkoder/bus-lanes/<sha256(realpath(lane))>.json, mode 0600.
    $MK_BUS_TOKEN_FILE overrides the path (tests and a relocated home).
    """
    override = os.environ.get("MK_BUS_TOKEN_FILE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    real = os.path.realpath(lane_dir)
    digest = hashlib.sha256(real.encode("utf-8")).hexdigest()
    base = home or os.environ.get("MK_BUS_DIR") or root_bus_dir()
    return os.path.join(os.path.abspath(os.path.expanduser(base)), "bus-lanes", digest + ".json")


def _load_mkdb():
    path = os.path.join(HERE, "mkdb.py")
    spec = importlib.util.spec_from_file_location("mkdb", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def store_mode(data_dir):
    """Self-hosting guard: store code runs only when the project store already exists."""
    return os.path.isfile(project_store_path(data_dir))


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
                            reply_to=None, text="", meta=None, artifact_id=None, t=None):
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
            self.data_dir, None, run, task=task, chan="bus", kind=kind,
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
        return fn(
            self.data_dir, None, run, task, to, note=note, round=round_n,
            sync_root=False, data_dir=self.data_dir,
        )

    def store_select(self, sql, params=()):
        """SELECT only, on the connection open_project returns. No writes."""
        mod = self._mod()
        con = mod.open_project(self.data_dir, data_dir=self.data_dir)
        try:
            return [dict(r) for r in con.execute(sql, params)]
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
    if arg:
        return os.path.abspath(os.path.expanduser(arg))
    if os.environ.get("MATRIXKODER_DATA"):
        return os.path.abspath(os.path.expanduser(os.environ["MATRIXKODER_DATA"]))
    d = os.path.realpath(os.getcwd())
    stop = os.path.realpath(os.path.expanduser("~"))
    while not os.path.exists(os.path.join(d, ".git")):
        parent = os.path.dirname(d)
        if parent == d or d == stop:
            break
        d = parent
    return os.path.join(d, ".matrixkoder")


def bus_location(data_dir):
    """Where bus.json / bus.lock / bus-tokens.json live for this data dir.

    File mode (no <data>/mk.db): the data dir, exactly as before — the
    self-hosting guard. Store mode: the MK home, one bus for every project.
    $MK_BUS_DIR overrides (tests pin a temp home without touching ~/.matrixkoder).
    """
    override = os.environ.get("MK_BUS_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if store_mode(data_dir):
        return root_bus_dir()
    return data_dir


def project_slug_for(data_dir):
    """The root projects.slug whose data_dir is this path, or None in file mode.

    Validated against the root projects table (slug rule C11) BEFORE any
    project store is opened. A store with no matching root row is refused.
    The root store is <home>/mk.db; $MK_BUS_DIR is that home in tests.
    """
    if not store_mode(data_dir):
        return None
    mod = _load_mkdb()
    home = os.environ.get("MK_BUS_DIR") or mk_home()
    real = os.path.realpath(data_dir)
    con = mod.open_root(home)
    try:
        rows = con.execute("SELECT slug, data_dir FROM projects").fetchall()
    finally:
        con.close()
    for row in rows:
        slug = row["slug"]
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            continue
        recorded = row["data_dir"]
        if recorded and os.path.realpath(os.path.expanduser(recorded)) == real:
            return slug
    # No root row yet. Register this store so it joins the root bus: a present
    # mk.db is store mode (task/status/report), and file mode would refuse
    # those kinds. The slug is derived from the data dir path.
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
            lines = _mk_config.read_lines(os.path.join(data_dir, "matrixkoder.yaml"))
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
        self.tokens = {}
        self.tokens_mtime = 0
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
        # Doorbell drain interval. A queued ring is delivered when the lane
        # goes idle even if no later message arrives to call _drain_rings.
        # Override with MK_BUS_RING_INTERVAL_SEC (seconds, minimum 0.05).
        self.ring_interval = 5.0
        try:
            self.ring_interval = max(0.05, float(os.environ.get("MK_BUS_RING_INTERVAL_SEC", "5")))
        except (TypeError, ValueError):
            self.ring_interval = 5.0

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
        tokens_path = os.path.join(self.data_dir, "bus-tokens.json")
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
        lock_path = os.path.join(self.data_dir, "bus-tokens.lock")
        tokens_path = os.path.join(self.data_dir, "bus-tokens.json")
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
        lock_path = os.path.join(self.data_dir, "bus-tokens.lock")
        tokens_path = os.path.join(self.data_dir, "bus-tokens.json")
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
            jsonl_path = os.path.join(base, "runs", run, "bus.jsonl")
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

    def append_message(self, run, task, from_val, to_val, kind, text, reply_to, project=None):
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
                run_dir = os.path.join(project_dir, "runs", run)
                line = json.dumps(msg, separators=(",", ":")) + "\n"
                self._append_jsonl(run_dir, line)

            rstate["messages"].append(msg)
            rstate["by_id"][msg_id] = msg
            rstate["cond"].notify_all()
            ring = msg
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
        """
        if not self.use_store:
            return self.store, self.project_dir
        if not isinstance(project, str) or project == "" or not SLUG_RE.match(project):
            raise Fail("invalid project slug")
        if project == self.project:
            return self.store, self.project_dir
        mod = _load_mkdb()
        home = os.environ.get("MK_BUS_DIR") or mk_home()
        con = mod.open_root(home)
        try:
            row = con.execute("SELECT data_dir FROM projects WHERE slug=?", (project,)).fetchone()
        finally:
            con.close()
        if not row or not row["data_dir"]:
            raise Fail("unknown project")
        data_dir = os.path.realpath(os.path.expanduser(row["data_dir"]))
        return StoreAdapter(data_dir), data_dir

    def _maybe_ring(self, msg):
        """C13b: ring an idle lane once for an unsolicited message.

        A solicited answer (one whose reply_to names an ask) needs no ring.
        A nudge, an info, or an answer with no waiter does. The ring is one
        fixed line, typed only while herdr says the lane is idle; a working
        lane is queued and rung on a later tick. One ring per message id,
        recorded as an event chan=bus kind=ring. With the bus down this
        method is never called, so nothing rings.
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
        if msg["id"] in self.rung:
            return
        self._ring_queue.append(msg)
        self._save_ring_queue()
        self._drain_rings()

    def _drain_rings(self):
        still = []
        for msg in self._ring_queue:
            if msg["id"] in self.rung:
                continue
            status = self._herdr_status(msg.get("to"))
            if status == "working":
                still.append(msg)  # queue until idle
                continue
            if status != "idle":
                continue  # unknown lane: do not ring
            # Claim before typing so a second drain cannot ring the same id.
            self.rung.add(msg["id"])
            self._ring(msg)
        self._ring_queue = still
        self._save_ring_queue()

    def _lane_worktrees(self, task):
        """Every lane worktree path a token for `task` was minted for.

        Store mode keeps tokens in <bus dir>/bus-tokens.json, which records the
        lane directory per token. The worktree path is how a real pane is
        identified (herdr agents carry `cwd`), so the ring can find the lane's
        pane without the orchestrator passing a pane id. A task can hold more
        than one live token (re-armed in a fresh worktree, a fix round), so all
        of them are candidates: matching the first one alone would miss the
        pane that is actually running.
        """
        try:
            tokens = read_json(os.path.join(self.data_dir, "bus-tokens.json"), default={})
        except Exception:
            return set()
        lanes = set()
        for v in (tokens or {}).values():
            if isinstance(v, dict) and v.get("task") == task and v.get("lane"):
                lanes.add(os.path.realpath(v["lane"]))
        return lanes

    def _herdr_agents(self):
        """Every agent dict from the real `herdr agent list`.

        $MK_BUS_HERDR stays the test seam: a program printing the same JSON,
        invoked exactly as today. With it unset the REAL `herdr` binary on
        PATH is called. Missing binary, non-zero exit, bad JSON or a timeout
        all return [] — no ring and no crash (the bus-down rule's twin).
        """
        fake = os.environ.get("MK_BUS_HERDR")
        if fake:
            argv = [sys.executable, fake]
        else:
            if shutil.which("herdr") is None:
                return []
            argv = ["herdr", "agent", "list"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
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

    def _herdr_pane_for(self, task):
        """The pane id of the lane agent for `task`, or None.

        Match order: the agent named after the task, then the agent whose cwd
        IS the task's lane worktree (the real-lane case, todo 66a / F4). The
        old `cwd.endswith("/" + task)` guess is kept last for the fake.
        """
        agents = self._herdr_agents()
        if not agents:
            return None
        lanes = self._lane_worktrees(task)
        by_name = by_cwd = by_suffix = None
        for a in agents:
            if not isinstance(a, dict):
                continue
            name = a.get("name") or ""
            cwd = a.get("cwd") or ""
            pane = a.get("pane_id") or a.get("pane") or name
            if name == task and by_name is None:
                by_name = (a, pane)
            if lanes and cwd and os.path.realpath(cwd) in lanes and by_cwd is None:
                by_cwd = (a, pane)
            if cwd.endswith("/" + task) and by_suffix is None:
                by_suffix = (a, pane)
        for hit in (by_cwd, by_name, by_suffix):
            if hit:
                return hit[0], hit[1]
        return None

    def _herdr_status(self, task):
        """agent_status of the pane whose name or cwd matches the task.

        $MK_BUS_HERDR is a fake: a program printing `herdr agent list` JSON.
        Unset means the REAL herdr binary; missing or failing means None, so
        nothing rings.
        """
        hit = self._herdr_pane_for(task)
        if not hit:
            return None
        return hit[0].get("agent_status")

    def _ring(self, msg):
        """Record the ring BEFORE typing it.

        A crash between the two leaves kind=ring with meta typed=false. On
        restart that row is typed once. A row with typed=true is never typed
        again. MK_BUS_RING_KILL, when set to a path, is a test seam: the
        process exits after the record and before the type.
        """
        skill = os.environ.get("MK_BUS_SKILL_DIR") or os.path.join(HERE, "..", "child-skills", "mk-builder")
        line = "Run: python3 %s/scripts/mk-builder.py inbox" % os.path.abspath(skill)
        store, _ = self.store_for(msg.get("project"))
        eid = store.store_append_event(
            msg["run"], task=None, kind="ring",
            from_id="bus", to_id=msg.get("to"), reply_to=msg["id"], text=line,
            meta={"typed": False, "msg": msg["id"]},
        )
        self.rung.add(msg["id"])
        kill = os.environ.get("MK_BUS_RING_KILL")
        if kill:
            try:
                open(kill, "w").write(str(msg.get("id")))
            except OSError:
                pass
            os._exit(86)
        self._type_ring(msg, line, eid, store)

    def _type_ring(self, msg, line, eid, store):
        """Type the fixed doorbell line into the lane's pane.

        One ring path: the test seam (`$MK_BUS_HERDR`, invoked as today) or the
        REAL `herdr agent prompt <pane> <line>`. The line is fixed — a message
        body never becomes typed text (C13a/C13b). A missing or failing herdr
        is recorded as typed (nothing more to try) and never raises.
        """
        task = msg.get("to")
        try:
            fake = os.environ.get("MK_BUS_HERDR")
            if fake:
                subprocess.run([sys.executable, fake, "prompt", task or "", line], timeout=5)
            else:
                hit = self._herdr_pane_for(task)
                if not hit:
                    return  # herdr missing or no pane for the lane
                _agent, pane = hit
                subprocess.run(["herdr", "agent", "prompt", str(pane), line], timeout=30)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return
        finally:
            # Mark typed. A second event would re-ring on replay, so the typed
            # bit is a sibling row kind=ring-typed that restart treats as done.
            store.store_append_event(
                msg["run"], task=None, kind="ring-typed",
                from_id="bus", to_id=msg.get("to"), reply_to=str(msg["id"]), text=str(eid or ""),
                meta={"typed": True, "msg": msg["id"]},
            )

    def _ring_queue_path(self):
        return os.path.join(self.data_dir, "ring-queue.json")

    def _save_ring_queue(self):
        """Persist queued rings so a restart can drain them once the lane is idle.

        The queue is messages that arrived while the lane was working. It is
        not the ring event itself; _ring still records that before typing.
        """
        if not self.use_store:
            return
        try:
            write_json(self._ring_queue_path(), self._ring_queue, mode=0o600)
        except OSError as e:
            sys.stderr.write("ring queue save failed: %r\n" % (e,))

    def _load_ring_queue(self):
        if not self.use_store:
            return
        saved = read_json(self._ring_queue_path(), default=[])
        if isinstance(saved, list):
            self._ring_queue = [m for m in saved if isinstance(m, dict) and m.get("id")]

    def restore_rings(self):
        """On startup: a recorded-but-untyped ring is typed once; a typed one never again."""
        if not self.use_store:
            return
        rows = self.store.store_select(
            "SELECT id, run, kind, to_id, reply_to, text, meta_json FROM events WHERE kind IN ('ring','ring-typed') ORDER BY id",
            (),
        )
        typed = set()
        pending = []
        for row in rows:
            meta = {}
            if row.get("meta_json"):
                try:
                    meta = json.loads(row["meta_json"])
                except (TypeError, ValueError):
                    meta = {}
            mid = row.get("reply_to") or (meta or {}).get("msg")
            if row.get("kind") == "ring-typed":
                typed.add(mid)
            elif mid:
                pending.append((mid, row))
        for mid, row in pending:
            self.rung.add(mid)
            if mid in typed:
                continue
            msg = {"id": mid, "run": row.get("run"), "to": row.get("to_id"), "project": self.project}
            try:
                self._type_ring(msg, row.get("text") or "", row.get("id"), self.store)
            except Exception as e:
                sys.stderr.write("ring restore failed: %r\n" % (e,))
        # A restart does not drain rings queued by the previous process: that
        # queue is not durable. Only an untyped ring event is finished.
        self._ring_queue = []

    def ring_worker(self, stop_event):
        """Drain the doorbell queue on a fixed interval and when a lane goes idle.

        _drain_rings also runs when a message arrives. This worker is what
        delivers a ring that was queued while the lane was working, after the
        lane becomes idle and no further message shows up.
        """
        while not stop_event.wait(self.ring_interval):
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
                run_dir = os.path.join(project_dir, "runs", run)
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
        body = (text or "").replace("\x1e", "")
        if len(body.encode("utf-8")) > STORE_TEXT_MAX:
            raise Fail("text exceeds 128 KiB")

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
        body = msg["text"].replace("\x1e", "")
        if len(body.encode("utf-8")) > STORE_TEXT_MAX:
            raise Fail("text exceeds 128 KiB")
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
                runs_dir = os.path.join(base, "runs")
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

                rstate = self.server.bus_state.get_run_state(run, project_dir)

                while True:
                    to_send = []
                    with rstate["cond"]:
                        for m in rstate["messages"]:
                            if m["seq"] > last_seq and matches_lane(m, lane):
                                to_send.append(dict(m))
                                m["ack_level"] = max(m.get("ack_level", 1), 2)
                                last_seq = m["seq"]

                        if not to_send:
                            rstate["cond"].wait(timeout=15.0)
                            for m in rstate["messages"]:
                                if m["seq"] > last_seq and matches_lane(m, lane):
                                    to_send.append(dict(m))
                                    m["ack_level"] = max(m.get("ack_level", 1), 2)
                                    last_seq = m["seq"]

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

            limit = STORE_TEXT_MAX + 4096 if self.server.bus_state.use_store else 65536
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
                text_max = STORE_TEXT_MAX if self.server.bus_state.use_store else FILE_TEXT_MAX
                if text_len > text_max:
                    self.send_error_json(400, "text exceeds 64 KiB" if text_max == FILE_TEXT_MAX else "text exceeds 128 KiB")
                    return
                # B18: text is non-empty for ask/answer/info; empty is allowed
                # only for ack.
                if kind in NONEMPTY_KINDS and not text:
                    self.send_error_json(400, "text must be non-empty")
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
                        project=project,
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
                        ask_reply["state_updated"], ask_reply["mk_state_error"] = _mk_state_task_set(
                            self.server.bus_state.data_dir, run, task, "blocked",
                            f"bus ask {msg['id']}: {text[:80]}", by="bus")
                    self.send_json(200, {"ok": True, "message": msg, **ask_reply})
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


def heartbeat_monitor(server, data_dir, stop_event):
    check_interval = float(os.environ.get("MK_BUS_HEARTBEAT_CHECK_SEC", "60.0"))
    timeout = owner_timeout_s(data_dir)  # env hook > bus.owner_timeout_min > schema default 30 min
    bus_json_path = os.path.join(data_dir, "bus.json")

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
    lock_path = os.path.join(bus_dir, "bus.lock")
    bus_json_path = os.path.join(bus_dir, "bus.json")

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
        args=(server, bus_dir, stop_event),
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
    bus_json_path = os.path.join(bus_dir, "bus.json")

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
                                if isinstance(h, dict) and h.get("project_dir") == os.path.realpath(data_dir):
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
    bus_json_path = os.path.join(bus_dir, "bus.json")
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

    bus_json_path = os.path.join(bus_location(data_dir), "bus.json")
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
        lane_file = lane_token_path(lane_dir)
    else:
        lane_file = os.path.join(lane_dir, ".matrixkoder-bus.json")
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
        "round": round_val,
    }

    write_json(lane_file, payload, mode=0o600)

    bus_dir = bus_location(data_dir)
    lock_path = os.path.join(bus_dir, "bus-tokens.lock")
    tokens_path = os.path.join(bus_dir, "bus-tokens.json")
    lfd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(lfd, 0o600)
    fcntl.flock(lfd, fcntl.LOCK_EX)
    try:
        tokens = read_json(tokens_path, default={})
        tokens[tok] = {
            "project": project_slug_for(data_dir) if store_mode(data_dir) else None,
            "token": tok,
            "role": "lane",
            "run": run_val,
            "task": args.task,
            "lane": lane_dir,
            "created_at": now_iso()
        }
        write_json(tokens_path, tokens, mode=0o600)
    finally:
        fcntl.flock(lfd, fcntl.LOCK_UN)
        os.close(lfd)

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

    bus_dir = bus_location(data_dir)
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
    bus_json_path = os.path.join(bus_location(data_dir), "bus.json")
    if not os.path.exists(bus_json_path):
        raise Fail("bus down")
    bdata = read_json(bus_json_path)
    if not bdata or "port" not in bdata:
        raise Fail("bus down")
    port = bdata["port"]
    token = bdata.get("token")
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
    try:
        store = StoreAdapter(data_dir)
        rows = store.store_select(
            "SELECT id, run, task, t, kind, from_id, to_id, reply_to, text, artifact_id, meta_json"
            " FROM events WHERE chan=? AND run=? ORDER BY id",
            ("bus", run),
        )
        ack_rows = store.store_select(
            "SELECT msg, lane, level FROM acks WHERE run=?", (run,)
        )
    except Exception:
        return []
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


def _read_run_messages(data_dir, run):
    """Every message of one run: the project store in store mode, else bus.jsonl.

    E2 (2): `answer` and `tail` must see a store-mode run, where the messages
    live in mk.db and `<data>/runs/<run>/bus.jsonl` is absent. File mode keeps
    reading bus.jsonl exactly as before.
    """
    if store_mode(data_dir):
        msgs = _store_run_messages(data_dir, run)
        if msgs:
            return msgs
    jsonl = os.path.join(data_dir, "runs", run, "bus.jsonl")
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


def cmd_answer(args):
    data_dir = resolve_data(getattr(args, "data", None))
    port, token = _get_client_conn(data_dir)

    runs_dir = os.path.join(data_dir, "runs")
    target_run = getattr(args, "run", None)
    if target_run and not is_valid_id(target_run):
        raise Fail("invalid run id")
    runs_to_search = [target_run] if target_run else []
    if not target_run and os.path.exists(runs_dir):
        runs_to_search = [d for d in os.listdir(runs_dir) if os.path.isdir(os.path.join(runs_dir, d)) and is_valid_id(d)]
    if not target_run and not runs_to_search and store_mode(data_dir):
        # Store mode: `<data>/runs/<run>` may not exist on disk at all, so the
        # run list comes from the project store (SELECT only).
        try:
            store_rows = StoreAdapter(data_dir).store_select("SELECT id FROM runs")
            runs_to_search = [r["id"] for r in store_rows if is_valid_id(r.get("id"))]
        except Exception:
            runs_to_search = []

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
    if task:
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
        runs_dir = os.path.join(data_dir, "runs")
        if os.path.exists(runs_dir):
            runs = [d for d in os.listdir(runs_dir) if os.path.isdir(os.path.join(runs_dir, d)) and is_valid_id(d)]
            if len(runs) == 1:
                run = runs[0]
        if not run and store_mode(data_dir):
            # Store mode: the run may exist only in mk.db (todo 68a).
            try:
                store_rows = StoreAdapter(data_dir).store_select("SELECT id FROM runs")
                runs = [r["id"] for r in store_rows if is_valid_id(r.get("id"))]
            except Exception:
                runs = []
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
    text = re.sub(r"\s+", " ", str(m.get("text") or "")).strip()[:200]
    return "MSG %s %s %s %s %s->%s: %s" % (
        m.get("id", ""), m.get("run", ""), m.get("task", ""), m.get("kind", ""),
        m.get("from", ""), m.get("to", ""), text,
    )


def _orch_token(data_dir):
    """The live orchestrator token, from bus.json or bus-tokens.json."""
    bus_json = read_json(os.path.join(bus_location(data_dir), "bus.json"))
    if bus_json and bus_json.get("token"):
        return bus_json["token"]
    tokens = read_json(os.path.join(bus_location(data_dir), "bus-tokens.json"), default={})
    for tok, v in (tokens or {}).items():
        if isinstance(v, dict) and v.get("role") == "orch":
            return tok
    return None


def cmd_watch(args):
    """E2 (4): the orchestrator's stream. One flushed line per bus message.

    Runs under the Claude Code Monitor tool so every lane message reaches the
    main session as a notification. After a drop or a bus RESTART it resumes
    with Last-Event-ID and re-reads bus.json for the new port. Exit 4 only
    when the bus stays down for WATCH_DOWN_SEC (60 s), never on one blip.
    """
    data_dir = resolve_data(getattr(args, "data", None))
    run = getattr(args, "run", "") or ""
    task = getattr(args, "task", "") or ""
    if run and not is_valid_id(run):
        raise Fail("invalid run id")
    if task and not is_valid_id(task):
        raise Fail("invalid task id")
    after = getattr(args, "after", 0) or 0
    last_seq = int(str(after)[2:]) if str(after).startswith("m-") and str(after)[2:].isdigit() else int(after or 0)
    max_messages = getattr(args, "max_messages", 0) or 0

    down_since = None
    printed = 0
    while True:
        port = None
        try:
            bdata = read_json(os.path.join(bus_location(data_dir), "bus.json"))
            if bdata and bdata.get("port"):
                port = int(bdata["port"])
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
            qs = urllib.parse.urlencode({"lane": "*"})
            if run:
                qs += "&run=" + urllib.parse.quote(run)
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
                        printed += 1
                        if max_messages and printed >= max_messages:
                            return 0
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
    """C13f: one JSON line of bus metrics for a run, from the project store."""
    data_dir = resolve_data(getattr(args, "data", None))
    run = args.run
    if not is_valid_id(run):
        raise Fail("invalid run id")
    if not store_mode(data_dir):
        raise Fail("metrics need a project store")
    store = StoreAdapter(data_dir)
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
        "ok": True, "run": run,
        "latency_p50": pct(lat, 0.50), "latency_p95": pct(lat, 0.95),
        "failed_deliveries": failed,
        "obedience": (round(obeyed / len(sent), 3) if sent else None),
        "false_stall": false_stall,
    }
    out(res)
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


def _store_self_test_body(mod):
    """Real store round-trip. Runs only after append_event exists.

    File-mode self_test (the 31 checks above) is not touched. This body is
    reached only when _mkdb_helpers_ready sees append_event.
    """
    import shutil

    tmp = tempfile.mkdtemp(prefix="mk-bus-store-")
    pids = []

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

    def spawn(data_dir, run, prefer=None):
        argv = [sys.executable, os.path.abspath(__file__), "serve", "--data", data_dir, "--run", run]
        if prefer:
            argv += ["--prefer-port", str(prefer)]
        proc = subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
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
        # project. The test home is temp; MK_HOME keeps it off the real one.
        home = os.path.join(tmp, "home")
        os.makedirs(home, exist_ok=True)
        os.environ["MK_BUS_DIR"] = home
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
        assert os.path.isfile(os.path.join(data, "mk.db")), "open_project did not create mk.db"

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
        over = "b" * (131072 + 1)
        code, over_res = post(port2, token, {"kind": "report", "text": over}, timeout=20.0)
        assert code == 400, (code, over_res)

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
        lanes_dir = os.path.join(home, "bus-lanes")
        assert os.path.isdir(lanes_dir) and os.listdir(lanes_dir), "token file not under bus-lanes"
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
            fh.write("    sys.exit(0)\n")
            fh.write("import os\n")
            fh.write("sf = sys.argv[0] + '.status'\n")
            fh.write("status = open(sf).read().strip() if os.path.exists(sf) else 'idle'\n")
            fh.write("print(json.dumps({'result': {'agents': [\n")
            fh.write("    {'name': 'T1', 'agent_status': status, 'cwd': '/x/T1'}]}}))\n")
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
        pid2, port2 = spawn(data, "r1")
        orch = mint_orch(data)
        btok = remint()
        code, nudge = post(port2, btok, {"run": "r1", "task": "T1", "kind": "nudge", "text": "look", "from": "orch", "to": "T1", "project": "proj"})
        assert code == 200, (code, nudge)
        time.sleep(0.3)
        rang = open(fake + ".rang").read() if os.path.isfile(fake + ".rang") else ""
        assert "T1" in rang and "mk-builder.py inbox" in rang, rang
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
            fh.write("with open(log, 'a') as f:\n")
            fh.write("    f.write(cmd + ' ' + ' '.join(sys.argv[3:]) + chr(10))\n")
            fh.write("if cmd == 'agent list':\n")
            fh.write("    sys.stdout.write(open(os.environ['MK_BUS_E2_HERDR_JSON']).read())\n")
            fh.write("    sys.exit(0)\n")
            fh.write("sys.exit(0)\n")
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
        assert "mk-builder.py inbox" in rings[0], rings

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

        os.environ.pop("MK_BUS_E2_HERDR_LOG", None)
        os.environ.pop("MK_BUS_E2_HERDR_JSON", None)

        rc_m = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "metrics", "--run", "r1", "--data", data],
            stdout=subprocess.PIPE, text=True,
        )
        assert rc_m.returncode == 0, rc_m.stdout
        metrics = json.loads(rc_m.stdout.strip().splitlines()[-1])
        assert metrics["ok"] is True and "latency_p50" in metrics and "obedience" in metrics and "false_stall" in metrics, metrics

        # no direct database-module import text anywhere in this script
        src = open(os.path.abspath(__file__), encoding="utf-8").read()
        assert ("sq" + "lite3") not in src, "mk-bus.py mentions the database module"


        kill(pid2)
    finally:
        for p in pids:
            stop_process(p)
        shutil.rmtree(tmp, ignore_errors=True)


def self_test():
    tmp = tempfile.mkdtemp(prefix="mk-bus-test-")
    pids = []

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
        subprocess.run([sys.executable, mk_state, "--data", data_state, "--run", "r2", "task", "add", "--id", "T2", "--title", "task two"], check=True, capture_output=True)
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

        # 11b. Ambiguous message id refused (MEDIUM 2): ids are per run, so
        # "m-1" exists in both r2 and r3; without --run the CLI must fail
        # instead of answering whichever run os.listdir returned first.
        subprocess.run([sys.executable, mk_state, "--data", data_state, "run", "init", "--id", "r3", "--feature", "f", "--branch", "b", "--force"], check=True, capture_output=True)
        subprocess.run([sys.executable, mk_state, "--data", data_state, "--run", "r3", "task", "add", "--id", "T3", "--title", "task three"], check=True, capture_output=True)

        rc, res_ask3, _ = cli(["send", "--run", "r3", "--task", "T3", "--kind", "ask", "--text", "second run ask", "--from", "T3", "--to", "orch", "--data", data_state])
        assert rc == 0 and res_ask3["ok"] is True, res_ask3
        assert res_ask3["message"]["id"] == "m-1", res_ask3

        rc, res_amb, _ = cli(["answer", "m-1", "which one?", "--data", data_state], check=False)
        assert rc != 0, "ambiguous answer without --run must fail"
        assert res_amb == {"ok": False, "error": "ambiguous message id, pass --run"}, res_amb

        # --run disambiguates: r3's ask is unanswered (r2's already answered)
        rc, res_dis, _ = cli(["answer", "m-1", "answered via run", "--run", "r3", "--data", data_state])
        assert rc == 0 and res_dis["ok"] is True, res_dis
        assert res_dis["message"]["reply_to"] == "m-1" and res_dis["message"]["run"] == "r3", res_dis

        rc, res_r2, _ = cli(["answer", "m-1", "nope", "--run", "r2", "--data", data_state], check=False)
        assert rc != 0 and "already answered" in res_r2.get("error", ""), res_r2
        done("ambiguous answer refused, --run disambiguates")

        # 11c. A lane ask over real HTTP marks its task blocked (once per ask id, --by bus) via
        # the one shared mk-state helper; the reply carries state_updated/mk_state_error; the
        # answer restores building through the same helper.
        data_ask = os.path.join(tmp, "data_ask")
        os.makedirs(data_ask, exist_ok=True)
        subprocess.run([sys.executable, mk_state, "--data", data_ask, "run", "init", "--id", "r4", "--feature", "f", "--branch", "b", "--force"], check=True, capture_output=True)
        subprocess.run([sys.executable, mk_state, "--data", data_ask, "--run", "r4", "task", "add", "--id", "T4", "--title", "ask lane"], check=True, capture_output=True)
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

        def r4_status():
            run_ = read_json(os.path.join(data_ask, "runs", "r4", "run.json"))
            return next(t["status"] for t in run_["tasks"] if t["id"] == "T4")

        def r4_bus_transitions():
            ev_path = os.path.join(data_ask, "runs", "r4", "events.jsonl")
            return [json.loads(l) for l in open(ev_path).readlines() if l.strip()
                    and json.loads(l).get("type") == "transition"
                    and json.loads(l).get("by") == "bus"]

        ask1 = post_lane_ask("which fixture should I load?")
        assert ask1["ok"] is True and ask1["state_updated"] is True and ask1["mk_state_error"] is None, ask1
        ask1_id = ask1["message"]["id"]
        assert r4_status() == "blocked"
        tr1 = r4_bus_transitions()
        assert len(tr1) == 1 and tr1[0]["text"].startswith(f"bus ask {ask1_id}: which fixture"), tr1
        assert tr1[0]["from"] == "building" and tr1[0]["to"] == "blocked", tr1

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
        assert tr2[1]["from"] == "building" and tr2[1]["to"] == "blocked", tr2
        done("lane ask marks blocked (--by bus), answer restores building")

        # 11d. mk-state failing on the ask path: the POST still succeeds, the failure rides in
        # the reply, and run state is untouched.
        data_askf = os.path.join(tmp, "data_askfail")
        os.makedirs(data_askf, exist_ok=True)
        subprocess.run([sys.executable, mk_state, "--data", data_askf, "run", "init", "--id", "r5", "--feature", "f", "--branch", "b", "--force"], check=True, capture_output=True)
        subprocess.run([sys.executable, mk_state, "--data", data_askf, "--run", "r5", "task", "add", "--id", "T5", "--title", "ask fail lane"], check=True, capture_output=True)
        subprocess.run([sys.executable, mk_state, "--data", data_askf, "--run", "r5", "task", "set", "T5", "status=building"], check=True, capture_output=True)
        r5_json = os.path.join(data_askf, "runs", "r5", "run.json")
        before = open(r5_json, "rb").read()
        fake_ms = os.path.join(tmp, "fake-mk-state.py")
        with open(fake_ms, "w") as f:
            f.write("#!/usr/bin/env python3\nimport sys\nsys.stderr.write('mk-state: run state locked\\n')\nsys.exit(1)\n")
        os.chmod(fake_ms, 0o755)
        os.environ["MK_STATE_CMD"] = fake_ms  # inherited by the serve this ensure starts
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
            assert askf["mk_state_error"] and "run state locked" in askf["mk_state_error"], askf
            assert open(r5_json, "rb").read() == before, "run.json must be untouched when mk-state fails"
        finally:
            os.environ.pop("MK_STATE_CMD", None)
        done("lane ask survives mk-state failure (200 + error carried)")

        # 11e. owner_timeout_s: env hook > yaml bus.owner_timeout_min (clamped to the schema's
        # [5, 120]) > 1800.0 s default. Asserted directly on the helper: fast, no waiting.
        data_ot = os.path.join(tmp, "data_ot")
        os.makedirs(data_ot, exist_ok=True)
        with open(os.path.join(data_ot, "matrixkoder.yaml"), "w") as f:
            f.write("bus:\n  owner_timeout_min: 5\n")
        assert owner_timeout_s(data_ot) == 300.0
        assert owner_timeout_s(data_ask) == 1800.0  # no yaml there
        os.environ["MK_BUS_HEARTBEAT_TIMEOUT_SEC"] = "7"
        try:
            assert owner_timeout_s(data_ot) == 7.0 and owner_timeout_s(data_ask) == 7.0
        finally:
            os.environ.pop("MK_BUS_HEARTBEAT_TIMEOUT_SEC", None)
        with open(os.path.join(data_ot, "matrixkoder.yaml"), "w") as f:
            f.write("bus:\n  owner_timeout_min: 999\n")
        assert owner_timeout_s(data_ot) == 120 * 60.0  # clamped up to the schema max
        done("owner_timeout_s: env > yaml (clamped) > 1800 s default")

        # 11f. Ask ids are per run ("m-1" exists in every run), so the once-per-ask guard
        # must key (run, id), not id alone (review MEDIUM 2): two runs on ONE bus, each
        # lane's first ask is m-1 — both tasks must go blocked. Also pins the argparse
        # side of the "--" terminator (review MEDIUM 1): option-shaped answer texts reach
        # mk-bus.py only after "--" and are stored verbatim, flags not hijacked.
        data_2r = os.path.join(tmp, "data_2runs")
        os.makedirs(data_2r, exist_ok=True)
        for rid, tid in (("r6", "T6"), ("r7", "T7")):
            subprocess.run([sys.executable, mk_state, "--data", data_2r, "run", "init", "--id", rid, "--feature", "f", "--branch", "b", "--force"], check=True, capture_output=True)
            subprocess.run([sys.executable, mk_state, "--data", data_2r, "--run", rid, "task", "add", "--id", tid, "--title", f"ask lane {rid}"], check=True, capture_output=True)
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
            run_ = read_json(os.path.join(data_2r, "runs", rid, "run.json"))
            return next(t["status"] for t in run_["tasks"] if t["id"] == tid)

        toks_2r = {}
        for rid, tid in (("r6", "T6"), ("r7", "T7")):
            lane_2r = os.path.join(tmp, f"lane_2r_{rid}")
            os.makedirs(lane_2r, exist_ok=True)
            rc, res_tok_2r, _ = cli(["token", "--lane", lane_2r, "--task", tid, "--run", rid, "--data", data_2r])
            assert rc == 0 and res_tok_2r["ok"] is True, res_tok_2r
            toks_2r[rid] = res_tok_2r["token"]

        ask_2r_a = post_ask_2r(toks_2r["r6"], "first run question")
        ask_2r_b = post_ask_2r(toks_2r["r7"], "second run question")
        assert ask_2r_a["message"]["id"] == "m-1", ask_2r_a
        assert ask_2r_b["message"]["id"] == "m-1", ask_2r_b  # per-run ids: both m-1
        assert ask_2r_a["state_updated"] is True and ask_2r_a["mk_state_error"] is None, ask_2r_a
        # the id-only guard returned state_updated=False here (run A's m-1 had already marked)
        assert ask_2r_b["state_updated"] is True and ask_2r_b["mk_state_error"] is None, ask_2r_b
        assert status_2r("r6", "T6") == "blocked"
        assert status_2r("r7", "T7") == "blocked"  # and T7 stayed building on the id-only guard

        rc, res_2r_ev, _ = cli(["answer", "--data", data_2r, "--run", "r6", "--", "m-1", "--run=evil"])
        assert rc == 0 and res_2r_ev["ok"] is True, res_2r_ev
        assert res_2r_ev["message"]["text"] == "--run=evil", res_2r_ev  # verbatim, not swallowed
        assert res_2r_ev["message"]["run"] == "r6", res_2r_ev  # --run not hijacked by the text

        ask_2r_c = post_ask_2r(toks_2r["r6"], "third question")
        ask_2r_c_id = ask_2r_c["message"]["id"]  # m-3: the --run=evil answer took m-2
        rc, res_2r_x, _ = cli(["answer", "--data", data_2r, "--run", "r6", "--", ask_2r_c_id, "-x"])
        assert rc == 0 and res_2r_x["ok"] is True, res_2r_x
        assert res_2r_x["message"]["text"] == "-x" and res_2r_x["message"]["run"] == "r6", res_2r_x
        done("ask-once keyed (run, id): two runs' m-1 asks both block; answer -- stores text verbatim")

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

    _store_self_test()
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
    p_send.add_argument("--kind", required=True, choices=["ask", "answer", "info", "ack"], help="Message kind")
    p_send.add_argument("--text", required=True, help="Message text")
    p_send.add_argument("--from", dest="from_sender", help="Sender identifier")
    p_send.add_argument("--to", dest="to_recipient", help="Recipient identifier")
    p_send.add_argument("--reply-to", dest="reply_to", help="Replied message id")
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
    p_metrics.add_argument("--run", required=True)
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
    p_watch.add_argument("--after", default="0", help="Resume after this message id or seq")
    p_watch.add_argument("--max-messages", type=int, default=0, help="Exit after N lines (0 = never)")
    p_watch.add_argument("--data", help="MatrixKoder data dir")

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
        }
        fn = dispatch.get(args.cmd)
        if fn:
            fn(args)
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
