// Improve parts tests: ImproveFooter and ImproveTab are called directly with a minimal element
// table, the way todo.test.ts does. Buttons and Inputs are recorded by key so a test can press
// them and see exactly which callbacks fired.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import type { MkImproveFlow } from '../../types'
import { bigNumber, newImproveFlow, wordCount } from '../improve-core'
import { ImproveFooter, ImproveTab, improveView } from './improve'
import type { ImproveKind, ImproveView } from './improve'
import { IMPROVE_FIXTURE } from './improve.fixture'
import { PALETTE, ROLE } from './palette'

const scalarProps = (props: object): Record<string, string | number | boolean> => {
  const out: Record<string, string | number | boolean> = {}
  for (const [k, v] of Object.entries(props)) {
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') out[k] = v
  }
  return out
}
const isNode = (k: RenderChildren): k is RenderNode =>
  typeof k === 'string' || (typeof k === 'object' && k !== null && !Array.isArray(k))
const flatten = (c: unknown): unknown[] => (Array.isArray(c) ? c.flatMap(flatten) : [c])
const kids = (c: RenderChildren | undefined): RenderNode[] | undefined =>
  c == null ? undefined : (flatten(c) as RenderChildren[]).filter(isNode)

const presses = new Map<string, () => void>()
const inputs = new Map<string, { onInput?: (v: string) => void; onSubmit: (v: string) => void; props: Record<string, unknown> }>()
const boxTag = (props: BoxProps & ElementChildren): RenderElement => ({ type: 'Box', props: scalarProps(props), children: kids(props.children) })
const textTag = (props: TextProps & ElementChildren): RenderElement => ({ type: 'Text', props: scalarProps(props), children: kids(props.children) })
const stub = (): RenderElement => ({ type: 'Text', props: {}, children: [] })
// a Button keeps its Text children (a chip) or draws its label; it records its press by key
const buttonTag = (props: { key?: string; label?: string; plain?: true; onPress?: () => void; children?: RenderChildren }): RenderElement => {
  if (props.key != null && props.onPress != null) presses.set(props.key, props.onPress)
  return { type: 'Button', props: scalarProps(props), children: props.children != null ? kids(props.children) : [String(props.label)] } as unknown as RenderElement
}
const inputTag = (props: { key: string; onInput?: (v: string) => void; onSubmit: (v: string) => void }): RenderElement => {
  inputs.set(props.key, { onInput: props.onInput, onSubmit: props.onSubmit, props: props as unknown as Record<string, unknown> })
  return { type: 'Input', props: scalarProps(props), children: [] } as unknown as RenderElement
}
const base = { Box: boxTag, Text: textTag, Button: buttonTag, Select: stub, Link: stub, Code: stub, Markdown: stub, Client: stub, Raster: stub, Image: stub }
const els = { ...base, Input: inputTag } as unknown as Elements['terminal']
const elsNoInput = base as unknown as Elements['terminal']

type Call = [ImproveKind, string | number | undefined, string | undefined]
function ui() {
  const calls: Call[] = []
  return { calls, ui: { improveAct: (k: ImproveKind, a?: string | number, b?: string) => { calls.push([k, a, b]) } } }
}
function footer(view: ImproveView, cols = 46, card = true, table: Elements['terminal'] = els) {
  presses.clear()
  inputs.clear()
  const u = ui()
  return { tree: ImproveFooter(table, view, cols, u.ui, card), calls: u.calls }
}
function tab(view: ImproveView, cols = 46) {
  presses.clear()
  inputs.clear()
  const u = ui()
  return { tree: ImproveTab(els, view, cols, u.ui), calls: u.calls }
}

type N = { type?: string; props?: Record<string, unknown>; children?: unknown[] }
const walkAll = (root: unknown, fn: (n: N) => void): void => {
  if (typeof root !== 'object' || root === null) return
  const c = root as N
  fn(c)
  c.children?.forEach(k => { walkAll(k, fn) })
}
const textNodes = (root: unknown): N[] => {
  const out: N[] = []
  walkAll(root, n => { if (n.type === 'Text' || n.type === 'Button') out.push(n) }) // a plain Button's label is text too
  return out
}
const own = (n: N): string => (n.children ?? []).map(c => (typeof c === 'string' ? c : '')).join('')
const texts = (root: unknown): string[] => textNodes(root).map(own).filter(s => s !== '')
const flat = (root: unknown): string => texts(root).join('\n')
/** the drawn words as one line: a sentence wrapped over several rows reads whole */
const said = (root: unknown): string => texts(root).join(' ')
const textOf = (root: unknown, needle: string): N | undefined => textNodes(root).find(n => own(n).includes(needle))

