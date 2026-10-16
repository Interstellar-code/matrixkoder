#!/usr/bin/env python3
"""c1-gate: literal store/run path names outside mkpaths (py strings and f-strings, ts strings and template
literals). Kept hits (message text, labels, fixtures) are listed in ALLOW by file + exact source text; `check`
fails on any other hit, so the baseline is 0.

  c1-gate.py count [--json] [--unlisted]
  c1-gate.py check --baseline N [--base REF]   exit 1 when count > N, listing new hits
  c1-gate.py ratchet --baseline N              prints the lower baseline when count went down
  c1-gate.py canary                            rename canary: mkpaths self-test + mk-e2e-layout run/check with every
                                               file-name constant overridden (MK_NAME_*) on a temp home
  c1-gate.py --self-test

Plan gate for F.3's runner (the same stanza ships in skills/matrixkoder/templates/plan.md):
  [gates.c1]
  cmd = "python3 scripts/c1-gate.py check --baseline <n> --base main"
  expect = 0
  timeout = 60
"""
import argparse
import collections
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import shutil
import tempfile
import tokenize
from pathlib import Path

NAMES = ("mk.db", "matrixkoder.yaml", "project.yaml", "lanes.json", "watch-cursors.json",
         "run.json", "task.json", "events.jsonl", "board.md")
NAME_RE = re.compile(r"(?<![\w.-])(" + "|".join(re.escape(n) for n in NAMES) + r")(?![\w-])")
EXCLUDED = ("scripts/mkpaths.py", "hooks/paths.ts", "scripts/c1-gate.py")
COMMENT = ("#", "//", "*", "/*")
ROOT = Path(__file__).resolve().parent.parent

