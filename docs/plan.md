# MatrixKoder — plan (v2, 2026-10-07)

Rebuild of the `stellar-orchestrator` skill (`~/.claude/skills/stellar-orchestrator/`) as a Claude Code
plugin named **MatrixKoder**: skill + scripts + agents + a mod (right-side in-session dashboard, status
chip, toasts, guards, resume, onboarding wizard). The old name is retired.

> **Superseded storage decisions.** D5, D7 and §2c/§4 below are the 2026-10-07 design. The
> projects/ layout replaced them (the old layout ran until 2026-10-15): two SQLite stores only, the root
> `~/.matrixkoder/mk.db` (overview numbers and linkage) and `~/.matrixkoder/projects/<slug>/mk.db`
> (the ONLY source of truth). The repo keeps no MK data, and `run.json`/`events.jsonl`/`task.json`/
> `board.md` are no longer written or read. The authoritative layout is
> [docs/state-schema.md](state-schema.md) and the storage-layout plan; read every old-layout
> mention below as history.

## 1. Decisions made

| # | Decision | By |
|---|---|---|
| D1 | Name: MatrixKoder (skill + plugin). Slash command `/mk` | user |
| D2 | Dashboard lives inside the Claude Code session, docked right (mod Pane), not a herdr/terminal pane | user |
| D3 | Usage library: **CodexBar CLI** (one source for all agents) — chosen over OpenUsage, ClaudeBar, codeburn, agentsview | user + probe |
| D4 | Develop locally in hermes-switchui first; dedicated repo `Interstellar-code/matrixkoder` later | user |
| D5 | ~~Project data in repo root `.matrixkoder/`, gitignored~~ (superseded: project data lives in `~/.matrixkoder/projects/<slug>/`) | user |
| D6 | ~~Wizard offers both install mode (local dev folder / marketplace) and data location (repo / home)~~ (superseded: install mode only; data location is always the project folder) | user |
| D7 | Run state = the project DB (run/task rows, events); per-task folders hold per-round Markdown | user |
| D8 | Worktree agents read only their brief; brief points read-only at the task's history folder | user |
| D9 | Dashboard phase 1 read-only; buttons later | agreed |

## 2. Where things live

### 2a. During development (now)

```
~/Development/
├─ matrixkoder/                    ← plugin SOURCE, own git repo; later: gh repo create + push as-is
└─ hermes-switchui/                ← the repo keeps NO MK data (no .matrixkoder/)
```

Project data for hermes-switchui runs lives in `~/.matrixkoder/projects/hermes-switchui/`.

Load while developing: `claude --plugin-dir ~/Development/matrixkoder`
(or `CLAUDE_CODE_PLUGIN_DIRS`). Plugin-dir folders hot-reload in an interactive session.

### 2b. After publishing

`/plugin install matrixkoder --marketplace Interstellar-code/matrixkoder` — repo carries
`.claude-plugin/marketplace.json`. Wizard step 1 detects which mode is active.

### 2c. Global / per-project (the projects/ layout)

```
~/.matrixkoder/
├─ mk.db              root: overview numbers and linkage (projects, plan_summary)
├─ usage.json         normalised quota ledger, all agents (one per user, not per project)
├─ bus/               bus.json, bus-tokens.json, lanes/<sha256(lane)>.json, ring-queue.json
└─ projects/<slug>/
   ├─ mk.db           the project DB — the ONLY source of truth
   ├─ project.yaml    today's matrixkoder.yaml
   ├─ watch-cursors.json   orchestrator bus watch cursors
   └─ plans/<plan-id>/
      ├─ plan.md  assets/  archive/  (plan-v1.md, plan-v2.md …)
      └─ runs/<run-id>/
         ├─ bus.jsonl          archived bus messages, acks, bus artifacts (run end)
         └─ <task-id>/         brief-rN.md, report-rN.md, review-rN.md
```

`scripts/mkpaths.py` / `hooks/paths.ts` are the only path authority (C1); `scripts/mk-migrate.py`
is the one migration tool (dry-run, copy, switch, rollback, archive). The 2026-10-07 design's
`projects.json` repo→data_dir map is gone.

## 3. Plugin source structure

