// Lanes tab tests (plan dashboard-v2-p0b, V2.1): the mounted pane over the
// Checkout v2 fixture, plus the render called directly with a stub element table
// so the assertions read the elements LanesTab actually built — the Cards and
// their role borders, the Raster stage strip (and its glyph fallback where the
// table has no Raster), the gate table vs the gate chips, and one ControlRow per
// live lane writing only `ui` (C10). Keys and widths are read off the tree.
import { test, expect } from 'claude-code/testing'
import type { Elements } from 'claude-code'
import { mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './../testkit'
import { LanesTab, laneControlState, laneRole, laneStateKey, lanesView } from './lanes'
import type { LaneData, TabUi } from './lanes'
import { ROLE } from './palette'

const MOCK = /MOCK · fixture data · not this run/

/** every Text node's full shown width (strings under it, nested Texts included). */
function textWidths(n: unknown): number[] {
  const out: number[] = []
  const shown = (node: unknown): string => {
    if (typeof node === 'string') return node
    if (typeof node !== 'object' || node === null) return ''
    const cur = node as { children?: unknown[] }
    return Array.isArray(cur.children) ? cur.children.map(shown).join('') : ''
  }
  const walk = (node: unknown): void => {
    if (typeof node !== 'object' || node === null) return
    const cur = node as { type?: string; children?: unknown[] }
    if (cur.type === 'Text') {
      out.push(shown(cur).length)
      return
    }
    if (Array.isArray(cur.children)) cur.children.forEach(walk)
  }
  walk(n)
  return out
}

// ---------- a stub element table: the plain-data nodes kit.tsx's JSX builds ----

type N = { type: string; props: Record<string, unknown>; children: unknown[] }

const node = (v: unknown): N => v as N
const kids = (v: unknown): N[] => {
  const arr = (v as { children?: unknown[] }).children
  return Array.isArray(arr) ? (arr as unknown[]).map(node) : []
}
const text = (v: unknown): string => kids(v).map(c => (typeof c === 'string' ? c : text(c))).join('')

/** every node of `type`, in document order. */
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

/** the Button nodes, in document order. */
const buttonsOf = (root: unknown): N[] => allOf(root, 'Button')

/** a stub table: Box/Text real, Raster present only when asked for (the desktop
 *  and mobile surfaces have none, which is what pickBar switches on). */
function stubEls(withRaster: boolean): Elements[keyof Elements] {
  const make = (type: string) => (props: Record<string, unknown>, ...children: unknown[]) =>
    ({ type, props, children: children.length > 0 ? children : ((props.children as unknown[] | undefined) ?? []) }) as unknown
  return {
    Box: make('Box'),
    Text: make('Text'),
    Button: (props: Record<string, unknown>) => ({ type: 'Button', props, children: [] }),
    ...(withRaster ? { Raster: (props: Record<string, unknown>) => ({ type: 'Raster', props, children: [] }) } : {}),
  } as unknown as Elements[keyof Elements]
}

/** the local view state as a Map (C12: get/set and nothing else). */
function fakeUi(): TabUi & { map: Map<string, string> } {
  const map = new Map<string, string>()
  return { map, get: (key: string) => map.get(key), set: (key: string, value: string) => { map.set(key, value) } }
}

const WIDTHS = [36, 40, 46, 56] as const

/** a Card: a Box with a round border. */
const cardsIn = (root: unknown): N[] => allOf(root, 'Box').filter(b => b.props.borderStyle === 'round')

/** the row widths of the drawn tree: a Text is its own text, a horizontal Box
 *  sums its children plus the gap columns, a Button is its drawn label. */
type Drawn = { type?: string; props?: { flexDirection?: string; gap?: number; label?: string; plain?: boolean }; children?: unknown[] }

function rowWidth(v: unknown): number {
  if (typeof v !== 'object' || v === null) return 0
  const cur = v as Drawn
  const cs = cur.children ?? []
  if (cur.type === 'Text') return text(cur).length
  if (cur.type === 'Button') return String(cur.props?.label ?? '').length + (cur.props?.plain ? 2 : 4)
  if (cur.type === 'Box' && cur.props?.flexDirection === 'row') {
    const gap = typeof cur.props?.gap === 'number' ? cur.props.gap : 0
    return cs.reduce((sum: number, c) => sum + rowWidth(c), 0) + gap * Math.max(0, cs.length - 1)
  }
  return cs.reduce((max: number, c) => Math.max(max, rowWidth(c)), 0)
}

function rowWidths(root: unknown): number[] {
  const out: number[] = []
  const walk = (v: unknown): void => {
    if (typeof v !== 'object' || v === null) return
    const cur = v as Drawn
    if (cur.type === 'Text' || cur.type === 'Button' || (cur.type === 'Box' && cur.props?.flexDirection === 'row')) out.push(rowWidth(cur))
    for (const c of cur.children ?? []) walk(c)
  }
  walk(root)
  return out
}

// ---------- the mounted pane ----------

test('the Lanes tab draws the MOCK row and its fixture lanes at 46 cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-lanes' })
  expect(await ui.find({ text: MOCK }), 'MOCK row').toBeTruthy()
  expect(await ui.find({ text: /stripe adapter/ }), 'fixture commit message').toBeTruthy()
  expect(await ui.find({ text: /\[Reroute\]/ }), 'fixture actions').toBeTruthy()
})

