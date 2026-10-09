// the /mk pane's Overview tab (plan dashboard-v2-p0b, V1.1, contract C9/C11):
// the Main.dc.html / Narrow.dc.html Overview boards redrawn from the V0 kit —
// five Cards (Plan, Lanes, Usage, Waiting, Recent), each budgeted for `cols - 4`
// (a Card eats a border and a padding cell per side). Fixture data unchanged
// (C6); the Old tab's fixture names (OverviewData, OverviewView, overviewView,
// OverviewTab) stay the contract the pane and the tests build against.
import type { Elements } from 'claude-code'
import { AGENT_COLOR, AGENT_GLYPH, NARROW } from './ui'
import { ActionButtons, Card, Chip, Section, SmoothBar } from './kit'
import type { ActionSpec } from './kit'
import { gradientBar, laneStrip, pickBar } from './raster'
import { PALETTE, ROLE } from './palette'
import type { PaletteRole } from './palette'
import { OVERVIEW_FIXTURE } from './overview.fixture'

type Els = Elements[keyof Elements]

/** C12: this tab's view-state handle — declared locally (structural typing; no
 *  import crosses lanes). */
type TabUi = { get(key: string): string | undefined; set(key: string, value: string): void }

/** The fixture's shape minus the mock flag the factory stamps (C1). */
export type OverviewData = {
  run: {
    name: string
    id: string
    short: string
    stage: number
    stages: number
    stageWord: string
    next: string
  }
  plan: { done: number; total: number }
  waves: { label: string; glyphs: string; tone: 'done' | 'active' | 'planned' }[]
  tasks: { id: string; title: string; status: string; note: string; short: string }[]
  lanes: { slot: string; agent: string; model: string; modelShort: string; effort: string; status: string; note: string; short: string }[]
  usage: { label: string; short: string; pct: number; weekPct: number; reset: string | null; exhausted: boolean; until: string | null }[]
  waiting: {
    since: string
    question: string
    options: string[]
    rec: number
    /** the Narrow board's waiting card: the short question and its button labels */
    shortQuestion: string
    shortOptions: readonly string[]
  }
  recent: { t: string; text: string }[]
}

/** The Overview's view: the fixture data plus the mock flag (C1). */
export type OverviewView = OverviewData & { mock: boolean }

/** T1 renders the fixture with the MOCK row; real run data lands in a later phase. */
export function overviewView(): OverviewView {
  return { ...OVERVIEW_FIXTURE, mock: true }
}

// the boards' status vocabulary: the Overview's own glyphs (Main.dc.html's W2
// glyphs '●◐!⊘' — blocked is '!' here, quota '⊘') and the palette role each
// status paints with
const TASK_GLYPH: Record<string, string> = { building: '●', review: '◐', blocked: '!', quota: '⊘', planned: '○' }
const TASK_ROLE: Record<string, PaletteRole> = {
  dispatched: 'live', building: 'live', verifying: 'live', review: 'live', fix: 'live', qa: 'live',
  merged: 'ok', done: 'ok',
  blocked: 'warn', quota: 'warn', stalled: 'warn', provider: 'warn',
  planned: 'idle', routed: 'idle',
}
const WAVE_ROLE: Record<OverviewData['waves'][number]['tone'], PaletteRole> = { done: 'ok', active: 'live', planned: 'idle' }
const DONE = new Set(['done', 'merged'])
/** the usage bar's heat stops (Main.dc.html's heat(): green → amber → red). */
const USAGE_STOPS: readonly string[] = [PALETTE.grn, PALETTE.amb, PALETTE.red]
/** the usage row's fixed columns: the 8-cell name, then the numbers' width. */
const NAME_W = 8
const NUM_W = { wide: 17, compact: 10 } as const

const glyphOf = (status: string): string => TASK_GLYPH[status] ?? '·'
const roleOf = (status: string): PaletteRole => TASK_ROLE[status] ?? 'idle'

/** `…`-truncate to `w` cells (the pane's one cut voice). */
function clip(text: string, w: number): string {
  return text.length <= w ? text : `${text.slice(0, Math.max(1, w) - 1)}…`
}

/** word-wrap to rows of at most `w` cells (the Waiting question); an overlong
 *  word is hard-sliced so no row can exceed the card's content width. */