const textWidth = (n: unknown): number => {
  if (typeof n === 'string') return n.length
  const c = n as N
  return c.type === 'Text' || c.type === 'Button' ? (c.children ?? []).reduce((s: number, k) => s + textWidth(k), 0) : 0
}
/** the cells a Box's padding takes across: paddingX both sides, paddingLeft, paddingRight */
const padOf = (c: N): number => {
  const num = (k: string): number => (typeof c.props?.[k] === 'number' ? (c.props[k] as number) : 0)
  return num('paddingX') * 2 + num('paddingLeft') + num('paddingRight')
}
const widthOf = (n: unknown): number => {
  if (typeof n !== 'object' || n === null) return 0
  const c = n as N
  if (c.type === 'Text' || c.type === 'Button') return textWidth(c)
  if (typeof c.props?.width === 'number') return c.props.width
  const ks = c.children ?? []
  const pad = padOf(c)
  const border = c.props?.borderStyle === 'round' ? 2 : 0
  if (c.type === 'Box' && c.props?.flexDirection === 'row') {
    const gap = typeof c.props.gap === 'number' ? c.props.gap : 0
    return ks.reduce((s: number, k) => s + widthOf(k), 0) + gap * Math.max(0, ks.length - 1) + pad + border
  }
  return ks.reduce((m: number, k) => Math.max(m, widthOf(k)), 0) + pad + border
}
/** every row (Text, Button or horizontal Box) with the padding of the Boxes around it (a Card's own padding is
 *  counted by its content limit) and whether it sits inside a round-bordered Card */
function rows(root: unknown): { w: number; inCard: boolean; text: string }[] {
  const out: { w: number; inCard: boolean; text: string }[] = []
  const walk = (n: unknown, inCard: boolean, inset: number): void => {
    if (typeof n !== 'object' || n === null) return
    const c = n as N
    const isCard = c.type === 'Box' && c.props?.borderStyle === 'round'
    const card = inCard || isCard
    if (c.type === 'Text' || c.type === 'Button' || (c.type === 'Box' && c.props?.flexDirection === 'row')) out.push({ w: widthOf(c) + inset, inCard: card, text: flat(c) })
    const next = c.type === 'Box' && !isCard ? inset + padOf(c) : inset
    c.children?.forEach(k => { walk(k, card, next) })
  }
  walk(root, false, 0)
  return out
}

const flowOf = (over: Partial<MkImproveFlow> = {}): MkImproveFlow => ({ ...IMPROVE_FIXTURE.flow!, ...over })
const viewOf = (over: Partial<ImproveView> = {}): ImproveView => ({ ...IMPROVE_FIXTURE, mock: false, ...over })
const QUESTIONS = [
  { question: 'Who is it for?', options: ['Customers', 'My team', 'Investors'] },
  { question: 'How long should it be?', options: ['One page', 'Short'] },
]

test('the fixture view is the design sample, marked MOCK on the tab only', () => {
  expect(improveView().mock).toBe(true)
  expect(flat(tab(improveView()).tree)).toContain('MOCK · fixture data · not this run')
  expect(flat(tab(viewOf()).tree)).not.toContain('MOCK')
})

test('footer: the solid Improve chip with black bold text and two dashed + tool slots sit above the counts, and no prompt field', () => {
  const { tree, calls } = footer(viewOf({ flow: null, history: [] }))
  const f = flat(tree)
  const chip = textOf(tree, '✦ Improve')
  expect(chip?.props).toMatchObject({ backgroundColor: ROLE.live.fg, color: PALETTE.bg, bold: true })
  expect(texts(tree).filter(s => s === '┊ + tool ┊')).toHaveLength(2)
  // the prompt is Claude Code's own line now: the footer draws no field and no `❯` row for it
  expect(f).not.toContain('❯')
  expect(inputs.get('improve-q')).toBeUndefined()
  presses.get('improve-f-go')?.()
  expect(calls).toEqual([['improve', undefined, undefined]])
})

