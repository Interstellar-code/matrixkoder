// The /mk pane's visual kit (plan dashboard-v2-p0b, V0.2 + V0.5, contract
// C5′/C7/C9): pure render helpers that take the element table (`els`, as every
// tab gets it today) and, where width matters, `cols`. Nothing here touches $,
// reads state or writes anything: C10 — a control only ever calls the
// `onChange`/`onPress` its caller handed in.
//
// The parts are the canvas boards Kit and Controls in the pane's terminal
// voice: Text/Box with palette fills and round borders for cards, chips and
// headers; native Button for everything pressable.
import type { Elements, RenderChildren, RenderElement } from 'claude-code'
import { PALETTE, ROLE } from './palette'
import type { PaletteRole } from './palette'

type Els = Elements[keyof Elements]

/** ── V0.2 · native parts ─────────────────────────────────────────────── */

/** A filled card: panel background, round border in the role colour (line by
 *  default), one cell of padding on each side. */
export function Card(els: Els, opts: { role?: PaletteRole; edge?: string }, children: RenderChildren): RenderElement {
  const { Box } = els
  const border = opts.role == null ? (opts.edge ?? PALETTE.line) : ROLE[opts.role].fg
  return (
    <Box backgroundColor={PALETTE.panel} borderStyle="round" borderColor={border} paddingX={1}>
      {children}
    </Box>
  )
}

/** A chip: a Text with the role tint behind it (soft) or the role colour
 *  (solid, then the dark ground token under bold text). One cell each side. */
export function Chip(els: Els, opts: { label: string; role: PaletteRole; solid?: boolean }): RenderElement {
  const { Text } = els
  const role = ROLE[opts.role]
  const padded = ` ${opts.label} `
  return opts.solid
    ? <Text backgroundColor={role.fg} color={PALETTE.bg} bold={true}>{padded}</Text>
    : <Text backgroundColor={role.tint} color={role.fg}>{padded}</Text>
}

/** The eighth-block fallback bar (Kit.dc.html's SmoothBar row): ▏▎▍▌▋▊▉█ of
 *  the role colour on a track background, `width` cells wide, never under 1. */
export function SmoothBar(els: Els, opts: { pct: number; width: number; role: PaletteRole }): RenderElement {
  const { Text } = els
  const width = Math.max(1, Math.floor(opts.width))
  const used = Math.max(0, Math.min(100, Number.isFinite(opts.pct) ? opts.pct : 0))
  const units = (used / 100) * width
  const full = Math.floor(units)
  const eighths = Math.min(7, Math.max(0, Math.round((units - full) * 8)))
  const partial = eighths > 0 && full < width ? '▏▎▍▌▋▊▉'[eighths - 1] ?? '' : ''
  const fill = '█'.repeat(Math.max(0, full)) + partial
  return (
    <Text color={ROLE[opts.role].fg} backgroundColor={PALETTE.track}>
      {fill + ' '.repeat(Math.max(0, width - fill.length))}
    </Text>
  )
}

/** A section header row: bold upper-case label on the left, optional dim text
 *  on the right. */
export function Section(els: Els, opts: { label: string; right?: string }): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="row" justifyContent="space-between">
      <Text bold={true} color="subtle">{opts.label.toUpperCase()}</Text>
      {opts.right != null && <Text dimColor={true}>{opts.right}</Text>}
    </Box>
  )
}

/** A row of dim text actions: `o open · n nudge`. */
export function ActionRow(els: Els, opts: { actions: string }): RenderElement {
  const { Text } = els
  return <Text dimColor={true}>{opts.actions}</Text>
}

/** ── V0.5 · controls ─────────────────────────────────────────────────── */

/** The button tones of Controls.dc.html, in the order they appear there. */
export type ActionTone = 'primary' | 'default' | 'warn' | 'danger' | 'quiet' | 'disabled'

