// The /mk pane's Events tab (plan dashboard-v2-p0b, Events tab redesign):
// Full visual pass layout matching the HTML design ("Events tab · bus + run, kind filters, pinned asks, lane activity"):
// 1. BUS Card (bus heartbeat, port, uptime, queue stats, files, rate sparkline)
// 2. Filter Card (source, kind multi-select dropdown, view preset dropdown, lane dropdown, search, pause)
// 3. Events Card (time, colored kind chip, who, text, ack level, problem indicator); a pressed row's
//    detail card (`events:detail` names its id) takes the Card's place until it is closed
// 4. LANE ACTIVITY Card (per-lane 24-cell sparkline & silence age)
// Pure render: elements only, no $, no direct state mutation. Filter and selection state flows through TabUi.
import type { Elements, RenderElement } from 'claude-code'
import { NARROW } from './ui'
import { ActionButton, Card, Chip, IconButton, Section, Segment, iconWidth } from './kit'
import type { IconName } from './kit'
import { EventDetailPanel } from './eventdetail'
import { EVENTS_FIXTURE } from './events.fixture'
import type { PaletteRole } from './palette'
import { PALETTE, ROLE, THEME } from './palette'

type Els = Elements[keyof Elements]

/** one timeline event row: clock time, type/kind, who, lane, text, and ack/status markers */
export type EventItem = {
  readonly id?: string
  readonly src?: 'bus' | 'run'
  readonly t: string
  readonly kind?: string
  readonly who?: string
  readonly lane?: string
  readonly text: string
  readonly ack?: number // 0: none, 1: delivered (✓), 2: read (✓✓)
  readonly bad?: boolean
  readonly answered?: boolean
  /** the message id this row replies to (an answer names its ask) */
  readonly reply_to?: string
  /** epoch ms of the row, when known: the Last hour view and Pause read it */
  readonly ts?: number
  /** the row's time as an ISO string: the detail card's full date and time */
  readonly date?: string
  /** the sender id of a bus message */
  readonly from?: string
  /** the receiver id of a bus message */
  readonly to?: string
  /** the plan task the row belongs to */
  readonly task?: string
  /** the store channel the row was written on */
  readonly chan?: string
  /** the author the row's meta names */
  readonly by?: string
  /** the whole text, newlines kept (capped by the store reader): the detail card reads it */
  readonly full?: string
  /** Backwards compatibility for earlier tests */
  readonly type?: string
  readonly short?: { readonly type: string; readonly text: string }
}

export type BusViewData = {
  readonly up: boolean
  readonly lastAge: string
  readonly port: string
  readonly pid: number | null
  readonly runningSince: string
  readonly queue: { readonly pending: number | null; readonly rate: number | null; readonly total: number | null }
  readonly files: { readonly busJson: boolean; readonly tokens: boolean; readonly ringQueue: number }
  readonly rateHistory: readonly { readonly pct: number; readonly color?: string }[]
  readonly rateStart?: string
}

export type LaneActivityItem = {
  readonly lane: string
  readonly cells: readonly string[]
  readonly age: string
  readonly silent?: boolean
}

export type EventsView = {
  readonly mock: boolean
  readonly bus?: BusViewData
  readonly events: readonly EventItem[]
  readonly lanes?: readonly LaneActivityItem[]
  readonly earlierCount: number
}

/** C12: the tab's view-state handle */
export type TabUi = {
  get(key: string): string | undefined
  set(key: string, value: string): void
}

/** the render's row cap: a timeline never draws more than this many events. */
export const EVENTS_MAX = 50

export const BUSK = ['ask', 'answer', 'info', 'ack', 'task', 'status', 'commit', 'report', 'review', 'nudge', 'ring'] as const

export const VIEWS = [
  ['none', 'Any'],
  ['needs', 'Unanswered'],
  ['problems', 'Problems'],
  ['hour', 'Last hour'],
] as const

