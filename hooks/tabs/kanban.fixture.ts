// Fixture card set for the Kanban tab (D8A). Shapes match `mkdb.py cards --json`
// rows: project, run, task, parent, type, title, status, col, attention,
// question, route_json, lane, round, verdict, gates, updated, change_no.
// `KANBAN_AFTER` is the same set after one transition (D3A building → verifying),
// so a render test can show the card leaving Building for Verify.

export type KanbanCard = {
  project: string
  run: string
  task: string
  parent: string | null
  type: string
  title: string
  status: string
  col: string
  attention: number
  question: string | null
  route_json: string | null
  lane: string | null
  round: number | null
  verdict: string | null
  gates: string | null
  updated: string
  change_no: number
}

export const KANBAN_COLUMNS = ['Backlog', 'Ready', 'Building', 'Verify', 'Review', 'Fix', 'Done'] as const

const route = (agent: string, model: string): string =>
  JSON.stringify({ agent, model, effort: 'medium' })

export const KANBAN_FIXTURE: readonly KanbanCard[] = [
  {
    project: 'alpha', run: 'mk-store-r1', task: 'D1', parent: 'D1', type: 'task',
    title: 'Root store', status: 'planned', col: 'Backlog', attention: 0,
    question: null, route_json: route('opencode', 'auto'), lane: null, round: null,
    verdict: null, gates: null, updated: '2026-10-12T09:00:00+02:00', change_no: 1,
  },
  {
    project: 'alpha', run: 'mk-store-r1', task: 'D2', parent: 'D2', type: 'task',
    title: 'Project store', status: 'ready', col: 'Ready', attention: 0,
    question: null, route_json: route('claude', 'opus'), lane: 'lane-a', round: 1,
    verdict: null, gates: null, updated: '2026-10-12T09:10:00+02:00', change_no: 2,
  },
  {
    project: 'alpha', run: 'mk-store-r1', task: 'D3A', parent: 'D3', type: 'task',
    title: 'Transition writer', status: 'building', col: 'Building', attention: 0,
    question: null, route_json: route('opencode', 'auto'), lane: 'lane-b', round: 1,
    verdict: null, gates: 'tsc pending', updated: '2026-10-12T09:40:00+02:00', change_no: 3,
  },
  {
    project: 'beta', run: 'pay-r2', task: 'P4', parent: 'P4', type: 'task',
    title: 'Charge path', status: 'blocked', col: 'Building', attention: 1,
    question: 'which currency?', route_json: route('codex', 'gpt'), lane: 'lane-c', round: 2,
    verdict: 'needs-decision', gates: 'selftest fail', updated: '2026-10-12T08:00:00+02:00', change_no: 4,
  },
  {
    project: 'beta', run: 'pay-r2', task: 'P5', parent: 'P5', type: 'task',
    title: 'Receipts', status: 'verifying', col: 'Verify', attention: 0,
    question: null, route_json: route('agy', 'gemini'), lane: 'lane-d', round: 1,
    verdict: null, gates: '2/3', updated: '2026-10-12T09:20:00+02:00', change_no: 5,
  },
  {
    project: 'alpha', run: 'mk-store-r1', task: 'D7S', parent: 'D7', type: 'task',
    title: 'Settings tab', status: 'review', col: 'Review', attention: 0,
    question: null, route_json: route('claude', 'sonnet'), lane: 'lane-e', round: 1,
    verdict: 'pass', gates: 'ok', updated: '2026-10-12T09:30:00+02:00', change_no: 6,
  },
  {
    project: 'gamma', run: 'docs-r1', task: 'F1', parent: 'F1', type: 'task',
    title: 'Fix the index', status: 'fix', col: 'Fix', attention: 0,
    question: null, route_json: null, lane: 'lane-f', round: 2,
    verdict: 'fail', gates: 'lint', updated: '2026-10-12T07:00:00+02:00', change_no: 7,
  },
  {
    project: 'gamma', run: 'docs-r1', task: 'F0', parent: null, type: 'phase',
    title: 'Docs phase', status: 'done', col: 'Done', attention: 0,
    question: null, route_json: route('opencode', 'auto'), lane: null, round: 1,
    verdict: 'pass', gates: 'ok', updated: '2026-10-12T06:00:00+02:00', change_no: 8,
  },
  {
    project: 'alpha', run: 'mk-store-r1', task: 'D5A', parent: 'D5', type: 'task',
    title: 'Lane watch', status: 'stalled', col: 'Verify', attention: 1,
    question: null, route_json: route('opencode', 'auto'), lane: 'lane-g', round: 1,
    verdict: null, gates: null, updated: '2026-10-12T05:00:00+02:00', change_no: 9,
  },
  {
    project: 'beta', run: 'pay-r2', task: 'P6', parent: 'P6', type: 'task',
    title: 'Quota wait', status: 'quota', col: 'Review', attention: 1,
    question: null, route_json: route('claude', 'opus'), lane: 'lane-h', round: 3,
    verdict: null, gates: null, updated: '2026-10-12T04:00:00+02:00', change_no: 10,
  },
  {
    project: 'gamma', run: 'docs-r1', task: 'F2', parent: 'F2', type: 'task',
    title: 'Provider down', status: 'provider', col: 'Ready', attention: 1,
    question: 'retry which host', route_json: route('codex', 'gpt'), lane: 'lane-i', round: 1,
    verdict: null, gates: null, updated: '2026-10-12T03:00:00+02:00', change_no: 11,
  },
]

/** D3A moved building → verifying. Column comes from the fixture, not recomputed. */
export const KANBAN_AFTER: readonly KanbanCard[] = KANBAN_FIXTURE.map(card =>
  card.task === 'D3A'
    ? { ...card, status: 'verifying', col: 'Verify', change_no: card.change_no + 100, updated: '2026-10-12T10:00:00+02:00' }
    : card,
)
