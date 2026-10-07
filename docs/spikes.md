# MatrixKoder Plugin API Feasibility Spikes

P0 research on Claude Code mod API capabilities for MatrixKoder plugin development.

## Q1: Multi-module plugin structure (skills + agents + hooks)

**Answer: YES, partial** — One plugin can carry `skills/<name>/SKILL.md`, `agents/*.md`, AND `hooks/hooks.json` in same folder. Validated with `claude plugin validate plugin-test-q1`; all three module types pass validation together, though warnings appear (frontmatter format in SKILL.md / agent.md, hooks config not required in plugin.json when using hooks/hooks.json). Skills and agents discovered auto-magically; hooks require explicit module reference via `plugin.json` `hooks.modules` or `hooks/hooks.json`. Shipped in one folder.

## Q2: Pane docking & lifecycle

**Answer: YES** — Pane lifecycle and properties:
- Open/close: `$.ui.open({ id, title?, focus?, closeOnEscape?, holdToasts?, rows?, columns? })` → `Promise<UiOpenResult>` (isPlaced: bool)
- Close: `$.ui.close({ id })` 
- `e.props.bodyColumns` (number): width available to pane content (excludes engine chrome)
- `e.viewport.isFullscreen` (bool, undefined on surfaces w/o docking): whether pane can dock
- Auto-open rule: ≥144 cols unasked, ≥110 cols if user opened once
- Redraw timer: `$.ui.invalidate("ui.render")` (capped 10/sec; 30/sec for terminal pane/band)
- State via `$.state.get/set` (persists across reloads); `atom()` / `derive()` / `read()` for reactivity

## Q3: Text/Box styling props

**Answer: YES, comprehensive** —
- **Color**: `ThemeKey` (named: text, inverseText, inactive, subtle, suggestion, remember, success, error, warning, merged, claude, permission, planMode, autoAccept, promptBorder, bashBorder, ide, diffAdded/Removed, diffAddedWord, etc.) or `string & {}` (any custom/hex)
- **Text**: bold, italic, underline, strikethrough, inverse, dimColor (bool); wrap ('wrap'|'end'|'middle'|'truncate'|'truncate-start/middle/end')
- **Box**: borderStyle (string: 'single'|'double'|'round'|'bold'|'singleDouble'|'doubleSingle'|'classic'|'arrow'|'dashed'|'quote'), borderColor (Color), borderDimColor, backgroundColor, flexDirection, gap, padding, margin, width/height, display, overflow, position
- No ProgressBar, Spinner, Badge, or Divider elements; use Box + Text for custom indicators

## Q4: File & process I/O

**Answer: YES, with sandbox limits** —
- **Files**: `$.fs.read(path, {as?: 'bytes'|'text'})` (returns text or {base64}); `$.fs.stat(path, {resolve?})` (returns type, size, realPath if resolve); `$.fs.watch/write/list/ancestors` also available
- **Process**: `$.process.run(argv, {cwd?, env?, input?, timeoutMs?})` → {exitCode, stdout, stderr, isStdoutTruncated?, isStderrTruncated?}; `$.process.spawn()` streams output chunk-by-chunk
- **Limits**: 4 MiB cap on reads/writes; output capped at 4194304 bytes per stream (truncation flags alert)
- **Sandbox**: Relative paths under session cwd; absolute paths used as-is. fs.* hooks can gate; realPath guards against symlink escapes
- Can read `<repo>/.matrixkoder/runs/*.json` and `~/.matrixkoder/usage.json` via relative/absolute paths

## Q5: Timers & lifecycle

