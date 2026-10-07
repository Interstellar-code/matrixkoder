# MatrixKoder Sidebar Dashboard — Design Specification

> **Component**: Claude Code Mod `Pane` (Fullscreen right-docked dashboard)  
> **Target Surface**: Terminal (`Elements['terminal']` in Claude Code `ui.render` hook)  
> **Brand**: MatrixKoder (`/mk`)  
> **Contract Ref**: `docs/plan.md` §6, `docs/state-schema.md` v1  
> **Types Declaration**: `plugin-authoring/types/claude-code.d.ts`  

---

## 1. Colour Roles & Theme Adaptability

### 1.1 Palette Philosophy: Named ANSI & ThemeKey Over Raw Hex

Terminal color rendering depends directly on the user's terminal emulator and host color profile (e.g., Solarized Dark vs. Solarized Light, macOS Terminal default white, iTerm2 Dracula, Alacritty Light).

* **The Problem with Raw Hex (`#00ff41`, `#111111`)**:
  Hardcoded hex values bypass the terminal's palette table. An RGB green like `#00ff41` designed on an OLED dark screen washes out completely or becomes blindingly unreadable on light terminal backgrounds. Conversely, dark greys or dark borders become invisible on dark themes and stark on light ones.
* **The Solution: Named ANSI Colors & Theme Keys**:
  Claude Code's `Color` type (`ThemeKey | (string & {})`, line 1673 in `claude-code.d.ts`) supports both semantic `ThemeKey` tokens (line 12510) and standard ANSI color names (`green`, `yellow`, `red`, `blue`, `magenta`, `cyan`, `gray`, `white`, `black`).
  * Terminal emulators map ANSI names to their user-calibrated palettes, guaranteeing high contrast in both dark and light modes.
  * Claude Code's `ThemeKey` tokens (`text`, `subtle`, `inactive`, `success`, `warning`, `error`, `claude`, `suggestion`, `promptBorder`) dynamically follow the user's active Claude theme.
  * `dimColor={true}` uses terminal SGR 2 (faint/dim), scaling luminance cleanly on any background.

### 1.2 Colour Role Matrix

Every styling choice in the dashboard maps directly to an allowed prop in `BoxProps` (lines 901–990) or `TextProps` (lines 12483–12501):

| Role | Meaning / Use Case | `TextProps` | `BoxProps` | Dark Theme Appearance | Light Theme Appearance |
|---|---|---|---|---|---|
| **`accent`** | Brand title, active focus, key headings | `color="cyan"` or `color="claude"` + `bold={true}` | `borderColor="cyan"` | Neon Matrix cyan / Anthropic coral | High-contrast saturated cyan / coral |
| **`ok`** | Completed tasks, merged PRs, clean gates, healthy quota (<60%) | `color="green"` (`success`) | `borderColor="green"` | Bright green (`#22c55e` equivalent) | Dark terminal green (`#15803d` equivalent) |
| **`warn`** | Stalled tasks, high quota (60–85%), non-blocking review | `color="yellow"` (`warning`) + `bold={true}` | `borderColor="yellow"` | Amber / bright yellow | Warm dark amber / brown |
| **`danger`** | Blocked tasks, exhausted quota (>85%), test failure, destructive alert | `color="red"` (`error`) + `bold={true}` | `borderColor="red"` | Vivid red (`#ef4444` equivalent) | Deep crimson red (`#b91c1c` equivalent) |
| **`info`** | Active agent tags, dispatch notices, model identifiers | `color="blue"` or `color="cyan"` (`suggestion`) | `borderColor="blue"` | Crisp sky blue / cyan | Slate / royal blue |
| **`muted`** | Table headers, commit SHAs, elapsed times, inactive waves | `dimColor={true}` + `color="text"` (`subtle`) | `borderDimColor={true}` | 50% opacity neutral grey | 55% opacity dark slate |
| **`building`** | In-flight execution, active worker lane | `color="cyan"` + `bold={true}` | `borderColor="cyan"` | High-visibility active pulse cyan | Deep teal active pulse |
| **`frame`** | Outer pane border, section dividers | N/A | `borderStyle="round"` + `borderColor="text"` + `borderDimColor={true}` | Faint rounded border | Faint rounded border |

---

## 2. Icon & Glyph System

### 2.1 Single-Width Monospace Rule

Standard emoji (e.g. `⛔`, `❌`, `🟢`, `⏳`, `🔧`) are treated by terminal emulators and POSIX `wcwidth()` as **2 character columns wide**. In docked terminal panes with fixed column budgets (44–48 columns), 2-column emoji break monospace tabular vertical alignment, causing jagged columns and line wraps.

**Rule**: All dashboard icons MUST be **single-width Unicode glyphs** (1 character cell wide). An accompanying **Plain-ASCII fallback set** is provided for raw or stripped terminals.

