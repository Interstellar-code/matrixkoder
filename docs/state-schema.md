# MatrixKoder run-state schema (v1)

Contract between `scripts/mk-state.py` (the **only writer**), `lane-watch.py`, `mk-usage.py` and the
dashboard mod (TypeScript, read-only). Field names are snake_case and stable; adding fields is
allowed, renaming or removing one needs `schema` bumped.

Since the `projects/` layout (C2–C7 of the storage-layout plan) the project DB is the only source of
truth: no `run.json`, `task.json`, `events.jsonl`, `board.md` or run-doc artifact is written or read
any more, and file mode is dropped (`/mk setup` always creates the project DB). Everything below is
the DB contract; the file shapes that lived here before the `projects/` layout (until 2026-10-15) are
gone, and `mk-migrate.py` is the ONE migration tool.

## Locations

| What | Path |
|---|---|
| Project data dir | `--data DIR` → `$MATRIXKODER_DATA` → the repo's `projects.data_dir` row in the root store (keyed by the **main checkout** path; `mkpaths.data_dir`) → the repo folder. toplevel = the nearest `.git` above cwd, never `$HOME` itself (a dotfiles repo is not a project); a linked worktree (`.git` file with `commondir`) resolves to its **main checkout**, a nested repo to itself (pass `--data` to reach the outer repo). Once the project is migrated (its root `projects.db_path` is set) the data dir is the project's **legacy** location; the store and every live folder come from `mkpaths.project_db(slug)` / `mkpaths.project_folder` |
| Global dir | `$MK_HOME` / `$MATRIXKODER_HOME` → `~/.matrixkoder` (`mkpaths.home` — **one** rule: both env names are the MK home and must agree; TS: `mkHomeEnv`/`globalDirOf` in `hooks/paths.ts`). Plugin prefs live in the plugin's userConfig, not in this dir |
| Root store | `<home>/mk-overview.db` on a new home, `<home>/mk.db` on an existing one (`mkpaths.root_db`) — the overview numbers and linkage (the `projects` row per project and one `plan_summary` row per plan, C3), the agent catalog tables and the user's `todos`. No task row, no event |
| Project store | `<home>/projects/<slug>/mk-project.db` on a new home, `.../mk.db` on an existing one (`mkpaths.project_db`) — the project DB, the only per-project DB and the ONE source of truth (C4). A migrated project's root `projects.db_path` names this file; `mkpaths.project_db(slug)` resolves it (the bus follows every switch through it) |
| Project folder | `<home>/projects/<slug>/` — holds the project DB (`mk-project.db`, or `mk.db` on an existing home), `project.yaml` (today's `matrixkoder.yaml`), `legacy/` and `plans/` (the C2 tree below). `watch-cursors.json` is no longer written: the bus watch cursors are `watch_cursors` rows in the project DB, and the file is read once as an import source |
| Plan folder | `<home>/projects/<slug>/plans/<plan-id>/` — `plan.md`, `assets/`, `archive/` (`<plan-id>-v1.md`, `<plan-id>-v2.md` …) and `runs/` |
| Run folder | `plans/<plan-id>/runs/<run-id>/` — `bus.jsonl` (C6) and one folder per task |
| Task round files | `plans/<plan-id>/runs/<run-id>/<task-id>/`: `brief-rN.md`, `report-rN.md`, `review-rN.md` (paths returned by `task round`; written by the orchestrator / agents, not by mk-state; `task round --undo` refuses while any of them exists for the latest round) |
| Lane registry | With a project DB: the `lane_slots` rows of the project DB, written **only** by `mk-lane.py`; `lanes.json` is neither read nor written. File mode (no project DB) only: `<data>/lanes.json`, written only by `mk-lane.py` (see below). `mk-lane.py carry-legacy` carries a pre-switch registry into the rows once. `mk-state.py` never touches either |
| Bus files | `<home>/bus/` — `bus.json`, `bus-tokens.json`, `bus-tokens.lock`, `bus.lock`, `bus-ensure.lock`, `ring-queue.json`, `lanes/<sha256(lane)>.json` (C6) |
| Watch cursors | The `watch_cursors` rows of the project DB (C6), keyed `<run or *>\|<task or *>`, written by `mk-bus.py`. `<home>/projects/<slug>/watch-cursors.json` (0600) is no longer written; `mk-bus.py` reads it once as the import source when a key has no row yet |
| Locks | `<home>/locks/<resource lower-cased>.lock` |
| Usage ledger | `<home>/usage.json` (owned by `mk-usage.py`; lane-health only adds `exhausted_*` fields, on the agent or on one pool, see below) |
| Usage ledger lock | `<home>/usage.json.lock` — an empty sidecar every read-modify-write of `usage.json` takes as an exclusive `fcntl.flock` (`mk-usage.py`'s poll and lane-health's quota mark; the two would otherwise lose each other's fields). It holds no data, and it is derived from `<home>` rather than from the ledger path, so writers pointed at different `--out`/`--ledger` paths still serialise |
| Run backup | `<data>/archive/<run>.db` — `run archive`'s `VACUUM INTO` copy under the legacy data dir (see `run archive`) |

Every one of these paths comes from `scripts/mkpaths.py` (Python) or `hooks/paths.ts` (TS) — C1: no
other file implements the MK home rule, the data-dir order, the project DB path, a plan/run/task
folder, the lane-token path or the watch-cursor path (`hooks/data.ts` keeps only the pure string
math they build on).

Store file names (L10.13): a fresh home names its stores `mk-overview.db` (root) and `mk-project.db`
(project). A store that already exists as `mk.db` keeps that name and keeps resolving: the lookup
(`mkpaths._resolve_db`) takes the new-name file when it exists, else an existing `mk.db`, else the new
name, and it never renames a file or creates a new-name file beside an old one (migrate v2, L10.8,
is not shipped). Store-creating calls name their kind (`mkpaths.root_db_in` / `project_db_in`; `db_in`
guesses it from the MK home). The names are overridable per file through `MK_NAME_ROOT_DB` /
`MK_NAME_PROJECT_DB` (and one `MK_NAME_*` for each other file-name constant) for the rename canary
(`c1-gate.py canary`, below). Where this document writes `mk.db` below it means whichever of the two
names the store has.

C1 is a standing check: `scripts/c1-gate.py check --baseline 0` fails on any literal store or run
file name outside `mkpaths.py` / `hooks/paths.ts` (kept hits are listed in the script), and the
self-test runs it. `c1-gate.py canary` runs `mkpaths.py --self-test` and the `mk-e2e-layout.py`
cycle with every `MK_NAME_*` overridden on a temp home, and fails on a default-named file in the
tree. `mk-e2e-layout.py audit [--home DIR]` is the read-only audit of a real home (default
`~/.matrixkoder`): it reads each store from a private copy, prints one JSON line
`{tree, tables, schema, rules}` and exits 1 on drift.

### The folder tree (C2)

```
~/.matrixkoder/
├── mk-overview.db                root: overview, catalog and todos (C3); `mk.db` on an existing home
├── usage.json
├── bus/                          bus.json, bus-tokens.json, lanes/<sha256(lane)>.json, ring-queue.json (C6)
└── projects/<slug>/
    ├── mk-project.db             the project DB (C4) — the only per-project DB; `mk.db` on an existing home
    ├── project.yaml              today's matrixkoder.yaml
    ├── legacy/                   read-only tarball of the project's old data dir (C5, after user confirm)
    └── plans/<plan-id>/
        ├── plan.md               the plan (reference after import)
        ├── assets/               mockups, design files
        ├── archive/              plan-v1.md, plan-v2.md …
        └── runs/<run-id>/
            ├── bus.jsonl         archived bus messages, acks, bus artifacts (C6)
            └── <task-id>/        brief-rN.md, report-rN.md, review-rN.md
```

Exactly two DB kinds: the root and one per project. No per-plan DB, no bus DB. The repo keeps **no**
MK data — `project.yaml` (today's `matrixkoder.yaml`) lives in the project folder. Lane-worktree
files (`.matrixkoder-*` in a lane) are not data and stay.

### Who writes what

| Writer | Writes | Never touches |
|---|---|---|
| `mk-state.py` | the project DB (runs, tasks, events, artifacts, waits, and its `ack`/`artifact` helpers); the board text it prints; the lane locks under `<home>/locks/` (the root store only through `mkdb`'s `plan_summary` refresh / `catch_up`) | `lanes.json`, `lane_slots` |
| `mk-plan.py` | the project DB (`plans`, the task rows) and the plan folder (`plan.md`, `archive/<id>-v<n>.md`) | the root store |
| `mk-lane.py` | the `lane_slots` rows of the project DB (the lane registry; best-effort mirror onto the task), and `lanes.json` in file mode only (no project DB) | the run/task state tables |
| `mk-migrate.py` | the root `projects.db_path` column (the ONLY root column it moves), the C2 project folder and the `legacy/` tarball | anything else |
| `mkdb.py` | the root's `plan_summary` rows; every project-table write; `root-cleanup` / `restore` on the root home | — |
| `mk-bus.py` | `<home>/bus/` files and the live bus rows of the project DB (`events` chan='bus', `acks`, bus `artifacts`) | the state tables |
| `lane-watch.py` / `lane-health.py` | events and the quota mark in `<home>/usage.json` (see below) | `lanes.json`, `lane_slots` |

## The project DB

`<home>/projects/<slug>/mk-project.db` (`mk.db` on an existing home) is the only source of truth (C4). The tables `plans`, `runs`, `tasks`,
`events`, `artifacts`, `acks`, `lane_slots`, `waits` and `change_log` are described under
[SQLite stores](#sqlite-stores-scriptsmkdbpy) below. No file view of the run state is written (L4) or
read (L5, L6.2); the board is **printed to stdout** from the DB by
`mk-state.py render`. New data goes into a new column or a new table of the existing project schema —
never a duplicated field. File mode is dropped: `/mk setup` always creates the project DB.

A run row (`runs`): `id`, `plan` (the plan **id**, C4), `plan_version`, `branch`, `status`, `stage`,
`next`, `started`, `ended` (C4). The run's task order is the `tasks` table's insertion order (parents
before children); the file-only fields the tables have no column for — `feature`, `waiting`, and the
computed `recent`/`ready`/`rounds` views — are derived at read time.

### Run fields (one `runs` row)

| Field | Type | Meaning |
|---|---|---|
| `id` | string | run id, unique in the project |
| `feature` | string \| null | `--feature` |
| `branch` | string \| null | `--branch` |
| `plan` | string \| null | an OLD-shape run (started before RUN): the plan file's path, still read by `close`, `plan sync` and `backfill-fields`. A run started from a stored plan version (RUN) holds `null` here: it keeps no plan file path. (The `runs.plan` column holds the plan **id** for both.) |
| `plan_id`, `plan_sha`, `plan_status` | string \| null | **C4 plan link**: the plan's id (also the `runs.plan` column). `plan_sha` (old-shape runs only): sha256 of the plan file bytes from the final `mk-plan.py parse` after `set --status running` + `add-run`; a run started from a stored version (RUN) stores **no** `plan_sha`. `plan_status` is the status at run start (`running`); the board of a stored-version run prints the store's `plans.status`, not this field. `plan sync --apply` refreshes `plan_sha`/`plan_status` of an old-shape run; the last plan-linked run set `done` flips the plan to `done` (best-effort). `null` on plan-less runs |
| `plan_version` | int \| null | the plan version the run was built from (`runs.plan_version` column, never NULL for a run started by `run init --plan <id>` since RUN). **Its presence is what makes a run store-based**: `close`, `plan sync`, `backfill-fields`, `plan_done_if_last` and `mk-builder.py plan_tasks` read that version's `plan_items` rows and never open a plan file. NULL on old-shape runs (they keep reading their plan file) and on plan-less runs |
| `plan_gates` | object \| null | (`runs.extra_json`, stored-version runs) the plan's `[gates]` copied from the store at `run init`, so `run set --status done` runs the same gates without the plan file; the plan's final gates ride `final_gates` as before |
| `status` | `active \| done \| archived` | `ended` is set when it leaves `active`, cleared when it returns. `run set --status` takes `active\|done`; `archived` only via `run archive` |
| `stage` | int \| null | loop stage 0–12 (SKILL.md) |
| `next` | string \| null | next action, shown by `render` and in the dashboard's resume section |
| `waiting` | object \| null | the one question waiting on the user: `question`, `options` (string[]), `rec` (string \| null), `since` (ts) — the `waits` table (cleared when answered) |
| `started`, `ended`, `updated` | ts | timestamps |
| `recent` | array | last 10 events, read from the `events` table (`chan='state'`) |
| `tasks` | array | task rows in insertion order (below) |

`run list` is ordered by `started`; `run get` defaults to the
single active run. Archive is a flag on the row; nothing moves.

### Task object (one `tasks` row)

| Field | Type | Meaning |
|---|---|---|
| `id`, `title` | string | `id` unique case-insensitively within the run (`T1` and `t1` clash) |
| `type`, `parent` | string \| null | `phase \| task \| subtask`; parent task id (same run). `--parent` onto a task in `merged`/`qa`/`done` is refused |
| `wave` | int \| null | |
| `deps`, `owned` | string[] | tasks it waits on (`T` in this run, `RUN:T` in another run of the same data dir); file globs it owns. Stored in the target's stored id case (ids match case-insensitively; a self-dep is refused at add). `task set status=dispatched\|building` fails while a dep of the task **or of any ancestor** is not `merged`/`qa`/`done` (`--force` overrides) |
| `parent` | string \| null | the parent task's id (same run); the same value the `type` cell's parent carries |
| `ready` | bool | derived (not a column): has deps, the published status is `planned`/`routed`, every dep met. The store status is `ready` (the kanban column) and the published status is kept in `tasks.file_status`; a flip to true logs a `deps met` note; the dashboard toasts it |
| `status` | enum | `planned → routed → dispatched → building → verifying → review → fix → merged → qa → done`; side states `blocked \| quota \| stalled \| provider`. Input aliases, stored as the canonical word (also on `event --from`/`--to`): `reviewing`→`review`, `merging`→`merged`, `fixing`→`fix`. A parent's status **rolls up** from its children (`mkdb.transition`): a side state if any child is in one (`blocked` > `stalled` > `quota` > `provider`), else the least advanced child, raised to `building` only when at least one child is past `routed` and the least advanced child is below `building` (parents are never lowered); the last non-attention status is kept in `tasks.prev_status`. Manual `status=` on a task with children **fans out**: every child is set to it (one transition event each, the same `--note`), and the parent itself is never written directly |
| `agent` | string \| null | agent kind or pane (`codex`, `agy`, ...) |
| `model`, `effort`, `lane`, `pane` | string \| null | `lane` = worktree (slot) path; `pane` = herdr pane id of that lane (`wK:p1`), written by `mk-lane-event.py arm` (`mk-state task set <T> pane=<pane>`, best-effort) |
| `branch`, `workspace`, `slot` | string \| null | the lane's own names, written by `mk-lane.py acquire`: `branch` = `mk/<run>/<task>`, `workspace` = herdr workspace label `mk:<repo>:<n>`, `slot` = slot number as a string. Empty value clears (`branch=`), like `lane=`/`pane=`; the board shows all three on the lane cell when set |
| `last_commit` | string \| null | short sha; also copied to the current round's `sha` |
| `gates` | any JSON \| null | e.g. `{"test": "pass", "tsc": "pass"}` (pass a JSON value to `task set`) |
| `verdict` | string \| null | latest review verdict; also copied to the current round's `verdict` |
| `round` | int | current round number (0 = none yet) |
| `rounds` | array | derived (not a column): the round history from the round events and the round `artifacts` rows, `{n, kind: build\|fix\|takeover, cause, verdict, sha, started}` |
| `plan_phase`, `route`, `lane_group`, `reviewer` | string \| null | **C4**: set on a *phase* task created by `run init --plan` — the plan phase id, its pinned `route` (`""` = Jev decides, stored `null`), its `lane_group` (phases sharing a value share one worktree), and the phase's `reviewer` (`""` = orchestrator decides, stored `""` — the plan's value or `""` when absent; `task set reviewer=` rewrites it). `null` on ad-hoc tasks (`task add`) and children; the `reviewer` key is absent there until `task set` adds it |
| `plan_task` | string \| null | **C4**: set on a *child* task created from a `[[phase.task]]` — the plan task id (`P1.1`); its `parent` is the phase task. `null` on ad-hoc and phase tasks |


## Schema 6 (additive: evidence, risk, reconcile)

Schema 6 adds three tables plus per-task risk fields. It is **additive**: every
writer auto-migrates, so the first v6 write upgrades an existing store in place.

### `evidence` (one row per gate/check run)

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PRIMARY KEY | |
| `run`, `task` | TEXT | the run and task the check belongs to |
| `line` | TEXT | the check's identity (file:line or command) |
| `gate` | TEXT | the gate name |
| `result` | TEXT | one of `pass fail timeout deferred error manual` (checked in the writer helper, not a DB CHECK) |
| `tree_sha` | TEXT | the git tree SHA the check ran against |
| `by` | TEXT | writer |
| `ref` | TEXT | external reference (log, artifact) |
| `output_tail` | TEXT | tail of the check output |
| `output_sha256` | TEXT | sha256 of the full output |
| `created` | TEXT | timestamp |

### `risk_reasons` (one row per open risk reason)

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PRIMARY KEY | |
| `run`, `task` | TEXT | the run and task the risk belongs to |
| `kind` | TEXT | risk kind |
| `points` | INTEGER | risk weight (feeds the derived risk rule below) |
| `source` | TEXT | what raised it |
| `opened` | TEXT | timestamp |
| `cleared` | TEXT | timestamp; **open** = `cleared IS NULL` |
| `cleared_by` | TEXT | an evidence id or a note |

### `reconcile_due` (one row per reconcile obligation)

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PRIMARY KEY | |
| `run` | TEXT | the run |
| `scope` | TEXT | what must be reconciled |
| `created` | TEXT | timestamp |
| `done` | TEXT | timestamp; pending = `done IS NULL` |

### Indexes

`evidence_run_task ON evidence(run, task)`,
`risk_reasons_run_task ON risk_reasons(run, task)`.

### Per-task fields in `tasks.extra_json`

- `critical` (bool, default false)
- `base_risk` (int 1–5, default 1)
- `gates` (any JSON, stored and returned; F.2 defines the format)
- `gate` (list of `{cmd, expect, timeout, lines}`), `manual` (string, never run),
  `acceptance_lines` (list of `{id, text}`): copied from the plan by `run init` / `plan sync --apply`
  for a plan with the F.2 contract. `base_risk` is the plan's task `risk`. All of these are
  read-only through `task set` (refused with a "read-only" error); the plan text is their only home.
- `description` (string, optional; task and subtask only): the plan's plain-English description. `run init` and `plan sync --apply` copy it when non-empty. `plan sync --apply` (with `--from-file` on a live run) updates or clears it on existing rows with no change line and no `updated` bump. `task set` does not accept it.

### Per-run fields in `runs.extra_json` (F.3)

- `fidelity` (bool): set by `run init` / `plan sync --apply` when the plan carries the F.2
  contract. A legacy run has none and is exempt from every evidence check below.
- `final_gates` (list of `{id, cmd, expect, every, timeout, needs, covers}`): the plan's `[[final_gate]]`s.

### Reconcile (F.5)

- A fidelity run writes one `reconcile_due(run, scope=<phase id>, created, done NULL)` row when the
  last task of a phase closes (a plain INSERT; no gate runs inside that write). A legacy run writes none.
- `reconcile --run R [--phase P]` runs the due and STALE gates (closed tasks whose latest evidence is
  missing, red or not on the current tree, and final gates) through the one gate runner and writes
  evidence rows. A red gate is rerun once: red twice on the same `tree_sha` is reproducible and moves
  the gate's `covers` tasks (or the task itself for a task gate) `merged` -> `rework`; red then green
  is flaky and changes nothing; timeout/error/deferred are inconclusive and open a `risk_reasons` row
  (kind `reconcile-inconclusive`, 1 point) per covered task without changing any status. A clean scope
  stamps its due rows done; a passing reconcile changes nothing else.
- `run set --status done` and `run archive` are refused in a fidelity run while any task is at
  `rework`, any risk reason is open, or any `reconcile_due` row is not done.
- `task round T --kind rework` moves a MERGED task to `rework` (round status `rework`); it is
  refused on any non-merged task.

### Gate evidence and the fidelity gate (F.3)

- `gate run <T> | --final | --due [--ref R]`: runs the gates in the integration checkout through
  the one gate runner (timeout kills the whole process group; load above 40 records `deferred`) and
  writes one `evidence` row per gate. `result` is one of `green|red|timeout|deferred|error`;
  `tree_sha` is `git rev-parse HEAD^{tree}` of the checked tree. Gate names: `<T>#<n>` (or the
  gate's `id`), final gates by `id`.
- `gate signoff <T> --ref R`: records a green `manual` evidence row; a `manual` line is never run.
- `risk add <T> --kind K --points N [--source S]` | `risk list [<T>]` | `risk clear <id>
  --evidence <evidence id>`: writes/reads `risk_reasons`. A clear needs a green, non-manual
  evidence row of that task on the current tree.
- `fidelity_gate` guards every closing path in a fidelity run: `task set` to merged/qa/done,
  parent fan-out (the parent and every descendant with its own gate/manual are checked, not just
  the leaves), automatic roll-up (a parent with its own gate/manual never rolls up to a closed
  status without fresh green evidence), `promote_closed`, `run set --status done` (tasks plus final
  gates) and `run archive`. The latest evidence row per gate must be green AND on the current tree;
  older evidence is stale and does not count. A `manual` line needs a sign-off (any tree).

### Derived risk rule

`risk = min(5, base_risk + sum(points of the task's OPEN risk_reasons))`.
Read-only, never stored, never settable by `task set`. The `plans --json` reply
carries `risk`, `critical`, and `open_reasons` (list of
`{kind, points, source, opened}`) on every task row.

### The `rework` status

In `COLUMNS` (maps to `Fix`) and in `ATTENTION` (card raises the flag). `column_of` always
returns `("Fix", 1)` for `rework` regardless of previous status. A phase with one `rework`
child rolls up to `rework` even when siblings are merged. NOT in `DEP_MET`, NOT in
`CLOSED_STATUSES` — an open status that blocks run close.

### Schema 6 rollout

Any `mkdb.py` copy older than this commit (an installed plugin cache such as
`~/.claude/plugins/cache/matrixkoder/<ver>`, a worktree cut before the merge)
refuses the store after the first v6 write: `_open_read` and `_migrate` exit 3
with "store schema v6 is newer than this code". The fix is to update the
`mkdb.py` copy. All writers auto-migrate, so the first v6 write upgrades the
store in place.

## Events (the `events` table)

```json
{"t": "2026-10-07T20:09:00+02:00", "run": "mk-p1", "task": "P1", "type": "transition", "text": "lane-watch STALL: agent idle, no change 11 min", "from": "building", "to": "stalled", "by": "lane-watch"}
```

One `events` row per event: `id` (the integer primary key, `mk-bus`'s SSE `Last-Event-ID`), `run`,
`task`, `t`, `chan`, `kind`, `from_id`, `to_id`, `reply_to`, `text`, `artifact_id`, `meta_json`.
`type` is `kind` in the object above: `transition | decision | verdict | quota | note`. `task` is
`null` for run-level events. `from`/`to` are task statuses (`from` is `null` when a task is added; the
status aliases resolve here too, the canonical word is stored). `by` = writer (`orchestrator`,
`lane-watch`, ...; `--by` flag); it rides in `meta_json`. `text` carries the evidence (plan §4 called
it `evidence`). `task set status=...` appends the transition event itself. Every `--force` override of
a safety gate writes exactly one `decision` event `forced by <by>: <command summary>` (`by` = the
`--by` flag, default `orchestrator`): `task set` past unmet own+ancestor deps, `run init --plan` past
an unapproved plan status. `text` must be non-empty; a `transition` event needs `to`.

`chan` is `state` on a state event, `bus` on a live bus message (C6), `note` for an ad-hoc note and
`inbox` for a plan file's step in the Inbox (C5: `run`/`task` NULL, so no run owns it — only this
channel may omit the run; `scripts/mk-inbox.py` writes it and the Events tab reads it with
`mkdb.py events --chan inbox --json`, never waking the orchestrator).
Status changes go **only** through `mkdb.transition()`: `append_event` refuses `chan='state'` with
`kind='transition'`. A live bus message stays in the project DB while the run is open; at run end the
run's messages, acks and bus artifacts are archived (count-checked) to
`plans/<plan>/runs/<run>/bus.jsonl` and leave the DB — the watch/wake/lane cursors tolerate the gap.

## `lanes.json` (slot registry, `scripts/mk-lane.py`)

**File mode only.** With a project DB the `lane_slots` rows are the lane registry and
`lanes.json` is neither read nor written; `mk-lane.py carry-legacy [--dry-run]` carries a pre-switch
registry (the legacy store's `lane_slots` and the legacy `lanes.json`) into the rows once: it inserts
only slots absent from the target, never overwrites a newer target row, and refuses while any slot is
busy. The rest of this section describes the file as file mode (no project DB) still uses it.

It describes the reusable **worktree slots** of this project, not a run (a project-level file beside
the data dir, not inside a run folder).
In file mode `mk-lane.py` is its only writer and mirrors the lane it hands out onto the task (`lane`, `branch`,
`workspace`, `slot` task keys, best-effort: a failed write there is a warning on the acquire, never a
failed one). `mk-state.py` never reads or writes it.

```json
{"schema": 1,
 "slots": {"1": {"path": "/Users/me/repo-mk-1", "workspace": "mk:repo:1", "workspace_id": "wD",
                 "state": "free", "run": null, "task": null, "branch": null,
                 "lock_hash": "9f2c…", "updated": "2026-10-07T20:09:01+02:00"}}}
```

| Field | Meaning |
|---|---|
| `path` | the slot's absolute checkout path (`repo.lanes.pattern` with `{repo}`/`{n}`, against the main checkout) |
| `workspace`, `workspace_id` | herdr workspace label and its id (`null` when the slot was created with plain `git worktree add`, or with `MK_HERDR=none` in the self-test) |
| `state` | `free` \| `busy`. A busy slot carries the `run`/`task`/`branch` it was acquired for |
| `lock_hash` | sha256 of the slot's lockfile when it was last acquired; a different hash (or a created slot) is what `install_needed` reports |
| `updated` | last write, ISO 8601 local time with offset |

Every read-modify-write holds an exclusive `fcntl.flock` on `<data>/lanes.lock` (wait up to 30 s, then
`{"ok": false, "error": "lanes.lock busy"}` exit 2) and writes atomically (temp file + `os.replace`,
`write_json`). `mk-lane.py list` is the only read-only command: it takes no lock and reports a live
`clean` per slot.

### What makes a slot reusable (`acquire` and `release` both refuse, exit 3)

- **Clean working tree.** `git status --porcelain=v1 -z --ignored --untracked-files=normal
  --ignore-submodules=none`, split on NUL. `-z` is what makes this trustworthy: porcelain v1
  C-quotes a path with a space, a non-ASCII byte or a leading `:`, and a quoted path matches nothing
  and reads as untracked. The flags are on the command line on purpose, because
  `status.showUntrackedFiles=no` in the user's config would otherwise hide `??` lines and this check
  is the only thing between a hidden untracked file and `git worktree remove`. Every record must be an
  ignored entry (`!! …`) on the allowlist: `.matrixkoder-*`, `.env`, `.env.*`, `node_modules/`,
  `.omc/`, `.serena/`, `.venv/`, `__pycache__/` plus `lanes_disposable`. The three build-cache dirs
  (`node_modules/`, `__pycache__/`, `.venv/`) match as a path component at any depth; every other
  entry is root-only. A **directory** entry is disposable only while the repo tracks nothing inside
  it, at every level including the root: `git --literal-pathspecs ls-files -z -- <dir>` must be empty
  (a non-zero exit counts as tracked, i.e. dirty). A vendored `node_modules/`, `.venv/` or a dir named
  `:x` is therefore source, and an ignored file inside it keeps the slot dirty.
  `lanes_disposable` entries must be literal names (`[A-Za-z0-9._-]+`, optional trailing `/`) — a `*`
  would make every ignored file disposable, so rejected entries are reported in the `warnings` field
  of every `ok` answer instead of being applied, and so is a `lanes:` line that does not parse (the
  defaults are kept, silently widening nothing).
- **No commit that no ref reaches.** `git rev-list HEAD --not --branches --tags --remotes` must be
  empty, checked before every `switch` **and before every removal** (`gc`, and `release` with
  `reuse: false`): a free slot sits detached, so a late builder commit there would be orphaned by the
  next switch — and `git worktree remove` deletes the worktree's `logs/HEAD`, so a removal would take
  it from reflog-only to dangling. `gc` reports it as a skip with the SHAs in `unreachable`; `release`
  exits 3.
- **The path is ours.** A stored `path` is never rewritten: if the configured pattern now gives a
  different path the acquire is refused (`slot 1 is at X, pattern now gives Y`). Before any switch or
  remove, the path must be a worktree registered by `git worktree list` for this repo and must not be
  the main checkout; a new slot's path must not exist at all (an empty directory included) and must
  not resolve inside the main checkout. The pattern must contain `{n}`.
- **Refs are untrusted.** `--base` / `--integration` may not start with `-` and must resolve through
  `git rev-parse --verify --quiet --end-of-options <ref>^{commit}`; generated branch names go through
  `git check-ref-format --branch`. git — and herdr — then get the resolved sha.
- **`--integration` is a branch, not a commit.** It must resolve to `refs/heads/<name>` *in its stored
  spelling* — `git for-each-ref --format=%(refname) -- refs/heads/<name>` must echo it back — and must
  not be the slot's own task branch. On a case-insensitive filesystem (macOS, `core.ignorecase`) git
  resolves a loose ref under any case and `rev-parse --symbolic-full-name` echoes the alias, so
  `--integration MK/R1/T2` would otherwise pass as the task branch `mk/r1/t2`. Ref names are compared
  as strings; no tip-sha comparison is made, because integration == task tip is a legitimate
  fast-forward merge. The task branch is looked up as `refs/heads/<b>`, so a tag of
  the same name cannot shadow it, and it is kept (not deleted) while `git worktree list --porcelain`
  shows it checked out in another worktree — `update-ref -d` does not know about worktrees.

Notes worth knowing before you rely on it: a `switch` can overwrite an allowlisted ignored file if the
target commit happens to track that name (git's own behaviour — keep secrets out of the base branch);
a crash between creating the worktree and writing the registry (`lanes.json` in file mode) leaves a worktree the registry does not
know (delete the directory and `git worktree prune`, then acquire again — the create path refuses an
existing directory); `run`/`task` are stored as typed while the branch is lowercased, so `T1` and `t1`
map to one branch; and a **hand-edited** registry (`lanes.json`, file mode) pointing a slot at another worktree of the same
repo is outside what the tool can detect — it trusts its own registry, so do not edit it by hand.

## Lock file `<home>/locks/<resource>.lock`

```json
{"resource": "dev-server", "reason": "QA on :3000", "scope": ["restart", "deploy"], "by": "orchestrator", "pid": 97433, "host": "Mac.lan", "taken_at": "2026-10-07T20:09:01+02:00", "expires_at": "2026-10-07T20:39:01+02:00"}
```

Created with an exclusive hard link (never half-written); file name lower-cased (APFS is
case-insensitive, so `DB` and `db` are one lock). `lock check` → `free | held | stale`
(`stale` = past `expires_at`). `lock take` on a held or stale lock fails (exit 1) and returns the
current holder; `--force-stale` replaces a stale lock only, never a held one. `--ttl-min` must be a
finite number >= 0. `lock release` refuses (exit 1) a lock whose `by` differs from the caller's `--by`
unless `--force`.

## Usage ledger additions (lane-health QUOTA fallback)

lane-health writes into `<home>/usage.json` → `agents.<kind>` (`--kind claude|codex|opencode|agy`;
pane name if no kind), or → `agents.<kind>.pools.<pool>` with `--pool NAME` for one OpenCode
provider's plan (`zai`, `go`, `ollama`, `zen`), which is where a mark belongs when CodexBar reports no
windows for that plan at all. It never touches CodexBar fields; it sets `source: "pane-text"` only when
the entry did not exist yet. Both read-modify-writes (this one and `mk-usage.py`'s poll) hold the
`<home>/usage.json.lock` flock for the whole cycle.

```json
{"agents": {"agy": {"source": "codexbar", "windows": ["…CodexBar data, untouched…"],
  "exhausted_until": "2026-10-13T18:43:00+02:00", "exhausted_window": "weekly", "exhausted_guess": false,
  "exhausted_source": "pane-text", "exhausted_observed_at": "2026-10-07T12:00:00+02:00",
  "exhausted_evidence": ["Individual quota reached"]},
  "opencode": {"pools": {"zai": {"five_hour": null, "weekly": null,
    "exhausted_until": "2026-10-08T16:16:57+02:00", "exhausted_window": "5h", "exhausted_guess": false,
    "exhausted_source": "pane-text", "exhausted_observed_at": "2026-10-08T11:50:00+02:00",
    "exhausted_evidence": ["Usage limit reached for 5 hour"]}}}}}
```

The same field set, written either on the agent (`agents.<kind>`) or on one plan
(`agents.<kind>.pools.<pool>`, `--pool`).

| Field | Meaning |
|---|---|
| `exhausted_until` | ts, never null when written. From the message ("reset at <date time>", "resets in 1h 30m / 45m / 2d 3h", "try again in 2 hours", "resets 3pm"); none printed → `observed_at + 5h` with `exhausted_guess: true` |
| `exhausted_window` | `5h \| weekly \| null` — weekly when the reset is more than 6 h ahead (or the message says "week") |
| `exhausted_guess` | true when `exhausted_until` is the +5h assumption. A later hit without a reset time never replaces a known future `exhausted_until` |
| `exhausted_source` / `_observed_at` / `_evidence` | `"pane-text"`, when, the matched phrases (max 2) |

**Contract with `mk-usage.py` (P2):** it keeps every `exhausted_*` field when it rewrites an agent entry
— and, the same way, when it rewrites a pool entry — and clears them (sets `exhausted_until` to `null`,
may drop the rest) once `exhausted_until` has passed. Only a pane-text mark whose `exhausted_until` is
still in the future is kept; an `exhausted_until` this script wrote itself is recomputed every poll.
Readers: an agent (or a pool) is exhausted while `exhausted_until` is non-null and in the future; null or
past = not blocked by pane text (CodexBar windows still apply).

## Usage ledger pools (mk-usage.py)

Each `agents.<kind>` entry groups CodexBar windows into **pools** (each window gets a `pool` field;
duplicates with the same pool/kind/`resets_at` are collapsed, preferring `antigravity-quota-summary-*` ids).
Mapping (`POOL_RULES` in `mk-usage.py`): agy `gemini` / `claude-gpt`; claude `fable` (Fable only) / `default`;
everything else `default`.

```json
{"agy": {"five_hour": {"used_pct": 19.6, "resets_at": "…"}, "weekly": {"used_pct": 53.9, "resets_at": "…"},
  "exhausted_until": null, "available_pools": ["gemini"],
  "pools": {"gemini": {"five_hour": {}, "weekly": {}, "exhausted_until": null},
            "claude-gpt": {"five_hour": {}, "weekly": {}, "exhausted_until": "2026-10-13T06:00:45Z"}}}}
```

- `pools.<name>` = `{five_hour, weekly, exhausted_until}`; `exhausted_until` = latest reset among that pool's windows at >= 100%, else null. A pool may also carry the lane-health `exhausted_*` set (`--pool`), so its shape is `{five_hour, weekly, exhausted_until, exhausted_window, exhausted_guess, exhausted_source, exhausted_observed_at, exhausted_evidence}`.
- Agent-level `five_hour` / `weekly` = the primary pool (agy `gemini`, others `default`); if that pool is absent, the first pool present.
- Agent-level `exhausted_until` = the primary pool's `exhausted_until` (claude `default`, agy `gemini`). Another pool being free does not clear it: claude's `fable` is a sub-scope, so a 100% `default` pool makes Claude exhausted. `available_pools` lists every pool not exhausted — a router wanting a secondary pool (agy `claude-gpt`, claude `fable`) checks it.
- A future pane-text `exhausted_until` **on the agent** from lane-health overrides that agent's own pools (pane text is newer evidence); the later of the two wins.
- **A pane-text mark on a pool survives a poll**, exactly as one on an agent does: it is kept while its `exhausted_until` is in the future, and CodexBar replaces it only when it reports a *later* `exhausted_until` for that same pool. A poll that reports no window for the plan — the normal case for every OpenCode one — therefore leaves the mark in place instead of recomputing it away; once the marked window has passed the mark is dropped. The mark lives inside `pools.<name>`, the dict `available_pools` is derived from, so a marked plan stops being offered to the router even though the agent-level `exhausted_until` stays `null` (one plan down is not the whole agent down).

## CLI

| Command | Flags |
|---|---|
| `run init` | `--id ID --feature F --branch B [--plan PLAN_ID [--version N] --force]` — `--plan` takes a plan **id** of the project store (import and approve the file first). The run's tasks, gates and final gates are copied from the `plan_items` rows of that version (default: the latest approved; only the plan's current version is accepted, since its `[gates]` are stored for that version) and `runs.plan_version` records it. A path (a `/`, a `.md` suffix or an existing file) is refused with a hint to `mk-plan.py import <path>` first; a plan that is not `approved` is refused (`approve it (mk-plan.py set --status approved) or pass --force`) unless `--force`, which writes a `decision` event naming `--by`; a plan approved before versioned items (no `plan_items` rows) is refused with `import this plan again first`; nothing is migrated or copied for the user. The plan then gets `set --status running` + `add-run --run ID` **by plan id** (store only), and the phase and child tasks are created through the task-add path. The run keeps no plan file path and no sha, and the file may be deleted. A run that already holds a plan file path keeps working through it. |
| `plan sync` | `[--run ID] [--apply] [--from-file]` — diff the run's plan against its tasks (dry run by default, nothing written; a run with `plan_version` reads that version's `plan_items` rows from the store, an old-shape run parses its plan `.md`): new phase/task → added via the add path; changed `title`/`wave`/`owned`/`deps` on a `planned\|routed` task → updated, on a started task → warning only; removed → warning, **never deleted**. On a **live** run (any task not `merged`/`done`) it REFUSES — the DB tasks are the source, and `task set` is how they change; `--from-file` is the explicit "the plan file wins" override. `--apply` stores the new `plan_sha` (old-shape runs; a stored-version run keeps none) and writes exactly ONE summary event (the reused paths' per-task events are suppressed) |
| `run list` / `run get` | `run get [--id ID \| ID]` (default: the single active run) |
| `run set` | `[--id ID] [--next TEXT] [--status active\|done]` (at least one) |
| `run stage` / `run archive` | `run stage N` (N >= 0, `[--id ID]`); `run archive ID` sets the row `archived` and, first, `VACUUM INTO`s the project DB to `<data>/archive/<run>.db` (temp file first, verified to hold the run and its task count; an existing backup is never overwritten — the new one lands as `<run>-<UTC timestamp>.db`; a failed backup leaves the run active) |
| `task add` | `--id T --title TEXT [--wave N] [--deps A,RUN:B] [--owned glob,glob] (--parent P | --root)` — F.11: one of `--parent` (nest it) or `--root` (a top-level phase) is required and the two exclude each other; plain `task add` without either is refused with the fix; deps are stored in the target's stored id case and a self-dep is refused; `--parent` onto a task in `merged`/`qa`/`done` fails; returns `warnings` for unknown deps, owned globs overlapping an unmerged task in any active run (the new task's own `--parent` chain is not an overlap: a parent owns its children's globs), and active sibling runs that could not be read |
| `task set` | `T key=value ... [--note TEXT] [--force]` (`--force`: dispatch past unmet own+ancestor deps; writes a `decision` event naming `--by`); `status=` on a task with children fans out to every child — one transition each, the same `--note`, the parent itself never set directly (it rolls up), the dep gate applied per child before any write; status aliases `reviewing`/`merging`/`fixing` store as `review`/`merged`/`fix`; keys `status agent model effort lane pane branch workspace slot last_commit gates verdict reviewer parent title acceptance owned deps refs`; a value starting `{`/`[` is parsed as JSON, empty = null. This is the way to change a task in a RUNNING plan: the change lands in the DB, never in the plan `.md` |
| `task round` | `T --kind build\|fix\|takeover [--cause TEXT]`; `T --undo` drops the latest round — but only while none of its brief/report/review files exist (the paths `task round` returned) in `plans/<plan-id>/runs/<run-id>/<task-id>/`, else `ok:false` naming them; exactly one note event (`round N undone`); no rounds / `--kind` with `--undo` are refused |
| `task verify` | `T --tests '<passed>/<failed>' [--tsc N] [--lint N] [--selftest '<run>/<failed>'] [--fail TEXT]` — stage 7; one `verdict` event, baseline compare, status move (see the subsection below). A failed count larger than the passed count is refused |
| `task get` | `T` |
| `event` | `[--task T] --type transition\|decision\|verdict\|quota\|note --text TEXT [--from S] [--to S]` |
| `wait set` / `wait clear` | `--question TEXT --options a,b [--rec TEXT]` |
| `lock take` / `release` / `check` | `RES --reason TEXT [--ttl-min 30] [--scope a,b] [--force-stale]`; `release RES [--force]` |
| `render` | print the board to **stdout** from the project DB (`{"ok": true, "board": "…"}`) |
| `store status` / `store enable` | the project DB's presence (`on`) or the legacy one-way import (see Store mode) |
| `sync` | `[--project SLUG]` — heal the root `plan_summary` rows against the project store (see Store mode) |

Common flags: `--data DIR`, `--by WHO` (default `orchestrator`; written to events and locks), `--run ID`
for task/event/wait/render (`run *` commands take `--id`, alias `--run`).

### Output

Every `mk-state.py` command prints exactly one JSON line: `{"ok": true, ...}` exit 0;
`{"ok": false, "error": "..."}` exit 2 on bad input or an unreadable store; lock refused →
exit 1 with `state` and `lock`. A malformed sibling active run only produces a `warnings` entry
and is skipped for cross-run `ready`/board upkeep. Flags never prefix-match (`--fo` is an unknown
argument). `task round` returns `{round, kind, brief, report, review}` absolute paths for that round
(under `plans/<plan-id>/runs/<run-id>/<task-id>/`);
`task round --undo` returns `{round, undone}` with `round` back to the remaining round number.

Every lane-watch verdict's evidence ends with a source tag saying which channel decided it:
`source=store` (the project DB's `tasks` row — `store row done` / `store row blocked`),
`source=pane` (the pane text: the DONE token, QUOTA, STALL, ERROR, WATCH_EXPIRED), or
`source=grace` (the no-token rule: commit and report present, agent idle, token missing). The
project DB is the only source (C4); a watch reads it read-only and never creates it.

lane-watch exit → task status (with `--task --data`, both required together): `DONE`/`DONE_SENT → verifying`,
`QUOTA → quota`, `STALL → stalled`, `PROVIDER → provider`, `BLOCKED → blocked` — only while the task is
`dispatched|building|stalled|quota|provider|blocked`; otherwise the status is kept and a `note` event is
logged. `DEV_SERVER`, `ERROR`, `WATCH_EXPIRED` only append a `note` event.

A verdict read from the store (`source=store`) carries the row it read (`seen`: task, status,
`updated`). `record()` re-reads the task before writing: when the status or `updated` changed since
(an `answer` set a `blocked` task back to `building`), the newer status is kept and only a `note`
event is logged (`status <s> kept: row changed since read`).

### `task verify` (stage 7)

`task verify T --tests '<pass>/<fail>' [--tsc N] [--lint N] [--selftest '<run>/<failed>'] [--fail 'why']`
records a verify gate run as **one `verdict` event** on `T` (the `--by` flag rides on it) whose text
spells the counts in human-readable form, e.g. `tests 250/1 · tsc 0 · lint 0 · selftest 8/0` — flags
not passed are simply left out. `--tests` is required and must match `<passed>/<failed>` — two integers. A failed count
larger than the passed count is refused (`bad --tests '3/5' … the failed count is larger
than the passed count, which looks like passed/total`), since that shape usually means the
caller passed passed/total; equal counts pass the check. So must `--selftest` when given.

The counts are compared with the run's **latest `baseline:` `decision` event** — the last event of
type `decision` whose text starts with `baseline:`; its `tests P/F` and `selftest R/F` numbers are
the gates (`tsc` and `lint` ride in the text as context only). A gate **drops** when the verify count
is below the baseline count (`tests dropped 250→249`, `selftest dropped 8→7`); equal or higher is
fine, and a count the call did not pass is not compared.

Then the status move: on **pass** (no `--fail` and no dropped count) `T` — and every direct child,
when `T` has children — moves to `review`; on **fail** (a `--fail` reason **or** a dropped count)
they move to `fix`. Each move is one `transition` event carrying the same `--by` and the note
`verify: pass` or `verify: fail — <why>` (the `--fail` text, or the drop list); a task already in
the target status gets no transition (nothing moved).

## Plan file → DB

`skills/matrixkoder/templates/plan.md` is the contract: every header key there carries a trailing
comment naming its DB home, and the table below is the same contract written out. Additive only —
no key is renamed or removed, so every plan written against an older template stays valid.

`plans(id, version, title, status, created, design, integration, header_json, body_md,
approved_at, archived_path)`. `status`, `created`, `design` and `integration` are real columns
(C1). `design` and `integration` are JSON array text. `runs`, the `[gates]` block, the
`[[phase]]` blocks (their `description` included) and the plan-level `description` stay in `header_json`.

`archived` is a DB-only `plans.status` (plan files keep draft/approved/running/done/parked).
`mk-plan.py archive` sets it, keeps the prior status in `header_json.archived_from` and moves the
plan's asset set under `<project>/archive/plans/<plan-id>/`, layout kept: the C2 plan folder as
`folder/`, and from the data dir `plans/<id>.md`, `plans/archive/<id>-v<n>.md` (exact id) and
`runs-legacy/<run>` / `runs/<run>` for each run of the plan. The moved (from, to) pairs are written
to `header_json.archived_files` (with `archived_to`) before the move and `plans.archived_path` is
rewritten to follow; `unarchive` restores each pair, refusing when an original is occupied or an
archived item is missing. `archive` on an archived plan with no record and assets still in place
finishes the move. `mk-plan.py delete` takes an archived plan only: after a verified
`<project>/archive/<plan-id>-pre-delete-<UTC ts>.db` backup, one transaction deletes the rows of
the plan's runs (`tasks`, `events`, `artifacts`, `acks`, `waits`, `change_log`, `evidence`,
`risk_reasons`, `reconcile_due`, `event_src`), the `runs` rows and the `plans` row. The archived
files stay.

| Header / block element | DB home |
|---|---|
| `plan` | `plans.id` |
| `status` | `plans.status` |
| `created` | `plans.created` |
| `runs` | `plans.header_json` |
| `design` | `plans.design` (JSON array text) |
| `integration` | `plans.integration` (JSON array text) and `plans.header_json` |
| `pushed` | `plans.header_json` |
| `[gates]` / `[[final_gate]]` / `use_gates` / `gate_lines` | `plans.header_json` (`use_gates` resolved to project gate commands at run init) |
| Block 2 Summary (`title`, `phases`, `method`, `lanes`, `delivery`, `gates`) | `plans.header_json` (`summary`) |
| Block 3 Goal (`G<n>` goals, `goal_meta`) | `plans.header_json` (`goals`) |
| Block 4 Design / context (`C/T/A/R/S` items) | `plans.header_json` (`items`) |
| Block 5 Phases: phase `id` / task `id` / subtask `id` | `tasks.id` (and `plan_items.id`) |
| `title` (phase, task, subtask) | `tasks.title` (and `plan_items.title`) |
| Phase `goal`, `serves`, `carries`, `hazards` | `plans.header_json` (inside `phases`) and `plan_items.extra_json` |
| Task `covers`, `size` | `plans.header_json` (inside `phases.tasks`) and `plan_items.extra_json` |
| `description` (header) | `plans.header_json` |
| `description` (phase, task, subtask) | `plans.header_json`; task and subtask also `tasks.extra_json` (copied at run init and plan sync) |
| `acceptance` (task, subtask) | `tasks.acceptance` (and `plan_items.acceptance`) |
| `owned` (phase, task, subtask) | `tasks.owned` (and `plan_items.owned`) |
| `deps` (phase, task, subtask) | `tasks.deps` (and `plan_items.deps`) |
| `parallel` (task, subtask) | `tasks.parallel` (and `plan_items.parallel`) |
| `route` (phase, task, subtask) | `tasks.route_json` (`{"agent","model","effort"}` or empty) |
| nesting of `phase` / `task` / `subtask` | `tasks.type` (`phase\|task\|subtask`) and `tasks.parent` (and `plan_items.type`, `plan_items.parent`) |
| Block 7 Risks (`K<n>` risks, levels, guards, targets) | `plans.header_json` (`risks`) |
| Block 8 Open questions (`Q<n>`, ticked, question, blocks, pick) | `plans.header_json` (`questions`), ticked and answered in store |
| Block 9 Log (`date`, `kind`, `text`, `who`) | `plans.header_json` (`log`), appended via `mk-plan.py log` |
| Recorded question overrides | `plans.header_json` (`overrides`) and question `overrides` list |
| `wave`, `forbidden`, `reviewer`, `size`, `risk`, `review`, `qa`, `commit`, `subagents`, `lane_group`, `refs`, `subagent` | `header_json` and `plan_items.extra_json` — no column (`critical` is removed) |
| the body below the closing `+++` | `plans.body_md` |
| the body H1 line | `plans.title` — the H1 text after the em dash (`# <id> — <title>`); the plan id when there is no H1 (C3) |

The task rows come from the importer `approve_file` (`scripts/mk-plan.py`): its `flat_items`
yields `(id, type, parent, title, acceptance, owned, deps, parallel, route)` per phase, task and
subtask, parent first, and each becomes one `tasks` row of the run (and, for a new version, one
`plan_items` row, below).

### Store modifications after approval (STORE.2, contract C4)

After a plan is approved, its approved version file under `archive/` is immutable and never changed.
The following commands update the plan state directly in the store (`plans.header_json`):
- `mk-plan.py log <plan id> --kind <k> --text <t> --by <who>` appends a Log entry to `header_json.log`.
- `mk-plan.py answer <plan id> Q<n> --text <t> --by <who>` ticks the question in `header_json.questions` and records an answer Log line.
- `mk-plan.py override <plan id> Q<n> --task <id> --by <who>` records a question override on a task in `header_json.overrides` and an override Log line.

### Versioned plan items (`plan_items`, `plan_versions`; STORE.1, contract C4)

Two tables, created in place when missing by the commands that write plans (`import`, `approve`;
`mkdb.ensure_plan_items`, idempotent `CREATE TABLE IF NOT EXISTS`). The schema version is
unchanged: an older store still opens for a read, and nothing existing is copied, converted or
rewritten. A store with no `plan_items` table reads exactly as before (`plans <id>` falls back to
`header_json`).

`plan_items(plan, version, id, parent, type, title, acceptance, owned, deps, parallel, route_json,
extra_json, ord)`, primary key `(plan, version, id)`. One row per phase, task and subtask of one
plan version, in `ord` order (parent first). `type` is `phase|task|subtask`; `owned` and `deps` are
comma-separated; `acceptance` is the flat text (acceptance lines joined by a newline); `route_json`
is `{agent, effort, model}` or NULL; `extra_json` holds every other key of the item (`wave`,
`description`, `gate`, `manual`, `risk`, `refs`, per-phase `goal`/`serves`/`carries`/`hazards`,
per-task `covers`/`size`, … — `critical` is removed and no longer stored), so the rows alone
rebuild the plan.

`plan_versions(plan, version, sha, created, approved_at)`, primary key `(plan, version)`: the
approved mark is per version (`approved_at` NULL = not approved) and `sha` is the sha256 of the file
the version was imported or approved from. This companion table is what makes "this version is
approved" and "this file is that import" answerable; a version with no phases would still have a row.

- `import` writes a new version (`plans.version` + 1) with its rows and a `plan_versions` row.
  The same bytes again (same sha) write nothing. A changed file is a new version and the reply
  carries `items` and `changes: {added, changed, removed}` (item ids versus the previous version; a
  plan stored before versioned items is compared with the phases kept in `header_json`).
- `import` records the path it was given as `header_json.source`. A reference only: nothing
  re-reads the file through it.
- `approve` marks a version approved and never deletes or rewrites an older version's rows. When the
  file is the version a prior import wrote (same sha), that version is approved as it is (edited rows
  included); otherwise approve writes a new version with its rows. The archive copy is
  `<id>-v<version>.md`.
- `item set <plan> <id> [--title] [--description] [--acceptance] [--owned] [--deps]` edits one row
  of a **draft** plan's current version in the store (the file is not read or written); see
  *Plan commands*.
- Reader: `mkdb.plan_items(con, plan, version=None)` returns a version's items in `ord` order
  (`[]` when there are none) and `mkdb.py plans <id> --json` builds `phases` from them instead of
  `header_json` when rows exist, adding each item's `acceptance` and `owned`.
- `delete` removes the plan's `plan_items` and `plan_versions` rows with the plan.

Lifecycle (C13): `import` loads the file into the store without approving it; `approve` checks
the file, marks a version approved, writes the tasks and copies the file to
`<home>/projects/<slug>/plans/<id>/archive/<id>-v<n>.md`; `run init --plan` starts the run. The plan
file is a source only until import: after import status, runs, tasks, rounds and verdicts live in
this DB, and `set`, `add-run` and `approve` do not rewrite the file when the store holds the plan
(a file plan with no store row is still rewritten by `set` and `add-run` as before). `parse` and
`check` answer with the store's `status` and `runs` once the plan is stored, so `run init --plan`
keeps working. When the plan is done the file moves into the plan folder's `archive/`.

`mk-plan.py check` reports a missing `status`, a missing `created` and a body with no H1 line as
**warnings**, never errors, so an older plan still checks `ok: true`.

## Plan commands (`mk-plan.py`)

`import`, `approve`, `delete`, `item set` and pause/resume, as `scripts/mk-plan.py` `build_parser`
ships them (C4, C10, C13). Every command prints one JSON line. `--data` and `--home` are optional on
`import`, `approve`, `delete`, `set`, `add-run`, `item set`, `parse` and `check`.

Data-dir resolution (`resolve_data_arg`), the same order as `mk-state` and copied rather than
imported: `--data`, then `$MATRIXKODER_DATA`, then the data dir this repo registered (the root
`projects.data_dir` row), else the repo folder. A
linked worktree uses the main checkout. None of these commands creates a store: a store exists
only when the project DB (`<home>/projects/<slug>/mk-project.db` for a migrated project, else
`<data>/mk-project.db`; an existing `mk.db` keeps its name) is already a file. Once the project is migrated, the store and the plan folder
come from `mkpaths.project_folder` / `mkpaths.plan_dir`, so nothing is written under the legacy
data dir.

`import <file> [--dry-run] [--data DIR] [--home DIR]` loads one plan and does not approve it. It
writes one `plans` row (status, created, design, integration, the H1 title, `header_json`,
`body_md`) and one `plan_items` row per phase, task and subtask for the new version, and no task
rows, and it never edits the file. A file whose first line is not `+++`,
or whose header does not parse, is refused (`ok: false`, `reason` set, exit 0). A header with no
`plan` id is refused the same way. The same bytes imported again upsert the same row and change
nothing (`noop: true`, version unchanged); a changed file is the next version (`1` when the plan is
new) and the reply lists `changes` (added, changed and removed item ids). `--dry-run` returns the
same reply and writes nothing. Import needs a store; without the project DB it errors.

`approve <path> [--data DIR] [--home DIR] [--run ID]` checks the file, writes the plan version and
its tasks, and copies the approved bytes to `plans/<id>/archive/<id>-v<n>.md` in the project
folder. The original
stays in place and is **not rewritten** (STORE.2; the store row and the archive say approved, and
`parse`/`check` answer with the store's status, so `run init --plan <original path>` still
resolves). The reply names the run id as `tasks_run` (the `--run` value, else the stored run list's
first entry, else the file's first `runs` entry, else the plan id) — no run row exists until `run
init`. Approving the same approved bytes again is a no-op. It warns `N unticked open questions` and
still approves.

`delete <id> [--data DIR] [--home DIR]` is refused while any `runs` row has `plan=<id>` and
`status='active'` (exit 2, the live run ids named). Otherwise it removes the `plans` row and
moves the plan's `plan.md` into the plan folder's `archive/` (a timestamp suffix when that name is
already taken). The file is never unlinked. Tasks of finished runs stay.

Pause and resume are `set <path> --status parked` and `set` back to the previous status. When the
store holds the plan, `plans.status` changes and the file is not touched (STORE.2); `add-run
<path> --run ID` appends to the stored run list (`header_json.runs`) the same way. A file plan the
store does not hold keeps today's behaviour: the status or runs line of the file is rewritten.

`item set <plan> <id> [--title T] [--description D] [--acceptance A] [--owned a,b] [--deps x,y]`
(STORE.3) changes one `plan_items` row of the plan's current version and prints
`{ok, plan, id, version, changed}` (`changed` maps each field whose value changed to its new value).
The plan file is not read or written; the plan row and every other row stay as they were. It
refuses, with exit 2 and nothing written: a plan that is not a draft, an unknown plan, a plan with
no stored items, an unknown id, a dep that is not an item of the same version, a self-dep and a dep
cycle. `--description` is stored in `extra_json` (empty clears it).

### Notes

- The plan text says import "refuses a non-`+++` file". The code does (`split_plan`), and it also
  refuses a `+++` file whose header does not parse or has no `plan` id, with exit 0 and `reason`
  rather than the usual exit 2.
- The plan text says `set <id> status=parked`. The code is `set <path> --status parked`: the
  argument is the plan file path, not the plan id, and the status is a flag.
- Re-import updates `title`, `header_json` and `body_md` only. It does not overwrite `status`,
  `created`, `design` or `integration` on a row that already exists (a conflicting upsert lists
  those four in the insert but not in the update set).

## Plan link (C4)

`mk-state.py` never imports `mk-plan.py`; it runs it as a subprocess (`$MK_PLAN_CMD` overrides the
command) and consumes its one JSON line (`parse`, `check`, `set --status`, `add-run`). Stored fields:
the run's `plan_id`/`plan_sha`/`plan_status` (plus the `runs.plan`/`plan_version` columns); phase
task `plan_phase`/`route`/
`lane_group`/`reviewer`; child task `plan_task` (its `parent` names the phase task). All `null` for
plan-less runs/tasks (a phase's `reviewer` is `""`, never `null`, when the plan has none).
The board of a plan-linked run shows `plan: \`id\` · status · sha \`…12\`` and, when the plan file's
current sha256 (plain read) differs from `plan_sha`, a `⚠ plan file changed since run start` line —
an unreadable plan file means no warning and no crash. Setting the **last** run of a plan `done`
runs `set --status done` on the plan and flips the run's `plan_status` (best-effort, never fatal; a
plan-tool error rides back as a `warning` in the `run set` JSON).
`run set --status done` also promotes: every task of this run at `merged` or `qa` moves to `done`,
one transition each, noted `promoted by closing the run`. The reply carries `promoted`, the list of
task ids this call moved (`[]` when there were none). The promotion is best-effort and never fails
the command: if one task raises midway, the tasks already moved stay moved and the reply also
carries `promote_error` (`"<Error>: <message>"`) — a key present only on that failure. Re-running
`run set --status done` on an already-done run promotes whatever is still at `merged` or `qa`, so
it repairs a partial promotion.
`task add` stays for ad-hoc tasks.

## Store mode

File mode is **dropped** (C4): `/mk setup` always creates the project DB, and `store status` answers
`{"store": "on"}` once it exists (`{"store": "off"}` only while the project DB is absent, which now
only a project that never ran `/mk setup` is). `store enable` is the legacy opt-in path kept for a
pre-store project: it creates the project store and imports every run from the legacy file views.
The importer (`scripts/mk-store-import.py`) lands them in dependency
order, so a `<run>:<id>` dep finds its target; deps that still cannot be resolved are reported in
`unresolved_deps` and do not abort the import. One run that fails to import aborts the whole
enable and the data dir is left exactly as it was — all or nothing. The reply is `{"store": "on",
"imported": "<run>" | ["<run>", …], "events": N, "tasks": N, "legacy": "<data>/runs-legacy"}`
(`imported` is the single id only when that id is the one active run, otherwise the list, in
dependency order). The task order of the imported run is kept: the tasks table inserts parents
before children, and the reader overlays those rows back onto the imported order. Before the
import, stale `mk-enable-*` directories in the data dir older than a minute are
swept (a directory younger than a minute, or one holding a live lock, is left alone). This is a
one-way import (the pre-migration file views are the source); **new** projects are created straight
into the DB and none of these file views is written again.

`export-legacy --run R` is removed with file mode: nothing rebuilds the old file views from the
tables any more.

`sync [--project SLUG]` calls `catch_up()`, which recomputes the root `plan_summary` rows from the
project store and writes only the rows that are missing or differ (a kill between the project commit
and the root write leaves them stale). It answers `{"store": "on", "project": "<slug>", "copied": N}`,
where `N` is the number of plan rows written; run again it writes nothing (`"copied": 0`). The
project defaults to `matrixkoder`. There is no root `cards` copy to catch up any more: `cards`,
`project_summary` and `sync` were dropped from the root (root v3, below) and the root's numbers come
from `plan_summary` (C3).

`run archive` first backs the project store up: `VACUUM INTO` a temp file
(`<data>/archive/<run>.db.tmp`), checked to hold the run and its task count, then renamed to
`<data>/archive/<run>.db`. The backup is written to the temp file first, and an existing backup is
never overwritten — when `<run>.db` is already there the new one lands beside it as
`<run>-<UTC timestamp>.db` (e.g. `r1-20261012T031854Z.db`). The backup happens before the status
commit: if it fails, the temp file is removed and the run stays active.

`plan sync` adds `version_diff` to its reply — the list of plan fields whose stored
version differs from the parsed file (`[]` when they match). The dry run still writes nothing.
`--apply` writes the new plan version into the `plans` table (version + 1) when there is a diff,
and the reply gains `version`, the stored version number.

## Data dir resolution

`<data>` resolves in this order: `--data` > `$MATRIXKODER_DATA` > the root `projects.data_dir` row
(by repo path) > the repo folder. The repo is the nearest
`.git` above the cwd, never `$HOME` itself, and a linked worktree maps to its main checkout.
Reading the root table checks that the root store (`mk-overview.db`, or `mk.db` on an existing home) exists first, so resolving never creates it.
Registering a project writes the root `projects` row (the single registry); the slug is checked
against `^[a-z0-9][a-z0-9-]{0,40}$` before either is touched, and a bad slug writes nothing. The
`db_path` column is the migrated marker: once set, `mkpaths.project_db(slug)` /
`project_folder` name the project folder, and `<data>` is only the project's legacy location.

## Migration (`mk-migrate.py`, C5)

`mk-migrate.py` is the ONE migration tool, and the only writer of `projects.db_path`. Default (no
flag): quiesce, copy, verify — which leaves the C2 copy in place and `projects.db_path` NULL, so
every reader keeps the old path. `--dry-run` prints every move and writes nothing.

- **quiesce** refuses unless no lane is armed/working and the project's bus inbox is drained.
- **copy** moves the WHOLE project DB (all runs stay queryable), `project.yaml`, the watch cursors
  and the live plans' `plan.md` + `assets` + `archive` + round files into the C2 tree. Only plans
  with an active run move their files; files of non-live plans live only in the `legacy/` tarball.
- **verify** re-counts and hashes under the switch lock; a mismatch removes the NEW copy ONLY (the
  old data is untouched) and refuses the switch.
- `--switch` repeats copy+verify under the lock, then sets `projects.db_path` and writes the switch
  record; a second switch refuses (use `--rollback`).
- `--rollback` refuses when the new DB has writes after the switch (or carries them back new→old
  with the same verify), then clears `db_path`.
- `--archive` (separate, only on the user's word) tars the old data dir into
  `projects/<slug>/legacy/`. **No command deletes the old folder** — the user does that.

The checklist after a switch: `mk-state` reads the new store, `mk-plan.py check`/`import` write the
plan folder, `mkdb.py plans`/`overview --json` answer, the dashboard's Plan and Settings tabs load,
and the C7 bus gate (the bus never breaks: `mk-bus-soak.py` sees no lost or duplicated seq, and the
bus e2e passes with one migrated and one pre-migration project) holds.


## SQLite stores (`scripts/mkdb.py`)

`mkdb.py` is the only module that opens a store read-write (research §8: a test greps `scripts/`
for `sqlite3.connect` and fails if any other script opens one without `mode=ro`). Every writer above
goes through it, and it is the ONE place a project-table write happens. Every self-test and caller
passes a temp home; the real `~/.matrixkoder` is never created by a test.

### Locations

| Store | Path | Holds |
|---|---|---|
| Root (C3) | `<home>/mk-overview.db` (`mk.db` on an existing home) | `projects` (with `db_path`) and `plan_summary`, the agent catalog tables (`agents`, `catalog_user`, `models`) and the user's `todos`. `cards`, `project_summary` and `sync` are dropped by root v3 when they hold no row |
| Project (C4) | `<home>/projects/<slug>/mk-project.db` (`mk.db` on an existing home) for a migrated project; a slug resolves it through `projects.db_path` (`mkpaths.project_db`). A `--data` (or pre-migration slug) opens the store in `<data>`, the legacy location | `plans`, `runs`, `tasks`, `events`, `artifacts`, `acks`, `lane_slots`, `waits`, `change_log`, `evidence`, `risk_reasons`, `reconcile_due`, `settings`, `watch_cursors` |

`<home>` is `--home` → `$MK_HOME` → `~/.matrixkoder`. `<data>` is `--data` / `data_dir`: it names
either the project folder or (pre-migration, and for `mk-migrate`'s rollback) the project's legacy
folder. The slug is checked against
`^[a-z0-9][a-z0-9-]{0,40}$` **before** any path is built from it. A home whose path contains
`Mobile Documents`, `CloudStorage`, `iCloud` or `Dropbox`, or starts with `/Volumes/`, is refused —
a path-string heuristic, so a custom sync root or a volume mounted elsewhere slips through
(`shortcut:` in `scripts/mkdb.py`).

### PRAGMAs and transactions

Every open sets `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`. Every write runs inside
`BEGIN IMMEDIATE`. WAL files (`-wal`, `-shm`) sit next to the db and must move with it (`VACUUM INTO`).

### `schema_version` and migrations

Both stores carry `schema_version(version INTEGER PRIMARY KEY)`. The project store is `SCHEMA = 7`;
the root store is `ROOT_SCHEMA = 3` (it migrates additively and independently). Migrations are an
ordered `(version, step)` list applied once. A step is either SQL or a Python callable. SQL steps
are idempotent by construction (`CREATE TABLE IF NOT EXISTS`, `INSERT OR IGNORE`); an
`ALTER TABLE ADD COLUMN` is not safe to run twice, so it goes through a guarded Python step that
checks `PRAGMA table_info` and adds the column only when it is missing. The step and its version
stamp are two transactions, so a crash between them re-runs the step. A store whose version is
newer than the code is refused:
`{"ok": false, "error": "store schema vN is newer …"}`, exit 3.

Root v2 (shipped, a guarded Python step) adds `projects.db_path` — last, matching the base
`CREATE TABLE`'s column order — and creates the `plan_summary` table empty. Additive only: a v1
root keeps every row it had (including its old `cards`) and reaches the same shape a fresh root gets,
and re-running the step changes nothing (the column is added only when `table_info` lacks it).

v2 (shipped, a Python step) adds `artifacts.src TEXT` — the importer's stable source key, NULL
on rows the live path writes — the index `art_run_src(run, src)`, and `event_src(run, src,
PRIMARY KEY(run, src))`, the same key for events so a repeat import is an indexed lookup. A v1
store is upgraded in place the next time it is opened and keeps every row it had; re-running the
step changes nothing.

v3 (shipped, a guarded Python step, the same `PRAGMA table_info` pattern as v2) adds
`lane_slots.workspace`, `workspace_id`, `branch`, `lock_hash` and `updated`, each `TEXT` and NULL
on rows written before the migration. A fresh store is created with those columns already in the
base `CREATE TABLE`, then the v2 and v3 steps run and stamp version 3. A v1 or v2 store is
upgraded in place on the next open and keeps every `lane_slots` row; opening it again changes
nothing. A crash after the columns are added and before the version stamp is repaired by the next
open, because each column is added only when it is missing.

v4 (shipped, a guarded Python step, the same `PRAGMA table_info` pattern as v3) adds
`plans.status`, `plans.created`, `plans.design` and `plans.integration`, each `TEXT` and NULL on
rows written before the migration. `design` and `integration` hold JSON array text. A fresh store
is created with those columns already on `plans`, then the v2, v3 and v4 steps run and stamp
version 4. A v1, v2 or v3 store is upgraded in place on the next open and keeps every `plans`
row; opening it again changes nothing.

v5 (shipped, a guarded Python step, the same `PRAGMA table_info` pattern as v4) adds
`runs.extra_json` and `tasks.extra_json`, each `TEXT` and NULL on rows written before the migration.
Additive only: an older store keeps every row and re-running the step changes nothing.

v6 (shipped) creates the empty tables `evidence`, `risk_reasons` and `reconcile_due` and their two
indexes (described under "Schema 6" above). No existing row changes.

v7 (shipped) creates the empty tables `settings(key TEXT PRIMARY KEY, value TEXT, updated TEXT)` and
`watch_cursors(key TEXT PRIMARY KEY, seq INTEGER NOT NULL, started TEXT, updated TEXT)`. No existing
row changes. A store older than v7 is upgraded in place on the next open.

Root v3 (shipped, L10.9, `_root_v3`) drops `cards`, `project_summary` and `sync` (the pre-linkage
copies of the project store; `ROOT_DROPPED_TABLES`) **only when the table holds no row**. A root that
still has data in one of them keeps it, and `root-cleanup` lists it as `keep`. A fresh root never
creates them. Re-running the step changes nothing.

### Project tables (C4)

`plans(id, version, title, status, created, design, integration, header_json, body_md,
approved_at, archived_path)`.

`runs(id TEXT PRIMARY KEY, plan, plan_version, branch, status, stage, next, started, ended)` —
`plan` is the plan **id** (C4); `status` is `active|done|archived`.

`tasks(run, id, parent, type, title, acceptance, owned, deps, parallel, route_json, status,
prev_status, file_status, lane, slot, branch, pane, round, verdict, updated, PRIMARY KEY(run, id))` — `type` is
`phase|task|subtask`; `prev_status` is the last non-attention status (NULL until the first one);
`file_status` is the published status (`planned` or `routed`) while `status` is the kanban column
`ready`; `deps` is one TEXT CSV of ids in the same plan, or `<run>:<id>` in the same project.
Validated on write: the dep must exist, a self-dep is refused, a cycle is refused with the path named
(`cycle: r:phase -> r:task -> r:phase`). `route_json` is `{"agent","model","effort"}` or empty.
Status rolls up subtask → task → phase through `transition()`: if any child is in an
attention status the parent takes that status, first match in the order `blocked`, `stalled`,
`quota`, `provider`, so the parent keeps its previous status through `prev_status` (it drives the
kanban column); when no child is in an attention status the parent returns to the least-done
status of its children's non-attention statuses. A parent already at the rolled
status is not transitioned again. The tasks table inserts parents before children (`_parents_first`);
the reader overlays the rows back onto the run's task order.

`events(id INTEGER PRIMARY KEY, run, task, t, chan, kind, from_id, to_id, reply_to, text,
artifact_id, meta_json)` with index `ev_task(run, task, id)`. `chan` is `state`, `bus`, `note` or
`inbox` (C5: an inbox row has `run` and `task` NULL; `meta_json` is `{file, plan, fit, by}`);
`kind` is the event type. Status changes go only through `transition()` (`chan='state'`,
`kind='transition'`); `append_event` refuses that pair.
`change_log(change_no INTEGER PRIMARY KEY, run, task, t)` — `change_no` is the rowid, gap-free.
`lane_slots(slot TEXT PRIMARY KEY, lane_path, state, run, task, workspace, workspace_id, branch, lock_hash, updated)` — the five trailing columns are v3, each `TEXT`, and are the slot record (with a project DB these rows ARE the lane registry and `lanes.json` is not written; file mode keeps the same record in `lanes.json`; `workspace` and `workspace_id` are null for a plain git worktree; `branch`, `lock_hash` and `updated` are null until the slot is acquired).
`waits(id INTEGER PRIMARY KEY, run, question, opened, cleared)` — the run's one live `waiting`
question. `acks(run, msg, lane, level, t, PRIMARY KEY(run, msg, lane))` — the bus's ack levels (C6).
`artifacts(id INTEGER PRIMARY KEY, run, task, round, kind, rev, text, created, src)` — `text` is
stored byte-for-byte; `src` is the v2 source key. A round's `brief`/`report`/`review` content is
stored here (and written to the task's folder as the round files) — no per-task file copy.
`event_src(run, src, PRIMARY KEY(run, src))` — the importer's stable source keys.
`settings(key TEXT PRIMARY KEY, value TEXT, updated TEXT)` (v7) — `key` maps to a JSON `value`; a
reset is a `DELETE` of the row. Written by `mk-config`.
`watch_cursors(key TEXT PRIMARY KEY, seq INTEGER NOT NULL, started TEXT, updated TEXT)` (v7) — the bus
watch cursors, keyed `<run or *>|<task or *>`. Written by `mk-bus.py`; `watch-cursors.json` is no
longer written (read once as the import source).


### Root tables (C3)

`projects(slug TEXT PRIMARY KEY, name, repo, data_dir, mode, created, last_activity,
db_path)` — `db_path` is new; **set = migrated**. `plan_summary(project, plan, title, status,
counts_json, live_run, updated, PRIMARY KEY(project, plan))` — new; one row per plan with the
numbers (`counts_json`: the task counts by status) and the linkage (`live_run`), refreshed by every
writer. The root holds **no task row and no event**: it carries the overview numbers, the linkage,
the agent catalog tables and the user's to-dos.

`todos(project TEXT NOT NULL, id INTEGER NOT NULL, text TEXT NOT NULL, done INTEGER NOT NULL
DEFAULT 0, created TEXT, PRIMARY KEY(project, id))` — one row per to-do. The write verbs create it
with `CREATE TABLE IF NOT EXISTS` (the way `mk-catalog.py` creates its tables); the root schema
number stays 3, so nothing that exists is altered. `id` is the project's `MAX(id)+1`, taken inside
one short `BEGIN IMMEDIATE`. `root-cleanup` neither lists nor removes it.

The root has no card table: `cards`, `project_summary` and `sync(project PRIMARY KEY,
last_change_no)` were the pre-linkage copies of the project store and root v3 drops them when empty
(see the migrations above). The Kanban column rules still apply to the project store's task status.
`col` is derived from `status`: Backlog (`planned`, `routed`) · Ready · Building (`dispatched`,
`building`, `takeover`) · Verify (`verifying`) · Review · Fix · Done (`merged`, `qa`, `done`); an
attention status (`blocked`, `stalled`, `quota`, `provider`) keeps the column of the task's previous
non-attention status — `tasks.prev_status`, written by `transition()` — and sets `attention=1`.
A task with no previous non-attention status uses Backlog. `column_of(status, prev_status=None)`
is pure; `transition()` and the rollup do the lookup. Only `plan_summary`, `projects.db_path`, the catalog tables and `todos`
are the root's content.

### `transition(home, project, run, task, to, note="", round=0, sync_root=False, data_dir=None, verdict=<unchanged>)`

The only status writer (C10). `verdict` is optional: omitted, the task's stored verdict is left
unchanged; a string or `None` replaces it. One project-store transaction updates the task row, appends an
`events` row (`chan='state'`, `kind='transition'`) and a `change_log` row, and returns `change_no`.
A failure inside it leaves nothing. The root `plan_summary` upsert is a **second**
transaction and run only when `sync_root=True`; with `sync_root=False` the `plan_summary` refresh
lands in its own root transaction right after the project commit, so the two can never interleave.
A crash between them leaves the project change durable and the root behind; `catch_up()` recomputes
the `plan_summary` rows and writes the ones that differ.

### API and CLI

API: `open_root(home)`, `open_project(home, slug=None, data_dir=None)`,
`add_task(con, run, task, type, title="", parent=None, deps="", route_json=None, status="planned")`,
`transition(home, project, run, task, to, note="", round=0, sync_root=False, data_dir=None)`,
`catch_up(home, project, data_dir=None)`, `timeline(home, project, run, task, data_dir=None)`,
`overview(home)` (the root's projects + plans: numbers and `db_path`,
read-only, never creates the root), `todo_list/add/toggle/delete/restore/clear_done(home, project, …)`, `check_slug`, `check_home`, `column_of(status, prev_status=None)`,
`import_history(home, project, run, *, run_row=None, tasks=(), events=(), artifacts=(), data_dir=None)`.

### `root-cleanup` and `restore`

The root home can carry tables and files that later layouts replaced (`cards`, `project_summary`,
`sync`, and `event_src`, which leaked into one real root). `mkdb.py root-cleanup` lists them:

- Default (or `--dry-run`): a read-only dry run. It opens the root DB read-only, creates and changes
  nothing, and prints the `remove`, `merge` and `keep` lists. A table that holds rows is `keep`.
- `--apply --backup-dir D`: first makes a backup under `D/root-cleanup-<stamp>/` — a copy of the root DB
  (kept under its source name, `mk-overview.db` or `mk.db`) through the SQLite backup API plus every removable file, with a sha256 manifest — and verifies it
  before anything is removed. Then it removes exactly the listed items. `--apply` without
  `--backup-dir` is refused, and `--dry-run` with `--apply` is refused.
- `mkdb.py restore --from <backup>` puts the root DB and every removed file back from a
  `root-cleanup-<stamp>` folder. It saves the current DB first.

### Live writers (`append_event`, `append_ack`, `put_artifact`)

Three helpers, each one `BEGIN IMMEDIATE` transaction on the project store. They validate with
`check_slug` (through `open_project`) and refuse an unknown run or task with `Fail`. None of them
writes `tasks.status`. `change_log` follows `import_history`: a row is written only when a task
row changes, and these helpers never change one, so they write no `change_log` row.

`append_event(home, project, run, *, task=None, chan, kind, from_id=None, to_id=None, reply_to=None, text="", meta=None, artifact_id=None, t=None, data_dir=None) -> int`
inserts one `events` row and returns its id (the bus SSE `Last-Event-ID`). `t` defaults to now;
a `meta` dict is stored as `meta_json`. `chan` must be `state`, `bus` or `note`. `chan='state'`
with `kind='transition'` is refused — status changes go through `transition()` only (C10). An
`artifact_id` that is not an artifact of this run is refused. `task=None` is a run-level event.

`append_ack(home, project, run, msg, lane, level, *, t=None, data_dir=None) -> bool` inserts into
`acks(run, msg, lane, level, t)` or leaves the row alone. The level never decreases: an equal or
lower level is a no-op, a higher level upgrades the row. Returns True only when a row was inserted
or upgraded.

`put_artifact(home, project, run, task, round, kind, text, *, event_id=None, data_dir=None) -> {"id", "rev"}`
inserts a live artifact with `src` NULL. `rev` is 1 plus the highest existing rev for
`(run, task, round, kind)`, or 1 when none. When `event_id` is given, `events.artifact_id` on that
event is set in the same transaction; a failure after the insert rolls both back. Text of 256 KiB
is accepted; a larger body is refused.


```
mkdb.py timeline --project P --run R --task T --json [--data DIR] [--home DIR]
mkdb.py overview --json [--home DIR]
mkdb.py import-history --project P --run R --json-file F [--data DIR] [--home DIR]
mkdb.py plans --json [--data DIR] [--home DIR]
mkdb.py plans <id> --json [--data DIR] [--home DIR]
mkdb.py todo list --project P [--filter all|open|done] [--home DIR]
mkdb.py todo add --project P --text T [--home DIR]
mkdb.py todo toggle --project P --id N [--home DIR]
mkdb.py todo delete --project P --id N [--home DIR]
mkdb.py todo restore --project P --id N --text T --done 0|1 --created C [--home DIR]
mkdb.py todo clear-done --project P [--home DIR]
mkdb.py --self-test
```


`plans --json` lists every plan. Each object has `id`, `title`, `status`, `created`, `design`,
`integration`, `version`, `approved_at`, `runs`, `live_run` and `counts`. `live_run` is the id of
the plan's live run, or null. `counts` is the task counts by status. `plans <id> --json` is that
same object plus `phases`: each phase with its `tasks`, taken from the latest run (`tasks` joined
to `runs` on `runs.plan = plans.id`), or from the `[[phase]]` / `[[phase.task]]` rows in
`header_json` when the plan has no run (status `planned`). Each phase is `{id, title, wave, deps,
status, tasks}`, each task `{id, title, status, parent, type, round, verdict}`. The plan, each phase and each task carry an optional `description`, omitted when empty. An unknown id
answers `ok: false` and exits 2. Both reads refuse a store still at v3 ("needs migration to v4;
run a writer command"): any writer, such as `mk-plan.py import`, migrates it first.


`todo` verbs (the `todos` table above), one JSON line each. `list` is read-only: an absent root file or
table answers `{"ok": true, "todos": [], "open": 0, "done": 0}` and creates nothing; otherwise it returns
`{"ok": true, "todos": [{"id", "text", "done" (bool), "created"}, …], "open": N, "done": N}`, newest
first (`id` DESC), narrowed by `--filter` (the counts always cover every row of the project). `add`,
`toggle` and `restore` return `{"ok": true, "todo": {…}}`; `delete` returns the removed row the same
way; `clear-done` returns `{"ok": true, "cleared": N}`. `restore` puts a row back with its old id,
text, done flag and created time (an id that already exists is refused). Text is stripped of control
characters (tab and newline too), must not be empty and is capped at 500 characters (longer is
refused); an unknown id, a bad project slug or a bad filter is `ok: false`, exit 2. Every statement is
parameterised, so SQL metacharacters in the text are stored verbatim.

`timeline` is `SELECT … FROM events WHERE run=? AND task=? ORDER BY id`. `overview` reads the root's `projects` + `plan_summary` (C3).
`mk-catalog.py` follows the same rule for the root store: its read commands (`list`, `show`, `routes`, `model`) go through `read_catalog`, which returns an empty result when the root store is absent (`list` and `routes` answer `{"ok": true, …: []}`) and never creates it, while the writers `seed` and `set` call `open_catalog` and do create it (`set` creates it even when the agent is unknown and the command then refuses). One JSON line per call; errors exit 2, a newer store exits 3.

### `import_history(home, project, run, *, run_row=None, tasks=(), events=(), artifacts=(), data_dir=None)`

The one write path for historical rows, so the importer never opens SQLite itself. The whole call
is one `BEGIN IMMEDIATE` transaction; any error rolls it back and the store is unchanged.

- `run_row` is a dict of the `runs` columns (`id plan plan_version branch status stage next
  started ended`) and is upserted by `id`, which must equal `run`.
- `tasks` are dicts of the `tasks` columns the importer has (`id parent type title acceptance
  owned deps parallel route_json status lane slot branch pane round verdict updated`). They are
  inserted with their original `status`, `round`, `verdict` and `updated`, parents before
  children. `prev_status` is set from the imported status: a column status keeps itself, an
  attention status (`blocked`, `stalled`, `quota`, `provider`) gets NULL, so `column_of` places it
  in Backlog — the same helper `transition()` uses, not a second copy. The C3 dep rules apply to
  the whole batch: a dep may name a task later in the same batch, and a cycle is refused with the
  path named.
- `events` are dicts `t chan kind from_id to_id reply_to text meta_json task` plus `src`, the
  stable source key. They are stored with the original `t`, `chan` and `kind` — no `now()`, no
  forced `kind='transition'` — in the order given, so ids ascend with the input. `src` is written
  into `meta_json` as `"src"` and recorded in `event_src`. The importer counts a blank line in a
  legacy event log (and a malformed line, and a line whose kind is not a state kind) in
  `skipped_events`, with the line number and reason in `skipped_event_reasons`; a blank line is
  not imported.
- `artifacts` are dicts `task round kind rev text created` plus `src`. `text` is stored
  byte-for-byte and `src` goes in the `artifacts.src` column.

Idempotent: an event or artifact whose `src` is already stored is skipped, and tasks and the run
row upsert, so the same call twice adds nothing. Each task actually added gets one `change_log`
row, and the `plan_summary` rows are refreshed after
the commit (in their own root transaction). It returns `{runs, tasks, events, artifacts}`, the
counts of rows actually added.

```
mkdb.py import-history --project P --run R --json-file F [--data DIR] [--home DIR]
```

`--json-file` is a JSON object with the same four keys (`run_row`, `tasks`, `events`,
`artifacts`). The command prints one JSON line: `{"ok": true, "runs": N, "tasks": N, "events": N,
"artifacts": N}`.
