#!/usr/bin/env python3
"""MatrixKoder Inbox — list, import, archive, restore and delete the plan files waiting in
<home>/inbox/ (plan inbox-and-parsing, phase INBOX; contracts C1/C2/C3). Python 3 stdlib only.

  list --json [--home DIR] [--data DIR]
                         one JSON line: {ok, files:[{id, path, title, fit, sections_found,
                         sections_total, missing, age_s, archived, note}], counts:{inbox, template,
                         fix, archived}}. Never writes or moves a plan file. The class of each file is
                         `mk-plan.py check --classify` (a subprocess, the C2 reply); nothing is parsed
                         twice. It records inbox events (below) in the project store, best effort.
  import <id>            mk-plan.py import into the project store, in three reported steps; a flat
                         <topic>.md then moves to inbox/archive/ (a plan folder moves itself)
  archive <id>           inbox/ -> inbox/archive/ (refused when the target exists)
  restore <id>           inbox/archive/ -> inbox/ (refused when the target exists)
  delete <id> [--archived]
                         copy to inbox/.deleted/<id>-<timestamp>/, verify the copy, then remove

The standard is one flat <inbox>/<topic>.md per plan; a folder holding plan.md (the form mk-plan.py
imports) is still listed and acted on. A plan's id is the `plan = "..."` of its header (a file with
no header falls back to its file name without .md). `path` is the base name (`<folder>/plan.md` for
a folder), never an absolute path.

Every action is recorded as an event on the project store's inbox channel (C5, run NULL):
found, checked, import-step, imported, import-failed, archived, restored, deleted, each with
meta {file, plan, fit, by}; `by` is --by (default user). list writes `found` once per file it has
never seen (same file name and plan id) and `checked` only when a file's class (fit, sections found,
missing names) differs from its last `checked` event, so repeated list calls add nothing. A failed
event write never fails the action: the reply carries a `warnings` entry instead.

Every command prints ONE final JSON line, {"ok": true, ...} exit 0 or {"ok": false, "errors": [...]}
exit 2, like mk-plan.py. `import` prints one {"progress": ...} line per step before it. The script
never prompts: the dashboard confirms a delete with the user. Delete refuses a symlink and any path
that resolves outside the inbox.
"""
import argparse, hashlib, importlib.util, json, os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mkpaths = _load("mkpaths", "mkpaths.py")

ARCHIVE, DELETED = "archive", ".deleted"
STEPS = {1: "Parse the template", 2: "Write the project", 3: "Write the tasks"}
NOT_A_PLAN_NOTE = "no phase or task headings"


class Fail(Exception):
    def __init__(self, errors, code=2, **extra):
        errors = [errors] if isinstance(errors, str) else errors
        super().__init__("; ".join(str(e) for e in errors))
        self.errors, self.code, self.extra = [str(e) for e in errors], code, extra


class Parser(argparse.ArgumentParser):
    def __init__(self, *a, **kw):
        kw.setdefault("allow_abbrev", False)
        super().__init__(*a, **kw)

    def error(self, message):
        raise Fail([message])


def emit(obj):
    print(json.dumps(obj, default=str), flush=True)


# ---------- reading one file ----------

def read_text(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def split_header(text):
    """(header text, body text) when the first line is +++ and a closing +++ follows, else (None, text)."""
    lines = text.split("\n")
    if lines[0].lstrip("﻿").strip() != "+++":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "+++":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


PLAN_LINE = re.compile(r"""^plan\s*=\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)')""", re.M)


def plan_id_of(text, stem):
    """The header's plan = "..." value; no header (or no plan line) -> the file name without .md."""
    header, _ = split_header(text)
    m = PLAN_LINE.search(header) if header is not None else None
    found = (m.group(1) if m.group(1) is not None else m.group(2)) if m else ""
    return found.strip() or stem


def title_of(body, plan_id):
    """The H1 text after the em dash (`# <id> — <title>`), the whole H1 without one; none -> the id.
    A '#' inside a fenced block is not a heading."""
    fenced, h1 = False, None
    for line in body.split("\n"):
        st = line.lstrip()
        if st.startswith("```") or st.startswith("~~~"):
            fenced = not fenced
        elif not fenced and st.startswith("# "):
            h1 = st[2:].strip()
            break
    if not h1:
        return plan_id
    _, dash, tail = h1.partition("—")
    return (tail.strip() if dash and tail.strip() else h1) or plan_id


# ---------- the inbox on disk ----------

def inbox_root(home):
    try:
        return mkpaths.inbox_dir(home)
    except mkpaths.Fail as e:
        raise Fail(str(e))


def scan(folder, archived):
    """The plan entries directly under `folder`: a *.md file, or a directory holding plan.md. Dotfiles,
    the archive/ folder and anything else are not entries. Nothing is read beyond the names."""
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        if name.startswith(".") or (name == ARCHIVE and not archived):
            continue
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            md = os.path.join(path, "plan.md")
            if os.path.isfile(md):
                out.append({"name": name, "path": path, "md": md, "kind": "folder", "archived": archived,
                            "shown": f"{name}/plan.md", "stem": name})
        elif name.endswith(".md") and os.path.isfile(path):
            out.append({"name": name, "path": path, "md": path, "kind": "file", "archived": archived,
                        "shown": name, "stem": name[:-3]})
    return out


def all_entries(box):
    return scan(box, False) + scan(os.path.join(box, ARCHIVE), True)


def check_plan_file(md, data, home):
    """Run mk-plan.py check --classify on one file (the C2 reply). Never raises: a file the check cannot
    class is not_a_plan with the reason as its note."""
    env = dict(os.environ)
    if data:
        env["MATRIXKODER_DATA"] = data
    env["MK_HOME"] = env["MATRIXKODER_HOME"] = home
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "mk-plan.py"), "check", "--classify", md],
                           capture_output=True, text=True, env=env, timeout=120)
        reply = json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
        return {"fit": "not_a_plan", "sections_found": 0, "sections_total": 7, "missing": [],
                "note": f"the check did not run: {type(e).__name__}", "errors": [], "warnings": []}
    fit = reply.get("fit")
    errors = [str(e) for e in reply.get("errors") or []]
    if fit not in ("template", "partial", "not_a_plan"):
        return {"fit": "not_a_plan", "sections_found": 0, "sections_total": 7, "missing": [],
                "note": errors[0] if errors else NOT_A_PLAN_NOTE, "errors": errors, "warnings": []}
    return {"fit": fit, "sections_found": reply.get("sections_found", 0),
            "sections_total": reply.get("sections_total", 7), "missing": list(reply.get("missing") or []),
            "note": NOT_A_PLAN_NOTE if fit == "not_a_plan" else "", "errors": errors,
            "warnings": [str(w) for w in reply.get("warnings") or []]}


