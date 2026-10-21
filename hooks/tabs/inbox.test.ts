// Inbox tab tests (plan inbox-and-parsing, phase UI): InboxTab is called directly
// with a minimal element table, the way todo.test.ts does. Buttons are recorded by
// key so a test can press them and see exactly which callbacks fired.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { InboxTab, inboxAge, inboxShown } from './inbox'
import type { InboxActKind, InboxCardState, InboxFile, InboxFilter, InboxUi, InboxView } from './inbox'
import { INBOX_FILES, INBOX_FIXTURE, withState } from './inbox.fixture'
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
// JSX children arrive nested (`{list}` beside other children): flatten them like the runtime does
const flatten = (c: RenderChildren): RenderNode[] =>
  Array.isArray(c) ? (c as readonly RenderChildren[]).flatMap(flatten) : isNode(c) ? [c] : []
const kids = (c: RenderChildren | undefined): RenderNode[] | undefined => (c == null ? undefined : flatten(c))

const presses = new Map<string, () => void>()
const boxTag = (props: BoxProps & ElementChildren): RenderElement => ({ type: 'Box', props: scalarProps(props), children: kids(props.children) })
const textTag = (props: TextProps & ElementChildren): RenderElement => ({ type: 'Text', props: scalarProps(props), children: kids(props.children) })
const stub = (): RenderElement => ({ type: 'Text', props: {}, children: [] })
// a Button draws as a Text: `[ label ]` (a plain one: the label alone, or the Text children it was given), and records its press by key
const buttonTag = (props: { key?: string; label?: string; plain?: true; onPress?: () => void; dimColor?: boolean; children?: RenderChildren }): RenderElement => {
  if (props.key != null && props.onPress != null) presses.set(props.key, props.onPress)
  if (props.children != null) return { type: 'Text', props: {}, children: kids(props.children) }
  const label = props.plain ? String(props.label) : `[ ${String(props.label)} ]`
  return { type: 'Text', props: props.dimColor ? { dimColor: true } : {}, children: [label] }
}
const els = { Box: boxTag, Text: textTag, Button: buttonTag, Select: stub, Link: stub, Code: stub, Markdown: stub, Client: stub, Raster: stub, Image: stub, Input: stub } as unknown as Elements['terminal']

type Call = [InboxActKind, string]
function draw(view: InboxView, cols = 46) {
  presses.clear()
  const calls: Call[] = []
  const sets: [string, string][] = []
  const ui: InboxUi = {
    set: (k, v) => { sets.push([k, v]) },
    inboxAct: (kind, id) => { calls.push([kind, id]) },
  }
  return { tree: InboxTab(els, view, cols, ui), calls, sets }
}

type N = { type?: string; props?: Record<string, unknown>; children?: unknown[] }
const nodesOf = (root: unknown, type: string): N[] => {
  const out: N[] = []
  const walk = (n: unknown): void => {
    if (typeof n !== 'object' || n === null) return
    const c = n as N
    if (c.type === type) out.push(c)
    c.children?.forEach(walk)
  }
  walk(root)
  return out
}
const own = (n: N): string => (n.children ?? []).map(c => (typeof c === 'string' ? c : '')).join('')
const texts = (root: unknown): string[] => nodesOf(root, 'Text').map(own).filter(s => s !== '')
const flat = (root: unknown): string => texts(root).join('\n')
/** the text with wrapped lines joined back by single spaces. */
const joined = (root: unknown): string => texts(root).join(' ').replace(/\s+/g, ' ')
const cards = (root: unknown): N[] => nodesOf(root, 'Box').filter(b => b.props?.borderStyle === 'round').slice(1) // [0] is the header card
const cardText = (card: N): string => texts(card).join(' ').replace(/\s+/g, ' ')
const textNode = (root: unknown, s: string): N | undefined => nodesOf(root, 'Text').find(n => own(n) === s)

const one = (f: InboxFile, state: InboxCardState, filter: InboxFilter = 'inbox'): InboxView =>
  ({ files: [withState(f, state)], counts: INBOX_FIXTURE.counts, filter })
const TEMPLATE = INBOX_FILES[0]!
const PARTIAL = INBOX_FILES[1]!
const NOT_A_PLAN = INBOX_FILES[2]!
const ARCHIVED = INBOX_FILES[3]!

