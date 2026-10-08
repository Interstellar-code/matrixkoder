#!/usr/bin/env python3
"""MatrixKoder plan files — parse, check and edit TOML plans (C1/C2 in the plan body).
Python 3 stdlib only.

  parse <path>
  check <path>                     parse + the P1.2 rules, adds "warnings"
  set <path> --status draft|approved|running|done|parked
  add-run <path> --run ID          appends to runs; idempotent

A plan file: first line +++, TOML until the next +++ line, the rest is free
markdown, never parsed. Every command prints ONE JSON line.
Errors: {"ok": false, "errors": ["line N: ..."]} exit 2; missing file exit 3.
set/add-run rewrite only the target header key's line(s) — the markdown body and
every other header line (comments included) stay byte-identical — via a temp
file + os.replace, and the rewritten text is re-parsed before anything is written.
"""
import argparse, contextlib, fnmatch, hashlib, json, os, re, stat, sys, tempfile, tomllib

STATUSES = ("draft", "approved", "running", "done", "parked")
SIZES = ("trivial", "standard", "complex")
RISKS = ("read_only", "reversible", "destructive")
REVIEWS = ("light", "standard", "data-loss")
PLAN_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
PHASE_ID_RE = re.compile(r"[A-Z][A-Z0-9]{0,7}")
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ROUTE_RE = re.compile(r"\S+( \S+){1,2}")  # "<agent> <model> [<effort>]"; "" means Jev
TOP_KEYS = ("plan", "status", "runs", "gates", "phase")  # header keys consumed by C2
PHASE_KEYS = ("id", "title", "wave", "deps", "owned", "forbidden", "route", "size",
              "risk", "review", "qa", "commit", "subagents", "lane_group", "task")
TASK_KEYS = ("id", "title", "acceptance", "refs", "subagent", "owned")
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
        return raw.decode("utf-8").lstrip("\ufeff"), hashlib.sha256(raw).hexdigest()
    except UnicodeDecodeError:
        raise Fail([f"line 1: {path} is not valid UTF-8"])