test('footer: the Undo chip shows only after an accept, beside Improve', () => {
  expect(flat(footer(viewOf({ flow: null, undo: null })).tree)).not.toContain('Undo')
  const { tree, calls } = footer(viewOf({ flow: null, undo: 'the old text' }))
  expect(flat(tree)).toContain('↶ Undo')
  presses.get('improve-f-undo')?.()
  expect(calls).toEqual([['undo', undefined, undefined]])
})

test('footer card: IMPROVED PROMPT with the counts, the struck original, the bright new text, Accept / Run Wizard / Discard', () => {
  const { tree, calls } = footer(viewOf())
  const f = flat(tree)
  expect(f).toContain('✦ IMPROVED PROMPT')
  expect(f).toContain(`${String(wordCount(IMPROVE_FIXTURE.flow!.original))} → ${String(wordCount(IMPROVE_FIXTURE.flow!.upgraded))} words`)
  expect(f).toContain('7 → 25 words')
  const was = textOf(tree, 'Check all the files')
  expect(was?.props).toMatchObject({ dimColor: true, strikethrough: true })
  expect(textOf(tree, 'Run tsc --noEmit')?.props).toMatchObject({ color: PALETTE.fg })
  expect(textOf(tree, '↵ Accept')?.props).toMatchObject({ backgroundColor: ROLE.live.fg, color: PALETTE.bg, bold: true })
  expect(f).toContain('Run Wizard')
  expect(f).toContain('Discard')
  expect(f).not.toContain('esc')
  presses.get('improve-f-accept')?.()
  presses.get('improve-f-wizard')?.()
  presses.get('improve-f-discard')?.()
  expect(calls.map(c => c[0])).toEqual(['accept', 'wizard', 'discard'])
})

test('footer card: hidden on the Improve tab (card false), the rows around it stay', () => {
  const f = flat(footer(viewOf(), 46, false).tree)
  expect(f).not.toContain('IMPROVED PROMPT')
  expect(f).toContain('✦ Improve')
})

test('footer card: working shows a calm line and Discard; an error shows the calm line, Try again and Discard', () => {
  const working = footer(viewOf({ flow: flowOf({ step: 'writing' }) }))
  expect(flat(working.tree)).toContain('Writing the new prompt…')
  expect(flat(footer(viewOf({ flow: flowOf({ step: 'asking' }) })).tree)).toContain('Reading your prompt')
  const err = footer(viewOf({ flow: flowOf({ step: 'error', error: 'Could not improve that one. Your prompt is untouched.' }) }))
  expect(said(err.tree)).toContain('Could not improve that one. Your prompt is untouched.')
  presses.get('improve-f-retry')?.()
  presses.get('improve-f-discard')?.()
  expect(err.calls.map(c => c[0])).toEqual(['retry', 'discard'])
  // an empty error text still draws the calm line
  expect(said(footer(viewOf({ flow: flowOf({ step: 'error', error: '' }) })).tree)).toContain('Your prompt is untouched.')
})

test('wizard questions: options, Other, Skip, the answered count and the go button', () => {
  const flow = flowOf({ step: 'questions', questions: QUESTIONS, answers: { '0': { kind: 'option', text: 'My team' } } })
  const { tree, calls } = footer(viewOf({ flow }))
  const f = flat(tree)
  expect(f).toContain('✦ WIZARD')
  expect(f).toContain('✓ Who is it for?') // answered
  expect(f).toContain('2 How long should it be?')
  expect(f).toContain('✓ My team')
  expect(f).toContain('Other (type it)')
  expect(f).toContain('Skip the rest, upgrade now')
  expect(f).toContain('1 of 2 answered')
  presses.get('improve-f-q1o0')?.()
  presses.get('improve-f-q1other')?.()
  presses.get('improve-f-q1skip')?.()
  presses.get('improve-f-upgrade')?.()
  expect(calls).toEqual([['answer', 1, 'One page'], ['other', 1, undefined], ['skip', 1, undefined], ['go', undefined, undefined]])
  // all answered: the go button reads Upgrade now; an open Other field has its own Input
  const done = flowOf({ step: 'questions', questions: QUESTIONS, other: 1, answers: { '0': { kind: 'skip' }, '1': { kind: 'other', text: 'two pages' } } })
  const d = footer(viewOf({ flow: done }))
  expect(flat(d.tree)).toContain('Upgrade now')
  expect(flat(d.tree)).not.toContain('Skip the rest')
  expect(flat(d.tree)).toContain('✓ Other: two pages')
  inputs.get('improve-other-1')?.onSubmit('three')
  expect(d.calls).toEqual([['other-save', 1, 'three']])
})