### 2.2 Task Status Glyphs (All 14 Enums)

From `docs/state-schema.md` §Task object: `planned → routed → dispatched → building → verifying → review → fix → merged → qa → done`, plus side states `blocked | quota | stalled | provider`.

| Status Enum | Meaning | Unicode Glyph | Codepoint | Plain ASCII | Colour Role | `TextProps` Styling |
|---|---|---|---|---|---|---|
| `planned` | In queue, awaiting wave or dependencies | `○` | `U+25CB` | `.` | `muted` | `dimColor={true}` |
| `routed` | Lane & model assigned by planner/Jev | `◇` | `U+25C7` | `?` | `muted` | `dimColor={true}` |
| `dispatched`| Brief written, worker starting up | `▷` | `U+25B7` | `>` | `info` | `color="blue"` |
| `building` | Worker active in lane/worktree | `●` | `U+25CF` | `*` | `building`| `color="cyan" bold={true}` |
| `verifying` | Running gates (tsc, tests, lint) | `⧖` | `U+29D6` | `%` | `info` | `color="cyan"` |
| `review` | Builder done; awaiting orchestrator review | `◐` | `U+25D0` | `o` | `warn` | `color="yellow"` |
| `fix` | Review verdict `fix`; round incremented | `▲` | `U+25B2` | `^` | `warn` | `color="yellow" bold={true}` |
| `merged` | Worktree branch merged to run branch | `◈` | `U+25C8` | `v` | `ok` | `color="green"` |
| `qa` | Post-merge verification / dev-server QA | `◆` | `U+25C6` | `+` | `info` | `color="blue"` |
| `done` | Task accepted, gates green, settled | `✓` | `U+2713` | `x` | `ok` | `color="green" bold={true}` |
| `blocked` | Waiting on dependency or lock conflict | `⊘` | `U+2298` | `!` | `danger`| `color="red" bold={true}` |
| `quota` | Agent rate limit or window reached | `⧗` | `U+29D7` | `$` | `danger`| `color="red"` |
| `stalled` | Worker idle with no commits/output >10m | `⚠` | `U+26A0`* | `!` | `warn` | `color="yellow" bold={true}` |
| `provider`| Upstream API 5xx, outage, auth error | `⊗` | `U+2297` | `@` | `danger`| `color="red"` |

*\*Note on `⚠` (U+26A0)*: If the host terminal renders `⚠` as 2 columns, MatrixKoder automatically falls back to single-width `▲` (`U+25B2`) or ASCII `!` to maintain exact columnar grid alignment.

### 2.3 Agent Kind Icons

| Agent Kind | Unicode Glyph | Codepoint | Plain ASCII | Brand Colour | `TextProps` |
|---|---|---|---|---|---|
| `claude` | `✦` | `U+2726` | `*` | `claude` / `magenta` | `color="magenta" bold={true}` |
| `agy` (Antigravity)| `▲` | `U+25B2` | `^` | `blue` | `color="blue" bold={true}` |
| `codex` | `◈` | `U+25C8` | `o` | `green` | `color="green" bold={true}` |
| `opencode` | `◇` | `U+25C7` | `#` | `cyan` | `color="cyan" bold={true}` |

### 2.4 Section Header & Control Glyphs

| Element | Glyph | Codepoint | Plain ASCII | Role |
|---|---|---|---|---|
| **MatrixKoder Brand** | `✦` | `U+2726` | `*` | `accent` (`color="cyan"`) |
| **Next Action / Prompt** | `▶` | `U+25B6` | `>` | `accent` (`color="cyan"`) |
| **Waiting on You Alert**| `⚠` / `!` | `U+26A0` / `U+0021` | `!` | `danger` (`color="red" bold={true}`) |
| **Wave Settled** | `✓` | `U+2713` | `v` | `ok` (`color="green"`) |
| **Wave In-Progress** | `◐` | `U+25D0` | `o` | `warn` (`color="yellow"`) |
| **Wave Pending** | `○` | `U+25CB` | `.` | `muted` (`dimColor={true}`) |
| **Usage Meter Full** | `█` | `U+2588` | `#` | `ok` / `warn` / `danger` |
| **Usage Meter Empty** | `░` | `U+2591` | `-` | `muted` (`dimColor={true}`) |
| **Exhausted / Blocked** | `⊘` | `U+2298` | `X` | `danger` (`color="red"`) |
| **Recent Event Bullet** | `·` | `U+00B7` | `-` | `muted` (`dimColor={true}`) |

---

## 3. Visual Mockups & State Variations

### 3.1 Standard Active Run (Base: 46 Columns)

The standard Pane is docked right in fullscreen layout (`placement: "dock"`, `bodyColumns ≈ 44–48`). Outer frame uses `borderStyle="round"`. Inside width is exactly 44 columns.