/** The bus card when bus.json cannot be read: down, every value unknown. */
export const BUS_UNKNOWN: BusViewData = {
  up: false,
  lastAge: '—',
  port: '—',
  pid: null,
  runningSince: '—',
  queue: { pending: null, rate: null, total: null },
  files: { busJson: false, tokens: false, ringQueue: 0 },
  rateHistory: [],
}

export const SHORT: Record<string, string> = {
  answer: 'ans',
  status: 'stat',
  report: 'rep',
  review: 'rev',
  commit: 'commit',
}

/** Type role mapping matching HTML design colors */
export const TYPE_ROLE: Record<string, PaletteRole> = {
  ask: 'warn',
  nudge: 'warn',
  answer: 'ok',
  ans: 'ok',
  commit: 'ok',
  task: 'live',
  status: 'live',
  stat: 'live',
  report: 'live',
  rep: 'live',
  review: 'live',
  rev: 'live',
  ring: 'live',
  info: 'idle',
  ack: 'idle',
  run: 'idle',
  lanes: 'idle',
  stage: 'idle',
  brief: 'idle',
  route: 'idle',
  'ring-failed': 'err',
  fail: 'err',
  blocked: 'warn',
  quota: 'warn',
  stalled: 'warn',
  dispatched: 'live',
  disp: 'live',
  fix: 'ok',
  merged: 'ok',
  verified: 'ok',
}

/** Default fixture view */
export function eventsView(): EventsView {
  return { ...EVENTS_FIXTURE, mock: true }
}

/** A view over caller-supplied rows */
export function eventsViewOf(events: EventsView['events'], earlierCount = 0): EventsView {
  return {
    mock: true,
    bus: EVENTS_FIXTURE.bus,
    lanes: EVENTS_FIXTURE.lanes,
    events,
    earlierCount,
  }
}

/** Fixed column widths */
const TIME_W = 5
const TYPE_W = 6

/** `…`-truncate to `w` cells */
const clip = (text: string, w: number): string => (w <= 0 ? '' : text.length <= w ? text : `${text.slice(0, w - 1)}…`)

/** Check if kind is ring-related */
const isRing = (k: string): boolean => k === 'ring' || k.startsWith('ring-')

type NormalizedEvent = {
  id: string
  src: 'bus' | 'run'
  t: string
  kind: string
  who: string
  lane: string
  text: string
  ack?: number
  bad?: boolean
  answered?: boolean
  reply_to?: string
  ts?: number
  date?: string
  from?: string
  to?: string
  task?: string
  chan?: string
  by?: string
  full?: string
  short?: { readonly type: string; readonly text: string }
}

