// MatrixKoder state layer (design §3/§4, state-schema v1): the PURE halves of
// loading — data-dir resolution math, JSON parsing, shape coercion, text
// sanitising, and the cross-module timer registry. Atoms and $-bound reads
// live in the files that use them: the engine's scan reads a module's own
// consts, and $ may not cross an import.
//
// The RULES over these primitives (the data-dir order, the MK home names, the
// file names under the data dir) are declared once in paths.ts (C1): the
// dashboard, tools and wake import them from there. This file stays the pure
// math they are built on — MATRIXKODER_DATA → nearest .git toplevel, a linked
// worktree → its main checkout; ~/.matrixkoder is the GLOBAL dir, never
// project data.
import type { MkRun, MkRunIndexEntry, MkSnapshot, MkUsage } from '../types'

export const PANE_ID = 'matrixkoder'

/** display order of the known build agents (dashboard usage rows, chip scan) */
export const AGENT_ORDER = ['claude', 'agy', 'codex', 'opencode'] as const

export const EMPTY_SNAPSHOT: MkPaneSnapshot = { now: 0, dataDir: null, run: null, runs: [], usage: null, projectAgents: null, activeRuns: [] }

/** The pane's snapshot: `activeRuns` carries EVERY active run loaded (newest
 *  first) so the pane renders a Run/Plan/Lanes block per run; `run` stays the
 *  newest of them — the status chip, toasts and wake keep consuming it. (The
 *  state key is declared MkSnapshot; the extra field travels in that value.) */
export type MkPaneSnapshot = MkSnapshot & { activeRuns: readonly MkRun[] }

/** Variadic path join: '/'-separates without doubling (paths here never end in '/'). */
export const join = (...parts: readonly string[]): string =>
  parts.reduce((a, b) => (a.endsWith('/') ? `${a}${b}` : `${a}/${b}`))

export function parentOf(dir: string): string {
  const cut = dir.lastIndexOf('/')
  return cut <= 0 ? '/' : dir.slice(0, cut)
}

export function basename(path: string): string {
  const parts = path.replace(/\/+$/, '').split('/')
  return parts[parts.length - 1] ?? ''
}

/** Resolves '.', '..' and '//' segments without touching the filesystem. */
export function normalizePath(p: string): string {
  const out: string[] = []
  for (const seg of p.split('/')) {
    if (seg === '' || seg === '.') continue
    if (seg === '..') out.pop()
    else out.push(seg)
  }
  return `/${out.join('/')}`
}

/** The walk stops BEFORE `stopBefore` (home): the global ~/.matrixkoder is never project data. */
export function candidateDirs(from: string, stopBefore?: string): string[] {
  const out: string[] = []
  let dir = from.replace(/\/+$/, '') || '/'
  for (;;) {
    if (stopBefore != null && dir === stopBefore.replace(/\/+$/, '')) return out
    out.push(dir)
    if (dir === '/') return out
    dir = parentOf(dir)
  }
}

/** The global dir: $MATRIXKODER_HOME (a leading ~ means HOME), else ~/.matrixkoder. */
export function globalDirPath(envHome: string | null | undefined, home: string | null): string | null {
  const e = (envHome ?? '').trim()
  if (e !== '') return e === '~' ? home : e.startsWith('~/') ? (home == null ? null : join(home, e.slice(2))) : e
  return home == null ? null : join(home, '.matrixkoder')
}

/** One row of `mkdb.py projects --json`: the root store's projects table. `db_path` is the C3
 *  migrated marker (set by mk-migrate --switch); the same reply carries it for free. */
export type MkProjectRow = { slug?: unknown; repo?: unknown; data_dir?: unknown; db_path?: unknown }

/** Root-store lookup (`mkdb.py projects --json`): first repo path whose row has a data_dir wins.
 *  Same path rules as projectDataDir. A failed command or an empty list is not a match — the
 *  caller falls back to projects.json.
 *
 *  C1 (L3): a row with `db_path` set is a MIGRATED project — its folder is the C2 target
 *  `<home>/projects/<slug>` (dirname of db_path), which is what every path under it (the store,
 *  the plan/run/task tree) hangs off. Mirrors mkpaths.project_dir: migrated → C2, else today's
 *  data_dir. Unmigrated rows keep the old answer byte-for-byte. */
export function projectDirFromStore(rows: readonly MkProjectRow[] | null | undefined, repoPaths: readonly string[], home: string | null): string | null {
  if (!Array.isArray(rows)) return null
  const wanted = new Set(repoPaths)
  for (const row of rows) {
    if (row == null || typeof row !== 'object') continue
    const repo = typeof row.repo === 'string' ? row.repo : ''
    if (!wanted.has(repo)) continue
    const dbPath = typeof row.db_path === 'string' ? row.db_path.trim() : ''
    if (dbPath !== '') return normalizePath(parentOf(normalizePath(dbPath)))
    const dir = typeof row.data_dir === 'string' ? row.data_dir.trim() : ''
    if (dir === '') continue
    if (dir === '~' || dir.startsWith('~/')) return home == null ? null : normalizePath(dir === '~' ? home : join(home, dir.slice(2)))
    return normalizePath(dir.startsWith('/') ? dir : join(repo, dir))
  }
  return null
}