```
╭─ ✦ MatrixKoder · wfv2-f678 ──────── 18:42 ─╮
│ workflows-v2 · main@82252eb · stage 7/12   │
│ ▶ Next: verify F6 gates in worktree-b      │
├─ Plan ─ 3/6 done ── W0 ✓  W1 ◐◐●  W2 ○ ────┤
│ ✓ W0   wizard split       merged  62811f9  │
│ ◐ F6   design+configure   review r2        │
│ ● F8   bulk delete        building 12m     │
│ ▲ FIX6 QA1 fixes          stalled 11m      │
│ ○ X1   design pass        waits F6, F8     │
├─ Lanes ────────────────────────────────────┤
│ a  AGY     flash-high   idle      54fc3b5  │
├─ Quota ──────────────── 5h   week   reset ─┤
│ claude    █░░░░░░░       7%   36%   4h03m  │
│ opencode  ████████     100%    —    18:54⊘ │
├─ ⚠ Waiting on you ─────────────────────────┤
│ F8 deletes rows — approve destructive step?│
├─ Recent ───────────────────────────────────┤
│ 18:40 F6 r2 gates 6196 ✓ · tsc 121 = base  │
╰────────────────────────────────────────────╯
```

#### Color & Style Annotations for Base Mockup
* **Title Bar**:
  * `╭─ `: `borderDimColor={true}`
  * `✦ MatrixKoder`: `color="cyan" bold={true}`
  * ` · `: `dimColor={true}`
  * `wfv2-f678`: `color="text"`
  * ` ──────── `: `borderDimColor={true}`
  * `18:42`: `dimColor={true}`
  * ` ─╮`: `borderDimColor={true}`
* **Header Body**:
  * Line 1: `workflows-v2` (`bold={true}`), ` · ` (`dimColor`), `main@82252eb` (`color="text"`), ` · ` (`dimColor`), `stage 7/12` (`color="cyan" bold={true}`)
  * Line 2: `▶ Next: ` (`color="cyan" bold={true}`), `verify F6 gates in worktree-b` (`color="text"`)
* **Plan Header**:
  * `Plan`: `bold={true}`
  * `3/6 done`: `color="green"`
  * `W0 ✓`: `W0` (`dimColor`), `✓` (`color="green" bold={true}`)
  * `W1 ◐◐●`: `W1` (`dimColor`), `◐` (`color="yellow"`), `◐` (`color="yellow"`), `●` (`color="cyan" bold={true}`)
  * `W2 ○`: `W2` (`dimColor`), `○` (`dimColor`)
* **Task Rows**:
  * `✓ W0`: `✓` (`color="green" bold={true}`), `W0` (`bold={true}`), `merged` (`color="green"`), `62811f9` (`dimColor`)
  * `◐ F6`: `◐` (`color="yellow"`), `F6` (`bold={true}`), `review r2` (`color="yellow"`)
  * `● F8`: `●` (`color="cyan" bold={true}`), `F8` (`bold={true}`), `building` (`color="cyan"`), `12m` (`dimColor`)
  * `▲ FIX6`: `▲` (`color="yellow" bold={true}`), `FIX6` (`bold={true}`), `stalled` (`color="yellow" bold={true}`), `11m` (`dimColor`)
  * `○ X1`: `○` (`dimColor`), `X1` (`dimColor`), `waits F6, F8` (`dimColor`)
* **Lanes Section**:
  * `a  AGY`: `a` (`dimColor`), `AGY` (`color="blue" bold={true}`), `flash-high` (`dimColor`), `idle` (`dimColor`), `54fc3b5` (`dimColor`)
* **Quota Section**:
  * Header: `Quota` (`bold={true}`), `5h   week   reset` (`dimColor`)
  * Row 1: `claude` (`color="magenta"`), `█░░░░░░░` (bar: `█` `color="green"`, `░` `dimColor`), `7%` (`color="green"`), `36%` (`color="green"`), `4h03m` (`dimColor`)
  * Row 2: `opencode` (`color="cyan"`), `████████` (bar: `color="red" bold={true}`), `100%` (`color="red" bold={true}`), `—` (`dimColor`), `18:54` (`dimColor`), `⊘` (`color="red" bold={true}`)
* **Waiting on You**:
  * Header: `⚠ Waiting on you`: `color="red" bold={true}`
  * Body: `F8 deletes rows — approve destructive step?`: `color="text" bold={true}`
* **Recent**:
  * `18:40`: `dimColor`
  * `F6 r2 gates 6196 ✓`: `✓` (`color="green"`)
  * `tsc 121 = base`: `dimColor`

---

### 3.2 State Variation: No Active Run (Empty State)

Shown when no run is active in `<data>/runs/` or after a run has been archived. Provides immediate actionable guidance.

