// the /mk pane's Overview tab (plan dashboard-v2-p0, T1.1): the artboard-A
// "Checkout v2" view rendered from its fixture — run header, plan waves, live
// tasks, lanes, usage bars, the waiting question and recent, in the pane's
// terminal voice (C5: no borders, no buttons, every row cut to `cols`). The
// names below are the C1 contract the later phases build against.
import type { Elements } from 'claude-code'
import { AGENT_COLOR, AGENT_GLYPH, NARROW, STATUS_COLOR, bar, fitLegend, leftCol, rightCol } from './ui'
import { OVERVIEW_FIXTURE } from './overview.fixture'

type Els = Elements[keyof Elements]

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
  waiting: { since: string; question: string; options: string[]; rec: number }
  recent: { t: string; text: string }[]
}

/** The Overview's view: the fixture data plus the mock flag (C1). */
export type OverviewView = OverviewData & { mock: boolean }

/** T1 renders the fixture with the MOCK row; real run data lands in a later phase. */
export function overviewView(): OverviewView {
  return { ...OVERVIEW_FIXTURE, mock: true }
}

// the board's task-row tones: in-flight rows read as one colour (blue→cyan),
// attention as amber→yellow, planned dim — checkbox and status word share it
const TASK_TONE: Record<string, { box: string; color: string }> = {
  building: { box: '[•]', color: 'cyan' },
  review: { box: '[•]', color: 'cyan' },
  blocked: { box: '[!]', color: 'yellow' },
  quota: { box: '[!]', color: 'yellow' },
  planned: { box: '[ ]', color: 'gray' },
}

// the board's lane glyphs (artboard A), not STATUS_GLYPH: the Overview draws
// ● ◐ ! ⊘ for its four live lanes
const LANE_GLYPH: Record<string, string> = { building: '●', review: '◐', blocked: '!', quota: '⊘' }

const WAVE_COLOR: Record<string, string> = { done: 'green', active: 'cyan', planned: 'gray' }

/** `…`-truncate to `budget` columns (dashboard.tsx's plan-row cut, local: not exported there). */
function ellipsed(text: string, budget: number): string {
  return text.length <= budget ? text : `${text.slice(0, Math.max(1, budget) - 1)}…`
}

/** the pane's usage colour rule (dashboard.tsx pctColor): red past 85 or exhausted, yellow past 60. */
function pctColor(pct: number, exhausted: boolean): string {
  if (exhausted || pct > 85) return 'red'
  if (pct > 60) return 'yellow'
  return 'green'
}

/** Pure render: elements only, no $, no state (C1). */
export function OverviewTab(els: Els, view: OverviewView, cols: number) {
  const { Box, Text } = els
  const compact = cols < NARROW
  return (
    <Box flexDirection="column" gap={1}>
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      {RunBlock(els, view.run, cols, compact)}
      {PlanBlock(els, view, cols, compact)}
      {LanesBlock(els, view.lanes, cols, compact)}
      {UsageBlock(els, view.usage, compact)}
      {WaitingBlock(els, view.waiting, cols)}
      {RecentBlock(els, view.recent, cols)}
    </Box>
  )
}

function RunBlock(els: Els, run: OverviewData['run'], cols: number, compact: boolean) {
  const { Box, Text } = els
  const stage = compact ? `stg ${run.stage}/${run.stages}` : `stage ${run.stage}/${run.stages} ${run.stageWord}`
  // the id line yields whole items first (fitLegend), then hard-cuts: the name
  // and the stage never move; the narrow board drops the id line entirely
  const budget = Math.max(1, cols - run.name.length - stage.length - 2)
  const idLine = compact ? '' : leftCol(fitLegend([run.id, `run ${run.short}`], budget), budget)
  const prefix = compact ? '▶ ' : '▶ Next: '
  return (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Box flexDirection="row" gap={1} flexShrink={1}>
          <Text bold={true}>{run.name}</Text>
          {idLine !== '' && <Text dimColor={true}>{idLine}</Text>}
        </Box>
        <Text color="cyan" bold={true}>{stage}</Text>
      </Box>
      <Text><Text color="cyan" bold={true}>{prefix}</Text>{leftCol(run.next, cols - prefix.length)}</Text>
    </Box>
  )
}

