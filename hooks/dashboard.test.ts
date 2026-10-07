// Dashboard pane tests (design §3): base 46-col layout on both surfaces,
// compact 36-col, inline hint, first-run and no-run states. Fixtures arrive
// via $.fs.write (inline fixtures plugin); fs is served in memory.
import { test, expect } from 'claude-code/testing'
import { mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './testkit'

test('renders section titles, a task row and the agy pool sub-row', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'matrixkoder', surface, component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
    for (const title of ['Run', 'Plan', 'Lanes', 'Usage', 'Waiting on you', 'Recent']) {
      expect(await ui.find({ text: title }), `${surface} ${title}`).toBeTruthy()
    }
    expect(await ui.find({ text: /F6/ }), `${surface} task row`).toBeTruthy()
    expect(await ui.find({ text: /claude-gpt/ }), `${surface} agy pool sub-row`).toBeTruthy()
    expect(await ui.find({ text: /review/ }), `${surface} status column`).toBeTruthy()
    expect(await ui.find({ text: /workflows-v2/ }), `${surface} feature`).toBeTruthy()
    expect(await ui.find({ text: /main@82252eb/ }), `${surface} branch@head`).toBeTruthy()
  }
})

test('compact variant at 36 cols drops the week column and shortens the stage', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: 'stg 7/12' })).toBeTruthy()
  expect(await ui.find({ text: 'week' })).toBeUndefined()
  expect(await ui.find({ text: /design\+configure/ })).toBeTruthy() // title still drawn, truncated by wrap
})

test('inline placement shows the /tui fullscreen hint', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.redraw(paneProps(46, 'inline'))
  expect(await ui.find({ text: /fullscreen/ })).toBeTruthy()
  await ui.redraw(paneProps(46, 'dock'))
  expect(await ui.find({ text: /fullscreen/ })).toBeUndefined()
})

test('first-run state when no .matrixkoder dir exists', async ($, on) => {
  mockFs(on, {}, [])
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /Welcome to MatrixKoder/ })).toBeTruthy()
  expect(await ui.find({ text: /mk setup/ })).toBeTruthy()
  expect(await ui.find({ text: /wizard \(coming in P5\)/ })).toBeTruthy()
  expect(await ui.find({ text: 'Plan' })).toBeUndefined()
})

test('no active run state lists recent runs and keeps usage', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  delete files['/tmp/mk-proj/.matrixkoder/runs/wfv2/run.json'] // run.json vanishes (archived reality)
  await clock.advance(5000) // next tick reloads → no active run
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: 'No active orchestration run.' })).toBeTruthy()
  expect(await ui.find({ text: /mk-p0/ })).toBeTruthy()
  expect(await ui.find({ text: 'Usage' })).toBeTruthy()
  expect(await ui.find({ text: 'Lanes' })).toBeUndefined() // run sections hidden without a run
})
