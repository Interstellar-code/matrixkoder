// The /mk pane's Plan tab List sub-tab (plan settings-plans-db, V1.1, contracts
// C8/C9/C10). Built only from the V0 kit — Section, Card, Chip, Segment,
// ActionButton, ActionButtons — plus laneStrip from raster.ts. The pane never
// writes state: every action calls the injected onAction; V1.3 binds it to the
// prompt-submit path. No $ import (C10).
import type { Elements, RenderChildren, RenderElement } from 'claude-code'
import { NARROW, STATUS_GLYPH } from './ui'
import { ActionButton, BoxButton, Card, Chip, Segment } from './kit'
import type { ActionSpec, ActionTone } from './kit'
import { laneStrip, pickBar } from './raster'
import { PALETTE, ROLE } from './palette'
import { paths } from '../paths'
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
  /** footer right side: when the list was last read, `HH:MM`; absent, no time shown */
  readonly cachedAt?: string
}

/** The structured line V1.3 submits (waiting.tsx's `mk:` voice). */
export function planActionLine(kind: string, id: string): string {
  return `mk: ${kind} plan ${id}`
}

const FILTERS = ['all', 'draft', 'running', 'parked', 'done', 'archived'] as const

const STATUS_ROLE: Record<string, PaletteRole> = {
  draft: 'idle', approved: 'idle', running: 'live', parked: 'warn', done: 'ok', archived: 'idle',
}
const STATUS_GLYPH_PLAN: Record<string, string> = {
  draft: '◇', approved: '◇', running: '◐', parked: '‖', done: '✓', archived: '▣',
}

type PlanAction = { kind: string; word: string; tone: ActionTone; primary?: boolean }

/** C10: the buttons a status shows. `approved` shares draft's. Delete only on `archived` (mk-plan.py refuses it elsewhere). */
function actionsFor(status: string): readonly PlanAction[] {
  switch (status) {
    case 'draft':
    case 'approved':
      return [
        { kind: 'start', word: 'Start', tone: 'primary', primary: true },
      ]
    case 'running':
      return [
        { kind: 'open', word: 'Open', tone: 'default', primary: true },
        { kind: 'pause', word: 'Pause', tone: 'quiet' },
      ]
    case 'parked':
      return [
        { kind: 'resume', word: 'Resume', tone: 'primary', primary: true },
      ]
    case 'done':
      return [{ kind: 'archive', word: 'Archive', tone: 'primary', primary: true }]
    case 'archived':
      return [
        { kind: 'unarchive', word: 'Unarchive', tone: 'default' },
        { kind: 'delete', word: 'Delete', tone: 'danger', primary: true },
      ]
    default:
      return []
  }
}

/** The design's filled button, now the kit's one button look (BoxButton). */
function Pill(els: Els, key: string, label: string, tone: ActionTone, onPress: () => void): RenderElement {
  return BoxButton(els, { key, label, tone, onPress })
}

function clip(text: string, w: number): string {
  if (text.length <= w) return text
  return `${text.slice(0, Math.max(1, w) - 1)}…`
}

/** Where Delete's pre-delete backup and the kept plan folder live, per mk-plan.py:
 *  `<project>/archive/<id>-pre-delete-<ts>.db` and `<project>/archive/plans/<id>/`, with
 *  `<project>` = `~/.matrixkoder/projects/<slug>` (`matrixkoder` is the C16 default slug). */
export function planDeleteInfo(plan: Pick<PlanRow, 'id' | 'project'>): { backup: string; folder: string } {
  const slug = plan.project != null && plan.project !== '' ? plan.project : 'matrixkoder'
  const archive = `~/.matrixkoder/projects/${slug}/archive`
  return { backup: `${archive}/${plan.id}-pre-delete-<ts>.db`, folder: `${archive}/plans/${plan.id}/` }
}

