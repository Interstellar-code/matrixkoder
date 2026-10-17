// the /mk pane's Overview tab (plan dashboard-v2-p0b, V1.1, contract C9/C11):
// the Main.dc.html / Narrow.dc.html Overview boards redrawn from the V0 kit —
// five Cards (Plan, Lanes, Usage, Waiting, Recent), each budgeted for `cols - 4`
// (a Card eats a border and a padding cell per side). Fixture data unchanged
// (C6); the fixture names (OverviewData, OverviewView, overviewView,
// OverviewTab) stay the contract the pane and the tests build against.
import type { Elements, RenderChildren, RenderElement } from 'claude-code'
import { AGENT_COLOR, AGENT_GLYPH, NARROW } from './ui'
import { ActionButtons, Card, SmoothBar } from './kit'
import type { ActionSpec } from './kit'
import { gradientBar, pickBar } from './raster'
import { PALETTE, ROLE, THEME } from './palette'
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
const TASK_GLYPH: Record<string, string> = { building: '●', review: '◐', blocked: '!', quota: '⊘', planned: '○', rework: '▲' }
const TASK_ROLE: Record<string, PaletteRole> = {
  dispatched: 'live', building: 'live', verifying: 'live', review: 'live', fix: 'live', qa: 'live',
  merged: 'ok', done: 'ok',
  blocked: 'warn', quota: 'warn', stalled: 'warn', provider: 'warn',
  planned: 'idle', routed: 'idle',
  // F.8: a rework task is the user's again — warn, like the other NEEDS YOU states
  rework: 'warn',
}
const WAVE_ROLE: Record<OverviewData['waves'][number]['tone'], PaletteRole> = { done: 'ok', active: 'live', planned: 'idle' }
const DONE = new Set(['done', 'merged'])
/** test-visible alias: the F.8 not-done-count tests read the set directly */
export const DONE_STATUSES_FOR_TEST = DONE
/** the usage bar's heat stops (Main.dc.html's heat(): green → amber → red). */
const USAGE_STOPS = (): readonly string[] => [PALETTE.grn, PALETTE.amb, PALETTE.red] // at draw time, for the theme
/** the usage row's fixed columns: the 8-cell name, then the numbers' width. */
const NAME_W = 8
const NUM_W = { wide: 17, compact: 10 } as const
/** C12: the Waiting Card's view-state keys, namespaced so they never clash with
 *  another tab's (`waiting:pick` means the Waiting tab's own option key). */
const PICK_KEY = 'overview:waiting:pick'
const MORE_KEY = 'overview:waiting:more'

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
export function OverviewTab(els: Els, view: OverviewData & { mock?: boolean }, cols: number, ui?: TabUi) {
  const { Box, Text } = els
  const compact = cols < NARROW
  const W = Math.max(1, cols - 4) // the Card's content width (border + padding per side)
  return (
    <Box flexDirection="column" gap={1}>
      {view.mock !== false && <Text bold={true} color={ROLE.warn.fg}>MOCK · fixture data · not this run</Text>}
      {PlanCard(els, view, W, compact)}
      {LanesCard(els, view.lanes, W, compact)}
      {UsageCard(els, view.usage, W, compact)}
      {view.waiting.question !== '' && WaitingCard(els, view.waiting, W, compact, ui)}
      {RecentCard(els, view.recent, W)}
    </Box>
  )
}

/** a card's header row: the `> LABEL` on the left, any coloured parts on the right (the board's
 *  `PLAN 3/8 DONE ……… W1 ✓✓✓ W2 ●◐!⊘ W3 ○`). */
function HeadRow(els: Els, label: string, right: RenderChildren): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="row" justifyContent="space-between" gap={1}>
      <Text bold={true} color={PALETTE.mut}>{`${THEME.name === 'matrix' ? '> ' : ''}${label.toUpperCase()}`}</Text>
      <Text>{right}</Text>
    </Box>
  )
}

