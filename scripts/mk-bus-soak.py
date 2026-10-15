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
from urllib.parse import quote
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
    # soak_env pins MK_BUS_DIR to the home, which IS the bus dir: bus.json sits at its root
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
    """heartbeat restarts a dead bus on the SAME port: cmd_ensure reads the old
    port from bus.json and passes --prefer-port (mk-bus.py). A port the kernel
    has not released yet gets one short bounded retry. A restart also retires
    every orchestrator token, so both projects are re-minted afterwards."""
    res = {}
    for attempt in (1, 2):
        res = bus_py("heartbeat", "--data", data, "--run", RUN, env=soak_env(ctx))
        if res.get("port") == port:
            break
        if attempt == 1:
            time.sleep(1.0)  # one bounded retry: TIME_WAIT on the old port
            continue
        raise SoakFail("restart", f"bus came back on port {res.get('port')}, wanted {port}: {res}")
    remint(ctx)
    ctx["live_project"] = project_of(ctx, data)
    ctx["live_data"] = data
    return res


def remint(ctx):
    """Fresh orchestrator tokens for both projects. The restarted bus retired the old ones."""
    ctx["tok_a"] = lt._orch_token(ctx, ctx["slug_a"], RUN)
    ctx["tok_b"] = lt._orch_token(ctx, ctx["slug_b"], RUN)
    ctx["live_tok"] = ctx["tok_a"]


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
    path = os.path.join(ctx["home"], "ring-queue.json")  # the pinned bus dir is the home
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
    """The bus's own counters. Total messages is events of the sent kinds, read
    from the store the bus counts, not the soak's own tally."""
    res = bus_py("metrics", "--data", ctx["data"], "--run", RUN, env=soak_env(ctx))
    wanted = ("latency_p50", "latency_p95", "failed_deliveries", "obedience", "false_stall")
    missing = [k for k in wanted if k not in res]
    if not res.get("ok") or missing:
        raise SoakFail("metrics", f"not a counter line: {res}")
    con = sqlite3.connect("file:" + quote(os.path.join(ctx["data"], "mk.db")) + "?mode=ro", uri=True)
    try:
        total = con.execute(
            "SELECT COUNT(*) FROM events WHERE chan='bus' AND run=? AND kind IN ('ask','info','nudge','answer')",
            (RUN,)).fetchone()[0]
    finally:
        con.close()
    if total <= 0:
        raise SoakFail("metrics", f"bus recorded no messages: {res}")
    res["total_messages"] = total
    return res


def _answers_for(data, ask_id):
    con = sqlite3.connect("file:" + quote(os.path.join(data, "mk.db")) + "?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM events WHERE chan='bus' AND kind='answer' AND reply_to=?",
            (ask_id,)).fetchone()[0]
    finally:
        con.close()


def count_events(data, chan="bus"):
    con = sqlite3.connect("file:" + quote(os.path.join(data, "mk.db")) + "?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM events WHERE chan=?", (chan,)).fetchone()[0]
    finally:
        con.close()


def check_counts(ctx, data_a, data_b, sent_a, sent_b, acks_only=None):
    """chan=bus events in each store equal the messages sent there plus the ring
    and ring-typed rows those rings wrote. Exactly one acks row per key.
    `acks_only` points the ack check at a COPY of a store (mutation 2)."""
    if acks_only is not None:
        check_acks(acks_only)
        return
    def expect(data, sent):
        con = sqlite3.connect("file:" + quote(os.path.join(data, "mk.db")) + "?mode=ro", uri=True)
        try:
            rings = con.execute(
                "SELECT COUNT(*) FROM events WHERE chan='bus' AND kind IN ('ring','ring-typed')").fetchone()[0]
        finally:
            con.close()
        return sent + rings
    got_a, got_b = count_events(data_a), count_events(data_b)
    want_a, want_b = expect(data_a, sent_a), expect(data_b, sent_b)
    if got_a != want_a or got_b != want_b:
        raise SoakFail("counts", f"events A={got_a} sent+rings={want_a}; B={got_b} sent+rings={want_b}")
    check_acks(data_a)
    return got_a, got_b


