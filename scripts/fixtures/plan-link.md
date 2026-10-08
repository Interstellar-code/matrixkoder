+++
plan = "plan-link"
status = "approved"
created = 2026-10-08
runs = []
design = [".matrixkoder/plans/plan-template-research.md"]
integration = "mk/plan-link/integration"

[gates]
test = "claude plugin test ."
typecheck = "npx tsc -p . --noEmit"
selftest = "bash scripts/selftest.sh"
validate = "claude plugin validate ."

[[phase]]
id = "P1"
title = "mk-plan.py: parse, check and edit TOML plans"
wave = 1
deps = []
owned = ["scripts/mk-plan.py"]
route = ""
size = "standard"
risk = "reversible"
review = "data-loss"          # it rewrites plan files: the markdown body must never be lost
qa = []
commit = "feat(plan): mk-plan.py — parse, check, set status, add run"
subagents = false
lane_group = "A"             # P1–P3 share one worktree; the lead gives each phase to one subagent

  [[phase.task]]
  id = "P1.1"
  title = "parse: TOML header + body split, JSON out per C2"
  acceptance = "the template example and this plan file parse to the C2 JSON; a file without +++ or with bad TOML gives ok:false exit 2 with a line number"

  [[phase.task]]
  id = "P1.2"
  title = "check: ids, deps, waves, overlaps, field values"
  acceptance = "duplicate ids, unknown dep, dep cycle, dep in a later wave, bad route/risk/review/size values are errors; owned overlap inside one wave and parallel subagent tasks sharing a file are errors; a task with subagent=true and no owned is an error; phases sharing a lane_group must be in the same wave and own disjoint files"

  [[phase.task]]
  id = "P1.3"
  title = "set --status / add-run: rewrite only the header keys"
  acceptance = "after set or add-run the markdown body is byte-identical and every other header line (incl. comments) is unchanged; write is atomic (temp + os.replace)"

[[phase]]
id = "P2"
title = "mk-state.py: run init from a plan, plan sync, link fields, board line"
wave = 1
deps = []
owned = ["scripts/mk-state.py", "docs/state-schema.md"]
route = ""
size = "complex"
risk = "reversible"
review = "standard"
qa = []
commit = "feat(state): runs built from plans — run init --plan, plan sync, plan links"
subagents = false
lane_group = "A"             # P1–P3 share one worktree; the lead gives each phase to one subagent

  [[phase.task]]
  id = "P2.1"
  title = "run init --plan creates phase tasks and child tasks"
  acceptance = "an approved plan with 2 phases and 3 tasks gives 2 tasks with plan_phase and 3 child tasks (--parent) with plan_task; an unapproved plan is refused unless --force, which writes an event"

  [[phase.task]]
  id = "P2.2"
  title = "plan sync (dry run, --apply)"
  acceptance = "new phase/task → added; changed title/wave/owned/deps on a planned|routed task → updated; on a started task → warning; removed → warning, never deleted; --apply stores the new plan_sha and one event"

  [[phase.task]]
  id = "P2.3"
  title = "link fields, index, board line, done roll-up"
  acceptance = "run.json plan_id/plan_sha, task plan_phase/plan_task/route/lane_group, index.json plan; board shows plan id/status/sha and the ⚠ line when the file changed; last run done → plan status done"