test('the Lanes tab fits 36, 40, 46 and 56 cols: MOCK row, fixture string, no row over cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-lanes' })
  for (const w of [36, 40, 46, 56]) {
    await ui.redraw(paneProps(w))
    expect(await ui.find({ text: MOCK }), `MOCK row at ${w}`).toBeTruthy()
    for (const width of textWidths(await ui.drawn())) {
      expect(width, `row wider than ${w} cells at ${w}: ${width}`).toBeLessThanOrEqual(w)
    }
  }
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: /stripe adapter/ }), 'fixture string at 36').toBeTruthy()
})

test('the gate table is three columns at 46 and 56 cols; under 40 it is the Narrow board\'s chips', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-lanes' })
  // V2.1 moved the breakpoint: the lane-vs-baseline table is the DEFAULT view now
  // (plan acceptance: "the 3-column gate table shows at the default 46 cols"),
  // so it is at 46 as well as 56 — p0's one-liner is gone from both.
  await ui.redraw(paneProps(56))
  expect(await ui.find({ text: /baseline/ }), 'gate header at 56').toBeTruthy()
  expect(await ui.find({ text: /410 \/ 2/ }), 'baseline column at 56').toBeTruthy()
  expect(await ui.find({ text: /tests 412\/2/ }), 'no one-liner at wide').toBeUndefined()
  await ui.redraw(paneProps(46))
  expect(await ui.find({ text: /baseline/ }), 'gate header at 46').toBeTruthy()
  expect(await ui.find({ text: /410 \/ 2/ }), 'baseline column at 46').toBeTruthy()
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: /baseline/ }), 'no gate table under 40').toBeUndefined()
  expect(await ui.find({ text: /t 412\/2/ }), 'gate chip under 40').toBeTruthy()
})

// ---------- the render, called directly with a stub table ----------

test('C11: every lane is a round-bordered Card whose border is the live/warn role of its status', () => {
  const view = lanesView()
  for (const cols of [36, 46] as const) {
    const cards = cardsIn(LanesTab(stubEls(true), view, cols))
    expect(cards, `one Card per lane at ${cols}`).toHaveLength(view.lanes.length)
    expect(cards.map(c => c.props.borderColor), `border roles at ${cols}`).toEqual(
      view.lanes.map(l => ROLE[laneRole(l.status)].fg),
    )
    for (const card of cards) {
      expect(card.props.backgroundColor, 'panel fill').toBe('#151a21')
      expect(card.props.paddingX, 'one cell of padding each side').toBe(1)
    }
    expect(cards.map(c => c.props.borderColor), 'two live, two held').toEqual([ROLE.live.fg, ROLE.live.fg, ROLE.warn.fg, ROLE.warn.fg])
  }
})

