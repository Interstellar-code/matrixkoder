# status-contract.md — the mk-builder status file, protocol `v: 1` (`mk-builder-status/v1`)

Frozen by task MB1.1 (plan `mk-builder-skill`, contract C1; decisions D1 and D3 binding), amended
in round 2 against review r1 (HIGH 1-2, MEDIUM 3-7, LOW 8-9, bus forward-fit). The writer is
`child-skills/mk-builder/scripts/mk-builder.py` (MB2.2). The readers are `scripts/lane-watch.py`
`decide()` (MB3.1) and `scripts/mk-lane-event.py` `verdict_for()` / `cmd_sweep()` (MB3.2). This
folder (`child-skills/`) is not a registered skill; it is reference material for those phases and
for the lane-agent skill. Carried items from MB1 review r2 were applied in MB2: R17a (the
`progress` / `progress --subagent` row split), R18 and R27.6 (reviewer identity), R27.9
(monotonic `seq`), R33-R34 (seconds), and the stamp below. Source:
`.matrixkoder/plans/mk-builder-skill-research.md` § "Status-file contract
(`mk-builder-status/v1`)". Every `file:line` below was re-verified in this tree at
`1d8cf51`.

## 1. Path, scope, writer

- **R1.** The status file lives at `<lane>/.matrixkoder-status.json`, where `<lane>` is the lane
  worktree root. One file per lane, shared by both roles: the builder and the reviewer run in the
  same worktree (a reviewer pane is a new pane in the same lane,
  `skills/matrixkoder/SKILL.md:79`), and the identity key separates their lives (R27, R21-R22).
  **Why one shared file:** one path keeps the readers' lookup and the lane's git-exclude entry
  (R2) single; per-role files would double both and let a stale role file hide in `git status`.
- **R2.** The path is git-excluded in lanes: `ensure_lane_exclude` appends `.matrixkoder-*` to the
  lane's `info/exclude` (`scripts/mk-lane-event.py:98-112`). The file never shows in `git status`
  and is never committed.
- **R3.** The only writers are `mk-builder.py` runs by the lane agent — role `builder` or
  `reviewer`. The orchestrator never writes the file and never treats it as sole proof: a `done`
  state still passes the commit and report checks (R30).
- **R4.** A `lane_group` lead writes exactly one file for its lane; each per-phase completion is
  appended to `phases_done` (R8). A non-lead lane writes the same file with `phases_done` `[]`.
- **R5.** The file carries no secrets: no tokens, keys or credentials in any field (lane rule
  "No secrets in code, logs or the report", `skills/matrixkoder/templates/brief.md:26`).
- **R6.** The file is one JSON object carrying **at least** the fields of Table 1, each with its
  stated type and null rule. Fields beyond Table 1 follow the version rules of §8 (optional
  fields since `v` 1, §9); a self-test pins the v1 required key set only for the fields it writes,
  never "no other keys".

## 2. Fields (v1)

Table 1 — required fields. Identity key = `(run, task, round, role)` (R27).

| field | type | null rule | meaning |
|---|---|---|---|
| `v` | integer | never null; always `1` | protocol version (§8) |
| `seq` | integer >= 1 | never null | write counter; `+1` on every accepted write (R25) |
| `run` | string | never null | MK run id; the `mk-state` id alphabet (letters, digits, `.`, `_`, `-`, max 64, `scripts/mk-state.py:134-136`) |
| `task` | string | never null | task id this lane serves; a group lane writes the group's lead task id — the watch's `task` (`scripts/mk-lane-event.py:633`) |
| `round` | integer >= 1 | never null | dispatch round; the `R<n>` token's `n` (R27) |
| `role` | string | never null; `builder` or `reviewer` | lane role; part of the identity key |
| `agent` | string | never null; one of `claude`, `codex`, `opencode`, `agy` (`scripts/lane-health.py:20`) or `unknown` | agent kind; `unknown` when the writer cannot detect it |
| `state` | string | never null; one of `started`, `working`, `subagent`, `done`, `blocked` | current state (§3) |
| `step` | string | never null; `start` until the first `progress` | current step id (e.g. `S1.2`); for a lead, the phase id |
| `phases_done` | array of strings | never null; `[]` when not a lead or no phase completed yet | phase ids completed by a `lane_group` lead, in completion order (R8) |
| `question` | string | null unless `state=blocked`; non-empty when `state=blocked` | the blocked question (R19) |
| `commit` | string | null until `state=done`; then the full 40-hex HEAD sha | the commit HEAD points at when `done` was accepted (R18) |
| `report` | string | never null after `start` | report path relative to the lane root, as given to `start`; a reviewer names its review file |
| `subagent` | object | null unless `state=subagent` | `{"task": <string>, "since": <float epoch>}` (R9) |
| `ctx_pct` | integer 0..100 | null until the agent reports it | context window use in percent (R10) |
| `started_at` | float epoch seconds | never null | first `start` of this (run, task, round, role) (R11) |
| `updated_at` | float epoch seconds | never null; >= `started_at` | last accepted write (R25) |

