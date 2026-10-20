// The /mk pane's Improve parts, drawn from the Prompt Upgrade design in the matrix theme: the
// FOOTER (the proposal card and the Improve / Undo chips) that sits on every tab, and the
// Improve TAB (the full review screen). Both draw ONE shared proposal (view.flow), so a
// press does the same thing in either place. The prompt itself lives in Claude Code's own prompt
// line: Improve reads it and Accept writes it, so neither place draws a field for it.
// A pure render (C10): no $, no state reads; a press only calls `ui.improveAct`, which the pane
// turns into the state writes and model calls.
import type { Elements, RenderElement } from 'claude-code'
import type { MkImproveEntry, MkImproveFlow } from '../../types'
import { IMPROVE_FAILED, bigDigits, bigNumber, canAccept, clip, finalText, modeOf, wordCount, wrap } from '../improve-core'
import { IMPROVE_FIXTURE } from './improve.fixture'
import { ActionButton, Card, Chip, Section } from './kit'
import { PALETTE, ROLE } from './palette'

type Els = Elements[keyof Elements]

/** What a press asks of the pane. `a` / `b` carry the question index, the option or the text. */
export type ImproveKind =
  | 'improve' | 'accept' | 'discard' | 'undo' | 'wizard' | 'retry' | 'copy' | 'open'
  | 'answer' | 'other' | 'other-draft' | 'other-save' | 'skip' | 'go' | 'edit' | 'edit-save' | 'draft'
  // the footer drop-up: 'menu' opens or closes it, 'compose' drafts a message ('answer' | 'question' in `a`),
  // 'send' submits the drafted message as the person's own
  | 'menu' | 'compose' | 'send'

export type ImproveUi = {
  improveAct?(kind: ImproveKind, a?: string | number, b?: string): void
}

/** everything the footer and the tab draw */
export type ImproveView = {
  mock: boolean
  /** the prompt text before the last Accept; the Undo chip shows while it is set */
  undo: string | null
  flow: MkImproveFlow | null
  /** saved improvements, newest first */
  history: readonly MkImproveEntry[]
  /** the footer's drop-up is open */
  menu?: boolean
}

/** the drop-up's rows: key, chip label, what it does */
const MENU: readonly (readonly [string, string, string])[] = [
  ['improve-f-m-improve', '✦ Improve ', 'rewrite my prompt'],
  ['improve-f-m-answer', '✦ Answer  ', 'reply to the last message'],
  ['improve-f-m-question', '✦ Question', 'ask about it'],
]

/** the card head and working line for each mode's draft */
const DRAFT_WORDS = {
  answer: { head: '✦ REPLY WRITTEN FOR YOU', short: '✦ ANSWER', doing: 'Reading the latest messages and writing a reply for you…' },
  question: { head: '✦ QUESTION WRITTEN FOR YOU', short: '✦ QUESTION', doing: 'Reading the latest messages and writing a question for you…' },
} as const
/** under the written text: what the two buttons do, so the card needs no explanation */
const DRAFT_HELP = 'Send posts it as you. Edit puts it in your prompt line first.'

/** the fixture view (the design's sample prompt, a proposal on show) */
export function improveView(): ImproveView {
  return { ...IMPROVE_FIXTURE }
}

// drop control characters (the text is the person's, headed for the terminal), as todo.tsx does
const clean = (s: string): string => s.replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')

