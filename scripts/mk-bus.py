#!/usr/bin/env python3
"""MatrixKoder loopback message bus — SSE / HTTP pubsub under flock. Python 3 stdlib only.

Subcommands:
  serve [--data DIR] [--run ID] [--port PORT]
      Runs foreground loopback HTTP/SSE server. Holds <data>/bus.lock exclusively,
      writes <data>/bus.json {pid, port, started, owner_heartbeat, token}, registers token in
      <data>/bus-tokens.json. Rebuilds state from <data>/runs/<run>/bus.jsonl.
      Self-exits when owner_heartbeat is older than 30 min (or $MK_BUS_HEARTBEAT_TIMEOUT_SEC).
  ensure [--data DIR] [--run ID] [--port PORT]
      Ensures a detached server process is running. Idempotent. Returns
      {"ok": true, "pid": PID, "port": PORT, "started": BOOL}.
  heartbeat [--data DIR]
      Updates owner_heartbeat in <data>/bus.json.
  token --lane DIR --task T [--run ID] [--data DIR]
      Mints bearer token, writes <lane>/.matrixkoder-bus.json (chmod 0600) with
      {"url": "http://127.0.0.1:<port>", "token": TOKEN, "run": RUN, "task": TASK},
      and registers in <data>/bus-tokens.json.
  revoke --lane DIR [--data DIR]
      Removes <lane>/.matrixkoder-bus.json and invalidates token.
  send --run ID --task T --kind KIND --text TEXT [--from S] [--to S] [--reply-to ID] [--data DIR]
      Sends a message via POST /send. If server is down, prints {"ok": false, "error": "bus down"}
      and exits non-zero without traceback.
  answer MSG_ID TEXT [--run ID] [--data DIR]
      Sends answer message with reply_to=MSG_ID. Also runs
      scripts/mk-state.py --run ID task set <T> status=building --note "bus answer".
  pending [--run ID] [--data DIR]
      Lists asks without an answer where reply_to == ask.id.
  tail [--run ID] [--n N] [--data DIR]
      Lists last N messages from bus.jsonl.
  --self-test
      Comprehensive self-test of server, round trip, auth, replay, lifecycle, error handling.

Every command prints ONE JSON line. Errors: {"ok": false, "error": ...} exit 2.
"""
import argparse, contextlib, datetime, fcntl, json, os, secrets, signal, socket, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))


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
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path, obj):
    """Atomic write: temp file in same dir, fsync, os.replace."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
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
    except OSError:
        return False


def stop_process(pid, timeout=3.0):
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
        self.lock = threading.Lock()
        self.tokens = {}
        self.tokens_mtime = 0
        self.runs = {}

    def get_token(self, token_str):
        if not token_str:
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
            return self.tokens.get(token_str)

    def add_token(self, token_str, run, task, lane):
        lock_path = os.path.join(self.data_dir, "bus-tokens.lock")
        tokens_path = os.path.join(self.data_dir, "bus-tokens.json")
        os.makedirs(self.data_dir, exist_ok=True)
        lfd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lfd, fcntl.LOCK_EX)
        try:
            tokens = read_json(tokens_path, default={})
            tokens[token_str] = {
                "token": token_str,
                "run": run or "",
                "task": task or None,
                "lane": lane or None,
                "created_at": now_iso()
            }
            write_json(tokens_path, tokens)
            with self.lock:
                self.tokens = tokens
                self.tokens_mtime = os.path.getmtime(tokens_path)
        finally:
            fcntl.flock(lfd, fcntl.LOCK_UN)
            os.close(lfd)

    def get_run_state(self, run):
        if not run:
            run = self.default_run or "default"
        if run in self.runs:
            return self.runs[run]

        jsonl_path = os.path.join(self.data_dir, "runs", run, "bus.jsonl")
        messages = []
        by_id = {}
        max_seq = 0
        if os.path.exists(jsonl_path):
            with open(jsonl_path, "r", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        seq = obj.get("seq", 0)
                        if seq > max_seq:
                            max_seq = seq
                        if obj.get("kind") == "ack":
                            target_id = obj.get("reply_to")
                            if target_id and target_id in by_id:
                                by_id[target_id]["ack_level"] = max(
                                    by_id[target_id].get("ack_level", 1),
                                    obj.get("ack_level", 1)
                                )
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

    def append_message(self, run, task, from_val, to_val, kind, text, reply_to):
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
            os.makedirs(run_dir, exist_ok=True)
            jsonl_path = os.path.join(run_dir, "bus.jsonl")
            line = json.dumps(msg, separators=(",", ":")) + "\n"
            with open(jsonl_path, "a", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            rstate["messages"].append(msg)
            rstate["by_id"][msg_id] = msg
            rstate["cond"].notify_all()
            return msg

    def record_ack(self, run, target_id, ack_level, from_val):
        with self.lock:
            if not run:
                for r_name, r_st in self.runs.items():
                    if target_id in r_st["by_id"]:
                        run = r_name
                        break
            if not run:
                run = self.default_run or "default"

            rstate = self.get_run_state(run)
            target_msg = rstate["by_id"].get(target_id)
            target_task = target_msg.get("task") if target_msg else None
            target_to = target_msg.get("from") if target_msg else "orch"

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
                "to": target_to,
                "kind": "ack",
                "reply_to": target_id,
                "text": f"ack {ack_level}",
                "ack_level": ack_level
            }

            run_dir = os.path.join(self.data_dir, "runs", run)
            os.makedirs(run_dir, exist_ok=True)
            jsonl_path = os.path.join(run_dir, "bus.jsonl")
            line = json.dumps(ack_rec, separators=(",", ":")) + "\n"
            with open(jsonl_path, "a", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            if target_msg:
                target_msg["ack_level"] = max(target_msg.get("ack_level", 1), ack_level)

            rstate["cond"].notify_all()
            return True

    def get_pending(self, run):
        with self.lock:
            if run:
                runs_to_check = [self.get_run_state(run)]
            else:
                runs_dir = os.path.join(self.data_dir, "runs")
                names = set(self.runs.keys())
                if os.path.exists(runs_dir):
                    for d in os.listdir(runs_dir):
                        if os.path.isdir(os.path.join(runs_dir, d)):
                            names.add(d)
                runs_to_check = [self.get_run_state(r) for r in names]

            pending = []
            for rstate in runs_to_check:
                answered = {
                    m["reply_to"] for m in rstate["messages"]
                    if m.get("kind") == "answer" and m.get("reply_to")
                }
                for m in rstate["messages"]:
                    if m.get("kind") == "ask" and m["id"] not in answered:
                        pending.append(m)
            return pending


class RequestHandler(BaseHTTPRequestHandler):
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

    def validate_request(self):
        if "Origin" in self.headers:
            self.send_error_json(403, "origin not allowed")
            return False

        host = self.headers.get("Host", "")
        host_name = host.split(":")[0].lower() if host else ""
        if host_name not in ("127.0.0.1", "localhost"):
            self.send_error_json(400, "invalid host")
            return False

        return True

    def check_auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self.send_error_json(401, "unauthorized")
            return None
        tok_str = auth[len("Bearer "):].strip()
        tok = self.server.bus_state.get_token(tok_str)
        if not tok:
            self.send_error_json(401, "unauthorized")
            return None
        return tok

    def do_GET(self):
        if not self.validate_request():
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if path == "/health":
            self.send_json(200, {"ok": True, "status": "healthy", "pid": os.getpid()})
            return

        tok = self.check_auth()
        if not tok:
            return

        if path == "/events":
            lane = params.get("lane") or params.get("task") or ""
            run = params.get("run") or tok.get("run") or self.server.bus_state.default_run
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
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            rstate = self.server.bus_state.get_run_state(run)
            with self.server.bus_state.lock:
                for m in rstate["messages"]:
                    if m["seq"] > last_seq and matches_lane(m, lane):
                        ev = f"id: {m['seq']}\nevent: message\ndata: {json.dumps(m)}\n\n"
                        try:
                            self.wfile.write(ev.encode("utf-8"))
                            self.wfile.flush()
                            m["ack_level"] = max(m.get("ack_level", 1), 2)
                            last_seq = m["seq"]
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            return

            while True:
                with rstate["cond"]:
                    rstate["cond"].wait(timeout=1.0)
                    for m in rstate["messages"]:
                        if m["seq"] > last_seq and matches_lane(m, lane):
                            ev = f"id: {m['seq']}\nevent: message\ndata: {json.dumps(m)}\n\n"
                            try:
                                self.wfile.write(ev.encode("utf-8"))
                                self.wfile.flush()
                                m["ack_level"] = max(m.get("ack_level", 1), 2)
                                last_seq = m["seq"]
                            except (BrokenPipeError, ConnectionResetError, OSError):
                                return

        elif path == "/wait":
            lane = params.get("lane") or params.get("task") or ""
            run = params.get("run") or tok.get("run") or self.server.bus_state.default_run
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
            except ValueError:
                timeout = 30.0
            timeout = min(max(timeout, 0.1), 90.0)

            deadline = time.time() + timeout
            rstate = self.server.bus_state.get_run_state(run)

            with rstate["cond"]:
                while True:
                    matching = None
                    for m in rstate["messages"]:
                        if m["seq"] > after_seq and matches_lane(m, lane):
                            matching = m
                            break
                    if matching:
                        matching["ack_level"] = max(matching.get("ack_level", 1), 2)
                        self.send_json(200, {"ok": True, "message": matching})
                        return

                    remaining = deadline - time.time()
                    if remaining <= 0:
                        self.send_json(200, {"ok": True, "message": None})
                        return

                    rstate["cond"].wait(timeout=min(remaining, 0.5))

        elif path == "/pending":
            run = params.get("run") or tok.get("run")
            if run == "*":
                run = None
            pending = self.server.bus_state.get_pending(run)
            self.send_json(200, {"ok": True, "pending": pending})

        else:
            self.send_error_json(404, "not found")

    def do_POST(self):
        if not self.validate_request():
            return

        tok = self.check_auth()
        if not tok:
            return

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

        if path == "/send":
            run = body.get("run") or tok.get("run") or self.server.bus_state.default_run
            if not run or run == "*":
                self.send_error_json(400, "missing run")
                return

            task = body.get("task") or tok.get("task")
            from_val = body.get("from") or tok.get("task") or "orch"
            to_val = body.get("to") or task or "orch"
            kind = body.get("kind") or "info"
            text = body.get("text") or ""
            reply_to = body.get("reply_to")

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
            target_id = body.get("id")
            if not target_id:
                self.send_error_json(400, "missing id")
                return

            ack_level = int(body.get("ack_level", 3))
            run = body.get("run") or tok.get("run")
            if run == "*":
                run = None
            from_val = tok.get("task") or "client"

            self.server.bus_state.record_ack(
                run=run,
                target_id=target_id,
                ack_level=ack_level,
                from_val=from_val
            )
            self.send_json(200, {"ok": True})

        else:
            self.send_error_json(404, "not found")


class BusServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass


def heartbeat_monitor(server, data_dir, stop_event):
    check_interval = float(os.environ.get("MK_BUS_HEARTBEAT_CHECK_SEC", "5.0"))
    timeout = float(os.environ.get("MK_BUS_HEARTBEAT_TIMEOUT_SEC", "1800.0"))
    bus_json_path = os.path.join(data_dir, "bus.json")

    while not stop_event.wait(check_interval):
        try:
            if not os.path.exists(bus_json_path):
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

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        out({"ok": False, "error": "bus already running"})
        sys.exit(2)

    port = getattr(args, "port", 0) or 0
    server = BusServer(("127.0.0.1", port), RequestHandler)
    server.bus_state = BusState(data_dir, getattr(args, "run", None))
    actual_port = server.server_address[1]

    orch_token = secrets.token_urlsafe(32)
    server.bus_state.add_token(orch_token, getattr(args, "run", "*") or "*", "orch", "orch")

    bus_data = {
        "pid": os.getpid(),
        "port": actual_port,
        "started": time.time(),
        "owner_heartbeat": time.time(),
        "token": orch_token
    }
    write_json(bus_json_path, bus_data)

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
    lock_path = os.path.join(data_dir, "bus.lock")
    bus_json_path = os.path.join(data_dir, "bus.json")

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    locked = False
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
    except (BlockingIOError, OSError):
        locked = False

    if not locked:
        os.close(lock_fd)
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
                            res = {"ok": True, "pid": pid, "port": port, "started": False}
                            out(res)
                            return res
            except Exception:
                pass
        raise Fail("bus lock held but server unresponsive")

    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)

    if os.path.exists(bus_json_path):
        try:
            os.remove(bus_json_path)
        except OSError:
            pass

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

    deadline = time.time() + 5.0
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
                            res = {"ok": True, "pid": pid, "port": port, "started": True}
                            out(res)
                            return res
            except Exception:
                pass
        time.sleep(0.05)

    raise Fail("server failed to start within timeout")


def cmd_heartbeat(args):
    data_dir = resolve_data(getattr(args, "data", None))
    bus_json_path = os.path.join(data_dir, "bus.json")
    if not os.path.exists(bus_json_path):
        raise Fail("bus not running")
    bdata = read_json(bus_json_path)
    if not bdata:
        raise Fail("bus not running")
    bdata["owner_heartbeat"] = time.time()
    write_json(bus_json_path, bdata)
    res = {"ok": True, "owner_heartbeat": bdata["owner_heartbeat"]}
    out(res)
    return res


def cmd_token(args):
    data_dir = resolve_data(getattr(args, "data", None))
    bus_json_path = os.path.join(data_dir, "bus.json")
    if not os.path.exists(bus_json_path):
        raise Fail("bus down")
    bdata = read_json(bus_json_path)
    if not bdata or "port" not in bdata:
        raise Fail("bus down")
    port = bdata["port"]

    tok = secrets.token_urlsafe(32)
    lane_dir = os.path.abspath(args.lane)
    os.makedirs(lane_dir, exist_ok=True)
    lane_file = os.path.join(lane_dir, ".matrixkoder-bus.json")
    run_val = getattr(args, "run", "") or ""
    payload = {
        "url": f"http://127.0.0.1:{port}",
        "token": tok,
        "run": run_val,
        "task": args.task
    }
    fd = os.open(lane_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    lock_path = os.path.join(data_dir, "bus-tokens.lock")
    tokens_path = os.path.join(data_dir, "bus-tokens.json")
    lfd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(lfd, fcntl.LOCK_EX)
    try:
        tokens = read_json(tokens_path, default={})
        tokens[tok] = {
            "token": tok,
            "run": run_val,
            "task": args.task,
            "lane": lane_dir,
            "created_at": now_iso()
        }
        write_json(tokens_path, tokens)
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
    lfd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
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
        write_json(tokens_path, new_tokens)
    finally:
        fcntl.flock(lfd, fcntl.LOCK_UN)
        os.close(lfd)

    res = {"ok": True, "removed": removed}
    out(res)
    return res


def cmd_send(args):
    data_dir = resolve_data(getattr(args, "data", None))
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
    except Exception:
        raise Fail("bus down")


def cmd_answer(args):
    data_dir = resolve_data(getattr(args, "data", None))
    bus_json_path = os.path.join(data_dir, "bus.json")
    if not os.path.exists(bus_json_path):
        raise Fail("bus down")
    bdata = read_json(bus_json_path)
    if not bdata or "port" not in bdata:
        raise Fail("bus down")
    port = bdata["port"]
    token = bdata.get("token")
    if not token:
        raise Fail("bus down")

    runs_dir = os.path.join(data_dir, "runs")
    target_run = getattr(args, "run", None)
    runs_to_search = [target_run] if target_run else []
    if not target_run and os.path.exists(runs_dir):
        runs_to_search = [d for d in os.listdir(runs_dir) if os.path.isdir(os.path.join(runs_dir, d))]

    target_msg = None
    for r in runs_to_search:
        jsonl = os.path.join(runs_dir, r, "bus.jsonl")
        if os.path.exists(jsonl):
            try:
                with open(jsonl, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            m = json.loads(line)
                            if m.get("id") == args.msg_id:
                                target_msg = m
                                target_run = r
                                break
            except Exception:
                pass
        if target_msg:
            break

    if not target_run:
        target_run = getattr(args, "run", None)
    if not target_run:
        raise Fail(f"cannot find run for message {args.msg_id}")

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
    except Exception:
        raise Fail("bus down")

    if task:
        mk_state_cmd = os.environ.get("MK_STATE_CMD") or os.path.join(HERE, "mk-state.py")
        cmd = [sys.executable, mk_state_cmd, "--run", target_run]
        if getattr(args, "data", None):
            cmd.extend(["--data", data_dir])
        cmd.extend(["task", "set", task, "status=building", "--note", "bus answer"])
        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        except Exception:
            pass

    out(send_res)
    return send_res


def cmd_pending(args):
    data_dir = resolve_data(getattr(args, "data", None))
    bus_json_path = os.path.join(data_dir, "bus.json")
    if not os.path.exists(bus_json_path):
        raise Fail("bus down")
    bdata = read_json(bus_json_path)
    if not bdata or "port" not in bdata:
        raise Fail("bus down")
    port = bdata["port"]
    token = bdata.get("token")
    if not token:
        raise Fail("bus down")

    run = getattr(args, "run", "") or ""
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
    except Exception:
        raise Fail("bus down")


def cmd_tail(args):
    data_dir = resolve_data(getattr(args, "data", None))
    run = getattr(args, "run", None)
    if not run:
        runs_dir = os.path.join(data_dir, "runs")
        if os.path.exists(runs_dir):
            runs = [d for d in os.listdir(runs_dir) if os.path.isdir(os.path.join(runs_dir, d))]
            if len(runs) == 1:
                run = runs[0]
    if not run:
        raise Fail("missing --run")

    jsonl_path = os.path.join(data_dir, "runs", run, "bus.jsonl")
    n = getattr(args, "n", 10) or 10
    messages = []
    if os.path.exists(jsonl_path):
        with open(jsonl_path, "r", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                lines = [l.strip() for l in f if l.strip()]
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        for l in lines[-n:]:
            try:
                messages.append(json.loads(l))
            except Exception:
                pass
    res = {"ok": True, "messages": messages}
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

        rc, res2, _ = cli(["ensure", "--data", data, "--run", "r1"])
        assert rc == 0 and res2["ok"] is True and res2["started"] is False, res2
        assert res2["pid"] == pid1 and res2["port"] == port1, res2

        # 2. Token generation and lane file 0600
        rc, res_tok, _ = cli(["token", "--lane", lane1, "--task", "T1", "--run", "r1", "--data", data])
        assert rc == 0 and res_tok["ok"] is True, res_tok
        token1 = res_tok["token"]
        lane_bus_json = os.path.join(lane1, ".matrixkoder-bus.json")
        assert os.path.exists(lane_bus_json)
        mode = os.stat(lane_bus_json).st_mode & 0o777
        assert mode == 0o600, f"Expected 0600 mode, got {oct(mode)}"

        # 3. wrong token and absent token -> 401, bus.jsonl unchanged
        jsonl_path = os.path.join(data, "runs", "r1", "bus.jsonl")
        count_before = len(open(jsonl_path).readlines()) if os.path.exists(jsonl_path) else 0

        # wrong token
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

        # absent token
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

        count_after = len(open(jsonl_path).readlines()) if os.path.exists(jsonl_path) else 0
        assert count_after == count_before, "bus.jsonl modified after 401 requests"

        # 4. ask -> answer round trip: blocking wait returns answer in < 2s; bus.jsonl contains ask, answer, ack lines
        rc, send_ask, _ = cli(["send", "--run", "r1", "--task", "T1", "--kind", "ask", "--text", "how to proceed?", "--from", "T1", "--to", "orch", "--data", data])
        assert rc == 0 and send_ask["ok"] is True, send_ask
        ask_msg = send_ask["message"]
        assert ask_msg["seq"] == 1 and ask_msg["id"] == "m-1" and ask_msg["kind"] == "ask"
        assert ask_msg["ack_level"] == 1

        # verify pending
        rc, pend, _ = cli(["pending", "--run", "r1", "--data", data])
        assert rc == 0 and len(pend["pending"]) == 1 and pend["pending"][0]["id"] == "m-1", pend

        # client blocking wait in thread
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
        time.sleep(0.1)  # ensure wait is blocked

        t0 = time.time()
        rc, send_ans, _ = cli(["answer", "m-1", "proceed with caution", "--run", "r1", "--data", data])
        assert rc == 0 and send_ans["ok"] is True, send_ans
        ans_msg = send_ans["message"]
        assert ans_msg["kind"] == "answer" and ans_msg["reply_to"] == "m-1"

        th.join(timeout=3.0)
        assert not th.is_alive(), "wait thread did not terminate"
        dt = wait_result.get("dt", 99)
        assert dt < 2.0, f"Blocking wait took {dt:.3f}s >= 2.0s"
        assert wait_result["res"]["ok"] is True
        recv_msg = wait_result["res"]["message"]
        assert recv_msg["id"] == ans_msg["id"] and recv_msg["text"] == "proceed with caution"

        # client sends ack for answer
        req_ack = urllib.request.Request(
            f"http://127.0.0.1:{port1}/ack",
            data=json.dumps({"id": ans_msg["id"], "ack_level": 3, "run": "r1"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token1}", "Host": f"127.0.0.1:{port1}"},
            method="POST"
        )
        with urllib.request.urlopen(req_ack, timeout=2.0) as resp:
            ack_res = json.loads(resp.read().decode("utf-8"))
        assert ack_res["ok"] is True

        # verify bus.jsonl contains ask, answer, ack lines
        lines = [json.loads(line.strip()) for line in open(jsonl_path).readlines() if line.strip()]
        kinds = [l["kind"] for l in lines]
        assert "ask" in kinds and "answer" in kinds and "ack" in kinds, kinds
        for l in lines:
            for k in ("seq", "id", "ts", "run", "task", "from", "to", "kind", "reply_to", "text", "ack_level"):
                assert k in l, f"Field {k} missing in line {l}"

        # verify pending is now empty
        rc, pend2, _ = cli(["pending", "--run", "r1", "--data", data])
        assert rc == 0 and len(pend2["pending"]) == 0, pend2

        # 5. restart: stop server, start again, client with Last-Event-ID: <n> receives only messages after n
        rc, send_info, _ = cli(["send", "--run", "r1", "--task", "T1", "--kind", "info", "--text", "update", "--from", "orch", "--to", "T1", "--data", data])
        assert rc == 0 and send_info["ok"] is True

        # Stop server 1
        stop_process(pid1)
        assert not is_pid_alive(pid1)

        # Start server 2 on same data dir
        rc, res_restart, _ = cli(["ensure", "--data", data, "--run", "r1"])
        assert rc == 0 and res_restart["ok"] is True and res_restart["started"] is True, res_restart
        pid2 = res_restart["pid"]
        port2 = res_restart["port"]
        pids.append(pid2)
        assert pid2 != pid1

        # Reconnect with Last-Event-ID: 2
        req_sse = urllib.request.Request(
            f"http://127.0.0.1:{port2}/events?lane=T1",
            headers={"Authorization": f"Bearer {token1}", "Last-Event-ID": "2", "Host": f"127.0.0.1:{port2}"}
        )
        with urllib.request.urlopen(req_sse, timeout=2.0) as resp:
            # Read first event id line
            first_line = resp.readline().decode("utf-8")
            assert first_line.startswith("id: "), f"Expected id: ..., got {first_line!r}"
            ev_id = int(first_line.split(":")[1].strip())
            assert ev_id > 2, f"Expected event id > 2, got {ev_id}"

        stop_process(pid2)
        assert not is_pid_alive(pid2)

        # 6. stale owner_heartbeat -> server exits by itself
        env_hb = os.environ.copy()
        env_hb["MK_BUS_HEARTBEAT_CHECK_SEC"] = "0.05"
        env_hb["MK_BUS_HEARTBEAT_TIMEOUT_SEC"] = "0.2"
        p_hb = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "serve", "--data", data, "--run", "r1"],
            env=env_hb,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        pids.append(p_hb.pid)

        bus_json_path = os.path.join(data, "bus.json")
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if os.path.exists(bus_json_path):
                break
            time.sleep(0.05)
        assert os.path.exists(bus_json_path)

        # Write stale heartbeat
        bdata = read_json(bus_json_path)
        bdata["owner_heartbeat"] = time.time() - 50.0
        write_json(bus_json_path, bdata)

        ret = p_hb.wait(timeout=2.0)
        assert ret == 0, f"Expected exit code 0, got {ret}"
        assert not os.path.exists(bus_json_path), "bus.json should be removed on exit"

        # 7. server down -> send prints {"ok": false, "error": "bus down"} and exits non-zero without traceback
        rc, res_down, err = cli(["send", "--run", "r1", "--task", "T1", "--kind", "ask", "--text", "hi", "--data", data], check=False)
        assert rc != 0, f"Expected non-zero exit, got {rc}"
        assert res_down == {"ok": False, "error": "bus down"}, res_down
        assert "Traceback" not in err, f"Unexpected traceback in stderr: {err}"

        # 8. answer calls mk-state: points at temp --data with run and task created via real scripts/mk-state.py, verifies task status became building
        data_state = os.path.join(tmp, "data_state")
        os.makedirs(data_state, exist_ok=True)
        mk_state = os.path.join(HERE, "mk-state.py")

        subprocess.run([sys.executable, mk_state, "--data", data_state, "run", "init", "--id", "r2", "--feature", "f", "--branch", "b", "--force"], check=True, capture_output=True)
        subprocess.run([sys.executable, mk_state, "--data", data_state, "--run", "r2", "task", "add", "--id", "T2", "--title", "task two"], check=True, capture_output=True)
        subprocess.run([sys.executable, mk_state, "--data", data_state, "--run", "r2", "task", "set", "T2", "status=blocked"], check=True, capture_output=True)

        p_get = subprocess.run([sys.executable, mk_state, "--data", data_state, "--run", "r2", "task", "get", "T2"], stdout=subprocess.PIPE, text=True, check=True)
        assert json.loads(p_get.stdout)["task"]["status"] == "blocked"

        rc, res_ens_st, _ = cli(["ensure", "--data", data_state, "--run", "r2"])
        assert rc == 0 and res_ens_st["ok"] is True
        pids.append(res_ens_st["pid"])

        rc, res_ask2, _ = cli(["send", "--run", "r2", "--task", "T2", "--kind", "ask", "--text", "how to build", "--from", "T2", "--to", "orch", "--data", data_state])
        assert rc == 0 and res_ask2["ok"] is True
        ask2_id = res_ask2["message"]["id"]

        rc, res_ans2, _ = cli(["answer", ask2_id, "build like this", "--run", "r2", "--data", data_state])
        assert rc == 0 and res_ans2["ok"] is True

        p_get2 = subprocess.run([sys.executable, mk_state, "--data", data_state, "--run", "r2", "task", "get", "T2"], stdout=subprocess.PIPE, text=True, check=True)
        assert json.loads(p_get2.stdout)["task"]["status"] == "building"

    finally:
        for p in pids:
            stop_process(p)
        with contextlib.suppress(OSError):
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    print("pass mk-bus.py: mk-bus self-test ok")
    print("mk-bus self-test ok")
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

    common = Parser(add_help=False)
    common.add_argument("--data", help="data directory")

    parser = Parser(description="MatrixKoder loopback SSE message bus", parents=[common])
    subs = parser.add_subparsers(dest="cmd")

    # serve
    p_serve = subs.add_parser("serve", parents=[common])
    p_serve.add_argument("--run", help="run id")
    p_serve.add_argument("--port", type=int, default=0, help="port to bind (0 for ephemeral)")

    # ensure
    p_ensure = subs.add_parser("ensure", parents=[common])
    p_ensure.add_argument("--run", help="run id")
    p_ensure.add_argument("--port", type=int, default=0, help="port to bind")

    # heartbeat
    subs.add_parser("heartbeat", parents=[common])

    # token
    p_token = subs.add_parser("token", parents=[common])
    p_token.add_argument("--lane", required=True, help="lane directory")
    p_token.add_argument("--task", required=True, help="task id")
    p_token.add_argument("--run", help="run id")

    # revoke
    p_revoke = subs.add_parser("revoke", parents=[common])
    p_revoke.add_argument("--lane", required=True, help="lane directory")

    # send
    p_send = subs.add_parser("send", parents=[common])
    p_send.add_argument("--run", required=True, help="run id")
    p_send.add_argument("--task", help="task id")
    p_send.add_argument("--kind", required=True, help="message kind (ask, answer, info, ack)")
    p_send.add_argument("--text", required=True, help="message text")
    p_send.add_argument("--from", dest="from_sender", help="sender id")
    p_send.add_argument("--to", dest="to_recipient", help="recipient id")
    p_send.add_argument("--reply-to", dest="reply_to", help="reply to message id")

    # answer
    p_ans = subs.add_parser("answer", parents=[common])
    p_ans.add_argument("msg_id", help="question message id")
    p_ans.add_argument("text", help="answer text")
    p_ans.add_argument("--run", help="run id")

    # pending
    p_pend = subs.add_parser("pending", parents=[common])
    p_pend.add_argument("--run", help="run id")

    # tail
    p_tail = subs.add_parser("tail", parents=[common])
    p_tail.add_argument("--run", help="run id")
    p_tail.add_argument("--n", type=int, default=10, help="number of lines")

    try:
        args = parser.parse_args()
        if not args.cmd:
            raise Fail("missing subcommand; see --help")

        if args.cmd == "serve":
            return cmd_serve(args)
        elif args.cmd == "ensure":
            return cmd_ensure(args)
        elif args.cmd == "heartbeat":
            return cmd_heartbeat(args)
        elif args.cmd == "token":
            return cmd_token(args)
        elif args.cmd == "revoke":
            return cmd_revoke(args)
        elif args.cmd == "send":
            return cmd_send(args)
        elif args.cmd == "answer":
            return cmd_answer(args)
        elif args.cmd == "pending":
            return cmd_pending(args)
        elif args.cmd == "tail":
            return cmd_tail(args)
    except SystemExit:
        raise
    except Fail as f:
        out({"ok": False, "error": str(f)})
        sys.exit(f.code)
    except Exception as e:
        out({"ok": False, "error": f"{type(e).__name__}: {e}"})
        sys.exit(2)


if __name__ == "__main__":
    main()