- **R7.** Every timestamp field (`started_at`, `updated_at`, `subagent.since`) is float epoch
  seconds, written with full precision (`time.time()`), never rounded or truncated.
  **Why:** the watch's `armed_at` is a float epoch (`scripts/mk-lane-event.py:636`), so the
  reader's `updated_at >= armed_at` (R27) is one numeric comparison — a whole-second ISO string
  compared to a float epoch either needs a conversion or rejects a write made in the same second
  as `armed_at`.
- **R8.** `phases_done` is append-only: the `phase-done <id>` command appends a phase id at most
  once per round and never reorders, edits or removes entries; it is `[]` for a non-lead lane.
- **R9.** `subagent`, while `state=subagent`, is `{"task": <the step the subagent owns>,
  "since": <when this subagent run began>}` with `since` a float epoch; a heartbeat bumps `seq`
  and `updated_at` only, so both ages (subagent start, last heartbeat) stay readable. It is null
  in every other state.
- **R10.** `ctx_pct` is written only by the `ctx` command and is an integer 0..100; any other
  value (including 101) is refused with a non-zero exit.
- **R11.** `started_at` keeps the time of the first `start` of this (run, task, round, role); a
  resume (R22) does not rewrite it.

## 3. States and transitions

- **R12.** `state` is one of `started`, `working`, `subagent`, `done`, `blocked`.
- **R13.** Legal state transitions, exhaustive:
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
- **R15.** Same-state rewrites are legal and are not transitions: `progress` in `working`,
  a heartbeat in `subagent`, a re-ask with a new question in `blocked`, `ctx <pct>` in every state
  except `done`, and `phase-done <id>` in `working`.
- **R16.** On an illegal transition the writer exits non-zero, prints one JSON error line naming
  the illegal transition, and leaves the file byte-identical; no temp file is left behind (R24).
- **R17.** The writer's command set is exactly: `start`, `progress <step> [--subagent]`,
  `blocked "<q>"`, `done`, `ctx <pct>`, `phase-done <id>`. Legality per state:

  | command | `started` | `working` | `subagent` | `blocked` | `done` |
  |---|---|---|---|---|---|
  | `progress <step>` | legal (R13.1) | legal (R15) | legal (R13.3) | legal (R13.5) | illegal (R14, R20) |
  | `progress <step> --subagent` | illegal (R14) | legal (R13.2) | legal (R15 heartbeat) | illegal (R14) | illegal (R14, R20) |
  | `blocked "<q>"` | illegal | legal (R13.4) | illegal | legal (re-ask, R15) | illegal |
  | `done` | illegal | legal (R13.6) | illegal | illegal | illegal (R20) |
  | `ctx <pct>` | legal (R15) | legal | legal | legal | illegal (R20) |
  | `phase-done <id>` | illegal | legal (R15) | illegal | illegal | illegal |
  | `start` | resume or archive (R21-R22) | resume or archive | resume or archive | resume or archive | archive only (R21) |

  **Why the table:** `ctx` in `started` was neither a legal rewrite nor illegal in v1's text, and
  `phases_done` had no named append command — a writer author had to guess both.

- **R17a.** `--subagent` and plain `progress` are two commands, not one form, and the row split
  above is the writer's rule: `--subagent` enters or heartbeats the `subagent` state and is legal
  only from `working` (R13.2) or `subagent` (R15); plain `progress` is legal from every state
  except `done` (R13.1, R13.3, R13.5, R15). **Why:** R13 lists `started -> subagent` and
  `blocked -> subagent` under no rule and R14 lists both as illegal, so `progress --subagent` in
  `started`/`blocked` was both legal and illegal at once; R14 is exhaustive and wins. A subagent
  run owns a step, and neither `started` (no step begun) nor `blocked` (waiting on an answer)
  has one to give it — a builder in `blocked` that needs a subagent calls `progress <step>` first
  (R13.5) and then `progress <step> --subagent` (R13.2). Two state-changing writes are the price
  of one readable transition list; the writer refuses the illegal pair with one JSON error line
  (R16) and leaves the file byte-identical.

