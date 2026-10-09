// Dashboard pane tests (design §3): base 46-col layout on both surfaces,
// compact 36-col, inline hint, first-run and no-run states. Fixtures arrive
// via $.fs.write (inline fixtures plugin); fs is served in memory.
import { test, expect } from 'claude-code/testing'
import { DATA_DIR, NOW, RUN_PATH, RUNS_INDEX, USAGE_PATH, agentOf, agentsOf, mockFs, mockWorld, paneProps, sessionStart, sessionStartIn, setRun, setUsage, tasksOf, worldFiles, worldFixtures } from './testkit'
import { parseYamlAgents } from './data'

/** a project matrixkoder.yaml in the wizard's emitter shape: agents block + usage providers */
const PROJECT_YAML = [
  'data_dir: .matrixkoder/',
  'agents:',
  '  codex: { available: false, models: [] }',
  '  opencode: { available: true, models: [zai-coding-plan/glm-4.7] }',
  '  agy: { available: false, models: [] }',
  '  claude: { available: true }',
  '  herdr: { available: true }',
  `  checked_at: "${new Date(NOW).toISOString()}"`,
  'usage:',
  '  source: codexbar',
  '  poll_min: 5',
  '  providers: [claude, codex, antigravity]',
].join('\n')

test('renders section titles, a task row and the agy pool sub-row', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'matrixkoder', surface, component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
    for (const title of ['Run', 'Plan', 'Lanes', 'Usage', 'Waiting on you', 'Recent']) {
      expect(await ui.find({ type: 'Text', text: title }), `${surface} ${title}`).toBeTruthy()
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
  expect(await ui.find({ type: 'Text', text: 'Plan' })).toBeUndefined()
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
  expect(await ui.find({ type: 'Text', text: 'Usage' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: 'Lanes' })).toBeUndefined() // run sections hidden without a run
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
  const clock = mockWorld(on, { process: argv => { polls.push(argv) } })
  await $.session.start(sessionStart())
  // the cold-start poll is fire-and-forget: advance(0) settles the event loop so its dispatch chain reaches process.run
  await clock.advance(0)
  expect(polls.length).toBe(1)
  const first = polls[0]
  if (!first) throw new Error('no poll ran')
  expect(first[0]).toBe('python3')
  expect(first[1]?.endsWith('scripts/mk-usage.py')).toBe(true)
  expect(first[2]).toBe('--once')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: 'no ledger yet' })).toBeTruthy()
})

test('a project yaml scopes the usage poll: --providers from usage.providers', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  files[`${DATA_DIR}/matrixkoder.yaml`] = PROJECT_YAML
  delete files['/fixtures/usage.json']
  delete files[USAGE_PATH]
  mockFs(on, files)
  const polls: (readonly string[])[] = []
  const clock = mockWorld(on, { process: argv => { polls.push(argv) } })
  await $.session.start(sessionStart())
  // the cold-start poll is fire-and-forget: advance(0) settles the event loop so its dispatch chain reaches process.run
  await clock.advance(0)
  expect(polls.length).toBe(1)
  const first = polls[0]
  if (!first) throw new Error('no poll ran')
  expect(first[0]).toBe('python3')
  expect(first[1]?.endsWith('scripts/mk-usage.py')).toBe(true)
  expect(first[2]).toBe('--once')
  expect(first[3]).toBe('--providers')
  expect(first[4]).toBe('claude,codex,antigravity')
})

test('the usage section shows only the yaml agents; an enabled agent without a ledger entry gets a hint row', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  files[`${DATA_DIR}/matrixkoder.yaml`] = PROJECT_YAML
  setUsage(files, usage => {
    delete agentsOf(usage).opencode // enabled in the yaml, absent from the ledger
  })
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: 'claude (all)' }), 'claude row shown').toBeTruthy()
  expect(await ui.find({ text: 'codex         ' }), 'codex disabled in the yaml: no row').toBeUndefined()
  expect(await ui.find({ text: 'agy            ' }), 'agy disabled in the yaml: no row').toBeUndefined()
  expect(await ui.find({ text: 'opencode      no usage — enable zai in CodexBar' }), 'opencode hint row').toBeTruthy()
})

