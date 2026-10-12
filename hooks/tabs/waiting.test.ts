// Waiting tab tests (plan dashboard-v2-p0b, V2.3): the mounted pane over the
// Checkout v2 decision, plus the render called directly with a stub element table
// so the assertions read the elements WaitingTab actually built — the warn Card
// with its solid header chip, the option chips (accent for the recommended one,
// ok for the picked one), the a/b/c ActionButtons with their hotkeys, the `⋯`
// reveal, and the widths.
import { test, expect } from 'claude-code/testing'
import type { Elements } from 'claude-code'
import { mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './../testkit'
import { ASK_CAP, WaitingTab, askKey, askView, emptyWaitingView, optionChipRole, readPending, runWaitView, waitingActions, waitingAsks, waitingMoreKey, waitingPick, waitingPickKey, waitingView } from './waiting'
import type { PendingAsk, RunFn, TabUi, WaitOption } from './waiting'
import { PENDING_CAPTURED } from './waiting.pending.fixture'
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

// ---------- a stub element table ----

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
    Button: (props: Record<string, unknown>) => ({ type: 'Button', props, children: [] }),
  } as unknown as Elements[keyof Elements]
}

/** the local view state as a Map (C12). */
function fakeUi(): TabUi & { map: Map<string, string> } {
  const map = new Map<string, string>()
  return { map, get: (key: string) => map.get(key), set: (key: string, value: string) => { map.set(key, value) } }
}

const WIDTHS = [36, 40, 46, 56] as const

/** a Card: a Box with a round border. */
const cardsIn = (root: unknown): N[] => allOf(root, 'Box').filter(b => b.props.borderStyle === 'round' && !/^(dash-tab|set-scope)-/.test(String(b.props?.key ?? '')))

type Drawn = { type?: string; props?: { flexDirection?: string; gap?: number; label?: string; plain?: boolean }; children?: unknown[] }

/** a row's width: a Text its own text, a horizontal Box its children plus gaps,
 *  a Button its drawn label with the engine's `[ ]` chrome. */
function rowWidth(v: unknown): number {
  if (typeof v !== 'object' || v === null) return 0
  const cur = v as Drawn
  const cs = cur.children ?? []
  if (cur.type === 'Text') return text(cur).length
  if (cur.type === 'Button') return String(cur.props?.label ?? '').length + 4
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

const buttonByKey = (root: unknown, key: string): N | undefined =>
  allOf(root, 'Button').find(b => b.props.key === key)

/** the kit's fold button (ActionButtons names it after the kept action). */
const more = (root: unknown): N | undefined =>
  allOf(root, 'Button').find(b => String(b.props.label).startsWith('⋯'))

// ---------- the mounted pane ----------

test('the Waiting tab draws the MOCK row and its fixture decision card at 46 cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-waiting' })
  expect(await ui.find({ text: MOCK }), 'MOCK row').toBeTruthy()
  expect(await ui.find({ text: /tax_lines/ }), 'fixture question').toBeTruthy()
  expect(await ui.find({ text: /recommended/ }), 'fixture recommendation').toBeTruthy()
})

test('the Waiting tab fits 36, 40, 46 and 56 cols: wrapped prose shows whole, no row over cols', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-waiting' })
  for (const w of [36, 40, 46, 56]) {
    await ui.redraw(paneProps(w))
    expect(await ui.find({ text: MOCK }), `MOCK row at ${w}`).toBeTruthy()
    for (const width of textWidths(await ui.drawn())) {
      expect(width, `row wider than ${w} cells at ${w}: ${width}`).toBeLessThanOrEqual(w)
    }
  }
  await ui.redraw(paneProps(36))
  expect(await ui.find({ text: /How should P6 proceed\?/ }), 'question tail at 36').toBeTruthy()
  expect(await ui.find({ text: /P6 brief stays r1\./ }), 'recommended explanation tail at 36').toBeTruthy()
})

