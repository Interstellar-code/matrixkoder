---
name: release
description: Cut a release with a changelog — version bump, CHANGELOG.md entry and git tag computed from Conventional Commits by MatrixKoder's mk-release.py, then (only after the user confirms) push and publish a GitHub release. Use when the user says "release", "cut a release", "tag a version", "bump the version", "update the changelog", "publish v…", or when an MK run has merged into main and the user wants it shipped. Works in any git repo whose commits follow Conventional Commits.
---

# Release

One script does the bookkeeping; you do the checks and ask before anything leaves the machine.

Script: `${CLAUDE_PLUGIN_ROOT}/scripts/mk-release.py` (stdlib Python, run from inside the repo). Every command prints one JSON line except `notes` (markdown).

| Command | Does | Writes |
|---|---|---|
| `mk-release.py plan [--version X.Y.Z]` | last `v*` tag, commits since, current → next version, bump kind, version files found | nothing |
| `mk-release.py notes [--version X.Y.Z]` | the changelog section for the next version | nothing (stdout) |
| `mk-release.py apply [--version X.Y.Z]` | prepends `CHANGELOG.md`, sets the version in `.claude-plugin/plugin.json` / `package.json` / `pyproject.toml` (those that exist), commits `chore(release): vX.Y.Z`, creates annotated tag `vX.Y.Z` | local commit + tag only, **never pushes** |

Version rule: breaking (`type!:` or `BREAKING CHANGE` in a body) → major (minor while `0.x`), any `feat` → minor, else patch. First release of a repo with no `v*` tag keeps the version the files already declare. `--version` overrides.

## Steps

1. **Preflight** — clean tree (`git status --porcelain` empty), on the default branch, up to date with the remote (`git fetch` + compare), the repo's own gates green (from `<data>/matrixkoder.yaml` `gates` when MK is set up, else the project's test command). Stop and report on any failure.
2. **Plan** — run `plan` and `notes`. Show the user: current → next version, bump kind, commit count, and the notes. Point out commits that are not Conventional (they land under "Other") and any that look mis-typed (a `fix` that adds a feature, a missing `!` on a breaking change); offer `--version` instead of rewriting history.
3. **Confirm** — ask the user to approve the version (or give one). Never pick a major bump on your own.
4. **Apply** — `apply [--version …]`. Report the commit, tag and changed files. Read the new `CHANGELOG.md` section once; fix wording only by a follow-up `docs(changelog):` commit before pushing — never amend, never move the tag.
5. **Push — ask first** (outward action): `git push origin <default-branch>` and `git push origin vX.Y.Z`.
6. **GitHub release — ask first**: `mk-release.py notes --version X.Y.Z > <scratchpad>/notes-vX.Y.Z.md` (run before step 4's commit lands, or strip the release commit line), then `gh release create vX.Y.Z --title vX.Y.Z --notes-file <that file>`. Private repos: same command; the release stays private with the repo.

## Rules

- Commit subjects follow Conventional Commits (`type(scope)!: description`); MK briefs already require it. The changelog is only as good as the subjects.
- Never delete, move or re-create a pushed tag; a bad release is fixed by the next patch release.
- Never force-push, never `--amend` the release commit.
- `apply` refuses a dirty tree, an existing tag, no new commits, and version files that disagree — fix the cause, don't work around it.
- In an MK run: release only after stage 12 (merged to main, gates green), as its own step with the user's go-ahead.
