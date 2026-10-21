# MatrixKoder Sidebar Dashboard — Design Specification

> **Component**: Claude Code Mod `Pane` (Fullscreen right-docked sidebar column)  
> **Target Surface**: Terminal (`Elements['terminal']` in Claude Code `ui.render` hook)  
> **Layout Style**: OpenCode Sidebar (Clean full-height column, plain bold section titles, no outer box-drawing frame)  
> **Brand**: MatrixKoder (`/mk`)  
> **Width Contract**: `$.ui.open({ id: "matrixkoder", title: "MatrixKoder", columns: 46 })` (Base: 46 cols, degrades to 36 cols)  
> **Contract Ref**: `docs/plan.md` §6, `docs/state-schema.md` v1, `B-usage-fix1.md` §1–3  
> **Types Declaration**: `plugin-authoring/types/claude-code.d.ts`  

---

## 1. Colour Roles & Theme Adaptability

### 1.1 Palette Philosophy: Named ANSI & ThemeKey Over Raw Hex

Terminal color rendering depends directly on the user's terminal emulator and host color profile (e.g. Solarized Dark vs. Solarized Light, macOS Terminal default white, iTerm2 Dracula, Alacritty Light).

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
| **`accent`** | Brand title, active focus, section headers | `color="cyan"` or `color="claude"` + `bold={true}` | `borderColor="cyan"` | Neon Matrix cyan / Anthropic coral | High-contrast saturated cyan / coral |
| **`ok`** | Completed tasks, merged PRs, clean gates, healthy quota (<60%) | `color="green"` (`success`) | `borderColor="green"` | Bright green (`#22c55e` equivalent) | Dark terminal green (`#15803d` equivalent) |
| **`warn`** | Stalled tasks, high quota (60–85%), non-blocking review | `color="yellow"` (`warning`) + `bold={true}` | `borderColor="yellow"` | Amber / bright yellow | Warm dark amber / brown |
| **`danger`** | Blocked tasks, exhausted quota (>85%), test failure, destructive alert | `color="red"` (`error`) + `bold={true}` | `borderColor="red"` | Vivid red (`#ef4444` equivalent) | Deep crimson red (`#b91c1c` equivalent) |
| **`info`** | Active agent tags, dispatch notices, model identifiers | `color="blue"` or `color="cyan"` (`suggestion`) | `borderColor="blue"` | Crisp sky blue / cyan | Slate / royal blue |
| **`muted`** | Table headers, commit SHAs, elapsed times, inactive waves, pending items | `dimColor={true}` + `color="text"` (`subtle`) | `borderDimColor={true}` | 50% opacity neutral grey | 55% opacity dark slate |
| **`building`** | In-flight execution, active worker lane | `color="cyan"` + `bold={true}` | `borderColor="cyan"` | High-visibility active pulse cyan | Deep teal active pulse |
| **`divider`** | Thin rules between sections | N/A | `borderStyle="single"` + `borderColor="text"` + `borderDimColor={true}` | Subtle faint separator | Subtle faint separator |

---

## 2. Icon & Glyph System

### 2.1 Single-Width Monospace Rule

Standard emoji (e.g. `⛔`, `❌`, `🟢`, `⏳`, `🔧`) are treated by terminal emulators and POSIX `wcwidth()` as **2 character columns wide**. In docked terminal panes with fixed column budgets (44–48 columns), 2-column emoji break monospace tabular vertical alignment, causing jagged columns and unintended line wraps.

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

*\*Note on `⚠` (U+26A0)*: If the host terminal renders `⚠` as 2 columns, MatrixKoder automatically falls back to single-width `!` to maintain exact columnar grid alignment.

### 2.3 Agent Kind Glyphs (Strictly Unique — No Collision with Task Statuses)

To resolve glyph collisions where agent icons previously reused task status symbols (e.g. AGY `▲` = fix, Codex `◈` = merged, OpenCode `◇` = routed), each agent is assigned a unique, dedicated single-width glyph:

| Agent Kind | Unicode Glyph | Codepoint | Plain ASCII | Visual Rationale | Brand Colour | `TextProps` |
|---|---|---|---|---|---|---|
| `claude` | `✦` | `U+2726` | `*` | Anthropic Sparkle / Star | `magenta` (`claude`) | `color="magenta" bold={true}` |
| `agy` (Antigravity)| `∇` | `U+2207` | `^` | Nabla / Anti-Gravity gradient | `blue` | `color="blue" bold={true}` |
| `codex` | `⊚` | `U+229A` | `o` | Circled Ring / OpenAI loop | `green` | `color="green" bold={true}` |
| `opencode` | `⌂` | `U+2302` | `#` | Open Terminal / Root | `cyan` | `color="cyan" bold={true}` |

### 2.4 Section, Control & Checkbox Glyphs

