// Bugs tab render tests (plan bugs-tab, UI.2/UI.3): the list mapper on a contract-shaped
// `mk-bug list` reply, the SIMPLE and LIST views, the drill-down and the kanban card press.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { BugsTab, ListView, SimpleView, bugsFromList, bugsTyping } from './bugs'
import type { BugsUi, BugsView } from './bugs'
import { bugDetailView } from './bugdetail'
import { bugDraftOf } from './bugnew'
import { bugKanbanView } from './bugs-kanban'

const scalarProps = (props: object): Record<string, string | boolean | number> => {
  const out: Record<string, string | boolean | number> = {}
  for (const [k, v] of Object.entries(props)) {
    if (typeof v === 'string' || typeof v === 'boolean' || typeof v === 'number') out[k] = v
  }
  return out
}
const isNode = (k: RenderChildren): k is RenderNode =>
  typeof k === 'string' || (typeof k === 'object' && k !== null && !Array.isArray(k))
const kids = (c: RenderChildren | undefined): RenderNode[] | undefined => {
  if (c == null) return undefined
  const arr: readonly RenderChildren[] = Array.isArray(c) ? c : [c]
  return arr.filter(isNode)
}
const inert = (): RenderElement => ({ type: 'Text', props: {}, children: [] })
const stubEls: Elements[keyof Elements] = {
  Box: (props: BoxProps & ElementChildren): RenderElement => ({ type: 'Box', props: scalarProps(props), children: kids(props.children) }),
  Text: (props: TextProps & ElementChildren): RenderElement => ({ type: 'Text', props: scalarProps(props), children: kids(props.children) }),
  Button: (props: Record<string, unknown>) => ({ type: 'Button', props, children: [] }),
  Input: (props: Record<string, unknown>) => ({ type: 'Input', props, children: [] }),
  Select: inert, Link: inert, Code: inert, Markdown: inert, Client: inert, Raster: inert, Image: inert,
} as unknown as Elements[keyof Elements]

type Node = { type?: string; props?: Record<string, unknown>; children?: unknown[] }
function nodesOf(n: unknown, type: string): Node[] {
  const out: Node[] = []
  const walk = (node: unknown): void => {
    if (typeof node !== 'object' || node === null) return
    const cur = node as Node
    if (cur.type === type) out.push(cur)
    for (const c of cur.children ?? []) walk(c)
  }
  walk(n)
  return out
}
const allText = (n: unknown): string =>
  nodesOf(n, 'Text').map(t => (t.children ?? []).map(c => (typeof c === 'string' ? c : '')).join('')).join('\n')
  + '\n' + nodesOf(n, 'Button').map(b => String(b.props?.label ?? '')).join('\n')
const button = (drawn: unknown, key: string): Node | undefined => nodesOf(drawn, 'Button').find(x => String(x.props?.key) === key)
function press(drawn: unknown, key: string): void {
  const b = button(drawn, key)
  expect(b, `button ${key}`).toBeDefined()
  ;(b?.props?.onPress as () => void)()
}

