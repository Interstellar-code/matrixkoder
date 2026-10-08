# MatrixKoder user guide

Task-oriented guide for end users. For a short overview see the [README](../README.md); for design detail see [plan.md](plan.md).

Contents: [Install](#install) · [First run and the wizard](#first-run-and-the-wizard) · [Reading the dashboard](#reading-the-dashboard) · [Running an orchestration](#running-an-orchestration) · [Usage tracking](#usage-tracking) · [Guards](#guards) · [Wake](#wake) · [Troubleshooting](#troubleshooting)

## Install

Requirements: Claude Code in the fullscreen layout (`/tui fullscreen`), `herdr`, `git`, `python3`, and the CodexBar CLI (`codexbar`). Optional: the Jev MCP, and builder CLIs `codex`, `opencode`, `agy`.

```bash
claude plugin marketplace add /path/to/matrixkoder
claude plugin install matrixkoder@matrixkoder --scope local    # or user / project
```

To try it without installing, start Claude Code with `claude --plugin-dir /path/to/matrixkoder`.

Change settings with `claude plugin configure matrixkoder`. The keys are `guards`, `autoOpen`, `chip`, `toasts`, `usagePollMin` and `wake` (defaults and meanings are in the README).

## First run and the wizard

Run `/mk setup` (alias `/mk init`). The wizard opens as a pane, one step at a time. Every step is pre-filled from detection; edit only what is wrong. **Nothing is written until the final confirm.** The wizard never asks for API keys.

| # | Step | What it asks |
|---|---|---|
| 1 | Install | Shows whether you run from a dev folder or a marketplace install. Info only. |
| 2 | Data | Where project data lives: `<repo>/.matrixkoder/` (default; added to `.gitignore`) or `~/.matrixkoder/projects/<slug>/`. |
| 3 | Agents | Which of Claude Code, Codex, OpenCode, AGY you use. Hotkeys 1 to 4 toggle them. |
| 4 | Usage | Which CodexBar providers to track. Hotkeys 1 to 9. `zai (OpenCode Z.AI plan)` and `opencode (OpenCode Zen)` are labelled so you pick the right one; zai is pre-selected when your opencode agent is on the Z.AI plan. Shows the z.ai key command (see [Usage tracking](#usage-tracking)). |
| 5 | Gates | Your test, typecheck, lint and format commands, as text inputs. Detection reads package.json, pyproject.toml, Makefile, `.claude-plugin/plugin.json`, `scripts/selftest.sh`, tsconfig.json, `Cargo.toml` and `go.mod`; when nothing is detected the first field takes the focus so you can just type. |
| 6 | Routing | Builder order, reserve floors, max lanes, effort by size, reviewers. |
| 7 | Autonomy | What the orchestrator may do without asking, and what pauses it. |
| 8 | QA | Whether there is a frontend, its start command, dev URL, and where QA runs. |
| 9 | Dashboard | Auto-open, status chip, toasts, poll intervals. |
| 10 | Review | Shows what will be written. `w` writes; `x` closes afterwards. |

Steps 5 to 9 have text inputs, so they have no single-key hotkeys.

On confirm the wizard writes:

- `matrixkoder.yaml` in the data directory. If one exists you choose keep (default) or replace; replace backs the old file up to `.bak`.
- `plans/` and `runs/` folders in the data directory.
- A `/.matrixkoder/` line in `.gitignore` when you chose the repo data location.
- `~/.matrixkoder/config.json` (merged) and `~/.matrixkoder/projects.json` (your project is added or updated).

If the pane cannot open (not fullscreen, or too narrow), the same questions are asked in chat.

## Reading the dashboard

Toggle it with `/mk` (or `/mk open`, `/mk close`). It docks on the right when the window is wide enough and Claude Code is in fullscreen (`/tui fullscreen`). Sections, top to bottom:

- **Run**: the active run, its stage and the next action.
- **Plan**: the waves, each as a row of status glyphs.
- **Lanes**: one line per task with its agent and status.
- **Usage**: remaining quota per agent pool, scoped to the agents your project's `matrixkoder.yaml` enables (every ledger agent when it names none). An enabled agent with no ledger entry yet shows a dim hint row instead of nothing. The claude main row is labelled `claude (all)` when it covers sub-pools. `—` means the pool has no such window; `n/a` means the provider could not be polled at all; `⊘` in red means the pool is exhausted. `no ledger yet` means usage has not been polled yet.
- **Waiting on you**: items that need your decision.
- **Recent**: the last few events.

Status glyphs: `○` planned, `◇` routed, `▷` dispatched, `●` building, `⧖` verifying, `◐` review, `▲` fix, `◈` merged, `◆` QA, `✓` done, `⊘` blocked, `⧗` quota, `⚠` stalled, `⊗` provider problem.

Agent glyphs: `✦ ∇ ⊚ ⌂`, one per agent in the pool.

Layout modes: below 40 columns the pane switches to compact wording (`building 12m` becomes `build 12m`). If the pane is shown inline instead of docked, it prints "Run /tui fullscreen to dock this panel on the right".

The status chip under the prompt shows run health in one line; toasts announce task status changes. Turn either off in settings.

## Running an orchestration

Say what you want in plain words, for example "orchestrate the plan in docs/plan.md". The `matrixkoder` skill takes over and runs the loop: plan, route each phase to an agent with quota left (Claude Code is spared), write briefs, dispatch to herdr panes and worktrees, watch, verify, review, fix, merge, QA, and open one PR. It stops only for the human decisions listed in `skills/matrixkoder/reference/safety.md`.

What lands on disk, under the data directory:

| Path | Content |
|---|---|
| `matrixkoder.yaml` | Your repo settings from the wizard |
| `runs/<id>/run.json`, `events.jsonl` | Run snapshot and append-only history |
| `runs/<id>/board.md` | Generated status board. Never hand-edit. |
| `runs/<id>/tasks/<T>/` | `task.json`, `brief-rN.md`, `report-rN.md`, `review-rN.md` per round |

All run state is written through `scripts/mk-state.py`; do not edit `run.json`, `events.jsonl` or `board.md` by hand. Read-only commands you can run yourself:

```bash
python3 scripts/mk-state.py run list
python3 scripts/mk-state.py run get --id <id>
```

Other subcommands exist (`run init|stage|set|archive`, `task add|set|round|get`, `event`, `wait set|clear`, `lock`, `render`); the skill uses them for you. The `mk_state` tool exposes a safe subset to the model.

## Usage tracking

`scripts/mk-usage.py` polls CodexBar and writes `~/.matrixkoder/usage.json`. The plugin runs it every `usagePollMin` minutes (default 5, minimum 1). Refresh by hand:

```bash
python3 scripts/mk-usage.py --once
```

Other options: `--watch --interval N`, `--out PATH`, `--providers a,b`, `--self-test`. When the project's `matrixkoder.yaml` lists `usage.providers`, the plugin polls exactly those.

Provider names map to CodexBar like this: claude to `claude`, codex to `codex`, antigravity to `agy`, zai to `opencode`, opencodego to `opencode-go`. Agent-style names work too — `opencode` in a provider list polls CodexBar `zai`. If a provider errors, its entry is `n/a` instead of failing the whole poll. Codex on an API key has no quota to read, so expect `n/a` there.

For z.ai, store the key in CodexBar yourself (never paste it into the chat):

```bash
printf '%s' "$Z_AI_API_KEY" | codexbar config set-api-key --provider zai --stdin
```

The orchestrator reads this ledger when routing: a pool near its limit is skipped, and `⊘` appears on the dashboard.

## Guards

With `guards` on, MatrixKoder checks every Bash tool call and denies:

- `git checkout`, `reset`, `clean`, `stash` (except `stash list` and `stash show`)
- `git restore` (except `--staged` alone)
- `git switch --force`, `commit --amend`
- force-push and remote ref deletion, `branch -D`
- `git rebase` (except `--abort`, `--continue`, `--skip`), `pull --rebase`
- `reflog expire|delete`, `update-ref -d`, `gc --prune`
- anything listed under `banned_commands` in `matrixkoder.yaml`

Everything else, including normal `add`, `commit`, `merge`, `push` and `stash list`, is allowed. Guards apply only to Bash calls.

Limitation: `banned_commands` is read from `<repo>/.matrixkoder/matrixkoder.yaml` only. If you chose the home data location in the wizard, you still get the git rules but not your `banned_commands`.

Turn guards off with `claude plugin configure matrixkoder` (`guards` = false).

## Wake

Wake lets MatrixKoder submit a prompt to your session when a lane needs attention, so a long run does not sit idle. It fires on `verifying`, `blocked`, `quota`, `stalled` and `provider` events. It checks every 30 seconds, waits 60 seconds to batch events, and allows one wake per task and status every 5 minutes. When you first turn it on, existing history is ignored.

It is **off by default** because it types into your session unprompted. Enable it with `wake` = true in `claude plugin configure matrixkoder`.

## Troubleshooting

- **Pane does not dock**: run `/tui fullscreen`. Auto-open needs a wide window (the design notes in `docs/spikes.md` say 144 columns). Otherwise the pane shows inline with the `/tui fullscreen` hint.
- **Pane too wide or narrow**: drag its edge to resize. Not checked in code; taken from the design notes.
- **Usage looks stale**: run `python3 scripts/mk-usage.py --once`, or lower `usagePollMin`. Check that `codexbar` is on your PATH.
- **A pool shows `n/a`**: the provider returned an error to CodexBar. Run `codexbar` for that provider directly to see why.
- **No pane at all**: `/mk open`, and check `autoOpen` in settings.
- **Plugin changes not showing**: restart the session, or start Claude Code with `--plugin-dir`.
- **A command is denied**: it matched a guard. Use a safe alternative, or turn `guards` off.
- **Dashboard says "Run /mk init <feature-name>"**: that hint is stale. Use `/mk setup` for the wizard and ask the model to "orchestrate …" to start a run.
