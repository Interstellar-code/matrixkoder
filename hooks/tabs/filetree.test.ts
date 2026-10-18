// Tests for FilesTab (plan filetree-tab-ralplan, phase F3, task F3.1).
// Covers all 16 acceptance criteria F3.1.a1 through F3.1.a16.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { FilesTab, FT } from './filetree'
import type { FilesTabUi, FilesView } from './filetree'
import {
  filetreeFixtureView,
  noGitFixtureView,
  longNamesFixtureView,
  FIXTURE_ROOTS,
} from './filetree.fixture'
import type { Row } from './filetree-data'

type Node = { type?: string; props?: Record<string, unknown>; children?: unknown[] }

const isNode = (k: RenderChildren): k is RenderNode =>
  typeof k === 'string' || (typeof k === 'object' && k !== null && !Array.isArray(k))

const kids = (c: RenderChildren | undefined): RenderNode[] | undefined => {
  if (c == null) return undefined
  const arr: readonly RenderChildren[] = Array.isArray(c) ? c : [c]
  return arr.filter(isNode)
}

const scalarProps = (props: object): Record<string, string | boolean | number> => {
  const out: Record<string, string | boolean | number> = {}
  for (const [k, v] of Object.entries(props)) {
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') out[k] = v
  }
  return out
}

const stubEls = {
  Box: (props: any): RenderElement => ({
    type: 'Box',
    props: scalarProps(props),
    children: kids(props.children),
  }),
  Text: (props: any): RenderElement => ({
    type: 'Text',
    props: scalarProps(props),
    children: kids(props.children),
  }),
  Button: (props: any): RenderElement => {
    // the real engine lets a Button hold only strings and Text
    for (const c of ([] as unknown[]).concat(props.children ?? []).flat()) {
      if (typeof c !== 'string' && (c as { type?: string } | null)?.type !== 'Text') {
        throw new Error(`JSX element <Button key="${props.key}"> holds strings and Text, not <${(c as { type?: string } | null)?.type ?? typeof c}>`)
      }
    }
    return {
      type: 'Button',
      props,
      children: Array.isArray(props.children) ? (props.children as unknown[]).flatMap(c => Array.isArray(c) ? c : [c]) : [],
    } as unknown as RenderElement
  },
  Input: (props: any): RenderElement => ({
    type: 'Input',
    props,
  }) as unknown as RenderElement,
} as unknown as Elements[keyof Elements]

function nodesOf(n: unknown, type?: string): Node[] {
  const out: Node[] = []
  const walk = (node: unknown): void => {
    if (typeof node !== 'object' || node === null) return
    const cur = node as Node
    if (!type || cur.type === type) out.push(cur)
    for (const c of cur.children ?? []) walk(c)
  }
  walk(n)
  return out
}

/** the row's line Box: it holds the row Button and the icon column beside it */
function lineOf(tree: unknown, rowKey: string): Node {
  return nodesOf(tree, 'Box').find(b => b.props?.key === `files-line:${rowKey.slice('files-row:'.length)}`)!
}

function textOf(n: unknown): string {
  if (typeof n === 'string') return n
  if (typeof n !== 'object' || n === null) return ''
  const cur = n as Node
  if (cur.type === 'Button' && typeof cur.props?.label === 'string') {
    return cur.props.label
  }
  return (cur.children ?? []).map(textOf).join('')
}

function allText(n: unknown): string {
  const texts = nodesOf(n, 'Text').map(t => (t.children ?? []).map(c => (typeof c === 'string' ? c : textOf(c))).join(''))
  const buttons = nodesOf(n, 'Button').map(b => (b.props?.label ? String(b.props.label) : textOf(b)))
  return [...texts, ...buttons].join('\n')
}

function pressButton(tree: unknown, key: string): void {
  const b = nodesOf(tree, 'Button').find(x => String(x.props?.key) === key)
  if (!b) throw new Error(`button ${key} not found`)
  ;(b.props?.onPress as (() => void) | undefined)?.()
}

function createMockUi(initialState: Record<string, string> = {}): {
  ui: FilesTabUi
  state: Record<string, string>
  revealed: string[]
  focused: string[]
  gets: string[]
} {
  const state: Record<string, string> = { ...initialState }
  const revealed: string[] = []
  const focused: string[] = []
  const gets: string[] = []
  const ui: FilesTabUi = {
    get: (key: string) => {
      gets.push(key)
      return state[key]
    },
    set: (key: string, val: string) => {
      state[key] = val
    },
    reveal: (key: string) => {
      revealed.push(key)
    },
    focus: (key: string) => {
      focused.push(key)
    },
  }
  return { ui, state, revealed, focused, gets }
}

// ────────────────────── Acceptance Tests F3.1.a1 – F3.1.a16 ──────────────────────

test('Files F3.1: root chips show repo and each lane with active chip colours on chosen one, pressing calls ui.set and all keys are unique', () => {
  const view = filetreeFixtureView()
  const { ui, state } = createMockUi()
  const tree = FilesTab(stubEls, view, 46, ui)

  // Root chips have keys files-root:<id>
  const chipButtons = nodesOf(tree, 'Button').filter(b => String(b.props?.key).startsWith('files-root:'))
  expect(chipButtons.length).toBe(3)
  expect(chipButtons.map(b => b.props?.key)).toEqual([
    'files-root:main',
    'files-root:slot-1',
    'files-root:slot-2',
  ])

  // Active chip (main) has activeChip colour and white bold text
  const mainChip = chipButtons[0]!
  const mainText = nodesOf(mainChip, 'Text')[0]!
  expect(mainText.props?.backgroundColor).toBe(FT.activeChip)
  expect(mainText.props?.color).toBe(FT.white)
  expect(mainText.props?.bold).toBe(true)
  expect(textOf(mainChip)).toContain('repo')

  // Inactive chip (slot-1) has chipFill and fileText
  const slot1Chip = chipButtons[1]!
  const slot1Text = nodesOf(slot1Chip, 'Text')[0]!
  expect(slot1Text.props?.backgroundColor).toBe(FT.chipFill)
  expect(slot1Text.props?.color).toBe(FT.fileText)
  expect(textOf(slot1Chip)).toContain('P4')

  // Pressing calls ui.set('files:root', id)
  pressButton(tree, 'files-root:slot-1')
  expect(state['files:root']).toBe('slot-1')

  // Every element key is unique and none starts with dash-tab-
  const allElements = nodesOf(tree)
  const keys: string[] = []
  for (const el of allElements) {
    if (el.props?.key) {
      keys.push(String(el.props.key))
    }
  }
  const uniqueKeys = new Set(keys)
  expect(uniqueKeys.size).toBe(keys.length)
  for (const k of keys) {
    expect(k.startsWith('files-')).toBe(true)
    expect(k.startsWith('dash-tab-')).toBe(false)
  }
})

