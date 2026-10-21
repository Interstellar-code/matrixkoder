+++
# The plan file — every header key below names where it lands in the project store
# (mk.db; docs/state-schema.md § "Plan file → DB" says the same). This template is the
# contract between a plan file and the DB, so a writer never has to read the code.
# Keys are optional unless marked REQUIRED; a plan that omits an optional key still checks clean. REQUIRED keys (id, title, wave, owned, commit on a phase; id, title on a task) are a check error for a NEW plan and only a warning for an older or undated one. A new plan is one whose `created` date is on or after 2026-10-20.
#
# What a new plan looks like (plain words):
#   A new plan is one whose `created` date is on or after 2026-10-20. The `created = 2026-10-08` example below is a placeholder: set it to today.
#   A plan has seven body sections, the `##` headings below the closing +++: Summary, Goal, Design / context, Phases, Risks, Open questions, Log. The `# <plan id> — …` title line is checked separately (a warning when missing).
#   The check also reports four header parts: header phases (at least one `[[phase]]` block; never the Phases body section), tasks (at least one task), acceptance (every task has some) and final gate (a `[[final_gate]]` with covers).
#   TEMPLATE = all seven sections and all four parts found. PARTIAL = a valid +++ header with something missing; the check lists the missing names (for example "missing: tasks, acceptance" or "missing: Risks"). NOT A PLAN = no +++ header, or no phase and no task. An Inbox card shows "n of 7 sections found" plus the missing names.
#   Method in the Summary is "mixed" when phases differ on `subagents`.
#
# Lifecycle (C13):
#   import <file>    loads the file into the store WITHOUT approving it
#   approve <file>   checks it, versions it, writes the tasks, copies the file to <project>/plans/<id>/archive/<id>-v<n>.md (an unmigrated project: <data>/plans/archive/) and leaves <project>/plans/<id>/plan.md
#   run init --plan  starts the run for an approved plan
#   `set`, `add-run` and `approve` rewrite the header of this file (the status and runs lines). The file is the source only until import; after import, status, tasks and runs live in the store (mk.db), and rounds and verdicts change there.
#   When the plan is done, the file stays where it is, with its versions in archive/.
#
# F.2 gates — two levels, one rule each:
#   [[final_gate]]      the plan-level gate: the command that proves the whole plan once its
#                       tasks are done. `every` says when the run reruns it, `covers` is the
#                       list of task ids it proves and is REQUIRED — mk-plan check refuses a
#                       final gate without it and warns when the plan has none at all.
#   gate / manual       every task and every subtask carries exactly ONE of the two: `gate` is
#                       a command table this task must pass, `manual` is the reason no command
#                       exists (a string, never run). Both in one table is a check error;
#                       neither is a check warning. `critical`/`risk` (task level) feed the
#                       same approval guard as the phase-level risk.
plan = "feature-id"                       # id [a-z0-9][a-z0-9-]{0,40}; the file is <project>/plans/<id>/plan.md — plans.id
status = "draft"                          # draft|approved|running|done|parked; run init --plan wants approved — plans.status
created = 2026-10-08                      # the day you draft the plan — set it to today; a date on or after 2026-10-20 makes it a NEW plan (the stricter check, see the top) — plans.created
runs = []                                 # run ids; mk-plan.py add-run appends, never by hand — plans.header_json
design = []                               # optional: the research, spike or design documents behind the decisions — paths or links (https://…); a design that exists as a file goes in this plan's inbox folder so it travels with the plan into assets/ — plans.design
integration = "mk/<feature>/integration"  # optional: branch the lanes merge into; default from the project.yaml `repo.integration_branch` — plans.integration
description = "A short plain-English summary of what this plan builds and why it matters. Two to five sentences, no test steps and no lists of names."  # optional: plain English, 2-5 sentences, what it is and why — plans.header_json only

[gates]                                   # full-suite gates the orchestrator reruns at merge and QA; scoped forms go in briefs — plans.header_json
test = "<test cmd>"                       # e.g. pnpm vitest run — plans.header_json
typecheck = "<typecheck cmd>"             # e.g. pnpm exec tsc --noEmit — plans.header_json
selftest = "<self-test cmd of the changed scripts>"  # e.g. python3 scripts/<name>.py --self-test; the full bash scripts/selftest.sh runs once on the integration tip, not per task — plans.header_json
validate = "<validate cmd>"               # e.g. claude plugin validate . — plans.header_json

[gates.c1]                                # F.10: the literal store-path-name count may only go down; baseline measured 377 at ecfa56f, lower it with `python3 scripts/c1-gate.py ratchet --baseline 377`, never raise it — plans.header_json
cmd = "python3 scripts/c1-gate.py check --baseline 377 --base main"  # exits 1 and lists the new hits when the count rises — plans.header_json
expect = 0                                # int, default 0 — plans.header_json
timeout = 60                              # seconds — plans.header_json