```
╭─ ✦ MatrixKoder ──────────────────── 18:42 ─╮
│                                            │
│   No active orchestration run.             │
│                                            │
│   To start a new multi-lane feature run:   │
│   ▶ Run  /mk init <feature-name>           │
│   ▶ Or   /mk resume                        │
│                                            │
├─ Recent Runs ──────────────────────────────┤
│ ✓ wfv2-f678  workflows-v2      archived    │
│ ✓ mk-p0      mod spikes        done        │
├─ Quota Status ─────────────── 5h    week ──┤
│ claude      █░░░░░░░          7%     36%   │
│ agy         ████░░░░         48%     52%   │
│ codex       ░░░░░░░░         n/a     n/a   │
╰────────────────────────────────────────────╯
```

---

### 3.3 State Variation: First-Run (Wizard Incomplete)

Shown when the repository has not yet been onboarded with `matrixkoder.yaml` or global `~/.matrixkoder/config.json` is missing.

```
╭─ ✦ MatrixKoder · Setup ──────────── 18:42 ─╮
│                                            │
│   ✦ Welcome to MatrixKoder!                │
│                                            │
│   Setup required for this project:         │
│   [!] Configuration file missing           │
│       (.matrixkoder/matrixkoder.yaml)      │
│                                            │
│   ▶ Type /mk setup to launch the wizard    │
│                                            │
├─ System Detection ─────────────────────────┤
│ ✓ Git Repository     main worktree         │
│ ✓ CodexBar CLI       v0.73.0 active        │
│ ⚠ Agents Configured  0 / 4 enabled         │
│ ⚠ Gate Baselines     not measured          │
╰────────────────────────────────────────────╯
```

---

### 3.4 State Variation: All Agents Exhausted

Critical warning state when all configured builder agents are locked by rate limits or weekly quotas.

```
╭─ ✦ MatrixKoder · wfv2-f678 ──────── 18:42 ─╮
│ ⊘ ALL BUILDER AGENTS EXHAUSTED             │
│ Next available agent reset in: 1h 12m      │
├─ Quota Status ─────────── 5h    week   rst ┤
│ claude    ████████      100%    100% 19:54⊘│
│ agy       ████████      100%    100% 10-13⊘│
│ opencode  ████████      100%     —   20:15⊘│
│ codex     ░░░░░░░░       n/a     n/a (key) │
├─ ⊘ Orchestrator Paused ────────────────────┤
│ Automated turns are suspended.             │
│ Run will resume automatically when quota   │
│ resets, or switch provider with /mk agent. │
╰────────────────────────────────────────────╯
```

---

### 3.5 State Variation: Waiting-On-User Present (Modal Action Card)

When an agent needs human permission or clarification (e.g. destructive database migration or design ambiguity), the `Waiting on you` section expands into a prominent card with options and recommendation.

```
╭─ ✦ MatrixKoder · wfv2-f678 ──────── 18:42 ─╮
│ workflows-v2 · main@82252eb · stage 7/12   │
│ ⚠ PAUSED: Awaiting human confirmation      │
├─ ⚠ Waiting on you ─────────────────────────┤
│                                            │
│   Task F8 requests confirmation:           │
│   "F8 deletes rows — approve destructive   │
│    database migration step?"               │
│                                            │
│   Options:                                 │
│   [1] Approve & run migration (Rec)        │
│   [2] Abort migration & keep backup        │
│   [3] Inspect diff in worktree             │
│                                            │
│   ▶ Reply 1, 2, or 3 in the prompt         │
│                                            │
├─ Plan ─ 3/6 done ── W0 ✓  W1 ◐◐●  W2 ○ ────┤
│ ● F8   bulk delete        waiting approval │
╰────────────────────────────────────────────╯
```

---

### 3.6 Compact 36-Column Degradation Variant (`bodyColumns = 36`)

When the user splits their terminal or reduces window width, `e.props.bodyColumns` drops to 36 columns.
Each section gracefully degrades without line breaks or clipping:

```
╭─ ✦ MK · wfv2-f678 ────── 18:42 ─╮
│ wfv2 · main@82252eb · stg 7/12 │
│ ▶ Next: verify F6 gates in wt-b│
├─ Plan · 3/6 ── W0:✓ W1:3 W2:○ ─┤
│ ✓ W0  wizard split   merged    │
│ ◐ F6  design+config  review r2 │
│ ● F8  bulk delete    build 12m │
│ ▲ FX6 QA1 fixes      stall 11m │
│ ○ X1  design pass    waits F6  │
├─ Lanes ────────────────────────┤
│ a AGY   flash-high  idle       │
├─ Quota ────────── 5h     reset ┤
│ claude   █░░░░░   7%     4h03m │
│ opencode ██████ 100%     18:54⊘│
├─ ⚠ Waiting on you ─────────────┤
│ Approve destructive step in F8?│
├─ Recent ───────────────────────┤
│ 18:40 F6 r2 gates 6196 ✓       │
╰────────────────────────────────╯
```

