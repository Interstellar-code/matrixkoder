// MatrixKoder dashboard pane (design §3/§6): /mk (or /mk open|close|setup|init)
// drives it, session.start auto-opens it when allowed and runs the state loop;
// the tree renders the snapshot atoms with the 46-col base layout, 36-col
// compact and inline variants. All $-bound loading lives here: $ may not cross
// an import; the pure data-dir math is in data.ts for wake/tools to reuse.
import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, On, PluginOptions, RenderElement } from 'claude-code'

// the per-surface element table $.ui.resolve(e) answers (every surface's has Box/Text)
type Els = Elements[keyof Elements]
import type { MkDashTab, MkEvent, MkRun, MkRunIndexEntry, MkSnapshot, MkStatusMemory, MkTask } from '../types'
import {
  AGENT_ORDER, PANE_ID, EMPTY_SNAPSHOT, activeEntries, basename, candidateDirs, coerceIndex, coerceRun, coerceUsage, join,
  globalDirPath, mainCheckout, mkSnapshot, normalizePath, orderAgents, parentOf, parseGitdir, parseJson, parseYamlAgents, parseYamlProviders,
  projectDataDir, sanitizeText, timerRegistry,
} from './data'
import type { MkPaneSnapshot } from './data'
import { AGENT_COLOR, AGENT_GLYPH, NARROW, STATUS_COLOR, STATUS_GLYPH, bar, leftCol, rightCol } from './tabs/ui'
import { mockRunHeader, mockTabCounts, stageWord, STAGE_TOTAL } from './tabs/header'
import type { RunHeaderData } from './tabs/header'
import { Chip } from './tabs/kit'
import { PALETTE, ROLE } from './tabs/palette'
import { withRaster } from './tabs/raster'
import { EventsTab, eventsView } from './tabs/events'
import { LanesTab, lanesView } from './tabs/lanes'
import { OverviewTab, overviewView } from './tabs/overview'
import { PlanTab, planView } from './tabs/plan'
import { SettingsTab } from './tabs/settings'
import type { SettingsValue, SettingsView } from './tabs/settings'
import {
  applySavedView, buildSettingsView, firstTagLine, runListFields, parsePluginManifest, runMigrate, saveGlobals, saveYaml,
  savedEditKeys, topChangelogEntry,
} from './settings-io'
import type { RunFn, SavePayload, YamlFieldIn } from './settings-io'
import { UsageTab, usageView } from './tabs/usage'
import { WaitingTab, waitingAsks, waitingView, readPending, askView, runWaitView, emptyWaitingView } from './tabs/waiting'
import type { PendingAsk, RunWait } from './tabs/waiting'
import { KanbanTab, kanbanView, readCards, emptyKanbanView, kanbanFixtureView, cardsErrorLine } from './tabs/kanban'
import type { KanbanCard, KanbanFilters, CardsRead } from './tabs/kanban'
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
// S0.2: 'old' (the pre-tab pane) is the default tab
const dashTabAtom = atom({ plugin: 'matrixkoder', key: 'dashTab' } as const, 'old')
// C12: the tabs' local view state (folded phases, revealed options). The tabs
// reach it through the `ui` handle the Pane render hands them; only an onPress
// writes it, never a render.
const viewStateAtom = atom({ plugin: 'matrixkoder', key: 'viewState' } as const, {} as Record<string, string>)

const COLUMNS = 46
const REFRESH_MS = 5000
const USAGE_POLL_DEFAULT_MIN = 5
const USAGE_POLL_MIN_MIN = 1 // a faster poll with the 120 s timeout would stack runs
const POLL_MIN_INTERVAL_MS = 60_000
const SWEEP_INTERVAL_MS = 120_000
// The bus host (mk-bus.py): one idempotent `ensure` (or `heartbeat`) every 15 s.
// Its own start window is a few seconds of flock + probe, so the spawn timeout
// stays short — a slow one is swallowed and the next tick retries.
const BUS_INTERVAL_MS = 15_000
const BUS_TIMEOUT_MS = 10_000
const PRIMARY_POOL: Record<string, string> = { agy: 'gemini', claude: 'default' }
// the pane's glyph/colour vocabularies moved to tabs/ui.ts: one copy for the
// pane and the tab modules (T1/T2)
/** the tab bar (S0.2): order and hotkeys are the plan's — o Overview, l Lanes,
 * p Plan, u Usage, e Events, w Waiting, 0 Old. V0.4 dropped `short`: the bar
 * carries one scheme at every width — the active tab shows its full word, the
 * others the hotkey letter plus their count (`l 4`, `p 3/8`, `w !1`, `0`). */
const TABS: readonly { key: MkDashTab; hotkey: string; label: string }[] = [
  { key: 'overview', hotkey: 'o', label: 'Overview' },
  { key: 'lanes', hotkey: 'l', label: 'Lanes' },
  { key: 'plan', hotkey: 'p', label: 'Plan' },
  { key: 'usage', hotkey: 'u', label: 'Usage' },
  { key: 'events', hotkey: 'e', label: 'Events' },
  { key: 'waiting', hotkey: 'w', label: 'Waiting' },
  { key: 'kanban', hotkey: 'k', label: 'Kanban' },
  { key: 'old', hotkey: '0', label: 'Old' },
  { key: 'settings', hotkey: '9', label: 'Settings' },
]

/** S2.3: the `hiddenTabs` userConfig value (a comma list of tab keys or
 *  labels) as a set of tab keys. Unknown names are ignored; Settings is never
 *  hidden even when named. */
function hiddenTabKeys(hiddenTabs: unknown): Set<MkDashTab> {
  if (typeof hiddenTabs !== 'string' || hiddenTabs.trim() === '') return new Set()
  const out = new Set<MkDashTab>()
  for (const part of hiddenTabs.split(',')) {
    const name = part.trim().toLowerCase()
    if (name === '') continue
    const tab = TABS.find(t => t.key === name || t.label.toLowerCase() === name)
    if (tab != null && tab.key !== 'settings') out.add(tab.key)
  }
  return out
}

