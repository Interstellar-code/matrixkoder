// The Improve TAB: the full review screen for the one shared proposal (the footer card draws
// the same proposal in a small card). It reads the same view and presses through the same
// `improveAct`, so a press does the same thing in either place. The shared drawing helpers
// (the question list, the chip rows, the wrapped text) live in improve.tsx, next to the footer
// card they serve. A pure render (C10): no $, no state reads.
import type { Elements, RenderElement } from 'claude-code'
import { bigDigits, bigNumber, canAccept, clip, finalText, modeOf, wordCount, IMPROVE_FAILED } from '../improve-core'
import { ActionButton, Card, Section } from './kit'
import { PALETTE, ROLE } from './palette'
import type { ChipSpec, ImproveKind, ImproveUi, ImproveView } from './improve'
import { DRAFT_WORDS, chipRows, clean, field, lines, questions } from './improve'

type Els = Elements[keyof Elements]

/** `MM-DD HH:mm` in the local time zone */
function when(at: number): string {
  const d = new Date(at)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
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