test('the decision card is the only bordered box and the recommended option is marked', { plugins: [worldFixtures] }, async ($, on) => {
  mockFs(on, worldFiles())
  mockWorld(on)
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-waiting' })
  const boxes = await ui.findAll({ type: 'Box' })
  expect(boxes.filter(b => b.props.borderStyle === 'round' && !/^(dash-tab|set-scope)-/.test(String(b.props?.key ?? ''))).length, 'exactly one bordered box').toBe(1)
  expect(await ui.find({ text: /recommended/ }), 'recommended option marked').toBeTruthy()
})

// ---------- the render, called directly with a stub table ----------

test('C11: the decision is one warn Card, and nothing but the MOCK row and the footer sits outside it', () => {
  const view = waitingView()
  for (const cols of [36, 46] as const) {
    const cards = cardsIn(WaitingTab(stubEls(), view, cols))
    expect(cards, `one Card at ${cols}`).toHaveLength(1)
    expect(cards[0]?.props.borderColor, 'the warn border').toBe(ROLE.warn.fg)
    expect(cards[0]?.props.backgroundColor, 'the panel fill').toBe('#151a21')
    expect(cards[0]?.props.paddingX).toBe(1)
    const root = node(WaitingTab(stubEls(), view, cols))
    const outside = kids(root).filter(c => c.type !== 'Box')
    expect(outside.map(t => text(t))[0]).toBe('MOCK · fixture data · not this run')
    expect(text(outside[1] ?? node(null)), 'the footer hint').toContain('wake: off')
  }
  const chip = allOf(WaitingTab(stubEls(), view, 46), 'Text').find(t => text(t) === ' ! DECISION ')
  expect(chip?.props.backgroundColor, 'the solid warn chip').toBe(ROLE.warn.fg)
  expect(chip?.props.color).toBe('#0e1116')
  expect(chip?.props.bold).toBe(true)
})

test('C11: no drawn row is wider than the pane at 36, 40, 46 and 56 cols', () => {
  const view = waitingView()
  const ui = fakeUi()
  for (const cols of WIDTHS) {
    for (const state of [undefined, ui] as (TabUi | undefined)[]) {
      const wide = rowWidths(WaitingTab(stubEls(), view, cols, state)).filter(w => w > cols)
      expect(wide, `over-wide rows at ${cols}: ${JSON.stringify(wide)}`).toEqual([])
    }
  }
  // and the revealed action rows fit too
  ui.set(waitingMoreKey(), '1')
  for (const cols of WIDTHS) {
    const wide = rowWidths(WaitingTab(stubEls(), view, cols, ui)).filter(w => w > cols)
    expect(wide, `over-wide revealed rows at ${cols}: ${JSON.stringify(wide)}`).toEqual([])
  }
})

test('the option Buttons carry the a b c hotkeys and pick the option into ui', () => {
  const view = waitingView()
  const ui = fakeUi()
  const draw = () => WaitingTab(stubEls(), view, 46, ui)
  const hotkeysOf = (tree: unknown): string[] => allOf(tree, 'Button').map(b => b.props.hotkey).filter(h => typeof h === 'string')
  expect(hotkeysOf(draw()), 'the folded row keeps the primary hotkey').toEqual(['a'])
  ;(more(draw())?.props.onPress as () => void)()
  const buttons = allOf(draw(), 'Button')
  const hotkeys = hotkeysOf(draw())
  expect(hotkeys, 'the three option hotkeys, once the row is revealed').toEqual(['a', 'b', 'c'])
  for (const glyph of ['o', 'l', 'p', 'u', 'e', 'w', '0']) {
    expect(hotkeys, `the tab bar keeps ${glyph}`).not.toContain(glyph)
  }
  const ask = buttons.find(b => b.props.key === 'waiting-ask')
  expect(ask?.props.label, 'the quiet ask action').toBe('? ask in chat')
  expect(ask?.props.hotkey, '? is not a hotkey').toBeUndefined()
  expect(ask?.props.dimColor).toBe(true)

  ;(buttonByKey(draw(), 'waiting-b')?.props.onPress as () => void)()
  expect(ui.get(waitingPickKey()), 'B is picked').toBe('B')
  expect(waitingPick(ui, view)).toBe('B')
  const afterPick = allOf(draw(), 'Button')
  const b = afterPick.find(x => x.props.key === 'waiting-b')
  expect(b?.props.label, 'the picked option shows as picked').toContain('✓')
  // a pick of an option this decision does not offer is not shown
  ui.set(waitingPickKey(), 'Z')
  expect(waitingPick(ui, view)).toBeUndefined()
  expect(waitingPick(undefined, view)).toBeUndefined()
})