## 4. `done` and `blocked` preconditions

- **R18.** Entering `done` requires: the report file exists at the `report` path relative to the
  lane root — the review file when the role is `reviewer` (`.matrixkoder-review-rN.md`,
  `skills/matrixkoder/templates/review.md:1`) — AND `git -C <lane> rev-parse HEAD` succeeds.
  The writer records that full sha in `commit`; for a reviewer that sha is the commit under review
  (the lane's HEAD). A reviewer is identified by the armed watch's `phase` carrying the ` REVIEW`
  marker, and by nothing else (R27.6): the watch has no `--no-commit` field, so the sweep reader's
  commit check for a reviewer follows the armed `commit_grep` (`scripts/mk-lane-event.py:222-241`,
  which always builds `rx` from it, and `arm` requires `--commit-grep`, `:615`). `--no-commit` is
  a `lane-watch.py` CLI flag (`scripts/lane-watch.py:15-18`) local to that CLI; only its own path
  counts any HEAD as a matching commit (`scripts/lane-watch.py:111-115`). The writer's `done`
  precondition does not differ per role — report file and a readable HEAD, both.
- **R19.** Entering `blocked` requires a non-empty `question` (whitespace-only counts as empty);
  otherwise exit non-zero, file byte-identical.
- **R20.** `done` is final per (run, task, round, role): after `state=done`, every further write
  for the same identity — another `done`, `progress`, `blocked`, `ctx`, `phase-done`, or a
  same-identity `start` — exits non-zero and leaves the file byte-identical. Only R21 (a different
  identity) moves the file away.

## 5. `start`: archive and resume

- **R21.** `start` with a different `run`, `task`, `round` or `role` than the existing file moves
  that file into `<lane>/.matrixkoder-old-<run>/` — `<run>` is the **archived file's own** `run`
  value, the directory created if missing, the file **never deleted** — under the name
  `status-<task>-r<round>-<role>-<seq>.json` with all four placeholders from the archived file,
  so a second archive in one run never overwrites the first. This holds even when the existing
  file is `state=done`: the reviewer's `start` in a lane whose builder file is done is an archive
  plus a fresh file, never an error. Then the writer writes a fresh file with `seq` 1 and
  `started_at` = now.
- **R22.** `start` with the same `run`, `task`, `round` and `role` as the existing file, whose
  state is not `done`, is a resume. It rewrites: `state=started`, `step=start`,
  `question=null`, `commit=null`, `subagent=null`, `ctx_pct=null`, the `report` path given to
  this `start`, the `agent` of this start (a takeover can change the agent kind — a resume by a
  different `agent` never keeps the stale value), the `role` of this start (a resume per this
  rule carries the same role by construction; a different role archives, R21), `phases_done`
  **kept** as-is (append-only, R8), the original `started_at` (R11), and `seq` continuing (+1).

## 6. Writer rules: atomicity and output

- **R23.** Write the file atomically: create a temp file in the same directory as the target
  (`tempfile.mkstemp(dir=...)`), write the JSON, flush, `os.fsync`, `chmod 0o644`, then
  `os.replace(tmp, path)` — the repo's `write_json` pattern (`scripts/mk-state.py:144-160`,
  `os.replace` at `scripts/mk-state.py:156`). A reader sees the whole old file or the whole new
  file, never a partial write.
- **R24.** On any failure (validation, illegal transition, OS error) the writer removes the temp
  file, exits non-zero with one JSON error line on stdout, and leaves the existing file
  byte-identical (unlink-on-error as in `scripts/mk-state.py:157-160`).
- **R25.** Every accepted write increments `seq` by exactly 1 and sets `updated_at` to the write
  time (float epoch, R7).
