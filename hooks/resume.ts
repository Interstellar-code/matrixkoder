// MatrixKoder resume section (plan §6): prompt.compose adds the active run's
// id, feature, stage, next action, waiting question and status counts as a
// session-scoped section, so the loop survives compaction. Free-text fields
// are control-stripped, whitespace-collapsed and capped so the counts tail
// survives the 600-char budget.
import { atom, read } from 'claude-code'
import type { On, PluginOptions } from 'claude-code'
import type { MkRun } from '../types'
import { EMPTY_SNAPSHOT, PANE_ID, sanitizeText } from './data'
import { resolveDataDir, sessionCwd } from './paths'
import { bannerText, readStatus } from './stop-guard'

const MAX_CHARS = 600
const FIELD_CAP = 200
const SECTION_ID = 'matrixkoder:run'
const BANNER_ID = 'matrixkoder:red'
const SOLO_TEXT = 'MK solo: no orchestration, build in this session'

// same plugin+key address as dashboard.tsx's atom — file-local for the scan
const snapshotAtom = atom({ plugin: 'matrixkoder', key: 'snapshot' } as const, EMPTY_SNAPSHOT)

function resumeText(run: MkRun): string {
  const counts = new Map<string, number>()
  for (const t of run.tasks) counts.set(t.status, (counts.get(t.status) ?? 0) + 1)
  const byStatus = [...counts.entries()].map(([s, n]) => `${n} ${s}`).join(', ')
  const next = sanitizeText(run.next, FIELD_CAP)
  const question = sanitizeText(run.waiting?.question, FIELD_CAP)
  const parts = [
    `MatrixKoder run ${run.id} (${sanitizeText(run.feature, 60)}) · mode orchestrator`,
    `stage ${run.stage}/12`,
    next ? `next: ${next}` : null,
    question ? `waiting on user: ${question}` : null,
    `tasks: ${byStatus} (${run.tasks.length} total)`,
  ].filter((p): p is string => p !== null)
  return parts.join(' · ').slice(0, MAX_CHARS)
}

export function registerResume(on: On, options: PluginOptions): void {
  on('prompt.compose', async ($, e, next) => {
    const { sections } = await next(e)
    if (options.mode === 'solo') {
      return { sections: [...sections, { id: SECTION_ID, text: SOLO_TEXT, scope: 'session' as const }] }
    }
    const snap = await read($, snapshotAtom)
    const run = snap.run
    if (!run || run.status === 'done' || run.status === 'archived') return { sections }
    const out = [...sections, { id: SECTION_ID, text: resumeText(run), scope: 'session' as const }]
    // F.7: the always-on piece. One banner line while a fidelity run has red, stale, deferred,
    // rework, due or open-reason rows; nothing for a green or legacy run, nothing when the
    // status read fails (a banner must never throw into prompt composition).
    try {
      const cwd = await sessionCwd($)
      const dataDir = snap.dataDir ?? await resolveDataDir($, { cwd })
      const text = dataDir ? bannerText(await readStatus($, dataDir, cwd, run.id)) : null
      if (text) out.push({ id: BANNER_ID, text, scope: 'session' as const })
    } catch {
      // no banner
    }
    return { sections: out }
  })
}