/** One pressable action. `glyph` is the hotkey marker the label starts with
 *  (Controls.dc.html: a dim letter before the word); `word` the rest. A glyph
 *  that is one lowercase letter or digit also becomes the engine hotkey, so the
 *  key presses it while the pane holds the focus. `noHotkey` opts out of that
 *  (R3: the Settings tab drops its letter/digit hotkeys while the find field
 *  holds the edit mode, so typing never fires a button). */
export type ActionSpec = {
  key: string
  glyph: string
  word: string
  tone?: ActionTone
  /** the main action of a row: the one ActionButtons keeps when folding */
  primary?: boolean
  /** draws without chrome (the `⋯` fold button); no engine hotkey then */
  plain?: true
  /** draw the chrome but arm no engine hotkey for the glyph */
  noHotkey?: true
  /** a plain button that still arms this engine hotkey (the Segment strips) */
  hotkey?: string
  onPress?: () => void
}

/** The label as drawn: the glyph, a space, the word (a bare glyph alone). */
export function actionLabel(spec: ActionSpec): string {
  return spec.word === '' ? spec.glyph : `${spec.glyph} ${spec.word}`
}

/** A native Button. The tone maps to the label styles a Button carries: the
 *  engine draws `[ label ]` around a non-plain one and a primary in the accent
 *  colour; `dimColor` is the label drawn dim at rest (quiet), full strength
 *  under the pointer or the focus — which is exactly Controls.dc.html's
 *  outline button. A `disabled` tone keeps the same look with a no-op press:
 *  the engine has no inert Button, so the kit makes one.
 *
 *  M3: a Button has no fill, so `warn`/`danger` draw their tone glyph as a
 *  separate leading Text in the role colour (amber / red) beside the Button —
 *  the colour cue Controls.dc.html asks for, with the Button's label its word. */
export function ActionButton(els: Els, spec: ActionSpec): RenderElement {
  const { Box, Button, Text } = els
  const tone = spec.tone ?? 'default'
  const split = tone === 'warn' || tone === 'danger'
  const label = split ? spec.word : actionLabel(spec)
  const variant = tone === 'primary' ? 'primary' : split ? 'secondary' : undefined
  const props: Parameters<typeof Button>[0] = { key: spec.key, label, onPress: () => { if (tone !== 'disabled') spec.onPress?.() } }
  if (!spec.plain && spec.noHotkey !== true && /^[a-z0-9]$/.test(spec.glyph)) props.hotkey = spec.glyph
  if (spec.plain) { props.plain = true; if (spec.hotkey != null) props.hotkey = spec.hotkey }
  else if (variant != null) props.variant = variant
  if (tone === 'quiet' || tone === 'disabled') props.dimColor = true
  const button = Button(props)
  if (!split) return button
  return (
    <Box flexDirection="row" gap={1}>
      <Text color={tone === 'warn' ? ROLE.warn.fg : ROLE.err.fg}>{spec.glyph}</Text>
      {button}
    </Box>
  )
}

/** A boxed strip segment (Settings.dc.html's `.seg button` / `.sub`): a plain
 *  press on a filled cell — the blue tint when `on`, the resting button fill
 *  otherwise (`flat`: no fill, for the scope strip's inactive scopes). Drawn
 *  width = label + 4 (one cell of padding each side + the plain press's 2). */