test('Files F3.1: card shows root label in bold neon, write count after amber dot, read count after teal ring, and dim clipped path', () => {
  const view = filetreeFixtureView({ nWrite: 7, nRead: 11 })
  const { ui } = createMockUi()
  const tree = FilesTab(stubEls, view, 46, ui)

  const textNodes = nodesOf(tree, 'Text')

  // Root label in bold neon
  const neonLabel = textNodes.find(t => t.props?.color === FT.neon && t.props?.bold === true)
  expect(neonLabel).toBeDefined()
  expect(textOf(neonLabel)).toBe('repo')

  // Amber dot and write count
  const amberDot = textNodes.find(t => t.props?.color === FT.write && textOf(t).includes('●'))
  expect(amberDot).toBeDefined()
  const writeCount = textNodes.find(t => textOf(t) === '7')
  expect(writeCount).toBeDefined()

  // Teal ring and read count
  const tealRing = textNodes.find(t => t.props?.color === FT.read && textOf(t).includes('◉'))
  expect(tealRing).toBeDefined()
  const readCount = textNodes.find(t => textOf(t) === '11')
  expect(readCount).toBeDefined()

  // Root path is dim
  const dimPath = textNodes.find(t => t.props?.color === FT.dim && textOf(t).includes('/repo'))
  expect(dimPath).toBeDefined()
  expect(dimPath?.props?.dimColor).toBe(true)
})

test('Files F3.1: folders are bold neon with caret, files in file text, hidden in hidden text, links dim with arrow, indent 2 cols per depth', () => {
  const view = filetreeFixtureView()
  const { ui } = createMockUi()
  const tree = FilesTab(stubEls, view, 46, ui)

  const textNodes = nodesOf(tree, 'Text')

  // Folders are bold neon with caret (open or folded)
  const docsFolderText = textNodes.find(t => textOf(t).trim() === 'docs')
  expect(docsFolderText?.props?.color).toBe(FT.neon)
  expect(docsFolderText?.props?.bold).toBe(true)

  // Files in file text
  const readmeText = textNodes.find(t => textOf(t).trim() === 'package.json')
  expect(readmeText?.props?.color).toBe(FT.fileText)

  // Hidden names in hidden text
  const hiddenText = textNodes.find(t => textOf(t).trim() === '.github')
  expect(hiddenText?.props?.color).toBe(FT.hidden)

  // Links dim with trailing arrow
  const linkText = textNodes.find(t => textOf(t).trim() === 'link-to-readme →')
  expect(linkText?.props?.color).toBe(FT.dim)

  // Indent: depth 0 has empty indent, depth 1 has 2 spaces, depth 2 has 4 spaces
  const depth2Row = view.rows.find(r => r.depth === 2)!
  const rowBtn = lineOf(tree, depth2Row.key)
  const rowTexts = nodesOf(rowBtn, 'Text')
  expect(textOf(rowTexts[1])).toBe('    ')
})

test('Files F3.1: written row has write tint and pulsing amber dot, read row has read tint and teal ring, folded folder shows icon and count', () => {
  // 1. Written row has write tint and amber dot, bright when pulse: true, dim when pulse: false
  const viewPulse = filetreeFixtureView({ pulse: true, sel: '' })
  const { ui } = createMockUi()
  const treePulse = FilesTab(stubEls, viewPulse, 46, ui)

  const mkdbRow = viewPulse.rows.find(r => r.path === '/repo/scripts/mkdb.py')!
  expect(mkdbRow.icon).toBe('write')
  const mkdbBtn = lineOf(treePulse, mkdbRow.key)
  const mkdbBox = nodesOf(mkdbBtn, 'Box')[0]!
  expect(mkdbBox.props?.backgroundColor).toBe(FT.writeRowTint)

  const mkdbIconText = nodesOf(mkdbBtn, 'Text').find(t => textOf(t).includes('●'))!
  expect(mkdbIconText.props?.color).toBe(FT.write)
  expect(mkdbIconText.props?.dimColor).toBe(false) // bright

  // With pulse false: amber dot is dim
  const viewDim = filetreeFixtureView({ pulse: false, sel: '' })
  const treeDim = FilesTab(stubEls, viewDim, 46, createMockUi().ui)
  const mkdbBtnDim = lineOf(treeDim, mkdbRow.key)
  const mkdbIconDim = nodesOf(mkdbBtnDim, 'Text').find(t => textOf(t).includes('●'))!
  expect(mkdbIconDim.props?.dimColor).toBe(true)

  // 2. Read row has read tint and teal ring
  const guideRow = viewPulse.rows.find(r => r.path === '/repo/docs/guide.md')!
  expect(guideRow.icon).toBe('read')
  const guideBtn = lineOf(treePulse, guideRow.key)
  const guideBox = nodesOf(guideBtn, 'Box')[0]!
  expect(guideBox.props?.backgroundColor).toBe(FT.readRowTint)
  const guideIconText = nodesOf(guideBtn, 'Text').find(t => textOf(t).includes('◉'))!
  expect(guideIconText.props?.color).toBe(FT.read)
  expect(textOf(guideIconText)).toContain('◉')

  // 3. Folded folder shows strongest icon plus count when > 1
  const customRows: Row[] = [
    {
      key: 'files-row:/repo/folded-folder',
      path: '/repo/folded-folder',
      name: 'folded-folder',
      depth: 0,
      dir: true,
      open: false,
      hidden: false,
      link: false,
      icon: 'write',
      count: 3,
    },
  ]
  const viewFolded = filetreeFixtureView({ rows: customRows, sel: '' })
  const treeFolded = FilesTab(stubEls, viewFolded, 46, createMockUi().ui)
  const foldedBtn = lineOf(treeFolded, 'files-row:/repo/folded-folder')
  const foldedIcon = nodesOf(foldedBtn, 'Text').find(t => textOf(t).includes('●'))!
  expect(textOf(foldedIcon)).toContain('● 3')
})

