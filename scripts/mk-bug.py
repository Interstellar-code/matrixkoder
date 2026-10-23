#!/usr/bin/env python3
"""MatrixKoder bugs — the per-project permanent `bugs` run, one bug per top-level row. Python 3 stdlib only.

  list [--status WORD] [--kind K]
  show <id>
  new --title=T [--kind=K] [--severity=S] [--description=D] [--area=A] [--found-in=F] [--source=S]
      [--steps=X] [--expected=X] [--actual=X] [--labels=a,b]
  set <id> [--status=WORD] [--kind=K] [--severity=S] [--title=T] [--description=D] [--area=A]
      [--found-in=F] [--source=S] [--steps=X] [--expected=X] [--actual=X] [--labels=a,b] [--assignee=X]
  close <id> [--wontfix]  |  reopen <id>  |  delete <id>
  task add <bug id> --title=T [--acceptance=A]
  task set <task id> [--status=STATUS] [--title=T] [--acceptance=A]
  subtask add <task id> --title=T [--acceptance=A]
  check <file>  |  import <file> [--dry-run]  |  drafts      bug draft files (templates/bug.md)
  draft --title=T [the flags of new] [--tasks=<one task title per line>]
  dispatch <task id> [--route="<agent> <model> [<effort>]"] [--owned=<glob,glob>]
      task round + brief (templates/brief.md, bug variant) + mk-lane.py acquire on run bugs; replies
      the mk-lane-event.py arm/dispatch commands to run next. Never starts an agent.
  --self-test

Common: --home DIR, --project SLUG or --data DIR (as mkdb.py), --by WHO (default mk-bug). Every
reply is ONE JSON line {"ok": true, ...}; a failure is {"ok": false, "error": ...}, exit 1. User
text arrives as one --flag=<text> argument and is stored verbatim.

Contract: ~/.matrixkoder/inbox/bugs-tab/contract.md. A bug is a top-level row B<n> of run bugs
(mkdb.BUGS_RUN); its tasks are B<n>.<m>, their subtasks B<n>.<m>.<k>. Rows and every status change
go through mk-state.py (task add / task set -> mkdb.transition, a state-channel event on the bus).
Title, acceptance and the bug metadata (tasks.extra_json key `bug`) are written here through
mkdb.open_project under mk-state's own lock; mk-state lists `bug` in TASK_FILE_KEYS so its later
writes keep it. A deleted bug leaves a note event, so its id is never handed out again.
Drafts are <project>/bugs/<slug>.md (mkpaths.bugs_dir); `import` creates the bug through new/task
add and moves the file to bugs/archive/B<n>-<file> (a file outside bugs/ is copied there and kept).
"""
import argparse, datetime, hashlib, importlib.util, json, os, re, shlex, shutil, subprocess, sys, tempfile, tomllib

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mkdb  # the only module allowed to open a store read-write
import mkpaths  # C1: every MK path

BUGS = mkdb.BUGS_RUN
KINDS = ("frontend", "backend", "cli", "store", "infra", "docs", "other")
SEVERITIES = ("high", "med", "low")
WORDS = ("open", "fixing", "needs", "fixed", "wontfix")
# contract item 6: task status -> bug word (wontfix is done + extra_json.bug.wontfix) and back
WORD_OF = {**dict.fromkeys(("planned", "routed", "ready"), "open"),
           **dict.fromkeys(("dispatched", "building", "takeover", "verifying", "review", "fix"), "fixing"),
           **dict.fromkeys(("blocked", "stalled", "quota", "provider", "qa", "rework", "paused"), "needs"),
           **dict.fromkeys(("merged", "done"), "fixed")}
STATUS_OF = {"open": "planned", "fixing": "building", "needs": "blocked", "fixed": "done", "wontfix": "done"}
TEXT_KEYS = ("description", "area", "found_in", "source", "steps", "expected", "actual", "assignee")
ROW_ORDER = {w: i for i, w in enumerate(("needs", "fixing", "open", "fixed", "wontfix"))}
BUG_RE = re.compile(r"^B([1-9][0-9]*)$")
TASK_RE = re.compile(r"^B[1-9][0-9]*\.[1-9][0-9]*$")
NUM_RE = re.compile(r"^B([0-9]+)(?:\.|$)")
DONE = ("merged", "done")
_ms = None


class Fail(Exception):
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


class Parser(argparse.ArgumentParser):
    def __init__(self, *a, **kw):
        kw.setdefault("allow_abbrev", False)
        super().__init__(*a, **kw)

    def error(self, message):
        raise Fail(message)


def mk_state_mod():
    """mk-state.py loaded by path (its Store gives the project slug, the lock and root_write)."""
    global _ms
    if _ms is None:
        spec = importlib.util.spec_from_file_location("mk_state", os.path.join(HERE, "mk-state.py"))
        _ms = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_ms)
    return _ms


class Ctx:
    """One project: the MK home, the store folder, the slug mk-state keys the overview row on."""

    def __init__(self, home_arg, project, data, by):
        self.home = mkdb.resolve_home(home_arg)
        # mk-state and mkpaths read the home from the environment; pin it to this one
        os.environ["MK_HOME"] = os.environ["MATRIXKODER_HOME"] = self.home
        if data:
            self.folder = mkdb.project_dir(self.home, data_dir=data)
        elif project:
            self.folder = mkdb.project_dir(self.home, project)
        else:
            self.folder = mkdb.project_dir(self.home, data_dir=mkpaths.data_dir())
        self.store = mk_state_mod().Store(self.folder)
        self.slug = self.store.project_slug()
        self.project = project or self.slug
        self.by = by or "mk-bug"
        self.note = None

    def ro(self):
        """A read-only connection, or None when the project has no store yet."""
        return mkdb._open_read(mkdb._store_path(self.home, data_dir=self.folder), mkdb.SCHEMA)

    def state(self, *args):
        """One mk-state.py call on this project; its JSON reply, or Fail with its error text."""
        r = subprocess.run([sys.executable, os.path.join(HERE, "mk-state.py"), "--data", self.folder,
                            "--by", self.by, *args], capture_output=True, text=True, timeout=600)
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            raise Fail(f"mk-state {' '.join(args[:2])}: {(r.stderr or r.stdout).strip() or 'no reply'}")
        if r.returncode != 0 or not out.get("ok"):
            raise Fail(out.get("error") or f"mk-state {' '.join(args[:2])} exit {r.returncode}")
        return out

    def refresh(self):
        """The overview plan_summary rows (bugs included), under mk-state's slug and skip rule."""
        st = self.store
        st.root_write(st.db().refresh_plan_summaries, self.home, self.slug, data_dir=self.folder)


def _extra(text):
    try:
        got = json.loads(text) if text else {}
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def _meta(extra):
    """extra_json.bug with every contract key present and typed."""
    raw = extra.get("bug") if isinstance(extra.get("bug"), dict) else {}
    out = {k: raw[k] if isinstance(raw.get(k), str) else "" for k in TEXT_KEYS}
    out["kind"] = raw.get("kind") if raw.get("kind") in KINDS else "other"
    out["severity"] = raw.get("severity") if raw.get("severity") in SEVERITIES else "med"
    for k in ("labels", "links"):
        out[k] = [x for x in raw.get(k) or [] if isinstance(x, str)] if isinstance(raw.get(k), list) else []
    out["wontfix"] = raw.get("wontfix") is True
    if isinstance(raw.get("draft_sha"), str):  # set by import only: the imported draft's sha256
        out["draft_sha"] = raw["draft_sha"]
    return out


def word_of(status, wontfix=False, verdict=None):
    """Contract item 6: a task status (+ the wontfix flag, the row verdict) as the bug word. A verdict
    needs changes when it is set and does not start with "approve" (any case), as plankanban.tsx
    verdictNeedsChanges; wontfix only ever for a done row."""
    if status in DONE:
        return "wontfix" if wontfix and status == "done" else "fixed"
    if verdict and not verdict.lower().startswith("approve"):
        return "needs"
    return WORD_OF.get(status, "open")


def _rows(con, bid=None):
    if con is None:
        return []
    if bid:
        return con.execute("SELECT * FROM tasks WHERE run=? AND (id=? OR id LIKE ?) ORDER BY rowid",
                           (BUGS, bid, bid + ".%")).fetchall()
    return con.execute("SELECT * FROM tasks WHERE run=? ORDER BY rowid", (BUGS,)).fetchall()


def _bug_obj(row, kids):
    """BugFull from the bug row and its task/subtask rows."""
    extra = _extra(row["extra_json"])
    meta = _meta(extra)
    meta["wontfix"] = meta["wontfix"] and row["status"] == "done"  # a row out of done is never wontfix
    obj = {"id": row["id"], "title": row["title"] or "",
           "status": word_of(row["status"], meta["wontfix"], row["verdict"]),
           "task_status": row["status"], "severity": meta["severity"], "kind": meta["kind"],
           "area": meta["area"], "found_in": meta["found_in"], "labels": meta["labels"],
           "tasks": {"done": sum(1 for k in kids if k["status"] in DONE), "total": len(kids)},
           "lane": next((r["lane"] for r in [row, *kids] if r["lane"]), None),
           "updated": row["updated"], "created": extra.get("created")}
    obj.update({k: meta[k] for k in ("description", "source", "steps", "expected", "actual", "links",
                                     "assignee", "wontfix")})
    return obj


def _bugs(con):
    rows = _rows(con)
    bugs = [r for r in rows if not r["parent"] and BUG_RE.match(r["id"])]
    return [_bug_obj(b, [r for r in rows if r["id"].startswith(b["id"] + ".")]) for b in bugs]


def _one(ctx, bid):
    if not isinstance(bid, str) or not BUG_RE.match(bid):
        raise Fail(f"bad bug id {bid!r}: want B<n>")
    con = ctx.ro()
    try:
        rows = _rows(con, bid)
    finally:
        if con is not None:
            con.close()
    if not rows or rows[0]["id"] != bid:
        raise Fail(f"no bug {bid}")
    return _bug_obj(rows[0], rows[1:])


def _task_obj(ctx, tid):
    con = ctx.ro()
    try:
        r = con.execute("SELECT * FROM tasks WHERE run=? AND id=?", (BUGS, tid)).fetchone() if con else None
    finally:
        if con is not None:
            con.close()
    if r is None:
        raise Fail(f"no task {tid}")
    return {k: r[k] for k in ("id", "parent", "type", "title", "status", "acceptance", "lane", "verdict",
                              "updated")}


def _event(con, tid, kind, text, by):
    """One run-bugs state event, in the caller's transaction (meta_json carries `by` as mk-state's)."""
    con.execute("INSERT INTO events(run, task, t, chan, kind, text, meta_json) VALUES (?,?,?,?,?,?,?)",
                (BUGS, tid, mkdb.now(), "state", kind, text, json.dumps({"by": by}, sort_keys=True)))


def _locked(ctx, fn):
    """fn(con) in one transaction on the project store, under mk-state's lock; its return value."""
    with ctx.store.locked():
        con = mkdb.open_project(ctx.home, ctx.slug, data_dir=ctx.folder)
        try:
            mkdb._begin(con)
            try:
                out = fn(con)
                con.execute("COMMIT")
                return out
            except Exception:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()


