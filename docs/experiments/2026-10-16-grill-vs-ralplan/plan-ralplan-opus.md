+++
# Ralplan consensus: Critic APPROVE at iteration 5 of 5. Output of a stress-test of plan.md. Pending plan-owner approval; not imported.
# Skills already written: skills/mk-ponytail, skills/mk-ponytail-review, skills/mk-ponytail-debt (untracked; commit them before H1).
plan = "mk-ponytail-hooks"
status = "parked"
created = 2026-10-15
runs = []
design = []

[gates]
selftest = "python3 scripts/mk-lane.py --self-test"
builder_selftest = "python3 child-skills/mk-builder/scripts/mk-builder.py --self-test"
config_selftest = "python3 scripts/mk-config.py --self-test"

[[final_gate]]
id = "F1"
cmd = "python3 scripts/mk-lane.py --self-test"
expect = 0
every = "merge"
covers = ["H1", "H2"]

[[final_gate]]
id = "F2"
cmd = "python3 child-skills/mk-builder/scripts/mk-builder.py --self-test"
expect = 0
every = "merge"
covers = ["H2", "H3"]

[[final_gate]]
id = "F3"
cmd = "python3 scripts/mk-config.py --self-test"
expect = 0
every = "merge"
covers = ["H2"]

[[final_gate]]
id = "F4"
cmd = "python3 scripts/mk-state.py --self-test && python3 scripts/mk-report.py --self-test"
expect = 0
every = "merge"
covers = ["H4"]
+++

# Goal

Install the mk-ponytail ruleset into a lane so the lane agent follows it, without the orchestrator restating it in a brief. Mode and review work on every host; the omp hook is the enforcement layer on top.

Settled already (grilling 2026-10-15; plan-owner answers 2026-10-16):

- Core 3 only: mode (`mk-ponytail`), review (`mk-ponytail-review`), debt (`mk-ponytail-debt`). No audit, gain, or help.
- Mode key is `session.ponytail.mode`: `off` | `lite` | `full` | `ultra`. Missing key means `full`. No `plugin.json` `userConfig` key.
- Debt ledger greps both `shortcut:` and `# ponytail:`.
- `templates/brief.md`, `templates/matrixkoder.yaml`, and `skills/matrixkoder/SKILL.md` are not edited by this plan.
- Hook source lives in a new dir outside the skill: `child-skills/omp-hooks/`.
- H3 trigger is `mk-builder.py commit` only. No omp `tool_result` hook.
- H3 is split: H3 (review trigger) and H4 (review count).

Changed by the review (flag to the plan owner): build order is now H2 (all hosts) before H0/H1 (omp hook). The owner's earlier text put the hook first; the Architect showed the hook alone reaches only omp and rests on an unverified API.

# Design

Defects found in plan.md by reading the code, and the fix each got:

1. **Hook install path.** `install_lane_skill` (`scripts/mk-lane.py:1072`, single caller in acquire ~`:1200`) does `shutil.copytree(SKILL_SRC, ...)` of `child-skills/mk-builder` into `.claude/skills/mk-builder` and `.omp/skills/mk-builder`. A hook placed under `child-skills/mk-builder/hooks/` would ship to Claude lanes and land at `.omp/skills/mk-builder/hooks/pre/` (not scanned by omp); that dir already holds `payloads/claude.json`. So the source moves to `child-skills/omp-hooks/pre/mk-ponytail.ts`, copied to `<lane>/.omp/hooks/pre/` by an extra step.
2. **Mode lives in the project yaml, not `.matrixkoder/matrixkoder.yaml`.** A lane worktree has no `.matrixkoder/` dir, and a migrated project resolves to `<data>/project.yaml` (e.g. `~/.matrixkoder/projects/matrixkoder/project.yaml`) via `mkpaths.project_yaml()` (`scripts/mkpaths.py`, `project_yaml`). The doc line in `skills/mk-ponytail/SKILL.md:10` names the old path and is fixed in H2 (that skill is new and untracked, so this is not an edit to the three frozen files).
3. **How the lane reads the yaml.** Lane config gives the data dir (`bus_lane_config` / `bus_data_dir`, as `todo_done_status` at `mk-builder.py:2471` does). But the lane's data dir can be the LEGACY dir (`<repo>/.matrixkoder`, holding a stale `matrixkoder.yaml` and no `project.yaml`), while `mk-config` writes the migrated `projects/<slug>/project.yaml`. Reading "whatever yaml the data dir holds" silently ignores the user's mode. So the lane must call `mkpaths.project_yaml(data)`. The `mkpaths_*` names in plan.md are functions in `mk-builder.py` (`:77-115`) that load `scripts/mkpaths.py`, which a lane copy lacks (`_paths` raises `FileNotFoundError` in a foreign lane; `bus_token_path` has the same flaw today). Fix in H2: `install_lane_skill` also copies `mkpaths.py` into both skill dirs, reusing the `copy_mkpaths` logic (`mk-builder.py:1113`). `session.ponytail.mode` is nested two deep, so the small reader finds `ponytail:` under `session:` then `mode:`, accepting both block form and flow form (`ponytail: { mode: x }`); a bare `mode:` match is wrong. It is a deliberate second reader, recorded as a shortcut (`# ponytail: minimal yaml reader, ...`); the full parser (`mk-config.py`: `flow_map`, `value_of`) is not shipped. Shipping it is the upgrade path.
4. **Config catalog and setter.** `mk-config.py` (~`:980`) refuses unknown keys, and its setter refuses to create a missing nested block or flow member (~`:590`, `:613`), while the real `project.yaml` has no `ponytail:` under `session:`. H2 adds the catalog row (default `full`, choices off/lite/full/ultra) AND makes `set session.ponytail.mode` work on a yaml with no `ponytail` block (extend the setter for this one key). Escape hatch if that setter work is not small: switch to a flat key `session.ponytail_mode` (one-line decision for the plan owner; it also drops the nested-form reader branches).
5. **Ruleset source, no drift.** The rules text lives only in `skills/mk-ponytail/SKILL.md`. In H2, `install_lane_skill` copies that file into BOTH skill dirs as `<skill>/reference/mk-ponytail.md`, so every host, and lanes in repos with no `skills/` dir, can read it. The hook (H1) carries no rules and adds no second sidecar.
6. **All-hosts reach (Architect).** `mk-builder.py ponytail` prints the filtered ruleset. A short line in `child-skills/mk-builder/SKILL.md` ("at session start run `mk-builder.py ponytail` and follow the output") reaches Claude, OpenCode, AGY, and omp, with no omp API. The omp hook then enforces the same call at `session_start`.
7. **Unverified.** `omp://hooks.md` was not readable. Hook discovery under `hooks/pre/`, the `session_start` event, and the context return shape come from plan.md and from the `pi.on("session_start", ...)` style in `~/.omp/agent/extensions/herdr-omp-agent-state.ts`. H0 proves it before H1.

# Contract

- `off` injects nothing: the reply is `{"ok":true,"text":""}`. A missing `session.ponytail` key means `full`.
- Unreadable yaml, unknown value, missing data dir (including a lane whose bus token file does not exist yet), or any failure: `ponytail` catches every error itself and replies `{"ok":true,"text":""}`, exit 0, one stderr line. It never raises `Fail`; `main()` always prints one JSON line. Never block a session.
- The hook and the subcommand never write the yaml and never write a flag file.
- Install is into every slot, inert unless the agent is omp. `install_lane_skill(lane)` does not know the host and slots are reused across agents, so "no `.omp/hooks/` for non-omp lanes" is not promised. Promised instead: `.claude/skills/mk-builder` gains exactly the SKILL.md pointer line, the new subcommand, `scripts/mkpaths.py`, and `reference/mk-ponytail.md`, and nothing else; Claude lanes load no omp file.
- A missing hook, rules, or `mkpaths.py` source is a warning that skips that step; the warning is appended to the acquire's existing warnings output (the list `acquire` already prints), and the self-test checks its exact text. Only the existing missing-skill and missing-guard sources stay hard `Fail`s. An optional feature must not block every acquire.
- R30 holds: nothing goes lane-to-lane; the review report goes lane → orchestrator on the bus.

