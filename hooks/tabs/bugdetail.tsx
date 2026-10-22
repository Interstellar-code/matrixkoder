// The /mk pane's Bugs tab detail (plan bugs-tab, UI.4; Bugs board BugDetail at
// 46 cols): `← Bugs ▸ B5` with the severity and status chips, the title, a
// METADATA card, a DESCRIPTION card, the sub-tab strip `tasks n/m` | `events n`
// (state key `bugs:detail:sub`), the tasks tree (a task press writes
// `bugs:task`; the pressed task opens the plan tab's PlanTaskPanel, running
// false) or the events rows, the action row and the key hint. The mapper reads
// the `mk-bug show` reply (the `plan` part is in `plans <id>` shape, so
// planFromReply and findPlanTask parse it unchanged). Pure render — no $, no
// state; a press only calls the `ui` callbacks the pane hands in (C1).
import type { Elements, RenderElement } from 'claude-code'
import { TYPE_ROLE } from './events'
import { ActionButton, Card, Chip, Section, Segment } from './kit'
import { PALETTE, ROLE } from './palette'
import type { PaletteRole } from './palette'
import { clip, planStatusGlyph, planStatusRole } from './plan'
import type { PlanView } from './plan'
import { planFromReply } from './plans-map'
import { PlanTaskPanel, findPlanTask } from './plantask'

type Els = Elements[keyof Elements]

/** the bug status words of the contract (CLI and UI) */
export const BUG_STATUS_ROLE: Record<string, PaletteRole> = { open: 'idle', fixing: 'live', needs: 'warn', fixed: 'ok', wontfix: 'idle' }
/** the severity chip roles */
export const BUG_SEVERITY_ROLE: Record<string, PaletteRole> = { high: 'err', med: 'warn', low: 'idle' }

/** one bug as `mk-bug show` returns it (BugFull); a field the reply lacks is '' / [] / null */
export type BugFull = {
  readonly id: string
  readonly title: string
  readonly status: string
  readonly severity: string
  readonly kind: string
  readonly area: string
  readonly found_in: string
  readonly labels: readonly string[]
  readonly tasks: { readonly done: number; readonly total: number }
  readonly lane: string | null
  readonly description: string
  readonly source: string
  readonly steps: string
  readonly expected: string
  readonly actual: string
  readonly links: readonly string[]
  readonly assignee: string
  readonly wontfix: boolean
}

/** one bus/state event of the bug's run, newest first */
export type BugEvent = { readonly id: string; readonly t: string; readonly kind: string; readonly task: string; readonly to: string; readonly text: string; readonly by: string }

export type BugDetailView = {
  /** null: the reply was malformed or named no bug */
  readonly bug: BugFull | null
  readonly project: string
  readonly plan: PlanView
  readonly events: readonly BugEvent[]
}

export type BugActKind = 'open-pane' | 'handoff' | 'nudge' | 'fixed'

/** C12: the tab's view-state handle; `bugAct` is the pane's action callback */
export type TabUi = {
  get(key: string): string | undefined
  set(key: string, value: string): void
  bugAct?(kind: BugActKind, id: string): void
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const str = (v: unknown): string => (typeof v === 'string' ? v : '')
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x !== '') : [])
const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
// drop control characters (bug text is user text headed for the terminal)
const clean = (s: string): string => s.replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')

/** The show reply as the tab's view; a malformed reply is an empty view and never throws. `project` is the
 *  slug the pane is on (the show reply does not carry it unless it adds a `project` string). */
export function bugDetailView(showReply: unknown, project = ''): BugDetailView {
  const empty: BugDetailView = { bug: null, project, plan: planFromReply(null), events: [] }
  if (!isRecord(showReply) || !isRecord(showReply.bug) || typeof showReply.bug.id !== 'string') return empty
  const b = showReply.bug
  const tasks = isRecord(b.tasks) ? b.tasks : {}
  const bug: BugFull = {
    id: b.id as string,
    title: str(b.title),
    status: str(b.status) || 'open',
    severity: str(b.severity) || 'med',
    kind: str(b.kind),
    area: str(b.area),
    found_in: str(b.found_in),
    labels: strs(b.labels),
    tasks: { done: num(tasks.done), total: num(tasks.total) },
    lane: typeof b.lane === 'string' && b.lane !== '' ? b.lane : null,
    description: str(b.description),
    source: str(b.source),
    steps: str(b.steps),
    expected: str(b.expected),
    actual: str(b.actual),
    links: strs(b.links),
    assignee: str(b.assignee),
    wontfix: b.wontfix === true,
  }
  const events: BugEvent[] = []
  if (Array.isArray(showReply.events)) {
    for (const raw of showReply.events) {
      if (!isRecord(raw)) continue
      events.push({ id: str(raw.id) || String(raw.id ?? ''), t: str(raw.t), kind: str(raw.kind), task: str(raw.task), to: str(raw.to), text: str(raw.text), by: str(raw.by) })
    }
  }
  return { bug, project: str(showReply.project) || project, plan: planFromReply(showReply.plan), events }
}

