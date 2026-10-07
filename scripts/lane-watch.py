#!/usr/bin/env python3
"""Watch one lane until it finishes, fails, or the watch window ends — never silently.

Usage:
  lane-watch.py --lane DIR --agent NAME|PANE --commit-grep REGEX [--report PATH] [--phase P1]
                [--kind claude|codex|opencode|agy] [--prompt-marker TEXT]
                [--then-send BRIEF_PATH] [--max-wait-min 30] [--poll 20] [--stall-min 10]
                [--task T --data DIR [--run ID]]
  lane-watch.py --self-test

Done = latest commit subject matches REGEX AND (report exists, if given) AND agent not working
       AND (with --phase) a "<PHASE> DONE" line in the pane since dispatch.
"<PHASE> BLOCKED: <question>" in the pane → BLOCKED with the question.
Idle/done agent, not Done, and no file/commit change for --stall-min since watch start → STALL.
With --then-send, on DONE it dispatches "Read and follow <BRIEF_PATH> exactly ..." to the same agent
via the herdr skill's herdr-ask.py (which refuses busy agents, so we only send once idle).
With --task and --data, every exit writes the task status via mk-state (DONE→verifying, QUOTA→quota,
STALL→stalled, PROVIDER→provider, BLOCKED→blocked; other exits only log an event).
Exit states (one JSON line): DONE | DONE_SENT | QUOTA | PROVIDER | STALL | BLOCKED | DEV_SERVER | ERROR | WATCH_EXPIRED.
Run in the background (30 min default; foreground calls must stay under their 10-min timeout); re-arm on WATCH_EXPIRED.
"""
import importlib.util, json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ASK = os.path.expanduser("~/.claude/skills/herdr/scripts/herdr-ask.py")
MK_STATE = os.path.join(HERE, "mk-state.py")
STATUS_MAP = {"DONE": "verifying", "DONE_SENT": "verifying", "QUOTA": "quota", "STALL": "stalled",
              "PROVIDER": "provider", "BLOCKED": "blocked"}
FAIL_STATES = ("QUOTA", "PROVIDER", "BLOCKED", "DEV_SERVER", "ERROR")

sys.dont_write_bytecode = True  # no stray __pycache__ next to the scripts
_spec = importlib.util.spec_from_file_location("lane_health", os.path.join(HERE, "lane-health.py"))
lh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lh)


def opt(a, f, d=None):
    return a[a.index(f) + 1] if f in a else d


def commit_subject(lane):
    return subprocess.run(["git", "-C", lane, "log", "-1", "--format=%h %s"], capture_output=True, text=True).stdout.strip()


def phase_tokens(text, phase):
    """-> (done, blocked_question). Tokens must start a line (bullets allowed), so prose that merely
    mentions "reply with P1 DONE" mid-sentence does not count."""
    if not phase:
        return False, None
    p = re.escape(phase)
    blocked = re.search(rf"(?m)^\W*{p} BLOCKED:\s*(.+)$", text)
    done = re.search(rf"(?m)^\W*{p} DONE\b", text)
    return bool(done), (blocked.group(1).strip() if blocked else None)


def decide(h, subj, rx, report_ok, text, phase, stall_min):
    """One poll's verdict -> (state | None to keep watching, evidence list). Pure: no herdr, no git."""
    done_tok, question = phase_tokens(text, phase)
    if question:
        return "BLOCKED", [f"{phase} BLOCKED: {question}"]
    if h["state"] in FAIL_STATES:
        return h["state"], h.get("evidence") or []
    commit_ok = bool(rx.search(subj))
    status = h.get("agent_status")
    if commit_ok and report_ok and status != "working" and (done_tok or not phase):
        return "DONE", [f"commit {subj}"] + ([f"{phase} DONE"] if phase else [])
    if h["state"] == "STALL":
        return "STALL", h.get("evidence") or []
    idle = h.get("last_change_min")
    if status in ("idle", "done") and idle is not None and idle >= stall_min:
        missing = [n for n, ok in (("matching commit", commit_ok), ("report", report_ok),
                                   (f"{phase} DONE token", done_tok or not phase)) if not ok]
        return "STALL", [f"agent {status}, no file/commit change for {idle} min, missing: {', '.join(missing)}"]
    return None, []


