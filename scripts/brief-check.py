#!/usr/bin/env python3
"""MatrixKoder brief template check — the Rules block points at the mk-builder skill, never restates
it. Python 3 stdlib only.

  (no args)    check the template once
  --self-test  the same check, plus a synthetic old-shape template the check must reject

Loads skills/matrixkoder/templates/brief.md relative to this script's own location (never cwd,
never a lane-only `.matrixkoder-*` path), renders the `<...>` placeholders with representative
values, then asserts:

(a) the `## Rules` block (lines between the `## Rules` header and the next `## ` header) has at
    most 4 content lines and names `mk-builder` — every lane rule lives in the mk-builder skill
    the acquire installs in the lane, so the brief carries only per-brief values;
(b) none of the gone rule-class markers appears in any line of the rendered template:
    jev_ / orchestrator / git checkout / git stash / git reset / git clean / git restore /
    background / secret / foreground / .matrixkoder- / git apply / subagent — each was in the old
    9-bullet Rules block (or the Tests/Commands text it owned); if one proves wrong for the current
    wording, fix the template, not this list;
(c) templates/review.md names the mk-builder skill (its reviewer role).

This is the fail-on-old-code check for plan task MB4.2: pointed at the old template shape it exits
non-zero (Rules block > 4 lines, markers present) — revert the template and run it to see it fail.
`scripts/selftest.sh` picks it up with every other `scripts/*.py --self-test`.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True  # no stray __pycache__ next to the scripts

BRIEF = os.path.normpath(os.path.join(HERE, os.pardir, "skills", "matrixkoder", "templates",
                                      "brief.md"))
REVIEW = os.path.normpath(os.path.join(HERE, os.pardir, "skills", "matrixkoder", "templates",
                                       "review.md"))
GONE = ("jev_", "orchestrator", "git checkout", "git stash", "git reset", "git clean",
        "git restore", "background", "secret", "foreground", ".matrixkoder-", "git apply",
        "subagent")
VALUES = {"PHASE": "W1", "n": "2", "N": "2", "T": "W1", "path": "/tmp/lane",
          "branch": "mk/r1/w1", "sha": "abc1234"}


def render(text):
    """Substitute every `<...>` placeholder with a representative value (exact names from VALUES,
    anything else short sample text) — the check runs on what a filled brief would say."""
    return re.sub(r"<[^>]+>", lambda m: VALUES.get(m.group(0)[1:-1], "sample"), text)


def rules_block(rendered):
    lines = rendered.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == "## Rules"), None)
    if start is None:
        return None
    block = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        block.append(line)
    return block


def failures_for(rendered):
    fails = []
    block = rules_block(rendered)
    if block is None:
        fails.append("no `## Rules` header found")
    else:
        content = [l for l in block if l.strip()]
        if len(content) > 4:
            fails.append("## Rules has %d content lines (max 4):" % len(content))
            fails.extend("    " + l for l in content)
        if not any("mk-builder" in l for l in content):
            fails.append("## Rules never names mk-builder")
    for marker in GONE:
        hits = [l for l in rendered.splitlines() if marker in l]
        if hits:
            fails.append('gone rule-class marker "%s" still in:' % marker)
            fails.extend("    " + l for l in hits)
    return fails


def check_brief(path):
    with open(path, encoding="utf-8") as f:
        return failures_for(render(f.read()))


def optional_spans(text):
    """Line ranges of the optional TAKEOVER / FIX / LEAD blocks: a placeholder there is allowed."""
    lines = text.splitlines()
    heads = ("## TAKEOVER", "## FIX", "## LEAD")
    spans, i = [], 0
    while i < len(lines):
        if lines[i].startswith(heads):
            start = i
            i += 1
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
            spans.append((start, i))
        else:
            i += 1
    return spans


def in_span(n, spans):
    return any(a <= n < b for a, b in spans)


def placeholder_failures(text):
    """Unfilled `<...>` outside the optional blocks, and a raw `# <PHASE>` title.

    Names the line. A filled brief has none of these.
    """
    fails = []
    spans = optional_spans(text)
    for n, line in enumerate(text.splitlines(), 1):
        if in_span(n - 1, spans):
            continue
        if re.search(r"<[^>\n]+>", line):
            fails.append(f"line {n}: unfilled placeholder: {line.strip()}")
    first = next((l for l in text.splitlines() if l.strip()), "")
    if re.match(r"#\s+<PHASE>", first):
        fails.insert(0, f"line 1: raw template title: {first.strip()}")
    return fails


def check_placeholders(path):
    """Exit-style check of one brief file. Returns the failures (empty = pass)."""
    with open(path, encoding="utf-8") as f:
        return placeholder_failures(f.read())


def self_test():
    # the raw template is rejected: its title is `# <PHASE>` and its body is unfilled
    raw = open(BRIEF, encoding="utf-8").read()
    raw_fails = placeholder_failures(raw)
    assert any("raw template title" in f for f in raw_fails), raw_fails
    assert any("unfilled placeholder" in f for f in raw_fails), raw_fails

    # a filled brief passes. Placeholders inside the FIX block are allowed.
    filled = "\n".join([
        "# W1 — the goal",
        "",
        "Repo: this folder (worktree `/tmp/lane`, branch `mk/r1/w1`, at `abc1234`).",
        "",
        "## Rules",
        "- Use the mk-builder skill — round R2.",
        "",
        "## FIX (review `review-r1.md`)",
        "1. [HIGH] hooks/wake.ts:481 — cursor — baseline it",
        "- Commit: `fix: x` subject, blank line, `Co-Authored-By: <agent/model> <email>`.",
        "",
    ])
    assert placeholder_failures(filled) == [], placeholder_failures(filled)

    # the same placeholder OUTSIDE the FIX block fails, and the line is named
    bad = filled.replace("## FIX (review `review-r1.md`)", "## Notes")
    bad_fails = placeholder_failures(bad)
    assert any(f.startswith("line ") and "unfilled placeholder" in f for f in bad_fails), bad_fails

    # the live template passes
    fails = check_brief(BRIEF)
    assert not fails, fails

    # review.md names the mk-builder skill (its reviewer role)
    with open(REVIEW, encoding="utf-8") as f:
        assert "mk-builder" in f.read(), "review.md must name the mk-builder skill"

    # an old-shape template (9-bullet Rules block, one bullet per gone rule class) is rejected —
    # the fail-on-old-code proof: this is the same check that must fail on the reverted template.
    # The sample carries every GONE marker, so each detector is proven to fire.
    old_rules = [
        "- builder, not the orchestrator: no jev_ routing, ever",
        "- never git checkout, never git stash",
        "- never git reset, git clean or git restore",
        "- foreground work only: no background tasks",
        "- no subagent without the plan flag",
        "- no secret values in code or reports",
        "- committed code never reads lane-only .matrixkoder- files",
        "- old-code proof: git apply the patch back afterwards",
        "- last line: <PHASE> R<n> DONE",
    ]
    assert all(m in "\n".join(old_rules) for m in GONE), "sample must carry every marker"
    old = "\n".join(["# <PHASE> sample", "", "## Rules"] + old_rules + ["", "## TAKEOVER"]) + "\n"
    old_fails = failures_for(render(old))
    assert any("content lines" in f for f in old_fails), old_fails
    for marker in GONE:
        assert any(f == 'gone rule-class marker "%s" still in:' % marker for f in old_fails), \
            (marker, old_fails)

    print("brief-check self-test ok")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # a path argument checks THAT brief for unfilled placeholders (exit non-zero, names the line)
    if argv and argv[0] != "--self-test":
        fails = []
        for path in argv:
            fails.extend(check_placeholders(path))
        for line in fails:
            print(line)
        if fails:
            return 1
        print("brief-check ok")
        return 0
    fails = check_brief(BRIEF)
    for line in fails:
        print(line)
    if fails:
        return 1
    print("brief-check ok")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        sys.exit(main())
