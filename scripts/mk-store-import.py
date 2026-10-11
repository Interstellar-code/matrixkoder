#!/usr/bin/env python3
"""Import a legacy MatrixKoder data dir into the project store. Python 3 stdlib only.

Reads <data>/runs read-only and writes only through mkdb.import_history (original timestamps,
one transaction, idempotent on a per-event and per-artifact source key) plus mkdb.catch_up
(root cards). Never opens SQLite itself and never modifies a source file.

  run --project SLUG --data DIR --run ID [--home DIR]
      Import one run: run.json (run row + task tree), events.jsonl (chan=state, original t),
      and every file under tasks/<T>/ as an artifact (task, round, kind). Then catch_up so the
      root cards exist. Importing the same run twice adds nothing.
  run --project SLUG --data DIR --all [--home DIR]
      Import every run under <data>/runs and print a parity report: source counts vs store
      counts for runs, tasks, events and artifacts.
  --self-test
      Synthetic legacy run in a temp folder, plus the real mk-bus-r1 tree when it is present.
      Refuses the real ~/.matrixkoder unless --allow-real-home is passed (the self-test never
      passes it). --project is checked with mkdb.check_slug before any path is built.

Every call prints ONE JSON line. Errors: {"ok": false, "error": ...} exit 2.
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mkdb  # the only writer; this script never opens sqlite itself

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_BUS = "/Users/rohits/Development/matrixkoder/.matrixkoder"
STATE_KINDS = ("transition", "note", "verdict", "decision", "quota")
KIND_RE = re.compile(r"^(brief|report|review|verify|review-brief)-r(\d+)\.[A-Za-z0-9]+$")


class Fail(Exception):
    def __init__(self, msg, code=2):
        super().__init__(msg)
        self.code = code


class Parser(argparse.ArgumentParser):
    def __init__(self, *a, **kw):
        kw.setdefault("allow_abbrev", False)
        super().__init__(*a, **kw)

    def error(self, message):
        raise Fail(message)


def real_home():
    return os.path.realpath(os.path.expanduser("~/.matrixkoder"))


def resolve_import_home(arg, allow_real):
    """Temp home by default. The real ~/.matrixkoder is refused unless explicitly allowed."""
    home = os.path.realpath(os.path.abspath(os.path.expanduser(
        arg or os.environ.get("MK_HOME") or "~/.matrixkoder")))
    if home == real_home() and not allow_real:
        raise Fail("refusing the real ~/.matrixkoder; pass --allow-real-home to override")
    mkdb.check_home(home)
    return home


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_checksums(root):
    """sha256 of every file under root, keyed by relative path. Read-only."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            path = os.path.join(dirpath, name)
            out[os.path.relpath(path, root)] = file_sha(path)
    return out