def record(out, task, data, run=None):
    """Write the exit into run state via mk-state. Returns mk-state's JSON reply (or an error dict)."""
    note = f"lane-watch {out['state']}: " + "; ".join(out.get("evidence") or [out.get("commit") or ""])
    common = ["--data", data, "--by", "lane-watch"] + (["--run", run] if run else [])
    status = STATUS_MAP.get(out["state"])
    if status:
        pairs = [f"status={status}"]
        if out["state"].startswith("DONE") and out.get("commit"):
            pairs.append(f"last_commit={out['commit'].split()[0]}")
        cmd = ["task", "set", task, *pairs, "--note", note[:300]]
    else:
        cmd = ["event", "--task", task, "--type", "note", "--text", note[:300]]
    r = subprocess.run([sys.executable, MK_STATE, *cmd, *common], capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error": (r.stderr or r.stdout).strip()[-200:]}


def watch(a):
    lane, agent, grep = opt(a, "--lane"), opt(a, "--agent"), opt(a, "--commit-grep")
    report, then_send, phase = opt(a, "--report"), opt(a, "--then-send"), opt(a, "--phase")
    kind, marker = opt(a, "--kind"), opt(a, "--prompt-marker")
    max_wait = float(opt(a, "--max-wait-min", "30")) * 60
    poll = float(opt(a, "--poll", "20"))
    stall_min = float(opt(a, "--stall-min", "10"))
    rx = re.compile(grep)
    start = time.time()
    while True:
        subj = commit_subject(lane)
        h = lh.check(agent, lane, kind, stall_min, start, marker)
        report_ok = not report or os.path.exists(os.path.join(lane, report))
        state, ev = decide(h, subj, rx, report_ok, h.get("text", ""), phase, stall_min)
        if state == "DONE" and then_send:
            msg = f"Read and follow {then_send} exactly. Work autonomously until complete, then reply with the DONE token it names."
            r = subprocess.run([sys.executable, ASK, agent, msg, "--no-wait"], capture_output=True, text=True)
            return {"state": "DONE_SENT" if r.returncode == 0 else "ERROR", "commit": subj, "evidence": ev,
                    "sent": then_send, "ask_rc": r.returncode, "ask_out": (r.stdout + r.stderr).strip()[-200:]}
        if state:
            return {"state": state, "commit": subj, "evidence": ev, "agent_status": h.get("agent_status")}
        if time.time() - start > max_wait:
            return {"state": "WATCH_EXPIRED", "commit": subj, "agent_status": h.get("agent_status"),
                    "last_change_min": h.get("last_change_min")}
        time.sleep(poll)


def self_test():
    rx = re.compile(r"^\w+ feat\(p1\)")
    ok = {"state": "OK", "evidence": [], "agent_status": "idle", "last_change_min": 1}
    good, other = "abc123 feat(p1): state core", "def456 chore: wip"
    # DONE without --phase: commit + report + not working
    assert decide(ok, good, rx, True, "", None, 10)[0] == "DONE"
    assert decide({**ok, "agent_status": "working"}, good, rx, True, "", None, 10)[0] is None
    assert decide(ok, good, rx, False, "", None, 10)[0] is None
    # with --phase the DONE token is also required, at line start only
    assert decide(ok, good, rx, True, "I will reply with P1 DONE when finished", "P1", 10)[0] is None
    assert decide(ok, good, rx, True, "all gates green\n⏺ P1 DONE\n", "P1", 10) == ("DONE", [f"commit {good}", "P1 DONE"])
    assert decide(ok, other, rx, True, "P1 DONE", "P1", 10)[0] is None
    assert decide(ok, good, rx, True, "P2 DONE", "P1", 10)[0] is None
    # BLOCKED token wins and carries the question
    st, ev = decide({**ok, "agent_status": "working"}, other, rx, True, "● P1 BLOCKED: which schema version?\n", "P1", 10)
    assert st == "BLOCKED" and ev == ["P1 BLOCKED: which schema version?"]
    # health failures pass through
    for s in FAIL_STATES:
        assert decide({**ok, "state": s, "evidence": ["x"]}, other, rx, True, "", None, 10) == (s, ["x"])
    assert decide({**ok, "state": "STALL", "agent_status": "working"}, other, rx, True, "", None, 10)[0] == "STALL"
    # new STALL: idle/done, no matching commit, no change for stall_min since watch start
    st, ev = decide({**ok, "last_change_min": 11}, other, rx, True, "", None, 10)
    assert st == "STALL" and "matching commit" in ev[0]
    assert decide({**ok, "agent_status": "done", "last_change_min": 11}, other, rx, True, "", None, 10)[0] == "STALL"
    assert decide({**ok, "last_change_min": 9}, other, rx, True, "", None, 10)[0] is None
    assert decide({**ok, "agent_status": "working", "last_change_min": 11}, other, rx, True, "", None, 10)[0] is None
    st, ev = decide({**ok, "last_change_min": 11}, good, rx, True, "", "P1", 10)  # committed but no token
    assert st == "STALL" and "P1 DONE token" in ev[0] and "matching commit" not in ev[0]
    # status map covers every mapped exit with a valid mk-state status
    sp = importlib.util.spec_from_file_location("mk_state", MK_STATE)
    mk = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(mk)
    assert set(STATUS_MAP.values()) <= set(mk.STATUSES)
    assert STATUS_MAP["DONE"] == "verifying" and STATUS_MAP["STALL"] == "stalled"

    # record(): writes status + transition event through mk-state, in a temp data dir
    import shutil, tempfile
    tmp = tempfile.mkdtemp(prefix="lane-watch-test-")
    os.environ["MATRIXKODER_HOME"] = os.path.join(tmp, "home")
    data = os.path.join(tmp, "data")
    try:
        def mks(*args):
            r = subprocess.run([sys.executable, MK_STATE, *args, "--data", data], capture_output=True, text=True)
            assert r.returncode == 0, r.stdout + r.stderr
        mks("run", "init", "--id", "r1", "--feature", "F", "--branch", "b")
        mks("task", "add", "--id", "P1", "--title", "State core")
        mks("task", "set", "P1", "status=building")
        r = record({"state": "STALL", "commit": other, "evidence": ["agent idle 11 min"]}, "P1", data)
        assert r["ok"] and r["task"]["status"] == "stalled", r
        r = record({"state": "DONE", "commit": good, "evidence": ["commit x"]}, "P1", data)
        assert r["task"]["status"] == "verifying" and r["task"]["last_commit"] == "abc123"
        r = record({"state": "WATCH_EXPIRED", "commit": good}, "P1", data)
        assert r["ok"] and r["event"]["type"] == "note"
        evs = [json.loads(x) for x in open(os.path.join(data, "runs", "r1", "events.jsonl"))]
        tr = [(e["from"], e["to"], e["by"]) for e in evs if e["type"] == "transition" and e["from"]]
        assert ("building", "stalled", "lane-watch") in tr and ("stalled", "verifying", "lane-watch") in tr
        assert "lane-watch STALL: agent idle 11 min" in [e["text"] for e in evs]
        assert record({"state": "DONE", "commit": good}, "NOPE", data)["ok"] is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("lane-watch self-test ok")


def main():
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    if not (opt(a, "--lane") and opt(a, "--agent") and opt(a, "--commit-grep")):
        sys.exit(__doc__)
    if opt(a, "--kind") and opt(a, "--kind") not in lh.KINDS:
        sys.exit(__doc__)
    out = watch(a)
    task, data = opt(a, "--task"), opt(a, "--data")
    if task and data:
        r = record(out, task, data, opt(a, "--run"))
        out["state_written"] = r.get("ok", False)
        if not r.get("ok"):
            out["state_error"] = r.get("error")
    print(json.dumps(out))


if __name__ == "__main__":
    main()
