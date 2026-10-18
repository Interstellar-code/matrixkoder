// Tests for F1: pure activity store and watch-only tool-call hook
import { test, expect } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { MkFileAct } from '../types'
import { recordActivity, liveActivity, registerFileActivity } from './filetree-activity'

test('Files F1.1: recordActivity returns a new list, drops entries older than 3000 ms and keeps at most 200', async () => {
  const initial: readonly MkFileAct[] = Object.freeze([{ path: '/old.ts', kind: 'read', at: 1000 }])
  const now = 4001
  const updated = recordActivity(initial, '/new.ts', 'write', now)

  // never mutates input and returns a new list
  expect(initial.length).toBe(1)
  expect(updated !== initial).toBe(true)

  // drops entries older than 3000 ms (4001 - 1000 = 3001 > 3000)
  expect(updated.find(x => x.path === '/old.ts')).toBeUndefined()
  expect(updated).toEqual([{ path: '/new.ts', kind: 'write', at: 4001 }])

  // keeps at most 200 (oldest dropped first)
  let store: readonly MkFileAct[] = []
  for (let i = 0; i < 250; i++) {
    store = recordActivity(store, `/file-${i}.ts`, 'read', 5000 + i)
  }
  expect(store.length).toBe(200)
  expect(store[0]!.path).toBe('/file-50.ts')
  expect(store[199]!.path).toBe('/file-249.ts')
})

test('Files F1.1: a read recorded inside 3 s after a write on the same path leaves the write', async () => {
  const s1 = recordActivity([], '/file.ts', 'write', 1000)
  expect(s1).toEqual([{ path: '/file.ts', kind: 'write', at: 1000 }])

  // inside 3s (1000ms later), recording a read leaves the write
  const s2 = recordActivity(s1, '/file.ts', 'read', 2000)
  expect(s2).toEqual([{ path: '/file.ts', kind: 'write', at: 1000 }])
  expect(liveActivity(s2, 2000).get('/file.ts')).toBe('write')

  // > 3s later (3500ms later), the write has expired, so read is recorded
  const s3 = recordActivity(s1, '/file.ts', 'read', 4500)
  expect(s3).toEqual([{ path: '/file.ts', kind: 'read', at: 4500 }])
  expect(liveActivity(s3, 4500).get('/file.ts')).toBe('read')
})

test('Files F1.1: liveActivity returns Map path to read or write with 2900ms in and 3100ms out', async () => {
  const now = 10000
  const store: readonly MkFileAct[] = [
    { path: '/recent.ts', kind: 'read', at: now - 2900 },
    { path: '/old.ts', kind: 'write', at: now - 3100 },
    { path: '/written.ts', kind: 'write', at: now - 1000 },
    { path: '/written.ts', kind: 'read', at: now - 500 },
  ]
  const live = liveActivity(store, now)
  // 2900ms old is in it
  expect(live.has('/recent.ts')).toBe(true)
  expect(live.get('/recent.ts')).toBe('read')
  // 3100ms old is not in it
  expect(live.has('/old.ts')).toBe(false)
  // a write beats a read
  expect(live.get('/written.ts')).toBe('write')

  // empty or undefined store
  expect(liveActivity(undefined, now).size).toBe(0)
  expect(liveActivity([], now).size).toBe(0)
})

test('Files F1.1: a relative path or an empty string returns the store unchanged', async () => {
  const initial: readonly MkFileAct[] = Object.freeze([{ path: '/valid.ts', kind: 'write', at: 1000 }])
  expect(recordActivity(initial, '', 'read', 2000)).toBe(initial)
  expect(recordActivity(initial, 'relative/path.ts', 'write', 2000)).toBe(initial)
  expect(recordActivity(initial, './rel.ts', 'read', 2000)).toBe(initial)
  expect(recordActivity(undefined, '', 'read', 2000)).toEqual([])
  expect(recordActivity(undefined, 'relative/path.ts', 'write', 2000)).toEqual([])
})

// Helper to set up in-memory state.get and state.set store matching wake.test.ts:39-50
function setupStateStore(on: On) {
  const store = new Map<string, unknown>()
  let ver = 1
  const listeners: Array<() => void> = []

  on('state.get', (_$, e, next) => {
    if (e.key === 'fileActivity') {
      return { value: { value: store.get('fileActivity'), version: ver } }
    }
    return next(e)
  })
  on('state.set', (_$, e, next) => {
    if (e.key === 'fileActivity') {
      if (e.ifVersion !== undefined && e.ifVersion !== ver) {
        return { value: { isSet: false, version: ver } }
      }
      ver++
      store.set('fileActivity', e.value)
      for (const fn of listeners) fn()
      return { value: { isSet: true, version: ver } }
    }
    return next(e)
  })

  const waitForSets = (count: number) =>
    new Promise<void>(resolve => {
      let seen = 0
      listeners.push(() => {
        seen++
        if (seen >= count) resolve()
      })
    })

  return { store, waitForSets }
}

