#!/usr/bin/env python3
"""MatrixKoder config — one schema, one writer: list, get, set, migrate. Python 3 stdlib only.

  list --json
  get KEY [--set KEY=VALUE]   (--set: the caller's own value, so a draft can be read back)
  set KEY VALUE               VALUE is JSON: 12, true, "main", ["a","b"]
  migrate
  --self-test

Common: --data DIR, resolved exactly as mk-state.py does (--data > $MATRIXKODER_DATA >
<home>/projects.json > <main checkout>/.matrixkoder). Every command prints ONE JSON line, like
mk-state.py. Exit 0 ok; 2 bad input, an unknown key, or a refused write — and a refusal writes
nothing at all, not even a .bak.

Why line-wise: the yaml is hand-edited and every line carries a comment the user wrote. There is no
YAML dependency here, and a real yaml round-trip would rewrite every comment, quote and spacing in
the file on the first write. This reads and patches one line at a time, so one value is a one-line
diff and the comment beside it survives. Only four shapes are ever written (see `patch`): a scalar
line, an insert at the end of an existing block, a whole appended block, and one member of a
one-line flow map. Everything else is refused rather than guessed at — a refusal costs the user a
retry, a wrong write costs them a hand-edited config.

The yaml parser (scalar, split_flow, strip_comment, flow_map, KEY_RE) lives here and mk-lane.py
loads it from this file: one parser for both tools, so a value can never mean two things.
"""
import contextlib, difflib, importlib.util, json, os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True  # no stray __pycache__ next to the scripts

KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*)$")


# ---------- yaml: line-wise, no dependency (moved verbatim from mk-lane.py) ----------

def scalar(v):
    """One YAML scalar: quoted or plain text up to a ` #` comment (hooks/guards.ts scalar())."""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return re.split(r"\s+#", v, maxsplit=1)[0].strip()


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
    _row("repo.integration_branch", "project", "text", "main", ["repo"], "integration_branch", None,
         "Integration branch", "lanes merge here; one PR to main at the end"),
    _row("repo.lanes.slots", "project", "num", 3, ["repo"], "lanes", "slots",
         "Lane slots", "reusable worktree slots", min=1, max=6),
    _row("repo.lanes.reuse", "project", "bool", True, ["repo"], "lanes", "reuse",
         "Reuse slots", "a released slot is reused instead of deleted"),
    _row("repo.lanes.copy", "project", "list", [], ["repo"], "lanes", "copy",
         "Copied paths", "gitignored paths a fresh slot needs"),
    _row("qa.integration_target", "project", "seg", "local-main", ["qa"], "integration_target", None,
         "QA target", "where the merged lanes are QA'd",
         opts=[("local-main", "merge into local main, no push"),
               ("integration", "QA the integration branch")]),
    _row("autonomy_contract.builder_skip_permissions", "project", "bool", True,
         ["autonomy_contract"], "builder_skip_permissions", None,
         "Builders skip permissions", "builders only, in their own worktree"),
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
         "Claude 5h floor", "never route Claude below this share of its 5h window",
         min=0, max=90, unit="%"),
    _row("routing_preferences.reserve_floor.claude.weekly_left_pct", "routing", "num", 30,
         ["routing_preferences", "reserve_floor"], "claude", "weekly_left_pct",
         "Claude weekly floor", "never route Claude below this share of its week",
         min=0, max=90, unit="%"),
    _row("routing_preferences.reserve_floor.others.five_hour_left_pct", "routing", "num", 10,
         ["routing_preferences", "reserve_floor"], "others", "five_hour_left_pct",
         "Others 5h floor", "floor for every non-Claude agent", min=0, max=90, unit="%"),
    _row("routing_preferences.reserve_floor.others.weekly_left_pct", "routing", "num", 15,
         ["routing_preferences", "reserve_floor"], "others", "weekly_left_pct",
         "Others weekly floor", "floor for every non-Claude agent", min=0, max=90, unit="%"),
    _row("routing_preferences.reviewer", "routing", "select", "claude sonnet medium",
         ["routing_preferences"], "reviewer", None,
         "Reviewer", "the route every review goes to"),
    _row("routing_preferences.visual_reviewer", "routing", "select", "claude haiku",
         ["routing_preferences"], "visual_reviewer", None,
         "Visual reviewer", "the route a visual/UI review goes to"),
    _row("usage.source", "project", "seg", "codexbar", ["usage"], "source", None,
         "Usage source", "where every agent's usage is read from",
         opts=[("codexbar", "the CodexBar CLI"), ("manual", "numbers typed into the pane")]),
    _row("usage.providers", "project", "list", [], ["usage"], "providers", None,
         "Usage providers", "providers shown in the usage block"),
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
    _row("session.reuse_reviewer", "child", "bool", True, ["session"], "reuse_reviewer", None,
         "Reuse reviewer", "one reviewer session serves the phase's rounds"),
]

