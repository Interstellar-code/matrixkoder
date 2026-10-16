// MatrixKoder guards (plan §6): tool.call deny list for destructive git and
// project-banned commands. Wired in by hooks/register.tsx in a later phase.
// A tripwire against plausible model-written commands, not a sandbox.
import type { On, PluginOptions, ToolCallResult } from 'claude-code'
import { paths } from './paths'

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
    if (c === undefined) continue
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
  // rtk is a CLI proxy: `rtk git …` runs git, `rtk proxy`/`rtk run` run the next command.
  // Global options that take a value are skipped; any other leading `-…` fails closed below.
  rtk: ['-c', '--command'],
}

/** Drop leading `VAR=val`, `{`, `!` and wrapper commands with their options: the words of what really runs. */
function stripPrefixes(ws: readonly string[]): readonly string[] {
  let i = 0
  while (i < ws.length) {
    const w = ws[i]
    if (!w) break
    if (w === '{' || w === '!' || /^[A-Za-z_]\w*=/.test(w)) {
      i++
      continue
    }
    const valued = WRAPPERS[base(w)]
    if (!valued) break
    const isRtk = base(w) === 'rtk'
    for (i++; i < ws.length; i++) {
      const opt = ws[i]
      if (!opt || !opt.startsWith('-')) break
      if (valued.includes(opt)) i++
      else if (isRtk) { i++; break } // unknown rtk flag: drop it, then the normal walk (deny only if a banned git form follows)
    }
    // `rtk proxy`/`rtk run` wrap the next command. `rtk git` stays: it is rtk's git subcommand, same git words.
    if (isRtk && (ws[i] === 'proxy' || ws[i] === 'run')) i++
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
    if (!w) continue
    if (['-o', '+o', '-O', '+O', '--rcfile', '--init-file'].includes(w)) i++
    else if (/^[-+]/.test(w)) hasC ||= /^-[a-zA-Z]*c/.test(w)
    else return hasC ? w : undefined
  }
  return undefined
}

// Read-only stash forms (list, show) are allowed below; all other stash forms are denied.
const GIT_UNDO = new Set(['checkout', 'reset', 'clean'])
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
  while (i < ws.length) {
    const opt = ws[i]
    if (!opt || !opt.startsWith('-')) break
    i += GIT_VALUED.has(opt) ? 2 : 1
  }
  const sub = ws[i]
  const rest = ws.slice(i + 1)
  if (sub === undefined) return undefined
  if (GIT_UNDO.has(sub)) return { cmd: `git ${sub}`, why: 'destructive git undo' }
  if (sub === 'stash') {
    if (rest[0] === 'list' || rest[0] === 'show') return undefined
    return { cmd: 'git stash', why: 'destructive git undo' }
  }
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
    for (let j = 0; j < rest.length; j++) {
      const w = rest[j]
      if (!w || w === '--') break
      if (/^--am(e(nd?)?)?$/.test(w)) return { cmd: 'git commit --amend', why: 'history rewrite' }
      if (COMMIT_VALUED.has(w)) { j++; continue } // long option: the next word is its value
      // Short cluster, walked like git: the first value letter (m F C c t) ends the walk.
      // Last letter → the next word is the value (skip one). More characters, or a space
      // inside the word (`-m"fix test"` arrives as `-mfix test`), → attached value (skip nothing).
      if (w.startsWith('-') && !w.startsWith('--')) {
        const cluster = w.slice(1)
        for (let k = 0; k < cluster.length; k++) {
          const letter = cluster[k]
          if (letter !== undefined && 'mFCct'.includes(letter)) {
            if (k === cluster.length - 1 && !w.includes(' ')) j++
            break
          }
        }
      }
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

const MKDB = /(^|\/)mk\.db(\?|$)/
const SQL_WRITE = /\b(insert|update|delete|drop|alter|create|reindex|replace\s+into)\b|^\s*\.(import|restore)\b/i
const VACUUM_MK = /\bvacuum\s+(\S+\s+)?into\s+['"]?\S*mk\.db/i
const noLiterals = (sql: string) => sql.replace(/'(?:[^']|'')*'|"[^"]*"/g, "''") // a keyword inside a literal is data

/** Raw `sqlite3` that may write an mk.db, as the db argument or as a path inside the SQL (ATTACH, VACUUM INTO).
 * Read-only (`-readonly`, `mode=ro`) and `VACUUM INTO <tmp>` pass. SQL fed by stdin (`<`, heredoc, `piped`) cannot be
 * seen, so it counts as a write on an mk.db. */
function sqliteBan(ws: readonly string[], piped: boolean): Ban | undefined {
  if (base(ws[0] ?? '') !== 'sqlite3') return undefined
  const args = ws.slice(1)
  if (args.some(w => /^--?readonly$/.test(w) || /mode=ro\b/.test(w))) return undefined
  const isDb = (w: string) => MKDB.test(w.replace(/^file:/, ''))
  const sql = args.filter(w => !w.startsWith('-') && !isDb(w))
  const dbArg = args.some(isDb)
  const inSql = sql.some(w => /mk\.db/.test(w))
  if (!dbArg && !inSql) return undefined
  const stdin = piped || args.some(w => w.startsWith('<'))
  const writes = sql.some(w => SQL_WRITE.test(noLiterals(w)) || VACUUM_MK.test(w))
  // ponytail: with a plain db arg, SQL-less or stdin SQL is denied; a quoted-literal scan only, not a SQL parser
  if (dbArg ? stdin || !sql.length || writes : writes) {
    return { cmd: 'sqlite3 write to mk.db', why: 'use mk-state / mkdb.py, or sqlite3 -readonly' }
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
  const w0 = ws[0]
  if (!w0) return undefined
  const got = shape(ws)
  const hit = banned.find(c => {
    const cs = c.split(/\s+/).filter(Boolean)
    const c0 = cs[0]
    if (!c0 || (c0.includes('/') ? w0 !== c0 : base(w0) !== c0)) return false
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
    const ban = gitBan(ws) ?? sqliteBan(ws, /\|[^|]*\bsqlite3\b/.test(command)) ?? bannedHit(ws, banned)
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
  if (dq && dq[1] !== undefined) return dq[1].replace(/\\(.)/g, '$1')
  const sq = t.match(/^'((?:[^']|'')*)'/)
  if (sq && sq[1] !== undefined) return sq[1].replace(/''/g, "'")
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
      if (!m || m[1] === undefined) continue
      const flow = m[1].match(/^\[(.*)\]/)
      if (flow && flow[1] !== undefined) {
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
        const st = await $.fs.stat(paths.legacyDataDir(dir)).catch(() => undefined)
        if (st?.kind === 'dir') {
          yamlPath = paths.projectYaml(paths.legacyDataDir(dir))
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