/** projects.json lookup (wizard-written `{ "<repo>": { data_dir } }`): first repo path listed wins. A leading ~
 *  in data_dir means HOME; a relative one is resolved against the repo. */
export function projectDataDir(projects: unknown, repoPaths: readonly string[], home: string | null): string | null {
  if (!isRecord(projects)) return null
  for (const repo of repoPaths) {
    const entry = projects[repo]
    const dir = isRecord(entry) && typeof entry.data_dir === 'string' ? entry.data_dir.trim() : ''
    if (dir === '') continue
    if (dir === '~' || dir.startsWith('~/')) return home == null ? null : normalizePath(dir === '~' ? home : join(home, dir.slice(2)))
    return normalizePath(dir.startsWith('/') ? dir : join(repo, dir))
  }
  return null
}

/** The gitdir path out of a linked worktree's `.git` FILE ("gitdir: /abs/path"). */
export function parseGitdir(dotGitText: string): string | null {
  const m = dotGitText.split('gitdir:', 2)[1]
  return m ? m.trim() : null
}

/** Pure core of mk-state.py's main_worktree: gitdir + commondir → the main checkout. */
export function mainCheckout(gitdir: string, commondirText: string): string | null {
  const common = normalizePath(join(gitdir, commondirText.trim()))
  return basename(common) === '.git' ? parentOf(common) : null
}

export function parseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown
  } catch {
    return null // missing, unreadable or corrupt
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null
}

export function coerceIndex(raw: unknown): MkRunIndexEntry[] {
  return Array.isArray(raw)
    ? (raw.filter(isRecord) as MkRunIndexEntry[]).filter(e => typeof e.id === 'string')
    : []
}

/** Every active run, newest started first (chronological, not lexical): the pane
 *  renders a Run/Plan/Lanes block per entry; the first is the run the chip,
 *  toasts and wake consume. */
export function activeEntries(entries: readonly MkRunIndexEntry[]): MkRunIndexEntry[] {
  const key = (e: MkRunIndexEntry): number => {
    const t = Date.parse(e.started ?? '')
    return Number.isFinite(t) ? t : 0
  }
  return entries.filter(e => e.status === 'active').sort((a, b) => key(b) - key(a))
}

export function coerceRun(obj: unknown): MkRun | null {
  if (!isRecord(obj) || typeof obj.id !== 'string' || !Array.isArray(obj.tasks)) return null
  const waiting = isRecord(obj.waiting) && typeof obj.waiting.question === 'string'
    ? {
        question: obj.waiting.question,
        options: Array.isArray(obj.waiting.options) ? obj.waiting.options.filter((o): o is string => typeof o === 'string') : undefined,
        rec: typeof obj.waiting.rec === 'string' ? obj.waiting.rec : null,
        since: typeof obj.waiting.since === 'string' ? obj.waiting.since : null,
      }
    : null
  return {
    ...obj,
    feature: typeof obj.feature === 'string' ? obj.feature : '',
    branch: typeof obj.branch === 'string' ? obj.branch : '',
    head_sha: typeof obj.head_sha === 'string' ? obj.head_sha : null,
    status: typeof obj.status === 'string' ? obj.status : 'active',
    stage: typeof obj.stage === 'number' && Number.isFinite(obj.stage) ? obj.stage : 0,
    next: typeof obj.next === 'string' ? obj.next : null,
    waiting,
    // non-object tasks / recent entries are dropped, never rendered or counted
    tasks: obj.tasks.filter((t): t is Record<string, unknown> => isRecord(t) && typeof t.id === 'string'),
    recent: Array.isArray(obj.recent) ? obj.recent.filter(isRecord) : [],
  } as unknown as MkRun
}

export function coerceUsage(obj: unknown): MkUsage | null {
  if (!isRecord(obj) || !isRecord(obj.agents)) return null
  return obj as unknown as MkUsage
}

/** Event text and questions carry pane evidence: strip control chars, collapse whitespace. */
export function sanitizeText(text: string | null | undefined, cap = 200): string {
  if (!text) return ''
  return text
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, cap)
}

export function mkSnapshot(now: number, dataDir: string | null, run: MkRun | null, activeRuns: readonly MkRun[], runs: readonly MkRunIndexEntry[], usage: MkUsage | null, projectAgents: readonly string[] | null): MkPaneSnapshot {
  return { now, dataDir, run, runs, usage, projectAgents, activeRuns }
}

// ---------- matrixkoder.yaml line parsers (no YAML dependency; the wizard's emitter shapes) ----------

