// Dashboard pane tests (design §3): base 46-col layout on both surfaces,
// compact 36-col, inline hint, first-run and no-run states. Fixtures arrive
// via $.fs.write (inline fixtures plugin); fs is served in memory.
import { test, expect } from 'claude-code/testing'
import { NOW, RUN_PATH, USAGE_PATH, mockFs, mockWorld, paneProps, sessionStart, sessionStartIn, setRun, setUsage, worldFiles, worldFixtures } from './testkit'

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

// ---------- fix1: resilience, data-dir rule, /mk args, timers, usage rows ----------

test('tasks:[null] run.json still renders and recovers once fixed', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    run.tasks = [null, run.tasks[4]]
  })
  const fs = mockFs(on, files)
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  let ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /X1/ })).toBeTruthy() // the null task is dropped, the rest render
  files['/fixtures/run.json'] = files[RUN_PATH] = worldFiles()[RUN_PATH]
  await clock.advance(5000)
  expect(await ui.find({ text: /F6/ })).toBeTruthy() // recovered
  expect(fs.reads.filter(p => p === RUN_PATH).length).toBeGreaterThan(1) // the timer survived the bad load
})

test('corrupt run.json shows the ⚠ note, then recovers', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  files['/fixtures/run.json'] = files[RUN_PATH] = '{not json'
  mockFs(on, files)
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /cannot read run\.json/ })).toBeTruthy()
  files['/fixtures/run.json'] = files[RUN_PATH] = worldFiles()[RUN_PATH]
  await clock.advance(5000)
  expect(await ui.find({ text: /cannot read run\.json/ })).toBeUndefined()
  expect(await ui.find({ text: /workflows-v2/ })).toBeTruthy()
})

test('a project under home without a data dir is first-run, never the global dir', async ($, on) => {
  const files: Record<string, string> = {
    // the GLOBAL dir exists (usage only) — it must not be picked as project data
    '/home/mk/.matrixkoder/usage.json': '{"agents":{}}',
  }
  mockFs(on, files, ['/home/mk/.matrixkoder'])
  mockWorld(on)
  await $.session.start(sessionStartIn('/home/mk/src/app'))
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /Welcome to MatrixKoder/ })).toBeTruthy()
  expect(await ui.find({ text: 'No active orchestration run.' })).toBeUndefined()
})

test('a linked worktree resolves to the main checkout data dir', async ($, on) => {
  const files: Record<string, string> = {
    '/tmp/wt-lane/.git': 'gitdir: /tmp/main/.git/worktrees/lane\n',
    '/tmp/main/.git/worktrees/lane/commondir': '../..\n',
    '/tmp/main/.matrixkoder/runs/index.json': JSON.stringify([{ id: 'wfv2', feature: 'workflows-v2', status: 'active', started: '2026-10-07T16:00:00+02:00' }]),
    '/tmp/main/.matrixkoder/runs/wfv2/run.json': worldFiles()[RUN_PATH] ?? '',
  }
  mockFs(on, files, ['/tmp/main/.matrixkoder', '/tmp/main/.git'], ['/tmp/wt-lane/.git'])
  mockWorld(on)
  await $.session.start(sessionStartIn('/tmp/wt-lane'))
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /workflows-v2/ })).toBeTruthy()
})

test('MATRIXKODER_DATA overrides the walk', async ($, on) => {
  const files: Record<string, string> = {
    '/tmp/alt-data/runs/index.json': JSON.stringify([{ id: 'alt', feature: 'alt-feature', status: 'active', started: '2026-10-07T16:00:00+02:00' }]),
    '/tmp/alt-data/runs/alt/run.json': worldFiles()[RUN_PATH] ?? '',
  }
  mockFs(on, files, [])
  mockWorld(on, { env: { MATRIXKODER_DATA: '/tmp/alt-data' } })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /workflows-v2/ })).toBeTruthy() // run from the env dir, not the walk
  expect(await ui.find({ text: /Welcome to MatrixKoder/ })).toBeUndefined()
})

test('missing feature/branch does not break compact rendering', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    delete run.feature
    delete run.branch
  })
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: 'stg 7/12' })).toBeTruthy()
})

