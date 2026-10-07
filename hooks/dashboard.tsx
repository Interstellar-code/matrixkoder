// MatrixKoder dashboard pane (design §3/§6): /mk (or /mk open|close|setup|init)
// drives it, session.start auto-opens it when allowed and runs the state loop;
// the tree renders the snapshot atoms with the 46-col base layout, 36-col
// compact and inline variants. All $-bound loading lives here: $ may not cross
// an import; the pure data-dir math is in data.ts for wake/tools to reuse.
import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, On, PluginOptions } from 'claude-code'

// the per-surface element table $.ui.resolve(e) answers (every surface's has Box/Text)
type Els = Elements[keyof Elements]
import type { MkEvent, MkRun, MkSnapshot, MkStatusMemory, MkTask } from '../types'
import {
  PANE_ID, EMPTY_SNAPSHOT, activeEntry, basename, candidateDirs, coerceIndex, coerceRun, coerceUsage, join,
  mainCheckout, mkSnapshot, normalizePath, parentOf, parseGitdir, parseJson, sanitizeText, timerRegistry,
} from './data'
import { chipText, toastsFor } from './status'

// atoms are file-local consts (the scan reads this file's own consts); the
// plugin+key address is what makes them one value across files
const snapshotAtom = atom({ plugin: 'matrixkoder', key: 'snapshot' } as const, EMPTY_SNAPSHOT)
const statusMemoryAtom = atom({ plugin: 'matrixkoder', key: 'taskStatuses' } as const, {
  statuses: {},
  waiting: false,
  loaded: false,
} satisfies MkStatusMemory)
const paneOpenAtom = atom({ plugin: 'matrixkoder', key: 'paneOpen' } as const, false)

const COLUMNS = 46
const REFRESH_MS = 5000
const USAGE_POLL_DEFAULT_MIN = 5
const USAGE_POLL_MIN_MIN = 1 // a faster poll with the 120 s timeout would stack runs
const POLL_MIN_INTERVAL_MS = 60_000
const AGENT_ORDER = ['claude', 'agy', 'codex', 'opencode'] as const
const PRIMARY_POOL: Record<string, string> = { agy: 'gemini', claude: 'default' }
const AGENT_GLYPH: Record<string, string> = { claude: '✦', agy: '∇', codex: '⊚', opencode: '⌂' }
const AGENT_COLOR: Record<string, string> = { claude: 'magenta', agy: 'blue', codex: 'green', opencode: 'cyan' }
const STATUS_COLOR: Record<string, string> = {
  planned: 'gray', routed: 'gray', dispatched: 'blue', building: 'cyan', verifying: 'cyan',
  review: 'yellow', fix: 'yellow', merged: 'green', qa: 'blue', done: 'green',
  blocked: 'red', quota: 'red', stalled: 'yellow', provider: 'red',
}
const STATUS_GLYPH: Record<string, string> = {
  planned: '○', routed: '◇', dispatched: '▷', building: '●', verifying: '⧖',
  review: '◐', fix: '▲', merged: '◈', qa: '◆', done: '✓',
  blocked: '⊘', quota: '⧗', stalled: '⚠', provider: '⊗',
}
/** compact status words per design §3.7 (`building 12m` → `build 12m`) */
const STATUS_SHORT: Record<string, string> = {
  building: 'build', verifying: 'verify', dispatched: 'disp', stalled: 'stall', planned: 'plan',
}
const DONE = new Set(['done', 'merged'])
const IN_PROGRESS = new Set(['dispatched', 'building', 'verifying', 'review', 'fix', 'qa', 'merged'])
const SIDE_STATE = new Set(['blocked', 'quota', 'stalled', 'provider'])
const LANE_LIVE = new Set(['dispatched', 'building', 'verifying', 'review', 'fix', 'stalled', 'blocked'])
const LANE_FALLBACK = new Set(['dispatched', 'building', 'verifying', 'fix'])
let pollInFlight = false
let lastPollAt = -Infinity
let lastChipText: string | null | undefined // undefined = never set; skip ui.status when unchanged