/** S2.3: the tabs the bar draws under `hiddenTabs` — Settings always stands. */
function visibleTabs(hiddenTabs: unknown): readonly { key: MkDashTab; hotkey: string; label: string }[] {
  const hidden = hiddenTabKeys(hiddenTabs)
  return TABS.filter(t => !hidden.has(t.key))
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
// The urgency order for a shared lane's rolled-up status, after mk-state.py's
// rollup(): a side state wins with SIDE precedence (blocked, provider, quota,
// stalled — "the first present wins"), else the most advanced status in the
// lifecycle order STATUSES defines (the order STATUS_COLOR/STATUS_GLYPH follow
// here) names the lane's current work; unknown statuses rank least advanced,
// like mk-state's order.get(s, 0).
const LANE_SIDE: readonly string[] = ['blocked', 'provider', 'quota', 'stalled']
const LANE_STATUS_ORDER: readonly string[] = [
  'planned', 'routed', 'dispatched', 'building', 'verifying', 'review', 'fix',
  'merged', 'qa', 'done', 'blocked', 'quota', 'stalled', 'provider',
]

function laneStatusRank(status: string): number {
  const i = LANE_STATUS_ORDER.indexOf(status)
  return i === -1 ? 0 : i
}

/** The one status a shared lane's tasks roll up to: the most urgent among them. */
function rollupLaneStatus(tasks: readonly MkTask[]): string {
  const statuses = tasks.map(t => t.status)
  for (const s of LANE_SIDE) {
    if (statuses.includes(s)) return s
  }
  return statuses.reduce((top, s) => (laneStatusRank(s) > laneStatusRank(top) ? s : top))
}

/** `…`-truncate to `budget` columns (data.ts's sanitizeText caps without
 *  marking the cut; the plan rows must show where the title yielded). */
function truncateTitle(text: string, budget: number): string {
  if (text.length <= budget) return text
  return `${text.slice(0, Math.max(1, budget) - 1)}…`
}
let pollInFlight = false
let lastPollAt = -Infinity
let sweepInFlight = false
let lastSweepAt = -Infinity
let busInFlight = false
const busTimers = timerRegistry
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
    await startLoop($, options, e.cwd) // the session's own words for where it runs; timers capture it
    void ensureBus($, options, e.cwd) // the 15 s tick is too late for a session that needs the bus now
    await loadBrandVersion($)
    // S2.3: `defaultTab` picks the opening tab — a known visible tab stands,
    // anything else keeps the Old default.
    const opening = typeof options.defaultTab === 'string'
      ? visibleTabs(options.hiddenTabs).find(t => t.key === options.defaultTab)
      : undefined
    if (opening != null) {
      try {
        await update($, dashTabAtom, () => opening.key)
      } catch {
        // the atom write failed — the pane keeps the Old default
      }
    }
    return next(e)
  })

  on('command.run', { command: 'mk' }, async ($, e, next) => {
    // /clear fires session.end (timers cancelled) but never session.start: /mk re-arms the loop
    if (timerRegistry.cancelRefresh == null) {
      try {
        const cwd = await $.session.cwd()
        await startLoop($, options, cwd)
        await loadBrandVersion($)
        void ensureBus($, options, cwd) // /mk is how /clear comes back — the bus needs no /clear
      } catch {
        // no cwd — the pane keeps its last snapshot
      }
    }
    const args = e.args.trim().split(/\s+/)[0]?.toLowerCase() ?? ''
    if (args === 'setup' || args === 'init') {
      // wizard.tsx's command.run hook (registered after this one) answers: $ cannot cross an import
      return next(e)
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

  on('ui.focus', { requestId: PANE_ID }, async ($, e, next) => {
    // Carried MEDIUM: keep the focused element's key where the Settings tab
    // reads it (`settings:focus`); the tab drops s/x/r/f/c + 1-8 while the
    // key names one of its Inputs. The ring move itself always goes through.
    const out = await next(e)
    await setViewKey($, 'settings:focus', e.element ?? '')
    return out
  })

  on('ui.render', { component: 'Pane', requestId: PANE_ID }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Text } = els
    const snap = await read($, snapshotAtom) as MkPaneSnapshot
    const dashTab = await read($, dashTabAtom)
    const viewState = await read($, viewStateAtom)
    const pane = waitingPane(viewState)
    const cols = e.props.bodyColumns || COLUMNS
    const compact = cols < NARROW
    // S2.3: `raster: false` hands the tabs a table without Raster, so every
    // pickBar falls back to its eighth-block bar.
    const tabEls = withRaster(els, options.raster)
    const tabs = visibleTabs(options.hiddenTabs)
    // V0.4: the header is the snapshot's run on Old, the mock world on the six mock tabs
    const realTab = dashTab === 'old' || dashTab === 'settings'
    const header = realTab ? realRunHeader(snap) : mockRunHeader()
    const counts = { ...(realTab ? realTabCounts(snap.run) : mockTabCounts()), kanban: '' }
    // C12: the tabs' view-state handle. `get` reads the atom's snapshot; `set`
    // writes it and is only ever called from an onPress (C10). `save`/`recheck`
    // are the Settings tab's live wires (S3): the ordered write and the About
    // re-check, both fire-and-forget from a press.
    const ui: TabUi = {
      get: key => viewState[key],
      set: (key, value) => { void update($, viewStateAtom, prev => ({ ...prev, [key]: value })) },
      save: payload => { void saveSettings($, payload) },
      recheck: () => { void recheckAbout($) },
    }
    // S3: the live Settings view once loaded. Before that a one-line
    // skeleton draws — never the fixture's mock rows (LOW 1). A tab opened
    // straight onto settings (defaultTab) loads here while nothing is stored;
    // every other open reloads from the tab switch (LOW 5) — a render must
    // never start a refresh on its own, or the refresh tick would re-list.
    const settingsState = readSettingsState(viewState)
    const settingsLive = settingsState.live
    if (dashTab === 'settings' && settingsLive == null && !settingsInflight && (viewState['settings:load:error'] ?? '') === '') {
      void ensureSettingsLoaded($)
    }

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
        {/* one header for every tab: the brand block, then the run block on every tab but Settings */}
        {BrandHeader(els, cols, settingsLive?.about.installed ?? viewState['brand:version'] ?? '', settingsLive?.about.latest ?? '', String(options.mode ?? 'orchestrator'))}
        {dashTab !== 'settings' && header != null && RunHeader(els, header, cols)}
        {/* V1.4: the engine has no per-side borders, so the header and tab bar
            read as one framed block above the tab's Cards through this rule.
            The bar and the rule share a gap-less column so the rule sits
            directly under the bar; the pane's own gap keeps the space after it. */}
        <Box flexDirection="column">
          {TabBar(els, $, dashTab, counts, tabs, compact)}
          <Text color={PALETTE.line}>{'─'.repeat(Math.max(0, cols))}</Text>
        </Box>
        {dashTab === 'old'
          ? OldTab(els, snap, cols, compact)
          : dashTab === 'overview'
            ? OverviewTab(tabEls, overviewView(), cols, ui)
            : dashTab === 'lanes'
              ? withViewState(LanesTab, tabEls, lanesView(), cols, ui)
              : dashTab === 'plan'
                ? PlanTab(tabEls, planView(), cols, ui)
            : dashTab === 'settings'
              ? settingsLive != null
                ? SettingsTab(tabEls, settingsLive, cols, ui)
                : SettingsLoading(els, cols, viewState['settings:load:error'] ?? '')
                  : dashTab === 'usage'
                    ? withViewState(UsageTab, tabEls, usageView(), cols, ui)
                    : dashTab === 'events'
                      ? withViewState(EventsTab, tabEls, eventsView(), cols, ui)
                      : dashTab === 'kanban'
                        ? KanbanTab(tabEls, kanbanPaneView(viewState), cols)
                        : pane.src === 'live'
                          ? waitingAsks(tabEls, pane.asks, cols, new Date().toISOString(), ui)
                          : withViewState(WaitingTab, tabEls, waitingPaneView(pane, snap.run?.waiting ?? null), cols, ui)}
      </Box>
    )
  })
}

/** C12: a tab's view-state handle — the structural type every tab file declares
 *  for itself (no import crosses lanes). `save`/`recheck` are the Settings
 *  tab's live wires (S3); every other tab ignores them. */
type TabUi = {
  get(key: string): string | undefined
  set(key: string, value: string): void
  save?: (payload: SavePayload) => void
  recheck?: () => void
}

/** C12 hands `ui` to all six tabs; V2's three still take three arguments, so
 *  their call goes through a widened view of their signature until they do. */
function withViewState<V>(
  tab: (els: Els, view: V, cols: number) => RenderElement,
  els: Els,
  view: V,
  cols: number,
  ui: TabUi,
): RenderElement {
  return (tab as (els: Els, view: V, cols: number, ui?: TabUi) => RenderElement)(els, view, cols, ui)
}

/** A tab-bar press (S0.2): the state write is legal in onPress (a render must
 * never write), and the pane scrolls back to the top — a refused scroll leaves
 * the switch standing. Opening settings kicks its live load (S3); every switch
 * drops a stale Settings ring key with it (MEDIUM 2). */
async function switchTab($: EngineInterface, tab: MkDashTab): Promise<void> {
  await update($, dashTabAtom, () => tab)
  await setViewKey($, 'settings:focus', '')
  if (tab === 'settings') void ensureSettingsLoaded($)
  try {
    await $.ui.scroll({ in: PANE_ID, to: 'start' })
  } catch {
    // no scroll here — the tab still switched
  }
}

/** S3: the first-open skeleton — one dim line, never mock rows (LOW 1). A
 *  settled load error draws its `⚠` line here too (no live view to carry it). */
function SettingsLoading(els: Els, cols: number, loadError: string): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="column" width={cols} gap={1}>
      {loadError !== '' && <Text color={ROLE.err.fg}>{`⚠ ${loadError.length > cols ? `${loadError.slice(0, Math.max(0, cols - 2))}…` : loadError}`}</Text>}
      {loadError === '' && <Text dimColor={true}>loading settings…</Text>}
    </Box>
  )
}

/** The Settings artboard's header (Settings.dc.html): the two-tone logo, the
 *  MATRIXKODER wordmark and the version chip, then the mode chip and, when a
 *  newer tag exists, the amber update chip. */
function BrandHeader(els: Els, cols: number, installed: string, latest: string, mode: string): RenderElement {
  const { Box, Text } = els
  const v = (x: string): string => (x === '' ? '' : x.startsWith('v') ? x : `v${x}`)
  const update = latest !== '' && latest !== installed
  return (
    <Box flexDirection="column" width={cols}>
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Box flexDirection="row" gap={1}>
          <Box flexDirection="row"><Text color={PALETTE.blue}>█</Text><Text color={PALETTE.grn}>█</Text></Box>
          <Text bold={true}>MATRIXKODER</Text>
        </Box>
        {installed !== '' && Chip(els, { label: v(installed), role: 'live', solid: true })}
      </Box>
      <Box flexDirection="row" gap={1}>
        {Chip(els, { label: mode.toUpperCase(), role: 'live' })}
        {update && Chip(els, { label: `↑ ${v(latest)} ready`, role: 'warn' })}
      </Box>
    </Box>
  )
}