test('Files F1.2: registerFileActivity registers Read as read, Edit and Write as write with file_path, and NotebookEdit as write with notebook_path', async ($, on) => {
  const { store, waitForSets } = setupStateStore(on)
  registerFileActivity(on)

  // Downstream handler to answer the tool calls so they succeed
  on('tool.call', {}, (_$, e) => ({ result: { ok: true } }))

  const wait4 = waitForSets(4)
  await $.tool.call({ tool: 'Read', file_path: '/src/read.ts' } as any)
  await $.tool.call({ tool: 'Edit', file_path: '/src/edit.ts' } as any)
  await $.tool.call({ tool: 'Write', file_path: '/src/write.ts' } as any)
  await $.tool.call({ tool: 'NotebookEdit', notebook_path: '/src/nb.ipynb' } as any)

  await wait4

  const entries = store.get('fileActivity') as readonly MkFileAct[] | undefined
  expect(entries).toBeDefined()
  expect(entries!.length).toBe(4)

  const map = new Map(entries!.map(e => [e.path, e.kind]))
  expect(map.get('/src/read.ts')).toBe('read')
  expect(map.get('/src/edit.ts')).toBe('write')
  expect(map.get('/src/write.ts')).toBe('write')
  expect(map.get('/src/nb.ipynb')).toBe('write')
})

test('Files F1.2: every call reaches next with same input and unchanged result even with missing, relative or non-string path', async ($, on) => {
  const { store } = setupStateStore(on)
  registerFileActivity(on)

  const receivedInputs: any[] = []
  on('tool.call', {}, (_$, e) => {
    receivedInputs.push(e)
    return { result: { custom: 'expected-output' } }
  })

  // Missing path
  const r1 = await $.tool.call({ tool: 'Read' } as any)
  expect(r1).toEqual({ result: { custom: 'expected-output' } })

  // Relative path
  const r2 = await $.tool.call({ tool: 'Edit', file_path: 'relative/file.ts' } as any)
  expect(r2).toEqual({ result: { custom: 'expected-output' } })

  // Non-string path
  const r3 = await $.tool.call({ tool: 'Write', file_path: 12345 } as any)
  expect(r3).toEqual({ result: { custom: 'expected-output' } })

  expect(receivedInputs.length).toBe(3)
  expect(receivedInputs[0].tool).toBe('Read')
  expect(receivedInputs[1].file_path).toBe('relative/file.ts')
  expect(receivedInputs[2].file_path).toBe(12345)

  // Verify the store remains empty (no activity recorded for missing, relative, or non-string paths)
  expect(store.has('fileActivity')).toBe(false)
})

test('Files F1.2: state write is update without await and rejection swallowed so throwing update lets tool run', async ($, on) => {
  let stateMode: 'pending' | 'reject' | 'throw' = 'pending'
  let updateStarted = false
  let nextCalled = false

  on('state.get', (_$, e, next) => {
    if (e.key === 'fileActivity') {
      updateStarted = true
      if (stateMode === 'pending') {
        return new Promise(() => {}) // pending forever: never resolves
      }
      if (stateMode === 'reject') {
        return Promise.reject(new Error('async update rejection'))
      }
      throw new Error('sync state.get throw')
    }
    return next(e)
  })

  on('tool.call', {}, () => {
    nextCalled = true
    return { result: { success: true } }
  })
  registerFileActivity(on)

  const timeout = (ms: number) =>
    new Promise((_, rej) => (globalThis as any).setTimeout(() => rej(new Error('timed out: hook awaited update before calling next')), ms))

  // 1. Pending update: hook must call next and settle without waiting for update
  const resPending = await Promise.race([
    $.tool.call({ tool: 'Read', file_path: '/src/pending.ts' } as any),
    timeout(4000),
  ])
  expect(updateStarted).toBe(true)
  expect(nextCalled).toBe(true)
  expect(resPending).toEqual({ result: { success: true } })

  // 2. Rejecting update: hook must swallow rejection, not throw or return a rejected promise
  stateMode = 'reject'
  nextCalled = false
  const resReject = await $.tool.call({ tool: 'Write', file_path: '/src/reject.ts' } as any)
  expect(nextCalled).toBe(true)
  expect(resReject).toEqual({ result: { success: true } })

  // 3. Synchronously throwing update: hook must swallow throw and let tool run
  stateMode = 'throw'
  nextCalled = false
  const resThrow = await $.tool.call({ tool: 'Edit', file_path: '/src/throw.ts' } as any)
  expect(nextCalled).toBe(true)
  expect(resThrow).toEqual({ result: { success: true } })
})

test('Files F1.2: two calls recorded at once both end up in the store', async ($, on) => {
  const { store, waitForSets } = setupStateStore(on)
  registerFileActivity(on)

  on('tool.call', {}, () => ({ result: { ok: true } }))

  const wait2 = waitForSets(2)
  // Concurrent tool calls
  await Promise.all([
    $.tool.call({ tool: 'Read', file_path: '/concurrent-1.ts' } as any),
    $.tool.call({ tool: 'Write', file_path: '/concurrent-2.ts' } as any),
  ])

  await wait2

  const entries = store.get('fileActivity') as readonly MkFileAct[] | undefined
  expect(entries).toBeDefined()
  expect(entries!.length).toBe(2)
  const paths = entries!.map(e => e.path)
  expect(paths).toContain('/concurrent-1.ts')
  expect(paths).toContain('/concurrent-2.ts')
})

test('Files F1.2: a call of any other tool records nothing', async ($, on) => {
  const { store } = setupStateStore(on)
  registerFileActivity(on)

  on('tool.call', {}, () => ({ result: { stdout: 'done' } }))

  await $.tool.call({ tool: 'Bash', command: 'ls' } as any)
  await $.tool.call({ tool: 'Grep', pattern: 'foo' } as any)

  expect(store.has('fileActivity')).toBe(false)
})
