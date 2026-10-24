+++ 1 Top block — plan identity [toml]
# The plan's identity. Words used in this file: a lane is one worktree where one agent builds; the check
# is MK's validation of this file (it reports TEMPLATE, PARTIAL or NOT A PLAN). The tools rewrite status
# and runs, so do not edit those two by hand once the plan is imported.
plan = "feature-id"          # the plan's id: lowercase letters, digits and dashes; it names the plan's folder and file
status = "draft"             # where the plan is: draft | approved | running | done | parked
created = 2026-10-24         # the day you draft the plan: set it to today
runs = []                    # ids of the runs started from this plan; the tools add to it
integration = "main"         # optional: the branch the lanes merge into; left out, the project's default branch is used
pushed = false               # optional: true = the finished branch is pushed to the remote; false = it stays local
/+++

+++ 2 Summary
# <plan id> — <one line: what this plan delivers>

## Summary
<!-- The plan at a glance: five lines that repeat the settings in blocks 1, 5 and 6, so copy the values from
there, never invent them; the check warns when they differ. Each line may hold only what the legend below allows. -->
- Phases: <n> — <id>: <title>; <id>: <title>
- Method: <waves | single lane> · subagents <on | off | mixed>
- Lanes: <one entry per lane, separated by ;>
- Delivery: <branch> · pushed: <yes | no>
- Gates: <the gate names in use, then each extra as name = command, separated by ;>

### Legend — the only values each line may hold
<!--
Phases: n is the number of phase blocks in block 5. After the dash, each phase as `<id>: <title>`, exactly as
  written in block 5, separated by ;.
Method: exactly one of these six lines, nothing else.
  waves · subagents on       waves · subagents off       waves · subagents mixed
  single lane · subagents on single lane · subagents off single lane · subagents mixed
  single lane = every phase shares one lane (one worktree, built one after another).
  waves = more than one lane; phases with the same wave number run in parallel, higher waves start later.
  subagents on = every phase has subagents = true. off = every phase has false. mixed = some of each.
Lanes: one entry per lane. Phases that share a lane_group value are one lane, written `<lane_group>: <phase ids>`.
  A phase with no lane_group is its own lane, written `<phase id> (own lane)`. Every phase appears exactly once.
