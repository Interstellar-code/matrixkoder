+++
# Parked. Not imported, not approved. Pick up after phase F and L10.
# Skills already written: skills/mk-ponytail, skills/mk-ponytail-review, skills/mk-ponytail-debt.
# Grilled 2026-10-16 from plan.md; every decision below was settled with the plan owner.
plan = "mk-ponytail-hooks"
status = "parked"
created = 2026-10-15
runs = []
design = []

[gates]
selftest = "python3 scripts/mk-lane.py --self-test"
builder_selftest = "python3 child-skills/mk-builder/scripts/mk-builder.py --self-test"
hook_test = "bun test child-skills/omp-hooks"

[[final_gate]]
id = "F1"
cmd = "python3 scripts/mk-lane.py --self-test"
expect = 0
every = "merge"
covers = ["H0", "H1", "H2", "H3a", "H3b"]

[[final_gate]]
id = "F1b"
cmd = "python3 child-skills/mk-builder/scripts/mk-builder.py --self-test"
expect = 0
every = "merge"
covers = ["H2", "H3a", "H3b"]

[[final_gate]]
id = "F1c"
cmd = "bun test child-skills/omp-hooks"
expect = 0
every = "merge"
covers = ["H0", "H1"]
+++

# Goal

Install the mk-ponytail ruleset into an omp lane so a builder-role agent follows it without the orchestrator restating it in a brief. A commit then triggers an advisory in-pane ponytail review and a debt grep. omp lanes only. Other hosts later.

Settled:

- Core 3 only: mode (`mk-ponytail`), review (`mk-ponytail-review`), debt (`mk-ponytail-debt`). No audit, gain, or help.
- The mode is `session.ponytail.mode`: `off` | `lite` | `full` | `ultra`. A missing key means `full`. No `plugin.json` `userConfig` key.
- The project yaml is found with `mkpaths.project_yaml(data)`: `<data>/matrixkoder.yaml`, or `project.yaml` for a migrated project. The earlier `.matrixkoder/matrixkoder.yaml` path in the first draft was wrong.
- The debt ledger matches both `shortcut:` and `# ponytail:`.
- `templates/brief.md`, `templates/matrixkoder.yaml`, and `skills/matrixkoder/SKILL.md` are not edited by this plan.
- Claude, OpenCode, and AGY lanes do not load omp hooks and are unchanged.

# Design

omp discovers hooks from `<cwd>/.omp/hooks/pre|post/*.{ts,js}`, and only those two subdirectories. `install_lane_skill` (`scripts/mk-lane.py:1072`) copies `child-skills/mk-builder/` into `<lane>/.omp/skills/mk-builder` and `<lane>/.claude/skills/mk-builder` on every acquire. A hook is not a skill, so that copy does not install it. `DISPOSABLE` (`scripts/mk-lane.py:60`) already lists `.omp/`, so the hook dir needs no new disposable entry; verify this when H1 is built.

Injection. omp's `session_start` handler returns nothing that can inject text. The hook uses `before_agent_start`, which returns `{message}`, once per session, and injects again after compaction. It injects the `mk-ponytail` ruleset for the configured level, filtered the way upstream `hooks/ponytail-instructions.js` `filterSkillBodyForMode` does.

Role. The hook injects only into builder-role sessions. A reviewer pane in the same slot gets nothing, because reviewers judge the diff and write no code.

Mode source. The hook is TypeScript and `mkpaths` is Python, so the hook shells out to a new `mk-builder.py ponytail-mode` subcommand. It reuses `mkpaths_candidates`, prints `off|lite|full|ultra`, and reads the yaml live on each call. One resolver, no second copy of the path logic.

Hook source and install. The hook lives in a separate source dir, `child-skills/omp-hooks/pre/`, with its own `HOOKS_SRC` constant in `mk-lane.py`. It does not live under `child-skills/mk-builder/`, because the skill copy would then put a dead omp hook into every Claude lane. `install_lane_skill` copies it to `<lane>/.omp/hooks/pre/` on every acquire, overwriting, with the same hard-fail when the source is missing and the same no-partial-install rollback as the skill copy.

Subagents. Whether omp loads project hooks inside subagents a lane spawns is not known. H0 proves it. Until it does, the plan makes no subagent-injection claim.

# Contract

- `off` injects nothing, and `mk-builder.py commit` behaves exactly as it does today.
- A missing `session.ponytail` key injects `full`.
- The hook never writes the yaml and never writes a mode flag file.
- A reviewer-role session gets no injection.
- A lane that is not omp is unchanged. `.claude/skills/mk-builder` carries no hook.
- The ponytail review is advisory. It never gives a verdict, never replaces or skips the AGY review, and runs before it. The lane applies the fixes before hand-off; the orchestrator does not edit the lane.
- R30 holds: nothing goes lane to lane. The bus message is lane to orchestrator, for observation.

# Commit flow

The only trigger is `mk-builder.py commit`. There is no `tool_result` hook on `git commit`. Every host and lane already calls `commit` to announce the sha, so the flow needs no new hook surface. A lane that skips `commit` gets no review, the same failure as skipping the bus announce today.

