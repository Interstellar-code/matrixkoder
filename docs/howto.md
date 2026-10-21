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

Run `/mk setup` (alias `/mk init`). The wizard opens as a pane at 46 columns, one step at a time. Every step is pre-filled from detection; edit only what is wrong. **Nothing is written until the final confirm** — the dashboard preferences included. The wizard never asks for API keys. `/mk init <name>` makes `<name>` the project slug (lowercased, with `_` and `.` folded to `-`); `/mk setup` takes no name.

| # | Step | What it asks |
|---|---|---|
| 1 | Install | Shows whether you run from a dev folder or a marketplace install. Info only. |
| 2 | Data | Where project data lives. The default is `<repo>/.matrixkoder/`, and the step offers `~/.matrixkoder/projects/<slug>/` instead; `$MATRIXKODER_DATA`, when set, decides it and the step says so. In repo mode it asks whether to add `/.matrixkoder/` to the repo `.gitignore` (a file that already ignores it is only confirmed). A bare repo with no build file starts in home mode; the project DB is created on confirm, not here. |
| 3 | Agents | Which builders you use, one row per agent on PATH with its state chip, path and model count; hotkeys 1 to 4 toggle Codex, OpenCode, AGY and the herdr agent (Claude Code is always on). Codex and AGY models come from `agy models` and `opencode models` (plus the Z.AI and Ollama Cloud plan lists). If `herdr` is found, choose whether to link the MatrixKoder herdr plugin so pane events push lane verdicts instead of polling. |
| 4 | Usage | Which CodexBar providers to track. Hotkeys 1 to 9. The short names (`Claude`, `OpenCode Zen`, `Z.AI`, `Ollama`, …) tell the similar ones apart; every provider CodexBar has enabled is pre-selected, and zai joins when your opencode agent is on the Z.AI plan (ollama when Ollama Cloud models are found). Shows the z.ai key command (see [Usage tracking](#usage-tracking)). |
| 5 | Gates | Five commands, as text inputs: install, test, typecheck, lint_changed, format_changed. They are whole-repo runs — detection proposes `pnpm exec vitest run`, `pnpm exec eslint .`, `ruff check .`, never a `{paths}` token, because nothing expands one. Detection reads package.json, pyproject.toml, Makefile, `.claude-plugin/plugin.json`, `scripts/selftest.sh`, tsconfig.json, `Cargo.toml` and `go.mod`; when nothing is detected the first field takes the focus so you can just type, and an empty command is named as `""` rather than refused. |
| 6 | Routing | Builder order, max build lanes (1 to 5), the two Claude reserve floors (5h left % and weekly left %), and the integration branch. A builder name that is not an enabled agent, and a branch that is empty or holds spaces, are each named in one dim line and still written as typed. |
| 7 | Autonomy | Whether builders skip permission prompts (they run in their own worktree only), and what is pre-authorised. |
| 8 | QA | Whether there is a frontend, the dev URL, the start command (orchestrator only), the themes and the mockups dir, and where QA runs: `local-main` = QA on the running dev server. |
| 9 | Dashboard | Auto-open, status chip, toasts, and the usage poll in minutes. These are plugin settings, and they are saved with everything else when you confirm: there is no Apply button here. A poll that is not a whole number of minutes ≥ 1 is named as written 5. |
| 10 | Review | Shows exactly what will be written: the whole file, scrollable, with the timestamp the write will stamp. If a settings file already exists you choose keep (the default, which shows that file untouched) or replace, which shows a real `-`/`+` diff against it and a `N lines changed · M unchanged` count. `w` writes; `x` closes afterwards. |

Steps 5 to 9 have text inputs, so they have no single-key hotkeys: Tab moves between the fields and `↵` goes on to the next step.

On confirm the wizard writes:

- The settings file under the data dir: `<repo>/.matrixkoder/matrixkoder.yaml` in repo mode, `~/.matrixkoder/projects/<slug>/matrixkoder.yaml` in home mode, and `project.yaml` in the same place once the project is migrated — your repo settings, the legacy layer under the project DB and `~/.matrixkoder/mk-settings.yaml`. If one exists you choose keep (default) or replace; replace backs the old file up to `.bak`, writes a temp file and moves it into place, and a kept file is left byte-identical.
- `/.matrixkoder/` in the repo `.gitignore`, when you chose repo mode and said yes.
- `<data>/mk-project.db` (existing projects keep `mk.db`) — the project DB, the ONLY source of truth. It is created at the write by `mk-state.py sync`, the script that owns it; once the project is migrated that folder is `~/.matrixkoder/projects/<slug>/`.
- `~/.matrixkoder/mk-overview.db` (an existing home keeps `mk.db`) — the root DB, with this project's `slug, repo, data_dir` row. `db_path` is set only by `mk-migrate.py --switch`, and it is what marks a project migrated.
- `<data>/plans/` and `<data>/runs/`, made at once; plan and run folders fill in as runs are created. In home mode the repo keeps no MK data; in repo mode `/.matrixkoder/` is the gitignore entry above.

Dashboard preferences (auto-open, chip, toasts, poll interval) are plugin settings — `claude plugin configure matrixkoder` — not files the wizard writes; they are set at the same confirm as everything else.

When the write made the file (a create or a replace), the wizard then runs `mk-config.py migrate` on the data dir: it adds the `session:`, `picks:`, `lanes.default_agent` and reviewer keys an old file lacks and drops the dead yaml copies of plugin settings. A kept file is not migrated — keep means keep. A failure warns on the post-write line and never fails the wizard.

If the pane cannot open (not fullscreen), `/mk setup` answers with a read-only summary of what detection found, ending `Nothing was written.` There are no chat questions and no width check: a docked or inline pane draws the steps, and the mobile app (which draws no text fields) gets a notice saying to run it on desktop.

## Reading the dashboard

Toggle it with `/mk` (or `/mk open`, `/mk close`). It docks on the right when the window is wide enough and Claude Code is in fullscreen (`/tui fullscreen`). Sections, top to bottom:

- **Run**: the active run, its stage and the next action.
- **Plan**: the waves, each as a row of status glyphs. On a running plan, an expanded phase and every task row carry Play (`▶`) and Pause (`‖`); a press drafts that item's prompt (its title, description, acceptance, deps, files and route) on the Improve card, where the Play draft is refined by the model and the Pause draft is shown as written, and Play and Pause only ask the orchestrator to start or pause — Pause stops no running builder, and nothing is sent until you accept the draft and send it. While a plan is still a draft, its phase and task rows carry Improve (`✦`) instead: the press drafts a prompt from the row's own fields that names `mk-plan.py item set <plan> <id>` as the way to write the change, so accepting and sending it edits the stored row, not a file. An approved, running or done plan draws no Improve button.
- **Lanes**: one line per task with its agent and status.
- **Usage**: remaining quota per agent pool, scoped to the agents your project's `project.yaml` enables (every ledger agent when it names none). An enabled agent with no ledger entry yet shows a dim hint row instead of nothing. The claude main row is labelled `claude (all)` when it covers sub-pools. `—` means the pool has no such window; `n/a` means the provider could not be polled at all; `⊘` in red means the pool is exhausted. `no ledger yet` means usage has not been polled yet.
- **Waiting on you**: items that need your decision (full tab: hotkey `w`, `hooks/tabs/waiting.tsx` — real pending asks).
- **Recent**: the last few events.
- **Files** (hotkey `f`, right after Usage; `hooks/tabs/filetree.tsx`): a plain folder tree of the main checkout (chip `main`) or of a lane's worktree (one chip per lane slot, named after its task, or `slot N` and dim when the slot is free). A Git column marks files `git status` reports (`M` changed, `A` added, `D` deleted, `R` renamed, `?` untracked) and a `•` on a folder that holds one. A header row names the columns Name, Git, Size and Modified (the last two only at 56 columns or wider); pressing a header sorts by it and pressing it again flips the order, folders always stay first. A coloured glyph in front of each name tells a folder, a link or a file kind by its extension. Folders such as `node_modules`, `.matrixkoder`, `.omc` and `.serena` are listed but never opened or scanned, so opening one shows `(not scanned)` and a `⟳ scan` button that lists that one folder level (and keeps rescanning it) on demand. A small icon marks a file that was just touched and fades after about 3 seconds: an amber dot `●` for a write (it pulses), a teal ring `◉` for a read. A write beats a read on the same file, and a folded folder shows the strongest icon inside it, with a count when more than one file inside is lit. Keys: `j` and `k` move the selection, `l` opens a folder, `h` folds an open folder, or moves to the parent folder from a file or a folded folder, `s` puts the cursor in the search box, `Enter` there keeps the filter; `Expand all` and `Collapse all` are buttons. The tab only scans: it lists the root again on the code-rain timer while it is on screen.
  - **What lights up.** Reads: only this session's own `Read` tool calls (not `cat` or `rg` in Bash, not Grep or Glob, not lane agents). Writes: this session's `Edit`, `Write` and `NotebookEdit` calls, and any file that appears or changes on disk under the shown root, whoever wrote it (a formatter or another agent counts). Lane reads are not shown.
  - **What it does not scan.** Every git-ignored folder (found with `git ls-files --others --ignored --exclude-standard --directory`) is also left unscanned. These folders are listed but never opened: `.omc`, `.matrixkoder`, `node_modules`, `dist`, `build`, `target`, `.venv`, `venv`, `__pycache__`, `.next`, `.turbo`, `.cache`, `coverage`, `.ruff_cache`, `.serena`, `.omp`, `.mypy_cache`, `.pytest_cache`, and any folder starting with `.matrixkoder-`. `.git` is not shown at all. A folder opened there reads `(not scanned)` with a `⟳ scan` button, and so does any folder the walk did not reach before its limit; scanning lists one level of it and then keeps it up to date. A walk stops at 2000 entries or 300 folder listings, a folder shows at most 500 of its entries (then an "N more" row), and the tree shows at most 600 rows; a dim line says when it stopped early.
- **Inbox** (hotkey `n`, `hooks/tabs/inbox.tsx`): the plan documents waiting in `<home>/inbox/` to become projects, one flat `<topic>.md` per plan (the older `<plan-id>/plan.md` folder form still lists and imports). Each card carries the file name, the plan's title, how well it follows the template (`✓ TEMPLATE`, `◐ PARTIAL` with the sections it is missing, `✕ NOT A PLAN`) and how old it is, plus `↗ Open`, `⇩ Import`, `Archive`, `Restore` and `Delete`. `Import` runs `scripts/mk-inbox.py` for the project the pane is open in: the card shows the three steps (parse the template, write the project, write the tasks), then `✓ Imported as <plan>` with the phase and task counts and `▤ Open project`, which opens the Plan tab on it. A refused import draws its step and reason on the card with `↻ Retry` and `Dismiss`, never just a toast. `Delete` asks on the card, and the script copies the file to `inbox/.deleted/` before it goes. Every step (found, checked, import-step, imported, import-failed, archived, restored, deleted) is a project event with source `inbox` on the Events tab, and an inbox row is shown there even when no run is active; no inbox event wakes the orchestrator. The list is read once when the tab opens and then with every dashboard refresh, only while the tab is open.
- **Todo** (hotkey `t`, `hooks/tabs/todo.tsx`): a personal to-do list. Every add, tick, delete and clear-done is saved at once in the overview database, per project, so the list survives restarts and is shared across all plans and runs. Keys: `a` focuses the add field, `z` undoes the last delete.
- **Improve** (hotkey `i`, `hooks/tabs/improve.tsx`): works on Claude Code's own prompt line, from a `✦ Improve` chip in the footer on every tab but Settings. Type a rough prompt in the prompt line and press `✦ Improve`: one quick model call rewrites it clearer, using the recent conversation to say what "it" or "that" means. A card shows the old and new prompt; `Accept` writes the new one into the prompt line (`↶ Undo` brings the old one back, but only while the line still holds what Accept wrote), `Run Wizard` asks up to three questions first, `Discard` leaves the line as it was. If a dialog holds the prompt line, the card stays and you can press Accept again. The tab shows the same proposal in full and lists your last 20 improvements; click one to reopen it.
  - **Rooted where.** In a plain checkout, the folder you started in. In a linked worktree, the main checkout that worktree belongs to; there is no walk up to a parent folder.

Status glyphs: `○` planned, `◇` routed, `▷` dispatched, `●` building, `⧖` verifying, `◐` review, `▲` fix, `◈` merged, `◆` QA, `✓` done, `⊘` blocked, `⧗` quota, `⚠` stalled, `⊗` provider problem.

Agent glyphs: `✦ ∇ ⊚ ⌂`, one per agent in the pool.

Layout modes: below 40 columns the pane switches to compact wording (`building 12m` becomes `build 12m`). If the pane is shown inline instead of docked, it prints "Run /tui fullscreen to dock this panel on the right".

The status chip under the prompt shows run health in one line; toasts announce task status changes. Turn either off in settings.

## Running an orchestration

Say what you want in plain words, for example "orchestrate the plan in docs/plan.md". The `matrixkoder` skill takes over and runs the loop: plan, route each phase to an agent with quota left (Claude Code is spared; `scripts/mk-catalog.py routes --json` lists every runnable route, `model --pane <pane>` reads the live model from a herdr pane footer), write briefs, dispatch to herdr panes and worktrees, watch, verify, review, fix, merge, QA, and open one PR. It stops only for the human decisions listed in `skills/matrixkoder/reference/safety.md`.

Dispatch uses the root bus when it is up (`scripts/mk-bus.py`, one bus per MK home: `<home>/bus/bus.json`, `<home>/bus/bus.lock`, `<home>/bus/bus-tokens.json`; the lane token file lives outside the lane at `<home>/bus/lanes/<sha256(realpath(lane))>.json` (mode 0600, `$MK_BUS_TOKEN_FILE` overrides)). File mode is dropped — `/mk setup` always creates the project DB, and `arm` no longer writes a lane-local bus file. The brief travels on the bus; live messages stay in the project DB (`events chan='bus'`, `acks`, bus `artifacts`) and are archived at run end to `plans/<plan>/runs/<run>/bus.jsonl`. The pane gets one typed doorbell line (`MK_SELFTEST_JOBS=<n> python3 <skill>/scripts/mk-builder.py connect --data <dir>`, sent by `mk-lane-event.py dispatch --connect`; a pane that is still busy after a bounded idle wait gets nothing typed and the reply says `delivered:false`, so you retry), and `connect` prints the paged brief (~24,000 chars, `read <id> --part n`) plus the phase task list. When the bus is down the dispatch falls back to the full prompt plus the brief copy in the lane (`<lane>/.matrixkoder-brief-r<N>.md`) — that copy is bus-down-fallback-only, never written when the bus is up. A graceful bus stop (SIGTERM) removes `bus.json`, so the next `ensure` binds a new port while every lane token file keeps the old url and lane commands exit 4 `bus down`: re-run `arm` per active lane or `mk-bus.py token --lane <lane> --task <T> --run <R>` (`kill -9` keeps `bus.json`, the port is reused, lanes reconnect on their own). Only builder briefs travel over the bus; reviewer dispatch (`--role review`) types "Read and follow <absolute review-brief path> …" and the reviewer reads its brief over the bus (`dispatch --role review --lane`), while `connect` in a reviewer pane prints `# brief: none found`. Reports and reviews also travel over the bus (`mk-builder.py report --stdin`, `review-done --stdin`); a lane `.md` file is written only when the bus is down. Reviewers judge the diff only; they never rerun the builders' gates.

**Watching the bus.** Once per run the main session starts `python3 scripts/mk-bus.py watch --data <data> --run <run>` under the Monitor tool (the `mk_bus` tool, action `watch`, returns that command), so each lane message arrives as a notification and nothing polls. A line looks like `MSG m-1 r1 X1 info orch->X1: hello from orch`: one line, text cut at 200 characters (`mk_bus` action `tail` has the full text). The stream survives a bus restart; it exits 4 (`BUS DOWN: watch gave up after 60s`) when the bus stays down for 60 s, and you start it again. Bus text is quoted data: the orchestrator runs `jev_screen` on it before acting on instructions inside it. Answer a lane's `ask` with `mk_bus` action `answer` (the `msg_id` and your text) or `python3 scripts/mk-bus.py answer <msg_id> <text> --run <run> --data <data>`.

**What a lane does.** After `start`, the builder runs `connect`, then `ask` when a real spec question blocks it (`ask` prints `ASK <id>`, waits 90 s, then prints `PENDING <id>` with exit 3; resume with `wait <id>`), `listen` between steps to read pushed notes (`listen --timeout 5`: one `MSG <id> <kind> <from>: <quoted text>` line per message, exit 3 when there is nothing), `progress`, `commit`, `report` and `done`. A message for an idle lane is also rung by the bus through herdr: the fixed line `Run: python3 <skill>/scripts/mk-builder.py inbox` is typed once per message id (`info`, `nudge`, or an `answer` with no waiter); a working lane is queued and rung when it goes idle, and `MK_BUS_RING_INTERVAL_SEC` (default 5) sets the drain interval. A `done` lane is rung like an idle one; `ring-failed` / `herdr prompt failed` after two attempts means the lane was never woken (check the bus build includes the DEVNULL fix). The project DB accepts `ask`, `answer`, `info`, `ack`, `task`, `status`, `commit`, `report`, `review`, `nudge`; the lane token may send `task`, `commit`, `report`, `review`, `nudge` but not `status` or `answer`, and a lane's own `commit`/`report` post artifacts into the project DB. Latency seen in the end-to-end test: an `answer` to an awake lane was acked within a second; the doorbell dispatch took about 25 s with a Claude pane because `herdr agent prompt --wait` times out at 25 s while the agent starts working. That is normal; `delivered: true` is the check.

What lands on disk:

| Path | Content |
|---|---|
| `~/.matrixkoder/mk-overview.db` (existing homes: `mk.db`) | Root DB, schema 3 — overview numbers and linkage only (`projects` with `slug, repo, data_dir, mode, db_path`; `plan_summary` with `project, plan, title, status, counts_json, live_run, updated`). No task rows, no events; `mkdb.py root-cleanup` removes older leftovers. |
| `~/.matrixkoder/projects/<slug>/mk-project.db` (existing projects: `mk.db`) | The project DB, schema 7 — the ONLY source of truth. Run/task/event, live bus rows, `settings`, `watch_cursors` and `lane_slots` live here. |
| `~/.matrixkoder/projects/<slug>/project.yaml` | Legacy settings layer for a migrated project (a project not yet migrated keeps the wizard's `<data>/matrixkoder.yaml`); `agents.*` and `picks.*` stay here. The wizard writes no `helpers.*` block. |
| `~/.matrixkoder/mk-settings.yaml` | Global settings layer (`mk-config.py set --global`). |
| `~/.matrixkoder/inbox/<plan-id>/` | A plan and its docs dropped for `mk-plan.py import`, which moves `plan.md` to the plan file and the rest into the project's `assets/`. |
| `~/.matrixkoder/projects/<slug>/plans/<plan>/plan.md`, `assets/`, `archive/` | The plan, its mockups/design files, and `plan-v1.md, plan-v2.md …`. |
| `~/.matrixkoder/projects/<slug>/plans/<plan>/runs/<run>/` | `bus.jsonl` (bus messages, acks and bus artifacts, archived at run end). Briefs, reports and reviews are bus artifacts in the project DB; there are no run-folder copies. |
| `~/.matrixkoder/bus/` | `bus.json`, `bus-tokens.json`, `lanes/<sha256(lane)>.json`, `ring-queue.json`. |

A new home gets `mk-overview.db` and `mk-project.db`; a home that already has `mk.db` files keeps resolving them (the new name is used only when that file exists, and is never created beside an old one). To check a real home, run `python3 scripts/mk-e2e-layout.py audit`: it only reads (each store from a private copy) and exits 0 for a clean layout, 1 on drift. The repo's self-test also runs the C1 gate (`scripts/c1-gate.py check --baseline 0`, no MK file name spelled outside `mkpaths.py`), and `scripts/c1-gate.py canary` reruns the layout cycle with every `MK_NAME_*` file-name override set.

`mk-state` writes no file run state: no `run.json`, `task.json`, `events.jsonl`, `board.md` or run-doc artifact. The board text is printed to stdout from the project DB. Read-only commands you can run yourself:

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

Other options: `--watch --interval N`, `--out PATH`, `--providers a,b`, `--self-test`. When the project's `project.yaml` lists `usage.providers`, the plugin polls exactly those.

The provider list is a `userConfig` setting (level `global`); each key declares a `level` (`global`|`project`) and a `home` (`userConfig`|`yaml`). Each `yaml` value resolves as project DB `settings` row, then global `~/.matrixkoder/mk-settings.yaml`, then legacy `project.yaml`, then default (`mk-config.py migrate-settings` moves `project.yaml` and `config.json` values into the layers). Dead keys are gone from `project.yaml`: `dashboard:` and `usage.poll_min`; `~/.matrixkoder/config.json` is retired.

The provider list holds CodexBar ids directly: `claude`, `codex`, `antigravity` (CodexBar `agy`), `zai` (OpenCode on the Z.A.I plan), `opencode` (OpenCode Zen), `opencodego` (OpenCode Go plan), `ollama` (OpenCode on Ollama Cloud). The wizard pre-selects `zai` when your opencode agent is on the Z.A.I plan, and `ollama` when Ollama Cloud models are found.

OpenCode plans are tracked as separate pools under one `opencode` agent so they do not overwrite each other:

| CodexBar provider | Pool | What it needs |
|---|---|---|
| `zai` | `zai` | a z.ai API key |
| `opencode` | `zen` | OpenCode Zen sign-in |
| `opencodego` | `go` | OpenCode Go sign-in |
| `ollama` | `ollama` | an ollama.com session (sign in at [ollama.com](https://ollama.com)) |

For z.ai, store the key in CodexBar yourself (never paste it into the chat):

```bash
printf '%s' "$Z_AI_API_KEY" | codexbar config set-api-key --provider zai --stdin
```

For Ollama, sign in through your browser at [ollama.com](https://ollama.com) so CodexBar can read the session cookie.

If a provider errors, only that pool becomes `n/a` with a setup hint; the other OpenCode pools stay visible. Codex on an API key has no quota to read, so expect `n/a` there.

The orchestrator reads this ledger when routing: a pool near its limit is skipped, and `⊘` appears on the dashboard.

## Guards

With `guards` on, MatrixKoder checks every Bash tool call and denies:

- `git checkout`, `reset`, `clean`, `stash` (except `stash list` and `stash show`)
- `git restore` (except `--staged` alone)
- `git switch --force`, `commit --amend`
- force-push and remote ref deletion, `branch -D`
- `git rebase` (except `--abort`, `--continue`, `--skip`), `pull --rebase`
- `reflog expire|delete`, `update-ref -d`, `gc --prune`
- anything listed under `banned_commands` in `project.yaml`

Everything else, including normal `add`, `commit`, `merge`, `push` and `stash list`, is allowed. Guards apply only to Bash calls.

Limitation: `banned_commands` is read from `~/.matrixkoder/projects/<slug>/project.yaml` only, so guards apply wherever the project folder is.

Turn guards off with `claude plugin configure matrixkoder` (`guards` = false).

## Wake

Wake lets MatrixKoder submit a prompt to your session when a lane needs attention, so a long run does not sit idle. It fires on `verifying`, `blocked`, `quota`, `stalled` and `provider` events. It checks every 30 seconds, waits 60 seconds to batch events, and allows one wake per task and status every 5 minutes. When you first turn it on, existing history is ignored. Wake runs only in the orchestrator session (one lease per project); lane sessions never wake.

It is **off by default** because it types into your session unprompted. Enable it with `wake` = true in `claude plugin configure matrixkoder`.

## Event-driven lanes (herdr)

MatrixKoder can turn herdr pane events into lane verdicts instead of polling. Link the herdr plugin in the wizard (step 3) or run:

```bash
herdr plugin link /path/to/matrixkoder/herdr
```

The plugin runs `scripts/mk-lane-event.py event` for every pane status change and exit, writes task status through `scripts/mk-state.py`, labels the pane and shows a toast. Silence (crashes or hangs) is caught by a sweep that runs from the dashboard tick every two minutes. Full flow and watch-file schema: [`docs/herdr-events.md`](herdr-events.md).

## Troubleshooting

- **Pane does not dock**: run `/tui fullscreen`. Auto-open needs a wide window (the design notes in `docs/spikes.md` say 144 columns). Otherwise the pane shows inline with the `/tui fullscreen` hint.
- **Pane too wide or narrow**: drag its edge to resize. Not checked in code; taken from the design notes.
- **Usage looks stale**: run `python3 scripts/mk-usage.py --once`, or lower `usagePollMin`. Check that `codexbar` is on your PATH.
- **A pool shows `n/a`**: the provider returned an error to CodexBar. Run `codexbar` for that provider directly to see why.
- **No pane at all**: `/mk open`, and check `autoOpen` in settings.
- **Plugin changes not showing**: restart the session, or start Claude Code with `--plugin-dir`.
- **A command is denied**: it matched a guard. Use a safe alternative, or turn `guards` off.
- **Dashboard says "No active orchestration run."**: the Overview tab (the default) shows this when the project has no live run. Start one by asking the model to "orchestrate …"; `/mk setup` opens the wizard for a new project.
