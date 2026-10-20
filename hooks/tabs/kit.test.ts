// Kit unit tests (plan dashboard-v2-p0b, V0.2 + V0.5): the pure render helpers
// called with a stub element table, so every assertion reads the element the
// helper actually built (backgroundColor, borderStyle, borderColor, the chip's
// one-space pad, the Button's variant/label/onPress). Controls are pressed by
// calling the closure the Button carries — C10: no press ever leaves the test.
import { test, expect } from 'claude-code/testing'
import type { Elements } from 'claude-code'
import { ICONS, ActionButton, ActionButtons, ActionRow, Card, Chip, ControlRow, IconButton, Section, SettingRow, SmoothBar, TreeRow, actionLabel, iconHint, iconWidth } from './kit'
import type { IconName } from './kit'
import type { SettingCtx, SettingField } from './kit'
import { PALETTE, ROLE } from './palette'

/** the plain-data node a stub constructor builds (kit.tsx returns these). */
type Node = { type: string; props: Record<string, unknown>; children: unknown[] }

const stubEls = (extra: Record<string, unknown> = {}): Elements[keyof Elements] => {
  const make = (type: string) => (props: Record<string, unknown>, ...children: unknown[]) =>
    ({ type, props, children: children.length > 0 ? children : (props.children as unknown[] | undefined) ?? [] }) as unknown
  return {
    Box: make('Box'),
    Text: make('Text'),
    Button: (props: Record<string, unknown>) => ({ type: 'Button', props, children: [] }),
    ...extra,
  } as unknown as Elements[keyof Elements]
}

const node = (el: unknown): Node => el as Node
const kids = (el: unknown): Node[] => (Array.isArray((el as { children?: unknown[] }).children) ? (el as Node).children : []).map(node)
const text = (el: unknown): string => kids(el).map(c => (typeof c === 'string' ? c : text(c))).join('')

// ---------- V0.2 · Card / Chip / SmoothBar / Section / ActionRow ----------

test('Card is a panel Box with a round border in the role colour, line by default', () => {
  const plain = node(Card(stubEls(), {}, 'body'))
  expect(plain.type).toBe('Box')
  expect(plain.props.backgroundColor).toBe(PALETTE.panel)
  expect(plain.props.borderStyle).toBe('round')
  expect(plain.props.borderColor).toBe(PALETTE.line)
  expect(plain.props.paddingX).toBe(1)

  for (const [role, want] of [['live', ROLE.live.fg], ['warn', ROLE.warn.fg], ['err', ROLE.err.fg]] as const) {
    const card = node(Card(stubEls(), { role }, 'body'))
    expect(card.props.borderColor, role).toBe(want)
    expect(card.props.backgroundColor, role).toBe(PALETTE.panel)
  }
  expect(text(plain)).toBe('body') // children are drawn inside the card
})

test('Chip pads its label with one cell each side: soft on the role tint, solid on the role colour', () => {
  const soft = node(Chip(stubEls(), { label: 'MOCK', role: 'idle' }))
  expect(soft.type).toBe('Text')
  expect(soft.props.backgroundColor).toBe(ROLE.idle.tint)
  expect(soft.props.color).toBe(ROLE.idle.fg)
  expect(text(soft)).toBe(' MOCK ')
  expect(soft.props.bold).toBeUndefined()

  const solid = node(Chip(stubEls(), { label: '6/12 Watch', role: 'live', solid: true }))
  expect(solid.props.backgroundColor).toBe(ROLE.live.fg)
  expect(solid.props.color).toBe(PALETTE.bg)
  expect(solid.props.bold).toBe(true)
  expect(text(solid)).toBe(' 6/12 Watch ')
})

test('SmoothBar fills in eighth blocks on a track and never drops below one cell', () => {
  const bar = (pct: number, width: number) => text(node(SmoothBar(stubEls(), { pct, width, role: 'live' })))
  expect(bar(0, 4)).toBe('    ')
  expect(bar(100, 4)).toBe('████')
  expect(bar(37.5, 4)).toBe('█▌  ') // one full cell + 4/8 of the next
  expect(bar(37.5, 0)).toBe('▍') // width is clamped to 1 cell, 3/8 of it filled
  expect(bar(0, 1)).toBe(' ')
  expect(bar(100, 2)).toBe('██')

  const el = node(SmoothBar(stubEls(), { pct: 50, width: 4, role: 'warn' }))
  expect(el.props.color).toBe(ROLE.warn.fg)
  expect(el.props.backgroundColor).toBe(PALETTE.track)
})

test('Section upper-cases its bold label beside an optional dim right text', () => {
  const sec = node(Section(stubEls(), { label: 'plan', right: '3/8' }))
  expect(sec.type).toBe('Box')
  const [head, right] = kids(sec)
  expect(head?.props.bold).toBe(true)
  expect(head?.props.color).toBe('#a3adb8') // PALETTE.mut: the muted label colour
  expect(text(head)).toBe('PLAN')
  expect(right?.props.dimColor).toBe(true)
  expect(text(right)).toBe('3/8')
  expect(text(node(Section(stubEls(), { label: 'lanes' })))).toBe('LANES')
})

