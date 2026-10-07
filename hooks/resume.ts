// MatrixKoder resume section (plan §6): prompt.compose adds the active run's
// id, feature, stage, next action, waiting question and status counts as a
// session-scoped section, so the loop survives compaction.
import { atom, read } from 'claude-code'
import type { On } from 'claude-code'
import type { MkRun } from '../types'
import { EMPTY_SNAPSHOT, PANE_ID } from './data'

const MAX_CHARS = 600
const SECTION_ID = 'matrixkoder:run'

// same plugin+key address as dashboard.tsx's atom — file-local for the scan
const snapshotAtom = atom({ plugin: 'matrixkoder', key: 'snapshot' } as const, EMPTY_SNAPSHOT)

function resumeText(run: MkRun): string {
  const counts = new Map<string, number>()
  for (const t of run.tasks) counts.set(t.status, (counts.get(t.status) ?? 0) + 1)
  const byStatus = [...counts.entries()].map(([s, n]) => `${n} ${s}`).join(', ')
  const parts = [
    `MatrixKoder run ${run.id} (${run.feature})`,
    `stage ${run.stage}/12`,
    run.next ? `next: ${run.next}` : null,
    run.waiting ? `waiting on user: ${run.waiting.question}` : null,
    `tasks: ${byStatus} (${run.tasks.length} total)`,
  ].filter((p): p is string => p !== null)
  return parts.join(' · ').slice(0, MAX_CHARS)
}

export function registerResume(on: On): void {
  on('prompt.compose', async ($, e, next) => {
    const { sections } = await next(e)
    const snap = await read($, snapshotAtom)
    const run = snap.run
    if (!run || run.status === 'done' || run.status === 'archived') return { sections }
    return { sections: [...sections, { id: SECTION_ID, text: resumeText(run), scope: 'session' as const }] }
  })
}
