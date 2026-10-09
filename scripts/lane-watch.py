#!/usr/bin/env python3
"""Watch one lane until it finishes, fails, or the watch window ends — never silently.

Usage:
  lane-watch.py --lane DIR --agent NAME|PANE --commit-grep REGEX [--report PATH] [--phase P1]
                [--kind claude|codex|opencode|agy] [--prompt-marker TEXT]
                [--then-send BRIEF_PATH] [--max-wait-min 30] [--poll 20] [--stall-min 10]
                [--task T --data DIR [--run ID]] [--no-commit]
  lane-watch.py --self-test

Done = latest commit subject matches REGEX (searched in the bare subject, so `^feat(x)`-style anchors work; the
       older "<sha> <subject>" form, e.g. `^[0-9a-f]+ feat`, still matches) AND (report exists, if given) AND agent not working
       AND (with --phase) a "<PHASE> DONE" line in the pane since dispatch. Done wins over a stray
       limit / dev-server string in the same pane.
With --no-commit (a reviewer pane: nothing is committed, so there is no commit to match) the commit half
       drops out everywhere: --commit-grep is not required (a given one is ignored; without --no-commit it
       is), Done = (report exists, if given) AND agent not working AND the token, the no-token grace is
       report + agent idle/done, and STALL never lists a matching commit as missing.
"<PHASE> BLOCKED: <question>" in the pane → BLOCKED with the question (the later of DONE / BLOCKED wins).
A herdr ERROR is retried; the watcher exits ERROR after 3 in a row, or on any crash (still one JSON line).
Idle/done/working agent, not Done, and no file/commit/pane change for --stall-min since watch start → STALL
(lane-watch decides STALL itself; lane-health's file-only STALL is not used here).
Pane output that changed since the last poll (digits ignored, so a ticking timer is not activity — the
`↳ N toolcalls` counter is exempt: a changed count is activity) counts as activity too, and so does the
mere presence of a running subagent task line (a braille spinner + "Task —"): a builder running tests,
a revert/re-apply proof or a lead waiting on its subagents is not STALL. Finished "✓ … Task —" lines
alone are not activity — with only those left, the stall clock runs again.
With --then-send, on DONE it dispatches "Read and follow <BRIEF_PATH> exactly ..." to the same agent
via the herdr skill's herdr-ask.py (which refuses busy agents, so we only send once idle).
With --task and --data (both or neither), every exit writes the task status via mk-state (DONE→verifying,
QUOTA→quota, STALL→stalled, PROVIDER→provider, BLOCKED→blocked) when the target is still in a watched
status (dispatched building stalled quota provider blocked); otherwise, and for other exits, it only logs
an event. A parent task is never written — mk-state refuses status= on it ("status rolls up from
children") — every child gets the verdict instead, and each mk-state reply is checked: an ok:false error
always lands in this script's own JSON output, never dropped.
Exit states (one JSON line): DONE | DONE_SENT | QUOTA | PROVIDER | STALL | BLOCKED | DEV_SERVER | ERROR | WATCH_EXPIRED.
Run in the background (30 min default; foreground calls must stay under their 10-min timeout); re-arm on WATCH_EXPIRED.
"""
import importlib.util, json, math, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ASK = os.path.expanduser("~/.claude/skills/herdr/scripts/herdr-ask.py")
MK_STATE = os.path.join(HERE, "mk-state.py")
STATUS_MAP = {"DONE": "verifying", "DONE_SENT": "verifying", "QUOTA": "quota", "STALL": "stalled",
              "PROVIDER": "provider", "BLOCKED": "blocked"}
FAIL_STATES = ("QUOTA", "PROVIDER", "BLOCKED", "DEV_SERVER", "ERROR")
WATCHED = ("dispatched", "building", "stalled", "quota", "provider", "blocked")  # a watcher may move these only
ERROR_RETRIES = 3
TOKEN_GRACE_MIN = 3  # commit + report + agent idle this long (report + idle with --no-commit), but no DONE token: DONE anyway (lesson 39)

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


