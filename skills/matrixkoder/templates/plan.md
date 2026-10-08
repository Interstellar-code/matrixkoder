+++
plan = "feature-id"                       # id [a-z0-9][a-z0-9-]{0,40}; the file is <data>/plans/<id>.md
status = "draft"                          # draft|approved|running|done|parked; run init --plan wants approved
created = 2026-10-08                      # date the plan was drafted
runs = []                                 # run ids; mk-plan.py add-run appends, never by hand
design = []                               # optional: research/spike docs behind the decisions (paths)
integration = "mk/<feature>/integration"  # optional: branch the lanes merge into; default from matrixkoder.yaml

[gates]                                   # full-suite gates the orchestrator reruns at merge and QA; scoped forms go in briefs
test = "<test cmd>"                       # e.g. pnpm vitest run
typecheck = "<typecheck cmd>"             # e.g. pnpm exec tsc --noEmit
selftest = "<selftest cmd>"               # e.g. bash scripts/selftest.sh
validate = "<validate cmd>"               # e.g. claude plugin validate .

[[phase]]                                 # one block per phase; phases in one wave must own disjoint files
id = "P1"                                 # [A-Z][A-Z0-9]{0,7}, unique; tasks are <phase id>.<n>
title = "what this phase delivers, one line"   # becomes the phase task title
wave = 1                                  # int >= 1; same-wave phases run in parallel
deps = []                                 # phase ids, or <run>:<task> to wait on another run's task
owned = ["src/area/", "src/file.ts"]      # the only files this phase touches; overlap within a wave is a check error
forbidden = []                            # optional: hot files this phase must leave alone (sequence them instead)
route = ""                                # "" = jev_decide at stage 2; "<agent> <model> [<effort>]" = pinned, skips Jev
size = "standard"                         # trivial|standard|complex — feeds jev_classify evidence
risk = "reversible"                       # read_only|reversible|destructive — feeds jev_classify evidence
review = "standard"                       # light|standard|data-loss — sets reviewer depth at stage 8
qa = []                                   # QA flows for stage 11 (frontend changes)
commit = "feat(scope): one phase, one commit"
subagents = false                         # true = the builder may hand each task to one subagent (its own default model)
lane_group = ""                           # same value = same wave, one shared worktree and lead session; "" = own lane

  [[phase.task]]                          # optional: splits a phase into separately accepted tasks
  id = "P1.1"                             # <phase id>.<n>
  title = "what this task delivers, one line"
  acceptance = "the outcome that decides done, checkable by the reviewer"
  refs = []                               # optional: path:line pointers the brief quotes
  subagent = false                        # true only inside a subagents=true phase; then owned is required
  owned = []                              # required when subagent = true; subset of the phase's owned
+++

# <plan id> — <one line: what this plan delivers>

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
