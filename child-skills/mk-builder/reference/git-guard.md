# git-guard.md — the lane-local git wrapper contract (`<lane>/.matrixkoder-bin/git`)

Frozen by the MB1 reference round (plan `mk-builder-skill`; siblings: `lane-rules.md` R2 and
`status-contract.md`). The wrapper itself is written in MB2.3 and smoke-tested in MB5; this file
is the contract both build against, and the deterrent boundary documented for MB5's findings. This
folder (`child-skills/`) is not a registered skill. The refused set is exactly the one
`hooks/guards.ts:154-225` (`gitBan`) already encodes Claude-side — the wrapper extends the same
ban to non-Claude lanes (OpenCode, Codex, AGY). Every `file:line` below was re-verified in this
tree at `4df9b9c`. The wrapper is a deterrent, not a sandbox — `hooks/guards.ts:3` says so of the
scanner and the same holds here (§7).

## 1. Placement and dispatch

- **G1.** The wrapper is installed as `<lane>/.matrixkoder-bin/git`, executable, and the lane
  shell's PATH has that directory first (the orchestrator exports it before starting the agent;
  plan decision D4). Everything that resolves `git` through PATH in that shell reaches the
  wrapper.
- **G2.** The wrapper writes nothing inside the repo or worktree — no logs, no state files. Its
  only effects are one refusal line (G22) or the real git's own effects passed through (G23).

## 2. Command grammar (same reading as `gitBan`)

- **G3.** Find the subcommand by skipping leading git options that take a value — `-C`, `-c`,
  `--git-dir`, `--work-tree`, `--namespace`, `--config-env`, `--super-prefix`
  (`hooks/guards.ts:143`, skipped at `:156-161`). So `git -C <path> stash` is refused as
  `git stash`.
- **G4.** Short-flag bundles count: a short option group containing the letter matches
  (`shortHas`, `hooks/guards.ts:148`) — `-f`, `-fd`, `-uf` all force; `-D` and `-d` differ
  (`:200-204`).
- **G5.** For the option-based conditions (G9, G10, G13, G15, G17, G18) read options only up to
  the first `--` (`optsOf`, `hooks/guards.ts:149-152`); the push conditions (G12) scan every word
  after the subcommand (`:190-199`), so a `--force` after `--` still refuses.
- **G6.** Match subcommand names literally, as `gitBan` does (`hooks/guards.ts:162-165`); see the
  alias caveat in §7.

## 3. Refused forms — one rule each, exact conditions

- **G7.** `git checkout`, `git reset`, `git clean` — refused always, any flags, any paths
  (`hooks/guards.ts:142`, `:165`). Why: destructive undo of others' uncommitted work
  (`skills/matrixkoder/reference/safety.md:36`).
- **G8.** `git stash <sub>` — refused unless `<sub>` is exactly `list` or `show`
  (`hooks/guards.ts:166-169`; the read-only allowance is stated at `:141`).
- **G9.** `git restore` — allowed only with `--staged` (or a short `-S`) present AND `--worktree`
  (or `-W`) absent; refused otherwise, including plain `git restore <file>`
  (`hooks/guards.ts:170-176`).
- **G10.** `git switch` — refused only when `--force`, `--discard-changes` or a short `-f` is
  present (`hooks/guards.ts:177-180`).
- **G11.** `git commit --amend` — refused on the `--am(e(nd?)*)?` prefix family (`--am`, `--ame`,
  `--amen`, `--amend`, `hooks/guards.ts:185`). Valued message options (`-m`, `-F`, `-C`, `-c`,
  `--message`, `--file`, `--author`, `--date`, `--template`, `--reuse-message`,
  `--reedit-message`, `--fixup`, `--squash`, `--trailer`, `--cleanup`,
  `hooks/guards.ts:144-147`) are skipped with their values, so `-m '--amend'` (message text) does
  not refuse; the scan stops at the first `--` (`:182-187`).
- **G12.** `git push` — refused on any of: an option starting `--force` (includes
  `--force-with-lease`); a short option containing `f`; any word starting `+` (the `+ref:ref`
  refspec form); `--delete`; a short option containing `d`; `--mirror`; `--prune`; a word matching
  `^:.` (empty-source refspec) (`hooks/guards.ts:190-199`).
- **G13.** `git branch` — refused on a short option group containing uppercase `D`, or on
  `--delete`/`-d` combined with `--force`/`-f` (`hooks/guards.ts:200-206`). A lowercase
  `git branch -d <merged>` passes.
- **G14.** `git rebase` — refused unless a following word is exactly `--abort`, `--continue` or
  `--skip`: ending a rebase already in progress is allowed because blocking it would strand the
  repo (`hooks/guards.ts:207-211`).
- **G15.** `git pull` — refused on `--rebase`, on `--rebase=<v>` whose value is neither `false`
  nor `no`, or a short option containing `r` (`hooks/guards.ts:212-215`).
- **G16.** `git reflog expire` and `git reflog delete` — refused; other `reflog` subcommands pass
  (`hooks/guards.ts:216-217`).
