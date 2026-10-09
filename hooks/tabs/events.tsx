// The /mk pane's Events tab (plan dashboard-v2-p0b, V1.3, contract C9/C11): the
// Main.dc.html timeline redrawn from the V0 kit — the list inside ONE Card with
// a Section header (EVENTS · last N), each row a dim time, a type Chip in its
// role colour and the text, cut to the card's content width. The render cap is
// unchanged. The C1 names (EventsView, eventsView, EventsTab) stay the contract
// the pane and the tests build against.
import type { Elements } from 'claude-code'
import { NARROW } from './ui'
import { Card, Chip, Section } from './kit'
import { EVENTS_FIXTURE } from './events.fixture'
import type { PaletteRole } from './palette'

type Els = Elements[keyof Elements]

/** one timeline row: clock time, the event-type word, the event text; `short`
 *  is the Narrow artboard's own pair for the row (review r1: compact mode). */
export type EventsView = {
  mock: boolean
  events: readonly { t: string; type: string; text: string; short?: { readonly type: string; readonly text: string } }[]
  earlierCount: number
}

/** the render's row cap: a timeline never draws more than this many events. */
export const EVENTS_MAX = 50

/** the fixture view (C6: Checkout v2, artboard E, newest first). */
export function eventsView(): EventsView {
  return { ...EVENTS_FIXTURE, mock: true }
}

/** a view over caller-supplied rows — how a test feeds the cap a longer list. */
export function eventsViewOf(events: EventsView['events'], earlierCount = 0): EventsView {
  return { mock: true, events, earlierCount }
}

// C2: the timeline's own type colours stay here — the mockup's blue/green/
// amber/neutral chip sets, not the pane's STATUS_COLOR (review is blue on this
// design, yellow there). 'disp' is the Narrow artboard's short type word for
// dispatched; the neutral types take the idle role's chip.
const TYPE_ROLE: Record<string, PaletteRole> = {
  review: 'live', dispatched: 'live', disp: 'live',
  fix: 'ok', merged: 'ok', verified: 'ok',
  blocked: 'warn', quota: 'warn',
  route: 'idle', brief: 'idle', lanes: 'idle', stage: 'idle',
}

// fixed columns: the clock (17:44) is 5 wide; the type word pads to 6 (the
// longest rows carry the chip's own one-cell padding on top of it)
const TIME_W = 5
const TYPE_W = 6

/** `…`-truncate to `w` cells — one cut style across the tabs (each tab keeps
 *  its own copy; ui.ts is T1-frozen). */
const clip = (text: string, w: number): string => (text.length <= w ? text : `${text.slice(0, Math.max(1, w) - 1)}…`)

/** Pure render: elements only, no $, no state (C1). */
export function EventsTab(els: Els, view: EventsView, cols: number) {
  const { Box, Text } = els
  const compact = cols < NARROW
  const W = Math.max(1, cols - 4) // the Card's content width (border + padding per side)
  const rows = view.events.slice(0, EVENTS_MAX)
  const headerSide = `last ${String(rows.length)}`
  return (
    <Box flexDirection="column">
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      {Card(els, {}, (
        <Box flexDirection="column">
          {Section(els, { label: 'Events', right: headerSide })}
          {rows.map((ev, i) => {
            // compact uses the Narrow artboard's own short pair for the row
            const typeWord = compact ? ev.short?.type ?? ev.type : ev.type
            const text = compact ? ev.short?.text ?? ev.text : ev.text
            const role = TYPE_ROLE[typeWord] ?? 'idle'
            const chipW = Math.max(typeWord.length, TYPE_W) + 2 // the Chip pads one cell each side
            const budget = Math.max(0, W - TIME_W - chipW - 2) // the two gap columns
            return (
              <Box key={`ev${String(i)}`} flexDirection="row" gap={1}>
                <Text dimColor={true}>{ev.t.slice(0, TIME_W).padStart(TIME_W)}</Text>
                {Chip(els, { label: typeWord.padEnd(TYPE_W), role })}
                <Text wrap="truncate-end">{clip(text, budget)}</Text>
              </Box>
            )
          })}
        </Box>
      ))}
      {view.earlierCount > 0 && (
        <Text dimColor={true}>{view.earlierCount} earlier events</Text>
      )}
    </Box>
  )
}
