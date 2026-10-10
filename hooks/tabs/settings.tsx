// The /mk pane's Settings tab (plan settings-tab S2.2, contracts C1/C2/C12):
// eight scopes on digit hotkeys — 1 Mode · 2 Project · 3 Routing · 4 Agents ·
// 5 Models · 6 Child · 7 Tabs · 8 About — each with its file + scope-note
// line, the artboard's rows drawn through the S2.1 kit (SettingRow), the find
// filter, the dirty footer and the per-scope danger reset. The tab is pure
// render (C10): no $, no state, no scripts — every change leaves through the
// C12 view-state handle (`ui`), written only inside onPress/onInput/onSubmit
// handlers, under `settings:` keys:
//   settings:scope / settings:sel / settings:open / settings:find /
//   settings:findfocus / settings:about:checked / settings:edit:<key> (JSON).
// Departures the plan binds over the artboard: no Hybrid mode (Solo /
// Orchestrator / Hive only, Hive shows the project checklist); Plugin +
// Skills + Update are ONE read-only About scope; Models rows are sorted by
// name with pick-order numbers only (no ranks or scores); `/` is never a
// hotkey — the reserved letters are s save · x discard · r reset · f find
// (+ c check on About) beside the scope digits 1-8.
import type { Elements, RenderElement } from 'claude-code'
import { SETTINGS_FIXTURE } from './settings.fixture'
import { ActionButton, Card, Chip, Segment, SettingRow } from './kit'
import type { SettingCtx, SettingField } from './kit'
import { PALETTE, ROLE } from './palette'
import type { SavePayload } from '../settings-io'
import { settingsInputKeys } from '../settings-io'

type Els = Elements[keyof Elements]

/** C12: this tab's view-state handle — declared locally (structural typing; no
 *  import crosses lanes). `save`/`recheck` are the dashboard's live wires (S3):
 *  save carries the dirty payload to the ordered mk-config + $.config write,
 *  recheck re-runs the latest-tag check; both absent in fixture/tests. */
type TabUi = {
  get(key: string): string | undefined
  set(key: string, value: string): void
  save?: (payload: SavePayload) => void
  recheck?: () => void
}

/** The eight scopes, in hotkey order; `file`/`note` draw the scope-note line. */
export type SettingsScopeId = 'mode' | 'project' | 'routing' | 'agents' | 'models' | 'child' | 'tabs' | 'about'

export type SettingsScope = {
  readonly id: SettingsScopeId
  readonly label: string
  readonly file: string
  readonly note: string
}

/** The value shapes a settings row can hold — C1's `list --json` vocabulary
 *  plus the readonly-string[] a `list` row carries. */
export type SettingsValue = string | number | boolean | readonly string[]

/** One settings row, shaped exactly like contract C1's `list --json` fields
 *  ({key, scope, label, type, value, default, min, max, unit, opts, help,
 *  editable, why}) — the C2 userConfig rows (mode, defaultTab, hiddenTabs,
 *  raster) use the same shape with scope 'mode'/'tabs'. A `list` row renders
 *  as a text row (comma-joined value) — the kit has no list variant, the tab
 *  converts. `opts` is C1's tuple pair [value, note]. */
export type SettingsField = {
  readonly key: string
  readonly scope: SettingsScopeId
  readonly label: string
  readonly type: 'bool' | 'num' | 'seg' | 'select' | 'text' | 'list'
  readonly value: SettingsValue
  readonly default: SettingsValue
  readonly min?: number
  readonly max?: number
  readonly unit?: string
  readonly opts?: readonly (readonly [string, string])[]
  readonly help: string
  readonly editable: boolean
  readonly why?: string
  /** S3: a live global row (its value came from $.config, keyed by suffix) —
   *  the save payload routes it to $.config.set, not to mk-config set. */
  readonly global?: true
}

/** One card of rows under a scope (the artboard's groups). */
export type SettingsGroup = {
  readonly scope: SettingsScopeId
  readonly title: string
  readonly right?: string
  readonly rows: readonly SettingsField[]
}

/** An operating-mode card (Mode scope; no Hybrid — the plan's departure). */
export type SettingsMode = {
  readonly id: 'solo' | 'orchestrator' | 'hive'
  readonly name: string
  readonly scope: string
  readonly desc: string
}

/** One Hive project checklist row. */
export type SettingsProject = { readonly name: string; readonly since: string; readonly on: boolean }

/** One catalogue model (Models scope): its name and its dim cost note — no
 *  rank, no score (the plan's departure). */
export type SettingsModelRow = { readonly name: string; readonly note: string }

/** One agent's Models card: the catalogue the picks are made from. */
export type SettingsAgentCat = {
  readonly id: string
  readonly name: string
  readonly provider: string
  readonly off: boolean
  readonly models: readonly SettingsModelRow[]
}

/** The default (fixture) ordered picks per agent, max 4. */
export type SettingsPicks = readonly { readonly agent: string; readonly models: readonly string[]; /** false: the yaml has no picks row for this agent (read-only) */ readonly editable?: boolean }[]

