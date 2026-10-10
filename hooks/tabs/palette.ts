// The /mk pane's palette (plan dashboard-v2-p0b, V0.1 / contract C7): ONE copy
// of the dashboard-v2 mockups' dark tokens and their chip tints. A tab never
// writes a hex literal — every fill, border and chip colour comes from here.
// Body text stays on ThemeKeys (text / subtle / inactive) so it follows the
// user's theme: the palette is for chrome (fills, borders, chips, bars), not
// for prose.
//
// Source: dashboard-v2-mockups/p0b/Kit.dc.html (the swatch grid) — 12 dark
// tokens plus the four chip tints, kept verbatim.

/** The 12 dark tokens: ground, panels, borders, the text ramp and the roles. */
export const PALETTE = {
  bg: '#0e1116', // ground
  panel: '#151a21', // card fill
  line: '#2a323d', // border
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
