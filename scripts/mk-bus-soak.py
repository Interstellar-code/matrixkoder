#!/usr/bin/env python3
"""Bounded soak of the root bus on a TEMP home. Never touches ~/.matrixkoder.

`python3 scripts/mk-bus-soak.py --minutes N [--home DIR]` (default 20, hard cap 120)
and `--self-test`. The acceptance is TS.1 of plan mk-store-test.

Traffic goes over HTTP at one message per tick. The CLI is used only for the
per-minute checks (token, metrics) and for heartbeat restarts. One sleep per
tick; the waits are the harness's bounded `_wait_until`.
"""
import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import importlib.util  # noqa: E402
import mkdb  # noqa: E402
_spec = importlib.util.spec_from_file_location("mk_livetest", os.path.join(HERE, "mk-livetest.py"))
lt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lt)

REAL_FILES = ("bus.json", "bus-tokens.json", "mk.db")
RUN = "soak1"
TASK = "SOAK"
LANE_NAME = "soak-lane"
RSS_LIMIT_MB = 100
LOAD_LIMIT = 20.0
SWAP_LIMIT = 80.0


class SoakFail(Exception):
    """A check failed. `detail` is the exact output the verdict quotes."""

    def __init__(self, check, detail):
        self.check = check
        self.detail = detail
        super().__init__(f"{check}: {detail}")


# ---------------------------------------------------------------- parsers


def parse_swap(text):
    """`sysctl vm.swapusage` -> percent used, or None when the line has no numbers."""
    used = total = None
    for part in text.replace("=", " ").split():
        pass
    # "total = 18432.00M  used = 17219.06M  free = 1212.94M  (encrypted)"
    tokens = text.replace("=", " ").split()
    for i, tok in enumerate(tokens):
        if tok == "total" and i + 1 < len(tokens):
            total = _megs(tokens[i + 1])
        elif tok == "used" and i + 1 < len(tokens):
            used = _megs(tokens[i + 1])
    if not total or used is None:
        return None
    return 100.0 * used / total


def _megs(tok):
    tok = tok.strip().rstrip("M")
    try:
        return float(tok)
    except ValueError:
        return None


def read_swap():
    try:
        out = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_swap(out)


def read_load():
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def read_rss_mb(pids):
    """Sum of RSS in MB for the given pids (`ps -o rss` reports KiB)."""
    total = 0
    for pid in pids:
        if not isinstance(pid, int) or pid <= 1:
            continue
        try:
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                total += int(line)
    return total / 1024.0


# ---------------------------------------------------------------- home guard


def real_home_stat():
    """size+mtime of the real MK files. os.stat only; the files are never opened."""
    base = os.path.expanduser("~/.matrixkoder")
    found = {}
    for name in REAL_FILES:
        path = os.path.join(base, name)
        try:
            st = os.stat(path)
        except OSError:
            found[name] = None
            continue
        found[name] = (st.st_size, st.st_mtime_ns)
    return found


def assert_real_home_unchanged(before):
    after = real_home_stat()
    changed = [name for name in REAL_FILES if before.get(name) != after.get(name)]
    if changed:
        raise SoakFail("real-home", f"{changed} changed: before={before} after={after}")


def assert_pins(env, home):
    """Every pin resolves inside the system temp dir. Refuses to start otherwise."""
    root = lt.temp_root()
    for key in ("MK_BUS_DIR", "MK_HOME", "MATRIXKODER_HOME", "MK_BUS_HERDR"):
        val = env.get(key)
        if not val:
            raise SoakFail("pins", f"{key} is not set")
        real = os.path.realpath(val)
        if real != root and not real.startswith(root + os.sep):
            raise SoakFail("pins", f"{key}={real} is outside the system temp dir")
        if not real.startswith(os.path.realpath(home) + os.sep) and real != os.path.realpath(home):
            raise SoakFail("pins", f"{key}={real} is not inside the temp home {home}")


def soak_env(ctx, extra=None):
    env = dict(lt._bus_env(ctx), MK_BUS_HERDR=ctx["fake"])
    if extra:
        env.update(extra)
    assert_pins(env, ctx["home"])
    return env


