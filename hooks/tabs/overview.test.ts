// Overview tab tests (plan dashboard-v2-p0b, V1.1): the REAL pane mounted the
// way dashboard.test.ts mounts it — the tab must render the Checkout v2
// fixture (a stub fails every assertion below), hide the Old pane, sit every
// section in its own kit Card, and keep every drawn row within the pane's
// width at 36 cols.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './../testkit'
import { OverviewTab, overviewView } from './overview'
import { PALETTE, ROLE } from './palette'

// ---------- row widths (review r1 HIGH): a row is a horizontal Box (or a
// Text); its width is the text of every Text descendant — nested Texts
// included — plus the gap columns between its children. A single Text node
// staying under `cols` proves nothing about the row it sits in.

type DrawnNode = { type?: string; props?: { flexDirection?: string; gap?: number; borderStyle?: string }; children?: unknown[] }

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
    const card = inCard || (n.type === 'Box' && n.props?.borderStyle === 'round')
    if (n.type === 'Text' || (n.type === 'Box' && n.props?.flexDirection === 'row')) {
      out.push({ width: widthOf(node), inCard: card })
    }
    for (const c of n.children ?? []) walk(c, card)
  }
  walk(root, false)
  return out
}

test('the Overview tab renders the Checkout v2 fixture and hides the Old pane', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-overview' })
  expect(await ui.find({ text: /MOCK · fixture data · not this run/ }), 'the MOCK row stays (C3)').toBeTruthy()
  expect(await ui.find({ text: 'Checkout v2' }), 'run header').toBeTruthy()
  expect(await ui.find({ text: /Payment provider adapter/ }), 'live task row').toBeTruthy()
  expect(await ui.find({ text: /watching P4 R1/ }), 'next line').toBeTruthy()
  expect(await ui.find({ text: /Hold P6 \(recommended\)/ }), 'waiting options').toBeTruthy()
  expect(await ui.find({ text: /workflows-v2/ }), 'the Old pane is gone').toBeUndefined()
})

test('the Overview keeps every drawn ROW within the pane width at 36, 39, 40 and 46 cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-overview' })
  for (const cols of [36, 39, 40, 46] as const) {
    await ui.redraw(paneProps(cols))
    expect(await ui.find({ text: /MOCK · fixture data · not this run/ }), `the MOCK row at ${cols}`).toBeTruthy()
    expect(await ui.find({ text: /watching P4 R1/ }), `the fixture renders at ${cols}`).toBeTruthy()
    // a row inside a Card is budgeted for the Card's content (cols - 4: the
    // border and one padding cell per side); the MOCK row and the header rows
    // outside any Card still owe only the pane's own width
    const rows = rowWidths(await ui.drawn())
    const wide = rows.filter(r => (r.inCard ? r.width > cols - 4 : r.width > cols))
    expect(wide, `over-wide rows at ${cols} cols: ${JSON.stringify(wide)}`).toEqual([])
  }
})

test('the clipped Plan and Lanes rows fill the Card content width exactly', () => {
  // the agy lane's model clips at both widths; the Plan row gets a long title
  // so it clips at both too
  const view: ReturnType<typeof overviewView> = {
    ...overviewView(),
    tasks: [{ id: 'P4', title: 'Payment provider adapter for the whole checkout flow', status: 'building', note: 'building 14m', short: 'build 14m' }],
  }
  for (const cols of [36, 46] as const) {
    const tree = OverviewTab(stubEls(true), view, cols)
    const rows = new Map(nodesOf(tree, 'Box').map(b => [String(b.props.key), b]))
    const planRow = rows.get('P4')
    // the sonnet lane's model clips at 36 (compact words), the agy lane's at 46
    const laneRow = rows.get(cols < 40 ? 'p5' : 'p7')
    expect(planRow, `the P4 row at ${cols}`).toBeDefined()
    expect(laneRow, `the clipping lane row at ${cols}`).toBeDefined()
    // the row lands on the Card's content width, never one cell into the padding
    expect(widthOf(planRow), `the clipped Plan row at ${cols}`).toBe(cols - 4)
    expect(widthOf(laneRow), `the clipped Lanes row at ${cols}`).toBe(cols - 4)
    // and the mark of the cut is drawn
    expect(nodesOf(planRow, 'Text').map(t => ownText(t)).some(t => t.endsWith('…')), `the title clips at ${cols}`).toBe(true)
    expect(nodesOf(laneRow, 'Text').map(t => ownText(t)).some(t => t.endsWith('…')), `the model clips at ${cols}`).toBe(true)
  }
})