#### Detailed 36-Column Degradation Strategy
1. **Title Bar**: Title shortens to `✦ MK`, dash rule reduces from 8 to 6 dashes.
2. **Header**: Feature name and stage string condensed (`stg 7/12`). Next action path shortened (`wt-b` instead of `worktree-b`).
3. **Plan Wave Pills**: Expansive glyph list (`W1 ◐◐●`) collapses to compact wave count (`W1:3` or `W1:2/3`).
4. **Task Rows**: Task title width constrained to 14 characters using `wrap="truncate-end"`. Commit SHAs (`62811f9`) hidden; status column stays visible.
5. **Lanes**: Commit SHA hidden; lane name, agent, and model preserved.
6. **Quota Section**: The `week` column is dropped entirely; only the critical short-cycle `5h` bar and `reset` time are retained. Bar width drops from 7 cells to 5 cells.
7. **Waiting on you**: Multi-line questions are summarized or wrapped cleanly with `wrap="wrap"`.
8. **Recent**: Secondary clauses (e.g. ` · tsc 121 = base`) dropped, keeping primary event and status.

---

## 4. Quota & Usage Meter Specification

### 4.1 Data Pipeline: CodexBar Normalization

The dashboard reads `~/.matrixkoder/usage.json` populated by `mk-usage.py` (via CodexBar CLI v0.73.0) and fallback lane-health QUOTA markers.

Ledger schema mapping:
* Agent keys: `claude`, `agy`, `opencode`, `codex`.
* Windows: `5h` (primary short rate limit, ~300 min) and `week` (rolling 7-day quota, ~10080 min).
* Reset timestamp: `resets_at` (ISO UTC string).
* Exhaustion marker: `exhausted_until` (future timestamp) + `exhausted_window`.

### 4.2 Bar Glyphs & Threshold Rules

Each usage meter is composed of single-width block characters:
* Full / Used block: `█` (`U+2588`, Full Block)
* Empty / Remaining block: `░` (`U+2591`, Light Shade)
* Standard bar width: **7 characters** (5 characters in 36-col mode).

#### Threshold Color Rules
Usage percentages are colored dynamically:
* **`< 60%` used**: `color="green"` (`ok`) — Healthy headroom.
* **`60% – 85%` used**: `color="yellow"` (`warn`) — Approaching limit; orchestrator paces dispatches.
* **`> 85%` used**: `color="red" bold={true}` (`danger`) — Critical quota; danger of stall.
* **`100%` or `exhausted_until > now`**: `color="red" bold={true}` + single-width `⊘` badge.

### 4.3 Meter Row Formats

#### 1. Normal Healthy Agent
```
claude    █░░░░░░░       7%   36%   4h03m
```
* Agent label: 8 cols (`claude  `)
* Bar: 7 cols (`█░░░░░░░` -> 1 green `█`, 6 dim `░`)
* 5h pct: 6 cols right-aligned (`   7%`, `color="green"`)
* Week pct: 6 cols right-aligned (`  36%`, `color="green"`)
* Reset countdown: 7 cols (` 4h03m `, `dimColor={true}`)

#### 2. Approaching Quota Warning
```
agy       █████░░       72%   54%   1h15m
```
* 5h pct: `72%` (`color="yellow"`)
* Bar: 5 yellow `█`, 2 dim `░`

#### 3. Quota Exhausted Agent
```
opencode  ████████     100%    —    18:54 ⊘
```
* Bar: 7 solid red `█` (`color="red" bold={true}`)
* 5h pct: `100%` (`color="red" bold={true}`)
* Reset time: exact local time (`18:54`)
* Badge: `⊘` (`color="red" bold={true}`)

#### 4. Not Applicable (API-Key Provider)
```
codex     ░░░░░░░░      n/a   n/a   (API key)
```
* Bar: 7 dim `░` (`dimColor={true}`)
* Percentages: `n/a` (`dimColor={true}`)
* Note: `(API key)` (`dimColor={true}`)

#### 5. Offline / Probe Error
```
custom    ░░░░░░░░      err   err   (offline)
```
* Percentages: `err` (`color="red"`)
* Note: `(offline)` (`dimColor={true}`)

---

## 5. Status-Line Chip & Notification Toasts

### 5.1 Status-Line Chip (`$.ui.status`)

Pinned under the prompt via `$.ui.status(text)`. Pinned chips must be concise (< 80 chars) and immediately communicate run health and blockers.

#### Copy Templates & State Mapping
1. **Normal Active Progress**:
   `✦ mk · 1 building · 1 review · 3/6 done`