test('tab review: three big-number tiles, the button row, WHAT I ADDED and the before / after cards', () => {
  const { tree, calls } = tab(viewOf(), 80)
  const f = flat(tree)
  // three heavy box-drawing rows per tile: 7 words before, 25 after, 4 details
  for (const n of [7, 25, 4]) for (const r of bigNumber(n, 20)) expect(texts(tree), `big ${String(n)}`).toContain(r)
  expect(f).toContain('words before')
  expect(f).toContain('words after')
  expect(f).toContain('details added')
  for (const label of ['↵ Accept', 'Run Wizard', 'Edit', 'Copy', 'Discard']) expect(f).toContain(label)
  expect(f).toContain('WHAT I ADDED')
  expect(texts(tree).filter(s => s === '+')).toHaveLength(4)
  expect(f).toContain('The check to run: tsc --noEmit')
  expect(f).toContain('BEFORE')
  expect(f).toContain('AFTER')
  for (const k of ['improve-t-accept', 'improve-t-wizard', 'improve-t-edit', 'improve-t-copy', 'improve-t-discard']) presses.get(k)?.()
  expect(calls.map(c => c[0])).toEqual(['accept', 'wizard', 'edit', 'copy', 'discard'])
})

test('tab review: before and after sit side by side from 60 columns, stacked below', () => {
  const sideBySide = (cols: number): boolean => {
    const { tree } = tab(viewOf(), cols)
    let isRow = false
    walkAll(tree, n => {
      if (n.type === 'Box' && n.props?.flexDirection === 'row' && texts(n).includes('BEFORE') && texts(n).includes('AFTER')) isRow = true
    })
    return isRow
  }
  expect(sideBySide(80)).toBe(true)
  expect(sideBySide(60)).toBe(true)
  expect(sideBySide(59)).toBe(false)
  expect(sideBySide(46)).toBe(false)
  expect(sideBySide(36)).toBe(false)
})

test('tab review: counts follow the proposal; editing shows the field and the live draft word count', () => {
  const editing = flowOf({ isEditing: true, draft: 'one two three' })
  const { tree, calls } = tab(viewOf({ flow: editing }), 80)
  expect(flat(tree)).toContain('Stop editing')
  expect(flat(tree)).not.toContain('BEFORE')
  expect(inputs.get('improve-edit')?.props.value).toBe('one two three')
  inputs.get('improve-edit')?.onInput?.('one two')
  inputs.get('improve-edit')?.onSubmit('one two')
  expect(calls).toEqual([['draft', 'one two', undefined], ['edit-save', undefined, 'one two']])
  // a count too wide for its tile falls back to plain digits, never a clipped glyph
  const wide = tab(viewOf({ flow: flowOf({ upgraded: 'w '.repeat(1200) }) }), 36)
  expect(flat(wide.tree)).toContain('1200')
})

test('tab empty state: the hint, then the saved improvements; a click reopens one', () => {
  const { tree, calls } = tab(viewOf({ flow: null }))
  const f = flat(tree)
  expect(said(tree)).toContain('Type a prompt in Claude Code’s prompt line and press ✦ Improve.')
  expect(f).toContain('RECENT')
  expect(f).toContain('Check all the files with type')
  presses.get('improve-t-h1')?.()
  expect(calls).toEqual([['open', '1', undefined]])
  // no saved improvements: just the hint
  expect(flat(tab(viewOf({ flow: null, history: [] })).tree)).not.toContain('RECENT')
  // twenty at most
  const many = Array.from({ length: 30 }, (_, i) => ({ id: `m${String(i)}`, at: i * 60_000, original: `prompt ${String(i)}`, upgraded: 'x', added: [] }))
  tab(viewOf({ flow: null, history: many }))
  expect([...presses.keys()].filter(k => k.startsWith('improve-t-h'))).toHaveLength(20)
})