// a list reply written from the contract (BugRow; order needs, fixing, open, fixed, wontfix)
const LIST = {
  ok: true,
  project: 'matrixkoder',
  counts: { open: 1, fixing: 1, needs: 1, fixed: 1, wontfix: 0 },
  bugs: [
    { id: 'B4', title: 'lane stalls on quota', status: 'needs', task_status: 'blocked', severity: 'low', kind: 'infra', area: '', found_in: '', labels: [], tasks: { done: 0, total: 1 }, lane: 'L2', updated: '2026-10-23T08:00:00Z', created: '2026-10-22T08:00:00Z' },
    { id: 'B3', title: 'kanban drops a card', status: 'fixing', task_status: 'building', severity: 'high', kind: 'frontend', area: '', found_in: '', labels: [], tasks: { done: 1, total: 2 }, lane: null, updated: '2026-10-23T09:00:00Z', created: '2026-10-21T08:00:00Z' },
    { id: 'B2', title: 'stale run in the header', status: 'open', task_status: 'planned', severity: 'med', kind: 'frontend', area: '', found_in: '', labels: [], tasks: { done: 0, total: 0 }, lane: null, updated: '2026-10-23T07:00:00Z', created: '2026-10-20T08:00:00Z' },
    { id: 'B1', title: 'old crash', status: 'fixed', task_status: 'done', severity: 'high', kind: 'cli', area: '', found_in: '', labels: [], tasks: { done: 1, total: 1 }, lane: null, updated: '2026-10-22T07:00:00Z', created: '2026-10-19T08:00:00Z' },
  ],
}
const SHOW_B3 = {
  ok: true,
  bug: { ...LIST.bugs[1], description: '', source: '', steps: '', expected: '', actual: '', links: [], assignee: '', wontfix: false },
  plan: { id: 'B3', title: 'kanban drops a card', status: 'building', live_run: 'bugs', phases: [{ id: 'B3', title: 'kanban drops a card', status: 'building', tasks: [
    { id: 'B3.1', title: 'map the status', status: 'done', subtasks: [{ id: 'B3.1.1', title: 'add a test', status: 'done' }] },
    { id: 'B3.2', title: 'draw the column', status: 'building', subtasks: [] },
  ] }] },
  events: [],
}

const NOW = '2026-10-23T10:00:00Z'
function viewOf(over: Partial<BugsView> = {}, state: Map<string, string> = new Map()): BugsView {
  const { counts, bugs } = bugsFromList(LIST)
  return {
    project: 'matrixkoder', loaded: true, counts, bugs, sub: 'simple', sel: '', detail: null,
    kanban: bugKanbanView(LIST, NOW), draft: bugDraftOf(k => state.get(k), 'B5', 'matrixkoder'), now: NOW, note: '', ...over,
  }
}
function uiOf(state: Map<string, string> = new Map()): { ui: BugsUi; sets: [string, string][]; acts: unknown[][] } {
  const sets: [string, string][] = []
  const acts: unknown[][] = []
  return {
    ui: { get: k => state.get(k), set: (k, v) => { sets.push([k, v]); state.set(k, v) }, bugAct: (...a: unknown[]) => { acts.push(a) } },
    sets,
    acts,
  }
}

test('bugsFromList reads the contract reply; a malformed one is empty and never throws', () => {
  const { counts, bugs } = bugsFromList(LIST)
  expect(counts).toEqual({ open: 1, fixing: 1, needs: 1, fixed: 1, wontfix: 0 })
  expect(bugs.map(b => b.id)).toEqual(['B4', 'B3', 'B2', 'B1'])
  expect(bugs[0]).toMatchObject({ status: 'needs', severity: 'low', lane: 'L2', tasks: { done: 0, total: 1 } })
  for (const bad of [null, 7, { ok: false }, { ok: true, bugs: [null, { id: 3 }] }]) expect(bugsFromList(bad).bugs).toEqual([])
})

test('SIMPLE: the counts per status word, the project line, open rows first and the Open/All filter', () => {
  const { ui, sets } = uiOf()
  const drawn = SimpleView(stubEls, viewOf(), 46, ui)
  const t = allText(drawn)
  for (const c of ['○ 1 open', '◐ 1 fixing', '⚠ 1 needs', '✓ 1 fixed']) expect(t).toContain(c)
  expect(t).toContain('matrixkoder · bugs run in the project db')
  const rows = nodesOf(drawn, 'Button').map(b => String(b.props?.key)).filter(k => k.startsWith('bugs-row-'))
  expect(rows, 'Open hides the fixed bug, reply order kept').toEqual(['bugs-row-B4', 'bugs-row-B3', 'bugs-row-B2'])
  expect(nodesOf(drawn, 'Input').map(i => i.props?.placeholder)).toEqual(['+ what broke? Enter'])
  press(drawn, 'bugs-sf-all')
  press(drawn, 'bugs-q-kind-cli')
  press(drawn, 'bugs-q-sev-high')
  expect(sets).toEqual([['bugs:simple:filter', 'all'], ['bugs:q:kind', 'cli'], ['bugs:q:sev', 'high']])
  const all = SimpleView(stubEls, viewOf(), 46, ui)
  expect(nodesOf(all, 'Button').map(b => String(b.props?.key)).filter(k => k.startsWith('bugs-row-'))).toEqual(['bugs-row-B4', 'bugs-row-B3', 'bugs-row-B2', 'bugs-row-B1'])
})