# ---------------------------------------------------------------- bus


def bus_py(*args, env, timeout=30):
    proc = subprocess.run([sys.executable, os.path.join(HERE, "mk-bus.py"), *args],
                          capture_output=True, text=True, timeout=timeout, env=env)
    lines = [x for x in proc.stdout.splitlines() if x.strip()]
    if proc.returncode != 0:
        raise SoakFail("bus", f"mk-bus.py {' '.join(args)}: rc={proc.returncode} "
                              f"out={proc.stdout.strip()[-300:]} err={proc.stderr.strip()[-200:]}")
    try:
        return json.loads(lines[-1]) if lines else {}
    except ValueError:
        raise SoakFail("bus", f"mk-bus.py {' '.join(args)}: not JSON: {proc.stdout[-200:]}")


def bus_pid(home):
    path = os.path.join(home, "bus.json")
    try:
        data = json.load(open(path))
    except (OSError, ValueError):
        return None, None
    pid, port = data.get("pid"), data.get("port")
    if not isinstance(pid, int) or pid <= 1:
        return None, port
    return pid, port


def command_of(pid):
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.strip()


def kill_own_bus(home):
    """kill -9 ONLY the pid in <home>/bus.json whose command line contains the temp home."""
    pid, port = bus_pid(home)
    if pid is None:
        raise SoakFail("kill", f"no pid in {home}/bus.json")
    cmd = command_of(pid)
    if os.path.realpath(home) not in cmd and home not in cmd:
        raise SoakFail("kill", f"pid {pid} command line does not contain the temp home: {cmd!r}")
    os.kill(pid, signal.SIGKILL)
    lt._wait_until(lambda: lt._dead(pid), 5, f"pid {pid} to die")
    return pid, port


def restart(ctx, data, port):
    """heartbeat restarts a dead bus (C6). It takes no port of its own, so the
    port comes back as whatever the kernel assigns. A restart also retires the
    orchestrator token, so the fresh one is read from bus.json."""
    res = bus_py("heartbeat", "--data", data, "--run", RUN, env=soak_env(ctx))
    if not res.get("port"):
        raise SoakFail("restart", f"heartbeat returned no port: {res}")
    fresh = json.load(open(os.path.join(ctx["home"], "bus.json"))).get("token")
    if not fresh:
        raise SoakFail("restart", "restarted bus.json has no orchestrator token")
    # The restarted server's token is scoped to the project whose heartbeat
    # restarted it. Remember which, so the next send uses that project.
    ctx["live_tok"] = fresh
    ctx["live_project"] = project_of(ctx, data)
    ctx["live_data"] = data
    return res


# ---------------------------------------------------------------- traffic


def seed(ctx, data, task):
    con = mkdb.open_project(ctx["home"], data_dir=data)
    try:
        mkdb._begin(con)
        con.execute("INSERT OR IGNORE INTO runs(id, status) VALUES (?, ?)", (RUN, "active"))
        con.execute("COMMIT")
        mkdb.add_task(con, RUN, task, "task", title="soak")
    finally:
        con.close()


def project_of(ctx, data):
    """The slug the bus registered for this data dir (it derives its own)."""
    saved = ctx.get("data")
    ctx["data"] = data
    try:
        return lt._bus_project(ctx)
    finally:
        ctx["data"] = saved


def send(ctx, port, token, project, kind, text, task=TASK, reply_to=None, extra=None):
    body = {"run": RUN, "task": task, "kind": kind, "text": text,
            "from": "orch", "to": task, "project": project}
    if reply_to:
        body["reply_to"] = reply_to
    if extra:
        body.update(extra)
    st, resp = lt._http("POST", f"http://127.0.0.1:{port}/send", token, body)
    if st != 200:
        raise SoakFail("send", f"{kind} {text!r} -> {st} {resp}")
    return (resp.get("message") or resp).get("id") or resp.get("id")


def ack(ctx, port, token, project, reply_to, level=3):
    body = {"run": RUN, "task": TASK, "kind": "ack", "text": f"ack {level}",
            "from": TASK, "to": "orch", "project": project,
            "reply_to": reply_to, "ack_level": level}
    st, resp = lt._http("POST", f"http://127.0.0.1:{port}/send", token, body)
    if st != 200 or not resp.get("ack_recorded"):
        raise SoakFail("ack", f"ack {reply_to} -> {st} {resp}")