/** `agents:` block of matrixkoder.yaml: names of available: true agents, herdr excluded.
 *  Accepts any indent >= 2 spaces and block-style or flow-style entries.
 *  Returns null when the yaml or the block is absent or unparseable ("no filter"),
 *  [] when the agents block is present but every agent is available: false (show no agents). */
export function parseYamlAgents(yaml: string): readonly string[] | null {
  const out: string[] = []
  let inBlock = false
  let sawBlock = false
  let sawAgent = false
  let agentIndent: number | null = null
  let currentAgent: string | null = null
  let currentAvailable: boolean | null = null

  const commit = () => {
    if (currentAgent && currentAgent !== 'herdr' && currentAgent !== 'checked_at' && currentAvailable === true) {
      out.push(currentAgent)
    }
  }

  for (const raw of yaml.split(/\r?\n/)) {
    if (!inBlock) {
      if (/^agents:\s*(#.*)?$/.test(raw)) {
        inBlock = true
        sawBlock = true
      }
      continue
    }
    const line = raw.trim()
    if (line === '' || line.startsWith('#')) continue
    if (!/^\s/.test(raw)) break // the next top-level key ends the block

    const m = /^(\s+)([A-Za-z0-9_-]+):\s*(.*)$/.exec(raw)
    const indent = m && m[1] ? m[1].length : (raw.match(/^\s*/)?.[0]?.length ?? 0)

    if (m && m[1] && m[2] && indent >= 2 && (agentIndent === null || indent === agentIndent)) {
      commit()
      agentIndent = indent
      currentAgent = m[2]
      sawAgent = true
      currentAvailable = null
      const rest = (m[3] ?? '').replace(/\s+#.*$/, '').trim()
      if (/\bavailable:\s*true\b/.test(rest)) {
        currentAvailable = true
      } else if (/\bavailable:\s*false\b/.test(rest)) {
        currentAvailable = false
      }
    } else if (currentAgent && agentIndent !== null && indent > agentIndent) {
      if (/\bavailable:\s*true\b/.test(line)) {
        currentAvailable = true
      } else if (/\bavailable:\s*false\b/.test(line)) {
        currentAvailable = false
      }
    }
  }
  commit()
  // block present with at least one named agent but none enabled = show no agents;
  // absent / empty / unparseable block = no filter (null)
  return sawBlock && sawAgent ? out : null
}

/** `usage:` → `providers:` list of matrixkoder.yaml: a `  providers: [a, b]` flow list or a `- item` block list. null when absent. */
export function parseYamlProviders(yaml: string): readonly string[] | null {
  const out: string[] = []
  let inUsage = false
  let inList = false
  const item = (text: string): string => text.trim().replace(/^["']|["']$/g, '')
  for (const raw of yaml.split(/\r?\n/)) {
    if (!inUsage) {
      if (/^usage:\s*(#.*)?$/.test(raw)) inUsage = true
      continue
    }
    if (!inList) {
      if (raw !== '' && !/^\s/.test(raw) && !/^usage:/.test(raw.trim())) break // next top-level key
      const m = /^ {2}providers:\s*(.*)$/.exec(raw)
      if (!m || m[1] === undefined) continue
      const flow = m[1].replace(/\s+#.*$/, '').match(/^\[(.*)\]/)
      if (flow && flow[1] !== undefined) return flow[1].split(',').map(item).filter(v => v !== '')
      inList = true // a block list follows
      continue
    }
    if (!raw.trim().startsWith('-')) break // the first non-item line ends the list
    const v = item(raw.trim().slice(1))
    if (v !== '') out.push(v)
  }
  return out.length > 0 || inList ? out : null
}

/** The project's enabled agents in display order: known ones first, then the yaml's own order. */
export function orderAgents(names: readonly string[] | null): readonly string[] | null {
  if (names == null) return null
  return [...AGENT_ORDER.filter(a => names.includes(a)), ...names.filter(n => !(AGENT_ORDER as readonly string[]).includes(n))]
}

/** Cross-module timer registry: dashboard arms these, status.ts's session.end cancels them
 *  (a second unmatched session.end registration would fail validation). */
export const timerRegistry: { cancelRefresh?: () => void; cancelPoll?: () => void; cancelSweep?: () => void; cancelBus?: () => void; cancelRain?: () => void; mkRegistered?: boolean } = {}

export function cancelDashboardTimers(): void {
  timerRegistry.cancelRefresh?.()
  timerRegistry.cancelPoll?.()
  timerRegistry.cancelSweep?.()
  timerRegistry.cancelBus?.()
  timerRegistry.cancelRain?.()
  timerRegistry.cancelRain = undefined
  timerRegistry.cancelRefresh = undefined
  timerRegistry.cancelPoll = undefined
  timerRegistry.cancelSweep = undefined
  timerRegistry.cancelBus = undefined
  timerRegistry.mkRegistered = false // a new session (after /clear) must declare /mk again
}