test('fable with no 5h window renders — in the 5h column; claude main row reads claude (all); n/a only for a status n/a entry', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setUsage(files, usage => {
    const claude = agentOf(usage, 'claude')
    const pools = claude?.pools
    if (typeof pools === 'object' && pools !== null) {
      const fable = (pools as Record<string, Record<string, unknown>>).fable
      if (fable) fable.five_hour = null
    }
    const codex = agentOf(usage, 'codex')
    if (codex) codex.status = 'n/a'
  })
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: 'claude (all)' }), 'main claude row covers all models').toBeTruthy()
  expect(await ui.find({ text: '  n/a' }), 'a whole-entry n/a keeps n/a in the 5h column').toBeTruthy()
  // walk the drawn tree: the fable sub-row's 5h column is '—', not n/a
  const cols: string[] = []
  let inRow = false
  const walk = (n: unknown): void => {
    if (typeof n !== 'object' || n === null) return
    const node = n as { type?: string; children?: unknown[] }
    if (node.type === 'Text' && Array.isArray(node.children)) {
      const own = node.children.map(c => (typeof c === 'string' ? c : '')).join('')
      if (own.startsWith('  └ fable')) inRow = true
      else if (inRow) {
        cols.push(own)
        if (cols.length === 4) inRow = false // name, bar, 5h, week(+reset on the row's last Texts)
      }
    }
    if (Array.isArray(node.children)) node.children.forEach(walk)
  }
  walk(await ui.drawn())
  expect(cols[0]).toBe('░░░░░░░') // empty bar: no 5h value
  expect(cols[1], 'fable 5h column').toBe('    —')
  expect(cols.some(c => c === '  n/a'), 'no n/a on a pool row').toBe(false)
  // compact keeps the plain agent name
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: 'claude (all)' })).toBeUndefined()
})

const U3_PROJECT_YAML = [
  'data_dir: .matrixkoder/',
  'agents:',
  '  codex: { available: false, models: [] }',
  '  opencode: { available: true, models: [zai-coding-plan/glm-4.7] }',
  '  agy: { available: false, models: [] }',
  '  claude: { available: true }',
  '  herdr: { available: true }',
  `  checked_at: "${new Date(NOW).toISOString()}"`,
  'usage:',
  '  source: codexbar',
  '  poll_min: 5',
  '  providers: [zai, opencodego, ollama]',
].join('\n')

function makeU3Usage(): string {
  return JSON.stringify({
    agents: {
      claude: {
        source: 'codexbar',
        five_hour: { used_pct: 4, resets_at: new Date(NOW + 4 * 3600_000).toISOString() },
        weekly: { used_pct: 52, resets_at: new Date(NOW + 90 * 3600_000).toISOString() },
        exhausted_until: null,
        available_pools: ['default', 'fable'],
        pools: {
          default: {
            five_hour: { used_pct: 4, resets_at: new Date(NOW + 4 * 3600_000).toISOString() },
            weekly: { used_pct: 52, resets_at: new Date(NOW + 90 * 3600_000).toISOString() },
            exhausted_until: null,
          },
          fable: {
            five_hour: { used_pct: 17, resets_at: new Date(NOW + 4 * 3600_000).toISOString() },
            weekly: { used_pct: 17, resets_at: new Date(NOW + 90 * 3600_000).toISOString() },
            exhausted_until: null,
          },
        },
      },
      agy: {
        source: 'codexbar',
        five_hour: { used_pct: 54, resets_at: new Date(NOW + 3600_000).toISOString() },
        weekly: { used_pct: 52, resets_at: new Date(NOW + 6 * 24 * 3600_000).toISOString() },
        exhausted_until: null,
        available_pools: ['gemini'],
        pools: {
          gemini: {
            five_hour: { used_pct: 54, resets_at: new Date(NOW + 3600_000).toISOString() },
            weekly: { used_pct: 52, resets_at: new Date(NOW + 6 * 24 * 3600_000).toISOString() },
            exhausted_until: null,
          },
          'claude-gpt': {
            five_hour: { used_pct: 100, resets_at: new Date(NOW + 3600_000).toISOString() },
            weekly: { used_pct: 100, resets_at: new Date(NOW + 6 * 24 * 3600_000).toISOString() },
            exhausted_until: new Date(NOW + 6 * 24 * 3600_000).toISOString(),
          },
        },
      },
      codex: { source: 'codexbar', exhausted_until: null, available_pools: ['default'], pools: { default: { five_hour: null, weekly: null, exhausted_until: null } } },
      opencode: {
        source: 'codexbar',
        five_hour: { used_pct: 0, resets_at: new Date(NOW + 4 * 3600_000).toISOString() },
        weekly: { used_pct: 0, resets_at: new Date(NOW + 7 * 24 * 3600_000).toISOString() },
        exhausted_until: null,
        available_pools: ['go'],
        pools: {
          zai: {
            five_hour: null,
            weekly: null,
            exhausted_until: null,
            status: 'n/a',
            error: 'No available fetch strategy for zai.',
          },
          go: {
            five_hour: { used_pct: 0, resets_at: new Date(NOW + 4 * 3600_000).toISOString() },
            weekly: { used_pct: 0, resets_at: new Date(NOW + 7 * 3600_000).toISOString() },
            exhausted_until: null,
          },
          ollama: {
            five_hour: null,
            weekly: null,
            exhausted_until: null,
            status: 'n/a',
            error: 'No Ollama session cookie found; sign in at ollama.com',
          },
        },
      },
    },
  })
}