2. **Awaiting User Action (Highest Priority)**:
   `✦ mk · ⚠ 1 waiting on you · 1 building`
3. **Worker Stalled**:
   `✦ mk · ⚠ FIX6 stalled (11m) · 1 building`
4. **Agent Quota Exhausted**:
   `✦ mk · agy ⊘ wk resets 10-13 · 1 building`
5. **Dependency Blocked**:
   `✦ mk · ⊘ X1 blocked by F6, F8 · 2 building`
6. **All Done**:
   `✦ mk · ✓ all 6 tasks merged · stage 12/12`
7. **Idle / Waiting Next**:
   `✦ mk · stage 7/12 · idle · next: verify F6`

### 5.2 Notification Toast Copy (`$.ui.toast`)

Triggered on asynchronous state transitions via `$.ui.toast(text, { timeoutMs: 4000 })`.

| Transition Event | Toast Text Template | Example Rendered Toast |
|---|---|---|
| **`DONE`** (Task Merged) | `✦ [MatrixKoder] Task <id> merged & passed gates (<sha>)` | `✦ [MatrixKoder] Task W0 merged & passed gates (62811f9)` |
| **`QUOTA`** (Rate Limit Hit)| `✦ [MatrixKoder] Quota reached for <agent> (<window>). Resets <time>.` | `✦ [MatrixKoder] Quota reached for opencode (5h). Resets in 18m.` |
| **`STALL`** (Worker Idle) | `✦ [MatrixKoder] Task <id> stalled: no activity in lane <lane> (<min>m)` | `✦ [MatrixKoder] Task FIX6 stalled: no activity in wt-fix6 (11m)` |
| **`BLOCKED`** (Dependency) | `✦ [MatrixKoder] Task <id> blocked waiting on <deps>` | `✦ [MatrixKoder] Task X1 blocked waiting on F6, F8` |
| **`PROVIDER`** (API Error) | `✦ [MatrixKoder] Provider error on <agent>: <summary>` | `✦ [MatrixKoder] Provider error on codex: 503 Service Unavailable` |
| **`WAITING`** (Human Prompt)| `✦ [MatrixKoder] Action required: <question>` | `✦ [MatrixKoder] Action required: Approve destructive step in F8?` |

---

## 6. Component Tree Architecture & Implementation Spec

All components are rendered in TypeScript/TSX for `ui.render` when `e.component === 'Pane'`.
Only props verified in `claude-code.d.ts` are used.

### 6.1 Root Dashboard Frame (`PaneRoot`)

```tsx
<Box
  flexDirection="column"
  width={e.props.bodyColumns || 46}
  borderStyle="round"
  borderColor="text"
  borderDimColor={true}
  paddingX={1}
  paddingY={0}
>
  <HeaderSection run={run} bodyColumns={bodyColumns} />
  <PlanSection tasks={run.tasks} bodyColumns={bodyColumns} />
  <LanesSection tasks={run.tasks} bodyColumns={bodyColumns} />
  <QuotaSection usage={usage} bodyColumns={bodyColumns} />
  {run.waiting && <WaitingSection waiting={run.waiting} bodyColumns={bodyColumns} />}
  <RecentSection events={run.recent} bodyColumns={bodyColumns} />
</Box>
```

---

### 6.2 Header & Next Action (`HeaderSection`)

```tsx
<Box flexDirection="column" marginBottom={1}>
  {/* Row 1: Brand, Run ID, and Timestamp */}
  <Box flexDirection="row" justifyContent="space-between">
    <Box flexDirection="row" gap={1}>
      <Text color="cyan" bold={true}>✦ MatrixKoder</Text>
      <Text dimColor={true}>·</Text>
      <Text color="text">{run.id}</Text>
    </Box>
    <Text dimColor={true}>{currentTimeStr}</Text>
  </Box>

  {/* Row 2: Feature Name, Branch, and Loop Stage */}
  <Box flexDirection="row" gap={1} overflow="hidden">
    <Text bold={true} wrap="truncate-end">{run.feature}</Text>
    <Text dimColor={true}>·</Text>
    <Text dimColor={true}>{run.branch}@{run.sha?.slice(0, 7)}</Text>
    <Text dimColor={true}>·</Text>
    <Text color="cyan" bold={true}>stage {run.stage}/12</Text>
  </Box>

  {/* Row 3: Next Action Banner */}
  {run.next && (
    <Box flexDirection="row" gap={1} marginTop={0}>
      <Text color="cyan" bold={true}>▶ Next:</Text>
      <Text color="text" wrap="truncate-end">{run.next}</Text>
    </Box>
  )}
</Box>
```

---

### 6.3 Plan Waves & Task Rows (`PlanSection`)