test('Files F3.1: typing in search input calls ui.set, and empty match shows dim no file matches line', () => {
  const view = filetreeFixtureView({ q: 'initial' })
  const { ui, state } = createMockUi()
  const tree = FilesTab(stubEls, view, 46, ui)

  // Input key files-q
  const inputEl = nodesOf(tree, 'Input').find(i => i.props?.key === 'files-q')!
  expect(inputEl).toBeDefined()
  expect(inputEl.props?.placeholder).toBe('filter files')
  expect(inputEl.props?.value).toBe('initial')

  // Typing calls ui.set('files:q', text)
  ;(inputEl.props?.onInput as (val: string) => void)('searchterm')
  expect(state['files:q']).toBe('searchterm')

  // Empty match: view.rows = [] shows dim "no file matches"
  const emptyView = filetreeFixtureView({ rows: [], q: 'nonexistent' })
  const treeEmpty = FilesTab(stubEls, emptyView, 46, ui)
  const noMatchText = nodesOf(treeEmpty, 'Text').find(t => textOf(t).includes('no file matches'))
  expect(noMatchText).toBeDefined()
  expect(noMatchText?.props?.dimColor).toBe(true)
  expect(noMatchText?.props?.color).toBe(FT.dim)
})

test('Files F3.1: Expand all sets files:open to JSON of view.dirs and Collapse all sets it to empty array', () => {
  const view = filetreeFixtureView()
  const { ui, state } = createMockUi()
  const tree = FilesTab(stubEls, view, 46, ui)

  pressButton(tree, 'files-expand')
  expect(state['files:open']).toBe(JSON.stringify(view.dirs))

  pressButton(tree, 'files-collapse')
  expect(state['files:open']).toBe(JSON.stringify([]))
})

test('Files F3.1: empty folder shows dim empty row, unlisted folder shows dim not listed row, note shows as line, capped shows limit line', () => {
  const customRows: Row[] = [
    {
      key: 'files-empty:/repo/empty-dir',
      path: '/repo/empty-dir',
      name: '(empty)',
      depth: 1,
      dir: false,
      open: false,
      hidden: false,
      link: false,
      icon: null,
      count: 0,
      note: 'empty',
    },
    {
      key: 'files-unlisted:/repo/unlisted-dir',
      path: '/repo/unlisted-dir',
      name: '(not listed)',
      depth: 1,
      dir: false,
      open: false,
      hidden: false,
      link: false,
      icon: null,
      count: 0,
      note: 'unlisted',
    },
  ]
  const view = filetreeFixtureView({
    rows: customRows,
    note: 'missing root, fallback to repo',
    capped: true,
  })
  const { ui } = createMockUi()
  const tree = FilesTab(stubEls, view, 46, ui)

  const textNodes = nodesOf(tree, 'Text')

  // empty folder shows dim (empty) row
  const emptyText = textNodes.find(t => textOf(t).includes('(empty)'))!
  expect(emptyText).toBeDefined()
  expect(emptyText.props?.color).toBe(FT.dim)
  expect(emptyText.props?.dimColor).toBe(true)

  // unlisted folder shows dim (not listed) row
  const unlistedText = textNodes.find(t => textOf(t).includes('(not listed)'))!
  expect(unlistedText).toBeDefined()
  expect(unlistedText.props?.color).toBe(FT.dim)
  expect(unlistedText.props?.dimColor).toBe(true)

  // view.note shows as one line
  const noteText = textNodes.find(t => textOf(t).includes('missing root, fallback to repo'))!
  expect(noteText).toBeDefined()
  expect(noteText.props?.color).toBe(FT.dim)

  // view.capped shows dim line that tree stopped at its limit
  const cappedText = textNodes.find(t => textOf(t).includes('tree stopped at its limit'))!
  expect(cappedText).toBeDefined()
  expect(cappedText.props?.color).toBe(FT.dim)
  expect(cappedText.props?.dimColor).toBe(true)
})

function boxOverhead(b: Node | undefined): number {
  if (!b || !b.props) return 0
  const border = b.props.borderStyle ? 2 : 0
  const padX = Number(b.props.paddingX ?? 0) * 2
  const padL = Number(b.props.paddingLeft ?? 0)
  const padR = Number(b.props.paddingRight ?? 0)
  return border + padX + padL + padR
}

/** columns a Button draws: a plain Button with a hotkey prints `<hotkey>: <label>` */
function buttonWidth(b: Node): number {
  const label = textOf(b).length
  return typeof b.props?.hotkey === 'string' ? b.props.hotkey.length + 2 + label : label
}

