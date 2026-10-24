+++ 1 Top block — plan identity [toml]
# Parked. Not imported, not approved. Pick up later.
# Skills already written: skills/mk-ponytail, skills/mk-ponytail-review, skills/mk-ponytail-debt.
plan = "mk-ponytail-hooks"
status = "parked"
created = 2026-10-15
runs = []
/+++

+++ 3 Goal
## Goal

Install the mk-ponytail ruleset into a lane so the lane agent follows it, without the orchestrator restating it in a brief. omp lanes first. Other hosts later.

Settled already (grilling, 2026-10-15):

- Core 3 only: mode (`mk-ponytail`), review (`mk-ponytail-review`), debt (`mk-ponytail-debt`). No audit, gain, or help.
- Mode lives in `.matrixkoder/matrixkoder.yaml` under `session.ponytail.mode`: `off` | `lite` | `full` | `ultra`. Missing key means `full`. No `plugin.json` `userConfig` key.
- Debt ledger greps both `shortcut:` and `# ponytail:`.
- `templates/brief.md`, `templates/matrixkoder.yaml`, and `skills/matrixkoder/SKILL.md` are not edited by this plan.
/+++

+++ 4 Design
## Design

omp discovers hooks from `<cwd>/.omp/hooks/pre|post/*.{ts,js}` (`omp://hooks.md`). `mk-lane.py` `install_lane_skill` already copies `child-skills/mk-builder/` into `<lane>/.omp/skills/mk-builder` and `<lane>/.claude/skills/mk-builder` on every acquire (`scripts/mk-lane.py:1006-1061`). A hook factory is not a skill, so that copy does not install it. The hook has to be copied to `<lane>/.omp/hooks/pre/` by the same acquire.

The hook reads `session.ponytail.mode` from the project yaml and, unless `off`, injects the `mk-ponytail` ruleset for that level on `session_start` (and into subagents the lane spawns). `lite` / `full` / `ultra` filter the same way upstream `hooks/ponytail-instructions.js` `filterSkillBodyForMode` does.

Claude, OpenCode, and AGY lanes do not load omp hooks. They are out of this plan.

## Contract

- `off` injects nothing.
- A missing `session.ponytail` key injects `full`.
- The hook never writes the yaml and never writes a mode flag file.
- A lane that is not omp is unchanged.
/+++

+++ 7 Risks
## Risks

- `install_lane_skill` treats `.omp/` as disposable and replaces the skill dir wholesale. The hook dir must be installed by the same function or a later acquire will not refresh it, and a release must not delete a hook the slot still needs. Check `DISPOSABLE` at `scripts/mk-lane.py:60-62` before adding a path.
- omp's native discovery only scans `hooks/pre/` and `hooks/post/`, not `hooks/` itself.

## H1 — copy the hook into the lane

- Add `child-skills/mk-builder/hooks/pre/mk-ponytail.ts`: on `session_start`, read `session.ponytail.mode`, inject the ruleset for that level.
- `install_lane_skill` copies it to `<lane>/.omp/hooks/pre/mk-ponytail.ts` on every acquire, same overwrite rule as the skill.
- `mk-lane.py --self-test` asserts the file lands in the omp slot and is absent when the source is missing.

## H2 — mode from the yaml

- The hook resolves the project yaml the way `mkpaths` does, not by a hardcoded path.
- Self-test: `off` injects nothing; missing key injects the `full` ruleset; `ultra` injects the ultra row only.

## H3 — commit triggers an in-pane ponytail subagent

Settled conceptually (user, 2026-10-16). H1/H2 put the mode into the lane; H3 runs the review and the debt ledger after a commit.

Flow, entirely inside the one pane that owns the lane:

1. The pane agent finishes a task and commits.
2. The hook fires on that commit and tells the pane agent to start the ponytail subagent.
3. The pane agent starts the subagent in its own pane. The subagent runs `mk-ponytail-review` (and `mk-ponytail-debt`) and returns the report.
4. The report lands back in the pane; the pane agent reads it.
5. The pane agent posts the report on the bus so the orchestrator sees it later.
6. The task records that a ponytail review ran, and how many times (confirm whether a review-count field already exists before adding one).

Who acts:

- The lane is the only actor on the findings; it applies the fixes. The orchestrator does not edit the lane.
- The bus copy is for the orchestrator to observe, not to act on.
- R30 still holds: nothing goes lane-to-lane; the bus message is lane → orchestrator.

Facts, not blockers:

- The hook can order the pane agent to start the subagent; it cannot spawn the subagent itself. In an omp lane, `additionalContext` on the commit carries that order.
- The subagent must run in the pane's own session so its report returns to that pane.
- `mk-ponytail-debt` is a grep with no model, so `mk-builder.py commit` can print it directly. `mk-ponytail-review` needs the lane model; it is the part that wants the subagent.
- The hook writes no report and posts nothing; the pane agent does both after the subagent returns.
/+++

+++ 8 Open questions
## Open questions

- [ ] Q1 — trigger point: an omp `tool_result` hook on `git commit`, or `mk-builder.py commit` (every host), or both — blocks: none — pick: both, with the commit command the portable one
- [ ] Q2 — review before or after the lane posts its own `commit` bus message — blocks: none — pick: after, so the orchestrator reads one message with the report on it
- [ ] Q3 — whether a review run counts against the task's fix-loop budget (3 loops on a HIGH) — blocks: none — pick: no, a review is not a fix round
/+++
