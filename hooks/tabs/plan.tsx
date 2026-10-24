// The /mk pane's Plan tab Detail (plan settings-plans-db, V1.2, contract C11):
// one Card per PHASE, folded by default. The word "wave" is not drawn; a phase
// that shares a wave with another shows a small `with <id>` chip. Fold state
// lives in the C12 view-state handle (`plan:phase:<id>`); absent = folded.
// The tab stays pure — no $, no state, no scripts.
import type { Elements, RenderElement } from 'claude-code'
import { PLAN_FIXTURE } from './plan.fixture'
import { NARROW, STATUS_GLYPH, fitLegend } from './ui'
import { ActionButton, BoxButton, Card, Chip, ICONS, IconButton, FilterChip, Segment, TreeRow } from './kit'
import type { ActionSpec } from './kit'
import { colorStrip, laneStrip, pickBar } from './raster'
import { PALETTE, ROLE } from './palette'
import type { PaletteRole } from './palette'
import { isDoneStatus, laneAgent, routeText } from './plans-map'
import { canPause, canPlay, depsMet, taskDepsMet } from './planrun'

type Els = Elements[keyof Elements]

/** C12: this tab's view-state handle — declared locally (structural typing; no
 *  import crosses lanes). */
type TabUi = { get(key: string): string | undefined; set(key: string, value: string): void }

/** The dispatch route of a task, from the store's route_json (V1.5). A member
 *  that is missing is simply absent — never a `null` string. */
export type PlanRoute = {
  readonly agent?: string
  readonly model?: string
  readonly effort?: string
}

/** One subtask under a task row: its own status and a right-side chip. */
export type PlanSub = {
  readonly id: string
  readonly title: string
  readonly status: string
  readonly right: string
  readonly rightRole?: PaletteRole
  /** optional plain-English description (2-5 sentences), trimmed; absent when empty */
  readonly description?: string
  /** V1.5: the routing fields the plans reply carries, when it has them */
  readonly slot?: number | null
  readonly pane?: string | null
  readonly branch?: string | null
  readonly updated?: string | null
  readonly acceptance?: string | null
  readonly owned?: readonly string[]
  readonly deps?: readonly string[]
  readonly route?: PlanRoute | null
  readonly type?: string
  readonly round?: number
  readonly verdict?: string
  /** F.1 contract: the derived risk and the open reasons. */
  readonly risk?: number
  readonly openReasons?: readonly { readonly kind: string; readonly points: number; readonly source: string; readonly opened: string }[]
}

/** One task row inside a phase. `short` is the Narrow form.
 *  `rows` (the old wave shape) lets the Checkout fixture keep folding its
 *  subtasks; the dashboard still mounts that fixture until V1.3. */
export type PlanTask = {
  readonly id: string
  readonly title: string
  readonly status: string
  readonly right: string
  readonly short?: { readonly title: string; readonly right: string }
  readonly subs?: readonly PlanSub[]
  /** optional plain-English description (2-5 sentences), trimmed; absent when empty */
  readonly description?: string
  /** legacy: a wave row that itself folds (the Checkout fixture) */
  readonly rows?: readonly PlanTask[]
  /** V1.5: the routing fields the plans reply carries, when it has them */
  readonly slot?: number | null
  readonly pane?: string | null
  readonly branch?: string | null
  readonly updated?: string | null
  readonly acceptance?: string | null
  readonly owned?: readonly string[]
  readonly deps?: readonly string[]
  readonly route?: PlanRoute | null
  /** a draft plan's own builder route and reviewer, from the plan file (not a run's route) */
  readonly plannedRoute?: PlanRoute | null
  readonly reviewer?: string
  readonly type?: string
  readonly round?: number
  readonly verdict?: string
  /** Detail row right side, when the backend sends them: the task's last merge
   *  commit (drawn as 7 chars on a done task) and its open review findings. */
  readonly last_commit?: string
  readonly findings?: number
  /** F.1 contract: the derived risk and the open reasons. All optional — an old row carries
   *  none of them. */
  readonly risk?: number
  readonly openReasons?: readonly { readonly kind: string; readonly points: number; readonly source: string; readonly opened: string }[]
}

