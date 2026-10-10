// Dashboard pane tests (design §3): base 46-col layout on both surfaces,
// compact 36-col, inline hint, first-run and no-run states. Fixtures arrive
// via $.fs.write (inline fixtures plugin); fs is served in memory.
import { test, expect } from 'claude-code/testing'
import type { On, ConfigRow } from 'claude-code'
import { DATA_DIR, NOW, PROJECT_DIR, RUN_PATH, RUNS_INDEX, USAGE_PATH, agentOf, agentsOf, mockFs, mockWorld, paneProps, sessionStart, sessionStartIn, setRun, setUsage, tasksOf, worldFiles, worldFixtures } from './testkit'
import type { Files, MockWorld } from './testkit'
import { parseYamlAgents } from './data'
import { mockTabCounts, waitingCount } from './tabs/header'

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
  { key: 'dash-tab-settings', name: 'Settings' },
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
  // Settings is live since S3 (loading line, then live rows) — the S3 tests cover it.
  for (const { key, name } of TAB_BUTTONS.filter(t => t.key !== 'dash-tab-settings')) {
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
  // Settings is live since S3 (loading line, then live rows) — the S3 tests cover it.
  for (const { key, name } of TAB_BUTTONS.filter(t => t.key !== 'dash-tab-settings')) {
    expect(await ui.find({ type: 'Button', text: name }), `${name} wide label dropped`).toBeUndefined()
  }
  expect(await ui.findAll({ type: 'Button' })).toHaveLength(TAB_BUTTONS.length + 1) // the bar still draws all seven
  for (const { key } of TAB_BUTTONS.filter(t => t.key !== 'dash-tab-settings')) {
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

// ---------- V0.4: the run header on every tab and the counted tab bar ----------

/** every tab-bar Button's label, in bar order. A pane body may draw its own
 *  Buttons (V2's lane controls, the tabs' option buttons): only the bar's own
 *  `dash-tab-*` keys name tabs. */
const tabLabels = async (ui: { findAll: (q: { type: string }) => Promise<{ props: Record<string, unknown> }[]> }): Promise<string[]> =>
  (await ui.findAll({ type: 'Button' }))
    .filter(b => String(b.props.key).startsWith('dash-tab-'))
    .map(b => String(b.props.label))

test('the mock tab counts read the fixtures: lanes, plan done/total, waiting only when open', async () => {
  expect(mockTabCounts()).toEqual({ overview: '', lanes: '4', plan: '3/8', usage: '', events: '', waiting: '!1', old: '', settings: '' })
  expect(waitingCount(0)).toBe('') // LOW: a fixture with no open decision shows no count, never `!0`
  expect(waitingCount(2)).toBe('!2')
})

test('the run header sits above the tab bar on Old: name, stage chip and ▶ Next', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: 'workflows-v2' }), 'run name').toBeTruthy()
  expect(await ui.find({ text: /7\/12 Verify/ }), 'stage chip').toBeTruthy()
  expect(await ui.find({ text: /▶ verify F6 gates/ }), 'the ▶ Next line').toBeTruthy()
  expect(await ui.find({ text: ' MOCK ' }), 'Old is not marked MOCK').toBeUndefined()
  expect(await ui.find({ text: /main@82252eb/ }), 'the Old pane under the header is unchanged').toBeTruthy()
  expect(await ui.find({ text: '▶ Next: ' }), 'the Run section still prints its own Next label').toBeTruthy()
})

test('every mock tab shows the fixture run header: name, stage chip, MOCK marker and ▶ Next', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  // Settings is live since S3 (loading line, then live rows) — the S3 tests cover it.
  for (const { key, name } of TAB_BUTTONS.filter(t => t.key !== 'dash-tab-settings')) {
    await ui.press({ key: 'dash-tab-old' })
    await ui.press({ key })
    expect(await ui.find({ text: 'Checkout v2' }), `${name} run name`).toBeTruthy()
    expect(await ui.find({ text: /6\/12 Watch/ }), `${name} stage chip`).toBeTruthy()
    expect(await ui.find({ text: ' MOCK ' }), `${name} MOCK marker`).toBeTruthy()
    expect(await ui.find({ text: /▶ watching P4 R1/ }), `${name} ▶ Next`).toBeTruthy()
    expect(await ui.find({ text: /workflows-v2/ }), `${name} hides Old`).toBeUndefined()
  }
})

test('F0: every tab Button carries its keyboard hotkey — o l p u e w 0, at 36 and 46 cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  for (const cols of [46, 36] as const) {
    await ui.press({ key: 'dash-tab-old' })
    await ui.redraw(paneProps(cols))
    const buttons = await ui.findAll({ type: 'Button' })
    expect(buttons.map(b => b.props.hotkey), `hotkeys at ${cols}`).toEqual(['o', 'l', 'p', 'u', 'e', 'w', '0', '9'])
    for (const b of buttons) {
      // an inactive tab's label is its count alone (or the `·` placeholder), so
      // a plain Button's `h: label` drawing never repeats the tab's own letter
      expect(String(b.props.label), `${String(b.props.hotkey)} label at ${cols}`).not.toBe(String(b.props.hotkey))
      expect(String(b.props.label).length, `${String(b.props.hotkey)} label at ${cols}`).toBeGreaterThan(0)
    }
    for (const row of barRows(buttons, cols)) {
      expect(row, `bar row at ${cols}`).toBeLessThanOrEqual(cols)
    }
  }
  // the press-by-key switch loop stands
  // Settings is live since S3 (loading line, then live rows) — the S3 tests cover it.
  for (const { key, name } of TAB_BUTTONS.filter(t => t.key !== 'dash-tab-settings')) {
    await ui.redraw(paneProps(46))
    await ui.press({ key })
    expect(await ui.find(mockRow()), `${name} MOCK row`).toBeTruthy()
    await ui.press({ key: 'dash-tab-old' })
    expect(await ui.find({ text: /workflows-v2/ }), 'Old is back').toBeTruthy()
  }
})