| Element | Glyph | Codepoint | Plain ASCII | Role |
|---|---|---|---|---|
| **MatrixKoder Brand** | `⬡` | `U+2B21` | `*` | `accent` (`color="cyan"`) — Cyber/Matrix lattice |
| **Next Action / Prompt** | `▶` | `U+25B6` | `>` | `accent` (`color="cyan"`) |
| **Sub-pool Tree Branch**| `└` | `U+2514` | `\` | `muted` (`dimColor={true}`) |
| **Todo: Current Task** | `[•]` (`•`) | `U+2022` | `[*]` | `building` (`color="cyan" bold={true}`) |
| **Todo: Pending Task** | `[ ]` | `U+0020` | `[ ]` | `muted` (`dimColor={true}`) |
| **Todo: Done Task** | `[✓]` (`✓`)| `U+2713` | `[x]` | `ok` (`color="green" bold={true}`) |
| **Todo: Attention Task**| `[!]` (`!`) | `U+0021` | `[!]` | `warn` / `danger` (`color="yellow"` / `color="red"`) |
| **Wave Settled** | `✓` | `U+2713` | `v` | `ok` (`color="green"`) — Same meaning as `done` |
| **Wave In-Progress** | `◐` | `U+25D0` | `o` | `warn` (`color="yellow"`) — Same meaning as `review` / active |
| **Wave Pending** | `○` | `U+25CB` | `.` | `muted` (`dimColor={true}`) — Same meaning as `planned` |
| **Usage Meter Full** | `█` | `U+2588` | `#` | `ok` / `warn` / `danger` |
| **Usage Meter Empty** | `░` | `U+2591` | `-` | `muted` (`dimColor={true}`) |
| **Exhausted Badge** | `⊘` | `U+2298` | `X` | `danger` (`color="red"`) — Same meaning as `blocked` |

### 2.5 Uniqueness Check Table

Every glyph used across §2.2, §2.3, and §2.4 is verified below to ensure zero collision. Each glyph has exactly **one canonical meaning**:

| Glyph | Unicode | Name | Assigned Category | Canonical Meaning |
|---|---|---|---|---|
| `○` | `U+25CB` | White Circle | Task Status / Wave | Planned / Pending wave |
| `◇` | `U+25C7` | White Diamond | Task Status | Routed |
| `▷` | `U+25B7` | White Right Triangle | Task Status | Dispatched |
| `●` | `U+25CF` | Black Circle | Task Status | Building |
| `⧖` | `U+29D6` | White Hourglass | Task Status | Verifying |
| `◐` | `U+25D0` | Circle Left Black | Task Status / Wave | In review / In-progress wave |
| `▲` | `U+25B2` | Up Black Triangle | Task Status | Fix round |
| `◈` | `U+25C8` | Diamond in Diamond | Task Status | Merged |
| `◆` | `U+25C6` | Black Diamond | Task Status | QA |
| `✓` | `U+2713` | Check Mark | Task Status / Wave / Todo | Done / Settled |
| `⊘` | `U+2298` | Circled Slash | Task Status / Quota | Blocked / Quota exhausted |
| `⧗` | `U+29D7` | Black Hourglass | Task Status | Quota rate-limited |
| `⚠` | `U+26A0` | Warning Sign | Task Status | Stalled worker |
| `⊗` | `U+2297` | Circled Times | Task Status | Provider error |
| `✦` | `U+2726` | 4-Pointed Star | Agent Kind | Claude |
| `∇` | `U+2207` | Nabla | Agent Kind | Antigravity (AGY) |
| `⊚` | `U+229A` | Circled Ring | Agent Kind | Codex |
| `⌂` | `U+2302` | House / Terminal | Agent Kind | OpenCode |
| `⬡` | `U+2B21` | White Hexagon | Brand | MatrixKoder brand lattice |
| `▶` | `U+25B6` | Black Right Pointer | Control | Next Action / Prompt pointer |
| `•` | `U+2022` | Bullet | Todo Checkbox | In-flight / Current task `[•]` |
| `└` | `U+2514` | Box Light Up-Right | Quota Tree | Sub-pool indicator |
| `█` | `U+2588` | Full Block | Quota Meter | Filled usage cell |
| `░` | `U+2591` | Light Shade | Quota Meter | Empty headroom cell |

> **Note on Semantic Reuse**: Reusing a status glyph with the **exact same meaning** (e.g. `✓` for task `done`, wave `settled`, and todo `[✓]`; `○` for `planned` and wave `pending`; `⊘` for `blocked` and pool `exhausted`) is intentional and semantically unified. No glyph represents two different concepts.

---

## 3. Visual Mockups & State Variations (OpenCode Sidebar Column)

### Layout Philosophy: OpenCode Right Panel
* **No Outer Frame**: The Claude Code mod engine already draws the docked pane's tab title (`MatrixKoder`), border, and close mark (`ctrl+x x`). Drawing an inner box (`╭───╮`, `│   │`, `╰───╯`) wastes 2 character columns of horizontal budget and creates visual clutter.
* **OpenCode Style**: A clean, full-height vertical column with plain bold section titles (`Run`, `Plan`, `Lanes`, `Usage`, `Waiting on you`, `Recent`), progress/todo-style task lists, and subtle thin rules or blank lines between sections.
* **Width**: `$.ui.open({ id: "matrixkoder", title: "MatrixKoder", columns: 46 })`. The requested base width is **46 columns**.

---

### 3.1 Standard Active Run (Base: 46 Columns — OpenCode Style)

