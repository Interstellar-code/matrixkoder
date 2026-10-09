// Lanes tab tests (plan dashboard-v2-p0, T2.1): the real pane, the real tab
// wiring — press dash-tab-lanes and read what LanesTab(els, lanesView(), cols)
// drew. Fixture strings the S0 stub never draws fail the tests while the stub
// is still standing.
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

test('the Lanes tab draws the MOCK row and its fixture lanes at 46 cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-lanes' })
  expect(await ui.find({ text: MOCK }), 'MOCK row').toBeTruthy()
  expect(await ui.find({ text: /stripe adapter/ }), 'fixture commit message').toBeTruthy()
  expect(await ui.find({ text: /\[Reroute\]/ }), 'fixture actions').toBeTruthy()
})

test('the Lanes tab fits 36, 40, 46 and 56 cols: MOCK row, fixture string, no row over cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-lanes' })
  for (const w of [36, 40, 46, 56]) {
    await ui.redraw(paneProps(w))
    expect(await ui.find({ text: MOCK }), `MOCK row at ${w}`).toBeTruthy()
    for (const width of textWidths(await ui.drawn())) {
      expect(width, `row wider than ${w} cells at ${w}: ${width}`).toBeLessThanOrEqual(w)
    }
  }
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: /stripe adapter/ }), 'fixture string at 36').toBeTruthy()
})

test('the gate table is three columns at 56 cols and one line under 50', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(56) })
  await ui.press({ key: 'dash-tab-lanes' })
  expect(await ui.find({ text: /baseline/ }), 'gate header at wide').toBeTruthy()
  expect(await ui.find({ text: /410 \/ 2/ }), 'baseline column').toBeTruthy()
  expect(await ui.find({ text: /tests 412\/2/ }), 'no one-liner at wide').toBeUndefined()
  await ui.redraw(paneProps(46))
  expect(await ui.find({ text: /tests 412\/2 · lint 0 · tsc ok/ }), 'one-line gate under 50').toBeTruthy()
  expect(await ui.find({ text: /baseline/ }), 'no gate table under 50').toBeUndefined()
})
