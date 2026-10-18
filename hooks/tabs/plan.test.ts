// Real-code tests for the Plan Detail sub-tab (settings-plans-db, V1.2): the
// pure PlanTab render. Default every phase is folded, one phase expands, Expand
// all / Collapse all, the word "wave" is absent, the header card carries its
// fields, and 36 cols still renders.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { PLAN_STATE, PlanTab, statusState } from './plan'
import { planFromReply } from './plans-map'
import detailFixture from './fixtures/plan-detail'
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

/** ── V1.5: the task row's id, cut title and lane·agent tag ────────────── */

/** PLAN_REAL_FIXTURE with V1.3 carrying a lane and a route — the shape the
 *  plans reply now maps (slot int, route from route_json). */
const routedFixture = (): typeof PLAN_REAL_FIXTURE => ({
  ...PLAN_REAL_FIXTURE,
  phases: (PLAN_REAL_FIXTURE.phases ?? []).map(phase => phase.id === 'V1'
    ? {
        ...phase,
        rows: phase.rows.map(row => row.id === 'V1.3'
          ? { ...row, slot: 3, route: { agent: 'opencode', model: 'zai-coding-plan/glm-5.3', effort: 'high' } }
          : row),
      }
    : phase),
})

test('a task row shows id, cut title and the dim lane·agent tag (V1.5)', () => {
  const text = allText(PlanTab(stubEls, routedFixture(), 80, new Map([['plan:phase:V1', '1']])))
  expect(text).toContain('V1.3')
  expect(text).toContain('Kanban filter, wiring')
  expect(text).toContain('L3 · opencode glm-5.3')
})

test('a merged or done task with a slot and route shows no lane tag; a building one does (Z26)', () => {
  const withStatus = (status: string): typeof PLAN_REAL_FIXTURE => {
    const f = routedFixture()
    return { ...f, phases: (f.phases ?? []).map(phase => ({
      ...phase, rows: phase.rows.map(row => row.id === 'V1.3' ? { ...row, status } : row),
    })) }
  }
  const ui = new Map([['plan:phase:V1', '1']])
  expect(allText(PlanTab(stubEls, withStatus('building'), 80, ui))).toContain('L3 · opencode glm-5.3')
  for (const status of ['merged', 'done']) {
    const text = allText(PlanTab(stubEls, withStatus(status), 80, ui))
    expect(text).toContain('V1.3')
    expect(text).not.toContain('L3 · opencode glm-5.3')
  }
})

test('at a narrow width the lane·agent tag drops before the title shrinks below 10', () => {
  const drawn = PlanTab(stubEls, routedFixture(), 41, new Map([['plan:phase:V1', '1']]))
  const text = allText(drawn)
  // 2341d2c: the phases sit in one Card and each task row is indented 4 more cells, so a
  // task row has cols − 12 cells (36 cols leave the title 5, below the 10 floor). 41 cols
  // (the narrowest non-compact pane) → the row cannot fit id + 10 title cols + the 20-col tag:
  // the tag is gone, and the title keeps at least 10 cols after the id
  expect(text).not.toContain('L3 · opencode glm-5.3')
  expect(text).toContain('V1.3 Kanban f')
  const row = nodesOf(drawn, 'Button').find(b => String(b.props?.key) === 'plan-task-V1.3')
  expect(row, 'pressable task row').toBeDefined()
  expect(String(row?.props?.label ?? '').length).toBeGreaterThanOrEqual('V1.3 '.length + 10)
})

test('pressing a task row selects it; pressing it again clears it (V1.5)', () => {
  const ui = new Map([['plan:phase:V1', '1']])
  press(PlanTab(stubEls, routedFixture(), 80, ui), 'plan-task-V1.3')
  expect(ui.get('plan:task')).toBe('V1.3')
  press(PlanTab(stubEls, routedFixture(), 80, ui), 'plan-task-V1.3')
  expect(ui.get('plan:task')).toBe('')
})

/** ── the report render (spec "Real inputs", V1.5) ───────────────────────
 *  The plankanban-style plain-text layout, pinned so the report paste is
 *  what the suite verified: the Detail sub-tab with phase D1 open, from the
 *  captured live fixture. */
