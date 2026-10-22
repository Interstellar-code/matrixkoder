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
  archive <id> [--data DIR] [--home DIR]
                                   refused on a live run; sets plans.status=archived (prior status
                                   in header_json.archived_from) and moves the plan's asset set (C2
                                   folder, legacy plan/version files, run folders) under
                                   <project>/archive/plans/<id>/ (a taken name gets a stamp)
  unarchive <id> [--data DIR] [--home DIR]
                                   archived plans only; puts every moved item back (refused when an
                                   original is occupied or an archived item is missing) and
                                   restores the prior status
  delete <id> [--data DIR] [--home DIR]
                                   archived plans only; refused on a live run or a held lane slot.
                                   Verified VACUUM INTO backup <project>/archive/<id>-pre-delete-
                                   <ts>.db, then one transaction purges the plan's run rows and the
                                   plans row. The archived folder is never unlinked (C10)
  export <path> [--data DIR] [--home DIR]
                                   rebuild the markdown from the store; body kept verbatim

A plan file: first line +++, TOML until the next +++ line, the rest is free
markdown, never parsed. A leading UTF-8 BOM is kept: check, approve and export
round-trip those bytes. Every command prints ONE JSON line.
A header [gates] value is either a plain string — a manual gate, never run and never
blocking — or a table [gates.<name>] whose cmd is the command, expect the exit code
(default 0) and timeout the seconds (default 600): a table gate is runnable, and check
rejects one without a cmd, or with an expect/timeout that is not an int.
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
import argparse, contextlib, datetime, fnmatch, glob, hashlib, importlib.util, json, os, re, shutil, stat, sys, tempfile, tomllib, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
_cspec = importlib.util.spec_from_file_location("mk_config", os.path.join(HERE, "mk-config.py"))
mk_config = importlib.util.module_from_spec(_cspec)
_cspec.loader.exec_module(mk_config)
_pspec = importlib.util.spec_from_file_location("mkpaths", os.path.join(HERE, "mkpaths.py"))
mkpaths = importlib.util.module_from_spec(_pspec)
_pspec.loader.exec_module(mkpaths)


def session_subagents():
    """session.subagents from <data>/matrixkoder.yaml, else the mk-config schema default. A phase
    without its own subagents key inherits this; any failure resolving or reading the config falls
    back to True — this tool must never fail because no config could be read."""
    try:
        lines = mk_config.read_lines(mkpaths.project_yaml(
            mk_config.mk_state.resolve_data(None)))
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
# bugs-tab: plan id `bugs` would collide with the permanent bugs run's plan_summary row
RESERVED_PLAN_ERR = "plan id 'bugs' is reserved for the project's bugs run: pick another plan id"
ROUTE_RE = re.compile(r"\S+( \S+){1,2}")  # "<agent> <model> [<effort>]"; "" means Jev
TOP_KEYS = ("plan", "status", "runs", "gates", "final_gate", "phase")  # header keys consumed by C2
FINAL_EVERY = ("merge", "phase", "run_end")  # when a [[final_gate]] runs (F.2)
PHASE_KEYS = ("id", "title", "wave", "deps", "owned", "forbidden", "route", "reviewer", "size",
              "risk", "review", "qa", "commit", "subagents", "lane_group", "task", "parallel")
TASK_KEYS = ("id", "title", "acceptance", "refs", "subagent", "owned", "deps", "parallel",
             "route", "subtask", "gate", "manual", "critical", "risk")
SUB_KEYS = ("id", "title", "acceptance", "refs", "owned", "deps", "parallel", "route",
            "gate", "manual", "critical", "risk")
TOML_LINE_RE = re.compile(r"line (\d+)")
NEW_PLAN_FROM = "2026-10-20"  # a plan created on or after this day gets the strict required-key check
BODY_SECTIONS = ("Summary", "Goal", "Design / context", "Phases", "Risks", "Open questions", "Log")
HEADER_PARTS = ("header phases", "tasks", "acceptance", "final gate")  # C1: facts read from the header
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
H2_RE = re.compile(r"##[ \t]+(.+?)[ \t]*")
SUMMARY_LINE_RE = re.compile(r"\s*-\s*(Phases|Method|Lanes|Gates)\s*:\s*(.*?)\s*")


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


def acceptance_lines_of(item_id, value):
    """F.2 acceptance → (flat text, lines). A TOML array is one line per entry; the flat text joins
    them with a newline so `tasks.acceptance` (one TEXT column) still holds every line. A plain
    string is one line. Each line is named `<item id>.a<n>` by position, which is what a gate's
    `lines` refers to."""
    if isinstance(value, list):
        texts = [str(x) for x in value]
    elif value is None or value == "":
        texts = []
    else:
        texts = [str(value)]
    base = f"{item_id}" if item_id else ""
    return "\n".join(texts), [{"id": f"{base}.a{i + 1}", "text": t} for i, t in enumerate(texts)]


def gate_list_of(value):
    """F.2 task gate: a table `{cmd, expect = 0, timeout = 600, lines = []}` or a list of them.
    Always a list so a caller can walk it; absent is []. A malformed element (a string, an int)
    stays RAW here for check_plan to reject — the same "fill only when the key is absent" rule the
    F.0 header gates follow."""
    if value is None or value == "":
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for g in items:
        if not isinstance(g, dict):
            out.append(g)
            continue
        lines = g.get("lines", [])
        out.append({"cmd": g.get("cmd"), "expect": g.get("expect", 0),
                    "timeout": g.get("timeout", 600),
                    "lines": list(lines) if isinstance(lines, list) else lines})
    return out


def gate_item_json(item_id, item):
    """F.2: the per-item keys shared by task_json and sub_json — acceptance (flat text) plus its
    lines, gate (always a list), manual, critical (default false) and risk (the plan's base risk,
    an int 1-5, default 1)."""
    text, lines = acceptance_lines_of(item_id, item.get("acceptance"))
    return {"acceptance": text, "acceptance_lines": lines,
            "gate": gate_list_of(item.get("gate")), "manual": item.get("manual", ""),
            "critical": bool(item.get("critical", False)), "risk": item.get("risk", 1)}


def final_gates_of(header):
    """F.2 header [[final_gate]]: id, cmd, expect (default 0), every (default "merge"), timeout
    (default 600), needs (default []), covers (default []). One block or a list of them; a
    malformed element stays raw for check_plan. F.0's [gates] tables are untouched."""
    raw = header.get("final_gate")
    items = raw if isinstance(raw, list) else ([] if raw is None else [raw])
    out = []
    for g in items:
        if not isinstance(g, dict):
            out.append(g)
            continue
        out.append({"id": str(g.get("id", "")), "cmd": g.get("cmd"), "expect": g.get("expect", 0),
                    "every": g.get("every", "merge"), "timeout": g.get("timeout", 600),
                    "needs": as_list(g.get("needs")), "covers": as_list(g.get("covers"))})
    return out


def sub_json(s):
    out = {"id": str(s.get("id", "")), "title": s.get("title", ""),
           "refs": as_list(s.get("refs")),
           "owned": as_list(s.get("owned")), "deps": deps_list(s.get("deps")),
           "parallel": parallel_of(s), "route": route_of(s)}
    out.update(gate_item_json(out["id"], s))
    for k, v in s.items():
        if k not in SUB_KEYS:
            out[k] = jsonable(v)
    return out


def task_json(t):
    out = {"id": str(t.get("id", "")), "title": t.get("title", ""),
           "refs": as_list(t.get("refs")),
           "subagent": bool(t.get("subagent", False)), "owned": as_list(t.get("owned")),
           "deps": deps_list(t.get("deps")), "parallel": parallel_of(t), "route": route_of(t),
           "subtasks": [sub_json(s) for s in as_list(t.get("subtask"))]}
    out.update(gate_item_json(out["id"], t))
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


def _gate_json(v):
    """One gate for C2. A plain string gate stays that string: it is manual — never run and it
    never blocks a run. A [gates.<name>] table is runnable and becomes {cmd, expect, timeout},
    with expect 0 and timeout 600 filled in only when the key is ABSENT, so a malformed
    expect = "zero" passes through for check_plan to reject."""
    if not isinstance(v, dict):
        return jsonable(v)
    return {"cmd": v.get("cmd"), "expect": v.get("expect", 0), "timeout": v.get("timeout", 600)}