/** The About scope's static data (read-only: installed/latest version, the
 *  What's-new lines). */
export type SettingsAbout = {
  readonly installed: string
  readonly latest: string
  readonly latestNote: string
  readonly commits: number
  readonly whatsNew: readonly { readonly kind: 'feat' | 'fix'; readonly line: string }[]
}

/** The tab's whole view (C1): the fixture satisfies it today, the live
 *  mk-config answer later (S3) — the render never knows which. */
export type SettingsView = {
  readonly mock: boolean
  readonly scopes: readonly SettingsScope[]
  readonly groups: readonly SettingsGroup[]
  readonly modes: readonly SettingsMode[]
  readonly projects: readonly SettingsProject[]
  readonly agents: readonly SettingsAgentCat[]
  readonly picks: SettingsPicks
  readonly about: SettingsAbout
}

/** S2.2: the fixture-backed view, mock flag on (C1/C6). */
export function settingsView(): SettingsView {
  return { ...SETTINGS_FIXTURE, mock: true }
}

// ---------- view-state readers/writers (all writes live in press closures)

/** A scalar edit's parsed value; absent (`''`/undefined/unparseable) = none. */
function editOf(ui: TabUi | undefined, key: string): string | number | boolean | undefined {
  const raw = ui?.get(`settings:edit:${key}`)
  if (raw == null || raw === '') return undefined
  try {
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed === 'string' || typeof parsed === 'number' || typeof parsed === 'boolean') return parsed
    return undefined
  } catch {
    return undefined
  }
}

/** A models-pick edit (a JSON string array); absent = the fixture picks. */
function pickEditOf(ui: TabUi | undefined, agent: string): readonly string[] | undefined {
  const raw = ui?.get(`settings:edit:models.${agent}`)
  if (raw == null || raw === '') return undefined
  try {
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : undefined
  } catch {
    return undefined
  }
}

/** A Hive-checklist edit (a JSON bool); absent = the fixture state. */
function hiveEditOf(ui: TabUi | undefined, name: string): boolean | undefined {
  const raw = ui?.get(`settings:edit:hive.${name}`)
  if (raw == null || raw === '') return undefined
  try {
    const parsed: unknown = JSON.parse(raw)
    return typeof parsed === 'boolean' ? parsed : undefined
  } catch {
    return undefined
  }
}

/** `…`-truncate to `w` cells (plan.tsx's clip — the row shows where it yielded). */
function clip(text: string, w: number): string {
  if (text.length <= w) return text
  return `${text.slice(0, Math.max(1, w - 1))}…`
}

/** a C1 value as the row draws it: a list joins with ', ', scalars stay. */
function toText(v: SettingsValue): string | number | boolean {
  if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return v
  return v.join(', ')
}

/** one edit write — the only way a control changes anything (JSON, so S3 can
 *  read the typed value back off the same key). */
function setEdit(ui: TabUi | undefined, key: string, value: string | number | boolean): void {
  ui?.set(`settings:edit:${key}`, JSON.stringify(value))
}

/** every row the view holds, flat (the C1 list). */
const flatFields = (view: SettingsView): readonly SettingsField[] => view.groups.flatMap(g => g.rows)

/** a field's current value: its edit when one exists, else the fixture value. */
function currentValue(f: SettingsField, ui: TabUi | undefined): string | number | boolean {
  const e = editOf(ui, f.key)
  return e ?? toText(f.value)
}

/** an edit differs from the loaded value → the row is dirty (`●`, unsaved).
 *  The default plays no part here (R3 MEDIUM 1): a row whose loaded value
 *  already differs from its default is clean until edited, and editing back
 *  to the loaded value clears the mark. */
function fieldDirty(f: SettingsField, ui: TabUi | undefined): boolean {
  const e = editOf(ui, f.key)
  return e !== undefined && String(e) !== String(toText(f.value))
}

/** the dirty keys in draw order: field keys, then models.<agent>, then
 *  hive:<project> — the footer's `<keys>`. */
function dirtyKeys(view: SettingsView, ui: TabUi | undefined): string[] {
  const out: string[] = []
  for (const f of flatFields(view)) if (fieldDirty(f, ui)) out.push(f.key)
  for (const p of view.picks) {
    // a read-only agent (no picks row in the yaml) never counts or saves: a stale edit is ignored
    const e = p.editable === false ? undefined : pickEditOf(ui, p.agent)
    if (e != null && e.join('|') !== p.models.join('|')) out.push(`models.${p.agent}`)
  }
  for (const pr of view.projects) {
    const e = hiveEditOf(ui, pr.name)
    if (e !== undefined && e !== pr.on) out.push(`hive:${pr.name}`)
  }
  return out
}

