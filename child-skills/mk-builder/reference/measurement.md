# measurement.md — the three metrics, the settings-tab-r1 baseline, and where the numbers are recorded

Frozen by MB5.2 (plan `mk-builder-skill`; the counters are `mk-builder.py measure`, MB5.1). The
audience is the orchestrator alone: it takes the "after" numbers, applies the verdict rule in §5,
and appends the results per §6. A lane agent never runs this loop. Numbers here are the real
output of `measure` on run `settings-tab-r1` (2026-10-10), pasted verbatim in §3 — nothing is
invented or rounded into prose.

## 1. The command

```
python3 child-skills/mk-builder/scripts/mk-builder.py measure --data <data> --run <id>
```

One JSON line on stdout. Read-only: the `--self-test` case `t_measure_read_only` asserts that no
file under `<data>` changes (paths, sizes and mtimes identical before and after) and that a
second run is byte-identical. It reads `<data>/runs/<id>/events.jsonl` and
`<data>/runs/<id>/tasks/*/brief-r*.md` and nothing else; torn event lines are skipped, not fatal
(`mk-state.py:355-365`).

## 2. The three metrics (exactly as `measure` reports them)

1. **Brief lines and bytes per task** — every `brief-rN.md` under the run's `tasks/<T>/`, lines
   (`wc -l` on the newline-terminated briefs mk-state writes) and bytes (`wc -c`). `task.json`,
   `review-brief-rN.md` and reports are not briefs.
2. **Missed DONE lines per run** — DONE verdict events that needed the grace rule: evidence
   `source=grace`, or the pre-MB3 shape `no <phase> DONE token (...)` (`lane-watch.py:233-240`;
   settings-tab-r1 wrote `no S2 R3 DONE token (commit + report, agent idle 3.1 min)`). Counted
   per EVENT: `record()` writes one event per child task, and once more as a kept-status note
   (`lane-watch.py:317-345`), so a 4-child lane records one verdict up to 8×. Compare event
   counts across runs (same rule every time); when the number moves, read the incident in the
   events themselves.
3. **False STALLs per run** — STALL verdict events followed by a DONE verdict, or by any event
   mentioning a commit sha (7-40 hex), for the SAME task within 10 minutes. A deliberately
   strict proxy: it undercounts. In settings-tab-r1 both STALL waves were false (the lead
   waiting on a foreground subagent, corrected by hand 26 s later) yet count 0, because the
   DONEs landed 46-56 min later — outside the window. The proxy answers "did the lane finish
   right behind the alarm", not "was the alarm right".

Alongside them the command reports `verdicts_by_source` (`status|pane|grace`, plus `none` for
runs that predate the MB3 source tag — plan C4, `mk-lane-event.py:330-340`) and
`blocked_questions` (the BLOCKED verdicts' evidence with the `; source=…` item, any
`(status … kept)` suffix, and the `<phase> BLOCKED: ` / `status file blocked: ` prefix
stripped).

## 3. Baseline — run `settings-tab-r1` (2026-10-10, before the skill existed)

