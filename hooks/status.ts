// MatrixKoder chip + toasts (design §5): pure text builders over a snapshot.
// The refresh loop that feeds them lives in dashboard.tsx (one session.start
// per module set); registerStatus owns the chip's end-of-session cleanup and
// cancels the dashboard timers (their registry lives in data.ts to avoid an
// import cycle).
import type { On, PluginOptions } from 'claude-code'
import type { MkRun, MkSnapshot, MkStatusMemory, MkTask } from '../types'
import { cancelDashboardTimers, sanitizeText } from './data'

const AGENT_ORDER = ['claude', 'agy', 'codex', 'opencode'] as const

function counts(run: MkRun): { total: number; done: number; byStatus: Map<string, number> } {
  const byStatus = new Map<string, number>()
  let done = 0
  for (const t of run.tasks) {
    byStatus.set(t.status, (byStatus.get(t.status) ?? 0) + 1)
    if (t.status === 'done' || t.status === 'merged') done += 1
  }
  return { total: run.tasks.length, done, byStatus }
}

function minutesSince(iso: string | null | undefined, now: number): number | null {
  if (!iso) return null
  const t = Date.parse(iso)
  if (!Number.isFinite(t) || !now) return null
  return Math.max(0, Math.round((now - t) / 60000))
}

function pick(run: MkRun, statuses: readonly string[]): MkTask[] {
  return run.tasks.filter(t => statuses.includes(t.status))
}