# Kept hits: a name in message text, a label, a name comparison, a mutation-test patch or a repo
# fixture, not a path build. Matched by file + exact source text, so a moved line still matches.
# A new literal that is not in this list is a `check` failure; add it here only with a reason.
ALLOW = {
    'hooks/dashboard.tsx': [
        "const error = doc.run != null && run == null ? 'cannot read run.json' : null // a run the store named that we cannot read",
        "loadError = `No ${yamlPath ?? 'matrixkoder.yaml'} — run /mk setup`",
    ],
    'hooks/guards.ts': [
        "return hit ? { cmd: hit, why: 'banned in matrixkoder.yaml' } : undefined",
        "return { cmd: 'sqlite3 write to mk.db', why: 'use mk-state / mkdb.py, or sqlite3 -readonly' }",
    ],
    'hooks/settings-io.ts': [
        "const YAML_FILE_LABEL = 'project.yaml'",
    ],
    'hooks/tabs/plan.fixture.ts': [
        "footer: ['5 phases · all folded by default', 'source mk.db'],",
    ],
    'hooks/tabs/plankanban.tsx': [
        '<Text dimColor={true}>{`${shown.length} tasks · this plan only · source mk.db`}</Text>',
    ],
    'hooks/tabs/settings.fixture.ts': [
        "{ id: 'agents', label: 'Agents', file: 'project.yaml', note: 'agents · usage' },",
        "{ id: 'child', label: 'Child', file: 'project.yaml', note: 'lanes.child' },",
        "{ id: 'models', label: 'Models', file: 'project.yaml', note: 'agents.*.models' },",
        "{ id: 'project', label: 'Project', file: 'project.yaml', note: 'this repo' },",
        "{ id: 'routing', label: 'Routing', file: 'project.yaml', note: 'routing_preferences' },",
    ],
    'hooks/tabs/settings.tsx': [
        "if (src === 'legacy') return '↳ from project.yaml'",
        "return src === 'legacy' ? '↳ not global · project.yaml' : '↳ not global · default'",
    ],
    'hooks/testkit.ts': [
        "['/fixtures/run.json', '/tmp/mk-proj/.matrixkoder/runs/wfv2/run.json'],",
        "['/fixtures/run.json']: RUN_JSON,",
        "const run = parseObj(files['/fixtures/run.json'])",
        "files['/fixtures/run.json'] = JSON.stringify(run)",
    ],
    'hooks/wizard-core.ts': [
        "L.push('# <data>/matrixkoder.yaml — written by the MatrixKoder onboarding wizard after the user confirmed.')",
    ],
    'hooks/wizard.tsx': [
        "<Text>{`${d.existingYaml && !d.replace ? 'keep   ' : d.existingYaml ? 'replace' : 'create '} ${data ?? '?'}/matrixkoder.yaml`}</Text>",
        "await patch($, { existingYaml: true, replace: false, phase: 'error', error: 'matrixkoder.yaml appeared since detection — choose keep or replace, then write again' })",
        'key="wz-replace" label="matrixkoder.yaml already exists" value={d.replace ? \'replace\' : \'keep\'}',
    ],
    'scripts/lane-watch.py': [
        'assert not os.path.exists(mkpaths.db_in(data)), "a watch created mk.db"',
        'assert not os.path.exists(mkpaths.db_in(data)), "file-path run created mk.db"',
    ],
    'scripts/mk-bus.py': [
        'assert not os.path.exists(cur_path), "a store-mode watch wrote watch-cursors.json"',
        'assert os.path.isfile(_load_mkpaths().db_in(data)), "open_project did not create mk.db"',
    ],
    'scripts/mk-config.py': [
        'FIXTURE = os.path.join(HERE, "fixtures", "matrixkoder.yaml")',
        'assert "matrixkoder.yaml.lock busy" in busy["error"], busy',
        'assert os.path.isfile(real_yaml), f"the switch did not place project.yaml: {os.listdir(c2)}"',
        'f"migrate created a phantom beside project.yaml: {os.listdir(c2)}"',
        'real_yaml = "/Users/rohits/Development/matrixkoder/.matrixkoder/matrixkoder.yaml"',
    ],
    'scripts/mk-e2e-layout.py': [
        '@m("R01", "a stray mk.db under the temp root", "fix-stray/mk.db")',
        '@m("R05", "a stray run.json", "run.json")',
        '@m("R10", "watch cursors in a data dir instead of the project folder", "watch-cursors.json")',
        'Rule("R01", "C2", "exactly the root mk.db and one mk.db per project", r01),',
        'Rule("R05", "C4", "no run.json/task.json/events.jsonl/board.md/index.json anywhere", r05),',
        'Rule("R10", "C6", "watch cursors live at projects/<slug>/watch-cursors.json", r10),',
        'bad.append(f"root mk.db has a {name} table (C3: root is numbers only)")',
        'return ok, f"{len(found)} mk.db under the temp root: {found}" \\',
    ],
    'scripts/mk-lane-event.py': [
        'return ((rec or {}).get("task") or None), (None if rec else f"slot {slot} not in lanes.json")',
    ],
    'scripts/mk-lane.py': [
        'assert not os.path.exists(mkpaths.legacy_lanes(rsd)), "a refused acquire wrote lanes.json"',
        'assert os.path.isfile(mkpaths.db_in(rsd)), "store enable wrote no mk.db"',
    ],
    'scripts/mk-migrate.py': [
        '\'if False:\\n            bad.append("mk.db (the source changed during the copy)")\'),',
        '\'if False:\\n            bad.append("mk.db")\'),',
        '(\'if dst_counts != src_counts or dst_digest != src_digest:\\n            bad.append("mk.db")\',',
        '(\'if now_counts != src_counts or now_digest != src_digest:\\n            bad.append("mk.db (the source changed during the copy)")\',',
        '_check(e.code == 3 and "lane registry unreadable" in str(e), f"non-SQLite mk.db: {e}")',
        '_check(e.code == 3 and "unreadable" in str(e), f"corrupt lanes.json: {e}")',
        '_check(not os.path.exists(mkpaths.task_doc_in(rt)), "task.json should not move (C2)")',
        '_check(os.path.isfile(mkpaths.target_project_yaml("fixture", home)), "project.yaml not copied")',
        'bad.append("mk.db (the source changed during the copy)")',
        'bad.append("mk.db")',
        'moves.append({"label": "lanes.json", "kind": "file", "src": lanes_json,',
        'moves.append({"label": "project.yaml", "kind": "file", "src": yaml,',
        'moves.append({"label": "watch-cursors.json", "kind": "file", "src": wc,',
        'raise SystemExit("SELF-TEST FAIL: a corrupt lanes.json did not refuse")',
        'raise SystemExit("SELF-TEST FAIL: a non-SQLite mk.db did not refuse")',
        'raise SystemExit("SELF-TEST FAIL: quiesce passed with a corrupt lanes.json")',
    ],
    'scripts/mk-plan.py': [
        'assert got["store"] is False and any("mk.db" in w for w in got.get("warnings") or []), got',
        'raise Fail(["no store: export needs <data>/mk.db"])',
        'raise Fail([f"no store: delete needs <data>/mk.db ({data})"])',
        'raise Fail([f"no store: import needs <data>/mk.db ({data})"])',
    ],
    'scripts/mk-state.py': [
        'assert os.path.exists(mkpaths.db_in(data)), "the store is always on: mk.db must exist"',
        'assert os.path.exists(old_db), "mk-state store enable wrote no <data>/mk.db"',
        'assert sha(old_db) == old_sha, "the legacy <data>/mk.db changed after the switch"',
        'return "banned", f"banned command {hit!r} (project.yaml banned_commands); gate not run", empty',
    ],
    'scripts/mk-store-import.py': [
        '"src": f"{run_id}:events.jsonl:{n}",',
        'raise Fail(f"bad run.json for {run_id}: expected an object")',
        'raise Fail(f"bad run.json for {run_id}: tasks must be objects with id")',
        'raise Fail(f"bad run.json for {run_id}: {e}")',
        'raise Fail(f"events.jsonl for {run_id} escapes the run dir")',
        'raise Fail(f"no run.json for {run_id}")',
        'raise Fail(f"run.json for {run_id} escapes the run dir")',
    ],
    'scripts/mkdb.py': [
        '_check("mk.db" in rr["restored"], "_root_cleanup: restore did not restore the DB")',
        '_check(not os.path.exists(_mkpaths().db_in(bare)), "overview created mk.db")',
        '_check(not os.path.exists(_mkpaths().db_in(bare)), "projects created mk.db")',
        '_check(projects(bare) == [], "projects() created mk.db")',
        'bad.append("mk.db copy does not match its sha256")',
        'bad.append("mk.db copy fails integrity_check")',
        'for name in ("mk.db", "mk.db-wal"):',
        'if rel.startswith("mk.db"):',
        'manifest["db"] = {"path": "mk.db", "size": os.path.getsize(_mkpaths().db_in(bdir)),',
        'restored.append("mk.db")',
    ],
}