/** One phase Card. `wave` is data only — never drawn as the word "wave"; when
 *  another phase carries the same number the row shows `with <id>`.
 *  V1.5: a phase can itself be a routed task row (the store roots a round's
 *  lane tasks at phase level — D1.5, V1.4, V1.5 today): it then carries the
 *  routing fields, draws its dim lane·agent tag, and its fold press also
 *  selects it (`plan:task`). */
export type PlanPhase = {
  readonly id: string
  readonly title: string
  /** a wave card has no status of its own */
  readonly status?: string
  readonly header: string
  readonly headerRole?: PaletteRole
  /** legacy wave tone, used only when headerRole is absent */
  readonly tone?: 'green' | 'blue' | 'gray'
  readonly shortHeader?: string
  readonly name?: string
  readonly wave?: number
  readonly rows: readonly PlanTask[]
  readonly note?: string
  /** optional plain-English description (2-5 sentences), trimmed; absent when empty */
  readonly description?: string
  /** V1.5: routing fields a phase-level task row carries, when it has them */
  readonly slot?: number | null
  readonly pane?: string | null
  readonly branch?: string | null
  readonly updated?: string | null
  readonly acceptance?: string | null
  readonly owned?: readonly string[]
  readonly deps?: readonly string[]
  readonly route?: PlanRoute | null
  /** a draft plan's own builder route and reviewer, from the plan file (not a run's route) */
  readonly plannedRoute?: PlanRoute | null
  readonly reviewer?: string
}

/** The tab's whole view: the header card plus the phases. */
export type PlanView = {
  readonly mock: boolean
  readonly plan?: {
    readonly id: string
    readonly title: string
    readonly status: string
    readonly statusChip: string
    readonly created: string
    readonly runs: string
    readonly mergesTo: string
    readonly counts: string
    /** L7.2: where this plan's round files (briefs/reports/reviews) are — a DONE
     *  plan's were archived (the legacy tarball), `archived · legacy tarball
     *  <path>`; anything else reads `missing` (they are not in the project
     *  folder the Detail can name). '' leaves the row out (a bare C8 row). */
    readonly roundFiles?: string
  }
  readonly phases?: readonly PlanPhase[]
  /** legacy Checkout fixture: wave cards. Mapped onto phases at render. */
  readonly waves?: readonly { readonly name: string; readonly header: string; readonly tone: 'green' | 'blue' | 'gray'; readonly shortHeader?: string; readonly rows: readonly PlanTask[] }[]
  /** ' · '-joined footer parts; the tail drops until the line fits (fitLegend). */
  readonly footer: readonly string[]
  /** header-card buttons (`pause`, `open-file`) call this with the plan id; absent, they do nothing */
  readonly onAction?: (kind: string, planId: string) => void
  /** a task's detail panel drawn in place of the phases box (the pop-up); header and footer stay */
  readonly taskPanel?: RenderElement
}

/** T1.2: the fixture, mock flag on (C1/C6). */
export function planView(): PlanView {
  return { ...PLAN_FIXTURE, mock: true }
}

const STATUS_ROLE: Record<string, PaletteRole> = {
  dispatched: 'live', building: 'live', verifying: 'live', review: 'live', fix: 'warn', qa: 'live',
  merged: 'ok', done: 'ok',
  blocked: 'warn', quota: 'warn', stalled: 'warn', provider: 'warn', paused: 'warn',
  planned: 'idle', routed: 'idle',
}
const SETTLED = new Set(['merged', 'done', 'routed', 'planned'])

const roleOf = (status: string): PaletteRole => STATUS_ROLE[status] ?? 'idle'
const glyphOf = (status: string): string => STATUS_GLYPH[status] ?? '○'

/** V1.5: shared with the task detail panel (plantask.tsx). */
export const planStatusRole = roleOf
export const planStatusGlyph = glyphOf

export function clip(text: string, w: number): string {
  if (text.length <= w) return text
  return `${text.slice(0, Math.max(1, w) - 1)}…`
}