test('ActionRow draws its dim action text', () => {
  const row = node(ActionRow(stubEls(), { actions: 'o open · n nudge' }))
  expect(row.type).toBe('Text')
  expect(row.props.dimColor).toBe(true)
  expect(text(row)).toBe('o open · n nudge')
})

// ---------- V0.5 · ActionButton / ActionButtons / ControlRow ----------

/** every Button node under `el`, in document order (a warn/danger action wraps
 *  its Button in a Box beside the coloured glyph Text). */
function buttonsIn(el: unknown): Node[] {
  const out: Node[] = []
  const walk = (n: unknown): void => {
    if (typeof n !== 'object' || n === null) return
    const cur = n as Node
    if (cur.type === 'Button') out.push(cur)
    for (const c of kids(cur)) walk(c)
  }
  walk(el)
  return out
}

test('ActionButton labels start with the hotkey glyph and carry the tone as native props', () => {
  const btn = (spec: Parameters<typeof ActionButton>[1]) => node(ActionButton(stubEls(), spec))
  const resume = btn({ key: 'a', glyph: '▶', word: 'resume', tone: 'primary' })
  expect(resume.type).toBe('Button')
  expect(resume.props.label).toBe('▶ resume')
  expect(resume.props.variant).toBe('primary')
  expect(resume.props.hotkey).toBeUndefined() // a glyph, not a letter: no engine hotkey

  const nudge = btn({ key: 'b', glyph: 'n', word: 'nudge' })
  expect(nudge.props.label).toBe('n nudge')
  expect(nudge.props.hotkey).toBe('n') // the letter is also the engine hotkey
  expect(nudge.props.variant).toBeUndefined()
  expect(nudge.props.dimColor).toBeUndefined()

  expect(btn({ key: 'e', glyph: '?', word: 'ask', tone: 'quiet' }).props.dimColor).toBe(true)
  expect(btn({ key: 'f', glyph: 'x', word: 'drop', tone: 'disabled' }).props.dimColor).toBe(true)
  expect(actionLabel({ key: 'g', glyph: '⋯', word: '' })).toBe('⋯')
})

test('M3: a warn/danger ActionButton draws its glyph in the palette colour beside the Button', () => {
  const pause = node(ActionButton(stubEls(), { key: 'p', glyph: '‖', word: 'pause', tone: 'warn' }))
  expect(pause.type).toBe('Box') // glyph + Button, not the glyph inside the label
  const [glyph, button] = kids(pause)
  expect(text(glyph)).toBe('‖')
  expect(glyph?.props.color).toBe(ROLE.warn.fg)
  expect(button?.props.label).toBe('pause')
  expect(button?.props.variant).toBe('secondary')

  const stop = node(ActionButton(stubEls(), { key: 's', glyph: '■', word: 'stop', tone: 'danger' }))
  const [dGlyph, dButton] = kids(stop)
  expect(dGlyph?.props.color).toBe(ROLE.err.fg)
  expect(dButton?.props.label).toBe('stop')

  const resume = node(ActionButton(stubEls(), { key: 'r', glyph: '▶', word: 'resume', tone: 'primary' }))
  expect(resume.type).toBe('Button') // other tones keep the glyph in the label
})

test('a disabled ActionButton does not run its onPress; every other tone does', () => {
  let live = 0
  let dead = 0
  const on = node(ActionButton(stubEls(), { key: 'b', glyph: 'n', word: 'nudge', onPress: () => { live++ } }))
  const off = node(ActionButton(stubEls(), { key: 'c', glyph: 'n', word: 'drop', tone: 'disabled', onPress: () => { dead++ } }))
  ;(on.props.onPress as () => void)()
  ;(off.props.onPress as () => void)()
  expect(live).toBe(1)
  expect(dead).toBe(0)
})

test('ActionButtons folds the rest into ⋯ More at 36 cols when the caller can reveal them', () => {
  const buttons = [
    { key: 'a', glyph: 'a', word: 'Hold P6', tone: 'primary' as const, primary: true },
    { key: 'b', glyph: 'b', word: 'Rebase later' },
    { key: 'c', glyph: 'c', word: 'Fold' },
  ]
  const labels = (cols: number, onMore?: () => void) =>
    kids(node(ActionButtons(stubEls(), buttons, cols, onMore))).map(b => b.props.label)
  expect(labels(46)).toEqual(['a Hold P6', 'b Rebase later', 'c Fold'])

  let more = 0
  const narrow = kids(node(ActionButtons(stubEls(), buttons, 36, () => { more++ })))
  expect(narrow.map(b => b.props.label)).toEqual(['a Hold P6', '⋯ More'])
  expect(narrow[1]?.props.plain).toBe(true)
  ;(narrow[1]?.props.onPress as () => void)()
  expect(more).toBe(1) // what a press of ⋯ runs: the caller's local reveal (C10)

  // no onMore: every action is drawn rather than a `⋯` that can do nothing
  expect(labels(36)).toEqual(['a Hold P6', 'b Rebase later', 'c Fold'])
  expect(kids(node(ActionButtons(stubEls(), buttons, 36))).every(b => b.props.label !== '⋯ More')).toBe(true)
})

