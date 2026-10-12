# Live test — mk-store (C14)

The matrix the orchestrator runs. Self-tests prove code; these gates prove the system.
Setup for every gate: `python3 scripts/mk-livetest.py setup` (a TEMP MK home and a scratch
git repo, never `~/.matrixkoder`), the build under test on a scratch branch, one lane slot
armed with the child skill of that build, OpenCode `manifest/auto` as the lane agent.
`python3 scripts/mk-livetest.py scripted --gate G1|G2|G3` runs every `scripted` row for that
gate. `agent` rows need a real lane and are never claimed by the scripted runner.

## Gate rules

- **G1 — after wave 4.** Stores, task tree, bus storage and the kanban tab are in.
  Scripted rows first, then a real agent.
- **G2 — after wave 6.** Root bus, todo hooks, review handoff and the Waiting tab are in.
- **G3 — before DM.** The full matrix once more on the finished build, then
  `mk-store-import.py move` on a COPY of a real project (never the live one).
- No gate is skipped. A gate failure is recorded as a verdict, diagnosed (brief gap,
  verification gap, scope, capability), fixed in a round, and the gate is re-run from its start.
  A failed gate blocks the next wave.

`kind` is `scripted` (no AI) or `agent` (a real OpenCode `manifest/auto` lane).
A row whose feature does not exist yet is `pending: <phase>`; the scripted runner
reports it `PENDING`, never as pass.

A gate is signed off only when every scripted row it runs has passed. The summary
line carries `pass`, `fail` and `PENDING` counts, and `ok` is true only when `fail`
and `PENDING` are both 0. The process exits 0 when ok, 1 when any row failed, and 3
when nothing failed but some rows are still PENDING. A later gate re-runs the earlier
ones: G1 runs the G1 rows, G2 runs G1 and G2, and G3 runs every row.

## Matrix

| id | contract | gate | kind | pending | do | see |
|---|---|---|---|---|---|---|
| C1-create | C1 | G1 | scripted | | `setup` opens the root store at `<home>/mk.db` through `mkdb.py`. | The file exists; `journal_mode=wal`, `busy_timeout=5000`, `foreign_keys=1`. |
| C1-projects | C1 | G1 | scripted | | Register a project row (slug, name, repo, data_dir, mode). | `projects` holds the row; `usage.json` and `locks/` stay files. |
| C1-summary | C1 | G2 | scripted | | After transitions, read `project_summary` and `sync`. | `active_runs`, `by_status_json`, `attention` and `last_change_no` match the project store. |
| C2-create | C2 | G1 | scripted | | `setup` opens the project store at `<data>/mk.db` through `mkdb.py`. | The file exists with the same three PRAGMAs; plans/runs/tasks/events tables are present. |
| C2-timeline | C2 | G1 | scripted | | `mkdb.py timeline --run R --task T --json` after one transition. | One JSON line; events for that task only, ordered by id, chan `state`. |
| C3-tree | C3 | G1 | scripted | | Add a phase, a task under it, and a subtask; parent comes from nesting. | All three rows exist; a missing parent is refused. |
| C3-cycle | C3 | G1 | scripted | | Add a task that depends on itself, then two tasks that depend on each other. | Both refused; the cycle error names the path; neither bad row is stored. |
| C3-ready | C3 | G1 | scripted | | Mark every dep of a task merged/done. | The task becomes ready; parallel siblings with no dep between them may both be building. |
| C4-columns | C4 | G1 | scripted | | Transition a task through planned, building, review, done, then blocked. | Columns are Backlog, Building, Review, Done; blocked keeps its column and raises attention. |
| C5-sync | C5 | G1 | scripted | | `transition()` with root sync, then `mk-state.py sync` after a crash between the two writes. | Root card and summary catch up from `sync.last_change_no`; only C1 fields cross. |
| C6-lifecycle | C6 | G2 | scripted | | Two scratch projects heartbeat one root bus; stop one project's heartbeats. | The bus stays up while the other heartbeats and exits only after `owner_timeout_min`. |
| C6-tokens | C6 | G2 | scripted | | Use a project-scoped token against the other project. | Denied (403); a lane token cannot read another project; the root token is read-only. |
| C7-connect | C7 | G1 | agent | pending: D7 | `connect` on a real `manifest/auto` lane. | A paged brief and the phase task list arrive; one `[<id>]` todo per task. |
| C7-todo | C7 | G2 | scripted | | `todo_hook: yes` and the backup `todo done` both fire for the same (task, state, round). | The card moves once (dedupe); `session.todo_done_status` verify and done both work. |
| C8-catalog | C8 | G1 | scripted | | Seed agents and models, then read a route pick list. | Rows match the seed; a model may override capabilities; routing reads the catalog. |
| C9-setting | C9 | G2 | scripted | | Set `session.todo_done_status` to verify, then to done. | Completing a todo lands the task in verifying or done accordingly; default is verify. |
| C10-transition | C10 | G1 | scripted | | Move a task with `mkdb.transition()` and try any other status write. | One state event, a bumped `change_no`, and the root card only when sync is on; no second writer. |
| C11-slug | C11 | G1 | scripted | | Open a store with a bad slug, then with a home under `/Volumes` or iCloud. | Both refused before any path is created; the error names the slug pattern or the synced home. |
| C11-wal | C11 | G1 | scripted | | After `setup`, list the store files. | `mk.db-wal` stays inside the temp home, never inside the scratch git repo. |
| C12-fallback | C12 | G1 | scripted | | Stop the bus, then `ask`. | Exit 4 and `BUS DOWN: use the BLOCKED token`; the task shows blocked; dispatch falls back to the full prompt. |
| C13-untrusted | C13 | G1 | agent | pending: D7 | A builder sends a bus message that contains instructions. | `mk_bus`, wake and `pending` show it as quoted data; the orchestrator does not follow it. |
| C13-doorbell | C13 | G2 | scripted | | Address a nudge to an idle agent, then to a working one. | The idle agent is rung once with the fixed herdr line; the working agent's nudge queues. |
| C13-restart | C13 | G1 | scripted | | `kill -9` the bus and start it again. | It binds the same port; the lane url still answers. |
| C13-ask-blocked | C13 | G1 | scripted | | `ask` while the bus is down. | Exit 4 and the task status is blocked. |
| C13-ack-once | C13 | G1 | scripted | | Deliver the same answer twice to a lane inbox. | One ack only (ack level recorded once). |
| C13-metrics | C13 | G2 | scripted | | `mk-bus.py metrics` after a scripted ask/answer. | A JSON line of the research counters, not an error. |
| C14-harness | C14 | G1 | scripted | | `setup`, one scripted row, `teardown`. | Temp home and scratch repo are gone; a second teardown of the same path is refused. |

## Agent rows (not run by `scripted`)

These need a real lane. The scripted runner does not score them.

- **G1.** `connect` delivers a paged brief and the task list (C7). Ask, answer and ack 3 are
  recorded in the project store. A seeded pending ask is visible via `mk-bus.py pending`
  (the Waiting tab shows it only after D8B).
- **G2.** An idle agent is rung exactly once; a card moves within one dashboard tick; the
  Waiting tab shows a real ask; review-handoff opens a fix round and the builder receives it
  through the orchestrator; a real OpenCode lane finishes a small task end to end with no file
  in the lane except its work.
- **G3.** The full matrix again, then `mk-store-import.py move` on a copy: copy, import,
  parity per run, rename, rollback via an `export-legacy` round-trip.
