#!/usr/bin/env python3
"""Classify one herdr agent lane: OK | STALL | QUOTA | PROVIDER | BLOCKED | DEV_SERVER | ERROR.

Usage: lane-health.py --agent NAME|PANE --lane DIR [--stall-min 10] [--ledger PATH] [--since EPOCH]
       lane-health.py --self-test

Prints one JSON line: {"state", "evidence": [...], "agent_status", "last_change_min"}.
On QUOTA it also records the agent in the quota ledger (default ~/.claude/state/agent-quota.json).
herdr may print {"error": ...} and still exit 0, so JSON is parsed, not exit codes.
"""
import json, os, re, subprocess, sys, time

QUOTA_RE = re.compile(r"(usage limit|limit reached|quota (reached|exceeded)|rate limit|resets? (in|at) [^\n]{0,40})", re.I)
RESET_AT_RE = re.compile(r"reset(?:s)? at (\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)", re.I)
RESET_IN_RE = re.compile(r"resets? in (\d+)h(?:(\d+)m)?", re.I)
# Provider/account failures: the agent is up but every request fails — fail over like QUOTA.
PROVIDER_RE = re.compile(r"(unsupported model|subscription is required|model service is temporarily unavailable|invalid api key|unauthori[sz]ed|insufficient (credit|balance))", re.I)
# Only real command lines or dev-server output — not prose like "do not run pnpm dev".
DEV_RE = re.compile(r"(?m)^\s*(?:\$|>|●\s*Bash\(|Bash\(|Running:?)\s*.{0,40}?\b(?:pnpm|npm|yarn|bun)\s+(?:run\s+)?dev\b"
                    r"|\[hermes-agent\] Exited|ELIFECYCLE|VITE v\d+[.\d]* +ready|Local:\s+https?://localhost", re.I)


def herdr_json(*args):
    r = subprocess.run(["herdr", *args], capture_output=True, text=True)
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": (r.stdout or r.stderr).strip()[:200]}


def find(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            hit = find(v, key)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find(v, key)
            if hit is not None:
                return hit
    return None


def pane_text(agent, lines=80):
    r = subprocess.run(["herdr", "agent", "read", agent, "--source", "recent-unwrapped", "--lines", str(lines)],
                       capture_output=True, text=True)
    return r.stdout if not r.stdout.lstrip().startswith('{"error"') else ""


def last_change_min(lane, since=0.0):
    newest = since  # watch start counts as activity, so a fresh lane is not "stale"
    out = subprocess.run(["git", "-C", lane, "status", "--porcelain"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        p = os.path.join(lane, line[3:].strip().split(" -> ")[-1])
        if os.path.exists(p):
            newest = max(newest, os.path.getmtime(p))
    head = subprocess.run(["git", "-C", lane, "log", "-1", "--format=%ct"], capture_output=True, text=True).stdout.strip()
    if head.isdigit():
        newest = max(newest, float(head))
    return round((time.time() - newest) / 60, 1) if newest else None


def classify(text, status, idle_min, stall_min):
    tail = text[-4000:]
    if QUOTA_RE.search(tail):
        return "QUOTA", [m.group(0) for m in QUOTA_RE.finditer(tail)][-2:]
    prov = PROVIDER_RE.search(tail)
    if prov:
        return "PROVIDER", [prov.group(0)]
    if status == "blocked":
        return "BLOCKED", ["agent_status=blocked (approval/permission dialog?)"]
    dev = DEV_RE.search(tail)
    if dev:
        return "DEV_SERVER", [dev.group(0)]
    if status == "working" and idle_min is not None and idle_min >= stall_min:
        return "STALL", [f"working but no file/commit change for {idle_min} min"]
    return "OK", []


def record_quota(ledger, agent, text):
    data = {}
    if os.path.exists(ledger):
        try:
            data = json.load(open(ledger))
        except (OSError, json.JSONDecodeError):
            data = {}
    resets = None
    m = RESET_AT_RE.search(text)
    if m:
        resets = m.group(1)
    else:
        m = RESET_IN_RE.search(text)
        if m:
            resets = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + int(m.group(1)) * 3600 + int(m.group(2) or 0) * 60))
    entry = data.setdefault(agent, {})
    entry["window_5h"] = {"used_pct": 100, "resets_at": resets}
    entry["source"] = "pane-text"
    entry["observed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    json.dump(data, open(ledger, "w"), indent=2)


def self_test():
    assert classify("Usage limit reached for 5 hour. Your limit will reset at 2026-10-07 10:02:14", "idle", 1, 10)[0] == "QUOTA"
    assert classify("⚠ Individual quota reached. Resets in 154h43m", "idle", 1, 10)[0] == "QUOTA"
    assert classify("all good", "blocked", 1, 10)[0] == "BLOCKED"
    assert classify("[hermes-agent] Exited with code 1", "idle", 1, 10)[0] == "DEV_SERVER"
    assert classify("Do not run pnpm dev, a build, or any server", "working", 1, 10)[0] == "OK"
    assert classify("  $ pnpm dev", "working", 1, 10)[0] == "DEV_SERVER"
    assert classify("thinking", "working", 12, 10)[0] == "STALL"
    assert classify("thinking", "working", 3, 10)[0] == "OK"
    m = RESET_AT_RE.search("will reset at 2026-10-07 10:02:14")
    assert m and m.group(1) == "2026-10-07 10:02:14"
    assert classify("Upstream request failed: An active OpenCode Go subscription is required", "working", 1, 10)[0] == "PROVIDER"
    assert classify('{"error":"unsupported model: gpt-6.1-sol"}', "done", 1, 10)[0] == "PROVIDER"
    print("self-test ok")


def main():
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    def opt(f, d=None):
        return a[a.index(f) + 1] if f in a else d
    agent, lane = opt("--agent"), opt("--lane")
    if not agent or not lane:
        sys.exit(__doc__)
    stall_min = float(opt("--stall-min", "10"))
    ledger = os.path.expanduser(opt("--ledger", "~/.claude/state/agent-quota.json"))
    got = herdr_json("agent", "get", agent)
    if "error" in got:
        print(json.dumps({"state": "ERROR", "evidence": [str(got["error"])[:200]]}))
        return
    status = find(got, "agent_status")
    text = pane_text(agent)
    idle = last_change_min(lane, float(opt("--since", "0")))
    state, ev = classify(text, status, idle, stall_min)
    if state == "QUOTA":
        record_quota(ledger, agent, text)
    print(json.dumps({"state": state, "evidence": ev, "agent_status": status, "last_change_min": idle}))


if __name__ == "__main__":
    main()
