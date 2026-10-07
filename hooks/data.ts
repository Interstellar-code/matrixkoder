// MatrixKoder state layer (design §3/§4, state-schema v1): the PURE halves of
// loading — data-dir resolution math, JSON parsing, shape coercion, text
// sanitising, and the cross-module timer registry. Atoms and $-bound reads
// live in the files that use them: the engine's scan reads a module's own
// consts, and $ may not cross an import. wake.ts / tools.ts reuse the path
// helpers so every module resolves the data dir by the same schema rule
// (MATRIXKODER_DATA → nearest .git toplevel, a linked worktree → its main
// checkout; ~/.matrixkoder is the GLOBAL dir, never project data).
import type { MkRun, MkRunIndexEntry, MkSnapshot, MkUsage } from '../types'

export const PANE_ID = 'matrixkoder'

export const EMPTY_SNAPSHOT: MkSnapshot = { now: 0, dataDir: null, run: null, runs: [], usage: null }

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

/** The active-run pick: status "active", newest started first (chronological, not lexical). */
export function activeEntry(entries: readonly MkRunIndexEntry[]): MkRunIndexEntry | null {
  const key = (e: MkRunIndexEntry): number => {
    const t = Date.parse(e.started ?? '')
    return Number.isFinite(t) ? t : 0
  }
  const active = entries.filter(e => e.status === 'active').sort((a, b) => key(b) - key(a))
  return active[0] ?? null
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

export function mkSnapshot(now: number, dataDir: string | null, run: MkRun | null, runs: readonly MkRunIndexEntry[], usage: MkUsage | null): MkSnapshot {
  return { now, dataDir, run, runs, usage }
}

/** Cross-module timer registry: dashboard arms these, status.ts's session.end cancels them
 *  (a second unmatched session.end registration would fail validation). */
export const timerRegistry: { cancelRefresh?: () => void; cancelPoll?: () => void } = {}

export function cancelDashboardTimers(): void {
  timerRegistry.cancelRefresh?.()
  timerRegistry.cancelPoll?.()
  timerRegistry.cancelRefresh = undefined
  timerRegistry.cancelPoll = undefined
}
