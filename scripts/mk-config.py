#!/usr/bin/env python3
"""MatrixKoder config — one schema, one writer: list, get, set, migrate, context, probe. Python 3 stdlib only.

  list --json
  get KEY [--set KEY=VALUE]   (--set: the caller's own value, so a draft can be read back)
  set KEY VALUE               VALUE is JSON: 12, true, "main", ["a","b"]
  migrate
  context [--usage PATH]
  probe
  --self-test

Common: --data DIR, resolved exactly as mk-state.py does (--data > $MATRIXKODER_DATA >
<home>/projects.json > <main checkout>/.matrixkoder). Every command prints ONE JSON line, like
mk-state.py. Exit 0 ok; 2 bad input, an unknown key, or a refused write — and a refusal writes
nothing at all, not even a .bak.

A successful `set` writes `<file>.bak` first: backup = previous version (one-step undo), not the
original file — the Settings tab saves many times in a row. `set` therefore reports
`"changed": true` with `"backup_of": "previous"`, or `"changed": false` and no backup at all when
the value already read that way. Line endings are preserved byte for byte: a CRLF file comes back
CRLF.

Why line-wise: the yaml is hand-edited and every line carries a comment the user wrote. There is no
YAML dependency here, and a real yaml round-trip would rewrite every comment, quote and spacing in
the file on the first write. This reads and patches one line at a time, so one value is a one-line
diff and the comment beside it survives. Only four shapes are ever written (see `patch`): a scalar
line, an insert at the end of an existing block, a whole appended block, and one member of a
one-line flow map. Everything else is refused rather than guessed at — a refusal costs the user a
retry, a wrong write costs them a hand-edited config.

The yaml parser (scalar, split_flow, strip_comment, flow_map, KEY_RE) lives here and mk-lane.py
loads it from this file: one parser for both tools, so a value can never mean two things.

session.selftest_jobs_total: integer 1..16, the whole-machine self-test job budget. When the yaml
has no value the default is derived as max(1, round(cpu_count/3)); list/get/context show that
number and say it is derived.
"""
import contextlib, datetime, difflib, fcntl, importlib.util, json, os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True  # no stray __pycache__ next to the scripts

KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*)$")


# ---------- yaml: line-wise, no dependency (moved verbatim from mk-lane.py) ----------

def scalar(v):
    """One YAML scalar: quoted or plain text up to a ` #` comment (hooks/guards.ts scalar()).

    The comment goes first and is quote-aware, so `"main"   # note` reads as main: hand-quoting a
    value is ordinary in a file people edit, and a value that cannot be read back is a value this
    tool could never write."""
    v = strip_comment(v).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def split_flow(body):
    """Split a flow collection on the commas that are not inside quotes or a nested [ list ]."""
    parts, cur, quote, depth = [], "", None, 0
    for ch in body:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch == "[":
            depth += 1
            cur += ch
        elif ch == "]":
            depth = max(0, depth - 1)
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p for p in parts if p.strip()]


def strip_comment(s):
    """Drop a trailing `# …` that is outside quotes. The wizard emits one on the lanes line, and
    without this the whole `{ … }` is unparseable and every value silently falls back to DEFAULTS."""
    quote = None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i]
    return s


def flow_map(body):
    body = strip_comment(body).strip()
    if not (body.startswith("{") and body.endswith("}")):
        return {}
    out = {}
    for item in split_flow(body[1:-1]):
        k, sep, v = item.partition(":")
        if sep:
            v = v.strip()
            out[k.strip()] = ([scalar(x) for x in split_flow(v[1:-1])]
                              if v.startswith("[") and v.endswith("]") else scalar(v))
    return out


def _load(name, path):
    _spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mod)
    return mod


mk_state = _load("mk_state", os.path.join(HERE, "mk-state.py"))  # data dir resolution, Fail


