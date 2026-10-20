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
import type { EventItem } from './events'
import { MATRIX, PALETTE, ROLE, withTheme } from './palette'
import { EVENT_FULL_MAX, fullOf } from './../dashboard'

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
/** a Button draws its label as a Text: a plain row press is the row's text, and the width probes read it */
const buttonTag = (props: Parameters<Elements['terminal']['Button']>[0]): RenderElement =>
  ({ type: 'Text', props: {}, children: [props.label ?? ''] })

/** enough of the terminal table for EventsTab's destructure — Box and Text real, Button its label as a Text. */
const els: Elements['terminal'] = {
  Box: boxTag,
  Text: textTag,
  Button: buttonTag,
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
    // the live run has no lane-attributed events, so no LANE ACTIVITY card (it no longer borrows the fixture's lanes)
    const live = cardsOf(await ui.drawn())
    expect(live, `the live Events Cards at ${cols}`).toHaveLength(3)
    // the fixture view carries lane activity: BUS, FILTERS, EVENTS and LANE ACTIVITY (drawn in the Matrix theme, as the pane wears it)
    const fixture = withTheme('matrix', () => cardsOf(EventsTab(els, eventsView(), cols)))
    expect(fixture, `the fixture Events Cards at ${cols}`).toHaveLength(4)
    for (const card of [...live, ...fixture]) {
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

test('the lane filter is a dropdown like Kind and View, and no row carries a selection notch', () => {
  const events = [
    { t: '06:01', type: 'note', text: 'one', lane: 'K1' },
    { t: '06:00', type: 'info', text: 'two', lane: 'K1.1' },
  ]
  const state = new Map<string, string>()
  const ui = { get: (k: string) => state.get(k), set: (k: string, v: string) => { state.set(k, v) } }
  const buttonEls = { ...els, Button: (props: Parameters<Elements['terminal']['Button']>[0]): RenderElement => ({ type: 'Button', props: { key: props.key ?? '', label: props.label ?? '' }, children: [] }) as unknown as RenderElement }
  const buttons = (tree: unknown) => nodesOf(tree, 'Button').map(b => String(b.props.label))
  const closed = EventsTab(buttonEls, eventsViewOf(events), 46, ui)
  expect(buttons(closed), 'a closed dropdown shows its one button').toContain('all lanes ▾')
  expect(buttons(closed).some(l => /\( \)|\(●\)/.test(l) && /K1/.test(l)), 'no lane options until it opens').toBe(false)
  state.set('events:open', 'lane')
  const open = buttons(EventsTab(buttonEls, eventsViewOf(events), 46, ui))
  expect(open).toContain('(●) all lanes')
  expect(open).toContain('( ) K1')
  expect(open).toContain('( ) K1.1')
  // picking a lane filters the list; the notch '▌' is gone from every row
  state.set('events:lane', 'K1.1')
  const pickedTree = EventsTab(buttonEls, eventsViewOf(events), 46, ui)
  const picked = [...textOwn(pickedTree), ...buttons(pickedTree)] // a row's text is its press: a Button label
  expect(picked.some(t => t === 'two')).toBe(true)
  expect(picked.some(t => t === 'one')).toBe(false)
  const noNotch = EventsTab(buttonEls, eventsViewOf(events), 46)
  expect(textOwn(noNotch).includes('▌'), 'no selection notch').toBe(false)
  expect(buttons(noNotch).some(l => l.includes('▌')), 'no selection notch on a row button either').toBe(false)
})

// ---------- the row detail card (`events:detail`) ----------

const DETAIL_EVENTS: readonly EventItem[] = [
  { id: 'm-2', src: 'bus', t: '06:02', kind: 'answer', who: 'p2→orch', lane: 'p2', text: 'use the cache', reply_to: 'm-1', ack: 1, short: { type: 'ans', text: 'cache' } },
  { id: 'm-1', src: 'bus', t: '06:01', kind: 'ask', who: 'orch→p2', lane: 'p2', text: 'cache or recompute?' },
  { id: 'r-9', src: 'run', t: '06:00', kind: 'run', text: 'run started' },
]

/** a fake view-state handle (every set recorded) and an element table whose Buttons keep key, label and onPress */
function pressable(initial: Record<string, string> = {}) {
  const state = new Map(Object.entries(initial))
  const sets: [string, string][] = []
  const ui = { get: (k: string) => state.get(k), set: (k: string, v: string) => { sets.push([k, v]); state.set(k, v) } }
  const pressEls = {
    ...els,
    Button: (props: Parameters<Elements['terminal']['Button']>[0]): RenderElement =>
      ({ type: 'Button', props: { key: props.key ?? '', label: props.label ?? '', onPress: props.onPress }, children: [] }) as unknown as RenderElement,
  }
  const press = (tree: unknown, key: string): void => {
    const b = nodesOf(tree, 'Button').find(x => x.props.key === key)
    expect(b, `button ${key}`).toBeDefined()
    ;(b?.props.onPress as () => void)()
  }
  return { state, sets, ui, pressEls, press }
}

const rowKeys = (tree: unknown): string[] => nodesOf(tree, 'Button').map(b => String(b.props.key)).filter(k => k.startsWith('ev-open-'))

test('pressing an Events row opens its detail card, pressing it again closes it', () => {
  for (const cols of [46, 36] as const) {
    const { state, sets, ui, pressEls, press } = pressable()
    const list = EventsTab(pressEls, eventsViewOf(DETAIL_EVENTS), cols, ui)
    expect(rowKeys(list), `every row is pressable at ${cols}`).toEqual(['ev-open-m-2', 'ev-open-m-1', 'ev-open-r-9'])
    press(list, 'ev-open-m-1')
    expect(sets, `a press opens the row at ${cols}`).toEqual([['events:detail', 'm-1']])
    // the row is open now (ui.get names it): the same press, from the list drawn before, closes it
    state.set('events:detail', 'm-1')
    press(list, 'ev-open-m-1')
    expect(sets.at(-1), `a second press closes at ${cols}`).toEqual(['events:detail', ''])
    // another row while one is open switches to it
    press(list, 'ev-open-r-9')
    expect(sets.at(-1), `another row switches at ${cols}`).toEqual(['events:detail', 'r-9'])
  }
})

test('with a row open the detail card replaces the list; BUS, filters and lane activity stay', () => {
  for (const cols of [46, 36] as const) {
    const { sets, ui, pressEls, press } = pressable({ 'events:detail': 'm-1' })
    const tree = EventsTab(pressEls, eventsViewOf(DETAIL_EVENTS), cols, ui)
    const texts = [...textOwn(tree), ...nodesOf(tree, 'Button').map(b => String(b.props.label))]
    expect(rowKeys(tree), `no list row at ${cols}`).toEqual([])
    expect(texts.some(t => /^EVENTS$/i.test(t)), `no list header at ${cols}`).toBe(false)
    expect(texts.some(t => /shown$/.test(t)), `no list count line at ${cols}`).toBe(false)
    expect(texts.some(t => t === 'm-1 · ask'), `the detail header at ${cols}`).toBe(true)
    expect(texts.some(t => t === 'cache or recompute?'), `the detail text at ${cols}`).toBe(true)
    expect(texts.some(t => /BUS/.test(t)), `the BUS card stays at ${cols}`).toBe(true)
    expect(texts.some(t => /LANE ACTIVITY/.test(t)), `the lane activity card stays at ${cols}`).toBe(true)
    const keys = nodesOf(tree, 'Button').map(b => String(b.props.key))
    expect(keys, `the filters stay at ${cols}`).toEqual(expect.arrayContaining(['ev-src-all', 'ev-kind-dd', 'ev-view-dd', 'ev-lane-dd', 'ev-pause-btn']))
    // both close buttons clear the key
    press(tree, 'ev-detail-close-top-m-1')
    press(tree, 'ev-detail-close-m-1')
    expect(sets, `both closes clear at ${cols}`).toEqual([['events:detail', ''], ['events:detail', '']])
  }
})

test('an open id the view does not carry draws the list', () => {
  const unknown = pressable({ 'events:detail': 'nope' })
  const list = EventsTab(unknown.pressEls, eventsViewOf(DETAIL_EVENTS), 46, unknown.ui)
  expect(rowKeys(list)).toHaveLength(3)
  expect(nodesOf(list, 'Button').some(b => String(b.props.key).startsWith('ev-detail-close')), 'no detail card').toBe(false)
})

test('a filter or search that hides the open row does not close its card', () => {
  // m-2 is a bus answer on lane p2 saying `use the cache`: each of these filters leaves it out of the list
  const hides: Record<string, string>[] = [{ 'events:source': 'run' }, { 'events:kinds': 'ask' }, { 'events:q': 'recompute' }, { 'events:lane': 'p9' }, { 'events:view': 'needs' }]
  for (const hide of hides) {
    const label = JSON.stringify(hide)
    const hidden = pressable(hide)
    expect(rowKeys(EventsTab(hidden.pressEls, eventsViewOf(DETAIL_EVENTS), 46, hidden.ui)), `${label} hides m-2 from the list`).not.toContain('ev-open-m-2')
    const open = pressable({ 'events:detail': 'm-2', ...hide })
    const tree = EventsTab(open.pressEls, eventsViewOf(DETAIL_EVENTS), 46, open.ui)
    expect(rowKeys(tree), `no list row under ${label}`).toEqual([])
    expect(textOwn(tree).some(t => t === 'm-2 · answer'), `the card stays under ${label}`).toBe(true)
  }
})

test('a row keeps its time, chip, who, text, ack and problem marker once pressable', () => {
  const events: EventItem[] = [{ id: 'm-3', src: 'bus', t: '06:03', kind: 'info', who: 'p1→orch', text: 'heads up', ack: 2, bad: true }]
  const { pressEls, ui } = pressable()
  const tree = EventsTab(pressEls, eventsViewOf(events), 80, ui)
  const row = nodesOf(tree, 'Box').find(b => String(b.props.key) === 'ev-row-m-3')
  expect(row, 'the row Box').toBeDefined()
  const cells = textOwn(row)
  expect(cells).toEqual(expect.arrayContaining(['!', '06:03', 'p1→orch', '✓✓']))
  expect(nodesOf(row, 'Button').map(b => b.props.label)).toEqual(['heads up'])
})

// ---------- the store rows -> EventItem conversion (hooks/dashboard.tsx refreshEvents) ----------

test('a stored row reaches the view with its date, from, to, task, chan, author and capped full text, and opens in the detail card', { plugins: [worldFixtures], timeoutMs: 15000 }, async ($, on) => {
  const long = 'abcdefghij'.repeat(300) // 3000 chars: past the list cap (120) and the detail cap (1500)
  const rows = [
    { id: 11, t: '2026-10-07T17:50:00+02:00', chan: 'bus', kind: 'ask', from_id: 'p3', to_id: 'orch', reply_to: null, task: 'F6', text: 'cache or recompute?\r\nsecond\tline\u0007', meta_json: JSON.stringify({ by: 'builder-3', delivered: true }) },
    { id: 12, t: '2026-10-07T17:51:00+02:00', chan: 'bus', kind: 'answer', from_id: 'orch', to_id: 'p3', reply_to: 'm-11', task: 'F6', text: long, meta_json: JSON.stringify({ delivered: true }) },
    { id: 13, t: '2026-10-07T17:52:00+02:00', chan: 'run', kind: 'run', text: 'run note', meta_json: '' },
  ]
  mockFs(on, worldFiles())
  const clock = mockWorld(on, {
    process: argv => argv.some(a => a.endsWith('mkdb.py')) && argv.includes('events')
      ? { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, events: rows }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      : undefined,
  })
  let stored: Record<string, string> = {}
  on('state.set', (_$, e, next) => {
    if (e.key === 'viewState' && e.value) stored = e.value as Record<string, string>
    return next(e)
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(80) })
  await ui.press({ key: 'dash-tab-events' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  await clock.advance(5000)
  for (let i = 0; i < 10; i++) await clock.advance(0)

  // 1. the stored view: the new fields travel, the list text stays flattened and capped at 120
  const items = (JSON.parse(stored['events:list'] ?? '{}') as { events?: EventItem[] }).events ?? []
  const byId = new Map(items.map(e => [e.id, e]))
  const ask = byId.get('m-11')
  const answer = byId.get('m-12')
  const note = byId.get('r-13')
  expect(items.map(e => e.id), 'newest first').toEqual(['r-13', 'm-12', 'm-11'])
  expect(ask).toMatchObject({ date: '2026-10-07T17:50:00+02:00', from: 'p3', to: 'orch', task: 'F6', chan: 'bus', by: 'builder-3', src: 'bus' })
  expect(ask?.full, '\\r\\n folded to \\n, a tab is a space, other control chars dropped').toBe('cache or recompute?\nsecond line')
  expect(ask?.text, 'the list text is flattened').toBe('cache or recompute? second line')
  expect(answer?.chan).toBe('bus')
  expect(answer?.text.length, 'the list text keeps its 120 cap').toBe(120)
  expect(answer?.full).toBe(`${long.slice(0, 1500)}\n… 1500 more`)
  expect(note?.chan).toBe('run')
  for (const k of ['from', 'to', 'task', 'by'] as const) expect(note?.[k], `a run row without ${k} leaves it out`).toBeUndefined()

  // 2. the row opens its detail card in place of the list, the card reads those fields, and ✕ close brings the list back
  await ui.redraw(paneProps(80))
  expect(await ui.find({ type: 'Button', key: 'ev-open-m-12' }), 'the answer row is pressable').toBeDefined()
  await ui.press({ key: 'ev-open-m-12' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  const drawn = await ui.drawn()
  const texts = textOwn(drawn)
  expect(texts.some(t => t === 'm-12 · answer'), 'the detail header').toBe(true)
  expect(texts.some(t => t === '2026-10-07 17:51:00+02:00'), 'when').toBe(true)
  expect(texts.some(t => t === 'orch → p3'), 'from → to').toBe(true)
  expect(texts.some(t => t === 'answers m-11'), 'the related ask').toBe(true)
  expect(texts.filter(t => t.startsWith('abcdefghij')), 'the kept 1500').toEqual([long.slice(0, 1500)])
  expect(texts.filter(t => /^… \d+ more$/.test(t)), 'the tail, on its own line').toEqual(['… 1500 more'])
  expect(await ui.find({ type: 'Button', key: 'ev-open-m-11' }), 'the list is not drawn').toBeUndefined()
  await ui.press({ key: 'ev-detail-close-top-m-12' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  expect(await ui.find({ type: 'Button', key: 'ev-open-m-11' }), 'closed: the list is back').toBeDefined()
})

// ---------- fullOf: the detail card's text cap ----------

test('fullOf keeps a text of exactly 1500 characters whole and cuts a longer one with a `… n more` tail', () => {
  expect(EVENT_FULL_MAX).toBe(1500)
  expect(fullOf('')).toBe('')
  expect(fullOf('a'.repeat(1499))).toBe('a'.repeat(1499))
  expect(fullOf('a'.repeat(1500)), 'exactly the cap: whole, no tail').toBe('a'.repeat(1500))
  expect(fullOf('a'.repeat(1501)), 'one over: the cap, then the tail').toBe(`${'a'.repeat(1500)}\n… 1 more`)
  expect(fullOf('a'.repeat(3000))).toBe(`${'a'.repeat(1500)}\n… 1500 more`)
})

test('fullOf never cuts inside a surrogate pair', () => {
  const pair = '😀' // two UTF-16 units
  const lone = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/
  // the pair straddles the cut (high at 1499, low at 1500): the cut moves back one unit and `n` counts the dropped units
  const straddle = `${'a'.repeat(1499)}${pair}tail`
  expect(fullOf(straddle)).toBe(`${'a'.repeat(1499)}\n… ${straddle.length - 1499} more`)
  expect(lone.test(fullOf(straddle)), 'no lone surrogate').toBe(false)
  // the pair ends the text at 1501 units: still moved back, 2 dropped
  expect(fullOf(`${'a'.repeat(1499)}${pair}`)).toBe(`${'a'.repeat(1499)}\n… 2 more`)
  // a pair wholly inside the cap is kept
  const inside = `${'a'.repeat(1498)}${pair}tail`
  expect(fullOf(inside)).toBe(`${inside.slice(0, 1500)}\n… ${inside.length - 1500} more`)
  expect(lone.test(fullOf(inside)), 'no lone surrogate').toBe(false)
})

test('fullOf folds CRLF, turns a tab or a lone CR into a space and drops other control characters', () => {
  expect(fullOf('one\r\ntwo\r\n\r\nfour')).toBe('one\ntwo\n\nfour')
  expect(fullOf('second\tline')).toBe('second line')
  expect(fullOf('a\rb')).toBe('a b')
  expect(fullOf('a\nb')).toBe('a\nb')
  expect(fullOf('a\u0007b\u0000c\u001bd\u007fe')).toBe('abcde')
})

// ---------- the pause button (`ev-pause-btn`), the count row and the footer ----------

/** the row Box holding the pause button, and its drawn width: a Text is its string, a Button its label, plus the gap columns */
function pauseRow(tree: unknown): { width: number; label: string } {
  const row = nodesOf(tree, 'Box').find(b => (b.children as { type?: string; props?: Record<string, unknown> }[]).some(c => c.type === 'Button' && c.props?.key === 'ev-pause-btn'))
  expect(row, 'the search row').toBeDefined()
  const parts = (row?.children ?? []) as { type?: string; props?: Record<string, unknown>; children?: unknown[] }[]
  const w = parts.reduce((sum, c) => sum + (c.type === 'Button' ? String(c.props?.label).length : textWidth(c)), 0)
  const gap = typeof row?.props.gap === 'number' ? row.props.gap : 0
  const btn = parts.find(c => c.type === 'Button')
  return { width: w + gap * Math.max(0, parts.length - 1), label: String(btn?.props?.label) }
}

/** the Events card's count row: the Box whose right Text is `live` or `paused` */
function countRow(tree: unknown): { left: string; right: string; width: number } {
  const row = nodesOf(tree, 'Box').find(b => {
    const t = textOwn(b)
    return b.props.flexDirection === 'row' && t.length === 2 && /shown/.test(t[0] ?? '') && /^(live|paused)$/.test(t[1] ?? '')
  })
  expect(row, 'the count row').toBeDefined()
  const [left = '', right = ''] = textOwn(row)
  return { left, right, width: left.length + right.length + 1 } // space-between keeps at least one column apart
}

/** the footer row: `↵ open` on the left, the `j k` keys on the right */
function footerRow(tree: unknown): string[] {
  const row = nodesOf(tree, 'Box').filter(b => b.props.flexDirection === 'row' && textOwn(b).some(t => /^j k/.test(t))).at(-1)
  expect(row, 'the footer row').toBeDefined()
  return textOwn(row)
}

test('the pause button toggles events:paused and events:pausedAt, reads `‖ pause` while live and `▶ play` while paused', () => {
  for (const cols of [46, 36] as const) {
    const live = pressable()
    const liveTree = EventsTab(live.pressEls, eventsViewOf(DETAIL_EVENTS), cols, live.ui)
    expect(pauseRow(liveTree).label, `live shows the pause word at ${cols}`).toBe('‖ pause')
    live.press(liveTree, 'ev-pause-btn')
    expect(live.sets.map(s => s[0]), `pausing stamps the time then flips the flag at ${cols}`).toEqual(['events:pausedAt', 'events:paused'])
    expect(live.sets[1], `paused at ${cols}`).toEqual(['events:paused', '1'])
    expect(Number(live.sets[0]?.[1])).toBeGreaterThan(0)

    const held = pressable({ 'events:paused': '1', 'events:pausedAt': '1' })
    const heldTree = EventsTab(held.pressEls, eventsViewOf(DETAIL_EVENTS), cols, held.ui)
    expect(pauseRow(heldTree).label, `paused shows the play word at ${cols}`).toBe('▶ play')
    held.press(heldTree, 'ev-pause-btn')
    expect(held.sets, `resuming only clears the flag at ${cols}`).toEqual([['events:paused', '0']])
  }
})

test('the search row, the count row and the footer stay within the pane at 46, 36 and 56 cols, live and paused', () => {
  for (const cols of [46, 36, 56] as const) {
    const states: Record<string, string>[] = [{}, { 'events:paused': '1' }, { 'events:q': 'a very long search text that cannot fit', 'events:paused': '1' }]
    for (const initial of states) {
      const { pressEls, ui } = pressable(initial)
      const tree = EventsTab(pressEls, eventsViewOf(DETAIL_EVENTS), cols, ui)
      const where = `at ${cols} ${JSON.stringify(initial)}`
      expect(pauseRow(tree).width, `the search row ${where}`).toBeLessThanOrEqual(cols - 4)
      const count = countRow(tree)
      expect(count.width, `the count row ${where}`).toBeLessThanOrEqual(cols - 4)
      expect(count.right, `the state word survives ${where}`).toBe(initial['events:paused'] === '1' ? 'paused' : 'live')
      const foot = footerRow(tree)
      expect(foot.reduce((n, t) => n + t.length, 0) + 1, `the footer ${where}`).toBeLessThanOrEqual(cols)
    }
  }
})

test('the ticks legend joins the count row only when the whole of it fits beside the state word', () => {
  const LEGEND = ' · ✓ delivered · ✓✓ read'
  const rowAt = (cols: number, initial: Record<string, string>, events: readonly EventItem[] = DETAIL_EVENTS) => {
    const { pressEls, ui } = pressable(initial)
    return countRow(EventsTab(pressEls, eventsViewOf(events), cols, ui))
  }
  // fits: the full legend, live and paused (3 of 3 shown + legend = 36 columns; the budget is cols - 4 - state word - 1)
  const both: Record<string, string>[] = [{}, { 'events:paused': '1' }]
  for (const initial of both) {
    const row = rowAt(56, initial)
    expect(row.left, `full legend at 56 ${JSON.stringify(initial)}`).toBe(`3 of 3 shown${LEGEND}`)
    expect(row.right).toBe(initial['events:paused'] === '1' ? 'paused' : 'live')
  }
  // the exact boundary: paused needs cols - 4 - 6 - 1 >= 36 (cols 47), live needs cols - 4 - 4 - 1 >= 36 (cols 45)
  expect(rowAt(47, { 'events:paused': '1' }).left).toBe(`3 of 3 shown${LEGEND}`)
  expect(rowAt(46, { 'events:paused': '1' }).left).toBe('3 of 3 shown')
  expect(rowAt(45, {}).left).toBe(`3 of 3 shown${LEGEND}`)
  expect(rowAt(44, {}).left).toBe('3 of 3 shown')
  // does not fit: the bare count, no tick, no ellipsis, and the state word still there
  for (const [cols, initial] of [[44, { 'events:paused': '1' }], [46, { 'events:paused': '1' }], [44, {}], [40, {}]] as const) {
    const row = rowAt(cols, initial)
    expect(row.left, `bare count at ${cols} ${JSON.stringify(initial)}`).toBe('3 of 3 shown')
    expect(row.left).not.toMatch(/✓|…/)
    expect(row.right).toBe('paused' in initial || 'events:paused' in initial ? 'paused' : 'live')
    expect(row.width).toBeLessThanOrEqual(cols - 4)
  }
  // a large count: 50 of 1234 shown + legend = 40 columns
  const many: EventItem[] = Array.from({ length: 1234 }, (_, i) => ({ id: `b-${i}`, src: 'run', t: '06:00', kind: 'run', text: `row ${i}` }))
  expect(rowAt(56, { 'events:paused': '1' }, many).left).toBe(`50 of 1234 shown${LEGEND}`)
  for (const [cols, initial] of [[48, {}], [50, { 'events:paused': '1' }], [46, {}]] as const) {
    const row = rowAt(cols, initial, many)
    expect(row.left, `bare large count at ${cols}`).toBe('50 of 1234 shown')
    expect(row.left).not.toMatch(/✓|…/)
    expect(row.right).toBe('events:paused' in initial ? 'paused' : 'live')
    expect(row.width).toBeLessThanOrEqual(cols - 4)
  }
  // compact never carries the legend
  expect(rowAt(36, {}).left).toBe('3/3 shown')
  // and the footer holds no legend at any width
  for (const cols of [60, 46, 36] as const) {
    const { pressEls, ui } = pressable()
    const tree = EventsTab(pressEls, eventsViewOf(DETAIL_EVENTS), cols, ui)
    expect(footerRow(tree), `the footer at ${cols}`).toEqual(['↵ open', cols < 56 ? 'j k' : 'j k /'])
  }
})