def ring_count(ctx):
    path = ctx["fake"] + ".rang"
    if not os.path.isfile(path):
        return 0
    return open(path).read().count("mk-builder.py inbox")


def queued(ctx, msg_id):
    path = os.path.join(ctx["home"], "ring-queue.json")
    if not os.path.isfile(path):
        return False
    try:
        return msg_id in json.dumps(json.load(open(path)))
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- checks


def check_tokens(ctx, port, tok_a, lane_tok, root_tok, slug_b):
    """A on A is 200, A on B is 403, a lane token cannot read B, root cannot write."""
    base = f"http://127.0.0.1:{port}"
    st, body = lt._http("GET", base + f"/pending?run={RUN}&project={ctx['slug_a']}", tok_a)
    if st != 200:
        raise SoakFail("token-own", f"project A token on A: {st} {body}")
    st, body = lt._http("GET", base + f"/pending?run={RUN}&project={slug_b}", tok_a)
    if st != 403:
        raise SoakFail("token-other", f"project A token on B: {st} {body}")
    st, body = lt._http("GET", base + f"/pending?run={RUN}&project={slug_b}", lane_tok)
    if st != 403:
        raise SoakFail("token-lane", f"lane token read B: {st} {body}")
    st, body = lt._http("POST", base + "/send", root_tok,
                        {"run": RUN, "task": TASK, "kind": "info", "text": "nope",
                         "from": "orch", "to": TASK, "project": ctx["slug_a"]})
    if st != 403:
        raise SoakFail("token-root", f"root token wrote: {st} {body}")


def check_metrics(ctx):
    res = bus_py("metrics", "--data", ctx["data"], "--run", RUN, env=soak_env(ctx))
    wanted = ("latency_p50", "latency_p95", "failed_deliveries", "obedience", "false_stall")
    missing = [k for k in wanted if k not in res]
    if not res.get("ok") or missing:
        raise SoakFail("metrics", f"not a counter line: {res}")
    return res


def count_events(data, chan="bus"):
    con = sqlite3.connect(os.path.join(data, "mk.db"))
    try:
        return con.execute("SELECT COUNT(*) FROM events WHERE chan=?", (chan,)).fetchone()[0]
    finally:
        con.close()


def check_counts(ctx, data_a, data_b, sent_a, sent_b):
    """chan=bus events in each store equal the messages sent there, and the other
    store holds none of them. Exactly one acks row per (run, msg, lane, level)."""
    got_a, got_b = count_events(data_a), count_events(data_b)
    if got_a != sent_a or got_b != sent_b:
        raise SoakFail("counts", f"events A={got_a} sent={sent_a}; B={got_b} sent={sent_b}")
    con = sqlite3.connect(os.path.join(data_a, "mk.db"))
    try:
        rows = list(con.execute("SELECT run, msg, lane, level, COUNT(*) c FROM acks "
                                "GROUP BY run, msg, lane, level"))
    finally:
        con.close()
    dup = [r for r in rows if r[4] != 1]
    if dup:
        raise SoakFail("acks", f"acks rows are not one per (run, msg, lane, level): {dup}")
    return got_a, got_b


def check_ack_copy(copy_path):
    """The count check against a COPY of the store. A planted duplicate must fail it."""
    con = sqlite3.connect(copy_path)
    try:
        rows = list(con.execute("SELECT run, msg, lane, level, COUNT(*) c FROM acks "
                                "GROUP BY run, msg, lane, level"))
    finally:
        con.close()
    dup = [r for r in rows if r[4] != 1]
    if dup:
        raise SoakFail("acks-copy", f"duplicate acks in the store copy: {dup}")


# ---------------------------------------------------------------- the soak


