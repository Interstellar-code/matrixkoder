// Real-code tests for the Plan tab (dashboard-v2-p0, T1.2; V1.2): mount the real
// pane, press the tab, read what it drew — the MOCK row, the Checkout v2
// fixture strings Old never draws, the merged-vs-done glyph split (the plan's
// acceptance line), one kit Card per wave, the fold/expand through the view
// state, and the under-NARROW fit (no Text node wider than cols).
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './../testkit'
import { PlanTab, planView } from './plan'
import { PALETTE, ROLE } from './palette'

/** every Text node's own text under `n`, in document order (dashboard.test.ts's walk). */
function rowTexts(n: unknown): string[] {
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

/** every drawn Text's own text under `n`, joined. */
const allText = (n: unknown): string => nodesOf(n, 'Text').map(t => (t.children ?? []).map(c => (typeof c === 'string' ? c : '')).join('')).join('\n')

/** every kit Card in the drawn tree: a round-bordered panel Box. */
const cardsOf = (drawn: unknown): { props: Record<string, unknown> }[] =>
  nodesOf(drawn, 'Box').filter(b => b.props.borderStyle === 'round')

/** every Button in the drawn tree. */
const buttonsOf = (drawn: unknown): { props: Record<string, unknown> }[] => nodesOf(drawn, 'Button')

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

test('Plan tab: MOCK row, the Checkout v2 fixture, Old gone (46 cols)', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-plan' })
  expect(await ui.find({ text: /MOCK · fixture data · not this run/ })).toBeTruthy()
  expect(await ui.find({ text: /Cart schema migration/ }), 'a W1 merged row').toBeTruthy()
  expect(await ui.find({ text: /E2E checkout QA/ }), 'the W3 planned row').toBeTruthy()
  expect(await ui.find({ text: /workflows-v2/ }), 'Old content hidden').toBeUndefined()
})

test('Plan tab: merged and done draw DISTINCT glyphs (the acceptance line)', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-plan' })
  // the phase row's pressable label starts with the status glyph
  const merged = await ui.find({ key: 'plan-fold-P1' }) // a W1 merged task
  const done = await ui.find({ key: 'plan-fold-P0' }) // the fixture's one done task
  expect(merged, 'the merged row drawn').toBeDefined()
  expect(done, 'the done row drawn').toBeDefined()
  const mergedLabel = String((merged as { props: { label?: string } }).props.label ?? '')
  const doneLabel = String((done as { props: { label?: string } }).props.label ?? '')
  expect(mergedLabel.startsWith('◈'), `merged ◈: ${mergedLabel}`).toBe(true) // never aliased
  expect(doneLabel.startsWith('✓'), `done ✓: ${doneLabel}`).toBe(true)
  expect(mergedLabel).not.toContain('✓')
  expect(doneLabel).not.toContain('◈')
})

test('Plan tab: every drawn ROW fits the pane at 36, 39, 40 and 46 cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-plan' })
  for (const cols of [36, 39, 40, 46] as const) {
    await ui.redraw(paneProps(cols))
    expect(await ui.find({ text: /MOCK · fixture data · not this run/ }), `the MOCK row at ${cols}`).toBeTruthy()
    // a row inside a wave Card is budgeted for the Card's content (cols - 4);
    // the MOCK row and the footer outside every Card owe only the pane's width
    const widths = rowWidths(await ui.drawn())
    expect(widths.length, `the pane drew rows at ${cols}`).toBeGreaterThan(0)
    const wide = widths.filter(w => (w.inCard ? w.width > cols - 4 : w.width > cols))
    expect(wide, `over-wide rows at ${cols} cols: ${JSON.stringify(wide)}`).toEqual([])
  }
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: /Cart schema/ }), 'the short W1 title').toBeTruthy()
})

test('Plan tab: each wave is its own kit Card, the live wave bordered in the live role', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-plan' })
  for (const cols of [36, 46] as const) {
    await ui.redraw(paneProps(cols))
    const cards = cardsOf(await ui.drawn())
    expect(cards, `one Card per wave at ${cols}`).toHaveLength(3)
    for (const card of cards) expect(card.props.backgroundColor, `card fill at ${cols}`).toBe(PALETTE.panel)
    const live = cards.filter(c => c.props.borderColor === ROLE.live.fg)
    expect(live, `the live wave's border at ${cols}`).toHaveLength(1)
    expect(cards.filter(c => c.props.borderColor === PALETTE.line), `the other waves keep the line border at ${cols}`).toHaveLength(2)
    // the header chips: ✓ merged (ok), ● live (live), ○ planned (idle) — the
    // Narrow board's W3 chip collapses to the bare glyph
    expect(await ui.find({ text: /merged 16:52/ }), `the W1 chip at ${cols}`).toBeTruthy()
    expect(await ui.find({ text: /2 live · 2 held/ }), `the W2 chip at ${cols}`).toBeTruthy()
    if (cols < 40) expect(await ui.find({ text: ' ○ ' }), `the W3 short chip at ${cols}`).toBeTruthy()
    else expect(await ui.find({ text: /planned/ }), `the W3 chip at ${cols}`).toBeTruthy()
  }
})

