+++ 1 Top block — plan identity [toml]
plan = "plan-format-refactor"
status = "draft"
created = 2026-10-24
runs = []
integration = "main"
pushed = false
/+++

+++ 2 Summary
# plan-format-refactor — the plan parser and everything that reads plan fields move to the numbered-block template

## Summary
- Phases: 9 — PARSE: Parser reads the numbered-block format; GATEKEY: Gate names agree across wizard and config; CHECK: The check enforces the new rules; STORE: The store keeps the new plan shape and the Log; CONSUM: Inbox, migrate and layout test read the new format; TSFIX: Dashboard stops showing removed fields; RUN: A run follows the new plan rules; TSNEW: Dashboard shows the new plan fields; DOCS: Docs and fixtures describe the new format
- Method: waves · subagents off
- Lanes: M1: PARSE; W1: GATEKEY; M2: CHECK, STORE; W2: CONSUM, TSFIX; M3: RUN; W3: TSNEW; M4: DOCS
- Delivery: main · pushed: no
- Gates: test; typecheck; selftest = bash scripts/selftest.sh; oldnames = ! rg -n 'lint_changed|format_changed' hooks scripts skills docs types
/+++

+++ 3 Goal
## Goal
- Done when:
  - G1 — `mk-plan.py` parses, checks, imports and approves a plan written in the numbered-block template, and a run starts from it
  - G2 — the 18 stored old-format plans and the 4 live runs still read, run and close unchanged
  - G3 — the new check rules flag each case the template promises (links, risks, open questions, settings, Summary legend)
  - G4 — the dashboard, wizard and Inbox accept the new fields and gate names and no longer show removed fields
  - G5 — the docs, templates and fixtures describe only the new format
- Why: the template was redesigned and nothing reads it yet; until the parser matches, the template cannot be committed or used
- From: the user, 2026-10-24, in the handoff after the plan-template review
- Sources: `assets/blast-A.md` (scripts, skills, docs) and `assets/blast-B.md` (hooks TypeScript) in this folder
/+++

+++ 4 Design / context
## Design / context

### What is wrong today
1. `parse` reads a TOML header plus a free body, and the plan builders in the self-test emit old TOML (`scripts/mk-plan.py:3046-3068`).
2. The self-test demands that the shipped template checks clean with no warnings (`scripts/mk-plan.py:3106`, `:3414`); the new template is full of `<...>` placeholders.
3. A task gate is a table `{cmd, expect, timeout, lines}` in the run (`scripts/mk-state.py:2058-2334`); the new task gate is a string.
4. The Inbox counts seven sections (`scripts/mk-inbox.py:168`, `:173`, `:176`); the template has nine blocks.
5. The wizard writes `lint_changed`, `format_changed` and `install` gates (`hooks/wizard-core.ts:176-246`, `:555-560`).
6. Eighteen stored plans in three project databases carry old-format `header_json`, and no migration script exists (`assets/blast-A.md` §5).

### Shape of the change
The parser reads blocks by name and keeps a read-only reader for the old format. `parse` keeps handing the old run-side shape to `mk-state.py` and `mkdb.py` (a shim), so the run side changes little; the dead fields are removed in a later plan. Files: `scripts/mk-plan.py` (parse, check, store, set), `scripts/mk-config.py` and the YAML (gate names), `scripts/mkdb.py`, `scripts/mk-state.py`, `scripts/mk-report.py`, `scripts/mk-inbox.py`, `scripts/mk-migrate.py`, the hooks TypeScript, and the docs.

### Contracts
C1 — `parse` keeps the old run-side reply shape (task gate string → `[{cmd, expect: 0, timeout, lines}]`, plan gate → a `final_gates` entry with `every = "run_end"`) — written by PARSE, read by STORE, RUN
C2 — shape of `plans.header_json` and `plan_items.extra_json` for the new fields (goals, summary, questions, log, `covers`, `serves`, `proves`) — written by STORE, read by RUN, CONSUM, TSNEW
C3 — project gate names `test`, `typecheck`, `lint`, `build` in the project YAML; `lint_changed`, `format_changed` and `install` are gone — written by GATEKEY, read by CHECK, TSFIX
C4 — a store command that appends a Log line, ticks a question and records an override after approval — written by STORE, read by RUN, TSNEW