/** V0.4's run header (Main.dc.html): the bold run name with the stage chip (and
 *  the MOCK marker on a mock tab) on the right, the dim id line under NARROW
 *  dropped, then the wrapped ▶ Next line. Every row is budgeted to `cols`. */
function RunHeader(els: Els, head: RunHeaderData, cols: number) {
  const { Box, Text } = els
  // a chip draws ' label ' — one cell each side — and the two keep a gap column
  const rightWidth = (head.mock ? 'MOCK'.length + 2 + 1 : 0) + head.stage.length + 2
  const nameBudget = Math.max(1, cols - rightWidth - 1)
  return (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text bold={true}>{truncateTitle(head.name, nameBudget)}</Text>
        <Box flexDirection="row" gap={1} flexShrink={0}>
          {head.mock && Chip(els, { label: 'MOCK', role: 'idle' })}
          {Chip(els, { label: head.stage, role: 'live' })}
        </Box>
      </Box>
      {cols >= NARROW && head.sub !== '' && (
        <Text dimColor={true}>{truncateTitle(head.sub, cols)}</Text>
      )}
      {wrapToCols(`▶ ${head.next}`, cols).map((line, i) => (
        <Text key={`next-${String(i)}`} color="subtle">{line}</Text>
      ))}
    </Box>
  )
}

/** Word-wrap to `cols` cells (Main.dc.html's Next line wraps), one Text per
 *  line: the pane's row-width contract holds and an overlong word is cut. */
function wrapToCols(text: string, cols: number): string[] {
  const width = Math.max(1, Math.floor(cols))
  const lines: string[] = []
  let line = ''
  const push = (): void => { if (line !== '') { lines.push(line); line = '' } }
  for (const word of text.split(/\s+/).filter(w => w !== '')) {
    if (word.length > width) {
      push()
      for (let i = 0; i < word.length; i += width) lines.push(word.slice(i, i + width))
      continue
    }
    if (line === '') line = word
    else if (line.length + 1 + word.length <= width) line += ` ${word}`
    else { push(); line = word }
  }
  push()
  return lines.length > 0 ? lines : ['']
}

/** V0.4's tab bar (Controls.dc.html): the active tab is the primary chip with
 *  its full word and count, the others the hotkey letter plus their count, dim.
 *  F0: every Button carries its keyboard `hotkey` again. A plain Button draws
 *  `h: label`, so an inactive tab's label is its count alone (`l: 4`, `p: 3/8`,
 *  `w: !1`) — or the `·` placeholder when it has none (`o: ·`), never its own
 *  letter. At 36 cols the wrapped bar runs to two rows; no row is wider than
 *  the pane. */
function TabBar(els: Els, $: EngineInterface, dashTab: MkDashTab, counts: Record<MkDashTab, string>, tabs: readonly { key: MkDashTab; hotkey: string; label: string }[], compact: boolean) {
  const { Box, Button } = els
  return (
    <Box flexDirection="row" flexWrap="wrap">
      {tabs.map(t => {
        const active = dashTab === t.key
        const count = counts[t.key]
        // the open tab sits on the tint like the Settings sub-strip's picked scope; it stays a press (its key and hotkey)
        const btn = (
          <Button
            key={`dash-tab-${t.key}`}
            hotkey={t.hotkey}
            label={active || !compact ? `${t.label}${count === '' ? '' : ` ${count}`}` : count === '' ? '·' : count}
            plain={true}
            dimColor={active ? undefined : true}
            onPress={() => { void switchTab($, t.key) }}
          />
        )
        return <Box key={`dash-tab-${t.key}-box`} borderStyle="round" borderColor={active ? ROLE.live.fg : '#5b6776'} backgroundColor={active ? ROLE.live.tint : undefined}>{btn}</Box>
      })}
    </Box>
  )
}

/** The Old tab's header from the real snapshot (the newest run); no run, no
 *  header — first-run and no-run keep their own panels. */
function realRunHeader(snap: MkPaneSnapshot): RunHeaderData | null {
  const run = snap.run
  if (run == null) return null
  const head = run.head_sha ? `@${run.head_sha.slice(0, 7)}` : ''
  return {
    name: run.feature,
    stage: `${run.stage}/${STAGE_TOTAL} ${stageWord(run.stage)}`.trim(),
    next: sanitizeText(run.next, 120),
    sub: `${run.id} · ${run.branch}${head}`,
    mock: false,
  }
}

/** The Old tab's tab-bar counts from the same snapshot (the mockup's count
 *  vocabulary): live lanes, done/total, one open decision. */
function realTabCounts(run: MkRun | null): Record<MkDashTab, string> {
  const none: Record<MkDashTab, string> = { overview: '', lanes: '', plan: '', usage: '', events: '', waiting: '', kanban: '', old: '', settings: '' }
  if (run == null) return none
  const done = run.tasks.filter(t => DONE.has(t.status)).length
  return {
    ...none,
    lanes: String(liveLaneRows(run)),
    plan: `${done}/${run.tasks.length}`,
    waiting: run.waiting ? '!1' : '',
  }
}

/** The rows LanesSection lists (one per lane path, else the live tasks standing
 *  in) — the tab-bar count must agree with the rows below it. */
function liveLaneRows(run: MkRun): number {
  const lanes = run.tasks.filter(t => t.lane && LANE_LIVE.has(t.status))
  if (lanes.length > 0) return new Set(lanes.map(t => t.lane)).size
  return run.tasks.filter(t => LANE_FALLBACK.has(t.status)).length
}

