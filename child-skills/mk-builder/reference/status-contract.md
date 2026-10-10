# status-contract.md — the mk-builder status file, protocol `v: 1` (`mk-builder-status/v1`)

Frozen by task MB1.1 (plan `mk-builder-skill`, contract C1; decisions D1 and D3 binding). The writer
is `child-skills/mk-builder/scripts/mk-builder.py` (MB2.2). The readers are `scripts/lane-watch.py`
`decide()` (MB3.1) and `scripts/mk-lane-event.py` `verdict_for()` / `cmd_sweep()` (MB3.2). This
folder (`child-skills/`) is not a registered skill; it is reference material for those phases and
for the lane-agent skill. Source: `.matrixkoder/plans/mk-builder-skill-research.md` § "Status-file
contract (`mk-builder-status/v1`)". Every `file:line` below was re-verified in this tree at `4df9b9c`.

## 1. Path, scope, writer

- **R1.** The status file lives at `<lane>/.matrixkoder-status.json`, where `<lane>` is the lane
  worktree root. Exactly one file per lane.
- **R2.** The path is git-excluded in lanes: `ensure_lane_exclude` appends `.matrixkoder-*` to the
  lane's `info/exclude` (`scripts/mk-lane-event.py:98-112`). The file never shows in `git status`
  and is never committed.
- **R3.** The only writer is `mk-builder.py`, run by the lane agent (role `builder` or `reviewer`).
  The orchestrator never writes the file and never treats it as sole proof: a `done` state still
  passes the commit and report checks (R29).
- **R4.** A `lane_group` lead writes exactly one file for its lane; each per-phase completion is
  appended to `phases_done` (R8). A non-lead lane writes the same file with `phases_done` `[]`.
- **R5.** The file carries no secrets: no tokens, keys or credentials in any field (lane rule
  "No secrets in code, logs or the report", `skills/matrixkoder/templates/brief.md:26`).

## 2. Fields (v1)

- **R6.** The file is one JSON object carrying exactly the fields of Table 1. Fields beyond Table 1
  follow the version rules of §7.

| field | type | null rule | meaning |
|---|---|---|---|
| `v` | integer | never null; always `1` | protocol version (§7) |
| `seq` | integer >= 1 | never null | write counter; `+1` on every accepted write (R24) |
| `run` | string | never null | MK run id; the `mk-state` id alphabet (letters, digits, `.`, `_`, `-`, max 64, `scripts/mk-state.py:134-136`) |
| `task` | string | never null | task id this lane serves; a group lane writes the listed task the watch carries |
| `round` | integer >= 1 | never null | dispatch round; the `R<n>` token's `n` (R1 = `1`) |
| `role` | string | never null; `builder` or `reviewer` | lane role |
| `agent` | string | never null; one of `claude`, `codex`, `opencode`, `agy` (`scripts/lane-health.py:20`) or `unknown` | agent kind; `unknown` when the writer cannot detect it |
| `state` | string | never null; one of `started`, `working`, `subagent`, `done`, `blocked` | current state (§3) |
| `step` | string | never null; `start` until the first `progress` | current step id (e.g. `S1.2`); for a lead, the phase id |
| `phases_done` | array of strings | never null; `[]` when not a lead or no phase completed yet | phase ids completed by a `lane_group` lead, in completion order (R8) |
| `question` | string | null unless `state=blocked`; non-empty when `state=blocked` | the blocked question (R18) |
| `commit` | string | null until `state=done`; then the full 40-hex HEAD sha | the commit HEAD points at when `done` was accepted (R17) |
| `report` | string | never null after `start` | report path relative to the lane root, as given to `start` |
| `subagent` | object | null unless `state=subagent` | `{"task": <string>, "since": <UTC ISO>}` (R9) |
| `ctx_pct` | integer 0..100 | null until the agent reports it | context window use in percent (R10) |
| `started_at` | string | never null | UTC ISO timestamp of the first `start` of this (run, task, round) (R11) |
| `updated_at` | string | never null; >= `started_at` | UTC ISO timestamp of the last accepted write |

- **R7.** Timestamps (`started_at`, `updated_at`, `subagent.since`) are UTC ISO
  `YYYY-MM-DDTHH:MM:SSZ`, produced with `time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())`.
  A reader parses them with `datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")`; a parse failure
  makes the file not accepted (R26 condition 6).