test('the Overview usage rows draw real bars and the exhausted marker', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-overview' })
  // the terminal table has a Raster: the 5h bars are Raster cells, not text
  expect(await ui.findAll({ type: 'Raster' }), 'Raster bars').not.toHaveLength(0)
  expect(await ui.find({ text: /38%/ }), 'the 5h percentage').toBeTruthy()
  expect(await ui.find({ text: /exhausted · 19:05/ }), 'the ⊘ exhausted row').toBeTruthy()
})

test('every Overview section sits in its own kit Card at 36 and 46 cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-overview' })
  for (const cols of [36, 46] as const) {
    await ui.redraw(paneProps(cols))
    expect(await ui.find({ text: /MOCK · fixture data · not this run/ }), `the MOCK row at ${cols}`).toBeTruthy()
    // the inline hint is the only other bordered Box and this pane is docked:
    // exactly the five section Cards
    const cards = cardsOf(await ui.drawn())
    expect(cards, `five Cards at ${cols}`).toHaveLength(5)
    for (const card of cards) {
      expect(card.props.backgroundColor, `card fill at ${cols}`).toBe(PALETTE.panel)
      expect([PALETTE.line, ROLE.warn.fg], `card border at ${cols}`).toContain(card.props.borderColor)
    }
    // the Waiting Card is the warn one: solid chip header + the a/b/c options
    expect(await ui.find({ text: /WAITING/ }), `the waiting header at ${cols}`).toBeTruthy()
    expect(await ui.find({ text: /Hold P6/ }), `the recommended option at ${cols}`).toBeTruthy()
  }
  const warnCards = cardsOf(await ui.drawn()).filter(c => c.props.borderColor === ROLE.warn.fg)
  expect(warnCards, 'the Waiting Card carries the warn border').toHaveLength(1)
})

// ---------- the render's element table, driven directly (the pane only ever
// mounts the terminal surface, which has a Raster): a stub table with and
// without Raster pins both bar branches

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
/** a Raster leaf that keeps its props (the engine's own carries a raster stamp). */
const rasterTag = (props: Record<string, unknown>) => ({ type: 'Raster', props: scalarProps(props), children: [] })

/** the table the tab destructures, Box/Text real, Button keeps its props
 *  verbatim (a press reads onPress off them), Raster optional — the key is
 *  absent without one, so `'Raster' in els` is false. */
const stubEls = (withRaster: boolean): Elements[keyof Elements] => ({
  Box: boxTag,
  Text: textTag,
  Button: (props: Record<string, unknown>) => ({ type: 'Button', props, children: [] }),
  ...(withRaster ? { Raster: rasterTag } : {}),
  Input: () => ({ type: 'Text', props: {}, children: [] }),
  Select: () => ({ type: 'Text', props: {}, children: [] }),
  Link: () => ({ type: 'Text', props: {}, children: [] }),
  Code: () => ({ type: 'Text', props: {}, children: [] }),
  Markdown: () => ({ type: 'Text', props: {}, children: [] }),
  Client: () => ({ type: 'Text', props: {}, children: [] }),
  Image: () => ({ type: 'Text', props: {}, children: [] }),
} as unknown as Elements[keyof Elements])

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

/** every kit Card in the drawn tree: a round-bordered panel Box. */
const cardsOf = (drawn: unknown): { props: Record<string, unknown> }[] =>
  nodesOf(drawn, 'Box').filter(b => b.props.borderStyle === 'round')

const ownText = (n: unknown): string => {
  if (typeof n === 'string') return n
  if (typeof n !== 'object' || n === null) return ''
  const node = n as { type?: string; children?: unknown[] }
  if (node.type !== 'Text') return ''
  return (node.children ?? []).map(ownText).join('')
}

/** every drawn Text's own text under `n`, joined. */
const allText = (n: unknown): string => nodesOf(n, 'Text').map(t => ownText(t)).join('\n')