/** the bar's wrapped rows: the engine draws a plain Button `h: label` and a
 *  non-plain one `[ label ]`, with a one-cell gap between the buttons. */
function chipWidth(b: { props: Record<string, unknown> }): number {
  const label = String(b.props.label ?? '')
  return b.props.plain === true ? `${String(b.props.hotkey ?? '')}: ${label}`.length : `[ ${label} ]`.length
}

function barRows(buttons: readonly { props: Record<string, unknown> }[], cols: number): number[] {
  const rows: number[] = []
  let row = 0
  for (const b of buttons) {
    const w = chipWidth(b)
    row = row === 0 ? w : row + 1 + w > cols ? (rows.push(row), w) : row + 1 + w
  }
  if (row > 0) rows.push(row)
  return rows
}

test('the tab bar shows the active word and count, the rest hotkey + count, at 46 and at 36 cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  // Old active: the counts come from the snapshot — 3 live lanes, 1/5 done, 1 open decision
  expect(await tabLabels(ui)).toEqual(['·', '3', '1/5', '·', '·', '!1', 'Old', '·'])
  for (const cols of [36, 46] as const) {
    await ui.press({ key: 'dash-tab-old' })
    await ui.press({ key: 'dash-tab-lanes' })
    await ui.redraw(paneProps(cols))
    const labels = await tabLabels(ui)
    expect(labels, `at ${cols}`).toEqual(['·', 'Lanes 4', '3/8', '·', '·', '!1', '·', '·'])
    expect(labels.join(' ')).not.toContain('o: o') // the S0 short labels are gone
    expect(labels.some(l => /^[a-z0-9]$/.test(l)), 'no tab shows its own letter twice').toBe(false)
    expect(labels.filter(l => l === '·').length, 'count-less tabs carry the placeholder').toBe(5)
  }
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

// ---------- V1.4: the rule under the tab bar, and C12: the view state ----------

/** every drawn Text's own text (dashboard.test.ts's rowTexts walk). */
const allTexts = async (ui: { drawn: () => Promise<unknown> }): Promise<string[]> => rowTexts(await ui.drawn())

/** every node of `type` under `n`, in document order. */
function nodesOf(n: unknown, type: string): { type: string; props: Record<string, unknown>; children: unknown[] }[] {
  const out: { type: string; props: Record<string, unknown>; children: unknown[] }[] = []
  const walk = (node: unknown): void => {
    if (typeof node !== 'object' || node === null) return
    const cur = node as { type?: string; props?: Record<string, unknown>; children?: unknown[] }
    if (cur.type === type) out.push({ type: cur.type, props: cur.props ?? {}, children: cur.children ?? [] })
    for (const c of cur.children ?? []) walk(c)
  }
  walk(n)
  return out
}

/** the rule Text's parent Box and that Box's children: the rule must sit
 *  directly under the tab bar (a gap-less wrapper, bar then rule). */
function ruleSiblings(drawn: unknown): { parent: Record<string, unknown>; kids: unknown[] } | undefined {
  const isRule = (n: unknown): boolean => {
    if (typeof n !== 'object' || n === null) return false
    const node = n as { type?: string; children?: unknown[] }
    if (node.type !== 'Text') return false
    const own = (node.children ?? []).map(c => (typeof c === 'string' ? c : '')).join('')
    return /^─+$/.test(own)
  }
  const walk = (n: unknown): { parent: Record<string, unknown>; kids: unknown[] } | undefined => {
    if (typeof n !== 'object' || n === null) return undefined
    const cur = n as { type?: string; props?: Record<string, unknown>; children?: unknown[] }
    const kids = cur.children ?? []
    if (kids.some(isRule)) return { parent: cur.props ?? {}, kids }
    for (const c of kids) { const hit = walk(c); if (hit != null) return hit }
    return undefined
  }
  return walk(drawn)
}

test('V1.4: a full-width rule of the palette line colour sits directly under the tab bar on every tab', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  for (const cols of [36, 46] as const) {
    await ui.press({ key: 'dash-tab-old' })
    await ui.redraw(paneProps(cols))
    // Old carries the rule too: the frame is the header + bar, not the tab body
    expect(await ui.find({ text: new RegExp(`^─{${String(cols)}}$`) }), `the rule on Old at ${cols}`).toBeTruthy()
    expect((await allTexts(ui)).filter(t => /^─+$/.test(t)), `exactly one rule at ${cols}`).toHaveLength(1)
    // flush: the rule's parent is a column Box with no gap that holds the tab
    // bar and the rule, and nothing else
    const wrap = ruleSiblings(await ui.drawn())
    expect(wrap, `the rule's wrapper at ${cols}`).toBeDefined()
    expect(wrap?.parent.flexDirection, `the wrapper is a column at ${cols}`).toBe('column')
    expect(wrap?.parent.gap, `no gap inside the wrapper at ${cols}`).toBeUndefined()
    expect(wrap?.kids, `bar then rule at ${cols}`).toHaveLength(2)
    const bar = nodesOf(wrap?.kids[0], 'Button')
    expect(bar.map(b => String(b.props.key)), `the bar is the first child at ${cols}`)
      .toEqual(['dash-tab-overview', 'dash-tab-lanes', 'dash-tab-plan', 'dash-tab-usage', 'dash-tab-events', 'dash-tab-waiting', 'dash-tab-old', 'dash-tab-settings'])
    // Settings is live since S3 (loading line, then live rows) — the S3 tests cover it.
    for (const { key, name } of TAB_BUTTONS.filter(t => t.key !== 'dash-tab-settings')) {
      await ui.press({ key: 'dash-tab-old' })
      await ui.press({ key })
      expect(await ui.find({ text: new RegExp(`^─{${String(cols)}}$`) }), `${name} rule at ${cols}`).toBeTruthy()
    }
  }
})