Real output, verbatim (rerun any time with §1's command):

```json
{"ok": true, "run": "settings-tab-r1", "data": "/Users/rohits/Development/matrixkoder/.matrixkoder", "briefs": {"S1": {"brief-r1.md": {"lines": 39, "bytes": 8450}, "brief-r2.md": {"lines": 25, "bytes": 4256}, "brief-r3.md": {"lines": 18, "bytes": 3097}, "brief-r4.md": {"lines": 18, "bytes": 3110}}, "S1.1": {}, "S1.2": {}, "S1.3": {}, "S1.4": {}, "S2": {"brief-r1.md": {"lines": 43, "bytes": 7339}, "brief-r3.md": {"lines": 26, "bytes": 4013}}, "S2.0": {}, "S2.1": {}, "S2.2": {}, "S2.3": {}, "S2b": {}, "S3": {"brief-r1.md": {"lines": 51, "bytes": 7169}, "brief-r2.md": {"lines": 24, "bytes": 3786}, "brief-r3.md": {"lines": 26, "bytes": 3474}}, "S3.1": {}, "S3.2": {}}, "verdicts_by_source": {"status": 0, "pane": 0, "grace": 0, "none": 70}, "missed_done_lines": 8, "stalls": 8, "false_stalls": 0, "blocked": 4, "blocked_questions": ["agent_status=blocked (approval/permission dialog?)", "agent_status=blocked (approval/permission dialog?)", "agent_status=blocked (approval/permission dialog?)", "agent_status=blocked (approval/permission dialog?)"]}
```

(Child tasks S1.1-S1.4, S2.0-S2.3, S3.1-S3.2 and S2b hold `{}`: only lead tasks get briefs.)

The reading:

- **Briefs.** First-round briefs: S1 39 lines / 8450 B, S2 43 lines / 7339 B, S3 51 lines /
  7169 B — mean **44.3 lines / 7652.7 B**. Fix-round briefs are short by nature (r2-r4: 18-26
  lines) and are excluded from every comparison. Each first-round brief carried a 10-line
  `## Rules` block of ~2.1 KB (research Part B: S1:30-39, S2:34-43) plus the old-code-proof
  bullet (~330 B) — the text MB4 removed from `templates/brief.md` in favour of this skill.
- **Verdicts by source.** 70 lane-watch verdict events, all `none`: the run predates the MB3
  `source=` tag, so every DONE/STALL/BLOCKED/QUOTA was pane scraping.
- **Missed DONE lines: 8 events = one incident.** The S2 R3 reply ended without its token
  (`no S2 R3 DONE token (commit + report, agent idle 3.1 min)`, commit `f6ec0d5`), fanned out
  over 4 child tasks × transition + kept-status note.
- **STALLs 8, false 0.** Both waves (S1.x at 11:53, S2.x at 11:54) were the lead idle-waiting on
  a foreground subagent — mk-improvements item 8, the reason MB3's `subagent` heartbeat exists.
  The proxy counts 0 because the DONEs came 46-56 min later.
- **Blocked 4 events, one question ×4** — `agent_status=blocked (approval/permission dialog?)`,
  S3's approval dialog, pre-MB3 pane detection.

## 4. The "after" protocol

The orchestrator runs the same `measure` on the next two runs whose briefs come from the slim
(MB4) template, after `mk-builder-skill` merges, and appends each run's JSON line plus the
reading to `.matrixkoder/plans/mk-improvements.md` (§6). Two runs, not one, because round-1
brief sizes vary by phase; two give the >= 20% rule something to bite on.

## 5. The verdict rule

Keep the skill (no per-agent plugins) only if, on both after runs:

- **brief lines drop >= 20%** — the mean of the run's `brief-r1.md` lines against the baseline's
  44.3, i.e. **<= 35.5 lines** (bytes reported alongside, same direction), and
- **missed DONE + false STALL do not rise** — against 8 + 0 events, read with the fan-out
  caveat in §2.2 (a lane with more child tasks inflates the event count without more
  incidents; check the events before calling it a rise).

Otherwise the orchestrator writes the gap list — what actually failed, from this file's numbers
plus the plan's risks: verdicts still `source=pane`/`none` (the skill never loaded, or the agent
ignored it), missed DONE lines not dropping (skill loaded, token rule ignored), wrapper smoke
failures or PATH loss per agent kind (plan `qa`, MB5), the project-skill trust prompt in fresh
worktrees — and THAT list, not the raw numbers, is what would justify per-agent plugins
(mk-improvements item 1: "Per-agent plugins only if measurement shows a gap").

## 6. Where the numbers are recorded

Results are appended to `.matrixkoder/plans/mk-improvements.md` under the run's own section
(`## Run <id>` — the baseline's findings under `## Run settings-tab-r1`, line 112 today), with
the final verdict cross-referenced from item 1 — **by the orchestrator, never by a lane agent**
(lane agents cannot read other runs' data dirs).

## 7. Results

- Baseline (`settings-tab-r1`): recorded in §3 of this file; to be appended to
  mk-improvements.md by the orchestrator.
- After runs: **pending** — two runs post-merge, taken by the orchestrator per §4.
- Wrapper smoke test (plan `qa`): **pending orchestrator smoke test** — one pane per agent
  kind, `git stash` refused with the guard line, `git status` works; a Claude pane whose shell
  snapshot re-prepends `/opt/homebrew/bin` counts as a finding, not a pass.