test('Files F3.1: at 46 and 36 columns no line is wider than column count and long names end in ellipsis', () => {
  const longView = longNamesFixtureView()
  const { ui } = createMockUi()

  for (const cols of [46, 36]) {
    const tree = FilesTab(stubEls, longView, cols, ui)

    const rootNode = tree as Node
    const rootChildren = (rootNode.children ?? []) as Node[]

    // 1. Chip row: chips plus gaps
    const chipRow = rootChildren[0] as Node
    expect(chipRow).toBeDefined()
    const chips = nodesOf(chipRow, 'Button')
    const chipRowGap = Number(chipRow.props?.gap ?? 0)
    const chipRowWidth = chips.reduce((sum, c) => sum + buttonWidth(c), 0) + Math.max(0, chips.length - 1) * chipRowGap + boxOverhead(chipRow)
    expect(chipRowWidth, `chip row at ${cols} cols: ${chipRowWidth}`).toBeLessThanOrEqual(cols)

    // 2. Card: border and padding plus line contents
    const cardBox = nodesOf(tree, 'Box').find(b => b.props?.borderColor === FT.cardBorder && b.props?.paddingX !== undefined)!
    expect(cardBox).toBeDefined()
    const cardOverhead = boxOverhead(cardBox) // borderStyle (2) + paddingX (2) = 4

    // Top line in card (root label + live activity counts + space + gaps)
    const topRow = ((cardBox.children ?? []) as Node[])[0] as Node
    const topLabelText = nodesOf((topRow.children as Node[] | undefined)?.[0], 'Text')[0]!
    const countsBox = (topRow.children as Node[] | undefined)?.[1] as Node
    const countTexts = nodesOf(countsBox, 'Text')
    const countGaps = Math.max(0, countTexts.length - 1) * Number(countsBox.props?.gap ?? 1)
    const countsWidth = countTexts.reduce((sum, t) => sum + textOf(t).length, 0) + countGaps
    const topLineWidth = textOf(topLabelText).length + 1 + countsWidth + cardOverhead
    expect(topLineWidth, `card top line at ${cols} cols: ${topLineWidth}`).toBeLessThanOrEqual(cols)

    // Path line in card
    const cardPathText = nodesOf(cardBox, 'Text').find(t => textOf(t).includes('/a/very/long'))!
    expect(cardPathText).toBeDefined()
    const cardPathLineWidth = textOf(cardPathText).length + cardOverhead
    expect(cardPathLineWidth, `card path line at ${cols} cols: ${cardPathLineWidth}`).toBeLessThanOrEqual(cols)

    // Search row in card (Input/Text + Button 's' + gap)
    const searchRow = ((cardBox.children ?? []) as Node[])[2] as Node
    const searchRowGap = Number(searchRow.props?.gap ?? 1)
    const searchTexts = nodesOf(searchRow, 'Text')
    const searchButtons = nodesOf(searchRow, 'Button')
    const searchRowContentWidth = searchTexts.reduce((sum, t) => sum + textOf(t).length, 0)
      + searchButtons.reduce((sum, b) => sum + buttonWidth(b), 0)
      + searchRowGap
    const searchRowLineWidth = searchRowContentWidth + cardOverhead
    expect(searchRowLineWidth, `search row at ${cols} cols: ${searchRowLineWidth}`).toBeLessThanOrEqual(cols)

    // Expand/Collapse row in card
    const expRow = ((cardBox.children ?? []) as Node[])[3] as Node
    const expRowGap = Number(expRow.props?.gap ?? 1)
    const expButtons = nodesOf(expRow, 'Button')
    const expRowContentWidth = expButtons.reduce((sum, b) => sum + buttonWidth(b), 0)
      + Math.max(0, expButtons.length - 1) * expRowGap
    const expRowLineWidth = expRowContentWidth + cardOverhead
    expect(expRowLineWidth, `expand/collapse row at ${cols} cols: ${expRowLineWidth}`).toBeLessThanOrEqual(cols)

    // 3. Tree Box: border plus each row line
    const treeBox = nodesOf(tree, 'Box').find(b => b.props?.borderColor === FT.cardBorder && b.props?.paddingX === undefined)!
    expect(treeBox).toBeDefined()
    const treeOverhead = boxOverhead(treeBox) // borderStyle (2) = 2

    // Path rows: the Button holds the whole line, icon column included
    const pathRowBtns = nodesOf(treeBox, 'Button')
    expect(pathRowBtns.length).toBeGreaterThan(0)
    for (const btn of pathRowBtns) {
      const rowTexts = nodesOf(btn, 'Text')
      const rowElementsWidth = rowTexts.reduce((sum, t) => sum + textOf(t).length, 0)
      const totalRowLineWidth = rowElementsWidth + treeOverhead
      expect(totalRowLineWidth, `path row at ${cols} cols: ${totalRowLineWidth}`).toBeLessThanOrEqual(cols)
    }

    // Note rows (empty, unlisted, etc.)
    const noteTexts = nodesOf(treeBox, 'Text').filter(t => {
      const k = String(t.props?.key ?? '')
      return k.startsWith('files-empty:') || k.startsWith('files-unlisted:') || k.startsWith('files-more:') || k === 'files-no-matches'
    })
    expect(noteTexts.length).toBeGreaterThan(0)
    for (const note of noteTexts) {
      const noteLineWidth = textOf(note).length + treeOverhead
      expect(noteLineWidth, `note row at ${cols} cols: ${noteLineWidth}`).toBeLessThanOrEqual(cols)
    }

    // 4. Line under the tree (counts + path)
    const underTreeBox = rootChildren.find(b => {
      return b.type === 'Box' && nodesOf(b, 'Text').some(t => textOf(t).includes('folders'))
    }) as Node
    expect(underTreeBox).toBeDefined()
    const underTexts = nodesOf(underTreeBox, 'Text')
    const underTreeLineWidth = underTexts.reduce((sum, t) => sum + textOf(t).length, 0) + 1 + boxOverhead(underTreeBox)
    expect(underTreeLineWidth, `under-tree line at ${cols} cols: ${underTreeLineWidth}`).toBeLessThanOrEqual(cols)

    // 5. Footer line (legend with gaps + space + keys with gaps)
    const footerBox = rootChildren.find(b => {
      return b.type === 'Box' && nodesOf(b, 'Button').some(btn => btn.props?.key === 'files-key-j')
    }) as Node
    expect(footerBox).toBeDefined()
    const legendBox = (footerBox.children as Node[] | undefined)?.[0] as Node
    const legendTexts = nodesOf(legendBox, 'Text')
    const legendGaps = Math.max(0, legendTexts.length - 1) * Number(legendBox.props?.gap ?? 1)
    const legendWidth = legendTexts.reduce((sum, t) => sum + textOf(t).length, 0) + legendGaps
    const keysBox = (footerBox.children as Node[] | undefined)?.[1] as Node
    const keyButtons = nodesOf(keysBox, 'Button')
    const keysGaps = Math.max(0, keyButtons.length - 1) * Number(keysBox.props?.gap ?? 1)
    const keysWidth = keyButtons.reduce((sum, b) => sum + buttonWidth(b), 0) + keysGaps
    // side by side with one space between when wide, stacked in two lines when not
    const footerLineWidth = (footerBox.props?.flexDirection === 'row' ? legendWidth + 1 + keysWidth : Math.max(legendWidth, keysWidth)) + boxOverhead(footerBox)
    expect(footerLineWidth, `footer line at ${cols} cols: ${footerLineWidth}`).toBeLessThanOrEqual(cols)

    // Long file name ends in ellipsis
    const longRowText = nodesOf(tree, 'Text').find(t => textOf(t).includes('an-extremely-long'))
    expect(longRowText).toBeDefined()
    expect(textOf(longRowText).endsWith('…')).toBe(true)

    // Long path ends in ellipsis
    expect(textOf(cardPathText).endsWith('…')).toBe(true)
  }
})

