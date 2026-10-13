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
KIND_RE = re.compile(r"^(brief-review|review-brief|brief|report|review|verify)-r(\d+)\.[A-Za-z0-9]+$")
KIND_OF = {"brief-review": "review-brief"}  # real filename; the table stores review-brief


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


def check_run_id(run_id):
    """C11 slug rule, same as --project, before any path is built from the id."""
    mkdb.check_slug(run_id)


def run_dir_of(data, run_id):
    """The run directory, refused when it is a symlink that escapes <data>/runs."""
    check_run_id(run_id)
    runs = os.path.realpath(os.path.join(data, "runs"))
    path = os.path.join(data, "runs", run_id)
    real = os.path.realpath(path)
    if os.path.islink(path) or not (real == runs or real.startswith(runs + os.sep)):
        if os.path.islink(path) or not real.startswith(runs + os.sep):
            raise Fail(f"run {run_id!r} escapes <data>/runs")
    return path


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


def inside(root, path):
    """True when path's real location is root or a file under it. commonpath, so a sibling
    directory named <root>-evil does not pass."""
    root, path = os.path.realpath(root), os.path.realpath(path)
    return os.path.commonpath((root, path)) == root


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_checksums(root):
    """sha256 of every file under root, keyed by relative path. A symlink that escapes root is skipped."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            path = os.path.join(dirpath, name)
            if not inside(root, path):
                continue
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


def _deps_of_task(task, by_id, run_id):
    """Keep every dep. <this_run>:<id> becomes the plain id. A self-dep is named, not stored."""
    kept, unresolved = [], []
    for dep in task.get("deps") or []:
        if not isinstance(dep, str) or not dep:
            continue
        if ":" in dep:
            other, _, tid = dep.partition(":")
            if other == run_id and tid == task["id"]:
                unresolved.append({"run": run_id, "task": task["id"], "dep": dep})
            elif other == run_id and tid in by_id:
                kept.append(tid)  # <this_run>:<id> normalises to the plain id
            else:
                kept.append(dep)  # dropped later if the target task is not in the project
        else:
            kept.append(dep)
    return kept, unresolved


def load_run(data, run_id):
    """Read one legacy run. Nothing here writes. The run id is checked before the path is built."""
    run_dir = run_dir_of(data, run_id)
    run_path = os.path.join(run_dir, "run.json")
    if not os.path.isfile(run_path):
        raise Fail(f"no run.json for {run_id}")
    if not inside(run_dir, run_path):
        raise Fail(f"run.json for {run_id} escapes the run dir")
    try:
        with open(run_path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise Fail(f"bad run.json for {run_id}: {e}")
    if not isinstance(doc, dict):
        raise Fail(f"bad run.json for {run_id}: expected an object")
    tasks = doc.get("tasks") or []
    if not isinstance(tasks, list) or any(not isinstance(t, dict) or "id" not in t for t in tasks):
        raise Fail(f"bad run.json for {run_id}: tasks must be objects with id")
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
            "deps": _csv(_deps_of_task(t, by_id, run_id)[0]),
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
    events, skipped, skip_reasons = [], 0, []
    ev_path = os.path.join(run_dir, "events.jsonl")
    if os.path.isfile(ev_path):
        if not inside(run_dir, ev_path):
            raise Fail(f"events.jsonl for {run_id} escapes the run dir")
        with open(ev_path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                raw = line.strip()
                if not raw:
                    skipped += 1
                    skip_reasons.append({"line": n, "reason": "blank line"})
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    skipped += 1
                    skip_reasons.append({"line": n, "reason": "malformed json"})
                    continue
                kind = ev.get("type") or ev.get("kind")
                if kind not in STATE_KINDS:
                    skipped += 1
                    skip_reasons.append({"line": n, "reason": f"kind {kind!r}"})
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
    artifacts, skipped_artifacts = [], []
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
                rel = os.path.relpath(path, run_dir)
                if not inside(run_dir, path):
                    skipped_artifacts.append({"name": rel, "reason": "symlink escapes run dir"})
                    continue
                # import_history takes text and stores it through JSON, so a non-UTF-8 file
                # cannot round-trip. It is named in skipped_artifacts and left untouched.
                with open(path, "rb") as fh:
                    raw = fh.read()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    skipped_artifacts.append({"name": rel, "reason": "not valid UTF-8"})
                    continue
                artifacts.append({
                    "src": f"{run_id}:{rel}",
                    "task": task_id,
                    "round": int(m.group(2)),
                    "kind": KIND_OF.get(m.group(1), m.group(1)),
                    "rev": 1,
                    "text": text,
                    "created": doc.get("updated") or doc.get("started"),
                })
    unresolved = []
    for t in tasks:
        _, extra = _deps_of_task(t, by_id, run_id)
        unresolved.extend(extra)
    return run_row, task_rows, events, artifacts, skipped, unresolved, skipped_artifacts, skip_reasons


def source_counts(data):
    """Count what the importer will read, without writing."""
    runs_dir = os.path.join(data, "runs")
    counts = {"runs": 0, "tasks": 0, "events": 0, "artifacts": 0, "skipped_events": 0}
    if not os.path.isdir(runs_dir):
        return counts
    for name in sorted(os.listdir(runs_dir)):
        if not os.path.isfile(os.path.join(runs_dir, name, "run.json")):
            continue
        try:
            _, tasks, events, artifacts, skipped, _, _, _ = load_run(data, name)
        except Fail:
            continue  # a bad run is not part of the parity the store can hold
        counts["runs"] += 1
        counts["tasks"] += len(tasks)
        counts["events"] += len(events)
        counts["artifacts"] += len(artifacts)
        counts["skipped_events"] = counts.get("skipped_events", 0) + skipped
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


def _present_runs(home, project):
    con = mkdb.open_project(home, project)
    try:
        return {r["id"] for r in con.execute("SELECT id FROM runs")}
    finally:
        con.close()


def _known_tasks(home, project):
    con = mkdb.open_project(home, project)
    try:
        return {(r["run"], r["id"]) for r in con.execute("SELECT run, id FROM tasks")}
    finally:
        con.close()


def _split_deps(tasks, run_id, known):
    """Drop deps whose target is not in the project. Returns (tasks, unresolved records)."""
    out, unresolved = [], []
    for t in tasks:
        kept = []
        for dep in [d for d in (t.get("deps") or "").split(",") if d]:
            if ":" in dep:
                other, _, tid = dep.partition(":")
                target = (other, tid)
            else:
                target = (run_id, dep)
            if target in known or target == (run_id, t["id"]):
                if target == (run_id, t["id"]):
                    unresolved.append({"run": run_id, "task": t["id"], "dep": dep})
                    continue
                kept.append(dep)
            else:
                unresolved.append({"run": run_id, "task": t["id"], "dep": dep})
        row = dict(t)
        row["deps"] = ",".join(kept)
        out.append(row)
    return out, unresolved


def _dep_runs(tasks, run_id):
    """Other runs this run's tasks name in <run>:<id> deps."""
    runs = set()
    for t in tasks:
        for dep in [d for d in (t.get("deps") or "").split(",") if d]:
            if ":" in dep:
                other = dep.split(":", 1)[0]
                if other != run_id:
                    runs.add(other)
    return runs