function wrap(text: string, w: number): string[] {
  const width = Math.max(1, Math.floor(w))
  const lines: string[] = []
  let line = ''
  for (const word of text.split(/\s+/).filter(x => x !== '')) {
    if (word.length > width) {
      if (line !== '') { lines.push(line); line = '' }
      for (let i = 0; i < word.length; i += width) lines.push(word.slice(i, i + width))
      continue
    }
    const cand = line === '' ? word : `${line} ${word}`
    if (cand.length > width) { lines.push(line); line = word } else { line = cand }
  }
  if (line !== '') lines.push(line)
  return lines.length > 0 ? lines : ['']
}

/** Pure render: elements only, no $, no state (C1); `ui` is the C12 view state. */
export function OverviewTab(els: Els, view: OverviewData, cols: number, ui?: TabUi) {
  const { Box, Text } = els
  const compact = cols < NARROW
  const W = Math.max(1, cols - 4) // the Card's content width (border + padding per side)
  return (
    <Box flexDirection="column" gap={1}>
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      {PlanCard(els, view, W, compact)}
      {LanesCard(els, view.lanes, W, compact)}
      {UsageCard(els, view.usage, W, compact)}
      {WaitingCard(els, view.waiting, W, compact, ui)}
      {RecentCard(els, view.recent, W)}
    </Box>
  )
}

/** PLAN 3/8 — the wave chips, the segment strip and the task rows. */
function PlanCard(els: Els, view: OverviewData, W: number, compact: boolean) {
  const { Box, Text } = els
  const live = view.tasks.filter(t => !DONE.has(t.status) && t.status !== 'planned').length
  // the strip: one segment per task, coloured by that task's role (Main's
  // planStrip); without a Raster the wave glyphs draw as one dim row
  const strip = pickBar(
    els,
    'overview-plan-strip',
    laneStrip(view.tasks.map(t => roleOf(t.status)), W),
    <Text dimColor={true}>{view.waves.map(w => w.glyphs).join('')}</Text>,
  )
  return Card(els, {}, (
    <Box flexDirection="column">
      {Section(els, { label: 'Plan', right: `${view.plan.done}/${view.plan.total}` })}
      <Box flexDirection="row" gap={1}>
        {view.waves.map(w => {
          const label = compact
            ? `${w.label} ${w.tone === 'done' ? '✓' : w.tone === 'active' ? `${live}/${view.tasks.length}` : '○'}`
            : `${w.label} ${w.glyphs}`
          return Chip(els, { label, role: WAVE_ROLE[w.tone] })
        })}
      </Box>
      {strip}
      {view.tasks.map(task => {
        // the canvas chip is a short status label ('● build 14m'), so it
        // carries the fixture's short word at every width and the title keeps
        // its cells (the wide `note` spelling stays in the fixture)
        const word = task.short
        const right = task.status === 'planned'
          ? <Text dimColor={true}>{`${glyphOf(task.status)} ${word}`}</Text>
          : Chip(els, { label: `${glyphOf(task.status)} ${word}`, role: roleOf(task.status) })
        const rightW = task.status === 'planned' ? word.length + 2 : word.length + glyphOf(task.status).length + 3
        const budget = Math.max(1, W - task.id.length - rightW - 2)
        return (
          <Box key={task.id} flexDirection="row" gap={1}>
            <Text bold={true}>{task.id}</Text>
            <Text wrap="truncate-end">{clip(task.title, budget)}</Text>
            <Box flexGrow={1} />
            {right}
          </Box>
        )
      })}
    </Box>
  ))
}

/** LANES 4 — one row per lane: slot, agent, model · effort, status chip. */
function LanesCard(els: Els, lanes: OverviewData['lanes'], W: number, compact: boolean) {
  const { Box, Text } = els
  return Card(els, {}, (
    <Box flexDirection="column">
      {Section(els, { label: 'Lanes', right: String(lanes.length) })}
      {lanes.map(lane => {
        const name = `${AGENT_GLYPH[lane.agent] ?? '⌂'} ${lane.agent.toUpperCase()}`
        const word = compact ? lane.short : lane.note
        const label = `${glyphOf(lane.status)} ${word}`
        const modelEffort = compact ? `${lane.modelShort} ${lane.effort.slice(0, 3)}` : `${lane.model} ${lane.effort}`
        const budget = Math.max(1, W - lane.slot.length - name.length - (label.length + 2) - 3)
        return (
          <Box key={lane.slot} flexDirection="row" gap={1}>
            <Text dimColor={true}>{lane.slot}</Text>
            <Text color={AGENT_COLOR[lane.agent] ?? 'cyan'} bold={true}>{name}</Text>
            <Text dimColor={true} wrap="truncate-end">{clip(modelEffort, budget)}</Text>
            <Box flexGrow={1} />
            {Chip(els, { label, role: roleOf(lane.status) })}
          </Box>
        )
      })}
    </Box>
  ))
}

