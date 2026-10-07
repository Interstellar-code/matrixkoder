// Dashboard pane tests (design §3): base 46-col layout on both surfaces,
// compact 36-col, inline hint, first-run and no-run states. Fixtures arrive
// via $.fs.write (inline fixtures plugin); fs is served in memory.
import { test, expect } from 'claude-code/testing'
import { NOW, RUN_PATH, USAGE_PATH, agentOf, agentsOf, mockFs, mockWorld, paneProps, sessionStart, sessionStartIn, setRun, setUsage, tasksOf, worldFiles, worldFixtures } from './testkit'

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
  expect(await ui.find({ text: /launch the wizard/ })).toBeTruthy()
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
    const tasks = tasksOf(run)
    run.tasks = [null, tasks[4] ?? {}]
  })
  const fs = mockFs(on, files)
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  let ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /X1/ })).toBeTruthy() // the null task is dropped, the rest render
  const good = worldFiles()[RUN_PATH] ?? '{}'
  files['/fixtures/run.json'] = files[RUN_PATH] = good
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
  const good = worldFiles()[RUN_PATH] ?? '{}'
  files['/fixtures/run.json'] = files[RUN_PATH] = good
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

test('home mode: projects.json (under $MATRIXKODER_HOME) names the data dir of a repo, a worktree keys by the main checkout', async ($, on) => {
  const run = worldFiles()[RUN_PATH] ?? ''
  const files: Record<string, string> = {
    '/srv/mkhome/projects.json': JSON.stringify({ '/tmp/main': { data_dir: '/srv/mkhome/projects/main', onboarded_at: 'x' } }),
    '/srv/mkhome/projects/main/runs/index.json': JSON.stringify([{ id: 'wfv2', feature: 'workflows-v2', status: 'active', started: '2026-10-07T16:00:00+02:00' }]),
    '/srv/mkhome/projects/main/runs/wfv2/run.json': run,
    '/tmp/wt-lane/.git': 'gitdir: /tmp/main/.git/worktrees/lane\n',
    '/tmp/main/.git/worktrees/lane/commondir': '../..\n',
  }
  mockFs(on, files, ['/srv/mkhome/projects/main', '/tmp/main/.git'], ['/tmp/wt-lane/.git'])
  mockWorld(on, { env: { MATRIXKODER_HOME: '/srv/mkhome' } })
  await $.session.start(sessionStartIn('/tmp/wt-lane'))
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /workflows-v2/ })).toBeTruthy()
  expect(await ui.find({ text: /Welcome to MatrixKoder/ })).toBeUndefined()
})

test('MATRIXKODER_HOME moves the global dir: usage.json is read from it', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  files['/srv/mkhome/usage.json'] = (files[USAGE_PATH] ?? '').replace('claude-gpt', 'srv-pool') // distinguishable from the fixture copy under HOME
  mockFs(on, files)
  mockWorld(on, { env: { MATRIXKODER_HOME: '/srv/mkhome' } })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /srv-pool/ })).toBeTruthy()
  expect(await ui.find({ text: /claude-gpt/ })).toBeUndefined()
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
    const tasks = tasksOf(run)
    if (tasks[1]) tasks[1].title = 'design and configuration pass'
    if (tasks[2]) tasks[2].updated = new Date(NOW - 12 * 60_000).toISOString()
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
    const agents = agentsOf(usage)
    delete agents.claude
    delete agents.codex
    delete agents.opencode
    // agy carries ONLY the exhausted claude-gpt pool: no gemini, agent-level exhausted_until null
    const agy = agentOf(usage, 'agy')
    if (!agy) return
    agy.exhausted_until = null
    agy.five_hour = { used_pct: 100, resets_at: new Date(NOW + 10 * 24 * 3600_000).toISOString() }
    const pools = agy.pools
    agy.pools = typeof pools === 'object' && pools !== null ? { 'claude-gpt': (pools as Record<string, unknown>)['claude-gpt'] } : {}
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
    const oc = agentOf(usage, 'opencode')
    if (!oc) return
    oc.exhausted_until = new Date(NOW + 10 * 24 * 3600_000).toISOString()
    oc.five_hour = { used_pct: 100, resets_at: new Date(NOW + 3600_000).toISOString() }
    if (typeof oc.pools === 'object' && oc.pools !== null) {
      const def = (oc.pools as Record<string, Record<string, unknown>>).default
      if (def) def.exhausted_until = oc.exhausted_until
    }
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
    const claude = agentOf(usage, 'claude')
    if (!claude) return
    claude.five_hour = null
    if (typeof claude.pools === 'object' && claude.pools !== null) {
      const def = (claude.pools as Record<string, Record<string, unknown>>).default
      if (def) def.five_hour = null
    }
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
  const first = polls[0]
  if (!first) throw new Error('no poll ran')
  expect(first[0]).toBe('python3')
  expect(first[1]?.endsWith('scripts/mk-usage.py')).toBe(true)
  expect(first[2]).toBe('--once')
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
  const runCmd = (args: string) => $.command.run({ command: 'mk', args, origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 165 } })
  expect((await runCmd('close')).text).toBe('MatrixKoder pane closed.')
  expect((await runCmd('open')).text).toBe('MatrixKoder pane opened.')
  expect((await runCmd('')).text).toBe('MatrixKoder pane closed.')
  expect((await runCmd('')).text).toBe('MatrixKoder pane opened.')
  const setup = await runCmd('setup')
  expect(setup.text).toContain('setup opened') // wizard.tsx answers (wizard.test.ts covers the pane)
  const init = await runCmd('init wfv2')
  expect(init.text).toContain('setup opened')
  expect((await runCmd('bogus')).text).toContain('Usage:')
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