test('tab states: working, questions, error', () => {
  expect(flat(tab(viewOf({ flow: newImproveFlow('a', 'fix it') })).tree)).toContain('Writing the new prompt…')
  expect(flat(tab(viewOf({ flow: flowOf({ step: 'questions', questions: QUESTIONS }) })).tree)).toContain('Skip the rest, upgrade now')
  const err = tab(viewOf({ flow: flowOf({ step: 'error', error: '' }) }))
  expect(said(err.tree)).toContain('Could not improve that one. Your prompt is untouched.')
  presses.get('improve-t-retry')?.()
  expect(err.calls.map(c => c[0])).toEqual(['retry'])
})

test('nothing drawn is wider than the pane at 36, 46 and 80 columns; long text and control characters are cut', () => {
  const long = 'x'.repeat(200)
  const messy = `${long}\u0007\u001b[31m a_very_long_unbroken_word_${long}`
  const flows: (MkImproveFlow | null)[] = [
    null,
    flowOf({ original: messy, upgraded: messy, added: [messy, 'short'] }),
    flowOf({ original: messy, upgraded: messy, isEditing: true, draft: messy }),
    flowOf({ step: 'writing', original: messy }),
    flowOf({ step: 'error', error: messy }),
    flowOf({ step: 'questions', questions: [{ question: messy, options: [long, 'a', 'b', 'c'] }, ...QUESTIONS], answers: { '1': { kind: 'other', text: messy } }, other: 0 }),
  ]
  const history = Array.from({ length: 22 }, (_, i) => ({ id: `h${String(i)}`, at: i * 1e9, original: messy, upgraded: 'x', added: [] }))
  for (const cols of [36, 46, 80]) {
    for (const flow of flows) {
      for (const undo of [null, 'old'] as const) {
        const v = viewOf({ flow, undo, history })
        for (const [name, tree] of [['footer', footer(v, cols, true, elsNoInput).tree], ['tab', tab(v, cols).tree]] as const) {
          // a Card's content is 4 cells narrower than the pane (border + padding); everything else is pane wide
          const over = rows(tree).filter(r => (r.inCard ? r.w > cols - 4 : r.w > cols))
          expect(over.map(r => `${String(r.w)}:${r.text.slice(0, 30)}`), `${name} over-wide rows at ${cols} (${flow?.step ?? 'empty'})`).toEqual([])
          expect(flat(tree)).not.toMatch(/[\u0000-\u0008\u001b]/)
        }
      }
    }
  }
})

test('F2: the open Other field draws the typed draft from the flow and reports every edit', () => {
  const flow = flowOf({ step: 'questions', questions: QUESTIONS, other: 1, otherDraft: 'half typed' })
  const { calls } = footer(viewOf({ flow }))
  expect(inputs.get('improve-other-1')?.props.value).toBe('half typed')
  inputs.get('improve-other-1')?.onInput?.('half typed more')
  inputs.get('improve-other-1')?.onSubmit('half typed more')
  expect(calls).toEqual([['other-draft', 1, 'half typed more'], ['other-save', 1, 'half typed more']])
  // the tab draws the same field
  tab(viewOf({ flow }))
  expect(inputs.get('improve-other-1')?.props.value).toBe('half typed')
})

test('F3: wizard option chips of 40 characters fit inside the padded box at 36, 46 and 80 columns, footer and tab', () => {
  const forty = 'o'.repeat(40)
  const flow = flowOf({
    step: 'questions',
    questions: [{ question: 'Pick one?', options: [forty, `${forty.slice(0, 39)}x`, 'b', 'c'] }, { question: forty, options: ['a', 'b'] }],
    answers: { '0': { kind: 'option', text: forty } },
  })
  for (const cols of [36, 46, 80]) {
    for (const [name, tree] of [['footer', footer(viewOf({ flow }), cols).tree], ['tab', tab(viewOf({ flow }), cols).tree]] as const) {
      const all = rows(tree)
      expect(all.some(r => r.text.includes('ooooooooooooooo')), `${name} drew the options at ${cols}`).toBe(true)
      expect(all.filter(r => (r.inCard ? r.w > cols - 4 : r.w > cols)).map(r => `${String(r.w)}:${r.text}`), `${name} at ${cols}`).toEqual([])
    }
  }
})

