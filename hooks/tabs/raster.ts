// The /mk pane's Raster parts (plan dashboard-v2-p0b, V0.3, contract C5′): the
// bars and strips a terminal cell grid draws, built procedurally from the
// palette. `Raster` paints half-height `▀` cells, so a bar is ONE row whose
// foreground is the fill and whose background is the track — the panel behind
// it, with a mut cell marking the floor under the bar (Kit.dc.html).
//
// Text in a Raster is unreadable, so every part that carries words stays
// native; `pickBar` hands the caller the Raster when the element table has one
// and its fallback (a SmoothBar or a glyph Text) when it does not — the
// desktop and mobile surfaces have no Raster.
import type { Elements, RenderElement } from 'claude-code'
import { PALETTE, ROLE, rgbWord } from './palette'
import type { PaletteRole } from './palette'

type Els = Elements[keyof Elements]

/** What a Raster element takes: the grid size and its encoded cells. */
export type RasterSpec = { columns: number; rows: number; cells: string }

/** `▀`, the upper-half block: one row of cells paints a half-height bar. */
const UPPER_HALF = 0x2580

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

/** Little-endian u32 triplets `[codePoint, fg, bg]` → padded standard base64.
 *  Hand-rolled: the plugin's lib has no `btoa` and no `Buffer`. */
export function encodeCells(words: readonly number[]): string {
  const bytes = new Uint8Array(words.length * 4)
  const view = new DataView(bytes.buffer)
  words.forEach((w, i) => view.setUint32(i * 4, w >>> 0, true))
  let out = ''
  for (let i = 0; i < bytes.length; i += 3) {
    const b0 = bytes[i] ?? 0
    const b1 = bytes[i + 1] ?? 0
    const b2 = bytes[i + 2] ?? 0
    out += B64[b0 >> 2]
    out += B64[((b0 & 0x03) << 4) | (b1 >> 4)]
    out += i + 1 < bytes.length ? B64[((b1 & 0x0f) << 2) | (b2 >> 6)] : '='
    out += i + 2 < bytes.length ? B64[b2 & 0x3f] : '='
  }
  return out
}

/** `#rrggbb` → the word a cell carries; the palette's values stay the source. */
function word(hex: string): number {
  return rgbWord(hex)
}

// read at draw time, not load time, so a theme (palette.ts withTheme) recolours the bars too
const PANEL = (): number => word(PALETTE.panel)
const TRACK = (): number => word(PALETTE.track)
const MUT = (): number => word(PALETTE.mut)

/** Clamp to 0..100, non-finite and NaN read as 0. */
function clampPct(value: number): number {
  return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0
}

/** The colour at position `t` (0..1) along `stops`: linear between adjacent
 *  stops, the first/last stop past the ends. Few stops keep the number of
 *  distinct pairs per bar low — the Raster palette snaps past 1024. */
function colorAt(stops: readonly string[], t: number): number {
  const list = stops.length > 0 ? stops : [PALETTE.track]
  if (list.length === 1) return word(list[0] ?? PALETTE.track)
  const x = Math.max(0, Math.min(1, t)) * (list.length - 1)
  const i = Math.min(list.length - 2, Math.floor(x))
  const a = word(list[i] ?? PALETTE.track)
  const b = word(list[i + 1] ?? PALETTE.track)
  const f = x - i
  const mix = (shift: number): number => {
    const lo = (a >> shift) & 0xff
    const hi = (b >> shift) & 0xff
    return Math.round(lo + (hi - lo) * f)
  }
  return (mix(16) << 16) | (mix(8) << 8) | mix(0)
}

// ---- memoization ----
const CACHE_CAP = 64
const cache = new Map<string, RasterSpec>()

function cacheKey(parts: unknown[]): string {
  return JSON.stringify(parts)
}

function frozen(spec: RasterSpec): RasterSpec {
  return Object.freeze({ columns: spec.columns, rows: spec.rows, cells: spec.cells })
}

function memoize(key: string, build: () => RasterSpec): RasterSpec {
  const hit = cache.get(key)
  if (hit) return hit
  if (cache.size >= CACHE_CAP) {
    const first = cache.keys().next().value
    if (first !== undefined) cache.delete(first)
  }
  const spec = frozen(build())
  cache.set(key, spec)
  return spec
}

/** One row of `▀` cells: `pct` of `cols` in the gradient colour of its own
 *  position, the rest on the track; the cell at `floorPct` carries a mut
 *  background — the floor tick under the bar (Kit.dc.html's grad()). */