/** save/discard in the fixture: both just clear every edit (S3 wires writes). */
// shortcut: fixture save clears edits, S3 wires real writes
function clearAllEdits(view: SettingsView, ui: TabUi | undefined): void {
  for (const f of flatFields(view)) ui?.set(`settings:edit:${f.key}`, '')
  for (const p of view.picks) ui?.set(`settings:edit:models.${p.agent}`, '')
  for (const pr of view.projects) ui?.set(`settings:edit:hive.${pr.name}`, '')
}

/** S3: the save press payload — every dirty row routed by its marker. A
 *  `global` row goes to $.config.set by suffix; a tabs.* bool rewrites the
 *  hiddenTabs comma list; a models.<agent> pick edit goes to `set
 *  picks.<agent>` as an array; everything else goes to one mk-config `set`
 *  per key (list rows encode their comma string as an array there). */
function savePayloadOf(view: SettingsView, ui: TabUi | undefined): SavePayload {
  const sets: { key: string; value: SettingsValue; list: boolean }[] = []
  const globals: { suffix: string; value: SettingsValue }[] = []
  const visibility: { tab: string; on: boolean }[] = []
  for (const f of flatFields(view)) {
    const e = editOf(ui, f.key)
    if (e === undefined) continue
    if (f.global === true) {
      globals.push({ suffix: f.key, value: e })
    } else if (f.scope === 'tabs' && f.key.startsWith('tabs.') && typeof e === 'boolean') {
      visibility.push({ tab: f.key.slice('tabs.'.length), on: e })
    } else {
      sets.push({ key: f.key, value: e, list: f.type === 'list' })
    }
  }
  for (const p of view.picks) {
    // a read-only agent (no picks row in the yaml) never counts or saves: a stale edit is ignored
    const e = p.editable === false ? undefined : pickEditOf(ui, p.agent)
    if (e != null && e.join('|') !== p.models.join('|')) {
      sets.push({ key: `picks.${p.agent}`, value: [...e], list: true })
    }
  }
  return { sets, globals, visibility }
}

/** Carried MEDIUM: the tab's letter/digit hotkeys (`s x r f c`, scope 1–8)
 *  are inert while the find field or a text-row Input really has the
 *  engine's focus ring — read off `settings:focus`, which the dashboard's
 *  ui.focus hook keeps (element key, '' when the ring leaves). A focused
 *  Button keeps its hotkey: only an Input swallows printable keys. */
function inputHot(view: SettingsView, ui: TabUi | undefined): boolean {
  const key = ui?.get('settings:focus') ?? ''
  return key !== '' && settingsInputKeys(view).has(key)
}

/** the danger reset: clears one scope's edits (never drawn on Mode/About). */
function clearScopeEdits(view: SettingsView, ui: TabUi | undefined, scopeId: SettingsScopeId): void {
  for (const f of flatFields(view)) if (f.scope === scopeId) ui?.set(`settings:edit:${f.key}`, '')
  if (scopeId === 'models') for (const p of view.picks) ui?.set(`settings:edit:models.${p.agent}`, '')
}

/** the current mode id: the `mode` field's edit, else its fixture value. */
function currentMode(view: SettingsView, ui: TabUi | undefined): string {
  const f = flatFields(view).find(x => x.key === 'mode')
  if (f == null) return 'orchestrator'
  return String(currentValue(f, ui))
}

/** an agent's picks: its edit, else the fixture list. */
function currentPicks(view: SettingsView, ui: TabUi | undefined, agent: string): readonly string[] {
  return pickEditOf(ui, agent) ?? view.picks.find(p => p.agent === agent)?.models ?? []
}

/** the artboard's short pick name: the last path segment with the noise
 *  suffixes stripped (`opencode/step-5-preview-free` → `step-5`). */
function shortModel(name: string): string {
  return name.split('/').pop()?.replace(/-preview|-free|claude-/g, '') ?? name
}

/** a row passes the find filter: its label + key contain the query. */
const rowMatches = (f: SettingsField, q: string): boolean =>
  q === '' || `${f.label} ${f.key}`.toLowerCase().includes(q)

/** the find filter's `N matches` count over the scope's rows (models count
 *  model names; Mode draws cards, not rows, so nothing matches there). */
function countMatches(view: SettingsView, scopeId: SettingsScopeId, q: string): number {
  if (q === '') return 0
  if (scopeId === 'mode') return 0
  if (scopeId === 'models') {
    return view.agents.reduce((n, a) => n + a.models.filter(m => m.name.toLowerCase().includes(q)).length, 0)
  }
  return view.groups
    .filter(g => g.scope === scopeId)
    .reduce((n, g) => n + g.rows.filter(r => rowMatches(r, q)).length, 0)
}

/** a seg option's display word: its note when one exists (the artboard's
 *  short button words — `spare`, `bal`, `qual`), else the value itself. The
 *  kit draws the opt's value as the button word, so the tab feeds it the
 *  short word and translates back on choose (the edit keeps the real value). */