# Risks

- The omp hook API is unverified (Design 7). H0 gates H1. If omp rejects `hooks/pre/*.ts`, fall back to the extension style under `<lane>/.omp/extensions/` and amend H1 only. H2/H3 are unaffected.
- The lane's data dir may be unreachable (fresh lane, bus down): H2 fails open to nothing. This is silent, so H2 self-test must run from a copied skill dir outside the plugin repo with a lane config, to prove resolution works where real lanes run. Self-tests inside the plugin checkout alone hide this.
- Rollback in `install_lane_skill` (`:1121`) uses `rmtree` and handles directories only. The new step creates files; its cleanup must `unlink` files and track them in `done`.
- Git exclusion proves nothing for `.omp/hooks/`: `ensure_lane_exclude` (`:598-610`) already excludes `.omp/`. Do not add an exclusion assertion as acceptance.
- A lane agent can ignore the injected rules. H3's review is the backstop.

## H2 — mode from the project yaml, all hosts

- `install_lane_skill`: copy `mkpaths.py` to `<skill>/scripts/mkpaths.py` (the resolver, `mkpaths_candidates` at `mk-builder.py:85`, looks only beside `mk-builder.py`; a copy at the skill root is never found) and `skills/mk-ponytail/SKILL.md` to `<skill>/reference/mk-ponytail.md`, in both skill dirs. mk-lane cannot import `copy_mkpaths` from `mk-builder.py`, so reimplement the copy. File-aware rollback, `fresh()` guard, warn-and-skip on a missing source (Contract). The return shape moves here from H1: `install_lane_skill` returns a 3-tuple today and cannot report a warning, so it gains a `warns` list (a 4th return value); `cmd_acquire` appends it to its own `warns`, which it emits as the `;`-joined `out["warning"]` string (`mk-lane.py` ~`:1231`; not `out["warnings"]`, which comes from config). Update the single caller (~`:1200`).
- Add `mk-builder.py ponytail`: lane config gives the data dir; `mkpaths.project_yaml(data)` gives the yaml (never "whatever the dir holds"); read `session.ponytail.mode` (block or flow form); apply the level filter (keep shared sections and only the chosen Levels row; same rule as upstream `filterSkillBodyForMode`); read rules from the lane's `reference/mk-ponytail.md`; print the text or nothing.
- stdout: `main()` prints exactly one JSON line. `ponytail` returns `{"ok": true, "mode": ..., "text": ...}` and the lane skill pointer says to read `text`; the hook wrapper parses that JSON. The self-test asserts one JSON line and that `off` gives empty `text`. Put the pointer line (before `## Reference`) in `child-skills/mk-builder/SKILL.md`; fix `skills/mk-ponytail/SKILL.md` line 10 (the yaml path) and line 3 (the description: it names `matrixkoder.yaml` and says "Activated by the lane hook"; reword to the project yaml and "the `mk-builder` pointer or the omp hook").
- Add the `mk-config.py` catalog row and the setter support for a missing `ponytail` block (Design 4).
- Self-tests (`mk-builder.py --self-test`): `off` gives empty `text`; missing key gives the `full` ruleset in `text`; `ultra` gives the ultra row and not lite/full; unknown value, missing data dir, or no token file gives empty `text`, exit 0, still one JSON line; a case that runs a copied skill dir under a temp path outside the repo, with no repo `skills/` dir, and still resolves the mode; a case with a legacy data dir holding a stale `matrixkoder.yaml` plus a migrated `project.yaml`, where the mode comes from `project.yaml`; block form and flow form both read. `mk-config.py --self-test`: the key validates, a bad value is refused, and `set` works on a yaml with no `ponytail` block. `mk-lane.py --self-test`: both skill dirs gain `scripts/mkpaths.py` and `reference/mk-ponytail.md` at exactly those paths; a missing source puts the exact text as a substring of the joined `warning` string (not list membership) and the acquire succeeds.

## H0 — spike: prove the omp hook contract

