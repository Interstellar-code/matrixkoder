#!/usr/bin/env python3
"""mk-wake.py — probe and ring an OpenCode session through the background server.

`probe` is read-only. `ring` posts the fixed doorbell only when exactly one
session for the lane is idle on positive evidence. The bus does not call this;
the hook point is Bus._drain_rings in scripts/mk-bus.py.

Observed API (2026-10-13, loopback :49374):
  GET  /api/session                  200  {"data":[{id, location.directory, time}]}
  POST /api/session/{id}/prompt      body {"text": "..."}
Auth: HTTP basic user `opencode`, password from the service file.
OPENCODE_SERVER is accepted only as loopback. Redirects are not followed.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PORT = 49374
# OpenCode-native fallback. E3 builds the real typed line `connect --data <dir>`.
DOORBELL = "python3 .claude/skills/mk-builder/scripts/mk-builder.py connect"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
TERMINAL = ("succeeded", "failed", "cancelled")


def emit(obj, code=0):
    print(json.dumps(obj, separators=(",", ":")))
    raise SystemExit(code)


def service_password():
    path = os.environ.get("MK_WAKE_SERVICE") or os.path.expanduser("~/.config/opencode/service.json")
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    pw = doc.get("password") if isinstance(doc, dict) else None
    return pw if isinstance(pw, str) and pw else None


def server_base():
    """Loopback only. A port number, or http://127.0.0.1|localhost|::1:<port>.
    Anything else returns None and no request is made."""
    env = (os.environ.get("OPENCODE_SERVER") or "").strip()
    if not env:
        return f"http://127.0.0.1:{DEFAULT_PORT}"
    if env.isdigit():
        port = int(env)
        if 1 <= port <= 65535:
            return f"http://127.0.0.1:{port}"
        return None
    try:
        parsed = urllib.parse.urlparse(env)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "http" or host not in LOOPBACK or port is None:
        return None
    return f"http://{host}:{port}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_json(method, url, body=None, password=None, timeout=5):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if password:
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"message": raw[:200]}
        return e.code, parsed
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ConnectionError(str(e)) from None


def session_directory(row):
    loc = row.get("location") if isinstance(row, dict) else None
    if isinstance(loc, dict) and isinstance(loc.get("directory"), str):
        return os.path.realpath(loc["directory"])
    return ""


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def session_status(row):
    """idle only on positive evidence. Anything else is unknown (ring refuses).

    Evidence: a terminal `outcome`, or numeric time.idle and time.updated with
    idle >= updated. Missing time, a missing idle, or non-numeric values
    (including bool) are unknown.
    """
    if not isinstance(row, dict):
        return "unknown"
    outcome = row.get("outcome")
    if isinstance(outcome, str) and outcome in TERMINAL:
        return "idle"
    time_ = row.get("time")
    if not isinstance(time_, dict):
        return "unknown"
    idle, updated = time_.get("idle"), time_.get("updated")
    if not _num(idle) or not _num(updated):
        return "unknown"
    if idle >= updated:
        return "idle"
    return "working"


def list_sessions(base, password):
    status, payload = http_json("GET", base + "/api/session?limit=100", password=password)
    if status == 401:
        raise RuntimeError("unauthorized (401)")
    if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError(f"API shape not as documented: GET /api/session -> {status}")
    return payload["data"]


def find_sessions(base, password, lane):
    want = os.path.realpath(lane)
    rows = list_sessions(base, password)
    return [r for r in rows if isinstance(r, dict) and session_directory(r) == want]


def claude_socket_present():
    return bool(os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET"))


def _port(base):
    try:
        return urllib.parse.urlparse(base).port if base else None
    except ValueError:
        return None


def cmd_probe(args):
    lane = os.path.abspath(args.lane)
    base = server_base()
    info = {"ok": False, "port": _port(base), "claude_socket": claude_socket_present()}
    if base is None:
        info["reason"] = "server not found"
        emit(info, 0)
    password = service_password()
    if password is None:
        info["reason"] = "server not found: no OpenCode service password"
        emit(info, 0)
    try:
        rows = find_sessions(base, password, lane)
    except ConnectionError:
        info["reason"] = "server not found"
        emit(info, 0)
    except RuntimeError as e:
        info["reason"] = str(e)
        emit(info, 0)
    if len(rows) > 1:
        info["reason"] = f"{len(rows)} sessions for the lane"
        emit(info, 0)
    if not rows:
        info["reason"] = "no session for the lane"
        emit(info, 0)
    row = rows[0]
    emit({"ok": True, "port": info["port"], "session": row.get("id"),
          "status": session_status(row), "directory": session_directory(row),
          "claude_socket": info["claude_socket"]}, 0)


def cmd_ring(args):
    text = args.text
    if text != DOORBELL:
        emit({"ok": False, "reason": "ring text must be the doorbell"}, 3)
    base = server_base()
    if base is None:
        emit({"ok": False, "reason": "server not found"}, 3)
    password = service_password()
    if password is None:
        emit({"ok": False, "reason": "no OpenCode service password"}, 3)
    lane = os.path.abspath(args.lane)
    try:
        rows = find_sessions(base, password, lane)
    except ConnectionError:
        emit({"ok": False, "reason": "server not found"}, 3)
    except RuntimeError as e:
        emit({"ok": False, "reason": str(e)}, 3)
    if len(rows) > 1:
        emit({"ok": False, "reason": f"{len(rows)} sessions for the lane"}, 3)
    if not rows:
        emit({"ok": False, "reason": "no session for the lane"}, 3)
    row = rows[0]
    sid = row.get("id")
    if not isinstance(sid, str) or not sid:
        emit({"ok": False, "reason": "shape mismatch"}, 3)
    status = session_status(row)
    if status != "idle":
        emit({"ok": False, "reason": f"session {status}", "session": sid}, 3)
    try:
        code, _payload = http_json(
            "POST", base + "/api/session/" + urllib.parse.quote(sid) + "/prompt",
            body={"text": DOORBELL}, password=password)
    except ConnectionError:
        emit({"ok": False, "reason": "server not found"}, 3)
    if code != 200:
        emit({"ok": False, "reason": f"prompt failed {code}", "session": sid}, 3)
    emit({"ok": True, "session": sid, "posted": True}, 0)


def self_test():
    """A tiny fake HTTP server that requires the known basic-auth password."""
    import subprocess
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    password = "test-secret"
    token = base64.b64encode(f"opencode:{password}".encode()).decode()
    state = {"sessions": [
        {"id": "ses_idle", "location": {"directory": "/tmp/lane-idle"},
         "time": {"created": 1, "updated": 1, "idle": 2}, "outcome": "succeeded"},
        {"id": "ses_work", "location": {"directory": "/tmp/lane-work"},
         "time": {"created": 1, "updated": 5, "idle": 2}},
        {"id": "ses_new", "location": {"directory": "/tmp/lane-new"},
         "time": {"created": 1, "updated": 9}},
        {"id": "ses_none", "location": {"directory": "/tmp/lane-none"}},
        {"id": "ses_a", "location": {"directory": "/tmp/lane-two"},
         "time": {"created": 1, "updated": 1, "idle": 2}},
        {"id": "ses_b", "location": {"directory": "/tmp/lane-two"},
         "time": {"created": 1, "updated": 1, "idle": 2}},
    ], "prompts": [], "gets": 0, "mode": "ok"}

    class H(BaseHTTPRequestHandler):
        def _json(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth(self):
            if self.headers.get("Authorization") != f"Basic {token}":
                self._json(401, {"message": "Authentication required"})
                return False
            return True

        def do_GET(self):
            if not self._auth():
                return
            state["gets"] += 1
            if state["mode"] == "shape":
                self._json(200, ["not", "a", "dict"])
                return
            if self.path.startswith("/api/session"):
                self._json(200, {"data": state["sessions"]})
                return
            self._json(404, {"message": "no"})

        def do_POST(self):
            if not self._auth():
                return
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode() or "{}")
            sid = self.path.split("/")[3]
            state["prompts"].append((sid, body.get("text")))
            self._json(200, {"ok": True})

        def log_message(self, *a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    tmp = tempfile.mkdtemp(prefix="mk-wake-")
    svc = os.path.join(tmp, "service.json")
    with open(svc, "w") as f:
        json.dump({"password": password}, f)
    env = dict(os.environ)
    env["OPENCODE_SERVER"] = f"http://127.0.0.1:{port}"
    env["MK_WAKE_SERVICE"] = svc
    env.pop("MK_WAKE_NO_AUTH", None)
    script = os.path.abspath(__file__)
    cases = []

    def check(name, ok):
        cases.append(name)
        if not ok:
            failed.append(name)

    def run(*args, env_=None):
        r = subprocess.run([sys.executable, script, *args], capture_output=True, text=True,
                           env=env_ or env, timeout=20)
        line = [x for x in r.stdout.splitlines() if x.strip()]
        try:
            obj = json.loads(line[-1]) if line else {}
        except ValueError:
            obj = {"raw": r.stdout, "err": r.stderr}
        return r.returncode, obj

    failed = []
    try:
        code, obj = run("probe", "--lane", "/tmp/lane-idle")
        check("probe idle", code == 0 and obj.get("ok") and obj.get("status") == "idle")
        code, obj = run("probe", "--lane", "/tmp/no-such-lane")
        check("probe miss", code == 0 and obj.get("ok") is False and "no session" in obj.get("reason", ""))
        code, obj = run("ring", "--lane", "/tmp/lane-idle", "--text", DOORBELL)
        check("ring idle", code == 0 and obj.get("posted") is True and state["prompts"] == [("ses_idle", DOORBELL)])
        code, obj = run("ring", "--lane", "/tmp/lane-work", "--text", DOORBELL)
        check("ring working", code == 3 and "working" in obj.get("reason", "") and len(state["prompts"]) == 1)
        code, obj = run("ring", "--lane", "/tmp/lane-new", "--text", DOORBELL)
        check("ring unknown time", code == 3 and "unknown" in obj.get("reason", "") and len(state["prompts"]) == 1)
        code, obj = run("ring", "--lane", "/tmp/lane-none", "--text", DOORBELL)
        check("ring no time", code == 3 and "unknown" in obj.get("reason", "") and len(state["prompts"]) == 1)
        code, obj = run("ring", "--lane", "/tmp/lane-idle", "--text", "a bus body")
        check("ring foreign text", code == 3 and obj.get("reason") == "ring text must be the doorbell" and len(state["prompts"]) == 1)
        code, obj = run("probe", "--lane", "/tmp/lane-two")
        check("two sessions", code == 0 and obj.get("reason") == "2 sessions for the lane")
        code, obj = run("ring", "--lane", "/tmp/lane-two", "--text", DOORBELL)
        check("ring two sessions", code == 3 and "2 sessions" in obj.get("reason", ""))
        code, obj = run("ring", "--lane", "/tmp/missing", "--text", DOORBELL)
        check("ring no session", code == 3 and obj.get("reason") == "no session for the lane")
        env_down = dict(env)
        env_down["OPENCODE_SERVER"] = "http://127.0.0.1:1"
        code, obj = run("probe", "--lane", "/tmp/lane-idle", env_=env_down)
        check("server down", obj.get("reason") == "server not found")
        gets = state["gets"]
        env_bad = dict(env)
        env_bad["OPENCODE_SERVER"] = "http://example.invalid:1"
        code, obj = run("probe", "--lane", "/tmp/lane-idle", env_=env_bad)
        check("remote host", obj.get("reason") == "server not found" and state["gets"] == gets)
        env_auth = dict(env)
        env_auth["MK_WAKE_SERVICE"] = os.path.join(tmp, "missing.json")
        code, obj = run("ring", "--lane", "/tmp/lane-idle", "--text", DOORBELL, env_=env_auth)
        check("no password", code == 3 and obj.get("reason") == "no OpenCode service password")
        wrong = os.path.join(tmp, "wrong.json")
        with open(wrong, "w") as f:
            json.dump({"password": "nope"}, f)
        env_wrong = dict(env)
        env_wrong["MK_WAKE_SERVICE"] = wrong
        code, obj = run("probe", "--lane", "/tmp/lane-idle", env_=env_wrong)
        check("auth 401", code == 0 and obj.get("reason") == "unauthorized (401)")
        state["mode"] = "shape"
        code, obj = run("probe", "--lane", "/tmp/lane-idle")
        check("shape mismatch", code == 0 and "shape" in obj.get("reason", ""))
        state["mode"] = "ok"
    finally:
        srv.shutdown()
    if failed:
        print("not ok " + ", ".join(failed))
        return 1
    print(f"ok {len(cases)} cases")
    return 0


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "--self-test":
        raise SystemExit(self_test())
    p = argparse.ArgumentParser(prog="mk-wake.py")
    sub = p.add_subparsers(dest="cmd")
    sp = sub.add_parser("probe")
    sp.add_argument("--lane", required=True)
    sp = sub.add_parser("ring")
    sp.add_argument("--lane", required=True)
    sp.add_argument("--text", required=True)
    args = p.parse_args(argv)
    if args.cmd == "probe":
        cmd_probe(args)
    elif args.cmd == "ring":
        cmd_ring(args)
    else:
        p.print_help()
        return 2
    return 0


if __name__ == "__main__":
    main()
