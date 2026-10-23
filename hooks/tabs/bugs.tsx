// The /mk pane's Bugs tab (plan bugs-tab, UI.2/UI.3; Bugs boards BugsSimple,
// BugsList, BugsKanban at 46 cols): the sub-tab row `simple | list | kanban`
// (s / k; `l` is the Lanes tab's), `+ new` (a), then the picked body. Drill-down
// replaces the body: `bugs:sel` = a bug id opens BugDetailTab, `new` the
// BugNewForm. SIMPLE: counts per status word, the quick-add field with its kind
// and severity chips, one row per bug (open first) and the picked bug's card.
// LIST: status / kind / sort filters and one card per bug that expands to its
// task tree. KANBAN: the plan board with BUG_KANBAN_CONFIG; a card press opens
// the detail. Pure render — no $, no state; a press only calls `ui.set` or
// `ui.bugAct` (C1). The letter hotkeys stand down while a field has the focus.
import type { Elements, RenderElement } from 'claude-code'
import { BUG_SEVERITY_ROLE, BUG_STATUS_ROLE, BugDetailTab, bugLaneLine, laneOffNote, laneTaskOf } from './bugdetail'
import type { BugDetailView } from './bugdetail'
import { BUG_KINDS, BUG_SEVERITIES, BugNewForm } from './bugnew'
import type { BugDraft } from './bugnew'
import { BUG_KANBAN_CONFIG } from './bugs-kanban'
import { ActionButton, Card, Chip, Segment } from './kit'
import { PALETTE, ROLE } from './palette'
import { clip, planStatusGlyph, planStatusRole } from './plan'
import { PlanKanbanTab, timeInStatus } from './plankanban'
import type { PlanKanbanView } from './plankanban'
import { naturalCompare } from './plans-map'

type Els = Elements[keyof Elements]

/** one bug as `mk-bug list` returns it (BugRow); a missing field is '' / [] / null */
export type BugRow = {
  readonly id: string
  readonly title: string
  /** the bug word: open | fixing | needs | fixed | wontfix */
  readonly status: string
  readonly severity: string
  readonly kind: string
  readonly tasks: { readonly done: number; readonly total: number }
  readonly lane: string | null
  readonly updated: string
  readonly created: string
}

export type BugsSub = 'simple' | 'list' | 'kanban'

/** everything the tab draws; the pane builds it from its view state (bugs-core bugsViewOf) */
export type BugsView = {
  /** this repo's slug; '' when the pane has no project */
  readonly project: string
  /** false until the first list reply landed */
  readonly loaded: boolean
  readonly counts: Readonly<Record<string, number>>
  /** in the reply's order: needs, fixing, open, fixed, wontfix */
  readonly bugs: readonly BugRow[]
  readonly sub: BugsSub
  /** '' = the sub-tab body, `new` = the form, else the open bug's id */
  readonly sel: string
  /** the last show reply (the open detail, or the expanded list card); null before it lands */
  readonly detail: BugDetailView | null
  readonly kanban: PlanKanbanView
  readonly draft: BugDraft
  /** ISO now for the age column; '' draws no age */
  readonly now: string
  /** a one-line note (a press's result, e.g. `+ B3 filed`) */
  readonly note: string
}

/** C12: the tab's view-state handle; `bugAct` runs a mk-bug verb (see the pane's bugAction), `submit`
 *  sends an `mk:` line to the orchestrator (the plan tab's Play path) */
export type BugsUi = {
  get(key: string): string | undefined
  set(key: string, value: string): void
  bugAct?(kind: string, id?: string, text?: string): void
  submit?(text: string): void
}

export const BUG_GLYPH: Readonly<Record<string, string>> = { open: '○', fixing: '◐', needs: '⚠', fixed: '✓', wontfix: '⊘' }
const WORDS = ['open', 'fixing', 'needs', 'fixed'] as const
const SEV_RANK: Readonly<Record<string, number>> = { high: 0, med: 1, low: 2 }
const closed = (b: BugRow): boolean => b.status === 'fixed' || b.status === 'wontfix'

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const str = (v: unknown): string => (typeof v === 'string' ? v : '')
const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
// drop control characters (bug text is user text headed for the terminal)
const clean = (s: string): string => s.replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')

