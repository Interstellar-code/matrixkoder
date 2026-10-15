# Live test: the C2 storage layout, end to end

`scripts/mk-e2e-layout.py` builds a throwaway MatrixKoder home, drives a whole MK cycle
through the production tools only, migrates one project, and then audits the folder tree it
produced against the `storage-layout` contracts. The tree is kept on disk so you can open
`/mk` against it and look at it yourself.

Nothing in it touches a real `~/.matrixkoder` or a real `.matrixkoder`: the home is refused
before anything is created unless it sits under the system temp dir (the rule
`scripts/mk-livetest.py` already enforces, reused here by import rather than re-implemented).

## Run it

```bash
# the mutation self-test: fast, no bus, no server, a tiny fixture tree
python3 scripts/mk-e2e-layout.py --self-test

# a real full cycle, then remove the tree again
python3 scripts/mk-e2e-layout.py run

# keep the tree so you can look at it
python3 scripts/mk-e2e-layout.py run --keep
# -> {"ok": true, "root": "/var/folders/.../mk-e2e-layout-XXXX", "home": "...", ...}

python3 scripts/mk-e2e-layout.py show   --home <home>     # the tree + the MK_HOME for /mk
python3 scripts/mk-e2e-layout.py check  --home <home>     # the audit; exit 1 on any violation
python3 scripts/mk-e2e-layout.py teardown --home <home>  # removes only what `run` created
```

`run` prints ONE JSON line: the temp root, the home, both scratch repos, both slugs, the plan
id, the run id and both store paths. Without `--keep` it tears the tree down immediately and
prints the same line.

## What the run actually does

No step is mocked. Every state change goes through a production script:

| step | tool |
|---|---|
| the project's folders and its `matrixkoder.yaml` | `mk-config.py list --json --data` |
| the root row + the project store | `mk-state.register_project`, `mkdb.open_project` (the same pair `mk-livetest.py:125-147` uses) |
| the plan, from `skills/matrixkoder/templates/plan.md` | `mk-plan.py import`, `mk-plan.py approve` |
| the run and its tasks | `mk-state.py run init`, `task set`, `task round --kind fix` |
| the bus | a real `mk-bus.py` server on the temp home; lane writers post the byte-for-byte mk-builder messages (connect / progress / status / commit / report / review) |
| the migration | `mk-migrate.py --dry-run`, then `--switch` |
| the pre-migration project | `mkdb.open_project(home, slug, data_dir=…)` and a root row with `db_path` NULL — never switched |

Two projects, on purpose:

- **project A** is *repo mode* (its data dir is `<repo>/.matrixkoder`). Only a repo-mode
  project has a real legacy store, so only it can prove C5 — that after `--switch` the old
  store is byte-identical and every later write lands in the new one.
- **project B** is *pre-migration*: its store stays at `<data>/mk.db` and its row keeps
  `db_path` NULL. It has its own clean scratch repo, and that repo is what R04 audits.

C5 says no script may delete a migrated project's old folder, so "no `.matrixkoder` in the
repo" can only ever hold for a project that never had one — hence the second repo.

## The audit rules

`check` writes `tree.txt` and `audit.md` into the temp root and prints one line per rule.
Exit 0 means every rule held; exit 1 means at least one did not, and each failure **names the
offending path**.

| rule | contract | what it holds |
|---|---|---|
| R01 | C2 | exactly the root `mk.db`, one `mk.db` per C2 project and one per pre-migration project — no third store anywhere under the temp root |
| R02 | C2 | the plan folder holds `plan.md`, `assets/` and `archive/` |
| R03 | C6 | the closed run has `plans/<plan>/runs/<run>/bus.jsonl`, every line one JSON object |
| R04 | C2 | the clean scratch repo holds no `.matrixkoder` |
| R05 | C4 | no `run.json`, `task.json`, `events.jsonl`, `board.md` or `runs/index.json` anywhere under the temp root |
| R06 | C3 | the root `plan_summary` counts equal the project DB's own counts for the same plan |
| R07 | C6 | the `bus.jsonl` line count equals the messages, acks and bus artifacts the run archived |
| R08 | C6 | every lane token is at `bus/lanes/<sha256(lane)>.json`, mode 0600, with no lane-local token and no pre-L6.1 `bus-lanes/` |
| R09 | C6 | the bus files live in `bus/` and nowhere else |
| R10 | C6 | the watch cursors live at `projects/<slug>/watch-cursors.json`, 0600, and nowhere else |
| R11 | C3, C5 | after the switch every write lands in the NEW store and the legacy store is byte-identical |
| R12 | C3 | the root store carries numbers and linkage only — no `tasks`, no `events`, and the frozen `plan_summary` column list |
| R13 | C2 | no per-plan DB and no bus DB |
| R14 | C3 | the root overview alone carries every plan row the Plan tab needs |

### Proving each rule is load-bearing

`--self-test` builds a clean fixture tree, checks that it passes, then breaks it **one rule at
a time** and requires that `check` fails, that *only* that rule fails, and that the failure
names the mutation. The mutations are: a stray `mk.db`, a plan folder with no `archive/`, a
missing `bus.jsonl`, a `.matrixkoder` in the scratch repo, a stray `run.json`, a wrong
`plan_summary` count, a `bus.jsonl` count that does not match the archive, a lane token at the
old path, a bus file at the pre-L6.1 home-root path, watch cursors in a data dir, a post-switch
write that never reached the new store, a `tasks` table in the root store, a per-plan DB, and a
missing `plan_summary` row.

## How you check it yourself

1. `python3 scripts/mk-e2e-layout.py run --keep` and keep the printed `home`.
2. Open `/mk` with that home (set `MK_HOME` and `MATRIXKODER_HOME` to it) and look at:
   - **Plan** — the mock plan, its phases and its counts;
   - **Settings** — the project settings, read from the C2 project folder;
   - **Overview** — both projects, one migrated and one pre-migration;
   - **Lanes**, **Events** and **k** — the run, its tasks and the open work.
3. `python3 scripts/mk-e2e-layout.py show --home <home>` prints the same tree as text, and
   `tree.txt` / `audit.md` sit in the temp root next to it.
4. `python3 scripts/mk-e2e-layout.py teardown --home <home>` when you are done. It removes
   only what `run` created — a marker file with a matching token proves the root is ours, and
   a path outside it is refused.

## Rules that fail until a phase merges

A rule that fails because the code is not merged yet is **reported, never silenced**. The
orchestrator re-runs this script after the remaining phases land and it must be green then.
On today's main the failures are the ones whose writers or commands have not merged — see the
lane report for the exact list, and `audit.md` for the paths.