test('M1: ControlRow keys carry the keyPrefix, so two rows never share one', () => {
  const keysOf = (prefix: string | undefined) =>
    buttonsIn(ControlRow(stubEls(), 'building', 46, () => {}, prefix)).map(b => String(b.props.key))
  expect(keysOf(undefined)).toEqual(['ctl-pause', 'ctl-stop']) // the default prefix
  expect(keysOf('p4')).toEqual(['p4-pause', 'p4-stop'])
  const all = [...keysOf('p4'), ...keysOf('p5')]
  expect(new Set(all).size, 'every key unique across rows').toBe(all.length)
})

test('ControlRow shows the state’s buttons and pressing only reports the next state', () => {
  const seen: string[] = []
  const row = (state: Parameters<typeof ControlRow>[1], cols = 46) =>
    buttonsIn(ControlRow(stubEls(), state, cols, next => { seen.push(next) }))
  const press = (state: Parameters<typeof ControlRow>[1], label: string): void => {
    const b = row(state).find(x => x.props.label === label)
    expect(b, `${state} has ${label}`).toBeDefined()
    ;(b?.props.onPress as () => void)()
  }

  expect(row('building').map(b => b.props.label)).toEqual(['pause', 'stop'])
  // the primary tones keep their glyph in the label; only warn/danger split it out
  expect(row('paused').map(b => b.props.label)).toEqual(['▶ resume', 'stop'])
  expect(row('stopped').map(b => b.props.label)).toEqual(['↻ restart'])

  press('building', 'pause')
  press('building', 'stop')
  press('paused', '▶ resume')
  press('paused', 'stop')
  press('stopped', '↻ restart')
  expect(seen).toEqual(['paused', 'stopped', 'building', 'stopped', 'building'])
})

test('LOW: ControlRow fits 36 and 46 cols — no fold, no dead ⋯ button', () => {
  for (const cols of [36, 46] as const) {
    const el = node(ControlRow(stubEls(), 'building', cols, () => {}))
    const labels = buttonsIn(el).map(b => String(b.props.label))
    expect(labels, `labels at ${cols}`).toEqual(['pause', 'stop'])
    expect(labels).not.toContain('More')
    // the drawn width: the glyph Text, its gap, and `[ word ]` per action
    const row = kids(el)
    const width = row.reduce((n, c) => n + (c.type === 'Box' ? 1 + 1 + String(buttonsIn(c)[0]?.props.label ?? '').length + 4 : 0), 0) + Math.max(0, row.length - 1)
    expect(width, `row width at ${cols}`).toBeLessThanOrEqual(cols)
  }
})

// ---------- M4 · TreeRow ----------

/** one TreeRow's drawn width: its children's text plus the gap columns. */
const treeWidth = (el: Node): number => {
  const parts = kids(el)
  return parts.reduce((n, p) => n + text(p).length, 0) + Math.max(0, parts.length - 1)
}

test('M4: TreeRow draws the depth prefix, the role glyph, a fitted title and an optional chip', () => {
  const first = kids(node(TreeRow(stubEls(), { depth: 0, last: false, glyph: '●', role: 'live', title: 'P4 Payment adapter' }, 46)))
  expect(first[0]?.props.dimColor).toBe(true)
  expect(text(first[0])).toBe('├ ')
  expect(first[1]?.props.color).toBe(ROLE.live.fg)
  expect(text(first[1])).toBe('●')
  expect(text(first[2])).toBe('P4 Payment adapter')
  expect(first).toHaveLength(3) // no chip without `right`

  const nested = kids(node(TreeRow(stubEls(), { depth: 1, last: true, glyph: '✓', role: 'ok', title: 'Cart schema' }, 46)))
  expect(text(nested[0])).toBe('│ └ ')
  expect(nested[1]?.props.color).toBe(ROLE.ok.fg)

  const deep = kids(node(TreeRow(stubEls(), { depth: 2, last: false, glyph: '○', role: 'idle', title: 'x' }, 46)))
  expect(text(deep[0])).toBe('│ │ ├ ')

  const titled = node(TreeRow(stubEls(), { depth: 0, last: false, glyph: '●', role: 'live', title: 'P4 Payment provider adapter for the checkout flow', right: 'build 14m' }, 46))
  const titleText = text(kids(titled)[2])
  expect(titleText.endsWith('…')).toBe(true) // the title yields to the chip
  expect(treeWidth(titled)).toBeLessThanOrEqual(46)
  const chip = kids(titled)[3]
  expect(text(chip)).toBe(' build 14m ')
  expect(chip?.props.backgroundColor).toBe(ROLE.live.tint)
  expect(chip?.props.color).toBe(ROLE.live.fg)

  // never wider than cols, at any width
  for (const cols of [36, 40, 46, 56] as const) {
    const row = node(TreeRow(stubEls(), { depth: 1, last: false, glyph: '●', role: 'warn', title: 'P6 Tax rules and the migration order', right: '! blocked' }, cols))
    expect(treeWidth(row), `row at ${cols}`).toBeLessThanOrEqual(cols)
  }
})

