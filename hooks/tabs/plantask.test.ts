// Task detail panel tests (plan settings-plans-db, V1.5): the pure
// PlanTaskPanel render called with a stub element table — every present
// field drawn, missing fields left out (no `null` text), owned capped at 8
// with `+n more`, the title wrapped not cut, the pressable `esc close`
// footer, and findPlanTask's lookup.
import { test, expect } from 'claude-code/testing'
import type { Elements } from 'claude-code'
import { PlanTaskPanel, findPlanTask } from './plantask'
import { planFromReply } from './plans-map'
import detailFixture from './fixtures/plan-detail'
import type { PlanTask } from './plan'

type N = { type: string; props: Record<string, unknown>; children: unknown[] }

const node = (v: unknown): N => v as N
const kids = (v: unknown): N[] => {
  const arr = (v as { children?: unknown[] }).children
  return Array.isArray(arr) ? (arr as unknown[]).map(node) : []
}
const text = (v: unknown): string => kids(v).map(c => (typeof c === 'string' ? c : text(c))).join('')

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
  return {
    Box: make('Box'),
    Text: make('Text'),
    Button: make('Button'),
  } as unknown as Elements[keyof Elements]
}

const allText = (drawn: unknown): string =>
  allOf(drawn, 'Text').map(t => text(t)).join('\n') + '\n' + allOf(drawn, 'Button').map(b => String(b.props.label ?? '')).join('\n')

function press(drawn: unknown, key: string): void {
  const b = allOf(drawn, 'Button').find(x => String(x.props.key) === key)
  expect(b, `button ${key}`).toBeDefined()
  ;(b?.props.onPress as () => void)()
}

/** A plain-text layout of the element tree (the plankanban report render's
 *  math): Box rows joined with their gap, columns stacked, a bordered Card
 *  padded one cell each side, Text wrapped at word edges to the width. */
function renderLines(v: unknown, width?: number): string[] {
  if (typeof v === 'string') return [v]
  if (typeof v !== 'object' || v === null) return []
  const cur = v as N
  if (cur.type === 'Text') {
    const s = text(cur)
    if (width == null || s.length <= width) return [s]
    const out: string[] = []
    let line = ''
    for (const word of s.split(' ')) {
      if (line === '') { line = word; continue }
      if (`${line} ${word}`.length > width) { out.push(line); line = word } else line = `${line} ${word}`
    }
    out.push(line)
    return out
  }
  if (cur.type === 'Button') return [String(cur.props.label ?? '')]
  const isBox = cur.type === 'Box'
  const row = isBox && cur.props.flexDirection === 'row'
  const bordered = isBox && cur.props.borderStyle === 'round'
  const own = typeof cur.props.width === 'number' ? cur.props.width : row ? undefined : width
  const childW = bordered && own != null ? own - 4 : own
  const blocks = kids(cur).map(k => renderLines(k, row ? undefined : childW))
  let inner: string[] = []
  if (row) {
    const gap = typeof cur.props.gap === 'number' ? cur.props.gap : 0
    const ws = blocks.map(b => Math.max(0, ...b.map(l => l.length)))
    const h = Math.max(0, ...blocks.map(b => b.length))
    for (let i = 0; i < h; i++) {
      inner.push(blocks.map((b, bi) => (b[i] ?? '').padEnd(ws[bi]!)).join(' '.repeat(gap)))
    }
  } else {
    const gap = typeof cur.props.gap === 'number' ? cur.props.gap : 0
    blocks.forEach((b, i) => {
      if (i > 0 && gap > 0) inner.push(...Array<string>(gap).fill(''))
      inner.push(...b)
    })
  }
  if (own != null && !bordered) inner = inner.map(l => (l.length > own ? l.slice(0, own) : l.padEnd(own, ' ')))
  if (bordered) {
    const iw = Math.max(0, ...inner.map(l => l.length))
    return [`╭${'─'.repeat(iw + 2)}╮`, ...inner.map(l => `│ ${l.padEnd(iw, ' ')} │`), `╰${'─'.repeat(iw + 2)}╯`]
  }
  return inner
}