// shortcut: counts string length, not cell width, so wide CJK or emoji text overflows; upgrade if non-English text is needed
/** `text` wrapped on spaces to `w` cols and cut to `max` lines; a cut ends its last line with `…`. */
function wrapCap(text: string, w: number, max: number): string[] {
  const width = Math.max(1, w)
  const lines: string[] = []
  let cur = ''
  for (let word of text.split(/\s+/).filter(x => x !== '')) {
    while (word.length > width) { // a word wider than the line is broken
      if (cur !== '') lines.push(cur)
      cur = ''
      lines.push(word.slice(0, width))
      word = word.slice(width)
    }
    if (cur === '') cur = word
    else if (cur.length + 1 + word.length <= width) cur += ` ${word}`
    else { lines.push(cur); cur = word }
  }
  if (cur !== '') lines.push(cur)
  if (lines.length <= max) return lines
  return [...lines.slice(0, max - 1), clip(`${lines[max - 1] ?? ''}…`, width)]
}

/** A description as plain muted lines (no label), at most `max` of them. */
function Describe(els: Els, key: string, text: string, w: number, max: number): RenderElement {
  const { Box, Text } = els
  return (
    <Box key={key} flexDirection="column">
      {wrapCap(text, w, max).map((line, i) => <Text key={`${key}-${i}`} color={PALETTE.mut}>{line}</Text>)}
    </Box>
  )
}

const phaseKey = (id: string): string => `plan:phase:${id}`

/** The title only: store titles carry the description after the first `: `, `; ` or ` — `. */
function headline(text: string): string {
  const m = /: |; | — /.exec(text)
  return m == null ? text : text.slice(0, m.index)
}

/** The five states every status is shown as, one hue and one shape each, so the phase bar, the
 *  PHASES filter chip, the task dot and the right-hand chip cannot drift apart. Plan-tab colours:
 *  the shared ROLE palette stays as it is for the other tabs. */
export type PlanState = 'done' | 'building' | 'review' | 'pending' | 'attention'
export const PLAN_STATE: Record<PlanState, { readonly glyph: string; readonly color: string; readonly tint: string }> = {
  done: { glyph: '◆', color: '#34d063', tint: '#0f3a1c' }, // green
  building: { glyph: '●', color: '#4da3ff', tint: '#10294a' }, // blue
  review: { glyph: '◐', color: '#b57bff', tint: '#2a1a4a' }, // purple
  pending: { glyph: '○', color: '#ffd84a', tint: '#3a3210' }, // yellow
  attention: { glyph: '▲', color: '#ff5555', tint: '#4a1414' }, // red
}
const STATE_OF: Record<string, PlanState> = {
  merged: 'done', qa: 'done', done: 'done',
  dispatched: 'building', building: 'building', verifying: 'building',
  review: 'review',
  fix: 'attention', rework: 'attention', blocked: 'attention', quota: 'attention', stalled: 'attention', provider: 'attention', paused: 'attention',
}
/** A task or phase status as one of the five states; planned, routed and anything unknown are pending. */
export const statusState = (status: string): PlanState => STATE_OF[status] ?? 'pending'

/** A phase's state for the bar colour and the PHASES filter: done when every task is done, else
 *  the most urgent task (attention, then building, then review), else pending. A phase with no
 *  task rows takes its own status. */
function phaseState(phase: PlanPhase): PlanState {
  if (isDoneStatus(phase.status ?? '') || (phase.rows.length > 0 && phase.rows.every(r => isDoneStatus(r.status)))) return 'done'
  const states = phase.rows.length > 0 ? phase.rows.map(r => statusState(r.status)) : [statusState(phase.status ?? 'planned')]
  for (const s of ['attention', 'building', 'review'] as const) if (states.includes(s)) return s
  return states.includes('done') ? 'done' : 'pending'
}

// the building chip also counts phases in review; attention has its own chip, drawn only when it has any
const PHASE_FILTERS = ['all', 'building', 'attention', 'pending', 'done'] as const
type PhaseFilter = typeof PHASE_FILTERS[number]
const inFilter = (f: PhaseFilter, state: PlanState): boolean =>
  f === 'all' || (f === 'building' ? state === 'building' || state === 'review' : state === f)