class Fail(Exception):
    def __init__(self, msg, code=2, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


# ---------- the schema: one row per setting, with its home in the yaml ----------

def _row(key, scope, type, default, sec, line, member, label, help, **kw):
    r = {"key": key, "scope": scope, "type": type, "default": default, "sec": sec, "line": line,
         "member": member, "label": label, "help": help}
    r.update(kw)
    return r


SCHEMA = [
    _row("repo.lanes.slots", "project", "num", 3, ["repo"], "lanes", "slots",
         "Worktree slots", "reusable worktree slots", min=1, max=6),
    _row("repo.lanes.reuse", "project", "bool", True, ["repo"], "lanes", "reuse",
         "Reuse slots", "a released slot is reused instead of deleted"),
    _row("repo.lanes.copy", "project", "list", [], ["repo"], "lanes", "copy",
         "Copy into slot", "gitignored paths a fresh slot needs"),
    _row("repo.integration_branch", "project", "text", "main", ["repo"], "integration_branch", None,
         "Integration branch", "lanes merge here; one PR to main at the end"),
    _row("qa.integration_target", "project", "seg", "local-main", ["qa"], "integration_target", None,
         "Merge target", "where the merged lanes are QA'd",
         opts=[("local-main", "merge into local main, no push"),
               ("integration", "QA the integration branch")]),
    _row("autonomy_contract.builder_skip_permissions", "project", "bool", True,
         ["autonomy_contract"], "builder_skip_permissions", None,
         "Builders skip perms", "builders only, in their own worktree"),
    _row("routing_preferences.policy", "routing", "seg", "claude-sparing",
         ["routing_preferences"], "policy", None, "Routing policy", "how hard the pool is spared",
         opts=[("claude-sparing", "Claude Code orchestrates; builders below"),
               ("balanced", "Claude Code and builders share the work"),
               ("quality", "the strongest route wins, quota second")]),
    _row("routing_preferences.builder_order", "routing", "list", [],
         ["routing_preferences"], "builder_order", None,
         "Builder order", "agents in the order a phase hands them work"),
    _row("routing_preferences.max_build_lanes", "routing", "num", 3,
         ["routing_preferences"], "max_build_lanes", None,
         "Max build lanes", "concurrent builder lanes", min=1, max=6),
    _row("routing_preferences.reserve_floor.claude.five_hour_left_pct", "routing", "num", 40,
         ["routing_preferences", "reserve_floor"], "claude", "five_hour_left_pct",
         "Claude 5h", "never route Claude below this share of its 5h window",
         min=0, max=90, unit="%"),
    _row("routing_preferences.reserve_floor.claude.weekly_left_pct", "routing", "num", 30,
         ["routing_preferences", "reserve_floor"], "claude", "weekly_left_pct",
         "Claude week", "never route Claude below this share of its week",
         min=0, max=90, unit="%"),
    _row("routing_preferences.reserve_floor.others.five_hour_left_pct", "routing", "num", 10,
         ["routing_preferences", "reserve_floor"], "others", "five_hour_left_pct",
         "Others 5h", "floor for every non-Claude agent", min=0, max=90, unit="%"),
    _row("routing_preferences.reserve_floor.others.weekly_left_pct", "routing", "num", 15,
         ["routing_preferences", "reserve_floor"], "others", "weekly_left_pct",
         "Others week", "floor for every non-Claude agent", min=0, max=90, unit="%"),
    _row("routing_preferences.reviewer", "routing", "select", "claude sonnet medium",
         ["routing_preferences"], "reviewer", None,
         "Code reviewer", "the route every review goes to"),
    _row("routing_preferences.visual_reviewer", "routing", "select", "claude haiku",
         ["routing_preferences"], "visual_reviewer", None,
         "Visual reviewer", "the route a visual/UI review goes to"),
    _row("usage.source", "agents", "seg", "codexbar", ["usage"], "source", None,
         "Source", "where every agent's usage is read from",
         opts=[("codexbar", "the CodexBar CLI"), ("manual", "numbers typed into the pane")]),
    _row("usage.providers", "agents", "list", [], ["usage"], "providers", None,
         "Providers", "providers shown in the usage block"),
    _row("session.fresh_session", "child", "seg", "new", ["session"], "fresh_session", None,
         "Fresh session per round", "what a fix or takeover round starts the agent with",
         opts=[("new", "/new or /clear before the prompt"), ("same", "keep the session")]),
    _row("session.subagents", "child", "bool", True, ["session"], "subagents", None,
         "Subagents", "a phase may hand its tasks to subagents"),
    _row("session.subagent_model", "child", "select", "", ["session"], "subagent_model", None,
         "Subagent model", "the model a subagent runs on (empty: the orchestrator's own)"),
    _row("session.stall_min", "child", "num", 15, ["session"], "stall_min", None,
         "Stall minutes", "idle minutes before a working lane is called stalled", min=5, max=60),
    _row("session.max_wait_min", "child", "num", 30, ["session"], "max_wait_min", None,
         "Max wait minutes", "minutes lane-watch waits before it gives up", min=10, max=300),
    _row("session.reviewer_split", "child", "seg", "right", ["session"], "reviewer_split", None,
         "Reviewer split", "where the reviewer runs while the builder waits",
         opts=[("right", "the reviewer pane sits beside the lane"),
               ("down", "the reviewer runs after the builder stops")]),
    _row("session.todo_done_status", "child", "seg", "verify", ["session"], "todo_done_status", None,
         "Todo completed becomes", "what a completed todo sets the task to",
         opts=[("verify", "a completed todo moves the task to verify"),
               ("done", "a completed todo moves the task to done")]),
    _row("session.selftest_jobs_total", "child", "num", None, ["session"], "selftest_jobs_total", None,
         "Self-test jobs", "parallel self-tests when MK_SELFTEST_JOBS is unset (1..16)",
         min=1, max=16, integer=True, derived=True),
    _row("session.reuse_reviewer", "child", "bool", True, ["session"], "reuse_reviewer", None,
         "Reuse reviewer", "one reviewer session serves the phase's rounds"),
    _row("bus.enabled", "child", "bool", True, ["bus"], "enabled", None,
         "Bus enabled", "arm starts the loopback bus and writes the lane token."),
    _row("bus.owner_timeout_min", "child", "num", 30, ["bus"], "owner_timeout_min", None,
         "Owner timeout", "bus self-exits after this many idle minutes.", min=5, max=120),
    _row("bus.ask_timeout_s", "child", "num", 600, ["bus"], "ask_timeout_s", None,
         "Ask timeout", "a lane ask waits this long for the answer.", min=60, max=3600),
    # C3: the lane agent when no route is pinned. Global (not per-repo).
    _row("lanes.default_agent", "global", "text", "omp", ["lanes"], "default_agent", None,
         "Default lane agent", "the agent a lane uses when no route is pinned"),
]

MIGRATED_KEYS = ("routing_preferences.reviewer", "routing_preferences.visual_reviewer")
# Dead yaml copies of plugin settings: reported, never written or deleted (C3 — userConfig is the home).
DEAD = (("dashboard", "auto_open", "autoOpen", True),
        ("dashboard", "chip", "chip", True),
        ("dashboard", "toasts", "toasts", True),
        ("usage", "poll_min", "usagePollMin", 5))
# The schema rows this tool writes as a whole block when the section is missing entirely.
SESSION_SECTION, PICKS_SECTION = "session", "picks"
AGENT_NAMES = {"opencode": "OpenCode", "claude": "Claude Code", "codex": "Codex", "agy": "AGY", "omp": "omp"}
HELPERS = [
    ("herdr", "Pane and worktree manager."),
    ("jev", "Judgement gates for routing."),
    ("context_mode", "Context compression for big outputs."),
    ("codebase_memory", "Serena-style project memory."),
    ("canary", "Browser QA recordings."),
]
# The agents whose model list a CLI can answer for, and the argv that asks. claude and codex have
# no such command here: their picks are written by hand and the list is small enough to keep.
# omp is NOT here. `omp models` is a box table (provider sections, thinking column), and the
# Settings pool is a curated allow-list (C1), not that table. Probe must not overwrite it.
MODEL_CLIS = {"opencode": "opencode", "agy": "agy"}
# User-editable seed for agents.omp.models. Edit this list (or the yaml models list); probe does not.
OMP_MODEL_ALLOWLIST = ["manifest/auto"]


# ---------- structure: where each line is ----------

def key_lines(lines):
    """[(index, indent, key, rest)] for every `key:` line; rest is the text after the colon."""
    out = []
    for i, raw in enumerate(lines):
        m = KEY_RE.match(raw)
        if m:
            out.append((i, len(m.group(1)), m.group(2), m.group(3)))
    return out


def section_range(lines, kl, path):
    """-> (header index, header indent, first child index, end index) for a block section path like
    ["routing_preferences", "reserve_floor"], or None when any level is absent or not a block.
    `end` is the next key line at or above the header's own indent — the block's last line is end-1."""
    ind, lo, hi = -1, 0, len(lines)  # line indexes, not key-line indexes: the tail of the file matters
    hit = None
    for want in path:
        hit = None
        for (i, indent, key, rest) in kl:
            if lo <= i < hi and indent > ind and key == want and not strip_comment(rest).strip():
                hit = (i, indent)
                break
        if not hit:
            return None
        i, ind = hit
        end = len(lines)
        for (j, jind, _k, _r) in kl:
            if j > i and jind <= ind:
                end = j
                break
        lo, hi = i + 1, end
    return (hit[0], hit[1], lo, hi)


def child_indent(lines, sr):
    """The indent a block section's own entries use: its first entry, or the header + 2."""
    lo, hi = sr[2], sr[3]
    for i in range(lo, hi):
        if KEY_RE.match(lines[i]):
            return len(KEY_RE.match(lines[i]).group(1))
    return sr[1] + 2


def agents(lines):
    """-> [(agent, flow map)] for every `<agent>: { … }` line in the agents block. The picked models
    for an agent are only knowable from the file, so those schema rows are built from it."""
    kl, sr = key_lines(lines), section_range(lines, key_lines(lines), ["agents"])
    if not sr:
        return []
    ind = child_indent(lines, sr)
    return [(k, flow_map(rest)) for (i, indent, k, rest) in kl
            if sr[2] <= i < sr[3] and indent == ind and flow_map(rest)]


def rows(lines):
    """The static schema rows plus one `agents.<a>.available` row per agent in the file and one
    `picks.<a>` row per agent that lists models, so a key that is not a known agent simply does not
    exist. herdr has no models and is not a coding agent: it gets an availability row, never a picks
    row — a Settings tab must not offer to order the models of something that has none."""
    out = list(SCHEMA)
    for name, m in agents(lines):
        out.append(_row(f"agents.{name}.available", "agents", "bool", bool(m.get("available", False)),
                        ["agents"], name, "available", AGENT_NAMES.get(name, name),
                        "detected at bootstrap, logged in and installed",
                        editable=False, why="detected at bootstrap — not a typed setting"))
        if isinstance(m.get("models"), list):
            pool = list(m.get("models") or [])
            if name == "omp":
                # C1: the picker offers the allow-list, never the probed `omp models` table.
                pool = list(OMP_MODEL_ALLOWLIST)
            out.append(_row(f"picks.{name}", "models", "list", [], [PICKS_SECTION], name, None,
                            f"{name} model picks", "this agent's models in preference order",
                            maxlen=4, pool=pool))
    for hid, hhelp in HELPERS:
        out.append(_row(f"helpers.{hid}", "about", "seg", "not probed", ["helpers"], "helpers", hid,
                        hid, hhelp, editable=False, why="Read-only: probed at preflight"))
    return out


def row_for(lines, key):
    for r in rows(lines):
        if r["key"] == key:
            return r
    return None


ROUTE_KEYS = ("routing_preferences.reviewer", "routing_preferences.visual_reviewer")
CATALOG_TIMEOUT_S = 10  # a settings screen that waits longer than this is broken, not loading
_ROUTES_CACHE = {}  # one catalog read per process; the self-test clears it for a fresh read


def catalog_home():
    """The MK home `mk-catalog.py` would use: $MK_HOME or $MATRIXKODER_HOME (both set must
    agree), else ~/.matrixkoder — the same rule as mkdb.resolve_home with no --home argument."""
    mkh = os.environ.get("MK_HOME")
    mch = os.environ.get("MATRIXKODER_HOME")
    if mkh and mch:
        left = os.path.realpath(os.path.abspath(os.path.expanduser(mkh)))
        right = os.path.realpath(os.path.abspath(os.path.expanduser(mch)))
        if left != right:
            raise Fail(f"MK_HOME ({mkh}) and MATRIXKODER_HOME ({mch}) differ; set one MK home")
    return os.path.abspath(os.path.expanduser(mkh or mch or "~/.matrixkoder"))


def catalog_routes():
    """The runnable routes `mk-catalog.py routes --json` lists, or None when the catalog is absent
    or errors. A settings screen must never crash because the catalog is missing, so every failure
    — a missing script, a non-zero exit, a timeout, output that is not the documented shape — reads
    as "no catalog" and the caller falls back to the current value alone.

    The catalog command creates <home>/mk.db when none exists, so it is never called unless the
    store is already there: opening Settings must not create ~/.matrixkoder/mk.db. The result is
    cached for the process, so one `list` starts at most one subprocess."""
    if "routes" in _ROUTES_CACHE:
        return _ROUTES_CACHE["routes"]
    if not os.path.isfile(os.path.join(catalog_home(), "mk.db")):
        _ROUTES_CACHE["routes"] = None  # no store: do not call the catalog, it would create one
        return None
    try:
        counter = os.environ.get("MK_CONFIG_COUNT_ROUTES")  # self-test only: counts the calls
        if counter:
            with open(counter, "a") as f:
                f.write("1")
        p = subprocess.run([sys.executable, os.path.join(HERE, "mk-catalog.py"), "routes", "--json"],
                           capture_output=True, text=True, timeout=CATALOG_TIMEOUT_S)
    except Exception:  # a timeout or a missing interpreter: the catalog is not there
        _ROUTES_CACHE["routes"] = None
        return None
    if p.returncode != 0:
        _ROUTES_CACHE["routes"] = None
        return None
    try:
        data = json.loads(p.stdout)
    except ValueError:
        _ROUTES_CACHE["routes"] = None
        return None
    routes = data.get("routes") if isinstance(data, dict) and data.get("ok") is True else None
    if not isinstance(routes, list) or not all(isinstance(x, str) and x.strip() for x in routes):
        _ROUTES_CACHE["routes"] = None
        return None
    # a home with no catalog seeds an empty one and still exits 0: an empty pool is "no catalog",
    # so the dropdown falls back to the current value instead of flagging it stale
    if not routes:
        _ROUTES_CACHE["routes"] = None
        return None
    got = list(dict.fromkeys(routes))  # the catalog's order, duplicates dropped
    _ROUTES_CACHE["routes"] = got
    return got


def opts_of(r, lines):
    """The [[value, note]] option list a UI shows; session.subagent_model's options are the models
    the yaml says this OpenCode build has, so they come from the file and never from a hard-coded list.

    The two reviewer rows take their options from the catalog's runnable routes. A saved value the
    catalog no longer lists stays in the list and is flagged `stale` — dropping it would hide the
    value the file holds, and re-saving it is refused (item 39). With the catalog absent the
    dropdown falls back to the current value alone, so the screen still draws."""
    if r["key"] == "session.subagent_model":
        return [[m, ""] for m in dict(agents(lines)).get("opencode", {}).get("models", [])]
    if r["key"] in ROUTE_KEYS:
        current = value_of(lines, r)[1]
        routes = catalog_routes()
        pool = [current] if routes is None else list(routes)
        if current not in pool:  # a saved route the catalog dropped: kept, and marked stale
            pool.append(current)
        return [[v, "stale" if routes is not None and v not in routes else ""] for v in pool]
    return [[v, note] for (v, note) in r["opts"]] if r.get("opts") else None


# ---------- reading ----------

def derived_selftest_jobs():
    """The job budget when the yaml has no session.selftest_jobs_total: max(1, round(cores/3))."""
    cores = os.cpu_count() or 1
    return max(1, round(cores / 3))


def effective_default(r):
    """The default a read shows. A derived row has no fixed number in the schema."""
    if r.get("derived") and r["key"] == "session.selftest_jobs_total":
        return derived_selftest_jobs()
    return r["default"]


def coerce(type, raw, default):
    """A yaml scalar as the schema's type; anything unreadable falls back to the default rather than
    failing a read, so one bad hand-edit never makes the whole config unreadable.

    The comment is stripped BEFORE the quotes are read: `integration_branch: "main"   # note` is the
    value main with a comment, not the string `"main"`. Hand-quoting is normal in a file people edit,
    and a value this cannot read back is a value `set` can never write."""
    s = scalar(strip_comment(raw))
    if type == "bool":
        return {"true": True, "false": False}.get(s.lower(), default)
    if type == "num":
        try:
            return int(s)
        except ValueError:
            return default  # a float or a word is unreadable: the default, never a guessed number
    if type == "list":
        t = s.strip()
        if t.startswith("[") and t.endswith("]"):
            return [scalar(x) for x in split_flow(t[1:-1])]
        return default
    return s


def value_of(lines, r):
    """-> (found, value): the yaml's value for a schema row, else the schema default
    (a derived default when the row has one)."""
    dflt = effective_default(r)
    if r["key"].startswith("helpers."):
        kl = key_lines(lines)
        hits = [i for (i, indent, k, _r) in kl if indent == 0 and k == "helpers"]
        if len(hits) != 1:
            return False, dflt
        rest = KEY_RE.match(lines[hits[0]]).group(3)
        m = flow_map(rest)
        if r["member"] in m:
            return True, coerce(r["type"], m[r["member"]], dflt)
        sr = section_range(lines, kl, ["helpers"])
        if sr:
            ch_hits = [i for (i, indent, k, _r) in kl if sr[2] <= i < sr[3] and k == r["member"]]
            if len(ch_hits) == 1:
                ch_rest = KEY_RE.match(lines[ch_hits[0]]).group(3)
                return True, coerce(r["type"], ch_rest, dflt)
        return False, dflt
    kl, sr = key_lines(lines), section_range(lines, key_lines(lines), r["sec"])
    if not sr:
        return False, dflt
    hits = [i for (i, indent, k, _r) in kl if sr[2] <= i < sr[3] and k == r["line"]]
    if len(hits) != 1:
        return False, dflt  # absent, or a duplicate: a read never guesses
    rest = KEY_RE.match(lines[hits[0]]).group(3)
    if r["member"] is None:
        return True, coerce(r["type"], rest, dflt)
    m = flow_map(rest)
    if r["member"] not in m:
        return False, dflt
    got = m[r["member"]]
    # flow_map already gives a nested [ list ] back as a list; everything else is still a scalar
    return True, got if r["type"] == "list" else coerce(r["type"], got, dflt)


# ---------- writing: values as yaml text ----------

PLAIN_RE = re.compile(r"[A-Za-z0-9_][^:#{}\[\]&*!|>%@`\"']*")
WORDY_RE = re.compile(r"[-+]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][-+]?[0-9]+)?\Z")


def needs_quotes(v):
    """A value is written plain only when a plain scalar round-trips to the same string: leading
    letter/underscore/digit, no colon-space, no comment start, nothing that yaml reads as a number."""
    if not PLAIN_RE.fullmatch(v) or v != v.strip() or ": " in v or " #" in v:
        return True
    return v.lower() in ("true", "false", "null", "yes", "no", "on", "off") or bool(WORDY_RE.fullmatch(v))


def emit_scalar(v):
    return json.dumps(v) if needs_quotes(v) else v


def emit(value, type):
    """The yaml text for a schema value."""
    if type == "list":
        return "[" + ", ".join(emit_scalar(str(x)) for x in value) + "]"
    if type == "bool":
        return "true" if value else "false"
    if type == "num":
        return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
    return emit_scalar(str(value))


def split_tail(s):
    """-> (value text, everything from the first ` #` on) so a patch keeps the comment a user wrote.
    The spaces in front of the `#` belong to the tail, not to the value, so a re-emitted line keeps
    the spacing the file already had."""
    quote, at = None, len(s)
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            at = i
            break
    head = s[:at].rstrip()
    return head, s[len(head):]


# ---------- the patcher ----------

def _check_target(lines, i, ind, sr, r):
    """A line is only rewritten when it is the section's own indentation and has no tab in it: a
    differently indented or tab-indented duplicate means the hand-edited file is not what we think."""
    if "\t" in lines[i][:ind]:
        raise Fail(f"{r['key']}: `{r['line']}:` is tab-indented; refusing")
    want = child_indent(lines, sr)
    if ind != want:
        raise Fail(f"{r['key']}: `{r['line']}:` is indented {ind}, the section uses {want}; refusing")


def duplicate_sections(lines, name):
    """1-based line numbers of every top-level `name:` block header. Two of them mean two parsers can
    disagree about which one wins, so a patch refuses instead of editing whichever it found first."""
    return [i + 1 for (i, indent, k, rest) in key_lines(lines)
            if indent == 0 and k == name and not strip_comment(rest).strip()]


def patch(lines, r, value):
    """-> (new lines, how many lines changed). Raises Fail for every shape this patcher will not
    write; the caller has already validated the value, so a Fail here means the file's shape."""
    for name in r["sec"]:
        dupes = duplicate_sections(lines, name)
        if len(dupes) > 1:
            raise Fail(f"{r['key']}: {name}: appears {len(dupes)} times (lines "
                       f"{', '.join(str(n) for n in dupes)}); which one wins is not ours to guess")
    kl, sr = key_lines(lines), section_range(lines, key_lines(lines), r["sec"])
    text = emit(value, r["type"])
    if not sr:  # shape 3: the whole block is missing — append it
        if len(r["sec"]) != 1:
            raise Fail(f"{r['key']}: {'.'.join(r['sec'])} is missing; a flow-map block is never invented")
        eol = file_ending(lines)
        block = [f"{r['sec'][0]}:{eol}", f"  {r['line']}: {text}{eol}"]
        if lines and not lines[-1].endswith("\n"):
            block[0] = eol + block[0]
        return list(lines) + block, 2
    hits = [(i, ind) for (i, ind, k, _r) in kl if sr[2] <= i < sr[3] and k == r["line"]]
    if r["member"] is not None:  # shape 4: one member of a one-line flow map, the rest kept as written
        if len(hits) != 1:
            raise Fail(f"{r['key']}: no single `{r['line']}:` flow line in {'.'.join(r['sec'])}; refusing")
        i, ind = hits[0]
        _check_target(lines, i, ind, sr, r)
        head, tail = split_tail(KEY_RE.match(lines[i]).group(3))
        b0, b1 = head.find("{"), head.rfind("}")
        if b0 < 0 or b1 <= b0:
            raise Fail(f"{r['key']}: `{r['line']}:` is not a one-line flow map; refusing")
        parts = [p.strip() for p in split_flow(head[b0 + 1:b1])]
        names = [p.partition(":")[0].strip() for p in parts]
        if r["member"] not in names:
            raise Fail(f"{r['key']}: `{r['member']}` is not in the `{r['line']}` flow map; refusing")
        parts[names.index(r["member"])] = f"{r['member']}: {text}"
        new = list(lines)
        new[i] = " " * ind + f"{r['line']}: {{ " + ", ".join(parts) + " }" + tail.rstrip("\r") \
            + ending_of(lines[i])
        return new, 1
    if len(hits) > 1:
        raise Fail(f"{r['key']}: {len(hits)} `{r['line']}:` lines in {'.'.join(r['sec'])} "
                   f"(lines {', '.join(str(i + 1) for i, _ in hits)}); refusing")
    if not hits:  # shape 2: the block is there, the key is not — insert at the end of the block
        ind, pos = child_indent(lines, sr), sr[3]
        eol = file_ending(lines)
        line = " " * ind + f"{r['line']}: {text}{eol}"
        if pos and not lines[pos - 1].endswith("\n"):
            line = eol + line
        return lines[:pos] + [line] + lines[pos:], len(line.splitlines())
    i, ind = hits[0]  # shape 1: one value on one line, the comment kept
    _check_target(lines, i, ind, sr, r)
    _head, tail = split_tail(KEY_RE.match(lines[i]).group(3))
    new = list(lines)
    new[i] = " " * ind + f"{r['line']}: {text}" + tail.rstrip("\r") + ending_of(lines[i])
    return new, 1


LOCK_WAIT_S = 5  # a Settings save waits this long for another writer, then refuses; never hangs
LOCK_NAME = "matrixkoder.yaml.lock"


@contextlib.contextmanager
def yaml_lock(path):
    """Exclusive flock on a sibling lock file for the whole read-modify-write.

    The kernel drops the lock when this process dies (kill -9 included). A holder that outlasts
    LOCK_WAIT_S is a named error, not a second writer and not a hang. Reads never call this.
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(d, exist_ok=True)
        f = open(os.path.join(d, LOCK_NAME), "a")
    except OSError as e:
        raise Fail(f"could not lock {path}: {e.strerror or e}")
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Fail(f"{LOCK_NAME} busy")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def write_yaml(path, old, new, count, checks):
    """Prove the patched text is the file we meant to write, back up the version being replaced, then
    replace it atomically. A yaml a human edited by hand is the only config there is: a wrong write is
    silent corruption, so nothing is written unless the new text parses back to exactly the values
    that were asked for and differs from the old file only in the lines the patch named.

    `.bak` is the version this write replaces (one-step undo), not the original file — the Settings tab
    saves many times in a row. It is made AFTER verify and AFTER the writability check, so a refused set
    leaves it exactly as it was. backup = previous version."""
    verify(old, new, count, checks)
    if os.path.exists(path):
        if not os.access(path, os.W_OK):
            raise Fail(f"{path} is not writable; fix its permissions or write it by hand")
        with contextlib.suppress(OSError):
            os.unlink(path + ".bak")  # a read-only .bak from an earlier run must not block the copy
        shutil.copyfile(path, path + ".bak")  # bytes only: copy2 would carry a read-only mode over
        os.chmod(path + ".bak", 0o644)
    d = os.path.dirname(path) or "."
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    except OSError as e:
        raise Fail(f"could not write in {d}: {e.strerror or e}")
    try:
        with os.fdopen(fd, "w", newline="") as f:  # newline='' : no \\n -> \\r\\n translation
            f.writelines(new)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; match the rest of <data>
        os.replace(tmp, path)  # readers never see a partial file
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise Fail(f"could not write {path}: {e.strerror or e}")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def changed_lines(old, new):
    """How many lines of the old file the new one replaces, counted independently of the patch."""
    n = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "delete":
            raise Fail("the patch would drop lines; refusing")
        if tag != "equal":
            n += max(j2 - j1, i2 - i1)
    return n


def is_block_scalar(raw):
    """True when a key line's inline value is a YAML block-scalar indicator (`|`, `>`, with
    optional chomping/indent modifiers and optional comment) — its deeper-indented lines are the
    scalar's text, not orphans."""
    return bool(re.match(r"^\s*[|>](?:[+-]?[0-9]?|[0-9]?[+-]?)?\s*(?:#.*)?$", raw))