def describe(e, data, home, now):
    """One C3 entry (plus `note`) for a scanned entry."""
    text = read_text(e["md"])
    pid = plan_id_of(text, e["stem"])
    _, body = split_header(text)
    cls = check_plan_file(e["md"], data, home)
    try:
        age = max(0, int(now - os.stat(e["md"]).st_mtime))
    except OSError:
        age = 0
    return {"id": pid, "path": e["shown"], "title": title_of(body, pid), "fit": cls["fit"],
            "sections_found": cls["sections_found"], "sections_total": cls["sections_total"],
            "missing": cls["missing"], "age_s": age, "archived": e["archived"], "note": cls["note"]}


# ---------- inbox events (C5) ----------

class EventLog:
    """Best-effort writer of the project store's inbox channel. It never creates a store and never
    raises: a missing store or a failed write becomes one entry in `warnings` and the action goes on."""

    def __init__(self, data, home, by):
        self.home, self.by, self.warnings, self.ok, self.db, self.data = home, by, [], False, None, None
        try:
            self.data = mkpaths.data_dir(data, required=False)
            self.db = _load("mkdb", "mkdb.py")
            self.ok = self.data is not None and self.db._store_present(home, None, self.data)
            if not self.ok:
                self.warnings.append("no project store: inbox events not recorded")
        except Exception as err:  # noqa: BLE001 - the action must not fail on its event log
            self.warnings.append(f"inbox events not recorded: {type(err).__name__}: {err}")

    def write(self, kind, file, plan, fit, text, **extra):
        if not self.ok:
            return
        meta = {"file": file, "plan": plan, "fit": fit, "by": self.by, **extra}
        try:
            self.db.append_event(self.home, None, None, chan="inbox", kind=kind, from_id=self.by,
                                 text=text, meta=meta, data_dir=self.data)
        except Exception as err:  # noqa: BLE001
            self.ok = False  # one warning, not one per remaining event
            self.warnings.append(f"inbox event {kind} not recorded: {type(err).__name__}: {err}")

    def seen(self):
        """({(file, plan)} that have a `found` event, {(file, plan): class of the last `checked`})."""
        found, checked = set(), {}
        if not self.ok:
            return found, checked
        try:
            rows = self.db.events(self.home, None, None, data_dir=self.data, chan="inbox")
        except Exception as err:  # noqa: BLE001
            self.ok = False
            self.warnings.append(f"inbox events not read: {type(err).__name__}: {err}")
            return found, checked
        for row in rows:
            try:
                meta = json.loads(row.get("meta_json") or "{}")
            except ValueError:
                continue
            key = (meta.get("file"), meta.get("plan"))
            if row.get("kind") == "found":
                found.add(key)
            elif row.get("kind") == "checked":
                checked[key] = class_sig(meta)
        return found, checked

    def fit_of(self, entry, home, data):
        """The fit of an entry, only worth a subprocess when events are being recorded."""
        return check_plan_file(entry["md"], data, home)["fit"] if self.ok else None


def class_sig(meta_or_file):
    """What `checked` compares: fit, sections found and the missing names."""
    m = meta_or_file
    return (m.get("fit"), m.get("sections_found"), tuple(m.get("missing") or ()))


def with_warnings(out, log):
    if log.warnings:
        out["warnings"] = list(out.get("warnings") or []) + log.warnings
    return out


# ---------- list ----------

def cmd_list(a):
    box, home = inbox_root(a.home), mkpaths.home(a.home)
    now = time.time()
    files = [describe(e, a.data, home, now) for e in all_entries(box)]
    log = EventLog(a.data, home, a.by) if files else None
    if log is not None and log.ok:
        found, checked = log.seen()
        for f in files:
            key, sig = (f["path"], f["id"]), class_sig(f)
            where = f"{f['path']}{' (archived)' if f['archived'] else ''}"
            if key not in found:
                log.write("found", f["path"], f["id"], f["fit"], f"found {where}")
            if checked.get(key) != sig:
                log.write("checked", f["path"], f["id"], f["fit"],
                          f"checked {where}: {f['fit']}, {f['sections_found']} of {f['sections_total']} sections",
                          sections_found=f["sections_found"], sections_total=f["sections_total"], missing=f["missing"])
    live = [f for f in files if not f["archived"]]
    counts = {"inbox": len(live), "template": sum(f["fit"] == "template" for f in live),
              "fix": sum(f["fit"] != "template" for f in live), "archived": len(files) - len(live)}
    out = {"ok": True, "files": files, "counts": counts}
    return out if log is None else with_warnings(out, log)