- **R26.** Token printing (plan contract C2): `done` prints exactly `<T> R<n> DONE` for a builder
  and `<T> REVIEW R<n> DONE` for a reviewer as its last stdout line — the reviewer form is the
  one `lane-watch.py`'s own self-test arms (`--phase "D2 REVIEW R1"`, matching
  `D2 REVIEW R1 DONE`, `scripts/lane-watch.py:579-588`); `blocked` prints
  `<T> R<n> BLOCKED: <q>`. The agent's reply
  still ends with that line (`skills/matrixkoder/templates/brief.md:30`). The file is the first
  channel, the printed line the fallback and the human-visible one.

## 7. Reader acceptance

- **R27.** Accept the file if and only if all hold:
  1. `<lane>/.matrixkoder-status.json` exists and `json.load` succeeds, and the parsed value is a
     JSON object (dict);
  2. `v` is an int and `v <= 1` (the version the reader supports);
  3. `run` equals the armed watch's `run` (a watch field,
     `scripts/mk-lane-event.py:633`);
  4. `task` equals the armed watch's `task` — the group's lead task id as passed to `arm --task`
     (`listed[0]`, `scripts/mk-lane-event.py:633`); a file naming another member of the watch's
     `tasks` list does not match;
  5. `round` equals the round parsed from the watch's `phase`: the parse rule is
     `^(\S+)( REVIEW)? R(\d+)$`, group 1 equals the watch's `task` (parse failure, a non-numeric
     or non-integer round, or a task mismatch → not accepted);
  6. `role` equals the armed pane's role, and a reviewer is identified by the watch's `phase`
     carrying the optional ` REVIEW` marker (the regex's group 2) and by nothing else. Such a
     watch accepts `role=reviewer` only; any other watch accepts `role=builder` only. The watch
     has no role field and no `--no-commit` field (`scripts/mk-lane-event.py:633-636`);
     `lane-watch.py`'s `--no-commit` CLI flag (`scripts/lane-watch.py:15-18`) is local to that CLI
     and carries no watch state a reader can match on;
  7. `updated_at` (float epoch, R7) is >= the watch's `armed_at` (float epoch,
     `scripts/mk-lane-event.py:636`) — both sides full precision, no rounding;
  8. `state` is one of R12's five values;
  9. `seq` is an int >= 1 (Table 1), and it is strictly greater than the highest `seq` this reader
     has already accepted for the same `(run, task, round, role)`; a first read of a process has
     nothing to compare, so only the type and lower bound apply to it. **Why:** `seq` is the write
     counter the writer bumps on every accepted write (R25) and the only field that orders two
     observations of one lane, so a rewound or replayed file is a stale lane, a forged file or a
     clock reset — never the newer truth a reader should act on. A file rejected on this condition
     falls back exactly as any other R27 miss does (R28), so a writer with a reset counter costs
     the lane its first channel once, not correctness.
- **R28.** Any missed condition in R27 — file absent, unreadable, wrong `v`, wrong
  `run`/`task`/`round`/`role`, `updated_at` before `armed_at`, unknown `state`, a non-monotonic
  `seq` — sends the reader
  down today's pane-text path, unchanged (token regexes `scripts/lane-watch.py:72-83`; `decide()`
  `scripts/lane-watch.py:109-136`).
- **R29.** Corrupt JSON is ignored without raising: the reader catches the parse error and takes
  the R28 fallback. No exception reaches the caller.

## 8. Verdicts from an accepted file

- **R30.** `state=done` counts as the DONE token. The commit check and the report-file check
  remain required, exactly as today (`commit_ok`/`report_ok` feed the DONE return,
  `scripts/lane-watch.py:114-118`). An accepted `done` file **overrides** the `agent_status !=
  "working"` half of that branch: the writer is the agent itself and a pane status lags a
  finished round; it also overrides the `h["state"] != "ERROR"` half, because the file, `git` and
  the report are all read without herdr. It overrides nothing else.
- **R31.** Precedence when an accepted file and the pane disagree. `px` = a valid per-round pane
  token since dispatch (`scripts/lane-watch.py:72-83`):

  | file `state` | pane token | verdict | `source` |
  |---|---|---|---|
  | `done` | `<…> DONE` present | DONE | `status` |
  | `done` | none | DONE | `status` |
  | `done` | `<…> BLOCKED` only | R36 rule (later wins) | — |
  | `blocked` | none | BLOCKED with `question` | `status` |
  | `blocked` | `<…> DONE` | R36 rule (later wins) | — |
  | `started`/`working`/`subagent` | `<…> DONE` | DONE (a builder that forgot `done` finished) | `pane` |
  | `started`/`working`/`subagent` | `<…> BLOCKED` | BLOCKED | `pane` |
  | `done` but commit or report missing | any | no verdict from the file → R28 pane path | `pane`/`grace` as today |

  Done never loses to a pane QUOTA/PROVIDER/DEV_SERVER string (as today,
  `scripts/lane-watch.py:11-14`, `:253-272`); STALL needs no token at all (R33).
