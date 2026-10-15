// Pure mappers from mkdb plans JSON to the Plan tab view types. No I/O.
import type { PlanRow, RunRow } from './plans'
import type { PlanPhase, PlanRoute, PlanSub, PlanTask, PlanView } from './plan'
// the ONE store-directory rule (C3): a migrated row's `db_path` folder, else its
// `data_dir`. Shared with `k` so the Plan list and the board agree on one store.
import { projectStoreDir } from './kanban'

export type PlansListReply = { ok?: boolean; plans?: unknown; error?: string }
export type PlanDetailReply = { ok?: boolean; id?: string; phases?: unknown; error?: string }

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const str = (value: unknown): string => (typeof value === 'string' ? value : '')

const strList = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []

/** A string or null: empty text and other types are null (V1.5 fields are
 *  absent-or-value, never ''). */
const strOrNull = (value: unknown): string | null =>
  typeof value === 'string' && value !== '' ? value : null

/** The slot as an int: the store's column is TEXT ('3'), the reply says int. */
const slotOf = (value: unknown): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.trunc(value)
  if (typeof value === 'string' && /^\d+$/.test(value.trim())) return Number.parseInt(value.trim(), 10)
  return null
}

/** route_json as {agent, model, effort}: null when it is not an object or
 *  names none of the three; a member that is missing stays absent. */
const routeOf = (value: unknown): PlanRoute | null => {
  if (!isRecord(value)) return null
  const route: { agent?: string; model?: string; effort?: string } = {}
  if (typeof value.agent === 'string' && value.agent !== '') route.agent = value.agent
  if (typeof value.model === 'string' && value.model !== '') route.model = value.model
  if (typeof value.effort === 'string' && value.effort !== '') route.effort = value.effort
  return Object.keys(route).length > 0 ? route : null
}

/** The V1.5 routing fields a plans-reply task node carries, copied when
 *  present and left absent when not (the view types mark them optional).
 *  Shared by the Detail mapping and the Kanban card mapping. */
export function routeFields(raw: Record<string, unknown>): {
  slot?: number | null
  pane?: string | null
  branch?: string | null
  updated?: string | null
  acceptance?: string | null
  owned?: readonly string[]
  deps?: readonly string[]
  route?: PlanRoute | null
} {
  const out: {
    slot?: number | null
    pane?: string | null
    branch?: string | null
    updated?: string | null
    acceptance?: string | null
    owned?: readonly string[]
    deps?: readonly string[]
    route?: PlanRoute | null
  } = {}
  const slot = slotOf(raw.slot)
  if (slot != null) out.slot = slot
  const pane = strOrNull(raw.pane)
  if (pane != null) out.pane = pane
  const branch = strOrNull(raw.branch)
  if (branch != null) out.branch = branch
  const updated = strOrNull(raw.updated)
  if (updated != null) out.updated = updated
  const acceptance = strOrNull(raw.acceptance)
  if (acceptance != null) out.acceptance = acceptance
  const owned = strList(raw.owned)
  if (owned.length > 0) out.owned = owned
  const deps = strList(raw.deps)
  if (deps.length > 0) out.deps = deps
  const route = routeOf(raw.route)
  if (route != null) out.route = route
  return out
}

/** V1.5: the lane·agent tag a task row and a Kanban card show — `L<slot> ·
 *  <agent> <model's last path segment>` (e.g. `L3 · opencode glm-5.3`). The
 *  part that is missing is omitted; when both are missing, ''. */
export function laneAgent(row: { slot?: number | null; route?: PlanRoute | null }): string {
  const lane = typeof row.slot === 'number' && Number.isFinite(row.slot) ? `L${row.slot}` : ''
  const agentParts: string[] = []
  if (row.route != null) {
    if (row.route.agent != null && row.route.agent !== '') agentParts.push(row.route.agent)
    if (row.route.model != null && row.route.model !== '') {
      const last = row.route.model.split('/').filter(seg => seg !== '').pop()
      if (last != null && last !== '') agentParts.push(last)
    }
  }
  const agent = agentParts.join(' ')
  if (lane === '') return agent
  return agent === '' ? lane : `${lane} · ${agent}`
}