```
Run
workflows-v2 · main@82252eb · stage 7/12
▶ Next: verify F6 gates in worktree-b

Plan  3/6 done  W0 ✓  W1 ◐◐●  W2 ○
[✓] W0   wizard split       merged   62811f9
[•] F6   design+configure   review r2
[•] F8   bulk delete        building 12m
[!] FIX6 QA1 fixes          stalled  11m
[ ] X1   design pass        waits F6, F8

Lanes
a  ∇ AGY     flash-high   idle       54fc3b5

Usage                        5h   week   reset
claude    █░░░░░░░           7%    36%   4h03m
agy       ████░░░░          54%    52%   1h15m
  └ claude-gpt ████████    100%   100% 10-13 ⊘
opencode  ████████         100%     —    18:54 ⊘

Waiting on you
F8 deletes rows — approve destructive step?

Recent
18:40 F6 r2 gates 6196 ✓ · tsc 121 = base
```

#### Color & Style Annotations for Base Mockup
* **Section Titles** (`Run`, `Plan`, `Lanes`, `Usage`, `Waiting on you`, `Recent`):
  * Rendered as plain `<Text bold={true} color="text">` or `<Text bold={true} color="cyan">`.
* **Run Section**:
  * Line 1: `workflows-v2` (`bold={true}`), ` · ` (`dimColor`), `main@82252eb` (`color="text"`), ` · ` (`dimColor`), `stage 7/12` (`color="cyan" bold={true}`).
  * Line 2: `▶ Next: ` (`color="cyan" bold={true}`), `verify F6 gates in worktree-b` (`color="text"`).
* **Plan Section**:
  * Header line: `Plan` (`bold={true}`), `3/6 done` (`color="green"`), `W0 ✓` (`W0` `dimColor`, `✓` `color="green" bold={true}`), `W1 ◐◐●` (`W1` `dimColor`, `◐` `color="yellow"`, `●` `color="cyan" bold={true}`), `W2 ○` (`W2` `dimColor`, `○` `dimColor`).
  * Row `[✓] W0`: `[✓]` (`color="green" bold={true}`), `W0` (`bold={true}`), `merged` (`color="green"`), `62811f9` (`dimColor`).
  * Row `[•] F6`: `[•]` (`color="cyan" bold={true}`), `F6` (`bold={true}`), `review r2` (`color="yellow"`).
  * Row `[•] F8`: `[•]` (`color="cyan" bold={true}`), `F8` (`bold={true}`), `building` (`color="cyan"`), `12m` (`dimColor`).
  * Row `[!] FIX6`: `[!]` (`color="yellow" bold={true}`), `FIX6` (`bold={true}`), `stalled` (`color="yellow" bold={true}`), `11m` (`dimColor`).
  * Row `[ ] X1`: `[ ]` (`dimColor`), `X1` (`dimColor`), `waits F6, F8` (`dimColor`).
* **Lanes Section**:
  * Row `a  ∇ AGY`: `a` (`dimColor`), `∇` (`color="blue" bold={true}`), `AGY` (`color="blue" bold={true}`), `flash-high` (`dimColor`), `idle` (`dimColor`), `54fc3b5` (`dimColor`).
* **Usage Section (Multi-Pool)**:
  * Header line: `Usage` (`bold={true}`), `5h   week   reset` (`dimColor`).
  * Row `claude`: `claude` (`color="magenta"`), `█░░░░░░░` (bar: `█` `color="green"`, `░` `dimColor`), `7%` (`color="green"`), `36%` (`color="green"`), `4h03m` (`dimColor`).
  * Row `agy`: `agy` (`color="blue"`), `████░░░░` (bar: `color="yellow"`), `54%` (`color="green"`), `52%` (`color="green"`), `1h15m` (`dimColor`).
  * Sub-row `  └ claude-gpt`: `  └` (`dimColor`), `claude-gpt` (`dimColor`), `████████` (`color="red" bold={true}`), `100%` (`color="red"`), `100%` (`color="red"`), `10-13` (`dimColor`), `⊘` (`color="red" bold={true}`).
  * Row `opencode`: `opencode` (`color="cyan"`), `████████` (`color="red" bold={true}`), `100%` (`color="red" bold={true}`), `—` (`dimColor`), `18:54` (`dimColor`), `⊘` (`color="red" bold={true}`).
* **Waiting on you**:
  * Title: `Waiting on you` (`color="red" bold={true}`).
  * Body: `F8 deletes rows — approve destructive step?` (`color="text" bold={true}`).
* **Recent**:
  * Title: `Recent` (`bold={true}`).
  * Body: `18:40` (`dimColor`), `F6 r2 gates 6196 ✓` (`✓` `color="green"`), ` · ` (`dimColor`), `tsc 121 = base` (`dimColor`).

---

### 3.2 Alternative Task Row Style: Status-Glyph List

Developers can toggle between progress/todo checkbox style (`[✓]`, `[•]`, `[ ]`) and canonical status glyph style:

```
Plan  3/6 done  W0 ✓  W1 ◐◐●  W2 ○
✓ W0   wizard split       merged   62811f9
◐ F6   design+configure   review r2
● F8   bulk delete        building 12m
⚠ FIX6 QA1 fixes          stalled  11m
○ X1   design pass        waits F6, F8
```

