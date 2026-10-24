# Agent pool, tiers, usage-aware routing

## Pool

| Agent | Role | Notes |
|---|---|---|
| Claude Code (this session) | Orchestrator | Spare it: it is shared with the user's other work |
| Codex | Full builder; cross-family reviewer | First choice for builder lanes |
| OpenCode | Builder; good at takeovers | Model per `opencode models` |
| AGY (Antigravity CLI) | Builder for bulk UI/feature lanes | Gemini models via `agy models`; whether AGY-hosted Claude models bill the Anthropic subscription is unverified — check once |
| Claude Code in a herdr pane | Builder lane in its own worktree (last in builder order) | `claude --model <m> --effort <lvl> --dangerously-skip-permissions` started in the worktree via herdr. Same quota as a subagent, but: watchable/steerable by the user, survives orchestrator compaction, keeps its context for the next fix round, effort via flag. Builds never go to an Agent-tool subagent. |
| Claude subagents (Agent tool) | Short read-only jobs: explore / review / verify / grade | Only when Jev routes them and Claude is above its floor; when Claude is the only option, include "wait for <agent> at <reset>" as a `jev_decide` candidate |
| omp | Lane agent; the default (`lanes.default_agent`) | Model + thinking from Settings (`picks.omp`): `herdr agent start <name> --kind omp --pane <p> -- --model <provider/model> --thinking <level>` (`--thinking` omitted when the model has no efforts). No subscription of its own — see **omp** under the usage ledger. |

Reviewer ≠ builder, preferably a different model family.

## Model menu (snapshot 2026-10-08 — verify ids with each CLI at bootstrap and date them in the project's `project.yaml`)

This is the menu `jev_decide` picks from, not a size → model mapping. Put the "Jev note" into each candidate's description: Jev judges from the description, so a bare id gives it nothing to weigh. Older generations are left out on purpose.

| Agent | Model id | Effort levels | Jev note |
|---|---|---|---|
| Claude | `haiku` (Haiku 5.5) | low / medium / high / max | Cheapest Claude: lookups, digests, checklist grading, 1–2 file mechanical edits. Weak on design/contract/safety reviews. Shared Claude quota. |
| Claude | `sonnet` (Sonnet 5.5) | low / medium / high / max | Balanced: bounded builds, browser QA, standard reviews. Shared Claude quota. |
| Claude | `opus` (Opus 5.5) | low / medium / high / max | Strong reasoning: cross-cutting, data-loss, security. Costly on the shared Claude quota. |
| Claude | `fable` (Fable 5.1) | low / medium / high / max | Most capable Claude, own weekly pool. Final reviews only. |
| Codex | `gpt-6-astra` | low … ultra | OpenAI frontier: hardest builds, cross-family final/security reviews. |
| Codex | `gpt-6.1-sol` | low … ultra | Latest OpenAI workhorse: first-choice builder, cross-family reviewer. |
| Codex | `gpt-6-luna` | low … max | Fast, cheap OpenAI model: mechanical edits, digests. Not for multi-file phases. |
| AGY | `gemini-3.8-flash-{low,medium,high}` | in the id | Latest Gemini Flash: bulk UI/feature lanes; `-high` for standard builds. Own quota. |
| AGY | `gemini-3.1-pro-{low,high}` | in the id | Gemini Pro: complex builds, cross-family reviews (`-high`). |
| AGY | `claude-{opus,sonnet}-5-5-{low,medium,high}` | in the id | AGY-hosted Claude; billing against the Anthropic subscription unverified. |
| AGY | `gpt-oss-120b-medium` | in the id | Open-weight cheap fallback for trivial work. |
| OpenCode | per `opencode models` | model variant | Main model (e.g. GLM-5.x); good at takeovers. |

Sources: the list the agent's configured provider actually serves, not the vendor catalog (lesson 32). `codex debug models` prints the official OpenAI list, which can include ids the provider rejects; the provider's `unsupported model` error prints its supported list — use that, and mark a model unavailable on its first rejection. Also `agy models`, `opencode models <provider>` for each provider the user pays for (e.g. `opencode models zai-coding-plan`; the bare `opencode models` shows only `opencode/*` and `opencode-go/*`), and the session's Claude model list.

**Live catalog:** `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mk-catalog.py routes --json` lists the runnable routes (enabled agents × picked models × efforts) backing the snapshot above; `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mk-catalog.py model --pane <pane>` reads the live model out of a herdr pane footer (`--text` feeds footer text directly). The settings reviewer rows take their options from the catalog's runnable routes (wired in `scripts/mk-config.py`): a saved value the catalog dropped stays in the list flagged `stale`; with the catalog absent the dropdown falls back to the current value alone.

Never put a multi-file phase on a "low" effort model: it looped after context compaction and stalled twice.

## Effort (part of every route — Jev picks it, the user never has to)

A route is **agent + model + effort**, not just a model. Jev picks the effort from the candidates; do not pre-set it from the size class. Static fallback only when Jev is skipped (recipe step 4): trivial → low, standard → medium, complex / data-loss / security → high, final review → max.