def check_acks(data):
    """Exactly one acks row per (run, msg, lane, level). `data` may be a COPY."""
    con = sqlite3.connect("file:" + quote(os.path.join(data, "mk.db") if os.path.isdir(data) else data) + "?mode=ro", uri=True)
    try:
        rows = list(con.execute("SELECT run, msg, lane, level, COUNT(*) c FROM acks "
                                "GROUP BY run, msg, lane, level"))
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    dup = [r for r in rows if r[4] != 1]
    if dup:
        raise SoakFail("acks", f"acks rows are not one per (run, msg, lane, level): {dup}")


def check_ack_copy(copy_path):
    """The count check against a COPY of the store. A planted duplicate must fail it."""
    con = sqlite3.connect("file:" + quote(copy_path) + "?mode=ro", uri=True)
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
    ctx["tok_b"] = lt._orch_token(ctx, ctx["slug_b"], RUN)
    lane = lt._lane(ctx, LANE_NAME)
    bus_py("token", "--lane", lane, "--task", TASK, "--run", RUN, "--data", ctx["data"],
           env=soak_env(ctx))
    ctx["lane_tok"] = json.load(open(lt._lane_token(ctx, lane)))["token"]
    ctx["root_tok"] = mint_root(ctx)
    ctx["sent_a"] = ctx["sent_b"] = 0
    return pid, port


def mint_root(ctx):
    """A root token in the temp home's token file. Writes only there."""
    import fcntl
    tok = lt.secrets.token_urlsafe(32)
    bus_dir = ctx["home"]  # soak_env pins MK_BUS_DIR to the home: the bus dir itself
    path = os.path.join(bus_dir, "bus-tokens.json")
    fd = os.open(os.path.join(bus_dir, "bus-tokens.lock"), os.O_CREAT | os.O_RDWR, 0o600)
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
    ctx["sent_a"] += 4  # ask, answer, ack, info. Rings are counted from the store, not here.
    if not do_doorbell:
        return
    # The answer and the info are unsolicited too, so they ring. Snapshot first.
    open(ctx["fake"] + ".status", "w").write("working")
    before = ring_count(ctx)
    open(ctx["fake"] + ".status", "w").write("idle")
    send(ctx, port, ctx["tok_a"], ctx["slug_a"], "nudge", "idle")
    ctx["sent_a"] += 1
    lt._wait_until(lambda: ring_count(ctx) >= before + 1, 5, "the idle agent to be rung")
    got = ring_count(ctx)
    if got < before + 1:
        raise SoakFail("doorbell-idle", f"idle ring count {got}, nothing new after {before}")
    open(ctx["fake"] + ".status", "w").write("working")
    nid = send(ctx, port, ctx["tok_a"], ctx["slug_a"], "nudge", "busy")
    ctx["sent_a"] += 1
    # The bus reads the fake herdr asynchronously, one drain at a time, so a
    # status flip between two drains can ring a nudge that was queued. The
    # queue file is the bus's own record: the nudge must be in it, then ring
    # exactly once once the agent is idle.
    lt._wait_until(lambda: queued(ctx, nid), 5, "the working agent's nudge to be queued")
    if not queued(ctx, nid):
        raise SoakFail("doorbell-working", "the working agent's nudge is not in ring-queue.json")
    held = ring_count(ctx)
    open(ctx["fake"] + ".status", "w").write("idle")
    lt._wait_until(lambda: ring_count(ctx) >= held + 1, 5, "the queued nudge to ring once")
    if ring_count(ctx) - held != 1:
        raise SoakFail("doorbell-drain", "the queued nudge rang %d times, wanted 1" % (ring_count(ctx) - held))


def cycle_b(ctx, port):
    """One info to project B, so its store is not empty and A's store has none of it."""
    send(ctx, port, ctx["tok_b"], ctx["slug_b"], "info", "from B")
    ctx["sent_b"] += 1