function renderLines(v: unknown, width?: number): string[] {
  if (typeof v === 'string') return [v]
  if (typeof v !== 'object' || v === null) return []
  const cur = v as Node
  if (cur.type === 'Text') {
    const s = (cur.children ?? []).map(c => (typeof c === 'string' ? c : '')).join('')
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
  if (cur.type === 'Button') return [String(cur.props?.label ?? '')]
  const isBox = cur.type === 'Box'
  const props = cur.props ?? {}
  const row = isBox && props.flexDirection === 'row'
  const between = isBox && props.justifyContent === 'space-between'
  const bordered = isBox && props.borderStyle === 'round'
  const own = typeof props.width === 'number' ? props.width : row && !between ? undefined : width
  const childW = bordered && own != null ? own - 4 : own
  const blocks = (cur.children ?? []).map(k => renderLines(k, row && !between ? undefined : childW))
  let inner: string[] = []
  if (row) {
    const gap = typeof props.gap === 'number' ? props.gap : 0
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
      } else {
        inner.push(...a)
        for (const l of b) inner.push(' '.repeat(Math.max(0, own - l.length)) + l)
      }
    } else {
      for (let i = 0; i < h; i++) {
        inner.push(blocks.map((b, bi) => (b[i] ?? ' '.repeat(ws[bi]!)).padEnd(ws[bi]!, ' ')).join(' '.repeat(gap)))
      }
    }
  } else {
    const gap = typeof props.gap === 'number' ? props.gap : 0
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

const liveDetailPic = (cols: number): string => {
  // structure only: no Raster in the table, so the laneStrip draws its text
  // fallback (the glyph run) — the same shape the report paste carries
  const els = { Box: boxTag, Text: textTag, Button: (props: Record<string, unknown>) => ({ type: 'Button', props, children: [] }) } as unknown as typeof stubEls
  return renderLines(PlanTab(els, planFromReply(detailFixture), cols, new Map([['plan:phase:D1', '1']])), cols).map(l => l.replace(/\s+$/, '')).join('\n')
}

test('report render: the Detail sub-tab with phase D1 open at 80 cols, exactly as pasted (V1.5)', () => {
  expect(liveDetailPic(80)).toBe(`╭──────────────────────────────────────────────────────────────────────────────╮
│ settings-plans-db                                                   running  │
│ Settings field mapping, plans in the DB, and Plan tab sub-tabs               │
│ created    2026-10-13                                                        │
│ runs       settings-plans-db-r1                                              │
│ merges to  main                                                              │
│ design     settings-tab · settings-wire · dashboard-v2 · mk-store            │
│ 32 tasks · 8 phases                                    ◖‖ Pause◗ ◖Open file◗ │
╰──────────────────────────────────────────────────────────────────────────────╯

PHASES all 8                                     ◖▾ Expand all◗ ◖▸ Collapse all◗

╭──────────────────────────────────────────────────────────────────────────────╮
│  ▸ ◆ › R1 · Self-tests never write a real DB   with T1   merged              │
│  ◆◆◆                                                                         │
│ ────────────────────────────────────────────────────────────────────────     │
│  ▸ ◆ › T1 · Plan template is the contract   with R1   done                   │
│  ◆◆                                                                          │
│ ────────────────────────────────────────────────────────────────────────     │
│  ▾ ◆ › D1 · Plans in the project DB   merged                                 │
│  ◆◆◆◆                                                                        │
│  ├  ◆ D1.1 mkdb.py  r1                                                       │
│  ├  ◆ D1.2 mk-plan.py  r1                                                    │
│  ├  ◆ D1.3 docs/state-schema.md  r1                                          │
│  └  ◆ D1.4 First real import  r0                                             │
│ ────────────────────────────────────────────────────────────────────────     │
│  ▸ ○ › S1 · Settings mapping   ready                                         │
│  ○○                                                                          │
│ ────────────────────────────────────────────────────────────────────────     │
│  ▸ ◆ › V1 · Plan tab sub-tabs from the existing kit   merged                 │
│  ◆◆◆                                                                         │
│ ────────────────────────────────────────────────────────────────────────     │
│  ▸ ◆ › V1.4 · Kanban sub-tab to PlanKanban.dc.html   merged                  │
│  ◆◆◆◆◆◆◆                                                                     │
│ ────────────────────────────────────────────────────────────────────────     │
│  ▸ ◐ › D1.5 · Task fields sur…  L5 · opencode space-bunny-free  review       │
│  ◐◐◐◐◐◐                                                                      │
│ ────────────────────────────────────────────────────────────────────────     │
│  ▸ ● › V1.5 · Plan tab task cards  L3 · opencode glm-5.3  planned  ◖‖◗       │
│  ●●●○○                                                                       │
╰──────────────────────────────────────────────────────────────────────────────╯

8 phases · all folded by default                                  source project`)
})

test('a DONE plan\u2019s absent round files read archived \u00b7 legacy tarball (L7.2); a running plan adds no such row', () => {
  const done = planFromReply({ ok: true, id: 'workflows-v2', title: 'Workflows v2', status: 'done', created: '2026-10-02', integration: 'main', design: [], runs: ['workflows-v2-r1'], live_run: null, counts: { merged: 6 }, phases: [] })
  const doneText = allText(PlanTab(stubEls, done, 120, new Map()))
  expect(doneText).toContain('round files')
  expect(doneText).toContain('archived \u00b7 legacy tarball')
  expect(doneText).not.toContain('missing')
  // a running plan (the same shape, status running) keeps the render byte-identical:
  // no round-files row at all.
  const running = planFromReply({ ok: true, id: 'workflows-v2', title: 'Workflows v2', status: 'running', created: '2026-10-02', integration: 'main', design: [], runs: ['workflows-v2-r1'], live_run: 'workflows-v2-r1', counts: { merged: 6 }, phases: [] })
  const runningText = allText(PlanTab(stubEls, running, 120, new Map()))
  expect(runningText).not.toContain('round files')
  expect(runningText).not.toContain('archived')
})

test('the report render: the Detail sub-tab with phase D1 open at 36 cols, exactly as pasted (V1.5)', () => {
  expect(liveDetailPic(36)).toBe(`╭──────────────────────────────────╮
│ settings-plans-db       running  │
│ Settings field mapping, plans i… │
│ created    2026-10-13            │
│ runs       settings-plans-db-r1  │
│ merges to  main                  │
│ design     settings-tab · setti… │
│ 32 task…   ◖‖ Pause◗ ◖Open file◗ │
╰──────────────────────────────────╯

PHASES
     ◖▾ Expand all◗ ◖▸ Collapse all◗

╭──────────────────────────────────╮
│  ▸ ◆ › R1 · Self…   with T1   me │
│  ◆◆◆                             │
│ ────────────────────────────     │
│  ▸ ◆ › T1 · Plan t…   with R1    │
│  ◆◆                              │
│ ────────────────────────────     │
│  ▾ ◆ › D1 · Plan…   merged       │
│  ◆◆◆◆                            │
│  ├  ◆ D1.1 mkdb…  r1             │
│  ├  ◆ D1.2 mk-p…  r1             │
│  ├  ◆ D1.3 docs…  r1             │
│  └  ◆ D1.4 Firs…  r0             │
│ ────────────────────────────     │
│  ▸ ○ › S1 · Setti…   ready       │
│  ○○                              │
│ ────────────────────────────     │
│  ▸ ◆ › V1 · Plan…   merged       │
│  ◆◆◆                             │
│ ────────────────────────────     │
│  ▸ ◆ › V1.4 · Ka…   merged       │
│  ◆◆◆◆◆◆◆                         │
│ ────────────────────────────     │
│  ▸ ◐ › D1.5 · Ta…   review       │
│  ◐◐◐◐◐◐                          │
│ ────────────────────────────     │
│  ▸ ● › V1.5 · P…   planned  ◖‖◗  │
│  ●●●○○                           │
╰──────────────────────────────────╯`)
})

test('a routed phase row shows its lane·agent tag and selects itself as a task (V1.5)', () => {
  const ui = new Map<string, string>()
  const drawn = PlanTab(stubEls, planFromReply(detailFixture), 80, ui)
  const text = allText(drawn)
  expect(text).toContain('L5 · opencode space-bunny-free') // D1.5's route
  expect(text).toContain('L3 · opencode glm-5.3') // V1.5's own route
  expect(text).toContain('opencode glm-5.3') // V1.4: agent only, no slot
  press(drawn, 'plan-fold-D1.5')
  expect(ui.get('plan:task')).toBe('D1.5')
  expect(ui.get('plan:phase:D1.5')).toBe('1') // the fold still toggles
  press(PlanTab(stubEls, planFromReply(detailFixture), 80, ui), 'plan-fold-D1.5')
  expect(ui.get('plan:task')).toBe('')
})

/** ── description: header (4 lines), expanded phase (3 lines), folded phase none ── */

const LONG = 'The first sentence sets the scene for a reviewer who has never seen this work. The second one says what changes. The third one says why it matters now. The fourth names the risk. The fifth explains how it was checked. The sixth is cut.'

const withDescriptions = (): typeof PLAN_REAL_FIXTURE => ({
  ...PLAN_REAL_FIXTURE,
  plan: PLAN_REAL_FIXTURE.plan == null ? undefined : { ...PLAN_REAL_FIXTURE.plan, description: LONG },
  phases: (PLAN_REAL_FIXTURE.phases ?? []).map(phase => phase.id === 'S1' ? { ...phase, description: LONG } : phase),
})

const descLines = (drawn: unknown, prefix: string): string[] =>
  nodesOf(drawn, 'Text').filter(t => String(t.props?.key ?? '').startsWith(prefix)).map(t => String((t.children ?? [])[0] ?? ''))

test('the header shows the plan description, wrapped and capped at 4 lines', () => {
  const lines = descLines(PlanTab(stubEls, withDescriptions(), 60, new Map()), 'plan-head-desc-')
  expect(lines.length).toBe(4)
  expect(lines[0]).toContain('The first sentence')
  for (const l of lines) expect(l.length).toBeLessThanOrEqual(56)
  expect(lines[3]?.endsWith('…')).toBe(true)
  expect(lines.join(' ')).not.toContain('sixth')
  // the shipped fixture text shows whole, and the word wave never appears
  const text = allText(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, new Map()))
  expect(text).toContain('This plan makes the settings screen')
  expect(text.toLowerCase()).not.toContain('wave')
})