def check_structure(reread):
    """Refuse corrupt yaml: no duplicate top-level sections, and no orphan child lines
    (no line indented deeper than a preceding key line that has an inline scalar/flow-map value;
    block-scalar values own their deeper-indented lines)."""
    seen_top = set()
    prev_key = None
    for i, raw in enumerate(reread):
        line = strip_comment(raw).rstrip()
        if not line.strip():
            continue
        m = KEY_RE.match(raw)
        if m:
            indent = len(m.group(1))
            k = m.group(2)
            rest = m.group(3)
            if indent == 0:
                if k in seen_top:
                    raise Fail(f"corrupt yaml: duplicate top-level section `{k}` (line {i + 1})")
                seen_top.add(k)
            if prev_key is not None:
                prev_i, prev_indent, prev_k, prev_rest = prev_key
                if strip_comment(prev_rest).strip() and not is_block_scalar(prev_rest) and indent > prev_indent:
                    raise Fail(f"corrupt yaml: orphan child line {i + 1} `{k}:` indented deeper than inline `{prev_k}:` (line {prev_i + 1})")
            prev_key = (i, indent, k, rest)
        else:
            indent = len(raw) - len(raw.lstrip())
            if prev_key is not None:
                prev_i, prev_indent, prev_k, prev_rest = prev_key
                if strip_comment(prev_rest).strip() and not is_block_scalar(prev_rest) and indent > prev_indent:
                    raise Fail(f"corrupt yaml: orphan child line {i + 1} indented deeper than inline `{prev_k}:` (line {prev_i + 1})")


def verify(old, new, count, checks):
    reread = "".join(new).splitlines(keepends=True)
    check_structure(reread)
    if changed_lines(old, new) != count:
        raise Fail(f"the patch would change {changed_lines(old, new)} lines, not the {count} it named")
    for key, value, r in checks:
        found, got = value_of(reread, r)
        if not found or got != value:
            raise Fail(f"{key}: the patched file reads {got!r}, not {value!r}; nothing written")


# ---------- commands ----------

def pin_tmp_data(tmp):
    """Point MATRIXKODER_DATA/HOME at temp dirs inside `tmp` for a whole self-test, and prove the
    resolution actually lands there. Run from a lane without --data, the data dir resolves to the
    MAIN checkout's .matrixkoder/ — a self-test would then read (and any future write path would
    touch) a real config. -> restore(), for the caller's finally."""
    data, home = os.path.join(tmp, "data"), os.path.join(tmp, "home")
    os.makedirs(data, exist_ok=True), os.makedirs(home, exist_ok=True)
    was = {k: os.environ.get(k) for k in ("MATRIXKODER_DATA", "MATRIXKODER_HOME", "MK_HOME", "MK_SELFTEST")}

    def restore():
        for k, v in was.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # One temp MK home under both names (C16), plus MK_SELFTEST=1 so the mkdb
    # write guard refuses the real home for the whole self-test.
    os.environ["MATRIXKODER_DATA"], os.environ["MATRIXKODER_HOME"] = data, home
    os.environ["MK_HOME"], os.environ["MK_SELFTEST"] = home, "1"
    try:
        got = os.path.realpath(mk_state.resolve_data(None))
        # the resolved dir, not the env value just written: a resolver that ignores the env and
        # lands in a real checkout must fail this assert
        assert os.path.commonpath([got, os.path.realpath(tmp)]) == os.path.realpath(tmp), \
            f"the self-test would resolve {got}, which is outside its temp dir {tmp}"
    except Exception:
        restore()  # a failed pin must not leave the caller's environment mutated
        raise
    return restore


def read_lines(path):
    """newline='' so a CRLF file keeps its \\r inside each line: the patcher re-emits the ending it
    read, and a config written on Windows must not come back rewritten as LF."""
    try:
        with open(path, newline="") as f:
            return f.readlines()
    except OSError:
        return []


def ending_of(line):
    """The line terminator a line already carries: a patched line keeps its own, an inserted one
    takes the file's (a file with no terminator at all gets \\n)."""
    return "\r\n" if line.endswith("\r\n") else "\n"


def file_ending(lines):
    return "\r\n" if any(l.endswith("\r\n") for l in lines) else "\n"


def out(obj, code=0):
    print(json.dumps(obj))
    return code


def parse_value(raw, r, lines=None):
    """`set` takes JSON so a number, a bool, a string and a list are told apart; a bare word is a
    string, and anything else is a refusal rather than a guess."""
    try:
        v = json.loads(raw)
    except ValueError:
        v = raw if isinstance(raw, str) else raw
    t = r["type"]
    if t == "bool":
        if not isinstance(v, bool):
            raise Fail(f"{r['key']} is a bool: {raw} is not true or false")
    elif t == "num":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            if r.get("integer"):
                raise Fail(f"{r['key']} must be an integer from {r.get('min')} to {r.get('max')}: {raw}")
            raise Fail(f"{r['key']} is a number: {raw} is not one")
        if r.get("integer") and isinstance(v, float):
            lo, hi = r.get("min"), r.get("max")
            raise Fail(f"{r['key']} must be an integer from {lo} to {hi}: {raw}")
        if "min" in r and v < r["min"] or "max" in r and v > r["max"]:
            lo, hi = r.get("min"), r.get("max")
            if r.get("integer"):
                raise Fail(f"{r['key']} must be an integer from {lo} to {hi}: {raw}")
            raise Fail(f"{r['key']} must be between {lo} and {hi}: {raw}")
    elif t == "list":
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise Fail(f"{r['key']} is a list of strings: {raw} is not")
        if r.get("maxlen") and len(v) > r["maxlen"]:
            raise Fail(f"{r['key']} takes at most {r['maxlen']} entries: {raw} has {len(v)}")
        if r["key"] == "routing_preferences.builder_order" and any(
                x.strip().casefold() == "codex" for x in v):
            raise Fail(f"{r['key']}: codex is never a builder; {raw}")
        # pool membership is checked again under the lock (cmd_set re-reads). This pass only
        # rejects a shape that cannot be a list of strings.
    else:
        if not isinstance(v, str):
            raise Fail(f"{r['key']} is a string: {raw} is not")
        if r.get("opts") and v not in [o[0] for o in r["opts"]]:
            raise Fail(f"{r['key']} must be one of {', '.join(o[0] for o in r['opts'])}: {raw}")
        if r["key"] in ROUTE_KEYS:  # the dropdown's options are the catalog, not the schema row
            allowed = [o[0] for o in (opts_of(r, lines) or [])]
            if v not in allowed:
                raise Fail(f"{r['key']} must be one of {', '.join(allowed)}: {raw}")
    return v


def cmd_list(path, lines):
    fields = []
    for r in rows(lines):
        found, value = value_of(lines, r)
        shown_default = effective_default(r)
        f = {"key": r["key"], "scope": r["scope"], "label": r["label"], "type": r["type"],
             "value": value, "default": shown_default, "help": r["help"],
             "editable": r.get("editable", True)}
        if r.get("derived") and not found:
            f["derived"] = True  # the yaml has no value: value and default are the derived number
        if r["type"] == "num":
            f["min"], f["max"] = r.get("min"), r.get("max")
            if r.get("unit"):
                f["unit"] = r["unit"]
        opts = opts_of(r, lines)
        if opts is not None:
            f["opts"] = opts
            if r["key"] in ROUTE_KEYS and any(n == "stale" for (_v, n) in opts):
                f["stale"] = True  # a saved route the catalog no longer lists stays visible and flagged
        if r["key"] == "routing_preferences.builder_order" and isinstance(value, list):
            bad = [x for x in value if isinstance(x, str) and x.strip().casefold() == "codex"]
            if bad:  # a hand edit from before the rule: shown, flagged, not silently dropped
                f["unavailable"] = bad
        if r.get("pool") is not None:
            f["pool"] = list(r["pool"])  # the only models `set` accepts for this agent: one pool, picker and validator
            if r["type"] == "list" and isinstance(value, list):
                gone = [x for x in value if isinstance(x, str) and x not in r["pool"]]
                if gone:  # a saved pick a later probe dropped: still shown, flagged unavailable
                    f["unavailable"] = gone
        if not f["editable"]:
            f["why"] = r["why"]
        fields.append(f)
    return out({"ok": True, "file": path, "fields": fields})


def cmd_get(path, lines, args):
    key = args[0] if args else None
    if not key:
        raise Fail("get needs a KEY")
    r = row_for(lines, key)
    if not r:
        raise Fail(f"unknown key: {key}")
    for i, a in enumerate(args):
        if a == "--set":
            k, sep, raw = args[i + 1].partition("=") if i + 1 < len(args) else ("", "", "")
            if not sep or k != key:
                raise Fail(f"--set needs KEY=VALUE for {key}")
            return out({"ok": True, "key": key, "value": parse_value(raw, r, lines)})  # the caller's value wins
    found, value = value_of(lines, r)
    ans = {"ok": True, "key": key, "value": value}
    if r.get("derived") and not found:
        ans["derived"] = True  # no yaml value: this number is max(1, round(cores/3))
        ans["default"] = value
    if r.get("integer"):
        # the scalar as written. A hand edit of "abc" or 99 still resolves to a value, and a
        # reader that must not trust it (selftest.sh) warns from this text.
        kl = key_lines(lines)
        sr = section_range(lines, kl, r["sec"])
        hits = [i for (i, indent, k, _r) in kl if sr and sr[2] <= i < sr[3] and k == r["line"]]
        if len(hits) == 1:
            raw = scalar(KEY_RE.match(lines[hits[0]]).group(3))
            if not (raw.isdigit() and r.get("min", 1) <= int(raw) <= r.get("max", 16)):
                ans["raw"] = raw  # selftest.sh warns and uses 4; list/context keep the derived value
    return out(ans)


def cmd_set(path, lines, args):
    if len(args) < 2:
        raise Fail("set needs KEY and VALUE (JSON)")
    key, raw = args[0], args[1]
    r = row_for(lines, key)
    if not r:
        raise Fail(f"unknown key: {key}")
    # agents.*.available is editable=False in rows() for Settings UI, but allowed in set CLI for bootstrap / drift detection
    if not r.get("editable", True) and not r["key"].startswith("agents."):
        raise Fail(f"{key} is read-only: {r.get('why', 'not editable')}")
    value = parse_value(raw, r, lines)
    with yaml_lock(path):
        lines = read_lines(path)  # the copy main() read is stale once another writer held the lock
        r = row_for(lines, key) or r
        found, current = value_of(lines, r)
        if r.get("pool") and isinstance(value, list):
            for x in value:
                if x not in r["pool"]:
                    saved = current if isinstance(current, list) else []
                    if x in saved:
                        continue  # already saved and since dropped from the pool: kept
                    raise Fail(f"{r['key']}: {x} is not a model of that agent")
        if found and current == value:  # a save sends every row it holds, most of them unchanged
            return out({"ok": True, "key": key, "value": value, "file": path, "changed": False})
        new, count = patch(lines, r, value)  # refused shapes raise before anything is touched
        write_yaml(path, lines, new, count, [(key, value, r)])
    return out({"ok": True, "key": key, "value": value, "file": path, "changed": True,
                "backup": path + ".bak", "backup_of": "previous"})


def cmd_migrate(path, lines):
    """Bring a config written before this schema up to it, and nothing else: the new sections and
    the two new routing keys get their schema defaults when absent. Idempotent, and it reports the
    dead yaml copies of plugin settings without ever writing or deleting them."""
    with yaml_lock(path):
        return _migrate_locked(path, read_lines(path))


