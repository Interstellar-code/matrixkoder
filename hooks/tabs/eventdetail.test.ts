// Event detail card tests (Events tab): the pure EventDetailPanel render
// called with a stub element table — every present field drawn, missing
// fields left out (no `undefined`/`null` text), the full text one Text per
// line, an answer's ask and an ask's answer, the problem line of a bad row
// and the two pressable closes.
import { test, expect } from 'claude-code/testing'
import type { Elements } from 'claude-code'
import { EventDetailPanel } from './eventdetail'
import { iconWidth } from './kit'
import type { EventItem } from './events'

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

/** every Text and Button label the card draws, one per line */
const allText = (drawn: unknown): string =>
  allOf(drawn, 'Text').map(t => text(t)).join('\n') + '\n' + allOf(drawn, 'Button').map(b => String(b.props.label ?? '')).join('\n')

/** the dim key of every field row: a row Box whose first Text is dim */
const keysOf = (drawn: unknown): string[] =>
  allOf(drawn, 'Box')
    .filter(b => b.props.flexDirection === 'row' && b.props.gap === 1)
    .map(b => kids(b)[0])
    .filter((t): t is N => t != null && t.type === 'Text' && t.props.dimColor === true)
    .map(t => text(t))

/** the card's text lines (the full text, one Text per line) */
const bodyLines = (drawn: unknown): string[] =>
  allOf(drawn, 'Text').filter(t => String(t.props.key ?? '').startsWith('ev-detail-line-')).map(t => text(t))

function press(drawn: unknown, key: string): void {
  const b = allOf(drawn, 'Button').find(x => String(x.props.key) === key)
  expect(b, `button ${key}`).toBeDefined()
  ;(b?.props.onPress as () => void)()
}

/** A row with every field the card can draw. */
const FULL: EventItem = {
  id: 'm-7', src: 'bus', t: '06:01', date: '2026-10-19T06:01:30.250+02:00', kind: 'answer',
  from: 'p3', to: 'orch', task: 'T1.2', chan: 'bus', reply_to: 'm-3', ack: 2, bad: true, by: 'orchestrator',
  text: 'use the cache', full: 'use the cache\nand drop the recompute',
}

test('the card draws every present field', () => {
  const drawn = EventDetailPanel(stubEls(), FULL, undefined, 80)
  const flat = allText(drawn)
  expect(flat).toContain('m-7 · answer') // the header
  expect(keysOf(drawn)).toEqual(['when', 'source', 'from', 'task', 'reply_to', 'ack', 'problem', 'by'])
  expect(flat).toContain('2026-10-19 06:01:30+02:00') // the full date and time
  expect(flat).toContain('bus')
  expect(flat).toContain('p3 → orch')
  expect(flat).toContain('T1.2')
  expect(flat).toContain('m-3')
  expect(flat).toContain('read ✓✓')
  expect(flat).toContain('! yes')
  expect(flat).toContain('orchestrator')
  expect(bodyLines(drawn)).toEqual(['use the cache', 'and drop the recompute']) // the full text, not the cut one
  const top = allOf(drawn, 'Button').find(b => String(b.props.key) === 'ev-detail-close-top-m-7')
  expect(top?.props.label, 'the top-right close is the icon alone').toBe('✕')
  expect(flat).not.toContain('✕ close')
  expect(flat).toContain('esc close')
})

test('a row with only id, kind, text and time shows no empty field line', () => {
  const drawn = EventDetailPanel(stubEls(), { id: 'm-1', kind: 'info', t: '06:00', text: 'hello' }, undefined, 46)
  const flat = allText(drawn)
  expect(flat).not.toMatch(/undefined|null|NaN/)
  expect(keysOf(drawn)).toEqual(['when']) // the clock time is the one thing it knows
  expect(flat).toContain('06:00')
  expect(bodyLines(drawn)).toEqual(['hello'])
  // a row with no time either, and empty strings for the optional fields, draws none of them
  const bare = EventDetailPanel(stubEls(), { id: 'm-2', kind: 'info', t: '', text: 'x', from: '', to: '', task: '', by: '', reply_to: '' }, null, 46)
  expect(keysOf(bare)).toEqual([])
  expect(allText(bare)).not.toMatch(/undefined|null|NaN/)
})

test('a row without a task falls back to its lane, and one on the run source has no ack line', () => {
  const lane = EventDetailPanel(stubEls(), { id: 'm-1', src: 'bus', kind: 'ask', t: '06:00', text: 'q', lane: 'p2', ack: 0 }, undefined, 46)
  expect(keysOf(lane)).toEqual(['when', 'source', 'lane', 'ack'])
  expect(allText(lane)).toContain('none') // a bus row nobody acked
  const all = EventDetailPanel(stubEls(), { id: 'm-1', src: 'bus', kind: 'ask', t: '06:00', text: 'q', lane: 'all', ack: 1 }, undefined, 46)
  expect(keysOf(all)).toEqual(['when', 'source', 'ack'])
  expect(allText(all)).toContain('delivered ✓')
  const run = EventDetailPanel(stubEls(), { id: 'r-1', src: 'run', kind: 'run', t: '06:00', text: 'started', ack: 2 }, undefined, 46)
  expect(keysOf(run)).toEqual(['when', 'source'])
})