### Test plan
T1 — a new-format plan parses to the expected blocks, ids and links — unit — `scripts/test-mk-plan.py` — written by PARSE.2
T2 — every stored old plan (a read-only copy of the three project databases) still parses — integration — `scripts/test-mk-plan.py` — written by PARSE.3
T3 — each new check rule flags its case and passes a clean plan — unit — `scripts/test-mk-plan.py` — written by CHECK.2
T4 — an unticked open question blocks dispatch of the task it names — unit — `scripts/mk-state.py --self-test` — written by RUN.2
T5 — a run started before this change, with the old gate shape, still closes — integration — `scripts/mk-state.py --self-test` — written by RUN.1
T6 — the wizard writes and drift-checks the new gate names — unit — `hooks/wizard.test.ts` — written by GATEKEY.2
T7 — the plan detail renders goals, risks, questions and the Log — unit — `hooks/tabs/plan.test.ts` — written by TSNEW.1

### Assumptions and dependencies
A1 — Orca can start an AGY worker and an OMP worker — if not, that lane falls back to Claude Sonnet 5.5 high and the user is told
A2 — the four live runs finish on the old shape before any old-shape reader is removed — if not, the shim stays longer

### Rollout and undo
R1 — ships by merge of the run's branch into `main`; undo = revert the merge; stored plans are not rewritten, so no data change — DOCS.2

### Settings and limits
S1 — most lanes at once — 4 — `mk-config.py list --json` (`routing_preferences.max_build_lanes`) — 2026-10-24
S2 — active agents — claude, agy, omp, opencode; codex needs a login — `~/.matrixkoder/usage.json` — 2026-10-24
S3 — limits left — Claude 5h 6% used, week 50% used, Fable-only 8%; AGY Gemini 5h 11%, week 55%; AGY Claude/GPT exhausted; OpenCode month 64% — `~/.matrixkoder/usage.json` — 2026-10-24
S4 — gates the project defines — not read yet, see Q2 — none — 2026-10-24
S5 — integration branch — main — `mk-config.py list --json` (`repo.integration_branch`) — 2026-10-24

### Out of scope
Removing the shim and the dead fields from the run side; migrating stored plans or live runs to the new format; the Orca or MK orchestration tooling itself.
/+++

+++ 5 Phases
## Phases

### Legend — task risk and what it requires
risk 1 trivial: no gate, light review. risk 2 low: no gate (the plan gate covers it), light review. risk 3-4: a gate command or manual, standard review. risk 5: destructive or data loss, a gate command, data-loss review, user asked first. A phase has no gate or risk of its own.

### Legend — lanes in this plan
Two worktrees carry the work, reused wave after wave. Column M is the main checkout on a short branch, built by AGY gemini-3.8-flash-high. Column W is a new Orca worktree, built by OMP ollama-cloud/deepseek-v4.1-flash:1017 at high effort. A phase's `lane_group` names its column and wave (M1, W1, M2 …). Reviews run on Claude Code Opus. This session orchestrates and builds nothing.
/+++

+++ 5.1 PARSE — Parser reads the numbered-block format
### PARSE — Parser reads the numbered-block format
- Goal: `mk-plan.py parse` reads a new-format plan into the plan dict and still reads every stored old-format plan.
- Serves: G1, G2
- Needs first: nothing; every other phase reads this parser's output.
- Build order: PARSE.1, PARSE.2, PARSE.3; read `scripts/mk-plan.py` functions at lines 63-340, 396-462, 777-940 first.
- Hazards: K1, K2, K3
- Carries: C1, T1, T2
/+++

+++ 5.1.0 PARSE settings [toml]
id = "PARSE"
title = "Parser reads the numbered-block format"
wave = 1
owned = ["scripts/mk-plan.py", "scripts/test-mk-plan.py"]
commit = "feat(plan): parse the numbered-block plan format"
deps = []
route = "agy gemini-3.8-flash-high"
reviewer = "claude opus high"
size = "complex"
subagents = false
lane_group = "M1"
qa = []
/+++

+++ 5.1.1 PARSE.1 — Move the plan self-tests to their own file [toml]
id = "PARSE.1"
title = "Move the plan self-tests to their own file"
covers = []
acceptance = [
  "`scripts/test-mk-plan.py` holds the self-tests and `python3 scripts/mk-plan.py --self-test` still runs them",
  "the assert count is the same as before the move (357)",
]
risk = 3
size = "standard"
gate = "python3 scripts/test-mk-plan.py"
/+++