```
matrixkoder/
├─ .claude-plugin/
│  ├─ plugin.json                  name, version, description, "types", userConfig
│  └─ marketplace.json             one-plugin marketplace (for publishing)
├─ skills/matrixkoder/
│  ├─ SKILL.md                     the loop, stages 0–12 (from stellar SKILL.md)
│  ├─ reference/
│  │  ├─ loop.md                   stage details, context budget, QA
│  │  ├─ pool-and-usage.md         agents, tiers, effort, CodexBar ledger, Jev recipe, failover
│  │  ├─ safety.md                 autonomy contract, pause list, locks, git/dev-server bans
│  │  ├─ onboarding.md             wizard steps (chat fallback when not fullscreen)
│  │  └─ lessons.md                26 lessons carried over + new ones
│  └─ templates/
│     ├─ brief.md  review.md
│     └─ project.yaml               project settings template (was stellar.yaml, then matrixkoder.yaml)
├─ agents/                         effort-low|medium|high|max.md (moved from ~/.claude/agents)
├─ hooks/
│  ├─ hooks.json                   { "modules": ["./register.tsx"] }
│  ├─ register.tsx                 wires the modules below
│  ├─ dashboard.tsx                Pane: header, plan/waves, lanes, usage, waiting, recent
│  ├─ wizard.tsx                   onboarding steps in the pane
│  ├─ status.ts                    status-line chip + toasts on state transitions
│  ├─ resume.ts                    prompt.compose section: active run, next action, waiting
│  ├─ guards.ts                    tool.call deny list (git undo cmds, force-push, banned cmds)
│  ├─ tools.ts                     mk_state tool for the model
│  └─ *.test.ts                    claude plugin test
├─ types/index.d.ts                $.state contract (pane open, selected run, wizard step)
├─ scripts/
│  ├─ mk-state.py                  ONLY writer of run state (init/task/set/event/lock/archive/render; the board text is printed to stdout from the DB)
│  ├─ mkpaths.py                   the only path authority (project DB, plan/run/task folders, lane token, watch cursors)
│  ├─ mk-migrate.py                the one migration tool (dry-run, copy, switch, rollback, archive)
│  ├─ mk-usage.py                  codexbar → ~/.matrixkoder/usage.json (normalise, expire resets)
│  ├─ lane-health.py               OK|STALL|QUOTA|PROVIDER|BLOCKED|DEV_SERVER|ERROR (fixed)
│  ├─ lane-watch.py                watch → writes task state via mk-state (fixed)
│  └─ selftest.sh                  runs every script's --self-test
└─ README.md
```

Skill-in-plugin and agents-in-plugin follow the standard Claude Code plugin layout; the mod docs only cover
the hooks module, so P0 confirms all three load from one plugin.

## 4. Project data structure (`~/.matrixkoder/projects/<slug>/`)

```
projects/<slug>/
├─ mk.db                           the project DB — the ONLY source of truth; no run.json,
│                                  events.jsonl, task.json, board.md or run-doc artifact
├─ project.yaml                    gates, baselines, banned cmds, agents, routing, autonomy, QA
├─ watch-cursors.json              orchestrator bus watch cursors
└─ plans/<plan-id>/
   ├─ plan.md                      human plan (phases, waves, ownership matrix, contract)
   ├─ assets/                      mockups, design files
   ├─ archive/                     plan-v1.md, plan-v2.md …
   └─ runs/<run-id>/
      ├─ bus.jsonl                 archived bus messages, acks, bus artifacts (run end)
      └─ <task-id>/                brief-r1.md report-r1.md review-r1.md; brief-r2-fix.md report-r2.md review-r2.md
```

- Task status: `planned → routed → dispatched → building → verifying → review → fix → merged → qa → done`,
  side states `blocked | quota | stalled | provider`.
- New project = a root `projects` row + the project DB; new run = rows in the project DB. Archive = a
  status flag; nothing moves.
- Briefs copied into a worktree are copies; the canonical brief is the DB record.
- Plan changes in a RUNNING plan go in the DB (`task set`, `task add`), not the plan `.md`;
  `plan sync` needs `--from-file` on a live run.
- Event row: `{"t":"2026-10-07T18:31","task":"FIX6","from":"building","to":"stalled","by":"lane-watch","evidence":"no change 11m"}`, stored in the project DB.

The root `~/.matrixkoder/mk.db` holds only overview: `projects(slug, repo, data_dir, mode, db_path)`
(`db_path` set = migrated) and `plan_summary(project, plan, title, status, counts_json, live_run, updated)`,
PK (project, plan). `cards`, `project_summary` and `sync` stay until L7.3 drops them; cards live in the
root only as a read-only view until dropped.