test('compact cuts the title to 14 chars and shows round/elapsed', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    const tasks = run.tasks as Record<string, unknown>[]
    tasks[1].title = 'design and configuration pass'
    tasks[2].updated = new Date(NOW - 12 * 60_000).toISOString()
  })
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: 'design and con' })).toBeTruthy()
  expect(await ui.find({ text: /configuration pass/ })).toBeUndefined()
  expect(await ui.find({ text: /review r2/ })).toBeTruthy()
  expect(await ui.find({ text: /build 12m|building 12m/ })).toBeTruthy()
})

test('red only when the agent-level exhaustion says so; exhausted rows show the clearing date', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setUsage(files, usage => {
    const agents = usage.agents as Record<string, Record<string, unknown>>
    delete agents.claude
    delete agents.codex
    delete agents.opencode
    // agy carries ONLY the exhausted claude-gpt pool: no gemini, agent-level exhausted_until null
    const agy = agents.agy
    agy.exhausted_until = null
    agy.five_hour = { used_pct: 100, resets_at: new Date(NOW + 10 * 24 * 3600_000).toISOString() }
    agy.pools = { 'claude-gpt': (agy.pools as Record<string, unknown>)['claude-gpt'] }
  })
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: '⊘' })).toBeUndefined() // agent row not marked exhausted
})

test('exhausted agent row shows exhausted_until (10-…), not the 5h reset', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setUsage(files, usage => {
    const agents = usage.agents as Record<string, Record<string, unknown>>
    const oc = agents.opencode as Record<string, any>
    oc.exhausted_until = new Date(NOW + 10 * 24 * 3600_000).toISOString()
    oc.five_hour = { used_pct: 100, resets_at: new Date(NOW + 3600_000).toISOString() }
    ;(oc.pools as Record<string, any>).default.exhausted_until = oc.exhausted_until
  })
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /10-1[0-9]/ })).toBeTruthy()
  expect(await ui.find({ text: /1h00m/ })).toBeUndefined()
})

test('null primary 5h with sub-pools labels n/a, not 0%', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setUsage(files, usage => {
    const agents = usage.agents as Record<string, Record<string, unknown>>
    const claude = agents.claude as Record<string, any>
    claude.five_hour = null
    ;(claude.pools as Record<string, any>).default.five_hour = null
  })
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: '  0%' })).toBeUndefined()
})

test('missing usage triggers the initial poll with the right argv', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  delete files['/fixtures/usage.json']
  delete files[USAGE_PATH]
  mockFs(on, files)
  const polls: (readonly string[])[] = []
  mockWorld(on, { process: argv => polls.push(argv) })
  await $.session.start(sessionStart())
  expect(polls.length).toBe(1)
  expect(polls[0][0]).toBe('python3')
  expect(polls[0][1].endsWith('scripts/mk-usage.py')).toBe(true)
  expect(polls[0][2]).toBe('--once')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: 'no ledger yet' })).toBeTruthy()
})

test('two session.start arm exactly one refresh timer', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const fs = mockFs(on, files)
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  await $.session.start(sessionStart())
  await clock.advance(5000)
  await clock.advance(5000)
  await clock.advance(5000)
  expect(fs.reads.filter(p => p === RUN_PATH).length).toBe(5) // 2 initial loads + 3 single ticks
})

test('/mk args: open, close, setup, init and unknown', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const run = (args: string) => $.command.run({ command: 'mk', args })
  expect((await run('close')).text).toBe('MatrixKoder pane closed.')
  expect((await run('open')).text).toBe('MatrixKoder pane opened.')
  expect((await run('')).text).toBe('MatrixKoder pane closed.')
  expect((await run('')).text).toBe('MatrixKoder pane opened.')
  const setup = await run('setup')
  expect(setup.text).toContain('coming in P5')
  const init = await run('init wfv2')
  expect(init.text).toContain('coming in P5')
  expect((await run('bogus')).text).toContain('Usage:')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /Welcome to MatrixKoder/ })).toBeTruthy() // setupHint forced the first-run panel
})

test('the chip is not re-sent when unchanged', { plugins: [worldFixtures] }, async ($, on) => {
  const chips: (string | undefined)[] = []
  mockFs(on, worldFiles())
  const clock = mockWorld(on, { status: t => chips.push(t) })
  await $.session.start(sessionStart())
  await clock.advance(5000)
  await clock.advance(5000)
  expect(chips.length).toBe(1)
})
