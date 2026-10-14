// Plan sub-tab wiring (settings-plans-db, V1.3 + V1.4): the Segment row,
// selection across sub-tabs, file mode (no store → today's Plan tab), action
// submit, and the Kanban sub-tab's PlanKanbanTab mount.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { PlanPane, planSubOf, readPlans, selectedPlanId } from '../dashboard'
import { planActionLine } from './plans'
import { planView } from './plan'

const scalarProps = (props: object): Record<string, string | boolean | number> => {
  const out: Record<string, string | boolean | number> = {}
  for (const [k, v] of Object.entries(props)) {
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') out[k] = v
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
const stubEls: Elements[keyof Elements] = {
  Box: (props: BoxProps & ElementChildren): RenderElement => ({ type: 'Box', props: scalarProps(props), children: kids(props.children) }),
  Text: (props: TextProps & ElementChildren): RenderElement => ({ type: 'Text', props: scalarProps(props), children: kids(props.children) }),
  Button: (props: Record<string, unknown>) => ({ type: 'Button', props, children: [] }),
  Input: () => ({ type: 'Text', props: {}, children: [] }),
  Select: () => ({ type: 'Text', props: {}, children: [] }),
  Link: () => ({ type: 'Text', props: {}, children: [] }),
  Code: () => ({ type: 'Text', props: {}, children: [] }),
  Markdown: () => ({ type: 'Text', props: {}, children: [] }),
  Client: () => ({ type: 'Text', props: {}, children: [] }),
  Raster: (props: Record<string, unknown>) => ({ type: 'Raster', props: scalarProps(props), children: [] }),
  Image: () => ({ type: 'Text', props: {}, children: [] }),
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

const LIST = JSON.stringify({
  ok: true,
  plans: [
    { id: 'alpha', title: 'Alpha', status: 'running', created: 'c', runs: ['alpha-r1'], live_run: 'alpha-r1', counts: { building: 1 } },
    { id: 'beta', title: 'Beta', status: 'draft', created: 'c', runs: [], live_run: null, counts: {} },
  ],
})
const DETAIL = JSON.stringify({
  ok: true, id: 'alpha', title: 'Alpha', status: 'running', created: 'c', runs: ['alpha-r1'], live_run: 'alpha-r1',
  integration: 'main', design: [], counts: { building: 1 },
  phases: [{ id: 'P1', title: 'Phase', status: 'building', wave: 1, tasks: [{ id: 'T1', title: 'Task', status: 'building', subtasks: [], slot: 3, pane: 'mk-3', branch: 'mk/alpha-r1/t1', updated: '2026-10-14T11:00:00+02:00', acceptance: 'it works', owned: ['P2'], deps: ['P0'], route: { agent: 'opencode', model: 'zai-coding-plan/glm-5.3', effort: 'high' } }] }],
})

test('file mode: no store draws today\'s Plan tab and no Segment row', () => {
  const ui = new Map<string, string>()
  const drawn = PlanPane(stubEls, {}, 80, { get: k => ui.get(k), set: (k, v) => { ui.set(k, v) } })
  const text = allText(drawn)
  expect(text).toContain('MOCK · fixture data · not this run')
  expect(text).not.toContain('List')
  expect(nodesOf(drawn, 'Button').some(b => String(b.props?.key) === 'plan-sub-list')).toBe(false)
  // the fixture planView is what file mode mounts
  expect(planView().mock).toBe(true)
})

test('store mode: Segment row List Detail Kanban, default list', () => {
  const ui = new Map<string, string>()
  const view: Record<string, string> = { 'plans:store': '1', 'plans:list': LIST }
  const drawn = PlanPane(stubEls, view, 80, { get: k => ui.get(k), set: (k, v) => { ui.set(k, v) } })
  const labels = nodesOf(drawn, 'Button').map(b => String(b.props?.label ?? ''))
  expect(labels).toContain('List')
  expect(labels).toContain('Detail')
  expect(labels).toContain('Kanban')
  expect(planSubOf(view)).toBe('list')
  expect(allText(drawn)).toContain('ONGOING RUNS')
})

test('sub-tab switch keeps plans:sel', () => {
  const ui = new Map<string, string>([['plans:sel', 'beta']])
  const view: Record<string, string> = { 'plans:store': '1', 'plans:list': LIST, 'plans:sel': 'beta', 'plan:sub': 'list' }
  const drawn = PlanPane(stubEls, view, 80, { get: k => ui.get(k), set: (k, v) => { ui.set(k, v) } })
  press(drawn, 'plan-sub-detail')
  expect(ui.get('plan:sub')).toBe('detail')
  expect(ui.get('plans:sel') ?? view['plans:sel']).toBe('beta')
  expect(selectedPlanId({ ...view, 'plan:sub': 'detail' }, [{ id: 'alpha', live_run: 'alpha-r1' }, { id: 'beta', live_run: null }])).toBe('beta')
})

test('open switches to detail; another action submits planActionLine once', () => {
  const ui = new Map<string, string>()
  const submitted: string[] = []
  const view: Record<string, string> = { 'plans:store': '1', 'plans:list': LIST }
  const handle = {
    get: (k: string) => ui.get(k),
    set: (k: string, v: string) => { ui.set(k, v) },
    submit: (text: string) => { submitted.push(text) },
  }
  press(PlanPane(stubEls, view, 80, handle), 'plans-open-alpha')
  expect(ui.get('plan:sub')).toBe('detail')
  expect(ui.get('plans:sel')).toBe('alpha')
  expect(submitted).toEqual([])
  press(PlanPane(stubEls, view, 80, handle), 'plans-pause-alpha')
  expect(submitted).toEqual([planActionLine('pause', 'alpha')])
})

test('store mode: the Kanban sub-tab renders PlanKanbanTab (four columns, no KanbanTab list)', () => {
  const ui = new Map<string, string>()
  const view: Record<string, string> = {
    'plans:store': '1', 'plans:list': LIST, 'plan:sub': 'kanban', 'plans:sel': 'alpha',
    'plans:detail': DETAIL, 'plans:detail-for': 'alpha',
  }
  const drawn = PlanPane(stubEls, view, 80, { get: k => ui.get(k), set: (k, v) => { ui.set(k, v) } })
  const text = allText(drawn)
  expect(text).toContain('PLANNED')
  expect(text).toContain('BUILDING')
  expect(text).toContain('NEEDS YOU')
  expect(text).toContain('ON MAIN')
  expect(text).toContain('T1')
  expect(text).not.toContain('Needs you off')
  expect(text).toContain('1 tasks · this plan only · source mk.db')
  // the active Segment carries the plan id (V1.4 spec 2)
  const labels = nodesOf(drawn, 'Button').map(b => String(b.props?.label ?? ''))
  expect(labels).toContain('Kanban · alpha')
  expect(labels).toContain('all phases')
})

/** ── V1.5: the task detail panel wiring ───────────────────────────────── */

const panelView = (extra: Record<string, string> = {}): Record<string, string> => ({
  'plans:store': '1', 'plans:list': LIST, 'plan:sub': 'detail', 'plans:sel': 'alpha',
  'plans:detail': DETAIL, 'plans:detail-for': 'alpha',
  ...extra,
})

test('with plan:task set the panel draws under the Detail body at 80 cols', () => {
  const ui = new Map<string, string>()
  const drawn = PlanPane(stubEls, panelView({ 'plan:task': 'T1' }), 80, { get: k => ui.get(k), set: (k, v) => { ui.set(k, v) } })
  const text = allText(drawn)
  expect(text).toContain('T1 · building') // the panel Card title
  expect(text).toContain('lane')
  expect(text).toContain('L3')
  expect(text).toContain('zai-coding-plan/glm-5.3') // the model row, uncut
  expect(text).toContain('effort')
  expect(text).toContain('acceptance')
  expect(text).toContain('esc close')
  expect(text).toContain('PHASES') // the sub-tab body still draws above it
})

test('under 59 cols the panel replaces the body', () => {
  const ui = new Map<string, string>()
  const drawn = PlanPane(stubEls, panelView({ 'plan:task': 'T1' }), 36, { get: k => ui.get(k), set: (k, v) => { ui.set(k, v) } })
  const text = allText(drawn)
  expect(text).toContain('T1 · building')
  expect(text).not.toContain('PHASES') // the body gave way to the panel
})

test('the panel also draws on the Kanban sub-tab (from the same detail reply)', () => {
  const ui = new Map<string, string>()
  const drawn = PlanPane(stubEls, panelView({ 'plan:sub': 'kanban', 'plan:task': 'T1' }), 80, { get: k => ui.get(k), set: (k, v) => { ui.set(k, v) } })
  const text = allText(drawn)
  expect(text).toContain('BUILDING')
  expect(text).toContain('T1 · building')
})

test('selecting the same task again clears it; the close line clears it too', () => {
  const ui = new Map<string, string>()
  const handle = { get: (k: string) => ui.get(k), set: (k: string, v: string) => { ui.set(k, v) } }
  const drawn = PlanPane(stubEls, panelView(), 80, handle)
  press(drawn, 'plan-fold-P1') // open the phase so the task row draws
  expect(ui.get('plan:phase:P1')).toBe('1')
  press(PlanPane(stubEls, panelView(), 80, handle), 'plan-task-T1') // the Detail task row's press
  expect(ui.get('plan:task')).toBe('T1')
  const withPanel = PlanPane(stubEls, panelView({ 'plan:task': 'T1' }), 80, handle)
  expect(allText(withPanel)).toContain('T1 · building')
  press(withPanel, 'plan-task-T1') // the same row again: toggle off
  expect(ui.get('plan:task')).toBe('')
  press(PlanPane(stubEls, panelView({ 'plan:task': 'T1' }), 80, handle), 'plan-task-close-T1')
  expect(ui.get('plan:task')).toBe('')
})

test('a plan:task naming no task of the shown plan draws no panel', () => {
  const ui = new Map<string, string>()
  const drawn = PlanPane(stubEls, panelView({ 'plan:task': 'ZZZ' }), 80, { get: k => ui.get(k), set: (k, v) => { ui.set(k, v) } })
  const text = allText(drawn)
  expect(text).not.toContain('ZZZ ·')
  expect(text).toContain('PHASES')
})

test('readPlans does not spawn when the store is missing', async () => {
  let calls = 0
  const got = await readPlans(async () => { calls += 1; return { stdout: '', code: 0 } }, '/plugin', '/data', false, 'alpha')
  expect(calls).toBe(0)
  expect(got.missing).toBe(true)
})

test('readPlans maps a list reply and a detail reply', async () => {
  const argvSeen: string[][] = []
  const got = await readPlans(async argv => {
    argvSeen.push([...argv])
    const detail = argv.includes('alpha')
    return { stdout: detail ? DETAIL : LIST, code: 0 }
  }, '/plugin', '/data', true, 'alpha')
  expect(got.error).toBeNull()
  expect(argvSeen[0]?.slice(-3)).toEqual(['--json', '--data', '/data'])
  expect(argvSeen[1]).toContain('alpha')
  const list = got.list as { plans: { id: string }[] }
  expect(list.plans[0]?.id).toBe('alpha')
  const detail = got.detail as { id: string }
  expect(detail.id).toBe('alpha')
})