test('a multi-line full text draws one line each, a long one wraps', () => {
  const drawn = EventDetailPanel(stubEls(), { id: 'm-1', kind: 'info', t: '06:00', text: 'a b c', full: 'first line\nsecond line\n\nfourth line' }, undefined, 46)
  const lines = allOf(drawn, 'Text').filter(t => String(t.props.key ?? '').startsWith('ev-detail-line-'))
  expect(lines.map(t => text(t).trim())).toEqual(['first line', 'second line', '', 'fourth line'])
  expect(lines.every(t => t.props.wrap === 'wrap'), 'wrapped, never cut').toBe(true)
  // a row without `full` draws its one-line text
  expect(bodyLines(EventDetailPanel(stubEls(), { id: 'm-1', kind: 'info', t: '06:00', text: 'plain' }, undefined, 46))).toEqual(['plain'])
  // an empty text draws no text lines
  expect(bodyLines(EventDetailPanel(stubEls(), { id: 'm-1', kind: 'info', t: '06:00', text: '' }, undefined, 46))).toEqual([])
})

test('an answer shows the ask it replies to, an ask the answer to it', () => {
  const ask: EventItem = { id: 'm-1', kind: 'ask', t: '06:00', text: 'cache or recompute?', full: 'cache or\nrecompute?' }
  const answer: EventItem = { id: 'm-2', kind: 'answer', t: '06:01', text: 'use the cache', reply_to: 'm-1' }
  const a = EventDetailPanel(stubEls(), answer, ask, 46)
  expect(allText(a)).toContain('answers m-1')
  expect(allText(a)).toContain('cache or\nrecompute?') // the ask's full text
  const q = EventDetailPanel(stubEls(), ask, answer, 46)
  expect(allText(q)).toContain('answered by m-2')
  expect(allText(q)).toContain('use the cache')
  // a missing counterpart leaves the section out, and an info row never shows one
  expect(allText(EventDetailPanel(stubEls(), answer, undefined, 46))).not.toMatch(/answers|answered by/)
  expect(allText(EventDetailPanel(stubEls(), ask, null, 46))).not.toMatch(/answers|answered by/)
  expect(allText(EventDetailPanel(stubEls(), { id: 'm-3', kind: 'info', t: '06:02', text: 'fyi' }, answer, 46))).not.toMatch(/answers|answered by/)
  // the related text is cut to ~300 characters
  const long = EventDetailPanel(stubEls(), answer, { ...ask, full: 'x'.repeat(500) }, 46)
  const cut = allOf(long, 'Text').map(t => text(t)).find(t => t.startsWith('xxx'))
  expect(cut?.length).toBe(300)
  expect(cut?.endsWith('…')).toBe(true)
})

test('only a bad row shows a problem line', () => {
  expect(keysOf(EventDetailPanel(stubEls(), { id: 'm-1', kind: 'ring-failed', t: '06:00', text: 'x', bad: true }, undefined, 46))).toContain('problem')
  expect(keysOf(EventDetailPanel(stubEls(), { id: 'm-1', kind: 'info', t: '06:00', text: 'x', bad: false }, undefined, 46))).not.toContain('problem')
  expect(keysOf(EventDetailPanel(stubEls(), { id: 'm-1', kind: 'info', t: '06:00', text: 'x' }, undefined, 46))).not.toContain('problem')
})

test('both close buttons call onClose', () => {
  let closed = 0
  const drawn = EventDetailPanel(stubEls(), { id: 'm-1', kind: 'info', t: '06:00', text: 'x' }, undefined, 46, () => { closed += 1 })
  press(drawn, 'ev-detail-close-top-m-1')
  press(drawn, 'ev-detail-close-m-1')
  expect(closed).toBe(2)
  // no callback: a press does nothing and does not throw
  const bare = EventDetailPanel(stubEls(), { id: 'm-1', kind: 'info', t: '06:00', text: 'x' }, undefined, 46)
  press(bare, 'ev-detail-close-m-1')
})

test('the header yields to the close button at a narrow width', () => {
  const header = (cols: number): string => {
    const drawn = EventDetailPanel(stubEls(), { id: 'm-1234567', kind: 'ring-failed', t: '06:00', text: 'x' }, undefined, cols)
    return text(allOf(drawn, 'Text').find(t => t.props.bold === true))
  }
  expect(header(80)).toBe('m-1234567 · ring-failed')
  expect(header(24).length).toBeLessThanOrEqual(24 - 4 - 1 - iconWidth('close'))
  expect(header(24).endsWith('…')).toBe(true)
})