test('Files F3.1: drawn text with no dot-git fixture matches none of the whole words git, branch, commit', () => {
  const view = noGitFixtureView()
  const { ui } = createMockUi()
  const tree = FilesTab(stubEls, view, 46, ui)

  const textDump = allText(tree)
  expect(textDump).not.toMatch(/\bgit\b/i)
  expect(textDump).not.toMatch(/\bbranch\b/i)
  expect(textDump).not.toMatch(/\bcommit\b/i)
})

test('Files F3.1: when view throws while drawing FilesTab returns one line starting Files could not draw', () => {
  const throwingView = {
    ...filetreeFixtureView(),
    get rows(): Row[] {
      throw new Error('corrupted tree state')
    },
  }
  const { ui } = createMockUi()
  const tree = FilesTab(stubEls, throwingView, 46, ui)

  const textDump = allText(tree).trim()
  expect(textDump.startsWith('Files could not draw')).toBe(true)
})

test('Files F3.1: pressing j and k calls ui.set for next and previous visible path row stopping at ends, note rows are Text and never selected', () => {
  const view = filetreeFixtureView()
  const pathRows = view.rows.filter(r => !r.note)
  const { ui, state, revealed } = createMockUi({ 'files:sel': pathRows[0]!.key })
  const tree = FilesTab(stubEls, view, 46, ui)

  // Pressing j selects next visible path row
  pressButton(tree, 'files-key-j')
  expect(state['files:sel']).toBe(pathRows[1]!.key)
  expect(revealed).toContain(pathRows[1]!.key)

  // At the end, pressing j stops at the end
  const { ui: uiEnd, state: stateEnd } = createMockUi({ 'files:sel': pathRows[pathRows.length - 1]!.key })
  const treeEnd = FilesTab(stubEls, view, 46, uiEnd)
  pressButton(treeEnd, 'files-key-j')
  expect(stateEnd['files:sel']).toBe(pathRows[pathRows.length - 1]!.key)

  // Pressing k moves to previous visible path row
  const { ui: uiMid, state: stateMid } = createMockUi({ 'files:sel': pathRows[2]!.key })
  const treeMid = FilesTab(stubEls, view, 46, uiMid)
  pressButton(treeMid, 'files-key-k')
  expect(stateMid['files:sel']).toBe(pathRows[1]!.key)

  // At the start, pressing k stops at index 0
  const { ui: uiStart, state: stateStart } = createMockUi({ 'files:sel': pathRows[0]!.key })
  const treeStart = FilesTab(stubEls, view, 46, uiStart)
  pressButton(treeStart, 'files-key-k')
  expect(stateStart['files:sel']).toBe(pathRows[0]!.key)

  // Note rows (empty, unlisted, more) are Text, never Button
  const customRows: Row[] = [
    {
      key: 'files-empty:/repo/e',
      path: '/repo/e',
      name: '(empty)',
      depth: 1,
      dir: false,
      open: false,
      hidden: false,
      link: false,
      icon: null,
      count: 0,
      note: 'empty',
    },
    {
      key: 'files-unlisted:/repo/u',
      path: '/repo/u',
      name: '(not listed)',
      depth: 1,
      dir: false,
      open: false,
      hidden: false,
      link: false,
      icon: null,
      count: 0,
      note: 'unlisted',
    },
    {
      key: 'files-more:/repo/m',
      path: '/repo/m',
      name: 'more',
      depth: 1,
      dir: false,
      open: false,
      hidden: false,
      link: false,
      icon: null,
      count: 50,
      note: 'more',
    },
  ]
  const viewNotes = filetreeFixtureView({ rows: customRows })
  const treeNotes = FilesTab(stubEls, viewNotes, 46, ui)
  const buttons = nodesOf(treeNotes, 'Button')
  expect(buttons.some(b => String(b.props?.key).startsWith('files-empty:'))).toBe(false)
  expect(buttons.some(b => String(b.props?.key).startsWith('files-unlisted:'))).toBe(false)
  expect(buttons.some(b => String(b.props?.key).startsWith('files-more:'))).toBe(false)
})

test('Files F3.1: pressing l opens selected folder, h folds it, or on a file h moves selection to parent folder', () => {
  // Case 1: pressing l opens selected folded folder
  const view = filetreeFixtureView()
  const foldedFolder = view.rows.find(r => r.dir && !r.open)!
  const { ui: uiFolded, state: stateFolded } = createMockUi({ 'files:sel': foldedFolder.key })
  const treeFolded = FilesTab(stubEls, view, 46, uiFolded)
  pressButton(treeFolded, 'files-key-l')
  const openListAfterL = JSON.parse(stateFolded['files:open'] ?? '[]') as string[]
  expect(openListAfterL).toContain(foldedFolder.path)

  // Case 2: pressing h folds selected open folder
  const openFolder = view.rows.find(r => r.dir && r.open)!
  const { ui: uiOpen, state: stateOpen } = createMockUi({
    'files:sel': openFolder.key,
    'files:open': JSON.stringify([openFolder.path]),
  })
  const treeOpen = FilesTab(stubEls, view, 46, uiOpen)
  pressButton(treeOpen, 'files-key-h')
  const openListAfterH = JSON.parse(stateOpen['files:open'] ?? '[]') as string[]
  expect(openListAfterH).not.toContain(openFolder.path)

  // Case 3: on a file, pressing h moves selection to parent folder
  const childFile = view.rows.find(r => !r.dir && r.path === '/repo/docs/guide.md')!
  const parentFolder = view.rows.find(r => r.dir && r.path === '/repo/docs')!
  const { ui: uiFile, state: stateFile, revealed } = createMockUi({ 'files:sel': childFile.key })
  const treeFile = FilesTab(stubEls, view, 46, uiFile)
  pressButton(treeFile, 'files-key-h')
  expect(stateFile['files:sel']).toBe(parentFolder.key)
  expect(revealed).toContain(parentFolder.key)
})