const SEG_SHORT: Record<string, string> = { 'claude-sparing': 'spare', balanced: 'bal', quality: 'qual', 'local-main': 'local', integration: 'integ' }
const segWord = (o: readonly [string, string]): string => SEG_SHORT[o[0]] ?? (o[1] !== '' && o[1].length <= 8 ? o[1] : o[0])

/** pre-shrink a seg's display words so the whole strip fits a CHANGED row at
 *  `W` (marker + one-cell label + two gaps + strip ≤ W). The kit shrinks on
 *  its own looser bound and clamps the label to one cell, which can overshoot
 *  by a column; feeding it words that already fit keeps every row inside W. */
function shrinkWords(words: readonly string[], maxSegW: number): string[] {
  const w = [...words]
  const segW = (): number => w.reduce((n, x) => n + x.length + 6, 0) + Math.max(0, w.length - 1)
  while (segW() > maxSegW && w.some(x => x.length > 1)) {
    let longest = 0
    for (let i = 1; i < w.length; i++) {
      if ((w[i] ?? '').length > (w[longest] ?? '').length) longest = i
    }
    w[longest] = (w[longest] ?? '').slice(0, -1)
  }
  return w
}

/** a C1 field as the kit's SettingField: list → text (comma-joined), the opts
 *  tuples → the kit's {value, note} (a seg's options carry their short display
 *  words — see segWord — pre-shrunk to the width; a select's keep the real
 *  values), the help tail's default clipped to what the line has room for,
 *  and a % unit draws the kit's bar. */
function kitField(f: SettingsField, value: string | number | boolean, W: number): SettingField {
  let cur = value
  let opts: { readonly value: string; readonly note: string }[] | undefined
  if (f.opts != null) {
    if (f.type === 'seg') {
      const words = shrinkWords(f.opts.map(segWord), W - 4)
      opts = f.opts.map(([v, n], i) => ({ value: words[i] ?? segWord([v, n]), note: n }))
      const idx = f.opts.findIndex(([real]) => real === String(value))
      if (idx >= 0) cur = words[idx] ?? String(value)
    } else {
      opts = f.opts.map(([v, n]) => ({ value: v, note: n }))
    }
  }
  return {
    key: f.key,
    label: f.label,
    type: f.type === 'list' ? 'text' : f.type,
    value: cur,
    default: clip(String(toText(f.default)), Math.max(1, W - 24)),
    min: f.min,
    max: f.max,
    unit: f.unit,
    pct: f.unit === '%' ? true : undefined,
    opts,
    help: f.help,
    editable: f.editable,
    why: f.why,
  }
}

/** one row's SettingCtx: the view state read at render, the writes fired only
 *  from the kit's controls. The artboard's stepper rule: step 5 past a max of
 *  60, else 1. Choosing an option also closes the open list. `noHk` drops the
 *  row's letter hotkey (the `r` reset) while an Input holds the real focus or
 *  the find field holds the edit mode. */
function rowCtx(f: SettingsField, W: number, ui: TabUi | undefined, noHk: true | undefined): SettingCtx {
  const cur = currentValue(f, ui)
  const n = Number(cur)
  const step = (f.max ?? 60) > 60 ? 5 : 1
  const selected = ui?.get('settings:sel') === f.key
  const open = ui?.get('settings:open') === f.key
  return {
    cols: W,
    selected,
    changed: fieldDirty(f, ui),
    open,
    // Carried MEDIUM (+ R3 MEDIUM 3): while the find field holds the edit
    // mode, or a text-row Input really has the engine's focus ring (the
    // engine holds every printable key in a focused Input — InputProps), no
    // letter/digit hotkey on the tab may fire, so the harness and every
    // surface agree.
    noHotkey: noHk,
    onPick: () => ui?.set('settings:sel', selected ? '' : f.key),
    onToggle: () => setEdit(ui, f.key, !(cur === true)),
    onInc: () => setEdit(ui, f.key, Math.min(f.max ?? Number.MAX_SAFE_INTEGER, Number.isFinite(n) ? n + step : Number(f.default))),
    onDec: () => setEdit(ui, f.key, Math.max(f.min ?? Number.MIN_SAFE_INTEGER, Number.isFinite(n) ? n - step : Number(f.default))),
    onChoose: v => {
      // a seg press carries the display word; the edit stores the real value
      const real = f.type === 'seg' ? (f.opts?.find(o => segWord(o) === v)?.[0] ?? v) : v
      setEdit(ui, f.key, real)
      ui?.set('settings:open', '')
    },
    onToggleOpen: () => ui?.set('settings:open', open ? '' : f.key),
    onEdit: v => setEdit(ui, f.key, v),
    // R3 LOW 4: resetting to a default that already equals the loaded value
    // stores no edit (nothing is unsaved); otherwise the default is the edit.
    onReset: () => {
      if (String(toText(f.default)) === String(toText(f.value))) ui?.set(`settings:edit:${f.key}`, '')
      else setEdit(ui, f.key, toText(f.default))
    },
  }
}

// ---------- the render

