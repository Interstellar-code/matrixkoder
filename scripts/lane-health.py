#!/usr/bin/env python3
"""Classify one herdr agent lane: OK | STALL | QUOTA | PROVIDER | BLOCKED | DEV_SERVER | ERROR.

Usage: lane-health.py --agent NAME|PANE --lane DIR [--kind claude|codex|opencode|agy] [--stall-min N]
                      [--since EPOCH] [--prompt-marker TEXT] [--pool NAME] [--ledger PATH]
       lane-health.py --self-test

Prints one JSON line: {"state", "evidence": [...], "agent_status", "last_change_min", "scope": "marker"|"tail"}.
Only pane text produced since dispatch is scanned: the tail after the last occurrence of --prompt-marker
(the dispatch prompt text), else the last 40 lines — old scrollback never re-triggers QUOTA.
On QUOTA it marks the agent exhausted in the usage ledger (default $MATRIXKODER_HOME/usage.json, i.e.
~/.matrixkoder/usage.json) under agents.<kind> (falls back to --agent when no --kind), never touching
the CodexBar windows mk-usage.py writes there. --pool NAME marks one of an OpenCode provider's per-plan
pools instead — agents.<kind>.pools.<pool> — because CodexBar reports no windows for those plans.
The mark is written under an fcntl.flock on <home>/usage.json.lock, shared with mk-usage.py's poll.
herdr may print {"error": ...} and still exit 0, so JSON is parsed, not exit codes.
--stall-min absent: <data>/matrixkoder.yaml session.stall_min, else the mk-config schema default.
"""
import contextlib, datetime, fcntl, importlib.util, json, os, re, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("mk_config", os.path.join(HERE, "mk-config.py"))
mk_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk_config)
_pspec = importlib.util.spec_from_file_location("mkpaths", os.path.join(HERE, "mkpaths.py"))
mkpaths = importlib.util.module_from_spec(_pspec)
_pspec.loader.exec_module(mkpaths)


def cfg(key, flag=None):
    """-> flag > <data>/matrixkoder.yaml > the mk-config schema default. One default, one home:
    a repo that never set the key gets the schema value, not a second literal here."""
    if flag is not None:
        return flag
    try:
        lines = mk_config.read_lines(mkpaths.project_yaml(
            mk_config.mk_state.resolve_data(None)))
    except Exception:  # outside a repo / unreadable data dir: the schema default, never a crash
        lines = []
    return mk_config.value_of(lines, mk_config.row_for(lines, key))[1]

KINDS = ("claude", "codex", "opencode", "agy", "omp")
# A quota hit needs a quota noun: bare "rate limit"/429, "Context limit reached" and a lone "resets in 3s"
# are throttles or compaction, not an exhausted plan. "(Free) usage exceeded" is one (OpenCode's free tier:
# "Free usage exceeded, subscribe to Go"). Reset phrases only feed parse_reset, never trigger.
QUOTA_RE = re.compile(r"\b(?:(?:usage|weekly|week|5-hour|5h|hourly|daily|session|plan|individual)\s+(?:limit|quota)\s+"
                      r"(?:reached|hit|exceeded)|(?:hit|reached)\s+your\s+(?:usage\s+|weekly\s+|5-hour\s+)?limit"
                      r"|quota\s+(?:reached|exceeded)|(?:free\s+)?usage\s+exceeded"
                      r"|reached\s+your\s+pro\s+5-hour\s+limit"
                      # omp banners (dist/cli.js, v18.10.4): the Claude subscription wall it notifies
                      # with ("Claude session limit reached — continuing at low priority until …")
                      # and its account-pool message ("all eligible accounts are quota-exhausted or
                      # temporarily blocked"). Both name a limit or a quota, so they are walls.
                      r"|session\s+limit\s+reached|all\s+eligible\s+accounts\s+are\s+quota-exhausted)", re.I)
# Lines that are code, diffs, grep hits or tool views — an agent editing this very file prints quota words.
# ponytail: heuristic; a real CLI message with a double quote or "==" in it would be ignored
CODE_LINE_RE = re.compile(r"[\"`]|\b(?:assert|def|return|import)\b|re\.compile|==|\w\s*=\s*['(\[]|\(\s*'"
                          r"|^[+-]|^\S+:\d+:|^\s*\d+\s*[│|→:]")
RESET_AT_RE = re.compile(r"reset(?:s)? at (\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)", re.I)
RESET_IN_RE = re.compile(r"(?:resets?|try again|retrying)\s+in\s+((?:\d+\s*[a-z]+[\s,]*(?:and\s+)?)+)", re.I)
RESET_CLOCK_RE = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
DUR_RE = re.compile(r"(\d+)\s*([dhms])", re.I)
WEEKLY_AFTER_H = 6  # no window named in the text: a reset further out than this is weekly, not the 5h one
NAMED_5H_RE = re.compile(r"\b(?:5|five)[\s-]*h(?:ou)?rs?\b|\b5h\b", re.I)
NAMED_WEEK_RE = re.compile(r"\bweek(?:ly)?\b", re.I)
# ponytail: Z.AI prints its reset in Beijing time; add other provider zones here if their resets look shifted
PROVIDER_TZ = datetime.timezone(datetime.timedelta(hours=8))
GUESS_H = 5  # reset unknown → assume the 5h window, flagged exhausted_guess, so the agent is re-tried
# Provider/account failures: the agent is up but every request fails — fail over like QUOTA.
PROVIDER_RE = re.compile(r"(unsupported model|subscription is required|model service is temporarily unavailable|invalid api key|unauthori[sz]ed|insufficient (credit|balance))", re.I)
# omp's own per-status provider errors (dist/cli.js Qku(), the OpenAI Responses API error map):
# 401 "OpenAI API key was rejected (401)" and 402 "OpenAI API billing limit reached (402)" fail
# every request on that account — a provider wall, so the lane fails over like QUOTA.
PROVIDER_OMP_RE = re.compile(r"(openai api key was rejected|api billing limit reached)", re.I)
# Only real command lines or dev-server output — not prose like "do not run pnpm dev".
DEV_RE = re.compile(r"(?m)^\s*(?:\$|>|●\s*Bash\(|Bash\(|Running:?)\s*.{0,40}?\b(?:pnpm|npm|yarn|bun)\s+(?:run\s+)?dev\b"
                    r"|\[hermes-agent\] Exited|ELIFECYCLE|VITE v\d+[.\d]* +ready|Local:\s+https?://localhost", re.I)