[[phase]]
id = "P3"
title = "Plan template and skill docs"
wave = 1
deps = []
owned = ["skills/matrixkoder/templates/plan.md", "skills/matrixkoder/templates/brief.md", "skills/matrixkoder/SKILL.md", "skills/matrixkoder/reference/loop.md", "README.md"]
route = ""
size = "standard"
risk = "reversible"
review = "light"
qa = []
commit = "docs(plan): TOML plan template, plan-driven stages 0-2, brief header"
subagents = false
lane_group = "A"             # P1–P3 share one worktree; the lead gives each phase to one subagent

  [[phase.task]]
  id = "P3.1"
  title = "templates/plan.md — the C1 skeleton with every field commented"
  acceptance = "parses with tomllib; matches C1; body sections Goal, Design / context, Phase notes, Risks, Open questions, Log"

  [[phase.task]]
  id = "P3.2"
  title = "SKILL.md stages 0–2 + Files table + scripts list"
  acceptance = "plan from templates/plan.md → mk-plan.py check → user approves → mk-plan.py set --status approved → run init --plan; pinned route skips jev_decide; size/risk go into jev_classify evidence; review sets reviewer depth"

  [[phase.task]]
  id = "P3.4"
  title = "SKILL.md: lane layout decision (separate lanes vs one lane + subagents)"
  acceptance = "stage 1 sets lane_group per phase; stage 2 makes the call per wave with one jev_decide when a wave has 2+ phases with disjoint files (candidates: separate lanes / one shared lane with one subagent per phase; evidence: owned files, risk, review, size, need for separate fix/merge, quota); a user-named layout overrides Jev; rules listed: share only disjoint files + same wave; keep separate for data-loss/destructive phases needing their own fix rounds, different agents/models, or test suites that would see each other's half-done work"

  [[phase.task]]
  id = "P3.3"
  title = "brief header + subagent rule, loop.md plan sync, README"
  acceptance = "brief names plan id, phase and its task ids; 'work yourself' unless phase subagents=true, then subagents use the builder's default model; loop.md says when to run plan sync"
+++

# plan-link — plans, runs and tasks linked, plans in TOML

User request 2026-10-08: "fix the plan–task link first; there should also be linkage of plan, tasks and the file structures"; "parallelise as much as possible". Template decisions (TOML, phase/task rule, all fields, subagent flags): `plan-template-research.md`.

## Goal

A plan file drives the run: `run init` builds tasks from it, every task points back to its phase or task, the run and the plan point to each other, and the board warns when the plan changed after the run started. Plans are written from one template, so later stages (briefs, routing, review, QA, dashboard) can read them.

## Design / context

What is wrong today: `run init --plan` only stores a path; tasks are copied by hand; tasks have no link to their phase; plans have no status or run list; 6 plans use 4 different shapes.

Linkage:
```
plans/<plan>.md       TOML header: plan, status, runs; [[phase]] → [[phase.task]]
   │ mk-plan.py parse → JSON (C2)
   ▼ mk-state.py run init --plan
runs/index.json       + plan
runs/<run>/run.json   + plan_id, plan_sha
tasks/<P>/task.json   + plan_phase, route            (phase = parent task)
tasks/<P.n>/task.json + plan_task, parent = <P>      (task = child task, status rolls up)
board.md              plan line + ⚠ when the plan changed since run start
```
`plans/` and `runs/` stay side by side, linked by ids both ways (user-approved).

Parallelism: P1, P2, P3 run at the same time in wave 1, in ONE worktree (lane_group A): one OpenCode session (model chosen by the user) leads and gives each phase to one subagent. Safe because they own disjoint files. They meet only through the contract C1–C5 below. While building, each phase runs only its own checks (P1 `mk-plan.py --self-test`, P2 `mk-state.py --self-test` with the fixture `MK_PLAN_CMD`, P3 `claude plugin validate .`); the lead commits each phase separately (`git add` of its own files only), then runs the full gates once.

### Contract

