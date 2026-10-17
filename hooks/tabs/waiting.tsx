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
  /** set on a real ask: its bus id, the task it belongs to, and its age */
  id?: string
  task?: string
  age?: string
}

/**
 * One pending ask as `mk-bus.py pending --json` returns it.
 * Keys pinned from the bus message `append_message` builds
 * (`scripts/mk-bus.py` around the `msg = {` in `append_message`) and from
 * `get_pending`, which returns those messages unanswered. `cmd_pending`
 * wraps them as `{"ok": true, "run": …, "pending": […]}`.
 */
export type PendingAsk = {
  id: string
  /** ids are per run (`m-1` repeats), so the card key and the answer hint need it */
  run?: string
  task?: string | null
  text?: string
  ts?: string
  seq?: number
  from?: string
  to?: string
  kind?: string
  reply_to?: string | null
  ack_level?: number
  /** present only when the ask carried a decision; a real bus message has none */
  options?: WaitOption[]
}

/** How many asks one render draws; the rest collapse to a dim "+N more" row. */
export const ASK_CAP = 8

/** What one `mk-bus.py pending` stdout looks like (`cmd_pending`; it prints JSON and takes no `--json`). */
export type PendingPayload = { ok?: boolean; run?: string; pending?: readonly PendingAsk[]; error?: string }

/** Injected process runner, same shape as the Kanban tab's. */
export type RunFn = (
  argv: readonly string[],
  init?: { timeoutMs?: number },
) => Promise<{ stdout?: string; code?: number }>

/** A run snapshot's wait, the bus-down fallback (`types` `MkWaiting`). */
export type RunWait = { question: string; options?: readonly string[]; rec?: string | null; since?: string | null }

/** One `pending --json` read for a dashboard tick. */
export type PendingRead = {
  asks: readonly PendingAsk[]
  /** the bus answered (even with zero asks): real source, never the fixture */
  live: boolean
  /** the bus did not answer: caller falls back to the run snapshot, else the fixture */
  down: boolean
}

/** the fixture with the mock flag (C1/C3). */
export function waitingView(): WaitingView {
  return { mock: true, ...WAITING_FIXTURE }
}

/** Whole minutes between an ask's `ts` and now. Unparseable clocks yield nothing. */
export function askAge(ts: string | undefined, nowIso: string): string {
  if (ts == null || ts === '') return ''
  const a = Date.parse(ts)
  const b = Date.parse(nowIso)
  if (!Number.isFinite(a) || !Number.isFinite(b)) return ''
  const mins = Math.max(0, Math.round((b - a) / 60000))
  if (mins < 60) return `${String(mins)}m`
  const hours = Math.floor(mins / 60)
  return hours < 48 ? `${String(hours)}h` : `${String(Math.floor(hours / 24))}d`
}

/** One real ask as a card. Options keep the decision layout; without them the
 *  card shows the id and the answer command as a dim hint (C10: no script). */
export function askView(ask: PendingAsk, nowIso: string): WaitingView {
  const age = askAge(ask.ts, nowIso)
  const task = ask.task ?? ''
  const options = ask.options ?? []
  return {
    mock: false,
    id: ask.id,
    task,
    age,
    header: task !== '' ? task : ask.id,
    time: age !== '' ? `open ${age}` : '',
    timeCompact: age,
    question: ask.text ?? '',
    options,
    actions: options.length === 0
      ? [`mk-bus.py answer ${ask.id}${ask.run != null && ask.run !== '' && ask.run !== '*' ? ` --run ${ask.run}` : ''} "<text>"`]
      : ['or type "mk: pick A"'],
    footerLeft: ask.id,
    footerRight: age,
  }
}

/** The run snapshot's wait, used only when the bus did not answer. */
export function runWaitView(wait: RunWait): WaitingView {
  const options: WaitOption[] = (wait.options ?? []).map(label => ({
    key: label,
    label,
    action: label,
    explanation: '',
    recommended: wait.rec != null && wait.rec === label,
  }))
  return {
    mock: false,
    header: 'run wait',
    time: wait.since ?? '',
    timeCompact: wait.since ?? '',
    question: wait.question,
    options,
    actions: [],
    footerLeft: 'from the run snapshot',
    footerRight: '',
  }
}

/** Nothing pending from a real source: the empty state, never the fixture. */
export function emptyWaitingView(): WaitingView {
  return {
    mock: false,
    header: '',
    time: '',
    timeCompact: '',
    question: '',
    options: [],
    actions: [],
    footerLeft: '',
    footerRight: '',
  }
}

/**
 * One `mk-bus.py pending` per dashboard tick. The command already prints one
 * JSON line and has no `--json` flag (`scripts/mk-bus.py` p_pend: only `--run`
 * and `--data`). `dataDir` is the active project's data dir, passed as
 * `--data` so the read does not depend on the plugin's cwd. The runner is
 * injected: the plugin test runtime cannot spawn. A missing script, a non-zero
 * exit, non-JSON or `{"ok": false}` is "bus down" (`down: true`) and spawns
 * nothing else — this never starts a bus and never creates a file.
 * `{"ok": true}` with an empty list is a real source (`live: true`, `asks: []`).
 */
