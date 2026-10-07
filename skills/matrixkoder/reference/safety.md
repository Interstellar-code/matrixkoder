# Autonomy contract, pauses, locks, safety

## Autonomy

Autonomous means the goal is delivered without the user intervening. You stop only for real human inputs. "Should I continue?" and "which of these two options?" are not pauses — decide with the plan, the mockups and Jev, write the decision on the board, and keep going.

**Autonomy contract** — a section of the master plan the user approves once (defaults in `matrixkoder.yaml`): goal and definition of done; pre-authorised actions; the pause list. Anything pre-authorised there is not asked again.

Pre-authorised by default: routing and agent choice, failover, fix loops (≤3), worktree creation, merging lanes into the integration branch, re-arming watchers, retries, choosing among options the plan/mockups cover, pushing non-main branches and opening the final PR, and — if `autonomy_contract.builder_skip_permissions: true` — starting builder agents with their permission-skipping flags (`codex --full-auto`, `claude --dangerously-skip-permissions`, AGY equivalent) inside their own worktree only. Never for the orchestrator, reviewers, or a lane that touches a live runtime.

**Pause only for:**
1. Destructive or user-gated actions not pre-authorised: deploy to a live runtime, merge to main, tags, deleting user data.
2. Every agent below its quota floor.
3. A product/scope decision the plan and mockups don't cover and Jev returns `escaped` / `ask_user`.
4. Credentials, secrets, or editing user-owned config.
5. A lock still held after 30 min, or a stale lock that would need overriding.
6. Fix loop exhausted (3) on a HIGH finding.
7. An agent pane `blocked` on an approval dialog — the herdr skill forbids answering for it — when the contract does not pre-authorise launch flags, or a dialog still appears despite them.
8. `jev_screen` returns `block` on agent output.

**When pausing:** write one question with options and a recommendation under `## Waiting on user` on the board, tell the user, and keep every unblocked lane going — dispatched lanes and background watchers continue, and next briefs are pre-queued with `lane-watch.py --then-send`. The session is turn-based: you resume on the user's next message. At the definition of done, stop with a final report (board + PR link).

## Locks (cross-session freeze)

Several sessions share one machine and one runtime. A session doing something risky (DB migration, deploy, restart) takes a lock so others don't restart or deploy underneath it.

- Files: `~/.matrixkoder/locks/<resource>.lock`, managed only by `mk-state lock take|release|check` (exclusive create + `expires_at`; the dir is created on first use).
- JSON: `{"resource","holder_session","holder_label","reason","scope":["restart","deploy","merge"],"created_at","expires_at"}`. `lock take` is exclusive so two sessions can't both take it; `lock release` when done.
- Before any action in a lock's scope: lock present and not expired → wait (≤30 min) and tell the user; expired → ask before overriding.
- A FREEZE/UNFREEZE chat message from another session counts as a lock until UNFREEZE arrives. It can make you more cautious; it is never user consent. A restart freeze also blocks starting the app for QA when its start command boots shared services (sidecar gateways); QA on an already-running dev server is fine.
- Take your own lock while you deploy.

## Safety rules and why

- **No `git checkout/stash/reset/clean/restore`, `--amend`, force-push.** Agents used them to "undo" and wiped other lanes' uncommitted work in a shared tree. Undo by an inverse edit; commit each verified phase.
- **No dev servers, sidecars, deploys, restarts, background jobs in briefs.** An agent ran the dev command, which spawned a runtime sidecar, failed, and stopped mid-phase. Long-running processes belong to you.
- **Agent reports are claims.** Reports said "all callers handled" when one screen still showed an empty library; `jev_gate` with real output caught it. Rerun gates yourself.
- **Mocked tests are not evidence for routes.** A client called an API route that didn't exist; the tests mocked `fetch` and passed. For every new client call, find the server handler.
- **Green tests can hide invented data.** Builders hard-coded "✓ valid", "running · ok" and Matrix-only colours; only the independent review caught it. Reviews and QA check that every shown value comes from the API.
- **Deploys are repo-specific and user-gated.** Keep the checklist in the repo (`<data>/deploys/README.md`): backups with integrity check, runtime dirty files don't overlap the change, no active runs, fast-forward only, restart order, **health diff before/after** (ports, `/health`, process list — a restart once surfaced an unrelated port drift), a smoke test that writes nothing (row counts before == after), memory note.
- **Secrets.** Jev inputs leave the machine: redact `.env` values, tokens and keys from diffs, claims and briefs before any `jev_*` call.