How effort is applied per agent:
| Agent | Knob |
|---|---|
| Codex | `codex -c model_reasoning_effort=low\|medium\|high\|xhigh\|max\|ultra` at launch (`gpt-6-luna` stops at `max`; `ultra` adds automatic task delegation) |
| AGY | encoded in the model name (Flash Low / Medium / High, Pro High) — pick the matching model |
| Claude Code in a herdr pane | `claude --effort <level>` at launch |
| omp | `--thinking <level>` at launch; omitted when the picked model's effort column is `-` (no efforts) |
| OpenCode | model variant, if the provider has one; else note "effort n/a" on the board |
| Claude subagents (Agent tool) | no per-call param, but frontmatter `effort:` works (docs: sub-agents → Frontmatter reference; overrides session effort, not `CLAUDE_CODE_EFFORT_LEVEL`). Launch `subagent_type: effort-<low\|medium\|high\|max>` (plugin agents `agents/effort-*.md`, namespaced `matrixkoder:effort-*` when installed as a plugin) with `model` per Jev. Plugin agents (OMC executor etc.) run at session effort. |

Record the route with `mk-state task set <T> agent=<kind> model=<m> effort=<e>` (the board shows `gpt-6.1-sol · high`).

## Usage ledger — `~/.matrixkoder/usage.json` (CodexBar)

One source for every agent: `${CLAUDE_PLUGIN_ROOT}/scripts/mk-usage.py` (`--once` / `--watch` / `--self-test`; the dashboard runs the poll, the pane-text fallback below writes the same ledger) runs `codexbar usage --format json --provider <p>` (providers from `usage.providers`, every `usagePollMin` minutes — a plugin setting, not a yaml key) and normalises into `usage.json`. Read that file; never call codexbar from the loop. Unknown stays `null` — never guess.

Rules:
- **Classify windows by `windowMinutes`** (300 = 5h, 10080 = week), never by primary/secondary position. AGY's primary is a weekly window. Keep every window with its `id`/`title`: AGY has two weekly pools, Claude has a Fable-only week.
- An agent is **available** unless a window that blocks the model in use is at 100%. A window whose `resetsAt` has passed counts as reset (available) — an exhausted quota clears itself.
- **Codex on an API key reports an auth error** → `n/a (API key)`, not numbers; route it by the static order and pane-text detection.
- Provider errors → `status: n/a` with the message.
- Fallback: `lane-health.py --kind <agent kind> --prompt-marker "<dispatch prompt start>"` matches limit text in the pane, but only text since dispatch (not scrollback), needs a quota phrase ("usage limit reached", "hit your limit", "quota exceeded"; never bare `rate limit`/429/"Context limit"), skips code/diff/grep lines, and writes `agents.<kind>.exhausted_until` (+ `exhausted_window` 5h|weekly by reset distance, `exhausted_guess: true` = no reset printed, assumed +5h). An agent is unavailable while `exhausted_until` is in the future; `mk-usage.py` clears it once passed (`docs/state-schema.md`).
- Claude cross-check: compare CodexBar with the session's own measure; flag drift, never overwrite.
- **omp** is `{state: unknown, reason: no subscription; limits belong to the picked model's provider}`: it has no 5h/weekly window of its own, so `mk-usage.py` never polls CodexBar `pi` for it (that payload is an estimated local counter with every window null, not a limit) and no percentage is ever shown. A real omp limit hit comes from pane text (`lane-health.py`, `exhausted_*`) and is the only thing that can make omp unavailable. `available` in `agents.omp` is therefore a CLI/availability fact, not a quota fact. Because omp usually shows `unknown` where one candidate shows numbers, give Jev the reason as evidence, not a score.

## Routing preferences (in the master plan)

Stage 1 writes `## Routing preferences` from the project `project.yaml` defaults; if the user never stated any, propose these and ask once:
- **Claude-sparing:** Claude Code orchestrates only; builders Codex → OpenCode → AGY → Claude.
- **Reserve floors:** Claude ≥40% of 5h and ≥30% weekly left; others may go down to 10% / 15%.
- The orchestrator saves Claude too: the watch/dispatch loop runs in scripts, the main thread can drop to sonnet between verify/merge stages; the reviewer route comes from `routing_preferences.reviewer` (`visual_reviewer` for visual/UI reviews) unless a phase pins its own.
- User statements always override; every quota event during the run appends a dated line under the section.

## Jev recipe (cheap — every call is paid)