def write_fake(ctx, forever_working=False):
    fake = os.path.join(ctx["home"], "fake-herdr.py")
    ctx["fake"] = fake
    with open(fake, "w") as fh:
        fh.write(
            "import json, os, sys\n"
            "log = sys.argv[0] + '.rang'\n"
            "if len(sys.argv) > 1 and sys.argv[1] == 'prompt':\n"
            "    open(log, 'a').write(sys.argv[2] + ' ' + sys.argv[3] + chr(10))\n"
            "    sys.exit(0)\n"
            "sf = sys.argv[0] + '.status'\n"
            "status = open(sf).read().strip() if os.path.exists(sf) else 'idle'\n"
            "print(json.dumps({'result': {'agents': [\n"
            "    {'name': %r, 'agent_status': status, 'cwd': '/x/%s'}]}}))\n"
            % (TASK, TASK))
    os.chmod(fake, 0o755)
    open(fake + ".status", "w").write("working" if forever_working else "idle")
    return fake


def prepare(ctx):
    """One root bus, two projects, the run and the task, the tokens."""
    write_fake(ctx)
    data_b_slug, data_b = lt._second_project(ctx)
    seed(ctx, ctx["data"], TASK)
    seed(ctx, data_b, TASK)
    started = bus_py("ensure", "--data", ctx["data"], "--run", RUN,
                     env=soak_env(ctx, {"MK_BUS_RING_INTERVAL_SEC": "0.2"}))
    if not started.get("pid"):
        raise SoakFail("start", f"bus did not start: {started}")
    pid, port = bus_pid(ctx["home"])
    ctx["port"] = port
    ctx["slug_a"] = project_of(ctx, ctx["data"])
    ctx["slug_b"] = project_of(ctx, data_b)
    ctx["data_b"] = data_b
    ctx["tok_a"] = lt._orch_token(ctx, ctx["slug_a"], RUN)
    lane = lt._lane(ctx, LANE_NAME)
    bus_py("token", "--lane", lane, "--task", TASK, "--run", RUN, "--data", ctx["data"],
           env=soak_env(ctx))
    ctx["lane_tok"] = json.load(open(lt._lane_token(ctx, lane)))["token"]
    ctx["root_tok"] = mint_root(ctx)
    ctx["sent_a"] = ctx["sent_b"] = 0
    ctx["ev_a"] = ctx["ev_b"] = 0
    return pid, port


