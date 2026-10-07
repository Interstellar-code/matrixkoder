// MatrixKoder guards (plan §6): tool.call deny list for destructive git and
// project-banned commands. Wired in by hooks/register.tsx in a later phase.
// A tripwire against plausible model-written commands, not a sandbox.
import type { On, PluginOptions, ToolCallResult } from 'claude-code'

type Ban = { cmd: string; why: string }

const MAX_DEPTH = 3 // nesting levels scanned: `bash -c`, eval, $( ) / backticks inside "…"
const SEPARATORS = new Set([';', '\n', '&', '|', '(', ')', '`']) // && || |& ;; and subshells fall out of these
const base = (w: string) => w.slice(w.lastIndexOf('/') + 1)

/** Index of the `)` closing a `$(` whose body starts at i (escapes skipped, parens counted); s.length if unclosed. */
function closeParen(s: string, i: number): number {
  // ponytail: a quoted `)` inside the body miscounts; quote-aware matching if that ever shows up
  for (let depth = 1; i < s.length; i++) {
    if (s[i] === '\\') i++
    else if (s[i] === '(') depth++
    else if (s[i] === ')' && --depth === 0) return i
  }
  return s.length
}

/**
 * Shell tokenizer: the simple commands of `s` as unquoted words. '…', "…" and `\` escapes are honoured,
 * so a separator inside quotes never splits; an unquoted `#` word starts a comment. Unquoted `$( )`,
 * backticks and `( )` split like separators (their insides are commands of `s`); `$( )` and backticks
 * inside "…" go to `subs` for the caller to scan.
 */
function tokenize(s: string, subs: string[]): string[][] {
  const out: string[][] = []
  let ws: string[] = []
  let w = ''
  let inWord = false
  const endWord = () => {
    if (inWord) ws.push(w)
    w = ''
    inWord = false
  }
  for (let i = 0; i < s.length; i++) {
    const c = s[i]
    if (c === '\\') {
      if (s[i + 1] !== '\n') {
        w += s[i + 1] ?? ''
        inWord = true
      }
      i++
    } else if (c === "'") {
      const j = s.indexOf("'", i + 1)
      const end = j < 0 ? s.length : j
      w += s.slice(i + 1, end)
      inWord = true
      i = end
    } else if (c === '"') {
      inWord = true
      for (i++; i < s.length && s[i] !== '"'; i++) {
        if (s[i] === '\\' && '"\\$`'.includes(s[i + 1] ?? '')) w += s[++i]
        else if (s[i] === '\\' && s[i + 1] === '\n') i++
        else if (s[i] === '`') {
          const j = s.indexOf('`', i + 1)
          const end = j < 0 ? s.length : j
          subs.push(s.slice(i + 1, end))
          i = end
        } else if (s[i] === '$' && s[i + 1] === '(') {
          const end = closeParen(s, i + 2)
          subs.push(s.slice(i + 2, end))
          i = end
        } else w += s[i]
      }
    } else if (c === '#' && !inWord) {
      const j = s.indexOf('\n', i)
      i = j < 0 ? s.length : j - 1
    } else if (c === '&' && inWord && /[<>]$/.test(w)) {
      w += c // `2>&1`, `<&3`: a redirection, not a separator
    } else if (SEPARATORS.has(c)) {
      endWord()
      if (ws.length) out.push(ws)
      ws = []
    } else if (/\s/.test(c)) {
      endWord()
    } else {
      w += c
      inWord = true
    }
  }
  endWord()
  if (ws.length) out.push(ws)
  return out
}

// wrapper → its options that take a value (skipped along with it)
const WRAPPERS: Readonly<Record<string, readonly string[]>> = {
  env: ['-u', '--unset', '-C', '--chdir'],
  sudo: ['-u', '-g', '-C', '-D', '-h', '-p', '-r', '-t', '-U', '-R'],
  nice: ['-n', '--adjustment'],
  time: ['-f', '--format', '-o', '--output'],
  xargs: ['-a', '-d', '-E', '-I', '-L', '-n', '-P', '-s', '--arg-file', '--delimiter', '--max-args', '--max-procs'],
  exec: ['-a'],
  command: [],
  builtin: [],
  nohup: [],
}