**Answer: YES, but reload-specific** —
- **Timer API**: `$.clock.after(ms, fn)` (one-shot); `$.clock.every(ms, fn)` (repeating, min 1ms)
- **Returns**: a cancel function `() => void` (types line 1575; verified by orchestrator)
- **Survival**: Timers are **cancelled on hot reload** of plugin code. Environment kept but pending timers dropped; restart in `session.start` hook if needed
- **Budget**: Timer callbacks run outside dispatch budget (don't consume hook timeout)

## Q6: Model wakeup & prompt composition

**Answer: YES; two modes** —
- **Wakeup**: `$.prompt.submit({text, context?})` queues prompt that starts own turn once session idle (never folds into running turn). Call resolves when turn starts, not when completes. Doc: "queued and run once the session is idle"
- **No direct "wake model" call** — use `$.prompt.submit` or `$.agent.spawn` to queue work
- **Prompt composition**: `$.prompt.compose()` → `{sections: [{id, text, scope: 'shared'|'session'}]}`. Scope determines cache boundary (shared = cross-org cache candidate). Hook via `on("prompt.compose", ...)` to inject/modify sections

## Q7: Deny Bash/tool call

**Answer: YES, simple shape** —
- **Hook handler**: `on("tool.call", ($, e, next) => e.tool === "Bash" ? { deny: "reason text" } : next(e))`
- **Deny shape**: `{ deny: string }` (the string is the error message model sees)
- **Read command**: `e.tool` (string) discriminates; after `if (e.tool === "Bash")` `e.command` is the shell string
- **Syntax**: Hook answers without calling `next(e)` to deny; call `next(e)` to allow

## Q8: Tool & agent registration

**Answer: YES, two registration paths** —
- **`$.tool.register(spec)`**: `spec = {name, description, inputSchema?}` → listed as `mcp__<plugin>__<name>`; hook `on("tool.call", {tool: 'mcp__<plugin>__<name>'}, ...)` to handle
- **`$.agent.register(spec)`**: `spec = {name, description, prompt, tools?, model?, ...agent-def-fields}` → listed as `<plugin>:<name>`; **also** agents/*.md files in plugin folder are auto-discovered and registered (no explicit call needed)
- **File-based agents**: `agents/*.md` files with YAML frontmatter auto-register; don't need $.agent.register call
- Both resolve to registration confirmation or deny on hook refusal

## Q9: Status line & toast

**Answer: YES, minimal** —
- **Toast**: `$.ui.toast(text, {timeoutMs?: 4000})` (text ≤2000 chars on terminal, 10000 remote) — small box stacked over transcript
- **Status**: `$.ui.status(text | undefined)` (text ≤2000 chars, 10000 remote; undefined to clear) — pinned one-liner under prompt
- **Color support**: Toast/status text is plain string; inline styling not available (use ui.render for styled text)

## Q10: Plugin config (userConfig & options)

**Answer: YES, schema in plugin.json** —
- **Definition**: `plugin.json` → `userConfig: { fieldName: {description?, type: 'string'|'boolean'|'number', default?, options?: [values for picker]} }`
- **Access**: Hook receives `options` parameter: `export function register(on, options) { const val = options.fieldName; }`
- **Persistent**: Stored in user settings under `pluginConfigs.<plugin>.<field>`
- **UI**: Each field appears as row in `/config` menu; `options` array makes it a picker dropdown

## Q11: Testing pattern (*.test.ts)

**Answer: YES, minimal runnable pattern** —
- **Import**: `import { test, expect, mock } from 'claude-code/testing'`
- **Structure**: 
  ```ts
  test('description', async ($, on) => {
    const now = await $.clock.now();
    expect(now).toBeGreaterThan(0);
  });
  ```
- **Hook registration**: Register hooks within the test body via `on()` before any `$` calls (they sit beneath the plugin)
- **Async**: Test body is async; `await` all `$` calls
- **UI tests**: Mount via test's `ui` noun: `ui.mount({plugin, surface, component, props})`, act with `press()`, `input()`, `select()`
- **Matchers**: `expect(val).toBe()`, `.toEqual()`, `.toContain()`, `.toThrow()`, `.resolves`/`.rejects` (like Jest)
- **Clock mocking**: `mock.clock` available; `advance(ms)` moves time through pending timers
- **Run**: `claude plugin test <dir>` finds and runs `*.test.ts` files

---

## Summary

**Multi-module structure:** ✓ Possible (plugins ship all three types together).
**Pane API:** ✓ Open/close/state management available; auto-open at ≥144 cols.
**Styling:** ✓ Text (bold, italic, underline, inverse, dimColor, wrap), Box (border, flex, padding, colors); no built-in Badge/Spinner.
**I/O:** ✓ File read/stat, process run/spawn; 4 MiB limits; sandbox under cwd.
**Timers:** ✓ `after()`/`every()`; cancelled on reload; restart in session.start if needed.
**Model wakeup:** ✓ `$.prompt.submit()` queues own turn; `prompt.compose()` hooks system prompt.
**Tool deny:** ✓ `{deny: string}` shape; hook before `next()` call.
**Registration:** ✓ Tools via `$.tool.register()`; agents via `$.agent.register()` or `agents/*.md` auto-discovery.
**UI feedback:** ✓ `$.ui.toast()` and `$.ui.status()` for simple notifications; no color support (use render).
**Config:** ✓ `userConfig` in plugin.json; picked in `/config` menu; passed to `register(on, options)`.
**Testing:** ✓ `test()` + `expect()` + `mock.clock`; run with `claude plugin test <dir>`.