/** Pure render: elements only, no $, no state (C1); `ui` is the C12 view state. */
export function PlansTab(els: Els, view: PlansView, cols: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const compact = cols < NARROW
  const W = Math.max(1, cols - 4)
  const filter = ui?.get('plans:filter') ?? 'all'
  // archived plans leave the all filter and every count; only the archived filter lists them
  const live = view.plans.filter(p => p.status !== 'archived')
  const shown = filter === 'all' ? live : view.plans.filter(p => p.status === filter)
  const filters = (
    <Box flexDirection="row" gap={1}>
      {FILTERS.map(f => Segment(els, {
        key: `plans-filter-${f}`,
        label: f === 'all' ? `all ${live.length}` : f,
        on: filter === f,
        onPress: () => { ui?.set('plans:filter', f) },
      }))}
    </Box>
  )
  return (
    // paddingRight: the right-aligned counts and the last filter sat on the pane edge, cut off
    <Box flexDirection="column" gap={1} paddingRight={1}>
      {view.mock && <Text bold={true} color={ROLE.warn.fg}>MOCK · fixture data · not this run</Text>}
      {Head(els, 'ONGOING RUNS', String(view.runs.length))}
      {view.runs.length === 0 && <Text dimColor={true}>no live runs · start a plan below</Text>}
      {view.runs.map(run => RunCard(els, run, view, W, ui))}
      {compact
        ? <Box flexDirection="column" gap={1}>{Head(els, 'PLANS')}{filters}</Box>
        : (
          <Box flexDirection="row" justifyContent="space-between" gap={1}>
            {Head(els, 'PLANS')}
            {filters}
          </Box>
        )}
      <Box flexDirection="column">
        {shown.length === 0 && <Text dimColor={true}>{`no ${filter} plans`}</Text>}
        {shown.map(plan => PlanLine(els, plan, view, W, compact, ui))}
      </Box>
      <Text dimColor={true}>Press a row for Detail · only imported plans are listed</Text>
      <Text color={PALETTE.line}>{'─'.repeat(Math.max(1, W + 2))}</Text>
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text dimColor={true}>{`source overview · ${live.length} plan${live.length === 1 ? '' : 's'}`}</Text>
        {view.cachedAt != null && view.cachedAt !== '' && <Text dimColor={true}>{`list cached ${view.cachedAt}`}</Text>}
      </Box>
    </Box>
  )
}

/** A section heading in the design's bright muted bold (kit Section draws it dim). */
function Head(els: Els, label: string, right?: string): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="row" justifyContent="space-between" gap={1}>
      <Text bold={true} color={PALETTE.mut}>{label}</Text>
      {right != null && <Text color={PALETTE.mut}>{right}</Text>}
    </Box>
  )
}

/** One run Card: live border, stage chip, title, the laneStrip, the counts and
 *  Open/Pause. Pressing the run id opens the plan's Detail, like Open. */