export function Segment(els: Els, opts: { key: string; label: string; on: boolean; flat?: boolean; hotkey?: string; static?: true; boxed?: true; onPress: () => void }): RenderElement {
  const { Box, Text } = els
  const edge = (inner: RenderElement): RenderElement => opts.boxed === true
    ? <Box key={`${opts.key}-edge`} borderStyle="round" borderColor={opts.on ? ROLE.live.fg : '#5b6776'}>{inner}</Box>
    : inner
  // the picked segment of a strip needs no press: plain bold blue text on the tint (a Button's label colour is the engine's)
  if (opts.on && opts.static === true) {
    return edge(<Text key={`${opts.key}-box`} bold={true} color={ROLE.live.fg} backgroundColor={ROLE.live.tint}>{opts.boxed === true ? `${opts.hotkey != null ? `${opts.hotkey}: ` : ''}${opts.label}` : ` ${opts.hotkey != null ? `${opts.hotkey}: ` : ''}${opts.label} `}</Text>)
  }
  const bg = opts.on ? ROLE.live.tint : opts.flat === true ? undefined : PALETTE.btn
  return edge(
    <Box key={`${opts.key}-box`} backgroundColor={bg} paddingX={opts.boxed === true ? 0 : 1}>
      {ActionButton(els, { key: opts.key, glyph: opts.label, word: '', plain: true, hotkey: opts.hotkey, tone: opts.on ? 'primary' : 'default', onPress: opts.onPress })}
    </Box>,
  )
}

/** The terminal's drawn width of an action — `[ label ]` around a Button (or
 *  `h: label` for a plain one), the label alone for a plain one, plus for a
 *  warn/danger action its leading glyph Text and that gap column. The row
 *  folds on these numbers. */
function actionWidth(spec: ActionSpec): number {
  const tone = spec.tone ?? 'default'
  const split = tone === 'warn' || tone === 'danger'
  const label = split ? spec.word : actionLabel(spec)
  const chrome = spec.plain ? `${spec.glyph}: ${label}`.length : label.length + 4
  return chrome + (split ? spec.glyph.length + 1 : 0)
}

/** A row of ActionButtons that never wraps: when the row does not fit `cols`
 *  the primary (the first marked primary, else the first action) stays and the
 *  rest collapse into one `⋯ More` button. A fold only happens when the caller
 *  passed `onMore` — what a press of `⋯` runs, revealing the folded actions in
 *  the caller's local view state (C10). Without one the row draws every action
 *  rather than a button that can do nothing (review r1 LOW). */
export function ActionButtons(els: Els, buttons: readonly ActionSpec[], cols: number, onMore?: () => void): RenderElement {
  const { Box } = els
  const gap = 1
  const fits = buttons.reduce((n, b) => n + actionWidth(b), 0) + gap * Math.max(0, buttons.length - 1)
  const row: readonly ActionSpec[] = fits <= cols || buttons.length === 0 || onMore == null
    ? buttons
    : (() => {
      const kept = buttons.find(b => b.primary) ?? buttons[0]
      const folded = buttons.filter(b => b !== kept)
      const more: ActionSpec = {
        key: `${kept?.key ?? 'actions'}-more-${folded.length}`,
        glyph: '⋯', word: 'More', tone: 'quiet', plain: true,
        onPress: onMore,
      }
      return kept == null ? [more] : [kept, more]
    })()
  return (
    <Box flexDirection="row" gap={gap}>
      {row.map(b => ActionButton(els, b))}
    </Box>
  )
}

/** The run/lane control state (LaneDetail.dc.html's three states). */
export type ControlState = 'building' | 'paused' | 'stopped'

/** The buttons a control state shows: building = pause + stop, paused =
 *  resume + stop, stopped = restart. Pressing one only calls
 *  `onChange(nextState)` — the caller keeps it in local view state (C10); no
 *  kit control ever writes run state.
 *
 *  M1: `keyPrefix` names the instance's keys (`${keyPrefix}-pause`, …), so a
 *  pane with one ControlRow per lane never draws two elements under one key.
 *  No `onMore` is passed: every control row's actions fit at 36 cols, and a
 *  `⋯` that reveals nothing is a dead button. */