export function registerDashboard(on: On, options: PluginOptions): void {
  on('session.start', async ($, e, next) => {
    try {
      await $.command.register({ name: 'mk', description: 'Toggle the MatrixKoder dashboard pane', argumentHint: '[open|close|setup|init]' })
    } catch {
      // command registration refused — /mk stays inert, the pane still renders
    }
    if (options.autoOpen !== false) {
      try {
        const opened = await $.ui.open({ id: PANE_ID, title: 'MatrixKoder', columns: COLUMNS })
        await update($, paneOpenAtom, () => opened.isPlaced)
      } catch {
        // the engine seats the pane itself; a refusal is not fatal
      }
    }
    const min = typeof options.usagePollMin === 'number' && options.usagePollMin > 0
      ? Math.max(USAGE_POLL_MIN_MIN, options.usagePollMin)
      : USAGE_POLL_DEFAULT_MIN
    const cwd = e.cwd // the session's own words for where it runs; timers capture it
    // timers first: a throwing first load must never leave the loop dead
    // a cancel may come back as a plain function or a { cancel } handle (mock clock)
    const arm = (cancel: unknown): (() => void) =>
      typeof cancel === 'function' ? (cancel as () => void) : () => { (cancel as { cancel?: () => void }).cancel?.() }
    timerRegistry.cancelRefresh?.()
    timerRegistry.cancelRefresh = arm($.clock.every(REFRESH_MS, () => refreshSafe($, options, cwd)))
    timerRegistry.cancelPoll?.()
    timerRegistry.cancelPoll = arm($.clock.every(Math.round(min * 60_000), () => pollUsage($)))
    lastChipText = undefined
    await refreshSafe($, options, cwd) // first load populates pane + chip and never toasts
    const home = await homeDir($)
    const usageMissing = home == null || (await readJson($, usagePath(home))) == null
    if (usageMissing) void pollUsage($) // cold start: do not show "no ledger yet" for N minutes
    return next(e)
  })

  on('command.run', { command: 'mk' }, async ($, e) => {
    const args = e.args.trim().split(/\s+/)[0]?.toLowerCase() ?? ''
    if (args === 'setup' || args === 'init') {
      // never a toggle-close: open and show the first-run panel whatever the state
      await openPane($)
      const prev = await read($, snapshotAtom)
      await update($, snapshotAtom, () => ({ ...prev, setupHint: true }))
      return { text: args === 'setup' ? 'Setup wizard coming in P5 — showing the setup panel.' : 'Run init arrives with the wizard coming in P5 — showing the setup panel.' }
    }
    if (args === 'open') {
      await openPane($)
      return { text: 'MatrixKoder pane opened.' }
    }
    if (args === 'close') {
      await closePane($)
      return { text: 'MatrixKoder pane closed.' }
    }
    if (args !== '' && args !== 'toggle') {
      return { text: 'Usage: /mk [open|close|setup|init] — bare /mk toggles the pane.' }
    }
    if (await read($, paneOpenAtom)) {
      await closePane($)
      return { text: 'MatrixKoder pane closed.' }
    }
    await openPane($)
    return { text: 'MatrixKoder pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE_ID }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const snap = await read($, snapshotAtom)
    const cols = e.props.bodyColumns || COLUMNS
    const compact = cols < 40
    const extraActive = snap.runs.filter(r => r.status === 'active').length - 1

    return (
      <Box flexDirection="column" width={cols} gap={1}>
        {e.props.placement === 'inline' && (
          <Box borderStyle="round" borderColor="yellow" paddingX={1}>
            <Text color="yellow">Run /tui fullscreen to dock this panel on the right.</Text>
          </Box>
        )}
        {snap.error && (
          <Text color="yellow" bold={true}>⚠ {snap.error}</Text>
        )}
        {snap.setupHint || snap.dataDir === null
          ? FirstRun($.ui.resolve(e))
          : snap.run === null
            ? NoRun($.ui.resolve(e), snap)
            : RunSection($.ui.resolve(e), snap.run, compact, extraActive)}
        {!snap.setupHint && snap.dataDir !== null && snap.run !== null && PlanSection($.ui.resolve(e), snap.run, cols, compact, snap.now)}
        {!snap.setupHint && snap.dataDir !== null && snap.run !== null && LanesSection($.ui.resolve(e), snap.run, cols)}
        {UsageSection($.ui.resolve(e), snap, compact)}
        {!snap.setupHint && snap.run?.waiting && WaitingSection($.ui.resolve(e), snap.run.waiting)}
        {!snap.setupHint && snap.run && RecentSection($.ui.resolve(e), (snap.run.recent ?? []).slice(-3).reverse(), compact)}
      </Box>
    )
  })
}

async function openPane($: EngineInterface): Promise<void> {
  try {
    const opened = await $.ui.open({ id: PANE_ID, title: 'MatrixKoder', columns: COLUMNS })
    await update($, paneOpenAtom, () => opened.isPlaced)
  } catch {
    await update($, paneOpenAtom, () => false)
  }
}

async function closePane($: EngineInterface): Promise<void> {
  try {
    await $.ui.close({ id: PANE_ID })
  } catch {
    // already closed — fine
  }
  await update($, paneOpenAtom, () => false)
}

// ---------- $-bound loading (file-local: $ never crosses an import) ----------

async function readJson($: EngineInterface, path: string): Promise<unknown> {
  try {
    return parseJson(await $.fs.read(path))
  } catch {
    return null
  }
}

async function readText($: EngineInterface, path: string): Promise<string | null> {
  try {
    return await $.fs.read(path)
  } catch {
    return null // missing or unreadable
  }
}

/** Schema rule (state-schema.md): MATRIXKODER_DATA → nearest .git toplevel's
 *  .matrixkoder; a linked worktree (.git FILE) resolves to its main checkout.
 *  The walk stops before home: ~/.matrixkoder is the GLOBAL dir, never project data. */
async function resolveDataDir($: EngineInterface, cwd: string, home: string | null): Promise<string | null> {
  try {
    const viaEnv = await $.env.get('MATRIXKODER_DATA')
    if (viaEnv) return viaEnv
  } catch {
    // env noun refused — fall through to the walk
  }
  for (const dir of candidateDirs(cwd, home ?? undefined)) {
    const dotGit = join(dir, '.git')
    let toplevel: string | null = null
    try {
      const stat = await $.fs.stat(dotGit)
      if (stat.kind === 'dir') {
        toplevel = dir
      } else if (stat.kind === 'file') {
        const gitdir = parseGitdir((await readText($, dotGit)) ?? '')
        if (gitdir) {
          const gd = gitdir.startsWith('/') ? gitdir : join(dir, gitdir)
          const commondir = await readText($, join(gd, 'commondir'))
          if (commondir != null) toplevel = mainCheckout(gd, commondir) ?? dir
        }
      }
    } catch {
      continue // no .git here — keep walking up
    }
    if (toplevel == null) continue
    // nearest repo boundary found: nested repos stop here (schema: pass --data for the outer)
    const data = join(toplevel, '.matrixkoder')
    try {
      const s = await $.fs.stat(data)
      if (s.kind === 'dir') return data
    } catch {
      // repo without a data dir yet → first-run
    }
    return null
  }
  return null
}

async function loadRun($: EngineInterface, dataDir: string): Promise<{ run: MkRun | null; runs: ReturnType<typeof coerceIndex>; error: string | null }> {
  const runs = coerceIndex(await readJson($, join(dataDir, 'runs/index.json')))
  const entry = activeEntry(runs)
  if (!entry) return { run: null, runs, error: null }
  const runPath = join(dataDir, 'runs', entry.id, 'run.json')
  const text = await readText($, runPath)
  if (text == null) return { run: null, runs, error: `cannot read ${entry.id}/run.json` }
  const run = coerceRun(parseJson(text))
  if (run == null) return { run: null, runs, error: 'cannot read run.json' }
  return { run, runs, error: null }
}

async function homeDir($: EngineInterface): Promise<string | null> {
  try {
    const viaEnv = await $.env.get('HOME')
    if (viaEnv) return viaEnv
  } catch {
    // env noun refused — fall through to the process
  }
  try {
    const ran = await $.process.run(['printenv', 'HOME'])
    const out = ran.stdout.trim()
    if (ran.exitCode === 0 && out) return out
  } catch {
    // no process either — usage stays empty
  }
  return null
}

function usagePath(home: string): string {
  return join(home, '.matrixkoder/usage.json')
}

async function loadSnapshot($: EngineInterface, cwd: string): Promise<MkSnapshot> {
  let now = 0
  try {
    now = await $.clock.now()
  } catch {
    // clock refused — remaining times show as unknown
  }
  const home = await homeDir($)
  const dataDir = await resolveDataDir($, cwd, home)
  let run: MkRun | null = null
  let runs: ReturnType<typeof coerceIndex> = []
  let error: string | null = null
  if (dataDir) {
    ;({ run, runs, error } = await loadRun($, dataDir))
  }
  const usage = home ? coerceUsage(await readJson($, usagePath(home))) : null
  return { ...mkSnapshot(now, dataDir, run, runs, usage), error }
}

async function applyStatus($: EngineInterface, options: PluginOptions, snap: MkSnapshot): Promise<void> {
  const mem = await read($, statusMemoryAtom)
  // a run switch must not toast old-vs-new status diffs as transitions
  const sameRun = mem.runId === (snap.run?.id ?? null)
  if (options.chip !== false) {
    const text = chipText(snap)
    if (text !== lastChipText) {
      lastChipText = text
      try {
        $.ui.status(text === '' ? undefined : text)
      } catch {
        // status line refused — the pane still shows everything
      }
    }
  }
  if (options.toasts !== false && mem.loaded && sameRun) {
    for (const text of toastsFor(mem, snap)) {
      try {
        $.ui.toast(text, { timeoutMs: 4000 })
      } catch {
        // toast refused — non-fatal
      }
    }
  }
  const statuses: Record<string, string> = {}
  for (const t of snap.run?.tasks ?? []) statuses[t.id] = t.status
  await update($, statusMemoryAtom, () => ({ statuses, waiting: !!snap.run?.waiting, loaded: true, runId: snap.run?.id ?? null }))
}

/** snapshot signature without `now`: the clock alone must not force a redraw */
function contentSig(snap: MkSnapshot): string {
  return JSON.stringify({ ...snap, now: 0 })
}

/** Never throws: a failing load keeps the last good snapshot and flags the error. */
async function refreshSafe($: EngineInterface, options: PluginOptions, cwd: string): Promise<void> {
  const prev = await read($, snapshotAtom)
  try {
    const snap = await loadSnapshot($, cwd)
    await applyStatus($, options, snap)
    if (contentSig(prev) !== contentSig(snap)) {
      await update($, snapshotAtom, () => ({ ...snap, setupHint: prev.setupHint }))
    }
  } catch (err) {
    try {
      await update($, snapshotAtom, () => ({ ...prev, error: `cannot read run.json (${String(err).slice(0, 40)})` }))
    } catch {
      // even the atom write failed — nothing more to do
    }
  }
}

async function pollUsage($: EngineInterface): Promise<void> {
  if (pollInFlight) return
  let nowMs = 0
  try {
    nowMs = await $.clock.now()
  } catch {
    // clock refused — interval check falls back to lastPollAt only
  }
  if (nowMs && nowMs - lastPollAt < POLL_MIN_INTERVAL_MS) return
  pollInFlight = true
  lastPollAt = nowMs || lastPollAt + POLL_MIN_INTERVAL_MS
  try {
    await $.process.run(['python3', `${$.plugin.root}/scripts/mk-usage.py`, '--once'], { timeoutMs: 120_000 })
  } catch {
    // codexbar missing or slow — the refresh ticks still re-read usage.json
  } finally {
    pollInFlight = false
  }
}

// ---------- sections (plain functions: JSX tags resolve to locals by name) ----------

function FirstRun(els: Els) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Text bold={true}>Setup</Text>
      <Text color="cyan" bold={true}>⬡ Welcome to MatrixKoder!</Text>
      <Text>Configuration required for this project:</Text>
      <Text color="yellow" bold={true}>[!] Missing .matrixkoder/matrixkoder.yaml</Text>
      <Text color="cyan" bold={true}>▶ Run /mk setup to launch the wizard (coming in P5)</Text>
    </Box>
  )
}