export function gradientBar(pct: number, cols: number, stops: readonly string[], floorPct?: number): RasterSpec {
  return memoize(cacheKey([pct, cols, [...stops], floorPct]), () => {
    const width = Math.max(1, Math.floor(cols))
    const filled = Math.round((clampPct(pct) / 100) * width)
    const floor = floorPct == null
      ? -1
      : Math.min(width - 1, Math.max(0, Math.floor((clampPct(floorPct) / 100) * width)))
    const words: number[] = []
    for (let i = 0; i < width; i++) {
      const fg = i < filled ? colorAt(stops, width <= 1 ? 0 : i / (width - 1)) : TRACK()
      words.push(UPPER_HALF, fg, i === floor ? MUT() : PANEL())
    }
    return { columns: width, rows: 1, cells: encodeCells(words) }
  })
}

/** Evenly pick `count` of `colors` (both ends kept) when the width cannot hold
 *  every segment: the strip keeps its shape, not its exact item count. */
export function sample<T>(colors: readonly T[], count: number): T[] {
  if (colors.length <= count || count <= 1) return colors.slice(0, Math.max(1, count))
  const out: T[] = []
  for (let k = 0; k < count; k++) {
    const v = colors[Math.round((k * (colors.length - 1)) / (count - 1))]
    if (v !== undefined) out.push(v)
  }
  return out
}

/** `count` coloured segments of `cols` cells with one-cell gaps (Kit.dc.html's
 *  segs()): every segment the same width, the leftover cells widening the
 *  first gaps, so the strip is always exactly `cols` columns. An empty list is
 *  an all-track row — never `cells: ''`, which the engine refuses. */
function strip(colors: readonly string[], cols: number): RasterSpec {
  const width = Math.max(1, Math.floor(cols))
  if (colors.length === 0) return gradientBar(0, width, [])
  // one cell of segment + one cell of gap is the minimum per item
  const fit = Math.max(1, Math.floor((width + 1) / 2))
  const list = sample(colors, Math.min(fit, colors.length))
  const n = list.length
  const seg = Math.max(1, Math.floor((width - (n - 1)) / n))
  const gaps: number[] = []
  for (let g = 0; g < n - 1; g++) gaps.push(1)
  let extra = width - (n * seg + (n - 1))
  let g = 0
  while (extra > 0 && n > 1) {
    gaps[g % (n - 1)] = (gaps[g % (n - 1)] ?? 1) + 1
    extra--
    g++
  }
  const words: number[] = []
  for (let i = 0; i < n; i++) {
    const c = word(list[i] ?? PALETTE.track)
    for (let k = 0; k < seg; k++) words.push(UPPER_HALF, c, PANEL())
    if (i < n - 1) {
      const gap = gaps[i] ?? 1
      for (let k = 0; k < gap; k++) words.push(UPPER_HALF, PANEL(), PANEL())
    }
  }
  return { columns: width, rows: 1, cells: encodeCells(words) }
}

/** A lane's stage strip: `total` segments — done in green, the current one in
 *  blue, the rest on the track — separated by one-cell gaps. */
export function stageBar(done: number, current: number, total: number, cols: number): RasterSpec {
  const n = Math.max(1, Math.floor(total))
  const colors: string[] = []
  for (let i = 0; i < n; i++) colors.push(i < done ? PALETTE.grn : i === current ? PALETTE.blue : PALETTE.track)
  return memoize(cacheKey(['stage', done, current, total, cols, colors]), () => strip(colors, cols))
}

/** One segment per lane, coloured by that lane's role (Lanes tab). */
/** A strip from plain `#rrggbb` colours, one segment per entry (the Plan tab's five status colours). */
export function colorStrip(colors: readonly string[], cols: number): RasterSpec {
  const list = colors.length > 0 ? colors : [PALETTE.track]
  return memoize(cacheKey(['colorStrip', [...colors], cols]), () => strip(list, cols))
}

export function laneStrip(states: readonly PaletteRole[], cols: number): RasterSpec {
  const mapped = states.map(s => ROLE[s]?.fg ?? PALETTE.track)
  return memoize(cacheKey(['laneStrip', [...states], cols]), () => strip(mapped, cols))
}

/** The Raster when the element table has one (the terminal), the fallback
 *  otherwise (every other surface): `key` is what `$.ui.blit` would name. */
export function pickBar(els: Els, key: string, spec: RasterSpec, fallback: RenderElement): RenderElement {
  if (!('Raster' in els)) return fallback
  return els.Raster({ key, columns: spec.columns, rows: spec.rows, cells: spec.cells })
}

/** S2.3: the element table the tabs render with under the `raster` userConfig
 *  option. Only an explicit `false` drops the Raster — anything else (absent,
 *  true) keeps the table whole — so `pickBar` above falls back to its
 *  eighth-block bar exactly as on a surface with no Raster. */
export function withRaster(els: Els, raster: unknown): Els {
  if (raster === false) return { ...els, Raster: undefined } as unknown as Els
  return els
}