/** Drop leading `VAR=val`, `{`, `!` and wrapper commands with their options: the words of what really runs. */
function stripPrefixes(ws: readonly string[]): readonly string[] {
  let i = 0
  while (i < ws.length) {
    const w = ws[i]
    if (w === '{' || w === '!' || /^[A-Za-z_]\w*=/.test(w)) {
      i++
      continue
    }
    const valued = WRAPPERS[base(w)]
    if (!valued) break
    for (i++; i < ws.length && ws[i].startsWith('-'); i++) if (valued.includes(ws[i])) i++
  }
  return ws.slice(i)
}

/** The script of `bash|sh|zsh|dash|ksh … -c <script>` or `eval <words>`, else undefined. */
function innerScript(ws: readonly string[]): string | undefined {
  const cmd = base(ws[0] ?? '')
  if (cmd === 'eval') return ws.slice(1).join(' ')
  if (!['bash', 'sh', 'zsh', 'dash', 'ksh'].includes(cmd)) return undefined
  let hasC = false
  for (let i = 1; i < ws.length; i++) {
    const w = ws[i]
    if (['-o', '+o', '-O', '+O', '--rcfile', '--init-file'].includes(w)) i++
    else if (/^[-+]/.test(w)) hasC ||= /^-[a-zA-Z]*c/.test(w)
    else return hasC ? w : undefined
  }
  return undefined
}

const GIT_UNDO = new Set(['checkout', 'stash', 'reset', 'clean'])
const GIT_VALUED = new Set(['-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env', '--super-prefix'])
const COMMIT_VALUED = new Set([
  '--message', '--file', '--author', '--date', '--template', '--reuse-message', '--reedit-message',
  '--fixup', '--squash', '--trailer', '--cleanup',
])
const shortHas = (w: string, letter: string) => new RegExp(`^-[a-zA-Z]*${letter}`).test(w)
const optsOf = (rest: readonly string[]) => {
  const end = rest.indexOf('--')
  return (end < 0 ? rest : rest.slice(0, end)).filter(w => w.startsWith('-'))
}