/** a card header (Settings.dc.html `.h`): bold upper-case in the muted ink, the
 *  note dim on the right. */
function SecHead(els: Els, opts: { label: string; right?: string }): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="row" justifyContent="space-between">
      <Text bold={true} color={PALETTE.mut}>{opts.label.toUpperCase()}</Text>
      {opts.right != null && <Text dimColor={true}>{opts.right}</Text>}
    </Box>
  )
}

/** Pure render: elements only, no $, no state (C1); `ui` is the C12 view state. */
export function SettingsTab(els: Els, view: SettingsView, cols: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const wanted = ui?.get('settings:scope')
  const scopeId = view.scopes.find(s => s.id === wanted)?.id ?? view.scopes[0]?.id ?? 'mode'
  const scope = view.scopes.find(s => s.id === scopeId) ?? view.scopes[0]
  if (scope == null) return <Box flexDirection="column" />
  const W = Math.max(1, cols - 4) // a Card's content width (border + padding)
  const filter = ui?.get('settings:find') ?? ''
  const q = filter.trim().toLowerCase()
  const findFocus = ui?.get('settings:findfocus') === '1'
  // Carried MEDIUM: the view flag OR the real ring — either drops s/x/r/f/c
  // and the scope digits (the engine already feeds printable keys to a
  // focused Input alone; the tab's own hotkeys stand down too).
  const noHk = findFocus || inputHot(view, ui) ? true : undefined
  const dirties = dirtyKeys(view, ui)
  const checked = ui?.get('settings:about:checked') === '1'
  const loadError = ui?.get('settings:load:error') ?? ''
  const saveMsg = ui?.get('settings:save:msg') ?? ''

  // the find field: the native Input (S2.0: on terminal + desktop), else the
  // dim fallback a surface with no fields draws
  let findField: RenderElement
  if ('Input' in els) {
    const { Input } = els
    findField = (
      <Input
        key="set-find-input" placeholder="find a setting" value={filter}
        onInput={v => { ui?.set('settings:find', v) }} onSubmit={v => { ui?.set('settings:find', v) }}
      />
    )
  } else {
    findField = <Text dimColor={true}>{clip(filter === '' ? 'type to filter' : filter, Math.max(1, cols - 14))}</Text>
  }

  return (
    <Box flexDirection="column">
      {view.mock && <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>}
      {ScopeBar(els, view, cols, scopeId, ui, noHk)}
      <Box flexDirection="row" gap={1} backgroundColor={PALETTE.field} paddingX={1} marginTop={1} marginBottom={1}>
        <Text dimColor={true}>/</Text>
        {findField}
        {q !== '' && (
          <Text dimColor={!findFocus} color={findFocus ? ROLE.live.fg : undefined}>
            {`${countMatches(view, scopeId, q)} match${countMatches(view, scopeId, q) === 1 ? '' : 'es'}`}
          </Text>
        )}
      </Box>
      <Text dimColor={true} wrap="truncate-end">{clip(`${scope.file} · ${scope.note}`, cols)}</Text>
      {loadError !== '' && <Text color={ROLE.err.fg}>{clip(`⚠ ${loadError}`, cols)}</Text>}
      {scopeId === 'mode'
        ? ModeCards(els, view, W, ui)
        : scopeId === 'models'
          ? ModelsCards(els, view, W, ui, q)
          : scopeId === 'about'
            ? AboutBody(els, view, W, ui, q, checked, noHk)
            : GroupCards(els, view, scopeId, W, ui, q, noHk)}
      {scopeId !== 'mode' && scopeId !== 'about' && (
        DangerRow(els, scope.label, cols, () => { clearScopeEdits(view, ui, scopeId) })
      )}
      {saveMsg !== '' && SaveMsg(els, saveMsg, cols)}
      {dirties.length > 0 && DirtyFooter(els, dirties, cols, noHk, () => {
        // S3: the save press runs the ordered write (yaml sets, then globals);
        // without the live wire (fixture/tests) it keeps clearing the edits.
        // Either way the footer unmounts with a stale ring key on it.
        ui?.set('settings:focus', '')
        if (ui?.save != null) ui.save(savePayloadOf(view, ui))
        else clearAllEdits(view, ui)
      }, () => { ui?.set('settings:focus', ''); clearAllEdits(view, ui) })}
      <Box flexDirection="row" gap={1}>
        <Text dimColor={true}>space toggle · ←→ value ·</Text>
        {ActionButton(els, {
          key: 'set-find', glyph: 'find', word: '', tone: 'quiet', plain: true, hotkey: noHk ? undefined : 'f',
          // closing or opening the find field drops a stale ring key with it:
          // the hook re-records the ring on the next move
          onPress: () => { ui?.set('settings:findfocus', findFocus ? '' : '1'); ui?.set('settings:focus', '') },
        })}
      </Box>
    </Box>
  )
}