- **R8.** `phases_done` is append-only: the writer appends a phase id at most once per round and
  never reorders, edits or removes entries; it is `[]` for a non-lead lane.
- **R9.** `subagent`, while `state=subagent`, is `{"task": <the step the subagent owns>,
  "since": <when this subagent run began>}`; a heartbeat bumps `seq` and `updated_at` only, so
  both ages (subagent start, last heartbeat) stay readable. It is null in every other state.
- **R10.** `ctx_pct` is written only by the `ctx` command and is an integer 0..100; any other
  value (including 101) is refused with a non-zero exit.
- **R11.** `started_at` keeps the time of the first `start` of this (run, task, round); a resume
  (R21) does not rewrite it.

## 3. States and transitions

- **R12.** `state` is one of `started`, `working`, `subagent`, `done`, `blocked`.
- **R13.** Legal transitions, exhaustive:
  1. `started -> working` — the first `progress`.
  2. `working -> subagent` — `progress <step> --subagent`.
  3. `subagent -> working` — `progress` after checking the subagent's owned files
     (`skills/matrixkoder/templates/brief.md:24`).
  4. `working -> blocked` — `blocked "<q>"`.
  5. `blocked -> working` — `progress` after the answer.
  6. `working -> done` — `done`.
- **R14.** Every transition not listed in R13 is illegal. In particular: `done -> anything`,
  `blocked -> done`, `blocked -> subagent`, `started -> done`, `started -> blocked`,
  `started -> subagent`, `subagent -> done`, `subagent -> blocked`, `working -> started`.
- **R15.** Same-state rewrites are legal for `working` (`progress`), `subagent` (heartbeat) and
  `blocked` (re-ask with a new question). `done` has none (R19).
- **R16.** On an illegal transition the writer exits non-zero, prints one JSON error line naming
  the illegal transition, and leaves the file byte-identical; no temp file is left behind (R23).
- **R17.** Entering `done` requires: the report file exists at the `report` path relative to the
  lane root, AND `git -C <lane> rev-parse HEAD` succeeds. The writer records that full sha in
  `commit`. (A reviewer lane: HEAD is the lane's base commit; the reader's `--no-commit` path
  counts any HEAD, `scripts/lane-watch.py:111-115`.)
- **R18.** Entering `blocked` requires a non-empty `question` (whitespace-only counts as empty);
  otherwise exit non-zero, file byte-identical.
- **R19.** `done` is final per round: after `state=done`, every further write to that file —
  another `done`, `progress`, `blocked`, or a same-round `start` — exits non-zero and leaves the
  file byte-identical. Only R20 (a new round) moves the file away.
- **R20.** `start` with a different `run`, `task` or `round` than the existing file moves that
  file into `<lane>/.matrixkoder-old-<run>/` (created if missing; kept, never deleted) and writes
  a fresh file with `seq` 1 and `started_at` = now.
- **R21.** `start` with the same `run`, `task` and `round` as the existing file, whose state is
  not `done`, is a resume: it rewrites the file with `state=started`, `step=start`,
  `question=null`, `commit=null`, `subagent=null`, `ctx_pct=null`, the `report` path given to this
  `start`, the original `started_at`, and `seq` continuing (+1).

## 4. Writer rules: atomicity and output

- **R22.** Write the file atomically: create a temp file in the same directory as the target
  (`tempfile.mkstemp(dir=...)`), write the JSON, flush, `os.fsync`, `chmod 0o644`, then
  `os.replace(tmp, path)` — the repo's `write_json` pattern (`scripts/mk-state.py:144-160`,
  `os.replace` at `scripts/mk-state.py:156`). A reader sees the whole old file or the whole new
  file, never a partial write.
- **R23.** On any failure (validation, illegal transition, OS error) the writer removes the temp
  file, exits non-zero with one JSON error line on stdout, and leaves the existing file
  byte-identical (unlink-on-error as in `scripts/mk-state.py:157-160`).
- **R24.** Every accepted write increments `seq` by exactly 1 and sets `updated_at` to the write
  time (UTC, R7).
- **R25.** Token printing (plan contract C2): `done` prints exactly `<T> R<n> DONE` as its last
  stdout line and `blocked` prints exactly `<T> R<n> BLOCKED: <q>`; the agent's reply still ends
  with that line (`skills/matrixkoder/templates/brief.md:30`). The file is the first channel, the
  printed line the fallback and the human-visible one.

