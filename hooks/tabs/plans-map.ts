// Pure mappers from mkdb plans JSON to the Plan tab view types. No I/O.
import { isBugsRun } from '../data'
import type { PlanRow, RunRow } from './plans'
import type { PlanPhase, PlanRoute, PlanSub, PlanTask, PlanView } from './plan'

export type PlansListReply = { ok?: boolean; plans?: unknown; error?: string }
export type PlanDetailReply = { ok?: boolean; id?: string; phases?: unknown; error?: string }

// the ONE store-directory rule (C3): a migrated row's `db_path` folder, else its
// `data_dir`. The Plan list, the Plan Kanban and the Todo tab agree on one store.
/** The root store's `projects` row (`mkdb.py overview --json`): the linkage Plans and Todo follow.
 *  A set `db_path` marks a migrated project (its store is `<dirname(db_path)>/mk.db`);
 *  a NULL one is pre-migration and is read at its `data_dir` (C3). */
export type ProjectRow = { slug?: unknown; repo?: unknown; data_dir?: unknown; db_path?: unknown }

/** Where a root row's project store lives (C3): the migrated C2 folder, else the recorded
 *  data dir. '' when the row names neither, so the caller cannot read a store that is not
 *  there. Pure string math — the row is the test's. */
export const projectStoreDir = (row: ProjectRow): string => {
  const dbPath = typeof row.db_path === 'string' ? row.db_path.trim() : ''
  if (dbPath !== '') {
    const cut = dbPath.lastIndexOf('/')
    return cut > 0 ? dbPath.slice(0, cut) : ''
  }
  return typeof row.data_dir === 'string' ? row.data_dir.trim() : ''
}

/** One distinct store directory and the slug its cards carry. */
export type StoreGroup = { dir: string; slug: string }

/** The slug a store's cards are labelled with: the row whose slug is the project
 *  NAME (basename of a non-empty `repo`), else the first row's slug, else ''. The
 *  live root carries both `matrixkoder` and a legacy alias for one store; the
 *  named row is the one a user recognises. */
const labelSlug = (group: readonly ProjectRow[]): string => {
  for (const row of group) {
    const repo = typeof row.repo === 'string' ? row.repo.trim() : ''
    const name = repo === '' ? '' : repo.replace(/\/+$/, '').split('/').pop() ?? ''
    if (name !== '' && typeof row.slug === 'string' && row.slug === name) return row.slug
  }
  const first = group[0]
  return typeof first?.slug === 'string' ? first.slug : ''
}

/** Group rows by their store directory (L7.2 R2): ONE read per distinct store, in
 *  first-seen order, so two rows sharing a `db_path` (the live alias) spawn one
 *  `run-snapshot`. Rows that name no store are dropped. Pure — the rows are the test's. */
