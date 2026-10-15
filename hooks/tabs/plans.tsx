// The /mk pane's Plan tab List sub-tab (plan settings-plans-db, V1.1, contracts
// C8/C9/C10). Built only from the V0 kit — Section, Card, Chip, Segment,
// ActionButton, ActionButtons — plus laneStrip from raster.ts. The pane never
// writes state: every action calls the injected onAction; V1.3 binds it to the
// prompt-submit path. No $ import (C10).
import type { Elements, RenderChildren, RenderElement } from 'claude-code'
import { NARROW, STATUS_GLYPH } from './ui'
import { ActionButton, ActionButtons, Card, Chip, Section, Segment } from './kit'
import type { ActionSpec, ActionTone } from './kit'
import { laneStrip, pickBar } from './raster'
import { ROLE } from './palette'
import type { PaletteRole } from './palette'

type Els = Elements[keyof Elements]

/** C12: this tab's view-state handle — declared locally (structural typing; no
 *  import crosses lanes). */
export type TabUi = { get(key: string): string | undefined; set(key: string, value: string): void }

/** One ongoing run card (PlanList.dc.html): id, the plan it belongs to, the
 *  title line, the stage chip (`7/12 Verify`) and the strip's counts. */
export type RunRow = {
  readonly id: string
  readonly plan: string
  readonly title: string
  readonly stage: string
  /** settled / live / planned segment counts for the laneStrip */
  readonly counts: { readonly done: number; readonly live: number; readonly planned: number }
  readonly countsLine: string
}

/** C8: one plan row. `counts` is the header's task/phase tally the Detail
 *  header card reads; `live_run` is the run a `running` plan is on. `project` is
 *  the root `plan_summary.project` (the slug) — the List renders from the root
 *  overview and needs it to name the C2 archive path (L7.2). */
export type PlanRow = {
  readonly id: string
  readonly title: string
  readonly status: string
  readonly created: string
  readonly runs: readonly string[]
  readonly live_run: string | null
  readonly counts: {
    readonly tasks: number
    readonly phases: number
    /** per-status tally from the overview's `counts` column (e.g. { merged: 53,
     *  building: 2, planned: 30 }); a bare row may carry none */
    readonly byStatus?: Readonly<Record<string, number>>
  }
  /** the owning project slug, when the source named one (the root overview's `plan_summary.project`) */
  readonly project?: string
  /** Detail header extras the loader will fill; optional so a bare C8 row renders */
  readonly mergesTo?: string
  readonly design?: readonly string[]
}

/** C8: the view the loader fills. `onAction` is injected with the view so this
 *  file never imports `$`; absent, the buttons still render and do nothing. */
export type PlansView = {
  readonly mock: boolean
  readonly runs: readonly RunRow[]
  readonly plans: readonly PlanRow[]
  readonly onAction?: (kind: string, planId: string) => void
}

/** The structured line V1.3 submits (waiting.tsx's `mk:` voice). */
export function planActionLine(kind: string, id: string): string {
  return `mk: ${kind} plan ${id}`
}

const FILTERS = ['all', 'draft', 'running', 'parked', 'done'] as const

const STATUS_ROLE: Record<string, PaletteRole> = {
  draft: 'idle', approved: 'idle', running: 'live', parked: 'warn', done: 'ok',
}
const STATUS_GLYPH_PLAN: Record<string, string> = {
  draft: '◇', approved: '◇', running: '◐', parked: '‖', done: '✓',
}

type PlanAction = { kind: string; word: string; tone: ActionTone; primary?: boolean }

/** C10: the buttons a status shows. `approved` shares draft's pair. */
function actionsFor(status: string): readonly PlanAction[] {
  switch (status) {
    case 'draft':
    case 'approved':
      return [
        { kind: 'start', word: 'Start', tone: 'primary', primary: true },
        { kind: 'delete', word: 'Delete', tone: 'danger' },
      ]
    case 'running':
      return [
        { kind: 'open', word: 'Open', tone: 'default', primary: true },
        { kind: 'pause', word: 'Pause', tone: 'quiet' },
      ]
    case 'parked':
      return [
        { kind: 'resume', word: 'Resume', tone: 'primary', primary: true },
        { kind: 'delete', word: 'Delete', tone: 'danger' },
      ]
    case 'done':
      return [{ kind: 'delete', word: 'Delete', tone: 'danger', primary: true }]
    default:
      return []
  }
}

function clip(text: string, w: number): string {
  if (text.length <= w) return text
  return `${text.slice(0, Math.max(1, w) - 1)}…`
}

/** C2: the folder a deleted plan's file moves to — `~/.matrixkoder/projects/<slug>/plans/<plan>/archive/`.
 *  A slug is needed; the row's `project` carries it, and `matrixkoder` is the C16 default when
 *  the source named none (the live project). Never the old `<repo>/.matrixkoder/plans/archive`. */
export function planArchiveDir(plan: Pick<PlanRow, 'id' | 'project'>): string {
  const slug = plan.project != null && plan.project !== '' ? plan.project : 'matrixkoder'
  return `~/.matrixkoder/projects/${slug}/plans/${plan.id}/archive/`
}

