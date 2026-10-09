// the /mk pane's Lanes tab (plan dashboard-v2-p0, T2.1): the artboard-C Lanes
// view over the Checkout v2 fixture — summary, four lanes (glyph+status header
// with round/age on the right, two-space indented detail rows), the gate table
// (three columns at >= 50 cols, one line under) and dim action text. C5: no
// borders, one Text per row, hard-truncated with leftCol/rightCol so nothing
// wraps at 36 or 46. Pure render (C1): elements only, no $, no state.
import type { Elements } from 'claude-code'
import { LANES_FIXTURE } from './lanes.fixture'
import { NARROW, leftCol, rightCol } from './ui'

type Els = Elements[keyof Elements]

/** header glyphs as artboard C draws them (its own vocabulary; ui.ts keeps the pane's). */
const LANE_GLYPH: Record<string, string> = { building: '●', review: '◐', blocked: '!', quota: '⊘' }

/** the row vocabulary: theme colour keys only (C5), 'dim' as dimColor. */
export type Tone = 'text' | 'dim' | 'success' | 'warning' | 'error' | 'suggestion'
export type Seg = { text: string; tone?: Tone; bold?: boolean }

/** one gate-table row: gate name · lane value · baseline (artboard C). */
export type GateRow = { name: string; lane: Seg[]; baseline: string }

/** one lane card, flattened to header + indented rows per C5. */
export type LaneData = {
  status: string
  statusTone: Tone
  task: string
  title: string
  right: string
  rows: Seg[][]
  gate: GateRow[]
  gateOneLine: Seg[]
  actions: string
  compact: {
    header: string
    statusTone: Tone
    right: string
    rows: { text: string; tone?: Tone }[]
  }
}

export type LanesView = {
  mock: boolean
  summary: string
  summaryRight: string
  lanes: LaneData[]
  footerLeft: string
  footerRight: string
}

/** the fixture with the mock flag (C1/C3). */
export function lanesView(): LanesView {
  return { mock: true, ...LANES_FIXTURE }
}

/** hard-truncate segments to `w` cells (leftCol semantics) so a row never wraps. */
function fitSegs(segs: Seg[], w: number): Seg[] {
  const out: Seg[] = []
  let used = 0
  for (const seg of segs) {
    const room = w - used
    if (room <= 0) break
    const text = seg.text.length > room ? leftCol(seg.text, room) : seg.text
    out.push({ ...seg, text })
    used += text.length
  }
  return out
}

/** fit segments and pad them to exactly `w` cells (column alignment). */
function padSegs(segs: Seg[], w: number): Seg[] {
  const fitted = fitSegs(segs, w)
  const used = fitted.reduce((n, seg) => n + seg.text.length, 0)
  return [...fitted, { text: ' '.repeat(Math.max(0, w - used)) }]
}

/** right-aligned tail on a segmented row: the tail survives, the head truncates. */
function withRight(left: Seg[], right: string, w: number): Seg[] {
  const tail = right.length > w ? rightCol(right, w) : right
  const room = Math.max(0, w - tail.length)
  return [...padSegs(left, room), { text: tail, tone: 'dim' as Tone }]
}

const sg = (text: string, tone?: Tone, bold?: boolean): Seg => ({ text, tone, bold })

/** Pure render: elements only, no $, no state (C1). */
export function LanesTab(els: Els, view: LanesView, cols: number) {
  const { Box, Text } = els
  const compact = cols < NARROW
  const avail = cols
  // gate columns: 2 indent + name + lane + baseline = avail
  const GW = 12
  const BW = 8
  const LW = Math.max(4, avail - 2 - GW - BW)

  const segEls = (segs: Seg[], w: number) =>
    fitSegs(segs, w).map((seg, j) =>
      seg.tone === 'dim' ? (
        <Text key={String(j)} bold={seg.bold} dimColor={true}>{seg.text}</Text>
      ) : seg.tone && seg.tone !== 'text' ? (
        <Text key={String(j)} bold={seg.bold} color={seg.tone}>{seg.text}</Text>
      ) : (
        <Text key={String(j)} bold={seg.bold}>{seg.text}</Text>
      ),
    )

  return (
    <Box flexDirection="column">
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      {compact || cols < 50 ? (
        <Text dimColor={true}>{leftCol(view.summary, avail)}</Text>
      ) : (
        <Text dimColor={true}>
          {leftCol(view.summary, Math.max(0, avail - view.summaryRight.length)) + view.summaryRight}
        </Text>
      )}
      {view.lanes.map((lane, li) => (
        <Box key={String(li)} flexDirection="column">
          {compact ? (
            <Text>
              {segEls(withRight([sg(lane.compact.header, lane.compact.statusTone, true)], lane.compact.right, avail), avail)}
            </Text>
          ) : (
            <Text>
              {segEls(
                withRight(
                  [
                    sg(`${LANE_GLYPH[lane.status] ?? ''} ${lane.status}`, lane.statusTone, true),
                    sg(` · ${lane.task} ${lane.title}`, undefined, true),
                  ],
                  lane.right,
                  avail,
                ),
                avail,
              )}
            </Text>
          )}
          {compact
            ? lane.compact.rows.map((row, ri) =>
                row.tone === 'dim' ? (
                  <Text key={String(ri)} dimColor={true}>{leftCol(row.text, avail)}</Text>
                ) : row.tone && row.tone !== 'text' ? (
                  <Text key={String(ri)} color={row.tone}>{leftCol(row.text, avail)}</Text>
                ) : (
                  <Text key={String(ri)}>{leftCol(row.text, avail)}</Text>
                ),
              )
            : lane.rows.map((row, ri) => <Text key={String(ri)}>{segEls(row, avail)}</Text>)}
          {!compact && cols >= 50 && lane.gate.length > 0 ? (
            <Box flexDirection="column">
              <Text>
                {segEls(
                  [sg('  ' + leftCol('gate', GW), 'dim'), ...padSegs([sg('lane', 'dim')], LW), sg(rightCol('baseline', BW), 'dim')],
                  avail,
                )}
              </Text>
              {lane.gate.map((g, gi) => (
                <Text key={String(gi)}>
                  {segEls(
                    [sg('  ' + leftCol(g.name, GW), 'dim'), ...padSegs(g.lane, LW), sg(rightCol(g.baseline, BW), 'dim')],
                    avail,
                  )}
                </Text>
              ))}
            </Box>
          ) : !compact && cols < 50 && lane.gateOneLine.length > 0 ? (
            <Text>{segEls(lane.gateOneLine, avail)}</Text>
          ) : null}
          <Text dimColor={true}>{leftCol(lane.actions, avail)}</Text>
          {li < view.lanes.length - 1 ? <Text>{' '}</Text> : null}
        </Box>
      ))}
      {compact || cols < 50 ? (
        <Text dimColor={true}>{leftCol(view.footerLeft, avail)}</Text>
      ) : (
        <Text dimColor={true}>
          {leftCol(view.footerLeft, Math.max(0, avail - view.footerRight.length)) + view.footerRight}
        </Text>
      )}
    </Box>
  )
}
