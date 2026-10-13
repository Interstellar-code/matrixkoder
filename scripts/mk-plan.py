#!/usr/bin/env python3
"""MatrixKoder plan files — parse, check and edit TOML plans (C1/C2 in the plan body).
Python 3 stdlib only.

  parse <path>
  check <path>                     parse + the P1.2 rules, adds "warnings"
  set <path> --status draft|approved|running|done|parked
                                   approved runs check first; errors refuse the write
  add-run <path> --run ID          appends to runs; idempotent
  approve <path> [--data DIR] [--home DIR] [--run ID]
                                   with a store: validate, import a plan version, copy the file
                                   to plans/archive/<id>-v<n>.md and leave it in place (C13)
  import <path> [--dry-run] [--data DIR] [--home DIR]
                                   load one +++ plan into the store WITHOUT approving it (C4):
                                   one plans row, no tasks, no file change
  delete <id> [--data DIR] [--home DIR]
                                   refused on a live run; else drops the plans row and moves the
                                   file into plans/archive/ (never unlinks, C10)
  export <path> [--data DIR] [--home DIR]
                                   rebuild the markdown from the store; body kept verbatim

A plan file: first line +++, TOML until the next +++ line, the rest is free
markdown, never parsed. A leading UTF-8 BOM is kept: check, approve and export
round-trip those bytes. Every command prints ONE JSON line.
Errors: {"ok": false, "errors": ["line N: ..."]} exit 2; missing file exit 3.
set/add-run rewrite only the target header key's line(s) — the markdown body and
every other header line (comments included) stay byte-identical — via a temp
file + os.replace, and the rewritten text is re-parsed before anything is written.

Kill seam (tests only, never set in normal use): MK_PLAN_KILL_AFTER=commit|archive
raises SystemExit at that step of approve-with-a-store. commit is after the plans
row and tasks are committed and before the archive exists; archive is after the
archive file is written and fsynced and before the original is brought up to the
approved bytes. The next approve converges: it finishes a missing archive, or is
a no-op when the archive bytes already match. The original file is never removed.
"""
import argparse, contextlib, datetime, fnmatch, hashlib, importlib.util, json, os, re, shutil, stat, sys, tempfile, tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
_cspec = importlib.util.spec_from_file_location("mk_config", os.path.join(HERE, "mk-config.py"))
mk_config = importlib.util.module_from_spec(_cspec)
_cspec.loader.exec_module(mk_config)


def session_subagents():
    """session.subagents from <data>/matrixkoder.yaml, else the mk-config schema default. A phase
    without its own subagents key inherits this; any failure resolving or reading the config falls
    back to True — this tool must never fail because no config could be read."""
    try:
        lines = mk_config.read_lines(os.path.join(
            mk_config.mk_state.resolve_data(None), "matrixkoder.yaml"))
        return bool(mk_config.value_of(lines, mk_config.row_for(lines, "session.subagents"))[1])
    except Exception:
        return True

STATUSES = ("draft", "approved", "running", "done", "parked")
SIZES = ("trivial", "standard", "complex")
RISKS = ("read_only", "reversible", "destructive")
REVIEWS = ("light", "standard", "data-loss")
PLAN_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
PHASE_ID_RE = re.compile(r"[A-Z][A-Z0-9]{0,7}")
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ROUTE_RE = re.compile(r"\S+( \S+){1,2}")  # "<agent> <model> [<effort>]"; "" means Jev
TOP_KEYS = ("plan", "status", "runs", "gates", "phase")  # header keys consumed by C2
PHASE_KEYS = ("id", "title", "wave", "deps", "owned", "forbidden", "route", "reviewer", "size",
              "risk", "review", "qa", "commit", "subagents", "lane_group", "task", "parallel")
TASK_KEYS = ("id", "title", "acceptance", "refs", "subagent", "owned", "deps", "parallel",
             "route", "subtask")
SUB_KEYS = ("id", "title", "acceptance", "refs", "owned", "deps", "parallel", "route")
TOML_LINE_RE = re.compile(r"line (\d+)")


class Fail(Exception):
    def __init__(self, errors, code=2, **extra):
        super().__init__("; ".join(str(e) for e in errors))
        self.errors = [str(e) for e in errors]
        self.code, self.extra = code, extra


class Parser(argparse.ArgumentParser):
    def __init__(self, *a, **kw):
        kw.setdefault("allow_abbrev", False)  # no --sta/--b=evil prefix matching
        super().__init__(*a, **kw)

    def error(self, message):
        raise Fail([message])


# ---------- parse (P1.1) ----------

