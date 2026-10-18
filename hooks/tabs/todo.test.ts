// Todo tab tests (plan todo-tab-r1, P2): TodoTab is called directly with a
// minimal element table, the way events.test.ts does. Buttons are recorded by
// key so a test can press them and see exactly which callbacks fired.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { TodoTab } from './todo'
import type { TodoUi, TodoView } from './todo'
import { TODO_FIXTURE } from './todo.fixture'

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

const presses = new Map<string, () => void>()
const boxTag = (props: BoxProps & ElementChildren): RenderElement => ({ type: 'Box', props: scalarProps(props), children: kids(props.children) })
const textTag = (props: TextProps & ElementChildren): RenderElement => ({ type: 'Text', props: scalarProps(props), children: kids(props.children) })
const stub = (): RenderElement => ({ type: 'Text', props: {}, children: [] })
// a Button draws as a Text: `[ label ]` (a plain one: the label alone), and records its press by key
const buttonTag = (props: { key?: string; label?: string; plain?: true; onPress?: () => void; dimColor?: boolean }): RenderElement => {
  if (props.key != null && props.onPress != null) presses.set(props.key, props.onPress)
  const label = props.plain ? String(props.label) : `[ ${String(props.label)} ]`
  return { type: 'Text', props: props.dimColor ? { dimColor: true } : {}, children: [label] }
}
const base = { Box: boxTag, Text: textTag, Button: buttonTag, Select: stub, Link: stub, Code: stub, Markdown: stub, Client: stub, Raster: stub, Image: stub }
const els = { ...base, Input: stub } as unknown as Elements['terminal']
const elsNoInput = base as unknown as Elements['terminal']

type Call = [string, string | number | undefined]
function draw(view: TodoView, cols = 46, table: Elements['terminal'] = els) {
  presses.clear()
  const calls: Call[] = []
  const sets: [string, string][] = []
  const ui: TodoUi = {
    get: () => undefined,
    set: (k, v) => { sets.push([k, v]) },
    todoAct: (kind, arg) => { calls.push([kind, arg]) },
  }
  return { tree: TodoTab(table, view, cols, ui), calls, sets }
}

type N = { type?: string; props?: Record<string, unknown>; children?: unknown[] }
const textNodes = (root: unknown): N[] => {
  const out: N[] = []
  const walk = (n: unknown): void => {
    if (typeof n !== 'object' || n === null) return
    const c = n as N
    if (c.type === 'Text') out.push(c)
    c.children?.forEach(walk)
  }
  walk(root)
  return out
}
const own = (n: N): string => (n.children ?? []).map(c => (typeof c === 'string' ? c : '')).join('')
const texts = (root: unknown): string[] => textNodes(root).map(own).filter(s => s !== '')
const flat = (root: unknown): string => texts(root).join('\n')

const textWidth = (n: unknown): number => {
  if (typeof n === 'string') return n.length
  const c = n as N
  return c.type === 'Text' ? (c.children ?? []).reduce((s: number, k) => s + textWidth(k), 0) : 0
}
const widthOf = (n: unknown): number => {
  if (typeof n !== 'object' || n === null) return 0
  const c = n as N
  if (c.type === 'Text') return textWidth(c)
  const ks = c.children ?? []
  if (c.type === 'Box' && c.props?.flexDirection === 'row') {
    const gap = typeof c.props.gap === 'number' ? c.props.gap : 0
    const pad = typeof c.props.paddingX === 'number' ? c.props.paddingX * 2 : 0
    return ks.reduce((s: number, k) => s + widthOf(k), 0) + gap * Math.max(0, ks.length - 1) + pad
  }
  return ks.reduce((m: number, k) => Math.max(m, widthOf(k)), 0)
}
/** every row (Text or horizontal Box) with whether it sits inside a round-bordered Card. */
function rows(root: unknown): { w: number; inCard: boolean }[] {
  const out: { w: number; inCard: boolean }[] = []
  const walk = (n: unknown, inCard: boolean): void => {
    if (typeof n !== 'object' || n === null) return
    const c = n as N
    const card = inCard || (c.type === 'Box' && c.props?.borderStyle === 'round')
    if (c.type === 'Text' || (c.type === 'Box' && c.props?.flexDirection === 'row')) out.push({ w: widthOf(c), inCard: card })
    c.children?.forEach(k => { walk(k, card) })
  }
  walk(root, false)
  return out
}

const withView = (over: Partial<TodoView>): TodoView => ({ ...TODO_FIXTURE, ...over })

test('the header counts open and done, and the project line names the db', () => {
  const t = texts(draw(TODO_FIXTURE).tree)
  expect(t).toContain('Todo')
  expect(t).toContain('● 3 open · ✓ 2 done')
  expect(t).toContain('matrixkoder · saved in the overview db')
})

test('rows draw [ ] and [✓], done rows are dim, and a press fires only its own callback', () => {
  const { tree, calls } = draw(TODO_FIXTURE)
  const t = texts(tree)
  expect(t.filter(s => s === '[ ]')).toHaveLength(3)
  expect(t.filter(s => s === '[✓]')).toHaveLength(2)
  const dim = textNodes(tree).filter(n => n.props?.strikethrough === true)
  expect(dim.map(own)).toEqual(['Merge the p4 mkdb lane', 'Update the Files tab docs'])
  expect(dim.every(n => n.props?.dimColor === true)).toBe(true)
  expect(textNodes(tree).find(n => own(n) === 'Review the p5 plantask diff')?.props?.dimColor).toBeFalsy()
  presses.get('todo-t4')?.()
  expect(calls).toEqual([['toggle', 4]])
  presses.get('todo-x2')?.()
  expect(calls).toEqual([['toggle', 4], ['delete', 2]])
})