test('the header card: title, counts, text, chips with counts, legend', () => {
  const { tree } = draw(INBOX_FIXTURE)
  const t = texts(tree)
  expect(t).toContain('✉ Inbox')
  expect(t).toContain('3 waiting · 1 archived')
  const j = joined(tree)
  expect(j).toContain('Plan documents written from the template, waiting to become projects. Folder: inbox/ in the MatrixKoder home.')
  for (const chip of [' Inbox 3 ', ' ✓ Template 1 ', ' ◐ Needs fixing 2 ', ' Archived 1 ']) expect(t, chip).toContain(chip)
  // the legend packs by part: whole at a wide pane, split between parts at a narrow one
  expect(joined(draw(INBOX_FIXTURE, 60).tree)).toContain('✓ follows the template · ◐ partly · ✕ not a plan')
  expect(t).toContain('✓ follows the template · ◐ partly')
  expect(t).toContain('✕ not a plan')
})

test('the picked chip is drawn on, the others off', () => {
  const colours: Record<InboxFilter, { fg: string; tint: string }> = {
    inbox: ROLE.accent, template: ROLE.ok, fix: ROLE.warn, archived: ROLE.idle,
  }
  const labels: Record<InboxFilter, string> = { inbox: ' Inbox 3 ', template: ' ✓ Template 1 ', fix: ' ◐ Needs fixing 2 ', archived: ' Archived 1 ' }
  for (const picked of Object.keys(labels) as InboxFilter[]) {
    const { tree } = draw({ ...INBOX_FIXTURE, filter: picked })
    for (const f of Object.keys(labels) as InboxFilter[]) {
      const chip = textNode(tree, labels[f])
      expect(chip, `${picked}/${f}`).toBeDefined()
      if (f === picked) {
        expect(chip?.props?.backgroundColor).toBe(colours[f].fg)
        expect(chip?.props?.color).toBe(PALETTE.bg)
        expect(chip?.props?.bold).toBe(true)
      } else {
        expect(chip?.props?.backgroundColor).toBe(colours[f].tint)
        expect(chip?.props?.color).toBe(colours[f].fg)
        expect(chip?.props?.bold).toBeFalsy()
      }
    }
  }
})

test('the filters pick the files: Inbox = not archived, Template, Needs fixing, Archived', () => {
  const ids = (filter: InboxFilter): string[] => inboxShown(INBOX_FILES, filter).map(f => f.id)
  expect(ids('inbox')).toEqual(['plan-inbox-and-parsing', 'wizards-redesign', 'notes-2026-10-20'])
  expect(ids('template')).toEqual(['plan-inbox-and-parsing'])
  expect(ids('fix')).toEqual(['wizards-redesign', 'notes-2026-10-20'])
  expect(ids('archived')).toEqual(['playpause'])
  for (const [filter, n] of [['inbox', 3], ['template', 1], ['fix', 2], ['archived', 1]] as const) {
    expect(cards(draw({ ...INBOX_FIXTURE, filter }).tree), filter).toHaveLength(n)
    expect(INBOX_FIXTURE.counts[filter], filter).toBe(n)
  }
})

test('each card draws its name, badge, title, fit note and age', () => {
  const { tree } = draw(INBOX_FIXTURE)
  const cs = cards(tree)
  expect(cs).toHaveLength(3)
  const [a, b, c] = cs.map(cardText) as [string, string, string]
  expect(a).toContain('plan-inbox-and-parsing.md')
  expect(a).toContain('✓ TEMPLATE')
  expect(a).toContain('Plan Inbox and Parsing')
  expect(a).toContain('7 of 7 sections found')
  expect(a).toContain('7 of 7 sections found 2h')
  expect(b).toContain('wizards-redesign.md')
  expect(b).toContain('◐ PARTIAL')
  expect(b).toContain('missing: Tasks, Acceptance')
  expect(b).toContain('missing: Tasks, Acceptance 1d')
  expect(c).toContain('✕ NOT A PLAN')
  expect(c).toContain('no phase or task headings')
  expect(c).toContain('no phase or task headings 3w')
  // the name is bold green, the badge carries the fit tone, the borders follow the fit
  const name = textNode(tree, 'plan-inbox-and-parsing.md')
  expect(name?.props?.bold).toBe(true)
  expect(name?.props?.color).toBe(ROLE.ok.fg)
  for (const [badge, role] of [[' ✓ TEMPLATE ', ROLE.ok], [' ◐ PARTIAL ', ROLE.warn], [' ✕ NOT A PLAN ', ROLE.err]] as const) {
    expect(textNode(tree, badge)?.props?.backgroundColor, badge).toBe(role.tint)
    expect(textNode(tree, badge)?.props?.color, badge).toBe(role.fg)
  }
  expect(cs.map(x => x.props?.borderColor)).toEqual([PALETTE.line, ROLE.warn.fg, ROLE.err.fg])
})