def wanted(rel):
    """True for a scanned source file: scripts/**/*.py or hooks/**/*.ts(x), minus the path owners and tests."""
    p = Path(rel)
    if rel in EXCLUDED or not p.parts:
        return False
    if p.parts[0] == "scripts" and p.suffix == ".py":
        pass
    elif p.parts[0] == "hooks" and p.suffix in (".ts", ".tsx"):
        pass
    else:
        return False
    n = p.name
    if n.startswith("test_") or n.endswith(("_test.py", ".test.ts", ".test.tsx")):
        return False
    return not {"__tests__", "tests", "fixtures", "__pycache__", "node_modules"} & set(p.parts)


def _line_hits(rel, first, text, src, out):
    for off, chunk in enumerate(text.split("\n")):
        for m in NAME_RE.finditer(chunk):
            out.append((rel, first + off, m.group(1), src[first + off - 1].strip()))


def _py_hits(rel, text):
    """Name hits inside string tokens only: comments and docstrings (a lone string statement) are skipped."""
    src = text.splitlines()
    out, prev = [], None
    toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    for i, tok in enumerate(toks):
        kind = tokenize.tok_name[tok.type]
        if kind == "STRING" and prev in (None, "NEWLINE", "INDENT", "DEDENT", "NL"):
            nxt = next((t for t in toks[i + 1:] if t.type not in (tokenize.NL, tokenize.COMMENT)), None)
            if nxt is None or nxt.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                prev = kind
                continue
        if kind in ("STRING", "FSTRING_MIDDLE"):
            _line_hits(rel, tok.start[0], tok.string, src, out)
        if kind not in ("NL", "COMMENT"):
            prev = kind
    return out