function NoRun(els: Els, snap: MkSnapshot) {
  const { Box, Text } = els
  const past = snap.runs.filter(r => r.status !== 'active').slice(-3).reverse()
  return (
    <Box flexDirection="column">
      <Text bold={true}>Run</Text>
      <Text>No active orchestration run.</Text>
      <Text dimColor={true}>To start a new multi-lane feature run:</Text>
      <Text color="cyan" bold={true}>▶ Run /mk init &lt;feature-name&gt;</Text>
      {past.length > 0 && (
        <Box flexDirection="column" marginTop={1}>
          <Text bold={true}>Recent Runs</Text>
          {past.map(r => (
            <Box key={r.id} flexDirection="row" gap={1}>
              <Text color="green">✓</Text>
              <Text bold={true}>{r.id}</Text>
              <Text dimColor={true}>{r.feature ?? ''} {r.status ?? ''}</Text>
            </Box>
          ))}
        </Box>
      )}
    </Box>
  )
}

function RunSection(els: Els, run: MkRun, compact: boolean, extraActive = 0) {
  const { Box, Text } = els
  const head = run.head_sha ? `@${run.head_sha.slice(0, 7)}` : ''
  const feature = compact ? run.feature.split('-')[0] : run.feature
  return (
    <Box flexDirection="column">
      <Text bold={true}>Run</Text>
      <Box flexDirection="row" gap={1}>
        <Text bold={true}>{feature}</Text>
        {run.branch && <Text dimColor={true}>· {run.branch}{head}</Text>}
        <Text dimColor={true}>·</Text>
        <Text color="cyan" bold={true}>{compact ? 'stg' : 'stage'} {run.stage}/12</Text>
        {extraActive > 0 && <Text color="yellow" dimColor={true}>+{extraActive} active</Text>}
      </Box>
      {run.next && (
        <Text wrap="truncate-end"><Text color="cyan" bold={true}>▶ Next: </Text>{sanitizeText(run.next, 120)}</Text>
      )}
    </Box>
  )
}