On the first `commit` call of a task round, with `ponytail.mode` not `off`:

1. It prints the debt grep: the lane diff against the merge base, matching `shortcut:` and `# ponytail:`. This is a plain grep with no model.
2. Unless `session.subagents` is `false`, it prints the order to start the ponytail subagent in the pane's own session.
3. It does not send the `commit` bus message yet.

Then the lane:

4. Runs the subagent, which runs `mk-ponytail-review` and `mk-ponytail-debt` and returns the report into the pane.
5. Reads the report and fixes the findings.
6. Commits again and calls `commit` again. This call announces the final sha.
7. Posts a bus message of kind `ponytail` carrying the review number and the findings count.

Rules:

- At most one ponytail review per task round. A second `commit` call in the same round prints the debt grep only, with no review order, and announces the sha even if the review was skipped. The announce is never blocked twice.
- The review does not count against the fix-loop budget (3 loops on a HIGH).
- The counter lives in the lane status file. There is no mk-state schema change; the orchestrator counts `ponytail` bus messages for a total.
- `session.subagents: false` suppresses only the review order. The debt grep still runs.
- The hook writes no report and posts nothing. The hook cannot spawn the subagent; `mk-builder.py commit` only prints the order, and the pane agent starts it and posts the message.

# Risks

- `install_lane_skill` treats `.omp/` as disposable and replaces the skill dir wholesale. The hook install is in the same function, so a later acquire refreshes it. A release must not delete a hook the slot still needs; check this against the release path.
- omp's native discovery scans only `hooks/pre/` and `hooks/post/`, not `hooks/` itself.
- If `before_agent_start` does not re-fire after compaction as assumed, the rules disappear mid-lane. H0 checks this.
- The `connect` fallback in H3b is the only subagent coverage if H0 shows hooks do not load in subagents.

## H0 — proof (gates H1 and H3b)

- Show with a throwaway omp session that a hook in `<cwd>/.omp/hooks/pre/` returning `{message}` from `before_agent_start` reaches the model, and that it fires again after compaction.
- Show whether a subagent spawned by that session loads the same project hook.
- Save the outputs under the task directory. Nothing in the repo changes.
- The result decides H3b: if subagents load the hook, the `connect` fallback is dropped; if not, it stays.

## H1 — hook and install

- Add `child-skills/omp-hooks/pre/mk-ponytail.ts`: on `before_agent_start`, once per session and again after compaction, call `mk-builder.py ponytail-mode`, and for builder role and a mode other than `off`, return the ruleset for that level.
- Add `HOOKS_SRC` to `mk-lane.py`. `install_lane_skill` copies the hook to `<lane>/.omp/hooks/pre/mk-ponytail.ts` on every acquire, overwriting, with a hard Fail when the source is missing and rollback of a partial install.
- `mk-lane.py --self-test` asserts the file lands in the omp slot, is absent when the source is missing (acquire refuses), and that `.claude/skills/mk-builder` has no hook.
- Bun hook test: `off` injects nothing; a missing key injects the `full` ruleset; `ultra` injects the ultra row only; a reviewer role injects nothing.

## H2 — ponytail-mode subcommand

- Add `mk-builder.py ponytail-mode`: resolve the project yaml through `mkpaths_candidates` and `project_yaml`, print `off|lite|full|ultra`, `full` when the key is missing. Read-only.
- `mk-builder.py --self-test` covers `off`, `lite`, `full`, `ultra`, a missing key, a missing yaml, and a migrated project.

## H3a — commit: debt grep, review order, counter

- `mk-builder.py commit` behaves as in Commit flow: first call per task round prints the debt grep and the review order and withholds the `commit` bus message; later calls print the debt grep only and announce.
- The per-round review counter lives in the lane status file.
- `ponytail.mode: off` leaves `commit` exactly as today. `session.subagents: false` drops the review order only.
- Self-test: `off` unchanged; first call withholds the announce; second call announces; one review per round; the counter increments; the grep matches both markers in a lane diff and ignores markers already on the merge base.

## H3b — bus kind and connect fallback (after H0)

- Add the `ponytail` bus message kind carrying the review number and the findings count. The lane posts it after fixing, and the lane-file fallback applies when the bus is down.
- Only if H0 shows subagents do not load the hook: `mk-builder.py connect` prints, for builder-role sessions, the instruction to pass the ponytail rules to the lane's subagents. `templates/brief.md` stays unedited.
- Self-test: the kind round-trips; the fallback text appears only for builder role and only when the mode is not `off`.

## F1 — final gates

- Run `mk-lane.py --self-test`, `mk-builder.py --self-test`, and the bun hook test (F1, F1b, F1c above).
- Plan-vs-delivery check: each Contract line has a test, and the settled list in Goal matches the diff.

# Dependencies

- Pick up after phase F and L10.
- H0 gates H1 and H3b. H2 gates H1 (the hook calls the subcommand) and H3a (the grep reads the mode). F1 follows all of them.
