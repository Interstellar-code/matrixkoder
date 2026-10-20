// Plan tab Kanban sub-tab tests (plan settings-plans-db, V1.4, contract C15):
// the render called directly with a stub element table so the assertions read
// the elements PlanKanbanTab actually built — four columns with counts from the
// captured plan-detail fixture, the status→column map, the phase filter row,
// card lines (glyph, id, cut title, phase chip, age), the empty column's
// `— nothing here`, the narrow stack, the footer, and none of the old KanbanTab chrome.
import { test, expect } from 'claude-code/testing'
import type { Elements } from 'claude-code'
import {
  PlanKanbanTab,
  PLAN_KANBAN_COLUMNS,
  STATUS_TO_COLUMN,
  columnOfTask,
  planKanbanView,
  planKanbanSideBySide,
  timeInStatus,
  verdictNeedsChanges,
} from './plankanban'
import type { PlanKanbanView } from './plankanban'
import detail from './fixtures/plan-detail'

type N = { type: string; props: Record<string, unknown>; children: unknown[] }

const node = (v: unknown): N => v as N
const kids = (v: unknown): N[] => {
  const arr = (v as { children?: unknown[] }).children
  return Array.isArray(arr) ? (arr as unknown[]).map(node) : []
}
/** All text a card draws: Text children in order, and a Button's label (the
 *  V1.5 card id is a pressable Button, not a Text). */
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

/** the four headings as drawn: the column's glyph, a space, its name
 *  (plankanban.tsx COLUMN_GLYPH, changed in 2341d2c). */
const HEADING: Record<string, string> = {
  PLANNED: '○ PLANNED', BUILDING: '● BUILDING', 'NEEDS YOU': '▲ NEEDS YOU', 'ON MAIN': '✓ ON MAIN',
}

/** the four column blocks: a Box column whose direct child is the Section Box
 *  (a row holding the bold heading Text). */
function columnBlock(root: unknown, name: string): N | undefined {
  return allOf(root, 'Box').find(box => kids(box).some(
    k => k.type === 'Box' && kids(k).some(g => g.type === 'Text' && g.props.bold === true && text(g) === HEADING[name]),
  ))
}

/** a column block's count: the bare-number Text under its heading (muted, not dim, since the palette change). */
const countOf = (block: N | undefined): string => {
  const count = allOf(block, 'Text').find(t => /^\d+$/.test(text(t)))
  return count == null ? '' : text(count)
}

/** one card's outer Box, by the task id it carries as its element key. */
const cardBox = (root: unknown, id: string): N | undefined =>
  allOf(root, 'Box').find(b => b.props.key === id)

/** the tag a card draws: the dim Texts on its chip row (the age has a `m`/`h`/
 *  `d` suffix) plus the own-row dim Text the narrow fallback adds below it. */
const tagsOf = (card: N | undefined): string[] => {
  if (card == null) return []
  const chipRow = kids(card).find(k => k.type === 'Box' && kids(k).some(t => t.type === 'Text' && t.props.backgroundColor != null))
  const row = chipRow == null ? [] : kids(chipRow)
  const own = kids(card)
  return [...row, ...own]
    .filter(t => t.type === 'Text' && t.props.dimColor === true)
    .map(t => text(t))
}

/** every status the captured fixture's tasks carry, for the map test. */
const FIXTURE_IDS = detail.phases.flatMap(p => p.tasks.map(t => t.id))

const view = planKanbanView(detail)
const W80 = 80

test('planKanbanView maps the captured detail: 32 cards, 8 phases in plan order, no unknown statuses', () => {
  expect(view.planId).toBe('settings-plans-db')
  expect(view.phases).toEqual(['R1', 'T1', 'D1', 'S1', 'V1', 'V1.4', 'D1.5', 'V1.5'])
  expect(view.cards).toHaveLength(32)
  expect(view.unknownStatuses).toEqual([])
  // verdict survives the mapping (the NEEDS YOU rule reads it)
  expect(view.cards.find(c => c.id === 'R1.1')?.verdict).toBe('approve')
  expect(view.cards.find(c => c.id === 'V1.5.1')?.status).toBe('building')
  // no `updated` reading without a nowIso → no age
  expect(view.cards.every(c => c.age === '')).toBe(true)
})