/** `MM-DD HH:mm` in the local time zone */
function when(at: number): string {
  const d = new Date(at)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

type Tone = 'solid' | 'on' | 'soft' | 'dim'
type ChipSpec = { key: string; label: string; tone: Tone; onPress: () => void }
const chipW = (c: ChipSpec): number => c.label.length + 2 // the Chip pads one cell each side

/** One chip-shaped press: a plain Button around a Chip text. solid = the green primary with black
 *  bold text, on = the picked option, soft = the default chip, dim = a quiet one. */
function chipBtn(els: Els, c: ChipSpec): RenderElement {
  const { Button, Text } = els
  const look = c.tone === 'dim'
    ? <Text backgroundColor={PALETTE.btn} color={PALETTE.dim}>{` ${c.label} `}</Text>
    : Chip(els, { label: c.label, role: c.tone === 'soft' ? 'idle' : 'live', solid: c.tone === 'solid' })
  return <Button key={c.key} plain={true} onPress={c.onPress}>{look}</Button>
}

/** Chips packed into rows of at most `w` cells (one cell between), so a row never wraps. */
function chipRows(els: Els, specs: readonly ChipSpec[], w: number, key: string): RenderElement {
  const { Box } = els
  const rows: ChipSpec[][] = [[]]
  let used = 0
  for (const c of specs) {
    const row = rows[rows.length - 1] ?? []
    if (row.length > 0 && used + 1 + chipW(c) > w) {
      rows.push([c])
      used = chipW(c)
    } else {
      used += row.length > 0 ? 1 + chipW(c) : chipW(c)
      row.push(c)
    }
  }
  return (
    <Box flexDirection="column">
      {rows.map((row, i) => (
        <Box key={`${key}-row${String(i)}`} flexDirection="row" gap={1}>{row.map(c => chipBtn(els, c))}</Box>
      ))}
    </Box>
  )
}

/** `text` wrapped to `w` cells, at most `max` lines then `… n more lines`. */
function lines(els: Els, key: string, text: string, w: number, max: number, style: { color?: string; dimColor?: boolean; strikethrough?: boolean; bold?: boolean }): RenderElement[] {
  const { Text } = els
  const all = wrap(text, w)
  const shown = all.slice(0, max)
  const more = all.length - shown.length
  const out = shown.map((ln, i) => (
    <Text key={`${key}${String(i)}`} {...style} wrap="truncate-end">{ln === '' ? ' ' : clean(ln)}</Text>
  ))
  if (more > 0) out.push(<Text key={`${key}more`} dimColor={true} wrap="truncate-end">{`… ${String(more)} more line${more > 1 ? 's' : ''}`}</Text>)
  return out
}

/** the `before → after words` label, shortened to `b→a` when `room` cells cannot hold it */
function countsLabel(before: number, after: number, room: number): string {
  const full = `${String(before)} → ${String(after)} words`
  return full.length <= room ? full : `${String(before)}→${String(after)}`
}

/** A one-line (or `rows`-line) text field; the surface carries no Input → null. */
function field(els: Els, props: { key: string; value: string; placeholder: string; rows?: number; autoFocus?: true; submitLabel?: string; onInput?: (v: string) => void; onSubmit: (v: string) => void }): RenderElement | null {
  if (!('Input' in els)) return null
  const { Input } = els
  return <Input {...props} />
}

/** The wizard's questions (up to three, 2-4 options each, plus Other and Skip), the answered count
 *  and the go button. One drawing for the footer card and the tab. */
function questions(els: Els, f: MkImproveFlow, w: number, act: (kind: ImproveKind, a?: string | number, b?: string) => void, p: string): RenderElement {
  const { Box, Text } = els
  const total = f.questions.length
  const done = Object.keys(f.answers).length
  const all = done === total
  return (
    <Box flexDirection="column" gap={1}>
      {f.questions.map((q, i) => {
        const a = f.answers[String(i)]
        const opts: ChipSpec[] = q.options.map((o, j) => {
          const picked = a?.kind === 'option' && a.text === o
          return { key: `${p}-q${String(i)}o${String(j)}`, label: picked ? `✓ ${clip(o, w - 6)}` : clip(o, w - 4), tone: picked ? 'on' : 'soft', onPress: () => act('answer', i, o) }
        })
        opts.push({
          key: `${p}-q${String(i)}other`, tone: a?.kind === 'other' ? 'on' : 'soft', onPress: () => act('other', i),
          label: a?.kind === 'other' ? `✓ Other: ${clip(a.text, Math.max(4, Math.min(24, w - 14)))}` : 'Other (type it)',
        })
        opts.push({ key: `${p}-q${String(i)}skip`, label: a?.kind === 'skip' ? 'skipped' : 'Skip', tone: 'dim', onPress: () => act('skip', i) })
        const box = f.other === i
          ? field(els, { key: `improve-other-${String(i)}`, autoFocus: true, placeholder: 'Type your answer', submitLabel: 'save', value: f.otherDraft, onInput: v => act('other-draft', i, v), onSubmit: v => act('other-save', i, v) })
          : null
        return (
          <Box key={`${p}-q${String(i)}`} flexDirection="column">
            {wrap(q.question, w - 2).map((ln, j) => (
              <Text key={`${p}-q${String(i)}l${String(j)}`} bold={true} wrap="truncate-end">{`${j === 0 ? (a ? '✓' : String(i + 1)) : ' '} ${clean(ln)}`}</Text>
            ))}
            <Box flexDirection="column" paddingLeft={2}>
              {chipRows(els, opts, w - 2, `${p}-q${String(i)}`)}
              {box}
            </Box>
          </Box>
        )
      })}
      <Text dimColor={true}>{`${String(done)} of ${String(total)} answered`}</Text>
      {chipRows(els, [
        { key: `${p}-upgrade`, label: all ? 'Upgrade now' : 'Skip the rest, upgrade now', tone: all ? 'solid' : 'soft', onPress: () => act('go') },
        { key: `${p}-discard`, label: 'Discard', tone: 'dim', onPress: () => act('discard') },
      ], w, `${p}-ask`)}
    </Box>
  )
}

/** The proposal's card in the footer, by step. */
function footerCard(els: Els, f: MkImproveFlow, cols: number, act: (kind: ImproveKind, a?: string | number, b?: string) => void): RenderElement {
  const { Box, Text } = els
  const w = Math.max(1, cols - 4) // the Card's content width
  const head = (left: string, right?: string): RenderElement => (
    <Box flexDirection="row" justifyContent="space-between">
      <Text bold={true} color={ROLE.live.fg}>{left}</Text>
      {right != null && <Text dimColor={true}>{right}</Text>}
    </Box>
  )
  const discard: ChipSpec = { key: 'improve-f-discard', label: 'Discard', tone: 'dim', onPress: () => act('discard') }
  const mode = modeOf(f)
  let body: RenderElement
  if (mode !== 'improve' && f.step === 'review') {
    // a drafted message: Send submits it as is, Edit puts it in the prompt line to change first
    body = (
      <Box flexDirection="column">
        {head(DRAFT_WORDS[mode].head, `${String(wordCount(finalText(f)))} words`)}
        {f.original !== '' && lines(els, 'improve-f-hint', `from what you typed: ${f.original}`, w, 1, { dimColor: true })}
        {lines(els, 'improve-f-new', finalText(f), w, 10, { color: PALETTE.fg })}
        {f.note !== '' ? <Text dimColor={true} wrap="truncate-end">{f.note}</Text> : lines(els, 'improve-f-help', DRAFT_HELP, w, 2, { dimColor: true })}
        {chipRows(els, [
          { key: 'improve-f-send', label: '↵ Send', tone: 'solid', onPress: () => act('send') },
          { key: 'improve-f-accept', label: 'Edit', tone: 'soft', onPress: () => act('accept') },
          discard,
        ], w, 'improve-f-review')}
      </Box>
    )
  } else if (mode !== 'improve' && f.step !== 'error') {
    body = (
      <Box flexDirection="column">
        {head(DRAFT_WORDS[mode].short)}
        {lines(els, 'improve-f-doing', DRAFT_WORDS[mode].doing, w, 2, { dimColor: true })}
        {chipRows(els, [discard], w, 'improve-f-doing')}
      </Box>
    )
  } else if (f.step === 'review') {
    body = (
      <Box flexDirection="column">
        {head('✦ IMPROVED PROMPT', countsLabel(wordCount(f.original), wordCount(finalText(f)), w - 18))}
        {lines(els, 'improve-f-was', f.original, w, 2, { dimColor: true, strikethrough: true })}
        {lines(els, 'improve-f-new', finalText(f), w, 10, { color: PALETTE.fg })}
        {f.note !== '' && <Text dimColor={true} wrap="truncate-end">{f.note}</Text>}
        {chipRows(els, [
          { key: 'improve-f-accept', label: '↵ Accept', tone: 'solid', onPress: () => act('accept') },
          { key: 'improve-f-wizard', label: 'Run Wizard', tone: 'soft', onPress: () => act('wizard') },
          discard,
        ], w, 'improve-f-review')}
      </Box>
    )
  } else if (f.step === 'questions') {
    body = <Box flexDirection="column">{head('✦ WIZARD')}{questions(els, f, w, act, 'improve-f')}</Box>
  } else if (f.step === 'error') {
    body = (
      <Box flexDirection="column">
        {head(mode === 'improve' ? '✦ IMPROVE' : DRAFT_WORDS[mode].short)}
        {canAccept(f) && lines(els, 'improve-f-prev', finalText(f), w, 4, { color: PALETTE.fg })}
        {lines(els, 'improve-f-err', f.error || IMPROVE_FAILED, w, 3, { color: ROLE.warn.fg })}
        {chipRows(els, [
          { key: 'improve-f-retry', label: 'Try again', tone: 'solid', onPress: () => act('retry') },
          ...(canAccept(f) ? [{ key: 'improve-f-accept', label: '↵ Accept', tone: 'soft' as const, onPress: () => act('accept') }] : []),
          discard,
        ], w, 'improve-f-err')}
      </Box>
    )
  } else {
    body = (
      <Box flexDirection="column">
        {head('✦ IMPROVING')}
        {lines(els, 'improve-f-doing', f.step === 'asking' ? 'Reading your prompt and working out what to ask…' : 'Writing the new prompt…', w, 2, { dimColor: true })}
        {chipRows(els, [discard], w, 'improve-f-doing')}
      </Box>
    )
  }
  return Card(els, {}, body)
}

/** A dashed, empty tool slot (the design's two `+ tool` placeholders); not a press. */
const SLOT = '┊ + tool ┊'

/**
 * The pane footer's rows, above the counts: the proposal card (`card` false on the Improve tab,
 * which draws the same proposal in full) and the chip row. Improve reads and writes Claude Code's
 * own prompt line, so the footer draws no field of its own; the chip row never wraps.
 */
export function ImproveFooter(els: Els, view: ImproveView, cols: number, ui: ImproveUi, card = true, chips = true): RenderElement {
  const { Box, Text } = els
  const act = (kind: ImproveKind, a?: string | number, b?: string): void => ui.improveAct?.(kind, a, b)
  const w = Math.max(1, cols - 2) // paddingX 1 each side
  const open = view.menu === true
  // a drop-up: the chip opens Improve / Answer / Question above the chip row
  const go: ChipSpec = { key: 'improve-f-go', label: open ? '✦ Improve ▾' : '✦ Improve ▴', tone: 'solid', onPress: () => act('menu') }
  const undo: ChipSpec | null = view.undo == null ? null : { key: 'improve-f-undo', label: '↶ Undo', tone: 'soft', onPress: () => act('undo') }
  // the slots join only while they fit
  let used = chipW(go) + (undo == null ? 0 : 1 + chipW(undo))
  const slots: string[] = []
  for (let i = 0; i < 2; i++) {
    if (used + 1 + SLOT.length > w) break
    used += 1 + SLOT.length
    slots.push(SLOT)
  }
  return (
    <Box flexDirection="column">
      {card && view.flow != null && footerCard(els, view.flow, cols, act)}
      {chips && open && MENU.map(([key, label, what]) => (
        <Box key={`${key}-row`} flexDirection="row" gap={1} paddingX={1}>
          {chipBtn(els, { key, label, tone: 'soft', onPress: () => (key === 'improve-f-m-improve' ? act('improve') : act('compose', key === 'improve-f-m-answer' ? 'answer' : 'question')) })}
          <Text dimColor={true} wrap="truncate-end">{what}</Text>
        </Box>
      ))}
      {chips && (
        <Box flexDirection="row" gap={1} paddingX={1}>
          {chipBtn(els, go)}
          {undo != null && chipBtn(els, undo)}
          {slots.map((s, i) => <Text key={`improve-slot${String(i)}`} color={PALETTE.edge}>{s}</Text>)}
        </Box>
      )}
    </Box>
  )
}

/** Pure render of the Improve tab: the review screen for the shared proposal, or the empty state. */
export function ImproveTab(els: Els, view: ImproveView, cols: number, ui: ImproveUi): RenderElement {
  const { Box, Text } = els
  const act = (kind: ImproveKind, a?: string | number, b?: string): void => ui.improveAct?.(kind, a, b)
  const f = view.flow
  const w = Math.max(1, cols - 4) // a Card's content width
  const discard: ChipSpec = { key: 'improve-t-discard', label: 'Discard', tone: 'dim', onPress: () => act('discard') }
  const status = f == null ? '' : f.step === 'review' ? 'proposal ready' : f.step === 'questions' ? 'questions' : f.step === 'error' ? 'could not improve' : 'working'

  let body: RenderElement
  if (f == null) {
    body = (
      <Box flexDirection="column" gap={1}>
        {Card(els, {}, (
          <Box flexDirection="column">
            <Text bold={true} color={ROLE.ok.fg}>✦ IMPROVE</Text>
            {lines(els, 'improve-t-empty', 'Type a prompt in Claude Code’s prompt line and press ✦ Improve.', w, 3, {})}
          </Box>
        ))}
        {view.history.length > 0 && (
          <Box flexDirection="column">
            {Section(els, { label: 'Recent', right: `${String(view.history.length)} saved` })}
            {view.history.slice(0, 20).map(en => (
              <Box key={`improve-t-hrow${en.id}`} flexDirection="row" paddingX={1}>
                {ActionButton(els, {
                  key: `improve-t-h${en.id}`, plain: true, word: '', onPress: () => act('open', en.id),
                  glyph: `${when(en.at)}  ${clip(clean(en.original).replace(/\s+/g, ' '), Math.max(4, cols - 2 - 13))}`,
                })}
              </Box>
            ))}
          </Box>
        )}
      </Box>
    )
  } else if (f.step === 'review') {
    const after = wordCount(finalText(f))
    const tw = Math.floor((cols - 2) / 3)
    const widths = [tw, tw, cols - 2 - 2 * tw]
    const longs = ['words before', 'words after', 'details added']
    const isLong = widths.every((tileW, i) => tileW - 2 >= (longs[i] ?? '').length)
    const tile = (i: number, n: number, color: string): RenderElement => {
      const tileW = widths[i] ?? tw
      const inner = tileW - 2
      return (
        <Box key={`improve-t-tile${String(i)}`} width={tileW} flexDirection="column" alignItems="center" borderStyle="round" borderColor={PALETTE.line} backgroundColor={PALETTE.panel}>
          {String(n).length <= bigDigits(inner)
            ? bigNumber(n, inner).map((r, j) => <Text key={`improve-t-tile${String(i)}r${String(j)}`} color={color} bold={true}>{r}</Text>)
            : <Text color={color} bold={true}>{String(n)}</Text>}
          <Text dimColor={true}>{isLong ? (longs[i] ?? '') : ['before', 'after', 'added'][i] ?? ''}</Text>
        </Box>
      )
    }
    const side = cols >= 60
    const cw = side ? Math.floor((cols - 1) / 2) : cols
    const max = side ? 12 : 8
    const cardOf = (key: string, label: string, text: string, inner: number, role: 'live' | undefined, style: { color?: string; dimColor?: boolean }): RenderElement => (
      <Box key={key} width={inner + 4} flexDirection="column">
        {Card(els, role == null ? {} : { role }, (
          <Box flexDirection="column">
            <Text bold={true} color={role == null ? PALETTE.mut : ROLE.live.fg}>{label}</Text>
            {lines(els, key, text, inner, max, style)}
          </Box>
        ))}
      </Box>
    )
    body = (
      <Box flexDirection="column" gap={1}>
        <Box flexDirection="row" gap={1}>
          {tile(0, wordCount(f.original), PALETTE.fg)}
          {tile(1, after, ROLE.live.fg)}
          {tile(2, f.added.length, ROLE.ok.fg)}
        </Box>
        <Box flexDirection="column">
          {chipRows(els, [
            ...(modeOf(f) === 'improve'
              ? [
                  { key: 'improve-t-accept', label: '↵ Accept', tone: 'solid' as const, onPress: () => act('accept') },
                  { key: 'improve-t-wizard', label: 'Run Wizard', tone: 'soft' as const, onPress: () => act('wizard') },
                ]
              : [
                  { key: 'improve-t-send', label: '↵ Send', tone: 'solid' as const, onPress: () => act('send') },
                  { key: 'improve-t-accept', label: 'To prompt line', tone: 'soft' as const, onPress: () => act('accept') },
                ]),
            { key: 'improve-t-edit', label: f.isEditing ? 'Stop editing' : 'Edit', tone: 'soft', onPress: () => act('edit') },
            { key: 'improve-t-copy', label: 'Copy', tone: 'soft', onPress: () => act('copy') },
            discard,
          ], cols, 'improve-t-btns')}
          {f.note !== '' && <Text dimColor={true} wrap="truncate-end">{f.note}</Text>}
        </Box>
        <Box flexDirection="column">
          {Section(els, { label: 'What I added' })}
          {f.added.length === 0 && <Text dimColor={true}>Nothing new was added.</Text>}
          {f.added.map((a, i) => (
            <Box key={`improve-t-add${String(i)}`} flexDirection="row" gap={1}>
              <Text color={ROLE.ok.fg}>+</Text>
              <Text wrap="truncate-end">{clip(clean(a), cols - 2)}</Text>
            </Box>
          ))}
        </Box>
        {f.isEditing ? (
          <Box flexDirection="column">
            {Section(els, { label: 'Edit, Enter keeps it' })}
            {field(els, { key: 'improve-edit', autoFocus: true, rows: 6, placeholder: 'The new prompt', submitLabel: 'keep', value: f.draft, onInput: v => act('draft', v), onSubmit: v => act('edit-save', undefined, v) })}
          </Box>
        ) : (
          <Box flexDirection="column">
            {Section(els, { label: 'Before and after' })}
            <Box flexDirection={side ? 'row' : 'column'} gap={1}>
              {cardOf('improve-t-was', modeOf(f) === 'improve' ? 'BEFORE' : 'WHAT YOU TYPED', f.original === '' && modeOf(f) !== 'improve' ? '(nothing)' : f.original, cw - 4, undefined, { dimColor: true })}
              {cardOf('improve-t-new', modeOf(f) === 'improve' ? 'AFTER' : 'WRITTEN FOR YOU', f.upgraded, (side ? cols - 1 - cw : cols) - 4, 'live', { color: PALETTE.fg })}
            </Box>
          </Box>
        )}
      </Box>
    )
  } else if (f.step === 'questions') {
    body = Card(els, {}, <Box flexDirection="column">{lines(els, 'improve-t-qh', 'A few quick questions', w, 1, { bold: true, color: ROLE.live.fg })}{questions(els, f, w, act, 'improve-t')}</Box>)
  } else if (f.step === 'error') {
    body = Card(els, {}, (
      <Box flexDirection="column">
        {canAccept(f) && lines(els, 'improve-t-prev', finalText(f), w, 8, { color: PALETTE.fg })}
        {lines(els, 'improve-t-err', f.error || IMPROVE_FAILED, w, 3, { color: ROLE.warn.fg })}
        {chipRows(els, [
          { key: 'improve-t-retry', label: 'Try again', tone: 'solid', onPress: () => act('retry') },
          ...(canAccept(f) ? [{ key: 'improve-t-accept', label: '↵ Accept', tone: 'soft' as const, onPress: () => act('accept') }] : []),
          discard,
        ], w, 'improve-t-err')}
      </Box>
    ))
  } else {
    body = Card(els, {}, (
      <Box flexDirection="column">
        {lines(els, 'improve-t-doing', modeOf(f) !== 'improve' ? DRAFT_WORDS[modeOf(f) as 'answer' | 'question'].doing : f.step === 'asking' ? 'Reading your prompt and working out what to ask…' : 'Writing the new prompt…', w, 2, { dimColor: true })}
        {lines(els, 'improve-t-mine', f.original, w, 4, { dimColor: true, strikethrough: true })}
        {chipRows(els, [discard], w, 'improve-t-doing')}
      </Box>
    ))
  }

  return (
    <Box flexDirection="column" gap={1}>
      {view.mock && <Text bold={true} color={ROLE.warn.fg}>MOCK · fixture data · not this run</Text>}
      <Box flexDirection="row" justifyContent="space-between" paddingX={1}>
        <Text bold={true} color={ROLE.ok.fg}>✦ Improve</Text>
        <Text dimColor={true}>{status}</Text>
      </Box>
      {body}
    </Box>
  )
}