// ---------- S2.1 · SettingRow ----------

/** a stub constructor maker for the field elements (Input, Select). */
const el = (type: string) => (props: Record<string, unknown>, ...children: unknown[]) =>
  ({ type, props, children: children.length > 0 ? children : (props.children as unknown[] | undefined) ?? [] }) as unknown

/** an element table with the field elements S2.0 found on terminal + desktop. */
const fieldEls = (): Elements[keyof Elements] => stubEls({ Input: el('Input'), Select: el('Select') })

/** every node of `type` under `el`, in document order. */
function nodesIn(type: string, tree: unknown): Node[] {
  const out: Node[] = []
  const walk = (n: unknown): void => {
    if (typeof n !== 'object' || n === null) return
    const cur = n as Node
    if (cur.type === type) out.push(cur)
    for (const c of kids(cur)) walk(c)
  }
  walk(tree)
  return out
}

/** a node's drawn width: its text run, `[ label ]` per Button (label + 2 for
 * a plain one), nested rows recursed, one gap column between parts. */
const drawnWidth = (n: Node): number => {
  if (n.type === 'Button') return String(n.props.label).length + (n.props.plain ? 2 : 4)
  if (n.type === 'Text') return text(n).length
  if (n.type === 'Input') return 0 // the engine lays the field out
  const parts = kids(n)
  return parts.reduce((acc, p) => acc + drawnWidth(node(p)), 0) + Math.max(0, parts.length - 1)
}

/** every row Box under a SettingRow fits `cols` (main row, stepper, option
 * lines, help line — wherever they sit in the tree). */
const everyRowFits = (root: Node, cols: number): void => {
  const walk = (n: unknown): void => {
    if (typeof n !== 'object' || n === null) return
    const cur = n as Node
    if (cur.type === 'Box' && cur.props.flexDirection === 'row') {
      expect(drawnWidth(cur), `row at ${cols}`).toBeLessThanOrEqual(cols)
    }
    for (const c of kids(cur)) walk(c)
  }
  walk(root)
}

/** a SettingCtx with counters, so a test reads what a press reported. */
const settingCtx = (over: Partial<SettingCtx> = {}): SettingCtx => ({
  cols: 46, selected: false, changed: false, open: false,
  onPick: () => {}, onToggle: () => {}, onInc: () => {}, onDec: () => {},
  onChoose: () => {}, onToggleOpen: () => {}, onEdit: () => {}, onReset: () => {},
  ...over,
})

test('S2.1 bool: `◉ on` toggle chip fits 46+36, `●` when changed, help + default + r reset when selected', () => {
  const field: SettingField = {
    key: 'autosave', label: 'Autosave snapshots', type: 'bool', value: true, default: false,
    help: 'Snapshot the plan before each wave', editable: true,
  }
  for (const cols of [46, 36] as const) {
    const root = node(SettingRow(stubEls(), field, settingCtx({ cols, changed: true })))
    everyRowFits(root, cols)
    const tgl = buttonsIn(root).find(b => b.props.label === '◉ on')
    expect(tgl, `toggle chip at ${cols}`).toBeDefined()
    expect(tgl?.props.hotkey).toBeUndefined() // a glyph, not a letter: no pane hotkey
    const marks = nodesIn('Text', root).filter(t => text(t) === '●')
    expect(marks).toHaveLength(1)
    expect(marks[0]?.props.color).toBe(ROLE.warn.fg) // the changed marker

    let toggled = 0
    const live = node(SettingRow(stubEls(), field, settingCtx({ cols, onToggle: () => { toggled++ } })))
    ;(buttonsIn(live).find(b => b.props.label === '◉ on')?.props.onPress as () => void)()
    expect(toggled).toBe(1)

    let resets = 0
    const sel = node(SettingRow(stubEls(), field, settingCtx({ cols, selected: true, onReset: () => { resets++ } })))
    const selText = text(sel)
    // the help yields to `default X · r reset` — its prefix survives at both widths
    expect(selText.includes('Snapsh')).toBe(true)
    expect(selText.includes('default off')).toBe(true)
    const reset = buttonsIn(sel).find(b => b.props.label === 'r reset')
    expect(reset).toBeDefined()
    expect(reset?.props.hotkey).toBe('r') // the one letter the Settings design allows
    ;(reset?.props.onPress as () => void)()
    expect(resets).toBe(1)
  }
  expect(text(node(SettingRow(stubEls(), field, settingCtx()))).includes('●')).toBe(false) // unchanged: no marker
})