def _ts_strings(text):
    """[(line, start col, string content)] for every quoted string and template-literal chunk in TS/TSX
    source, comments skipped. A template literal that spans lines yields one chunk per line, so a name on
    a continuation line (no quote on it) is still seen; `${ ... }` expressions are scanned as code.
    shortcut: a regex literal holding a quote can confuse it; none exists in hooks/ today."""
    out, i, n, line, col0 = [], 0, len(text), 1, 0
    stack = []  # open template literals: the brace depth to return to after a ${ ... }
    depth = 0
    cur, cur_line, cur_col, quote = "", 1, 0, None  # the chunk being read, and its opening quote

    def flush():
        nonlocal cur
        if cur:
            out.append((cur_line, cur_col, cur))
        cur = ""

    while i < n:
        c = text[i]
        if quote is None:
            nxt = text[i + 1] if i + 1 < n else ""
            if c == "/" and nxt == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if c == "/" and nxt == "*":
                j = text.find("*/", i + 2)
                j = n if j < 0 else j + 2
                line += text.count("\n", i, j)
                col0 = i if "\n" not in text[i:j] else j - (j - text.rfind("\n", i, j) - 1)
                i = j
                continue
            if c in "\"'`":
                quote, cur, cur_line, cur_col = c, "", line, i - col0
                i += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                if stack and depth == stack[-1]:  # the ${ ... } of an open template ends: its text resumes
                    stack.pop()
                    quote, cur, cur_line, cur_col = "`", "", line, i - col0
                else:
                    depth -= 1
            elif c == "\n":
                line += 1
                col0 = i + 1
            i += 1
            continue
        # inside a string or a template chunk
        if c == "\\":
            cur += text[i:i + 2]
            if text[i + 1:i + 2] == "\n":
                line += 1
                col0 = i + 2
            i += 2
            continue
        if c == quote:
            flush()
            quote = None
            i += 1
            continue
        if quote == "`" and c == "$" and text[i + 1:i + 2] == "{":
            flush()
            stack.append(depth)
            quote = None
            i += 2
            continue
        if c == "\n":
            if quote == "`":
                flush()
                line += 1
                col0 = i + 1
                cur_line, cur_col = line, 0
            else:  # an unterminated ' or " string ends at the line (a bad scan must not eat the file)
                flush()
                quote = None
                line += 1
                col0 = i + 1
            i += 1
            continue
        cur += c
        i += 1
    flush()
    return out


def _ts_hits(rel, text):
    src = text.splitlines()
    out = []
    for ln, _col, chunk in _ts_strings(text):
        for m in NAME_RE.finditer(chunk):
            out.append((rel, ln, m.group(1), src[ln - 1].strip()))
    return out


def hits_in(rel, text):
    if rel.endswith(".py"):
        try:
            return _py_hits(rel, text)
        except (tokenize.TokenError, SyntaxError):
            pass  # shortcut: an untokenizable file falls back to the line scan, which counts docstrings
    elif rel.endswith((".ts", ".tsx")):
        return _ts_hits(rel, text)
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith(COMMENT) or not any(q in line for q in "\"'`"):
            continue
        for m in NAME_RE.finditer(line):
            out.append((rel, i, m.group(1), s))
    return out


def scan_dir(root):
    hits = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in (".git", "node_modules", "__pycache__")]
        for f in fn:
            rel = os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/")
            if wanted(rel):
                hits += hits_in(rel, Path(root, rel).read_text(errors="replace"))
    return sorted(hits)


def _git(root, *a):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          capture_output=True, text=True, check=True).stdout


def scan_ref(root, ref):
    hits = []
    for rel in _git(root, "ls-tree", "-r", "--name-only", ref).splitlines():
        if wanted(rel):
            hits += hits_in(rel, _git(root, "show", f"{ref}:{rel}"))
    return sorted(hits)


def new_hits(now, old):
    """Hits in `now` with no equal (file, line text, name) in `old`; line numbers shift, text does not."""
    left = collections.Counter((h[0], h[3], h[2]) for h in old)
    out = []
    for h in now:
        k = (h[0], h[3], h[2])
        if left[k] > 0:
            left[k] -= 1
        else:
            out.append(h)
    return out