test('C12: the view state starts empty — every phase folded — and only a press opens one', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-plan' })
  // the empty default: no subtask tree is drawn before any press
  expect(await ui.find({ text: /R1 stripe adapter/ }), 'nothing folded at the default').toBeUndefined()
  expect(await ui.find({ text: /│ ├/ }), 'no tree rows at the default').toBeUndefined()
  await ui.press({ key: 'plan-fold-P4' })
  expect(await ui.find({ text: /R1 stripe adapter/ }), 'the press opened the phase').toBeTruthy()
})

test('C12: every tab call receives the ui handle — all six tabs act on it, not just Overview and Plan', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  // the three-argument tabs (lanes, usage, waiting, events) take the extra
  // argument through the pane's widened call and still render their fixture
  // Settings is live since S3 (loading line, then live rows) — the S3 tests cover it.
  for (const { key, name } of TAB_BUTTONS.filter(t => t.key !== 'dash-tab-settings')) {
    await ui.press({ key: 'dash-tab-old' })
    await ui.press({ key })
    expect(await ui.find(mockRow()), `${name} renders with the ui argument`).toBeTruthy()
  }
  // Overview: the ⋯ reveal reads its flag back from ui and draws the rest
  await ui.press({ key: 'dash-tab-overview' })
  expect(await ui.find({ text: /b Start on main, rebase later/ }), 'hidden before the press').toBeUndefined()
  await ui.press({ key: 'waiting-opt-a-more-2' })
  expect(await ui.find({ text: /b Start on main, rebase later/ }), 'the Overview reveal after the press').toBeTruthy()
  // Plan: the phase rows fold through ui
  await ui.press({ key: 'dash-tab-plan' })
  expect(await ui.find({ text: /R1 stripe adapter/ }), 'folded to start').toBeUndefined()
  await ui.press({ key: 'plan-fold-P4' })
  expect(await ui.find({ text: /R1 stripe adapter/ }), 'the fold reaches the Plan tab through ui').toBeTruthy()
  // Lanes: a lane's pause control re-renders that lane with its resume button
  await ui.press({ key: 'dash-tab-lanes' })
  expect(await ui.find({ type: 'Button', key: 'p4-resume' }), 'a building lane offers pause, not resume').toBeUndefined()
  await ui.press({ key: 'p4-pause' })
  expect(await ui.find({ type: 'Button', key: 'p4-resume' }), 'the paused lane offers resume').toBeTruthy()
  // Waiting: ⋯ reveals the folded actions; a pick marks the option it picked
  await ui.press({ key: 'dash-tab-waiting' })
  const labels = await tabLabels(ui)
  expect(labels, 'the tab bar keeps its own labels while a body draws buttons').toHaveLength(8)
  await ui.press({ key: 'waiting-a-more-3' })
  expect(await ui.find({ type: 'Button', text: /Rebase later/ }), 'the revealed option button').toBeTruthy()
  await ui.press({ key: 'waiting-b' })
  expect(await ui.find({ type: 'Button', text: /Rebase later ✓/ }), 'the picked option is marked').toBeTruthy()
})

// S2.0 answer (host types: Elements[terminal|desktop|vscode] carry Input and
// Select; Elements[mobile] carries neither): YES on terminal + desktop. Text
// rows therefore use the native Input; a Select covers constrained values.
// The probe mounts a Pane whose render records `'Input' in els` /
// `'Select' in els` straight off `$.ui.resolve(e)` — the same call the pane
// and the wizard make — so the test keeps passing whichever way the engine
// answers, while asserting and naming today's answer.
test('S2.0 spike: $.ui.resolve(e) exposes Input and Select on terminal + desktop', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  const seen: { surface: string; input: boolean; select: boolean }[] = []
  on('ui.render', { component: 'Pane', requestId: 'mk-spike-probe' }, async ($probe, e) => {
    const els = $probe.ui.resolve(e) as unknown as Record<string, unknown>
    seen.push({ surface: String(e.surface), input: 'Input' in els, select: 'Select' in els })
    const Text = els['Text'] as (props: { children: unknown }) => never
    return Text({ children: 'spike' })
  })
  await $.session.start(sessionStart())
  for (const surface of ['terminal', 'desktop'] as const) {
    await $.ui.mount({ plugin: 'matrixkoder', surface, component: 'Pane', requestId: 'mk-spike-probe', props: paneProps(46) })
  }
  expect(seen.map(s => s.surface).sort()).toEqual(['desktop', 'terminal'])
  for (const s of seen) {
    expect(s.input, `${s.surface} exposes Input`).toBe(true)
    expect(s.select, `${s.surface} exposes Select`).toBe(true)
  }
})

// ---------- S2.3: settings wiring — hotkey 9, hiddenTabs, defaultTab, raster=false, solo ----------

test('S2.3: hotkey 9 switches to the Settings tab and hides Old', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const clock = mockSettingsBackend(on, files, calls, R3_FIELDS)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  const nine = (await ui.findAll({ type: 'Button' })).find(b => b.props.hotkey === '9')
  expect(nine, 'the bar carries hotkey 9').toBeDefined()
  expect(String(nine?.props.key)).toBe('dash-tab-settings')
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls)
  expect(await ui.find({ text: /MOCK · fixture data/ }), 'never mock rows').toBeUndefined()
  expect((await ui.findAll({ type: 'Button' })).find(b => b.props.key === 'set-scope-mode'), 'the settings scope bar').toBeTruthy()
  expect(await ui.find({ text: /Checkout v2/ }), 'the real run header, never the mock world').toBeUndefined()
  await ui.press({ key: 'dash-tab-old' })
  expect(await ui.find({ text: /workflows-v2/ }), 'Old is back').toBeTruthy()
})