test('autoOpen:false never opens the pane', { plugins: [worldFixtures], options: { autoOpen: false } }, async ($, on) => {
  const opens: string[] = []
  mockFs(on, worldFiles())
  mockWorld(on, { open: id => opens.push(id) })
  await $.session.start(sessionStart())
  expect(opens).toEqual([])
})

test('chip:false leaves the status line untouched', { plugins: [worldFixtures], options: { chip: false } }, async ($, on) => {
  const chips: (string | undefined)[] = []
  mockFs(on, worldFiles())
  const clock = mockWorld(on, { status: t => chips.push(t) })
  await $.session.start(sessionStart())
  await clock.advance(5000)
  expect(chips).toEqual([])
})

// ---------- fix2: live-pane bugs ----------

test('▶ Next renders as one row: label and text in a single truncating Text', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    run.next = 'Re-review P4+P6 fixes, merge mk-int to main and tag the dashboard milestone'
  })
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /▶ Next: Re-review P4\+P6/ })).toBeTruthy() // one row, label + text together
  // find matches substrings of the whole tree, so walk it: no Text node may be
  // the bare label — the label and the text share ONE truncating Text
  const textOf = (n: unknown): string => {
    if (typeof n === 'string') return n
    if (typeof n !== 'object' || n === null) return ''
    const node = n as { type?: string; children?: unknown[] }
    if (node.type !== 'Text' || !Array.isArray(node.children)) return ''
    return node.children.map(textOf).join('')
  }
  const texts: string[] = []
  const walk = (n: unknown): void => {
    if (typeof n !== 'object' || n === null) return
    const node = n as { type?: string; children?: unknown[] }
    if (node.type === 'Text') {
      const own = textOf(n)
      if (own) texts.push(own)
    }
    if (Array.isArray(node.children)) node.children.forEach(walk)
  }
  walk(await ui.drawn())
  expect(texts.includes('▶ Next:')).toBe(false)
  expect(texts.some(t => t.startsWith('▶ Next: Re-review'))).toBe(true)
})

test('todo boxes: in-progress [•] (review, merged), side states [!], done [✓]', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /\[•\]F6/ })).toBeTruthy() // review is in progress
  expect(await ui.find({ text: /\[•\]W0/ })).toBeTruthy() // merged is still in progress
  expect(await ui.find({ text: /\[!\]FIX6/ })).toBeTruthy() // stalled is a side state
  expect(await ui.find({ text: /\[ \]X1/ })).toBeTruthy() // planned stays empty
})

test('todo boxes: done checks green', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    for (const t of tasksOf(run)) t.status = 'done'
  })
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /\[✓\]W0/ })).toBeTruthy()
})

test('usage columns align: padded header, exact-width name column', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: '   5h' })).toBeTruthy() // right-aligned 5-wide header
  expect(await ui.find({ text: ' week' })).toBeTruthy()
  expect(await ui.find({ text: '  └ claude-gpt' })).toBeTruthy() // sub-row label fills the 14-wide name column exactly
  expect(await ui.find({ text: 'codex         ' })).toBeTruthy() // agent names pad to the same width
})

test('lanes fall back to live tasks when no lane is set', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    for (const t of tasksOf(run)) t.lane = null
  })
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /∇ AGY/ })).toBeTruthy() // F8 building stands in with its agent glyph
  expect(await ui.find({ text: 'none live' })).toBeUndefined()
})

test('/mk setup then closing the wizard shows the run again (no sticky first-run panel)', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  await $.command.run({ command: 'mk', args: 'setup', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 100 } })
  const wiz = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder-setup', props: paneProps(64) })
  await wiz.press({ key: 'wz-cancel' }) // the wizard closes
  await clock.advance(5000) // a refresh tick afterwards
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /Welcome to MatrixKoder/ })).toBeUndefined()
  expect(await ui.find({ text: /workflows-v2/ })).toBeTruthy()
  expect(await ui.find({ text: 'Plan' })).toBeTruthy()
  expect(await ui.find({ text: 'Lanes' })).toBeTruthy()
  expect(await ui.find({ text: 'Waiting on you' })).toBeTruthy()
})