def load(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raise Fail([f"no such file: {path}"], code=3)
    except OSError as e:
        raise Fail([f"{type(e).__name__}: {e}"], code=3)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Fail([f"line 1: {path} is not valid UTF-8"])
    # a BOM is kept on the text so a later rewrite writes it back; only the parse strips it
    return text, hashlib.sha256(raw).hexdigest()


def split_plan(text):
    """(lines, close): lines[0] is the opening +++, lines[close] the closing one.
    A leading BOM stays on lines[0] so rewrites keep it; the +++ check ignores it."""
    lines = text.split("\n")
    if not lines or lines[0].lstrip("\ufeff").strip() != "+++":
        raise Fail(["line 1: first line must be '+++'"])
    for i in range(1, len(lines)):
        if lines[i].strip() == "+++":
            return lines, i
    raise Fail([f"line {len(lines)}: no closing '+++' for the header"])


def parse_header(header_lines, start=2):
    """TOML header → dict; tomllib's relative line is offset to a file line number.
    One trailing \r per line is dropped for PARSING ONLY: tomllib rejects the bare \r a joined
    CRLF header would end on, so a CRLF plan now parses like its LF twin. set/add-run instead
    split the raw lines and rewrite only the key's line(s), so the file keeps its own endings."""
    try:
        return tomllib.loads("\n".join(l[:-1] if l.endswith("\r") else l for l in header_lines))
    except tomllib.TOMLDecodeError as e:
        rel = TOML_LINE_RE.search(str(e))
        line = int(rel.group(1)) + start - 1 if rel else start
        raise Fail([f"line {line}: {e}"])


def as_list(v):
    if isinstance(v, list):
        return list(v)
    return [] if v is None or v == "" else [v]


def jsonable(v):
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if hasattr(v, "isoformat"):  # TOML dates are not JSON
        return v.isoformat()
    return v


def deps_list(v):
    """deps is a TOML array or one comma-separated string (also <run>:<id>)."""
    if isinstance(v, str):
        return [d.strip() for d in v.split(",") if d.strip()]
    return [str(d) for d in as_list(v)]


def parallel_of(item):
    return bool(item.get("parallel", False))


def route_of(item):
    """Keep the string form mk-plan already accepts, or an inline {agent, model, effort} table."""
    route = item.get("route", "")
    if isinstance(route, dict):
        return {k: route.get(k, "") for k in ("agent", "model", "effort")}
    return "" if route is None else str(route)


def sub_json(s):
    out = {"id": str(s.get("id", "")), "title": s.get("title", ""),
           "acceptance": s.get("acceptance", ""), "refs": as_list(s.get("refs")),
           "owned": as_list(s.get("owned")), "deps": deps_list(s.get("deps")),
           "parallel": parallel_of(s), "route": route_of(s)}
    for k, v in s.items():
        if k not in SUB_KEYS:
            out[k] = jsonable(v)
    return out


def task_json(t):
    out = {"id": str(t.get("id", "")), "title": t.get("title", ""),
           "acceptance": t.get("acceptance", ""), "refs": as_list(t.get("refs")),
           "subagent": bool(t.get("subagent", False)), "owned": as_list(t.get("owned")),
           "deps": deps_list(t.get("deps")), "parallel": parallel_of(t), "route": route_of(t),
           "subtasks": [sub_json(s) for s in as_list(t.get("subtask"))]}
    for k, v in t.items():  # unknown keys pass through
        if k not in TASK_KEYS:
            out[k] = jsonable(v)
    return out


def phase_json(ph):
    out = {"id": str(ph.get("id", "")), "title": ph.get("title", ""),
           "wave": ph.get("wave", 1), "deps": deps_list(ph.get("deps")),
           "owned": as_list(ph.get("owned")), "forbidden": as_list(ph.get("forbidden")),
           "route": ph.get("route", ""), "reviewer": ph.get("reviewer", ""),
           "size": ph.get("size", "standard"),
           "risk": ph.get("risk", "reversible"), "review": ph.get("review", "standard"),
           "qa": as_list(ph.get("qa")), "commit": ph.get("commit", ""),
           "subagents": bool(ph["subagents"]) if "subagents" in ph else session_subagents(),
           "lane_group": ph.get("lane_group", ""),
           "parallel": parallel_of(ph),
           "tasks": [task_json(t) for t in as_list(ph.get("task"))]}
    for k, v in ph.items():  # unknown keys pass through
        if k not in PHASE_KEYS:
            out[k] = jsonable(v)
    return out


def plan_json(header, sha):
    out = {"ok": True, "plan": header.get("plan", ""), "status": header.get("status", "draft"),
           "runs": as_list(header.get("runs")), "sha": sha,
           "gates": dict(header.get("gates") or {}),
           "phases": [phase_json(p) for p in as_list(header.get("phase"))]}
    for k, v in header.items():  # created/design/integration/unknown keys pass through
        if k not in TOP_KEYS:
            out[k] = jsonable(v)
    return out


def read_plan(path):
    """parse shared by parse/check/set/add-run: → (header dict, C2 dict, sha)."""
    text, sha = load(path)
    lines, close = split_plan(text)
    header = parse_header(lines[1:close])
    return header, plan_json(header, sha), sha


def body_md_of(text, close):
    """The markdown below the closing +++, the same bytes approve_file stores in plans.body_md."""
    body = "\n".join(text.split("\n")[close + 1:])
    return body[1:] if body.startswith("\n") else body


# ---------- glob overlap (same rules as mk-state.py owned globs) ----------

def norm_glob(g):
    while g.startswith("./"):
        g = g[2:]
    return g


def glob_variants(g):
    g = norm_glob(g)
    vs = {g}
    if re.search(r"[*?\[]", g):
        vs.add(g.replace("**/", ""))
    else:  # a literal path (or dir/) also covers its subtree
        vs.add(g.rstrip("/") + "/**")
    return vs


def globs_overlap(a, b):
    va, vb = glob_variants(a), glob_variants(b)
    if any(fnmatch.fnmatchcase(x, y) for x in va for y in vb) or any(
            fnmatch.fnmatchcase(x, y) for x in vb for y in va):
        return True
    na, nb = norm_glob(a), norm_glob(b)
    la, lb = (re.split(r"[*?\[]", g, maxsplit=1)[0] for g in (na, nb))
    wild = lambda g: bool(re.search(r"[*?\[]", g))
    return wild(na) and wild(nb) and (la.startswith(lb) or lb.startswith(la))


def owned_clash(a, b):
    return next((f"{x} ~ {y}" for x in a for y in b if globs_overlap(x, y)), None)


# ---------- check (P1.2) ----------

def find_cycle(deps):
    state, stack = {}, []

    def dfs(n):
        state[n], _ = 1, stack.append(n)
        for m in deps.get(n, []):
            if state.get(m) == 1:
                return stack[stack.index(m):] + [m]
            if state.get(m, 0) == 0 and m in deps:
                r = dfs(m)
                if r:
                    return r
        stack.pop()
        state[n] = 2
        return None

    for n in deps:
        if state.get(n, 0) == 0:
            r = dfs(n)
            if r:
                return r
    return None


def has_h1(body):
    """True when the markdown body carries a level-1 heading — the line that becomes
    plans.title. A '#' inside a fenced code block does not count."""
    fenced = False
    for line in body.split("\n"):
        st = line.lstrip()
        if st.startswith("```") or st.startswith("~~~"):
            fenced = not fenced
        elif not fenced and st.startswith("# "):
            return True
    return False


def check_plan(header, out, body=None):
    """→ (errors, warnings). Errors are exactly the P1.2 contract rules; anything
    advisory (cross-run deps, malformed id shapes) is a warning. `body` is the markdown
    below the closing +++ when the caller has it: the T1 header-key gaps are warnings
    there too, so a plan written before the key existed still checks ok."""
    errors, warnings = [], []
    phases = out["phases"]
    raw = as_list(header.get("phase"))

    if str(out["plan"]) and not PLAN_ID_RE.fullmatch(str(out["plan"])):
        warnings.append(f"plan id {out['plan']!r} does not match [a-z0-9][a-z0-9-]{{0,40}}")

    # T1 contract: the header keys that fill a plans column. A missing one is a gap in the
    # contract, never a rule violation, so it stays a warning (plans.status / plans.created
    # are columns added by D1; the key is header_json until then).
    if "status" not in header:
        warnings.append("no status key in the header; it is plans.status (a column added by "
                        "D1, header_json until then) — run init --plan wants approved")
    if "created" not in header:
        warnings.append("no created key in the header; it is plans.created (a column added by "
                        "D1, header_json until then) — the date the plan was drafted")
    if body is not None and not has_h1(body):
        warnings.append("body has no H1 line; the first '# ' line is what becomes plans.title")

    ids = [p["id"] for p in phases if p["id"]]
    for pid in sorted({i for i in ids if ids.count(i) > 1}):
        errors.append(f"duplicate phase id {pid}")
    tids = [t["id"] for p in phases for t in p["tasks"] if t["id"]]
    for tid in sorted({i for i in tids if tids.count(i) > 1}):
        errors.append(f"duplicate task id {tid}")
    sids = [s["id"] for p in phases for t in p["tasks"] for s in t.get("subtasks") or [] if s["id"]]
    for sid in sorted({i for i in sids if sids.count(i) > 1}):
        errors.append(f"duplicate subtask id {sid}")

    by_id = {p["id"]: p for p in phases if p["id"]}
    for n, p in enumerate(phases):
        pid = p["id"] or f"phase {n + 1}"
        if not p["id"]:
            warnings.append(f"phase {n + 1} has no id")
        elif not PHASE_ID_RE.fullmatch(p["id"]):
            warnings.append(f"phase id {pid!r} does not match [A-Z][A-Z0-9]{{0,7}}")
        w = raw[n].get("wave", 1) if n < len(raw) else 1
        if not isinstance(w, int) or isinstance(w, bool) or w < 1:
            warnings.append(f"{pid}: wave {w!r} is not an int >= 1")
        for d in p["deps"]:
            d = str(d)
            if ":" in d:
                if RUN_ID_RE.fullmatch(d.split(":", 1)[0]) and d.split(":", 1)[1]:
                    warnings.append(f"{pid}: dep {d} is cross-run (<run>:<task>); cannot verify it here")
                else:
                    warnings.append(f"{pid}: dep {d!r} is not a phase id or <run>:<task>")
            elif d in by_id:
                if by_id[d] is not p and by_id[d]["wave"] > p["wave"]:
                    errors.append(f"{pid}: dep {d} is in a later wave ({by_id[d]['wave']} > {p['wave']})")
            else:
                errors.append(f"{pid}: unknown dep {d}")
        if p["size"] not in SIZES:
            errors.append(f"{pid}: bad size {p['size']!r} ({'|'.join(SIZES)})")
        if p["risk"] not in RISKS:
            errors.append(f"{pid}: bad risk {p['risk']!r} ({'|'.join(RISKS)})")
        if p["review"] not in REVIEWS:
            errors.append(f"{pid}: bad review {p['review']!r} ({'|'.join(REVIEWS)})")
        if p["route"] != "" and not ROUTE_RE.fullmatch(str(p["route"])):
            errors.append(f'{pid}: bad route {p["route"]!r} ("" or "<agent> <model> [<effort>]")')
        if not isinstance(p["reviewer"], str):
            errors.append(f'{pid}: reviewer {p["reviewer"]!r} is not a string ("" = the orchestrator decides)')
        for t in p["tasks"]:
            tid = t["id"] or f"{pid} task"
            if not t["id"]:
                errors.append(f"{pid}: task has no id")
            if t["id"] and pid in by_id and not re.fullmatch(re.escape(pid) + r"\.\d+", t["id"]):
                warnings.append(f"task id {t['id']!r} is not <phase id>.<n> ({pid}.<n>)")
            for s in t.get("subtasks") or []:
                if not s["id"]:
                    errors.append(f"{tid}: subtask has no id")
                if s["id"] and t["id"] and not re.fullmatch(re.escape(t["id"]) + r"\.\d+", s["id"]):
                    warnings.append(f"subtask id {s['id']!r} is not <task id>.<m> ({t['id']}.<m>)")
                if isinstance(s.get("route"), dict):
                    pass
                elif s.get("route") not in ("", None) and not ROUTE_RE.fullmatch(str(s.get("route"))):
                    errors.append(f'{s["id"] or tid}: bad route {s.get("route")!r} ("" or "<agent> <model> [<effort>]")')
            if t["subagent"]:
                if not p["subagents"]:
                    warnings.append(f"{tid}: subagent=true outside a subagents=true phase")
                if not t["owned"]:
                    errors.append(f"{tid}: subagent task with no owned")
                elif p["owned"] and owned_clash(t["owned"], p["owned"]) is None:
                    warnings.append(f"{tid}: owned {t['owned']} is not a subset of phase {pid} owned {p['owned']}")
        subs = [t for t in p["tasks"] if t["subagent"]]
        for i in range(len(subs)):
            for j in range(i + 1, len(subs)):
                clash = owned_clash(subs[i]["owned"], subs[j]["owned"])
                if clash:
                    errors.append(f"{pid}: subagent tasks {subs[i]['id']} and {subs[j]['id']} share a file ({clash})")

    waves = {}
    for p in phases:
        waves.setdefault(p["wave"], []).append(p)
    for wave in sorted(waves):
        group = waves[wave]
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                clash = owned_clash(group[i]["owned"], group[j]["owned"])
                if clash:
                    errors.append(f"wave {wave}: {group[i]['id']} and {group[j]['id']} both own {clash}")

    lanes = {}
    for p in phases:
        if p["lane_group"]:
            lanes.setdefault(p["lane_group"], []).append(p)
    for g, group in sorted(lanes.items()):
        ws = sorted({p["wave"] for p in group})
        if len(ws) > 1:
            errors.append(f"lane_group {g}: phases in different waves ({', '.join(str(w) for w in ws)})")

    # same graph validate_tree walks: phases, tasks and subtasks, keyed by id.
    # a cross-run dep (<run>:<id>) is not an edge here — check cannot see the other run —
    # and stays the warning recorded above. a self-dep is its own one-node cycle.
    nodes = {}
    def add_node(item_id, item_deps):
        if not item_id or item_id in nodes:
            return
        nodes[item_id] = [str(d) for d in item_deps if ":" not in str(d)]
    for p in phases:
        add_node(p["id"], p["deps"])
        for t in p["tasks"]:
            add_node(t["id"], t.get("deps") or [])
            for s in t.get("subtasks") or []:
                add_node(s["id"], s.get("deps") or [])
    cycle = find_cycle(nodes)
    if cycle:
        errors.append("dep cycle: " + " -> ".join(cycle))
    return errors, warnings


# ---------- set / add-run (P1.3): rewrite only the key's line(s) ----------

def split_comment(seg):
    """(code, comment) — the comment starts at the first # outside a string."""
    q = None
    for i, ch in enumerate(seg):
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "#":
            return seg[:i], seg[i:]
    return seg, None


def key_span(lines, key, close):
    """Lines [start, end] (inclusive) of the top-level `key = ...` before any table;
    runs may span lines until its brackets close. None when the key is absent."""
    start = None
    for i in range(1, close):
        st = lines[i].lstrip()
        if st.startswith("["):
            return None  # [table] / [[phase]]: top-level keys end here
        if re.match(rf"{key}\s*=", st):
            start = i
            break
    if start is None:
        return None
    depth, q, seen = 0, None, False
    for j in range(start, close):
        for ch in lines[j]:
            if q:
                if ch == q:
                    q = None
            elif ch in "\"'":
                q = ch
            elif ch == "#":
                break  # rest of the line is a comment
            elif ch == "[":
                depth, seen = depth + 1, True
            elif ch == "]":
                depth -= 1
                if seen and depth <= 0:
                    return start, j
        if q is None and not seen:
            return start, j  # single-line value
        if '"""' in lines[j] or "'''" in lines[j]:
            raise Fail([f"line {j + 1}: multi-line strings are not supported for {key}"])
    return start, close - 1  # unterminated: the re-parse below refuses the write


def rebuild_line(prefix, value, old_code, comment):
    """prefix + quoted value + the old line's trailing whitespace/comment, \r kept."""
    code = old_code.rstrip("\r")
    ws = code[len(code.rstrip()):] or ("  " if comment else "")
    return prefix + value + (ws + comment if comment else "") + old_code[len(code):]


def rewrite(path, lines, start, end, newline):
    """Swap lines[start:end+1] for newline, re-parse, then write atomically."""
    changed = lines[:start] + [newline] + lines[end + 1:]
    text = "\n".join(changed)
    try:  # a broken result is never written
        new_lines, close = split_plan(text)
        header = parse_header(new_lines[1:close])
    except Fail as e:
        raise Fail([f"refusing to write: {e.errors[0]}"])
    write_atomic(path, text)
    return header


def write_atomic(path, text):
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)  # a read-only plan stays read-only
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        blob = text.encode("utf-8") if isinstance(text, str) else text
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)  # mkstemp makes 0600; keep the file's own mode
        os.replace(tmp, path)
        # fsync the directory so the new name survives a kill
        dirfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def set_status(path, value):
    if value not in STATUSES:
        raise Fail([f"bad status {value!r} ({'|'.join(STATUSES)})"])
    note = None
    if value == "approved":  # F3.1: approving runs the check first; errors refuse the write
        header, out, _ = read_plan(path)
        errors, warnings = check_plan(header, out)
        if errors:
            raise Fail(errors, warnings=warnings)
        # a cross-run dep stays a warning (check cannot see the other run) but the reply says so
        cross = [w for w in warnings if "cross-run" in w]
        if cross:
            note = "; ".join(cross)
    text, _ = load(path)
    lines, close = split_plan(text)
    span = key_span(lines, "status", close)
    if span is None:
        raise Fail([f"line {close + 1}: no top-level status key in the header"])
    start, end = span
    m = re.match(r"^(\s*status\s*=\s*)(.*)$", lines[start])
    if m is None:  # key_span already matched this shape; multi-line values are refused there
        raise Fail([f"line {start + 1}: cannot rewrite the status value"])
    code, comment = split_comment(m.group(2))
    newline = rebuild_line(m.group(1), f'"{value}"', code, comment)
    header = rewrite(path, lines, start, end, newline)
    if header.get("status") != value:
        raise Fail([f"refusing to write: status is {header.get('status')!r}, wanted {value!r}"])
    out = {"ok": True, "status": value}
    if note:
        out["warnings"] = [note] if isinstance(note, str) else note
    return out


def add_run(path, run_id):
    if not run_id or not RUN_ID_RE.fullmatch(run_id):
        raise Fail([f"bad run id {run_id!r} (letters, digits, . _ -; max 64)"])
    header, _, _ = read_plan(path)
    runs = as_list(header.get("runs"))
    if run_id in runs:  # idempotent: the existing entry stays as it was
        return {"ok": True, "runs": runs, "added": False}
    text, _ = load(path)
    lines, close = split_plan(text)
    span = key_span(lines, "runs", close)
    if span is None:
        raise Fail([f"line {close + 1}: no top-level runs key in the header"])
    start, end = span
    m = re.match(rf"^(\s*)runs\s*=\s*(.*)$", lines[start])
    if not m or ('"""' in lines[start] or "'''" in lines[start]):
        raise Fail([f"line {start + 1}: cannot rewrite the runs value (multi-line strings unsupported)"])
    code, comment = split_comment(lines[end])
    values = ", ".join(json.dumps(str(r)) for r in runs + [run_id])
    newline = rebuild_line(f"{m.group(1)}runs = ", f"[{values}]", code, comment)
    header = rewrite(path, lines, start, end, newline)
    if run_id not in as_list(header.get("runs")):
        raise Fail(["refusing to write: run id missing after re-parse"])
    return {"ok": True, "runs": as_list(header.get("runs")), "added": True}


# ---------- commands ----------

def cmd_parse(a):
    return read_plan(a.path)[1]


def cmd_check(a):
    header, out, _ = read_plan(a.path)
    text, _ = load(a.path)
    _lines, close = split_plan(text)
    errors, warnings = check_plan(header, out, body_md_of(text, close))
    if errors:
        raise Fail(errors, warnings=warnings)
    out["warnings"] = warnings
    return out