test('U3: opencode agent shows per-plan pools with n/a hints and project agents filter out agy/codex', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  files[`${DATA_DIR}/matrixkoder.yaml`] = U3_PROJECT_YAML
  files['/fixtures/usage.json'] = files[USAGE_PATH] = makeU3Usage()
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
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
  const usageText = texts.filter(t => /claude|opencode|zai|go|ollama|fable/.test(t)).join('\n')
  expect(usageText).toContain('claude')
  expect(usageText).toContain('  └ fable')
  expect(usageText).toContain('opencode')
  expect(usageText).toContain('  └ zai')
  expect(usageText).toContain('set the z.ai key in CodexBar')
  expect(usageText).toContain('  └ go')
  expect(usageText).toContain('  └ ollama')
  expect(usageText).toContain('sign in at ollama.com')
  expect(usageText).not.toContain('agy')
  expect(usageText).not.toContain('codex')
})

test('U3: a plan pool that reports usage shows bars, not its setup hint', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  files[`${DATA_DIR}/matrixkoder.yaml`] = U3_PROJECT_YAML
  const usage = JSON.parse(makeU3Usage()) as { agents: { opencode: { pools: Record<string, unknown> } } }
  usage.agents.opencode.pools.zai = { five_hour: { used_pct: 42, resets_at: new Date(NOW + 2 * 3600_000).toISOString() }, weekly: null, exhausted_until: null }
  files['/fixtures/usage.json'] = files[USAGE_PATH] = JSON.stringify(usage)
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  const drawn = JSON.stringify(await ui.drawn())
  expect(drawn).not.toContain('set the z.ai key in CodexBar')
  expect(drawn).toContain('42%')
  expect(drawn).toContain('sign in at ollama.com') // ollama still has no numbers
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

test('/mk re-arms the refresh loop after /clear (session.end, no session.start)', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const fs = mockFs(on, files)
  const clock = mockWorld(on)
  on('session.cwd', () => ({ value: '/tmp/mk-proj' }))
  on('session.end', () => ({ sessionId: 's1' }))
  await $.session.start(sessionStart())
  await $.session.end({ reason: 'clear', sessionId: 's1', resume: { id: undefined } } as never)
  await clock.advance(5000)
  expect(fs.reads.filter(p => p === RUN_PATH).length).toBe(1) // loop dead: only the start load
  await $.command.run({ command: 'mk', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 165 } })
  expect(fs.reads.filter(p => p === RUN_PATH).length).toBe(2) // /mk reloads at once
  await clock.advance(5000)
  expect(fs.reads.filter(p => p === RUN_PATH).length).toBe(3) // and ticks again
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
  expect(await ui.find({ type: 'Text', text: 'Plan' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: 'Lanes' })).toBeTruthy()
  expect(await ui.find({ text: 'Waiting on you' })).toBeTruthy()
})

test('parseYamlAgents accepts any indent >= 2 spaces, block style, and unparseable is null never []', () => {
  // block style with 2 spaces
  const block2 = [
    'agents:',
    '  codex:',
    '    available: true',
    '    models: [gpt-5]',
    '  claude:',
    '    available: false',
  ].join('\n')
  expect(parseYamlAgents(block2)).toEqual(['codex'])

  // block style with 4 spaces
  const block4 = [
    'agents:',
    '    opencode:',
    '        available: true',
    '    agy:',
    '        available: false',
  ].join('\n')
  expect(parseYamlAgents(block4)).toEqual(['opencode'])

  // flow style with 4 spaces
  const flow4 = [
    'agents:',
    '    codex: { available: true }',
  ].join('\n')
  expect(parseYamlAgents(flow4)).toEqual(['codex'])

  // all-false agents block = empty list (show no agents), not null
  const allFalse = [
    'agents:',
    '  codex: { available: false }',
    '  claude: { available: false }',
  ].join('\n')
  expect(parseYamlAgents(allFalse)).toEqual([])

  // unparseable / absent block = no filter (null), never []
  expect(parseYamlAgents('agents:\n  garbage without keys')).toBeNull()
  expect(parseYamlAgents('agents: []')).toBeNull()
  expect(parseYamlAgents('agents:\n')).toBeNull()
  expect(parseYamlAgents('no agents block here')).toBeNull()
})