test('S2.3: hiddenTabs hides named tabs but never Settings', { plugins: [worldFixtures], options: { hiddenTabs: 'lanes,plan' } }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  const keys = (await ui.findAll({ type: 'Button' })).map(b => String(b.props.key))
  expect(keys).not.toContain('dash-tab-lanes')
  expect(keys).not.toContain('dash-tab-plan')
  expect(keys).toContain('dash-tab-settings')
  expect(keys).toContain('dash-tab-old')
})

test('S2.3: hiddenTabs naming Settings still draws it', { plugins: [worldFixtures], options: { hiddenTabs: 'settings,lanes' } }, async ($, on) => {
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const clock = mockSettingsBackend(on, files, calls, R3_FIELDS)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  const keys = (await ui.findAll({ type: 'Button' })).map(b => String(b.props.key))
  expect(keys).toContain('dash-tab-settings')
  expect(keys).not.toContain('dash-tab-lanes')
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls)
  expect(await ui.find({ text: /MOCK · fixture data/ }), 'Settings renders live while named hidden').toBeUndefined()
  expect((await ui.findAll({ type: 'Button' })).find(b => b.props.key === 'set-scope-mode'), 'the settings scope bar').toBeTruthy()
})

test('S2.3: defaultTab opens the pane on that tab', { plugins: [worldFixtures], options: { defaultTab: 'settings' } }, async ($, on) => {
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const clock = mockSettingsBackend(on, files, calls, R3_FIELDS)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await loadLiveSettings(clock, ui, calls) // the render fallback loads it, no press needed
  expect(await ui.find({ text: /MOCK · fixture data/ }), 'never mock rows').toBeUndefined()
  expect((await ui.findAll({ type: 'Button' })).find(b => b.props.key === 'set-scope-mode'), 'the settings scope bar').toBeTruthy()
  expect(await ui.find({ text: /Checkout v2/ }), 'the real run header, never the mock world').toBeUndefined()
})

test('S2.3: an unknown or hidden defaultTab keeps the Old default', { plugins: [worldFixtures], options: { defaultTab: 'nope' } }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /workflows-v2/ }), 'Old stays the default').toBeTruthy()
  expect(await ui.find(mockRow())).toBeUndefined()
})

test('S2.3: a hidden defaultTab falls back to Old', { plugins: [worldFixtures], options: { defaultTab: 'lanes', hiddenTabs: 'lanes' } }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /workflows-v2/ }), 'Old stays the default').toBeTruthy()
})

test('S2.3: raster:false draws the eighth-block fallback (no Raster); default draws Raster', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-plan' })
  expect(await ui.findAll({ type: 'Raster' }), 'the live wave strip is a Raster by default').not.toHaveLength(0)
})

test('S2.3: raster:false draws the eighth-block fallback (no Raster)', { plugins: [worldFixtures], options: { raster: false } }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-plan' })
  expect(await ui.findAll({ type: 'Raster' }), 'no Raster anywhere under raster:false').toHaveLength(0)
  expect(await ui.find({ text: /Payment provider adapter/ }), 'the live wave still renders').toBeTruthy()
  // R3 LOW 2: a second bar-drawing mock tab (Usage draws pickBar strips) falls
  // back too. Old intentionally keeps `els`: OldTab never calls pickBar today
  // (text bars only), so there is nothing to fall back — out of scope.
  await ui.press({ key: 'dash-tab-usage' })
  expect(await ui.findAll({ type: 'Raster' }), 'no Raster on Usage either').toHaveLength(0)
  expect(await ui.find(mockRow()), 'the Usage mock world still renders').toBeTruthy()
})

test('S2.3: mode solo skips the lane sweep', { plugins: [worldFixtures], options: { mode: 'solo' } }, async ($, on) => {
  mockFs(on, worldFiles())
  const sweeps: (readonly string[])[] = []
  const clock = mockWorld(on, { process: argv => { if (isSweep(argv)) sweeps.push(argv) } })
  await $.session.start(sessionStart())
  await clock.advance(120_000)
  expect(sweeps, 'no sweep in solo mode with an active run').toHaveLength(0)
})

// ---------- R3: review-r1 fixes — real row selection, dirty vs loaded, width, typing hotkeys ----------

// R3 MEDIUM 2's measure: every Text and every horizontal Box in the drawn
// tree, card-aware (plan.test.ts's discipline — a row inside a kit Card owes
// cols - 4). Buttons count their drawn chrome; Inputs, Selects and Rasters
// count 0 (the engine lays those out).
type R3DrawnNode = { type?: string; props?: { key?: string; flexDirection?: string; gap?: number; borderStyle?: string; label?: string; plain?: boolean }; children?: unknown[] }

function r3TextWidth(node: unknown): number {
  if (typeof node === 'string') return node.length
  if (typeof node !== 'object' || node === null) return 0
  const n = node as R3DrawnNode
  if (n.type !== 'Text') return 0
  return (n.children ?? []).reduce((sum: number, c) => sum + r3TextWidth(c), 0)
}

function r3WidthOf(node: unknown): number {
  if (typeof node !== 'object' || node === null) return 0
  const n = node as R3DrawnNode
  if (n.type === 'Text') return r3TextWidth(n)
  if (n.type === 'Button') return String(n.props?.label ?? '').length + (n.props?.plain ? 2 : 4)
  if (n.type === 'Input' || n.type === 'Select' || n.type === 'Raster') return 0
  const kids = n.children ?? []
  if (n.type === 'Box' && n.props?.flexDirection === 'row') {
    return kids.reduce((sum: number, c) => sum + r3WidthOf(c), 0) + (typeof n.props.gap === 'number' ? n.props.gap : 0) * Math.max(0, kids.length - 1)
  }
  return kids.reduce((max: number, c) => Math.max(max, r3WidthOf(c)), 0)
}

