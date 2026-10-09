// the /mk pane's Usage tab (plan dashboard-v2-p0, T2.2): the artboard-D quota
// view over the Checkout v2 fixture — quota rows per agent and pool with ui.ts
// bar() (5h + week, reserve-floor tick), the fitLegend text legend, then the
// Jev routing decisions (dim candidates line with the pick bold). C5: no
// borders, one Text per row, hard-truncated so nothing wraps at 36 or 46. Bars
// only at >= NARROW and never at width 0. Pure render (C1): elements only.
import type { Elements } from 'claude-code'
import { USAGE_FIXTURE } from './usage.fixture'
import { NARROW, bar, fitLegend, leftCol, rightCol } from './ui'

type Els = Elements[keyof Elements]

/** the row vocabulary: theme colour keys only (C5), 'dim' as dimColor. */
export type Tone = 'text' | 'dim' | 'success' | 'warning' | 'error' | 'suggestion'
export type Seg = { text: string; tone?: Tone; bold?: boolean }

/** one quota bar cell: used share, its label and the reserve floor (80 = 20 %). */
export type Quota = { pct: number; used: string; floor?: number; tone?: Tone }

/** one agent/pool row (artboard D) — note rows carry no bars. */
export type QuotaRow = {
  name: string
  nameTone?: Tone
  five?: Quota
  week?: Quota
  reset?: string
  note?: string
  noteTone?: Tone
  compactLine?: string
  compactTone?: Tone
}

/** one Jev routing decision (artboard D). */
export type Decision = {
  time: string
  task: string
  title: string
  sizeRisk: string
  candidates: string[]
  pick: string
  conf: string
  rule: string
}

export type UsageView = {
  mock: boolean
  headerLeft: string
  headerRight: string
  rows: QuotaRow[]
  legend: string[]
  decisionsHeader: string
  decisionsRight: string
  decisions: Decision[]
  actions: string
  footerLeft: string
  footerRight: string
}

/** the fixture with the mock flag (C1/C3). */
export function usageView(): UsageView {
  return { mock: true, ...USAGE_FIXTURE }
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

/** right-aligned tail on a segmented row: the tail survives, the head truncates. */
function withRight(left: Seg[], right: string, w: number): Seg[] {
  const tail = right.length > w ? rightCol(right, w) : right
  const room = Math.max(0, w - tail.length)
  const fitted = fitSegs(left, room)
  const used = fitted.reduce((n, seg) => n + seg.text.length, 0)
  return [...fitted, { text: ' '.repeat(Math.max(0, room - used)) }, { text: tail, tone: 'dim' as Tone }]
}

const sg = (text: string, tone?: Tone, bold?: boolean): Seg => ({ text, tone, bold })

const SWATCH: Record<string, Tone> = { '■ above 70%': 'warning', '■ exhausted': 'error' }

/** the fitLegend line rebuilt as segments so each `■` carries its colour. */
function legendSegs(line: string): Seg[] {
  const out: Seg[] = []
  line.split(' · ').forEach((item, i) => {
    if (i > 0) out.push(sg(' · ', 'dim'))
    const tone = SWATCH[item]
    if (tone && item.startsWith('■')) {
      out.push(sg('■', tone))
      out.push(sg(item.slice(1), 'dim'))
    } else {
      out.push(sg(item, 'dim'))
    }
  })
  return out
}

/** Pure render: elements only, no $, no state (C1). */
export function UsageTab(els: Els, view: UsageView, cols: number) {
  const { Box, Text } = els
  const compact = cols < NARROW
  const avail = cols
  // 2 indent + name + 5h bar + used + week bar + used + reset = cols
  const bw = Math.max(1, Math.floor((cols - 32) / 2))

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

  const usedTone = (q: Quota): Tone | undefined =>
    q.used === '⊘' ? 'error' : q.pct >= 70 ? 'warning' : undefined

  const quotaSegs = (row: QuotaRow): Seg[] => {
    if (row.note) return [sg('  ' + leftCol(row.name, 13), row.nameTone), sg(row.note, row.noteTone)]
    if (compact) return [sg('  ' + leftCol(row.name, 13), row.nameTone), sg(row.compactLine ?? '', row.compactTone)]
    const cell = (part: Quota | undefined): Seg[] =>
      part ? [sg(bar(part.pct, bw, part.floor), part.tone)] : [sg(leftCol('', bw))]
    return [
      sg('  ' + leftCol(row.name, 13), row.nameTone),
      ...cell(row.five),
      sg(' ' + leftCol(row.five?.used ?? '', 4), row.five ? usedTone(row.five) : undefined),
      ...cell(row.week),
      sg(' ' + leftCol(row.week?.used ?? '', 4), row.week ? usedTone(row.week) : undefined),
      sg(' ' + rightCol(row.reset ?? '', 6), 'dim'),
    ]
  }

  const candSegs = (d: Decision): Seg[] => {
    const out: Seg[] = []
    d.candidates.forEach((c, i) => {
      if (i > 0) out.push(sg(' · ', 'dim'))
      out.push(c === d.pick ? sg(c, undefined, true) : sg(c, 'dim'))
    })
    return out
  }

  return (
    <Box flexDirection="column">
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      {compact || cols < 50 ? (
        <Text>{segEls([sg(view.headerLeft, undefined, true)], avail)}</Text>
      ) : (
        <Text>{segEls(withRight([sg(view.headerLeft, undefined, true)], view.headerRight, avail), avail)}</Text>
      )}
      {!compact ? (
        <Text>
          {segEls(
            [
              sg('  ' + leftCol('agent / pool', 13), 'dim'),
              sg(leftCol('5h', bw), 'dim'),
              sg(' ' + leftCol('used', 4), 'dim'),
              sg(leftCol('week', bw), 'dim'),
              sg(' ' + leftCol('used', 4), 'dim'),
              sg(' ' + rightCol('reset', 6), 'dim'),
            ],
            avail,
          )}
        </Text>
      ) : null}
      {view.rows.map((row, i) => (
        <Text key={String(i)}>{segEls(quotaSegs(row), avail)}</Text>
      ))}
      <Text>{segEls(legendSegs(fitLegend(view.legend, avail)), avail)}</Text>
      <Text>{' '}</Text>
      {compact || cols < 50 ? (
        <Text>{segEls([sg(view.decisionsHeader, undefined, true)], avail)}</Text>
      ) : (
        <Text>{segEls(withRight([sg(view.decisionsHeader, undefined, true)], view.decisionsRight, avail), avail)}</Text>
      )}
      {view.decisions.map((d, i) => (
        <Box key={String(i)} flexDirection="column">
          <Text>
            {segEls([sg(d.time, 'dim'), sg(' ' + d.task, undefined, true), sg(' ' + d.title + ' · '), sg(d.sizeRisk, 'dim')], avail)}
          </Text>
          <Text>{segEls(candSegs(d), avail)}</Text>
          <Text>
            {segEls(
              [
                sg('pick ' + d.pick, 'suggestion', true),
                sg(' · conf ', 'dim'),
                sg(d.conf, parseFloat(d.conf) >= 0.7 ? 'success' : 'warning'),
                sg(' · ' + d.rule, 'dim'),
              ],
              avail,
            )}
          </Text>
        </Box>
      ))}
      <Text dimColor={true}>{leftCol(view.actions, avail)}</Text>
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