test('the recommended option has the solid accent chip; a picked one goes ok', () => {
  const view = waitingView()
  const chipFor = (root: unknown, key: string): N | undefined =>
    allOf(root, 'Text').find(t => text(t) === ` ${key} `)
  const tree = WaitingTab(stubEls(), view, 46)
  const a = chipFor(tree, 'A')
  expect(a?.props.backgroundColor, 'the recommended chip is solid accent').toBe(ROLE.accent.fg)
  expect(optionChipRole({ key: 'A', label: 'x', action: 'x', explanation: 'x', recommended: true } as WaitOption, false)).toEqual({ role: 'accent', solid: true })
  expect(optionChipRole({ key: 'B', label: 'x', action: 'x', explanation: 'x' } as WaitOption, false)).toEqual({ role: 'idle', solid: false })
  for (const key of ['B', 'C']) {
    const idle = chipFor(tree, key)
    expect(idle?.props.backgroundColor, `${key} is the resting chip`).toBe(ROLE.idle.tint)
    expect(idle?.props.color).toBe(ROLE.idle.fg)
  }

  const ui = fakeUi()
  ui.set(waitingPickKey(), 'B')
  const picked = chipFor(WaitingTab(stubEls(), view, 46, ui), 'B')
  expect(picked?.props.backgroundColor, 'the picked chip').toBe(ROLE.ok.fg)
  expect(optionChipRole({ key: 'A', label: 'x', action: 'x', explanation: 'x', recommended: true } as WaitOption, true)).toEqual({ role: 'ok', solid: true })
})

test('the action row folds to the primary plus ⋯ More, and ⋯ reveals the rest one per row', () => {
  const view = waitingView()
  const ui = fakeUi()
  const draw = () => WaitingTab(stubEls(), view, 46, ui)
  const labels = (tree: unknown): string[] => allOf(tree, 'Button').map(b => String(b.props.label))
  expect(labels(draw()), 'folded at 46').toEqual(['a Hold P6', '⋯ More'])
  expect(labels(draw()).length, 'nothing is a dead button').toBe(2)

  ;(more(draw())?.props.onPress as () => void)()
  expect(ui.get(waitingMoreKey()), 'the reveal flag').toBe('1')
  expect(labels(draw()), 'the primary plus the revealed actions').toEqual(['a Hold P6', '⋯ less', 'b Rebase later', 'c Fold', '? ask in chat'])
  ;(buttonByKey(draw(), 'waiting-c')?.props.onPress as () => void)()
  expect(ui.get(waitingPickKey())).toBe('C')
  ;(buttonByKey(draw(), 'waiting-less')?.props.onPress as () => void)()
  expect(ui.get(waitingMoreKey()), 'less collapses it again').toBe('')
  expect(labels(draw())).toEqual(['a Hold P6', '⋯ More'])

  // waitingActions is the spec list the row is built from: three options + the ask
  const specs = waitingActions(view, ui)
  expect(specs.map(s => s.key)).toEqual(['waiting-a', 'waiting-b', 'waiting-c', 'waiting-ask'])
  expect(specs.filter(s => s.primary === true).map(s => s.key), 'the recommended action leads').toEqual(['waiting-a'])
})