Delivery: `<branch> · pushed: yes` or `<branch> · pushed: no`. The branch is `integration` in block 1 (the
  project's default branch when it is left out). pushed is yes when `pushed = true` in block 1, else no.
Gates: the names in `use` in block 6.0 (test and typecheck when `use` is left out), then each `extra` line
  as `<name> = <command>`, separated by ;.
-->

<!-- Worked example (delete when writing a plan):
- Phases: 2 — STORE: the plan items table; RUN: a run reads the store only
- Method: waves · subagents on
- Lanes: STORE (own lane); RUN (own lane)
- Delivery: main · pushed: no
- Gates: test; typecheck -->
/+++

+++ 3 Goal
## Goal
<!-- What the plan is for. Four parts; keep each to one line.
Done when: one to four outcomes, each with an id (G1, G2 …) and each something a reader can check. Every goal
must be served by at least one phase (its `Serves` line, block 5) and proved by at least one gate (block 6);
the check flags a goal that is not. Gates are not goals: they are the evidence that a goal was met. -->
- Done when:
  - G1 — <an outcome that is true when this plan is finished, checkable>
  - G2 — <a second outcome, if there is one>
- Why: <why it matters, one sentence>
- From: <who asked and when: the user's words, a research document, an earlier run>
- Sources: <paths or links to the research, spike or design documents behind the decisions, or none>

<!-- Sources: a design that is a file goes in this plan's inbox folder so it travels with the plan.
Worked example (delete when writing a plan):
- Done when:
  - G1 — a run starts from a stored plan and never reads the plan file again
- Why: the file and the store drift apart, and two sources of truth break resume
- From: the user, 2026-10-20, after the Play/Pause work
- Sources: none -->
/+++

+++ 4 Design / context
## Design / context
<!-- What the phases need to know that is not in the phases themselves. Eight parts, in this order; keep each short.
Items in Contracts (C), Test plan (T), Assumptions (A) and Rollout (R) carry an id and must each be carried out
by at least one task: the task lists the id in `covers` (block 5). An assumption is carried by the task that
would break if it were false, and that task's brief checks it. Settings (S) are facts, not carried. The check
flags an id no task carries and a task that names an id that does not exist. -->

### What is wrong today
<!-- Required when the plan changes existing behaviour; write "none, this is new" otherwise. Three to six numbered
facts you checked, each with a `path:line`. No opinions, no "might". -->
1. <a fact you checked, with path:line>

### Shape of the change
<!-- One short paragraph, plus a file map (file → what changes) when more than two files are involved. -->
<the change in a few sentences>

### Contracts
<!-- Numbered agreements (C1, C2 …) the phases meet through, since phases own disjoint files. Each names what is
agreed, the phase that writes it and the phase that reads it. none is fine for a one-phase plan. -->
C1 — <what is agreed> — written by <phase id>, read by <phase id>

### Test plan
<!-- What must be tested, at what level (unit, integration, end-to-end or manual), where the test lives and which
task writes it. One line per item. A task that adds or changes behaviour writes its own test, or an earlier task
does; never assume a test exists. A new project's first phase sets up the test runner, so the project's `test`
gate exists afterwards; until then name the command in the plan gate's `extra`. Write none only for a plan that
changes no behaviour. The task that writes the test lists the T id in `covers`; the check warns when the
"written by" task and `covers` differ. -->
T1 — <what is tested> — <level> — <test file> — written by <task id>

### Assumptions and dependencies
<!-- What the plan relies on but does not build: access, a service, another plan, a tool. One line per item.
none is fine. -->
A1 — <what the plan relies on> — <what happens if it is not true>

### Rollout and undo
<!-- How the change ships and how it is reversed. One line per item, including any docs or changelog change.
For one merge and no data, "ships by merge; undo = revert the merge" is enough. -->
R1 — <how it ships, how it is reversed, what happens to existing data, or the docs change> — <the task that does it>

### Settings and limits
<!-- What the project's settings allow, read before writing the phases. Copy the answers, never guess them.
Run `python3 scripts/mk-config.py list --json` (lanes, agents and models, gates, branch) and read
`~/.matrixkoder/usage.json` (limits left). One line per item, with an id (S1, S2 …):
`S1 — <setting> — <value> — <command or file used> — <date>`
Cover: the most lanes that may run at once; the agents and models that are active; the limits left (the Claude
5-hour and weekly percentage, any agent that is exhausted); the gates the project defines; the integration branch.
The check tests the plan against these lines: phases in one wave must not exceed the most lanes, a `route` must
name an active agent, a gate named in `use` must exist. It warns at approval and refuses at run start, because
limits change between writing and running. -->
S1 — <setting> — <value> — <command or file used> — <date>

### Out of scope
<!-- One to three lines on what this plan deliberately does not do. -->
<what this plan does not do>

<!-- Worked example (delete when writing a plan):
### What is wrong today
1. `run init` reads the plan file, not the store (`scripts/mk-state.py:1495`).
### Shape of the change
Run init takes a plan id and copies the stored items. Files: `scripts/mk-state.py` → read from the store.
### Contracts
C1 — plan items — table `plan_items`; written by STORE, read by RUN
### Test plan
T1 — run init copies a stored plan — unit — `scripts/test-run-init.py` — written by RUN.1
T2 — an old run that holds only a file path still closes — unit — `scripts/test-run-init.py` — written by RUN.2
### Assumptions and dependencies
A1 — the STORE phase merges first — RUN cannot read rows that do not exist yet
### Rollout and undo
R1 — ships by merge; undo = revert the merge, no data change — RUN.2
### Settings and limits
S1 — most lanes at once — 3 — mk-config.py list --json — 2026-10-24
### Out of scope
No change to how a plan is written. -->
/+++

+++ 5 Phases
## Phases
<!-- One group of blocks per phase, numbered 5.1, 5.2 …. Each phase has its text (5.n, exactly the six lines
below), its settings (5.n.0) and its tasks (5.n.1, 5.n.2 …). Tasks may have subtasks (5.n.m.1 …), which are
rare. Every phase has all of them. Each block closes before the next opens. The block name and its `title`
key should match; the check reads the `title` key. -->

### Legend — task risk and what it requires
<!--
risk 1 trivial: no gate, light review.
risk 2 low: no gate (the plan gate covers it), light review.
risk 3-4 medium or high: a gate command or manual, standard review.
risk 5 destructive or data loss: a gate command is required, data-loss review, and the user is asked first.
A phase has no gate and no risk of its own: its review depth is that of its riskiest task.
-->

### Legend — phase and task settings
<!--
wave: whole number from 1. Same wave = parallel; a higher wave starts after the lower waves are done.
size: how much work, which the orchestrator uses to pick the agent and model. A task's own size wins; if it
  has none, its phase's size is used.
  trivial: a few lines in one file, a rename or a doc fix. Routes to a small, fast model.
  standard: a normal change in a few files with an obvious design. Routes to a mid-tier model.
  complex: several files or areas, a new design, or cross-cutting work. Routes to the strongest model.
  Size is not risk: size is how much work, risk (above) is how bad a mistake would be.
route: "" or "<agent> <model> [<effort>]", with names from the project's routing catalog. "" lets Jev (the
  router that picks a builder) choose.
