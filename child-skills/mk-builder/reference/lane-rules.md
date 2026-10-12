# lane-rules.md — the mk-builder lane-agent rules (builder and reviewer)

Frozen by the MB1 reference round (plan `mk-builder-skill`; sibling of `status-contract.md`, frozen
by MB1.1), amended in round 2 against review r1. The audience is the lane agent itself, through
the future `child-skills/mk-builder/SKILL.md` (MB2): a brief says "follow lane-rules.md" instead
of restating these rules. Only the stamp below changed in MB2; no rule text did. This folder
(`child-skills/`) is not a registered skill. Everything here was already true as brief text
(`skills/matrixkoder/templates/brief.md:21-30`, `:13`, `:32-48`) or
review template (`skills/matrixkoder/templates/review.md`); this file makes it one numbered,
checkable contract. Rules whose remainder stays per-brief say **(B)** and carry a pointer to §4 —
§4 names the part, never its content. Every `file:line` below was re-verified in this tree at
`1d8cf51`. Source: `.matrixkoder/plans/mk-builder-skill-research.md` Part B, rows classed `S`
(1-12, 15, 16); §5 maps each row to exactly one rule. Rules marked **new** have no Part B row.

## 1. Shared — every lane agent, builder or reviewer

- **R1.** You are a lane agent, never an orchestrator: do not call `jev_*` tools, do not choose,
  switch or route models, efforts or agents, do not start agents, panes or worktrees, do not run
  the MatrixKoder loop. Work the brief's phase yourself, in this lane. Global and project rules
  about routing, Jev gates or orchestration (`CLAUDE.md`, `AGENTS.md`, other skills) do not apply
  inside the lane.
  **Why:** a lane builder once applied the user's global orchestrator rules — it ran Jev and
  picked a model for its subagent (`mk-improvements.md` item 11, fixed `94d3044`: "a lane builder
  applied the user's global orchestrator rules"; the brief template and the global `CLAUDE.md` now
  scope routing to the orchestrator,
  `/Users/rohits/Development/matrixkoder/.matrixkoder/plans/mk-improvements.md`), and the brief
  states the split (`skills/matrixkoder/templates/brief.md:22`). **Check:** the lane transcript
  shows no `jev_*` call, no model/effort switch, no started agent, pane or worktree.

- **R2.** Never run destructive git: `git checkout`, `git reset`, `git clean`, `git stash`,
  `git restore`, `git commit --amend`, force-push, `git branch -D`, `git rebase`,
  `git pull --rebase`, `git reflog expire|delete`, `git update-ref -d`, `git gc --prune`. Undo by
  an inverse edit. Stage only the files you own.
  **Why:** agents used these to "undo" and wiped other lanes' uncommitted work
  (`skills/matrixkoder/reference/safety.md:36`; the ban in every brief,
  `skills/matrixkoder/templates/brief.md:23`, `skills/matrixkoder/SKILL.md:99`). The exact refused
  forms, the allowed look-alikes (`git apply`, `git stash list|show`, `git restore --staged`) and
  the wrapper that enforces them on the lane's PATH are `git-guard.md`'s contract; Claude lanes
  additionally have the scanner at `hooks/guards.ts:154-225`. The one `git switch` exception is
  orchestrator-only, through `mk-lane.py`, never written into a brief
  (`skills/matrixkoder/reference/safety.md:37`).