test('without ui the recommended state draws and every press is a no-op', () => {
  const view = waitingView()
  const tree = WaitingTab(stubEls(), view, 46)
  const labels = allOf(tree, 'Button').map(b => String(b.props.label))
  expect(labels).toEqual(['a Hold P6', '⋯ More'])
  for (const key of ['waiting-ask', 'waiting-a']) {
    expect(() => (buttonByKey(tree, key)?.props.onPress as () => void)()).not.toThrow()
  }
  expect(allOf(WaitingTab(stubEls(), view, 46), 'Button').map(b => String(b.props.label))).toEqual(labels)
  expect(waitingPickKey()).toBe('waiting:pick') // the key the pick is written under
})

test('the card still shows the fixture: the question, every option and the hints', () => {
  const view = waitingView()
  const texts = allOf(WaitingTab(stubEls(), view, 46), 'Text').map(t => text(t))
  expect(texts.some(t => t.includes('P6 reads the `tax_lines` table')), 'the question').toBe(true)
  expect(texts.some(t => t.includes('How should P6 proceed?'))).toBe(true)
  for (const o of view.options) {
    // the label row is cut to the card width (B's is longer than 46 cols)
    expect(texts.some(t => t.startsWith(o.label.slice(0, 20))), `${o.key}'s label`).toBe(true)
    expect(texts.some(t => t.includes(o.explanation.split('.')[0] ?? '')), `${o.key}'s explanation`).toBe(true)
  }
  expect(texts.some(t => t.includes('mk: pick A')), 'the type-a-pick hint').toBe(true)
  expect(texts.some(t => t.includes('wake: off')), 'the footer hint').toBe(true)
})

// ---------- real pending asks (D8B) ----
// Shape pinned from scripts/mk-bus.py: append_message builds the message
// ({seq, id, ts, run, task, from, to, kind, reply_to, text, ack_level}) and
// get_pending returns the unanswered kind:"ask" messages; cmd_pending wraps
// them as {"ok": true, "run", "pending": [...]} (the /pending handler).

const NOW = '2026-10-12T10:00:00+02:00'

const ASK_A: PendingAsk = {
  id: 'm-1', task: 'T1', text: 'how to proceed?', ts: '2026-10-12T09:57:00+02:00',
}
const ASK_B: PendingAsk = {
  id: 'm-2', task: 'T2', text: 'which lane?', ts: '2026-10-12T09:00:00+02:00',
  options: [{ key: 'A', label: 'Hold', action: 'Hold', recommended: true, explanation: 'wait' }],
}

const runner = (stdout: string, code = 0): RunFn & { calls: number } => {
  const fn = (async () => {
    fn.calls += 1
    return { stdout, code }
  }) as unknown as RunFn & { calls: number }
  fn.calls = 0
  return fn
}

test('the captured real pending output renders its id, task, text and run', () => {
  const payload = JSON.parse(PENDING_CAPTURED) as { ok: boolean; pending: PendingAsk[] }
  expect(payload.ok).toBe(true)
  const ask = payload.pending[0]
  expect(ask?.id).toBe('m-1')
  expect(ask?.run).toBe('r1')
  const tree = waitingAsks(stubEls(), payload.pending, 46, '2026-10-12T12:26:53Z')
  const texts = allOf(tree, 'Text').map(t => text(t))
  expect(texts.some(t => t.includes('m-1'))).toBe(true)
  expect(texts.some(t => t.includes('T1'))).toBe(true)
  expect(texts.some(t => t.includes('how to proceed?'))).toBe(true)
  expect(texts.some(t => t.includes('3m')), 'age from the captured ts').toBe(true)
  expect(texts.some(t => t.includes('mk-bus.py answer m-1 --run r1')), 'the hint names the run').toBe(true)
  expect(texts.some(t => MOCK.test(t))).toBe(false)
})