subagents: a phase setting. true = the builder may hand tasks marked `subagent = true` to their own subagents;
  false = no subagents in this phase. The Summary's Method line says on, off or mixed from these.
lane_group: phases sharing a value share one lane and must have the same wave; "" = its own lane.
owned: file or folder paths; a folder ends with `/`. A phase may touch nothing else.
forbidden: files many phases change (hot files) that this phase must leave alone; sequence the work instead.
qa: names of QA flows to run for this phase (frontend changes only), or []. The names are passed on as
  written and are not validated yet.
-->
/+++

+++ 5.1 P1 — <phase title>
### P1 — <phase title>
- Goal: <what is true when this phase is done, one checkable sentence>
- Serves: <the G ids from block 3>
- Needs first: <the phases it waits on and why, or nothing>
- Build order: <the order of its tasks, and what to read first (path:line)>
- Hazards: <the K ids from block 7 that apply, or none>
- Carries: <the C, T, A and R ids from block 4 that its tasks cover, or none>

<!-- Needs first repeats `deps` (5.n.0) and Carries is the union of its tasks' `covers`; the check warns when
they differ.
Worked example (delete when writing a plan):
### RUN — A run reads the store only
- Goal: `run init` copies a stored plan version and no later step opens the file.
- Serves: G1
- Needs first: STORE, because the rows must exist before a run can copy them.
- Build order: RUN.1 then RUN.2; read `scripts/mk-state.py:1495` first.
- Hazards: K1
- Carries: C1, T1, A1, R1, T2 -->
/+++

+++ 5.1.0 P1 settings [toml]
# Settings of phase P1. The phase's goal is in its text block (5.1), not here. REQUIRED: id, title, wave,
# owned, commit. Every other key is optional; the default is shown. Quoted values are examples: replace them.
id = "P1"                        # REQUIRED. one capital letter, then capitals or digits, up to 8 characters, unique in the plan; its tasks are <id>.<n>
title = "<what this phase delivers, one line>"   # REQUIRED
wave = 1                         # REQUIRED. whole number from 1; phases with the same wave run in parallel
owned = ["src/area/", "src/file.ts"]   # REQUIRED. the only files this phase may touch; two phases in one wave must not overlap
commit = "<feat(scope): one phase, one commit>"   # REQUIRED. the commit subject the builder uses
deps = []                        # phase ids that must be done first, or <run>:<task> to wait on another run's task
forbidden = []                   # files this phase must leave alone
route = ""                       # "" lets Jev choose the builder; or pin one: "<agent> <model> [<effort>]"
reviewer = ""                    # "" lets the orchestrator choose; or "<agent> <model>" for the reviewer
size = "standard"                # trivial | standard | complex; the default for its tasks, a task may set its own
subagents = true                 # true: tasks marked subagent = true get their own subagent; false: none in this phase
lane_group = ""                  # phases with the same value share one lane and must have the same wave; "" = its own lane
qa = []                          # QA flow names to run for this phase (frontend changes only)
/+++

+++ 5.1.1 P1.1 — <task title> [toml]
# Writing a task: the title says what it delivers, not how. Acceptance is one checkable outcome per line
# ("works well" is not checkable). risk (1-5) decides the gate, see the legend in block 5: 1-2 no gate, the
# plan gate covers it; 3-4 a gate command or manual; 5 a gate command. Quoted values are examples: replace them.
# Worked example, RUN.2: covers = ["R1", "T2"], risk = 3, gate = "python3 scripts/test-run-init.py"
id = "P1.1"                     # REQUIRED. <phase id>.<n>
title = "<what this task delivers, one line>"   # REQUIRED. The check reads this key; the block name should match it
description = "<a short plain-English summary of what this task is and why it is needed, two to five sentences>"   # optional
covers = ["T1"]                 # the block 4 ids (C, T, A, R) this task carries out; every such id must be carried by some task
acceptance = [                  # advised: a list of strings, one checkable outcome each, even with one item
  "<the outcome that decides done>",
  "<the second outcome the same task must also meet>",
]
risk = 3                        # optional, default 1: 1 trivial, 2 low, 3 medium, 4 high, 5 destructive or data loss
size = "standard"               # optional: trivial | standard | complex; if left out, the phase's size is used. Routing picks the agent and model from it
gate = "<the command that proves this task>"   # risk 3 or more: the one command that proves this task (omit at risk 1-2)
# manual = "<who checks and how>"   # instead of gate when no command exists; a string, never run
deps = []                       # optional: task or phase ids that must be done first
parallel = false                # optional: true lets parallel-flagged siblings with no dep between them run at once
route = ""                      # optional: "" lets Jev decide, or "<agent> <model> [<effort>]" to pin it
refs = []                       # optional: path:line pointers the brief (the task text the builder receives) quotes
subagent = false                # true: this task gets its own subagent (only in a phase with subagents = true); then owned is required
owned = []                      # required when subagent = true: the files this task touches, a subset of the phase's
/+++

