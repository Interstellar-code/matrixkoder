// Plan tab Kanban sub-tab tests (plan settings-plans-db, V1.4, contract C15):
// the render called directly with a stub element table so the assertions read
// the elements PlanKanbanTab actually built — four columns with counts from the
// captured plan-detail fixture, the status→column map, the phase filter row,
// card lines (glyph, id, cut title, phase chip, age), the empty column's
// `none`, the narrow stack, the footer, and none of the old KanbanTab chrome.
import { test, expect } from 'claude-code/testing'
import type { Elements } from 'claude-code'
import {
  PlanKanbanTab,
  PLAN_KANBAN_COLUMNS,
  STATUS_TO_COLUMN,
  columnOfTask,
  planKanbanView,
  planKanbanSideBySide,
  verdictNeedsChanges,
} from './plankanban'
import detail from './fixtures/plan-detail'

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

const uiOf = (init?: Map<string, string>) => {
  const ui = init ?? new Map<string, string>()
  return { ui, handle: { get: (k: string) => ui.get(k), set: (k: string, v: string) => { ui.set(k, v) } } }
}

/** the four column blocks: a Box column whose direct child is the Section Box
 *  (a row holding the bold heading Text). */
function columnBlock(root: unknown, name: string): N | undefined {
  return allOf(root, 'Box').find(box => kids(box).some(
    k => k.type === 'Box' && kids(k).some(g => g.type === 'Text' && g.props.bold === true && text(g) === name),
  ))
}

/** a column block's count: the bare-number dim Text under its heading. */
const countOf = (block: N | undefined): string => {
  const dim = allOf(block, 'Text').find(t => t.props.dimColor === true && /^\d+$/.test(text(t)))
  return dim == null ? '' : text(dim)
}

/** every status the captured fixture's tasks carry, for the map test. */
const FIXTURE_IDS = detail.phases.flatMap(p => p.tasks.map(t => t.id))

const view = planKanbanView(detail)
const W80 = 80

test('planKanbanView maps the captured detail: 14 cards, 5 phases in plan order, no unknown statuses', () => {
  expect(view.planId).toBe('settings-plans-db')
  expect(view.phases).toEqual(['R1', 'T1', 'D1', 'S1', 'V1'])
  expect(view.cards).toHaveLength(14)
  expect(view.unknownStatuses).toEqual([])
  // verdict survives the mapping (the NEEDS YOU rule reads it)
  expect(view.cards.find(c => c.id === 'R1.1')?.verdict).toBe('approve')
  expect(view.cards.find(c => c.id === 'V1.3')?.status).toBe('building')
  // no `updated` in the plans reply → no age
  expect(view.cards.every(c => c.age === '')).toBe(true)
})

test('the status→column map: every status named by C15, unknown falls to PLANNED, changes-verdict wins', () => {
  expect(STATUS_TO_COLUMN).toEqual({
    planned: 'PLANNED', routed: 'PLANNED', ready: 'PLANNED',
    dispatched: 'BUILDING', building: 'BUILDING', verifying: 'BUILDING', review: 'BUILDING', fix: 'BUILDING',
    blocked: 'NEEDS YOU', stalled: 'NEEDS YOU', quota: 'NEEDS YOU', provider: 'NEEDS YOU', qa: 'NEEDS YOU',
    merged: 'ON MAIN', done: 'ON MAIN',
  })
  for (const [status, col] of Object.entries(STATUS_TO_COLUMN)) {
    expect(columnOfTask({ id: 'X', title: '', status, phase: 'P', verdict: null, age: '' }), status).toBe(col)
  }
  expect(columnOfTask({ id: 'X', title: '', status: 'martian', phase: 'P', verdict: null, age: '' })).toBe('PLANNED')
  expect(verdictNeedsChanges('approve')).toBe(false)
  expect(verdictNeedsChanges(null)).toBe(false)
  expect(verdictNeedsChanges('changes')).toBe(true)
  expect(columnOfTask({ id: 'X', title: '', status: 'review', phase: 'P', verdict: 'changes', age: '' })).toBe('NEEDS YOU')
  // qa is the user's gate: NEEDS YOU
  expect(columnOfTask({ id: 'X', title: '', status: 'qa', phase: 'P', verdict: null, age: '' })).toBe('NEEDS YOU')
})