[[final_gate]]                            # F.2: the plan-level gate; F.2 wants one with covers, and mk-plan check warns when the header has none — plans.header_json
id = "F1"                                 # optional: names the gate in errors and in the change report (`final_gate[F1]` vs `final_gate[1]`) — plans.header_json
cmd = "<the command that proves the whole plan>"  # the command the run executes at every `every`; keep it the one end-to-end proof of the plan — plans.header_json
expect = 0                                # int, default 0: the exit code that means pass — plans.header_json
every = "merge"                           # merge|phase|run_end, default merge: run it at every lane merge, at each phase end, or once when the run ends — bad values are a check error — plans.header_json
timeout = 600                             # int seconds, default 600 — plans.header_json
needs = []                                # optional: things the gate needs in place before it runs, e.g. ["bus"]; list, default [] — plans.header_json
covers = ["P1.1", "P1.2"]                 # required, non-empty: the task ids of THIS plan the gate proves; mk-plan check refuses a final gate without covers and errors on an id that is not a task here — plans.header_json

[[final_gate]]                            # L10.14 example: the read-only layout + DB audit of the MK home as a standing gate; delete it for a plan that does not touch the storage layout — plans.header_json
id = "LAYOUT"                             # the audit's own name in errors and in the change report — plans.header_json
cmd = "python3 scripts/mk-e2e-layout.py audit"  # never writes the home: reads every store from a private copy; exit 0 = clean v2 home, 1 = drift (the failing rule names the path or table) — plans.header_json
every = "merge"                           # rerun after every lane merge, so a merge that leaves a stray file or table fails at once — plans.header_json
covers = ["P1.1", "P1.2"]                 # the tasks whose merge can change the home layout; same rule as above: non-empty and real task ids — plans.header_json

