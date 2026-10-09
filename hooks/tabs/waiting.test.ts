// Waiting tab tests (plan dashboard-v2-p0, T2.3): the real pane, the real tab
// wiring — press dash-tab-waiting and read what WaitingTab(els, waitingView(),
// cols) drew. Fixture strings the S0 stub never draws fail the tests while the
// stub is still standing.
import { test, expect } from 'claude-code/testing'
import { mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './../testkit'

const MOCK = /MOCK · fixture data · not this run/

/** every Text node's full shown width (strings under it, nested Texts included). */
function textWidths(n: unknown): number[] {
  const out: number[] = []
  const shown = (node: unknown): string => {
    if (typeof node === 'string') return node
    if (typeof node !== 'object' || node === null) return ''
    const cur = node as { children?: unknown[] }
    return Array.isArray(cur.children) ? cur.children.map(shown).join('') : ''
  }
  const walk = (node: unknown): void => {
    if (typeof node !== 'object' || node === null) return
    const cur = node as { type?: string; children?: unknown[] }
    if (cur.type === 'Text') {
      out.push(shown(cur).length)
      return
    }
    if (Array.isArray(cur.children)) cur.children.forEach(walk)
  }
  walk(n)
  return out
}

test('the Waiting tab draws the MOCK row and its fixture decision card at 46 cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-waiting' })
  expect(await ui.find({ text: MOCK }), 'MOCK row').toBeTruthy()
  expect(await ui.find({ text: /tax_lines/ }), 'fixture question').toBeTruthy()
  expect(await ui.find({ text: /recommended/ }), 'fixture recommendation').toBeTruthy()
})

test('the Waiting tab fits 36 cols: MOCK row, fixture string, no row over 36 cells', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-waiting' })
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: MOCK }), 'MOCK row').toBeTruthy()
  expect(await ui.find({ text: /tax_lines/ }), 'fixture string at 36').toBeTruthy()
  for (const w of textWidths(await ui.drawn())) {
    expect(w, `row wider than 36 cells: ${w}`).toBeLessThanOrEqual(36)
  }
})

test('the decision card is the only bordered box and the recommended option is marked', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-waiting' })
  const boxes = await ui.findAll({ type: 'Box' })
  expect(boxes.filter(b => typeof b.props.borderStyle === 'string').length, 'exactly one bordered box').toBe(1)
  expect(await ui.find({ text: /recommended/ }), 'recommended option marked').toBeTruthy()
})
