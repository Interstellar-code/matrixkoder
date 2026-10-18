// Events tab tests (dashboard-v2-p0, T1.3): the mounted pane draws the
// Checkout v2 timeline (MOCK row, header, rows, footer) and hides Old; at 36
// cols nothing drawn is wider than the pane. The cap probe calls the real
// EventsTab directly with a minimal element table — the pane only ever hands
// the tab the 12-row fixture, so a longer view reaches the render only this
// way ($.ui.resolve is not available to a plain test).
import { test, expect } from 'claude-code/testing'
import type {
  BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps,
} from 'claude-code'
import { mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './../testkit'
import { EVENTS_MAX, EventsTab, eventsView, eventsViewOf } from './events'
import { MATRIX, PALETTE, ROLE } from './palette'

/** every node of `type` under `n`, in document order. */
function nodesOf(n: unknown, type: string): { type: string; props: Record<string, unknown>; children: unknown[] }[] {
  const out: { type: string; props: Record<string, unknown>; children: unknown[] }[] = []
  const walk = (node: unknown): void => {
    if (typeof node !== 'object' || node === null) return
    const cur = node as { type?: string; props?: Record<string, unknown>; children?: unknown[] }
    if (cur.type === type) out.push({ type: cur.type, props: cur.props ?? {}, children: cur.children ?? [] })
    for (const c of cur.children ?? []) walk(c)
  }
  walk(n)
  return out
}

const textOf = (n: unknown): string => ((n as { children?: unknown[] }).children ?? []).map(c => (typeof c === 'string' ? c : '')).join('')

/** every kit Card in the drawn tree: a round-bordered panel Box. */
const cardsOf = (drawn: unknown): { props: Record<string, unknown> }[] =>
  nodesOf(drawn, 'Box').filter(b => b.props.borderStyle === 'round' && !/^(dash-tab|set-scope)-/.test(String(b.props?.key ?? '')))

/** every type Chip: a tinted Text, in document order. */
const typeChipsOf = (drawn: unknown): { backgroundColor: unknown; children: unknown[] }[] =>
  nodesOf(drawn, 'Text')
    .filter(t => Object.values(ROLE).some(r => r.tint === t.props.backgroundColor))
    .map(t => ({ backgroundColor: t.props.backgroundColor, children: t.children }))

// ---------- a minimal element table: the tags build the same plain-data nodes
// the engine's tables do (children among the props, as h passes them); the
// elements EventsTab never touches are inert stubs ----------

const scalarProps = (props: object): Record<string, string | number | boolean> => {
  const out: Record<string, string | number | boolean> = {}
  for (const [k, v] of Object.entries(props)) {
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') out[k] = v
  }
  return out
}

const isNode = (k: RenderChildren): k is RenderNode =>
  typeof k === 'string' || (typeof k === 'object' && k !== null && !Array.isArray(k))

const kids = (c: RenderChildren | undefined): RenderNode[] | undefined => {
  if (c == null) return undefined
  const arr: readonly RenderChildren[] = Array.isArray(c) ? c : [c]
  return arr.filter(isNode)
}

const boxTag = (props: BoxProps & ElementChildren): RenderElement =>
  ({ type: 'Box', props: scalarProps(props), children: kids(props.children) })
const textTag = (props: TextProps & ElementChildren): RenderElement =>
  ({ type: 'Text', props: scalarProps(props), children: kids(props.children) })
const stub = (): RenderElement => ({ type: 'Text', props: {}, children: [] })

/** enough of the terminal table for EventsTab's destructure — Box and Text real. */
const els: Elements['terminal'] = {
  Box: boxTag,
  Text: textTag,
  Button: stub,
  Input: stub,
  Select: stub,
  Link: stub,
  Code: stub,
  Markdown: stub,
  Client: stub,
  Raster: stub,
  Image: stub,
}

/** every Text node's own text under `n`, in document order (walked the same way as ui.drawn). */
function textOwn(n: unknown): string[] {
  const out: string[] = []
  const walk = (node: unknown): void => {
    if (typeof node !== 'object' || node === null) return
    const cur = node as { type?: string; children?: unknown[] }
    if (cur.type === 'Text' && Array.isArray(cur.children)) {
      const own = cur.children.map(c => (typeof c === 'string' ? c : '')).join('')
      if (own) out.push(own)
    }
    if (Array.isArray(cur.children)) cur.children.forEach(walk)
  }
  walk(n)
  return out
}

// ---------- row widths (review r1 HIGH): a row is a horizontal Box (or a
// Text); its width is the text of every Text descendant — nested Texts
// included — plus the gap columns between its children. A single Text node
// staying under `cols` proves nothing about the row it sits in.

type DrawnNode = { type?: string; props?: { key?: string; flexDirection?: string; gap?: number; borderStyle?: string }; children?: unknown[] }

/** a Text's whole drawn width: its own strings plus any nested Text's strings. */
function textWidth(node: unknown): number {
  if (typeof node === 'string') return node.length
  if (typeof node !== 'object' || node === null) return 0
  const n = node as DrawnNode
  if (n.type !== 'Text') return 0
  return (n.children ?? []).reduce((sum: number, c) => sum + textWidth(c), 0)
}

/** a subtree's width: a row sums its children plus gap columns; a column takes its widest row. */
function widthOf(node: unknown): number {
  if (typeof node !== 'object' || node === null) return 0
  const n = node as DrawnNode
  const kids = n.children ?? []
  if (n.type === 'Text') return textWidth(n)
  if (n.type === 'Box' && n.props?.flexDirection === 'row') {
    const gap = typeof n.props.gap === 'number' ? n.props.gap : 0
    return kids.reduce((sum: number, c) => sum + widthOf(c), 0) + gap * Math.max(0, kids.length - 1)
  }
  return kids.reduce((max: number, c) => Math.max(max, widthOf(c)), 0)
}

/** every row's width in the drawn tree: each Text and each horizontal Box,
 *  with whether it sits inside a kit Card (a round-bordered Box). */
function rowWidths(root: unknown): { width: number; inCard: boolean }[] {
  const out: { width: number; inCard: boolean }[] = []
  const walk = (node: unknown, inCard: boolean): void => {
    if (typeof node !== 'object' || node === null) return
    const n = node as DrawnNode
    const card = inCard || (n.type === 'Box' && n.props?.borderStyle === 'round' && !/^(dash-tab|set-scope)-/.test(String(n.props?.key ?? '')))
    if (n.type === 'Text' || (n.type === 'Box' && n.props?.flexDirection === 'row')) {
      out.push({ width: widthOf(node), inCard: card })
    }
    for (const c of n.children ?? []) walk(c, card)
  }
  walk(root, false)
  return out
}

// ---------- the mounted pane ----------

test('the Events tab draws the Checkout v2 timeline over its fixture', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-events' })
  // the mounted pane draws the live run: no MOCK row, one live event, no footer
  expect(await ui.find({ text: /MOCK · fixture data · not this run/ })).toBeUndefined()
  expect(await ui.find({ text: /EVENTS/ })).toBeTruthy() // the Card's Section header
  expect(await ui.find({ text: /last 1$/ })).toBeTruthy() // the Section's right side
  expect(await ui.find({ text: /F6 r2 gates 6196/ })).toBeTruthy() // the live 17:40 row
  expect(await ui.find({ text: /earlier events/ })).toBeUndefined()
  // the tab itself still draws the Checkout v2 fixture view
  const texts = textOwn(EventsTab(els, eventsView(), 46))
  expect(texts.some(t => /MOCK · fixture data · not this run/.test(t)), 'first row, always').toBe(true)
  expect(texts.some(t => /EVENTS/i.test(t))).toBe(true) // the Card's Section header
  expect(texts.some(t => /last 12/.test(t))).toBe(true) // the Section's right side
  expect(texts.some(t => /P5 r2 sent to opencode glm/.test(t))).toBe(true) // 17:44 row (clipped to the card's width)
  expect(texts.some(t => /worktrees p4 p5 p6 p7/.test(t))).toBe(true) // 17:35 row (its head: the row is clipped with … at 46)
  expect(texts.some(t => /24 earlier events/.test(t))).toBe(true) // the footer row
  expect(texts.some(t => /workflows-v2/.test(t))).toBe(false)
  const flat = texts.join('\n')
  expect(flat.indexOf('P5 r2 sent to opencode glm-5'), 'newest row first').toBeLessThan(flat.indexOf('P3 r1 approved'))
})