function shortDate(iso: string | null | undefined): string {
  const t = Date.parse(iso ?? '')
  if (!Number.isFinite(t)) return '—'
  const d = new Date(t)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** Only agents the run actually routes to (no task agents named → every ledger agent). */
function exhaustedAgent(snap: MkSnapshot): { agent: string; resetShort: string; window: string } | null {
  const agents = snap.usage?.agents
  if (!agents || !snap.now) return null
  const used = [...new Set(snap.run?.tasks.map(t => t.agent).filter((a): a is string => !!a) ?? [])]
  const names = used.length > 0
    ? used
    : [...AGENT_ORDER, ...Object.keys(agents).filter(a => !(AGENT_ORDER as readonly string[]).includes(a))]
  for (const agent of names) {
    const entry = agents[agent]
    if (!entry) continue
    const until = Date.parse(entry.exhausted_until ?? '')
    if (Number.isFinite(until) && until > snap.now) {
      return { agent, resetShort: shortDate(entry.exhausted_until), window: untilHours(snap, agent) }
    }
  }
  return null
}

function windowKind(snap: MkSnapshot, iso: string | null | undefined): string {
  const until = Date.parse(iso ?? '')
  if (!Number.isFinite(until) || !snap.now) return 'window'
  return until - snap.now > 6 * 3600_000 ? 'wk' : '5h'
}

function untilHours(snap: MkSnapshot, agent: string): string {
  return windowKind(snap, snap.usage?.agents?.[agent]?.exhausted_until)
}

/** The exhausted pool of an agent (the one whose exhausted_until is future), for toast copy. */
function exhaustedPool(snap: MkSnapshot, agent: string): string | null {
  const pools = snap.usage?.agents?.[agent]?.pools
  if (!pools || !snap.now) return null
  for (const [name, p] of Object.entries(pools)) {
    const t = Date.parse(p.exhausted_until ?? '')
    if (Number.isFinite(t) && t > snap.now) return name
  }
  return null
}

/** The §5.1 chip, by descending priority; '' clears the line (no run). */
export function chipText(snap: MkSnapshot): string {
  const run = snap.run
  if (!run || run.status === 'done' || run.status === 'archived') return ''
  const { total, done, byStatus } = counts(run)
  const building = (byStatus.get('building') ?? 0) + (byStatus.get('verifying') ?? 0)
  if (run.waiting) return `⬡ mk · ⚠ 1 waiting on you · ${building} building`
  const stalled = pick(run, ['stalled'])[0]
  if (stalled) {
    const mins = minutesSince(stalled.updated, snap.now)
    return `⬡ mk · ⚠ ${stalled.id} stalled${mins != null ? ` (${mins}m)` : ''} · ${building} building`
  }
  const blocked = pick(run, ['blocked'])[0]
  if (blocked) {
    const deps = (blocked.deps ?? []).join(', ') || 'dependencies'
    return `⬡ mk · ⊘ ${blocked.id} blocked by ${deps} · ${building} building`
  }
  if (done === total && total > 0) return `⬡ mk · ✓ all ${total} tasks merged · stage ${run.stage}/12`
  const exhausted = exhaustedAgent(snap)
  if (exhausted) return `⬡ mk · ${exhausted.agent} ⊘ ${exhausted.window} resets ${exhausted.resetShort} · ${building} building`
  if (building > 0 || (byStatus.get('review') ?? 0) > 0) {
    const parts: string[] = []
    if (building) parts.push(`${building} building`)
    const review = byStatus.get('review') ?? 0
    if (review) parts.push(`${review} review`)
    return `⬡ mk · ${parts.join(' · ')} · ${done}/${total} done`
  }
  const next = sanitizeText(run.next, 30)
  return `⬡ mk · stage ${run.stage}/12 · idle${next ? ` · next: ${next}` : ''}`
}

/** The §5.2 toasts for what changed since the previous snapshot (first load: mem.loaded false → none). */
export function toastsFor(mem: MkStatusMemory, snap: MkSnapshot): string[] {
  const out: string[] = []
  const run = snap.run
  if (run) {
    for (const t of run.tasks) {
      if (t.ready && mem.ready && !mem.ready.includes(t.id)) {
        out.push(`⬡ [MatrixKoder] Task ${t.id} ready: deps met (${(t.deps ?? []).join(', ')})`)
      }
      const was = mem.statuses[t.id]
      if (was === undefined || was === t.status) continue
      const sha = (t.last_commit ?? '').slice(0, 7)
      const mins = minutesSince(t.updated, snap.now)
      switch (t.status) {
        case 'merged':
        case 'done':
          out.push(`⬡ [MatrixKoder] Task ${t.id} ${t.status} & passed gates${sha ? ` (${sha})` : ''}`)
          break
        case 'stalled': {
          const lane = (t.lane ?? 'lane').split('/').filter(Boolean).at(-1) ?? 'lane'
          out.push(`⬡ [MatrixKoder] Task ${t.id} stalled: no activity in ${lane}${mins != null ? ` (${mins}m)` : ''}`)
          break
        }
        case 'blocked':
          out.push(`⬡ [MatrixKoder] Task ${t.id} blocked waiting on ${(t.deps ?? []).join(', ') || 'dependencies'}`)
          break
        case 'quota': {
          const agent = t.agent ?? 'agent'
          const pool = exhaustedPool(snap, agent)
          const poolUntil = pool ? snap.usage?.agents?.[agent]?.pools?.[pool]?.exhausted_until : null
          const window = poolUntil ? windowKind(snap, poolUntil) : untilHours(snap, agent)
          const ex = exhaustedAgent(snap)
          out.push(`⬡ [MatrixKoder] Quota reached for ${agent}${pool ? `.${pool}` : ''} (${window}).${ex ? ` Resets ${ex.resetShort}.` : ''}`)
          break
        }
        case 'provider': {
          const ev = (run.recent ?? []).filter(e => e.task === t.id).at(-1)
          out.push(`⬡ [MatrixKoder] Provider error on ${t.agent ?? 'agent'}: ${sanitizeText(ev?.text, 60) || 'upstream error'}`)
          break
        }
      }
    }
    if (!mem.waiting && run.waiting) out.push(`⬡ [MatrixKoder] Action required: ${sanitizeText(run.waiting.question, 80)}`)
  }
  return out
}

export function registerStatus(on: On, options: PluginOptions): void {
  // the refresh loop itself starts in dashboard.tsx's single session.start hook
  // (the validator rejects a second unmatched session.start registration)
  on('session.end', async ($, e, next) => {
    cancelDashboardTimers() // a tick after end must not resurrect the chip
    if (options.chip !== false) {
      try {
        $.ui.status(undefined)
      } catch {
        // clearing refused — the session is ending anyway
      }
    }
    return next(e)
  })
}