test('S2.1 num: `− 12m +` stepper fits 46+36; pct adds the SmoothBar at value/max', () => {
  const stall: SettingField = {
    key: 'stall', label: 'Stall before flagging a lane stalled', type: 'num', value: 12, default: 15,
    min: 1, max: 60, unit: 'm', help: 'Pane silence that flags a lane stalled', editable: true,
  }
  for (const cols of [46, 36] as const) {
    const root = node(SettingRow(stubEls(), stall, settingCtx({ cols, changed: true })))
    everyRowFits(root, cols)
    const labels = buttonsIn(root).map(b => String(b.props.label))
    expect(labels).toContain('−')
    expect(labels).toContain('+')
    expect(buttonsIn(root).every(b => b.props.hotkey === undefined)).toBe(true)
    expect(text(root).includes('12m')).toBe(true)
    expect(text(root).includes('●')).toBe(true)

    let inc = 0
    let dec = 0
    const live = node(SettingRow(stubEls(), stall, settingCtx({ cols, onInc: () => { inc++ }, onDec: () => { dec++ } })))
    ;(buttonsIn(live).find(b => b.props.label === '+')?.props.onPress as () => void)()
    ;(buttonsIn(live).find(b => b.props.label === '−')?.props.onPress as () => void)()
    expect(inc).toBe(1)
    expect(dec).toBe(1)

    const sel = text(node(SettingRow(stubEls(), stall, settingCtx({ cols, selected: true }))))
    expect(sel.includes('default 15m')).toBe(true)
  }

  const conf: SettingField = {
    key: 'conf', label: 'Review confidence floor', type: 'num', value: 60, default: 50,
    max: 100, pct: true, unit: '%', help: 'Reviews below this ask the reviewer to justify', editable: true,
  }
  const bar = (cols: number): Node => {
    const bars = nodesIn('Text', SettingRow(stubEls(), conf, settingCtx({ cols }))).filter(t => t.props.backgroundColor === PALETTE.track)
    expect(bars).toHaveLength(1)
    return bars[0] as Node
  }
  expect(text(bar(46))).toBe('██████    ') // 60% of the 10-cell bar
  expect(bar(46).props.color).toBe(ROLE.live.fg)
  expect(text(bar(36))).toBe('███▋  ') // 60% of the 6-cell bar: 3 full cells + 5/8
  for (const cols of [46, 36] as const) {
    const root = node(SettingRow(stubEls(), conf, settingCtx({ cols, changed: true })))
    everyRowFits(root, cols)
    expect(text(root).includes('60%')).toBe(true)
  }
})

test('S2.1 seg: one pressable per opt, the current `●`-marked; a press reports the chosen value', () => {
  const field: SettingField = {
    key: 'revsplit', label: 'Reviewer split', type: 'seg', value: 'auto', default: 'ask',
    opts: [
      { value: 'ask', note: 'ask each time' },
      { value: 'auto', note: 'route by size' },
      { value: 'never', note: 'one pass' },
    ],
    help: 'When a second reviewer joins a review round', editable: true,
  }
  for (const cols of [46, 36] as const) {
    const root = node(SettingRow(stubEls(), field, settingCtx({ cols, changed: true })))
    everyRowFits(root, cols)
    const labels = buttonsIn(root).map(b => String(b.props.label))
    // the boxed strip: one plain press per option, the current one on the blue tint
    for (const w of ['ask', 'never']) expect(labels).toContain(w) // the picked `auto` is plain tint text, not a press
    expect(buttonsIn(root).every(b => b.props.hotkey === undefined)).toBe(true) // glyphs, not letters

    const chosen: string[] = []
    const live = node(SettingRow(stubEls(), field, settingCtx({ cols, onChoose: v => { chosen.push(v) } })))
    // a shown word stays a prefix of its full value (shrunk at 36 cols), so
    // the press still resolves to the option behind it
    const shown = (label: unknown): string => String(label).replace(/^[●○] /, '')
    const btnFor = (full: string) =>
      buttonsIn(live).find(b => { const w = shown(b.props.label); return w !== '' && full.startsWith(w) })
    ;(btnFor('never')?.props.onPress as () => void)()
    ;(btnFor('ask')?.props.onPress as () => void)()
    expect(chosen).toEqual(['never', 'ask'])

    const sel = node(SettingRow(stubEls(), field, settingCtx({ cols, selected: true })))
    expect(text(sel).includes('default ask')).toBe(true)
    expect(buttonsIn(sel).some(b => b.props.label === 'r reset')).toBe(true)
  }
})