## 5. Usage tracking (CodexBar) — verified 2026-10-07

Installed: CodexBar CLI v0.73.0 (sha256 checked) at `~/.local/share/codexbar/`, symlink
`~/.local/bin/codexbar`. Config: `~/.config/codexbar/config.json`. Enabled: codex, claude, antigravity.

Command: `codexbar usage --format json --provider <p>` → array of
`{ source, usage: { primary, secondary, extraRateWindows: [{ id, title, window }] }, error }`,
each window `{ usedPercent, windowMinutes, resetsAt (ISO UTC), resetDescription }`.

| Provider | Result | Notes |
|---|---|---|
| claude | 5h 10% · week 37% · Fable-only week 12% | source `claude` (OAuth) |
| antigravity | 5h (Gemini) 10% · week pool A 51.9% · week pool B **100%** (resets 2026-10-13) | source `cli`; **primary is weekly here** |
| codex | error `chatgpt authentication required to read rate limits` | API-key provider → show "n/a (API key)" |
| zai (OpenCode plan) | not probed | needs the z.ai API key → wizard step, user enters it (secret) |

Normaliser rules (`mk-usage.py`):
- Classify by `windowMinutes` (300 = 5h, 10080 = week), never by primary/secondary position.
- Keep every window with `id`/`title` (AGY has two weekly pools; Claude has a Fable-only week).
- Agent-level "available" = no window at 100% that blocks the model in use; a window past `resetsAt` counts as reset (gap 5).
- `error.kind == provider` → `status: n/a` with message, never guessed numbers.
- Poll every 5 min in the background (probes take several seconds each); mod reads only `usage.json`.
- Pane-text QUOTA detection stays as a fallback that writes the same ledger (gaps 2, 4 fixed there).
- Claude cross-check: mod `session.measure` → compare with CodexBar; flag drift, never overwrite.

## 6. Mod (in-session)

| Piece | Behaviour |
|---|---|
| Pane `/mk` | Toggle. Auto-opens at session start only when fullscreen layout + ≥144 cols (engine rule); else inline on command |
| Sections | Header (run, branch, stage, next action) · Plan (waves, task rows) · Lanes · Usage bars (5h / week / reset) · Waiting on you · Recent (last events) |
| Status chip | `✦ mk 1 building · 1 review · ⚠ 1 waiting · agy ⛔ wk 10-13` |
| Toasts | DONE, QUOTA, STALL, BLOCKED, PROVIDER, waiting-on-user |
| Resume | `prompt.compose` adds active run + next action + waiting item → survives compaction |
| Guards | `tool.call` Bash deny: `git checkout/stash/reset/clean/restore`, `--amend`, force-push, `banned_commands` from yaml |
| `mk_state` tool | model updates state without shell calls |
| Wake (spike) | lane DONE → `$.command.run({command:"matrixkoder:tick"})` queues a turn when idle; if it works, background `lane-watch` + `/loop` retire |

Mockup: see conversation 2026-10-07 (46-col right pane); becomes `matrixkoder/docs/mockup.txt` in P4.

## 7. Onboarding wizard (first `/mk` in a repo)

Runs as 46-column pane steps; every step pre-filled, one confirm at the end. There is no chat-question fallback: outside fullscreen it prints a read-only summary ending `Nothing was written.`, and a mobile surface gets a short notice to run it on desktop.

1. Install mode detected (dev `--plugin-dir` / marketplace) — info only
2. Data: default repo mode (`<repo>/.matrixkoder/`, added to `.gitignore`); home mode (`<globalDir>/projects/<slug>/`) is offered and is the default only for a bare repo. The data dir follows the root store's row for the repo (a migrated project writes `<project>/project.yaml`); the write creates the project store
3. Agents: detect claude / codex / opencode / agy / herdr + model lists; tick to enable an agent, and choose whether to link the herdr plugin
4. Usage: codexbar present? (show install cmd if not) → every enabled provider pre-selected; the z.ai key is printed as a `codexbar config set-api-key` command to run yourself, never entered here
5. Gates: detect `install` / test / typecheck / lint / format; proposed commands are whole-repo runs (no `{paths}`/`{files}` scope — nothing expands them); baselines written null and measured at first run
6. Routing: builder order, max lanes, the two Claude reserve floors, integration branch; `effort_by_size` and the reviewers are written as fixed defaults, never asked
7. Autonomy: pre-authorised list, builder permission-skip flags; `pause_only_for` is a fixed eight-item default, not asked
8. QA: frontend?, dev URL, start command (orchestrator only), themes, mockups dir, QA integration target; the port is derived from the dev URL, never asked
9. Dashboard prefs: auto-open, chip, toasts, poll intervals chosen here but written at the step-10 confirm with everything else — there is no Apply button
10. Review and confirm → whole file, `exactly what is written` (no truncation, no `pending`); a replace draws a real `-`/`+` diff with a changed/unchanged count; keep is the default and a kept file is never rewritten (`mk-config migrate` skipped for it); the write saves `<data>/matrixkoder.yaml` (or `project.yaml` when migrated), registers the project, creates its store, and puts the four prefs in the plugin's userConfig; Cancel/Close discard the answers