- In a scratch omp session (not a lane), place a minimal `.omp/hooks/pre/x.ts` that returns a fixed string on session start.
- Record in `<data>/plans/mk-ponytail-hooks/h0-note.md`: discovery path, event name, return field, one observed injection. Subagent coverage is recorded as observed or unknown; it is not a pass condition.
- No repo change. Acceptance: the note names event, return field, and one observed injection, or states the fallback to extension style.

## H1 — copy the omp hook into the lane (needs H0, H2)

- Add `child-skills/omp-hooks/pre/mk-ponytail.ts`: a thin wrapper that runs `python3 <lane>/.omp/skills/mk-builder/scripts/mk-builder.py ponytail --lane <lane>` (the lane is the session cwd; without `--lane`, `lane_dir` defaults to the process cwd and the token path hash misses from a subdirectory) parses its one JSON line, and returns `text` as session-start context only when `text` is non-empty; on any error or bad JSON it returns nothing.
- `install_lane_skill` gains the extra step: copy the hook to `<lane>/.omp/hooks/pre/mk-ponytail.ts` only (the rules already arrive via H2). Same `fresh()` guard (symlink and outside-slot refusal), overwrite on every acquire, file-aware rollback.
- Warnings use the channel H2 already added; H1 adds no new return shape.
- `mk-lane.py --self-test` asserts: the hook lands in an omp slot; a second acquire replaces it; a missing source warns (substring of the joined `warning` string) and installs nothing for this step while the skill and guard still install; every file the `.claude/skills/mk-builder` copy shares with `SKILL_SRC` matches byte for byte, and its extra files are exactly `scripts/mkpaths.py` and `reference/mk-ponytail.md`; the wrapper test (run the `.ts` with `bun`, which is on this machine, from a Python subprocess in the self-test; skip with a printed notice when `bun` is absent): empty `text` returns nothing, non-empty returns it, bad JSON returns nothing, and a start from a subdirectory still resolves via `--lane`; a copy failure leaves no half-installed file.

## H3 — `mk-builder.py commit` triggers the ponytail review (needs H2)

Flow, inside the one pane that owns the lane:

1. The pane agent commits and runs `mk-builder.py commit` (`cmd_commit_msg`, `mk-builder.py:3165`), which announces the sha on the bus first (unchanged).
2. After that, `commit` prints on stdout: (a) the `mk-ponytail-debt` result — a plain grep of the commit's diff for `shortcut:` and `# ponytail:`, no model; (b) one instruction line: start the `mk-ponytail-review` subagent in this pane on the sha. Both go in new JSON fields (`ponytail_debt`, `ponytail_next`) of the one-line reply, not extra stdout lines. When mode is `off` the fields are absent.
3. The pane agent runs the subagent in its own session, reads the report, applies the fixes itself.
4. The pane agent runs the existing `mk-builder.py note --kind ponytail-review --stdin` (`cmd_note`, `:3215`), adding `ponytail-review` to its `--kind` choices (`:3340`). It sends one bus `info` message whose first line is `ponytail-review round: <n>`, then the body (the body's first line is `sha: <sha>`). That one call is the only writer of the review event. On a failed send `cmd_note` appends the report to `.matrixkoder-ponytail-review-r<n>.md`, so the report is never lost. That name must be added to `fallback_names` (~`:451`, else `done` prints `stray lane file`, ~`:913`) and to the stale-file pattern `STALE_RE` (~`:143`, else the next round does not archive it); extend `t_z25_done_warns_on_stray_lane_files` for it. `<n>` is the task round, not a review index; the new `STALE_RE` pattern needs a round capture group (`archive_other_rounds` reads it), and a self-test checks an old-round `.matrixkoder-ponytail-review-r1.md` is archived when round 2 starts. The round report may mention it.

Rules:

- `commit` cannot spawn the subagent; the instruction line is the whole mechanism.
- Order: commit bus message, then review output. The sha must reach the orchestrator before a review can delay it.
- Fix-loop exemption: a ponytail review is not a fix round. It is recorded only as that `info` event and, in H4, the review count. The fix-loop counter is not touched because nothing in H3 increments it; no new enforcement code is written. The brief/review docs already say what a loop is.
- The orchestrator never edits the lane and never acts on the bus copy.
- Self-test: with mode `full`, `commit` prints the instruction line and a debt hit for a seeded `shortcut:` line; with `off` neither; `commit` fails open on a ponytail read error: the new fields are left out and the exit code is unchanged (a self-test case with an unreadable yaml); the existing bus message, fallback fields (`lane-file`, `bus-down`) and exit code are unchanged, including with the bus down; `note --kind ponytail-review` sends one `info` event whose first line is `ponytail-review round: <n>`; with the bus down it writes the fallback file with the full report; `commit`'s stdout stays one JSON line with the new fields.

## H4 — review count (optional, after H3)

- Re-grep `scripts/mk-state.py` for an existing per-task review counter. As of this review none was found.
- If absent: no new field. Count `events` rows (`chan='bus'`, `kind='info'`, same task, text starting `ponytail-review round:`) written by H3's `note --kind ponytail-review`; `SELECT COUNT(*)`, no schema change. Show it in `mk-report.py` (read its current per-task layout first). If a field already fits, use it and add nothing.
- Self-test: two `note --kind ponytail-review` events on one task read back as 2 in `mk-report.py`; zero reads as 0; an unrelated `info` event is not counted.
- Known limit: a review whose send fell back to the lane file, or any review in file-mode bus, writes no `events` row and reads 0. H4's report line says "store-recorded reviews", not "all reviews". Counting the fallback file is the upgrade path.
- Droppable without affecting H0-H3.

# Verification map

- `off` / missing key / `ultra` / fail-open / outside-repo resolution: H2 self-tests.
- Hook installs every acquire, replaced, file-aware rollback, missing source warns: H1 self-test.
- Hook injects in a real omp lane: H0 note plus one manual omp lane check at phase end. Not a CI gate.
- Review trigger, debt grep, `commit` unchanged on bus: H3 self-test.
- Count: H4 self-test.

# ADR

**Decision.** Ship H2 (all hosts), H0, H1 (omp enforcement), H3; H4 optional. Hook source in `child-skills/omp-hooks/`; mode and level filtering in `mk-builder.py ponytail`; review trigger in `mk-builder.py commit`.

**Drivers.** Rules reach lanes without brief text. One copy of the rules. Claude/OpenCode/AGY lanes keep working and also get the rules. A missing optional source must not block acquire.

**Alternatives considered.** (a) Hook source inside `child-skills/mk-builder/hooks/pre/`: rejected, wrong omp path and ships to Claude lanes. (b) Hook only, no skill pointer: rejected, reaches omp alone on an unproven API. (c) Skill pointer only, no hook: viable and the cheapest, kept as the fallback if H0 fails; the hook is added because an instruction can be skipped. (d) Mode logic in TS: rejected, third reader of the yaml. (e) Install only into omp slots by passing the agent to acquire: rejected for now, acquire does not know the agent and slots are reused. (f) Ship `mk-config.py` (the full parser) into lanes: rejected for now; only the small `mkpaths.py` ships, and the minimal reader stays a recorded shortcut. (g) omp `tool_result` hook on `git commit` for the review: rejected, omp-only, payload unverified.

**Why chosen.** Smallest set that reaches every host, isolates the one unverified dependency (omp hook API) in H0, and reuses `install_lane_skill`, the `todo_done_status` yaml pattern, and `cmd_commit_msg`.

**Consequences.** `install_lane_skill` grows an optional step. `mk-builder.py` gains a subcommand, a longer `commit` output, and a second, minimal yaml reader. `mk-config.py` gains one catalog row and setter support for a missing block. Per-acquire cost is two skill-dir file copies per skill dir (`mkpaths.py`, rules) plus the hook file in H1; the hook is installed into every slot and inert off omp.

**Follow-ups.** Pass the agent into acquire and install omp files only for omp; ship the full parser to lanes if more session keys are read there; `tool_result` safety net for raw `git commit`; review subagent model via `session.subagent_model`.