TAIL_LINES = 40
POOL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def herdr_json(*args):
    r = subprocess.run(["herdr", *args], capture_output=True, text=True)
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": (r.stdout or r.stderr).strip()[:200]}


def find(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            hit = find(v, key)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find(v, key)
            if hit is not None:
                return hit
    return None


def pane_text(agent, lines=300):
    """Recent pane history. A working full-screen agent (OpenCode, AGY) refuses history reads with
    `agent_not_idle` (alternate-screen history needs scrolling), so read the visible screen instead.
    Returning "" there made the pane look frozen and caused false STALLs (lesson 37)."""
    for args in (["--source", "recent-unwrapped", "--lines", str(lines)], ["--source", "visible"]):
        r = subprocess.run(["herdr", "agent", "read", agent, *args], capture_output=True, text=True)
        if not r.stdout.lstrip().startswith('{"error"'):
            return r.stdout
    return ""


def since_dispatch(text, marker=None):
    """-> (pane text produced after the dispatch prompt, "marker"); without a visible marker the last 40
    lines and "tail"."""
    if marker:
        i = text.rfind(marker.strip())
        if i >= 0:
            return text[i + len(marker.strip()):], "marker"
    return "\n".join(text.splitlines()[-TAIL_LINES:]), "tail"


def quota_hits(text):
    """Quota phrases on lines that are not code / diff / grep output."""
    return [m.group(0) for line in text.splitlines() if not CODE_LINE_RE.search(line)
            for m in QUOTA_RE.finditer(line)]


def last_change_min(lane, since=0.0):
    newest = since  # watch start counts as activity, so a fresh lane is not "stale"
    out = subprocess.run(["git", "-C", lane, "status", "--porcelain"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        p = os.path.join(lane, line[3:].strip().split(" -> ")[-1])
        if os.path.exists(p):
            newest = max(newest, os.path.getmtime(p))
    head = subprocess.run(["git", "-C", lane, "log", "-1", "--format=%ct"], capture_output=True, text=True).stdout.strip()
    if head.isdigit():
        newest = max(newest, float(head))
    return round((time.time() - newest) / 60, 1) if newest else None


# A verdict needs a banner SHAPE, not a keyword in prose or in something the agent ran.
# A quota phrase counts only when it is the main content of the line: the line starts with it
# (after a glyph or whitespace), or the line is one of Claude's own walls ("hit/reached your
# limit", "reached your pro 5-hour limit"), which are prose-shaped and have no marker.
# An error line counts when it starts with "Error:" / "API Error:" / "Upstream request failed:",
# holds "retrying in", or holds an HTTP status inside an API error block.
# What stays undetectable: a real wall the agent quotes mid-sentence ("the usage limit reached
# branch is covered"), a line that STARTS with "Error:" but is prose ("Error: unauthorized
# handling is now fixed"), a wall whose lead word is not "Claude" ("Claude Code: usage limit
# reached" — _QUOTA_LEAD accepts only that one lead word), and a real banner whose line also
# holds a double quote (CODE_LINE_RE). A mid-line "hit your limit" is NOT one of these: it is
# anchored to the line start, so prose no longer reads as a wall.
_QUOTA_LEAD = r"(?:(?:usage|weekly|week|5-hour|5h|hourly|daily|session|plan|individual)\s+(?:limit|quota)\s+(?:reached|hit|exceeded)|quota\s+(?:reached|exceeded)|(?:free\s+)?usage\s+exceeded|all\s+eligible\s+accounts\s+are\s+quota-exhausted)"
_QUOTA_FREE = r"^\W*(?:you(?:'ve|\s+have)?\s+)?(?:hit|reached)\s+your\s+(?:usage\s+|weekly\s+|5-hour\s+|pro\s+5-hour\s+)?limit"
BANNER_RE = re.compile(
    r"(?i)(?:"
    r"^\W*(?:Claude\s+)?\W*" + _QUOTA_LEAD + r"|"
    + _QUOTA_FREE + r"|"
    r"^\W*(?:error|api\s+error|upstream\s+request\s+failed)\s*:"
    r"|\bretrying\s+in\b"
    r"|\bapi\s+error\b.*\b[45]\d\d\b"
    r")")
# A command block is tool output only when it contains a shell prompt. herdr draws BOTH the
# message column and command output with a bar, so a bar alone is not tool output (a real banner
# can sit inside one). The bar comes BEFORE the `$`, so the prompt is matched after non-word
# characters: `  ┃  $ pytest`.
CMD_BLOCK_RE = re.compile(r"(?m)^\W*\$\s")
# Lines that are code, diffs, file views or tables, plus test-runner result lines. A real banner
# never starts with `+`, a line number, a table cell, or `FAIL`/`PASS`/`ok <n>`.
# `Error: 4xx` is suppressed even bare: a harness that prints `Error: 403 Forbidden` with no
# "API" in front is treated as a raw test log. Accepted trade-off; no real pane has shown it.
TOOL_LINE_RE = re.compile(
    r"(?:" + CODE_LINE_RE.pattern + r")"
    r"|^\s*\|"                              # markdown table row
    r"|^(?:FAIL|PASS)\s+\S"                 # test-runner result
    r"|^(?:ok|not ok)\s+\d+"                # TAP
    r"|^\s*(?:AssertionError|Traceback)\b"  # a test failure, not a harness banner
    r"|^\W*Error:\s*[45]\d\d\b")         # a raw HTTP log line, bar or not
# Limit: a banner the agent quotes inside a fenced code block, or a real error whose line also
# contains a double quote, is dropped (CODE_LINE_RE). A `$ ` prompt inside a message column
# marks that whole bar-block as tool output.


def _tool_block(lines, i):
    """True when line i sits in a bar block that contains a shell prompt — a command the agent ran.
    The prompt may follow the bar (`  ┃  $ pytest`), so CMD_BLOCK_RE allows non-word characters first."""
    if not re.search(r"[\u2503\u2502]", lines[i]):
        return False
    lo = i
    while lo > 0 and re.search(r"[\u2503\u2502]", lines[lo - 1]):
        lo -= 1
    hi = i
    while hi + 1 < len(lines) and re.search(r"[\u2503\u2502]", lines[hi + 1]):
        hi += 1
    return any(CMD_BLOCK_RE.search(l) for l in lines[lo:hi + 1])


def banner_lines(text):
    """Lines carrying an error-banner shape, from the last TAIL_LINES lines.

    Dropped: a line inside a command block (a bar-drawn run of lines holding a `$ ` prompt, the
    bar coming before the `$`), a code / diff / file-view / table / test-runner line, and a raw
    `Error: 401 ...` log line. A banner drawn inside a bar with no prompt still counts. A quota
    phrase buried in a sentence does not, because the phrase has to lead the line."""
    lines = text.splitlines()[-TAIL_LINES:]
    out = []
    for i, ln in enumerate(lines):
        # a JSON error object is the harness's own line, even though it holds quotes; so is one of
        # omp's own per-status provider errors, which lead with the provider and carry "(401)"
        if re.match(r'\s*\{\s*"error"\s*:', ln) and PROVIDER_RE.search(ln):
            out.append(ln)
            continue
        if PROVIDER_OMP_RE.search(ln) and not _tool_block(lines, i) and not TOOL_LINE_RE.search(ln) \
                and not CODE_LINE_RE.search(ln):  # a diff/code/grep line quoting the banner is not one
            out.append(ln)
            continue
        if _tool_block(lines, i) or TOOL_LINE_RE.search(ln):
            continue
        if BANNER_RE.search(ln):
            out.append(ln)
    return out


def classify(text, status, idle_min, stall_min):
    """text must already be scoped with since_dispatch()."""
    tail = text[-4000:]
    # QUOTA and PROVIDER come only from the agent's own error banner, never from tool output.
    banner = "\n".join(banner_lines(tail))
    hits = quota_hits(banner)
    if hits:
        return "QUOTA", hits[-2:]
    prov = PROVIDER_RE.search(banner) or PROVIDER_OMP_RE.search(banner)
    if prov:
        return "PROVIDER", [prov.group(0)]
    if status == "blocked":
        return "BLOCKED", ["agent_status=blocked (approval/permission dialog?)"]
    dev = DEV_RE.search(tail)
    if dev:
        return "DEV_SERVER", [dev.group(0)]
    if status == "working" and idle_min is not None and idle_min >= stall_min:
        return "STALL", [f"working but no file/commit change for {idle_min} min"]
    return "OK", []


def named_window(text):
    """The window the limit message names ("5 hour", "5-hour", "5h", "weekly", "week"), from the last limit
    line (plus the line after it), or None. A named window beats the reset distance (lesson 38)."""
    lines = text.splitlines()
    hits = [i for i, l in enumerate(lines) if not CODE_LINE_RE.search(l) and QUOTA_RE.search(l)]
    scope = " ".join(lines[hits[-1]:hits[-1] + 2]) if hits else text
    if NAMED_WEEK_RE.search(scope):
        return "weekly"
    return "5h" if NAMED_5H_RE.search(scope) else None


def parse_reset(text, now=None):
    """-> (exhausted_until ISO or None, window "5h" | "weekly" | None) from limit text.
    Understands "reset at 2026-10-07 10:02", "resets in 154h43m / 1h 30m / 45m / 2d 3h / 2 hours",
    "try again in ...", "retrying in 23m 36s" (OpenCode free tier) and a bare clock time
    "resets 3pm" / "resets at 15:30" (next occurrence)."""
    now = now or datetime.datetime.now().astimezone()
    until, named = None, named_window(text)
    m = RESET_AT_RE.search(text)
    if m:  # most CLIs print local wall time
        naive = datetime.datetime.fromisoformat(m.group(1).replace(" ", "T"))
        until = naive.astimezone() if now.tzinfo is None else naive.replace(tzinfo=now.tzinfo)
        five = datetime.timedelta(hours=5, minutes=5)
        if named == "5h" and until - now > five:  # a "5 hour" reset can't be further out: provider's own zone
            alt = naive.replace(tzinfo=PROVIDER_TZ)
            if now < alt <= now + five:
                until = alt.astimezone(now.tzinfo)
            else:
                return None, "5h"  # zone unknown: caller guesses now + 5h, flagged exhausted_guess
    if until is None:
        m = RESET_IN_RE.search(text)
        mins = sum(int(n) * {"d": 1440, "h": 60, "m": 1, "s": 1 / 60}[u.lower()] for n, u in DUR_RE.findall(m.group(1))) if m else 0
        if mins:
            until = now + datetime.timedelta(minutes=mins)
    if until is None:
        m = RESET_CLOCK_RE.search(text)
        if m and (m.group(2) or m.group(3)):
            h = int(m.group(1)) % 12 + (12 if (m.group(3) or "").lower() == "pm" else 0) if m.group(3) else int(m.group(1))
            if h < 24 and int(m.group(2) or 0) < 60:
                until = now.replace(hour=h, minute=int(m.group(2) or 0), second=0, microsecond=0)
                if until <= now:
                    until += datetime.timedelta(days=1)
    if until is not None:
        window = named or ("weekly" if until - now > datetime.timedelta(hours=WEEKLY_AFTER_H) else "5h")
        return until.isoformat(timespec="seconds"), window
    return None, named


def record_quota(ledger, key, text, now=None, pool=None):
    """Mark agents.<key> exhausted — under agents.<key>.pools.<pool> when `pool` names one of an
    OpenCode provider's per-plan pools, else on the agent itself. Writes only the exhausted_* fields;
    CodexBar windows stay as they are."""
    if pool is not None and not POOL_RE.fullmatch(pool):
        raise ValueError(f"bad --pool {pool!r} (letters, digits, . _ -; max 64)")
    with usage_lock():
        try:
            with open(ledger) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}
        now = now or datetime.datetime.now().astimezone()
        until, window = parse_reset(text, now)
        entry = data.setdefault("agents", {}).setdefault(key, {})
        if pool:
            entry = entry.setdefault("pools", {}).setdefault(pool, {})
        entry.setdefault("source", "pane-text")  # an existing CodexBar entry keeps its own source
        guess = until is None
        if guess:
            prev = entry.get("exhausted_until")
            try:
                prev_future = prev and datetime.datetime.fromisoformat(prev) > now
            except (TypeError, ValueError):
                prev_future = False
            if prev_future:  # a known future reset beats a guess
                until, window, guess = prev, entry.get("exhausted_window"), entry.get("exhausted_guess", False)
            else:
                until = (now + datetime.timedelta(hours=GUESS_H)).isoformat(timespec="seconds")
        entry.update({"exhausted_until": until, "exhausted_window": window, "exhausted_guess": guess,
                      "exhausted_source": "pane-text", "exhausted_observed_at": now.isoformat(timespec="seconds"),
                      "exhausted_evidence": quota_hits(text)[-2:]})
        d = os.path.dirname(ledger) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".usage-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, ledger)
    return entry


def _mk_home():
    """One MK home rule (C16): $MK_HOME and $MATRIXKODER_HOME are the same setting; when both
    are set they must agree. The rule itself is scripts/mkpaths.home (C1); this copy raised a
    plain RuntimeError and keeps raising one for its callers."""
    try:
        return mkpaths.home()
    except mkpaths.Fail as e:
        raise RuntimeError(str(e))


def default_ledger():
    return mkpaths.usage_ledger()


@contextlib.contextmanager
def usage_lock():
    """# ponytail: the one lock both writers of usage.json take — mk-usage.py's poll and this quota
    mark. Without it a poll read the file, and its atomic replace then dropped a fresh mark.
    Fixed under <home>, not beside the ledger, so the two serialise even with different --out paths."""
    path = mkpaths.usage_lock()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def check(agent, lane, kind=None, stall_min=10.0, since=0.0, marker=None, ledger=None, pool=None):
    """One health probe. Returns the result dict plus "text" (pane text since dispatch) for lane-watch."""
    got = herdr_json("agent", "get", agent)
    if "error" in got:
        return {"state": "ERROR", "evidence": [str(got["error"])[:200]], "text": ""}
    status = find(got, "agent_status")
    text, scope = since_dispatch(pane_text(agent), marker)
    idle = last_change_min(lane, since)
    state, ev = classify(text, status, idle, stall_min)
    if state == "QUOTA":
        record_quota(ledger or default_ledger(), kind or agent, text, pool=pool)
    return {"state": state, "evidence": ev, "agent_status": status, "last_change_min": idle, "scope": scope,
            "text": text}


def self_test():
    import shutil
    # every ledger path here is a temp file, but usage_lock() resolves <home> independently of --ledger:
    # pin it before the first write so a run can never create a lock in the real ~/.matrixkoder
    mk_home = tempfile.mkdtemp(prefix="lane-health-home-")
    os.environ["MK_HOME"] = mk_home
    os.environ["MATRIXKODER_HOME"] = mk_home
    os.environ["MK_SELFTEST"] = "1"
    tmp_data = tempfile.mkdtemp(prefix="lane-health-data-")
    restore_data = mk_config.pin_tmp_data(tmp_data)  # cfg() resolves the data dir: keep it in temp
    try:
        self_test_body(shutil)
    finally:
        restore_data()
        shutil.rmtree(tmp_data, ignore_errors=True)
        shutil.rmtree(mk_home, ignore_errors=True)


def self_test_body(shutil):
    # no flag and no yaml: the stall default this script now uses IS the mk-config schema's
    empty = tempfile.mkdtemp(prefix="lane-health-cfg-")
    old_md = os.environ.get("MATRIXKODER_DATA")
    os.environ["MATRIXKODER_DATA"] = empty
    try:
        assert cfg("session.stall_min") == mk_config.value_of(
            [], mk_config.row_for([], "session.stall_min"))[1], cfg("session.stall_min")
        assert cfg("session.stall_min", "9") == "9"  # a given flag wins
        assert "omp" in KINDS  # the pi agent (omp) is a lane kind: lane-watch and arm accept --kind omp
    finally:
        if old_md is None:
            os.environ.pop("MATRIXKODER_DATA", None)
        else:
            os.environ["MATRIXKODER_DATA"] = old_md
        shutil.rmtree(empty, ignore_errors=True)
    assert classify("Usage limit reached for 5 hour. Your limit will reset at 2026-10-07 10:02:14", "idle", 1, 10)[0] == "QUOTA"
    assert classify("⚠ Individual quota reached. Resets in 154h43m", "idle", 1, 10)[0] == "QUOTA"
    assert classify("all good", "blocked", 1, 10)[0] == "BLOCKED"
    assert classify("[hermes-agent] Exited with code 1", "idle", 1, 10)[0] == "DEV_SERVER"
    assert classify("Do not run pnpm dev, a build, or any server", "working", 1, 10)[0] == "OK"
    assert classify("  $ pnpm dev", "working", 1, 10)[0] == "DEV_SERVER"
    assert classify("thinking", "working", 12, 10)[0] == "STALL"
    assert classify("thinking", "working", 3, 10)[0] == "OK"

    # A1: words inside tool output never raise a verdict while the agent is working.
    # The ┃ block is how a real herdr pane draws command output (scripts/fixtures/pane-working.txt).
    tool = "\n".join([
        "running the suite",
        "  \u2503",
        "  \u2503  FAIL test_unauthorized_401",
        "  \u2503  AssertionError: 403 quota rate limit",
        "  \u2503",
    ])
    assert classify(tool, "working", 1, 10)[0] == "OK", classify(tool, "working", 1, 10)
    # a banner of the agent's own, then tool output below it, still matches
    banner_then_tool = "Error: subscription is required\n" + tool
    assert classify(banner_then_tool, "working", 1, 10)[0] == "PROVIDER"
    # a banner scrolled above the last N lines does not
    scrolled = "Error: unauthorized\n" + "\n".join(["working"] * (TAIL_LINES + 5))
    assert classify(scrolled, "working", 1, 10)[0] == "OK", classify(scrolled, "working", 1, 10)

    # A1 R2: every quota shape is a banner shape, including Claude's own wall
    for msg in ("You've hit your limit · resets 3pm", "You have reached your limit",
                "ERROR: Usage limit reached resets in 3h",
                "┃  Free usage exceeded, subscribe to Go [retrying in 23m]",
                "│ hit your limit │"):
        assert classify(msg, "idle", 1, 10)[0] == "QUOTA", msg
    assert classify("┃  Upstream request failed: An active OpenCode Go subscription is required",
                    "working", 1, 10)[0] == "PROVIDER"
    # prose, and tool output that is not drawn with a bar, never raise a verdict
    for msg in ("So I hit your limit detection fix and moved on",
                "I think you hit your limit, so wait",
                "The agent says it hit your limit handling is done",
                "I handled unauthorized and 401 in the test",
                "+    x = 'Error: unauthorized'",
                "   12→ Error: subscription is required",
                "| a | unauthorized |",
                "Error: 401 Unauthorized"):
        assert classify(msg, "working", 1, 10)[0] == "OK", (msg, classify(msg, "working", 1, 10))
    # a real sanitized pane whose tool output carries the trigger words: no verdict
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("pane-working.txt", "pane-live-1.txt", "pane-live-2.txt"):
        fx = os.path.join(here, "fixtures", name)
        assert classify(open(fx).read(), "working", 1, 10)[0] == "OK", fx

    # A1 R3: a banner-shaped line inside a bar-drawn command (`  ┃  $ ...`) is tool output.
    # These fail when _tool_block is stubbed to return False.
    for block in ("  \u2503  $ npm test\n  \u2503  Error: unauthorized\n  \u2503",
                  "  \u2503  $ curl x\n  \u2503  Error: 401 Unauthorized\n  \u2503",
                  "  \u2503  $ cat notes\n  \u2503  Upstream request failed: foo unauthorized\n  \u2503",
                  "  \u2503  $ pytest\n  \u2503  Usage limit reached for 5 hour resets in 3h\n  \u2503"):
        assert classify(block, "working", 1, 10)[0] == "OK", (block, classify(block, "working", 1, 10))
    # a quota phrase buried in a sentence is prose, not a wall
    for msg in ("The quota exceeded handling is done; 5-hour limit",
                "I fixed the usage limit reached detection"):
        assert classify(msg, "working", 1, 10)[0] == "OK", msg
    m = RESET_AT_RE.search("will reset at 2026-10-07 10:02:14")
    assert m and m.group(1) == "2026-10-07 10:02:14"
    assert classify("Upstream request failed: An active OpenCode Go subscription is required", "working", 1, 10)[0] == "PROVIDER"
    assert classify('{"error":"unsupported model: gpt-6.1-sol"}', "done", 1, 10)[0] == "PROVIDER"

    # real quota wording from the CLIs we drive
    for msg in ("■ You've hit your usage limit. Upgrade to Pro or try again in 2 hours 13 minutes.",
                "Claude usage limit reached. Your limit will reset at 3pm.",
                "5-hour limit reached ∙ resets 3pm", "Weekly limit reached · resets Oct 13, 9am",
                "Quota exceeded for model gemini-pro"):
        assert classify(msg, "idle", 1, 10)[0] == "QUOTA", msg
    # O2.2: omp's own banners, quoted from @oh-my-pi/pi-coding-agent v18.10.4 dist/cli.js —
    # the Claude subscription wall it notifies with, and its exhausted account pool.
    for msg in ("Claude session limit reached — continuing at low priority until 4pm. "
                "Your weekly limit still applies",
                "all eligible accounts are quota-exhausted or temporarily blocked"):
        assert classify(msg, "idle", 1, 10)[0] == "QUOTA", msg
    # its per-status provider errors (Qku()): the account serves nothing, so PROVIDER
    for msg in ("OpenAI API key was rejected (401)", "OpenAI API billing limit reached (402)"):
        assert classify(msg, "working", 1, 10)[0] == "PROVIDER", msg

    # false positives (each matched the r1 regex): throttles, compaction, code / grep / diff text
    for msg in ("API Error: 429 rate limit exceeded, retrying in 5s", "Rate limit hit, backing off",
                "Context limit reached, compacting conversation", "API Error 429 Too Many Requests, resets in 3s",
                '    assert classify("Usage limit reached for 5 hour", "idle", 1, 10)[0] == "QUOTA"',
                "scripts/lane-health.py:20:QUOTA_RE = re.compile(r'(usage limit|limit reached|quota exceeded)')",
                "+    msg = 'Individual quota reached. Resets in 154h43m'",
                "    171→    weekly usage limit reached handling", "    if text == 'usage limit reached':",
                "+    msg = 'OpenAI API key was rejected (401)'",
                "scripts/lane-health.py:69:# 401 \"OpenAI API key was rejected (401)\" fail",
                '    assert x == "OpenAI API billing limit reached (402)"'):
        assert classify(msg, "working", 1, 10)[0] == "OK", msg

    # only text since dispatch counts: old scrollback above the prompt marker is ignored
    pane = "Usage limit reached. resets in 3h\n" + "old\n" * 5 + "> Read and follow brief-r2.md exactly.\nworking on it\n"
    txt, scope = since_dispatch(pane, "Read and follow brief-r2.md")
    assert txt.strip().startswith("exactly.") and scope == "marker"
    assert classify(txt, "working", 1, 10)[0] == "OK"
    txt, scope = since_dispatch(pane, "marker not on screen")
    assert scope == "tail" and classify(txt, "working", 1, 10)[0] == "QUOTA"  # in last 40 lines
    old = "Usage limit reached. resets in 3h\n" + "noise\n" * 60
    assert classify(since_dispatch(old)[0], "working", 1, 10)[0] == "OK"  # scrolled past the 40-line tail

    # window kind by reset distance
    now = datetime.datetime(2026, 10, 7, 12, 0).astimezone()
    assert parse_reset("Resets in 154h43m", now)[1] == "weekly"
    until, win = parse_reset("limit reached, resets in 2h30m", now)
    assert win == "5h" and until == (now + datetime.timedelta(hours=2, minutes=30)).isoformat(timespec="seconds")
    assert parse_reset("will reset at 2026-10-07 15:00:00", now)[1] == "5h"
    assert parse_reset("will reset at 2026-10-13 09:00", now)[1] == "weekly"
    assert parse_reset("weekly usage limit reached", now) == (None, "weekly")
    assert parse_reset("usage limit reached", now) == (None, None)
    # a named window wins over the distance; a "5 hour" reset printed in Beijing time is converted (lesson 38)
    zai = "Usage limit reached for 5 hour. Your limit will reset at 2026-10-08 22:16:57"
    zn = datetime.datetime(2026, 10, 8, 11, 50, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
    assert parse_reset(zai, zn) == ("2026-10-08T16:16:57+02:00", "5h"), parse_reset(zai, zn)
    assert parse_reset("Usage limit reached for 5 hour. Your limit will reset at 2026-10-09 09:00:00", zn) == (None, "5h")
    assert parse_reset("Weekly usage limit reached. Resets in 3h", zn)[1] == "weekly"
    assert parse_reset("5-hour limit reached, resets in 7h", zn)[1] == "5h"
    tmp_l = tempfile.mkdtemp(prefix="lane-health-zai-")
    try:
        led = os.path.join(tmp_l, "usage.json")
        e = record_quota(led, "opencode", "> brief\n" + zai, zn)
        assert e["exhausted_window"] == "5h" and e["exhausted_until"] == "2026-10-08T16:16:57+02:00" and not e["exhausted_guess"], e
    finally:
        shutil.rmtree(tmp_l, ignore_errors=True)
    later = lambda **kw: (now + datetime.timedelta(**kw)).isoformat(timespec="seconds")
    assert parse_reset("limit reached, resets in 45m", now) == (later(minutes=45), "5h")
    assert parse_reset("resets in 1h 30m", now) == (later(minutes=90), "5h")
    assert parse_reset("resets in 2d 3h", now) == (later(hours=51), "weekly")
    assert parse_reset("try again in 2 hours 13 minutes", now) == (later(minutes=133), "5h")
    assert parse_reset("5-hour limit reached ∙ resets 3pm", now) == (later(hours=3), "5h")
    assert parse_reset("resets at 9am", now) == (later(hours=21), "weekly")  # 9am already passed → tomorrow
    assert parse_reset("resets at 15:30", now) == (later(hours=3, minutes=30), "5h")
    assert parse_reset("resets 5 times a day", now) == (None, None)

    # F1: OpenCode free tier — the exact pane line seen tonight, retry delay kept (minutes+seconds)
    opencode_line = "Free usage exceeded, subscribe to Go [retrying in 23m 36s attempt #1]"
    assert classify(opencode_line, "idle", 1, 10)[0] == "QUOTA", opencode_line
    assert parse_reset(opencode_line, now) == (later(minutes=23, seconds=36), "5h"), parse_reset(opencode_line, now)
    assert parse_reset("Free usage exceeded, subscribe to Go [retrying in 23m attempt #1]", now) == (later(minutes=23), "5h")

    # ledger keyed by kind, CodexBar windows preserved
    import shutil
    tmp = tempfile.mkdtemp(prefix="lane-health-test-")
    try:
        ledger = os.path.join(tmp, "usage.json")
        codexbar = {"source": "codexbar", "status": "ok",
                    "windows": [{"kind": "5h", "used_pct": 40, "resets_at": "2026-10-07T15:00:00Z"}]}
        json.dump({"updated": "x", "agents": {"agy": codexbar}}, open(ledger, "w"))
        record_quota(ledger, "agy", "⚠ Individual quota reached. Resets in 154h43m", now)
        d = json.load(open(ledger))
        agy = d["agents"]["agy"]
        assert agy["windows"] == codexbar["windows"] and agy["source"] == "codexbar" and d["updated"] == "x"
        assert agy["exhausted_window"] == "weekly" and agy["exhausted_source"] == "pane-text"
        assert agy["exhausted_until"] == (now + datetime.timedelta(hours=154, minutes=43)).isoformat(timespec="seconds")
        assert agy["exhausted_guess"] is False
        # a later hit without reset text keeps the known future reset
        record_quota(ledger, "agy", "Individual quota reached", now + datetime.timedelta(hours=1))
        agy2 = json.load(open(ledger))["agents"]["agy"]
        assert agy2["exhausted_until"] == agy["exhausted_until"] and agy2["exhausted_guess"] is False
        # unknown reset → guessed +5h (never null forever), keyed by pane name when no --kind
        record_quota(ledger, "w2-codex-pane", "usage limit reached", now)
        pane_e = json.load(open(ledger))["agents"]["w2-codex-pane"]
        assert pane_e["source"] == "pane-text" and pane_e["exhausted_guess"] is True
        assert pane_e["exhausted_until"] == later(hours=GUESS_H)
        assert os.listdir(tmp) == ["usage.json"] and os.stat(ledger).st_mode & 0o777 == 0o644
        os.environ["MATRIXKODER_HOME"] = tmp
        os.environ["MK_HOME"] = tmp
        assert default_ledger() == ledger
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --pool marks the OpenCode plan, not the agent: CodexBar reports no windows for those providers,
    # so the agent-level entry must stay clean while the pool carries the mark
    tmp_pool = tempfile.mkdtemp(prefix="lane-health-pool-")
    try:
        pled = os.path.join(tmp_pool, "usage.json")
        json.dump({"agents": {"opencode": {"status": "ok", "source": "codexbar",
                                           "pools": {"zai": {"five_hour": None, "weekly": None,
                                                             "exhausted_until": None,
                                                             "source": "codexbar"}}}}},
                  open(pled, "w"))
        e = record_quota(pled, "opencode", "> brief\n" + zai, zn, pool="zai")
        assert e["exhausted_window"] == "5h" and e["exhausted_source"] == "pane-text", e
        assert e["exhausted_until"] == "2026-10-08T16:16:57+02:00" and not e["exhausted_guess"], e
        oc = json.load(open(pled))["agents"]["opencode"]
        assert "exhausted_until" not in oc and "exhausted_source" not in oc, oc  # agent level untouched
        zai_pool = oc["pools"]["zai"]
        assert zai_pool["source"] == "codexbar" and zai_pool["exhausted_until"] == "2026-10-08T16:16:57+02:00"
        assert zai_pool["exhausted_evidence"] and zai_pool["exhausted_window"] == "5h"
        assert set(oc["pools"]) == {"zai"}  # a fresh agent gets its pool created, not a sibling plan
        # a second hit without reset text keeps the pool's known future reset
        record_quota(pled, "opencode", "usage limit reached", zn + datetime.timedelta(hours=1), pool="zai")
        z2 = json.load(open(pled))["agents"]["opencode"]["pools"]["zai"]
        assert z2["exhausted_until"] == zai_pool["exhausted_until"] and z2["exhausted_guess"] is False
        # no --pool still writes the agent-level fields, exactly as before
        record_quota(pled, "opencode", "usage limit reached", now)
        oc2 = json.load(open(pled))["agents"]["opencode"]
        assert oc2["exhausted_source"] == "pane-text" and oc2["exhausted_guess"] is True
        for bad in ("../escape", "has space", "", "a" * 65):
            try:
                record_quota(pled, "opencode", "usage limit reached", now, pool=bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"pool {bad!r} should be refused")
    finally:
        shutil.rmtree(tmp_pool, ignore_errors=True)

    # K3: Ollama Cloud Pro 5-hour wall — one pool for every ollama-cloud/* model, keyed "ollama"
    ollama_hint = "You reached your Pro 5-hour limit. resets in 2h 14m"
    ollama_bare = "You reached your Pro 5-hour limit"
    assert classify(ollama_hint, "idle", 1, 10)[0] == "QUOTA", ollama_hint
    assert classify(ollama_bare, "working", 1, 10)[0] == "QUOTA", ollama_bare
    assert classify("YOU REACHED YOUR PRO 5-HOUR LIMIT — try another model", "idle", 1, 10)[0] == "QUOTA"
    buried = ("thinking\n" * 20) + "error: You reached your Pro 5-hour limit. resets at 3:14pm\n" + ("more\n" * 10)
    assert classify(buried, "working", 1, 10)[0] == "QUOTA", buried
    # a mere mention of "5-hour" is not the wall; an ordinary working pane stays OK
    for msg in ("the Pro plan's 5-hour window is usually enough",
                "docs say a 5-hour limit exists for ollama cloud",
                "editing lane-health.py, tests still green"):
        assert classify(msg, "working", 1, 10)[0] == "OK", msg
    # an existing agent's quota line still classifies as before
    assert classify("⚠ Individual quota reached. Resets in 154h43m", "idle", 1, 10)[0] == "QUOTA"
    assert parse_reset(ollama_hint, now) == (later(hours=2, minutes=14), "5h"), parse_reset(ollama_hint, now)
    assert parse_reset(ollama_bare, now) == (None, "5h"), parse_reset(ollama_bare, now)
    assert parse_reset("You reached your Pro 5-hour limit. resets at 3:14pm", now) == (later(hours=3, minutes=14), "5h")
    tmp_ol = tempfile.mkdtemp(prefix="lane-health-ollama-")
    try:
        olled = os.path.join(tmp_ol, "usage.json")
        json.dump({"updated": "keep", "agents": {"opencode": {"source": "codexbar", "status": "ok"}}}, open(olled, "w"))
        e = record_quota(olled, "ollama", "> brief\n" + ollama_hint, now)
        assert e["exhausted_until"] == later(hours=2, minutes=14) and e["exhausted_window"] == "5h", e
        assert e["exhausted_guess"] is False and e["exhausted_source"] == "pane-text" and e["exhausted_evidence"], e
        saved = json.load(open(olled))
        assert saved["updated"] == "keep" and saved["agents"]["opencode"]["source"] == "codexbar"
        assert "exhausted_until" not in saved["agents"]["opencode"]
        assert set(json.load(open(olled))["agents"]) == {"opencode", "ollama"}
        # no reset hint: exhausted_until is now + 5h, flagged a guess (fresh key, not the hinted one)
        bare_led = os.path.join(tmp_ol, "bare.json")
        bare = record_quota(bare_led, "ollama", ollama_bare, now)
        assert bare["exhausted_guess"] is True and bare["exhausted_until"] == later(hours=GUESS_H), bare
        assert bare["exhausted_window"] == "5h" and set(json.load(open(bare_led))["agents"]) == {"ollama"}
    finally:
        shutil.rmtree(tmp_ol, ignore_errors=True)

    # F1: the OpenCode free-tier hit recorded under kind opencode, exhausted_until = now + retry delay
    tmp_oc = tempfile.mkdtemp(prefix="lane-health-opencode-")
    try:
        ocled = os.path.join(tmp_oc, "usage.json")
        e = record_quota(ocled, "opencode", "> brief\n" + opencode_line, now)
        assert e["exhausted_until"] == later(minutes=23, seconds=36) and e["exhausted_window"] == "5h", e
        assert e["exhausted_guess"] is False and e["exhausted_source"] == "pane-text" and e["exhausted_evidence"], e
    finally:
        shutil.rmtree(tmp_oc, ignore_errors=True)
    # R1.2: the mkdb write guard refuses the real home from this suite too.
    import importlib.util as _iu
    _spec = _iu.spec_from_file_location("mkdb", os.path.join(HERE, "mkdb.py"))
    _mod = _iu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _mod.selftest_guard_probe(os.environ["MK_HOME"])
    print("lane-health self-test ok")


def main():
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    def opt(f, d=None):
        if f not in a:
            return d
        if a.index(f) + 1 >= len(a):
            sys.exit(f"{f} needs a value\n{__doc__}")
        return a[a.index(f) + 1]
    agent, lane, kind = opt("--agent"), opt("--lane"), opt("--kind")
    if not agent or not lane or (kind and kind not in KINDS):
        sys.exit(__doc__)
    ledger = opt("--ledger")
    try:
        stall_min, since = float(cfg("session.stall_min", opt("--stall-min"))), float(opt("--since", "0"))
    except ValueError as e:
        sys.exit(f"{e}\n{__doc__}")
    r = check(agent, lane, kind, stall_min, since, opt("--prompt-marker"), ledger and os.path.expanduser(ledger),
              opt("--pool"))
    r.pop("text")
    print(json.dumps(r))


if __name__ == "__main__":
    main()