test('four headings with the right counts: PLANNED 3 · BUILDING 1 · NEEDS YOU 0 · ON MAIN 10', () => {
  const tree = PlanKanbanTab(stubEls(), view, W80, uiOf().handle)
  const heads = allOf(tree, 'Text').filter(t => t.props.bold === true).map(t => text(t))
  expect(heads).toEqual([...PLAN_KANBAN_COLUMNS])
  const counts = PLAN_KANBAN_COLUMNS.map(col => countOf(columnBlock(tree, col)))
  expect(counts).toEqual(['3', '1', '0', '10'])
  // every fixture task id is drawn exactly once across the four columns
  const all = text(tree)
  for (const id of FIXTURE_IDS) expect(all.includes(id), id).toBe(true)
})

test('a card shows glyph, id, cut title and phase chip (V1.3 building, R1.1 merged)', () => {
  const tree = PlanKanbanTab(stubEls(), view, W80, uiOf().handle)
  const building = columnBlock(tree, 'BUILDING')
  expect(text(building)).toContain('●V1.3') // glyph and id are sibling Texts; the row's gap is layout, not text
  // 80 cols → each column 19 wide → card inner 15: the long title is cut, not wrapped
  expect(text(building)).toContain('Kanban sub-tab…')
  expect(text(building)).not.toContain('sub-tab wiring in dashboard.tsx')
  expect(text(building)).toContain(' V1 ')
  const onMain = columnBlock(tree, 'ON MAIN')
  expect(text(onMain)).toContain('✓R1.1')
  expect(text(onMain)).toContain('One home rule')
  expect(text(onMain)).not.toContain('$MK_HOME') // R1.1's long title is cut, not wrapped
  expect(text(onMain)).toContain(' R1 ')
  // no `updated` in the data → no age text
  expect(text(tree)).not.toMatch(/\b\d+[mhd]\b/)
})

test('empty column shows a dim none', () => {
  const tree = PlanKanbanTab(stubEls(), view, W80, uiOf().handle)
  const needsYou = columnBlock(tree, 'NEEDS YOU')
  const none = kids(needsYou).find(k => k.type === 'Text' && text(k) === 'none')
  expect(none, 'dim none line').toBeDefined()
  expect((none as N | undefined)?.props.dimColor).toBe(true)
})

test('phase filter narrows the cards; all phases restores them', () => {
  const { ui, handle } = uiOf()
  const tree = PlanKanbanTab(stubEls(), view, W80, handle)
  // the S1 segment press sets plankanban:phase
  const seg = allOf(tree, 'Button').find(b => String(b.props.key) === 'plankanban-phase-S1')
  expect(seg, 'S1 segment').toBeDefined()
  ;(seg?.props.onPress as () => void)()
  expect(ui.get('plankanban:phase')).toBe('S1')
  const narrowed = PlanKanbanTab(stubEls(), view, W80, handle)
  const all = text(narrowed)
  expect(all).toContain('S1.1')
  expect(all).toContain('S1.2')
  expect(all).not.toContain('R1.1')
  expect(all).not.toContain('V1.3')
  const counts = PLAN_KANBAN_COLUMNS.map(col => countOf(columnBlock(narrowed, col)))
  expect(counts).toEqual(['2', '0', '0', '0'])
  // all phases restores
  const allSeg = allOf(narrowed, 'Button').find(b => String(b.props.key) === 'plankanban-phase-all')
  ;(allSeg?.props.onPress as () => void)()
  expect(ui.get('plankanban:phase')).toBe('')
  const restored = PlanKanbanTab(stubEls(), view, W80, handle)
  expect(text(restored)).toContain('R1.1')
  expect(text(restored)).toContain('V1.3')
})