# ---------- finding one plan ----------

def check_id(pid):
    if not pid or pid in (".", "..") or "/" in pid or "\\" in pid or "\0" in pid:
        raise Fail(f"bad plan id {pid!r}")


def find(box, pid, where):
    """The one entry whose plan id is `pid` in `where` ('inbox', 'archive' or 'any'). Two matches in one
    place are ambiguous and refuse; a match in both places asks for --archived (delete only)."""
    check_id(pid)
    hits = {"inbox": [], "archive": []}
    for e in all_entries(box):
        if plan_id_of(read_text(e["md"]), e["stem"]) == pid:
            hits["archive" if e["archived"] else "inbox"].append(e)
    places = ["inbox", "archive"] if where == "any" else [where]
    found = [(p, e) for p in places for e in hits[p]]
    if not found:
        raise Fail(f"no plan with id {pid!r} in the {'inbox' if where == 'inbox' else 'archive' if where == 'archive' else 'inbox or archive'}")
    if len(found) > 1:
        names = ", ".join(("archive/" if e["archived"] else "") + e["shown"] for _, e in found)
        raise Fail(f"plan id {pid!r} is ambiguous ({names})" + ("; pass --archived to pick the archived one" if where == "any" else ""))
    return found[0][1]


def inside(box, path):
    """True when `path` is a real, non-symlink entry whose resolved path stays under the inbox."""
    if os.path.islink(path):
        return False
    real, root = os.path.realpath(path), os.path.realpath(box)
    return os.path.commonpath([real, root]) == root and real != root


def need_inside(box, e):
    if not inside(box, e["path"]):
        raise Fail(f"{e['shown']} is a symlink or resolves outside the inbox; refused, nothing changed")


# ---------- archive / restore ----------

def move_entry(box, e, to_archive):
    need_inside(box, e)
    arch = os.path.join(box, ARCHIVE)
    dst_dir = arch if to_archive else box
    if not to_archive and (e["name"] == ARCHIVE or e["name"].startswith(".")):
        raise Fail(f"{e['name']} cannot be restored: the name is reserved in the inbox")
    dst = os.path.join(dst_dir, e["name"])
    if os.path.lexists(dst):
        raise Fail(f"{os.path.relpath(dst, box)} already exists; nothing moved")
    os.makedirs(dst_dir, exist_ok=True)
    try:
        os.rename(e["path"], dst)
    except OSError as err:
        raise Fail(f"move failed: {err}")
    return dst


def cmd_archive(a):
    box = inbox_root(a.home)
    e = find(box, a.id, "inbox")
    log = EventLog(a.data, mkpaths.home(a.home), a.by)
    fit = log.fit_of(e, mkpaths.home(a.home), a.data)  # before the move: the check reads the file where it is
    dst = move_entry(box, e, True)
    log.write("archived", e["shown"], a.id, fit, f"archived {e['shown']}")
    return with_warnings({"ok": True, "id": a.id, "path": os.path.relpath(dst, box)}, log)


def cmd_restore(a):
    box = inbox_root(a.home)
    e = find(box, a.id, "archive")
    log = EventLog(a.data, mkpaths.home(a.home), a.by)
    fit = log.fit_of(e, mkpaths.home(a.home), a.data)
    dst = move_entry(box, e, False)
    log.write("restored", e["shown"], a.id, fit, f"restored {e['shown']}")
    return with_warnings({"ok": True, "id": a.id, "path": os.path.relpath(dst, box)}, log)


# ---------- delete ----------

def tree_sig(path):
    """{relative path: sha256 | link target} of a file or folder, symlinks not followed."""
    sig = {}
    if os.path.isfile(path) and not os.path.islink(path):
        return {".": file_sha(path)}
    for dirpath, dirs, files in os.walk(path):
        for name in dirs + files:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, path)
            sig[rel] = "link:" + os.readlink(p) if os.path.islink(p) else (None if os.path.isdir(p) else file_sha(p))
    return sig


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(pid):
    s = re.sub(r"[^A-Za-z0-9._-]", "_", pid).lstrip(".")
    return s or "plan"


def cmd_delete(a):
    box = inbox_root(a.home)
    e = find(box, a.id, "archive" if a.archived else "any")
    need_inside(box, e)
    log = EventLog(a.data, mkpaths.home(a.home), a.by)
    fit = log.fit_of(e, mkpaths.home(a.home), a.data)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(box, DELETED, f"{safe_name(a.id)}-{stamp}")
    keep, n = base, 1
    while os.path.lexists(keep):
        n += 1
        keep = f"{base}-{n}"
    copy = os.path.join(keep, e["name"])
    try:
        os.makedirs(keep)
        if e["kind"] == "folder":
            shutil.copytree(e["path"], copy, symlinks=True)
        else:
            shutil.copy2(e["path"], copy)
        if tree_sig(e["path"]) != tree_sig(copy):
            raise OSError("the copy does not match the original")
    except OSError as err:
        shutil.rmtree(keep, ignore_errors=True)  # only the copy this call made
        raise Fail(f"could not keep a copy first ({err}); nothing deleted")
    need_inside(box, e)  # the entry could have been swapped for a link while it was copied
    try:
        if e["kind"] == "folder":
            shutil.rmtree(e["path"])
        else:
            os.unlink(e["path"])
    except OSError as err:
        raise Fail(f"delete failed: {err}; the copy is at {copy}")
    kept = os.path.relpath(copy, box)
    log.write("deleted", e["shown"], a.id, fit, f"deleted {e['shown']}; a copy is kept in {kept}", copy=kept)
    return with_warnings({"ok": True, "id": a.id, "deleted": e["shown"], "copy": kept}, log)