+++ 5.1.2 PARSE.2 — Read the block format [toml]
id = "PARSE.2"
title = "Read the block format"
covers = ["C1", "T1"]
acceptance = [
  "blocks open with `+++ <number> <name>` and close with `/+++`, are read by name, and number gives order and nesting",
  "toml blocks and the text lines (goals, contracts, risks, questions, Log, Summary, phase lines) are parsed into the plan dict",
  "`parse` keeps the old run-side reply shape (C1)",
  "`set` and `add-run` edit block 1",
]
risk = 3
gate = "python3 scripts/test-mk-plan.py"
deps = ["PARSE.1"]
/+++

+++ 5.1.3 PARSE.3 — Keep reading old-format plans [toml]
id = "PARSE.3"
title = "Keep reading old-format plans"
covers = ["T2"]
acceptance = [
  "a file whose first line is the old TOML header is read by the legacy reader and produces the same plan dict as before",
  "all 18 stored plans parse from a read-only copy of the project databases",
]
risk = 4
gate = "python3 scripts/test-mk-plan.py"
deps = ["PARSE.2"]
/+++

+++ 5.2 GATEKEY — Gate names agree across wizard and config
### GATEKEY — Gate names agree across wizard and config
- Goal: the wizard writes, and `mk-config.py` reads, the gate names `test`, `typecheck`, `lint` and `build`, and no old gate name remains.
- Serves: G4
- Needs first: nothing; it does not touch the parser.
- Build order: GATEKEY.1 then GATEKEY.2; read `hooks/wizard-core.ts:176-246` first.
- Hazards: K4, K5
- Carries: C3, A1, T6
/+++

+++ 5.2.0 GATEKEY settings [toml]
id = "GATEKEY"
title = "Gate names agree across wizard and config"
wave = 1
owned = ["hooks/wizard-core.ts", "hooks/wizard-steps-a.tsx", "hooks/wizard.tsx", "hooks/wizard.test.ts", "hooks/wizard-steps-a.test.ts", "types/index.d.ts", "scripts/mk-config.py", "scripts/mk-livetest.py", "skills/matrixkoder/templates/matrixkoder.yaml", "scripts/fixtures/matrixkoder.yaml"]
commit = "refactor(gates): rename project gate names"
deps = []
route = "omp ollama-cloud/deepseek-v4.1-flash:1017 high"
reviewer = "claude opus high"
size = "standard"
subagents = false
lane_group = "W1"
qa = []
/+++

+++ 5.2.1 GATEKEY.1 — Rename gate keys in config and YAML [toml]
id = "GATEKEY.1"
title = "Rename gate keys in config and YAML"
covers = ["C3", "A1"]
acceptance = [
  "the YAML templates, `mk-config.py` and `mk-livetest.py` use `test`, `typecheck`, `lint`, `build` and no `lint_changed`, `format_changed` or `install`",
  "`python3 scripts/mk-config.py --self-test` passes",
]
risk = 3
gate = "python3 scripts/mk-config.py --self-test"
/+++

+++ 5.2.2 GATEKEY.2 — Rename gates in the wizard and types [toml]
id = "GATEKEY.2"
title = "Rename gates in the wizard and types"
covers = ["T6"]
acceptance = [
  "`MkWizardGates`, `proposeGates`, `buildYaml`, `DRIFT_GATES` and the `wz-gate-install` focus key use the new names",
  "the wizard tests and the typecheck pass",
]
risk = 3
gate = "claude plugin test . && tsc -p . --noEmit"
deps = ["GATEKEY.1"]
/+++

+++ 5.3 CHECK — The check enforces the new rules
### CHECK — The check enforces the new rules
- Goal: `mk-plan.py check` applies the old rules and every new link, risk, question, settings and Summary rule, and reports the template as TEMPLATE.
- Serves: G3
- Needs first: PARSE for the plan dict; GATEKEY for the gate names.
- Build order: CHECK.1, CHECK.2, CHECK.3; read `check_plan` at `scripts/mk-plan.py:511-772` first.
- Hazards: K6, K7
- Carries: T3
/+++