def _import_order(loaded):
    """A run follows the runs it depends on. A cycle falls back to name order."""
    names = set(loaded)
    edges = {}
    for name, bundle in loaded.items():
        tasks = bundle[1]
        edges[name] = {r for r in _dep_runs(tasks, name) if r in names}
    order, pending = [], set(names)
    while pending:
        ready = sorted(n for n in pending if edges[n].isdisjoint(pending))
        if not ready:  # cycle or unsortable: the rest go by name
            order.extend(sorted(pending))
            break
        order.extend(ready)
        pending -= set(ready)
    return order


def import_one(home, project, data, run_id):
    """Slug and run id are checked before any store path is built (C11)."""
    mkdb.check_slug(project)
    check_run_id(run_id)
    run_dir = run_dir_of(data, run_id)
    before = tree_checksums(run_dir)
    run_row, tasks, events, artifacts, skipped, unresolved, skipped_arts, skip_reasons = load_run(data, run_id)
    known = _known_tasks(home, project) | {(run_id, t["id"]) for t in tasks}
    tasks, dropped = _split_deps(tasks, run_id, known)
    added = mkdb.import_history(home, project, run_id, run_row=run_row, tasks=tasks,
                                events=events, artifacts=artifacts)
    cards = mkdb.catch_up(home, project)
    if tree_checksums(run_dir) != before:
        raise Fail("source files changed during import")
    return {"added": added, "cards": cards, "skipped_events": skipped,
            "skipped_event_reasons": skip_reasons,
            "skipped_artifacts": skipped_arts,
            "unresolved_deps": list(unresolved) + dropped,
            "source": {"tasks": len(tasks), "events": len(events), "artifacts": len(artifacts)}}