/** The `mk-bug list` reply's rows and counts; a malformed reply is empty and never throws. */
export function bugsFromList(reply: unknown): { counts: Record<string, number>; bugs: BugRow[] } {
  const counts: Record<string, number> = { open: 0, fixing: 0, needs: 0, fixed: 0, wontfix: 0 }
  const bugs: BugRow[] = []
  if (!isRecord(reply) || reply.ok !== true) return { counts, bugs }
  if (isRecord(reply.counts)) for (const k of Object.keys(counts)) counts[k] = num(reply.counts[k])
  for (const raw of Array.isArray(reply.bugs) ? reply.bugs : []) {
    if (!isRecord(raw) || typeof raw.id !== 'string') continue
    const tasks = isRecord(raw.tasks) ? raw.tasks : {}
    bugs.push({
      id: raw.id,
      title: str(raw.title),
      status: str(raw.status) || 'open',
      severity: str(raw.severity) || 'med',
      kind: str(raw.kind),
      tasks: { done: num(tasks.done), total: num(tasks.total) },
      lane: typeof raw.lane === 'string' && raw.lane !== '' ? raw.lane : null,
      updated: str(raw.updated),
      created: str(raw.created),
    })
  }
  return { counts, bugs }
}

/** The letter hotkeys stand down while one of these fields holds the focus (it swallows letters). */
export function bugsTyping(focus: string): boolean {
  return focus === 'bugs-q' || focus === 'bugs-task-q' || focus.startsWith('bug-new-in-') || focus === 'improve-edit' || focus.startsWith('improve-other-')
}

function inputOf(els: Els): ((props: Record<string, unknown>) => RenderElement) | null {
  return 'Input' in els ? (els as unknown as { Input: (props: Record<string, unknown>) => RenderElement }).Input : null
}

const glyphText = (els: Els, status: string): RenderElement => {
  const { Text } = els
  return <Text color={ROLE[BUG_STATUS_ROLE[status] ?? 'idle'].fg}>{BUG_GLYPH[status] ?? '○'}</Text>
}