function PlanBlock(els: Els, view: OverviewData, cols: number, compact: boolean) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between">
        <Box flexDirection="row" gap={1}>
          <Text bold={true}>Plan</Text>
          <Text color="green">{view.plan.done}/{view.plan.total}{compact ? '' : ' done'}</Text>
        </Box>
        <Box flexDirection="row" gap={1}>
          {view.waves.map(w => (
            <Box key={w.label} flexDirection="row">
              <Text dimColor={true}>{w.label} </Text>
              <Text color={WAVE_COLOR[w.tone] ?? 'gray'}>{w.glyphs}</Text>
            </Box>
          ))}
        </Box>
      </Box>
      {view.tasks.map(task => {
        const tone = TASK_TONE[task.status] ?? { box: '[ ]', color: 'gray' }
        const word = compact ? task.short : task.note
        // the title yields to the checkbox, id and status column (§3.7: 14 chars compact)
        const budget = Math.max(1, cols - (tone.box.length + 1 + task.id.length + 1) - 1 - word.length)
        const title = compact ? leftCol(task.title, Math.min(14, budget)) : ellipsed(task.title, budget)
        return (
          <Box key={task.id} flexDirection="row" justifyContent="space-between" gap={1}>
            <Box flexDirection="row" gap={1} flexShrink={1}>
              <Text color={tone.color} bold={true}>{tone.box}</Text>
              <Text bold={true}>{task.id}</Text>
              <Text dimColor={task.status === 'planned'}>{title}</Text>
            </Box>
            <Text color={tone.color}>{word}</Text>
          </Box>
        )
      })}
    </Box>
  )
}

function LanesBlock(els: Els, lanes: OverviewData['lanes'], cols: number, compact: boolean) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Text bold={true}>Lanes</Text>
      {lanes.map(lane => {
        const name = `${AGENT_GLYPH[lane.agent] ?? '⌂'} ${lane.agent.toUpperCase()}`
        const word = `${LANE_GLYPH[lane.status] ?? '·'} ${compact ? lane.short : lane.note}`
        // the narrow board keeps only the short model and drops the effort
        const modelEffort = compact ? lane.modelShort : `${lane.model} ${lane.effort}`
        const budget = Math.max(1, cols - (lane.slot.length + 1 + name.length + 1) - 1 - word.length)
        return (
          <Box key={lane.slot} flexDirection="row" justifyContent="space-between" gap={1}>
            <Box flexDirection="row" gap={1} flexShrink={1}>
              <Text dimColor={true}>{lane.slot}</Text>
              <Text color={AGENT_COLOR[lane.agent] ?? 'cyan'} bold={true}>{name}</Text>
              <Text dimColor={true}>{leftCol(modelEffort, budget)}</Text>
            </Box>
            <Text color={STATUS_COLOR[lane.status] ?? 'gray'}>{word}</Text>
          </Box>
        )
      })}
    </Box>
  )
}

function UsageBlock(els: Els, usage: OverviewData['usage'], compact: boolean) {
  const { Box, Text } = els
  const barW = compact ? 5 : 7 // the pane's usage bar widths (design §4.2)
  const NAME_W = compact ? 8 : 12
  const NUM_W = compact ? 11 : 19 // '38% · 2h10m' compact; wide fits agy's '⊘ exhausted · 19:05'
  return (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between">
        <Text bold={true}>Usage</Text>
        <Text dimColor={true}>{rightCol(compact ? '5h' : '5h · week · reset', NUM_W)}</Text>
      </Box>
      {usage.map(u => {
        const color = pctColor(u.pct, u.exhausted)
        const nums = u.exhausted
          ? (compact ? `⊘ ${u.until ?? '—'}` : `⊘ exhausted · ${u.until ?? '—'}`)
          : (compact ? `${u.pct}% · ${u.reset ?? '—'}` : `${u.pct}% · ${u.weekPct}% · ${u.reset ?? '—'}`)
        return (
          <Box key={u.label} flexDirection="row" gap={1}>
            <Text>{leftCol(compact ? u.short : u.label, NAME_W)}</Text>
            <Text color={color} bold={u.exhausted}>{bar(u.pct, barW)}</Text>
            <Text color={u.exhausted ? 'red' : undefined} bold={u.exhausted}>{rightCol(nums, NUM_W)}</Text>
          </Box>
        )
      })}
    </Box>
  )
}

// the bordered decision card belongs to the Waiting tab (C5): here the
// question is a summary — one clipped line, lettered options, no border
function WaitingBlock(els: Els, waiting: OverviewData['waiting'], cols: number) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between">
        <Text color="yellow" bold={true}>! Waiting on you</Text>
        <Text dimColor={true}>since {waiting.since}</Text>
      </Box>
      <Text>{leftCol(waiting.question, cols)}</Text>
      {waiting.options.map((opt, i) => (
        <Text
          key={opt}
          color={i === waiting.rec ? 'cyan' : undefined}
          bold={i === waiting.rec}
          dimColor={i !== waiting.rec}
        >
          {leftCol(`${String.fromCharCode(65 + i)} · ${opt}`, cols)}
        </Text>
      ))}
    </Box>
  )
}

function RecentBlock(els: Els, recent: OverviewData['recent'], cols: number) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Text bold={true}>Recent</Text>
      {recent.map((ev, i) => (
        <Box key={`ev${String(i)}`} flexDirection="row" gap={1}>
          <Text dimColor={true}>{ev.t}</Text>
          <Text dimColor={true} wrap="truncate-end">{leftCol(ev.text, cols - ev.t.length - 1)}</Text>
        </Box>
      ))}
    </Box>
  )
}