function wavePills(tasks: readonly MkTask[], compact: boolean): { label: string; glyphs: string; color: string }[] {
  const waves = new Map<string, MkTask[]>()
  for (const t of tasks) {
    const key = t.wave == null ? 'W·' : `W${t.wave}`
    waves.set(key, [...(waves.get(key) ?? []), t])
  }
  return [...waves.entries()].sort((a, b) => a[0].localeCompare(b[0], undefined, { numeric: true })).map(([label, list]) => {
    if (list.every(t => DONE.has(t.status))) return { label, glyphs: '✓', color: 'green' }
    if (list.every(t => t.status === 'planned')) return { label, glyphs: '○', color: 'gray' }
    const active = list.filter(t => !DONE.has(t.status) && t.status !== 'planned').length
    if (compact) return { label, glyphs: `${active}/${list.length}`, color: 'yellow' }
    const glyphs = list.map(t => STATUS_GLYPH[t.status] ?? '○').join('')
    return { label, glyphs, color: 'yellow' }
  })
}

function elapsedMin(task: MkTask, nowMs: number): number | null {
  const t = Date.parse(task.updated ?? '')
  if (!Number.isFinite(t) || !nowMs) return null
  return Math.max(0, Math.round((nowMs - t) / 60000))
}

function statusWord(status: string, compact: boolean): string {
  return compact ? STATUS_SHORT[status] ?? status : status
}