def unlisted(hits):
    """The hits that are not an allow-listed kept hit (file + exact source text)."""
    return [h for h in hits if h[3] not in ALLOW.get(h[0], ())]


# the env var that overrides each mkpaths file-name constant, and the name the canary gives it
CANARY_NAMES = {
    "MK_NAME_ROOT_DB": ("mk.db", "mk-overview.db"),
    "MK_NAME_SETTINGS": ("mk-settings.yaml", "mk-prefs.yaml"),
    "MK_NAME_LEGACY_YAML": ("matrixkoder.yaml", "mk-old.yaml"),
    "MK_NAME_PROJECT_YAML": ("project.yaml", "mk-proj.yaml"),
    "MK_NAME_RUN_DOC": ("run.json", "r-doc.json"),
    "MK_NAME_TASK_DOC": ("task.json", "t-doc.json"),
    "MK_NAME_EVENTS_DOC": ("events.jsonl", "ev.jsonl"),
    "MK_NAME_BOARD_DOC": ("board.md", "bd.md"),
    "MK_NAME_LANES": ("lanes.json", "ln.json"),
    "MK_NAME_CURSORS": ("watch-cursors.json", "cur.json"),
}


def canary_env():
    return dict(os.environ, MK_STATE_LOADAVG="0", **{k: v[1] for k, v in CANARY_NAMES.items()})


def leaked_default_names(root):
    """Files under `root` still carrying a DEFAULT name (or its -wal/-shm sidecar): a path built outside mkpaths."""
    default = {v[0] for v in CANARY_NAMES.values()}
    default |= {n + suf for n in ("mk.db",) for suf in ("-wal", "-shm")}
    return sorted(os.path.join(dp, f) for dp, _dn, fn in os.walk(root) for f in fn if f in default)


def _run(argv, env, timeout=900):
    r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def cmd_canary(root):
    """Rename canary. The whole plugin suite is NOT run (too heavy): mkpaths' own self-test plus the production-tool
    cycle of mk-e2e-layout, both with every mkpaths name constant overridden. Passes only when both pass and no file
    with a default name appears in the tree the cycle built."""
    scripts = Path(root) / "scripts"
    env = canary_env()
    ok = True
    rc, out, err = _run([sys.executable, str(scripts / "mkpaths.py"), "--self-test"], env)
    print(f"canary mkpaths --self-test: {'ok' if rc == 0 else 'FAIL'}")
    if rc != 0:
        print((out + err).strip()[-600:])
        return 1
    rc, out, err = _run([sys.executable, str(scripts / "mk-e2e-layout.py"), "run", "--keep"], env)
    try:
        info = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        info = {}
    home = info.get("home")
    if rc != 0 or not home:
        print(f"canary mk-e2e-layout run: FAIL rc={rc}\n{(out + err).strip()[-600:]}")
        return 1
    print("canary mk-e2e-layout run: ok")
    try:
        rc, out, err = _run([sys.executable, str(scripts / "mk-e2e-layout.py"), "check", "--home", home], env)
        print(f"canary mk-e2e-layout check: {'ok' if rc == 0 else 'FAIL'}")
        if rc != 0:
            print(out.strip()[-800:])
            ok = False
        leaked = leaked_default_names(os.path.dirname(home))
        print(f"canary default-name files in the tree: {len(leaked)}")
        for f in leaked:
            print(f"  LEAK {f}")
        ok = ok and not leaked
    finally:
        _run([sys.executable, str(scripts / "mk-e2e-layout.py"), "teardown", "--home", home], env)
    print("canary ok: every name overridden" if ok else "canary FAIL")
    return 0 if ok else 1


def fmt(h):
    return f"{h[0]}:{h[1]}: {h[2]}"


def cmd_count(root, as_json, only_unlisted=False):
    hits = scan_dir(root)
    if only_unlisted:
        hits = unlisted(hits)
    if as_json:
        print(json.dumps({"count": len(hits), "hits": [{"file": h[0], "line": h[1], "name": h[2]} for h in hits]}))
        return 0
    print(len(hits))
    for h in hits:
        print(fmt(h))
    return 0


