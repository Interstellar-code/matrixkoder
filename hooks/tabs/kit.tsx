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
export function Card(els: Els, opts: { role?: PaletteRole }, children: RenderChildren): RenderElement {
  const { Box } = els
  const border = opts.role == null ? PALETTE.line : ROLE[opts.role].fg
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
 *  key presses it while the pane holds the focus. */
export type ActionSpec = {
  key: string
  glyph: string
  word: string
  tone?: ActionTone
  /** the main action of a row: the one ActionButtons keeps when folding */
  primary?: boolean
  /** draws without chrome (the `⋯` fold button); no engine hotkey then */
  plain?: true
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
  if (!spec.plain && /^[a-z0-9]$/.test(spec.glyph)) props.hotkey = spec.glyph
  if (spec.plain) props.plain = true
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
