// Resume section tests: prompt.compose carries the active run while one exists,
// and leaves the prompt untouched without one.
import { test, expect } from 'claude-code/testing'
import { composeArgs, mockFs, mockWorld, sessionStart, setRun, worldFiles, worldFixtures } from './testkit'

test('compose adds a session-scoped section while a run is active', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  await clock.advance(0) // R3: session.start no longer blocks on the first load
  const { sections } = await $.prompt.compose(composeArgs())
  const mk = sections.filter(s => s.id === 'matrixkoder:run')
  expect(mk).toHaveLength(1)
  const section = mk[0]
  if (!section) throw new Error('compose section missing')
  expect(section.scope).toBe('session')
  expect(section.text).toContain('wfv2')
  expect(section.text).toContain('workflows-v2')
  expect(section.text).toContain('stage 7/12')
  expect(section.text).toContain('next: verify F6 gates in worktree-b')
  expect(section.text).toContain('waiting on user: F8 deletes rows')
  expect(section.text).toContain('1 merged')
  expect(section.text.length).toBeLessThanOrEqual(600)
})

test('compose is unchanged without a run', async ($, on) => {
  mockFs(on, {}, [])
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  await clock.advance(0) // R3: session.start no longer blocks on the first load
  const { sections } = await $.prompt.compose(composeArgs())
  expect(sections.filter(s => s.id === 'matrixkoder:run')).toHaveLength(0)
})

// ---------- fix1: caps and sanitising ----------

test('long next/question are capped and control-stripped, counts survive', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  setRun(files, run => {
    run.next = `next ${'x'.repeat(500)}`
    run.waiting = { question: `approve\n${'y'.repeat(300)}\ttab`, options: ['a', 'b'], rec: 'a' }
  })
  mockFs(on, files)
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  await clock.advance(0) // R3: session.start no longer blocks on the first load
  const { sections } = await $.prompt.compose(composeArgs())
  const mk = sections.filter(s => s.id === 'matrixkoder:run')
  expect(mk).toHaveLength(1)
  const section = mk[0]
  if (!section) throw new Error('compose section missing')
  expect(section.text.length).toBeLessThanOrEqual(600)
  expect(section.text).toContain('tasks:') // the counts tail survived
  expect(section.text).not.toContain('\n')
  expect(section.text).not.toContain('\t')
  expect(section.text).not.toMatch(/x{201,}/) // next capped at 200
  expect(section.text).not.toMatch(/y{201,}/) // question capped at 200
})

// ---------- S1.3: the section names the mode ----------

test('solo mode: the section is exactly the solo line, no run lines', { plugins: [worldFixtures], options: { mode: 'solo' } }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  await clock.advance(0) // R3: session.start no longer blocks on the first load
  const { sections } = await $.prompt.compose(composeArgs())
  const mk = sections.filter(s => s.id === 'matrixkoder:run')
  expect(mk).toHaveLength(1)
  expect(mk[0]?.text).toBe('MK solo: no orchestration, build in this session')
})

test('a missing mode (orchestrator) names the mode and still carries the run lines', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on)
  await $.session.start(sessionStart())
  await clock.advance(0) // R3: session.start no longer blocks on the first load
  const { sections } = await $.prompt.compose(composeArgs())
  const section = sections.find(s => s.id === 'matrixkoder:run')
  expect(section?.text).toContain('mode orchestrator')
  expect(section?.text).toContain('MatrixKoder run wfv2 (workflows-v2)')
  expect(section?.text).toContain('stage 7/12')
  expect(section?.text).toContain('tasks:')
  expect(section?.text.length).toBeLessThanOrEqual(600)
})

// ---------- F.7 R2: the RED/REWORK/DUE banner ----------

const bannerStatus = (over: Record<string, unknown> = {}): string =>
  JSON.stringify({
    ok: true, run: 'wfv2', fidelity: true, green: false, red: 1, stale: 0, deferred: 0, manual: 0, rework: 1, due: 2, open_reasons: 0,
    rows: [{ id: 'F6.a1', evidence: [7], state: 'RED' }], ...over,
  })

const reportProc = (stdout: string, exitCode = 0) => (argv: readonly string[]) =>
  argv.some(a => a.endsWith('mk-report.py'))
    ? { value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    : undefined

test('a fidelity run with open rows gets one banner line with counts and the first ids', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on, { process: reportProc(bannerStatus()) })
  await $.session.start(sessionStart())
  await clock.advance(0) // R3: session.start no longer blocks on the first load
  const { sections } = await $.prompt.compose(composeArgs())
  const banner = sections.filter(s => s.id === 'matrixkoder:red')
  expect(banner).toHaveLength(1)
  expect(banner[0]?.text).toContain('red 1')
  expect(banner[0]?.text).toContain('rework 1')
  expect(banner[0]?.text).toContain('due 2')
  expect(banner[0]?.text).toContain('F6.a1(RED)')
  expect(sections.filter(s => s.id === 'matrixkoder:run')).toHaveLength(1) // the run section is untouched
})

test('no banner for a green run, a legacy run, a failed status read, or solo mode', { plugins: [worldFixtures] }, async ($, on) => {
  let out = bannerStatus({ green: true, red: 0, rework: 0, due: 0, rows: [] })
  let code = 0
  mockFs(on, worldFiles())
  const clock = mockWorld(on, { process: (argv) => reportProc(out, code)(argv) })
  await $.session.start(sessionStart())
  await clock.advance(0) // R3: session.start no longer blocks on the first load
  const banner = async () => (await $.prompt.compose(composeArgs())).sections.filter(s => s.id === 'matrixkoder:red')
  expect(await banner()).toHaveLength(0) // green
  out = bannerStatus({ fidelity: false })
  expect(await banner()).toHaveLength(0) // legacy
  out = 'garbage'
  expect(await banner()).toHaveLength(0)
  out = bannerStatus()
  code = 2
  expect(await banner()).toHaveLength(0) // reader failed
})

test('solo mode shows no banner', { plugins: [worldFixtures], options: { mode: 'solo' } }, async ($, on) => {
  mockFs(on, worldFiles())
  const clock = mockWorld(on, { process: reportProc(bannerStatus()) })
  await $.session.start(sessionStart())
  await clock.advance(0) // R3: session.start no longer blocks on the first load
  const { sections } = await $.prompt.compose(composeArgs())
  expect(sections.filter(s => s.id === 'matrixkoder:red')).toHaveLength(0)
})
