// The /mk pane's Waiting tab (plan dashboard-v2-p0b, V2.3; contract C9/C11/C12):
// the decision as one kit Card with a warn border and a panel fill — a solid warn
// `! DECISION` chip over the task, the question wrapped, the options as rows (an
// accent chip on the recommended one, the picked one showing as picked) and the
// actions as kit ActionButtons with the mockup's `a`/`b`/`c` picks.
//
// C10: a pick only writes the local view state (`waiting:pick`) and the `⋯`
// reveal only `waiting:more` — the tab calls no script, and without a `ui` the
// fixture's default state draws and every press does nothing.
import type { Elements, RenderElement } from 'claude-code'
import { WAITING_FIXTURE } from './waiting.fixture'
import { NARROW } from './ui'
import { ActionButton, ActionButtons, ActionRow, Card, Chip } from './kit'
import type { ActionSpec, ActionTone } from './kit'
import { ROLE } from './palette'
import type { PaletteRole } from './palette'

type Els = Elements[keyof Elements]

/** The local view state V1 hands every mock tab (C12), declared structurally. */
export type TabUi = { get(key: string): string | undefined; set(key: string, value: string): void }

/** one decision option: its key (`A`), the label, whether it is the recommended
 *  one, and the explanation under it. `action` is the short word its ActionButton
 *  carries (the mockup's chip form); `row` is the option's own row width. */
export type WaitOption = { key: string; label: string; recommended?: boolean; action: string; explanation: string }

export type WaitingView = {
  mock: boolean
  header: string
  time: string
  timeCompact: string
  question: string
  options: WaitOption[]
  /** the dim hints under the actions */
  actions: string[]
  footerLeft: string
  footerRight: string
}

/** the fixture with the mock flag (C1/C3). */
export function waitingView(): WaitingView {
  return { mock: true, ...WAITING_FIXTURE }
}

/** where the tab keeps its view state (C12). */
export function waitingPickKey(): string {
  return 'waiting:pick'
}

/** where the `⋯` reveal keeps its flag (C12). */
export function waitingMoreKey(): string {
  return 'waiting:more'
}

/** the option the view state holds, when it is one this decision offers. */
export function waitingPick(ui: TabUi | undefined, view: WaitingView): string | undefined {
  const held = ui?.get(waitingPickKey())
  return held != null && view.options.some(o => o.key === held) ? held : undefined
}

/** cut `text` to `w` cells, marking the cut with `…` — a row never wraps. */
function cut(text: string, w: number): string {
  if (w <= 0) return ''
  if (text.length <= w) return text
  if (w === 1) return '…'
  return `${text.slice(0, w - 1)}…`
}

/**
 * word-wrap prose to rows of at most `w` cells: each sentence starts a new row
 * (so the question's "How should P6 proceed?" stays whole) and continuation rows
 * keep the first row's column (a hanging indent).
 */
function wrapProse(text: string, w: number, firstPrefix: string, contPrefix: string): string[] {
  const out: string[] = []
  let prefix = firstPrefix
  for (const sentence of text.split(/(?<=[.?])\s+/)) {
    let cur = ''
    for (const raw of sentence.split(' ').filter(Boolean)) {
      const room = Math.max(1, w - prefix.length)
      const word = raw.length > room ? cut(raw, room) : raw
      const cand = cur ? `${cur} ${word}` : word
      if (cur && cand.length > room) {
        out.push(prefix + cur)
        prefix = contPrefix
        cur = word
      } else {
        cur = cand
      }
    }
    if (cur) {
      out.push(prefix + cur)
      prefix = contPrefix
    }
  }
  return out
}

/** one dim row: the left text, then the right tail (the tail survives). */
function hintRow(left: string, right: string, w: number): string {
  return `${cut(left, Math.max(0, w - right.length))}${cut(right, w)}`
}

/** the card's header: the solid warn chip, the task, the open time right. */
function headerRow(els: Els, view: WaitingView, cols: number, w: number): RenderElement {
  const { Box, Text } = els
  const chip = '! DECISION'
  const right = cut(cols >= NARROW ? view.time : view.timeCompact, Math.max(0, Math.floor(w / 3)))
  const room = Math.max(1, w - (chip.length + 2) - right.length - 2)
  return (
    <Box flexDirection="row" gap={1} justifyContent="space-between">
      {Chip(els, { label: chip, role: 'warn', solid: true })}
      <Text bold={true}>{cut(view.header, room)}</Text>
      <Text dimColor={true}>{right}</Text>
    </Box>
  )
}