def _run_names(data):
    runs_dir = os.path.join(data, "runs")
    names = []
    if not os.path.isdir(runs_dir):
        return names
    for name in sorted(os.listdir(runs_dir)):
        if os.path.isfile(os.path.join(runs_dir, name, "run.json")):
            names.append(name)
    return names


def import_all(home, project, data):
    """One bad run is reported and skipped; the rest still import. Exit 4 when any failed."""
    mkdb.check_slug(project)
    runs_dir = os.path.join(data, "runs")
    if not os.path.isdir(runs_dir):
        raise Fail(f"no runs dir at {runs_dir}")
    before = tree_checksums(runs_dir)
    loaded, failed = {}, []
    for name in _run_names(data):
        try:
            check_run_id(name)
            loaded[name] = load_run(data, name)  # 8-tuple
        except Exception as e:  # one bad run.json must not stop the others
            failed.append({"run": name, "error": str(e)})
    # Runs land in dependency order, so a <run>:<id> dep finds its target task.
    imported, unresolved_deps, skipped_events, skipped_artifacts, skipped_event_reasons = [], [], 0, [], []
    known = _known_tasks(home, project)
    for name in _import_order(loaded):
        run_row, tasks, events, artifacts, skipped, unresolved, skipped_arts, skip_reasons = loaded[name]
        known |= {(name, t["id"]) for t in tasks}
        tasks, dropped = _split_deps(tasks, name, known)
        try:
            mkdb.import_history(home, project, name, run_row=run_row, tasks=tasks,
                                events=events, artifacts=artifacts)
        except Exception as e:
            failed.append({"run": name, "error": str(e)})
            continue
        imported.append(name)
        skipped_events += skipped
        skipped_event_reasons.extend(skip_reasons)
        skipped_artifacts.extend(skipped_arts)
        unresolved_deps.extend(unresolved)
        unresolved_deps.extend(dropped)
    cards = mkdb.catch_up(home, project)
    if tree_checksums(runs_dir) != before:
        raise Fail("source files changed during import")
    return {"imported": imported, "failed": failed, "cards": cards,
            "skipped_events": skipped_events, "skipped_event_reasons": skipped_event_reasons,
            "skipped_artifacts": skipped_artifacts,
            "unresolved_deps": unresolved_deps,
            "source": source_counts(data), "store": store_counts(home, project),
            "exit": 0 if not failed else 4}


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
        shared = ("runs", "tasks", "events", "artifacts")
        _check({k: parity["source"][k] for k in shared} == {k: parity["store"][k] for k in shared},
               f"parity {parity}")
        _check(parity["skipped_events"] == 1, parity)
        reasons = parity.get("skipped_event_reasons") or []
        _check(any(r.get("reason") == "malformed json" for r in reasons), reasons)
        _check(parity["source"]["runs"] == 2 and parity["source"]["events"] == 7, parity["source"])
        _check(tree_checksums(os.path.join(data, "runs")) == before, "source changed by --all")

        _guards_r2(tmp)
        _blank_events(tmp)
        _symlinks(tmp)
        _real_bus(tmp)
        print("mk-store-import self-test ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)




def _blank_events(tmp):
    """A blank line in events.jsonl is counted in skipped_events with a reason; it is not imported."""
    data = os.path.join(tmp, "blank")
    home = os.path.join(tmp, "blank-home")
    os.makedirs(home)
    _write(os.path.join(data, "runs", "b1", "run.json"), json.dumps(
        {"id": "b1", "status": "done", "tasks": [
            {"id": "T", "title": "t", "status": "done", "parent": None, "deps": [], "verdict": "APPROVE"}]}))
    note = json.dumps({"t": "2020-01-01T00:00:01+00:00", "task": "T", "type": "note", "text": "kept"})
    _write(os.path.join(data, "runs", "b1", "events.jsonl"), "\n" + note + "\n   \nnot-json\n")
    code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", data, "--run", "b1", "--home", home])
    _check(code == 0, f"blank: {code} {out} {err}")
    doc = json.loads(out)
    _check(doc["skipped_events"] == 3, doc)
    reasons = doc.get("skipped_event_reasons") or []
    _check(sum(1 for r in reasons if r.get("reason") == "blank line") == 2, reasons)
    _check(any(r.get("reason") == "malformed json" for r in reasons), reasons)
    _check(doc["added"]["events"] == 1, doc["added"])
    got = [c for c in mkdb.cards(home, "matrixkoder") if c["task"] == "T"]
    _check(len(got) == 1 and got[0]["verdict"] == "APPROVE", got)


def _guards_r2(tmp):
    """Cross-run deps, hostile run ids, a bad run inside --all, non-UTF-8, brief-review names."""
    data = os.path.join(tmp, "r2")
    home = os.path.join(tmp, "r2-home")
    os.makedirs(home)
    # r1:T1 is a dep of r2:T2, and r2 also names a run that is not in the project.
    r1 = {"id": "r1", "status": "done", "tasks": [
        {"id": "T1", "title": "one", "status": "done", "parent": None, "deps": []}]}
    r2 = {"id": "r2", "status": "done", "tasks": [
        {"id": "T2", "title": "two", "status": "done", "parent": None,
         "deps": ["r1:T1", "r2:T2", "missing-run:Z"]}]}
    _write(os.path.join(data, "runs", "r1", "run.json"), json.dumps(r1))
    _write(os.path.join(data, "runs", "r2", "run.json"), json.dumps(r2))
    _write(os.path.join(data, "runs", "r2", "events.jsonl"), "this is not json\n")
    _write(os.path.join(data, "runs", "r2", "tasks", "T2", "brief-review-r1.md"), "review brief\n")
    blob = bytes([0xff, 0xfe, 0x00])
    with open(os.path.join(data, "runs", "r2", "tasks", "T2", "report-r1.md"), "wb") as fh:
        fh.write(blob)
    code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", data, "--all", "--home", home])
    _check(code == 0, f"r2 import: {code} {out} {err}")
    doc = json.loads(out)
    _check(doc["skipped_events"] == 1, doc)
    con = mkdb.open_project(home, "matrixkoder")
    try:
        deps = con.execute("SELECT deps FROM tasks WHERE run='r2' AND id='T2'").fetchone()["deps"]
        _check(deps == "r1:T1", f"deps dropped: {deps}")
        text = con.execute("SELECT text, kind FROM artifacts WHERE run='r2' AND kind='review-brief'").fetchone()
        _check(text and text["text"] == "review brief\n", f"brief-review {text and dict(text)}")
        stored = con.execute("SELECT text FROM artifacts WHERE run='r2' AND kind='report'").fetchone()
        _check(stored is None, "non-UTF-8 file was stored")
        _check(open(os.path.join(data, "runs", "r2", "tasks", "T2", "report-r1.md"), "rb").read() == blob,
               "source bytes altered")
    finally:
        con.close()
    deps = {(d["task"], d["dep"]) for d in doc["unresolved_deps"]}
    _check(("T2", "missing-run:Z") in deps and ("T2", "r2:T2") in deps, doc["unresolved_deps"])
    _check(("T2", "r1:T1") not in deps, doc["unresolved_deps"])
    _check(any(a["name"].endswith("report-r1.md") and a["reason"] == "not valid UTF-8"
               for a in doc["skipped_artifacts"]), doc["skipped_artifacts"])


    # case A: the target task is already in the store (r1 imported above), a new run depends on it
    later = os.path.join(tmp, "later")
    _write(os.path.join(later, "runs", "r3", "run.json"), json.dumps(
        {"id": "r3", "status": "done", "tasks": [
            {"id": "T3", "title": "three", "status": "done", "parent": None, "deps": ["r1:T1"]}]}))
    code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", later, "--run", "r3", "--home", home])
    _check(code == 0, f"case A: {code} {out} {err}")
    con = mkdb.open_project(home, "matrixkoder")
    try:
        deps = con.execute("SELECT deps FROM tasks WHERE run='r3' AND id='T3'").fetchone()["deps"]
        _check(deps == "r1:T1", f"case A deps {deps}")
    finally:
        con.close()
    # case B: neither run is stored yet; dependency order imports the target first
    fresh = os.path.join(tmp, "order")
    fresh_home = os.path.join(tmp, "order-home")
    os.makedirs(fresh_home)
    _write(os.path.join(fresh, "runs", "b2", "run.json"), json.dumps(
        {"id": "b2", "status": "done", "tasks": [
            {"id": "T", "title": "needs b1", "status": "done", "parent": None, "deps": ["b1:T"]}]}))
    _write(os.path.join(fresh, "runs", "b1", "run.json"), json.dumps(
        {"id": "b1", "status": "done", "tasks": [
            {"id": "T", "title": "base", "status": "done", "parent": None, "deps": []}]}))
    code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", fresh, "--all", "--home", fresh_home])
    _check(code == 0, f"case B: {code} {out} {err}")
    con = mkdb.open_project(fresh_home, "matrixkoder")
    try:
        deps = con.execute("SELECT deps FROM tasks WHERE run='b2' AND id='T'").fetchone()["deps"]
        _check(deps == "b1:T", f"case B deps {deps}")
    finally:
        con.close()
    # case C: the target run is in neither the store nor this call, so the dep is dropped and named
    lone = os.path.join(tmp, "lone")
    lone_home = os.path.join(tmp, "lone-home")
    os.makedirs(lone_home)
    _write(os.path.join(lone, "runs", "lone", "run.json"), json.dumps(
        {"id": "lone", "status": "done", "tasks": [
            {"id": "T", "title": "alone", "status": "done", "parent": None, "deps": ["nowhere:Z"]}]}))
    code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", lone, "--run", "lone", "--home", lone_home])
    _check(code == 0, f"case C: {code} {out} {err}")
    doc = json.loads(out)
    _check(doc["unresolved_deps"] == [{"run": "lone", "task": "T", "dep": "nowhere:Z"}], doc["unresolved_deps"])
    con = mkdb.open_project(lone_home, "matrixkoder")
    try:
        deps = con.execute("SELECT deps FROM tasks WHERE run='lone' AND id='T'").fetchone()["deps"]
        _check(deps == "", f"case C stored {deps!r}")
    finally:
        con.close()

    # hostile run ids: nothing outside the data dir is read
    marker = os.path.join(tmp, "outside-read")
    for bad in ("../x", "/tmp", ""):
        before = set(os.listdir(tmp))
        code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", data, "--run", bad, "--home", home])
        _check(code != 0 and out.startswith('{"ok": false'), f"hostile {bad!r}: {code} {out} {err}")
        _check("Traceback" not in err, f"traceback for {bad!r}: {err}")
        _check(not os.path.exists(marker), f"{bad!r} read outside")
    outside = os.path.join(tmp, "outside")
    os.makedirs(os.path.join(outside, "secret"), exist_ok=True)
    _write(os.path.join(outside, "run.json"), json.dumps({"id": "nope", "tasks": []}))
    link = os.path.join(data, "runs", "linked")
    os.symlink(outside, link)
    code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", data, "--run", "linked", "--home", home])
    _check(code != 0 and "escapes" in out, f"symlink: {code} {out} {err}")

    # one malformed run.json among two good runs
    bad_dir = os.path.join(tmp, "mixed")
    good_home = os.path.join(tmp, "mixed-home")
    os.makedirs(good_home)
    for name in ("good1", "good2"):
        _write(os.path.join(bad_dir, "runs", name, "run.json"),
               json.dumps({"id": name, "status": "done", "tasks": [
                   {"id": "T", "title": name, "status": "done", "parent": None, "deps": []}]}))
    _write(os.path.join(bad_dir, "runs", "bad", "run.json"), "{not json")
    code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", bad_dir, "--all", "--home", good_home])
    _check(code == 4, f"mixed exit {code} {out} {err}")
    doc = json.loads(out)
    _check(sorted(doc["imported"]) == ["good1", "good2"], doc["imported"])
    _check(any(f["run"] == "bad" for f in doc["failed"]), doc["failed"])

    # a non-object run.json is a JSON error, never a traceback
    obj = os.path.join(tmp, "obj")
    _write(os.path.join(obj, "runs", "arr", "run.json"), "[1, 2, 3]")
    code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", obj, "--run", "arr",
                               "--home", os.path.join(tmp, "obj-home")])
    _check(code == 2 and out.startswith('{"ok": false') and "Traceback" not in err, f"non-object: {code} {out} {err}")