---

### 3.3 State Variation: No Active Run (Empty State)

Shown when no run is live in the project DB (`~/.matrixkoder/projects/<slug>/mk.db`) or after a run has been archived.

```
Run
No active orchestration run.

To start a multi-lane feature run, ask Claude to orchestrate it.
▶ New project? Run /mk setup

Recent Runs
✓ wfv2-f678  workflows-v2      archived
✓ mk-p0      mod spikes        done
```

This is the Overview tab's empty state; the Usage tab shows the ledger on its own.

---

### 3.4 State Variation: First-Run Setup (Wizard Incomplete)

Shown when the project's configuration is missing. The wizard writes `<data>/matrixkoder.yaml`, or the C2 `<data>/project.yaml` for a project whose root row has a `db_path` set (migrated); in home mode `<data>` is `~/.matrixkoder/projects/<slug>`. Global plugin prefs live in the plugin's userConfig, not a file. `/mk setup` opens a 46-column pane, `/mk init <name>` sets the project slug, and nothing is written until the final confirm — the dashboard preferences included.

```
Setup
⬡ Welcome to MatrixKoder!

Configuration required for this project:
[!] No project config yet (projects/<slug>/project.yaml)

▶ Run /mk setup to launch the wizard

System Detection
✓ Git Repository       main worktree
✓ CodexBar CLI         v0.73.0 active
⚠ Agents Configured    0 / 4 enabled
⚠ Gate Baselines       not measured
```

---

### 3.5 State Variation: All Agents Exhausted (All Pools Locked)

Critical state when every builder pool is locked by rate limits or weekly quotas. Automated turns pause and display reset countdowns.

```
Run
workflows-v2 · main@82252eb · stage 7/12
⊘ ALL BUILDER AGENTS EXHAUSTED
Next available reset in: 1h 12m

Usage                        5h   week   reset
claude    ████████         100%   100% 19:54 ⊘
agy       ████████         100%   100% 19:15 ⊘
  └ claude-gpt ████████    100%   100% 10-13 ⊘
opencode  ████████         100%     —  20:15 ⊘
codex     ░░░░░░░░          n/a    n/a (key)

Orchestrator Paused
Automated turns are suspended until quota resets.
Switch provider or adjust limits with /mk agent.
```

---

### 3.6 State Variation: Waiting-On-User Present (Modal Action Card)

When an agent needs human confirmation (destructive SQL migration or ambiguous spec), the `Waiting on you` section expands into a prominent action card.

```
Run
workflows-v2 · main@82252eb · stage 7/12
⚠ PAUSED: Awaiting human confirmation

Waiting on you
Task F8 requests confirmation:
"F8 deletes rows — approve destructive
 database migration step?"

Options:
[1] Approve & run migration (Rec)
[2] Abort migration & keep backup
[3] Inspect diff in worktree

▶ Reply 1, 2, or 3 in the prompt

Plan  3/6 done  W0 ✓  W1 ◐◐●  W2 ○
[•] F8   bulk delete        waiting approval
```

---

### 3.7 Compact 36-Column Degradation Variant (`bodyColumns = 36`)

When the terminal split or window width shrinks to `bodyColumns = 36`:

```
Run
wfv2 · main@82252eb · stg 7/12
▶ Next: verify F6 gates in wt-b

Plan · 3/6 ── W0:✓ W1:3 W2:○
[✓] W0  wizard split   merged
[•] F6  design+config  review r2
[•] F8  bulk delete    build 12m
[!] FX6 QA1 fixes      stall 11m
[ ] X1  design pass    waits F6

Lanes
a ∇ AGY   flash-high  idle

Usage               5h     reset
claude   █░░░░░     7%     4h03m
agy      ████░     54%     1h15m
 └ c-gpt █████    100%   10-13 ⊘
opencode █████    100%   18:54 ⊘

Waiting on you
Approve destructive step in F8?

Recent
18:40 F6 r2 gates 6196 ✓
```

#### Detailed 36-Column Degradation Strategy
1. **Run**: Feature name and stage string condensed (`stg 7/12`). Next action path shortened (`wt-b` instead of `worktree-b`).
2. **Plan Wave Pills**: Expansive glyph list (`W1 ◐◐●`) collapses to compact wave count (`W1:3` or `W1:2/3`).
3. **Task Rows**: Task title width constrained to 14 characters using `wrap="truncate-end"`. Commit SHAs (`62811f9`) hidden; status column stays visible.
4. **Lanes**: Commit SHA hidden; lane name, agent icon, and model preserved.
5. **Usage Section**: The `week` column is dropped entirely; only the critical short-cycle `5h` bar and `reset` time are retained. Bar width drops from 7 cells to 5 cells. Sub-pool labels shorten (`c-gpt`).
6. **Waiting on you**: Multi-line questions are summarized or wrapped cleanly with `wrap="wrap"`.
7. **Recent**: Secondary clauses (e.g. ` · tsc 121 = base`) dropped, keeping primary event and status.

---

### 3.8 Inline Layout State (`placement === 'inline'`)

When the user is not in fullscreen terminal layout, the Claude Code engine places the pane inline above the prompt (`e.props.placement === 'inline'`).

