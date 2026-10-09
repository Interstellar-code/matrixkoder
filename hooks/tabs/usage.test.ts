// Usage tab tests (plan dashboard-v2-p0b, V2.2): the mounted pane over the
// Checkout v2 fixture, plus the render called directly with a stub element table
// so the assertions read the elements UsageTab actually built — two Cards, the
// gradient bars (their cells compared against the kit's own gradientBar, floor
// mark included) and the SmoothBar fallback where the table has no Raster, the
// exhausted agent's err-coloured name, the legend chips' real roles and the
// routing rows' one-line form.
import { test, expect } from 'claude-code/testing'
import type { Elements } from 'claude-code'
import { mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './../testkit'
import { UsageTab, barRole, isExhausted, usageView } from './usage'
import { ROLE } from './palette'
import { gradientBar } from './raster'

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

// ---------- a stub element table ----

type N = { type: string; props: Record<string, unknown>; children: unknown[] }

const node = (v: unknown): N => v as N
const kids = (v: unknown): N[] => {
  const arr = (v as { children?: unknown[] }).children
  return Array.isArray(arr) ? (arr as unknown[]).map(node) : []
}
const text = (v: unknown): string => kids(v).map(c => (typeof c === 'string' ? c : text(c))).join('')
/** a Text's OWN string children (not a nested Text's) — the element that carries
 *  the props the assertion reads. */
const own = (v: unknown): string => kids(v).filter(c => typeof c === 'string').join('')

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

/** a stub table: Box/Text real, Raster present only when asked for — the desktop
 *  and mobile surfaces have none, which is what pickBar switches on. */
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

const HEAT: readonly string[] = [ROLE.ok.fg, ROLE.warn.fg, ROLE.err.fg]
const WIDTHS = [36, 40, 46, 56] as const

/** a Card: a Box with a round border. */
const cardsIn = (root: unknown): N[] => allOf(root, 'Box').filter(b => b.props.borderStyle === 'round')

type Drawn = { type?: string; props?: { flexDirection?: string; gap?: number; label?: string; plain?: boolean; columns?: number }; children?: unknown[] }

/** a row's width: a Text its own text, a horizontal Box its children plus gaps. */
function rowWidth(v: unknown): number {
  if (typeof v !== 'object' || v === null) return 0
  const cur = v as Drawn
  const cs = cur.children ?? []
  if (cur.type === 'Text') return text(cur).length
  if (cur.type === 'Raster') return Number(cur.props?.columns ?? 0)
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
    if (cur.type === 'Text' || cur.type === 'Raster' || (cur.type === 'Box' && cur.props?.flexDirection === 'row')) out.push(rowWidth(cur))
    for (const c of cur.children ?? []) walk(c)
  }
  walk(root)
  return out
}

// ---------- the mounted pane ----------

test('the Usage tab draws the MOCK row and its fixture quota/Jev rows at 46 cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-usage' })
  expect(await ui.find({ text: MOCK }), 'MOCK row').toBeTruthy()
  expect(await ui.find({ text: /sign in at ollama.com/ }), 'fixture ollama note').toBeTruthy()
  // V2.2 draws routing in the board's one-line form, `task → pick conf · rule`
  // (p0 drew a `pick <route> · conf <n> · <rule>` line, three columns instead)
  expect(await ui.find({ text: /→ opencode glm-5/ }), 'fixture Jev pick row').toBeTruthy()
})

test('the Usage tab fits 36, 40, 46 and 56 cols: MOCK row, fixture string, no row over cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-usage' })
  for (const w of [36, 40, 46, 56]) {
    await ui.redraw(paneProps(w))
    expect(await ui.find({ text: MOCK }), `MOCK row at ${w}`).toBeTruthy()
    for (const width of textWidths(await ui.drawn())) {
      expect(width, `row wider than ${w} cells at ${w}: ${width}`).toBeLessThanOrEqual(w)
    }
  }
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: /sign in at ollama.com/ }), 'fixture string at 36').toBeTruthy()
})

test('the terminal surface draws each quota window as a Raster bar', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-usage' })
  const rasters = await ui.findAll({ type: 'Raster' })
  // 10 windows with bars (claude, fable, codex, agy and go, 5h and wk each); the
  // three note rows (opencode, glm-5, ollama) have no window, so no bar
  expect(rasters.length).toBe(10)
  for (const r of rasters) {
    expect(r.props.columns, 'the bar takes the card content width').toBe(33)
    expect(r.props.rows).toBe(1)
  }
})