+++ 5.3.0 CHECK settings [toml]
id = "CHECK"
title = "The check enforces the new rules"
wave = 2
owned = ["scripts/mk-plan.py", "scripts/test-mk-plan.py", "scripts/mk-config.py"]
commit = "feat(plan): check the new plan rules"
deps = ["PARSE", "GATEKEY"]
route = "agy gemini-3.8-flash-high"
reviewer = "claude opus high"
size = "complex"
subagents = false
lane_group = "M2"
qa = []
/+++

+++ 5.3.1 CHECK.1 — Re-express the old rules [toml]
id = "CHECK.1"
title = "Re-express the old rules"
covers = []
acceptance = [
  "every old rule that still applies is enforced on the new plan dict",
  "the new template reports TEMPLATE, a filled example checks clean",
]
risk = 2
/+++

+++ 5.3.2 CHECK.2 — Add the link and risk rules [toml]
id = "CHECK.2"
title = "Add the link and risk rules"
covers = ["T3"]
acceptance = [
  "a goal no phase serves or no gate proves is flagged; a block 4 id no task carries is flagged; a task naming a missing id is flagged",
  "a risk whose task has a lower risk than its level, a level 3 or more risk with no gate or test guard, a task at risk 3 or more no risk line mentions, a task covering more than 5 ids, and an unknown Log kind are flagged",
  "Summary values off the legend are flagged",
]
risk = 3
gate = "python3 scripts/test-mk-plan.py"
deps = ["CHECK.1"]
/+++

+++ 5.3.3 CHECK.3 — Settings limits and the project gates reader [toml]
id = "CHECK.3"
title = "Settings limits and the project gates reader"
covers = []
acceptance = [
  "`mk-config.py` can list the gates a project defines",
  "the check warns at approval, and refuses at run start, when phases in one wave exceed the most lanes, a route names an inactive agent, or a `use` gate is undefined",
]
risk = 3
gate = "python3 scripts/test-mk-plan.py"
deps = ["CHECK.2"]
/+++

+++ 5.4 STORE — The store keeps the new plan shape and the Log
### STORE — The store keeps the new plan shape and the Log
- Goal: import and approve store the new fields, and a Log line, a question tick and an override can be written after approval.
- Serves: G1, G2
- Needs first: CHECK, because approve runs the check and the rows follow its plan dict.
- Build order: STORE.1, STORE.2, STORE.3; read `scripts/mk-plan.py:1240-1876` and `scripts/mkdb.py:1568-1862` first.
- Hazards: K8, K9
- Carries: C2, C4
/+++

+++ 5.4.0 STORE settings [toml]
id = "STORE"
title = "The store keeps the new plan shape and the Log"
wave = 2
owned = ["scripts/mk-plan.py", "scripts/test-mk-plan.py", "scripts/mkdb.py", "docs/state-schema.md"]
commit = "feat(store): store the new plan shape"
deps = ["CHECK"]
route = "agy gemini-3.8-flash-high"
reviewer = "claude opus high"
size = "complex"
subagents = false
lane_group = "M2"
qa = []
/+++

+++ 5.4.1 STORE.1 — Store the new plan shape [toml]
id = "STORE.1"
title = "Store the new plan shape"
covers = ["C2"]
acceptance = [
  "import and approve write the new fields into `header_json` and `plan_items.extra_json`",
  "rows of an old-format plan read back as before",
]
risk = 3
gate = "python3 scripts/mkdb.py --self-test"
/+++

+++ 5.4.2 STORE.2 — Write the Log, question ticks and overrides [toml]
id = "STORE.2"
title = "Write the Log, question ticks and overrides"
covers = ["C4"]
acceptance = [
  "a command appends a Log line, ticks a question and records an override in the store, and the approved version file is not changed",
]
risk = 3
gate = "python3 scripts/test-mk-plan.py"
deps = ["STORE.1"]
/+++

+++ 5.4.3 STORE.3 — Update the mkdb readers and the schema doc [toml]
id = "STORE.3"
title = "Update the mkdb readers and the schema doc"
covers = []
acceptance = [
  "`mkdb.py plans --json` carries the new fields and no longer emits `critical`",
  "`docs/state-schema.md` § Plan file → DB matches",
]
risk = 2
deps = ["STORE.2"]
/+++

+++ 5.5 CONSUM — Inbox, migrate and layout test read the new format
### CONSUM — Inbox, migrate and layout test read the new format
- Goal: the Inbox counts nine blocks and the migrate and layout tests build their plans in the new format.
- Serves: G1, G2
- Needs first: PARSE, because the Inbox and the tests read a plan the parser must understand.
- Build order: CONSUM.1 then CONSUM.2; read `scripts/mk-inbox.py:168-176` first.
- Hazards: K10
- Carries: none
/+++