TOOLCALLS_RE = re.compile(r"(↳\s*)(\d+)(\s*toolcalls)")  # the ↳ N toolcalls subagent counter, kept below
COUNT_AS_LETTERS = str.maketrans("0123456789", "abcdefghij")  # digits that survive the drop
SPINNERS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille glyphs a busy CLI spins through
RUNNING_TASK_RE = re.compile(rf"(?m)^[{SPINNERS}].*Task —")  # a running subagent task line (em dash)


def pane_fingerprint(text):
    """Pane text with digits dropped: spinners and elapsed-time counters change digits only. The
    `↳ N toolcalls` counter is exempt (its digits become letters): a changed count between polls is
    subagent activity, the `12m 48s` timer it sits next to still is not.
    ponytail: a redrawn spinner glyph already moves this; a spinner line whose glyph happens not to
    change between two polls is caught by running_task() instead (presence, not change)."""
    kept = TOOLCALLS_RE.sub(lambda m: m.group(1) + m.group(2).translate(COUNT_AS_LETTERS) + m.group(3), text)
    return re.sub(r"\d+", "", kept)


def running_task(text):
    """True while a running subagent task line is on screen — a braille spinner (⠋⠙⠹…) then
    "Task —": the subagent is actively working, so the line's presence alone is activity even when
    the glyph does not change between two polls. Finished "✓ … Task —" lines (no spinner) never count."""
    return bool(RUNNING_TASK_RE.search(text))


def decide(h, subj, rx, report_ok, text, phase, stall_min):
    """One poll's verdict -> (state | None to keep watching, evidence list). Pure: no herdr, no git.
    rx None = --no-commit (a reviewer pane: no commit to match): any commit counts as matching, no
    commit evidence is reported, and STALL never lists a matching commit as missing."""
    done_tok, question = phase_tokens(text, phase)
    commit_ok = rx is None or bool(rx.search(subj.partition(" ")[2]) or rx.search(subj))  # subj = "<sha> <subject>"
    commit_ev = [] if rx is None else [f"commit {subj}"]
    status = h.get("agent_status")
    if h["state"] != "ERROR" and commit_ok and report_ok and status != "working" and (done_tok or not phase):
        return "DONE", commit_ev + ([f"{phase} DONE"] if phase else [])
    if question:
        return "BLOCKED", [f"{phase} BLOCKED: {question}"]
    idle = h.get("last_change_min")  # since the last file/commit change OR pane activity (watch() passes it)
    if (h["state"] != "ERROR" and commit_ok and report_ok and status in ("idle", "done") and phase
            and idle is not None and idle >= min(TOKEN_GRACE_MIN, stall_min)):
        # builders (Claude especially) end with a summary and forget the token; the work is there, so the
        # orchestrator verifies it rather than waiting for WATCH_EXPIRED
        got = "commit + report" if rx is not None else "report"
        return "DONE", commit_ev + [f"no {phase} DONE token ({got}, agent {status} {idle} min)"]
    if h["state"] in FAIL_STATES:
        return h["state"], h.get("evidence") or []
    if status == "working" and idle is not None and idle >= stall_min:
        return "STALL", [f"working but no file/commit/pane change for {idle} min"]
    if status in ("idle", "done") and idle is not None and idle >= stall_min:
        missing = [n for n, ok in (("matching commit", commit_ok), ("report", report_ok),
                                   (f"{phase} DONE token", done_tok or not phase)) if not ok]
        return "STALL", [f"agent {status}, no file/commit change for {idle} min, missing: {', '.join(missing)}"]
    return None, []


