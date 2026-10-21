+++
plan = "plan-inbox-and-parsing"           # plans.id
status = "running"                          # draft until the user approves once
created = 2026-10-20
runs = ["inbox-r1"]
design = ["https://claude.ai/artifact/XXBpu7L6u72V5SFrWebidn (canvas board project/Inbox.dc.html, slide play-6f7405e9eb0d)"]
integration = "main"                      # each phase merges to local main once its scoped gates pass; never pushed without the user's go
description = "Makes a plan travel one clean way: a plan document written from the template lands in an Inbox, is checked against the template, is imported once into the project store as phases and tasks, and from then on a run reads only the store and never the markdown file. It also adds the Inbox tab to the /mk dashboard, where each plan file shows how well it follows the template and can be imported, archived or deleted. Every Inbox step is recorded as an event with its own source, inbox, so it shows in the Events tab next to bus and run events."

[gates]
test = "claude plugin test ."
typecheck = "tsc -p . --noEmit"
selftest = "python3 scripts/mk-plan.py --self-test"
validate = "claude plugin validate ."

[[final_gate]]
id = "SUITE"
cmd = "claude plugin test ."
every = "run_end"
timeout = 1200
covers = ["TPL.1", "TPL.2", "UI.1", "UI.2", "STORE.1", "STORE.2", "INBOX.1", "INBOX.2", "EVT.1", "EVT.2", "RUN.1", "RUN.2", "WIRE.1", "WIRE.2"]

[[final_gate]]
id = "TYPES"
cmd = "tsc -p . --noEmit"
every = "merge"
timeout = 600
covers = ["UI.1", "UI.2", "EVT.2", "WIRE.1", "WIRE.2"]

[[final_gate]]
id = "LAYOUT"
cmd = "python3 scripts/mk-e2e-layout.py audit"
every = "merge"
timeout = 600
covers = ["STORE.1", "STORE.2", "RUN.1", "RUN.2"]

# ---------------------------------------------------------------- wave 1

[[phase]]
id = "TPL"
title = "The plan template and a per-section check"
description = "The template and the checker disagree in ten small places, and the checker cannot say which parts of a plan are present. This phase fixes the template wording, writes down the seven body sections an Inbox card counts and the four header parts beside them, and makes the check report each one as found or missing, so a file can be classed as following the template, partly, or not a plan."
wave = 1
deps = []
owned = ["skills/matrixkoder/templates/plan.md", "scripts/mk-plan.py"]
route = "claude sonnet medium"
reviewer = "agy gemini-3.8-flash-high"
size = "standard"
risk = "reversible"
review = "standard"
commit = "feat(plan): the check reports each template section and classes a file"
subagents = true

  [[phase.task]]
  id = "TPL.1"
  title = "Make the template say what the code does"
  description = "The template and mk-plan.py disagree: the archive file name, wording that still says a column is added later, a claim that MK never edits the file, task-level keys only shown on subtasks, and the description key missing from the key lists. This task makes the template true to the code and writes down what the seven sections are."
  acceptance = ["templates/plan.md names the archive copy <id>-v<n>.md, drops the 'added by D1' wording, and lists the task-level deps, parallel and route keys", "templates/plan.md states that a new plan is one with created on or after 2026-10-20, that the example created date is a placeholder (mk-plan.py --self-test, which pins the old date, is updated with it), and that Method is mixed when phases differ on subagents", "templates/plan.md names the seven body sections (Summary, Goal, Design / context, Phases, Risks, Open questions, Log) and the four header parts (header phases, tasks, acceptance, final gate), and says which missing ones make a plan PARTIAL", "description is accepted as a phase, task and top-level key by mk-plan.py check without an unknown-key warning", "templates/plan.md states the file is the source only until import, and that status and runs live in the store afterwards"]
  refs = ["skills/matrixkoder/templates/plan.md:10", "skills/matrixkoder/templates/plan.md:12", "scripts/mk-plan.py:1204"]
  gate = { cmd = "python3 scripts/mk-plan.py --self-test", expect = 0, timeout = 300, lines = ["TPL.1.a2", "TPL.1.a3"] }

  [[phase.task]]
  id = "TPL.2"
  title = "mk-plan.py check returns a result for each section"
  description = "Today check returns one ok or an error list and never reads the body headings. This task adds a sections result and a fit class to the check output, derived from the parsed header and the body, with no change to what check accepts or refuses."
  acceptance = ["mk-plan.py check on a good plan returns sections with the seven body entries and parts with the four header entries, each found or missing, plus sections_found, sections_total (7) and missing", "check returns fit = template when all seven sections and all four parts are found, partial when the file has a valid header but some are missing (the missing names are listed), and not_a_plan when there is no +++ header or no phase or task at all", "a file that is not a plan (no +++ first line) returns fit = not_a_plan with exit 0 on a new --classify flag and keeps today's error without it", "check errors on a [[phase]] missing id, title, wave, owned or commit only when the plan's created date is on or after 2026-10-20; an older or undated plan gets a warning instead, description missing is always only a warning, and no existing plan is edited or backfilled", "a task with no acceptance, or with neither gate nor manual, or whose acceptance lines no gate proves, gets a warning only, never an error, for new and old plans alike; a task missing id or title is an error for a new plan only", "the existing check results and exit codes for every other case are unchanged", "check warns, never errors, when the Summary block's phase count, method (waves or single lane, subagents on, off or mixed), lanes or gate differ from the header, and when the Summary or Goal section is missing", "mk-plan.py approve prints a warning 'n unticked open questions' when the body's Open questions section has unticked `- [ ]` lines, and still approves; check never treats them as an error", "a self-test covers one file per class: template, partial (missing Tasks and Acceptance) and not a plan"]
  refs = ["scripts/mk-plan.py:401", "scripts/mk-plan.py:127", "scripts/mk-plan.py:388"]
  gate = { cmd = "python3 scripts/mk-plan.py --self-test", expect = 0, timeout = 300, lines = ["TPL.2.a1", "TPL.2.a2", "TPL.2.a3", "TPL.2.a4", "TPL.2.a5"] }

