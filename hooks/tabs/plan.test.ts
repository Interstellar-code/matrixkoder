// Real-code tests for the Plan Detail sub-tab (settings-plans-db, V1.2): the
// pure PlanTab render. Default every phase is folded, one phase expands, Expand
// all / Collapse all, the word "wave" is absent, the header card carries its
// fields, and 36 cols still renders.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { PlanTab } from './plan'
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
  const drawn = PlanTab(stubEls, routedFixture(), 36, new Map([['plan:phase:V1', '1']]))
  const text = allText(drawn)
  // 36 cols → the row cannot fit id + 10 title cols + the 20-col tag: the
  // tag is gone, and the title keeps at least 10 cols after the id
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
│ created 2026-10-13                                                           │
│ runs settings-plans-db-r1                                                    │
│ merges to main                                                               │
│ design .matrixkoder/plans/settings-tab.md .matrixkoder/plans/settings-wire…  │
│ 32 tasks · 8 phases                                                          │
╰──────────────────────────────────────────────────────────────────────────────╯

PHASES                                               ▾ Expand all ▸ Collapse all

╭──────────────────────────────────────────────────────────────────────────────╮
│ ▸ ◈ › R1 · Self-tests never write a real DB: one MK home variab…   with T1   │
│ ◈◈◈                                                                          │
╰──────────────────────────────────────────────────────────────────────────────╯

╭──────────────────────────────────────────────────────────────────────────────╮
│ ▸ ✓ › T1 · Plan template is the contract: every header key names …   with R1 │
│ ✓✓                                                                           │
╰──────────────────────────────────────────────────────────────────────────────╯

╭──────────────────────────────────────────────────────────────────────────────╮
│ ▾ ◈ › D1 · Plans in the project DB: the four new columns, impor…   merged    │
│ ◈◈◈◈                                                                         │
│ ├  ◈ D1.1 mkdb.py: plans gains status, created, design, integration (…       │
│ ├  ◈ D1.2 mk-plan.py: write the new columns; \`import <file>\`; \`delete…       │
│ ├  ◈ D1.3 docs/state-schema.md: drop the 'added by D1' marks; documen…       │
│ ├  ◈ D1.4 First real import: settings-plans-db.md into .matrixkoder/m…       │
╰──────────────────────────────────────────────────────────────────────────────╯

╭──────────────────────────────────────────────────────────────────────────────╮
│ ▸ ○ › S1 · Settings mapping: every key carries scope and home, d…   ready    │
│ ○○                                                                           │
╰──────────────────────────────────────────────────────────────────────────────╯

╭──────────────────────────────────────────────────────────────────────────────╮
│ ▸ ◈ › V1 · Plan tab sub-tabs from the existing kit: List with p…   merged    │
│ ◈◈◈                                                                          │
╰──────────────────────────────────────────────────────────────────────────────╯

╭──────────────────────────────────────────────────────────────────────────────╮
│ ▸ ◈ › V1.4 · Kanban sub-tab to PlanKanban.dc.html: 4 side-by-si…   merged    │
│ ◈◈◈◈◈◈◈                                                                      │
╰──────────────────────────────────────────────────────────────────────────────╯

╭──────────────────────────────────────────────────────────────────────────────╮
│ ▸ ◐ › D1.5 · Task fields survive…  L5 · opencode space-bunny-free  review    │
│ ◐◐◐◐◐◐                                                                       │
╰──────────────────────────────────────────────────────────────────────────────╯

╭──────────────────────────────────────────────────────────────────────────────╮
│ ▸ ○ › V1.5 · Plan tab task cards: id, cu…  L3 · opencode glm-5.3  planned    │
│ ●⧖●○○                                                                        │
╰──────────────────────────────────────────────────────────────────────────────╯

phases 8`)
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
│ created 2026-10-13               │
│ runs settings-plans-db-r1        │
│ merges to main                   │
│ design .matrixkoder/plans/sett…  │
│ 32 tasks · 8 phases              │
╰──────────────────────────────────╯

PHASES   ▾ Expand all ▸ Collapse all

╭──────────────────────────────────╮
│ ▸ ◈ › R1 · Self-tes…   with T1   │
│ ◈◈◈                              │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ▸ ✓ › T1 · Plan templ…   with R1 │
│ ✓✓                               │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ▾ ◈ › D1 · Plans in…   merged    │
│ ◈◈◈◈                             │
│ ├  ◈ D1.1 mkdb.py: plans …       │
│ ├  ◈ D1.2 mk-plan.py: wri…       │
│ ├  ◈ D1.3 docs/state-sche…       │
│ ├  ◈ D1.4 First real impo…       │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ▸ ○ › S1 · Settings …   ready    │
│ ○○                               │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ▸ ◈ › V1 · Plan tab…   merged    │
│ ◈◈◈                              │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ▸ ◈ › V1.4 · Kanban…   merged    │
│ ◈◈◈◈◈◈◈                          │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ▸ ◐ › D1.5 · Task f…   review    │
│ ◐◐◐◐◐◐                           │
╰──────────────────────────────────╯

╭──────────────────────────────────╮
│ ▸ ○ › V1.5 · Plan …   planned    │
│ ●⧖●○○                            │
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