test('an expanded phase shows its description capped at 3 lines; a folded one shows nothing', () => {
  const view = withDescriptions()
  const open = descLines(PlanTab(stubEls, view, 60, new Map([['plan:phase:S1', '1']])), 'plan-desc-S1-')
  expect(open.length).toBe(3)
  expect(open[2]?.endsWith('…')).toBe(true)
  expect(descLines(PlanTab(stubEls, view, 60, new Map()), 'plan-desc-')).toEqual([])
  const short = allText(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, new Map([['plan:phase:S1', '1']])))
  expect(short).toContain('Each setting is matched to the folder')
  expect(allText(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, new Map()))).not.toContain('Each setting is matched')
})

test('a phase without a description draws no description rows', () => {
  expect(descLines(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, new Map([['plan:phase:R1', '1']])), 'plan-desc-')).toEqual([])
})

/** ── wrapCap: a long word breaks; exact N lines has no ellipsis, N+1 cuts ── */

const LONG_WORD = 'matrixkoder/plans/settings-plans-db/integration/review/round-one/notes.md'
const HEAD_FILL = 'The settings screen and the saved plans now share one database. Tests stop touching the real one. Plans move into the project store. The Plan tab gets list, detail and board views.'
const HEAD_OVER = `${HEAD_FILL} Old keys go and nothing else changes. Sample data follows the real reply.`
// an expanded phase at 60 cols wraps to 50 cols (PhaseCard W-4, then Describe W-2)
const PHASE_FILL = 'Each setting is matched to the folder it belongs in. Keys nobody reads any more are removed. The sample data is brought back in line.'
const PHASE_OVER = `${PHASE_FILL} Done and checked.`