/** the task's checkbox, as the board draws it: `[•]` live, `[!]` needs you, `[✓]` done, `[ ]` planned */
function boxOf(status: string): string {
  if (DONE.has(status)) return '[✓]'
  const role = roleOf(status)
  return role === 'live' ? '[•]' : role === 'warn' ? '[!]' : '[ ]'
}

/** PLAN 3/8 DONE — wave glyphs on the right, one `[•] P4 title …… building 14m` row per task. */
function PlanCard(els: Els, view: OverviewData, W: number, compact: boolean) {
  const { Box, Text } = els
  const waves = view.waves.map((w, i) => (
    <Text key={w.label} color={ROLE[WAVE_ROLE[w.tone]].fg} dimColor={w.tone === 'planned'}>{`${i > 0 ? ' ' : ''}${w.label} ${w.glyphs}`}</Text>
  ))
  return Card(els, {}, (
    <Box flexDirection="column">
      {HeadRow(els, `Plan ${view.plan.done}/${view.plan.total} done`, compact ? null : waves)}
      {view.tasks.length === 0 && <Text dimColor={true}>no tasks yet</Text>}
      {view.tasks.map(task => {
        const planned = task.status === 'planned'
        const right = compact ? task.short : task.note
        const role = roleOf(task.status)
        // box · id · title · filler · right, with a gap column between each pair
        const budget = Math.max(1, W - 3 - task.id.length - right.length - 4)
        return (
          <Box key={task.id} flexDirection="row" gap={1}>
            <Text color={planned ? undefined : ROLE[role].fg} dimColor={planned}>{boxOf(task.status)}</Text>
            <Text bold={!planned} dimColor={planned}>{task.id}</Text>
            <Text dimColor={planned} wrap="truncate-end">{clip(task.title, budget)}</Text>
            <Box flexGrow={1} />
            <Text color={planned ? undefined : ROLE[role].fg} dimColor={planned}>{right}</Text>
          </Box>
        )
      })}
    </Box>
  ))
}

/** LANES — `p4 ⌂ CODEX gpt-6.1-sol high …… ● building`. */
function LanesCard(els: Els, lanes: OverviewData['lanes'], W: number, compact: boolean) {
  const { Box, Text } = els
  return Card(els, {}, (
    <Box flexDirection="column">
      {HeadRow(els, 'Lanes', <Text dimColor={true}>{String(lanes.length)}</Text>)}
      {lanes.length === 0 && <Text dimColor={true}>none live</Text>}
      {lanes.map(lane => {
        const name = `${AGENT_GLYPH[lane.agent] ?? '⌂'} ${lane.agent.toUpperCase()}`
        const label = `${glyphOf(lane.status)} ${compact ? lane.short : lane.note}`
        const model = compact ? lane.modelShort : lane.model
        const effort = compact ? lane.effort.slice(0, 3) : lane.effort
        // slot · agent · model effort · filler · status: four gap columns to give back
        const budget = Math.max(1, W - lane.slot.length - name.length - label.length - 4)
        // one cut over `model effort`, then the model part bold and the rest dim
        const cut = clip(`${model} ${effort}`, budget)
        return (
          <Box key={lane.slot} flexDirection="row" gap={1}>
            <Text dimColor={true}>{lane.slot}</Text>
            <Text color={AGENT_COLOR[lane.agent] ?? ROLE.live.fg} bold={true}>{name}</Text>
            <Text wrap="truncate-end">
              <Text bold={true}>{cut.slice(0, model.length)}</Text>
              {cut.length > model.length && <Text dimColor={true}>{cut.slice(model.length)}</Text>}
            </Text>
            <Box flexGrow={1} />
            <Text color={ROLE[roleOf(lane.status)].fg}>{label}</Text>
          </Box>
        )
      })}
    </Box>
  ))
}

