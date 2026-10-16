+++
# Parked. Not imported, not approved. Pick up later.
# Skills already written: skills/mk-ponytail, skills/mk-ponytail-review, skills/mk-ponytail-debt.
# Consensus rewrite of plan.md (ralplan solo: Planner, Architect, Critic by one session; owner answered 3 decisions 2026-10-16).
plan = "mk-ponytail-hooks"
status = "parked"
created = 2026-10-15
runs = []
design = []

[gates]
selftest = "python3 scripts/mk-lane.py --self-test"
builder = "python3 child-skills/mk-builder/scripts/mk-builder.py --self-test"

[[final_gate]]
id = "F1"
cmd = "python3 scripts/mk-lane.py --self-test"
expect = 0
every = "merge"
covers = ["H0", "H1", "H2"]

[[final_gate]]
id = "F2"
cmd = "python3 child-skills/mk-builder/scripts/mk-builder.py --self-test"
expect = 0
every = "merge"
covers = ["H2", "H3"]
+++

# Goal

Install the mk-ponytail ruleset into a lane so the lane agent follows it, without the orchestrator restating it in a brief. omp lanes first. Other hosts later.

Settled already (grilling, 2026-10-15; owner, 2026-10-16):

- Core 3 only: mode (`mk-ponytail`), review (`mk-ponytail-review`), debt (`mk-ponytail-debt`). No audit, gain, or help.
- Mode lives in `.matrixkoder/matrixkoder.yaml` under `session.ponytail.mode`: `off` | `lite` | `full` | `ultra`. Missing key means `full`. No `plugin.json` `userConfig` key.
- Debt ledger greps both `shortcut:` and `# ponytail:`.
- `templates/brief.md`, `templates/matrixkoder.yaml`, and `skills/matrixkoder/SKILL.md` are not edited by this plan.
- H3 trigger is `mk-builder.py commit` only. No omp `tool_result` hook on `git commit`.
- The hook is thin: it calls Python for the mode and the ruleset. It does not parse yaml in TypeScript.
- One ponytail review per round. The lane fixes Must-fix findings only. No new DB field.

# Design

omp discovers hooks from `<cwd>/.omp/hooks/pre|post/*.{ts,js}` (`omp://hooks.md`; the doc is not on this machine, see H0). `mk-lane.py` `install_lane_skill` (`scripts/mk-lane.py:1073`) copies `child-skills/mk-builder/` (`SKILL_SRC`) into `<lane>/.omp/skills/mk-builder` and `<lane>/.claude/skills/mk-builder` on every acquire. A hook is not a skill, so that copy does not put it where omp scans.

Three parts:

1. **Ruleset source in the lane.** `skills/mk-ponytail*` are plugin skills; a lane does not have them. Copy the three `SKILL.md` files into `child-skills/mk-builder/reference/ponytail/` (`mk-ponytail.md`, `mk-ponytail-review.md`, `mk-ponytail-debt.md`) so the existing wholesale skill copy carries them. One source of truth: a self-test asserts each copy is byte-equal to `skills/<name>/SKILL.md`.
2. **Python resolver.** New `mk-builder.py ponytail` subcommand. It reads `session.ponytail.mode` through the existing resolver (`bus_data_dir` then `_paths("project_yaml", data)`, same tiny-scan style as `todo_done_status`, `child-skills/mk-builder/scripts/mk-builder.py:2473`), so a migrated project's `project.yaml` is followed. It prints the ruleset for that level, or nothing for `off`.
3. **Thin TS hook.** `child-skills/mk-builder/hooks/pre/mk-ponytail.ts` runs `python3 <lane>/.omp/skills/mk-builder/scripts/mk-builder.py ponytail` on `session_start` and injects the output. Acquire copies it to `<lane>/.omp/hooks/pre/mk-ponytail.ts`.

Claude, OpenCode, and AGY lanes do not load omp hooks. They are out of H1. H3 works on every host because it lives in `mk-builder.py`.

# Contract

- `off` injects nothing and prints nothing.
- A missing `session.ponytail` key, or an unreadable yaml, means `full`. An unknown mode value means `full` and one stderr warning.
- Level filter: `lite` / `full` / `ultra` keep the shared rules and only the matching Levels-table row. `full` keeps the `full` row; `ultra` keeps the `ultra` row only (self-test: `ultra` output has no `**lite**` row).
- The hook and the resolver never write the yaml and never write a mode flag file.
- A lane that is not omp is unchanged by H0-H2.
- A hook failure (python missing, non-zero exit, timeout) injects nothing and never blocks the session. It logs one line to stderr.
- The builder never picks a model for the review subagent; it uses its own default (standing MK rule).

