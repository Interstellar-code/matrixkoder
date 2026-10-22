// New-bug form tests (plan bugs-tab, UI.4): kind and severity selection, the
// inputs only when the element table has Input, the task list, the save target,
// the submit callbacks and the `mk-bug new` argv.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { BUG_KINDS, BUG_SEVERITIES, BugNewForm, bugDraftOf, bugNewArgv } from './bugnew'
import type { BugDraft } from './bugnew'

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
const base = {
  Box: (props: BoxProps & ElementChildren): RenderElement => ({ type: 'Box', props: scalarProps(props), children: kids(props.children) }),
  Text: (props: TextProps & ElementChildren): RenderElement => ({ type: 'Text', props: scalarProps(props), children: kids(props.children) }),
  Button: (props: Record<string, unknown>) => ({ type: 'Button', props, children: [] }),
  Select: inert, Link: inert, Code: inert, Markdown: inert, Client: inert, Raster: inert, Image: inert,
}
const withInput = { ...base, Input: (props: Record<string, unknown>) => ({ type: 'Input', props, children: [] }) } as unknown as Elements[keyof Elements]
const noInput = base as unknown as Elements[keyof Elements]

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
const press = (drawn: unknown, key: string): void => {
  const b = button(drawn, key)
  expect(b, `button ${key}`).toBeDefined()
  ;(b?.props?.onPress as () => void)()
}

const DRAFT: BugDraft = {
  nextId: 'B7', project: 'matrixkoder', title: 'Plans list is stale', kind: 'frontend', severity: 'high',
  found_in: 'v0.7.5', description: 'it stays after a merge', tasks: ['drop the run', 'add a test'], task: '', target: 'store',
}
function draw(draft: BugDraft = DRAFT, els = withInput, cols = 46) {
  const map = new Map<string, string>()
  const acts: string[] = []
  const ui = { get: (k: string) => map.get(k), set: (k: string, v: string) => { map.set(k, v) }, bugAct: (kind: 'create' | 'save-draft') => { acts.push(kind) } }
  return { tree: BugNewForm(els, draft, cols, ui), map, acts }
}

test('the fixed lists', () => {
  expect([...BUG_KINDS]).toEqual(['frontend', 'backend', 'cli', 'store', 'infra', 'docs', 'other'])
  expect([...BUG_SEVERITIES]).toEqual(['high', 'med', 'low'])
})

test('header, kind and severity chips: the draft picks one of each; a press writes bugs:new:*', () => {
  const d = draw()
  const text = allText(d.tree)
  for (const s of ['← Bugs', 'New bug', 'B7', 'matrixkoder', 'DESCRIPTION', 'TASKS', 'SAVE TO']) expect(text, s).toContain(s)
  for (const k of BUG_KINDS) expect(button(d.tree, `bug-new-kind-${k}`), k).toBeDefined()
  for (const s of BUG_SEVERITIES) expect(button(d.tree, `bug-new-sev-${s}`), s).toBeDefined()
  // exactly one kind and one severity are drawn "on" (a tinted box)
  const on = nodesOf(d.tree, 'Box').filter(b => /^bug-new-(kind|sev)-.*-box$/.test(String(b.props?.key)) && b.props?.backgroundColor !== undefined)
  const tinted = (id: string): unknown => on.find(b => b.props?.key === id)?.props?.backgroundColor
  expect(tinted('bug-new-kind-frontend-box')).toBeDefined()
  expect(tinted('bug-new-kind-cli-box')).not.toBe(tinted('bug-new-kind-frontend-box'))
  expect(tinted('bug-new-sev-high-box')).not.toBe(tinted('bug-new-sev-low-box'))
  press(d.tree, 'bug-new-kind-cli')
  press(d.tree, 'bug-new-sev-low')
  expect(d.map.get('bugs:new:kind')).toBe('cli')
  expect(d.map.get('bugs:new:severity')).toBe('low')
  press(d.tree, 'bug-new-back')
  expect(d.map.get('bugs:sel')).toBe('')
  // `esc cancel` is a pressable button (the pane arms no raw Esc) that leaves the form
  const esc = draw()
  expect(button(esc.tree, 'bug-new-esc')?.props?.label).toBe('esc cancel')
  expect(allText(esc.tree)).not.toContain('↑↓')
  press(esc.tree, 'bug-new-esc')
  expect(esc.map.get('bugs:sel')).toBe('')
})