## 5. Reader acceptance

- **R26.** Accept the file if and only if all hold:
  1. `<lane>/.matrixkoder-status.json` exists and `json.load` succeeds;
  2. the parsed value is a JSON object (dict);
  3. `v` is an int and `v <= 1` (the version the reader supports);
  4. `run` and `task` equal the armed watch's (both are watch fields,
     `scripts/mk-lane-event.py:633-634`);
  5. `round` equals the round the orchestrator armed for this dispatch (the `R<n>` of the
     token, C2);
  6. `updated_at` parses per R7 and is >= the watch's `armed_at` (a watch field,
     `scripts/mk-lane-event.py:636`) — a stale file from a reused slot never counts.
- **R27.** Any missed condition in R26 — file absent, unreadable, wrong `v`, wrong
  `run`/`task`/`round`, `updated_at` before `armed_at` — sends the reader down today's pane-text
  path, unchanged (token regexes `scripts/lane-watch.py:72-83`; `decide()`
  `scripts/lane-watch.py:109-136`).
- **R28.** Corrupt JSON is ignored without raising: the reader catches the parse error and takes
  the R27 fallback. No exception reaches the caller.

## 6. Verdicts from an accepted file

- **R29.** `state=done` counts as the DONE token. The commit check and the report-file check
  remain required, exactly as today (`commit_ok`/`report_ok` feed the DONE return,
  `scripts/lane-watch.py:114-118`).
- **R30.** `state=blocked` is the BLOCKED verdict, carrying `question` as the blocked text (the
  pane path's shape, `scripts/lane-watch.py:119-120`).
- **R31.** `state=working` or `state=subagent`, with `now - updated_at < stall_min`, counts as
  activity for that poll: the reader moves the lane's idle-clock mark to `updated_at` (a writer
  that stops moving lets the clock run out). `stall_min` comes from the armed watch or the
  reader's own default, exactly as today (CLI default 10 at `scripts/lane-watch.py:282`; the
  event path's watch default is 15, `scripts/mk-lane-event.py:626`).
- **R32.** `state=subagent` resets the idle clock only while `now - armed_at <= max_wait_min`
  (default 30, `scripts/lane-watch.py:280`). Past that, `subagent` no longer counts as activity
  and the existing pane spinner/toolcall detection alone keeps the lane alive
  (`scripts/lane-watch.py:86-106`).
- **R33.** `QUOTA`, `PROVIDER` and `DEV_SERVER` are never taken from the file; they stay
  pane-text detections (quota footer `scripts/lane-watch.py:253-272`; PROVIDER and DEV_SERVER
  from lane-health `classify` `scripts/lane-health.py:114-130`, surfaced via `FAIL_STATES`
  `scripts/lane-watch.py:49`). An agent that hit a quota or provider wall cannot write a file.
- **R34.** When an accepted file and the pane text disagree about BLOCKED, the newer observation
  wins: the file's `updated_at` against the poll time at which the reader first saw the pane
  token; if neither is newer, BLOCKED wins. Every disagreement is logged as a note.

## 7. Version rules

- **R35.** `v` stays `1`. A writer adds new optional fields without bumping `v`.
- **R36.** A reader ignores every field it does not know.
- **R37.** A reader that finds `v` greater than it supports ignores the whole file (R27
  fallback); it never raises and never applies part of a newer file.

## 8. Verdict source (plan contract C4)

- **R38.** Every verdict event derived from a lane poll carries `source` of `status`, `pane` or
  `grace`: `status` = decided from an accepted status file; `pane` = today's pane-text path;
  `grace` = the no-token DONE rule `TOKEN_GRACE_MIN` (`scripts/lane-watch.py:52`, applied
  `scripts/lane-watch.py:122-127`). Counted by `measure` (MB5.1).
- **R39.** `sweep` reads the status file inside the existing per-lane flock (`class locked`,
  `scripts/mk-lane-event.py:192-205`; taken per lane in `cmd_sweep`,
  `scripts/mk-lane-event.py:730`). No second locking scheme is introduced.

## 9. Open points decided here

Each rule the research left open, with its one-line why:

- `question` / `commit` null when not applicable (Table 1): absence is the signal; readers test
  `is None`.
- `report` never null after `start`: `start` takes the path (MB2.2), and a never-null declared
  path lets `done` and the reader find the report without the watch.