test('S2.1 select: closed `value ▾` toggles the option list below (current `●`, every opt pressable)', () => {
  const field: SettingField = {
    key: 'surface', label: 'Dashboard surface', type: 'select', value: 'terminal', default: 'terminal',
    opts: [
      { value: 'terminal', note: 'in-session TUI' },
      { value: 'desktop', note: 'desktop window' },
    ],
    help: 'Where the /mk pane docks', editable: true,
  }
  for (const cols of [46, 36] as const) {
    const closed = node(SettingRow(fieldEls(), field, settingCtx({ cols })))
    everyRowFits(closed, cols)
    const chip = buttonsIn(closed).find(b => b.props.label === ' terminal ▾')
    expect(chip, `closed chip at ${cols}`).toBeDefined()
    expect(chip?.props.plain).toBe(true) // no engine hotkey, however short the value
    expect(chip?.props.hotkey).toBeUndefined()
    expect(text(closed).includes('desktop')).toBe(false) // closed: no option list

    let opened = 0
    const live = node(SettingRow(fieldEls(), field, settingCtx({ cols, onToggleOpen: () => { opened++ } })))
    ;(buttonsIn(live).find(b => b.props.label === ' terminal ▾')?.props.onPress as () => void)()
    expect(opened).toBe(1)

    const open = node(SettingRow(fieldEls(), field, settingCtx({ cols, open: true })))
    everyRowFits(open, cols)
    const openText = text(open)
    expect(openText.includes('in-session TUI')).toBe(true) // the dim notes
    expect(openText.includes('desktop window')).toBe(true)
    const chosen: string[] = []
    const liveOpen = node(SettingRow(fieldEls(), field, settingCtx({ cols, open: true, onChoose: v => { chosen.push(v) } })))
    const opt = (v: string): Node | undefined => buttonsIn(liveOpen).find(b => b.props.label === v)
    ;(opt('desktop')?.props.onPress as () => void)()
    ;(opt('terminal')?.props.onPress as () => void)()
    expect(chosen).toEqual(['desktop', 'terminal'])
    const marks = nodesIn('Text', open).filter(t => text(t) === '●')
    expect(marks).toHaveLength(1) // the current option, in the live role colour
    expect(marks[0]?.props.color).toBe(ROLE.live.fg)

    const sel = node(SettingRow(fieldEls(), field, settingCtx({ cols, selected: true })))
    expect(text(sel).includes('default terminal')).toBe(true)
    expect(buttonsIn(sel).some(b => b.props.label === 'r reset')).toBe(true)
  }
})

test('S2.1 text: the native Input carries the label (● when changed), value and onEdit on both field chains', () => {  const field: SettingField = {
    key: 'reviewer', label: 'Fallback reviewer', type: 'text', value: 'claude sonnet', default: 'opus',
    help: 'Which model reviews when the pick is exhausted', editable: true,
  }
  for (const cols of [46, 36] as const) {
    const inputs = nodesIn('Input', SettingRow(fieldEls(), field, settingCtx({ cols })))
    expect(inputs).toHaveLength(1)
    const input = inputs[0] as Node
    expect(input.props.value).toBe('claude sonnet')
    expect(input.props.label, 'the name draws once, on the row press').toBeUndefined()
    // the label press selects the row (R3 HIGH 1)
    const pick = buttonsIn(node(SettingRow(fieldEls(), field, settingCtx({ cols })))).find(b => String(b.props.label) === 'Fallback reviewer')
    expect(pick, `the label press at ${cols}`).toBeDefined()
    expect(pick?.props.plain).toBe(true)
    expect(pick?.props.hotkey).toBeUndefined()
    const edits: string[] = []
    const live = node(SettingRow(fieldEls(), field, settingCtx({ cols, onEdit: v => { edits.push(v) } })))
    const liveInput = nodesIn('Input', live)[0] as Node
    ;(liveInput.props.onInput as (v: string) => void)('o')
    ;(liveInput.props.onSubmit as (v: string) => void)('opus')
    expect(edits).toEqual(['o', 'opus'])

    const changed = buttonsIn(node(SettingRow(fieldEls(), field, settingCtx({ cols, changed: true })))).find(b => String(b.props.label).startsWith('● '))
    expect(changed, `the changed label press at ${cols}`).toBeDefined()

    const sel = node(SettingRow(fieldEls(), field, settingCtx({ cols, selected: true })))
    const selText = text(sel)
    expect(selText.includes('Which')).toBe(true) // the help's prefix (it yields to the tail)
    expect(selText.includes('default opus')).toBe(true)
    expect(buttonsIn(sel).some(b => b.props.label === 'r reset')).toBe(true)
  }
})