/** The right-hand state chip of a phase: the state's tint and colour. */
function StateChip(els: Els, label: string, state: PlanState): RenderElement {
  const { Text } = els
  const c = PLAN_STATE[state]
  return <Text backgroundColor={c.tint} color={c.color}>{` ${label} `}</Text>
}

const PAUSED = new Set(['paused', 'parked', 'held', 'on_hold'])

/** `‖` pauses a running phase/task, `▶` resumes a paused one; the glyphs come from the
 *  kit's ICONS so Play and Pause cannot drift. `canPause` decides a running row;
 *  a phase whose tasks are building is running even when its own status says planned. */
function pauseButton(els: Els, key: string, status: string, live: boolean, running: boolean, act: ((kind: string) => void) | undefined, what: string, id: string): RenderElement | null {
  const paused = PAUSED.has(status)
  // only a running plan draws it: a task follows canPause, a phase its building state
  if (!running || (!paused && !(what === 'task' ? canPause(status, true) : live))) return null
  return BoxButton(els, {
    key, label: paused ? ICONS.play.glyph : ICONS.pause.glyph, tone: paused ? 'primary' : 'default',
    onPress: () => { act?.(`${paused ? 'resume' : 'pause'} ${what} ${id}`) },
  })
}

/** The Play icon of a phase or task: drawn when the plan is running and the row can start
 *  (its own deps and, for a task, its phase's deps are all merged or done). The press
 *  drafts its prompt on the Improve card. */
function playButton(els: Els, key: string, status: string, depsMet: boolean, running: boolean, act: ((kind: string) => void) | undefined, what: string, id: string): RenderElement | null {
  // a held row already carries the Resume ▶ (pauseButton), so Play is not drawn twice
  if (!running || act == null || PAUSED.has(status) || !canPlay(status, depsMet)) return null
  return IconButton(els, { key, icon: 'play', onPress: () => { act(`play ${what} ${id}`) } })
}

/** The Improve icon of a phase or task row on a DRAFT plan: a draft has no builder to start
 *  (Play appears once the plan runs), so this is the row's one action — it drafts a prompt for
 *  the row's own fields on the Improve card, and accepting it asks for `mk-plan.py item set`. */
function improveButton(els: Els, key: string, draft: boolean, act: ((kind: string) => void) | undefined, what: string, id: string): RenderElement | null {
  if (!draft || act == null) return null
  return ActionButton(els, { key, glyph: '✦', word: '', plain: true, tone: 'quiet', noHotkey: true, onPress: () => { act(`improve ${what} ${id}`) } })
}