The pane renders an immediate layout hint at the top of the body:

```
┌────────────────────────────────────────────┐
│ Run /tui fullscreen to dock this panel on  │
│ the right.                                 │
└────────────────────────────────────────────┘

Run
workflows-v2 · main@82252eb · stage 7/12
▶ Next: verify F6 gates in worktree-b

Plan  3/6 done  W0 ✓  W1 ◐◐●  W2 ○
...
```

* Rendered via: `<Box borderStyle="round" borderColor="yellow" paddingX={1}><Text color="yellow">Run /tui fullscreen to dock this panel on the right.</Text></Box>`.

---

## 4. Quota & Multi-Pool Usage Architecture

### 4.1 Pool Hierarchy & Mapping Table

Per `B-usage-fix1.md` §1–3, usage tracking groups provider windows into pools to prevent a blocked secondary model pool from falsely marking the entire agent exhausted.

Mapping table:
* **`agy`**:
  * `gemini`: Primary pool (window id contains `-gemini-` or title starts `Gemini`).
  * `claude-gpt`: Secondary pool (id contains `-3p-` or title `Claude and GPT` / `Claude/GPT`).
* **`claude`**:
  * `default`: Primary pool.
  * `fable`: Secondary pool (id contains `scoped-fable` or title `Fable only`).
* **`codex` / `opencode`**:
  * `default`: Primary pool.

Data shape in `~/.matrixkoder/usage.json`:
```json
{
  "agents": {
    "agy": {
      "source": "codexbar",
      "available_pools": ["gemini"],
      "five_hour": { "used_pct": 54, "resets_at": "2026-10-07T22:30:00Z" },
      "weekly": { "used_pct": 52, "resets_at": "2026-10-13T18:43:00Z" },
      "exhausted_until": null,
      "pools": {
        "gemini": {
          "five_hour": { "used_pct": 54, "resets_at": "2026-10-07T22:30:00Z" },
          "weekly": { "used_pct": 52, "resets_at": "2026-10-13T18:43:00Z" },
          "exhausted_until": null
        },
        "claude-gpt": {
          "five_hour": { "used_pct": 100, "resets_at": "2026-10-07T22:30:00Z" },
          "weekly": { "used_pct": 100, "resets_at": "2026-10-13T18:43:00Z" },
          "exhausted_until": "2026-10-13T18:43:00Z"
        }
      }
    }
  }
}
```

### 4.2 Bar Glyphs & Threshold Rules

* Full / Used block: `█` (`U+2588`, Full Block)
* Empty / Remaining block: `░` (`U+2591`, Light Shade)
* Standard bar width: **7 characters** (5 characters in 36-col mode).

#### Threshold Color Rules
* **`< 60%` used**: `color="green"` (`ok`) — Healthy headroom.
* **`60% – 85%` used**: `color="yellow"` (`warn`) — Approaching limit; orchestrator paces dispatches.
* **`> 85%` used**: `color="red" bold={true}` (`danger`) — Critical quota; danger of stall.
* **`100%` or `exhausted_until > now`**: `color="red" bold={true}` + single-width `⊘` badge.

### 4.3 Multi-Pool Display & Exhaustion Rules

1. **Primary Row**:
   * Displays the agent's primary pool (AGY: `gemini`, Claude: `default`).
   * Displays the agent's overall status.
2. **Sub-Rows (`└ <pool>`)**:
   * Indented sub-rows are rendered for secondary pools (`claude-gpt`, `fable`).
   * Formatted as: `  └ <pool-name> <bar> <5h%> <week%> <reset> <badge>`.
3. **Agent Exhaustion Rule**:
   * An agent is marked red / exhausted (`exhausted_until` set at agent level) **ONLY when EVERY pool is exhausted**.
   * If a secondary pool alone is exhausted (e.g. AGY `claude-gpt` is 100% until 10-13, but `gemini` is 54%), the primary AGY row remains green/yellow and available for routing. Only the sub-row shows `100%` and `⊘`!

---

## 5. Status-Line Chip & Notification Toasts

### 5.1 Status-Line Chip (`$.ui.status`)

Pinned under the prompt via `$.ui.status(text)`. Pinned chips must be concise (< 80 chars) and immediately communicate run health and blockers.

#### Copy Templates & State Mapping
1. **Normal Active Progress**:
   `⬡ mk · 1 building · 1 review · 3/6 done`
2. **Awaiting User Action (Highest Priority)**:
   `⬡ mk · ⚠ 1 waiting on you · 1 building`
3. **Worker Stalled**:
   `⬡ mk · ⚠ FIX6 stalled (11m) · 1 building`
4. **Agent Quota Exhausted**:
   `⬡ mk · agy ⊘ wk resets 10-13 · 1 building`
5. **Dependency Blocked**:
   `⬡ mk · ⊘ X1 blocked by F6, F8 · 2 building`
6. **All Done**:
   `⬡ mk · ✓ all 6 tasks merged · stage 12/12`
7. **Idle / Waiting Next**:
   `⬡ mk · stage 7/12 · idle · next: verify F6`

### 5.2 Notification Toast Copy (`$.ui.toast`)