/** the option's chip: solid accent when it is the recommended one, solid ok when
 *  it is the one picked, the resting idle chip otherwise (the mockup's cB / cn). */
export function optionChipRole(option: WaitOption, picked: boolean): { role: PaletteRole; solid: boolean } {
  if (picked) return { role: 'ok', solid: true }
  if (option.recommended === true) return { role: 'accent', solid: true }
  return { role: 'idle', solid: false }
}

/** one option: its chip and label on a row, the explanation wrapped under it at
 *  the chip's hanging indent (the Main board's 4ch padding). */
function optionRows(els: Els, option: WaitOption, picked: boolean, w: number): RenderElement[] {
  const { Box, Text } = els
  const chip = optionChipRole(option, picked)
  const budget = Math.max(1, w - (option.key.length + 2) - 1)
  const prose = option.recommended === true ? `★ recommended · ${option.explanation}` : option.explanation
  return [
    (
      <Box key={`${option.key}-row`} flexDirection="row" gap={1}>
        {Chip(els, { label: option.key, role: chip.role, solid: chip.solid })}
        <Text bold={picked || option.recommended === true}>{cut(option.label, budget)}</Text>
      </Box>
    ),
    ...wrapProse(prose, w, '    ', '    ').map((line, i) => (
      <Text key={`${option.key}-why-${String(i)}`} dimColor={true}>{line}</Text>
    )),
  ]
}

/** the action specs: one button per option (`a`/`b`/`c` are engine hotkeys) and
 *  the quiet `? ask in chat`. Glyphs never clash with the tab bar's own. */
export function waitingActions(view: WaitingView, ui: TabUi | undefined): ActionSpec[] {
  const pick = waitingPick(ui, view)
  const optionButtons = view.options.map((o, i) => ({
    key: `waiting-${o.key.toLowerCase()}`,
    glyph: String.fromCharCode(97 + i), // a, b, c
    word: pick === o.key ? `${o.action} ✓` : o.action,
    tone: (o.recommended === true ? 'primary' : 'default') as ActionTone,
    primary: o.recommended === true,
    onPress: () => { ui?.set(waitingPickKey(), o.key) },
  }))
  return [
    ...optionButtons,
    { key: 'waiting-ask', glyph: '?', word: 'ask in chat', tone: 'quiet' as const, onPress: () => {} },
  ]
}

/** the action row: folded to the primary plus `⋯ More` when the row is too wide,
 *  and `⋯` reveals the rest as one button per row (a column is what fits a 36-col
 *  pane; a second four-button row does not). */
function actionRows(els: Els, view: WaitingView, ui: TabUi | undefined, w: number): RenderElement[] {
  const buttons = waitingActions(view, ui)
  const revealed = ui?.get(waitingMoreKey()) === '1'
  const primary = buttons.find(b => b.primary === true) ?? buttons[0]
  const folded = buttons.filter(b => b !== primary)
  const less: ActionSpec = { key: 'waiting-less', glyph: '⋯', word: 'less', tone: 'quiet', plain: true, onPress: () => { ui?.set(waitingMoreKey(), '') } }
  // the kit folds the rest into its own `⋯ More` (onMore is what a press runs)
  if (!revealed || primary == null) return [ActionButtons(els, buttons, w, () => { ui?.set(waitingMoreKey(), '1') })]
  return [
    ActionButtons(els, [primary, less], w),
    ...folded.map(b => ActionButton(els, b)),
  ]
}

/** Pure render: elements only, no $, no state (C1). */
export function WaitingTab(els: Els, view: WaitingView, cols: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const w = Math.max(8, cols - 4) // the Card's border + padding each side (C11)
  const picked = waitingPick(ui, view)
  const rows: RenderElement[] = [headerRow(els, view, cols, w)]
  for (const [i, line] of wrapProse(view.question, w, '  ', '  ').entries()) {
    rows.push(<Text key={`q-${String(i)}`}>{line}</Text>)
  }
  for (const option of view.options) rows.push(...optionRows(els, option, picked === option.key, w))
  rows.push(...actionRows(els, view, ui, w))
  for (const hint of view.actions) rows.push(ActionRow(els, { actions: cut(hint, w) }))
  return (
    <Box flexDirection="column">
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      {Card(els, { role: 'warn' }, <Box flexDirection="column">{rows}</Box>)}
      <Text dimColor={true}>{hintRow(view.footerLeft, view.footerRight, cols)}</Text>
    </Box>
  )
}