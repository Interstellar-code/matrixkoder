// The /mk pane's Events tab detail card: the full content of one event row,
// drawn in place of the list when a row is pressed (`events:detail` holds its
// id) — a kit Card titled `<id> · <kind>`: when (full date and time), source,
// from → to, task (or lane), reply_to, ack (bus rows), problem (a bad row) and
// the author, then the whole text (wrapped, one line per newline). An answer
// shows the ask it replies to, an ask the answer to it, when the view carries
// that row. A field the row does not carry is simply left out — no `undefined`
// text. The footer dim line `esc close` is pressable (`onClose` clears
// `events:detail`), as the Plan tab's task panel. Pure render — no $, no state.
import type { Elements, RenderElement } from 'claude-code'
import { ActionButton, Card, IconButton, iconWidth } from './kit'
import { ROLE } from './palette'
import type { EventItem } from './events'

type Els = Elements[keyof Elements]

/** the related message is cut to this many characters */
const RELATED_MAX = 300

/** `…`-truncate to `w` characters */
const clip = (text: string, w: number): string => (text.length <= w ? text : `${text.slice(0, Math.max(1, w) - 1)}…`)

/** a field the row carries: a string that is not empty */
const has = (v: string | undefined): v is string => v != null && v !== ''

/** the ack level as the card words it */
const ACK_WORD = ['none', 'delivered ✓', 'read ✓✓'] as const

/** One dim-key row. The value wraps, never cuts. */
function fieldRow(els: Els, key: string, value: string): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="row" gap={1}>
      <Text dimColor={true}>{key}</Text>
      <Text wrap="wrap">{value}</Text>
    </Box>
  )
}

/** The event detail card. Pure render from one row; every missing field is
 *  left out. `related` is the answer's ask or the ask's answer (null when the
 *  view does not carry it); `onClose` is the caller's callback for both close
 *  buttons. */
export function EventDetailPanel(els: Els, ev: EventItem, related: EventItem | null | undefined, cols: number, onClose?: () => void): RenderElement {
  const { Box, Text } = els
  const id = ev.id ?? ''
  const kind = ev.kind ?? ev.type ?? 'info'
  // the header row's text yields to the `✕ close` button (iconWidth cells and a gap) inside the Card (border and padding: 4)
  const title = clip(id === '' ? kind : `${id} · ${kind}`, Math.max(1, cols - 4 - 1 - iconWidth('close', true)))
  const when = has(ev.date) ? ev.date.replace('T', ' ').replace(/\.\d+/, '') : ev.t
  const route = has(ev.from) && has(ev.to) ? `${ev.from} → ${ev.to}` : null
  const lane = has(ev.lane) && ev.lane !== 'all' ? ev.lane : null
  const body = ev.full ?? ev.text
  const lines = body.trim() === '' ? [] : body.split(/\r?\n/)
  const relatedText = related == null ? '' : clip(related.full ?? related.text, RELATED_MAX)
  const relatedLabel = related == null ? '' : kind === 'answer' ? `answers ${ev.reply_to ?? related.id ?? ''}`.trim() : kind === 'ask' ? `answered by ${related.id ?? ''}`.trim() : ''
  return Card(els, { role: 'live' }, (
    <Box key={`ev-detail-panel-${id}`} flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text bold={true}>{title}</Text>
        {IconButton(els, { key: `ev-detail-close-top-${id}`, icon: 'close', labelled: true, onPress: () => { onClose?.() } })}
      </Box>
      {when !== '' && fieldRow(els, 'when', when)}
      {has(ev.src) && fieldRow(els, 'source', ev.src)}
      {route != null && fieldRow(els, 'from', route)}
      {route == null && has(ev.from) && fieldRow(els, 'from', ev.from)}
      {route == null && has(ev.to) && fieldRow(els, 'to', ev.to)}
      {has(ev.task) && fieldRow(els, 'task', ev.task)}
      {!has(ev.task) && lane != null && fieldRow(els, 'lane', lane)}
      {has(ev.reply_to) && fieldRow(els, 'reply_to', ev.reply_to)}
      {ev.src === 'bus' && fieldRow(els, 'ack', ACK_WORD[ev.ack === 2 ? 2 : ev.ack === 1 ? 1 : 0])}
      {ev.bad === true && (
        <Box flexDirection="row" gap={1}>
          <Text dimColor={true}>problem</Text>
          <Text color={ROLE.err.fg}>{'! yes'}</Text>
        </Box>
      )}
      {has(ev.by) && fieldRow(els, 'by', ev.by)}
      {lines.length > 0 && (
        <Box flexDirection="column" marginTop={1}>
          {lines.map((line, i) => <Text key={`ev-detail-line-${i}`} wrap="wrap">{line === '' ? ' ' : line}</Text>)}
        </Box>
      )}
      {related != null && relatedLabel !== '' && relatedText !== '' && (
        <Box flexDirection="column" marginTop={1}>
          <Text dimColor={true}>{relatedLabel}</Text>
          <Text dimColor={true} wrap="wrap">{relatedText}</Text>
        </Box>
      )}
      {ActionButton(els, {
        key: `ev-detail-close-${id}`,
        glyph: 'esc close',
        word: '',
        plain: true,
        tone: 'quiet',
        onPress: () => { onClose?.() },
      })}
    </Box>
  ))
}
