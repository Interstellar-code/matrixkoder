#!/usr/bin/env python3
"""mk-wake.py — probe and ring an OpenCode session through the background server.

Read-only `probe` finds the session whose directory is the lane. `ring` posts a
fixed doorbell only when that session is idle. The bus does not call this yet;
the hook point is listed in the E6 report.

Observed on this machine (OpenCode background service, 2026-10-13):
  GET  /api/session                         200  {"data":[{id, location.directory, time.idle?}]}
  POST /api/session/{id}/prompt            200  body {"text": "..."}  (empty body is 400 Missing key text)
  DELETE /api/session/{id}                 204
Auth is HTTP basic, user `opencode`, password from ~/.config/opencode/service.json.
A session with no `time.idle` and no `outcome` is idle (a brand-new session).
A session with `time.idle` absent but `outcome` set is also idle. A session whose
`time` has `updated` newer than `idle` is working.
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
DOORBELL = "MK_SELFTEST_JOBS=1 python3 .claude/skills/mk-builder/scripts/mk-builder.py connect"


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
    """OPENCODE_SERVER wins (http://host:port). Else the known loopback port."""
    env = os.environ.get("OPENCODE_SERVER")
    if env:
        return env.rstrip("/")
    return f"http://127.0.0.1:{DEFAULT_PORT}"


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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def session_status(row):
    """idle when the session is not mid-turn. `time.idle` missing on a new
    session means idle. `working` when updated is newer than idle."""
    time_ = row.get("time") if isinstance(row, dict) else None
    if not isinstance(time_, dict):
        return "idle"
    idle = time_.get("idle")
    updated = time_.get("updated")
    if idle is None:
        return "idle"
    if isinstance(updated, (int, float)) and isinstance(idle, (int, float)) and updated > idle:
        return "working"
    return "idle"


def list_sessions(base, password):
    status, payload = http_json("GET", base + "/api/session?limit=100", password=password)
    if status == 401:
        raise RuntimeError("API shape not as documented: 401 unauthorized")
    if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError(f"API shape not as documented: GET /api/session -> {status}")
    return payload["data"]


def find_session(base, password, lane):
    want = os.path.realpath(lane)
    rows = list_sessions(base, password)
    matches = [r for r in rows if isinstance(r, dict) and session_directory(r) == want]
    return matches[0] if matches else None


def claude_socket_present():
    return bool(os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET"))


def cmd_probe(args):
    lane = os.path.abspath(args.lane)
    base = server_base()
    password = service_password()
    info = {"ok": False, "port": urllib.parse.urlparse(base).port,
            "claude_socket": claude_socket_present()}
    if password is None and not os.environ.get("MK_WAKE_NO_AUTH"):
        info["reason"] = "server not found: no OpenCode service password"
        emit(info, 0)
    try:
        row = find_session(base, password, lane)
    except ConnectionError:
        info["reason"] = "server not found"
        emit(info, 0)
    except RuntimeError as e:
        info["reason"] = str(e)
        emit(info, 0)
    if row is None:
        info["reason"] = "no session for the lane"
        emit(info, 0)
    emit({"ok": True, "port": info["port"], "session": row.get("id"),
          "status": session_status(row), "directory": session_directory(row),
          "claude_socket": info["claude_socket"]}, 0)


def cmd_ring(args):
    lane = os.path.abspath(args.lane)
    text = args.text
    if not text or not text.strip():
        emit({"ok": False, "reason": "ring needs fixed doorbell text"}, 3)
    # Never forward a bus body. The caller passes the fixed line.
    base = server_base()
    password = service_password()
    try:
        row = find_session(base, password, lane)
    except ConnectionError:
        emit({"ok": False, "reason": "server not found"}, 3)
    except RuntimeError as e:
        emit({"ok": False, "reason": str(e)}, 3)
    if row is None:
        emit({"ok": False, "reason": "no session for the lane"}, 3)
    status = session_status(row)
    if status != "idle":
        emit({"ok": False, "reason": f"session {status}", "session": row.get("id")}, 3)
    code, payload = http_json(
        "POST", base + "/api/session/" + urllib.parse.quote(row["id"]) + "/prompt",
        body={"text": text}, password=password)
    if code != 200:
        emit({"ok": False, "reason": f"prompt failed {code}", "session": row.get("id")}, 3)
    emit({"ok": True, "session": row.get("id"), "posted": True}, 0)


def self_test():
    """A tiny fake HTTP server. No real OpenCode, no real home."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"sessions": [
        {"id": "ses_idle", "location": {"directory": "/tmp/lane-idle"},
         "time": {"created": 1, "updated": 1}},
        {"id": "ses_work", "location": {"directory": "/tmp/lane-work"},
         "time": {"created": 1, "updated": 5, "idle": 2}},
    ], "prompts": []}

    class H(BaseHTTPRequestHandler):
        def _json(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path.startswith("/api/session"):
                self._json(200, {"data": state["sessions"]})
                return
            self._json(404, {"message": "no"})

        def do_POST(self):
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
    env = dict(os.environ)
    env["OPENCODE_SERVER"] = f"http://127.0.0.1:{port}"
    env["MK_WAKE_NO_AUTH"] = "1"
    env.pop("MK_WAKE_SERVICE", None)
    import subprocess
    script = os.path.abspath(__file__)

    def run(*args):
        r = subprocess.run([sys.executable, script, *args], capture_output=True, text=True, env=env, timeout=20)
        line = r.stdout.strip().splitlines()
        return r.returncode, json.loads(line[-1]) if line else {}

    failed = []
    try:
        code, obj = run("probe", "--lane", "/tmp/lane-idle")
        if not (code == 0 and obj.get("ok") and obj.get("session") == "ses_idle" and obj.get("status") == "idle"):
            failed.append(f"probe idle {code} {obj}")
        code, obj = run("probe", "--lane", "/tmp/no-such-lane")
        if not (code == 0 and obj.get("ok") is False and "no session" in obj.get("reason", "")):
            failed.append(f"probe miss {code} {obj}")
        code, obj = run("ring", "--lane", "/tmp/lane-idle", "--text", DOORBELL)
        if not (code == 0 and obj.get("posted") is True and state["prompts"] == [("ses_idle", DOORBELL)]):
            failed.append(f"ring idle {code} {obj} {state['prompts']}")
        code, obj = run("ring", "--lane", "/tmp/lane-work", "--text", DOORBELL)
        if not (code == 3 and "working" in obj.get("reason", "") and len(state["prompts"]) == 1):
            failed.append(f"ring working {code} {obj}")
        # server down
        env2 = dict(env)
        env2["OPENCODE_SERVER"] = "http://127.0.0.1:1"
        r = subprocess.run([sys.executable, script, "probe", "--lane", "/tmp/lane-idle"],
                           capture_output=True, text=True, env=env2, timeout=20)
        obj = json.loads(r.stdout.strip() or "{}")
        if obj.get("reason") != "server not found":
            failed.append(f"down {obj}")
    finally:
        srv.shutdown()
    if failed:
        print("not ok " + "; ".join(failed))
        return 1
    print("ok 5 cases")
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
