// The /mk pane's Lanes tab (plan dashboard-v2-p0b, V2.1; contract C9/C11/C12):
// one kit Card per lane — a live border while it builds or is in review, a warn
// border while it is held — carrying the status chip and the round/age, the
// Raster stage strip (a glyph Text where the surface has no Raster), the agent
// and commit lines, the three-column gate table at >= 40 cols (gate chips under
// it), the review counts, the activity line and one ControlRow. Pure render
// (C1/C10): a control only writes `ui`, the local view state V1 wires, so
// without one the fixture's default state draws and a press does nothing.
//
// The boards Main (46 cols) and Narrow (36 cols) are the reference: a Card eats
// four columns (border + padding each side), so every row here is laid out for
// `cols - 4` and nothing draws wider than the pane.
import type { Elements, RenderElement } from 'claude-code'
import { LANES_FIXTURE } from './lanes.fixture'
import { NARROW } from './ui'
import { ActionRow, Card, Chip, ControlRow } from './kit'
import type { ControlState } from './kit'
import { ROLE } from './palette'
import type { PaletteRole } from './palette'
import { pickBar, stageBar } from './raster'

type Els = Elements[keyof Elements]

/** The local view state V1 hands every mock tab (C12): a Map-like get/set and
 *  nothing else. Declared here structurally — this file does not know (or ask)
 *  what backs it. */
export type TabUi = { get(key: string): string | undefined; set(key: string, value: string): void }

/** the row vocabulary: theme colour keys only (C5′), 'dim' as dimColor. */
export type Tone = 'text' | 'dim' | 'subtle' | 'success' | 'warning' | 'error' | 'suggestion'
export type Seg = { text: string; tone?: Tone; bold?: boolean }

/** one gate row: the gate name, the lane's value, the run's baseline. `short`
 *  is the chip that same gate collapses to under NARROW (the Narrow board's
 *  `t 412/2`); a gate without one (the review rounds) is not chipped. */
export type GateRow = {
  name: string
  lane: Seg[]
  baseline: string
  short?: { label: string; role: PaletteRole }
}