/** Sum of a status-to-count map. Non-numeric values are skipped. */
function sumCounts(counts: unknown): number {
  if (!isRecord(counts)) return 0
  let total = 0
  for (const value of Object.values(counts)) {
    if (typeof value === 'number' && Number.isFinite(value)) total += value
  }
  return total
}
/** Map a task status to its run-card bucket: done / live / planned. Shared with
 *  the run-card counts and the plan-row per-status tally. */
const STATUS_BUCKET: Record<string, 'done' | 'live' | 'planned'> = {
  merged: 'done', done: 'done',
  dispatched: 'live', building: 'live', verifying: 'live', review: 'live', fix: 'live', qa: 'live',
  planned: 'planned', routed: 'planned',
}

/** Sum the per-status counts into the run-card bucket totals. */
function bucketCounts(counts: unknown): { done: number; live: number; planned: number } {
  const out = { done: 0, live: 0, planned: 0 }
  if (typeof counts !== 'object' || counts === null) return out
  for (const [status, n] of Object.entries(counts)) {
    const bucket = STATUS_BUCKET[status]
    if (bucket != null && typeof n === 'number') out[bucket] += n
  }
  return out
}

/** The raw per-status counts as a { status: count } record, for the plan row. */
function statusCounts(counts: unknown): Record<string, number> {
  const out: Record<string, number> = {}
  if (typeof counts !== 'object' || counts === null) return out
  for (const [status, n] of Object.entries(counts)) {
    if (typeof n === 'number') out[status] = n
  }
  return out
}

/** A human-readable counts line from the raw per-status counts. */
function countsLineFrom(counts: unknown): string {
  const sc = statusCounts(counts)
  const parts: string[] = []
  for (const [status, n] of Object.entries(sc)) {
    parts.push(`${n} ${status}`)
  }
  return parts.join(' · ')
}

function taskOf(raw: unknown): PlanTask | null {
  if (!isRecord(raw) || typeof raw.id !== 'string') return null
  const status = str(raw.status)
  const subs: PlanSub[] = []
  if (Array.isArray(raw.subtasks)) {
    for (const sub of raw.subtasks) {
      if (!isRecord(sub) || typeof sub.id !== 'string') continue
      const subStatus = str(sub.status)
      const subType = strOrNull(sub.type)
      const subRound = typeof sub.round === 'number' && Number.isFinite(sub.round) ? sub.round : undefined
      const subVerdict = strOrNull(sub.verdict)
      subs.push({
        id: sub.id, title: str(sub.title), status: subStatus, right: subStatus,
        ...routeFields(sub),
        ...(subType != null ? { type: subType } : {}),
        ...(subRound !== undefined ? { round: subRound } : {}),
        ...(subVerdict != null ? { verdict: subVerdict } : {}),
      })
    }
  }
  const type = strOrNull(raw.type)
  const round = typeof raw.round === 'number' && Number.isFinite(raw.round) ? raw.round : undefined
  const verdict = strOrNull(raw.verdict)
  return {
    id: raw.id, title: str(raw.title), status, right: status, subs,
    ...routeFields(raw),
    ...(type != null ? { type } : {}),
    ...(round !== undefined ? { round } : {}),
    ...(verdict != null ? { verdict } : {}),
  }
}

/**
 * List reply to plan rows and one run row per plan that has a live_run.
 * counts.phases is 0: the list reply has no phase list. counts.tasks is
 * the sum of the status-to-count map.
 */
