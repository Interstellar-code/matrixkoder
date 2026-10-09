// The /mk pane's Usage tab (plan dashboard-v2-p0b, V2.2; contract C9/C11/C12):
// two kit Cards — Quota, with a name row per agent and a 5h and a week bar drawn
// by `gradientBar` (a `SmoothBar` where the surface has no Raster) carrying the
// reserve-floor mark, and Jev routing in the mockup's one-line form
// `task → pick conf · rule`. C7: every colour comes from the palette — the
// exhausted agent's name in err, the legend chips in their real roles.
//
// The fourth argument is the local view state (C12). This tab draws no control
// — the Usage board has none — so `ui` is accepted and unused: the fixture's
// default state is what draws, and nothing here can write anything (C10).
import type { Elements, RenderElement } from 'claude-code'
import { USAGE_FIXTURE } from './usage.fixture'
import { leftCol } from './ui'
import { ActionRow, Card, Chip, Section, SmoothBar } from './kit'
import { ROLE } from './palette'
import type { PaletteRole } from './palette'
import { gradientBar, pickBar } from './raster'

type Els = Elements[keyof Elements]

/** The local view state V1 hands every mock tab (C12), declared structurally. */
export type TabUi = { get(key: string): string | undefined; set(key: string, value: string): void }

/** the row vocabulary: `tone` is a ThemeKey ('dim' draws as dimColor) so body
 *  text follows the theme, and `paint` is a palette colour for chrome-like words
 *  (a picked route, an exhausted agent). Never both. */
export type Tone = 'text' | 'dim' | 'subtle' | 'success' | 'warning' | 'error' | 'suggestion'
export type Seg = { text: string; tone?: Tone; paint?: string; bold?: boolean }

/** one quota bar: the share used, the text beside it and the reserve floor
 *  (80 = 20% left), which the gradient bar marks under itself. */
export type Quota = { pct: number; used: string; floor?: number }

/** one agent or pool row (the Main board's Usage tab); a note row carries no bars. */
export type QuotaRow = {
  name: string
  nameTone?: Tone
  five?: Quota
  week?: Quota
  reset?: string
  note?: string
  noteTone?: Tone
}

/** one Jev routing decision, as its one line draws it. */
export type Decision = {
  task: string
  /** the route the decision picked, as `usage.json` names it */
  pick: string
  /** the board's short form of it (`codex sol high`) */
  pickShort?: string
  conf: string
  rule: string
  /** shown on a second line only when the whole line fits */
  candidates: string[]
}

export type UsageView = {
  mock: boolean
  headerLeft: string
  headerRight: string
  rows: QuotaRow[]
  /** the legend chips, each in its real role colour */
  legend: { label: string; role: PaletteRole }[]
  /** the dim source line under the quota rows */
  actions: string
  decisionsHeader: string
  decisionsRight: string
  decisions: Decision[]
  /** the routing card's dim hint */
  routingHint: string
  footerLeft: string
  footerRight: string
}

/** the fixture with the mock flag (C1/C3). */
export function usageView(): UsageView {
  return { mock: true, ...USAGE_FIXTURE }
}

/** the heat gradient's stops: the mockup's grad() runs green → amber → red. */
const HEAT: readonly string[] = [ROLE.ok.fg, ROLE.warn.fg, ROLE.err.fg]

/** the bar role at `pct`: the mockup's tone() — spent and nearly spent are red. */
export function barRole(pct: number): PaletteRole {
  return pct >= 100 ? 'err' : pct > 80 ? 'err' : pct >= 50 ? 'warn' : 'ok'
}

/** a row whose 5h window is spent (the board's `agy · 5h exhausted`). */
export function isExhausted(row: QuotaRow): boolean {
  return row.five?.used === '⊘' || (row.five?.pct ?? 0) >= 100
}

/** cut `text` to `w` cells, marking the cut with `…` — a row never wraps. */
function cut(text: string, w: number): string {
  if (w <= 0) return ''
  if (text.length <= w) return text
  if (w === 1) return '…'
  return `${text.slice(0, w - 1)}…`
}

