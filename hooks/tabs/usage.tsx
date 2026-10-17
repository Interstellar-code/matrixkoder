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
import { ActionRow, BoxButton, Card, Chip, Section, SmoothBar } from './kit'
import { ROLE } from './palette'
import type { PaletteRole } from './palette'
import { gradientBar, pickBar } from './raster'
import type { SavePayload } from '../settings-io'

type Els = Elements[keyof Elements]

/** The local view state V1 hands every mock tab (C12), declared structurally. */
export type TabUi = { get(key: string): string | undefined; set(key: string, value: string): void; save?: (payload: SavePayload) => void }

/** one reserve-floor setting (% left) the quota card edits in place */
export type FloorSetting = { key: string; left: number; min: number; max: number }
/** the floors one routing group shares: Claude, or every other agent */
export type FloorCtl = { who: 'claude' | 'others'; label: string; five: FloorSetting; week: FloorSetting }

/** the row vocabulary: `tone` is a ThemeKey ('dim' draws as dimColor) so body
 *  text follows the theme, and `paint` is a palette colour for chrome-like words
 *  (a picked route, an exhausted agent). Never both. */
export type Tone = 'text' | 'dim' | 'subtle' | 'success' | 'warning' | 'error' | 'suggestion'
export type Seg = { text: string; tone?: Tone; paint?: string; bold?: boolean }

/** one quota bar: the share used, the text beside it and the reserve floor
 *  (80 = 20% left), which the gradient bar marks under itself. */
export type Quota = {
  pct: number
  used: string
  floor?: number
  /** CodexBar's burn-rate line (`7% in deficit · Runs out in 2d 4h`); paceBad when it runs out before the reset */
  pace?: string
  paceBad?: boolean
  /** the reserve-floor setting this bar's mark shows; drawn as −/+ beside the bar */
  floorSet?: FloorSetting
}

