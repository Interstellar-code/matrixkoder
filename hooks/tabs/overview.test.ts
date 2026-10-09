// Overview tab tests (plan dashboard-v2-p0, T1.1): the REAL pane mounted the
// way dashboard.test.ts mounts it — the tab must render the Checkout v2
// fixture (a stub fails every assertion below), hide the Old pane, and keep
// every drawn Text within the pane's width at 36 cols.
import { test, expect } from 'claude-code/testing'
import { mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './../testkit'

/** every Text node's own text under the drawn tree, in document order (the dashboard tests' walk). */
function textNodes(root: unknown): string[] {
  const out: string[] = []
  const walk = (n: unknown): void => {
    if (typeof n !== 'object' || n === null) return
    const node = n as { type?: string; children?: unknown[] }
    if (node.type === 'Text' && Array.isArray(node.children)) {
      const own = node.children.map(c => (typeof c === 'string' ? c : '')).join('')
      if (own) out.push(own)
    }
    if (Array.isArray(node.children)) node.children.forEach(walk)
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

test('at 36 cols the Overview keeps every drawn Text within the pane width', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-overview' })
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: /MOCK · fixture data · not this run/ }), 'the MOCK row redraws narrow').toBeTruthy()
  expect(await ui.find({ text: /watching P4 R1/ }), 'the fixture still renders narrow').toBeTruthy()
  for (const text of textNodes(await ui.drawn())) {
    expect(text.length, `oversize Text (${String(text.length)}): ${text}`).toBeLessThanOrEqual(36)
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