def _write(ctx, tid, cols=None, mutate=None):
    """Title/acceptance columns and/or extra_json.bug of one row, under mk-state's lock. Never a
    status: those go through mk-state. mutate(meta) edits extra_json.bug as read INSIDE the lock, so
    two concurrent writers never drop each other's fields; a row out of done loses wontfix. A changed
    title/acceptance logs a decision event with the old and new value, as mk-state task set does."""
    def go(con):
        row = con.execute("SELECT title, acceptance, status, extra_json FROM tasks WHERE run=? AND id=?",
                          (BUGS, tid)).fetchone()
        if row is None:
            raise Fail(f"no task {tid}")
        sets = {}
        for k, new in (cols or {}).items():
            if row[k] != new:  # a key set to its current value is not an edit: no event
                sets[k] = new
                sv = mk_state_mod().short_val
                _event(con, tid, "decision", f"task set {tid} {k}: {sv(row[k])} → {sv(new)}", ctx.by)
        if mutate is not None:
            extra = _extra(row["extra_json"])
            meta = _meta(extra)
            mutate(meta)
            if row["status"] != "done":
                meta["wontfix"] = False
            if extra.get("bug") != meta:
                extra["bug"] = meta
                sets["extra_json"] = json.dumps(extra, ensure_ascii=False, sort_keys=True)
        if sets:
            sets["updated"] = mkdb.now()
            con.execute(f"UPDATE tasks SET {', '.join(k + '=?' for k in sets)} WHERE run=? AND id=?",
                        (*sets.values(), BUGS, tid))
            mkdb._change_log(con, BUGS, tid)
    _locked(ctx, go)


def _clear_wontfix(ctx, tid):
    """After a status change on a bug or its tasks: the bug row out of done drops wontfix."""
    _write(ctx, tid.split(".")[0], mutate=lambda meta: None)


