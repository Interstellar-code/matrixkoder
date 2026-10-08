// Tests for MatrixKoder model tools: mk_state and mk_usage
import { test, expect } from 'claude-code/testing'
import type { ProcessRunResult, ToolCallArgs } from 'claude-code'

const procResult = (overrides: Partial<ProcessRunResult> = {}): ProcessRunResult => ({
  exitCode: 0,
  stdout: '',
  stderr: '',
  isStdoutTruncated: false,
  isStderrTruncated: false,
  ...overrides,
})

const callTool = ($: { tool: { call: (input: ToolCallArgs) => Promise<any> } }, input: { tool: string; [k: string]: unknown }) =>
  $.tool.call(input as unknown as ToolCallArgs)

test('allowed subcommand runs and returns JSON', async ($, on) => {
  const capturedRuns: Array<{ readonly argv: readonly string[]; readonly init?: { readonly timeoutMs?: number } }> = []
  on('process.run', (_$, e) => {
    capturedRuns.push(e)
    return {
      value: procResult({
        exitCode: 0,
        stdout: JSON.stringify({ ok: true, runs: ['mk-p1'] }),
        stderr: '',
      }),
    }
  })

  const ran = await callTool($, {
    tool: 'mcp__matrixkoder__mk_state',
    args: ['run', 'list'],
  })

  expect(ran.isError).toBeUndefined()
  expect(typeof ran.result).toBe('string')
  const parsed = JSON.parse(ran.result as string)
  expect(parsed.ok).toBe(true)
  expect(parsed.runs).toEqual(['mk-p1'])

  expect(capturedRuns.length).toBe(1)
  const capturedRun = capturedRuns[0]
  expect(capturedRun?.argv[0]).toBe('python3')
  expect(capturedRun?.argv[1]).toContain('scripts/mk-state.py')
  expect(capturedRun?.argv.slice(2)).toEqual(['run', 'list'])
  expect(capturedRun?.init?.timeoutMs).toBe(30000)
})

test('disallowed first arg rejected without running process', async ($, on) => {
  let processCalled = false
  on('process.run', (_$, e) => {
    processCalled = true
    return { value: procResult({ stdout: '{}' }) }
  })

  for (const badCmd of ['destroy', 'clean', 'rm', 'unknown', '--data', '-h']) {
    const ran = await callTool($, {
      tool: 'mcp__matrixkoder__mk_state',
      args: [badCmd],
    })

    expect(ran.isError).toBe(true)
    expect(String(ran.result)).toContain(`disallowed subcommand '${badCmd}'`)
  }
  expect(processCalled).toBe(false)
})

test('rejects --data, --by, and --run overrides', async ($, on) => {
  let processCalled = false
  on('process.run', (_$, e) => {
    processCalled = true
    return { value: procResult({ stdout: '{}' }) }
  })

  for (const badArg of ['--data=/tmp/evil', '--data', '--by=attacker', '--by', '--run=other', '--run']) {
    const ran = await callTool($, {
      tool: 'mcp__matrixkoder__mk_state',
      args: ['task', 'set', 'F6', badArg],
    })

    expect(ran.isError).toBe(true)
    expect(String(ran.result)).toContain('overrides --data, --by, and --run are not allowed')
  }
  expect(processCalled).toBe(false)
})

test('rejects task set --force (dep gate bypass)', async ($, on) => {
  let processCalled = false
  on('process.run', (_$, e) => {
    processCalled = true
    return { value: procResult({ stdout: '{}' }) }
  })

  for (const args of [
    ['task', 'set', 'F6', 'status=dispatched', '--force'],
    ['task', 'set', 'F6', '--force', 'status=dispatched'],
    ['task', 'set', 'F6', 'status=dispatched', '--force=yes'],
  ]) {
    const ran = await callTool($, { tool: 'mcp__matrixkoder__mk_state', args })
    expect(ran.isError).toBe(true)
    expect(String(ran.result)).toContain('task set --force is not allowed')
  }

  const plain = await callTool($, {
    tool: 'mcp__matrixkoder__mk_state',
    args: ['task', 'set', 'F6', 'status=routed'],
  })
  expect(plain.isError).toBeUndefined()
  expect(processCalled).toBe(true)
})