test('a seeded pending ask renders its id, task, text and age', () => {
  const tree = waitingAsks(stubEls(), [ASK_A], 46, NOW)
  const texts = allOf(tree, 'Text').map(t => text(t))
  expect(texts.some(t => t.includes('m-1')), 'the id').toBe(true)
  expect(texts.some(t => t.includes('T1')), 'the task').toBe(true)
  expect(texts.some(t => t.includes('how to proceed?')), 'the text').toBe(true)
  expect(texts.some(t => t.includes('3m')), 'the age').toBe(true)
  expect(texts.some(t => MOCK.test(t)), 'no MOCK banner on a real source').toBe(false)
  expect(texts.some(t => t.includes('mk-bus.py answer m-1')), 'the answer hint when there are no options').toBe(true)
})

test('two asks stack, and the one with options keeps the decision layout', () => {
  const tree = waitingAsks(stubEls(), [ASK_A, ASK_B], 46, NOW)
  const texts = allOf(tree, 'Text').map(t => text(t))
  expect(texts.filter(t => t.includes('m-1') || t.includes('m-2')).length, 'both ids').toBeGreaterThanOrEqual(2)
  expect(texts.some(t => t.includes('which lane?'))).toBe(true)
  expect(allOf(tree, 'Button').some(b => String(b.props.label).includes('Hold')), 'the option action button').toBe(true)
  expect(texts.some(t => t.includes('2 waiting')), 'the count').toBe(true)
})

test('an answered ask is gone on the next tick: the empty state, not the fixture', () => {
  const tree = waitingAsks(stubEls(), [], 46, NOW)
  const texts = allOf(tree, 'Text').map(t => text(t))
  expect(texts.some(t => t.includes('nothing waiting'))).toBe(true)
  expect(texts.some(t => MOCK.test(t)), 'a real empty source shows no banner').toBe(false)
  expect(texts.some(t => t.includes('tax_lines')), 'the fixture is not drawn').toBe(false)
})

test('the MOCK banner shows only when no real source is available', () => {
  const mock = allOf(WaitingTab(stubEls(), waitingView(), 46), 'Text').map(t => text(t))
  expect(mock.some(t => MOCK.test(t))).toBe(true)
  const real = allOf(WaitingTab(stubEls(), askView(ASK_A, NOW), 46), 'Text').map(t => text(t))
  expect(real.some(t => MOCK.test(t))).toBe(false)
})

test('bus down falls back to the run snapshot wait, without the banner', () => {
  const view = runWaitView({ question: 'hold the merge?', options: ['yes', 'no'], rec: 'yes', since: '17:41' })
  const texts = allOf(WaitingTab(stubEls(), view, 46), 'Text').map(t => text(t))
  expect(texts.some(t => t.includes('hold the merge?'))).toBe(true)
  expect(texts.some(t => t.includes('yes'))).toBe(true)
  expect(texts.some(t => MOCK.test(t))).toBe(false)
  expect(view.mock).toBe(false)
})

test('the same id in two runs gets two keys, and the hint names the run', () => {
  const a = { ...ASK_A, run: 'r1' }
  const b = { ...ASK_A, run: 'r2', text: 'other run' }
  expect(askKey(a)).not.toBe(askKey(b))
  const texts = allOf(waitingAsks(stubEls(), [a, b], 46, NOW), 'Text').map(t => text(t))
  expect(texts.some(t => t.includes('--run r1'))).toBe(true)
  expect(texts.some(t => t.includes('--run r2'))).toBe(true)
})

test('50 asks cap at 8 cards plus a +N more row, and long, unicode and odd ids fit 46 and 36', () => {
  const many: PendingAsk[] = Array.from({ length: 50 }, (_, i) => ({
    id: i === 7 ? 'm-7-odd' : `m-${String(i)}`,
    run: i % 2 === 0 ? 'r1' : 'r2',
    task: 'T1',
    text: i === 3 ? 'a very long question that would overflow a narrow pane if it were not cut: ' + 'x'.repeat(200) : 'ask',
    ts: NOW,
  }))
  many.push({ id: 'm-ü', run: 'r1', task: 'TΩ', text: 'unicode: 質問 🤔 café', ts: NOW })
  for (const cols of [46, 36]) {
    const tree = waitingAsks(stubEls(), many, cols, NOW)
    const widths = textWidths(tree)
    expect(Math.max(...widths), `${String(cols)} cols`).toBeLessThanOrEqual(cols)
    const texts = allOf(tree, 'Text').map(t => text(t))
    expect(texts.filter(t => t.startsWith('m-')).length, 'capped').toBeLessThanOrEqual(ASK_CAP)
    expect(texts.some(t => t.includes('+') && t.includes('more')), 'the overflow row').toBe(true)
  }
})

