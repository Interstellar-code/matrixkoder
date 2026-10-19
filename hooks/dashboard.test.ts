// Dashboard pane tests (design §3): base 46-col layout on both surfaces,
// compact 36-col, inline hint, first-run and no-run states. Fixtures arrive
// via $.fs.write (inline fixtures plugin); fs is served in memory.
import { test, expect } from 'claude-code/testing'
import type { Engine, Plugin } from 'claude-code/testing'
import type { On, ConfigRow, FsEntry } from 'claude-code'
import { snapshotCount, DATA_DIR, NOW, PROJECT_DIR, RUN_PATH, RUNS_INDEX, USAGE_PATH, agentOf, agentsOf, mockFs, mockWorld, paneProps, sessionStart, sessionStartIn, setRun, setUsage, tasksOf, worldFiles, worldFixtures } from './testkit'
import type { Files, MockWorld } from './testkit'


import { PANE_ID, parseYamlAgents } from './data'
import { buildRainGrid, filesFocus, footerParts, liveOverviewView, modelShort, openWaiting, planActionPatch, rainGridFor, resolveMainCheckout, runPlanAction, taskNote, usageShorts, waitingPane, waitingPaneView } from './dashboard'
import type { MkSnapshot } from '../types'
import { mockTabCounts, waitingCount } from './tabs/header'
import { RUN_SNAPSHOT } from './fixtures/run-snapshot'
import planDetail from './tabs/fixtures/plan-detail'

/* ---------------------------------------------------------------------------
 * The REAL `mkdb.py run-snapshot --data <dir> --json` replies, captured by
 * RUNNING the command on 2026-10-15 (see .mk-scratch-r2/reply-*.json, written by
 * .mk-scratch-r2/build-store.py). The data dirs are real: one with no mk.db at
 * all, one holding a real store built exactly the way mkdb.py's own --self-test
 * builds a temp store (open_project + add_task + transition + a waits row).
 *
 * The capture is the PRE-fix shape: mkdb.py does not yet print a `store` key, so
 * these two payloads are BYTE-IDENTICAL — which is the bug. A no-store reply
 * `{ok:true, runs: [], active:null, run:null, waits:[]}` at exit 0 is exactly what a
 * store that exists and holds no run also prints, so readStoreSnapshot cannot tell
 * them apart and loadRunsFromFiles never runs on a pre-migration project.
 *
 * The fix adds ONE key: `"store": false` on a missing store, `"store": true` when
 * the store opened. Everything else, the exit code and every existing key are
 * unchanged. So the constants below are the captured bytes plus that key, and the
 * tests are written against the POST-fix shape. Verified verbatim:
 *   no store   `{"ok": true, "runs": [], "active": null, "run": null, "waits": []}`
 *   0-run store (identical, apart from `mk.db` existing on disk)
 * -------------------------------------------------------------------------- */

/** the captured no-store reply + `"store": false` — the store is MISSING */
const NO_STORE_SNAPSHOT = '{"ok": true, "runs": [], "active": null, "run": null, "waits": [], "store": false}'

/** the captured 0-run-store reply + `"store": true` — the store is THERE and holds no run */
const EMPTY_STORE_SNAPSHOT = '{"ok": true, "runs": [], "active": null, "run": null, "waits": [], "store": true}'

/** The `process` answer for the pane's store read (C4/L5): `mkdb.py run-snapshot` replies
 *  with the captured real payload, every other spawn with today's empty answer. The reply is
 *  always a stdout STRING, because that is what `mkdb.py run-snapshot` really does: a missing
 *  store is a successful exit-0 empty snapshot, NOT a failing exit code, so the only way the
 *  pane learns the store is missing is the `store:false` key in the payload. Handed to
 *  `mockWorld`, so it stays the ONE process.run registration the test has; the reply is read
 *  through `state` because a test cannot register a second handler after the first `$.` call. */
type StoreState = { reply: string; argv: string[][] }

function storeSnapshot(state: StoreState): (argv: readonly string[]) => {
  value: { exitCode: number; stdout: string; stderr: string; isStdoutTruncated: boolean; isStderrTruncated: boolean }
} | undefined {
  return a => {
    state.argv.push([...a] as string[])
    if (!a.includes('run-snapshot')) return undefined
    return { value: { exitCode: 0, stdout: state.reply, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  }
}

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

// --- C4/L5: the pane reads the project store; the file views are a fallback ---------------

test('C4/L5: the store reply renders the pane with the file views deleted', async ($, on) => {
  const files: Files = {} // post-L4: no runs/index.json, no run.json, nothing
  mockFs(on, files, [DATA_DIR, `${PROJECT_DIR}/.git`])
  const store: StoreState = { reply: JSON.stringify(RUN_SNAPSHOT), argv: [] }
  mockWorld(on, { process: storeSnapshot(store) })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: 'L5 fixtures' }), 'the run name comes from the store').toBeTruthy()
  expect(await ui.find({ text: /verify T1 gates/ }), 'the next-action line').toBeTruthy()
  const snap = store.argv.find(a => a.includes('run-snapshot'))
  expect(snap?.[0]).toBe('python3')
  expect(snap?.slice(2)).toEqual(['--data', DATA_DIR, 'run-snapshot', '--json'])
})

test('C4/L5: no store (store:false) draws the NoRun panel, never a file view', async ($, on) => {
  // The REAL mkdb.py answer for a project with no mk.db, captured verbatim (exit 0, `store:false`).
  // The store is the only truth: a run.json/index.json still on disk is not read, so the pane
  // shows NoRun instead of resurrecting the old file views.
  const files = worldFiles()
  const fs = mockFs(on, files)
  mockWorld(on, { process: storeSnapshot({ reply: NO_STORE_SNAPSHOT, argv: [] }) })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: 'No active orchestration run.' }), 'no store is the NoRun panel').toBeTruthy()
  expect(await ui.find({ text: 'workflows-v2' }), 'the stale run.json is not drawn').toBeUndefined()
  expect(fs.reads, 'the file views are never consulted').not.toContain(RUN_PATH)
  expect(fs.reads).not.toContain(RUNS_INDEX)
})

test('C4/L5: a store that EXISTS with no run is not a missing store — no file fallback', async ($, on) => {
  // The discriminating half of the pair above: a real mk.db holding zero runs prints the very
  // same payload, and the only difference is `"store": true`. That is the NoRun panel, not a
  // fallback — a stale run.json beside a live empty store must NOT be resurrected. Before the
  // `store` key existed this reply and the no-store reply were byte-identical, so the pane read
  // this one as a successful empty store by accident and only ever drew NoRun by luck.
  const files = worldFiles()
  const fs = mockFs(on, files)
  mockWorld(on, { process: storeSnapshot({ reply: EMPTY_STORE_SNAPSHOT, argv: [] }) })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: 'No active orchestration run.' }), 'an empty store is the NoRun panel').toBeTruthy()
  expect(await ui.find({ text: 'workflows-v2' }), 'the stale run.json is not resurrected').toBeUndefined()
  expect(fs.reads, 'the store answered, so the file views are never consulted').not.toContain(RUN_PATH)
})

test('C4/L5: the store wins over the file views when both are present', async ($, on) => {
  const files = worldFiles()
  setRun(files, run => { run.feature = 'from-the-file-view' })
  mockFs(on, files)
  mockWorld(on, { process: storeSnapshot({ reply: JSON.stringify(RUN_SNAPSHOT), argv: [] }) })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: 'L5 fixtures' }), 'the store is the source of truth').toBeTruthy()
  expect(await ui.find({ text: 'from-the-file-view' }), 'the file view is not consulted').toBeUndefined()
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

test('no active run state lists recent runs', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  delete files['/tmp/mk-proj/.matrixkoder/runs/wfv2/run.json'] // run.json vanishes (archived reality)
  await clock.advance(5000) // next tick reloads → no active run
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: 'No active orchestration run.' })).toBeTruthy()
  expect(await ui.find({ text: /mk-p0/ })).toBeTruthy()
})

// ---------- fix1: resilience, data-dir rule, /mk args, timers, usage rows ----------

test('tasks:[null] run.json still renders and recovers once fixed', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    const tasks = tasksOf(run)
    run.tasks = [null, tasks[4] ?? {}]
  })
  mockFs(on, files)
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  let ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /X1/ })).toBeTruthy() // the null task is dropped, the rest render
  const good = worldFiles()[RUN_PATH] ?? '{}'
  files['/fixtures/run.json'] = files[RUN_PATH] = good
  await clock.advance(5000)
  expect(await ui.find({ text: /F6/ })).toBeTruthy() // recovered
  expect(snapshotCount()).toBeGreaterThan(1) // the timer survived the bad load
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

test('the root store alone names the data dir when projects.json is absent', async ($, on) => {
  const run = worldFiles()[RUN_PATH] ?? ''
  const files: Record<string, string> = {
    '/srv/store-data/runs/index.json': JSON.stringify([{ id: 'wfv2', feature: 'workflows-v2', status: 'active', started: '2026-10-07T16:00:00+02:00' }]),
    '/srv/store-data/runs/wfv2/run.json': run,
  }
  mockFs(on, files, ['/srv/store-data', '/tmp/main/.git'])
  mockWorld(on, {
    env: { MATRIXKODER_HOME: '/srv/mkhome' },
    process: (argv) => argv.includes('projects')
      ? { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, projects: [{ slug: 'main', repo: '/tmp/main', data_dir: '/srv/store-data' }] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      : undefined,
  })
  await $.session.start(sessionStartIn('/tmp/main'))
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /workflows-v2/ })).toBeTruthy()
  expect(await ui.find({ text: /Welcome to MatrixKoder/ })).toBeUndefined()
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

test('missing usage triggers the initial poll with the right argv', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  delete files['/fixtures/usage.json']
  delete files[USAGE_PATH]
  mockFs(on, files)
  const polls: (readonly string[])[] = []
  const clock = mockWorld(on, { process: argv => { if (argv.some(a => a.endsWith('scripts/mk-usage.py'))) polls.push(argv) } })
  await $.session.start(sessionStart())
  // P1.3: the cold-start poll is scheduled 5 s out (it must not stack onto the first load),
  // so the first tick of the mock clock is what starts it.
  await clock.advance(5000)
  expect(polls.length).toBe(1)
  const first = polls[0]
  if (!first) throw new Error('no poll ran')
  expect(first[0]).toBe('python3')
  expect(first[1]?.endsWith('scripts/mk-usage.py')).toBe(true)
  expect(first[2]).toBe('--once')
})

test('a project yaml scopes the usage poll: --providers from usage.providers', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  files[`${DATA_DIR}/matrixkoder.yaml`] = PROJECT_YAML
  delete files['/fixtures/usage.json']
  delete files[USAGE_PATH]
  mockFs(on, files)
  const polls: (readonly string[])[] = []
  const clock = mockWorld(on, { process: argv => { if (argv.some(a => a.endsWith('scripts/mk-usage.py'))) polls.push(argv) } })
  await $.session.start(sessionStart())
  // P1.3: the cold-start poll is scheduled 5 s out, not fired with the first load.
  await clock.advance(5000)
  expect(polls.length).toBe(1)
  const first = polls[0]
  if (!first) throw new Error('no poll ran')
  expect(first[0]).toBe('python3')
  expect(first[1]?.endsWith('scripts/mk-usage.py')).toBe(true)
  expect(first[2]).toBe('--once')
  expect(first[3]).toBe('--providers')
  expect(first[4]).toBe('claude,codex,antigravity')
})

test('P1.3.a4: no usage poll happens in the first 5 s after start on the mock clock', { plugins: [worldFixtures] }, async ($, on) => {
  // The cold-start poll is scheduled with $.clock.after(5000, ...), so a start that is
  // missing usage.json still polls nothing for the first tick. FAILS ON THE OLD CODE, which
  // polled straight from startLoop (no timer involved).
  const files = worldFiles()
  delete files['/fixtures/usage.json']
  delete files[USAGE_PATH]
  mockFs(on, files)
  const polls: (readonly string[])[] = []
  const clock = mockWorld(on, { process: argv => { if (argv.some(a => a.endsWith('scripts/mk-usage.py'))) polls.push(argv) } })
  await $.session.start(sessionStart())
  await clock.advance(4999)
  expect(polls, 'nothing polls inside the 5 s window').toHaveLength(0)
  await clock.advance(1)
  expect(polls, 'the one-shot fires at 5 s').toHaveLength(1)
  // and it is a one-shot, not an interval: the next 5 s adds no second call
  await clock.advance(5000)
  expect(polls, 'started at most once for a cold start').toHaveLength(1)
})

test('two session.start arm exactly one refresh timer', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  await $.session.start(sessionStart())
  await clock.advance(5000)
  await clock.advance(5000)
  await clock.advance(5000)
  expect(snapshotCount()).toBe(5) // 2 initial loads + 3 single ticks
})

test('/mk re-arms the refresh loop after /clear (session.end, no session.start)', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  const clock = mockWorld(on)
  on('session.cwd', () => ({ value: '/tmp/mk-proj' }))
  on('session.end', () => ({ sessionId: 's1' }))
  await $.session.start(sessionStart())
  await $.session.end({ reason: 'clear', sessionId: 's1', resume: { id: undefined } } as never)
  await clock.advance(5000)
  expect(snapshotCount()).toBe(1) // loop dead: only the start load
  await $.command.run({ command: 'mk', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 165 } })
  // P1.3: /mk arms the loop and starts the first load in the background, so settle one step
  // before counting it (the number itself is unchanged).
  await clock.advance(0)
  expect(snapshotCount()).toBe(2) // /mk reloads at once
  await clock.advance(5000)
  expect(snapshotCount()).toBe(3) // and ticks again
})

test('an open pane re-arms the refresh loop after /clear without /mk (render self-heal)', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  const clock = mockWorld(on)
  on('session.cwd', () => ({ value: '/tmp/mk-proj' }))
  on('session.end', () => ({ sessionId: 's1' }))
  await $.session.start(sessionStart())
  await $.session.end({ reason: 'clear', sessionId: 's1', resume: { id: undefined } } as never)
  await clock.advance(5000)
  expect(snapshotCount()).toBe(1) // loop dead: only the start load
  await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await clock.advance(0)
  expect(snapshotCount()).toBe(2) // the render restarted the loop and reloaded
  await clock.advance(5000)
  expect(snapshotCount()).toBe(3) // and it ticks again
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

// ---------- BD: the plugin host keeps the bus alive ----------

function isBus(argv: readonly string[]): boolean {
  return argv.length >= 3 && argv[0] === 'python3' && !!argv[1]?.endsWith('scripts/mk-bus.py') && (argv[2] === 'ensure' || argv[2] === 'heartbeat')
}

function isBusEnabledGet(argv: readonly string[]): boolean {
  return argv.length >= 3 && argv[0] === 'python3' && !!argv[1]?.endsWith('scripts/mk-config.py') && argv[2] === 'get' && argv[3] === 'bus.enabled'
}

test('bus ensure runs with --data and --run on session.start, then every 15 s', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  const bus: (readonly string[])[] = []
  const clock = mockWorld(on, { process: argv => { if (isBus(argv)) bus.push(argv) } })
  await $.session.start(sessionStart())
  await clock.advance(0) // the session.start call is fire-and-forget
  expect(bus.length).toBe(1)
  const first = bus[0] ?? []
  expect(first[0]).toBe('python3')
  expect(first[1]?.endsWith('scripts/mk-bus.py')).toBe(true)
  expect(first[2]).toBe('ensure')
  expect(first.slice(3)).toEqual(['--data', DATA_DIR, '--run', 'wfv2'])
  await clock.advance(15_000)
  expect(bus.length).toBe(2) // the tick, not only the session-start call
  expect(bus[1]?.slice(2)).toEqual(['ensure', '--data', DATA_DIR, '--run', 'wfv2'])
})

test('a bus.json in the data dir turns the tick into a heartbeat', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  files[`${DATA_DIR}/bus.json`] = JSON.stringify({ pid: 4242, port: 51234, started: 0, owner_heartbeat: 0 })
  mockFs(on, files)
  const bus: (readonly string[])[] = []
  const clock = mockWorld(on, { process: argv => { if (isBus(argv)) bus.push(argv) } })
  await $.session.start(sessionStart())
  await clock.advance(0)
  expect(bus.length).toBe(1)
  expect(bus[0]?.slice(2)).toEqual(['heartbeat', '--data', DATA_DIR, '--run', 'wfv2'])
  await clock.advance(15_000)
  expect(bus.filter(a => a[2] === 'ensure'), 'ensure never runs while bus.json names a server').toHaveLength(0)
  expect(bus.length).toBe(2)
})

test('mode solo runs no bus command', { plugins: [worldFixtures], options: { mode: 'solo' } }, async ($, on) => {
  mockFs(on, worldFiles())
  const bus: (readonly string[])[] = []
  const clock = mockWorld(on, { process: argv => { if (isBus(argv)) bus.push(argv) } })
  await $.session.start(sessionStart())
  await clock.advance(15_000)
  expect(bus, 'solo hosts no bus even with an active run').toHaveLength(0)
})

test('no active run runs no bus command', async ($, on) => {
  const files = worldFiles()
  const index = files[RUNS_INDEX] ?? ''
  const runJson = files[RUN_PATH] ?? '{}'
  delete files[RUNS_INDEX]
  delete files[RUN_PATH]
  mockFs(on, files) // the data dir must resolve, or the host would skip for the wrong reason
  const bus: (readonly string[])[] = []
  const clock = mockWorld(on, { process: argv => { if (isBus(argv)) bus.push(argv) } })
  await $.session.start(sessionStart())
  await clock.advance(15_000)
  expect(bus, 'nothing to keep alive without a run').toHaveLength(0)
  // control: the same session with a run restored calls ensure, so the run is what kept this quiet
  files[RUNS_INDEX] = index
  files[RUN_PATH] = runJson
  await clock.advance(15_000)
  expect(bus.length, 'the run, not a dead host, is what kept this quiet').toBe(1)
})