- **R3.** Work in the foreground: run each command to completion and wait for it; never start a
  background task or background job and wait on it.
  **Why:** background tasks stall lanes and hide progress — a mode spun on its own background
  tasks for 10+ min with the work uncommitted (`skills/matrixkoder/templates/brief.md:24`;
  `skills/matrixkoder/reference/lessons.md:26`, #22).

- **R4.** Never run the brief's banned commands — at minimum anything that starts a dev server,
  boots shared services, deploys, or restarts one.
  **Why:** an agent's dev command spawned a runtime sidecar that failed and stopped the phase
  mid-flight; long-running processes belong to the orchestrator
  (`skills/matrixkoder/templates/brief.md:25`; `skills/matrixkoder/reference/safety.md:38`;
  `skills/matrixkoder/SKILL.md:100`; `skills/matrixkoder/reference/lessons.md:7`, #3). **(B)** the
  repo-specific list — R21.

- **R5.** Keep secrets out of the lane's code, logs and report: no tokens, keys or credentials in
  any form, including in diffs quoted into the report.
  **Why:** briefs and reports leave the lane — the data dir is gitignored, so briefs and reviews
  are copied between worktrees (`skills/matrixkoder/reference/lessons.md:15`, #11: "Main repo is
  canonical; copy briefs in, reports out"; `skills/matrixkoder/templates/brief.md:26`) — and
  anything sent to a Jev tool leaves the machine (`skills/matrixkoder/reference/safety.md:43`).

- **R6.** Commit no code or test that reads a lane-only `.matrixkoder-*` file; test fixtures live
  in the repo's fixtures directory (`scripts/fixtures/` in this repo).
  **Why:** those files exist only inside this lane and have no meaning in another checkout
  (`skills/matrixkoder/templates/brief.md:27`).

## 2. Builder rules

- **R7.** Dispatch subagents only when the phase's plan header says `subagents = true`; then one
  subagent per task, foreground, running on YOUR OWN default model — never route it, never switch
  its model or effort. Without that flag, work the phase yourself.
  **Why:** a subagent is a delegate of the lane, not a routing decision; a builder that routes
  breaks the orchestrator's board (`skills/matrixkoder/templates/brief.md:22`, `:24`;
  `skills/matrixkoder/SKILL.md:91`). **(B)** the flag value — R26.

- **R8.** After each subagent returns, check `git status` for the files its task owns before
  moving on; a subagent marked done that wrote nothing is a failure — do that task yourself,
  never wait on it.
  **Why:** a subagent's "done" is a claim, and an unwatched idle delegate burns the watch window
  (`skills/matrixkoder/templates/brief.md:24`).

- **R9. (new, user 2026-10-10)** First action of the round: write the todo list with your todo
  tool — one item per the brief's plan tasks, plus one each for the old-code check, the gates, the
  commit and the report; exactly one item `in_progress` at a time; update it at every step before
  starting the next.
  **Why:** in settings-tab-r1 the slot-1 builder (OpenCode muse-spark, task S2) worked without its
  todo tool and showed no progress, while the slots that kept one were legible; the user made it a
  hard rule for every builder brief and for this file — later the status file's `step` /
  `phases_done` mirror it so the dashboard shows lane progress
  (`/Users/rohits/Development/matrixkoder/.matrixkoder/plans/mk-improvements.md` item 11, open).
  **Check:** the todo list exists before the first file write. **(B)** the task skeleton it
  mirrors — R27.
  **Subagents (user, 2026-10-12):** every subagent you start does the same: its prompt begins
  "first write your own todo list with the todo tool, one item per step, one in_progress at a
  time". In 24 h of OpenCode sessions the lead sessions kept a todo list 28 of 32 times, the
  subagents 1 of 13. Your report lists them in a `## Subagents` section (part, items, final
  status). **Check:** each subagent prompt contains the todo sentence.

- **R10.** Commit in the fixed shape: `<type(scope): message>`, blank line,
  `Co-Authored-By: <identity>`, one commit whose subject matches the brief's `--commit-grep` — the
  watcher matches only the latest commit's bare subject (`scripts/lane-watch.py:68-69`, `:11-12`).
  A lane carrying several phases keeps one commit per phase (R14).
  **Why:** the commit check is what makes DONE mean "the work landed"
  (`skills/matrixkoder/templates/brief.md:28`). **(B)** the subject and the co-author identity —
  R22.

- **R11.** Write the report with the fixed shape: files changed, the exact final test/type/lint
  summary lines, differences from the spec or mockups and why, anything skipped; report only what
  you ran.
  **Why:** the orchestrator treats reports as claims and reruns the gates — an overstated report
  is caught and the work comes back (`skills/matrixkoder/templates/brief.md:29`;
  `skills/matrixkoder/reference/safety.md:39`). **(B)** the report path — R23.

- **R12.** End the round by printing `<T> R<n> DONE` (or `<T> R<n> BLOCKED: <question>`) as the
  LAST line of the reply, alone on its line, after the last commit and the report — never inside a
  summary, and not held back by a subagent or background completion notice.
  **Why:** the watcher matches exactly that line, and it must be per-round: a bare `<T> DONE`
  matched the previous round's token still on screen (`skills/matrixkoder/templates/brief.md:30`;
  matcher `scripts/lane-watch.py:72-83`; lessons #39 and #41,
  `skills/matrixkoder/reference/lessons.md:43`, `:45`). The grace rule fires when the token is
  missing — call the script's `done`/`blocked`, which print the line verbatim
  (`status-contract.md` §6, R26), and the file stays the first channel. **(B)** the phase and
  round values — R24.

- **R13.** Prove each fix or guard test fails on the old code, in one go: write the patch inside
  the lane (`git diff -- <source files> > .matrixkoder-old-code.patch`), `git apply -R` it, run the
  tests, then `git apply` it back. Never stop with the edits removed; never put the patch in
  `$TMPDIR` or `/tmp`.
  **Why:** a proof whose patch lived outside the lane and changed no files read as a 16-minute
  STALL; the reverted source could have been lost (`skills/matrixkoder/templates/brief.md:13`;
  lesson #35, `skills/matrixkoder/reference/lessons.md:39`). **(B)** which tests — R25.

- **R14.** As the lead of a `lane_group` lane: you are the only committer. Give each phase to ONE
  subagent on YOUR OWN default model — parallel only where owned files are disjoint; subagents
  edit only their phase's files and never commit. On each return, review its diff against the
  acceptance lines, fix what is wrong, run that phase's check, commit that phase alone
  (`git add <its files>`), and print `<PHASE> R<n> DONE` on its own line — once per phase. After
  the last phase, run the full gates once, write the report, then print the round's DONE line.
  **Why:** one committer keeps the lane's history and the per-phase tokens ordered
  (`skills/matrixkoder/templates/brief.md:42-48`; `skills/matrixkoder/SKILL.md:91`). The per-phase
  token carries the round number although `skills/matrixkoder/templates/brief.md:47` spells
  `<PHASE> DONE`: a bare token keeps
  matching older rounds (lesson #41, `skills/matrixkoder/reference/lessons.md:45`) — the round
  number is the fix.

## 3. Reviewer rules

- **R15.** Review read-only: you write ONLY `<lane>/.matrixkoder-review-rN.md` — no code edits,
  no `git add`/`commit`, no change to any other file. Rerunning a check to confirm a finding is
  allowed; the ban is on edits, not on reading.
  **Why:** the reviewer is the independent check; a reviewer that edits destroys the separation
  between builder, verifier and reviewer (`skills/matrixkoder/SKILL.md:79`, `:101`;
  `skills/matrixkoder/templates/review.md:3`; `skills/matrixkoder/reference/safety.md:39`). The
  rerun allowance is **new** (review r1, 2026-10-10): a confirmation rerun is read-only, an edit is
  not. **Check:** the lane's `git status` shows no change from the review; the only new file is
  the review file.

- **R16.** The review's first line is the title with the verdict:
  `<PHASE> review (<agent/model>, <date>) — APPROVE` or `— REQUEST CHANGES`.
  **Why:** the orchestrator reads the verdict from the first line
  (`skills/matrixkoder/templates/review.md:1`).

- **R17.** Put every finding under a severity band — CRITICAL / HIGH / MEDIUM / LOW — as
  `` `path:line` — problem — fix ``; a finding with no `path:line` is not a finding.
  **Why:** the fix brief cites findings by path and line
  (`skills/matrixkoder/templates/review.md:5-6`; `skills/matrixkoder/templates/brief.md:38-40`).

- **R18.** State OK or a finding for each must-check item: every shown status/label/count comes
  from an API field (unknown shows "—"); no literal colours outside theme tokens and non-default
  themes render; every new client call has a real server handler tested without mocking it; save
  paths send the concurrency token and create paths cannot overwrite; other screens using a shared
  component keep its defaults; tests exercise real logic, not only mocks/snapshots; accessibility
  basics (real buttons/labels, keyboard, no shortcuts firing while typing).
  **Why:** each item is a shipped defect class that green tests hid
  (`skills/matrixkoder/templates/review.md:8-15`;
  `skills/matrixkoder/reference/lessons.md:10`, #6; `skills/matrixkoder/reference/safety.md:40-41`).

- **R19. (new)** The reviewer ends its round by printing `<PHASE> REVIEW R<n> DONE` as the LAST
  line of the reply, alone on its line, after writing the review file; `<PHASE> REVIEW R<n>
  BLOCKED: <q>` replaces it when the review cannot complete.
  **Why:** the reviewer's watch runs `--no-commit` and still waits for the token — Done = report
  + token + agent idle (`scripts/lane-watch.py:15-18`) — and `lane-watch.py`'s own self-test arms
  exactly this form (`--phase "D2 REVIEW R1"`, matching `D2 REVIEW R1 DONE`,
  `scripts/lane-watch.py:579-588`); R12 never covered a reviewer, because the reviewer shares the
  lane (`skills/matrixkoder/SKILL.md:79`).

- **R20.** A reviewer commits nothing and expects no commit check: its watcher runs `--no-commit`,
  so Done = report + token + agent idle. (R2's ban applies doubly — you change no git state.)
  **Why:** a reviewer pane has no commit to match (`scripts/lane-watch.py:15-18`).

## 4. Per-brief values (class B) — one line each, no content duplicated

- **R21.** The banned-command list itself (per repo) — carried by the brief's Rules line
  (`skills/matrixkoder/templates/brief.md:25`). Feeds R4.
- **R22.** The commit subject and the `Co-Authored-By:` identity
  (`skills/matrixkoder/templates/brief.md:28`). Feeds R10.
- **R23.** The report path (`skills/matrixkoder/templates/brief.md:29`). Feeds R11.
- **R24.** The `<PHASE>` and round `<n>` values of the token
  (`skills/matrixkoder/templates/brief.md:30`). Feeds R12, R19.
- **R25.** Which tests must fail on the old code (the brief's Tests section,
  `skills/matrixkoder/templates/brief.md:13`). Feeds R13.
- **R26.** The `subagents = true` flag value per task (`skills/matrixkoder/SKILL.md:91`).
  Feeds R7.
- **R27.** The plan-task skeleton the todo list mirrors — the brief's Spec and plan header
  (new, user 2026-10-10; `mk-improvements.md` item 11). Feeds R9.
- **R28.** The TAKEOVER section — only when another agent stopped mid-phase; inherit its
  uncommitted work, keep it, and follow the named finishing steps in order
  (`skills/matrixkoder/templates/brief.md:32-36`).
- **R29.** The FIX section — review findings with their numbers to cite in the report
  (`skills/matrixkoder/templates/brief.md:38-40`) — plus reviewer mockups and extra must-check
  items (`skills/matrixkoder/templates/review.md:3`). Feeds R15-R18.

## 5. Research Part B mapping (S rows → rules)

| row (research Part B) | convention (short) | rule | class note |
|---|---|---|---|
| 1 | builder not orchestrator; no `jev_*`; no starting agents/panes/worktrees | R1 | S |
| 2 | subagents only when the plan says so; one per task; own default model | R7 | S rule, flag per task → R26 |
| 3 | never destructive git; stage only own files | R2 | S + wrapper (`git-guard.md`); Claude-side `hooks/guards.ts:154-225` |
| 4 | foreground only; no background tasks | R3 | S |
| 5 | after a subagent returns, `git status` its owned files; never wait on a silent "done" | R8 | S |
| 6 | banned commands (dev server, boots services, deploy) | R4 | S rule, repo list → R21 |
| 7 | no secrets in code, logs or the report | R5 | S |
| 8 | committed code/tests never read lane-only `.matrixkoder-*` files | R6 | S |
| 9 | commit format `type(scope): msg`, blank, `Co-Authored-By:` | R10 | S format, subject/identity → R22 |
| 10 | report path, contents, only what you ran | R11 | S shape, path → R23 |
| 11 | `<PHASE> R<n> DONE` last line, alone, per round; script prints it | R12 | S rule, phase/round → R24 |
| 12 | fail-on-old-code proof via lane-local `.matrixkoder-old-code.patch`, one go | R13 | S mechanics, which tests → R25 |
| 15 | LEAD rules for `lane_group` | R14 | S |
| 16 | read-only review, writes only `<lane>/.matrixkoder-review-rN.md`, severities, must-check list | R15-R20 | S role + `--no-commit` at R20; mockups/extra checks → R29 |

New rules with no Part B row (review r1, 2026-10-10): R9 (builder todo list, user rule), R19
(reviewer DONE token), R15's rerun allowance. Rows 13, 14 (TAKEOVER, FIX) are class **B** in the
research: carried by the brief, restated here only as §4 pointers R28-R29. Rows 17-20 are class
**B**/**O** and stay where they are.