test('Files F3.1: pressing a row or Enter on a focused row toggles folder and selects file', () => {
  const view = filetreeFixtureView()
  const { ui, state, revealed } = createMockUi()
  const tree = FilesTab(stubEls, view, 46, ui)

  // Pressing a folder row toggles it and sets selection
  const folderRow = view.rows.find(r => r.dir && r.open)!
  pressButton(tree, folderRow.key)
  expect(state['files:sel']).toBe(folderRow.key)
  const openList = JSON.parse(state['files:open'] ?? '[]') as string[]
  expect(openList).not.toContain(folderRow.path) // toggled closed

  // Pressing a file row selects it and calls reveal
  const fileRow = view.rows.find(r => !r.dir && r.path === '/repo/README.md')!
  pressButton(tree, fileRow.key)
  expect(state['files:sel']).toBe(fileRow.key)
  expect(revealed).toContain(fileRow.key)
})

test('Files F3.1: pressing search button calls ui.focus for files-q, never reads settings:focus, and hotkeys j k h l s are always armed', () => {
  const view = filetreeFixtureView()
  const { ui, focused, gets } = createMockUi({ 'settings:focus': 'files-q' })
  const tree = FilesTab(stubEls, view, 46, ui)

  // Pressing search button files-s calls ui.focus('files-q')
  pressButton(tree, 'files-s')
  expect(focused).toContain('files-q')

  // The tab never reads settings:focus
  expect(gets).not.toContain('settings:focus')

  // Buttons j, k, h, l, s are armed with their hotkeys
  const buttons = nodesOf(tree, 'Button')
  const sBtn = buttons.find(b => b.props?.key === 'files-s')!
  expect(sBtn.props?.hotkey).toBe('s')
  const jBtn = buttons.find(b => b.props?.key === 'files-key-j')!
  expect(jBtn.props?.hotkey).toBe('j')
  const kBtn = buttons.find(b => b.props?.key === 'files-key-k')!
  expect(kBtn.props?.hotkey).toBe('k')
  const hBtn = buttons.find(b => b.props?.key === 'files-key-h')!
  expect(hBtn.props?.hotkey).toBe('h')
  const lBtn = buttons.find(b => b.props?.key === 'files-key-l')!
  expect(lBtn.props?.hotkey).toBe('l')
})

test('Files F3.1: submitting search input keeps files:q and focuses selected row if visible, else first path row, else files-s', () => {
  const view = filetreeFixtureView()
  const pathRows = view.rows.filter(r => !r.note)

  // 1. Selected row is still visible: focuses selected row
  const { ui: ui1, state: state1, focused: focused1 } = createMockUi({ 'files:sel': pathRows[2]!.key })
  const tree1 = FilesTab(stubEls, view, 46, ui1)
  const input1 = nodesOf(tree1, 'Input').find(i => i.props?.key === 'files-q')!
  ;(input1.props?.onSubmit as (v: string) => void)('newquery')
  expect(state1['files:q']).toBe('newquery')
  expect(focused1).toEqual([pathRows[2]!.key])

  // 2. Selected row filtered out (e.g. was some path not in view.rows): focuses first visible path row
  const { ui: ui2, state: state2, focused: focused2 } = createMockUi({ 'files:sel': 'files-row:/vanished/path' })
  const tree2 = FilesTab(stubEls, view, 46, ui2)
  const input2 = nodesOf(tree2, 'Input').find(i => i.props?.key === 'files-q')!
  ;(input2.props?.onSubmit as (v: string) => void)('filtered')
  expect(state2['files:q']).toBe('filtered')
  expect(focused2).toEqual([pathRows[0]!.key])

  // 3. First visible row is a note row (no path rows at all): focuses files-s
  const noteOnlyView = filetreeFixtureView({
    rows: [
      {
        key: 'files-empty:/repo/empty',
        path: '/repo/empty',
        name: '(empty)',
        depth: 0,
        dir: false,
        open: false,
        hidden: false,
        link: false,
        icon: null,
        count: 0,
        note: 'empty',
      },
    ],
  })
  const { ui: ui3, focused: focused3 } = createMockUi()
  const tree3 = FilesTab(stubEls, noteOnlyView, 46, ui3)
  const input3 = nodesOf(tree3, 'Input').find(i => i.props?.key === 'files-q')!
  ;(input3.props?.onSubmit as (v: string) => void)('emptysearch')
  expect(focused3).toEqual(['files-s'])
})

test('Files F3.1: selected row draws lime left bar on selected fill and selection move calls ui.reveal with row key', () => {
  const view = filetreeFixtureView()
  const selectedRow = view.rows.find(r => r.key === view.sel)!
  const { ui, revealed } = createMockUi({ 'files:sel': selectedRow.key })
  const tree = FilesTab(stubEls, view, 46, ui)

  // Selected row draws lime bar on selected fill
  const selBtn = lineOf(tree, selectedRow.key)
  const selBox = nodesOf(selBtn, 'Box')[0]!
  expect(selBox.props?.backgroundColor).toBe(FT.selectedRow)
  const barText = nodesOf(selBtn, 'Text').find(t => textOf(t).includes('▌'))!
  expect(textOf(barText)).toBe('▌')
  expect(barText.props?.color).toBe(FT.limeBar)

  // Selection move calls ui.reveal with that row's key
  pressButton(tree, 'files-key-j')
  expect(revealed.length).toBeGreaterThan(0)
})

