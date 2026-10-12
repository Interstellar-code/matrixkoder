# Event-driven lanes (herdr push)

herdr is MatrixKoder's event bus. Instead of a 20 s polling watcher per lane
(`scripts/lane-watch.py`, still available as a manual fallback), the herdr
plugin `matrixkoder` pushes pane events to `scripts/mk-lane-event.py`, which
decides the verdict with the exact same rules lane-watch uses — the verdict
logic is imported from lane-watch / lane-health, never copied.

```
builder pane (herdr agent) ──status change──► herdr server
   herdr plugin "matrixkoder" [[events]] pane.agent_status_changed / pane.exited
      └─► scripts/mk-lane-event.py event   (one short process per event)
            1. pane → armed lane: <lane>/.matrixkoder-watch.json (written at dispatch by `mk-lane-event.py arm`)
            2. same verdict as lane-watch: decide()/phase_tokens()/lane-health classify (imported, not copied)
            3. mk-state task set … status=verifying|blocked|quota|provider|stalled (CLI)  → events.jsonl
            4. herdr pane.report_metadata label (`H1 · verifying`), herdr notification.show on terminal states
   MK Claude plugin wake (exists) ──reads events.jsonl──► prompt into the orchestrator session
   stall backstop: `mk-lane-event.py sweep` (no event = silence) — run by the MK dashboard tick
```

## The herdr plugin

`herdr/herdr-plugin.toml` registers `[[events]]` for `pane.agent_status_changed`
and `pane.exited`; each runs `python3 ../scripts/mk-lane-event.py event` with
the plugin directory as cwd. Link it from a checkout:

```bash
herdr plugin link /path/to/matrixkoder/herdr
herdr plugin unlink matrixkoder      # to remove
```

Every event process receives `HERDR_PLUGIN_EVENT` (e.g. `pane.agent_status_changed`)
and `HERDR_PLUGIN_EVENT_JSON`
(`{"event":"pane_agent_status_changed","data":{"type":"pane_agent_status_changed","pane_id":"wD:p1","workspace_id":"wD","agent_status":"done","agent":"opencode"}}`;
`pane.exited` carries `pane_id` too), plus `HERDR_SOCKET_PATH` / `HERDR_BIN_PATH`
for the label and toast calls. herdr runs one process per event with **no
ordering guarantee** — all watch-file read/decide/write cycles hold an
`fcntl.flock` on `<lane>/.matrixkoder-watch.lock` (the lock is a sidecar file
because the watch file itself is atomically replaced, which would swap the
locked inode out from under a lock).

## Commands (`scripts/mk-lane-event.py`)