test('C11: nothing is drawn outside a Card but the MOCK row and the two hint lines', () => {
  const view = lanesView()
  const root = node(LanesTab(stubEls(true), view, 46))
  expect(root.type).toBe('Box')
  // the root column: the three one-line hints plus one Box per lane
  const outside = kids(root).filter(c => c.type !== 'Box')
  const hintTexts = outside.map(t => text(t))
  expect(hintTexts[0]).toBe('MOCK · fixture data · not this run')
  expect(hintTexts[1], 'the summary hint').toContain('4 lanes')
  expect(hintTexts[2], 'the footer hint').toContain('lane-watch')
  const laneBoxes = kids(root).filter(c => c.type === 'Box')
  expect(laneBoxes, 'one wrapper per lane').toHaveLength(view.lanes.length)

  // every Text inside a lane's wrapper is inside a bordered Card (C11)
  const orphans: string[] = []
  const walk = (v: unknown, inCard: boolean): void => {
    if (typeof v !== 'object' || v === null) return
    const cur = node(v)
    const card = inCard || cur.props.borderStyle === 'round'
    if (cur.type === 'Text' && !card) orphans.push(text(cur))
    kids(cur).forEach(c => walk(c, card))
  }
  for (const box of laneBoxes) walk(box, false)
  expect(orphans, 'no row outside its Card').toEqual([])
})

test('C11: no drawn row is wider than the pane at 36, 40, 46 and 56 cols', () => {
  const view = lanesView()
  for (const cols of WIDTHS) {
    const tree = LanesTab(stubEls(true), view, cols)
    const widths = rowWidths(tree)
    expect(widths.length, `the tab drew rows at ${cols}`).toBeGreaterThan(0)
    const wide = widths.filter(w => w > cols)
    expect(wide, `over-wide rows at ${cols}: ${JSON.stringify(wide)}`).toEqual([])
  }
})

test('the stage strip is a Raster where the table has one, the mockup glyph Text where it does not', () => {
  const view = lanesView()
  const withRaster = LanesTab(stubEls(true), view, 46)
  const rasters = allOf(withRaster, 'Raster')
  expect(rasters, 'one strip per lane that has stages').toHaveLength(2)
  for (const r of rasters) {
    expect(r.props.columns, 'the strip takes the card content width').toBe(42)
    expect(r.props.rows).toBe(1)
    expect(String(r.props.key)).toMatch(/^lane-p[45]-stage$/)
  }

  const fallback = LanesTab(stubEls(false), view, 46)
  expect(allOf(fallback, 'Raster'), 'no Raster without one').toHaveLength(0)
  const stripText = allOf(fallback, 'Box')
    .filter(b => text(b).includes('◐'))
    .map(b => text(b))
  expect(stripText.some(t => t.startsWith('●●●●●◐○○○○○○') && t.includes('stage 6/12')), 'P4 stage glyphs').toBe(true)
  expect(stripText.some(t => t.startsWith('●●●●●●●◐○○○○') && t.includes('stage 8/12')), 'P5 stage glyphs').toBe(true)
  // the fallback row is the whole card content width at most
  expect(Math.max(...rowWidths(fallback))).toBeLessThanOrEqual(46)
})

test('the gates are the lane-vs-baseline table at 46 and the Narrow board\'s chips at 36', () => {
  const view = lanesView()
  const wide = allOf(LanesTab(stubEls(true), view, 46), 'Text').map(t => text(t))
  const header = wide.find(t => t.startsWith('gate') && t.includes('baseline'))
  expect(header, 'the three gate columns').toBeTruthy()
  expect(header).toContain('lane')
  expect(header).toContain('baseline')
  expect(wide.some(t => t.includes('412 / 2 known') && t.includes('410 / 2')), 'lane value beside its baseline').toBe(true)
  expect(wide.some(t => t.includes('t 412/2')), 'no gate chips at 46').toBe(false)

  const narrow = allOf(LanesTab(stubEls(true), view, 36), 'Text').map(t => text(t))
  expect(narrow.some(t => t.includes('baseline')), 'no gate table under 40').toBe(false)
  expect(narrow.some(t => t === ' t 412/2 '), 'the tests chip').toBe(true)
  expect(narrow.some(t => t === ' tsc ✓ '), 'the typecheck chip').toBe(true)
})

test('the review counts are chips: a blocker red, a minor amber, what is open green', () => {
  const view = lanesView()
  const tree = LanesTab(stubEls(true), view, 46)
  const chips = allOf(tree, 'Text').filter(t => /\d (blocker|minor|open)/.test(text(t)))
  expect(chips.map(c => text(c))).toEqual([' 1 blocker ', ' 2 minor ', ' 0 open '])
  expect(chips.map(c => c.props.backgroundColor)).toEqual([ROLE.err.tint, ROLE.warn.tint, ROLE.ok.tint])
  expect(chips.map(c => c.props.color)).toEqual([ROLE.err.fg, ROLE.warn.fg, ROLE.ok.fg])
  // only P5 carries them
  const withReviews = view.lanes.filter((l: LaneData) => l.reviews != null).map(l => l.task)
  expect(withReviews).toEqual(['P5'])
})