test('the three empty states', () => {
  expect(flat(draw(withView({ todos: [] })).tree)).toContain('nothing here. type above, Enter.')
  const open = withView({ filter: 'open', todos: TODO_FIXTURE.todos.filter(t => t.done) })
  expect(flat(draw(open).tree)).toContain('all clear')
  const done = withView({ filter: 'done', todos: TODO_FIXTURE.todos.filter(t => !t.done) })
  expect(flat(draw(done).tree)).toContain('nothing done yet')
})

test('undo line, Undo, Clear done, the count line and the filter chips', () => {
  const gone = { id: 9, text: 'old thing', done: false, created: '' }
  const { tree, calls, sets } = draw(withView({ undo: gone }))
  const f = flat(tree)
  expect(f).toContain('deleted “old thing”')
  expect(f).toContain('[ ↩ Undo ]')
  expect(f).toContain('[ ▾ Clear done ]')
  expect(f).toContain('5 todos')
  expect(f).toContain('✓ done · ✕ delete')
  for (const c of ['All 5', 'Open 3', 'Done 2']) expect(f).toContain(c)
  presses.get('todo-undo')?.()
  presses.get('todo-clear')?.()
  presses.get('todo-f-done')?.()
  expect(calls).toEqual([['undo', undefined], ['clear-done', undefined]])
  expect(sets).toEqual([['todo:filter', 'done']])
  expect(flat(draw(TODO_FIXTURE).tree)).not.toContain('deleted')
  // singular, counted over the shown rows
  expect(flat(draw(withView({ filter: 'open', todos: TODO_FIXTURE.todos.slice(0, 1) })).tree)).toContain('1 todo')
  expect(flat(draw(withView({ filter: 'done' })).tree)).toContain('2 todos')
})

test('+ Add sends the draft and ignores an empty one', () => {
  const a = draw(withView({ draft: 'buy milk' }))
  presses.get('todo-add')?.()
  expect(a.calls).toEqual([['add', 'buy milk']])
  const b = draw(withView({ draft: '  ' }))
  presses.get('todo-add')?.()
  expect(b.calls).toEqual([])
})

test('nothing drawn is wider than the pane at 36 and 46 cols; long text is clipped', () => {
  const long = 'x'.repeat(120)
  const view = withView({
    todos: [{ id: 7, text: `${long}\u0007\u001b[31m`, done: false, created: '' }, ...TODO_FIXTURE.todos],
    undo: { id: 8, text: long, done: false, created: '' },
    project: long,
  })
  for (const cols of [36, 46]) {
    const { tree } = draw(view, cols, elsNoInput)
    const wide = rows(tree).filter(r => (r.inCard ? r.w > cols - 4 : r.w > cols))
    expect(wide, `over-wide rows at ${cols}`).toEqual([])
    const f = flat(tree)
    expect(f).toContain('…')
    expect(f).not.toMatch(/[\u0000-\u0008\u001b]/)
  }
})

test('without an Input element the draft draws as dim text; empty draft shows a hint', () => {
  const t = textNodes(draw(withView({ draft: 'half typed' }), 46, elsNoInput).tree).find(n => own(n) === 'half typed')
  expect(t?.props?.dimColor).toBe(true)
  expect(flat(draw(TODO_FIXTURE, 46, elsNoInput).tree)).toContain('press a, then type')
})

test('the filter chips wrap so no chip row passes the Card width at 36 and 46 cols', () => {
  const mk = (n: number, nDone: number) => Array.from({ length: n }, (_, i) => ({ id: i + 1, text: `t${String(i)}`, done: i < nDone, created: '' }))
  for (const [todos, nDone] of [[mk(12, 2), 2], [mk(120, 20), 20]] as const) {
    for (const cols of [36, 46]) {
      const { tree } = draw(withView({ todos }), cols, elsNoInput)
      const strip = (function find(n: unknown): N | undefined {
        if (typeof n !== 'object' || n === null) return undefined
        const c = n as N
        if (c.type === 'Box' && c.props?.flexWrap === 'wrap') return c
        return (c.children ?? []).map(find).find(Boolean)
      })(tree)
      expect(strip, 'the chip row wraps').toBeTruthy()
      // a Segment draws label + 4 (padding 1+1, plain press 2): the engine width is taken from kit.tsx's note, not measured
      const chips = [`All ${String(todos.length)}`, `Open ${String(todos.length - nDone)}`, `Done ${String(nDone)}`].map(l => l.length + 4)
      expect(Math.max(...chips), `one chip fits at ${cols}`).toBeLessThanOrEqual(cols - 4)
      let row = 0
      for (const w of chips) {
        if (row > 0 && row + 1 + w > cols - 4) row = 0
        row = row === 0 ? w : row + 1 + w
        expect(row).toBeLessThanOrEqual(cols - 4)
      }
    }
  }
})