export async function readPending(run: RunFn, pluginRoot: string, dataDir?: string | null): Promise<PendingRead> {
  const down: PendingRead = { asks: [], live: false, down: true }
  const argv = ['python3', `${pluginRoot}/scripts/mk-bus.py`, 'pending',
    ...(dataDir != null && dataDir !== '' ? ['--data', dataDir] : [])]
  let result: { stdout?: string; code?: number }
  try {
    result = await run(argv, { timeoutMs: 5_000 })
  } catch {
    return down
  }
  if (result.code !== 0 && result.code != null) return down
  const stdout = result.stdout ?? ''
  // An empty answer is "no bus spoke": the caller keeps whatever it was showing
  // (the fixture before the first real tick). A dead bus exits non-zero or
  // throws, which is the `down` path above.
  if (stdout.trim() === '') return { asks: [], live: false, down: false }
  let parsed: PendingPayload
  try {
    parsed = JSON.parse(stdout) as PendingPayload
  } catch {
    return down
  }
  if (parsed == null || typeof parsed !== 'object' || parsed.ok !== true) return down
  const asks = Array.isArray(parsed.pending) ? parsed.pending.filter(a => typeof a?.id === 'string') : []
  return { asks, live: true, down: false }
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

/** Terminal cells a string takes. Wide: emoji presentation, extended
 *  pictographic with an emoji presentation, and East Asian Width W/F (CJK,
 *  kana, hangul, fullwidth). Zero: ZWJ, variation selectors, combining marks.
 *  The ellipsis is 1: most terminals draw it in one cell. */
const WIDE = /\p{Emoji_Presentation}|\p{Extended_Pictographic}\uFE0F?|[\u1100-\u115F\u2329\u232A\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE10-\uFE19\uFE30-\uFE6F\uFF00-\uFF60\uFFE0-\uFFE6\u{20000}-\u{3FFFD}]/u
const ZERO = /\p{Mn}|\u200D|[\uFE00-\uFE0F]/u

export function cellWidth(text: string): number {
  let n = 0
  for (const ch of text) {
    if (ZERO.test(ch)) continue
    n += WIDE.test(ch) ? 2 : 1
  }
  return n
}

/** cut `text` to `w` terminal cells, marking the cut with `…` — a row never wraps. */
function cut(text: string, w: number): string {
  if (w <= 0) return ''
  if (cellWidth(text) <= w) return text
  let out = ''
  for (const ch of text) {
    if (cellWidth(out + ch) > w - 1) break
    out += ch
  }
  return `${out}…`
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
      const room = Math.max(1, w - cellWidth(prefix))
      const word = cellWidth(raw) > room ? cut(raw, room) : raw
      const cand = cur ? `${cur} ${word}` : word
      if (cur && cellWidth(cand) > room) {
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

/** one ask as a card. A real ask with no options shows its id and the answer
 *  command instead of the decision buttons (C10: the tab calls no script). */
function askCard(els: Els, view: WaitingView, cols: number, ui: TabUi | undefined, key: string): RenderElement {
  const { Box, Text } = els
  const w = Math.max(8, cols - 4) // the Card's border + padding each side (C11)
  const picked = waitingPick(ui, view)
  const rows: RenderElement[] = [headerRow(els, view, cols, w)]
  if (view.id != null && view.id !== '') rows.push(<Text key={`${key}-id`} dimColor={true}>{cut(`${view.id}${view.task != null && view.task !== '' ? ` · ${view.task}` : ''}${view.age != null && view.age !== '' ? ` · ${view.age}` : ''}`, w)}</Text>)
  for (const [i, line] of wrapProse(view.question, w, '  ', '  ').entries()) {
    rows.push(<Text key={`${key}-q-${String(i)}`}>{line}</Text>)
  }
  if (view.options.length > 0) {
    for (const option of view.options) rows.push(...optionRows(els, option, picked === option.key, w))
    rows.push(...actionRows(els, view, ui, w))
  }
  for (const hint of view.actions) rows.push(ActionRow(els, { actions: cut(hint, w) }))
  return Card(els, { role: 'warn' }, <Box key={key} flexDirection="column">{rows}</Box>)
}

/** Pure render: elements only, no $, no state (C1).
 *  One view draws one card (the fixture, the run-snapshot fallback, one ask).
 *  Several real asks stack through `waitingAsks`. */
export function WaitingTab(els: Els, view: WaitingView, cols: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  if (!view.mock && view.question === '' && view.options.length === 0 && (view.id == null || view.id === '')) {
    return (
      <Box flexDirection="column">
        <Text dimColor={true}>nothing waiting</Text>
      </Box>
    )
  }
  return (
    <Box flexDirection="column">
      {view.mock && <Text bold={true} color={ROLE.warn.fg}>MOCK · fixture data · not this run</Text>}
      {askCard(els, view, cols, ui, 'ask')}
      <Text dimColor={true}>{hintRow(view.footerLeft, view.footerRight, cols)}</Text>
    </Box>
  )
}

/** The card's key. Ids repeat across runs, so the run is part of it. */
export function askKey(ask: PendingAsk): string {
  return `${ask.run ?? ''}:${ask.id}`
}

/** Several pending asks, one card each, in the order the bus returned them.
 *  Only the first ASK_CAP are drawn; the rest are a dim "+N more" row. */
export function waitingAsks(els: Els, asks: readonly PendingAsk[], cols: number, nowIso: string, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  if (asks.length === 0) return WaitingTab(els, emptyWaitingView(), cols, ui)
  const shown = asks.slice(0, ASK_CAP)
  const more = asks.length - shown.length
  return (
    <Box flexDirection="column">
      {shown.map(ask => askCard(els, askView(ask, nowIso), cols, ui, askKey(ask)))}
      {more > 0 && <Text dimColor={true}>{cut(`+${String(more)} more`, cols)}</Text>}
      <Text dimColor={true}>{hintRow(`${String(asks.length)} waiting`, '', cols)}</Text>
    </Box>
  )
}