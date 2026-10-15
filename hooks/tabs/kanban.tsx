// The /mk pane's Kanban tab (plan mk-store, D8A, contract C4/C1): seven columns
// from the `col` key `mkdb.py cards` returns — never recomputed here. A card
// shows project, plan/run, task id + title, phase (parent), agent/model, lane,
// round, time in status, the blocked question, last verdict and gates. Attention
// statuses (blocked, stalled, quota, provider) keep a red badge in place.
//
// Pure render. Live rows arrive through an injected runner (the plugin test
// runtime cannot spawn); with no root store the tab draws "no store yet" and
// never calls mkdb. Narrow layouts follow the other tabs: 46 is the card width,
// under NARROW (36) each card is one clipped line plus an unwrapped badge.
import type { Elements, RenderElement } from 'claude-code'
import { NARROW, STATUS_COLOR } from './ui'
import { Chip } from './kit'
import { KANBAN_COLUMNS, KANBAN_FIXTURE } from './kanban.fixture'
import type { KanbanCard } from './kanban.fixture'

type Els = Elements[keyof Elements]

export type { KanbanCard }

/** What one `mkdb.py cards --json` stdout looks like. */
export type CardsPayload = { ok?: boolean; cards?: readonly KanbanCard[]; error?: string }

/** Injected process runner, same shape as settings-io RunFn. */
export type RunFn = (
  argv: readonly string[],
  init?: { timeoutMs?: number },
) => Promise<{ stdout?: string; code?: number }>

export type KanbanFilters = {
  needsYou: boolean
  project: string | null
  /** When a non-empty string, keep cards whose `run` is this id or `<id>-…`. */
  plan?: string | null
}

export type KanbanView = {
  mock: boolean
  empty: boolean
  columns: readonly string[]
  cards: readonly KanbanCard[]
  projects: readonly string[]
  filters: KanbanFilters
  /** set when the last cards call was slower than 100 ms (read every 2nd tick) */
  slow: boolean
  /** one short line when the last cards read failed; last good cards stay */
  error: string | null
  /** true when `error` is set and the cards shown are from an earlier good read */
  stale: boolean
}

const ATTENTION = ['blocked', 'stalled', 'quota', 'provider'] as const

export function isAttention(status: string): boolean {
  return (ATTENTION as readonly string[]).includes(status)
}

/** Column from the card's `col` key. Unknown columns are dropped, not remapped. */
export function columnOf(card: KanbanCard): string {
  return card.col
}

/** The root store's `projects` row (`mkdb.py overview --json`): the linkage k follows.
 *  A set `db_path` marks a migrated project (its store is `<dirname(db_path)>/mk.db`);
 *  a NULL one is pre-migration and is read at its `data_dir` (C3). */
export type ProjectRow = { slug?: unknown; repo?: unknown; data_dir?: unknown; db_path?: unknown }

/** The root store's `plan_summary` row, as `overview --json` returns it. */
export type OverviewReply = { ok?: boolean; projects?: readonly ProjectRow[]; plans?: readonly unknown[] }

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

/** The pane's status → column map, for cards built from a per-project task read
 *  (`run-snapshot`) rather than the retired root `cards` table. Attention statuses keep
 *  their own column and are badged; an unknown status falls to Backlog, never dropped. */
const STATUS_COLUMN: Readonly<Record<string, string>> = {
  planned: 'Backlog', routed: 'Backlog', ready: 'Ready',
  dispatched: 'Building', building: 'Building', takeover: 'Building',
  verifying: 'Verify', review: 'Review', fix: 'Fix',
  merged: 'Done', qa: 'Done', done: 'Done',
  blocked: 'Building', stalled: 'Building', quota: 'Building', provider: 'Building',
}

/** One task row from `mkdb.py run-snapshot --json` onto a Kanban card. The reader returns
 *  the run document's tasks; the column and the attention flag are the pane's own rule
 *  (STATUS_COLUMN), since that read has no `col`. */
