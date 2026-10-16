// F.7 R2: the Stop hook blocks once per red-set hash and never traps; the status reader and the
// banner text are pure. `claude plugin test` cannot spawn, so mk-report.py status is answered by the
// process mock with a line captured from the real `mk-report.py status` shape.
import { test, expect } from 'claude-code/testing'
import type { On } from 'claude-code'
import { mockFs, mockWorld, PROJECT_DIR, DATA_DIR } from './testkit'
import { bannerText, openCount, parseStatus, redSetHash } from './stop-guard'

const statusLine = (over: Record<string, unknown> = {}): string =>
  JSON.stringify({
    ok: true, run: 'r1', fidelity: true, green: false, red: 1, stale: 1, deferred: 0, manual: 0, rework: 1, due: 0, open_reasons: 0,
    rows: [{ id: 'F.1.a1', evidence: [4], state: 'RED' }, { id: 'F.2.a1', evidence: [], state: 'STALE' }, { id: 'F.3.a1', evidence: [9], state: 'GREEN' }],
    ...over,
  })

const proc = (stdout: string, exitCode = 0, seen?: string[][]) => (argv: readonly string[]) => {
  if (!argv.some(a => a.endsWith('mk-report.py'))) return undefined
  seen?.push([...argv])
  return { value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
}

const ENV = { MATRIXKODER_DATA: DATA_DIR }

// the bottom of the classic.Stop chain: what the engine does when no hook blocks
const bottom = (on: On) => on('classic.Stop', (_$, _e) => ({}))

test('parseStatus keeps the fidelity flag and the rows, and refuses anything else', () => {
  const s = parseStatus(statusLine())
  expect(s?.fidelity).toBe(true)
  expect(s?.rows.map(r => r.id)).toEqual(['F.1.a1', 'F.2.a1', 'F.3.a1'])
  expect(parseStatus('{"ok":false}')).toBeNull()
  expect(parseStatus('not json')).toBeNull()
  expect(openCount(s!)).toBe(3)
})

test('bannerText: counts and the first open ids; nothing for green or legacy', () => {
  const s = parseStatus(statusLine())
  const text = bannerText(s)
  expect(text).toContain('red 1')
  expect(text).toContain('rework 1')
  expect(text).toContain('F.1.a1(RED)')
  expect(text).not.toContain('F.3.a1')
  expect(bannerText(parseStatus(statusLine({ fidelity: false })))).toBeNull()
  expect(bannerText(parseStatus(statusLine({ red: 0, stale: 0, rework: 0, rows: [] })))).toBeNull()
  expect(bannerText(null)).toBeNull()
})

test('redSetHash is stable for one red set and changes when a row changes', () => {
  const a = parseStatus(statusLine())!
  const b = parseStatus(statusLine())!
  const c = parseStatus(statusLine({ rows: [{ id: 'F.1.a1', evidence: [], state: 'STALE' }] }))!
  expect(redSetHash(a)).toBe(redSetHash(b))
  expect(redSetHash(a)).not.toBe(redSetHash(c))
})

test('blocks once for a red set; the second stop of the same set passes', { options: { stopGuard: true } }, async ($, on) => {
  bottom(on)
  mockFs(on, {})
  mockWorld(on, { env: ENV, process: proc(statusLine()) })
  const first = await $.classic.Stop({ stop_hook_active: false, cwd: PROJECT_DIR })
  expect(first.block).toContain('RED/REWORK/DUE')
  expect(first.block).toContain('F.1.a1(RED)')
  const again = await $.classic.Stop({ stop_hook_active: false, cwd: PROJECT_DIR })
  expect(again.block).toBeUndefined() // same red set: already blocked once
})

test('a changed red set blocks again', { options: { stopGuard: true } }, async ($, on) => {
  let line = statusLine()
  bottom(on)
  mockFs(on, {})
  mockWorld(on, { env: ENV, process: (argv) => proc(line)(argv) })
  expect((await $.classic.Stop({ stop_hook_active: false, cwd: PROJECT_DIR })).block).toBeDefined()
  line = statusLine({ red: 2, rows: [{ id: 'F.9.a1', evidence: [], state: 'RED' }] })
  expect((await $.classic.Stop({ stop_hook_active: false, cwd: PROJECT_DIR })).block).toBeDefined()
})

test('stop_hook_active never blocks (no trap), and asks nothing of the status reader', { options: { stopGuard: true } }, async ($, on) => {
  const seen: string[][] = []
  bottom(on)
  mockFs(on, {})
  mockWorld(on, { env: ENV, process: proc(statusLine(), 0, seen) })
  const res = await $.classic.Stop({ stop_hook_active: true, cwd: PROJECT_DIR })
  expect(res.block).toBeUndefined()
  expect(seen).toHaveLength(0)
})

test('lane cwds pass: a -mk-<n> slot, a linked worktree .git file, a lane status file, the lane PATH shim', { options: { stopGuard: true } }, async ($, on) => {
  bottom(on)
  mockFs(on, {}, [`${PROJECT_DIR}/.git`, '/tmp/other/.git', '/tmp/status-lane/.git'],
    ['/tmp/wt/.git', '/tmp/status-lane/.matrixkoder-status.json'])
  mockWorld(on, { env: ENV, process: proc(statusLine()) })
  for (const cwd of ['/tmp/proj-mk-2', '/tmp/wt', '/tmp/status-lane']) {
    expect((await $.classic.Stop({ stop_hook_active: false, cwd })).block).toBeUndefined()
  }
})

test('the lane PATH shim passes', { options: { stopGuard: true } }, async ($, on) => {
  bottom(on)
  mockFs(on, {})
  mockWorld(on, { env: { ...ENV, PATH: '/usr/bin:/lane/.matrixkoder-bin' }, process: proc(statusLine()) })
  expect((await $.classic.Stop({ stop_hook_active: false, cwd: PROJECT_DIR })).block).toBeUndefined()
})

test('a legacy run, a green run and an unreadable status pass', { options: { stopGuard: true } }, async ($, on) => {
  let out = statusLine({ fidelity: false })
  let code = 0
  bottom(on)
  mockFs(on, {})
  mockWorld(on, { env: ENV, process: (argv) => proc(out, code)(argv) })
  const stop = () => $.classic.Stop({ stop_hook_active: false, cwd: PROJECT_DIR })
  expect((await stop()).block).toBeUndefined() // legacy
  out = statusLine({ green: true, red: 0, stale: 0, rework: 0, rows: [] })
  expect((await stop()).block).toBeUndefined() // green
  out = statusLine({ green: false, red: 0, stale: 0, rework: 0, rows: [] })
  expect((await stop()).block).toBeUndefined() // just in progress: nothing red
  out = 'garbage'
  expect((await stop()).block).toBeUndefined() // unreadable
  out = statusLine()
  code = 2
  expect((await stop()).block).toBeUndefined() // reader failed
})

test('the off switch (the default) and solo mode register nothing', async ($, on) => {
  bottom(on)
  mockFs(on, {})
  mockWorld(on, { env: ENV, process: proc(statusLine()) })
  expect((await $.classic.Stop({ stop_hook_active: false, cwd: PROJECT_DIR })).block).toBeUndefined()
})

test('solo mode passes even with the switch on', { options: { stopGuard: true, mode: 'solo' } }, async ($, on) => {
  bottom(on)
  mockFs(on, {})
  mockWorld(on, { env: ENV, process: proc(statusLine()) })
  expect((await $.classic.Stop({ stop_hook_active: false, cwd: PROJECT_DIR })).block).toBeUndefined()
})