def child_tasks(task, data, run=None):
    """Stored ids of <task>'s status-holding descendants — every child, recursing past children
    that are parents themselves (mk-state refuses status= on ANY parent: "status rolls up from
    children"; intermediate parents get the verdict by roll-up, leaves by the write). [] for a
    leaf, an unknown task, an unreadable or ambiguous run: the mk-state write itself then decides,
    exactly as before. Mirrors mk-state's run pick: --run names the run; else the single active
    run; with several, the one that owns the task (ids match case-insensitively, as there)."""
    runs = os.path.join(data, "runs")

    def load(rid):
        try:
            with open(os.path.join(runs, rid, "run.json")) as f:
                j = json.load(f)
            return j if isinstance(j, dict) else None
        except (OSError, ValueError):
            return None

    def owns(j):
        return isinstance(j.get("tasks"), list) and any(
            isinstance(t, dict) and str(t.get("id") or "").lower() == task.lower() for t in j["tasks"])

    if run:
        src = load(run)
        if src is None or not owns(src):
            return []
    else:
        try:
            with open(os.path.join(runs, "index.json")) as f:
                actives = [r["id"] for r in json.load(f)
                           if isinstance(r, dict) and r.get("status") == "active" and r.get("id")]
        except (OSError, ValueError):
            return []
        owners = [j for j in (load(r) for r in actives) if j and owns(j)]
        if len(owners) != 1:
            return []
        src = owners[0]
    ts = [t for t in src["tasks"] if isinstance(t, dict) and t.get("id")]
    pid = next(str(t["id"]) for t in ts if str(t["id"]).lower() == task.lower())
    # parent stored case-exactly like mk-state's roll-up compares it (c.get("parent") == t["id"])
    parent_of = lambda tid: [str(c["id"]) for c in ts if c.get("parent") == tid]
    kids, stack, seen = [], [pid], {pid}
    while stack:  # walk the subtree; a cyclic hand-edit must not hang this
        cur = stack.pop()
        next_ = [k for k in parent_of(cur) if k not in seen]
        seen.update(next_)
        stack.extend(next_)
        if cur != pid and not next_:
            kids.append(cur)  # a leaf descendant: it can hold a status
    return [str(t["id"]) for t in ts if str(t["id"]) in kids]  # run.json insertion order


