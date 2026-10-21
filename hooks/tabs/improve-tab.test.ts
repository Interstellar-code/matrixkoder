// The Improve TAB's render tests: the review screen and the wizard's states, pinned to the
// approved slides at 46 columns (RunTab, RunQuestions, RunAsking, RunWriting, RunFailed).
// ImproveTab is called directly with a minimal element table, the way todo.test.ts does.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import type { MkImproveFlow } from '../../types'
import { bigNumber, PLAN_PROMPT_CHIP, planPromptNote } from '../improve-core'
import { ImproveTab } from './improve-tab'
import type { ImproveKind, ImproveView } from './improve'
import { IMPROVE_FIXTURE } from './improve.fixture'

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
const boxTag = (props: BoxProps & ElementChildren): RenderElement => ({ type: 'Box', props: scalarProps(props), children: kids(props.children) })
const textTag = (props: TextProps & ElementChildren): RenderElement => ({ type: 'Text', props: scalarProps(props), children: kids(props.children) })
const stub = (): RenderElement => ({ type: 'Text', props: {}, children: [] })
// a Button keeps its Text children (a chip) or draws its label; it records its press by key
const buttonTag = (props: { key?: string; label?: string; plain?: true; onPress?: () => void; children?: RenderChildren }): RenderElement => {
  if (props.key != null && props.onPress != null) presses.set(props.key, props.onPress)
  return { type: 'Button', props: scalarProps(props), children: props.children != null ? kids(props.children) : [String(props.label)] } as unknown as RenderElement
}
const base = { Box: boxTag, Text: textTag, Button: buttonTag, Select: stub, Link: stub, Code: stub, Markdown: stub, Client: stub, Raster: stub, Image: stub }
const els = base as unknown as Elements['terminal']

type Call = [ImproveKind, string | number | undefined, string | undefined]
function ui() {
  const calls: Call[] = []
  return { calls, ui: { improveAct: (k: ImproveKind, a?: string | number, b?: string) => { calls.push([k, a, b]) } } }
}
function tab(view: ImproveView, cols = 46) {
  presses.clear()
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
  walkAll(root, n => { if (n.type === 'Text' || n.type === 'Button') out.push(n) })
  return out
}
const own = (n: N): string => (n.children ?? []).map(c => (typeof c === 'string' ? c : '')).join('')
/** the drawn rows, in the order they are drawn */
const rows = (root: unknown): string[] => textNodes(root).map(own).filter(s => s !== '')
/** the drawn words as one line: a sentence wrapped over several rows reads whole */
const said = (root: unknown): string => rows(root).join(' ')

const viewOf = (over: Partial<ImproveView> = {}): ImproveView => ({ ...IMPROVE_FIXTURE, mock: false, ...over })
const flowOf = (over: Partial<MkImproveFlow> = {}): MkImproveFlow => ({ ...IMPROVE_FIXTURE.flow!, ...over })

// the RunTab slide's own numbers: 7 words before, 25 after, 3 details added
const BEFORE = 'Check all the files with type errors.'
const AFTER = "Run tsc --noEmit and list every file with type errors. Give each error's file, line and cause, grouped by root cause. Don't edit anything yet."
const ADDED = ['the exact command: tsc --noEmit', 'file, line and cause per error', 'do not edit anything yet']
const slideFlow = (over: Partial<MkImproveFlow> = {}): MkImproveFlow =>
  flowOf({ step: 'review', original: BEFORE, upgraded: AFTER, added: ADDED, isEditing: false, draft: '', error: '', note: '', ...over })

const QUESTIONS = [
  { question: 'What should I do with the errors?', options: ['List them only', 'Fix the easy ones', 'Fix everything'] },
  { question: 'How should I report them?', options: ['By root cause', 'By file', 'Just a count'] },
  { question: 'Which files?', options: ['Whole repo', 'Changed files', 'src/ only'] },
]