[[phase]]                                 # one block per phase; phases in one wave must own disjoint files. REQUIRED keys per phase: id, title, wave, owned, commit (a check error for a new plan, see the top; an older plan only gets a warning). description is strongly advised (a warning when missing). All other keys are optional with the default shown — plans.header_json
id = "P1"                                 # REQUIRED. [A-Z][A-Z0-9]{0,7}, unique; tasks are <phase id>.<n> — tasks.id
title = "what this phase delivers, one line"   # REQUIRED. becomes the phase task title — tasks.title
description = "A short plain-English summary of what this phase is and why it exists. Two to five sentences."  # advised (warning when missing): plain English, 2-5 sentences — header_json only
wave = 1                                  # REQUIRED. int >= 1; same-wave phases run in parallel — header_json only (the wave is not a tasks column)
deps = []                                 # optional, default []. phase ids, or <run>:<task> to wait on another run's task — tasks.deps
owned = ["src/area/", "src/file.ts"]      # REQUIRED. the only files this phase touches; overlap within a wave is a check error — tasks.owned
forbidden = []                            # optional: hot files this phase must leave alone (sequence them instead) — header_json only
route = ""                                # "" = jev_decide at stage 2; "<agent> <model> [<effort>]" = pinned, skips Jev — tasks.route_json
reviewer = "opencode opencode-go/kimi-k3" # optional: <agent> <model> for the stage 8 reviewer pane; "" = the orchestrator decides — header_json only
size = "standard"                         # trivial|standard|complex — feeds jev_classify evidence — header_json only
risk = "reversible"                       # read_only|reversible|destructive — feeds jev_classify evidence — header_json only
review = "standard"                       # light|standard|data-loss — sets reviewer depth at stage 8 — header_json only
qa = []                                   # QA flows for stage 11 (frontend changes) — header_json only
commit = "feat(scope): one phase, one commit"  # REQUIRED. the phase commit subject the builder uses — header_json only
subagents = true                          # default true: the builder hands each task to one subagent (its own default model); false = the builder works every task itself — header_json only
lane_group = ""                           # same value = same wave, one shared worktree and lead session; "" = own lane — header_json only

  # Writing a task (REQUIRED for a new plan: id, title; advised: acceptance and exactly one of gate or manual, a warning when missing; the rest is optional):
  #   title      one line saying what the task delivers, not how.
  #   acceptance one outcome per line, each checkable by the reviewer; no "works well".
  #   gate       the one command that proves it; `lines` names the acceptance lines it proves. A task whose lines no gate proves gets a warning. A task that changes behaviour proves it with a test: name it in `refs` or run it in the gate.
  #   manual     when no command exists: who checks and how.
  #   Subtasks (below) are optional and rare: use one only when a task has parts that are accepted separately.
  # Worked example (delete when writing a plan):
  #   id = "P1.1"
  #   title = "run init copies a stored plan"
  #   acceptance = ["run init --plan <id> creates the run's tasks from the store"]
  #   gate = { cmd = "python3 scripts/mk-state.py --self-test", expect = 0, timeout = 600, lines = ["P1.1.a1"] }
  #   a second task with no command: manual = "the user opens the Plan tab and checks the rows"
  [[phase.task]]                          # REQUIRED keys: id, title (an error for a new plan only); the rest see the guide above — plans.header_json; the rows below are what the importer writes
  id = "P1.1"                             # REQUIRED. <phase id>.<n> — tasks.id
  title = "what this task delivers, one line"  # REQUIRED — tasks.title
  description = "A short plain-English summary of what this task is and why it is needed. Two to five sentences."  # optional: plain English, 2-5 sentences — header_json; copied to tasks.extra_json at run init / plan sync
  acceptance = ["the outcome that decides done, checkable by the reviewer", "the second outcome the same task must also meet"]  # advised (warning when missing): tasks.acceptance; a list = two acceptance lines, and every line gets an id by position: <task id>.a1, <task id>.a2 (a plain string is the single line <task id>.a1)
  deps = []                               # optional: ids in the same plan, or <run>:<id> to wait on another run — tasks.deps
  parallel = false                        # optional; true: parallel-flagged siblings with no dep between them may run at once — tasks.parallel
  route = ""                              # optional; "" or "<agent> <model> [<effort>]" — tasks.route_json
  refs = []                               # optional: path:line pointers the brief quotes — header_json only
  subagent = false                        # true only inside a subagents=true phase; then owned is required — header_json only
  owned = []                              # required when subagent = true; subset of the phase's owned — tasks.owned
  gate = { cmd = "<the command that proves this task>", expect = 0, timeout = 600, lines = ["P1.1.a1", "P1.1.a2"] }  # the task must pass this command; `lines` names the acceptance line ids (above) it proves and mk-plan check errors on an id that is not one of <task id>'s lines; `gate` may also be a LIST of such tables when the task needs several commands — plans.header_json
  critical = true                         # bool, default false: a task the plan cannot ship without; approving a file that flips this true -> false needs --user-ack — header_json only
  risk = 3                                # this task's base risk as an int 1-5 (default 1): approving a file that LOWERS it needs --user-ack; separate from the phase-level risk string above — header_json only

  [[phase.task.subtask]]                  # optional: splits a task into subtasks; id is <phase>.<n>.<m> — plans.header_json
  id = "P1.1.1"                           # <phase id>.<n>.<m>; the importer derives tasks.type="subtask" and tasks.parent from the nesting
  title = "what this subtask delivers, one line"  # tasks.title
  description = "A short plain-English summary of what this subtask is and why it is needed. Two to five sentences."  # optional: plain English, 2-5 sentences — header_json; copied to tasks.extra_json at run init / plan sync
  acceptance = "the outcome that decides this subtask done"  # tasks.acceptance
  deps = []                               # comma-separated ids in the same plan, or <run>:<id> to wait on another run — tasks.deps
  parallel = false                        # true: parallel-flagged siblings with no dep between them may run at once — tasks.parallel
  route = ""                              # "" or "<agent> <model> [<effort>]"; or an inline table {agent, model, effort} — tasks.route_json
  owned = []                              # files this subtask touches — tasks.owned
  manual = "<why this subtask has no command of its own>"  # a subtask carries gate OR manual, never both: `manual` is a string and is never run, it just records why the acceptance cannot be a command — header_json only

  [[phase.task]]                          # a phase may hold more than one task; each is accepted on its own
  id = "P1.2"                             # <phase id>.<n> — tasks.id
  title = "the second task this phase delivers"  # tasks.title
  acceptance = "the outcome that decides this task done"  # tasks.acceptance (plain string = the one line P1.2.a1)
  refs = []                               # optional: path:line pointers the brief quotes — header_json only
  subagent = false                        # true only inside a subagents=true phase; then owned is required — header_json only
  owned = []                              # required when subagent = true; subset of the phase's owned — tasks.owned
  manual = "<why no command exists for this task>"  # gate OR manual, never both (mk-plan check errors on both, warns on neither); `manual` is a string: say who checks this outcome and how — header_json only
+++

# <plan id> — <one line: what this plan delivers>
# This H1 line is what becomes plans.title,
# so keep it to one line and start it with the plan id. `mk-plan.py check` warns when it is missing.