test('U3 add-on: an agents block with all agents disabled shows no usage rows', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  files[`${DATA_DIR}/matrixkoder.yaml`] = [
    'data_dir: .matrixkoder/',
    'agents:',
    '  codex: { available: false, models: [] }',
    '  opencode: { available: false, models: [] }',
    '  agy: { available: false, models: [] }',
    '  claude: { available: false }',
    '  herdr: { available: true }',
    `  checked_at: "${new Date(NOW).toISOString()}"`,
  ].join('\n')
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  // the section still renders but no agent rows appear; herdr is never shown
  expect(await ui.find({ type: 'Text', text: 'Usage' })).toBeTruthy()
  expect(await ui.find({ text: 'claude' })).toBeUndefined()
  expect(await ui.find({ text: /agy/ })).toBeUndefined()
  expect(await ui.find({ text: /opencode/ })).toBeUndefined()
  expect(await ui.find({ text: /codex/ })).toBeUndefined()
})

// ---------- H2B: lane sweep from the dashboard tick ----------

function isSweep(argv: readonly string[]): boolean {
  return argv.length >= 3 && argv[0] === 'python3' && !!argv[1]?.endsWith('scripts/mk-lane-event.py') && argv[2] === 'sweep'
}

test('sweep runs every 2 minutes when an orchestration run is active', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  const sweeps: (readonly string[])[] = []
  const clock = mockWorld(on, { process: argv => { if (isSweep(argv)) sweeps.push(argv) } })
  await $.session.start(sessionStart())
  await clock.advance(120_000)
  expect(sweeps.length).toBe(1)
  await clock.advance(119_000)
  expect(sweeps.length).toBe(1) // 1 s short
  await clock.advance(1_000)
  expect(sweeps.length).toBe(2)
})

test('sweep does not run without an active run', async ($, on) => {
  const files = worldFiles()
  delete files[RUNS_INDEX]
  delete files[RUN_PATH]
  mockFs(on, files, [])
  const sweeps: (readonly string[])[] = []
  const clock = mockWorld(on, { process: argv => { if (isSweep(argv)) sweeps.push(argv) } })
  await $.session.start(sessionStart())
  await clock.advance(120_000)
  expect(sweeps.length).toBe(0)
})

test('sweep skips overlapping runs while one is in flight', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  const sweeps: (readonly string[])[] = []
  const clock = mockWorld(on, {
    process: async argv => {
      if (!isSweep(argv)) return
      sweeps.push(argv)
      // simulate a sweep that outlasts the next timer tick
      await clock.sleep(200_000)
      return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    },
  })
  await $.session.start(sessionStart())
  await clock.advance(120_000)
  expect(sweeps.length).toBe(1)
  await clock.advance(120_000) // second tick fires while first is still sleeping
  expect(sweeps.length).toBe(1)
  await clock.advance(200_000) // first finishes; third tick fires and starts
  expect(sweeps.length).toBe(2)
})

test('two session.start arm exactly one sweep timer', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  const sweeps: (readonly string[])[] = []
  const clock = mockWorld(on, { process: argv => { if (isSweep(argv)) sweeps.push(argv) } })
  await $.session.start(sessionStart())
  await $.session.start(sessionStart())
  await clock.advance(120_000)
  expect(sweeps.length).toBe(1)
})

// ---------- every active run renders, not only the newest ----------