test('the phase row keeps the dim k = all runs hint and Segments for all phases + each phase id', () => {
  const tree = PlanKanbanTab(stubEls(), view, W80, uiOf().handle)
  const labels = allOf(tree, 'Button').map(b => String(b.props.label ?? ''))
  expect(labels).toContain('all phases')
  for (const p of view.phases) expect(labels).toContain(p)
  expect(text(tree)).toContain('k = all runs')
})

test('narrow (36 cols) stacks the columns; wide keeps them side by side', () => {
  expect(planKanbanSideBySide(80)).toBe(true)
  expect(planKanbanSideBySide(36)).toBe(false)
  expect(planKanbanSideBySide(59)).toBe(true)
  expect(planKanbanSideBySide(58)).toBe(false)
  const narrow = PlanKanbanTab(stubEls(), view, 36, uiOf().handle)
  // the four column blocks hang off one column-direction Box (stacked)
  const parent = allOf(narrow, 'Box').find(b => kids(b).includes(columnBlock(narrow, 'PLANNED') as N) && kids(b).includes(columnBlock(narrow, 'ON MAIN') as N))
  expect(parent, 'one stacked parent').toBeDefined()
  expect(parent?.props.flexDirection).toBe('column')
  for (const col of PLAN_KANBAN_COLUMNS) {
    const block = columnBlock(narrow, col)
    expect(block, `narrow heading ${col}`).toBeDefined()
  }
  expect(text(columnBlock(narrow, 'BUILDING'))).toContain('●V1.3')
  // wide: the same parent is a row
  const wide = PlanKanbanTab(stubEls(), view, W80, uiOf().handle)
  const wideParent = allOf(wide, 'Box').find(b => kids(b).includes(columnBlock(wide, 'PLANNED') as N) && kids(b).includes(columnBlock(wide, 'ON MAIN') as N))
  expect(wideParent?.props.flexDirection).toBe('row')
})

test('footer: <n> tasks · this plan only · source mk.db, following the filter', () => {
  const tree = PlanKanbanTab(stubEls(), view, W80, uiOf().handle)
  expect(text(tree)).toContain('14 tasks · this plan only · source mk.db')
  const { handle } = uiOf(new Map([['plankanban:phase', 'S1']]))
  const narrowed = PlanKanbanTab(stubEls(), view, W80, handle)
  expect(text(narrowed)).toContain('2 tasks · this plan only · source mk.db')
})

test('no KanbanTab chrome: no Needs you off line, no Backlog/Ready/Verify headings', () => {
  const tree = PlanKanbanTab(stubEls(), view, W80, uiOf().handle)
  const all = text(tree)
  expect(all).not.toContain('Needs you off')
  const heads = allOf(tree, 'Text').filter(t => t.props.bold === true).map(t => text(t))
  expect(heads).toEqual([...PLAN_KANBAN_COLUMNS])
  expect(all).not.toContain('Backlog')
  expect(all).not.toContain('Ready')
  expect(all).not.toContain('Verify')
})

test('an unknown status falls to PLANNED and is listed in the view', () => {
  const odd = planKanbanView({
    ok: true, id: 'odd', phases: [{ id: 'P1', tasks: [{ id: 'P1.1', title: 't', status: 'martian' }] }],
  })
  expect(odd.unknownStatuses).toEqual(['martian'])
  const tree = PlanKanbanTab(stubEls(), odd, W80, uiOf().handle)
  expect(text(columnBlock(tree, 'PLANNED'))).toContain('P1.1')
})