1. Plan time: **one** `jev_classify` over all phases, size and risk as separate items (size: the task's own `size`, else its phase's; risk: the phase's riskiest task `risk`, 1-5). Catalog: `trivial`, `standard`, `complex_cross_cutting`, `read_only`, `reversible`, `destructive`, `manual_review`.
2. Per phase: **one** `jev_decide` — the per-phase calls are independent, so issue them in one message as parallel tool calls, never one after another (`jev_classify` above is already one batched call). **Jev picks the route; the orchestrator supplies the menu, never the answer.** Candidates = agent + model + effort routes from the model menu (≤4, e.g. `codex gpt-6.1-sol high`, `agy gemini-3.8-flash-medium`), each described with its Jev note, spanning at least one tier below and one above your own guess and more than one agent where available, and clearly distinct (never 3+ adjacent levels of one family: Jev splits the vote between them) — plus "pause"/"wait for <agent> at <reset>" and Jev's built-in `ask_user` / `investigate`. Evidence = the phase brief verbatim + raw classify result + `mk-config.py context` fields (`agents`, `below_floor`, `policy`, `builder_order`) + `usage.json` snapshot + track record from the board (not your own size verdict). Priorities = routing preferences from `mk-config.py context` (`policy`, `builder_order`; never a size → tier mapping); when two conflict for this phase (e.g. accuracy vs Claude-sparing), state which wins. Requirements (max 3): "keeps Claude above its reserve floor", "agent not inside a reset window", "agent supports the tier".
2a. **Default lane agent:** the default route for lane work is `lanes.default_agent` (`omp`) with its Settings pick `picks.omp`. A user-pinned route or a phase `route` wins over it; when neither is set, put that agent first in the candidate list and still call `jev_decide` — Jev decides size/effort from the candidates, the orchestrator does not decide it. If the default agent is not `available` (`agents.omp.available` false, or inside an `exhausted_until` window), drop it and run the normal Jev flow.
3. Re-decide only on failover or a usage change. Reviewers are assigned by the rule above, not by Jev.
4. `usage.json` empty or n/a for all candidates → skip Jev, use the static Claude-sparing order (`builder_order` from `mk-config.py context`).
5. Re-route after a failed round only after the SKILL.md "diagnose the miss" step: brief/verification gaps are fixed in the brief at the same tier. Low confidence between two adjacent tiers: take the **lower** tier when the job is read-only or scripted (browser QA, grading, exploration) — verification catches misses cheaply; go one tier up only for build work on data-loss/security paths. `escaped` → stronger model on a non-Claude agent first, then Claude, and say so. Never re-phrase and re-call.

## Failover

`QUOTA`, `PROVIDER` (unsupported model, subscription required, service unavailable, auth) or `STALL` (working but no file/commit change for 10 min, or idle without the DONE token): record `git status --porcelain` + `git diff --stat` counts as an event (`mk-state event --task <T> --type note`), `task round <T> --kind takeover --cause "quota|stall|provider"`, write the TAKEOVER section, re-decide with Jev (or static order), dispatch. Skip agents inside a reset window. All agents below their floor → pause the run and ask.

## Review sizing

Not every lane needs a separate human reviewer. Depth follows risk (the phase's riskiest task: `risk` 1-2 light, 3-4 standard, 5 data-loss; old plans: the phase `review` field), and the reviewer is always a different model family than the builder where possible.

| Lane kind | Review depth |
|---|---|
| Docs, typos, 1-line or mechanical changes | **Skip the reviewer.** Real gate output + `jev_review`/`jev_gate` on the real diff (never a placeholder) is enough. |
| Ordinary feature/UI lanes | **Cheap first pass:** `jev_review` on the real diff. Escalate to a reviewer only when it flags something (`review`/`escalate`) or the lane touches a risk path. |
| Shared run state, data-loss, security, guards, auth, shell, YAML/config writers, cross-run or concurrency logic | **Full reviewer.** (Evidence 2026-10-08: two full reviews on `mk-state` found 9 then 6 real bugs that passed every gate.) |

**Reviewer choice** is generic, from the repo's MK config:
- Candidates = agents with `available: true` in the project `project.yaml` `agents:`, excluding the builder's model family, with quota left in `~/.matrixkoder/usage.json`.
- Prefer a non-Claude agent in a herdr pane to spare Claude.
- Use a Claude subagent only when it is the only family left or Jev routes it there.
- Jev still picks agent + model + effort from those candidates.
- A data-loss/security review uses the strongest tier available.
- The settings UI's reviewer rows offer the catalog's runnable routes (see **Live catalog** above).

**Reviewer brief** = the diff + the brief's acceptance criteria + gate output. Never "explore the repo".

## Typical routes (static fallback when Jev is skipped; the "never" rules are `jev_decide` requirements)

Claude tier aliases (`haiku`, `sonnet`, `opus`, `fable`) resolve to the newest model of that tier; record the resolved model on the task. Haiku 5.5 (user, 2026-10-07) is the default for small and easy Claude jobs: include `claude haiku <effort>` as a `jev_decide` candidate for every trivial or read-only job.

| Job | Route |
|---|---|
| Browser QA / Playwright flows with a checklist | sonnet · medium |
| Exploration, doc/API lookups, grading against a checklist, small mechanical edits (1–2 files) | **haiku · low–medium** (Haiku 5.5: cheap, capable — first choice for small read-only jobs) |
| Status sweeps, log/test-output digests, board renders | haiku · low |
| Reviews of designs, contracts or safety code | sonnet · high or a non-Claude pro tier — **never haiku** (lesson 29) |
| Bounded build, clear brief | non-Claude builder · medium |
| Data-loss / security build or review | opus-tier · high |
| Final release review | fable · max |