test('bus.enabled=false skips ensure, any other mk-config answer proceeds', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  const bus: (readonly string[])[] = []
  const gets: (readonly string[])[] = []
  const ok = (stdout: string) => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
  const clock = mockWorld(on, {
    process: argv => {
      if (argv.includes('run-snapshot')) return // the default store reply
      if (isBusEnabledGet(argv)) { gets.push(argv); return ok('{"ok": true, "value": false}') } // the row BC adds
      if (isBus(argv)) bus.push(argv)
      return ok('{"ok": false, "error": "unknown key: bus.enabled"}') // every checkout without BC
    },
  })
  await $.session.start(sessionStart())
  await clock.advance(0)
  expect(gets.length, 'the row is read before ensure').toBe(1)
  expect(gets[0]?.slice(3)).toEqual(['bus.enabled', '--data', DATA_DIR])
  expect(bus, 'an explicit false leaves the bus down').toHaveLength(0)
})

test('an unknown bus.enabled key does not stop ensure', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const bus: (readonly string[])[] = []
  const clock = mockWorld(on, {
    process: argv => {
      if (argv.includes('run-snapshot')) return // the default store reply
      if (isBus(argv)) bus.push(argv)
      // mk-config.py prints ok:false and exits 2 for a key no row declares
      return { value: { exitCode: 2, stdout: '{"ok": false, "error": "unknown key: bus.enabled"}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    },
  })
  await $.session.start(sessionStart())
  await clock.advance(0)
  expect(bus.length).toBe(1)
  expect(bus[0]?.[2]).toBe('ensure')
})

test('a failing ensure never throws and the next tick retries', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  let attempts = 0
  const clock = mockWorld(on, {
    process: argv => {
      if (!isBus(argv)) return
      attempts++
      throw new Error('mk-bus.py exit 2: server failed to start within timeout')
    },
  })
  await $.session.start(sessionStart())
  await clock.advance(0)
  expect(attempts).toBe(1)
  await clock.advance(15_000)
  expect(attempts, 'the tick after a failure tries again').toBe(2)
})

test('a bus tick in flight blocks the next one', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const bus: (readonly string[])[] = []
  const clock = mockWorld(on, {
    process: async argv => {
      if (!isBus(argv)) return
      bus.push(argv)
      await clock.sleep(60_000) // the start outlives every tick inside it
      return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    },
  })
  await $.session.start(sessionStart())
  await clock.advance(0)
  expect(bus.length).toBe(1)
  await clock.advance(15_000)
  await clock.advance(15_000)
  expect(bus.length, 'no second process while the first still runs').toBe(1)
  await clock.advance(120_000)
  expect(bus.length, 'a tick after it finishes starts again').toBeGreaterThan(1)
})

test('the /mk re-arm path calls ensure once, without a 15 s wait', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const bus: (readonly string[])[] = []
  const clock = mockWorld(on, { process: argv => { if (isBus(argv)) bus.push(argv) } })
  on('session.cwd', () => ({ value: PROJECT_DIR }))
  on('session.end', () => ({ sessionId: 's1' }))
  await $.session.start(sessionStart())
  await clock.advance(0)
  expect(bus.length).toBe(1)
  await $.session.end({ reason: 'clear', sessionId: 's1', resume: { id: undefined } } as never)
  await clock.advance(60_000)
  expect(bus.length, '/clear stopped the timers, nothing ticked').toBe(1)
  await $.command.run({ command: 'mk', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 165 } })
  await clock.advance(0)
  expect(bus.length, '/mk re-arms and calls ensure at once').toBe(2)
})

// ---------- the store names ONE run document: the newest active run ----------

test('two active runs: the store reply draws only the newest run, the older stays in the index', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  // a second, OLDER active run in the index; run-snapshot carries only the newest run's document
  const index = JSON.stringify([
    { id: 'mk-ui', feature: 'ui-polish', status: 'active', started: '2026-10-07T14:30:00+02:00', ended: null },
    { id: 'wfv2', feature: 'workflows-v2', status: 'active', started: '2026-10-07T16:00:00+02:00', ended: null },
    { id: 'mk-p0', feature: 'mod spikes', status: 'done', started: '2026-10-01T10:00:00+02:00', ended: '2026-10-02T10:00:00+02:00' },
  ])
  files['/fixtures/index.json'] = index // worldFixtures re-copies this on session.start
  files[RUNS_INDEX] = index
  mockFs(on, files)
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /workflows-v2/ }), 'newest run header').toBeTruthy()
  expect(await ui.find({ text: /ui-polish/ }), 'the older active run has no document in the reply, so no block').toBeUndefined()
})

// ---------- S0: the tab bar and the tabs ----------

const MOCK_ROW = /MOCK · fixture data · not this run/
const mockRow = () => ({ text: MOCK_ROW })
/** What each live tab draws against the world fixtures. Since 2341d2c Overview, Lanes, Plan, Usage and Events
 *  draw live data (no MOCK row); only Waiting still draws its fixture. */
const LIVE_MARK: Record<string, RegExp> = {
  'dash-tab-overview': /LANES 3/,
  'dash-tab-lanes': /3 lanes · 2 live/,
  'dash-tab-plan': /no plans yet/,
  'dash-tab-usage': /QUOTA/,
  'dash-tab-events': /EVENTS/,
}
async function expectTabBody(ui: { find: (q: { text: RegExp }) => Promise<unknown> }, key: string, label: string): Promise<void> {
  const mark = LIVE_MARK[key]
  if (mark === undefined) {
    expect(await ui.find(mockRow()), `${label} MOCK row`).toBeTruthy()
    return
  }
  expect(await ui.find({ text: mark }), `${label} live body`).toBeTruthy()
  expect(await ui.find(mockRow()), `${label} has no MOCK row`).toBeUndefined()
}
const TAB_BUTTONS = [
  { key: 'dash-tab-overview', name: 'Overview' },
  { key: 'dash-tab-lanes', name: 'Lanes' },
  { key: 'dash-tab-plan', name: 'Plan' },
  { key: 'dash-tab-usage', name: 'Usage' },
  { key: 'dash-tab-files', name: 'Files' },
  { key: 'dash-tab-events', name: 'Events' },
  { key: 'dash-tab-waiting', name: 'Waiting' },
  { key: 'dash-tab-kanban', name: 'Kanban' },
  { key: 'dash-tab-todo', name: 'Todo' },
  { key: 'dash-tab-settings', name: 'Settings' },
] as const

test('a fresh mount opens on Overview: real run data and no MOCK row', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /workflows-v2/ })).toBeTruthy() // the real run header
  expect(await ui.find(mockRow())).toBeUndefined()
})

test('every tab hotkey switches to that tab’s body: live data, MOCK only on Waiting', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  // Settings is live since S3 (loading line, then live rows) — the S3 tests cover it.
  for (const { key, name } of TAB_BUTTONS.filter(t => t.key !== 'dash-tab-settings' && t.key !== 'dash-tab-kanban' && t.key !== 'dash-tab-files' && t.key !== 'dash-tab-todo')) {
    await ui.press({ key })
    await expectTabBody(ui, key, name)
  }
})

test('at 36 cols the tab bar drops to short labels and every tab still renders', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.redraw(paneProps(36))
  // Settings is live since S3 (loading line, then live rows) — the S3 tests cover it.
  // the active tab (Overview) keeps its word; the others show their hotkey letter (V0.4: one scheme at every width)
  for (const { key, name } of TAB_BUTTONS.filter(t => t.key !== 'dash-tab-settings')) {
    const wide = await ui.find({ type: 'Button', text: name })
    if (key === 'dash-tab-overview') expect(wide, `${name} is active and keeps its word`).toBeDefined()
    else expect(wide, `${name} wide label dropped`).toBeUndefined()
  }
  expect((await ui.findAll({ type: 'Button' })).filter(b => String(b.props.key).startsWith('dash-tab-')), 'the bar still draws every tab').toHaveLength(TAB_BUTTONS.length)
  for (const { key } of TAB_BUTTONS.filter(t => t.key !== 'dash-tab-settings' && t.key !== 'dash-tab-kanban' && t.key !== 'dash-tab-files' && t.key !== 'dash-tab-todo')) {
    await ui.press({ key })
    await expectTabBody(ui, key, `${key} renders narrow`)
  }
})

test('the chosen tab survives a refresh tick', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-plan' })
  expect(await ui.find({ text: /no plans yet/ })).toBeTruthy()
  await clock.advance(5000) // the refresh tick reloads the snapshot atom
  expect(await ui.find({ text: /no plans yet/ })).toBeTruthy() // the tab choice stands
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
  expect(mockTabCounts()).toEqual({ overview: '', lanes: '4', plan: '3/8', usage: '', files: '', events: '', waiting: '!1', kanban: '', todo: '', settings: '' })
  expect(waitingCount(0)).toBe('') // LOW: a fixture with no open decision shows no count, never `!0`
  expect(waitingCount(2)).toBe('!2')
})

test('the run header sits above the tab bar: name, stage chip and ▶ Next', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: 'workflows-v2' }), 'run name').toBeTruthy()
  expect(await ui.find({ text: /7\/12 Verify/ }), 'stage chip').toBeTruthy()
  expect(await ui.find({ text: /▶ verify F6 gates/ }), 'the ▶ Next line').toBeTruthy()
  expect(await ui.find({ text: ' MOCK ' }), 'the real header is not marked MOCK').toBeUndefined()
})

test('every tab shows the real run header: name, stage chip and ▶ Next, never a MOCK marker', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  // Settings is live since S3 (loading line, then live rows) — the S3 tests cover it.
  for (const { key, name } of TAB_BUTTONS.filter(t => t.key !== 'dash-tab-settings' && t.key !== 'dash-tab-kanban' && t.key !== 'dash-tab-files' && t.key !== 'dash-tab-todo')) {
    await ui.press({ key })
    expect(await ui.find({ text: 'MATRIXKODER' }), `${name} shared brand header`).toBeTruthy()
    expect(await ui.find({ text: /ORCHESTRATOR/ }), `${name} mode chip`).toBeTruthy()
    expect(await ui.find({ text: 'workflows-v2' }), `${name} run name`).toBeTruthy()
    expect(await ui.find({ text: /7\/12 Verify/ }), `${name} stage chip`).toBeTruthy()
    expect(await ui.find({ text: ' MOCK ' }), `${name} the real header is not marked MOCK`).toBeUndefined()
    expect(await ui.find({ text: /▶ verify F6 gates/ }), `${name} ▶ Next`).toBeTruthy()
  }
})

test('the shared brand header shows plugin.json\'s version chip on a run tab and keeps the run block off Settings', { plugins: [worldFixtures] }, async ($, on) => {
  // the engine names the plugin root, so the manifest is matched by its suffix
  const manifest = JSON.stringify({ name: 'matrixkoder', version: '9.8.7' })
  const isManifest = (k: string | symbol): boolean => typeof k === 'string' && k.endsWith('/.claude-plugin/plugin.json')
  mockFs(on, new Proxy(worldFiles(), {
    has: (f, k) => isManifest(k) || k in f,
    get: (f, k) => (isManifest(k) ? manifest : f[k as string]),
  }))
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(36) })
  await ui.press({ key: 'dash-tab-overview' })
  expect(await ui.find({ text: 'MATRIXKODER' }), 'brand wordmark').toBeTruthy()
  expect(await ui.find({ text: / v9\.8\.7 / }), 'version chip from plugin.json').toBeTruthy()
  expect(await ui.find({ text: 'workflows-v2' }), 'run block under the brand').toBeTruthy()
  await ui.press({ key: 'dash-tab-settings' })
  expect(await ui.find({ text: 'MATRIXKODER' }), 'same brand header on Settings').toBeTruthy()
  expect(await ui.find({ text: 'workflows-v2' }), 'no run block on Settings').toBeUndefined()
})

test('F0: every tab Button carries its keyboard hotkey — o l p u e w k t 9, at 36 and 46 cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  for (const cols of [46, 36] as const) {
    await ui.redraw(paneProps(cols))
    // only the bar's own `dash-tab-*` keys name tabs: a body (Waiting's options) draws Buttons too
    const buttons = (await ui.findAll({ type: 'Button' })).filter(b => String(b.props.key).startsWith('dash-tab-'))
    expect(buttons.map(b => b.props.hotkey), `hotkeys at ${cols}`).toEqual(['o', 'l', 'p', 'u', 'f', 'e', 'w', 'k', 't', '9'])
    for (const b of buttons) {
      // an inactive tab's label is its count alone (or the `·` placeholder), so
      // a plain Button's `h: label` drawing never repeats the tab's own letter
      // wide: full words. narrow: the active tab keeps its word, the others show their hotkey letter (V0.4)
      if (cols === 46) expect(String(b.props.label), `${String(b.props.hotkey)} label at ${cols}`).not.toBe(String(b.props.hotkey))
      expect(String(b.props.label).length, `${String(b.props.hotkey)} label at ${cols}`).toBeGreaterThan(0)
    }
    for (const row of barRows(buttons, cols)) {
      expect(row, `bar row at ${cols}`).toBeLessThanOrEqual(cols)
    }
  }
  // the press-by-key switch loop stands
  // Settings is live since S3 (loading line, then live rows) — the S3 tests cover it.
  for (const { key, name } of TAB_BUTTONS.filter(t => t.key !== 'dash-tab-settings' && t.key !== 'dash-tab-kanban' && t.key !== 'dash-tab-files' && t.key !== 'dash-tab-todo')) {
    await ui.redraw(paneProps(46))
    await ui.press({ key })
    await expectTabBody(ui, key, name)
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

test('the tab bar shows every word at 46 and the active word plus hotkey letters at 36 (counts dropped in 2341d2c)', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await tabLabels(ui)).toEqual(['Overview', 'Lanes', 'Plan', 'Usage', 'Files', 'Events', 'Waiting', 'Kanban', 'Todo', 'Settings'])
  for (const cols of [36, 46] as const) {
    await ui.press({ key: 'dash-tab-lanes' })
    await ui.redraw(paneProps(cols))
    const labels = await tabLabels(ui)
    expect(labels, `at ${cols}`).toEqual(cols === 46 ? ['Overview', 'Lanes', 'Plan', 'Usage', 'Files', 'Events', 'Waiting', 'Kanban', 'Todo', 'Settings'] : ['o', 'Lanes', 'p', 'u', 'f', 'e', 'w', 'k', 't', '9'])
    expect(labels.join(' ')).not.toContain('o: o') // the S0 short labels are gone
    expect(labels.filter(l => /^[a-z0-9]$/.test(l)).length, 'narrow: every inactive tab shows its hotkey letter, wide: none').toBe(cols === 46 ? 0 : 9)
    expect(labels.filter(l => l === '·').length, 'the count placeholder is gone').toBe(0)
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
  // 2341d2c: the Lanes tab draws live lane cards; a shared lane is ONE card keyed by its phase ids
  await ui.press({ key: 'dash-tab-lanes' })
  expect(await ui.find({ text: /E1 E2 E3 E4/ }), 'the phase ids on one row').toBeTruthy()
  expect(await ui.find({ text: /OPENCODE/ }), 'the shared lane agent').toBeTruthy()
  expect(await ui.find({ text: /● building/ }), 'the rolled-up status').toBeTruthy()
  const row = await ui.find({ key: 'E1 E2 E3 E4' })
  expect(row, 'one card keyed by the phase ids').toBeDefined()
  const flat = rowTexts(row).join('')
  expect(flat).toContain('E1 E2 E3 E4')
  expect(flat).toContain('building')
  expect(flat).not.toContain('E1.1') // child tasks are never listed
  expect(rowTexts(await ui.drawn()).filter(t => t === 'E1 E2 E3 E4'), 'the shared lane drawn exactly once').toHaveLength(1)
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
  await ui.press({ key: 'dash-tab-lanes' })
  const rowA = await ui.find({ key: 'E1 E2 E3' })
  const rowB = await ui.find({ key: 'E5' })
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
  expect(await ui.find({ text: /build 12m/ }), 'status word and age still found').toBeTruthy()
  const row = await ui.find({ key: 'F6' })
  expect(row, 'the plan row').toBeDefined()
  const flat = rowTexts(row).join('')
  expect(flat).toContain('…')
  expect(flat).toContain('build 12m') // same row as the (truncated) title
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
    await ui.redraw(paneProps(cols))
    expect(await ui.find({ text: new RegExp(`^─{${String(cols)}}$`) }), `the rule at ${cols}`).toBeTruthy()
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
      .toEqual(['dash-tab-overview', 'dash-tab-lanes', 'dash-tab-plan', 'dash-tab-usage', 'dash-tab-files', 'dash-tab-events', 'dash-tab-waiting', 'dash-tab-kanban', 'dash-tab-todo', 'dash-tab-settings'])
    // Settings is live since S3 (loading line, then live rows) — the S3 tests cover it.
    for (const { key, name } of TAB_BUTTONS.filter(t => t.key !== 'dash-tab-settings')) {
      await ui.press({ key })
      expect(await ui.find({ text: new RegExp(`^─{${String(cols)}}$`) }), `${name} rule at ${cols}`).toBeTruthy()
    }
  }
})

/** A world whose root overview lists the plan `settings-plans-db` and whose `plans <id>` read answers the
 *  captured detail reply: the Plan tab is store-driven (2341d2c), so its phase rows and `plan-fold-*`
 *  buttons exist only once a plan is listed and opened. Every other spawn gets the default answer. */
