# MatrixKoder run-state schema (v1)

Contract between `scripts/mk-state.py` (the **only writer**), `lane-watch.py`, `mk-usage.py` and the
dashboard mod (TypeScript, read-only). Field names are snake_case and stable; adding fields is
allowed, renaming or removing one needs `schema` bumped.

## Locations

| What | Path |
|---|---|
| Project data dir | `--data DIR` → `$MATRIXKODER_DATA` → the repo's entry in `<global>/projects.json` (`{ "<repo path>": { "data_dir": "..." } }`, written by the onboarding wizard's "home" mode; keyed by the **main checkout** path; `~` allowed, a relative path is resolved against the repo) → `<toplevel>/.matrixkoder`. toplevel = the nearest `.git` above cwd, never `$HOME` itself (a dotfiles repo is not a project); a linked worktree (`.git` file with `commondir`) resolves to its **main checkout**, a nested repo to itself (pass `--data` to reach the outer repo). One order in `scripts/mk-state.py`, `hooks/dashboard.tsx` and `hooks/wake.ts`; the pure helpers are in `hooks/data.ts` (`projectDataDir`) |
| Global dir | `$MATRIXKODER_HOME` → `~/.matrixkoder` (TS: `globalDirPath` in `hooks/data.ts`; the wizard writes `projects.json` here — plugin prefs live in the plugin's userConfig, not in this dir) |
| Run index | `<data>/runs/index.json` |
| Run snapshot | `<data>/runs/<run_id>/run.json` — written atomically (temp file + `os.replace`); **the dashboard reads only this** |
| Event log | `<data>/runs/<run_id>/events.jsonl` — append-only (`O_APPEND`), one JSON object per line |
| Board | `<data>/runs/<run_id>/board.md` — regenerated on every write and by `render` |
| Lane registry | `<data>/lanes.json` — written **only** by `mk-lane.py` (see below); `mk-state.py` never touches it |
| Task | `<data>/runs/<run_id>/tasks/<task_id>/task.json` (copy of the task object in `run.json`) |
| Round files | same folder: `brief-rN.md`, `report-rN.md`, `review-rN.md` (paths returned by `task round`; written by the orchestrator / agents, not by mk-state; `task round --undo` refuses while any of them exists for the latest round) |
| Locks | `<home>/locks/<resource lower-cased>.lock` |
| Usage ledger | `<home>/usage.json` (owned by `mk-usage.py`; lane-health only adds `exhausted_*` fields, on the agent or on one pool, see below) |
| Usage ledger lock | `<home>/usage.json.lock` — an empty sidecar every read-modify-write of `usage.json` takes as an exclusive `fcntl.flock` (`mk-usage.py`'s poll and lane-health's quota mark; the two would otherwise lose each other's fields). It holds no data, and it is derived from `<home>` rather than from the ledger path, so writers pointed at different `--out`/`--ledger` paths still serialise |

IDs (run, task, lock resource): `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. Timestamps: ISO 8601 local time
with offset, seconds precision (`2026-10-07T20:09:01+02:00`). Unknown values are `null`, never guessed.

## `runs/index.json`

```json
[{ "id": "mk-p1", "feature": "MatrixKoder P1", "status": "active", "started": "2026-10-07T20:09:00+02:00", "ended": null, "plan": "plan-link" }]
```

`status`: `active | done | archived`. Sorted by `started`. Archive is a flag; the folder never moves.
`plan` (C4): the plan id of the run this entry was built from (`run init --plan`); `null` for plan-less
and legacy path-only runs.

## `run.json`

```json
{
  "schema": 1,
  "id": "mk-p1",
  "feature": "MatrixKoder P1",
  "branch": "main",
  "head_sha": null,
  "plan": ".matrixkoder/plans/matrixkoder.md",
  "plan_id": "plan-link",
  "plan_sha": "9f2c7ab1c8...",
  "plan_status": "running",
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
      "pane": "wK:p1",
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
| `head_sha` | string \| null | optional (design §7.2): the run branch's current commit; the dashboard shows the branch only while absent |
| `plan` | string \| null | the plan file path, as passed to `run init --plan` (legacy runs store only this) |
| `plan_id`, `plan_sha`, `plan_status` | string \| null | **C4 plan link**: the plan's id; the sha256 of the plan file bytes taken from a final `mk-plan.py parse` **after** the `set --status running` + `add-run` mutations (so the board's ⚠ does not fire at birth); the plan status then (`running`). `plan sync --apply` refreshes `plan_sha`/`plan_status`; the last plan-linked run set `done` flips `plan_status` to `done` (best-effort). `null` on plan-less runs |
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
| `deps`, `owned` | string[] | tasks it waits on (`T` in this run, `RUN:T` in another run of the same data dir); file globs it owns. Stored in the target's stored id case (ids match case-insensitively; a self-dep is refused at add). `task set status=dispatched\|building` fails while a dep of the task **or of any ancestor** is not `merged`/`qa`/`done` (`--force` overrides) |
| `parent` | string \| null | parent task id (same run); `--parent` onto a task in `merged`/`qa`/`done` is refused. The parent's status rolls up from its children: a side state if any child is in one (`blocked` > `provider` > `quota` > `stalled`), else the least advanced child, raised to `building` only when at least one child is past `routed` and the least advanced child is below `building` (parents are never lowered); manual `status=` on a task with children **fans out**: every child is set to it (one transition event each, the same `--note`), and the parent itself is never written directly |
| `ready` | bool | written by mk-state: has deps, `planned`/`routed`, every dep met. A flip to true logs a `deps met` note; the dashboard toasts it |
| `status` | enum | `planned → routed → dispatched → building → verifying → review → fix → merged → qa → done`; side states `blocked \| quota \| stalled \| provider`. Input aliases, stored as the canonical word (also on `event --from`/`--to`): `reviewing`→`review`, `merging`→`merged`, `fixing`→`fix` |
| `agent` | string \| null | agent kind or pane (`codex`, `agy`, ...) |
| `model`, `effort`, `lane`, `pane` | string \| null | `lane` = worktree (slot) path; `pane` = herdr pane id of that lane (`wK:p1`), written by `mk-lane-event.py arm` (`mk-state task set <T> pane=<pane>`, best-effort) |
| `branch`, `workspace`, `slot` | string \| null | the lane's own names, written by `mk-lane.py acquire`: `branch` = `mk/<run>/<task>`, `workspace` = herdr workspace label `mk:<repo>:<n>`, `slot` = slot number as a string. Empty value clears (`branch=`), like `lane=`/`pane=`; the board shows all three on the lane cell when set |
| `last_commit` | string \| null | short sha; also copied to the current round's `sha` |
| `gates` | any JSON \| null | e.g. `{"test": "pass", "tsc": "pass"}` (pass a JSON value to `task set`) |
| `verdict` | string \| null | latest review verdict; also copied to the current round's `verdict` |
| `round` | int | current round number (0 = none yet) |
| `rounds` | array | `{n, kind: build\|fix\|takeover, cause, verdict, sha, started}` |
| `plan_phase`, `route`, `lane_group`, `reviewer` | string \| null | **C4**: set on a *phase* task created by `run init --plan` — the plan phase id, its pinned `route` (`""` = Jev decides, stored `null`), its `lane_group` (phases sharing a value share one worktree), and the phase's `reviewer` (`""` = orchestrator decides, stored `""` — the plan's value or `""` when absent; `task set reviewer=` rewrites it). `null` on ad-hoc tasks (`task add`) and children; the `reviewer` key is absent there until `task set` adds it || `plan_task` | string \| null | **C4**: set on a *child* task created from a `[[phase.task]]` — the plan task id (`P1.1`); its `parent` is the phase task. `null` on ad-hoc and phase tasks |

## `events.jsonl` line

```json
{"t": "2026-10-07T20:09:00+02:00", "run": "mk-p1", "task": "P1", "type": "transition", "text": "lane-watch STALL: agent idle, no change 11 min", "from": "building", "to": "stalled", "by": "lane-watch"}
```

`type`: `transition | decision | verdict | quota | note`. `task` is `null` for run-level events.
`from`/`to` are task statuses (`from` is `null` when a task is added; the status aliases resolve here
too, the canonical word is stored). `by` = writer (`orchestrator`,
`lane-watch`, ...; `--by` flag). `text` carries the evidence (plan §4 called it `evidence`).
`task set status=...` appends the transition event itself. Every `--force` override of a safety gate
writes exactly one `decision` event `forced by <by>: <command summary>` (`by` = the `--by` flag,
default `orchestrator`): `task set` past unmet own+ancestor deps, `run init --plan` past an unapproved
plan status. `text` must be non-empty; a `transition`
event needs `to`. Readers skip a line that is not valid JSON (a crash mid-append); `run.json` is
authoritative, so `events.jsonl` may hold one event more than `recent` after a crash.

## `lanes.json` (slot registry, `scripts/mk-lane.py`)

Lives outside `runs/`: it describes the reusable **worktree slots** of this project, not a run.
`mk-lane.py` is its only writer and mirrors the lane it hands out onto the task (`lane`, `branch`,
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
a crash between creating the worktree and writing `lanes.json` leaves a worktree the registry does not
know (delete the directory and `git worktree prune`, then acquire again — the create path refuses an
existing directory); `run`/`task` are stored as typed while the branch is lowercased, so `T1` and `t1`
map to one branch; and a **hand-edited** `lanes.json` pointing a slot at another worktree of the same
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
| `run init` | `--id ID --feature F --branch B [--plan PATH --force]` — with `--plan`: the plan tool (`mk-plan.py`, or `$MK_PLAN_CMD`) `check` must pass and its status be `approved` (refused before any local write); `--force` overrides an unapproved status and writes a `decision` event naming `--by`. The plan then gets `set --status running` + `add-run --run ID`, the run stores `plan_id`/`plan_sha`/`plan_status`, and phase + child tasks are created through the `task add` path |
| `plan sync` | `[--run ID] [--apply]` — diff the run's plan against its tasks (dry run by default, nothing written): new phase/task → added via the add path; changed `title`/`wave`/`owned`/`deps` on a `planned\|routed` task → updated, on a started task → warning only; removed → warning, **never deleted**. `--apply` stores the new `plan_sha` and writes exactly ONE summary event (the reused paths' per-task events are suppressed) |
| `run list` / `run get` | `run get [--id ID \| ID]` (default: the single active run) |
| `run set` | `[--id ID] [--next TEXT] [--status active\|done]` (at least one) |
| `run stage` / `run archive` | `run stage N` (N >= 0, `[--id ID]`); `run archive --id ID` or `run archive ID` |
| `task add` | `--id T --title TEXT [--wave N] [--deps A,RUN:B] [--owned glob,glob] [--parent P]` — deps are stored in the target's stored id case and a self-dep is refused; `--parent` onto a task in `merged`/`qa`/`done` fails; returns `warnings` for unknown deps, owned globs overlapping an unmerged task in any active run (the new task's own `--parent` chain is not an overlap: a parent owns its children's globs), and active sibling runs that could not be read |
| `task set` | `T key=value ... [--note TEXT] [--force]` (`--force`: dispatch past unmet own+ancestor deps; writes a `decision` event naming `--by`); `status=` on a task with children fans out to every child — one transition each, the same `--note`, the parent itself never set directly (it rolls up), the dep gate applied per child before any write; status aliases `reviewing`/`merging`/`fixing` store as `review`/`merged`/`fix`; keys `status agent model effort lane pane branch workspace slot last_commit gates verdict reviewer`; a value starting `{`/`[` is parsed as JSON, empty = null |
| `task round` | `T --kind build\|fix\|takeover [--cause TEXT]`; `T --undo` drops the latest round — but only while none of its brief/report/review files exist (the paths `task round` returned), else `ok:false` naming them; exactly one note event (`round N undone`); no rounds / `--kind` with `--undo` are refused |
| `task verify` | `T --tests '<passed>/<failed>' [--tsc N] [--lint N] [--selftest '<run>/<failed>'] [--fail TEXT]` — stage 7; one `verdict` event, baseline compare, status move (see the subsection below). A failed count larger than the passed count is refused |
| `task get` | `T` |
| `event` | `[--task T] --type transition\|decision\|verdict\|quota\|note --text TEXT [--from S] [--to S]` |
| `wait set` / `wait clear` | `--question TEXT --options a,b [--rec TEXT]` |
| `lock take` / `release` / `check` | `RES --reason TEXT [--ttl-min 30] [--scope a,b] [--force-stale]`; `release RES [--force]` |
| `render` | regenerate `board.md` |
| `store enable` / `store status` | opt-in to the SQLite store (see below) |
| `sync` | `[--project SLUG]` — root-card catch-up (see below) |
| `export-legacy` | `--run R` — rebuild the legacy files from the tables (see below) |

Common flags: `--data DIR`, `--by WHO` (default `orchestrator`; written to events and locks), `--run ID`
for task/event/wait/render (`run *` commands take `--id`, alias `--run`).

### Output

Every `mk-state.py` command prints exactly one JSON line: `{"ok": true, ...}` exit 0;
`{"ok": false, "error": "..."}` exit 2 on bad input or a corrupt/unreadable state file; lock refused →
exit 1 with `state` and `lock`. A malformed sibling active run only produces a `warnings` entry
and is skipped for cross-run `ready`/board upkeep. Flags never prefix-match (`--fo` is an unknown
argument). `task round` returns `{round, kind, brief, report, review}` absolute paths for that round;
`task round --undo` returns `{round, undone}` with `round` back to the remaining round number.

Every lane-watch verdict's evidence ends with a source tag saying which channel decided it:
`source=store` (the project store's `tasks` row — `store row done` / `store row blocked`),
`source=status` (the accepted status file — `status file done (seq N)`), `source=pane` (the
pane text: the DONE token, QUOTA, STALL, ERROR, WATCH_EXPIRED), or `source=grace` (the
no-token rule: commit and report present, agent idle, token missing). The store is read only
when `<data>/mk.db` already exists — a watch never creates it, and without it the status file
and the pane text decide.

lane-watch exit → task status (with `--task --data`, both required together): `DONE`/`DONE_SENT → verifying`,
`QUOTA → quota`, `STALL → stalled`, `PROVIDER → provider`, `BLOCKED → blocked` — only while the task is
`dispatched|building|stalled|quota|provider|blocked`; otherwise the status is kept and a `note` event is
logged. `DEV_SERVER`, `ERROR`, `WATCH_EXPIRED` only append a `note` event.

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
(C1). `design` and `integration` are JSON array text. `runs`, the `[gates]` block and the
`[[phase]]` blocks stay in `header_json`.

| Header key | DB home |
|---|---|
| `plan` | `plans.id` |
| `status` | `plans.status` |
| `created` | `plans.created` |
| `runs` | `plans.header_json` |
| `design` | `plans.design` (JSON array text) |
| `integration` | `plans.integration` (JSON array text) |
| `[gates]` (`test`, `typecheck`, `selftest`, `validate`) | `plans.header_json` |
| `[[phase]]` blocks | `plans.header_json` |
| `[[phase]] id` / `[[phase.task]] id` / `[[phase.task.subtask]] id` | `tasks.id` |
| `title` (phase, task, subtask) | `tasks.title` |
| `acceptance` (task, subtask) | `tasks.acceptance` |
| `owned` (phase, task, subtask) | `tasks.owned` |
| `deps` (phase, task, subtask) | `tasks.deps` |
| `parallel` (task, subtask) | `tasks.parallel` |
| `route` (phase, task, subtask) | `tasks.route_json` (`{"agent","model","effort"}` or empty) |
| nesting of `[[phase.task]]` / `[[phase.task.subtask]]` | `tasks.type` (`phase\|task\|subtask`) and `tasks.parent` |
| `wave`, `forbidden`, `reviewer`, `size`, `risk`, `review`, `qa`, `commit`, `subagents`, `lane_group`, `refs`, `subagent` | `header_json` only — no column |
| the body below the closing `+++` | `plans.body_md` |
| the body H1 line | `plans.title` — the H1 text after the em dash (`# <id> — <title>`); the plan id when there is no H1 (C3) |

The task rows come from the importer `approve_file` (`scripts/mk-plan.py`): its `flat_items`
yields `(id, type, parent, title, acceptance, owned, deps, parallel, route)` per phase, task and
subtask, parent first, and each becomes one `tasks` row of the run.

Lifecycle (C13): `import` loads the file into the store without approving it; `approve` checks
the file, versions it, writes the tasks and copies it to `<data>/plans/archive/<id>-v<n>.md`,
leaving the original in `<data>/plans/`; `run init --plan` starts the run. After import the run
and this DB are the source — status, tasks, rounds and verdicts change there, and MK does not
edit the plan file. When the plan is done the file moves to `<data>/plans/archive/`.

`mk-plan.py check` reports a missing `status`, a missing `created` and a body with no H1 line as
**warnings**, never errors, so an older plan still checks `ok: true`.

## Plan commands (`mk-plan.py`)

`import`, `approve`, `delete` and pause/resume, as `scripts/mk-plan.py` `build_parser` ships them
(C4, C10, C13). Every command prints one JSON line. `--data` and `--home` are optional on
`import`, `approve`, `delete` and `set`.

Data-dir resolution (`resolve_data_arg`), the same order as `mk-state` and copied rather than
imported: `--data`, then `$MATRIXKODER_DATA`, then the data dir this repo registered (the root
`projects.data_dir` row, else `<home>/projects.json`), else `<main checkout>/.matrixkoder`. A
linked worktree uses the main checkout. None of these commands creates a store: a store exists
only when `<data>/mk.db` is already a file.

`import <file> [--dry-run] [--data DIR] [--home DIR]` loads one plan and does not approve it. It
writes one `plans` row (status, created, design, integration, the H1 title, `header_json`,
`body_md`) and no task rows, and it never edits the file. A file whose first line is not `+++`,
or whose header does not parse, is refused (`ok: false`, `reason` set, exit 0). A header with no
`plan` id is refused the same way. The same bytes imported again upsert the same row and change
nothing (`noop: true`); version is left as stored, or `0` when the row is new. `--dry-run`
returns the same reply and writes nothing. Import needs a store; without `<data>/mk.db` it errors.

`approve <path> [--data DIR] [--home DIR] [--run ID]` checks the file, writes the plan version and
its tasks, and copies the approved bytes to `<data>/plans/archive/<id>-v<n>.md`. The original
stays in place (refreshed to those approved bytes, so `run init --plan <original path>` still
resolves). The reply names the run id as `tasks_run` (the `--run` value, else the plan's first
`runs` entry, else the plan id) — no run row exists until `run init`. Approving the same approved
bytes again is a no-op. With no store, approve only flips the file status to `approved` and, when
an `mk.db` sits beside the file that was not the resolved store, adds a warning naming it.

`delete <id> [--data DIR] [--home DIR]` is refused while any `runs` row has `plan=<id>` and
`status='active'` (exit 2, the live run ids named). Otherwise it removes the `plans` row and
moves `<data>/plans/<id>.md` into `<data>/plans/archive/` (a timestamp suffix when that name is
already taken). The file is never unlinked. Tasks of finished runs stay.

Pause and resume are `set <path> --status parked` and `set` back to the previous status. The file
is rewritten as before, and when the store has a `plans.status` column and a row for this plan,
that column follows.

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
`run.json` `plan_id`/`plan_sha`/`plan_status` + index `plan`; phase task `plan_phase`/`route`/
`lane_group`/`reviewer`; child task `plan_task` (its `parent` names the phase task). All `null` for
plan-less runs/tasks (a phase's `reviewer` is `""`, never `null`, when the plan has none); a legacy
run keeps its path-only `plan` field and the plain `plan: \`path\`` board line.
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

## Store mode (`store enable`)

Off by default: `store status` answers `{"store": "off"}` while `<data>/mk.db` does not exist, and
every command keeps using the JSON files. `store enable` is the opt-in. It creates the project
store, and imports every run: each entry in `runs/index.json`, plus any `runs/<id>/run.json`
the index does not list. The importer (`scripts/mk-store-import.py`) lands them in dependency
order, so a `<run>:<id>` dep finds its target; deps that still cannot be resolved are reported in
`unresolved_deps` and do not abort the import. One run that fails to import aborts the whole
enable and `<data>` is left exactly as it was — all or nothing. The reply is `{"store": "on",
"imported": "<run>" | ["<run>", …], "events": N, "tasks": N, "legacy": "<data>/runs-legacy"}`
(`imported` is the single id only when that id is the one active run, otherwise the list, in
dependency order). The current `runs/` tree moves to `<data>/runs-legacy/` — a read-only copy
taken at enable time, not updated again. `run.json`'s task order is kept: the tasks table inserts
parents before children, and the republished view overlays those rows back onto the artifact's
order. Before the import, stale `mk-enable-*` directories in `<data>` older than a minute are
swept (a directory younger than a minute, or one holding a live lock, is left alone).

With the store on, all writes go through the store and the legacy views under `runs/` are
republished from it: `run.json`, `tasks/` and `board.md` are rewritten, and `events.jsonl` is
append-only — new events are appended, the file is never rewritten. `runs/<run>/.store-event`
holds the last published event id and the byte offset where its line ends, written before the
append. A crash between the two leaves the marker ahead of the file; the next publish appends
exactly the missing bytes, and a marker that was rewound skips lines that are already the tail,
so a crash never duplicates a line. `store status` then answers `{"store": "on"}`.

`export-legacy --run R` rebuilds that run's legacy files from the tables (reply `{"ok": true, "run": R}`).

`sync [--project SLUG]` catches the root `cards` up from `sync.last_change_no`: it copies the
`change_log` rows the root is behind on and answers `{"store": "on", "project": "<slug>", "copied": N}`.
Run again it copies nothing (`"copied": 0`) — it is idempotent, and `last_change_no` only moves
forward. The project defaults to `matrixkoder`.

`run archive` with the store on first backs the project store up: `VACUUM INTO` a temp file
(`<data>/archive/<run>.db.tmp`), checked to hold the run and its task count, then renamed to
`<data>/archive/<run>.db`. The backup is written to the temp file first, and an existing backup is
never overwritten — when `<run>.db` is already there the new one lands beside it as
`<run>-<UTC timestamp>.db` (e.g. `r1-20261012T031854Z.db`). The backup happens before the status
commit: if it fails, the temp file is removed and the run stays active.

`plan sync` on the store adds `version_diff` to its reply — the list of plan fields whose stored
version differs from the parsed file (`[]` when they match). The dry run still writes nothing.
`--apply` writes the new plan version into the `plans` table (version + 1) when there is a diff,
and the reply gains `version`, the stored version number.

## Data dir resolution

`<data>` resolves in this order: `--data` > `$MATRIXKODER_DATA` > the root `projects` table (by
repo path) > `<home>/projects.json` > `<main checkout>/.matrixkoder`. The repo is the nearest
`.git` above the cwd, never `$HOME` itself, and a linked worktree maps to its main checkout.
Reading the root table checks that `<home>/mk.db` exists first, so resolving never creates it.
`projects.json` is `{"<repo>": {"data_dir": ...}}`, keyed by the repo's real path. Registering a
project writes both the root `projects` table and `projects.json`; the slug is checked against
`^[a-z0-9][a-z0-9-]{0,40}$` before either is touched, and a bad slug writes nothing.


## SQLite stores (`scripts/mkdb.py`)

`mkdb.py` is the only module that opens a store read-write (research §8: a test greps `scripts/`
for `sqlite3.connect` and fails if any other script opens one without `mode=ro`). Nothing else
calls it yet — the run still uses the JSON/file state above. Every self-test and caller passes a
temp home; the real `~/.matrixkoder` is never created here.

### Locations

| Store | Path | Holds |
|---|---|---|
| Root (C1) | `<home>/mk.db` | `projects`, `cards`, `project_summary`, `sync` |
| Project (C2) | `<data>/mk.db`, or `<home>/projects/<slug>/mk.db` when only a slug is given | `plans`, `runs`, `tasks`, `events`, `artifacts`, `acks`, `lane_slots`, `waits`, `change_log` |

`<home>` is `--home` → `$MK_HOME` → `~/.matrixkoder`. `<data>` is `--data` / `data_dir`: until DM a
project lives at `<repo>/.matrixkoder/`, not under the home. The slug is checked against
`^[a-z0-9][a-z0-9-]{0,40}$` **before** any path is built from it. A home whose path contains
`Mobile Documents`, `CloudStorage`, `iCloud` or `Dropbox`, or starts with `/Volumes/`, is refused —
a path-string heuristic, so a custom sync root or a volume mounted elsewhere slips through
(`shortcut:` in `scripts/mkdb.py`).

### PRAGMAs and transactions

Every open sets `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`. Every write runs inside
`BEGIN IMMEDIATE`. WAL files (`-wal`, `-shm`) sit next to the db and must move with it (`VACUUM INTO`).

### `schema_version` and migrations

Both stores carry `schema_version(version INTEGER PRIMARY KEY)`. `SCHEMA = 4`. Migrations are an
ordered `(version, step)` list applied once. A step is either SQL or a Python callable. SQL steps
are idempotent by construction (`CREATE TABLE IF NOT EXISTS`, `INSERT OR IGNORE`); an
`ALTER TABLE ADD COLUMN` is not safe to run twice, so it goes through a guarded Python step that
checks `PRAGMA table_info` and adds the column only when it is missing. The step and its version
stamp are two transactions, so a crash between them re-runs the step. A store whose version is
newer than the code is refused:
`{"ok": false, "error": "store schema vN is newer …"}`, exit 3.

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

### Project tables (C2)

`tasks(run, id, parent, type, title, acceptance, owned, deps, parallel, route_json, status,
prev_status, lane, slot, branch, pane, round, verdict, updated, PRIMARY KEY(run, id))` — `type` is
`phase|task|subtask`; `prev_status` is the last non-attention status (NULL until the first one);
`deps` is one TEXT CSV of ids in the same plan, or `<run>:<id>` in the same project. Validated on
write: the dep must exist, a self-dep is refused, a cycle is refused with the path named
(`cycle: r:phase -> r:task -> r:phase`). `route_json` is `{"agent","model","effort"}` or empty.
Status rolls up subtask → task → phase through `transition()`: if any child is in an
attention status the parent takes that status, first match in the order `blocked`, `stalled`,
`quota`, `provider`, so the parent keeps its previous column through `prev_status` and gets
`attention=1`; when no child is in an attention status the parent returns to the least-done
column of its children's non-attention statuses, attention 0. A parent already at the rolled
status is not transitioned again.

`events(id INTEGER PRIMARY KEY, run, task, t, chan, kind, from_id, to_id, reply_to, text,
artifact_id, meta_json)` with index `ev_task(run, task, id)`. `chan` is `state` or `bus` on rows
`transition()` and `import_history` write; `append_event` also accepts `note`.
`change_log(change_no INTEGER PRIMARY KEY, run, task, t)` — `change_no` is the rowid, gap-free.
`lane_slots(slot TEXT PRIMARY KEY, lane_path, state, run, task, workspace, workspace_id, branch, lock_hash, updated)` — the five trailing columns are v3, each `TEXT`, and match the slot record in `lanes.json` (`workspace` and `workspace_id` are null for a plain git worktree; `branch`, `lock_hash` and `updated` are null until the slot is acquired). `runs`, `acks`, `waits` as in contract C2.

`plans(id, version, title, status, created, design, integration, header_json, body_md, approved_at, archived_path)`. `status TEXT`, `created TEXT`, `design TEXT` (JSON array text) and `integration TEXT` are v4 (C1), NULL on a row written before the migration. `title` is the H1 text after the em dash, or the plan id when the body has no H1 (C3).
`artifacts(id INTEGER PRIMARY KEY, run, task, round, kind, rev, text, created, src)` — `text` is
stored byte-for-byte; `src` is the v2 source key.

### Root tables (C1)

`cards(project, run, task, parent, type, title, status, col, attention, question, route_json, lane,
round, verdict, gates, updated, change_no, PRIMARY KEY(project, run, task))`. The root card carries
`verdict`: the value `transition()` stored on the task (the imported verdict when a later
status-only transition did not replace it). `col` (C4) is derived
from `status`: Backlog (`planned`, `routed`) · Ready · Building (`dispatched`, `building`,
`takeover`) · Verify (`verifying`) · Review · Fix · Done (`merged`, `qa`, `done`); an attention
status (`blocked`, `stalled`, `quota`, `provider`) keeps the column of the task's previous
non-attention status — `tasks.prev_status`, written by `transition()` — and sets `attention=1`.
A task with no previous non-attention status uses Backlog. `column_of(status, prev_status=None)`
is pure; `transition()` and the rollup do the lookup. `sync(project PRIMARY KEY, last_change_no)`
records how far the root copy got and only ever moves forward (`MAX` of the stored value and the
new `change_no`); a card's own `change_no` is monotonic the same way.

### `transition(home, project, run, task, to, note="", round=0, sync_root=False, data_dir=None, verdict=<unchanged>)`

The only status writer (C10). `verdict` is optional: omitted, the task's stored verdict is left
unchanged; a string or `None` replaces it, and the root card (when `sync_root=True`) and a later
`catch_up()` both copy that value. One project-store transaction updates the task row, appends an
`events` row (`chan='state'`, `kind='transition'`) and a `change_log` row, and returns `change_no`.
A failure inside it leaves nothing. The C5 root card upsert is a **second** transaction and runs
only when `sync_root=True`; the default is `False`, so D1 and D3A do not write `cards`. A crash
between the two leaves the project change durable and the root behind; `catch_up()` replays
`change_log` from `sync.last_change_no`.

### API and CLI

API: `open_root(home)`, `open_project(home, slug=None, data_dir=None)`,
`add_task(con, run, task, type, title="", parent=None, deps="", route_json=None, status="planned")`,
`transition(home, project, run, task, to, note="", round=0, sync_root=False, data_dir=None)`,
`catch_up(home, project, data_dir=None)`, `timeline(home, project, run, task, data_dir=None)`,
`cards(home, project=None)`, `check_slug`, `check_home`, `column_of(status, prev_status=None)`,
`import_history(home, project, run, *, run_row=None, tasks=(), events=(), artifacts=(), data_dir=None)`.

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
mkdb.py cards [--project P] --json [--home DIR]
mkdb.py import-history --project P --run R --json-file F [--data DIR] [--home DIR]
mkdb.py plans --json [--data DIR] [--home DIR]
mkdb.py plans <id> --json [--data DIR] [--home DIR]
mkdb.py --self-test
```


`plans --json` lists every plan. Each object has `id`, `title`, `status`, `created`, `design`,
`integration`, `version`, `approved_at`, `runs`, `live_run` and `counts`. `live_run` is the id of
the plan's live run, or null. `counts` is the task counts by status. `plans <id> --json` is that
same object plus `phases`: each phase with its `tasks`, taken from the latest run (`tasks` joined
to `runs` on `runs.plan = plans.id`), or from the `[[phase]]` / `[[phase.task]]` rows in
`header_json` when the plan has no run (status `planned`). Each phase is `{id, title, wave, deps,
status, tasks}`, each task `{id, title, status, parent, type, round, verdict}`. An unknown id
answers `ok: false` and exits 2. Both reads refuse a store still at v3 ("needs migration to v4;
run a writer command"): any writer, such as `mk-plan.py import`, migrates it first.


`timeline` is `SELECT … FROM events WHERE run=? AND task=? ORDER BY id`. `cards` reads the root
store. `mk-catalog.py` follows the same rule for the root store: its read commands (`list`, `show`, `routes`, `model`) go through `read_catalog`, which returns an empty result when `<home>/mk.db` is absent (`list` and `routes` answer `{"ok": true, …: []}`) and never creates it, while the writers `seed` and `set` call `open_catalog` and do create it (`set` creates it even when the agent is unknown and the command then refuses). One JSON line per call; errors exit 2, a newer store exits 3.

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
  into `meta_json` as `"src"` and recorded in `event_src`. The importer counts a blank line in
  `events.jsonl` (and a malformed line, and a line whose kind is not a state kind) in
  `skipped_events`, with the line number and reason in `skipped_event_reasons`; a blank line is
  not imported.
- `artifacts` are dicts `task round kind rev text created` plus `src`. `text` is stored
  byte-for-byte and `src` goes in the `artifacts.src` column.

Idempotent: an event or artifact whose `src` is already stored is skipped, and tasks and the run
row upsert, so the same call twice adds nothing. Each task actually added gets one `change_log`
row, so `catch_up()` can build its root card later. `sync_root` stays off; this function never
writes the root store. It returns `{runs, tasks, events, artifacts}`, the counts of rows actually
added.

```
mkdb.py import-history --project P --run R --json-file F [--data DIR] [--home DIR]
```

`--json-file` is a JSON object with the same four keys (`run_row`, `tasks`, `events`,
`artifacts`). The command prints one JSON line: `{"ok": true, "runs": N, "tasks": N, "events": N,
"artifacts": N}`.