test('S2.1 fallback: no Input/Select in the element table → dim value + `edit in file`, and a non-editable row draws the why line', () => {
  const textField: SettingField = {
    key: 'reviewer', label: 'Fallback reviewer', type: 'text', value: 'claude sonnet', default: 'opus',
    help: 'Which model reviews when the pick is exhausted', editable: true,
  }
  const selectField: SettingField = {
    key: 'surface', label: 'Dashboard surface', type: 'select', value: 'terminal', default: 'terminal',
    opts: [{ value: 'terminal', note: 'in-session TUI' }, { value: 'desktop', note: 'desktop window' }],
    help: 'Where the /mk pane docks', editable: true,
  }
  for (const cols of [46, 36] as const) {
    for (const field of [textField, selectField] as const) {
      const root = node(SettingRow(stubEls(), field, settingCtx({ cols }))) // no Input, no Select
      expect(nodesIn('Input', root)).toHaveLength(0)
      expect(nodesIn('Select', root)).toHaveLength(0)
      everyRowFits(root, cols)
      const flat = text(root)
      expect(flat.includes(String(field.value))).toBe(true) // the dim value
      expect(flat.includes('edit in file')).toBe(true) // plus the note, no crash
      const dims = nodesIn('Text', root).filter(t => t.props.dimColor === true)
      expect(dims.map(t => text(t))).toContain('edit in file')
      // the one pressable is the R3 label press (it fires onPick and reaches
      // the help line); no control draws without a field element. At 36 the
      // long label yields to the wide dim control, at 46 it draws whole.
      const picks = buttonsIn(root)
      expect(picks).toHaveLength(1)
      expect(picks[0]?.props.plain).toBe(true)
      const want = cols === 46 ? field.label : field.label.slice(0, 6)
      expect(String(picks[0]?.props.label).startsWith(want)).toBe(true)
      let picked = 0
      const live = node(SettingRow(stubEls(), field, settingCtx({ cols, onPick: () => { picked++ } })))
      ;((buttonsIn(live)[0]?.props.onPress ?? (() => {})) as () => void)()
      expect(picked).toBe(1)
    }
  }

  const ro: SettingField = {
    key: 'pluginver', label: 'Plugin version', type: 'bool', value: true, default: true,
    help: 'The installed plugin version', editable: false, why: 'Read-only: set by the marketplace install',
  }
  for (const cols of [46, 36] as const) {
    const root = node(SettingRow(stubEls(), ro, settingCtx({ cols })))
    everyRowFits(root, cols)
    // the one pressable is the R3 label press (it reaches the why line); a control would do nothing
    expect(buttonsIn(root).map(b => String(b.props.label))).toEqual(['Plugin version'])
    expect(nodesIn('Text', root).map(t => text(t))).toContain(' ◉ on ') // the read-only chip, not pressable
    const sel = node(SettingRow(stubEls(), ro, settingCtx({ cols, selected: true })))
    const selText = text(sel)
    expect(selText.includes('Read-only: set by the')).toBe(true) // the why line, truncated to cols
    expect(selText.includes('default')).toBe(false) // nothing to reset
    expect(buttonsIn(sel).map(b => String(b.props.label))).toEqual(['Plugin version']) // the open press; no reset on read-only
  }
})

test('LOW: a text row caps its label so label + Input fit 36 cols', () => {
  const field: SettingField = {
    key: 'branch', label: 'Integration branch name lane', type: 'text', value: 'main', default: 'main',
    help: 'Lanes merge here', editable: true,
  }
  // the label budget leaves the minimum Input beside it (plain chrome + gap)
  const budget = (cols: number): number => Math.max(1, cols - 12 - 3)
  for (const cols of [46, 36] as const) {
    const root = node(SettingRow(fieldEls(), field, settingCtx({ cols })))
    everyRowFits(root, cols)
    const pick = buttonsIn(root).find(b => String(b.props.key).endsWith('-pick'))
    expect(pick, `the label press at ${cols}`).toBeDefined()
    expect(String(pick?.props.label ?? '').length, `label capped at ${cols}`).toBeLessThanOrEqual(budget(cols))
    // the changed marker rides inside the same budget
    const changed = buttonsIn(node(SettingRow(fieldEls(), field, settingCtx({ cols, changed: true })))).find(b =>
      String(b.props.key).endsWith('-pick'))
    expect(String(changed?.props.label ?? '').startsWith('● '), `changed marker at ${cols}`).toBe(true)
    expect(String(changed?.props.label ?? '').length, `changed label capped at ${cols}`).toBeLessThanOrEqual(budget(cols))
  }
  // the 28-char label draws whole at 46 (budget 31) and yields at 36 (budget 21)
  const glyph = (cols: number): string => {
    const pick = buttonsIn(node(SettingRow(fieldEls(), field, settingCtx({ cols })))).find(b =>
      String(b.props.key).endsWith('-pick'))
    return String(pick?.props.label ?? '')
  }
  expect(glyph(46)).toBe('Integration branch name lane')
  expect(glyph(36)).toBe('Integration branch n…')
})

// ---------- IconButton · ICONS · iconWidth · iconHint ----------

const iconNames = Object.keys(ICONS) as IconName[]

test('IconButton draws just the icon glyph, plain, under the caller key, and a press calls onPress', () => {
  for (const name of iconNames) {
    let pressed = 0
    const b = node(IconButton(stubEls(), { key: `ik-${name}`, icon: name, onPress: () => { pressed += 1 } }))
    expect(b.type).toBe('Button')
    expect(b.props.key).toBe(`ik-${name}`)
    expect(b.props.label, `${name} label is the glyph alone`).toBe(ICONS[name].glyph)
    expect([...String(b.props.label)].length, `${name} is one cell wide`).toBe(1)
    expect(b.props.plain).toBe(true)
    expect(b.props.hotkey, 'no engine hotkey').toBeUndefined()
    expect(b.props.title, 'no tooltip').toBeUndefined()
    ;(b.props.onPress as () => void)()
    expect(pressed).toBe(1)
  }
})