export function cardOfTask(project: string, runId: string, task: Record<string, unknown>): KanbanCard {
  const status = typeof task.status === 'string' ? task.status : ''
  const route = task.route_json ?? (task.agent != null || task.model != null
    ? JSON.stringify({ agent: task.agent ?? undefined, model: task.model ?? undefined })
    : null)
  return {
    project,
    run: runId,
    task: typeof task.id === 'string' ? task.id : '',
    parent: typeof task.parent === 'string' && task.parent !== '' ? task.parent : null,
    type: typeof task.type === 'string' ? task.type : 'task',
    title: typeof task.title === 'string' ? task.title : '',
    status,
    col: STATUS_COLUMN[status] ?? 'Backlog',
    attention: isAttention(status) ? 1 : 0,
    question: typeof task.question === 'string' && task.question !== '' ? task.question : null,
    route_json: typeof route === 'string' ? route : null,
    lane: typeof task.lane === 'string' && task.lane !== '' ? task.lane : null,
    round: typeof task.round === 'number' ? task.round : null,
    verdict: typeof task.verdict === 'string' && task.verdict !== '' ? task.verdict : null,
    gates: typeof task.gates === 'string' && task.gates !== '' ? task.gates : null,
    updated: typeof task.updated === 'string' ? task.updated : '',
    change_no: typeof task.change_no === 'number' ? task.change_no : 0,
  }
}

/** The open tasks of one `run-snapshot` reply onto cards. Only the run's non-settled tasks
 *  are "open"; a merged/done task is not a board card (the spec's "open tasks"). */
export function cardsOfSnapshot(project: string, reply: unknown): KanbanCard[] {
  if (reply == null || typeof reply !== 'object' || !('run' in reply)) return []
  const run = reply.run
  if (run == null || typeof run !== 'object') return []
  const runId = 'id' in run && typeof run.id === 'string' ? run.id : ''
  const tasks = 'tasks' in run ? run.tasks : undefined
  if (!Array.isArray(tasks)) return []
  const out: KanbanCard[] = []
  for (const task of tasks) {
    if (task == null || typeof task !== 'object' || Array.isArray(task)) continue
    const card = cardOfTask(project, runId, task as Record<string, unknown>)
    if (card.task === '') continue
    if (card.status === 'merged' || card.status === 'done') continue
    out.push(card)
  }
  return out
}

export function applyFilters(cards: readonly KanbanCard[], filters: KanbanFilters): KanbanCard[] {
  return cards.filter(card => {
    if (filters.needsYou && card.attention !== 1 && !isAttention(card.status)) return false
    if (filters.project != null && filters.project !== '' && card.project !== filters.project) return false
    const plan = filters.plan
    if (plan != null && plan !== '' && card.run !== plan && !card.run.startsWith(`${plan}-`)) return false
    return true
  })
}

function routeOf(card: KanbanCard): { agent: string; model: string } | null {
  if (card.route_json == null || card.route_json === '') return null
  try {
    const parsed = JSON.parse(card.route_json) as { agent?: unknown; model?: unknown }
    const agent = typeof parsed.agent === 'string' ? parsed.agent : ''
    const model = typeof parsed.model === 'string' ? parsed.model : ''
    if (agent === '' && model === '') return null
    return { agent, model }
  } catch {
    return null
  }
}

/** Minutes between `updated` and `nowIso`. Unparseable clocks yield an em dash. */
export function timeInStatus(updated: string, nowIso: string): string {
  const a = Date.parse(updated)
  const b = Date.parse(nowIso)
  if (!Number.isFinite(a) || !Number.isFinite(b)) return '—'
  const mins = Math.max(0, Math.round((b - a) / 60000))
  if (mins < 60) return `${String(mins)}m`
  const hours = Math.floor(mins / 60)
  if (hours < 48) return `${String(hours)}h`
  return `${String(Math.floor(hours / 24))}d`
}

export function kanbanView(cards: readonly KanbanCard[], filters: KanbanFilters, nowIso: string, slow = false): KanbanView {
  const filtered = applyFilters(cards, filters)
  const projects = [...new Set(cards.map(c => c.project))].sort()
  return {
    mock: false,
    empty: false,
    columns: KANBAN_COLUMNS,
    cards: filtered.map(card => ({ ...card, updated: card.updated })),
    projects,
    filters,
    slow,
    error: null,
    stale: false,
  }
}

/** Fixture view the tests render. `now` is fixed so time-in-status is stable. */
export const KANBAN_NOW = '2026-10-12T10:00:00+02:00'

export function kanbanFixtureView(filters: KanbanFilters = { needsYou: false, project: null }, cards: readonly KanbanCard[] = KANBAN_FIXTURE): KanbanView {
  return { ...kanbanView(cards, filters, KANBAN_NOW), mock: true }
}

export function emptyKanbanView(): KanbanView {
  return { mock: false, empty: true, columns: KANBAN_COLUMNS, cards: [], projects: [], filters: { needsYou: false, project: null }, slow: false, error: null, stale: false }
}