/** Pure render: elements only, no $, no state (C1); `ui` is the C12 view state. */
export function PlanTab(els: Els, view: PlanView, cols: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const compact = cols < NARROW
  const W = Math.max(1, cols - 4)
  const phases: readonly PlanPhase[] = view.phases ?? (view.waves ?? []).map(wave => ({ id: wave.name, name: wave.name, title: wave.name, header: wave.header, tone: wave.tone, shortHeader: wave.shortHeader, rows: wave.rows }))
  const shared = new Map<number, string[]>()
  for (const phase of phases) {
    if (phase.wave == null) continue
    const list = shared.get(phase.wave) ?? []
    list.push(phase.id)
    shared.set(phase.wave, list)
  }
  const act = (kind: string): void => { view.onAction?.(kind, view.plan?.id ?? '') }
  // Play and Pause exist only while the plan runs (its tasks exist only then); Improve takes
  // their place while the plan is still a draft
  const running = view.plan?.status === 'running'
  const draft = view.plan?.status === 'draft'
  const phaseFilter = ui?.get('plan:phase-filter') ?? 'all'
  const attentionCount = phases.filter(p => phaseState(p) === 'attention').length
  // a picked attention chip with nothing left to show falls back to all
  const activeFilter: PhaseFilter = phaseFilter === 'attention' && attentionCount === 0 ? 'all' : (PHASE_FILTERS as readonly string[]).includes(phaseFilter) ? phaseFilter as PhaseFilter : 'all'
  const shownPhases = activeFilter === 'all' ? phases : phases.filter(p => inFilter(activeFilter, phaseState(p)))
  return (
    <Box flexDirection="column" gap={1}>
      {view.mock && <Text bold={true} color={ROLE.warn.fg}>MOCK · fixture data · not this run</Text>}
      {view.plan != null && HeaderCard(els, view, W, compact)}
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Box flexDirection="row" gap={1}>
          <Text bold={true} color={PALETTE.mut}>PHASES</Text>
          {!compact && PHASE_FILTERS.filter(f => f !== 'attention' || attentionCount > 0).map(f => f === 'all'
            ? Segment(els, {
              key: 'plan-phase-filter-all',
              label: `all ${phases.length}`,
              on: activeFilter === 'all',
              onPress: () => { ui?.set('plan:phase-filter', 'all') },
            })
            : FilterChip(els, {
              key: `plan-phase-filter-${f}`,
              label: `${f} ${phases.filter(p => inFilter(f, phaseState(p))).length}`,
              glyph: PLAN_STATE[f].glyph,
              color: PLAN_STATE[f].color,
              tint: PLAN_STATE[f].tint,
              on: activeFilter === f,
              onPress: () => { ui?.set('plan:phase-filter', f) },
            }))}
        </Box>
        <Box flexDirection="row" gap={1}>
          {BoxButton(els, {
            key: 'plan-expand-all', label: '▾ Expand all',
            onPress: () => { for (const p of phases) ui?.set(phaseKey(p.id), '1') },
          })}
          {BoxButton(els, {
            key: 'plan-collapse-all', label: '▸ Collapse all',
            onPress: () => { for (const p of phases) ui?.set(phaseKey(p.id), '') },
          })}
        </Box>
      </Box>
      <Box key="plan-phases" flexDirection="column" position="relative">
        {shownPhases.length === 0 && <Text dimColor={true}>{`no ${activeFilter} phases`}</Text>}
        {shownPhases.length > 0 && Card(els, { edge: PALETTE.mut }, (
          <Box flexDirection="column">
            {shownPhases.map((phase, i) => (
              <Box key={`plan-phase-wrap-${phase.id}`} flexDirection="column">
                {i > 0 && <Text color={PALETTE.line}>{'─'.repeat(Math.max(1, W - 4))}</Text>}
                {PhaseCard(els, phase, shared.get(phase.wave ?? -1) ?? [], W - 4, compact, ui, act, running, phases, draft)}
              </Box>
            ))}
          </Box>
        ))}
        {/* the task pop-up floats over the phases, inset so the rows around it stay visible and pressable */}
        {view.taskPanel != null && (
          <Box key="plan-task-popup" position="absolute" top={2} left={compact ? 1 : 4} right={compact ? 1 : 4} flexDirection="column">
            {view.taskPanel}
          </Box>
        )}
      </Box>
      {!compact && (
        <Box flexDirection="row" justifyContent="space-between" gap={1}>
          <Text dimColor={true} wrap="truncate-end">{clip(view.footer[0] ?? '', Math.max(1, cols - 20))}</Text>
          <Text dimColor={true} wrap="truncate-end">{clip(fitLegend(view.footer.slice(1), 18), 18)}</Text>
        </Box>
      )}
    </Box>
  )
}