[[phase]]
id = "UI"
title = "The Inbox tab as a pure render"
description = "Builds the Inbox tab exactly as the design shows it: a header card, filter chips, and one card per plan file with its template badge and actions. It is a pure render against an agreed view type and a few callbacks, so it can be built before any data exists."
wave = 1
deps = []
owned = ["hooks/tabs/inbox.tsx", "hooks/tabs/inbox.fixture.ts", "hooks/tabs/inbox.test.ts", "hooks/tabs/kit.tsx", "hooks/tabs/kit.test.ts"]
route = "claude sonnet medium"
reviewer = "agy gemini-3.8-flash-high"
size = "standard"
risk = "reversible"
review = "standard"
commit = "feat(inbox): the Inbox tab render, filter chips and file cards"
subagents = true

  [[phase.task]]
  id = "UI.1"
  title = "The view type, callbacks and the card list"
  description = "Defines the contract the wiring phase fills (InboxView, one InboxFile per card, and the callbacks inboxAct and filter set) and draws the header card, the four filter chips and the cards with badge, note and age, as in the design. FilterChip moves from plan.tsx into the kit so both tabs share it."
  acceptance = ["hooks/tabs/inbox.tsx exports InboxView, InboxFile and InboxTab as a pure render: no $, no state reads, only the callbacks passed in", "each card shows the file name, a TEMPLATE, PARTIAL or NOT A PLAN badge, the fit note (n of 7 sections found, or missing: names, or no phase or task headings), and the age", "the filter chips Inbox, Template, Needs fixing and Archived show their counts, the picked one is on, and an empty filter shows the design's empty text", "button keys follow inbox-open-<id>, inbox-import-<id>, inbox-archive-<id>, inbox-restore-<id>, inbox-delete-<id> and inbox-chip-<filter>", "FilterChip is exported from hooks/tabs/kit.tsx and plan.tsx imports it from there with no change to the Plan tab's drawn output"]
  refs = ["hooks/tabs/todo.tsx:15", "hooks/tabs/plan.tsx:268", "hooks/tabs/kit.tsx:198"]
  gate = { cmd = "claude plugin test .", expect = 0, timeout = 1200, lines = ["UI.1.a1", "UI.1.a2", "UI.1.a3", "UI.1.a4", "UI.1.a5"] }

  [[phase.task]]
  id = "UI.2"
  title = "Per-card states: importing, done, failed, confirm"
  description = "The design gives each card a state below its text: a three-step Import progress, an Imported line with Open project, a failed line with Retry and Dismiss, and a delete confirm. This task draws those states and tests the narrow widths."
  acceptance = ["a card in state importing draws the step name, step n of 3 (Parse the template, Write the project, Write the tasks), a 20-cell bar and the line 'runs in the background'", "done (Imported as <project>, phase and task counts, Open project and Import again), failed (the failing step and reason, Retry and Dismiss) and confirm (Yes delete, Keep it) each draw as the design shows", "no drawn line is wider than the pane at 36, 40 and 46 columns", "a test presses every button and checks the callback receives the right kind and file id"]
  refs = ["hooks/tabs/todo.test.ts:161", "hooks/tabs/kit.test.ts:262"]
  gate = { cmd = "claude plugin test .", expect = 0, timeout = 1200, lines = ["UI.2.a1", "UI.2.a2", "UI.2.a3", "UI.2.a4"] }

# ---------------------------------------------------------------- wave 2

[[phase]]
id = "STORE"
title = "The store keeps a plan's phases and tasks as versioned rows"
description = "After approve, a plan's phases and tasks exist only as nested JSON and as task rows keyed to one run, overwritten on every re-approve, and import writes no tasks at all. This phase adds versioned plan item rows written by both import and approve, and makes the plan's status and run list live in the store so the markdown file is a source only until import."
wave = 2
deps = ["TPL"]
owned = ["scripts/mkdb.py", "scripts/mk-plan.py", "docs/state-schema.md"]
route = "claude sonnet medium"
reviewer = "agy gemini-3.8-flash-high"
size = "complex"
risk = "reversible"
review = "data-loss"
commit = "feat(store): versioned plan items; the plan file is a source only until import"
subagents = false

  [[phase.task]]
  id = "STORE.1"
  title = "A plan_items table, versioned, written by import and approve"
  description = "Adds the new plan_items(plan, version, id, parent, type, title, acceptance, owned, deps, parallel, route_json, extra_json, ord) table to the project store, reusing flat_items. Only the table is added; old plans and runs are not copied into it. From now on import writes the rows as a new version and approve stamps that version approved, so every new version keeps its tasks."
  acceptance = ["the project store gets the new plan_items table (created if missing, idempotent) and nothing else changes: no existing row is copied, converted or rewritten, and every existing plan still reads as before; a self-test opens an old store and checks that the old tables and rows are byte-identical", "mk-plan.py import writes one plan_items row per phase, task and subtask for the new version and bumps the plans version; re-importing an unchanged file writes nothing", "mk-plan.py approve marks that version approved and no longer deletes or rewrites older versions' rows", "mkdb.py has a reader that returns a plan version's items in order, and mkdb.py plans <id> --json uses it instead of header_json when rows exist", "the change report of an import lists added, changed and removed items versus the previous version", "import records one link to the file it came from (source = the path given to import) in the plan's header_json beside the sha, with no new column; the link is reference only, so nothing re-reads the file or takes data from the link, and the plan and its inbox file are one to one"]
  refs = ["scripts/mkdb.py:79", "scripts/mk-plan.py:936", "scripts/mk-plan.py:1104", "scripts/mk-plan.py:1486"]
  gate = [{ cmd = "python3 scripts/mkdb.py --self-test", expect = 0, timeout = 600, lines = ["STORE.1.a1", "STORE.1.a4"] }, { cmd = "python3 scripts/mk-plan.py --self-test", expect = 0, timeout = 600, lines = ["STORE.1.a2", "STORE.1.a3", "STORE.1.a5"] }]
  critical = true
  risk = 3

  [[phase.task]]
  id = "STORE.2"
  title = "Plan status and run ids live in the store"
  description = "set --status and add-run rewrite header lines of the markdown file today, and approve rewrites its status line. After this task the store row is the only place status and runs change, and the file is read once at import and never written."
  acceptance = ["mk-plan.py set --status and add-run change plans.status and the run list in the store and do not touch the file's bytes", "approve no longer rewrites the file; the sha of the imported file is stored on the version and the file stays as it was", "mk-plan.py export still returns the approved version byte for byte from the store", "a file plan with no store row keeps working through check and parse, with no write"]
  refs = ["scripts/mk-plan.py:733", "scripts/mk-plan.py:808", "scripts/mk-plan.py:923", "scripts/mk-plan.py:1352"]
  gate = { cmd = "python3 scripts/mk-plan.py --self-test", expect = 0, timeout = 600, lines = ["STORE.2.a1", "STORE.2.a2", "STORE.2.a3", "STORE.2.a4"] }
  critical = true
  risk = 3

  [[phase.task]]
  id = "STORE.3"
  title = "item set: edit one phase or task of a draft plan in the store"
  description = "Gives the Improve button on a draft plan's rows a place to write. mk-plan.py item set <plan> <id> changes one phase or task's title, description, acceptance, owned or deps in the plan_items rows of the plan's current draft version. It works only while the plan is a draft, so an approved or running plan is never edited this way."
  acceptance = ["mk-plan.py item set <plan> <id> --title/--description/--acceptance/--owned/--deps changes that one row of the current version in the store and prints one JSON line with ok and the changed fields; the plan file is not read or written", "the command refuses a plan whose status is not draft, an unknown plan or id, and a value that would break the plan (a dep that is not in the plan, a cycle), writes nothing on a refusal, and exits 2", "after an edit, mkdb.py plans <id> --json shows the new value, and the version number and every other row are unchanged", "the self-test covers a good edit of each field, the three refusals, and an approved plan refused"]
  refs = ["scripts/mk-plan.py:1881", "scripts/mkdb.py:1773"]
  gate = { cmd = "python3 scripts/mk-plan.py --self-test", expect = 0, timeout = 600, lines = ["STORE.3.a1", "STORE.3.a2", "STORE.3.a3", "STORE.3.a4"] }

