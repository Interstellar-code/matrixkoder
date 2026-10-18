# MatrixKoder

MatrixKoder is a Claude Code plugin that runs a multi-phase build across several coding agents (Claude Code, Codex, OpenCode, AGY, omp). Each agent works in its own herdr pane and git worktree. You say "orchestrate …"; the `matrixkoder` skill plans the phases, routes each one to an agent that still has quota, writes briefs, dispatches, watches, verifies, reviews, fixes, merges, QAs and opens one PR. Run state is durable on disk, usage comes from CodexBar, and a dashboard pane docks beside your session.

```
┌ MatrixKoder ───────────────────────┐
│ Run   mk-build · building          │
│ Plan  W1 ✓✓  W2 ●▷○                │
│ Lanes ● P4 claude  ⧖ P6 codex      │
│ Usage claude 62%  codex 18%  ⊘ agy │
│ Waiting on you: 1 review           │
│ Recent  P6 → verifying             │
└────────────────────────────────────┘
```

(Illustrative; see [docs/howto.md](docs/howto.md#reading-the-dashboard) for the real sections.)

## Requirements

- Claude Code with the fullscreen layout: `/tui fullscreen`
- `herdr` (panes and worktrees), `git`, `python3`
- CodexBar CLI (`codexbar`) for usage across agents
- Optional: Jev MCP for routing and gates; builder CLIs `codex`, `opencode`, `agy`, `omp`

## Install

Local marketplace:

```bash
claude plugin marketplace add /path/to/matrixkoder
claude plugin install matrixkoder@matrixkoder --scope local   # or user / project
```

For development, load it straight from the folder: `claude --plugin-dir /path/to/matrixkoder`.

A GitHub marketplace install (`Interstellar-code/matrixkoder`, private for now) will come later.

## Quick start

1. `/mk setup` — the 10-step wizard. Every step is pre-filled from detection; nothing is written until the final confirm.
2. `/mk` — toggle the dashboard pane (`/mk open` and `/mk close` also work).
3. Ask for work: "orchestrate <your plan>".

Full walkthrough: [docs/howto.md](docs/howto.md).

## What is in the box

- **Skill** `matrixkoder`: the orchestration loop (`skills/matrixkoder/`).
- **`/mk` pane**: right-docked dashboard with run, plan waves, lanes, usage, what waits on you, and recent events.
- **Status chip**: run health under the prompt.
- **Toasts**: on task status changes.
- **Guards**: deny destructive git and project-banned commands in Bash calls.
- **Tools**: `mk_state` (read run state, limited writes) and `mk_usage` (refresh usage).
- **Wake**: optionally submits a prompt when a lane needs attention. Off by default.
- **herdr plugin**: event-driven lane verdicts from pane status changes, with a dashboard sweep backstop. See `docs/herdr-events.md`.
- **Root bus** (`scripts/mk-bus.py`): one bus per MK home for builder `connect`/`read`/`todo`/`commit`/`report`; reports and reviews travel over the bus, and a lane `.md` file is written only when the bus is down.
- **Kanban tab** (`hooks/tabs/kanban.tsx`, hotkey `k`): per-tick `mkdb.py overview --json` + `run-snapshot --json`. **Waiting tab** (`hooks/tabs/waiting.tsx`): real pending asks.
- **Files tab** (`hooks/tabs/filetree.tsx`, hotkey `f`, after Usage): a plain folder tree of the main checkout (`repo` chip) or of the worktree of a lane that is working on a task, with no git information. An amber dot marks a file being written and a teal ring a file being read, fading after about 3 s. Keys `j k h l`, search on `s`. Reads come only from this session's `Read` tool; writes from its `Edit`/`Write`/`NotebookEdit` and from any change on disk under the shown root. See [docs/howto.md](docs/howto.md#reading-the-dashboard) for what it skips. Inspired by [claude-code-filetree](https://github.com/data-goblin/claude-code-filetree) by data-goblin (MIT); the ideas are studied, none of its code is copied.
- **Catalog** (`scripts/mk-catalog.py`): `routes --json`, `model --pane <pane>` for routing.
- **Wizard**: `/mk setup`.
- **Agents**: `matrixkoder:effort-low|medium|high|max`, effort-pinned workers.

## Settings

Six options, set with `claude plugin configure matrixkoder`:

| Key | Default | Meaning |
|---|---|---|
| `guards` | on | Deny destructive git and project-banned commands |
| `autoOpen` | on | Open the pane when a session starts |
| `chip` | on | Pin the run health chip under the prompt |
| `toasts` | on | Toast on task status transitions |
| `usagePollMin` | 5 | Minutes between `mk-usage.py` runs (minimum 1) |
| `wake` | off | Submit a prompt when lanes hit verifying, blocked, quota, stalled or provider |

Each setting key declares a `level` (`global` or `project`) and a `home` (`userConfig` or `yaml`); the existing `scope` field is the Settings tab's UI category (`project|routing|agents|child`) and is unchanged. `userConfig` keys are level `global`. Each effective `yaml` value resolves in layers: the project DB `settings` row, then the global `~/.matrixkoder/mk-settings.yaml`, then the legacy `~/.matrixkoder/projects/<slug>/project.yaml`, then the default. `scripts/mk-config.py` has `set`, `set --global`, `unset`, `--project SLUG` and `migrate-settings` (moves `project.yaml` and `config.json` values into the layers); the Settings tab has Project and Global sub-tabs. `agents.*`, `picks.*` and `helpers.*` stay in `project.yaml`, which `/mk setup` still writes.

## Folder map

| Path | Content |
|---|---|
| `skills/matrixkoder/` | `SKILL.md` loop; `reference/` (loop, pool-and-usage, safety, onboarding, lessons); `templates/` |
| `scripts/` | `mk-state.py` (only writer of run state), `mkdb.py` (SQLite stores: `overview`, `plans`, `run-snapshot`, `events`, `timeline`, `projects`, `root-cleanup`), `mk-bus.py` (root bus), `mk-catalog.py` (`routes --json`, `model --pane`), `mk-plan.py` (parse/check/edit/approve TOML plan files; approve validates and imports the version into the project store and archives it to `projects/<slug>/plans/<plan>/archive/plan-v<n>.md`), `mkpaths.py` (the only path authority), `mk-migrate.py` (the one migration tool: dry-run, copy, switch, rollback, archive), `mk-lane.py` (reusable lane worktree slots, kept in the project DB `lane_slots` table; `carry-legacy` carries an older `lanes.json` once), `mk-release.py` (changelog + version bump + tag), `mk-usage.py`, `lane-health.py`, `lane-watch.py`, `selftest.sh` |
| `agents/` | effort-pinned workers |
| `hooks/` | dashboard, wizard, guards, status chip, wake and tools (TypeScript); tabs `kanban.tsx` (hotkey `k`) and `waiting.tsx` |
| `types/` | plugin API types |
| `docs/` | plan, spikes, state schema, dashboard design, user guide |

Two SQLite stores and nothing else: the root `~/.matrixkoder/mk-overview.db` (schema 3: `projects` and `plan_summary` only) holds overview numbers and linkage only, and `~/.matrixkoder/projects/<slug>/mk-project.db` (schema 7, with `settings` and `watch_cursors`) is the project's ONLY source of truth. The repo keeps no MK data. Global state lives in `~/.matrixkoder/` (`mk-overview.db`, `usage.json`, `mk-settings.yaml`, `inbox/<plan-id>/` for a plan and its docs that `mk-plan.py import` moves into a project, `bus/`, `projects/<slug>/`); bus files are `bus/bus.json`, `bus/bus-tokens.json`, `bus/lanes/<sha256(lane)>.json` and `bus/ring-queue.json`, and the orchestrator's bus watch cursors are `watch_cursors` rows in the project DB. `scripts/mkpaths.py` / `hooks/paths.ts` are the only path authority. `scripts/mk-migrate.py` is the one migration tool (dry-run, copy, switch, rollback, archive). Those are the names a new home gets; a home that already has `mk.db` files keeps using them (the new name applies only when that file exists, and is never created beside an old one), and store-creating code names its kind (`root_db_in` / `project_db_in`). The file-name constants can be overridden with `MK_NAME_*` environment variables (rename canary only). Before the projects/ layout (until 2026-10-15) project data lived in `<repo>/.matrixkoder/` and a `~/.matrixkoder/projects.json` repo→data_dir map; the repo folder is gone and `projects.json` is no longer written (read only as a last-resort fallback). `mkdb.py root-cleanup` (dry run by default; `--apply --backup-dir D`; `restore --from`) backs up and removes root DB leftovers. `scripts/skill-check.py` does not exist.

## Development

Gates, all of which must stay green:

```bash
claude plugin validate .
claude plugin test .
bash scripts/selftest.sh
tsc -p . --noEmit
```

Standing checks: `selftest.sh` includes the C1 gate (`python3 scripts/c1-gate.py check --baseline 0`: no MK file name spelled outside `mkpaths.py` / `hooks/paths.ts`). `python3 scripts/c1-gate.py canary` runs the rename canary: the `mkpaths.py` self-test and a full `mk-e2e-layout.py` cycle with every `MK_NAME_*` override set. `python3 scripts/mk-e2e-layout.py audit` is a read-only audit of the real home's layout and stores (each store is read from a private copy; exit 0 clean, 1 drift); the plan template ships it as an example `[[final_gate]]` that reruns after every merge.

Load the working tree with `claude --plugin-dir .`. Design and build history: `docs/plan.md`.

## License

TBD. No license file yet.