def _csv(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return str(value)


def _route(task):
    # check_route wants exactly the three keys, or nothing.
    if not any(task.get(k) for k in ("agent", "model", "effort")):
        return None
    return json.dumps({k: task.get(k) for k in ("agent", "model", "effort")})


def _task_type(task, by_id):
    if task.get("type") in mkdb.TYPES:
        return task["type"]
    parent = task.get("parent")
    if parent and parent in by_id and not by_id[parent].get("parent"):
        return "task"
    if parent:
        return "subtask"
    return "phase"


def load_run(data, run_id):
    """Read one legacy run. Nothing here writes."""
    run_dir = os.path.join(data, "runs", run_id)
    run_path = os.path.join(run_dir, "run.json")
    if not os.path.isfile(run_path):
        raise Fail(f"no run.json at {run_path}")
    with open(run_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    tasks = doc.get("tasks") or []
    by_id = {t["id"]: t for t in tasks}
    run_row = {
        "id": run_id,
        "plan": doc.get("plan_id") or (doc.get("plan") if isinstance(doc.get("plan"), str) else None),
        "plan_version": doc.get("plan_version"),
        "branch": doc.get("branch"),
        "status": doc.get("status") or "active",
        "stage": None if doc.get("stage") is None else str(doc["stage"]),
        "next": doc.get("next") if isinstance(doc.get("next"), str) else None,
        "started": doc.get("started"),
        "ended": doc.get("ended"),
    }
    task_rows = []
    for t in tasks:
        task_rows.append({
            "id": t["id"],
            "parent": t.get("parent") or None,
            "type": _task_type(t, by_id),
            "title": t.get("title") or "",
            "acceptance": t.get("acceptance") or "",
            "owned": _csv(t.get("owned")),
            "deps": _csv([d for d in (t.get("deps") or []) if isinstance(d, str) and ":" not in d and d in by_id]),
            "parallel": 1 if t.get("parallel") else 0,
            "route_json": _route(t),
            "status": t.get("status") or "planned",
            "lane": t.get("lane"),
            "slot": None if t.get("slot") is None else str(t["slot"]),
            "branch": t.get("branch"),
            "pane": t.get("pane"),
            "round": t.get("round") or 0,
            "verdict": t.get("verdict"),
            "updated": t.get("updated") or doc.get("updated"),
        })
    events, skipped = [], 0
    ev_path = os.path.join(run_dir, "events.jsonl")
    if os.path.isfile(ev_path):
        with open(ev_path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    skipped += 1
                    continue
                kind = ev.get("type") or ev.get("kind")
                if kind not in STATE_KINDS:
                    skipped += 1
                    continue
                events.append({
                    "src": f"{run_id}:events.jsonl:{n}",
                    "task": ev.get("task"),
                    "t": ev.get("t"),
                    "chan": "state",
                    "kind": kind,
                    "from_id": ev.get("from"),
                    "to_id": ev.get("to"),
                    "text": ev.get("text") or "",
                    "meta_json": json.dumps({"by": ev.get("by"), "line": n}),
                })
    artifacts = []
    tasks_dir = os.path.join(run_dir, "tasks")
    if os.path.isdir(tasks_dir):
        for dirpath, dirs, files in os.walk(tasks_dir):
            dirs.sort()
            rel_dir = os.path.relpath(dirpath, tasks_dir)
            task_id = None if rel_dir == "." else rel_dir.split(os.sep)[0]
            if not task_id or task_id not in by_id:
                continue
            for name in sorted(files):
                m = KIND_RE.match(name)
                if not m:
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", newline="") as fh:
                    text = fh.read()
                rel = os.path.relpath(path, run_dir)
                artifacts.append({
                    "src": f"{run_id}:{rel}",
                    "task": task_id,
                    "round": int(m.group(2)),
                    "kind": m.group(1),
                    "rev": 1,
                    "text": text,
                    "created": doc.get("updated") or doc.get("started"),
                })
    return run_row, task_rows, events, artifacts, skipped


def source_counts(data):
    """Count what the importer will read, without writing."""
    runs_dir = os.path.join(data, "runs")
    counts = {"runs": 0, "tasks": 0, "events": 0, "artifacts": 0}
    if not os.path.isdir(runs_dir):
        return counts
    for name in sorted(os.listdir(runs_dir)):
        if not os.path.isfile(os.path.join(runs_dir, name, "run.json")):
            continue
        _, tasks, events, artifacts, _ = load_run(data, name)
        counts["runs"] += 1
        counts["tasks"] += len(tasks)
        counts["events"] += len(events)
        counts["artifacts"] += len(artifacts)
    return counts


def store_counts(home, project):
    con = mkdb.open_project(home, project)
    try:
        return {
            "runs": con.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"],
            "tasks": con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"],
            "events": con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"],
            "artifacts": con.execute("SELECT COUNT(*) c FROM artifacts").fetchone()["c"],
        }
    finally:
        con.close()


def import_one(home, project, data, run_id):
    """Slug is checked before any store path is built (C11)."""
    mkdb.check_slug(project)
    run_dir = os.path.join(data, "runs", run_id)
    before = tree_checksums(run_dir)
    run_row, tasks, events, artifacts, skipped = load_run(data, run_id)
    # The project store lives under the temp home, never inside the legacy data dir.
    added = mkdb.import_history(home, project, run_id, run_row=run_row, tasks=tasks,
                                events=events, artifacts=artifacts)
    cards = mkdb.catch_up(home, project)
    if tree_checksums(run_dir) != before:
        raise Fail("source files changed during import")
    return {"added": added, "cards": cards, "skipped_events": skipped,
            "source": {"tasks": len(tasks), "events": len(events), "artifacts": len(artifacts)}}


def import_all(home, project, data):
    mkdb.check_slug(project)
    runs_dir = os.path.join(data, "runs")
    if not os.path.isdir(runs_dir):
        raise Fail(f"no runs dir at {runs_dir}")
    before = tree_checksums(runs_dir)
    imported = []
    for name in sorted(os.listdir(runs_dir)):
        if not os.path.isfile(os.path.join(runs_dir, name, "run.json")):
            continue
        imported.append(name)
        run_row, tasks, events, artifacts, _ = load_run(data, name)
        mkdb.import_history(home, project, name, run_row=run_row, tasks=tasks,
                            events=events, artifacts=artifacts)
    cards = mkdb.catch_up(home, project)
    if tree_checksums(runs_dir) != before:
        raise Fail("source files changed during import")
    return {"runs": imported, "cards": cards,
            "source": source_counts(data), "store": store_counts(home, project)}


def _check(cond, msg):
    if not cond:
        raise SystemExit("self-test failed: " + str(msg))


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _synthetic(root):
    """A legacy data dir: parents, deps, every event kind, unicode, a file with no trailing newline."""
    run = "synth-r1"
    run_dir = os.path.join(root, "runs", run)
    doc = {
        "id": run, "feature": "synthetic", "branch": "mk/synth", "status": "done",
        "stage": 3, "plan_id": "synth", "started": "2020-01-01T00:00:00+00:00",
        "ended": "2020-01-02T00:00:00+00:00", "updated": "2020-01-02T00:00:00+00:00",
        "tasks": [
            {"id": "P", "title": "phase", "status": "done", "parent": None, "deps": [], "round": 1},
            {"id": "P.1", "title": "child cafe", "status": "merged", "parent": "P",
             "deps": ["P.2"], "round": 2, "verdict": "APPROVE", "agent": "opencode"},
            {"id": "P.2", "title": "later", "status": "blocked", "parent": "P", "deps": [], "round": 0},
        ],
    }
    _write(os.path.join(run_dir, "run.json"), json.dumps(doc))
    lines = [
        {"t": "2020-01-01T00:00:01+00:00", "run": run, "task": "P.1", "type": "transition",
         "text": "started", "from": "planned", "to": "building", "by": "orchestrator"},
        {"t": "2020-01-01T00:00:02+00:00", "run": run, "task": "P.1", "type": "note",
         "text": "a note \u2014 caf\u00e9", "from": None, "to": None, "by": "builder"},
        {"t": "2020-01-01T00:00:03+00:00", "run": run, "task": "P.1", "type": "verdict",
         "text": "APPROVE", "by": "reviewer"},
        {"t": "2020-01-01T00:00:04+00:00", "run": run, "task": "P.1", "type": "decision",
         "text": "forced by orchestrator: task set", "by": "orchestrator"},
        {"t": "2020-01-01T00:00:05+00:00", "run": run, "task": "P.1", "type": "quota",
         "text": "rate limited", "by": "lane-watch"},
        {"t": "2020-01-01T00:00:06+00:00", "run": run, "task": None, "type": "note",
         "text": "run level", "by": "orchestrator"},
    ]
    body = "".join(json.dumps(ln) + "\n" for ln in lines) + "not json at all\n"
    _write(os.path.join(run_dir, "events.jsonl"), body)
    _write(os.path.join(run_dir, "tasks", "P.1", "brief-r2.md"), "brief caf\u00e9\n")
    _write(os.path.join(run_dir, "tasks", "P.1", "report-r2.md"), "report \u2014 no newline")
    _write(os.path.join(run_dir, "tasks", "P.1", "review-r2.md"), "review\n")
    _write(os.path.join(run_dir, "tasks", "P", "task.json"), "{}")  # not an artifact
    other = {"id": "synth-r2", "status": "done", "branch": "b", "started": "2020-02-01T00:00:00+00:00",
             "tasks": [{"id": "Q", "title": "only", "status": "done", "parent": None, "deps": []}]}
    _write(os.path.join(root, "runs", "synth-r2", "run.json"), json.dumps(other))
    _write(os.path.join(root, "runs", "synth-r2", "events.jsonl"),
           json.dumps({"t": "2020-02-01T00:00:01+00:00", "run": "synth-r2", "task": "Q",
                       "type": "note", "text": "hi", "by": "orchestrator"}) + "\n")
    return run


def _run_cli(args):
    cmd = [sys.executable, os.path.join(HERE, "mk-store-import.py")] + args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr


def self_test():
    tmp = tempfile.mkdtemp(prefix="mk-store-import-")
    try:
        data = os.path.join(tmp, "legacy")
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        run = _synthetic(data)
        before = tree_checksums(os.path.join(data, "runs"))

        code, out, err = _run_cli(["run", "--project", "../Bad", "--data", data, "--run", run, "--home", home])
        _check(code == 2 and "invalid slug" in out, f"bad slug: {code} {out} {err}")
        _check(not os.path.exists(os.path.join(home, "projects")), "slug refusal created a path")

        code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", data, "--run", run,
                                   "--home", real_home()])
        _check(code == 2 and "real" in out, f"real home: {code} {out} {err}")

        code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", data, "--run", run, "--home", home])
        _check(code == 0, f"import failed: {code} {out} {err}")
        first = json.loads(out)
        _check(first["ok"] is True, first)
        _check(first["added"] == {"runs": 1, "tasks": 3, "events": 6, "artifacts": 3}, first["added"])

        tl = mkdb.timeline(home, "matrixkoder", run, "P.1")
        _check([e["kind"] for e in tl] == ["transition", "note", "verdict", "decision", "quota"],
               [e["kind"] for e in tl])
        _check([e["t"] for e in tl] == [
            "2020-01-01T00:00:01+00:00", "2020-01-01T00:00:02+00:00", "2020-01-01T00:00:03+00:00",
            "2020-01-01T00:00:04+00:00", "2020-01-01T00:00:05+00:00"], "order or timestamps")
        _check(all(e["chan"] == "state" for e in tl), "chan")

        con = mkdb.open_project(home, "matrixkoder")
        try:
            rows = {r["src"]: r["text"] for r in con.execute("SELECT src, text FROM artifacts")}
            brief = rows[f"{run}:tasks/P.1/brief-r2.md"]
            report = rows[f"{run}:tasks/P.1/report-r2.md"]
            _check(brief == "brief caf\u00e9\n", "brief bytes")
            _check(hashlib.sha256(brief.encode()).hexdigest()
                   == hashlib.sha256("brief caf\u00e9\n".encode()).hexdigest(), "brief sha")
            _check(report == "report \u2014 no newline" and not report.endswith("\n"), "no-newline file")
            row = con.execute("SELECT parent, deps FROM tasks WHERE id='P.1'").fetchone()
            _check(row["parent"] == "P" and row["deps"] == "P.2", dict(row))
        finally:
            con.close()

        ids = sorted(c["task"] for c in mkdb.cards(home, "matrixkoder"))
        _check(ids == ["P", "P.1", "P.2"], f"cards {ids}")

        code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", data, "--run", run, "--home", home])
        _check(code == 0, f"second import: {out} {err}")
        second = json.loads(out)
        _check(second["added"] == {"runs": 0, "tasks": 0, "events": 0, "artifacts": 0}, second["added"])
        _check(tree_checksums(os.path.join(data, "runs")) == before, "source checksums changed")

        code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", data, "--all", "--home", home])
        _check(code == 0, f"--all: {code} {out} {err}")
        parity = json.loads(out)
        _check(parity["source"] == parity["store"], f"parity {parity}")
        _check(parity["source"]["runs"] == 2 and parity["source"]["events"] == 7, parity["source"])
        _check(tree_checksums(os.path.join(data, "runs")) == before, "source changed by --all")

        _real_bus(tmp)
        print("mk-store-import self-test ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _real_bus(tmp):
    """Read-only import of the real mk-bus-r1 tree. Skipped when the folder is absent."""
    data = REAL_BUS
    run_dir = os.path.join(data, "runs", "mk-bus-r1")
    if not os.path.isdir(run_dir):
        print("mk-bus-r1 absent, skipped")
        return
    home = os.path.join(tmp, "bus-home")
    os.makedirs(home)
    before = tree_checksums(run_dir)
    code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", data,
                               "--run", "mk-bus-r1", "--home", home])
    _check(code == 0, f"mk-bus-r1 import: {code} {out} {err}")
    _check(tree_checksums(run_dir) == before, "mk-bus-r1 source changed")
    want = []
    with open(os.path.join(run_dir, "events.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            raw = line.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if ev.get("task") == "BA" and (ev.get("type") in STATE_KINDS):
                want.append(ev)
    got = mkdb.timeline(home, "matrixkoder", "mk-bus-r1", "BA")
    _check(len(got) == len(want), f"BA count {len(got)} != {len(want)}")
    _check([e["t"] for e in got] == [e["t"] for e in want], "BA order")
    _check([e["kind"] for e in got] == [e["type"] for e in want], "BA kinds")
    _check([e["text"] for e in got] == [(e.get("text") or "") for e in want], "BA text")
    con = mkdb.open_project(home, "matrixkoder")
    try:
        n = 0
        for row in con.execute("SELECT src, text FROM artifacts WHERE run=? AND task=?", ("mk-bus-r1", "BA")):
            path = os.path.join(run_dir, row["src"].split(":", 1)[1])
            with open(path, encoding="utf-8", newline="") as fh:
                disk = fh.read()
            _check(row["text"] == disk, f"artifact bytes {row['src']}")
            n += 1
        _check(n > 0, "no BA artifacts")
    finally:
        con.close()
    cards = mkdb.cards(home, "matrixkoder")
    _check(any(c["task"] == "BA" and c["run"] == "mk-bus-r1" for c in cards), "BA card missing")
    print(f"mk-bus-r1 ok: BA events {len(got)}")


def main(argv):
    # Common options are accepted before or after the subcommand, same as mkdb.py.
    common = Parser(add_help=False)
    common.add_argument("--home")
    common.add_argument("--allow-real-home", action="store_true")
    known, rest = common.parse_known_args(argv)
    p = Parser(prog="mk-store-import.py", description=__doc__,
               formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    run = sub.add_parser("run")
    run.add_argument("--project", required=True)
    run.add_argument("--data", required=True)
    which = run.add_mutually_exclusive_group(required=True)
    which.add_argument("--run")
    which.add_argument("--all", action="store_true")
    args = p.parse_args(rest)
    args.home = known.home
    args.allow_real_home = known.allow_real_home
    # Slug first, before the home or any store path is built.
    mkdb.check_slug(args.project)
    home = resolve_import_home(args.home, args.allow_real_home)
    if args.all:
        return import_all(home, args.project, os.path.abspath(args.data))
    return import_one(home, args.project, os.path.abspath(args.data), args.run)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--self-test"]:
            self_test()
        else:
            print(json.dumps({"ok": True, **main(sys.argv[1:])}))
    except Fail as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(e.code)
    except mkdb.Fail as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(getattr(e, "code", 2))