// ---------- the render, called directly with a stub table ----------

test('C11: the Quota block and the routing block each sit in their own Card', () => {
  const view = usageView()
  for (const cols of [36, 46] as const) {
    const cards = cardsIn(UsageTab(stubEls(true), view, cols))
    expect(cards, `two Cards at ${cols}`).toHaveLength(2)
    for (const card of cards) {
      expect(card.props.backgroundColor).toBe('#151a21')
      expect(card.props.paddingX).toBe(1)
    }
    const texts = allOf(UsageTab(stubEls(true), view, cols), 'Text').map(t => text(t))
    expect(texts.some(t => t === 'QUOTA'), 'the quota section header').toBe(true)
    expect(texts.some(t => t === 'JEV ROUTING'), 'the routing section header').toBe(true)
  }
  // nothing outside a Card but the MOCK row and the footer hint
  const root = node(UsageTab(stubEls(true), view, 46))
  const outside = kids(root).filter(c => c.type !== 'Box')
  expect(outside).toHaveLength(2)
  expect(text(outside[0])).toBe('MOCK · fixture data · not this run')
  expect(text(outside[1]), 'the footer hint').toContain('jev 5 calls · 0 escaped')
})

test('C11: no drawn row is wider than the pane at 36, 40, 46 and 56 cols', () => {
  const view = usageView()
  for (const cols of WIDTHS) {
    const withRaster = UsageTab(stubEls(true), view, cols)
    const wide = rowWidths(withRaster).filter(w => w > cols)
    expect(wide, `over-wide rows at ${cols}: ${JSON.stringify(wide)}`).toEqual([])
    // and with the fallback bars instead of Rasters
    const wide2 = rowWidths(UsageTab(stubEls(false), view, cols)).filter(w => w > cols)
    expect(wide2, `over-wide fallback rows at ${cols}`).toEqual([])
  }
})

test('each window draws gradientBar with the reserve floor, at the card content width', () => {
  const view = usageView()
  const rasters = allOf(UsageTab(stubEls(true), view, 46), 'Raster')
  expect(rasters, 'one bar per window').toHaveLength(10)
  const bw = 42 - 9 // the card content width less the label, the share and two gaps
  const claude5h = rasters.find(r => r.props.key === 'quota-0-5h')
  expect(claude5h?.props.columns).toBe(bw)
  expect(claude5h?.props.cells, 'the 5h gradient with its floor mark').toBe(gradientBar(38, bw, HEAT, 80).cells)
  const claudeWk = rasters.find(r => r.props.key === 'quota-0-wk')
  expect(claudeWk?.props.cells, 'the week gradient with its floor mark').toBe(gradientBar(61, bw, HEAT, 80).cells)
  const agy5h = rasters.find(r => r.props.key === 'quota-3-5h')
  expect(agy5h?.props.cells, 'the spent window has no floor to mark').toBe(gradientBar(100, bw, HEAT, undefined).cells)
  // a bar without a floor really differs from one with it
  expect(gradientBar(38, bw, HEAT, undefined).cells === claude5h?.props.cells).toBe(false)
})

test('without a Raster each window falls back to a SmoothBar in its role colour', () => {
  const view = usageView()
  const tree = UsageTab(stubEls(false), view, 46)
  expect(allOf(tree, 'Raster'), 'no Raster without one').toHaveLength(0)
  const bars = allOf(tree, 'Text').filter(t => /[█▏▎▍▌▋▊▉]/.test(text(t)))
  expect(bars.length, 'one SmoothBar per window').toBe(10)
  const roles = [...new Set(bars.map(b => b.props.color))]
  expect(roles.sort(), 'ok, warn and err roles only').toEqual([ROLE.ok.fg, ROLE.warn.fg, ROLE.err.fg].sort())
  // the spent window is a full err bar
  const full = bars.filter(b => text(b).startsWith('████'))
  expect(full.length, 'the 100% windows').toBeGreaterThan(0)
})

