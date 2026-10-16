# Experiment: grilling vs ralplan (2026-10-16)

Question: which planning skill gives the better plan for MatrixKoder work, the
`grilling` skill or OMC `ralplan`?

## Setup

- Input: the parked plan `mk-ponytail-hooks` (`plan-input.md`).
- Each skill ran in its own herdr pane, Claude Sonnet 5.5 at medium effort, same
  prompt apart from the skill name and output file: read the plan and the code it
  names, edit nothing, ask decisions of the plan owner, write one output plan.
- The orchestrator (Opus 5.5) answered every pane question as plan owner. A
  question asked by both panes got the same answer.
- The first ralplan run sent its Architect and Critic passes to Opus subagents
  (5 iterations, about 700K Opus tokens). To compare like with like, ralplan was
  rerun in a fresh session with no subagents ("solo"); the transcript shows zero
  Agent calls.

| File | Skill | Questions asked | Subagents |
|---|---|---|---|
| `plan-grilled.md` | grilling | 13, in 2 rounds, plus a summary to confirm | none |
| `plan-ralplan-solo.md` | ralplan, no subagents | 3 | none |
| `plan-ralplan-opus.md` | ralplan, default | 4 | Opus Architect + Critic, 5 rounds |

## Blind review

One Opus critic, read-only, scored the three plans with names and method words
redacted (labels shuffled). Criteria scored 1-5: facts against the code, risk
coverage, buildability, minimality, internal consistency.

| Plan | Fact | Risk | Build | Minimal | Consist | Total |
|---|---|---|---|---|---|---|
| ralplan + Opus review | 4 | 4 | 4 | 2 | 4 | 18 |
| ralplan solo | 3 | 3 | 3 | 4 | 3 | 16 |
| grilling | 3 | 3 | 3 | 3 | 2 | 14 |

All three needed revision before a build. Notable findings:

- grilling alone got the omp injection API right (`before_agent_start`, not
  `session_start`), filtered reviewer panes, and capped reviews per round. Its
  worst defects were recommendations the owner accepted without a code check
  (mkpaths inside a lane, holding back the commit announce, an unregistered bus
  kind).
- ralplan solo found more code facts on its own (ruleset text never reaches the
  lane, bus kind `review` already taken) but made several decisions silently and
  its in-head Architect/Critic passes cannot be audited.
- The Opus review added the mkpaths gap, the mk-config unknown-key refusal and the
  fallback-file wiring, at a large token cost and with scope growth.

## Verdict

Use ralplan for MK plans. The user runs many projects, plans and executions at
once and wants plan output per minute of chat, not a perfect plan. ralplan does
its own code homework, asks few questions, and has a built-in Architect/Critic
review loop. Steering happens later: once the plan becomes MK builder tasks, each
task states what it does and the user edits any task that drifts. Plans should end
with a short "decisions I made for you" list so silent assumptions stay visible.

grilling still fits when the decision is the user's own: a new direction or an
unsettled scope.

Limits: one plan, one blind judge (a Claude model judging Claude-written plans),
no tests or live omp session run.