Triggered on asynchronous state transitions via `$.ui.toast(text, { timeoutMs: 4000 })`.

| Transition Event | Toast Text Template | Example Rendered Toast |
|---|---|---|
| **`DONE`** (Task Merged) | `⬡ [MatrixKoder] Task <id> merged & passed gates (<sha>)` | `⬡ [MatrixKoder] Task W0 merged & passed gates (62811f9)` |
| **`QUOTA`** (Rate Limit Hit)| `⬡ [MatrixKoder] Quota reached for <agent>.<pool> (<window>). Resets <time>.` | `⬡ [MatrixKoder] Quota reached for agy.claude-gpt (weekly). Resets 10-13.` |
| **`STALL`** (Worker Idle) | `⬡ [MatrixKoder] Task <id> stalled: no activity in lane <lane> (<min>m)` | `⬡ [MatrixKoder] Task FIX6 stalled: no activity in wt-fix6 (11m)` |
| **`BLOCKED`** (Dependency) | `⬡ [MatrixKoder] Task <id> blocked waiting on <deps>` | `⬡ [MatrixKoder] Task X1 blocked waiting on F6, F8` |
| **`PROVIDER`** (API Error) | `⬡ [MatrixKoder] Provider error on <agent>: <summary>` | `⬡ [MatrixKoder] Provider error on codex: 503 Service Unavailable` |
| **`WAITING`** (Human Prompt)| `⬡ [MatrixKoder] Action required: <question>` | `⬡ [MatrixKoder] Action required: Approve destructive step in F8?` |

---

## 6. Component Tree Architecture & Implementation Spec (OpenCode Style)

All components are rendered in TypeScript/TSX for `ui.render` when `e.component === 'Pane'`.
Only props verified in `claude-code.d.ts` are used.

### 6.1 Root Dashboard Container (`PaneRoot`)

```tsx
<Box
  flexDirection="column"
  width={e.props.bodyColumns || 46}
  paddingX={0}
  paddingY={0}
  gap={1}
>
  {/* Inline fullscreen suggestion if not docked */}
  {e.props.placement === 'inline' && (
    <Box borderStyle="round" borderColor="yellow" paddingX={1} marginBottom={1}>
      <Text color="yellow">Run /tui fullscreen to dock this panel on the right.</Text>
    </Box>
  )}

  <RunSection run={run} bodyColumns={bodyColumns} />
  <PlanSection tasks={run.tasks} bodyColumns={bodyColumns} />
  <LanesSection tasks={run.tasks} bodyColumns={bodyColumns} />
  <UsageSection usage={usage} bodyColumns={bodyColumns} />
  {run.waiting && <WaitingSection waiting={run.waiting} bodyColumns={bodyColumns} />}
  <RecentSection events={run.recent} bodyColumns={bodyColumns} />
</Box>
```

---

### 6.2 Run Section (`RunSection`)

```tsx
<Box flexDirection="column">
  <Text bold={true} color="text">Run</Text>
  <Box flexDirection="row" gap={1} overflow="hidden">
    <Text bold={true}>{run.feature}</Text>
    <Text dimColor={true}>·</Text>
    <Text dimColor={true}>{run.branch}{run.head_sha ? `@${run.head_sha.slice(0, 7)}` : ''}</Text>
    <Text dimColor={true}>·</Text>
    <Text color="cyan" bold={true}>stage {run.stage}/12</Text>
  </Box>

  {run.next && (
    <Box flexDirection="row" gap={1}>
      <Text color="cyan" bold={true}>▶ Next:</Text>
      <Text color="text" wrap="truncate-end">{run.next}</Text>
    </Box>
  )}
</Box>
```

---

### 6.3 Plan Section (`PlanSection`)

```tsx
<Box flexDirection="column">
  {/* Section Header with Wave Pills */}
  <Box flexDirection="row" justifyContent="space-between">
    <Box flexDirection="row" gap={1}>
      <Text bold={true} color="text">Plan</Text>
      <Text color="green">{doneCount}/{totalCount} done</Text>
    </Box>
    <Box flexDirection="row" gap={1}>
      {waves.map((w) => (
        <Box key={w.id} flexDirection="row">
          <Text dimColor={true}>{w.id} </Text>
          <Text color={w.statusColor} bold={w.isCurrent}>{w.statusGlyphs}</Text>
        </Box>
      ))}
    </Box>
  </Box>

  {/* Todo-Style Task Rows */}
  <Box flexDirection="column">
    {run.tasks.map((task) => {
      const isDone = task.status === 'done' || task.status === 'merged';
      const isBuilding = task.status === 'building' || task.status === 'verifying';
      const isAttention = task.status === 'stalled' || task.status === 'blocked';
      const checkbox = isDone ? '[✓]' : isBuilding ? '[•]' : isAttention ? '[!]' : '[ ]';
      const checkColor = isDone ? 'green' : isBuilding ? 'cyan' : isAttention ? 'yellow' : 'gray';

      return (
        <Box key={task.id} flexDirection="row" justifyContent="space-between" gap={1}>
          <Box flexDirection="row" gap={1} flexShrink={1}>
            <Text color={checkColor} bold={!isDone && task.status !== 'planned'}>{checkbox}</Text>
            <Text bold={true}>{task.id}</Text>
            <Text dimColor={task.status === 'planned'} wrap="truncate-end">{task.title}</Text>
          </Box>
          <Box flexDirection="row" gap={1} flexShrink={0}>
            <Text color={statusColorMap[task.status]}>{task.status}</Text>
            {bodyColumns >= 44 && task.last_commit && (
              <Text dimColor={true}>{task.last_commit.slice(0, 7)}</Text>
            )}
          </Box>
        </Box>
      );
    })}
  </Box>
</Box>
```

