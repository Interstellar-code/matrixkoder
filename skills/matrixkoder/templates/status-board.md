<!-- Reference for the board as `mk-state render` PRINTS it to stdout from the project DB (scripts/mk-state.py Store.render). The board is not a file: mk-state writes no board.md and no run.json — the project DB (<project>/mk.db) is the one source of truth (C4). Never hand-edit a board: change state with mk-state, then `render` to print the current one. This comment line is not in the real output. -->
# <run id> — <feature>

branch `<branch>` · stage <N> · <active|done|archived> · updated <ISO time>
plan: `<plan path>`

**Next:** <one line from `run set --next`, or —>
**Waiting on you:** <question> — options: <a> | <b> — rec: <recommendation>

## Tasks

| Task | Title | Wave | Status | Deps | Agent | Model · effort | Round | Lane | Last commit | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| P1 | State core | 1 | building | P0 ✓ | codex | gpt-6.1-sol · high | 1 | ../repo-mk-1 · mk/mk-p1/p1 · slot 1 · mk:repo:1 | 9f2c1ab | approve |

The Lane cell is the lane's path plus, when `mk-lane.py acquire` set them, its `branch`, `slot` and
`workspace`; with none of them set it stays the bare path (`—` when there is no lane).

## Recent events (last 15)

- <ISO time> · <task or run> · <type> · <from → to> · <text> (<by>)