function drawnRowWidths(root: unknown): { width: number; inCard: boolean; text: string }[] {
  const out: { width: number; inCard: boolean; text: string }[] = []
  const preview = (node: unknown): string => {
    if (typeof node === 'string') return node
    if (typeof node !== 'object' || node === null) return ''
    const n = node as R3DrawnNode
    if (n.type === 'Button') return `[${String(n.props?.label ?? '')}]`
    return (n.children ?? []).map(preview).join('')
  }
  // the main tab bar wraps visually (flexWrap), so the tree never gives its
  // drawn rows — skip it (the F0 barRows test owns bar wrapping); every other
  // row Box, including the settings scope bar's pre-wrapped rows, is measured.
  const isTabBar = (node: R3DrawnNode): boolean =>
    (node.children ?? []).some(c =>
      typeof c === 'object' && c !== null && (c as R3DrawnNode).type === 'Button' &&
      String((c as R3DrawnNode).props?.key ?? '').startsWith('dash-tab-'))
  const walk = (node: unknown, inCard: boolean): void => {
    if (typeof node !== 'object' || node === null) return
    const n = node as R3DrawnNode
    const card = inCard || (n.type === 'Box' && n.props?.borderStyle === 'round')
    if (n.type === 'Box' && n.props?.flexDirection === 'row' && isTabBar(n)) {
      for (const c of n.children ?? []) walk(c, card)
      return
    }
    if (n.type === 'Text' || (n.type === 'Box' && n.props?.flexDirection === 'row')) {
      out.push({ width: r3WidthOf(node), inCard: card, text: preview(node).slice(0, 60) })
    }
    for (const c of n.children ?? []) walk(c, card)
  }
  walk(root, false)
  return out
}

// R3 HIGH 1: the row label is a real press target. Pressing it through the
// mounted pane selects the row (help + `default X · r reset` appear); `r`
// resets it. On the R1 code there is no `-pick` press, so this fails there.
test('R3 HIGH 1: pressing a row label selects it — help + default + r reset; r resets', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const clock = mockSettingsBackend(on, files, calls, R3_FIELDS)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls)
  await ui.press({ key: 'set-scope-routing' })
  // Max build lanes loads 4 over a default of 3: clean until edited
  expect(await ui.find({ text: /unsaved/ })).toBeUndefined()
  await ui.press({ key: 'set-routing_preferences.max_build_lanes-pick' })
  expect(await ui.find({ text: /Build lanes th/ }), 'the selected row help line').toBeTruthy()
  expect(await ui.find({ text: /default 3/ }), 'the default tail').toBeTruthy()
  expect(await ui.find({ type: 'Button', key: 'set-routing_preferences.max_build_lanes-reset' }), 'the r reset press').toBeTruthy()
  await ui.press({ key: 'set-routing_preferences.max_build_lanes-reset' })
  expect(await ui.find({ text: /● 1 unsaved/ }), 'reset wrote the default as an edit (it differs from loaded 4)').toBeTruthy()
})

// R3 MEDIUM 1 (+LOW 4): dirty tracks the loaded value, not the default.
// Stepping away dirties; stepping back to the loaded value clears. On the R1
// code the way back still counts unsaved (edit 4 ≠ default 3), so this fails.
test('R3 MEDIUM 1: value≠default starts clean; editing back to loaded clears ●', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const clock = mockSettingsBackend(on, files, calls, R3_FIELDS)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls)
  await ui.press({ key: 'set-scope-routing' })
  expect(await ui.find({ text: /unsaved/ }), 'loaded 4 over default 3 is clean').toBeUndefined()
  await ui.press({ key: 'set-routing_preferences.max_build_lanes-inc' }) // 4 → 5
  expect(await ui.find({ text: /● 1 unsaved/ }), 'the step dirtied the row').toBeTruthy()
  await ui.press({ key: 'set-routing_preferences.max_build_lanes-dec' }) // 5 → 4, back to loaded
  expect(await ui.find({ text: /unsaved/ }), 'back to the loaded value is clean').toBeUndefined()
  const pick = await ui.find({ type: 'Button', key: 'set-routing_preferences.max_build_lanes-pick' }) as { props: { label?: string } } | undefined
  expect(pick, 'the row press still stands').toBeDefined()
  expect(String(pick?.props.label ?? ''), 'no changed marker on the label').toBe('Max build lanes')
})

// R3 MEDIUM 3: while the find field holds the edit mode, the tab drops its
// letter/digit hotkeys, so typing `s` filters but never saves. The engine
// holds printable keys in a focused Input anyway
// (.claude-plugin/types/claude-code/index.d.ts:5437-5439: "while it has focus
// every printable key reaches it alone"); this test pins the tab's half: the
// hotkeys are gone in find mode and back out of it. On the R1 code the save
// press still carries hotkey `s`, so this fails there.
test('R3 MEDIUM 3: typing s in find mode filters and never saves', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const clock = mockSettingsBackend(on, files, calls, R3_FIELDS)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls)
  await ui.press({ key: 'set-scope-routing' })
  await ui.press({ key: 'set-routing_preferences.max_build_lanes-inc' }) // an edit to protect
  expect(await ui.find({ text: /● 1 unsaved/ })).toBeTruthy()
  await ui.press({ key: 'set-find' }) // enter find mode
  const hotkeyOf = async (key: string): Promise<unknown> =>
    (await ui.find({ type: 'Button', key }) as { props: { hotkey?: string } } | undefined)?.props.hotkey
  expect(await hotkeyOf('set-save'), 'no s hotkey in find mode').toBeUndefined()
  expect(await hotkeyOf('set-discard'), 'no x hotkey in find mode').toBeUndefined()
  expect(await hotkeyOf('set-scope-routing'), 'no digit hotkey in find mode').toBeUndefined()
  await ui.input({ key: 'set-find-input', text: 'stall' }) // types an `s`
  expect(await ui.find({ text: /● 1 unsaved/ }), 'the edit survived typing').toBeTruthy()
  await ui.press({ key: 'set-find' }) // leave find mode
  expect(await hotkeyOf('set-save'), 's save is back out of find mode').toBe('s')
})

