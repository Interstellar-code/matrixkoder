// Kit unit tests (plan dashboard-v2-p0b, V0.2 + V0.5): the pure render helpers
// called with a stub element table, so every assertion reads the element the
// helper actually built (backgroundColor, borderStyle, borderColor, the chip's
// one-space pad, the Button's variant/label/onPress). Controls are pressed by
// calling the closure the Button carries — C10: no press ever leaves the test.
import { test, expect } from 'claude-code/testing'
import type { Elements } from 'claude-code'
import { ActionButton, ActionButtons, ActionRow, Card, Chip, ControlRow, Section, SmoothBar, actionLabel } from './kit'
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

  expect(btn({ key: 'c', glyph: '‖', word: 'pause', tone: 'warn' }).props.variant).toBe('secondary')
  expect(btn({ key: 'd', glyph: '■', word: 'stop', tone: 'danger' }).props.variant).toBe('secondary')
  expect(btn({ key: 'e', glyph: '?', word: 'ask', tone: 'quiet' }).props.dimColor).toBe(true)
  expect(btn({ key: 'f', glyph: 'x', word: 'drop', tone: 'disabled' }).props.dimColor).toBe(true)
  expect(actionLabel({ key: 'g', glyph: '⋯', word: '' })).toBe('⋯')
})

test('a disabled ActionButton does not run its onPress; every other tone does', () => {
  let live = 0
  let dead = 0
  node(ActionButton(stubEls(), { key: 'a', glyph: 'n', word: 'nudge', onPress: () => { live++ } })).props.onPress as () => void
  const on = node(ActionButton(stubEls(), { key: 'b', glyph: 'n', word: 'nudge', onPress: () => { live++ } }))
  const off = node(ActionButton(stubEls(), { key: 'c', glyph: 'n', word: 'drop', tone: 'disabled', onPress: () => { dead++ } }))
  ;(on.props.onPress as () => void)()
  ;(off.props.onPress as () => void)()
  expect(live).toBe(1)
  expect(dead).toBe(0)
})

test('ActionButtons keeps a row that fits and folds the rest into ⋯ More at 36 cols', () => {
  const buttons = [
    { key: 'a', glyph: 'a', word: 'Hold P6', tone: 'primary' as const, primary: true },
    { key: 'b', glyph: 'b', word: 'Rebase later' },
    { key: 'c', glyph: 'c', word: 'Fold' },
  ]
  const wide = kids(node(ActionButtons(stubEls(), buttons, 46)))
  expect(wide.map(b => b.props.label)).toEqual(['a Hold P6', 'b Rebase later', 'c Fold'])

  const narrow = kids(node(ActionButtons(stubEls(), buttons, 36)))
  expect(narrow.map(b => b.props.label)).toEqual(['a Hold P6', '⋯ More'])
  expect(narrow[1]?.props.plain).toBe(true)
})

test('ControlRow shows the state’s buttons and pressing only reports the next state', () => {
  const seen: string[] = []
  const row = (state: Parameters<typeof ControlRow>[1]) =>
    kids(node(ControlRow(stubEls(), state, 46, next => { seen.push(next) })))
  const press = (state: Parameters<typeof ControlRow>[1], label: string): void => {
    const b = row(state).find(x => x.props.label === label)
    expect(b, `${state} has ${label}`).toBeDefined()
    ;(b?.props.onPress as () => void)()
  }

  expect(row('building').map(b => b.props.label)).toEqual(['‖ pause', '■ stop'])
  expect(row('paused').map(b => b.props.label)).toEqual(['▶ resume', '■ stop'])
  expect(row('stopped').map(b => b.props.label)).toEqual(['↻ restart'])

  press('building', '‖ pause')
  press('building', '■ stop')
  press('paused', '▶ resume')
  press('paused', '■ stop')
  press('stopped', '↻ restart')
  expect(seen).toEqual(['paused', 'stopped', 'building', 'stopped', 'building'])
})