export function ControlRow(els: Els, state: ControlState, cols: number, onChange: (next: ControlState) => void, keyPrefix = 'ctl'): RenderElement {
  const go = (key: string, glyph: string, word: string, tone: ActionTone, next: ControlState): ActionSpec =>
    ({ key: `${keyPrefix}-${key}`, glyph, word, tone, primary: tone === 'primary', onPress: () => onChange(next) })
  const buttons: readonly ActionSpec[] = state === 'building'
    ? [go('pause', '‖', 'pause', 'warn', 'paused'), go('stop', '■', 'stop', 'danger', 'stopped')]
    : state === 'paused'
      ? [go('resume', '▶', 'resume', 'primary', 'building'), go('stop', '■', 'stop', 'danger', 'stopped')]
      : [go('restart', '↻', 'restart', 'primary', 'building')]
  return ActionButtons(els, buttons, cols)
}

/** ── M4 · the tree row V1's Plan tab composes phases and subtasks from ───── */

/** One tree row (Plan tab): a dim prefix (`│ ` per depth level, then `├ ` for a
 *  non-last sibling or `└ ` for the last), the status glyph in its role colour,
 *  the title truncated with `…` to the cells that are left, and an optional
 *  right-side chip. One row, at most `cols` cells wide. */
export function TreeRow(els: Els, opts: {
  depth: number
  last: boolean
  glyph: string
  role: PaletteRole
  title: string
  right?: string
  rightRole?: PaletteRole
}, cols: number): RenderElement {
  const { Box, Text } = els
  const prefix = `${'│ '.repeat(Math.max(0, Math.floor(opts.depth)))}${opts.last ? '└ ' : '├ '}`
  const chipWidth = opts.right == null ? 0 : opts.right.length + 2 // the Chip pads one cell each side
  const gaps = opts.right == null ? 2 : 3
  const budget = Math.max(1, cols - prefix.length - 1 - gaps - chipWidth)
  const title = opts.title.length > budget ? `${opts.title.slice(0, Math.max(1, budget) - 1)}…` : opts.title
  return (
    <Box flexDirection="row" gap={1}>
      <Text dimColor={true}>{prefix}</Text>
      <Text color={ROLE[opts.role].fg}>{opts.glyph}</Text>
      <Text>{title}</Text>
      {opts.right != null && Chip(els, { label: opts.right, role: opts.rightRole ?? opts.role })}
    </Box>
  )
}

/** ── S2.1 · the Settings tab's setting rows ─────────────────────────── */

/** The value shapes a settings row can hold. */
export type SettingType = 'bool' | 'num' | 'seg' | 'select' | 'text'

/** One option of a seg/select field: its value and its one-line note. */
export type SettingOpt = { readonly value: string; readonly note: string }

/** One settings row's data. Everything the row draws comes from here — it
 *  never reads config, state or scripts (C10); every change leaves through
 *  a SettingCtx callback. */
export type SettingField = {
  readonly key: string
  readonly label: string
  readonly type: SettingType
  readonly value: string | number | boolean
  readonly default: string | number | boolean
  readonly min?: number
  readonly max?: number
  readonly unit?: string
  readonly pct?: boolean
  readonly opts?: readonly SettingOpt[]
  readonly help: string
  readonly editable: boolean
  readonly why?: string
}

/** What the tab hands a row: the pane width, the selection / dirty / open
 *  view state, and one callback per interaction. `onPick` is the row-level
 *  press — the kit's `▸`/`▾` button fires it, selecting the row (its help
 *  line); `noHotkey` drops the row's letter hotkey (the `r` reset) while the
 *  tab holds an edit mode. */
export type SettingCtx = {
  readonly cols: number
  readonly selected: boolean
  readonly changed: boolean
  readonly open: boolean
  readonly noHotkey?: boolean
  readonly onPick: () => void
  readonly onToggle: () => void
  readonly onInc: () => void
  readonly onDec: () => void
  readonly onChoose: (value: string) => void
  readonly onToggleOpen: () => void
  readonly onEdit: (value: string) => void
  readonly onReset: () => void
}

/** a bool value as the row words it (the toggle chip, the default line). */
const onOff = (v: boolean): string => (v ? 'on' : 'off')