/** Independent of `cellWidth`: emoji presentation and CJK/kana/hangul/
 *  fullwidth are 2 cells; marks, ZWJ and variation selectors are 0. */
function refWidth(s: string): number {
  const wide = /\p{Emoji_Presentation}|[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFF00-\uFF60\uFFE0-\uFFE6]/u
  const zero = /\p{Mn}|\u200D|[\uFE00-\uFE0F]/u
  let n = 0
  for (const ch of s) {
    if (zero.test(ch)) continue
    n += wide.test(ch) ? 2 : 1
  }
  return n
}

test('emoji, CJK, ZWJ, combining, tab and ellipsis fit the pane', () => {
  const samples = [
    '🚀'.repeat(40), '✅'.repeat(40), '🫶'.repeat(40), '⚡'.repeat(40), '❌'.repeat(40),
    '🤔'.repeat(40), '質問'.repeat(30), 'ひらがな'.repeat(15), '한국어'.repeat(20),
    '👨‍👩‍👧'.repeat(20), 'é'.repeat(60), 'a\tb '.repeat(20), '…'.repeat(60),
  ]
  for (const cols of [46, 36]) {
    for (const sample of samples) {
      const tree = waitingAsks(stubEls(), [{ id: 'm-1', run: 'r1', task: 'T1', text: sample, ts: NOW }], cols, NOW)
      const widest = Math.max(...allOf(tree, 'Text').map(t => refWidth(text(t))))
      expect(widest, `${JSON.stringify(sample.slice(0, 4))} at ${String(cols)}`).toBeLessThanOrEqual(cols)
    }
  }
})

test('46 and 36 col renders do not overflow', () => {
  for (const cols of [46, 36]) {
    const tree = waitingAsks(stubEls(), [ASK_A, ASK_B], cols, NOW)
    const widths = textWidths(tree)
    expect(Math.max(...widths), `${String(cols)} cols`).toBeLessThanOrEqual(cols)
    expect(allOf(WaitingTab(stubEls(), emptyWaitingView(), cols), 'Text').map(t => text(t)).join('').length).toBeLessThanOrEqual(cols)
  }
})

test('the tick calls the runner once, and a dead bus is down without a second spawn', async () => {
  const seen: string[][] = []
  const live = (async (argv: readonly string[]) => {
    seen.push([...argv])
    return { stdout: PENDING_CAPTURED, code: 0 }
  }) as unknown as RunFn
  const got = await readPending(live, '/plugin', '/tmp/proj')
  expect(seen).toHaveLength(1)
  expect(seen[0]?.slice(2)).toEqual(['pending', '--data', '/tmp/proj'])
  expect(seen[0]?.join(' ').includes('--json')).toBe(false)
  expect(got.live).toBe(true)
  expect(got.asks.map(a => a.id)).toEqual(['m-1'])
  expect(got.asks[0]?.run).toBe('r1')
  const dead = runner('{"ok": false, "error": "bus down"}', 2)
  const down = await readPending(dead, '/plugin')
  expect(dead.calls).toBe(1)
  expect(down.down).toBe(true)
  expect(down.live).toBe(false)
  const empty = runner(JSON.stringify({ ok: true, run: 'r1', pending: [] }))
  const none = await readPending(empty, '/plugin')
  expect(none.live).toBe(true)
  expect(none.asks).toEqual([])
})