/** every Button in the drawn tree. */
const buttonsOf = (n: unknown): { props: Record<string, unknown> }[] => nodesOf(n, 'Button')

test('the Usage rows use the Raster bar when the table has one, the SmoothBar otherwise', () => {
  const withRaster = OverviewTab(stubEls(true), overviewView(), 46)
  // the Plan card's strip is a Raster too; the usage bars are the keyed ones
  const bars = nodesOf(withRaster, 'Raster').filter(r => String(r.props.key).startsWith('overview-usage-'))
  expect(bars, 'one 5h Raster per usage row').toHaveLength(4)
  const width = Number(bars[0]?.props.columns)
  expect(width, 'the bar is columns wide').toBeGreaterThan(0)
  expect(typeof bars[0]?.props.cells, 'encoded cells').toBe('string')

  const without = OverviewTab(stubEls(false), overviewView(), 46)
  expect(nodesOf(without, 'Raster'), 'no Raster without one in the table').toHaveLength(0)
  // the fallback draws the eighth-block bar at the very same width
  const fills = nodesOf(without, 'Text').map(t => ownText(t)).filter(t => /█/.test(t))
  expect(fills, 'a SmoothBar per usage row').toHaveLength(4)
  for (const fill of fills) expect(fill.length, 'fallback width matches the Raster').toBe(width)
})

test('the Waiting options are a/b/c Buttons; the pick and the ⋯ reveal are view state only', () => {
  const map = new Map<string, string>()
  const wide = OverviewTab(stubEls(true), overviewView(), 46, map)
  const buttons = nodesOf(wide, 'Button')
  // at 46 the row folds: the primary stays, the rest collapse into ⋯ More
  expect(buttons.map(b => String(b.props.label)), 'the folded row').toEqual(['a Hold P6 (recommended)', '⋯ More'])
  expect(buttons[0]?.props.hotkey, 'the primary hotkey').toBe('a')
  expect(buttons[1]?.props.plain, 'the fold button is plain').toBe(true)

  const press = (key: string): void => {
    const b = buttons.find(x => String(x.props.key) === key)
    expect(b, `the ${key} button`).toBeDefined()
    ;(b?.props.onPress as () => void)()
  }
  press('waiting-opt-a')
  expect(map.get('overview:waiting:pick'), 'the pick lands in view state').toBe('a')
  // the keys are namespaced: the Waiting tab's own view-state keys stay untouched
  expect(map.get('waiting:pick'), 'the Waiting tab pick key is not ours').toBeUndefined()
  expect(map.get('waiting:more'), 'the Waiting tab reveal key is not ours').toBeUndefined()
  press('waiting-opt-a-more-2')
  expect(map.get('overview:waiting:more'), 'the reveal lands in view state').toBe('1')
  expect(map.get('waiting:more'), 'still not the Waiting tab key').toBeUndefined()
  const revealed = allText(OverviewTab(stubEls(true), overviewView(), 46, map))
  expect(revealed.includes('b Start on main, rebase later'), 'the folded option is revealed').toBe(true)
  expect(revealed.includes('c Fold into P4 lane'), 'the other folded option is revealed').toBe(true)

  // the Overview shows the pick: that option's button takes the primary variant
  const afterPick = buttonsOf(OverviewTab(stubEls(true), overviewView(), 46, map)).find(b => String(b.props.key) === 'waiting-opt-a')
  expect(afterPick?.props.variant, 'the picked option is primary').toBe('primary')

  // a wide pane fits all three options, each with its own hotkey
  const roomy = buttonsOf(OverviewTab(stubEls(true), overviewView(), 100, map)).filter(b => String(b.props.key).startsWith('waiting-opt-'))
  expect(roomy.map(b => String(b.props.hotkey)), 'a b c').toEqual(['a', 'b', 'c'])
  ;(roomy[1]?.props.onPress as () => void)()
  expect(map.get('overview:waiting:pick'), 'pressing b records b').toBe('b')
  const pickedB = buttonsOf(OverviewTab(stubEls(true), overviewView(), 100, map)).find(b => String(b.props.key) === 'waiting-opt-b')
  expect(pickedB?.props.variant, 'the Overview shows the b pick').toBe('primary')
})