function gitBan(ws: readonly string[]): Ban | undefined {
  if (base(ws[0] ?? '') !== 'git') return undefined
  let i = 1
  while (i < ws.length && ws[i].startsWith('-')) i += GIT_VALUED.has(ws[i]) ? 2 : 1
  const sub = ws[i]
  const rest = ws.slice(i + 1)
  if (sub === undefined) return undefined
  if (GIT_UNDO.has(sub)) return { cmd: `git ${sub}`, why: 'destructive git undo' }
  if (sub === 'restore') {
    // --staged alone only unstages; anything touching the worktree discards edits
    const opts = optsOf(rest)
    const staged = opts.some(w => w === '--staged' || shortHas(w, 'S'))
    const worktree = opts.some(w => w === '--worktree' || shortHas(w, 'W'))
    return staged && !worktree ? undefined : { cmd: 'git restore', why: 'destructive git undo' }
  }
  if (sub === 'switch') {
    const f = optsOf(rest).find(w => w === '--force' || w === '--discard-changes' || shortHas(w, 'f'))
    return f ? { cmd: `git switch ${f}`, why: 'discards worktree edits' } : undefined
  }
  if (sub === 'commit') {
    for (let j = 0; j < rest.length && rest[j] !== '--'; j++) {
      const w = rest[j]
      if (/^--am(e(nd?)?)?$/.test(w)) return { cmd: 'git commit --amend', why: 'history rewrite' }
      if (COMMIT_VALUED.has(w) || /^-[a-zA-Z]*[mFCct]$/.test(w)) j++ // skip the message/value: `-m '--amend'`
    }
    return undefined
  }
  if (sub === 'push') {
    const f = rest.find(w => w.startsWith('--force') || shortHas(w, 'f'))
    if (f) return { cmd: `git push ${f}`, why: 'force push' }
    const plus = rest.find(w => w.startsWith('+'))
    if (plus) return { cmd: `git push ${plus}`, why: 'force push' }
    // deletes or overwrites remote refs: --delete/-d, `:ref` (empty source), --mirror, --prune
    const del = rest.find(w => ['--delete', '--mirror', '--prune'].includes(w) || shortHas(w, 'd') || /^:./.test(w))
    if (del) return { cmd: `git push ${del}`, why: 'deletes remote refs' }
    return undefined
  }
  if (sub === 'branch') {
    const opts = optsOf(rest)
    const del = opts.some(w => w === '--delete' || shortHas(w, 'd'))
    const force = opts.some(w => w === '--force' || shortHas(w, 'f'))
    if (opts.some(w => shortHas(w, 'D')) || (del && force)) return { cmd: 'git branch -D', why: 'drops unmerged commits' }
    return undefined
  }
  if (sub === 'rebase') {
    // --abort/--continue/--skip end a rebase already running; blocking them would strand the repo
    const ends = rest.some(w => ['--abort', '--continue', '--skip'].includes(w))
    return ends ? undefined : { cmd: 'git rebase', why: 'history rewrite' }
  }
  if (sub === 'pull') {
    const r = optsOf(rest).find(w => w === '--rebase' || /^--rebase=(?!false$|no$)/.test(w) || shortHas(w, 'r'))
    return r ? { cmd: `git pull ${r}`, why: 'history rewrite' } : undefined
  }
  if (sub === 'reflog' && (rest[0] === 'expire' || rest[0] === 'delete'))
    return { cmd: `git reflog ${rest[0]}`, why: 'drops recovery points' }
  if (sub === 'update-ref' && optsOf(rest).some(w => shortHas(w, 'd')))
    return { cmd: 'git update-ref -d', why: 'deletes a ref' }
  if (sub === 'gc') {
    const p = optsOf(rest).find(w => w === '--prune' || w.startsWith('--prune='))
    return p ? { cmd: `git gc ${p}`, why: 'drops unreachable objects' } : undefined
  }
  return undefined
}

/** A command's short flag letters, other flags and positional words; rm's -R/--recursive/--force fold to r/f. */
function shape(ws: readonly string[]) {
  const rm = base(ws[0] ?? '') === 'rm'
  const RM_LONG: Readonly<Record<string, string>> = { '--recursive': 'r', '--force': 'f' }
  const letters = new Set<string>()
  const longs = new Set<string>()
  const pos: string[] = []
  for (const w of ws.slice(1)) {
    if (rm && RM_LONG[w]) letters.add(RM_LONG[w])
    else if (/^-[a-zA-Z]+$/.test(w)) for (const l of w.slice(1)) letters.add(rm && l === 'R' ? 'r' : l)
    else if (w.startsWith('-')) longs.add(w)
    else pos.push(w)
  }
  return { letters, longs, pos }
}

/** A banned item matches when the command name matches, its flags all appear (any order or split: `rm -r -f`
 * hits `rm -rf`) and its other words lead the command's positional words. */
function bannedHit(ws: readonly string[], banned: readonly string[]): Ban | undefined {
  if (!ws.length) return undefined
  const got = shape(ws)
  const hit = banned.find(c => {
    const cs = c.split(/\s+/).filter(Boolean)
    if (!cs.length || (cs[0].includes('/') ? ws[0] !== cs[0] : base(ws[0]) !== cs[0])) return false
    const want = shape(cs)
    return (
      [...want.letters].every(l => got.letters.has(l)) &&
      [...want.longs].every(l => got.longs.has(l)) &&
      want.pos.every((w, i) => got.pos[i] === w)
    )
  })
  return hit ? { cmd: hit, why: 'banned in matrixkoder.yaml' } : undefined
}

