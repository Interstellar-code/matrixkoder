// The /mk pane's Plan tab (dashboard-v2-p0, T1.2): artboard A's "Checkout v2"
// Plan view rendered from its fixture, under the C1 contract names the pane
// already calls. Pure elements only — no $, no state; every string is cut to
// `cols` before it is emitted so nothing draws wider than the pane (C5).
import type { Elements } from 'claude-code'
import { PLAN_FIXTURE } from './plan.fixture'
import { NARROW, STATUS_COLOR, STATUS_GLYPH, fitLegend, leftCol, rightCol } from './ui'

type Els = Elements[keyof Elements]

/** One task row: `status` keys ui.ts's status vocabulary (marker, colours);
 *  `right` is the mockup's right column verbatim — status word + suffix, or
 *  agent · sha; `short` is the Narrow artboard's hand-shortened form. */
export type PlanRow = {
  readonly id: string
  readonly title: string
  readonly status: string
  readonly right: string
  readonly short?: { readonly title: string; readonly right: string }
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

// The acceptance line: Old's DONE set folds merged into done and its
// IN_PROGRESS set even draws merged as live — this tab keeps the two
// distinct (merged ◈ vs done ✓, both green); the remaining statuses keep
// Old's three checkboxes ([•] live, [!] side, [ ] not yet dispatched).
const LIVE = new Set(['dispatched', 'building', 'verifying', 'review', 'fix', 'qa'])
const SIDE = new Set(['blocked', 'quota', 'stalled', 'provider'])

/** `…`-truncate to `budget` cells — dashboard.tsx's truncateTitle voice (the
 *  row must show where the title yielded); local because importing the pane's
 *  copy would cycle dashboard.tsx → plan.tsx → dashboard.tsx. */
function clip(text: string, budget: number): string {
  if (text.length <= budget) return text
  return `${text.slice(0, Math.max(1, budget) - 1)}…`
}

/** One task row: marker + bold id + truncating title left, the mockup's right
 *  column right (Old's D1.2 budget voice: the title yields, never the status). */
function PlanTaskRow(els: Els, row: PlanRow, cols: number, compact: boolean) {
  const { Box, Text } = els
  const live = LIVE.has(row.status)
  const side = SIDE.has(row.status)
  const marker = row.status === 'merged' || row.status === 'done'
    ? `[${STATUS_GLYPH[row.status] ?? '✓'}]` // ◈ merged vs ✓ done — never aliased
    : live ? '[•]' : side ? '[!]' : '[ ]'
  const color = STATUS_COLOR[row.status] ?? 'gray'
  const title = compact ? row.short?.title ?? row.title : row.title
  const rightText = compact ? row.short?.right ?? row.right : row.right
  const fixed = marker.length + 1 + row.id.length + 1 // marker + gaps + id
  // C5 fit: the right column never crowds out the title (≥1 cell stays), then
  // the title yields the rest; both sides hard-clip so a row cannot exceed cols
  const rightW = Math.max(1, cols - fixed - 2)
  const right = rightText.length > rightW ? rightCol(rightText, rightW) : rightText
  const budget = Math.max(1, cols - fixed - 1 - right.length)
  // the mockup dims the settled columns (merged's agent · sha, planned's
  // note); live/side words take their STATUS_COLOR
  const settled = row.status === 'merged' || row.status === 'routed' || row.status === 'planned'
  return (
    <Box key={row.id} flexDirection="row" justifyContent="space-between" gap={1}>
      <Box flexDirection="row" gap={1} flexShrink={1}>
        <Text color={color} bold={live || side}>{marker}</Text>
        <Text bold={true}>{row.id}</Text>
        <Text dimColor={row.status === 'planned'} wrap="truncate-end">{clip(title, budget)}</Text>
      </Box>
      <Box flexDirection="row" gap={1} flexShrink={0}>
        <Text color={settled ? 'text' : color} dimColor={settled}>{right}</Text>
      </Box>
    </Box>
  )
}

/** Pure render: elements only, no $, no state (C1). */
export function PlanTab(els: Els, view: PlanView, cols: number) {
  const { Box, Text } = els
  const compact = cols < NARROW
  const footer = fitLegend([...view.footer], cols)
  return (
    <Box flexDirection="column" gap={1}>
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      {view.waves.map(wave => (
        <Box key={wave.name} flexDirection="column">
          <Box flexDirection="row" justifyContent="space-between" gap={1}>
            <Text bold={true}>{wave.name}</Text>
            <Text color={wave.tone} dimColor={wave.tone === 'gray'} wrap="truncate-end">
              {compact && wave.shortHeader != null ? wave.shortHeader : wave.header}
            </Text>
          </Box>
          {wave.rows.map(row => PlanTaskRow(els, row, cols, compact))}
        </Box>
      ))}
      {/* the Narrow artboard drops the footer; wide, fitLegend sheds tail items and clip marks the last cut */}
      {!compact && (
        <Text dimColor={true} wrap="truncate-end">{clip(footer, cols)}</Text>
      )}
    </Box>
  )
}