test('inputs are drawn only when the element table has Input', () => {
  const d = draw(DRAFT, withInput)
  const inputs = nodesOf(d.tree, 'Input')
  expect(inputs.map(i => i.props?.key)).toEqual(['bug-new-in-title', 'bug-new-in-found_in', 'bug-new-in-description', 'bug-new-in-task'])
  ;(inputs[0]?.props?.onInput as (v: string) => void)('new title')
  expect(d.map.get('bugs:new:title')).toBe('new title')
  // + task: submit appends a line and clears the pending text
  ;(inputs[3]?.props?.onSubmit as (v: string) => void)('third task')
  expect(d.map.get('bugs:new:tasks')).toBe('drop the run\nadd a test\nthird task')
  expect(d.map.get('bugs:new:task')).toBe('')
  ;(inputs[3]?.props?.onSubmit as (v: string) => void)('   ')
  expect(d.map.get('bugs:new:tasks')).toBe('drop the run\nadd a test\nthird task')
  // the engine refuses an Input without onSubmit: every input has one; Enter in title / found in / description keeps the text and files the bug
  for (const i of inputs) expect(typeof i.props?.onSubmit, String(i.props?.key)).toBe('function')
  ;(inputs[1]?.props?.onSubmit as (v: string) => void)('v0.8.0')
  expect(d.map.get('bugs:new:found_in')).toBe('v0.8.0')
  expect(d.acts).toEqual(['create'])
  ;(inputs[0]?.props?.onSubmit as (v: string) => void)('another title')
  expect(d.map.get('bugs:new:title')).toBe('another title')
  expect(d.acts).toEqual(['create', 'create'])
  const empty = draw({ ...DRAFT, title: '' }, withInput)
  ;(nodesOf(empty.tree, 'Input')[2]?.props?.onSubmit as (v: string) => void)('text')
  ;(nodesOf(empty.tree, 'Input')[0]?.props?.onSubmit as (v: string) => void)('  ')
  expect(empty.acts, 'no title, no bug').toEqual([])
  const toDraft = draw({ ...DRAFT, target: 'draft' }, withInput)
  ;(nodesOf(toDraft.tree, 'Input')[2]?.props?.onSubmit as (v: string) => void)('why')
  expect(toDraft.acts).toEqual(['save-draft'])
  const plain = draw(DRAFT, noInput)
  expect(nodesOf(plain.tree, 'Input').length).toBe(0)
  expect(allText(plain.tree)).toContain('Plans list is stale')
})

test('the task list shows each task and a press removes it', () => {
  const d = draw()
  const text = allText(d.tree)
  expect(text).toContain('1.')
  expect(text).toContain('drop the run')
  expect(text).toContain('2.')
  press(d.tree, 'bug-new-task-x0')
  expect(d.map.get('bugs:new:tasks')).toBe('add a test')
  expect(allText(draw({ ...DRAFT, tasks: [] }).tree)).toContain('no tasks yet')
})

test('save to: store now submits create, draft submits save-draft; an empty title submits nothing', () => {
  const store = draw()
  expect(allText(store.tree)).toContain('+ Create B7')
  expect(allText(store.tree)).toContain('an event on the bus')
  press(store.tree, 'bug-new-create')
  expect(store.acts).toEqual(['create'])
  press(store.tree, 'bug-new-target-draft')
  expect(store.map.get('bugs:new:target')).toBe('draft')
  const draft = draw({ ...DRAFT, target: 'draft' })
  expect(allText(draft.tree)).toContain('+ Save draft')
  expect(allText(draft.tree)).toContain('projects/matrixkoder/bugs/B7.md')
  press(draft.tree, 'bug-new-save-draft')
  expect(draft.acts).toEqual(['save-draft'])
  const empty = draw({ ...DRAFT, title: '  ' })
  press(empty.tree, 'bug-new-create')
  expect(empty.acts).toEqual([])
})

test('bugDraftOf reads the view state with defaults', () => {
  const m = new Map<string, string>([['bugs:new:title', 'T'], ['bugs:new:kind', 'nope'], ['bugs:new:tasks', 'a\n\nb'], ['bugs:new:target', 'draft']])
  const d = bugDraftOf(k => m.get(k), 'B3', 'repo')
  expect(d).toMatchObject({ nextId: 'B3', project: 'repo', title: 'T', kind: 'other', severity: 'med', tasks: ['a', 'b'], target: 'draft' })
  expect(bugDraftOf(() => undefined, 'B1', 'r').target).toBe('store')
})

test('bugNewArgv: one --flag=<text> argument per filled field, text verbatim, empty fields skipped', () => {
  expect(bugNewArgv(DRAFT)).toEqual(['--title=Plans list is stale', '--kind=frontend', '--severity=high', '--found-in=v0.7.5', '--description=it stays after a merge'])
  const odd = bugNewArgv({ ...DRAFT, title: 'a "quoted" --kind=cli; $(x)', found_in: '', description: '  ' })
  expect(odd).toEqual(['--title=a "quoted" --kind=cli; $(x)', '--kind=frontend', '--severity=high'])
  expect(bugNewArgv({ ...DRAFT, title: '' }).some(a => a.startsWith('--title'))).toBe(false)
})
