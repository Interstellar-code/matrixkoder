// Real-code tests for the Plan Detail sub-tab (settings-plans-db, V1.2): the
// pure PlanTab render. Default every phase is folded, one phase expands, Expand
// all / Collapse all, the word "wave" is absent, the header card carries its
// fields, and 36 cols still renders.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { PlanTab } from './plan'
import { PLAN_REAL_FIXTURE } from './plan.fixture'

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

const allText = (n: unknown): string =>
  nodesOf(n, 'Text').map(t => (t.children ?? []).map(c => (typeof c === 'string' ? c : '')).join('')).join('\n')
  + '\n' + nodesOf(n, 'Button').map(b => String(b.props?.label ?? '')).join('\n')

function press(drawn: unknown, key: string): void {
  const b = nodesOf(drawn, 'Button').find(x => String(x.props?.key) === key)
  expect(b, `button ${key}`).toBeDefined()
  ;(b?.props?.onPress as () => void)()
}

test('Plan detail: default all phases folded', () => {
  const ui = new Map<string, string>()
  const text = allText(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, ui))
  expect(text).toContain('R1 · Self-tests never write a real DB')
  expect(text).toContain('V1 · Plan tab sub-tabs')
  // a folded phase does not draw its tasks
  expect(text).not.toContain('R1.1')
  expect(text).not.toContain('D1.2')
  expect(text).not.toContain('V1.3')
  expect(text.toLowerCase()).not.toContain('wave')
})

test('Plan detail: expanding one phase shows only its tasks', () => {
  const ui = new Map<string, string>()
  press(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, ui), 'plan-fold-D1')
  expect(ui.get('plan:phase:D1')).toBe('1')
  const text = allText(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, ui))
  expect(text).toContain('D1.1')
  expect(text).toContain('D1.2')
  expect(text).toContain('import, delete, pause')
  expect(text).not.toContain('R1.1')
  expect(text).not.toContain('S1.2')
})

test('Plan detail: Expand all then Collapse all', () => {
  const ui = new Map<string, string>()
  press(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, ui), 'plan-expand-all')
  for (const id of ['R1', 'T1', 'D1', 'S1', 'V1']) expect(ui.get(`plan:phase:${id}`)).toBe('1')
  const open = allText(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, ui))
  expect(open).toContain('R1.3')
  expect(open).toContain('T1.2')
  expect(open).toContain('V1.3')
  expect(open).toContain('fold state per phase')
  press(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, ui), 'plan-collapse-all')
  for (const id of ['R1', 'T1', 'D1', 'S1', 'V1']) expect(ui.get(`plan:phase:${id}`)).toBe('')
  const shut = allText(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, ui))
  expect(shut).not.toContain('R1.3')
  expect(shut).not.toContain('V1.3')
})

test('Plan detail: the word wave is nowhere; shared phases show a with chip', () => {
  const ui = new Map<string, string>()
  press(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, ui), 'plan-expand-all')
  const text = allText(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, ui))
  expect(text.toLowerCase()).not.toContain('wave')
  expect(text).toContain('with T1')
  expect(text).toContain('with R1')
})

test('Plan detail: the header card carries its fields', () => {
  const text = allText(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, new Map()))
  expect(text).toContain('MOCK · fixture data · not this run')
  expect(text).toContain('settings-plans-db')
  expect(text).toContain('Settings mapping, plans in the DB, Plan sub-tabs')
  expect(text).toContain('2026-10-13')
  expect(text).toContain('settings-plans-db-r1')
  expect(text).toContain('main')
  expect(text).toContain('settings-tab')
  expect(text).toContain('14 tasks · 5 phases')
  expect(text).toContain('PHASES')
})

test('Plan detail: the narrow layout at 36 cols renders folded', () => {
  const drawn = PlanTab(stubEls, PLAN_REAL_FIXTURE, 36, new Map())
  const text = allText(drawn)
  expect(text).toContain('PHASES')
  expect(text).toContain('settings-plans-db')
  expect(text).not.toContain('D1.1')
  expect(text.toLowerCase()).not.toContain('wave')
  expect(nodesOf(drawn, 'Button').length).toBeGreaterThan(0)
})