const hhmm = (t: string): string => /T(\d\d:\d\d)/.exec(t)?.[1] ?? t.slice(0, 5)

/** the dim-key row of the cards: the key padded to the key column, the value wrapped */
function kv(els: Els, key: string, value: string): RenderElement {
  const { Box, Text } = els
  return (
    <Box key={`bug-kv-${key}`} flexDirection="row" gap={1}>
      <Text dimColor={true}>{key.padEnd(9)}</Text>
      <Box flexShrink={1}><Text wrap="wrap">{clean(value)}</Text></Box>
    </Box>
  )
}

/** Pure render: elements only, no $, no state. */
export function BugDetailTab(els: Els, view: BugDetailView, cols: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const W = Math.max(1, cols - 4) // the Card's content width
  const bug = view.bug
  const back = ActionButton(els, { key: 'bug-back', glyph: '←', word: 'Bugs', plain: true, tone: 'quiet', onPress: () => { ui?.set('bugs:sel', '') } })
  if (bug == null) {
    return (
      <Box flexDirection="column" gap={1}>
        <Box flexDirection="row" gap={1}>{back}</Box>
        {Card(els, {}, <Text dimColor={true}>no such bug</Text>)}
      </Box>
    )
  }

  const sub = ui?.get('bugs:detail:sub') === 'events' ? 'events' : 'tasks'
  const taskSel = ui?.get('bugs:task') ?? ''
  const act = (kind: BugActKind): void => ui?.bugAct?.(kind, bug.id)
  const tasks = view.plan.phases?.flatMap(p => p.rows) ?? []
  const nTasks = bug.tasks.total > 0 ? bug.tasks : { done: tasks.filter(t => t.status === 'done' || t.status === 'merged').length, total: tasks.length }
  const closed = bug.status === 'fixed' || bug.status === 'wontfix'

  const meta: RenderElement[] = [
    <Box key="bug-kv-kind" flexDirection="row" gap={1}>
      <Text dimColor={true}>{'kind'.padEnd(9)}</Text>
      {bug.kind !== '' ? Chip(els, { label: bug.kind, role: 'live' }) : <Text dimColor={true}>—</Text>}
    </Box>,
    ...(view.project !== '' ? [kv(els, 'project', view.project)] : []),
    ...(bug.area !== '' ? [kv(els, 'area', bug.area)] : []),
    ...(bug.found_in !== '' ? [kv(els, 'found in', bug.found_in)] : []),
    ...(bug.source !== '' ? [kv(els, 'source', bug.source)] : []),
    ...(bug.assignee !== '' ? [kv(els, 'assignee', bug.assignee)] : []),
    ...(bug.labels.length > 0 ? [
      <Box key="bug-kv-labels" flexDirection="row" gap={1}>
        <Text dimColor={true}>{'labels'.padEnd(9)}</Text>
        <Box flexDirection="row" gap={1} flexWrap="wrap">{bug.labels.map(l => <Box key={`bug-label-${l}`}>{Chip(els, { label: clean(l), role: 'idle' })}</Box>)}</Box>
      </Box>,
    ] : []),
    ...(bug.links.length > 0 ? [kv(els, 'links', bug.links.join(' '))] : []),
  ]

  const taskRow = (id: string, title: string, status: string, slot: number | null | undefined, depth: number, pressable: boolean): RenderElement => {
    const lane = slot != null ? `L${String(slot)}` : null
    const budget = Math.max(1, W - 2 * depth - 2 - (lane == null ? 0 : lane.length + 3))
    const text = clip(clean(`${id} ${title}`), budget)
    return (
      <Box key={`bug-trow-${id}`} flexDirection="row" gap={1}>
        {depth > 0 && <Text dimColor={true}>{'  '.repeat(depth - 1)}└</Text>}
        <Text color={ROLE[planStatusRole(status)].fg}>{planStatusGlyph(status)}</Text>
        {pressable
          ? ActionButton(els, { key: `bug-task-${id}`, glyph: text, word: '', plain: true, tone: taskSel === id ? 'primary' : 'default', onPress: () => { ui?.set('bugs:task', taskSel === id ? '' : id) } })
          : <Text wrap="truncate-end">{text}</Text>}
        {lane != null && <Box flexGrow={1} />}
        {lane != null && Chip(els, { label: lane, role: 'live' })}
      </Box>
    )
  }

  const eventRow = (ev: BugEvent, i: number): RenderElement => {
    const word = ev.kind === 'transition' && ev.to !== '' ? ev.to : ev.kind
    const role = ev.kind === 'transition' && ev.to !== '' ? planStatusRole(ev.to) : TYPE_ROLE[ev.kind] ?? 'idle'
    const chipW = Math.max(word.length, 6) + 2
    const budget = Math.max(1, W - 5 - chipW - 2)
    return (
      <Box key={`bug-ev-${ev.id || i}`} flexDirection="row" gap={1}>
        <Text dimColor={true}>{hhmm(ev.t).padStart(5)}</Text>
        {Chip(els, { label: word.padEnd(6), role })}
        <Text wrap="truncate-end">{clip(clean(ev.text !== '' ? ev.text : ev.task), budget)}</Text>
      </Box>
    )
  }

  const selRow = taskSel !== '' ? findPlanTask(view.plan.phases, taskSel) : null
  return (
    <Box flexDirection="column" gap={1}>
      <Box flexDirection="row" gap={1}>
        {back}
        <Text dimColor={true}>▸</Text>
        <Text bold={true}>{bug.id}</Text>
        <Box flexGrow={1} />
        {Chip(els, { label: bug.severity, role: BUG_SEVERITY_ROLE[bug.severity] ?? 'idle' })}
        {Chip(els, { label: bug.status, role: BUG_STATUS_ROLE[bug.status] ?? 'idle' })}
      </Box>
      <Text bold={true} wrap="wrap">{clean(bug.title)}</Text>

      {Card(els, {}, (
        <Box flexDirection="column">
          {Section(els, { label: 'Metadata' })}
          {meta}
        </Box>
      ))}

      {Card(els, {}, (
        <Box flexDirection="column">
          {Section(els, { label: 'Description' })}
          <Text color={bug.description === '' ? PALETTE.dim : undefined} wrap="wrap">{bug.description === '' ? 'no description' : clean(bug.description)}</Text>
          {bug.steps !== '' && kv(els, 'repeat', bug.steps)}
          {bug.expected !== '' && kv(els, 'expected', bug.expected)}
          {bug.actual !== '' && kv(els, 'actual', bug.actual)}
        </Box>
      ))}

      <Box flexDirection="row" gap={1}>
        {Segment(els, { key: 'bug-sub-tasks', label: `tasks ${String(nTasks.done)}/${String(nTasks.total)}`, on: sub === 'tasks', onPress: () => { ui?.set('bugs:detail:sub', 'tasks') } })}
        {Segment(els, { key: 'bug-sub-events', label: `events ${String(view.events.length)}`, on: sub === 'events', onPress: () => { ui?.set('bugs:detail:sub', 'events') } })}
      </Box>

      {sub === 'tasks' && selRow != null && PlanTaskPanel(els, selRow, cols, () => { ui?.set('bugs:task', '') }, undefined, false)}
      {sub === 'tasks' && selRow == null && Card(els, {}, (
        <Box flexDirection="column">
          {tasks.length === 0 && <Text dimColor={true}>no tasks yet</Text>}
          {tasks.map(t => (
            <Box key={`bug-tree-${t.id}`} flexDirection="column">
              {taskRow(t.id, t.title, t.status, t.slot, 0, true)}
              {(t.subs ?? []).map(s => taskRow(s.id, s.title, s.status, s.slot, 1, false))}
            </Box>
          ))}
        </Box>
      ))}
      {sub === 'events' && Card(els, {}, (
        <Box flexDirection="column">
          {view.events.length === 0 && <Text dimColor={true}>no events yet</Text>}
          {view.events.map(eventRow)}
        </Box>
      ))}

      <Box flexDirection="row" gap={1}>
        {ActionButton(els, { key: 'bug-act-open-pane', glyph: '↗', word: 'open pane', tone: 'primary', primary: true, onPress: () => { act('open-pane') } })}
        {ActionButton(els, { key: 'bug-act-handoff', glyph: 'h', word: 'hand off', plain: true, hotkey: 'h', onPress: () => { act('handoff') } })}
        {ActionButton(els, { key: 'bug-act-nudge', glyph: '↻', word: 'nudge', plain: true, onPress: () => { act('nudge') } })}
        <Box flexGrow={1} />
        {!closed && ActionButton(els, { key: 'bug-act-fixed', glyph: '✓', word: 'fixed', plain: true, tone: 'quiet', onPress: () => { act('fixed') } })}
      </Box>
      {ActionButton(els, { key: 'bug-esc-back', glyph: 'esc back', word: '', plain: true, tone: 'quiet', onPress: () => { ui?.set('bugs:sel', '') } })}
    </Box>
  )
}
