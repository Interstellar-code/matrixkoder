// F.7 completion guard, part 2: the status reader behind the resume banner and the Stop hook.
// Both read `mk-report.py status` (read-only, F.6) and nothing else: no store access from TS.
// The Stop hook blocks ONCE per red-set hash, only in the orchestrator's own checkout of a
// fidelity run, and never when stop_hook_active (the turn is already continuing: no trap).
import type { EngineInterface, On, PluginOptions } from 'claude-code'
import { join } from './data'
import { resolveDataDir, sessionCwd } from './paths'

const STATUS_MS = 20_000
const FIRST_IDS = 3
const LANE_DIR_RE = /-mk-\d+$/ // a worktree lane slot: <repo>-mk-<n>
// typed state keys live in types/index.d.ts (not ours): ride wakeSentAt under an id of our own, as wake's pollEpoch does
const STATE_REF = { plugin: 'matrixkoder', key: 'wakeSentAt', id: 'stopGuardHash' } as const

export type GuardRow = { id: string; evidence: readonly number[]; state: string }
export type GuardStatus = {
  run: string
  fidelity: boolean
  green: boolean
  red: number
  stale: number
  deferred: number
  rework: number
  due: number
  openReasons: number
  rows: readonly GuardRow[]
}

const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0)

/** Parse one `mk-report.py status` JSON line; null when it is not the expected shape. */
export function parseStatus(stdout: string): GuardStatus | null {
  let doc: unknown
  try {
    doc = JSON.parse(stdout.trim().split('\n').pop() ?? '')
  } catch {
    return null
  }
  if (doc === null || typeof doc !== 'object') return null
  const o = doc as Record<string, unknown>
  if (o.ok !== true || typeof o.run !== 'string') return null
  const rows: GuardRow[] = []
  if (Array.isArray(o.rows)) {
    for (const r of o.rows) {
      if (r === null || typeof r !== 'object') continue
      const x = r as Record<string, unknown>
      if (typeof x.id === 'string' && typeof x.state === 'string') {
        rows.push({ id: x.id, state: x.state, evidence: Array.isArray(x.evidence) ? x.evidence.filter((n): n is number => typeof n === 'number') : [] })
      }
    }
  }
  return {
    run: o.run,
    fidelity: o.fidelity === true,
    green: o.green === true,
    red: num(o.red),
    stale: num(o.stale),
    deferred: num(o.deferred),
    rework: num(o.rework),
    due: num(o.due),
    openReasons: num(o.open_reasons),
    rows,
  }
}

/** One status spawn for the run, or null when it could not answer (refused spawn, non-zero exit,
 *  unreadable payload). Never throws: a failed read means no banner and no block. */
export async function readStatus($: EngineInterface, dataDir: string, where: string, run?: string): Promise<GuardStatus | null> {
  try {
    const argv = ['python3', `${$.plugin.root}/scripts/mk-report.py`, 'status', '--data', dataDir, '--where', where]
    if (run) argv.push('--run', run)
    const ran = await $.process.run(argv, { timeoutMs: STATUS_MS })
    return ran.exitCode === 0 ? parseStatus(ran.stdout) : null
  } catch {
    return null
  }
}

/** The open-row total the banner and the guard act on (stale counts: a gate with no fresh green). */
export const openCount = (s: GuardStatus): number => s.red + s.stale + s.deferred + s.rework + s.due + s.openReasons

const BAD_STATES = new Set(['RED', 'STALE', 'DEFERRED'])

/** The first few non-passing row ids, `id(STATE)`. */
function firstOpen(s: GuardStatus): string {
  return s.rows.filter(r => BAD_STATES.has(r.state)).slice(0, FIRST_IDS).map(r => `${r.id}(${r.state})`).join(', ')
}

/** One banner line, or null for a green run or a legacy run (no fidelity). */
export function bannerText(s: GuardStatus | null): string | null {
  if (!s || !s.fidelity || openCount(s) === 0) return null
  const counts = [['red', s.red], ['stale', s.stale], ['deferred', s.deferred], ['rework', s.rework], ['due', s.due], ['reasons', s.openReasons]]
    .filter(([, n]) => (n as number) > 0).map(([k, n]) => `${k} ${n}`).join(', ')
  const first = firstOpen(s)
  return `MK RED/REWORK/DUE run ${s.run}: ${counts}${first ? ` · first: ${first}` : ''} · mk-report.py final --run ${s.run}`.slice(0, 300)
}

/** djb2 over the red set: the not-passing row ids+states and the counts. Same set, same hash. */
export function redSetHash(s: GuardStatus): number {
  const text = `${s.run}|${s.red},${s.stale},${s.deferred},${s.rework},${s.due},${s.openReasons}|`
    + s.rows.filter(r => BAD_STATES.has(r.state)).map(r => `${r.id}:${r.state}`).sort().join(',')
  let h = 5381
  for (let i = 0; i < text.length; i++) h = ((h << 5) + h + text.charCodeAt(i)) | 0
  return h >>> 0
}

/** True when cwd is a lane (a slot worktree, a linked worktree, a lane's status file, or the lane
 *  PATH shim), so the guard never fires there. Anything that cannot be read counts as a lane. */
export async function isLaneCwd($: EngineInterface, cwd: string): Promise<boolean> {
  if (LANE_DIR_RE.test(cwd.replace(/\/+$/, ''))) return true
  try {
    if ((await $.env.get('PATH'))?.includes('.matrixkoder-bin')) return true
  } catch {
    return true
  }
  try {
    if ((await $.fs.stat(join(cwd, '.git'))).kind !== 'dir') return true // a linked worktree has a .git FILE
  } catch {
    return true // no .git here: not the orchestrator's checkout root
  }
  try {
    await $.fs.stat(join(cwd, '.matrixkoder-status.json'))
    return true // only a lane writes its status file here
  } catch {
    return false
  }
}

export function registerStopGuard(on: On, options: PluginOptions): void {
  // solo mode has no run to protect; the switch is off by default (a new hook never ships blocking)
  if (options.mode === 'solo' || options.stopGuard !== true) return
  on('classic.Stop', async ($, e, next) => {
    if (e.stop_hook_active) return next(e) // the turn is already continuing because of a block: never twice in a row
    try {
      const cwd = e.cwd || await sessionCwd($)
      if (await isLaneCwd($, cwd)) return next(e)
      const dataDir = await resolveDataDir($, { cwd })
      if (!dataDir) return next(e)
      const status = await readStatus($, dataDir, cwd)
      if (!status || !status.fidelity || status.green || openCount(status) === 0) return next(e) // legacy, green, or just in progress: nothing red
      const hash = redSetHash(status)
      const seen = await $.state.get(STATE_REF).catch(() => ({ value: undefined }))
      if (seen.value === hash) return next(e) // this red set already blocked once
      await $.state.set(STATE_REF, hash)
      const why = bannerText(status) ?? `run ${status.run} is not green`
      return { ...(await next(e)), block: `${why}. Fix these or tell the user what is open before stopping; this guard blocks once per set of open rows.` }
    } catch {
      return next(e) // a guard that errors must never trap the session
    }
  })
}
