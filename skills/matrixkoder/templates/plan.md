+++
# The plan file — every header key below names where it lands in the project store
# (mk.db; docs/state-schema.md § "Plan file → DB" says the same). This template is the
# contract between a plan file and the DB, so a writer never has to read the code.
# Every key is optional and additive: a plan that omits one still checks clean.
#
# Lifecycle (C13):
#   import <file>    loads the file into the store WITHOUT approving it (command added by D1)
#   approve <file>   checks it, versions it, writes the tasks, copies the file to
#                    <project>/plans/<id>/archive/plan-v<n>.md and leaves <project>/plans/<id>/plan.md
#   run init --plan  starts the run for an approved plan
#   After import the run and the project DB are the source: status, tasks, rounds and verdicts
#   change there, never in this file — MK does not edit the file, it is a reference.
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
plan = "feature-id"                       # id [a-z0-9][a-z0-9-]{0,40}; the file is <project>/plans/<id>/plan.md — plans.id (plans.title until D1 fills the H1 title)
status = "draft"                          # draft|approved|running|done|parked; run init --plan wants approved — plans.status (column added by D1; header_json until then)
created = 2026-10-08                      # date the plan was drafted — plans.created (column added by D1; header_json until then)
runs = []                                 # run ids; mk-plan.py add-run appends, never by hand — plans.header_json
design = []                               # optional: research/spike docs behind the decisions (paths) — plans.design (column added by D1; header_json until then)
integration = "mk/<feature>/integration"  # optional: branch the lanes merge into; default from the project.yaml `repo.integration_branch` — plans.integration (column added by D1; header_json until then)

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

[[phase]]                                 # one block per phase; phases in one wave must own disjoint files — plans.header_json
id = "P1"                                 # [A-Z][A-Z0-9]{0,7}, unique; tasks are <phase id>.<n> — tasks.id
title = "what this phase delivers, one line"   # becomes the phase task title — tasks.title
wave = 1                                  # int >= 1; same-wave phases run in parallel — header_json only (the wave is not a tasks column)
deps = []                                 # phase ids, or <run>:<task> to wait on another run's task — tasks.deps
owned = ["src/area/", "src/file.ts"]      # the only files this phase touches; overlap within a wave is a check error — tasks.owned
forbidden = []                            # optional: hot files this phase must leave alone (sequence them instead) — header_json only
route = ""                                # "" = jev_decide at stage 2; "<agent> <model> [<effort>]" = pinned, skips Jev — tasks.route_json
reviewer = "opencode opencode-go/kimi-k3" # optional: <agent> <model> for the stage 8 reviewer pane; "" = the orchestrator decides — header_json only
size = "standard"                         # trivial|standard|complex — feeds jev_classify evidence — header_json only
risk = "reversible"                       # read_only|reversible|destructive — feeds jev_classify evidence — header_json only
review = "standard"                       # light|standard|data-loss — sets reviewer depth at stage 8 — header_json only
qa = []                                   # QA flows for stage 11 (frontend changes) — header_json only
commit = "feat(scope): one phase, one commit"  # the phase commit subject the builder uses — header_json only
subagents = true                          # default true: the builder hands each task to one subagent (its own default model); false = the builder works every task itself — header_json only
lane_group = ""                           # same value = same wave, one shared worktree and lead session; "" = own lane — header_json only

  [[phase.task]]                          # optional: splits a phase into separately accepted tasks — plans.header_json; the rows below are what the importer writes
  id = "P1.1"                             # <phase id>.<n> — tasks.id
  title = "what this task delivers, one line"  # tasks.title
  acceptance = ["the outcome that decides done, checkable by the reviewer", "the second outcome the same task must also meet"]  # tasks.acceptance; a list = two acceptance lines, and every line gets an id by position: <task id>.a1, <task id>.a2 (a plain string is the single line <task id>.a1)
  refs = []                               # optional: path:line pointers the brief quotes — header_json only
  subagent = false                        # true only inside a subagents=true phase; then owned is required — header_json only
  owned = []                              # required when subagent = true; subset of the phase's owned — tasks.owned
  gate = { cmd = "<the command that proves this task>", expect = 0, timeout = 600, lines = ["P1.1.a1"] }  # the task must pass this command; `lines` names the acceptance line ids (above) it proves and mk-plan check errors on an id that is not one of <task id>'s lines; `gate` may also be a LIST of such tables when the task needs several commands — plans.header_json
  critical = true                         # bool, default false: a task the plan cannot ship without; approving a file that flips this true -> false needs --user-ack — header_json only
  risk = 3                                # this task's base risk as an int 1-5 (default 1): approving a file that LOWERS it needs --user-ack; separate from the phase-level risk string above — header_json only

  [[phase.task.subtask]]                  # optional: splits a task into subtasks; id is <phase>.<n>.<m> — plans.header_json
  id = "P1.1.1"                           # <phase id>.<n>.<m>; the importer derives tasks.type="subtask" and tasks.parent from the nesting
  title = "what this subtask delivers, one line"  # tasks.title
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
# This H1 line is what becomes plans.title (after D1; the plan id is the title until then),
# so keep it to one line and start it with the plan id. `mk-plan.py check` warns when it is missing.

<Where the request came from and what is already decided: user words, research docs (`design`), prior runs. The header is the contract every phase builds against; the body is the context.>

## Goal
<One paragraph: what is true when this plan is done.>

## Design / context
<What is wrong today. The shape of the change, with a file map if it helps. Anything the phases must agree on goes here as a numbered contract (C1, C2, …) — phases own disjoint files and meet only through it.>

## Phase notes
<One `### <phase id> — <title>` block per phase: build order, pointers the briefs need (`path:line`), hazards.>

## Risks
<What can go wrong and the guard for each: a review depth, a test, a fallback.>

## Open questions
<None left, or the question plus who answers it. Anything here blocks approval.>

## Log
- <date> — drafted.
- <date> — <decision + who took it; one line each; append only.>
