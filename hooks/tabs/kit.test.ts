// Kit unit tests (plan dashboard-v2-p0b, V0.2 + V0.5): the pure render helpers
// called with a stub element table, so every assertion reads the element the
// helper actually built (backgroundColor, borderStyle, borderColor, the chip's
// one-space pad, the Button's variant/label/onPress). Controls are pressed by
// calling the closure the Button carries — C10: no press ever leaves the test.
import { test, expect } from 'claude-code/testing'
import type { Elements } from 'claude-code'
import { ActionButton, ActionButtons, ActionRow, Card, Chip, ControlRow, Section, SmoothBar, TreeRow, actionLabel } from './kit'
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
  expect(head?.props.color).toBe('subtle') // a ThemeKey: body text follows the theme
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