/** hard-fit segments to `w` cells. */
function fitSegs(segs: Seg[], w: number): Seg[] {
  const out: Seg[] = []
  let used = 0
  for (const seg of segs) {
    const room = w - used
    if (room <= 0) break
    const text = seg.text.length > room ? cut(seg.text, room) : seg.text
    out.push({ ...seg, text })
    used += text.length
  }
  return out
}

const sg = (text: string, tone?: Tone, bold?: boolean): Seg => ({ text, tone, bold })

/** the segments as Texts, each in its tone's colour (or a palette colour). */
function segEls(els: Els, segs: Seg[]): RenderElement[] {
  const { Text } = els
  return segs.map((seg, i) => {
    const colour = seg.paint ?? seg.tone
    return seg.tone === 'dim' && seg.paint == null ? (
      <Text key={String(i)} bold={seg.bold} dimColor={true}>{seg.text}</Text>
    ) : colour != null && colour !== 'text' ? (
      <Text key={String(i)} bold={seg.bold} color={colour}>{seg.text}</Text>
    ) : (
      <Text key={String(i)} bold={seg.bold}>{seg.text}</Text>
    )
  })
}

/** one dim row: the left text, then the right tail (the tail survives). */
function hintRow(left: string, right: string, w: number): string {
  return `${cut(left, Math.max(0, w - right.length))}${cut(right, w)}`
}

/** chips side by side while the row fits — the tail is dropped, never cut. */
function chipRow(els: Els, chips: readonly { label: string; role: PaletteRole }[], w: number): RenderElement | null {
  const { Box } = els
  const kept: { label: string; role: PaletteRole }[] = []
  let used = 0
  for (const chip of chips) {
    const next = used + (kept.length === 0 ? 0 : 1) + chip.label.length + 2
    if (next > w) break
    kept.push(chip)
    used = next
  }
  if (kept.length === 0) return null
  return (
    <Box flexDirection="row" gap={1}>
      {kept.map((c, i) => (
        <Box key={String(i)}>{Chip(els, { label: c.label, role: c.role })}</Box>
      ))}
    </Box>
  )
}

/** a Section header whose right text is budgeted to the card, never wider. */
function sectionRow(els: Els, label: string, right: string, w: number): RenderElement {
  const budget = Math.max(0, w - label.length - 1)
  return Section(els, { label, right: right === '' ? undefined : cut(right, budget) })
}

/** one quota bar row: the `5h`/`wk` label, the bar, the share right-aligned. */
function barRow(els: Els, key: string, label: string, quota: Quota, w: number): RenderElement {
  const { Box, Text } = els
  const LW = 3 // `5h ` / `wk `
  const PW = 4 // the share, right-aligned
  const bw = Math.max(1, w - LW - PW - 2) // two gap columns
  const role = barRole(quota.pct)
  const bar = pickBar(
    els,
    key,
    gradientBar(quota.pct, bw, HEAT, quota.floor),
    SmoothBar(els, { pct: quota.pct, width: bw, role }),
  )
  const share = cut(quota.used, PW)
  return (
    <Box flexDirection="row" gap={1}>
      <Text dimColor={true}>{leftCol(label, LW)}</Text>
      {bar}
      <Text color={share === '⊘' ? ROLE.err.fg : role === 'warn' ? ROLE.warn.fg : undefined}>{share.padStart(PW)}</Text>
    </Box>
  )
}

/** one agent row: its name (err when its 5h window is spent) and its reset,
 *  then a 5h and a week bar, or its note where it has no window. */