def sync_store_status(path, data, home, status):
    """Keep plans.status (a column only once D1.1 adds it) in step with the file. A store without
    the column, no store at all, or a plan the store does not know: nothing to do, never an error —
    the file is still the record for every command that does not read the store."""
    data = resolve_data_arg(data)
    if not store_present(data):
        return None
    try:
        header, out, _sha = read_plan(path)
    except Fail:
        return None
    plan_id = str(out.get("plan") or "")
    if not plan_id:
        return None
    db = _mkdb()
    home = db.resolve_home(home)
    con = db.open_project(home, data_dir=data)
    try:
        if "status" not in plan_columns(con):
            return None
        if not con.execute("SELECT 1 FROM plans WHERE id=?", (plan_id,)).fetchone():
            return None
        con.execute("UPDATE plans SET status=? WHERE id=?", (status, plan_id))
        con.commit()
    finally:
        con.close()
    return plan_id


def cmd_set(a):
    """set --status parked and back is pause/resume (C10): the file is rewritten as before and,
    when a store holds this plan, plans.status follows (guarded on the column existing)."""
    out = set_status(a.path, a.status)
    plan_id = sync_store_status(a.path, a.data, a.home, a.status)
    if plan_id:
        out["plan"] = plan_id
        out["store"] = True
    return out


def cmd_add_run(a):
    return add_run(a.path, a.run)


# ---------- approve / export (D4): the store is optional ----------