/** SIMPLE: the counts card with the quick add, the Open/All rows and the picked bug's card. */
export function SimpleView(els: Els, view: BugsView, cols: number, ui: BugsUi): RenderElement {
  const { Box, Text } = els
  const W = Math.max(1, cols - 4) // the Card's content width
  const q = ui.get('bugs:q') ?? ''
  const qKind = ui.get('bugs:q:kind') ?? 'other'
  const qSev = ui.get('bugs:q:sev') ?? 'med'
  const filter = ui.get('bugs:simple:filter') === 'all' ? 'all' : 'open'
  const shown = view.bugs.filter(b => filter === 'all' || !closed(b))
  const pick = view.bugs.find(b => b.id === (ui.get('bugs:pick') ?? ''))
  // an empty Enter opens the form with details; text files the bug at once
  const submit = (text: string): void => { if (text.trim() === '') ui.set('bugs:sel', 'new'); else ui.bugAct?.('quick-add', '', text) }
  const Input = inputOf(els)
  const field = Input != null
    ? Input({ key: 'bugs-q', placeholder: '+ what broke? Enter', value: q, onInput: (v: string) => { ui.set('bugs:q', v) }, onSubmit: submit })
    : <Text dimColor={true}>{clip(q === '' ? '+ what broke? Enter' : clean(q), W)}</Text>
  const nOpen = view.bugs.filter(b => !closed(b)).length
  // ▶ to lane acts on the picked bug's only task (its show reply is the tick's detail); else pick one in the detail
  const picked = pick != null && view.detail?.bug?.id === pick.id ? view.detail : null
  const laneTask = picked == null ? null : laneTaskOf(picked.plan, '')
  const nRows = picked?.plan.phases?.flatMap(p => p.rows).length ?? 0
  const laneOff = pick == null ? ''
    : picked == null ? 'to lane: loading tasks…'
      : nRows === 0 ? 'to lane: add a task first'
        : nRows > 1 ? 'to lane: pick a task in the detail'
          : laneOffNote(['dispatch'], laneTask)
  const empty = !view.loaded ? 'loading…' : view.bugs.length === 0 ? 'no bugs yet. type above, Enter.' : 'all clear'
  return (
    <Box flexDirection="column" gap={1}>
      {Card(els, {}, (
        <Box flexDirection="column">
          <Text bold={true} color={ROLE.ok.fg}>Bugs</Text>
          <Box flexDirection="row" gap={2} flexWrap="wrap">
            {WORDS.map(w => <Text key={`bugs-count-${w}`} color={ROLE[BUG_STATUS_ROLE[w] ?? 'idle'].fg}>{`${BUG_GLYPH[w] ?? ''} ${String(view.counts[w] ?? 0)} ${w}`}</Text>)}
          </Box>
          <Text dimColor={true}>{clip(`${clean(view.project || 'no project')} · bugs run in the project db`, W)}</Text>
          <Box flexDirection="row" gap={1}>{field}</Box>
          <Box flexDirection="row" gap={1} flexWrap="wrap">
            {BUG_KINDS.map(k => Segment(els, { key: `bugs-q-kind-${k}`, label: k, on: qKind === k, onPress: () => { ui.set('bugs:q:kind', k) } }))}
          </Box>
          <Box flexDirection="row" gap={1}>
            {BUG_SEVERITIES.map(s => Segment(els, { key: `bugs-q-sev-${s}`, label: s, on: qSev === s, onPress: () => { ui.set('bugs:q:sev', s) } }))}
          </Box>
        </Box>
      ))}
      <Box flexDirection="row" gap={1}>
        {Segment(els, { key: 'bugs-sf-open', label: `Open ${String(nOpen)}`, on: filter === 'open', onPress: () => { ui.set('bugs:simple:filter', 'open') } })}
        {Segment(els, { key: 'bugs-sf-all', label: `All ${String(view.bugs.length)}`, on: filter === 'all', onPress: () => { ui.set('bugs:simple:filter', 'all') } })}
      </Box>
      {Card(els, {}, (
        <Box flexDirection="column">
          {shown.length === 0 && <Text dimColor={true}>{empty}</Text>}
          {shown.map(b => (
            <Box key={`bugs-srow-${b.id}`} flexDirection="row" gap={1}>
              {glyphText(els, b.status)}
              {ActionButton(els, {
                key: `bugs-row-${b.id}`, glyph: clip(clean(`${b.id} ${b.title}`), Math.max(1, W - 2 - b.severity.length - 3)), word: '', plain: true,
                tone: pick?.id === b.id ? 'primary' : 'default', onPress: () => { ui.set('bugs:pick', pick?.id === b.id ? '' : b.id) },
              })}
              <Box flexGrow={1} />
              {Chip(els, { label: b.severity, role: BUG_SEVERITY_ROLE[b.severity] ?? 'idle' })}
            </Box>
          ))}
        </Box>
      ))}
      {pick != null && Card(els, {}, (
        <Box flexDirection="column">
          <Box flexDirection="row" gap={1}>
            {glyphText(els, pick.status)}
            <Text bold={true}>{pick.id}</Text>
            <Text dimColor={true}>{pick.status}</Text>
            <Box flexGrow={1} />
            {Chip(els, { label: pick.severity, role: BUG_SEVERITY_ROLE[pick.severity] ?? 'idle' })}
          </Box>
          <Text wrap="wrap">{clean(pick.title)}</Text>
          <Box flexDirection="row" gap={1} flexWrap="wrap">
            {ActionButton(els, { key: 'bugs-pick-open', glyph: '↵', word: 'open', tone: 'primary', onPress: () => { ui.set('bugs:sel', pick.id) } })}
            {ActionButton(els, { key: 'bugs-pick-lane', glyph: '▶', word: 'to lane', tone: laneOff === '' ? 'default' : 'disabled', onPress: () => { if (laneTask != null) ui.submit?.(bugLaneLine('dispatch', laneTask.id)) } })}
            {!closed(pick) && ActionButton(els, { key: 'bugs-pick-fixed', glyph: '✓', word: 'fixed', onPress: () => { ui.bugAct?.('fixed', pick.id) } })}
          </Box>
          {laneOff !== '' && <Text dimColor={true} wrap="wrap">{clean(laneOff)}</Text>}
        </Box>
      ))}
    </Box>
  )
}

const STATUS_FILTERS = [['active', 'active'], ['needs', '⚠ needs'], ['fixing', '◐ fixing'], ['fixed', '✓ fixed'], ['all', 'all']] as const
const SORTS = ['severity', 'age', 'id'] as const