# ---------- import ----------

def progress(step):
    emit({"progress": True, "step": step, "of": 3, "name": STEPS[step]})


def run_plan(args, data, home):
    """Run mk-plan.py; (exit code, JSON reply). A reply that is not one JSON line is a failure."""
    env = dict(os.environ)
    if data:
        env["MATRIXKODER_DATA"] = data
    env["MK_HOME"] = env["MATRIXKODER_HOME"] = home
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "mk-plan.py"), *args],
                           capture_output=True, text=True, env=env, timeout=300)
        return r.returncode, json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
        return 1, {"ok": False, "errors": [f"mk-plan.py did not answer: {type(e).__name__}: {e}"]}


def step_fail(step, reason, **extra):
    return Fail([f"import failed at step {step} ({STEPS[step]}): {reason}"], step=step, reason=reason, **extra)


def cmd_import(a):
    box, home = inbox_root(a.home), mkpaths.home(a.home)
    e = find(box, a.id, "inbox")
    need_inside(box, e)
    log, seen = EventLog(a.data, home, a.by), {"fit": None}
    try:
        out = do_import(a, box, home, e, log, seen)
    except Fail as err:
        if "step" in err.extra:
            log.write("import-failed", e["shown"], a.id, seen["fit"],
                      f"import failed at step {err.extra['step']}: {err.extra['reason']}",
                      step=err.extra["step"], reason=err.extra["reason"])
        err.extra.update(with_warnings({}, log))
        raise
    log.write("imported", e["shown"], a.id, seen["fit"], f"imported {e['shown']} as {out.get('plan')}",
              plan_in_store=out.get("plan"), archived_to=out.get("archived_to"))
    return with_warnings(out, log)


def step_event(log, seen, e, pid, n):
    log.write("import-step", e["shown"], pid, seen["fit"], f"import step {n} of 3: {STEPS[n]}", step=n, of=3, name=STEPS[n])


def do_import(a, box, home, e, log, seen):
    # step 1: the check, before anything is written
    progress(1)
    cls = check_plan_file(e["md"], a.data, home)
    seen["fit"] = cls["fit"]
    step_event(log, seen, e, a.id, 1)
    if cls["fit"] == "not_a_plan":
        raise step_fail(1, cls["note"] or NOT_A_PLAN_NOTE)
    target = os.path.join(box, ARCHIVE, e["name"])
    if e["kind"] == "file" and os.path.lexists(target):
        raise step_fail(1, f"inbox/archive/{e['name']} already exists; nothing imported")
    # step 2: the project store row (mk-plan.py import)
    progress(2)
    step_event(log, seen, e, a.id, 2)
    args = ["import", e["path"], "--home", home] + (["--data", a.data] if a.data else [])
    code, reply = run_plan(args, a.data, home)
    if code != 0 or reply.get("ok") is not True:
        why = reply.get("reason") or "; ".join(str(x) for x in reply.get("errors") or []) or f"mk-plan.py exited {code}"
        raise step_fail(2, why)
    # step 3: the tasks come with the import call; this step finishes it (the flat file leaves the inbox)
    progress(3)
    step_event(log, seen, e, a.id, 3)
    archived_to = None
    if e["kind"] == "file":
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.rename(e["path"], target)
            archived_to = os.path.relpath(target, box)
        except OSError as err:
            raise step_fail(3, f"imported, but moving the file to inbox/archive/ failed: {err}", imported=True)
    return {"ok": True, "id": a.id, "plan": reply.get("plan"), "title": reply.get("title"),
            "version": reply.get("version"), "noop": reply.get("noop"), "steps": 3,
            "archived_to": archived_to, "moved": reply.get("moved") or [],
            "warnings": list(reply.get("warnings") or []) + cls["errors"] + cls["warnings"]}


# ---------- CLI ----------

def build_parser():
    p = Parser(prog="mk-inbox.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, func in (("list", cmd_list), ("import", cmd_import), ("archive", cmd_archive),
                       ("restore", cmd_restore), ("delete", cmd_delete)):
        c = sub.add_parser(name)
        if name != "list":
            c.add_argument("id")
        if name == "list":
            c.add_argument("--json", action="store_true")
        if name == "delete":
            c.add_argument("--archived", action="store_true")
        c.add_argument("--home", default=None)
        c.add_argument("--data", default=None)
        c.add_argument("--by", default="user", help="who asked, recorded on the inbox events (default user)")
        c.set_defaults(func=func)
    return p


def main(argv):
    try:
        a = build_parser().parse_args(argv)
        out, code = a.func(a), 0
    except Fail as e:
        out, code = {"ok": False, "errors": e.errors, **e.extra}, e.code
    except mkpaths.Fail as e:
        out, code = {"ok": False, "errors": [str(e)]}, 2
    emit(out)
    return code


# ---------- self-test ----------