---

### 6.4 Lanes Section (`LanesSection`)

```tsx
<Box flexDirection="column">
  <Text bold={true} color="text">Lanes</Text>

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

### 6.5 Usage Section with Multi-Pool Sub-Rows (`UsageSection`)

```tsx
<Box flexDirection="column">
  {/* Header with Columns */}
  <Box flexDirection="row" justifyContent="space-between">
    <Text bold={true} color="text">Usage</Text>
    <Box flexDirection="row" gap={2}>
      <Text dimColor={true}>5h</Text>
      {bodyColumns >= 44 && <Text dimColor={true}>week</Text>}
      <Text dimColor={true}>reset</Text>
    </Box>
  </Box>

  {/* Agent and Pool Rows */}
  {agents.map((ag) => {
    const isExhausted = ag.exhaustedUntil && new Date(ag.exhaustedUntil) > now;
    const barColor = isExhausted || ag.fiveHourPct > 85 ? 'red' : ag.fiveHourPct > 60 ? 'yellow' : 'green';

    return (
      <Box key={ag.name} flexDirection="column">
        {/* Primary Agent Row */}
        <Box flexDirection="row" justifyContent="space-between">
          <Box flexDirection="row" gap={1}>
            <Text color={agentColorMap[ag.name]} bold={isExhausted}>
              {ag.name.padEnd(8)}
            </Text>
            <Text color={barColor} bold={isExhausted}>
              {renderBar(ag.fiveHourPct, bodyColumns >= 44 ? 7 : 5)}
            </Text>
          </Box>
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

        {/* Indented Sub-Pool Rows */}
        {ag.extraPools?.map((pool) => {
          const poolExhausted = pool.exhaustedUntil && new Date(pool.exhaustedUntil) > now;
          const poolBarColor = poolExhausted || pool.fiveHourPct > 85 ? 'red' : pool.fiveHourPct > 60 ? 'yellow' : 'green';

          return (
            <Box key={pool.name} flexDirection="row" justifyContent="space-between">
              <Box flexDirection="row" gap={1}>
                <Text dimColor={true}>  └ {pool.name.slice(0, 10).padEnd(10)}</Text>
                <Text color={poolBarColor} bold={poolExhausted}>
                  {renderBar(pool.fiveHourPct, bodyColumns >= 44 ? 7 : 5)}
                </Text>
              </Box>
              <Box flexDirection="row" gap={1}>
                <Text color={poolBarColor} bold={poolExhausted}>
                  {String(pool.fiveHourPct).padStart(3)}%
                </Text>
                {bodyColumns >= 44 && (
                  <Text dimColor={true}>
                    {pool.weekPct != null ? `${String(pool.weekPct).padStart(4)}%` : '   —'}
                  </Text>
                )}
                <Text dimColor={!poolExhausted} color={poolExhausted ? 'red' : undefined} bold={poolExhausted}>
                  {pool.resetStr}
                </Text>
                {poolExhausted && <Text color="red" bold={true}>⊘</Text>}
              </Box>
            </Box>
          );
        })}
      </Box>
    );
  })}
</Box>
```

---

### 6.6 Waiting On You Section (`WaitingSection`)

```tsx
<Box flexDirection="column">
  <Text color="red" bold={true}>Waiting on you</Text>
  <Text color="text" bold={true} wrap="wrap">{waiting.question}</Text>
  {waiting.options && (
    <Box flexDirection="column" marginTop={1}>
      {waiting.options.map((opt, i) => (
        <Text key={opt} dimColor={opt !== waiting.rec} color={opt === waiting.rec ? 'cyan' : undefined}>
          [{i + 1}] {opt} {opt === waiting.rec ? '(Rec)' : ''}
        </Text>
      ))}
    </Box>
  )}
</Box>
```

---

### 6.7 Recent Section (`RecentSection`)

```tsx
<Box flexDirection="column">
  <Text bold={true} color="text">Recent</Text>
  {events.slice(0, 3).map((ev, i) => (
    <Box key={i} flexDirection="row" gap={1} overflow="hidden">
      <Text dimColor={true}>{ev.t.slice(11, 16)}</Text>
      <Text color={ev.type === 'verdict' ? 'green' : 'text'} wrap="truncate-end">
        {ev.text}
      </Text>
    </Box>
  ))}
