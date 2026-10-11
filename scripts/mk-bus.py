#!/usr/bin/env python3
"""MatrixKoder loopback message bus — SSE / HTTP pubsub under flock. Python 3 stdlib only.

Subcommands:
  serve [--data DIR] [--run ID] [--port PORT]
      Runs foreground loopback HTTP/SSE server. Holds <data>/bus.lock exclusively (0600),
      writes <data>/bus.json {pid, port, started, owner_heartbeat, token} (0600), registers token in
      <data>/bus-tokens.json (0600). Rebuilds state from <data>/runs/<run>/bus.jsonl.
      Self-exits when owner_heartbeat is older than 30 min (or $MK_BUS_HEARTBEAT_TIMEOUT_SEC)
      or when bus.json is missing.
  ensure [--data DIR] [--run ID] [--port PORT]
      Ensures a detached server process is running. Idempotent. Returns
      {"ok": true, "pid": PID, "port": PORT, "started": BOOL}.
  heartbeat [--data DIR]
      Updates owner_heartbeat in <data>/bus.json.
  token --lane DIR --task T [--run ID] [--data DIR]
      Mints bearer token, writes <lane>/.matrixkoder-bus.json (0600) with
      {"v": 1, "url": "http://127.0.0.1:<port>", "token": TOKEN, "run": RUN, "task": TASK},
      and registers in <data>/bus-tokens.json (0600). The reserved task id "orch" is refused.
  revoke --lane DIR [--data DIR]
      Removes <lane>/.matrixkoder-bus.json and invalidates token.
  send --run ID --task T --kind KIND --text TEXT [--from S] [--to S] [--reply-to ID] [--data DIR]
      Sends a message via POST /send.
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
import argparse, contextlib, datetime, fcntl, hmac, json, math, os, re, secrets, signal, socket, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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


def is_pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user (pid reused after our
        # server exited): treat as not our process.
        return False
    except OSError:
        return False


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


def matches_lane(msg, lane):
    if not lane or lane == "*":
        return True
    to_val = msg.get("to")
    if to_val:
        return to_val == lane or to_val in ("*", "all")
    task_val = msg.get("task")
    return task_val == lane


class BusState:
    def __init__(self, data_dir, default_run=None):
        self.data_dir = data_dir
        self.default_run = default_run
        # RLock: append_message/record_ack/get_pending hold self.lock and call
        # get_run_state, which also takes self.lock. A plain Lock would self-deadlock.
        self.lock = threading.RLock()
        self.tokens = {}
        self.tokens_mtime = 0
        self.runs = {}

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

    def add_token(self, token_str, run, task, lane, role="lane"):
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
                "created_at": now_iso()
            }
            write_json(tokens_path, tokens, mode=0o600)
            with self.lock:
                self.tokens = tokens
                self.tokens_mtime = os.path.getmtime(tokens_path)
        finally:
            fcntl.flock(lfd, fcntl.LOCK_UN)
            os.close(lfd)

    def get_run_state(self, run):
        if not run:
            run = self.default_run or "default"
        if not is_valid_id(run):
            raise Fail("invalid run id")

        with self.lock:
            if run in self.runs:
                return self.runs[run]

            jsonl_path = os.path.join(self.data_dir, "runs", run, "bus.jsonl")
            messages = []
            by_id = {}
            max_seq = 0
            if os.path.exists(jsonl_path):
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
            self.runs[run] = rstate
            return rstate

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

    def append_message(self, run, task, from_val, to_val, kind, text, reply_to):
        if not is_valid_id(run):
            raise Fail("invalid run id")
        if task and not is_valid_id(task):
            raise Fail("invalid task id")

        with self.lock:
            rstate = self.get_run_state(run)
            seq = rstate["next_seq"]
            rstate["next_seq"] += 1
            msg_id = f"m-{seq}"
            ts = now_iso()
            msg = {
                "seq": seq,
                "id": msg_id,
                "ts": ts,
                "run": run,
                "task": task or None,
                "from": from_val,
                "to": to_val,
                "kind": kind,
                "reply_to": reply_to or None,
                "text": text,
                "ack_level": 1
            }

            run_dir = os.path.join(self.data_dir, "runs", run)
            line = json.dumps(msg, separators=(",", ":")) + "\n"
            self._append_jsonl(run_dir, line)

            rstate["messages"].append(msg)
            rstate["by_id"][msg_id] = msg
            rstate["cond"].notify_all()
            return msg

    def record_ack(self, run, target_id, ack_level, from_val, to_val="orch"):
        if not is_valid_id(run):
            raise Fail("invalid run id")

        with self.lock:
            rstate = self.get_run_state(run)
            target_msg = rstate["by_id"].get(target_id)
            target_task = target_msg.get("task") if target_msg else None

            seq = rstate["next_seq"]
            rstate["next_seq"] += 1
            ack_id = f"m-{seq}"
            ts = now_iso()
            ack_rec = {
                "seq": seq,
                "id": ack_id,
                "ts": ts,
                "run": run,
                "task": target_task,
                "from": from_val or "client",
                "to": to_val or "orch",
                "kind": "ack",
                "reply_to": target_id,
                "text": f"ack {ack_level}",
                "ack_level": ack_level
            }

            run_dir = os.path.join(self.data_dir, "runs", run)
            line = json.dumps(ack_rec, separators=(",", ":")) + "\n"
            self._append_jsonl(run_dir, line)

            if target_msg:
                target_msg["ack_level"] = max(target_msg.get("ack_level", 1), ack_level)

            rstate["cond"].notify_all()
            return True

    def get_pending(self, run):
        with self.lock:
            if run:
                if not is_valid_id(run):
                    raise Fail("invalid run id")
                runs_to_check = [self.get_run_state(run)]
            else:
                runs_dir = os.path.join(self.data_dir, "runs")
                names = set(self.runs.keys())
                if os.path.exists(runs_dir):
                    for d in os.listdir(runs_dir):
                        if os.path.isdir(os.path.join(runs_dir, d)) and is_valid_id(d):
                            names.add(d)
                runs_to_check = [self.get_run_state(r) for r in names]

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

    def do_GET(self):
        try:
            if not self.validate_request():
                return

            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

            if path == "/health":
                uptime = time.time() - getattr(self.server, "started_at", time.time())
                self.send_json(200, {
                    "ok": True,
                    "status": "healthy",
                    "pid": os.getpid(),
                    "uptime_sec": round(uptime, 3)
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

                rstate = self.server.bus_state.get_run_state(run)

                while True:
                    to_send = []
                    with rstate["cond"]:
                        for m in rstate["messages"]:
                            if m["seq"] > last_seq and matches_lane(m, lane):
                                to_send.append(m)
                                m["ack_level"] = max(m.get("ack_level", 1), 2)
                                last_seq = m["seq"]

                        if not to_send:
                            rstate["cond"].wait(timeout=15.0)
                            for m in rstate["messages"]:
                                if m["seq"] > last_seq and matches_lane(m, lane):
                                    to_send.append(m)
                                    m["ack_level"] = max(m.get("ack_level", 1), 2)
                                    last_seq = m["seq"]

                    if to_send:
                        for m in to_send:
                            ev = f"id: {m['seq']}\nevent: message\ndata: {json.dumps(m)}\n\n"
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
                rstate = self.server.bus_state.get_run_state(run)

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

                pending = self.server.bus_state.get_pending(run)
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

            is_orch = tok.get("role") == "orch"

            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            try:
                content_len = int(self.headers.get("Content-Length", 0))
            except ValueError:
                content_len = 0

            if content_len > 65536:
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
                if kind not in ("ask", "answer", "info", "ack"):
                    self.send_error_json(400, "invalid kind")
                    return

                text = body.get("text")
                if text is None or not isinstance(text, str):
                    self.send_error_json(400, "text must be a string")
                    return
                if len(text.encode("utf-8")) > 65536:
                    self.send_error_json(400, "text exceeds 64 KiB")
                    return
                # B18: text is non-empty for ask/answer/info; empty is allowed
                # only for ack.
                if kind in ("ask", "answer", "info") and not text:
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
                if kind in ("ask", "info") and reply_to is not None:
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
                    if kind == "answer":
                        self.send_error_json(403, "lane tokens cannot send answers")
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
                    # Orchestrator token
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

                rstate = self.server.bus_state.get_run_state(run)

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
                    self.server.bus_state.record_ack(
                        run=run,
                        target_id=reply_to,
                        ack_level=ack_level,
                        from_val=from_val,
                        to_val=to_val,
                    )
                    target_msg = rstate["by_id"][reply_to]
                    self.send_json(200, {
                        "ok": True,
                        "id": reply_to,
                        "ack_level": target_msg.get("ack_level"),
                        "ack_recorded": True,
                    })
                    return

                msg = self.server.bus_state.append_message(
                    run=run,
                    task=task,
                    from_val=from_val,
                    to_val=to_val,
                    kind=kind,
                    text=text,
                    reply_to=reply_to
                )
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

                rstate = self.server.bus_state.get_run_state(run)
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

                self.server.bus_state.record_ack(
                    run=run,
                    target_id=target_id,
                    ack_level=ack_level,
                    from_val=from_val,
                    to_val=target_msg.get("from") or "orch"
                )
                self.send_json(200, {"ok": True, "id": target_id, "ack_level": ack_level})

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
    timeout = float(os.environ.get("MK_BUS_HEARTBEAT_TIMEOUT_SEC", "1800.0"))
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
    lock_path = os.path.join(data_dir, "bus.lock")
    bus_json_path = os.path.join(data_dir, "bus.json")

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(lock_fd, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        out({"ok": False, "error": "bus already running"})
        sys.exit(2)

    port = getattr(args, "port", 0) or 0
    server = BusServer(("127.0.0.1", port), RequestHandler)
    server.started_at = time.time()
    server.bus_state = BusState(data_dir, getattr(args, "run", None))
    actual_port = server.server_address[1]

    orch_token = secrets.token_urlsafe(32)
    server.bus_state.add_token(orch_token, getattr(args, "run", "*") or "*", "orch", "orch", role="orch")

    bus_data = {
        "pid": os.getpid(),
        "port": actual_port,
        "started": server.started_at,
        "owner_heartbeat": server.started_at,
        "token": orch_token
    }
    write_json(bus_json_path, bus_data, mode=0o600)

    stop_event = threading.Event()
    hb_thread = threading.Thread(
        target=heartbeat_monitor,
        args=(server, data_dir, stop_event),
        daemon=True
    )
    hb_thread.start()

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
                if d and d.get("pid") == os.getpid():
                    os.remove(bus_json_path)
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
    ensure_lock_path = os.path.join(data_dir, "bus-ensure.lock")
    bus_json_path = os.path.join(data_dir, "bus.json")

    # Serialize ensure calls with dedicated flock
    efd = os.open(ensure_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(efd, 0o600)
    fcntl.flock(efd, fcntl.LOCK_EX)
    try:
        # Check if server is already running. bus.json is only removed when
        # the recorded pid is DEAD: a live server whose /health probe times
        # out once (busy box, slow start) must never be declared down — the
        # old remove-and-respawn behavior made the healthy server self-exit
        # (review LOW 5). Probe with retries while the pid lives.
        if os.path.exists(bus_json_path):
            bdata = read_json(bus_json_path)
            pid = (bdata or {}).get("pid")
            port = (bdata or {}).get("port")
            if pid and port and is_pid_alive(pid):
                probe_deadline = time.time() + 5.0
                while time.time() < probe_deadline:
                    try:
                        req = urllib.request.Request(
                            f"http://127.0.0.1:{port}/health",
                            headers={"Host": f"127.0.0.1:{port}"}
                        )
                        with urllib.request.urlopen(req, timeout=1.0) as resp:
                            if resp.status == 200:
                                # Refresh heartbeat
                                bdata["owner_heartbeat"] = time.time()
                                write_json(bus_json_path, bdata, mode=0o600)
                                res = {"ok": True, "pid": pid, "port": port, "started": False}
                                out(res)
                                return res
                    except Exception:
                        pass
                    if not is_pid_alive(pid):
                        # Died under our probe: fall through to restart.
                        break
                    time.sleep(0.2)
                else:
                    # pid alive but /health never answered within the retry
                    # window. The server holds bus.lock, so a new serve would
                    # just exit; refuse instead of destroying live state.
                    raise Fail("bus process alive but not answering health probes")

        # Not running (no bus.json, unreadable bus.json, or dead pid):
        # start a detached serve process
        if os.path.exists(bus_json_path):
            with contextlib.suppress(OSError):
                os.remove(bus_json_path)

        cmd = [sys.executable, os.path.abspath(__file__), "serve", "--data", data_dir]
        if getattr(args, "run", None):
            cmd.extend(["--run", args.run])
        if getattr(args, "port", None):
            cmd.extend(["--port", str(args.port)])

        p = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        deadline = time.time() + 10.0
        while time.time() < deadline:
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
    bus_json_path = os.path.join(data_dir, "bus.json")
    if not os.path.exists(bus_json_path):
        raise Fail("bus down")
    bdata = read_json(bus_json_path)
    if not bdata or "pid" not in bdata:
        raise Fail("bus down")

    bdata["owner_heartbeat"] = time.time()
    write_json(bus_json_path, bdata, mode=0o600)
    res = {"ok": True, "owner_heartbeat": bdata["owner_heartbeat"]}
    out(res)
    return res


def cmd_token(args):
    data_dir = resolve_data(getattr(args, "data", None))
    lane_dir = os.path.abspath(args.lane)
    os.makedirs(lane_dir, exist_ok=True)

    if not is_valid_id(args.task):
        raise Fail("invalid task id")
    # Reserved sender identity: a lane token must never carry it (review LOW 2)
    if args.task == "orch":
        raise Fail("task id 'orch' is reserved")
    run_val = getattr(args, "run", "") or ""
    if run_val and not is_valid_id(run_val):
        raise Fail("invalid run id")

    bus_json_path = os.path.join(data_dir, "bus.json")
    port = None
    if os.path.exists(bus_json_path):
        d = read_json(bus_json_path)
        if d:
            port = d.get("port")
    if not port:
        port = 0

    tok = secrets.token_urlsafe(32)
    lane_file = os.path.join(lane_dir, ".matrixkoder-bus.json")
    # "v": 1 matches the contract B24 lane-file example (protocol version).
    payload = {
        "v": 1,
        "url": f"http://127.0.0.1:{port}",
        "token": tok,
        "run": run_val,
        "task": args.task
    }

    fd = os.open(lane_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.chmod(lane_file, 0o600)

    lock_path = os.path.join(data_dir, "bus-tokens.lock")
    tokens_path = os.path.join(data_dir, "bus-tokens.json")
    lfd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(lfd, 0o600)
    fcntl.flock(lfd, fcntl.LOCK_EX)
    try:
        tokens = read_json(tokens_path, default={})
        tokens[tok] = {
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
    lane_dir = os.path.abspath(args.lane)
    lane_file = os.path.join(lane_dir, ".matrixkoder-bus.json")
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

    lock_path = os.path.join(data_dir, "bus-tokens.lock")
    tokens_path = os.path.join(data_dir, "bus-tokens.json")
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
    bus_json_path = os.path.join(data_dir, "bus.json")
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

    # Message ids are per run ("m-1" exists in every run), so without --run
    # the id can match in several runs. Collect every match instead of
    # answering whichever run os.listdir happens to return first (review
    # MEDIUM 2), then refuse when it is ambiguous.
    matches = []
    for r in runs_to_search:
        jsonl = os.path.join(runs_dir, r, "bus.jsonl")
        if os.path.exists(jsonl):
            try:
                with open(jsonl, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            try:
                                m = json.loads(line)
                            except Exception:
                                continue
                            if not isinstance(m, dict):
                                continue
                            if m.get("id") == args.msg_id:
                                matches.append((r, m))
                                break
            except Exception:
                pass

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
        mk_state_cmd = os.environ.get("MK_STATE_CMD") or os.path.join(HERE, "mk-state.py")
        cmd = [sys.executable, mk_state_cmd, "--data", data_dir, "--run", target_run, "task", "set", task, "status=building", "--note", "bus answer"]
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if p.returncode == 0:
                state_updated = True
            else:
                mk_state_error = (p.stderr.strip() or p.stdout.strip())
        except Exception as e:
            mk_state_error = str(e)

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
    if not run or not is_valid_id(run):
        raise Fail("missing or invalid --run")

    lane_filter = getattr(args, "lane", None)
    after_seq = getattr(args, "after", 0) or 0
    jsonl_path = os.path.join(data_dir, "runs", run, "bus.jsonl")
    n = getattr(args, "n", 10) or 10

    messages = []
    if os.path.exists(jsonl_path):
        with open(jsonl_path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        for l in lines:
            try:
                msg = json.loads(l)
                if not isinstance(msg, dict):
                    continue
                if after_seq and msg.get("seq", 0) <= after_seq:
                    continue
                if lane_filter and not matches_lane(msg, lane_filter):
                    continue
                messages.append(msg)
            except Exception:
                pass
    res = {"ok": True, "messages": messages[-n:] if n > 0 else messages}
    out(res)
    return res


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

    finally:
        for p in pids:
            stop_process(p)
        with contextlib.suppress(OSError):
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

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

    p_ensure = subparsers.add_parser("ensure")
    p_ensure.add_argument("--data", help="MatrixKoder data dir")
    p_ensure.add_argument("--run", help="Run id")
    p_ensure.add_argument("--port", type=int, default=0, help="Port to bind if starting")

    p_hb = subparsers.add_parser("heartbeat")
    p_hb.add_argument("--data", help="MatrixKoder data dir")

    p_token = subparsers.add_parser("token")
    p_token.add_argument("--lane", required=True, help="Lane worktree directory")
    p_token.add_argument("--task", required=True, help="Task identifier")
    p_token.add_argument("--run", default="", help="Run identifier")
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
    p_tail.add_argument("--run", help="Run identifier")
    p_tail.add_argument("--lane", help="Lane filter")
    p_tail.add_argument("--after", type=int, default=0, help="After seq filter")
    p_tail.add_argument("--n", type=int, default=10, help="Number of messages (default 10)")
    p_tail.add_argument("--data", help="MatrixKoder data dir")

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
            "tail": cmd_tail
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
