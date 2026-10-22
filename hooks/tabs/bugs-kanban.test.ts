// Bugs tab Kanban tests (plan bugs-tab, UI.3): PlanKanbanTab driven by
// BUG_KANBAN_CONFIG through a stub element table — the four bug labels, the
// bugs: view-state keys (never plan:), the kind filter row — and bugKanbanView
// over a sample mk-bug `list` reply written from the contract.
import { test, expect } from 'claude-code/testing'
import type { Elements } from 'claude-code'
import { PlanKanbanTab, columnOfTask } from './plankanban'
import { BUG_KANBAN_CONFIG, bugKanbanView } from './bugs-kanban'

type N = { type: string; props: Record<string, unknown>; children: unknown[] }

const node = (v: unknown): N => v as N
const kids = (v: unknown): N[] => {
  const arr = (v as { children?: unknown[] }).children
  return Array.isArray(arr) ? (arr as unknown[]).map(node) : []
}
const text = (v: unknown): string => {
  if (typeof v === 'string') return v
  const cur = node(v)
  if (cur.type === 'Button') return String(cur.props.label ?? '')
  return kids(v).map(c => (typeof c === 'string' ? c : text(c))).join('')
}
function allOf(root: unknown, type: string): N[] {
  const out: N[] = []
  const walk = (v: unknown): void => {
    if (typeof v !== 'object' || v === null) return
    const cur = node(v)
    if (cur.type === type) out.push(cur)
    kids(cur).forEach(walk)
  }
  walk(root)
  return out
}
const stubEls = (): Elements[keyof Elements] => {
  const make = (type: string) => (props: Record<string, unknown>, ...children: unknown[]) =>
    ({ type, props, children: children.length > 0 ? children : ((props.children as unknown[] | undefined) ?? []) }) as unknown
  return { Box: make('Box'), Text: make('Text'), Button: make('Button') } as unknown as Elements[keyof Elements]
}
const uiOf = (init?: Map<string, string>) => {
  const ui = init ?? new Map<string, string>()
  return { ui, handle: { get: (k: string) => ui.get(k), set: (k: string, v: string) => { ui.set(k, v) } } }
}

const row = (id: string, task_status: string, kind: string, extra: Record<string, unknown> = {}) => ({
  id, title: `title ${id}`, status: 'open', task_status, severity: 'med', kind, area: '', found_in: '',
  labels: [], tasks: { done: 0, total: 0 }, lane: null, updated: '2026-10-23T10:00:00Z', created: '2026-10-22T10:00:00Z',
  ...extra,
})
const reply = {
  ok: true,
  project: 'matrixkoder',
  counts: { open: 1, fixing: 1, needs: 1, fixed: 1, wontfix: 1 },
  bugs: [
    row('B5', 'blocked', 'cli', { status: 'needs', severity: 'high' }),
    row('B4', 'building', 'frontend', { status: 'fixing', lane: 'L2 opencode', tasks: { done: 1, total: 3 } }),
    row('B3', 'planned', 'store', { tasks: { done: 0, total: 2 } }),
    row('B2', 'done', 'frontend', { status: 'fixed' }),
    row('B1', 'done', 'docs', { status: 'wontfix', severity: 'low' }),
  ],
}
const NOW = '2026-10-23T12:00:00Z'
const view = bugKanbanView(reply, NOW)
const W92 = 92
const W46 = 46

test('bugKanbanView: one card per bug, kind as phase, kinds in the contract order', () => {
  expect(view.planId).toBe('matrixkoder')
  expect(view.cards.map(c => c.id)).toEqual(['B5', 'B4', 'B3', 'B2', 'B1'])
  expect(view.phases).toEqual(['frontend', 'cli', 'store', 'docs'])
  const b5 = view.cards[0]
  expect(b5).toMatchObject({ status: 'blocked', phase: 'cli', verdict: null, severity: 'high', age: '2h', title: 'title B5' })
  expect(view.unknownStatuses).toEqual([])
})

test('bugKanbanView: dim tag is the lane, else done/total tasks, else none', () => {
  expect(view.cards.find(c => c.id === 'B4')?.note).toBe('L2 opencode')
  expect(view.cards.find(c => c.id === 'B3')?.note).toBe('0/2 tasks')
  expect(view.cards.find(c => c.id === 'B5')?.note).toBeUndefined()
  // no clock: no age
  expect(bugKanbanView(reply).cards.every(c => c.age === '')).toBe(true)
})