MIGRATED_KEYS = ("routing_preferences.reviewer", "routing_preferences.visual_reviewer")
# Dead yaml copies of plugin settings: reported, never written or deleted (C3 — userConfig is the home).
DEAD = (("dashboard", "auto_open", "autoOpen", True),
        ("dashboard", "chip", "chip", True),
        ("dashboard", "toasts", "toasts", True),
        ("usage", "poll_min", "usagePollMin", 5))
# The schema rows this tool writes as a whole block when the section is missing entirely.
SESSION_SECTION, PICKS_SECTION = "session", "picks"


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
    """The static schema rows plus one `agents.<a>.available` and one `picks.<a>` row per agent in
    the file, so a key that is not a known agent simply does not exist."""
    out = list(SCHEMA)
    for name, m in agents(lines):
        out.append(_row(f"agents.{name}.available", "agents", "bool", bool(m.get("available", False)),
                        ["agents"], name, "available", f"{name} available",
                        "detected at bootstrap, logged in and installed",
                        editable=False, why="detected at bootstrap — not a typed setting"))
        out.append(_row(f"picks.{name}", "models", "list", [], [PICKS_SECTION], name, None,
                        f"{name} model picks", "this agent's models in preference order",
                        maxlen=4, pool=list(m.get("models") or [])))
    return out


def row_for(lines, key):
    for r in rows(lines):
        if r["key"] == key:
            return r
    return None


def opts_of(r, lines):
    """The [[value, note]] option list a UI shows; session.subagent_model's options are the models
    the yaml says this OpenCode build has, so they come from the file and never from a hard-coded list."""
    if r["key"] == "session.subagent_model":
        return [[m, ""] for m in dict(agents(lines)).get("opencode", {}).get("models", [])]
    return [[v, note] for (v, note) in r["opts"]] if r.get("opts") else None


# ---------- reading ----------

def coerce(type, raw, default):
    """A yaml scalar as the schema's type; anything unreadable falls back to the default rather than
    failing a read, so one bad hand-edit never makes the whole config unreadable."""
    if type == "bool":
        s = scalar(raw)
        return {"true": True, "false": False}.get(s.lower(), default)
    if type == "num":
        try:
            return int(scalar(raw))
        except ValueError:
            try:
                return float(scalar(raw))
            except ValueError:
                return default
    if type == "list":
        s = strip_comment(raw).strip()
        if s.startswith("[") and s.endswith("]"):
            return [scalar(x) for x in split_flow(s[1:-1])]
        return default
    return scalar(raw)


def value_of(lines, r):
    """-> (found, value): the yaml's value for a schema row, else the schema default."""
    kl, sr = key_lines(lines), section_range(lines, key_lines(lines), r["sec"])
    if not sr:
        return False, r["default"]
    hits = [i for (i, indent, k, _r) in kl if sr[2] <= i < sr[3] and k == r["line"]]
    if len(hits) != 1:
        return False, r["default"]  # absent, or a duplicate: a read never guesses
    rest = KEY_RE.match(lines[hits[0]]).group(3)
    if r["member"] is None:
        return True, coerce(r["type"], rest, r["default"])
    m = flow_map(rest)
    if r["member"] not in m:
        return False, r["default"]
    got = m[r["member"]]
    # flow_map already gives a nested [ list ] back as a list; everything else is still a scalar
    return True, got if r["type"] == "list" else coerce(r["type"], got, r["default"])


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