test('an archived card is quiet, and a partial file with no names listed says how many sections it found', () => {
  const { tree } = draw({ ...INBOX_FIXTURE, filter: 'archived' })
  const [card] = cards(tree) as [N]
  expect(card.props?.borderColor).toBe(PALETTE.line)
  expect(textNode(tree, 'playpause.md')?.props?.color).toBe(PALETTE.dim)
  expect(textNode(tree, ' ✓ TEMPLATE ')?.props?.backgroundColor).toBe(ROLE.idle.tint)
  const five = { ...PARTIAL, missing: [] }
  expect(joined(draw(one(five, { kind: 'idle' })).tree)).toContain('5 of 7 sections found')
})

test('the ages: now, minutes, hours, days, weeks', () => {
  expect([10, 300, 7200, 86400, 1036800, 1814400].map(inboxAge)).toEqual(['now', '5m', '2h', '1d', '1w', '3w'])
})

test('each filter has its own empty text', () => {
  const want: Record<InboxFilter, [string, string]> = {
    inbox: ['Inbox is empty', 'Drop a plan written from the template into inbox/ and it shows up here.'],
    template: ['No clean files', 'Nothing here follows the template yet.'],
    fix: ['Nothing to fix', 'Every file in the Inbox follows the template.'],
    archived: ['Nothing archived', 'Archived plans stay here until you restore or delete them.'],
  }
  for (const f of Object.keys(want) as InboxFilter[]) {
    const { tree } = draw({ files: [], counts: { inbox: 0, template: 0, fix: 0, archived: 0 }, filter: f })
    const [title, text] = want[f]
    expect(texts(tree), f).toContain(title)
    expect(joined(tree), f).toContain(text)
    expect(cards(tree), f).toHaveLength(1) // only the empty-state card
  }
})

test('every button key has the right id pattern, and a press calls inboxAct with its kind and file id', () => {
  const { tree, calls, sets } = draw(INBOX_FIXTURE)
  expect([...presses.keys()].filter(k => k.startsWith('inbox-') && !k.startsWith('inbox-chip-')).sort()).toEqual([
    'inbox-archive-notes-2026-10-20', 'inbox-archive-plan-inbox-and-parsing', 'inbox-archive-wizards-redesign',
    'inbox-delete-notes-2026-10-20', 'inbox-delete-plan-inbox-and-parsing', 'inbox-delete-wizards-redesign',
    'inbox-import-notes-2026-10-20', 'inbox-import-plan-inbox-and-parsing', 'inbox-import-wizards-redesign',
    'inbox-open-notes-2026-10-20', 'inbox-open-plan-inbox-and-parsing', 'inbox-open-wizards-redesign',
  ])
  const id = 'wizards-redesign'
  for (const kind of ['open', 'import', 'archive', 'delete'] as const) presses.get(`inbox-${kind}-${id}`)?.()
  expect(calls).toEqual([['open', id], ['import', id], ['archive', id], ['delete', id]])
  expect(flat(tree)).not.toMatch(/improve/i)
  for (const k of presses.keys()) expect(k).not.toMatch(/improve/i)
  // the chips call the filter set
  expect(sets).toEqual([])
  for (const f of ['inbox', 'template', 'fix', 'archived']) presses.get(`inbox-chip-${f}`)?.()
  expect(sets).toEqual([['inbox:filter', 'inbox'], ['inbox:filter', 'template'], ['inbox:filter', 'fix'], ['inbox:filter', 'archived']])
})

test('an archived card has Open, Restore and Delete, and no Import or Archive', () => {
  const { calls } = draw({ ...INBOX_FIXTURE, filter: 'archived' })
  expect([...presses.keys()].filter(k => k.startsWith('inbox-') && !k.startsWith('inbox-chip-')).sort()).toEqual([
    'inbox-delete-playpause', 'inbox-open-playpause', 'inbox-restore-playpause',
  ])
  presses.get('inbox-restore-playpause')?.()
  presses.get('inbox-delete-playpause')?.()
  expect(calls).toEqual([['restore', 'playpause'], ['delete', 'playpause']])
})