[[phase]]
id = "INBOX"
title = "The inbox script: list, classify and act on plan files"
description = "A new script that the dashboard calls for everything the Inbox tab needs: list the plan files with their fit, and archive, restore, delete and import one. It reuses the TPL check for the classification and mk-plan.py import for the import, so no parsing is written twice."
wave = 2
deps = ["TPL"]
owned = ["scripts/mk-inbox.py"]
route = "claude sonnet medium"
reviewer = "agy gemini-3.8-flash-high"
size = "standard"
risk = "destructive"
review = "data-loss"
commit = "feat(inbox): mk-inbox.py lists, classifies, imports, archives and deletes plan files"
subagents = false

  [[phase.task]]
  id = "INBOX.1"
  title = "list --json: one entry per plan file"
  description = "Reads the inbox folder: every <topic>.md file in it is one plan (a folder holding a plan.md is still accepted, as shipped, and is not extended), and the plan id is the plan = line of its header, never the file name. Returns for each file its id, path, title from the H1, fit and sections found, the missing names, age, and whether it is archived. It reads files only and writes nothing."
  acceptance = ["mk-inbox.py list --json prints one JSON line with ok, a files array and counts for inbox, template, fix and archived", "each entry has id, path, title, fit (template, partial, not_a_plan), sections_found, sections_total, missing, age_s and archived", "a file with no +++ header is not_a_plan with the note 'no phase or task headings' or the parse reason, never an error exit", "list never writes or moves a file and finishes on an empty or missing inbox folder with an empty list", "the self-test builds a temp home with a template, a partial and a not-a-plan file and checks the three entries"]
  refs = ["scripts/mkpaths.py:337", "scripts/mk-plan.py:1409"]
  gate = { cmd = "python3 scripts/mk-inbox.py --self-test", expect = 0, timeout = 300, lines = ["INBOX.1.a1", "INBOX.1.a2", "INBOX.1.a3", "INBOX.1.a4", "INBOX.1.a5"] }

  [[phase.task]]
  id = "INBOX.2"
  title = "import, archive, restore and delete a file"
  description = "The four actions the card buttons call. Import runs mk-plan.py import in three reported steps. Archive and restore move the file (or folder) between inbox/ and inbox/archive/. Delete removes the file only after the user's confirm in the tab, and keeps a copy first."
  acceptance = ["mk-inbox.py import <id> runs mk-plan.py import into the current project's store and reports step 1 of 3 (parse the template), 2 (write the project) and 3 (write the tasks) as it goes; a failure names the step and the reason and writes nothing to the store", "import of a flat <topic>.md runs mk-plan.py import on that file and, after it succeeds, moves the file to inbox/archive/ so the inbox does not list it again (mk-plan.py is not edited here; STORE owns it in the same wave); a folder is imported as shipped", "archive <id> and restore <id> move the plan file (or folder) between inbox/ and inbox/archive/ and refuse when the target exists", "delete <id> copies the file (or folder) to inbox/.deleted/<id>-<timestamp>/ before removing it and refuses a path outside the inbox", "every action prints one JSON line with ok or errors and exits 0 or 2 like mk-plan.py", "the self-test covers each action and the refusals"]
  refs = ["scripts/mk-plan.py:1409", "scripts/mk-plan.py:1861"]
  gate = { cmd = "python3 scripts/mk-inbox.py --self-test", expect = 0, timeout = 300, lines = ["INBOX.2.a1", "INBOX.2.a2", "INBOX.2.a3", "INBOX.2.a4", "INBOX.2.a5"] }
  critical = true
  risk = 3

# ---------------------------------------------------------------- wave 3