test('rejects abbreviated and --flag=value forms of override flags', async ($, on) => {
  let processCalled = false
  on('process.run', (_$, e) => {
    processCalled = true
    return { value: procResult({ stdout: '{}' }) }
  })

  for (const badArg of ['--b=x', '--by=x', '--ru=x', '--run=x', '--dat=x', '--data=x', '--b', '--ru']) {
    const ran = await callTool($, {
      tool: 'mcp__matrixkoder__mk_state',
      args: ['event', '--type', 'note', '--text', 'x', badArg],
    })

    expect(ran.isError).toBe(true)
    expect(String(ran.result)).toContain('overrides --data, --by, and --run are not allowed')
  }
  expect(processCalled).toBe(false)
})

test('rejects lock release --force and lock take --force-stale', async ($, on) => {
  let processCalled = false
  on('process.run', (_$, e) => {
    processCalled = true
    return { value: procResult({ stdout: '{}' }) }
  })

  const ran1 = await callTool($, {
    tool: 'mcp__matrixkoder__mk_state',
    args: ['lock', 'release', 'dev-server', '--force'],
  })
  expect(ran1.isError).toBe(true)
  expect(String(ran1.result)).toContain('lock release --force is not allowed')

  const ran2 = await callTool($, {
    tool: 'mcp__matrixkoder__mk_state',
    args: ['lock', 'take', 'dev-server', '--force-stale'],
  })
  expect(ran2.isError).toBe(true)
  expect(String(ran2.result)).toContain('lock take --force-stale is not allowed')

  const ran3 = await callTool($, {
    tool: 'mcp__matrixkoder__mk_state',
    args: ['lock', 'take', 'dev-server', '--reason', 'r', '--force-stale=1'],
  })
  expect(ran3.isError).toBe(true)
  expect(String(ran3.result)).toContain('lock take --force-stale is not allowed')

  const ran4 = await callTool($, {
    tool: 'mcp__matrixkoder__mk_state',
    args: ['lock', 'release', 'dev-server', '--force=1'],
  })
  expect(ran4.isError).toBe(true)
  expect(String(ran4.result)).toContain('lock release --force is not allowed')

  expect(processCalled).toBe(false)
})

test('tool matches exact registered name, ignores foreign suffix matches', async ($, on) => {
  let processCalled = false
  on('process.run', (_$, e) => {
    processCalled = true
    return { value: procResult({ stdout: '{}' }) }
  })
  on('tool.call', (_$, e) => ({ result: 'unhandled-foreign-tool' }))

  // tool from another plugin ending with __mk_state should not be handled by this hook
  const ran = await callTool($, {
    tool: 'mcp__otherplugin__mk_state',
    args: ['run', 'list'],
  })
  expect(processCalled).toBe(false)
  expect(ran.result).toBe('unhandled-foreign-tool')
})

test('empty or non-array args rejected', async ($) => {
  const emptyArgs = await callTool($, {
    tool: 'mcp__matrixkoder__mk_state',
    args: [],
  })
  expect(emptyArgs.isError).toBe(true)
  expect(String(emptyArgs.result)).toContain('requires non-empty string[] in args')
})

test('process error surfaced on non-zero exit', async ($, on) => {
  on('process.run', (_$, e) => {
    return {
      value: procResult({
        exitCode: 2,
        stdout: '',
        stderr: 'Error: invalid task specification',
      }),
    }
  })

  const ran = await callTool($, {
    tool: 'mcp__matrixkoder__mk_state',
    args: ['task', 'set', 'invalid'],
  })

  expect(ran.isError).toBe(true)
  expect(String(ran.result)).toContain('invalid task specification')
})

test('truncation surfaces error result', async ($, on) => {
  on('process.run', (_$, e) => {
    return {
      value: procResult({
        exitCode: 0,
        stdout: '{"partial": true}',
        stderr: '',
        isStdoutTruncated: true,
      }),
    }
  })

  const ran = await callTool($, {
    tool: 'mcp__matrixkoder__mk_state',
    args: ['run', 'list'],
  })

  expect(ran.isError).toBe(true)
  expect(String(ran.result)).toContain('output truncated')
})

test('mk_usage runs with --once and returns summary line', async ($, on) => {
  let sawArgs: readonly string[] = []
  on('process.run', (_$, e) => {
    sawArgs = e.argv
    return {
      value: procResult({
        exitCode: 0,
        stdout: JSON.stringify({ agents: { claude: { status: 'ok' } } }),
        stderr: '',
      }),
    }
  })

  const ran = await callTool($, {
    tool: 'mcp__matrixkoder__mk_usage',
  })

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

  await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
  expect(registered).toContain('mk_state')
  expect(registered).toContain('mk_usage')
})