/** The scope bar: one pressable per scope, its digit the hotkey (1-8 are legal
 *  engine hotkeys), the active scope drawn primary. The bar wraps as whole
 *  buttons — a row never exceeds `cols` (4 scopes per row at 46, 3 at 36). */
function ScopeBar(els: Els, view: SettingsView, cols: number, activeId: SettingsScopeId, ui: TabUi | undefined, noHk: true | undefined): RenderElement {
  const { Box } = els
  // R3 MEDIUM 3 + carried MEDIUM: the scope digits must not fire while the
  // find field holds the edit mode or a text Input holds the real ring
  // (typing `1` filters; it must never switch scope).
  type Item = { s: SettingsScope; digit: string; w: number }
  const rows: Item[][] = []
  let cur: Item[] = []
  let curW = 0
  view.scopes.forEach((s, i) => {
    const digit = String(i + 1)
    const w = `${digit}: ${s.label}`.length + 4 // a Segment: padding + the plain press (the engine draws `d: ` before a hotkeyed label)
    if (cur.length > 0 && curW + w > cols) {
      rows.push(cur)
      cur = []
      curW = 0
    }
    curW += w
    cur.push({ s, digit, w })
  })
  if (cur.length > 0) rows.push(cur)
  return (
    <Box flexDirection="column">
      {rows.map((row, ri) => (
        <Box key={`set-scopebar-${String(ri)}`} flexDirection="row">
          {row.map(({ s, digit }) => Segment(els, {
            key: `set-scope-${s.id}`,
            label: s.label,
            on: s.id === activeId,
            flat: true,
            static: true,
            hotkey: noHk ? undefined : digit,
            onPress: () => {
              ui?.set('settings:scope', s.id)
              ui?.set('settings:sel', '')
              ui?.set('settings:open', '')
              ui?.set('settings:find', '')
              ui?.set('settings:findfocus', '')
              ui?.set('settings:focus', '') // the scope's Inputs unmount with it
            },
          }))}
        </Box>
      ))}
    </Box>
  )
}

/** The row scopes' cards: one kit Card per group (the artboard's LANES /
 * RESERVE FLOOR / … headers), the group's rows filtered by the find query. */
function GroupCards(els: Els, view: SettingsView, scopeId: SettingsScopeId, W: number, ui: TabUi | undefined, q: string, noHk: true | undefined): RenderElement {
  const { Box } = els
  const groups = view.groups
    .filter(g => g.scope === scopeId)
    .map(g => ({ g, rows: (g.title === 'builders' ? [...g.rows].sort((a, b) => Number(currentValue(b, ui) === true) - Number(currentValue(a, ui) === true)) : g.rows).filter(r => rowMatches(r, q)) }))
    .filter(({ rows }) => rows.length > 0)
  return (
    <Box flexDirection="column">
      {groups.map(({ g, rows }) => Card(els, {}, (
        <Box key={`set-group-${g.title}`} flexDirection="column" width={W}>
          {SecHead(els, { label: g.title, right: g.right })}
          {rows.map(r => SettingRow(els, kitField(r, currentValue(r, ui), W), rowCtx(r, W, ui, noHk)))}
        </Box>
      )))}
    </Box>
  )
}

/** The Mode scope: the three mode cards (no Hybrid), and when Hive holds the
 *  mode, the project checklist card (the plan's departures). */
function ModeCards(els: Els, view: SettingsView, W: number, ui: TabUi | undefined): RenderElement {
  const { Box, Text } = els
  const mode = currentMode(view, ui)
  return (
    <Box flexDirection="column">
      {Card(els, {}, (
        <Box flexDirection="column" width={W}>
          {SecHead(els, { label: 'operating mode', right: 'radio' })}
          {view.modes.map(m => {
            const cur = m.id === mode
            return (
              <Box key={`set-modecard-${m.id}`} flexDirection="column" borderStyle="round" borderColor={cur ? ROLE.live.fg : PALETTE.line} backgroundColor={cur ? PALETTE.modeOn : PALETTE.bg} paddingX={1}>
                <Box flexDirection="row" justifyContent="space-between" gap={1}>
                  <Box flexDirection="row" gap={1}>
                    <Text color={ROLE.live.fg}>{cur ? '◉' : '○'}</Text>
                    {ActionButton(els, {
                      key: `set-mode-${m.id}`, glyph: m.name, word: '', plain: true,
                      onPress: () => { setEdit(ui, 'mode', m.id) },
                    })}
                  </Box>
                  <Text dimColor={true}>{m.scope}</Text>
                </Box>
                <Text dimColor={true}>{clip(m.desc, W - 2)}</Text>
              </Box>
            )
          })}
        </Box>
      ))}
      {mode === 'hive' && Card(els, { role: 'live' }, (
          <Box flexDirection="column" width={W}>
            {SecHead(els, { label: 'hive · projects', right: 'projects.json' })}
            {view.projects.map(p => {
              const on = hiveEditOf(ui, p.name) ?? p.on
              const since = `onboarded ${p.since}`
              const nameBudget = Math.max(1, W - 1 /* marker */ - since.length - 3 /* gaps */ - 2 /* plain chrome */)
              return (
                <Box key={`set-hiverow-${p.name}`} flexDirection="row" justifyContent="space-between" gap={1}>
                  <Box flexDirection="row" gap={1}>
                    <Text color={on ? ROLE.ok.fg : undefined} dimColor={!on}>{on ? '☑' : '☐'}</Text>
                    {ActionButton(els, {
                      key: `set-hive-${p.name}`, glyph: clip(p.name, nameBudget), word: '', plain: true,
                      onPress: () => { setEdit(ui, `hive.${p.name}`, !on) },
                    })}
                  </Box>
                  <Text dimColor={true}>{since}</Text>
                </Box>
              )
            })}
          </Box>
      ))}
    </Box>
  )
}