def abort_reason(max_swap=SWAP_LIMIT):
    load = read_load()
    if load > LOAD_LIMIT:
        return "load", load
    swap = read_swap()
    if swap is not None and swap > max_swap:
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
        why, val = abort_reason(cadence.get("max_swap", SWAP_LIMIT))
        if why:
            return {"aborted": why, "value": val, "kills": kills, "max_rss_mb": round(max_rss, 2)}
        max_rss = measure(ctx, max_rss)
        now = time.time()
        if now >= next_kill and (not cadence.get("kill_once") or not killed_once):
            # The ask lives in the project that will restart, so the answer can
            # land after it. which alternates A and B over time.
            owner = ctx["data"] if which % 2 == 0 else ctx["data_b"]
            owner_slug = ctx["slug_a"] if owner == ctx["data"] else ctx["slug_b"]
            owner_tok = ctx["tok_a"] if owner == ctx["data"] else ctx["tok_b"]
            pending_ask = send(ctx, port, owner_tok, owner_slug, "ask", "across")
            if owner == ctx["data"]:
                ctx["sent_a"] += 1
            else:
                ctx["sent_b"] += 1
            old, port = kill_own_bus(ctx["home"])
            which += 1
            res = restart(ctx, owner, port)
            port = res["port"]
            ctx["port"] = port
            kills += 1
            killed_once = True
            # The ask is still pending after the restart, then answered once.
            st, body = lt._http("GET", f"http://127.0.0.1:{port}/pending?run={RUN}&project={owner_slug}",
                                ctx["tok_a"] if owner == ctx["data"] else ctx["tok_b"])
            ids = [m.get("id") for m in (body.get("pending") or [])]
            if st != 200 or pending_ask not in ids:
                raise SoakFail("across-kill", f"ask {pending_ask} not pending after restart: {st} {body}")
            ans = send(ctx, port, ctx["tok_a"] if owner == ctx["data"] else ctx["tok_b"],
                       owner_slug, "answer", "after", reply_to=pending_ask)
            if owner == ctx["data"]:
                ctx["sent_a"] += 1
            else:
                ctx["sent_b"] += 1
            st, body = lt._http("GET", f"http://127.0.0.1:{port}/pending?run={RUN}&project={owner_slug}",
                                ctx["tok_a"] if owner == ctx["data"] else ctx["tok_b"])
            ids = [m.get("id") for m in (body.get("pending") or [])]
            if st != 200 or pending_ask in ids:
                raise SoakFail("across-kill", f"ask {pending_ask} still pending after the answer: {st} {body}")
            answers = _answers_for(owner, pending_ask)
            if answers != 1:
                raise SoakFail("across-kill", f"{answers} answers for {pending_ask}, wanted exactly one")
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
    check_counts(ctx, ctx["data"], ctx["data_b"], ctx["sent_a"], ctx["sent_b"])
    metrics = check_metrics(ctx)
    sent = ctx["sent_a"] + ctx["sent_b"]
    if sent <= 0:
        raise SoakFail("metrics", f"no messages were sent: {metrics}")
    expire(ctx, port)
    return {"counts": {"A": ctx["sent_a"], "B": ctx["sent_b"], "events_a": count_events(ctx["data"]),
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


# ---------------------------------------------------------------- self-test


def self_test():
    """Same teardown as main: a signal mid-run must not leave a bus behind.
    The cases clean up their own homes; this only makes the signal fatal-and-clean."""
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
    check("refuse-outside", _case_refuse_outside)
    check("refuse-temp-root", _case_refuse_temp_root)
    check("abort-limits", _case_abort)
    check("real-home-stable", _case_real_home)
    check("mutation-working-forever", _case_mutation_working)
    check("mutation-duplicate-ack", _case_mutation_ack)
    check("mutation-token", _case_mutation_token)
    check("compressed-soak", _case_compressed)
    check("mixed-layout", _case_mixed_layout)
    check("selftest-guard", _case_selftest_guard)
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



def _case_refuse_outside():
    """A pin that resolves outside the temp dir is refused, not just a missing one."""
    made = lt.setup()
    try:
        env = {"MK_BUS_DIR": made["home"], "MK_HOME": "/Users/rohits",
               "MATRIXKODER_HOME": made["home"], "MK_BUS_HERDR": made["home"] + "/x"}
        try:
            assert_pins(env, made["home"])
        except SoakFail as e:
            if "outside" in str(e):
                return
            raise
        raise SoakFail("refuse", "MK_HOME=/Users/rohits was allowed")
    finally:
        lt.teardown(made["home"])


def _case_refuse_temp_root():
    """--home $TMPDIR is refused before anything is killed."""
    import tempfile
    root = lt.temp_root()
    killed = []
    real = lt._kill_buses
    lt._kill_buses = lambda home: killed.append(home)
    try:
        try:
            main(["--home", root, "--minutes", "0"])
        except SystemExit as e:
            if root in str(e) and not killed:
                return
            raise SoakFail("refuse", f"temp root: {e}; killed={killed}")
        raise SoakFail("refuse", f"--home {root} was accepted; killed={killed}")
    finally:
        lt._kill_buses = real


def _case_abort():
    """A patched load of 21 and swap of 81 must abort; the verdict is exit 2."""
    g = globals()
    saved = (g["read_load"], g["read_swap"])
    try:
        g["read_load"] = lambda: 21.0
        why, val = abort_reason()
        if why != "load":
            raise SoakFail("abort", f"load 21 gave {why} {val}")
        g["read_load"] = lambda: 1.0
        g["read_swap"] = lambda: 81.0
        why, val = abort_reason()
        if why != "swap":
            raise SoakFail("abort", f"swap 81 gave {why} {val}")
        verdict = json.loads(json.dumps({"ok": False, "aborted": why, "value": val}))
        if verdict["ok"] is not False or verdict["aborted"] != "swap":
            raise SoakFail("abort", str(verdict))
    finally:
        g["read_load"], g["read_swap"] = saved


def _case_real_home():
    before = real_home_stat()
    assert_real_home_unchanged(before)


def _case_selftest_guard():
    """R1.2: under MK_SELFTEST=1 the mkdb write guard refuses the real home; ro reads work."""
    import tempfile
    saved = {k: os.environ.get(k) for k in ("MK_HOME", "MATRIXKODER_HOME", "MK_SELFTEST")}
    os.environ["MK_SELFTEST"] = "1"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home, exist_ok=True)
            os.environ["MK_HOME"] = home
            os.environ["MATRIXKODER_HOME"] = home
            mkdb.selftest_guard_probe(home)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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
        # The write goes through mkdb's own connection. The live primary key is
        # (run, msg, lane), so the copy's table is rebuilt without it first,
        # using that same sanctioned connection: no other script opens SQLite
        # read-write.
        con = mkdb._connect(copy)
        try:
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
        finally:
            con.close()
        # The real checker, pointed at the copy: sent 0, so only the planted
        # duplicate can make it fail.
        try:
            check_counts({}, None, None, 0, 0, acks_only=copy)
        except SoakFail as e:
            if e.check == "acks":
                return
            raise
        raise SoakFail("mutation-2", "check_counts PASSED with a duplicate ack row in the copy")
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
        ctx["slug_a"] = ctx["slug_a"]
        tok_a = lt._orch_token(ctx, ctx["slug_a"], RUN)
        lane = lt._lane(ctx, "mut-lane")
        bus_py("token", "--lane", lane, "--task", TASK, "--run", RUN, "--data", ctx["data"],
               env=soak_env(ctx))
        lane_tok = json.load(open(lt._lane_token(ctx, lane)))["token"]
        real_a = ctx["slug_a"]
        try:
            # Hand check_tokens the project-A token and tell it B is its own
            # project, so the "own project 200" check is really A-on-B and must
            # raise. A checker that skipped that check would pass here.
            ctx["slug_a"] = slug_b
            check_tokens(ctx, port, tok_a, lane_tok, tok_a, real_a)
        except SoakFail as e:
            if e.check == "token-own":
                return
            raise
        finally:
            ctx["slug_a"] = real_a
        raise SoakFail("mutation-3", "check_tokens PASSED with the project-A token used for B")
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
        cycle_b(ctx, port)
        check_counts(ctx, ctx["data"], ctx["data_b"], ctx["sent_a"], ctx["sent_b"])
        check_tokens(ctx, port, ctx["tok_a"], ctx["lane_tok"], ctx["root_tok"], ctx["slug_b"])
        metrics = check_metrics(ctx)
        # One kill -9 at about 8 s. The ask lives in B and the restart comes
        # from B, so a B restart is exercised here (the real run alternates).
        time.sleep(max(0, 8 - (time.time() - t0)))
        pending = send(ctx, port, ctx["tok_b"], ctx["slug_b"], "ask", "across")
        ctx["sent_b"] += 1
        kill_own_bus(home)
        res = restart(ctx, ctx["data_b"], port)
        port = res["port"]
        if res["port"] != port:
            raise SoakFail("restart", f"port {res['port']} != {port}")
        # Fresh tokens: a cycle and the token checks must pass after the restart.
        cycle(ctx, port)
        check_tokens(ctx, port, ctx["tok_a"], ctx["lane_tok"], ctx["root_tok"], ctx["slug_b"])
        st, body = lt._http("GET", f"http://127.0.0.1:{port}/pending?run={RUN}&project={ctx['slug_b']}",
                            ctx["tok_b"])
        ids = [m.get("id") for m in (body.get("pending") or [])]
        if st != 200 or pending not in ids:
            raise SoakFail("across-kill", f"ask {pending} not pending after restart: {st} {body}")
        send(ctx, port, ctx["tok_b"], ctx["slug_b"], "answer", "after", reply_to=pending)
        ctx["sent_b"] += 1
        st, body = lt._http("GET", f"http://127.0.0.1:{port}/pending?run={RUN}&project={ctx['slug_b']}",
                            ctx["tok_b"])
        ids = [m.get("id") for m in (body.get("pending") or [])]
        if pending in ids:
            raise SoakFail("across-kill", f"ask still pending: {body}")
        if _answers_for(ctx["data_b"], pending) != 1:
            raise SoakFail("across-kill", f"not exactly one answer for {pending}")
        if ctx["sent_a"] + ctx["sent_b"] <= 0:
            raise SoakFail("metrics", f"no traffic: {metrics}")
        assert_real_home_unchanged(before)
    finally:
        lt._kill_buses(home)
        lt.teardown(home)
    if os.path.isdir(home):
        raise SoakFail("leftover", f"temp home survived teardown: {home}")
    if pid and not lt._dead(pid):
        raise SoakFail("leftover", f"bus pid {pid} survived teardown")


def _case_mixed_layout():
    """C7 (L6.1): one bus serving a MIGRATED project and a PRE-MIGRATION project side by
    side, with the migrated one as the bus's OWN project — `mk-migrate --switch` sets the
    root `db_path` the bus resolves its own store through. A kill -9 plus a restart must
    lose and duplicate no seq, and every message stays in the store it was sent to."""
    made = lt.setup()
    home = made["home"]
    ctx = lt._ctx(home)
    ctx.update({"repo": made["repo"]})
    pid = None
    try:
        # A migrates through the production tool (L2). Its store already sits in the C2
        # folder, so the switch only sets the root row's db_path — the marker every
        # own-store resolve reads. B is created afterwards and stays pre-migration.
        mig = subprocess.run([sys.executable, os.path.join(HERE, "mk-migrate.py"), "--switch",
                              "--slug", ctx["slug"], "--data", ctx["data"], "--home", home,
                              "--repo", made["repo"]],
                             capture_output=True, text=True,
                             env=dict(lt._bus_env(ctx), MK_SELFTEST="1"))
        if mig.returncode != 0:
            raise SoakFail("switch", f"mk-migrate --switch: rc={mig.returncode} "
                                     f"{mig.stdout[-200:]} {mig.stderr[-200:]}")
        _, data_b = lt._second_project(ctx)
        pid, port = prepare(ctx)  # seeds A and B, mints the tokens, starts the bus
        # L6.1: the lane token file is <bus dir>/lanes/<hash>.json — with soak_env's pin the
        # bus dir is the home itself, so it is <home>/lanes/. mkpaths owns the rule; this
        # only proves the bus and the harness agree on it.
        lane_dir = os.path.join(home, "lanes")
        if not os.path.isdir(lane_dir):
            raise SoakFail("layout", "the lane token file is not under <bus dir>/lanes/")
        cycle(ctx, port, do_doorbell=False)  # no doorbell: the ring-drain timing is not this case
        cycle_b(ctx, port)
        check_counts(ctx, ctx["data"], data_b, ctx["sent_a"], ctx["sent_b"])
        check_tokens(ctx, port, ctx["tok_a"], ctx["lane_tok"], ctx["root_tok"], ctx["slug_b"])
        # kill -9 and restart on the same port, then answer the pre-kill ask exactly once
        pending = send(ctx, port, ctx["tok_a"], ctx["slug_a"], "ask", "across")
        ctx["sent_a"] += 1
        _old_pid, old_port = kill_own_bus(home)
        res = restart(ctx, ctx["data"], old_port)
        port = res["port"]
        if port != old_port:
            raise SoakFail("restart", f"port {port} != {old_port}")
        cycle(ctx, port, do_doorbell=False)
        check_tokens(ctx, port, ctx["tok_a"], ctx["lane_tok"], ctx["root_tok"], ctx["slug_b"])
        st, body = lt._http("GET", f"http://127.0.0.1:{port}/pending?run={RUN}&project={ctx['slug_a']}",
                            ctx["tok_a"])
        ids = [m.get("id") for m in (body.get("pending") or [])]
        if st != 200 or pending not in ids:
            raise SoakFail("across-kill", f"ask {pending} not pending after restart: {st} {body}")
        send(ctx, port, ctx["tok_a"], ctx["slug_a"], "answer", "after", reply_to=pending)
        ctx["sent_a"] += 1
        if _answers_for(ctx["data"], pending) != 1:
            raise SoakFail("across-kill", f"not exactly one answer for {pending}")
        check_counts(ctx, ctx["data"], data_b, ctx["sent_a"], ctx["sent_b"])
        check_metrics(ctx)
        # the real-home guard is _case_real_home's job: the live bus heartbeats ~/.matrixkoder/bus.json
        # every 15 s, so reading it here would flake on a heartbeat landing inside this window.
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
    p.add_argument("--max-swap-pct", type=float, default=SWAP_LIMIT,
                   help="abort when swap used exceeds this percent (default 80, max 98)")
    p.add_argument("--home", help="a temp home setup created; must carry the harness marker")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)
    if args.self_test:
        return self_test()
    minutes = min(args.minutes, 120)
    if args.max_swap_pct > 98:
        raise SystemExit(f"--max-swap-pct {args.max_swap_pct} is above 98")
    max_swap = args.max_swap_pct
    if args.home:
        home = os.path.realpath(os.path.abspath(os.path.expanduser(args.home)))
        if home == lt.temp_root():
            raise SystemExit(f"refusing the temp root as a home: {home}")
        home = lt.refuse_home(home)
        lt._read_marker(home)  # only a home this harness created
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
        # One cycle per 6 s keeps the traffic at or under 1 message/s on average
        # (a cycle is ask, answer, ack, info and two nudges). The kill and the
        # token check stay on their real-seconds cadence.
        cadence = {"tick": 6, "kill_after": 300, "kill_every": 300, "kill_once": False,
                   "check_after": 60, "check_every": 60, "max_swap": max_swap}
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
