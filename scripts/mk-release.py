#!/usr/bin/env python3
"""MatrixKoder release helper — changelog + version bump + tag from Conventional Commits. Stdlib only.

  mk-release.py plan    [--version X.Y.Z]   what the next release would be (one JSON line)
  mk-release.py notes   [--version X.Y.Z]   the release notes as markdown (stdout)
  mk-release.py apply   [--version X.Y.Z]   prepend CHANGELOG.md, bump version files, commit, annotated tag
  mk-release.py --self-test

Run from inside the repo. Commits since the last `vX.Y.Z` tag (all history when there is none), merges
skipped. Bump: a breaking change (`type!:` or `BREAKING CHANGE` in the body) → major (minor while 0.x),
any `feat` → minor, otherwise patch. Version files: `.claude-plugin/plugin.json`, `package.json`,
`pyproject.toml` (`version = "…"` under [project]/[tool.poetry]) — whichever exist. `apply` never pushes:
pushing the branch + tag and publishing a GitHub release are separate, user-confirmed steps (skill
`release`).
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

SECTIONS = (("breaking", "Breaking"), ("feat", "Added"), ("fix", "Fixed"), ("perf", "Performance"),
            ("docs", "Docs"), ("other", "Other"))
OTHER = {"test", "chore", "refactor", "build", "ci", "style", "revert"}
SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?: (?P<desc>.+)$")
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
SEP = "\x1e"


class Fail(Exception):
    pass


def git(*args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Fail(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout


def last_tag():
    r = subprocess.run(["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def commits(since):
    rng = [f"{since}..HEAD"] if since else ["HEAD"]
    out = git("log", "--no-merges", f"--format=%h%x1f%s%x1f%b{SEP}", *rng)
    rows = []
    for rec in out.split(SEP):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, subject, body = (rec.split("\x1f") + ["", ""])[:3]
        m = SUBJECT.match(subject.strip())
        breaking = bool(m and m.group("bang")) or "BREAKING CHANGE" in body
        kind = m.group("type") if m else "other"
        if kind not in {"feat", "fix", "perf", "docs"}:
            kind = "other"
        rows.append({"sha": sha, "subject": subject.strip(), "type": kind, "breaking": breaking,
                     "scope": m.group("scope") if m else None, "desc": m.group("desc") if m else subject.strip()})
    return rows


def version_files(root):
    found = []
    for rel in (".claude-plugin/plugin.json", "package.json"):
        p = os.path.join(root, rel)
        if os.path.exists(p):
            v = json.load(open(p)).get("version")
            if isinstance(v, str):
                found.append((rel, v))
    p = os.path.join(root, "pyproject.toml")
    if os.path.exists(p):
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', open(p).read())
        if m:
            found.append(("pyproject.toml", m.group(1)))
    return found


def parse_semver(v):
    m = SEMVER.match(v.lstrip("v"))
    if not m:
        raise Fail(f"not a plain semver version: {v!r}")
    return tuple(int(x) for x in m.groups())


def bump(cur, rows):
    major, minor, patch = parse_semver(cur)
    if any(r["breaking"] for r in rows):
        return (f"{major + 1}.0.0" if major > 0 else f"0.{minor + 1}.0"), "major" if major > 0 else "minor"
    if any(r["type"] == "feat" for r in rows):
        return f"{major}.{minor + 1}.0", "minor"
    return f"{major}.{minor}.{patch + 1}", "patch"


def plan(root, forced=None):
    tag = last_tag()
    rows = commits(tag)
    files = version_files(root)
    versions = sorted({v for _, v in files})
    if len(versions) > 1:
        raise Fail(f"version files disagree: {files}")
    current = tag.lstrip("v") if tag else (versions[0] if versions else "0.0.0")
    if forced:
        parse_semver(forced)
        nxt, kind = forced.lstrip("v"), "manual"
    elif not tag and versions:
        nxt, kind = current, "first"  # first tagged release keeps the version the files already declare
    else:
        nxt, kind = bump(current, rows)
    return {"ok": True, "last_tag": tag, "current": current, "next": nxt, "bump": kind,
            "commits": len(rows), "files": [f for f, _ in files], "rows": rows}


def notes(p, date=None):
    date = date or datetime.date.today().isoformat()
    out = [f"## [{p['next']}] - {date}", ""]
    for key, title in SECTIONS:
        if key == "breaking":
            items = [r for r in p["rows"] if r["breaking"]]
        else:
            items = [r for r in p["rows"] if r["type"] == key and not r["breaking"]]
        if items:
            out.append(f"### {title}")
            out += [f"- {('**' + r['scope'] + ':** ') if r['scope'] else ''}{r['desc']} ({r['sha']})" for r in items]
            out.append("")
    if len(out) == 2:
        out += ["- No changes.", ""]
    return "\n".join(out)


def set_versions(root, files, new):
    for rel in files:
        p = os.path.join(root, rel)
        text = open(p).read()
        if rel.endswith(".json"):
            text, n = re.subn(r'("version"\s*:\s*")[^"]*(")', lambda m: m.group(1) + new + m.group(2), text, count=1)
        else:
            text, n = re.subn(r'(?m)^(version\s*=\s*")[^"]*(")', lambda m: m.group(1) + new + m.group(2), text, count=1)
        if n != 1:
            raise Fail(f"could not set version in {rel}")
        open(p, "w").write(text)


def apply(root, p):
    if git("status", "--porcelain").strip():
        raise Fail("working tree not clean: commit or move your changes first")
    if p["commits"] == 0 and p["bump"] != "first":
        raise Fail(f"no commits since {p['last_tag']}")
    tag = f"v{p['next']}"
    if git("tag", "-l", tag).strip():
        raise Fail(f"tag {tag} already exists")
    body = notes(p)
    path = os.path.join(root, "CHANGELOG.md")
    head = "# Changelog\n\nAll notable changes to this project. Format: Keep a Changelog; versions: SemVer.\n\n"
    old = open(path).read() if os.path.exists(path) else head
    if not old.startswith("# Changelog"):
        old = head + old
    pos = old.find("\n## ")
    new = (old[:pos + 1] + body + "\n" + old[pos + 1:]) if pos >= 0 else old.rstrip("\n") + "\n\n" + body
    open(path, "w").write(new)
    set_versions(root, p["files"], p["next"])
    git("add", "CHANGELOG.md", *p["files"])
    git("commit", "-q", "-m", f"chore(release): {tag}")
    git("tag", "-a", tag, "-m", f"{tag}\n\n{body}")
    return {"ok": True, "tag": tag, "commit": git("rev-parse", "--short", "HEAD").strip(),
            "changelog": "CHANGELOG.md", "files": p["files"]}


def main(argv):
    if argv[:1] == ["--self-test"]:
        return self_test()
    cmd = argv[0] if argv else "plan"
    forced = argv[argv.index("--version") + 1] if "--version" in argv else None
    try:
        root = git("rev-parse", "--show-toplevel").strip()
        p = plan(root, forced)
        if cmd == "plan":
            print(json.dumps({k: v for k, v in p.items() if k != "rows"}))
        elif cmd == "notes":
            print(notes(p))
        elif cmd == "apply":
            print(json.dumps(apply(root, p)))
        else:
            raise Fail(f"unknown command {cmd!r} (plan | notes | apply)")
    except Fail as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    return 0


def self_test():
    tmp = tempfile.mkdtemp(prefix="mk-release-")
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        os.makedirs(".claude-plugin")
        json.dump({"name": "x", "version": "0.1.0"}, open(".claude-plugin/plugin.json", "w"), indent=2)

        def commit(msg, body=""):
            open("f.txt", "a").write(msg + "\n")
            git("add", "-A")
            git("commit", "-q", "-m", msg + (f"\n\n{body}" if body else ""))

        commit("feat(x): first thing")
        commit("fix: a bug")
        p = plan(tmp)
        assert p["last_tag"] is None and p["next"] == "0.1.0" and p["bump"] == "first", p
        r = apply(tmp, p)
        assert r["tag"] == "v0.1.0" and git("tag", "-l", "v0.1.0").strip() == "v0.1.0", r
        log = open("CHANGELOG.md").read()
        assert "## [0.1.0]" in log and "### Added" in log and "**x:** first thing" in log and "### Fixed" in log, log
        assert "chore(release)" not in notes(plan(tmp))  # release commit is "other", listed only next time

        commit("fix: second bug")
        p = plan(tmp)
        assert p["next"] == "0.1.1" and p["bump"] == "patch", p
        commit("feat: new thing")
        assert plan(tmp)["next"] == "0.2.0"
        commit("refactor!: drop old api")
        assert plan(tmp)["next"] == "0.2.0"  # one bump per release; breaking while 0.x → minor
        r = apply(tmp, plan(tmp))
        assert r["tag"] == "v0.2.0" and json.load(open(".claude-plugin/plugin.json"))["version"] == "0.2.0"
        log = open("CHANGELOG.md").read()
        assert log.index("## [0.2.0]") < log.index("## [0.1.0]") and "### Breaking" in log, log

        json.dump({"name": "x", "version": "1.4.2"}, open(".claude-plugin/plugin.json", "w"))
        git("commit", "-qam", "chore: set 1.4.2")
        git("tag", "-a", "v1.4.2", "-m", "v1.4.2")
        commit("fix: z", "BREAKING CHANGE: behaviour changed")
        assert plan(tmp)["next"] == "2.0.0"
        assert plan(tmp, "1.9.0")["next"] == "1.9.0"

        open("dirty.txt", "w").write("x")
        try:
            apply(tmp, plan(tmp))
            raise AssertionError("dirty tree must be refused")
        except Fail as e:
            assert "not clean" in str(e)
        os.remove("dirty.txt")
        json.dump({"name": "x", "version": "9.9.9"}, open("package.json", "w"))
        try:
            plan(tmp)
            raise AssertionError("disagreeing version files must be refused")
        except Fail as e:
            assert "disagree" in str(e)
        print("mk-release self-test ok")
        return 0
    finally:
        os.chdir(cwd)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
