# Karpathy coding principles

Source: https://www.claudemod.com/mods/karpathy-claude-rules (from forrestchang/andrej-karpathy-skills).
Simplicity First is left out: the `ponytail` plugin already enforces it.

These rules favour caution over speed. For trivial tasks (typo fixes, obvious one-liners) use judgment: not every change needs the full rigor.

## 1. Think before coding

Don't assume. Don't hide confusion. Surface tradeoffs.

- State assumptions explicitly. If uncertain, ask rather than guess.
- If a request has several readings, present them. Don't pick one silently.
- Push back when a simpler approach exists.
- When confused, stop. Name what is unclear and ask.

## 2. Surgical changes

Touch only what you must. Clean up only your own mess.

- Don't "improve" adjacent code, comments or formatting.
- Don't refactor things that aren't broken.
- Match the existing style, even if you would do it differently.
- Unrelated dead code: mention it, don't delete it.
- Remove imports, variables and functions that YOUR change made unused. Leave pre-existing dead code unless asked.

Test: every changed line traces directly to the user's request.

## 3. Goal-driven execution

Define success criteria. Loop until verified.

- "Add validation" → write tests for invalid inputs, then make them pass.
- "Fix the bug" → write a test that reproduces it, then make it pass.
- "Refactor X" → tests pass before and after.

Only for complex code (several files, risky or cross-cutting changes), state a brief plan first. Skip it for simple or routine work:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
```

Weak criteria ("make it work") need constant clarification. Strong criteria let you loop alone.

# CodeGraph

This repo has a `.codegraph/` index. To find or trace code, call `codegraph_explore` before grep or reading files, and name exact symbols or files (e.g. "walkRoot treeRows rootsOf"), not a sentence. Use it for "where is X", "who calls X" and "what breaks if I change X". Don't grep to re-check its results.