export function plansFromReply(reply: unknown): { plans: PlanRow[]; runs: RunRow[] } {
  const plans: PlanRow[] = []
  const runs: RunRow[] = []
  if (!isRecord(reply) || !Array.isArray(reply.plans)) return { plans, runs }
  for (const raw of reply.plans) {
    if (!isRecord(raw) || typeof raw.id !== 'string') continue
    const live = typeof raw.live_run === 'string' && raw.live_run !== '' ? raw.live_run : null
    const row: PlanRow = {
      id: raw.id,
      title: str(raw.title),
      status: str(raw.status),
      created: str(raw.created),
      runs: strList(raw.runs),
      live_run: live,
      counts: { tasks: sumCounts(raw.counts), phases: 0, byStatus: statusCounts(raw.counts) },
      ...(str(raw.project) !== '' ? { project: str(raw.project) } : {}),
      ...(Array.isArray(raw.design) ? { design: strList(raw.design) } : {}),
      ...(branchOf(raw.integration) !== '' ? { mergesTo: branchOf(raw.integration) } : {}),
    }
    plans.push(row)
    if (live != null) {
      const buckets = bucketCounts(raw.counts)
      runs.push({
        id: live,
        plan: row.id,
        title: row.title,
        stage: str(raw.stage),
        counts: buckets,
        countsLine: countsLineFrom(raw.counts),
      })
    }
  }
  return { plans, runs }
}


/**
 * L7.2: the ROOT store's `mkdb.py overview --json` reply onto the same list
 * rows — the List sub-tab renders from this and never opens a project DB.
 *
 * Every field the List draws (`id`, `title`, `status`, `live_run`, plus the
 * `counts` tally) is a column of the root `plan_summary` row, so the mapping is
 * lossless for the list. `created`, `runs`, `mergesTo` and `design` are Detail
 * header fields the root row does not carry: they stay empty here and the
 * Detail still reads them from the project DB.
 *
 * An overview with no `plans` (a pre-migration root, or none) maps to no rows.
 *
 * L7.2 R2: the root holds EVERY project's `plan_summary`, so an unfiltered map
 * leaks other projects' plans into the List (hermes-switchui's plan showed in
 * this project's Plan tab, and Detail then asked the active project's store for a
 * plan it does not have). When `activeDir` names the ACTIVE project's store, only
 * rows whose `project` slug belongs to that store are kept. The slug set follows
 * `projects`: every root row sharing the active store directory (a migrated row's
 * `db_path` folder, a pre-migration row's `data_dir`), so the live legacy alias
 * is accepted too. An empty `activeDir`, or one no root row matches, keeps every
 * row — the previous behavior, never a blank list.
 */
export function plansFromOverview(reply: unknown, activeDir?: string | null): { plans: PlanRow[]; runs: RunRow[] } {
  const plans: PlanRow[] = []
  const runs: RunRow[] = []
  if (!isRecord(reply) || !Array.isArray(reply.plans)) return { plans, runs }
  const wanted = activeSlugs(reply, activeDir)
  for (const raw of reply.plans) {
    if (!isRecord(raw) || typeof raw.plan !== 'string' || raw.plan === '') continue
    const project = str(raw.project)
    if (wanted != null && !wanted.has(project)) continue
    const live = typeof raw.live_run === 'string' && raw.live_run !== '' ? raw.live_run : null
    const row: PlanRow = {
      id: raw.plan,
      title: str(raw.title),
      status: str(raw.status),
      created: str(raw.created),
      runs: [],
      live_run: live,
      counts: { tasks: sumCounts(raw.counts), phases: 0, byStatus: statusCounts(raw.counts) },
      ...(project !== '' ? { project } : {}),
    }
    plans.push(row)
    if (live != null) {
      const buckets = bucketCounts(raw.counts)
      runs.push({
        id: live,
        plan: row.id,
        title: row.title,
        stage: str(raw.stage),
        counts: buckets,
        countsLine: countsLineFrom(raw.counts),
      })
    }
  }
  return { plans, runs }
}

/** The slugs of the root rows that name the active store dir (L7.2 R2). null when
 *  there is nothing to filter by — an empty `activeDir`, a reply with no `projects`,
 *  or a dir no row matches — and the caller then keeps every row. */