def split_plan(text):
    """(lines, close): lines[0] is the opening +++, lines[close] the closing one."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "+++":
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


def task_json(t):
    out = {"id": str(t.get("id", "")), "title": t.get("title", ""),
           "acceptance": t.get("acceptance", ""), "refs": as_list(t.get("refs")),
           "subagent": bool(t.get("subagent", False)), "owned": as_list(t.get("owned"))}
    for k, v in t.items():  # unknown keys pass through
        if k not in TASK_KEYS:
            out[k] = jsonable(v)
    return out


def phase_json(ph):
    out = {"id": str(ph.get("id", "")), "title": ph.get("title", ""),
           "wave": ph.get("wave", 1), "deps": as_list(ph.get("deps")),
           "owned": as_list(ph.get("owned")), "forbidden": as_list(ph.get("forbidden")),
           "route": ph.get("route", ""), "size": ph.get("size", "standard"),
           "risk": ph.get("risk", "reversible"), "review": ph.get("review", "standard"),
           "qa": as_list(ph.get("qa")), "commit": ph.get("commit", ""),
           "subagents": bool(ph.get("subagents", False)), "lane_group": ph.get("lane_group", ""),
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


def check_plan(header, out):
    """→ (errors, warnings). Errors are exactly the P1.2 contract rules; anything
    advisory (cross-run deps, missing optional keys, malformed id shapes) is a warning."""
    errors, warnings = [], []
    phases = out["phases"]
    raw = as_list(header.get("phase"))

    if str(out["plan"]) and not PLAN_ID_RE.fullmatch(str(out["plan"])):
        warnings.append(f"plan id {out['plan']!r} does not match [a-z0-9][a-z0-9-]{{0,40}}")
    for k in ("design", "integration"):
        if k not in header:
            warnings.append(f"no {k} key in the header (optional)")

    ids = [p["id"] for p in phases if p["id"]]
    for pid in sorted({i for i in ids if ids.count(i) > 1}):
        errors.append(f"duplicate phase id {pid}")
    tids = [t["id"] for p in phases for t in p["tasks"] if t["id"]]
    for tid in sorted({i for i in tids if tids.count(i) > 1}):
        errors.append(f"duplicate task id {tid}")

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
        for k in ("forbidden", "lane_group"):
            if n < len(raw) and k not in raw[n]:
                warnings.append(f"{pid}: no {k} key (default {p[k]!r})")
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
        raw_tasks = as_list(raw[n].get("task")) if n < len(raw) else []
        for t, rt in zip(p["tasks"], raw_tasks):
            tid = t["id"] or f"{pid} task"
            if t["id"] and pid in by_id and not re.fullmatch(re.escape(pid) + r"\.\d+", t["id"]):
                warnings.append(f"task id {t['id']!r} is not <phase id>.<n> ({pid}.<n>)")
            if "refs" not in rt:
                warnings.append(f"{tid}: no refs key (optional)")
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

    deps = {p["id"]: [str(d) for d in p["deps"] if ":" not in str(d)]
            for p in phases if p["id"]}
    cycle = find_cycle(deps)
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
    d = os.path.dirname(os.path.abspath(path))
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)  # a read-only plan stays read-only
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)  # mkstemp makes 0600; keep the file's own mode
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def set_status(path, value):
    if value not in STATUSES:
        raise Fail([f"bad status {value!r} ({'|'.join(STATUSES)})"])
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
    return {"ok": True, "status": value}


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
    errors, warnings = check_plan(header, out)
    if errors:
        raise Fail(errors, warnings=warnings)
    out["warnings"] = warnings
    return out


def cmd_set(a):
    return set_status(a.path, a.status)


def cmd_add_run(a):
    return add_run(a.path, a.run)


def build_parser():
    p = Parser(prog="mk-plan.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, func in (("parse", cmd_parse), ("check", cmd_check)):
        c = sub.add_parser(name)
        c.add_argument("path")
        c.set_defaults(func=func)
    s = sub.add_parser("set")
    s.add_argument("path"), s.add_argument("--status", required=True)
    s.set_defaults(func=cmd_set)
    r = sub.add_parser("add-run")
    r.add_argument("path"), r.add_argument("--run", required=True)
    r.set_defaults(func=cmd_add_run)
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
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    real_plan = os.path.join(root, ".matrixkoder-plan.md")
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
        assert p0["id"] == "P1" and p0["wave"] == 1 and p0["lane_group"] == "" and p0["subagents"] is False
        assert p0["tasks"][0]["id"] == "P1.1" and p0["tasks"][0]["subagent"] is False
        assert out["created"] == "2026-10-08" and out["design"] == []  # unknown/extra keys pass through

        out = mk("parse", real_plan)
        assert out["ok"] and out["plan"] == "plan-link" and out["status"] == "approved"
        assert [p["id"] for p in out["phases"]] == ["P1", "P2", "P3"]
        assert [t["id"] for t in out["phases"][0]["tasks"]] == ["P1.1", "P1.2", "P1.3"]
        assert all(p["wave"] == 1 and p["lane_group"] == "A" and p["deps"] == [] for p in out["phases"])
        assert out["phases"][0]["review"] == "data-loss" and out["phases"][0]["size"] == "standard"

        # P1.2 check — both real files are clean (ok:true, exit 0); the template
        # carries every key, so it must also be warning-free
        out = mk("check", template)
        assert out["ok"] and out["warnings"] == [] and out["sha"]
        out = mk("check", real_plan)
        assert out["ok"] and out["sha"] and out["warnings"], out["plan"]

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
        assert out["ok"] is True
        assert any("forbidden" in w for w in out["warnings"]), out
        assert any("design" in w for w in out["warnings"]), out
        assert any("integration" in w for w in out["warnings"]), out

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

        print("mk-plan self-test ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        sys.exit(main(sys.argv[1:]))
