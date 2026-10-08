# Onboarding wizard (first `/mk` in a repo)

Runs as pane steps in the dashboard; when the pane is not available (not fullscreen, < 144 cols) the same steps run as chat questions. Every step is pre-filled from detection; the user edits only what is wrong. **One confirm at the end** — nothing is written before it. Detection is read-only (run through `ctx_batch_execute`; only counts and names come back).

| # | Step | Detects | Answer lands in |
|---|---|---|---|
| 1 | Install mode | `--plugin-dir` dev folder vs marketplace install (info only) | nowhere |
| 2 | Data location | git root, `.gitignore`; default `<repo>/.matrixkoder/` (adds it to `.gitignore`) or `~/.matrixkoder/projects/<slug>/` | `matrixkoder.yaml` `data_dir`; `~/.matrixkoder/projects.json` |
| 3 | Agents | `herdr` + `HERDR_ENV`; `claude`, `codex`, `opencode`, `agy` installed + logged in; `agy models`, `opencode models` plus `opencode models <provider>` per paid provider (e.g. `zai-coding-plan`); Codex models from the provider's supported list, not `codex debug models`; Codex sandbox mode | `matrixkoder.yaml` `agents` |
| 4 | Usage | `codexbar` on PATH (else show the install command); enabled providers; live probe per agent; z.ai key for the OpenCode plan is entered by the user into CodexBar config, never via the model | `matrixkoder.yaml` `usage`; `~/.matrixkoder/usage.json` (first poll) |
| 5 | Gates | package manager, test / typecheck / lint / format commands, scopable-to-paths; measured baselines; server-starting commands | `matrixkoder.yaml` `gates`, `baselines`, `banned_commands` |
| 6 | Routing | builder order, reserve floors, max lanes, effort by size, reviewers | `matrixkoder.yaml` `routing_preferences` |
| 7 | Autonomy | pre-authorised list, pause list, builder permission-skip flags (lesson 27: the user may need a permission rule or start the pane) | `matrixkoder.yaml` `autonomy_contract` |
| 8 | QA | frontend?, start cmd, dev URL/port, themes, mockups dir, `qa.integration_target` | `matrixkoder.yaml` `qa` |
| 9 | Dashboard prefs | auto-open, status chip, toasts, poll intervals | `matrixkoder.yaml` `dashboard`; `~/.matrixkoder/config.json` |
| 10 | Review + confirm | shows the filled `templates/matrixkoder.yaml` as one diff; single question "Write these?" | writes `<data>/matrixkoder.yaml`, `~/.matrixkoder/config.json`, `~/.matrixkoder/projects.json` |

Steps 3–8 are the checks listed in `loop.md` §Bootstrap detection. Open items go in as proposed defaults marked `# proposed`, never as extra questions. Later runs skip the wizard and only drift-check.