/** one agent or pool row (the Main board's Usage tab); a note row carries no bars. */
export type QuotaRow = {
  name: string
  nameTone?: Tone
  five?: Quota
  week?: Quota
  reset?: string
  note?: string
  noteTone?: Tone
  /** a pool row under its agent: drawn without the blank line that separates agents */
  pool?: boolean
  /** a monthly window (OpenCode Go), drawn as a third `mo` bar */
  month?: Quota
  /** the numbers are CodexBar's local estimate, not the provider's: an `est.` tag by the name */
  est?: boolean
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
  /** the reserve floors, editable here (live only; the fixture has none) */
  floors?: FloorCtl[]
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
const HEAT = (): readonly string[] => [ROLE.ok.fg, ROLE.warn.fg, ROLE.err.fg] // at draw time, for the theme

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
function barRow(els: Els, key: string, label: string, quota: Quota, w: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const LW = 3 // `5h ` / `wk `
  const PW = 4 // the share, right-aligned
  // the floor stepper `◖−◗ 40% ◖+◗` (11 cells + its gap) takes the bar's right end
  // every bar keeps the column, stepper or not, so bars line up across rows and cards
  const CW = ui?.save != null ? FLOOR_W + 1 : 0
  // with a stepper the bar keeps ~60% of what is left, so the floor column breathes
  const free = Math.max(1, w - LW - PW - 2 - CW) // two gap columns
  const bw = CW > 0 ? Math.max(1, Math.floor(free * 0.6)) : free
  const role = barRole(quota.pct)
  const bar = pickBar(
    els,
    key,
    gradientBar(quota.pct, bw, HEAT(), quota.floor),
    SmoothBar(els, { pct: quota.pct, width: bw, role }),
  )
  const share = cut(quota.used, PW)
  const line = (
    <Box key="bar" flexDirection="row" gap={1} width={w}>
      <Text dimColor={true}>{leftCol(label, LW)}</Text>
      {bar}
      <Text color={share === '⊘' ? ROLE.err.fg : role === 'warn' ? ROLE.warn.fg : undefined}>{share.padStart(PW)}</Text>
      {/* the slack the shortened bar leaves: pushes the floor column to the card's right edge */}
      {CW > 0 && <Box key="slack" flexGrow={1} />}
      {CW > 0 && (quota.floorSet != null ? floorStepper(els, key, quota.floorSet, ui) : <Box key={`${key}-floor`} width={FLOOR_W} flexShrink={0} />)}
    </Box>
  )
  if (quota.pace == null) return line
  // the pace sits under the bar, aligned with its left edge
  return (
    <Box key={key} flexDirection="column">
      {line}
      <Box key="pace" paddingLeft={LW + 1}>
        <Text color={quota.paceBad ? ROLE.err.fg : undefined} dimColor={!quota.paceBad}>{cut(quota.pace, w - LW - 1)}</Text>
      </Box>
    </Box>
  )
}

/** one agent row: its name (err when its 5h window is spent) and its reset,
 *  then a 5h and a week bar, or its note where it has no window. */
function quotaRows(els: Els, row: QuotaRow, w: number, keyPrefix: string, ui?: TabUi): RenderElement[] {
  const { Box, Text } = els
  if (row.note != null) {
    return [<Text key="note">{segEls(els, fitSegs([sg(row.name, row.nameTone), sg(' · '), sg(row.note, row.noteTone)], w))}</Text>]
  }
  const reset = cut(row.reset && row.reset !== '—' ? `resets ${row.reset}` : '', Math.max(0, Math.floor(w / 3)))
  const dead = isExhausted(row)
  const name: Seg = {
    text: cut(row.name, Math.max(1, w - reset.length - 1 - (row.est === true ? 6 : 0))),
    bold: true,
    tone: dead ? undefined : row.nameTone,
    paint: dead ? ROLE.err.fg : undefined,
  }
  const out: RenderElement[] = [
    (
      <Box key="name" flexDirection="row" justifyContent="space-between" gap={1} width={w}>
        <Text>{segEls(els, row.est === true ? [name, sg(' est.', 'dim')] : [name])}</Text>
        <Text color={dead ? ROLE.warn.fg : undefined} dimColor={!dead}>{reset}</Text>
      </Box>
    ),
  ]
  if (row.five != null) out.push(barRow(els, `${keyPrefix}-5h`, '5h', row.five, w, ui))
  if (row.week != null) out.push(barRow(els, `${keyPrefix}-wk`, 'wk', row.week, w, ui))
  if (row.month != null) out.push(barRow(els, `${keyPrefix}-mo`, 'mo', row.month, w, ui))
  return out
}

const FLOOR_W = 17 // `◖ − ◗ 40% ◖ + ◗`: two 6-cell buttons, the value and its gaps

/** the reserve-floor stepper beside a bar: % left before routing skips the agent; a press saves at once */
function floorStepper(els: Els, key: string, f: FloorSetting, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const step = (by: number): void => {
    const value = Math.min(f.max, Math.max(f.min, f.left + by))
    if (value !== f.left) ui?.save?.({ sets: [{ key: f.key, value, list: false }], globals: [], visibility: [] })
  }
  return (
    <Box key={`${key}-floor`} flexDirection="row" width={FLOOR_W} flexShrink={0} gap={1} justifyContent="flex-end">
      {BoxButton(els, { key: `${key}-floor-dec`, label: '−', onPress: () => step(-5) })}
      <Text bold={true}>{`${String(f.left)}%`.padStart(3)}</Text>
      {BoxButton(els, { key: `${key}-floor-inc`, label: '+', onPress: () => step(5) })}
    </Box>
  )
}

/** the Quota card (C11): one bordered card per agent (its pools inside),
 *  the legend chips and the dim source line. */
function quotaCard(els: Els, view: UsageView, w: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const rows: RenderElement[] = [sectionRow(els, view.headerLeft, view.headerRight, w)]
  const msg = ui?.get('settings:save:msg') ?? ''
  if (msg !== '') rows.push(<Text key="floor-msg" dimColor={true}>{cut(msg, w)}</Text>)
  // an agent row opens a group; its `└` pool rows join it
  const groups: QuotaRow[][] = []
  for (const row of view.rows) {
    if (row.pool === true && groups.length > 0) groups[groups.length - 1]!.push(row)
    else groups.push([row])
  }
  const iw = Math.max(8, w - 4) // the inner card's border + padding
  groups.forEach((g, gi) => {
    const body: RenderElement[] = []
    g.forEach((row, i) => body.push(<Box key={`r-${String(i)}`} flexDirection="column">{quotaRows(els, row, iw, `quota-${String(gi)}-${String(i)}`, ui)}</Box>))
    // a fixed-width column so every agent card spans the same width
    rows.push(<Box key={`g-${String(gi)}`} width={w} flexDirection="column">{Card(els, {}, <Box flexDirection="column">{body}</Box>)}</Box>)
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
  if (view.decisions.length === 0) rows.push(<Text key="none" dimColor={true}>no routing data yet</Text>)
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
  return (
    <Box flexDirection="column">
      {view.mock && <Text bold={true} color={ROLE.warn.fg}>MOCK · fixture data · not this run</Text>}
      {quotaCard(els, view, w, ui)}
      {routingCard(els, view, w)}
      <Text dimColor={true}>{hintRow(view.footerLeft, view.footerRight, cols)}</Text>
    </Box>
  )
}