const withPlanDesc = (description: string): typeof PLAN_REAL_FIXTURE => ({
  ...PLAN_REAL_FIXTURE,
  plan: PLAN_REAL_FIXTURE.plan == null ? undefined : { ...PLAN_REAL_FIXTURE.plan, description },
})
const withPhaseDesc = (description: string): typeof PLAN_REAL_FIXTURE => ({
  ...PLAN_REAL_FIXTURE,
  phases: (PLAN_REAL_FIXTURE.phases ?? []).map(phase => phase.id === 'S1' ? { ...phase, description } : phase),
})

test('wrapCap: a word wider than the line is broken into chunks inside the width', () => {
  // header width at 60 cols is 56: the 73-col path is cut to a 56-col chunk, nothing dropped
  const lines = descLines(PlanTab(stubEls, withPlanDesc(`Read the plan at ${LONG_WORD} before touching anything.`), 60, new Map()), 'plan-head-desc-')
  expect(lines.every(l => l.length <= 56)).toBe(true)
  expect(lines[1]).toBe(LONG_WORD.slice(0, 56))
  expect(lines.join('')).toContain(LONG_WORD)
})

test('wrapCap: text that fills exactly 4 header lines has no ellipsis; a 5th line cuts the last one', () => {
  const exact = descLines(PlanTab(stubEls, withPlanDesc(HEAD_FILL), 60, new Map()), 'plan-head-desc-')
  expect(exact.length).toBe(4)
  expect(exact.some(l => l.includes('…'))).toBe(false)
  expect(exact[3]).toBe('and board views.')
  const over = descLines(PlanTab(stubEls, withPlanDesc(HEAD_OVER), 60, new Map()), 'plan-head-desc-')
  expect(over.length).toBe(4)
  expect(over[2]).toBe(exact[2])
  expect(over[3]?.endsWith('…')).toBe(true)
})