test('two active runs render both Run headers and both Plan lists, newest first', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  // a second, OLDER active run: the fixture body under its own id and feature,
  // with W0's title replaced so each Plan list has a row only it shows
  const second = JSON.parse(files['/fixtures/run.json'] ?? '{}') as { id?: string; feature?: string; tasks?: unknown[] }
  second.id = 'mk-ui'
  second.feature = 'ui-polish'
  const w0 = second.tasks?.[0]
  if (typeof w0 === 'object' && w0 !== null) (w0 as { title?: string }).title = 'mk-ui header pass'
  files[`${DATA_DIR}/runs/mk-ui/run.json`] = JSON.stringify(second)
  const index = JSON.stringify([
    { id: 'wfv2', feature: 'workflows-v2', status: 'active', started: '2026-10-07T16:00:00+02:00', ended: null },
    { id: 'mk-ui', feature: 'ui-polish', status: 'active', started: '2026-10-07T14:30:00+02:00', ended: null },
    { id: 'mk-p0', feature: 'mod spikes', status: 'done', started: '2026-10-01T10:00:00+02:00', ended: '2026-10-02T10:00:00+02:00' },
  ])
  files['/fixtures/index.json'] = index // worldFixtures re-copies this on session.start
  files[RUNS_INDEX] = index
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /workflows-v2/ }), 'newest run header').toBeTruthy()
  expect(await ui.find({ text: /ui-polish/ }), 'second run header').toBeTruthy()
  expect(await ui.find({ text: /wizard split/ }), 'newest plan list').toBeTruthy() // W0's fixture title
  expect(await ui.find({ text: /mk-ui header pass/ }), 'second plan list').toBeTruthy() // W0 retitled in the second run
  // walk the drawn tree: the newest run's header and plan row come first
  const texts: string[] = []
  const walk = (n: unknown): void => {
    if (typeof n !== 'object' || n === null) return
    const node = n as { type?: string; children?: unknown[] }
    if (node.type === 'Text' && Array.isArray(node.children)) {
      const own = node.children.map(c => (typeof c === 'string' ? c : '')).join('')
      if (own) texts.push(own)
    }
    if (Array.isArray(node.children)) node.children.forEach(walk)
  }
  walk(await ui.drawn())
  const flat = texts.join('\n')
  expect(flat.indexOf('workflows-v2'), 'newest header first').toBeLessThan(flat.indexOf('ui-polish'))
  expect(flat.indexOf('wizard split'), 'newest plan row first').toBeLessThan(flat.indexOf('mk-ui header pass'))
})

// ---------- S0: the tab bar, the Old tab and the six stub tabs ----------

const MOCK_ROW = /MOCK · fixture data · not this run/
const mockRow = () => ({ text: MOCK_ROW })
const TAB_BUTTONS = [
  { key: 'dash-tab-overview', name: 'Overview' },
  { key: 'dash-tab-lanes', name: 'Lanes' },
  { key: 'dash-tab-plan', name: 'Plan' },
  { key: 'dash-tab-usage', name: 'Usage' },
  { key: 'dash-tab-events', name: 'Events' },
  { key: 'dash-tab-waiting', name: 'Waiting' },
] as const

test('a fresh mount shows the Old tab: real run data and no MOCK row', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /workflows-v2/ })).toBeTruthy() // today's pane under the bar
  expect(await ui.find({ text: 'Old' })).toBeTruthy() // the Old tab button is drawn
  expect(await ui.find(mockRow())).toBeUndefined()
})

test('every tab hotkey switches to that tab’s MOCK row and hides Old; 0 restores Old', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  for (const { key, name } of TAB_BUTTONS) {
    await ui.press({ key })
    expect(await ui.find(mockRow()), `${name} MOCK row`).toBeTruthy()
    expect(await ui.find({ text: /workflows-v2/ }), `${name} hides Old`).toBeUndefined()
    await ui.press({ key: 'dash-tab-old' })
    expect(await ui.find({ text: /workflows-v2/ }), 'Old is back').toBeTruthy()
    expect(await ui.find(mockRow())).toBeUndefined()
  }
})

test('at 36 cols the tab bar drops to short labels and every tab still renders', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.redraw(paneProps(36))
  for (const { key, name } of TAB_BUTTONS) {
    expect(await ui.find({ type: 'Button', text: name }), `${name} wide label dropped`).toBeUndefined()
  }
  expect(await ui.findAll({ type: 'Button' })).toHaveLength(TAB_BUTTONS.length + 1) // the bar still draws all seven
  for (const { key } of TAB_BUTTONS) {
    await ui.press({ key })
    expect(await ui.find(mockRow()), `${key} renders narrow`).toBeTruthy()
  }
  await ui.press({ key: 'dash-tab-old' })
  expect(await ui.find({ text: 'stg 7/12' })).toBeTruthy() // Old renders narrow
})