def cmd_check(root, baseline, base):
    """Fail when the hits outside the ALLOW list exceed the baseline (0 once the kept hits are listed)."""
    every = scan_dir(root)
    hits = unlisted(every)
    if len(hits) <= baseline:
        print(f"c1 ok: {len(hits)} unlisted <= baseline {baseline} ({len(every) - len(hits)} kept hits listed)")
        return 0
    shown = new_hits(hits, unlisted(scan_ref(root, base))) if base else hits
    print(f"c1 FAIL: {len(hits)} unlisted > baseline {baseline}; {'new' if base else 'all'} hits:")
    for h in shown:
        print(fmt(h))
    return 1


def cmd_ratchet(root, baseline):
    n = len(unlisted(scan_dir(root)))
    if n < baseline:
        print(n)
    else:
        print(f"c1 ratchet: {n} not below baseline {baseline}; keep {baseline}", file=sys.stderr)
    return 0


def _self_test_real_tree():
    """The committed tree passes at baseline 0; a planted literal in a temp copy of it fails; an allow-listed
    kept hit does not. Proves the ALLOW list and the scanner agree (a stale entry or a new hit shows here)."""
    assert cmd_check_quiet(ROOT, 0), "the tree has hits outside ALLOW: run `c1-gate.py count --unlisted`"
    with tempfile.TemporaryDirectory() as d:
        for part in ("scripts", "hooks"):
            shutil.copytree(ROOT / part, Path(d) / part, ignore=shutil.ignore_patterns("node_modules", "__pycache__", ".git"))
        kept = next(f for f in sorted(ALLOW) if f.startswith("scripts/") and (Path(d) / f).exists())
        assert cmd_check_quiet(d, 0), "an unchanged copy of the tree must pass"
        with open(Path(d) / kept, "a") as fh:
            fh.write('\nPLANTED = f"{base}/mk.db"\n')
        assert not cmd_check_quiet(d, 0), "a planted literal must fail check"
        with open(Path(d) / "hooks" / "paths.test.ts", "w") as fh:  # test files are not scanned
            fh.write("const x = `${a}/mk.db`\n")
        with open(Path(d) / "hooks" / "newfile.ts", "w") as fh:
            fh.write("export const p = `${a}/\n  lanes.json`\n")
        out = subprocess.run([sys.executable, __file__, "check", "--baseline", "0", "--root", d], capture_output=True, text=True)
        assert out.returncode == 1 and "newfile.ts:2: lanes.json" in out.stdout and "PLANTED" not in out.stdout.split("hits:")[0], out.stdout


def cmd_check_quiet(root, baseline):
    with contextlib.redirect_stdout(io.StringIO()):
        return cmd_check(root, baseline, None) == 0


