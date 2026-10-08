# MatrixKoder

MatrixKoder is a Claude Code plugin that runs a multi-phase build across several coding agents (Claude Code, Codex, OpenCode, AGY). Each agent works in its own herdr pane and git worktree. You say "orchestrate …"; the `matrixkoder` skill plans the phases, routes each one to an agent that still has quota, writes briefs, dispatches, watches, verifies, reviews, fixes, merges, QAs and opens one PR. Run state is durable on disk, usage comes from CodexBar, and a dashboard pane docks beside your session.

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
- Optional: Jev MCP for routing and gates; builder CLIs `codex`, `opencode`, `agy`

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

## Folder map

| Path | Content |
|---|---|
| `skills/matrixkoder/` | `SKILL.md` loop; `reference/` (loop, pool-and-usage, safety, onboarding, lessons); `templates/` |
| `scripts/` | `mk-state.py` (only writer of run state), `mk-usage.py`, `lane-health.py`, `lane-watch.py`, `selftest.sh` |
| `agents/` | effort-pinned workers |
| `hooks/` | dashboard, wizard, guards, status chip, wake and tools (TypeScript) |
| `types/` | plugin API types |
| `docs/` | plan, spikes, state schema, dashboard design, user guide |

Project data lives in `<repo>/.matrixkoder/` (or `~/.matrixkoder/projects/<slug>/`). Global state lives in `~/.matrixkoder/` (`usage.json`, `config.json`, `projects.json`, `locks/`).

## Development

Gates, all of which must stay green:

```bash
claude plugin validate .
claude plugin test .
bash scripts/selftest.sh
tsc -p . --noEmit
```

Load the working tree with `claude --plugin-dir .`. Design and build history: `docs/plan.md`.

## License

TBD. No license file yet.