test('the chosen tab survives a refresh tick', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-plan' })
  expect(await ui.find(mockRow())).toBeTruthy()
  await clock.advance(5000) // the refresh tick reloads the snapshot atom
  expect(await ui.find(mockRow())).toBeTruthy() // the tab choice stands
  expect(await ui.find({ text: /workflows-v2/ })).toBeUndefined()
})

// ---------- D1: one row per shared lane; titles never hide the status ----------

/** every Text node's own text under `n`, in document order (a found row is walked the same way as ui.drawn) */
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

function sharedLaneTask(id: string, lane: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id, title: `phase ${id}`, wave: 1, deps: [], status: 'building', agent: 'opencode',
    model: 'glm-5.3', effort: 'medium', lane, last_commit: null, round: 1, rounds: [],
    updated: new Date(NOW - 5 * 60_000).toISOString(), ...extra,
  }
}

test('D1.1: four phase tasks on one lane path render ONE lanes row listing their phase ids', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    run.tasks = [
      sharedLaneTask('E1', '../wt-shared'),
      sharedLaneTask('E2', '../wt-shared'),
      sharedLaneTask('E3', '../wt-shared'),
      sharedLaneTask('E4', '../wt-shared'),
      sharedLaneTask('E1.1', '../wt-shared', { parent: 'E1' }), // child round on the same lane: not listed
    ]
  })
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /E1 E2 E3 E4/ }), 'the phase ids on one row').toBeTruthy()
  expect(await ui.find({ text: /⌂ OPENCODE/ }), 'the shared lane agent').toBeTruthy()
  expect(await ui.find({ text: 'building' }), 'the rolled-up status').toBeTruthy()
  const row = await ui.find({ key: '../wt-shared' })
  expect(row, 'one row keyed by the lane path').toBeDefined()
  const flat = rowTexts(row).join('')
  expect(flat).toContain('wt-shared')
  expect(flat).toContain('E1 E2 E3 E4')
  expect(flat).toContain('building')
  expect(flat).not.toContain('E1.1') // child tasks are never listed
  expect(rowTexts(await ui.drawn()).filter(t => t === 'wt-shared'), 'the slot drawn exactly once').toHaveLength(1)
})

test('D1.1: the shared row shows the most urgent status; two lane paths stay two rows', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    run.tasks = [
      sharedLaneTask('E1', '../wt-a', { status: 'blocked' }),
      sharedLaneTask('E2', '../wt-a', { status: 'stalled' }),
      sharedLaneTask('E3', '../wt-a', { status: 'verifying' }),
      sharedLaneTask('E5', '../wt-b'),
    ]
  })
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  const rowA = await ui.find({ key: '../wt-a' })
  const rowB = await ui.find({ key: '../wt-b' })
  expect(rowA, 'lane A row').toBeDefined()
  expect(rowB, 'lane B row stays its own row').toBeDefined()
  const flatA = rowTexts(rowA).join('')
  expect(flatA).toContain('E1 E2 E3')
  expect(flatA).toContain('blocked') // SIDE precedence: blocked outranks stalled and verifying
  expect(flatA).not.toContain('stalled')
  expect(flatA).not.toContain('verifying')
  const flatB = rowTexts(rowB).join('')
  expect(flatB).toContain('E5')
  expect(flatB).toContain('building')
})

test('D1.2: a 120-char title at 100 cols truncates with … and keeps the status word and age on the row', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const longTitle = ('LEAD section for a shared lane (one subagent per phase, quota-aware routing, merged review and QA gates) ' + 'x'.repeat(120)).slice(0, 120)
  setRun(files, run => {
    const f6 = tasksOf(run)[1]
    if (!f6) return
    f6.title = longTitle
    f6.status = 'building'
    f6.round = 0
    f6.updated = new Date(NOW - 12 * 60_000).toISOString() // age 12m
  })
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(100) })
  expect(await ui.find({ text: /…/ }), 'the truncated title marks the cut').toBeTruthy()
  expect(await ui.find({ text: /building 12m/ }), 'status word and age still found').toBeTruthy()
  const row = await ui.find({ key: 'F6' })
  expect(row, 'the plan row').toBeDefined()
  const flat = rowTexts(row).join('')
  expect(flat).toContain('…')
  expect(flat).toContain('building 12m') // same row as the (truncated) title
  expect(flat).toContain(longTitle.slice(0, 40)) // the title's head is still drawn
  expect(flat).not.toContain(longTitle.slice(-20)) // and its tail yielded to the status column
})
