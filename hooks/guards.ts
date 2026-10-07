// MatrixKoder guards (plan §6): tool.call deny list for destructive git and
// project-banned commands. Wired in by hooks/register.tsx in a later phase.
import type { On, PluginOptions, ToolCallResult } from 'claude-code'

const GIT_UNDO: Readonly<Record<string, string>> = {
  checkout: 'destructive git undo', // includes `git checkout -b` — the plan bans checkout outright
  stash: 'destructive git undo',
  reset: 'destructive git undo',
  clean: 'destructive git undo',
  restore: 'destructive git undo',
}
const FORCE_PUSH_FLAGS = new Set(['--force', '-f', '--force-with-lease'])
const YAML_PATH = '.matrixkoder/matrixkoder.yaml'

/** Split a shell line into command segments; a quoted string stays inside its segment. */
function segments(command: string): string[] {
  return command.split(/;|&&|\|\||\||\n/).map(s => s.trim()).filter(Boolean)
}

function gitBan(ws: readonly string[]): { cmd: string; why: string } | undefined {
  if (ws[0] !== 'git' || ws.length < 2) return undefined
  const [_, sub, ...rest] = ws
  const undo = GIT_UNDO[sub]
  if (undo) return { cmd: `git ${sub}`, why: undo }
  if (sub === 'commit' && rest.includes('--amend')) return { cmd: 'git commit --amend', why: 'history rewrite' }
  if (sub === 'push') {
    const flag = rest.find(w => FORCE_PUSH_FLAGS.has(w))
    if (flag) return { cmd: `git push ${flag}`, why: 'force push' }
  }
  return undefined
}

/** `banned_commands:` items from matrixkoder.yaml — `- "cmd"`, `- 'cmd'` or `- cmd` lines only. */
function parseBannedCommands(yaml: string): string[] {
  const out: string[] = []
  let inList = false
  for (const raw of yaml.split('\n')) {
    const line = raw.trim()
    if (!inList) {
      if (line.startsWith('banned_commands:')) inList = true
      continue
    }
    const m = line.match(/^-(?:\s+(?:"([^"]*)"|'([^']*)'|(\S.*)))?$/)
    if (!m) break // first non-item line ends the list
    const cmd = (m[1] ?? m[2] ?? m[3] ?? '').trim()
    if (cmd) out.push(cmd)
  }
  return out
}

function wordPrefix(ws: readonly string[], cmd: string): boolean {
  const cs = cmd.trim().split(/\s+/).filter(Boolean)
  return cs.length > 0 && cs.length <= ws.length && cs.every((w, i) => ws[i] === w)
}

function deny(cmd: string, why: string): ToolCallResult {
  return { deny: `MatrixKoder guard: ${cmd} is banned (${why}). Undo by an inverse edit instead.` }
}

export function registerGuards(on: On, options: PluginOptions): void {
  if (options.guards === false) return // kill switch: userConfig `guards`, default true
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    let banned: string[] = []
    try {
      banned = parseBannedCommands(await $.fs.read(YAML_PATH))
    } catch {
      // missing or unreadable yaml → no banned commands
    }
    for (const ws of segments(e.command).map(s => s.split(/\s+/))) {
      const git = gitBan(ws)
      if (git) return deny(git.cmd, git.why)
      const hit = banned.find(c => wordPrefix(ws, c))
      if (hit) return deny(hit, 'banned in matrixkoder.yaml')
    }
    return next(e)
  })
}
