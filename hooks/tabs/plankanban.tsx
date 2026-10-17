// The /mk pane's Plan tab Kanban sub-tab (plan settings-plans-db, V1.4,
// contract C15 — PlanKanban.dc.html): four columns (PLANNED · BUILDING ·
// NEEDS YOU · ON MAIN) of THIS plan's tasks, a phase filter row, and a dim
// footer. Pure render from the raw `plans <id>` reply — kept raw because the
// mapped PlanView (plans-map.ts) drops `verdict`, which the NEEDS YOU rule
// reads. The top-level `k` tab (KanbanTab) is untouched: only the Plan
// sub-tab changes.
import type { Elements, RenderElement } from 'claude-code'
import { NARROW } from './ui'
import { ActionButton, Card, Chip, Segment } from './kit'
import { PALETTE, ROLE } from './palette'
import type { PaletteRole } from './palette'
import { timeInStatus } from './kanban'
import { laneAgent, routeFields } from './plans-map'
import type { PlanRoute } from './plan'

type Els = Elements[keyof Elements]

/** C12: this tab's view-state handle — declared locally (structural typing; no
 *  import crosses lanes). */
type TabUi = { get(key: string): string | undefined; set(key: string, value: string): void }

/** The four columns, in the mockup's order. */
export const PLAN_KANBAN_COLUMNS = ['PLANNED', 'BUILDING', 'NEEDS YOU', 'ON MAIN'] as const
export type PlanKanbanColumn = typeof PLAN_KANBAN_COLUMNS[number]

/** C15: status → column. A status not here falls to PLANNED and is listed in
 *  the view's `unknownStatuses` for the report; a stored review verdict that
 *  asks for changes wins over the status and lands the card in NEEDS YOU
 *  (columnOfTask). `qa` is the user's gate: NEEDS YOU. */
export const STATUS_TO_COLUMN: Readonly<Record<string, PlanKanbanColumn>> = {
  planned: 'PLANNED', routed: 'PLANNED', ready: 'PLANNED',
  dispatched: 'BUILDING', building: 'BUILDING', verifying: 'BUILDING', review: 'BUILDING', fix: 'BUILDING',
  blocked: 'NEEDS YOU', stalled: 'NEEDS YOU', quota: 'NEEDS YOU', provider: 'NEEDS YOU', qa: 'NEEDS YOU',
  rework: 'NEEDS YOU',
  merged: 'ON MAIN', done: 'ON MAIN',
}

/** A stored review verdict asks for changes when it is not an approve. */
export function verdictNeedsChanges(verdict: string | null): boolean {
  if (verdict == null || verdict === '') return false
  return !verdict.toLowerCase().startsWith('approve')
}

/** One card: a task of this plan. `age` is precomputed by planKanbanView
 *  (`22m` / `8h` / `2d`); '' when the task carries no `updated`. V1.5 adds the
 *  routing fields (slot, route) the lane·agent line and the press read. */
export type PlanKanbanCard = {
  readonly id: string
  readonly title: string
  readonly status: string
  readonly phase: string
  readonly verdict: string | null
  readonly age: string
  /** raw task `findings` count (backend adds it); absent shows no chip */
  readonly findings?: number
  readonly slot?: number | null
  readonly route?: PlanRoute | null
  /** F.1 contract: derived risk (int 1-5) and its critical flag. Absent/unknown
   *  shows nothing — never a made-up badge. */
  readonly risk?: number
  readonly critical?: boolean
  readonly openReasons?: readonly { readonly kind: string; readonly points: number; readonly source: string; readonly opened: string }[]
}