def patch(lines, r, value):
    """-> (new lines, how many lines changed). Raises Fail for every shape this patcher will not
    write; the caller has already validated the value, so a Fail here means the file's shape."""
    kl, sr = key_lines(lines), section_range(lines, key_lines(lines), r["sec"])
    text = emit(value, r["type"])
    if not sr:  # shape 3: the whole block is missing — append it
        if len(r["sec"]) != 1:
            raise Fail(f"{r['key']}: {'.'.join(r['sec'])} is missing; a flow-map block is never invented")
        block = [f"{r['sec'][0]}:\n", f"  {r['line']}: {text}\n"]
        if lines and not lines[-1].endswith("\n"):
            block[0] = "\n" + block[0]
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
        new[i] = " " * ind + f"{r['line']}: {{ " + ", ".join(parts) + " }" + tail + "\n"
        return new, 1
    if len(hits) > 1:
        raise Fail(f"{r['key']}: {len(hits)} `{r['line']}:` lines in {'.'.join(r['sec'])}; refusing")
    if not hits:  # shape 2: the block is there, the key is not — insert at the end of the block
        ind, pos = child_indent(lines, sr), sr[3]
        line = " " * ind + f"{r['line']}: {text}\n"
        if pos and not lines[pos - 1].endswith("\n"):
            line = "\n" + line
        return lines[:pos] + [line] + lines[pos:], len(line.splitlines())
    i, ind = hits[0]  # shape 1: one value on one line, the comment kept
    _check_target(lines, i, ind, sr, r)
    _head, tail = split_tail(KEY_RE.match(lines[i]).group(3))
    new = list(lines)
    new[i] = " " * ind + f"{r['line']}: {text}" + tail + "\n"
    return new, 1


# ponytail: last-writer-wins, no lock; add mk_state lock if wizard+settings race
def write_yaml(path, old, new, count, checks):
    """Back up, prove the patched text is the file we meant to write, then replace it atomically.
    A yaml a human edited by hand is the only config there is: a wrong write is silent corruption,
    so nothing is written unless the new text parses back to exactly the values that were asked for
    and differs from the old file only in the lines the patch named."""
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")  # the original bytes, before anything is touched
    verify(old, new, count, checks)
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(new)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; match the rest of <data>
        os.replace(tmp, path)  # readers never see a partial file
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


def verify(old, new, count, checks):
    reread = "".join(new).splitlines(keepends=True)
    if changed_lines(old, new) != count:
        raise Fail(f"the patch would change {changed_lines(old, new)} lines, not the {count} it named")
    for key, value, r in checks:
        found, got = value_of(reread, r)
        if not found or got != value:
            raise Fail(f"{key}: the patched file reads {got!r}, not {value!r}; nothing written")


# ---------- commands ----------

def read_lines(path):
    try:
        with open(path) as f:
            return f.readlines()
    except OSError:
        return []


def out(obj, code=0):
    print(json.dumps(obj))
    return code


def parse_value(raw, r):
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
            raise Fail(f"{r['key']} is a number: {raw} is not one")
        if "min" in r and v < r["min"] or "max" in r and v > r["max"]:
            raise Fail(f"{r['key']} must be between {r.get('min')} and {r.get('max')}: {raw}")
    elif t == "list":
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise Fail(f"{r['key']} is a list of strings: {raw} is not")
        if r.get("maxlen") and len(v) > r["maxlen"]:
            raise Fail(f"{r['key']} takes at most {r['maxlen']} entries: {raw} has {len(v)}")
        for x in v:
            if r.get("pool") and x not in r["pool"]:
                raise Fail(f"{r['key']}: {x} is not a model of that agent")
    else:
        if not isinstance(v, str):
            raise Fail(f"{r['key']} is a string: {raw} is not")
        if r.get("opts") and v not in [o[0] for o in r["opts"]]:
            raise Fail(f"{r['key']} must be one of {', '.join(o[0] for o in r['opts'])}: {raw}")
    return v