/** one lane, as its Card draws it. */
export type LaneData = {
  status: string
  task: string
  title: string
  /** the header's right tail: the round and the lane's age */
  right: string
  /** the stage strip: `done` of `total` stages done, the next one current */
  stage?: { done: number; total: number }
  /** the agent/model line */
  agent: Seg[]
  /** the agent line's short form under NARROW (the Narrow board) */
  agentShort?: string
  /** the last commit; the sha yields to the subject when the row is narrow */
  commit?: { sha: string; subject: string }
  /** one dim line under the agent line (what the lane waits on) */
  note?: Seg[]
  gate: GateRow[]
  /** the review round's counts, as chips: a blocker, a minor, what is open */
  reviews?: { blocker: number; minor: number; open: number }
  activity?: Seg[]
  /** the dim action line under the controls */
  actions: string
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

/** the header glyphs the boards draw per status. */
const STATUS_GLYPH: Record<string, string> = { building: '●', review: '◐', blocked: '!', quota: '⊘' }

/** the statuses a lane is HELD in — nothing to control, a warn border (C11). */
const HELD: readonly string[] = ['blocked', 'quota']

/** the Card role for a status: live while it works, warn while it is held. */
export function laneRole(status: string): PaletteRole {
  return HELD.includes(status) ? 'warn' : 'live'
}

/** the `ui` key a lane's control state lives under (C12). */
export function laneStateKey(task: string): string {
  return `lane:${task}`
}

/** cut `text` to `w` cells, marking the cut with `…` — a row never wraps. */
function cut(text: string, w: number): string {
  if (w <= 0) return ''
  if (text.length <= w) return text
  if (w === 1) return '…'
  return `${text.slice(0, w - 1)}…`
}

/** hard-fit segments to `w` cells (the head truncates). */
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

/** fit segments and pad them to exactly `w` cells (column alignment). */
function padSegs(segs: Seg[], w: number): Seg[] {
  const fitted = fitSegs(segs, w)
  const used = fitted.reduce((n, seg) => n + seg.text.length, 0)
  return [...fitted, { text: ' '.repeat(Math.max(0, w - used)) }]
}

const sg = (text: string, tone?: Tone, bold?: boolean): Seg => ({ text, tone, bold })

/** the segments as Texts, each in its tone's colour (or dim). */
function segEls(els: Els, segs: Seg[]): RenderElement[] {
  const { Text } = els
  return segs.map((seg, i) =>
    seg.tone === 'dim' ? (
      <Text key={String(i)} bold={seg.bold} dimColor={true}>{seg.text}</Text>
    ) : seg.tone && seg.tone !== 'text' ? (
      <Text key={String(i)} bold={seg.bold} color={seg.tone}>{seg.text}</Text>
    ) : (
      <Text key={String(i)} bold={seg.bold}>{seg.text}</Text>
    ),
  )
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

/** the header row: the status chip, the task and title, the round/age right. */
function laneHeader(els: Els, lane: LaneData, w: number): RenderElement {
  const { Box, Text } = els
  const role = laneRole(lane.status)
  const label = `${STATUS_GLYPH[lane.status] ?? ''} ${lane.status}`.trim()
  const right = cut(lane.right, Math.max(0, Math.floor(w / 2)))
  const room = w - (label.length + 2) - right.length - 2 // chip + two gap columns
  // the Narrow board's bare task id when the title would leave nothing readable
  const title = room - (lane.task.length + 1) < 6 ? lane.task : cut(`${lane.task} ${lane.title}`, room)
  return (
    <Box flexDirection="row" gap={1} justifyContent="space-between">
      {Chip(els, { label, role })}
      <Text bold={true}>{cut(title, Math.max(1, room))}</Text>
      <Text dimColor={true}>{right}</Text>
    </Box>
  )
}

/** the stage strip: a Raster where the surface has one, the mockup's glyph
 *  fallback (`●●●●●◐○○○○○○ stage 6/12`) where it does not. */
function laneStage(els: Els, lane: LaneData, w: number): RenderElement | null {
  const { Box, Text } = els
  if (lane.stage == null) return null
  const total = Math.max(1, Math.floor(lane.stage.total))
  const done = Math.max(0, Math.min(total - 1, Math.floor(lane.stage.done)))
  const rest = '○'.repeat(Math.max(0, total - done - 1))
  const label = `stage ${String(done + 1)}/${String(total)}`
  const fallback = (
    <Box flexDirection="row" gap={1}>
      <Text color={ROLE.ok.fg}>{'●'.repeat(done)}</Text>
      <Text color={ROLE.live.fg}>{'◐'}</Text>
      <Text dimColor={true}>{rest}</Text>
      <Text dimColor={true}>{cut(label, Math.max(0, w - total - 1))}</Text>
    </Box>
  )
  return pickBar(els, `lane-${lane.task.toLowerCase()}-stage`, stageBar(done, done, total, w), fallback)
}

/** one gate row as its three columns (name · lane · baseline), exactly `w` cells. */
function gateCols(name: string, laneSegs: Seg[], baseline: string, w: number): Seg[] {
  const NW = 10
  const BW = 8
  const LW = Math.max(4, w - NW - BW - 2)
  return [
    ...padSegs([sg(cut(name, NW), 'dim')], NW),
    sg(' '),
    ...padSegs(laneSegs, LW),
    sg(' '),
    { text: cut(baseline, BW).padStart(BW), tone: 'dim' },
  ]
}

/** the gates: the lane-vs-baseline table at >= NARROW, the Narrow board's chips
 *  under it. */
function gateSection(els: Els, lane: LaneData, w: number, wide: boolean): RenderElement | null {
  const { Box, Text } = els
  if (lane.gate.length === 0) return null
  if (wide) {
    const row = (name: string, value: Seg[], baseline: string, i: number) => (
      <Text key={String(i)}>{segEls(els, gateCols(name, value, baseline, w))}</Text>
    )
    return (
      <Box flexDirection="column">
        {row('gate', [sg('lane', 'dim')], 'baseline', 0)}
        {lane.gate.map((g, i) => row(g.name, g.lane, g.baseline, i + 1))}
      </Box>
    )
  }
  return chipRow(els, lane.gate.flatMap(g => (g.short == null ? [] : [{ label: g.short.label, role: g.short.role }])), w)
}

/** the review round's counts: a blocker red, a minor amber, what is open green. */
function reviewSection(els: Els, reviews: NonNullable<LaneData['reviews']>, w: number): RenderElement | null {
  return chipRow(els, [
    { label: `${String(reviews.blocker)} blocker`, role: 'err' },
    { label: `${String(reviews.minor)} minor`, role: 'warn' },
    { label: `${String(reviews.open)} open`, role: 'ok' },
  ], w)
}

/** the lane's control state: what `ui` holds, else the fixture's default — a
 *  lane that works starts `building`. C10: a press only reports the next
 *  state; the kit never calls a script. */
export function laneControlState(ui: TabUi | undefined, task: string): ControlState {
  const stored = ui?.get(laneStateKey(task))
  return stored === 'paused' || stored === 'stopped' ? stored : 'building'
}

/** one lane as one Card. `wide` picks the gate table over the gate chips. */
function laneCard(els: Els, lane: LaneData, cols: number, ui: TabUi | undefined): RenderElement {
  const { Box, Text } = els
  const w = Math.max(8, cols - 4) // the Card's border + padding each side (C11)
  const wide = cols >= NARROW
  const rows: RenderElement[] = [laneHeader(els, lane, w)]
  const stage = laneStage(els, lane, w)
  if (stage != null) rows.push(stage)
  // the Narrow board's short agent line under NARROW (the long one truncates)
  const agent = !wide && lane.agentShort != null ? [sg(lane.agentShort)] : lane.agent
  rows.push(<Text key="agent">{segEls(els, fitSegs(agent, w))}</Text>)
  if (lane.commit != null) {
    // the sha yields to the subject when both do not fit (the Narrow board)
    const full = `${lane.commit.sha} ${lane.commit.subject}`
    rows.push(<Text key="commit" dimColor={true}>{cut(full.length <= w ? full : lane.commit.subject, w)}</Text>)
  }
  if (lane.note != null) rows.push(<Text key="note">{segEls(els, fitSegs(lane.note, w))}</Text>)
  const gates = gateSection(els, lane, w, wide)
  if (gates != null) rows.push(gates)
  if (lane.reviews != null) {
    const reviews = reviewSection(els, lane.reviews, w)
    if (reviews != null) rows.push(reviews)
  }
  if (lane.activity != null) rows.push(<Text key="activity">{segEls(els, fitSegs(lane.activity, w))}</Text>)
  // a held lane has no agent to pause, so it draws no ControlRow (its dim
  // action line below carries what it can be told instead)
  if (!HELD.includes(lane.status)) {
    rows.push(ControlRow(els, laneControlState(ui, lane.task), w, next => { ui?.set(laneStateKey(lane.task), next) }, lane.task.toLowerCase()))
  }
  rows.push(ActionRow(els, { actions: cut(lane.actions, w) }))
  return Card(els, { role: laneRole(lane.status) }, <Box flexDirection="column">{rows}</Box>)
}

/** Pure render: elements only, no $, no state (C1). */
export function LanesTab(els: Els, view: LanesView, cols: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      <Text dimColor={true}>{hintRow(view.summary, view.summaryRight, cols)}</Text>
      {view.lanes.map(lane => (
        <Box key={lane.task} flexDirection="column">
          {laneCard(els, lane, cols, ui)}
        </Box>
      ))}
      <Text dimColor={true}>{hintRow(view.footerLeft, view.footerRight, cols)}</Text>
    </Box>
  )
}