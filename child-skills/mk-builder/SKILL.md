---
name: mk-builder
description: MatrixKoder lane agents only: start, progress, blocked and done for a mk-builder lane round, and the lane rules that go with them. Lane agents, never the orchestrator.
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/mk-builder.py *)
---

# mk-builder — the lane-round status procedure for MatrixKoder lane agents (builder and reviewer). Not for the orchestrator.

Your runtime has no skill support? Read this file by path and follow it: `.claude/skills/mk-builder/SKILL.md` in your lane, or `child-skills/mk-builder/SKILL.md` in the plugin checkout.

The script, both forms, before any command: under OpenCode it is under this skill's base directory — `scripts/mk-builder.py`, relative to the folder holding this SKILL.md; under Claude Code it is `${CLAUDE_SKILL_DIR}/scripts/mk-builder.py`. The commands below use the Claude Code form; an OpenCode agent substitutes its base-directory path.

Every command runs in the foreground against `<lane>`, the lane worktree root. `<lane>` defaults to the working directory, so pass `--lane <dir>` only when yours is not.

## 1. start

First command of the round:

```
python3 ${CLAUDE_SKILL_DIR}/scripts/mk-builder.py start --run <run> --task <task> --round <n> --role builder --report <report-path>
```

`--role review` is the reviewer spelling of the same start. A second `start` with the same (run, task, round, role) resumes the round; a different identity archives the old status file first.

## 2. progress, per task step

1. You begin a step: `python3 ${CLAUDE_SKILL_DIR}/scripts/mk-builder.py progress <step>`
2. You hand that step to a subagent: `python3 ${CLAUDE_SKILL_DIR}/scripts/mk-builder.py progress <step> --subagent`
3. The subagent returned and you checked its files with `git status`: `python3 ${CLAUDE_SKILL_DIR}/scripts/mk-builder.py progress <step>` again

(`ctx <pct>` exists for an agent that can report a context percentage; no agent is known to, so the field stays null.)

## 3. blocked

`python3 ${CLAUDE_SKILL_DIR}/scripts/mk-builder.py blocked "<question>"` — then stop and wait. The token it prints is the last line of your reply.

## 4. done

`python3 ${CLAUDE_SKILL_DIR}/scripts/mk-builder.py done` — only once the report file exists at the `report` path given to `start` and the phase commit exists. It records HEAD and prints the token.

## Asking the orchestrator

When a real spec ambiguity blocks the task — not a design choice you could make and defend yourself — ask instead of guessing or stalling:

```
python3 ${CLAUDE_SKILL_DIR}/scripts/mk-builder.py ask "<question>"
```

`ask` blocks and returns the answer: it prints the answer text and exits 0, and while it waits the status file says `blocked`, so no STALL fires. `PENDING <ask id>` (exit 3) means the answer has not arrived within the timeout — run `wait <ask id>` to resume the same wait, and keep working only after it returns. `BUS DOWN: use the BLOCKED token` (exit 4) means the bus is unreachable — fall back to `blocked "<question>"` and end your reply with its token.

Run `inbox` at step boundaries, right after each `progress` call: `progress` prints one extra line, `N unread — run inbox`, when messages are waiting; `inbox` prints them as JSON lines (acknowledging each) and prints nothing when there is nothing to read. `ack <msg id>` records a single message as read.

## Token rule

The last stdout line of `done` / `blocked` is the line your reply must end with — alone on its line, after the commit and the report.

## Todo list, first action

First action of the round: write the todo list with your todo tool — one item per plan task, plus one each for the old-code check, the gates, the commit and the report; exactly one item in progress at a time, updated at every step (`reference/lane-rules.md` R9).

## Reference

- `reference/lane-rules.md` — builder and reviewer rules, R1-R29.
- `reference/status-contract.md` — the lane status file this script writes, R1-R42.
- `reference/git-guard.md` — what the lane-local `git` wrapper refuses, G1-G30.