```tsx
<Box flexDirection="column" marginBottom={1}>
  {/* Section Divider & Wave Pill Summary */}
  <Box flexDirection="row" justifyContent="space-between" borderStyle="single" borderColor="text" borderDimColor={true}>
    <Box flexDirection="row" gap={1}>
      <Text bold={true}>Plan</Text>
      <Text dimColor={true}>─</Text>
      <Text color="green">{doneCount}/{totalCount} done</Text>
    </Box>
    {/* Wave summary pills */}
    <Box flexDirection="row" gap={1}>
      {waves.map((w) => (
        <Box key={w.id} flexDirection="row">
          <Text dimColor={true}>{w.id} </Text>
          <Text color={w.statusColor} bold={w.isCurrent}>{w.statusGlyphs}</Text>
        </Box>
      ))}
    </Box>
  </Box>

  {/* Task List Rows */}
  <Box flexDirection="column">
    {run.tasks.map((task) => (
      <Box key={task.id} flexDirection="row" justifyContent="space-between" gap={1}>
        {/* Left: Status Icon, Task ID, and Title */}
        <Box flexDirection="row" gap={1} flexShrink={1}>
          <Text color={statusColorMap[task.status]} bold={task.status === 'building' || task.status === 'done'}>
            {statusGlyphMap[task.status]}
          </Text>
          <Text bold={true} color="text">{task.id}</Text>
          <Text dimColor={task.status === 'planned'} wrap="truncate-end">
            {task.title}
          </Text>
        </Box>

        {/* Right: Status Tag / Elapsed / Short SHA */}
        <Box flexDirection="row" gap={1} flexShrink={0}>
          <Text color={statusColorMap[task.status]}>
            {task.status === 'merged' ? 'merged' : task.status}
          </Text>
          {bodyColumns >= 44 && task.last_commit && (
            <Text dimColor={true}>{task.last_commit.slice(0, 7)}</Text>
          )}
        </Box>
      </Box>
    ))}
  </Box>
</Box>
```

---

### 6.4 Active Lanes (`LanesSection`)

```tsx
<Box flexDirection="column" marginBottom={1}>
  <Box borderStyle="single" borderColor="text" borderDimColor={true}>
    <Text bold={true}>Lanes</Text>
  </Box>

  {activeLanes.map((lane) => (
    <Box key={lane.id} flexDirection="row" justifyContent="space-between" gap={1}>
      <Box flexDirection="row" gap={1}>
        <Text dimColor={true}>{lane.slot}</Text>
        <Text color={agentColorMap[lane.agent]} bold={true}>
          {agentGlyphMap[lane.agent]} {lane.agent.toUpperCase()}
        </Text>
        <Text dimColor={true} wrap="truncate-end">{lane.model}</Text>
      </Box>
      <Box flexDirection="row" gap={1}>
        <Text color={lane.isIdle ? 'gray' : 'cyan'}>{lane.state}</Text>
        {bodyColumns >= 44 && lane.lastSha && (
          <Text dimColor={true}>{lane.lastSha.slice(0, 7)}</Text>
        )}
      </Box>
    </Box>
  ))}
</Box>
```

---

### 6.5 Quota & Usage Meter (`QuotaSection`)

```tsx
<Box flexDirection="column" marginBottom={1}>
  {/* Header with Table Columns */}
  <Box flexDirection="row" justifyContent="space-between" borderStyle="single" borderColor="text" borderDimColor={true}>
    <Text bold={true}>Quota</Text>
    <Box flexDirection="row" gap={2}>
      <Text dimColor={true}>5h</Text>
      {bodyColumns >= 44 && <Text dimColor={true}>week</Text>}
      <Text dimColor={true}>reset</Text>
    </Box>
  </Box>

  {/* Agent Usage Rows */}
  {agents.map((ag) => {
    const isExhausted = ag.exhaustedUntil && new Date(ag.exhaustedUntil) > now;
    const barColor = isExhausted || ag.fiveHourPct > 85 ? 'red' : ag.fiveHourPct > 60 ? 'yellow' : 'green';

    return (
      <Box key={ag.name} flexDirection="row" justifyContent="space-between">
        {/* Agent Name + Progress Bar */}
        <Box flexDirection="row" gap={1}>
          <Text color={agentColorMap[ag.name]} bold={isExhausted}>
            {ag.name.padEnd(8)}
          </Text>
          <Text color={barColor} bold={isExhausted}>
            {renderBar(ag.fiveHourPct, bodyColumns >= 44 ? 7 : 5)}
          </Text>
        </Box>

        {/* Pct Columns & Reset Time */}
        <Box flexDirection="row" gap={1}>
          <Text color={barColor} bold={isExhausted}>
            {ag.isNa ? ' n/a' : `${String(ag.fiveHourPct).padStart(3)}%`}
          </Text>
          {bodyColumns >= 44 && (
            <Text dimColor={true}>
              {ag.isNa ? ' n/a' : `${String(ag.weekPct || '—').padStart(4)}%`}
            </Text>
          )}
          <Text dimColor={!isExhausted} color={isExhausted ? 'red' : undefined} bold={isExhausted}>
            {ag.resetStr}
          </Text>
          {isExhausted && <Text color="red" bold={true}>⊘</Text>}
        </Box>
      </Box>
    );
  })}
</Box>
```