function HeaderCard(els: Els, view: PlanView, W: number, compact: boolean): RenderElement {
  const { Box, Text } = els
  const head = view.plan
  if (head == null) return Card(els, {}, <Text></Text>)
  const KEY_W = 11 // the design's two-column kv: keys padded to one width
  const kv = (k: string, v: string): RenderElement | null => v === '' ? null : (
    <Box flexDirection="row">
      <Text dimColor={true}>{k.padEnd(KEY_W)}</Text>
      <Text wrap="truncate-end">{clip(v, Math.max(1, W - KEY_W))}</Text>
    </Box>
  )
  const running = head.status === 'running'
  const chipRole: PaletteRole = running ? 'live' : isDoneStatus(head.status) ? 'ok' : head.status === 'parked' ? 'warn' : 'idle'
  const fire = (kind: string): void => { view.onAction?.(kind, head.id) }
  const buttons: ActionSpec[] = [
    ...(head.status === 'running' ? [{ key: 'plan-head-pause', glyph: '‖ Pause', word: '', tone: 'quiet', plain: true, onPress: () => { fire('pause') } } satisfies ActionSpec] : []),
    ...(PAUSED.has(head.status) ? [{ key: 'plan-head-resume', glyph: '▶ Resume', word: '', tone: 'primary', plain: true, onPress: () => { fire('resume') } } satisfies ActionSpec] : []),
    { key: 'plan-head-open-file', glyph: 'Open file', word: '', tone: 'default', plain: true, onPress: () => { fire('open-file') } },
  ]
  return Card(els, running ? { role: 'live' } : { edge: PALETTE.mut }, (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text bold={true}>{clip(head.id, Math.max(1, W - head.statusChip.length - 6))}</Text>
        {Chip(els, { label: compact ? clip(head.statusChip, 12) : head.statusChip, role: chipRole })}
      </Box>
      <Text color={ROLE.idle.fg} wrap="truncate-end">{clip(head.title, W)}</Text>
      {kv('created', head.created)}
      {kv('runs', head.runs)}
      {kv('merges to', head.mergesTo)}
      {head.roundFiles != null && head.roundFiles !== '' && kv('round files', head.roundFiles)}
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text dimColor={true} wrap="truncate-end">{clip(head.counts, Math.max(1, W - 24))}</Text>
        <Box flexDirection="row" gap={1}>{buttons.map(b => BoxButton(els, { key: b.key, label: b.glyph, tone: b.tone, onPress: () => { b.onPress?.() } }))}</Box>
      </Box>
    </Box>
  ))
}

/** V1.5: a phase node the store itself routed (slot/pane/branch or a route on
 *  the root row — a round's lane task, D1.5/V1.4/V1.5 today). Such a row
 *  draws its lane·agent tag and its fold press also selects it. */
export function phaseRouted(phase: PlanPhase): boolean {
  return laneAgent(phase) !== '' || phase.pane != null || phase.branch != null
}

/** One phase Card. Folded (the default) is one row: fold glyph, status glyph,
 *  `<id> · <title>`, the header chip and the laneStrip. A routed phase adds
 *  its dim lane·agent tag right-aligned, and the fold press selects it too. */
