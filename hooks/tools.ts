// MatrixKoder model tools: mk_state and mk_usage (strict types)
import type { On, PluginOptions, ToolCallResult } from 'claude-code'

const ALLOWED_SUBCOMMANDS = new Set([
  'run',
  'task',
  'event',
  'wait',
  'lock',
  'render',
])

// --data/--by/--run steer mk-state at another project or run; the model tool must not pass them in
// any form: exact, --flag=value, or an abbreviation like --b=x (argparse prefix matching is off,
// but reject here too so the process never runs)
const OVERRIDE_FLAGS = ['--data', '--by', '--run']

const flagName = (arg: string): string => arg.split('=', 1)[0] ?? arg

const isOverrideFlag = (arg: string): boolean => {
  const name = flagName(arg)
  if (!name.startsWith('--') || name.length <= 2) return false
  return OVERRIDE_FLAGS.some((b) => name === b || b.startsWith(name) || name.startsWith(b))
}

export function registerTools(on: On, _options: PluginOptions): void {
  on('session.start', { cwd: /.*/ }, async ($, e, next) => {
    try {
      await $.tool.register({
        name: 'mk_state',
        description:
          'Execute MatrixKoder state operations (run, task, event, wait, lock, render). Pass command arguments as an array of strings in args.',
        inputSchema: {
          type: 'object',
          properties: {
            args: {
              type: 'array',
              items: { type: 'string' },
              description: 'mk-state.py arguments, e.g. ["task", "set", "F6", "status=review"]',
            },
          },
          required: ['args'],
        },
      })

      await $.tool.register({
        name: 'mk_usage',
        description: 'Refresh and return MatrixKoder usage ledger summary for all agents.',
        inputSchema: {
          type: 'object',
        },
      })
    } catch {
      // tool registration refused or unready
    }

    return next(e)
  })

  on('tool.call', async ($, e, next): Promise<ToolCallResult> => {
    const tool: string = e.tool
    const pluginName = $.plugin.name
    const isMkState = tool === 'mk_state' || tool === `mcp__${pluginName}__mk_state`
    const isMkUsage = tool === 'mk_usage' || tool === `mcp__${pluginName}__mk_usage`

    if (!isMkState && !isMkUsage) {
      return next(e)
    }

    const pluginRoot = $.plugin.root
    let cwd = '.'
    try {
      cwd = await $.session.cwd()
    } catch {
      // fallback
    }

    if (isMkState) {
      const inputObj = e as Record<string, unknown>
      const nestedInput = inputObj.input as Record<string, unknown> | undefined
      const argsRaw = inputObj.args ?? nestedInput?.args
      if (!Array.isArray(argsRaw) || argsRaw.length === 0) {
        return {
          isError: true,
          result: 'mk_state: requires non-empty string[] in args',
        }
      }

      const firstArg = String(argsRaw[0])
      if (!ALLOWED_SUBCOMMANDS.has(firstArg)) {
        return {
          isError: true,
          result: `mk_state: disallowed subcommand '${firstArg}'. Allowed: ${Array.from(ALLOWED_SUBCOMMANDS).join(', ')}`,
        }
      }

      const hasBannedOverride = argsRaw.some(arg => isOverrideFlag(String(arg)))
      if (hasBannedOverride) {
        return {
          isError: true,
          result: 'mk_state: overrides --data, --by, and --run are not allowed from model tools',
        }
      }

      // task set --force bypasses the dep gate; only the orchestrator (shell) may pass it
      if (firstArg === 'task' && String(argsRaw[1] ?? '') === 'set' &&
          argsRaw.some(a => flagName(String(a)) === '--force')) {
        return {
          isError: true,
          result: 'mk_state: task set --force is not allowed from model tools',
        }
      }

      if (firstArg === 'lock') {
        const sub = String(argsRaw[1] ?? '')
        if (sub === 'release' && argsRaw.some(a => flagName(String(a)) === '--force')) {
          return {
            isError: true,
            result: 'mk_state: lock release --force is not allowed from model tools',
          }
        }
        if (sub === 'take' && argsRaw.some(a => flagName(String(a)) === '--force-stale')) {
          return {
            isError: true,
            result: 'mk_state: lock take --force-stale is not allowed from model tools',
          }
        }
      }

      const scriptPath = `${pluginRoot}/scripts/mk-state.py`
      try {
        const proc = await $.process.run(['python3', scriptPath, ...argsRaw.map(String)], {
          cwd,
          timeoutMs: 30000,
        })
        if (proc.isStdoutTruncated || proc.isStderrTruncated) {
          return {
            isError: true,
            result: 'mk_state error: process output truncated (exceeded 4 MiB limit)',
          }
        }
        if (proc.exitCode === 0) {
          return { result: proc.stdout.trim() }
        }
        return {
          isError: true,
          result: (proc.stderr || proc.stdout || `Process exited with code ${proc.exitCode}`).trim(),
        }
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err)
        return {
          isError: true,
          result: `mk_state process error: ${msg}`,
        }
      }
    }

    if (isMkUsage) {
      const scriptPath = `${pluginRoot}/scripts/mk-usage.py`
      try {
        const proc = await $.process.run(['python3', scriptPath, '--once'], {
          cwd,
          timeoutMs: 30000,
        })
        if (proc.isStdoutTruncated || proc.isStderrTruncated) {
          return {
            isError: true,
            result: 'mk_usage error: process output truncated (exceeded 4 MiB limit)',
          }
        }
        if (proc.exitCode === 0) {
          return { result: proc.stdout.trim() }
        }
        return {
          isError: true,
          result: (proc.stderr || proc.stdout || `Process exited with code ${proc.exitCode}`).trim(),
        }
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err)
        return {
          isError: true,
          result: `mk_usage process error: ${msg}`,
        }
      }
    }

    return next(e)
  })
}