def plan_json(header, sha):
    gates = header.get("gates")
    out = {"ok": True, "plan": header.get("plan", ""), "status": header.get("status", "draft"),
           "runs": as_list(header.get("runs")), "sha": sha,
           "gates": {k: _gate_json(v) for k, v in (gates if isinstance(gates, dict) else {}).items()},
           "final_gates": final_gates_of(header),
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


def body_lines(body):
    """The body's lines outside HTML comments (the template's worked examples) and fenced code."""
    kept, fenced = [], False
    for line in HTML_COMMENT_RE.sub("", body).split("\n"):
        st = line.lstrip()
        if st.startswith("```") or st.startswith("~~~"):
            fenced = not fenced
        elif not fenced:
            kept.append(line.rstrip("\r"))
    return kept


def section_block(body, name):
    """The lines under the `## <name>` heading, up to the next `##` heading; [] when absent."""
    block, inside = [], False
    for line in body_lines(body):
        m = H2_RE.fullmatch(line)
        if m:
            inside = m.group(1).casefold() == name.casefold()
        elif inside:
            block.append(line)
    return block


def body_sections(body):
    """C1: {section name: found?} for the seven body sections. Found = a `## <name>` heading."""
    heads = {m.group(1).casefold() for m in map(H2_RE.fullmatch, body_lines(body)) if m}
    return {name: name.casefold() in heads for name in BODY_SECTIONS}


def header_parts(out):
    """C1: the four facts read from the header. "header phases" counts [[phase]] blocks, never the
    Phases body section."""
    phases = out["phases"]
    tasks = [t for p in phases for t in p["tasks"]]
    return {"header phases": bool(phases), "tasks": bool(tasks),
            "acceptance": bool(tasks) and all(t["acceptance_lines"] for t in tasks),
            "final gate": any(isinstance(g, dict) and g.get("covers") for g in out.get("final_gates") or [])}


def classify(out, body):
    """C2: sections/parts (found|missing), sections_found, sections_total, missing and fit.
    fit: not_a_plan = no phase and no task; template = every section and part found; else partial."""
    secs, parts = body_sections(body), header_parts(out)
    missing = [n for n in BODY_SECTIONS if not secs[n]] + [n for n in HEADER_PARTS if not parts[n]]
    if not parts["header phases"] and not parts["tasks"]:
        fit = "not_a_plan"
    else:
        fit = "partial" if missing else "template"
    word = lambda ok: "found" if ok else "missing"
    return {"sections": {n: word(v) for n, v in secs.items()},
            "parts": {n: word(v) for n, v in parts.items()},
            "sections_found": sum(secs.values()), "sections_total": len(BODY_SECTIONS),
            "missing": missing, "fit": fit}


def is_new_plan(header):
    """True when `created` is a date on or after NEW_PLAN_FROM; an undated plan is an old one."""
    c = header.get("created")
    s = c.isoformat() if hasattr(c, "isoformat") else str(c or "")
    return re.match(r"\d{4}-\d{2}-\d{2}", s) is not None and s[:10] >= NEW_PLAN_FROM


def unticked_questions(body):
    """How many `- [ ]` lines the Open questions section holds."""
    return sum(1 for line in section_block(body, "Open questions") if re.match(r"\s*[-*]\s+\[ \]", line))


def summary_drift(out, body):
    """Warnings for a Summary line that differs from the header. A value still in <placeholder>
    form is not compared, and a missing line is not reported."""
    phases, finals, warnings = out["phases"], out.get("final_gates") or [], []
    for line in section_block(body, "Summary"):
        m = SUMMARY_LINE_RE.fullmatch(line)
        if not m or m.group(2).startswith("<"):
            continue
        key, val = m.groups()
        if key == "Phases":
            n = re.match(r"\d+", val)
            if n and int(n.group()) != len(phases):
                warnings.append(f"Summary says {n.group()} phases; the header has {len(phases)}")
        elif key == "Method" and phases:
            mm = re.match(r"(waves|single lane)\s*·\s*subagents\s+(on|off|mixed)\b", val, re.I)
            if mm:
                waves = "waves" if len({repr(p["wave"]) for p in phases}) > 1 else "single lane"
                subs = {bool(p["subagents"]) for p in phases}
                sub = "mixed" if len(subs) > 1 else ("on" if True in subs else "off")
                if (mm.group(1).lower(), mm.group(2).lower()) != (waves, sub):
                    warnings.append(f"Summary Method says {mm.group(1)} · subagents {mm.group(2)}; "
                                    f"the header says {waves} · subagents {sub}")
        elif key == "Lanes":
            for g in sorted({p["lane_group"] for p in phases if p["lane_group"]}):
                if not re.search(r"(?<![\w-])" + re.escape(g) + r"(?![\w-])", val):
                    warnings.append(f"Summary Lanes does not name lane_group {g}")
        elif key == "Gates":
            for i, g in enumerate(finals):
                if isinstance(g, dict) and (str(final_gate_id(g, i)) not in val
                                            or (g.get("cmd") and g["cmd"] not in val)):
                    warnings.append(f"Summary Gates does not list final gate {final_gate_id(g, i)} as id = cmd")
    return warnings


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
    if _mkdb().is_bugs_run(str(out["plan"])):
        errors.append(RESERVED_PLAN_ERR)

    # F.0 gate shape: a plain string gate is manual (never run, never blocks), a
    # [gates.<name>] table is runnable. Only its shape is checked here — running it is the
    # run's job. expect/timeout may be absent (0 / 600); a present-but-wrong value is an error.
    header_gates = header.get("gates")
    if "gates" in header and not isinstance(header_gates, dict):
        errors.append("gates is not a table")
    elif isinstance(header_gates, dict):
        for name, g in header_gates.items():
            if isinstance(g, str):
                continue  # manual: never run, never blocks
            if not isinstance(g, dict):
                errors.append(f"gate {name}: a gate is a string (manual) or a table "
                              f"(cmd/expect/timeout), not {type(g).__name__}")
                continue
            if not isinstance(g.get("cmd"), str) or not g["cmd"].strip():
                errors.append(f"gate {name}: a table gate needs cmd (a non-empty string)")
            if "expect" in g and type(g["expect"]) is not int:
                errors.append(f"gate {name}: expect must be an int, not {g['expect']!r}")
            if "timeout" in g and type(g["timeout"]) is not int:
                errors.append(f"gate {name}: timeout must be an int, not {g['timeout']!r}")

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
    if not str(header.get("description") or "").strip():
        warnings.append("no description key in the header; advised: two to five plain sentences "
                        "(plans.header_json only)")
    # TPL.2: a plan created on or after NEW_PLAN_FROM gets the REQUIRED keys as errors; an older or
    # undated plan only a warning
    strict = errors if is_new_plan(header) else warnings
    if body is not None:
        secs = body_sections(body)
        for name in ("Summary", "Goal"):
            if not secs[name]:
                warnings.append(f"body has no '## {name}' section (see templates/plan.md)")
        warnings.extend(summary_drift(out, body))

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
            strict.append(f"phase {n + 1} has no id")
        elif not PHASE_ID_RE.fullmatch(p["id"]):
            warnings.append(f"phase id {pid!r} does not match [A-Z][A-Z0-9]{{0,7}}")
        rp = raw[n] if n < len(raw) and isinstance(raw[n], dict) else {}
        for key in ("title", "wave", "owned", "commit"):
            if key not in rp:
                strict.append(f"{pid}: no {key} key (required in a plan created on or after {NEW_PLAN_FROM})")
        if not str(rp.get("description") or "").strip():
            warnings.append(f"{pid}: no description; advised: two to five plain sentences")
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
        raw_tasks = as_list(rp.get("task"))
        for ti, t in enumerate(p["tasks"]):
            tid = t["id"] or f"{pid} task"
            if not t["id"]:
                errors.append(f"{pid}: task has no id")
            rt = raw_tasks[ti] if ti < len(raw_tasks) and isinstance(raw_tasks[ti], dict) else {}
            if "title" not in rt:
                strict.append(f"{tid}: no title key (required in a plan created on or after {NEW_PLAN_FROM})")
            if not t["acceptance_lines"]:
                warnings.append(f"{tid}: no acceptance")
            elif any(isinstance(g, dict) for g in t["gate"]):
                proved = {str(x) for g in t["gate"] if isinstance(g, dict) and isinstance(g.get("lines"), list)
                          for x in g["lines"]}
                loose = [ln["id"] for ln in t["acceptance_lines"] if ln["id"] not in proved]
                if loose:
                    warnings.append(f"{tid}: acceptance line(s) {', '.join(loose)} proved by no gate")
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
    check_gate_fields(out, errors, warnings)
    return errors, warnings


def final_gate_id(g, i):
    """The one name of a final gate: its id, else its 1-based position. check_gate_fields (the
    error label) and gate_fields_of (the frozen-field key) both use it, so an unnamed gate is
    called the same thing in a check error and in an approve `changed_gates` reply."""
    return g["id"] if isinstance(g, dict) and g.get("id") else i + 1


def check_gate_fields(out, errors, warnings):
    """F.2: the per-task gate/manual/critical/risk rules and the header [[final_gate]] blocks.
    A plan written before F keeps checking clean: no gate and no final gate are WARNINGS, never
    errors. A gate is an exact command, so a malformed one is an error here and a `lines id` must
    name an acceptance line of the SAME item."""
    # F.2 task/subtask fields. ids collect every task and subtask id for `covers`.
    items = [(t, t["id"] or "") for p in out["phases"] for t in p["tasks"]]
    items += [(s, s["id"] or "") for t, _tid in items for s in t.get("subtasks") or []]
    task_ids = [tid for t, tid in items if tid]
    for t, tid in items:
        rid = tid or "task"
        gates = t.get("gate") or []
        manual = t.get("manual", "")
        if gates and manual:
            errors.append(f"{rid}: gate and manual are exclusive; pick one")
        elif not gates and not manual:
            warnings.append(f"{rid}: neither gate nor manual line")
        lines = {ln["id"] for ln in t.get("acceptance_lines") or []}
        for i, g in enumerate(gates):
            glabel = f"{tid} gate" if len(gates) == 1 else f"{rid} gate {i + 1}"
            if not isinstance(g, dict):
                errors.append(f"{glabel}: a gate is a table (cmd/expect/timeout/lines), "
                              f"not {type(g).__name__}")
                continue
            if not isinstance(g.get("cmd"), str) or not g["cmd"].strip():
                errors.append(f"{glabel}: needs cmd (a non-empty string)")
            if type(g.get("expect")) is not int:
                errors.append(f"{glabel}: expect must be an int, not {g.get('expect')!r}")
            if type(g.get("timeout")) is not int:
                errors.append(f"{glabel}: timeout must be an int, not {g.get('timeout')!r}")
            glines = g.get("lines", [])
            if not isinstance(glines, list):
                errors.append(f"{glabel}: lines must be a list of acceptance line ids")
                continue
            for lid in glines:
                if str(lid) not in lines:
                    errors.append(f"{glabel}: lines id {lid!r} is not an acceptance line of {rid}")
        risk = t.get("risk", 1)
        if type(risk) is not int or isinstance(risk, bool) or not 1 <= risk <= 5:
            errors.append(f"{rid}: risk {risk!r} is not an int 1-5")
    # F.2 header [[final_gate]] blocks. F.0's [gates] tables and string gates are checked above,
    # unchanged: only the shape of the new key is added here.
    finals = out.get("final_gates") or []
    for i, g in enumerate(finals):
        if not isinstance(g, dict):
            label = f"final gate {final_gate_id(g, i)}"
            errors.append(f"{label}: a final gate is a table "
                          f"(id/cmd/expect/every/timeout/needs/covers), not {type(g).__name__}")
            continue
        label = f"final gate {final_gate_id(g, i)}"
        if not isinstance(g.get("cmd"), str) or not g["cmd"].strip():
            errors.append(f"{label}: needs cmd (a non-empty string)")
        if type(g.get("expect")) is not int:
            errors.append(f"{label}: expect must be an int, not {g.get('expect')!r}")
        if type(g.get("timeout")) is not int:
            errors.append(f"{label}: timeout must be an int, not {g.get('timeout')!r}")
        if g["every"] not in FINAL_EVERY:
            errors.append(f"{label}: bad every {g['every']!r} ({'|'.join(FINAL_EVERY)})")
        covers = g.get("covers") or []
        if not covers:
            errors.append(f"{label}: covers is required (the task ids this gate proves)")
        for cid in covers:
            if str(cid) not in task_ids:
                errors.append(f"{label}: covers {cid!r} is not a task in this plan")
    if not finals:
        warnings.append("no final_gate in the plan: F.2 wants one plan-level gate with covers")


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


def status_precheck(path, value):
    """The checks every `set --status` runs before it writes anything (file or store): a known
    status, and for approved the plan check (F3.1). Returns the cross-run note, or None."""
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
    return note


def set_status(path, value):
    note = status_precheck(path, value)
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
    return with_store_state(read_plan(a.path)[1], a.data, a.home)


def cmd_check(a):
    if a.classify:  # a file with no +++ first line is a class (not_a_plan), not an error
        text, sha = load(a.path)
        if text.lstrip("﻿").split("\n", 1)[0].strip() != "+++":
            return {"ok": True, "plan": "", "sha": sha, "warnings": [],
                    **classify({"phases": [], "final_gates": []}, text)}
    header, out, _ = read_plan(a.path)
    text, _ = load(a.path)
    _lines, close = split_plan(text)
    body = body_md_of(text, close)
    errors, warnings = check_plan(header, out, body)
    cls = classify(out, body)
    if errors:
        raise Fail(errors, warnings=warnings, **(cls if a.classify else {}))
    out["warnings"] = warnings
    out.update(cls)
    return with_store_state(out, a.data, a.home)


def is_plan_id_arg(arg):
    """RUN: `set` and `add-run` also take a plan id: no `/`, no `.md` suffix and not an existing
    file. The file-path form is exactly as it was."""
    arg = str(arg)
    return "/" not in arg and not arg.endswith(".md") and not os.path.exists(arg) \
        and PLAN_ID_RE.fullmatch(arg) is not None


def plan_in_store(path, data, home):
    """(db, con, plan_id, row) when the store already holds this plan, else None: a file plan with
    no store row keeps today's file-only behaviour. `path` is the plan file, or (RUN) a plan id:
    the id form reads no file and must name a stored plan. The connection stays open for the
    caller to close. A file that does not parse is None too: the file path reports its own error."""
    data = resolve_data_arg(data)
    if is_plan_id_arg(path):
        if not store_present(data):
            raise Fail([f"no store: {path!r} is a plan id and needs the project store ({data})"])
        plan_id = str(path)
    else:
        if not store_present(data):
            return None
        try:
            _header, out, _sha = read_plan(path)
        except Fail:
            return None
        plan_id = str(out.get("plan") or "")
        if not plan_id:
            return None
    db = _mkdb()
    con = db.open_project(db.resolve_home(home), data_dir=mkpaths.project_folder(data, home))
    row = con.execute("SELECT status, header_json FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row:
        con.close()
        if is_plan_id_arg(path):
            raise Fail([f"no plan {plan_id} in the store"])
        return None
    return db, con, plan_id, row


def cmd_set(a):
    """set --status parked and back is pause/resume (C10). When the store holds the plan, plans.status
    changes there and the file's bytes are not touched (STORE.2: after import the store is the
    source); a file plan with no store row is rewritten as before. A plan id (RUN) works on the
    store row only, and refreshes the root plan_summary the dashboard List reads."""
    by_id = is_plan_id_arg(a.path)
    held = plan_in_store(a.path, a.data, a.home)
    if held is None:
        return set_status(a.path, a.status)
    db, con, plan_id, row = held
    try:
        note = None
        if by_id:
            if a.status not in STATUSES:
                raise Fail([f"bad status {a.status!r} ({'|'.join(STATUSES)})"])
        else:
            note = status_precheck(a.path, a.status)
        header = header_of(row)
        header["status"] = a.status
        con.execute("UPDATE plans SET status=?, header_json=? WHERE id=?",
                    (a.status, json.dumps(header, sort_keys=True), plan_id))
        con.commit()
    finally:
        con.close()
    out = {"ok": True, "status": a.status, "plan": plan_id, "store": True}
    if note:
        out["warnings"] = [note]
    if by_id:
        err = _sync_summary(db, resolve_data_arg(a.data), db.resolve_home(a.home), plan_id)
        if err:
            out["warnings"] = out.get("warnings", []) + [err]
    return out


def cmd_add_run(a):
    """add-run: with the plan in the store the run list changes there (header_json runs) and the
    file is not touched; a file plan with no store row keeps today's file rewrite. A plan id (RUN)
    works on the store row only and refreshes the root plan_summary."""
    by_id = is_plan_id_arg(a.path)
    held = plan_in_store(a.path, a.data, a.home)
    if held is None:
        return add_run(a.path, a.run)
    db, con, plan_id, row = held
    try:
        if not a.run or not RUN_ID_RE.fullmatch(a.run):
            raise Fail([f"bad run id {a.run!r} (letters, digits, . _ -; max 64)"])
        header = header_of(row)
        runs = [str(r) for r in as_list(header.get("runs"))]
        if a.run in runs:  # idempotent, like the file form
            return {"ok": True, "runs": runs, "added": False}
        header["runs"] = runs + [a.run]
        con.execute("UPDATE plans SET header_json=? WHERE id=?",
                    (json.dumps(header, sort_keys=True), plan_id))
        con.commit()
        out = {"ok": True, "runs": header["runs"], "added": True}
    finally:
        con.close()
    if by_id:
        err = _sync_summary(db, resolve_data_arg(a.data), db.resolve_home(a.home), plan_id)
        if err:
            out["warnings"] = [err]
    return out


def _id_list(text):
    """A comma-separated --owned / --deps value as a clean list: spaces and empty parts dropped."""
    return [x.strip() for x in str(text).split(",") if x.strip()]


def item_set(plan_id, item_id, fields, data, home):
    """STORE.3: change one phase, task or subtask row of a DRAFT plan's current version in the store.
    The plan file is neither read nor written. Everything is checked before the one UPDATE, so a
    refusal (exit 2) writes nothing: no plan, a plan that is not a draft, no stored items, no such
    id, a dep that is not an item of this version, a self-dep or a dep cycle."""
    if not PLAN_ID_RE.fullmatch(plan_id):
        raise Fail([f"bad plan id {plan_id!r} ({PLAN_ID_RE.pattern})"])
    if not any(v is not None for v in fields.values()):
        raise Fail(["nothing to change: give --title, --description, --acceptance, --owned or --deps"])
    if fields.get("title") is not None and not fields["title"].strip():
        raise Fail(["--title must not be empty"])
    data = resolve_data_arg(data)
    if not store_present(data):
        raise Fail([f"no store: item set needs the project store ({data})"])
    db = _mkdb()  # no ensure here: a store with no plan_items has nothing to edit, and a refusal writes nothing
    con = db.open_project(db.resolve_home(home), data_dir=mkpaths.project_folder(data, home))
    try:
        row = con.execute("SELECT version, status, header_json FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise Fail([f"no plan {plan_id} in the store"])
        status = row["status"] or header_of(row).get("status") or "draft"
        if status != "draft":
            raise Fail([f"plan {plan_id} is {status}, not draft: item set edits a draft plan only"])
        version = int(row["version"] or 0)
        items = db.plan_items(con, plan_id, version)
        if not items:
            raise Fail([f"plan {plan_id} has no stored items for version {version}: import it first"])
        by_id = {it["id"]: it for it in items}
        if item_id not in by_id:
            raise Fail([f"no item {item_id} in plan {plan_id} version {version}"])
        it = by_id[item_id]
        new = {"title": it["title"], "acceptance": it["acceptance"], "owned": ",".join(it["owned"]),
               "deps": ",".join(it["deps"])}
        extra = dict(it["extra"])
        if fields["title"] is not None:
            new["title"] = fields["title"].strip()
        if fields["acceptance"] is not None:
            new["acceptance"] = fields["acceptance"]
        if fields["owned"] is not None:
            new["owned"] = ",".join(_id_list(fields["owned"]))
        if fields["description"] is not None:
            if fields["description"].strip():
                extra["description"] = fields["description"]
            else:
                extra.pop("description", None)
        if fields["deps"] is not None:
            deps = _id_list(fields["deps"])
            for d in deps:
                if d == item_id:
                    raise Fail([f"self-dep: {item_id} depends on itself"])
                if d not in by_id:
                    raise Fail([f"missing dep: {d} is not an item of plan {plan_id} version {version}"])
            edges = {i["id"]: [d for d in i["deps"] if d in by_id] for i in items}
            edges[item_id] = deps
            path = db._cycle_through(item_id, edges)
            if path:
                raise Fail(["cycle: " + " -> ".join(path)])
            new["deps"] = ",".join(deps)
        extra_json = json.dumps(extra, sort_keys=True)
        old = {"title": it["title"], "acceptance": it["acceptance"], "owned": ",".join(it["owned"]),
               "deps": ",".join(it["deps"]), "description": it["extra"].get("description", "")}
        now = dict(new, description=extra.get("description", ""))
        changed = {k: now[k] for k in old if fields[k] is not None and old[k] != now[k]}
        db._begin(con)
        try:
            con.execute("UPDATE plan_items SET title=?, acceptance=?, owned=?, deps=?, extra_json=? "
                        "WHERE plan=? AND version=? AND id=?",
                        (new["title"], new["acceptance"], new["owned"], new["deps"], extra_json,
                         plan_id, version, item_id))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return {"ok": True, "plan": plan_id, "id": item_id, "version": version, "changed": changed}


def cmd_item_set(a):
    return item_set(a.plan, a.id, {k: getattr(a, k) for k in ("title", "description", "acceptance",
                                                              "owned", "deps")}, a.data, a.home)


# ---------- approve / export (D4): the store is optional ----------

def _mkdb():
    spec = importlib.util.spec_from_file_location("mkdb", os.path.join(HERE, "mkdb.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def store_present(data):
    """A store exists only when the resolved project mk.db is already there. Never create one.

    C1 (L3): the resolved folder is mkpaths.project_folder's — the C2 target for a migrated
    project, today's <data> for a pre-migration one — the same folder db.open_project will use.
    """
    if not data:
        return False
    try:
        return os.path.isfile(mkpaths.db_in(mkpaths.project_folder(data)))
    except mkpaths.Fail:
        return False


# the data-dir resolution is mk-state.py resolve_data's order, now read out of scripts/mkpaths.py
# (C1): mk-plan must never import mk-state (C3 — the plan tool is only ever a subprocess), and it
# must not edit it. Same order, same "never create a store to answer a read" rule.


def mk_home():
    # one MK home rule (C16): either name; mkdb.resolve_home checks that both agree
    return mkpaths.home()


def main_worktree(top):
    """A linked worktree (.git is a file + commondir) shares the main checkout's data dir."""
    return mkpaths.main_worktree(top)


def repo_top():
    """The main checkout of the nearest git repo above cwd, or None when there is none.
    The walk stops before $HOME; a linked worktree maps to its main checkout."""
    return mkpaths.repo_top()


def registered_data_dir(top):
    """The data dir this repo registered: the root projects table first, then <home>/projects.json.
    Neither read ever creates <home>/mk.db (the root store is checked before it is opened)."""
    return mkpaths.root_data_dir(top) or mkpaths.projects_json_data(top)


def resolve_data_arg(arg):
    """--data > $MATRIXKODER_DATA > the data dir this repo registered > <main checkout>/.matrixkoder.
    None means "no store": no --data, no env and no git repo above cwd (mk-state refuses instead;
    here a plan file must stay usable without a store)."""
    return mkpaths.data_dir(arg, required=False)


def side_store_warning(path):
    """A file-only answer next to an existing mk.db is the hermes-switchui trap: approve silently
    skipped the store that was on. Named in the reply instead of staying quiet."""
    side = mkpaths.db_in(os.path.dirname(os.path.abspath(path)))
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
    gates, the F.2 final gates, phases and the body, plus the plan-level `description` when it
    is a non-empty string. Other unknown header keys (created, design, integration, …) live
    here on a store without the D1.1 columns."""
    hdr = {"sha": sha, "status": status, "runs": out["runs"], "gates": out["gates"],
           "final_gates": out["final_gates"], "phases": out["phases"], "body": body_md}
    desc = out.get("description")
    if isinstance(desc, str) and desc.strip():
        hdr["description"] = desc
    return json.dumps(hdr, sort_keys=True)


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


# ---------- STORE: versioned plan items (C4) ----------

# keys of a phase, task or subtask dict that have a plan_items column of their own; every other key
# (wave, description, gate, manual, critical, risk, refs, ...) travels in extra_json
ITEM_COLUMN_KEYS = {
    "phase": {"id", "title", "deps", "owned", "route", "parallel", "tasks"},
    "task": {"id", "title", "acceptance", "acceptance_lines", "owned", "deps", "parallel", "route",
             "subtasks"},
    "subtask": {"id", "title", "acceptance", "acceptance_lines", "owned", "deps", "parallel", "route"},
}


def plan_item_rows(out):
    """The plan_items rows of one parsed plan, in flat_items order: the id, parent, type, title,
    acceptance, owned, deps, parallel and route columns plus extra_json (every other key of the
    item, so the rows alone rebuild the plan). A duplicate or empty id is refused: it would be two
    rows for one primary key."""
    nodes = {}
    for ph in out["phases"]:
        nodes[ph["id"]] = ("phase", ph)
        for t in ph["tasks"]:
            nodes.setdefault(t["id"], ("task", t))
            for sub in t.get("subtasks") or []:
                nodes.setdefault(sub["id"], ("subtask", sub))
    rows, seen = [], set()
    for n, (iid, typ, parent, title, acceptance, owned, deps, parallel, route) in enumerate(flat_items(out)):
        if not iid or iid in seen:
            raise Fail([f"item id {iid!r} is empty or repeated: a plan version holds one row per id "
                        f"(run check for the list)"], code=0, reason="empty or repeated item id")
        seen.add(iid)
        extra = {k: jsonable(v) for k, v in nodes[iid][1].items() if k not in ITEM_COLUMN_KEYS[typ]}
        try:
            route_json = route_json_of(route)
        except Fail:  # an unusable route is kept raw; check/approve refuse it, import does not
            route_json, extra["route"] = None, jsonable(route)
        rows.append({"id": iid, "parent": parent, "type": typ, "title": title, "acceptance": acceptance,
                     "owned": ",".join(str(x) for x in owned), "deps": ",".join(str(d) for d in deps),
                     "parallel": 1 if parallel else 0, "route_json": route_json,
                     "extra_json": json.dumps(extra, sort_keys=True), "ord": n})
    return rows


def _row_sig(r):
    return (r["parent"], r["type"], r["title"], r["acceptance"], r["owned"], r["deps"],
            r["parallel"], r["route_json"], r["extra_json"])


def item_changes(old_rows, new_rows):
    """The import change report: ids added, changed and removed versus the previous version."""
    old = {r["id"]: _row_sig(r) for r in old_rows}
    new = {r["id"]: _row_sig(r) for r in new_rows}
    return {"added": [r["id"] for r in new_rows if r["id"] not in old],
            "changed": [r["id"] for r in new_rows if r["id"] in old and old[r["id"]] != new[r["id"]]],
            "removed": [r["id"] for r in old_rows if r["id"] not in new]}


def stored_rows(db, con, plan_id, version, prev_header):
    """The previous version as plan_items-shaped rows: its stored rows, else (a plan stored before
    versioned items) rebuilt from the phases kept in header_json. [] when there is nothing."""
    got = db.plan_items(con, plan_id, version) if version else []
    if got:
        return [{"id": g["id"], "parent": g["parent"], "type": g["type"], "title": g["title"],
                 "acceptance": g["acceptance"], "owned": ",".join(g["owned"]),
                 "deps": ",".join(g["deps"]), "parallel": 1 if g["parallel"] else 0,
                 "route_json": g["route_json"],
                 "extra_json": json.dumps(g["extra"], sort_keys=True)} for g in got]
    phases = prev_header.get("phases")
    if isinstance(phases, list) and phases:
        try:
            return plan_item_rows({"phases": phases})
        except Fail:
            return []
    return []


def insert_items(con, plan_id, version, rows):
    for r in rows:
        con.execute(
            "INSERT INTO plan_items(plan, version, id, parent, type, title, acceptance, owned, deps,"
            " parallel, route_json, extra_json, ord) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, version, r["id"], r["parent"], r["type"], r["title"], r["acceptance"], r["owned"],
             r["deps"], r["parallel"], r["route_json"], r["extra_json"], r["ord"]))


def merged_header_json(out, sha, body_md, status, prev_header, source=None):
    """header_json_of plus what the store already holds: the run list (the store, not the file, owns
    it after import: runs the file lacks stay) and the import `source`."""
    hdr = json.loads(header_json_of(out, sha, body_md, status))
    runs = [str(r) for r in as_list(prev_header.get("runs"))]
    hdr["runs"] = runs + [str(r) for r in hdr["runs"] if str(r) not in runs]
    src = source or prev_header.get("source")
    if src:
        hdr["source"] = src
    return json.dumps(hdr, sort_keys=True)


def header_of(row):
    try:
        got = json.loads(row["header_json"] or "{}") if row else {}
    except ValueError:
        got = {}
    return got if isinstance(got, dict) else {}


def stored_plan(plan_id, data, home):
    """(status, runs, version) the store holds for a plan, read-only and never raising: a reader
    (parse/check) must work with no store, an unreadable one, or a plan the store does not know
    (None then, and the file answers as it always did)."""
    data = resolve_data_arg(data)
    if not plan_id or not store_present(data):
        return None
    try:
        db = _mkdb()
        home = db.resolve_home(home)
        con = db._open_read(mkpaths.project_db_in(mkpaths.project_folder(data, home)), db.SCHEMA)
        if con is None:
            return None
        try:
            row = con.execute("SELECT status, version, header_json FROM plans WHERE id=?",
                              (plan_id,)).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row:
        return None
    return row["status"], [str(r) for r in as_list(header_of(row).get("runs"))], row["version"]


def with_store_state(out, data=None, home=None):
    """Once a plan is in the store, its status and run list live there (STORE.2): the parse and check
    replies carry the store's values over the file's, so a run reads the same answer as before."""
    got = stored_plan(str(out.get("plan") or ""), data, home)
    if got:
        status, runs, _version = got
        if status:
            out["status"] = status
        out["runs"] = runs
    return out


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


def _ensure_plan_assets(data, plan_id):
    """C2: a migrated project's plan folder carries `assets/` beside plan.md, even when the plan
    has no asset yet. Best-effort like the plan.md copy — the row and the archive are the record."""
    with contextlib.suppress(OSError):
        os.makedirs(os.path.join(mkpaths.plan_dir(data, plan_id), "assets"), exist_ok=True)


def changed_gate_names(prev, out):
    """F.2: which frozen gate fields differ from the approved version in the store. `prev` is the
    stored header_json; with no stored version at all there is nothing to differ from, so the
    reply names nothing (a first approve is not a change). Keys are per item id, and a pre-F
    header that carries the same ids compares equal on the defaults, so only a real move is
    named. Sorted; [] when nothing moved."""
    if not (prev.get("phases") or prev.get("gates") or prev.get("final_gates")):
        return []
    old, new = gate_fields_of(prev), gate_fields_of(out)
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def approve_file(path, data, home, run, user_ack=None):
    """No store: validate and mark approved, exactly as set --status approved.
    With a store: validate, mark the plan version approved (STORE.1: the version a prior import
    wrote when the file is that same import, else a new version with its plan_items rows), write the
    run's tasks, copy the approved bytes into the archive and leave the original file untouched
    (C13 — approve copies, never moves; STORE.2 — approve no longer rewrites the file). An older
    version's plan_items rows are never deleted or rewritten.

    F.2 freezes the gate fields of an approved version: a changed file is a NEW version, never an
    overwrite, and the reply names the fields that moved (`P1.1.gate`, `gates.test`,
    `final_gate[fg1]`, …). Lowering a task's `risk` or flipping `critical` true -> false is refused
    unless `--user-ack "<quote>"` is given. The quote is recorded VERBATIM in the reply; the tool
    cannot prove it came from the user (anyone who can run mk-plan can pass a quote), so
    `user_ack` is a record of intent, not an authority check — the honest limit stated in the
    skill."""
    data = resolve_data_arg(data)
    header, out, sha = read_plan(path)
    errors, warnings = check_plan(header, out)
    if errors:
        raise Fail(errors, warnings=warnings)
    text, _ = load(path)
    open_qs = unticked_questions(body_md_of(text, split_plan(text)[1]))
    if open_qs:  # a question the author lives with stays unticked: warn, never block
        warnings = warnings + [f"{open_qs} unticked open questions"]
    counts = gate_counts_of(out)
    if not store_present(data):
        side = side_store_warning(path)
        if side:  # a store next to the file was skipped: say so instead of going quiet
            warnings = warnings + [side]
        reply = {"ok": True, "status": "approved", "store": False, "gate_counts": counts,
                 "changed_gates": [], "warnings": warnings}
        if user_ack is not None:  # recorded verbatim, the same way the store path does
            reply["user_ack"] = user_ack
        if out["status"] == "approved":
            return {**reply, "noop": True}
        set_status(path, "approved")
        return reply
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
    con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
    db.ensure_plan_items(con)
    added = []
    try:
        row = con.execute(
            "SELECT version, header_json, archived_path FROM plans WHERE id=?", (plan_id,)).fetchone()
        prev = header_of(row)
        if run is None and prev.get("runs"):  # after import the run list is the store's
            run_id = str(prev["runs"][0])
            if not RUN_ID_RE.fullmatch(run_id):
                raise Fail([f"bad run id {run_id!r} (letters, digits, . _ -; max 64)"])
        validate_tree(out, db, con, run_id)
        row_before = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        cur = int(row["version"] or 0) if row else 0
        pv = db.plan_version_row(con, plan_id, cur) if row else None
        # F.2: the gate fields of the approved version are frozen. Name what moved, and refuse a
        # lowering of the bar (risk down, critical off) unless the caller recorded the user's word.
        changed = changed_gate_names(prev, out)
        lowered = lowered_gate_fields(changed, gate_fields_of(prev), gate_fields_of(out))
        if lowered and not user_ack:
            raise Fail([f"gate field(s) lowered: {', '.join(lowered)} — the approved version is "
                        f"frozen; approve again with --user-ack \"<the user's words>\" to record "
                        f"the decision"])
        reply = {"ok": True, "status": "approved", "store": True, "gate_counts": counts,
                 "changed_gates": changed}
        if user_ack is not None:
            reply["user_ack"] = user_ack
        # the archived file holds the approved bytes; a second approve of those bytes is a no-op.
        # the file itself is never rewritten (STORE.2): the store and the archive are the record.
        current = open(path, "rb").read()
        approved = approved_bytes(current)
        archive_cur = os.path.join(mkpaths.plan_archives_dir(data, plan_id), f"{plan_id}-v{cur}.md")
        if pv and pv["sha"] == sha and pv["approved_at"] and os.path.isfile(archive_cur):
            return {**reply, "noop": True, "version": cur, "archived": archive_cur,
                    "tasks_run": run_id, "warnings": warnings}
        if row and os.path.isfile(row["archived_path"] or ""):  # a plan approved before versioned items
            archived_now = open(row["archived_path"], "rb").read()
            if archived_now == approved or archived_now == current:
                return {**reply, "noop": True, "version": cur,
                        "archived": row["archived_path"], "tasks_run": run_id,
                        "warnings": warnings}
        # a kill after the plans row committed and before the archive existed: finish THAT
        # version instead of inserting another. the row's archived_path is where it belongs.
        resume = None
        if row and row["archived_path"] and not os.path.isfile(row["archived_path"]):
            resume = row["archived_path"]
        # the file is the version a prior import wrote (same sha, not yet approved): approve THAT
        # version as it is, edited rows and all; anything else is a new version
        reuse = bool(not resume and pv and pv["sha"] == sha and not pv["approved_at"])
        version = cur if (resume or reuse) else cur + 1
        # a new version replaces this run's tasks from the previous import; other runs stay.
        # a resume keeps the tasks the killed approve already committed.
        if not resume:
            # drop this plan's tasks whether or not a plans row exists yet: run init may have
            # inserted the same ids, and a re-approve replaces them
            for tid, *_rest in flat_items({"phases": (prev.get("phases") or out["phases"])}):
                con.execute("DELETE FROM tasks WHERE run=? AND id=?", (run_id, tid))
            con.commit()
        archive = os.path.join(mkpaths.plan_archives_dir(data, plan_id), f"{plan_id}-v{version}.md")
        header_json = merged_header_json(out, sha, body_md, "approved", prev)
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
                if not resume:
                    if not reuse:  # a new version: its rows (an older version's rows stay as they are)
                        con.execute("DELETE FROM plan_items WHERE plan=? AND version=?", (plan_id, version))
                        con.execute("DELETE FROM plan_versions WHERE plan=? AND version=?", (plan_id, version))
                        insert_items(con, plan_id, version, plan_item_rows(out))
                        con.execute("INSERT INTO plan_versions(plan, version, sha, created) VALUES (?,?,?,?)",
                                    (plan_id, version, sha, now_local()))
                    con.execute("UPDATE plan_versions SET approved_at=? WHERE plan=? AND version=?",
                                (now_local(), plan_id, version))
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
            if not resume:  # undo this approve only; an earlier version's rows are never touched
                if reuse:
                    con.execute("UPDATE plan_versions SET approved_at=NULL WHERE plan=? AND version=?",
                                (plan_id, version))
                else:
                    con.execute("DELETE FROM plan_items WHERE plan=? AND version=?", (plan_id, version))
                    con.execute("DELETE FROM plan_versions WHERE plan=? AND version=?", (plan_id, version))
                if row_before:  # put the plans row back as it was
                    cols = row_before.keys()
                    con.execute(f"INSERT OR REPLACE INTO plans({', '.join(cols)}) "
                                f"VALUES ({', '.join('?' * len(cols))})", tuple(row_before))
                else:
                    con.execute("DELETE FROM plans WHERE id=?", (plan_id,))
            con.commit()
            raise Fail([f"archive failed: {e}"])
        if os.environ.get("MK_PLAN_KILL_AFTER") == "archive":
            raise SystemExit("MK_PLAN_KILL_AFTER=archive")
        # C13/STORE.2: approve copies, it never moves and never rewrites the file: the original keeps
        # its bytes (the store row, not the file's status line, says approved).
        # C2 (L3.2): a MIGRATED project keeps its live plan at <project>/plans/<id>/plan.md, the
        # reference the run tree hangs off. Copy the approved bytes there too (still a copy — the
        # user's file is untouched). A pre-migration project is unchanged: its plan_file is
        # <data>/plans/<id>.md, which approve has never written. Best-effort: the archive and the
        # row are the record.
        if mkpaths.migrated_slug(data):
            plan_md = mkpaths.plan_file(data, plan_id)
            if os.path.abspath(plan_md) != os.path.abspath(path):  # never rewrite the file approved
                with contextlib.suppress(OSError):
                    write_atomic(plan_md, approved)
            _ensure_plan_assets(data, plan_id)
        return {**reply, "version": version, "archived": archive, "tasks_run": run_id,
                "warnings": warnings}
    finally:
        con.close()


def gate_fields_of(out):
    """F.2: the snapshot of every gate field an approved version freezes — per task/subtask `gate`,
    `manual`, `critical` and `risk`, the header `[[final_gate]]` blocks and the F.0 `[gates]`
    values. The names are what the approve reply reports when they change. A key whose stored
    value is missing counts as changed when the new plan carries it, so a gate ADDED to an
    approved plan is named too."""
    snap = {}
    for p in out.get("phases") or []:
        for t in p["tasks"]:
            for item in [t] + list(t.get("subtasks") or []):
                iid = item.get("id") or ""
                if not iid:
                    continue
                snap[f"{iid}.gate"] = jsonable(item.get("gate") or [])
                snap[f"{iid}.manual"] = item.get("manual", "")
                snap[f"{iid}.critical"] = bool(item.get("critical", False))
                snap[f"{iid}.risk"] = item.get("risk", 1)
    for k, v in (out.get("gates") or {}).items():
        snap[f"gates.{k}"] = jsonable(v)
    for i, g in enumerate(out.get("final_gates") or []):
        snap[f"final_gate[{final_gate_id(g, i)}]"] = jsonable(g)
    return snap


def lowered_gate_fields(changed, old, new):
    """F.2: the changed field names that LOWER the plan's bar — a task `risk` int going down, or
    `critical` flipping true -> false. Those need `--user-ack`. Raising risk, adding a gate or
    setting critical true is a tightening and stays a plain version bump."""
    out = []
    for k in changed:
        if k.endswith(".risk"):
            before, after = old.get(k), new.get(k)
            if type(before) is int and type(after) is int and after < before:
                out.append(k)
        elif k.endswith(".critical") and old.get(k) is True and new.get(k) is False:
            out.append(k)
    return sorted(out)


def gate_counts_of(out):
    """F.2 approve counts: `gated N, manual M, final K`. N counts every task or subtask that
    carries at least one gate; M counts the ones whose only acceptance path is a `manual` line
    (a manual beside a gate is refused by check, so the two never double-count)."""
    gated = manual = 0
    for p in out["phases"]:
        for t in p["tasks"]:
            for item in [t] + list(t.get("subtasks") or []):
                if item.get("gate"):
                    gated += 1
                elif item.get("manual"):
                    manual += 1
    final = len(out.get("final_gates") or [])
    return {"gated": gated, "manual": manual, "final": final,
            "line": f"gated {gated}, manual {manual}, final {final}"}


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
    con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
    try:
        row = con.execute(
            "SELECT version, header_json, body_md, archived_path FROM plans WHERE id=? ORDER BY version DESC LIMIT 1",
            (plan_id,)).fetchone()
        approved_v = None
        if db._has_table(con, "plan_versions"):  # the archive is the last APPROVED version, which an
            got = con.execute(  # import of a newer draft does not move
                "SELECT MAX(version) AS v FROM plan_versions WHERE plan=? AND approved_at IS NOT NULL",
                (plan_id,)).fetchone()
            approved_v = got["v"] if got else None
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
    return {"ok": True, "plan": plan_id, "version": approved_v or row["version"], "path": archived,
            "text": rebuilt}


def cmd_approve(a):
    return approve_file(a.path, a.data, a.home, a.run, a.user_ack)


def cmd_export(a):
    out = export_plan(a.path, a.data, a.home)
    out.pop("text", None)
    return out


# ---------- import / delete (D1.2) ----------

def _file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inbox_plan(path, home):
    """L10.6: (inbox folder, plan id) when `path` is an inbox plan folder or its plan.md, else None."""
    real, box = os.path.realpath(path), os.path.realpath(mkpaths.inbox_dir(home))
    if os.path.isfile(real) and os.path.basename(real) == "plan.md":
        real = os.path.dirname(real)
    rel = os.path.relpath(real, box).split(os.sep)
    if len(rel) != 1 or rel[0] in ("", ".", "..") or not os.path.isdir(real):
        return None
    return real, rel[0]


def _inbox_moves(folder, data, plan_id):
    """The (src, dst) pairs that move an inbox plan into its project: plan.md -> the plan file, every
    other file -> <plan dir>/assets/ keeping its relative path. A target holding the same bytes is
    skipped (nothing moves twice); one holding other bytes, or a symlink in the inbox, refuses."""
    pairs = [(os.path.join(folder, "plan.md"), mkpaths.plan_file(data, plan_id))]
    assets = os.path.join(mkpaths.plan_dir(data, plan_id), "assets")
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            src = os.path.join(dirpath, name)
            rel = os.path.relpath(src, folder)
            if rel != "plan.md":
                pairs.append((src, os.path.join(assets, rel)))
    moves = []
    for src, dst in pairs:
        if os.path.islink(src):
            raise Fail([f"inbox file {src} is a symlink; nothing moved"], code=0, reason="symlink in the inbox")
        if os.path.lexists(dst):
            if os.path.islink(dst) or not os.path.isfile(dst) or _file_sha(dst) != _file_sha(src):
                raise Fail([f"target {dst} already exists with other content; nothing moved"],
                           code=0, reason="target exists")
        moves.append((src, dst))
    return moves


def _inbox_move(moves, folder):
    """Copy each file (size + sha256 checked), then unlink its source; then rmdir the now-empty
    inbox folder (os.rmdir only, never rmtree). A failure leaves the source in place."""
    done = []
    try:
        for src, dst in moves:
            if not os.path.exists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            if os.path.getsize(src) != os.path.getsize(dst) or _file_sha(src) != _file_sha(dst):
                raise OSError(f"copy of {src} to {dst} did not verify")
            os.unlink(src)
            done.append({"from": src, "to": dst})
    except OSError as e:
        raise Fail([f"inbox move stopped: {e}; moved so far: {len(done)}; sources kept"], moved=done)
    for dirpath, _dirs, _files in os.walk(folder, topdown=False):
        with contextlib.suppress(OSError):
            os.rmdir(dirpath)
    return done


def import_file(path, data, home, dry_run=False):
    """Load one +++ plan into the store without approving it (C4): a new plan version — a plans row
    plus one plan_items row per phase, task and subtask (STORE.1) — and no task rows, no file
    change, no status flip; approve is the command that marks a version approved.
    Idempotent: re-importing the same bytes writes nothing. A changed file is a new version and the
    reply's `changes` lists the item ids added, changed and removed versus the previous one.
    The path given is kept in header_json as `source` (a reference only; nothing re-reads it).
    L10.6: a plan in <home>/inbox/<plan-id>/ (the folder or its plan.md) is then MOVED into the
    project: plan.md to the plan file, other files to <plan dir>/assets/; reply `moved` lists them."""
    source = os.path.abspath(path)
    data = resolve_data_arg(data)
    inbox = _inbox_plan(path, home)
    if inbox:
        path = os.path.join(inbox[0], "plan.md")
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
    if _mkdb().is_bugs_run(plan_id):
        raise Fail([RESERVED_PLAN_ERR])
    if inbox and inbox[1] != plan_id:
        raise Fail([f"inbox folder {inbox[1]!r} must equal the header plan id {plan_id!r}"],
                   code=0, reason="inbox folder name differs from the plan id")
    body_md = body_md_of(text, close)
    title = plan_title(body_md, plan_id)
    if not store_present(data):
        raise Fail([f"no store: import needs <data>/mk.db ({data})"])
    db = _mkdb()
    home = db.resolve_home(home)
    moves = _inbox_moves(inbox[0], data, plan_id) if inbox else []
    new_rows = plan_item_rows(out)
    if not dry_run and mkpaths.migrated_slug(data):
        _ensure_plan_assets(data, plan_id)
    con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
    try:
        row = con.execute("SELECT version, approved_at, header_json, status FROM plans WHERE id=?",
                          (plan_id,)).fetchone()
        cur = int(row["version"] or 0) if row else 0
        prev = header_of(row)
        if row and row["approved_at"]:
            # F.2: an approved version's gate fields are frozen. Overwriting header_json here would
            # make the next approve compare the file against itself and skip the --user-ack check
            # for a lowered risk/critical, so an import that would move a gate field is refused.
            moved = changed_gate_names(prev, out)
            if moved:
                raise Fail([f"plan {plan_id} is approved; import would change frozen gate field(s): "
                            f"{', '.join(moved)} — approve the edited file instead (a new version, "
                            f"--user-ack if it lowers risk or critical)"])
        pv = db.plan_version_row(con, plan_id, cur) if row else None
        unchanged = bool(row) and ((pv or {}).get("sha") or prev.get("sha")) == sha
        if unchanged:  # the same bytes: nothing is written, the version stays
            version = cur
            changes = {"added": [], "changed": [], "removed": []}
            n_items = len(db.plan_items(con, plan_id, cur))
        else:
            version = cur + 1
            changes = item_changes(stored_rows(db, con, plan_id, cur, prev), new_rows)
            n_items = len(new_rows)
            if not dry_run:
                db.ensure_plan_items(con)
                cols = new_plan_columns(header, out)
                header_json = merged_header_json(
                    out, sha, body_md, str((row["status"] if row else None) or out["status"] or "draft"),
                    prev, source)
                db._begin(con)
                try:
                    # rows of a version nobody references yet (a half-removed plan) are replaced
                    con.execute("DELETE FROM plan_items WHERE plan=? AND version=?", (plan_id, version))
                    con.execute("DELETE FROM plan_versions WHERE plan=? AND version=?", (plan_id, version))
                    insert_items(con, plan_id, version, new_rows)
                    con.execute("INSERT INTO plan_versions(plan, version, sha, created) VALUES (?,?,?,?)",
                                (plan_id, version, sha, now_local()))
                    write_plan_row(con, plan_id, dict(
                        version=version, title=title, header_json=header_json, body_md=body_md,
                        **cols),
                        # approved_at and status stay the store's once set (after import the store
                        # is the source); the version moves to the one just imported
                        update=["version", "title", "header_json", "body_md", "created", "design",
                                "integration"])
                    if row and "status" in plan_columns(con):
                        con.execute("UPDATE plans SET status=? WHERE id=? AND status IS NULL",
                                    (cols["status"], plan_id))
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
        reply = {"ok": True, "store": True, "plan": plan_id, "version": version,
                 "title": title, "noop": unchanged, "dry_run": bool(dry_run),
                 "items": n_items, "changes": changes}
    finally:
        con.close()
    if dry_run:
        reply["moved"] = [{"from": a, "to": b} for a, b in moves]
    else:
        reply["moved"] = _inbox_move(moves, inbox[0]) if inbox else []
        err = _sync_summary(db, data, home, plan_id)  # the dashboard List reads the root summary
        if err:
            reply["warnings"] = [err]
    return reply


def _open_plan_store(plan_id, data, home, verb):
    """Shared by archive/unarchive/delete: id check, store check, the open connection, the plans
    row and the plan's run ids. Raises Fail (connection closed) when the plan is not in the store."""
    if not PLAN_ID_RE.fullmatch(plan_id):
        raise Fail([f"bad plan id {plan_id!r} ({PLAN_ID_RE.pattern})"])
    data = resolve_data_arg(data)
    if not store_present(data):
        raise Fail([f"no store: {verb} needs the project store ({data})"])
    db = _mkdb()
    home = db.resolve_home(home)
    con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
    row = con.execute("SELECT status, header_json FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row:
        con.close()
        raise Fail([f"no plan {plan_id} in the store"])
    runs = [r["id"] for r in con.execute("SELECT id FROM runs WHERE plan=? ORDER BY id", (plan_id,))]
    return data, home, con, row, runs


def _live_runs(con, plan_id):
    return [r["id"] for r in con.execute(
        "SELECT id FROM runs WHERE plan=? AND status='active' ORDER BY id", (plan_id,))]


def _refuse_live(con, plan_id, verb):
    live = _live_runs(con, plan_id)
    if live:
        raise Fail([f"refusing to {verb} {plan_id}: live run(s) {', '.join(live)} — "
                    f"close the run first (C10)"])


def _free_name(path):
    """A name nothing uses yet: path itself, else path-<UTC stamp>[-n]. Never overwrites."""
    out, n = path, 0
    while os.path.exists(out):
        n += 1
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = f"{path}-{stamp}" + (f"-{n}" if n > 1 else "")
    return out


def _set_header(con, plan_id, status, moves=(), **keys):
    """Write plans.status and merge keys into header_json (a None value drops the key). moves is
    [(src, dst)]: a plans.archived_path under a src is rewritten, so mk-state's plan fallback
    follows the files."""
    row = con.execute("SELECT header_json, archived_path FROM plans WHERE id=?", (plan_id,)).fetchone()
    header = json.loads(row["header_json"] or "{}")
    for k, v in keys.items():
        if v is None:
            header.pop(k, None)
        else:
            header[k] = v
    path = row["archived_path"]
    for a, b in moves:
        if path and (path == a or path.startswith(a + os.sep)):
            path = b + path[len(a):]
            break
    con.execute("UPDATE plans SET status=?, header_json=?, archived_path=? WHERE id=?",
                (status, json.dumps(header), path, plan_id))
    con.commit()
    return header


def _sync_summary(db, data, home, plan_id, gone=False):
    """Keep the root plan_summary projection current: refresh the project's rows, or drop this
    plan's row on delete. Returns an error text or None (the store change is already committed)."""
    slug = mkpaths.migrated_slug(data=data, root=home)
    if not slug:
        return None
    try:
        if gone:
            root = db.open_root(home)
            try:
                root.execute("DELETE FROM plan_summary WHERE project=? AND plan=?", (slug, plan_id))
                root.commit()
            finally:
                root.close()
        else:
            db.refresh_plan_summaries(home, slug, data_dir=mkpaths.project_folder(data, home))
    except Exception as e:
        return f"plan_summary not synced: {type(e).__name__}: {e}"
    return None


def _asset_pairs(data, plan_id, runs, base):
    """[(src, dst)] for every existing asset of the plan: the C2 folder (folder/), then the legacy
    plan file, version files and run folders (their layout under <data> kept) — all under base."""
    out = []
    folder = mkpaths.plan_dir(data, plan_id)
    if os.path.isdir(folder):
        out.append((folder, os.path.join(base, "folder")))
    for f in mkpaths.plan_legacy_files(data, plan_id, runs):
        out.append((f, os.path.join(base, os.path.relpath(f, data))))
    return out


def _move_all(pairs):
    for src, dst in pairs:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)


def archive_plan(plan_id, data, home):
    """Mark the plan archived (prior status in header_json.archived_from), then move its asset set
    — the C2 folder and the legacy plan files and run folders — into <project>/archive/plans/<id>.
    The pairs go into header_json (archived_to, archived_files) BEFORE the move, so a crash leaves
    unarchive a correct pointer and a re-run finishes the move. An archive whose recorded
    destination exists and has nothing left to move is refused; one with no record whose assets
    still sit in place is repaired."""
    data, home, con, row, runs = _open_plan_store(plan_id, data, home, "archive")
    db = _mkdb()
    try:
        _refuse_live(con, plan_id, "archive")
        header = json.loads(row["header_json"] or "{}")
        done_at = header.get("archived_to")
        if row["status"] == "archived" and done_at:
            pairs = [(a, b) for a, b in header.get("archived_files") or [] if os.path.exists(a)]
            if not pairs:
                if os.path.isdir(done_at):
                    raise Fail([f"plan {plan_id} is already archived at {done_at}"])
                pairs = _asset_pairs(data, plan_id, runs, done_at)  # recorded dir lost: nothing moved yet
            keep = {}
        else:
            base = _free_name(mkpaths.archive_dir(data, plan_id, home))
            pairs = _asset_pairs(data, plan_id, runs, base)
            if row["status"] == "archived" and not pairs:
                raise Fail([f"plan {plan_id} is already archived"])
            keep = {"archived_from": header.get("archived_from") or row["status"] or "done"} \
                if row["status"] != "archived" else {}
            keep.update(archived_to=base if pairs else None,
                        archived_files=[list(p) for p in pairs] if pairs else None)
        if pairs or row["status"] != "archived":
            _set_header(con, plan_id, "archived", moves=pairs, **keep)
        _move_all(pairs)
    finally:
        con.close()
    out = {"ok": True, "plan": plan_id, "moved": [dst for _, dst in pairs] or None}
    err = _sync_summary(db, data, home, plan_id)
    if err:
        out["warnings"] = [err]
    return out


def unarchive_plan(plan_id, data, home):
    """Put every recorded asset back where it came from (header_json.archived_files; a plan archived
    before that record keeps the single archived folder -> C2 folder pair) and restore the prior
    status. Refused, naming them, when an original path is occupied or an archived item is missing.
    An unmigrated plan with no folder and no record just changes status."""
    data, home, con, row, runs = _open_plan_store(plan_id, data, home, "unarchive")
    db = _mkdb()
    try:
        if row["status"] != "archived":
            raise Fail([f"plan {plan_id} is not archived (status {row['status']!r})"])
        header = json.loads(row["header_json"] or "{}")
        pairs = [tuple(p) for p in header.get("archived_files") or []]
        if not pairs:
            src = header.get("archived_to") or mkpaths.archive_dir(data, plan_id, home)
            here = os.path.isdir(mkpaths.plan_dir(data, plan_id)) or mkpaths.plan_legacy_files(data, plan_id, runs)
            if os.path.isdir(src) or (not here and (header.get("archived_to")
                                                    or mkpaths.migrated_slug(data=data, root=home))):
                pairs = [(mkpaths.plan_dir(data, plan_id), src)]  # else: archive died before the move
        todo, bad = [], []
        for orig, arch in pairs:
            if os.path.exists(arch):
                if os.path.exists(orig):
                    bad.append(f"original {orig} already exists")
                else:
                    todo.append((arch, orig))
            elif not os.path.exists(orig):  # neither side: lost; orig present alone = restored already
                bad.append(f"archived item {arch} is missing")
        if bad:
            raise Fail([f"refusing to unarchive {plan_id}: " + "; ".join(bad)])
        _move_all(todo)
        top = header.get("archived_to")
        for arch, _ in todo:  # drop the folders the move left empty, up to the archive root
            d = os.path.dirname(arch)
            while top and (d + os.sep).startswith(top + os.sep) \
                    and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                d = os.path.dirname(d)
        _set_header(con, plan_id, header.get("archived_from") or "done",
                    moves=[(b, a) for a, b in pairs],
                    archived_from=None, archived_to=None, archived_files=None)
    finally:
        con.close()
    out = {"ok": True, "plan": plan_id, "moved": [dst for _, dst in todo] or None}
    err = _sync_summary(db, data, home, plan_id)
    if err:
        out["warnings"] = [err]
    return out


# tables keyed by run id, children first; a store older than a table skips it
PURGE_BY_RUN = ("tasks", "events", "artifacts", "acks", "waits", "change_log", "evidence",
                "risk_reasons", "reconcile_due", "event_src")


def _delete_checks(con, plan_id, verb="delete"):
    """Under the write lock: archived, no live run, no lane slot on a run. Returns the run ids."""
    st = con.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not st:
        raise Fail([f"no plan {plan_id} in the store"])
    if st["status"] != "archived":
        raise Fail([f"refusing to {verb} {plan_id}: status is {st['status']!r}, archive it first"])
    _refuse_live(con, plan_id, verb)
    held = [r["slot"] for r in con.execute(
        "SELECT slot FROM lane_slots WHERE run IN (SELECT id FROM runs WHERE plan=?)", (plan_id,))]
    if held:
        raise Fail([f"refusing to {verb} {plan_id}: lane slot(s) {', '.join(held)} still hold its runs"])
    return [r["id"] for r in con.execute("SELECT id FROM runs WHERE plan=? ORDER BY id", (plan_id,))]


# rows of the plan: its runs' own, plus import-time rows keyed run=<plan id> (no run exists yet)
RUN_KEYED = "(run IN (SELECT id FROM runs WHERE plan=?) OR run=?)"


def _child_counts(con, plan_id):
    """{table: rows} for the plan's runs, over every PURGE_BY_RUN table the store has."""
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {t: con.execute(f"SELECT COUNT(*) FROM {t} WHERE {RUN_KEYED}", (plan_id, plan_id)).fetchone()[0]
            for t in PURGE_BY_RUN if t in have}


def delete_plan(plan_id, data, home):
    """C10: delete an ARCHIVED plan. Checks under the write lock, a verified VACUUM INTO backup
    (which cannot run inside a transaction), then the lock again: the checks and the run set are
    re-verified (run set AND per-table child row counts: a write between the backup and the purge
    aborts, the backup stays) before one transaction drops the rows of the plan's runs, the runs and the plans
    row. The archived folder is never unlinked (C13)."""
    data, home, con, row, _ = _open_plan_store(plan_id, data, home, "delete")
    db = _mkdb()
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            runs = _delete_checks(con, plan_id)
            counts = _child_counts(con, plan_id)
        finally:
            con.rollback()
        backup = _predelete_backup(con, plan_id, mkpaths.archive_dir(data, None, home), counts)
        race = os.environ.get("MK_DELETE_RACE_SQL")
        if race:  # test seam: a write lands between the backup and the second lock
            con.execute(race)
            con.commit()
        con.execute("BEGIN IMMEDIATE")
        try:
            if _delete_checks(con, plan_id) != runs:
                raise Fail([f"refusing to delete {plan_id}: its runs changed during the backup; "
                            f"retry (backup kept at {backup})"])
            now = _child_counts(con, plan_id)
            for t, n in counts.items():
                if now.get(t) != n:
                    raise Fail([f"refusing to delete {plan_id}: {t} went from {n} to {now.get(t)} rows "
                                f"during the backup; retry (backup kept at {backup})"])
            have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            purged = {}
            for t in PURGE_BY_RUN:
                if t in have:
                    purged[t] = con.execute(
                        f"DELETE FROM {t} WHERE {RUN_KEYED}", (plan_id, plan_id)).rowcount
                if t == "tasks" and os.environ.get("MK_PURGE_FAIL") == "1":  # test seam: dies mid-purge
                    raise RuntimeError("injected purge failure")
            for t in ("plan_items", "plan_versions"):  # the plan's versioned items go with it
                if t in have:
                    n = con.execute(f"DELETE FROM {t} WHERE plan=?", (plan_id,)).rowcount
                    if n:
                        purged[t] = n
            purged["runs"] = con.execute("DELETE FROM runs WHERE plan=?", (plan_id,)).rowcount
            purged["plans"] = con.execute("DELETE FROM plans WHERE id=?", (plan_id,)).rowcount
            con.commit()
        except BaseException:
            con.rollback()
            raise
    finally:
        con.close()
    out = {"ok": True, "plan": plan_id, "backup": backup, "purged": purged}
    err = _sync_summary(db, data, home, plan_id, gone=True)
    if err:
        out["warnings"] = [err]
    return out


def _predelete_backup(con, plan_id, dest_dir, counts):
    """VACUUM INTO a temp file, open it, check integrity, the plan row and the plan's run count
    and every child table's row count against `counts` (taken under the lock), then rename into place. Any failure removes the temp file and raises
    before a row is touched (shape of mk-state archive_backup)."""
    import sqlite3
    os.makedirs(dest_dir, exist_ok=True)
    for old in glob.glob(os.path.join(glob.escape(dest_dir), glob.escape(plan_id) + "-pre-delete-*.tmp")):
        with contextlib.suppress(OSError):  # left by a killed backup
            os.remove(old)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = _free_name(os.path.join(dest_dir, f"{plan_id}-pre-delete-{stamp}.db"))
    tmp = final + ".tmp"
    try:
        if "1" in (os.environ.get("MK_DELETE_BACKUP_FAIL"), os.environ.get("MK_ARCHIVE_FAIL")):  # test seam: fails before any rename
            raise RuntimeError("injected archive backup failure")
        con.execute("VACUUM INTO ?", (tmp,))
        chk = sqlite3.connect("file:" + urllib.parse.quote(tmp) + "?mode=ro", uri=True)
        try:
            if chk.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("backup failed quick_check")
            if not chk.execute("SELECT 1 FROM plans WHERE id=?", (plan_id,)).fetchone():
                raise RuntimeError(f"backup has no plan {plan_id}")
            held = _child_counts(chk, plan_id)
        finally:
            chk.close()
        if held != counts:
            raise RuntimeError(f"backup rows {held} differ from the store {counts}")
        os.replace(tmp, final)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise Fail([f"pre-delete backup failed: {type(e).__name__}: {e}"])
    return final


def cmd_import(a):
    return import_file(a.path, a.data, a.home, a.dry_run)


def cmd_delete(a):
    return delete_plan(a.plan, a.data, a.home)


def cmd_archive(a):
    return archive_plan(a.plan, a.data, a.home)


def cmd_unarchive(a):
    return unarchive_plan(a.plan, a.data, a.home)


def build_parser():
    p = Parser(prog="mk-plan.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, func in (("parse", cmd_parse), ("check", cmd_check)):
        c = sub.add_parser(name)
        c.add_argument("path")
        c.add_argument("--data", default=None), c.add_argument("--home", default=None)
        if name == "check":
            c.add_argument("--classify", action="store_true")
        c.set_defaults(func=func)
    s = sub.add_parser("set")
    s.add_argument("path"), s.add_argument("--status", required=True)
    s.add_argument("--data", default=None), s.add_argument("--home", default=None)
    s.set_defaults(func=cmd_set)
    r = sub.add_parser("add-run")
    r.add_argument("path"), r.add_argument("--run", required=True)
    r.add_argument("--data", default=None), r.add_argument("--home", default=None)
    r.set_defaults(func=cmd_add_run)
    it = sub.add_parser("item").add_subparsers(dest="item_cmd", required=True)
    i = it.add_parser("set")
    i.add_argument("plan"), i.add_argument("id")
    for flag in ("title", "description", "acceptance", "owned", "deps"):
        i.add_argument(f"--{flag}", default=None)
    i.add_argument("--data", default=None), i.add_argument("--home", default=None)
    i.set_defaults(func=cmd_item_set)
    for name, func in (("approve", cmd_approve), ("export", cmd_export), ("import", cmd_import)):
        c = sub.add_parser(name)
        c.add_argument("path")
        c.add_argument("--data", default=None)
        c.add_argument("--home", default=None)
        if name == "approve":
            c.add_argument("--run", default=None)
            c.add_argument("--user-ack", default=None)
        if name == "import":
            c.add_argument("--dry-run", action="store_true")
        c.set_defaults(func=func)
    for name, func in (("delete", cmd_delete), ("archive", cmd_archive), ("unarchive", cmd_unarchive)):
        d = sub.add_parser(name)
        d.add_argument("plan")
        d.add_argument("--data", default=None)
        d.add_argument("--home", default=None)
        d.set_defaults(func=func)
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


# the advisories TPL.2 added to check (the self-test filters them out of the "otherwise quiet" asserts)
TPL_ADVICE_RE = re.compile(r"no description|body has no '## |: no acceptance$|proved by no gate"
                           r"|: no (title|wave|owned|commit) key|^Summary ")


def f2_check_self_test(tmp, mk, write, plan_text, phase, task):
    # ---------- parse: acceptance array -> acceptance_lines (<id>.a1, <id>.a2, in order) ----------
    text = plan_text(phases=phase("P1", tasks=task("P1.1", acceptance="ARR")) + "\n")
    arr = 'acceptance = ["alpha", "beta"]'
    p = write("f2-acc-arr.md", text.replace('acceptance = "ARR"', arr))
    t = mk("parse", p)["phases"][0]["tasks"][0]
    assert arr in open(p).read() and t["acceptance"] == "alpha\nbeta" \
        and t.get("acceptance_lines") == [{"id": "P1.1.a1", "text": "alpha"},
                                          {"id": "P1.1.a2", "text": "beta"}], t

    # a plain string stays that string and is exactly one line, <id>.a1
    p = write("f2-acc-str.md", plan_text(
        phases=phase("P1", tasks=task("P1.1", acceptance="only line")) + "\n"))
    t = mk("parse", p)["phases"][0]["tasks"][0]
    assert t["acceptance"] == "only line" \
        and t.get("acceptance_lines") == [{"id": "P1.1.a1", "text": "only line"}], t

    # ---------- parse: gate is ALWAYS a list; absent = []; manual ""; critical bool; risk 1 ----------
    p = write("f2-gate-one.md", plan_text(phases=phase(
        "P1", tasks=task("P1.1", acceptance="a", extra='  gate = { cmd = "true" }\n')) + "\n"))
    t = mk("parse", p)["phases"][0]["tasks"][0]
    assert t.get("gate") == [{"cmd": "true", "expect": 0, "timeout": 600, "lines": []}], t.get("gate")

    p = write("f2-gate-two.md", plan_text(phases=phase("P1", tasks=task(
        "P1.1", acceptance="a",
        extra='  gate = [{ cmd = "a" }, { cmd = "b", expect = 1, timeout = 2, lines = ["P1.1.a1"] }]\n')) + "\n"))
    t = mk("parse", p)["phases"][0]["tasks"][0]
    assert t.get("gate") == [{"cmd": "a", "expect": 0, "timeout": 600, "lines": []},
                             {"cmd": "b", "expect": 1, "timeout": 2, "lines": ["P1.1.a1"]}], t.get("gate")

    p = write("f2-none.md", plan_text(phases=phase("P1", tasks=task("P1.1", acceptance="a")) + "\n"))
    t = mk("parse", p)["phases"][0]["tasks"][0]
    assert "gate" in t and t["gate"] == [] and t["manual"] == "" and t["risk"] == 1 and \
        t["critical"] is False, t  # absent gate is [], manual "", risk 1, critical bool(False)

    # ---------- parse: [[final_gate]] defaults, and explicit values overriding them ----------
    fg_min = '[[final_gate]]\nid = "F1"\ncmd = "true"\ncovers = ["P1.1"]\n'
    manual_task = task("P1.1", acceptance="a", extra='  manual = "by hand"\n')
    p = write("f2-fg-default.md", plan_text(top=fg_min, phases=phase("P1", tasks=manual_task) + "\n"))
    out = mk("parse", p)
    assert out.get("final_gates") == [{"id": "F1", "cmd": "true", "expect": 0, "every": "merge",
                                       "timeout": 600, "needs": [], "covers": ["P1.1"]}] \
        and out["gates"] == {"test": "true"}, out  # F.0 header gates untouched by the new key

    p = write("f2-fg-explicit.md", plan_text(
        top='[[final_gate]]\nid = "F1"\ncmd = "true"\ncovers = ["P1.1"]\n'
            'expect = 3\ntimeout = 5\nevery = "phase"\nneeds = ["P1.1"]\n',
        phases=phase("P1", tasks=manual_task) + "\n"))
    fgs = mk("parse", p).get("final_gates") or []
    assert len(fgs) == 1, fgs
    fg = fgs[0] if fgs and isinstance(fgs[0], dict) else {}
    assert (fg["id"], fg["cmd"], fg["expect"], fg["every"], fg["timeout"], fg["needs"], fg["covers"]) \
        == ("F1", "true", 3, "phase", 5, ["P1.1"], ["P1.1"]), fg

    # ---------- check errors: one case per rule. each ok:false, exit 2, parse stays ok:true ----------
    def bad(name, phases, top="", want=None):
        q = write(name, plan_text(top=top, phases=phases))
        got = mk("check", q, code=2)
        parsed = mk("parse", q)  # parse never runs the check rules: the file is still parseable
        assert got["ok"] is False and parsed["ok"] is True \
            and "acceptance_lines" in parsed["phases"][0]["tasks"][0], (name, got, parsed)
        if want is not None:
            assert want in [str(e) for e in got["errors"]], (name, got["errors"])
        return got

    gated = task("P1.1", acceptance="a", extra='  gate = { cmd = "true" }\n')

    # E1: gate and manual are exclusive
    bad("f2-e1.md", phase("P1", tasks=task(
        "P1.1", acceptance="a", extra='  gate = { cmd = "true" }\n  manual = "by hand"\n')) + "\n",
        want="P1.1: gate and manual are exclusive; pick one")

    # E2: a gate table needs a non-empty cmd
    bad("f2-e2.md", phase("P1", tasks=task(
        "P1.1", acceptance="a", extra='  gate = { expect = 0 }\n')) + "\n",
        want="P1.1 gate: needs cmd (a non-empty string)")

    # E5: a gate line that is ANOTHER task's acceptance line
    bad("f2-e5.md", phase("P1", tasks=task(
        "P1.1", acceptance="a", extra='  gate = { cmd = "true", lines = ["P1.2.a1"] }\n')
        + task("P1.2", acceptance="b")) + "\n",
        want="P1.1 gate: lines id 'P1.2.a1' is not an acceptance line of P1.1")

    # E6: a gate list element that is not a table. one element -> "P1.1 gate", two -> "P1.1 gate 2"
    bad("f2-e6-single.md", phase("P1", tasks=task(
        "P1.1", acceptance="a", extra='  gate = ["true"]\n')) + "\n",
        want="P1.1 gate: a gate is a table (cmd/expect/timeout/lines), not str")
    bad("f2-e6-second.md", phase("P1", tasks=task(
        "P1.1", acceptance="a", extra='  gate = [{ cmd = "true" }, "true"]\n')) + "\n",
        want="P1.1 gate 2: a gate is a table (cmd/expect/timeout/lines), not str")

    # E7: lines must be a list of acceptance line ids
    bad("f2-e7.md", phase("P1", tasks=task(
        "P1.1", acceptance="a", extra='  gate = { cmd = "true", lines = "P1.1.a1" }\n')) + "\n",
        want="P1.1 gate: lines must be a list of acceptance line ids")

    # E8: risk is an int 1-5 (an out-of-range int and a string)
    bad("f2-e8-int.md", phase("P1", tasks=task(
        "P1.1", acceptance="a", extra="  risk = 9\n")) + "\n",
        want="P1.1: risk 9 is not an int 1-5")
    bad("f2-e8-str.md", phase("P1", tasks=task(
        "P1.1", acceptance="a", extra='  risk = "high"\n')) + "\n",
        want="P1.1: risk 'high' is not an int 1-5")

    # E9: a final gate needs covers
    bad("f2-e9.md", phase("P1", tasks=task("P1.1", acceptance="a")) + "\n",
        top='[[final_gate]]\nid = "F1"\ncmd = "true"\n',
        want="final gate F1: covers is required (the task ids this gate proves)")

    # E10: covers names something that is not a task in this plan
    bad("f2-e10.md", phase("P1", tasks=task("P1.1", acceptance="a")) + "\n",
        top='[[final_gate]]\nid = "F1"\ncmd = "true"\ncovers = ["NOPE"]\n',
        want="final gate F1: covers 'NOPE' is not a task in this plan")

    # E11: every is merge|phase|run_end
    bad("f2-e11.md", phase("P1", tasks=task("P1.1", acceptance="a")) + "\n",
        top='[[final_gate]]\nid = "F1"\ncmd = "true"\ncovers = ["P1.1"]\nevery = "weekly"\n',
        want="final gate F1: bad every 'weekly' (merge|phase|run_end)")

    # E12/E13: a final gate's expect/timeout must be ints
    bad("f2-e12.md", phase("P1", tasks=task("P1.1", acceptance="a")) + "\n",
        top='[[final_gate]]\nid = "F1"\ncmd = "true"\ncovers = ["P1.1"]\nexpect = "0"\n',
        want="final gate F1: expect must be an int, not '0'")
    bad("f2-e13.md", phase("P1", tasks=task("P1.1", acceptance="a")) + "\n",
        top='[[final_gate]]\nid = "F1"\ncmd = "true"\ncovers = ["P1.1"]\ntimeout = "60"\n',
        want="final gate F1: timeout must be an int, not '60'")

    # E15: a final gate is a table; with no id the label is its 1-based index
    bad("f2-e15.md", phase("P1", tasks=task("P1.1", acceptance="a")) + "\n",
        top='final_gate = ["x"]\n',
        want="final gate 1: a final gate is a table (id/cmd/expect/every/timeout/needs/covers), not str")

    # ---------- check warnings: W1, W2, and the plan that carries neither ----------
    def t1(w):  # the pre-existing T1 warnings: created/status/H1
        return ("created" in w) or ("status" in w) or ("H1" in w) or bool(TPL_ADVICE_RE.search(w))

    out = mk("check", write("f2-w1.md", plan_text(phases=phase("P1", tasks=gated) + "\n")))
    assert out["ok"] is True and not [w for w in out["warnings"] if "neither gate nor manual" in w] \
        and "no final_gate in the plan: F.2 wants one plan-level gate with covers" in out["warnings"], out

    out = mk("check", write("f2-w2.md", plan_text(
        top=fg_min, phases=phase("P1", tasks=task("P1.1", acceptance="a")) + "\n")))
    assert out["ok"] is True and not [w for w in out["warnings"] if "no final_gate" in w] \
        and "P1.1: neither gate nor manual line" in out["warnings"], out

    # a gate on one task, a manual on another and one final gate over both: no W1 and no W2
    out = mk("check", write("f2-quiet.md", plan_text(
        top='[[final_gate]]\nid = "F1"\ncmd = "true"\ncovers = ["P1.1", "P1.2"]\n',
        phases=phase("P1", tasks=gated
                     + task("P1.2", acceptance="b", extra='  manual = "by hand"\n')) + "\n")))
    # [w for w in ... ] must be empty AND the gate-bearing task must still report its F.2 keys
    qg = mk("parse", os.path.join(tmp, "f2-quiet.md"))["phases"][0]["tasks"][0].get("gate")
    assert out["ok"] is True and [w for w in out["warnings"] if not t1(w)] == [] \
        and isinstance(qg, list) and qg == [{"cmd": "true", "expect": 0, "timeout": 600, "lines": []}], \
        (out, qg)

    # ---------- subtask gates: a foreign line is E5, its own line checks clean ----------
    def sub(line_id):
        return ('  [[phase.task.subtask]]\n  id = "P1.1.1"\n  title = "s"\n  acceptance = "sub"\n'
                '  refs = []\n  owned = []\n'
                f'  gate = {{ cmd = "true", lines = ["{line_id}"] }}\n')

    bad("f2-sub-foreign.md", phase("P1", tasks=task(
        "P1.1", acceptance="a", extra='  manual = "by hand"\n' + sub("P1.1.a1"))) + "\n",
        want="P1.1.1 gate: lines id 'P1.1.a1' is not an acceptance line of P1.1.1")

    p = write("f2-sub-own.md", plan_text(
        top=fg_min, phases=phase("P1", tasks=task(
            "P1.1", acceptance="a", extra='  manual = "by hand"\n' + sub("P1.1.1.a1"))) + "\n"))
    out = mk("parse", p)
    subs = out["phases"][0]["tasks"][0].get("subtasks") or []
    st = subs[0] if subs and isinstance(subs[0], dict) else {}
    assert mk("check", p)["ok"] is True \
        and st.get("gate") == [{"cmd": "true", "expect": 0, "timeout": 600, "lines": ["P1.1.1.a1"]}] \
        and st.get("acceptance_lines") == [{"id": "P1.1.1.a1", "text": "sub"}], (out, st)

    # ---------- F.0 regression guard: the header [gates] contract has not moved ----------
    # F.0 regression guard, on purpose: the first four clauses ([gates.slow] and [gates.strict]
    # unchanged, a plain string gate still a string, the plan still checking clean) must hold on
    # main and on the F.2 tip alike. The F.2 clause that follows them — an absent task `gate` is a
    # list — is what makes this same assert fail on main, so the guard is never a no-op there.
    p = write("f2-f0.md", plan_text(
        head_extra='slow = { cmd = "sleep 1", expect = 7, timeout = 99 }\nstrict = "npm run strict"\n',
        phases=phase("P1", tasks=task("P1.1", acceptance="a")) + "\n"))
    out = mk("parse", p)
    assert out["gates"]["test"] == "true" \
        and out["gates"]["slow"] == {"cmd": "sleep 1", "expect": 7, "timeout": 99} \
        and out["gates"]["strict"] == "npm run strict" \
        and mk("check", p)["ok"] is True \
        and out["phases"][0]["tasks"][0].get("gate") == [], out


def f2_approve_self_test(tmp, mk, write, plan_text, phase, task, db, mkpaths):
    """F.2: approve counts every gate and manual line, a changed gate field is a NEW version that
    names the field, and lowering the bar (risk down, critical off) needs --user-ack. FAILS on
    main: no gate_counts, no changed_gates, and --user-ack does not exist there."""
    data = os.path.join(tmp, "f2-store")
    home = os.path.join(data, ".mk-home")
    os.makedirs(home, exist_ok=True)
    db.open_project(home, data_dir=mkpaths.project_folder(data, home)).close()

    def doc(pid, risk=4, gate='pytest -q', final="", head_test="true", crit=False, body="# body\n"):
        return plan_text(plan=f'plan = "{pid}"', top=final,
                         head_extra="", status='status = "draft"',
                         phases=phase("P1", tasks=task(
                             "P1.1", acceptance="a",
                             extra=(f'  gate = {{ cmd = "{gate}" }}\n'
                                    f'  critical = {str(crit).lower()}\n  risk = {risk}\n'))) + "\n").replace(
            'test = "true"', f'test = "{head_test}"') + body

    def approve(path, *args, code=0):
        return mk("approve", path, "--data", data, "--home", home, *args, code=code)

    # 1. no store: the reply still counts the gate lines (no version to compare against)
    d1, h1 = os.path.join(tmp, "f2-empty"), os.path.join(tmp, "f2-empty-home")
    counts = mk("approve", write("f2-counts.md", plan_text(
        top='[[final_gate]]\nid = "F1"\ncmd = "true"\ncovers = ["P1.1", "P1.2"]\n',
        phases=phase("P1", tasks=(
            task("P1.1", acceptance="a", extra='  gate = { cmd = "true" }\n')
            + task("P1.2", acceptance="b", extra='  manual = "read it by eye"\n'))) + "\n")),
        "--data", d1, "--home", h1)
    assert counts["gate_counts"]["line"] == "gated 1, manual 1, final 1", counts
    assert (counts["gate_counts"]["gated"], counts["gate_counts"]["manual"],
            counts["gate_counts"]["final"]) == (1, 1, 1), counts

    # 2. first approve with a store: version 1, nothing named as changed, no user_ack key
    p = write("f2a.md", doc("f2a"))
    v1 = approve(p, "--run", "f2r")
    assert v1["ok"] and v1["version"] == 1 and v1["changed_gates"] == [], v1
    assert "user_ack" not in v1, v1
    again = approve(p, "--run", "f2r")
    assert again.get("noop") is True and again["changed_gates"] == [], again

    # 3. a changed gate command is a NEW version naming the field; the old archive stays, and the
    #    file itself is left in place (C13)
    write("f2a.md", doc("f2a", gate="pytest -q -x"))
    v2 = approve(p, "--run", "f2r")
    assert v2["version"] == 2 and "P1.1.gate" in v2["changed_gates"], v2
    assert os.path.isfile(v1["archived"]) and v2["archived"] != v1["archived"], (v1, v2)
    assert os.path.isfile(p), "approve must leave the original file in place"

    # 4. lowering risk 4 -> 2 is refused without --user-ack (file and store unchanged), and
    #    accepted with it: the quote is recorded verbatim
    write("f2a.md", doc("f2a", risk=2, gate="pytest -q -x"))
    before = open(p, "rb").read()
    refused = approve(p, "--run", "f2r", code=2)
    assert refused["ok"] is False, refused
    assert any("gate field(s) lowered:" in e and "P1.1.risk" in e for e in refused["errors"]), refused
    assert open(p, "rb").read() == before
    con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
    try:
        assert con.execute("SELECT version FROM plans WHERE id='f2a'").fetchone()["version"] == 2
    finally:
        con.close()
    acked = approve(p, "--run", "f2r", "--user-ack", "the user said ship it")
    assert acked["version"] == 3 and acked["user_ack"] == "the user said ship it", acked
    assert "P1.1.risk" in acked["changed_gates"], acked

    # 5. critical true -> false is refused without the ack; false -> true is a plain version bump
    write("f2a.md", doc("f2a", risk=2, gate="pytest -q -x", crit=True))
    on = approve(p, "--run", "f2r")
    assert on["version"] == 4 and "P1.1.critical" in on["changed_gates"], on
    write("f2a.md", doc("f2a", risk=2, gate="pytest -q -x", crit=False))
    off = approve(p, "--run", "f2r", code=2)
    assert any("gate field(s) lowered:" in e and "P1.1.critical" in e for e in off["errors"]), off

    # 6. RAISING risk 2 -> 4 is a tightening: no ack needed, still a new version naming the field
    acked_off = approve(p, "--run", "f2r", "--user-ack", "drop the critical flag")
    assert acked_off["version"] == 5 and "P1.1.critical" in acked_off["changed_gates"], acked_off
    write("f2a.md", doc("f2a", risk=4, gate="pytest -q -x", crit=False))
    up = approve(p, "--run", "f2r")
    assert up["version"] == 6 and "P1.1.risk" in up["changed_gates"], up

    # 7. a NEW [[final_gate]] block and a changed header [gates] value are both named; the counts
    #    follow the file
    write("f2a.md", doc("f2a", risk=4, gate="pytest -q -x", crit=False,
                        final='[[final_gate]]\nid = "fg1"\ncmd = "true"\ncovers = ["P1.1"]\n'))
    fg = approve(p, "--run", "f2r")
    assert fg["version"] == 7 and fg["changed_gates"] == ["final_gate[fg1]"], fg
    assert fg["gate_counts"]["line"] == "gated 1, manual 0, final 1", fg
    write("f2a.md", doc("f2a", risk=4, gate="pytest -q -x", crit=False, head_test="false",
                        final='[[final_gate]]\nid = "fg1"\ncmd = "true"\ncovers = ["P1.1"]\n'))
    hg = approve(p, "--run", "f2r")
    assert hg["version"] == 8 and hg["changed_gates"] == ["gates.test"], hg

    # 8. import never rewrites an approved version's frozen gate fields: a file that LOWERS risk is
    #    refused (else the next approve would compare the file with itself and skip --user-ack);
    #    the store row stays, the approve still demands the ack, and a same-gates import is a noop
    imp = write("f2imp.md", doc("f2imp", risk=4))
    approve(imp, "--run", "f2ir")
    same = mk("import", imp, "--data", data, "--home", home)
    assert same["ok"] and same["noop"] is True, same
    write("f2imp.md", doc("f2imp", risk=2))
    refused = mk("import", imp, "--data", data, "--home", home, code=2)
    assert refused["ok"] is False and any("P1.1.risk" in e and "frozen gate field" in e
                                          for e in refused["errors"]), refused
    con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
    try:
        stored = json.loads(con.execute("SELECT header_json FROM plans WHERE id='f2imp'").fetchone()[0])
    finally:
        con.close()
    assert stored["phases"][0]["tasks"][0]["risk"] == 4, stored["phases"][0]["tasks"][0]
    low = approve(imp, "--run", "f2ir", code=2)
    assert any("gate field(s) lowered:" in e for e in low["errors"]), low

    # 9. an unnamed final gate has ONE number: the check error label and the changed_gates key
    #    both call the second unnamed gate "2" (1-based)
    def two_unnamed(cmd2):
        fin = ('[[final_gate]]\ncmd = "true"\ncovers = ["P1.1"]\n'
               f'[[final_gate]]\ncmd = {json.dumps(cmd2)}\ncovers = ["P1.1"]\n')
        return doc("f2un", final=fin)
    approve(write("f2un.md", two_unnamed("true")), "--run", "f2ur")
    write("f2un.md", two_unnamed("false"))
    moved = approve(os.path.join(tmp, "f2un.md"), "--run", "f2ur")
    assert moved["changed_gates"] == ["final_gate[2]"], moved
    nocmd = write("f2un2.md", doc("f2un2", final='[[final_gate]]\ncmd = "true"\ncovers = ["P1.1"]\n'
                                  '[[final_gate]]\ncovers = ["P1.1"]\n'))
    bad2 = mk("check", nocmd, code=2)
    assert any(e.startswith("final gate 2: needs cmd") for e in bad2["errors"]), bad2


def store_self_test(tmp, mk, write, plan_text, phase, task, db, mkpaths):
    """STORE.1-3: versioned plan_items, status and runs in the store, and item set. Every write goes
    to a temp home; `mk` runs the real CLI."""
    import subprocess
    sdata = os.path.join(tmp, "st-store")
    shome = os.path.join(sdata, ".mk-home")
    os.makedirs(shome)
    folder = mkpaths.project_folder(sdata, shome)
    db.open_project(shome, data_dir=folder).close()

    def st(*args, code=0):
        return mk(*args, "--data", sdata, "--home", shome, code=code)

    def conn():
        return db.open_project(shome, data_dir=folder)

    def rows(plan, version):
        con = conn()
        try:
            return [tuple(r) for r in con.execute(
                "SELECT * FROM plan_items WHERE plan=? AND version=? ORDER BY ord", (plan, version))]
        finally:
            con.close()

    def one(sql, *args):
        con = conn()
        try:
            r = con.execute(sql, args).fetchone()
            return dict(r) if r else None
        finally:
            con.close()

    def dump():
        con = conn()
        try:
            return {t: [tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                    for t in ("plans", "plan_items", "plan_versions", "tasks", "runs")}
        finally:
            con.close()

    def sha_of(path):
        return hashlib.sha256(open(path, "rb").read()).hexdigest()

    sub = ('  [[phase.task.subtask]]\n  id = "S1.1.1"\n  title = "leaf"\n  acceptance = "leaf done"\n'
           '  deps = "S1.1"\n  parallel = true\n  route = ""\n  owned = []\n')

    def stp(extra_task="", s11_title="first task", with_s21=True):
        body = phase("S1", extra='description = "phase why"\n', owned='"src/"', tasks=(
            task("S1.1", title=s11_title, acceptance="a1", owned='"src/a.py"',
                 extra='  description = "task why"\n' + sub) + task("S1.2", acceptance="b1") + extra_task)) + "\n"
        if with_s21:
            body += phase("S2", wave=2, deps='"S1"', tasks=task("S2.1", acceptance="c1")) + "\n"
        return plan_text(plan='plan = "stp"', phases=body).replace("# body", "# stp — Store Plan")

    # ---- STORE.1: import writes versioned rows; the same bytes again write nothing ----
    f = write("stp.md", stp())
    original = open(f, "rb").read()
    out = st("import", f)
    assert out["ok"] and out["version"] == 1 and out["noop"] is False and out["items"] == 6, out
    ids = ["S1", "S1.1", "S1.1.1", "S1.2", "S2", "S2.1"]
    assert out["changes"] == {"added": ids, "changed": [], "removed": []}, out
    v1 = rows("stp", 1)
    assert [r[2] for r in v1] == ids and [r[12] for r in v1] == list(range(6)), v1
    assert [r[4] for r in v1] == ["phase", "task", "subtask", "task", "phase", "task"], v1
    assert v1[2][3] == "S1.1" and v1[1][3] == "S1" and v1[0][3] is None  # parent
    assert json.loads(v1[0][11])["description"] == "phase why" and json.loads(v1[0][11])["wave"] == 1
    assert v1[1][9] == 0 and v1[2][9] == 1 and v1[1][7] == "src/a.py" and v1[4][8] == "S1"  # parallel, owned, deps
    row = one("SELECT version, status, header_json FROM plans WHERE id='stp'")
    hdr = json.loads(row["header_json"])
    assert row["version"] == 1 and hdr["source"] == os.path.abspath(f) and hdr["sha"] == sha_of(f), hdr  # a6
    assert one("SELECT sha, approved_at FROM plan_versions WHERE plan='stp' AND version=1") == {
        "sha": sha_of(f), "approved_at": None}
    assert open(f, "rb").read() == original
    before = dump()
    out = st("import", f)  # a2: the same bytes: nothing is written
    assert out["ok"] and out["noop"] is True and out["version"] == 1 and out["changes"] == {
        "added": [], "changed": [], "removed": []}, out
    assert dump() == before

    # an edited file is a new version; the change report names added, changed and removed ids (a2, a5)
    s2 = stp(extra_task=task("S1.3", acceptance="d1"), s11_title="renamed task", with_s21=False)
    open(f, "w").write(s2)
    out = st("import", f)
    assert out["ok"] and out["version"] == 2 and out["noop"] is False, out
    assert out["changes"] == {"added": ["S1.3"], "changed": ["S1.1"], "removed": ["S2", "S2.1"]}, out
    assert rows("stp", 1) == v1, "an older version's rows changed"
    assert [r[2] for r in rows("stp", 2)] == ["S1", "S1.1", "S1.1.1", "S1.2", "S1.3"]
    assert one("SELECT version FROM plans WHERE id='stp'")["version"] == 2

    # a plan stored before versioned items: the same bytes are a no-op, an edit diffs against header_json
    legacy = write("legacy.md", plan_text(plan='plan = "legacy"', phases=phase("L1", tasks=task("L1.1")) + "\n"))
    lparsed = mk("parse", legacy)
    con = conn()
    con.execute("INSERT INTO plans(id, version, title, header_json, body_md, status) VALUES (?,?,?,?,?,?)",
                ("legacy", 1, "legacy", json.dumps({"sha": lparsed["sha"], "phases": lparsed["phases"],
                                                   "runs": []}), "# body", "draft"))
    con.commit()
    con.close()
    before = dump()
    out = st("import", legacy)
    assert out["noop"] is True and out["version"] == 1 and dump() == before, out
    open(legacy, "w").write(plan_text(plan='plan = "legacy"', phases=phase("L1", tasks=task("L1.1", title="new")) + "\n"))
    out = st("import", legacy)
    assert out["version"] == 2 and out["changes"] == {"added": [], "changed": ["L1.1"], "removed": []}, out

    # ---- STORE.1/2: approve marks the imported version, never rewrites the file or an older version ----
    v2 = rows("stp", 2)
    file_bytes = open(f, "rb").read()
    out = st("approve", f, "--run", "rA")
    assert out["ok"] and out["version"] == 2 and "noop" not in out, out  # a3: THAT version, no v3
    assert open(f, "rb").read() == file_bytes  # STORE.2 a2: the file is not rewritten
    assert one("SELECT approved_at FROM plan_versions WHERE plan='stp' AND version=2")["approved_at"]
    assert one("SELECT approved_at FROM plan_versions WHERE plan='stp' AND version=1")["approved_at"] is None
    assert rows("stp", 1) == v1 and rows("stp", 2) == v2
    assert os.path.basename(out["archived"]) == "stp-v2.md" and os.path.isfile(out["archived"])
    assert one("SELECT status, version FROM plans WHERE id='stp'") == {"status": "approved", "version": 2}
    assert hdr_source(one, "stp") == os.path.abspath(f)  # approve keeps the import source
    assert st("approve", f, "--run", "rA")["noop"] is True
    # an edited file approved without a prior import is a NEW version; versions 1 and 2 stay
    open(f, "w").write(stp(extra_task=task("S1.3", acceptance="d1"), s11_title="third title", with_s21=False))
    out = st("approve", f, "--run", "rA")
    assert out["ok"] and out["version"] == 3, out
    assert rows("stp", 1) == v1 and rows("stp", 2) == v2 and len(rows("stp", 3)) == 5
    assert one("SELECT approved_at FROM plan_versions WHERE plan='stp' AND version=3")["approved_at"]
    # a4: `mkdb plans <id>` reads the rows, not header_json (the current version's task titles)
    got = db.plans(shome, data_dir=sdata, plan_id="stp")
    assert [t["title"] for t in got["phases"][0]["tasks"]] == ["third title", "t", "t"], got
    assert got["version"] == 3 and got["phases"][0]["description"] == "phase why", got

    # ---- STORE.2: status and runs live in the store; the file's bytes stay ----
    sha_before = sha_of(f)
    out = st("set", f, "--status", "running")
    assert out["ok"] and out["status"] == "running" and out["store"] is True, out
    out = st("add-run", f, "--run", "rB")
    assert out["ok"] and out["runs"] == ["rB"] and out["added"] is True, out
    assert st("add-run", f, "--run", "rB")["added"] is False
    assert sha_of(f) == sha_before, "set/add-run rewrote the file"
    assert one("SELECT status FROM plans WHERE id='stp'")["status"] == "running"
    assert json.loads(one("SELECT header_json FROM plans WHERE id='stp'")["header_json"])["runs"] == ["rB"]
    parsed = st("parse", f)  # a run reads status and runs from the store through parse/check
    assert parsed["status"] == "running" and parsed["runs"] == ["rB"] and parsed["sha"] == sha_before, parsed
    assert st("check", f)["status"] == "running"
    os.makedirs(os.path.join(tmp, "exp-out"))
    exp = os.path.join(tmp, "exp-out", "stp.md")
    ex = st("export", exp)
    assert ex["ok"] and ex["version"] == 3 and open(exp, "rb").read() == open(ex["path"], "rb").read()  # a3
    assert b'status = "approved"' in open(ex["path"], "rb").read()
    # approve after status moved on keeps the store's run list (the file has none)
    assert "rB" in json.loads(one("SELECT header_json FROM plans WHERE id='stp'")["header_json"])["runs"]
    # RUN: set and add-run also take a plan id, which works on the store row only (no file read)
    out = st("set", "stp", "--status", "parked")
    assert out["ok"] and out["status"] == "parked" and out["plan"] == "stp" and out["store"] is True, out
    assert one("SELECT status FROM plans WHERE id='stp'")["status"] == "parked"
    assert sha_of(f) == sha_before, "set by id touched the file"
    out = st("add-run", "stp", "--run", "rC")
    assert out["ok"] and out["runs"] == ["rB", "rC"] and out["added"] is True, out
    assert st("add-run", "stp", "--run", "rC")["added"] is False
    assert st("parse", f)["status"] == "parked" and st("parse", f)["runs"] == ["rB", "rC"]
    st("set", "stp", "--status", "running")
    before_ids = dump()
    for args, needle in ((("set", "ghost-plan", "--status", "running"), "no plan ghost-plan"),
                         (("add-run", "ghost-plan", "--run", "rZ"), "no plan ghost-plan"),
                         (("set", "stp", "--status", "bogus"), "bad status")):
        bad = st(*args, code=2)
        assert bad["ok"] is False and any(needle in e for e in bad["errors"]), (args, bad)
    assert dump() == before_ids
    # regression guard (passes on the old code too): a file plan the store does not hold is read
    # as before, and a read writes nothing
    fo = write("fileonly.md", plan_text(plan='plan = "fileonly"', phases=phase("F1") + "\n"))
    fo_bytes, store_dump = open(fo, "rb").read(), dump()
    assert st("parse", fo)["status"] == "draft" and st("check", fo)["ok"]
    assert open(fo, "rb").read() == fo_bytes and dump() == store_dump
    st("set", fo, "--status", "approved")  # no store row: the file is rewritten as it always was
    assert b'status = "approved"' in open(fo, "rb").read() and dump() == store_dump

    # ---- STORE.3: item set edits one row of a draft plan, in the store only ----
    itp = write("itp.md", plan_text(plan='plan = "itp"', phases=(
        phase("I1", tasks=task("I1.1", acceptance="old acc") + task("I1.2")) + "\n"
        + phase("I2", wave=2, tasks=task("I2.1")) + "\n")))
    assert st("import", itp)["version"] == 1
    itp_bytes = open(itp, "rb").read()
    os.unlink(itp)  # the plan file is not read: the edit works with no file at all
    base = dump()
    out = st("item", "set", "itp", "I1.1", "--title", "better title", "--description", "why now",
             "--acceptance", "line one\nline two", "--owned", "a.py, b.py", "--deps", "I1.2")
    assert out["ok"] and out["id"] == "I1.1" and out["version"] == 1, out
    assert out["changed"] == {"title": "better title", "description": "why now", "acceptance": "line one\nline two",
                              "owned": "a.py,b.py", "deps": "I1.2"}, out
    after = dump()
    assert after["plans"] == base["plans"], "item set moved the plans row or its version"
    changed_rows = [(a, b) for a, b in zip(base["plan_items"], after["plan_items"]) if a != b]
    assert len(changed_rows) == 1 and changed_rows[0][0][2] == "I1.1", changed_rows  # every other row is unchanged
    ph = {t["id"]: t for t in db.plans(shome, data_dir=sdata, plan_id="itp")["phases"][0]["tasks"]}["I1.1"]
    assert ph["title"] == "better title" and ph["description"] == "why now", ph  # a3: plans <id> shows it
    assert ph["acceptance"] == "line one\nline two" and ph["owned"] == ["a.py", "b.py"] and ph["deps"] == ["I1.2"], ph
    assert st("item", "set", "itp", "I1.1", "--title", "better title")["changed"] == {}  # nothing differs
    # refusals: exit 2 and nothing written (a2)
    base = dump()
    for args, needle in (
            (("stp", "S1.1", "--title", "x"), "not draft"),                       # approved/running plan
            (("nope", "I1.1", "--title", "x"), "no plan nope"),                    # unknown plan
            (("itp", "ZZ", "--title", "x"), "no item ZZ"),                         # unknown id
            (("itp", "I1.1", "--deps", "ghost"), "missing dep: ghost"),            # a dep not in the plan
            (("itp", "I1.1", "--deps", "I1.1"), "self-dep"),                       # self dep
            (("itp", "I1.2", "--deps", "I1.1"), "cycle:"),                         # I1.1 -> I1.2 -> I1.1
            (("itp", "I1.1", "--title", " "), "title must not be empty"),
            (("itp", "I1.1"), "nothing to change")):
        bad = st("item", "set", *args, code=2)
        assert bad["ok"] is False and any(needle in e for e in bad["errors"]), (args, bad)
        assert dump() == base, args
    legacy_only = st("item", "set", "legacy", "L1.1", "--title", "x", "--description", "d")  # legacy v2 has rows
    assert legacy_only["ok"]
    # a plan whose rows were never written (stored before versioned items) is refused, not guessed at
    con = conn()
    con.execute("INSERT INTO plans(id, version, title, header_json, status) VALUES ('bare', 1, 'bare', '{}', 'draft')")
    con.commit()
    con.close()
    assert any("no stored items" in e for e in st("item", "set", "bare", "X", "--title", "x", code=2)["errors"])

    # ---- delete takes the plan's versioned rows with it ----
    out = st("archive", "itp")
    out = st("delete", "itp")
    assert out["ok"] and out["purged"].get("plan_items") == 5 and out["purged"].get("plan_versions") == 1, out
    assert rows("itp", 1) == [] and one("SELECT 1 AS x FROM plan_versions WHERE plan='itp'") is None
    assert len(rows("stp", 1)) == 6, "delete of one plan touched another plan's rows"

    # ---- hazard (STORE.2, RUN): `mk-state run init --plan <plan id>` copies a stored version and calls
    # set and add-run by id: the store (not the file) learns running and the run, the file is not read
    state = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mk-state.py")
    rp = write("rinit.md", plan_text(plan='plan = "rinit"', status='status = "draft"',
                                     phases=phase("R1", tasks=task("R1.1")) + "\n"))
    st("import", rp)
    st("approve", rp)
    rp_bytes = open(rp, "rb").read()
    r = subprocess.run([sys.executable, state, "--data", sdata, "run", "init", "--id", "rx",
                        "--feature", "F", "--branch", "b", "--plan", "rinit"],
                       capture_output=True, text=True, env=dict(os.environ, MATRIXKODER_DATA=sdata))
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    run_out = json.loads(r.stdout.strip().splitlines()[-1])
    assert run_out["ok"] and run_out["run"]["plan_status"] == "running", run_out
    assert open(rp, "rb").read() == rp_bytes  # the file was not rewritten by set / add-run
    assert run_out["run"]["plan_version"] == 1 and run_out["run"].get("plan_sha") is None
    assert one("SELECT status FROM plans WHERE id='rinit'")["status"] == "running"
    assert json.loads(one("SELECT header_json FROM plans WHERE id='rinit'")["header_json"])["runs"] == ["rx"]
    return itp_bytes


def hdr_source(one, plan):
    return json.loads(one("SELECT header_json FROM plans WHERE id=?", plan)["header_json"]).get("source")


def self_test():
    import shutil, sqlite3, subprocess
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

    def f2_warnings(warnings):
        """F.2: the two advisory warnings a pre-F plan now carries (no plan-level final gate, an
        item with neither gate nor manual). The assertions below compare against the warnings
        that are NOT these, so an old plan still has to be otherwise quiet."""
        # TPL.2 added advisories (missing description, Summary/Goal, no acceptance, a line no gate
        # proves, a missing required key on an old plan) are filtered the same way: an old plan
        # must still be otherwise quiet
        return [w for w in warnings if "no final_gate in the plan" not in w
                and not w.endswith(": neither gate nor manual line")
                and not TPL_ADVICE_RE.search(w)]

    try:
        # P1.1 parse — the two real files (read-only) must give the C2 JSON
        out = mk("parse", template)
        with open(template, "rb") as f:
            assert out["ok"] and out["sha"] == hashlib.sha256(f.read()).hexdigest()
        assert out["plan"] == "feature-id" and out["status"] == "draft" and out["runs"] == []
        assert set(out["gates"]) == {"test", "typecheck", "selftest", "validate", "c1"}
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

        # P1.2 check — both real files are clean (ok:true, exit 0): neither carries a real
        # problem. F.2 adds the two advisories a plan written before F earns (no final gate, an
        # item with neither gate nor manual), and the shipped template carries the F.2 fields, so
        # only the fixture plan-link.md — which is NOT part of this change and stays as it is —
        # is allowed to carry them.
        out = mk("check", template)
        assert out["ok"] and out["warnings"] == [] and out["sha"], out.get("warnings")
        out = mk("check", real_plan)
        assert out["ok"] and out["sha"] and f2_warnings(out["warnings"]) == [], out["plan"]
        assert any("no final_gate in the plan" in w for w in out["warnings"])  # the F.2 advisory

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
        with open(mkpaths.legacy_yaml(dsub), "w") as f:
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
        assert f2_warnings(mk("check", p)["warnings"]) == []

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
        assert out["ok"] is True and f2_warnings(out["warnings"]) == [], out  # no optional keys: still quiet

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

        # ---------- TPL.2: per-section result, fit class, --classify, date strictness ----------
        sections_md = "".join(f"## {n}\ntext\n\n" for n in BODY_SECTIONS)
        fin = '[[final_gate]]\nid = "F1"\ncmd = "true"\ncovers = ["P1.1"]\n'
        ph_head = ('[[phase]]\nid = "P1"\ntitle = "phase P1"\ndescription = "d"\nwave = 1\n'
                   'owned = []\n{commit}subagents = false\n')
        ph_task = ('  [[phase.task]]\n  id = "P1.1"\n  title = "t"\n  acceptance = ["a"]\n'
                   '  gate = { cmd = "true", expect = 0, timeout = 60, lines = ["P1.1.a1"] }\n')

        def tpl_plan(name, created="2026-10-08", head=ph_head.format(commit='commit = "c"\n'),
                     task_md=ph_task, final=fin, body=None, status='status = "draft"'):
            cr = f"created = {created}\n" if created else ""
            return write(name, f'+++\nplan = "t1"\n{status}\n{cr}runs = []\ndescription = "d"\n{final}\n'
                               f'{head}{task_md}+++\n\n# t1 — title\n\n'
                               f'{sections_md if body is None else body}')

        # template class: all seven sections and four parts found, no warning (TPL.2.a1)
        out = mk("check", tpl_plan("tpl-full.md"))
        assert out["ok"] and out["fit"] == "template" and out["missing"] == [], out
        assert out["sections_found"] == 7 and out["sections_total"] == 7 and out["warnings"] == [], out
        assert set(out["sections"]) == set(BODY_SECTIONS) and set(out["sections"].values()) == {"found"}
        assert set(out["parts"]) == set(HEADER_PARTS) and set(out["parts"].values()) == {"found"}
        # the shipped template and the repo's real plan fixture class by the same rule
        assert mk("check", template)["fit"] == "template"
        assert mk("check", real_plan)["fit"] in ("template", "partial")

        # partial class: a valid header with no task lists tasks and acceptance (TPL.2.a2)
        out = mk("check", tpl_plan("tpl-notask.md", task_md="", final=""))
        assert out["ok"] and out["fit"] == "partial", out
        assert out["missing"] == ["tasks", "acceptance", "final gate"], out
        assert out["parts"]["header phases"] == "found" and out["parts"]["tasks"] == "missing"
        assert out["sections_found"] == 7
        # a missing body section is named and counted (TPL.2.a9)
        out = mk("check", tpl_plan("tpl-norisks.md", body=sections_md.replace("## Risks\ntext\n\n", "")
                                   + "### Risks\nonly a ### heading\n"))
        assert out["fit"] == "partial" and out["missing"] == ["Risks"], out
        assert out["sections_found"] == 6 and out["sections"]["Risks"] == "missing", out
        # "header phases" means [[phase]] blocks, never the Phases body section
        out = mk("check", write("tpl-nophase.md", '+++\nplan = "t1"\nstatus = "draft"\n'
                                                 'created = 2026-10-08\nruns = []\n+++\n\n# t1\n\n'
                                + sections_md))
        assert out["fit"] == "not_a_plan" and out["sections"]["Phases"] == "found", out
        assert out["parts"]["header phases"] == "missing" and out["sections_found"] == 7, out
        # a section heading inside a comment or a fence is not a section
        out = mk("check", tpl_plan("tpl-hidden.md", body=sections_md.replace("## Log\ntext\n\n", "")
                                   + "<!-- ## Log -->\n```\n## Log\n```\n"))
        assert out["missing"] == ["Log"], out

        # not a plan: no +++ first line. --classify returns a class with exit 0; without it the
        # old error and exit code stay (TPL.2.a3)
        np = write("not-a-plan.md", "# just notes\n\n## Summary\nhello\n")
        out = mk("check", np, "--classify")
        assert out["ok"] is True and out["fit"] == "not_a_plan" and out["sections_found"] == 1, out
        assert out["sections_total"] == 7 and "Goal" in out["missing"] and "tasks" in out["missing"], out
        out = mk("check", np, code=2)
        assert out["ok"] is False and out["errors"] == ["line 1: first line must be '+++'"], out
        assert "fit" not in out
        mk("check", os.path.join(tmp, "no-such-plan.md"), "--classify", code=3)  # a missing file stays an error

        # date strictness: the same phase without commit is an error for a new plan and a warning
        # for an older or undated one (TPL.2.a4, a5)
        nocommit = ph_head.format(commit="")
        out = mk("check", tpl_plan("tpl-new.md", created="2026-10-20", head=nocommit), code=2)
        assert out["errors"] == ["P1: no commit key (required in a plan created on or after 2026-10-20)"], out
        assert "fit" not in out  # an error reply carries the class only under --classify
        out = mk("check", tpl_plan("tpl-new2.md", created="2026-10-20", head=nocommit), "--classify", code=2)
        assert out["fit"] == "template" and out["missing"] == [], out  # the class rides on the error reply
        for name, created in (("tpl-old.md", "2026-10-19"), ("tpl-undated.md", "")):
            out = mk("check", tpl_plan(name, created=created, head=nocommit))
            assert out["ok"] and any("P1: no commit key" in w for w in out["warnings"]), out
        out = mk("check", tpl_plan("tpl-notitle.md", created="2026-10-21",
                                   task_md=ph_task.replace('  title = "t"\n', "")), code=2)
        assert any("P1.1: no title key" in e for e in out["errors"]), out
        out = mk("check", tpl_plan("tpl-notitle-old.md", task_md=ph_task.replace('  title = "t"\n', "")))
        assert out["ok"] and any("P1.1: no title key" in w for w in out["warnings"]), out
        # a task with no acceptance / a line no gate proves is a warning for a new plan too
        out = mk("check", tpl_plan("tpl-noacc.md", created="2026-10-21",
                                   task_md=ph_task.replace('  acceptance = ["a"]\n', "")
                                   .replace(', lines = ["P1.1.a1"]', "")))
        assert out["ok"] and "P1.1: no acceptance" in out["warnings"], out
        out = mk("check", tpl_plan("tpl-loose.md", created="2026-10-21",
                                   task_md=ph_task.replace('acceptance = ["a"]', 'acceptance = ["a", "b"]')))
        assert out["ok"] and "P1.1: acceptance line(s) P1.1.a2 proved by no gate" in out["warnings"], out
        # a missing description is only ever a warning, at the top and phase level
        out = mk("check", write("tpl-nodesc.md", open(os.path.join(tmp, "tpl-full.md")).read()
                                .replace('description = "d"\n', "")), )
        assert out["ok"] and "no description key in the header; advised: two to five plain sentences " \
            "(plans.header_json only)" in out["warnings"], out
        assert "P1: no description; advised: two to five plain sentences" in out["warnings"], out
        # description at the top, phase and task level: accepted, passed through, no warning (TPL.1.a4).
        # A regression guard: the old check never warned on an unknown key, so this passes on the old code too
        dp = tpl_plan("tpl-desc.md", created="2026-10-21",
                      task_md=ph_task + '  description = "what the task is"\n')
        out = mk("check", dp)
        assert out["ok"] and not [w for w in out["warnings"] if "description" in w], out
        parsed = mk("parse", dp)
        assert parsed["description"] == "d" and parsed["phases"][0]["description"] == "d"
        assert parsed["phases"][0]["tasks"][0]["description"] == "what the task is"

        # Summary drift warns, never errors (TPL.2.a7); matching lines are quiet
        summ = ("## Summary\n- Phases: 1 — P1\n- Method: single lane · subagents off\n"
                "- Lanes: P1 is its own lane\n- Delivery: main · pushed: no\n- Gates: F1 = true\n\n")
        quiet = sections_md.replace("## Summary\ntext\n\n", summ)
        out = mk("check", tpl_plan("tpl-sum-ok.md", created="2026-10-21", body=quiet))
        assert out["ok"] and out["warnings"] == [], out
        drift = quiet.replace("Phases: 1", "Phases: 3").replace("single lane · subagents off", "waves · subagents on") \
            .replace("F1 = true", "G9 = other")
        out = mk("check", tpl_plan("tpl-sum-drift.md", created="2026-10-21", body=drift))
        assert out["ok"] is True, out
        assert "Summary says 3 phases; the header has 1" in out["warnings"], out
        assert any(w.startswith("Summary Method says waves · subagents on") for w in out["warnings"]), out
        assert "Summary Gates does not list final gate F1 as id = cmd" in out["warnings"], out
        out = mk("check", tpl_plan("tpl-sum-lane.md", created="2026-10-21", body=quiet.replace(
            "P1 is its own lane", "each phase alone"), head=ph_head.format(commit='commit = "c"\n')
            + 'lane_group = "A"\n'))
        assert "Summary Lanes does not name lane_group A" in out["warnings"], out
        # a missing Summary or Goal is a warning, not an error
        out = mk("check", tpl_plan("tpl-nosum.md", created="2026-10-21", body=sections_md.replace(
            "## Summary\ntext\n\n", "").replace("## Goal\ntext\n\n", "")))
        assert out["ok"] and "body has no '## Summary' section (see templates/plan.md)" in out["warnings"], out
        assert "body has no '## Goal' section (see templates/plan.md)" in out["warnings"], out

        # approve warns "N unticked open questions" and still approves; check never errors on them (a8)
        oq = sections_md.replace("## Open questions\ntext\n", "## Open questions\n- [ ] one — decides a\n"
                                 "- [x] two — decided\n- [ ] three — decides b\n")
        p = tpl_plan("tpl-oq.md", body=oq)
        assert mk("check", p)["ok"] is True and not any("unticked" in w for w in mk("check", p)["warnings"])
        tdata, thome = os.path.join(tmp, "tpl-data"), os.path.join(tmp, "tpl-home")
        out = mk("approve", p, "--data", tdata, "--home", thome)
        assert out["ok"] and out["status"] == "approved" and "2 unticked open questions" in out["warnings"], out
        p = tpl_plan("tpl-oq0.md", body=oq.replace("- [ ] ", "- [x] "))
        out = mk("approve", p, "--data", tdata, "--home", thome)
        assert out["ok"] and not any("unticked" in w for w in out["warnings"]), out

        # regression guard (TPL.2.a6, passes on the old code too): the old errors and exit code are
        # unchanged by the new fields
        out = mk("check", write("tpl-guard.md", plan_text(phases=phase("P1", route="opencode") + "\n")), code=2)
        assert out["ok"] is False and any("bad route" in e for e in out["errors"]), out
        assert "fit" not in out and "sections" not in out

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
        # F.2: the template documents the new contract and carries a working example — one task
        # with a gate, one with a manual, critical/risk, an acceptance list and a final gate.
        for needle in ("[[final_gate]]", "covers", "gate = { cmd", "manual = \"<why",
                       "critical = true", "risk = 3"):
            assert needle in tmpl, needle
        p0 = out["phases"][0]
        assert p0["tasks"][0]["gate"] and p0["tasks"][0]["acceptance_lines"] \
            and len(p0["tasks"][0]["acceptance_lines"]) == 2, p0["tasks"][0]
        assert any(t["manual"] for t in p0["tasks"]), p0["tasks"]
        assert out["final_gates"] and out["final_gates"][0]["covers"], out["final_gates"]

        # F0-BLOCK-BEGIN — F.0: a table gate is runnable, a plain string gate stays manual
        p = write("gate-table.md", plan_text(head_extra=(
            '\n[gates.slow]\ncmd = "sleep 5"\ntimeout = 1\n'
            '\n[gates.strict]\ncmd = "true"\nexpect = 3\n')))
        out = mk("parse", p)
        assert out["gates"]["test"] == "true", out  # a plain string gate stays a string (manual)
        assert out["gates"]["slow"] == {"cmd": "sleep 5", "expect": 0, "timeout": 1}, out
        assert out["gates"]["strict"] == {"cmd": "true", "expect": 3, "timeout": 600}, out
        assert mk("check", p)["ok"] is True, "a well-formed table gate must check clean"

        # check rejects a table gate without cmd, and a non-int expect / timeout
        for name, body, why in (("gate-nocmd.md", '[gates.x]\nexpect = 1\n', "needs cmd"),
                                ("gate-badexpect.md", '[gates.x]\ncmd = "true"\nexpect = "zero"\n',
                                 "expect must be an int"),
                                ("gate-badtimeout.md", '[gates.x]\ncmd = "true"\ntimeout = "fast"\n',
                                 "timeout must be an int")):
            p = write(name, plan_text(head_extra="\n" + body))
            e = mk("check", p, code=2)
            assert e["ok"] is False, e
            assert any("gate x" in x and why in x for x in e["errors"]), (name, e)
        # F0-BLOCK-END

        # F.2 — the plan contract: acceptance lines, per-task gate OR manual, critical/risk,
        # header [[final_gate]] with covers, and the frozen-gate rules in approve.
        f2_check_self_test(tmp, mk, write, plan_text, phase, task)

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
        assert not os.path.exists(mkpaths.db_in(empty_data))
        assert not os.path.exists(mkpaths.db_in(empty_home))
        assert mk("parse", p)["status"] == "approved"
        assert mk("approve", p, "--data", empty_data, "--home", empty_home).get("noop") is True
        # a failing check refuses the write and still creates no store
        badp = write("nostore-bad.md", plan_text(phases=phase("P1", route="opencode") + "\n"))
        before = open(badp, "rb").read()
        refused = mk("approve", badp, "--data", empty_data, "--home", empty_home, code=2)
        assert any("bad route" in e for e in refused["errors"])
        assert open(badp, "rb").read() == before
        assert not os.path.exists(mkpaths.db_in(empty_data))

        # with a store: import, archive, export; a failed approve leaves file and data
        data = os.path.join(tmp, "store")
        home = os.path.join(data, ".mk-home")
        os.makedirs(data)
        os.makedirs(home)
        db = _mkdb()
        db.open_project(home, data_dir=mkpaths.project_folder(data, home)).close()
        good = write("sample.md", plan_text(
            plan='plan = "sample"',
            phases=phase("P1", tasks=task("P1.1", extra=(
                '  [[phase.task.subtask]]\n  id = "P1.1.1"\n  title = "leaf"\n'
                '  acceptance = "leaf done"\n  deps = "P1.1"\n  parallel = true\n  route = ""\n  owned = []\n'
            ))) + "\n"))
        good_before = open(good, "rb").read()
        out = mk("approve", good, "--data", data, "--home", home, "--run", "r1")
        assert out["ok"] and out["store"] is True and out["version"] == 1, out
        # C13 + STORE.2: approve copies; the original file stays, byte for byte (the store row and
        # the archive carry the approved status, the file's status line is not rewritten)
        assert os.path.isfile(good), "approve must leave the original in place"
        assert open(good, "rb").read() == good_before
        assert b'status = "approved"' in open(out["archived"], "rb").read()
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
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
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
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        n_before = con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
        con.close()
        bad = mk("approve", cyc, "--data", data, "--home", home, "--run", "r1", code=2)
        # check and approve share the graph, so the refusal is check's "dep cycle:" (ids named)
        assert any("cycle:" in e and "P1.1" in e and "P1.1.1" in e for e in bad["errors"]), bad
        assert open(cyc, "rb").read() == blob
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        assert con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == n_before
        assert con.execute("SELECT id FROM plans WHERE id='cyc'").fetchone() is None
        con.close()

        # <run>:<id> is validated: present passes, missing is named and changes nothing
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
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
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        deps = {r["id"]: r["deps"] for r in con.execute("SELECT id, deps FROM tasks WHERE run='rf'")}
        con.close()
        assert deps["PA"] == "PC" and deps["PA.1"] == "PA.2" and deps["PA.2"] == "", deps

        # a cycle across two later items is refused and names both ids; file and store stay
        cyc2 = write("cyc2.md", plan_text(plan='plan = "cyc2"', phases=(
            phase("PA", wave=1, deps='"PC"') + "\n" + phase("PC", wave=1, deps='"PA"') + "\n")))
        blob = open(cyc2, "rb").read()
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        n_before = con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
        con.close()
        bad = mk("approve", cyc2, "--data", data, "--home", home, "--run", "rc", code=2)
        assert any("cycle:" in e and "PA" in e and "PC" in e for e in bad["errors"]), bad
        assert open(cyc2, "rb").read() == blob
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
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
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
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
        assert open(bom, "rb").read() == raw_bom  # STORE.2: the file is not rewritten, the archive holds the approved bytes
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
                src_before = open(src, "rb").read()
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
                # C13 + STORE.2: a kill leaves the original in place and the converging approve
                # does not rewrite it; the archive holds the approved bytes
                assert os.path.isfile(src), step
                assert open(src, "rb").read() == src_before
                assert os.path.isfile(got["archived"])
                assert b'status = "approved"' in open(got["archived"], "rb").read()


        # the run's plan path still resolves through mk-state after the file is archived
        state = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mk-state.py")
        link = os.path.join(tmp, "link.md")
        open(link, "w").write(plan_text(plan='plan = "link"', phases=phase("P1") + "\n"))
        r = subprocess.run([sys.executable, state, "--data", data, "run", "init", "--force",
                            "--id", "lk", "--feature", "L", "--branch", "bl", "--plan", link],
                           capture_output=True, text=True,  # an old-shape run: it starts from the plan FILE
                           env=dict(os.environ, MK_LEGACY_RUN_INIT="1"))
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        ap = mk("approve", link, "--data", data, "--home", home, "--run", "lk")
        # the original path still resolves after the approve (the hermes-switchui issue): C13
        # approve copies, so the file mk-state was pointed at is still there
        assert ap["ok"] and os.path.isfile(link), ap
        # the run is live (P1 is planned, not terminal): T1's rule refuses a bare sync,
        # so this mechanics test passes --from-file ("the plan file wins")
        r = subprocess.run([sys.executable, state, "--data", data, "plan", "sync", "--run", "lk",
                            "--from-file"], capture_output=True, text=True)
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
        # the stored version is 1 for a fresh id (STORE.1: an import is a version) and the reply reports it.
        imp1 = write("imp1.md", plan_text(plan='plan = "imp1"', status='status = "draft"',
                                          phases=phase("P1", tasks=task("P1.1")) + "\n").replace(
            "# body", "# imp1 — Sample Title"))
        blob1 = open(imp1, "rb").read()
        out = mk("import", imp1, "--data", data, "--home", home)
        assert out["ok"] is True and out["store"] is True and out["plan"] == "imp1", out
        assert out["title"] == "Sample Title", out  # the H1 text after the em dash
        n, row1 = plans_rows("imp1")
        assert n == 1, n
        assert row1["version"] == 1 and out["version"] == row1["version"], (row1, out)
        assert row1["title"] == "Sample Title"
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
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

        # L10.6: inbox plans move into the project. The home is the one the import above used.
        def inbox_plan(pid, header_id=None, docs=("notes.md", "mock/a.png")):
            d = os.path.join(mkpaths.inbox_dir(home), pid)
            for rel in docs:
                os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
                with open(os.path.join(d, rel), "w") as f:
                    f.write(f"doc {rel}\n")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "plan.md"), "w") as f:
                f.write(plan_text(plan=f'plan = "{header_id or pid}"',
                                  phases=phase("P1", tasks=task("P1.1")) + "\n"))
            return d
        ib = inbox_plan("ibx1")
        plan_md_bytes = open(os.path.join(ib, "plan.md"), "rb").read()
        tgt_plan, tgt_assets = mkpaths.plan_file(data, "ibx1"), os.path.join(mkpaths.plan_dir(data, "ibx1"), "assets")
        # dry-run: lists every move, changes nothing
        out = mk("import", ib, "--dry-run", "--data", data, "--home", home)
        assert out["ok"] and len(out["moved"]) == 3 and out["moved"][0]["to"] == tgt_plan, out
        assert os.path.isdir(ib) and not os.path.exists(tgt_plan) and plans_rows("ibx1")[0] == 0
        # a target plan.md with other bytes: refused, nothing moved, no row
        os.makedirs(os.path.dirname(tgt_plan), exist_ok=True)
        with open(tgt_plan, "w") as f:
            f.write("someone else's plan\n")
        out = mk("import", ib, "--data", data, "--home", home)
        assert out["ok"] is False and out["reason"] == "target exists", out
        assert os.path.isfile(os.path.join(ib, "notes.md")) and plans_rows("ibx1")[0] == 0
        os.unlink(tgt_plan)
        # a taken asset name refuses too
        os.makedirs(tgt_assets, exist_ok=True)
        with open(os.path.join(tgt_assets, "notes.md"), "w") as f:
            f.write("other\n")
        out = mk("import", os.path.join(ib, "plan.md"), "--data", data, "--home", home)
        assert out["ok"] is False and out["reason"] == "target exists", out
        assert not os.path.exists(tgt_plan)
        os.unlink(os.path.join(tgt_assets, "notes.md"))
        # the real move: row recorded, files at targets, source bytes kept, inbox folder gone
        out = mk("import", ib, "--data", data, "--home", home)
        assert out["ok"] and len(out["moved"]) == 3 and plans_rows("ibx1")[0] == 1, out
        assert open(tgt_plan, "rb").read() == plan_md_bytes
        assert open(os.path.join(tgt_assets, "notes.md")).read() == "doc notes.md\n"
        assert open(os.path.join(tgt_assets, "mock", "a.png")).read() == "doc mock/a.png\n"
        assert not os.path.exists(ib), "the emptied inbox folder was not removed"
        # header id differs from the folder name: refused, nothing moved
        bad = inbox_plan("ibx2", header_id="other-id")
        out = mk("import", bad, "--data", data, "--home", home)
        assert out["ok"] is False and "plan id" in out["reason"], out
        assert os.path.isfile(os.path.join(bad, "notes.md")) and plans_rows("other-id")[0] == 0
        # outside the inbox: unchanged behaviour, moved is empty, the file stays
        out = mk("import", imp1, "--data", data, "--home", home)
        assert out["ok"] and out["moved"] == [] and os.path.isfile(imp1), out

        # 6. delete takes an ARCHIVED plan only; an unmigrated plan has no folder, so archive just
        # flips the status. Backup first, then the rows go; an unarchived plan is refused.
        plans_dir = os.path.join(data, "plans")
        os.makedirs(plans_dir, exist_ok=True)
        live = os.path.join(plans_dir, "imp6.md")
        open(live, "w").write(plan_text(plan='plan = "imp6"', phases=phase("P1") + "\n"))
        assert mk("import", live, "--data", data, "--home", home)["ok"]
        out = mk("delete", "imp6", "--data", data, "--home", home, code=2)
        assert "archive it first" in " ".join(out["errors"]) and plans_rows("imp6")[0] == 1, out
        out = mk("archive", "imp6", "--data", data, "--home", home)
        assert out["ok"] and len(out["moved"]) == 1 and not os.path.exists(live), out  # legacy plan file moved
        out = mk("delete", "imp6", "--data", data, "--home", home)
        assert out["ok"] and out["plan"] == "imp6" and out["purged"]["plans"] == 1, out
        assert os.path.isfile(out["backup"]) and "imp6-pre-delete-" in out["backup"], out
        assert os.path.isfile(mkpaths.archive_dir(data, "imp6", home) + "/plans/imp6.md")  # never unlinked
        assert plans_rows("imp6")[0] == 0

        # 7. live run refused (archive and delete); a held lane slot refuses delete; MK_ARCHIVE_FAIL
        # leaves every row; a full purge leaves no row naming the plan and another plan's rows stay.
        def project_con():
            return db.open_project(home, data_dir=mkpaths.project_folder(data, home))

        def named(con, plan_id, run_ids):
            n = con.execute("SELECT COUNT(*) FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
            n += con.execute("SELECT COUNT(*) FROM runs WHERE plan=?", (plan_id,)).fetchone()[0]
            for t in PURGE_BY_RUN:
                n += con.execute(f"SELECT COUNT(*) FROM {t} WHERE run IN ({','.join('?' * len(run_ids))})",
                                 run_ids).fetchone()[0]
            return n

        for pid in ("imp7", "imp8"):
            f = os.path.join(plans_dir, pid + ".md")
            open(f, "w").write(plan_text(plan=f'plan = "{pid}"', phases=phase("P1") + "\n"))
            assert mk("import", f, "--data", data, "--home", home)["ok"]
        con = project_con()
        for pid, run in (("imp7", "imp7run"), ("imp8", "imp8run")):
            con.execute("INSERT OR REPLACE INTO runs(id, plan, plan_version, branch, status, stage)"
                        " VALUES (?,?,0,'br','active','build')", (run, pid))
        con.commit()
        con.close()
        out = mk("archive", "imp7", "--data", data, "--home", home, code=2)
        said = " ".join(out["errors"])
        assert "imp7run" in said and "live run" in said and plans_rows("imp7")[0] == 1, out
        con = project_con()
        con.execute("UPDATE runs SET status='done'")
        con.commit()
        for run in ("imp7run", "imp8run", "imp7", "imp7-b"):  # imp7: import-time rows keyed by the plan id
            db.add_task(con, run, "P1", "phase", title="kept")
        for pid, run in (("imp7", "imp7run"), ("imp8", "imp8run")):
            con.execute("INSERT INTO events(run, task, t, chan, kind, text) VALUES (?,'P1','t','c','k','x')", (run,))
            con.execute("INSERT INTO artifacts(run, task, kind, text) VALUES (?,'P1','k','x')", (run,))
            con.execute("INSERT INTO acks(run, msg, lane) VALUES (?,'m','l')", (run,))
            con.execute("INSERT INTO waits(run, question) VALUES (?,'q')", (run,))
            con.execute("INSERT INTO event_src(run, src) VALUES (?,'s')", (run,))
        con.execute("INSERT INTO lane_slots(slot, run, state) VALUES ('s1','imp7run','busy')")
        con.commit()
        con.close()
        assert mk("archive", "imp7", "--data", data, "--home", home)["ok"]
        assert mk("archive", "imp8", "--data", data, "--home", home)["ok"]
        out = mk("delete", "imp7", "--data", data, "--home", home, code=2)
        assert "lane slot" in " ".join(out["errors"]) and plans_rows("imp7")[0] == 1, out
        con = project_con()
        con.execute("DELETE FROM lane_slots")
        con.commit()
        con.close()
        os.environ["MK_DELETE_BACKUP_FAIL"] = "1"
        try:
            out = mk("delete", "imp7", "--data", data, "--home", home, code=2)
        finally:
            os.environ.pop("MK_DELETE_BACKUP_FAIL", None)
        assert "backup failed" in " ".join(out["errors"]), out
        con = project_con()
        assert named(con, "imp7", ["imp7run"]) == 8, named(con, "imp7", ["imp7run"])
        con.close()
        arch = mkpaths.archive_dir(data, None, home)
        assert not [n for n in os.listdir(arch) if n.endswith(".tmp")], os.listdir(arch)
        # a child row written between the backup and the purge aborts delete; nothing is deleted
        os.environ["MK_DELETE_RACE_SQL"] = ("INSERT INTO events(run, task, t, chan, kind, text)"
                                            " VALUES ('imp7run','P1','t','c','k','late')")
        try:
            out = mk("delete", "imp7", "--data", data, "--home", home, code=2)
        finally:
            os.environ.pop("MK_DELETE_RACE_SQL", None)
        said = " ".join(out["errors"])
        assert "events went from 1 to 2" in said and "backup kept at" in said, out
        con = project_con()
        assert named(con, "imp7", ["imp7run"]) == 9, "the aborted delete changed rows"
        con.execute("DELETE FROM events WHERE text='late'")
        con.commit()
        con.close()
        # a failure mid-purge rolls the whole transaction back
        os.environ["MK_PURGE_FAIL"] = "1"
        try:
            r = subprocess.run([sys.executable, os.path.abspath(__file__), "delete", "imp7",
                                "--data", data, "--home", home], capture_output=True, text=True)
        finally:
            os.environ.pop("MK_PURGE_FAIL", None)
        assert r.returncode != 0 and "injected purge failure" in r.stderr, (r.stdout, r.stderr)
        con = project_con()
        assert named(con, "imp7", ["imp7run"]) == 8, "a mid-purge failure left a partial delete"
        con.close()
        out = mk("delete", "imp7", "--data", data, "--home", home)
        assert out["purged"]["event_src"] == 1, out
        assert out["ok"] and out["purged"]["tasks"] == 2 and out["purged"]["runs"] == 1, out
        con = project_con()
        assert named(con, "imp7", ["imp7run"]) == 0
        assert named(con, "imp8", ["imp8run"]) == 8, "another plan's rows were touched"
        left = con.execute("SELECT run FROM tasks WHERE run IN ('imp7','imp7-b','imp7run','imp8run') ORDER BY run").fetchall()
        assert [r[0] for r in left] == ["imp7-b", "imp8run"], [r[0] for r in left]  # plan-id rows gone, prefix plan kept
        con.close()
        chk = sqlite3.connect("file:" + urllib.parse.quote(out["backup"]) + "?mode=ro", uri=True)
        assert chk.execute("SELECT COUNT(*) FROM runs WHERE plan='imp7'").fetchone()[0] == 1
        chk.close()

        # 8. asset set: archive moves the C2 folder, plans/<id>.md, plans/archive/<id>-v<n>.md and the
        # run folders (layout kept) and nothing of an id-prefix plan; unarchive restores them
        # byte-identical; a completed archive is refused; a taken archive name gets a stamp.
        def put(path, text):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").write(text)

        def snap(paths):
            out = {}
            for q in paths:
                for dp, _, fs in os.walk(q) if os.path.isdir(q) else [(os.path.dirname(q), 0, [os.path.basename(q)])]:
                    for f in fs:
                        out[os.path.join(dp, f)] = open(os.path.join(dp, f), "rb").read()
            return out

        def seed(pid, status=None):
            f = os.path.join(plans_dir, pid + ".md")
            put(f, plan_text(plan=f'plan = "{pid}"', phases=phase("P1") + "\n"))
            assert mk("import", f, "--data", data, "--home", home)["ok"]
            con = project_con()
            con.execute("UPDATE plans SET status=? WHERE id=?", (status, pid))
            con.execute("INSERT INTO runs(id, plan, plan_version, branch, status, stage) VALUES (?,?,0,'b','done','x')",
                        (pid + "run", pid))
            con.commit()
            con.close()

        seed("imp9", "parked")
        seed("imp9-extra", "done")  # shares the id prefix: must stay untouched
        pdir = mkpaths.plan_dir(data, "imp9")
        put(os.path.join(pdir, "assets", "a.txt"), "asset\n")
        put(os.path.join(pdir, "plan.md"), "x\n")
        v1, v2 = (os.path.join(plans_dir, "archive", f"imp9-v{n}.md") for n in (1, 2))
        put(v1, "v1\n"), put(v2, "v2\n")
        put(os.path.join(plans_dir, "archive", "imp9-extra-v1.md"), "decoy\n")
        put(os.path.join(data, mkpaths.RUNS_LEGACY_NAME, "imp9run", "r.txt"), "legacy run\n")
        put(os.path.join(data, "runs", "imp9run", "y.txt"), "run\n")
        con = project_con()
        con.execute("UPDATE plans SET archived_path=? WHERE id='imp9'", (v2,))
        con.commit()
        con.close()
        decoys = [os.path.join(plans_dir, "imp9-extra.md"), os.path.join(plans_dir, "archive", "imp9-extra-v1.md")]
        assets = [pdir, os.path.join(plans_dir, "imp9.md"), v1, v2,
                  os.path.join(data, mkpaths.RUNS_LEGACY_NAME, "imp9run"), os.path.join(data, "runs", "imp9run")]
        before_assets, before_decoys = snap(assets), snap(decoys)
        out = mk("archive", "imp9", "--data", data, "--home", home)
        base = mkpaths.archive_dir(data, "imp9", home)
        assert len(out["moved"]) == 6 and all(m.startswith(base + os.sep) for m in out["moved"]), out
        assert not any(os.path.exists(q) for q in assets), "an asset stayed behind"
        assert snap(decoys) == before_decoys, "an id-prefix plan's files were touched"
        assert os.path.isfile(os.path.join(base, "folder", "assets", "a.txt"))
        assert os.path.isfile(os.path.join(base, "plans", "archive", "imp9-v2.md"))
        assert os.path.isfile(os.path.join(base, mkpaths.RUNS_LEGACY_NAME, "imp9run", "r.txt"))
        con = project_con()
        r9 = con.execute("SELECT status, header_json, archived_path FROM plans WHERE id='imp9'").fetchone()
        con.close()
        h9 = json.loads(r9["header_json"])
        assert r9["status"] == "archived" and h9["archived_from"] == "parked" and h9["archived_to"] == base
        assert len(h9["archived_files"]) == 6 and r9["archived_path"] == os.path.join(base, "plans", "archive", "imp9-v2.md")
        out = mk("archive", "imp9", "--data", data, "--home", home, code=2)  # completed: refused
        assert "already archived at" in " ".join(out["errors"]), out
        put(os.path.join(plans_dir, "imp9.md"), "squatter\n")
        out = mk("unarchive", "imp9", "--data", data, "--home", home, code=2)  # original occupied
        assert "already exists" in " ".join(out["errors"]) and "imp9.md" in " ".join(out["errors"]), out
        os.remove(os.path.join(plans_dir, "imp9.md"))
        os.rename(os.path.join(base, "plans", "archive", "imp9-v1.md"), os.path.join(base, "away"))
        out = mk("unarchive", "imp9", "--data", data, "--home", home, code=2)  # archived item gone
        assert "missing" in " ".join(out["errors"]) and "imp9-v1.md" in " ".join(out["errors"]), out
        os.rename(os.path.join(base, "away"), os.path.join(base, "plans", "archive", "imp9-v1.md"))
        out = mk("unarchive", "imp9", "--data", data, "--home", home)
        assert len(out["moved"]) == 6 and not os.path.exists(base), out
        assert snap(assets) == before_assets, "unarchive did not restore the files byte-identical"
        assert snap(decoys) == before_decoys
        con = project_con()
        r9 = con.execute("SELECT status, header_json, archived_path FROM plans WHERE id='imp9'").fetchone()
        con.close()
        h9 = json.loads(r9["header_json"])
        assert r9["status"] == "parked" and not {"archived_from", "archived_to", "archived_files"} & set(h9)
        assert r9["archived_path"] == v2
        out = mk("unarchive", "imp9", "--data", data, "--home", home, code=2)  # not archived now
        assert "not archived" in " ".join(out["errors"]), out

        # crash recovery: status written, archived_to recorded, folder never moved -> unarchive
        # just restores the status
        seed("impc", "done")
        con = project_con()
        con.execute("UPDATE plans SET status='archived', header_json=? WHERE id='impc'",
                    (json.dumps({"archived_from": "done", "archived_to": os.path.join(data, "nowhere")}),))
        con.commit()
        con.close()
        out = mk("unarchive", "imp" + "c", "--data", data, "--home", home)
        assert out["ok"] and os.path.isfile(os.path.join(plans_dir, "impc.md")), out
        con = project_con()
        assert con.execute("SELECT status FROM plans WHERE id='impc'").fetchone()[0] == "done"
        con.close()

        # repair: already 'archived' with no record and the legacy files still in place -> the move
        # happens; a second archive then refuses; a taken archive name gets a stamp, never overwritten
        seed("impb", "archived")
        taken = mkpaths.archive_dir(data, "impb", home)
        put(os.path.join(taken, "old.md"), "older\n")
        out = mk("archive", "impb", "--data", data, "--home", home)
        assert len(out["moved"]) == 1 and out["moved"][0].startswith(taken + "-"), out  # plan file only
        assert open(os.path.join(taken, "old.md")).read() == "older\n"
        assert not os.path.exists(os.path.join(plans_dir, "impb.md"))
        con = project_con()
        hb = json.loads(con.execute("SELECT header_json FROM plans WHERE id='impb'").fetchone()[0])
        con.close()
        assert out["moved"][0].startswith(hb["archived_to"] + os.sep) and hb["archived_to"].startswith(taken + "-")
        out = mk("archive", "impb", "--data", data, "--home", home, code=2)
        assert "already archived at" in " ".join(out["errors"]), out

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

        # pause and back (STORE.2): plans.status changes in the store and the file's bytes stay; parse
        # and check answer with the store's status once the plan is stored
        col_before = open(colp, "rb").read()
        parked = mk("set", colp, "--status", "parked", "--data", cols_data, "--home", cols_home)
        assert parked["status"] == "parked" and parked.get("store") is True, parked
        assert open(colp, "rb").read() == col_before
        assert mk("parse", colp, "--data", cols_data, "--home", cols_home)["status"] == "parked"
        assert mk("parse", colp)["status"] == "draft"  # no store resolved: the file answers as before
        n, crow = plans_rows("cols", cols_data, cols_home)
        assert crow["status"] == "parked", crow
        back = mk("set", colp, "--status", "approved", "--data", cols_data, "--home", cols_home)
        assert back["status"] == "approved", back
        n, crow = plans_rows("cols", cols_data, cols_home)
        assert crow["status"] == "approved", crow
        # the plan is in the store: set changes the store row and never the file
        plain_before = open(plain, "rb").read()
        assert mk("set", plain, "--status", "parked", "--data", data, "--home", home)["status"] == "parked"
        assert mk("parse", plain, "--data", data, "--home", home)["status"] == "parked"
        assert mk("set", plain, "--status", "approved", "--data", data, "--home", home)["status"] == "approved"
        assert open(plain, "rb").read() == plain_before

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
        open(mkpaths.db_in(side), "wb").close()
        sp = os.path.join(side, "side.md")
        open(sp, "w").write(plan_text(plan='plan = "sideplan"', phases=phase("P1") + "\n"))
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "approve", sp],
                           capture_output=True, text=True, cwd=tmp,
                           env={k: v for k, v in os.environ.items() if k != "MATRIXKODER_DATA"})
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        assert got["store"] is False and any(os.path.basename(mkpaths.db_in(side)) in w for w in got.get("warnings") or []), got

        # C2 (L3.2): on a MIGRATED project (root db_path set by L2's mk-migrate --switch) import,
        # approve and delete write plans and archives under projects/<slug>/plans/<id>/ and leave
        # the repo's .matrixkoder byte-identical.
        def tree_hash(top):
            h = hashlib.sha256()
            for dirpath, dirnames, filenames in os.walk(top):
                dirnames.sort()
                for name in sorted(filenames):
                    full = os.path.join(dirpath, name)
                    h.update(os.path.relpath(full, top).encode())
                    with open(full, "rb") as fh:
                        h.update(fh.read())
            return h.hexdigest()

        mig_repo = os.path.join(tmp, "mig-repo")
        mig_data = os.path.join(mig_repo, ".matrixkoder")
        os.makedirs(os.path.join(mig_data, "plans", "archive"), exist_ok=True)
        subprocess.run(["git", "init", "-q", mig_repo], check=True)
        pre = os.path.join(mig_data, "plans", "old.md")
        open(pre, "w").write(plan_text(plan='plan = "old"', phases=phase("P1") + "\n"))
        open(mkpaths.legacy_yaml(mig_data), "w").write("data_dir: .matrixkoder/\n")
        home = os.environ["MK_HOME"]
        db.open_project(home, "migs", data_dir=mig_data).close()
        con = db.open_root(home)
        con.execute("INSERT OR REPLACE INTO projects(slug, name, repo, data_dir, mode, db_path)"
                    " VALUES (?,?,?,?,?,NULL)", ("migs", "migs", mig_repo, os.path.realpath(mig_data), "home"))
        con.commit()
        con.close()
        r = subprocess.run([sys.executable, os.path.join(root, "scripts", "mk-migrate.py"),
                            "--switch", "--slug", "migs", "--data", mig_data, "--home", home,
                            "--repo", mig_repo],
                           capture_output=True, text=True, env=dict(os.environ, MK_SELFTEST="1"))
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        proj = os.path.join(home, "projects", "migs")
        before = tree_hash(mig_data)

        mp = write("migplan.md", plan_text(plan='plan = "migplan"',
                                           phases=phase("P1", tasks=task("P1.1")) + "\n"))
        out = mk("import", mp, "--data", mig_data, "--home", home)
        assert out["ok"], out
        # Z15 (R02): import on a migrated project ensures the plan folder's assets/ too
        assert os.path.isdir(os.path.join(proj, "plans", "migplan", "assets")), \
            "a migrated import did not create the plan folder's assets/"
        # the dashboard List reads the root plan_summary: an imported draft must be in it
        con = db.open_root(home)
        n_sum = con.execute("SELECT COUNT(*) c FROM plan_summary WHERE project='migs' AND plan='migplan'").fetchone()["c"]
        con.close()
        assert n_sum == 1 and "warnings" not in out, (n_sum, out)
        out = mk("approve", mp, "--data", mig_data, "--home", home, "--run", "migrun")
        assert out["ok"] and out["version"] == 1, out
        assert out["archived"] == os.path.join(proj, "plans", "migplan", "archive", "migplan-v1.md"), out
        assert os.path.isfile(os.path.join(proj, "plans", "migplan", "archive", "migplan-v1.md"))
        # Z15 (R02): approve keeps the plan.md copy AND ensures assets/ exists (C2). This FAILS on
        # e83e6e9 (proof in the report): assets/ was never created.
        assert os.path.isfile(os.path.join(proj, "plans", "migplan", "plan.md")), \
            "the approved plan.md is missing from the migrated plan folder"
        assert os.path.isdir(os.path.join(proj, "plans", "migplan", "assets")), \
            "a migrated approve did not create the plan folder's assets/ (R02)"
        # a second approve on the same bytes stays a no-op and keeps the folder intact
        out = mk("approve", mp, "--data", mig_data, "--home", home, "--run", "migrun")
        assert out["ok"] and out.get("noop") is True, out
        assert os.path.isdir(os.path.join(proj, "plans", "migplan", "assets")), \
            "the no-op approve dropped the plan folder's assets/"
        out = mk("archive", "migplan", "--data", mig_data, "--home", home)
        arch = os.path.join(proj, "archive", "plans", "migplan")
        assert out["ok"] and out["moved"] == [os.path.join(arch, "folder")], out
        assert os.path.isfile(os.path.join(arch, "folder", "plan.md")) and os.path.isdir(os.path.join(arch, "folder", "assets"))
        assert not os.path.exists(os.path.join(proj, "plans", "migplan"))
        out = mk("delete", "migplan", "--data", mig_data, "--home", home)
        assert out["ok"] and os.path.dirname(out["backup"]) == os.path.join(proj, "archive"), out
        assert os.path.isfile(os.path.join(arch, "folder", "plan.md")), "delete unlinked the archived folder"
        # nothing was written under the repo's .matrixkoder
        assert tree_hash(mig_data) == before, "a migrated mk-plan wrote under the repo .matrixkoder"

        # F.2 approve — counts, the frozen gate fields (a change is a new version naming the field)
        # and the --user-ack refusal. db and mkpaths are already in scope here.
        f2_approve_self_test(tmp, mk, write, plan_text, phase, task, db, mkpaths)

        # C1: the plan-level `description` rides header_json when it is a non-empty string, else
        # the key is absent (a plan without one is unchanged)
        base = {"runs": [], "gates": {}, "final_gates": [], "phases": []}
        assert json.loads(header_json_of(dict(base, description="Why this plan"), "s", "b", "draft")
                          )["description"] == "Why this plan"
        for none in ({}, {"description": ""}, {"description": "  "}, {"description": 5}):
            assert "description" not in json.loads(header_json_of(dict(base, **none), "s", "b", "draft"))

        # STORE: versioned plan_items, status/runs in the store, item set
        store_self_test(tmp, mk, write, plan_text, phase, task, db, mkpaths)

        # bugs-tab: plan id `bugs` is reserved for the permanent bugs run — check and import refuse it
        bp = write("bugs-plan.md", plan_text(plan='plan = "bugs"', phases=phase("P1", tasks=task("P1.1")) + "\n"))
        got = mk("check", bp, code=2)
        assert RESERVED_PLAN_ERR in got["errors"], got
        bdata = os.path.join(tmp, "bugs-plan-data")
        db.open_project(home, data_dir=bdata).close()  # a real store: the refusal is not "no store"
        got = mk("import", bp, "--data", bdata, "--home", home, code=2)
        assert RESERVED_PLAN_ERR in got["errors"], got
        bcon = db.open_project(home, data_dir=bdata)
        try:
            assert bcon.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0, "bugs plan imported"
        finally:
            bcon.close()

        print("mk-plan self-test ok")
    finally:
        restore_data()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        sys.exit(main(sys.argv[1:]))