def _migrate_locked(path, lines):
    """Migrate body. Caller holds yaml_lock, so this read is the file that gets written."""
    old = list(lines)
    new, changed, checks, count = list(lines), [], [], 0
    eol = file_ending(new)
    if not section_range(new, key_lines(new), [SESSION_SECTION]):
        block = [f"{SESSION_SECTION}:{eol}"]
        if new and not new[-1].endswith("\n"):
            block[0] = eol + block[0]
        for r in SCHEMA:
            if r["sec"] == [SESSION_SECTION]:
                block.append(f"  {r['line']}: {emit(effective_default(r), r['type'])}{eol}")
        new += block
        changed += [f"{SESSION_SECTION}.{r['line']}" for r in SCHEMA if r["sec"] == [SESSION_SECTION]]
        count += len(block)
    picks_agents = [name for name, m in agents(old) if isinstance(m.get("models"), list)]
    if not section_range(new, key_lines(new), [PICKS_SECTION]):
        block = [f"{PICKS_SECTION}:{eol}"]
        if new and not new[-1].endswith("\n"):
            block[0] = eol + block[0]
        block += [f"  {name}: []{eol}" for name in picks_agents]
        new += block
        changed += [f"{PICKS_SECTION}.{name}" for name in picks_agents]
        count += len(block)
    for key in MIGRATED_KEYS:
        r = row_for(old, key)
        if not value_of(new, r)[0]:
            patched, n = patch(new, r, r["default"])  # appends routing_preferences when it is gone
            new, count = patched, count + n
            changed.append(key)
            checks.append((key, r["default"], r))
    # omp (C1): an agents block with no omp line gains one whose models are the allow-list.
    # Inserted as its own line at the end of the agents block; every other line stays.
    kl = key_lines(new)
    sr = section_range(new, kl, ["agents"])
    if sr and not any(k == "omp" and sr[2] <= i < sr[3] for (i, _ind, k, _r) in kl):
        ind, pos = child_indent(new, sr), sr[3]
        models = ", ".join(OMP_MODEL_ALLOWLIST)
        line = " " * ind + f"omp: {{ available: false, models: [{models}] }}{eol}"
        if pos and not new[pos - 1].endswith("\n"):
            line = eol + line
        new = new[:pos] + [line] + new[pos:]
        changed.append("agents.omp")
        count += 1
    # lanes.default_agent (C3) through the same patcher the other migrated keys use.
    r = row_for(old, "lanes.default_agent")
    if r and not value_of(new, r)[0]:
        patched, n = patch(new, r, r["default"])
        new, count = patched, count + n
        changed.append("lanes.default_agent")
        checks.append(("lanes.default_agent", r["default"], r))
    notes = dead_notes(new)
    if changed:
        write_yaml(path, old, new, count, checks)
    return out({"ok": True, "changed": changed, "notes": notes})


def dead_notes(lines):
    """Dead yaml copies of plugin settings, reported so the user can move them, never written: the
    plugin setting is the only home (C3). Only a value that differs from the plugin default is worth
    a line — one that matches has nothing left to move."""
    kl, out_lines = key_lines(lines), []
    for sec, key, setting, dflt in DEAD:
        sr = section_range(lines, kl, [sec])
        if not sr:
            continue
        hits = [i for (i, _ind, k, _r) in kl if sr[2] <= i < sr[3] and k == key]
        if len(hits) != 1:
            continue
        got = coerce("bool" if isinstance(dflt, bool) else "num",
                     KEY_RE.match(lines[hits[0]]).group(3), dflt)
        if got != dflt:
            out_lines.append(f"{sec}.{key} is {got!r} here and is no longer read — the plugin "
                             f"setting {setting} owns it now (claude plugin configure matrixkoder)")
    return out_lines


def load_usage(usage_path_arg):
    path = usage_path_arg
    if not path:
        path = os.path.join(catalog_home(), "usage.json")
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def extract_left(val):
    if val is None:
        return None
    num = None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        num = val
    elif isinstance(val, dict):
        if "used_pct" in val and isinstance(val["used_pct"], (int, float)) and not isinstance(val["used_pct"], bool):
            num = 100 - val["used_pct"]
        elif "left_pct" in val and isinstance(val["left_pct"], (int, float)) and not isinstance(val["left_pct"], bool):
            num = val["left_pct"]
        elif "left" in val and isinstance(val["left"], (int, float)) and not isinstance(val["left"], bool):
            num = val["left"]
    if num is None:
        return None
    rounded = round(float(num), 1)
    return int(rounded) if rounded.is_integer() else rounded


def is_future_iso(ts):
    if not isinstance(ts, str):
        return False
    try:
        s = ts.strip().replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        now = datetime.datetime.now(datetime.timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt > now
    except Exception:  # an unparseable timestamp is not a reset window
        return False


def cmd_context(path, lines, usage_arg=None):
    schema_rows = {r["key"]: r for r in rows(lines)}  # row_for scans the schema per call; resolve once

    def val(key):
        r = schema_rows.get(key)
        if not r:
            raise Fail(f"unknown schema row: {key}")
        return value_of(lines, r)[1]

    builder_skip_permissions = val("autonomy_contract.builder_skip_permissions")
    if builder_skip_permissions:
        launch_flags = {
            "claude": ["--dangerously-skip-permissions"],
            "codex": ["--dangerously-bypass-approvals-and-sandbox"],
            "opencode": [],
            "agy": [],
            "omp": [],
        }
    else:
        launch_flags = {
            "claude": [],
            "codex": [],
            "opencode": [],
            "agy": [],
            "omp": [],
        }

    usage_data = load_usage(usage_arg)
    usage_agents = usage_data.get("agents") if isinstance(usage_data.get("agents"), dict) else {}

    claude_five_floor = val("routing_preferences.reserve_floor.claude.five_hour_left_pct")
    claude_week_floor = val("routing_preferences.reserve_floor.claude.weekly_left_pct")
    others_five_floor = val("routing_preferences.reserve_floor.others.five_hour_left_pct")
    others_week_floor = val("routing_preferences.reserve_floor.others.weekly_left_pct")

    agents_map = {}
    for name, m in agents(lines):
        r_avail = row_for(lines, f"agents.{name}.available")
        avail = value_of(lines, r_avail)[1] if r_avail else bool(m.get("available", False))

        r_picks = row_for(lines, f"picks.{name}")
        picks = value_of(lines, r_picks)[1] if r_picks else []

        if name == "claude":
            floor = {"five_hour_left_pct": claude_five_floor, "weekly_left_pct": claude_week_floor}
        else:
            floor = {"five_hour_left_pct": others_five_floor, "weekly_left_pct": others_week_floor}

        u_agent = usage_agents.get(name) if isinstance(usage_agents.get(name), dict) else {}
        five_hour_left = extract_left(u_agent.get("five_hour"))
        weekly_left = extract_left(u_agent.get("weekly"))
        left = {"five_hour": five_hour_left, "weekly": weekly_left}

        if five_hour_left is None and weekly_left is None:
            below_floor = None
        else:
            below = False
            if five_hour_left is not None and five_hour_left < floor["five_hour_left_pct"]:
                below = True
            if weekly_left is not None and weekly_left < floor["weekly_left_pct"]:
                below = True
            below_floor = below

        if is_future_iso(u_agent.get("exhausted_until")):
            below_floor = True

        agents_map[name] = {
            "available": avail,
            "picks": picks,
            "floor": floor,
            "left": left,
            "below_floor": below_floor,
        }

    return out({
        "ok": True,
        "policy": val("routing_preferences.policy"),
        "builder_order": val("routing_preferences.builder_order"),
        "builder_order_unavailable": [x for x in val("routing_preferences.builder_order")
                                      if isinstance(x, str) and x.strip().casefold() == "codex"],
        "max_build_lanes": val("routing_preferences.max_build_lanes"),
        "reviewer": val("routing_preferences.reviewer"),
        "visual_reviewer": val("routing_preferences.visual_reviewer"),
        "subagent_model": val("session.subagent_model"),
        "fresh_session": val("session.fresh_session"),
        "reviewer_split": val("session.reviewer_split"),
        "todo_done_status": val("session.todo_done_status"),
        "selftest_jobs_total": val("session.selftest_jobs_total"),
        "selftest_jobs_total_derived": not value_of(
            lines, schema_rows["session.selftest_jobs_total"])[0],
        "reuse_reviewer": val("session.reuse_reviewer"),
        "bus_ask_timeout_s": val("bus.ask_timeout_s"),
        "integration_branch": val("repo.integration_branch"),
        "integration_target": val("qa.integration_target"),
        "builder_skip_permissions": builder_skip_permissions,
        "usage_source": val("usage.source"),
        "providers": val("usage.providers"),
        "launch_flags": launch_flags,
        "default_agent": val("lanes.default_agent"),
        "agents": agents_map,
    })


PROBE_TIMEOUT_S = 30  # every probe gets this long; a health check that needs longer is stale, not missing


def probe_herdr():
    try:
        p = subprocess.run(["herdr", "agent", "list"], capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT_S)
        return "ok" if p.returncode == 0 else "stale"
    except FileNotFoundError:
        return "missing"
    except Exception:  # a timeout or a crash: installed but unverified
        return "stale"


def probe_claude_mcps():
    """The three MCP helpers from `claude mcp list`. `missing` only when the binary is absent or the
    line is absent from a successful listing; a timeout or a non-zero exit leaves them unverified,
    which is `stale`, not `missing`."""
    stale = {"jev": "stale", "context_mode": "stale", "codebase_memory": "stale"}
    try:
        p = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT_S)
    except FileNotFoundError:
        return {"jev": "missing", "context_mode": "missing", "codebase_memory": "missing"}
    except Exception:
        return stale
    if p.returncode != 0:
        return stale

    stdout = p.stdout or ""
    lines = stdout.splitlines()

    def check(pattern):
        matching = [l for l in lines if pattern in l.lower()]
        if not matching:
            return "missing"
        if any("connected" in l.lower() for l in matching):
            return "ok"
        return "stale"

    return {
        "jev": check("jev"),
        "context_mode": check("context-mode"),
        "codebase_memory": check("codebase-memory"),
    }


