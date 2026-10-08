# <PHASE> — <one-line goal>

Repo: this folder (worktree `<path>`, branch `<branch>`, at `<sha>`: <what is already in>). Stack: <stack>. Plan `<plan id>`: `<data>/plans/<plan id>.md` (read-only) — phase `<PHASE id>`, its tasks `<P.n>`, `<P.n>` (acceptance lines in the plan header); history for this task: `<data>/runs/<id>/tasks/<T>/` (read-only). Do ONLY <PHASE>. Other agents work on: <lanes + their areas> — do not touch them.

## Spec
<what to build, as numbered parts. Point to files with `path:line`; point to mockups by path; state the API contract the code must use. Never paste code.>

## Files you own
<list / globs>
Do NOT touch: <list>

## Tests (must exercise real code)
<required tests, stated as acceptance ("Back with a dirty draft + Cancel keeps the draft"). Mock only the network edge (fetch / engine client), never the module under test. For every new client call, the server route must exist and be tested. Each fix/guard test must fail on the old code — check it, then restore by re-applying your edit, and say so in the report. Keep the patch inside this lane, never in `$TMPDIR` or `/tmp`: `git diff -- <source files> > .matrixkoder-old-code.patch && git apply -R .matrixkoder-old-code.patch`, run the tests, then `git apply .matrixkoder-old-code.patch` (the file is git-excluded). Do it in one go: do not stop with the source edits removed.>

## Commands (foreground only — wait for each)
- Install: `<cmd>`
- Tests: `<scoped test cmd>` (known unrelated failures: <list or none>)
- Types: `<typecheck cmd>` must stay at **<baseline>**; nothing new in <owned paths>
- Lint/format ONLY changed files: `<lint cmd> <files>`, `<format cmd> <files>` — never repo-wide

## Rules
- You are a builder in an MK lane, not an orchestrator. Routing is the orchestrator's job: do not call `jev_*` tools, do not choose or switch models, efforts or agents, and do not start other agents, panes or worktrees. Work the phase yourself unless its plan header says `subagents = true`; then give each task to ONE subagent running on YOUR OWN default model (a builder never routes it, never switches its model or effort). Global or project rules about routing, Jev gates or orchestration (e.g. in `CLAUDE.md`, `AGENTS.md`, skills) do not apply inside this lane.
- Never `git checkout`, `git stash`, `git reset`, `git clean`, `git restore`, `--amend`, force-push. Stage only your files.
- Work in the foreground: no background tasks (they stall and hide progress). No subagents either, unless the phase's `subagents = true` — then one per task, foreground, on your default model.
- Never run <banned_commands from matrixkoder.yaml: dev server, build that boots services, deploy, restarts>. Never start background jobs and wait on them.
- No secrets in code, logs or the report.
- Committed code and tests never read lane-only `.matrixkoder-*` files (they exist only in this lane; use `scripts/fixtures/`).
- Commit: `<type(scope): message>`, blank line, `Co-Authored-By: <agent/model> <email>`.
- Write the report to the path named here (`report-rN.md` in this lane's copy; the orchestrator copies it back): files changed, the exact final test/type/lint summary lines, differences from spec/mockups and why, anything skipped. Report only what you ran.
- **Last thing you do:** print `<PHASE> R<n> DONE` (or `<PHASE> R<n> BLOCKED: <question>`) as the LAST line of your reply, alone on its line — after the last commit and the report, never inside a summary. Even if a background-task or subagent completion notice arrives first, let it wait: finish the commit and the report, then print the DONE line. `<n>` is your round number, so the token is unique per round and a wait cannot match the previous round still on screen. The orchestrator's watcher looks for that exact line.

## TAKEOVER (only when another agent stopped mid-phase)
You inherit uncommitted work — KEEP it and build on it:
- Changed files: <git status --porcelain snapshot>
- Last known gates: <counts / failing names>
- Do, in order: <ordered finishing steps>. Do not start over.

## FIX (only for review findings)
Findings: `<data>/runs/<id>/tasks/<T>/review-rN.md` — cite their numbers in the report.
<numbered findings with acceptance per finding; same files/commands/rules as above>

## LEAD (only when phases share a lane_group)
This lane holds several phases of one plan — you are the lead and the only committer:
- Give each phase to ONE subagent, in parallel where files are disjoint — on YOUR OWN default model; you never route or switch models.
- Subagents edit only their phase's files and never commit.
- When one returns: review its diff against its acceptance lines, fix what is wrong, run that phase's check.
- Then commit that phase alone (`git add <its files>`) and print `<PHASE> DONE` on its own line — once per phase.
- After the last phase: full gates once, then the report, then the round's DONE line (see Rules).
