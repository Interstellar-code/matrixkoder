// Overview tab tests (plan dashboard-v2-p0, T1.1): the REAL pane mounted the
// way dashboard.test.ts mounts it — the tab must render the Checkout v2
// fixture (a stub fails every assertion below), hide the Old pane, and keep
// every drawn Text within the pane's width at 36 cols.
import { test, expect } from 'claude-code/testing'
import { mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './../testkit'

// ---------- row widths (review r1 HIGH): a row is a horizontal Box (or a
// Text); its width is the text of every Text descendant — nested Texts
// included — plus the gap columns between its children. A single Text node
// staying under `cols` proves nothing about the row it sits in.

type DrawnNode = { type?: string; props?: { flexDirection?: string; gap?: number }; children?: unknown[] }

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

/** every row's width in the drawn tree: each Text and each horizontal Box. */
function rowWidths(root: unknown): number[] {
  const out: number[] = []
  const walk = (node: unknown): void => {
    if (typeof node !== 'object' || node === null) return
    const n = node as DrawnNode
    if (n.type === 'Text' || (n.type === 'Box' && n.props?.flexDirection === 'row')) out.push(widthOf(node))
    for (const c of n.children ?? []) walk(c)
  }
  walk(root)
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
    const wide = rowWidths(await ui.drawn()).filter(w => w > cols)
    expect(wide, `over-wide rows at ${cols} cols: ${JSON.stringify(wide)}`).toEqual([])
  }
})

test('the Overview usage rows draw real bars and the exhausted marker', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-overview' })
  expect(await ui.find({ text: /█/ }), 'a filled bar').toBeTruthy()
  expect(await ui.find({ text: /38%/ }), 'the 5h percentage').toBeTruthy()
  expect(await ui.find({ text: /exhausted · 19:05/ }), 'the ⊘ exhausted row').toBeTruthy()
})