function PlanSection(els: Els, run: MkRun, cols: number, compact: boolean, nowMs: number) {
  const { Box, Text } = els
  const done = run.tasks.filter(t => DONE.has(t.status)).length
  const total = run.tasks.length
  return (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between">
        <Box flexDirection="row" gap={1}>
          <Text bold={true}>Plan</Text>
          <Text color="green">{done}/{total} done</Text>
        </Box>
        <Box flexDirection="row" gap={1}>
          {wavePills(run.tasks, compact).map(w => (
            <Box key={w.label} flexDirection="row">
              <Text dimColor={true}>{w.label} </Text>
              <Text color={w.color}>{w.glyphs}</Text>
            </Box>
          ))}
        </Box>
      </Box>
      {run.tasks.map(task => {
        const isDone = task.status === 'done'
        const isActive = IN_PROGRESS.has(task.status)
        const isAttention = SIDE_STATE.has(task.status)
        const checkbox = isDone ? '[✓]' : isActive ? '[•]' : isAttention ? '[!]' : '[ ]'
        const checkColor = isDone ? 'green' : isActive ? 'cyan' : isAttention ? 'yellow' : 'gray'
        const title = compact ? task.title.slice(0, 14) : task.title // §3.7: 14-char title budget
        const round = task.round && (task.status === 'review' || task.status === 'fix') ? ` r${task.round}` : ''
        const mins = task.status === 'building' || task.status === 'verifying' || task.status === 'stalled' ? elapsedMin(task, nowMs) : null
        const suffix = `${round}${mins != null ? ` ${mins}m` : ''}`
        return (
          <Box key={task.id} flexDirection="row" justifyContent="space-between" gap={1}>
            <Box flexDirection="row" gap={1} flexShrink={1}>
              <Text color={checkColor} bold={isActive || isAttention}>{checkbox}</Text>
              <Text bold={true}>{task.id}</Text>
              <Text dimColor={task.status === 'planned'} wrap={compact ? 'truncate-end' : undefined}>{title}</Text>
            </Box>
            <Box flexDirection="row" gap={1} flexShrink={0}>
              <Text color={STATUS_COLOR[task.status] ?? 'text'}>{statusWord(task.status, compact)}{suffix}</Text>
              {cols >= 44 && task.last_commit && <Text dimColor={true}>{task.last_commit.slice(0, 7)}</Text>}
            </Box>
          </Box>
        )
      })}
    </Box>
  )
}

