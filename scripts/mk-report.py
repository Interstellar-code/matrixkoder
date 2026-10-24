#!/usr/bin/env python3
"""mk-report.py — the final report of a run, read from the project store (F.6).

  mk-report.py final  --run R [--data DIR] [--where CHECKOUT] [--tree SHA] [--json]
  mk-report.py status --run R [--data DIR] [--where CHECKOUT] [--tree SHA] [--json]
  mk-report.py --self-test

`final` prints the report: one row per acceptance line (evidence id or NONE, and a state
STALE / RED / DEFERRED / MANUAL / GREEN), the evidence counts, risk + open reasons, rework,
parked findings, due reconciles, and CORRECTIVE ACTIONS (open tasks first). Exit 0 when the run is
green, 1 when not. `status --json` is the small, stable shape hooks read: counts + the rows.

Read-only: the store is opened `?mode=ro` through mkdb, paths come from mkpaths. A legacy run (no
fidelity) renders too: its rows are the tasks' acceptance text with evidence NONE and state MANUAL.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mkpaths = _load("mkpaths", os.path.join(HERE, "mkpaths.py"))
mkdb = _load("mkdb", os.path.join(HERE, "mkdb.py"))

CLOSED = ("merged", "qa", "done")
STATES = ("GREEN", "MANUAL", "DEFERRED", "STALE", "RED")  # best to worst; a row takes the worst of its gates


class Fail(Exception):
    pass


def tree_of(where):
    """git tree id of HEAD in `where`, or None (mk-state's current_tree_sha, read-only)."""
    try:
        r = subprocess.run(["git", "-C", where, "rev-parse", "HEAD^{tree}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def gate_state(row, tree):
    """The state of one gate's latest evidence row (None = never run -> STALE)."""
    if row is None:
        return "STALE"
    result = row["result"]
    if result == "green":
        return "GREEN" if tree is not None and row["tree_sha"] == tree else "STALE"
    return "DEFERRED" if result == "deferred" else "RED"


def worst(states):
    return max(states, key=STATES.index)


def build(con, run, tree, where=""):
    """The whole report as a dict, from one read-only connection."""
    r = con.execute("SELECT id, status, extra_json FROM runs WHERE id=?", (run,)).fetchone()
    if r is None:
        raise Fail(f"no run {run!r} in this project store")
    try:
        rx = json.loads(r["extra_json"] or "{}")
    except ValueError:
        rx = {}
    fidelity = bool(rx.get("fidelity"))
    tasks = [dict(t) for t in con.execute(
        "SELECT id, parent, type, title, acceptance, status FROM tasks WHERE run=? ORDER BY rowid", (run,))]
    latest = {}  # (task or None, gate) -> newest evidence row
    for e in con.execute("SELECT id, task, gate, result, tree_sha FROM evidence WHERE run=? ORDER BY id", (run,)):
        latest[(e["task"], e["gate"])] = e
    rows = []
    for t in tasks:
        extra = mkdb._task_extra(con, run, t["id"])
        gates = [g for g in extra.get("gate") or [] if isinstance(g, dict)]
        named = [(g.get("id") or f"{t['id']}#{i}", g) for i, g in enumerate(gates, 1)]
        manual = extra.get("manual")
        lines = [x for x in extra.get("acceptance_lines") or [] if isinstance(x, dict) and x.get("id")]
        if not lines and not fidelity:
            if (t["acceptance"] or "").strip() and t["type"] != "phase":
                rows.append({"id": t["id"], "task": t["id"], "evidence": [], "state": "MANUAL", "ok": True,
                             "text": t["acceptance"].strip().splitlines()[0][:100]})
            continue
        if not lines and (gates or manual):
            lines = [{"id": t["id"], "text": ""}]  # a gate or manual line with no acceptance ids: one row for the task
        for ln in lines:
            cover = [(n, g) for n, g in named
                     if not g.get("lines") or ln["id"] in (g.get("lines") if isinstance(g.get("lines"), list) else [])]
            if cover:
                ev = [latest.get((t["id"], n)) for n, _ in cover]
                state = worst([gate_state(e, tree) for e in ev])
                rows.append({"id": ln["id"], "task": t["id"], "state": state, "ok": state == "GREEN",
                             "evidence": [e["id"] for e in ev if e is not None], "text": ln.get("text", "")})
            elif manual:
                e = latest.get((t["id"], "manual"))
                rows.append({"id": ln["id"], "task": t["id"], "state": "MANUAL", "ok": e is not None and e["result"] == "green",
                             "evidence": [e["id"]] if e is not None else [], "text": ln.get("text", "")})
            else:
                rows.append({"id": ln["id"], "task": t["id"], "state": "MANUAL", "ok": True, "evidence": [],
                             "text": ln.get("text", "")})
    for i, g in enumerate(rx.get("final_gates") or [], 1):
        if not isinstance(g, dict):
            continue
        name = g.get("id") or f"final#{i}"
        e = latest.get((None, name))
        state = gate_state(e, tree)
        rows.append({"id": f"final:{name}", "task": None, "state": state, "ok": state == "GREEN",
                     "evidence": [e["id"]] if e else [], "text": "final gate"})
    counts = {k: v for k, v in con.execute(
        "SELECT result, count(*) FROM evidence WHERE run=? GROUP BY result", (run,))}
    risk, parked = [], []
    for t in tasks:
        reasons = mkdb._open_reasons(con, run, t["id"])
        if reasons:
            risk.append({"task": t["id"], "risk": mkdb._task_risk(con, run, t["id"]), "reasons": reasons})
            parked += [{"task": t["id"], **x} for x in reasons if x["kind"] == "medium_parked"]
    rework = [t["id"] for t in tasks if t["status"] == "rework"]
    due = [x["scope"] for x in con.execute(
        "SELECT scope FROM reconcile_due WHERE run=? AND done IS NULL ORDER BY id", (run,))]
    by_task = {t["id"]: t for t in tasks}
    actions = corrective(rows, risk, rework, due, by_task)
    opens = [t["id"] for t in tasks if t["status"] not in CLOSED]
    green = (all(x["ok"] for x in rows) and not rework and not risk and not due and not opens)
    return {"run": run, "fidelity": fidelity, "green": green, "tree": tree, "where": where,
            "evidence_counts": counts, "rows": rows, "risk": risk, "rework": rework, "parked": parked,
            "reconcile_due": due, "open_tasks": opens, "actions": actions}


def corrective(rows, risk, rework, due, by_task):
    """CORRECTIVE ACTIONS: every action that continues an existing OPEN task comes first, then the
    ones that need a closed task reworked or a gate rerun."""
    acts = []

    def add(task, text):
        st = (by_task.get(task) or {}).get("status")
        acts.append({"task": task, "open": task is not None and st not in CLOSED and st is not None, "text": text})

    for tid, t in by_task.items():
        if t["status"] not in CLOSED and t["type"] != "phase":
            add(tid, f"finish open task {tid} ({t['status']}) and close it")
    for x in rows:
        if x["ok"]:
            continue
        who = x["task"]
        if x["state"] == "RED":
            add(who, f"{x['id']} is RED: fix it, then `gate run {who or '--final'}`")
        elif x["state"] == "DEFERRED":
            add(who, f"{x['id']} was DEFERRED (load): rerun `gate run {who or '--final'}` when the machine is quiet")
        elif x["state"] == "STALE":
            add(who, f"{x['id']} has no green evidence on the current tree: `gate run {who or '--final'}`")
        else:
            add(who, f"{x['id']} is a manual line with no sign-off: `gate signoff {who} --ref <who>`")
    for t in rework:
        add(t, f"{t} is at rework: dispatch a rework round (`task round {t} --kind rework`), then re-close it")
    for r in risk:
        for x in r["reasons"]:
            add(r["task"], f"{r['task']} has open risk reason {x['kind']} (+{x['points']}): clear it with a green gate run")
    for scope in due:
        add(scope, f"reconcile is due for {scope}: `reconcile --run <run> --phase {scope}`")
    return sorted(acts, key=lambda a: not a["open"])


def render(rep):
    out = [f"FINAL REPORT run {rep['run']} — {'GREEN' if rep['green'] else 'NOT GREEN'}"
           + ("" if rep["fidelity"] else " (legacy run: no gate evidence required)")]
    out.append(f"Tree: {str(rep['tree'])[:12]}" + (f" in {rep['where']}" if rep["where"] else ""))
    c = rep["evidence_counts"]
    out.append("Evidence (SELECT result, count(*) FROM evidence WHERE run=…): "
               + (", ".join(f"{k} {c[k]}" for k in sorted(c)) or "none"))
    n = {s: sum(1 for x in rep["rows"] if x["state"] == s) for s in STATES}
    out.append(f"Rows: {len(rep['rows'])} — " + ", ".join(f"{s} {n[s]}" for s in reversed(STATES)))
    out.append("")
    out.append("## Acceptance rows (id | evidence | state)")
    for x in rep["rows"]:
        ev = ",".join(f"#{i}" for i in x["evidence"]) or "NONE"
        out.append(f"- {x['id']} | {ev} | {x['state']}" + (f" | {x['text']}" if x["text"] else ""))
    out.append("")
    out.append("## Risk")
    out += [f"- {r['task']}: risk {r['risk']}; " + ", ".join(f"{x['kind']} +{x['points']}" for x in r["reasons"])
            for r in rep["risk"]] or ["- no open reasons"]
    out.append("## Rework")
    out += [f"- {t}" for t in rep["rework"]] or ["- none"]
    out.append("## Parked findings")
    out += [f"- {p['task']}: {p['source'] or 'medium_parked'}" for p in rep["parked"]] or ["- none"]
    out.append("## Reconcile due")
    out += [f"- {s}" for s in rep["reconcile_due"]] or ["- none"]
    out.append("## Corrective actions")
    if rep["open_tasks"]:
        out.append("- open tasks: " + ", ".join(rep["open_tasks"]))
    out += [f"{i}. {'[open task] ' if a['open'] else ''}{a['text']}" for i, a in enumerate(rep["actions"], 1)] \
        or ["- none"]
    return "\n".join(out)


def status(rep):
    """The small stable shape hooks read (F.7 banner, Stop hook)."""
    n = {s.lower(): sum(1 for x in rep["rows"] if x["state"] == s) for s in STATES}
    return {"ok": True, "run": rep["run"], "fidelity": rep["fidelity"], "green": rep["green"], "red": n["red"], "stale": n["stale"],
            "deferred": n["deferred"], "manual": n["manual"], "rework": len(rep["rework"]),
            "due": len(rep["reconcile_due"]), "open_reasons": sum(len(r["reasons"]) for r in rep["risk"]),
            "rows": [{"id": x["id"], "evidence": x["evidence"], "state": x["state"]} for x in rep["rows"]]}


def open_store(data):
    path = mkpaths.project_db(data=data)
    con = mkdb._open_read(path, mkdb.SCHEMA)
    if con is None:
        raise Fail(f"no project store at {path}")
    return con


def pick_run(con, run):
    if run:
        return run
    rows = [r["id"] for r in con.execute("SELECT id FROM runs WHERE status='active' ORDER BY started DESC")
            if not mkdb.is_bugs_run(r["id"])]  # bugs-tab: the permanent bugs run is never the default
    if len(rows) != 1:
        raise Fail("--run is required" + (f" (active runs: {', '.join(rows)})" if rows else ""))
    return rows[0]


def report_for(a):
    data = mkpaths.data_dir(a.data)
    con = open_store(data)
    try:
        run = pick_run(con, a.run)
        where = a.where or mkpaths.repo_top() or ""
        tree = a.tree or (tree_of(where) if where else None)
        return build(con, run, tree, where)
    finally:
        con.close()


def self_test():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="mk-report-test-")
    os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = tmp
    os.environ["MK_SELFTEST"] = "1"
    data = os.path.join(tmp, "data")
    con = mkdb.open_project(tmp, data_dir=data)
    T = "tree-now"

    def task(run, tid, status, parent=None, typ="task", extra=None, acc=None):
        con.execute("INSERT INTO tasks(run, id, parent, type, title, status, acceptance, extra_json)"
                    " VALUES (?,?,?,?,?,?,?,?)", (run, tid, parent, typ, tid, status, acc, json.dumps(extra or {})))

    def ev(run, tid, gate, result, tree=T):
        return con.execute("INSERT INTO evidence(run, task, gate, result, tree_sha, created) VALUES (?,?,?,?,?,'t')",
                           (run, tid, gate, result, tree)).lastrowid

    con.execute("INSERT INTO runs(id, plan, status, started, extra_json) VALUES ('fr','p','active','2026-01-01',?)",
                (json.dumps({"fidelity": True, "final_gates": [{"id": "fg", "cmd": "true", "covers": ["G"]}]}),))
    task("fr", "P", "merged", typ="phase")
    g = lambda *ids: {"gate": [{"cmd": "true", "lines": list(ids)}], "acceptance_lines": [{"id": i, "text": f"line {i}"} for i in ids]}  # noqa: E731
    task("fr", "G", "merged", "P", extra=g("G.a1"))
    task("fr", "R", "merged", "P", extra=g("R.a1"))
    task("fr", "S", "merged", "P", extra=g("S.a1"))
    task("fr", "D", "merged", "P", extra=g("D.a1"))
    task("fr", "M", "merged", "P", extra={"manual": "human checks", "acceptance_lines": [{"id": "M.a1", "text": "m"}]})
    task("fr", "N", "building", "P", extra=g("N.a1"))
    e_g = ev("fr", "G", "G#1", "green")
    ev("fr", "R", "R#1", "green")
    e_r = ev("fr", "R", "R#1", "red")  # the latest wins: R is RED
    e_s = ev("fr", "S", "S#1", "green", tree="tree-old")
    e_d = ev("fr", "D", "D#1", "deferred")
    e_m = ev("fr", "M", "manual", "green")
    e_f = ev("fr", None, "fg", "green")
    con.execute("INSERT INTO risk_reasons(run, task, kind, points, source, opened) VALUES ('fr','R','medium_parked',1,'review R1','t')")
    con.execute("INSERT INTO reconcile_due(run, scope, created) VALUES ('fr','P','t')")
    con.commit()
    rep = build(con, "fr", T, "/x")
    st = {x["id"]: x for x in rep["rows"]}
    assert st["G.a1"]["state"] == "GREEN" and st["G.a1"]["evidence"] == [e_g], st["G.a1"]
    assert st["R.a1"]["state"] == "RED" and st["R.a1"]["evidence"] == [e_r], st["R.a1"]
    assert st["S.a1"]["state"] == "STALE" and st["S.a1"]["evidence"] == [e_s], st["S.a1"]
    assert st["D.a1"]["state"] == "DEFERRED" and st["D.a1"]["evidence"] == [e_d], st["D.a1"]
    assert st["M.a1"]["state"] == "MANUAL" and st["M.a1"]["evidence"] == [e_m] and st["M.a1"]["ok"], st["M.a1"]
    assert st["N.a1"]["state"] == "STALE" and st["N.a1"]["evidence"] == [], st["N.a1"]  # never run
    assert st["final:fg"]["state"] == "GREEN" and st["final:fg"]["evidence"] == [e_f]
    assert {x["state"] for x in rep["rows"]} == {"GREEN", "RED", "STALE", "DEFERRED", "MANUAL"}
    assert rep["green"] is False
    # the counts line equals a SELECT over the evidence table
    sel = dict(con.execute("SELECT result, count(*) FROM evidence WHERE run='fr' GROUP BY result").fetchall())
    assert rep["evidence_counts"] == sel and sum(sel.values()) == 7, (rep["evidence_counts"], sel)
    text = render(rep)
    line = next(ln for ln in text.splitlines() if ln.startswith("Evidence ("))
    assert line.endswith(", ".join(f"{k} {sel[k]}" for k in sorted(sel))), line
    # every row carries an evidence id or the word NONE
    for ln in text.split("## Acceptance rows")[1].split("## Risk")[0].strip().splitlines()[1:]:
        ev_cell = ln.split(" | ")[1]
        assert ev_cell == "NONE" or ev_cell.startswith("#"), ln
    assert "| NONE | STALE" in text
    assert rep["rework"] == [] and rep["reconcile_due"] == ["P"]
    assert rep["parked"] == [{"task": "R", "kind": "medium_parked", "points": 1, "source": "review R1", "opened": "t"}]
    # corrective actions: the open task N comes first
    assert rep["actions"][0]["task"] == "N" and rep["actions"][0]["open"], rep["actions"]
    assert [a["open"] for a in rep["actions"]] == sorted((a["open"] for a in rep["actions"]), reverse=True)
    # status --json is small and stable
    s = status(rep)
    assert s["fidelity"] is True, s
    assert set(s) == {"ok", "run", "fidelity", "green", "red", "stale", "deferred", "manual", "rework", "due",
                      "open_reasons", "rows"}, set(s)
    assert (s["red"], s["stale"], s["deferred"], s["manual"], s["due"], s["open_reasons"]) == (1, 2, 1, 1, 1, 1), s
    # a green run: all rows green/signed-off, nothing open, nothing due
    con.execute("UPDATE tasks SET status='merged' WHERE run='fr'")
    con.execute("UPDATE risk_reasons SET cleared='t'")
    con.execute("UPDATE reconcile_due SET done='t'")
    for tid, gate in (("R", "R#1"), ("S", "S#1"), ("D", "D#1"), ("N", "N#1")):
        ev("fr", tid, gate, "green")
    con.commit()
    ok = build(con, "fr", T, "/x")
    assert ok["green"] is True and ok["actions"] == [], (ok["rows"], ok["actions"])
    assert any("finish open task N" in a["text"] and a["open"] for a in rep["actions"]), rep["actions"]
    assert render(ok).startswith("FINAL REPORT run fr — GREEN")
    # a legacy run renders: no fidelity, acceptance text rows are NONE / MANUAL
    con.execute("INSERT INTO runs(id, plan, status, started) VALUES ('old','p','active','2026-01-01')")
    task("old", "L1", "merged", acc="it works\nand more")
    task("old", "L2", "merged", acc=None)
    con.commit()
    leg = build(con, "old", None)
    assert [(x["id"], x["evidence"], x["state"]) for x in leg["rows"]] == [("L1", [], "MANUAL")], leg["rows"]
    assert status(leg)["fidelity"] is False, "a legacy run says so in status"
    assert leg["green"] is True and "| NONE | MANUAL" in render(leg) and "legacy run" in render(leg)
    con.close()
    # read-only: the store file is byte-identical after a report
    path = mkpaths.project_db(data=data)
    before = open(path, "rb").read()
    c2 = open_store(data)
    build(c2, "fr", T)
    c2.close()
    assert open(path, "rb").read() == before, "mk-report wrote to the store"
    try:
        build(open_store(data), "nope", T)
    except Fail as e:
        assert "nope" in str(e)
    else:
        raise AssertionError("an unknown run must be refused")
    # bugs-tab: the permanent bugs run (newest here) is never the picked run
    bc = mkdb.open_project(tmp, data_dir=os.path.join(tmp, "bugs-data"))
    bc.execute("INSERT INTO runs(id, plan, status, started) VALUES ('bugs',NULL,'active','2026-02-01')")
    bc.commit()
    try:
        pick_run(bc, None)
    except Fail as e:
        assert "--run is required" in str(e) and "bugs" not in str(e), str(e)
    else:
        raise AssertionError("pick_run picked the bugs run")
    bc.execute("INSERT INTO runs(id, plan, status, started) VALUES ('p1','p','active','2026-01-01')")
    bc.commit()
    assert pick_run(bc, None) == "p1" and pick_run(bc, "bugs") == "bugs"
    bc.close()
    print("mk-report self-test ok")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("final", "status"):
        p = sub.add_parser(name)
        p.add_argument("--run")
        p.add_argument("--data")
        p.add_argument("--where")
        p.add_argument("--tree")
        p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if a.cmd not in ("final", "status"):
        ap.print_help()
        return 2
    try:
        rep = report_for(a)
    except (Fail, mkdb.Fail, mkpaths.Fail) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    if a.cmd == "status":
        print(json.dumps(status(rep), separators=(",", ":")))
        return 0
    print(json.dumps({"ok": True, **rep}) if a.json else render(rep))
    return 0 if rep["green"] else 1


if __name__ == "__main__":
    sys.exit(main())