[[phase]]
id = "RUN"
title = "A run reads the store only, never the plan file"
description = "Today run init takes a plan file path, re-parses the markdown, and writes status and run ids back into it, and several later steps read the file again. This phase makes run init take a plan id and version, copy the items from the store, and removes every later read of the markdown, then moves the runs that already exist."
wave = 3
deps = ["STORE"]
owned = ["scripts/mk-state.py", "child-skills/mk-builder/scripts/mk-builder.py", "scripts/mk-e2e-layout.py", "docs/state-schema.md"]
route = "claude sonnet medium"
reviewer = "agy gemini-3.8-flash-high"
size = "complex"
risk = "reversible"
review = "data-loss"
commit = "refactor(run): a run starts from a stored plan version and never reads the plan file"
subagents = false

  [[phase.task]]
  id = "RUN.1"
  title = "run init --plan <id> copies the stored items"
  description = "Replaces the path argument with a plan id and optional version. The run takes its tasks, gates and final gates from the plan_items rows, records plan_id and plan_version on the run, and stops writing plan, plan_sha and plan_status into the run."
  acceptance = ["mk-state.py run init --plan <id> [--version N] creates the run's tasks from plan_items and runs.plan_version holds the version used (never NULL)", "run init refuses a plan that is not approved unless --force, with today's message", "run init works with the plan file deleted from disk", "a path given to --plan is refused with a message telling the user to import the file first", "a plan that has no plan_items rows (approved before this change) is refused with the message 'import this plan again first'; the user re-imports the file and nothing is migrated or copied for them", "a new run no longer stores the plan file path or sha; the plan status shown on the board for a new run comes from the store"]
  refs = ["scripts/mk-state.py:1495", "scripts/mk-state.py:901", "scripts/mk-state.py:1258", "scripts/mk-state.py:613"]
  gate = { cmd = "python3 scripts/mk-state.py --self-test", expect = 0, timeout = 900, lines = ["RUN.1.a1", "RUN.1.a2", "RUN.1.a3", "RUN.1.a4", "RUN.1.a5"] }
  critical = true
  risk = 4

  [[phase.task]]
  id = "RUN.2"
  title = "Every other reader moves to the store"
  description = "Moves close_checks, plan_done_if_last, plan sync, backfill-fields and the builder's plan_tasks off the file, drops the plan_file lookup for new runs and rewrites the fixture builder. Runs that already exist are not migrated: they keep reading their plan file until they finish."
  acceptance = ["for a run with a stored plan version, close_checks, plan_done_if_last, plan sync and run backfill-fields read the plan version from the store and never open the markdown; a test deletes the file and runs each", "mk-builder.py plan_tasks lists a phase's tasks from the store rows", "plan gates are never silently skipped: a missing stored plan fails the close with a named error", "a run that already holds a plan file path keeps working through the old file path until it finishes; nothing is migrated, backfilled or rewritten for it, and a run with a stored plan version never reads the file", "mk-e2e-layout.py builds its fixtures through import and approve and run init --plan <id>, and its audit passes"]
  refs = ["scripts/mk-state.py:2357", "scripts/mk-state.py:2392", "scripts/mk-state.py:3173", "scripts/mk-state.py:3384", "child-skills/mk-builder/scripts/mk-builder.py:2504", "scripts/mk-e2e-layout.py:581"]
  gate = [{ cmd = "python3 scripts/mk-state.py --self-test", expect = 0, timeout = 900, lines = ["RUN.2.a1", "RUN.2.a3", "RUN.2.a4"] }, { cmd = "python3 scripts/mk-e2e-layout.py audit", expect = 0, timeout = 600, lines = ["RUN.2.a5"] }]
  critical = true
  risk = 4

[[phase]]
id = "EVT"
title = "Inbox events: a new source in the events table and the Events tab"
description = "Every Inbox step is recorded as an event with the source inbox, next to the bus and run sources. Inbox events have no run, which the event writer refuses today, and the Events tab reads one run only. This phase adds the writer and reader and the Events tab source, chip and filter."
wave = 3
deps = ["STORE", "INBOX"]
owned = ["scripts/mkdb.py", "scripts/mk-inbox.py", "hooks/tabs/events.tsx", "hooks/tabs/eventdetail.tsx", "hooks/tabs/events.fixture.ts", "hooks/tabs/events.test.ts", "hooks/tabs/eventdetail.test.ts", "docs/herdr-events.md"]
route = "claude sonnet medium"
reviewer = "agy gemini-3.8-flash-high"
size = "standard"
risk = "reversible"
review = "standard"
commit = "feat(events): an inbox source for plan inbox steps"
subagents = false

  [[phase.task]]
  id = "EVT.1"
  title = "The inbox channel: a writer without a run and a reader"
  description = "Adds inbox to the allowed channels, lets append_event write a row with no run for that channel only, adds a reader that returns inbox rows without a run, and makes mk-inbox.py emit one event per step."
  acceptance = ["mkdb.append_event accepts chan inbox with run NULL; every other channel still refuses a missing run, and a self-test pins both", "mkdb.py events --chan inbox --json returns the project's inbox rows with no --run", "mk-inbox.py writes an event for each of: found, checked, import-step (n of 3), imported, import-failed, archived, restored and deleted, with meta_json holding the file, plan id, fit and by", "inbox events never trigger the orchestrator wake: the wake reader still fetches only the state channel", "the events section of docs/herdr-events.md lists the inbox channel and its kinds"]
  refs = ["scripts/mkdb.py:1178", "scripts/mkdb.py:1182", "scripts/mkdb.py:1198", "scripts/mkdb.py:1353", "hooks/wake.ts:311"]
  gate = { cmd = "python3 scripts/mkdb.py --self-test", expect = 0, timeout = 600, lines = ["EVT.1.a1", "EVT.1.a2", "EVT.1.a3", "EVT.1.a5"] }
  critical = true
  risk = 3

  [[phase.task]]
  id = "EVT.2"
  title = "The Events tab shows the inbox source"
  description = "Widens the event source from bus or run to include inbox, adds an Inbox segment to the source filter, gives each inbox kind a chip word and colour, and makes the detail card show the file and plan id."
  acceptance = ["EventItem.src and NormalizedEvent.src accept inbox, and an inbox event is not given the run source by the default rule", "the source filter has an Inbox segment next to All, Bus and Run; picking it shows only inbox events", "each inbox kind has a chip word in SHORT and a role in TYPE_ROLE, and the kind list applies to inbox events", "the event detail card shows the file name and plan id for an inbox event", "events.fixture.ts has inbox rows and the Events tests cover the filter at 36, 40 and 46 columns"]
  refs = ["hooks/tabs/events.tsx:23", "hooks/tabs/events.tsx:112", "hooks/tabs/events.tsx:389", "hooks/tabs/eventdetail.tsx:44"]
  gate = { cmd = "claude plugin test .", expect = 0, timeout = 1200, lines = ["EVT.2.a1", "EVT.2.a2", "EVT.2.a3", "EVT.2.a4", "EVT.2.a5"] }

# ---------------------------------------------------------------- wave 4

