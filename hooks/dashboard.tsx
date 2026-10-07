// MatrixKoder dashboard pane (design §3/§6): /mk toggles it, session.start
// auto-opens it when allowed and runs the state loop; the tree renders the
// snapshot atoms with the 46-col base layout, 36-col compact and inline
// variants. All $-bound loading lives here: $ may not cross an import.
import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, On, PluginOptions } from 'claude-code'
import type { MkEvent, MkRun, MkSnapshot, MkStatusMemory, MkTask } from '../types'
import { PANE_ID, EMPTY_SNAPSHOT, activeEntry, candidateDirs, coerceIndex, coerceRun, coerceUsage, join, mkSnapshot, parseJson } from './data'
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
const DONE = new Set(['done', 'merged'])
const ATTENTION = new Set(['stalled', 'blocked', 'quota', 'provider'])
const LANE_LIVE = new Set(['dispatched', 'building', 'verifying', 'review', 'fix', 'stalled', 'blocked'])

export function registerDashboard(on: On, options: PluginOptions): void {
  on('session.start', async ($, e, next) => {
    try {
      await $.command.register({ name: 'mk', description: 'Toggle the MatrixKoder dashboard pane' })
    } catch {
      // command registration refused — /mk stays inert, the pane still renders
    }
    if (options.autoOpen !== false) {
      void $.ui.open({ id: PANE_ID, title: 'MatrixKoder', columns: COLUMNS }).catch(() => {
        // the engine seats the pane itself; a refusal is not fatal
      })
      await update($, paneOpenAtom, () => true)
    }
    const min = typeof options.usagePollMin === 'number' && options.usagePollMin > 0
      ? options.usagePollMin
      : USAGE_POLL_DEFAULT_MIN
    $.clock.every(Math.round(min * 60_000), () => pollUsage($))
    const cwd = e.cwd // the session's own words for where it runs; timers capture it
    await refresh($, options, cwd) // first load populates pane + chip and never toasts
    $.clock.every(REFRESH_MS, () => refresh($, options, cwd))
    return next(e)
  })

  on('command.run', { command: 'mk' }, async $ => {
    if (await read($, paneOpenAtom)) {
      try {
        await $.ui.close({ id: PANE_ID })
      } catch {
        // already closed — fine
      }
      await update($, paneOpenAtom, () => false)
      return { text: 'MatrixKoder pane closed.' }
    }
    await $.ui.open({ id: PANE_ID, title: 'MatrixKoder', columns: COLUMNS })
    await update($, paneOpenAtom, () => true)
    return { text: 'MatrixKoder pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE_ID }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const snap = await read($, snapshotAtom)
    const cols = e.props.bodyColumns || COLUMNS
    const compact = cols < 40

    return (
      <Box flexDirection="column" width={cols} gap={1}>
        {e.props.placement === 'inline' && (
          <Box borderStyle="round" borderColor="yellow" paddingX={1}>
            <Text color="yellow">Run /tui fullscreen to dock this panel on the right.</Text>
          </Box>
        )}
        {snap.dataDir === null
          ? FirstRun($.ui.resolve(e))
          : snap.run === null
            ? NoRun($.ui.resolve(e), snap)
            : RunSection($.ui.resolve(e), snap.run, cols, compact)}
        {snap.dataDir !== null && snap.run !== null && PlanSection($.ui.resolve(e), snap.run, cols, compact)}
        {snap.dataDir !== null && snap.run !== null && LanesSection($.ui.resolve(e), snap.run, cols)}
        {UsageSection($.ui.resolve(e), snap, cols, compact)}
        {snap.run?.waiting && WaitingSection($.ui.resolve(e), snap.run.waiting)}
        {snap.run && RecentSection($.ui.resolve(e), (snap.run.recent ?? []).slice(-3).reverse(), compact)}
      </Box>
    )
  })
}

// ---------- $-bound loading (file-local: $ never crosses an import) ----------

async function readJson($: EngineInterface, path: string): Promise<unknown> {
  try {
    return parseJson(await $.fs.read(path))
  } catch {
    return null
  }
}

async function findDataDir($: EngineInterface, cwd: string): Promise<string | null> {
  for (const dir of candidateDirs(cwd)) {
    try {
      const stat = await $.fs.stat(join(dir, '.matrixkoder'))
      if (stat.kind === 'dir') return join(dir, '.matrixkoder')
    } catch {
      // not there — walk up
    }
  }
  return null
}

async function loadRun($: EngineInterface, dataDir: string): Promise<{ run: MkRun | null; runs: ReturnType<typeof coerceIndex> }> {
  const runs = coerceIndex(await readJson($, join(dataDir, 'runs/index.json')))
  const entry = activeEntry(runs)
  if (!entry) return { run: null, runs }
  const run = coerceRun(await readJson($, join(dataDir, 'runs', entry.id, 'run.json')))
  return { run, runs }
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

async function loadSnapshot($: EngineInterface, cwd: string): Promise<MkSnapshot> {
  let now = 0
  try {
    now = await $.clock.now()
  } catch {
    // clock refused — remaining times show as unknown
  }
  const dataDir = await findDataDir($, cwd)
  let run: MkRun | null = null
  let runs: ReturnType<typeof coerceIndex> = []
  if (dataDir) {
    ;({ run, runs } = await loadRun($, dataDir))
  }
  const home = await homeDir($)
  const usage = home ? coerceUsage(await readJson($, join(home, '.matrixkoder/usage.json'))) : null
  return mkSnapshot(now, dataDir, run, runs, usage)
}

async function applyStatus($: EngineInterface, options: PluginOptions, snap: MkSnapshot): Promise<void> {
  const mem = await read($, statusMemoryAtom)
  if (options.chip !== false) {
    const text = chipText(snap)
    try {
      $.ui.status(text === '' ? undefined : text)
    } catch {
      // status line refused — the pane still shows everything
    }
  }
  if (options.toasts !== false && mem.loaded) {
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
  await update($, statusMemoryAtom, () => ({ statuses, waiting: !!snap.run?.waiting, loaded: true }))
}

async function refresh($: EngineInterface, options: PluginOptions, cwd: string): Promise<void> {
  const snap = await loadSnapshot($, cwd)
  await applyStatus($, options, snap)
  const prev = await read($, snapshotAtom)
  if (JSON.stringify(prev) !== JSON.stringify(snap)) {
    await update($, snapshotAtom, () => snap)
  }
}

async function pollUsage($: EngineInterface): Promise<void> {
  try {
    await $.process.run(['python3', `${$.plugin.root}/scripts/mk-usage.py`, '--once'], { timeoutMs: 120_000 })
  } catch {
    // codexbar missing or slow — the refresh ticks still re-read usage.json
  }
}

// ---------- sections (plain functions: JSX tags resolve to locals by name) ----------

function FirstRun(els: Elements) {
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

function NoRun(els: Elements, snap: MkSnapshot) {
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

function RunSection(els: Elements, run: MkRun, cols: number, compact: boolean) {
  const { Box, Text } = els
  const head = run.head_sha ? `@${run.head_sha.slice(0, 7)}` : ''
  return (
    <Box flexDirection="column">
      <Text bold={true}>Run</Text>
      <Box flexDirection="row" gap={1}>
        <Text bold={true}>{compact ? run.feature.split('-')[0] : run.feature}</Text>
        <Text dimColor={true}>·</Text>
        <Text dimColor={true}>{run.branch}{head}</Text>
        <Text dimColor={true}>·</Text>
        <Text color="cyan" bold={true}>{compact ? 'stg' : 'stage'} {run.stage}/12</Text>
      </Box>
      {run.next && (
        <Box flexDirection="row" gap={1}>
          <Text color="cyan" bold={true}>▶ Next:</Text>
          <Text wrap="truncate-end">{run.next}</Text>
        </Box>
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

function PlanSection(els: Elements, run: MkRun, cols: number, compact: boolean) {
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
        const isDone = DONE.has(task.status)
        const isBuilding = task.status === 'building' || task.status === 'verifying'
        const isAttention = ATTENTION.has(task.status)
        const checkbox = isDone ? '[✓]' : isBuilding ? '[•]' : isAttention ? '[!]' : '[ ]'
        const checkColor = isDone ? 'green' : isBuilding ? 'cyan' : isAttention ? 'yellow' : 'gray'
        return (
          <Box key={task.id} flexDirection="row" justifyContent="space-between" gap={1}>
            <Box flexDirection="row" gap={1} flexShrink={1}>
              <Text color={checkColor} bold={isBuilding || isAttention}>{checkbox}</Text>
              <Text bold={true}>{task.id}</Text>
              <Text dimColor={task.status === 'planned'} wrap={compact ? 'truncate-end' : undefined}>{task.title}</Text>
            </Box>
            <Box flexDirection="row" gap={1} flexShrink={0}>
              <Text color={STATUS_COLOR[task.status] ?? 'text'}>{task.status}</Text>
              {cols >= 44 && task.last_commit && <Text dimColor={true}>{task.last_commit.slice(0, 7)}</Text>}
            </Box>
          </Box>
        )
      })}
    </Box>
  )
}

function LanesSection(els: Elements, run: MkRun, cols: number) {
  const { Box, Text } = els
  const lanes = run.tasks.filter(t => t.lane && LANE_LIVE.has(t.status))
  return (
    <Box flexDirection="column">
      <Text bold={true}>Lanes</Text>
      {lanes.length === 0 && <Text dimColor={true}>none live</Text>}
      {lanes.map(t => {
        const agent = t.agent ?? 'agent'
        const slot = (t.lane ?? '').split('/').filter(Boolean).at(-1) ?? t.lane ?? ''
        return (
          <Box key={t.id} flexDirection="row" justifyContent="space-between" gap={1}>
            <Box flexDirection="row" gap={1} flexShrink={1}>
              <Text dimColor={true}>{slot}</Text>
              <Text color={AGENT_COLOR[agent] ?? 'cyan'} bold={true}>{AGENT_GLYPH[agent] ?? '⌂'} {agent.toUpperCase()}</Text>
              <Text dimColor={true} wrap="truncate-end">{[t.model, t.effort].filter(Boolean).join('-')}</Text>
            </Box>
            <Box flexDirection="row" gap={1} flexShrink={0}>
              <Text color={ATTENTION.has(t.status) ? 'yellow' : 'cyan'}>{t.status}</Text>
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
  exhausted: boolean
  sub: { name: string; pct: number | null; weekPct: number | null; reset: string | null; exhausted: boolean }[]
}

function usageRows(snap: MkSnapshot, compact: boolean): UsageRow[] {
  const agents = snap.usage?.agents ?? {}
  const names = [...AGENT_ORDER, ...Object.keys(agents).filter(a => !(AGENT_ORDER as readonly string[]).includes(a))]
  const now = snap.now
  const isExhausted = (until: string | null | undefined): boolean => {
    const t = Date.parse(until ?? '')
    return Number.isFinite(t) && now > 0 && t > now
  }
  return names.filter(n => agents[n]).map(name => {
    const a = agents[name]
    const pools = Object.entries(a.pools ?? {})
    const primary = PRIMARY_POOL[name] ?? 'default'
    const sub = pools
      .filter(([pname]) => pname !== primary)
      .map(([pname, p]) => ({
        name: compact ? pname.slice(0, 6) : pname,
        pct: p.five_hour?.used_pct ?? null,
        weekPct: p.weekly?.used_pct ?? null,
        reset: p.five_hour?.resets_at ?? p.weekly?.resets_at ?? null,
        exhausted: isExhausted(p.exhausted_until),
      }))
    return {
      name,
      pct: a.five_hour?.used_pct ?? null,
      weekPct: a.weekly?.used_pct ?? null,
      reset: a.five_hour?.resets_at ?? a.weekly?.resets_at ?? null,
      // agent red only when every pool is exhausted (design §4.3)
      exhausted: isExhausted(a.exhausted_until) || (pools.length > 0 && pools.every(([, p]) => isExhausted(p.exhausted_until))),
      sub,
    }
  })
}

function UsageSection(els: Elements, snap: MkSnapshot, cols: number, compact: boolean) {
  const { Box, Text } = els
  const rows = usageRows(snap, compact)
  const barWidth = compact ? 5 : 7
  return (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between">
        <Text bold={true}>Usage</Text>
        <Box flexDirection="row" gap={compact ? 1 : 2}>
          <Text dimColor={true}>5h</Text>
          {!compact && <Text dimColor={true}>week</Text>}
          <Text dimColor={true}>reset</Text>
        </Box>
      </Box>
      {rows.length === 0 && <Text dimColor={true}>no ledger yet</Text>}
      {rows.map(r => {
        const na = r.pct == null && r.sub.length === 0
        const color = pctColor(r.pct, r.exhausted)
        return (
          <Box key={r.name} flexDirection="column">
            <Box flexDirection="row" justifyContent="space-between">
              <Box flexDirection="row" gap={1}>
                <Text color={AGENT_COLOR[r.name] ?? 'cyan'} bold={r.exhausted}>{r.name.padEnd(8)}</Text>
                <Text color={color} bold={r.exhausted}>{bar(r.pct, barWidth)}</Text>
              </Box>
              <Box flexDirection="row" gap={1}>
                <Text color={na ? 'gray' : color} bold={r.exhausted}>{na ? 'n/a' : `${String(Math.round(r.pct ?? 0)).padStart(3)}%`}</Text>
                {!compact && <Text dimColor={true}>{na || r.weekPct == null ? '—' : `${String(Math.round(r.weekPct)).padStart(3)}%`}</Text>}
                <Text dimColor={!r.exhausted} color={r.exhausted ? 'red' : undefined} bold={r.exhausted}>{resetStr(r.reset, snap.now, compact)}</Text>
                {r.exhausted && <Text color="red" bold={true}>⊘</Text>}
              </Box>
            </Box>
            {r.sub.map(p => {
              const pcolor = pctColor(p.pct, p.exhausted)
              return (
                <Box key={p.name} flexDirection="row" justifyContent="space-between">
                  <Box flexDirection="row" gap={1}>
                    <Text dimColor={true}>  └ {p.name.padEnd(10)}</Text>
                    <Text color={pcolor} bold={p.exhausted}>{bar(p.pct, barWidth)}</Text>
                  </Box>
                  <Box flexDirection="row" gap={1}>
                    <Text color={pcolor} bold={p.exhausted}>{p.pct == null ? 'n/a' : `${String(Math.round(p.pct)).padStart(3)}%`}</Text>
                    {!compact && <Text dimColor={true}>{p.weekPct == null ? '—' : `${String(Math.round(p.weekPct)).padStart(3)}%`}</Text>}
                    <Text dimColor={!p.exhausted} color={p.exhausted ? 'red' : undefined} bold={p.exhausted}>{resetStr(p.reset, snap.now, compact)}</Text>
                    {p.exhausted && <Text color="red" bold={true}>⊘</Text>}
                  </Box>
                </Box>
              )
            })}
          </Box>
        )
      })}
    </Box>
  )
}

function WaitingSection(els: Elements, waiting: NonNullable<MkRun['waiting']>) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Text color="red" bold={true}>Waiting on you</Text>
      <Text bold={true} wrap="wrap">{waiting.question}</Text>
      {waiting.options && waiting.options.length > 0 && (
        <Box flexDirection="column" marginTop={1}>
          {waiting.options.map((opt, i) => (
            <Text key={opt} dimColor={opt !== waiting.rec} color={opt === waiting.rec ? 'cyan' : undefined}>
              [{i + 1}] {opt}{opt === waiting.rec ? ' (Rec)' : ''}
            </Text>
          ))}
        </Box>
      )}
    </Box>
  )
}

function RecentSection(els: Elements, events: readonly MkEvent[], compact: boolean) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Text bold={true}>Recent</Text>
      {events.length === 0 && <Text dimColor={true}>nothing yet</Text>}
      {events.map((ev, i) => (
        <Box key={i} flexDirection="row" gap={1}>
          <Text dimColor={true}>{(ev.t ?? '').slice(11, 16)}</Text>
          <Text wrap="truncate-end" color={ev.type === 'verdict' ? 'green' : undefined}>{ev.text ?? ''}</Text>
        </Box>
      ))}
    </Box>
  )
}