function LanesSection(els: Els, run: MkRun, cols: number) {
  const { Box, Text } = els
  const lanes = run.tasks.filter(t => t.lane && LANE_LIVE.has(t.status))
  // no task carries a lane (early run, lanes not dispatched yet): the live
  // dispatched|building|verifying|fix tasks stand in with agent glyph + model
  const rows = lanes.length > 0
    ? lanes.map(t => ({ task: t, slot: (t.lane ?? '').split('/').filter(Boolean).at(-1) ?? t.lane ?? '' }))
    : run.tasks.filter(t => LANE_FALLBACK.has(t.status)).map(t => ({ task: t, slot: '·' }))
  return (
    <Box flexDirection="column">
      <Text bold={true}>Lanes</Text>
      {rows.length === 0 && <Text dimColor={true}>none live</Text>}
      {rows.map(({ task: t, slot }) => {
        const agent = t.agent ?? 'agent'
        return (
          <Box key={t.id} flexDirection="row" justifyContent="space-between" gap={1}>
            <Box flexDirection="row" gap={1} flexShrink={1}>
              <Text dimColor={true}>{slot}</Text>
              <Text color={AGENT_COLOR[agent] ?? 'cyan'} bold={true}>{AGENT_GLYPH[agent] ?? '⌂'} {agent.toUpperCase()}</Text>
              <Text dimColor={true} wrap="truncate-end">{[t.model, t.effort].filter(Boolean).join('-')}</Text>
            </Box>
            <Box flexDirection="row" gap={1} flexShrink={0}>
              <Text color={SIDE_STATE.has(t.status) ? 'yellow' : 'cyan'}>{t.status}</Text>
              {cols >= 44 && t.last_commit && <Text dimColor={true}>{t.last_commit.slice(0, 7)}</Text>}
            </Box>
          </Box>
        )
      })}
    </Box>
  )
}