- **G17.** `git update-ref` with a short option containing `d` — refused
  (`hooks/guards.ts:218-219`).
- **G18.** `git gc` with `--prune` or `--prune=<any>` (includes `--prune=now`) — refused; plain
  `git gc` passes (`hooks/guards.ts:220-223`).

## 4. Allowed forms — pass through to the real git

- **G19.** The look-alikes the ban deliberately does not catch:
  - plain `git switch <branch>` with no force flag (`hooks/guards.ts:177-180`),
  - `git commit` without the amend family (`:181-189`),
  - `git push` with none of G12's triggers (`:190-199`),
  - `git branch -d <merged>` (`:200-206`),
  - `git stash list` / `git stash show` (`:166-169`),
  - `git restore --staged` (or `-S`) (`:170-176`),
  - `git rebase --abort|--continue|--skip` (`:207-211`).
  **Why:** the lane's own workflow needs them — `git apply` / `git apply -R` (the fail-on-old-code
  proof, `lane-rules.md` R12, `skills/matrixkoder/templates/brief.md:13`) is not a listed
  subcommand at all, and an unbanned refusal would break legitimate work (`hooks/guards.ts:224`
  returns "allow" for every unlisted subcommand).
- **G20.** Everyday read/record commands pass: `status`, `log`, `diff`, `add`, `commit`,
  `worktree`, `rev-parse`, `show`, `branch` (without G13's triggers), `fetch`, `merge`, `tag`,
  `pull` (without G15's triggers), `gc` (without G18's trigger) — everything `gitBan` does not
  refuse (`hooks/guards.ts:224`).

## 5. Finding the real git

- **G21.** On every pass-through, locate the real git by removing the wrapper's own directory from
  `PATH` and taking the first `git` found on what remains (a PATH walk, or `command -v git` after
  the drop). Never hardcode a git path.
  **Why:** this machine has two gits — `/usr/bin/git` and `/opt/homebrew/bin/git`
  (`which -a git`, research `[local]`,
  `/Users/rohits/Development/matrixkoder/.matrixkoder/plans/mk-builder-skill-research.md:39`) —
  so a fixed path can pin the wrong install and goes stale at the next Homebrew update; the
  drop-own-dir rule also makes the wrapper unreachable by itself (infinite exec loop otherwise).
  The scanner's location-independent analogue is `base()` stripping any path prefix
  (`hooks/guards.ts:10`).

## 6. Refusal and pass-through behaviour

- **G22.** A refusal prints one line naming the refused rule and why — `mk git-guard: refused
  "git stash" (destructive git undo — lane-rules.md R2, git-guard.md G8)` — exits 126, and changes
  nothing in the repo: parse and decide before exec, and never create, write or delete files
  first.
- **G23.** An allowed command `exec`s the real git with the argv untouched — the wrapper adds no
  flags, rewrites nothing, and passes stdout, stderr and the exit code through unchanged.
- **G24.** The wrapper never prompts, never opens an editor, and never blocks on stdin: it only
  refuses or forwards (a lane agent cannot answer a prompt).

## 7. Known bypasses — why this is a deterrent, not a sandbox

- **G25.** An absolute path never consults PATH: `/usr/bin/git stash` or
  `/opt/homebrew/bin/git stash` reach the real git directly. Same for any program, script,
  library or IDE that calls git by path rather than by name.
- **G26.** Git aliases hide the subcommand: `git -c alias.<n>=<cmd> …` or a `git config alias.<n>`
  makes the wrapper see `<n>`, which matches nothing in §3; the Claude-side scanner cannot see it
  either, since it skips `-c <value>` as a valued option before the subcommand
  (`hooks/guards.ts:143`, `:156-161`).
- **G27.** PATH re-injection: a shell snapshot or rc file that re-prepends a git directory ahead
  of `<lane>/.matrixkoder-bin` makes the wrapper inert (Claude Code sources a shell snapshot;
  OpenCode's `shell.env` plugin hook is the lever that would fix it; research A5, `[inferred]`
  without the hook). The export order set at dispatch (G1) is the one defence.
- **G28.** Consequences, so later phases and users measure them: Claude lanes keep full coverage
  — `hooks/guards.ts` scans every Bash tool call, strips wrapper commands (`env`, `sudo`, `nice`,
  `time`, `xargs`, `command`, … `hooks/guards.ts:92-123`, `:99`) and unwraps `$( )`/backticks/
  `bash -c`/`eval` to depth 3 (`:8`, `:29-89`), and matches any path form (`:10`) — so the wrapper
  is the only coverage for OpenCode, Codex and AGY lanes. MB5's smoke test asks each agent kind to
  run `git stash` and records the result; a shell snapshot that re-prepends a git dir counts as a
  finding, not a pass. If the smoke test or the measurement shows a gap, the documented next step
  (plan decision D4) is an OpenCode plugin hook (`tool.execute.before`), not a stronger wrapper.
