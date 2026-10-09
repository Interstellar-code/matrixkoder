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

test('Plan tab: under NARROW the compact fixture fits — no Text wider than 36 cells', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-plan' })
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: /MOCK · fixture data · not this run/ })).toBeTruthy()
  expect(await ui.find({ text: /Cart schema/ }), 'the short W1 title').toBeTruthy()
  const texts = rowTexts(await ui.drawn())
  expect(texts.length, 'the pane drew text').toBeGreaterThan(0)
  const wide = texts.filter(t => t.length > 36)
  expect(wide, `over-wide Texts: ${JSON.stringify(wide)}`).toEqual([])
})