| Command | What it does |
|---|---|
| `arm --lane DIR --pane ID --task T --run R --data DIR --phase P --commit-grep RX [--report PATH] [--kind K] [--prompt-marker TEXT] [--stall-min 15]` | Writes `<lane>/.matrixkoder-watch.json` (atomic replace) and records the pane on the task (`mk-state task set T pane=<pane>`, `--by lane-watch`), so the board and the dashboard name the lane's pane without re-reading the watch files. That write is best-effort: a task that is not in run state yet still arms, and the reply carries `task_pane_error` instead of failing. Validates: lane is a git worktree, the regex compiles, `--task/--run/--data` present. Re-arming a lane (a fix round) overwrites the file and clears the verdict. Variant: `--tasks A,B` arms a shared lane group (one lane serving several phases of one plan). When yaml `bus.enabled` is true, `arm` also runs `mk-bus.py ensure` then `token` for the primary task and `disarm` runs `mk-bus.py revoke` — a bus failure rides in the reply as `bus:{ok:false,error}` and never fails the command (`$MK_BUS_CMD` overrides the script; `bus.enabled:false` runs no bus command at all). |
| `event` | The plugin hook (env above). Unknown pane → `{"ok": true, "ignored": "not an armed lane"}` exit 0 — fast, this fires for every pane on the machine. `working` → bumps `last_activity` only. `done`/`idle`/`blocked` → the same verdict one lane-watch poll would make; a verdict is recorded via lane-watch's `record()` (verdict → mk-state status, never moves a task past `WATCHED`), the pane is labeled (`pane.report_metadata`, source `matrixkoder`, `state_labels` like `{"mk": "H1 · verifying"}`) and a toast fires (`notification.show`, title `MatrixKoder`, body `H1 DONE → verifying`) — both best-effort. `pane.exited` → `STALL` with evidence `agent exited`; an exit after any verdict is post-finish cleanup (no-op). A herdr `ERROR` probe is not a verdict: the next event or sweep retries. |
| `dispatch --pane ID --text TEXT [--timeout-ms 25000] [--role build\|review] [--kind build\|fix\|takeover]` | One-call prompt helper: `herdr agent prompt <pane> <text> --wait --timeout <ms>`. With `--connect` (bus on) the typed prompt is only the connect doorbell (`mk-builder.py connect` line) after publishing the brief; without it — or when the bus is down or the publish fails — the caller's full prompt is typed unchanged (bus-down path). A delivered build prompt re-arms the board for the next round; `--role review` skips that write (`status_unchanged:true`). Reply carries `attempts`, `herdr_error`, `pane_entry` (see `--help`). |
| `review-handoff --task T [--run R] [--data DIR] [--hold] [--min medium]` | Reviewer handoff for a task (see `python3 scripts/mk-lane-event.py --help`). |
| `sweep [--data DIR]` | Backstop for silence: every armed lane (same discovery) with no verdict yet gets the same health check; no file/commit/pane change for `stall_min` since `last_activity` → `STALL`. Prints `{"ok": true, "checked": n, "verdicts": [...]}`. `--data` limits the sweep to one data dir. |
| `disarm --lane DIR` | Removes the watch file (keeps the lock file so concurrent waiters don't split inodes). The orchestrator runs this after acting on a terminal verdict. |

Every command prints one JSON line. Full flags: `python3 scripts/mk-lane-event.py --help`.

## Bus token (outside the lane)

The lane bus token file is NOT in the lane: `~/.matrixkoder/bus-lanes/<sha256(realpath(lane))>.json` (0600); `$MK_BUS_TOKEN_FILE` overrides the path. (An older `mk-bus.py` docstring still says `<lane>/.matrixkoder-bus.json` — that path is stale; the `token_path` function and the `--help` above are authoritative.) No brief copy is written into the lane when the bus is up — the `<lane>/.matrixkoder-brief.md` copy is bus-down-fallback-only.

## Watch-file schema (`<lane>/.matrixkoder-watch.json`)

| Key | Meaning |
|---|---|
| `lane`, `pane` | The worktree and the herdr pane id it is armed for |
| `task`, `run`, `data` | Where to write the verdict (`mk-state task set`, `--data`/`--run`) |
| `phase`, `commit_grep`, `report`, `kind`, `prompt_marker`, `stall_min` | The verdict inputs, identical semantics to lane-watch's flags. `phase` is the per-round token prefix: `<T> R<n>`, so `phase_tokens()` matches this round only |
| `armed_at`, `last_activity` | Epoch seconds; `last_activity` moves on `working` events and on new pane output (digits ignored) |
| `last_pane_fp` | Fingerprint of the last observed pane text (used by `sweep` to see pane activity) |
| `verdict`, `verdict_at` | Set after a terminal verdict; a repeated identical event is a no-op. `DONE` is final; a softer verdict (`stalled`, `blocked`, `quota`, `provider`) can still be upgraded (e.g. a stalled lane finishing → `verifying`). |

Idempotency: the same event twice never double-writes — the verdict stamp plus
lane-watch's `WATCHED` guard (a task past `dispatched building stalled quota
provider blocked`, e.g. already in `review`, keeps its status and only gets a
note event) make late and duplicate events safe.

## Limits

- **Silence needs `sweep`.** A crashed or hung agent emits no events; run
  `sweep` periodically (the MK dashboard tick, wave 2) as the stall backstop.
- **A finished turn is not a finished task.** Status `done` only means the turn
  settled; the verdict still requires lane-watch's rules — matching commit
  subject, the report (if armed with one), the `<PHASE> DONE` token (with
  `--phase`) and no quota/provider/dev-server finding. `P1 BLOCKED: <question>`
  in the pane wins as `blocked`.
- **The DONE token is per round.** Arm `--phase "<T> R<n>"` and require
  `<T> R<n> DONE` / `<T> R<n> BLOCKED: <q>`. A bare `<T> DONE` also matches the
  previous round's token still on screen — `pane wait-output` searches the
  current snapshot before it blocks, so it returned in 0 s on the earlier line.
  Prefer `herdr agent wait <pane>` (turn end, no text match) when you only need
  the turn to settle.
- **The pane is on the task too.** `arm` sets `pane` (e.g. `wK:p1`) on the task
  alongside the watch file; the watch file remains the source of truth for the
  verdict inputs.
- **Ordering is not guaranteed** between event processes; the flock serializes
  the read/decide/write cycle per lane.
- Discovery scans the worktrees of the repos in `$MATRIXKODER_HOME/projects.json`
  (`~/.matrixkoder` by default) — lanes must belong to a registered repo.
- `record()` attributes writes to `by: lane-watch` and notes are prefixed
  `lane-watch <state>:` (the shared helper, imported as-is).
