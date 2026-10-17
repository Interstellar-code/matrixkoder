// the /mk pane's Overview tab (plan dashboard-v2-p0b, V1.1, contract C9/C11):
// the Main.dc.html / Narrow.dc.html Overview boards redrawn from the V0 kit —
// five Cards (Plan, Lanes, Usage, Waiting, Recent), each budgeted for `cols - 4`
// (a Card eats a border and a padding cell per side). The fixture carries the
// board's copy (C6); the fixture names (OverviewData, OverviewView, overviewView,
// OverviewTab) stay the contract the pane and the tests build against.
import type { Elements, RenderChildren, RenderElement } from 'claude-code'
import { NARROW } from './ui'
import { ActionButtons, Card, Chip, SmoothBar } from './kit'
import type { ActionSpec } from './kit'
import { gradientBar, laneStrip, pickBar, sample } from './raster'
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
  lanes: { slot: string; agent: string; modelShort: string; effort: string; status: string; short: string }[]
  usage: { label: string; short: string; pct: number; weekPct: number | null; reset: string | null; exhausted: boolean; until: string | null }[]
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

/** The fixture view with the mock flag (the pane itself renders liveOverviewView). */
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
/** the usage row's name column is at most 8 cells; the numbers' column is as wide as the widest row */
const NAME_W = 8
const BAR_MAX = 14
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
export function OverviewTab(els: Els, view: OverviewData, cols: number, ui?: TabUi) {
  const { Box } = els
  const compact = cols < NARROW
  const W = Math.max(1, cols - 4) // the Card's content width (border + padding per side)
  return (
    <Box flexDirection="column" gap={1}>
      {PlanCard(els, view, W, compact)}
      {LanesCard(els, view.lanes, W)}
      {UsageCard(els, view.usage, W, compact)}
      {view.waiting.question !== '' && WaitingCard(els, view.waiting, W, compact, ui)}
      {RecentCard(els, view.recent, W)}
    </Box>
  )
}

/** a card's header row: the `> LABEL` on the left, any coloured parts on the right (the board's
 *  `PLAN 3/8 ……… W1 ✓✓✓ W2 ●◐!⊘ W3 ○`). */
function HeadRow(els: Els, label: string, right: RenderChildren): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="row" justifyContent="space-between" gap={1}>
      <Text bold={true} color={PALETTE.mut}>{`${THEME.name === 'matrix' ? '> ' : ''}${label.toUpperCase()}`}</Text>
      <Box flexDirection="row" gap={1}>{right}</Box>
    </Box>
  )
}

/** a chip's drawn width: its label plus one padding cell each side */
const chipW = (label: string): number => label.length + 2

/** the segment strip: one segment per role, gaps between (raster.ts laneStrip); the glyph
 *  fallback is one `▬` per role with a space between, sampled evenly down to `w`. */
function Strip(els: Els, key: string, roles: readonly PaletteRole[], w: number): RenderElement {
  const { Text } = els
  const fit = sample(roles, Math.min(roles.length, Math.max(1, Math.floor((w + 1) / 2))))
  return pickBar(
    els,
    key,
    laneStrip(roles, w),
    <Text>{fit.map((r, i) => <Text key={String(i)} color={ROLE[r].fg}>{i > 0 ? ' ▬' : '▬'}</Text>)}</Text>,
  )
}

/** PLAN 3/8 — wave chips on the right, a segment strip per task (done ones too: the live view
 *  filters them out of `tasks`, so `plan.done` stands in), one `P4 title …… ● build 14m` row per task. */
function PlanCard(els: Els, view: OverviewData, W: number, compact: boolean) {
  const { Box, Text } = els
  // the chips keep their order and the tail drops when the header has no room
  const room = W - `> PLAN ${view.plan.done}/${view.plan.total}`.length - 1
  const waves: RenderElement[] = []
  let used = 0
  for (const w of compact ? [] : view.waves) {
    const next = used + chipW(`${w.label} ${w.glyphs}`) + (waves.length > 0 ? 1 : 0)
    if (next > room) break
    used = next
    waves.push(Chip(els, { label: `${w.label} ${w.glyphs}`, role: WAVE_ROLE[w.tone] }))
  }
  const strip: PaletteRole[] = [...Array<PaletteRole>(view.plan.done).fill('ok'), ...view.tasks.map(t => roleOf(t.status))]
  return Card(els, {}, (
    <Box flexDirection="column">
      {HeadRow(els, `Plan ${view.plan.done}/${view.plan.total}`, waves)}
      {strip.length > 0 && Strip(els, 'overview-plan-strip', strip, W)}
      {view.tasks.length === 0 && <Text dimColor={true}>{view.plan.total === 0 ? 'no tasks yet' : 'all done'}</Text>}
      {view.tasks.map(task => {
        const planned = task.status === 'planned'
        const label = `${glyphOf(task.status)} ${compact ? task.short : task.note}`
        // id · title · filler · chip, with a gap column between each pair; a planned row's tag is plain text
        const budget = Math.max(1, W - task.id.length - (planned ? label.length : chipW(label)) - 3)
        return (
          <Box key={task.id} flexDirection="row" gap={1}>
            <Text bold={!planned} dimColor={planned}>{task.id}</Text>
            <Text dimColor={planned} wrap="truncate-end">{clip(task.title, budget)}</Text>
            <Box flexGrow={1} />
            {planned ? <Text dimColor={true}>{label}</Text> : Chip(els, { label, role: roleOf(task.status) })}
          </Box>
        )
      })}
    </Box>
  ))
}

