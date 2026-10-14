// Pure mappers from mkdb plans JSON to the Plan tab view types. No I/O.
import type { PlanRow, RunRow } from './plans'
import type { PlanPhase, PlanRoute, PlanSub, PlanTask, PlanView } from './plan'

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
      counts: { tasks: sumCounts(raw.counts), phases: 0 },
      ...(Array.isArray(raw.design) ? { design: strList(raw.design) } : {}),
      ...(branchOf(raw.integration) !== '' ? { mergesTo: branchOf(raw.integration) } : {}),
    }
    plans.push(row)
    if (live != null) {
      runs.push({
        id: live,
        plan: row.id,
        title: row.title,
        stage: '',
        counts: { done: 0, live: 0, planned: 0 },
        countsLine: '',
      })
    }
  }
  return { plans, runs }
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