test('IconButton tone: primary and default draw at full strength, quiet dims, the icon tone is the default', () => {
  const draw = (icon: IconName, tone?: 'primary' | 'default' | 'quiet'): Node =>
    node(IconButton(stubEls(), { key: 'k', icon, onPress: () => {}, ...(tone != null ? { tone } : {}) }))
  expect(draw('play').props.dimColor).toBeUndefined() // primary
  expect(draw('pause').props.dimColor).toBeUndefined() // default
  expect(draw('close').props.dimColor).toBe(true) // quiet
  expect(draw('close', 'default').props.dimColor).toBeUndefined() // an override wins
  expect(draw('pause', 'quiet').props.dimColor).toBe(true)
  expect(draw('pause', 'primary').props.dimColor).toBeUndefined()
  // every tone is one Button (no split Text beside it): the glyph is the whole label
  for (const tone of ['primary', 'default', 'quiet'] as const) expect(draw('play', tone).type).toBe('Button')
  expect(ICONS.play.tone).toBe('primary')
  expect(ICONS.pause.tone).toBe('default')
  expect(ICONS.close.tone).toBe('quiet')
  expect(ICONS.delete.tone).toBe('quiet')
})

test('a labelled IconButton draws the glyph, a space and the word as its one plain label', () => {
  for (const name of iconNames) {
    let pressed = 0
    const b = node(IconButton(stubEls(), { key: `lk-${name}`, icon: name, labelled: true, onPress: () => { pressed += 1 } }))
    expect(b.type).toBe('Button')
    expect(b.props.key).toBe(`lk-${name}`)
    expect(b.props.label).toBe(`${ICONS[name].glyph} ${ICONS[name].word}`)
    expect(b.props.plain).toBe(true)
    expect(b.props.hotkey, 'no engine hotkey').toBeUndefined()
    expect(b.props.title, 'no tooltip').toBeUndefined()
    expect(b.props.dimColor === true, 'same tone as the glyph-only form').toBe(ICONS[name].tone === 'quiet')
    ;(b.props.onPress as () => void)()
    expect(pressed).toBe(1)
  }
  expect(node(IconButton(stubEls(), { key: 'k', icon: 'pause', labelled: true, onPress: () => {} })).props.label).toBe('‖ pause')
  expect(node(IconButton(stubEls(), { key: 'k', icon: 'play', labelled: true, onPress: () => {} })).props.label).toBe('▶ play')
  expect(node(IconButton(stubEls(), { key: 'k', icon: 'close', labelled: true, onPress: () => {} })).props.label).toBe('✕ close')
})

test('iconWidth is the width a plain Button draws for the label (no hotkey), glyph-only and labelled', () => {
  for (const name of iconNames) {
    const bare = node(IconButton(stubEls(), { key: 'k', icon: name, onPress: () => {} }))
    expect(iconWidth(name), name).toBe(String(bare.props.label).length)
    expect(iconWidth(name), name).toBe(1)
    const full = node(IconButton(stubEls(), { key: 'k', icon: name, labelled: true, onPress: () => {} }))
    expect(iconWidth(name, true), name).toBe(String(full.props.label).length)
    expect(iconWidth(name, true), name).toBe(1 + 1 + ICONS[name].word.length)
  }
  expect([iconWidth('pause', true), iconWidth('play', true), iconWidth('close', true)]).toEqual([7, 6, 7])
})

test('iconHint joins glyph and word with a dot', () => {
  expect(iconHint('pause', 'close')).toBe('‖ pause · ✕ close')
  expect(iconHint('play')).toBe('▶ play')
  expect(iconHint('close', 'delete')).toBe('✕ close · ✕ delete')
  expect(iconHint()).toBe('')
})

test('every ICONS glyph is exactly one column: one code point, not an emoji or a wide character', () => {
  for (const name of iconNames) {
    const { glyph } = ICONS[name]
    expect([...glyph].length, `${name} is one code point`).toBe(1)
    expect(glyph, `${name} is not emoji-presentation`).not.toMatch(/\p{Emoji_Presentation}/u)
    const cp = glyph.codePointAt(0)!
    const wide = (cp >= 0x1100 && cp <= 0x115f) || (cp >= 0x2e80 && cp <= 0xa4cf) || (cp >= 0xac00 && cp <= 0xd7a3) || (cp >= 0xf900 && cp <= 0xfaff) || (cp >= 0xfe30 && cp <= 0xfe6f) || (cp >= 0xff00 && cp <= 0xff60) || cp >= 0x1f000
    expect(wide, `${name} is not in a double-width block`).toBe(false)
  }
})
