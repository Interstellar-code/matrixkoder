// The /mk pane's Plan tab (plan dashboard-v2-p0b, V1.2, contract C9/C11): the
// Plan.dc.html tree redrawn from the V0 kit — one Card per wave with its header
// chip, the live wave's border in the live role, the live wave's segment strip,
// and phase rows that fold (▸) / expand (▾) on press to show their subtasks as
// TreeRows. Fold state lives in the C12 view-state handle (`plan:<phaseId>`);
// the tab itself stays pure — no $, no state, no scripts (C10).
import type { Elements } from 'claude-code'
import { PLAN_FIXTURE } from './plan.fixture'
import { NARROW, STATUS_GLYPH, fitLegend } from './ui'
import { ActionButton, Card, Chip, TreeRow } from './kit'
import { laneStrip, pickBar } from './raster'
import { ROLE } from './palette'
import type { PaletteRole } from './palette'

type Els = Elements[keyof Elements]

/** C12: this tab's view-state handle — declared locally (structural typing; no
 *  import crosses lanes). */
type TabUi = { get(key: string): string | undefined; set(key: string, value: string): void }

/** One subtask under a phase row (Plan.dc.html's tree): its own status and a
 *  right-side chip — a round marker (idle) or a findings count (warn). */
export type PlanSub = {
  readonly id: string
  readonly title: string
  readonly status: string
  readonly right: string
  readonly rightRole?: PaletteRole
}

/** One task row: `status` keys ui.ts's status vocabulary (marker, colours);
 *  `right` is the mockup's right column verbatim — status word + suffix, or
 *  agent · sha; `short` is the Narrow artboard's hand-shortened form. */
export type PlanRow = {
  readonly id: string
  readonly title: string
  readonly status: string
  readonly right: string
  readonly short?: { readonly title: string; readonly right: string }
  /** the phase's subtasks; absent (or empty) is a leaf with nothing to fold */
  readonly subs?: readonly PlanSub[]
}

/** One wave: `tone` is the mockup's header colour (green merged, blue live,
 *  gray planned — gray also dims); under NARROW a `shortHeader` replaces the
 *  long one (W3 collapses to its bare glyph). */
export type PlanWave = {
  readonly name: string
  readonly header: string
  readonly tone: 'green' | 'blue' | 'gray'
  readonly shortHeader?: string
  readonly rows: readonly PlanRow[]
}

/** The tab's whole view (C1): the fixture satisfies it today, the live run later. */
export type PlanView = {
  readonly mock: boolean
  readonly waves: readonly PlanWave[]
  /** ' · '-joined footer parts; the tail drops until the line fits (fitLegend). */
  readonly footer: readonly string[]
}

/** T1.2: the Checkout v2 fixture, mock flag on (C1/C6). */
export function planView(): PlanView {
  return { ...PLAN_FIXTURE, mock: true }
}

// the wave tones → palette roles: merged (ok), live (live), planned (idle)
const WAVE_ROLE: Record<PlanWave['tone'], PaletteRole> = { green: 'ok', blue: 'live', gray: 'idle' }
// a row's status → palette role, the same split the wave chip carries
const STATUS_ROLE: Record<string, PaletteRole> = {
  dispatched: 'live', building: 'live', verifying: 'live', review: 'live', fix: 'live', qa: 'live',
  merged: 'ok', done: 'ok',
  blocked: 'warn', quota: 'warn', stalled: 'warn', provider: 'warn',
  planned: 'idle', routed: 'idle',
}
const SETTLED = new Set(['merged', 'routed', 'planned'])

const roleOf = (status: string): PaletteRole => STATUS_ROLE[status] ?? 'idle'
const glyphOf = (status: string): string => STATUS_GLYPH[status] ?? '○'

/** `…`-truncate to `w` cells — dashboard.tsx's truncateTitle voice (the row
 *  must show where the title yielded); local because importing the pane's copy
 *  would cycle dashboard.tsx → plan.tsx → dashboard.tsx. */
