# Changelog

All notable changes to this project. Format: Keep a Changelog; versions: SemVer.

## [0.4.0] - 2026-10-11

### Added
- **dashboard,settings:** rounded tab buttons, no inner padding, compact scope strip (eaba2d6)
- **dashboard,settings:** bordered tab and scope buttons, the picked one in blue (6d458df)
- **dashboard,settings:** full tab names at normal width; every Settings card gets a visible border (8f0d3c9)
- **dashboard:** open main tab sits on the tint fill like the Settings sub-strip (626c2fa)
- **settings:** S2c — artboard fills: selected-row highlight and bar, picked segment as tint text, field fill (10a91d9)
- **settings:** S2c — read-only bools as chips, flush select field, tests follow (2aa154d)
- **settings:** S2c — artboard labels, card order, usage under Agents, boxed fields, tinted toggle chip (6c1e408)
- **settings:** S2c — toggles draw as the artboard's tinted on/off chip (17ef67c)
- **settings:** S2c — artboard header on the Settings tab, find spacing, hotkey labels not doubled (5f4b32d)
- **settings:** S2c — card headers in muted ink, bordered mode cards, amber unsaved bar, red danger button (2915409)
- **settings:** S2c — boxed scope strip, find box, segmented strips, bar-left steppers, tighter cards (faae2ff)
- **mk-builder:** lane-agent skill, mk-builder.py status writer, git-guard wrapper (fa82aa7)
- **dashboard:** Settings tab reads and writes real config (b65e245)
- **config:** mk-config.py — one schema, safe yaml patcher, unified config homes (ea7ff7d)
- **dashboard:** Settings tab — scopes, typed rows, dirty footer, About (99c5f33)

### Fixed
- **settings:** S2c — text rows right-align their field (384167d)
- **settings:** S2c — card contents span the card so values right-align (d2b3838)
- **settings:** S2c — mode option typed as string for the header (9e57533)
- **settings:** S2b — drop stale cached Settings view and clear old save message on reload (6e01c22)
- **settings:** S2b — stale edits of read-only agents are never dirty or saved (78b29e6)
- **settings:** S2b — short seg words, real header on Settings, read-only picks for agents without a model list (e8565de)
- **mk-builder:** MB2 re-review r2 — attached short-option values never hide --amend (e99dc96)
- **mk-builder:** MB2 review r1 — guard catches long-option amend, safe ids, worktree exclude (2f1a0b7)
- **dashboard:** Settings tab review r1 — CLI errors shown, real focus hook, no migrate without yaml (387f522)
- **config:** S1 re-review r3 — real stalled-reply test, self-tests pinned to temp data (0c07f95)
- **config:** S1 re-review r2 — migrate emits valid yaml, reset gate trusts idle pane (607ab8e)
- **config:** S1 review r1 — CRLF-safe patcher, quoted comments, safe .bak, refuse duplicates, wizard keeps settings (878017d)
- **dashboard:** Settings tab review r1 — real row selection, dirty vs loaded value, width, typing hotkeys (f6ec0d5)

### Docs
- **mk-builder:** MB1 review r1 — role in identity, reader inputs match the watch, todo-list rule, citations (1d8cf51)
- **mk-builder:** status-file contract v1, lane-agent rules, git-wrapper contract (c2ee953)