/** The Models scope: one card per agent — the n/4 count chip, the catalogue
 *  sorted by name, picked models numbered by pick order, a 5th pick refused
 *  (the press is a no-op on a full card). No ranks, no scores. */
function ModelsCards(els: Els, view: SettingsView, W: number, ui: TabUi | undefined, q: string): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      {view.agents.map(cat => {
        const picks = currentPicks(view, ui, cat.id)
        const full = picks.length >= 4
        const readOnly = view.picks.find(p => p.agent === cat.id)?.editable === false
        // off in the catalogue, or switched off in Agents → not routed
        const availField = flatFields(view).find(f => f.key === `agents.${cat.id}.available`)
        const avail = availField == null ? true : currentValue(availField, ui) !== false
        const off = cat.off || !avail
        const rows = cat.models
          .filter(m => q === '' || m.name.toLowerCase().includes(q))
          .slice()
          .sort((a, b) => a.name.localeCompare(b.name))
        const chip = Chip(els, { label: `${picks.length}/4`, role: full ? 'warn' : picks.length > 0 ? 'live' : 'idle' })
        const provBudget = Math.max(1, W - cat.name.length - `${picks.length}/4`.length - 2 - 4)
        const orderText = picks.length > 0 ? picks.map(shortModel).join(' → ') : 'none picked'
        return Card(els, { role: off ? undefined : picks.length > 0 ? 'live' : 'warn' }, (
          <Box key={`set-agent-${cat.id}`} flexDirection="column" width={W}>
            <Box flexDirection="row" justifyContent="space-between" gap={1}>
              <Box flexDirection="row" gap={1}>
                <Text bold={true} color="subtle">{cat.name}</Text>
                <Text dimColor={true}>{clip(cat.provider, provBudget)}</Text>
              </Box>
              {chip}
            </Box>
            {off && <Text dimColor={true}>{clip('agent off in Agents · not routed', W)}</Text>}
            {rows.map(m => {
              const idx = picks.indexOf(m.name)
              const on = idx >= 0
              const blocked = readOnly || (!on && full)
              const markerW = on ? 3 : 1 // the Chip pads one cell each side
              const noteW = m.note !== '' ? m.note.length + 1 : 0
              const nameBudget = Math.max(1, W - markerW - noteW - 2 /* gaps */ - 2 /* plain button */)
              return (
                <Box key={`set-modelrow-${cat.id}-${m.name}`} flexDirection="row" gap={1}>
                  {on
                    ? Chip(els, { label: String(idx + 1), role: 'live', solid: true })
                    : <Text dimColor={true}>{blocked ? '·' : '☐'}</Text>}
                  {ActionButton(els, {
                    key: `set-model-${cat.id}-${m.name}`,
                    glyph: clip(m.name, nameBudget), word: '', plain: true,
                    tone: blocked ? 'disabled' : undefined,
                    onPress: () => {
                      if (blocked) return // the 5th pick (or a read-only agent) is refused, edits unchanged
                      const next = on ? picks.filter(x => x !== m.name) : [...picks, m.name]
                      ui?.set(`settings:edit:models.${cat.id}`, JSON.stringify(next))
                    },
                  })}
                  {m.note !== '' && <Text dimColor={true}>{m.note}</Text>}
                </Box>
              )
            })}
            {readOnly && <Text dimColor={true}>{clip('read-only · no model list in matrixkoder.yaml', W)}</Text>}
            {full && <Text color={ROLE.warn.fg}>{clip('4 of 4 picked · untick one to swap', W)}</Text>}
            <Box flexDirection="row" justifyContent="space-between" gap={1}>
              <Text dimColor={true}>routing order = pick order</Text>
              <Text dimColor={true}>{clip(orderText, Math.max(1, W - 'routing order = pick order'.length - 1))}</Text>
            </Box>
          </Box>
        ))
      })}
    </Box>
  )
}

/** The About scope (the plan's one-read-only-scope merge of Plugin + Skills +
 * Update): the read-only plugin-option and helper-probe groups, the version
 * card with its view-state-only `c check`, and the What's-new lines. */