test('SIMPLE: Enter with text files it, an empty Enter opens the form; the picked card opens, fixes, and draws to lane inert', () => {
  const state = new Map<string, string>()
  const { ui, sets, acts } = uiOf(state)
  const drawn = SimpleView(stubEls, viewOf(), 46, ui)
  const input = nodesOf(drawn, 'Input')[0]!
  ;(input.props?.onSubmit as (v: string) => void)('pane crashes')
  ;(input.props?.onSubmit as (v: string) => void)('  ')
  expect(acts).toEqual([['quick-add', '', 'pane crashes']])
  expect(sets).toEqual([['bugs:sel', 'new']])
  press(drawn, 'bugs-row-B3')
  const picked = SimpleView(stubEls, viewOf(), 46, ui)
  expect(allText(picked)).toContain('▶ to lane')
  press(picked, 'bugs-pick-lane')
  press(picked, 'bugs-pick-fixed')
  press(picked, 'bugs-pick-open')
  expect(acts.at(-1)).toEqual(['fixed', 'B3'])
  expect(sets.at(-1)).toEqual(['bugs:sel', 'B3'])
  expect(acts, 'to lane is inert').toHaveLength(2)
  expect(allText(picked), 'its tasks are not loaded yet').toContain('to lane: loading tasks…')
})

test('LANE 5: SIMPLE ▶ to lane submits mk: dispatch bug <task id> for the picked bug\'s only task; off with a note otherwise', () => {
  const state = new Map<string, string>([['bugs:pick', 'B3']])
  const sent: string[] = []
  const { ui } = uiOf(state)
  const withSubmit: BugsUi = { ...ui, submit: t => { sent.push(t) } }
  const task = (over: Record<string, unknown>) => ({ ...SHOW_B3, plan: { ...SHOW_B3.plan, phases: [{ ...SHOW_B3.plan.phases[0]!, tasks: [{ id: 'B3.2', title: 'draw the column', status: 'building', subtasks: [], ...over }] }] } })
  const ok = SimpleView(stubEls, viewOf({ detail: bugDetailView(task({ acceptance: 'the column draws' }), 'matrixkoder') }), 46, withSubmit)
  expect(button(ok, 'bugs-pick-lane')?.props?.dimColor).not.toBe(true)
  press(ok, 'bugs-pick-lane')
  expect(sent).toEqual(['mk: dispatch bug B3.2'])
  const noAcc = SimpleView(stubEls, viewOf({ detail: bugDetailView(task({}), 'matrixkoder') }), 46, withSubmit)
  expect(allText(noAcc)).toContain('to lane: no acceptance')
  press(noAcc, 'bugs-pick-lane')
  const two = SimpleView(stubEls, viewOf({ detail: bugDetailView(SHOW_B3, 'matrixkoder') }), 46, withSubmit)
  expect(allText(two)).toContain('to lane: pick a task in the detail')
  press(two, 'bugs-pick-lane')
  expect(sent, 'off presses send nothing').toEqual(['mk: dispatch bug B3.2'])
})

