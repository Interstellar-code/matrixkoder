#!/usr/bin/env python3
"""snapshot-drift: keep the dashboard's store fixture in step with the real reader.

`hooks/dashboard.tsx` reads its snapshot through `mkdb.py run-snapshot` (L5, C4). The test
that pins the pane's text needs a real reply, and the plugin test runtime can neither read a
file nor spawn a process — it is handed the reply through the injected runner. A hand-kept
copy of that reply goes stale the moment the reader changes, so this script regenerates it:

  * it drives the PRODUCTION write path (`scripts/mk-state.py`) in a throwaway temp data dir,
    captures the file views while they are still the only truth, then turns the store on and
    reads the same run back with `mkdb.py run-snapshot`;
  * it writes `hooks/fixtures/run-snapshot.json` (the capture) and
    `hooks/fixtures/run-snapshot.ts` (the same object as a TS module — the plugin tsconfig
    does not enable `resolveJsonModule`, so a static JSON import does not compile);
  * `--check` re-captures and compares the WHOLE payload, so a reader change that the
    committed fixture does not show up fails the suite.

Usage:
  snapshot-drift.py --write     regenerate the committed files
  snapshot-drift.py --check     fail with a diff summary when the committed fixture drifted
  snapshot-drift.py --self-test  prove --check fails on a tampered temp copy (the committed
                                 files are never touched; an internal argv names the pair)

The real `.matrixkoder` is never touched: MK_HOME, MATRIXKODER_HOME, MATRIXKODER_DATA and
MK_BUS_DIR all point into the temp tree.
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MK_STATE = os.path.join(REPO_ROOT, "scripts", "mk-state.py")
MKDB = os.path.join(REPO_ROOT, "scripts", "mkdb.py")
OUT_JSON = os.path.join(REPO_ROOT, "hooks", "fixtures", "run-snapshot.json")
OUT_TS = os.path.join(REPO_ROOT, "hooks", "fixtures", "run-snapshot.ts")

RUN_ID = "l5fix"
# The absolute temp paths never reach the fixture: the capture is normalised below, so the
# committed payload is the same on every machine and the check compares like for like.
VOLATILE_KEYS = ("created", "updated", "started", "t", "since", "workspace")


def mk(env, *args, check=True):
    """One production mk-state.py call. Its stdout is one JSON line; nothing is printed."""
    proc = subprocess.run([sys.executable, MK_STATE, "--data", env["MATRIXKODER_DATA"], *args],
                          capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        raise SystemExit(f"mk-state.py {' '.join(args)} failed: {proc.stdout}{proc.stderr}")
    return proc


def build_run(env) -> str:
    """Drive the production path over a small, deliberately varied run. Returns the data dir.

    The shape is what the pane renders: a phase, two children under it (one gated on the
    other), two root tasks, a route and a lane on the first child, a reviewer and a round on
    the second, an attention status on the third, an unrouted fourth, and a wait."""
    data = env["MATRIXKODER_DATA"]
    mk(env, "run", "init", "--id", RUN_ID, "--feature", "L5 fixtures", "--branch", "mk/l5")
    mk(env, "task", "add", "--id", "P1", "--title", "Phase one", "--wave", "1",
        "--owned", "scripts/p1.py")
    mk(env, "task", "add", "--id", "T1", "--title", "First", "--parent", "P1", "--wave", "1",
        "--owned", "scripts/a.py")
    mk(env, "task", "add", "--id", "T2", "--title", "Second", "--parent", "P1", "--wave", "2",
        "--deps", "T1", "--owned", "scripts/b.py,scripts/c.py")
    mk(env, "task", "add", "--id", "T3", "--title", "Third", "--wave", "1")
    mk(env, "task", "add", "--id", "T4", "--title", "Fourth", "--wave", "2")
    for status in ("routed", "dispatched", "building"):
        mk(env, "task", "set", "T1", f"status={status}",
            "--note", "lane-watch", "--by", "lane-watch")
    mk(env, "task", "set", "T1", "agent=claude", "model=opus", "effort=high", "lane=mk/l5",
        "pane=w1P:pN", "branch=mk/l5", "slot=1", "--by", "lane-watch")
    mk(env, "task", "set", "T1", "last_commit=1a2b3c4", "--by", "lane-watch")
    mk(env, "task", "set", "T1", "status=merged", "--by", "lane-watch")
    mk(env, "task", "set", "T2", "agent=codex", "model=gpt", "effort=low", "reviewer=rex",
        "--by", "lead")
    mk(env, "task", "set", "T2", "status=building", "--note", "picked up", "--by", "lead")
    mk(env, "task", "set", "T3", "status=blocked", "--note", "waiting on upstream", "--by", "lead")
    mk(env, "task", "set", "T4", "status=planned", "--note", "queued", "--by", "lead")
    mk(env, "wait", "set", "--question", "Merge T1 now?", "--options", "yes,no", "--rec", "yes")
    mk(env, "event", "--type", "note", "--text", "fixture capture complete")
    mk(env, "run", "stage", "3")
    mk(env, "run", "set", "--next", "verify T1 gates")
    mk(env, "render")
    return data


def file_views(data) -> dict:
    """The retired views, captured before the store exists — the ground truth the reader must match."""
    runs = os.path.join(data, "runs")
    view = {}
    for rel, key in (("index.json", "runs"), (os.path.join(RUN_ID, "run.json"), "run")):
        path = os.path.join(runs, rel)
        with contextlib.suppress(OSError, ValueError):
            with open(path) as fh:
                view[key] = json.load(fh)
    return view


def capture() -> dict:
    """One full capture: the file views and the store reader's answer for the same run."""
    tmp = tempfile.mkdtemp(prefix="mk-snapshot-drift-")
    try:
        env = dict(os.environ)
        env.update({
            "MK_HOME": os.path.join(tmp, "home"),
            "MATRIXKODER_HOME": os.path.join(tmp, "home"),
            "MATRIXKODER_DATA": os.path.join(tmp, "data"),
            "MK_BUS_DIR": os.path.join(tmp, "bus"),
        })
        os.makedirs(env["MATRIXKODER_DATA"], exist_ok=True)
        data = build_run(env)
        views = file_views(data)
        mk(env, "store", "enable")
        proc = subprocess.run([sys.executable, MKDB, "--data", data, "run-snapshot",
                               "--run", RUN_ID, "--json"], capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            raise SystemExit(f"run-snapshot failed: {proc.stdout}{proc.stderr}")
        reply = json.loads(proc.stdout)
        return {"file_views": views, "reader": reply, "data": data}
    finally:
        # store enable snapshots runs/ into a READ-ONLY runs-legacy/, so plain rm fails
        subprocess.run(["chmod", "-R", "u+w", tmp], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def normalise(doc: dict) -> dict:
    """The payload with every wall-clock and absolute-path value pinned.

    The reader answers real timestamps and real paths; a fixture carrying them would differ
    on every capture. Pinning keeps the SHAPE and the words the pane renders — which is what
    the render-equality test compares — and drops nothing else."""
    text = json.dumps(doc, sort_keys=True, indent=2)
    doc = json.loads(text)
    for key in list(doc.keys()):
        if key in VOLATILE_KEYS and isinstance(doc[key], str):
            doc[key] = f"<{key}>"
    for entry in doc.get("runs") or []:
        if isinstance(entry, dict):
            for key in ("started", "ended"):
                if isinstance(entry.get(key), str):
                    entry[key] = f"<{key}>"
    run = doc.get("run")
    if isinstance(run, dict):
        for key in list(run.keys()):
            if key in VOLATILE_KEYS and isinstance(run[key], str):
                run[key] = f"<{key}>"
        wait = run.get("waiting")
        if isinstance(wait, dict) and isinstance(wait.get("since"), str):
            wait["since"] = "<since>"
        recent = run.get("recent")
        if isinstance(recent, list):
            for ev in recent:
                if isinstance(ev, dict) and isinstance(ev.get("t"), str):
                    ev["t"] = "<t>"
        for task in run.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            for key in list(task.keys()):
                if key in VOLATILE_KEYS and isinstance(task[key], str):
                    task[key] = f"<{key}>"
            if isinstance(task.get("rounds"), list):
                for rnd in task["rounds"]:
                    if isinstance(rnd, dict) and isinstance(rnd.get("started"), str):
                        rnd["started"] = "<started>"
    return doc


def render_json(payload) -> str:
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def render_ts(payload) -> str:
    """The same object as a TS module: the plugin tsconfig has no resolveJsonModule."""
    body = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
    return ("// Captured `mkdb.py run-snapshot` reply, generated from run-snapshot.json by\n"
            "// scripts/snapshot-drift.py. Do not hand-edit; run `snapshot-drift.py --write`.\n"
            "// The capture: a run written by scripts/mk-state.py in a temp data dir, read back\n"
            "// through the production reader. Timestamps and absolute paths are pinned.\n"
            "export const RUN_SNAPSHOT = " + body + " as const\n")


def compare(committed: str, fresh: str, label: str) -> list[str]:
    if committed == fresh:
        return []
    diff = list(difflib.unified_diff(committed.splitlines(), fresh.splitlines(),
                                     fromfile=f"committed {label}", tofile=f"captured {label}",
                                     lineterm=""))
    return diff[:40]


def cmd_check(json_path: str, ts_path: str) -> int:
    payload = normalise(capture()["reader"])
    fresh_json, fresh_ts = render_json(payload), render_ts(payload)
    problems: list[str] = []
    for path, fresh, label in ((json_path, fresh_json, os.path.basename(json_path)),
                               (ts_path, fresh_ts, os.path.basename(ts_path))):
        try:
            with open(path) as fh:
                problems += compare(fh.read(), fresh, label)
        except OSError:
            problems = [f"missing fixture: {path} — run snapshot-drift.py --write"]
    if problems:
        print("snapshot-drift: the committed fixture is stale; run snapshot-drift.py --write")
        print("\n".join(problems))
        return 1
    print("snapshot-drift: the committed fixture matches the real reader")
    return 0


def cmd_write() -> int:
    payload = normalise(capture()["reader"])
    for path, text in ((OUT_JSON, render_json(payload)), (OUT_TS, render_ts(payload))):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        print(f"wrote {path}")
    return 0


def self_test() -> int:
    """--check passes on the committed pair and fails on a tampered temp copy."""
    if not os.path.exists(OUT_TS) or not os.path.exists(OUT_JSON):
        raise SystemExit("SELF-TEST FAIL: no committed fixture; run snapshot-drift.py --write")
    if cmd_check(OUT_JSON, OUT_TS) != 0:
        raise SystemExit("SELF-TEST FAIL: --check failed against the committed fixture")
    with tempfile.TemporaryDirectory() as tmp:
        bad_ts = os.path.join(tmp, "run-snapshot.ts")
        with open(OUT_TS) as fh:
            tampered = fh.read().replace('"stage": 3', '"stage": 9', 1)
        if tampered == open(OUT_TS).read():
            tampered = fh.read().replace('"merged"', '"done"', 1)
        with open(bad_ts, "w") as fh:
            fh.write(tampered)
        rc = cmd_check(OUT_JSON, bad_ts)
        if rc == 0:
            raise SystemExit("SELF-TEST FAIL: --check accepted a tampered fixture")
    print("snapshot-drift self-test: pass")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # internal: self_test names a tampered pair instead of the committed one
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--check-json", help=argparse.SUPPRESS)
    ap.add_argument("--check-ts", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if a.write:
        return cmd_write()
    if a.check or a.check_json or a.check_ts:
        return cmd_check(a.check_json or OUT_JSON, a.check_ts or OUT_TS)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))