const cut = (text: string, w: number): string => {
  if (w <= 0) return ''
  if (text.length <= w) return text
  if (w === 1) return '…'
  return `${text.slice(0, w - 1)}…`
}

/** One display line for a card. The attention badge is a separate Chip, never folded into this string. */
export function cardLine(card: KanbanCard, nowIso: string, width: number): string {
  const route = routeOf(card)
  const agent = route == null ? '—' : `${route.agent}/${route.model}`
  const parts = [
    card.project,
    card.run,
    `${card.task} ${card.title}`,
    card.parent ?? '—',
    agent,
    card.lane ?? '—',
    card.round == null ? 'r—' : `r${String(card.round)}`,
    timeInStatus(card.updated, nowIso),
    card.question ?? '',
    card.verdict ?? '',
    card.gates ?? '',
  ].filter(part => part !== '')
  return cut(parts.join(' · '), width)
}

const BADGE = '! '

function cardRow(els: Els, card: KanbanCard, nowIso: string, width: number): RenderElement {
  const { Box, Text } = els
  const badge = isAttention(card.status) || card.attention === 1
  const badgeW = badge ? BADGE.length : 0
  const line = cardLine(card, nowIso, Math.max(1, width - badgeW))
  const color = badge ? 'text' : (STATUS_COLOR[card.status] ?? 'text')
  return (
    <Box key={`${card.project}/${card.run}/${card.task}`} flexDirection="row">
      {badge ? <Text color="red" bold={true} dimColor={false}>{BADGE}</Text> : null}
      <Text color={color} wrap="truncate">{line}</Text>
    </Box>
  )
}

/** Pure render. `nowIso` defaults to the fixture clock so tests stay stable. */
export function KanbanTab(els: Els, view: KanbanView, cols: number, nowIso: string = KANBAN_NOW): RenderElement {
  const { Box, Text } = els
  const width = Math.max(8, cols)
  if (view.empty) {
    return (
      <Box flexDirection="column" width={cols}>
        <Text>no store yet</Text>
      </Box>
    )
  }
  const compact = cols < NARROW
  const errorLine = view.error == null || view.error === ''
    ? null
    : cut(`⚠ ${view.stale ? 'stale · ' : ''}${view.error}`, width)
  const inner = Math.max(8, width - (compact ? 0 : 2))
  return (
    <Box flexDirection="column" width={cols}>
      {view.mock ? <Text bold={true} color="warning">MOCK · fixture data · not this run</Text> : null}
      {errorLine != null ? <Text color="yellow" bold={true}>{errorLine}</Text> : null}
      <Text dimColor={true}>{cut(
        `Needs you ${view.filters.needsYou ? 'on' : 'off'} · project ${view.filters.project ?? 'all'}${view.slow ? ' · slow' : ''}`,
        width,
      )}</Text>
      {(view.error != null && view.cards.length === 0 ? [] : view.columns).map(col => {
        const rows = view.cards.filter(card => columnOf(card) === col)
        const head = `${col} ${String(rows.length)}`
        return (
          <Box key={col} flexDirection="column">
            <Text bold={true}>{cut(head, width)}</Text>
            {rows.map(card => cardRow(els, card, nowIso, inner))}
          </Box>
        )
      })}
    </Box>
  )
}

export type CardsRead = {
  cards: KanbanCard[]
  ms: number
  skipped: boolean
  empty: boolean
  /** set on a failed read; cards stay empty so the caller keeps the last good set */
  error: string | null
}

/** One short line for a failed cards read. Never the raw traceback. */
export function cardsErrorLine(kind: 'exit' | 'json' | 'ok' | 'spawn' | 'timeout', detail?: string): string {
  if (kind === 'timeout') return 'cards read timed out'
  if (kind === 'spawn') return 'cards read failed: cannot run mkdb'
  if (kind === 'json') return 'cards read failed: not JSON'
  if (kind === 'ok') {
    const why = (detail ?? '').replace(/\s+/g, ' ').trim().slice(0, 60)
    return why === '' ? 'cards read failed' : `cards read failed: ${why}`
  }
  return 'cards read failed: mkdb exited'
}

