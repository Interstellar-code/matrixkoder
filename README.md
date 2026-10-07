# MatrixKoder

Claude Code plugin that orchestrates a multi-phase build across several coding agents (Claude Code, Codex, OpenCode, AGY) running in herdr panes and git worktrees. Skill-driven loop (plan, route, brief, dispatch, watch, verify, review, fix, merge, QA, PR) with durable run state, CodexBar usage tracking, and an in-session dashboard. Command: `/mk`. Successor of `stellar-orchestrator`; design in `docs/plan.md`.

## Install

- Dev: `claude --plugin-dir /path/to/matrixkoder`
- Later: `/plugin install matrixkoder --marketplace Interstellar-code/matrixkoder`

First `/mk` in a repo runs the onboarding wizard (`skills/matrixkoder/reference/onboarding.md`).

## Requirements

- `herdr` (panes and worktrees), `git`, `python3`
- CodexBar CLI (`codexbar`) for usage across agents
- Agent CLIs you want as builders: `codex`, `opencode`, `agy`
- Jev MCP (optional) for routing and gates

## Folder map

| Path | Content |
|---|---|
| `skills/matrixkoder/` | `SKILL.md` loop; `reference/` (loop, pool-and-usage, safety, onboarding, lessons); `templates/` |
| `scripts/` | `mk-state.py` (only writer of run state), `mk-usage.py`, `lane-health.py`, `lane-watch.py` |
| `agents/` | `effort-low\|medium\|high\|max` effort-pinned workers; as a plugin they are `matrixkoder:effort-*` |
| `hooks/`, `types/` | dashboard mod (added in a later phase) |
| `docs/` | plan, spikes, state schema, dashboard design |

Project data lives in `<repo>/.matrixkoder/` (or `~/.matrixkoder/projects/<slug>/`); global state in `~/.matrixkoder/` (`usage.json`, `config.json`, `projects.json`, `locks/`).