function planStoreWorld(on: On, files: Files): ReturnType<typeof mockWorld> {
  const HOME = '/tmp/mk-c12'
  const reply = (body: unknown) => ({ value: { exitCode: 0, stdout: JSON.stringify(body), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
  const clock = mockWorld(on, {
    env: { MK_HOME: HOME },
    process: argv => {
      if (argv.includes('overview')) {
        return reply({ ok: true, projects: [], plans: [{ project: 'matrixkoder', plan: planDetail.id, title: planDetail.title, status: 'running', created: 'c', live_run: null, counts: { building: 2 } }] })
      }
      if (argv.includes('plans') && argv.includes(planDetail.id)) return reply(planDetail)
      return undefined
    },
  })
  on('fs.exists', (_$, e) => ({ value: [`${HOME}/mk.db`, `${DATA_DIR}/mk.db`].includes(String(e.path)) }))
  mockFs(on, files)
  return clock
}

/** list -> open the plan -> let the detail read land; the phase fold buttons are then drawn. */
async function openPlanDetail(ui: { press: (q: { key: string }) => Promise<unknown> }, clock: ReturnType<typeof mockWorld>): Promise<void> {
  await ui.press({ key: 'dash-tab-plan' })
  for (let i = 0; i < 5; i++) await clock.advance(0)
  await ui.press({ key: `plans-detail-${planDetail.id}` })
  await clock.advance(5000)
  for (let i = 0; i < 10; i++) await clock.advance(0)
}

test('C12: the view state starts empty — every phase folded — and only a press opens one', { plugins: [worldFixtures] }, async ($, on) => {
  const clock = planStoreWorld(on, worldFiles())
  await $.session.start(sessionStart())
  for (let i = 0; i < 5; i++) await clock.advance(0)
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await openPlanDetail(ui, clock)
  // the empty default: no task row is drawn before any press
  expect(await ui.find({ type: 'Button', key: 'plan-fold-R1' }), 'the phase fold button is drawn').toBeTruthy()
  expect(await ui.find({ type: 'Button', key: 'plan-task-R1.1' }), 'nothing unfolded at the default').toBeUndefined()
  await ui.press({ key: 'plan-fold-R1' })
  expect(await ui.find({ type: 'Button', key: 'plan-task-R1.1' }), 'the press opened the phase').toBeTruthy()
})

test('C12: every tab call receives the ui handle — all six tabs act on it, not just Overview and Plan', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  // three options, so the Overview waiting card folds b and c behind ⋯ (waiting-opt-a-more-2)
  setRun(files, run => { run.waiting = { question: "P6 needs P4's schema change. Hold P6?", options: ['Hold P6', 'Rebase later', 'Fold'], rec: 'Hold P6', since: new Date(NOW).toISOString() } })
  const clock = planStoreWorld(on, files)
  await $.session.start(sessionStart())
  for (let i = 0; i < 5; i++) await clock.advance(0)
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  // the four-argument tabs (lanes, usage, waiting, events) take the extra argument through the
  // pane's widened call and still render their body; Plan is store-driven and acts on ui below
  // Settings is live since S3 (loading line, then live rows) — the S3 tests cover it.
  for (const { key, name } of TAB_BUTTONS.filter(t => !['dash-tab-settings', 'dash-tab-kanban', 'dash-tab-files', 'dash-tab-todo', 'dash-tab-plan'].includes(t.key))) {
    await ui.press({ key })
    await expectTabBody(ui, key, `${name} renders with the ui argument`)
  }
  // Overview: the ⋯ reveal reads its flag back from ui and draws the rest
  await ui.press({ key: 'dash-tab-overview' })
  expect(await ui.find({ text: /b Rebase later/ }), 'hidden before the press').toBeUndefined()
  await ui.press({ key: 'waiting-opt-a-more-2' })
  expect(await ui.find({ text: /b Rebase later/ }), 'the Overview reveal after the press').toBeTruthy()
  // Plan: the phase rows fold through ui
  await openPlanDetail(ui, clock)
  expect(await ui.find({ type: 'Button', key: 'plan-task-R1.1' }), 'folded to start').toBeUndefined()
  await ui.press({ key: 'plan-fold-R1' })
  expect(await ui.find({ type: 'Button', key: 'plan-task-R1.1' }), 'the fold reaches the Plan tab through ui').toBeTruthy()
  // Lanes: a lane's pause control re-renders that lane with its resume button
  await ui.press({ key: 'dash-tab-lanes' })
  expect(await ui.find({ type: 'Button', key: 'f8-resume' }), 'a building lane offers pause, not resume').toBeUndefined()
  await ui.press({ key: 'f8-pause' })
  expect(await ui.find({ type: 'Button', key: 'f8-resume' }), 'the paused lane offers resume').toBeTruthy()
  // Waiting: ⋯ reveals the folded actions; a pick marks the option it picked
  await ui.press({ key: 'dash-tab-waiting' })
  const labels = await tabLabels(ui)
  expect(labels, 'the tab bar keeps its own labels while a body draws buttons').toHaveLength(10)
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

test('S2.3: hotkey 9 switches to the Settings tab', { plugins: [worldFixtures] }, async ($, on) => {
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
  expect((await ui.findAll({ type: 'Button' })).find(b => b.props.key === 'set-scope-project'), 'the settings scope bar').toBeTruthy()
  expect(await ui.find({ text: /Checkout v2/ }), 'the real run header, never the mock world').toBeUndefined()
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
  expect(keys).toContain('dash-tab-overview')
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
  expect((await ui.findAll({ type: 'Button' })).find(b => b.props.key === 'set-scope-project'), 'the settings scope bar').toBeTruthy()
})

test('S2.3: defaultTab opens the pane on that tab', { plugins: [worldFixtures], options: { defaultTab: 'settings' } }, async ($, on) => {
  const files = worldFiles()
  const calls: (readonly string[])[] = []
  const clock = mockSettingsBackend(on, files, calls, R3_FIELDS)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await loadLiveSettings(clock, ui, calls) // the render fallback loads it, no press needed
  expect(await ui.find({ text: /MOCK · fixture data/ }), 'never mock rows').toBeUndefined()
  expect((await ui.findAll({ type: 'Button' })).find(b => b.props.key === 'set-scope-project'), 'the settings scope bar').toBeTruthy()
  expect(await ui.find({ text: /Checkout v2/ }), 'the real run header, never the mock world').toBeUndefined()
})

// at 36 cols only the open tab keeps its word, so 'Overview' in the bar means Overview is open
test('S2.3: an unknown or retired (old) defaultTab keeps the Overview default', { plugins: [worldFixtures], options: { defaultTab: 'old' } }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(36) })
  expect(await tabLabels(ui), 'Overview stays the default').toContain('Overview')
})

test('S2.3: a hidden defaultTab falls back to Overview', { plugins: [worldFixtures], options: { defaultTab: 'lanes', hiddenTabs: 'lanes' } }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(36) })
  expect(await tabLabels(ui), 'Overview stays the default').toContain('Overview')
})

test('S2.3: raster:false draws the eighth-block fallback (no Raster); default draws Raster', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-lanes' })
  expect(await ui.findAll({ type: 'Raster' }), 'the live lane stage strip is a Raster by default').not.toHaveLength(0)
})

test('S2.3: raster:false draws the eighth-block fallback (no Raster)', { plugins: [worldFixtures], options: { raster: false } }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-lanes' })
  expect(await ui.findAll({ type: 'Raster' }), 'no Raster anywhere under raster:false').toHaveLength(0)
  expect(await ui.find({ text: /3 lanes · 2 live/ }), 'the live lanes still render').toBeTruthy()
  // R3 LOW 2: a second bar-drawing tab (Usage draws pickBar strips) falls back too.
  await ui.press({ key: 'dash-tab-usage' })
  expect(await ui.findAll({ type: 'Raster' }), 'no Raster on Usage either').toHaveLength(0)
  expect(await ui.find({ text: /> QUOTA/ }), 'the live Usage still renders').toBeTruthy()
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
      typeof c === 'object' && c !== null &&
      String((c as R3DrawnNode).props?.key ?? '').startsWith('dash-tab-'))
  const walk = (node: unknown, inCard: boolean): void => {
    if (typeof node !== 'object' || node === null) return
    const n = node as R3DrawnNode
    const card = inCard || (n.type === 'Box' && n.props?.borderStyle === 'round' && !/^(dash-tab|set-scope)-/.test(String(n.props?.key ?? '')))
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
      // the bus host's own spawns are not Settings calls (BD runs them on session.start)
      if (isBus(argv) || isBusEnabledGet(argv)) return undefined
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
  await ui.press({ key: 'dash-tab-overview' })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls, 3)
  const mk = mkConfigCalls(calls)
  expect(mk.filter(a => a[a.length - 1] === 'migrate')).toHaveLength(1) // still once per session
  expect(mk.filter(a => a[a.length - 1] === '--json')).toHaveLength(2)
  // the Child scope is still the active one: it draws as tint text, no press needed
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
  await ui.press({ key: 'dash-tab-overview' })
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
      if (isBus(argv) || isBusEnabledGet(argv)) return undefined // the bus host's own spawns are not Settings calls
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
  // L7.6: the note names the REAL path it probed (project.yaml on a migrated
  // project), not a hardcoded file name — this is the pre-migration answer here.
  // the note truncates at the pane width; the probed folder prefix fits
  expect(await ui.find({ text: new RegExp(`No ${DATA_DIR.replace(/[/.]/g, ch => `\\${ch}`)}`) }), 'the note names the probed data dir').toBeTruthy()
  expect(await ui.find({ text: /MOCK · fixture data/ }), 'never mock rows').toBeUndefined()
  await ui.press({ key: 'set-scope-about' })
  expect(await ui.find({ text: /Status chip/ }), 'the global row still shows').toBeTruthy()
})