+++ 5.5.0 CONSUM settings [toml]
id = "CONSUM"
title = "Inbox, migrate and layout test read the new format"
wave = 2
owned = ["scripts/mk-inbox.py", "scripts/mk-migrate.py", "scripts/mk-e2e-layout.py", "child-skills/mk-builder/scripts/mk-builder.py", "scripts/fixtures/"]
commit = "refactor(plan): move the plan consumers to the new format"
deps = ["PARSE"]
route = "omp ollama-cloud/deepseek-v4.1-flash:1017 high"
reviewer = "claude opus high"
size = "standard"
subagents = false
lane_group = "W2"
qa = []
/+++

+++ 5.5.1 CONSUM.1 — Inbox counts nine blocks [toml]
id = "CONSUM.1"
title = "Inbox counts nine blocks"
covers = []
acceptance = [
  "the Inbox reports `n of 9 blocks found` and names missing blocks",
  "`python3 scripts/mk-inbox.py --self-test` passes",
]
risk = 2
/+++

+++ 5.5.2 CONSUM.2 — Move migrate and layout fixtures to the new format [toml]
id = "CONSUM.2"
title = "Move migrate and layout fixtures to the new format"
covers = []
acceptance = [
  "the plan text in the migrate self-test, `PLAN_TEMPLATE` in the layout test, and the plan fixtures are in the new format",
  "`python3 scripts/mk-migrate.py --self-test` and `python3 scripts/mk-e2e-layout.py --self-test` pass",
]
risk = 3
gate = "python3 scripts/mk-migrate.py --self-test"
deps = ["CONSUM.1"]
/+++

+++ 5.6 TSFIX — Dashboard stops showing removed fields
### TSFIX — Dashboard stops showing removed fields
- Goal: the dashboard no longer shows `critical`, plan `design` or plan `description`, and the Inbox card says nine blocks.
- Serves: G4
- Needs first: CONSUM and GATEKEY, because the card reads the Inbox reply and the types file is shared.
- Build order: TSFIX.1; read `hooks/tabs/plans-map.ts:112-155` first.
- Hazards: none
- Carries: none
/+++

+++ 5.6.0 TSFIX settings [toml]
id = "TSFIX"
title = "Dashboard stops showing removed fields"
wave = 2
owned = ["hooks/tabs/", "hooks/dashboard.tsx", "types/index.d.ts"]
commit = "refactor(dashboard): drop removed plan fields"
deps = ["CONSUM", "GATEKEY"]
route = "omp ollama-cloud/deepseek-v4.1-flash:1017 high"
reviewer = "claude opus high"
size = "standard"
subagents = false
lane_group = "W2"
qa = []
/+++

+++ 5.6.1 TSFIX.1 — Remove dead fields and change the Inbox wording [toml]
id = "TSFIX.1"
title = "Remove dead fields and change the Inbox wording"
covers = []
acceptance = [
  "`critical`, plan `design` and plan `description` are not rendered and their tests are updated",
  "the Inbox card and the Inbox fixtures say nine blocks",
]
risk = 2
/+++

+++ 5.7 RUN — A run follows the new plan rules
### RUN — A run follows the new plan rules
- Goal: a run closes on one plan gate after the last merge, an old-shape run still closes, and an unticked question blocks its task.
- Serves: G1, G2, G3
- Needs first: STORE, because the run reads the stored rows and the write path.
- Build order: RUN.1 then RUN.2; read `scripts/mk-state.py:2058-2334` first.
- Hazards: K11, K12
- Carries: T4, T5, A2
/+++

+++ 5.7.0 RUN settings [toml]
id = "RUN"
title = "A run follows the new plan rules"
wave = 3
owned = ["scripts/mk-state.py", "scripts/mk-report.py"]
commit = "feat(run): run the plan gate once and block on open questions"
deps = ["STORE"]
route = "agy gemini-3.8-flash-high"
reviewer = "claude opus high"
size = "complex"
subagents = false
lane_group = "M3"
qa = []
/+++

