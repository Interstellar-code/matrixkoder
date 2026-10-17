// The /mk pane's palette (plan dashboard-v2-p0b, V0.1 / contract C7): ONE copy
// of the dashboard-v2 mockups' dark tokens and their chip tints. A tab never
// writes a hex literal — every fill, border and chip colour comes from here.
// Every coloured word comes from here too (muted headings, warn/err/ok lines),
// so a second palette (theme) recolours the whole pane; plain body text with
// no colour still follows the user's terminal theme.
//
// Source: dashboard-v2-mockups/p0b/Kit.dc.html (the swatch grid) — 12 dark
// tokens plus the four chip tints, kept verbatim.

/** The 12 dark tokens: ground, panels, borders, the text ramp and the roles. */
export const PALETTE = {
  bg: '#0e1116', // ground
  panel: '#151a21', // card fill
  line: '#2a323d', // border
  edge: '#5b6776', // resting outline of a boxed control (strip segments, Settings fields)
  btn: '#1c232c', // idle chip / resting button fill
  fg: '#e7eaee', // brightest text (ThemeKey text equivalent)
  mut: '#a3adb8', // muted text (ThemeKey subtle equivalent)
  dim: '#8691a0', // dim text (ThemeKey inactive equivalent)
  track: '#2a323d', // the empty part of a bar (same value as line)
  blue: '#7cb3ff', // live / accent
  amb: '#e8b04a', // warn
  grn: '#6fd39a', // ok
  red: '#ff8a6a', // err
  // the four chip tints: the role colour at ~20% over the panel
  // Settings.dc.html's extra fills: the selected row, the picked mode card, a field
  sel: '#1a2230',
  modeOn: '#18202b',
  field: '#0b0e12',
  blueTint: '#243145',
  ambTint: '#3a3120',
  grnTint: '#213830',
  redTint: '#3e2927',
} as const

/** The role names a part asks for; each resolves to a chip fill and a tint. */
export type PaletteRole = 'live' | 'warn' | 'ok' | 'err' | 'idle' | 'accent'

/** Role → { fg, tint }: `fg` paints glyphs/borders, `tint` is the soft chip fill. */
export const ROLE: Record<PaletteRole, { readonly fg: string; readonly tint: string }> = {
  live: { fg: PALETTE.blue, tint: PALETTE.blueTint },
  warn: { fg: PALETTE.amb, tint: PALETTE.ambTint },
  ok: { fg: PALETTE.grn, tint: PALETTE.grnTint },
  err: { fg: PALETTE.red, tint: PALETTE.redTint },
  // idle is the resting chip (btn fill) with muted text — the MOCK marker's tone
  idle: { fg: PALETTE.mut, tint: PALETTE.btn },
  accent: { fg: PALETTE.blue, tint: PALETTE.blueTint },
}

/** `#rrggbb` → `0xrrggbb`, the word a Raster cell carries (raster.ts's input). */
export function rgbWord(hex: string): number {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim())
  return m?.[1] == null ? 0 : parseInt(m[1], 16)
}

/** The Matrix theme: near-black ground, a green text ramp, green borders; warn and err stay
 *  distinct (pale lime, red) so a warning still reads as one. Same keys as PALETTE. */
export const MATRIX: Record<keyof typeof PALETTE, string> = {
  bg: '#020a04',
  panel: '#04140a',
  line: '#0d3b1a',
  edge: '#145c28',
  btn: '#062612',
  fg: '#b6ffc6',
  mut: '#3ddc6a',
  dim: '#1f8a3c',
  track: '#0d3b1a',
  blue: '#00ff41', // live / accent: the bright Matrix green
  amb: '#d4ff7a', // warn: pale lime, still apart from the greens
  grn: '#00c832',
  red: '#ff4d4d',
  sel: '#06301a',
  modeOn: '#05240f',
  field: '#010603',
  blueTint: '#063d16',
  ambTint: '#2a3a0c',
  grnTint: '#04300f',
  redTint: '#3a0d0d',
}

export type ThemeName = 'default' | 'matrix'

/** The theme being drawn right now (kit parts read it for the Matrix lettering). */
export const THEME: { name: ThemeName } = { name: 'default' }

/** Draws `render` under `theme`: swaps PALETTE and ROLE in place for the synchronous render,
 *  then restores them, so one tab can wear a theme while the rest of the pane does not.
 *  shortcut: a whole-pane theme setting would set this once per render instead of per tab. */
export function withTheme<T>(theme: ThemeName, render: () => T): T {
  if (theme === 'default') return render()
  const pal = PALETTE as unknown as Record<string, string>
  const savedPal = { ...pal }
  const savedRole = Object.fromEntries(Object.entries(ROLE).map(([k, v]) => [k, { ...v }]))
  Object.assign(pal, MATRIX)
  const role = ROLE as unknown as Record<string, { fg: string; tint: string }>
  role.live = { fg: pal.blue!, tint: pal.blueTint! }
  role.warn = { fg: pal.amb!, tint: pal.ambTint! }
  role.ok = { fg: pal.grn!, tint: pal.grnTint! }
  role.err = { fg: pal.red!, tint: pal.redTint! }
  role.idle = { fg: pal.mut!, tint: pal.btn! }
  role.accent = { fg: pal.blue!, tint: pal.blueTint! }
  THEME.name = theme
  try {
    return render()
  } finally {
    Object.assign(pal, savedPal)
    Object.assign(role, savedRole)
    THEME.name = 'default'
  }
}

/** Matrix lettering for every Text: no colour → the theme's body green, `dimColor` → the
 *  theme's dim green (a terminal's dim is grey). Colours are read now, inside withTheme, so
 *  the wrapper keeps them whenever the engine calls it. Other element kinds pass through. */
export function themeText<T extends { Text: (props: never) => unknown }>(els: T): T {
  if (THEME.name !== 'matrix') return els
  const fg = PALETTE.fg
  const dim = PALETTE.dim
  const Text = els.Text as unknown as (props: Record<string, unknown>) => unknown
  const wrapped = (props: Record<string, unknown>): unknown => {
    if (props.color != null) return Text(props)
    return Text({ ...props, color: props.dimColor === true ? dim : fg, dimColor: false })
  }
  return { ...els, Text: wrapped } as unknown as T
}
