# MatrixKoder run-state schema (v1)

Contract between `scripts/mk-state.py` (the **only writer**), `lane-watch.py`, `mk-usage.py` and the
dashboard mod (TypeScript, read-only). Field names are snake_case and stable; adding fields is
allowed, renaming or removing one needs `schema` bumped.

## Locations

| What | Path |
|---|---|
| Project data dir | `--data DIR` → `$MATRIXKODER_DATA` → `<git toplevel of cwd>/.matrixkoder` (the nearest `.git`, so inside a nested repo pass `--data`) |
| Global dir | `$MATRIXKODER_HOME` → `~/.matrixkoder` |
| Run index | `<data>/runs/index.json` |
| Run snapshot | `<data>/runs/<run_id>/run.json` — written atomically (temp file + `os.replace`); **the dashboard reads only this** |
| Event log | `<data>/runs/<run_id>/events.jsonl` — append-only (`O_APPEND`), one JSON object per line |
| Board | `<data>/runs/<run_id>/board.md` — regenerated on every write and by `render` |
| Task | `<data>/runs/<run_id>/tasks/<task_id>/task.json` (copy of the task object in `run.json`) |
| Round files | same folder: `brief-rN.md`, `report-rN.md`, `review-rN.md` (paths returned by `task round`; written by the orchestrator / agents, not by mk-state) |
| Locks | `<home>/locks/<resource>.lock` |
| Usage ledger | `<home>/usage.json` (owned by `mk-usage.py`; lane-health only adds `exhausted_*` fields, see below) |