[[phase]]
id = "WIRE"
title = "Wire the Inbox tab into the dashboard"
description = "Connects the tab to the data: the tab row and hotkey, the loader and refresher, the actions, the Events tab reading inbox rows, the settings and counts, and the docs."
wave = 4
deps = ["UI", "INBOX", "EVT", "RUN"]
owned = ["hooks/dashboard.tsx", "hooks/dashboard.test.ts", "hooks/inbox-pane.test.ts", "hooks/tabs/plan.tsx", "hooks/tabs/planrun.ts", "hooks/tabs/plan.test.ts", "hooks/tabs/planrun.test.ts", "hooks/wake.test.ts", "types/index.d.ts", ".claude-plugin/plugin.json", "hooks/settings-io.ts", "hooks/tabs/header.ts", "hooks/tabs/settings.schema.ts", "scripts/settings-drift.py", "README.md", "docs/howto.md", "docs/state-schema.md", "skills/matrixkoder/SKILL.md"]
route = "omp ollama-cloud/deepseek-v4.1-flash:1017"
reviewer = "agy gemini-3.8-flash-high"
size = "standard"
risk = "reversible"
review = "standard"
commit = "feat(inbox): the Inbox tab in the dashboard with its settings and docs"
subagents = false

  [[phase.task]]
  id = "WIRE.1"
  title = "The tab, the loader, the actions and the events read"
  description = "Adds the tab id, tab row entry and hotkey n, the refresher that calls mk-inbox.py list, the action handler for Open, Import, Archive, Restore and Delete, and the Events tab read that merges the inbox channel into the run's rows."
  acceptance = ["the tab bar shows n: Inbox after Waiting and before Todo, the MkDashTab union and the defaultTab option include inbox, and hiddenTabs inbox hides it", "opening the tab loads mk-inbox.py list once at once and then on the tick, only while the tab is open, with one read in flight", "Import, Archive, Restore and Delete call mk-inbox.py and reload the list; a failure shows the card's failed state, never a toast only", "the Events tab also reads chan inbox with no run and merges those rows, so an Inbox step shows with source inbox even when no run is active", "a wake test proves an inbox event does not wake the orchestrator"]
  refs = ["hooks/dashboard.tsx:113", "hooks/dashboard.tsx:442", "hooks/dashboard.tsx:1792", "hooks/dashboard.tsx:3323", "types/index.d.ts:119", "hooks/settings-io.ts:257"]
  gate = { cmd = "claude plugin test .", expect = 0, timeout = 1200, lines = ["WIRE.1.a1", "WIRE.1.a2", "WIRE.1.a3", "WIRE.1.a4", "WIRE.1.a5"] }

  [[phase.task]]
  id = "WIRE.2"
  title = "Settings, counts, drift check and docs"
  description = "Finishes the pieces every new tab needs: the Settings visibility toggle, the header count, the settings drift check, and the README and howto lines. The new chan value is added to the state schema doc."
  acceptance = ["the Settings Open on select and the tab visibility toggle list Inbox, and mockTabCounts has an inbox entry so the type check passes", "python3 scripts/settings-drift.py --check passes after --write", "README.md and docs/howto.md each describe the Inbox tab (hotkey n, hooks/tabs/inbox.tsx) in the form the Todo and Improve tabs use, with no version number", "docs/state-schema.md says chan is state, bus, note or inbox and describes plan_items and runs.plan_version", "CHANGELOG.md is untouched"]
  refs = ["hooks/tabs/header.ts:44", "docs/howto.md:63", "docs/state-schema.md:290"]
  gate = [{ cmd = "python3 scripts/settings-drift.py --check", expect = 0, timeout = 120, lines = ["WIRE.2.a2"] }, { cmd = "tsc -p . --noEmit", expect = 0, timeout = 600, lines = ["WIRE.2.a1"] }]

  [[phase.task]]
  id = "WIRE.3"
  title = "Improve button on the rows of a draft plan"
  description = "In the Plan tab, a plan whose status is draft shows an Improve button on each phase and task row (where Play shows once a plan runs). The press drafts a prompt from the row's own data and seeds the existing Improve card, as Play and Pause already do; nothing is sent until the user accepts, and the agent writes the change with mk-plan.py item set (STORE.3). No model-calling code is added."
  acceptance = ["a draft plan's phase and task rows draw an Improve button; a plan that is approved, running or done does not, and Play and Pause are unchanged", "pressing it calls the Improve card seed with a prompt from planrun.ts built from the row's id, title, description, acceptance, owned files and deps, naming mk-plan.py item set as the way to write the change, and sends nothing until the user accepts", "a press with no loaded detail or no Improve card does nothing harmful and keeps today's fallback", "plan.test.ts and planrun.test.ts cover the draft button, its absence on a non-draft plan, and the prompt text"]
  refs = ["hooks/tabs/plan.tsx:306", "hooks/tabs/planrun.ts:83", "hooks/dashboard.tsx:1425"]
  gate = { cmd = "claude plugin test .", expect = 0, timeout = 1200, lines = ["WIRE.3.a1", "WIRE.3.a2", "WIRE.3.a3", "WIRE.3.a4"] }
+++

# plan-inbox-and-parsing — one clean path from a plan document to the project store, with an Inbox tab

## Summary
- Phases: 7 — TPL template and check; UI the Inbox render; STORE versioned plan items; INBOX the inbox script; RUN a run reads the store; EVT inbox events; WIRE the dashboard wiring
- Method: waves · subagents mixed
- Lanes: each phase is its own lane (no lane_group is set); the three build lanes are described in Design / context
- Delivery: main · pushed: no
- Gates: SUITE = claude plugin test . ; TYPES = tsc -p . --noEmit ; LAYOUT = python3 scripts/mk-e2e-layout.py audit

## Goal
A plan document dropped in the Inbox is checked against the template, shown on an Inbox card, and imported once into the project store as versioned phases and tasks; a run then starts from the stored plan and never opens the markdown file again, and every Inbox step is an event with source inbox.
Why: today the plan file and the store drift apart, so a run depends on a file that can change or vanish, and plan documents have no clear way in.
From: the user's request of 2026-10-20 and the Inbox design (https://claude.ai/artifact/XXBpu7L6u72V5SFrWebidn, board Inbox, slide play-6f7405e9eb0d), after four read-only research passes. Agents, models and routes come after this draft: every phase has route = "" and reviewer = "".

## Design / context

