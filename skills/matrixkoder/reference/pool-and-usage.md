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

Reviewer ≠ builder, preferably a different model family.

## Tiers (verify ids with each CLI at bootstrap and date them in `<data>/matrixkoder.yaml`)

| Jev size class | Codex | OpenCode | AGY | Claude |
|---|---|---|---|---|
| trivial / mechanical | default | fast model | gemini flash medium | haiku subagent |
| standard | default | main model (e.g. GLM-5.x) | gemini flash high | sonnet |
| complex / cross-cutting / reviews | high reasoning | — | gemini pro high | opus |
| final review | review pass | — | gemini pro high | fable / opus |

Never put a multi-file phase on a "low" effort model: it looped after context compaction and stalled twice.

## Effort (part of every route — Jev picks it, the user never has to)

A route is **agent + model + effort**, not just a model. Default by Jev size class: trivial → low, standard → medium, complex / data-loss / security → high, final review → max. Jev may move one step either way given evidence (e.g. a lane that already failed a fix round → one step up).

How effort is applied per agent:
| Agent | Knob |
|---|---|
| Codex | `codex -c model_reasoning_effort=low\|medium\|high` at launch |
| AGY | encoded in the model name (Flash Low / Medium / High, Pro High) — pick the matching model |
| Claude Code in a herdr pane | `claude --effort <level>` at launch |
| OpenCode | model variant, if the provider has one; else note "effort n/a" on the board |
| Claude subagents (Agent tool) | no per-call param, but frontmatter `effort:` works (docs: sub-agents → Frontmatter reference; overrides session effort, not `CLAUDE_CODE_EFFORT_LEVEL`). Launch `subagent_type: effort-<low\|medium\|high\|max>` (plugin agents `agents/effort-*.md`, namespaced `matrixkoder:effort-*` when installed as a plugin) with `model` per Jev. Plugin agents (OMC executor etc.) run at session effort. |

Record the route with `mk-state task set <T> agent=<kind> model=<m> effort=<e>` (the board shows `gpt-6.1-sol · high`).

## Usage ledger — `~/.matrixkoder/usage.json` (CodexBar)

One source for every agent: `${CLAUDE_PLUGIN_ROOT}/scripts/mk-usage.py` (being built, P2 — until then only the pane-text fallback below writes the ledger) runs `codexbar usage --format json --provider <p>` (providers from `usage.providers`, every `usage.poll_min` minutes) and normalises into `usage.json`. Read that file; never call codexbar from the loop. Unknown stays `null` — never guess.

Rules:
- **Classify windows by `windowMinutes`** (300 = 5h, 10080 = week), never by primary/secondary position. AGY's primary is a weekly window. Keep every window with its `id`/`title`: AGY has two weekly pools, Claude has a Fable-only week.
- An agent is **available** unless a window that blocks the model in use is at 100%. A window whose `resetsAt` has passed counts as reset (available) — an exhausted quota clears itself.
- **Codex on an API key reports an auth error** → `n/a (API key)`, not numbers; route it by the static order and pane-text detection.
- Provider errors → `status: n/a` with the message.
- Fallback: `lane-health.py --kind <agent kind> --prompt-marker "<dispatch prompt start>"` matches limit text in the pane, but only text since dispatch (not scrollback), needs a quota phrase ("usage limit reached", "hit your limit", "quota exceeded"; never bare `rate limit`/429/"Context limit"), skips code/diff/grep lines, and writes `agents.<kind>.exhausted_until` (+ `exhausted_window` 5h|weekly by reset distance, `exhausted_guess: true` = no reset printed, assumed +5h). An agent is unavailable while `exhausted_until` is in the future; `mk-usage.py` clears it once passed (`docs/state-schema.md`).
- Claude cross-check: compare CodexBar with the session's own measure; flag drift, never overwrite.

## Routing preferences (in the master plan)

Stage 1 writes `## Routing preferences` from `matrixkoder.yaml` defaults; if the user never stated any, propose these and ask once:
- **Claude-sparing:** Claude Code orchestrates only; builders Codex → OpenCode → AGY → Claude.
- **Reserve floors:** Claude ≥40% of 5h and ≥30% weekly left; others may go down to 10% / 15%.
- The orchestrator saves Claude too: the watch/dispatch loop runs in scripts, the main thread can drop to sonnet between verify/merge stages, reviewers default to Codex or AGY pro unless Jev routes the review to opus.
- User statements always override; every quota event during the run appends a dated line under the section.

## Jev recipe (cheap — every call is paid)

1. Plan time: **one** `jev_classify` over all phases, size and risk as separate items. Catalog: `trivial`, `standard`, `complex_cross_cutting`, `read_only`, `reversible`, `destructive`, `manual_review`.
2. Per phase: **one** `jev_decide`. Candidates = agent + model + effort combos (≤4, e.g. `codex gpt-6.1-sol high`, `agy flash-medium`) — the tier and default effort come from step 1 — plus Jev's built-in `ask_user` / `investigate`. Evidence = classify result + `usage.json` snapshot + track record from the board. Priorities = routing preferences. Requirements (max 3): "keeps Claude above its reserve floor", "agent not inside a reset window", "agent supports the tier".
3. Re-decide only on failover or a usage change. Reviewers are assigned by the rule above, not by Jev.
4. `usage.json` empty or n/a for all candidates → skip Jev, use the static Claude-sparing order.
5. Re-route after a failed round only after the SKILL.md "diagnose the miss" step: brief/verification gaps are fixed in the brief at the same tier. Low confidence between two adjacent tiers: take the **lower** tier when the job is read-only or scripted (browser QA, grading, exploration) — verification catches misses cheaply; go one tier up only for build work on data-loss/security paths. `escaped` → stronger model on a non-Claude agent first, then Claude, and say so. Never re-phrase and re-call.

## Failover

`QUOTA`, `PROVIDER` (unsupported model, subscription required, service unavailable, auth) or `STALL` (working but no file/commit change for 10 min, or idle without the DONE token): record `git status --porcelain` + `git diff --stat` counts as an event (`mk-state event --task <T> --type note`), `task round <T> --kind takeover --cause "quota|stall|provider"`, write the TAKEOVER section, re-decide with Jev (or static order), dispatch. Skip agents inside a reset window. All agents below their floor → pause the run and ask.

## Typical routes (starting points, Jev adjusts)
| Job | Route |
|---|---|
| Browser QA / Playwright flows with a checklist | sonnet · medium |
| Grading, exploration, doc lookups | sonnet or haiku · low–medium |
| Bounded build, clear brief | non-Claude builder · medium |
| Data-loss / security build or review | opus-tier · high |
| Final release review | fable · max |