/** Pure render: elements only, no $, no state */
export function EventsTab(els: Els, view: EventsView, cols: number, ui?: TabUi): RenderElement {
  const { Box, Text, Button } = els
  const compact = cols < NARROW
  const W = Math.max(1, cols - 4) // Card's content width (border + padding per side)

  // Read view-state from TabUi
  const source = ui?.get('events:source') ?? 'all'
  const kindsRaw = ui?.get('events:kinds') ?? ''
  const selectedKinds = new Set(kindsRaw ? kindsRaw.split(',') : [])
  const anyKind = selectedKinds.size > 0
  const viewFilter = ui?.get('events:view') ?? 'none'
  const laneFilter = ui?.get('events:lane') ?? 'all'
  const q = (ui?.get('events:q') ?? '').trim().toLowerCase()
  const paused = ui?.get('events:paused') === '1'
  const pausedAt = Number(ui?.get('events:pausedAt') ?? '') || Infinity
  const openMenu = ui?.get('events:open') ?? ''

  // Normalize events: ensure kind, id, who, lane are populated
  const allEvents: NormalizedEvent[] = view.events.map((e, idx) => ({
    id: e.id ?? `e-${idx}`,
    src: e.src ?? (e.type === 'review' || e.type === 'dispatched' || e.type === 'fix' || e.type === 'stage' || e.type === 'lanes' || e.type === 'brief' ? 'run' : 'bus'),
    t: e.t ?? '12:00',
    kind: e.kind ?? e.type ?? 'info',
    who: e.who ?? (e.src === 'run' ? 'run' : 'orch'),
    lane: e.lane ?? 'all',
    text: e.text ?? '',
    ack: e.ack,
    bad: e.bad ?? (e.kind === 'ring-failed' || e.type === 'ring-failed'),
    answered: e.answered,
    reply_to: e.reply_to,
    ts: e.ts,
    date: e.date,
    from: e.from,
    to: e.to,
    task: e.task,
    chan: e.chan,
    by: e.by,
    full: e.full,
    short: e.short,
  }))

  // asks some answer row replies to
  const answeredSet = new Set(allEvents.filter(e => e.kind === 'answer' && e.reply_to).map(e => e.reply_to))
  const hourAgo = Date.now() - 3_600_000
  const inView: Record<string, (e: NormalizedEvent) => boolean> = {
    needs: e => e.kind === 'ask' && !e.answered && !answeredSet.has(e.id),
    problems: e => !!e.bad || isRing(e.kind) && e.kind !== 'ring' || e.kind === 'blocked',
    hour: e => e.ts != null && e.ts >= hourAgo,
  }
  // events that arrived after Pause: held back from the list until Live
  const isNew = (e: NormalizedEvent): boolean => paused && e.ts != null && e.ts > pausedAt
  const newCount = allEvents.filter(isNew).length

  // Filter list
  const filtered = allEvents.filter(e => {
    if (isNew(e)) return false
    if (source !== 'all' && e.src !== source) return false
    if (source !== 'run' && anyKind && e.src === 'bus') {
      const k = isRing(e.kind) ? 'ring' : e.kind
      if (!selectedKinds.has(k)) return false
    }
    if (inView[viewFilter] && !inView[viewFilter]!(e)) return false
    if (laneFilter !== 'all' && e.lane !== laneFilter) return false
    if (q) {
      const haystack = `${e.text} ${e.id} ${e.who} ${e.kind}`.toLowerCase()
      if (!haystack.includes(q)) return false
    }
    return true
  })

  const rows = filtered.slice(0, EVENTS_MAX)
  const earlierCount = view.earlierCount

  // the open row's detail card: `events:detail` holds its id; a row the view does not carry draws the list.
  // The view's events, not the filtered rows: a filter or new traffic does not close an open card.
  const detailId = ui?.get('events:detail') ?? ''
  const detail = detailId !== '' ? allEvents.find(e => e.id === detailId) : undefined
  // an answer's ask, or an ask's answer, when the view carries it
  const related = detail == null ? undefined
    : detail.kind === 'answer' && detail.reply_to ? allEvents.find(e => e.id === detail.reply_to)
      : detail.kind === 'ask' ? allEvents.find(e => e.kind === 'answer' && e.reply_to === detail.id)
        : undefined
  /** a row's text as the press that opens (or, pressed again, closes) its detail card */
  const rowButton = (ev: NormalizedEvent, i: number, label: string): RenderElement => ActionButton(els, {
    key: `ev-open-${ev.id || i}`,
    glyph: label === '' ? '·' : label,
    word: '',
    plain: true,
    onPress: () => { if (ui != null) ui.set('events:detail', ui.get('events:detail') === ev.id ? '' : ev.id) },
  })

  // Lanes list for filter
  const laneNames: string[] = ['all', ...Array.from(new Set(allEvents.map(e => e.lane).filter((l): l is string => typeof l === 'string' && l !== '' && l !== 'all')))]
  if (laneNames.length === 1 && view.lanes) {
    for (const l of view.lanes) {
      if (!laneNames.includes(l.lane)) laneNames.push(l.lane)
    }
  }

  // Kind count helper
  const countKind = (k: string) => allEvents.filter(e => e.src === 'bus' && (k === 'ring' ? isRing(e.kind) : e.kind === k)).length

  // Kind label
  const activeKindsList = Array.from(selectedKinds)
  const kindLabel = activeKindsList.length === 0
    ? 'all kinds'
    : activeKindsList.length <= 3
      ? activeKindsList.map(k => SHORT[k] ?? k).join(' · ')
      : `${activeKindsList.length} kinds`

  // View label
  const viewOption = VIEWS.find(([k]) => k === viewFilter)
  const viewLabel = viewOption ? viewOption[1] : 'Any'

  const bus = view.bus ?? BUS_UNKNOWN
  const busUp = bus.up
  const labelW = compact ? 4 : 7
  const num = (n: number | null): string => (n == null ? '—' : String(n))
  const mark = (ok: boolean): string => (ok ? '✓' : '✗')
  const filesOk = bus.files.busJson && bus.files.tokens
  const filesLong = `bus.json ${mark(bus.files.busJson)} · tokens ${mark(bus.files.tokens)} · ring-queue ${bus.files.ringQueue}`
  const filesText = 'files'.length + 1 + filesLong.length <= W
    ? filesLong
    : `bus ${mark(bus.files.busJson)} tok ${mark(bus.files.tokens)} rq ${bus.files.ringQueue}`
  const pauseIcon: IconName = paused ? 'play' : 'pause'
  // the count line; the ticks legend joins it only when the whole of it fits beside the state word (never cut mid-word)
  const countBudget = W - (paused ? 'paused' : 'live').length - 1
  const countBare = compact ? `${rows.length}/${allEvents.length} shown` : `${rows.length} of ${allEvents.length} shown`
  const countFull = `${countBare} · ✓ delivered · ✓✓ read`
  const countText = !compact && countFull.length <= countBudget ? countFull : clip(countBare, countBudget)

  return (
    <Box flexDirection="column" gap={1}>
      {view.mock && <Text bold={true} color={ROLE.warn.fg}>MOCK · fixture data · not this run</Text>}

      {/* ── CARD 1: BUS ── */}
      {Card(els, {}, (
        <Box flexDirection="column">
          <Box flexDirection="row" justifyContent="space-between">
            <Text bold={true} color={PALETTE.mut}>{`${THEME.name === 'matrix' ? '> ' : ''}BUS`}</Text>
            <Text color={busUp ? ROLE.live.fg : ROLE.err.fg}>
              {`${busUp ? '● up' : '○ down'} · ${compact ? bus.lastAge : `heartbeat ${bus.lastAge}`}`}
            </Text>
          </Box>
          <Box flexDirection="row" justifyContent="space-between">
            <Text dimColor={true}>port</Text>
            <Text>{compact ? bus.port : `${bus.port} · pid ${bus.pid ?? '—'}`}</Text>
          </Box>
          <Box flexDirection="row" justifyContent="space-between">
            <Text dimColor={true}>running since</Text>
            <Text>{compact ? (bus.runningSince.split('·')[0]?.trim() ?? bus.runningSince) : bus.runningSince}</Text>
          </Box>
          <Box flexDirection="row" justifyContent="space-between">
            <Text dimColor={true}>queue</Text>
            <Text>{compact ? `${num(bus.queue.pending)} · ${num(bus.queue.rate)}/m · ${num(bus.queue.total)}` : `${num(bus.queue.pending)} · ${num(bus.queue.rate)} msg/min · ${num(bus.queue.total)} total`}</Text>
          </Box>
          <Box flexDirection="row" justifyContent="space-between">
            <Text dimColor={true}>files</Text>
            <Text color={filesOk ? ROLE.live.fg : ROLE.warn.fg}>{filesText}</Text>
          </Box>

          {/* Rate Sparkline: only when the bus reports a history */}
          {bus.rateHistory.length > 0 && (
            <Box flexDirection="row" gap={0}>
              {bus.rateHistory.slice(0, Math.min(bus.rateHistory.length, W)).map((c, i) => (
                <Text key={`rate-cell-${i}`} color={c.color ?? ROLE.live.fg}>■</Text>
              ))}
            </Box>
          )}
          {bus.rateHistory.length > 0 && (
            <Box flexDirection="row" justifyContent="space-between">
              <Text dimColor={true}>{compact ? 'up' : `up since ${bus.rateStart ?? '—'}`}</Text>
              {!compact && <Text dimColor={true}>msgs per minute</Text>}
              <Text dimColor={true}>now</Text>
            </Box>
          )}
        </Box>
      ))}

      {/* ── CARD 2: FILTERS ── */}
      {Card(els, {}, (
        <Box flexDirection="column" gap={1}>
          {/* Source Filter */}
          <Box flexDirection="row" gap={1} alignItems="center">
            <Text dimColor={true}>{(compact ? 'src' : 'source').padEnd(labelW)}</Text>
            {Segment(els, {
              key: 'ev-src-all',
              label: 'All',
              on: source === 'all',
              onPress: () => ui?.set('events:source', 'all'),
            })}
            {Segment(els, {
              key: 'ev-src-bus',
              label: 'Bus',
              on: source === 'bus',
              onPress: () => ui?.set('events:source', 'bus'),
            })}
            {Segment(els, {
              key: 'ev-src-run',
              label: 'Run',
              on: source === 'run',
              onPress: () => ui?.set('events:source', 'run'),
            })}
          </Box>

          {/* Kind Dropdown (shown when source !== 'run') */}
          {source !== 'run' && (
            <Box flexDirection="column">
              <Box flexDirection="row" gap={1} alignItems="center">
                <Text dimColor={true}>{'kind'.padEnd(labelW)}</Text>
                <Box flexGrow={1}>
                  <Button
                    key="ev-kind-dd"
                    label={`${clip(kindLabel, compact ? 18 : 26)} ▾`}
                    onPress={() => ui?.set('events:open', openMenu === 'kind' ? '' : 'kind')}
                  />
                </Box>
              </Box>
              {openMenu === 'kind' && (
                <Box flexDirection="column" paddingX={1} backgroundColor={PALETTE.field}>
                  {BUSK.map(k => {
                    const isChecked = selectedKinds.has(k)
                    const count = countKind(k)
                    return (
                      <Box key={`kind-opt-${k}`} flexDirection="row" justifyContent="space-between">
                        <Button
                          key={`kind-btn-${k}`}
                          plain={true}
                          label={`${isChecked ? '[✓]' : '[ ]'} ${k}`}
                          onPress={() => {
                            const next = new Set(selectedKinds)
                            if (next.has(k)) next.delete(k)
                            else next.add(k)
                            ui?.set('events:kinds', Array.from(next).join(','))
                          }}
                        />
                        <Text dimColor={true}>{String(count)}</Text>
                      </Box>
                    )
                  })}
                  <Box flexDirection="row" justifyContent="space-between">
                    <Button
                      key="kind-clear"
                      plain={true}
                      label="c clear"
                      onPress={() => ui?.set('events:kinds', '')}
                    />
                    {/* the menu's width (W less paddingX) less the plain `c clear` button and a gap */}
                    <Text dimColor={true}>{clip('space toggle · esc close', W - 2 - 'c clear'.length - 1)}</Text>
                  </Box>
                </Box>
              )}
            </Box>
          )}

          {/* View Dropdown */}
          <Box flexDirection="column">
            <Box flexDirection="row" gap={1} alignItems="center">
              <Text dimColor={true}>{'view'.padEnd(labelW)}</Text>
              <Box flexGrow={1}>
                <Button
                  key="ev-view-dd"
                  label={`${clip(viewLabel, compact ? 18 : 26)} ▾`}
                  onPress={() => ui?.set('events:open', openMenu === 'view' ? '' : 'view')}
                />
              </Box>
            </Box>
            {openMenu === 'view' && (
              <Box flexDirection="column" paddingX={1} backgroundColor={PALETTE.field}>
                {VIEWS.map(([k, name]) => {
                  const test = inView[k]
                  const count = test ? String(allEvents.filter(test).length) : ''
                  return (
                  <Box key={`view-opt-${k}`} flexDirection="row" justifyContent="space-between">
                    <Button
                      key={`view-btn-${k}`}
                      plain={true}
                      label={`${viewFilter === k ? '(●)' : '( )'} ${name}`}
                      onPress={() => {
                        ui?.set('events:view', k)
                        ui?.set('events:open', '')
                      }}
                    />
                    {count !== '' && <Text dimColor={true}>{count}</Text>}
                  </Box>
                  )
                })}
              </Box>
            )}
          </Box>

          {/* Lane Dropdown */}
          <Box flexDirection="column">
            <Box flexDirection="row" gap={1} alignItems="center">
              <Text dimColor={true}>{'lane'.padEnd(labelW)}</Text>
              <Box flexGrow={1}>
                <Button
                  key="ev-lane-dd"
                  label={`${clip(laneFilter === 'all' ? 'all lanes' : laneFilter, compact ? 18 : 26)} ▾`}
                  onPress={() => ui?.set('events:open', openMenu === 'lane' ? '' : 'lane')}
                />
              </Box>
            </Box>
            {openMenu === 'lane' && (
              <Box flexDirection="column" paddingX={1} backgroundColor={PALETTE.field}>
                {laneNames.map(n => (
                  <Box key={`lane-opt-${n}`} flexDirection="row" justifyContent="space-between">
                    <Button
                      key={`lane-btn-${n}`}
                      plain={true}
                      label={`${laneFilter === n ? '(●)' : '( )'} ${n === 'all' ? 'all lanes' : n}`}
                      onPress={() => {
                        ui?.set('events:lane', n)
                        ui?.set('events:open', '')
                      }}
                    />
                    <Text dimColor={true}>{String(n === 'all' ? allEvents.length : allEvents.filter(e => e.lane === n).length)}</Text>
                  </Box>
                ))}
              </Box>
            )}
          </Box>

          {/* Search Input & Pause Button */}
          <Box flexDirection="row" gap={1} alignItems="center" justifyContent="space-between">
            <Text dimColor={true}>
              {clip(q ? `/ ${q}` : '/ : search text, id, task', Math.min(compact ? 20 : 28, W - iconWidth(pauseIcon, true) - 1))}
            </Text>
            {IconButton(els, {
              key: 'ev-pause-btn',
              icon: pauseIcon,
              labelled: true,
              onPress: () => {
                if (!paused) ui?.set('events:pausedAt', String(Date.now()))
                ui?.set('events:paused', paused ? '0' : '1')
              },
            })}
          </Box>
        </Box>
      ))}

      {/* ── CARD 3: EVENTS LIST (a pressed row's detail card takes its place) ── */}
      {detail != null && EventDetailPanel(els, detail, related, cols, () => { ui?.set('events:detail', '') })}
      {detail == null && Card(els, {}, (
        <Box flexDirection="column">
          {/* Header */}
          {Section(els, { label: 'Events', right: paused ? `paused · ${newCount} new` : `last ${String(rows.length)}` })}

          <Box flexDirection="row" justifyContent="space-between">
            <Text dimColor={true}>
              {countText}
            </Text>
            <Text color={paused ? ROLE.warn.fg : ROLE.live.fg}>
              {paused ? 'paused' : 'live'}
            </Text>
          </Box>

          {/* Event Rows */}
          {rows.length === 0 && <Text dimColor={true}>no event matches the filters</Text>}
          {rows.map((ev, i) => {
            const typeWord = compact ? ev.short?.type ?? ev.kind : ev.kind
            const role = TYPE_ROLE[typeWord] ?? (ev.bad ? 'err' : 'idle')
            const text = compact ? ev.short?.text ?? ev.text : ev.text

            if (compact) {
              const chipW = Math.max(typeWord.length, TYPE_W) + 2
              const budget = Math.max(0, W - TIME_W - chipW - 2)
              return (
                <Box key={`ev-row-${ev.id || i}`} flexDirection="row" gap={1}>
                  <Text dimColor={true}>{ev.t.slice(0, TIME_W).padStart(TIME_W)}</Text>
                  {Chip(els, { label: typeWord.padEnd(TYPE_W), role })}
                  {rowButton(ev, i, clip(text, budget))}
                </Box>
              )
            }

            const isBus = ev.src === 'bus'
            const ackStr = isBus ? (ev.ack === 2 ? '✓✓' : ev.ack === 1 ? '✓' : '·') : ''
            const ackColor = ev.ack === 2 ? ROLE.live.fg : PALETTE.dim
            const showWho = isBus && ev.who !== 'run' && ev.who !== ''
            const whoStr = showWho ? clip(ev.who, Math.floor(W / 3)) : ''
            const hasMarker = ev.bad

            // Layout: [marker 1 gap 1] + time 5 + gap 1 + chip (TYPE_W + 2) + [who + gap 1] + text + [gap 1 + ack]
            const chipW = Math.max(typeWord.length, TYPE_W) + 2
            let prefixW = TIME_W + 1 + chipW + 1
            if (hasMarker) prefixW += 1 + 1
            if (showWho) prefixW += whoStr.length + 1
            const suffixW = ackStr ? 1 + ackStr.length : 0
            const textBudget = Math.max(0, W - prefixW - suffixW)

            const displayText = clip(text, textBudget)

            return (
              <Box key={`ev-row-${ev.id || i}`} flexDirection="row" gap={1} alignItems="center">
                {hasMarker && <Text color={ROLE.err.fg}>!</Text>}
                <Text dimColor={true}>{ev.t.slice(0, TIME_W).padStart(TIME_W)}</Text>
                {Chip(els, { label: typeWord.padEnd(TYPE_W), role })}
                {showWho && <Text dimColor={true}>{whoStr}</Text>}
                {rowButton(ev, i, displayText)}
                {ackStr !== '' && <Text color={ackColor}>{ackStr}</Text>}
              </Box>
            )
          })}

          {/* Earlier Events */}
          {earlierCount > 0 && (
            <Text dimColor={true}>{`${earlierCount} earlier events`}</Text>
          )}
        </Box>
      ))}

      {/* ── CARD 4: LANE ACTIVITY ── */}
      {view.lanes && view.lanes.length > 0 && (
        Card(els, {}, (
          <Box flexDirection="column">
            <Box flexDirection="row" justifyContent="space-between">
              <Text bold={true} color={PALETTE.mut}>{`${THEME.name === 'matrix' ? '> ' : ''}LANE ACTIVITY`}</Text>
              <Text dimColor={true}>last 2h</Text>
            </Box>

            {view.lanes.map(l => {
              const laneW = compact ? 4 : 8
              const ageW = compact ? 8 : 12
              const laneLabel = clip(l.lane, laneW).padEnd(laneW)
              const ageColor = l.silent ? ROLE.warn.fg : PALETTE.dim
              const sparkW = Math.max(0, W - laneW - ageW - 2)
              const activeCells = sparkW > 0 ? l.cells.slice(-sparkW) : [] // oldest left, newest right: a narrow card keeps the newest

              return (
                <Box key={`lane-act-${l.lane}`} flexDirection="row" justifyContent="space-between" alignItems="center">
                  <Text>{laneLabel}</Text>
                  <Box flexDirection="row" gap={0}>
                    {activeCells.map((col, idx) => (
                      <Text key={`c-${idx}`} color={col}>■</Text>
                    ))}
                  </Box>
                  <Text color={ageColor}>{clip(l.age, ageW).padStart(ageW)}</Text>
                </Box>
              )
            })}
          </Box>
        ))
      )}

      {/* Footer shortcut hints */}
      <Box flexDirection="row" justifyContent="space-between">
        <Text dimColor={true}>↵ open</Text>
        <Text dimColor={true}>{cols < 56 ? 'j k' : 'j k /'}</Text>
      </Box>
    </Box>
  )
}