// R3 MEDIUM 2: the long-default help line (Code reviewer defaults to
// `claude sonnet medium`) fits 46 and 36 cols — every drawn row, card-aware.
// On the R1 code the help tail overflows, so this fails there.
test('R3 MEDIUM 2: the long-default help line fits 46 and 36 cols', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const clock = mockSettingsBackend(on, files, calls, R3_FIELDS)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls)
  await ui.press({ key: 'set-scope-routing' })
  await ui.press({ key: 'set-routing_preferences.reviewer-pick' })
  expect(await ui.find({ text: /default claude/ }), 'the clipped default tail').toBeTruthy()
  for (const cols of [46, 36] as const) {
    await ui.redraw(paneProps(cols))
    const wide = drawnRowWidths(await ui.drawn()).filter(w => (w.inCard ? w.width > cols - 4 : w.width > cols))
    expect(wide, `over-wide rows at ${cols} cols: ${JSON.stringify(wide)}`).toEqual([])
  }
})

// ---------- S3: Settings tab live load + save ----------

/** mk-config.py argv among the spawns (pollUsage's mk-usage.py excluded). */
const mkConfigCalls = (calls: (readonly string[])[]): string[][] =>
  calls.filter(a => a.some(x => x.endsWith('scripts/mk-config.py'))).map(a => [...a])

const YAML_PATH = `${DATA_DIR}/matrixkoder.yaml`

/** Two C1-shaped child rows the save test dirties through the steppers. */
const S3_FIELDS = [
  { key: 'session.stall_min', scope: 'child', label: 'Stall after', type: 'num', value: 20, default: 15, min: 5, max: 60, unit: 'm', help: 'No change flags a lane.', editable: true },
  { key: 'session.max_wait_min', scope: 'child', label: 'Give up after', type: 'num', value: 120, default: 30, min: 10, max: 300, unit: 'm', help: 'Give up on a silent lane.', editable: true },
]

/** Routing rows with the S2 fixture's values, for the R3 tests against live rows. */
const R3_FIELDS = [
  { key: 'routing_preferences.max_build_lanes', scope: 'routing', label: 'Max build lanes', type: 'num', value: 4, default: 3, min: 1, max: 6, help: 'Build lanes the router may open at once.', editable: true },
  { key: 'routing_preferences.reviewer', scope: 'routing', label: 'Code reviewer', type: 'select', value: 'claude sonnet medium', default: 'claude sonnet medium', opts: [['claude sonnet medium', 'pane'], ['opencode-go/kimi-k3', 'pane'], ['claude opus high', 'data-loss']], help: 'New pane in the lane.', editable: true },
]

type SettingsAnswer = { exitCode: number; stdout: string }

function asConfigRows(rows?: unknown[]): ConfigRow[] {
  return ((rows ?? []) as { key: string; value: boolean | string | number; isLocked?: boolean }[]).map(r => ({
    key: r.key,
    label: r.key,
    kind: typeof r.value === 'boolean' ? 'boolean' : typeof r.value === 'number' ? 'number' : 'text',
    value: r.value,
    provider: { plugin: 'matrixkoder', tier: 'user' },
    isLocked: r.isLocked ?? false,
  }))
}

/** A canned settings backend: the yaml exists, migrate ok, list answers
 *  `fields`, sets answer ok (or per-command via `setAnswer`). Also bottoms
 *  session.cwd and $.config.list (default no rows). Returns the mock clock. */
function mockSettingsBackend(
  on: On,
  files: Files,
  calls: (readonly string[])[],
  fields: unknown[],
  opts: { configRows?: unknown[]; setAnswer?: (cmd: string[]) => SettingsAnswer } = {},
): MockWorld {
  files[YAML_PATH] = 'session:\n  stall_min: 20\n'
  mockFs(on, files, [DATA_DIR, `${PROJECT_DIR}/.git`], [YAML_PATH])
  const clock = mockWorld(on, {
    process: argv => {
      calls.push(argv)
      if (!argv.some(a => a.endsWith('scripts/mk-config.py'))) return undefined
      const cmd = argv.slice(argv.indexOf('--data') + 2)
      const sub = cmd[0] ?? ''
      let answer: SettingsAnswer = { exitCode: 0, stdout: '{"ok":true}\n' }
      if (sub === 'migrate') answer = { exitCode: 0, stdout: '{"ok":true,"changed":[],"notes":[]}\n' }
      else if (sub === 'list') answer = { exitCode: 0, stdout: `${JSON.stringify({ ok: true, file: YAML_PATH, fields })}\n` }
      else if (sub === 'set') answer = opts.setAnswer?.(cmd) ?? { exitCode: 0, stdout: '{"ok":true}\n' }
      return { value: { exitCode: answer.exitCode, stdout: answer.stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    },
  })
  on('session.cwd', () => ({ value: PROJECT_DIR }))
  on('config.list', () => ({ value: asConfigRows(opts.configRows) }))
  return clock
}

type Pressable = { press(q: { key: string }): Promise<unknown>; redraw(p: unknown): Promise<unknown> }

/** Flush the fire-and-forget settings chain until `want` mk-config calls
 *  landed, then redraw onto the settled view. */
async function loadLiveSettings(clock: MockWorld, ui: Pressable, calls: (readonly string[])[], want = 2): Promise<void> {
  for (let i = 0; i < 60 && mkConfigCalls(calls).length < want; i++) await clock.advance(0)
  await ui.redraw(paneProps(46))
}

test('S3: opening the Settings tab runs migrate then list --json, both with --data', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const clock = mockSettingsBackend(on, files, calls, S3_FIELDS)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls)
  const mk = mkConfigCalls(calls)
  expect(mk).toHaveLength(2)
  expect(mk[0]?.[0]).toBe('python3')
  expect(mk[0]?.[1]?.endsWith('scripts/mk-config.py')).toBe(true)
  for (const a of mk) {
    const i = a.indexOf('--data')
    expect(i, '--data on every mk-config call').toBeGreaterThanOrEqual(0)
    expect(a[i + 1]).toBe(DATA_DIR)
  }
  expect(mk[0]?.slice(-1)).toEqual(['migrate'])
  expect(mk[1]?.slice(-2)).toEqual(['list', '--json'])
  // the live view arrived: no MOCK row anywhere
  expect(await ui.find({ text: /MOCK · fixture data/ })).toBeUndefined()
})

