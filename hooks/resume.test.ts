// Resume section tests: prompt.compose carries the active run while one exists,
// and leaves the prompt untouched without one.
import { test, expect } from 'claude-code/testing'
import { composeArgs, mockFs, mockWorld, sessionStart, worldFiles, worldFixtures } from './testkit'

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