/** LIST: the filter rows, then one card per bug with its two-line head; ▸ expands its task tree. */
export function ListView(els: Els, view: BugsView, cols: number, ui: BugsUi): RenderElement {
  const { Box, Text } = els
  const W = Math.max(1, cols - 4)
  const status = ui.get('bugs:list:status') || 'active'
  const kind = ui.get('bugs:list:kind') ?? ''
  const sort = ui.get('bugs:list:sort') || 'severity'
  const open = ui.get('bugs:open') ?? ''
  const adding = ui.get('bugs:addtask') ?? ''
  const kinds = BUG_KINDS.filter(k => view.bugs.some(b => b.kind === k))
  const ageOf = (b: BugRow): string => {
    if (view.now === '' || b.updated === '') return ''
    const a = timeInStatus(b.updated, view.now)
    return a === '—' ? '' : a
  }
  const shown = view.bugs
    .filter(b => (status === 'all' ? true : status === 'active' ? !closed(b) : status === 'fixed' ? closed(b) : b.status === status))
    .filter(b => kind === '' || b.kind === kind)
  const sorted = [...shown].sort((a, b) =>
    sort === 'age' ? a.created.localeCompare(b.created) || naturalCompare(a.id, b.id)
      : sort === 'id' ? naturalCompare(b.id, a.id)
        : (SEV_RANK[a.severity] ?? 1) - (SEV_RANK[b.severity] ?? 1) || naturalCompare(b.id, a.id))
  const Input = inputOf(els)
  const tree = (id: string): RenderElement => {
    const d = view.detail
    if (d == null || d.bug?.id !== id) return <Text dimColor={true}>loading tasks…</Text>
    const rows = d.plan.phases?.flatMap(p => p.rows) ?? []
    if (rows.length === 0) return <Text dimColor={true}>no tasks yet</Text>
    const line = (rid: string, title: string, st: string, depth: number): RenderElement => (
      <Box key={`bugs-tree-${rid}`} flexDirection="row" gap={1}>
        <Text dimColor={true}>{depth === 0 ? '├' : '  └'}</Text>
        <Text color={ROLE[planStatusRole(st)].fg}>{planStatusGlyph(st)}</Text>
        <Text wrap="truncate-end">{clip(clean(`${rid} ${title}`), Math.max(1, W - 4 - 2 * depth))}</Text>
      </Box>
    )
    return (
      <Box flexDirection="column">
        {rows.map(t => (
          <Box key={`bugs-treeg-${t.id}`} flexDirection="column">
            {line(t.id, t.title, t.status, 0)}
            {(t.subs ?? []).map(s => line(s.id, s.title, s.status, 1))}
          </Box>
        ))}
      </Box>
    )
  }
  const card = (b: BugRow): RenderElement => {
    const isOpen = open === b.id
    const head2 = [view.project || 'no project', `tasks ${String(b.tasks.done)}/${String(b.tasks.total)}`, b.lane ?? 'no lane', ageOf(b)].filter(s => s !== '').join(' · ')
    return (
      <Box key={`bugs-card-${b.id}`} flexDirection="column">
        {Card(els, {}, (
          <Box flexDirection="column">
            <Box flexDirection="row" gap={1}>
              {ActionButton(els, { key: `bugs-x-${b.id}`, glyph: isOpen ? '▾' : '▸', word: '', plain: true, onPress: () => { ui.set('bugs:open', isOpen ? '' : b.id) } })}
              {glyphText(els, b.status)}
              <Text bold={true} wrap="truncate-end">{clip(clean(`${b.id} ${b.title}`), Math.max(1, W - 4 - b.severity.length - 3))}</Text>
              <Box flexGrow={1} />
              {Chip(els, { label: b.severity, role: BUG_SEVERITY_ROLE[b.severity] ?? 'idle' })}
            </Box>
            <Text dimColor={true}>{clip(`    ${clean(head2)}`, W)}</Text>
            {isOpen && tree(b.id)}
            {isOpen && (
              <Box flexDirection="row" gap={1}>
                {ActionButton(els, { key: `bugs-detail-${b.id}`, glyph: '↵', word: 'detail', plain: true, onPress: () => { ui.set('bugs:sel', b.id) } })}
                {ActionButton(els, { key: `bugs-addtask-${b.id}`, glyph: '+', word: 'task', plain: true, onPress: () => { ui.set('bugs:addtask', adding === b.id ? '' : b.id) } })}
              </Box>
            )}
            {isOpen && adding === b.id && Input != null && Input({
              key: 'bugs-task-q', placeholder: 'type a task, Enter', value: ui.get('bugs:task:q') ?? '',
              onInput: (v: string) => { ui.set('bugs:task:q', v) },
              onSubmit: (v: string) => { if (v.trim() !== '') ui.bugAct?.('task-add', b.id, v) },
            })}
          </Box>
        ))}
      </Box>
    )
  }
  return (
    <Box flexDirection="column" gap={1}>
      <Box flexDirection="row" gap={1} flexWrap="wrap">
        {STATUS_FILTERS.map(([k, label]) => Segment(els, { key: `bugs-ls-${k}`, label, on: status === k, onPress: () => { ui.set('bugs:list:status', k) } }))}
      </Box>
      {kinds.length > 0 && (
        <Box flexDirection="row" gap={1} flexWrap="wrap">
          {Segment(els, { key: 'bugs-lk-all', label: 'all kinds', on: kind === '', onPress: () => { ui.set('bugs:list:kind', '') } })}
          {kinds.map(k => Segment(els, { key: `bugs-lk-${k}`, label: k, on: kind === k, onPress: () => { ui.set('bugs:list:kind', k) } }))}
        </Box>
      )}
      <Box flexDirection="row" gap={1}>
        <Text dimColor={true}>sort</Text>
        {SORTS.map(s => Segment(els, { key: `bugs-lo-${s}`, label: s, on: sort === s, onPress: () => { ui.set('bugs:list:sort', s) } }))}
      </Box>
      {sorted.length === 0 && <Text dimColor={true}>{view.loaded ? 'no bugs here' : 'loading…'}</Text>}
      {sorted.map(card)}
    </Box>
  )
}