# Risks

- **omp hook API is unverified.** `omp://hooks.md` is not on disk. The only omp extension seen (`~/.omp/agent/extensions/herdr-omp-agent-state.ts`) uses `export default function (pi) { pi.on("session_start", (event, ctx) => ...) }`, but nothing shows how a `session_start` hook injects text, and nothing shows that a `hooks/pre/` file takes the same factory shape. H0 settles this before any build.
- **Skill copy and hook copy are separate.** `fresh(dst)` in `install_lane_skill` removes and recreates a destination. The hook path needs its own `fresh()` call, and the install must not leave a half state if the hook copy fails (same rollback as the skill copies).
- **Already covered, no change needed:** `.omp/` is in `DISPOSABLE` (`scripts/mk-lane.py:60-62`) and in `ensure_lane_exclude` (`scripts/mk-lane.py:611`), so `<lane>/.omp/hooks/` reads clean and is never a pause item. Release does not need a new entry. The self-test asserts this rather than assuming it.
- `SKILL_SRC` already holds `hooks/payloads/`. The new `hooks/pre/` inside it also rides into the skill copy (`.omp/skills/mk-builder/hooks/pre/`). Harmless: omp scans only `<cwd>/.omp/hooks/pre|post/`. The plan keeps one source file, not two.
- The bus kind `review` is the reviewer's verdict kind (`mk-lane-event` parses `review_verdict` / `review_findings` from it). A builder posting `review` would be read as a reviewer verdict. H3 uses kind `info` instead.

## H0 — spike: omp hook contract (read-only, no repo change)

Answer from a live omp session or its docs, and write the answers into this plan's Notes before H1 starts:

1. Does a file in `<cwd>/.omp/hooks/pre/` load with the same `export default function (pi)` shape as an extension? Which events does it get (`session_start`, subagent start)?
2. How does a `session_start` handler add text to the agent's context (return value, `pi.sendMessage`, `prompt.compose` section)?
3. Does a subagent the lane spawns run the hook too? If not, the ruleset must also be in the brief path (open item; the plan does not fake it).

If answer 1 or 2 is "no", H1/H2 stop and the owner decides: fall back to a `connect` printout in `mk-builder.py` (every host, no hook), or park.

## H1 — copy the hook and ruleset into the lane

- Add `child-skills/mk-builder/hooks/pre/mk-ponytail.ts`: on `session_start`, run `mk-builder.py ponytail`, inject its stdout; on empty output or any failure inject nothing.
- Add the three ruleset copies under `child-skills/mk-builder/reference/ponytail/`.
- `install_lane_skill` copies the hook to `<lane>/.omp/hooks/pre/mk-ponytail.ts` on every acquire, same overwrite rule as the skill, rolled back with the skill copies on failure. The return tuple gains the hook path; update its callers in the same change (grep first).
- A missing hook source is a hard `Fail`, like a missing skill source.
- `mk-lane.py --self-test` asserts: the file lands in the omp slot with the source bytes; a second acquire refreshes a modified copy; the source missing raises `Fail` and leaves no half install; the slot's status stays clean with `.omp/hooks/` present; a non-omp slot gets no `.omp/hooks/` outside what the skill copy already creates under `.omp/skills`.

## H2 — mode from the yaml

- `mk-builder.py ponytail [--mode off|lite|full|ultra]` (flag for tests only): resolve the mode from the project yaml via the existing data-dir resolver, filter the ruleset, print it.
- The scan reads `session:` then a nested `ponytail:` then `mode:` by indentation. `ponytail: scalars only, no flow maps (session: { ponytail: { mode: off } }); parse that form if a project writes it.` Marked in a code comment in the form `shortcut:`.
- Self-test: `off` prints nothing; missing key prints the `full` rules; `ultra` prints the ultra row only; `lite` prints the lite row only; an unknown value prints `full`; a migrated project (`project.yaml`) is read, not `matrixkoder.yaml`; the three ruleset copies equal `skills/*/SKILL.md`.
- Bundle note: edits to `skills/mk-ponytail*/SKILL.md` need the copies refreshed. The byte-equal self-test is the guard.