function activeSlugs(reply: Record<string, unknown>, activeDir: string | null | undefined): Set<string> | null {
  const dir = typeof activeDir === 'string' ? activeDir.trim() : ''
  if (dir === '' || !Array.isArray(reply.projects)) return null
  const out = new Set<string>()
  for (const raw of reply.projects) {
    if (!isRecord(raw)) continue
    // Match both the migrated store dir (from db_path) and the pre-migration data_dir.
    // activeDir is the data_dir; projectStoreDir returns the db_path folder for
    // migrated rows, so we check both.
    const storeDir = projectStoreDir(raw)
    const dataDir = typeof raw.data_dir === 'string' ? raw.data_dir.trim() : ''
    if (storeDir !== dir && dataDir !== dir) continue
    const slug = str(raw.slug)
    if (slug !== '') out.add(slug)
  }
  return out.size > 0 ? out : null
}


/** integration is a branch name. mkdb on main returns a string; a captured
 *  store from before that change returned a one-item list. Both map. */
function branchOf(value: unknown): string {
  if (typeof value === 'string') return value
  if (Array.isArray(value) && typeof value[0] === 'string') return value[0]
  return ''
}

const emptyPlan = (): PlanView => ({ mock: false, footer: [] })

/**
 * Detail reply to one PlanView. mock is always false. A reply that is not an
 * object, or that has no string id, is an empty view.
 */
export function planFromReply(reply: unknown): PlanView {
  if (!isRecord(reply) || typeof reply.id !== 'string') return emptyPlan()
  const phasesRaw = Array.isArray(reply.phases) ? reply.phases : []
  const phases: PlanPhase[] = []
  let tasksInPhases = 0
  for (const raw of phasesRaw) {
    if (!isRecord(raw) || typeof raw.id !== 'string') continue
    const status = str(raw.status)
    const rows: PlanTask[] = []
    if (Array.isArray(raw.tasks)) {
      for (const task of raw.tasks) {
        const mapped = taskOf(task)
        if (mapped == null) continue
        rows.push(mapped)
        tasksInPhases += 1
      }
    }
    const wave = typeof raw.wave === 'number' ? raw.wave : undefined
    phases.push({
      id: raw.id,
      title: str(raw.title),
      status,
      ...(wave !== undefined ? { wave } : {}),
      header: status,
      rows,
      ...routeFields(raw),
    })
  }
  const summed = sumCounts(reply.counts)
  const taskCount = summed > 0 ? summed : tasksInPhases
  const status = str(reply.status)
  return {
    mock: false,
    plan: {
      id: reply.id,
      title: str(reply.title),
      status,
      statusChip: status,
      created: str(reply.created),
      runs: strList(reply.runs).join(' '),
      mergesTo: branchOf(reply.integration),
      design: Array.isArray(reply.design) ? strList(reply.design).join(' ') : '',
      // C5/L7.2: a DONE plan's round files left the project folder for the legacy
      // tarball the migration wrote, so the Detail names that instead of letting them
      // read as absent. Any other plan keeps them in its project folder (the running
      // plan renders exactly as before), so no row is added.
      ...(status === 'done' ? { roundFiles: 'archived · legacy tarball' } : {}),
      counts: `${taskCount} tasks · ${phases.length} phases`,
    },
    phases,
    footer: [`phases ${phases.length}`],
  }
}

/** One short line for a failed plans read. Never the raw traceback. */
export function plansErrorLine(kind: 'exit' | 'json' | 'ok' | 'spawn' | 'timeout', detail?: string): string {
  if (kind === 'timeout') return 'plans read timed out'
  if (kind === 'spawn') return 'plans read failed: cannot run mkdb'
  if (kind === 'json') return 'plans read failed: not JSON'
  if (kind === 'ok') {
    const why = (detail ?? '').replace(/\s+/g, ' ').trim().slice(0, 60)
    return why === '' ? 'plans read failed' : `plans read failed: ${why}`
  }
  return 'plans read failed: mkdb exited'
}
