<!-- Reference for <data>/runs/<id>/board.md as `mk-state render` writes it (scripts/mk-state.py Store.render). Never hand-edit a real board: change state with mk-state, then `render`. This comment line is not in the real output. -->
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