/** a field's value (or default) as the row draws it: bool → on/off, num →
 *  the number plus its unit, anything else → the string itself. */
const settingText = (field: SettingField, pick: 'value' | 'default'): string => {
  const v = pick === 'value' ? field.value : field.default
  if (typeof v === 'boolean') return onOff(v)
  if (field.type === 'num') return `${v}${field.unit ?? ''}`
  return String(v)
}

/** `…`-truncate to a budget of at least one cell (TreeRow's rule). */
const ellipsize = (s: string, budget: number): string =>
  s.length > budget ? `${s.slice(0, Math.max(0, budget - 1))}…` : s

/** a bracketed Button's drawn width (`[ label ]` — ActionButtons' math). */
const settingBtnW = (label: string): number => label.length + 4

/** a plain Button's drawn width (no brackets; +2 keeps the budget honest). */
const settingPlainW = (label: string): number => label.length + 2

/** The narrowest Input worth drawing beside a text label (the engine lays the
 *  field out; the tab only budgets the label): the label yields past this. */
const TEXT_INPUT_MIN = 12

/** One settings row (plan settings-tab S2.1, Settings.dc.html's rows):
 *  the label left, the control right, one logical row that fits `cols` (46
 *  base, 36 compact — the label yields with `…`); a changed value prefixes
 *  the label with `●` in the warn colour; the selected row adds a second
 *  dim line with the help, `default <X>` and an `r reset` ActionButton.
 *
 *  Variants: bool = a pressable toggle chip `◉ on` / `○ off`; num = a
 *  `− value +` stepper, plus a SmoothBar at value/max when `pct`; seg = one
 *  pressable per option with the current one `●`-marked; select = a closed
 *  `value ▾` pressable that toggles the option list below (current `●`,
 *  every option pressable); text = the native Input (S2.0: present on
 *  terminal + desktop) beside the same `▸` pick. A field whose surface carries
 *  no Input/Select
 *  (mobile) draws the dim value plus an `edit in file` note instead of
 *  crashing; a non-editable row draws the dim value, and its selected
 *  second line is the `why` (or help), not a reset control. Glyphs never
 *  take a pane hotkey — `r` for reset is the one letter allowed (the
 *  Settings design reserves s/x/r/f; scopes hold 1-8), dropped via
 *  `noHotkey` while the tab holds an edit mode. */