+++ 5.1.1.1 P1.1.1 — <subtask title> [toml]
# A subtask is optional and rare: use one only when a task has parts that are accepted separately. It has no
# gate, no risk and no size of its own: its task's, or the plan gate, covers it. Quoted values are examples.
id = "P1.1.1"                   # REQUIRED. <phase id>.<n>.<m>
title = "<what this subtask delivers, one line>"   # REQUIRED
description = "<a short plain-English summary, two to five sentences>"   # optional
acceptance = ["<the outcome that decides this subtask done>"]   # a list of strings, even with one item
deps = []                       # optional: ids in the same plan
parallel = false                # optional
route = ""                      # optional
owned = []                      # the files this subtask touches
/+++

+++ 5.1.2 P1.2 — <task title> [toml]
# A phase may hold more than one task; each is accepted on its own. This one is low risk, so it has no gate: the plan gate covers it.
id = "P1.2"                     # REQUIRED. <phase id>.<n>
title = "<the second task this phase delivers>"   # REQUIRED
covers = []                     # block 4 ids this task carries out, if any
acceptance = ["<the outcome that decides this task done>"]   # a list of strings, even with one item
risk = 1                        # 1-2: no gate
/+++

+++ 6 Gates
## Gates
<!-- The plan gate shows the goals were met. It runs once, after the last task is merged. The plan is done only
when every gate passes; a failure keeps the plan running and goes back to the task that broke it.
One line per gate, then the settings in 6.0:
- <gate name> — proves <G ids from block 3> — <what it shows, in a few words>
The names on these lines are exactly `use` plus the `extra` names in 6.0; the check warns when they differ.
Every goal in block 3 must be proved by at least one gate; the check flags one that is not.
Phases have no gate. A task has its own gate only at risk 3 or more (legend in block 5). -->
- <gate name> — proves <G1> — <what it shows>

### Legend — gate names for `use`
<!--
test, typecheck, lint, build: the project's own commands, defined in its YAML (project.yaml, or
matrixkoder.yaml before the project is migrated). The check refuses a name the project does not define.
Installers and formatters are not gates, since they change files.
If `use` is left out, the gates are `test` and `typecheck`.
A new project has no `test` gate yet: its first phase sets up the test runner, and until then the plan names
the command under `extra`.
-->

<!-- Worked example (delete when writing a plan):
- test — proves G1 — run init copies a stored plan and no later step opens the file
- typecheck — proves G1 — the new store reads are typed -->
/+++

+++ 6.0 Gate settings [toml]
# The plan gate runs once, after the last task is merged. Every gate must exit 0.
use = ["test", "typecheck"]   # optional: names from the legend in block 6; left out = test and typecheck
timeout = 600                 # optional: seconds each gate may run
# [extra]                     # optional: a gate only this plan needs, one `name = "command"` line each, for example:
# selftest = "python3 scripts/<name>.py --self-test"
/+++

+++ 7 Risks
## Risks
<!-- What can go wrong while building this plan, one per line, with an id (K1, K2 …):
`K1 — <what can go wrong> — level <1-5> — guard: <the guard> — <the task or phase it belongs to>`
Level uses the same scale as a task's risk (legend in block 5). A guard is something concrete: a gate, a test
from block 4 (T1), a rollout step (R1), a review depth or a fallback; never "be careful".
When a task is named, its risk must be at least the level; when a phase is named, its riskiest task's risk
must be. From level 3 the guard must be a gate or a test. Level 5 (data loss or destructive) means the task's
gate is a command and its review is data-loss. Assumptions the plan relies on go in block 4 (A1); a risk may
cite them. The run can raise a task's risk while it works; the plan states the starting level.
Write none for a plan with no real risk. -->
- K1 — <what can go wrong> — level <1-5> — guard: <the guard> — <task or phase id>