Bootstrap of the old skill (read-only checks) becomes steps 3–8's detection.

## 8. Known gaps from the stellar review → where fixed

| Gap | Fix | Phase |
|---|---|---|
| 1 Claude usage never recorded | CodexBar claude provider (+ mod cross-check) | P2 |
| 2 quota stored under pane id | ledger keyed by agent kind; lane-health `--kind` | P1/P2 |
| 3 idle-without-commit waits 30 min | STALL on idle + no matching commit + no change N min; match `<PHASE> BLOCKED:` | P1 |
| 4 quota regex misfires (scrollback, 429, weekly as 5h) | scan only text since dispatch; drop bare `rate limit`; window from codexbar | P2 |
| 5 exhausted quota never clears | `resetsAt` passed → available | P2 |
| 6 locks only on paper | `mk-state lock take/release/check` (locks in the project DB) | P1 |
| 7 DONE token never checked | lane-watch matches `<PHASE> DONE` in pane | P1 |
| 8 stray `__pycache__` | not carried over | P3 |

## 9. Phases

| # | Phase | Exit criteria |
|---|---|---|
| P0 | Spikes (done 2026-10-07, under the 2026-10-07 storage design): (a) pane docks right in fullscreen; (b) skill + agents + hooks load from one plugin via `--plugin-dir`; (c) `$.command.run` starts an orchestrator turn when idle; (d) mod `$.fs`/`$.process` can read the old data dir and run `codexbar`; (e) z.ai via codexbar with key | each answered yes/no in this file |
| P1 | State core: `mk-state.py` (schema, run/task/event/lock/render/archive), lane-health + lane-watch fixes | `--self-test` passes; dry run creates a run and 2 tasks (before the projects/ layout that meant `run.json`, `events.jsonl`, `board.md`; today the rows land in the project DB) |
| P2 | Usage: `mk-usage.py` normaliser + background poll | `usage.json` live for claude + agy; codex n/a; zai if key given |
| P3 | Scaffold `matrixkoder/` (nested git), port skill/refs/templates/agents with renames, `.gitignore` entries | `claude plugin validate` clean; skill triggers on "orchestrate" |
| P4 | Mod read-only: pane, chip, toasts, resume section | `claude plugin test`; visual check in fullscreen vs mockup |
| P5 | Wizard | fresh repo onboarding writes `project.yaml`, creates the project DB and the root `projects` row |
| P6 | Guards, `mk_state` tool, wake (if P0c yes) | tests: denied commands, state writes, wake turn |
| P7 | Pane buttons (approve waiting item, re-arm watcher, open review) | tests |
| P8 | Publish: `gh repo create Interstellar-code/matrixkoder`, push nested repo, marketplace install test | install works in a clean session — user go required |

Build lanes: P1 + P2 parallel (separate files); P3 after; P4–P7 sequential (same hooks module).
Lanes B, D, G (wave 1) share the main checkout with file ownership. **From P4 on every lane gets its own git worktree**
`~/Development/matrixkoder-wt-<x>` (branch `mk-<x>`), merged into `main` by the orchestrator after review (decided 2026-10-07).
Commits only on user go; nothing pushed before P8.

## 10. Open questions

- Q1: Fullscreen layout toggle — how the user switches Claude Code into it (affects pane docking).
- Q2: z.ai API key — user enters it into CodexBar config (secret; never via Jev or briefs).
- Q3: Codex — keep API-key provider (usage n/a) or sign in with ChatGPT for windows?
- Q4: Migrate the finished `wfv2-f678` board into the new format, or start fresh?