export function storeGroups(rows: readonly ProjectRow[]): StoreGroup[] {
  const byDir = new Map<string, ProjectRow[]>()
  for (const row of rows) {
    const dir = projectStoreDir(row)
    if (dir === '') continue
    const group = byDir.get(dir)
    if (group == null) byDir.set(dir, [row])
    else group.push(row)
  }
  return [...byDir.entries()].map(([dir, group]) => ({ dir, slug: labelSlug(group) }))
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const str = (value: unknown): string => (typeof value === 'string' ? value : '')

const strList = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []

/** A string or null: empty text and other types are null (V1.5 fields are
 *  absent-or-value, never ''). */
const strOrNull = (value: unknown): string | null =>
  typeof value === 'string' && value !== '' ? value : null

/** A description: trimmed, and null when it is not a string or only blanks. */
const descOf = (value: unknown): string | null =>
  typeof value === 'string' && value.trim() !== '' ? value.trim() : null

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
  description?: string
  owned?: readonly string[]
  deps?: readonly string[]
  route?: PlanRoute | null
  plannedRoute?: PlanRoute | null
  reviewer?: string
  /** F.1 contract: the derived risk (int 1-5), its critical flag, and the open
   *  reasons. All optional — an old row carries none of them. */
  risk?: number
  critical?: boolean
  openReasons?: readonly { readonly kind: string; readonly points: number; readonly source: string; readonly opened: string }[]
} {
  const out: {
    slot?: number | null
    pane?: string | null
    branch?: string | null
    updated?: string | null
    acceptance?: string | null
    description?: string
    owned?: readonly string[]
    deps?: readonly string[]
    route?: PlanRoute | null
    plannedRoute?: PlanRoute | null
    reviewer?: string
    risk?: number
    critical?: boolean
    openReasons?: readonly { readonly kind: string; readonly points: number; readonly source: string; readonly opened: string }[]
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
  const description = descOf(raw.description)
  if (description != null) out.description = description
  const owned = strList(raw.owned)
  if (owned.length > 0) out.owned = owned
  const deps = strList(raw.deps)
  if (deps.length > 0) out.deps = deps
  const route = routeOf(raw.route)
  if (route != null) out.route = route
  const plannedRoute = routeOf(raw.planned_route)
  if (plannedRoute != null) out.plannedRoute = plannedRoute
  const reviewer = descOf(raw.reviewer)
  if (reviewer != null) out.reviewer = reviewer
  if (typeof raw.risk === 'number' && Number.isInteger(raw.risk) && raw.risk >= 1 && raw.risk <= 5) out.risk = raw.risk
  if (raw.critical === true) out.critical = true
  if (Array.isArray(raw.open_reasons)) {
    const reasons = raw.open_reasons.filter((r): r is { kind: string; points: number; source: string; opened: string } =>
      isRecord(r) && typeof r.kind === 'string' && typeof r.points === 'number' &&
      typeof r.source === 'string' && typeof r.opened === 'string')
    if (reasons.length > 0) out.openReasons = reasons
  }
  return out
}

/** A route as the words a person reads: `agy gemini-3.8-flash-high`. */
export const routeText = (route: PlanRoute): string =>
  [route.agent, route.model, route.effort].filter((p): p is string => p != null && p !== '').join(' ')

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
 *  the run-card counts and the plan-row per-status tally. ready and parked are
 *  still-to-start work; the stalled/quota/provider/rework/takeover states are
 *  live — a run needs the user. A status not in the table lands in planned:
 *  every task counts, nothing is dropped. */
const STATUS_BUCKET: Record<string, 'done' | 'live' | 'planned'> = {
  merged: 'done', done: 'done',
  dispatched: 'live', building: 'live', verifying: 'live', review: 'live', fix: 'live', qa: 'live',
  blocked: 'live', stalled: 'live', quota: 'live', provider: 'live', paused: 'live', rework: 'live', takeover: 'live',
  planned: 'planned', routed: 'planned', ready: 'planned', parked: 'planned',
}

/** True for a finished task (merged/done): its slot and route are history, the
 *  slot may already hold another task, so the plan tree shows no lane tag. */
export const isDoneStatus = (status: string): boolean => STATUS_BUCKET[status] === 'done'

/** Natural compare without ICU: digit runs compare as numbers, everything else by code point. */
export function naturalCompare(a: string, b: string): number {
  const pa = a.match(/\d+|\D+/g) ?? []
  const pb = b.match(/\d+|\D+/g) ?? []
  for (let i = 0; i < Math.min(pa.length, pb.length); i++) {
    const x = pa[i] as string
    const y = pb[i] as string
    if (x === y) continue
    const nx = /^\d/.test(x)
    const ny = /^\d/.test(y)
    if (nx && ny) {
      const d = Number(x) - Number(y)
      if (d !== 0) return d
    } else {
      const d = x < y ? -1 : 1
      return nx !== ny ? (nx ? -1 : 1) : d
    }
  }
  return pa.length - pb.length
}

/** Natural order by task id within one parent: F.2 < F.4 < F.10 < F.S. */
const byTaskId = (a: { id: string }, b: { id: string }): number => naturalCompare(a.id, b.id)

/** Sum the per-status counts into the run-card bucket totals. A status not in
 *  STATUS_BUCKET lands in planned: every task counts. */
function bucketCounts(counts: unknown): { done: number; live: number; planned: number } {
  const out = { done: 0, live: 0, planned: 0 }
  if (typeof counts !== 'object' || counts === null) return out
  for (const [status, n] of Object.entries(counts)) {
    if (typeof n !== 'number') continue
    out[STATUS_BUCKET[status] ?? 'planned'] += n
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
  const lastCommit = strOrNull(raw.last_commit)
  const findings = typeof raw.findings === 'number' && Number.isFinite(raw.findings) ? raw.findings : undefined
  subs.sort(byTaskId)
  return {
    id: raw.id, title: str(raw.title), status, right: status, subs,
    ...routeFields(raw),
    ...(type != null ? { type } : {}),
    ...(round !== undefined ? { round } : {}),
    ...(verdict != null ? { verdict } : {}),
    ...(lastCommit != null ? { last_commit: lastCommit } : {}),
    ...(findings !== undefined ? { findings } : {}),
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
    if (isBugsRun(raw.id)) continue // the permanent bugs run's row is not a plan
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
 * is accepted too. An empty (or absent) `activeDir` keeps every row — the
 * previous behavior; so does an overview whose `projects` lists nothing (a
 * pre-projects root). A non-empty `activeDir` and projects list that match
 * nothing keep none: the dir cannot name this store (Z20).
 */
export function plansFromOverview(reply: unknown, activeDir?: string | null): { plans: PlanRow[]; runs: RunRow[] } {
  const plans: PlanRow[] = []
  const runs: RunRow[] = []
  if (!isRecord(reply) || !Array.isArray(reply.plans)) return { plans, runs }
  const wanted = activeSlugs(reply, activeDir)
  for (const raw of reply.plans) {
    if (!isRecord(raw) || typeof raw.plan !== 'string' || raw.plan === '') continue
    if (isBugsRun(raw.plan)) continue // the permanent bugs run's row is not a plan
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

/** The slugs of the root rows that name the active store dir (L7.2 R2; Z20).
 *  null when there is nothing to filter by — an empty (or absent) activeDir,
 *  or a reply whose `projects` is empty or missing (a pre-projects root has
 *  listed nothing yet: no filter). A NON-EMPTY projects list that matches no
 *  row is the leak case: a dir that is not on the root can never name this
 *  store, so the EMPTY set stands and the caller keeps no row. */
function activeSlugs(reply: Record<string, unknown>, activeDir: string | null | undefined): Set<string> | null {
  const dir = typeof activeDir === 'string' ? activeDir.trim() : ''
  if (dir === '') return null
  if (!Array.isArray(reply.projects) || reply.projects.length === 0) return null
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
  return out
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
    rows.sort(byTaskId)
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
  const description = descOf(reply.description)
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
      ...(description != null ? { description } : {}),
    },
    phases,
    footer: [`${phases.length} phases · all folded by default`, 'source project'],
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