function clip(text: string, w: number): string {
  if (text.length <= w) return text
  return `${text.slice(0, Math.max(1, w) - 1)}…`
}

/** Pure render: elements only, no $, no state (C1); `ui` is the C12 view state. */
export function PlanTab(els: Els, view: PlanView, cols: number, ui?: TabUi) {
  const { Box, Text } = els
  const compact = cols < NARROW
  const W = Math.max(1, cols - 4) // the Card's content width (border + padding per side)
  return (
    <Box flexDirection="column" gap={1}>
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      {view.waves.map(wave => WaveCard(els, wave, W, compact, ui))}
      {/* the Narrow artboard drops the footer; wide, fitLegend sheds tail items and clip marks the last cut */}
      {!compact && (
        <Text dimColor={true} wrap="truncate-end">{clip(fitLegend([...view.footer], cols), cols)}</Text>
      )}
    </Box>
  )
}

/** One wave Card: name + header chip, the live wave's strip and border, then
 *  the phase rows. */
function WaveCard(els: Els, wave: PlanWave, W: number, compact: boolean, ui?: TabUi) {
  const { Box, Text } = els
  const role = WAVE_ROLE[wave.tone]
  const live = wave.tone === 'blue'
  // the live wave's segment strip: one segment per row role (Plan.dc.html's
  // v0strip); the no-Raster fallback draws the row glyphs as one dim row
  const strip = live
    ? pickBar(
      els,
      `plan-strip-${wave.name}`,
      laneStrip(wave.rows.map(r => roleOf(r.status)), W),
      <Text dimColor={true}>{wave.rows.map(r => glyphOf(r.status)).join('')}</Text>,
    )
    : null
  return Card(els, live ? { role: 'live' } : {}, (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text bold={true} color="subtle">{wave.name}</Text>
        {Chip(els, { label: compact && wave.shortHeader != null ? wave.shortHeader : wave.header, role })}
      </Box>
      {strip}
      {wave.rows.map(row => PhaseRow(els, row, W, compact, ui))}
    </Box>
  ))
}

/** One phase row: the fold arrow, the status glyph, the pressable title and the
 *  right column; expanded, its subtasks hang under it as TreeRows. */
function PhaseRow(els: Els, row: PlanRow, W: number, compact: boolean, ui?: TabUi) {
  const { Box, Text } = els
  const expanded = ui?.get(`plan:${row.id}`) === '1'
  const role = roleOf(row.status)
  const settled = SETTLED.has(row.status)
  const title = compact ? row.short?.title ?? row.title : row.title
  const rightText = compact ? row.short?.right ?? row.right : row.right
  // the arrow (1) + the label's '◐ ' (2) + the right column + three gap columns
  const budget = Math.max(1, W - 1 - 2 - rightText.length - 3)
  const subs = row.subs ?? []
  return (
    <Box key={row.id} flexDirection="column">
      <Box flexDirection="row" gap={1}>
        <Text dimColor={true}>{expanded ? '▾' : '▸'}</Text>
        {ActionButton(els, {
          key: `plan-fold-${row.id}`,
          glyph: glyphOf(row.status),
          word: clip(title, budget),
          plain: true,
          tone: settled ? 'quiet' : 'default',
          onPress: () => { ui?.set(`plan:${row.id}`, expanded ? '' : '1') },
        })}
        <Box flexGrow={1} />
        <Text color={settled ? undefined : ROLE[role].fg} dimColor={settled} wrap="truncate-end">{rightText}</Text>
      </Box>
      {expanded && subs.map((sub, i) => TreeRow(els, {
        depth: 1,
        last: i === subs.length - 1,
        glyph: glyphOf(sub.status),
        role: roleOf(sub.status),
        title: sub.title,
        right: sub.right,
        rightRole: sub.rightRole ?? 'idle',
      }, W))}
    </Box>
  )
}
