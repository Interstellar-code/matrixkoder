// Bug detail tests (plan bugs-tab, UI.4): the mapper on a `mk-bug show` reply
// written from the contract, a malformed reply, the sub-tab switch, the
// metadata rows, the task drill-down and the action callbacks.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { BugDetailTab, bugDetailView } from './bugdetail'
import type { BugActKind } from './bugdetail'

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
  Input: inert, Select: inert, Link: inert, Code: inert, Markdown: inert, Client: inert, Raster: inert, Image: inert,
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
function press(drawn: unknown, key: string): void {
  const b = nodesOf(drawn, 'Button').find(x => String(x.props?.key) === key)
  expect(b, `button ${key}`).toBeDefined()
  ;(b?.props?.onPress as () => void)()
}

// a show reply written from the contract: BugFull, the plan in `plans <id>` shape (one phase: the bug row), events newest first
const SHOW = {
  ok: true,
  bug: {
    id: 'B5', title: 'Plans tab shows a stale run', status: 'fixing', task_status: 'building', severity: 'high', kind: 'frontend',
    area: 'hooks/tabs/plans', found_in: 'v0.7.5', labels: ['regression', 'ui'], tasks: { done: 1, total: 2 }, lane: 'L3',
    updated: '2026-10-23T09:00:00+02:00', created: '2026-10-22T09:00:00+02:00',
    description: 'The list keeps the old run after a merge.', source: 'todo 4', steps: 'merge a run, open Plans', expected: 'the run leaves the list', actual: 'it stays',
    links: ['https://example.test/b5'], assignee: 'neo', wontfix: false,
  },
  plan: {
    ok: true, id: 'B5', title: 'Plans tab shows a stale run', status: 'building', runs: ['bugs'], live_run: 'bugs',
    phases: [{
      id: 'B5', title: 'Plans tab shows a stale run', status: 'building',
      tasks: [
        { id: 'B5.1', title: 'drop the merged run', status: 'building', slot: 3, acceptance: 'list is fresh', subtasks: [{ id: 'B5.1.1', title: 'add a test', status: 'done' }] },
        { id: 'B5.2', title: 'check the kanban', status: 'planned', subtasks: [] },
      ],
    }],
  },
  events: [
    { id: 12, t: '2026-10-23T09:12:00+02:00', kind: 'transition', task: 'B5.1', to: 'building', text: 'B5.1 planned to building', by: 'orch' },
    { id: 11, t: '2026-10-23T08:50:00+02:00', kind: 'info', task: 'B5', to: '', text: 'created', by: 'user' },
  ],
}

function draw(state: Record<string, string> = {}, reply: unknown = SHOW, cols = 46) {
  const map = new Map(Object.entries(state))
  const acts: [BugActKind, string][] = []
  const ui = { get: (k: string) => map.get(k), set: (k: string, v: string) => { map.set(k, v) }, bugAct: (kind: BugActKind, id: string) => { acts.push([kind, id]) } }
  return { tree: BugDetailTab(stubEls, bugDetailView(reply, 'matrixkoder'), cols, ui), map, acts }
}

test('the mapper reads the bug, the plan tree and the events from a show reply', () => {
  const v = bugDetailView(SHOW, 'matrixkoder')
  expect(v.bug?.id).toBe('B5')
  expect(v.bug?.labels).toEqual(['regression', 'ui'])
  expect(v.bug?.tasks).toEqual({ done: 1, total: 2 })
  expect(v.project).toBe('matrixkoder')
  expect(v.plan.phases?.[0]?.rows.map(r => r.id)).toEqual(['B5.1', 'B5.2'])
  expect(v.plan.phases?.[0]?.rows[0]?.subs?.map(s => s.id)).toEqual(['B5.1.1'])
  expect(v.events.map(e => e.id)).toEqual(['12', '11'])
})

test('a malformed reply is an empty view and never throws', () => {
  for (const bad of [null, undefined, 3, 'x', [], {}, { ok: false, error: 'no such bug' }, { bug: 7 }, { bug: { title: 'no id' } }, { bug: { id: 'B1' }, plan: 5, events: 'x' }]) {
    expect(() => bugDetailView(bad)).not.toThrow()
  }
  expect(bugDetailView({ ok: false }).bug).toBeNull()
  expect(bugDetailView({ bug: { id: 'B1' }, plan: 5, events: 'x' }).events).toEqual([])
  expect(allText(draw({}, { ok: false }).tree)).toContain('no such bug')
})