test('L7.6: the Settings tab loads on a MIGRATED project — the C2 project.yaml, not matrixkoder.yaml', { plugins: [worldFixtures] }, async ($, on) => {
  // The real layout mk-migrate --switch produces, captured from its own output:
  // the root row's db_path is `<home>/projects/<slug>/mk.db`, so the data dir the
  // dashboard resolves IS the C2 folder and the yaml there is `project.yaml`
  // (mkpaths.project_yaml / mk-config.project_yaml_path's answer).
  const slug = 'matrixkoder'
  const c2 = `/home/mk/.matrixkoder/projects/${slug}`
  const projectYaml = `${c2}/project.yaml`
  const files = worldFiles()
  files[projectYaml] = 'session:\n  stall_min: 20\n'
  mockFs(on, files, [c2, `${PROJECT_DIR}/.git`], [projectYaml])
  const calls: (readonly string[])[] = []
  const clock = mockWorld(on, {
    process: argv => {
      if (isBus(argv) || isBusEnabledGet(argv)) return undefined
      calls.push(argv)
      // the root store, as `mkdb.py projects --json` answers it after a switch
      if (argv.some(a => a.endsWith('scripts/mkdb.py'))) {
        return { value: { exitCode: 0, stdout: `${JSON.stringify({ projects: [{ slug, repo: PROJECT_DIR, data_dir: DATA_DIR, db_path: `${c2}/mk.db` }] })}\n`, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      if (!argv.some(a => a.endsWith('scripts/mk-config.py'))) return undefined
      const sub = argv[argv.indexOf('--data') + 2]
      const stdout = sub === 'migrate'
        ? '{"ok":true,"changed":[],"notes":[]}\n'
        : `${JSON.stringify({ ok: true, file: projectYaml, fields: S3_FIELDS })}\n`
      return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    },
  })
  on('session.cwd', () => ({ value: PROJECT_DIR }))
  on('config.list', () => ({ value: [] }))
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-settings' })
  await loadLiveSettings(clock, ui, calls)
  await ui.press({ key: 'set-scope-child' }) // S3_FIELDS' rows live in the Child scope
  await ui.redraw(paneProps(46))

  // the tab runs migrate + list with the C2 folder as --data, exactly as before
  const mk = mkConfigCalls(calls)
  expect(mk.map(a => a[a.length - 1])).toEqual(['migrate', '--json'])
  for (const a of mk) expect(a[a.indexOf('--data') + 1]).toBe(c2)
  // no "No matrixkoder.yaml" note: the probed file is the C2 project.yaml
  expect(await ui.find({ text: /No matrixkoder\.yaml/ }), 'never the pre-migration note').toBeUndefined()
  // the live rows arrived from the migrated project
  expect(await ui.find({ text: /MOCK · fixture data/ }), 'no fixture rows').toBeUndefined()
  expect(await ui.find({ text: /^20m$/ }), 'the live yaml value draws').toBeTruthy()
  // the scope note names the real file, not the bare shape label; the line
  // truncates at the pane width, so match the folder prefix that fits
  expect(await ui.find({ text: new RegExp(c2.replace(/[/.]/g, ch => `\\${ch}`)) }), 'the scope note carries the live path').toBeTruthy()
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
  await ui.press({ key: 'dash-tab-overview' })
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

test('D8A: hotkey k opens the Kanban tab between Waiting and Settings', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on, { env: { MK_HOME: '/tmp/mk-d8a-missing' } })
  on('fs.exists', () => ({ value: false }))
  await $.session.start(sessionStart())
  await clock.advance(0)
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  const buttons = await ui.findAll({ type: 'Button' })
  const keys = buttons.map(b => String(b.props.key))
  const w = keys.indexOf('dash-tab-waiting')
  const k = keys.indexOf('dash-tab-kanban')
  const t = keys.indexOf('dash-tab-todo')
  const o = keys.indexOf('dash-tab-settings')
  expect(k, 'kanban sits after waiting').toBe(w + 1)
  expect(t, 'todo sits after kanban').toBe(k + 1)
  expect(o, 'settings sits after todo').toBe(t + 1)
  const kanban = buttons.find(b => b.props.key === 'dash-tab-kanban')
  expect(kanban?.props.hotkey).toBe('k')
  expect(String(kanban?.props.label)).toContain('Kanban')
  await ui.press({ key: 'dash-tab-kanban' })
  expect(await ui.find({ text: 'no store yet' }), 'missing root store draws the empty state').toBeTruthy()
})

test('D8A: a dashboard tick reads the root overview once when the root store exists, and never when it does not', { plugins: [worldFixtures] }, async ($, on) => {
  const calls: string[][] = []
  const clock = mockWorld(on, {
    env: { MK_HOME: '/tmp/mk-d8a-home' },
    process: argv => {
      calls.push([...argv])
      if (argv.includes('overview')) {
        return { value: { exitCode: 0, stdout: '{"ok":true,"projects":[],"plans":[]}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      return undefined
    },
  })
  on('fs.exists', (_$, e) => ({ value: String(e.path).endsWith('/tmp/mk-d8a-home/mk.db') }))
  mockFs(on, worldFiles())
  await $.session.start(sessionStart())
  await clock.advance(0)
  const cardCalls = () => calls.filter(a => a.includes('overview'))
  expect(cardCalls(), 'the opening tick reads the root overview once').toHaveLength(1)
  expect(cardCalls()[0]).toContain('--json')
  expect(cardCalls()[0]).toContain('--home')
  const before = cardCalls().length
  await clock.advance(5000)
  expect(cardCalls().length, 'the next tick reads the overview once more').toBe(before + 1)
})


test('D8A R2: MK_HOME unset reads ~/.matrixkoder/mk.db and shows no store yet', { plugins: [worldFixtures] }, async ($, on) => {
  const calls: string[][] = []
  const exists: string[] = []
  const clock = mockWorld(on, {
    process: argv => {
      calls.push([...argv])
      return undefined
    },
  })
  on('fs.exists', (_$, e) => {
    exists.push(String(e.path))
    return { value: false }
  })
  mockFs(on, worldFiles())
  await $.session.start(sessionStart())
  await clock.advance(0)
  const homeIdx = calls.flat().indexOf('--home')
  const cardCalls = calls.filter(a => a.includes('cards'))
  expect(cardCalls, 'a missing store never spawns mkdb').toHaveLength(0)
  expect(exists.some(p => p === '/home/mk/.matrixkoder/mk.db'), `exists looked at ${exists.join(',')}`).toBe(true)
  expect(exists.some(p => p === '/home/mk/mk.db'), 'must not probe $HOME/mk.db').toBe(false)
  void homeIdx
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-kanban' })
  expect(await ui.find({ text: 'no store yet' })).toBeTruthy()
  expect(await ui.find({ text: /alpha/ })).toBeUndefined()
  expect(await ui.find({ text: /beta/ })).toBeUndefined()
  expect(await ui.find({ text: /gamma/ })).toBeUndefined()
})

test('D8A R2: a present store with zero linked projects draws an empty board, not the fixture', { plugins: [worldFixtures] }, async ($, on) => {
  const calls: string[][] = []
  const clock = mockWorld(on, {
    process: argv => {
      calls.push([...argv])
      if (argv.includes('overview')) {
        return { value: { exitCode: 0, stdout: '{"ok":true,"projects":[],"plans":[]}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      return undefined
    },
  })
  on('fs.exists', (_$, e) => ({ value: String(e.path) === '/home/mk/.matrixkoder/mk.db' }))
  mockFs(on, worldFiles())
  await $.session.start(sessionStart())
  await clock.advance(0)
  const cardCalls = calls.filter(a => a.includes('overview'))
  expect(cardCalls, 'one overview read').toHaveLength(1)
  const homeAt = cardCalls[0]!.indexOf('--home')
  expect(cardCalls[0]![homeAt + 1]).toBe('/home/mk/.matrixkoder')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-kanban' })
  expect(await ui.find({ text: 'no store yet' }), 'a present store is not the empty-store line').toBeUndefined()
  expect(await ui.find({ text: 'Backlog 0' })).toBeTruthy()
  expect(await ui.find({ text: /alpha/ })).toBeUndefined()
})


test('FC: a corrupt per-project read shows an error and keeps the last good cards', { plugins: [worldFixtures] }, async ($, on) => {
  let mode: 'good' | 'corrupt' = 'good'
  const calls: string[][] = []
  const clock = mockWorld(on, {
    env: { MK_HOME: '/tmp/mk-fc-home' },
    process: argv => {
      calls.push([...argv])
      if (argv.includes('overview')) {
        return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, projects: [{ slug: 'alpha', repo: '/r/alpha', data_dir: '/srv/alpha/.matrixkoder', db_path: null }], plans: [] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      if (!argv.includes('run-snapshot')) return undefined
      if (mode === 'corrupt') {
        // real mkdb.py against a corrupt mk.db: empty stdout, exit 1
        return { value: { exitCode: 1, stdout: '', stderr: 'sqlite3.DatabaseError: file is not a database', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      const snap = { ok: true, runs: [], active: 'r', waits: [], run: { id: 'r', plan: null, status: 'active', tasks: [{ id: 'D3A', title: 'Writer', status: 'building', parent: 'D3', updated: '2026-10-12T09:00:00+02:00' }] } }
      return { value: { exitCode: 0, stdout: JSON.stringify(snap), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    },
  })
  on('fs.exists', (_$, e) => ({ value: String(e.path) === '/tmp/mk-fc-home/mk.db' }))
  mockFs(on, worldFiles())
  await $.session.start(sessionStart())
  for (let i = 0; i < 20; i++) await clock.advance(0)
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-kanban' })
  expect(await ui.find({ text: /D3A/ }), 'the good read draws the card').toBeTruthy()
  mode = 'corrupt'
  await clock.advance(5000)
  for (let i = 0; i < 30; i++) await clock.advance(0)
  expect(await ui.find({ text: /cards read failed/ }), 'the failed read is a visible error').toBeTruthy()
  expect(await ui.find({ text: /stale/ }), 'last good cards are flagged stale').toBeTruthy()
  expect(await ui.find({ text: /D3A/ }), 'last good card stays').toBeTruthy()
  expect(await ui.find({ text: 'no store yet' }), 'an error is not the empty-store line').toBeUndefined()
})

test('FC: {ok:false} shows an error, not empty columns', { plugins: [worldFixtures] }, async ($, on) => {
  const clock = mockWorld(on, {
    env: { MK_HOME: '/tmp/mk-fc-okfalse' },
    process: argv => {
      if (argv.includes('overview')) {
        return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, projects: [{ slug: 'alpha', repo: '/r/alpha', data_dir: '/d/alpha', db_path: null }], plans: [] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      if (!argv.includes('run-snapshot')) return undefined
      return { value: { exitCode: 0, stdout: '{"ok":false,"error":"locked"}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    },
  })
  on('fs.exists', (_$, e) => ({ value: String(e.path) === '/tmp/mk-fc-okfalse/mk.db' }))
  mockFs(on, worldFiles())
  await $.session.start(sessionStart())
  for (let i = 0; i < 20; i++) await clock.advance(0)
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-kanban' })
  expect(await ui.find({ text: /cards read failed: locked/ })).toBeTruthy()
  expect(await ui.find({ text: 'no store yet' })).toBeUndefined()
  expect(await ui.find({ text: 'Backlog 0' })).toBeUndefined()
})

test('FC: non-JSON stdout shows an error, not empty columns', { plugins: [worldFixtures] }, async ($, on) => {
  const clock = mockWorld(on, {
    env: { MK_HOME: '/tmp/mk-fc-nonjson' },
    process: argv => {
      if (!argv.includes('cards')) return undefined
      return { value: { exitCode: 0, stdout: 'not-json', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    },
  })
  on('fs.exists', (_$, e) => ({ value: String(e.path) === '/tmp/mk-fc-nonjson/mk.db' }))
  mockFs(on, worldFiles())
  await $.session.start(sessionStart())
  for (let i = 0; i < 20; i++) await clock.advance(0)
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-kanban' })
  expect(await ui.find({ text: /cards read failed: not JSON/ })).toBeTruthy()
  expect(await ui.find({ text: 'no store yet' })).toBeUndefined()
  expect(await ui.find({ text: 'Backlog 0' })).toBeUndefined()
})

test('FC: a thrown spawn shows an error, not the empty-store line', { plugins: [worldFixtures] }, async ($, on) => {
  const clock = mockWorld(on, {
    env: { MK_HOME: '/tmp/mk-fc-throw' },
    process: argv => {
      if (argv.includes('overview')) {
        return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, projects: [{ slug: 'alpha', repo: '/r/alpha', data_dir: '/d/alpha', db_path: null }], plans: [] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      if (!argv.includes('run-snapshot')) return undefined
      throw new Error('spawn python3 ENOENT')
    },
  })
  on('fs.exists', (_$, e) => ({ value: String(e.path) === '/tmp/mk-fc-throw/mk.db' }))
  mockFs(on, worldFiles())
  await $.session.start(sessionStart())
  for (let i = 0; i < 20; i++) await clock.advance(0)
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-kanban' })
  expect(await ui.find({ text: /cards read failed: cannot run mkdb/ })).toBeTruthy()
  expect(await ui.find({ text: 'no store yet' })).toBeUndefined()
  expect(await ui.find({ text: 'Backlog 0' })).toBeUndefined()
})

test('FC: a slow per-project read does not hold the tick, and only one read is in flight', { plugins: [worldFixtures] }, async ($, on) => {
  const held: Array<(v: { value: { exitCode: number; stdout: string; stderr: string; isStdoutTruncated: boolean; isStderrTruncated: boolean } }) => void> = []
  let started = 0
  const clock = mockWorld(on, {
    env: { MK_HOME: '/tmp/mk-fc-slow' },
    process: argv => {
      if (argv.includes('overview')) {
        return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, projects: [{ slug: 'alpha', repo: '/r/alpha', data_dir: '/d/alpha', db_path: null }], plans: [] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      if (!argv.includes('run-snapshot')) return undefined
      // Hold ONLY the Kanban cards read — the overview row's store (/d/alpha). The
      // pane's OWN per-project read (L5: `--data <session data dir>`) is awaited by the
      // refresh tick by design, so holding it would hold the tick and prove nothing about
      // the fire-and-forget read this test is about. Answer it "no store" so the tick
      // proceeds to the file fallback.
      if (argv[argv.indexOf('--data') + 1] !== '/d/alpha') {
        return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, store: false, runs: [] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      started += 1
      return new Promise<{ value: { exitCode: number; stdout: string; stderr: string; isStdoutTruncated: boolean; isStderrTruncated: boolean } }>(resolve => {
        held.push(resolve)
      })
    },
  })
  on('fs.exists', (_$, e) => ({ value: String(e.path) === '/tmp/mk-fc-slow/mk.db' }))
  mockFs(on, worldFiles())
  await $.session.start(sessionStart())
  for (let i = 0; i < 10; i++) await clock.advance(0)
  expect(started, 'the read was started').toBe(1)
  await clock.advance(5000)
  for (let i = 0; i < 10; i++) await clock.advance(0)
  expect(started, 'a second read does not start while one is in flight').toBe(1)
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: 'Overview' }), 'the pane still rendered').toBeTruthy()
  held[0]?.({ value: { exitCode: 0, stdout: JSON.stringify({ ok: true, runs: [], active: null, waits: [], run: null }), isStdoutTruncated: false, isStderrTruncated: false, stderr: '' } })
  for (let i = 0; i < 20; i++) await clock.advance(0)
  await ui.press({ key: 'dash-tab-kanban' })
  expect(await ui.find({ text: 'Backlog 0' }), 'the landed read draws the empty board').toBeTruthy()
})

// D8B: the tick's view state feeds the Waiting tab. `waitingPane` parses the
// stored asks once; `waitingPaneView` picks the fixture, the empty state or the
// run-snapshot fallback from that one parse.
test('the waiting tick feeds the tab: live, empty, down, then the fixture', () => {
  const ask = { id: 'm-1', run: 'r1', task: 'T1', text: 'how to proceed?', ts: '2026-10-12T12:23:53Z' }
  const live = waitingPane({ 'waiting:src': 'live', 'waiting:pending': JSON.stringify([ask]) })
  expect(live.src).toBe('live')
  expect(live.asks).toHaveLength(1)
  expect(live.asks[0]?.id).toBe('m-1')
  const again = waitingPane({ 'waiting:src': 'live', 'waiting:pending': JSON.stringify([ask]) })
  expect(again.asks).toEqual(live.asks)
  const empty = waitingPaneView({ src: 'live', asks: [] }, null)
  expect(empty.mock).toBe(false)
  expect(empty.question).toBe('')
  const down = waitingPaneView({ src: 'down', asks: [] }, { question: 'hold the merge?', since: '17:41' })
  expect(down.mock).toBe(false)
  expect(down.question).toBe('hold the merge?')
  const fixture = waitingPaneView({ src: 'down', asks: [] }, null)
  expect(fixture.mock).toBe(true)
  const unset = waitingPaneView({ src: '', asks: [] }, null)
  expect(unset.mock).toBe(true)
  const garbage = waitingPane({ 'waiting:src': 'live', 'waiting:pending': '{not json' })
  expect(garbage.asks).toEqual([])
})

// D8B R3: a slow pending read must not stack. The mock holds the promise, so
// four ticks later exactly one call is outstanding; releasing it lets the next
// tick start exactly one more.
test('a slow pending read stays one in flight across four ticks', { plugins: [worldFixtures] }, async ($, on) => {
  const held: Array<(v: { value: { exitCode: number; stdout: string; stderr: string; isStdoutTruncated: boolean; isStderrTruncated: boolean } }) => void> = []
  let started = 0
  const clock = mockWorld(on, {
    process: argv => {
      if (!argv.includes('pending')) return undefined
      started += 1
      return new Promise<{ value: { exitCode: number; stdout: string; stderr: string; isStdoutTruncated: boolean; isStderrTruncated: boolean } }>(resolve => {
        held.push(resolve)
      })
    },
  })
  mockFs(on, worldFiles())
  await $.session.start(sessionStart())
  for (let i = 0; i < 4; i++) {
    await clock.advance(5000)
    for (let j = 0; j < 5; j++) await clock.advance(0)
  }
  expect(started, 'one read outstanding after four ticks').toBe(1)
  expect(held).toHaveLength(1)
  held[0]?.({ value: { exitCode: 0, stdout: '{"ok":true,"run":"*","pending":[]}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
  for (let j = 0; j < 10; j++) await clock.advance(0)
  await clock.advance(5000)
  for (let j = 0; j < 5; j++) await clock.advance(0)
  expect(started, 'the next tick starts exactly one new read').toBe(2)
  // settle the second read too, so no promise outlives the test
  held[1]?.({ value: { exitCode: 0, stdout: '{"ok":true,"run":"*","pending":[]}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
  for (let j = 0; j < 10; j++) await clock.advance(0)
})

// ── L7.2: the Plan list from the root overview, and k across linked projects ──

test('L7.2: the Plan List renders from the root overview with the project DB unavailable', { plugins: [worldFixtures] }, async ($, on) => {
  const calls: string[][] = []
  const clock = mockWorld(on, {
    env: { MK_HOME: '/tmp/mk-l72-list' },
    process: argv => {
      calls.push([...argv])
      const reply = (body: unknown) => ({ value: { exitCode: 0, stdout: JSON.stringify(body), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
      if (argv.includes('overview')) {
        return reply({
          ok: true, projects: [],
          plans: [{ project: 'matrixkoder', plan: 'alpha', title: 'Alpha plan', status: 'running', created: 'c', live_run: 'alpha-r1', counts: { building: 2 } }],
        })
      }
      // EVERY project-DB subcommand fails: the List must still render (acceptance).
      if (argv.includes('plans')) return { value: { exitCode: 2, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      return undefined
    },
  })
  on('fs.exists', (_$, e) => ({ value: String(e.path).endsWith('/tmp/mk-l72-list/mk.db') }))
  mockFs(on, worldFiles())
  await $.session.start(sessionStart())
  for (let i = 0; i < 5; i++) await clock.advance(0)
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-plan' })
  for (let i = 0; i < 5; i++) await clock.advance(0)
  expect(await ui.find({ text: /alpha/ }), 'the plan row from the root overview').toBeTruthy()
  expect(await ui.find({ text: /Alpha plan/ }), 'the title from the root plan_summary').toBeTruthy()
  expect(calls.some(a => a.includes('overview')), 'the list read the root overview').toBe(true)
  // The retired List read was `mkdb.py plans --json --data <project>` (it opened the project
  // DB). It must not appear: the list is the root overview alone (acceptance).
  expect(calls.some(a => a.includes('plans') && a.includes('--data')), 'the list never opened a project DB').toBe(false)
  expect(await ui.find({ text: /⚠/ }), 'no error line: the list is not the failing read').toBeUndefined()
})

test('L7.2: the top-level k reads open tasks of every linked project — migrated via db_path, pre-migration via its data_dir', { plugins: [worldFixtures] }, async ($, on) => {
  const HOME = '/tmp/mk-l72-k'
  const MIGRATED_DIR = `${HOME}/projects/alpha`
  const MIGRATED_DB = `${MIGRATED_DIR}/mk.db`
  const LEGACY_DIR = '/srv/legacy/.matrixkoder'
  const calls: string[][] = []
  const clock = mockWorld(on, {
    env: { MK_HOME: HOME },
    process: argv => {
      calls.push([...argv])
      const reply = (body: unknown) => ({ value: { exitCode: 0, stdout: JSON.stringify(body), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
      if (argv.includes('overview')) {
        return reply({
          ok: true,
          projects: [
            { slug: 'alpha', name: 'alpha', repo: '/srv/alpha', data_dir: '/srv/alpha/.matrixkoder', mode: 'home', db_path: MIGRATED_DB },
            { slug: 'legacy', name: 'legacy', repo: '/srv/legacy', data_dir: LEGACY_DIR, mode: 'home', db_path: null },
          ],
          plans: [],
        })
      }
      const taskSnap = (id: string, title: string) => ({
        ok: true, runs: [], active: 'r1', waits: [],
        run: { id: 'r1', plan: null, status: 'active', tasks: [{ id, title, status: 'building', parent: null, updated: '2026-10-12T09:00:00+02:00' }] },
      })
      const dataAt = argv.indexOf('--data')
      const dir = dataAt >= 0 ? argv[dataAt + 1] ?? '' : ''
      if (dir === MIGRATED_DIR) return reply(taskSnap('A-OPEN', 'alpha open task'))
      if (dir === LEGACY_DIR) return reply(taskSnap('L-OPEN', 'legacy open task'))
      return undefined
    },
  })
  on('fs.exists', (_$, e) => ({ value: [`${HOME}/mk.db`, MIGRATED_DB, `${LEGACY_DIR}/mk.db`].includes(String(e.path)) }))
  mockFs(on, worldFiles())
  await $.session.start(sessionStart())
  for (let i = 0; i < 5; i++) await clock.advance(0)
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-kanban' })
  for (let i = 0; i < 5; i++) await clock.advance(0)
  expect(await ui.find({ text: /A-OPEN/ }), 'the migrated project’s open task').toBeTruthy()
  expect(await ui.find({ text: /L-OPEN/ }), 'the pre-migration project’s open task').toBeTruthy()
  const dirs = calls.map(a => {
    const i = a.indexOf('--data')
    return i >= 0 ? a[i + 1] ?? '' : ''
  })
  expect(dirs, 'the migrated row read the C2 project folder').toContain(MIGRATED_DIR)
  expect(dirs, 'the db_path-NULL row fell back to its legacy data_dir').toContain(LEGACY_DIR)
})

// a real-shaped snapshot for the Overview's pure functions (types cast: only the read fields are set)
const NOW_MS = Date.parse('2026-10-07T18:00:00Z')
const ago = (ms: number): string => new Date(NOW_MS - ms).toISOString()
const task = (o: Record<string, unknown>) => ({ title: 't', deps: [], wave: 1, agent: 'codex', model: 'gpt-6.1-sol', effort: 'high', lane: null, round: 0, ...o })
const OVERVIEW_SNAP = {
  now: NOW_MS,
  dataDir: '/d',
  runs: [],
  projectAgents: ['agy', 'codex'],
  usage: {
    agents: {
      agy: {
        exhausted_until: ago(-3600_000),
        pools: {
          gemini: { five_hour: { used_pct: 54, resets_at: ago(-3600_000) }, weekly: null, exhausted_until: null },
          'claude-gpt': { five_hour: { used_pct: 100, resets_at: ago(-3600_000) }, weekly: { used_pct: 80, resets_at: ago(-3600_000) }, exhausted_until: ago(-3600_000) },
        },
      },
      codex: { five_hour: { used_pct: 10, resets_at: ago(-3600_000) }, weekly: null, exhausted_until: null, pools: { default: { five_hour: { used_pct: 10, resets_at: ago(-3600_000) }, weekly: null, exhausted_until: null } } },
    },
  },
  run: {
    id: 'r1', feature: 'f', branch: 'b', stage: 6, next: '', head_sha: 'abc1234', recent: [],
    waiting: { question: 'Hold P6?', options: ['Hold', 'Fold'], rec: 'Hold', since: ago(0) },
    tasks: [
      task({ id: 'P3', status: 'merged', updated: ago(0) }),
      task({ id: 'P4', status: 'building', updated: ago(14 * 60_000), lane: '../wt-p4' }),
      task({ id: 'P5', status: 'review', round: 2, updated: ago(0), lane: '../wt-p5' }),
      task({ id: 'P6', status: 'blocked', deps: ['P3', 'P4'], updated: ago(0), lane: '../wt-p6' }),
      task({ id: 'P7', status: 'quota', agent: 'agy', model: 'gemini-3.8-flash-lite', updated: ago(0) }),
      task({ id: 'P8', status: 'planned', wave: 3, updated: ago(0) }),
    ],
  },
} as unknown as MkSnapshot

test('the live Overview task chips carry the board copy: build 14m, review r2, blocked P4, quota HH:MM, W3', () => {
  const v = liveOverviewView(OVERVIEW_SNAP)
  const notes = Object.fromEntries(v.tasks.map(t => [t.id, t.note]))
  const hhmm = new Date(NOW_MS + 3600_000)
  expect(notes.P4).toBe('build 14m')
  expect(notes.P5).toBe('review r2')
  expect(notes.P6, 'the first dep that is not done').toBe('blocked P4')
  expect(notes.P7).toBe(`quota ${String(hhmm.getHours()).padStart(2, '0')}:${String(hhmm.getMinutes()).padStart(2, '0')}`)
  expect(notes.P8).toBe('W3')
  expect(v.plan, 'done tasks count in the plan, not in the rows').toEqual({ done: 1, total: 6 })
  expect(taskNote(task({ id: 'Q', status: 'blocked' }) as never, [], OVERVIEW_SNAP, ''), 'no dep, no id').toBe('blocked')
  expect(v.lanes.map(l => l.modelShort)).toEqual(['sol', 'sol', 'sol'])
})

test('the live usage rows: pool names stay unique, an unknown week is null, not 0', () => {
  const v = liveOverviewView(OVERVIEW_SNAP)
  const gem = v.usage.find(u => u.label.includes('gemini'))
  const gpt = v.usage.find(u => u.label.includes('claude-gpt'))
  expect([gem?.short, gpt?.short]).toEqual(['gemini', 'claude-gpt'])
  expect(gem?.weekPct, 'no weekly data').toBeNull()
  expect(gpt?.weekPct).toBe(80)
  // two pools sharing a suffix keep the agent prefix so they never clip alike
  const rows = [{ name: 'agy:default' }, { name: 'codex:default' }, { name: 'agy:gemini' }]
  expect(usageShorts(rows).map(([, short]) => short)).toEqual(['agy:default', 'codex:default', 'gemini'])
})

test('modelShort: family word, case-insensitive, -lite kept, provider prefix dropped', () => {
  expect(['gpt-6.1-sol', 'gemini-3.8-flash', 'Gemini-3.8-FLASH-Lite', 'claude-sonnet-4-5', 'sonnet', 'openai/gpt-6.1-sol', 'openai/', 'glm-5'].map(modelShort))
    .toEqual(['sol', 'flash', 'flash-lite', 'sonnet', 'sonnet', 'sol', 'openai', 'glm-5'])
})

test('the pane footer: counts, waiting omitted at 0, never wider than the row; waiting agrees with the card', () => {
  expect(footerParts(2, 2, 1, 44)).toEqual({ left: '2 live · 2 held · 1 waiting', right: 'tick 5s' })
  expect(footerParts(2, 2, 0, 44).left).toBe('2 live · 2 held')
  for (const w of [10, 20, 30, 34, 44]) {
    const p = footerParts(2, 2, 1, w)
    expect(p.left.length + p.right.length + (p.right === '' ? 0 : 1), `fits ${w}`).toBeLessThanOrEqual(w)
  }
  expect(footerParts(2, 2, 1, 34).right, 'tight: the word tick goes first').toBe('5s')
  const run = OVERVIEW_SNAP.run
  expect(openWaiting([], run), 'no bus asks: the run waiting entry').toBe(1)
  expect(openWaiting([{}, {}], run)).toBe(2)
  expect(openWaiting([], null)).toBe(0)
})

test('runPlanAction runs mk-plan.py <kind> <id> --data and reports ok as null', async () => {
  const calls: string[][] = []
  const run = async (argv: readonly string[]) => { calls.push([...argv]); return { exitCode: 0, stdout: '{"ok":true,"id":"p1"}\n', stderr: '' } }
  expect(await runPlanAction(run, '/root', '/data', 'archive', 'p1')).toBeNull()
  expect(calls).toEqual([['python3', '/root/scripts/mk-plan.py', 'archive', 'p1', '--data', '/data']])
})

test('runPlanAction surfaces the first error of an ok:false reply, else the exit text', async () => {
  const bad = async () => ({ exitCode: 1, stdout: '{"ok":false,"errors":["run r1 is active","second"]}', stderr: '' })
  expect(await runPlanAction(bad, '/root', '/data', 'delete', 'p1')).toBe('run r1 is active')
  const crash = async () => ({ exitCode: 2, stdout: '', stderr: 'boom\n' })
  expect(await runPlanAction(crash, '/root', '/data', 'delete', 'p1')).toBe('delete p1 exit 2: boom')
})

const okRun = (stdout: string, exitCode = 0) => async () => ({ exitCode, stdout, stderr: '' })

test('runPlanAction edge cases: non-object reply, no errors array, errors with no strings', async () => {
  expect(await runPlanAction(okRun('[1]'), '/r', '/d', 'archive', 'p1')).toBe('archive p1 exit 0')
  expect(await runPlanAction(okRun('{"ok":false}', 1), '/r', '/d', 'archive', 'p1')).toBe('archive p1 refused')
  expect(await runPlanAction(okRun('{"ok":false,"errors":[1,""]}', 1), '/r', '/d', 'archive', 'p1')).toBe('archive p1 refused')
})

test('runPlanAction: ok:true with a non-zero exit is a failure', async () => {
  expect(await runPlanAction(okRun('{"ok":true}', 3), '/r', '/d', 'archive', 'p1')).toBe('archive p1 exit 3')
})

test('runPlanAction refuses an id mk-plan.py would read as a flag, without spawning', async () => {
  let spawned = 0
  const run = async () => { spawned += 1; return { exitCode: 0, stdout: '{"ok":true}', stderr: '' } }
  expect(await runPlanAction(run, '/r', '/d', 'delete', '--home')).toBe('bad plan id --home')
  expect(spawned).toBe(0)
})

test('planActionPatch: failure sets the error and stale 0 and does not bump reload', async () => {
  const run = okRun('{"ok":false,"errors":["run r1 is active","x"]}', 1)
  const patch = await planActionPatch(run, '/r', { 'plans:dir': '/d' }, 'delete', 'p1', 5)
  expect(patch).toEqual({ 'plans:error': 'run r1 is active', 'plans:stale': '0' })
})

test('planActionPatch: success bumps plans:reload only', async () => {
  expect(await planActionPatch(okRun('{"ok":true}'), '/r', { 'plans:dir': '/d' }, 'archive', 'p1', 7)).toEqual({ 'plans:reload': '7' })
})

test('planActionPatch: a missing plans:dir spawns nothing and shows an error', async () => {
  let spawned = 0
  const run = async () => { spawned += 1; return { exitCode: 0, stdout: '{"ok":true}', stderr: '' } }
  const patch = await planActionPatch(run, '/r', {}, 'archive', 'p1', 7)
  expect(spawned).toBe(0)
  expect(patch).toEqual({ 'plans:error': 'no project data dir', 'plans:stale': '0' })
})

test('planActionPatch: a thrown spawn becomes the error line', async () => {
  const run = async (): Promise<{ exitCode: number; stdout: string; stderr: string }> => { throw new Error('spawn failed') }
  expect((await planActionPatch(run, '/r', { 'plans:dir': '/d' }, 'archive', 'p1', 7))['plans:error']).toBe('archive p1: spawn failed')
})

test('Files F4.1: the tab bar order is o l p u f e w k t 9 and pressing f opens Files from Overview, Lanes, Plan, Usage, Events and Kanban', { timeoutMs: 15000, plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  const buttons = (await ui.findAll({ type: 'Button' })).filter(b => String(b.props.key).startsWith('dash-tab-'))
  expect(buttons.map(b => b.props.hotkey)).toEqual(['o', 'l', 'p', 'u', 'f', 'e', 'w', 'k', 't', '9'])
  const fromTabs = ['dash-tab-overview', 'dash-tab-lanes', 'dash-tab-plan', 'dash-tab-usage', 'dash-tab-events', 'dash-tab-kanban'] as const
  for (const tabKey of fromTabs) {
    await ui.press({ key: tabKey })
    await ui.press({ key: 'dash-tab-files' })
    const filesBtn = await ui.find({ type: 'Button', key: 'dash-tab-files' })
    expect(filesBtn).toBeDefined()
    expect(filesBtn?.props.label).toBe('Files')
  }
})

test('Files F4.1: hiddenTabs set to files hides the tab', { timeoutMs: 15000, plugins: [worldFixtures], options: { hiddenTabs: 'files' } }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  const keys = (await ui.findAll({ type: 'Button' })).map(b => String(b.props.key))
  expect(keys).not.toContain('dash-tab-files')
})

test('Files F4.1: hiddenTabs set to Files hides the tab (capitalized)', { timeoutMs: 15000, plugins: [worldFixtures], options: { hiddenTabs: 'Files' } }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  const keys = (await ui.findAll({ type: 'Button' })).map(b => String(b.props.key))
  expect(keys).not.toContain('dash-tab-files')
})

test('Files F4.2: main checkout: if <cwd>/.git is a file, parseGitdir, commondir, mainCheckout give the path, else cwd', async () => {
  const cwd1 = '/worktree/lane-1'
  const files1: Record<string, string> = {
    '/worktree/lane-1/.git': 'gitdir: /main/repo/.git/worktrees/lane-1\n',
    '/main/repo/.git/worktrees/lane-1/commondir': '../..\n',
  }
  const mockEngine1 = {
    fs: {
      stat: async (p: string) => ({ kind: p in files1 ? 'file' : 'dir' }),
      read: async (p: string) => files1[p] ?? '',
    },
  }
  const res1 = await resolveMainCheckout(mockEngine1, cwd1)
  expect(res1).toBe('/main/repo')

  const cwd2 = '/plain/repo'
  const mockEngine2 = {
    fs: {
      stat: async (_p: string) => ({ kind: 'dir' }),
      read: async (_p: string) => '',
    },
  }
  const res2 = await resolveMainCheckout(mockEngine2, cwd2)
  expect(res2).toBe(cwd2)

  const cwd3 = '/no/git'
  const mockEngine3 = {
    fs: {
      stat: async (_p: string) => { throw new Error('ENOENT') },
      read: async (_p: string) => '',
    },
  }
  const res3 = await resolveMainCheckout(mockEngine3, cwd3)
  expect(res3).toBe(cwd3)
})

test('Files F4.2: a render on Files with no cache for the chosen root starts walkRoot, including opening straight from saved dashTab', { timeoutMs: 15000, plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on)
  on('state.get', (_$, e, next) => {
    if (e.key === 'dashTab') {
      return { value: { value: 'files', version: 1 } }
    }
    return next(e)
  })
  const walked: string[] = []
  on('fs.list', (_$, e) => {
    walked.push(e.path)
    return { value: [{ name: 'file1.txt', kind: 'file', size: 10, mtimeMs: 1000, isLink: false }] }
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  await ui.redraw(paneProps(46))
  expect(walked.length).toBeGreaterThan(0)
  expect(await ui.find({ text: /file1\.txt/ })).toBeTruthy()
})

test('Files F4.2: walk result lives in module Map, files:v bumped after change or first walk', { timeoutMs: 15000, plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on)
  let latestViewState: Record<string, string> = {}
  on('state.set', (_$, e, next) => {
    if (e.key === 'viewState' && e.value) {
      latestViewState = e.value as Record<string, string>
    }
    return next(e)
  })
  on('fs.list', (_$, e) => {
    return { value: [{ name: 'hello.ts', kind: 'file', size: 10, mtimeMs: 1000, isLink: false }] }
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-files' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  await ui.redraw(paneProps(46))
  expect(await ui.find({ text: /hello\.ts/ })).toBeTruthy()
  expect(Number(latestViewState['files:v'] ?? '0')).toBeGreaterThan(0)
  expect(latestViewState['files:walk']).toBeUndefined()
  expect(latestViewState['files:tree']).toBeUndefined()
})

test('Files F4.2: walk uses SCAN_SKIP and .matrixkoder- prefix only, expand and search need no load', { timeoutMs: 15000, plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on)
  let listCalls = 0
  on('fs.list', (_$, e) => {
    listCalls++
    if (e.path.endsWith('/src')) {
      return { value: [{ name: 'main.ts', kind: 'file', size: 100, mtimeMs: 1000, isLink: false }] }
    }
    return {
      value: [
        { name: 'src', kind: 'dir', size: 0, mtimeMs: 0, isLink: false },
        { name: '.git', kind: 'dir', size: 0, mtimeMs: 0, isLink: false },
        { name: '.omc', kind: 'dir', size: 0, mtimeMs: 0, isLink: false },
        { name: '.matrixkoder-status.json', kind: 'file', size: 0, mtimeMs: 1000, isLink: false },
      ],
    }
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-files' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  await ui.redraw(paneProps(46))
  const initialCalls = listCalls
  await ui.press({ key: 'files-expand' })
  expect(listCalls).toBe(initialCalls)
  await ui.input({ key: 'files-q', text: 'main' })
  expect(listCalls).toBe(initialCalls)
})

test('Files F4.2: lane slots reload every 30s of filesNow without touching Date.now', { timeoutMs: 15000, plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  let laneSlotCalls = 0
  const clock = mockWorld(on, {
    process: argv => {
      if (argv.some(a => a.includes('mk-lane.py')) && argv.includes('list')) {
        laneSlotCalls++
        return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, slots: [] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      return undefined
    },
  })
  on('fs.list', (_$, e) => ({ value: [] }))
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-files' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  expect(laneSlotCalls).toBe(1)

  await clock.advance(29_000)
  await ui.redraw(paneProps(46))
  expect(laneSlotCalls).toBe(1)

  await clock.advance(2_000)
  await ui.redraw(paneProps(46))
  for (let i = 0; i < 10; i++) await clock.advance(0)
  expect(laneSlotCalls).toBe(2)
})

test('Files F4.2: render reads fileActivity only when dashTab is files', { timeoutMs: 15000, plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  let actReadCount = 0
  on('state.get', (_$, e, next) => {
    if (e.key === 'fileActivity') {
      actReadCount++
      return { value: { value: [{ path: `${PROJECT_DIR}/test.ts`, kind: 'write', at: NOW }], version: 1 } }
    }
    return next(e)
  })
  on('fs.list', (_$, e) => ({ value: [{ name: 'test.ts', kind: 'file', size: 10, mtimeMs: 1000, isLink: false }] }))
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(actReadCount).toBe(0)

  await ui.press({ key: 'dash-tab-files' })
  expect(actReadCount).toBeGreaterThan(0)
})

test('Files F4.2: every lane slot gets a root chip, idle ones too', { timeoutMs: 15000, plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on, {
    process: argv => {
      if (argv.some(a => a.includes('mk-lane.py')) && argv.includes('list')) {
        return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, slots: [{ slot: 1, path: '/lane/1', task: 'P4', state: 'busy' }, { slot: 3, path: '/lane/3', task: null, state: 'free' }, { slot: 4, path: '/lane/4', state: 'free' }] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      return undefined
    },
  })
  on('fs.list', () => ({ value: [{ name: 'a.ts', kind: 'file', size: 1, mtimeMs: 1000, isLink: false }] }))
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-files' })
  let keys: string[] = []
  for (let i = 0; i < 60; i++) {
    await clock.advance(0)
    await ui.redraw(paneProps(46))
    keys = (await ui.findAll({ type: 'Button' })).map(b => String(b.props.key)).filter(k => k.startsWith('files-root:'))
    if (keys.includes('files-root:slot-1')) break
  }
  expect(keys).toContain('files-root:main')
  expect(keys).toContain('files-root:slot-1')
  expect(keys).toContain('files-root:slot-3')
  expect(keys).toContain('files-root:slot-4')
})

test('Files F4.2: missing lane root falls back to main checkout with note', { timeoutMs: 15000, plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  let processCalls = 0
  const clock = mockWorld(on, {
    process: argv => {
      if (argv.some(a => a.includes('mk-lane.py')) && argv.includes('list')) {
        processCalls++
        return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, slots: [{ slot: 1, path: '/missing/lane-1', task: 'P4' }] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      return undefined
    },
  })
  on('fs.list', (_$, e) => {
    if (e.path.startsWith('/missing')) {
      throw new Error('ENOENT')
    }
    return { value: [{ name: 'main-repo-file.ts', kind: 'file', size: 10, mtimeMs: 1000, isLink: false }] }
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-files' })
  for (let i = 0; i < 60; i++) {
    await clock.advance(0)
    await ui.redraw(paneProps(46))
    const btn = (await ui.findAll({ type: 'Button' })).find(b => b.props.key === 'files-root:slot-1')
    if (btn) break
  }
  await ui.press({ key: 'files-root:slot-1' })
  for (let i = 0; i < 60 && !(await ui.find({ text: /missing root, fallback to repo/ })); i++) {
    await clock.advance(0)
    await ui.redraw(paneProps(46))
  }
  expect(await ui.find({ text: /missing root, fallback to repo/ })).toBeTruthy()
  expect(await ui.find({ text: /main-repo-file\.ts/ })).toBeTruthy()
})

test('Files F4.2: fs.list rejecting everywhere still draws tab bar with note', { timeoutMs: 15000, plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on)
  on('fs.list', () => {
    throw new Error('EPERM')
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-files' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  await ui.redraw(paneProps(46))
  const tabButtons = await ui.findAll({ type: 'Button' })
  const keys = tabButtons.map(b => String(b.props.key))
  expect(keys).toContain('dash-tab-overview')
  expect(keys).toContain('dash-tab-files')
  expect(await ui.find({ text: /missing root/ })).toBeTruthy()
})

test('Files F4.2: helper-level only, the engine focus call is not proven: filesFocus calls $.ui.focus with the pane id and key, ignores a rejection and a deny; pressing s draws no error', { timeoutMs: 15000, plugins: [worldFixtures] }, async ($, on) => {
  const calls: unknown[] = []
  await filesFocus({ ui: { focus: async args => { calls.push(args); return {} } } }, 'files-q')
  expect(calls).toEqual([{ requestId: PANE_ID, key: 'files-q' }])
  await filesFocus({ ui: { focus: async () => { throw new Error('focus refused') } } }, 'files-q')
  await filesFocus({ ui: { focus: async () => ({ deny: 'site does not hold the keys' }) } }, 'files-q')
  mockFs(on, worldFiles())
  const clock = mockWorld(on)
  on('fs.list', () => ({ value: [{ name: 'test.ts', kind: 'file', size: 10, mtimeMs: 1000, isLink: false }] }))
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: { ...paneProps(46), isFocused: true } })
  await ui.press({ key: 'dash-tab-files' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  await ui.redraw({ ...paneProps(46), isFocused: true })
  await ui.press({ key: 'files-s' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  expect(await ui.find({ text: /Files could not draw/ })).toBeUndefined()
  expect(await ui.find({ type: 'Button', key: 'files-s' })).toBeDefined()
})

test('Files F4.2: Files wiring never writes settings:focus', { timeoutMs: 15000, plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  let shouldReject = false
  on('ui.focus', () => {
    if (shouldReject) throw new Error('focus refused')
    return { deny: 'denied' }
  })
  on('fs.list', () => ({
    value: [
      { name: 'a.ts', kind: 'file', size: 10, mtimeMs: 1000, isLink: false },
      { name: 'b.ts', kind: 'file', size: 10, mtimeMs: 1000, isLink: false },
    ],
  }))
  let latestViewState: Record<string, string> = {}
  on('state.set', (_$, e, next) => {
    if (e.key === 'viewState' && e.value) {
      latestViewState = e.value as Record<string, string>
    }
    return next(e)
  })
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: { ...paneProps(46), isFocused: true } })
  await ui.press({ key: 'dash-tab-files' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  await ui.redraw({ ...paneProps(46), isFocused: true })

  await ui.press({ key: 'files-s' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  expect(latestViewState['settings:focus'] ?? '').toBe('')

  shouldReject = true
  await ui.press({ key: 'files-s' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  expect(latestViewState['settings:focus'] ?? '').toBe('')

  await ui.press({ key: 'files-key-j' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  expect(latestViewState['files:sel']).toBeDefined()
})

/* ---------------------------------------------------------------------------
 * Files F4.3: the Files tab rides the rain tick (RAIN_MS = 1500). Time moves only
 * with mock.clock `advance`; fs.list is answered in the test; nothing sleeps.
 * ------------------------------------------------------------------------- */
const f43Entry = (name: string, mtimeMs = 1000, kind: 'file' | 'dir' = 'file'): FsEntry => ({ name, kind, size: 10, mtimeMs, isLink: false })
const f43Lane = (path: string) => ({ value: { exitCode: 0, stdout: JSON.stringify({ ok: true, slots: [{ slot: 1, path, task: 'P4' }] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })

/** every state write recorded; `startOnFiles` opens the pane straight onto Files (saved dashTab) */
function f43Rig(on: On, opts: {
  listing: (path: string) => FsEntry[] | Promise<FsEntry[]>
  activity?: unknown[]
  startOnFiles?: boolean
  /** the first files:v write is refused with this reason (the engine's own wording for a write made during a render) */
  refuseFilesV?: string
  /** the project's .git is a worktree FILE pointing at /main */
  gitFile?: boolean
  process?: Parameters<typeof mockWorld>[1] extends infer O ? O extends { process?: infer P } ? P : never : never
}) {
  const sets: { key: string; value: unknown }[] = []
  let viewState: Record<string, string> = {}
  const files = worldFiles()
  if (opts.gitFile) {
    files[`${PROJECT_DIR}/.git`] = 'gitdir: /main/.git/worktrees/x\n'
    files['/main/.git/worktrees/x/commondir'] = '../..\n'
  }
  const { reads } = mockFs(on, files, opts.gitFile ? [DATA_DIR] : undefined, opts.gitFile ? [`${PROJECT_DIR}/.git`] : undefined)
  const clock = mockWorld(on, { process: opts.process })
  let refused = 0
  on('state.get', (_$, e, next) => {
    if (e.key === 'dashTab' && opts.startOnFiles) return { value: { value: 'files', version: 1 } }
    if (e.key === 'fileActivity' && opts.activity) return { value: { value: opts.activity, version: 1 } }
    return next(e)
  })
  on('state.set', (_$, e, next) => {
    if (typeof opts.refuseFilesV === 'string' && refused === 0 && e.key === 'viewState' && (e.value as Record<string, string> | undefined)?.['files:v'] !== undefined) {
      refused++
      return { deny: opts.refuseFilesV }
    }
    sets.push({ key: e.key, value: e.value })
    if (e.key === 'viewState' && e.value) viewState = e.value as Record<string, string>
    return next(e)
  })
  on('fs.list', async (_$, e) => ({ value: await opts.listing(e.path) }))
  const settle = async (): Promise<void> => { for (let i = 0; i < 10; i++) await clock.advance(0) }
  const writes = (): number => sets.filter(s => s.key === 'fileActivity').length
  return { clock, sets, settle, writes, reads, refusals: () => refused, vs: () => viewState }
}

/** the text of every Text on the pane, joined by spaces */
async function f43Screen(ui: { findAll: (q: { type: 'Text' }) => Promise<readonly { text: string }[]> }): Promise<string> {
  return (await ui.findAll({ type: 'Text' })).map(t => t.text).join(' ')
}

const F43_OPTS = { timeoutMs: 15000, plugins: [worldFixtures] }
const F43_MOUNT = { plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder' } as const

test('Files F4.3: a2 rain:tick follows the mock clock (filesNow), not Date.now, with Files the only tab drawn', F43_OPTS, async ($, on) => {
  const rig = f43Rig(on, {
    startOnFiles: true,
    // P4.4: the pulse only redraws rows that carry an icon, so this fixture carries one and the
    // second advance stays inside its 3 s activity TTL (NOW + 2900)
    activity: [{ path: `${PROJECT_DIR}/a.ts`, kind: 'write', at: NOW }],
    listing: () => [f43Entry('a.ts')],
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46, 'inline') })
  await rig.settle()
  await rig.clock.advance(1500)
  await rig.settle()
  expect(rig.vs()['rain:tick']).toBe(String(Math.floor((NOW + 1500) / 1500)))
  await rig.clock.advance(1400)
  await rig.settle()
  await ui.redraw(paneProps(46, 'inline'))
  expect(rig.vs()['rain:tick']).toBe(String(Math.floor((NOW + 2900) / 1500)))
})

test('Files F4.3: a3 the tick walks and writes nothing when the last render was not Files, (the over-10 s half is not reachable here: a mounted pane redraws on every tick bump)', F43_OPTS, async ($, on) => {
  let rootCalls = 0
  const rig = f43Rig(on, { listing: p => { if (p === PROJECT_DIR) rootCalls++; return p === PROJECT_DIR ? [f43Entry('a.ts')] : [] } })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await ui.press({ key: 'dash-tab-files' })
  await rig.settle()
  await ui.redraw(paneProps(46))
  const walkedBefore = rootCalls
  await rig.clock.advance(1500)
  await rig.settle()
  expect(rootCalls).toBeGreaterThan(walkedBefore) // Files drawn: the tick walks
  // the last render was Overview
  await ui.press({ key: 'dash-tab-overview' })
  await rig.settle()
  await ui.redraw(paneProps(46))
  const before = { calls: rootCalls, writes: rig.writes(), v: rig.vs()['files:v'] }
  await rig.clock.advance(4500)
  await rig.settle()
  expect({ calls: rootCalls, writes: rig.writes(), v: rig.vs()['files:v'] }).toEqual(before)
})

/** the amber write dots on screen: one per lit row, plus the legend's */
const dots = (screen: string): number => (screen.match(/●/g) ?? []).length

test('Files F4.3: a4 an entry recorded by a tool call is gone from the tab after advance(4500)', F43_OPTS, async ($, on) => {
  const rig = f43Rig(on, { startOnFiles: true, activity: [{ path: `${PROJECT_DIR}/a.ts`, kind: 'write', at: NOW }], listing: () => [f43Entry('a.ts')] })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46, 'inline') })
  await rig.settle()
  await ui.redraw(paneProps(46, 'inline'))
  expect(dots(await f43Screen(ui))).toBe(2) // the lit row and the legend
  await rig.clock.advance(4500)
  await rig.settle()
  expect(dots(await f43Screen(ui))).toBe(1) // the legend only; // no redraw by the test: the tick's rain:tick bump redraws
})

test('Files F4.3: a5 a file created or changed in a listed folder shows as a write on the next tick, also inside a folded folder, and fades', F43_OPTS, async ($, on) => {
  let rootMtime = 1000
  let subMtime = 1000
  let extra = false
  const rig = f43Rig(on, {
    startOnFiles: true,
    listing: p => {
      if (p === PROJECT_DIR) return [f43Entry('a.ts', rootMtime), f43Entry('sub', 0, 'dir'), ...(extra ? [f43Entry('c.ts', NOW)] : [])]
      if (p === `${PROJECT_DIR}/sub`) return [f43Entry('b.ts', subMtime)]
      return []
    },
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await rig.settle()
  await ui.redraw(paneProps(46))
  expect(rig.writes()).toBe(0)
  extra = true
  subMtime = 5000
  await rig.clock.advance(1500)
  await rig.settle()
  await ui.redraw(paneProps(46))
  const last = rig.sets.filter(s => s.key === 'fileActivity').pop()?.value as { path: string; kind: string }[]
  expect(last.map(a => a.path).sort()).toEqual([`${PROJECT_DIR}/c.ts`, `${PROJECT_DIR}/sub/b.ts`])
  expect(last.every(a => a.kind === 'write')).toBe(true)
  expect(dots(await f43Screen(ui))).toBeGreaterThanOrEqual(2)
  await rig.clock.advance(4500)
  await rig.settle()
  await ui.redraw(paneProps(46))
  expect(dots(await f43Screen(ui))).toBe(1)
})

test('Files F4.3: a6 the first walk of a root, and the first walk after switching roots, records nothing', F43_OPTS, async ($, on) => {
  let mainExtra = false
  const rig = f43Rig(on, {
    process: argv => (argv.some(a => a.includes('mk-lane.py')) && argv.includes('list') ? f43Lane('/tmp/lane-1') : undefined),
    listing: p => {
      if (p === PROJECT_DIR) return [f43Entry('a.ts'), ...(mainExtra ? [f43Entry('a2.ts', NOW)] : [])]
      if (p === '/tmp/lane-1') return [f43Entry('l.ts', NOW)]
      return []
    },
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await ui.press({ key: 'dash-tab-files' })
  for (let i = 0; i < 60 && !(await ui.find({ type: 'Button', key: 'files-root:slot-1' })); i++) {
    await rig.clock.advance(0)
    await ui.redraw(paneProps(46))
  }
  expect(rig.writes()).toBe(0)
  await ui.press({ key: 'files-root:slot-1' })
  await rig.settle()
  await rig.clock.advance(1500)
  await rig.settle()
  await ui.redraw(paneProps(46))
  expect(await ui.find({ text: /l\.ts/ })).toBeTruthy()
  expect(rig.writes()).toBe(0)
  mainExtra = true // main changes while the lane is shown
  await ui.press({ key: 'files-root:main' })
  await rig.settle()
  await rig.clock.advance(6000) // P4.2: one walk per 5 s window — the next window after the walk above
  await rig.settle()
  await ui.redraw(paneProps(46))
  expect(await ui.find({ text: /a2\.ts/ })).toBeTruthy()
  expect(rig.writes()).toBe(0)
})

test('Files F4.3: a7 the pulse flag is Math.floor(filesNow / RAIN_MS) % 2 === 0 and flips after advance(1500)', F43_OPTS, async ($, on) => {
  const rig = f43Rig(on, { startOnFiles: true, activity: [{ path: `${PROJECT_DIR}/a.ts`, kind: 'write', at: NOW }], listing: () => [f43Entry('a.ts')] })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await rig.settle()
  await ui.redraw(paneProps(46))
  // the write icon on the row is dim when the pulse is off
  const dimOf = async (): Promise<unknown> => (await ui.findAll({ type: 'Text' })).find(t => typeof t.props.dimColor === 'boolean' && t.text.trim() === '●')?.props.dimColor
  expect(await dimOf()).toBe(!(Math.floor(NOW / 1500) % 2 === 0))
  await rig.clock.advance(1500)
  await rig.settle()
  await ui.redraw(paneProps(46))
  expect(await dimOf()).toBe(!(Math.floor((NOW + 1500) / 1500) % 2 === 0))
})

test('Files F4.3: a8 walk budget: a walk over 1000 ms of clock time makes the next tick start no walk, the tick after walks again, and no walk starts while one runs', F43_OPTS, async ($, on) => {
  let rootCalls = 0
  let walkMs = 0
  let rig!: ReturnType<typeof f43Rig>
  rig = f43Rig(on, {
    startOnFiles: true,
    listing: async p => {
      if (p === PROJECT_DIR) rootCalls++
      if (walkMs > 0) await rig.clock.sleep(walkMs) // fs.list runs for walkMs of clock time
      return p === PROJECT_DIR ? [f43Entry('a.ts')] : []
    },
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await rig.settle()
  const base = rootCalls
  const step = async (ms: number): Promise<void> => {
    await rig.clock.advance(ms)
    await rig.settle()
    await ui.redraw(paneProps(46)) // keeps Files inside the 10 s hot window
  }
  // a walk that takes 1100 ms (over the 1000 ms budget): beat 1500 starts it, and the 5 s window
  // holds the beats at 3000, 4500 and 6000 back, so the next walk is at 7500
  walkMs = 1100
  await step(1500)
  expect(rootCalls).toBe(base + 1)
  walkMs = 0
  await step(1500)
  expect(rootCalls).toBe(base + 1)
  await step(1500)
  expect(rootCalls).toBe(base + 1)
  await step(4500)
  expect(rootCalls).toBe(base + 2)
  // a walk that takes 3200 ms: it starts at beat 14000 and ends at 17200, so the beats at 15500 and
  // 17000 find it running; beat 18500 is still inside the window, beat 20000 walks
  walkMs = 3200
  await step(5000)
  expect(rootCalls).toBe(base + 3)
  walkMs = 0
  await step(1500)
  expect(rootCalls).toBe(base + 3)
  await step(1500)
  expect(rootCalls).toBe(base + 3)
  await step(1500)
  expect(rootCalls).toBe(base + 3)
  await step(1500)
  expect(rootCalls).toBe(base + 4)
})

test('Files F4.3: a9 a tick whose walk finds no change writes only rain:tick: no files:v bump and no fileActivity write', F43_OPTS, async ($, on) => {
  const rig = f43Rig(on, { startOnFiles: true, listing: p => (p === PROJECT_DIR ? [f43Entry('a.ts')] : []) })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46, 'inline') })
  await rig.settle()
  await ui.redraw(paneProps(46, 'inline'))
  const before = rig.vs()
  const mark = rig.sets.length
  await rig.clock.advance(1500)
  await rig.settle()
  const after = rig.sets.slice(mark)
  expect(after.every(s => s.key === 'viewState')).toBe(true)
  const changed = Object.keys(rig.vs()).filter(k => rig.vs()[k] !== before[k])
  expect(changed).toEqual([])
})

test('Files F4.3: a10 on a fixture shaped like this repo one tick lists at most 60 folders, none inside .omc, .matrixkoder or .matrixkoder-old-1, is not capped, and a newer .matrixkoder-status.json records no write', F43_OPTS, async ($, on) => {
  const listed: string[] = []
  let statusMtime = 1000
  const others = Array.from({ length: 40 }, (_, i) => `d${i}`)
  const rig = f43Rig(on, {
    startOnFiles: true,
    listing: p => {
      listed.push(p)
      if (p === PROJECT_DIR) {
        return [
          f43Entry('.matrixkoder', 0, 'dir'), f43Entry('.omc', 0, 'dir'), f43Entry('.matrixkoder-old-1', 0, 'dir'),
          f43Entry('.matrixkoder-status.json', statusMtime), f43Entry('.matrixkoder-bus-cursor'),
          ...others.map(n => f43Entry(n, 0, 'dir')),
        ]
      }
      return Array.from({ length: 6 }, (_, i) => f43Entry(`f${i}.ts`))
    },
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await rig.settle()
  await ui.redraw(paneProps(46))
  listed.length = 0
  statusMtime = NOW
  await rig.clock.advance(1500)
  await rig.settle()
  await ui.redraw(paneProps(46))
  expect(listed.length).toBeGreaterThan(0)
  expect(listed.length).toBeLessThanOrEqual(60)
  expect(listed.filter(p => /\/(\.omc|\.matrixkoder|\.matrixkoder-old-1)(\/|$)/.test(p))).toEqual([])
  expect(await ui.find({ text: /tree stopped at its limit/ })).toBeUndefined()
  expect(rig.writes()).toBe(0)
})

/* ---------------------------------------------------------------------------
 * Files F4 review r1 fixes (3 MEDIUM, 1 LOW)
 * ------------------------------------------------------------------------- */
test('Files F4.3: review fix 1, with a missing lane root chosen the tick walks the main checkout on screen and a new file there shows as a write', F43_OPTS, async ($, on) => {
  let newFile = false
  const rig = f43Rig(on, {
    process: argv => (argv.some(a => a.includes('mk-lane.py')) && argv.includes('list') ? f43Lane('/missing/lane-1') : undefined),
    listing: p => {
      if (p.startsWith('/missing')) throw new Error('ENOENT')
      return p === PROJECT_DIR ? [f43Entry('a.ts'), ...(newFile ? [f43Entry('new.ts', NOW)] : [])] : []
    },
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await ui.press({ key: 'dash-tab-files' })
  for (let i = 0; i < 60 && !(await ui.find({ type: 'Button', key: 'files-root:slot-1' })); i++) {
    await rig.clock.advance(0)
    await ui.redraw(paneProps(46))
  }
  await ui.press({ key: 'files-root:slot-1' })
  for (let i = 0; i < 60 && !(await ui.find({ text: /missing root, fallback to repo/ })); i++) {
    await rig.clock.advance(0)
    await ui.redraw(paneProps(46))
  }
  expect(await ui.find({ text: /missing root, fallback to repo/ })).toBeTruthy()
  await rig.clock.advance(1500) // the first walk after the root change records nothing
  await rig.settle()
  expect(rig.writes()).toBe(0)
  newFile = true
  await rig.clock.advance(6000) // P4.2: the next 5 s walk window after the walk above
  await rig.settle()
  const last = rig.sets.filter(s => s.key === 'fileActivity').pop()?.value as { path: string; kind: string }[] | undefined
  expect(last?.map(a => a.path)).toEqual([`${PROJECT_DIR}/new.ts`])
})

test('Files F4.2: review fix 2, a files:v write refused during a render is retried by the next tick, not dropped', F43_OPTS, async ($, on) => {
  const rig = f43Rig(on, { startOnFiles: true, refuseFilesV: 'it was made while ui.render is being dispatched, and drawing is pure', listing: p => (p === PROJECT_DIR ? [f43Entry('a.ts')] : []) })
  await $.session.start(sessionStart())
  await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await rig.settle()
  expect(rig.refusals()).toBe(1)
  expect(rig.vs()['files:v']).toBeUndefined()
  await rig.clock.advance(1500)
  await rig.settle()
  expect(rig.vs()['files:v']).toBe('1')
})

test('Files F4.2: review fix 2, pressing f from another tab starts the walk of the root on screen', F43_OPTS, async ($, on) => {
  let rootCalls = 0
  const rig = f43Rig(on, { listing: p => { if (p === PROJECT_DIR) rootCalls++; return p === PROJECT_DIR ? [f43Entry('a.ts')] : [] } })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await rig.settle()
  expect(rootCalls).toBe(0)
  await ui.press({ key: 'dash-tab-files' })
  await rig.clock.advance(0)
  expect(rootCalls).toBeGreaterThan(0)
  expect(rig.vs()['files:v']).toBe('1')
})

test('Files F4.2: review fix 3, an Overview render never asks $.session.cwd', F43_OPTS, async ($, on) => {
  let cwdCalls = 0
  const rig = f43Rig(on, { listing: () => [] })
  on('session.cwd', (_$, e, next) => { cwdCalls++; return next(e) })
  await $.session.start(sessionStartIn(''))
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await rig.settle()
  await ui.redraw(paneProps(46))
  await ui.redraw(paneProps(46))
  expect(cwdCalls).toBe(0)
})

test('Files F4.2: review fix 3, on Files several renders resolve the main checkout once', F43_OPTS, async ($, on) => {
  let mainListed = 0
  const rig = f43Rig(on, { startOnFiles: true, gitFile: true, listing: p => { if (p === '/main') mainListed++; return p === '/main' ? [f43Entry('a.ts')] : [] } })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await rig.settle()
  // other code (the data-dir lookup) reads commondir too: count only what the renders add
  const commondirReads = (): number => rig.reads.filter(r => r === '/main/.git/worktrees/x/commondir').length
  const before = commondirReads()
  for (let i = 0; i < 3; i++) {
    await rig.clock.advance(1500)
    await ui.redraw(paneProps(46))
  }
  expect(commondirReads() - before).toBe(0)
  expect(mainListed).toBeGreaterThan(0) // the Files renders ran and walked the resolved main checkout
})

// literals only (the module scan drops closures): the clock is refused once the test writes this file
const clockBreaker: Plugin = {
  name: 'mk-clock-breaker',
  tier: 'prepend',
  register(on, _options) {
    return on('clock.now', async ($, e, next) => {
      let broken = false
      try { broken = (await $.fs.read('/mk-clock-broken')) === '1' } catch { broken = false }
      if (broken) return { deny: 'clock gone' }
      return next(e)
    })
  },
}

test('Files F4.3: review fix 4, a rejecting $.clock.now still bumps rain:tick for the code rain on a non-Files tab', { timeoutMs: 15000, plugins: [worldFixtures, clockBreaker] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  const clock = mockWorld(on)
  let viewState: Record<string, string> = {}
  on('state.set', (_$, e, next) => {
    if (e.key === 'viewState' && e.value) viewState = e.value as Record<string, string>
    return next(e)
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  await ui.redraw(paneProps(46)) // Overview draws the rain
  files['/mk-clock-broken'] = '1'
  await clock.advance(1500)
  for (let i = 0; i < 10; i++) await clock.advance(0)
  expect(Number(viewState['rain:tick'])).toBeGreaterThanOrEqual(Math.floor(Date.now() / 1500) - 1)
})

/* ---------------------------------------------------------------------------
 * Files F4 re-review fixes (1 MEDIUM, 2 LOW)
 * ------------------------------------------------------------------------- */
test('Files F4.2: review fix 5, a files:v write that fails for any other reason is retried by the next tick too', F43_OPTS, async ($, on) => {
  const rig = f43Rig(on, { startOnFiles: true, refuseFilesV: 'storage unavailable', listing: p => (p === PROJECT_DIR ? [f43Entry('a.ts')] : []) })
  await $.session.start(sessionStart())
  await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await rig.settle()
  expect(rig.refusals()).toBe(1)
  expect(rig.vs()['files:v']).toBeUndefined()
  await rig.clock.advance(1500)
  await rig.settle()
  expect(rig.vs()['files:v']).toBe('1')
})

test('Files F4.3: review fix 6, a missing lane root that comes back replaces the fallback tree and the note goes away after one tick', F43_OPTS, async ($, on) => {
  let laneBack = false
  const rig = f43Rig(on, {
    process: argv => (argv.some(a => a.includes('mk-lane.py')) && argv.includes('list') ? f43Lane('/lane/one') : undefined),
    listing: p => {
      if (p === '/lane/one') {
        if (!laneBack) throw new Error('ENOENT')
        return [f43Entry('lane-file.ts')]
      }
      return p === PROJECT_DIR ? [f43Entry('main-file.ts')] : []
    },
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await ui.press({ key: 'dash-tab-files' })
  for (let i = 0; i < 60 && !(await ui.find({ type: 'Button', key: 'files-root:slot-1' })); i++) {
    await rig.clock.advance(0)
    await ui.redraw(paneProps(46))
  }
  await ui.press({ key: 'files-root:slot-1' })
  for (let i = 0; i < 60 && !(await ui.find({ text: /missing root, fallback to repo/ })); i++) {
    await rig.clock.advance(0)
    await ui.redraw(paneProps(46))
  }
  expect(await ui.find({ text: /main-file\.ts/ })).toBeTruthy()
  laneBack = true
  await rig.clock.advance(1500)
  await rig.settle()
  await ui.redraw(paneProps(46))
  expect(await ui.find({ text: /lane-file\.ts/ })).toBeTruthy()
  expect(await ui.find({ text: /missing root/ })).toBeUndefined()
})


/* ---------------------------------------------------------------------------
 * P1 — dedupe and parallelize the tick and the start (dashboard-perf-r1)
 *
 * ONE counting fake runner for the whole tick: every `python3 <script>` argv the
 * pane spawns is recorded and answered with the fixture shapes the tests above
 * already use (`mkdb.py projects|overview|todo|run-snapshot`, `mk-bus.py pending`,
 * `mk-config.py get`). Only the process runner and the clock are faked — refreshSafe,
 * loadSnapshot and every refresher under test are the production ones.
 * ------------------------------------------------------------------------- */

/** One `python3 …` spawn of the pane, as the counting runner saw it. */
type Spawn = { script: string; argv: string[] }

const P1_HOME = '/tmp/mk-p1-home'
const P1_SLUG = 'matrixkoder'
const P1_C2 = `${P1_HOME}/projects/${P1_SLUG}`
/** the project yaml in the wizard's emitter shape: agents + usage providers */
const P1_PROJECT_YAML = [
  'agents:',
  '  claude: { available: true }',
  'usage:',
  '  source: codexbar',
  '  poll_min: 5',
  '  providers: [claude]',
].join('\n')

const okReply = (body: unknown) => ({
  value: { exitCode: 0, stdout: JSON.stringify(body), stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
})

/** A promise plus its resolver: this tsconfig's lib has no `Promise.withResolvers`, so the
 *  executor form is the only one available for a deferred the test releases later. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(res => { resolve = res })
  return { promise, resolve }
}

/** Every `mkdb.py projects --json` spawn in the recorded list. */
const projectReads = (spawns: readonly Spawn[]): Spawn[] =>
  spawns.filter(s => s.script === 'mkdb.py' && s.argv.includes('projects'))

/** Settle every fire-and-forget chain a tick left, without moving time. */
async function settle(clock: MockWorld, steps = 40): Promise<void> {
  for (let i = 0; i < steps; i++) await clock.advance(0)
}

/**
 * The worst-case P1 world and its counting runner. `state.projects` and `state.run`
 * are live: a test rewrites them between two ticks to prove the cache is dropped.
 * Every reader is answered, so nothing short-circuits before its own spawn:
 *   - `mkdb.py projects --json`  → one row naming this project (C2 db_path)
 *   - `mkdb.py overview --json`  → that row + one plan (Cards and the Plan list)
 *   - `mkdb.py run-snapshot --json` → the run fixture (the pane's own read)
 *   - `mkdb.py todo list`        → one open item (only while the Todo tab is open)
 *   - `mk-bus.py pending`        → a live empty ask list
 *   - `mk-config.py get`         → true (bus enabled)
 */
function p1Runner(spawns: Spawn[]) {
  const state = {
    projects: [
      { slug: P1_SLUG, name: P1_SLUG, repo: PROJECT_DIR, data_dir: DATA_DIR, mode: 'home', db_path: `${P1_C2}/mk.db` },
    ] as Record<string, unknown>[],
    run: null as unknown,
  }
  const process = (argv: readonly string[]) => {
    const script = (argv[1] ?? '').split('/').pop() ?? ''
    spawns.push({ script, argv: [...argv] })
    switch (script) {
      case 'mkdb.py':
        if (argv.includes('projects')) return okReply({ ok: true, projects: state.projects })
        if (argv.includes('overview')) {
          return okReply({
            ok: true,
            projects: state.projects.map(r => ({ slug: r.slug, repo: r.repo, data_dir: r.data_dir, db_path: r.db_path })),
            plans: [{ project: P1_SLUG, plan: 'alpha', title: 'Alpha', status: 'running', created: 'c', live_run: null, counts: { building: 1 } }],
          })
        }
        if (argv.includes('todo')) return okReply({ ok: true, todos: [{ id: 1, text: 'one', status: 'open', created: 'c' }] })
        return okReply({ ok: true, runs: [], active: 'wfv2', waits: [], store: true, run: state.run })
      case 'mk-bus.py':
        return okReply({ ok: true, run: 'wfv2', pending: [] })
      case 'mk-config.py':
        return okReply({ ok: true, value: true })
      default:
        return undefined
    }
  }
  return { process, state }
}

/** The world one worst-case tick reads: fixtures, both stores on disk, every tab's data due. */
function p1World(on: On, spawns: Spawn[], opts: { storeFails?: boolean } = {}) {
  const files = worldFiles()
  files[`${P1_C2}/project.yaml`] = P1_PROJECT_YAML
  const fs = mockFs(on, files, [DATA_DIR, `${PROJECT_DIR}/.git`, P1_C2])
  const world = p1Runner(spawns)
  world.state.run = JSON.parse(files[RUN_PATH] ?? '{}') as unknown
  const runner = world.process
  on('fs.exists', (_$, e) => ({ value: [`${P1_HOME}/mk.db`, `${P1_C2}/mk.db`, `${DATA_DIR}/mk.db`].includes(String(e.path)) }))
  const clock = mockWorld(on, {
    env: { HOME: '/home/mk', MK_HOME: P1_HOME },
    process: argv => {
      // the refused-resolve case: every root-store read fails at the spawn
      if (opts.storeFails === true && argv.some(a => a.endsWith('mkdb.py')) && argv.includes('projects')) {
        spawns.push({ script: 'mkdb.py', argv: [...argv] })
        throw new Error('spawn python3 ENOENT')
      }
      return runner(argv)
    },
  })
  return { clock, world, files, fs }
}

/** The production refreshSafe, driven through a real session.start and a real tick. */
async function p1Start($: Engine, on: On, opts: { storeFails?: boolean } = {}) {
  const spawns: Spawn[] = []
  const { clock, world, files, fs } = p1World(on, spawns, opts)
  await $.session.start(sessionStart())
  await settle(clock)
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  return { spawns, clock, world, files, fs, ui }
}

// ── P1.1: one project/dataDir read per tick ──────────────────────────────────

test('P1.1.a1: a worst-case tick runs `mkdb.py projects --json` at most once', { plugins: [worldFixtures] }, async ($, on) => {
  // FAILS ON THE OLD CODE: loadSnapshot, refreshPending, refreshPlans, refreshTodos and
  // yamlPathFor each resolved the data dir on their own, so one due tick started the
  // command once per caller (about 5).
  const { spawns, clock } = await p1Start($, on)
  expect(projectReads(spawns).length, 'the start load reads the projects table once').toBeLessThanOrEqual(1)
  // ONE refresh tick: every refresher that resolves the data dir runs off the same answer
  spawns.length = 0
  await clock.advance(5000)
  await settle(clock)
  expect(projectReads(spawns).length, `reads: ${projectReads(spawns).map(r => r.argv.slice(2).join(' ')).join(' | ')}`).toBeLessThanOrEqual(1)
})

test('P1.1.a3: the tick answer is dropped, so a store change between two ticks is seen', { plugins: [worldFixtures] }, async ($, on) => {
  // FAILS ON THE OLD CODE (the second tick reads a memoised row forever if a cache is not dropped).
  const { spawns, clock, world, ui } = await p1Start($, on)
  // a SECOND project appears in the root store between two ticks
  world.state.projects = [
    ...world.state.projects,
    { slug: 'added', name: 'added', repo: '/srv/added', data_dir: '/srv/added/.matrixkoder', mode: 'home', db_path: null },
  ]
  spawns.length = 0
  await clock.advance(30_000)
  await settle(clock)
  // the Plan list renders from the root overview, which now names two plans' worth of rows
  await ui.press({ key: 'dash-tab-plan' })
  await settle(clock)
  const overview = spawns.filter(s => s.script === 'mkdb.py' && s.argv.includes('overview'))
  expect(overview.length, 'the next tick re-read the root store').toBeGreaterThan(0)
  expect(projectReads(spawns).length, 'and the projects table with it').toBeGreaterThan(0)
})

test('P1.1.a4: a refused projects resolve keeps today’s result in every refresher', { plugins: [worldFixtures] }, async ($, on) => {
  // A refused/empty resolve must not throw out of the tick and must keep today's answer
  // in each refresher: the pane still draws, the Plan list still renders from the root
  // overview, and the Kanban tab still counts its open task from the project store.
  const { spawns, clock, ui } = await p1Start($, on, { storeFails: true })
  expect(projectReads(spawns).length, 'the refused read was attempted').toBeGreaterThan(0)
  expect(await ui.find({ text: /workflows-v2|Alpha|No active orchestration/ }), 'the pane still drew').toBeTruthy()
  await ui.press({ key: 'dash-tab-plan' })
  await settle(clock)
  expect(await ui.find({ text: /Alpha plan|Alpha|no plans yet/ }), 'no throw out of the tick').toBeTruthy()
  await ui.press({ key: 'dash-tab-kanban' })
  await settle(clock)
  expect(await ui.find({ text: /Backlog|cards read failed|no store yet/ }), 'the Kanban tab still answers').toBeTruthy()
})

// ── P1.2: the two first reads run side by side ───────────────────────────────

test('P1.2.a1: both first reads are pending at the same moment', { plugins: [worldFixtures] }, async ($, on) => {
  // FAILS ON THE OLD CODE: loadSnapshot was awaited to completion before readRootOverview
  // was even started, so the overview spawn could not exist while the snapshot was held.
  // Both reads are held open here: on the old order only ONE of the two can ever be pending.
  const heldSnapshot: Array<() => void> = []
  const heldOverview: Array<() => void> = []
  const clock = mockWorld(on, {
    env: { HOME: '/home/mk', MK_HOME: P1_HOME },
    process: argv => {
      if (argv.some(a => a.endsWith('mkdb.py')) && argv.includes('run-snapshot')) {
        const { promise, resolve } = deferred<ReturnType<typeof okReply>>()
        heldSnapshot.push(() => resolve(okReply({ ok: true, runs: [], active: 'wfv2', waits: [], store: true, run: null })))
        return promise
      }
      if (argv.some(a => a.endsWith('mkdb.py')) && argv.includes('overview')) {
        const { promise, resolve } = deferred<ReturnType<typeof okReply>>()
        heldOverview.push(() => resolve(okReply({ ok: true, projects: [], plans: [] })))
        return promise
      }
      if (argv.includes('projects')) return okReply({ ok: true, projects: [] })
      return okReply({ ok: true, pending: [], value: true, todos: [] })
    },
  })
  on('fs.exists', (_$, e) => ({ value: [`${P1_HOME}/mk.db`, `${DATA_DIR}/mk.db`].includes(String(e.path)) }))
  mockFs(on, worldFiles(), [DATA_DIR, `${PROJECT_DIR}/.git`])
  // not awaited: the hook finishes with the held load, which is the whole point
  const started = $.session.start(sessionStart())
  for (let i = 0; i < 10; i++) await clock.advance(0)
  expect(heldSnapshot.length, 'the snapshot read is pending').toBe(1)
  expect(heldOverview.length, 'the overview read is pending at the same time').toBe(1)
  for (const go of [...heldSnapshot, ...heldOverview]) go()
  await started
  await settle(clock)
})

test('P1.2.a2: a failing first read does not lose the other', { plugins: [worldFixtures] }, async ($, on) => {
  // FAILS ON THE OLD CODE only in the sense that the old order made the overview
  // unreachable after a snapshot throw; here the overview's own result still lands.
  // The production refreshSafe runs: the snapshot spawn throws, the overview does not.
  const clock = mockWorld(on, {
    env: { HOME: '/home/mk', MK_HOME: P1_HOME },
    process: argv => {
      if (argv.some(a => a.endsWith('mkdb.py')) && argv.includes('run-snapshot')) throw new Error('spawn python3 ENOENT')
      if (argv.some(a => a.endsWith('mkdb.py')) && argv.includes('overview')) {
        return okReply({
          ok: true,
          projects: [],
          plans: [{ project: P1_SLUG, plan: 'beta', title: 'Beta plan', status: 'running', created: 'c', live_run: null, counts: {} }],
        })
      }
      if (argv.includes('projects')) return okReply({ ok: true, projects: [] })
      return okReply({ ok: true, pending: [], value: true, todos: [] })
    },
  })
  on('fs.exists', (_$, e) => ({ value: [`${P1_HOME}/mk.db`, `${DATA_DIR}/mk.db`].includes(String(e.path)) }))
  mockFs(on, worldFiles(), [DATA_DIR, `${PROJECT_DIR}/.git`])
  await $.session.start(sessionStart())
  await settle(clock)
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  // The failed snapshot keeps today's answer on the Overview tab (readStoreSnapshot swallows
  // the refusal into "null store"), while the overview read still lands on the Plan list.
  expect(await ui.find({ text: 'No active orchestration run.' }), 'the failed snapshot is today’s NoRun panel').toBeTruthy()
  await ui.press({ key: 'dash-tab-plan' })
  await settle(clock)
  expect(await ui.find({ text: /Beta plan|beta/ }), 'the overview result still reached the Plan list').toBeTruthy()
})

// ── P1.3: the session hands over before the first load finishes ──────────────

/** A process answer that holds the pane's own store read open until `release()`. */
function p1HeldRun(held: Array<() => void>, spawns: Spawn[]) {
  return (argv: readonly string[]) => {
    const script = (argv[1] ?? '').split('/').pop() ?? ''
    spawns.push({ script, argv: [...argv] })
    if (script === 'mkdb.py' && argv.includes('run-snapshot') && !argv.includes('--home')) {
      const { promise, resolve } = deferred<ReturnType<typeof okReply>>()
      held.push(() => resolve(okReply({ ok: true, runs: [], active: 'wfv2', waits: [], store: true, run: null })))
      return promise
    }
    if (script === 'mkdb.py' && argv.includes('projects')) {
      return okReply({ ok: true, projects: [{ slug: P1_SLUG, repo: PROJECT_DIR, data_dir: DATA_DIR, db_path: null }] })
    }
    if (script === 'mkdb.py' && argv.includes('overview')) return okReply({ ok: true, projects: [], plans: [] })
    if (script === 'mkdb.py') return okReply({ ok: true, todos: [] })
    return okReply({ ok: true, pending: [], value: true })
  }
}

test('P1.3.a1 (session.start): the handler’s promise resolves while the first load is held open', { plugins: [worldFixtures] }, async ($, on) => {
  // FAILS ON 9fa3f19, where the handler ended with `await firstLoad`, so the promise the engine
  // waits on stayed pending until the load finished. (It also fails on 270da7b, where
  // `await startLoop(...)` put the whole load in front of `return next(e)`.)
  const held: Array<() => void> = []
  const spawns: Spawn[] = []
  const clock = mockWorld(on, { env: { HOME: '/home/mk', MK_HOME: P1_HOME }, process: p1HeldRun(held, spawns) })
  on('fs.exists', (_$, e) => ({ value: [`${P1_HOME}/mk.db`, `${DATA_DIR}/mk.db`].includes(String(e.path)) }))
  mockFs(on, worldFiles(), [DATA_DIR, `${PROJECT_DIR}/.git`])
  // the promise the engine awaits: `$.session.start` resolves it when the hook returns
  let handlerSettled = false
  const started = $.session.start(sessionStart())
  void started.then(() => { handlerSettled = true })
  for (let i = 0; i < 12 && held.length === 0; i++) await clock.advance(0)
  expect(held.length, 'the first load is held').toBe(1)
  await settle(clock, 4)
  expect(handlerSettled, 'the hook’s promise resolved with the load still pending').toBe(true)
  // the pane drew from stored atoms meanwhile, and the load runs ONCE
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ text: /MatrixKoder|Overview/ }), 'the pane drew while the load was still pending').toBeTruthy()
  expect(spawns.filter(s => s.script === 'mkdb.py' && s.argv.includes('run-snapshot') && !s.argv.includes('--home')).length, 'one first load').toBe(1)
  for (const go of held) go()
  await started
  await settle(clock)
})

test('P1.3.a1 (reopen path): the re-arm returns while the first load is still held open', { plugins: [worldFixtures] }, async ($, on) => {
  // The second startLoop caller: rearmLoop, reached by /mk, turn.start, a render and a focus.
  // FAILS ON THE OLD CODE, where rearmLoop awaited startLoop -> refreshSafe before returning,
  // so /mk could not return while that load was outstanding.
  const held: Array<() => void> = []
  const spawns: Spawn[] = []
  const clock = mockWorld(on, { env: { HOME: '/home/mk', MK_HOME: P1_HOME }, process: p1HeldRun(held, spawns) })
  on('fs.exists', (_$, e) => ({ value: [`${P1_HOME}/mk.db`, `${DATA_DIR}/mk.db`].includes(String(e.path)) }))
  on('session.cwd', () => ({ value: PROJECT_DIR }))
  on('session.end', () => ({ sessionId: 's1' }))
  mockFs(on, worldFiles(), [DATA_DIR, `${PROJECT_DIR}/.git`])
  const started = $.session.start(sessionStart())
  for (let i = 0; i < 12 && held.length === 0; i++) await clock.advance(0)
  expect(held.length, 'the start load is pending').toBe(1)
  for (const go of held.splice(0)) go()
  await started
  await settle(clock)
  await $.session.end({ reason: 'clear', sessionId: 's1', resume: { id: undefined } } as never)
  spawns.length = 0
  // /mk re-arms: its handler must return while the first load it started is outstanding.
  // The command is NOT awaited yet — the assertion below is exactly that it settled anyway.
  let returned = false
  const cmd = $.command.run({ command: 'mk', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 165 } })
  void cmd.then(() => { returned = true })
  for (let i = 0; i < 12 && held.length === 0; i++) await clock.advance(0)
  expect(held.length, 'the re-armed load is pending').toBe(1)
  expect(returned, '/mk returned while that load was still pending').toBe(true)
  expect(spawns.filter(s => s.script === 'mkdb.py' && s.argv.includes('run-snapshot') && !s.argv.includes('--home')).length, 'exactly one re-armed load').toBe(1)
  for (const go of held) go()
  await cmd
  await settle(clock)
})

test('P1.3.a1 (brand + default tab): both arrive after next(), with the load still held', { plugins: [worldFixtures], options: { defaultTab: 'settings' } }, async ($, on) => {
  // FAILS ON THE OLD CODE: `loadBrandVersion` and the default-tab write both sat behind
  // `await startLoop`, which awaited the first load.
  const held: Array<() => void> = []
  const spawns: Spawn[] = []
  const clock = mockWorld(on, { env: { HOME: '/home/mk', MK_HOME: P1_HOME }, process: p1HeldRun(held, spawns) })
  on('fs.exists', (_$, e) => ({ value: [`${P1_HOME}/mk.db`, `${DATA_DIR}/mk.db`].includes(String(e.path)) }))
  mockFs(on, worldFiles(), [DATA_DIR, `${PROJECT_DIR}/.git`])
  const started = $.session.start(sessionStart())
  await settle(clock, 12)
  expect(held.length, 'the first load is held').toBe(1)
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  // defaultTab settings: the Settings tab is the open one, so its word is in the bar
  expect(await ui.find({ text: /Settings/ }), 'the default tab landed before the load finished').toBeTruthy()
  for (const go of held) go()
  await started
  await settle(clock)
  await ui.redraw(paneProps(46))
  expect(await ui.find({ text: 'MATRIXKODER' }), 'the brand header still drew').toBeTruthy()
})

// ── P1.4: the spawn bound ────────────────────────────────────────────────────

test('P1.4.a1: one worst-case tick starts at most 6 Python processes', { plugins: [worldFixtures] }, async ($, on) => {
  // FAILS ON THE OLD CODE: measured at 270da7b, one such tick started 8 processes, 4 of them
  // the same `mkdb.py projects --json` (see the report's before/after numbers).
  const { spawns, clock } = await p1Start($, on)
  // the worst case: a refresh tick with every refresher due (Cards and Pending every tick)
  spawns.length = 0
  await clock.advance(5000)
  await settle(clock)
  const tick = spawns.filter(s => s.script === 'mkdb.py' || s.script === 'mk-bus.py' || s.script === 'mk-usage.py')
  expect(projectReads(spawns).length, 'one projects read').toBeLessThanOrEqual(1)
  expect(tick.length, `one tick's Python starts: ${tick.map(t => `${t.script} ${t.argv.slice(2).join(' ')}`).join(' | ')}`).toBeLessThanOrEqual(6)
})

// ── R3 FIX 3: a cold start whose first load failed still polls usage once ────

test('P1.3.a4 (R3): a first load that rejects before its usage check still polls once, at 5 s', { plugins: [worldFixtures] }, async ($, on) => {
  // FAILS ON 9fa3f19: there `const prev = await read($, snapshotAtom)` sat OUTSIDE the try, so a
  // refused snapshot read rejected the whole first load — and `startLoop`'s 5 s one-shot only
  // polls on a RESOLVED first load. `firstLoadDone` stayed false, `usageMissingAtLoad` false,
  // and a cold start whose ledger is missing never polled: the pane kept "no ledger yet" for
  // the whole poll interval. With the read moved inside the try the tick completes, the
  // fallback (`usageMissingNow`) runs, and exactly one poll happens.
  const files = worldFiles()
  delete files['/fixtures/usage.json']
  delete files[USAGE_PATH]
  mockFs(on, files, [DATA_DIR, `${PROJECT_DIR}/.git`])
  // the engine refuses the snapshot atom read: the first load cannot get its previous value
  on('state.get', (_$, e, next) => (e.key === 'snapshot' ? { deny: 'state store unavailable' } : next(e)))
  const polls: (readonly string[])[] = []
  const clock = mockWorld(on, {
    env: { HOME: '/home/mk', MK_HOME: P1_HOME },
    process: argv => {
      if (argv.some(a => a.endsWith('scripts/mk-usage.py'))) { polls.push([...argv]); return undefined }
      if (argv.some(a => a.endsWith('mkdb.py')) && argv.includes('projects')) return okReply({ ok: true, projects: [] })
      if (argv.some(a => a.endsWith('mkdb.py')) && argv.includes('overview')) return okReply({ ok: true, projects: [], plans: [] })
      if (argv.some(a => a.endsWith('mkdb.py'))) return okReply({ ok: true, todos: [] })
      return okReply({ ok: true, pending: [], value: true })
    },
  })
  await $.session.start(sessionStart())
  await clock.advance(4999)
  expect(polls, 'nothing polls inside the 5 s window').toHaveLength(0)
  await clock.advance(1)
  await settle(clock, 20)
  expect(polls, 'the failed first load still leaves exactly one cold-start poll').toHaveLength(1)
})

/* ---------------------------------------------------------------------------
 * P4 — rain and Files render economy (dashboard-perf-r1)
 * ------------------------------------------------------------------------- */

test('P4.1.a1: a closed dock writes no rain:tick and redraws nothing, and a reopen restarts the beat', F43_OPTS, async ($, on) => {
  // FAILS ON THE OLD CODE: closePane reset neither filesShown nor lastRainDraw, so the beat kept
  // bumping rain:tick for up to 10 s after the dock closed and the pane re-rendered unseen.
  const rig = f43Rig(on, { startOnFiles: true, listing: p => (p === PROJECT_DIR ? [f43Entry('a.ts')] : []) })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await rig.settle()
  await ui.redraw(paneProps(46))
  await rig.clock.advance(1500)
  await rig.settle()
  expect(rig.vs()['rain:tick'], 'the open dock beats').toBeDefined()

  await $.command.run({ command: 'mk', args: 'close', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 165 } })
  await rig.settle()
  const mark = rig.sets.length
  const tickAtClose = rig.vs()['rain:tick']
  await rig.clock.advance(4500) // three beats, all inside the refresh tick's 5 s
  await rig.settle()
  const after = rig.sets.slice(mark)
  expect(after.filter(s => s.key === 'viewState' && (s.value as Record<string, string>)['rain:tick'] !== tickAtClose)).toEqual([])

  await $.command.run({ command: 'mk', args: 'open', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 165 } })
  await rig.settle()
  await ui.redraw(paneProps(46)) // the engine paints the reopened pane: codeRain stamps lastRainDraw
  const mark2 = rig.sets.length
  await rig.clock.advance(1500)
  await rig.settle()
  expect(rig.sets.slice(mark2).some(s => s.key === 'viewState' && (s.value as Record<string, string>)['rain:tick'] !== tickAtClose), 'the beat is back').toBe(true)
})

test('P4.2.a1: on an unchanged repo with Files shown, 30 s of beats walk at most once per 5 s and run git at most once per 5 s', F43_OPTS, async ($, on) => {
  // FAILS ON THE OLD CODE, which walked the tree and ran `git ls-files` + `git status` on every
  // 1.5 s beat (20 walks and 40 git runs in 30 s).
  let walks = 0
  const gits: string[][] = []
  const rig = f43Rig(on, {
    startOnFiles: true,
    process: argv => {
      if (argv[0] === 'git') gits.push([...argv])
      return undefined
    },
    listing: p => {
      if (p === PROJECT_DIR) walks++
      return p === PROJECT_DIR ? [f43Entry('a.ts'), f43Entry('sub', 0, 'dir')] : [f43Entry('b.ts')]
    },
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await rig.settle()
  await ui.redraw(paneProps(46))
  const baseWalks = walks
  const baseGits = gits.length
  for (let i = 0; i < 20; i++) { // 20 beats = 30 s
    await rig.clock.advance(1500)
    await rig.settle()
    await ui.redraw(paneProps(46))
  }
  const walked = walks - baseWalks
  const gitRuns = gits.length - baseGits
  expect(walked, 'at most one walk per 5 s window').toBeLessThanOrEqual(6)
  expect(gitRuns, 'at most one git run pair per 5 s window').toBeLessThanOrEqual(12)
  expect(walked, 'and the beat does keep walking').toBeGreaterThan(0)
  expect(gits.some(a => a.includes('ls-files')), 'the walk still asks git for the ignored dirs').toBe(true)
  expect(gits.some(a => a.includes('status')), 'the walk still asks git for the status').toBe(true)
})

test('P4.2.a2: a file edited in place (same listing, changed git status) shows its status within one 5 s window', F43_OPTS, async ($, on) => {
  // C2: skipping git whenever the LISTING is unchanged would miss this edit entirely.
  let status = ''
  const rig = f43Rig(on, {
    startOnFiles: true,
    process: argv => {
      if (argv[0] === 'git' && argv.includes('status')) {
        return { value: { exitCode: 0, stdout: status, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      return undefined
    },
    listing: () => [f43Entry('a.ts', 1000)],
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46) })
  await rig.settle()
  await ui.redraw(paneProps(46))
  expect(await ui.find({ text: /✓ clean/ }), 'the quiet repo reads clean').toBeTruthy()
  status = ' M a.ts\n' // the real `git status --porcelain` line for an edited, still-listed file
  await rig.clock.advance(4500) // the next beat inside the window after the boot walk
  await rig.settle()
  await ui.redraw(paneProps(46))
  expect(await ui.find({ text: /● 1 dirty/ }), 'the edit is seen within one window').toBeTruthy()
})

test('P4.3.a1/a2: the rain grid is built once per (cols, rows, tick) and equals the uncached build', () => {
  // FAILS ON THE OLD CODE, which had no cache at all: every render rebuilt the 46x40 grid.
  const tick = Math.floor(NOW / 1500)
  const a = rainGridFor(46, 40, tick)
  const b = rainGridFor(46, 40, tick)
  expect(b).toBe(a) // same object: built once for two renders in one tick and size
  expect(a).toEqual(buildRainGrid(46, 40, tick)) // identical to the uncached output
  const nextTick = rainGridFor(46, 40, tick + 1)
  expect(nextTick).not.toBe(a)
  const resized = rainGridFor(30, 40, tick + 1)
  expect(resized).not.toBe(nextTick)
  // single entry: the earlier ones are gone, the latest is held
  expect(rainGridFor(30, 40, tick + 1)).toBe(resized)
  expect(rainGridFor(46, 40, tick)).not.toBe(a)
})

test('P4.4.a2: an inline Files pane with no activity icons writes no rain:tick across several beats', F43_OPTS, async ($, on) => {
  // FAILS ON THE OLD CODE, which bumped rain:tick on every 1.5 s beat while Files was shown even
  // though the pulse changed no visible prop on any row.
  const rig = f43Rig(on, { startOnFiles: true, listing: () => [f43Entry('a.ts')] })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46, 'inline') })
  await rig.settle()
  await ui.redraw(paneProps(46, 'inline'))
  const mark = rig.sets.length
  for (let i = 0; i < 4; i++) {
    await rig.clock.advance(1500)
    await rig.settle()
  }
  const ticks = rig.sets.slice(mark)
    .filter(s => s.key === 'viewState')
    .map(s => (s.value as Record<string, string>)['rain:tick'])
    .filter(v => v !== undefined)
  expect(ticks, 'no rain:tick write at all').toEqual([])
})

test('P4.4.a2: an inline Files pane that draws an icon still pulses it, and the fade is drawn', F43_OPTS, async ($, on) => {
  const rig = f43Rig(on, { startOnFiles: true, activity: [{ path: `${PROJECT_DIR}/a.ts`, kind: 'write', at: NOW }], listing: () => [f43Entry('a.ts')] })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ ...F43_MOUNT, props: paneProps(46, 'inline') })
  await rig.settle()
  await ui.redraw(paneProps(46, 'inline'))
  expect(dots(await f43Screen(ui))).toBe(2) // the lit row and the legend
  const mark = rig.sets.length
  await rig.clock.advance(1500)
  await rig.settle()
  expect(rig.sets.slice(mark).some(s => s.key === 'viewState' && (s.value as Record<string, string>)['rain:tick'] != null), 'the icon pulses').toBe(true)
  await ui.redraw(paneProps(46, 'inline'))
  // the activity entry expires at 3 s: the beat keeps redrawing until a render shows the faded row
  await rig.clock.advance(3000)
  await rig.settle()
  expect(dots(await f43Screen(ui))).toBe(1) // the legend only
})
