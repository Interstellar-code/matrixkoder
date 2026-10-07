// Chip + toast tests (design §5): the chip text after the first load, a toast
// exactly once per status change, none on the first load.
import { test, expect } from 'claude-code/testing'
import { NOW, RUN_PATH, mockFs, mockWorld, sessionStart, worldFiles, worldFixtures } from './testkit'

test('chip shows waiting-on-you over everything else', { plugins: [worldFixtures] }, async ($, on) => {
  const status: (string | undefined)[] = []
  mockFs(on, worldFiles())
  mockWorld(on, { status: text => status.push(text) })
  await $.session.start(sessionStart())
  expect(status.at(-1)).toBe('⬡ mk · ⚠ 1 waiting on you · 1 building')
})

test('chip falls back to the stalled template without a waiting item', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const run = JSON.parse(files[RUN_PATH])
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
  const run = JSON.parse(files[RUN_PATH])
  run.tasks[2] = { ...run.tasks[2], status: 'merged', last_commit: 'abc1234', updated: new Date(NOW).toISOString() }
  files[RUN_PATH] = JSON.stringify(run)
  await clock.advance(5000)
  expect(toasts).toEqual(['⬡ [MatrixKoder] Task F8 merged & passed gates (abc1234)'])
  await clock.advance(5000)
  await clock.advance(5000)
  expect(toasts).toEqual(['⬡ [MatrixKoder] Task F8 merged & passed gates (abc1234)']) // still exactly once
})