test('C10: one ControlRow per live lane, keys prefixed by the task, and a press writes only ui', () => {
  const view = lanesView()
  const ui = fakeUi()
  const draw = () => LanesTab(stubEls(true), view, 46, ui)
  const keys = buttonsOf(draw()).map(b => String(b.props.key))
  expect(keys, 'every key unique across the pane').toEqual(['p4-pause', 'p4-stop', 'p5-pause', 'p5-stop'])
  expect(new Set(keys).size, 'no key twice').toBe(keys.length)
  // the held lanes draw no controls (nothing to pause): the P6/P7 dim action line
  const held = view.lanes.filter(l => ['blocked', 'quota'].includes(l.status)).map(l => l.task)
  expect(held).toEqual(['P6', 'P7'])

  const press = (tree: unknown, key: string): void => {
    const button = buttonsOf(tree).find(b => b.props.key === key)
    expect(button, `button ${key}`).toBeDefined()
    ;(button?.props.onPress as () => void)()
  }
  press(draw(), 'p4-pause')
  expect(ui.get(laneStateKey('P4')), 'pause writes the view state').toBe('paused')
  const afterPause = buttonsOf(draw())
  expect(afterPause.map(b => String(b.props.label)), 'the re-render offers resume and stop for P4').toEqual(['▶ resume', 'stop', 'pause', 'stop'])
  expect(laneControlState(ui, 'P4')).toBe('paused')
  press(draw(), 'p4-resume')
  expect(ui.get(laneStateKey('P4'))).toBe('building')

  press(draw(), 'p5-stop')
  expect(ui.get(laneStateKey('P5'))).toBe('stopped')
  expect(buttonsOf(draw()).map(b => String(b.props.label)).slice(2, 3), 'a stopped lane offers restart').toEqual(['↻ restart'])
  // nothing beyond ui: the tab module never reaches for $ or a script
  expect([...ui.map.keys()].sort()).toEqual(['lane:P4', 'lane:P5'])
})

test('without ui the fixture default draws and a press is a no-op', () => {
  const tree = LanesTab(stubEls(true), lanesView(), 46)
  const pause = buttonsOf(tree).find(b => b.props.key === 'p4-pause')
  expect(buttonsOf(tree).map(b => String(b.props.label)).slice(0, 2), 'the default control state').toEqual(['pause', 'stop'])
  expect(() => (pause?.props.onPress as () => void)()).not.toThrow()
  expect(buttonsOf(LanesTab(stubEls(true), lanesView(), 46)).map(b => String(b.props.label)).slice(0, 2)).toEqual(['pause', 'stop'])
})

test('the lane fixture still draws: agent lines, the commit line, held-lane notes and the actions', () => {
  const view = lanesView()
  const wide = allOf(LanesTab(stubEls(true), view, 46), 'Text').map(t => text(t))
  expect(wide.some(t => t.includes('codex · gpt-6.1-sol · effort high')), 'P4 agent line').toBe(true)
  expect(wide.some(t => t.includes('2b8e41d feat(payments): stripe adapter')), 'sha and subject at 46').toBe(true)
  expect(wide.some(t => t.includes('agy 5h exhausted')), 'P7 quota note').toBe(true)
  expect(wide.some(t => t.includes('[Reroute]')), 'P7 actions').toBe(true)

  // under NARROW the agent line takes the Narrow board's short form and the sha
  // yields to the subject, so the subject is never cut away
  const narrow = allOf(LanesTab(stubEls(true), view, 36), 'Text').map(t => text(t))
  expect(narrow.some(t => t.includes('codex · sol · high')), 'P4 short agent line').toBe(true)
  expect(narrow.some(t => t === 'feat(payments): stripe adapter'), 'the commit subject alone at 36').toBe(true)
  expect(narrow.some(t => t.includes('gpt-6.1-sol')), 'no long agent line under 40').toBe(false)
})