// Raster part tests (plan dashboard-v2-p0b, V0.3): encodeCells is decoded back
// to words by an INDEPENDENT base64 decoder written here, so a wrong encoding
// fails loudly. Every builder is pinned at 36, 40, 46 and 56 columns for its
// column count, the `▀` code point and the exact fg/bg colours of the first
// filled, last filled, first empty and floor cells.
import { test, expect } from 'claude-code/testing'
import type { Elements, RenderElement } from 'claude-code'
import { encodeCells, gradientBar, laneStrip, pickBar, stageBar } from './raster'
import type { RasterSpec } from './raster'
import { PALETTE, ROLE, rgbWord } from './palette'

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

/** base64 (padding stripped) → the LE u32 words encodeCells packed. */
function decode(cells: string): number[] {
  const clean = cells.replace(/=+$/, '')
  const bytes: number[] = []
  for (let i = 0; i < clean.length; i += 4) {
    const q = [0, 1, 2, 3].map(k => B64.indexOf(clean[i + k] ?? ''))
    const n = ((q[0] ?? 0) << 18) | ((q[1] ?? 0) << 12) | ((q[2] ?? 0) << 6) | (q[3] ?? 0)
    bytes.push((n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff)
  }
  const out: number[] = []
  for (let i = 0; i + 3 < bytes.length; i += 4) {
    out.push((bytes[i] ?? 0) | ((bytes[i + 1] ?? 0) << 8) | ((bytes[i + 2] ?? 0) << 16) | ((bytes[i + 3] ?? 0) << 24))
  }
  return out
}

/** the cell triplets of a spec: [codePoint, fg, bg] per column, one row. */
function cells(spec: RasterSpec): { cp: number; fg: number; bg: number }[] {
  const w = decode(spec.cells)
  const out: { cp: number; fg: number; bg: number }[] = []
  for (let i = 0; i < spec.columns; i++) {
    out.push({ cp: w[i * 3] ?? -1, fg: w[i * 3 + 1] ?? -1, bg: w[i * 3 + 2] ?? -1 })
  }
  return out
}

const WIDTHS = [36, 40, 46, 56] as const
const HEAT: readonly string[] = [PALETTE.grn, PALETTE.amb, PALETTE.red] // the mockup's gradient stops

// ---------- encodeCells ----------

test('encodeCells packs LE u32 triplets as padded base64 and round-trips', () => {
  const words = [0x2588, 0xff8800, 0x01000000, 0x2580, 0x000000, 0xffffff]
  const encoded = encodeCells(words)
  expect(decode(encoded)).toEqual(words)
  expect(encoded.length % 4).toBe(0) // padded
  expect(encoded).toMatch(/^[A-Za-z0-9+/]+={0,2}$/)

  expect(encodeCells([])).toBe('')
  // a single cell: the doc's own example — █ orange on the default background
  expect(decode(encodeCells([0x2588, 0xff8800, 0x01000000]))).toEqual([0x2588, 0xff8800, 0x01000000])
})

// ---------- gradientBar ----------

test('gradientBar is one ▀ row: filled cells take the gradient, empty the track, the floor cell is mut', () => {
  const cols = 40
  const pct = 50 // 20 filled cells
  const spec = gradientBar(pct, cols, HEAT, 80)
  expect(spec.rows).toBe(1)
  expect(spec.columns).toBe(cols)
  const c = cells(spec)
  expect(c).toHaveLength(cols)
  for (const cell of c) expect(cell.cp).toBe(0x2580)

  const filled = Math.round((pct / 100) * cols) // 20
  // first filled: the first stop, exactly
  expect(c[0]?.fg).toBe(rgbWord(PALETTE.grn))
  expect(c[0]?.bg).toBe(rgbWord(PALETTE.panel))
  // last filled: a mix along the stops — greener than the mid stop, brighter than the first
  const last = c[filled - 1]?.fg ?? -1
  expect(last).toBeGreaterThan(rgbWord(PALETTE.grn))
  expect(last).not.toBe(rgbWord(PALETTE.amb))
  // first empty: the track
  expect(c[filled]?.fg).toBe(rgbWord(PALETTE.track))
  expect(c[filled]?.bg).toBe(rgbWord(PALETTE.panel))
  // the floor tick: the cell at 80% carries the mut background
  const floor = Math.floor((80 / 100) * cols)
  expect(c[floor]?.bg).toBe(rgbWord(PALETTE.mut))
  expect(c[floor - 1]?.bg).toBe(rgbWord(PALETTE.panel))
  expect(c[floor + 1]?.bg).toBe(rgbWord(PALETTE.panel))
})

test('gradientBar interpolates between its stops by column position', () => {
  const c = cells(gradientBar(100, 3, ['#000000', '#ffffff']))
  expect(c.map(x => x.fg)).toEqual([0x000000, 0x808080, 0xffffff]) // 0, mid grey, full
  expect(c.every(x => x.bg === rgbWord(PALETTE.panel))).toBe(true)
})

test('gradientBar without a floor paints no mut cell and clamps pct', () => {
  const none = cells(gradientBar(40, 12, HEAT))
  expect(none.every(x => x.bg === rgbWord(PALETTE.panel))).toBe(true)
  expect(cells(gradientBar(160, 12, HEAT)).filter(x => x.fg !== rgbWord(PALETTE.track))).toHaveLength(12)
  expect(cells(gradientBar(-20, 12, HEAT)).filter(x => x.fg !== rgbWord(PALETTE.track))).toHaveLength(0)
  expect(cells(gradientBar(Number.NaN, 12, HEAT)).filter(x => x.fg !== rgbWord(PALETTE.track))).toHaveLength(0)
})

test('every builder returns exactly `cols` columns at 36, 40, 46 and 56', () => {
  for (const w of WIDTHS) {
    expect(gradientBar(72, w, HEAT, 80).columns, `gradientBar ${w}`).toBe(w)
    expect(stageBar(5, 6, 12, w).columns, `stageBar ${w}`).toBe(w)
    expect(laneStrip(['live', 'live', 'warn', 'warn'], w).columns, `laneStrip ${w}`).toBe(w)
  }
})

// ---------- stageBar ----------

test('stageBar draws one segment per stage with gaps, done green / current blue / rest track', () => {
  const cols = 36
  const spec = stageBar(5, 5, 12, cols) // 5 done, the 6th segment in progress (Main.dc.html's stage 6/12)
  expect(spec.columns).toBe(cols)
  const c = cells(spec)
  // 12 segments, 11 gaps; segments are equal width, gaps hold the leftover
  const runs: { fg: number; n: number }[] = []
  for (const cell of c) {
    const last = runs.at(-1)
    if (last != null && last.fg === cell.fg) last.n++
    else runs.push({ fg: cell.fg, n: 1 })
  }
  const segments = runs.filter(r => r.fg !== rgbWord(PALETTE.panel))
  expect(segments).toHaveLength(12)
  expect(new Set(segments.map(s => s.n)).size).toBe(1) // every segment the same width
  expect(segments.slice(0, 5).every(s => s.fg === rgbWord(PALETTE.grn))).toBe(true)
  expect(segments[5]?.fg).toBe(rgbWord(PALETTE.blue))
  expect(segments.slice(6).every(s => s.fg === rgbWord(PALETTE.track))).toBe(true)
  expect(runs.length).toBe(23) // 12 segments + 11 gaps alternate
})

test('laneStrip colours one segment per lane by its role', () => {
  const c = cells(laneStrip(['live', 'live', 'warn', 'warn'], 11))
  const segs: number[] = []
  for (const cell of c) {
    if (cell.fg !== rgbWord(PALETTE.panel) && (segs.at(-1) ?? -1) !== cell.fg) segs.push(cell.fg)
  }
  expect(segs).toEqual([rgbWord(ROLE.live.fg), rgbWord(ROLE.warn.fg)])
})

// ---------- pickBar ----------

const rasterEls = { Raster: (props: Record<string, unknown>) => ({ type: 'Raster', props }) } as unknown as Elements[keyof Elements]
const bareEls = { Box: () => undefined, Text: () => undefined } as unknown as Elements[keyof Elements]
const fallback = { type: 'Text', props: {}, children: [] } as unknown as RenderElement

test('pickBar returns the Raster when the element table has one, else the fallback', () => {
  const spec = gradientBar(50, 20, HEAT)
  const raster = pickBar(rasterEls, 'usage-5h', spec, fallback) as unknown as { type: string; props: Record<string, unknown> }
  expect(raster.type).toBe('Raster')
  expect(raster.props).toMatchObject({ key: 'usage-5h', columns: 20, rows: 1, cells: spec.cells })
  expect(pickBar(bareEls, 'usage-5h', spec, fallback)).toBe(fallback)
})