def _symlinks(tmp):
    """Files inside a run dir may not point outside it. A link to another file in the same dir may."""
    data = os.path.join(tmp, "links")
    home = os.path.join(tmp, "links-home")
    os.makedirs(home)
    secret = os.path.join(tmp, "secret-events.jsonl")
    _write(secret, json.dumps({"t": "1999-01-01T00:00:00+00:00", "run": "lnk", "task": "T",
                               "type": "note", "text": "SECRET-OUTSIDE", "by": "x"}) + "\n")
    run_dir = os.path.join(data, "runs", "lnk")
    _write(os.path.join(run_dir, "run.json"), json.dumps(
        {"id": "lnk", "status": "done", "tasks": [
            {"id": "T", "title": "t", "status": "done", "parent": None, "deps": []}]}))
    os.symlink(secret, os.path.join(run_dir, "events.jsonl"))
    code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", data, "--run", "lnk", "--home", home])
    _check(code == 2 and "escapes" in out, f"events link: {code} {out} {err}")
    _check("SECRET-OUTSIDE" not in out, "secret leaked into the result")
    if os.path.exists(os.path.join(home, "projects")):
        con = mkdb.open_project(home, "matrixkoder")
        try:
            texts = [r["text"] for r in con.execute("SELECT text FROM events")]
            _check("SECRET-OUTSIDE" not in texts, texts)
        finally:
            con.close()

    # the events file is legitimate; the artifact link escapes and is skipped by name
    os.remove(os.path.join(run_dir, "events.jsonl"))
    _write(os.path.join(run_dir, "events.jsonl"), json.dumps(
        {"t": "2020-01-01T00:00:00+00:00", "run": "lnk", "task": "T", "type": "note",
         "text": "inside", "by": "x"}) + "\n")
    outside_art = os.path.join(tmp, "secret-brief.md")
    _write(outside_art, "SECRET-ART")
    os.makedirs(os.path.join(run_dir, "tasks", "T"))
    os.symlink(outside_art, os.path.join(run_dir, "tasks", "T", "brief-r1.md"))
    real_report = os.path.join(run_dir, "tasks", "T", "real-report.md")
    _write(real_report, "kept report\n")
    os.symlink(real_report, os.path.join(run_dir, "tasks", "T", "report-r1.md"))
    code, out, err = _run_cli(["run", "--project", "matrixkoder", "--data", data, "--run", "lnk", "--home", home])
    _check(code == 0, f"artifact link: {code} {out} {err}")
    doc = json.loads(out)
    _check(any(a["reason"] == "symlink escapes run dir" and a["name"].endswith("brief-r1.md")
               for a in doc["skipped_artifacts"]), doc["skipped_artifacts"])
    con = mkdb.open_project(home, "matrixkoder")
    try:
        texts = [r["text"] for r in con.execute("SELECT text FROM artifacts")]
        _check("SECRET-ART" not in texts, texts)
        _check("kept report\n" in texts, texts)
    finally:
        con.close()

    # a sibling directory whose name only shares the run id's prefix is not inside the run dir
    evil = os.path.join(data, "runs", "lnk-evil")
    _write(os.path.join(evil, "run.json"), json.dumps({"id": "lnk-evil", "status": "done", "tasks": []}))
    _check(not inside(run_dir, os.path.join(evil, "run.json")), "sibling prefix passed inside()")


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
    if args.run:
        check_run_id(args.run)
    if args.all:
        result = import_all(home, args.project, os.path.abspath(args.data))
        code = result.pop("exit")
        print(json.dumps({"ok": code == 0, **result}))
        return code
    print(json.dumps({"ok": True, **import_one(home, args.project, os.path.abspath(args.data), args.run)}))
    return 0


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--self-test"]:
            self_test()
        else:
            sys.exit(main(sys.argv[1:]))
    except Fail as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(e.code)
    except mkdb.Fail as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(getattr(e, "code", 2))