---

### 6.6 Waiting On You Modal Card (`WaitingSection`)

```tsx
<Box
  flexDirection="column"
  borderStyle="round"
  borderColor="red"
  paddingX={1}
  paddingY={1}
  marginBottom={1}
>
  <Box flexDirection="row" gap={1} marginBottom={1}>
    <Text color="red" bold={true}>⚠ Waiting on you</Text>
  </Box>
  <Text color="text" bold={true} wrap="wrap">
    {waiting.question}
  </Text>
  {waiting.options && (
    <Box flexDirection="column" marginTop={1}>
      {waiting.options.map((opt, i) => (
        <Text key={opt} dimColor={opt !== waiting.recommendation} color={opt === waiting.recommendation ? 'cyan' : undefined}>
          [{i + 1}] {opt} {opt === waiting.recommendation ? '(Rec)' : ''}
        </Text>
      ))}
    </Box>
  )}
</Box>
```

---

### 6.7 Recent Events Log (`RecentSection`)

```tsx
<Box flexDirection="column">
  <Box borderStyle="single" borderColor="text" borderDimColor={true}>
    <Text bold={true}>Recent</Text>
  </Box>
  {events.slice(0, 3).map((ev, i) => (
    <Box key={i} flexDirection="row" gap={1} overflow="hidden">
      <Text dimColor={true}>{ev.timeStr}</Text>
      <Text color={ev.type === 'verdict' ? 'green' : 'text'} wrap="truncate-end">
        {ev.text}
      </Text>
    </Box>
  ))}
</Box>
```

---

## 7. Verification & Type Cross-Check

All props specified in this document were verified directly against the TypeScript declarations in `/private/tmp/claude-501/bundled-skills/2.1.292/7890036b07367e09a42ab1310b2b02f2/plugin-authoring/types/claude-code.d.ts`:

1. **`Elements['terminal']`** (lines 3775–3795):
   * Confirmed available elements: `Box`, `Text`, `Button`, `Input`, `Select`, `Link`, `Code`, `Markdown`, `Client`, `Raster`, `Image`.
   * **Confirmed absent**: `ProgressBar`, `Spinner`, `Badge`, `Divider` do NOT exist as JSX elements in the terminal constructor table. All progress bars, badges, dividers, and spinners in MatrixKoder are intentionally composed using nested `Box` and `Text` primitives.
2. **`BoxProps`** (lines 901–990):
   * Verified props used: `flexDirection`, `flexGrow`, `flexShrink`, `justifyContent`, `gap`, `width`, `paddingX`, `paddingY`, `marginBottom`, `marginTop`, `borderStyle`, `borderColor`, `borderDimColor`, `overflow`, `display`.
   * Verified allowed `borderStyle` values: `'single' | 'double' | 'round' | 'bold' | 'singleDouble' | 'doubleSingle' | 'classic' | 'arrow' | 'dashed' | 'quote'`.
3. **`TextProps`** (lines 12483–12501):
   * Verified props used: `color`, `backgroundColor`, `dimColor`, `bold`, `italic`, `underline`, `strikethrough`, `inverse`, `wrap` (`'wrap' | 'end' | 'middle' | 'truncate' | 'truncate-start' | 'truncate-middle' | 'truncate-end'`).
4. **`Color` & `ThemeKey`** (lines 1673 & 12510):
   * Verified theme tokens: `'text' | 'inverseText' | 'inactive' | 'subtle' | 'suggestion' | 'remember' | 'success' | 'error' | 'warning' | 'merged' | 'claude' | 'permission' | 'planMode' | 'autoAccept' | 'promptBorder' | 'bashBorder' | 'ide' | 'diffAdded' | 'diffRemoved' | 'diffAddedDimmed' | 'diffRemovedDimmed' | 'diffAddedWord' | 'diffRemovedWord'`.
   * Verified support for raw named ANSI colors: `'green'`, `'red'`, `'yellow'`, `'blue'`, `'magenta'`, `'cyan'`, `'gray'`, `'white'`, `'black'`.
5. **Pane Site Props** (lines 10236–10282):
   * `bodyColumns` (line 10254), `placement` (`'dock' | 'inline'`, line 10262), `isFocused` (line 10249), `title` (line 10244).
6. **API Calls**:
   * `$.ui.status(text: string | undefined)` (line 2449)
   * `$.ui.toast(text: string, options?: { timeoutMs?: number })` (lines 2437, 12551)
