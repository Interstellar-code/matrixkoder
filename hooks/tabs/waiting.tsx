// the /mk pane's Waiting tab (plan dashboard-v2-p0, T2.3): the artboard-E
// decision card over the Checkout v2 fixture — the ONLY bordered box in the
// pane (C5), with the question, `A · <label>` bold options, dim two-space
// indented explanations ((recommended) marked) and dim action text. Pure
// render (C1): elements only, no $, no state.
import type { Elements } from 'claude-code'
import { WAITING_FIXTURE } from './waiting.fixture'
import { NARROW, leftCol, rightCol } from './ui'

type Els = Elements[keyof Elements]

/** the row vocabulary: theme colour keys only (C5), 'dim' as dimColor. */
export type Tone = 'text' | 'dim' | 'success' | 'warning' | 'error' | 'suggestion'
export type Seg = { text: string; tone?: Tone; bold?: boolean }

/** one decision option (artboard E): `A · <label>` plus its explanation. */
export type WaitOption = { key: string; label: string; recommended?: boolean; explanation: string }

export type WaitingView = {
  mock: boolean
  header: string
  time: string
  timeCompact: string
  question: string
  options: WaitOption[]
  actions: string[]
  footerLeft: string
  footerRight: string
}

/** the fixture with the mock flag (C1/C3). */
export function waitingView(): WaitingView {
  return { mock: true, ...WAITING_FIXTURE }
}

/** hard-truncate segments to `w` cells (leftCol semantics) so a row never wraps. */
function fitSegs(segs: Seg[], w: number): Seg[] {
  const out: Seg[] = []
  let used = 0
  for (const seg of segs) {
    const room = w - used
    if (room <= 0) break
    const text = seg.text.length > room ? leftCol(seg.text, room) : seg.text
    out.push({ ...seg, text })
    used += text.length
  }
  return out
}

/** right-aligned tail on a segmented row: the tail survives, the head truncates. */
function withRight(left: Seg[], right: string, w: number): Seg[] {
  const tail = right.length > w ? rightCol(right, w) : right
  const room = Math.max(0, w - tail.length)
  const fitted = fitSegs(left, room)
  const used = fitted.reduce((n, seg) => n + seg.text.length, 0)
  return [...fitted, { text: ' '.repeat(Math.max(0, room - used)) }, { text: tail, tone: 'dim' as Tone }]
}

/** cut a single-line item to `w` cells, marked with '…' (the tail goes). */
function cut(text: string, w: number): string {
  if (text.length <= w) return text
  if (w <= 1) return leftCol(text, w)
  return leftCol(text, w - 1) + '…'
}

/**
 * word-wrap prose to rows of at most `w` cells: each sentence starts a new row
 * (so the question's "How should P6 proceed?" stays whole) and continuation
 * rows keep the first row's column (hanging indent).
 */
function wrapProse(text: string, w: number, firstPrefix: string, contPrefix: string): string[] {
  const out: string[] = []
  let prefix = firstPrefix
  for (const sentence of text.split(/(?<=[.?])\s+/)) {
    let cur = ''
    for (const raw of sentence.split(' ').filter(Boolean)) {
      const room = Math.max(1, w - prefix.length)
      const word = raw.length > room ? leftCol(raw, room) : raw
      const cand = cur ? cur + ' ' + word : word
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

const sg = (text: string, tone?: Tone, bold?: boolean): Seg => ({ text, tone, bold })

/** Pure render: elements only, no $, no state (C1). */
export function WaitingTab(els: Els, view: WaitingView, cols: number) {
  const { Box, Text } = els
  const compact = cols < NARROW
  // the card border (2) and its paddingX (2) eat into the row width
  const avail = Math.max(1, cols - 4)

  const segEls = (segs: Seg[], w: number) =>
    fitSegs(segs, w).map((seg, j) =>
      seg.tone === 'dim' ? (
        <Text key={String(j)} bold={seg.bold} dimColor={true}>{seg.text}</Text>
      ) : seg.tone && seg.tone !== 'text' ? (
        <Text key={String(j)} bold={seg.bold} color={seg.tone}>{seg.text}</Text>
      ) : (
        <Text key={String(j)} bold={seg.bold}>{seg.text}</Text>
      ),
    )

  return (
    <Box flexDirection="column">
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      <Box borderStyle="round" borderColor="warning" flexDirection="column" paddingX={1}>
        <Text>
          {segEls(withRight([sg('! ' + view.header, 'warning', true)], compact ? view.timeCompact : view.time, avail), avail)}
        </Text>
        {wrapProse(view.question, avail, '  ', '  ').map((line, i) => (
          <Text key={String(i)} dimColor={true}>{line}</Text>
        ))}
        {view.options.map((o, i) => {
          const rows = wrapProse(o.explanation, avail, o.recommended ? '  (recommended) ' : '  ', '  ')
          return (
            <Box key={String(i)} flexDirection="column">
              <Text>{segEls([sg(cut(`${o.key} · ${o.label}`, avail), undefined, true)], avail)}</Text>
              {rows.map((line, ri) =>
                o.recommended && ri === 0 ? (
                  <Text key={String(ri)}>
                    {segEls([sg('  (recommended) ', 'success', true), sg(line.slice('  (recommended) '.length), 'dim')], avail)}
                  </Text>
                ) : (
                  <Text key={String(ri)}>{segEls([sg(line, 'dim')], avail)}</Text>
                ),
              )}
            </Box>
          )
        })}
        {view.actions.map((a, i) => (
          <Text key={String(i)}>{segEls([sg(cut('  ' + a, avail), 'dim')], avail)}</Text>
        ))}
      </Box>
      {compact || cols < 50 ? (
        <Text dimColor={true}>{leftCol(view.footerLeft, cols)}</Text>
      ) : (
        <Text dimColor={true}>
          {leftCol(view.footerLeft, Math.max(0, cols - view.footerRight.length)) + view.footerRight}
        </Text>
      )}
    </Box>
  )
}