def record(out, task, data, run=None):
    """Write the exit into run state via mk-state: every child when <task> is a parent (mk-state
    refuses status= on a parent — "status rolls up from children" — so the children carry the verdict
    and the parent only rolls up), else the task itself. -> mk-state's reply for one target, or
    {ok, children, results[, error]} for several; every ok:false reply is reported, never dropped."""
    note = f"lane-watch {out['state']}: " + "; ".join(out.get("evidence") or [out.get("commit") or ""])
    common = ["--data", data, "--by", "lane-watch"] + (["--run", run] if run else [])
    status = STATUS_MAP.get(out["state"])
    targets = child_tasks(task, data, run) or [task]
    results = []
    for t in targets:
        t_note, t_status = note, status
        if t_status:
            r = subprocess.run([sys.executable, MK_STATE, "task", "get", t, *common], capture_output=True, text=True)
            try:
                cur = json.loads(r.stdout.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                cur = {"ok": False}
            if cur.get("ok") and cur["task"]["status"] not in WATCHED:  # late watcher: never overwrite review/merged/...
                t_note += f" (status {cur['task']['status']} kept)"
                t_status = None
        if t_status:
            pairs = [f"status={t_status}"]
            if out["state"].startswith("DONE") and out.get("commit"):
                pairs.append(f"last_commit={out['commit'].split()[0]}")
            cmd = ["task", "set", t, *pairs, "--note", t_note[:300]]
        else:
            cmd = ["event", "--task", t, "--type", "note", "--text", t_note[:300]]
        r = subprocess.run([sys.executable, MK_STATE, *cmd, *common], capture_output=True, text=True)
        try:
            results.append(json.loads(r.stdout.strip().splitlines()[-1]))
        except (json.JSONDecodeError, IndexError):
            results.append({"ok": False, "error": (r.stderr or r.stdout).strip()[-200:]})
    if len(results) == 1:
        return results[0]
    bad = [f"{t}: {x.get('error') or 'mk-state replied ok:false without an error'}"
           for t, x in zip(targets, results) if not x.get("ok")]
    res = {"ok": not bad, "children": targets, "results": results}
    if bad:
        res["error"] = "; ".join(bad)[:300]
    return res


def watch(a):
    no_commit = "--no-commit" in a
    lane, agent, grep = opt(a, "--lane"), opt(a, "--agent"), opt(a, "--commit-grep")
    report, then_send, phase = opt(a, "--report"), opt(a, "--then-send"), opt(a, "--phase")
    kind, marker = opt(a, "--kind"), opt(a, "--prompt-marker")
    max_wait = float(opt(a, "--max-wait-min", "30")) * 60
    poll = float(opt(a, "--poll", "20"))
    stall_min = float(opt(a, "--stall-min", "10"))
    rx = None if no_commit else re.compile(grep)  # --no-commit: no commit to match; decide() counts rx None as ok
    start, errors = time.time(), 0
    active, last_fp = start, None  # last time the pane showed new output
    while True:
        subj = commit_subject(lane)
        # math.inf: lane-health judges only files/commits; STALL is decided in decide() with pane activity
        h = lh.check(agent, lane, kind, math.inf, active, marker)
        text = h.get("text", "")
        fp = pane_fingerprint(text)
        # pane moved, or a subagent task line (spinner + "Task —") is running: the lane is alive, so
        # idle time counts from now; only when no task line remains does the stall clock run again
        if last_fp is not None and (fp != last_fp or running_task(text)):
            active = time.time()
            h = lh.check(agent, lane, kind, math.inf, active, marker)
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
    # working agent: STALL only from lane-watch's own idle (files/commits/pane); lane-health's STALL is ignored
    assert decide({**ok, "state": "STALL", "agent_status": "working"}, other, rx, True, "", None, 10)[0] is None
    st, ev = decide({**ok, "agent_status": "working", "last_change_min": 11}, other, rx, True, "", None, 10)
    assert st == "STALL" and "pane" in ev[0]
    # new STALL: idle/done, no matching commit, no change for stall_min since watch start
    st, ev = decide({**ok, "last_change_min": 11}, other, rx, True, "", None, 10)
    assert st == "STALL" and "matching commit" in ev[0]
    assert decide({**ok, "agent_status": "done", "last_change_min": 11}, other, rx, True, "", None, 10)[0] == "STALL"
    assert decide({**ok, "last_change_min": 9}, other, rx, True, "", None, 10)[0] is None
    # commit + report + idle, no token: DONE after the grace (lesson 39), never before; no report: still STALL
    st, ev = decide({**ok, "last_change_min": 4}, good, rx, True, "", "P1", 10)
    assert st == "DONE" and "no P1 DONE token" in ev[1], (st, ev)
    assert decide({**ok, "last_change_min": 2}, good, rx, True, "", "P1", 10)[0] is None
    assert decide({**ok, "agent_status": "working", "last_change_min": 4}, good, rx, True, "", "P1", 10)[0] is None
    st, ev = decide({**ok, "last_change_min": 11}, good, rx, False, "", "P1", 10)
    assert st == "STALL" and "report" in ev[0] and "matching commit" not in ev[0]
    # --no-commit (rx None, a reviewer pane): no commit to match — any subject is fine, no commit evidence
    assert decide(ok, other, None, True, "P1 DONE", "P1", 10) == ("DONE", ["P1 DONE"])
    assert decide(ok, other, None, True, "I will reply with P1 DONE", "P1", 10)[0] is None  # token still required
    assert decide({**ok, "agent_status": "working"}, other, None, True, "P1 DONE", "P1", 10)[0] is None
    assert decide(ok, other, None, False, "P1 DONE", "P1", 10)[0] is None  # report still required
    # the no-token grace loses its commit half too: report + idle 3 min is DONE, never before
    st, ev = decide({**ok, "last_change_min": 4}, other, None, True, "", "P1", 10)
    assert st == "DONE" and ev == ["no P1 DONE token (report, agent idle 4 min)"], (st, ev)
    assert decide({**ok, "last_change_min": 2}, other, None, True, "", "P1", 10)[0] is None
    assert decide({**ok, "agent_status": "working", "last_change_min": 4}, other, None, True, "", "P1", 10)[0] is None
    # idle STALL never lists a matching commit as missing in this mode (report gone, so the grace cannot fire)
    st, ev = decide({**ok, "last_change_min": 11}, other, None, False, "P1 DONE", "P1", 10)
    assert st == "STALL" and "report" in ev[0] and "matching commit" not in ev[0]
    # pane activity: digit-only changes (timers) are not activity; new output lines are
    assert pane_fingerprint("Working (12s) esc to interrupt") == pane_fingerprint("Working (73s) esc to interrupt")
    assert pane_fingerprint("$ vitest run\n") != pane_fingerprint("$ vitest run\n 10 failed\n")
    # the ↳ N toolcalls counter is activity (a changed count moves the fingerprint); its timer is not
    assert pane_fingerprint("⠼ General Task — E2\n↳ 49 toolcalls · 12m 48s\n") != \
        pane_fingerprint("⠼ General Task — E2\n↳ 50 toolcalls · 12m 48s\n")
    assert pane_fingerprint("⠼ General Task — E2\n↳ 49 toolcalls · 12m 48s\n") == \
        pane_fingerprint("⠼ General Task — E2\n↳ 49 toolcalls · 13m 5s\n")
    # a running task line (spinner + "Task —") is activity by presence; finished ✓ lines are not
    assert running_task("⠼ General Task — E2 reviewer key, quiet optional\n")
    assert running_task("⠋ Task — x") and running_task("⠏Task — x")
    assert not running_task("✓ General Task — E2 reviewer key\n")
    assert not running_task("some Task — mentioned mid-prose\n") and not running_task("↳ 49 toolcalls\n")
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

        # a parent task: mk-state refuses status= on it ("status rolls up from children"), so the
        # verdict goes to every child, never the parent (which only rolls up)
        mks("task", "add", "--id", "P2", "--title", "group")
        for c in ("C1", "C2", "C3"):
            mks("task", "add", "--id", c, "--title", c, "--parent", "P2")
            mks("task", "set", c, "status=building")

        def statuses():
            run_ = json.load(open(os.path.join(data, "runs", "r1", "run.json")))
            return {t["id"]: t for t in run_["tasks"]}

        r = record({"state": "DONE", "commit": good, "evidence": ["commit x"]}, "P2", data)
        assert r["ok"] and r["children"] == ["C1", "C2", "C3"], r
        st = statuses()
        assert [st[c]["status"] for c in ("C1", "C2", "C3")] == ["verifying"] * 3, st
        assert st["C1"]["last_commit"] == "abc123" and st["P2"]["status"] == "verifying", st
        for c in ("C1", "C2", "C3"):  # QUOTA / STALL / BLOCKED move all children the same way
            mks("task", "set", c, "status=building")
        r = record({"state": "QUOTA", "evidence": ["usage limit"]}, "P2", data)
        assert r["ok"], r
        st = statuses()
        assert [st[c]["status"] for c in ("C1", "C2", "C3")] == ["quota"] * 3 and st["P2"]["status"] == "quota", st
        # a child past the watched set keeps its status (late watcher, per child); the others move
        for c, s in (("C1", "building"), ("C2", "review"), ("C3", "building")):
            mks("task", "set", c, f"status={s}")
        r = record({"state": "STALL", "commit": other, "evidence": ["agent idle 11 min"]}, "P2", data)
        assert r["ok"], r
        st = statuses()
        assert (st["C1"]["status"], st["C2"]["status"], st["C3"]["status"]) == \
            ("stalled", "review", "stalled"), st
        evs2 = [json.loads(x) for x in open(os.path.join(data, "runs", "r1", "events.jsonl"))]
        assert any(e.get("task") == "C2" and "status review kept" in e.get("text", "") for e in evs2)
        # nested groups: a child that is itself a parent never gets the write either — the verdict
        # lands on the subtree's leaves, intermediate parents roll up
        mks("task", "add", "--id", "P5", "--title", "nested")
        mks("task", "add", "--id", "C4", "--title", "mid", "--parent", "P5")
        mks("task", "add", "--id", "G1", "--title", "grandchild", "--parent", "C4")
        mks("task", "add", "--id", "C5", "--title", "leaf", "--parent", "P5")
        mks("task", "set", "G1", "status=building")
        mks("task", "set", "C5", "status=building")
        r = record({"state": "DONE", "commit": good, "evidence": ["commit x"]}, "P5", data)
        assert r["ok"] and r["children"] == ["G1", "C5"], r
        st = statuses()
        assert st["G1"]["status"] == "verifying" and st["C5"]["status"] == "verifying", st
        assert st["C4"]["status"] == "verifying" and st["P5"]["status"] == "verifying", st  # rolled up

        # one child's write fails (its task.json is a directory): ok is False and the error
        # names the child — never dropped, and the healthy child still got the verdict
        mks("task", "add", "--id", "P6", "--title", "errors")
        mks("task", "add", "--id", "E1", "--title", "e1", "--parent", "P6")
        mks("task", "add", "--id", "E2", "--title", "e2", "--parent", "P6")
        mks("task", "set", "E1", "status=building")
        mks("task", "set", "E2", "status=building")
        e2json = os.path.join(data, "runs", "r1", "tasks", "E2", "task.json")
        os.unlink(e2json)
        os.makedirs(e2json)  # unwritable: the write fails, the error is reported
        r = record({"state": "DONE", "commit": good, "evidence": ["commit x"]}, "P6", data)
        assert r["ok"] is False and r["children"] == ["E1", "E2"], r
        assert r["error"].startswith("E2:") and "IsADirectoryError" in r["error"], r
        st = statuses()
        assert st["E1"]["status"] == "verifying", st  # the healthy child still got the verdict
        assert st["E2"]["status"] == "building", st  # the failing child kept its status

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
        # CLI end to end on a parent: the verdict reaches its children through the same record()
        mks("task", "add", "--id", "P4", "--title", "cli group")
        for c in ("D1", "D2", "D3"):
            mks("task", "add", "--id", c, "--title", c, "--parent", "P4")
            mks("task", "set", c, "status=building")
        out = cli("--commit-grep", r"feat\(p1\)", "--phase", "P1", "--kind", "codex", "--prompt-marker",
                  "Read and follow brief-r1.md", "--poll", "0.1", "--task", "P4", "--data", data)
        assert out["state"] == "DONE" and out["state_written"] is True, out
        st = statuses()
        assert [st[c]["status"] for c in ("D1", "D2", "D3")] == ["verifying"] * 3, st
        assert st["P4"]["status"] == "verifying", st
        out = cli("--commit-grep", "(", "--poll", "0.1", "--task", "P3", "--data", data)  # bad regex → ERROR
        assert out["state"] == "ERROR" and "missing )" in out["evidence"][0] and out["state_written"] is True, out
        assert cli("--commit-grep", "x", "--task", "P3", code=1).returncode == 1  # --task without --data
        # --no-commit (a reviewer pane): --commit-grep is not required and the commit subject is irrelevant
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "docs: merge lane notes"], check=True)
        open(pane, "w").write("> Read and follow review-brief-r1.md\nreport written, gates green\n⏺ D2 REVIEW R1 DONE\n")
        open(os.path.join(lane, "review-r1.md"), "w").write("# review r1\nship it\n")
        out = cli("--no-commit", "--report", "review-r1.md", "--phase", "D2 REVIEW R1", "--kind", "codex",
                  "--prompt-marker", "Read and follow review-brief-r1.md", "--poll", "0.1",
                  "--task", "P3", "--data", data)
        assert out["state"] == "DONE" and out["evidence"] == ["D2 REVIEW R1 DONE"], out
        assert out["state_written"] is True, out
        # without --no-commit, --commit-grep stays required: usage error, exit 2
        r = subprocess.run([sys.executable, __file__, "--lane", lane, "--agent", "w1", "--report", "review-r1.md",
                            "--phase", "D2 REVIEW R1"], env=env, capture_output=True, text=True)
        assert r.returncode == 2 and "Usage" in r.stderr, (r.returncode, r.stderr[:200])
        # working agent, no file/commit change: a pane that keeps moving is never STALL (lesson 37); a frozen one is
        work_bin = os.path.join(tmp, "workbin")
        os.makedirs(work_bin)
        grow = os.path.join(tmp, "grow.txt")
        cnt = os.path.join(tmp, "cnt.txt")
        open(grow, "w").write("")
        with open(os.path.join(work_bin, "herdr"), "w") as f:
            f.write('#!/bin/sh\nif [ "$2" = get ]; then echo \'{"result":{"agent":{"agent_status":"working"}}}\'; '
                    # like real herdr: a working full-screen agent refuses history reads; only --source visible
                    'elif [ "$5" != visible ]; then echo \'{"error":{"code":"agent_not_idle"}}\'; '
                    f'else echo "> Read and follow brief-r3.md"; cat "{grow}"; '
                    f'if [ -n "$MK_GROW" ]; then echo thinking >> "{grow}"; fi; '
                    # MK_COUNT: only the ↳ N toolcalls counter moves between polls, nothing else does
                    f'if [ -n "$MK_COUNT" ]; then n=$(cat "{cnt}" 2>/dev/null || echo 49); '
                    f'echo "↳ $n toolcalls · 12m 48s"; echo $((n + 1)) > "{cnt}"; fi; fi\n')
        os.chmod(os.path.join(work_bin, "herdr"), 0o755)
        for grows, want in ((True, "WATCH_EXPIRED"), (False, "STALL")):
            e2 = dict(env, PATH=work_bin + os.pathsep + os.environ["PATH"], **({"MK_GROW": "1"} if grows else {}))
            r = subprocess.run([sys.executable, __file__, "--lane", lane, "--agent", "w1", "--commit-grep", "nomatch",
                                "--prompt-marker", "Read and follow brief-r3.md", "--poll", "0.1",
                                "--stall-min", "0.02", "--max-wait-min", "0.06"], env=e2, capture_output=True, text=True)
            out = json.loads(r.stdout.strip().splitlines()[-1])
            assert out["state"] == want, (grows, out)

        def probe(pane_txt, want, **extra):
            """One watch run against the working-agent fake; assert its exit state."""
            open(grow, "w").write(pane_txt)
            e3 = dict(env, PATH=work_bin + os.pathsep + os.environ["PATH"], **extra)
            r = subprocess.run([sys.executable, __file__, "--lane", lane, "--agent", "w1", "--commit-grep", "nomatch",
                                "--prompt-marker", "Read and follow brief-r3.md", "--poll", "0.1",
                                "--stall-min", "0.02", "--max-wait-min", "0.06"], env=e3, capture_output=True, text=True)
            out = json.loads(r.stdout.strip().splitlines()[-1])
            assert out["state"] == want, (pane_txt or extra, out)

        # tonight's false STALL: a pane whose only life is a running subagent task line (spinner +
        # "Task —", text frozen between polls so the fingerprint never moves) is activity; a pane
        # with only finished "✓ … Task —" lines is not; a lone ticking ↳ N toolcalls counter is
        probe("⠼ General Task — E2 reviewer key, quiet optional\n↳ 49 toolcalls · 12m 48s\n", "WATCH_EXPIRED")
        probe("✓ General Task — E2 reviewer key\n", "STALL")
        probe("", "WATCH_EXPIRED", MK_COUNT="1")
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
    if not (opt(a, "--lane") and opt(a, "--agent")) or ("--no-commit" not in a and not opt(a, "--commit-grep")):
        sys.stderr.write(__doc__)  # usage: --commit-grep is required unless --no-commit (a reviewer pane)
        sys.exit(2)
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