const picture = (row: PlanTask, cols: number): string =>
  renderLines(PlanTaskPanel(stubEls(), row, cols), cols).map(l => l.replace(/\s+$/, '')).join('\n')

/** A row with every field the panel can draw. */
const FULL: PlanTask = {
  id: 'D1.5', title: 'Backfill route_json for dispatched tasks so cards and rows can show the lane and agent', status: 'building',
  right: 'building',
  type: 'task', round: 1, verdict: 'approve',
  slot: 5, pane: 'mk-5', branch: 'mk/settings-plans-db-r1/d1.5',
  updated: '2026-10-14T11:20:00+02:00', acceptance: 'plans --json carries route for every dispatched task',
  owned: ['D1.1', 'D1.2', 'D1.3', 'D1.4', 'V1.1', 'V1.2', 'V1.3', 'V1.4', 'S1.1', 'S1.2'],
  deps: ['D1.4'],
  route: { agent: 'opencode', model: 'opencode/space-bunny-free', effort: 'medium' },
  subs: [
    { id: 'D1.5a', title: 'write route at dispatch', status: 'done', right: 'done' },
    { id: 'D1.5b', title: 'backfill migration', status: 'building', right: 'building' },
  ],
}

test('the panel draws every present field', () => {
  const text80 = allText(PlanTaskPanel(stubEls(), FULL, 80))
  expect(text80).toContain('D1.5 · building') // the Card title
  expect(text80).toContain('Backfill route_json for dispatched tasks') // full title
  expect(text80).toContain('task')
  expect(text80).toContain('round') // the round kv row
  expect(text80).toContain('approve') // the verdict
  expect(text80).toContain('L5')
  expect(text80).toContain('mk-5')
  expect(text80).toContain('mk/settings-plans-db-r1/d1.5')
  expect(text80).toContain('opencode')
  expect(text80).toContain('opencode/space-bunny-free')
  expect(text80).toContain('medium')
  expect(text80).toContain('2026-10-14T11:20:00+02:00')
  expect(text80).toContain('D1.4') // deps
  expect(text80).toContain('acceptance')
  expect(text80).toContain('plans --json carries route for every dispatched task')
  expect(text80).toContain('subtasks')
  expect(text80).toContain('D1.5a write route at dispatch')
  expect(text80).toContain('esc close')
})

test('a missing field is left out — no null text', () => {
  const bare: PlanTask = { id: 'T9', title: 'Bare', status: 'planned', right: 'planned' }
  const t = allText(PlanTaskPanel(stubEls(), bare, 80))
  expect(t).toContain('T9 · planned')
  expect(t).toContain('Bare')
  expect(t).not.toContain('null')
  expect(t).not.toContain('undefined')
  expect(t).not.toContain('lane')
  expect(t).not.toContain('pane')
  expect(t).not.toContain('branch')
  expect(t).not.toContain('agent')
  expect(t).not.toContain('model')
  expect(t).not.toContain('effort')
  expect(t).not.toContain('updated')
  expect(t).not.toContain('deps')
  expect(t).not.toContain('owned')
  expect(t).not.toContain('acceptance')
  expect(t).not.toContain('subtasks')
  expect(t).toContain('esc close')
})

test('owned caps at 8 lines with +n more', () => {
  const t = allText(PlanTaskPanel(stubEls(), FULL, 80))
  // each owned item draws its own bullet line; 10 owned → 8 bullets + +2 more
  const bullets = t.split('\n').filter(l => l.trim() === '·')
  expect(bullets.length).toBe(8)
  expect(t).toContain('V1.4') // the 8th owned line shows
  expect(t).not.toContain('S1.1') // the 9th is capped away
  expect(t).not.toContain('S1.2') // the 10th is capped away
  expect(t).toContain('+2 more')
})

test('the title wraps, it is never cut', () => {
  const lines = picture(FULL, 36).split('\n')
  // the 100-char title occupies more than one line at 32 inner cols
  const titleLines = lines.filter(l => l.includes('Backfill') || l.includes('route_json') || l.includes('dispatched'))
  expect(titleLines.length).toBeGreaterThanOrEqual(2)
  expect(lines.join('\n')).not.toContain('Backfill route_json for dispatched tasks so cards and rows can show the lane and agent')
})