/** Arms the refresh, usage-poll and lane-sweep timers for `cwd`, then loads the first snapshot. */
async function startLoop($: EngineInterface, options: PluginOptions, cwd: string): Promise<void> {
  const min = typeof options.usagePollMin === 'number' && options.usagePollMin > 0
    ? Math.max(USAGE_POLL_MIN_MIN, options.usagePollMin)
    : USAGE_POLL_DEFAULT_MIN
  // timers first: a throwing first load must never leave the loop dead
  // a cancel may come back as a plain function or a { cancel } handle (mock clock)
  const arm = (cancel: unknown): (() => void) =>
    typeof cancel === 'function' ? (cancel as () => void) : () => { (cancel as { cancel?: () => void }).cancel?.() }
  timerRegistry.cancelRefresh?.()
  timerRegistry.cancelRefresh = arm($.clock.every(REFRESH_MS, () => refreshSafe($, options, cwd)))
  timerRegistry.cancelPoll?.()
  timerRegistry.cancelPoll = arm($.clock.every(Math.round(min * 60_000), () => pollUsage($, cwd)))
  timerRegistry.cancelSweep?.()
  timerRegistry.cancelSweep = arm($.clock.every(SWEEP_INTERVAL_MS, () => sweepLanes($, options, cwd)))
  busTimers.cancelBus?.()
  busTimers.cancelBus = arm($.clock.every(BUS_INTERVAL_MS, () => ensureBus($, options, cwd)))
  lastChipText = undefined
  await refreshSafe($, options, cwd) // first load populates pane + chip and never toasts
  const gdir = await globalDirOf($, await homeDir($))
  const usageMissing = gdir == null || (await readJson($, usagePath(gdir))) == null
  if (usageMissing) void pollUsage($, cwd) // cold start: do not show "no ledger yet" for N minutes
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

/** Schema rule (state-schema.md): MATRIXKODER_DATA → ~/.matrixkoder/projects.json entry for the repo → nearest .git
 *  toplevel's .matrixkoder; a linked worktree (.git FILE) resolves to its main checkout.
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
    // the wizard's projects.json (home mode) names the data dir of this repo; else <repo>/.matrixkoder
    const gdir = await globalDirOf($, home)
    const listed = gdir == null ? null : projectDataDir(await readJson($, join(gdir, 'projects.json')), [toplevel], home)
    const data = listed ?? join(toplevel, '.matrixkoder')
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

/** Loads every active run, newest first. The NEWEST keeps the single-run
 *  semantics it always had — its run/error drive the ⚠ note, the NoRun panel
 *  and the chip/toasts/wake inputs (snap.run); an OLDER run whose run.json
 *  cannot be read is skipped, never blanking the pane. */
async function loadRuns($: EngineInterface, dataDir: string): Promise<{ run: MkRun | null; activeRuns: MkRun[]; runs: ReturnType<typeof coerceIndex>; error: string | null }> {
  const runs = coerceIndex(await readJson($, join(dataDir, 'runs/index.json')))
  const active = activeEntries(runs)
  const head = active[0]
  if (!head) return { run: null, activeRuns: [], runs, error: null }
  const first = await readRun($, dataDir, head)
  if (first.run == null) return { run: null, activeRuns: [], runs, error: first.error }
  const activeRuns = [first.run]
  for (const entry of active.slice(1)) {
    const more = await readRun($, dataDir, entry)
    if (more.run != null) activeRuns.push(more.run)
  }
  return { run: first.run, activeRuns, runs, error: null }
}

/** One run.json → a coerced run, or the error the loader reports for it. */
async function readRun($: EngineInterface, dataDir: string, entry: MkRunIndexEntry): Promise<{ run: MkRun | null; error: string | null }> {
  const runPath = join(dataDir, 'runs', entry.id, 'run.json')
  const text = await readText($, runPath)
  if (text == null) return { run: null, error: `cannot read ${entry.id}/run.json` }
  const run = coerceRun(parseJson(text))
  if (run == null) return { run: null, error: 'cannot read run.json' }
  return { run, error: null }
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

/** The global dir ($MATRIXKODER_HOME, else ~/.matrixkoder): usage.json, projects.json. */
async function globalDirOf($: EngineInterface, home: string | null): Promise<string | null> {
  let envHome: string | undefined
  try {
    envHome = (await $.env.get('MATRIXKODER_HOME')) || undefined
  } catch {
    // env noun refused — the default
  }
  return globalDirPath(envHome, home)
}

function usagePath(globalDir: string): string {
  return join(globalDir, 'usage.json')
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
  let activeRuns: MkRun[] = []
  let runs: ReturnType<typeof coerceIndex> = []
  let error: string | null = null
  if (dataDir) {
    ;({ run, activeRuns, runs, error } = await loadRuns($, dataDir))
  }
  const gdir = await globalDirOf($, home)
  const usage = gdir ? coerceUsage(await readJson($, usagePath(gdir))) : null
  // the project yaml scopes the usage section (and its poll) to this project's agents and providers
  const yaml = dataDir != null ? await readText($, join(dataDir, 'matrixkoder.yaml')) : null
  const projectAgents = orderAgents(parseYamlAgents(yaml ?? ''))
  return { ...mkSnapshot(now, dataDir, run, activeRuns, runs, usage, projectAgents), error }
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
  const ready = (snap.run?.tasks ?? []).filter(t => t.ready).map(t => t.id)
  await update($, statusMemoryAtom, () => ({ statuses, ready, waiting: !!snap.run?.waiting, loaded: true, runId: snap.run?.id ?? null }))
}

/** snapshot signature without `now`: the clock alone must not force a redraw */
function contentSig(snap: MkSnapshot): string {
  return JSON.stringify({ ...snap, now: 0 })
}


/** Pending asks kept on the view-state atom so a tick is one read, not a render.
 *  `waiting:src` is `live` (the bus answered), `down` (it did not) or unset
 *  (no tick yet, so the tab still shows the fixture). */
const PENDING_KEY = 'waiting:pending'
const PENDING_SRC_KEY = 'waiting:src'
const PENDING_TICK_KEY = 'waiting:tick'

export function waitingPane(view: Record<string, string>): { src: string; asks: PendingAsk[] } {
  let asks: PendingAsk[] = []
  try {
    const parsed = JSON.parse(view[PENDING_KEY] ?? '[]') as unknown
    if (Array.isArray(parsed)) asks = parsed as PendingAsk[]
  } catch {
    asks = []
  }
  return { src: view[PENDING_SRC_KEY] ?? '', asks }
}

export function waitingPaneView(pane: { src: string; asks: PendingAsk[] }, runWaiting: RunWait | null) {
  const { src, asks } = pane
  if (src === '') return waitingView()
  if (src === 'live') {
    const first = asks[0]
    if (first == null) return emptyWaitingView()
    return askView(first, new Date().toISOString())
  }
  if (runWaiting != null && runWaiting.question !== '') return runWaitView(runWaiting)
  return waitingView()
}

/** One `mk-bus.py pending` per dashboard tick, for the active project's data dir.
 *  Never starts a bus and never creates a file: `readPending` only runs the
 *  injected command. The in-flight guard is the tick counter on the view state,
 *  not a module flag: a tick whose number is already recorded has a read in
 *  flight or done, so a second call for the same tick returns. */
async function refreshPending($: EngineInterface, cwd: string): Promise<void> {
  const view = await read($, viewStateAtom)
  const tick = String(Number(view[PENDING_TICK_KEY] ?? '0') + 1)
  const current = await read($, viewStateAtom)
  if (current[PENDING_TICK_KEY] === tick) return
  await update($, viewStateAtom, prev => (
    prev[PENDING_TICK_KEY] === tick ? prev : { ...prev, [PENDING_TICK_KEY]: tick }
  ))
  const dataDir = await resolveDataDir($, cwd, await homeDir($)).catch(() => null)
  const run = (argv: readonly string[], init?: { timeoutMs?: number }) =>
    $.process.run([...argv], init).then(r => ({ stdout: r.stdout, code: r.exitCode }))
  try {
    const got = await readPending(run, $.plugin.root, dataDir)
    if (!got.live && !got.down) return
    await update($, viewStateAtom, prev => ({
      ...prev,
      [PENDING_SRC_KEY]: got.down ? 'down' : 'live',
      ...(got.live ? { [PENDING_KEY]: JSON.stringify(got.asks) } : {}),
    }))
  } catch {
    await update($, viewStateAtom, prev => ({ ...prev, [PENDING_SRC_KEY]: 'down' }))
  }
}

/** Cards JSON kept on the view-state atom so a tick is one read, not a render. */
const CARDS_KEY = 'kanban:cards'
const CARDS_MS_KEY = 'kanban:ms'
const CARDS_TICK_KEY = 'kanban:tick'
const CARDS_EMPTY_KEY = 'kanban:empty'
const CARDS_ERROR_KEY = 'kanban:error'
const CARDS_STALE_KEY = 'kanban:stale'

function kanbanFiltersOf(view: Record<string, string>): KanbanFilters {
  return {
    needsYou: view['kanban:needs'] === '1',
    project: view['kanban:project'] ? view['kanban:project'] : null,
  }
}

/** The Kanban body: empty until a root store has been seen, else the last cards read. */
function kanbanPaneView(view: Record<string, string>) {
  // Fixture only in the explicit mock mode the other tabs use (kanban:mock=1).
  // Anything else with no store, or before the first tick, is the empty state —
  // never the fixture cards.
  if (view['kanban:mock'] === '1') return kanbanFixtureView()
  const err = view[CARDS_ERROR_KEY] ?? ''
  if ((view[CARDS_EMPTY_KEY] === '1' || (view[CARDS_KEY] ?? '') === '') && err === '') return emptyKanbanView()
  let cards: KanbanCard[] = []
  try {
    const parsed = JSON.parse(view[CARDS_KEY] ?? '[]') as unknown
    if (Array.isArray(parsed)) cards = parsed as KanbanCard[]
  } catch {
    cards = []
  }
  const viewOut = kanbanView(cards, kanbanFiltersOf(view), new Date().toISOString(), Number(view[CARDS_MS_KEY] ?? '0') > 100)
  if (err === '') return viewOut
  return { ...viewOut, error: err, stale: view[CARDS_STALE_KEY] === '1' }
}

/**
 * One `mkdb.py cards --json` per dashboard tick. The root file is checked with
 * fs.exists first: a missing ~/.matrixkoder/mk.db (or $MK_HOME/mk.db) never
 * spawns mkdb, which would create the store. The read is started and not
 * awaited, so a slow cards call cannot hold the tick; one read is in flight
 * at most (the 5 s timeout lives in readCards). A read slower than 100 ms
 * still skips every other tick.
 */
let cardsInFlight = false

function applyCardsRead(
  prev: Record<string, string>,
  tick: number,
  lastMs: number,
  got: CardsRead,
): Record<string, string> {
  const had = (prev[CARDS_KEY] ?? '') !== ''
  if (got.error != null) {
    return {
      ...prev,
      [CARDS_TICK_KEY]: String(tick),
      [CARDS_MS_KEY]: String(got.ms),
      [CARDS_ERROR_KEY]: got.error,
      [CARDS_STALE_KEY]: had ? '1' : '0',
      [CARDS_EMPTY_KEY]: '0',
    }
  }
  return {
    ...prev,
    [CARDS_TICK_KEY]: String(tick),
    [CARDS_MS_KEY]: String(got.skipped ? lastMs : got.ms),
    [CARDS_EMPTY_KEY]: got.empty ? '1' : '0',
    [CARDS_ERROR_KEY]: '',
    [CARDS_STALE_KEY]: '0',
    ...(got.skipped ? {} : { [CARDS_KEY]: JSON.stringify(got.cards) }),
  }
}

async function refreshCards($: EngineInterface): Promise<void> {
  // storeHome is MK_HOME when set, otherwise $HOME/.matrixkoder. Both the
  // exists-check and the --home handed to mkdb use that directory: cards()
  // would create a missing root store, so exists() runs first.
  let mkHome = ''
  try {
    mkHome = (await $.env.get('MK_HOME')) || ''
  } catch {
    mkHome = ''
  }
  const userHome = mkHome === '' ? await homeDir($) : null
  const storeHome = mkHome !== '' ? mkHome : (userHome == null ? null : join(userHome, '.matrixkoder'))
  const path = storeHome == null ? '' : join(storeHome, 'mk.db')
  let exists = false
  if (path !== '') {
    try {
      exists = await $.fs.exists(path)
    } catch {
      exists = false
    }
  }
  const view = await read($, viewStateAtom)
  const tick = Number(view[CARDS_TICK_KEY] ?? '0') + 1
  const lastMs = Number(view[CARDS_MS_KEY] ?? '0')
  const run = (argv: readonly string[], init?: { timeoutMs?: number }) => $.process.run([...argv], init)
  // A missing store is synchronous inside readCards (no spawn). Record it
  // before returning so the pane can show "no store yet" on this tick.
  if (storeHome == null || !exists) {
    const got = await readCards(async () => ({ stdout: '', code: 0 }), $.plugin.root, storeHome, exists, tick, lastMs)
    await update($, viewStateAtom, prev => applyCardsRead(prev, tick, lastMs, got))
    return
  }
  // A slow or failed read must not hold this tick. One read in flight.
  if (cardsInFlight) {
    await update($, viewStateAtom, prev => ({ ...prev, [CARDS_TICK_KEY]: String(tick) }))
    return
  }
  cardsInFlight = true
  const started = Date.now()
  void readCards(
    async (argv, init) => {
      const r = await run(argv, init)
      return { stdout: r.stdout, code: r.exitCode }
    },
    $.plugin.root, storeHome, exists, tick, lastMs,
  ).then(async got => {
    const ms = Math.max(got.ms, Date.now() - started)
    await update($, viewStateAtom, prev => applyCardsRead(prev, tick, lastMs, { ...got, ms }))
  }).catch(async () => {
    await update($, viewStateAtom, prev => applyCardsRead(prev, tick, lastMs, {
      cards: [], ms: Date.now() - started, skipped: false, empty: false, error: cardsErrorLine('spawn'),
    }))
  }).finally(() => {
    cardsInFlight = false
  })
}

/** Never throws: a failing load keeps the last good snapshot and flags the error. */
async function refreshSafe($: EngineInterface, options: PluginOptions, cwd: string): Promise<void> {
  const prev = await read($, snapshotAtom)
  try {
    const snap = await loadSnapshot($, cwd)
    await applyStatus($, options, snap)
    if (contentSig(prev) !== contentSig(snap)) {
      await update($, snapshotAtom, () => snap)
    }
    await refreshCards($)
    await refreshPending($, cwd)
  } catch (err) {
    try {
      await update($, snapshotAtom, () => ({ ...prev, error: `cannot read run.json (${String(err).slice(0, 40)})` }))
    } catch {
      // even the atom write failed — nothing more to do
    }
  }
}

/** Polls mk-usage.py for this project: `--providers` from the project yaml when it lists any, else whatever CodexBar has enabled. */
async function pollUsage($: EngineInterface, cwd: string): Promise<void> {
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
    const providers = await projectProviders($, cwd)
    const argv = ['python3', `${$.plugin.root}/scripts/mk-usage.py`, '--once', ...(providers != null ? ['--providers', providers.join(',')] : [])]
    await $.process.run(argv, { timeoutMs: 120_000 })
  } catch {
    // codexbar missing or slow — the refresh ticks still re-read usage.json
  } finally {
    pollInFlight = false
  }
}

/** Sweeps armed herdr lanes for silence. Mirrors pollUsage: in-flight guard, interval guard, failures swallowed. S2.3: `mode: 'solo'` runs no orchestration, so the sweep never fires there. */
async function sweepLanes($: EngineInterface, options: PluginOptions, cwd: string): Promise<void> {
  if (options.mode === 'solo') return
  if (sweepInFlight) return
  let nowMs = 0
  try {
    nowMs = await $.clock.now()
  } catch {
    // clock refused — interval check falls back to lastSweepAt only
  }
  if (nowMs && nowMs - lastSweepAt < SWEEP_INTERVAL_MS) return
  sweepInFlight = true
  lastSweepAt = nowMs || lastSweepAt + SWEEP_INTERVAL_MS
  try {
    const snap = await loadSnapshot($, cwd)
    if (snap.run == null) return // only sweep when an orchestration run is active
    await $.process.run(['python3', `${$.plugin.root}/scripts/mk-lane-event.py`, 'sweep'], { timeoutMs: 120_000 })
  } catch {
    // herdr or mk-lane-event.py missing — the next tick retries
  } finally {
    sweepInFlight = false
  }
}

/** Keeps the bus host alive for the active run: `heartbeat` while `<data>/bus.json`
 *  names a server, else the idempotent `ensure`. Mirrors sweepLanes — an in-flight
 *  guard, every failure swallowed — and writes nothing, so it never blocks a render. */
async function ensureBus($: EngineInterface, options: PluginOptions, cwd: string): Promise<void> {
  if (options.mode === 'solo') return // S2.3: solo runs no orchestration, so it hosts no bus
  if (timerRegistry.cancelRefresh == null) { // orphaned by /clear: the registry knows three cancels, not this one
    busTimers.cancelBus?.()
    busTimers.cancelBus = undefined
    return
  }
  if (busInFlight) return
  busInFlight = true
  try {
    const dataDir = await resolveDataDir($, cwd, await homeDir($))
    if (dataDir == null) return // first-run: the wizard writes the data dir
    // the pane's own snapshot, not a second loadSnapshot: the refresh timer owns
    // reading run.json, and startLoop always populates this before either call site
    const snap = await read($, snapshotAtom) as MkPaneSnapshot
    if (snap.run == null) return // only an active run owns a bus
    const script = `${$.plugin.root}/scripts/mk-bus.py`
    // Root bus (C6): a project with a store heartbeats the one bus under the
    // MK home. File mode (no mk.db) keeps the per-project bus.json.
    let mkHome = ''
    try { mkHome = (await $.env.get('MK_HOME')) || '' } catch { mkHome = '' }
    const userHome = mkHome === '' ? await homeDir($) : null
    const busHome = mkHome !== '' ? mkHome : (userHome == null ? null : join(userHome, '.matrixkoder'))
    const rootBus = busHome != null && (await readText($, join(dataDir, 'mk.db'))) != null
    const busFile = rootBus ? join(busHome as string, 'bus.json') : join(dataDir, 'bus.json')
    if ((await readText($, busFile)) != null) {
      await $.process.run(['python3', script, 'heartbeat', '--data', dataDir, '--run', snap.run.id], { timeoutMs: BUS_TIMEOUT_MS })
      return
    }
    if (await busDisabled($, dataDir)) return
    await $.process.run(['python3', script, 'ensure', '--data', dataDir, '--run', snap.run.id], { timeoutMs: BUS_TIMEOUT_MS })
  } catch {
    // mk-bus.py missing or slow — the next tick retries
  } finally {
    busInFlight = false
  }
}

/** True only for an explicit `{"ok":true,"value":false}` from mk-config.py. Every
 *  other answer leaves the bus on — including the "unknown key" a checkout without
 *  BC's `bus.enabled` row prints — so the host never waits on that row to exist. */
async function busDisabled($: EngineInterface, dataDir: string): Promise<boolean> {
  try {
    const argv = ['python3', `${$.plugin.root}/scripts/mk-config.py`, 'get', 'bus.enabled', '--data', dataDir]
    const r = await $.process.run(argv, { timeoutMs: BUS_TIMEOUT_MS })
    const ans = parseJson(r.stdout) as { ok?: unknown; value?: unknown } | null
    return typeof ans === 'object' && ans !== null && ans.ok === true && ans.value === false
  } catch {
    return false
  }
}

/** The project yaml's usage providers, or null when it lists none (mk-usage.py then picks CodexBar's enabled set). */
async function projectProviders($: EngineInterface, cwd: string): Promise<readonly string[] | null> {
  try {
    const home = await homeDir($)
    const dataDir = await resolveDataDir($, cwd, home)
    if (dataDir == null) return null
    const yaml = await readText($, join(dataDir, 'matrixkoder.yaml'))
    if (yaml == null) return null
    const providers = parseYamlProviders(yaml)
    return providers != null && providers.length > 0 ? providers : null
  } catch {
    return null // an unreadable yaml polls the default set, never blocks the poll
  }
}

// ---------- S3: the Settings tab's live layer (settings-io does the pure
// work; these closures hand it this module's $ — $ never crosses an import) ----------

let settingsInflight = false

/** One view-state key write that never throws (a refused write keeps the
 *  tab's last message — the load/save paths report through these keys). */
async function setViewKey($: EngineInterface, key: string, value: string): Promise<void> {
  try {
    await update($, viewStateAtom, prev => ({ ...prev, [key]: value }))
  } catch {
    // the atom write failed — the tab keeps its last message
  }
}

/** S3: the live Settings load state rides the view-state atom as JSON
 *  strings (the engine's atom keys are a closed registry in types/index.d.ts
 *  — S3 owns no new atom): `settings:live` (the SettingsView, absent until
 *  the first tab open loads it), `settings:migrated` ('1' once migrate ran),
 *  `settings:datadir`. A torn JSON parses to null — the tab reloads. */
function readSettingsState(viewState: Record<string, string>): {
  live: SettingsView | null
  migrated: boolean
  dataDir: string | null
} {
  let live: SettingsView | null = null
  try {
    const raw = viewState['settings:live']
    if (raw != null && raw !== '') {
      const v: unknown = JSON.parse(raw) as unknown
      if (typeof v === 'object' && v !== null && Array.isArray((v as { groups?: unknown }).groups)) {
        // a view cached by older code has no `editable` on its picks: drop it so the tab reloads
        const picks = (v as { picks?: { editable?: boolean }[] }).picks
        if (Array.isArray(picks) && picks.every(p => typeof p.editable === 'boolean')) live = v as SettingsView
      }
    }
  } catch {
    live = null
  }
  return { live, migrated: viewState['settings:migrated'] === '1', dataDir: viewState['settings:datadir'] ?? null }
}

/** S3: load the live Settings rows. MEDIUM 3: `migrate` runs only when
 *  `<dataDir>/matrixkoder.yaml` already exists — a tab open never creates
 *  one; without it neither `migrate` nor `list` runs and the tab says so.
 *  LOW 5: every open re-reads (`list`, globals, About); `migrate` still runs
 *  at most once per session (`settings:migrated` is stored right after it
 *  succeeds — LOW 2 — so a failing `list` never reruns it). Any failure
 *  still stores a live (possibly yaml-less) view, so the tab draws the `⚠`
 *  line over live rows and never the fixture (LOW 1). */
async function ensureSettingsLoaded($: EngineInterface): Promise<void> {
  if (settingsInflight) return
  settingsInflight = true // synchronous: the render fallback and the tab switch race here
  try {
    let seen: Record<string, string> = {}
    try {
      seen = await read($, viewStateAtom)
    } catch {
      return // the atom read failed — the loading line stands
    }
    const st = readSettingsState(seen)
    let cwd: string | null = null
    try {
      cwd = await $.session.cwd()
    } catch {
      cwd = null
    }
    const dataDir = cwd == null ? null : await resolveDataDir($, cwd, await homeDir($))
    let yamlExists = false
    if (dataDir != null) {
      try {
        yamlExists = (await $.fs.stat(join(dataDir, 'matrixkoder.yaml'))).kind === 'file'
      } catch {
        yamlExists = false
      }
    }
    const root = $.plugin.root
    const run: RunFn = (argv, init) => $.process.run(argv, init)
    let fields: YamlFieldIn[] = []
    let loadError = ''
    if (!yamlExists) {
      loadError = 'No matrixkoder.yaml — run /mk setup'
    } else if (dataDir != null) {
      try {
        if (!st.migrated) {
          await runMigrate(run, root, dataDir)
          await setViewKey($, 'settings:migrated', '1')
        }
        fields = (await runListFields(run, root, dataDir)).fields
      } catch (e) {
        loadError = String(e instanceof Error ? e.message : e).slice(0, 120)
      }
    }
    let cfg: { key: string; value: SettingsValue; isLocked: boolean }[] = []
    try {
      cfg = (await $.config.list()).map(r => ({ key: r.key, value: r.value, isLocked: r.isLocked }))
    } catch {
      cfg = [] // the config noun refused — yaml rows still draw, globals drop out
    }
    const about = await loadAbout($, run, root)
    const view = buildSettingsView({ fields, file: '', configRows: cfg, globalDefaults: about.defaults, about })
    try {
      await update($, viewStateAtom, prev => ({
        ...prev,
        ['settings:live']: JSON.stringify(view),
        ['settings:save:msg']: '',
        ...(dataDir != null ? { ['settings:datadir']: dataDir } : {}),
      }))
    } catch {
      // the view did not stick — the next open retries
    }
    await setViewKey($, 'settings:load:error', loadError)
  } catch (e) {
    await setViewKey($, 'settings:load:error', String(e instanceof Error ? e.message : e).slice(0, 120))
  } finally {
    settingsInflight = false
  }
}

/** The brand header's version chip on every tab: plugin.json's version, read
 *  once at session start (the Settings load refines it with the latest tag). */
async function loadBrandVersion($: EngineInterface): Promise<void> {
  try {
    const version = parsePluginManifest(await $.fs.read(`${$.plugin.root}/.claude-plugin/plugin.json`)).version
    await setViewKey($, 'brand:version', version === 'unknown' ? '' : version)
  } catch {
    // unreadable manifest — the header draws without the version chip
  }
}

/** S3.2: the About scope's probed data — installed version from the shipped
 *  plugin.json, latest as the highest git tag in the plugin root, What's new
 *  as the top CHANGELOG.md entry's lines. Each source falls back alone, so a
 *  missing file never blanks the rest. */
async function loadAbout($: EngineInterface, run: RunFn, root: string): Promise<{
  installed: string
  latest: string
  whatsNew: SettingsView['about']['whatsNew']
  defaults: Record<string, SettingsValue>
}> {
  let installed = 'unknown'
  let defaults: Record<string, SettingsValue> = {}
  try {
    const parsed = parsePluginManifest(await $.fs.read(`${root}/.claude-plugin/plugin.json`))
    installed = parsed.version
    defaults = parsed.defaults
  } catch {
    // unreadable manifest — the About card shows unknown, globals take live values
  }
  let latest = installed
  try {
    const r = await run(['git', '-C', root, 'tag', '--sort=-v:refname'], { timeoutMs: 30_000 })
    if (r.exitCode === 0) latest = firstTagLine(r.stdout) || installed
  } catch {
    // git refused — latest stays on installed
  }
  let whatsNew: SettingsView['about']['whatsNew'] = []
  try {
    whatsNew = topChangelogEntry(await $.fs.read(`${root}/CHANGELOG.md`))
  } catch {
    whatsNew = []
  }
  return { installed, latest, whatsNew, defaults }
}

/** S3.1: the tab's save press — yaml sets in order (the first ok:false stops
 *  the rest and its error is the message), then the globals (a {deny} stops
 *  the rest and its reason is shown). Discard writes nothing: the tab clears
 *  locally without calling here. Success clears every edit and leaves
 *  `✓ saved N · backups written first`; any error leaves the edits intact.
 *  LOW 4: a second press while a save runs does nothing. */
let settingsSaveInflight = false

async function saveSettings($: EngineInterface, payload: SavePayload): Promise<void> {
  if (settingsSaveInflight) return
  settingsSaveInflight = true // synchronous: two presses race here
  try {
    await saveSettingsInner($, payload)
  } finally {
    settingsSaveInflight = false
  }
}

async function saveSettingsInner($: EngineInterface, payload: SavePayload): Promise<void> {
  let seen: Record<string, string> = {}
  try {
    seen = await read($, viewStateAtom)
  } catch {
    await setViewKey($, 'settings:save:msg', 'settings not loaded — open the tab first')
    return
  }
  const st = readSettingsState(seen)
  // the yaml leg needs its data dir; a globals-only save runs without one
  // (no yaml rows draw, so saveYaml spawns nothing)
  if (st.live == null || (payload.sets.length > 0 && st.dataDir == null)) {
    await setViewKey($, 'settings:save:msg', 'settings not loaded — open the tab first')
    return
  }
  const run: RunFn = (argv, init) => $.process.run(argv, init)
  const yaml = await saveYaml(run, $.plugin.root, st.dataDir ?? '', payload.sets).catch((e: unknown) => ({
    saved: 0,
    error: String(e instanceof Error ? e.message : e) as string | undefined,
  }))
  if (yaml.error != null) {
    await setViewKey($, 'settings:save:msg', yaml.error)
    return
  }
  let cfg: { key: string; value: SettingsValue; isLocked: boolean }[] = []
  try {
    cfg = (await $.config.list()).map(r => ({ key: r.key, value: r.value, isLocked: r.isLocked }))
  } catch {
    await setViewKey($, 'settings:save:msg', 'config unavailable — yaml saved, globals kept')
    return
  }
  const globals = await saveGlobals(
    async () => cfg,
    async (key, value) => $.config.set({ key, value }),
    payload.globals,
    payload.visibility,
  ).catch((e: unknown) => ({ saved: 0, error: String(e instanceof Error ? e.message : e) as string | undefined }))
  if (globals.error != null) {
    await setViewKey($, 'settings:save:msg', globals.error)
    return
  }
  const n = yaml.saved + globals.saved
  try {
    const drop = new Set(savedEditKeys(st.live))
    const kept = applySavedView(st.live, payload)
    await update($, viewStateAtom, prev => {
      const next: Record<string, string> = {}
      for (const [k, v] of Object.entries(prev)) if (!drop.has(k)) next[k] = v
      next['settings:live'] = JSON.stringify(kept)
      next['settings:save:msg'] = yaml.saved > 0 ? `✓ saved ${n} · backups written first` : `✓ saved ${n}`
      return next
    })
  } catch {
    // the edits stand — the next save retries them
  }
}

/** S3.2: About `c` — re-run the latest-tag check and stamp the check flag
 *  (the tab already sets the flag; the card flips to `checked now` at once). */
async function recheckAbout($: EngineInterface): Promise<void> {
  await setViewKey($, 'settings:about:checked', '1')
  let seen: Record<string, string> = {}
  try {
    seen = await read($, viewStateAtom)
  } catch {
    return
  }
  const st = readSettingsState(seen)
  if (st.live == null) return
  try {
    const r = await $.process.run(['git', '-C', $.plugin.root, 'tag', '--sort=-v:refname'], { timeoutMs: 30_000 })
    if (r.exitCode !== 0) return
    const latest = firstTagLine(r.stdout) || st.live.about.installed
    await update($, viewStateAtom, prev => {
      const cur = readSettingsState(prev)
      if (cur.live == null) return prev
      return { ...prev, ['settings:live']: JSON.stringify({ ...cur.live, about: { ...cur.live.about, latest } }) }
    })
  } catch {
    // git refused — the flag still flipped, the card keeps its last tag
  }
}

// ---------- sections (plain functions: JSX tags resolve to locals by name) ----------

/** The pane as it was before the tab bar (S0.2): first-run, no-run and the
 * per-run sections with Usage/Waiting/Recent below, moved here unchanged. */
function OldTab(els: Els, snap: MkPaneSnapshot, cols: number, compact: boolean) {
  const { Box } = els
  const active = snap.activeRuns
  return (
    <Box flexDirection="column" gap={1}>
      {snap.dataDir === null
        ? FirstRun(els)
        : active.length === 0
          ? NoRun(els, snap)
          : active.map(run => (
            // one Run/Plan/Lanes block per active run, newest first; Usage /
            // Waiting / Recent below are global and render once
            <Box key={run.id} flexDirection="column" gap={1}>
              {RunSection(els, run, compact)}
              {PlanSection(els, run, cols, compact, snap.now)}
              {LanesSection(els, run, cols)}
            </Box>
          ))}
      {UsageSection(els, snap, compact)}
      {snap.run?.waiting && WaitingSection(els, snap.run.waiting)}
      {snap.run && RecentSection(els, (snap.run.recent ?? []).slice(-3).reverse(), compact)}
    </Box>
  )
}

function FirstRun(els: Els) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Text bold={true}>Setup</Text>
      <Text color="cyan" bold={true}>⬡ Welcome to MatrixKoder!</Text>
      <Text>Configuration required for this project:</Text>
      <Text color="yellow" bold={true}>[!] Missing .matrixkoder/matrixkoder.yaml</Text>
      <Text color="cyan" bold={true}>▶ Run /mk setup to launch the wizard</Text>
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

function RunSection(els: Els, run: MkRun, compact: boolean) {
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
        const word = statusWord(task.status, compact)
        const round = task.round && (task.status === 'review' || task.status === 'fix') ? ` r${task.round}` : ''
        const mins = task.status === 'building' || task.status === 'verifying' || task.status === 'stalled' ? elapsedMin(task, nowMs) : null
        const suffix = `${round}${mins != null ? ` ${mins}m` : ''}`
        const sha = cols >= 44 && task.last_commit ? task.last_commit.slice(0, 7) : ''
        // the title yields to the status column and the age (D1.2): truncate
        // with … to the columns left after checkbox + id and status + age + sha
        const budget = cols - (checkbox.length + 1 + task.id.length + 1) - 1 - (word.length + suffix.length + (sha ? 1 + sha.length : 0))
        const title = compact
          ? task.title.slice(0, Math.min(14, Math.max(1, budget))) // §3.7: 14-char title budget
          : truncateTitle(task.title, budget)
        return (
          <Box key={task.id} flexDirection="row" justifyContent="space-between" gap={1}>
            <Box flexDirection="row" gap={1} flexShrink={1}>
              <Text color={checkColor} bold={isActive || isAttention}>{checkbox}</Text>
              <Text bold={true}>{task.id}</Text>
              <Text dimColor={task.status === 'planned'} wrap="truncate-end">{title}</Text>
            </Box>
            <Box flexDirection="row" gap={1} flexShrink={0}>
              <Text color={STATUS_COLOR[task.status] ?? 'text'}>{word}{suffix}</Text>
              {sha && <Text dimColor={true}>{sha}</Text>}
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
  // one row per LANE PATH (D1.1): phases sharing a worktree share one lane row
  // listing their phase ids — parents only, child tasks are never listed. No
  // task carries a lane (early run, lanes not dispatched yet): the live
  // dispatched|building|verifying|fix tasks stand in with agent glyph + model.
  const rows: { key: string; slot: string; head: MkTask; tasks: MkTask[] }[] = []
  if (lanes.length > 0) {
    const byLane = new Map<string, { head: MkTask; tasks: MkTask[] }>()
    for (const t of lanes) {
      const key = t.lane ?? ''
      const group = byLane.get(key) ?? { head: t, tasks: [] }
      group.tasks.push(t)
      byLane.set(key, group)
    }
    for (const [lane, group] of byLane) {
      rows.push({ key: lane, slot: lane.split('/').filter(Boolean).at(-1) ?? lane, ...group })
    }
  } else {
    for (const t of run.tasks.filter(t => LANE_FALLBACK.has(t.status))) {
      rows.push({ key: t.id, slot: '·', head: t, tasks: [t] })
    }
  }
  return (
    <Box flexDirection="column">
      <Text bold={true}>Lanes</Text>
      {rows.length === 0 && <Text dimColor={true}>none live</Text>}
      {rows.map(({ key, slot, head, tasks }) => {
        const status = rollupLaneStatus(tasks)
        const rep = tasks.find(t => t.status === status) ?? head
        const parents = tasks.filter(t => !t.parent)
        const ids = (parents.length > 0 ? parents : tasks).map(t => t.id).join(' ')
        const agent = rep.agent ?? 'agent'
        return (
          <Box key={key} flexDirection="row" justifyContent="space-between" gap={1}>
            <Box flexDirection="row" gap={1} flexShrink={1}>
              <Text dimColor={true}>{slot}</Text>
              <Text color={AGENT_COLOR[agent] ?? 'cyan'} bold={true}>{AGENT_GLYPH[agent] ?? '⌂'} {agent.toUpperCase()}</Text>
              <Text dimColor={true} wrap="truncate-end">{[rep.model, rep.effort].filter(Boolean).join('-')}</Text>
              <Text wrap="truncate-end">{ids}</Text>
            </Box>
            <Box flexDirection="row" gap={1} flexShrink={0}>
              <Text color={SIDE_STATE.has(status) ? 'yellow' : 'cyan'}>{status}</Text>
              {cols >= 44 && rep.last_commit && <Text dimColor={true}>{rep.last_commit.slice(0, 7)}</Text>}
            </Box>
          </Box>
        )
      })}
    </Box>
  )
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
  label: string
  hint: string | null
  pct: number | null
  weekPct: number | null
  reset: string | null
  exhaustedUntil: string | null
  exhausted: boolean
  /** the whole ledger entry is status 'n/a' (provider error) — the only case the 5h column shows n/a */
  na: boolean
  /** true when this row is the agent header that only labels the group */
  header: boolean
  sub: { name: string; pct: number | null; weekPct: number | null; reset: string | null; exhaustedUntil: string | null; exhausted: boolean; status?: string; error?: string; hint?: string | null }[]
}

const POOL_HINT: Record<string, string> = {
  zai: 'set the z.ai key in CodexBar',
  ollama: 'sign in at ollama.com',
}

function usageRows(snap: MkSnapshot, compact: boolean): UsageRow[] {
  const agents = snap.usage?.agents ?? {}
  const names = snap.projectAgents ?? [...AGENT_ORDER, ...Object.keys(agents).filter(a => !(AGENT_ORDER as readonly string[]).includes(a))]
  const now = snap.now
  const isExhausted = (until: string | null | undefined): boolean => {
    const t = Date.parse(until ?? '')
    return Number.isFinite(t) && now > 0 && t > now
  }
  // a setup hint only for a pool with no numbers yet: once the plan reports usage, its bars show
  const poolHint = (name: string, error?: string, hasData = false): string | null => {
    if (hasData) return null
    if (POOL_HINT[name]) return POOL_HINT[name]
    if (error) return error
    return null
  }
  return names.flatMap(name => {
    const a = agents[name]
    if (!a) {
      // no yaml scoping: an agent without a ledger entry is simply not shown, as it always was
      if (snap.projectAgents == null) return []
      // the project enables it: one dim hint row instead of nothing (opencode's quota needs the zai provider)
      return [{
        name,
        label: name,
        hint: name === 'opencode' ? 'no usage — enable zai in CodexBar' : 'no usage yet',
        pct: null, weekPct: null, reset: null, exhaustedUntil: null, exhausted: false, na: false, header: false, sub: [],
      } as UsageRow]
    }
    const pools = Object.entries(a.pools ?? {})
    // claude keeps its main row = default pool and fable as a sub-row
    const configured = PRIMARY_POOL[name] ?? 'default'
    const primary = name === 'claude' ? 'default' : (pools.some(([pname]) => pname === configured) ? configured : pools[0]?.[0])
    const subs = pools.map(([pname, p]) => ({
      name: compact ? pname.slice(0, 6) : pname,
      pct: p.five_hour?.used_pct ?? null,
      weekPct: p.weekly?.used_pct ?? null,
      reset: p.five_hour?.resets_at ?? p.weekly?.resets_at ?? null,
      exhaustedUntil: p.exhausted_until ?? null,
      exhausted: isExhausted(p.exhausted_until),
      status: (p as { status?: string }).status,
      error: (p as { error?: string }).error,
      hint: poolHint(compact ? pname.slice(0, 6) : pname, (p as { error?: string }).error, p.five_hour != null || p.weekly != null),
    }))
    // claude with sub-pools: single row layout, main row aggregates all models
    if (name === 'claude' && pools.length > 1) {
      const sub = subs.filter(s => s.name !== primary)
      return [{
        name,
        label: !compact ? 'claude (all)' : name,
        hint: null,
        pct: a.five_hour?.used_pct ?? null,
        weekPct: a.weekly?.used_pct ?? null,
        reset: a.five_hour?.resets_at ?? a.weekly?.resets_at ?? null,
        exhaustedUntil: a.exhausted_until ?? null,
        exhausted: isExhausted(a.exhausted_until),
        na: a.status === 'n/a',
        header: false,
        sub: sub.map(s => ({ ...s, hint: poolHint(s.name, s.error, s.pct != null || s.weekPct != null) })),
      } as UsageRow]
    }
    // every other multi-pool agent: header row + one sub-row per pool
    if (pools.length > 1) {
      return [
        {
          name,
          label: name,
          hint: null,
          pct: null, weekPct: null, reset: null, exhaustedUntil: null, exhausted: false, na: false, header: true, sub: [],
        } as UsageRow,
        ...subs.map(s => ({
          name: `${name}:${s.name}`,
          label: `  └ ${compact ? s.name.slice(0, 6) : s.name}`,
          hint: poolHint(s.name, s.error, s.pct != null || s.weekPct != null),
          pct: s.pct,
          weekPct: s.weekPct,
          reset: s.reset,
          exhaustedUntil: s.exhaustedUntil,
          exhausted: s.exhausted,
          na: s.status === 'n/a',
          header: false,
          sub: [],
        } as UsageRow)),
      ]
    }
    // single-pool agent
    return [{
      name,
      label: name,
      hint: null,
      pct: a.five_hour?.used_pct ?? null,
      weekPct: a.weekly?.used_pct ?? null,
      reset: a.five_hour?.resets_at ?? a.weekly?.resets_at ?? null,
      exhaustedUntil: a.exhausted_until ?? null,
      exhausted: isExhausted(a.exhausted_until),
      na: a.status === 'n/a',
      header: false,
      sub: [],
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
  // a missing 5h window is '—' like the week column; 'n/a' only for a whole entry the provider could not answer
  const pctCol = (pct: number | null, na: boolean): string => (pct == null ? rightCol(na ? 'n/a' : '—', NUM_W) : rightCol(`${Math.round(pct)}%`, NUM_W))
  const weekCol = (pct: number | null): string => (pct == null ? rightCol('—', NUM_W) : rightCol(`${Math.round(pct)}%`, NUM_W))
  const resetCol = (iso: string | null): string => rightCol(resetStr(iso, snap.now, compact), RESET_W)

  const row = (keyId: string, name: string, nameColor: string | undefined, boldName: boolean, pct: number | null, weekPct: number | null, resetIso: string | null, exhausted: boolean, exhaustedUntil: string | null, na: boolean, isHeader: boolean) => {
    const color = pctColor(pct, exhausted)
    const resetIsoShown = exhausted && exhaustedUntil ? exhaustedUntil : resetIso
    return (
      <Box key={keyId} flexDirection="row" gap={0}>
        <Text color={nameColor} bold={boldName}>{leftCol(name, NAME_W)}</Text>
        {isHeader ? (
          <Text dimColor={true}>{' '.repeat(barW + NUM_W + (compact ? 0 : NUM_W) + RESET_W)}</Text>
        ) : (
          [
            <Text key="bar" color={color} bold={exhausted}>{bar(pct, barW)}</Text>,
            <Text key="pct" color={pct == null ? 'gray' : color} bold={exhausted}>{pctCol(pct, na)}</Text>,
            ...(compact ? [] : [<Text key="week" dimColor={true}>{weekCol(weekPct)}</Text>]),
            <Text key="reset" dimColor={!exhausted} color={exhausted ? 'red' : undefined} bold={exhausted}>{resetCol(resetIsoShown)}</Text>,
            ...(exhausted ? [<Text key="ex" color="red" bold={true}>⊘</Text>] : []),
          ]
        )}
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
          {r.hint != null
            ? <Text dimColor={true}>{leftCol(r.label, NAME_W)}{r.hint}</Text>
            : row(r.name, leftCol(r.label, NAME_W), AGENT_COLOR[r.name] ?? 'cyan', r.exhausted, r.pct, r.weekPct, r.reset, r.exhausted, r.exhaustedUntil, r.na, r.header)}
          {r.sub.map(p => (
            p.hint != null
              ? <Text key={`${r.name}:${p.name}`} dimColor={true}>{leftCol(`  └ ${compact ? p.name.slice(0, 6) : p.name}`, NAME_W)}{p.hint}</Text>
              : row(`${r.name}:${p.name}`, `  └ ${compact ? p.name.slice(0, 6) : p.name}`, undefined, false, p.pct, p.weekPct, p.reset, p.exhausted, p.exhaustedUntil, false, false)
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
