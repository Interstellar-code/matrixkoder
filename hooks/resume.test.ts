// Resume section tests: prompt.compose carries the active run while one exists,
// and leaves the prompt untouched without one.
import { test, expect } from 'claude-code/testing'
import { composeArgs, mockFs, mockWorld, sessionStart, setRun, worldFiles, worldFixtures } from './testkit'

test('compose adds a session-scoped section while a run is active', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const { sections } = await $.prompt.compose(composeArgs())
  const mk = sections.filter(s => s.id === 'matrixkoder:run')
  expect(mk).toHaveLength(1)
  expect(mk[0].scope).toBe('session')
  expect(mk[0].text).toContain('wfv2')
  expect(mk[0].text).toContain('workflows-v2')
  expect(mk[0].text).toContain('stage 7/12')
  expect(mk[0].text).toContain('next: verify F6 gates in worktree-b')
  expect(mk[0].text).toContain('waiting on user: F8 deletes rows')
  expect(mk[0].text).toContain('1 merged')
  expect(mk[0].text.length).toBeLessThanOrEqual(600)
})

test('compose is unchanged without a run', async ($, on) => {
  mockFs(on, {}, [])
  mockWorld(on)
  await $.session.start(sessionStart())
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
  mockWorld(on)
  await $.session.start(sessionStart())
  const { sections } = await $.prompt.compose(composeArgs())
  const mk = sections.filter(s => s.id === 'matrixkoder:run')
  expect(mk).toHaveLength(1)
  expect(mk[0].text.length).toBeLessThanOrEqual(600)
  expect(mk[0].text).toContain('tasks:') // the counts tail survived
  expect(mk[0].text).not.toContain('\n')
  expect(mk[0].text).not.toContain('\t')
  expect(mk[0].text).not.toMatch(/x{201,}/) // next capped at 200
  expect(mk[0].text).not.toMatch(/y{201,}/) // question capped at 200
})