function bar(pct: number | null | undefined, width: number): string {
  const used = Math.max(0, Math.min(100, pct ?? 0))
  const full = Math.round((used / 100) * width)
  return '█'.repeat(full) + '░'.repeat(Math.max(0, width - full))
}

function pctColor(pct: number | null | undefined, exhausted: boolean): string {
  if (exhausted || (pct ?? 0) > 85) return 'red'
  if ((pct ?? 0) > 60) return 'yellow'
  return 'green'
}

function resetStr(iso: string | null | undefined, now: number, compact: boolean): string {
  const t = Date.parse(iso ?? '')
  if (!Number.isFinite(t) || !now) return '—'
  const ms = t - now
  if (ms < 0) return 'now'
  if (ms < 24 * 3600_000) {
    const h = Math.floor(ms / 3600_000)
    const m = Math.floor((ms % 3600_000) / 60_000)
    return compact ? `${h}h${String(m).padStart(2, '0')}` : `${h}h${String(m).padStart(2, '0')}m`
  }
  const d = new Date(t)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

type UsageRow = {
  name: string
  pct: number | null
  weekPct: number | null
  reset: string | null
  exhaustedUntil: string | null
  exhausted: boolean
  sub: { name: string; pct: number | null; weekPct: number | null; reset: string | null; exhaustedUntil: string | null; exhausted: boolean }[]
}

function usageRows(snap: MkSnapshot, compact: boolean): UsageRow[] {
  const agents = snap.usage?.agents ?? {}
  const names = [...AGENT_ORDER, ...Object.keys(agents).filter(a => !(AGENT_ORDER as readonly string[]).includes(a))]
  const now = snap.now
  const isExhausted = (until: string | null | undefined): boolean => {
    const t = Date.parse(until ?? '')
    return Number.isFinite(t) && now > 0 && t > now
  }
  return names.flatMap(name => {
    const a = agents[name]
    if (!a) return [] // noUncheckedIndexedAccess: presence checked, then narrowed
    const pools = Object.entries(a.pools ?? {})
    // primary = the configured pool when present, else the first (schema: the
    // agent-level fields mirror the primary pool); the rest render as sub-rows
    const configured = PRIMARY_POOL[name] ?? 'default'
    const primary = pools.some(([pname]) => pname === configured) ? configured : pools[0]?.[0]
    const sub = pools
      .filter(([pname]) => pname !== primary)
      .map(([pname, p]) => ({
        name: compact ? pname.slice(0, 6) : pname,
        pct: p.five_hour?.used_pct ?? null,
        weekPct: p.weekly?.used_pct ?? null,
        reset: p.five_hour?.resets_at ?? p.weekly?.resets_at ?? null,
        exhaustedUntil: p.exhausted_until ?? null,
        exhausted: isExhausted(p.exhausted_until),
      }))
    return [{
      name,
      pct: a.five_hour?.used_pct ?? null,
      weekPct: a.weekly?.used_pct ?? null,
      reset: a.five_hour?.resets_at ?? a.weekly?.resets_at ?? null,
      exhaustedUntil: a.exhausted_until ?? null,
      // agent red only on the agent-level exhausted_until (schema: set only
      // when every pool is exhausted — a lone secondary pool stays a sub-row)
      exhausted: isExhausted(a.exhausted_until),
      sub,
    } as UsageRow]
  })
}

function UsageSection(els: Els, snap: MkSnapshot, compact: boolean) {
  const { Box, Text } = els
  const rows = usageRows(snap, compact)
  const barW = compact ? 5 : 7 // design §4.2: 7 standard, 5 compact
  const NAME_W = 14 // fits '  └ claude-gpt': sub-row labels and agent names share one column
  const NUM_W = 5
  const RESET_W = 6
  const leftCol = (text: string, w: number): string => (text.length > w ? text.slice(0, w) : text.padEnd(w))
  const rightCol = (text: string, w: number): string => (text.length > w ? text.slice(0, w) : text.padStart(w))
  const pctCol = (pct: number | null): string => (pct == null ? rightCol('n/a', NUM_W) : rightCol(`${Math.round(pct)}%`, NUM_W))
  const weekCol = (pct: number | null): string => (pct == null ? rightCol('—', NUM_W) : rightCol(`${Math.round(pct)}%`, NUM_W))
  const resetCol = (iso: string | null): string => rightCol(resetStr(iso, snap.now, compact), RESET_W)

  const row = (keyId: string, name: string, nameColor: string | undefined, boldName: boolean, pct: number | null, weekPct: number | null, resetIso: string | null, exhausted: boolean, exhaustedUntil: string | null) => {
    const color = pctColor(pct, exhausted)
    const resetIsoShown = exhausted && exhaustedUntil ? exhaustedUntil : resetIso
    return (
      <Box key={keyId} flexDirection="row" gap={0}>
        <Text color={nameColor} bold={boldName}>{leftCol(name, NAME_W)}</Text>
        <Text color={color} bold={exhausted}>{bar(pct, barW)}</Text>
        <Text color={pct == null ? 'gray' : color} bold={exhausted}>{pctCol(pct)}</Text>
        {!compact && <Text dimColor={true}>{weekCol(weekPct)}</Text>}
        <Text dimColor={!exhausted} color={exhausted ? 'red' : undefined} bold={exhausted}>{resetCol(resetIsoShown)}</Text>
        {exhausted && <Text color="red" bold={true}>⊘</Text>}
      </Box>
    )
  }

  return (
    <Box flexDirection="column">
      <Box flexDirection="row" gap={0}>
        <Text bold={true}>Usage</Text>
        <Text>{' '.repeat(Math.max(1, NAME_W + barW - 6))}</Text>
        <Text dimColor={true}>{rightCol('5h', NUM_W)}</Text>
        {!compact && <Text dimColor={true}>{rightCol('week', NUM_W)}</Text>}
        <Text dimColor={true}>{rightCol('reset', RESET_W)}</Text>
      </Box>
      {rows.length === 0 && <Text dimColor={true}>no ledger yet</Text>}
      {rows.map(r => (
        <Box key={r.name} flexDirection="column">
          {row(r.name, leftCol(r.name, NAME_W), AGENT_COLOR[r.name] ?? 'cyan', r.exhausted, r.pct, r.weekPct, r.reset, r.exhausted, r.exhaustedUntil)}
          {r.sub.map(p => (
            row(`${r.name}:${p.name}`, `  └ ${compact ? p.name.slice(0, 6) : p.name}`, undefined, false, p.pct, p.weekPct, p.reset, p.exhausted, p.exhaustedUntil)
          ))}
        </Box>
      ))}
    </Box>
  )
}

function WaitingSection(els: Els, waiting: NonNullable<MkRun['waiting']>) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Text color="red" bold={true}>Waiting on you</Text>
      <Text bold={true} wrap="wrap">{sanitizeText(waiting.question, 160)}</Text>
      {waiting.options && waiting.options.length > 0 && (
        <Box flexDirection="column" marginTop={1}>
          {waiting.options.map((opt, i) => (
            <Text key={opt} dimColor={opt !== waiting.rec} color={opt === waiting.rec ? 'cyan' : undefined}>
              [{i + 1}] {sanitizeText(opt, 60)}{opt === waiting.rec ? ' (Rec)' : ''}
            </Text>
          ))}
        </Box>
      )}
    </Box>
  )
}

function RecentSection(els: Els, events: readonly MkEvent[], compact: boolean) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Text bold={true}>Recent</Text>
      {events.length === 0 && <Text dimColor={true}>nothing yet</Text>}
      {events.map((ev, i) => (
        <Box key={`ev${String(i)}`} flexDirection="row" gap={1}>
          {!compact && <Text dimColor={true}>{(ev.t ?? '').slice(11, 16)}</Text>}
          {/* event text is lane-watch pane evidence — sanitised before display */}
          <Text wrap="truncate-end" color={ev.type === 'verdict' ? 'green' : undefined}>{sanitizeText(ev.text ?? '', compact ? 40 : 80)}</Text>
        </Box>
      ))}
    </Box>
  )
}