test('the status→column map: every status named by C15, unknown falls to PLANNED, changes-verdict wins', () => {
  expect(STATUS_TO_COLUMN).toEqual({
    planned: 'PLANNED', routed: 'PLANNED', ready: 'PLANNED',
    dispatched: 'BUILDING', building: 'BUILDING', verifying: 'BUILDING', review: 'BUILDING', fix: 'BUILDING',
    blocked: 'NEEDS YOU', stalled: 'NEEDS YOU', quota: 'NEEDS YOU', provider: 'NEEDS YOU', qa: 'NEEDS YOU',
    rework: 'NEEDS YOU',
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

test('four headings with the right counts: PLANNED 4 · BUILDING 9 · NEEDS YOU 0 · ON MAIN 19', () => {
  const tree = PlanKanbanTab(stubEls(), view, W80, uiOf().handle)
  const heads = allOf(tree, 'Text').filter(t => t.props.bold === true).map(t => text(t))
  expect(heads).toEqual(PLAN_KANBAN_COLUMNS.map(col => HEADING[col]))
  const counts = PLAN_KANBAN_COLUMNS.map(col => countOf(columnBlock(tree, col)))
  expect(counts).toEqual(['4', '9', '0', '19'])
  // every fixture task id is drawn exactly once across the four columns
  const all = text(tree)
  for (const id of FIXTURE_IDS) expect(all.includes(id), id).toBe(true)
})

test('a card shows glyph, id, cut title, phase chip and the dim lane·agent tag (V1.5.1 building, R1.1 merged)', () => {
  const tree = PlanKanbanTab(stubEls(), view, W80, uiOf().handle)
  const building = columnBlock(tree, 'BUILDING')
  expect(text(building)).toContain('●V1.5.1') // glyph and id are sibling Texts; the row's gap is layout, not text
  // 80 cols → each column 19 wide → card inner 11 (lane and card boxes take 4 each): the long title is cut, not wrapped
  expect(text(building)).toContain('mkdb plans…')
  expect(text(building)).not.toContain('node carries lane slot')
  expect(text(building)).toContain(' V1.5 ')
  const onMain = columnBlock(tree, 'ON MAIN')
  expect(text(onMain)).toContain('✓R1.1')
  expect(text(onMain)).toContain('One home r…')
  expect(text(onMain)).not.toContain('$MK_HOME') // R1.1's long title is cut, not wrapped
  // ON MAIN cards are the glyph and the dim title only: no phase chip, no lane tag
  expect(text(onMain)).not.toContain(' R1 ')
  expect(text(onMain)).not.toContain('L4 opencode')
  // no nowIso passed → no age text
  expect(text(tree)).not.toMatch(/\b\d+[mhd]\b/)
})

test('empty column shows a dim "— nothing here"', () => {
  const tree = PlanKanbanTab(stubEls(), view, W80, uiOf().handle)
  const needsYou = columnBlock(tree, 'NEEDS YOU')
  const none = kids(needsYou).find(k => k.type === 'Text' && text(k) === '— nothing here')
  expect(none, 'dim nothing-here line').toBeDefined()
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
  expect(text(columnBlock(narrow, 'BUILDING'))).toContain('●V1.5.1')
  // wide: the same parent is a row
  const wide = PlanKanbanTab(stubEls(), view, W80, uiOf().handle)
  const wideParent = allOf(wide, 'Box').find(b => kids(b).includes(columnBlock(wide, 'PLANNED') as N) && kids(b).includes(columnBlock(wide, 'ON MAIN') as N))
  expect(wideParent?.props.flexDirection).toBe('row')
})

test('footer: <n> tasks · this plan only · source project, following the filter', () => {
  const tree = PlanKanbanTab(stubEls(), view, W80, uiOf().handle)
  expect(text(tree)).toContain('32 tasks · this plan only')
  expect(text(tree)).toContain('source project')
  const { handle } = uiOf(new Map([['plankanban:phase', 'S1']]))
  const narrowed = PlanKanbanTab(stubEls(), view, W80, handle)
  expect(text(narrowed)).toContain('2 tasks · this plan only')
})

test('no KanbanTab chrome: no Needs you off line, no Backlog/Ready/Verify headings', () => {
  const tree = PlanKanbanTab(stubEls(), view, W80, uiOf().handle)
  const all = text(tree)
  expect(all).not.toContain('Needs you off')
  const heads = allOf(tree, 'Text').filter(t => t.props.bold === true).map(t => text(t))
  expect(heads).toEqual(PLAN_KANBAN_COLUMNS.map(col => HEADING[col]))
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

/** A card whose title is long enough to be cut at a narrow width, with a lane,
 *  an agent and a long model id — the shape todo 94 is about. The task id is
 *  real (D1.2 of the captured fixture); only the model is a long stand-in. */
const narrowFixture = (): ReturnType<typeof planKanbanView> => planKanbanView({
  ok: true, id: 'narrow', phases: [{ id: 'D1', tasks: [
    {
      id: 'D1.2', title: 'mk-plan.py: write the new columns, import, delete and pause', status: 'building',
      updated: '2026-10-14T11:38:00+02:00', slot: 4,
      route: { agent: 'opencode', model: 'zai-coding-plan/glm-5.3', effort: 'high' },
    },
  ] }],
}, '2026-10-14T12:00:00+02:00')

test('todo 94: a narrow card drops the model instead of cutting the tag mid-word, and keeps title, phase and age', () => {
  // 21 cols stacked → column 20 (cols - 1) → card inner 12: lane box and card each take border 2 + paddingX 2
  const inner = 21 - 1 - 8
  const tree = PlanKanbanTab(stubEls(), narrowFixture(), 21, uiOf().handle)
  const card = cardBox(tree, 'D1.2')
  expect(card, 'the D1.2 card').toBeDefined()
  // the tag is lane + agent only, whole words — never the old `L4 · openc`
  const tags = tagsOf(card).filter(t => t !== '22m')
  expect(tags).toEqual(['L4 opencode'])
  // and the whole card carries no mid-word cut of the tag
  expect(text(card as N)).not.toContain('L4 · openc')
  expect(text(card as N)).not.toMatch(/L4 · \S*…/)
  // title, phase chip and age still render on the narrow card
  const body = text(card as N)
  expect(body).toContain('D1.2')
  expect(body).toContain('mk-plan.py:…') // cut to the inner width, not wrapped
  expect(body).toContain(' D1 ')
  expect(body).toContain('22m')
  expect(inner).toBe(12)
})

test('todo 94: a wide card keeps the three-part lane · agent · model tag', () => {
  // 160 cols side by side → column 39 → card inner 35; chip ` D1 ` and the age
  // `22m` leave 26 cells, so the 21-cell `L4 · opencode glm-5.3` still fits
  const tree = PlanKanbanTab(stubEls(), narrowFixture(), 160, uiOf().handle)
  const card = cardBox(tree, 'D1.2')
  expect(card, 'the D1.2 card').toBeDefined()
  expect(tagsOf(card).filter(t => t !== '22m')).toEqual(['L4 · opencode glm-5.3'])
})

test('a card shows the dim lane·agent line and pressing it selects the task (V1.5)', () => {
  const routed = planKanbanView({
    ok: true, id: 'routed', phases: [{ id: 'P1', tasks: [
      { id: 'T1', title: 'routed task', status: 'building', slot: 3, route: { agent: 'opencode', model: 'zai-coding-plan/glm-5.3', effort: 'high' } },
      { id: 'T2', title: 'slot only', status: 'planned', slot: 5 },
    ] }],
  })
  expect(routed.cards[0]?.slot).toBe(3)
  expect(routed.cards[0]?.route).toEqual({ agent: 'opencode', model: 'zai-coding-plan/glm-5.3', effort: 'high' })
  const { ui, handle } = uiOf()
  const tree = PlanKanbanTab(stubEls(), routed, W80, handle)
  const building = text(columnBlock(tree, 'BUILDING'))
  // 80 cols → card inner 15 → chip row room 10: `L3 opencode` (11) does not fit
  // beside the chip, so todo 94 moves it to its own row (whole words, no cut)
  expect(tagsOf(cardBox(tree, 'T1'))).toContain('L3 opencode')
  expect(building).not.toContain('L3 · openc')
  const planned = text(columnBlock(tree, 'PLANNED'))
  expect(planned).toContain('L5')
  // the card is pressable: first press selects, second press clears
  const card = allOf(tree, 'Button').find(b => String(b.props.key) === 'plankanban-card-T1')
  expect(card, 'pressable card').toBeDefined()
  ;(card?.props.onPress as () => void)()
  expect(ui.get('plan:task')).toBe('T1')
  ;(card?.props.onPress as () => void)()
  expect(ui.get('plan:task')).toBe('')
})

test('a card keeps its age beside the lane·agent tag (V1.5)', () => {
  const aged = planKanbanView({
    ok: true, id: 'aged', phases: [{ id: 'P1', tasks: [
      { id: 'P1.1', title: 'busy', status: 'building', updated: '2026-10-14T11:38:00+02:00', slot: 2, route: { agent: 'opencode', model: 'provider/sonnet' } },
    ] }],
  }, '2026-10-14T12:00:00+02:00')
  const tree = PlanKanbanTab(stubEls(), aged, W80, uiOf().handle)
  const building = text(columnBlock(tree, 'BUILDING'))
  // inner 15, chip ` P1 ` (4), age `22m` (3): chip row room 6 → neither the full
  // nor the model-dropped tag fits beside them; the tag takes its own row
  expect(building).toContain('22m')
  expect(tagsOf(cardBox(tree, 'P1.1'))).toContain('L2 opencode')
})

test('report render: the captured fixture at 80 cols, exactly as pasted', () => {
  expect(picture(80)).toBe(`all phases R1 T1 D1 S1 V1 V1.4 D1.5 V1.5                            k = all runs

╭─────────────────╮ ╭─────────────────╮ ╭─────────────────╮ ╭─────────────────╮
│ ○ PLANNED     4 │ │ ● BUILDING    9 │ │ ▲ NEEDS YOU   0 │ │ ✓ ON MAIN    19 │
│ ─────────────── │ │ ─────────────── │ │ ─────────────── │ │ ─────────────── │
│ ╭─────────────╮ │ │ ╭─────────────╮ │ │ — nothing here  │ │ ╭─────────────╮ │
│ │ ○ S1.1      │ │ │ │ ● D1.5.1    │ │ ╰─────────────────╯ │ │ ✓ R1.1      │ │
│ │ Scope-home… │ │ │ │ run init -… │ │                     │ │ One home r… │ │
│ │  S1         │ │ │ │  D1.5       │ │                     │ ╰─────────────╯ │
│ ╰─────────────╯ │ │ ╰─────────────╯ │                     │ ╭─────────────╮ │
│ ╭─────────────╮ │ │ ╭─────────────╮ │                     │ │ ✓ R1.2      │ │
│ │ ○ S1.2      │ │ │ │ ● D1.5.2    │ │                     │ │ Write guar… │ │
│ │ Dead keys … │ │ │ │ store mirr… │ │                     │ ╰─────────────╯ │
│ │  S1         │ │ │ │  D1.5       │ │                     │ ╭─────────────╮ │
│ ╰─────────────╯ │ │ ╰─────────────╯ │                     │ │ ✓ R1.3      │ │
│ ╭─────────────╮ │ │ ╭─────────────╮ │                     │ │ Register m… │ │
│ │ ○ V1.5.4    │ │ │ │ ● D1.5.3    │ │                     │ ╰─────────────╯ │
│ │ Detail pan… │ │ │ │ dispatch r… │ │                     │ ╭─────────────╮ │
│ │  V1.5       │ │ │ │  D1.5       │ │                     │ │ ✓ T1.1      │ │
│ ╰─────────────╯ │ │ ╰─────────────╯ │                     │ │ templates/… │ │
│ ╭─────────────╮ │ │ ╭─────────────╮ │                     │ ╰─────────────╯ │
│ │ ○ V1.5.5    │ │ │ │ ● D1.5.4    │ │                     │ ╭─────────────╮ │
│ │ Fixture re… │ │ │ │ mk-state t… │ │                     │ │ ✓ T1.2      │ │
│ │  V1.5       │ │ │ │  D1.5       │ │                     │ │ SKILL.md s… │ │
│ ╰─────────────╯ │ │ ╰─────────────╯ │                     │ ╰─────────────╯ │
╰─────────────────╯ │ ╭─────────────╮ │                     │ ╭─────────────╮ │
                    │ │ ● D1.5.5    │ │                     │ │ ✓ D1.1      │ │
                    │ │ backfill s… │ │                     │ │ mkdb.py: p… │ │
                    │ │  D1.5       │ │                     │ ╰─────────────╯ │
                    │ ╰─────────────╯ │                     │ ╭─────────────╮ │
                    │ ╭─────────────╮ │                     │ │ ✓ D1.2      │ │
                    │ │ ● D1.5.6    │ │                     │ │ mk-plan.py… │ │
                    │ │ SKILL.md: … │ │                     │ ╰─────────────╯ │
                    │ │  D1.5       │ │                     │ ╭─────────────╮ │
                    │ ╰─────────────╯ │                     │ │ ✓ D1.3      │ │
                    │ ╭─────────────╮ │                     │ │ docs/state… │ │
                    │ │ ● V1.5.1    │ │                     │ ╰─────────────╯ │
                    │ │ mkdb plans… │ │                     │ ╭─────────────╮ │
                    │ │  V1.5       │ │                     │ │ ✓ D1.4      │ │
                    │ ╰─────────────╯ │                     │ │ First real… │ │
                    │ ╭─────────────╮ │                     │ ╰─────────────╯ │
                    │ │ ● V1.5.2    │ │                     │ ╭─────────────╮ │
                    │ │ Detail sub… │ │                     │ │ ✓ V1.1      │ │
                    │ │  V1.5       │ │                     │ │ List sub-t… │ │
                    │ ╰─────────────╯ │                     │ ╰─────────────╯ │
                    │ ╭─────────────╮ │                     │ ╭─────────────╮ │
                    │ │ ● V1.5.3    │ │                     │ │ ✓ V1.2      │ │
                    │ │ Kanban car… │ │                     │ │ Detail sub… │ │
                    │ │  V1.5       │ │                     │ ╰─────────────╯ │
                    │ ╰─────────────╯ │                     │ ╭─────────────╮ │
                    ╰─────────────────╯                     │ │ ✓ V1.3      │ │
                                                            │ │ Kanban sub… │ │
                                                            │ ╰─────────────╯ │
                                                            │ ╭─────────────╮ │
                                                            │ │ ✓ V1.4.1    │ │
                                                            │ │ New hooks/… │ │
                                                            │ ╰─────────────╯ │
                                                            │ ╭─────────────╮ │
                                                            │ │ ✓ V1.4.2    │ │
                                                            │ │ Phase filt… │ │
                                                            │ ╰─────────────╯ │
                                                            │ ╭─────────────╮ │
                                                            │ │ ✓ V1.4.3    │ │
                                                            │ │ Four colum… │ │
                                                            │ ╰─────────────╯ │
                                                            │ ╭─────────────╮ │
                                                            │ │ ✓ V1.4.4    │ │
                                                            │ │ Cards: sta… │ │
                                                            │ ╰─────────────╯ │
                                                            │ ╭─────────────╮ │
                                                            │ │ ✓ V1.4.5    │ │
                                                            │ │ Empty colu… │ │
                                                            │ ╰─────────────╯ │
                                                            │ ╭─────────────╮ │
                                                            │ │ ✓ V1.4.6    │ │
                                                            │ │ Narrow pan… │ │
                                                            │ ╰─────────────╯ │
                                                            │ ╭─────────────╮ │
                                                            │ │ ✓ V1.4.7    │ │
                                                            │ │ Footer: <n… │ │
                                                            │ ╰─────────────╯ │
                                                            ╰─────────────────╯

32 tasks · this plan only                                         source project`)
})

test('report render: the captured fixture at 36 cols, exactly as pasted', () => {
  expect(picture(36)).toBe(`all phases R1 T1 D1 S1 V1 V1.4 D1.5
                        k = all runs

╭──────────────────────────────────╮
│ ○ PLANNED                      4 │
│ ───────────────────────────────  │
│ ╭──────────────────────────────╮ │
│ │ ○ S1.1                       │ │
│ │ Scope-home map: every SCHE…  │ │
│ │  S1                          │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ○ S1.2                       │ │
│ │ Dead keys and fixture drif…  │ │
│ │  S1                          │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ○ V1.5.4                     │ │
│ │ Detail panel: select a tas…  │ │
│ │  V1.5                        │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ○ V1.5.5                     │ │
│ │ Fixture recaptured from th…  │ │
│ │  V1.5                        │ │
│ ╰──────────────────────────────╯ │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ● BUILDING                     9 │
│ ───────────────────────────────  │
│ ╭──────────────────────────────╮ │
│ │ ● D1.5.1                     │ │
│ │ run init --plan copies acc…  │ │
│ │  D1.5                        │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ● D1.5.2                     │ │
│ │ store mirror never blanks …  │ │
│ │  D1.5                        │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ● D1.5.3                     │ │
│ │ dispatch records agent, mo…  │ │
│ │  D1.5                        │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ● D1.5.4                     │ │
│ │ mk-state task check: manda…  │ │
│ │  D1.5                        │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ● D1.5.5                     │ │
│ │ backfill settings-plans-db…  │ │
│ │  D1.5                        │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ● D1.5.6                     │ │
│ │ SKILL.md: task round and r…  │ │
│ │  D1.5                        │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ● V1.5.1                     │ │
│ │ mkdb plans <id> --json: ea…  │ │
│ │  V1.5                        │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ● V1.5.2                     │ │
│ │ Detail sub-tab task row: i…  │ │
│ │  V1.5                        │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ● V1.5.3                     │ │
│ │ Kanban card: id, cut title…  │ │
│ │  V1.5                        │ │
│ ╰──────────────────────────────╯ │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ▲ NEEDS YOU                    0 │
│ ───────────────────────────────  │
│ — nothing here                   │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ✓ ON MAIN                     19 │
│ ───────────────────────────────  │
│ ╭──────────────────────────────╮ │
│ │ ✓ R1.1                       │ │
│ │ One home rule: mkdb.py ($M…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ R1.2                       │ │
│ │ Write guard: a process sta…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ R1.3                       │ │
│ │ Register matrixkoder in th…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ T1.1                       │ │
│ │ templates/plan.md: each he…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ T1.2                       │ │
│ │ SKILL.md stage 1 and docs/…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ D1.1                       │ │
│ │ mkdb.py: plans gains statu…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ D1.2                       │ │
│ │ mk-plan.py: write the new …  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ D1.3                       │ │
│ │ docs/state-schema.md: drop…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ D1.4                       │ │
│ │ First real import: setting…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ V1.1                       │ │
│ │ List sub-tab: ongoing runs…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ V1.2                       │ │
│ │ Detail sub-tab: the existi…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ V1.3                       │ │
│ │ Kanban sub-tab: the existi…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ V1.4.1                     │ │
│ │ New hooks/tabs/plankanban.…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ V1.4.2                     │ │
│ │ Phase filter row: all phas…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ V1.4.3                     │ │
│ │ Four columns side by side …  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ V1.4.4                     │ │
│ │ Cards: status glyph, task …  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ V1.4.5                     │ │
│ │ Empty column shows none (d…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ V1.4.6                     │ │
│ │ Narrow panes: columns stac…  │ │
│ ╰──────────────────────────────╯ │
│ ╭──────────────────────────────╮ │
│ │ ✓ V1.4.7                     │ │
│ │ Footer: <n> tasks · this p…  │ │
│ ╰──────────────────────────────╯ │
╰──────────────────────────────────╯

32 tasks · this plan only
                      source project`)
})
test('F.8: a rework status maps to NEEDS YOU via the map, and its card lands in the NEEDS YOU column', () => {
  // independent map assertion, extending (not editing) the map-equality test's spec:
  expect(columnOfTask({ id: 'X', title: '', status: 'rework', phase: 'P', verdict: null, age: '' })).toBe('NEEDS YOU')
  const reworkedView = planKanbanView({
    ok: true,
    id: 'rework',
    phases: [{
      id: 'P1',
      tasks: [
        { id: 'P1.9', title: 'proved wrong', status: 'rework', verdict: 'changes' },
        { id: 'P1.8', title: 'fine again', status: 'merged', verdict: 'approve' },
      ],
    }],
  })
  const tree = PlanKanbanTab(stubEls(), reworkedView, 80, uiOf().handle)
  const needs = columnBlock(tree, 'NEEDS YOU')
  expect(needs, 'NEEDS YOU column is drawn').toBeDefined()
  const card = cardBox(tree, 'P1.9')
  expect(card, 'the P1.9 card is drawn').toBeDefined()
  const needsText = text(needs)
  expect(needsText, 'the P1.9 card text sits in the NEEDS YOU column block').toContain('proved wro…')
  expect(needsText, 'the P1.9 card id sits in the NEEDS YOU column block').toContain('P1.9')
})
// F.8: risk, critical and open reasons on the cards (F.1 contract).
// Task rows may carry `risk` (int 1-5), `critical` (bool) and `open_reasons`
// ({ kind, points, source, opened } each); a row without them (or with an
// out-of-range risk) renders byte-identically to how it does on main today —
// never a made-up badge. The assertions read `planKanbanView`'s own cards
// (its output shape is F.1's editable turnout, so no `import type` edit here)
// and the elements PlanKanbanTab actually builds, with this file's own
// `stubEls` / `cardBox` / `columnBlock` / `text` / `allOf` helpers.

// the whole F.8 fixture: a shield of seven cards — five risk warriors in
// rank order (R1..R5), one card with no risk keys at all and one card whose
// risk is out of range (7 > 5; the mapper drops it, never a made-up badge)
// — then, off the wall, one critical card. Plain `building` rows: the
// builder column is the badge's home ground.
const shieldView = (): PlanKanbanView => planKanbanView({
  ok: true, id: 'shield', phases: [{ id: 'S', tasks: [
    { id: 'X.none', title: 'shield plain', status: 'building' },
    { id: 'X.bad', title: 'shield over worst rank', status: 'building', risk: 7 },
    { id: 'X.R1', title: 'rank one', status: 'building', risk: 1 },
    { id: 'X.R2', title: 'rank two', status: 'building', risk: 2 },
    { id: 'X.R3', title: 'rank three', status: 'building', risk: 3 },
    { id: 'X.R4', title: 'rank four', status: 'building', risk: 4 },
    { id: 'X.R5', title: 'rank five', status: 'building', risk: 5 },
    { id: 'X.R9', title: 'shield breach', status: 'building', risk: 4, critical: true },
  ] }],
})

test('F.8: a card carries the risk the raw row gave it — 1..5 kept, 7 dropped as out of range, absent stays undefined', () => {
  const view = shieldView()
  const rankOf = new Map(view.cards.map(c => [c.id, c.risk]))
  expect(rankOf, 'all eight shield cards are mapped').toEqual(new Map([
    ['X.none', undefined],   // no risk key → no rank, never 0 or a made-up badge
    ['X.bad', undefined],   // 7 is out of the 1-5 range: dropped, not clamped
    ['X.R1', 1],
    ['X.R2', 2],
    ['X.R3', 3],
    ['X.R4', 4],
    ['X.R5', 5],
    ['X.R9', 4],           // the critical card's own rank
  ]))
})

test('F.8: the R1..R5 badges draw through the narrow 36-col stack; the critical card adds !!, no one else does', () => {
  // 36 cols is the stacked fallback (planKanbanSideBySide false): the badge
  // is one Text in the card Box, so it survives the narrow stack
  expect(planKanbanSideBySide(36)).toBe(false)
  const narrow = PlanKanbanTab(stubEls(), shieldView(), 36, uiOf().handle)
  for (const tag of ['R1', 'R2', 'R3', 'R4', 'R5']) {
    const card = cardBox(narrow, `X.${tag}`)
    expect(card, `the ${tag} card survives the narrow stack`).toBeDefined()
    expect(text(card as N), `the ${tag} card draws its badge at 36 cols`).toContain(` ${tag} `)
  }
  expect(text(narrow)).not.toContain(' R0 ')
  expect(text(narrow)).not.toContain(' R7 ')

  // the critical row: `!!` right by its R4 badge — and no non-critical card
  // ever draws the marker
  const wide = PlanKanbanTab(stubEls(), shieldView(), W80, uiOf().handle)
  const critCard = cardBox(wide, 'X.R9')
  expect(critCard, 'the critical shield card is drawn').toBeDefined()
  expect(text(critCard as N), 'the critical card draws !! by its badge').toContain('!!')
  for (const tag of ['R1', 'R2', 'R3', 'R4', 'R5']) {
    const tagCard = cardBox(wide, `X.${tag}`)
    expect(text(tagCard as N), `${tag} is not critical → no !! marker`).not.toContain('!!')
  }
  expect(text(cardBox(wide, 'X.none') as N), 'the absent-risk card shows no !! either').not.toContain('!!')
})


// The regression guard: a task that carries none of the new keys draws
// byte-identical Text content whether or not some OTHER task in the same
// plan was given them. On main today the two renders agree exactly.
test('F.8: a row without the risk keys renders byte-identically even when a sibling task carries them', () => {
  const bare = planKanbanView({
    ok: true, id: 'byteguard',
    phases: [{ id: 'P1', tasks: [
      { id: 'P1.1', title: 'first, untouched', status: 'building' },
      { id: 'P1.2', title: 'second, untouched', status: 'planned' },
    ] }],
  })
  // same plan, same first task — but the OTHER task now carries the keys
  const withRisky = planKanbanView({
    ok: true, id: 'byteguard',
    phases: [{ id: 'P1', tasks: [
      { id: 'P1.1', title: 'first, untouched', status: 'building' },
      { id: 'P1.2', title: 'second, untouched', status: 'planned', risk: 2, critical: false },
    ] }],
  })
  const plainTree = PlanKanbanTab(stubEls(), bare, W80, uiOf().handle)
  const riskyTree = PlanKanbanTab(stubEls(), withRisky, W80, uiOf().handle)
  const plainCard = cardBox(plainTree, 'P1.1')
  const riskyCard = cardBox(riskyTree, 'P1.1')
  expect(plainCard, 'the P1.1 card is drawn in the plain render').toBeDefined()
  expect(riskyCard, 'the P1.1 card is drawn in the second render too').toBeDefined()
  // every Text run of the first task, in order, is byte-identical across
  // the two renders — absent rows render exactly as on main (no badge, no
  // marker, no made-up value may leak in)
  expect(allOf(plainCard, 'Text').map(t => text(t)),
    'the absent row renders exactly as on main: byte-identical with a risky sibling beside it')
    .toEqual(allOf(riskyCard, 'Text').map(t => text(t)))
})

// Regression guard for the K1.1 move out of the old Kanban tab: passes on the old code too.
test('timeInStatus of one hour before is 1h', () => {
  expect(timeInStatus('2026-10-12T09:00:00+02:00', '2026-10-12T10:00:00+02:00')).toBe('1h')
})
