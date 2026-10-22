// The Bugs tab's Kanban (plan bugs-tab, UI.3): the Plan tab's board with a bug
// config — OPEN · FIXING · NEEDS YOU · FIXED, the kind filter row in place of
// the phase row — and the mk-bug `list` reply mapped onto its PlanKanbanView.
// Pure: no I/O; a malformed reply maps to an empty view.
import { STATUS_TO_COLUMN, timeInStatus } from './plankanban'
import type { KanbanConfig, PlanKanbanCard, PlanKanbanView } from './plankanban'

/** Bug kinds in the filter row's fixed order (contract: extra_json.bug.kind). */
const KINDS: readonly string[] = ['frontend', 'backend', 'cli', 'store', 'infra', 'docs', 'other']

/** The plan's status map with the plan's labels swapped for the bug ones;
 *  `paused` is the bugs-only addition (a bug on hold needs the user). A wontfix
 *  bug is task_status done: FIXED. */
const RELABEL: Readonly<Record<string, string>> = {
  PLANNED: 'OPEN', BUILDING: 'FIXING', 'NEEDS YOU': 'NEEDS YOU', 'ON MAIN': 'FIXED',
}
const BUG_STATUS_TO_COLUMN: Readonly<Record<string, string>> = {
  ...Object.fromEntries(Object.entries(STATUS_TO_COLUMN).map(([status, col]) => [status, RELABEL[col] ?? 'OPEN'])),
  paused: 'NEEDS YOU',
}

export const BUG_KANBAN_CONFIG: KanbanConfig = {
  labels: ['OPEN', 'FIXING', 'NEEDS YOU', 'FIXED'],
  statusMap: BUG_STATUS_TO_COLUMN,
  footer: shown => `${String(shown)} bugs · this repo's bugs`,
  allLabel: 'all kinds',
  hint: '',
  phaseKey: 'bugs:kind',
  taskKey: 'bugs:task',
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const str = (value: unknown): string => (typeof value === 'string' ? value : '')

/** The `mk-bug list` reply onto the board's view: one card per bug (status =
 *  task_status, phase = kind, no verdict), `phases` the kinds present in the
 *  contract's order (an unknown kind counts as `other`). The dim tag is the
 *  lane, else `done/total tasks`. */
export function bugKanbanView(listReply: unknown, nowIso = ''): PlanKanbanView {
  const cards: PlanKanbanCard[] = []
  const unknownStatuses: string[] = []
  if (!isRecord(listReply) || listReply.ok !== true || !Array.isArray(listReply.bugs)) {
    return { planId: '', phases: [], cards, unknownStatuses }
  }
  for (const raw of listReply.bugs) {
    if (!isRecord(raw) || typeof raw.id !== 'string') continue
    const status = str(raw.task_status)
    if (status !== '' && !(status in BUG_STATUS_TO_COLUMN) && !unknownStatuses.includes(status)) unknownStatuses.push(status)
    const updated = str(raw.updated)
    const age = updated === '' || nowIso === '' ? '' : timeInStatus(updated, nowIso)
    const lane = str(raw.lane)
    const tasks = isRecord(raw.tasks) ? raw.tasks : {}
    const note = lane !== '' ? lane
      : typeof tasks.done === 'number' && typeof tasks.total === 'number' && tasks.total > 0 ? `${String(tasks.done)}/${String(tasks.total)} tasks` : ''
    const severity = str(raw.severity)
    const kind = str(raw.kind)
    cards.push({
      id: raw.id,
      title: str(raw.title),
      status,
      phase: KINDS.includes(kind) ? kind : 'other',
      verdict: null,
      age: age === '—' ? '' : age,
      ...(severity !== '' ? { severity } : {}),
      ...(note !== '' ? { note } : {}),
    })
  }
  const present = new Set(cards.map(card => card.phase))
  return { planId: str(listReply.project), phases: KINDS.filter(kind => present.has(kind)), cards, unknownStatuses }
}
