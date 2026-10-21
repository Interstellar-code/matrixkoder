// The Improve TAB: the full review screen for the one shared proposal (the footer card draws
// the same proposal in a small card). It reads the same view and presses through the same
// `improveAct`, so a press does the same thing in either place. The shared drawing helpers
// (the question list, the chip rows, the wrapped text) live in improve.tsx, next to the footer
// card they serve. A pure render (C10): no $, no state reads.
import type { Elements, RenderElement } from 'claude-code'
import { answeredLabel, bigDigits, bigNumber, CARD_HEAD, canAccept, clip, finalText, improveError, modeOf, pendingPlanPrompt, PLAN_PROMPT_CHIP, planPromptLine, wordCount, workingText } from '../improve-core'
import { ActionButton, Card, Section } from './kit'
import { PALETTE, ROLE } from './palette'
import type { ChipSpec, ImproveKind, ImproveUi, ImproveView } from './improve'
import { DRAFT_WORDS, chipRows, clean, field, lines, questions, wizardUnusedNote } from './improve'

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
  /** the card's head row: the head on the left, the count beside it on the right (the footer's head) */
  const head = (left: string, right?: string): RenderElement => (
    <Box flexDirection="row" justifyContent="space-between">
      <Text bold={true} color={ROLE.live.fg}>{left}</Text>
      {right != null && <Text dimColor={true}>{right}</Text>}
    </Box>
  )
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
    // the slide's tiles read `before` / `after` / `added`; the long labels need a wide pane
    const isLong = cols >= 60 && widths.every((tileW, i) => tileW - 2 >= (longs[i] ?? '').length)
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
            ...(pendingPlanPrompt(f) == null ? [] : [{ key: 'improve-t-plan', label: PLAN_PROMPT_CHIP, tone: 'soft' as const, onPress: () => act('plan-prompt') }]),
            discard,
          ], cols, 'improve-t-btns')}
          {f.note !== '' && pendingPlanPrompt(f) == null && <Text dimColor={true} wrap="truncate-end">{f.note}</Text>}
          {planPromptLine(f) !== '' && <Text dimColor={true} wrap="truncate-end">{planPromptLine(f)}</Text>}
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
    // the wizard's head is the footer's head: `✦ WIZARD` with the answered count beside it (R-7, R-8)
    body = Card(els, {}, (
      <Box flexDirection="column">
        {head(CARD_HEAD.wizard, answeredLabel(f))}
        {questions(els, f, w, act, 'improve-t')}
      </Box>
    ))
  } else if (f.step === 'error') {
    body = Card(els, {}, (
      <Box flexDirection="column">
        {head(modeOf(f) === 'improve' ? CARD_HEAD.improve : DRAFT_WORDS[modeOf(f) as 'answer' | 'question'].short)}
        {canAccept(f) && lines(els, 'improve-t-prev', finalText(f), w, 8, { color: PALETTE.fg })}
        {lines(els, 'improve-t-err', improveError(f), w, 3, { color: ROLE.warn.fg })}
        {wizardUnusedNote(f) !== '' && lines(els, 'improve-t-note', wizardUnusedNote(f), w, 2, { dimColor: true })}
        {chipRows(els, [
          { key: 'improve-t-retry', label: 'Try again', tone: 'solid', onPress: () => act('retry') },
          ...(canAccept(f) ? [{ key: 'improve-t-accept', label: '↵ Accept', tone: 'soft' as const, onPress: () => act('accept') }] : []),
          discard,
        ], w, 'improve-t-err')}
      </Box>
    ))
  } else {
    // the working card: the same head and the same lines the footer card shows (R-8, R-13)
    const draft = modeOf(f) !== 'improve'
    const wt = draft ? { head: DRAFT_WORDS[modeOf(f) as 'answer' | 'question'].short, lines: [DRAFT_WORDS[modeOf(f) as 'answer' | 'question'].doing] } : workingText(f)
    body = Card(els, {}, (
      <Box flexDirection="column">
        {head(wt.head)}
        {wt.lines.map((ln, i) => lines(els, `improve-t-doing${String(i)}`, ln, w, 2, { dimColor: true }))}
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
