# <PHASE> — <one-line goal>

Repo: this folder (worktree `<path>`, branch `<branch>`, at `<sha>`: <what is already in>). Stack: <stack>. Plan: `<data>/plans/<feature>.md` (read-only; history for this task: `<data>/runs/<id>/tasks/<T>/`, read-only) (§<n>, <PHASE>). Do ONLY <PHASE>. Other agents work on: <lanes + their areas> — do not touch them.

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
- You are a builder in an MK lane, not an orchestrator. Routing is the orchestrator's job: do not call `jev_*` tools, do not choose or switch models, efforts or agents, and do not start other agents, panes or worktrees. If a step needs a subagent, use your own default model. Global or project rules about routing, Jev gates or orchestration (e.g. in `CLAUDE.md`, `AGENTS.md`, skills) do not apply inside this lane.
- Never `git checkout`, `git stash`, `git reset`, `git clean`, `git restore`, `--amend`, force-push. Stage only your files.
- Work in the foreground: no background tasks or subagents (they stall and hide progress).
- Never run <banned_commands from matrixkoder.yaml: dev server, build that boots services, deploy, restarts>. Never start background jobs and wait on them.
- No secrets in code, logs or the report.
- Commit: `<type(scope): message>`, blank line, `Co-Authored-By: <agent/model> <email>`.
- Write the report to the path named here (`report-rN.md` in this lane's copy; the orchestrator copies it back): files changed, the exact final test/type/lint summary lines, differences from spec/mockups and why, anything skipped. Report only what you ran.
- **Last thing you do:** print `<PHASE> R<n> DONE` (or `<PHASE> R<n> BLOCKED: <question>`) as the LAST line of your reply, alone on its line — after any summary, never inside it. `<n>` is your round number, so the token is unique per round and a wait cannot match the previous round still on screen. The orchestrator's watcher looks for that exact line.

## TAKEOVER (only when another agent stopped mid-phase)
You inherit uncommitted work — KEEP it and build on it:
- Changed files: <git status --porcelain snapshot>
- Last known gates: <counts / failing names>
- Do, in order: <ordered finishing steps>. Do not start over.

## FIX (only for review findings)
Findings: `<data>/runs/<id>/tasks/<T>/review-rN.md` — cite their numbers in the report.
<numbered findings with acceptance per finding; same files/commands/rules as above>