+++ 5.7.1 RUN.1 — Run the plan gate once and keep the old shape [toml]
id = "RUN.1"
title = "Run the plan gate once and keep the old shape"
covers = ["T5", "A2"]
acceptance = [
  "the plan gate runs once after the last task merges and the run is done only when it passes",
  "a run started before this change, with the old gate shape, still closes",
]
risk = 4
gate = "python3 scripts/mk-state.py --self-test"
/+++

+++ 5.7.2 RUN.2 — Block a task on its open question [toml]
id = "RUN.2"
title = "Block a task on its open question"
covers = ["T4"]
acceptance = [
  "dispatch of a task refuses while a question that names it is unticked, and works after an answer or an override",
  "run start refuses when phases in one wave exceed the most lanes, a route names an inactive agent, or a `use` gate is undefined",
]
risk = 3
gate = "python3 scripts/mk-state.py --self-test"
deps = ["RUN.1"]
/+++

+++ 5.8 TSNEW — Dashboard shows the new plan fields
### TSNEW — Dashboard shows the new plan fields
- Goal: the plan detail shows goals, the Summary, `covers` and `serves` links, risks, open questions and the Log.
- Serves: G4
- Needs first: STORE, because the new fields come from the `mkdb` reply.
- Build order: TSNEW.1; read `hooks/tabs/plan.tsx` first.
- Hazards: none
- Carries: T7
/+++

+++ 5.8.0 TSNEW settings [toml]
id = "TSNEW"
title = "Dashboard shows the new plan fields"
wave = 3
owned = ["hooks/tabs/", "hooks/dashboard.tsx", "types/index.d.ts"]
commit = "feat(dashboard): show the new plan fields"
deps = ["STORE"]
route = "omp ollama-cloud/deepseek-v4.1-flash:1017 high"
reviewer = "claude opus high"
size = "standard"
subagents = false
lane_group = "W3"
qa = []
/+++

+++ 5.8.1 TSNEW.1 — Render the new fields [toml]
id = "TSNEW.1"
title = "Render the new fields"
covers = ["T7"]
acceptance = [
  "the plan detail renders goals, risks, open questions (unticked ones marked as blocking) and the Log",
  "`pushed` and the per-task `size` are shown",
]
risk = 2
/+++

+++ 5.9 DOCS — Docs and fixtures describe the new format
### DOCS — Docs and fixtures describe the new format
- Goal: no skill, doc, README or fixture describes the old format as current, and the template is committed.
- Serves: G5
- Needs first: every other phase, because the docs describe what shipped.
- Build order: DOCS.1 then DOCS.2.
- Hazards: none
- Carries: R1
/+++

+++ 5.9.0 DOCS settings [toml]
id = "DOCS"
title = "Docs and fixtures describe the new format"
wave = 4
owned = ["skills/", "docs/", "README.md", "CHANGELOG.md", "scripts/fixtures/"]
commit = "docs(plan): describe the numbered-block plan format"
deps = ["RUN", "TSNEW", "TSFIX"]
route = "agy gemini-3.8-flash-high"
reviewer = "claude opus high"
size = "standard"
subagents = false
lane_group = "M4"
qa = []
/+++

+++ 5.9.1 DOCS.1 — Update the skill, docs and fixtures [toml]
id = "DOCS.1"
title = "Update the skill, docs and fixtures"
covers = []
acceptance = [
  "`SKILL.md`, `reference/loop.md`, `reference/pool-and-usage.md`, `templates/brief.md` and the docs name the new fields and drop the old ones",
  "the brief template shows the plan goals, the phase goal and the task `covers` ids",
  "the install command has a stated source now that `install` is not a gate (from the lockfile or a non-gate key such as `repo.install`), and `templates/brief.md:35,38` and `SKILL.md:149` say so",
  "`docs/howto.md:30`, `docs/plan.md:194`, `reference/onboarding.md:11,22` and `reference/loop.md:8` name the four gates test, typecheck, lint, build",
]
risk = 2
/+++

+++ 5.9.2 DOCS.2 — Changelog and template commit [toml]
id = "DOCS.2"
title = "Changelog and template commit"
covers = ["R1"]
acceptance = [
  "`CHANGELOG.md` records the change and `skills/matrixkoder/templates/plan.md` is part of the merged branch",
]
risk = 1
/+++