test('esc close is pressable and fires onClose', () => {
  let closed = 0
  const drawn = PlanTaskPanel(stubEls(), FULL, 80, () => { closed += 1 })
  press(drawn, 'plan-task-close-D1.5')
  expect(closed).toBe(1)
})

test('findPlanTask finds the row and rejects unknown or empty ids', () => {
  const phases = [{ id: 'P1', title: '', header: '', rows: [FULL] }]
  expect(findPlanTask(phases, 'D1.5')?.id).toBe('D1.5')
  expect(findPlanTask(phases, 'nope')).toBeNull()
  expect(findPlanTask(phases, '')).toBeNull()
  expect(findPlanTask(undefined, 'D1.5')).toBeNull()
})

test('report render: the D1.5 panel at 80 cols, exactly as pasted (V1.5)', () => {
  const row = findPlanTask(planFromReply(detailFixture).phases, 'D1.5')
  expect(row, 'the D1.5 phase-rooted task').not.toBeNull()
  expect(picture(row!, 80)).toBe(`╭──────────────────────────────────────────────────────────────────────────────╮
│ D1.5 · review                                                                │
│ Task fields survive the store mirror; dispatch records the route;            │
│ mandatory-field check                                                        │
│ lane L5                                                                      │
│ pane w1R:p1                                                                  │
│ branch mk/settings-plans-db-r1/d1.5                                          │
│ agent opencode                                                               │
│ model opencode/space-bunny-free                                              │
│ effort medium                                                                │
│ updated 2026-10-14T12:11:36+02:00                                            │
│ owned                                                                        │
│ · scripts/mk-state.py                                                        │
│ · scripts/mkdb.py                                                            │
│ · scripts/mk-lane-event.py                                                   │
│ · skills/matrixkoder/SKILL.md                                                │
│ subtasks                                                                     │
│ ◐ D1.5.1 run init --plan copies acceptance, refs, owned, deps, type, route i │
│ ◐ D1.5.2 store mirror never blanks acceptance/owned/deps/route_json/type the │
│ ◐ D1.5.3 dispatch records agent, model, effort on the task  review           │
│ ◐ D1.5.4 mk-state task check: mandatory fields; dispatch refuses a task that │
│ ◐ D1.5.5 backfill settings-plans-db-r1 task fields from the plan  review     │
│ ◐ D1.5.6 SKILL.md: task round and route recorded at dispatch  review         │
│ esc close                                                                    │
╰──────────────────────────────────────────────────────────────────────────────╯`)
})

test('report render: the D1.5 panel at 36 cols, exactly as pasted (V1.5)', () => {
  const row = findPlanTask(planFromReply(detailFixture).phases, 'D1.5')
  expect(row, 'the D1.5 phase-rooted task').not.toBeNull()
  expect(picture(row!, 36)).toBe(`╭──────────────────────────────────╮
│ D1.5 · review                    │
│ Task fields survive the store    │
│ mirror; dispatch records the     │
│ route; mandatory-field check     │
│ lane L5                          │
│ pane w1R:p1                      │
│ branch mk/settings-plans-db-r1/d │
│ agent opencode                   │
│ model opencode/space-bunny-free  │
│ effort medium                    │
│ updated 2026-10-14T12:11:36+02:0 │
│ owned                            │
│ · scripts/mk-state.py            │
│ · scripts/mkdb.py                │
│ · scripts/mk-lane-event.py       │
│ · skills/matrixkoder/SKILL.md    │
│ subtasks                         │
│ ◐ D1.5.1 run init --plan copies  │
│ ◐ D1.5.2 store mirror never blan │
│ ◐ D1.5.3 dispatch records agent, │
│ ◐ D1.5.4 mk-state task check: ma │
│ ◐ D1.5.5 backfill settings-plans │
│ ◐ D1.5.6 SKILL.md: task round an │
│ esc close                        │
╰──────────────────────────────────╯`)
})