// ---------- the fold/expand through the view state (C12), driven directly:
// a fake `ui` (a Map — structurally the TabUi each tab declares), then the
// real Pane mount

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

/** the table the tab destructures; Button keeps its props verbatim so a press
 *  reads onPress off them. */
const stubEls: Elements[keyof Elements] = {
  Box: boxTag,
  Text: textTag,
  Button: (props: Record<string, unknown>) => ({ type: 'Button', props, children: [] }),
  Input: () => ({ type: 'Text', props: {}, children: [] }),
  Select: () => ({ type: 'Text', props: {}, children: [] }),
  Link: () => ({ type: 'Text', props: {}, children: [] }),
  Code: () => ({ type: 'Text', props: {}, children: [] }),
  Markdown: () => ({ type: 'Text', props: {}, children: [] }),
  Client: () => ({ type: 'Text', props: {}, children: [] }),
  Raster: (props: Record<string, unknown>) => ({ type: 'Raster', props: scalarProps(props), children: [] }),
  Image: () => ({ type: 'Text', props: {}, children: [] }),
} as unknown as Elements[keyof Elements]

test('Plan tab: pressing a phase row toggles its subtasks through a fake ui; TreeRow prefixes present', () => {
  const ui = new Map<string, string>()
  // the default view state holds nothing: every phase is folded
  expect(allText(PlanTab(stubEls, planView(), 46, ui)).includes('├'), 'nothing folded at default').toBe(false)
  expect(buttonsOf(PlanTab(stubEls, planView(), 46, ui)).map(b => String(b.props.key))).toContain('plan-fold-P4')

  const press = (key: string): void => {
    const b = buttonsOf(PlanTab(stubEls, planView(), 46, ui)).find(x => String(x.props.key) === key)
    expect(b, `the ${key} button`).toBeDefined()
    ;(b?.props.onPress as () => void)()
  }
  press('plan-fold-P4')
  expect(ui.get('plan:P4'), 'the fold lands in view state').toBe('1')
  press('plan-fold-P5')
  expect(ui.get('plan:P5'), 'each phase keeps its own key').toBe('1')
  const expanded = allText(PlanTab(stubEls, planView(), 46, ui))
  expect(expanded).toContain('│ ├ ') // the TreeRow prefix, per the canvas Plan board
  expect(expanded).toContain('│ └ ')
  expect(expanded).toContain('R1 stripe adapter') // the fixture's subtask
  expect(expanded).toContain('2 findings') // the right chip for findings
  // the arrow flips with the state
  expect(allText(PlanTab(stubEls, planView(), 46, ui)).includes('▾'), 'the expanded arrow').toBe(true)
  // and pressing again folds it back
  press('plan-fold-P4')
  expect(ui.get('plan:P4')).toBe('')
  expect(allText(PlanTab(stubEls, planView(), 46, ui)).includes('R1 stripe adapter'), 'folded back').toBe(false)
  expect(allText(PlanTab(stubEls, planView(), 46, ui)).includes('2 findings'), 'the other phase stays open').toBe(true)
})

test('Plan tab: the pane mount folds a phase row and the fold survives a refresh tick', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-plan' })
  expect(await ui.find({ text: /R1 stripe adapter/ }), 'folded to start').toBeUndefined()
  await ui.press({ key: 'plan-fold-P4' })
  expect(await ui.find({ text: /R1 stripe adapter/ }), 'the subtask after the press').toBeTruthy()
  expect(await ui.find({ text: /│ ├/ }), 'the tree prefix drawn').toBeTruthy()
  await clock.advance(5000) // the refresh tick reloads the snapshot and redraws
  expect(await ui.find({ text: /R1 stripe adapter/ }), 'the fold survived the tick').toBeTruthy()
  await ui.press({ key: 'plan-fold-P4' })
  expect(await ui.find({ text: /R1 stripe adapter/ }), 'pressed again folds back').toBeUndefined()
})
