// Real-code tests for the Plan List sub-tab (settings-plans-db, V1.1): the
// pure PlansTab render through the same stub elements the Plan tab tests use.
// They press Buttons and read text — runs before plans, the C10 button set per
// status, the two-press Delete, the filter, the row press, the no-live-run
// chip, and a 36-col render.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { PlansTab, planActionLine } from './plans'
import type { PlansView } from './plans'
import { PLANS_FIXTURE } from './plans.fixture'

const scalarProps = (props: object): Record<string, string | number | boolean> => {
  const out: Record<string, string | number | boolean> = {}
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

const boxTag = (props: BoxProps & ElementChildren): RenderElement =>
  ({ type: 'Box', props: scalarProps(props), children: kids(props.children) })
const textTag = (props: TextProps & ElementChildren): RenderElement =>
  ({ type: 'Text', props: scalarProps(props), children: kids(props.children) })

const stubEls: Elements[keyof Elements] = {
  Box: boxTag,
  Text: textTag,
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

// visible text in draw order: Text children plus the name Buttons (plan rows `plans-detail-*`, run cards `plans-run-<id>`)
const isName = (key: string): boolean => key.startsWith('plans-detail-') || (key.startsWith('plans-run-') && !/^plans-run-(open|pause)-/.test(key))
const allText = (n: unknown): string => {
  const out: string[] = []
  const walk = (node: unknown): void => {
    if (typeof node !== 'object' || node === null) return
    const cur = node as Node
    if (cur.type === 'Text') out.push((cur.children ?? []).map(c => (typeof c === 'string' ? c : '')).join(''))
    if (cur.type === 'Button' && isName(String(cur.props?.key))) out.push(String(cur.props?.label))
    for (const c of cur.children ?? []) walk(c)
  }
  walk(n)
  return out.join('\n')
}

function press(drawn: unknown, key: string): void {
  const b = nodesOf(drawn, 'Button').find(x => String(x.props?.key) === key)
  expect(b, `button ${key}`).toBeDefined()
  ;(b?.props?.onPress as () => void)()
}

const ARCHIVED_VIEW: PlansView = {
  ...PLANS_FIXTURE,
  plans: [...PLANS_FIXTURE.plans, { id: 'old-plan', title: 'old', status: 'archived', created: '2026-10-01', runs: [], live_run: null, counts: { tasks: 1, phases: 0 } }],
}

const viewWith = (onAction: (kind: string, id: string) => void): PlansView =>
  ({ ...PLANS_FIXTURE, onAction })

test('Plans list: runs render before plans, MOCK chip on', () => {
  const ui = new Map<string, string>()
  const text = allText(PlansTab(stubEls, PLANS_FIXTURE, 80, ui))
  expect(text).toContain('MOCK · fixture data · not this run')
  expect(text).toContain('ONGOING RUNS')
  const runsAt = text.indexOf('settings-plans-db-r1')
  const plansAt = text.indexOf('PLANS')
  const planRowAt = text.indexOf('checkout-v2')
  expect(runsAt, 'the run card').toBeGreaterThan(-1)
  expect(runsAt).toBeLessThan(plansAt)
  expect(plansAt).toBeLessThan(planRowAt)
  expect(text).toContain('7/12 Verify')
  expect(text).toContain('mk-bus-e2e-r1')
})

test('Plans list: each status shows exactly its buttons', () => {
  const ui = new Map<string, string>()
  const drawn = PlansTab(stubEls, PLANS_FIXTURE, 80, ui)
  const of = (id: string): string[] =>
    nodesOf(drawn, 'Button')
      .filter(b => {
        const k = String(b.props?.key ?? '')
        return k.startsWith('plans-') && k.endsWith(`-${id}`) && !k.startsWith('plans-detail-') && !k.includes('confirm')
      })
      .map(b => String(b.props?.key ?? '').split('-')[1] ?? '')
  expect(of('lane-nudge')).toEqual(['start'])
  expect(of('checkout-v2')).toEqual(['start'])
  expect(of('settings-plans-db').sort()).toEqual(['open', 'pause'])
  expect(of('hive-cards')).toEqual(['resume'])
  expect(of('workflows-v2')).toEqual(['archive'])
  const archived = PlansTab(stubEls, ARCHIVED_VIEW, 80, new Map([['plans:filter', 'archived']]))
  const keys = nodesOf(archived, 'Button').map(b => String(b.props?.key ?? '')).filter(k => k.endsWith('-old-plan') && !k.startsWith('plans-detail-'))
  expect(keys.sort()).toEqual(['plans-delete-old-plan', 'plans-unarchive-old-plan'])
})

test('Plans list: Delete needs two presses; Cancel clears; the second calls onAction once', () => {
  const ui = new Map<string, string>()
  const calls: string[] = []
  const view: PlansView = { ...ARCHIVED_VIEW, onAction: (kind, id) => { calls.push(`${kind}:${id}`) } }
  ui.set('plans:filter', 'archived')
  const first = PlansTab(stubEls, view, 80, ui)
  expect(allText(first)).not.toContain('Delete old-plan?')
  press(first, 'plans-delete-old-plan')
  expect(ui.get('plans:confirm')).toBe('old-plan')
  expect(calls, 'the first press does not act').toEqual([])
  const confirm = PlansTab(stubEls, view, 80, ui)
  expect(allText(confirm)).toContain('Delete old-plan?')
  press(confirm, 'plans-confirm-cancel-old-plan')
  expect(ui.get('plans:confirm')).toBe('')
  expect(allText(PlansTab(stubEls, view, 80, ui))).not.toContain('Delete old-plan?')
  press(PlansTab(stubEls, view, 80, ui), 'plans-delete-old-plan')
  press(PlansTab(stubEls, view, 80, ui), 'plans-confirm-delete-old-plan')
  expect(calls).toEqual(['delete:old-plan'])
})

test('Plans list: Start, Pause and Resume call onAction with the right kind', () => {
  const ui = new Map<string, string>()
  const calls: string[] = []
  const view = viewWith((kind, id) => { calls.push(`${kind}:${id}`) })
  const drawn = PlansTab(stubEls, view, 80, ui)
  press(drawn, 'plans-start-lane-nudge')
  press(drawn, 'plans-pause-mk-bus-e2e')
  press(drawn, 'plans-resume-hive-cards')
  press(drawn, 'plans-open-settings-plans-db')
  expect(calls).toEqual([
    'start:lane-nudge',
    'pause:mk-bus-e2e',
    'resume:hive-cards',
    'open:settings-plans-db',
  ])
})

test('planActionLine builds the prompt line V1.3 submits', () => {
  expect(planActionLine('start', 'checkout-v2')).toBe('mk: start plan checkout-v2')
  expect(planActionLine('pause', 'settings-plans-db')).toBe('mk: pause plan settings-plans-db')
  expect(planActionLine('resume', 'hive-cards')).toBe('mk: resume plan hive-cards')
  expect(planActionLine('delete', 'workflows-v2')).toBe('mk: delete plan workflows-v2')
})

test('Plans list: the filter Segments narrow the list', () => {
  const ui = new Map<string, string>()
  press(PlansTab(stubEls, PLANS_FIXTURE, 80, ui), 'plans-filter-parked')
  expect(ui.get('plans:filter')).toBe('parked')
  const parked = allText(PlansTab(stubEls, PLANS_FIXTURE, 80, ui))
  expect(parked).toContain('hive-cards')
  expect(parked).not.toContain('checkout-v2')
  expect(parked).not.toContain('workflows-v2')
  press(PlansTab(stubEls, PLANS_FIXTURE, 80, ui), 'plans-filter-done')
  const done = allText(PlansTab(stubEls, PLANS_FIXTURE, 80, ui))
  expect(done).toContain('workflows-v2')
  expect(done).not.toContain('hive-cards')
})

test('Plans list: pressing a row sets plans:sel and plan:sub', () => {
  const ui = new Map<string, string>()
  const drawn = PlansTab(stubEls, PLANS_FIXTURE, 80, ui)
  // the plan name is part of the press target, not only the one-cell ›
  const btn = nodesOf(drawn, 'Button').find(x => String(x.props?.key) === 'plans-detail-settings-plans-db')
  expect(String(btn?.props?.label)).toBe('settings-plans-db ›')
  press(drawn, 'plans-detail-settings-plans-db')
  expect(ui.get('plans:sel')).toBe('settings-plans-db')
  expect(ui.get('plan:sub')).toBe('detail')
})

test('Plans list: a running plan with no live run says so', () => {
  const text = allText(PlansTab(stubEls, PLANS_FIXTURE, 80, new Map()))
  expect(text).toContain('running (no live run)')
  expect(text).toContain('running · r1')
})

test('Plans list: Delete confirm names the pre-delete backup and the kept folder', () => {
  const ui = new Map<string, string>([['plans:filter', 'archived']])
  const view: PlansView = { ...viewWith(() => undefined), plans: ARCHIVED_VIEW.plans.map(p => (p.id === 'old-plan' ? { ...p, project: 'matrixkoder' } : p)) }
  press(PlansTab(stubEls, view, 300, ui), 'plans-delete-old-plan')
  const confirm = allText(PlansTab(stubEls, view, 300, ui))
  expect(confirm).toContain('~/.matrixkoder/projects/matrixkoder/archive/old-plan-pre-delete-<ts>.db')
  expect(confirm).toContain('~/.matrixkoder/projects/matrixkoder/archive/plans/old-plan/')
})

test('Plans list: Archive and Unarchive call onAction; the archived filter lists them, all and the count do not', () => {
  const calls: string[] = []
  const view: PlansView = { ...ARCHIVED_VIEW, onAction: (k, id) => { calls.push(`${k}:${id}`) } }
  const ui = new Map<string, string>()
  const all = PlansTab(stubEls, view, 80, ui)
  press(all, 'plans-archive-workflows-v2')
  expect(allText(all)).not.toContain('old-plan')
  const seg = nodesOf(all, 'Button').find(x => String(x.props?.key) === 'plans-filter-all')
  expect(String(seg?.props?.label)).toBe(`all ${PLANS_FIXTURE.plans.length}`)
  expect(allText(all)).toContain(`source overview · ${PLANS_FIXTURE.plans.length} plans`)
  press(all, 'plans-filter-archived')
  expect(ui.get('plans:filter')).toBe('archived')
  const arch = PlansTab(stubEls, view, 80, ui)
  expect(allText(arch)).toContain('old-plan')
  expect(allText(arch)).not.toContain('checkout-v2')
  press(arch, 'plans-unarchive-old-plan')
  expect(calls).toEqual(['archive:workflows-v2', 'unarchive:old-plan'])
})

test('Plans list: the narrow layout at 36 cols still renders', () => {
  const drawn = PlansTab(stubEls, PLANS_FIXTURE, 36, new Map())
  const text = allText(drawn)
  expect(text).toContain('ONGOING RUNS')
  expect(text).toContain('settings-plans-db')
  expect(nodesOf(drawn, 'Button').length).toBeGreaterThan(0)
})