test('LIST: the status, kind and sort filters; a card has its two-line head and expands to the task tree', () => {
  const state = new Map<string, string>()
  const { ui, sets } = uiOf(state)
  const drawn = ListView(stubEls, viewOf({ sub: 'list' }), 46, ui)
  const keys = nodesOf(drawn, 'Button').map(b => String(b.props?.key))
  expect(keys.filter(k => k.startsWith('bugs-x-')), 'active, severity first').toEqual(['bugs-x-B3', 'bugs-x-B2', 'bugs-x-B4'])
  expect(keys.filter(k => k.startsWith('bugs-lk-')), 'only the kinds present, in the contract order').toEqual(['bugs-lk-all', 'bugs-lk-frontend', 'bugs-lk-cli', 'bugs-lk-infra'])
  expect(allText(drawn)).toContain('matrixkoder · tasks 0/1 · L2 · 2h')
  press(drawn, 'bugs-x-B3')
  expect(sets).toEqual([['bugs:open', 'B3']])
  const loading = ListView(stubEls, viewOf({ sub: 'list' }), 46, ui)
  expect(allText(loading)).toContain('loading tasks…')
  const open = ListView(stubEls, viewOf({ sub: 'list', detail: bugDetailView(SHOW_B3, 'matrixkoder') }), 46, ui)
  const t = allText(open)
  expect(t).toContain('B3.1 map the status')
  expect(t).toContain('B3.1.1 add a test')
  expect(t).toContain('▾')
  press(open, 'bugs-detail-B3')
  expect(sets.at(-1)).toEqual(['bugs:sel', 'B3'])
  state.set('bugs:list:status', 'fixed')
  state.set('bugs:list:sort', 'id')
  const fixed = ListView(stubEls, viewOf({ sub: 'list' }), 46, ui)
  expect(nodesOf(fixed, 'Button').map(b => String(b.props?.key)).filter(k => k.startsWith('bugs-x-'))).toEqual(['bugs-x-B1'])
})

test('the tab: sub-tab row with s / k / a hotkeys (none while typing), drill-down to new and detail, kanban card opens the bug', () => {
  const state = new Map<string, string>()
  const { ui, sets } = uiOf(state)
  const drawn = BugsTab(stubEls, viewOf(), 46, ui)
  const hot = (d: unknown) => nodesOf(d, 'Button').filter(b => String(b.props?.key).startsWith('bugs-sub-') || b.props?.key === 'bugs-new').map(b => b.props?.hotkey ?? '-')
  expect(hot(drawn)).toEqual(['s', '-', 'k', 'a'])
  expect(bugsTyping('bugs-q') && bugsTyping('bug-new-in-title') && !bugsTyping('dash-tab-bugs')).toBe(true)
  state.set('settings:focus', 'bugs-q')
  expect(hot(BugsTab(stubEls, viewOf(), 46, ui))).toEqual(['-', '-', '-', '-'])
  expect(allText(BugsTab(stubEls, viewOf({ sel: 'new' }), 46, ui))).toContain('New bug')
  // the form's own Inputs carry onSubmit (the engine refuses one without), with no wrapper in BugsTab
  const formInputs = nodesOf(BugsTab(stubEls, viewOf({ sel: 'new' }), 46, ui), 'Input')
  expect(formInputs.length).toBe(4)
  for (const i of formInputs) expect(typeof i.props?.onSubmit, String(i.props?.key)).toBe('function')
  expect(allText(BugsTab(stubEls, viewOf({ sel: 'B3' }), 46, ui))).toContain('▸ B3 loading…')
  expect(allText(BugsTab(stubEls, viewOf({ sel: 'B3', detail: bugDetailView(SHOW_B3) }), 46, ui))).toContain('METADATA')
  const board = BugsTab(stubEls, viewOf({ sub: 'kanban' }), 46, ui)
  const bt = allText(board)
  for (const col of ['OPEN', 'FIXING', 'NEEDS YOU', 'FIXED']) expect(bt).toContain(col)
  press(board, 'plankanban-card-B2')
  expect(sets.at(-1)).toEqual(['bugs:sel', 'B2'])
  expect(sets.some(([k]) => k.startsWith('plan')), 'never a plan key').toBe(false)
  expect(allText(BugsTab(stubEls, viewOf({ note: 'handoff: comes with lane dispatch' }), 46, ui))).toContain('handoff: comes with lane dispatch')
})