### Other
- **settings:** S2c — assertions follow the new look (74f9f8b)
- **selftest:** run child-skills/*/scripts self-tests too (0230226)

## [0.3.0] - 2026-10-10

### Added
- **mk-state:** parent status fans out, status aliases, task verify (fb6af0f)
- **dashboard:** restyle Overview, Plan and Events tabs (b34d7f9)
- **dashboard:** restyle Lanes, Usage and Waiting tabs (0404354)
- **dashboard:** restyle Lanes, Usage and Waiting tabs (417d449)
- **dashboard:** restyle the Usage tab (a14cb09)
- **skill:** subagents default to true in new plans (589fb17)
- **dashboard:** restyle the Lanes tab (5d24f6a)
- **dashboard:** visual kit — palette, cards, chips, bars, Raster parts, controls, run header (2e63411)
- **dashboard:** visual kit — Raster parts (3122cf7)
- **dashboard:** visual kit — palette and native parts (e850c84)

### Fixed
- **wake:** never rewind the event cursor on hold give-up; test identical text after hold (3083796)
- **mk:** lane A review r2 — wake hold by content, no history rescan, dispatch marker by count (2f54675)
- **mk:** lane A review r1 — verify judges failures, strict dispatch marker, wake claim lease and text window, baseline parse, descendants (7cd1fba)
- **wake:** one poller across reloads, claim batches, drop stale transitions (dc8ba7e)
- **dispatch:** reviewer prompts keep task status; recheck delivery; skill stages 5-8 (0522605)
- **dashboard:** V1 review r1 — overview view-state keys, title budget, ui on all tabs, flush rule (3771afc)
- **dashboard:** V0 review r1 — tab hotkeys, ControlRow keys, empty strip, tone glyphs, TreeRow (98c6f1d)

### Other
- **mk-state:** restore executable bit lost in mk-fixes-1 lane A (c8dba4a)

## [0.2.0] - 2026-10-09

### Added
- **dashboard:** mock Lanes, Usage and Waiting tabs (be19ecc)
- **dashboard:** mock Overview, Plan and Events tabs (e8ec4b3)
- **dashboard:** tab shell with Old tab and stub mock tabs (2eaf342)

### Fixed
- **dashboard:** T1 review r1 — row-width tests, narrow layouts, colours (e797a4d)
- **dashboard:** T2 review r1 — wrap Waiting prose, usage header, colours (9b7852e)

### Docs
- **skill:** stage 5 dispatches via mk-lane-event.py dispatch; set child tasks, check ok (dbdb08a)

### Other
- **dashboard:** drop the stub-line assertion so T1/T2 can replace stub bodies (510e1a2)

## [0.1.0] - 2026-10-09

### Added
- **watch:** arm/dispatch/watch take --tasks for a lane group; a quota line is QUOTA, not STALL (ad4ac43)
- **dispatch:** delivered prompt marks the armed task's children building (823d3de)
- **state:** --force records who allowed it (abb6da8)
- **watch:** --no-commit mode for reviewer panes (c07f713)
- **state:** task round --undo for an empty round; phase task keeps reviewer (a8c1b2b)
- **plan:** reviewer key per phase; check is quiet about optional keys (ae77076)
- **watch:** verdicts move a phase's child tasks; mk-lane-event.py dispatch (2bf420b)
- **state:** runs built from plans — run init --plan, plan sync, plan links (d39e62b)
- **plan:** mk-plan.py — parse, check, set status, add run (912b834)
- **release:** mk-release.py + release skill — changelog, version bump, tag from Conventional Commits (c56ac73)
- **lanes:** mk-lane.py naming + reusable worktree slots; task keys branch/workspace/slot (69d2136)
- **wizard:** lanes block (reusable worktree slots) replaces worktree_pattern (9d5a70b)
- **hooks:** H2B dashboard sweep, herdr plugin link, ollama-cloud models (6d8a180)
- **herdr:** dispatch/watch on herdr events, usage.json lock, pane on task (f98fb94)
- **usage:** OpenCode pools per plan, project agents only, setup hints (c621096)
- **herdr:** plugin + mk-lane-event.py — event-driven lane verdicts (a85eeee)
- **mk-state:** enforce task deps, cross-run deps, parent roll-up, owned overlap warning (09e4a2a)
- **wizard:** 10-step setup pane, /mk setup|init opens it, inline text fallback (c9e8f83)
- **wizard:** pure core — detection parsers, gate proposals, YAML emitter, wizard draft types (c1fc8dd)
- **wake:** prompt.submit on lane events (286eea4)
- **tools:** mk_state + mk_usage model tools (de453d3)
- **mod:** status chip, transition toasts, resume compose section (0c8762c)
- **mod:** state atoms + /mk dashboard pane (variants, timers, usage poll) (7546da2)
- **guards:** tool.call guard for destructive git + banned commands (07fb8ca)
- **usage:** mk-usage normalises codexbar limits (2bcea71)
- **state:** mk-state run/task/event/lock CLI + lane-health/lane-watch fixes (7647226)
- **skill:** port stellar-orchestrator to MatrixKoder plugin (38665b4)

### Fixed
- **watch:** quota-before-stall reads only the pane footer, not the transcript (97a8f2e)
- **dispatch:** one automatic retry when the pane stays idle; raw herdr output kept (82bf771)
- **wake:** no duplicate prompts when submit fails on a busy session (b0a4581)
- **lanes:** lanes.copy and workspace id refreshed on every acquire (d5cd681)
- **plan:** set --status approved refuses a plan with check errors (98f5bb7)
- **wake:** skip transitions a later status change already reversed (a44d3da)
- **health:** OpenCode 'Free usage exceeded' is quota, retry time kept (33c3d5b)
- **watch:** a pane with a running subagent/task line is not stalled (2dc158e)
- **dashboard:** one row per shared lane; long titles truncated, status kept (1c38a83)
- **lanes:** copy only untracked paths, multi-child error test, dispatch timeout uses pane state (924dd11)
- **lanes:** real herdr workspace id, known run ids, task groups, slot copies, .omo/ (f91776b)
- **plan:** CRLF plans, run init validates before writing, keep file mode (d83a52b)
- **dashboard:** every active run shown, not only the newest (a363e3c)
- **dashboard:** /mk re-arms the refresh loop after /clear (5d0e5f0)
- **lanes:** mk-lane.py r4 — NUL-safe clean check, tracked dirs at any level, exact ref names (33a4619)
- **lanes:** mk-lane.py r3 — unreachable commits on remove, integration must be a branch, tracked dirs (d020c50)
- **lanes:** mk-lane.py data-loss guards (status config, slot paths, detached commits, ref args) (dcf4b73)
- **hooks:** no re-link when already linked; marketplace re-link note (966fe0f)
- **usage:** setup hint only for a plan pool without numbers (6fe3e84)
- **lane-watch:** finished lane without DONE token exits DONE after a grace (58c3d78)
- **wizard,usage:** review r1 — uncapped model merge, no opencode alias, detection order (2d30599)
- **mk-state:** review r1 — roll-up raise-only, wrong-shape siblings; docs: review sizing (4538764)
- **herdr:** keep the lock file on disarm (86ce646)
- **lane-health:** named quota window wins; Z.AI 5h reset read in provider time (0d8a724)
- **lane-watch:** no false STALL on working full-screen agents (26f1af8)
- **wizard,usage:** editable gates, more stacks, project-scoped usage pane (61ac40c)
- **mk-state:** review findings on task deps (09e4a2a) (a86b160)
- **lane-watch:** --commit-grep searches the bare subject, so ^ anchors work (9778420)
- **lane-watch:** pane output counts as activity; old-code patch stays in lane (75eb6ef)
- **mk:** handoff 2026-10-08 — task owner picks the run, provider model lists, mandatory MK (c2855f3)
- **mod:** r4 — safe wizard writes, data-dir via projects.json, hotkey/input focus (cd32f2b)
- **mod:** r3 — wizard hotkeys, setupHint, wake data dir + reload safety (c737f17)
- **guards,wake,tools:** allow read-only stash forms; strict types (b0f81e3)
- **mod:** live-pane fixes r2 + strict types (8471db3)
- **usage:** keep only pane-text exhausted_until; recompute codexbar's own each poll (4a13c4d)
- **wake,tools:** review r1 — validated wake prompt, first-enable baseline, durable cap (dce3cc3)
- **mod:** review r1 — resilient refresh, data-dir rule, /mk args, timers (8165769)
- **mod:** wire tools + wake; matcher on tools session.start; wake state in types contract (289022a)
- **guards:** ban branch -D, rebase, reflog expire, ref-deleting pushes (acbd368)
- **usage:** agent exhausted follows primary pool (52fc4f8)
- **guards:** close review bypasses (a3968a7)
- **usage:** per-pool limits for agy and claude (8112bca)
- **state:** global --data/--by/--run before subcommand (e8df151)
- **state:** review r1 findings (caaca0d)

### Docs
- **skill:** reviewer pane at stage 8, release at stage 12, lane tools as built (8a97e48)
- **brief:** DONE line always, shared-lane lead form, lane-only files never read by code (87c00b2)
- **plan:** TOML plan template, plan-driven stages 0-2, lane layout rule (544e331)
- **brief:** builders never route — no jev_*, no model or agent choice (94d3044)
- **skill:** lanes r2 — slot reuse across rounds, start once (aa8dcb4)
- **skill:** lanes via mk-lane.py — names, reusable slots, safety exception (add7827)
- **skill:** D1 r3 — report copy-back precondition, detect_changes needs integration checkout (0401e95)
- **skill:** fix D1 review — lane clean check, impact via trace_path, one index, shorter cells (ba475d3)
- **skill:** lane cleanup, mk-usage drift, codebase-memory recipe, parallel jev_decide (dff7359)
- **state-schema:** pane key, per-pool quota marks, usage.json lock (ccd8a1f)
- **lessons:** Jev routing echo, split votes, provider model ids, MK-only dispatch (30-33) (d5682c1)
- **routing:** Jev picks the route from a dated model menu (d22bfaf)
- README for users + docs/howto.md (d57b3f0)
- **skill:** route small Claude jobs to Haiku 5.5; lesson 29 (fe3e01e)
- **design:** fix1 — unique glyphs, usage pools, OpenCode-style column (7a068c9)
- **plan:** worktree lanes from P4 (d6bec3e)
- **design:** MatrixKoder sidebar dashboard spec (687c6c1)
- **plan:** plugin source moves to ~/Development/matrixkoder (91c48bb)
- **spikes:** P0 mod API feasibility findings (5826f95)

### Other
- **plan:** mk-plan self-test reads a fixture plan, not a lane-only file (c6dfdf3)
- **usage:** race self-test resets relative to now (41bfa76)
- ignore the repo-local .matrixkoder/ data dir (24eb31c)
- **mk-state:** event --task picks the owning run (fec2cea)
- **wizard:** detection, navigation, review writes, keep/replace, no API key, inline fallback (8b31355)
- **mod:** autoOpen/chip kill-switch coverage (1142cb0)
- **mod:** pane variants, chip/toasts and compose coverage (8928c2c)
- ignore .serena/ (05eaf2f)
- ignore .omc/ (5416078)
- baseline import from stellar-orchestrator skill (7b68eb0)
