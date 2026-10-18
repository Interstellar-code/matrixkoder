// File activity store and watch-only tool-call hook (F1)
import { atom, update } from 'claude-code'
import type { EngineInterface, On, PluginOptions } from 'claude-code'
import type { MkFileAct } from '../types'

const fileActivityAtom = atom(
  { plugin: 'matrixkoder', key: 'fileActivity' } as const,
  [] as readonly MkFileAct[],
)

const MAX_ACTIVITY_ENTRIES = 200
const ACTIVITY_TTL_MS = 3000

/**
 * Pure function: takes the old activity list and returns a new one with the entry recorded.
 * Drops entries older than 3000 ms and keeps at most 200 (oldest dropped first).
 * A relative path or empty string returns the store unchanged.
 * A read recorded inside 3 s after a write on the same path leaves the write.
 */
export function recordActivity(
  store: readonly MkFileAct[] | undefined,
  path: string,
  kind: 'read' | 'write',
  now: number,
): readonly MkFileAct[] {
  if (typeof path !== 'string' || !path || !path.startsWith('/')) {
    return store ?? []
  }

  const valid = (store ?? []).filter(e => now - e.at <= ACTIVITY_TTL_MS)

  if (kind === 'read') {
    const hasActiveWrite = valid.some(e => e.path === path && e.kind === 'write')
    if (hasActiveWrite) {
      return valid.length > MAX_ACTIVITY_ENTRIES ? valid.slice(-MAX_ACTIVITY_ENTRIES) : valid
    }
  }

  const next = [...valid, { path, kind, at: now }]
  return next.length > MAX_ACTIVITY_ENTRIES ? next.slice(-MAX_ACTIVITY_ENTRIES) : next
}

/**
 * Pure function: returns a Map of path to 'read' | 'write' for entries younger than 3000 ms.
 * A write beats a read.
 */
export function liveActivity(
  store: readonly MkFileAct[] | undefined,
  now: number,
): Map<string, 'read' | 'write'> {
  const map = new Map<string, 'read' | 'write'>()
  if (!store) return map

  for (const entry of store) {
    if (now - entry.at <= ACTIVITY_TTL_MS) {
      const prev = map.get(entry.path)
      if (entry.kind === 'write' || !prev) {
        map.set(entry.path, entry.kind)
      }
    }
  }

  return map
}

function onToolCall<E extends { tool: string; file_path?: string; notebook_path?: string }, R>(
  $: EngineInterface,
  e: E,
  next: (event: E) => R,
): R {
  try {
    const rawPath = e.tool === 'NotebookEdit' ? e.notebook_path : e.file_path
    if (typeof rawPath === 'string' && rawPath.startsWith('/')) {
      const kind = e.tool === 'Read' ? 'read' : 'write'
      void update($, fileActivityAtom, s => recordActivity(s, rawPath, kind, Date.now())).catch(() => {})
    }
  } catch {}
  return next(e)
}

/**
 * Registers the watch-only tool-call hook on Read, Edit, Write and NotebookEdit.
 * Adds the entry with update() without await, swallowing errors. Never blocks or modifies the call.
 */
export function registerFileActivity(on: On, _options?: PluginOptions): void {
  on('tool.call', { tool: 'Read' }, onToolCall)
  on('tool.call', { tool: 'Edit' }, onToolCall)
  on('tool.call', { tool: 'Write' }, onToolCall)
  on('tool.call', { tool: 'NotebookEdit' }, onToolCall)
}