/** The sub-tab's whole view: the plan's phases in plan order and its cards. */
export type PlanKanbanView = {
  readonly planId: string
  readonly phases: readonly string[]
  readonly cards: readonly PlanKanbanCard[]
  /** statuses not in STATUS_TO_COLUMN (they fall to PLANNED) — for the report */
  readonly unknownStatuses: readonly string[]
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const str = (value: unknown): string => (typeof value === 'string' ? value : '')

/** `22m` / `8h` / `2d` from `updated` against `nowIso`; '' when either side is
 *  missing or unparseable (KanbanTab's timeInStatus, dimmed to nothing). */
const ageOf = (updated: string, nowIso: string): string => {
  if (updated === '' || nowIso === '') return ''
  const got = timeInStatus(updated, nowIso)
  return got === '—' ? '' : got
}

/** The raw `plans <id>` reply onto the sub-tab's view. No I/O; a reply that is
 *  not an object with a string id maps to an empty view. */
export function planKanbanView(detail: unknown, nowIso = ''): PlanKanbanView {
  const cards: PlanKanbanCard[] = []
  const phases: string[] = []
  const unknownStatuses: string[] = []
  if (!isRecord(detail) || typeof detail.id !== 'string') {
    return { planId: '', phases, cards, unknownStatuses }
  }
  for (const raw of Array.isArray(detail.phases) ? detail.phases : []) {
    if (!isRecord(raw) || typeof raw.id !== 'string') continue
    phases.push(raw.id)
    for (const task of Array.isArray(raw.tasks) ? raw.tasks : []) {
      if (!isRecord(task) || typeof task.id !== 'string') continue
      const status = str(task.status)
      if (status !== '' && !(status in STATUS_TO_COLUMN) && !unknownStatuses.includes(status)) unknownStatuses.push(status)
      const verdictRaw = str(task.verdict)
      cards.push({
        id: task.id,
        title: str(task.title),
        status,
        phase: raw.id,
        verdict: verdictRaw === '' ? null : verdictRaw,
        age: ageOf(str(task.updated), nowIso),
        ...(typeof task.findings === 'number' && Number.isInteger(task.findings) && task.findings > 0 ? { findings: task.findings } : {}),
        // routeFields already maps risk (1-5), critical and open_reasons —
        // an old row out of the range or without them maps to absent.
        ...routeFields(task),
      })
    }
  }
  return { planId: detail.id, phases, cards, unknownStatuses }
}

/** A card's column: a changes verdict wins, then the status map, then
 *  PLANNED (the unknown-status home). */
export function columnOfTask(card: PlanKanbanCard): PlanKanbanColumn {
  if (verdictNeedsChanges(card.verdict)) return 'NEEDS YOU'
  return STATUS_TO_COLUMN[card.status] ?? 'PLANNED'
}

/** Side by side only when the pane is not NARROW and every column keeps ~14
 *  cols; otherwise the columns stack vertically (spec 6). Four columns, gap
 *  1 each: 4 × 14 + 3 = 59 is the first side-by-side width. */
export function planKanbanSideBySide(cols: number): boolean {
  return cols >= NARROW && Math.floor((cols - 3) / 4) >= 14
}

/** The mockup's per-column glyph and colour: ○ planned (dim), ● building
 *  (blue), ▲ needs you (amber), ✓ on main (green). */
const COLUMN_GLYPH: Readonly<Record<PlanKanbanColumn, string>> = {
  PLANNED: '○', BUILDING: '●', 'NEEDS YOU': '▲', 'ON MAIN': '✓',
}
const COLUMN_FG: Readonly<Record<PlanKanbanColumn, string>> = {
  PLANNED: PALETTE.dim, BUILDING: ROLE.live.fg, 'NEEDS YOU': ROLE.warn.fg, 'ON MAIN': ROLE.ok.fg,
}

const cut = (text: string, w: number): string => {
  if (w <= 0) return ''
  if (text.length <= w) return text
  if (w === 1) return '…'
  return `${text.slice(0, w - 1)}…`
}

/** todo 94: the lane tag when the full `L4 · opencode model` form does not
 *  fit — lane and agent only, no model and no ` · ` separator (`L4 opencode`).
 *  The wide form stays `laneAgent`'s; this is the whole-word fallback so a
 *  narrow card never cuts a word. */
const laneOnly = (card: PlanKanbanCard): string =>
  typeof card.slot === 'number' && Number.isFinite(card.slot) ? `L${card.slot}` : ''

const narrowLane = (card: PlanKanbanCard): string => {
  const lane = laneOnly(card)
  const agent = card.route?.agent ?? ''
  if (lane === '') return agent
  return agent === '' ? lane : `${lane} ${agent}`
}

/** One card (mockup): line 1 the column glyph in its colour + the task id;
 *  line 2 the title cut to the column width; line 3 the phase Chip, the dim
 *  lane·agent tag (V1.5) and the age when the data has one. The tag takes the
 *  narrow fallbacks below when the chip row cannot hold it whole. The card is
 *  pressable and selects the task (`plan:task`; pressing the selected task
 *  again clears it). BUILDING keeps the live border, NEEDS YOU the warn
 *  border, as the mockup's `.card.live` / amber border. */
function planCard(els: Els, card: PlanKanbanCard, col: PlanKanbanColumn, inner: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const edge = col === 'BUILDING' ? { role: 'live' as const } : col === 'NEEDS YOU' ? { role: 'warn' as const } : {}
  const full = laneAgent(card)
  const short = narrowLane(card)
  // todo 94: never cut the tag mid-word. Two fallbacks, in order: the chip row
  // gives the tag the inner width less the phase chip (label + 2), the gap
  // before it, and the age with its gap when there is one. The full
  // `L4 · opencode model` form wins when it fits there; else the model is
  // dropped (`L4 opencode`). When that still does not fit, the tag moves to
  // its own row (a plain Text, so the engine wraps it at word edges, never
  // mid-word). The three-part form is untouched wherever it does fit.
  // F.8: a risk badge (R1..R5, theme tokens only — ok ≤2 / idle 3 / warn ≥4,
  // never a made-up value when the row carries none) and the critical marker.
  // Badge cells come out of the chip row's room before the lane tag is sized.
  const riskTag = typeof card.risk === 'number' && card.risk >= 1 && card.risk <= 5 ? `R${card.risk}` : ''
  const riskRole: PaletteRole = riskTag === '' ? 'idle' : card.risk! <= 2 ? 'ok' : card.risk! >= 4 ? 'warn' : 'idle'
  const criticalTag = card.critical === true ? '!!' : ''
  const findTag = col === 'NEEDS YOU' && card.findings != null ? `${card.findings} ${card.findings === 1 ? 'finding' : 'findings'}` : ''
  const tagRoom = inner - (card.phase.length + 2) - 1 - (findTag !== '' ? findTag.length + 3 : 0) - (card.age !== '' ? 1 + card.age.length : 0)
    - (riskTag !== '' ? 1 + riskTag.length : 0) - (criticalTag !== '' ? 1 + criticalTag.length : 0)
  const fits = (tag: string): boolean => tag !== '' && tag.length <= tagRoom
  const rowTag = fits(full) ? full : fits(short) ? short : ''
  const ownTag = rowTag !== '' ? '' : short !== '' ? short : full
  return Card(els, edge, (
    <Box key={card.id} flexDirection="column">
      <Box flexDirection="row" gap={1}>
        <Text color={COLUMN_FG[col]}>{COLUMN_GLYPH[col]}</Text>
        {ActionButton(els, {
          key: `plankanban-card-${card.id}`,
          glyph: card.id,
          word: '',
          plain: true,
          onPress: () => { if (ui != null) ui.set('plan:task', ui.get('plan:task') === card.id ? '' : card.id) },
        })}
      </Box>
      <Text {...(col === 'ON MAIN' ? { dimColor: true } : { color: 'subtle' })} wrap="truncate-end">{cut(card.title, inner)}</Text>
      {col !== 'ON MAIN' && <Box flexDirection="row" gap={1}>
        {Chip(els, { label: card.phase, role: 'idle' })}
        {col === 'NEEDS YOU' && card.findings != null && Chip(els, { label: findTag, role: 'warn' })}
        {criticalTag !== '' && <Text color={ROLE.err.fg}>{criticalTag}</Text>}
        {riskTag !== '' && Chip(els, { label: riskTag, role: riskRole, solid: card.critical === true })}
        {rowTag !== '' && <Text dimColor={true}>{rowTag}</Text>}
        {card.age !== '' && <Text dimColor={true}>{card.age}</Text>}
      </Box>}
      {col !== 'ON MAIN' && ownTag !== '' && <Text dimColor={true}>{ownTag}</Text>}
    </Box>
  ))
}

/** One column: the Section heading with its count, then the cards — or the
 *  mockup's dim `none` when empty. `width` is the column's cell budget. */
function planColumn(els: Els, col: PlanKanbanColumn, cards: readonly PlanKanbanCard[], width: number, fixedWidth: boolean, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  // a lane: its own bordered box, header (glyph + name in the column colour, count right),
  // a rule, then the cards stacked tight; an empty lane says so dimly
  const inner = Math.max(1, width - 8) // lane border 2 + paddingX 2, card border 2 + paddingX 2
  const ruleW = Math.max(1, width - 4)
  return (
    <Box key={col} flexDirection="column" borderStyle="round" borderColor={cards.length > 0 && col !== 'ON MAIN' && col !== 'PLANNED' ? COLUMN_FG[col] : PALETTE.line} paddingX={1} {...(fixedWidth ? { width } : {})}>
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text bold={true} color={COLUMN_FG[col]}>{`${COLUMN_GLYPH[col]} ${col}`}</Text>
        <Text color={PALETTE.mut}>{String(cards.length)}</Text>
      </Box>
      <Text color={PALETTE.line}>{'─'.repeat(ruleW)}</Text>
      {cards.length === 0
        ? <Text dimColor={true}>— nothing here</Text>
        : cards.map(card => planCard(els, card, col, inner, ui))}
    </Box>
  )
}

/** The phase filter row (spec 3): `all phases` + one Segment per phase id in
 *  plan order, selection in `plankanban:phase` (absent or '' = all), and the
 *  mockup's dim `k = all runs` hint at the right. */
function phaseRow(els: Els, view: PlanKanbanView, phase: string, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="row" justifyContent="space-between" gap={1}>
      <Box flexDirection="row" gap={1}>
        {Segment(els, {
          key: 'plankanban-phase-all',
          label: 'all phases',
          on: phase === '',
          onPress: () => { ui?.set('plankanban:phase', '') },
        })}
        {view.phases.map(p => Segment(els, {
          key: `plankanban-phase-${p}`,
          label: p,
          on: phase === p,
          onPress: () => { ui?.set('plankanban:phase', p) },
        }))}
      </Box>
      <Text dimColor={true}>k = all runs</Text>
    </Box>
  )
}