def self_test():
    mk_config = _load("mk_config", "mk-config.py")
    # byte copies of two real plans taken 2026-10-21: plan-inbox-and-parsing (a template plan) and mk-ponytail-hooks (no phase blocks)
    real_tpl = os.path.join(HERE, "fixtures", "inbox-plan-template.md")
    real_not = os.path.join(HERE, "fixtures", "inbox-plan-not-a-plan.md")
    tmp = tempfile.mkdtemp(prefix="mk-inbox-test-")
    restore_env = mk_config.pin_tmp_data(tmp)  # data = tmp/data, home = tmp/home, MK_SELFTEST=1
    data, home = os.path.join(tmp, "data"), os.path.join(tmp, "home")
    box = mkpaths.inbox_dir(home)
    me = os.path.abspath(__file__)

    def mi(*args, code=0):
        """Run mk-inbox.py in a subprocess: (exit code checked, [progress lines], the final reply)."""
        r = subprocess.run([sys.executable, me, *args, "--home", home, "--data", data],
                           capture_output=True, text=True)
        lines = [json.loads(l) for l in r.stdout.strip().splitlines()]
        assert lines and r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        assert "ok" in lines[-1] and all("progress" in l for l in lines[:-1]), (args, lines)
        return lines[:-1], lines[-1]

    def put(rel, text):
        p = os.path.join(box, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def snapshot(path):
        return {os.path.relpath(os.path.join(d, n), path): (os.stat(os.path.join(d, n)).st_mtime_ns, os.path.getsize(os.path.join(d, n)))
                for d, _ds, fs in os.walk(path) for n in fs}

    def plan_rows():
        db = _load("mkdb", "mkdb.py")
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        try:
            return {r["id"]: r["title"] for r in con.execute("SELECT id, title FROM plans")}
        finally:
            con.close()

    def tree(path):
        return sorted(os.path.relpath(os.path.join(d, n), path) for d, ds, fs in os.walk(path) for n in ds + fs)

    def evs(after=0):
        """The project's inbox-channel events past an id, as written by this script (read back through mkdb)."""
        rows = _load("mkdb", "mkdb.py").events(home, None, None, data_dir=data, chan="inbox")
        return [dict(r, meta=json.loads(r["meta_json"])) for r in rows if r["id"] > after]

    def last_id():
        return max([e["id"] for e in evs()] or [0])

    def raw(*args, data_arg=None):
        """Run mk-inbox.py with its own --data (mi always passes the good store)."""
        r = subprocess.run([sys.executable, me, *args, "--home", home, "--data", data_arg or data], capture_output=True, text=True)
        lines = [json.loads(l) for l in r.stdout.strip().splitlines()]
        return r.returncode, lines

    try:
        with open(real_tpl) as f:
            tpl_text = f.read()
        with open(real_not) as f:
            not_text = f.read()
        db = _load("mkdb", "mkdb.py")
        os.makedirs(data, exist_ok=True)
        db.open_project(home, data_dir=mkpaths.project_folder(data, home)).close()
        assert plan_rows() == {}

        # ---- INBOX.1 list ----
        # a missing inbox, then an empty one: ok, empty, and the empty list creates nothing
        assert not os.path.exists(box)
        _, out = mi("list", "--json")
        assert out == {"ok": True, "files": [], "counts": {"inbox": 0, "template": 0, "fix": 0, "archived": 0}}, out
        assert not os.path.exists(box), "list created the inbox"
        os.makedirs(box)
        _, out = mi("list", "--json")
        assert out["ok"] and out["files"] == [], out

        # a partial: the real template plan with its Risks section heading renamed away
        assert "## Risks" in tpl_text, "the real plan lost its Risks section"
        partial = tpl_text.replace("## Risks", "## Hazards", 1).replace('plan = "plan-inbox-and-parsing"', 'plan = "inbox-partial"', 1)
        put("plan-inbox-and-parsing.md", tpl_text)
        put("inbox-partial.md", partial)
        put("ponytail-notes.md", not_text)
        put("loose-note.md", "# just some thoughts\n\nno header here\n")
        put("archive/old-one.md", tpl_text.replace('plan = "plan-inbox-and-parsing"', 'plan = "old-one"', 1))
        put(".hidden.md", tpl_text)  # dotfiles are not listed
        put(".deleted/stale-1/x.md", tpl_text)  # nor is .deleted/
        put("readme.txt", "not a plan\n")
        before = snapshot(box)
        names_before = tree(box)
        _, out = mi("list", "--json")
        assert snapshot(box) == before and tree(box) == names_before, "list wrote or moved something"
        by = {f["path"]: f for f in out["files"]}
        assert sorted(by) == ["inbox-partial.md", "loose-note.md", "old-one.md", "plan-inbox-and-parsing.md", "ponytail-notes.md"], sorted(by)
        t = by["plan-inbox-and-parsing.md"]
        assert t["id"] == "plan-inbox-and-parsing" and t["fit"] == "template" and t["missing"] == [], t
        assert t["sections_found"] == t["sections_total"] == 7 and t["archived"] is False and t["age_s"] >= 0, t
        assert t["title"] and t["title"] != t["id"], t
        p = by["inbox-partial.md"]
        assert p["id"] == "inbox-partial" and p["fit"] == "partial" and p["missing"] == ["Risks"], p
        assert p["sections_found"] == 6 and p["sections_total"] == 7, p
        n = by["ponytail-notes.md"]
        assert n["id"] == "mk-ponytail-hooks" and n["fit"] == "not_a_plan" and n["note"] == NOT_A_PLAN_NOTE, n  # the id is the header's, not the file name
        s = by["loose-note.md"]  # no header at all: not_a_plan, never an error, the id falls back to the file name
        assert s["id"] == "loose-note" and s["fit"] == "not_a_plan" and s["title"] == "just some thoughts", s
        a_ = by["old-one.md"]
        assert a_["archived"] is True and a_["id"] == "old-one" and a_["fit"] == "template", a_
        assert out["counts"] == {"inbox": 4, "template": 1, "fix": 3, "archived": 1}, out["counts"]
        for f in out["files"]:
            assert set(f) == {"id", "path", "title", "fit", "sections_found", "sections_total", "missing", "age_s", "archived", "note"}, f
            assert not os.path.isabs(f["path"]), f

        # ---- EVT.1 list records `found` once per new file and `checked` when its class changes ----
        ev = evs()
        want = ["inbox-partial.md", "loose-note.md", "old-one.md", "plan-inbox-and-parsing.md", "ponytail-notes.md"]
        assert sorted(e["meta"]["file"] for e in ev if e["kind"] == "found") == want, ev
        assert sorted(e["meta"]["file"] for e in ev if e["kind"] == "checked") == want, ev
        assert all(e["run"] is None and e["task"] is None and e["chan"] == "inbox" and e["from_id"] == "user" for e in ev), ev
        m = next(e["meta"] for e in ev if e["kind"] == "checked" and e["meta"]["file"] == "inbox-partial.md")
        assert m["plan"] == "inbox-partial" and m["fit"] == "partial" and m["by"] == "user" and m["missing"] == ["Risks"], m
        n_before = len(ev)
        mi("list", "--json")
        assert len(evs()) == n_before, "a second list with nothing new added events"
        # the class of a file changes -> exactly one new `checked`, no new `found`
        with open(os.path.join(box, "inbox-partial.md"), "a") as f:
            f.write("\n## Hazards again\n")
        mi("list", "--json")
        assert len(evs()) == n_before, "an edit that leaves the class alone added an event"
        os.replace(os.path.join(box, "inbox-partial.md"), os.path.join(tmp, "partial.bak"))
        put("inbox-partial.md", tpl_text.replace('plan = "plan-inbox-and-parsing"', 'plan = "inbox-partial"', 1))
        mi("list", "--json")
        new = evs(after=max(e["id"] for e in ev))
        assert [(e["kind"], e["meta"]["file"], e["meta"]["fit"]) for e in new] == [("checked", "inbox-partial.md", "template")], new
        os.replace(os.path.join(tmp, "partial.bak"), os.path.join(box, "inbox-partial.md"))
        with open(os.path.join(box, "inbox-partial.md"), "w") as f:
            f.write(partial)
        mi("list", "--json")  # back to the partial class: one more `checked`
        assert len(evs()) == n_before + 2, evs()

        # a folder-form plan lists (path is <folder>/plan.md) and archives, folder intact
        put("fold-plan/plan.md", tpl_text.replace('plan = "plan-inbox-and-parsing"', 'plan = "fold-plan"', 1))
        put("fold-plan/notes.md", "side notes\n")
        _, out = mi("list", "--json")
        fp = next(f for f in out["files"] if f["id"] == "fold-plan")
        assert fp["path"] == "fold-plan/plan.md" and fp["fit"] == "template" and fp["archived"] is False, fp
        assert out["counts"]["inbox"] == 5, out["counts"]
        mark = last_id()
        _, out = mi("archive", "fold-plan", "--by", "lane-x")
        assert out["ok"] and out["path"] == "archive/fold-plan", out
        assert "warnings" not in out, out
        new = evs(after=mark)
        assert [(e["kind"], e["meta"]) for e in new] == [("archived", {"file": "fold-plan/plan.md", "plan": "fold-plan", "fit": "template", "by": "lane-x"})], new
        assert new[0]["from_id"] == "lane-x" and new[0]["run"] is None, new
        assert os.path.isfile(os.path.join(box, "archive", "fold-plan", "plan.md")) and os.path.isfile(os.path.join(box, "archive", "fold-plan", "notes.md"))
        assert not os.path.exists(os.path.join(box, "fold-plan"))
        _, out = mi("list", "--json")
        assert next(f for f in out["files"] if f["id"] == "fold-plan")["archived"] is True and out["counts"]["archived"] == 2, out["counts"]
        mark = last_id()
        _, out = mi("restore", "fold-plan")
        assert out["ok"] and os.path.isfile(os.path.join(box, "fold-plan", "notes.md")), out
        new = evs(after=mark)
        assert [(e["kind"], e["meta"]["file"], e["meta"]["plan"], e["meta"]["fit"], e["meta"]["by"]) for e in new] == [("restored", "fold-plan/plan.md", "fold-plan", "template", "user")], new

        # ---- INBOX.2 archive / restore round trip, and each refuses when the target exists ----
        blob = open(os.path.join(box, "inbox-partial.md"), "rb").read()
        _, out = mi("archive", "inbox-partial")
        assert out["ok"] and not os.path.exists(os.path.join(box, "inbox-partial.md"))
        assert open(os.path.join(box, "archive", "inbox-partial.md"), "rb").read() == blob
        _, out = mi("restore", "inbox-partial")
        assert out["ok"] and open(os.path.join(box, "inbox-partial.md"), "rb").read() == blob
        assert not os.path.exists(os.path.join(box, "archive", "inbox-partial.md"))
        # archive refuses: a different file already sits at the target
        put("archive/inbox-partial.md", "someone else's file\n")
        _, out = mi("archive", "inbox-partial", code=2)
        assert out["ok"] is False and "already exists" in out["errors"][0], out
        assert open(os.path.join(box, "inbox-partial.md"), "rb").read() == blob
        assert open(os.path.join(box, "archive", "inbox-partial.md")).read() == "someone else's file\n"
        os.unlink(os.path.join(box, "archive", "inbox-partial.md"))
        # restore refuses: the inbox already holds that name
        put("archive/loose-note.md", "# archived twin\n")
        _, out = mi("restore", "loose-note", code=2)  # two plans share the id loose-note: ambiguous only across places, restore looks at the archive
        assert out["ok"] is False and "already exists" in out["errors"][0], out
        assert open(os.path.join(box, "loose-note.md")).read().startswith("# just some thoughts")
        os.unlink(os.path.join(box, "archive", "loose-note.md"))
        # an unknown id and a bad id refuse
        _, out = mi("archive", "no-such-plan", code=2)
        assert out["ok"] is False, out
        for bad in ("..", ".", "a/b"):
            _, out = mi("archive", bad, code=2)
            assert out["ok"] is False and "bad plan id" in out["errors"][0], (bad, out)

        # ---- INBOX.2 import ----
        # a not-a-plan file fails at step 1, nothing written, the file stays
        notb = open(os.path.join(box, "ponytail-notes.md"), "rb").read()
        mark = last_id()
        lines, out = mi("import", "mk-ponytail-hooks", code=2)
        assert [l["step"] for l in lines] == [1] and out["ok"] is False and out["step"] == 1, (lines, out)
        new = evs(after=mark)
        assert [(e["kind"], e["meta"].get("step")) for e in new] == [("import-step", 1), ("import-failed", 1)], new
        assert new[1]["meta"]["fit"] == "not_a_plan" and NOT_A_PLAN_NOTE in new[1]["meta"]["reason"] and new[1]["meta"]["plan"] == "mk-ponytail-hooks", new
        assert NOT_A_PLAN_NOTE in out["reason"], out
        assert plan_rows() == {} and open(os.path.join(box, "ponytail-notes.md"), "rb").read() == notb
        lines, out = mi("import", "loose-note", code=2)  # no header at all: also step 1
        assert [l["step"] for l in lines] == [1] and out["step"] == 1 and plan_rows() == {}, (lines, out)
        # an archive target already taken: refused at step 1 too, before the store is touched
        put("archive/plan-inbox-and-parsing.md", "taken\n")
        lines, out = mi("import", "plan-inbox-and-parsing", code=2)
        assert [l["step"] for l in lines] == [1] and out["step"] == 1 and "already exists" in out["reason"], (lines, out)
        assert plan_rows() == {}
        os.unlink(os.path.join(box, "archive", "plan-inbox-and-parsing.md"))
        # a template file: three progress lines in order, then the reply; the row is in the store, the file in archive/
        tpl_blob = open(os.path.join(box, "plan-inbox-and-parsing.md"), "rb").read()
        mark = last_id()
        lines, out = mi("import", "plan-inbox-and-parsing")
        new = evs(after=mark)
        assert [(e["kind"], e["meta"].get("step")) for e in new] == [("import-step", 1), ("import-step", 2), ("import-step", 3), ("imported", None)], new
        assert all(e["meta"]["fit"] == "template" and e["meta"]["plan"] == "plan-inbox-and-parsing" and e["meta"]["file"] == "plan-inbox-and-parsing.md" and e["meta"]["by"] == "user" for e in new), new
        assert [e["meta"].get("of") for e in new[:3]] == [3, 3, 3] and new[3]["meta"]["archived_to"] == "archive/plan-inbox-and-parsing.md", new
        assert lines == [{"progress": True, "step": s, "of": 3, "name": STEPS[s]} for s in (1, 2, 3)], lines
        assert out["ok"] and out["plan"] == "plan-inbox-and-parsing" and out["archived_to"] == "archive/plan-inbox-and-parsing.md", out
        assert plan_rows() == {"plan-inbox-and-parsing": out["title"]}, plan_rows()
        assert not os.path.exists(os.path.join(box, "plan-inbox-and-parsing.md"))
        assert open(os.path.join(box, "archive", "plan-inbox-and-parsing.md"), "rb").read() == tpl_blob
        _, out = mi("list", "--json")
        assert "plan-inbox-and-parsing" not in [f["id"] for f in out["files"] if not f["archived"]], out
        # a partial imports too (a draft to improve); the folder form is imported as shipped (mk-plan moves the folder itself)
        lines, out = mi("import", "inbox-partial")
        assert len(lines) == 3 and out["ok"] and "inbox-partial" in plan_rows(), (lines, out)
        lines, out = mi("import", "fold-plan")
        assert len(lines) == 3 and out["ok"] and out["archived_to"] is None and out["moved"], (lines, out)
        assert not os.path.exists(os.path.join(box, "fold-plan")) and "fold-plan" in plan_rows()
        # no store: step 2 fails, names the step and the reason, and the file stays
        put("nostore.md", tpl_text.replace('plan = "plan-inbox-and-parsing"', 'plan = "nostore"', 1))
        r = subprocess.run([sys.executable, me, "import", "nostore", "--home", home, "--data", os.path.join(tmp, "empty-data")],
                           capture_output=True, text=True)
        lines = [json.loads(l) for l in r.stdout.strip().splitlines()]
        assert r.returncode == 2 and [l.get("step") for l in lines] == [1, 2, 2] and lines[-1]["ok"] is False and lines[-1]["step"] == 2, r.stdout
        assert any("no project store" in w for w in lines[-1]["warnings"]), lines[-1]  # no store: no events, said in the reply
        assert os.path.isfile(os.path.join(box, "nostore.md")) and not os.path.exists(mkpaths.db_in(os.path.join(tmp, "empty-data")))
        os.unlink(os.path.join(box, "nostore.md"))

        # ---- INBOX.2 delete ----
        put("kill-me.md", tpl_text.replace('plan = "plan-inbox-and-parsing"', 'plan = "kill-me"', 1))
        kill_blob = open(os.path.join(box, "kill-me.md"), "rb").read()
        mark = last_id()
        _, out = mi("delete", "kill-me")
        assert out["ok"] and out["deleted"] == "kill-me.md" and out["copy"].startswith(".deleted/kill-me-"), out
        new = evs(after=mark)
        assert [(e["kind"], e["meta"]["file"], e["meta"]["plan"], e["meta"]["fit"], e["meta"]["by"]) for e in new] == [("deleted", "kill-me.md", "kill-me", "template", "user")], new
        assert new[0]["meta"]["copy"] == out["copy"], new
        assert not os.path.exists(os.path.join(box, "kill-me.md"))
        assert open(os.path.join(box, out["copy"]), "rb").read() == kill_blob, "the kept copy differs"
        # a folder is copied whole, then removed
        put("fold-del/plan.md", tpl_text.replace('plan = "plan-inbox-and-parsing"', 'plan = "fold-del"', 1))
        put("fold-del/assets/a.txt", "asset\n")
        _, out = mi("delete", "fold-del")
        assert out["ok"] and not os.path.exists(os.path.join(box, "fold-del"))
        assert open(os.path.join(box, out["copy"], "assets", "a.txt")).read() == "asset\n"
        # an archived plan deletes too; the same id in both places is ambiguous unless --archived says which
        put("twin.md", tpl_text.replace('plan = "plan-inbox-and-parsing"', 'plan = "twin"', 1))
        put("archive/twin.md", tpl_text.replace('plan = "plan-inbox-and-parsing"', 'plan = "twin"', 1))
        _, out = mi("delete", "twin", code=2)
        assert "ambiguous" in out["errors"][0] and os.path.isfile(os.path.join(box, "twin.md")) and os.path.isfile(os.path.join(box, "archive", "twin.md")), out
        _, out = mi("delete", "twin", "--archived")
        assert out["ok"] and os.path.isfile(os.path.join(box, "twin.md")) and not os.path.exists(os.path.join(box, "archive", "twin.md")), out
        # a symlink pointing out of the inbox (file and folder) is refused, and the outside stays whole
        outside = os.path.join(tmp, "outside")
        os.makedirs(os.path.join(outside, "dir"))
        ofile, odir_plan = os.path.join(outside, "evil.md"), os.path.join(outside, "dir", "plan.md")
        for p in (ofile, odir_plan):
            open(p, "w").write(tpl_text.replace('plan = "plan-inbox-and-parsing"', 'plan = "evil"', 1))
        os.symlink(ofile, os.path.join(box, "evil-link.md"))
        before_out = snapshot(outside)
        _, out = mi("delete", "evil", code=2)
        assert out["ok"] is False and "outside the inbox" in out["errors"][0], out
        os.unlink(os.path.join(box, "evil-link.md"))
        os.symlink(os.path.join(outside, "dir"), os.path.join(box, "evil-dir"))
        _, out = mi("delete", "evil", code=2)
        assert out["ok"] is False and "outside the inbox" in out["errors"][0], out
        for verb in ("archive", "import"):
            _, out = mi(verb, "evil", code=2)
            assert out["ok"] is False and "outside the inbox" in out["errors"][0], (verb, out)
        assert snapshot(outside) == before_out and os.path.isdir(os.path.join(box, "evil-dir")) is True
        assert not any(n.startswith("evil") for n in os.listdir(os.path.join(box, DELETED))), "a copy of an outside path was made"
        os.unlink(os.path.join(box, "evil-dir"))
        # a refused action records nothing
        mark = last_id()
        mi("delete", "evil-none", code=2)
        assert evs(after=mark) == []
        # a failed event write never fails the action: a store that is not a database
        bad = os.path.join(tmp, "bad-data")
        os.makedirs(bad)
        with open(mkpaths.project_db_in(mkpaths.project_folder(bad, home)), "wb") as f:
            f.write(b"this is not a database")
        put("quiet.md", tpl_text.replace('plan = "plan-inbox-and-parsing"', 'plan = "quiet"', 1))
        code, lines = raw("archive", "quiet", data_arg=bad)
        assert code == 0 and lines[-1]["ok"] is True and any("not recorded" in w for w in lines[-1]["warnings"]), lines
        assert os.path.isfile(os.path.join(box, "archive", "quiet.md")) and not os.path.exists(os.path.join(box, "quiet.md"))
        code, lines = raw("list", "--json", data_arg=bad)
        assert code == 0 and lines[-1]["ok"] is True and lines[-1]["files"] and any("inbox events not" in w for w in lines[-1]["warnings"]), lines
        code, lines = raw("delete", "quiet", "--archived", data_arg=bad)
        assert code == 0 and lines[-1]["ok"] is True and not os.path.exists(os.path.join(box, "archive", "quiet.md")), lines
        assert open(mkpaths.project_db_in(mkpaths.project_folder(bad, home)), "rb").read() == b"this is not a database"
        # ids that resolve outside: .. and a header id holding a slash can never become a path
        for bad in ("..", "../x", ".deleted"):
            _, out = mi("delete", bad, code=2)
            assert out["ok"] is False, (bad, out)
        put("sneaky.md", tpl_text.replace('plan = "plan-inbox-and-parsing"', 'plan = "../../escape"', 1))
        _, out = mi("delete", "../../escape", code=2)
        assert out["ok"] is False and os.path.isfile(os.path.join(box, "sneaky.md")), out
        assert not os.path.exists(os.path.join(tmp, "escape")) and not os.path.exists(os.path.join(home, "escape"))
        os.unlink(os.path.join(box, "sneaky.md"))
        # the id in the .deleted name is made safe whatever the header says
        assert safe_name("../../x y") == "_.._x_y", safe_name("../../x y")
        assert "/" not in safe_name("a/b") and safe_name("...") == "plan"

        # the real inbox was never used
        assert os.path.realpath(box).startswith(os.path.realpath(tmp))
        print("mk-inbox self-test ok")
    finally:
        restore_env()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        sys.exit(main(sys.argv[1:]))
