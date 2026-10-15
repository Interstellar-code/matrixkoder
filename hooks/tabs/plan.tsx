// The /mk pane's Plan tab Detail (plan settings-plans-db, V1.2, contract C11):
// one Card per PHASE, folded by default. The word "wave" is not drawn; a phase
// that shares a wave with another shows a small `with <id>` chip. Fold state
// lives in the C12 view-state handle (`plan:phase:<id>`); absent = folded.
// The tab stays pure — no $, no state, no scripts.
import type { Elements, RenderElement } from 'claude-code'
import { PLAN_FIXTURE } from './plan.fixture'
import { NARROW, STATUS_GLYPH, fitLegend } from './ui'
import { ActionButton, Card, Chip, TreeRow } from './kit'
import { laneStrip, pickBar } from './raster'
import { ROLE } from './palette'
import type { PaletteRole } from './palette'
import { laneAgent } from './plans-map'

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
  /** F.1 contract: the derived risk, its critical flag and the open reasons. */
  readonly risk?: number
  readonly critical?: boolean
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
  readonly type?: string
  readonly round?: number
  readonly verdict?: string
  /** F.1 contract: the derived risk (int 1-5), its critical flag, and the open
   *  reasons. All optional — an old row carries none of them. */
  readonly risk?: number
  readonly critical?: boolean
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
  /** V1.5: routing fields a phase-level task row carries, when it has them */
  readonly slot?: number | null
  readonly pane?: string | null
  readonly branch?: string | null
  readonly updated?: string | null
  readonly acceptance?: string | null
  readonly owned?: readonly string[]
  readonly deps?: readonly string[]
  readonly route?: PlanRoute | null
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
    readonly design: string
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
}

/** T1.2: the fixture, mock flag on (C1/C6). */
export function planView(): PlanView {
  return { ...PLAN_FIXTURE, mock: true }
}

const STATUS_ROLE: Record<string, PaletteRole> = {
  dispatched: 'live', building: 'live', verifying: 'live', review: 'live', fix: 'warn', qa: 'live',
  merged: 'ok', done: 'ok',
  blocked: 'warn', quota: 'warn', stalled: 'warn', provider: 'warn',
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

const phaseKey = (id: string): string => `plan:phase:${id}`

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
  return (
    <Box flexDirection="column" gap={1}>
      {view.mock && <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>}
      {view.plan != null && HeaderCard(els, view, W, compact)}
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text bold={true} color="subtle">PHASES</Text>
        <Box flexDirection="row" gap={1}>
          {ActionButton(els, {
            key: 'plan-expand-all', glyph: '▾', word: 'Expand all', tone: 'quiet',
            onPress: () => { for (const p of phases) ui?.set(phaseKey(p.id), '1') },
          })}
          {ActionButton(els, {
            key: 'plan-collapse-all', glyph: '▸', word: 'Collapse all', tone: 'quiet',
            onPress: () => { for (const p of phases) ui?.set(phaseKey(p.id), '') },
          })}
        </Box>
      </Box>
      {phases.map(phase => PhaseCard(els, phase, shared.get(phase.wave ?? -1) ?? [], W, compact, ui))}
      {!compact && (
        <Text dimColor={true} wrap="truncate-end">{clip(fitLegend([...view.footer], cols), cols)}</Text>
      )}
    </Box>
  )
}

