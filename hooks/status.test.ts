// Chip + toast tests (design §5): the chip text after the first load, a toast
// exactly once per status change, none on the first load.
import { test, expect } from 'claude-code/testing'
import { NOW, RUNS_INDEX, RUN_PATH, agentOf, mockFs, mockWorld, sessionStart, setRun, setUsage, tasksOf, worldFiles, worldFixtures } from './testkit'

test('chip shows waiting-on-you over everything else', { plugins: [worldFixtures] }, async ($, on) => {
  const status: (string | undefined)[] = []
  mockFs(on, worldFiles())
  mockWorld(on, { status: text => status.push(text) })
  await $.session.start(sessionStart())
  expect(status.at(-1)).toBe('⬡ mk · ⚠ 1 waiting on you · 1 building')
})

test('chip falls back to the stalled template without a waiting item', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const run = JSON.parse(files[RUN_PATH] ?? '{}') as { waiting?: unknown }
  run.waiting = null
  files['/fixtures/run.json'] = JSON.stringify(run) // the fixtures copy reads this source
  files[RUN_PATH] = JSON.stringify(run)
  const status: (string | undefined)[] = []
  mockFs(on, files)
  mockWorld(on, { status: text => status.push(text) })
  await $.session.start(sessionStart())
  expect(status.at(-1)).toBe('⬡ mk · ⚠ FIX6 stalled (11m) · 1 building') // stalled outranks plain progress
})

test('toast fires once on a status change, never on first load', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  mockFs(on, files)
  const toasts: string[] = []
  const clock = mockWorld(on, { toast: text => toasts.push(text) })
  await $.session.start(sessionStart())
  expect(toasts).toEqual([]) // first load never toasts
  const run = JSON.parse(files[RUN_PATH] ?? '{}') as { tasks?: Record<string, unknown>[] }
  const tasks: Record<string, unknown>[] = run.tasks ?? []
  tasks[2] = { ...(tasks[2] ?? {}), status: 'merged', last_commit: 'abc1234', updated: new Date(NOW).toISOString() }
  files[RUN_PATH] = JSON.stringify(run)
  await clock.advance(5000)
  expect(toasts).toEqual(['⬡ [MatrixKoder] Task F8 merged & passed gates (abc1234)'])
  await clock.advance(5000)
  await clock.advance(5000)
  expect(toasts).toEqual(['⬡ [MatrixKoder] Task F8 merged & passed gates (abc1234)']) // still exactly once
})

// ---------- fix1: chip window kind, run agents, ordering; toast copy ----------

test('quota chip uses the real window kind (5h)', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    run.waiting = null
    const tasks = tasksOf(run)
    if (tasks[3]) {
      tasks[3].status = 'building' // no stalled task
      tasks[3].agent = 'agy'
    }
  })
  setUsage(files, usage => {
    const agy = agentOf(usage, 'agy')
    if (agy) agy.exhausted_until = new Date(NOW + 2 * 3600_000).toISOString()
  })
  const chips: (string | undefined)[] = []
  mockFs(on, files)
  mockWorld(on, { status: t => chips.push(t) })
  await $.session.start(sessionStart())
  expect(chips.at(-1) ?? '').toBe('⬡ mk · agy ⊘ 5h resets 10-07 · 2 building')
})

test('only agents the run routes to can raise the quota chip', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    run.waiting = null
    const tasks = tasksOf(run)
    if (tasks[3]) {
      tasks[3].status = 'building'
      tasks[3].agent = 'codex' // no task uses opencode
    }
  })
  setUsage(files, usage => {
    const oc = agentOf(usage, 'opencode')
    if (oc) oc.exhausted_until = new Date(NOW + 18 * 3600_000).toISOString() // unrelated, must be ignored
  })
  const chips: (string | undefined)[] = []
  mockFs(on, files)
  mockWorld(on, { status: t => chips.push(t) })
  await $.session.start(sessionStart())
  expect(chips.at(-1) ?? '').not.toContain('⊘ wk')
  expect(chips.at(-1) ?? '').toBe('⬡ mk · 2 building · 1 review · 1/5 done')
})

