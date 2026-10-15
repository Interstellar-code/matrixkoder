# <PHASE> review (<reviewer agent/model>, <date>) — <APPROVE | REQUEST CHANGES>

Commit(s): <sha range>. Brief: <path>. Mockups: <paths>. Read-only review of the commit, not the working tree.

You run as the mk-builder skill's reviewer role (a lane agent: read-only, writes ONLY `<lane>/.matrixkoder-review-rN.md`, findings under severities, ends with the `<PHASE> REVIEW R<n> DONE` token) — those rules are the skill's, not this template's (`.claude/skills/mk-builder/`, `reference/lane-rules.md` R15-R20).

When the bus is up, this brief reaches you over it: `dispatch --role review --lane <lane>` publishes `<project>/plans/<plan>/runs/<run>/<T>/review-brief-rN.md` as a `review-brief` message and types `connect --role review`, which prints `# review brief round <n>` and the body. Otherwise the prompt names this file by absolute path — read it.

Review the commit (or the uncommitted diff) and report. Do NOT rerun the self-tests, typecheck or suite the builder and orchestrator already ran (user rule 2026-10-14) — read their output instead. Run a probe ONLY when a new proof is needed: an old-vs-new comparison, hostile input, or a temp-store trial.

## CRITICAL / HIGH / MEDIUM / LOW
- `path:line` — problem — fix

## Acceptance
One row per acceptance line of the task (the brief's Spec / Tests). Verdict is exactly MET, NOT MET or UNVERIFIED; evidence is a `path:line` or the command output you read. An APPROVE with an UNVERIFIED row opens a `no_evidence` risk reason (review-handoff), and a review without this table is flagged.
| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | <acceptance line> | <MET / NOT MET / UNVERIFIED> | <path:line or output> |

## Must-check list (state OK or finding for each)
- Invented data: every status/label/count shown comes from an API field; unknown shows "—".
- Theming: no literal colours outside theme tokens; non-default themes render.
- Routes: every new client call has a real server handler, tested without mocking the handler.
- Overwrite / data loss: saves send the concurrency token; create paths can't overwrite.
- Shared components: other screens using them keep their defaults.
- Tests exercise real logic, not just mocks/snapshots.
- Accessibility basics: real buttons/labels, keyboard, no shortcuts firing while typing.

## Verified OK
<list>

## Not verified
<list>
