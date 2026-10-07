#!/usr/bin/env python3
"""Classify one herdr agent lane: OK | STALL | QUOTA | PROVIDER | BLOCKED | DEV_SERVER | ERROR.

Usage: lane-health.py --agent NAME|PANE --lane DIR [--kind claude|codex|opencode|agy] [--stall-min 10]
                      [--since EPOCH] [--prompt-marker TEXT] [--ledger PATH]
       lane-health.py --self-test

Prints one JSON line: {"state", "evidence": [...], "agent_status", "last_change_min"}.
Only pane text produced since dispatch is scanned: the tail after the last occurrence of --prompt-marker
(the dispatch prompt text), else the last 40 lines — old scrollback never re-triggers QUOTA.
On QUOTA it marks the agent exhausted in the usage ledger (default $MATRIXKODER_HOME/usage.json, i.e.
~/.matrixkoder/usage.json) under agents.<kind> (falls back to --agent when no --kind), never touching
the CodexBar windows mk-usage.py writes there.
herdr may print {"error": ...} and still exit 0, so JSON is parsed, not exit codes.
"""
import datetime, json, os, re, subprocess, sys, tempfile, time

KINDS = ("claude", "codex", "opencode", "agy")
# Bare "rate limit" / 429 is a transient throttle, not an exhausted quota — deliberately not matched.
QUOTA_RE = re.compile(r"(usage limit|limit reached|quota (reached|exceeded)|resets? (in|at) [^\n]{0,40})", re.I)
RESET_AT_RE = re.compile(r"reset(?:s)? at (\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)", re.I)
RESET_IN_RE = re.compile(r"resets? in (\d+)h(?:(\d+)m)?", re.I)
WEEKLY_AFTER_H = 6  # a reset further out than this is a weekly window, not the 5h one
# Provider/account failures: the agent is up but every request fails — fail over like QUOTA.
PROVIDER_RE = re.compile(r"(unsupported model|subscription is required|model service is temporarily unavailable|invalid api key|unauthori[sz]ed|insufficient (credit|balance))", re.I)
# Only real command lines or dev-server output — not prose like "do not run pnpm dev".
DEV_RE = re.compile(r"(?m)^\s*(?:\$|>|●\s*Bash\(|Bash\(|Running:?)\s*.{0,40}?\b(?:pnpm|npm|yarn|bun)\s+(?:run\s+)?dev\b"
                    r"|\[hermes-agent\] Exited|ELIFECYCLE|VITE v\d+[.\d]* +ready|Local:\s+https?://localhost", re.I)
TAIL_LINES = 40


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


def pane_text(agent, lines=300):
    r = subprocess.run(["herdr", "agent", "read", agent, "--source", "recent-unwrapped", "--lines", str(lines)],
                       capture_output=True, text=True)
    return r.stdout if not r.stdout.lstrip().startswith('{"error"') else ""


def since_dispatch(text, marker=None):
    """Pane text produced after the dispatch prompt; without a visible marker, only the last 40 lines."""
    if marker:
        i = text.rfind(marker.strip())
        if i >= 0:
            return text[i + len(marker.strip()):]
    return "\n".join(text.splitlines()[-TAIL_LINES:])


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
    """text must already be scoped with since_dispatch()."""
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


def parse_reset(text, now=None):
    """-> (exhausted_until ISO or None, window "5h" | "weekly" | None) from limit text."""
    now = now or datetime.datetime.now().astimezone()
    until = None
    m = RESET_AT_RE.search(text)
    if m:  # CLIs print local wall time
        until = datetime.datetime.fromisoformat(m.group(1).replace(" ", "T")).astimezone()
    else:
        m = RESET_IN_RE.search(text)
        if m:
            until = now + datetime.timedelta(hours=int(m.group(1)), minutes=int(m.group(2) or 0))
    if until is not None:
        window = "weekly" if until - now > datetime.timedelta(hours=WEEKLY_AFTER_H) else "5h"
        return until.isoformat(timespec="seconds"), window
    return None, ("weekly" if re.search(r"week", text, re.I) else None)


def record_quota(ledger, key, text, now=None):
    """Mark agents.<key> exhausted. Writes only the exhausted_* fields; CodexBar windows stay as they are."""
    try:
        with open(ledger) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    until, window = parse_reset(text, now)
    entry = data.setdefault("agents", {}).setdefault(key, {})
    entry.setdefault("source", "pane-text")  # an existing CodexBar entry keeps its own source
    entry.update({"exhausted_until": until, "exhausted_window": window, "exhausted_source": "pane-text",
                  "exhausted_observed_at": (now or datetime.datetime.now().astimezone()).isoformat(timespec="seconds"),
                  "exhausted_evidence": [m.group(0) for m in QUOTA_RE.finditer(text)][-2:]})
    d = os.path.dirname(ledger) or "."
    os.makedirs(d, exist_ok=True)
    # ponytail: atomic replace, but read-modify-write can race mk-usage.py; add a shared flock if both write often
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".usage-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, ledger)
    return entry