// ────────────────────── Polish round: labels, whole-row Button, h on folded folder, render dump ──────────────────────

type Seg = { t: string; fg?: string; bg?: string; flags?: string }
type Block = Seg[][]

const COLOUR_NAMES = new Map<string, string>(Object.entries(FT).map(([k, v]) => [v, k]))
const cname = (c: unknown): string | undefined => (typeof c === 'string' ? COLOUR_NAMES.get(c) ?? c : undefined)
const segW = (line: Seg[]): number => line.reduce((n, s) => n + s.t.length, 0)

/**
 * A tiny text layout of the stub tree: Box column or row (gap, margin, border, paddingX, space-between,
 * flex-end, wrap), Text, Button (hotkey prints `<hotkey>: <label>`) and Input. Enough to read the tab as lines.
 */
function layoutNode(n: unknown, avail: number, bg?: string, stretch = true): Block {
  if (typeof n === 'string') return [[{ t: n, bg }]]
  const node = n as Node
  const p = node.props ?? {}
  const kidsOf = (b?: string): Block[] => (node.children ?? []).map(c => layoutNode(c, avail, b, false))
  if (node.type === 'Text') {
    const flags = `${p.bold ? 'b' : ''}${p.dimColor ? 'd' : ''}`
    const text = (node.children ?? []).map(c => (typeof c === 'string' ? c : textOf(c))).join('')
    return [[{ t: text, fg: cname(p.color), bg: cname(p.backgroundColor) ?? bg, flags }]]
  }
  if (node.type === 'Button') {
    const parts: Seg[] = []
    if (typeof p.hotkey === 'string') parts.push({ t: `${p.hotkey}: `, flags: 'hk', bg })
    if (typeof p.label === 'string') parts.push({ t: p.label, bg })
    for (const k of kidsOf(bg)) parts.push(...(k[0] ?? []))
    return [parts]
  }
  if (node.type === 'Input') return [[{ t: `[${String(p.value || p.placeholder)}]`, flags: 'input', bg }]]
  // Box
  const ownBg = cname(p.backgroundColor) ?? bg
  const bordered = Boolean(p.borderStyle)
  const padX = Number(p.paddingX ?? 0)
  const inner = (typeof p.width === 'number' ? p.width : avail) - (bordered ? 2 : 0) - padX * 2
  const gap = Number(p.gap ?? 0)
  const blocks = (node.children ?? []).flat().filter(Boolean).map(c => layoutNode(c, inner, ownBg, p.flexDirection !== 'row'))
  let lines: Seg[][] = []
  if (p.flexDirection === 'row') {
    const widths = blocks.map(b => Math.max(0, ...b.map(segW)))
    const rows: Block[][] = [[]]
    let used = 0
    blocks.forEach((b, i) => {
      if (p.flexWrap === 'wrap' && used > 0 && used + gap + widths[i]! > inner) { rows.push([]); used = 0 }
      rows[rows.length - 1]!.push(b)
      used += (used > 0 ? gap : 0) + widths[i]!
    })
    for (const r of rows) {
      const rw = r.map(b => Math.max(0, ...b.map(segW)))
      const free = Math.max(0, inner - rw.reduce((a, b) => a + b, 0) - gap * Math.max(0, r.length - 1))
      const height = Math.max(1, ...r.map(b => b.length))
      for (let y = 0; y < height; y++) {
        const line: Seg[] = []
        r.forEach((b, i) => {
          if (i > 0) {
            const sp = stretch && p.justifyContent === 'space-between' && r.length === 2 ? free + gap : gap
            line.push({ t: ' '.repeat(sp), bg: ownBg })
          } else if (stretch && p.justifyContent === 'flex-end' && free > 0) line.push({ t: ' '.repeat(free), bg: ownBg })
          line.push(...(b[y] ?? [{ t: ' '.repeat(rw[i]!), bg: ownBg }]))
        })
        lines.push(line)
      }
    }
  } else {
    blocks.forEach((b, i) => {
      if (i > 0) for (let g = 0; g < gap; g++) lines.push([])
      lines.push(...b)
    })
  }
  if (p.marginTop) lines = [...Array.from({ length: Number(p.marginTop) }, () => [] as Seg[]), ...lines]
  if (bordered || padX) {
    const w = typeof p.width === 'number' ? p.width - (bordered ? 2 : 0) - padX * 2 : Math.max(0, ...lines.map(segW))
    lines = lines.map(l => [
      ...(bordered ? [{ t: '│', fg: cname(p.borderColor) }] : []), { t: ' '.repeat(padX), bg: ownBg }, ...l,
      { t: ' '.repeat(Math.max(0, w - segW(l)) + padX), bg: ownBg }, ...(bordered ? [{ t: '│', fg: cname(p.borderColor) }] : []),
    ])
    if (bordered) {
      const bw = w + padX * 2
      lines = [[{ t: `╭${'─'.repeat(bw)}╮`, fg: cname(p.borderColor) }], ...lines, [{ t: `╰${'─'.repeat(bw)}╯`, fg: cname(p.borderColor) }]]
    }
  }
  return lines
}

/** plain lines (what the terminal draws) and the same lines with [fg/bg flags:text] notes per Text */
function renderLines(tree: unknown, cols: number): { plain: string[]; noted: string[] } {
  const block = layoutNode(tree, cols)
  return {
    plain: block.map(l => l.map(s => s.t).join('')),
    noted: block.map(l => l.map(s => (s.fg || s.bg || s.flags ? `⟦${[s.fg, s.bg && `bg:${s.bg}`, s.flags].filter(Boolean).join(' ')}|${s.t}⟧` : s.t)).join('')),
  }
}