## Summary
<Five fixed lines, right under the title, so a card or a reader sees the plan at a glance. Phases, method, lanes and the gate are read from the header, so keep them equal to it; the check warns when they differ. Method is "waves" when the phases use more than one wave value, else "single lane"; "mixed" when some phases run subagents and others do not. A lane that runs across waves is described in Design / context, since a lane_group stays inside one wave.>
- Phases: <n> — <P1 title; P2 title; …>
- Method: <waves | single lane> · subagents <on | off | mixed>
- Lanes: <each lane_group value with its phases; every other phase is its own lane>
- Delivery: <the branch it merges to> · pushed: <yes | no>
- Gates: <each final_gate as id = cmd, separated by ;>

<!-- Worked example (delete when writing a plan):
- Phases: 2 — RUN reads the store only; WIRE connects the tab
- Method: waves · subagents on
- Lanes: RUN and WIRE are each their own lane
- Delivery: main · pushed: no
- Gates: SUITE = claude plugin test . ; TYPES = tsc -p . --noEmit -->

## Goal
<Three lines. First: what is true when this plan is done, in one sentence. Second: why it matters. Then, in one line, where the request came from: user words, research docs (`design`), prior runs. The header is the contract every phase builds against; the body is the context.>

<!-- Worked example (delete when writing a plan):
A run starts from a stored plan and never reads the plan file again.
Why: the file and the store drift apart, and two sources of truth break resume.
From: the user, 2026-10-20, after the Play/Pause work. -->

## Design / context
<What the phases need to know that is not in the phases themselves. Four parts, in this order; keep each short.>

### What is wrong today
<Required when the plan changes existing behaviour; write "none, this is new" otherwise. Three to six numbered facts you checked, each with a `path:line`. No opinions, no "might".>

### Shape of the change
<One short paragraph, plus a file map (file → what changes) when more than two files are involved.>

### Contracts
<Numbered agreements (C1, C2, …) the phases meet through, since phases own disjoint files. Each names what is agreed, the phase that writes it and the phase that reads it. None is fine for a one-phase plan.>

### Out of scope
<One to three lines on what this plan deliberately does not do.>

<!-- Worked example (delete when writing a plan):
### What is wrong today
1. `run init` reads the plan file, not the store (`scripts/mk-state.py:1495`).
### Shape of the change
Run init takes a plan id and copies the stored items. Files: `scripts/mk-state.py` → read from the store.
### Contracts
C1 — plan items: table `plan_items`; STORE writes it, RUN reads it.
### Out of scope
No change to how a plan is written. -->

## Phases
<One `### <phase id> — <title>` block per phase, with exactly these four lines. The files a phase owns are not repeated here; the header holds them. Every header phase has a block and every block has a header phase.>

### P1 — <title>
- Delivers: <one sentence>
- Needs first: <the phases it waits on and why, or "nothing">
- Build order: <the order of its tasks, and what to read first (`path:line`)>
- Hazards: <what can go wrong here, and the guard: a review depth, a test, a fallback>

<!-- Worked example (delete when writing a plan):
### RUN — A run reads the store only
- Delivers: `run init` copies a stored plan version and no later step opens the file.
- Needs first: STORE, because the rows must exist before a run can copy them.
- Build order: RUN.1 then RUN.2; read `scripts/mk-state.py:1495` first.
- Hazards: old runs hold only a file path and keep it until they finish; nothing is migrated. -->


## Risks
<Three to six numbered risks, each "what can go wrong — the guard — the phase or task it belongs to". A guard is something concrete (a review depth, a test, a gate, a fallback), never "be careful". A risk of data loss means that phase has review = "data-loss".>

<!-- Worked example (delete when writing a plan):
1. Old runs hold only a file path — a test that an old run still closes — RUN.2. -->

## Open questions
<One line per question: `- [ ] <question> — <what it decides>`. Ask the user only for preferences, never for facts the code can answer. `approve` warns "n unticked open questions"; it does not block (a question you decide to live with stays unticked). Write "none" when there are none.>
<Every unresolved question, deferred decision or item needing clarification goes here, never scattered through the Phases; questions raised by a review of the plan are copied here too.>
<A question is closed by ticking it `- [x]` and adding a Log line with the answer and who gave it; never by deleting it.>

## Log
<One line per entry: `- <date> — <what was decided or changed> — <who>`. Decisions, answers to open questions, scope cuts and approvals only, oldest first, append only (correct a mistake with a new line). It covers the time up to approval; after that the run records decisions as events and this Log is not added to.>
- <date> — drafted — <who>
- <date> — <decision> — <who>

<!-- Worked example (delete when writing a plan):
- 2026-10-20 — scope cut: old runs are not migrated; they keep the file path until they finish — user -->