/** Pure render: elements only, no $, no state (C1); `ui` is the C12 view state. */
export function PlansTab(els: Els, view: PlansView, cols: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const compact = cols < NARROW
  const W = Math.max(1, cols - 4)
  const filter = ui?.get('plans:filter') ?? 'all'
  const shown = filter === 'all' ? view.plans : view.plans.filter(p => p.status === filter)
  return (
    <Box flexDirection="column" gap={1}>
      {view.mock && <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>}
      {Section(els, { label: 'ONGOING RUNS', right: String(view.runs.length) })}
      {view.runs.map(run => RunCard(els, run, W))}
      {Section(els, { label: 'PLANS' })}
      <Box flexDirection="row" gap={1}>
        {FILTERS.map(f => Segment(els, {
          key: `plans-filter-${f}`,
          label: f === 'all' ? `all ${view.plans.length}` : f,
          on: filter === f,
          onPress: () => { ui?.set('plans:filter', f) },
        }))}
      </Box>
      {Card(els, {}, (
        <Box flexDirection="column">
          {shown.map(plan => PlanLine(els, plan, view, W, compact, ui))}
        </Box>
      ))}
      <Text dimColor={true}>Press a row for Detail · only imported plans are listed</Text>
    </Box>
  )
}

/** One run Card: live border, stage chip, title, the laneStrip and the counts. */
function RunCard(els: Els, run: RunRow, W: number): RenderElement {
  const { Box, Text } = els
  const { done, live, planned } = run.counts
  const states: PaletteRole[] = [
    ...Array<PaletteRole>(done).fill('ok'),
    ...Array<PaletteRole>(live).fill('live'),
    ...Array<PaletteRole>(planned).fill('idle'),
  ]
  const strip = pickBar(
    els,
    `plans-strip-${run.id}`,
    laneStrip(states, W),
    <Text dimColor={true}>{`${'◈'.repeat(done)}${'●'.repeat(live)}${'○'.repeat(planned)}`}</Text>,
  )
  return Card(els, { role: 'live' }, (
    <Box key={run.id} flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text color={ROLE.live.fg}>{`● ${clip(run.id, Math.max(1, W - run.stage.length - 6))}`}</Text>
        {Chip(els, { label: run.stage, role: 'live' })}
      </Box>
      <Text color={ROLE.idle.fg} wrap="truncate-end">{clip(run.title, W)}</Text>
      {strip}
      <Text dimColor={true} wrap="truncate-end">{clip(run.countsLine, W)}</Text>
    </Box>
  ))
}

/** One plan row: status glyph, id (press opens Detail), status chip, actions.
 *  Delete's first press sets `plans:confirm` and draws the confirm row. */
function PlanLine(els: Els, plan: PlanRow, view: PlansView, W: number, compact: boolean, ui?: TabUi): RenderChildren {
  const { Box, Text } = els
  const role = STATUS_ROLE[plan.status] ?? 'idle'
  const glyph = STATUS_GLYPH_PLAN[plan.status] ?? STATUS_GLYPH[plan.status] ?? '○'
  const confirming = ui?.get('plans:confirm') === plan.id
  const liveTag = plan.live_run != null && plan.live_run.startsWith(`${plan.id}-`)
    ? plan.live_run.slice(plan.id.length + 1)
    : plan.live_run
  const chip = plan.status === 'running' && (plan.live_run == null || plan.live_run === '')
    ? 'running (no live run)'
    : plan.status === 'running' && liveTag != null
      ? `running · ${liveTag}`
      : plan.status
  const fire = (kind: string): void => { view.onAction?.(kind, plan.id) }
  const specs: ActionSpec[] = actionsFor(plan.status).map(a => ({
    key: `plans-${a.kind}-${plan.id}`,
    // a danger button draws its glyph as separate text and its word as the Button label
    // (kit.tsx ActionButton split), so Delete carries the word, not the glyph
    glyph: a.tone === 'danger' ? '✕' : a.word,
    word: a.tone === 'danger' ? a.word : '',
    tone: a.tone,
    primary: a.primary,
    plain: true,
    onPress: () => {
      if (a.kind === 'delete') { ui?.set('plans:confirm', plan.id); return }
      fire(a.kind)
    },
  }))
  const idBudget = Math.max(1, compact ? 22 : 32)
  return (
    <Box key={`plans-row-${plan.id}`} flexDirection="column">
      <Box flexDirection="row" gap={1}>
        <Text color={ROLE[role].fg}>{glyph}</Text>
        <Text>{clip(plan.id, idBudget)}</Text>
        {ActionButton(els, {
          key: `plans-detail-${plan.id}`,
          glyph: '›',
          word: '',
          plain: true,
          noHotkey: true,
          onPress: () => { ui?.set('plans:sel', plan.id); ui?.set('plan:sub', 'detail') },
        })}
        <Box flexGrow={1} />
        {Chip(els, { label: clip(chip, compact ? 18 : 28), role })}
        {ActionButtons(els, specs, W, () => { ui?.set(`plans:more:${plan.id}`, '1') })}
      </Box>
      {plan.counts.byStatus && Object.keys(plan.counts.byStatus).length > 0 && (
        <Text dimColor={true} wrap="truncate-end">
          {clip(Object.entries(plan.counts.byStatus).map(([s, n]) => `${n} ${s}`).join(' · '), Math.max(1, W - 4))}
        </Text>
      )}
      {confirming && (
        <Box flexDirection="row" gap={1}>
          <Text color={ROLE.err.fg}>{clip(`Delete ${plan.id}? File moves to ${planArchiveDir(plan)}.`, Math.max(1, W - 20))}</Text>
          {ActionButton(els, {
            key: `plans-confirm-delete-${plan.id}`, glyph: '✕', word: 'Delete', tone: 'danger', plain: true,
            onPress: () => { ui?.set('plans:confirm', ''); fire('delete') },
          })}
          {ActionButton(els, {
            key: `plans-confirm-cancel-${plan.id}`, glyph: 'Cancel', word: '', tone: 'quiet', plain: true,
            onPress: () => { ui?.set('plans:confirm', '') },
          })}
        </Box>
      )}
    </Box>
  )
}