/** Pure render: elements only, no $, no state (C1); `ui` is the C12 view state. */
export function PlanKanbanTab(els: Els, view: PlanKanbanView, cols: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const phase = ui?.get('plankanban:phase') ?? ''
  const shown = phase === '' ? view.cards : view.cards.filter(card => card.phase === phase)
  const side = planKanbanSideBySide(cols)
  const colW = side ? Math.floor((cols - 3) / 4) : cols - 1
  const byColumn = (col: PlanKanbanColumn): readonly PlanKanbanCard[] => shown.filter(card => columnOfTask(card) === col)
  return (
    <Box flexDirection="column" gap={1}>
      {phaseRow(els, view, phase, ui)}
      {side ? (
        <Box flexDirection="row" gap={1}>
          {PLAN_KANBAN_COLUMNS.map(col => planColumn(els, col, byColumn(col), colW, true, ui))}
        </Box>
      ) : (
        <Box flexDirection="column" gap={1}>
          {PLAN_KANBAN_COLUMNS.map(col => planColumn(els, col, byColumn(col), colW, false, ui))}
        </Box>
      )}
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text dimColor={true}>{`${shown.length} tasks · same cards as k, this plan only`}</Text>
        <Text dimColor={true}>source project</Text>
      </Box>
    </Box>
  )
}
