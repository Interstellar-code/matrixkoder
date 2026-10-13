+++
# The plan file — every header key below names where it lands in the project store
# (mk.db; docs/state-schema.md § "Plan file → DB" says the same). This template is the
# contract between a plan file and the DB, so a writer never has to read the code.
# Every key is optional and additive: a plan that omits one still checks clean.
#
# Lifecycle (C13):
#   import <file>    loads the file into the store WITHOUT approving it (command added by D1)
#   approve <file>   checks it, versions it, writes the tasks, copies the file to
#                    <data>/plans/archive/<id>-v<n>.md and leaves it in <data>/plans/
#   run init --plan  starts the run for an approved plan
#   After import the run and mk.db are the source: status, tasks, rounds and verdicts change
#   there, never in this file — MK does not edit the file, it is a reference.
#   When the plan is done, the file moves to <data>/plans/archive/.
plan = "feature-id"                       # id [a-z0-9][a-z0-9-]{0,40}; the file is <data>/plans/<id>.md — plans.id (plans.title until D1 fills the H1 title)
status = "draft"                          # draft|approved|running|done|parked; run init --plan wants approved — plans.status (column added by D1; header_json until then)
created = 2026-10-08                      # date the plan was drafted — plans.created (column added by D1; header_json until then)
runs = []                                 # run ids; mk-plan.py add-run appends, never by hand — plans.header_json
design = []                               # optional: research/spike docs behind the decisions (paths) — plans.design (column added by D1; header_json until then)
integration = "mk/<feature>/integration"  # optional: branch the lanes merge into; default from matrixkoder.yaml — plans.integration (column added by D1; header_json until then)

[gates]                                   # full-suite gates the orchestrator reruns at merge and QA; scoped forms go in briefs — plans.header_json
test = "<test cmd>"                       # e.g. pnpm vitest run — plans.header_json
typecheck = "<typecheck cmd>"             # e.g. pnpm exec tsc --noEmit — plans.header_json
selftest = "<self-test cmd of the changed scripts>"  # e.g. python3 scripts/<name>.py --self-test; the full bash scripts/selftest.sh runs once on the integration tip, not per task — plans.header_json
validate = "<validate cmd>"               # e.g. claude plugin validate . — plans.header_json

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
  acceptance = "the outcome that decides done, checkable by the reviewer"  # tasks.acceptance
  refs = []                               # optional: path:line pointers the brief quotes — header_json only
  subagent = false                        # true only inside a subagents=true phase; then owned is required — header_json only
  owned = []                              # required when subagent = true; subset of the phase's owned — tasks.owned

  [[phase.task.subtask]]                  # optional: splits a task into subtasks; id is <phase>.<n>.<m> — plans.header_json
  id = "P1.1.1"                           # <phase id>.<n>.<m>; the importer derives tasks.type="subtask" and tasks.parent from the nesting
  title = "what this subtask delivers, one line"  # tasks.title
  acceptance = "the outcome that decides this subtask done"  # tasks.acceptance
  deps = []                               # comma-separated ids in the same plan, or <run>:<id> to wait on another run — tasks.deps
  parallel = false                        # true: parallel-flagged siblings with no dep between them may run at once — tasks.parallel
  route = ""                              # "" or "<agent> <model> [<effort>]"; or an inline table {agent, model, effort} — tasks.route_json
  owned = []                              # files this subtask touches — tasks.owned
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