function AboutBody(els: Els, view: SettingsView, W: number, ui: TabUi | undefined, q: string, checked: boolean, noHk: true | undefined): RenderElement {
  const { Box, Text } = els
  const a = view.about
  return (
    <Box flexDirection="column">
      {GroupCards(els, view, 'about', W, ui, q, noHk)}
      {Card(els, { role: 'live' }, (
        <Box flexDirection="column" width={W}>
          {SecHead(els, { label: 'version', right: 'plugin.json · git tags' })}
          <Box flexDirection="row" justifyContent="space-between" gap={1}>
            <Text dimColor={true}>installed</Text>
            <Text>{`v${a.installed}`}</Text>
            {Chip(els, { label: 'local folder', role: 'idle' })}
          </Box>
          <Box flexDirection="row" justifyContent="space-between" gap={1}>
            <Text dimColor={true}>latest</Text>
            <Text>{`v${a.latest}`}</Text>
            <Text dimColor={true}>{checked ? 'checked now' : clip(a.latestNote, Math.max(1, W - 6 - 6 - 2))}</Text>
          </Box>
          <Box flexDirection="row" gap={1}>
            {ActionButton(els, {
              key: 'set-about-check', glyph: 'c', word: 'check',
              noHotkey: noHk,
              // S3.2: `c` re-runs the latest-tag check (the dashboard's wire);
              // the flag alone keeps the stale-note behaviour in tests.
              onPress: () => { ui?.set('settings:about:checked', '1'); ui?.recheck?.() },
            })}
          </Box>
        </Box>
      ))}
      {Card(els, {}, (
        <Box flexDirection="column" width={W}>
          {SecHead(els, { label: `what's new · v${a.latest}`, right: `${String(a.commits)} commits` })}
          {a.whatsNew.map(w => (
            <Box key={`set-whatsnew-${w.line}`} flexDirection="row" gap={1}>
              {Chip(els, { label: w.kind, role: w.kind === 'feat' ? 'ok' : 'live' })}
              <Text>{clip(w.line, W - 7)}</Text>
            </Box>
          ))}
        </Box>
      ))}
    </Box>
  )
}

/** The danger row: `⌫ reset <scope> to defaults` clears that scope's edits.
 *  Drawn on every scope except Mode and About; the dim `danger zone` label
 *  rides the button's row while it fits, else the line above it. */
function DangerRow(els: Els, label: string, cols: number, onReset: () => void): RenderElement {
  const { Box, Text } = els
  const word = `reset ${label} to defaults`
  const btn = (
    <Box backgroundColor={PALETTE.redTint} paddingX={1} flexDirection="row" gap={1}>
      <Text color={ROLE.err.fg}>⌫</Text>
      {ActionButton(els, { key: 'set-danger', glyph: word, word: '', plain: true, onPress: onReset })}
    </Box>
  )
  if ('danger zone'.length + 1 + word.length + 8 + 2 <= cols) {
    return (
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text dimColor={true}>danger zone</Text>
        {btn}
      </Box>
    )
  }
  return (
    <Box flexDirection="column">
      <Text dimColor={true}>danger zone</Text>
      {btn}
    </Box>
  )
}

/** The save-result line: `✓ saved N · backups written first` on success, the
 *  set error or the {deny} reason otherwise (drawn even while dirty, so the
 *  reason for the still-dirty footer reads on the same screen). */
function SaveMsg(els: Els, msg: string, cols: number): RenderElement {
  const { Text } = els
  const ok = msg.startsWith('✓')
  return <Text bold={ok} color={ok ? undefined : ROLE.err.fg}>{clip(msg, Math.max(1, cols))}</Text>
}

/** The dirty footer: `● N unsaved · <keys> … s save x discard` — the keys
 *  truncated with `…` to what remains of the line. The save press runs the
 *  ordered write (S3); discard only clears the edits, never writes. */
function DirtyFooter(els: Els, keys: readonly string[], cols: number, noHk: true | undefined, onSave: () => void, onClear: () => void): RenderElement {
  const { Box, Text } = els
  const head = `● ${keys.length} unsaved`
  const save = Segment(els, { key: 'set-save', label: 'save', on: true, hotkey: noHk ? undefined : 's', onPress: onSave })
  const discard = Segment(els, { key: 'set-discard', label: 'discard', on: false, hotkey: noHk ? undefined : 'x', onPress: onClear })
  const fixed = head.length + 2 + 's: save'.length + 4 + 'x: discard'.length + 4 + 3 /* gaps */ + 3 /* ' · ' */
  const avail = cols - fixed
  const keysText = avail >= 4 ? clip(keys.join(', '), avail) : ''
  return (
    <Box flexDirection="row" gap={1} borderStyle="round" borderColor={ROLE.warn.fg} paddingX={1}>
      {Chip(els, { label: head, role: 'warn', solid: true })}
      {keysText !== '' && <Text dimColor={true}>{keysText}</Text>}
      {save}
      {discard}
    </Box>
  )
}