/** LANES 4 — a square per lane by status on the right; `p4 codex sol high …… ● build`. */
function LanesCard(els: Els, lanes: OverviewData['lanes'], W: number) {
  const { Box, Text } = els
  const strip = lanes.length === 0 ? null : Strip(els, 'overview-lanes-strip', lanes.map(l => roleOf(l.status)), Math.min(14, lanes.length * 3 - 1))
  return Card(els, {}, (
    <Box flexDirection="column">
      {HeadRow(els, `Lanes ${lanes.length}`, strip)}
      {lanes.length === 0 && <Text dimColor={true}>none live</Text>}
      {lanes.map(lane => {
        const label = `${glyphOf(lane.status)} ${lane.short}`
        const effort = lane.effort === 'medium' ? 'med' : lane.effort
        const agent = lane.agent.toLowerCase()
        // slot · agent · model effort · filler · chip: four gap columns to give back
        const budget = Math.max(1, W - lane.slot.length - agent.length - chipW(label) - 4)
        // one cut over `model effort`, then the model part plain and the rest dim
        const cut = clip(`${lane.modelShort} ${effort}`, budget)
        return (
          <Box key={lane.slot} flexDirection="row" gap={1}>
            <Text>{lane.slot}</Text>
            <Text color={PALETTE.mut}>{agent}</Text>
            <Text wrap="truncate-end">
              <Text>{cut.slice(0, lane.modelShort.length)}</Text>
              {cut.length > lane.modelShort.length && <Text dimColor={true}>{cut.slice(lane.modelShort.length)}</Text>}
            </Text>
            <Box flexGrow={1} />
            {Chip(els, { label, role: roleOf(lane.status) })}
          </Box>
        )
      })}
    </Box>
  ))
}

/** USAGE — `claude …… [bar] 38% 61% 2h10m`: the short name left, the bar and the numbers right —
 *  5h % in its heat colour, week % plain, reset dim; an exhausted row reads `⊘ 58% 19:05`. */
export function UsageCard(els: Els, usage: OverviewData['usage'], W: number, compact: boolean) {
  const { Box, Text } = els
  const role = (u: OverviewData['usage'][number]): PaletteRole => {
    if (u.exhausted || u.pct > 80) return 'err'
    if (u.pct >= 50) return 'warn'
    return 'ok'
  }
  // [5h part, week part, reset part] per row; compact drops the week, unknown week data reads `—`
  const partsOf = (u: OverviewData['usage'][number]): { t: string; c?: string; dim?: true }[] => {
    const week = compact ? [] : [{ t: u.weekPct == null ? '—' : `${u.weekPct}%` }]
    return u.exhausted
      ? [{ t: '⊘', c: ROLE.err.fg }, ...week, { t: u.until ?? '—', c: ROLE.warn.fg }]
      : [{ t: `${u.pct}%`, c: ROLE[role(u)].fg }, ...week, { t: u.reset ?? '—', dim: true }]
  }
  const widthOfParts = (ps: { t: string }[]): number => ps.reduce((n, p) => n + p.t.length, 0) + ps.length - 1
  const numW = Math.max(1, ...usage.map(u => widthOfParts(partsOf(u))))
  const nameCol = Math.min(NAME_W, Math.max(1, ...usage.map(u => u.short.length)))
  const barW = Math.max(3, Math.min(BAR_MAX, W - nameCol - numW - 3))
  const nameW = Math.max(1, W - barW - numW - 3)
  return Card(els, {}, (
    <Box flexDirection="column">
      {HeadRow(els, 'Usage', <Text dimColor={true}>{compact ? '5h · reset' : '5h · wk · reset'}</Text>)}
      {usage.length === 0 && <Text dimColor={true}>no usage data yet</Text>}
      {usage.map(u => {
        const parts = partsOf(u)
        const pad = Math.max(0, numW - widthOfParts(parts))
        return (
          <Box key={u.label} flexDirection="row" gap={1}>
            <Text wrap="truncate-end">{clip(u.short, nameW)}</Text>
            <Box flexGrow={1} />
            {pickBar(
              els,
              `overview-usage-${u.short}`,
              gradientBar(u.exhausted ? 100 : u.pct, barW, u.exhausted ? [PALETTE.red] : USAGE_STOPS()),
              SmoothBar(els, { pct: u.exhausted ? 100 : u.pct, width: barW, role: role(u) }),
            )}
            <Text>
              {' '.repeat(pad)}
              {parts.map((p, i) => (
                <Text key={String(i)} color={p.c} dimColor={p.dim === true}>{`${i > 0 ? ' ' : ''}${p.t}`}</Text>
              ))}
            </Text>
          </Box>
        )
      })}
    </Box>
  ))
}

/** ! WAITING ON YOU — the warn Card: a solid amber chip and the dim time, the wrapped question and the
 *  a/b/c option buttons (the recommended one ★) (folded into ⋯ More, the reveal in view state, C10). */
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
      word: i === waiting.rec ? `${opt} ★` : opt,
      tone: chosen ? 'primary' : 'default',
      primary: chosen,
      onPress: () => { ui?.set(PICK_KEY, glyph) },
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
      {ActionButtons(els, specs, W, ui == null ? undefined : () => { ui.set(MORE_KEY, '1') })}
      {revealed && options.slice(1).map((opt, i) => (
        <Text key={`fold${String(i)}`} dimColor={true}>{clip(`${glyphs[i + 1] ?? '·'} ${opt}${i + 1 === waiting.rec ? ' ★' : ''}`, W)}</Text>
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