test('wrapCap: text that fills exactly 3 phase lines has no ellipsis; a 4th line cuts the last one', () => {
  const open = new Map([['plan:phase:S1', '1']])
  const exact = descLines(PlanTab(stubEls, withPhaseDesc(PHASE_FILL), 60, open), 'plan-desc-S1-')
  expect(exact.length).toBe(3)
  expect(exact.some(l => l.includes('…'))).toBe(false)
  const over = descLines(PlanTab(stubEls, withPhaseDesc(PHASE_OVER), 60, open), 'plan-desc-S1-')
  expect(over.length).toBe(3)
  expect(over[1]).toBe(exact[1])
  expect(over[2]?.endsWith('…')).toBe(true)
})

test('wrapCap: an 8-col plan does not crash and still draws the header description, capped at 4 lines', () => {
  const lines = descLines(PlanTab(stubEls, withDescriptions(), 8, new Map()), 'plan-head-desc-')
  expect(lines.length).toBe(4)
  expect(lines.every(l => l.length <= 4)).toBe(true)
  expect(lines[3]?.endsWith('…')).toBe(true)
})

test('an expanded draft phase names its builder and reviewer; a folded one does not', () => {
  const view = { ...PLAN_REAL_FIXTURE, phases: (PLAN_REAL_FIXTURE.phases ?? []).map(phase => phase.id === 'S1'
    ? { ...phase, plannedRoute: { agent: 'agy', model: 'gemini-3.8-flash-high' }, reviewer: 'claude sonnet' } : phase) }
  const open = allText(PlanTab(stubEls, view, 80, new Map([['plan:phase:S1', '1']])))
  expect(open).toContain('builder agy gemini-3.8-flash-high · reviewer claude sonnet')
  expect(allText(PlanTab(stubEls, view, 80, new Map()))).not.toContain('builder agy')
  expect(allText(PlanTab(stubEls, PLAN_REAL_FIXTURE, 80, new Map([['plan:phase:S1', '1']])))).not.toContain('builder')
})

/** ── five states: one hue and one shape each (bar, filter chip, state chip, task dot) ── */

const task = (id: string, status: string) => ({ id, title: `${id} task`, status, subtasks: [] })
const statesView = () => planFromReply({
  ok: true, id: 'p', title: 'T', status: 'running',
  phases: [
    { id: 'D', title: 'Done phase', status: 'merged', tasks: [task('D.1', 'merged')] },
    { id: 'B', title: 'Building phase', status: 'building', tasks: [task('B.1', 'building')] },
    { id: 'R', title: 'Review phase', status: 'review', tasks: [task('R.1', 'review')] },
    { id: 'P', title: 'Pending phase', status: 'planned', tasks: [task('P.1', 'planned')] },
    { id: 'X', title: 'Attention phase', status: 'fix', tasks: [task('X.1', 'fix')] },
  ],
})
const chipOf = (drawn: unknown, state: keyof typeof PLAN_STATE): Node | undefined =>
  nodesOf(nodesOf(drawn, 'Button').find(b => String(b.props?.key) === `plan-phase-filter-${state}`), 'Text')[0]
  ?? ((nodesOf(drawn, 'Button').find(b => String(b.props?.key) === `plan-phase-filter-${state}`)?.props?.children as Node[] | undefined) ?? [])[0]