def default_ledger():
    return os.path.join(os.path.expanduser(os.environ.get("MATRIXKODER_HOME") or "~/.matrixkoder"), "usage.json")


def check(agent, lane, kind=None, stall_min=10.0, since=0.0, marker=None, ledger=None):
    """One health probe. Returns the result dict plus "text" (pane text since dispatch) for lane-watch."""
    got = herdr_json("agent", "get", agent)
    if "error" in got:
        return {"state": "ERROR", "evidence": [str(got["error"])[:200]], "text": ""}
    status = find(got, "agent_status")
    text = since_dispatch(pane_text(agent), marker)
    idle = last_change_min(lane, since)
    state, ev = classify(text, status, idle, stall_min)
    if state == "QUOTA":
        record_quota(ledger or default_ledger(), kind or agent, text)
    return {"state": state, "evidence": ev, "agent_status": status, "last_change_min": idle, "text": text}


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

    # a 429 / bare rate limit is a throttle, not quota
    assert classify("API Error: 429 rate limit exceeded, retrying in 5s", "working", 1, 10)[0] == "OK"
    assert classify("Rate limit hit, backing off", "working", 1, 10)[0] == "OK"

    # only text since dispatch counts: old scrollback above the prompt marker is ignored
    pane = "Usage limit reached. resets in 3h\n" + "old\n" * 5 + "> Read and follow brief-r2.md exactly.\nworking on it\n"
    assert since_dispatch(pane, "Read and follow brief-r2.md").strip().startswith("exactly.")
    assert classify(since_dispatch(pane, "Read and follow brief-r2.md"), "working", 1, 10)[0] == "OK"
    assert classify(since_dispatch(pane, "marker not on screen"), "working", 1, 10)[0] == "QUOTA"  # in last 40 lines
    old = "Usage limit reached. resets in 3h\n" + "noise\n" * 60
    assert classify(since_dispatch(old), "working", 1, 10)[0] == "OK"  # scrolled past the 40-line tail

    # window kind by reset distance
    now = datetime.datetime(2026, 10, 7, 12, 0).astimezone()
    assert parse_reset("Resets in 154h43m", now)[1] == "weekly"
    until, win = parse_reset("limit reached, resets in 2h30m", now)
    assert win == "5h" and until == (now + datetime.timedelta(hours=2, minutes=30)).isoformat(timespec="seconds")
    assert parse_reset("will reset at 2026-10-07 15:00:00", now)[1] == "5h"
    assert parse_reset("will reset at 2026-10-13 09:00", now)[1] == "weekly"
    assert parse_reset("weekly usage limit reached", now) == (None, "weekly")
    assert parse_reset("usage limit reached", now) == (None, None)

    # ledger keyed by kind, CodexBar windows preserved
    import shutil
    tmp = tempfile.mkdtemp(prefix="lane-health-test-")
    try:
        ledger = os.path.join(tmp, "usage.json")
        codexbar = {"source": "codexbar", "status": "ok",
                    "windows": [{"kind": "5h", "used_pct": 40, "resets_at": "2026-10-07T15:00:00Z"}]}
        json.dump({"updated": "x", "agents": {"agy": codexbar}}, open(ledger, "w"))
        record_quota(ledger, "agy", "⚠ Individual quota reached. Resets in 154h43m", now)
        d = json.load(open(ledger))
        agy = d["agents"]["agy"]
        assert agy["windows"] == codexbar["windows"] and agy["source"] == "codexbar" and d["updated"] == "x"
        assert agy["exhausted_window"] == "weekly" and agy["exhausted_source"] == "pane-text"
        assert agy["exhausted_until"] == (now + datetime.timedelta(hours=154, minutes=43)).isoformat(timespec="seconds")
        record_quota(ledger, "w2-codex-pane", "usage limit reached", now)  # no --kind → pane name key
        d = json.load(open(ledger))
        assert d["agents"]["w2-codex-pane"]["source"] == "pane-text" and d["agents"]["w2-codex-pane"]["exhausted_until"] is None
        assert os.listdir(tmp) == ["usage.json"]
        os.environ["MATRIXKODER_HOME"] = tmp
        assert default_ledger() == ledger
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("lane-health self-test ok")


def main():
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    def opt(f, d=None):
        return a[a.index(f) + 1] if f in a else d
    agent, lane, kind = opt("--agent"), opt("--lane"), opt("--kind")
    if not agent or not lane or (kind and kind not in KINDS):
        sys.exit(__doc__)
    ledger = opt("--ledger")
    r = check(agent, lane, kind, float(opt("--stall-min", "10")), float(opt("--since", "0")),
              opt("--prompt-marker"), ledger and os.path.expanduser(ledger))
    r.pop("text")
    print(json.dumps(r))


if __name__ == "__main__":
    main()
