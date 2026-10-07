# MatrixKoder run-state schema (v1)

Contract between `scripts/mk-state.py` (the **only writer**), `lane-watch.py`, `mk-usage.py` and the
dashboard mod (TypeScript, read-only). Field names are snake_case and stable; adding fields is
allowed, renaming or removing one needs `schema` bumped.

## Locations

| What | Path |
|---|---|
| Project data dir | `--data DIR` → `$MATRIXKODER_DATA` → `<toplevel>/.matrixkoder`, toplevel = the nearest `.git` above cwd; a linked worktree (`.git` file with `commondir`) resolves to its **main checkout**, a nested repo to itself (pass `--data` to reach the outer repo) |
| Global dir | `$MATRIXKODER_HOME` → `~/.matrixkoder` |
| Run index | `<data>/runs/index.json` |
| Run snapshot | `<data>/runs/<run_id>/run.json` — written atomically (temp file + `os.replace`); **the dashboard reads only this** |
| Event log | `<data>/runs/<run_id>/events.jsonl` — append-only (`O_APPEND`), one JSON object per line |
| Board | `<data>/runs/<run_id>/board.md` — regenerated on every write and by `render` |
| Task | `<data>/runs/<run_id>/tasks/<task_id>/task.json` (copy of the task object in `run.json`) |
| Round files | same folder: `brief-rN.md`, `report-rN.md`, `review-rN.md` (paths returned by `task round`; written by the orchestrator / agents, not by mk-state) |
| Locks | `<home>/locks/<resource lower-cased>.lock` |
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
| `status` | `active \| done \| archived` | `ended` is set when it leaves `active`, cleared when it returns. `run set --status` takes `active\|done`; `archived` only via `run archive` |
| `stage` | int | loop stage 0–12 (SKILL.md) |
| `next` | string \| null | next action, shown in the dashboard header and resume section |
| `waiting` | object \| null | the one question waiting on the user: `question`, `options` (string[]), `rec` (string \| null), `since` (ts) |
| `tasks` | array | task objects in insertion order (below) |
| `recent` | array | last 10 events, same shape as `events.jsonl` lines |
| `updated` | ts | last write |

### Task object

| Field | Type | Meaning |
|---|---|---|
| `id`, `title` | string | `id` unique case-insensitively within the run (`T1` and `t1` clash) |
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
`task set status=...` appends the transition event itself. `text` must be non-empty; a `transition`
event needs `to`. Readers skip a line that is not valid JSON (a crash mid-append); `run.json` is
authoritative, so `events.jsonl` may hold one event more than `recent` after a crash.

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
pane name if no kind). It never touches CodexBar fields; it sets `source: "pane-text"` only when the
entry did not exist yet.

```json
{"agents": {"agy": {"source": "codexbar", "windows": ["…CodexBar data, untouched…"],
  "exhausted_until": "2026-10-13T18:43:00+02:00", "exhausted_window": "weekly", "exhausted_guess": false,
  "exhausted_source": "pane-text", "exhausted_observed_at": "2026-10-07T12:00:00+02:00",
  "exhausted_evidence": ["Individual quota reached"]}}}
```

| Field | Meaning |
|---|---|
| `exhausted_until` | ts, never null when written. From the message ("reset at <date time>", "resets in 1h 30m / 45m / 2d 3h", "try again in 2 hours", "resets 3pm"); none printed → `observed_at + 5h` with `exhausted_guess: true` |
| `exhausted_window` | `5h \| weekly \| null` — weekly when the reset is more than 6 h ahead (or the message says "week") |
| `exhausted_guess` | true when `exhausted_until` is the +5h assumption. A later hit without a reset time never replaces a known future `exhausted_until` |
| `exhausted_source` / `_observed_at` / `_evidence` | `"pane-text"`, when, the matched phrases (max 2) |

**Contract with `mk-usage.py` (P2):** it keeps every `exhausted_*` field when it rewrites an agent
entry, and clears them (sets `exhausted_until` to `null`, may drop the rest) once `exhausted_until` has
passed. Readers: an agent is exhausted while `exhausted_until` is non-null and in the future; null or
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

- `pools.<name>` = `{five_hour, weekly, exhausted_until}`; `exhausted_until` = latest reset among that pool's windows at >= 100%, else null.
- Agent-level `five_hour` / `weekly` = the primary pool (agy `gemini`, others `default`); if that pool is absent, the first pool present.
- Agent-level `exhausted_until` = the primary pool's `exhausted_until` (claude `default`, agy `gemini`). Another pool being free does not clear it: claude's `fable` is a sub-scope, so a 100% `default` pool makes Claude exhausted. `available_pools` lists every pool not exhausted — a router wanting a secondary pool (agy `claude-gpt`, claude `fable`) checks it.
- A future pane-text `exhausted_until` from lane-health overrides the pools (pane text is newer evidence); the later of the two wins.

## CLI

| Command | Flags |
|---|---|
| `run init` | `--id ID --feature F --branch B [--plan PATH]` |
| `run list` / `run get` | `run get [--id ID \| ID]` (default: the single active run) |
| `run set` | `[--id ID] [--next TEXT] [--status active\|done]` (at least one) |
| `run stage` / `run archive` | `run stage N` (N >= 0, `[--id ID]`); `run archive --id ID` or `run archive ID` |
| `task add` | `--id T --title TEXT [--wave N] [--deps A,B] [--owned glob,glob]` |
| `task set` | `T key=value ... [--note TEXT]`; keys `status agent model effort lane last_commit gates verdict`; a value starting `{`/`[` is parsed as JSON, empty = null |
| `task round` | `T --kind build\|fix\|takeover [--cause TEXT]` |
| `task get` | `T` |
| `event` | `[--task T] --type transition\|decision\|verdict\|quota\|note --text TEXT [--from S] [--to S]` |
| `wait set` / `wait clear` | `--question TEXT --options a,b [--rec TEXT]` |
| `lock take` / `release` / `check` | `RES --reason TEXT [--ttl-min 30] [--scope a,b] [--force-stale]`; `release RES [--force]` |
| `render` | regenerate `board.md` |

Common flags: `--data DIR`, `--by WHO` (default `orchestrator`; written to events and locks), `--run ID`
for task/event/wait/render (`run *` commands take `--id`, alias `--run`).

### Output

Every `mk-state.py` command prints exactly one JSON line: `{"ok": true, ...}` exit 0;
`{"ok": false, "error": "..."}` exit 2 on bad input or a corrupt/unreadable state file; lock refused →
exit 1 with `state` and `lock`.
`task round` returns `{round, kind, brief, report, review}` absolute paths for that round.

lane-watch exit → task status (with `--task --data`, both required together): `DONE`/`DONE_SENT → verifying`,
`QUOTA → quota`, `STALL → stalled`, `PROVIDER → provider`, `BLOCKED → blocked` — only while the task is
`dispatched|building|stalled|quota|provider|blocked`; otherwise the status is kept and a `note` event is
logged. `DEV_SERVER`, `ERROR`, `WATCH_EXPIRED` only append a `note` event.