/** First ban among the commands of `command`, nested scripts included down to MAX_DEPTH. */
function check(command: string, banned: readonly string[], depth = 0): Ban | undefined {
  const subs: string[] = []
  for (const raw of tokenize(command, subs)) {
    const ws = stripPrefixes(raw)
    const ban = gitBan(ws) ?? bannedHit(ws, banned)
    if (ban) return ban
    const inner = depth < MAX_DEPTH ? innerScript(ws) : undefined
    const nested = inner === undefined ? undefined : check(inner, banned, depth + 1)
    if (nested) return nested
  }
  if (depth < MAX_DEPTH) {
    for (const sub of subs) {
      const nested = check(sub, banned, depth + 1)
      if (nested) return nested
    }
  }
  return undefined
}

/** One YAML scalar: "…" (with \ escapes), '…' ('' = '), or plain text up to a ` #` comment. */
function scalar(s: string): string {
  const t = s.trim()
  const dq = t.match(/^"((?:[^"\\]|\\.)*)"/)
  if (dq) return dq[1].replace(/\\(.)/g, '$1')
  const sq = t.match(/^'((?:[^']|'')*)'/)
  if (sq) return sq[1].replace(/''/g, "'")
  return t.replace(/(^|\s)#.*$/, '').trim()
}

/** `banned_commands:` from matrixkoder.yaml: a block list of `- item` lines, or a one-line flow list `[a, "b"]`. */
function parseBannedCommands(yaml: string): string[] {
  const out: string[] = []
  let inList = false
  for (const raw of yaml.split(/\r?\n/)) {
    const line = raw.trim()
    if (!inList) {
      const m = line.match(/^banned_commands:\s*(.*)$/)
      if (!m) continue
      const flow = m[1].match(/^\[(.*)\]/)
      if (flow) {
        // ponytail: one-line flow list only; a multi-line one reads as empty
        for (const item of flow[1].matchAll(/"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^,]+/g)) {
          const v = scalar(item[0])
          if (v) out.push(v)
        }
        return out
      }
      inList = true
      continue
    }
    if (!line || line.startsWith('#')) continue // blank and comment lines stay inside the list
    if (line !== '-' && !line.startsWith('- ')) break // first other line ends it
    const v = scalar(line.slice(1))
    if (v) out.push(v)
  }
  return out
}

function deny(cmd: string, why: string): ToolCallResult {
  return { deny: `MatrixKoder guard: ${cmd} is banned (${why}). Undo by an inverse edit instead.` }
}

export function registerGuards(on: On, options: PluginOptions): void {
  if (options.guards === false) return // kill switch: userConfig `guards`, default true
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    // banned_commands: project data dir = nearest ancestor of the session cwd holding `.matrixkoder/`.
    // Any failure here only drops banned_commands; it must never skip the git bans below.
    let banned: string[] = []
    let yamlPath: string | undefined
    try {
      for (let dir = await $.session.cwd(); dir; dir = dir.slice(0, dir.lastIndexOf('/'))) {
        const st = await $.fs.stat(`${dir}/.matrixkoder`).catch(() => undefined)
        if (st?.kind === 'dir') {
          yamlPath = `${dir}/.matrixkoder/matrixkoder.yaml`
          break
        }
      }
      if (yamlPath) banned = parseBannedCommands(await $.fs.read(yamlPath))
    } catch {
      try {
        // a missing yaml means none; an unreadable one is said, not swallowed
        if (yamlPath && (await $.fs.exists(yamlPath)))
          $.ui.log(`MatrixKoder guard: cannot read ${yamlPath}; banned_commands not enforced`)
      } catch {}
    }
    const ban = check(e.command, banned)
    return ban ? deny(ban.cmd, ban.why) : next(e)
  })
}