function PhaseCard(els: Els, phase: PlanPhase, waveMates: readonly string[], W: number, compact: boolean, ui?: TabUi, act?: (kind: string) => void, running = false, phases: readonly PlanPhase[] = [], draft = false): RenderElement {
  const { Box, Text } = els
  const legacy = phase.tone != null
  const expanded = legacy ? true : ui?.get(phaseKey(phase.id)) === '1'
  const routed = phaseRouted(phase)
  const lane = isDoneStatus(phase.status ?? '') ? '' : laneAgent(phase)
  const toneRole: PaletteRole = phase.tone === 'green' ? 'ok' : phase.tone === 'blue' ? 'live' : 'idle'
  const headRole = phase.headerRole ?? toneRole
  const live = (phase.tone === 'blue') || phase.rows.some(r => roleOf(r.status) === 'live' || roleOf(r.status) === 'warn')
  const findings = phase.rows.reduce((n, r) => n + (r.findings ?? 0), 0)
  const chipLabel = findings > 0 ? `${findings} findings` : compact && phase.shortHeader != null ? phase.shortHeader : phase.header
  const chipRole: PaletteRole = findings > 0 ? 'warn' : headRole
  const others = waveMates.filter(id => id !== phase.id)
  const strip = pickBar(
    els,
    `plan-strip-${phase.id}`,
    legacy ? laneStrip(phase.rows.map(r => roleOf(r.status)), W) : colorStrip(phase.rows.map(r => PLAN_STATE[statusState(r.status)].color), W),
    <Text dimColor={true}>{phase.rows.map(r => legacy ? glyphOf(r.status) : PLAN_STATE[statusState(r.status)].glyph).join('')}</Text>,
  )
  // state as a one-column bar down the left edge: building blue, pending yellow, done the plain line
  // (phases share one Card, so one divider row between them, not two borders)
  const state: PlanState = legacy ? (live ? 'building' : 'done') : phaseState(phase)
  const bar = PLAN_STATE[state].color
  return (
    <Box key={phase.id} flexDirection="row" gap={1}>
    <Box width={1} backgroundColor={bar} />
    <Box flexDirection="column" flexGrow={1}>
      <Box flexDirection="row" gap={1}>
        {!legacy && <Text dimColor={!live} color={live ? ROLE.live.fg : undefined}>{expanded ? '▾' : '▸'}</Text>}
        {!legacy && <Text color={PLAN_STATE[state].color}>{PLAN_STATE[state].glyph}</Text>}
        {!legacy && ActionButton(els, {
          key: `plan-fold-${phase.id}`,
          glyph: '›',
          word: clip(`${phase.id} · ${headline(phase.title)}`, Math.max(1, W - chipLabel.length - 12 - (!compact && lane !== '' ? lane.length + 1 : 0))),
          plain: true,
          noHotkey: true,
          onPress: () => {
            ui?.set(phaseKey(phase.id), expanded ? '' : '1')
            if (routed) ui?.set('plan:task', ui?.get('plan:task') === phase.id ? '' : phase.id)
          },
        })}
        {legacy && <Text bold={true} color={PALETTE.mut}>{phase.name ?? phase.id}</Text>}
        <Box flexGrow={1} />
        {!compact && routed && lane !== '' && <Text dimColor={true} wrap="truncate-end">{lane}</Text>}
        {others.length > 0 && Chip(els, { label: `with ${others[0]}`, role: 'idle' })}
        {legacy || findings > 0 ? Chip(els, { label: chipLabel, role: chipRole }) : StateChip(els, chipLabel, state)}
        {!legacy && pauseButton(els, `plan-phase-pause-${phase.id}`, phase.status ?? '', state === 'building', running, act, 'phase', phase.id)}
        {expanded && !legacy && playButton(els, `plan-phase-play-${phase.id}`, phase.status ?? '', depsMet(phase.deps, phases), running, act, 'phase', phase.id)}
        {improveButton(els, `plan-phase-improve-${phase.id}`, draft, act, 'phase', phase.id)}
      </Box>
      {expanded && phase.description != null && Describe(els, `plan-desc-${phase.id}`, phase.description, W - 2, 3)}
      {expanded && !routed && (phase.plannedRoute != null || phase.reviewer != null) && (
        <Text key={`plan-who-${phase.id}`} color={PALETTE.mut} wrap="truncate-end">{clip([phase.plannedRoute != null ? `builder ${routeText(phase.plannedRoute)}` : '', phase.reviewer != null ? `reviewer ${phase.reviewer}` : ''].filter(p => p !== '').join(' · '), W - 2)}</Text>
      )}
      {strip}
      {expanded && !legacy && phase.rows.map((row, i) => TaskBlock(els, row, i === phase.rows.length - 1, W - 4, compact, ui, act, running, phases, draft))}
      {legacy && phase.rows.map(row => LegacyRow(els, row, W, compact, ui))}
      {expanded && phase.note != null && <Text dimColor={true}>{clip(phase.note, W)}</Text>}
    </Box>
    </Box>
  )
}

/** One task: the V1.5 row — status glyph, task id, the title cut to the space
 *  left, and the dim lane·agent tag right-aligned. The row is pressable and
 *  selects the task (`plan:task`); pressing the selected task again clears it.
 *  When the width cannot fit id + ≥10 title cols + the tag, the tag drops
 *  first — the title never shrinks below 10 to keep it. */
