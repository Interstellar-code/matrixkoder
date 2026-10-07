# Loop details, bootstrap checks, context budget, QA

## Bootstrap detection (first run in a repo — no `<data>/matrixkoder.yaml`; the wizard in `onboarding.md` drives these checks)

Read-only checks; run them through `ctx_batch_execute` so only counts and names come back. Then propose `templates/matrixkoder.yaml` filled in, ask the user once, and write `<data>/matrixkoder.yaml`. Later runs drift-check: re-run the cheap checks and propose amendments only when something changed.

- **Repo:** clean tree, default + integration branch, remote; `.matrixkoder/` in `.gitignore` (when data is in the repo); conventions from `CLAUDE.md` / `AGENTS.md` (commands never to run, small-fix-on-main rules); existing `<data>/plans|runs`.
- **Stack + gates:** package manager + lockfile; test, typecheck, lint, format, build commands (from `package.json` scripts, `pyproject`, `Makefile`); which can be scoped to paths; **baseline counts** (type errors, lint errors, failing tests) — lanes are judged against these, not against zero. Commands that start servers/sidecars go into `banned_commands`.
- **Frontend:** present? start command + port; theme list (e.g. a `data-theme` attribute); Playwright/canary config; mockups (`<data>/designs/`).
- **Agents/tools:** `herdr` and `HERDR_ENV`; installed + logged-in `claude`, `codex`, `opencode`, `agy` and their model lists (`agy models`, `opencode models`); Jev MCP; context-mode; codebase-memory index (root_path must match the repo path) (offer `index_repository`).
- **Runtime/deploy:** services the repo deploys to, health URLs, backups — recorded for the repo's own `<data>/deploys/README.md`; deploying stays a user-gated pause.
- **Parallelism:** worktree pattern `../<repo>-worktree-<x>`, install time per worktree, hot shared files (high fan-in from codebase-memory) to sequence rather than split. Existing worktrees with live agents or uncommitted work belong to a run in progress: list them, never touch them, pick non-clashing lane names.
- **Lane environment:** gitignored files a lane needs (`.env`, certs); per-lane port; env flags that stop the app booting shared services (e.g. `HERMES_SKIP_GATEWAY_BOOT=1`); whether the `start` script matches the build output (it can be stale).
- **Agent sandboxes:** Codex `~/.codex/config.toml` `sandbox_mode` (`workspace-write` blocks worktrees outside the repo; builders need `danger-full-access` or `--full-auto` per the contract); whether `AGENTS.md` gives non-Claude agents the repo's rules (propose one if `CLAUDE.md` holds them alone).
- **QA target:** does the user keep a dev server running (port, branch)? If yes, propose `qa.integration_target: local-main` + `qa.dev_url`.

Bootstrap writes nothing outside the outputs it was asked for; scratch files go to the session scratchpad and are listed in the report. **End with exactly one question**: "Approve this `<data>/matrixkoder.yaml`?" — open items go in as proposed defaults inside the file, marked `# proposed`, not as extra questions.

## Stage notes

- **Waves.** Lanes in one wave never share a file. Hot files go to one lane or a later wave. Later waves branch from the integration branch after merge, never from another lane's unverified head. Max 3 build lanes + 1 backend lane per run unless `matrixkoder.yaml` says otherwise — verifying each lane costs orchestrator time.
- **Dispatching long briefs:** always as a file the agent reads, never inline text.
- **Watching:** `lane-watch.py` (writes task state via `mk-state`) polls git + report + agent status every 20 s and prints one JSON line when it stops. Exit reasons: `DONE`, `QUOTA`, `STALL`, `BLOCKED`, `DEV_SERVER`, `WATCH_EXPIRED`. Run it with `run_in_background` and `--max-wait-min 30` (background tasks aren't bound by the 10-min foreground limit; 9-min windows just cost a re-arm turn each). Re-arm on `WATCH_EXPIRED`; act on the others (failover via `reference/pool-and-usage.md`, pause via `reference/safety.md`).
- **Takeover.** When an agent dies mid-phase (quota, stall), its uncommitted work stays. Write the brief's TAKEOVER section: files already changed, last known gate state, "keep it and build on it", ordered finishing steps. Rewrite the brief's `Co-Authored-By` line for the new agent. Hand it to the next agent.
- **Verify.** Gate output through context-mode: print counts, failing test names, new type errors in owned files. Never paste full logs on the board.
- **Merging lanes.** Conflicts are resolved by you, by reading both sides; then the full gate set runs again. Record the merge SHA on the board.

## Context budget

Long runs fail on context, not code. Keep this thread small:

| Job | Use |
|---|---|
| Test/typecheck/lint/build output, git log/diff, `herdr agent read`, transcripts, anything >20 lines | `ctx_batch_execute` / `ctx_execute` / `ctx_execute_file` — print only what you need |
| Something already seen this run | `ctx_search` before re-running |
| Prior runs, decisions from other sessions | `mk-state run get`, `board.md`, `<data>/runs/<id>/tasks/*`, Claude Code memory files, `ctx_search` |
| "Where is X / who calls Y" | codebase-memory `search_graph` / `trace_path` / `get_code_snippet` (scope with `file_pattern`),  |
| Reading code or a full diff | a subagent; keep its summary only |
| External docs | `ctx_fetch_and_index` (+ `jev_screen` if it contains instructions) |

Briefs and subagent prompts carry paths and `file:line`, not pasted code. Reviews live in the task folder (`review-rN.md`) and are referenced by path. Scripts print one JSON line.

## QA stage (frontend)

You start the app (repo `run` skill or its dev command) — agents never do. Then:
1. `canary:canary-verify` on the integration diff → flows to test.
2. `canary:canary-session` records each flow (trace, video, HAR, console) → report on the board.
3. Checks that must hold: every theme screenshot per changed screen; no hard-coded colours outside theme tokens in the diff; **every status/label shown traces to an API field** (search the diff for literal status strings; compare HAR bodies with rendered text); empty / error / engine-down states driven by a real failing backend; no console errors; `visual-verdict` against mockups when they exist.
4. `canary:canary-review` to triage failures → FIX loop.