def self_test():
    with tempfile.TemporaryDirectory() as d:
        t = Path(d)
        (t / "scripts").mkdir()
        (t / "hooks" / "tabs").mkdir(parents=True)
        (t / "scripts" / "a.py").write_text('p = os.path.join(root, "mk.db")\nq = f"{r}/run.json"\n# "mk.db" comment\n')
        (t / "scripts" / "mkpaths.py").write_text('DB = "mk.db"\n')
        (t / "scripts" / "test_a.py").write_text('x = "mk.db"\n')
        (t / "hooks" / "paths.ts").write_text('export const D = "mk.db"\n')
        (t / "hooks" / "tabs" / "x.test.ts").write_text('const x = "board.md"\n')
        (t / "hooks" / "tabs" / "v.ts").write_text("const p = `${r}/events.jsonl`\nconst s = 'prerun.json'\n")
        hits = scan_dir(t)
        assert [fmt(h) for h in hits] == ["hooks/tabs/v.ts:1: events.jsonl", "scripts/a.py:1: mk.db",
                                          "scripts/a.py:2: run.json"], hits  # mkpaths, tests, comments not counted
        assert cmd_check(t, 3, None) == 0
        # a docstring and an inline comment naming a store file are not counted
        (t / "scripts" / "d.py").write_text('"""Module doc: mk.db lives here."""\n\n\ndef f():\n    """Reads "run.json"."""\n'
                                            '    x = 1  # "mk.db" note\n    return x\n')
        assert len(scan_dir(t)) == 3, scan_dir(t)
        # --root is accepted after the subcommand
        out = subprocess.run([sys.executable, __file__, "count", "--root", d], capture_output=True, text=True)
        assert out.stdout.splitlines()[0] == "3", out
        with open(t / "scripts" / "a.py", "a") as f:
            f.write('z = "lanes.json"\n')
        assert cmd_check(t, 3, None) == 1  # rise fails
        # base diff names only the new hit
        _git(d, "init", "-q")
        (t / "scripts" / "a.py").write_text('p = os.path.join(root, "mk.db")\nq = f"{r}/run.json"\n')
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "base")
        (t / "scripts" / "a.py").write_text('n = 1\np = os.path.join(root, "mk.db")\nq = f"{r}/run.json"\nz = "lanes.json"\n')
        assert [fmt(h) for h in new_hits(scan_dir(t), scan_ref(t, "HEAD"))] == ["scripts/a.py:4: lanes.json"]
        # check against the base: a rise exits 1 and names only the new hit; equal or lower exits 0
        (t / "scripts" / "a.py").write_text('n = 1\np = os.path.join(root, "mk.db")\nq = f"{r}/run.json"\nz = "lanes.json"\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert cmd_check(t, 3, "HEAD") == 1
        assert "scripts/a.py:4: lanes.json" in buf.getvalue() and "mk.db" not in buf.getvalue(), buf.getvalue()
        with contextlib.redirect_stdout(io.StringIO()):
            assert cmd_check(t, 4, "HEAD") == 0 and cmd_check(t, 5, "HEAD") == 0
        # removing a hit lowers the baseline
        (t / "scripts" / "a.py").write_text('q = f"{r}/run.json"\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_ratchet(t, 3)
        assert buf.getvalue().strip() == "2", buf.getvalue()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            cmd_ratchet(t, 1)
        assert buf.getvalue() == "", "ratchet never prints a higher number"
        # a name on the continuation line of a multi-line template literal is seen (no quote on that line)
        (t / "scripts" / "a.py").write_text("")
        (t / "hooks" / "tabs" / "v.ts").write_text("const p = `\n  ${root}/\n  lanes.json\n`\nconst c = 1 // 'mk.db'\n/* \"run.json\" */\n")
        assert [fmt(h) for h in scan_dir(t)] == ["hooks/tabs/v.ts:3: lanes.json"], scan_dir(t)
    _self_test_real_tree()
    # the canary's leak scan: a default-named file in a built tree is named; renamed files are not
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "p").mkdir()
        for n in ("mk-overview.db", "r-doc.json", "plan.md"):
            (Path(d) / "p" / n).write_text("")
        assert leaked_default_names(d) == [], leaked_default_names(d)
        (Path(d) / "p" / "run.json").write_text("")
        (Path(d) / "p" / "mk.db-wal").write_text("")
        assert [os.path.basename(x) for x in leaked_default_names(d)] == ["mk.db-wal", "run.json"], leaked_default_names(d)
    env = canary_env()
    assert set(env) >= set(CANARY_NAMES) and all(env[k] != v[0] for k, v in CANARY_NAMES.items())
    print("c1-gate self-test ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--root", default=str(ROOT))
    # SUPPRESS keeps a --root given before the subcommand when the subcommand's own is absent
    rootopt = argparse.ArgumentParser(add_help=False)
    rootopt.add_argument("--root", default=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("count", parents=[rootopt])
    c.add_argument("--json", action="store_true")
    c.add_argument("--unlisted", action="store_true", help="only hits outside the ALLOW list")
    k = sub.add_parser("check", parents=[rootopt])
    k.add_argument("--baseline", type=int, required=True)
    k.add_argument("--base")
    sub.add_parser("canary", parents=[rootopt])
    r = sub.add_parser("ratchet", parents=[rootopt])
    r.add_argument("--baseline", type=int, required=True)
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.cmd == "count":
        return cmd_count(a.root, a.json, a.unlisted)
    if a.cmd == "check":
        return cmd_check(a.root, a.baseline, a.base)
    if a.cmd == "canary":
        return cmd_canary(a.root)
    if a.cmd == "ratchet":
        return cmd_ratchet(a.root, a.baseline)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