test('statusState: every status is one of the five states, unknown is pending', () => {
  const want: Record<string, string> = {
    merged: 'done', qa: 'done', done: 'done',
    dispatched: 'building', building: 'building', verifying: 'building',
    review: 'review',
    fix: 'attention', blocked: 'attention', quota: 'attention', stalled: 'attention', provider: 'attention',
    planned: 'pending', routed: 'pending', nonsense: 'pending',
  }
  for (const [status, state] of Object.entries(want)) expect(statusState(status), status).toBe(state)
  const hues = Object.values(PLAN_STATE).map(c => c.color)
  expect(new Set(hues).size).toBe(5)
  expect(new Set(Object.values(PLAN_STATE).map(c => c.glyph)).size).toBe(5)
})

test('each phase bar and state chip takes its state colour', () => {
  const drawn = PlanTab(stubEls, statesView(), 80, new Map())
  const bars = nodesOf(drawn, 'Box').filter(b => b.props?.width === 1 && typeof b.props?.backgroundColor === 'string').map(b => b.props?.backgroundColor)
  expect(bars).toEqual([PLAN_STATE.done.color, PLAN_STATE.building.color, PLAN_STATE.review.color, PLAN_STATE.pending.color, PLAN_STATE.attention.color])
  for (const state of Object.keys(PLAN_STATE) as (keyof typeof PLAN_STATE)[]) {
    const c = PLAN_STATE[state]
    expect(nodesOf(drawn, 'Text').some(t => t.props?.backgroundColor === c.tint && t.props?.color === c.color), `${state} chip`).toBe(true)
  }
})

test('the PHASES chips match the bars: coloured, building counts review, attention only when needed', () => {
  const drawn = PlanTab(stubEls, statesView(), 80, new Map())
  const text = (state: keyof typeof PLAN_STATE): string => String(((chipOf(drawn, state)?.children ?? []) as unknown[]).join(''))
  expect(chipOf(drawn, 'building')?.props?.backgroundColor).toBe(PLAN_STATE.building.tint)
  expect(chipOf(drawn, 'building')?.props?.color).toBe(PLAN_STATE.building.color)
  expect(text('building')).toContain('building 2')
  expect(text('pending')).toContain('pending 1')
  expect(text('done')).toContain('done 1')
  expect(text('attention')).toContain('attention 1')
  expect(text('done')).toContain(PLAN_STATE.done.glyph)
  // nothing needs attention: the chip is not drawn
  const calm = planFromReply({ ok: true, id: 'p', title: 'T', status: 'running', phases: [{ id: 'B', title: 'b', status: 'building', tasks: [task('B.1', 'building')] }] })
  expect(nodesOf(PlanTab(stubEls, calm, 80, new Map()), 'Button').some(b => String(b.props?.key) === 'plan-phase-filter-attention')).toBe(false)
})

test('the picked chip is filled solid, and building also shows the review phase', () => {
  const ui = new Map<string, string>([['plan:phase-filter', 'building']])
  const drawn = PlanTab(stubEls, statesView(), 80, ui)
  expect(chipOf(drawn, 'building')?.props?.backgroundColor).toBe(PLAN_STATE.building.color)
  const shown = allText(drawn)
  expect(shown).toContain('Building phase')
  expect(shown).toContain('Review phase')
  expect(shown).not.toContain('Done phase')
  press(drawn, 'plan-phase-filter-pending')
  expect(ui.get('plan:phase-filter')).toBe('pending')
})

test('a picked attention filter with nothing to show falls back to all', () => {
  const calm = planFromReply({ ok: true, id: 'p', title: 'T', status: 'running', phases: [{ id: 'B', title: 'Building phase', status: 'building', tasks: [task('B.1', 'building')] }] })
  const drawn = PlanTab(stubEls, calm, 80, new Map([['plan:phase-filter', 'attention']]))
  expect(allText(drawn)).toContain('Building phase')
  expect(allText(drawn)).not.toContain('no attention phases')
})

test('task dots take the state colour and shape', () => {
  const ui = new Map<string, string>([['plan:phase:X', '1'], ['plan:phase:R', '1'], ['plan:phase:D', '1']])
  const drawn = PlanTab(stubEls, statesView(), 80, ui)
  for (const state of ['attention', 'review', 'done'] as const) {
    const c = PLAN_STATE[state]
    expect(nodesOf(drawn, 'Text').some(t => t.props?.color === c.color && (t.children ?? []).join('') === c.glyph), `${state} dot`).toBe(true)
  }
})