test('LOW 5: every open re-lists and shows the fresh reply; migrate still runs once', { plugins: [worldFixtures] }, async ($, on) => {
  let stall = 20
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const fields = (): unknown[] => S3_FIELDS.map(f => f.key === 'session.stall_min' ? { ...f, value: stall } : f)
  const clock = mockWorld(on, {
    process: argv => {
      calls.push(argv)
      if (!argv.some(a => a.endsWith('scripts/mk-config.py'))) return undefined
      const sub = argv[argv.indexOf('--data') + 2]
      const stdout = sub === 'migrate'
        ? '{"ok":true,"changed":[],"notes":[]}\n'
        : `${JSON.stringify({ ok: true, file: YAML_PATH, fields: fields() })}\n`
      return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    },
  })
  files[YAML_PATH] = 'session:\n  stall_min: 20\n'
  mockFs(on, files, [DATA_DIR, `${PROJECT_DIR}/.git`], [YAML_PATH])
  on('session.cwd', () => ({ value: PROJECT_DIR }))
  on('config.list', () => ({ value: [] }))
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls)
  await ui.press({ key: 'set-scope-child' })
  expect(await ui.find({ text: '20m' }), 'the first list reply').toBeTruthy()
  // the yaml changes under the pane; the next open re-reads it
  stall = 25
  await ui.press({ key: 'dash-tab-old' })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls, 3)
  const mk = mkConfigCalls(calls)
  expect(mk.filter(a => a[a.length - 1] === 'migrate')).toHaveLength(1) // still once per session
  expect(mk.filter(a => a[a.length - 1] === '--json')).toHaveLength(2)
  await ui.press({ key: 'set-scope-child' })
  expect(await ui.find({ text: /^25m$/ }), 'the changed reply shows').toBeTruthy()
  expect(await ui.find({ text: /^20m$/ }), 'the stale value is gone').toBeUndefined()
})

test('S3: save writes one set per dirty yaml row in order, then shows ✓ saved N', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const clock = mockSettingsBackend(on, files, calls, S3_FIELDS)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls)
  await ui.press({ key: 'set-scope-child' })
  await ui.press({ key: 'set-session.stall_min-inc' }) // 20 → 21 (max 60: step 1)
  await ui.press({ key: 'set-session.max_wait_min-inc' }) // 120 → 125 (max 300: step 5)
  await ui.press({ key: 'set-save' })
  for (let i = 0; i < 60 && mkConfigCalls(calls).length < 4; i++) await clock.advance(0)
  const sets = mkConfigCalls(calls).slice(2)
  expect(sets).toHaveLength(2)
  expect(sets[0]?.slice(-3)).toEqual(['set', 'session.stall_min', '21'])
  expect(sets[1]?.slice(-3)).toEqual(['set', 'session.max_wait_min', '125'])
  await ui.redraw(paneProps(46))
  expect(await ui.find({ text: /✓ saved 2 · backups written first/ }), 'the saved footer').toBeTruthy()
})

test('MEDIUM 1: a refused set shows the CLI error text in the footer; the next set never runs', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  // refusal exactly like the real CLI: exit 2, the JSON on stdout, empty stderr
  const clock = mockSettingsBackend(on, files, calls, S3_FIELDS, {
    setAnswer: cmd => cmd[1] === 'session.stall_min'
      ? { exitCode: 2, stdout: '{"ok":false,"error":"session.stall_min must be between 5 and 60: 2"}\n' }
      : { exitCode: 0, stdout: '{"ok":true}\n' },
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls)
  await ui.press({ key: 'set-scope-child' })
  await ui.press({ key: 'set-session.stall_min-inc' })
  await ui.press({ key: 'set-session.max_wait_min-inc' })
  await ui.press({ key: 'set-save' })
  for (let i = 0; i < 60 && mkConfigCalls(calls).length < 3; i++) await clock.advance(0)
  for (let i = 0; i < 10; i++) await clock.advance(0)
  await ui.redraw(paneProps(46))
  expect(mkConfigCalls(calls).filter(a => a.includes('set'))).toHaveLength(1) // the second set never ran
  expect(await ui.find({ text: /must be between 5 and 60/ }), 'the CLI reason in the footer').toBeTruthy()
})

test('MEDIUM 2: the ui.focus hook records the ring; leaving the Input re-arms s', { plugins: [worldFixtures] }, async ($, on) => {
  on('ui.focus', () => ({}))
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const clock = mockSettingsBackend(on, files, calls, S3_FIELDS)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls)
  await ui.press({ key: 'set-scope-child' })
  await ui.press({ key: 'set-session.stall_min-inc' }) // dirty, so save draws
  const hotkeyOf = async (key: string): Promise<unknown> =>
    (await ui.find({ type: 'Button', key }) as { props: { hotkey?: string } } | undefined)?.props.hotkey
  // the engine moves the ring onto the find Input: the producer records it
  await $.ui.focus({ component: 'Pane', requestId: 'matrixkoder', element: 'set-find-input', origin: { kind: 'person' } })
  await ui.redraw(paneProps(46))
  expect(await hotkeyOf('set-save'), 's inert while the Input holds the ring').toBeUndefined()
  // the ring moves on to a Button: s is armed again
  await $.ui.focus({ component: 'Pane', requestId: 'matrixkoder', element: 'set-save', origin: { kind: 'person' } })
  await ui.redraw(paneProps(46))
  expect(await hotkeyOf('set-save'), 's back once the ring leaves the Input').toBe('s')
  // a tab switch clears a stale ring key with it
  await $.ui.focus({ component: 'Pane', requestId: 'matrixkoder', element: 'set-find-input', origin: { kind: 'person' } })
  await ui.redraw(paneProps(46))
  expect(await hotkeyOf('set-save'), 's inert again').toBeUndefined()
  await ui.press({ key: 'dash-tab-old' })
  await ui.press({ key: 'dash-tab-settings' })
  await ui.redraw(paneProps(46))
  expect(await hotkeyOf('set-save'), 's back after the tab switch').toBe('s')
})

