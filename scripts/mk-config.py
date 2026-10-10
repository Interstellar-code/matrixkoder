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
"""
import contextlib, difflib, importlib.util, json, os, re, shutil, subprocess, sys, tempfile

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
AGENT_NAMES = {"opencode": "OpenCode", "claude": "Claude Code", "codex": "Codex", "agy": "AGY"}


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
            try:
                return float(s)
            except ValueError:
                return default
    if type == "list":
        t = s.strip()
        if t.startswith("[") and t.endswith("]"):
            return [scalar(x) for x in split_flow(t[1:-1])]
        return default
    return s


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


# ponytail: last-writer-wins, no lock; add mk_state lock if wizard+settings race
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


def verify(old, new, count, checks):
    reread = "".join(new).splitlines(keepends=True)
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
    was = {k: os.environ.get(k) for k in ("MATRIXKODER_DATA", "MATRIXKODER_HOME")}
    os.environ["MATRIXKODER_DATA"], os.environ["MATRIXKODER_HOME"] = data, home
    got = os.path.realpath(mk_state.resolve_data(None))
    assert got.startswith(os.path.realpath(tmp)), \
        f"the self-test would resolve {got}, which is outside its temp dir {tmp}"

    def restore():
        for k, v in was.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
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
    found, current = value_of(lines, r)
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
    old = list(lines)
    new, changed, checks, count = list(lines), [], [], 0
    eol = file_ending(new)
    if not section_range(new, key_lines(new), [SESSION_SECTION]):
        block = [f"{SESSION_SECTION}:{eol}"]
        if new and not new[-1].endswith("\n"):
            block[0] = eol + block[0]
        for r in SCHEMA:
            if r["sec"] == [SESSION_SECTION]:
                block.append(f"  {r['line']}: {emit(r['default'], r['type'])}{eol}")
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
    restore = pin_tmp_data(tmp)  # the in-process calls below must resolve here, not in a real repo
    try:
        data, home, yaml_path = os.path.join(tmp, "data"), os.path.join(tmp, "home"), None
        os.makedirs(data, exist_ok=True)
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
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)
    print("mk-config self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())