def _integration(ctx):
    """The project's configured integration branch, else main (and ctx.note says so)."""
    r = subprocess.run([sys.executable, os.path.join(HERE, "mk-config.py"), "get", "repo.integration_branch",
                        "--data", ctx.folder], capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        out = {}
    val = out.get("value")
    if r.returncode == 0 and out.get("source") not in (None, "default") and isinstance(val, str) and val.strip():
        return val.strip()
    ctx.note = "no repo.integration_branch in the project config: run bugs uses main"
    return "main"


def ensure_run(ctx):
    con = ctx.ro()
    try:
        have = con is not None and con.execute("SELECT 1 FROM runs WHERE id=?", (BUGS,)).fetchone()
    finally:
        if con is not None:
            con.close()
    if not have:
        ctx.state("run", "init", "--bugs-run", "--branch", _integration(ctx))


def _next_id(ctx, parent=None):
    """B<max+1> over every bug id the run has seen (rows and events: a deleted bug's id stays
    used), or <parent>.<max+1> over the parent's children."""
    con = ctx.ro()
    try:
        if parent:
            ids = [r["id"] for r in con.execute("SELECT id FROM tasks WHERE run=? AND parent=?", (BUGS, parent))]
            return f"{parent}.{max([int(i.rsplit('.', 1)[1]) for i in ids if i.rsplit('.', 1)[1].isdigit()] + [0]) + 1}"
        ids = [r[0] for r in con.execute("SELECT id FROM tasks WHERE run=? UNION SELECT task FROM events"
                                         " WHERE run=? AND task IS NOT NULL", (BUGS, BUGS))] if con else []
    finally:
        if con is not None:
            con.close()
    return f"B{max([int(m.group(1)) for m in map(NUM_RE.match, ids) if m] + [0]) + 1}"


def _labels(text):
    return [x.strip() for x in text.split(",") if x.strip()]


def _meta_changes(a, meta):
    """Apply the metadata flags of new/set onto meta (validated), in place."""
    if a.kind is not None:
        if a.kind not in KINDS:
            raise Fail(f"unknown kind {a.kind!r}; one of: {' '.join(KINDS)}")
        meta["kind"] = a.kind
    if a.severity is not None:
        if a.severity not in SEVERITIES:
            raise Fail(f"unknown severity {a.severity!r}; one of: {' '.join(SEVERITIES)}")
        meta["severity"] = a.severity
    for k in TEXT_KEYS:
        v = getattr(a, k, None)
        if v is not None:
            meta[k] = v
    if a.labels is not None:  # a --labels text, or a draft's list
        meta["labels"] = _labels(a.labels) if isinstance(a.labels, str) else list(a.labels)
    return meta


def _title(text):
    if text is None or not text.strip():
        raise Fail("--title needs a non-empty text")
    return text


def _set_status(ctx, tid, status):
    ctx.state("--run", BUGS, "task", "set", tid, f"status={status}")


def cmd_list(ctx, a):
    if a.status is not None and a.status not in WORDS:
        raise Fail(f"unknown status {a.status!r}; one of: {' '.join(WORDS)}")
    if a.kind is not None and a.kind not in KINDS:
        raise Fail(f"unknown kind {a.kind!r}; one of: {' '.join(KINDS)}")
    con = ctx.ro()
    try:
        bugs = _bugs(con)
    finally:
        if con is not None:
            con.close()
    counts = {w: sum(1 for b in bugs if b["status"] == w) for w in WORDS}
    shown = [b for b in bugs if (a.status is None or b["status"] == a.status)
             and (a.kind is None or b["kind"] == a.kind)]
    shown.sort(key=lambda b: -int(b["id"][1:]))
    shown.sort(key=lambda b: (ROW_ORDER[b["status"]], SEVERITIES.index(b["severity"])))
    keep = ("id", "title", "status", "task_status", "severity", "kind", "area", "found_in", "labels",
            "tasks", "lane", "updated", "created")
    return {"ok": True, "project": ctx.project, "counts": counts,
            "bugs": [{k: b[k] for k in keep} for b in shown]}


def cmd_show(ctx, bid):
    bug = _one(ctx, bid)
    con = ctx.ro()
    try:
        rows = con.execute(
            "SELECT id, parent, type, title, status, deps, round, verdict, acceptance, owned,"
            " route_json, slot, branch, pane, updated FROM tasks"
            " WHERE run=? AND (id=? OR id LIKE ?) ORDER BY rowid", (BUGS, bid, bid + ".%")).fetchall()
        phases = mkdb._phases_from_run(rows, {}, BUGS, con)
        ids = [r["id"] for r in rows]
        evs = con.execute(  # the bug's own events only, newest 50 (never the run's whole history)
            f"SELECT id, t, kind, task, to_id, text, meta_json FROM events WHERE run=? AND chan='state'"
            f" AND task IN ({','.join('?' * len(ids))}) ORDER BY id DESC LIMIT 50", (BUGS, *ids)).fetchall()
    finally:
        con.close()
    counts = {}
    for r in rows[1:]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    out = []
    for e in evs:
        by = _extra(e["meta_json"]).get("by")
        out.append({"id": e["id"], "t": e["t"], "kind": e["kind"], "task": e["task"], "to": e["to_id"],
                    "text": e["text"], "by": by if isinstance(by, str) and by else "orchestrator"})
    plan = {"id": bid, "title": bug["title"], "status": bug["task_status"], "live_run": BUGS,
            "counts": counts, "phases": phases}
    return {"ok": True, "project": ctx.project, "bug": bug, "plan": plan, "events": out}


def _reply(ctx, bid):
    ctx.refresh()
    out = {"ok": True, "bug": _one(ctx, bid)}
    if ctx.note:
        out["note"] = ctx.note
    return out


def _clash(e, tid):
    """mk-state add_task's own id-clash refusal (`task <id> exists[ as <id>]`), nothing else."""
    return re.fullmatch(rf"task {re.escape(tid)} exists( as \S+)?", str(e)) is not None


def _create(ctx, title):
    """A new bug row B<n> through mk-state; its id."""
    ensure_run(ctx)
    for _ in range(3):  # a concurrent `new` may take the id first: mk-state refuses the clash
        bid = _next_id(ctx)
        try:
            ctx.state("--run", BUGS, "task", "add", "--root", "--id", bid, f"--title={title}")
            return bid
        except Fail as e:
            if not _clash(e, bid):
                raise
    raise Fail("could not pick a free bug id after 3 tries")


def cmd_new(ctx, a):
    title = _title(a.title)
    meta = _meta_changes(a, _meta({}))
    bid = _create(ctx, title)
    _write(ctx, bid, mutate=lambda m: m.update(meta))
    return _reply(ctx, bid)


def cmd_set(ctx, a):
    bug = _one(ctx, a.id)
    if a.status is not None and a.status not in WORDS:
        raise Fail(f"unknown status {a.status!r}; one of: {' '.join(WORDS)}")
    _meta_changes(a, _meta({}))  # validate the flags before any write
    cols = {"title": _title(a.title)} if a.title is not None else None
    # a word the bug already shows is a no-op; otherwise only a different task status is written
    if a.status is not None and bug["status"] != a.status and STATUS_OF[a.status] != bug["task_status"]:
        _set_status(ctx, a.id, STATUS_OF[a.status])

    def mutate(meta):  # on the row as read under the lock: a concurrent set keeps its own fields
        _meta_changes(a, meta)
        if a.status is not None and bug["status"] != a.status:
            meta["wontfix"] = a.status == "wontfix"
    _write(ctx, a.id, cols, mutate)
    return _reply(ctx, a.id)


def cmd_close(ctx, bid, wontfix):
    a = argparse.Namespace(id=bid, status="wontfix" if wontfix else "fixed", title=None, kind=None,
                           severity=None, labels=None, **dict.fromkeys(TEXT_KEYS))
    return cmd_set(ctx, a)


def cmd_reopen(ctx, bid):
    a = argparse.Namespace(id=bid, status="open", title=None, kind=None, severity=None, labels=None,
                           **dict.fromkeys(TEXT_KEYS))
    return cmd_set(ctx, a)


# tables whose rows belong to one task of a run: a deleted bug takes its rows along. Kept: events
# and change_log (the history; the note event also keeps the id from being reused). reconcile_due is
# keyed by run + scope, not by task, so it holds nothing of one bug.
TASK_TABLES = ("tasks", "artifacts", "evidence", "risk_reasons")


def cmd_delete(ctx, bid):
    _one(ctx, bid)
    like = (BUGS, bid, bid + ".%")

    def go(con):  # the lane check and the delete in one locked transaction
        laned = [r[0] for r in con.execute("SELECT id FROM tasks WHERE run=? AND (id=? OR id LIKE ?) AND"
                                           " lane IS NOT NULL AND lane != ''", like)]
        laned += [r[0] for r in con.execute("SELECT task FROM lane_slots WHERE run=? AND (task=? OR task LIKE ?)",
                                            like)]
        if laned:
            raise Fail(f"bug {bid} has a lane on {', '.join(sorted(set(laned)))}: finish or free the lane first")
        if con.execute("SELECT 1 FROM tasks WHERE run=? AND id=?", (BUGS, bid)).fetchone() is None:
            raise Fail(f"no bug {bid}")
        # the note keeps the id in the run's events, so _next_id never hands it out again
        _event(con, bid, "note", f"bug {bid} deleted", ctx.by)
        for table in TASK_TABLES:
            key = "id" if table == "tasks" else "task"
            con.execute(f"DELETE FROM {table} WHERE run=? AND ({key}=? OR {key} LIKE ?)", like)
        mkdb._change_log(con, BUGS, bid)
    _locked(ctx, go)
    ctx.refresh()
    return {"ok": True, "deleted": bid}


def cmd_add(ctx, parent, title, acceptance, sub):
    want = TASK_RE if sub else BUG_RE
    if not isinstance(parent, str) or not want.match(parent):
        raise Fail(f"bad {'task' if sub else 'bug'} id {parent!r}: want {'B<n>.<m>' if sub else 'B<n>'}")
    title = _title(title)
    p = _task_obj(ctx, parent)
    for _ in range(3):  # a concurrent add may take the child id first: mk-state refuses the clash
        tid = _next_id(ctx, parent)
        args = ["--run", BUGS, "task", "add", "--parent", parent, "--id", tid, f"--title={title}"]
        if p["status"] in ("merged", "qa", "done"):
            args.append("--followup")  # a task on a closed bug/task re-opens it (mk-state's rule)
        try:
            ctx.state(*args)
            break
        except Fail as e:
            if not _clash(e, tid):
                raise
    else:
        raise Fail(f"could not pick a free task id under {parent} after 3 tries")
    if acceptance is not None:
        _write(ctx, tid, {"acceptance": acceptance})
    _clear_wontfix(ctx, parent)
    ctx.refresh()
    return {"ok": True, "task": _task_obj(ctx, tid)}


def cmd_task_set(ctx, a):
    if not isinstance(a.id, str) or not re.match(r"^B[1-9][0-9]*(\.[1-9][0-9]*){1,2}$", a.id):
        raise Fail(f"bad task id {a.id!r}: want B<n>.<m> or B<n>.<m>.<k>")
    _task_obj(ctx, a.id)
    cols = {}
    if a.title is not None:
        cols["title"] = _title(a.title)
    if a.acceptance is not None:
        cols["acceptance"] = a.acceptance
    if a.status is not None:
        _set_status(ctx, a.id, a.status)
        _clear_wontfix(ctx, a.id)
    if cols:
        _write(ctx, a.id, cols)
    ctx.refresh()
    return {"ok": True, "task": _task_obj(ctx, a.id)}


# --- dispatch (phase LANE): round + brief + lane acquire on run bugs; never starts an agent --------

BRIEF_TMPL = os.path.join(os.path.dirname(HERE), "skills", "matrixkoder", "templates", "brief.md")


def kinds_arm():
    """lane-health.py's KINDS (the mk-lane-event.py arm --kind values), loaded by path."""
    spec = importlib.util.spec_from_file_location("lane_health", os.path.join(HERE, "lane-health.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.KINDS


def _repo(ctx):
    """The project's registered repo (root projects.repo) when it is a folder, else the cwd: the
    main checkout mk-lane.py acquire works from."""
    con = mkdb._open_read(mkpaths.root_db(ctx.home), mkdb.ROOT_SCHEMA)
    try:
        row = con.execute("SELECT repo FROM projects WHERE slug=?", (ctx.slug,)).fetchone() if con else None
    finally:
        if con is not None:
            con.close()
    return row["repo"] if row and row["repo"] and os.path.isdir(row["repo"]) else os.getcwd()


def _other_lanes(ctx, tid):
    con = ctx.ro()
    try:
        rows = con.execute("SELECT run, id FROM tasks WHERE lane IS NOT NULL AND lane != '' AND NOT"
                           " (run=? AND id=?) ORDER BY run, id", (BUGS, tid)).fetchall()
    finally:
        con.close()
    return ", ".join(f"{r['id']} (run {r['run']})" for r in rows) or "no other lane"


def _base_sha(repo, ref):
    r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                        ref + "^{commit}"], capture_output=True, text=True, timeout=60)
    return r.stdout.strip()[:12] if r.returncode == 0 and r.stdout.strip() else f"the tip of {ref}"


def _slots(line, values):
    """Fill every `<slot>` of one template line from values; an unknown slot is a template drift."""
    def one(m):
        if m.group(1) not in values:
            raise Fail(f"brief template slot <{m.group(1)}> has no bug value: update mk-bug dispatch")
        return values[m.group(1)]
    return re.sub(r"<([^<>\n]+)>", one, line)


def brief_text(v):
    """templates/brief.md filled for one bug task (the plan-less variant). v: the values below."""
    with open(BRIEF_TMPL, encoding="utf-8") as fh:
        tmpl = fh.read().splitlines()
    t, n = v["task"], v["round"]
    bodies = {"## Spec": v["spec"], "## Files you own": v["owned"], "## Real inputs": v["inputs"],
              "## Parallel parts": ["none, because a bug task is one focused fix: work it alone."]}
    lines = {"- Risk:": f"- Risk: {v['risk']}. Critical: no.",
             "- Gates:": "- Gates: none (run bugs has no plan gates). You run the ones in Commands.",
             "<required tests": "A test that reproduces the bug through the real code path: it fails on the "
                                "old code and passes with the fix; the acceptance in Spec is what it checks. Mock "
                                "only the network edge (fetch / engine client), never the module under test. Prove "
                                "the fail-on-old-code with the mk-builder skill's proof method "
                                "(reference/lane-rules.md R13) — follow it as written.",
             "- Install:": "- Install: none, unless the repo's README needs a step for the files you change",
             "- Tests:": "- Tests: the tests covering the files you change, one command each (known unrelated "
                         "failures: none known). Not the full suite; mk-builder skill R33.",
             "- Types:": "- Types: the repo's typecheck, when it has one, gains no new error; nothing new in "
                         "the files you own",
             "- Lint/format": "- Lint/format ONLY changed files with the repo's own tools — never repo-wide",
             "- Banned commands": "- Banned commands (repo list): none beyond the mk-builder lane rules",
             "- Commit:": f"- Commit: subject `fix(bugs): {t} ` plus a short summary, blank line, "
                          "`Co-Authored-By: ` your agent/model name and email."}
    rules = {"n": str(n), "PHASE": t, "report path in this lane": f".matrixkoder-report-r{n}.md",
             "question": "<question>"}  # the BLOCKED token's own syntax stays
    out, skip, sec = [], None, None
    for line in tmpl:
        if line.startswith("## "):
            sec = line
            skip = line if line.startswith(("## TAKEOVER", "## FIX", "## LEAD")) or line in bodies else None
            if line in bodies:
                out += [line, *bodies[line], ""]
                continue
        if skip == "plan repo":
            skip = None
            if not line.strip():
                continue
        if skip:
            continue
        if line.startswith("# <PHASE>"):
            line = f"# {t} — {v['title']}"
        elif line.startswith("Repo: "):
            skip = "plan repo"  # the plan variant and the blank line after it
            continue
        elif line.startswith("Repo, bug variant"):
            line = "Repo: " + _slots(line.split("): ", 1)[1], v["repo"])
        elif line.startswith("What it is"):
            line = f"What it is (plain English): {v['what']}"
        elif any(line.startswith(k) for k in lines):
            line = next(lines[k] for k in lines if line.startswith(k))
        elif sec == "## Rules":
            line = _slots(line, rules)
        out.append(line)
    return "\n".join(out).rstrip("\n") + "\n"


def _route(text):
    parts = (text or "").split()
    if text is not None and len(parts) not in (2, 3):
        raise Fail(f"--route {text!r}: want \"<agent> <model> [<effort>]\"")
    return dict(zip(("agent", "model", "effort"), parts))


def cmd_dispatch(ctx, a):
    """Contract LANE.1: validate, round, brief, lane acquire; the arm/dispatch commands to run next."""
    tid = a.id
    if not isinstance(tid, str) or not re.match(r"^B[1-9][0-9]*(\.[1-9][0-9]*){1,2}$", tid):
        raise Fail(f"bad task id {tid!r}: want B<n>.<m> or B<n>.<m>.<k> (a bug row B<n> is never dispatched)")
    row = _task_obj(ctx, tid)
    if row["type"] not in ("task", "subtask"):
        raise Fail(f"{tid} is a {row['type']} row: dispatch takes a task or subtask")
    if not (row["acceptance"] or "").strip():
        raise Fail(f"task {tid} has no acceptance: set it first (mk-bug task set {tid} --acceptance=...)")
    if row["lane"]:
        raise Fail(f"task {tid} already has a lane ({row['lane']}): a fix round reuses it, never a second acquire")
    bug = _one(ctx, tid.split(".")[0])
    if row["status"] in DONE or bug["status"] in ("fixed", "wontfix"):
        raise Fail(f"task {tid} is {row['status']} and bug {bug['id']} is {bug['status']}: reopen it before a dispatch")
    con = ctx.ro()
    try:
        prior = con.execute("SELECT round FROM tasks WHERE run=? AND id=?", (BUGS, tid)).fetchone()["round"] or 0
    finally:
        con.close()
    if prior > 0:
        raise Fail(f"task {tid} is at round {prior} with no lane: an earlier dispatch stopped part way (its error "
                   "named what was done). Finish that by hand (mk-state task check, then mk-lane.py acquire "
                   f"--run {BUGS} --task {tid} from the repo), never a second round")
    route = _route(a.route)
    owned = _labels(a.owned) if a.owned is not None else ctx.state("--run", BUGS, "task", "get", tid)["task"].get("owned") or []
    if not owned:
        raise Fail(f"task {tid} owns no files: pass --owned=<glob,glob> (a briefed task needs owned)")
    done = []

    def step(what, fn, wrote=True):  # wrote=False: a lookup, never listed as done
        try:
            out = fn()
        except Exception as e:  # Fail, and a TimeoutExpired / OSError from a step's subprocess or file
            if not done:
                raise
            raise Fail(f"dispatch {tid}: done: {'; '.join(done)}; then {what} failed: {e}. "
                       "Nothing was undone: fix the cause and finish by hand, or free what was done")
        if wrote:
            done.append(what)
        return out

    sets = ([f"owned={','.join(owned)}"] if a.owned is not None else []) + [f"{k}={v}" for k, v in route.items()]
    if sets:
        step("task set " + " ".join(sets), lambda: ctx.state("--run", BUGS, "task", "set", tid, *sets))
    rnd = step("task round", lambda: ctx.state("--run", BUGS, "task", "round", tid, "--kind", "build",
                                               "--cause", "mk-bug dispatch"))
    n, path = rnd["round"], rnd["brief"]
    repo = step("repo lookup", wrote=False, fn=lambda: _repo(ctx))
    integ = step("integration branch", wrote=False, fn=lambda: _integration(ctx))
    reasons = step("risk list", wrote=False, fn=lambda: ctx.state("--run", BUGS, "risk", "list", tid))
    open_r = [r for r in reasons.get("reasons") or [] if not r.get("cleared")]
    risk = (f"{reasons['risk'].get(tid, 1)}: " + "; ".join(f"{r['kind']} {r['points']} ({r.get('source') or 'no source'})"
                                                          for r in open_r)) if open_r else "1, no open reasons"
    m = bug
    facts = [f"{k}: {m[k]}" for k in ("kind", "severity", "area", "found_in", "source") if m.get(k)]
    parts = [[f"Bug {bug['id']}: {bug['title']} ({', '.join(facts)})."]]
    for k, label in (("description", "Description"), ("steps", "Steps"), ("expected", "Expected"),
                     ("actual", "Actual")):
        if m.get(k):
            parts.append([f"{label}:", *("   " + x for x in m[k].splitlines())])
    if row["type"] == "subtask":
        ptitle = step("parent lookup", wrote=False, fn=lambda: _task_obj(ctx, row["parent"])["title"])
        parts.append([f"This is a subtask of {row['parent']} ({ptitle})."])
    parts.append([f"Task {tid}: {row['title']}. Fix the cause, the smallest change that meets:",
                  *("   Acceptance: " + x for x in row["acceptance"].splitlines() if x.strip())])
    spec = [f"{i}. {p[0]}" if j == 0 else p[j] for i, p in enumerate(parts, 1) for j in range(len(p))]
    sha = step("base sha", wrote=False, fn=lambda: _base_sha(repo, integ))
    text = step("brief text", wrote=False, fn=lambda: brief_text({
        "task": tid, "round": n, "title": row["title"], "risk": risk, "spec": spec,
        "what": f"bug {bug['id']}: {bug['title']}." + (f" {' '.join(bug['description'].split())}"
                                                       if bug["description"] else ""),
        "owned": [*owned, "Do NOT touch: anything outside the list above."],
        "inputs": ["The bug's own steps in Spec, run on the real code path; real data where the steps name it."],
        "repo": {"path": "this lane's worktree", "branch": f"mk/{BUGS}/{tid.lower()}", "sha": sha,
                 "stack": "see the repo README", "bug id": bug["id"], "T": tid,
                 "bug task folder": os.path.dirname(path), "lanes + their areas": _other_lanes(ctx, tid)}}))
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(text)
    try:
        step("task brief", lambda: ctx.state("--run", BUGS, "task", "brief", tid, "--file", fh.name))
    finally:
        os.unlink(fh.name)
    def check():  # passes when the checks above hold; never acquire on a failing check
        r = subprocess.run([sys.executable, os.path.join(HERE, "mk-state.py"), "--data", ctx.folder, "--run", BUGS,
                            "task", "check", tid], capture_output=True, text=True, timeout=600)
        try:
            got = json.loads(r.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            got = {}
        if not got.get("ok"):
            raise Fail(f"missing {got.get('missing')}" if got.get("missing") else (r.stderr or r.stdout).strip()[:300])
    step("task check", check)

    def acquire():
        r = subprocess.run([sys.executable, os.path.join(HERE, "mk-lane.py"), "--data", ctx.folder, "acquire",
                            "--run", BUGS, "--task", tid], capture_output=True, text=True, timeout=600, cwd=repo)
        try:
            got = json.loads(r.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            raise Fail((r.stderr or r.stdout).strip()[:400] or f"exit {r.returncode}")
        if r.returncode != 0 or not got.get("ok"):
            raise Fail(" ".join(str(got.get(k)) for k in ("error", "stderr") if got.get(k)) or f"exit {r.returncode}")
        return got
    lane = step("lane acquire", acquire)
    con = ctx.ro()
    try:
        r = con.execute("SELECT * FROM tasks WHERE run=? AND id=?", (BUGS, tid)).fetchone()
    finally:
        con.close()
    task = {**_task_obj(ctx, tid), "slot": r["slot"], "branch": r["branch"], "round": r["round"]}
    q = shlex.quote
    py = f"python3 {q(os.path.join(HERE, 'mk-lane-event.py'))}"
    kind = route.get("agent") if route.get("agent") in kinds_arm() else "<kind>"
    grep = r"^fix\(bugs\): " + re.escape(tid) + " "
    report = os.path.join(lane["path"], f".matrixkoder-report-r{n}.md")
    arm = (f"{py} arm --lane {q(lane['path'])} --pane <pane> --task {tid} --run {BUGS} --data {q(ctx.folder)}"
           f" --phase {q(f'{tid} R{n}')} --commit-grep {q(grep)} --report {q(report)} --kind {kind}")
    say = f"Read and follow {path} exactly; the last line of your reply must be {tid} R{n} DONE"
    disp = f"{py} dispatch --connect --pane <pane> --task {tid} --text {q(say)}" + "".join(
        f" --{k} {q(v)}" for k, v in route.items())
    out = {"ok": True, "task": task, "lane": r["lane"], "branch": r["branch"], "brief": path, "next": [arm, disp]}
    if lane.get("warning"):
        out["warning"] = lane["warning"]
    return out


# --- drafts (phase DRAFT): <project>/bugs/<slug>.md, templates/bug.md -----------------------------

DRAFT_KEYS = ("bug", "title", "kind", "severity", "found_in", "area", "labels", "source")
SECTIONS = ("description", "steps", "expected", "actual")  # the `## ` body sections, as metadata keys
TASK_KEYS = ("title", "acceptance", "subtask")


def _read_draft(path):
    if os.path.islink(path):
        raise Fail(f"draft {path} is a symlink")
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8")
    except OSError as e:
        raise Fail(f"cannot read draft {path}: {e.strerror or e}")
    except UnicodeDecodeError:
        raise Fail(f"draft {path} is not valid UTF-8")


def _parse_draft(text):
    """(header dict or None, {section: text}, errors, warnings). The +++ TOML header is split as mk-plan's
    split_plan/parse_header do (BOM and one \\r per line ignored); body `## X` sections by name."""
    lines = [l[:-1] if l.endswith("\r") else l for l in text.lstrip("﻿").split("\n")]
    if lines[0].strip() != "+++":
        return None, {}, ["line 1: first line must be '+++'"], []
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "+++"), None)
    if close is None:
        return None, {}, [f"line {len(lines)}: no closing '+++' for the header"], []
    try:
        head = tomllib.loads("\n".join(lines[1:close]))
    except tomllib.TOMLDecodeError as e:
        return None, {}, [f"header: {e}"], []
    body, cur, warnings = {}, None, []
    for l in lines[close + 1:]:
        m = re.match(r"^##\s+(.+?)\s*$", l)
        if m:
            name = m.group(1).lower()
            if name in body:
                warnings.append(f"section '## {name}' appears twice: its parts are joined")
            cur = body.setdefault(name, [])
        elif cur is not None:
            if re.match(r"^##\s", l):
                warnings.append(f"section '## {name}': a line starting '##' is text, not a heading")
            cur.append(l)
    for k, v in body.items():
        while v and not v[0].strip():
            v.pop(0)
        while v and not v[-1].strip():
            v.pop()
        body[k] = "\n".join(v)
    return head, body, [], warnings


def _check_task(t, where, errors, warnings):
    if not isinstance(t.get("title"), str) or not t["title"].strip():
        errors.append(f"{where} without title")
    if "acceptance" not in t:
        warnings.append(f"{where} has no acceptance")
    elif not isinstance(t["acceptance"], str):
        errors.append(f"{where} acceptance must be a string")
    for k in sorted(set(t) - set(TASK_KEYS)):
        warnings.append(f"{where}: unknown key {k!r}")


def check_draft(text):
    """-> ({ok, errors, warnings}, header dict or {}, body sections)."""
    head, body, errors, warnings = _parse_draft(text)
    if head is None:
        return {"ok": False, "errors": errors, "warnings": warnings}, {}, {}
    for k in sorted(set(head) - set(DRAFT_KEYS) - {"task"}):
        warnings.append(f"unknown header key {k!r}")
    for k in DRAFT_KEYS:
        if k in head and k != "labels" and not isinstance(head[k], str):
            errors.append(f"{k} must be a string")
    if not isinstance(head.get("title"), str) or not head["title"].strip():
        errors.append("missing title")
    if isinstance(head.get("kind"), str) and head["kind"] not in KINDS:
        errors.append(f"unknown kind {head['kind']!r}; one of: {' '.join(KINDS)}")
    if isinstance(head.get("severity"), str) and head["severity"] not in SEVERITIES:
        errors.append(f"unknown severity {head['severity']!r}; one of: {' '.join(SEVERITIES)}")
    if "bug" not in head:
        warnings.append("no bug slug")
    elif isinstance(head["bug"], str) and not mkpaths.SLUG_RE.match(head["bug"]):
        errors.append(f"bad bug slug {head['bug']!r}: want [a-z0-9][a-z0-9-]{{0,40}}")
    labels = head.get("labels", [])
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        errors.append("labels must be a list of strings")
    tasks = head.get("task", [])
    if not isinstance(tasks, list) or not all(isinstance(t, dict) for t in tasks):
        errors.append("task must be [[task]] blocks")
        tasks = []
    if not tasks:
        warnings.append("no [[task]]: the bug is created without tasks")
    for i, t in enumerate(tasks, 1):
        _check_task(t, f"task {i}", errors, warnings)
        subs = t.get("subtask", [])
        if not isinstance(subs, list) or not all(isinstance(s, dict) for s in subs):
            errors.append(f"task {i}: subtask must be [[task.subtask]] blocks")
            continue
        for j, s in enumerate(subs, 1):
            _check_task(s, f"task {i} subtask {j}", errors, warnings)
            if "subtask" in s:
                errors.append(f"task {i} subtask {j}: a subtask has no subtasks")
    for k in sorted(set(body) - set(SECTIONS)):
        warnings.append(f"unknown section '## {k}': not imported")
    if not body.get("description"):
        warnings.append("no ## Description text")
    return {"ok": not errors, "errors": errors, "warnings": warnings}, head, body


def cmd_check(path):
    res = check_draft(_read_draft(os.path.abspath(os.path.expanduser(path))))[0]
    return {"ok": res["ok"], "errors": res["errors"], "warnings": res["warnings"]}


def _draft_ns(head, body):
    """The `new` flags a checked draft stands for."""
    a = argparse.Namespace(title=head["title"], kind=head.get("kind"), severity=head.get("severity"),
                           labels=head.get("labels"), assignee=None)
    for k in ("found_in", "area", "source"):
        setattr(a, k, head.get(k))
    for k in SECTIONS:
        setattr(a, k, body.get(k))
    return a


def _imported_as(ctx, sha):
    """The bug whose extra_json.bug.draft_sha is sha, or None."""
    con = ctx.ro()
    try:
        rows = con.execute("SELECT id, extra_json FROM tasks WHERE run=? AND parent IS NULL",
                           (BUGS,)).fetchall() if con else []
    finally:
        if con is not None:
            con.close()
    return next((r["id"] for r in rows if _meta(_extra(r["extra_json"])).get("draft_sha") == sha), None)


IMPORT_FAILS = (Fail, mkdb.Fail, mkpaths.Fail, OSError, subprocess.SubprocessError)


def cmd_import(ctx, path, dry_run):
    path = os.path.abspath(os.path.expanduser(path))
    text = _read_draft(path)
    res, head, body = check_draft(text)
    if not res["ok"]:
        raise Fail(f"draft {path} does not check: {'; '.join(res['errors'])}")
    raw = text.encode("utf-8")  # the bytes read: valid UTF-8 round-trips exactly
    sha = hashlib.sha256(raw).hexdigest()
    dup = _imported_as(ctx, sha)
    if dup:  # a killed or half-failed import left its bug: never a second one from the same draft
        raise Fail(f"draft {path} was already imported as bug {dup} (same draft_sha)")
    # only a draft in the project's bugs folder moves; any other file (the shipped template) stays
    move = os.path.realpath(os.path.dirname(path)) == os.path.realpath(mkpaths.bugs_dir(ctx.folder))
    a = _draft_ns(head, body)
    tasks = head.get("task", [])
    archive = mkpaths.bugs_archive_dir(ctx.folder)
    if dry_run:  # nothing written: the ids are what the next import would take now
        bid = _next_id(ctx)
        meta = _meta_changes(a, _meta({}))
        meta.pop("wontfix")
        plan = [{"id": f"{bid}.{i}", "title": t["title"], "acceptance": t.get("acceptance"),
                 "subtasks": [{"id": f"{bid}.{i}.{j}", "title": s["title"], "acceptance": s.get("acceptance")}
                              for j, s in enumerate(t.get("subtask", []), 1)]}
                for i, t in enumerate(tasks, 1)]
        return {"ok": True, "dry_run": True, "check": res,
                "would": {"bug": {"id": bid, "title": a.title, **meta}, "tasks": plan,
                          "archive": os.path.join(archive, f"{bid}-{os.path.basename(path)}")}}
    meta = _meta_changes(a, _meta({}))
    meta["draft_sha"] = sha
    bid = _create(ctx, _title(a.title))
    made, wrote = [], False
    # the B<n>- prefix keeps one archive name per bug, as bug ids are never reused
    dst = os.path.join(archive, f"{bid}-{os.path.basename(path)}")
    try:  # any failure from here on rolls the bug back (delete path), so the draft can be re-imported
        _write(ctx, bid, mutate=lambda m: m.update(meta))
        for t in tasks:
            tid = cmd_add(ctx, bid, t["title"], t.get("acceptance"), False)["task"]["id"]
            made.append(tid)
            for s in t.get("subtask", []):
                made.append(cmd_add(ctx, tid, s["title"], s.get("acceptance"), True)["task"]["id"])
        # the archive copy: never through or over an existing name; the bytes read, checked, then stat
        os.makedirs(archive, exist_ok=True)
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        wrote = True
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        shutil.copystat(path, dst)
        with open(dst, "rb") as fh:
            if hashlib.sha256(fh.read()).hexdigest() != sha:
                raise OSError(f"copy of {path} to {dst} did not verify")
        out = _reply(ctx, bid)
    except IMPORT_FAILS as e:
        why = f"bug {bid} created with tasks {made}, then: {e}"
        if wrote:
            try:
                os.unlink(dst)
            except OSError as e2:
                why += f"; archive copy {dst} left: {e2}"
        try:
            cmd_delete(ctx, bid)
        except IMPORT_FAILS as e2:
            raise Fail(f"{why}; rollback failed: {e2}; the draft stays at {path}")
        raise Fail(f"{why}; rolled back (bug {bid} deleted); the draft stays at {path}")
    out.update(tasks=made, check=res, archived=dst, kept_source=not move)
    if move:  # the verified copy is in the archive: remove the source, only if it is still those bytes
        try:
            with open(path, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() != sha:
                    raise OSError(f"{path} changed during the import")
            os.unlink(path)
        except OSError as e:
            out["note"] = (f"bug {bid} imported and archived to {dst}, but the draft could not be removed: {e};"
                           " a re-import of it is refused (same draft_sha)")
    return out


def cmd_drafts(ctx):
    folder = mkpaths.bugs_dir(ctx.folder)
    out = []
    names = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
    for n in names:
        p = os.path.join(folder, n)
        if not n.endswith(".md") or os.path.islink(p) or not os.path.isfile(p):
            continue
        try:
            res, head, _ = check_draft(_read_draft(p))
        except Fail as e:
            res, head = {"ok": False, "errors": [str(e)], "warnings": []}, {}
        title = head.get("title")
        out.append({"file": n, "path": p, "bug": head.get("bug") if isinstance(head.get("bug"), str) else None,
                    "title": title if isinstance(title, str) else None,
                    "modified": datetime.datetime.fromtimestamp(os.path.getmtime(p), datetime.timezone.utc)
                    .isoformat(timespec="seconds"), "check": res})
    return {"ok": True, "project": ctx.project, "folder": folder, "drafts": out}


def _toml(s):
    """A TOML basic string (JSON escapes are TOML escapes; DEL must be escaped too)."""
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")


def cmd_draft(ctx, a):
    title = _title(a.title)
    meta = _meta_changes(a, _meta({}))
    for k in SECTIONS:  # the parser reads a `## ` line as the next section: its text would be lost
        if any(re.match(r"^##\s", l) for l in meta[k].split("\n")):
            raise Fail(f"--{k} has a line starting '## ': in a draft it would start a new section")
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or "bug"
    tasks = [t for t in (a.tasks or "").split("\n") if t.strip()]
    folder = mkpaths.bugs_dir(ctx.folder)
    os.makedirs(folder, exist_ok=True)
    for n in range(1, 100):
        slug = base if n == 1 else f"{base[:37].strip('-')}-{n}"
        head = [f"bug = {_toml(slug)}", f"title = {_toml(title)}"]
        head += [f"{k} = {_toml(meta[k])}" for k in ("kind", "severity", "found_in", "area")]
        head += [f"labels = [{', '.join(_toml(x) for x in meta['labels'])}]", f"source = {_toml(meta['source'])}"]
        for t in tasks:
            head += ["", "[[task]]", f"title = {_toml(t.strip())}"]
        text = "+++\n" + "\n".join(head) + "\n+++\n"
        text += "".join(f"\n## {k.capitalize()}\n\n" + (meta[k] + "\n" if meta[k] else "") for k in SECTIONS)
        path = os.path.join(folder, slug + ".md")
        try:  # encoded before the create: a text that cannot be written leaves no 0-byte draft
            raw = text.encode("utf-8")
        except UnicodeEncodeError as e:
            raise Fail(f"draft text is not valid UTF-8: {e.reason}")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
        except OSError:
            os.unlink(path)
            raise
        return {"ok": True, "path": path}
    raise Fail(f"no free draft file name for {base!r} in {folder}")


def _meta_flags(sp, title_required=False):
    sp.add_argument("--title", required=title_required)
    sp.add_argument("--kind"), sp.add_argument("--severity"), sp.add_argument("--labels")
    for k in TEXT_KEYS:
        if k != "assignee":
            sp.add_argument("--" + k.replace("_", "-"), dest=k)


def _cli(argv):
    common = Parser(prog="mk-bug.py", add_help=False)
    common.add_argument("--home"), common.add_argument("--project"), common.add_argument("--data")
    common.add_argument("--by")
    known, rest = common.parse_known_args(argv)
    p = Parser(prog="mk-bug.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    ls = sub.add_parser("list")
    ls.add_argument("--status"), ls.add_argument("--kind")
    sub.add_parser("show").add_argument("id")
    _meta_flags(sub.add_parser("new"), title_required=True)
    st = sub.add_parser("set")
    st.add_argument("id"), st.add_argument("--status"), st.add_argument("--assignee")
    _meta_flags(st)
    cl = sub.add_parser("close")
    cl.add_argument("id"), cl.add_argument("--wontfix", action="store_true")
    sub.add_parser("reopen").add_argument("id")
    sub.add_parser("delete").add_argument("id")
    for name in ("task", "subtask"):
        verbs = sub.add_parser(name).add_subparsers(dest="verb")
        ad = verbs.add_parser("add")
        ad.add_argument("id"), ad.add_argument("--title", required=True), ad.add_argument("--acceptance")
        if name == "task":
            ts = verbs.add_parser("set")
            ts.add_argument("id"), ts.add_argument("--status"), ts.add_argument("--title")
            ts.add_argument("--acceptance")
    sub.add_parser("check").add_argument("file")
    im = sub.add_parser("import")
    im.add_argument("file"), im.add_argument("--dry-run", action="store_true")
    sub.add_parser("drafts")
    dr = sub.add_parser("draft")
    _meta_flags(dr, title_required=True)
    dr.add_argument("--tasks")
    dp = sub.add_parser("dispatch")
    dp.add_argument("id"), dp.add_argument("--route"), dp.add_argument("--owned")
    a = p.parse_args(rest)
    if a.cmd is None or (a.cmd in ("task", "subtask") and a.verb is None):
        raise Fail("usage: mk-bug.py list|show|new|set|close|reopen|delete|task add|task set|subtask add"
                   "|check|import|drafts|draft|dispatch  (or --self-test)")
    if a.cmd == "check":  # a file check needs no project
        return cmd_check(a.file)
    ctx = Ctx(known.home, known.project, known.data, known.by)
    if a.cmd == "import":
        return cmd_import(ctx, a.file, a.dry_run)
    if a.cmd == "drafts":
        return cmd_drafts(ctx)
    if a.cmd == "draft":
        return cmd_draft(ctx, a)
    if a.cmd == "list":
        return cmd_list(ctx, a)
    if a.cmd == "show":
        return cmd_show(ctx, a.id)
    if a.cmd == "new":
        a.assignee = None
        return cmd_new(ctx, a)
    if a.cmd == "set":
        return cmd_set(ctx, a)
    if a.cmd == "close":
        return cmd_close(ctx, a.id, a.wontfix)
    if a.cmd == "reopen":
        return cmd_reopen(ctx, a.id)
    if a.cmd == "delete":
        return cmd_delete(ctx, a.id)
    if a.cmd == "dispatch":
        return cmd_dispatch(ctx, a)
    if a.verb == "add":
        return cmd_add(ctx, a.id, a.title, a.acceptance, a.cmd == "subtask")
    return cmd_task_set(ctx, a)


def main(argv):
    out = _cli(argv)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1  # `check` of a draft with errors


# --- self-test ----------------------------------------------------------------

def _check(cond, msg):
    if not cond:
        raise SystemExit(f"SELF-TEST FAIL: {msg}")


def _sql(home, data, sql, args=()):
    con = mkdb.open_project(home, None, data_dir=data)
    try:
        con.execute(sql, args)
        con.commit()
    finally:
        con.close()


def _patched(name, fn, obj=None):
    """Swap one module global (or attribute of obj) for fn; -> the restore callable."""
    target = obj if obj is not None else sys.modules[__name__]
    old = getattr(target, name)
    setattr(target, name, fn)
    return lambda: setattr(target, name, old)


def _review_fixes(home, data, run, refused):
    """The reviewer's fixes, one case each (each fails on the code before the fix)."""
    g = sys.modules[__name__]
    ctx = Ctx(home, None, data, None)
    # 3: the verdict rule of plankanban.tsx verdictNeedsChanges (case-insensitive "approve" prefix)
    _check(word_of("building", verdict="Approved") == "fixing" and word_of("building", verdict="APPROVE") == "fixing"
           and word_of("building", verdict="Changes") == "needs" and word_of("building", verdict="") == "fixing",
           "verdict rule")
    # 5: set --status=<word> the bug already shows is a no-op (routed is open: no move to planned)
    bid = run("new", "--title=five")["bug"]["id"]
    ctx.state("--run", BUGS, "task", "set", bid, "status=routed")
    n = len(mkdb.events(home, None, BUGS, data_dir=data, task=bid))
    got = run("set", bid, "--status=open")["bug"]
    _check(got["task_status"] == "routed" and len(mkdb.events(home, None, BUGS, data_dir=data, task=bid)) == n,
           f"set --status=open on an open bug moved it: {got}")
    # 1: a set landing between another set's status change and its metadata write is not lost
    orig = g._set_status

    def racing(c, tid, status):
        orig(c, tid, status)
        undo()
        run("set", tid, "--severity=low")
    undo = _patched("_set_status", racing)
    try:
        got = run("set", bid, "--status=fixing", "--area=ui")["bug"]
    finally:
        undo()
    _check(got["severity"] == "low" and got["area"] == "ui" and got["status"] == "fixing", f"lost update {got}")
    # 2: a task on a wontfix bug reopens it and drops wontfix; done later is fixed, not wontfix
    run("close", bid, "--wontfix")
    tid = run("task", "add", bid, "--title=more")["task"]["id"]
    got = run("show", bid)["bug"]
    _check(got["task_status"] != "done" and got["wontfix"] is False, f"task add kept wontfix: {got}")
    run("task", "set", tid, "--status=done")
    got = run("show", bid)["bug"]
    _check(got["status"] != "wontfix" and got["wontfix"] is False, f"stale wontfix after done: {got}")
    # 7: direct title/acceptance writes log a decision event with old and new value
    run("task", "set", tid, "--title=renamed", "--acceptance=acc2")
    run("set", bid, "--title=five renamed")
    texts = [e["text"] for e in run("show", bid)["events"] if e["kind"] == "decision"]
    _check(any("title: more → renamed" in t for t in texts) and any("acceptance:" in t and "acc2" in t for t in texts)
           and any("title: five → five renamed" in t for t in texts), f"no decision events: {texts}")
    # 9: task add retries when a concurrent add took the child id first
    calls = []
    orig_next = g._next_id

    def clash(c, parent=None):
        calls.append(parent)
        return tid if parent == bid and len(calls) == 1 else orig_next(c, parent)
    undo = _patched("_next_id", clash)
    try:
        got = run("task", "add", bid, "--title=second")["task"]
    finally:
        undo()
    _check(got["id"] == f"{bid}.2" and len(calls) == 2, f"no retry on a child-id clash: {got} {calls}")
    # 8: show reads the bug's own events by SQL, newest 50, never the run's whole history
    _sql(home, data, "INSERT INTO events(run, task, t, chan, kind, text) VALUES " +
         ",".join(["('bugs', ?, 't', 'state', 'note', 'n')"] * 60), [tid] * 60)

    def whole(*a, **k):
        raise AssertionError("show read the whole run history")
    undo = _patched("events", whole, mkdb)
    try:
        evs = run("show", bid)["events"]
    finally:
        undo()
    _check(len(evs) == 50 and [e["id"] for e in evs] == sorted((e["id"] for e in evs), reverse=True)
           and all(e["task"] in (bid, tid, f"{bid}.2") for e in evs), f"show events {len(evs)}")
    # 6: delete drops the bug's rows in every task-keyed table ...
    d6 = run("new", "--title=six")["bug"]["id"]
    t6 = run("task", "add", d6, "--title=t")["task"]["id"]
    for sql in ("INSERT INTO artifacts(run, task, kind, text) VALUES ('bugs', ?, 'brief', 'x')",
                "INSERT INTO evidence(run, task, line) VALUES ('bugs', ?, 'x')",
                "INSERT INTO risk_reasons(run, task, kind) VALUES ('bugs', ?, 'x')"):
        _sql(home, data, sql, (t6,))
    run("delete", d6)
    con = mkdb._open_read(mkdb._store_path(home, data_dir=data), mkdb.SCHEMA)
    left = {t: con.execute(f"SELECT count(*) FROM {t} WHERE run='bugs' AND task LIKE ?", (d6 + "%",)).fetchone()[0]
            for t in ("artifacts", "evidence", "risk_reasons")}
    con.close()
    _check(not any(left.values()), f"delete left rows: {left}")
    # ... and checks the lane inside its locked transaction (a lane set just before the lock stops it)
    d7 = run("new", "--title=seven")["bug"]["id"]
    t7 = run("task", "add", d7, "--title=t")["task"]["id"]
    store_cls = mk_state_mod().Store
    orig_locked = store_cls.locked

    def lane_first(self, *a, **k):
        undo()
        _sql(home, data, "UPDATE tasks SET lane='lane-race' WHERE run='bugs' AND id=?", (t7,))
        return orig_locked(self, *a, **k)
    undo = _patched("locked", lane_first, store_cls)
    try:
        refused("delete", d7, want="lane")
    finally:
        undo()
    # 4: an unexpected exception is still one JSON line, exit 1
    bad = os.path.join(os.path.dirname(data), "corrupt")
    os.makedirs(bad)
    with open(mkpaths.new_db_in(bad, "project"), "wb") as fh:
        fh.write(b"not a store" * 100)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--home", home, "--data", bad, "list"],
                       capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        out = {}
    _check(r.returncode == 1 and out.get("ok") is False and out.get("error", "").startswith("DatabaseError: "),
           f"unexpected error not JSON: {r.returncode} {r.stdout!r} {r.stderr[-300:]!r}")


def _drafts(home, data, run, refused):
    """Phase DRAFT: check, draft, drafts, import [--dry-run]."""
    tmpl = os.path.join(os.path.dirname(HERE), "skills", "matrixkoder", "templates", "bug.md")
    got = cmd_check(tmpl)
    _check(got == {"ok": True, "errors": [], "warnings": []}, f"the template does not check clean: {got}")
    tmp = os.path.dirname(data)

    def chk(text):
        p = os.path.join(tmp, "c.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return run("check", p)
    got = chk('+++\nkind = "gui"\nseverity = "urgent"\nbogus = 1\n[[task]]\nacceptance = "a"\n'
              '[[task.subtask]]\ntitle = ""\n+++\n')
    _check(got["ok"] is False and {"missing title", "task 1 without title", "task 1 subtask 1 without title"}
           <= set(got["errors"]) and any("unknown kind" in e for e in got["errors"])
           and any("unknown severity" in e for e in got["errors"])
           and any("bogus" in w for w in got["warnings"]), f"check errors {got}")
    _check(chk("no header\n")["ok"] is False and chk('+++\ntitle = "x\n+++\n')["ok"] is False, "bad header")
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "check", os.path.join(tmp, "c.md")],
                       capture_output=True, text=True, timeout=60)
    _check(r.returncode == 1 and json.loads(r.stdout)["ok"] is False, f"check with errors exit {r.returncode}")
    # draft: a file in the bugs folder from flags; a second one with the same title gets -2
    folder = mkpaths.bugs_dir(data)
    d1 = run("draft", "--title=Crash on X!", "--kind=cli", "--severity=high", "--description=d1\nd2",
             "--labels=a,b", "--steps=s", "--tasks=fix it\n\ntest \"it\"")
    d2 = run("draft", "--title=Crash on X!")
    _check(d1 == {"ok": True, "path": os.path.join(folder, "crash-on-x.md")}
           and d2["path"] == os.path.join(folder, "crash-on-x-2.md"), f"draft {d1} {d2}")
    _check(run("check", d1["path"])["ok"], "a saved draft does not check")
    with open(os.path.join(folder, "broken.md"), "w", encoding="utf-8") as fh:
        fh.write("+++\nkind = 1\n+++\n")
    ls = run("drafts")
    _check([d["file"] for d in ls["drafts"]] == ["broken.md", "crash-on-x-2.md", "crash-on-x.md"]
           and ls["drafts"][2]["title"] == "Crash on X!" and ls["drafts"][2]["check"]["ok"]
           and ls["drafts"][0]["check"]["ok"] is False and ls["folder"] == folder, f"drafts {ls}")
    # import --dry-run writes nothing
    before = run("list")["counts"]
    dry = run("import", d1["path"], "--dry-run")
    bid = dry["would"]["bug"]["id"]
    _check(dry["dry_run"] and [t["title"] for t in dry["would"]["tasks"]] == ["fix it", 'test "it"']
           and dry["would"]["bug"]["labels"] == ["a", "b"] and os.path.isfile(d1["path"])
           and run("list")["counts"] == before and not os.path.exists(mkpaths.bugs_archive_dir(data)), f"dry {dry}")
    # import: the bug, its tasks, then the file moves to bugs/archive/
    im = run("import", d1["path"])
    b = im["bug"]
    _check(b["id"] == bid and b["title"] == "Crash on X!" and b["kind"] == "cli" and b["severity"] == "high"
           and b["description"] == "d1\nd2" and b["steps"] == "s" and b["labels"] == ["a", "b"]
           and im["tasks"] == [f"{bid}.1", f"{bid}.2"] and b["tasks"]["total"] == 2, f"import {im}")
    _check(im["archived"] == os.path.join(mkpaths.bugs_archive_dir(data), f"{bid}-crash-on-x.md")
           and os.path.isfile(im["archived"]) and not os.path.exists(d1["path"]), f"archive {im}")
    # the template (a task with a subtask) imports as written; a draft with errors creates nothing
    shutil.copy(tmpl, os.path.join(folder, "tmpl.md"))
    im = run("import", os.path.join(folder, "tmpl.md"))
    t = run("show", im["bug"]["id"])["plan"]["phases"][0]["tasks"]
    _check(len(im["tasks"]) == 2 and im["tasks"][1].count(".") == 2 and t[0]["subtasks"][0]["id"] == im["tasks"][1]
           and im["bug"]["expected"] == "A check error naming the missing header.", f"template import {im}")
    before = run("list")["counts"]
    refused("import", os.path.join(folder, "broken.md"), want="does not check")
    _check(run("list")["counts"] == before and os.path.isfile(os.path.join(folder, "broken.md")), "bad import wrote")
    _check([d["file"] for d in run("drafts")["drafts"]] == ["broken.md", "crash-on-x-2.md"], "drafts after import")


def _import_fixes(home, data, run):
    """The import/draft review fixes, one case each (each fails on the code before the fix)."""
    g = sys.modules[__name__]
    folder, tmp, archive = mkpaths.bugs_dir(data), os.path.dirname(data), mkpaths.bugs_archive_dir(data)

    def put(name, title, where=folder, body="## Description\n\nd\n"):
        p = os.path.join(where, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f'+++\ntitle = "{title}"\n\n[[task]]\ntitle = "a"\nacceptance = "x"\n\n'
                     f'[[task]]\ntitle = "b"\nacceptance = "y"\n+++\n\n{body}')
        return p

    def err(*args):
        try:
            got = run(*args)
        except (Fail, mkdb.Fail, mkpaths.Fail) as e:
            return str(e)
        raise SystemExit(f"SELF-TEST FAIL: {args} accepted: {got}")
    # 1 + 6: a failure after the bug row exists (Fail, mkdb.Fail, OSError) keeps the "created"
    # context, rolls the bug back and keeps the draft
    for i, exc in enumerate((Fail("boom"), mkdb.Fail("store boom"), OSError("disk boom"))):
        p = put(f"roll{i}.md", f"roll {i}")
        before, calls, orig = run("list")["counts"], [], g.cmd_add

        def second_fails(*a, **k):
            if calls:
                raise exc
            calls.append(1)
            return orig(*a, **k)
        undo = _patched("cmd_add", second_fails)
        try:
            msg = err("import", p)
        finally:
            undo()
        _check("created with tasks" in msg and "rolled back" in msg and run("list")["counts"] == before
               and os.path.isfile(p), f"no rollback on {type(exc).__name__}: {msg}")
    # 1: an unlink failing after a verified copy is reported; draft_sha (kept by a later set) refuses
    # a re-import, naming the bug
    p = put("dup.md", "dup")
    orig_unlink = os.unlink

    def stuck(q, *a, **k):
        if q == p:
            raise PermissionError(13, "Permission denied", q)
        return orig_unlink(q, *a, **k)
    undo = _patched("unlink", stuck, os)
    try:
        im = run("import", p)
    finally:
        undo()
    bid = im["bug"]["id"]
    _check(im["archived"] == os.path.join(archive, f"{bid}-dup.md") and os.path.isfile(im["archived"])
           and os.path.isfile(p) and "could not be removed" in im.get("note", "") and im["kept_source"] is False,
           f"unlink failure {im}")
    run("set", bid, "--area=x")
    msg = err("import", p)
    _check(f"already imported as bug {bid}" in msg, f"re-import {msg}")
    # 2: a file outside the bugs folder is copied, never removed (the shipped template above all)
    out_p = put("outside.md", "outside", where=tmp)
    im = run("import", out_p)
    _check(im["kept_source"] is True and os.path.isfile(out_p) and os.path.isfile(im["archived"]), f"outside {im}")
    tmpl = os.path.join(os.path.dirname(HERE), "skills", "matrixkoder", "templates", "bug.md")
    _check("already imported" in err("import", tmpl) and os.path.isfile(tmpl), "template import")
    # 5: an archive name taken by a symlink (here: after any exists-check) is never written through
    p = put("link.md", "link")
    target = os.path.join(tmp, "target")
    nxt = run("import", p, "--dry-run")["would"]["archive"]
    os.symlink(target, nxt)
    orig_lexists = os.path.lexists
    undo = _patched("lexists", lambda q: False if q == nxt else orig_lexists(q), os.path)
    try:
        msg = err("import", p)
    finally:
        undo()
    _check("rolled back" in msg and not os.path.exists(target) and os.path.islink(nxt) and os.path.isfile(p), msg)
    os.unlink(nxt)
    # 3: draft refuses a `## ` line in section text; check warns on a `##` text line and a repeated section
    _check("'## '" in err("draft", "--title=heads", "--description=a\n## Steps\nb")
           and not os.path.exists(os.path.join(folder, "heads.md")), "draft with a ## line")
    got = run("check", put("h.md", "h", where=tmp, body="## Description\n\na\n## \nb\n\n## Steps\n\ns\n## steps\nt\n"))
    _check(any("is text, not a heading" in w for w in got["warnings"])
           and any("appears twice" in w for w in got["warnings"]), f"check ## warnings {got}")
    # 4: a text that cannot be encoded leaves no 0-byte draft
    _check("UTF-8" in err("draft", "--title=zz \udcff") and not os.path.exists(os.path.join(folder, "zz.md")),
           "0-byte draft")
    # 7: an mk-state refusal that merely contains "exists" is not an id clash: no retry, its own text
    orig_state = Ctx.state

    def odd(self, *args):
        if "add" in args:
            raise Fail("store file exists but is read-only")
        return orig_state(self, *args)
    undo = _patched("state", odd, Ctx)
    try:
        msg = err("new", "--title=odd")
    finally:
        undo()
    _check(msg == "store file exists but is read-only", f"retry on a non-clash: {msg}")


def _dispatch(home, data, run, refused):
    """Phase LANE: dispatch refusals, a good dispatch on a temp git repo (MK_HERDR=none, as
    mk-lane.py's own self-test), the brief it writes, and a failure after round/brief reported."""
    tmp = os.path.dirname(data)
    repo, other = os.path.join(tmp, "repo"), os.path.join(tmp, "notgit")
    os.makedirs(repo), os.makedirs(other)

    def g(*args):
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=60)
        _check(r.returncode == 0, f"git {args}: {r.stderr}")
    g("init", "-q", "-b", "main"), g("config", "user.email", "t@t"), g("config", "user.name", "t")
    with open(os.path.join(repo, "README.md"), "w") as fh:
        fh.write("x\n")
    g("add", "-A"), g("commit", "-qm", "base"), g("branch", "dev")  # dev: the config's integration branch

    def point(path):  # the registered project's repo, which dispatch runs mk-lane.py acquire from
        root = mkdb.open_root(home)
        try:
            root.execute("UPDATE projects SET repo=? WHERE slug='bugdemo'", (path,))
            root.commit()
        finally:
            root.close()
    point(repo)
    bid = run("new", "--title=lane bug", "--kind=cli", "--description=crash on <x>", "--steps=run it")["bug"]["id"]
    bare = run("task", "add", bid, "--title=no acceptance")["task"]["id"]
    good = run("task", "add", bid, "--title=fix the crash", "--acceptance=no crash\nexit 0")["task"]["id"]
    late = run("task", "add", bid, "--title=second", "--acceptance=works")["task"]["id"]
    # LANE.1.a2: refusals, nothing written; mk-state's own task check refuses the same task
    refused("dispatch", bid, want="bug row")
    refused("dispatch", bare, want="no acceptance")
    def chk(tid, *flags):
        r = subprocess.run([sys.executable, os.path.join(HERE, "mk-state.py"), "--data", data, "--run", BUGS,
                            "task", "check", tid, *flags], capture_output=True, text=True, timeout=120)
        return json.loads(r.stdout.strip().splitlines()[-1])
    got = chk(bare)
    _check(got["ok"] is False and got["type"] == "task" and "acceptance" in got["missing"], f"task check {got}")
    refused("dispatch", good, want="owns no files")
    refused("dispatch", good, "--owned=a.py", "--route=claude", want="--route")
    _check(_task_obj(Ctx(home, None, data, None), good)["lane"] is None
           and not os.path.exists(mkpaths.task_dir(data, BUGS, good)), "a refused dispatch wrote")
    # LANE.1.a1: round, brief, acquire; lane/branch/slot on the row; next = arm + dispatch
    got = run("dispatch", good, "--owned=src/x.py,tests/x_test.py", "--route=claude opus high")
    lane = got["lane"]
    _check(set(got) == {"ok", "task", "lane", "branch", "brief", "next"} and got["ok"]
           and got["branch"] == f"mk/{BUGS}/{good.lower()}" and lane and os.path.isdir(lane)
           and str(got["task"]["slot"]) == "1" and got["task"]["lane"] == lane and got["task"]["round"] == 1
           and got["brief"] == os.path.join(mkpaths.task_dir(data, BUGS, good), "brief-r1.md"), f"dispatch {got}")
    arm, disp = got["next"]
    _check("mk-lane-event.py arm" in arm and f"--task {good}" in arm and "--kind claude" in arm
           and "--run bugs" in arm and lane in arm, f"next arm {arm}")
    _check("mk-lane-event.py dispatch --connect --pane <pane>" in disp and got["brief"] in disp
           and f"{good} R1 DONE" in disp and "--agent claude --model opus --effort high" in disp, f"next {disp}")
    with open(got["brief"], encoding="utf-8") as fh:
        text = fh.read()
    spec = importlib.util.spec_from_file_location("brief_check", os.path.join(HERE, "brief-check.py"))
    bc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bc)
    rules = [x for x in bc.rules_block(text) if x.strip()]
    _check(bc.placeholder_failures(text) == [] and len(rules) <= 4 and "mk-builder" in rules[0]
           and "round R1" in rules[0] and f"`{good} R1 DONE`" in rules[0], f"brief-check: {text}")
    _check(text.startswith(f"# {good} — fix the crash\n") and "Plan `" not in text and f"Bug `{bid}`" in text
           and "Acceptance: no crash" in text and "Acceptance: exit 0" in text and "crash on <x>" in text
           and "src/x.py" in text and "## TAKEOVER" not in text and "## FIX" not in text, f"brief text {text}")
    bc_cli = subprocess.run([sys.executable, os.path.join(HERE, "brief-check.py"), got["brief"]],
                            capture_output=True, text=True, timeout=60)
    _check(bc_cli.returncode == 0 and "brief-check ok" in bc_cli.stdout, f"brief-check CLI {bc_cli.stdout}")
    at = chk(good, "--at-dispatch")
    _check(at["ok"] and at["type"] == "task", f"task check --at-dispatch {at}")
    refused("dispatch", good, want="already has a lane")
    # a failure after round/brief names what was done (no silent half-state)
    point(other)
    try:
        run("dispatch", late, "--owned=b.py")
        raise SystemExit("SELF-TEST FAIL: dispatch on a non-git repo succeeded")
    except Fail as e:
        msg = str(e)
    point(repo)
    _check("done: task set owned=b.py; task round; task brief; task check; then lane acquire failed" in msg, msg)
    # that half-dispatched task (round 1, no lane) is refused, never a round 2
    refused("dispatch", late, "--owned=b.py", want=f"task {late} is at round 1 with no lane")
    # a step after the round that raises a non-Fail (a missing brief template: OSError) still names what was done
    drift = run("task", "add", bid, "--title=third", "--acceptance=ok")["task"]["id"]
    saved = globals()["BRIEF_TMPL"]
    globals()["BRIEF_TMPL"] = os.path.join(tmp, "no-such-brief.md")
    try:
        run("dispatch", drift, "--owned=c.py")
        raise SystemExit("SELF-TEST FAIL: dispatch without a brief template succeeded")
    except Fail as e:
        msg = str(e)
    finally:
        globals()["BRIEF_TMPL"] = saved
    _check("done: task set owned=c.py; task round; then brief text failed" in msg and "no-such-brief" in msg, msg)
    # a done task, or a task of a fixed / wontfix bug, is refused
    b2 = run("new", "--title=closed bug", "--kind=cli")["bug"]["id"]
    t2 = run("task", "add", b2, "--title=t", "--acceptance=ok")["task"]["id"]
    run("close", b2)
    refused("dispatch", t2, "--owned=d.py", want=f"bug {b2} is fixed")
    run("close", b2, "--wontfix")
    refused("dispatch", t2, "--owned=d.py", want=f"bug {b2} is wontfix")
    run("reopen", b2)
    run("task", "set", t2, "--status=done")
    refused("dispatch", t2, "--owned=d.py", want=f"task {t2} is done")