**C1 — plan file.** Starts with a line `+++`; TOML until the next `+++` line; the rest is free markdown, never parsed. Keys:
- top: `plan` (id `[a-z0-9][a-z0-9-]{0,40}`), `status` ∈ draft|approved|running|done|parked, `created` (date), `runs` (array), optional `design` (array), `integration` (string), `[gates]` (table of strings).
- `[[phase]]`: `id` `[A-Z][A-Z0-9]{0,7}`, `title`, `wave` int ≥1, `deps` (phase ids or `<run>:<task>`), `owned` (paths/globs), optional `forbidden`, `route` ("" = Jev, else `"<agent> <model> [<effort>]"`), `size` ∈ trivial|standard|complex, `risk` ∈ read_only|reversible|destructive, `review` ∈ light|standard|data-loss, `qa` (array), `commit`, `subagents` (bool, default false), `lane_group` (string; phases with the same value share one worktree and one lead session, each phase run by one subagent of the lead; empty = own lane).
- `[[phase.task]]`: `id` `<phase id>.<n>`, `title`, `acceptance`, optional `refs` (array of `path:line`), `subagent` (bool), `owned` (required when `subagent = true`; subset of the phase's owned).
- Unknown keys are allowed and passed through. A plan with `status` done or parked may have no phases.

**C2 — `scripts/mk-plan.py`** (stdlib + `tomllib`, one JSON line out):
- `parse <path>` → `{"ok":true,"plan":…,"status":…,"runs":[…],"sha":"<sha256 of file bytes>","gates":{…},"phases":[{"id":"P1","title":…,"wave":1,"deps":[…],"owned":[…],"forbidden":[…],"route":"","size":…,"risk":…,"review":…,"qa":[…],"commit":…,"subagents":false,"tasks":[{"id":"P1.1","title":…,"acceptance":…,"refs":[…],"subagent":false,"owned":[…]}]}]}`; error → `{"ok":false,"errors":["line N: …"]}` exit 2; missing file exit 3.
- `check <path>` → parse + the P1.2 rules; adds `"warnings":[…]`.
- `set <path> --status <s>`, `add-run <path> --run <id>` → rewrite only that header key's line(s).

**C3 —** `mk-state.py` calls `mk-plan.py` (next to itself) only via C2; env `MK_PLAN_CMD` overrides the command.

**C4 — stored fields:** `run.json` `plan_id`, `plan_sha`; phase task `plan_phase`, `route`, `lane_group`; child task `plan_task`, `parent`; `runs/index.json` entry `plan`. All null for runs/tasks without a plan.

**C5 — `mk-state.py`:** `run init --plan` → `check`; refuse unless `status = approved` (`--force` + event); create phase tasks then child tasks through the existing task-add path; store links; `mk-plan.py set --status running` + `add-run`. `plan sync [--run] [--apply]` per P2.2. Last run done → plan `done`. Board line per P2.3. `task add` stays for ad-hoc tasks.

## Phase notes

### P1 — mk-plan.py
1. P1.1 parse. 2. P1.2 check. 3. P1.3 header edits that never touch the body. `--self-test` covers all three, using temp files only.

### P2 — mk-state.py
1. P2.1 run init from a plan. 2. P2.2 plan sync. 3. P2.3 link fields, index, board, roll-up. Self-test uses a fixture `MK_PLAN_CMD` script printing C2 JSON. `docs/state-schema.md` documents C4.

### P3 — docs
1. P3.1 `templates/plan.md`. 2. P3.2 SKILL.md stages 0–2. 3. P3.3 brief header + subagent rule, loop.md, README.

### Wave 2 — join (orchestrator)
Review the 3 commits once (P1's header rewrite as data-loss); merge the lane; end-to-end smoke in a temp data dir with this file: check → set approved → run init → 3 phase tasks + 9 child tasks → edit a title → ⚠ on board → plan sync --apply → run done → plan done. Full gates, merge into local main.

### Alongside wave 1 (orchestrator, inline)
Convert the 6 existing plans to a C1 header (`docs-deps`, `herdr-push`, `lanes`, `usability-r1` → done with their run ids, no phases needed; `dashboard-v2` → draft, phases from its table; `parked` → parked). Check with `mk-plan.py check` once P1 lands.

## Risks
- `mk-plan.py set` corrupting a plan body → `review = "data-loss"` on P1, byte-identical test.
- Contract drift between P1 and P2 → contract fixed here; wave-2 smoke runs the real pair together.
- First live use of `mk-lane.py acquire` (1 lane) (herdr path never exercised) → fall back to `herdr worktree create` and record it.

## Open questions
None left (TOML, phase/task rule, all fields, subagent flags, side-by-side folders, refuse unapproved plans — all decided 2026-10-08).

## Log
- 2026-10-08 — drafted; decisions above taken by the user in chat.
- 2026-10-08 — user: one worktree, one OpenCode session leading 3 subagents (one per phase) instead of 3 worktrees; user picks the model; added lane_group (C1, C4) and P3.4 (skill rule for this call, Jev may decide it at stage 2).
- 2026-10-08 — approved by the user ("yes go ahead"); builder model chosen by the user.
