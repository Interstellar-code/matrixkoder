#!/usr/bin/env python3
"""Watch one lane until it finishes, fails, or the watch window ends — never silently.

Usage:
  lane-watch.py --lane DIR --agent NAME|PANE --commit-grep REGEX [--report PATH]
                [--then-send BRIEF_PATH] [--max-wait-min 30] [--poll 20] [--stall-min 10]

Done = latest commit subject matches REGEX AND (report exists, if given) AND agent not working.
With --then-send, on DONE it dispatches "Read and follow <BRIEF_PATH> exactly ..." to the same agent
via the herdr skill's herdr-ask.py (which refuses busy agents, so we only send once idle).
Exit states (one JSON line): DONE | DONE_SENT | QUOTA | PROVIDER | STALL | BLOCKED | DEV_SERVER | ERROR | WATCH_EXPIRED.
Run in the background (30 min default; foreground calls must stay under their 10-min timeout); re-arm on WATCH_EXPIRED.
"""
import json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ASK = os.path.expanduser("~/.claude/skills/herdr/scripts/herdr-ask.py")


def opt(a, f, d=None):
    return a[a.index(f) + 1] if f in a else d


def health(agent, lane, stall_min, since):
    r = subprocess.run([sys.executable, os.path.join(HERE, "lane-health.py"), "--agent", agent, "--lane", lane,
                        "--stall-min", str(stall_min), "--since", str(since)], capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"state": "ERROR", "evidence": [r.stderr.strip()[:200]]}


def commit_subject(lane):
    return subprocess.run(["git", "-C", lane, "log", "-1", "--format=%h %s"], capture_output=True, text=True).stdout.strip()


def main():
    a = sys.argv[1:]
    lane, agent, grep = opt(a, "--lane"), opt(a, "--agent"), opt(a, "--commit-grep")
    if not (lane and agent and grep):
        sys.exit(__doc__)
    report = opt(a, "--report")
    then_send = opt(a, "--then-send")
    max_wait = float(opt(a, "--max-wait-min", "30")) * 60
    poll = float(opt(a, "--poll", "20"))
    stall_min = float(opt(a, "--stall-min", "10"))
    rx = re.compile(grep)
    start = time.time()
    while True:
        subj = commit_subject(lane)
        h = health(agent, lane, stall_min, start)
        done = bool(rx.search(subj)) and (not report or os.path.exists(os.path.join(lane, report)))
        if h["state"] in ("QUOTA", "PROVIDER", "BLOCKED", "DEV_SERVER", "ERROR"):
            print(json.dumps({"state": h["state"], "commit": subj, "evidence": h.get("evidence")}))
            return
        if done and h.get("agent_status") != "working":
            if then_send:
                msg = f"Read and follow {then_send} exactly. Work autonomously until complete, then reply with the DONE token it names."
                r = subprocess.run([sys.executable, ASK, agent, msg, "--no-wait"], capture_output=True, text=True)
                state = "DONE_SENT" if r.returncode == 0 else "ERROR"
                print(json.dumps({"state": state, "commit": subj, "sent": then_send, "ask_rc": r.returncode,
                                  "ask_out": (r.stdout + r.stderr).strip()[-200:]}))
            else:
                print(json.dumps({"state": "DONE", "commit": subj}))
            return
        if h["state"] == "STALL":
            print(json.dumps({"state": "STALL", "commit": subj, "evidence": h.get("evidence")}))
            return
        if time.time() - start > max_wait:
            print(json.dumps({"state": "WATCH_EXPIRED", "commit": subj, "agent_status": h.get("agent_status"),
                              "last_change_min": h.get("last_change_min")}))
            return
        time.sleep(poll)


if __name__ == "__main__":
    main()