export function SettingRow(els: Els, field: SettingField, ctx: SettingCtx): RenderElement {
  const { Box, Text } = els
  const key = `set-${field.key}`
  const val = settingText(field, 'value')

  /** the main row: marker (when changed) + label press + control, the label
   *  budgeted to what is left after the control (the plain press draws
   *  without chrome). */
  const rowOf = (ctrl: RenderElement, ctrlW: number, label: string): RenderElement => {
    const gaps = (ctx.changed ? 2 : 1) + (ctx.selected ? 1 : 0)
    const budget = Math.max(1, ctx.cols - ctrlW - 2 /* the plain label press */ - gaps - (ctx.changed ? 1 : 0) - (ctx.selected ? 1 : 0))
    return (
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        {ctx.selected && <Text color={ROLE.live.fg}>▌</Text>}
        {ctx.changed && <Text color={ROLE.warn.fg}>●</Text>}
        {ActionButton(els, {
          key: `${key}-pick`, glyph: ellipsize(label, budget), word: '',
          plain: true, onPress: ctx.onPick,
        })}
        {ctrl}
      </Box>
    )
  }

  /** the dim `value · edit in file` control of a surface with no field
   *  element (S2.0: mobile carries neither Input nor Select). */
  const fileNote = (): RenderElement => (
    <Box flexDirection="row" gap={1}>
      <Text dimColor={true}>{val}</Text>
      <Text dimColor={true}>edit in file</Text>
    </Box>
  )
  const fileNoteW = val.length + 1 + 'edit in file'.length

  let main: RenderElement
  let openList: RenderElement | false = false
  if (!field.editable) {
    // read-only: the dim value instead of a control; the selected line is
    // the why (or help), not a default + reset
    // a read-only bool draws the artboard's non-pressable on/off chip
    main = typeof field.value === 'boolean'
      ? rowOf(Chip(els, { label: `${field.value ? '◉' : '○'} ${onOff(field.value)}`, role: field.value ? 'live' : 'idle' }), onOff(field.value).length + 4, field.label)
      : rowOf(<Text dimColor={true}>{val}</Text>, val.length, field.label)
  } else if (field.type === 'text') {
    if ('Input' in els) {
      const { Input } = els
      // the label press selects the row (R3 HIGH 1); the Input carries no
      // label of its own, so the name draws once. Carried LOW: the label is
      // capped to leave the minimum Input beside it (plain chrome + gap), so
      // a long label can never push the Input off a 36-col row.
      const labelBudget = Math.max(1, ctx.cols - TEXT_INPUT_MIN - 3)
      const changedShown = ellipsize(ctx.changed ? `● ${field.label}` : field.label, labelBudget)
      main = (
        <Box flexDirection="row" gap={1} justifyContent="space-between" width={ctx.cols}>
          {ActionButton(els, {
            key: `${key}-pick`, glyph: changedShown, word: '',
            plain: true, onPress: ctx.onPick,
          })}
          <Box backgroundColor={PALETTE.field} paddingX={1}>
            <Input
              key={key} value={String(field.value)}
              onInput={v => ctx.onEdit(v)} onSubmit={v => ctx.onEdit(v)}
            />
          </Box>
        </Box>
      )
    } else {
      main = rowOf(fileNote(), fileNoteW, field.label)
    }
  } else if (field.type === 'bool') {
    const on = field.value === true
    main = rowOf(
      Segment(els, { key: `${key}-tog`, label: `${on ? '◉' : '○'} ${onOff(on)}`, on, onPress: ctx.onToggle }),
      onOff(on).length + 6,
      field.label,
    )
  } else if (field.type === 'num') {
    // % bar: wide panes get 10 cells, compact 6
    const barW = ctx.cols >= 42 ? 10 : 6
    const max = field.max ?? 100
    const n = Number(field.value)
    main = rowOf(
      <Box flexDirection="row" gap={1}>
        {field.pct === true && SmoothBar(els, { pct: Number.isFinite(n) && max > 0 ? (n / max) * 100 : 0, width: barW, role: 'live' })}
        <Box flexDirection="row">
          {Segment(els, { key: `${key}-dec`, label: '−', on: false, onPress: ctx.onDec })}
          <Box backgroundColor={PALETTE.field} paddingX={1}><Text>{val}</Text></Box>
          {Segment(els, { key: `${key}-inc`, label: '+', on: false, onPress: ctx.onInc })}
        </Box>
      </Box>,
      val.length + 2 + 10 + (field.pct === true ? 1 + barW : 0), // two boxed steppers (5 each) + the boxed value + the bar's
      field.label,
    )
  } else if (field.type === 'seg') {
    const opts = field.opts ?? []
    // the option words shrink (longest first) until the label keeps a cell —
    // the budget reserves the plain label press and the changed marker (R3)
    let words = opts.map(o => o.value)
    const segW = (ws: readonly string[]): number =>
      ws.reduce((n, w) => n + w.length + 4, 0)
    while (ctx.cols - segW(words) - 2 - (ctx.changed ? 3 : 1) < 1 && words.some(w => w.length > 1)) {
      let longest = 0
      for (let i = 1; i < words.length; i++) {
        if ((words[i] ?? '').length > (words[longest] ?? '').length) longest = i
      }
      words = words.map((w, i) => (i === longest ? w.slice(0, -1) : w))
    }
    main = rowOf(
      <Box flexDirection="row">
        {opts.map((o, i) => Segment(els, {
          key: `${key}-seg-${o.value}`,
          label: words[i] ?? o.value,
          on: o.value === String(field.value),
          static: true,
          onPress: () => ctx.onChoose(o.value),
        }))}
      </Box>,
      segW(words),
      field.label,
    )
  } else {
    // select: S2.0 gates on the surface carrying the element; without it
    // the value is dim text plus the `edit in file` note
    if (!('Select' in els)) {
      main = rowOf(fileNote(), fileNoteW, field.label)
    } else {
      // the label keeps its cells first (it names the row); a long closed
      // value yields — opening the list still shows it whole (R3 MEDIUM 2)
      const fullVal = String(field.value)
      const chromeClosed = 2 /* plain label press */ + 2 /* gaps */ + (ctx.changed ? 2 : 0)
      const valBudget = Math.max(8, ctx.cols - field.label.length - chromeClosed - ' ▾'.length - 2)
      const valShown = fullVal.length + ' ▾'.length + 2 <= ctx.cols - field.label.length - chromeClosed
        ? fullVal
        : ellipsize(fullVal, valBudget)
      main = rowOf(
        <Box backgroundColor={PALETTE.field}>
          {ActionButton(els, { key: `${key}-sel`, glyph: ` ${valShown}`, word: '▾', plain: true, onPress: ctx.onToggleOpen })}
        </Box>,
        settingPlainW(`${valShown} ▾`) + 1,
        field.label,
      )
      if (ctx.open) {
        openList = (
          <Box flexDirection="column">
            {(field.opts ?? []).map(o => {
              const cur = o.value === String(field.value)
              return (
                <Box key={`${key}-opt-${o.value}`} flexDirection="row" gap={1}>
                  <Text color={cur ? ROLE.live.fg : undefined} dimColor={!cur}>{cur ? '●' : '○'}</Text>
                  {ActionButton(els, { key: `${key}-pick-${o.value}`, glyph: o.value, word: '', plain: true, noHotkey: ctx.noHotkey === true ? true : undefined, onPress: () => ctx.onChoose(o.value) })}
                  {o.note !== '' && <Text dimColor={true}>{ellipsize(o.note, Math.max(1, ctx.cols - o.value.length - 7))}</Text>}
                </Box>
              )
            })}
          </Box>
        )
      }
    }
  }

  // the selected row's second line: dim help + `default <X> · r reset`
  // (non-editable: the why/or help alone — nothing to reset). The line owns
  // `cols` minus the reset button and its gap; the default yields first, then
  // the help, so a long default (R3 MEDIUM 2) can never push the line over.
  let helpLine: RenderElement | false = false
  if (ctx.selected) {
    if (!field.editable) {
      helpLine = <Text dimColor={true}>{ellipsize(field.why ?? field.help, ctx.cols)}</Text>
    } else {
      const defText = settingText(field, 'default')
      const resetW = settingBtnW('r reset')
      const room = Math.max(0, ctx.cols - resetW - 1)
      const fixedTail = ' · default '.length + ' · '.length
      const dBudget = Math.max(0, room - fixedTail)
      const defShown = dBudget <= 0 ? '' : ellipsize(defText, dBudget)
      const tail = ` · default ${defShown} · `
      const helpBudget = Math.max(0, room - tail.length)
      const helpShown = helpBudget <= 0 ? '' : ellipsize(field.help, helpBudget)
      const reset = ActionButton(els, { key: `${key}-reset`, glyph: 'r', word: 'reset', tone: 'quiet', noHotkey: ctx.noHotkey === true ? true : undefined, onPress: ctx.onReset })
      helpLine = (
        <Box flexDirection="row" gap={1}>
          <Text dimColor={true}>{helpShown}{tail}</Text>
          {reset}
        </Box>
      )
    }
  }

  return (
    <Box flexDirection="column" backgroundColor={ctx.selected ? PALETTE.sel : undefined}>
      {main}
      {openList}
      {helpLine}
    </Box>
  )
}
