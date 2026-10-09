// Real-code tests for the Plan tab (dashboard-v2-p0, T1.2): mount the real
// pane, press the tab, read what it drew — the MOCK row, the Checkout v2
// fixture strings Old never draws, the merged-vs-done marker split (the plan's
// acceptance line) and the under-NARROW fit (no Text node wider than cols).
import { test, expect } from 'claude-code/testing'
import { mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './../testkit'

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

/** a row's checkbox marker: the one [x]-shaped Text in it. */
const markerOf = (row: unknown): string | undefined => rowTexts(row).find(t => /^\[.\]$/.test(t))

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

test('Plan tab: merged and done draw DISTINCT markers (the acceptance line)', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-plan' })
  const mergedRow = await ui.find({ key: 'P1' }) // a W1 merged task
  const doneRow = await ui.find({ key: 'P0' }) // the fixture's one done task
  expect(mergedRow, 'the merged row drawn').toBeDefined()
  expect(doneRow, 'the done row drawn').toBeDefined()
  const merged = markerOf(mergedRow)
  const done = markerOf(doneRow)
  expect(merged).toBe('[◈]') // merged: neither in-progress [•] nor done [✓]
  expect(done).toBe('[✓]')
  expect(merged).not.toBe(done)
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
    const widths = rowWidths(await ui.drawn())
    expect(widths.length, `the pane drew rows at ${cols}`).toBeGreaterThan(0)
    const wide = widths.filter(w => w > cols)
    expect(wide, `over-wide rows at ${cols} cols: ${JSON.stringify(wide)}`).toEqual([])
  }
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: /Cart schema/ }), 'the short W1 title').toBeTruthy()
})
