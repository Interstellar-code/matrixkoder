#!/usr/bin/env python3
"""c1-gate: count literal store/run path names outside mkpaths, a ratchet that may only go down.

  c1-gate.py count [--json]
  c1-gate.py check --baseline N [--base REF]   exit 1 when count > N, listing new hits
  c1-gate.py ratchet --baseline N              prints the lower baseline when count went down
  c1-gate.py --self-test

Plan gate for F.3's runner:
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
import tempfile
from pathlib import Path

NAMES = ("mk.db", "matrixkoder.yaml", "project.yaml", "lanes.json", "watch-cursors.json",
         "run.json", "task.json", "events.jsonl", "board.md")
NAME_RE = re.compile(r"(?<![\w.-])(" + "|".join(re.escape(n) for n in NAMES) + r")(?![\w-])")
EXCLUDED = ("scripts/mkpaths.py", "hooks/paths.ts", "scripts/c1-gate.py")
COMMENT = ("#", "//", "*", "/*")
ROOT = Path(__file__).resolve().parent.parent


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


def hits_in(rel, text):
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        # shortcut: a docstring body line still counts, upgrade to ast/tokenize if it gets noisy
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


def fmt(h):
    return f"{h[0]}:{h[1]}: {h[2]}"


def cmd_count(root, as_json):
    hits = scan_dir(root)
    if as_json:
        print(json.dumps({"count": len(hits), "hits": [{"file": h[0], "line": h[1], "name": h[2]} for h in hits]}))
        return 0
    print(len(hits))
    for h in hits:
        print(fmt(h))
    return 0


def cmd_check(root, baseline, base):
    hits = scan_dir(root)
    if len(hits) <= baseline:
        print(f"c1 ok: {len(hits)} <= baseline {baseline}")
        return 0
    shown = new_hits(hits, scan_ref(root, base)) if base else hits
    print(f"c1 FAIL: {len(hits)} > baseline {baseline}; {'new' if base else 'all'} hits:")
    for h in shown:
        print(fmt(h))
    return 1


def cmd_ratchet(root, baseline):
    n = len(scan_dir(root))
    if n < baseline:
        print(n)
    else:
        print(f"c1 ratchet: {n} not below baseline {baseline}; keep {baseline}", file=sys.stderr)
    return 0


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
    print("c1-gate self-test ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--root", default=str(ROOT))
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("count")
    c.add_argument("--json", action="store_true")
    k = sub.add_parser("check")
    k.add_argument("--baseline", type=int, required=True)
    k.add_argument("--base")
    r = sub.add_parser("ratchet")
    r.add_argument("--baseline", type=int, required=True)
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.cmd == "count":
        return cmd_count(a.root, a.json)
    if a.cmd == "check":
        return cmd_check(a.root, a.baseline, a.base)
    if a.cmd == "ratchet":
        return cmd_ratchet(a.root, a.baseline)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
