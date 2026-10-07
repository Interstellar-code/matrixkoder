// MatrixKoder state layer (design §3/§4, state-schema v1): the PURE halves of
// loading — candidate dirs, JSON parsing, shape coercion. Atoms and $-bound
// reads live in the files that use them: the engine's scan reads a module's
// own consts, and $ may not cross an import.
import type { MkRun, MkRunIndexEntry, MkSnapshot, MkUsage } from '../types'

export const PANE_ID = 'matrixkoder'

export const EMPTY_SNAPSHOT: MkSnapshot = { now: 0, dataDir: null, run: null, runs: [], usage: null }

/** Variadic path join: '/'-separates without doubling (dataDir never ends in '/'). */
export const join = (...parts: readonly string[]): string =>
  parts.reduce((a, b) => (a.endsWith('/') ? `${a}${b}` : `${a}/${b}`))

/** The session cwd first, then every ancestor up to / — findDataDir stats these for .matrixkoder/. */
export function candidateDirs(from: string): string[] {
  const out: string[] = []
  let dir = from.replace(/\/+$/, '') || '/'
  for (;;) {
    out.push(dir)
    if (dir === '/') return out
    const cut = dir.lastIndexOf('/')
    dir = cut <= 0 ? '/' : dir.slice(0, cut)
  }
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

/** The active-run pick: status "active", newest started first. */
export function activeEntry(entries: readonly MkRunIndexEntry[]): MkRunIndexEntry | null {
  const active = entries
    .filter(e => e.status === 'active')
    .sort((a, b) => String(b.started ?? '').localeCompare(String(a.started ?? '')))
  return active[0] ?? null
}

export function coerceRun(obj: unknown): MkRun | null {
  if (!isRecord(obj) || typeof obj.id !== 'string' || !Array.isArray(obj.tasks)) return null
  return obj as unknown as MkRun
}

export function coerceUsage(obj: unknown): MkUsage | null {
  if (!isRecord(obj) || !isRecord(obj.agents)) return null
  return obj as unknown as MkUsage
}

export function mkSnapshot(now: number, dataDir: string | null, run: MkRun | null, runs: readonly MkRunIndexEntry[], usage: MkUsage | null): MkSnapshot {
  return { now, dataDir, run, runs, usage }
}