test('a task with updated data shows its age (22m, 8h, 2d) and a changes verdict lands in NEEDS YOU', () => {
  const now = '2026-10-14T12:00:00+02:00'
  const aged = planKanbanView({
    ok: true, id: 'aged', phases: [{ id: 'P1', tasks: [
      { id: 'P1.1', title: 'fresh', status: 'building', updated: '2026-10-14T11:38:00+02:00' },
      { id: 'P1.2', title: 'hours', status: 'building', updated: '2026-10-14T04:00:00+02:00' },
      { id: 'P1.3', title: 'days', status: 'building', updated: '2026-10-12T04:00:00+02:00' },
      { id: 'P1.4', title: 'changes wanted', status: 'review', verdict: 'changes' },
    ] }],
  }, now)
  const byId = new Map(aged.cards.map(c => [c.id, c]))
  expect(byId.get('P1.1')?.age).toBe('22m')
  expect(byId.get('P1.2')?.age).toBe('8h')
  expect(byId.get('P1.3')?.age).toBe('2d')
  const tree = PlanKanbanTab(stubEls(), aged, W80, uiOf().handle)
  const needsYou = text(columnBlock(tree, 'NEEDS YOU'))
  expect(needsYou).toContain('▲P1.4')
  const building = text(columnBlock(tree, 'BUILDING'))
  expect(building).toContain('22m')
  expect(building).toContain('8h')
  expect(building).toContain('2d')
})

/** ── the report render (spec "Real inputs") ─────────────────────────────
 *  A plain-text layout of the element tree with the tab's own width math
 *  (Box rows joined with their gap and space-between, columns stacked with
 *  their gap, a bordered Card padded one cell each side) — structure only,
 *  no engine colours. The exact 80- and 36-col pictures are pinned below so
 *  the report paste is what the suite verified. */