- **R32.** `state=blocked` is the BLOCKED verdict, carrying `question` as the blocked text (the
  pane path's shape, `scripts/lane-watch.py:119-120`).
- **R33.** `state=working` or `state=subagent`, with `now - updated_at < stall_min * 60`, counts as
  activity for that poll: the reader moves the lane's idle-clock mark to `updated_at` (a writer
  that stops moving lets the clock run out). **The comparison is in seconds on both sides** —
  `now` and `updated_at` are float epoch seconds (R7) and the watch's `stall_min` is minutes, so
  the reader multiplies the minutes by 60 and never compares a float to a bare minute count.
  `stall_min` comes from the armed watch or the reader's own default, exactly as today (CLI
  default 10 at `scripts/lane-watch.py:282`; the event path's watch default is 15,
  `scripts/mk-lane-event.py:626`).
- **R34.** `state=subagent` resets the idle clock only while `now - armed_at <= max_wait_min * 60`
  — seconds again, by the same multiplication as R33 — and `armed_at` is the watch's float epoch
  (`scripts/mk-lane-event.py:636`), not the file's `started_at`. The default is 30
  (`scripts/lane-watch.py:280`). Past that, `subagent` no longer counts as activity
  and the existing pane spinner/toolcall detection alone keeps the lane alive
  (`scripts/lane-watch.py:86-106`).
- **R35.** `QUOTA`, `PROVIDER` and `DEV_SERVER` are never taken from the file; they stay
  pane-text detections (quota footer `scripts/lane-watch.py:253-272`; PROVIDER and DEV_SERVER
  from lane-health `classify` `scripts/lane-health.py:114-130`, surfaced via `FAIL_STATES`
  `scripts/lane-watch.py:49`). An agent that hit a quota or provider wall cannot write a file.
- **R36.** When an accepted file and the pane text disagree about a terminal verdict (the file
  says `done` and the pane's latest token says BLOCKED, or vice versa), the newer observation
  wins: the file's `updated_at` against the poll time at which the reader first saw the pane
  token; if neither is newer, BLOCKED wins. Every disagreement is logged as a note.

## 9. Version rules and reserved fields

- **R37.** `v` stays `1`. A writer adds new optional fields without bumping `v`.
- **R38.** A reader ignores every field it does not know — required (Table 1) and reserved
  (Table 2) alike. It never rejects a file for an unknown field.
- **R39.** A reader that finds `v` greater than it supports ignores the whole file (R28
  fallback); it never raises and never applies part of a newer file.
- **R40.** Table 2 — optional fields reserved for the planned message bus (forward-fit only;
  this file does not design the bus). All are `string | null`, all null in v1, and v1 writers
  never set them; readers apply R38:

  | field | reserved meaning |
  |---|---|
  | `proto` | fixed `"mk-builder-status/v1"`; tells this file apart from the planned `.matrixkoder-bus.json`, which also uses a `v` for its own protocol |
  | `msg_id` | id of the bus message this write answers |
  | `question_id` | id of the bus `ask` this `question` pairs with |
  | `answer_id` | id of the bus `answer` sent for the current `question` |
  | `last_read_id` | highest bus message id the lane agent printed (ack level 3, "read") |

  The bus repeats `run`, `task`, `round`, `role` with the same names and types as Table 1, so one
  identity check (R27) serves both files.

## 10. Verdict source (plan contract C4)

- **R41.** Every verdict event derived from a lane poll carries `source` as a **top-level key on
  the `events.jsonl` record**, beside the record's fixed fields (`t`, `run`, `task`, `type`,
  `text`, `from`, `to`, `by`, `docs/state-schema.md:108-116`): `status` = decided from an accepted
  status file; `pane` = today's pane-text path; `grace` = the no-token DONE rule
  `TOKEN_GRACE_MIN` (`scripts/lane-watch.py:52`, applied `scripts/lane-watch.py:122-127`).
  `QUOTA`, `PROVIDER`, `DEV_SERVER`, `STALL`, `ERROR` and `WATCH_EXPIRED` verdicts always carry
  `pane` — they are pane detections by definition (R35). Counted by `measure` (MB5.1).
