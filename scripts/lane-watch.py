#!/usr/bin/env python3
"""Watch one lane until it finishes, fails, or the watch window ends — never silently.

Usage:
  lane-watch.py --lane DIR --agent NAME|PANE --commit-grep REGEX [--report PATH] [--phase P1]
                [--kind claude|codex|opencode|agy] [--prompt-marker TEXT]
                [--then-send BRIEF_PATH] [--max-wait-min 30] [--poll 20] [--stall-min 10]
                [--task T --data DIR [--run ID]]
  lane-watch.py --self-test

Done = latest commit subject matches REGEX (searched in the bare subject, so `^feat(x)`-style anchors work; the
       older "<sha> <subject>" form, e.g. `^[0-9a-f]+ feat`, still matches) AND (report exists, if given) AND agent not working
       AND (with --phase) a "<PHASE> DONE" line in the pane since dispatch. Done wins over a stray
       limit / dev-server string in the same pane.
"<PHASE> BLOCKED: <question>" in the pane → BLOCKED with the question (the later of DONE / BLOCKED wins).
A herdr ERROR is retried; the watcher exits ERROR after 3 in a row, or on any crash (still one JSON line).
Idle/done agent, not Done, and no file/commit change for --stall-min since watch start → STALL.
Pane output that changed since the last poll (digits ignored, so a ticking timer is not activity) counts as
activity too: a builder running tests or a revert/re-apply proof without touching files is not STALL.
With --then-send, on DONE it dispatches "Read and follow <BRIEF_PATH> exactly ..." to the same agent
via the herdr skill's herdr-ask.py (which refuses busy agents, so we only send once idle).
With --task and --data (both or neither), every exit writes the task status via mk-state (DONE→verifying,
QUOTA→quota, STALL→stalled, PROVIDER→provider, BLOCKED→blocked) when the task is still in a watched status
(dispatched building stalled quota provider blocked); otherwise, and for other exits, it only logs an event.
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
WATCHED = ("dispatched", "building", "stalled", "quota", "provider", "blocked")  # a watcher may move these only
ERROR_RETRIES = 3

sys.dont_write_bytecode = True  # no stray __pycache__ next to the scripts
_spec = importlib.util.spec_from_file_location("lane_health", os.path.join(HERE, "lane-health.py"))
lh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lh)


def opt(a, f, d=None):
    if f not in a:
        return d
    if a.index(f) + 1 >= len(a):
        sys.exit(f"{f} needs a value\n{__doc__}")
    return a[a.index(f) + 1]


def commit_subject(lane):
    return subprocess.run(["git", "-C", lane, "log", "-1", "--format=%h %s"], capture_output=True, text=True).stdout.strip()


def phase_tokens(text, phase):
    """-> (done, blocked_question); only the later of the two counts. Tokens must start a line (bullets
    allowed), so prose that merely mentions "reply with P1 DONE" does not count, and lines with a "<"
    placeholder (an echoed brief: "P1 BLOCKED: <question>") are ignored."""
    if not phase:
        return False, None
    p = re.escape(phase)
    blocked = list(re.finditer(rf"(?m)^\W*{p} BLOCKED:\s*([^<\n]+)$", text))
    done = list(re.finditer(rf"(?m)^\W*{p} DONE\b[^<\n]*$", text))
    if blocked and (not done or blocked[-1].start() > done[-1].start()):
        return False, blocked[-1].group(1).strip()
    return bool(done), None


def pane_fingerprint(text):
    """Pane text with digits dropped: spinners and elapsed-time counters change digits only.
    ponytail: a hung CLI that redraws non-digit text (an animated spinner glyph) still looks active; match its spinner line here if that shows up."""
    return re.sub(r"\d+", "", text)


def decide(h, subj, rx, report_ok, text, phase, stall_min):
    """One poll's verdict -> (state | None to keep watching, evidence list). Pure: no herdr, no git."""
    done_tok, question = phase_tokens(text, phase)
    commit_ok = bool(rx.search(subj.partition(" ")[2]) or rx.search(subj))  # subj = "<sha> <subject>"
    status = h.get("agent_status")
    if h["state"] != "ERROR" and commit_ok and report_ok and status != "working" and (done_tok or not phase):
        return "DONE", [f"commit {subj}"] + ([f"{phase} DONE"] if phase else [])
    if question:
        return "BLOCKED", [f"{phase} BLOCKED: {question}"]
    if h["state"] in FAIL_STATES:
        return h["state"], h.get("evidence") or []
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
        r = subprocess.run([sys.executable, MK_STATE, "task", "get", task, *common], capture_output=True, text=True)
        try:
            cur = json.loads(r.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            cur = {"ok": False}
        if cur.get("ok") and cur["task"]["status"] not in WATCHED:  # late watcher: never overwrite review/merged/...
            note += f" (status {cur['task']['status']} kept)"
            status = None
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
    start, errors = time.time(), 0
    active, last_fp = start, None  # last time the pane showed new output
    while True:
        subj = commit_subject(lane)
        h = lh.check(agent, lane, kind, stall_min, active, marker)
        fp = pane_fingerprint(h.get("text", ""))
        if last_fp is not None and fp != last_fp:  # pane moved: idle time counts from now, re-check with it
            active = time.time()
            h = lh.check(agent, lane, kind, stall_min, active, marker)
        last_fp = fp
        report_ok = not report or os.path.exists(os.path.join(lane, report))
        state, ev = decide(h, subj, rx, report_ok, h.get("text", ""), phase, stall_min)
        errors = errors + 1 if state == "ERROR" else 0
        if state == "ERROR" and errors < ERROR_RETRIES and time.time() - start <= max_wait:
            time.sleep(poll)
            continue
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
    # a ^-anchored regex matches the bare subject (lesson 36); an unanchored one is unaffected
    assert decide(ok, good, re.compile(r"^feat\(p1\): state"), True, "", None, 10)[0] == "DONE"
    assert decide(ok, other, re.compile(r"^feat\(p1\)"), True, "", None, 10)[0] is None
    assert decide(ok, good, re.compile(r"state core"), True, "", None, 10)[0] == "DONE"
    # with --phase the DONE token is also required, at line start only
    assert decide(ok, good, rx, True, "I will reply with P1 DONE when finished", "P1", 10)[0] is None
    assert decide(ok, good, rx, True, "all gates green\n⏺ P1 DONE\n", "P1", 10) == ("DONE", [f"commit {good}", "P1 DONE"])
    assert decide(ok, other, rx, True, "P1 DONE", "P1", 10)[0] is None
    assert decide(ok, good, rx, True, "P2 DONE", "P1", 10)[0] is None
    # BLOCKED token wins and carries the question
    st, ev = decide({**ok, "agent_status": "working"}, other, rx, True, "● P1 BLOCKED: which schema version?\n", "P1", 10)
    assert st == "BLOCKED" and ev == ["P1 BLOCKED: which schema version?"]
    # placeholder lines from an echoed brief never count; the later of DONE / BLOCKED wins
    assert decide(ok, other, rx, True, "Reply P1 BLOCKED: <question> if stuck", "P1", 10)[0] is None
    assert phase_tokens("P1 BLOCKED: <question>\nP1 DONE <sha>\n", "P1") == (False, None)
    assert phase_tokens("P1 BLOCKED: which port?\nok, 3001\nP1 DONE\n", "P1") == (True, None)
    assert phase_tokens("P1 DONE\nP1 BLOCKED: tests now fail?\n", "P1") == (False, "tests now fail?")
    # a finished lane is DONE even if a stray limit / dev-server string is on screen; ERROR never means DONE
    for s_ in ("QUOTA", "DEV_SERVER", "PROVIDER"):
        assert decide({**ok, "state": s_, "evidence": ["x"]}, good, rx, True, "P1 DONE", "P1", 10)[0] == "DONE"
    assert decide({**ok, "state": "ERROR", "evidence": ["x"]}, good, rx, True, "", None, 10)[0] == "ERROR"
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
    # pane activity: digit-only changes (timers) are not activity; new output lines are
    assert pane_fingerprint("Working (12s) esc to interrupt") == pane_fingerprint("Working (73s) esc to interrupt")
    assert pane_fingerprint("$ vitest run\n") != pane_fingerprint("$ vitest run\n 10 failed\n")
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
        # late watcher: a task already in review keeps its status, the exit is only noted
        mks("task", "set", "P1", "status=review")
        r = record({"state": "STALL", "commit": other, "evidence": ["late"]}, "P1", data)
        assert r["ok"] and "event" in r and "status review kept" in r["event"]["text"]
        assert json.load(open(os.path.join(data, "runs", "r1", "run.json")))["tasks"][0]["status"] == "review"

        # CLI end to end with a fake herdr on PATH and a real git lane
        bin_ = os.path.join(tmp, "bin")
        os.makedirs(bin_)
        pane = os.path.join(tmp, "pane.txt")
        with open(os.path.join(bin_, "herdr"), "w") as f:
            f.write('#!/bin/sh\nif [ "$2" = get ]; then echo \'{"result":{"agent":{"agent_status":"idle"}}}\'; '
                    f'else cat "{pane}"; fi\n')
        os.chmod(os.path.join(bin_, "herdr"), 0o755)
        env = dict(os.environ, PATH=bin_ + os.pathsep + os.environ["PATH"])
        lane = os.path.join(tmp, "lane")
        git = ["git", "-C", lane, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", lane], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "feat(p1): state core"], check=True)
        mks("task", "add", "--id", "P3", "--title", "cli")
        mks("task", "set", "P3", "status=building")

        def cli(*args, code=0):
            r = subprocess.run([sys.executable, __file__, "--lane", lane, "--agent", "w1", *args], env=env,
                               capture_output=True, text=True)
            assert r.returncode == code, r.stdout + r.stderr
            return json.loads(r.stdout.strip().splitlines()[-1]) if code == 0 else r
        open(pane, "w").write("old: Usage limit reached\n> Read and follow brief-r1.md exactly.\nall green\n⏺ P1 DONE\n")
        out = cli("--commit-grep", r"feat\(p1\)", "--phase", "P1", "--kind", "codex", "--prompt-marker",
                  "Read and follow brief-r1.md", "--poll", "0.1", "--task", "P3", "--data", data)
        assert out["state"] == "DONE" and out["state_written"] is True, out
        assert json.load(open(os.path.join(data, "runs", "r1", "run.json")))["tasks"][-1]["status"] == "verifying"
        out = cli("--commit-grep", "(", "--poll", "0.1", "--task", "P3", "--data", data)  # bad regex → ERROR
        assert out["state"] == "ERROR" and "missing )" in out["evidence"][0] and out["state_written"] is True, out
        assert cli("--commit-grep", "x", "--task", "P3", code=1).returncode == 1  # --task without --data
        # lane-health CLI: QUOTA keyed by kind into $MATRIXKODER_HOME/usage.json, scope reported
        open(pane, "w").write("> Read and follow brief-r2.md\n■ You've hit your usage limit. Try again in 2 hours.\n")
        r = subprocess.run([sys.executable, os.path.join(HERE, "lane-health.py"), "--agent", "w1", "--lane", lane,
                            "--kind", "codex", "--prompt-marker", "Read and follow brief-r2.md"],
                           env=env, capture_output=True, text=True)
        h = json.loads(r.stdout)
        assert h["state"] == "QUOTA" and h["scope"] == "marker" and h["agent_status"] == "idle", h
        led = json.load(open(os.path.join(tmp, "home", "usage.json")))["agents"]["codex"]
        assert led["exhausted_window"] == "5h" and led["exhausted_guess"] is False
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
    task, data = opt(a, "--task"), opt(a, "--data")
    if bool(task) != bool(data):
        sys.exit("--task and --data go together (state is written only with both)\n" + __doc__)
    try:
        out = watch(a)
    except Exception as e:  # never die silently: missing herdr/git, bad regex, bad number → ERROR + state note
        out = {"state": "ERROR", "evidence": [f"{type(e).__name__}: {e}"[:200]]}
    if task and data:
        r = record(out, task, data, opt(a, "--run"))
        out["state_written"] = r.get("ok", False)
        if not r.get("ok"):
            out["state_error"] = r.get("error")
    print(json.dumps(out))


if __name__ == "__main__":
    main()