test('Files polish: no hotkey letter is printed twice, every keyed Button reads `<hotkey>: <word>` and fits at 80, 46 and 36 columns', () => {
  const view = filetreeFixtureView()
  for (const cols of [80, 46, 36]) {
    const { plain } = renderLines(FilesTab(stubEls, view, cols, createMockUi().ui), cols)
    const dump = plain.join('\n')
    expect(dump).toContain('s: search')
    for (const want of ['j: down', 'k: up', 'h: fold', 'l: open']) expect(dump).toContain(want)
    expect(dump).not.toMatch(/\b([jklhs]): \1\b/)
    expect(dump).not.toContain('s : search')
  }
  const tree = FilesTab(stubEls, view, 46, createMockUi().ui)
  const labels = Object.fromEntries(['files-s', 'files-key-j', 'files-key-k', 'files-key-h', 'files-key-l'].map(k => {
    const b = nodesOf(tree, 'Button').find(x => x.props?.key === k)!
    return [k, b.props?.label]
  }))
  expect(labels).toEqual({ 'files-s': 'search', 'files-key-j': 'down', 'files-key-k': 'up', 'files-key-h': 'fold', 'files-key-l': 'open' })
})

test('Files polish: the icon and count are Text inside the row Button, keep their colours, and every row line is exactly the box width at 80, 46 and 36 columns', () => {
  for (const pulse of [true, false]) {
    const view = filetreeFixtureView({ pulse, sel: '' })
    for (const cols of [80, 46, 36]) {
      const tree = FilesTab(stubEls, view, cols, createMockUi().ui)
      const mkdb = view.rows.find(r => r.path === '/repo/scripts/mkdb.py')!
      const btn = nodesOf(tree, 'Button').find(b => b.props?.key === mkdb.key)!
      const icon = nodesOf(btn, 'Text').find(t => textOf(t).includes('●'))
      expect(icon, 'write icon sits inside the row Button').toBeDefined()
      expect(icon!.props?.color).toBe(FT.write)
      expect(icon!.props?.dimColor).toBe(!pulse)
      const read = view.rows.find(r => r.path === '/repo/docs/guide.md')!
      const readBtn = nodesOf(tree, 'Button').find(b => b.props?.key === read.key)!
      expect(nodesOf(readBtn, 'Text').find(t => textOf(t).includes('◉'))?.props?.color).toBe(FT.read)
      // outside the Button the line Box holds nothing else
      expect(nodesOf(lineOf(tree, mkdb.key), 'Text').length).toBe(nodesOf(btn, 'Text').length)
      // every path row is exactly the tree box's inner width, so the whole line is the click target
      for (const b of nodesOf(tree, 'Button').filter(x => String(x.props?.key).startsWith('files-row:'))) {
        const w = nodesOf(b, 'Text').reduce((n, t) => n + textOf(t).length, 0)
        expect(w, `${String(b.props?.key)} at ${cols}`).toBe(Math.max(12, cols) - 2)
      }
    }
  }
  // a folded folder keeps its count inside the Button
  const folded: Row = { key: 'files-row:/repo/f', path: '/repo/f', name: 'f', depth: 0, dir: true, open: false, hidden: false, link: false, icon: 'read', count: 4 }
  const tree = FilesTab(stubEls, filetreeFixtureView({ rows: [folded], sel: '' }), 36, createMockUi().ui)
  const btn = nodesOf(tree, 'Button').find(b => b.props?.key === folded.key)!
  expect(textOf(btn)).toMatch(/◉ 4 $/)
})

test('Files polish: h on a folded folder moves to its parent folder, on a depth-0 folder it does nothing', () => {
  const view = filetreeFixtureView()
  const tabs = view.rows.find(r => r.path === '/repo/hooks/tabs')!
  const hooks = view.rows.find(r => r.path === '/repo/hooks')!
  // /repo/hooks/tabs shown folded (its parent is open): h goes up to hooks
  const foldedTabs = { ...view, rows: view.rows.map(r => (r.path === tabs.path ? { ...r, open: false } : r)) }
  const { ui, state, revealed } = createMockUi({ 'files:sel': tabs.key })
  pressButton(FilesTab(stubEls, foldedTabs, 46, ui), 'files-key-h')
  expect(state['files:sel']).toBe(hooks.key)
  expect(revealed).toContain(hooks.key)
  expect(state['files:open']).toBeUndefined()
  // depth-0 folded folder: no parent row, nothing changes
  const docs = view.rows.find(r => r.path === '/repo/docs')!
  const foldedDocs = { ...view, rows: view.rows.map(r => (r.path === docs.path ? { ...r, open: false } : r)) }
  const { ui: ui0, state: state0, revealed: revealed0 } = createMockUi({ 'files:sel': docs.key })
  pressButton(FilesTab(stubEls, foldedDocs, 46, ui0), 'files-key-h')
  expect(state0['files:sel']).toBe(docs.key)
  expect(state0['files:open']).toBeUndefined()
  expect(revealed0).toEqual([])
})

test('Files polish: the tab drawn as text lines at 80, 46 and 36 columns, with every note, never wider than the columns', () => {
  const extra: Row[] = [
    { key: 'files-more:/repo/hooks', path: '/repo/hooks', name: 'more', depth: 1, dir: false, open: false, hidden: false, link: false, icon: null, count: 12, note: 'more' },
  ]
  const base = filetreeFixtureView()
  const view = filetreeFixtureView({ rows: [...base.rows, ...extra], note: 'missing root, fallback to repo', capped: true })
  for (const cols of [80, 46, 36]) {
    const r = renderLines(FilesTab(stubEls, view, cols, createMockUi().ui), cols)
    for (const l of r.plain) expect(l.length, `${cols}: ${l}`).toBeLessThanOrEqual(cols)
  }
})

test('Files polish: note rows start in the name column, the no-match line shows the query, the footer sits one blank line under the tree line', () => {
  const view = filetreeFixtureView()
  const { plain } = renderLines(FilesTab(stubEls, view, 46, createMockUi().ui), 46)
  const col = (needle: string): number => plain.find(l => l.includes(needle))!.indexOf(needle)
  expect(col('(empty)')).toBe(col('guide.md'))
  const none = renderLines(FilesTab(stubEls, filetreeFixtureView({ rows: [], q: 'zzz' }), 46, createMockUi().ui), 46).plain
  expect(none.some(l => l.includes('no file matches “zzz”'))).toBe(true)
  const under = plain.findIndex(l => l.includes('folders ·'))
  expect(plain[under + 1]).toBe('')
  expect(plain[under + 2]).toContain('writing')
})