function RunCard(els: Els, run: RunRow, view: PlansView, W: number, ui?: TabUi): RenderElement {
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
  const open = (): void => { ui?.set('plans:sel', run.plan); ui?.set('plan:sub', 'detail') }
  const buttons: ActionSpec[] = [
    { key: `plans-run-open-${run.id}`, glyph: 'Open', word: '', tone: 'default', primary: true, plain: true, onPress: open },
    { key: `plans-run-pause-${run.id}`, glyph: 'Pause', word: '', tone: 'quiet', plain: true, onPress: () => { view.onAction?.('pause', run.plan) } },
  ]
  return Card(els, { role: 'live' }, (
    <Box key={run.id} flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Box flexDirection="row" gap={1}>
          <Text color={ROLE.live.fg}>●</Text>
          {ActionButton(els, {
            key: `plans-run-${run.id}`,
            glyph: clip(run.id, Math.max(1, W - run.stage.length - 8)),
            word: '',
            plain: true,
            noHotkey: true,
            onPress: open,
          })}
        </Box>
        {run.stage !== '' && Chip(els, { label: run.stage, role: 'live' })}
      </Box>
      <Text color={ROLE.idle.fg} wrap="truncate-end">{clip(run.title, W)}</Text>
      {strip}
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text dimColor={true} wrap="truncate-end">{clip(run.countsLine, Math.max(1, W - 14))}</Text>
        <Box flexDirection="row" gap={1}>{buttons.map(b => Pill(els, b.key, b.glyph, b.tone ?? 'default', () => { b.onPress?.() }))}</Box>
      </Box>
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
  const del = planDeleteInfo(plan)
  const liveTag = plan.live_run != null && plan.live_run.startsWith(`${plan.id}-`)
    ? plan.live_run.slice(plan.id.length + 1)
    : plan.live_run
  const chip = plan.status === 'running' && (plan.live_run == null || plan.live_run === '')
    ? 'running (no live run)'
    : plan.status === 'running' && liveTag != null
      ? `running · ${liveTag}`
      : plan.status
  const fire = (kind: string): void => { view.onAction?.(kind, plan.id) }

  const idBudget = Math.max(1, compact ? 22 : 32)
  const by = plan.counts.byStatus ?? {}
  const countsLine = Object.entries(by).map(([k, n]) => `${n} ${k}`).join(' · ')
  // the strip: settled green, live blue, the rest the track, one segment per task bucket
  const settled = Object.entries(by).filter(([k]) => /done|merged|qa/.test(k)).reduce((n, [, v]) => n + v, 0)
  const total = Object.values(by).reduce((n, v) => n + v, 0)
  const liveN = Object.entries(by).filter(([k]) => /build|review|verify|fix|run/.test(k)).reduce((n, [, v]) => n + v, 0)
  const barW = Math.max(4, W - 6)
  const okW = total > 0 ? Math.round(settled / total * barW) : 0
  const liveW = total > 0 ? Math.round(liveN / total * barW) : 0
  const edge = plan.status === 'running' ? { role: 'live' as const } : plan.status === 'parked' ? { role: 'warn' as const } : {}
  return Card(els, edge, (
    <Box key={`plans-row-${plan.id}`} flexDirection="column">
      <Box flexDirection="row" gap={1}>
        <Text color={ROLE[role].fg}>{glyph}</Text>
        {ActionButton(els, {
          key: `plans-detail-${plan.id}`,
          // the name itself is the press target, not only the one-cell ›
          glyph: clip(plan.id, idBudget),
          word: '',
          plain: true,
          noHotkey: true,
          onPress: () => { ui?.set('plans:sel', plan.id); ui?.set('plan:sub', 'detail') },
        })}
        <Box flexGrow={1} />
        {Chip(els, { label: clip(chip, compact ? 18 : 28), role })}
      </Box>
      {plan.title !== '' && plan.title !== plan.id && <Text color={ROLE.idle.fg} wrap="truncate-end">{clip(plan.title, W - 4)}</Text>}
      {total > 0 && (
        <Text>
          <Text color={ROLE.ok.fg}>{'━'.repeat(okW)}</Text>
          <Text color={ROLE.live.fg}>{'━'.repeat(Math.min(liveW, barW - okW))}</Text>
          <Text color={PALETTE.line}>{'━'.repeat(Math.max(0, barW - okW - liveW))}</Text>
        </Text>
      )}
      <Box flexDirection="row" gap={1}>
        <Text dimColor={true} wrap="truncate-end">{clip(countsLine, Math.max(1, W - 24))}</Text>
        <Box flexGrow={1} />
        <Box flexDirection="row" gap={1}>{actionsFor(plan.status).map(x => Pill(els, `plans-${x.kind}-${plan.id}`, x.word, x.tone, () => { if (x.kind === 'delete') { ui?.set('plans:confirm', plan.id); return } fire(x.kind) }))}</Box>
      </Box>
      {confirming && (
        <Box flexDirection="row" gap={1}>
          <Text color={ROLE.err.fg}>{clip(`Delete ${plan.id}? Backup ${del.backup}; folder stays in ${del.folder}`, Math.max(1, W - 20))}</Text>
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
  ))
}
