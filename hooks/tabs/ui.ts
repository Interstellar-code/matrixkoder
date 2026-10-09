// Shared UI helpers for the /mk dashboard pane (S0.3): the pane's width
// consts, glyph/colour tables and column/bar primitives live here so every
// tab module (T1/T2) renders from one copy; dashboard.tsx drops its own
// versions and imports these back — no behaviour change, just a move.
export const NARROW = 40
export const WIDE = 120

// moved verbatim from dashboard.tsx: one home for the pane's agent and status
// vocabularies so the tabs and the pane can never drift apart
export const AGENT_GLYPH: Record<string, string> = { claude: '✦', agy: '∇', codex: '⊚', opencode: '⌂' }
export const AGENT_COLOR: Record<string, string> = { claude: 'magenta', agy: 'blue', codex: 'green', opencode: 'cyan' }
export const STATUS_COLOR: Record<string, string> = {
  planned: 'gray', routed: 'gray', dispatched: 'blue', building: 'cyan', verifying: 'cyan',
  review: 'yellow', fix: 'yellow', merged: 'green', qa: 'blue', done: 'green',
  blocked: 'red', quota: 'red', stalled: 'yellow', provider: 'red',
}
export const STATUS_GLYPH: Record<string, string> = {
  planned: '○', routed: '◇', dispatched: '▷', building: '●', verifying: '⧖',
  review: '◐', fix: '▲', merged: '◈', qa: '◆', done: '✓',
  blocked: '⊘', quota: '⧗', stalled: '⚠', provider: '⊗',
}

// the pane's usage bar, unchanged from dashboard.tsx: pct clamped to 0..100
// (null/undefined = 0), full cells rounded to the nearest column. `floor` (a
// 0..100 percentage) stamps '│' at its column over fill and empty alike, so
// a quota reset line stays readable at any usage; absent floor = the original
// two-arg behaviour the pane still calls.
export function bar(pct: number | null | undefined, width: number, floor?: number): string {
  const used = Math.max(0, Math.min(100, pct ?? 0))
  const full = Math.round((used / 100) * width)
  const cells = '█'.repeat(full) + '░'.repeat(Math.max(0, width - full))
  if (floor == null) return cells
  const idx = Math.min(width - 1, Math.floor((floor / 100) * width))
  return cells.slice(0, idx) + '│' + cells.slice(idx + 1)
}

// a legend line: ' · '-joined, dropping whole items from the end until it
// fits `width`. The last item is never dropped — a lone item too wide for
// the line is returned as-is (wrapping/truncation is downstream's call).
export function fitLegend(items: string[], width: number): string {
  const sep = ' · '
  const kept = [...items]
  while (kept.length > 1 && kept.join(sep).length > width) kept.pop()
  return kept.join(sep)
}

// moved verbatim from dashboard.tsx's UsageSection: fixed-width columns pad
// short text (left/right aligned) and hard-truncate long text
export function leftCol(text: string, w: number): string {
  return text.length > w ? text.slice(0, w) : text.padEnd(w)
}

export function rightCol(text: string, w: number): string {
  return text.length > w ? text.slice(0, w) : text.padStart(w)
}