/** USAGE — gradient bars (Raster) with the SmoothBar fallback, then the numbers. */
function UsageCard(els: Els, usage: OverviewData['usage'], W: number, compact: boolean) {
  const { Box, Text } = els
  const numW = compact ? NUM_W.compact : NUM_W.wide
  const barW = Math.max(3, W - NAME_W - numW - 2)
  const role = (u: OverviewData['usage'][number]): PaletteRole => {
    if (u.exhausted || u.pct > 80) return 'err'
    if (u.pct >= 50) return 'warn'
    return 'ok'
  }
  return Card(els, {}, (
    <Box flexDirection="column">
      {Section(els, { label: 'Usage', right: compact ? '5h · reset' : '5h · wk · reset' })}
      {usage.map(u => {
        const nums = u.exhausted
          ? (compact ? `⊘ ${u.until ?? '—'}` : `exhausted · ${u.until ?? '—'}`)
          : (compact ? `${u.pct}% ${u.reset ?? '—'}` : `${u.pct}% · ${u.weekPct}% · ${u.reset ?? '—'}`)
        return (
          <Box key={u.label} flexDirection="row" gap={1}>
            <Text>{u.short.padEnd(NAME_W).slice(0, NAME_W)}</Text>
            {pickBar(
              els,
              `overview-usage-${u.short}`,
              gradientBar(u.pct, barW, u.exhausted ? [PALETTE.red] : USAGE_STOPS),
              SmoothBar(els, { pct: u.pct, width: barW, role: role(u) }),
            )}
            <Text color={u.exhausted ? ROLE.err.fg : undefined} bold={u.exhausted}>{clip(nums, numW)}</Text>
          </Box>
        )
      })}
    </Box>
  ))
}

/** WAITING — the warn Card: solid chip header, the wrapped question and the
 *  a/b/c option buttons (folded into ⋯ More, the reveal in view state, C10). */
function WaitingCard(els: Els, waiting: OverviewData['waiting'], W: number, compact: boolean, ui?: TabUi) {
  const { Box, Text } = els
  const question = compact ? waiting.shortQuestion : waiting.question
  const options = compact ? waiting.shortOptions : waiting.options
  const pick = ui?.get('waiting:pick')
  const revealed = ui?.get('waiting:more') === '1'
  const glyphs = ['a', 'b', 'c']
  const specs: ActionSpec[] = options.slice(0, 3).map((opt, i) => {
    const glyph = glyphs[i] ?? '·'
    const chosen = pick === glyph || (pick == null && i === waiting.rec)
    return {
      key: `waiting-opt-${glyph}`,
      glyph,
      word: opt,
      tone: chosen ? 'primary' : 'default',
      primary: chosen,
      onPress: () => { ui?.set('waiting:pick', glyph) },
    }
  })
  return Card(els, { role: 'warn' }, (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        {Chip(els, { label: compact ? '! WAITING' : '! WAITING ON YOU', role: 'warn', solid: true })}
        <Text dimColor={true}>{waiting.since}</Text>
      </Box>
      {wrap(question, W).map((line, i) => (
        <Text key={`q${String(i)}`}>{line}</Text>
      ))}
      {ActionButtons(els, specs, W, ui == null ? undefined : () => { ui.set('waiting:more', '1') })}
      {revealed && options.slice(1).map((opt, i) => (
        <Text key={`fold${String(i)}`} dimColor={true}>{clip(`${glyphs[i + 1] ?? '·'} ${opt}`, W)}</Text>
      ))}
    </Box>
  ))
}

/** RECENT — dim time + text rows. */
function RecentCard(els: Els, recent: OverviewData['recent'], W: number) {
  const { Box, Text } = els
  return Card(els, {}, (
    <Box flexDirection="column">
      {Section(els, { label: 'Recent' })}
      {recent.map(ev => (
        <Box key={`${ev.t}${ev.text}`} flexDirection="row" gap={1}>
          <Text dimColor={true}>{ev.t}</Text>
          <Text wrap="truncate-end">{clip(ev.text, Math.max(1, W - ev.t.length - 1))}</Text>
        </Box>
      ))}
    </Box>
  ))
}