const actionKeys = (): string[] => [...presses.keys()].filter(k => /^inbox-(open|import|archive|restore|delete)-/.test(k))

test('importing draws the step, the 20-cell bar, the note and a live border, with no action row', () => {
  for (const step of [1, 2, 3] as const) {
    const { tree } = draw(one(TEMPLATE, { kind: 'importing', step }))
    const name = ['Parse the template', 'Write the project', 'Write the tasks'][step - 1]
    expect(texts(tree), String(step)).toContain(`▶ ${name}`)
    expect(texts(tree), String(step)).toContain(`${String(step)} of 3`)
    const bar = nodesOf(tree, 'Text').find(n => n.props?.backgroundColor === PALETTE.track)
    expect(bar && [...own(bar)].length, String(step)).toBe(20)
    expect(own(bar!)).toContain('█')
    expect(joined(tree)).toContain('runs in the background · you can leave this tab')
    expect(cards(tree)[0]?.props?.borderColor).toBe(ROLE.live.fg)
    expect(actionKeys(), String(step)).toEqual([])
  }
})

test('done draws the project, the counts, the note and two buttons, and keeps the action row without Import', () => {
  const { tree, calls } = draw(one(TEMPLATE, { kind: 'done', project: 'inbox-parsing', phases: 7, tasks: 14 }), 60)
  const j = joined(tree)
  expect(j).toContain('✓ Imported as inbox-parsing')
  expect(j).toContain('7 phases · 14 tasks')
  expect(j).toContain('Written to the database. The file stays in the Inbox until you archive it.')
  expect(texts(tree)).toContain('[ ▤ Open project ]')
  expect(texts(tree)).toContain('[ ↻ Import again ]')
  expect(actionKeys().sort()).toEqual(['inbox-archive-plan-inbox-and-parsing', 'inbox-delete-plan-inbox-and-parsing', 'inbox-open-plan-inbox-and-parsing'])
  presses.get('inbox-openproj-plan-inbox-and-parsing')?.()
  presses.get('inbox-again-plan-inbox-and-parsing')?.()
  expect(calls).toEqual([['open-project', TEMPLATE.id], ['import', TEMPLATE.id]])
})

test('failed draws the step, the reason and Retry / Dismiss, and keeps the action row', () => {
  const { tree, calls } = draw(one(PARTIAL, { kind: 'failed', step: 2, reason: 'Phase P3 has no tasks.' }))
  expect(texts(tree)).toContain('✕ Import failed at step 2')
  expect(joined(tree)).toContain('Phase P3 has no tasks.')
  expect(texts(tree)).toContain('[ ↻ Retry ]')
  expect(texts(tree)).toContain('[ Dismiss ]')
  expect(actionKeys().sort()).toEqual(['inbox-archive-wizards-redesign', 'inbox-delete-wizards-redesign', 'inbox-import-wizards-redesign', 'inbox-open-wizards-redesign'])
  presses.get('inbox-retry-wizards-redesign')?.()
  presses.get('inbox-dismiss-wizards-redesign')?.()
  expect(calls).toEqual([['retry', PARTIAL.id], ['dismiss', PARTIAL.id]])
})

test('confirm asks about the delete, says a copy is kept, and hides the action row', () => {
  const { tree, calls } = draw(one(TEMPLATE, { kind: 'confirm' }))
  const j = joined(tree)
  expect(j).toContain('Delete plan-inbox-and-parsing.md from the Inbox? A copy is kept in inbox/.deleted/.')
  expect(j).not.toContain('removed from disk')
  const yes = textNode(tree, '✕')
  expect(yes?.props?.color).toBe(ROLE.err.fg)
  expect(texts(tree)).toContain('[ Yes, delete ]')
  expect(texts(tree)).toContain('[ Keep it ]')
  expect(actionKeys()).toEqual([])
  presses.get('inbox-yes-plan-inbox-and-parsing')?.()
  presses.get('inbox-keep-plan-inbox-and-parsing')?.()
  expect(calls).toEqual([['confirm-delete', TEMPLATE.id], ['cancel-delete', TEMPLATE.id]])
})

// ---- widths: no drawn line is wider than the pane ----