/**
 * One cards read for a dashboard tick: the top-level `k` (L7.2).
 *
 * C3: the root store holds only numbers and linkage. Each linked project's open
 * tasks come from that project's own store, found through its root row —
 * `<dirname(db_path)>/mk.db` for a migrated project (the C2 folder), the
 * recorded `data_dir` for a pre-migration one. So this read is: one
 * `mkdb.py overview --json` against the root, then one per-project read for
 * each row that names a store (L5's read-only `run-snapshot`).
 *
 * `homeHasStore` must be checked by the caller (or passed false): a missing
 * root store returns empty and does not invoke `run`, so mkdb cannot create it.
 * A per-project read that fails is skipped — one broken project must not blank
 * the whole board; the error is reported only when NO project read succeeded.
 * When the previous read took longer than 100 ms, every other tick is skipped.
 */
export async function readCards(
  run: RunFn,
  pluginRoot: string,
  home: string | null,
  homeHasStore: boolean,
  tick: number,
  lastMs: number,
  overview?: { doc: unknown; failure: { kind: 'exit' | 'json' | 'ok' | 'spawn' | 'timeout'; detail: string } | null },
): Promise<CardsRead> {
  const fail = (error: string, ms: number): CardsRead => ({ cards: [], ms, skipped: false, empty: false, error })
  if (home == null || !homeHasStore) return { cards: [], ms: 0, skipped: true, empty: true, error: null }
  if (lastMs > 100 && tick % 2 === 1) return { cards: [], ms: lastMs, skipped: true, empty: false, error: null }
  const started = Date.now()
  const call = async (argv: string[]): Promise<{ parsed: unknown; error: string | null }> => {
    let result: { stdout?: string; code?: number }
    try {
      result = await run(argv, { timeoutMs: 5_000 })
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      return { parsed: null, error: /timed out|timeout/i.test(msg) ? cardsErrorLine('timeout') : cardsErrorLine('spawn') }
    }
    if (result.code !== 0 && result.code != null) return { parsed: null, error: cardsErrorLine('exit') }
    const stdout = result.stdout ?? ''
    if (stdout.trim() === '') return { parsed: null, error: cardsErrorLine('json') }
    try {
      const parsed = JSON.parse(stdout) as { ok?: boolean; error?: string }
      if (parsed == null || typeof parsed !== 'object' || parsed.ok !== true) {
        return { parsed: null, error: cardsErrorLine('ok', typeof parsed?.error === 'string' ? parsed.error : '') }
      }
      return { parsed, error: null }
    } catch {
      return { parsed: null, error: cardsErrorLine('json') }
    }
  }
  const mkdb = `${pluginRoot}/scripts/mkdb.py`
  const overviewGot = overview != null
    ? (overview.failure != null
      ? { parsed: null, error: cardsErrorLine(overview.failure.kind, overview.failure.detail) }
      : { parsed: overview.doc, error: null })
    : await call(['python3', mkdb, '--home', home, 'overview', '--json'])
  if (overviewGot.error != null) return fail(overviewGot.error, Date.now() - started)
  const projects = overviewGot.parsed != null && typeof overviewGot.parsed === 'object' && 'projects' in overviewGot.parsed
    ? overviewGot.parsed.projects
    : undefined
  const rows: readonly ProjectRow[] = Array.isArray(projects)
    ? projects.filter((r): r is ProjectRow => r != null && typeof r === 'object' && !Array.isArray(r))
    : []
  const cards: KanbanCard[] = []
  let failures = 0
  let attempted = 0
  let firstError: string | null = null
  // L7.2 R2: TWO root rows can name ONE store — the live root has both `matrixkoder`
  // and the legacy alias slug, sharing a `db_path`. Reading per ROW would spawn
  // `run-snapshot` twice and push every task twice, so group by store directory and
  // read once per distinct store. `labelRow` chooses the slug those cards carry: the
  // row whose slug IS the project name (basename of `repo`), else the first row.
  const groups = storeGroups(rows)
  for (const group of groups) {
    attempted += 1
    const got = await call(['python3', mkdb, '--data', group.dir, 'run-snapshot', '--json'])
    if (got.error != null) {
      failures += 1
      if (firstError == null) firstError = got.error
      continue
    }
    cards.push(...cardsOfSnapshot(group.slug, got.parsed))
  }
  const ms = Date.now() - started
  if (attempted > 0 && failures === attempted) return fail(firstError ?? cardsErrorLine('exit'), ms)
  // A successful read is never the empty-store state: zero open tasks still draws the
  // empty board (the columns), exactly as the retired root `cards` read did.
  return { cards, ms, skipped: false, empty: false, error: null }
}
