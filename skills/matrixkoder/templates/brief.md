# <PHASE> — <one-line goal>

Repo: this folder (worktree `<path>`, branch `<branch>`, at `<sha>`: <what is already in>). Stack: <stack>. Plan `<plan id>`: `<data>/plans/<plan id>.md` (read-only) — phase `<PHASE id>`, its tasks `<P.n>`, `<P.n>` (acceptance lines in the plan header); history for this task: `<data>/runs/<id>/tasks/<T>/` (read-only). Do ONLY <PHASE>. Other agents work on: <lanes + their areas> — do not touch them.

## Spec
<what to build, as numbered parts. Point to files with `path:line`; point to mockups by path; state the API contract the code must use. Never paste code.>

## Files you own
<list / globs>
Do NOT touch: <list>

## Tests (must exercise real code)
<required tests, stated as acceptance ("Back with a dirty draft + Cancel keeps the draft"). Mock only the network edge (fetch / engine client), never the module under test. For every new client call, the server route must exist and be tested. Each fix/guard test must fail on the old code: <which tests>; prove it with the mk-builder skill's proof method (reference/lane-rules.md R13) — follow it as written.>
The plugin test runtime cannot read files or spawn processes, so a TS test cannot check drift against a script directly: commit a generated JSON beside the fixture and add a `scripts/*-drift.py --self-test` that checks it against the real sources.
Test guidance (mk-store-r1; keep this short):
- A fixture of an external command (a CLI, a bus route, a tool) is CAPTURED from the real command, with its source noted, never hand-written. D8B called `mk-bus.py pending --json`, a flag that does not exist, and the tests passed on a hand-written payload.
- A timing bound scales from a measured baseline (one spawn plus one round trip) or counts calls. A fixed wall-clock second failed 1 run in 3 under load (B1, W1, X1, D7).
- Show each new test FAILING when the thing it checks is broken (a mutation, or the old file via `git show <sha>:<path>`). A test that passes on the old code is a regression guard and must say so. A1 `_tool_block` was dead code with green tests.
- The plugin test runtime cannot read files or spawn processes (todo 29): inject the runner, and check a committed generated fixture with `scripts/*-drift.py --self-test` against the real sources.
- Run the full suite from the python3 on PATH (3.14, not a 3.9 system python: no `tomllib`). Name any failing suite from the output file before reporting.

## Real inputs
<Name the REAL fixtures the tests must use: a copy of real run directories, real pane footers or API responses, the production code path with environment variables unset, forward references from the real plan. Require at least one test that runs the production path. Made-up data alone is not enough when real data exists.>

## Commands (wait for each)
- Install: `<cmd>`
- Tests: `<the self-tests of the changed scripts only, one command each, e.g. python3 scripts/<name>.py --self-test>` (known unrelated failures: <list or none>). Not the full suite unless this line says so; mk-builder skill R33.
- Types: `<typecheck cmd>` must stay at **<baseline>**; nothing new in <owned paths>
- Lint/format ONLY changed files: `<lint cmd> <files>`, `<format cmd> <files>` — never repo-wide
- Banned commands (repo list): <banned commands>
- A check over ~2 min runs via `mk-builder.py check --name <n> -- <cmd>`: it starts detached, returns at once, and its result comes back to you as a typed line in your pane (and to the orchestrator over the bus); post progress, then end your turn or keep working, and resume when that line arrives (`mk-builder.py inbox`). Shorter checks: start each finite check detached with its output in a file and the exit code appended (`cmd > out.txt 2>&1; echo EXIT:$? >> out.txt`), then in the SAME turn wait with a bounded loop (`until grep -q '^EXIT:' out.txt; do sleep 5; done; tail -n 5 out.txt`, upper bound 20 minutes; print a message if it expires) and never end a turn while a job you started is still running; read every result before committing, quote its last line, never start servers.
- Run the full suite once before DONE and quote its summary line (the `selftest: N run, M failed` line the repo's suite prints). A red full suite means the round is not done even if the scoped tests pass.

## Parallel parts (default ON)
<Name the separable parts and who takes them, for example "tests and fixtures: helper 1; docs: helper 2; the shared hot file and the integration: you". The rules for helpers (todo list first, only the lead runs the gates, the report section) are in the mk-builder skill, reference/lane-rules.md R9. Write "none, because <reason>" only when the plan turns the flag off. omp is a lane agent like any other (Codex, OpenCode, AGY, Claude), but its helpers return without writing files (todo 97), so an omp brief always writes "none" here until that is fixed.>

## Rules
- Use the mk-builder skill (installed in this lane at `.claude/skills/mk-builder/`) — round R<n>. Every lane rule is the skill's (`reference/lane-rules.md` R1-R29); never restate one here. This brief carries only the per-brief values: the round number <n>, and the end-of-round token `<PHASE> R<n> DONE` (or `<PHASE> R<n> BLOCKED: <question>`) as the LAST line of your reply, alone on its line.
- Commit: `<type(scope): message>` subject, blank line, `Co-Authored-By: <agent/model> <email>`.
- Report: write the report to `<report path in this lane>`.
- After each subtask and each test run, run `mk-builder.py todo done <id>` and post `mk-builder.py progress "<subtask>: <result>; tests <passed>/<failed>"` so the orchestrator sees progress without asking; reply to an orchestrator question the same way (a lane has no answer command).
- No skill support in your runtime? Read `.claude/skills/mk-builder/SKILL.md` (in this lane) and follow it.

## TAKEOVER (only when another agent stopped mid-phase)
You inherit uncommitted work — KEEP it and build on it:
- Changed files: <git status --porcelain snapshot>
- Last known gates: <counts / failing names>
- Do, in order: <ordered finishing steps>. Do not start over.

## FIX (only for review findings)
Findings: `<data>/runs/<id>/tasks/<T>/review-rN.md` — cite their numbers in the report.
<numbered findings with acceptance per finding; same files/commands/rules as above>

## LEAD (only when phases share a lane_group)
This lane holds several phases of one plan, worked in order: <phases, in order>. You are the lead; how a lead works each phase — its checks, per-phase commits, per-phase tokens — is the mk-builder skill's lead rule (`reference/lane-rules.md` R14).