test('MEDIUM 3: without matrixkoder.yaml neither migrate nor list runs; globals still show', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles() // no yaml file, and none stattable
  mockFs(on, files)
  const calls: (readonly string[])[] = []
  const clock = mockWorld(on, {
    process: argv => {
      calls.push(argv)
      return undefined
    },
  })
  on('session.cwd', () => ({ value: PROJECT_DIR }))
  on('config.list', () => ({ value: asConfigRows([{ key: 'matrixkoder.chip', value: true, isLocked: false }]) }))
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  for (let i = 0; i < 20; i++) await clock.advance(0)
  await ui.redraw(paneProps(46))
  expect(mkConfigCalls(calls), 'no mk-config.py call at all').toEqual([])
  expect(await ui.find({ text: /No matrixkoder\.yaml — run \/mk setup/ }), 'the setup note').toBeTruthy()
  expect(await ui.find({ text: /MOCK · fixture data/ }), 'never mock rows').toBeUndefined()
  await ui.press({ key: 'set-scope-about' })
  expect(await ui.find({ text: /Status chip/ }), 'the global row still shows').toBeTruthy()
})

test('LOW 1: a failed load shows the ⚠ line over live rows, never the fixture', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  files[YAML_PATH] = 'session:\n  stall_min: 20\n'
  mockFs(on, files, [DATA_DIR, `${PROJECT_DIR}/.git`], [YAML_PATH])
  const calls: (readonly string[])[] = []
  const clock = mockWorld(on, {
    process: argv => {
      calls.push(argv)
      return undefined // the default answer carries no JSON: migrate throws
    },
  })
  on('session.cwd', () => ({ value: PROJECT_DIR }))
  on('config.list', () => ({ value: [] }))
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  for (let i = 0; i < 20; i++) await clock.advance(0)
  await ui.redraw(paneProps(46))
  expect(await ui.find({ text: /printed no JSON/ }), 'the ⚠ reason').toBeTruthy()
  expect(await ui.find({ text: /MOCK · fixture data/ }), 'never mock rows').toBeUndefined()
})

test('LOW 2: a failing list records settings:migrated so migrate is not rerun on retry', { plugins: [worldFixtures] }, async ($, on) => {
  let listAttempts = 0
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const clock = mockWorld(on, {
    process: argv => {
      calls.push(argv)
      if (!argv.some(a => a.endsWith('scripts/mk-config.py'))) return undefined
      const sub = argv[argv.indexOf('--data') + 2]
      if (sub === 'migrate') return { value: { exitCode: 0, stdout: '{"ok":true,"changed":[],"notes":[]}\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      listAttempts++
      if (listAttempts === 1) return { value: { exitCode: 1, stdout: '', stderr: 'temporary list fail', isStdoutTruncated: false, isStderrTruncated: false } }
      return { value: { exitCode: 0, stdout: `${JSON.stringify({ ok: true, file: YAML_PATH, fields: S3_FIELDS })}\n`, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    },
  })
  files[YAML_PATH] = 'session:\n  stall_min: 20\n'
  mockFs(on, files, [DATA_DIR, `${PROJECT_DIR}/.git`], [YAML_PATH])
  on('session.cwd', () => ({ value: PROJECT_DIR }))
  on('config.list', () => ({ value: [] }))
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  for (let i = 0; i < 20; i++) await clock.advance(0)
  // first open: migrate (ok) + list (failed)
  expect(mkConfigCalls(calls).filter(a => a.includes('migrate'))).toHaveLength(1)
  expect(mkConfigCalls(calls).filter(a => a.includes('list'))).toHaveLength(1)
  // second open: list retried, but migrate NOT rerun
  await ui.press({ key: 'dash-tab-old' })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls, 3)
  expect(mkConfigCalls(calls).filter(a => a.includes('migrate'))).toHaveLength(1)
  expect(mkConfigCalls(calls).filter(a => a.includes('list'))).toHaveLength(2)
})

test('LOW 4: a second save press while saving does nothing', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const clock = mockSettingsBackend(on, files, calls, [], {
    configRows: [{ key: 'matrixkoder.chip', value: true, isLocked: false }],
  })
  let release: () => void = () => {}
  const gate = new Promise<void>(res => { release = res })
  const setCalls: [string, unknown][] = []
  on('config.set', async (_$, e) => {
    setCalls.push([String((e as { key?: unknown }).key), (e as { value?: unknown }).value])
    if (setCalls.length === 1) await gate // the first save parks here
    return { value: true }
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls)
  await ui.press({ key: 'set-scope-about' })
  await ui.press({ key: 'set-chip-tog' }) // a dirty global row, no yaml sets
  await ui.press({ key: 'set-save' })
  for (let i = 0; i < 20; i++) await clock.advance(0)
  await ui.press({ key: 'set-save' }) // still saving: dropped
  for (let i = 0; i < 20; i++) await clock.advance(0)
  expect(setCalls).toHaveLength(1)
  release()
  for (let i = 0; i < 60 && !(await ui.find({ text: /✓ saved 1/ })); i++) await clock.advance(0)
  expect(await ui.find({ text: /✓ saved 1/ }), 'the save lands once').toBeTruthy()
})
