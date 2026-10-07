// Tests for MatrixKoder model tools: mk_state and mk_usage
import { test, expect } from 'claude-code/testing'

test('allowed subcommand runs and returns JSON', async ($, on) => {
  on('process.run', (_$, e) => {
    return {
      value: {
        exitCode: 0,
        stdout: JSON.stringify({ ok: true, runs: ['mk-p1'] }),
        stderr: '',
      },
    }
  })

  const ran = await $.tool.call({
    tool: 'mcp__matrixkoder__mk_state',
    args: ['run', 'list', '--data', '/tmp/test-data'],
  } as any)

  expect(ran.isError).toBeUndefined()
  expect(typeof ran.result).toBe('string')
  const parsed = JSON.parse(ran.result as string)
  expect(parsed.ok).toBe(true)
  expect(parsed.runs).toEqual(['mk-p1'])
})

test('disallowed first arg rejected without running process', async ($, on) => {
  let processCalled = false
  on('process.run', (_$, e) => {
    processCalled = true
    return { value: { exitCode: 0, stdout: '{}', stderr: '' } }
  })

  for (const badCmd of ['destroy', 'clean', 'rm', 'unknown']) {
    const ran = await $.tool.call({
      tool: 'mcp__matrixkoder__mk_state',
      args: [badCmd],
    } as any)

    expect(ran.isError).toBe(true)
    expect(String(ran.result)).toContain(`disallowed subcommand '${badCmd}'`)
  }
  expect(processCalled).toBe(false)
})

test('empty or non-array args rejected', async ($) => {
  const emptyArgs = await $.tool.call({
    tool: 'mcp__matrixkoder__mk_state',
    args: [],
  } as any)
  expect(emptyArgs.isError).toBe(true)
  expect(String(emptyArgs.result)).toContain('requires non-empty string[] in args')
})

test('process error surfaced on non-zero exit', async ($, on) => {
  on('process.run', (_$, e) => {
    return {
      value: {
        exitCode: 2,
        stdout: '',
        stderr: 'Error: invalid task specification',
      },
    }
  })

  const ran = await $.tool.call({
    tool: 'mcp__matrixkoder__mk_state',
    args: ['task', 'set', 'invalid'],
  } as any)

  expect(ran.isError).toBe(true)
  expect(String(ran.result)).toContain('invalid task specification')
})

test('mk_usage runs with --once and returns summary line', async ($, on) => {
  let sawArgs: readonly string[] = []
  on('process.run', (_$, e) => {
    sawArgs = e.argv
    return {
      value: {
        exitCode: 0,
        stdout: JSON.stringify({ agents: { claude: { status: 'ok' } } }),
        stderr: '',
      },
    }
  })

  const ran = await $.tool.call({
    tool: 'mcp__matrixkoder__mk_usage',
  } as any)

  expect(ran.isError).toBeUndefined()
  expect(sawArgs).toContain('--once')
  const parsed = JSON.parse(ran.result as string)
  expect(parsed.agents.claude.status).toBe('ok')
})

test('tool registrations succeed on session.start', async ($, on) => {
  const registered: string[] = []
  on('tool.register', (_$, e, next) => {
    registered.push(e.name)
    return { value: { tool: `mcp__matrixkoder__${e.name}` } }
  })
  on('session.start', (_$, e, next) => ({ cwd: '/tmp' }))

  await $.session.start({ cwd: '/tmp' })
  expect(registered).toContain('mk_state')
  expect(registered).toContain('mk_usage')
})
