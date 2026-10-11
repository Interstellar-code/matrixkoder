// Tests for MatrixKoder model tools: mk_state, mk_usage and mk_bus
import { test, expect, mock } from 'claude-code/testing'
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

test('allows values that start with a banned flag name but contain whitespace', async ($, on) => {
  let processCalled = false
  const capturedArgs: Array<readonly string[]> = []
  on('process.run', (_$, e) => {
    processCalled = true
    capturedArgs.push(e.argv)
    return { value: procResult({ stdout: '{}' }) }
  })

  for (const args of [
    ['event', '--type', 'note', '--text', '--run tests failed'],
    ['event', '--type', 'note', '--text', '--by design'],
    ['event', '--type', 'note', '--text', '--data is messy'],
    ['event', '--type', 'note', '--text', ' --run tests failed'],
  ]) {
    const ran = await callTool($, { tool: 'mcp__matrixkoder__mk_state', args })
    expect(ran.isError).toBeUndefined()
  }
  expect(processCalled).toBe(true)
  expect(capturedArgs.some(argv => argv.includes('--run tests failed'))).toBe(true)
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
  expect(registered).toContain('mk_bus')
})

test('mk_bus pending runs mk-bus.py with --data and returns JSON', async ($, on) => {
  mock.env(on, { MATRIXKODER_DATA: '/data' })
  let sawArgs: readonly string[] = []
  on('process.run', (_$, e) => {
    sawArgs = e.argv
    return {
      value: procResult({
        exitCode: 0,
        stdout: JSON.stringify({ ok: true, pending: [] }),
        stderr: '',
      }),
    }
  })

  const ran = await callTool($, {
    tool: 'mcp__matrixkoder__mk_bus',
    action: 'pending',
  })

  expect(ran.isError).toBeUndefined()
  expect(sawArgs[0]).toBe('python3')
  expect(sawArgs[1]).toContain('scripts/mk-bus.py')
  expect(sawArgs).toContain('pending')
  expect(sawArgs[sawArgs.indexOf('--data') + 1]).toBe('/data')
  const parsed = JSON.parse(ran.result as string)
  expect(parsed.ok).toBe(true)
  expect(parsed.pending).toEqual([])
})

test('mk_bus answer with run builds the exact argv', async ($, on) => {
  mock.env(on, { MATRIXKODER_DATA: '/data' })
  let sawArgs: readonly string[] = []
  on('process.run', (_$, e) => {
    sawArgs = e.argv
    return { value: procResult({ exitCode: 0, stdout: '{"ok":true}' }) }
  })

  const ran = await callTool($, {
    tool: 'mcp__matrixkoder__mk_bus',
    action: 'answer',
    msg_id: 'm-5',
    text: 'use fixture v3',
    run: 'r1',
  })

  expect(ran.isError).toBeUndefined()
  expect(sawArgs[1]).toContain('scripts/mk-bus.py')
  expect(sawArgs).toEqual(['python3', sawArgs[1], 'answer', '--data', '/data', '--run', 'r1', '--', 'm-5', 'use fixture v3'])
})

test('mk_bus answer passes option-shaped text after -- so it stays positional', async ($, on) => {
  // review MEDIUM 1: a space-free text starting with "-" is option-shaped for argparse —
  // without the "--" terminator "--run=evil" was swallowed as the --run flag (text lost),
  // and "--data=/x" would redirect the answer to another data dir. The argv must carry
  // "--" before the positionals, and the text must appear after it verbatim.
  mock.env(on, { MATRIXKODER_DATA: '/data' })
  const seen: string[][] = []
  on('process.run', (_$, e) => {
    seen.push([...e.argv])
    return { value: procResult({ exitCode: 0, stdout: '{"ok":true}' }) }
  })

  for (const hostile of ['--run=evil', '-x']) {
    const ran = await callTool($, {
      tool: 'mcp__matrixkoder__mk_bus',
      action: 'answer',
      msg_id: 'm-3',
      text: hostile,
    })

    expect(ran.isError).toBeUndefined()
    const argv = seen[seen.length - 1] ?? []
    expect(argv.length, 'the process must have run').toBeGreaterThan(0)
    const dd = argv.indexOf('--')
    expect(dd, `argv must contain -- before the positionals: ${argv.join(' ')}`).toBeGreaterThan(0)
    expect(argv.slice(dd)).toEqual(['--', 'm-3', hostile])
  }
})

test('mk_bus answer rejects bad msg_id without running the process', async ($, on) => {
  mock.env(on, { MATRIXKODER_DATA: '/data' })
  let processCalled = false
  on('process.run', (_$, e) => {
    processCalled = true
    return { value: procResult({ stdout: '{}' }) }
  })

  for (const badMsgId of ['m-x', 5, undefined]) {
    const input: { tool: string; action: string; msg_id?: unknown; text: string } = {
      tool: 'mcp__matrixkoder__mk_bus',
      action: 'answer',
      text: 'hi',
    }
    if (badMsgId !== undefined) input.msg_id = badMsgId
    const ran = await callTool($, input)
    expect(ran.isError).toBe(true)
    expect(String(ran.result)).toContain('mk_bus:')
  }
  expect(processCalled).toBe(false)
})

test('mk_bus answer rejects text over 64 KiB without running the process', async ($, on) => {
  mock.env(on, { MATRIXKODER_DATA: '/data' })
  let processCalled = false
  on('process.run', (_$, e) => {
    processCalled = true
    return { value: procResult({ stdout: '{}' }) }
  })

  const ran = await callTool($, {
    tool: 'mcp__matrixkoder__mk_bus',
    action: 'answer',
    msg_id: 'm-5',
    text: 'a'.repeat(65537),
  })

  expect(ran.isError).toBe(true)
  expect(String(ran.result)).toContain('64 KiB')
  expect(processCalled).toBe(false)
})

test('mk_bus rejects missing/unknown action and bad run without running the process', async ($, on) => {
  mock.env(on, { MATRIXKODER_DATA: '/data' })
  let processCalled = false
  on('process.run', (_$, e) => {
    processCalled = true
    return { value: procResult({ stdout: '{}' }) }
  })

  const noAction = await callTool($, { tool: 'mcp__matrixkoder__mk_bus' })
  expect(noAction.isError).toBe(true)
  expect(String(noAction.result)).toContain('action')

  const badAction = await callTool($, { tool: 'mcp__matrixkoder__mk_bus', action: 'purge' })
  expect(badAction.isError).toBe(true)
  expect(String(badAction.result)).toContain('action')

  const badRun = await callTool($, { tool: 'mcp__matrixkoder__mk_bus', action: 'pending', run: '..evil' })
  expect(badRun.isError).toBe(true)

  expect(processCalled).toBe(false)
})

test('mk_bus surfaces non-zero exit with the script error', async ($, on) => {
  mock.env(on, { MATRIXKODER_DATA: '/data' })
  on('process.run', (_$, e) => {
    return {
      value: procResult({
        exitCode: 2,
        stdout: JSON.stringify({ ok: false, error: 'bus down' }),
        stderr: '',
      }),
    }
  })

  const ran = await callTool($, {
    tool: 'mcp__matrixkoder__mk_bus',
    action: 'pending',
  })

  expect(ran.isError).toBe(true)
  expect(String(ran.result)).toContain('bus down')
})