IDs (run, task, lock resource): `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. Timestamps: ISO 8601 local time
with offset, seconds precision (`2026-10-07T20:09:01+02:00`). Unknown values are `null`, never guessed.

## `runs/index.json`

```json
[{ "id": "mk-p1", "feature": "MatrixKoder P1", "status": "active", "started": "2026-10-07T20:09:00+02:00", "ended": null }]
```

`status`: `active | done | archived`. Sorted by `started`. Archive is a flag; the folder never moves.

## `run.json`

```json
{
  "schema": 1,
  "id": "mk-p1",
  "feature": "MatrixKoder P1",
  "branch": "main",
  "plan": ".matrixkoder/plans/matrixkoder.md",
  "status": "active",
  "stage": 5,
  "next": "watch P1",
  "waiting": { "question": "Merge P1 now?", "options": ["yes", "later"], "rec": "yes", "since": "2026-10-07T20:09:01+02:00" },
  "started": "2026-10-07T20:09:00+02:00",
  "ended": null,
  "updated": "2026-10-07T20:09:01+02:00",
  "tasks": [
    {
      "id": "P1", "title": "State core", "wave": 1, "deps": [], "owned": ["scripts/mk-state.py", "scripts/lane-*.py"],
      "status": "building", "agent": "codex", "model": "gpt-5.5", "effort": "high", "lane": "../wt-p1",
      "last_commit": null, "gates": null, "verdict": null,
      "round": 1,
      "rounds": [{ "n": 1, "kind": "build", "cause": null, "verdict": null, "sha": null, "started": "2026-10-07T20:09:00+02:00" }],
      "created": "2026-10-07T20:09:00+02:00", "updated": "2026-10-07T20:09:00+02:00"
    }
  ],
  "recent": [
    { "t": "2026-10-07T20:09:01+02:00", "run": "mk-p1", "task": "P1", "type": "decision", "text": "Jev: codex high", "from": null, "to": null, "by": "orchestrator" }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `schema` | int | `1` |
| `status` | `active \| done \| archived` | `ended` is set when it leaves `active` |
| `stage` | int | loop stage 0–12 (SKILL.md) |
| `next` | string \| null | next action, shown in the dashboard header and resume section |
| `waiting` | object \| null | the one question waiting on the user; `options` list, `rec` recommendation |
| `tasks` | array | task objects in insertion order (below) |
| `recent` | array | last 10 events, same shape as `events.jsonl` lines |
| `updated` | ts | last write |

### Task object

| Field | Type | Meaning |
|---|---|---|
| `id`, `title` | string | |
| `wave` | int \| null | |
| `deps`, `owned` | string[] | task ids it waits on; file globs it owns |
| `status` | enum | `planned → routed → dispatched → building → verifying → review → fix → merged → qa → done`; side states `blocked \| quota \| stalled \| provider` |
| `agent` | string \| null | agent kind or pane (`codex`, `agy`, ...) |
| `model`, `effort`, `lane` | string \| null | `lane` = worktree path |
| `last_commit` | string \| null | short sha; also copied to the current round's `sha` |
| `gates` | any JSON \| null | e.g. `{"test": "pass", "tsc": "pass"}` (pass a JSON value to `task set`) |
| `verdict` | string \| null | latest review verdict; also copied to the current round's `verdict` |
| `round` | int | current round number (0 = none yet) |
| `rounds` | array | `{n, kind: build\|fix\|takeover, cause, verdict, sha, started}` |

## `events.jsonl` line

```json
{"t": "2026-10-07T20:09:00+02:00", "run": "mk-p1", "task": "P1", "type": "transition", "text": "lane-watch STALL: agent idle, no change 11 min", "from": "building", "to": "stalled", "by": "lane-watch"}
```

`type`: `transition | decision | verdict | quota | note`. `task` is `null` for run-level events.
`from`/`to` are task statuses (`from` is `null` when a task is added). `by` = writer (`orchestrator`,
`lane-watch`, ...; `--by` flag). `text` carries the evidence (plan §4 called it `evidence`).
`task set status=...` appends the transition event itself.

## Lock file `<home>/locks/<resource>.lock`

```json
{"resource": "dev-server", "reason": "QA on :3000", "scope": ["restart", "deploy"], "by": "orchestrator", "pid": 97433, "host": "Mac.lan", "taken_at": "2026-10-07T20:09:01+02:00", "expires_at": "2026-10-07T20:39:01+02:00"}
```

Created with an exclusive hard link (never half-written). `lock check` → `free | held | stale`
(`stale` = past `expires_at`). `lock take` on a held or stale lock fails (exit 1) and returns the
current holder; `--force-stale` replaces a stale lock only, never a held one.

## Usage ledger additions (lane-health QUOTA fallback)

lane-health writes into `<home>/usage.json` → `agents.<kind>` (`--kind claude|codex|opencode|agy`;
pane name if no kind). It never touches CodexBar fields; it sets `source: "pane-text"` only when the
entry did not exist yet.

```json
{"agents": {"agy": {"source": "codexbar", "windows": ["…CodexBar data, untouched…"],
  "exhausted_until": "2026-10-13T18:43:00+02:00", "exhausted_window": "weekly", "exhausted_source": "pane-text",
  "exhausted_observed_at": "2026-10-07T12:00:00+02:00", "exhausted_evidence": ["quota reached", "Resets in 154h43m"]}}}
```

`exhausted_window`: `5h | weekly | null` — weekly when the reset is more than 6 h ahead (or the
message says "week" and has no reset time). `exhausted_until: null` = reset time unknown. Readers
treat the agent as available again once `exhausted_until` has passed.

## CLI output

Every `mk-state.py` command prints exactly one JSON line: `{"ok": true, ...}` exit 0;
`{"ok": false, "error": "..."}` exit 2 on bad input; lock refused → exit 1 with `state` and `lock`.
`task round` returns `{round, kind, brief, report, review}` absolute paths for that round.

lane-watch exit → task status (with `--task --data`): `DONE`/`DONE_SENT → verifying`,
`QUOTA → quota`, `STALL → stalled`, `PROVIDER → provider`, `BLOCKED → blocked`; `DEV_SERVER`,
`ERROR`, `WATCH_EXPIRED` only append a `note` event.
