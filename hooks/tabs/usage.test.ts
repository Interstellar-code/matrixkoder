// Usage tab tests (plan dashboard-v2-p0, T2.2): the real pane, the real tab
// wiring — press dash-tab-usage and read what UsageTab(els, usageView(), cols)
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

test('the Usage tab draws the MOCK row and its fixture quota/Jev rows at 46 cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-usage' })
  expect(await ui.find({ text: MOCK }), 'MOCK row').toBeTruthy()
  expect(await ui.find({ text: /sign in at ollama.com/ }), 'fixture ollama note').toBeTruthy()
  expect(await ui.find({ text: /pick opencode glm-5 high/ }), 'fixture Jev pick line').toBeTruthy()
})

test('the Usage tab fits 36 cols: MOCK row, fixture string, no row over 36 cells', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-usage' })
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: MOCK }), 'MOCK row').toBeTruthy()
  expect(await ui.find({ text: /sign in at ollama.com/ }), 'fixture string at 36').toBeTruthy()
  for (const w of textWidths(await ui.drawn())) {
    expect(w, `row wider than 36 cells: ${w}`).toBeLessThanOrEqual(36)
  }
})

test('a quota bar row carries the reserve floor tick', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-usage' })
  const tickRows = await ui.findAll({ text: /│/ })
  expect(tickRows.some(el => /[█░]/.test(el.text)), 'floor tick inside a bar row').toBeTruthy()
})
