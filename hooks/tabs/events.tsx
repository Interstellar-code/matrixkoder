// The /mk pane's Events tab (dashboard-v2-p0, T1.3): artboard E's Timeline
// card over its fixture — the MOCK row, a header, one row per event (time,
// type, text) and the earlier-count footer as the last row (C5). Pure render:
// elements only, no $, no state; the C1 names (EventsView, eventsView,
// EventsTab) stay the contract the pane and the tests build against.
import type { Elements } from 'claude-code'
import { NARROW, leftCol, rightCol } from './ui'
import { EVENTS_FIXTURE } from './events.fixture'

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
// amber/dim sets, not the pane's STATUS_COLOR (review is blue on this design,
// yellow there), and the dim types carry dimColor, no colour at all. 'disp'
// is the Narrow artboard's short type word for dispatched
const TYPE_COLOR: Record<string, string | undefined> = {
  review: 'blue', dispatched: 'blue', disp: 'blue',
  fix: 'green', merged: 'green', verified: 'green',
  blocked: 'warning', quota: 'warning',
  route: undefined, brief: undefined, lanes: undefined, stage: undefined,
}

// fixed columns: the clock (17:44) is 5 wide, the type word 10 (the longest,
// "dispatched"), so every row's text starts at the same column
const TIME_W = 5
const TYPE_W = 10

/** `…`-truncate to `w` cells — one cut style across the tabs (each tab keeps
 *  its own copy; ui.ts is T1-frozen). */
const clip = (text: string, w: number): string => (text.length <= w ? text : `${text.slice(0, Math.max(1, w) - 1)}…`)

/** Pure render: elements only, no $, no state (C1). */
export function EventsTab(els: Els, view: EventsView, cols: number) {
  const { Box, Text } = els
  const compact = cols < NARROW
  const textW = Math.max(0, cols - TIME_W - TYPE_W - 2) // the two gap columns
  // the timeline's connector line and dots are CSS art (C5): they translate to
  // nothing — the columns and the type colours carry the design unfaked
  const headerSide = compact ? 'events.jsonl · newest first' : 'events.jsonl · W2 · newest first'
  return (
    <Box flexDirection="column">
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      <Box flexDirection="row" gap={1}>
        <Text bold={true}>Timeline</Text>
        <Text dimColor={true}>{clip(headerSide, Math.max(0, cols - 'Timeline'.length - 1))}</Text>
      </Box>
      {view.events.slice(0, EVENTS_MAX).map((ev, i) => {
        // compact uses the Narrow artboard's own short pair for the row
        const typeWord = compact ? ev.short?.type ?? ev.type : ev.type
        const text = compact ? ev.short?.text ?? ev.text : ev.text
        const tone = TYPE_COLOR[typeWord]
        return (
          <Box key={`ev${String(i)}`} flexDirection="row" gap={1}>
            <Text dimColor={true}>{rightCol(ev.t, TIME_W)}</Text>
            <Text color={tone} dimColor={tone == null}>{leftCol(typeWord, TYPE_W)}</Text>
            <Text wrap="truncate-end">{clip(text, textW)}</Text>
          </Box>
        )
      })}
      {view.earlierCount > 0 && (
        <Text dimColor={true}>{view.earlierCount} earlier events</Text>
      )}
    </Box>
  )
}