function HeaderCard(els: Els, view: PlanView, W: number, compact: boolean): RenderElement {
  const { Box, Text } = els
  const head = view.plan
  if (head == null) return Card(els, {}, <Text></Text>)
  const kv = (k: string, v: string): RenderElement => (
    <Box flexDirection="row" gap={1}>
      <Text dimColor={true}>{k}</Text>
      <Text wrap="truncate-end">{clip(v, Math.max(1, W - k.length - 2))}</Text>
    </Box>
  )
  return Card(els, { role: 'live' }, (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text bold={true}>{clip(head.id, Math.max(1, W - head.statusChip.length - 6))}</Text>
        {Chip(els, { label: compact ? clip(head.statusChip, 12) : head.statusChip, role: 'live' })}
      </Box>
      <Text color={ROLE.idle.fg} wrap="truncate-end">{clip(head.title, W)}</Text>
      {kv('created', head.created)}
      {kv('runs', head.runs)}
      {kv('merges to', head.mergesTo)}
      {kv('design', head.design)}
      {head.roundFiles != null && head.roundFiles !== '' && kv('round files', head.roundFiles)}
      <Text dimColor={true}>{head.counts}</Text>
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
function PhaseCard(els: Els, phase: PlanPhase, waveMates: readonly string[], W: number, compact: boolean, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const legacy = phase.tone != null
  const expanded = legacy ? true : ui?.get(phaseKey(phase.id)) === '1'
  const routed = phaseRouted(phase)
  const lane = laneAgent(phase)
  const toneRole: PaletteRole = phase.tone === 'green' ? 'ok' : phase.tone === 'blue' ? 'live' : 'idle'
  const headRole = phase.headerRole ?? toneRole
  const live = (phase.tone === 'blue') || phase.rows.some(r => roleOf(r.status) === 'live' || roleOf(r.status) === 'warn')
  const others = waveMates.filter(id => id !== phase.id)
  const strip = pickBar(
    els,
    `plan-strip-${phase.id}`,
    laneStrip(phase.rows.map(r => roleOf(r.status)), W),
    <Text dimColor={true}>{phase.rows.map(r => glyphOf(r.status)).join('')}</Text>,
  )
  return Card(els, live ? { role: 'live' } : {}, (
    <Box key={phase.id} flexDirection="column">
      <Box flexDirection="row" gap={1}>
        {!legacy && <Text dimColor={true}>{expanded ? '▾' : '▸'}</Text>}
        {!legacy && <Text color={ROLE[headRole].fg}>{glyphOf(phase.status ?? 'planned')}</Text>}
        {!legacy && ActionButton(els, {
          key: `plan-fold-${phase.id}`,
          glyph: '›',
          word: clip(`${phase.id} · ${phase.title}`, Math.max(1, W - phase.header.length - 12 - (!compact && lane !== '' ? lane.length + 1 : 0))),
          plain: true,
          noHotkey: true,
          onPress: () => {
            ui?.set(phaseKey(phase.id), expanded ? '' : '1')
            if (routed) ui?.set('plan:task', ui?.get('plan:task') === phase.id ? '' : phase.id)
          },
        })}
        {legacy && <Text bold={true} color="subtle">{phase.name ?? phase.id}</Text>}
        <Box flexGrow={1} />
        {!compact && routed && lane !== '' && <Text dimColor={true} wrap="truncate-end">{lane}</Text>}
        {others.length > 0 && Chip(els, { label: `with ${others[0]}`, role: 'idle' })}
        {Chip(els, { label: compact && phase.shortHeader != null ? phase.shortHeader : phase.header, role: headRole })}
      </Box>
      {strip}
      {expanded && !legacy && phase.rows.map(row => TaskBlock(els, row, W, compact, ui))}
      {legacy && phase.rows.map(row => LegacyRow(els, row, W, compact, ui))}
      {expanded && phase.note != null && <Text dimColor={true}>{clip(phase.note, W)}</Text>}
    </Box>
  ))
}

/** One task: the V1.5 row — status glyph, task id, the title cut to the space
 *  left, and the dim lane·agent tag right-aligned. The row is pressable and
 *  selects the task (`plan:task`); pressing the selected task again clears it.
 *  When the width cannot fit id + ≥10 title cols + the tag, the tag drops
 *  first — the title never shrinks below 10 to keep it. */
function TaskBlock(els: Els, row: PlanTask, W: number, compact: boolean, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const role = roleOf(row.status)
  const title = compact ? row.short?.title ?? row.title : row.title
  const lane = laneAgent(row)
  const chrome = 6 // '├ ' + gap + glyph + gap + the gap before the tag
  const showLane = lane !== '' && chrome + row.id.length + 1 + 10 + lane.length <= W
  const titleBudget = Math.max(1, showLane
    ? W - chrome - row.id.length - 1 - lane.length
    : W - chrome - row.id.length - 1)
  const subs = row.subs ?? []
  return (
    <Box key={row.id} flexDirection="column">
      <Box flexDirection="row" gap={1}>
        <Text dimColor={true}>{'├ '}</Text>
        <Text color={ROLE[role].fg}>{glyphOf(row.status)}</Text>
        {ActionButton(els, {
          key: `plan-task-${row.id}`,
          glyph: clip(`${row.id} ${title}`, titleBudget),
          word: '',
          plain: true,
          onPress: () => { if (ui != null) ui.set('plan:task', ui.get('plan:task') === row.id ? '' : row.id) },
        })}
        <Box flexGrow={1} />
        {showLane && <Text dimColor={true} wrap="truncate-end">{lane}</Text>}
      </Box>
      {subs.map((sub, i) => TreeRow(els, {
        depth: 1,
        last: i === subs.length - 1,
        glyph: glyphOf(sub.status),
        role: roleOf(sub.status),
        title: `${sub.id} ${sub.title}`,
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