function renderLines(v: unknown, width?: number): string[] {
  if (typeof v === 'string') return [v]
  if (typeof v !== 'object' || v === null) return []
  const cur = v as N
  if (cur.type === 'Text') {
    const s = text(cur)
    if (width == null || s.length <= width) return [s]
    const out: string[] = [] // the engine wraps prose at word edges
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
  const between = isBox && cur.props.justifyContent === 'space-between'
  const bordered = isBox && cur.props.borderStyle === 'round'
  // a space-between row and a column stretch to the width they got; a plain
  // row hugs its content (the engine's flex rules the tab relies on)
  const own = typeof cur.props.width === 'number'
    ? cur.props.width
    : row && !between ? undefined : width
  const childW = bordered && own != null ? own - 4 : own
  const blocks = kids(cur).map(k => renderLines(k, row && !between ? undefined : childW))
  let inner: string[] = []
  if (row) {
    const gap = typeof cur.props.gap === 'number' ? cur.props.gap : 0
    const ws = blocks.map(b => Math.max(0, ...b.map(l => l.length)))
    const h = Math.max(0, ...blocks.map(b => b.length))
    if (between && own != null && blocks.length === 2) {
      const a = blocks[0] ?? []
      const b = blocks[1] ?? []
      const bw = ws[1] ?? 0
      const aw = ws[0] ?? 0
      const room = own - bw - gap
      if (aw <= room) {
        for (let i = 0; i < h; i++) inner.push((a[i] ?? '').padEnd(room, ' ') + ' '.repeat(gap) + (b[i] ?? ''))
      } else { // it wraps: the right block lands below, right-aligned
        inner.push(...a)
        for (const l of b) inner.push(' '.repeat(Math.max(0, own - l.length)) + l)
      }
    } else {
      for (let i = 0; i < h; i++) {
        inner.push(blocks.map((b, bi) => (b[i] ?? ' '.repeat(ws[bi]!)).padEnd(ws[bi]!, ' ')).join(' '.repeat(gap)))
      }
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
    return [`\u256d${'\u2500'.repeat(iw + 2)}\u256e`, ...inner.map(l => `\u2502 ${l.padEnd(iw, ' ')} \u2502`), `\u2570${'\u2500'.repeat(iw + 2)}\u256f`]
  }
  return inner
}

const picture = (cols: number): string =>
  renderLines(PlanKanbanTab(stubEls(), view, cols, uiOf().handle), cols).map(l => l.replace(/\s+$/, '')).join('\n')

test('report render: the captured fixture at 80 cols, exactly as pasted', () => {
  expect(picture(80)).toBe(`all phases R1 T1 D1 S1 V1                                           k = all runs

PLANNED           3 BUILDING          1 NEEDS YOU         0 ON MAIN          10

╭─────────────────╮ ╭─────────────────╮ none                ╭─────────────────╮
│ ○ D1.4          │ │ ● V1.3          │                     │ ✓ R1.1          │
│ First real imp… │ │ Kanban sub-tab… │                     │ One home rule:… │
│  D1             │ │  V1             │                     │  R1             │
╰─────────────────╯ ╰─────────────────╯                     ╰─────────────────╯

╭─────────────────╮                                         ╭─────────────────╮
│ ○ S1.1          │                                         │ ✓ R1.2          │
│ Scope-home map… │                                         │ Write guard: a… │
│  S1             │                                         │  R1             │
╰─────────────────╯                                         ╰─────────────────╯

╭─────────────────╮                                         ╭─────────────────╮
│ ○ S1.2          │                                         │ ✓ R1.3          │
│ Dead keys and … │                                         │ Register matri… │
│  S1             │                                         │  R1             │
╰─────────────────╯                                         ╰─────────────────╯

                                                            ╭─────────────────╮
                                                            │ ✓ T1.1          │
                                                            │ templates/plan… │
                                                            │  T1             │
                                                            ╰─────────────────╯

                                                            ╭─────────────────╮
                                                            │ ✓ T1.2          │
                                                            │ SKILL.md stage… │
                                                            │  T1             │
                                                            ╰─────────────────╯

                                                            ╭─────────────────╮
                                                            │ ✓ D1.1          │
                                                            │ mkdb.py: plans… │
                                                            │  D1             │
                                                            ╰─────────────────╯

                                                            ╭─────────────────╮
                                                            │ ✓ D1.2          │
                                                            │ mk-plan.py: wr… │
                                                            │  D1             │
                                                            ╰─────────────────╯

                                                            ╭─────────────────╮
                                                            │ ✓ D1.3          │
                                                            │ docs/state-sch… │
                                                            │  D1             │
                                                            ╰─────────────────╯

                                                            ╭─────────────────╮
                                                            │ ✓ V1.1          │
                                                            │ List sub-tab: … │
                                                            │  V1             │
                                                            ╰─────────────────╯

                                                            ╭─────────────────╮
                                                            │ ✓ V1.2          │
                                                            │ Detail sub-tab… │
                                                            │  V1             │
                                                            ╰─────────────────╯

14 tasks · this plan only · source mk.db`)
})

test('report render: the captured fixture at 36 cols, exactly as pasted', () => {
  expect(picture(36)).toBe(`all phases R1 T1 D1 S1 V1
                        k = all runs

PLANNED                            3

╭──────────────────────────────────╮
│ ○ D1.4                           │
│ First real import: settings-pla… │
│  D1                              │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ○ S1.1                           │
│ Scope-home map: every SCHEMA ke… │
│  S1                              │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ○ S1.2                           │
│ Dead keys and fixture drift: dr… │
│  S1                              │
╰──────────────────────────────────╯

BUILDING                           1

╭──────────────────────────────────╮
│ ● V1.3                           │
│ Kanban sub-tab: the existing Ka… │
│  V1                              │
╰──────────────────────────────────╯

NEEDS YOU                          0

none

ON MAIN                           10

╭──────────────────────────────────╮
│ ✓ R1.1                           │
│ One home rule: mkdb.py ($MK_HOM… │
│  R1                              │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ✓ R1.2                           │
│ Write guard: a process started … │
│  R1                              │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ✓ R1.3                           │
│ Register matrixkoder in the roo… │
│  R1                              │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ✓ T1.1                           │
│ templates/plan.md: each header … │
│  T1                              │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ✓ T1.2                           │
│ SKILL.md stage 1 and docs/state… │
│  T1                              │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ✓ D1.1                           │
│ mkdb.py: plans gains status, cr… │
│  D1                              │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ✓ D1.2                           │
│ mk-plan.py: write the new colum… │
│  D1                              │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ✓ D1.3                           │
│ docs/state-schema.md: drop the … │
│  D1                              │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ✓ V1.1                           │
│ List sub-tab: ongoing runs on t… │
│  V1                              │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ✓ V1.2                           │
│ Detail sub-tab: the existing Pl… │
│  V1                              │
╰──────────────────────────────────╯

14 tasks · this plan only · source
mk.db`)
})