function quotaRows(els: Els, row: QuotaRow, w: number, keyPrefix: string): RenderElement[] {
  const { Box, Text } = els
  if (row.note != null) {
    return [<Text key="note">{segEls(els, fitSegs([sg(row.name, row.nameTone), sg(' · '), sg(row.note, row.noteTone)], w))}</Text>]
  }
  const reset = cut(row.reset ?? '', Math.max(0, Math.floor(w / 3)))
  const dead = isExhausted(row)
  const name: Seg = {
    text: cut(row.name, Math.max(1, w - reset.length - 1)),
    bold: true,
    tone: dead ? undefined : row.nameTone,
    paint: dead ? ROLE.err.fg : undefined,
  }
  const out: RenderElement[] = [
    (
      <Box key="name" flexDirection="row" justifyContent="space-between" gap={1}>
        <Text>{segEls(els, [name])}</Text>
        <Text color={dead ? ROLE.warn.fg : undefined} dimColor={!dead}>{reset}</Text>
      </Box>
    ),
  ]
  if (row.five != null) out.push(barRow(els, `${keyPrefix}-5h`, '5h', row.five, w))
  if (row.week != null) out.push(barRow(els, `${keyPrefix}-wk`, 'wk', row.week, w))
  return out
}

/** the Quota card (C11): the rows, the legend chips and the dim source line. */
function quotaCard(els: Els, view: UsageView, w: number): RenderElement {
  const { Box, Text } = els
  const rows: RenderElement[] = [sectionRow(els, view.headerLeft, view.headerRight, w)]
  view.rows.forEach((row, i) => {
    if (i > 0) rows.push(<Text key={`gap-${String(i)}`}>{' '}</Text>)
    rows.push(...quotaRows(els, row, w, `quota-${String(i)}`))
  })
  const legend = chipRow(els, view.legend, w)
  if (legend != null) rows.push(legend)
  rows.push(ActionRow(els, { actions: cut(view.actions, w) }))
  return Card(els, {}, <Box flexDirection="column">{rows}</Box>)
}

/** one decision: `task → pick conf · rule`, plus its candidates on a second
 *  line only when the whole line fits (nothing is cut to show half a list). */
function decisionRows(els: Els, d: Decision, w: number): RenderElement[] {
  const { Text } = els
  const pick = d.pickShort ?? d.pick
  const conf = parseFloat(d.conf)
  const line: RenderElement = (
    <Text key="line">{segEls(els, fitSegs([
      sg(`${d.task} → `),
      { text: pick, bold: true, paint: ROLE.live.fg },
      sg(' · '),
      { text: d.conf, paint: ROLE[conf >= 0.7 ? 'ok' : 'warn'].fg },
      sg(' ' + d.rule, 'dim'),
    ], w))}</Text>
  )
  const candidates = d.candidates.join(' · ')
  const fitted = fitSegs([sg(candidates, 'dim')], w)
  const fits = fitted.length === 1 && fitted[0]?.text === candidates
  return fits ? [line, <Text key="cand">{segEls(els, fitted)}</Text>] : [line]
}

/** the routing card (C11): the decisions and the dim hint. */
function routingCard(els: Els, view: UsageView, w: number): RenderElement {
  const { Box, Text } = els
  const rows: RenderElement[] = [sectionRow(els, view.decisionsHeader, view.decisionsRight, w)]
  view.decisions.forEach((d, i) => {
    rows.push(<Text key={`gap-${String(i)}`}>{' '}</Text>)
    rows.push(...decisionRows(els, d, w))
  })
  rows.push(ActionRow(els, { actions: cut(view.routingHint, w) }))
  return Card(els, {}, <Box flexDirection="column">{rows}</Box>)
}

/** Pure render: elements only, no $, no state (C1). */
export function UsageTab(els: Els, view: UsageView, cols: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const w = Math.max(8, cols - 4) // the Card's border + padding each side (C11)
  void ui // the Usage board has no control; see the header note (C12)
  return (
    <Box flexDirection="column">
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      {quotaCard(els, view, w)}
      {routingCard(els, view, w)}
      <Text dimColor={true}>{hintRow(view.footerLeft, view.footerRight, cols)}</Text>
    </Box>
  )
}