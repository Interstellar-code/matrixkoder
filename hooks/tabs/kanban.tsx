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
 * One cards read for a dashboard tick.
 * `homeHasStore` must be checked by the caller (or passed false): a missing
 * root store returns empty and does not invoke `run`, so mkdb cannot create it.
 * A non-zero exit, non-JSON stdout, `{"ok": false}`, a thrown spawn or a
 * timeout sets `error` and returns no cards (the caller keeps the last good
 * set). When the previous read took longer than 100 ms, every other tick is
 * skipped.
 */
export async function readCards(
  run: RunFn,
  pluginRoot: string,
  home: string | null,
  homeHasStore: boolean,
  tick: number,
  lastMs: number,
): Promise<CardsRead> {
  const fail = (error: string, ms: number): CardsRead => ({ cards: [], ms, skipped: false, empty: false, error })
  if (home == null || !homeHasStore) return { cards: [], ms: 0, skipped: true, empty: true, error: null }
  if (lastMs > 100 && tick % 2 === 1) return { cards: [], ms: lastMs, skipped: true, empty: false, error: null }
  const argv = ['python3', `${pluginRoot}/scripts/mkdb.py`, '--home', home, 'cards', '--json']
  const started = Date.now()
  let result: { stdout?: string; code?: number }
  try {
    result = await run(argv, { timeoutMs: 5_000 })
  } catch (err) {
    const ms = Date.now() - started
    const msg = err instanceof Error ? err.message : String(err)
    if (/timed out|timeout/i.test(msg)) return fail(cardsErrorLine('timeout'), ms)
    return fail(cardsErrorLine('spawn'), ms)
  }
  const ms = Date.now() - started
  if (result.code !== 0 && result.code != null) return fail(cardsErrorLine('exit'), ms)
  const stdout = result.stdout ?? ''
  if (stdout.trim() === '') return fail(cardsErrorLine('json'), ms)
  let parsed: CardsPayload
  try {
    parsed = JSON.parse(stdout) as CardsPayload
  } catch {
    return fail(cardsErrorLine('json'), ms)
  }
  if (parsed == null || typeof parsed !== 'object' || parsed.ok !== true) {
    return fail(cardsErrorLine('ok', typeof parsed?.error === 'string' ? parsed.error : ''), ms)
  }
  const cards = Array.isArray(parsed.cards) ? [...parsed.cards] : []
  return { cards, ms, skipped: false, empty: false, error: null }
}