def _mkdb():
    spec = importlib.util.spec_from_file_location("mkdb", os.path.join(HERE, "mkdb.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def store_present(data):
    """A store exists only when <data>/mk.db is already there. Never create one."""
    if not data:
        return False
    return os.path.isfile(os.path.join(os.path.abspath(data), "mk.db"))


# the data-dir resolution below is mk-state.py resolve_data's order, copied here on purpose:
# mk-plan must never import mk-state (C3 — the plan tool is only ever a subprocess), and it must
# not edit it. Same order, same "never create a store to answer a read" rule.


def mk_home():
    # one MK home rule (C16): either name; mkdb.resolve_home checks that both agree
    return os.path.expanduser(os.environ.get("MK_HOME") or os.environ.get("MATRIXKODER_HOME") or "~/.matrixkoder")


def main_worktree(top):
    """A linked worktree (.git is a file + commondir) shares the main checkout's data dir."""
    try:
        with open(os.path.join(top, ".git")) as f:
            gitdir = f.read().split("gitdir:", 1)[1].strip()
        gitdir = os.path.join(top, gitdir)
        with open(os.path.join(gitdir, "commondir")) as f:
            common = os.path.normpath(os.path.join(gitdir, f.read().strip()))
    except (OSError, IndexError):  # .git is a directory, or a submodule (no commondir)
        return top
    return os.path.dirname(common) if os.path.basename(common) == ".git" else top


def repo_top():
    """The main checkout of the nearest git repo above cwd, or None when there is none.
    The walk stops before $HOME; a linked worktree maps to its main checkout."""
    d, stop = os.path.realpath(os.getcwd()), os.path.realpath(os.path.expanduser("~"))
    while not os.path.exists(os.path.join(d, ".git")):  # nearest repo wins
        if os.path.dirname(d) == d or d == stop:
            return None
        d = os.path.dirname(d)
    if d == stop:
        return None
    return main_worktree(d)


def registered_data_dir(top):
    """The data dir this repo registered: the root projects table first, then <home>/projects.json.
    Neither read ever creates <home>/mk.db (the root store is checked before it is opened)."""
    home = mk_home()
    root = os.path.join(home, "mk.db")
    if os.path.exists(root):
        try:
            con = _mkdb().open_root(home)
            try:
                row = con.execute(
                    "SELECT data_dir FROM projects WHERE repo=? OR repo=? ORDER BY slug LIMIT 1",
                    (top, os.path.realpath(top))).fetchone()
            finally:
                con.close()
        except Exception:
            row = None  # an unreadable root store is not a reason to fail a plan command
        if row and isinstance(row["data_dir"], str) and row["data_dir"].strip():
            return os.path.normpath(os.path.join(top, os.path.expanduser(row["data_dir"].strip())))
    try:  # the wizard's home mode: ~/.matrixkoder/projects.json {"<repo>": {"data_dir": ...}}
        with open(os.path.join(home, "projects.json")) as f:
            projects = json.load(f)
        entry = projects.get(top) or projects.get(os.path.realpath(top))
        listed = entry.get("data_dir") if isinstance(entry, dict) else None
    except (OSError, ValueError, AttributeError):
        listed = None
    if isinstance(listed, str) and listed.strip():
        return os.path.normpath(os.path.join(top, os.path.expanduser(listed.strip())))
    return None


def resolve_data_arg(arg):
    """--data > $MATRIXKODER_DATA > the data dir this repo registered > <main checkout>/.matrixkoder.
    None means "no store": no --data, no env and no git repo above cwd (mk-state refuses instead;
    here a plan file must stay usable without a store)."""
    if arg:
        return os.path.abspath(os.path.expanduser(arg))
    env = os.environ.get("MATRIXKODER_DATA")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    top = repo_top()
    if top is None:
        return None
    return registered_data_dir(top) or os.path.join(top, ".matrixkoder")


def side_store_warning(path):
    """A file-only answer next to an existing mk.db is the hermes-switchui trap: approve silently
    skipped the store that was on. Named in the reply instead of staying quiet."""
    side = os.path.join(os.path.dirname(os.path.abspath(path)), "mk.db")
    if not os.path.isfile(side):
        return None
    return (f"file-only: no store resolved, but {side} exists — pass "
            f"--data {os.path.dirname(side)} or set MATRIXKODER_DATA to approve into it")


def now_local():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def approved_bytes(raw):
    """The file bytes with the first draft status value flipped to approved. BOM and CRLF stay."""
    if not isinstance(raw, (bytes, bytearray)):
        raw = raw.encode("utf-8")
    lines = raw.splitlines(keepends=True)
    for i, line in enumerate(lines):
        bare = line.lstrip(b"\xef\xbb\xbf").lstrip()
        if bare.startswith(b"status") and b'"draft"' in line:
            lines[i] = line.replace(b'"draft"', b'"approved"', 1)
            break
    return b"".join(lines)


def flat_items(out):
    """(id, type, parent, title, acceptance, owned, deps, parallel, route) in parent-first order."""
    rows = []
    for ph in out["phases"]:
        rows.append((ph["id"], "phase", None, ph.get("title") or "", "", ph.get("owned") or [],
                     ph.get("deps") or [], bool(ph.get("parallel")), ph.get("route") or ""))
        for task in ph["tasks"]:
            rows.append((task["id"], "task", ph["id"], task.get("title") or "",
                         task.get("acceptance") or "", task.get("owned") or [],
                         task.get("deps") or [], bool(task.get("parallel")), task.get("route") or ""))
            for sub in task.get("subtasks") or []:
                rows.append((sub["id"], "subtask", task["id"], sub.get("title") or "",
                             sub.get("acceptance") or "", sub.get("owned") or [],
                             sub.get("deps") or [], bool(sub.get("parallel")), sub.get("route") or ""))
    return rows


def route_json_of(route):
    """mkdb wants {"agent","model","effort"} or empty. A route string becomes that object."""
    if route in ("", None):
        return None
    if isinstance(route, dict):
        return json.dumps({k: str(route.get(k, "")) for k in ("agent", "model", "effort")}, sort_keys=True)
    parts = str(route).split()
    if len(parts) == 2:
        agent, model, effort = parts[0], parts[1], ""
    elif len(parts) == 3:
        agent, model, effort = parts
    else:
        raise Fail([f'bad route {route!r} ("" or "<agent> <model> [<effort>]")'])
    return json.dumps({"agent": agent, "effort": effort, "model": model}, sort_keys=True)


def plan_columns(con):
    """The plans columns this store actually has. D1.1 adds status/created/design/integration;
    until then they live in header_json only and must never appear in an INSERT."""
    return {r["name"] for r in con.execute("PRAGMA table_info(plans)")}


def plan_title(body_md, plan_id):
    """C3: plans.title is the H1 text after the em dash (`# <id> — <title>`); no H1 -> the plan id.
    A '#' inside a fenced block is not a heading, same rule as has_h1."""
    fenced, h1 = False, None
    for line in (body_md or "").split("\n"):
        st = line.lstrip()
        if st.startswith("```") or st.startswith("~~~"):
            fenced = not fenced
        elif not fenced and st.startswith("# "):
            h1 = st[2:].strip()
            break
    if not h1:
        return plan_id
    head, dash, tail = h1.partition("—")
    text = tail.strip() if (dash and tail.strip()) else h1
    return text or plan_id


def header_json_of(out, sha, body_md, status):
    """The header_json approve and import both write: the sha of the file, the status, runs,
    gates, phases and the body. Unknown header keys (created, design, integration, …) live here
    on a store without the D1.1 columns."""
    return json.dumps({"sha": sha, "status": status, "runs": out["runs"], "gates": out["gates"],
                       "phases": out["phases"], "body": body_md}, sort_keys=True)


def write_plan_row(con, plan_id, values, update=None):
    """One plans row, upserted by id. Every column is filtered through PRAGMA table_info, so the
    four D1.1 columns are written only once they exist and an older store is never sent a column
    it lacks. `update` names the columns a conflicting row is overwritten with (default: all)."""
    present = plan_columns(con)
    values = {k: v for k, v in values.items() if k in present}
    if not values:
        return
    cols = sorted(values)
    updated = [c for c in cols if c in set(update)] if update else cols
    if not updated:  # an upsert must update something; fall back to every written column
        updated = cols
    marks = ", ".join("?" * (len(cols) + 1))
    sets = ", ".join(f"{c}=excluded.{c}" for c in updated)
    con.execute(f"INSERT INTO plans(id, {', '.join(cols)}) VALUES ({marks}) "
                f"ON CONFLICT(id) DO UPDATE SET {sets}",
                [plan_id] + [values[c] for c in cols])


def new_plan_columns(header, out):
    """status, created, design and integration as column values. design and integration are JSON
    arrays (the header key may be one value); created is the TOML date, stored as its text."""
    return {"status": str(out.get("status") or "draft"),
            "created": str(jsonable(header.get("created", ""))),
            "design": json.dumps(as_list(header.get("design"))),
            "integration": json.dumps(as_list(header.get("integration")))}


def validate_tree(out, db, con, run):
    """Ids, self-deps and cycles, named the way mkdb names them. Nothing is written."""
    rows = flat_items(out)
    ids = []
    for tid, typ, parent, *_rest in rows:
        if not tid:
            raise Fail(["item has no id"])
        if tid in ids:
            raise Fail([f"duplicate id {tid}"])
        ids.append(tid)
        if parent and parent not in ids:
            raise Fail([f"missing parent: {parent}"])
    known = set(ids)
    # also ids already stored in this run, so a <run>:<id> of another run can be checked below
    stored = {(r["run"], r["id"]) for r in con.execute("SELECT run, id FROM tasks")}
    edges = {}
    for tid, typ, parent, title, acceptance, owned, deps, parallel, route in rows:
        me = (run, tid)
        targets = []
        for dep in deps:
            dep = str(dep)
            if ":" in dep:
                other, _, rest = dep.partition(":")
                if not other or not rest or ":" in rest:
                    raise Fail([f"bad dep {dep!r}: expected <run>:<id>"])
                target = (other, rest)
                if target not in stored and not (other == run and rest in known):
                    raise Fail([f"missing dep: {dep} is not a task in this project"])
            else:
                target = (run, dep)
                if dep not in known and target not in stored:
                    raise Fail([f"missing dep: {dep} is not a task in this project"])
            if target == me:
                raise Fail([f"self-dep: {tid} depends on itself"])
            targets.append(target)
        edges[me] = targets
        route_json_of(route)  # refuse a bad route before any write
    # cycles over the plan graph plus stored deps of this run
    for row in con.execute("SELECT id, deps FROM tasks WHERE run=?", (run,)):
        key = (run, row["id"])
        if key in edges:
            continue
        edges[key] = []
        for dep in db._deps_of(row["deps"]):
            edges[key].append(db._dep_target(run, dep))
    for me in list(edges):
        path = db._cycle_through(me, edges)
        if path:
            raise Fail(["cycle: " + " -> ".join(f"{r}:{t}" for r, t in path)])


def approve_file(path, data, home, run):
    """No store: validate and mark approved, exactly as set --status approved.
    With a store: validate, write the plan version and its tasks, copy the approved bytes into
    the archive and leave the original file in place (C13 — approve copies, never moves)."""
    data = resolve_data_arg(data)
    header, out, sha = read_plan(path)
    errors, warnings = check_plan(header, out)
    if errors:
        raise Fail(errors, warnings=warnings)
    if not store_present(data):
        side = side_store_warning(path)
        if side:  # a store next to the file was skipped: say so instead of going quiet
            warnings = warnings + [side]
        if out["status"] == "approved":
            return {"ok": True, "status": "approved", "store": False, "noop": True,
                    "warnings": warnings}
        set_status(path, "approved")
        return {"ok": True, "status": "approved", "store": False, "warnings": warnings}
    db = _mkdb()
    home = db.resolve_home(home)
    plan_id = str(out["plan"])
    if not plan_id:
        raise Fail(["plan has no id"])
    run_id = run or (out["runs"][0] if out["runs"] else plan_id)
    if not RUN_ID_RE.fullmatch(str(run_id)):
        raise Fail([f"bad run id {run_id!r} (letters, digits, . _ -; max 64)"])
    text, _ = load(path)
    _lines, close = split_plan(text)
    body = "\n".join(_lines[close + 1:])
    if body.startswith("\n"):
        body_md = body[1:]
    else:
        body_md = body
    con = db.open_project(home, data_dir=data)
    added = []
    try:
        validate_tree(out, db, con, run_id)
        row = con.execute(
            "SELECT version, header_json, archived_path FROM plans WHERE id=?", (plan_id,)).fetchone()
        prev = {}
        if row and row["header_json"]:
            try:
                prev = json.loads(row["header_json"])
            except ValueError:
                prev = {}
        # the archived file holds the approved bytes; a second approve of those bytes is a no-op.
        # a kill after the archive was written leaves the original on its old bytes: copy them.
        current = open(path, "rb").read()
        approved = approved_bytes(current)
        if row and os.path.isfile(row["archived_path"] or ""):
            archived_now = open(row["archived_path"], "rb").read()
            if archived_now == approved or archived_now == current:
                # the archive is the approved version and the original stays (C13). a kill between
                # the archive and the original leaves the file on its pre-approve bytes, so bring
                # it back to what the archive holds; a separate copy that already matches is
                # left byte-identical.
                if (archived_now != current
                        and os.path.abspath(path) != os.path.abspath(row["archived_path"])):
                    write_atomic(path, archived_now)
                return {"ok": True, "status": "approved", "store": True, "noop": True,
                        "version": row["version"], "archived": row["archived_path"],
                        "tasks_run": run_id, "warnings": warnings}
        # a kill after the plans row committed and before the archive existed: finish THAT
        # version instead of inserting another. the row's archived_path is where it belongs.
        resume = None
        if row and row["archived_path"] and not os.path.isfile(row["archived_path"]):
            resume = row["archived_path"]
        version = row["version"] if resume else (row["version"] if row else 0) + 1
        # a new version replaces this run's tasks from the previous import; other runs stay.
        # a resume keeps the tasks the killed approve already committed.
        if not resume:
            # drop this plan's tasks whether or not a plans row exists yet: run init may have
            # inserted the same ids, and a re-approve replaces them
            for tid, *_rest in flat_items({"phases": (prev.get("phases") or out["phases"])}):
                con.execute("DELETE FROM tasks WHERE run=? AND id=?", (run_id, tid))
            con.commit()
        archive = os.path.join(data, "plans", "archive", f"{plan_id}-v{version}.md")
        header_json = header_json_of(out, sha, body_md, "approved")
        # tasks first (each add_task commits its own transaction), plans row next, archive last.
        # a later failure deletes only the tasks this call inserted. a resume only writes the archive.
        try:
            items = [] if resume else flat_items(out)
            # pass 1: insert with empty deps so a forward dep (a later phase or sibling) exists
            for tid, typ, parent, title, acceptance, owned, deps, parallel, route in items:
                db.add_task(con, run_id, tid, typ, title=title, parent=parent,
                            deps="", route_json=route_json_of(route))
                added.append(tid)
                con.execute(
                    "UPDATE tasks SET acceptance=?, owned=?, parallel=? WHERE run=? AND id=?",
                    (acceptance, ",".join(str(x) for x in owned), 1 if parallel else 0, run_id, tid))
                con.commit()
            # pass 2: store the authored deps and let mkdb name a cycle, self-dep or missing id
            for tid, typ, parent, title, acceptance, owned, deps, parallel, route in items:
                dep_text = ",".join(str(d) for d in deps)
                db.check_deps(con, run_id, tid, dep_text)
                con.execute("UPDATE tasks SET deps=? WHERE run=? AND id=?", (dep_text, run_id, tid))
                con.commit()
            db._begin(con)
            try:
                row_values = dict(version=version, title=plan_title(body_md, plan_id),
                                  header_json=header_json, body_md=body_md,
                                  approved_at=now_local(), archived_path=archive,
                                  **new_plan_columns(header, out))
                row_values["status"] = "approved"  # this write IS the approval, the file may say draft
                write_plan_row(con, plan_id, row_values)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        except Exception as e:
            for tid in added:
                con.execute("DELETE FROM tasks WHERE run=? AND id=?", (run_id, tid))
            con.commit()
            if isinstance(e, Fail):
                raise
            msg = getattr(e, "args", ("",))[0]
            raise Fail([str(msg)])
        # approved bytes go to the archive first (write_atomic fsyncs), then the original
        # is brought up to the approved bytes. a kill after the commit but before the archive, or
        # after the archive but before the original was refreshed, is recovered by the next approve.
        if os.environ.get("MK_PLAN_KILL_AFTER") == "commit":
            raise SystemExit("MK_PLAN_KILL_AFTER=commit")
        try:
            write_atomic(archive, approved)
        except Exception as e:
            for tid in added:
                con.execute("DELETE FROM tasks WHERE run=? AND id=?", (run_id, tid))
            con.execute("DELETE FROM plans WHERE id=? AND version=?", (plan_id, version))
            con.commit()
            raise Fail([f"archive failed: {e}"])
        if os.environ.get("MK_PLAN_KILL_AFTER") == "archive":
            raise SystemExit("MK_PLAN_KILL_AFTER=archive")
        # C13: approve copies, it never moves. the original file stays in place and now carries
        # the approved bytes, so `run init --plan <original path>` still resolves after an approve
        # (the hermes-switchui issue). a write failure here is not a lost approve: the archive
        # and the row are already there and the next approve converges.
        if current != approved and os.path.abspath(path) != os.path.abspath(archive):
            write_atomic(path, approved)
        return {"ok": True, "status": "approved", "store": True, "version": version,
                "archived": archive, "tasks_run": run_id, "warnings": warnings}
    finally:
        con.close()


def toml_quote(s):
    return json.dumps(str(s))


def toml_array(items):
    return "[" + ", ".join(toml_quote(x) for x in items) + "]"


def route_toml(route):
    if isinstance(route, dict):
        return "{ " + ", ".join(f"{k} = {toml_quote(route.get(k, ''))}" for k in ("agent", "model", "effort")) + " }"
    return toml_quote(route or "")


def export_plan(path, data, home):
    """Rebuild markdown from the store. The TOML header matches the archived bytes; body verbatim."""
    data = resolve_data_arg(data)
    if not store_present(data):
        raise Fail(["no store: export needs <data>/mk.db"])
    db = _mkdb()
    home = db.resolve_home(home)
    # the path may already be gone; the plan id is the file stem, else read the file
    plan_id = os.path.splitext(os.path.basename(path))[0]
    if os.path.isfile(path):
        _h, out, _s = read_plan(path)
        if out.get("plan"):
            plan_id = str(out["plan"])
    con = db.open_project(home, data_dir=data)
    try:
        row = con.execute(
            "SELECT version, header_json, body_md, archived_path FROM plans WHERE id=? ORDER BY version DESC LIMIT 1",
            (plan_id,)).fetchone()
    finally:
        con.close()
    if not row:
        raise Fail([f"no plan {plan_id} in the store"])
    header = json.loads(row["header_json"])
    # byte-equal header: the archived file is the source of the header bytes
    archived = row["archived_path"]
    if archived and os.path.isfile(archived):
        with open(archived, "rb") as fh:
            raw = fh.read()
        rebuilt = raw.decode("utf-8")  # BOM kept; split_plan accepts it
        split_plan(rebuilt)  # refuse a corrupt archive before writing it back
    else:
        raise Fail([f"archived plan missing: {archived}"])
    if path != archived:
        write_atomic(path, raw)  # the archive bytes, CRLF included
    return {"ok": True, "plan": plan_id, "version": row["version"], "path": archived, "text": rebuilt}


def cmd_approve(a):
    return approve_file(a.path, a.data, a.home, a.run)


def cmd_export(a):
    out = export_plan(a.path, a.data, a.home)
    out.pop("text", None)
    return out


# ---------- import / delete (D1.2) ----------

def import_file(path, data, home, dry_run=False):
    """Load one +++ plan into the store without approving it (C4): a plans row and nothing else.
    No task rows, no file change, no status flip — approve is the command that does those.
    Idempotent: the same bytes upsert onto the same row and change nothing."""
    data = resolve_data_arg(data)
    text, sha = load(path)
    # a file that is not a plan, or a header that does not parse, is refused with the reason in
    # the reply and exit 0 — it is a "this file is not importable" answer, not a failed command.
    try:
        lines, close = split_plan(text)
        header = parse_header(lines[1:close])
    except Fail as e:
        raise Fail(e.errors, code=0, reason=e.errors[0])
    out = plan_json(header, sha)
    plan_id = str(out["plan"])
    if not plan_id:
        raise Fail(["the header has no plan id (C4: only a +++ file with plan = \"<id>\" imports)"],
                   code=0, reason="no plan id in the header")
    body_md = body_md_of(text, close)
    title = plan_title(body_md, plan_id)
    if not store_present(data):
        raise Fail([f"no store: import needs <data>/mk.db ({data})"])
    db = _mkdb()
    home = db.resolve_home(home)
    header_json = header_json_of(out, sha, body_md, str(out["status"] or "draft"))
    con = db.open_project(home, data_dir=data)
    try:
        row = con.execute("SELECT version FROM plans WHERE id=?", (plan_id,)).fetchone()
        version = row["version"] if row and row["version"] is not None else 0
        if not dry_run:
            db._begin(con)
            try:
                cols = new_plan_columns(header, out)
                write_plan_row(con, plan_id, dict(
                    version=version, title=title, header_json=header_json, body_md=body_md,
                    **cols),
                    # a re-import must not move the plan's version or stamp it as approved again;
                    # status stays the store's once set (after import the store is the source)
                    update=["title", "header_json", "body_md", "created", "design", "integration"])
                if row and "status" in plan_columns(con):
                    con.execute("UPDATE plans SET status=? WHERE id=? AND status IS NULL",
                                (cols["status"], plan_id))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        return {"ok": True, "store": True, "plan": plan_id, "version": version,
                "title": title, "noop": bool(row), "dry_run": bool(dry_run)}
    finally:
        con.close()


def delete_plan(plan_id, data, home):
    """C10: drop the plans row and archive the file — never unlink it (C13). Refused while any
    run of this plan is still active; tasks of finished runs stay."""
    if not PLAN_ID_RE.fullmatch(plan_id):
        raise Fail([f"bad plan id {plan_id!r} ({PLAN_ID_RE.pattern})"])
    data = resolve_data_arg(data)
    if not store_present(data):
        raise Fail([f"no store: delete needs <data>/mk.db ({data})"])
    db = _mkdb()
    home = db.resolve_home(home)
    con = db.open_project(home, data_dir=data)
    try:
        row = con.execute("SELECT archived_path FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise Fail([f"no plan {plan_id} in the store"])
        live = [r["id"] for r in con.execute(
            "SELECT id FROM runs WHERE plan=? AND status='active' ORDER BY id", (plan_id,))]
        if live:
            raise Fail([f"refusing to delete {plan_id}: live run(s) {', '.join(live)} — "
                        f"close the run first (C10)"])
        con.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        con.commit()
    finally:
        con.close()
    # the file moves to the archive only after the row is gone; a name already taken gets a
    # timestamp suffix so an earlier archived version is never overwritten
    src = os.path.join(data, "plans", f"{plan_id}.md")
    moved = None
    if os.path.isfile(src):
        dst = os.path.join(data, "plans", "archive", os.path.basename(src))
        n = 0
        while os.path.exists(dst):
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            n += 1
            dst = os.path.join(data, "plans", "archive", f"{plan_id}-{stamp}" + (f"-{n}" if n > 1 else "") + ".md")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        moved = dst
    return {"ok": True, "plan": plan_id, "moved": moved}


def cmd_import(a):
    return import_file(a.path, a.data, a.home, a.dry_run)


def cmd_delete(a):
    return delete_plan(a.plan, a.data, a.home)


def build_parser():
    p = Parser(prog="mk-plan.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, func in (("parse", cmd_parse), ("check", cmd_check)):
        c = sub.add_parser(name)
        c.add_argument("path")
        c.set_defaults(func=func)
    s = sub.add_parser("set")
    s.add_argument("path"), s.add_argument("--status", required=True)
    s.add_argument("--data", default=None), s.add_argument("--home", default=None)
    s.set_defaults(func=cmd_set)
    r = sub.add_parser("add-run")
    r.add_argument("path"), r.add_argument("--run", required=True)
    r.set_defaults(func=cmd_add_run)
    for name, func in (("approve", cmd_approve), ("export", cmd_export), ("import", cmd_import)):
        c = sub.add_parser(name)
        c.add_argument("path")
        c.add_argument("--data", default=None)
        c.add_argument("--home", default=None)
        if name == "approve":
            c.add_argument("--run", default=None)
        if name == "import":
            c.add_argument("--dry-run", action="store_true")
        c.set_defaults(func=func)
    d = sub.add_parser("delete")
    d.add_argument("plan")
    d.add_argument("--data", default=None)
    d.add_argument("--home", default=None)
    d.set_defaults(func=cmd_delete)
    return p


def main(argv):
    try:
        a = build_parser().parse_args(argv)
        out, code = a.func(a), 0
    except Fail as e:
        out, code = {"ok": False, "errors": e.errors, **e.extra}, e.code
    print(json.dumps(out, default=str))
    return code


# ---------- self-test ----------

def self_test():
    import shutil, subprocess
    tmp = tempfile.mkdtemp(prefix="mk-plan-test-")
    # every parse reads session.subagents, which resolves the data dir: pin it here so the run can
    # never read the main checkout's .matrixkoder/ (and fail if it would)
    restore_data = mk_config.pin_tmp_data(tmp)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    real_plan = os.path.join(root, "scripts", "fixtures", "plan-link.md")  # a real plan, kept as a fixture
    template = os.path.join(root, "skills", "matrixkoder", "templates", "plan.md")

    def mk(*args, code=0):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), *args],
                           capture_output=True, text=True)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    def write(name, text):
        p = os.path.join(tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def plan_text(phases="", status='status = "draft"', runs="runs = []", plan='plan = "t1"',
                  top="", head_extra=""):
        return (f"+++\n{plan}\n{status}\ncreated = 2026-10-08\n{runs}\n{top}\n"
                f"[gates]\ntest = \"true\"\n{head_extra}\n{phases}+++\n\n# body\nmarkdown stays\n")

    def phase(pid, wave=1, deps="", owned="", route="", size="standard", risk="reversible",
              review="standard", subagents=False, lane_group="", extra="", tasks=""):
        ow = f'owned = [{owned}]' if owned else "owned = []"
        rt = f'route = "{route}"' if route else 'route = ""'
        lg = f'lane_group = "{lane_group}"' if lane_group else 'lane_group = ""'
        sa = "true" if subagents else "false"
        dp = f"deps = [{deps}]" if deps else "deps = []"
        return (f'[[phase]]\nid = "{pid}"\ntitle = "phase {pid}"\nwave = {wave}\n{dp}\n{ow}\n'
                f'forbidden = []\n{rt}\nsize = "{size}"\nrisk = "{risk}"\nreview = "{review}"\n'
                f"qa = []\ncommit = \"\"\nsubagents = {sa}\n{lg}\n{extra}{tasks}")

    def task(tid, title="t", acceptance="a", refs="", subagent=False, owned="", extra=""):
        sa = "true" if subagent else "false"
        rf = f'refs = [{refs}]' if refs else "refs = []"
        ow = f'owned = [{owned}]' if owned else "owned = []"
        return (f"  [[phase.task]]\n  id = \"{tid}\"\n  title = \"{title}\"\n"
                f"  acceptance = \"{acceptance}\"\n  {rf}\n  subagent = {sa}\n  {ow}\n{extra}")

    try:
        # P1.1 parse — the two real files (read-only) must give the C2 JSON
        out = mk("parse", template)
        with open(template, "rb") as f:
            assert out["ok"] and out["sha"] == hashlib.sha256(f.read()).hexdigest()
        assert out["plan"] == "feature-id" and out["status"] == "draft" and out["runs"] == []
        assert set(out["gates"]) == {"test", "typecheck", "selftest", "validate"}
        p0 = out["phases"][0]
        assert p0["id"] == "P1" and p0["wave"] == 1 and p0["lane_group"] == "" and p0["subagents"] is True  # template default (user 2026-10-09)
        assert p0["reviewer"] == "opencode opencode-go/kimi-k3"  # template's reviewer example passes through
        assert p0["tasks"][0]["id"] == "P1.1" and p0["tasks"][0]["subagent"] is False
        assert out["created"] == "2026-10-08" and out["design"] == []  # unknown/extra keys pass through

        out = mk("parse", real_plan)
        assert out["ok"] and out["plan"] == "plan-link" and out["status"] == "approved"
        assert [p["id"] for p in out["phases"]] == ["P1", "P2", "P3"]
        assert [t["id"] for t in out["phases"][0]["tasks"]] == ["P1.1", "P1.2", "P1.3"]
        assert all(p["wave"] == 1 and p["lane_group"] == "A" and p["deps"] == [] for p in out["phases"])
        assert all(p["reviewer"] == "" for p in out["phases"])  # optional key defaults to ""
        assert out["phases"][0]["review"] == "data-loss" and out["phases"][0]["size"] == "standard"

        # P1.2 check — both real files are clean (ok:true, exit 0, no warnings):
        # missing optional keys are quiet, and neither file carries a real problem
        out = mk("check", template)
        assert out["ok"] and out["warnings"] == [] and out["sha"]
        out = mk("check", real_plan)
        assert out["ok"] and out["sha"] and out["warnings"] == [], out["plan"]

        # a phase without its own subagents key inherits session.subagents (yaml), else the
        # schema default true; a phase that sets it keeps its own value
        def parse_env(p, datadir):
            r = subprocess.run([sys.executable, os.path.abspath(__file__), "parse", p],
                               capture_output=True, text=True,
                               env=dict(os.environ, MATRIXKODER_DATA=datadir))
            lines = [x for x in r.stdout.strip().splitlines() if x]
            assert r.returncode == 0 and len(lines) == 1, (p, r.stdout, r.stderr)
            return json.loads(lines[0])

        nokey = ('[[phase]]\nid = "P1"\ntitle = "t"\nwave = 1\ndeps = []\nowned = []\n'
                 'forbidden = []\nroute = ""\nsize = "standard"\nrisk = "read_only"\n'
                 'review = "light"\nqa = []\ncommit = ""\n\n')
        dsub, dempty = os.path.join(tmp, "data-sub"), os.path.join(tmp, "data-empty")
        os.makedirs(dsub), os.makedirs(dempty)
        with open(os.path.join(dsub, "matrixkoder.yaml"), "w") as f:
            f.write("session:\n  subagents: false\n")
        p = write("inherit-nokey.md", plan_text(phases=nokey))
        assert parse_env(p, dsub)["phases"][0]["subagents"] is False  # inherited from the yaml
        assert parse_env(p, dempty)["phases"][0]["subagents"] is True  # the schema default
        p = write("inherit-own.md", plan_text(phases=phase("P1", subagents=True) + "\n"))
        assert parse_env(p, dsub)["phases"][0]["subagents"] is True  # its own key wins

        # a done/parked plan may have no phases
        for st in ("done", "parked"):
            p = write(f"done-{st}.md", plan_text(status=f'status = "{st}"'))
            out = mk("parse", p)
            assert out["ok"] and out["phases"] == []
            assert mk("check", p)["ok"]

        # unknown keys pass through (top, phase, task)
        p = write("unknown.md", plan_text(
            top='extra_top = "y"', phases=phase("P1", extra='foo = "bar"\n',
                                                tasks=task("P1.1", extra="  zip = 1\n")) + "\n"))
        out = mk("parse", p)
        assert out["extra_top"] == "y" and out["phases"][0]["foo"] == "bar"
        assert out["phases"][0]["tasks"][0]["zip"] == 1
        assert mk("check", p)["ok"] is True

        # errors: no leading +++, bad TOML (line number inside the file), missing file
        p = write("noopen.md", 'plan = "x"\n+++\n+++\n')
        out = mk("parse", p, code=2)
        assert out["ok"] is False and out["errors"][0].startswith("line 1:"), out
        mk("check", p, code=2)
        p = write("noclose.md", '+++\nplan = "x"\n')
        assert mk("parse", p, code=2)["errors"][0].startswith("line 3:")
        p = write("badtoml.md", '+++\nplan = "t1"\nstatus = "draft"\ncreated == 2026-10-08\n+++\nbody\n')
        out = mk("parse", p, code=2)
        assert out["ok"] is False and out["errors"][0].startswith("line 4:"), out
        mk("check", os.path.join(tmp, "nope.md"), code=3)

        # CRLF plans parse like their LF twin; set/add-run keep the file's own endings
        p = write("crlf.md", plan_text(phases=phase("P1") + "\n").replace("\n", "\r\n"))
        with open(p, "rb") as f:
            raw = f.read()
        out = mk("parse", p)
        assert out["ok"] and out["sha"] == hashlib.sha256(raw).hexdigest()
        assert mk("check", p)["ok"] is True
        assert mk("set", p, "--status", "approved")["status"] == "approved"
        crlf = open(p, "rb").read()
        assert crlf.replace(b"\r\n", b"\n") == raw.replace(b"\r\n", b"\n").replace(
            b'status = "draft"', b'status = "approved"')
        assert b"\n" not in crlf.replace(b"\r\n", b"")  # every newline still a \r\n
        assert mk("parse", p)["status"] == "approved"
        assert mk("add-run", p, "--run", "r1")["runs"] == ["r1"]
        crlf = open(p, "rb").read()
        assert 'runs = ["r1"]\r' in crlf.decode("utf-8") and b"\n" not in crlf.replace(b"\r\n", b"")

        # the eight P1.2 error rules — each violates exactly one, parse stays ok
        def bad(name, phases, needle):
            p = write(f"{name}.md", plan_text(phases=phases + "\n"))
            assert mk("parse", p)["ok"] is True, name
            out = mk("check", p, code=2)
            assert out["ok"] is False and any(needle in e for e in out["errors"]), (name, out)

        bad("dup", phase("P1") + "\n" + phase("P1"), "duplicate phase id P1")
        bad("dup-task", phase("P1", tasks=task("P1.1") + task("P1.1")), "duplicate task id P1.1")
        bad("unknown-dep", phase("P1", deps='"PX"'), "unknown dep PX")
        bad("cycle", phase("P1", deps='"P2"') + "\n" + phase("P2", wave=2, deps='"P1"'),
            "dep cycle: P1 -> P2 -> P1")
        bad("self-dep", phase("P1", deps='"P1"'), "dep cycle: P1 -> P1")
        bad("later-wave", phase("P1", deps='"P2"') + "\n" + phase("P2", wave=2),
            "dep P2 is in a later wave")
        bad("size", phase("P1", size="huge"), "bad size 'huge'")
        bad("risk", phase("P1", risk="wild"), "bad risk 'wild'")
        bad("review", phase("P1", review="deep"), "bad review 'deep'")
        bad("route-1", phase("P1", route="claude"), "bad route")
        bad("route-4", phase("P1", route="a b c d"), "bad route")
        bad("reviewer", phase("P1", extra='reviewer = 5\n'), "reviewer 5 is not a string")
        bad("overlap", phase("P1", owned='"src/"') + "\n" + phase("P2", owned='"src/x.py"'),
            "P1 and P2 both own")
        bad("sub-share", phase("P1", subagents=True,
                               tasks=task("P1.1", subagent=True, owned='"a.py"')
                               + task("P1.2", subagent=True, owned='"a.py"')),
            "subagent tasks P1.1 and P1.2 share a file")
        bad("sub-no-owned", phase("P1", subagents=True, tasks=task("P1.1", subagent=True)),
            "P1.1: subagent task with no owned")
        bad("lane-waves", phase("P1", lane_group="A") + "\n" + phase("P2", wave=2, lane_group="A"),
            "lane_group A: phases in different waves")

        # reviewer: a string value passes through parse verbatim and checks clean
        p = write("reviewer.md", plan_text(
            phases=phase("P1", extra='reviewer = "opencode opencode-go/kimi-k3"\n') + "\n"))
        out = mk("parse", p)
        assert out["phases"][0]["reviewer"] == "opencode opencode-go/kimi-k3"
        assert mk("check", p)["warnings"] == []

        # warnings: advisories never fail the check
        p = write("warn.md", plan_text(
            plan='plan = "Bad_ID"', phases=phase("P1", deps='"r9:T1"',
                                                 tasks=task("X.9")) + "\n"))
        out = mk("check", p)
        assert out["ok"] is True and out["warnings"], out
        assert any("cross-run" in w and "r9:T1" in w for w in out["warnings"])
        assert any("plan id" in w for w in out["warnings"])
        assert any("<phase id>.<n>" in w for w in out["warnings"])
        p = write("warn-keys.md", plan_text(
            top="", phases='[[phase]]\nid = "P1"\ntitle = "t"\nwave = 1\ndeps = []\nowned = []\n'
                           'route = ""\nsize = "standard"\nrisk = "read_only"\nreview = "light"\n'
                           'qa = []\ncommit = ""\nsubagents = false\n\n'))
        out = mk("check", p)
        assert out["ok"] is True and out["warnings"] == [], out  # no optional keys: still quiet

        # T1 — the header keys that fill a plans column: a missing one warns, never errors.
        # each case is ok: true; on the pre-T1 code the warnings list is empty and these fail.
        base = phase("P1", tasks=task("P1.1", acceptance="a")) + "\n"

        p = write("no-created.md", '+++\nplan = "t1"\nstatus = "draft"\nruns = []\n'
                                   '[gates]\ntest = "true"\n' + base + '+++\n\n# body\nmarkdown stays\n')
        out = mk("check", p)
        assert out["ok"] is True, out
        assert any("created" in w and "plans.created" in w for w in out["warnings"]), out
        assert not any("status" in w for w in out["warnings"]), out  # only the missing one

        p = write("no-status.md", '+++\nplan = "t1"\ncreated = 2026-10-08\nruns = []\n'
                                  '[gates]\ntest = "true"\n' + base + '+++\n\n# body\nmarkdown stays\n')
        out = mk("check", p)
        assert out["ok"] is True, out
        assert any("status" in w and "plans.status" in w for w in out["warnings"]), out
        assert not any("created" in w for w in out["warnings"]), out

        p = write("no-h1.md", '+++\nplan = "t1"\nstatus = "draft"\ncreated = 2026-10-08\nruns = []\n'
                               '[gates]\ntest = "true"\n' + base + '+++\n\n## Goal\nbody with no H1\n')
        out = mk("check", p)
        assert out["ok"] is True, out
        assert any("H1" in w and "plans.title" in w for w in out["warnings"]), out
        assert not any("status" in w or "created" in w for w in out["warnings"]), out

        # a '#' inside a fenced block is not an H1; the fenced code is not a heading
        p = write("fenced-h1.md", '+++\nplan = "t1"\nstatus = "draft"\ncreated = 2026-10-08\nruns = []\n'
                                  '[gates]\ntest = "true"\n' + base + '+++\n\n```\n# not a heading\n```\n')
        assert any("H1" in w for w in mk("check", p)["warnings"])

        # both keys missing: both warnings, still ok: true (warnings never fail the check)
        p = write("neither.md", '+++\nplan = "t1"\nruns = []\n[gates]\ntest = "true"\n' + base
                               + '+++\n\nbody\n')
        out = mk("check", p)
        assert out["ok"] is True, out
        assert any("created" in w for w in out["warnings"]) and any("status" in w for w in out["warnings"])
        assert any("H1" in w for w in out["warnings"]), out

        # the shipped template is the contract: it names every key's home and checks with
        # no new warning (the whole set above is empty for it)
        out = mk("check", template)
        assert out["ok"] and out["warnings"] == [], out
        with open(template, encoding="utf-8") as fh:
            tmpl = fh.read()
        assert tmpl.startswith("+++\n") and "# Lifecycle" in tmpl
        for needle in ("plans.id", "plans.status", "plans.created", "plans.design",
                       "plans.integration", "plans.header_json", "plans.title",
                       "tasks.id", "tasks.title", "tasks.acceptance", "tasks.owned",
                       "tasks.deps", "tasks.parallel", "tasks.route_json", "tasks.type",
                       "tasks.parent", "header_json only"):
            assert needle in tmpl, needle

        # P1.3 set — body byte-identical, only the status line changes
        import shutil as sh
        target = os.path.join(tmp, "set-target.md")
        sh.copy(real_plan, target)
        orig = open(target, "rb").read()
        out = mk("set", target, "--status", "running")
        assert out["ok"] and out["status"] == "running"
        new = open(target, "rb").read()
        ol, nl = orig.decode().split("\n"), new.decode().split("\n")
        assert ol[2] == 'status = "approved"' and nl[2] == 'status = "running"'
        assert "\n".join(ol[3:]) == "\n".join(nl[3:])  # body byte-identical
        assert [l for i, l in enumerate(ol) if i != 2] == [l for i, l in enumerate(nl) if i != 2]
        assert mk("parse", target)["status"] == "running"
        assert os.listdir(tmp).count("set-target.md") == 1  # atomic: no temp files left
        assert not [f for f in os.listdir(tmp) if f.startswith(".tmp-")]
        mk("set", target, "--status", "bogus", code=2)
        assert open(target, "rb").read() == new  # refused write changed nothing
        p = write("set-comment.md", '+++\nplan = "t1"\nstatus = "draft"  # keep me\nruns = []\n+++\nbody\n')
        mk("set", p, "--status", "approved")
        got = open(p).read().split("\n")
        assert got[2] == 'status = "approved"  # keep me', got[2]
        assert got[3] == "runs = []" and got[4] == "+++" and "\n".join(got[5:]) == "body\n"
        p = write("set-missing.md", '+++\nplan = "t1"\nruns = []\n+++\nbody\n')
        before = open(p, "rb").read()
        mk("set", p, "--status", "approved", code=2)
        assert open(p, "rb").read() == before

        # F3.1 — set --status approved runs check first: errors refuse the write
        p = write("approve-bad.md", plan_text(phases=phase("P1", route="opencode") + "\n"))
        assert mk("check", p, code=2)["errors"], "route 'opencode' (no model) fails check"
        before = open(p, "rb").read()
        out = mk("set", p, "--status", "approved", code=2)
        assert out["ok"] is False and any("bad route" in e for e in out["errors"]), out
        assert open(p, "rb").read() == before  # refused approval: file byte-identical
        assert mk("set", p, "--status", "running")["status"] == "running"  # no check, unchanged
        assert mk("parse", p)["status"] == "running"
        # warnings alone never block an approval
        p = write("approve-warn.md", plan_text(
            plan='plan = "Bad_ID"', phases=phase("P1", deps='"r9:T1"') + "\n"))
        out = mk("check", p)
        assert out["ok"] is True and out["warnings"], out
        assert mk("set", p, "--status", "approved")["status"] == "approved"
        assert mk("parse", p)["status"] == "approved"

        # write_atomic keeps the file's own mode; the temp + os.replace lands in a writable dir
        p = write("mode.md", plan_text())
        os.chmod(p, 0o444)
        mk("set", p, "--status", "approved")
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o444, oct(os.stat(p).st_mode)
        assert mk("parse", p)["status"] == "approved"
        os.chmod(p, 0o644)  # cleanup deletes it either way

        # P1.3 add-run — appends, idempotent, comments and body untouched
        target = os.path.join(tmp, "run-target.md")
        sh.copy(real_plan, target)
        orig = open(target, "rb").read()
        out = mk("add-run", target, "--run", "mk-1")
        assert out["ok"] and out["added"] is True and out["runs"] == ["mk-1"]
        new = open(target, "rb").read()
        ol, nl = orig.decode().split("\n"), new.decode().split("\n")
        assert nl[4] == 'runs = ["mk-1"]' and ol[4] == "runs = []"
        assert [l for i, l in enumerate(ol) if i != 4] == [l for i, l in enumerate(nl) if i != 4]
        assert mk("parse", target)["runs"] == ["mk-1"]
        out = mk("add-run", target, "--run", "r2")
        assert out["runs"] == ["mk-1", "r2"] and out["added"] is True
        twice = open(target, "rb").read()
        out = mk("add-run", target, "--run", "r2")  # idempotent: no rewrite at all
        assert out["added"] is False and out["runs"] == ["mk-1", "r2"]
        assert open(target, "rb").read() == twice
        mk("add-run", target, "--run", "bad id", code=2)
        assert open(target, "rb").read() == twice
        p = write("run-comment.md", '+++\nplan = "t1"\nstatus = "draft"\nruns = []  # ids\n+++\nbody\n')
        mk("add-run", p, "--run", "r1")
        assert open(p).read().split("\n")[3] == 'runs = ["r1"]  # ids'
        p = write("run-multi.md", '+++\nplan = "t1"\nstatus = "draft"\nruns = [\n  "r1",\n]  # end\n+++\nbody\n')
        mk("add-run", p, "--run", "r2")
        got = open(p).read().split("\n")
        assert got[3] == 'runs = ["r1", "r2"]  # end', got[3]
        assert got[4] == "+++" and "\n".join(got[5:]) == "body\n"  # span collapsed, body intact
        assert mk("parse", p)["runs"] == ["r1", "r2"]

        # D4 — nested subtasks parse; a plan with none is unchanged
        p = write("nested.md", plan_text(phases=phase(
            "P1", tasks=task("P1.1", extra=(
                '  deps = "P1"\n  parallel = true\n  route = "opencode model high"\n'
                '  [[phase.task.subtask]]\n  id = "P1.1.1"\n  title = "sub"\n'
                '  acceptance = "done"\n  deps = ["P1.1"]\n  parallel = false\n'
                '  route = { agent = "opencode", model = "m", effort = "low" }\n  owned = ["a.py"]\n'
            ))) + "\n"))
        out = mk("parse", p)
        task0 = out["phases"][0]["tasks"][0]
        sub = task0["subtasks"][0]
        assert sub["id"] == "P1.1.1" and sub["deps"] == ["P1.1"] and sub["parallel"] is False
        assert sub["route"] == {"agent": "opencode", "model": "m", "effort": "low"}
        assert task0["deps"] == ["P1"] and task0["parallel"] is True
        assert mk("check", p)["ok"] is True

        # no store: approve validates and marks approved, and creates no mk.db
        p = write("nostore.md", plan_text(phases=phase("P1", tasks=task("P1.1")) + "\n"))
        empty_data = os.path.join(tmp, "empty-data")
        empty_home = os.path.join(tmp, "empty-home")
        out = mk("approve", p, "--data", empty_data, "--home", empty_home)
        assert out["ok"] and out["store"] is False and out["status"] == "approved"
        assert not os.path.exists(os.path.join(empty_data, "mk.db"))
        assert not os.path.exists(os.path.join(empty_home, "mk.db"))
        assert mk("parse", p)["status"] == "approved"
        assert mk("approve", p, "--data", empty_data, "--home", empty_home).get("noop") is True
        # a failing check refuses the write and still creates no store
        badp = write("nostore-bad.md", plan_text(phases=phase("P1", route="opencode") + "\n"))
        before = open(badp, "rb").read()
        refused = mk("approve", badp, "--data", empty_data, "--home", empty_home, code=2)
        assert any("bad route" in e for e in refused["errors"])
        assert open(badp, "rb").read() == before
        assert not os.path.exists(os.path.join(empty_data, "mk.db"))

        # with a store: import, archive, export; a failed approve leaves file and data
        data = os.path.join(tmp, "store")
        home = os.path.join(data, ".mk-home")
        os.makedirs(data)
        os.makedirs(home)
        db = _mkdb()
        db.open_project(home, data_dir=data).close()
        good = write("sample.md", plan_text(
            plan='plan = "sample"',
            phases=phase("P1", tasks=task("P1.1", extra=(
                '  [[phase.task.subtask]]\n  id = "P1.1.1"\n  title = "leaf"\n'
                '  acceptance = "leaf done"\n  deps = "P1.1"\n  parallel = true\n  route = ""\n  owned = []\n'
            ))) + "\n"))
        out = mk("approve", good, "--data", data, "--home", home, "--run", "r1")
        assert out["ok"] and out["store"] is True and out["version"] == 1, out
        # C13: approve copies, the original file stays (with the approved status line)
        assert os.path.isfile(good), "approve must leave the original in place"
        assert b'status = "approved"' in open(good, "rb").read()
        assert "run" not in out and out["tasks_run"] == "r1", out
        arch = out["archived"]
        assert os.path.isfile(arch)
        archived_bytes = open(arch, "rb").read()
        exp = mk("export", os.path.join(tmp, "sample.md"), "--data", data, "--home", home)
        assert exp["ok"] and open(exp["path"], "rb").read() == archived_bytes
        # header bytes of the round trip equal the archived header
        def header_bytes(blob):
            text = blob.decode("utf-8")
            lines = text.split("\n")
            close = next(i for i in range(1, len(lines)) if lines[i].strip() == "+++")
            return "\n".join(lines[:close + 1]).encode()
        assert header_bytes(open(exp["path"], "rb").read()) == header_bytes(archived_bytes)
        con = db.open_project(home, data_dir=data)
        rows = {r["id"]: (r["type"], r["parent"], r["deps"]) for r in con.execute(
            "SELECT id, type, parent, deps FROM tasks WHERE run='r1'")}
        con.close()
        assert rows["P1"] == ("phase", None, "")
        assert rows["P1.1"] == ("task", "P1", "")
        assert rows["P1.1.1"] == ("subtask", "P1.1", "P1.1")
        # same bytes again: no-op, archive kept
        shutil.copy(arch, good)
        out2 = mk("approve", good, "--data", data, "--home", home, "--run", "r1")
        assert out2.get("noop") is True and out2["version"] == 1, out2
        assert os.path.isfile(arch)
        # a changed body is a new version; the old archive file stays
        changed = open(good).read().replace("# body", "# body changed")
        open(good, "w").write(changed)
        out3 = mk("approve", good, "--data", data, "--home", home, "--run", "r1")
        assert out3["version"] == 2 and os.path.isfile(arch) and os.path.isfile(out3["archived"])
        assert out3["archived"] != arch and os.path.isfile(good)  # still a copy, not a move

        # a named cycle leaves the file and the store unchanged
        cyc = write("cyc.md", plan_text(plan='plan = "cyc"', phases=phase(
            "P1", tasks=task("P1.1", extra=(
                '  deps = "P1.1.1"\n  [[phase.task.subtask]]\n  id = "P1.1.1"\n  title = "s"\n'
                '  acceptance = "a"\n  deps = "P1.1"\n  parallel = false\n  route = ""\n  owned = []\n'
            ))) + "\n"))
        blob = open(cyc, "rb").read()
        con = db.open_project(home, data_dir=data)
        n_before = con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
        con.close()
        bad = mk("approve", cyc, "--data", data, "--home", home, "--run", "r1", code=2)
        # check and approve share the graph, so the refusal is check's "dep cycle:" (ids named)
        assert any("cycle:" in e and "P1.1" in e and "P1.1.1" in e for e in bad["errors"]), bad
        assert open(cyc, "rb").read() == blob
        con = db.open_project(home, data_dir=data)
        assert con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == n_before
        assert con.execute("SELECT id FROM plans WHERE id='cyc'").fetchone() is None
        con.close()

        # <run>:<id> is validated: present passes, missing is named and changes nothing
        con = db.open_project(home, data_dir=data)
        db.add_task(con, "other", "T9", "task", title="ext")
        con.close()
        xref = write("xref.md", plan_text(plan='plan = "xref"', phases=phase("P1", deps='"other:T9"') + "\n"))
        assert mk("approve", xref, "--data", data, "--home", home, "--run", "rx")["ok"]
        missing = write("xref-bad.md", plan_text(
            plan='plan = "xrefbad"', phases=phase("P1", deps='"other:NOPE"') + "\n"))
        blob = open(missing, "rb").read()
        bad = mk("approve", missing, "--data", data, "--home", home, "--run", "ry", code=2)
        assert any("missing dep" in e and "other:NOPE" in e for e in bad["errors"]), bad
        assert open(missing, "rb").read() == blob

        # R3 — a forward dep (phase A on a later phase C, a task on a later sibling) approves
        fwd = write("fwd.md", plan_text(plan='plan = "fwd"', phases=(
            phase("PA", wave=1, deps='"PC"', tasks=task("PA.1", extra='  deps = "PA.2"\n')
                  + task("PA.2"))
            + "\n" + phase("PC", wave=1) + "\n")))
        out = mk("approve", fwd, "--data", data, "--home", home, "--run", "rf")
        assert out["ok"] and out["version"] == 1, out
        con = db.open_project(home, data_dir=data)
        deps = {r["id"]: r["deps"] for r in con.execute("SELECT id, deps FROM tasks WHERE run='rf'")}
        con.close()
        assert deps["PA"] == "PC" and deps["PA.1"] == "PA.2" and deps["PA.2"] == "", deps

        # a cycle across two later items is refused and names both ids; file and store stay
        cyc2 = write("cyc2.md", plan_text(plan='plan = "cyc2"', phases=(
            phase("PA", wave=1, deps='"PC"') + "\n" + phase("PC", wave=1, deps='"PA"') + "\n")))
        blob = open(cyc2, "rb").read()
        con = db.open_project(home, data_dir=data)
        n_before = con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
        con.close()
        bad = mk("approve", cyc2, "--data", data, "--home", home, "--run", "rc", code=2)
        assert any("cycle:" in e and "PA" in e and "PC" in e for e in bad["errors"]), bad
        assert open(cyc2, "rb").read() == blob
        con = db.open_project(home, data_dir=data)
        assert con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == n_before
        assert con.execute("SELECT id FROM plans WHERE id='cyc2'").fetchone() is None
        con.close()

        # a copy of the real mk-store plan approves; every phase and task lands with its deps
        real = "/Users/rohits/Development/matrixkoder/.matrixkoder/plans/mk-store.md"
        copy = os.path.join(tmp, "mk-store-copy.md")
        shutil.copy(real, copy)
        parsed = mk("parse", copy)
        want = {}
        for ph in parsed["phases"]:
            want[ph["id"]] = ",".join(str(d) for d in ph["deps"])
            for task_row in ph["tasks"]:
                want[task_row["id"]] = ",".join(str(d) for d in task_row["deps"])
        out = mk("approve", copy, "--data", data, "--home", home, "--run", "mkstore")
        assert out["ok"] and os.path.isfile(copy), out  # copied, not moved
        assert os.path.isfile(real)  # the real plan was never touched
        con = db.open_project(home, data_dir=data)
        got = {r["id"]: r["deps"] for r in con.execute(
            "SELECT id, deps FROM tasks WHERE run='mkstore'")}
        con.close()
        assert set(got) == set(want), (sorted(set(want) - set(got)), sorted(set(got) - set(want)))
        assert got == want

        # CRLF export is byte-equal to the archived file
        crlf = write("crlf-exp.md", plan_text(plan='plan = "crlfexp"', phases=phase("P1") + "\n").replace("\n", "\r\n"))
        original = open(crlf, "rb").read()
        out = mk("approve", crlf, "--data", data, "--home", home, "--run", "rcrlf")
        assert out["ok"], out
        archived = open(out["archived"], "rb").read()
        dest = os.path.join(tmp, "crlfexp.md")
        exp = mk("export", dest, "--data", data, "--home", home)
        assert exp["ok"] and open(dest, "rb").read() == archived
        assert b"\r\n" in archived and archived != original  # status line changed, endings did not
        assert archived.replace(b"\r\n", b"") .count(b"\n") == 0

        # a task or subtask with no id is a check error
        noid = write("noid.md", plan_text(phases=phase("P1", tasks=(
            '  [[phase.task]]\n  title = "missing"\n  acceptance = "a"\n'
            '  [[phase.task.subtask]]\n  title = "also missing"\n  acceptance = "b"\n')) + "\n"))
        bad = mk("check", noid, code=2)
        assert any("task has no id" in e for e in bad["errors"]), bad
        assert any("subtask has no id" in e for e in bad["errors"]), bad

        # FB — a task-level cycle and a subtask self-dep are check errors, so set cannot approve them
        tcyc = write("task-cycle.md", plan_text(phases=phase("P1", tasks=(
            task("P1.1", extra='  deps = "P1.2"\n') + task("P1.2", extra='  deps = "P1.1"\n'))) + "\n"))
        bad = mk("check", tcyc, code=2)
        assert any(e.startswith("dep cycle:") and "P1.1" in e and "P1.2" in e for e in bad["errors"]), bad
        before = open(tcyc, "rb").read()
        refused = mk("set", tcyc, "--status", "approved", code=2)
        assert any("dep cycle:" in e for e in refused["errors"])
        assert open(tcyc, "rb").read() == before
        refused = mk("approve", tcyc, "--data", empty_data, "--home", empty_home, code=2)
        assert any("dep cycle:" in e for e in refused["errors"])
        assert open(tcyc, "rb").read() == before
        selfdep = write("self-sub.md", plan_text(phases=phase("P1", tasks=task("P1.1", extra=(
            '  [[phase.task.subtask]]\n  id = "P1.1.1"\n  title = "s"\n  acceptance = "a"\n'
            '  deps = "P1.1.1"\n  parallel = false\n  route = ""\n  owned = []\n'))) + "\n"))
        bad = mk("check", selfdep, code=2)
        assert any("dep cycle:" in e and "P1.1.1" in e for e in bad["errors"]), bad
        before = open(selfdep, "rb").read()
        mk("set", selfdep, "--status", "approved", code=2)
        assert open(selfdep, "rb").read() == before

        # FB — set --status approved names an unresolved cross-run dep instead of staying silent
        xrefw = write("xref-warn.md", plan_text(phases=phase("P1", deps='"other-run:T1"') + "\n"))
        warned = mk("set", xrefw, "--status", "approved")
        assert warned["status"] == "approved", warned
        assert any("cross-run" in w and "other-run:T1" in w for w in warned.get("warnings") or []), warned

        # FB — a UTF-8 BOM survives check -> approve -> export byte for byte (status flip aside)
        bom_text = plan_text(plan='plan = "bomplan"', phases=phase("P1", tasks=task("P1.1")) + "\n").replace("\n", "\r\n")
        bom = write("bom.md", bom_text)
        raw_bom = b"\xef\xbb\xbf" + open(bom, "rb").read()
        open(bom, "wb").write(raw_bom)
        assert mk("check", bom)["ok"] is True
        out = mk("approve", bom, "--data", data, "--home", home, "--run", "rbom")
        assert out["ok"] and os.path.isfile(bom), out  # the BOM file stays too
        archived_bom = open(out["archived"], "rb").read()
        assert open(bom, "rb").read() == archived_bom  # original synced to the approved bytes
        assert archived_bom.startswith(b"\xef\xbb\xbf") and b"\r\n" in archived_bom
        assert b'status = "approved"' in archived_bom
        dest = os.path.join(tmp, "bomplan.md")
        exp = mk("export", dest, "--data", data, "--home", home)
        assert open(dest, "rb").read() == archived_bom

        # FB — kill seam: 20 trials after the commit and 20 after the archive; the next approve converges
        for step in ("commit", "archive"):
            for n in range(20):
                sdata = os.path.join(tmp, f"seam-{step}-{n}")
                shome = os.path.join(sdata, ".mk-home")
                os.makedirs(shome)
                db.open_project(shome, data_dir=sdata).close()
                src = write(f"seam-{step}-{n}.md", plan_text(
                    plan=f'plan = "seam{step}{n}"', phases=phase("P1", tasks=task("P1.1")) + "\n"))
                env = dict(os.environ, MATRIXKODER_DATA=sdata, MK_PLAN_KILL_AFTER=step)
                r = subprocess.run([sys.executable, os.path.abspath(__file__), "approve", src,
                                    "--data", sdata, "--home", shome, "--run", "s1"],
                                   capture_output=True, text=True, env=env)
                assert r.returncode != 0, (step, n, r.returncode, r.stdout, r.stderr)
                env.pop("MK_PLAN_KILL_AFTER", None)
                r2 = subprocess.run([sys.executable, os.path.abspath(__file__), "approve", src,
                                     "--data", sdata, "--home", shome, "--run", "s1"],
                                    capture_output=True, text=True, env=env)
                assert r2.returncode == 0, (step, n, r2.returncode, r2.stdout, r2.stderr)
                got = json.loads(r2.stdout.strip().splitlines()[-1])
                # commit-kill: the next approve finishes the missing archive (same version).
                # archive-kill: the archive already matches, so the next approve is a no-op
                # and the original stays, now on the approved bytes.
                assert got["ok"] and got["version"] == 1, got
                if step == "archive":
                    assert got.get("noop") is True, got
                # C13: a kill leaves the original in place, and the converging approve brings it
                # back to the approved bytes the archive holds
                assert os.path.isfile(src), step
                assert open(src, "rb").read() == open(got["archived"], "rb").read()
                assert os.path.isfile(got["archived"])
                assert b'status = "approved"' in open(got["archived"], "rb").read()


        # the run's plan path still resolves through mk-state after the file is archived
        state = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mk-state.py")
        link = os.path.join(tmp, "link.md")
        open(link, "w").write(plan_text(plan='plan = "link"', phases=phase("P1") + "\n"))
        r = subprocess.run([sys.executable, state, "--data", data, "run", "init", "--force",
                            "--id", "lk", "--feature", "L", "--branch", "bl", "--plan", link],
                           capture_output=True, text=True)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        ap = mk("approve", link, "--data", data, "--home", home, "--run", "lk")
        # the original path still resolves after the approve (the hermes-switchui issue): C13
        # approve copies, so the file mk-state was pointed at is still there
        assert ap["ok"] and os.path.isfile(link), ap
        r = subprocess.run([sys.executable, state, "--data", data, "plan", "sync", "--run", "lk"],
                           capture_output=True, text=True)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        assert got["ok"] and got.get("plan") == "link", got

        # D1.2 — approve copies (C13), data-dir fallback, tasks_run, import, delete, pause
        def plans_rows(pid, datadir=data, homedir=home):
            con = db.open_project(homedir, data_dir=datadir)
            try:
                n = con.execute("SELECT COUNT(*) c FROM plans WHERE id=?", (pid,)).fetchone()["c"]
                row = con.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
            finally:
                con.close()
            return n, (dict(row) if row is not None else None)

        # 1. a good file imports into exactly one plans row and touches no file byte.
        # the stored version is 0 for a fresh id and the reply reports that same number.
        imp1 = write("imp1.md", plan_text(plan='plan = "imp1"', status='status = "draft"',
                                          phases=phase("P1", tasks=task("P1.1")) + "\n").replace(
            "# body", "# imp1 — Sample Title"))
        blob1 = open(imp1, "rb").read()
        out = mk("import", imp1, "--data", data, "--home", home)
        assert out["ok"] is True and out["store"] is True and out["plan"] == "imp1", out
        assert out["title"] == "Sample Title", out  # the H1 text after the em dash
        n, row1 = plans_rows("imp1")
        assert n == 1, n
        assert row1["version"] == 0 and out["version"] == row1["version"], (row1, out)
        assert row1["title"] == "Sample Title"
        con = db.open_project(home, data_dir=data)
        n_tasks = con.execute(
            "SELECT COUNT(*) c FROM tasks WHERE id IN ('P1','P1.1') AND run='imp1'").fetchone()["c"]
        con.close()
        assert n_tasks == 0, n_tasks  # import loads the plan, it does not approve the work
        assert open(imp1, "rb").read() == blob1

        # 2. the same bytes again: the upsert lands on the same row, byte for byte
        out2 = mk("import", imp1, "--data", data, "--home", home)
        assert out2["ok"] is True and out2["plan"] == "imp1", out2
        n, row2 = plans_rows("imp1")
        assert n == 1 and row2 == row1, (row1, row2)
        assert out2["version"] == row1["version"]

        # 3. no H1 in the body: the plan id is the title
        imp3 = write("imp3.md", plan_text(plan='plan = "imp3"',
                                          phases=phase("P1", tasks=task("P1.1")) + "\n").replace(
            "# body", "## only a subheading\nbody text\n"))
        out = mk("import", imp3, "--data", data, "--home", home)
        assert out["ok"] is True and out["plan"] == "imp3" and out["title"] == "imp3", out
        n, row3 = plans_rows("imp3")
        assert n == 1 and row3["title"] == "imp3", (n, row3)

        # 4. a file that is not a plan, and one whose header does not parse: both refused
        # with ok:false and a reason, exit 0, and neither leaves a plans row behind
        noopen = write("imp4a.md", 'plan = "imp4a"\n+++\n+++\nbody\n')
        out = mk("import", noopen, "--data", data, "--home", home)
        assert out["ok"] is False and isinstance(out.get("reason"), str) and out["reason"], out
        badtoml = write("imp4b.md", '+++\nplan = "imp4b"\nstatus = "draft"\ncreated == 2026-10-08\n'
                                    '+++\n\n# body\n')
        out = mk("import", badtoml, "--data", data, "--home", home)
        assert out["ok"] is False and isinstance(out.get("reason"), str) and out["reason"], out
        assert plans_rows("imp4a")[0] == 0 and plans_rows("imp4b")[0] == 0

        # 5. --dry-run reports exactly what a real import would, and writes nothing
        imp5 = write("imp5.md", plan_text(plan='plan = "imp5"', status='status = "draft"',
                                          phases=phase("P1", tasks=task("P1.1")) + "\n").replace(
            "# body", "# imp5 — Dry Title"))
        blob5 = open(imp5, "rb").read()
        out = mk("import", imp5, "--dry-run", "--data", data, "--home", home)
        assert out["ok"] is True and out["dry_run"] is True, out
        assert out["plan"] == "imp5" and out["title"] == "Dry Title", out
        assert plans_rows("imp5")[0] == 0
        assert open(imp5, "rb").read() == blob5

        # 6. delete moves the plan file into plans/archive and never unlinks it; a clash with an
        # existing archive name gets a timestamp suffix
        plans_dir = os.path.join(data, "plans")
        arch_dir = os.path.join(plans_dir, "archive")
        os.makedirs(arch_dir, exist_ok=True)
        live = os.path.join(plans_dir, "imp6.md")
        open(live, "w").write(plan_text(plan='plan = "imp6"', phases=phase("P1") + "\n"))
        live_bytes = open(live, "rb").read()
        clash = os.path.join(arch_dir, "imp6.md")
        open(clash, "w").write("older archived copy\n")
        clash_bytes = open(clash, "rb").read()
        assert mk("import", live, "--data", data, "--home", home)["ok"]
        out = mk("delete", "imp6", "--data", data, "--home", home)
        assert out["ok"] is True and out["plan"] == "imp6", out
        moved = out["moved"]
        assert moved and os.path.dirname(os.path.abspath(moved)) == os.path.abspath(arch_dir), moved
        assert os.path.abspath(moved) != os.path.abspath(clash)
        assert os.path.isfile(moved) and open(moved, "rb").read() == live_bytes
        assert not os.path.exists(live)
        assert open(clash, "rb").read() == clash_bytes
        assert plans_rows("imp6")[0] == 0

        # 7. a plan an active run still points at is refused: the row and the file both stay
        live7 = os.path.join(plans_dir, "imp7.md")
        open(live7, "w").write(plan_text(plan='plan = "imp7"', phases=phase("P1") + "\n"))
        assert mk("import", live7, "--data", data, "--home", home)["ok"]
        con = db.open_project(home, data_dir=data)
        con.execute("INSERT OR REPLACE INTO runs(id, plan, plan_version, branch, status, stage)"
                    " VALUES ('imp7run','imp7',0,'br','active','build')")
        con.commit()
        con.close()
        out = mk("delete", "imp7", "--data", data, "--home", home, code=2)
        assert out["ok"] is False, out
        said = " ".join(out.get("errors") or [])
        assert "imp7run" in said and "live run" in said, out
        assert plans_rows("imp7")[0] == 1
        assert os.path.isfile(live7)

        # a finished run does not block delete, and its tasks stay
        con = db.open_project(home, data_dir=data)
        con.execute("UPDATE runs SET status='done' WHERE id='imp7run'")
        con.commit()
        db.add_task(con, "imp7run", "P1", "phase", title="kept")
        con.close()
        out = mk("delete", "imp7", "--data", data, "--home", home)
        assert out["ok"] is True and plans_rows("imp7")[0] == 0, out
        con = db.open_project(home, data_dir=data)
        kept = con.execute("SELECT id FROM tasks WHERE run='imp7run'").fetchall()
        con.close()
        assert [r["id"] for r in kept] == ["P1"]

        # D1.1 columns: filled when present, harmless when absent
        cols_data = os.path.join(tmp, "store-cols")
        cols_home = os.path.join(cols_data, ".mk-home")
        os.makedirs(cols_home)
        db.open_project(cols_home, data_dir=cols_data).close()
        con = db.open_project(cols_home, data_dir=cols_data)
        have = {r[1] for r in con.execute("PRAGMA table_info(plans)")}
        for col, decl in (("status", "TEXT"), ("created", "TEXT"),
                          ("design", "TEXT"), ("integration", "TEXT")):
            if col not in have:  # a v4 store (D1.1) already has them
                con.execute(f"ALTER TABLE plans ADD COLUMN {col} {decl}")
        con.commit()
        con.close()
        colp = write("cols.md", plan_text(
            plan='plan = "cols"', top='design = ["a.py"]\nintegration = ["b.py"]',
            phases=phase("P1", tasks=task("P1.1")) + "\n").replace("# body", "# cols — Col Title"))
        out = mk("import", colp, "--data", cols_data, "--home", cols_home)
        assert out["ok"] and out["title"] == "Col Title", out
        n, crow = plans_rows("cols", cols_data, cols_home)
        assert n == 1 and crow["status"] == "draft" and crow["created"] == "2026-10-08", crow
        assert json.loads(crow["design"]) == ["a.py"] and json.loads(crow["integration"]) == ["b.py"]
        out = mk("approve", colp, "--data", cols_data, "--home", cols_home, "--run", "colsrun")
        assert out["ok"] and os.path.isfile(colp), out
        n, crow = plans_rows("cols", cols_data, cols_home)
        assert crow["status"] == "approved" and crow["version"] == 1, crow
        # the shared store has none of the four columns: approve and import still succeed
        plain = write("plain.md", plan_text(plan='plan = "plain"', phases=phase("P1") + "\n"))
        assert mk("import", plain, "--data", data, "--home", home)["ok"]
        assert mk("approve", plain, "--data", data, "--home", home, "--run", "plainrun")["ok"]

        # pause and back: the file and, with the column, plans.status
        parked = mk("set", colp, "--status", "parked", "--data", cols_data, "--home", cols_home)
        assert parked["status"] == "parked" and parked.get("store") is True, parked
        assert mk("parse", colp)["status"] == "parked"
        n, crow = plans_rows("cols", cols_data, cols_home)
        assert crow["status"] == "parked", crow
        back = mk("set", colp, "--status", "approved", "--data", cols_data, "--home", cols_home)
        assert back["status"] == "approved", back
        n, crow = plans_rows("cols", cols_data, cols_home)
        assert crow["status"] == "approved", crow
        # a store without the column: set still rewrites the file and does not error
        assert mk("set", plain, "--status", "parked", "--data", data, "--home", home)["status"] == "parked"
        assert mk("parse", plain)["status"] == "parked"
        assert mk("set", plain, "--status", "approved", "--data", data, "--home", home)["status"] == "approved"

        # no --data, cwd = a temp repo whose .matrixkoder/mk.db exists -> store: true
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".matrixkoder"))
        subprocess.run(["git", "init", "-q", repo], check=True)
        db.open_project(os.path.join(repo, ".mk-home"),
                        data_dir=os.path.join(repo, ".matrixkoder")).close()
        rp = os.path.join(repo, "repoplan.md")
        open(rp, "w").write(plan_text(plan='plan = "repoplan"', phases=phase("P1") + "\n"))
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "approve", rp,
                            "--home", os.path.join(repo, ".mk-home")],
                           capture_output=True, text=True, cwd=repo,
                           env={k: v for k, v in os.environ.items() if k != "MATRIXKODER_DATA"})
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        assert got["ok"] and got["store"] is True, got
        assert os.path.isfile(rp) and os.path.isfile(got["archived"])
        assert "tasks_run" in got and "run" not in got, got

        # file-only next to an existing mk.db names that store in a warning
        side = os.path.join(tmp, "side")
        os.makedirs(side)
        open(os.path.join(side, "mk.db"), "wb").close()
        sp = os.path.join(side, "side.md")
        open(sp, "w").write(plan_text(plan='plan = "sideplan"', phases=phase("P1") + "\n"))
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "approve", sp],
                           capture_output=True, text=True, cwd=tmp,
                           env={k: v for k, v in os.environ.items() if k != "MATRIXKODER_DATA"})
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        assert got["store"] is False and any("mk.db" in w for w in got.get("warnings") or []), got

        print("mk-plan self-test ok")
    finally:
        restore_data()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        sys.exit(main(sys.argv[1:]))