test('all-done outranks quota', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    run.waiting = null
    for (const t of tasksOf(run)) t.status = 'done'
  })
  setUsage(files, usage => {
    const agy = agentOf(usage, 'agy')
    if (agy) agy.exhausted_until = new Date(NOW + 6 * 24 * 3600_000).toISOString()
  })
  const chips: (string | undefined)[] = []
  mockFs(on, files)
  mockWorld(on, { status: t => chips.push(t) })
  await $.session.start(sessionStart())
  expect(chips.at(-1) ?? '').toBe('⬡ mk · ✓ all 5 tasks merged · stage 7/12')
})

test('blocked chip names the dependencies', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    run.waiting = null
    const tasks = tasksOf(run)
    if (tasks[3]) tasks[3].status = 'building'
    if (tasks[4]) tasks[4].status = 'blocked'
  })
  const chips: (string | undefined)[] = []
  mockFs(on, files)
  mockWorld(on, { status: t => chips.push(t) })
  await $.session.start(sessionStart())
  expect(chips.at(-1) ?? '').toBe('⬡ mk · ⊘ X1 blocked by F6, F8 · 2 building')
})

test('stalled toast uses the lane basename', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    run.waiting = null
    const tasks = tasksOf(run)
    if (tasks[3]) tasks[3].status = 'building'
  })
  mockFs(on, files)
  const toasts: string[] = []
  const clock = mockWorld(on, { toast: t => toasts.push(t) })
  await $.session.start(sessionStart())
  setRun(files, run => {
    const tasks = tasksOf(run)
    if (tasks[3]) {
      tasks[3].status = 'stalled'
      tasks[3].updated = new Date(NOW - 11 * 60_000).toISOString()
    }
  })
  await clock.advance(5000)
  expect(toasts).toEqual(['⬡ [MatrixKoder] Task FIX6 stalled: no activity in wt-fix6 (11m)'])
})

test('quota toast carries the exhausted pool', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    run.waiting = null
    const tasks = tasksOf(run)
    if (tasks[3]) tasks[3].status = 'building'
  })
  mockFs(on, files)
  const toasts: string[] = []
  const clock = mockWorld(on, { toast: t => toasts.push(t) })
  await $.session.start(sessionStart())
  setRun(files, run => {
    const tasks = tasksOf(run)
    if (tasks[2]) tasks[2].status = 'quota'
  })
  await clock.advance(5000)
  expect(toasts[0]).toContain('Quota reached for agy.claude-gpt (wk).')
})

test('a run switch never toasts old-vs-new diffs', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    run.waiting = null
  })
  mockFs(on, files)
  const toasts: string[] = []
  const clock = mockWorld(on, { toast: t => toasts.push(t) })
  await $.session.start(sessionStart())
  files['/fixtures/index.json'] = files[RUNS_INDEX] = JSON.stringify([
    { id: 'other', feature: 'other-feature', status: 'active', started: '2026-10-07T17:00:00+02:00' },
  ])
  files['/fixtures/run.json'] = files[RUN_PATH] = JSON.stringify({ ...JSON.parse(worldFiles()[RUN_PATH] ?? '{}'), id: 'other', feature: 'other-feature', waiting: { question: 'Merge now?' } })
  await clock.advance(5000)
  expect(toasts).toEqual([]) // same ids, different run: silent reset
})

test('provider toast sanitises the event text', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    run.waiting = null
    run.recent = [{ t: '2026-10-07T18:00:00+02:00', task: 'FIX6', type: 'note', text: '503\nService\tUnavailable' }]
    const tasks = tasksOf(run)
    if (tasks[3]) tasks[3].status = 'building'
  })
  mockFs(on, files)
  const toasts: string[] = []
  const clock = mockWorld(on, { toast: t => toasts.push(t) })
  await $.session.start(sessionStart())
  setRun(files, run => {
    const tasks = tasksOf(run)
    if (tasks[3]) tasks[3].status = 'provider'
  })
  await clock.advance(5000)
  expect(toasts[0]).toContain('Provider error on codex: 503 Service Unavailable')
  expect(toasts[0]).not.toContain('\n')
})