test('columns: open/fixing/needs/fixed, paused needs you, wontfix lands in FIXED', () => {
  const col = (status: string) => columnOfTask({ id: 'B9', title: '', status, phase: 'cli', verdict: null, age: '' }, BUG_KANBAN_CONFIG)
  expect(col('planned')).toBe('PLANNED')
  expect(col('routed')).toBe('PLANNED')
  expect(col('building')).toBe('BUILDING')
  expect(col('review')).toBe('BUILDING')
  expect(col('blocked')).toBe('NEEDS YOU')
  expect(col('paused')).toBe('NEEDS YOU')
  expect(col('done')).toBe('ON MAIN')
  expect(col('merged')).toBe('ON MAIN')
  expect(col('mystery')).toBe('PLANNED')
  expect(columnOfTask(view.cards.find(c => c.id === 'B1')!, BUG_KANBAN_CONFIG)).toBe('ON MAIN')
  expect(BUG_KANBAN_CONFIG.labels).toEqual(['OPEN', 'FIXING', 'NEEDS YOU', 'FIXED'])
})

test('malformed input maps to an empty view and never throws', () => {
  const empty = { planId: '', phases: [], cards: [], unknownStatuses: [] }
  for (const bad of [null, undefined, 3, 'x', [], {}, { ok: false, error: 'nope' }, { ok: true, bugs: 'no' }]) {
    expect(bugKanbanView(bad, NOW)).toEqual(empty)
  }
  const partial = bugKanbanView({ ok: true, bugs: [null, 7, { title: 'no id' }, { id: 'B1', kind: 'weird', task_status: 'zzz' }] })
  expect(partial.cards).toHaveLength(1)
  expect(partial.cards[0]).toMatchObject({ id: 'B1', phase: 'other', status: 'zzz', title: '' })
  expect(partial.unknownStatuses).toEqual(['zzz'])
})

test('render: bug labels as headings, no plan labels, bug footer, kind filter row', () => {
  const { handle } = uiOf()
  const tree = PlanKanbanTab(stubEls(), view, W92, handle, BUG_KANBAN_CONFIG)
  const all = text(tree)
  for (const label of ['○ OPEN', '● FIXING', '▲ NEEDS YOU', '✓ FIXED']) expect(all).toContain(label)
  for (const gone of ['PLANNED', 'BUILDING', 'ON MAIN', 'k = all runs', 'this plan only']) expect(all).not.toContain(gone)
  expect(all).toContain("5 bugs · this repo's bugs")
  expect(all).toContain('all kinds')
  expect(all).not.toContain('all phases')
  // severity chip and tag line on a card
  expect(all).toContain('high')
  expect(all).toContain('L2 opencode')
})

test('render: stacked at 46 cols holds the same four columns', () => {
  const { handle } = uiOf()
  const all = text(PlanKanbanTab(stubEls(), view, W46, handle, BUG_KANBAN_CONFIG))
  for (const label of ['○ OPEN', '● FIXING', '▲ NEEDS YOU', '✓ FIXED']) expect(all).toContain(label)
})

test('kind filter writes bugs:kind and narrows the board; plan:/plankanban: keys stay untouched', () => {
  const { ui, handle } = uiOf()
  const tree = PlanKanbanTab(stubEls(), view, W92, handle, BUG_KANBAN_CONFIG)
  const seg = allOf(tree, 'Button').find(b => b.props.label === 'frontend' || String(b.props.label).includes('frontend'))
  ;(seg?.props.onPress as () => void)()
  expect(ui.get('bugs:kind')).toBe('frontend')
  expect([...ui.keys()]).toEqual(['bugs:kind'])
  const narrowed = PlanKanbanTab(stubEls(), view, W92, uiOf(new Map([['bugs:kind', 'frontend']])).handle, BUG_KANBAN_CONFIG)
  expect(text(narrowed)).toContain("2 bugs · this repo's bugs")
})

test('card press writes bugs:task (toggle), never plan:task', () => {
  const { ui, handle } = uiOf()
  const tree = PlanKanbanTab(stubEls(), view, W92, handle, BUG_KANBAN_CONFIG)
  const press = () => (allOf(tree, 'Button').find(b => b.props.label === 'B4')?.props.onPress as () => void)()
  press()
  expect(ui.get('bugs:task')).toBe('B4')
  press()
  expect(ui.get('bugs:task')).toBe('')
  expect(ui.has('plan:task')).toBe(false)
  expect(ui.has('plankanban:phase')).toBe(false)
})