test('F8a: the footer card shows the proposal as Accept takes it, the edit draft while editing', () => {
  const editing = flowOf({ isEditing: true, draft: 'my own edit of it' })
  const f = said(footer(viewOf({ flow: editing })).tree)
  expect(f).toContain('my own edit of it')
  expect(f).not.toContain('Run tsc')
  expect(f).toContain('7 → 5 words')
})

test('F8b: an error that kept the earlier rewrite shows it with Try again, Accept and Discard; one without has no Accept', () => {
  const kept = flowOf({ step: 'error', failedStep: 'questions', error: '' })
  for (const [name, draw, retry, accept] of [
    ['footer', () => footer(viewOf({ flow: kept })), 'improve-f-retry', 'improve-f-accept'],
    ['tab', () => tab(viewOf({ flow: kept })), 'improve-t-retry', 'improve-t-accept'],
  ] as const) {
    const view = draw() // drawn one at a time: each draw resets the recorded presses
    expect(said(view.tree), name).toContain('Run tsc --noEmit and list every file')
    expect(said(view.tree), name).toContain('Your prompt is untouched.')
    presses.get(retry)?.()
    presses.get(accept)?.()
    expect(view.calls.map(c => c[0]), name).toEqual(['retry', 'accept'])
  }
  const bare = flowOf({ step: 'error', failedStep: 'rewrite', error: '', upgraded: '', added: [] })
  footer(viewOf({ flow: bare }))
  expect(presses.has('improve-f-accept')).toBe(false)
  tab(viewOf({ flow: bare }))
  expect(presses.has('improve-t-accept')).toBe(false)
})

test('F9: no Box shares a key with the Button inside it, and every key is drawn once', () => {
  const history = Array.from({ length: 3 }, (_, i) => ({ id: `h${String(i)}`, at: i, original: `old ${String(i)}`, upgraded: 'x', added: [] }))
  for (const tree of [tab(viewOf({ flow: null, history })).tree, footer(viewOf({ flow: flowOf({ step: 'questions', questions: QUESTIONS }) })).tree, tab(viewOf()).tree]) {
    const keys: string[] = []
    walkAll(tree, n => { if (n.props?.key != null && n.type !== 'Text') keys.push(String(n.props.key)) })
    expect(keys.filter((k, i) => keys.indexOf(k) !== i), 'duplicate keys').toEqual([])
  }
})

test('F10: the footer chip row drops the slots that do not fit, and the Improve chip always shows', () => {
  for (const cols of [36, 39, 40, 46]) {
    const { tree } = footer(viewOf({ flow: null, undo: 'x' }), cols)
    expect(textOf(tree, '✦ Improve'), `Improve at ${cols}`).toBeTruthy()
    expect(textOf(tree, '↶ Undo'), `Undo at ${cols}`).toBeTruthy()
  }
  // 36 cols with Undo: Improve + Undo + one slot fit (11 + 8 + 10 + gaps), the second slot is dropped
  expect(texts(footer(viewOf({ flow: null, undo: 'x' }), 36).tree).filter(s => s.includes('+ tool'))).toHaveLength(1)
  expect(texts(footer(viewOf({ flow: null, undo: null }), 36).tree).filter(s => s.includes('+ tool'))).toHaveLength(2)
})

test('without an Input element the footer and the tab still draw every non-field row', () => {
  const { tree } = footer(viewOf({ flow: null, undo: 'x' }), 46, true, elsNoInput)
  expect(textOf(tree, '✦ Improve')).toBeTruthy()
  expect(textOf(tree, '↶ Undo')).toBeTruthy()
  expect(flat(tab(viewOf()).tree)).toContain('BEFORE AND AFTER')
})