def self_test():
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home")
        saved = {k: os.environ.get(k) for k in ("MK_HOME", "MATRIXKODER_HOME", "MK_SELFTEST", "MATRIXKODER_DATA",
                                                "MK_HERDR", "MK_OMP_USER_SKILLS")}
        # dispatch's mk-lane.py acquire: no herdr, and a temp stand-in for the user-level omp skills
        os.environ.update(MK_HOME=home, MATRIXKODER_HOME=home, MK_SELFTEST="1", MK_HERDR="none",
                          MK_OMP_USER_SKILLS=os.path.join(tmp, "omp-skills"))
        os.environ.pop("MATRIXKODER_DATA", None)
        data, bare = os.path.join(tmp, "proj"), os.path.join(tmp, "bare")
        os.makedirs(data), os.makedirs(bare)
        root = mkdb.open_root(home)
        try:  # a registered project, so mk-state and this script key the overview row on one slug
            root.execute("INSERT INTO projects(slug, name, repo, data_dir, mode) VALUES (?,?,?,?,?)",
                         ("bugdemo", "bugdemo", tmp, os.path.realpath(data), "home"))
            root.commit()
        finally:
            root.close()

        def run(*args, at=data):
            return _cli(["--home", home, "--data", at, *args])

        def refused(*args, want=""):
            try:
                got = run(*args)
            except (Fail, mkdb.Fail) as e:
                _check(want in str(e), f"{args}: wrong error {e}")
                return
            raise SystemExit(f"SELF-TEST FAIL: {args} accepted: {got}")

        cfg = subprocess.run([sys.executable, os.path.join(HERE, "mk-config.py"), "set",
                              "repo.integration_branch", '"dev"', "--data", data],
                             capture_output=True, text=True, timeout=60)
        _check(cfg.returncode == 0, f"mk-config set: {cfg.stdout} {cfg.stderr}")
        # BRUN.2.a1/a2/a3: new, ids, verbatim text, metadata
        b1 = run("new", "--title=[ui] -x crash", "--kind=cli", "--severity=high", "--labels=a, b,",
                 "--description=line1\nline2", "--found-in=v1")["bug"]
        _check(b1["id"] == "B1" and b1["title"] == "[ui] -x crash" and b1["status"] == "open"
               and b1["task_status"] == "planned" and b1["labels"] == ["a", "b"] and b1["kind"] == "cli"
               and b1["description"] == "line1\nline2" and b1["wontfix"] is False, f"new {b1}")
        con = mkdb._open_read(mkdb._store_path(home, data_dir=data), mkdb.SCHEMA)
        run_row = con.execute("SELECT branch, status, plan FROM runs WHERE id='bugs'").fetchone()
        brow = con.execute("SELECT type, parent FROM tasks WHERE run='bugs' AND id='B1'").fetchone()
        con.close()
        _check(tuple(run_row) == ("dev", "active", None), f"run bugs from config: {tuple(run_row)}")
        _check(tuple(brow) == ("phase", None), f"bug row shape {tuple(brow)}")
        b2 = run("new", "--title=two")
        _check(b2["bug"]["id"] == "B2" and b2["bug"]["kind"] == "other" and b2["bug"]["severity"] == "med"
               and "note" not in b2, f"defaults {b2}")
        fb = run("new", "--title=elsewhere", at=bare)
        _check(fb["bug"]["id"] == "B1" and "main" in fb.get("note", ""), f"fallback note {fb}")
        refused("new", "--title=x", "--kind=gui", want="unknown kind")
        refused("new", "--title=x", "--severity=urgent", want="unknown severity")
        refused("new", "--title=  ", want="non-empty")
        refused("new", "--kind=cli", want="--title")
        refused("set", "B1", "--status=closed", want="unknown status")
        refused("show", "X1", want="bad bug id")
        refused("show", "B9", want="no bug")
        refused("list", "--bogus", want="unrecognized")
        refused("task", "add", "B1.1", "--title=t", want="bad bug id")
        # tasks and subtasks
        t1 = run("task", "add", "B1", "--title=fix it", "--acceptance=works")["task"]
        t2 = run("task", "add", "B1", "--title=test it")["task"]
        s1 = run("subtask", "add", "B1.1", "--title=part")["task"]
        _check((t1["id"], t1["type"], t1["parent"], t1["acceptance"]) == ("B1.1", "task", "B1", "works"), t1)
        _check(t2["id"] == "B1.2" and (s1["id"], s1["type"], s1["parent"]) == ("B1.1.1", "subtask", "B1.1"),
               f"ids {t2} {s1}")
        # status mapping both ways
        for s in mk_state_mod().STATUSES + ["ready", "takeover"]:
            _check(word_of(s) in WORDS and (s not in WORD_OF or word_of(s) == WORD_OF[s]), f"word_of {s}")
        _check(word_of("done", True) == "wontfix" and word_of("merged", True) == "fixed"
               and word_of("building", verdict="changes") == "needs", "word_of flags")
        for w in WORDS:
            got = run("set", "B2", f"--status={w}")["bug"]
            _check(got["status"] == w and got["task_status"] == STATUS_OF[w], f"set {w}: {got}")
        # BRUN.2.a4: one state-channel transition event per status change, run bugs
        before = [e for e in mkdb.events(home, None, BUGS, data_dir=data) if e["task"] == "B2"]
        run("set", "B2", "--status=needs")
        after = [e for e in mkdb.events(home, None, BUGS, data_dir=data) if e["task"] == "B2"]
        new = [e for e in after if e["id"] not in {x["id"] for x in before}]
        _check([(e["chan"], e["kind"], e["to_id"]) for e in new] == [("state", "transition", "blocked")],
               f"one transition event: {new}")
        # a status on a bug with tasks fans out and rolls back up
        got = run("set", "B1", "--status=fixing", "--area=parser", "--assignee=rohit")["bug"]
        _check(got["status"] == "fixing" and got["task_status"] == "building" and got["area"] == "parser"
               and got["assignee"] == "rohit", f"fan-out {got}")
        # metadata survives a later mk-state write on the same row
        Ctx(home, None, data, None).state("--run", BUGS, "task", "set", "B1", "verdict=approve")
        con = mkdb._open_read(mkdb._store_path(home, data_dir=data), mkdb.SCHEMA)
        kept = _extra(con.execute("SELECT extra_json FROM tasks WHERE run='bugs' AND id='B1'").fetchone()[0])
        con.close()
        _check(kept.get("bug", {}).get("area") == "parser" and kept["bug"].get("kind") == "cli",
               f"mk-state write dropped extra_json.bug: {kept}")
        # close --wontfix / reopen
        got = run("close", "B2", "--wontfix")["bug"]
        _check((got["status"], got["task_status"], got["wontfix"]) == ("wontfix", "done", True), f"wontfix {got}")
        got = run("reopen", "B2")["bug"]
        _check((got["status"], got["task_status"], got["wontfix"]) == ("open", "planned", False), f"reopen {got}")
        got = run("task", "set", "B1.2", "--status=done", "--title={odd} title")["task"]
        _check(got["status"] == "done" and got["title"] == "{odd} title", f"task set {got}")
        # BRUN.2.a5: show in the `plans <id>` shape, events newest first
        sh = run("show", "B1")
        ph = sh["plan"]["phases"]
        _check(sh["plan"]["id"] == "B1" and len(ph) == 1 and ph[0]["id"] == "B1"
               and {"id", "title", "status", "tasks", "wave", "deps"} <= set(ph[0]), f"show phase {ph}")
        node = ph[0]["tasks"][0]
        _check({"id", "title", "status", "parent", "type", "round", "verdict", "subtasks", "slot", "pane",
                "branch", "updated", "acceptance", "owned", "deps", "route"} <= set(node)
               and node["subtasks"][0]["id"] == "B1.1.1" and [t["id"] for t in ph[0]["tasks"]] == ["B1.1", "B1.2"],
               f"show task node {node}")
        ids = [e["id"] for e in sh["events"]]
        _check(ids == sorted(ids, reverse=True) and len(ids) <= 50
               and any(e["kind"] == "transition" and e["to"] == "building" for e in sh["events"])
               and all(e["task"] == "B1" or e["task"].startswith("B1.") for e in sh["events"])
               and all(set(e) == {"id", "t", "kind", "task", "to", "text", "by"} for e in sh["events"])
               and any(e["by"] == "mk-bug" for e in sh["events"]), f"show events {sh['events']}")
        # delete: refused with a lane, then an id is never reused
        Ctx(home, None, data, None).state("--run", BUGS, "task", "set", "B1.1", "lane=lane-1")
        refused("delete", "B1", want="lane")
        b3 = run("new", "--title=three", "--kind=docs", "--severity=low")["bug"]
        _check(run("delete", "B3") == {"ok": True, "deleted": "B3"}, "delete B3")
        refused("show", "B3", want="no bug")
        _check(run("new", "--title=four")["bug"]["id"] == "B4", f"id reused after delete of {b3['id']}")
        # list: order, counts, filters, --project resolves the same store
        ls = run("list")
        _check(ls["project"] == "bugdemo" and [b["id"] for b in ls["bugs"]] == ["B1", "B4", "B2"]
               and ls["counts"] == {"open": 2, "fixing": 1, "needs": 0, "fixed": 0, "wontfix": 0}
               and ls["bugs"][0]["tasks"] == {"done": 1, "total": 3} and ls["bugs"][0]["lane"] == "lane-1",
               f"list {ls}")
        _check([b["id"] for b in run("list", "--kind=cli")["bugs"]] == ["B1"], "list --kind")
        _check([b["id"] for b in run("list", "--status=open")["bugs"]] == ["B4", "B2"], "list --status")
        refused("list", "--kind=gui", want="unknown kind")
        # --project names a migrated project's C2 folder, as in mkdb.py
        mig = mkpaths.target_project_dir("mig", home)
        root = mkdb.open_root(home)
        try:
            root.execute("INSERT INTO projects(slug, name, repo, data_dir, mode, db_path) VALUES (?,?,?,?,?,?)",
                         ("mig", "mig", tmp, os.path.join(tmp, "old"), "home", mkpaths.project_db_in(mig)))
            root.commit()
        finally:
            root.close()
        _check(_cli(["--home", home, "--project", "mig", "new", "--title=m"])["bug"]["id"] == "B1", "--project new")
        by_slug = _cli(["--home", home, "--project", "mig", "list"])
        _check(by_slug["project"] == "mig" and [b["id"] for b in by_slug["bugs"]] == ["B1"]
               and os.path.isfile(mkpaths.project_db_in(mig)), f"--project {by_slug}")
        # BRUN.3.a1/a2: one overview row, counts per status of the bugs, no bug text in the root
        root = mkdb._open_read(mkpaths.root_db(home), mkdb.ROOT_SCHEMA)
        rows = [dict(r) for r in root.execute("SELECT * FROM plan_summary WHERE project='bugdemo'")]
        dump = "\n".join(str(tuple(r)) for t in ("plan_summary", "projects")
                         for r in root.execute(f"SELECT * FROM {t}"))
        root.close()
        _check(len(rows) == 1 and rows[0]["plan"] == "bugs" and rows[0]["live_run"] == "bugs"
               and json.loads(rows[0]["counts_json"]) == {"building": 1, "planned": 2}, f"overview {rows}")
        _check("crash" not in dump and "parser" not in dump and "B1" not in dump, f"bug text in root: {dump}")
        _review_fixes(home, data, run, refused)
        _drafts(home, data, run, refused)
        _import_fixes(home, data, run)
        _dispatch(home, data, run, refused)
        mkdb.selftest_guard_probe(home)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print(json.dumps({"ok": True, "self_test": "pass"}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(self_test() if sys.argv[1:] == ["--self-test"] else main(sys.argv[1:]))
    except (Fail, mkdb.Fail, mkpaths.Fail) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:  # anything else keeps the one-JSON-line contract too
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        sys.exit(1)