test('the event list sits in kit Cards with coloured type chips', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-events' })
  for (const cols of [36, 46] as const) {
    await ui.redraw(paneProps(cols))
    const cards = cardsOf(await ui.drawn())
    expect(cards, `the Events Cards at ${cols}`).toHaveLength(4)
    for (const card of cards) {
      expect(card.props.backgroundColor, `card fill at ${cols}`).toBe(MATRIX.panel) // the pane wears the Matrix palette
      expect(card.props.borderStyle, `round border at ${cols}`).toBe('round')
      expect(card.props.borderColor, `line border at ${cols}`).toBe(MATRIX.line)
    }
  }
  // the type Chips paint by role: review is the live tint, blocked/quota the warn tint
  // (the live run has one 'verdict' event; the fixture view carries all four roles)
  const chips = typeChipsOf(EventsTab(els, eventsView(), 46))
  expect(chips.some(c => c.backgroundColor === ROLE.live.tint), 'a review chip').toBe(true)
  expect(chips.some(c => c.backgroundColor === ROLE.warn.tint), 'a blocked chip').toBe(true)
  expect(chips.some(c => c.backgroundColor === ROLE.ok.tint), 'a merged chip').toBe(true)
})

test('the render caps the timeline at EVENTS_MAX rows however long the view', async () => {
  const long = Array.from({ length: EVENTS_MAX + 10 }, (_, i) => ({ t: '17:44', type: 'review', text: `row-${String(i)}` }))
  const tree = EventsTab(els, eventsViewOf(long), 46)
  const texts = textOwn(tree)
  expect(texts.filter(t => /^row-/.test(t)), 'exactly the cap draws').toHaveLength(EVENTS_MAX)
  expect(texts.some(t => t.includes('row-50')), 'past the cap is dropped').toBe(false)
  expect(texts.some(t => t.includes('MOCK · fixture data')), 'MOCK row still first').toBe(true)
  expect(texts.some(t => t.includes('earlier events')), 'no footer without earlier events').toBe(false)
})