- `subagent` object while `state=subagent`, null otherwise (R9): one shape per state, no
  half-filled object.
- `ctx_pct` null until reported (R10): no agent is known to expose its own context percentage,
  so null is the honest value.
- `phases_done` `[]` for a non-lead lane (R8): one list type everywhere, so a reader needs no
  null branch.
- Timestamps UTC ISO with `Z` suffix (R7): one parseable format, no local-time ambiguity between
  machines.
- `started_at` on a resumed round = the first start of that (run, task, round) (R11, R21): the
  round's true age survives a resume.

## 10. Worked examples

Five snapshots of one lane's file, one per state, `seq` growing by 1 between blocks to show the
counter's shape. They are independent snapshots: a real round passes through `working` between
`subagent` and `blocked` (R13), and every write consumes its own `seq`. Armed watch for all five:
run `settings-tab-r1`, task `S1`, round 1, `armed_at` `2026-10-10T09:14:52Z`.

`state=started` — fresh `start`, nothing archived, no progress yet (seq 1):

```json
{
  "v": 1,
  "seq": 1,
  "run": "settings-tab-r1",
  "task": "S1",
  "round": 1,
  "role": "builder",
  "agent": "opencode",
  "state": "started",
  "step": "start",
  "phases_done": [],
  "question": null,
  "commit": null,
  "report": ".matrixkoder-report-r1.md",
  "subagent": null,
  "ctx_pct": null,
  "started_at": "2026-10-10T09:15:00Z",
  "updated_at": "2026-10-10T09:15:00Z"
}
```

`state=working` — first `progress parse-plan` (seq 2):

```json
{
  "v": 1,
  "seq": 2,
  "run": "settings-tab-r1",
  "task": "S1",
  "round": 1,
  "role": "builder",
  "agent": "opencode",
  "state": "working",
  "step": "parse-plan",
  "phases_done": [],
  "question": null,
  "commit": null,
  "report": ".matrixkoder-report-r1.md",
  "subagent": null,
  "ctx_pct": null,
  "started_at": "2026-10-10T09:15:00Z",
  "updated_at": "2026-10-10T09:22:41Z"
}
```

`state=subagent` — `progress S1.2 --subagent` at 09:31:12Z, heartbeat at 09:44:03Z (seq 3):

```json
{
  "v": 1,
  "seq": 3,
  "run": "settings-tab-r1",
  "task": "S1",
  "round": 1,
  "role": "builder",
  "agent": "opencode",
  "state": "subagent",
  "step": "S1.2",
  "phases_done": [],
  "question": null,
  "commit": null,
  "report": ".matrixkoder-report-r1.md",
  "subagent": {
    "task": "S1.2",
    "since": "2026-10-10T09:31:12Z"
  },
  "ctx_pct": null,
  "started_at": "2026-10-10T09:15:00Z",
  "updated_at": "2026-10-10T09:44:03Z"
}
```

`state=blocked` — `blocked "<q>"` from `working` (seq 4):

```json
{
  "v": 1,
  "seq": 4,
  "run": "settings-tab-r1",
  "task": "S1",
  "round": 1,
  "role": "builder",
  "agent": "opencode",
  "state": "blocked",
  "step": "S1.3",
  "phases_done": [],
  "question": "The spec names two fixture formats (v2 and v3) for the config parser - which one is the acceptance target?",
  "commit": null,
  "report": ".matrixkoder-report-r1.md",
  "subagent": null,
  "ctx_pct": null,
  "started_at": "2026-10-10T09:15:00Z",
  "updated_at": "2026-10-10T10:02:55Z"
}
```

`state=done` — report file exists, HEAD recorded, final for round 1 (seq 5):

```json
{
  "v": 1,
  "seq": 5,
  "run": "settings-tab-r1",
  "task": "S1",
  "round": 1,
  "role": "builder",
  "agent": "opencode",
  "state": "done",
  "step": "selftest",
  "phases_done": [],
  "question": null,
  "commit": "1f3a9c2e77b4d6f8a0c5e3b2d1a09f8e7c6b5a4d",
  "report": ".matrixkoder-report-r1.md",
  "subagent": null,
  "ctx_pct": null,
  "started_at": "2026-10-10T09:15:00Z",
  "updated_at": "2026-10-10T10:41:18Z"
}
```