const textW = (n: unknown): number =>
  typeof n === 'string' ? [...n].length : ((n as N).children ?? []).reduce((s: number, k) => s + textW(k), 0)
/** the first line that is wider than `avail` cells (a Card takes 4 for its border and padding), or null. */
function tooWide(n: unknown, avail: number): string | null {
  if (typeof n !== 'object' || n === null) return null
  const c = n as N
  if (c.type === 'Text') return textW(c) > avail ? `${String(textW(c))}>${String(avail)} ${own(c)}` : null
  const p = c.props ?? {}
  const inner = avail - (p.borderStyle != null ? 2 : 0) - (typeof p.paddingX === 'number' ? p.paddingX * 2 : 0)
  const ks = c.children ?? []
  if (p.flexDirection === 'row' && p.flexWrap !== 'wrap') {
    const gap = typeof p.gap === 'number' ? p.gap : 0
    const w = ks.reduce((s: number, k) => s + (typeof k === 'object' && k !== null && (k as N).type === 'Text' ? textW(k) : widthOfBox(k)), 0) + gap * Math.max(0, ks.length - 1)
    if (w > inner) return `row ${String(w)}>${String(inner)} ${texts(c).join('|')}`
  }
  for (const k of ks) {
    const bad = tooWide(k, inner)
    if (bad != null) return bad
  }
  return null
}
function widthOfBox(n: unknown): number {
  if (typeof n !== 'object' || n === null) return 0
  const c = n as N
  if (c.type === 'Text') return textW(c)
  const p = c.props ?? {}
  const ks = c.children ?? []
  const chrome = (p.borderStyle != null ? 2 : 0) + (typeof p.paddingX === 'number' ? p.paddingX * 2 : 0)
  if (p.flexDirection === 'row') {
    const gap = typeof p.gap === 'number' ? p.gap : 0
    // a wrapping row is as wide as its widest child (the rest wraps below it)
    return chrome + (p.flexWrap === 'wrap' ? Math.max(0, ...ks.map(widthOfBox)) : ks.reduce((s: number, k) => s + widthOfBox(k), 0) + gap * Math.max(0, ks.length - 1))
  }
  return chrome + Math.max(0, ...ks.map(widthOfBox))
}

const STATES: [string, InboxCardState][] = [
  ['idle', { kind: 'idle' }],
  ['importing 1', { kind: 'importing', step: 1 }],
  ['importing 2', { kind: 'importing', step: 2 }],
  ['importing 3', { kind: 'importing', step: 3 }],
  ['done', { kind: 'done', project: 'a-very-long-project-name-for-the-width-check', phases: 12, tasks: 140 }],
  ['failed', { kind: 'failed', step: 3, reason: 'Task T1.12 lists a dependency that no task defines, so the tasks cannot be written.' }],
  ['confirm', { kind: 'confirm' }],
]
const LONG: InboxFile = {
  ...PARTIAL,
  path: 'a-rather-long-plan-directory-name/with-a-long-file-name-plan.md',
  title: 'A plan title that is a good deal longer than any pane is wide, to be cut',
  missing: ['Summary', 'Goal', 'Design / context', 'Phases', 'Risks', 'Open questions', 'Log'],
  age_s: 99 * 604800,
}

test('no drawn line is wider than the pane at 36, 40 and 46 columns, in every state', () => {
  for (const cols of [36, 40, 46]) {
    for (const [name, state] of STATES) {
      for (const f of [TEMPLATE, PARTIAL, NOT_A_PLAN, ARCHIVED, LONG]) {
        const { tree } = draw(one(f, state, f.archived ? 'archived' : 'inbox'), cols)
        expect(tooWide(tree, cols), `${name} ${f.id} @${String(cols)}`).toBeNull()
      }
    }
    expect(tooWide(draw(INBOX_FIXTURE, cols).tree, cols), `fixture @${String(cols)}`).toBeNull()
    for (const filter of ['inbox', 'template', 'fix', 'archived'] as const) {
      expect(tooWide(draw({ files: [], counts: { inbox: 0, template: 0, fix: 0, archived: 0 }, filter }, cols).tree, cols), `empty ${filter} @${String(cols)}`).toBeNull()
    }
  }
  // the checker itself catches a wide line
  expect(tooWide(draw(INBOX_FIXTURE, 46).tree, 20)).not.toBeNull()
})