/** USAGE — `claude (all) …… [bar] 38% · 61% · 2h10m`: the name left, a short bar and the numbers right. */
export function UsageCard(els: Els, usage: OverviewData['usage'], W: number, compact: boolean) {
  const { Box, Text } = els
  const numW = compact ? NUM_W.compact : NUM_W.wide
  const barW = Math.max(3, Math.min(12, W - NAME_W - numW - 3))
  const nameW = Math.max(1, W - barW - numW - 3)
  const role = (u: OverviewData['usage'][number]): PaletteRole => {
    if (u.exhausted || u.pct > 80) return 'err'
    if (u.pct >= 50) return 'warn'
    return 'ok'
  }
  return Card(els, {}, (
    <Box flexDirection="column">
      {HeadRow(els, 'Usage', <Text dimColor={true}>{compact ? '5h · reset' : '5h · week · reset'}</Text>)}
      {usage.length === 0 && <Text dimColor={true}>no usage data yet</Text>}
      {usage.map(u => {
        const nums = u.exhausted
          ? (compact ? `⊘ ${u.until ?? '—'}` : `⊘ exhausted · ${u.until ?? '—'}`)
          : (compact ? `${u.pct}% ${u.reset ?? '—'}` : `${u.pct}% · ${u.weekPct}% · ${u.reset ?? '—'}`)
        return (
          <Box key={u.label} flexDirection="row" gap={1}>
            <Text wrap="truncate-end">{clip(compact ? u.short : u.label, nameW)}</Text>
            <Box flexGrow={1} />
            {pickBar(
              els,
              `overview-usage-${u.short}`,
              gradientBar(u.exhausted ? 100 : u.pct, barW, u.exhausted ? [PALETTE.red] : USAGE_STOPS()),
              SmoothBar(els, { pct: u.exhausted ? 100 : u.pct, width: barW, role: role(u) }),
            )}
            <Text color={u.exhausted ? ROLE.err.fg : ROLE[role(u)].fg}>{clip(nums, numW).padStart(numW)}</Text>
          </Box>
        )
      })}
    </Box>
  ))
}

/** WAITING ON YOU — the warn Card: amber header and its time, the wrapped question and the
 *  a/b/c option buttons (folded into ⋯ More, the reveal in view state, C10). */
function WaitingCard(els: Els, waiting: OverviewData['waiting'], W: number, compact: boolean, ui?: TabUi) {
  const { Box, Text } = els
  const question = compact ? waiting.shortQuestion : waiting.question
  const options = compact ? waiting.shortOptions : waiting.options
  // C12: this Card's keys are namespaced — the Waiting tab owns its own
  // `waiting:pick` / `waiting:more`, whose values mean something else there
  const pick = ui?.get(PICK_KEY)
  const revealed = ui?.get(MORE_KEY) === '1'
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
      onPress: () => { ui?.set(PICK_KEY, glyph) },
    }
  })
  return Card(els, { role: 'warn' }, (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text bold={true} color={ROLE.warn.fg}>{compact ? 'WAITING' : 'WAITING ON YOU'}</Text>
        <Text dimColor={true}>{waiting.since}</Text>
      </Box>
      {wrap(question, W).map((line, i) => (
        <Text key={`q${String(i)}`}>{line}</Text>
      ))}
      {ActionButtons(els, specs, W, ui == null ? undefined : () => { ui.set(MORE_KEY, '1') })}
      {revealed && options.slice(1).map((opt, i) => (
        <Text key={`fold${String(i)}`} dimColor={true}>{clip(`${glyphs[i + 1] ?? '·'} ${opt}`, W)}</Text>
      ))}
    </Box>
  ))
}

/** RECENT — no card, as on the board: the header, then dim time + text rows. */
function RecentCard(els: Els, recent: OverviewData['recent'], W: number) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column" paddingX={1}>
      {HeadRow(els, 'Recent', null)}
      {recent.length === 0 && <Text dimColor={true}>nothing yet</Text>}
      {recent.map(ev => (
        <Box key={`${ev.t}${ev.text}`} flexDirection="row" gap={1}>
          <Text dimColor={true}>{ev.t}</Text>
          <Text wrap="truncate-end">{clip(ev.text, Math.max(1, W - ev.t.length - 1))}</Text>
        </Box>
      ))}
    </Box>
  )
}