test('the header, metadata and description rows are drawn', () => {
  const text = allText(draw().tree)
  for (const s of ['← Bugs', 'B5', 'high', 'fixing', 'Plans tab shows a stale run', 'METADATA', 'frontend', 'matrixkoder', 'hooks/tabs/plans', 'v0.7.5', 'todo 4', 'neo', 'regression', 'ui', 'https://example.test/b5']) {
    expect(text, s).toContain(s)
  }
  for (const k of ['kind', 'project', 'area', 'found in', 'source', 'assignee', 'labels', 'links']) expect(text, k).toContain(k)
  for (const s of ['DESCRIPTION', 'The list keeps the old run', 'repeat', 'merge a run, open Plans', 'expected', 'the run leaves the list', 'actual', 'it stays']) expect(text, s).toContain(s)
  // empty fields are left out
  const bare = allText(draw({}, { bug: { id: 'B9', title: 'bare' } }).tree)
  expect(bare).toContain('METADATA')
  for (const k of ['area', 'found in', 'assignee', 'labels', 'links', 'repeat']) expect(bare, k).not.toContain(k)
})

test('the sub-tab strip is tasks n/m and events n; default tasks, a press writes bugs:detail:sub', () => {
  const d = draw()
  const labels = nodesOf(d.tree, 'Button').map(b => String(b.props?.label ?? ''))
  expect(labels).toContain('tasks 1/2')
  expect(labels).toContain('events 2')
  expect(labels.filter(l => /^(tasks|events|plan|log)/.test(l)).length).toBe(2)
  expect(allText(d.tree)).toContain('B5.1 drop the merged run')
  expect(allText(d.tree)).not.toContain('B5.1 planned to building')
  press(d.tree, 'bug-sub-events')
  expect(d.map.get('bugs:detail:sub')).toBe('events')
  const ev = allText(draw({ 'bugs:detail:sub': 'events' }).tree)
  expect(ev).toContain('09:12')
  expect(ev).toContain('B5.1 planned to building')
  expect(ev).toContain('building')
  expect(ev).not.toContain('B5.2 check the kanban')
  press(draw({ 'bugs:detail:sub': 'events' }).tree, 'bug-sub-tasks')
})

test('the tasks tree shows status glyphs, the lane chip and the subtask; a press writes bugs:task and opens the panel', () => {
  const d = draw()
  const text = allText(d.tree)
  expect(text).toContain('L3')
  expect(text).toContain('B5.1.1 add a test')
  press(d.tree, 'bug-task-B5.1')
  expect(d.map.get('bugs:task')).toBe('B5.1')
  const open = draw({ 'bugs:task': 'B5.1' })
  const t = allText(open.tree)
  expect(t).toContain('B5.1 · building') // the PlanTaskPanel title
  expect(t).toContain('list is fresh')
  expect(t).toContain('subtasks')
  expect(t).not.toContain('B5.2 check the kanban') // the panel takes the tree's place
  press(open.tree, 'plan-task-close-B5.1')
  expect(open.map.get('bugs:task')).toBe('')
})

test('the action row calls bugAct with the bug id; a closed bug has no fixed action', () => {
  const d = draw()
  press(d.tree, 'bug-act-open-pane')
  press(d.tree, 'bug-act-handoff')
  press(d.tree, 'bug-act-nudge')
  press(d.tree, 'bug-act-fixed')
  expect(d.acts).toEqual([['open-pane', 'B5'], ['handoff', 'B5'], ['nudge', 'B5'], ['fixed', 'B5']])
  press(d.tree, 'bug-back')
  expect(d.map.get('bugs:sel')).toBe('')
  const closed = draw({}, { ...SHOW, bug: { ...SHOW.bug, status: 'fixed' } })
  expect(nodesOf(closed.tree, 'Button').some(b => String(b.props?.key) === 'bug-act-fixed')).toBe(false)
  // `esc back` is a pressable button (the pane arms no raw Esc), and no unbound key hint is drawn
  expect(allText(d.tree)).not.toContain('↑↓ task')
  press(d.tree, 'bug-esc-back')
  expect(d.map.get('bugs:sel')).toBe('')
  expect(nodesOf(d.tree, 'Button').some(b => b.props?.label === 'esc back')).toBe(true)
})

test('no detail button arms a hotkey that is a tab-bar key (o l p u f e w n b t i 9)', () => {
  const TAB_KEYS = new Set(['o', 'l', 'p', 'u', 'f', 'e', 'w', 'n', 'b', 't', 'i', '9'])
  for (const d of [draw(), draw({ 'bugs:detail:sub': 'events' })]) {
    const armed = nodesOf(d.tree, 'Button').map(b => b.props?.hotkey).filter((h): h is string => typeof h === 'string')
    expect(armed.filter(h => TAB_KEYS.has(h))).toEqual([])
    expect(armed).toEqual(['h'])
  }
})

test('without ui the render still draws and presses do nothing', () => {
  const tree = BugDetailTab(stubEls, bugDetailView(SHOW), 36)
  expect(allText(tree)).toContain('METADATA')
  expect(() => press(tree, 'bug-act-nudge')).not.toThrow()
})