test('the Events tab keeps every drawn ROW within the pane width at 36, 39, 40 and 46 cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-events' })
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: /MOCK · fixture data · not this run/ })).toBeUndefined() // the live pane has no MOCK row
  const narrow = textOwn(EventsTab(els, eventsView(), 36))
  expect(narrow.some(t => /agy ⊘ 19:05/.test(t))).toBe(true) // the Narrow board's short row
  expect(narrow.some(t => /P5 r2 → glm-5/.test(t))).toBe(true) // the Narrow board's short text
  for (const cols of [36, 39, 40, 46] as const) {
    await ui.redraw(paneProps(cols))
    // a row inside the Card is budgeted for its content (cols - 4); the MOCK
    // row and the earlier-count footer outside it owe only the pane's width
    const widths = rowWidths(await ui.drawn())
    expect(widths.length, `the tab drew rows at ${cols}`).toBeGreaterThan(0)
    const wide = widths.filter(w => (w.inCard ? w.width > cols - 4 : w.width > cols))
    expect(wide, `over-wide rows at ${cols} cols: ${JSON.stringify(wide)}`).toEqual([])
    const fixtureWide = rowWidths(EventsTab(els, eventsView(), cols)).filter(w => (w.inCard ? w.width > cols - 4 : w.width > cols))
    expect(fixtureWide, `over-wide fixture rows at ${cols} cols: ${JSON.stringify(fixtureWide)}`).toEqual([])
  }
})
