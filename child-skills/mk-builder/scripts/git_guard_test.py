#!/usr/bin/env python3
"""git_guard_test.py — tests for the mk-builder lane `git` wrapper (`reference/git-guard.md`).

Every case runs against a temp git repo under one temp root: this worktree's own `.git` is
never touched, and a refused form is never run against it. The wrapper is installed as `git` in
`<root>/bin`, which is placed first on PATH (G1).

    python3 child-skills/mk-builder/scripts/git_guard_test.py [--self-test]

Prints one line per case and a final `ok <n> cases`; exit 0 only when every case passed.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER_SRC = os.path.join(HERE, "git-guard")
PREFIX = "mk git-guard: "


# ---------- harness ----------

class Case:
    """Fail now with a readable message; a crash inside a case is a failed case, not a stop."""

    def __init__(self, root):
        self.root = root
        self.bin = os.path.join(root, "bin")
        self.real_git = shutil.which("git")
        self.home = os.path.join(root, "home")
        os.makedirs(self.bin, exist_ok=True)
        os.makedirs(self.home, exist_ok=True)
        shutil.copy2(WRAPPER_SRC, os.path.join(self.bin, "git"))
        os.chmod(os.path.join(self.bin, "git"), 0o755)

    def check(self, cond, msg):
        if not cond:
            raise AssertionError(msg)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def env(self, wrapper=True, extra_path=None, path_override=None):
        """A hermetic env: no ambient git config, no ambient HOME, no repo above the temp root
        (GIT_CEILING_DIRECTORIES), and the wrapper's directory first on PATH (G1)."""
        e = dict(os.environ)
        e.update(PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_GLOBAL=os.devnull,
                 GIT_CONFIG_SYSTEM=os.devnull, HOME=self.home, GIT_EDITOR=":",
                 GIT_MERGE_AUTOEDIT="no", GIT_ASKPASS="/bin/echo", GIT_PAGER="cat", GIT_CEILING_DIRECTORIES=self.root, LC_ALL="C")
        if path_override is not None:
            e["PATH"] = path_override
        else:
            head = [self.bin] if wrapper else []
            head += [extra_path] if extra_path else []
            e["PATH"] = os.pathsep.join(head + [os.environ.get("PATH", "")])
        return e

    def run(self, cwd, args, wrapper=True, extra_path=None, path_override=None):
        cmd = ([os.path.join(self.bin, "git")] if wrapper else [self.real_git]) + list(args)
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
                           env=self.env(wrapper, extra_path, path_override))
        return p.returncode, p.stdout, p.stderr

    def git(self, repo, *args, check=True):
        """Real git, never through the wrapper — this is test setup, not the system under test."""
        p = subprocess.run([self.real_git, "-C", repo, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=120, env=self.env(wrapper=False))
        if check and p.returncode != 0:
            raise AssertionError(f"setup git {' '.join(args)} failed: {p.stderr.strip()}")
        return p.stdout.strip()

    # --- repos ---

    def repo(self, name, setup=None):
        """A fresh temp git repo with one commit. A `setup` callback owns the whole repo, so each
        case gets its own directory and no case can see another's leftovers."""
        if setup:
            return setup(self, name)
        repo = self.base_repo(name)
        return repo

    def base_repo(self, name):
        repo = os.path.join(self.root, name)
        os.makedirs(repo, exist_ok=True)
        self.git(repo, "init", "-q")
        self.git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
        self.write(repo, "README", "hello\n")
        self.git(repo, "add", "README")
        self.git(repo, "commit", "-q", "-m", "init")
        return repo

    def write(self, repo, name, text):
        path = os.path.join(repo, name)
        os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def snapshot(self, repo):
        """Every file under the repo with its bytes — the proof that a refusal changed nothing."""
        out = {}
        for d, dirs, files in os.walk(repo):
            dirs.sort()
            for name in sorted(files):
                p = os.path.join(d, name)
                with open(p, "rb") as f:
                    out[os.path.relpath(p, repo)] = hashlib.sha256(f.read()).hexdigest()
        return out

    def restore(self, repo, backup):
        shutil.rmtree(repo)
        shutil.copytree(backup, repo, symlinks=True)

    # --- the two assertions the contract makes ---

    def refused(self, repo, args, gn, cwd=None):
        """G22: exit 126, exactly one stdout line naming git-guard.md <gn>, no stderr, and not one
        byte of the repo changed (G2)."""
        self.git(repo, "status", "--porcelain")  # warm the index so it is not a false diff
        before, head0, st0 = self.snapshot(repo), self.git(repo, "rev-parse", "HEAD"), \
            self.git(repo, "status", "--porcelain")
        code, out, err = self.run(cwd or repo, args)
        after, head1, st1 = self.snapshot(repo), self.git(repo, "rev-parse", "HEAD"), \
            self.git(repo, "status", "--porcelain")
        self.check(code == 126, f"expected exit 126 from git {' '.join(args)}, got {code}: {out!r}")
        lines = [x for x in out.splitlines() if x.strip()]
        self.check(len(lines) == 1, f"expected exactly one stdout line, got {lines}")
        self.check(lines[0].startswith(f'{PREFIX}refused "git '),
                   f"not a refusal line: {lines[0]!r}")
        self.check(f"git-guard.md {gn}" in lines[0],
                   f"expected git-guard.md {gn} in {lines[0]!r}")
        self.check("lane-rules.md R2" in lines[0], f"the line must name the lane rule: {lines[0]!r}")
        self.check(err == "", f"nothing goes to stderr on a refusal: {err!r}")
        self.check(before == after, f"the repo changed on a refused {args}")
        self.check(head0 == head1 and st0 == st1, f"HEAD or status moved on a refused {args}")
        return lines[0]

    def passes_through(self, repo, args, expect=0):
        """G23: the wrapper runs the real git, so its stdout is byte-identical to the same command
        run without the wrapper, and its exit code is the real git's."""
        self.git(repo, "status", "--porcelain")  # warm the index, or `status` output may differ
        backup = self.path("backup")
        if os.path.isdir(backup):
            shutil.rmtree(backup)
        shutil.copytree(repo, backup, symlinks=True)
        w_code, w_out, _ = self.run(repo, args)
        self.restore(repo, backup)
        g_code, g_out, _ = self.run(repo, args, wrapper=False)
        self.check(w_out == g_out,
                   f"stdout differs for git {' '.join(args)}:\n wrapper {w_out!r}\n real    {g_out!r}")
        self.check(w_code == g_code,
                   f"exit code differs for git {' '.join(args)}: {w_code} vs {g_code}")
        if expect is not None:
            self.check(w_code == expect,
                       f"git {' '.join(args)} should succeed in a prepared repo, got {w_code}")
        self.check(f"{PREFIX}refused" not in w_out,
                   f"git {' '.join(args)} must not be refused: {w_out!r}")


# ---------- setup states ----------

def setup_modified(c, name):
    repo = c.base_repo(name)
    c.write(repo, "README", "changed\n")
    return repo


def setup_staged(c, name):
    repo = c.base_repo(name)
    c.write(repo, "README", "staged change\n")
    c.git(repo, "add", "README")
    return repo


def setup_stashed(c, name):
    repo = c.base_repo(name)
    c.write(repo, "README", "work in progress\n")
    c.git(repo, "stash", "push", "-q", "-m", "wip")
    c.write(repo, "README", "hello\n")
    return repo


def setup_branch(c, name):
    """A repo with a merged branch and a tag — the look-alikes G19 allows."""
    repo = c.base_repo(name)
    c.git(repo, "tag", "v1")
    c.git(repo, "branch", "merged")
    return repo


def setup_feature(c, name):
    repo = c.base_repo(name)
    c.git(repo, "branch", "other")
    return repo


def setup_patch_applied(c, name):
    repo = c.base_repo(name)
    c.git(repo, "apply", c.patch_path())
    return repo


def setup_conflict(c, name):
    """A rebase in progress with one conflicting file — G14's abort/continue/skip all need one,
    or real git would fail and the pass-through comparison would prove nothing."""
    repo = c.base_repo(name)
    c.git(repo, "checkout", "-q", "-b", "feature")
    c.write(repo, "README", "feature line\n")
    c.git(repo, "commit", "-q", "-am", "feature")
    c.git(repo, "checkout", "-q", "main")
    c.write(repo, "README", "main line\n")
    c.git(repo, "commit", "-q", "-am", "main")
    c.git(repo, "checkout", "-q", "feature")
    c.git(repo, "rebase", "main", check=False)
    return repo


def setup_conflict_resolved(c, name):
    repo = setup_conflict(c, name)
    c.git(repo, "checkout", "--ours", "README")
    c.git(repo, "add", "README")
    return repo


def setup_pushed(c, name):
    """A repo whose branch tracks a bare origin it is exactly in sync with. Every remote case
    starts here and then moves one side, so push/pull/fetch each have a defined state."""
    bare = os.path.join(c.root, name + "-origin.git")
    subprocess.run([c.real_git, "init", "-q", "--bare", "-b", "main", bare], check=True,
                   capture_output=True, stdin=subprocess.DEVNULL, env=c.env(wrapper=False))
    repo = c.base_repo(name)
    c.git(repo, "remote", "add", "origin", bare)
    c.git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def setup_remote(c, name):
    """The origin one commit ahead, so `pull` fast-forwards and `fetch` has something to do."""
    repo = setup_pushed(c, name)
    other = os.path.join(c.root, name + "-other")
    subprocess.run([c.real_git, "clone", "-q", os.path.join(c.root, name + "-origin.git"), other],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL,
                   env=c.env(wrapper=False))
    c.write(other, "README", "from origin\n")
    subprocess.run([c.real_git, "-C", other, "commit", "-q", "-am", "origin commit"], check=True,
                   capture_output=True, stdin=subprocess.DEVNULL, env=c.env(wrapper=False))
    subprocess.run([c.real_git, "-C", other, "push", "-q"], check=True, capture_output=True,
                   stdin=subprocess.DEVNULL, env=c.env(wrapper=False))
    return repo


def setup_ahead(c, name):
    """One local commit on top of a synced origin, so a plain `git push` fast-forwards and
    succeeds — the look-alike G19 allows (hooks/guards.ts:190-199)."""
    repo = setup_pushed(c, name)
    c.write(repo, "README", "local change\n")
    c.git(repo, "commit", "-q", "-am", "local change")
    return repo


def setup_merge(c, name):
    repo = c.base_repo(name)
    c.git(repo, "checkout", "-q", "-b", "topic")
    c.write(repo, "README", "topic\n")
    c.git(repo, "commit", "-q", "-am", "topic")
    c.git(repo, "checkout", "-q", "main")
    return repo


def make_patch(c):
    """A patch that adds a line to README — the old-code proof's `apply` / `apply -R` (G19)."""
    path = c.path("demo.patch")
    with open(path, "w") as f:
        f.write("diff --git a/README b/README\n--- a/README\n+++ b/README\n"
                "@@ -1 +1,2 @@\n hello\n+added by patch\n")
    return path


def patch_path(self):
    if not os.path.isfile(self.path("demo.patch")):
        make_patch(self)
    return self.path("demo.patch")


Case.patch_path = patch_path


# ---------- refused forms: G7-G18, one case per rule ----------

REFUSED = [
    # G7 checkout / reset / clean
    ("g7_checkout_branch", ["checkout", "-b", "other"], "G7", None),
    ("g7_checkout_path", ["checkout", "HEAD~1", "--", "README"], "G7", None),
    ("g7_checkout_global_opt", ["-C", ".", "checkout", "--", "README"], "G7", None),
    ("g7_reset_hard", ["reset", "--hard"], "G7", None),
    ("g7_reset_mixed", ["reset"], "G7", None),
    ("g7_clean", ["clean", "-fd"], "G7", None),
    ("g7_clean_all", ["clean", "-fdx"], "G7", None),
    # G8 stash, except list and show
    ("g8_stash", ["stash"], "G8", None),
    ("g8_stash_pop", ["stash", "pop"], "G8", None),
    ("g8_stash_push", ["stash", "push", "-m", "wip"], "G8", None),
    ("g8_stash_drop", ["stash", "drop"], "G8", None),
    ("g8_stash_after_c", ["-c", "user.name=t", "stash"], "G8", None),
    # G9 restore without --staged, or with --worktree
    ("g9_restore_plain", ["restore", "README"], "G9", setup_modified),
    ("g9_restore_worktree", ["restore", "--worktree", "README"], "G9", setup_modified),
    ("g9_restore_short_worktree", ["restore", "-S", "-W", "README"], "G9", setup_staged),
    ("g9_restore_staged_and_worktree", ["restore", "--staged", "--worktree", "README"], "G9",
     setup_staged),
    # G10 switch with a force flag
    ("g10_switch_force", ["switch", "--force", "other"], "G10", setup_feature),
    ("g10_switch_discard", ["switch", "--discard-changes", "other"], "G10", setup_feature),
    ("g10_switch_short_force", ["switch", "-f", "other"], "G10", setup_feature),
    ("g10_switch_short_bundle", ["switch", "-fd", "other"], "G10", setup_feature),
    # G11 commit --amend, the prefix family
    ("g11_amend", ["commit", "--amend", "--no-edit"], "G11", None),
    ("g11_amend_am", ["commit", "--am"], "G11", None),
    ("g11_amend_ame", ["commit", "--ame"], "G11", None),
    ("g11_amend_amen", ["commit", "--amen"], "G11", None),
    # G12 push: every trigger
    ("g12_push_force", ["push", "--force"], "G12", None),
    ("g12_push_force_lease", ["push", "--force-with-lease"], "G12", None),
    ("g12_push_short_force", ["push", "-f"], "G12", None),
    ("g12_push_short_bundle", ["push", "-fd"], "G12", None),
    ("g12_push_plus_refspec", ["push", "+main:other"], "G12", None),
    ("g12_push_plus_long", ["push", "+refs/heads/main:refs/heads/other"], "G12", None),
    ("g12_push_delete", ["push", "--delete", "other"], "G12", None),
    ("g12_push_short_delete", ["push", "-d", "other"], "G12", None),
    ("g12_push_mirror", ["push", "--mirror"], "G12", None),
    ("g12_push_prune", ["push", "--prune"], "G12", None),
    ("g12_push_empty_source", ["push", ":refs/heads/other"], "G12", None),
    ("g12_push_force_after_dashdash", ["push", "--", "--force"], "G12", None),
    # G13 branch: uppercase -D, or delete + force
    ("g13_branch_delete_upper", ["branch", "-D", "other"], "G13", setup_feature),
    ("g13_branch_delete_force", ["branch", "--delete", "--force", "other"], "G13", setup_feature),
    ("g13_branch_short_delete_force", ["branch", "-d", "-f", "other"], "G13", setup_feature),
    # G14 rebase without abort/continue/skip
    ("g14_rebase_plain", ["rebase", "main"], "G14", None),
    ("g14_rebase_onto", ["rebase", "--onto", "main", "HEAD~1"], "G14", None),
    ("g14_rebase_interactive", ["rebase", "-i", "HEAD~1"], "G14", None),
    # G15 pull --rebase
    ("g15_pull_rebase", ["pull", "--rebase"], "G15", setup_remote),
    ("g15_pull_rebase_value", ["pull", "--rebase=true"], "G15", setup_remote),
    ("g15_pull_rebase_one", ["pull", "--rebase=1"], "G15", setup_remote),
    ("g15_pull_short_rebase", ["pull", "-r"], "G15", setup_remote),
    # G16 reflog expire / delete
    ("g16_reflog_expire", ["reflog", "expire", "--all"], "G16", None),
    ("g16_reflog_delete", ["reflog", "delete", "HEAD@{1}"], "G16", None),
    # G17 update-ref with a short d
    ("g17_update_ref_delete", ["update-ref", "-d", "refs/heads/other"], "G17", setup_feature),
    ("g17_update_ref_short_bundle", ["update-ref", "-fd", "refs/heads/other"], "G17", setup_feature),
    # G18 gc --prune, every value
    ("g18_gc_prune", ["gc", "--prune"], "G18", None),
    ("g18_gc_prune_now", ["gc", "--prune=now"], "G18", None),
    ("g18_gc_prune_never", ["gc", "--prune=never"], "G18", None),
]


# ---------- allowed forms: G19/G20 pass-through ----------

ALLOWED = [
    ("a_status", ["status"], None),
    ("a_status_porcelain", ["status", "--porcelain"], setup_modified),
    ("a_log", ["log", "--oneline"], None),
    ("a_diff", ["diff"], setup_modified),
    ("a_add", ["add", "README"], setup_modified),
    ("a_commit", ["commit", "-m", "a message"], setup_staged),
    ("a_apply", ["apply", None], None),  # None -> the patch path, filled in by the runner
    ("a_apply_reverse", ["apply", "-R", None], setup_patch_applied),
    ("a_stash_list", ["stash", "list"], setup_stashed),
    ("a_stash_show", ["stash", "show"], setup_stashed),
    ("a_restore_staged", ["restore", "--staged", "README"], setup_staged),
    ("a_restore_short_staged", ["restore", "-S", "README"], setup_staged),
    ("a_switch", ["switch", "merged"], setup_branch),
    ("a_branch_delete_merged", ["branch", "-d", "merged"], setup_branch),
    ("a_rebase_abort", ["rebase", "--abort"], setup_conflict),
    ("a_rebase_continue", ["rebase", "--continue"], setup_conflict_resolved),
    ("a_rebase_skip", ["rebase", "--skip"], setup_conflict),
    ("a_pull", ["pull"], setup_remote),
    ("a_fetch", ["fetch"], setup_remote),
    ("a_merge", ["merge", "topic"], setup_merge),
    ("a_gc", ["gc"], None),
    ("a_tag", ["tag"], setup_branch),
    ("a_worktree", ["worktree", "list"], None),
    ("a_rev_parse", ["rev-parse", "HEAD"], None),
    ("a_show", ["show"], None),
    ("a_reflog", ["reflog"], None),
    ("a_push", ["push"], setup_ahead),
]

# Not refused, but not required to succeed either: the wrapper must forward them verbatim, and
# the real git decides the exit code (G19's `-m '--amend'` message-text case, G15's
# `--rebase=false|no`, and G16's other reflog subcommands).
FORWARDED = [
    ("f_commit_message_is_amend_text", ["commit", "-m", "--amend"], None),
    ("f_pull_rebase_false", ["pull", "--rebase=false"], setup_remote),
    ("f_pull_rebase_no", ["pull", "--rebase=no"], setup_remote),
    ("f_reflog_show", ["reflog", "show"], None),
    ("f_switch_plain", ["switch", "other"], setup_feature),
    ("f_update_ref_plain", ["update-ref", "refs/heads/extra", "HEAD"], setup_feature),
]


# ---------- cases ----------

def case_refused_forms(c):
    for name, args, gn, setup in REFUSED:
        repo = c.repo(name.replace("_", "-"), setup)
        c.refused(repo, args, gn)


def case_allowed_forms(c):
    for name, args, setup in ALLOWED:
        repo = c.repo(name.replace("_", "-"), setup)
        args = [a if a is not None else c.patch_path() for a in args]
        c.passes_through(repo, args)


def case_forwarded_forms(c):
    for name, args, setup in FORWARDED:
        repo = c.repo(name.replace("_", "-"), setup)
        c.passes_through(repo, args, expect=None)


def case_subcommand_detection(c):
    """G3: `git -C <path> stash` and `git -c k=v stash` are refused as `git stash`; an unknown
    subcommand passes."""
    repo = c.repo("g3", setup_feature)
    c.refused(repo, ["-C", repo, "stash"], "G8")
    c.refused(repo, ["--git-dir", os.path.join(repo, ".git"), "checkout", "README"], "G7")
    c.refused(repo, ["--work-tree", repo, "reset", "--hard"], "G7")
    c.check(c.run(repo, ["frobnicate"])[0] != 126,
            "a subcommand the ban does not name must not be refused (G19)")


def case_wrapper_is_executable(c):
    """The acceptance line: the executable bit is part of the contract (G1, `ls -l`)."""
    for p in (WRAPPER_SRC, os.path.join(c.bin, "git")):
        c.check(os.access(p, os.X_OK), f"{p} is not executable")


def case_real_git_lookup_ignores_fixed_path(c):
    """G21, the fake-second-git case: a second `git` placed earlier on PATH is the one the wrapper
    runs, which is only possible if the lookup walks PATH — a hardcoded /usr/bin/git could never
    reach it. G21 says "the first remaining PATH entry holding an executable git", so this is what
    a faithful wrapper does."""
    fake = c.path("fakebin")
    os.makedirs(fake, exist_ok=True)
    marker = os.path.join(fake, "git")
    with open(marker, "w") as f:
        f.write("#!/bin/sh\nprintf 'FAKE GIT\\n'\nexit 0\n")
    os.chmod(marker, 0o755)
    repo = c.repo("fake")
    real_out = c.run(repo, ["rev-parse", "HEAD"], wrapper=False)[1]
    code, out, err = c.run(repo, ["rev-parse", "HEAD"], extra_path=fake)
    c.check(code == 0, f"the fake git should have run: {code} {err!r}")
    c.check(out.strip() == "FAKE GIT", f"expected the first PATH git to win, got {out!r}")
    c.check(out != real_out, "a fixed-path implementation would print the real git's output")
    # and the wrapper's own directory, reached through a symlink, is still dropped (G21 realpath)
    alias = c.path("aliasbin")
    os.symlink(c.bin, alias)
    path = os.pathsep.join([c.bin, alias, os.environ.get("PATH", "")])
    code, out, err = c.run(repo, ["rev-parse", "HEAD"], path_override=path)
    c.check(code == 0 and out == real_out,
            f"the real git must be reached through the symlinked wrapper dir: {code} {out!r} {err!r}")


def case_no_real_git(c):
    """G21: a PATH holding nothing but the wrapper's own directory has no entry left — exit 127,
    one line, and nothing in the repo touched."""
    repo = c.repo("nogit")
    before = c.snapshot(repo)
    code, out, err = c.run(repo, ["status"], path_override=c.bin)
    c.check(code == 127, f"expected exit 127, got {code}")
    c.check(out == f"{PREFIX}no real git on PATH\n", f"unexpected output {out!r}")
    c.check(err == "", f"nothing goes to stderr here either: {err!r}")
    c.check(c.snapshot(repo) == before, "the repo must be untouched")


def case_wrapper_writes_nothing(c):
    """G2: no log, no state file, no lock — the only effects are the refusal line and git's own."""
    repo = c.repo("nowrite", setup_feature)
    before = c.snapshot(repo)
    c.refused(repo, ["stash"], "G8")
    c.refused(repo, ["clean", "-fd"], "G7")
    c.check(c.snapshot(repo) == before, "a refusal must leave no trace at all")


CASES = [
    ("refused_forms", case_refused_forms),
    ("allowed_forms", case_allowed_forms),
    ("forwarded_forms", case_forwarded_forms),
    ("subcommand_detection", case_subcommand_detection),
    ("wrapper_is_executable", case_wrapper_is_executable),
    ("real_git_lookup_ignores_fixed_path", case_real_git_lookup_ignores_fixed_path),
    ("no_real_git", case_no_real_git),
    ("wrapper_writes_nothing", case_wrapper_writes_nothing),
]


def main(argv):
    if argv and argv[0] not in ("--self-test", "-h", "--help"):
        print(f"usage: {os.path.basename(__file__)} [--self-test]")
        return 2
    root = tempfile.mkdtemp(prefix="mk-git-guard-test-")
    failed = []
    try:
        c = Case(root)
        for name, fn in CASES:
            try:
                fn(c)
                print(f"ok {name}")
            except Exception as e:
                failed.append(name)
                print(f"not ok {name}: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if failed:
        print(f"{len(failed)} of {len(CASES)} cases failed: {', '.join(failed)}")
        return 1
    print(f"ok {len(CASES)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))