test('barRole and isExhausted follow the mockup: spent is err, 72% is warn, 38% is ok', () => {
  expect(barRole(38)).toBe('ok')
  expect(barRole(61)).toBe('warn')
  expect(barRole(72)).toBe('warn')
  expect(barRole(100)).toBe('err')
  const view = usageView()
  const agy = view.rows.find(r => r.name === 'agy')
  expect(agy != null && isExhausted(agy)).toBe(true)
  expect(view.rows.filter(r => isExhausted(r)).map(r => r.name)).toEqual(['agy'])
})

test('the exhausted agent\'s name row is in the err colour and its 5h share in err too', () => {
  const view = usageView()
  const tree = UsageTab(stubEls(true), view, 46)
  const names = allOf(tree, 'Text').filter(t => /^(claude \(all\)|codex|agy|opencode)$/.test(own(t)))
  expect(names.map(own), 'one name Text per top-level agent').toEqual(['claude (all)', 'codex', 'agy', 'opencode'])
  const agy = names.find(t => own(t) === 'agy')
  expect(agy?.props.color, 'the spent name').toBe(ROLE.err.fg)
  for (const other of names.filter(t => own(t) !== 'agy')) expect(other.props.color, `${own(other)} is not err`).toBeUndefined()
  const share = allOf(tree, 'Text').find(t => own(t) === '   ⊘')
  expect(share?.props.color, 'the spent 5h share').toBe(ROLE.err.fg)
})

test('the legend chips are drawn in their real role colours', () => {
  const view = usageView()
  const chips = allOf(UsageTab(stubEls(true), view, 46), 'Text')
    .filter(t => /^(<50%|50–80|>80 ⊘|▄ floor)$/.test(text(t).trim()) && (text(t).startsWith(' ') || text(t).endsWith(' ')))
  const labels = chips.map(c => text(c).trim())
  expect(labels).toEqual(['<50%', '50–80', '>80 ⊘', '▄ floor'])
  expect(chips.map(c => c.props.backgroundColor)).toEqual([ROLE.ok.tint, ROLE.warn.tint, ROLE.err.tint, ROLE.idle.tint])
  expect(chips.map(c => c.props.color)).toEqual([ROLE.ok.fg, ROLE.warn.fg, ROLE.err.fg, ROLE.idle.fg])
  // all four fit even at 36 cols (32 cells of card content), so the legend never
  // drops a chip on the narrowest pane
  const narrow = allOf(UsageTab(stubEls(true), view, 36), 'Text').map(t => text(t).trim())
  for (const label of ['<50%', '50–80', '>80 ⊘', '▄ floor']) {
    expect(narrow, `the ${label} chip at 36`).toContain(label)
  }
})

test('routing rows read `task → pick conf · rule`, with the pick in the accent colour', () => {
  const view = usageView()
  const tree = UsageTab(stubEls(true), view, 46)
  const texts = allOf(tree, 'Text').map(t => text(t))
  const row = texts.find(t => t.startsWith('P4 → '))
  expect(row, 'the first decision row').toBeTruthy()
  expect(row).toContain('P4 → codex sol high · 0.81')
  expect(row).toContain('top pick')
  expect(row?.length).toBeLessThanOrEqual(42)
  const picks = new Set(view.decisions.map(d => d.pickShort ?? d.pick))
  const pickEls = allOf(tree, 'Text').filter(t => picks.has(text(t)))
  expect(pickEls.length, 'one pick per decision').toBe(5)
  for (const pick of pickEls) expect(pick.props.color, 'the picked route is accented').toBe(ROLE.live.fg)
  // the candidates line only appears when the whole list fits
  expect(texts.some(t => t.includes('codex gpt-6.1-sol high · claude sonnet medium')), 'no cut candidate list at 46').toBe(false)
})

test('the quota rows still show the fixture: names, resets, notes and the source line', () => {
  const view = usageView()
  const texts = allOf(UsageTab(stubEls(true), view, 46), 'Text').map(t => text(t))
  expect(texts.some(t => t.includes('└ go')), 'a pool row').toBe(true)
  expect(texts.some(t => t.includes('2h10m')), 'the claude reset').toBe(true)
  expect(texts.some(t => t.includes('sign in at ollama.com')), 'the ollama note').toBe(true)
  expect(texts.some(t => t.includes('usage.json 17:42')), 'the dim source line').toBe(true)
  expect(texts.some(t => t.includes('candidates for the selected row')), 'the routing hint').toBe(true)
})