/** Pure render: elements only, no $, no state. */
export function BugsTab(els: Els, view: BugsView, cols: number, ui: BugsUi): RenderElement {
  const { Box, Text } = els
  if (view.sel === 'new') return BugNewForm(els, view.draft, cols, ui)
  if (view.sel !== '') {
    if (view.detail != null && (view.detail.bug == null || view.detail.bug.id === view.sel)) return BugDetailTab(els, view.detail, cols, ui)
    return (
      <Box flexDirection="column" gap={1}>
        <Box flexDirection="row" gap={1}>
          {ActionButton(els, { key: 'bug-back', glyph: '←', word: 'Bugs', plain: true, tone: 'quiet', onPress: () => { ui.set('bugs:sel', '') } })}
          <Text dimColor={true}>{`▸ ${view.sel} loading…`}</Text>
        </Box>
      </Box>
    )
  }
  const typing = bugsTyping(ui.get('settings:focus') ?? '')
  const hot = (k: string): string | undefined => (typing ? undefined : k)
  // the kanban card press writes its task key: on this board it opens the bug's detail
  const kanbanUi = {
    get: (k: string) => (k === BUG_KANBAN_CONFIG.taskKey ? '' : ui.get(k)),
    set: (k: string, v: string) => { ui.set(k === BUG_KANBAN_CONFIG.taskKey ? 'bugs:sel' : k, v) },
  }
  const subs: readonly [BugsSub, string, string | undefined][] = [['simple', 'simple', 's'], ['list', 'list', undefined], ['kanban', 'kanban', 'k']]
  return (
    <Box flexDirection="column" gap={1}>
      <Box flexDirection="row" gap={1}>
        {subs.map(([name, label, key]) => Segment(els, { key: `bugs-sub-${name}`, label, on: view.sub === name, ...(key != null ? { hotkey: hot(key) } : {}), onPress: () => { ui.set('bugs:sub', name) } }))}
        <Box flexGrow={1} />
        {Segment(els, { key: 'bugs-new', label: '+ new', on: false, hotkey: hot('a'), onPress: () => { ui.set('bugs:sel', 'new') } })}
      </Box>
      {view.note !== '' && <Text color={PALETTE.dim} wrap="wrap">{clean(view.note)}</Text>}
      {view.sub === 'list' ? ListView(els, view, cols, ui)
        : view.sub === 'kanban' ? PlanKanbanTab(els, view.kanban, cols, kanbanUi, BUG_KANBAN_CONFIG)
          : SimpleView(els, view, cols, ui)}
    </Box>
  )
}
