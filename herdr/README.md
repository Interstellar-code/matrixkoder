# MatrixKoder herdr plugin

Turns herdr pane events into MatrixKoder lane verdicts. herdr runs
`../scripts/mk-lane-event.py event` (one short process) for every
`pane.agent_status_changed` and `pane.exited` event in every workspace; the
script finds the MatrixKoder lane armed for that pane, applies the same verdict
rules `scripts/lane-watch.py` uses, writes the task status through
`scripts/mk-state.py`, labels the pane in herdr's sidebar and shows a toast.
Full flow, watch-file schema and limits: `../docs/herdr-events.md`.

## Link / unlink

The plugin is this directory inside the MatrixKoder repository (it stays here;
`herdr plugin link` registers the path, it does not copy):

```bash
herdr plugin link /path/to/matrixkoder/herdr     # from the repo: herdr plugin link ./herdr
herdr plugin list
herdr plugin log list --plugin matrixkoder       # event-command logs, for debugging
herdr plugin unlink matrixkoder                  # unregister; files stay in the repo
```

Needs herdr >= 0.9.0, `python3` and `git` on PATH. The script lives at
`../scripts/mk-lane-event.py` relative to this directory — herdr runs event
commands with the plugin directory as cwd, so the relative path resolves both
for a linked checkout and for a `herdr plugin install owner/repo/herdr` clone.

## What runs

- Every pane status change / exit spawns `python3 ../scripts/mk-lane-event.py
  event`. Panes with no armed MatrixKoder lane exit immediately (one JSON line,
  no state touched).
- Lanes are armed per build task with `mk-lane-event.py arm` (see
  `../docs/herdr-events.md`); the orchestrator disarms with `disarm` after
  acting on a verdict.
- No events at all (crashed or hung agent) is silence: run
  `mk-lane-event.py sweep` periodically as the stall backstop.

Builders never link, enable or install this plugin; that is the orchestrator's
or user's call.