def probe_canary():
    try:
        p = subprocess.run(["npx", "--no-install", "@usecanary/cli", "--version"],
                           capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
        return "ok" if p.returncode == 0 else "missing"  # spec: a failing npx means no canary
    except FileNotFoundError:
        return "missing"
    except Exception:  # a timeout is installed-but-unverified here too: stale, not missing
        return "stale"


def probe_models(cli):
    """The model ids `<cli> models` prints, one per line, in the order it printed them. Only the
    first whitespace-separated field of a line is the id — `agy models` prints `id<TAB>description`,
    and a description pasted into the yaml would make every pick of that agent unwritable. [] whenever
    the CLI is absent, fails, times out or prints nothing — the caller then leaves the yaml's list
    exactly as it is, because a list nobody could read is not evidence that the agent has no models."""
    try:
        p = subprocess.run([cli, "models"], capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except Exception:  # FileNotFoundError (no such CLI), a timeout, anything it raises on the way
        return []
    if p.returncode != 0:
        return []
    out = []
    for raw in (p.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "\t" in line:  # id<TAB>description: the description may end in "..." and still be an id
            mid = line.split("\t", 1)[0].strip()
        else:
            # no tab: the whole line is the id, so a banner with spaces is not one
            mid = line
            if " " in mid or mid.endswith(":") or mid.endswith("...") or set(mid) <= set("-"):
                continue
        if not mid or not re.fullmatch(r"[A-Za-z0-9._:/@+-]+", mid):
            continue
        if mid not in out:
            out.append(mid)
    return out


def agent_models_row(agent):
    """The patcher row for one agent's `models` member inside its one-line `agents:` flow map. Not
    a settings row — `models` is detected, never typed — only the shape `patch` needs."""
    return _row(f"agents.{agent}.models", "agents", "list", [], ["agents"], agent, "models",
                f"{agent} models", "the models this agent can pick")


def refresh_models(lines):
    """Patch `agents.<a>.models` for every agent whose CLI answered, through the safe patcher, and
    -> (new lines, per-agent state, verify checks). The helpers line is patched first, so a model
    refresh never has to know how the helpers section was written."""
    new, state, checks = lines, {}, []
    for agent, cli in MODEL_CLIS.items():
        ids = probe_models(cli)
        state[agent] = "unchanged"
        if not ids:
            continue
        r = agent_models_row(agent)
        if not value_of(new, r)[0]:
            continue  # no `models` member on this agent's line: there is nothing to refresh
        new, _count = patch(new, r, ids)
        state[agent] = "updated"
        checks.append((r["key"], ids, r))
    return new, state, checks


def cmd_probe(path, lines):
    """Probe without the lock (each CLI may take PROBE_TIMEOUT_S), then lock only to re-read,
    apply the result and write. A set during the probes is not blocked."""
    probed = _probe_collect(lines)
    with yaml_lock(path):
        return _probe_write(path, read_lines(path), probed)


def _probe_collect(lines):
    """The probe results and the model ids, computed before the lock is taken."""
    herdr_status = probe_herdr()
    mcp_statuses = probe_claude_mcps()
    canary_status = probe_canary()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    res = {
        "ok": True,
        "herdr": herdr_status,
        "jev": mcp_statuses["jev"],
        "context_mode": mcp_statuses["context_mode"],
        "codebase_memory": mcp_statuses["codebase_memory"],
        "canary": canary_status,
        "probed_at": now_iso,
    }
    models = {}
    for agent, cli in MODEL_CLIS.items():
        ids = probe_models(cli)
        models[agent] = ids
    return res, models


def _probe_write(path, lines, probed):
    """Apply a finished probe to the yaml. Caller holds yaml_lock and just re-read `lines`."""
    res, models = probed
    helpers_line = (f"helpers: {{ herdr: {res['herdr']}, jev: {res['jev']}, "
                    f"context_mode: {res['context_mode']}, codebase_memory: {res['codebase_memory']}, "
                    f"canary: {res['canary']}, probed_at: {json.dumps(res['probed_at'])} }}")

    kl = key_lines(lines)
    hits = [i for (i, indent, k, _r) in kl if indent == 0 and k == "helpers"]
    if len(hits) > 1:
        raise Fail(f"helpers: appears {len(hits)} times (lines {', '.join(str(i + 1) for i in hits)}); refusing")

    if len(hits) == 1:
        i = hits[0]
        eol = ending_of(lines[i])
        new = list(lines)
        rest = KEY_RE.match(lines[i]).group(3)
        if not strip_comment(rest).strip():
            # a block-style `helpers:` section is replaced whole: its indented children would be
            # orphaned under the new flow-map line and corrupt the file
            sr = section_range(lines, kl, ["helpers"])
            end = sr[3]
            while end > sr[2]:
                body = strip_comment(lines[end - 1]).strip()
                if body:
                    break
                end -= 1
            new[i:end] = [helpers_line + eol]
        else:
            new[i] = helpers_line + eol
    else:
        eol = file_ending(lines)
        new = list(lines)
        line_to_add = helpers_line + eol
        if new and not new[-1].endswith("\n"):
            line_to_add = eol + line_to_add
        new.append(line_to_add)

    checks = [(f"helpers.{hid}", res[hid], row_for(new, f"helpers.{hid}"))
              for hid, _help in HELPERS]
    new, state, model_checks = _apply_models(new, models)
    res["models"] = state
    checks += model_checks
    count = changed_lines(lines, new)
    write_yaml(path, lines, new, count, checks)
    return out(res)


def _apply_models(lines, models):
    """Patch agents.<a>.models from ids already collected. [] means the CLI did not answer:
    leave that agent's list as it is."""
    new, state, checks = lines, {}, []
    for agent, ids in models.items():
        state[agent] = "unchanged"
        if not ids:
            continue
        r = agent_models_row(agent)
        if not value_of(new, r)[0]:
            continue
        new, _count = patch(new, r, ids)
        state[agent] = "updated"
        checks.append((r["key"], ids, r))
    return new, state, checks


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        return self_test()
    data_arg = None
    if "--data" in argv:
        i = argv.index("--data")
        if i + 1 >= len(argv):
            return out({"ok": False, "error": "--data needs a directory"}, 2)
        data_arg, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    usage_arg = None
    if "--usage" in argv:
        i = argv.index("--usage")
        if i + 1 >= len(argv):
            return out({"ok": False, "error": "--usage needs a path"}, 2)
        usage_arg, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    try:
        data = mk_state.resolve_data(data_arg)
        path = os.path.join(data, "matrixkoder.yaml")
        lines = read_lines(path)
        cmd, args = (argv[0], argv[1:]) if argv else (None, [])
        if cmd == "list":
            if "--json" not in args:
                raise Fail("list needs --json")
            return cmd_list(path, lines)
        if cmd == "get":
            return cmd_get(path, lines, args)
        if cmd == "set":
            return cmd_set(path, lines, args)
        if cmd == "migrate":
            return cmd_migrate(path, lines)
        if cmd == "context":
            return cmd_context(path, lines, usage_arg)
        if cmd == "probe":
            return cmd_probe(path, lines)
        raise Fail("usage: mk-config.py [--data DIR] list --json | get KEY [--set KEY=VALUE] | "
                   "set KEY VALUE | migrate | context [--usage PATH] | probe | --self-test")
    except mk_state.Fail as e:  # data dir resolution failed
        return out({"ok": False, "error": str(e)}, e.code)
    except Fail as e:
        return out(dict({"ok": False, "error": str(e)}, **e.extra), e.code)


# ---------- self-test: on a temp copy of scripts/fixtures/matrixkoder.yaml ----------

FIXTURE = os.path.join(HERE, "fixtures", "matrixkoder.yaml")
SESSION_LINES = ("session:\n"
                 "  fresh_session: new   # a fix or takeover round starts a fresh agent session "
                 "unless told otherwise\n"
                 "  subagents: true\n"
                 "  stall_min: 15   # idle minutes before a lane is called stalled\n")
FIVE = ('["opencode/space-bunny-free", "opencode/muse-spark-1.3-contributor-free", '
        '"opencode/big-pickle", "opencode/exo-free", "opencode/fledge-alpha-free"]')


def self_test():
    tmp = tempfile.mkdtemp(prefix="mk-config-selftest-")
    # (a) the pin assert fires when resolution lands outside the temp dir, and a failed pin
    # restores the environment itself. The sibling path starts with the temp path, so the old
    # startswith check would have let it through.
    real = mk_state.resolve_data
    before = {k: os.environ.get(k) for k in ("MATRIXKODER_DATA", "MATRIXKODER_HOME")}
    try:
        mk_state.resolve_data = lambda arg: os.path.realpath(tmp) + "-sibling"
        try:
            pin_tmp_data(tmp)
        except AssertionError as e:
            assert "outside its temp dir" in str(e), e
        else:
            raise AssertionError("pin_tmp_data did not fire when resolution landed outside the temp dir")
        assert {k: os.environ.get(k) for k in before} == before, os.environ
    finally:
        mk_state.resolve_data = real
    restore = None
    try:
        restore = pin_tmp_data(tmp)  # the in-process calls below must resolve here, not in a real repo
        data, home, yaml_path = os.path.join(tmp, "data"), os.path.join(tmp, "home"), None
        os.makedirs(data, exist_ok=True)
        yaml_path = os.path.join(data, "matrixkoder.yaml")
        with open(FIXTURE) as f:
            fixture = f.read()
        env = dict(os.environ, MATRIXKODER_DATA=data, MATRIXKODER_HOME=home,
                   PYTHONDONTWRITEBYTECODE="1")
        script = os.path.join(HERE, "mk-config.py")

        def run(*args, ok=True):
            run_env = dict(env)
            if os.environ.get("MK_HOME"):  # a seeded temp catalog for the reviewer dropdowns
                run_env["MK_HOME"] = os.environ["MK_HOME"]
            if os.environ.get("MATRIXKODER_HOME"):  # kept in lockstep with MK_HOME (one home rule)
                run_env["MATRIXKODER_HOME"] = os.environ["MATRIXKODER_HOME"]
            if os.environ.get("MK_CONFIG_COUNT_ROUTES"):  # the subprocess-count probe
                run_env["MK_CONFIG_COUNT_ROUTES"] = os.environ["MK_CONFIG_COUNT_ROUTES"]
            p = subprocess.run([sys.executable, script, *args], capture_output=True, text=True,
                               env=run_env, cwd=tmp)
            lines = [x for x in (p.stdout or "").splitlines() if x.strip()]
            assert len(lines) == 1, f"{args}: expected ONE json line, got {p.stdout!r} {p.stderr!r}"
            obj = json.loads(lines[0])
            assert obj.get("ok") is ok, f"{args}: {obj}"
            assert p.stderr == "", f"{args}: wrote to stderr: {p.stderr!r}"
            assert (p.returncode == 0) is ok, f"{args}: exit {p.returncode} for ok={ok}"
            return obj

        def rd():
            with open(yaml_path) as f:
                return f.read()

        def diff(a, b):
            return [i for i, (x, y) in enumerate(zip(a.splitlines(), b.splitlines())) if x != y]

        with open(yaml_path, "w") as f:
            f.write(fixture)

        # 1. one value on one commented line: exactly one line changes, the comment stays
        run("set", "session.stall_min", "12")
        after = rd()
        assert diff(fixture, after) == [fixture.splitlines().index(SESSION_LINES.splitlines()[-1])], \
            diff(fixture, after)
        assert "  stall_min: 12   # idle minutes before a lane is called stalled" in after.splitlines(), \
            after.splitlines()
        assert os.path.exists(yaml_path + ".bak") and open(yaml_path + ".bak").read() == fixture

        # 2. a whole missing block is appended, nothing else moves
        before = rd()
        run("set", "picks.opencode", '["opencode/big-pickle", "opencode/exo-free"]')
        added = rd().splitlines()
        assert added[:len(before.splitlines())] == before.splitlines(), "an existing line moved"
        assert added[-2:] == ["picks:", "  opencode: [opencode/big-pickle, opencode/exo-free]"], added[-2:]
        assert run("get", "picks.opencode")["value"] == ["opencode/big-pickle", "opencode/exo-free"]

        # 3. five model picks are refused: nothing written, not even a .bak
        before, bak = rd(), yaml_path + ".bak"
        os.unlink(bak)
        run("set", "picks.opencode", FIVE, ok=False)
        assert rd() == before and not os.path.exists(bak)

        # 4. a value below the schema minimum is refused
        run("set", "session.stall_min", "0", ok=False)
        run("set", "session.max_wait_min", "9", ok=False)
        assert rd() == before and not os.path.exists(bak)

        # 5. one member of a one-line flow map: that line only, every other member kept as written
        run("set", "agents.agy.available", "true")
        assert diff(before, rd()) == [before.splitlines().index(
            "  agy: { available: false, models: [gemini-3.8-flash-medium, claude-opus-5-5-low] }")]
        assert "  agy: { available: true, models: [gemini-3.8-flash-medium, claude-opus-5-5-low] }" \
            in rd().splitlines()
        os.unlink(bak)

        # 6. a comment on an edited line survives
        before = rd()
        run("set", "usage.source", "manual")
        assert "  source: manual   # one source for all agents; mk-usage.py -> ~/.matrixkoder/usage.json" \
            in rd().splitlines()
        os.unlink(bak)

        # 7. migrate twice: the second run changes nothing and the bytes are identical
        run("migrate")
        once = rd()
        out2 = run("migrate")
        assert out2["changed"] == [], out2
        assert rd() == once, "migrate is not idempotent"
        assert "  reviewer: claude sonnet medium" in rd().splitlines()
        assert "  visual_reviewer: claude haiku" in rd().splitlines()
        assert out2["notes"] == [], out2["notes"]  # nothing dead yet differs from the plugin default

        # 8. an absent key reads the schema default; a dead value differing from the plugin default is
        #    reported and never written
        assert run("get", "session.max_wait_min")["value"] == 30
        tampered_dead = rd().replace("  poll_min: 5", "  poll_min: 9")  # read before the file is opened
        with open(yaml_path, "w") as f:
            f.write(tampered_dead)
        notes = run("migrate")["notes"]
        assert any("poll_min" in n and "usagePollMin" in n for n in notes), notes
        assert "  poll_min: 9" in rd().splitlines(), "migrate wrote a dead value"

        # 9. flag > yaml > default
        assert run("get", "session.stall_min")["value"] == 12  # the yaml, not the default 15
        assert run("get", "session.stall_min", "--set", "session.stall_min=20")["value"] == 20
        assert run("get", "session.stall_min", "--set", "session.max_wait_min=45", ok=False)

        # 10. list: every schema row, with its value, default and editability
        fields = run("list", "--json")["fields"]
        keys = [f["key"] for f in fields]
        assert len(keys) == len(set(keys)) and "session.stall_min" in keys
        assert "picks.opencode" in keys and "agents.agy.available" in keys
        assert all({"key", "type", "value", "default", "editable"} <= set(f) for f in fields)
        stall = next(f for f in fields if f["key"] == "session.stall_min")
        assert (stall["value"], stall["default"], stall["min"], stall["max"]) == (12, 15, 5, 60), stall
        agy = next(f for f in fields if f["key"] == "agents.agy.available")
        assert agy["editable"] is False and agy["why"]
        assert [o[0] for o in next(f for f in fields if f["key"] == "session.subagent_model")["opts"]] \
            == ["opencode/space-bunny-free", "opencode/muse-spark-1.3-contributor-free",
                "opencode/big-pickle", "opencode/exo-free", "opencode/fledge-alpha-free"]

        # 11. an unknown key and an unpatchable shape are refused, not guessed at
        run("set", "no.such.key", "1", ok=False)
        with open(yaml_path) as f:
            tampered = f.read().replace("session:\n", "session:\n\tstall_min: 12\n")
        with open(yaml_path, "w") as f:
            f.write(tampered)
        run("set", "session.subagents", "false", ok=False)  # a tab-indented duplicate of the key
        assert rd() == tampered

        # 12. MEDIUM 1: a CRLF file comes back CRLF — one line differs, every \r\n survives
        crlf = fixture.replace("\n", "\r\n")
        with open(yaml_path, "wb") as f:
            f.write(crlf.encode())
        run("set", "session.stall_min", "12")
        got = open(yaml_path, "rb").read().decode()
        assert got.count("\r\n") == crlf.count("\r\n"), "a CRLF terminator was lost"
        assert "\n" not in got.replace("\r\n", ""), "a bare LF appeared in a CRLF file"
        assert diff(crlf, got) == [crlf.splitlines().index(
            "  stall_min: 15   # idle minutes before a lane is called stalled")], diff(crlf, got)
        assert "  stall_min: 12   # idle minutes before a lane is called stalled\r\n" in got
        with open(yaml_path, "wb") as f:
            f.write(fixture.encode())

        # 13. MEDIUM 2: a hand-quoted value with a comment reads as the value, and a set keeps both
        quoted = fixture.replace("  integration_branch: main   #",
                                 '  integration_branch: "main"   #')
        with open(yaml_path, "w") as f:
            f.write(quoted)
        assert run("get", "repo.integration_branch")["value"] == "main", "quotes leaked into the value"
        run("set", "repo.integration_branch", "integration")
        assert '  integration_branch: integration   # lanes merge here; one PR to main at the end\n' \
            in rd().splitlines(keepends=True)
        for tricky in ('"a: b"', '"a#b"', '""', '"007"', '"yes"'):
            run("set", "repo.integration_branch", tricky)
            assert run("get", "repo.integration_branch")["value"] == json.loads(tricky), tricky
        with open(yaml_path, "w") as f:
            f.write(fixture)

        # 14. MEDIUM 3 + LOW 4: .bak is the version the write replaces, made after verify, so a
        # refused set never touches it and two sets leave the FIRST set's result in it
        run("set", "session.stall_min", "22")
        first = rd()
        assert open(yaml_path + ".bak").read() == fixture, "the first .bak is not the replaced version"
        run("set", "session.stall_min", "0", ok=False)
        run("set", "repo.integration_branch", '"a\nb"', ok=False)  # refused inside verify, where the backup used to be
        assert open(yaml_path + ".bak").read() == fixture, "a refused write rewrote .bak"
        run("set", "session.max_wait_min", "45")
        assert open(yaml_path + ".bak").read() == first, ".bak is not one-step undo (previous version)"
        out4 = run("set", "session.max_wait_min", "30")
        assert out4["backup_of"] == "previous" and out4["changed"] is True, out4
        with open(yaml_path, "w") as f:
            f.write(fixture)

        # 15. review (a): a save sends unchanged rows too — an equal value is a no-op, not a refusal
        before, bak = rd(), (yaml_path + ".bak")
        bak_before = open(bak).read() if os.path.exists(bak) else None
        out3 = run("set", "agents.agy.available", "false")
        assert out3["changed"] is False and "backup" not in out3, out3
        assert rd() == before, "a no-op write changed the file"
        assert (open(bak).read() if os.path.exists(bak) else None) == bak_before, \
            "a no-op rewrote .bak"
        assert run("set", "session.stall_min", "12")["changed"] is True
        with open(yaml_path, "w") as f:
            f.write(fixture)

        # 16. MEDIUM 4: a read-only yaml is one JSON error line and a non-zero exit, never a traceback
        if os.geteuid() != 0:  # root writes anything, so the refusal cannot be asserted as root
            with open(yaml_path, "w") as f:
                f.write(fixture)
            os.chmod(yaml_path, 0o444)
            try:
                r = run("set", "session.stall_min", "12", ok=False)
                assert "not writable" in r["error"], r
                assert open(yaml_path).read() == fixture, "a refused write touched the file"
                os.chmod(yaml_path, 0o644)
                run("set", "session.stall_min", "12")
                assert oct(os.stat(yaml_path + ".bak").st_mode)[-3:] == "644", ".bak kept the 0444 mode"
            finally:
                os.chmod(yaml_path, 0o644)
            with open(yaml_path, "w") as f:
                f.write(fixture)

        # 17. MEDIUM 5: a section or key that appears twice is refused with its line numbers — a
        # patch must never edit the block a different parser would not read
        two = fixture.rstrip("\n") + "\nsession:\n  stall_min: 99\n"  # the fixture has no final newline
        with open(yaml_path, "w") as f:
            f.write(two)
        r = run("set", "session.stall_min", "12", ok=False)
        assert "appears 2 times" in r["error"] and str(two.splitlines().index("session:") + 1) in r["error"], r
        assert rd() == two, "a duplicated section was edited anyway"
        dup_key = fixture.replace("  subagents: true\n", "  subagents: true\n  subagents: false\n")
        with open(yaml_path, "w") as f:
            f.write(dup_key)
        r = run("set", "session.subagents", "false", ok=False)
        assert "lines" in r["error"], r
        assert rd() == dup_key

        # 18. review (b): herdr has no models and is not a builder — it never gets a picks row
        with open(yaml_path, "w") as f:
            f.write(fixture)
        keys = [f["key"] for f in run("list", "--json")["fields"]]
        assert "agents.herdr.available" in keys, "herdr availability is a real row"
        assert not any(k.startswith("picks.herdr") for k in keys), keys
        run("migrate")
        migrated = rd().splitlines()
        assert "  herdr: []" not in migrated, "migrate added a herdr picks row"
        for agent in ("codex", "opencode", "agy"):  # exact lines: `codex:[]` is a string to a real parser
            assert f"  {agent}: []" in migrated, f"migrate did not write `{agent}: []`"
        # omp (C1/C3): migrate adds the agent line and lanes.default_agent; every other line stays
        assert "  omp: { available: false, models: [manifest/auto] }" in migrated, migrated
        assert "default_agent: omp" in "\n".join(migrated)
        before_lines = fixture.splitlines()
        # every original line is still present, byte for byte, in order
        kept = [ln for ln in migrated if ln in before_lines or ln in ("lanes:", "  default_agent: omp",
                "  omp: { available: false, models: [manifest/auto] }", "picks:")]
        assert before_lines == [ln for ln in migrated if ln in before_lines], "migrate rewrote an existing line"
        pool = next(f for f in run("list", "--json")["fields"] if f["key"] == "picks.omp")
        assert pool["pool"] == ["manifest/auto"], pool
        assert "ollama-cloud" not in pool["pool"]

        # 19. HIGH 1: every line migrate or set writes is a `key: value` (colon + space) or a bare
        # section header — a missing space parses as a string in a real YAML reader
        for line in migrated:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            assert re.match(r"^\s*[A-Za-z0-9_.-]+:( |$)", line), f"not a yaml key line: {line!r}"
        run("set", "picks.opencode", '["opencode/big-pickle"]')
        run("set", "session.subagent_model", '"opencode/exo-free"')
        for line in rd().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            assert re.match(r"^\s*[A-Za-z0-9_.-]+:( |$)", line), f"not a yaml key line: {line!r}"

        # 20. context subcommand
        with open(yaml_path, "w") as f:
            f.write(fixture)
        ctx = run("context")
        assert ctx["ok"] is True
        assert ctx["policy"] == "claude-sparing"
        assert ctx["builder_skip_permissions"] is True
        assert ctx["launch_flags"]["claude"] == ["--dangerously-skip-permissions"]
        assert ctx["launch_flags"]["codex"] == ["--dangerously-bypass-approvals-and-sandbox"]
        assert ctx["launch_flags"]["opencode"] == []
        assert ctx["launch_flags"]["agy"] == []
        assert ctx["launch_flags"]["omp"] == []
        assert ctx["default_agent"] == "omp"
        assert ctx["agents"]["opencode"]["available"] is True
        assert ctx["agents"]["codex"]["available"] is False
        assert ctx["agents"]["opencode"]["picks"] == []
        # absent yaml key returns schema default
        assert ctx["subagent_model"] == ""
        assert ctx["fresh_session"] == "new"
        assert ctx["reviewer_split"] == "right"
        assert ctx["reuse_reviewer"] is True

        # picks populated per agent
        run("set", "picks.opencode", '["opencode/big-pickle"]')
        ctx2 = run("context")
        assert ctx2["agents"]["opencode"]["picks"] == ["opencode/big-pickle"]

        # launch_flags with skip false
        run("set", "autonomy_contract.builder_skip_permissions", "false")
        ctx3 = run("context")
        assert ctx3["builder_skip_permissions"] is False
        assert ctx3["launch_flags"] == {"claude": [], "codex": [], "opencode": [], "agy": [], "omp": []}
        run("set", "autonomy_contract.builder_skip_permissions", "true")

        # below_floor true / false / null for claude and for one non-Claude agent (opencode)
        usage_fake_path = os.path.join(tmp, "fake_usage.json")
        # null when no usage numbers exist (both five_hour and weekly are None)
        with open(usage_fake_path, "w") as f:
            json.dump({"agents": {
                "claude": {"five_hour": None, "weekly": None},
                "opencode": {},
            }}, f)
        ctx_null = run("context", "--usage", usage_fake_path)
        assert ctx_null["agents"]["claude"]["below_floor"] is None
        assert ctx_null["agents"]["claude"]["left"] == {"five_hour": None, "weekly": None}
        assert ctx_null["agents"]["opencode"]["below_floor"] is None
        assert ctx_null["agents"]["opencode"]["left"] == {"five_hour": None, "weekly": None}

        # false when left >= floor
        # claude floor: 5h=40, week=30. opencode floor: 5h=10, week=15.
        with open(usage_fake_path, "w") as f:
            json.dump({"agents": {
                "claude": {"five_hour": {"used_pct": 20}, "weekly": {"used_pct": 50}},  # 5h left=80, week left=50
                "opencode": {"five_hour": {"used_pct": 50}, "weekly": 70},  # 5h left=50, week left=70
            }}, f)
        ctx_false = run("context", "--usage", usage_fake_path)
        assert ctx_false["agents"]["claude"]["below_floor"] is False
        assert ctx_false["agents"]["claude"]["left"] == {"five_hour": 80, "weekly": 50}
        assert ctx_false["agents"]["opencode"]["below_floor"] is False
        assert ctx_false["agents"]["opencode"]["left"] == {"five_hour": 50, "weekly": 70}

        # true when any existing window left < floor
        with open(usage_fake_path, "w") as f:
            json.dump({"agents": {
                "claude": {"five_hour": {"used_pct": 75}},  # 5h left=25 < 40
                "opencode": {"weekly": {"used_pct": 90}},  # week left=10 < 15
            }}, f)
        ctx_true = run("context", "--usage", usage_fake_path)
        assert ctx_true["agents"]["claude"]["below_floor"] is True
        assert ctx_true["agents"]["claude"]["left"] == {"five_hour": 25, "weekly": None}
        assert ctx_true["agents"]["opencode"]["below_floor"] is True
        assert ctx_true["agents"]["opencode"]["left"] == {"five_hour": None, "weekly": 10}

        # 20b. below_floor is also true while an exhausted_until reset window is still running,
        #      and false again once it is in the past (review r1 LOW: below_floor is not only %)
        future = (datetime.datetime.now(datetime.timezone.utc)
                  + datetime.timedelta(hours=1)).isoformat()
        past = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=1)).isoformat()
        with open(usage_fake_path, "w") as f:
            json.dump({"agents": {
                "claude": {"five_hour": {"used_pct": 10}, "exhausted_until": future},  # 5h left=90
                "opencode": {"weekly": {"used_pct": 10}, "exhausted_until": past},  # week left=90
            }}, f)
        ctx_exh = run("context", "--usage", usage_fake_path)
        assert ctx_exh["agents"]["claude"]["below_floor"] is True, ctx_exh["agents"]["claude"]
        assert ctx_exh["agents"]["claude"]["left"] == {"five_hour": 90, "weekly": None}
        assert ctx_exh["agents"]["opencode"]["below_floor"] is False, ctx_exh["agents"]["opencode"]

        # 21. probe subcommand & helpers rows
        # (a) list emits all five helpers.* rows (not probed when absent)
        with open(yaml_path, "w") as f:
            f.write(fixture)
        list_fields = run("list", "--json")["fields"]
        helper_fields = {f["key"]: f for f in list_fields if f["key"].startswith("helpers.")}
        assert set(helper_fields.keys()) == {
            "helpers.herdr", "helpers.jev", "helpers.context_mode", "helpers.codebase_memory", "helpers.canary"
        }
        for hkey, hf in helper_fields.items():
            assert hf["value"] == "not probed", hf
            assert hf["default"] == "not probed", hf
            assert hf["editable"] is False, hf
            assert hf["scope"] == "about", hf
            assert hf["why"] == "Read-only: probed at preflight", hf

        # (b) set helpers.jev ok is refused
        run("set", "helpers.jev", '"ok"', ok=False)

        # (c) fake herdr, claude, npx scripts on temp PATH giving ok, failing and absent results
        fake_bin = os.path.join(tmp, "bin")
        os.makedirs(fake_bin, exist_ok=True)
        orig_path = env.get("PATH", "")
        env["PATH"] = f"{fake_bin}:/usr/bin:/bin"

        def make_bin(name, content):
            p = os.path.join(fake_bin, name)
            with open(p, "w") as bf:
                bf.write(content)
            os.chmod(p, 0o755)

        def rm_bin(name):
            p = os.path.join(fake_bin, name)
            if os.path.exists(p):
                os.unlink(p)

        make_bin("herdr", "#!/bin/sh\nexit 0\n")
        make_bin("claude", "#!/bin/sh\necho 'jev: connected'\necho 'plugin:context-mode: connected'\necho 'codebase-memory: error'\nexit 0\n")
        make_bin("npx", "#!/bin/sh\nexit 0\n")

        p_res1 = run("probe")
        assert p_res1["ok"] is True
        assert p_res1["herdr"] == "ok"
        assert p_res1["jev"] == "ok"
        assert p_res1["context_mode"] == "ok"
        assert p_res1["codebase_memory"] == "stale"
        assert p_res1["canary"] == "ok"
        assert "probed_at" in p_res1

        # Check values in yaml
        yaml_text1 = rd()
        assert f"helpers: {{ herdr: ok, jev: ok, context_mode: ok, codebase_memory: stale, canary: ok, probed_at: \"{p_res1['probed_at']}\" }}" in yaml_text1.splitlines()

        # (d) probe twice gives identical bytes except probed_at
        time.sleep(0.01)
        p_res2 = run("probe")
        yaml_text2 = rd()
        assert p_res1["probed_at"] != p_res2["probed_at"]
        re_probe = re.compile(r'probed_at: "[^"]*"')
        assert re_probe.sub("probed_at: XXX", yaml_text1) == re_probe.sub("probed_at: XXX", yaml_text2)

        # (e) failing and absent results
        make_bin("herdr", "#!/bin/sh\nexit 1\n")
        rm_bin("claude")
        make_bin("npx", "#!/bin/sh\nexit 1\n")

        p_res3 = run("probe")
        assert p_res3["herdr"] == "stale"
        assert p_res3["jev"] == "missing"  # claude absent: the MCP helpers are missing
        assert p_res3["context_mode"] == "missing"
        assert p_res3["codebase_memory"] == "missing"
        assert p_res3["canary"] == "missing"

        for hid in ("jev", "context_mode", "codebase_memory", "canary"):
            assert run("get", f"helpers.{hid}")["value"] == "missing"
        assert run("get", "helpers.herdr")["value"] == "stale"

        # (f) review r1 HIGH: a block-style helpers section is replaced whole — its indented
        # children must not survive as orphans under the new flow-map line
        block_style = fixture.rstrip("\n") + "\nhelpers:\n  herdr: ok\n  jev: ok\n" \
            "  context_mode: ok\n  codebase_memory: ok\n  canary: ok\n" \
            '  probed_at: "2000-01-01T00:00:00Z"\n'
        with open(yaml_path, "w") as f:
            f.write(block_style)
        make_bin("herdr", "#!/bin/sh\nexit 0\n")
        make_bin("claude", "#!/bin/sh\necho 'jev: connected'\nexit 0\n")
        make_bin("npx", "#!/bin/sh\nexit 0\n")
        p_res4 = run("probe")
        assert p_res4["ok"] is True and p_res4["herdr"] == "ok" and p_res4["canary"] == "ok"
        after_block = rd()
        assert "helpers:\n" not in after_block, f"block header survived:\n{after_block}"
        assert "  herdr: ok\n" not in after_block, f"orphan child survived:\n{after_block}"
        flow_hits = [l for l in after_block.splitlines() if l.startswith("helpers: {")]
        assert len(flow_hits) == 1, after_block
        # every row reads back to what was probed, from the flow line
        for hid in ("herdr", "jev", "context_mode", "codebase_memory", "canary"):
            assert run("get", f"helpers.{hid}")["value"] == p_res4[hid]
        # idempotent on the flow line: probe again -> one flow line, same rows
        p_res5 = run("probe")
        after_block2 = rd()
        re_probe2 = re.compile(r'probed_at: "[^"]*"')
        assert re_probe2.sub("probed_at: X", after_block2) \
            == re_probe2.sub("probed_at: X", after_block)
        # and the file above and below the helpers line did not move
        assert after_block.splitlines()[:fixture.rstrip("\n").count("\n") + 1] \
            == block_style.splitlines()[:fixture.rstrip("\n").count("\n") + 1]

        # (g) verify refuses a corrupt result: a patch that would orphan an indented child under
        # an inline flow-map line fails and writes nothing
        corrupt = fixture.replace("session:\n", "session:\n  stall_min: 15\nsessionx: { a: 1 }\n"
                                        "  orphan: 2\n")
        with open(yaml_path, "w") as f:
            f.write(corrupt)
        r = run("set", "session.stall_min", "20", ok=False)
        assert "corrupt yaml" in r["error"] and "orphan" in r["error"], r
        assert rd() == corrupt, "a corrupt-shaped result was written"
        assert not os.path.exists(yaml_path + ".bak") or open(yaml_path + ".bak").read() != corrupt

        # (h) review r1 MEDIUM: a claude that fails or times out leaves the MCP helpers stale
        # (installed but unverified), never missing; a successful listing without a server's line
        # is missing
        with open(yaml_path, "w") as f:
            f.write(fixture)
        make_bin("claude", "#!/bin/sh\nexit 1\n")
        p_res6 = run("probe")
        assert p_res6["jev"] == "stale" and p_res6["context_mode"] == "stale" \
            and p_res6["codebase_memory"] == "stale", p_res6
        make_bin("claude", "#!/bin/sh\nsleep 45\n")  # longer than the 30 s probe timeout
        t0 = time.time()
        p_res7 = run("probe")
        assert time.time() - t0 < 45, "the 30 s probe timeout did not fire"
        assert p_res7["jev"] == "stale" and p_res7["canary"] in ("ok", "stale"), p_res7
        make_bin("claude", "#!/bin/sh\necho 'something-else: connected'\nexit 0\n")
        p_res8 = run("probe")
        assert p_res8["jev"] == "missing" and p_res8["context_mode"] == "missing" \
            and p_res8["codebase_memory"] == "missing", p_res8
        # canary: failing npx is missing (spec), a timing-out npx is stale
        make_bin("npx", "#!/bin/sh\nexit 1\n")
        p_res9 = run("probe")
        assert p_res9["canary"] == "missing", p_res9
        make_bin("npx", "#!/bin/sh\nsleep 45\n")
        p_res10 = run("probe")
        assert p_res10["canary"] == "stale", p_res10
        # (i) review r2 HIGH: a key whose inline value is a block-scalar indicator owns its
        # deeper-indented lines — set and probe succeed and the block text is byte-identical;
        # a `- item` list under a mapping key stays accepted (the orphan refusal is pinned by (g))
        block_scalar = ("session:\n  stall_min: 15\nnote: |\n  folded text\n"
                        "picks:\n  opencode:\n    - alpha\n    - beta\n"
                        "repo:\n  integration_branch: main\n")
        with open(yaml_path, "w") as f:
            f.write(block_scalar)
        run("set", "session.stall_min", "20")
        after_bs = rd()
        assert "note: |\n  folded text\n" in after_bs, after_bs
        assert "    - alpha\n    - beta\n" in after_bs, after_bs
        assert after_bs.count("  stall_min: 20\n") == 1, after_bs
        make_bin("herdr", "#!/bin/sh\nexit 0\n")
        make_bin("claude", "#!/bin/sh\necho 'jev: connected'\nexit 0\n")
        make_bin("npx", "#!/bin/sh\nexit 0\n")
        p_bs = run("probe")
        assert p_bs["ok"] is True and p_bs["herdr"] == "ok", p_bs
        after_bs2 = rd()
        assert "note: |\n  folded text\n" in after_bs2, after_bs2
        assert "    - alpha\n    - beta\n" in after_bs2, after_bs2
        assert after_bs2.count("  stall_min: 20\n") == 1, after_bs2

        # (j) review r2 MEDIUM: probe's block-style branch keeps trailing blank and comment
        # lines after the helpers section byte-identical
        trailing = ("helpers:\n  herdr: ok\n  jev: ok\n\n# keep\n"
                    "repo:\n  integration_branch: main\n")
        with open(yaml_path, "w") as f:
            f.write(trailing)
        p_tr = run("probe")
        assert p_tr["ok"] is True, p_tr
        after_tr = rd()
        flow_tr = [l for l in after_tr.splitlines() if l.startswith("helpers: {")]
        assert len(flow_tr) == 1, after_tr
        assert "  herdr: ok\n" not in after_tr, f"old child survived:\n{after_tr}"
        assert "\n# keep\n" in after_tr, f"trailing comment lost:\n{after_tr}"
        assert after_tr.endswith("repo:\n  integration_branch: main\n"), after_tr
        assert after_tr.index("\n# keep\n") < after_tr.index("repo:"), after_tr

        # 22. bus.* rows: the whole missing section is appended (the same mechanism as
        #     picks), values round-trip, and a value below the minimum is refused
        with open(yaml_path, "w") as f:
            f.write(fixture)
        before, bak = rd(), yaml_path + ".bak"
        run("set", "bus.enabled", "false")
        added = rd().splitlines()
        assert added[:len(before.splitlines())] == before.splitlines(), "an existing line moved"
        assert added[-2:] == ["bus:", "  enabled: false"], added[-2:]
        assert run("get", "bus.enabled")["value"] is False
        run("set", "bus.owner_timeout_min", "7")  # the block exists now: the key is inserted into it
        assert run("get", "bus.owner_timeout_min")["value"] == 7
        os.unlink(bak)
        before = rd()
        run("set", "bus.owner_timeout_min", "4", ok=False)  # below min 5
        run("set", "bus.ask_timeout_s", "59", ok=False)  # below min 60
        assert rd() == before and not os.path.exists(bak)

        # 23. context carries the bus ask timeout: the schema default, then the set value
        assert run("context")["bus_ask_timeout_s"] == 600
        run("set", "bus.ask_timeout_s", "900")
        assert run("context")["bus_ask_timeout_s"] == 900

        # 25. session.todo_done_status (C9): the schema default, then `done` changes what context
        #     returns, and a value outside verify|done is refused with nothing written
        assert run("get", "session.todo_done_status")["value"] == "verify"
        row = next(f for f in run("list", "--json")["fields"] if f["key"] == "session.todo_done_status")
        assert (row["type"], row["scope"], row["default"], row["label"]) == \
            ("seg", "child", "verify", "Todo completed becomes"), row
        assert [o[0] for o in row["opts"]] == ["verify", "done"], row
        assert run("context")["todo_done_status"] == "verify"
        run("set", "session.todo_done_status", '"done"')
        assert run("context")["todo_done_status"] == "done"
        before, bak = rd(), yaml_path + ".bak"
        os.unlink(bak)
        bad = run("set", "session.todo_done_status", '"merged"', ok=False)
        assert "must be one of" in bad["error"], bad
        assert rd() == before and not os.path.exists(bak)
        with open(yaml_path, "w") as f:
            f.write(fixture)

        # 28. session.selftest_jobs_total: absent yaml derives max(1, round(cores/3)) and says so;
        #     set writes an integer into session:; 0, 17, negatives, floats, strings and bools refuse
        want = max(1, round((os.cpu_count() or 1) / 3))
        got = run("get", "session.selftest_jobs_total")
        assert got["value"] == want and got.get("derived") is True and got.get("default") == want, got
        row = next(f for f in run("list", "--json")["fields"] if f["key"] == "session.selftest_jobs_total")
        assert (row["type"], row["scope"], row["min"], row["max"], row["value"], row["default"],
                row.get("derived"), row["label"]) == \
            ("num", "child", 1, 16, want, want, True, "Self-test jobs"), row
        ctx = run("context")
        assert ctx["selftest_jobs_total"] == want and ctx["selftest_jobs_total_derived"] is True, ctx
        run("set", "session.selftest_jobs_total", "2")
        saved = run("get", "session.selftest_jobs_total")
        assert saved["value"] == 2 and "derived" not in saved, saved
        assert "  selftest_jobs_total: 2\n" in rd()
        ctx = run("context")
        assert ctx["selftest_jobs_total"] == 2 and ctx["selftest_jobs_total_derived"] is False, ctx
        listed = next(f for f in run("list", "--json")["fields"] if f["key"] == "session.selftest_jobs_total")
        assert listed["value"] == 2 and "derived" not in listed and listed["default"] == want, listed
        before, bak = rd(), yaml_path + ".bak"
        if os.path.exists(bak):
            os.unlink(bak)
        for bad_raw in ("0", "17", "-1", "1.5", '"abc"', "true"):
            bad = run("set", "session.selftest_jobs_total", bad_raw, ok=False)
            assert "integer from 1 to 16" in bad["error"], bad
            assert rd() == before and not os.path.exists(bak)
        # a hand-edited float or word is unreadable: the read falls back to the derived default
        with open(yaml_path, "w") as f:
            f.write(fixture + "  selftest_jobs_total: abc\n")
        garbled = run("get", "session.selftest_jobs_total")
        assert garbled["value"] == want and garbled.get("derived") is True, garbled
        with open(yaml_path, "w") as f:
            f.write(fixture)

        # 29. selftest.sh: env wins; else the saved integer; garbage and a missing yaml fall back
        #     to 4. Garbage warns once. A missing yaml is silent. MATRIXKODER_DATA stays inside tmp.
        real_yaml = "/Users/rohits/Development/matrixkoder/.matrixkoder/matrixkoder.yaml"
        prod = os.path.join(tmp, "prod")
        os.makedirs(prod)
        shutil.copy(real_yaml, os.path.join(prod, "matrixkoder.yaml"))
        subprocess.run([sys.executable, script, "--data", prod, "set",
                        "session.selftest_jobs_total", "2"], check=True,
                       capture_output=True, text=True)
        harness = os.path.join(tmp, "harness", "scripts")
        os.makedirs(harness)
        raw_sh = open(os.path.join(HERE, "selftest.sh")).read()
        needle_data = 'cfg=$(python3 "$cfg_py" get session.selftest_jobs_total 2>/dev/null) || cfg=\n'
        repl_data = 'cfg=$(python3 "$cfg_py" --data "$MATRIXKODER_DATA" get session.selftest_jobs_total 2>/dev/null) || cfg=\n'
        if raw_sh.count(needle_data) != 1:
            raise AssertionError("selftest.sh config call changed")
        raw_sh = raw_sh.replace(needle_data, repl_data, 1)
        needle = '  [ -e "$f" ] || continue\n  files+=("$f")\n'
        repl = ('  [ -e "$f" ] || continue\n'
                '  case "$(basename "$f")" in mk-config.py|mk-state.py) continue ;; esac\n'
                '  files+=("$f")\n')
        if raw_sh.count(needle) != 1:
            raise AssertionError("selftest.sh file loop changed; the harness skip no longer matches")
        with open(os.path.join(harness, "selftest.sh"), "w") as f:
            f.write(raw_sh.replace(needle, repl, 1))
        # the lane resolves mk-config.py beside selftest.sh; the copy must be the script under test
        shutil.copy(script, os.path.join(harness, "mk-config.py"))
        shutil.copy(os.path.join(HERE, "mk-state.py"), os.path.join(harness, "mk-state.py"))
        for name in ("w1.py", "w2.py", "w3.py"):
            with open(os.path.join(harness, name), "w") as f:
                f.write(
                    "import os, sys, time\n"
                    "if '--self-test' in sys.argv:\n"
                    "    d = os.environ['MK_JOBS_DIR']\n"
                    "    os.makedirs(d, exist_ok=True)\n"
                    "    open(os.path.join(d, str(os.getpid())), 'w').write(str(time.time()))\n"
                    "    time.sleep(0.5)\n"
                    "    print('ok')\n"
                )

        def trace(data, env_jobs, text=None):
            if text is not None:
                with open(os.path.join(data, "matrixkoder.yaml"), "w") as f:
                    f.write(text)
            stamp = os.path.join(tmp, "trace-%s" % time.time())
            slots = stamp + ".slots"
            env = dict(os.environ, MATRIXKODER_DATA=data, MATRIXKODER_HOME=home,
                       MK_SELFTEST_TRACE=stamp, MK_JOBS_DIR=slots, PYTHONDONTWRITEBYTECODE="1",
                       )
            env.pop("MK_SELFTEST_JOBS", None)
            if env_jobs is not None:
                env["MK_SELFTEST_JOBS"] = env_jobs
            p = subprocess.run(["bash", "selftest.sh"], cwd=harness, env=env,
                               capture_output=True, text=True)
            got = open(stamp).read().strip() if os.path.exists(stamp) else ""
            times = []
            if os.path.isdir(slots):
                for name in os.listdir(slots):
                    try:
                        times.append(float(open(os.path.join(slots, name)).read()))
                    except ValueError:
                        pass
            # how many workers had already started before the first one could finish (0.5s sleep)
            peak = 1
            if times:
                t0 = min(times)
                peak = sum(1 for t in times if t < t0 + 0.35)
            return p, got, peak

        base = open(os.path.join(prod, "matrixkoder.yaml")).read()
        p_env, j_env, peak_env = trace(prod, "1")
        assert p_env.returncode == 0 and j_env == "1" and peak_env == 1, (j_env, peak_env, p_env.stdout, p_env.stderr)
        assert p_env.stderr == "", p_env.stderr
        assert p_env.stdout.strip().splitlines()[-1] == "selftest: 3 run, 0 failed", p_env.stdout
        p_saved, j_saved, peak_saved = trace(prod, None)
        assert p_saved.returncode == 0 and j_saved == "2" and peak_saved == 2 and p_saved.stderr == "", \
            (j_saved, peak_saved, p_saved.stderr, p_saved.stdout[-300:])
        # three workers, jobs=2: the pid files exist, and the run is not serial.
        # Serial would be 3 * 0.5s; two workers finish in about 1s. Bound it loosely.
        p_g, j_g, peak_g = trace(prod, None, base.replace("selftest_jobs_total: 2", "selftest_jobs_total: abc"))
        assert p_g.returncode == 0 and j_g == "4", (j_g, p_g.stderr)
        warns = [ln for ln in p_g.stderr.splitlines() if ln.startswith("warning:")]
        assert len(warns) == 1 and "abc" in warns[0], p_g.stderr
        p_99, j_99, peak_99 = trace(prod, None, base.replace("selftest_jobs_total: 2", "selftest_jobs_total: 99"))
        assert p_99.returncode == 0 and j_99 == "4", (j_99, p_99.stderr)
        assert len([ln for ln in p_99.stderr.splitlines() if ln.startswith("warning:")]) == 1, p_99.stderr
        assert "99" in p_99.stderr, p_99.stderr
        empty = os.path.join(tmp, "empty-data")
        os.makedirs(empty)
        p_e, j_e, peak_e = trace(empty, None)
        cfg_txt = open(p_e.stdout and "" or "").read() if False else ""
        # the trace file's sibling holds the raw config line
        assert p_e.returncode == 0 and j_e == "4" and peak_e == 3 and p_e.stderr == "", (j_e, peak_e, p_e.stderr, p_e.stdout[-200:])
        # restore the fixture yaml the later cases read
        with open(yaml_path, "w") as f:
            f.write(fixture)

        # 26. the reviewer dropdowns offer the catalog's routes, and every offered option passes set
        cat_home = os.path.join(tmp, "catalog-home")
        cat_yaml = os.path.join(tmp, "catalog.yaml")
        with open(cat_yaml, "w") as f:
            f.write("agents:\n  opencode: { available: true, models: [opencode/alpha] }\n"
                    "  claude: { available: true, models: [claude-sonnet] }\n")
        seeded = subprocess.run([sys.executable, os.path.join(HERE, "mk-catalog.py"),
                                 "--home", cat_home, "seed", "--yaml", cat_yaml, "--no-probe"],
                                capture_output=True, text=True)
        assert seeded.returncode == 0, seeded.stderr
        os.environ["MK_HOME"] = cat_home
        os.environ["MATRIXKODER_HOME"] = cat_home
        try:
            live = "opencode opencode/alpha low"  # a route the seeded catalog lists
            run("set", "routing_preferences.reviewer", json.dumps(live))
            run("set", "routing_preferences.visual_reviewer", json.dumps(live))
            fields = {f["key"]: f for f in run("list", "--json")["fields"]}
            for key in ("routing_preferences.reviewer", "routing_preferences.visual_reviewer"):
                offered = [o[0] for o in fields[key]["opts"]]
                assert offered and all(n == "" for (_v, n) in fields[key]["opts"]), fields[key]
                assert "stale" not in fields[key], fields[key]
                for route in offered:  # every option the dropdown shows is one set accepts
                    run("set", key, json.dumps(route))
                    assert run("get", key)["value"] == route
            # a saved value the catalog no longer lists stays visible and is flagged stale
            with open(yaml_path, "w") as f:
                f.write(fixture)  # reviewer is the schema default, not a seeded route
            stale = next(f for f in run("list", "--json")["fields"]
                         if f["key"] == "routing_preferences.reviewer")
            assert stale.get("stale") is True, stale
            assert stale["opts"][-1] == ["claude sonnet medium", "stale"], stale["opts"]
            run("set", "routing_preferences.reviewer", '"claude sonnet medium"')  # kept, so re-saving it passes
        finally:
            del os.environ["MK_HOME"]
            os.environ["MATRIXKODER_HOME"] = home
        with open(yaml_path, "w") as f:
            f.write(fixture)

        # 27. with the catalog absent the dropdown falls back to the current value alone and
        #     the screen still answers — and an empty home stays empty: list and context must
        #     never create <home>/mk.db (review R1 finding 1)
        missing = os.path.join(tmp, "no-catalog")
        os.makedirs(missing)
        os.environ["MK_HOME"] = missing
        os.environ["MATRIXKODER_HOME"] = missing
        try:
            fb = next(f for f in run("list", "--json")["fields"]
                      if f["key"] == "routing_preferences.reviewer")
            assert fb["opts"] == [["claude sonnet medium", ""]], fb
            assert "stale" not in fb, fb
            run("context")
            assert os.listdir(missing) == [], "list or context created a file in an empty home"
            run("set", "routing_preferences.reviewer", '"claude sonnet medium"')
            refused = run("set", "routing_preferences.reviewer", '"opencode opencode/alpha low"', ok=False)
            assert "must be one of" in refused["error"], refused
        finally:
            del os.environ["MK_HOME"]
            os.environ["MATRIXKODER_HOME"] = home
        with open(yaml_path, "w") as f:
            f.write(fixture)

        # 28. one list reads the catalog once: two route rows share one subprocess (review R1 finding 2).
        #     The count is taken inside the child process, where the cache lives.
        os.environ["MK_HOME"] = cat_home
        os.environ["MATRIXKODER_HOME"] = cat_home
        counter = os.path.join(tmp, "route-calls")
        open(counter, "w").write("")
        os.environ["MK_CONFIG_COUNT_ROUTES"] = counter
        try:
            run("list", "--json")
            n = len(open(counter).read())
            assert n <= 1, n
        finally:
            del os.environ["MK_HOME"]
            os.environ["MATRIXKODER_HOME"] = home
            del os.environ["MK_CONFIG_COUNT_ROUTES"]
        with open(yaml_path, "w") as f:
            f.write(fixture)

        # 24. probe refreshes agents.<a>.models from the agent CLIs through the safe patcher, and
        #     every model the refreshed pool lists is one `set picks.<a>` accepts
        with open(yaml_path, "w") as f:
            f.write(fixture)
        make_bin("opencode", "#!/bin/sh\nprintf 'opencode/space-bunny-free\\nollama-cloud/minimax-m3\\n'\n")
        make_bin("agy", "#!/bin/sh\n"
                        "printf 'gemini-3.8-flash-medium\\tGemini 3.8 Flash (Medium)\\n"
                        "gemini-3.1-pro-high\\tGemini 3.1 Pro (High)\\n'\n")  # id<TAB>label, as the real CLI prints

        def agent_lines():
            return [l for l in rd().splitlines()
                    if l.startswith("  opencode: ") or l.startswith("  agy: ")]

        p_m1 = run("probe")
        assert p_m1["models"] == {"opencode": "updated", "agy": "updated"}, p_m1
        assert ("  opencode: { available: true, models: [opencode/space-bunny-free, "
                "ollama-cloud/minimax-m3] }") in agent_lines(), rd()
        assert "  agy: { available: false, models: [gemini-3.8-flash-medium, gemini-3.1-pro-high] }" \
            in agent_lines(), rd()
        pools = {f["key"]: f.get("pool") for f in run("list", "--json")["fields"]
                 if f["key"].startswith("picks.")}
        assert pools["picks.opencode"] == ["opencode/space-bunny-free", "ollama-cloud/minimax-m3"], pools
        assert pools["picks.agy"] == ["gemini-3.8-flash-medium", "gemini-3.1-pro-high"], pools
        # every option a picker can show for these agents is accepted by set
        for pkey, pool in pools.items():
            for mid in pool:
                run("set", pkey, json.dumps([mid]))
        assert run("get", "picks.opencode")["value"] == ["ollama-cloud/minimax-m3"]
        # a model the refreshed pool no longer lists is refused: the tab cannot offer it either
        run("set", "picks.opencode", '["opencode/step-5-preview-free"]', ok=False)
        refused = run("set", "picks.opencode", '["opencode/step-5-preview-free"]', ok=False)
        assert "is not a model of that agent" in refused["error"], refused

        # 29. a saved pick a later probe drops stays visible (flagged unavailable) and does not
        #     refuse a set of the picks that remain
        run("set", "picks.opencode", '["opencode/space-bunny-free", "ollama-cloud/minimax-m3"]')
        make_bin("opencode", "#!/bin/sh\nprintf 'ollama-cloud/minimax-m3\\n'\n")
        p_drop = run("probe")
        assert p_drop["models"]["opencode"] == "updated", p_drop
        field = next(f for f in run("list", "--json")["fields"] if f["key"] == "picks.opencode")
        assert field["value"] == ["opencode/space-bunny-free", "ollama-cloud/minimax-m3"], field
        assert field["unavailable"] == ["opencode/space-bunny-free"], field
        assert field["pool"] == ["ollama-cloud/minimax-m3"], field
        # keeping the dropped pick is allowed; a model that was never in the pool is still refused
        run("set", "picks.opencode", '["opencode/space-bunny-free", "ollama-cloud/minimax-m3"]')
        never = run("set", "picks.opencode", '["opencode/never-existed"]', ok=False)
        run("set", "picks.opencode", '["ollama-cloud/minimax-m3"]')
        assert run("get", "picks.opencode")["value"] == ["ollama-cloud/minimax-m3"]
        assert "is not a model of that agent" in never["error"], never
        make_bin("opencode", "#!/bin/sh\nprintf 'opencode/space-bunny-free\\nollama-cloud/minimax-m3\\n'\n")

        # 30. a line with a TAB yields the id before it; a line without one must be a single
        #     id token. Banners, labels and dashes are skipped.
        make_bin("opencode", "#!/bin/sh\n"
                 "printf 'Fetching available models...\\n"
                 "Models: 3 available\\n"
                 "Models:\\n"
                 "-----\\n"
                 "gemini-3.8-flash-high\\tGemini 3.8 Flash (High)...\\n"
                 "opencode/big-pickle\\n'\n")
        p_ban = run("probe")
        assert p_ban["models"]["opencode"] == "updated", p_ban
        assert ("  opencode: { available: true, models: [gemini-3.8-flash-high, "
                "opencode/big-pickle] }") in agent_lines(), rd()

        # a failing CLI, an absent CLI and one that prints nothing all leave the list as it was
        before_agents = agent_lines()
        make_bin("opencode", "#!/bin/sh\nexit 1\n")
        make_bin("agy", "#!/bin/sh\ntrue\n")
        p_m2 = run("probe")
        assert p_m2["models"] == {"opencode": "unchanged", "agy": "unchanged"}, p_m2
        rm_bin("agy")
        p_m3 = run("probe")
        assert p_m3["models"] == {"opencode": "unchanged", "agy": "unchanged"}, p_m3
        assert agent_lines() == before_agents, "a probe that could not read the models rewrote them"

        # 31. codex is never a builder_order value: refused, yaml unchanged, no .bak
        before, bak = rd(), yaml_path + ".bak"
        if os.path.exists(bak):
            os.unlink(bak)
        refused = run("set", "routing_preferences.builder_order", '["codex"]', ok=False)
        assert "never a builder" in refused["error"], refused
        assert rd() == before and not os.path.exists(bak)
        refused = run("set", "routing_preferences.builder_order", '["opencode", "codex"]', ok=False)
        assert "never a builder" in refused["error"] and rd() == before
        for shaped in ('["CODEX"]', '["Codex"]', '["agy", "codex "]', '[" codex"]'):
            refused = run("set", "routing_preferences.builder_order", shaped, ok=False)
            assert "never a builder" in refused["error"], (shaped, refused)
            assert rd() == before and not os.path.exists(bak)

        # a yaml that already names codex: list and context flag it, and a valid value re-saves
        hand = before.replace("builder_order:", "builder_order: [opencode, codex, agy] #", 1)
        if "builder_order:" not in before:
            hand = before + "\nrouting_preferences:\n  builder_order: [opencode, codex, agy]\n"
        # the fixture's builder_order line is replaced whole so the old value does not remain
        lines_now = before.splitlines(keepends=True)
        rewritten = []
        for ln in lines_now:
            if ln.lstrip().startswith("builder_order:"):
                rewritten.append("  builder_order: [opencode, codex, agy]\n")
            else:
                rewritten.append(ln)
        with open(yaml_path, "w") as f:
            f.write("".join(rewritten))
        listed = next(f for f in run("list", "--json")["fields"]
                      if f["key"] == "routing_preferences.builder_order")
        assert "codex" in listed["value"] and listed.get("unavailable") == ["codex"], listed
        ctx = run("context")
        assert ctx["builder_order_unavailable"] == ["codex"], ctx
        run("set", "routing_preferences.builder_order", '["opencode", "agy"]')
        assert run("get", "routing_preferences.builder_order")["value"] == ["opencode", "agy"]
        with open(yaml_path, "w") as f:
            f.write(before)

        # 32. the lock serialises two real set processes, and a holder that never releases it
        #     makes set return the named error inside a bound scaled from a measured spawn.
        import signal as _signal
        run("set", "session.stall_min", "15")
        before = rd()
        devnull = subprocess.DEVNULL
        t_spawn = time.monotonic()
        warm = subprocess.Popen([sys.executable, "-c", "pass"], stdout=devnull, stderr=devnull)
        warm.wait()
        spawn_s = max(0.5, time.monotonic() - t_spawn)
        procs = [
            subprocess.Popen([sys.executable, script, "set", "session.stall_min", "22"],
                             env=env, cwd=tmp, stdout=devnull, stderr=devnull),
            subprocess.Popen([sys.executable, script, "set", "session.max_wait_min", "40"],
                             env=env, cwd=tmp, stdout=devnull, stderr=devnull),
        ]
        for proc in procs:
            proc.wait(timeout=max(30, LOCK_WAIT_S + spawn_s * 20))
        after = rd()
        assert "stall_min: 22" in after and "max_wait_min: 40" in after, after
        hold = os.path.join(data, "matrixkoder.yaml.lock")
        hf = open(hold, "a")
        fcntl.flock(hf, fcntl.LOCK_EX)
        try:
            t0 = time.monotonic()
            busy = run("set", "session.stall_min", "30", ok=False)
            waited = time.monotonic() - t0
            assert "matrixkoder.yaml.lock busy" in busy["error"], busy
            assert waited < LOCK_WAIT_S + max(2, spawn_s * 10), (waited, spawn_s)
            assert rd() == after
        finally:
            fcntl.flock(hf, fcntl.LOCK_UN)
            hf.close()
        # kill -9 only after the child has the lock, so the assertion cannot pass vacuously
        ready = os.path.join(tmp, "lock-held")
        sleeper = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl,time; f=open(%r,'a'); fcntl.flock(f,fcntl.LOCK_EX); "
             "open(%r,'w').write('held'); time.sleep(60)" % (hold, ready)],
            stdout=devnull, stderr=devnull)
        deadline = time.monotonic() + max(5, spawn_s * 20)
        while not os.path.exists(ready):
            assert time.monotonic() < deadline, "the holder never took the lock"
            assert sleeper.poll() is None, "the holder exited before taking the lock"
            time.sleep(0.05)
        sleeper.send_signal(_signal.SIGKILL)
        sleeper.wait(timeout=max(5, spawn_s * 10))
        run("set", "session.stall_min", "18")
        assert "stall_min: 18" in rd()

        # 33. a probe whose CLI sleeps does not block a concurrent set; both results land
        make_bin("herdr", "#!/bin/sh\nsleep 8\nexit 0\n")
        make_bin("claude", "#!/bin/sh\nexit 1\n")
        make_bin("npx", "#!/bin/sh\nexit 1\n")
        make_bin("opencode", "#!/bin/sh\nexit 1\n")
        make_bin("agy", "#!/bin/sh\nexit 1\n")
        before_set = rd()
        probe_p = subprocess.Popen([sys.executable, script, "probe"], env=env, cwd=tmp,
                                   stdout=devnull, stderr=devnull)
        time.sleep(0.3)  # let the probe enter the slow CLI before set starts
        t0 = time.monotonic()
        set_during = run("set", "session.stall_min", "19")
        set_dt = time.monotonic() - t0
        assert set_during.get("ok") and set_dt < 5, (set_dt, set_during)
        probe_p.wait(timeout=40)
        final = rd()
        assert "stall_min: 19" in final, final
        assert "herdr: stale" in final or "herdr: ok" in final, final
        assert "probed_at:" in final
        # R1.2: the mkdb write guard refuses the real home from this suite too.
        import importlib.util as _iu
        _spec = _iu.spec_from_file_location("mkdb", os.path.join(HERE, "mkdb.py"))
        _mod = _iu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _mod.selftest_guard_probe(home)
    finally:
        if restore:
            restore()
        shutil.rmtree(tmp, ignore_errors=True)
    print("mk-config self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())