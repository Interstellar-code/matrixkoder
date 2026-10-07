// Tests for MatrixKoder wake prompt.submit on lane events
import { test, expect, mock } from 'claude-code/testing'
import type { Plugin } from 'claude-code'

test('wake fires once for a new verifying event', { options: { wake: true } }, async ($, on) => {
  const clock = mock.clock(on)
  const submitted: string[] = []

  const files: Record<string, string> = {
    '/data/runs/index.json': JSON.stringify([{ id: 'test-run', status: 'active' }]),
    '/data/runs/test-run/events.jsonl': JSON.stringify({
      t: '2026-10-07T20:00:00+02:00',
      run: 'test-run',
      task: 'F6',
      type: 'transition',
      from: 'building',
      to: 'verifying',
    }) + '\n',
  }

  mock.env(on, { MATRIXKODER_DATA: '/data' })

  on('fs.read', (_$, e) => {
    if (e.path in files) return { value: files[e.path] }
    return { value: '' }
  })
  on('prompt.submit', (_$, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  on('session.start', (_$, e, next) => ({ cwd: '/tmp' }))

  await $.session.start({ cwd: '/tmp' })
  expect(submitted.length).toBe(0)

  // Advance 30s for the poll timer
  await clock.advance(30000)
  expect(submitted.length).toBe(0)

  // Advance 60s for the debounce timer
  await clock.advance(60000)
  expect(submitted).toEqual([
    'MatrixKoder: F6 → verifying. Continue the run from its Next action.',
  ])
})

test('not for review event', { options: { wake: true } }, async ($, on) => {
  const clock = mock.clock(on)
  const submitted: string[] = []

  const files: Record<string, string> = {
    '/data/runs/index.json': JSON.stringify([{ id: 'test-run-rev', status: 'active' }]),
    '/data/runs/test-run-rev/events.jsonl': JSON.stringify({
      t: '2026-10-07T20:00:00+02:00',
      run: 'test-run-rev',
      task: 'F6',
      type: 'transition',
      from: 'building',
      to: 'review',
    }) + '\n',
  }

  mock.env(on, { MATRIXKODER_DATA: '/data' })

  on('fs.read', (_$, e) => {
    if (e.path in files) return { value: files[e.path] }
    return { value: '' }
  })
  on('prompt.submit', (_$, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  on('session.start', (_$, e, next) => ({ cwd: '/tmp' }))

  await $.session.start({ cwd: '/tmp' })
  await clock.advance(30000)
  await clock.advance(60000)

  expect(submitted.length).toBe(0)
})

test('not twice after reload', { options: { wake: true } }, async ($, on) => {
  const clock = mock.clock(on)
  const submitted: string[] = []

  const files: Record<string, string> = {
    '/data/runs/index.json': JSON.stringify([{ id: 'test-run-reload', status: 'active' }]),
    '/data/runs/test-run-reload/events.jsonl': JSON.stringify({
      t: '2026-10-07T20:00:00+02:00',
      run: 'test-run-reload',
      task: 'F6',
      type: 'transition',
      from: 'building',
      to: 'verifying',
    }) + '\n',
  }

  mock.env(on, { MATRIXKODER_DATA: '/data' })

  on('fs.read', (_$, e) => {
    if (e.path in files) return { value: files[e.path] }
    return { value: '' }
  })
  on('prompt.submit', (_$, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })

  on('state.get', (_$, e) => {
    if (e.key === 'wakeEventLines' && e.id === 'test-run-reload') {
      return { value: 1, version: 1 }
    }
    return { value: undefined, version: 0 }
  })

  on('session.start', (_$, e, next) => ({ cwd: '/tmp' }))

  await $.session.start({ cwd: '/tmp' })
  await clock.advance(30000)
  await clock.advance(60000)

  // Should NOT fire because line count 1 was already tracked in state
  expect(submitted.length).toBe(0)
})

test('debounce with mock clock batches events into one submit', { options: { wake: true } }, async ($, on) => {
  const clock = mock.clock(on)
  const submitted: string[] = []

  let eventLines = [
    JSON.stringify({
      t: '2026-10-07T20:00:00+02:00',
      run: 'batch-run',
      task: 'F6',
      type: 'transition',
      from: 'building',
      to: 'verifying',
    }),
  ]

  mock.env(on, { MATRIXKODER_DATA: '/data' })

  on('fs.read', (_$, e) => {
    if (e.path === '/data/runs/index.json') {
      return { value: JSON.stringify([{ id: 'batch-run', status: 'active' }]) }
    }
    if (e.path === '/data/runs/batch-run/events.jsonl') {
      return { value: eventLines.join('\n') + '\n' }
    }
    return { value: '' }
  })
  on('prompt.submit', (_$, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  on('session.start', (_$, e, next) => ({ cwd: '/tmp' }))

  await $.session.start({ cwd: '/tmp' })

  // Poll 1 at 30s: finds F6 -> verifying, starts 60s debounce
  await clock.advance(30000)
  expect(submitted.length).toBe(0)

  // Append second event before debounce expires
  eventLines.push(
    JSON.stringify({
      t: '2026-10-07T20:00:30+02:00',
      run: 'batch-run',
      task: 'F8',
      type: 'transition',
      from: 'building',
      to: 'stalled',
    }),
  )

  // Poll 2 at 60s: finds F8 -> stalled, reschedules debounce for another 60s
  await clock.advance(30000)
  expect(submitted.length).toBe(0)

  // Advance remaining 60s for the debounce timer to fire
  await clock.advance(60000)

  // Exactly ONE combined batch prompt submitted!
  expect(submitted).toEqual([
    'MatrixKoder: F6 → verifying, F8 → stalled. Continue the run from its Next action.',
  ])
})

test('wake: false registers nothing', { options: { wake: false } }, async ($, on) => {
  const clock = mock.clock(on)
  const submitted: string[] = []

  on('prompt.submit', (_$, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  on('session.start', (_$, e, next) => ({ cwd: '/tmp' }))

  await $.session.start({ cwd: '/tmp' })
  await clock.advance(60000)

  expect(submitted.length).toBe(0)
})