- **R42.** `sweep` reads the status file inside the existing per-lane flock (`class locked`,
  `scripts/mk-lane-event.py:192-205`; taken per lane in `cmd_sweep`,
  `scripts/mk-lane-event.py:730`). No second locking scheme is introduced.

## 11. Open points decided here

Each rule the research or a review round left open, with its one-line why:

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
- Timestamps are float epoch seconds, full precision (R7): one numeric comparison against
  `armed_at` (itself a float epoch) needs no conversion and cannot reject a same-second write.
- `started_at` on a resumed round = the first start of that (run, task, round, role) (R11, R22):
  the round's true age survives a resume.
- Builder and reviewer share one file, separated by `role` in the identity key (R1, R27): one
  path to exclude and to read; the archive rule (R21) — legal even over a `done` file — is what
  makes a reviewer's `start` after the builder's `done` work.
- The round comes from parsing the watch's `phase` (`^(\S+)( REVIEW)? R(\d+)$`, R27.5), and the
  same parse marks the reviewer role (R27.6): the watch has no `round` or `role` field
  (`scripts/mk-lane-event.py:633-636`), and `lane-watch.py`'s self-test already arms the
  `REVIEW` form (`scripts/lane-watch.py:579-588`).
- An accepted `done` overrides the pane status and herdr-ERROR halves of the DONE branch (R30):
  file, `git` and the report are pane-independent evidence; the commit and report checks stay.
- Terminal file/pane disagreement resolves by time, tie to BLOCKED (R36): a pane line has no
  timestamp, so the reader logs the poll time at which it first saw the token; blocking beats
  finishing on a tie because a missed answer is recoverable and a false DONE is not.
- `ctx` is legal in every state except `done`, and `phase-done` only in `working` (R15, R17): a
  context reading is not progress, and a phase completes between steps, not inside one.

## 12. Worked examples

Snapshots of one lane's file. Armed watch for the builder examples: run `settings-tab-r1`, task
`S1`, round 1, `armed_at` `1791627619.538958` (float epoch). The reviewer example is the same
lane after the builder's `done`: the builder file was archived by the reviewer's `start` (R21)
and this is the reviewer's fresh file (`seq` restarts at 1).

`state=started` — builder's fresh `start`, nothing archived, no progress yet (seq 1):

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
  "started_at": 1791627650.0,
  "updated_at": 1791627650.0
}
```

`state=working` — first `progress parse-plan`, `ctx 12` written (seq 2):

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
  "ctx_pct": 12,
  "started_at": 1791627650.0,
  "updated_at": 1791627761.0
}
```

`state=subagent` — `progress S1.2 --subagent` at 1791627872.0, heartbeat at 1791628163.0
(seq 3):

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
    "since": 1791627872.0
  },
  "ctx_pct": null,
  "started_at": 1791627650.0,
  "updated_at": 1791628163.0
}
```

`state=blocked` — `blocked "<q>"` from `working`, question set, `commit` null (seq 4):

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
  "started_at": 1791627650.0,
  "updated_at": 1791628975.0
}
```

`state=done` — report file exists, HEAD recorded, final for round 1 (seq 5); the reviewer's later
`start` archives this file (R21):

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
  "started_at": 1791627650.0,
  "updated_at": 1791629878.0
}
```

`state=done`, `role=reviewer` — the reviewer's own file in the same lane, same identity except
`role` (R27.6 armed the reviewer on the `REVIEW` phase form); the report is the review file and
`commit` is the reviewed HEAD (R18):

```json
{
  "v": 1,
  "seq": 2,
  "run": "settings-tab-r1",
  "task": "S1",
  "round": 1,
  "role": "reviewer",
  "agent": "claude",
  "state": "done",
  "step": "review",
  "phases_done": [],
  "question": null,
  "commit": "1f3a9c2e77b4d6f8a0c5e3b2d1a09f8e7c6b5a4d",
  "report": ".matrixkoder-review-r1.md",
  "subagent": null,
  "ctx_pct": null,
  "started_at": 1791630010.0,
  "updated_at": 1791630520.0
}
```