### What is wrong today
1. The template and mk-plan.py disagree in ten places: the archive file name (template plan-v<n>.md, code <id>-v<n>.md), stale D1 wording, a claim that MK never edits the file while set, add-run and approve rewrite it, task-level deps, parallel and route not documented, description missing from the key lists, and others.
2. check never reads the body; it only looks for an H1. There is no per-section result, so TEMPLATE, PARTIAL and NOT A PLAN cannot be told apart. A file with no +++ header is a hard error, not a class.
3. import writes only a plans row (header, body); it writes no task rows. approve writes task rows keyed to one run id and overwrites them on re-approve; the plans table has one row per id, so old versions exist only as archive files.
4. run init --plan <path> runs mk-plan.py check and parse on the markdown file, writes status running and the run id back into the file, and copies tasks from the parsed file, not from the store. plan_version is never written. close_checks, plan_done_if_last, plan sync, run backfill-fields and the builder's plan_tasks read the file again; the board compares the file's sha. The only store use is a fallback to the archived copy when the file is missing.
5. The events table allows a NULL run, but the only writer, mkdb.append_event, refuses it, the allowed channels are state, bus and note, and the Events tab reads one run only and sets the source to bus or run.
6. The inbox folder exists as a path (<home>/inbox/<plan-id>/plan.md, mkpaths.py:337, paths.ts:281) and mk-plan.py import can read a folder from it (plan.md becomes the plan file, every other file goes to the plan's assets/), but nothing scans or classifies it. Decided 2026-10-21: the standard becomes one flat <home>/inbox/<topic>.md per plan; the folder form keeps working as shipped and is not extended.
7. There is no Workflows tab in the code (tab row today: overview, lanes, plan, usage, files, events, waiting, todo, improve, settings); the design's n: Inbox after Workflows therefore lands between Waiting and Todo. The hotkey n is free.

### Shape of the change
A plan file is checked and classed in the Inbox, imported into the store as versioned plan_items rows, and a new run copies its tasks from those rows. Files by phase: TPL templates/plan.md and scripts/mk-plan.py; UI hooks/tabs/inbox.* and kit.tsx; STORE scripts/mkdb.py and mk-plan.py; INBOX scripts/mk-inbox.py; RUN scripts/mk-state.py, mk-builder.py and mk-e2e-layout.py; EVT mkdb.py and the Events tab; WIRE hooks/dashboard.tsx, settings and docs.

Lane setup (three build lanes; agents and models set by the user on 2026-10-21: lanes A and B = claude sonnet medium, lane C = omp ollama-cloud/deepseek-v4.1-flash:1017; every reviewer = agy gemini-3.8-flash-high). A lane that runs across waves is described here, because a lane_group stays inside one wave:

1. Lane A, the store and runs (sonnet): TPL (wave 1), STORE (wave 2), RUN (wave 3). One chain through mk-plan.py, mkdb.py and mk-state.py, so it stays on one worktree to avoid overlap.
2. Lane B, the Inbox (sonnet): UI (wave 1), INBOX (wave 2), EVT (wave 3). UI and INBOX share nothing; EVT touches mkdb.py after STORE has merged.
3. Lane C, wiring (omp): WIRE (wave 4), after A and B have merged. It is the only phase that touches hooks/dashboard.tsx.
4. Inside each phase the builder hands tasks to subagents only where a phase says subagents = true (TPL and UI); the others stay with the lead because their tasks share one file and the store is data-loss review.

### Contracts
Phases own disjoint files and meet only here:

1. C1 — Sections. A plan has seven body sections, the seven `##` headings of the template: Summary, Goal, Design / context, Phases, Risks, Open questions, Log. The `# <plan id> — …` title line is checked separately (a warning when missing). Four facts from the header are reported beside them as parts: header phases (at least one [[phase]]), tasks (at least one task), acceptance (every task has some) and final gate (a final_gate with covers). TEMPLATE = all seven sections and all four parts found; PARTIAL = a valid +++ header but something missing, with the missing names listed (for example "missing: tasks, acceptance" or "missing: Risks"; "header phases" always means the [[phase]] blocks, never the Phases section); NOT A PLAN = no +++ header, or no phase and no task. The card note reads "n of 7 sections found" plus the missing names.
2. C2 — Check output. mk-plan.py check (with --classify) returns sections (name to found or missing), sections_found, sections_total, missing and fit (template, partial, not_a_plan). TPL.2 writes it; INBOX.1 reads it; the UI shows it.
3. C3 — Inbox entry. mk-inbox.py list --json returns {ok, files:[{id, path, title, fit, sections_found, sections_total, missing, age_s, archived}], counts:{inbox, template, fix, archived}}. INBOX.1 writes it; WIRE.1 maps it to InboxView.
4. C4 — Plan items. The store table plan_items(plan, version, id, parent, type, title, acceptance, owned, deps, parallel, route_json, extra_json, ord) holds every version's phases and tasks. import writes a version, approve marks it approved, a run copies from it. runs.plan_version is always set. STORE.1 writes it; RUN.1 reads it.
5. C5 — Event channel. chan = inbox, run NULL, kinds found, checked, import-step, imported, import-failed, archived, restored, deleted; meta_json = {file, plan, fit, by}. Only the inbox channel may omit the run. EVT.1 writes it; EVT.2 and WIRE.1 read it.
6. C6 — View. InboxView and InboxFile in hooks/tabs/inbox.tsx mirror C3, plus the callbacks inboxAct(kind, id) and a filter set. UI.1 defines it; WIRE.1 fills it.

### Out of scope
1. No migration or backfill of existing plans or runs: old runs keep the plan file path until they finish.
2. No Improve on the Inbox and no model pass over the plan file: a plan is improved after import, while it is a draft, row by row in the Plan tab (WIRE.3), writing to the store (STORE.3).
3. No Workflows tab, and no change to how a plan is written beyond the template.

## Phases

### TPL — The plan template and a per-section check
- Delivers: a template that matches the code, and a check that reports each section found or missing and classes a file.
- Needs first: nothing.
- Build order: TPL.1 (the wording, the seven sections and the four parts) then TPL.2 (the check); keep what check accepts and refuses unchanged and put the additions behind --classify.
- Hazards: mk-plan.py is shared with STORE, which runs in the next wave.

### UI — The Inbox tab as a pure render
- Delivers: the Inbox tab drawn as in the design, with every card state, against an agreed view type.
- Needs first: nothing.
- Build order: UI.1 then UI.2; follow Todo's tab pattern (`hooks/tabs/todo.tsx`, `todo.test.ts`) and promote FilterChip from `plan.tsx:268` to `kit.tsx`.
- Hazards: pure render only (rule C10): no $, no state reads; moving FilterChip must not change the Plan tab's output.

### STORE — The store keeps a plan's phases and tasks as versioned rows
- Delivers: versioned plan_items rows written by import and approve, plan status and run ids kept in the store, and mk-plan.py item set to edit one row of a draft plan.
- Needs first: TPL, because both phases edit mk-plan.py.
- Build order: STORE.1 (table, import, approve) then STORE.2 (status and runs in the store).
- Hazards: this changes how new plans are stored and the review depth is data-loss: only a table is added, and the existing plans (mk-caveman-lanes, mk-ponytail-hooks, wizards-redesign-r1 and the live store's plans) must read the same before and after, with nothing copied.

### INBOX — The inbox script
- Delivers: mk-inbox.py with list, import, archive, restore and delete for plan files.
- Needs first: TPL, for the classification it reuses.
- Build order: INBOX.1 (list, read-only) then INBOX.2 (the actions).
- Hazards: delete is destructive: copy to inbox/.deleted/ first and refuse paths outside the inbox; the dashboard confirms with the user and the script never asks.

### RUN — A run reads the store only
- Delivers: run init from a stored plan version, and every other reader moved to the store for new runs.
- Needs first: STORE, because the rows must exist before a run can copy them.
- Build order: RUN.1 (run init) then RUN.2 (the other readers); read `scripts/mk-state.py:1495` first.
- Hazards: mk-state.py is the hottest file, and the run that builds this plan must not depend on run init --plan <id> until RUN.1 has merged; old runs keep the file path and nothing is migrated, so both paths exist side by side for a while.

### EVT — Inbox events
- Delivers: the inbox channel in the events table and the Inbox source in the Events tab.
- Needs first: STORE (mkdb.py is edited after it) and INBOX (the events are emitted from mk-inbox.py).
- Build order: EVT.1 (writer, reader, emit) then EVT.2 (the Events tab).
- Hazards: inbox rows have no run, which the writer refuses today; the wake reader fetches only the state channel, so inbox rows never wake the orchestrator, and WIRE.1 pins that with a test.

### WIRE — Wire the Inbox tab into the dashboard
- Delivers: the Inbox tab live in the dashboard, with its settings, count and docs, and an Improve button on the rows of a draft plan.
- Needs first: UI, INBOX, EVT and RUN.
- Build order: WIRE.1 then WIRE.2; follow the Todo wiring (a TABS row, a render branch, a switchTab kick, a refresher that returns early when the tab is closed, a TabUi callback), then run settings-drift.py --write and --check.
- Hazards: hooks/dashboard.tsx is shared, and only this phase touches it.

## Risks
1. A new table disturbs existing plans — a data-loss review and a self-test that old rows are byte-identical, plus the LAYOUT final gate — STORE.1.
2. Old runs and new runs read plans in two ways until the old ones finish — a test that an old-style run still closes and a test that a new run works with the plan file deleted — RUN.1, RUN.2.
3. Delete in the Inbox removes a file from disk — a copy to inbox/.deleted/ first, the tab's confirm and path checks — INBOX.2.
4. Hot files are edited by several phases (mk-plan.py, mkdb.py, mk-state.py, dashboard.tsx) — the waves order them and no two phases in one wave own the same file — TPL, STORE, EVT, RUN, WIRE.
5. The sections list is used by the card, the check and the template and could drift — C1 fixes it at seven body sections plus four header parts, TPL.1 writes it into the template and TPL.2 tests one file per class — TPL.1, TPL.2.
6. The test suite is load-sensitive, since earlier gates went red under machine load and green on rerun — run gates on a quiet machine and rerun a red gate once before rework — all phases.

## Open questions
- [x] What are the seven sections the Inbox counts? Answered 2026-10-20 by the user through the template steps: the seven `##` headings (Summary, Goal, Design / context, Phases, Risks, Open questions, Log); tasks, acceptance, phases and gates are header parts reported beside them. See C1 and the Log.
- [x] Where does the Inbox folder live? Answered 2026-10-21 by the user: <home>/inbox/, and a plan is one flat <topic>.md file there, not a folder per plan (folders mean creating, parsing, managing and deleting a folder for every plan). The shipped folder form still works and is not extended. — decided INBOX.1 and what the tab says.
- [x] Which project's store does Import write to, and which project's events table gets the inbox rows? Answered 2026-10-21 by the user: the inbox lifecycle ends at import and the plan's lifecycle starts there, so they do not overlap; Import writes the plan into the project the dashboard is opened in, the inbox events of that import go to the same project, and from then on everything is read from the store; the plan keeps only one link to its inbox file. — decided INBOX.2 and EVT.1.
- [x] Should an Inbox event with no run be a NULL run on chan inbox, or attach to a synthetic run? Answered 2026-10-21 by the same reasoning (inbox steps happen before any run exists): NULL run on chan inbox, no synthetic run. — decided EVT.1.
- [x] What does Improve do in the Inbox? Answered 2026-10-21 by the user: nothing; improving a whole plan file is too much effort and the file is not the thing to improve. The plan is imported first and stays a draft until it is set to run; in that draft status each phase and task row in the Plan tab shows an Improve button that seeds the existing Improve card, and the change is written to the store. — decided WIRE.1, UI.2, and added STORE.3 and WIRE.3.
- [x] Is the tab placed after Waiting and before Todo (no Workflows tab exists in the code)? Answered 2026-10-21 by the user: yes, between Waiting and Todo; it matches the design and keeps the Inbox close to the Plan tab. — decided WIRE.1.
- [x] After this plan, is a path to run init --plan refused, or still accepted by importing on the fly? Answered 2026-10-21 by the user: refused, with a message to import the file first; a run reads only the store and the markdown file is never read again after import. — decided RUN.1.
- [x] Agents, models, efforts and reviewers for each phase? Answered 2026-10-21 by the user: see the Log entry of that date and each phase's route and reviewer lines. — decided routes before approval.
- [x] What does the Summary's Lanes line list, how is Method written, what is Gates, and what is a new plan? Answered 2026-10-20 by the user after the Opus review: Lanes lists lane_group values with their phases and every other phase is its own lane (cross-wave chains go in Design / context); Method is waves | single lane with subagents on | off | mixed, and waves means more than one wave value; Gates lists each final gate as id = cmd separated by ;; a new plan has created on or after 2026-10-20. See the Log.

## Log
- 2026-10-21 — open question 7 and routes, decided by the user after Jev routing (no fallback): builders TPL, UI, WIRE = agy gemini-3.8-flash-high; STORE, INBOX, RUN = claude opus high; EVT = claude sonnet high (user override: its size call was low confidence); reviewers TPL, UI, EVT, WIRE = claude sonnet medium; STORE, INBOX, RUN = agy gemini-4-argon-high (user pick, a different family than the opus builders; effort high chosen by the assistant, user named the model only; not in the user's agy picks, which lists only gemini-3.8-flash-medium and -high) — user
- 2026-10-21 — open question 6, decided by the user: run init --plan refuses a file path with a message to import the file first; a run reads only the store (RUN.1 stays as drafted) — user
- 2026-10-21 — open question 5, decided by the user: the Inbox tab (hotkey n) sits between Waiting and Todo, as the design implies — user
- 2026-10-21 — open question 4, decided by the user: no Improve on the Inbox and no pass over the plan file; a plan is improved after import, while it is a draft, with an Improve button on each phase and task row in the Plan tab that seeds the existing Improve card; the change is written to the store, so STORE.3 (mk-plan.py item set, draft plans only) and WIRE.3 (the button) were added, and the Inbox lost its Improve button, improving card state and improved event — user
- 2026-10-21 — open questions 2 and 3, decided by the user: the inbox lifecycle ends at import and the plan's lifecycle starts there, so inbox events and the plan do not overlap; Import writes into the project the dashboard is opened in; inbox events are NULL-run rows on chan inbox in that project; the plan and its inbox file are one to one, the plan keeps one reference-only source link in header_json (STORE.1), and everything else comes from the store — user
- 2026-10-21 — open question 1, decided by the user: the inbox stays at <home>/inbox/ but a plan is one flat <topic>.md file, not a folder per plan; the shipped folder import keeps working and is not extended; the plan id is the header's plan = line; INBOX.1 and INBOX.2 and design point 6 updated; this file moved from inbox/plan-inbox-and-parsing/plan.md to inbox/plan-inbox-and-parsing.md — user
- 2026-10-20 — template review by an Opus agent (SHIP AFTER FIXES), fixes applied by the user's picks: Lanes, Method (mixed), Gates and the new-plan cutoff defined in the template; stale code claims (archive name, D1 wording, "MK does not edit the file", import writes tasks) corrected; five nits fixed; this plan's body reshaped to the template; C1 renamed the header parts "header phases, tasks, acceptance, final gate"; not applied: the Hazards-vs-Risks overlap, the one-line Open questions guidance, the cut of [gates.c1], LAYOUT and the header_json endings (user: leave for now) — user
- 2026-10-20 — drafted from four read-only research reports and the Inbox design; no routes set.
- 2026-10-20 — template step 8, decided by the user: the seven sections are the seven body `##` headings (Summary, Goal, Design / context, Phases, Risks, Open questions, Log); the title line is checked as a warning; phases, tasks, acceptance and gates are four header parts reported beside them; C1, the card text and TPL.1/TPL.2 updated, and the first open question closed — user
- 2026-10-20 — template step 7, decided by the user (step 1 choices): a Summary block of five fixed lines (Phases, Method as fixed choices, Lanes, Delivery, Gates) sits right under the title, then Goal (what is true when done, why, where it came from); the check compares the Summary with the header as a warning only — user
- 2026-10-20 — template step 6, decided by the user: the Log holds decisions up to approval, one line each (date — what — who), append only; after approval the run records decisions as events and the file is not edited. No new code, no ids, no check — user
- 2026-10-20 — template step 5, decided by the user: Risks are numbered "what can go wrong — the guard — the phase or task"; Open questions are `- [ ] question — what it decides`, and approve only WARNS about unticked ones (no block); the design key takes paths or links, with files going in the plan's inbox folder. The approve warning is in TPL.2.
- 2026-10-20 — template step 4, decided by the user: tasks stay in the header only (no body tasks section); required for a new plan are id and title, while acceptance and one of gate or manual are advised and only a warning when missing (the error list stays id, title, wave, owned, commit); subtasks get one short mention. Noted in TPL.2.
- 2026-10-20 — decided by the user (option 2): STORE.2 is left as written. An old run's plan file status may differ from the store after set --status stops writing the file, so `plan sync` on an old run may report a status difference. Accepted: old runs end soon, nothing breaks, no extra file-write exception.
- 2026-10-20 — three consistency edits, decided by the user after a check for leftover backfill assumptions: RUN.1 refuses a plan with no plan_items rows with "import this plan again first" (re-import, no migration); RUN.2's first acceptance line applies only to a run with a stored plan version; RUN.1's last line says "a new run".
- 2026-10-20 — scope cut, decided by the user: RUN.2 no longer migrates or backfills existing runs (they keep the plan file path until they finish); STORE.1 only adds the new plan_items table and copies no old data. Reason: the project is at the start with one user; good enough now, no migrations for small benefit.
- 2026-10-20 — template step 3, decided by the user: "Phase notes" is renamed "Phases" (four fixed lines per phase: Delivers, Needs first, Build order, Hazards); every [[phase]] must carry id, title, description, wave, deps, owned and commit, and a missing one becomes a check error. Counted before enforcing: of 67 existing plan files with phases, 63 miss at least one (62 miss description, 1 misses commit); user decision on enforcement: the check errors only on id, title, wave, owned and commit, and only for a new plan; description stays a warning; old plans are left alone and are NOT backfilled (good enough now, not perfect from day 1). Recorded in the user's long-term memory as a standing rule.
- 2026-10-20 — template step 2, decided by the user: Design / context has four parts (What is wrong today, required only when behaviour changes; Shape of the change; Contracts; Out of scope).
- 2026-10-20 — template step 1, decided by the user: the Summary is a body block right under the H1 (card and reader see it first); Method is fixed choices (waves, single lane, subagents on or off), not free text, so the check can compare it. C1 (the seven sections) is being redone section by section and is not final.
- 2026-10-21 — routes reset by the user: lane A (TPL, STORE, RUN) and lane B (UI, INBOX, EVT) = claude sonnet medium; lane C (WIRE) = omp ollama-cloud/deepseek-v4.1-flash:1017; all reviewers = agy gemini-3.8-flash-high. This replaces the Jev routes in the entry above. The user confirmed the model: DeepSeek V4.1 Flash 1017 = ollama-cloud/deepseek-v4.1-flash:1017 (listed by omp --list-models under provider ollama-cloud; lane C runs in omp, not OpenCode), and confirmed claude sonnet medium is enough for every Sonnet phase including STORE and RUN.