## H3 — commit triggers an in-pane ponytail review

Flow, entirely inside the one pane that owns the lane:

1. The pane agent finishes a task and runs `mk-builder.py commit`.
2. `commit` (after its normal bus `commit` message, unchanged) prints the `mk-ponytail-debt` ledger directly: a grep, no model, scoped to files changed in the round (`git diff --name-only <round base>..HEAD`; if the base is unknown, the last commit only). It then prints one order line: start your default-model subagent now, run the review rules in `reference/ponytail/mk-ponytail-review.md` on `HEAD`, and return the report to this pane.
3. The pane agent starts the subagent in its own session. The report returns to the pane.
4. The pane agent fixes the **Must fix** findings only. A commit made for those fixes does not print the review order again. Should-fix and Nice-to-have go into the report's `## Todo` section as skipped items with the reason `ponytail`.
5. The pane agent posts the report once: `mk-builder.py info "ponytail-review: <N must, M should, K nice>\n<report>"`. Kind `info`, never `review`.
6. The count of reviews run in a task is the number of bus messages starting `ponytail-review:`. There is no DB field and no schema change (project schema 7 just landed).

Gating (what makes "one per round" true):

- `commit` prints the order only when `session.ponytail.mode` is not `off`, and only the first time in a round. The "already ordered" marker is the existing git-excluded lane file `.matrixkoder-ponytail-rN.md` written by the pane agent's report step, not a new status key. Absent file = order not yet satisfied = print again on the next `commit`; the order text says to write the file first. This is the one place the plan accepts a nudge-style rule, not a hard guard.
- Reviewer-role lanes (`--role reviewer`) never get the order.

Who acts:

- The lane is the only actor on the findings; the orchestrator does not edit the lane.
- The bus copy is for the orchestrator to observe, not to act on. R30 holds: the message is lane to orchestrator.
- The review does not count against the 3-loop fix budget on a HIGH (that budget counts reviewer rounds from the orchestrator; this is a builder self-check). It is capped at one per round by the gating above.

Self-test (`mk-builder.py --self-test`): `commit` with mode `off` prints no order and no ledger; mode `full` prints the ledger rows for a seeded `shortcut:` and a `# ponytail:` marker in a changed file, and the order line; a second `commit` after the marker file exists prints neither order; a reviewer-role lane prints neither; bus-down and lane-file fallbacks still return their existing JSON unchanged.

# Out of scope

- Claude / OpenCode / AGY hook installs (no equivalent discovery path confirmed).
- Posting the report from a hook, or spawning the subagent from a hook (a hook cannot).
- A `tool_result` hook on `git commit`.
- A review-count field in the DB.
- Any edit to `templates/brief.md`, `templates/matrixkoder.yaml`, `skills/matrixkoder/SKILL.md`.

# Defects found in plan.md (for the owner)

1. `plan.md` cites `install_lane_skill` at `scripts/mk-lane.py:1006-1061`; it is at 1073+. Cosmetic.
2. `plan.md` says the hook "injects the `mk-ponytail` ruleset", but the ruleset text only exists under `skills/`, which a lane does not receive. Fixed by the bundled copies in H1.
3. `plan.md` leaves the `DISPOSABLE` question open as a risk. Checked: `.omp/` is already disposable and excluded, so no new entry is needed. H1 asserts it.
4. `plan.md` never says how the hook learns the data dir. It does not know the project path; the Python resolver does (`MK_BUS_DATA` or the token's `data` field). Fixed by routing through `mk-builder.py ponytail`.
5. `plan.md` H3 has no gate that limits the review to one per round and no host-neutral trigger; both fixed above.
6. `final_gate` ran only the `mk-lane.py` self-test, but H2/H3 change `mk-builder.py`. F2 added.
7. The omp hook injection API is assumed, not verified. H0 added as a blocker for H1/H2.

# Notes

- Ralplan solo, run in one session with no subagents. The Planner, Architect and Critic passes were done in my head; no intermediate draft was written. The Critic pass found the `review` bus-kind collision and the missing omp spike, both fixed above, and approved this version on the first loop. Residual risk: H3 step 4 ("fix Must-fix only") relies on the lane agent obeying its brief, like every other lane rule.
- Not verified here: omp hook behavior (H0), the exact name of the round base ref for the changed-file scope in H3 (look up `lane_head` / `branch_files` in `mk-builder.py` when building).