+++ 6 Gates
## Gates
- test — proves G1, G2, G3 — the Python tests pass on new-format and stored old-format plans
- typecheck — proves G4 — the dashboard and wizard types agree with the new fields
- selftest — proves G1, G2, G5 — every script self-test passes, fixtures included
- oldnames — proves G4, G5 — no old gate name remains in the code or docs
/+++

+++ 6.0 Gate settings [toml]
use = ["test", "typecheck"]
timeout = 900
[extra]
selftest = "bash scripts/selftest.sh"
oldnames = "! rg -n 'lint_changed|format_changed' hooks scripts skills docs types --glob '!*.test.ts' | rg -v 'GATE_ALIASES|GATE_KEY_ALIASES|gates_of|self-test|28b'"
/+++

+++ 7 Risks
## Risks
- K1 — the legacy reader misses a stored plan shape — level 4 — guard: T2 reads all 18 stored plans — PARSE.3
- K2 — the new block grammar mis-reads a block, built by a fast model — level 3 — guard: T1 and the Opus review — PARSE.2
- K3 — moving the self-tests drops or changes an assert — level 3 — guard: the gate compares the assert count with the 357 baseline — PARSE.1
- K4 — an old gate name stays in a YAML an existing project holds — level 3 — guard: T6 and the `oldnames` gate — GATEKEY.1
- K5 — the wizard writes a gate name `mk-config.py` does not read — level 3 — guard: T6 — GATEKEY.2
- K6 — a new check rule flags a valid plan — level 3 — guard: T3 includes a clean filled plan — CHECK.2
- K7 — the project-gates reader fails on a project with no gates block — level 3 — guard: T3 includes that case — CHECK.3
- K8 — the new `header_json` shape breaks readers of stored rows — level 3 — guard: T2 and the `mkdb.py` self-test — STORE.1
- K9 — the write path changes an approved version file — level 3 — guard: the gate asserts the archive file is unchanged — STORE.2
- K10 — migrate and layout fixtures drift from real plans — level 3 — guard: the `mk-migrate.py` self-test — CONSUM.2
- K11 — a live run with the old gate shape cannot close — level 4 — guard: T5 — RUN.1
- K12 — the question gate blocks the wrong tasks — level 3 — guard: T4 — RUN.2
/+++

+++ 8 Open questions
## Open questions
- [x] Q1 — is the OMP model `ollama-cloud/deepseek-v4.1-flash:1017` (the user wrote "DeepSpace 4.11017")? — blocks: GATEKEY — pick: yes, it is the only 4.1 / 1017 entry in the OMP list
- [x] Q2 — which commands run the hooks tests and the typecheck, and does the project define `test` and `typecheck` gates? — blocks: GATEKEY.2 — pick: `claude plugin test .` and `tsc -p . --noEmit` (README.md:101-103)
- [ ] Q3 — what does `pushed` mean? — blocks: none — pick: push the finished branch to the remote
- [ ] Q4 — is phase `subagents` the same as task `subagent`? — blocks: none — pick: yes, the phase flag allows it and the task flag uses it
- [ ] Q5 — what format does `qa` take? — blocks: PARSE.2 — pick: a list of flow names, passed on as written
- [ ] Q6 — keep the old run-side shape (shim) instead of rewriting the run side? — blocks: STORE.1 — pick: shim
- [ ] Q7 — keep a read-only legacy reader for old plans? — blocks: PARSE.3 — pick: yes
- [ ] Q8 — may a lane group hold phases that depend on each other inside one wave (CHECK then STORE)? — blocks: CHECK — pick: yes, the dependency orders them
/+++

+++ 9 Log
## Log
- 2026-10-24 — drafted — the plan parser and the plan consumers move to the numbered-block template — user
- 2026-10-24 — decision — orchestrate with Orca, not MatrixKoder; the main checkout builds with AGY gemini-3.8-flash-high, a new worktree with OMP deepseek-v4.1-flash:1017 (Q1), reviews on Claude Code Opus; this session orchestrates — user
- 2026-10-24 — answer — Q2: hooks tests `claude plugin test .`, typecheck `tsc -p . --noEmit` (README.md:101-103) — orchestrator
- 2026-10-24 — amended — `oldnames` gate skips the required alias sites; DOCS.1 gains the install-command source and the stale gate lists (Opus review of GATEKEY) — orchestrator
- 2026-10-24 — answer — Q1: user said "go ahead" on the reading `ollama-cloud/deepseek-v4.1-flash:1017` — user
/+++