def cmd_list(path, lines):
    fields = []
    for r in rows(lines):
        value = value_of(lines, r)[1]
        f = {"key": r["key"], "scope": r["scope"], "label": r["label"], "type": r["type"],
             "value": value, "default": r["default"], "help": r["help"],
             "editable": r.get("editable", True)}
        if r["type"] == "num":
            f["min"], f["max"] = r.get("min"), r.get("max")
            if r.get("unit"):
                f["unit"] = r["unit"]
        opts = opts_of(r, lines)
        if opts is not None:
            f["opts"] = opts
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
            return out({"ok": True, "key": key, "value": parse_value(raw, r)})  # the caller's value wins
    found, value = value_of(lines, r)
    return out({"ok": True, "key": key, "value": value})


def cmd_set(path, lines, args):
    if len(args) < 2:
        raise Fail("set needs KEY and VALUE (JSON)")
    key, raw = args[0], args[1]
    r = row_for(lines, key)
    if not r:
        raise Fail(f"unknown key: {key}")
    value = parse_value(raw, r)
    new, count = patch(lines, r, value)  # refused shapes raise before anything is touched
    write_yaml(path, lines, new, count, [(key, value, r)])
    return out({"ok": True, "key": key, "value": value, "file": path, "backup": path + ".bak"})


def cmd_migrate(path, lines):
    """Bring a config written before this schema up to it, and nothing else: the new sections and
    the two new routing keys get their schema defaults when absent. Idempotent, and it reports the
    dead yaml copies of plugin settings without ever writing or deleting them."""
    old = list(lines)
    new, changed, checks, count = list(lines), [], [], 0
    if not section_range(new, key_lines(new), [SESSION_SECTION]):
        block = [f"{SESSION_SECTION}:\n"]
        if new and not new[-1].endswith("\n"):
            block[0] = "\n" + block[0]
        for r in SCHEMA:
            if r["sec"] == [SESSION_SECTION]:
                block.append(f"  {r['line']}: {emit(r['default'], r['type'])}\n")
        new += block
        changed += [f"{SESSION_SECTION}.{r['line']}" for r in SCHEMA if r["sec"] == [SESSION_SECTION]]
        count += len(block)
    if not section_range(new, key_lines(new), [PICKS_SECTION]):
        block = [f"{PICKS_SECTION}:\n"]
        if new and not new[-1].endswith("\n"):
            block[0] = "\n" + block[0]
        block += [f"  {name}: []\n" for name, _m in agents(old)]
        new += block
        changed += [f"{PICKS_SECTION}.{name}" for name, _m in agents(old)]
        count += len(block)
    for key in MIGRATED_KEYS:
        r = row_for(old, key)
        if not value_of(new, r)[0]:
            patched, n = patch(new, r, r["default"])  # appends routing_preferences when it is gone
            new, count = patched, count + n
            changed.append(key)
            checks.append((key, r["default"], r))
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
        raise Fail("usage: mk-config.py [--data DIR] list --json | get KEY [--set KEY=VALUE] | "
                   "set KEY VALUE | migrate | --self-test")
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
    try:
        data, home, yaml_path = os.path.join(tmp, "data"), os.path.join(tmp, "home"), None
        os.makedirs(data)
        yaml_path = os.path.join(data, "matrixkoder.yaml")
        with open(FIXTURE) as f:
            fixture = f.read()
        env = dict(os.environ, MATRIXKODER_DATA=data, MATRIXKODER_HOME=home,
                   PYTHONDONTWRITEBYTECODE="1")
        script = os.path.join(HERE, "mk-config.py")

        def run(*args, ok=True):
            p = subprocess.run([sys.executable, script, *args], capture_output=True, text=True,
                               env=env, cwd=tmp)
            lines = [x for x in (p.stdout or "").splitlines() if x.strip()]
            assert len(lines) == 1, f"{args}: expected ONE json line, got {p.stdout!r} {p.stderr!r}"
            obj = json.loads(lines[0])
            assert obj.get("ok") is ok, f"{args}: {obj}"
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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("mk-config self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())