function TaskBlock(els: Els, row: PlanTask, last: boolean, W: number, compact: boolean, ui?: TabUi, act?: (kind: string) => void, running = false, phases: readonly PlanPhase[] = [], draft = false): RenderElement {
  const { Box, Text } = els
  const role = roleOf(row.status)
  const title = headline(compact ? row.short?.title ?? row.title : row.title)
  const lane = isDoneStatus(row.status) ? '' : laneAgent(row)
  const find = row.findings ?? 0
  const sha = isDoneStatus(row.status) && row.last_commit != null ? row.last_commit.slice(0, 7) : ''
  const rnd = row.round != null ? `r${row.round}` : ''
  const rightLen = find > 0 ? `${find} findings`.length + 2 : sha !== '' ? sha.length : rnd.length
  const chrome = 6 + (rightLen > 0 ? rightLen + 1 : 0) // '├ ' + gap + glyph + gap + the gap before the tag + the right text
  const showLane = lane !== '' && chrome + row.id.length + 1 + 10 + lane.length <= W
  const titleBudget = Math.max(1, showLane
    ? W - chrome - row.id.length - 1 - lane.length
    : W - chrome - row.id.length - 1)
  const subs = row.subs ?? []
  return (
    <Box key={row.id} flexDirection="column">
      <Box flexDirection="row" gap={1}>
        <Text dimColor={true}>{last ? '└ ' : '├ '}</Text>
        <Text color={PLAN_STATE[statusState(row.status)].color}>{PLAN_STATE[statusState(row.status)].glyph}</Text>
        {ActionButton(els, {
          key: `plan-task-${row.id}`,
          glyph: clip(`${row.id} ${title}`, titleBudget),
          word: '',
          plain: true,
          onPress: () => { if (ui != null) ui.set('plan:task', ui.get('plan:task') === row.id ? '' : row.id) },
        })}
        <Box flexGrow={1} />
        {showLane && <Text dimColor={true} wrap="truncate-end">{lane}</Text>}
        {find > 0 ? Chip(els, { label: `${find} findings`, role: 'warn' }) : sha !== '' ? <Text dimColor={true}>{sha}</Text> : rnd !== '' ? <Text dimColor={true}>{rnd}</Text> : null}
        {pauseButton(els, `plan-task-pause-${row.id}`, row.status, role === 'live', running, act, 'task', row.id)}
        {playButton(els, `plan-task-play-${row.id}`, row.status, taskDepsMet(row, phases), running, act, 'task', row.id)}
        {improveButton(els, `plan-task-improve-${row.id}`, draft, act, 'task', row.id)}
      </Box>
      {subs.map((sub, i) => TreeRow(els, {
        depth: 1,
        last: i === subs.length - 1,
        glyph: PLAN_STATE[statusState(sub.status)].glyph,
        glyphColor: PLAN_STATE[statusState(sub.status)].color,
        role: roleOf(sub.status),
        title: `${sub.id} ${headline(sub.title)}`,
        right: sub.right,
        rightRole: sub.rightRole ?? roleOf(sub.status),
      }, W))}
    </Box>
  )
}

/** The Checkout fixture's wave row: fold glyph + pressable title, subtasks as
 *  TreeRows. Fold key `plan:<id>` is what the mounted pane tests press. */
function LegacyRow(els: Els, row: PlanTask, W: number, compact: boolean, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const open = ui?.get(`plan:${row.id}`) === '1'
  const role = roleOf(row.status)
  const settled = SETTLED.has(row.status)
  const title = compact ? row.short?.title ?? row.title : row.title
  const rightText = compact ? row.short?.right ?? row.right : row.right
  const budget = Math.max(1, W - 1 - 2 - rightText.length - 3)
  const subs = row.subs ?? []
  return (
    <Box key={row.id} flexDirection="column">
      <Box flexDirection="row" gap={1}>
        <Text dimColor={true}>{open ? '▾' : '▸'}</Text>
        {ActionButton(els, {
          key: `plan-fold-${row.id}`,
          glyph: glyphOf(row.status),
          word: clip(title, budget),
          plain: true,
          tone: settled ? 'quiet' : 'default',
          onPress: () => { ui?.set(`plan:${row.id}`, open ? '' : '1') },
        })}
        <Box flexGrow={1} />
        <Text color={settled ? undefined : ROLE[role].fg} dimColor={settled} wrap="truncate-end">{rightText}</Text>
      </Box>
      {open && subs.map((sub, i) => TreeRow(els, {
        depth: 1,
        last: i === subs.length - 1,
        glyph: glyphOf(sub.status),
        role: roleOf(sub.status),
        title: sub.title,
        right: sub.right,
        rightRole: sub.rightRole ?? 'idle',
      }, W))}
    </Box>
  )
}