<!-- Worked example (delete when writing a plan):
- K1 — an old run holds only a file path and could fail to close — level 3 — guard: T2, a test that an old run still closes — RUN.2 -->
/+++

+++ 8 Open questions
## Open questions
<!-- What is not decided yet, one per line, with an id (Q1, Q2 …):
`- [ ] Q1 — <the question> — blocks: <task or phase id, or none> — pick: <the recommended answer>`
Ask the user only for preferences, never for facts the code can answer. A question is unknown and needs an
answer; something the plan relies on and believes true is an assumption (block 4). Every unresolved question,
deferred decision or item from a review goes here, never scattered through the phases.
While a question is unticked, the run refuses to start the task or phase it blocks. The user lifts that in
one of two ways: answer the question, or override it for that task after approval. Either one is recorded as
a Log line with the id, the answer or the override, and who gave it.
Close a question by ticking it `- [x]`; never delete it. `approve` warns "n unticked open questions" and
does not block. Write none when there are none. -->
- [ ] Q1 — <the question> — blocks: <task or phase id, or none> — pick: <the recommended answer>

<!-- Worked example (delete when writing a plan):
- [ ] Q1 — should old runs be migrated to the store? — blocks: RUN.2 — pick: no, they keep the file path until they finish -->
/+++

+++ 9 Log
## Log
<!-- The plan's own history, one line per entry, oldest first, append only (correct a mistake with a new line):
`- <date> — <kind> — <what happened, citing the ids it is about> — <who>`
kind is one of: drafted, decision, answer, override, scope cut, approved, amended, parked, resumed.
Write a line for: the plan being drafted, a decision, the answer to a question (cite its Q id), an override
of a task a question blocks, a scope cut, the approval, a change after approval, and parking or resuming the plan.
The Log stays open after approval for entries about the plan itself. What the run does (rounds, verdicts,
merges) is recorded by the run as events, not here. -->
- <date> — drafted — <what the plan is for, one line> — <who>

<!-- Worked example (delete when writing a plan):
- 2026-10-20 — drafted — a run reads the stored plan, never the file — user
- 2026-10-21 — answer — Q1: old runs are not migrated, they keep the file path until they finish — user
- 2026-10-21 — scope cut — migration of old runs left out of this plan (see Out of scope) — user -->
/+++

+++ 10 Notes — how to use this template
Delete this block when you write a plan. It is not counted by the check ("n of 9 blocks found" is blocks 1 to 9).

Writing the plan
One plan, one file, made of numbered blocks. First read the project's settings (run
`python3 scripts/mk-config.py list --json` and read `~/.matrixkoder/usage.json`), then fill the blocks in order:
1 top block, 2 summary, 3 goal, 4 design (including Settings and limits), then the phases (5), gates (6), risks
(7), open questions (8) and log (9). Keys marked REQUIRED must be present; any other key you may leave out.
Three kinds of text: `<angle brackets>` are a value to type over; `<!-- … -->` comments are instructions and
worked examples, which you delete; quoted example values in [toml] blocks are placeholders too, so replace them.

Block format
A block opens with `+++ <number> <name>` and closes with `/+++`, for example `+++ 3 Goal` and `+++ 5.1 P1 — title`.
Blocks marked [toml] hold settings; the others are Markdown. Numbers nest by dots: 5.1 is the first phase,
5.1.0 its settings, 5.1.1 its first task, 5.1.1.1 a subtask. No line inside a block may start with `+++ ` or `/+++`.

Ids
G goal (block 3) · C contract, T test, A assumption, R rollout, S setting (block 4) · K risk (block 7) · Q open
question (block 8). A phase names the goals it serves (`Serves`); a task names the block 4 items it carries
(`covers`); a gate names the goals it proves; a risk names its task.

What the check looks for
The check calls a file TEMPLATE (follows this template), PARTIAL (something is missing, and it names what)
or NOT A PLAN (no top block, or no phase and no task). It also checks the links: every goal is served by a
phase and proved by a gate, every block 4 item is carried by a task, and every id used exists.

Life of a plan
draft → import → approve → run. import loads the file into the store without approving it. approve checks it,
saves a numbered version and creates the tasks. run init --plan starts a run from an approved plan. After
import the store is the source and the tools keep status and runs up to date.

Gates
See block 5 (task gates, by risk) and block 6 (the plan gate).
/+++