test('tab review: the RunTab slide rows at 46 columns', () => {
  const { tree } = tab(viewOf({ flow: slideFlow() }), 46)
  const raw = rows(tree)
  const r = raw.map(s => s.trim())
  const flat = r.join('\n')
  // three big-number tiles: 7 before, 25 after, 3 added
  for (const n of [7, 25, 3]) for (const row of bigNumber(n, 12)) expect(raw, `big ${String(n)}`).toContain(row)
  expect(r).toContain('before')
  expect(r).toContain('after')
  expect(r).toContain('added')
  // the chip row: Accept, Run Wizard, Edit, Copy, Discard
  for (const label of ['↵ Accept', 'Run Wizard', 'Edit', 'Copy', 'Discard']) expect(r).toContain(label)
  // What I added, then Before and after
  expect(r).toContain('WHAT I ADDED')
  expect(r.filter(s => s === '+')).toHaveLength(3)
  for (const a of ADDED) expect(flat).toContain(a)
  expect(r).toContain('BEFORE')
  expect(r).toContain('AFTER')
  // the order the slide draws: tiles, chips, What I added, then the before and after cards
  const at = (needle: string): number => r.findIndex(x => x === needle)
  expect(at('↵ Accept')).toBeLessThan(at('WHAT I ADDED'))
  expect(at('WHAT I ADDED')).toBeLessThan(at('BEFORE'))
  expect(at('BEFORE')).toBeLessThan(at('AFTER'))
  // the before and after cards are stacked at 46 columns
  let sideBySide = false
  walkAll(tree, n => {
    if (n.type === 'Box' && n.props?.flexDirection === 'row' && rows(n).map(s => s.trim()).includes('BEFORE') && rows(n).map(s => s.trim()).includes('AFTER')) sideBySide = true
  })
  expect(sideBySide).toBe(false)
})

test('R-2: the error line names the call that failed', () => {
  // a failed questions call says so; a failed rewrite keeps the rewrite line
  const q = tab(viewOf({ flow: slideFlow({ step: 'error', failedStep: 'questions', error: '' }) }), 46)
  expect(said(q.tree)).toContain('The wizard could not write its questions. Your prompt line is untouched.')
  // a kept rewrite is still on show, with Try again and Accept
  expect(said(q.tree)).toContain(AFTER)
  for (const k of ['improve-t-retry', 'improve-t-accept', 'improve-t-discard']) presses.get(k)?.()
  expect(q.calls.map(c => c[0])).toEqual(['retry', 'accept', 'discard'])
  const r = tab(viewOf({ flow: slideFlow({ step: 'error', failedStep: 'rewrite', error: '' }) }), 46)
  expect(said(r.tree)).toContain('Could not improve that one. Your prompt is untouched.')
  expect(said(r.tree)).not.toContain('could not write its questions')
})

test('a waiting Plan prompt shows its chip and its line on the tab', () => {
  const flow = slideFlow({ note: planPromptNote('pause', 'MatrixKoder · pause task B1.2') })
  const { tree, calls } = tab(viewOf({ flow }), 46)
  const r = rows(tree).map(s => s.trim())
  expect(r).toContain(PLAN_PROMPT_CHIP)
  expect(said(tree)).toContain('A Plan prompt is waiting: MatrixKoder · pause task B1.2')
  presses.get('improve-t-plan')?.()
  expect(calls.map(c => c[0])).toEqual(['plan-prompt'])
  // no waiting prompt, no chip and no line
  const plain = tab(viewOf({ flow: slideFlow() }), 46)
  expect(rows(plain.tree).map(s => s.trim())).not.toContain(PLAN_PROMPT_CHIP)
  expect(said(plain.tree)).not.toContain('A Plan prompt is waiting')
})

test('R-8: the tab draws the same heads as the footer card', () => {
  // the questions state: the wizard's head with the answered count beside it
  const q = tab(viewOf({ flow: flowOf({ step: 'questions', questions: QUESTIONS, answers: {} }) }), 46)
  const qf = said(q.tree)
  expect(qf).toContain('✦ WIZARD')
  expect(qf).toContain('0 of 3 answered')
  expect(qf).not.toContain('A few quick questions')
  // the working states: the head the working card names
  const asking = tab(viewOf({ flow: flowOf({ step: 'asking' }) }), 46)
  expect(said(asking.tree)).toContain('✦ IMPROVING')
  expect(said(asking.tree)).toContain('Reading your prompt and working out what to ask…')
  const writing = tab(viewOf({ flow: flowOf({ step: 'writing' }) }), 46)
  expect(said(writing.tree)).toContain('✦ IMPROVE')
  expect(said(writing.tree)).toContain('Reading the last 150 messages…')
  expect(said(writing.tree)).toContain('Writing the new prompt…')
  // the error state: the head the error card carries
  const err = tab(viewOf({ flow: flowOf({ step: 'error', failedStep: 'rewrite', error: '' }) }), 46)
  expect(said(err.tree)).toContain('✦ IMPROVE')
})