</Box>
```

---

## 7. Schema Requests & Field Inventory

Every data field displayed across the dashboard mockups was checked against `docs/state-schema.md` (the project DB `~/.matrixkoder/projects/<slug>/mk.db`, plus `usage.json`):

### 7.1 Field Inventory & Verification
The dashboard reads the project DB (the ONLY source of truth); the root `~/.matrixkoder/mk.db` holds only overview numbers and linkage (`projects`, `plan_summary`). No `run.json` snapshot is written or read.
* `run.id`: **Exists** (project DB run row `id`).
* `run.feature`: **Exists** (project DB run row `feature`).
* `run.branch`: **Exists** (project DB run row `branch`).
* `run.stage`: **Exists** (project DB run row `stage`, int 0–12).
* `run.next`: **Exists** (project DB run row `next`, string | null).
* `run.waiting`: **Exists** (project DB run row `waiting`: `{ question, options, rec, since }`).
* `tasks[].id`, `title`, `wave`, `status`, `last_commit`: **Exists** in project DB `tasks`.
* `tasks[].agent`, `model`, `lane`: **Exists** in project DB `tasks`.
* `recent[].t`, `text`, `type`: **Exists** in the project DB `events` rows.
* `usage.agents.<kind>.pools`: **Exists** per `B-usage-fix1.md`.
* `usage.agents.<kind>.available_pools`: **Exists** per `B-usage-fix1.md`.

### 7.2 Formal Schema Requests
To ensure no fields are invented or assumed without schema contract:

1. **Schema Request 1: `run.head_sha` on the project DB run row**
   * *Motivation*: The mockups display the current commit SHA of the run branch (`main@82252eb`). The run row carries `"branch": "main"` but no root commit SHA (only individual tasks carry `last_commit`).
   * *Requested Addition*: Add optional field `"head_sha": string | null` to the project DB run row in `docs/state-schema.md`.
   * *Fallback in Mod*: If `head_sha` is absent, the dashboard renders `main` without `@<sha>`.
2. **Schema Request 2: Confirmation of `pools` Ledger Structure**
   * *Status*: Confirmed aligned with `B-usage-fix1.md` §1–3 (`pools: { <name>: { five_hour, weekly, exhausted_until } }`).

---

## 8. Verification & Types Cross-Check

All props specified in this document were verified directly against the TypeScript declarations in `/private/tmp/claude-501/bundled-skills/2.1.292/7890036b07367e09a42ab1310b2b02f2/plugin-authoring/types/claude-code.d.ts`:

1. **`Elements['terminal']`** (lines 3775–3795):
   * Confirmed available elements: `Box`, `Text`, `Button`, `Input`, `Select`, `Link`, `Code`, `Markdown`, `Client`, `Raster`, `Image`.
   * **Confirmed absent**: `ProgressBar`, `Spinner`, `Badge`, and `Divider` do NOT exist as standalone terminal JSX constructors in Claude Code's element table (`Spinner` only exists as a transcript interception hook `RenderComponent`, line 9292). All meters, dividers, and badges are composed using nested `Box` and `Text` primitives.
2. **`BoxProps`** (lines 901–990):
   * Verified props used: `flexDirection`, `flexGrow`, `flexShrink`, `flexWrap`, `alignItems`, `alignSelf`, `justifyContent`, `gap`, `columnGap`, `rowGap`, `width`, `height`, `minWidth`, `minHeight`, `margin`, `marginX`, `marginY`, `padding`, `paddingX`, `paddingY`, `borderStyle`, `borderColor`, `borderDimColor`, `backgroundColor`, `overflow`, `display`, `position`, `top`, `bottom`, `left`, `right`, `hover`.
   * Verified allowed `borderStyle` values: `'single' | 'double' | 'round' | 'bold' | 'singleDouble' | 'doubleSingle' | 'classic' | 'arrow' | 'dashed' | 'quote'`.
3. **`TextProps`** (lines 12483–12501):
   * Verified props used: `color`, `backgroundColor`, `dimColor`, `bold`, `italic`, `underline`, `strikethrough`, `inverse`, `wrap` (`'wrap' | 'end' | 'middle' | 'truncate' | 'truncate-start' | 'truncate-middle' | 'truncate-end'`).
4. **`Color` & `ThemeKey`** (lines 1673 & 12510):
   * Verified theme tokens: `'text' | 'inverseText' | 'inactive' | 'subtle' | 'suggestion' | 'remember' | 'success' | 'error' | 'warning' | 'merged' | 'claude' | 'permission' | 'planMode' | 'autoAccept' | 'promptBorder' | 'bashBorder' | 'ide' | 'diffAdded' | 'diffRemoved' | 'diffAddedDimmed' | 'diffRemovedDimmed' | 'diffAddedWord' | 'diffRemovedWord'`. Raw named ANSI colors (`'green'`, `'red'`, `'yellow'`, `'blue'`, `'magenta'`, `'cyan'`, `'gray'`, `'white'`, `'black'`) are accepted and adapt to light/dark terminal palettes.
5. **Pane Site Props** (lines 10236–10282):
   * `bodyColumns` (line 10254), `title` (line 10244), `isFocused` (line 10249), `placement` (`'dock' | 'inline'`, line 10262), `scroll` (line 10271), `view` (line 10280).
6. **API Calls**:
   * `$.ui.status(text: string | undefined)` (line 2449)
   * `$.ui.toast(text: string, options?: { timeoutMs?: number })` (lines 2437, 12551)
   * `$.ui.open({ id: string, title?: string, columns?: number })` (lines 2465–2472)