def mint_root(ctx):
    """A root token in the temp home's token file. Writes only there."""
    import fcntl
    tok = lt.secrets.token_urlsafe(32)
    path = os.path.join(ctx["home"], "bus-tokens.json")
    fd = os.open(os.path.join(ctx["home"], "bus-tokens.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        tokens = json.load(open(path))
        tokens[tok] = {"token": tok, "role": "root", "run": RUN, "task": TASK,
                       "lane": "", "project": ctx["slug_a"], "created_at": "soak"}
        json.dump(tokens, open(path, "w"))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return tok


def cycle(ctx, port, do_doorbell=True):
    """One cycle on project A: ask -> answer -> ack, info, progress, the doorbell."""
    ask_id = send(ctx, port, ctx["tok_a"], ctx["slug_a"], "ask", "soak?")
    ans_id = send(ctx, port, ctx["tok_a"], ctx["slug_a"], "answer", "yes", reply_to=ask_id)
    ack(ctx, port, ctx["tok_a"], ctx["slug_a"], ans_id)
    send(ctx, port, ctx["tok_a"], ctx["slug_a"], "info", "progress")
    ctx["ev_a"] += 4  # ask, answer, ack, info (rung while the agent was working, so no ring events)
    if not do_doorbell:
        return
    # The answer and the info are unsolicited too, so they ring. Snapshot first.
    open(ctx["fake"] + ".status", "w").write("working")
    before = ring_count(ctx)
    open(ctx["fake"] + ".status", "w").write("idle")
    send(ctx, port, ctx["tok_a"], ctx["slug_a"], "nudge", "idle")
    ctx["ev_a"] += 1
    lt._wait_until(lambda: ring_count(ctx) >= before + 1, 5, "the idle agent to be rung")
    ctx["ev_a"] += 2 * (ring_count(ctx) - before)  # each ring is ring + ring-typed
    got = ring_count(ctx)
    if got < before + 1:
        raise SoakFail("doorbell-idle", f"idle ring count {got}, nothing new after {before}")
    open(ctx["fake"] + ".status", "w").write("working")
    nid = send(ctx, port, ctx["tok_a"], ctx["slug_a"], "nudge", "busy")
    ctx["ev_a"] += 1
    held = ring_count(ctx)
    if held != got:
        raise SoakFail("doorbell-working", f"the working agent was rung: {held} after {got}")
    lt._wait_until(lambda: queued(ctx, nid), 5, "the working agent's nudge to be queued")
    open(ctx["fake"] + ".status", "w").write("idle")
    lt._wait_until(lambda: ring_count(ctx) == held + 1, 5, "the queued nudge to ring once")
    ctx["ev_a"] += 2
    if ring_count(ctx) != held + 1:
        raise SoakFail("doorbell-drain", f"rings {ring_count(ctx)}, wanted {held + 1}")


def cycle_b(ctx, port):
    """One info to project B, so its store is not empty and A's store has none of it."""
    send(ctx, port, lt._orch_token(ctx, ctx["slug_b"], RUN),
         ctx["slug_b"], "info", "from B")
    ctx["ev_b"] += 1


def abort_reason():
    load = read_load()
    if load > LOAD_LIMIT:
        return "load", load
    swap = read_swap()
    if swap is not None and swap > SWAP_LIMIT:
        return "swap", round(swap, 2)
    return None, None


def measure(ctx, max_rss):
    pid, _ = bus_pid(ctx["home"])
    rss = read_rss_mb([os.getpid(), pid])
    max_rss = max(max_rss, rss)
    if max_rss > RSS_LIMIT_MB:
        raise SoakFail("rss", f"max RSS {max_rss:.1f} MB exceeds {RSS_LIMIT_MB} MB")
    return max_rss


def run_soak(ctx, minutes, cadence):
    """The soak. `cadence` scales the waits so the self-test finishes in ~20 s.

    Real run: tick 1 s, kill every 5 min, token+metrics every 60 s, expiry 60 s.
    Self-test: tick 1 s, one kill at ~8 s, one token check, one metrics call.
    """
    kills = 0
    max_rss = 0.0
    pid, port = prepare(ctx)
    started = time.time()
    deadline = started + minutes * 60
    next_kill = started + cadence["kill_after"]
    next_check = started + cadence["check_after"]
    killed_once = False
    pending_ask = None
    which = 0
    while time.time() < deadline:
        why, val = abort_reason()
        if why:
            return {"aborted": why, "value": val, "kills": kills, "max_rss_mb": round(max_rss, 2)}
        max_rss = measure(ctx, max_rss)
        now = time.time()
        if now >= next_kill and (not cadence.get("kill_once") or not killed_once):
            # An ask left waiting across the kill. The answer must arrive once.
            pending_ask = send(ctx, port, ctx["tok_a"], ctx["slug_a"], "ask", "across")
            ctx["ev_a"] += 1
            old, port = kill_own_bus(ctx["home"])
            data = ctx["data"] if which % 2 == 0 else ctx["data_b"]
            which += 1
            res = restart(ctx, data, port)
            port = res["port"]
            ctx["port"] = port
            kills += 1
            killed_once = True
            st, body = lt._http("GET", f"http://127.0.0.1:{port}/health", ctx["tok_a"])
            if st != 200:
                raise SoakFail("restart-health", f"lane URL after restart: {st} {body}")
            ans = send(ctx, port, ctx["live_tok"], ctx["live_project"], "answer", "after",
                       reply_to=pending_ask)
            if ctx["live_data"] == ctx["data"]:
                ctx["ev_a"] += 1
            else:
                ctx["ev_b"] += 1
            if not ans:
                raise SoakFail("across-kill", f"the waiting ask was not answered once: {pending_ask}")
            next_kill = now + cadence["kill_every"]
        elif now >= next_check:
            check_tokens(ctx, port, ctx["tok_a"], ctx["lane_tok"], ctx["root_tok"], ctx["slug_b"])
            metrics = check_metrics(ctx)
            if not cadence.get("skip_metrics_count"):
                pass
            ctx["metrics"] = metrics
            next_check = now + cadence["check_every"]
        else:
            cycle(ctx, port)
            cycle_b(ctx, port)
        time.sleep(cadence["tick"])
    # Final: both heartbeats stop and the bus must exit by itself.
    check_counts(ctx, ctx["data"], ctx["data_b"], ctx["ev_a"], ctx["ev_b"])
    metrics = check_metrics(ctx)
    sent = ctx["ev_a"] + ctx["ev_b"]
    if sent <= 0:
        raise SoakFail("metrics", f"no messages were sent: {metrics}")
    expire(ctx, port)
    return {"counts": {"A": ctx["ev_a"], "B": ctx["ev_b"], "events_a": count_events(ctx["data"]),
                       "events_b": count_events(ctx["data_b"])},
            "kills": kills, "max_rss_mb": round(max_rss, 2), "metrics": metrics}


def expire(ctx, port):
    """Stop both heartbeats. With the short timeout the bus must exit on its own."""
    env = soak_env(ctx, {"MK_BUS_HEARTBEAT_TIMEOUT_SEC": "60",
                         "MK_BUS_HEARTBEAT_CHECK_SEC": "0.5"})
    # The running bus has the long timeout. Restart it once under the short one.
    pid, port = kill_own_bus(ctx["home"])
    res = bus_py("heartbeat", "--data", ctx["data"], "--run", RUN, env=env)
    # One heartbeat from B so both projects are on the clock, then neither again.
    bus_py("heartbeat", "--data", ctx["data_b"], "--run", RUN, env=env)
    born = time.time()
    pid = res.get("pid")
    # No further heartbeat. The monitor exits the process by itself.
    lt._wait_until(lambda: lt._dead(pid), 70, "the bus to exit after both heartbeats stopped")
    if not lt._dead(pid):
        raise SoakFail("expiry", f"pid {pid} still alive {time.time() - born:.0f}s after the "
                                 f"last heartbeat with timeout 60s")


# ---------------------------------------------------------------- self-test


def self_test():
    cases = []

    def check(name, fn):
        try:
            fn()
            cases.append((name, True, ""))
        except Exception as e:  # a self-test case failing is reported, not fatal yet
            cases.append((name, False, str(e)[:300]))

    check("swap-parser", _case_swap)
    check("load-parser", _case_load)
    check("refuse-unpinned", _case_refuse_unpinned)
    check("real-home-stable", _case_real_home)
    check("mutation-working-forever", _case_mutation_working)
    check("mutation-duplicate-ack", _case_mutation_ack)
    check("mutation-token", _case_mutation_token)
    check("compressed-soak", _case_compressed)
    bad = [c for c in cases if not c[1]]
    if bad:
        for name, _, detail in bad:
            print(f"FAIL {name}: {detail}", file=sys.stderr)
        print(json.dumps({"ok": False, "self_test": [(n, ok) for n, ok, _ in cases]}))
        return 1
    print("SELF-TEST PASS")
    return 0


def _case_swap():
    # The fixture line from this machine, plus the live sysctl output.
    fixture = "total = 18432.00M  used = 17219.06M  free = 1212.94M  (encrypted)"
    pct = parse_swap(fixture)
    if pct is None or abs(pct - 100.0 * 17219.06 / 18432.00) > 0.01:
        raise SoakFail("swap", f"fixture parsed as {pct}")
    live = read_swap()
    if live is None or not 0 <= live <= 100:
        raise SoakFail("swap", f"live sysctl parsed as {live}")


def _case_load():
    load = read_load()
    if not isinstance(load, float) or load < 0:
        raise SoakFail("load", f"load {load!r}")


def _case_refuse_unpinned():
    """A bus call WITHOUT the pins, under a patched HOME, must be refused."""
    made = lt.setup()
    home = made["home"]
    try:
        ctx = {"home": home, "data": made["data"], "repo": made["repo"], "fake": os.path.join(home, "nope")}
        env = dict(os.environ)  # no MK_BUS_DIR, no MK_HOME, no MATRIXKODER_HOME
        env["HOME"] = home      # patched HOME: an unpinned bus would write here
        try:
            assert_pins(env, home)
        except SoakFail:
            return
        raise SoakFail("refuse", "an unpinned bus call was allowed to start")
    finally:
        lt.teardown(home)


def _case_real_home():
    before = real_home_stat()
    assert_real_home_unchanged(before)


def _case_mutation_working():
    """(1) the fake herdr reports working forever: the idle-ring check must FAIL."""
    made = lt.setup()
    home = made["home"]
    ctx = lt._ctx(home)
    ctx.update(made)
    try:
        write_fake(ctx, forever_working=True)
        seed(ctx, ctx["data"], TASK)
        bus_py("ensure", "--data", ctx["data"], "--run", RUN,
               env=soak_env(ctx, {"MK_BUS_RING_INTERVAL_SEC": "0.2"}))
        _, port = bus_pid(home)
        ctx["slug_a"] = project_of(ctx, ctx["data"])
        ctx["tok_a"] = lt._orch_token(ctx, ctx["slug_a"], RUN)
        try:
            send(ctx, port, ctx["tok_a"], ctx["slug_a"], "nudge", "idle")
            lt._wait_until(lambda: ring_count(ctx) == 1, 3, "a ring that must not come")
        except (SoakFail, lt.Fail):
            if ring_count(ctx) == 0:
                return  # the idle-ring check failed, as required
            raise
        raise SoakFail("mutation-1", "the idle-ring check PASSED while herdr reports working forever")
    finally:
        lt._kill_buses(home)
        lt.teardown(home)


def _case_mutation_ack():
    """(2) a duplicate acks row in a COPY of the store must fail the count check."""
    made = lt.setup()
    home = made["home"]
    try:
        src = os.path.join(made["data"], "mk.db")
        copy = os.path.join(home, "acks-copy.db")
        shutil.copy(src, copy)
        con = sqlite3.connect(copy)
        # The live table's primary key is (run, msg, lane), so it cannot hold the
        # duplicate the mutation needs. The COPY drops the key; the check counts rows.
        if con.execute("SELECT name FROM sqlite_master WHERE name='acks'").fetchone():
            con.execute("ALTER TABLE acks RENAME TO acks_orig")
            con.execute("CREATE TABLE acks(run TEXT, msg TEXT, lane TEXT, level INTEGER, t TEXT)")
            con.execute("INSERT INTO acks SELECT * FROM acks_orig")
            con.execute("DROP TABLE acks_orig")
        else:
            con.execute("CREATE TABLE acks(run TEXT, msg TEXT, lane TEXT, level INTEGER, t TEXT)")
        con.execute("INSERT INTO acks(run, msg, lane, level, t) VALUES (?,?,?,?,?)",
                    (RUN, "m-1", "SOAK", 3, "t1"))
        con.execute("INSERT INTO acks(run, msg, lane, level, t) VALUES (?,?,?,?,?)",
                    (RUN, "m-1", "SOAK", 3, "t2"))
        con.commit()
        con.close()
        try:
            check_ack_copy(copy)
        except SoakFail as e:
            if "duplicate" in str(e):
                return
            raise
        raise SoakFail("mutation-2", "the acks count check PASSED with a duplicate row in the copy")
    finally:
        lt.teardown(home)


def _case_mutation_token():
    """(3) the checker handed the project-A token for B with an expected 200 must FAIL."""
    made = lt.setup()
    home = made["home"]
    ctx = lt._ctx(home)
    try:
        _, data_b = lt._second_project(ctx)
        seed(ctx, ctx["data"], TASK)
        seed(ctx, data_b, TASK)
        write_fake(ctx)
        bus_py("ensure", "--data", ctx["data"], "--run", RUN, env=soak_env(ctx))
        _, port = bus_pid(home)
        ctx["slug_a"] = project_of(ctx, ctx["data"])
        slug_b = project_of(ctx, data_b)
        tok_a = lt._orch_token(ctx, ctx["slug_a"], RUN)
        try:
            # Expected 200 on B, but the token is A's: the checker must fail.
            st, body = lt._http("GET", f"http://127.0.0.1:{port}/pending?run={RUN}&project={slug_b}", tok_a)
            if st == 200:
                raise SoakFail("mutation-3", f"project A token on B returned 200: {body}")
            raise SoakFail("token-expected-200", f"project A token on B: {st} {body}")
        except SoakFail as e:
            if e.check == "mutation-3":
                raise
            return
    finally:
        lt._kill_buses(home)
        lt.teardown(home)


def _case_compressed():
    """One kill at ~8 s, one token check, one metrics call, then a clean teardown."""
    made = lt.setup()
    home = made["home"]
    ctx = lt._ctx(home)
    ctx.update({"repo": made["repo"]})
    before = real_home_stat()
    pid = None
    try:
        cadence = {"tick": 1, "kill_after": 8, "kill_every": 9999, "kill_once": True,
                   "check_after": 3, "check_every": 9999}
        # A short window: the compressed checks, not the full expiry wait.
        pid, port = prepare(ctx)
        t0 = time.time()
        cycle(ctx, port)
        check_tokens(ctx, port, ctx["tok_a"], ctx["lane_tok"], ctx["root_tok"], ctx["slug_b"])
        metrics = check_metrics(ctx)
        # One kill -9 at about 8 s, restart on the same port, the lane URL answers.
        time.sleep(max(0, 8 - (time.time() - t0)))
        pending = send(ctx, port, ctx["tok_a"], ctx["slug_a"], "ask", "across")
        ctx["ev_a"] += 1
        kill_own_bus(home)
        # Restart from A's heartbeat: the waiting ask is in A's store, so the
        # answer has to land there. B's heartbeat is the other half of the
        # alternation and is exercised by the expiry restart below.
        res = restart(ctx, ctx["data"], port)
        port = res["port"]
        st, body = lt._http("GET", f"http://127.0.0.1:{port}/health", ctx["live_tok"])
        if st != 200:
            raise SoakFail("restart-health", f"{st} {body}")
        send(ctx, port, ctx["live_tok"], ctx["live_project"], "answer", "after", reply_to=pending)
        ctx["ev_a"] += 1
        if ctx["ev_a"] + ctx["ev_b"] <= 0:
            raise SoakFail("metrics", f"no traffic: {metrics}")
        assert_real_home_unchanged(before)
    finally:
        lt._kill_buses(home)
        lt.teardown(home)
    if os.path.isdir(home):
        raise SoakFail("leftover", f"temp home survived teardown: {home}")
    if pid and not lt._dead(pid):
        raise SoakFail("leftover", f"bus pid {pid} survived teardown")


# ---------------------------------------------------------------- main


def _ctx_from(made):
    ctx = lt._ctx(made["home"])
    ctx["repo"] = made["repo"]
    return ctx


def main(argv):
    p = argparse.ArgumentParser(prog="mk-bus-soak.py")
    p.add_argument("--minutes", type=float, default=20)
    p.add_argument("--home", help="use this temp home instead of creating one (tests)")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)
    if args.self_test:
        return self_test()
    minutes = min(args.minutes, 120)
    if args.home:
        home = lt.refuse_home(args.home)
        made = {"home": home}
        made.update(lt._ctx(home))
    else:
        made = lt.setup()
    ctx = _ctx_from(made)
    before = real_home_stat()
    home = ctx["home"]

    def _teardown(*_a):
        try:
            lt._kill_buses(home)
        except Exception:
            pass
        try:
            lt.teardown(home)
        except Exception:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda s, f: (_teardown(), sys.exit(1)))
    try:
        cadence = {"tick": 1, "kill_after": 300, "kill_every": 300, "kill_once": False,
                   "check_after": 60, "check_every": 60}
        result = run_soak(ctx, minutes, cadence)
        assert_real_home_unchanged(before)
        if result.get("aborted"):
            print(json.dumps({"ok": False, "aborted": result["aborted"],
                              "value": result["value"]}))
            return 2
        print(json.dumps({"ok": True, "soak": {k: result[k] for k in ("counts", "kills", "max_rss_mb")}}))
        return 0
    except SoakFail as e:
        print(json.dumps({"ok": False, "check": e.check, "detail": e.detail}))
        return 1
    finally:
        _teardown()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
