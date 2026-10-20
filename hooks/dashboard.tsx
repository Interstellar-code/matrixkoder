// MatrixKoder dashboard pane (design §3/§6): /mk (or /mk open|close|setup|init)
// drives it, session.start auto-opens it when allowed and runs the state loop;
// the tree renders the snapshot atoms with the 46-col base layout, 36-col
// compact and inline variants. All $-bound loading lives here: $ may not cross
// an import; the pure data-dir math is in data.ts for wake/tools to reuse.
import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, On, PluginOptions, RenderElement } from 'claude-code'

// the per-surface element table $.ui.resolve(e) answers (every surface's has Box/Text)
type Els = Elements[keyof Elements]
import type { MkDashTab, MkFileAct, MkImprove, MkImproveEntry, MkImproveFlow, MkImproveStep, MkRun, MkRunIndexEntry, MkSnapshot, MkStatusMemory, MkTask } from '../types'
import {
  AGENT_ORDER, PANE_ID, EMPTY_SNAPSHOT, basename, coerceIndex, coerceRun, coerceUsage, join,
  mainCheckout, mkSnapshot, normalizePath, orderAgents, parentOf, parseGitdir, parseJson, parseYamlAgents, parseYamlProviders,
  sanitizeText, timerRegistry,
} from './data'
import type { MkPaneSnapshot } from './data'
import { beginStoreTick, endStoreTick, globalDirOf, homeDir, paths, projectYamlPath, resolveDataDir, resetStoreMemo } from './paths'
import {
  DRAFT_EMPTY, DRAFT_SEND_FAILED, DRAFT_SYSTEM, IMPROVE_EMPTY, IMPROVE_FAILED, IMPROVE_FILL_FAILED, IMPROVE_MODEL, QUESTIONS_SYSTEM, REWRITE_SYSTEM,
  answeredPairs, canAccept, contextBlock, draftContext, draftPrompt, finalText, modeOf, newImproveFlow, questionsPrompt, readQuestions, readRewrite, rewritePrompt,
} from './improve-core'
import { NARROW, STATUS_GLYPH } from './tabs/ui'
import { stageWord, STAGE_TOTAL } from './tabs/header'
import type { RunHeaderData } from './tabs/header'
import { Card, Chip, Section, Segment } from './tabs/kit'
import { PALETTE, ROLE, themeText, withTheme } from './tabs/palette'
import { withRaster } from './tabs/raster'
import { BUS_UNKNOWN, EventsTab, EVENTS_MAX, eventsView } from './tabs/events'
import type { BusViewData, EventItem, EventsView, LaneActivityItem } from './tabs/events'
import { EVENTS_FIXTURE } from './tabs/events.fixture'
import { LanesTab } from './tabs/lanes'
import type { LaneData, LanesView } from './tabs/lanes'
import { OverviewTab, UsageCard } from './tabs/overview'
import type { OverviewView } from './tabs/overview'
import { PlanTab } from './tabs/plan'
import type { PlanView } from './tabs/plan'
import { PlansTab, planActionLine } from './tabs/plans'
import type { PlansView } from './tabs/plans'
import { planFromReply, plansErrorLine, plansFromOverview } from './tabs/plans-map'
import { PlanKanbanTab, planKanbanView } from './tabs/plankanban'
import { PlanTaskPanel, findPlanTask } from './tabs/plantask'
import { SettingsTab } from './tabs/settings'
import type { SettingsValue, SettingsView } from './tabs/settings'
import { FilesTab } from './tabs/filetree'
import type { FilesTabUi, FilesView } from './tabs/filetree'
import { diffWalk, gitStatusMap, ignoredDirs, pickRoot, rootsOf, treeRows, walkRoot } from './tabs/filetree-data'
import type { RootItem, SortSpec, Walk } from './tabs/filetree-data'
import { liveActivity, recordActivity } from './filetree-activity'
import {
  applySavedView, buildSettingsView, firstTagLine, runListFields, parsePluginManifest, runMigrate, saveGlobals, saveYaml,
  savedEditKeys, topChangelogEntry,
} from './settings-io'
import type { RunFn, SavePayload, YamlFieldIn } from './settings-io'
import { UsageTab } from './tabs/usage'
import type { FloorCtl, FloorSetting, Quota, QuotaRow, UsageView } from './tabs/usage'
import { USAGE_FIXTURE } from './tabs/usage.fixture'
import { WaitingTab, waitingAsks, waitingView, readPending, askView, runWaitView, emptyWaitingView } from './tabs/waiting'
import type { PendingAsk, RunWait } from './tabs/waiting'
import { projectStoreDir, storeGroups } from './tabs/plans-map'
import { TodoTab } from './tabs/todo'
import { ImproveFooter, ImproveTab } from './tabs/improve'
import type { ImproveKind, ImproveView } from './tabs/improve'
import type { TodoFilter, TodoItem, TodoView } from './tabs/todo'
import { chipText, toastsFor } from './status'

const fileActivityAtom = atom(
  { plugin: 'matrixkoder', key: 'fileActivity' } as const,
  [] as readonly MkFileAct[],
)

// atoms are file-local consts (the scan reads this file's own consts); the
// plugin+key address is what makes them one value across files
const snapshotAtom = atom({ plugin: 'matrixkoder', key: 'snapshot' } as const, EMPTY_SNAPSHOT)
const statusMemoryAtom = atom({ plugin: 'matrixkoder', key: 'taskStatuses' } as const, {
  statuses: {},
  waiting: false,
  loaded: false,
} satisfies MkStatusMemory)
const paneOpenAtom = atom({ plugin: 'matrixkoder', key: 'paneOpen' } as const, false)
const dashTabAtom = atom({ plugin: 'matrixkoder', key: 'dashTab' } as const, 'overview')
// C12: the tabs' local view state (folded phases, revealed options). The tabs
// reach it through the `ui` handle the Pane render hands them; only an onPress
// writes it, never a render.
const viewStateAtom = atom({ plugin: 'matrixkoder', key: 'viewState' } as const, {} as Record<string, string>)
// the open improvement and the prompt text Accept replaced: ONE value the footer card and the Improve tab both draw
const improveAtom = atom({ plugin: 'matrixkoder', key: 'improve' } as const, { undo: null, written: null, flow: null } as MkImprove)
const improveHistoryAtom = atom({ plugin: 'matrixkoder', key: 'improveHistory' } as const, [] as readonly MkImproveEntry[])

const COLUMNS = 46
const REFRESH_MS = 5000
const USAGE_POLL_DEFAULT_MIN = 5
const USAGE_POLL_MIN_MIN = 1 // a faster poll with the 120 s timeout would stack runs
const POLL_MIN_INTERVAL_MS = 60_000
/** The cold-start usage poll waits this long, so it does not stack onto the first load. */
const FIRST_POLL_MS = 5000
const SWEEP_INTERVAL_MS = 120_000
// The bus host (mk-bus.py): one idempotent `ensure` (or `heartbeat`) every 15 s.
// Its own start window is a few seconds of flock + probe, so the spawn timeout
// stays short — a slow one is swallowed and the next tick retries.
const BUS_INTERVAL_MS = 15_000
const BUS_TIMEOUT_MS = 10_000
// The store read (mkdb.py run-snapshot): one python3 spawn per refresh tick (5 s). The
// reader opens the store mode=ro, so it is a query, not a lock; the timeout only covers a
// wedged spawn, and a refused one falls back to the file views.
const STORE_READ_MS = 5000
const PRIMARY_POOL: Record<string, string> = { agy: 'gemini', claude: 'default' }
// the pane's glyph/colour vocabularies moved to tabs/ui.ts: one copy for the
// pane and the tab modules (T1/T2)
/** the tab bar (S0.2): order and hotkeys are the plan's — o Overview, l Lanes,
 * p Plan, u Usage, e Events, w Waiting. V0.4 dropped `short`: the bar
 * carries one scheme at every width — the active tab shows its full word, the
 * others the hotkey letter plus their count (`l 4`, `p 3/8`, `w !1`). */
const TABS: readonly { key: MkDashTab; hotkey: string; label: string }[] = [
  { key: 'overview', hotkey: 'o', label: 'Overview' },
  { key: 'lanes', hotkey: 'l', label: 'Lanes' },
  { key: 'plan', hotkey: 'p', label: 'Plan' },
  { key: 'usage', hotkey: 'u', label: 'Usage' },
  { key: 'files', hotkey: 'f', label: 'Files' },
  { key: 'events', hotkey: 'e', label: 'Events' },
  { key: 'waiting', hotkey: 'w', label: 'Waiting' },
  { key: 'todo', hotkey: 't', label: 'Todo' },
  { key: 'improve', hotkey: 'i', label: 'Improve' },
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
/** The first load of a start, in the background: the 5 s timer is its backstop. */
let firstLoad: Promise<void> = Promise.resolve()
/** The cold-start usage one-shot (`$.clock.after`); cancelled with the other timers. */
let cancelFirstPoll: (() => void) | null = null
/** The poll canceller this loop armed: the interval plus the cold-start one-shot. */
let cancelPoll: (() => void) | null = null
/** loadSnapshot's answer for the start: true when usage.json was missing at the load. */
let usageMissingAtLoad = false
/** true once the start's own load has recorded that answer (later ticks leave it alone). */
let firstLoadDone = false
let sweepInFlight = false
let lastSweepAt = -Infinity
let busInFlight = false
const busTimers = timerRegistry
let lastChipText: string | null | undefined // undefined = never set; skip ui.status when unchanged

let filesNow = 0
let lastFilesDraw = 0
let filesShown = false
let filesSlotsStamp: number | null = null
const filesWalkCache = new Map<string, Walk>()
/** git status marks per walked root, refreshed with each walk */
const filesGitCache = new Map<string, Map<string, string>>()
/** folders the user pressed scan on: each is listed one level deep on every walk */
const filesScanned = new Set<string>()
let filesWalkInFlight = false
let filesSkipOnce = false
/** when the last walk+git of the Files tick started (the engine clock): one per FILES_WALK_MS window */
let lastFilesWalk = 0
/** whether the last Files render drew any activity icon: its fade must be drawn, so the beat keeps
 *  redrawing until a render shows the faded row. An icon-free pane leaves this false. */
let filesIconsDrawn = false
let filesCurrentRoot: string | null = null
let filesChosenRoot: string | null = null
let filesRenderWalk = true // a render may start the first walk only when the pane opened straight onto Files
let lastRootWalked: string | null = null
let filesBumpPending = false
const filesMainCache = new Map<string, Promise<string>>()

let filesSessionCwd = ''

export function registerDashboard(on: On, options: PluginOptions): void {
  on('session.start', async ($, e, next) => {
    resetStoreMemo() // a new session must not answer from the previous one's tick
    filesSessionCwd = e.cwd ?? ''
    filesNow = 0
    lastFilesDraw = 0
    filesShown = false
    filesSlotsStamp = null
    slotsInflight = false
    filesWalkCache.clear()
    filesWalkInFlight = false
    filesSkipOnce = false
    filesCurrentRoot = null
    filesChosenRoot = null
    filesRenderWalk = true
    lastRootWalked = null
    filesBumpPending = false
    filesMainCache.clear()
    await registerMk($)
    if (options.autoOpen !== false) {
      try {
        const opened = await $.ui.open({ id: PANE_ID, title: 'MatrixKoder', columns: COLUMNS })
        await update($, paneOpenAtom, () => opened.isPlaced)
      } catch {
        // the engine seats the pane itself; a refusal is not fatal
      }
    }
    void startLoop($, options, e.cwd).catch(() => {}) // arms the timers now; the first load runs in the background
    const out = next(e) // the engine continues at once: the pane is usable before any data has loaded
    // Neither the brand version nor the opening tab waits on, or is produced by, the first load.
    await loadBrandVersion($)
    void loadImproveHistory($) // the saved list should not hold the start up
    await resetImproveWork($)
    // S2.3: `defaultTab` picks the opening tab — a known visible tab stands,
    // anything else keeps the Overview default.
    const opening = typeof options.defaultTab === 'string'
      ? visibleTabs(options.hiddenTabs).find(t => t.key === options.defaultTab)
      : undefined
    if (opening != null) {
      try {
        await update($, dashTabAtom, () => opening.key)
      } catch {
        // the atom write failed — the pane keeps the Overview default
      }
    }
    // The hook's returned promise must NOT wait for data: the engine awaits it, and the pane
    // draws from stored atoms meanwhile. The load is `.catch`ed in startLoop and the 5 s tick
    // is its backstop, so nothing here awaits `firstLoad`.
    // ensureBus reads the snapshot atom and needs a run to find one, so it runs once the first
    // load has landed — as a side task, never as something the hook waits on.
    void firstLoad.then(() => ensureBus($, options, e.cwd)).catch(() => {})
    return out
  })

  // /mk is a per-session declaration. session.start does not fire on /clear or on a
  // reload of unchanged modules, so the first turn of any session declares it too.
  on('turn.start', async ($, e, next) => {
    if (timerRegistry.mkRegistered !== true) await registerMk($)
    void rearmLoop($, options)
    return next(e)
  })

  on('command.run', { command: 'mk' }, async ($, e, next) => {
    await rearmLoop($, options)
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
    void rearmLoop($, options)
    const out = await next(e)
    await setViewKey($, 'settings:focus', e.element ?? '')
    return out
  })

  on('ui.render', { component: 'Pane', requestId: PANE_ID }, async ($, e) => {
    void rearmLoop($, options) // an open pane after /clear restarts its own loop, no /mk needed
    const rawEls = $.ui.resolve(e)
    const snap = await read($, snapshotAtom) as MkPaneSnapshot
    const dashTab = await read($, dashTabAtom).then(t => (TABS.some(x => x.key === t) ? t : 'overview')) // C2: an unknown stored tab is Overview
    const viewState = await read($, viewStateAtom)
    const improve = await read($, improveAtom)
    const improveHistory = await read($, improveHistoryAtom)
    const pane = waitingPane(viewState)
    const cols = e.props.bodyColumns || COLUMNS
    const compact = cols < NARROW
    // S2.3: `raster: false` hands the tabs a table without Raster, so every
    // pickBar falls back to its eighth-block bar.
    const rawTabEls = withRaster(rawEls, options.raster)
    const tabs = visibleTabs(options.hiddenTabs)
    // the run header comes from the live snapshot on every tab
    const header = realRunHeader(snap)
    // first run (no data dir): the setup gate shows on every tab; only Settings keeps its body
    const needsSetup = snap.dataDir === null
    // C12: the tabs' view-state handle. `get` reads the atom's snapshot; `set`
    // writes it and is only ever called from an onPress (C10). `save`/`recheck`
    // are the Settings tab's live wires (S3): the ordered write and the About
    // re-check, both fire-and-forget from a press.
    const ui: TabUi = {
      get: key => viewState[key],
      set: (key, value) => { void update($, viewStateAtom, prev => ({ ...prev, [key]: value })) },
      save: payload => { void saveSettings($, payload) },
      recheck: () => { void recheckAbout($) },
      submit: text => { void $.prompt.submit({ text }) },
      planAct: (kind, id) => { void planAction($, kind, id) },
      todoAct: (kind, arg) => { void todoAction($, kind, arg) },
      improveAct: (kind, a, b) => { void improveAction($, kind, a, b) },
    }
    const improveView: ImproveView = { mock: false, undo: improve.undo, flow: improve.flow, history: improveHistory, menu: improve.menu === true }
    // S3: the live Settings view once loaded. Before that a one-line
    // skeleton draws — never the fixture's mock rows (LOW 1). A tab opened
    // straight onto settings (defaultTab) loads here while nothing is stored;
    // every other open reloads from the tab switch (LOW 5) — a render must
    // never start a refresh on its own, or the refresh tick would re-list.
    if ((dashTab === 'lanes' || dashTab === 'files') && Date.now() - Number(viewState['lanes:slots-at'] ?? '0') > 30_000) void loadLaneSlots($)
    filesShown = dashTab === 'files'
    if (dashTab !== 'files') filesIconsDrawn = false // the pulse is a Files-tab animation only
    // the Files reads below may find the plugin unloaded when a render lands during teardown: they settle to empty, never reject
    if (dashTab === 'files') {
      if (!filesNow) filesNow = await Promise.resolve($.clock.now()).catch(() => 0)
      lastFilesDraw = filesNow
    }
    const fileAct = dashTab === 'files' ? await read($, fileActivityAtom).catch(() => undefined) : undefined
    let cwd = ''
    let mainCheckoutPath = ''
    if (dashTab === 'files') {
      cwd = await filesCwd($).catch(() => '')
      mainCheckoutPath = cwd ? await cachedMainCheckout($, cwd).catch(() => '') : ''
    }
    const settingsState = readSettingsState(viewState)
    const settingsLive = settingsState.live
    // Usage reads the reserve floors from the same live settings, so it loads them too
    if ((dashTab === 'settings' || dashTab === 'usage') && settingsLive == null && !settingsInflight && (viewState['settings:load:error'] ?? '') === '') {
      void ensureSettingsLoaded($)
    }

    // Matrix theme across the whole pane: palette.ts swaps PALETTE/ROLE for this synchronous build
    // docked, the body is a window the engine scrolls over the tree: the footer is drawn at the window's last rows instead of the tree's
    const docked = e.props.placement === 'dock' && e.props.scroll != null
    const pinnedRows = pinnedFooterRows(snap, improve.menu === true)
    return withTheme('matrix', () => {
      // every Text in the pane gets the Matrix greens (a plain or dim Text would draw terminal grey)
      const els = themeText(rawEls)
      const tabEls = themeText(rawTabEls)
      const { Box, Text } = els
      return (
      // minHeight: paint the Matrix ground down the whole dock, not just under the content
      <Box flexDirection="column" width={cols} minHeight={e.props.placement === 'dock' ? e.props.scroll?.bodyRows : undefined} gap={1} backgroundColor={PALETTE.bg}>
        {/* Matrix code rain behind every tab — first child, so everything after paints over it */}
        {e.props.placement === 'dock' && codeRain(els, cols, e.props.scroll?.bodyRows ?? 40, Date.now())}
        {e.props.placement === 'inline' && (
          <Box borderStyle="round" borderColor={ROLE.warn.fg} paddingX={1}>
            <Text color={ROLE.warn.fg}>Run /tui fullscreen to dock this panel on the right.</Text>
          </Box>
        )}
        {snap.error && (
          <Text color={ROLE.warn.fg} bold={true}>⚠ {snap.error}</Text>
        )}
        {/* one header for every tab: the brand block, then the run block on every tab but Settings */}
        {BrandHeader(els, cols, settingsLive?.about.installed ?? viewState['brand:version'] ?? '', settingsLive?.about.latest ?? '', String(options.mode ?? 'orchestrator'))}
        {dashTab !== 'settings' && header != null && RunHeader(els, header, cols)}
        {/* V1.4: the engine has no per-side borders, so the header and tab bar
            read as one framed block above the tab's Cards through this rule.
            The bar and the rule share a gap-less column so the rule sits
            directly under the bar; the pane's own gap keeps the space after it. */}
        <Box flexDirection="column">
          {TabBar(els, $, dashTab, tabs, compact)}
          <Text color={PALETTE.line}>{'─'.repeat(Math.max(0, cols))}</Text>
        </Box>
        {needsSetup && FirstRun(els)}
        {needsSetup && dashTab !== 'settings'
          ? null
          : dashTab === 'overview'
            ? (snap.run == null ? NoRun(tabEls, snap, cols - 2) : OverviewTab(tabEls, liveOverviewView(snap), cols - 2, ui))
            : dashTab === 'lanes'
              ? withViewState(LanesTab, tabEls, { ...liveLanesView(snap), slots: laneSlotsOf(viewState) }, cols, ui)
              : dashTab === 'plan'
                ? PlanPane(tabEls, viewState, cols, ui)
            : dashTab === 'settings'
              ? settingsLive != null
                ? SettingsTab(tabEls, settingsLive, cols, ui)
                : SettingsLoading(els, cols, viewState['settings:load:error'] ?? '')
                  : dashTab === 'usage'
                    ? withViewState(UsageTab, tabEls, liveUsageView(snap, typeof options.usagePollMin === 'number' && options.usagePollMin > 0 ? Math.max(USAGE_POLL_MIN_MIN, options.usagePollMin) : USAGE_POLL_DEFAULT_MIN, settingsLive), cols, ui)
                    : dashTab === 'files'
                      ? FilesPane($, tabEls, cols, viewState, fileAct, cwd, mainCheckoutPath)
                    : dashTab === 'events'
                      ? withViewState(EventsTab, tabEls, liveEventsView(snap, viewState), cols, ui)
                        : dashTab === 'todo'
                          ? TodoPane($, tabEls, viewState, cols, ui)
                        : dashTab === 'improve'
                          ? ImproveTab(tabEls, improveView, cols, ui)
                        : pane.src === 'live'
                          ? waitingAsks(tabEls, pane.asks, cols, new Date().toISOString(), ui)
                          : withViewState(WaitingTab, tabEls, waitingPaneView(pane, snap.run?.waiting ?? null), cols, ui)}
        {dashTab !== 'settings' && !needsSetup && <Box flexGrow={1} />}
        {/* docked: the proposal card ends the content; the rule, chips and counts are pinned to the window's last rows */}
        {dashTab !== 'settings' && !needsSetup && !docked && PaneFooter(els, snap, openWaiting(pane.asks, snap.run), cols, improveView, ui, dashTab !== 'improve')}
        {dashTab !== 'settings' && !needsSetup && docked && dashTab !== 'improve' && improveView.flow != null && ImproveFooter(els, improveView, cols, ui, true, false)}
        {dashTab !== 'settings' && !needsSetup && docked && <Box height={pinnedRows} flexShrink={0} />}
        {dashTab !== 'settings' && !needsSetup && docked && (
          <Box position="absolute" flexDirection="column" top={Math.max(0, (e.props.scroll?.offset ?? 0) + (e.props.scroll?.bodyRows ?? 0) - pinnedRows)} left={0} width={cols} backgroundColor={PALETTE.bg}>
            {PaneFooter(els, snap, openWaiting(pane.asks, snap.run), cols, improveView, ui, false)}
          </Box>
        )}
      </Box>
      )
    })
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
  /** Plan actions: the Waiting tab's prompt-submit path. The pane never writes state. */
  submit?: (text: string) => void
  /** archive | unarchive | delete: runs mk-plan.py directly (see planAction). */
  planAct?: (kind: string, id: string) => void
  /** Todo tab presses: runs `mkdb.py todo <verb>` (see todoAction). */
  todoAct?: (kind: 'add' | 'toggle' | 'delete' | 'undo' | 'clear-done', arg?: string | number) => void
  /** Improve presses (footer card and tab): see improveAction. */
  improveAct?: (kind: ImproveKind, a?: string | number, b?: string) => void
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
  await update($, improveAtom, prev => (prev.menu === true ? { ...prev, menu: false } : prev)) // the drop-up closes on a tab switch
  if (tab === 'settings') void ensureSettingsLoaded($)
  if (tab === 'todo') void kickTodos($) // the first list should not wait for the 5 s tick
  if (tab === 'plan') void kickPlans($) // the gated tab loads on open, not 5 s later
  if (tab === 'waiting') void kickPending($) // a retry now, whatever the sticky-down backoff says
  if (tab !== 'files') {
    filesSlotsStamp = null
    filesNow = 0
  } else {
    // a render may not write state, so the lane slots load from here and from the rain tick
    filesNow = await $.clock.now()
    filesSlotsStamp = filesNow
    void loadLaneSlots($).catch(() => {})
    filesRenderWalk = false // from here the walks start from presses and ticks, where state writes are allowed
    void startFilesWalk($).catch(() => {}) // a press may write state, a render may not: the first paint does not wait for the tick
  }
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
        <Text key={`next-${String(i)}`} color={PALETTE.mut}>{line}</Text>
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
function TabBar(els: Els, $: EngineInterface, dashTab: MkDashTab, tabs: readonly { key: MkDashTab; hotkey: string; label: string }[], compact: boolean) {
  const { Box, Button } = els
  return (
    <Box flexDirection="row" flexWrap="wrap">
      {tabs.map(t => {
        const active = dashTab === t.key
        // the open tab sits on the tint like the Settings sub-strip's picked scope; it stays a press (its key and hotkey)
        const btn = (
          <Button
            key={`dash-tab-${t.key}`}
            hotkey={t.hotkey}
            // no counts on the tab buttons (3/8, 4, !1 read as noise); the hotkey prefix stays
            label={active || !compact ? t.label : t.hotkey}
            plain={true}
            dimColor={active ? undefined : true}
            onPress={() => { void switchTab($, t.key) }}
          />
        )
        return <Box key={`dash-tab-${t.key}-box`} backgroundColor={active ? ROLE.live.tint : PALETTE.btn} paddingX={1} marginRight={1}>{btn}</Box>
      })}
    </Box>
  )
}

/** The run header from the real snapshot (the newest run); no run, no
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

/** Declares /mk for this session; safe to call again (a re-declare replaces it). */
async function registerMk($: EngineInterface): Promise<void> {
  try {
    await $.command.register({ name: 'mk', description: 'Toggle the MatrixKoder dashboard pane', argumentHint: '[open|close|setup|init]' })
    timerRegistry.mkRegistered = true
  } catch {
    // command registration refused — /mk stays inert, the pane still renders
  }
}

let rearmInFlight = false

/** /clear fires session.end (timers cancelled) but never session.start, so the pane kept
 *  drawing frozen data until /mk. /mk, a pane render and a pane focus all call this; it
 *  restarts the loop only when it is dead, once at a time. */
export async function rearmLoop($: EngineInterface, options: PluginOptions): Promise<void> {
  if (timerRegistry.cancelRefresh != null || rearmInFlight) return
  rearmInFlight = true
  try {
    const cwd = await $.session.cwd()
    // Same hand-over as session.start: the timers are armed at once and the first load
    // runs in the background; the 5 s tick is its backstop. Nothing here waits for data.
    // The promise is left un-awaited by design, so it is caught here: a refused clock or
    // atom during teardown must not surface as an unhandled rejection.
    void startLoop($, options, cwd).catch(() => {})
    await loadBrandVersion($)
    void ensureBus($, options, cwd) // the bus needs no /clear either
  } catch {
    // no cwd — the pane keeps its last snapshot
  } finally {
    rearmInFlight = false
  }
}

/** Arms the refresh, usage-poll and lane-sweep timers for `cwd`, then loads the first snapshot. */
async function startLoop($: EngineInterface, options: PluginOptions, cwd: string): Promise<void> {
  filesNow = 0
  lastFilesDraw = 0
  filesShown = false
  filesSlotsStamp = 0
  lastFilesWalk = 0
  filesIconsDrawn = false
  filesWalkCache.clear()
  filesWalkInFlight = false
  filesSkipOnce = false
  filesCurrentRoot = null
  filesChosenRoot = null
  filesRenderWalk = true
  lastRootWalked = null
  filesBumpPending = false
  filesMainCache.clear()
  const min = typeof options.usagePollMin === 'number' && options.usagePollMin > 0
    ? Math.max(USAGE_POLL_MIN_MIN, options.usagePollMin)
    : USAGE_POLL_DEFAULT_MIN
  pendingDownSkips = 0 // a new loop (start or /mk) retries the bus at once (P3.3)
  // timers first: a throwing first load must never leave the loop dead
  // a cancel may come back as a plain function or a { cancel } handle (mock clock)
  const arm = (cancel: unknown): (() => void) =>
    typeof cancel === 'function' ? (cancel as () => void) : () => { (cancel as { cancel?: () => void }).cancel?.() }
  timerRegistry.cancelRefresh?.()
  timerRegistry.cancelRefresh = arm($.clock.every(REFRESH_MS, () => refreshSafe($, options, cwd)))
  cancelPoll?.() // the previous loop's interval AND its cold-start one-shot
  const pollInterval = arm($.clock.every(Math.round(min * 60_000), () => pollUsage($, cwd)))
  cancelPoll = () => {
    pollInterval()
    cancelFirstPoll?.()
    cancelFirstPoll = null
  }
  timerRegistry.cancelPoll = cancelPoll
  timerRegistry.cancelSweep?.()
  timerRegistry.cancelSweep = arm($.clock.every(SWEEP_INTERVAL_MS, () => sweepLanes($, options, cwd)))
  busTimers.cancelBus?.()
  busTimers.cancelBus = arm($.clock.every(BUS_INTERVAL_MS, () => ensureBus($, options, cwd)))
  // the code rain's own frame: a view-state bump redraws the pane. It runs only while the dock is
  // OPEN (a closed pane must not render), and while something on screen is still animating.
  timerRegistry.cancelRain?.()
  timerRegistry.cancelRain = arm($.clock.every(RAIN_MS, async () => {
    let clockOk = true
    try {
      filesNow = await $.clock.now()
    } catch {
      clockOk = false // only the Files work skips this beat; the rain still beats
    }
    if (!(await read($, paneOpenAtom).catch(() => false))) return // a closed dock draws nothing
    // one clock for the whole beat: the engine clock when it answers, else wall time so a
    // refused clock.now still lets the rain move (C1: the review fix 4 test pins that)
    const now = clockOk ? filesNow : Date.now()
    const files = clockOk && filesShown && filesNow - lastFilesDraw < 10_000
    const rain = now - lastRainDraw < 10_000
    // The pulse only changes rows that carry an activity icon, and an icon exists only while a
    // tool-call entry is live (3 s): with none, a flip changes nothing on screen, so no redraw.
    const act = files && clockOk ? await read($, fileActivityAtom).catch(() => undefined) : undefined
    const icons = act != null && act.some(e => filesNow - e.at <= 3000)
    // a pane whose last render carried an icon keeps redrawing until a render draws its fade
    if (rain || icons || filesIconsDrawn) {
      void setViewKey($, 'rain:tick', String(Math.floor(now / RAIN_MS))).catch(() => {})
    }
    if (files) {
      if (filesSlotsStamp === null || filesNow - filesSlotsStamp >= 30_000) {
        filesSlotsStamp = filesNow
        void loadLaneSlots($).catch(() => {})
      }
      // the walk and its two git runs cost a tree listing each on a quiet repo: once per
      // FILES_WALK_MS window is enough for a create, delete, rename OR an edit to show.
      // A files:v write that was refused is retried at once: that is a lost redraw, not a quiet repo.
      if (filesSkipOnce) {
        filesSkipOnce = false
      } else if (!filesWalkInFlight && (filesBumpPending || filesNow - lastFilesWalk >= FILES_WALK_MS)) {
        lastFilesWalk = filesNow
        void filesTick($).catch(() => {})
      }
    }
  }))
  lastChipText = undefined
  firstLoadDone = false
  usageMissingAtLoad = false
  // The first load runs in the background: the pane draws from stored values and the
  // 5 s tick is the backstop, so a session does not wait two Python starts to hand over.
  // refreshSafe never throws; the catch is for a rejected promise only.
  firstLoad = refreshSafe($, options, cwd).catch(() => {})
  // Cold start: no usage.json yet means "no ledger yet" for the whole first poll interval.
  // The check usually rides loadSnapshot (it reads usage.json already); this one-shot fires
  // after 5 s and polls at most once. A first load that REJECTED before its usage check left
  // `firstLoadDone` false — the check runs here instead, so a cold start with a missing
  // ledger still polls once rather than never.
  cancelFirstPoll?.()
  cancelFirstPoll = arm($.clock.after(FIRST_POLL_MS, () => {
    void firstLoad
      .then(async () => {
        const missing = firstLoadDone ? usageMissingAtLoad : await usageMissingNow($)
        if (missing) await pollUsage($, cwd)
      })
      .catch(() => {})
  }))
}

/** The cold start's usage check when the first load never got that far: usage.json present?
 *  One `$.fs.read` (no spawn), the same file loadSnapshot reads. */
async function usageMissingNow($: EngineInterface): Promise<boolean> {
  const gdir = await globalDirOf($, await homeDir($))
  return gdir == null || (await readJson($, paths.usage(gdir))) == null
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
  // a closed pane draws nothing: drop the stamps that would keep the beat and the Files walk hot
  // for up to 10 s after the dock goes; the next tick after a reopen starts them again
  filesShown = false
  filesIconsDrawn = false
  lastRainDraw = 0
  lastFilesDraw = 0
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

/** The data dir of this session (mk-state.py's resolve_data order) and the global dir
 *  ($MK_HOME or $MATRIXKODER_HOME, else ~/.matrixkoder) are ONE rule, in paths.ts (C1):
 *  globalDirOf, listedDataDir, resolveDataDir and homeDir. The dashboard asks them with
 *  `requireExisting`, so a repo whose data dir is not there yet answers null (first-run). */

/** The runs list + the active run's document, from the project store (C4/L5).
 *
 *  `mkdb.py run-snapshot --data <dir> --json` is the ONE read-only reader the pane uses for
 *  the runs list and the active run. It opens the store `mode=ro`, never migrates and never
 *  creates it; `--data` resolves through mkpaths.project_folder, so a migrated project is
 *  read from its C2 store.
 *
 *  null means "the store could not answer" — no store (`store: false`), a store this code is
 *  too new/old for, a refused or slow spawn. The caller shows the NoRun panel. An empty but
 *  successful answer ({runs: [], run: null}) is NOT null: the store is there and holds no
 *  active run. A run the store named but we cannot coerce answers with `error` set. */
async function readStoreSnapshot($: EngineInterface, dataDir: string): Promise<{
  runs: MkRunIndexEntry[]
  run: MkRun | null
  error: string | null
} | null> {
  let ran: { exitCode: number; stdout: string }
  try {
    ran = await $.process.run(
      ['python3', `${$.plugin.root}/scripts/mkdb.py`, '--data', dataDir, 'run-snapshot', '--json'],
      { timeoutMs: STORE_READ_MS },
    )
  } catch {
    return null
  }
  if (ran.exitCode !== 0) return null
  const doc = parseJson(ran.stdout) as { runs?: unknown; run?: unknown; store?: unknown } | null
  if (doc === null || typeof doc !== 'object' || !Array.isArray(doc.runs)) return null
  if (doc.store === false) return null // no store: nothing to show
  const run = coerceRun(doc.run)
  const error = doc.run != null && run == null ? 'cannot read run.json' : null // a run the store named that we cannot read
  return { runs: coerceIndex(doc.runs), run, error }
}

/** Loads the active run. The project store is the only source of truth (C4/L5): one read
 *  answers the runs list and the newest active run's document. No store or an unreadable
 *  one is the NoRun panel. */
async function loadRuns($: EngineInterface, dataDir: string): Promise<{ run: MkRun | null; activeRuns: MkRun[]; runs: ReturnType<typeof coerceIndex>; error: string | null }> {
  const store = await readStoreSnapshot($, dataDir)
  if (store == null) return { run: null, activeRuns: [], runs: [], error: null }
  const activeRuns = store.run == null ? [] : [store.run]
  return { run: store.run, activeRuns, runs: store.runs, error: store.error }
}

async function loadSnapshot($: EngineInterface, cwd: string): Promise<MkSnapshot> {
  let now = 0
  try {
    now = await $.clock.now()
  } catch {
    // clock refused — remaining times show as unknown
  }
  const home = await homeDir($)
  const dataDir = await resolveDataDir($, { cwd, home, requireExisting: true, store: true })
  let run: MkRun | null = null
  let activeRuns: MkRun[] = []
  let runs: ReturnType<typeof coerceIndex> = []
  let error: string | null = null
  if (dataDir) {
    ;({ run, activeRuns, runs, error } = await loadRuns($, dataDir))
  }
  const gdir = await globalDirOf($, home)
  const usageJson = gdir ? await readJson($, paths.usage(gdir)) : null
  // The cold start's usage check rides this read: usage.json is already in hand, so
  // startLoop needs no second read and no extra spawn to know the ledger is missing.
  // Only the start's own load decides, so a later tick cannot overwrite the answer.
  if (!firstLoadDone) {
    usageMissingAtLoad = usageJson == null
    firstLoadDone = true
  }
  const usage = gdir ? coerceUsage(usageJson) : null
  // the project yaml scopes the usage section (and its poll) to this project's agents and
  // providers. yamlPathFor, not paths.projectYaml: on a MIGRATED project the file is the C2
  // `<project>/project.yaml`, and the pre-migration `<data>/matrixkoder.yaml` is gone.
  const yaml = dataDir != null
    ? await readText($, await yamlPathFor($, dataDir, home)).catch(() => null)
    : null
  const projectAgents = orderAgents(parseYamlAgents(yaml ?? ''))
  return { ...mkSnapshot(now, dataDir, run, activeRuns, runs, usage, projectAgents), error }
}

// --- the project yaml path, resolved once per refresh tick (todo 118) ---
// projectYamlPath answers with the C2 `project.yaml` on a migrated project and
// `<data>/matrixkoder.yaml` before it. It costs one `mkdb.py projects --json` spawn
// (paths.ts's single storeRows reader), and three callers want it per tick: the
// snapshot's usage scoping, the usage poll's providers, and the Settings loader.
// One memo per tick gives them one spawn; `storeRows` itself stays private to paths.ts.
let yamlPathMemo: { dataDir: string; path: string } | null = null

/** The project's yaml path for this tick (see yamlPathMemo). A refused resolve
 *  answers the pre-migration path, which is also the FALLBACK a store-less project has. */
async function yamlPathFor($: EngineInterface, dataDir: string, home: string | null): Promise<string> {
  if (yamlPathMemo?.dataDir === dataDir) return yamlPathMemo.path
  let path = paths.projectYaml(dataDir)
  try {
    path = await projectYamlPath($, dataDir, { home, store: true })
  } catch {
    // the store read refused — the pre-migration path is also what a store-less project has
  }
  yamlPathMemo = { dataDir, path }
  return path
}

/** Drops the memo at the start of a tick, so the next tick re-reads the root store. */
function resetYamlPathMemo(): void {
  yamlPathMemo = null
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
 *  injected command. The read is fired and not awaited, so
 *  a slow one cannot hold the tick; `pendingInFlight` is set before it starts
 *  and cleared in `finally`, so a tick that finds the previous read still
 *  running skips instead of stacking another process. */
let pendingInFlight = false
/** Sticky-down (P3.3): after the bus answered `down`, it is not asked again for
 *  `PENDING_RETRY_TICKS - 1` ticks, and nothing is written while the stored state
 *  still says down. A live answer — the next retry, or the Waiting tab's kick —
 *  clears it and shows the asks. */
const PENDING_RETRY_TICKS = 6
let pendingDownSkips = 0

async function refreshPending($: EngineInterface, cwd: string): Promise<void> {
  if (pendingInFlight) return
  // sticky-down: the ticks after a `down` answer ask nothing
  if (pendingDownSkips > 0) {
    pendingDownSkips -= 1
    return
  }
  pendingInFlight = true
  const dataDir = await resolveDataDir($, { cwd, home: await homeDir($), requireExisting: true, store: true }).catch(() => null)
  const run = (argv: readonly string[], init?: { timeoutMs?: number }) =>
    $.process.run([...argv], init).then(r => ({ stdout: r.stdout, code: r.exitCode }))
  const markDown = (): Promise<unknown> => {
    pendingDownSkips = PENDING_RETRY_TICKS - 1
    return update($, viewStateAtom, prev => (prev[PENDING_SRC_KEY] === 'down' ? prev : { ...prev, [PENDING_SRC_KEY]: 'down' })).catch(() => {})
  }
  void readPending(run, $.plugin.root, dataDir).then(async got => {
    if (!got.live && !got.down) return
    if (got.down) {
      await markDown()
      return
    }
    pendingDownSkips = 0
    const pending = JSON.stringify(got.asks)
    await update($, viewStateAtom, prev => (prev[PENDING_SRC_KEY] === 'live' && prev[PENDING_KEY] === pending
      ? prev
      : { ...prev, [PENDING_SRC_KEY]: 'live', [PENDING_KEY]: pending }))
  }).catch(() => {
    // this chain outlives the tick it started in, so a refused write here would surface as an
    // unhandled rejection long after the session's hooks module is gone. Swallow it.
    return markDown()
  }).finally(() => {
    pendingInFlight = false
  })
}

/** The Waiting tab's kick: whatever the backoff says, ask now. */
async function kickPending($: EngineInterface): Promise<void> {
  pendingDownSkips = 0
  await refreshPending($, await filesCwd($)).catch(() => {})
}

/** Plan sub-tabs (V1.3). File mode (no project mk.db) is today's Plan tab:
 *  the checkout fixture, no Segment row. Store mode draws List · Detail · Kanban. */
const PLANS_KEY = 'plans:list'
const PLANS_MS_KEY = 'plans:ms'
const PLANS_AT_KEY = 'plans:at'
const PLANS_SEL_AT_KEY = 'plans:sel-at'
const PLANS_SUB_AT_KEY = 'plans:sub-at'
const PLANS_ERROR_KEY = 'plans:error'
const PLANS_STALE_KEY = 'plans:stale'
const PLANS_EMPTY_KEY = 'plans:empty'
const PLANS_STORE_KEY = 'plans:store'
/** The active project's data dir, so the List can keep only ITS plans (L7.2 R2). */
const PLANS_DIR_KEY = 'plans:dir'
const PLAN_DETAIL_KEY = 'plans:detail'
const PLAN_DETAIL_FOR_KEY = 'plans:detail-for'
const PLANS_RELOAD_MS = 30_000
const PLAN_SUBS = ['list', 'detail', 'kanban'] as const

export function planSubOf(view: Record<string, string>): 'list' | 'detail' | 'kanban' {
  const sub = view['plan:sub']
  return sub === 'detail' || sub === 'kanban' ? sub : 'list'
}

/** Detail and Kanban with no selection: the first live plan, else the first plan. */
export function selectedPlanId(view: Record<string, string>, plans: readonly { id: string; live_run: string | null }[]): string {
  const sel = view['plans:sel'] ?? ''
  if (sel !== '' && plans.some(p => p.id === sel)) return sel
  const live = plans.find(p => p.live_run != null && p.live_run !== '')
  return (live ?? plans[0])?.id ?? ''
}

const PLAN_SCRIPT_KINDS = ['archive', 'unarchive', 'delete']

/** `mk-plan.py <kind> <id> --data <dataDir>` through the RunFn seam. Resolves the first error
 *  of the one-JSON-line reply (`{"ok":false,"errors":[...]}`), or the exit text when stdout
 *  holds no JSON; null only on `{"ok":true}` WITH exit 0 (the exit code wins). An id mk-plan.py
 *  would not accept (it also reads a leading '-' as a flag) is refused before any spawn. */
export async function runPlanAction(run: RunFn, root: string, dataDir: string, kind: string, id: string): Promise<string | null> {
  if (!/^[a-z0-9][a-z0-9-]*$/.test(id)) return `bad plan id ${id}`
  const r = await run(['python3', `${root}/scripts/mk-plan.py`, kind, id, '--data', dataDir], { timeoutMs: 60_000 })
  let reply: unknown = null
  try { reply = JSON.parse(r.stdout.trim()) } catch { reply = null }
  const err = r.stderr.trim()
  const exitText = `${kind} ${id} exit ${r.exitCode}${err !== '' ? `: ${err}` : ''}`
  if (typeof reply === 'object' && reply !== null && !Array.isArray(reply)) {
    const { ok, errors } = reply as { ok?: unknown; errors?: unknown }
    if (ok === true) return r.exitCode === 0 ? null : exitText
    const first = Array.isArray(errors) ? errors.find((e): e is string => typeof e === 'string' && e !== '') : undefined
    return first ?? `${kind} ${id} refused`
  }
  return exitText
}

/** The view-state patch for one press: a reload bump on success; on failure the plans error
 *  line (no reload: the refresh would clear it). An empty data dir spawns nothing. */
export async function planActionPatch(run: RunFn, root: string, view: Record<string, string>, kind: string, id: string, now: number): Promise<Record<string, string>> {
  const dataDir = view[PLANS_DIR_KEY] ?? ''
  let error: string | null
  try {
    error = dataDir === '' ? 'no project data dir' : await runPlanAction(run, root, dataDir, kind, id)
  } catch (e) {
    error = `${kind} ${id}: ${e instanceof Error ? e.message : String(e)}`
  }
  return error == null ? { 'plans:reload': String(now) } : { [PLANS_ERROR_KEY]: error, [PLANS_STALE_KEY]: '0' }
}

/** A press on Archive/Unarchive/Delete: run it, then apply the patch. */
async function planAction($: EngineInterface, kind: string, id: string): Promise<void> {
  const view = await read($, viewStateAtom)
  const patch = await planActionPatch((argv, init) => $.process.run([...argv], init), $.plugin.root, view, kind, id, Date.now())
  await update($, viewStateAtom, prev => ({ ...prev, ...patch }))
}

function plansViewOf(view: Record<string, string>, onAction: (kind: string, id: string) => void): PlansView {
  let parsed: unknown = null
  try { parsed = JSON.parse(view[PLANS_KEY] ?? '') } catch { parsed = null }
  // L7.2 R2: the root overview holds every project's plan_summary; keep only the
  // active project's plans (the alias row sharing its store dir counts).
  const mapped = plansFromOverview(parsed ?? {}, view[PLANS_DIR_KEY] ?? '')
  const at = Number(view[PLANS_AT_KEY] ?? '0')
  const cachedAt = at > 0 ? new Date(at).toTimeString().slice(0, 5) : ''
  return { mock: false, runs: mapped.runs, plans: mapped.plans, onAction, cachedAt }
}

function planDetailViewOf(view: Record<string, string>, id: string): PlanView | null {
  if ((view[PLAN_DETAIL_FOR_KEY] ?? '') !== id) return null
  let parsed: unknown = null
  try { parsed = JSON.parse(view[PLAN_DETAIL_KEY] ?? '') } catch { parsed = null }
  if (parsed == null) return null
  return planFromReply(parsed)
}

export async function resolveMainCheckout(
  $: { fs: { stat: (p: string) => Promise<{ kind: string }>; read: (p: string) => Promise<string> } },
  cwd: string,
): Promise<string> {
  if (!cwd) return ''
  const gitPath = join(cwd, '.git')
  try {
    const stat = await $.fs.stat(gitPath)
    if (stat.kind === 'file') {
      const content = await $.fs.read(gitPath)
      const gitdir = parseGitdir(content)
      if (gitdir) {
        const gd = gitdir.startsWith('/') ? gitdir : join(cwd, gitdir)
        const commondir = await $.fs.read(join(gd, 'commondir'))
        const main = mainCheckout(gd, commondir)
        if (main) return main
      }
    }
  } catch {
    // missing or unreadable
  }
  return cwd
}

async function filesCwd($: EngineInterface): Promise<string> {
  if (filesSessionCwd) return filesSessionCwd
  try { return await $.session.cwd() } catch { return '' }
}

function cachedMainCheckout($: EngineInterface, cwd: string): Promise<string> {
  let hit = filesMainCache.get(cwd)
  if (!hit) {
    hit = resolveMainCheckout($, cwd)
    filesMainCache.set(cwd, hit)
  }
  return hit
}

/** starts the first walk of the root on screen from a press or a tick, where state writes are allowed */
async function startFilesWalk($: EngineInterface): Promise<void> {
  try {
    const cwd = await filesCwd($)
    if (!cwd) return
    const view = await read($, viewStateAtom)
    const root = pickRoot(rootsOf(await cachedMainCheckout($, cwd), laneSlotsOf(view)), view['files:root'])
    if (!filesWalkCache.has(root.path)) await runFilesWalk($, root.path)
  } catch {
    // fire and forget: a state read refused after the plugin unloaded must not surface; the next tick starts again
  }
}

async function runFilesWalk($: EngineInterface, rootPath: string): Promise<void> {
  if (filesWalkInFlight || !rootPath) return
  filesWalkInFlight = true
  try {
    filesNow = await $.clock.now()
    const walkStart = filesNow
    const prevWalk = filesWalkCache.get(rootPath)
    // git-ignored folders are not scanned; only a press on their scan button lists one
    const ignOut = await $.process.run(['git', '-C', rootPath, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory'], { timeoutMs: 2000 }).catch(() => null)
    const skip = ignOut != null && ignOut.exitCode === 0 ? ignoredDirs(ignOut.stdout, rootPath) : new Set<string>()
    const nextWalk = await walkRoot(rootPath, dir => $.fs.list(dir), skip)
    for (const dir of filesScanned) {
      if (!dir.startsWith(`${rootPath}/`)) continue
      try { nextWalk.dirs.set(dir, await $.fs.list(dir)) } catch { /* gone: the next walk drops it */ }
    }
    const walkEnd = await $.clock.now()
    const duration = walkEnd - walkStart
    if (duration > 1000) {
      filesSkipOnce = true
    }
    const isFirstWalk = !prevWalk
    const isRootSwitch = rootPath !== lastRootWalked
    if (!nextWalk.missing) lastRootWalked = rootPath // a probe of a missing root is not the root on screen
    // a root that was missing and is back must redraw
    let changed = isFirstWalk || filesBumpPending || (prevWalk?.missing === true && !nextWalk.missing)
    if (prevWalk && !isRootSwitch) {
      const diff = diffWalk(prevWalk, nextWalk)
      if (diff.length > 0) {
        changed = true
        void update($, fileActivityAtom, s => {
          let cur = s
          for (const file of diff) {
            cur = recordActivity(cur, file, 'write', filesNow)
          }
          return cur
        }).catch(() => {})
      }
    }
    filesWalkCache.set(rootPath, nextWalk)
    const gitOut = await $.process.run(['git', '-C', rootPath, 'status', '--porcelain'], { timeoutMs: 2000 }).catch(() => null)
    const nextGit = gitOut != null && gitOut.exitCode === 0 ? gitStatusMap(gitOut.stdout) : new Map<string, string>()
    const prevGit = filesGitCache.get(rootPath)
    if (JSON.stringify([...nextGit]) !== JSON.stringify([...(prevGit ?? [])])) changed = true
    filesGitCache.set(rootPath, nextGit)
    if (changed) {
      // cleared only once the write landed; any failure (a render-time refusal too) retries on the next tick
      filesBumpPending = true
      await update($, viewStateAtom, prev => ({
        ...prev,
        'files:v': String(Number(prev['files:v'] ?? '0') + 1),
      }))
      filesBumpPending = false
    }
  } catch {
    // a failed clock read, list or bump write: the next tick walks again (a pending bump is retried)
  } finally {
    filesWalkInFlight = false
  }
}

async function filesTick($: EngineInterface): Promise<void> {
  if (filesWalkInFlight) return
  if (!filesCurrentRoot) {
    await startFilesWalk($) // nothing drawn yet to name a root
    return
  }
  // a chosen lane root that was missing may be back: look again, so its tree replaces the fallback
  if (filesChosenRoot && filesChosenRoot !== filesCurrentRoot && filesWalkCache.get(filesChosenRoot)?.missing) {
    await runFilesWalk($, filesChosenRoot)
  }
  await runFilesWalk($, filesCurrentRoot)
}

/** the Files tab's focus wiring: fire and forget, a rejection or a { deny } is ignored; never writes settings:focus */
export function filesFocus($: { ui: { focus: (args: { requestId: string; key: string }) => Promise<unknown> } }, key: string): void {
  void $.ui.focus({ requestId: PANE_ID, key }).catch(() => {})
}

function FilesPane(
  $: EngineInterface,
  els: Els,
  cols: number,
  viewState: Record<string, string>,
  activity: readonly MkFileAct[] | undefined,
  cwd: string,
  mainCheckoutPath: string,
): RenderElement {
  const { Text } = els
  try {
    const slots = laneSlotsOf(viewState)
    const roots = rootsOf(mainCheckoutPath || cwd, slots)
    const chosenId = viewState['files:root']
    const chosenRoot = pickRoot(roots, chosenId)

    let rootToRender = chosenRoot
    let note: string | null = null

    let walk = filesWalkCache.get(chosenRoot.path)
    if (!walk && !filesWalkInFlight && filesRenderWalk) {
      void runFilesWalk($, chosenRoot.path).catch(() => {})
    }

    const mainRoot = roots[0] ?? { id: 'main', label: 'main', path: mainCheckoutPath || cwd }
    if (chosenRoot.id !== 'main' && walk?.missing) {
      rootToRender = mainRoot
      note = 'missing root, fallback to repo'
      walk = filesWalkCache.get(mainRoot.path)
      if (!walk && !filesWalkInFlight && filesRenderWalk) {
        void runFilesWalk($, mainRoot.path).catch(() => {})
      }
    } else if (walk?.missing) {
      note = 'missing root'
    }

    filesCurrentRoot = rootToRender.path // the tick walks the root on screen, not a missing lane root
    filesChosenRoot = chosenRoot.path

    const effectiveWalk: Walk = walk ?? { dirs: new Map(), capped: false, missing: false }

    let openSet = new Set<string>()
    try {
      const rawOpen = viewState['files:open']
      if (rawOpen) {
        const parsed = JSON.parse(rawOpen)
        if (Array.isArray(parsed)) openSet = new Set(parsed as string[])
      }
    } catch {}

    const q = viewState['files:q'] ?? ''
    const actMap = liveActivity(activity, filesNow)
    const sortBy = viewState['files:sort']
    const sort: SortSpec = { by: sortBy === 'size' || sortBy === 'date' ? sortBy : 'name', desc: viewState['files:dir'] === 'desc' }
    const rows = treeRows(effectiveWalk, rootToRender.path, openSet, q, actMap, sort)

    let nFolders = 0
    let nFiles = 0
    for (const [_, entries] of effectiveWalk.dirs) {
      for (const e of entries) {
        if (e.name === '.git') continue
        if (e.kind === 'dir') nFolders++
        else if (e.kind === 'file' || e.kind === 'other') nFiles++
      }
    }

    let nWrite = 0
    let nRead = 0
    for (const [p, kind] of actMap) {
      if (p === rootToRender.path || p.startsWith(rootToRender.path.endsWith('/') ? rootToRender.path : `${rootToRender.path}/`)) {
        if (kind === 'write') nWrite++
        else if (kind === 'read') nRead++
      }
    }

    const pulse = Math.floor(filesNow / RAIN_MS) % 2 === 0
    // an icon on screen means the next pulse step changes something visible there
    filesIconsDrawn = rows.some(r => r.icon != null)
    const view: FilesView = {
      roots,
      root: rootToRender,
      rows,
      dirs: Array.from(effectiveWalk.dirs.keys()),
      nWrite,
      nRead,
      nFolders,
      nFiles,
      pulse,
      capped: effectiveWalk.capped,
      note,
      git: filesGitCache.get(rootToRender.path),
      q,
      sel: viewState['files:sel'],
    }

    const filesTabUi: FilesTabUi = {
      get: key => viewState[key],
      set: (key, value) => {
        void update($, viewStateAtom, prev => ({ ...prev, [key]: value })).then(() => { if (key === 'files:root') return startFilesWalk($) }).catch(() => {})
      },
      reveal: key => { void $.ui.scroll({ in: PANE_ID, to: { key }, block: 'center' }).catch(() => {}) },
      focus: key => filesFocus($, key),
      scan: path => {
        filesScanned.add(path)
        void $.fs.list(path)
          .then(entries => {
            filesWalkCache.get(rootToRender.path)?.dirs.set(path, entries)
            return update($, viewStateAtom, prev => ({ ...prev, 'files:v': String(Number(prev['files:v'] ?? '0') + 1) }))
          })
          .catch(() => {})
      },
    }

    return FilesTab(els, view, cols, filesTabUi)
  } catch (err) {
    return <Text>Files could not draw</Text>
  }
}

export function PlanPane(els: Els, view: Record<string, string>, paneCols: number, ui: TabUi): RenderElement {
  const cols = paneCols - 2 // the paddingX of the navy ground below
  const { Box, Text } = els
  // no project store yet: say so, never the Checkout v2 fixture
  if (view[PLANS_STORE_KEY] !== '1') return <Text dimColor={true}>no plans yet · approve a plan to see it here</Text>
  const sub = planSubOf(view)
  const onAction = (kind: string, id: string): void => {
    if (kind === 'open') {
      ui.set('plans:sel', id)
      ui.set('plan:sub', 'detail')
      return
    }
    if (PLAN_SCRIPT_KINDS.includes(kind) && ui.planAct != null) { ui.planAct(kind, id); return }
    ui.set('plans:reload', String(Date.now()))
    void ui.submit?.(planActionLine(kind, id))
  }
  const plans = plansViewOf(view, onAction)
  const sel = selectedPlanId(view, plans.plans)
  const err = view[PLANS_ERROR_KEY] ?? ''
  const errorLine = err === '' ? null : `⚠ ${view[PLANS_STALE_KEY] === '1' ? 'stale · ' : ''}${err}`
  let body: RenderElement
  let detailView: PlanView | null = null // the shown plan's mapped view (V1.5 panel)
  if (view[PLANS_EMPTY_KEY] === '1' && plans.plans.length === 0 && err === '') {
    body = <Text>no plans yet</Text>
  } else if (sub === 'list') {
    body = PlansTab(els, plans, cols, ui)
  } else if (sel === '') {
    body = <Text>no plan selected</Text>
  } else if (sub === 'detail') {
    const detail = planDetailViewOf(view, sel)
    detailView = detail
    body = detail == null ? <Text>loading {sel}</Text> : PlanTab(els, { ...detail, onAction }, cols, ui)
  } else {
    // V1.4: PlanKanbanTab from the raw `plans <id>` reply — kept raw because
    // the mapped PlanView drops `verdict`, which the NEEDS YOU column reads.
    let raw: unknown = null
    if ((view[PLAN_DETAIL_FOR_KEY] ?? '') === sel) {
      try { raw = JSON.parse(view[PLAN_DETAIL_KEY] ?? '') } catch { raw = null }
    }
    detailView = raw == null ? null : planFromReply(raw)
    body = raw == null ? <Text>loading {sel}</Text> : PlanKanbanTab(els, planKanbanView(raw, new Date().toISOString()), cols, ui)
  }
  // V1.5: when `plan:task` names a task of the shown plan, the task detail
  // panel draws under the sub-tab body at ≥59 cols and in place of it below
  // (the panel needs the width; under NARROW there is no room for both). Esc
  // cannot be armed without a kit change, so the panel's `esc close` line is
  // the pressable close.
  const selTask = view['plan:task'] ?? ''
  const selRow = selTask !== '' ? findPlanTask(detailView?.phases, selTask) : null
  if (selRow != null) {
    const panel = PlanTaskPanel(els, selRow, cols, () => { ui.set('plan:task', '') })
    // the pop-up: Detail draws it in place of the phases box (header, PHASES row and footer stay);
    // Kanban has no such slot, so the panel takes the body
    body = sub === 'detail' && detailView != null
      ? PlanTab(els, { ...detailView, onAction, taskPanel: panel }, cols, ui)
      : panel
  }
  return (
    // the design's navy ground under the whole Plan tab, not the terminal grey around navy cards
    <Box flexDirection="column" gap={1} flexGrow={1} backgroundColor={PALETTE.bg} paddingX={1} paddingY={1}>
      <Box flexDirection="row" gap={1}>
        {PLAN_SUBS.map(name => Segment(els, {
          key: `plan-sub-${name}`,
          label: name === 'list' ? 'List' : name === 'detail' ? 'Detail' : sub === 'kanban' && sel !== '' ? `Kanban · ${sel}` : 'Kanban',
          on: sub === name,
          onPress: () => { ui.set('plan:sub', name) },
        }))}
        <Box flexGrow={1} />
        {Segment(els, { key: 'plan-refresh', label: '↻ Refresh', on: false, hotkey: 'r', onPress: () => { ui.set('plans:reload', String(Date.now())) } })}
      </Box>
      {errorLine != null ? <Text color={ROLE.warn.fg} bold={true}>{errorLine}</Text> : null}
      {body}
    </Box>
  )
}

export type PlansRead = {
  list: unknown
  detail: unknown
  ms: number
  skipped: boolean
  empty: boolean
  missing: boolean
  error: string | null
}

/** Where readPlans reads from (L7.2): the ROOT store for the list, one project store
 *  for the Detail/Kanban. `rootExists` gates the list read, `projectExists` the detail.
 *  `overview` is the root `overview --json` document already read this tick (the top-level
 *  `k` needs the same read); when given, the list call is skipped (C1: one read per tick). */
export type PlansReadSources = {
  home: string | null
  rootExists: boolean
  dataDir: string | null
  projectExists: boolean
  detailId: string
  overview?: { doc: unknown; failure: { kind: 'exit' | 'json' | 'ok' | 'spawn' | 'timeout'; detail: string } | null }
}

/**
 * One plans read (L7.2). The LIST comes from the ROOT store's `mkdb.py overview --json`
 * (C3: numbers and linkage; no project DB is opened), so the List renders on a project
 * whose own store is unavailable. The DETAIL still comes from the project DB
 * (`mkdb.py plans <id> --json --data <dir>`), which is what Detail and Kanban need.
 * Neither read spawns mkdb when its store is known to be missing.
 */
export async function readPlans(
  run: (argv: readonly string[], init?: { timeoutMs?: number }) => Promise<{ stdout?: string; code?: number }>,
  pluginRoot: string,
  sources: PlansReadSources,
): Promise<PlansRead> {
  const { home, rootExists, dataDir, projectExists, detailId } = sources
  const fail = (error: string, ms: number): PlansRead => ({ list: null, detail: null, ms, skipped: false, empty: false, missing: false, error })
  if (home == null || !rootExists) return { list: null, detail: null, ms: 0, skipped: true, empty: true, missing: true, error: null }
  const started = Date.now()
  const call = async (argv: string[]): Promise<{ parsed: unknown; error: string | null }> => {
    let result: { stdout?: string; code?: number }
    try {
      result = await run(argv, { timeoutMs: 5_000 })
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      return { parsed: null, error: /timed out|timeout/i.test(msg) ? plansErrorLine('timeout') : plansErrorLine('spawn') }
    }
    if (result.code !== 0 && result.code != null) return { parsed: null, error: plansErrorLine('exit') }
    const stdout = result.stdout ?? ''
    if (stdout.trim() === '') return { parsed: null, error: plansErrorLine('json') }
    try {
      const parsed = JSON.parse(stdout) as { ok?: boolean; error?: string }
      if (parsed == null || typeof parsed !== 'object' || parsed.ok !== true) {
        return { parsed: null, error: plansErrorLine('ok', typeof parsed?.error === 'string' ? parsed.error : '') }
      }
      return { parsed, error: null }
    } catch {
      return { parsed: null, error: plansErrorLine('json') }
    }
  }
  const base = `${pluginRoot}/scripts/mkdb.py`
  const list = sources.overview != null
    ? (sources.overview.failure != null
      ? { parsed: null, error: plansErrorLine(sources.overview.failure.kind, sources.overview.failure.detail) }
      : { parsed: sources.overview.doc, error: null })
    : await call(['python3', base, '--home', home, 'overview', '--json'])
  if (list.error != null) return fail(list.error, Date.now() - started)
  const detail = detailId === '' || dataDir == null || !projectExists
    ? { parsed: null, error: null }
    : await call(['python3', base, 'plans', detailId, '--json', '--data', dataDir])
  if (detail.error != null) return { list: list.parsed, detail: null, ms: Date.now() - started, skipped: false, empty: false, missing: false, error: detail.error }
  const plans = list.parsed != null && typeof list.parsed === 'object' && 'plans' in list.parsed
    ? list.parsed.plans
    : undefined
  return {
    list: list.parsed,
    detail: detail.parsed,
    ms: Date.now() - started,
    skipped: false,
    empty: Array.isArray(plans) && plans.length === 0,
    missing: false,
    error: null,
  }
}

let plansInFlight = false

function plansDue(view: Record<string, string>, now: number): boolean {
  if ((view[PLANS_KEY] ?? '') === '') return true
  if ((view['plans:reload'] ?? '') !== (view['plans:reload-seen'] ?? '')) return true
  if ((view['plan:sub'] ?? '') !== (view[PLANS_SUB_AT_KEY] ?? '')) return true
  if ((view['plans:sel'] ?? '') !== (view[PLANS_SEL_AT_KEY] ?? '')) return true
  const at = Number(view[PLANS_AT_KEY] ?? '0')
  return now - at >= PLANS_RELOAD_MS
}

/** The Plan tab's open: load now, not on the next tick. `plans:reload` is already bumped by
 *  the presses that need one; this is the plain switch. */
async function kickPlans($: EngineInterface): Promise<void> {
  const cwd = await filesCwd($)
  await refreshPlans($, cwd, await readRootOverview($)).catch(() => {})
}

async function refreshPlans($: EngineInterface, cwd: string, overview: RootOverview): Promise<void> {
  // P3.4: nothing outside the Plan tab reads the plans keys (P3.0). A gate ahead of
  // every write keeps a closed tab from redrawing the pane.
  if ((await read($, dashTabAtom)) !== 'plan') return
  const storeHome = overview.home
  // The LIST reads the ROOT store (its plan_summary, read once per tick in readRootOverview),
  // the DETAIL the project DB (L7.2).
  if (storeHome == null || !overview.exists) {
    await update($, viewStateAtom, prev => ({ ...prev, [PLANS_STORE_KEY]: '0', [PLANS_EMPTY_KEY]: '1' }))
    return
  }
  const dataDir = await resolveDataDir($, { cwd, home: storeHome, requireExisting: true, store: true }).catch(() => null)
  let exists = false
  if (dataDir != null) {
    const projectPath = await paths.pickDb(dataDir, 'project', p => $.fs.exists(p).catch(() => false), p => $.fs.stat(p).then(st => st.size).catch(() => 0))
    try { exists = await $.fs.exists(projectPath) } catch { exists = false }
  }
  const view = await read($, viewStateAtom)
  let now = Date.now()
  try { now = await $.clock.now() } catch { /* wall clock */ }
  if (!plansDue(view, now)) {
    if (view[PLANS_STORE_KEY] !== '1') await update($, viewStateAtom, prev => ({ ...prev, [PLANS_STORE_KEY]: '1' }))
    return
  }
  if (plansInFlight) return
  plansInFlight = true
  const sel = view['plans:sel'] ?? ''
  const sub = view['plan:sub'] ?? ''
  const reload = view['plans:reload'] ?? ''
  const run = (argv: readonly string[], init?: { timeoutMs?: number }) =>
    $.process.run([...argv], init).then(r => ({ stdout: r.stdout, code: r.exitCode }))
  void readPlans(run, $.plugin.root, {
    home: storeHome, rootExists: overview.exists, dataDir, projectExists: exists, detailId: sel,
    overview: { doc: overview.doc, failure: overview.failure },
  }).then(async got => {
    await update($, viewStateAtom, prev => {
      if (got.missing) return { ...prev, [PLANS_STORE_KEY]: '0', [PLANS_EMPTY_KEY]: '1' }
      const had = (prev[PLANS_KEY] ?? '') !== ''
      const base = {
        ...prev,
        [PLANS_STORE_KEY]: '1',
        [PLANS_DIR_KEY]: dataDir ?? '',
        [PLANS_AT_KEY]: String(now),
        [PLANS_MS_KEY]: String(got.ms),
        [PLANS_SEL_AT_KEY]: sel,
        [PLANS_SUB_AT_KEY]: sub,
        'plans:reload-seen': reload,
      }
      if (got.error != null) {
        return { ...base, [PLANS_ERROR_KEY]: got.error, [PLANS_STALE_KEY]: had ? '1' : '0', ...(got.list != null ? { [PLANS_KEY]: JSON.stringify(got.list) } : {}) }
      }
      return {
        ...base,
        [PLANS_ERROR_KEY]: '',
        [PLANS_STALE_KEY]: '0',
        [PLANS_EMPTY_KEY]: got.empty ? '1' : '0',
        [PLANS_KEY]: JSON.stringify(got.list),
        ...(got.detail != null && sel !== '' ? { [PLAN_DETAIL_KEY]: JSON.stringify(got.detail), [PLAN_DETAIL_FOR_KEY]: sel } : {}),
      }
    })
  }).catch(() => {
    // see refreshPending: a detached chain's failure handler must not reject on its own
    return update($, viewStateAtom, prev => ({
      ...prev,
      [PLANS_STORE_KEY]: '1',
      [PLANS_ERROR_KEY]: plansErrorLine('spawn'),
      [PLANS_STALE_KEY]: (prev[PLANS_KEY] ?? '') !== '' ? '1' : '0',
    })).catch(() => {})
  }).finally(() => { plansInFlight = false })
}

// ── Todo tab: the list is read on the tick (only while the tab is open), writes run from presses ──

const TODO_ITEMS_KEY = 'todo:items'
const TODO_ERROR_KEY = 'todo:error'
const TODO_SLUG_KEY = 'todo:slug'
const TODO_HOME_KEY = 'todo:home'
const TODO_UNDO_KEY = 'todo:undo'

/** The active project's slug in the root overview: the row(s) whose store dir is the pane's data
 *  dir (the same match the Plan list uses), named after the row `storeGroups` labels it with. */
function activeSlug(doc: unknown, dataDir: string | null): string {
  const rows = doc != null && typeof doc === 'object' ? (doc as { projects?: unknown }).projects : undefined
  if (dataDir == null || dataDir === '' || !Array.isArray(rows)) return ''
  const mine = rows.filter((r): r is Record<string, unknown> =>
    r != null && typeof r === 'object' && (projectStoreDir(r) === dataDir || (typeof r.data_dir === 'string' && r.data_dir.trim() === dataDir)))
  return storeGroups(mine)[0]?.slug ?? ''
}

function todoPaneView(view: Record<string, string>): TodoView {
  const parse = (key: string): unknown => { try { return JSON.parse(view[key] ?? '') } catch { return null } }
  const items = parse(TODO_ITEMS_KEY)
  const undo = parse(TODO_UNDO_KEY)
  const f = view['todo:filter']
  return {
    mock: false,
    project: view[TODO_SLUG_KEY] || 'no project',
    todos: Array.isArray(items) ? items as TodoItem[] : [],
    filter: (f === 'open' || f === 'done' ? f : 'all') as TodoFilter,
    draft: view['todo:draft'] ?? '',
    undo: undo != null && typeof undo === 'object' ? undo as TodoItem : null,
  }
}

/** The Todo body: an error line (a failed verb), the tab, then the a / z hotkeys. The hotkeys
 *  stand down while the add field has the focus (it swallows letters), like Settings' noHotkey. */
function TodoPane($: EngineInterface, els: Els, view: Record<string, string>, cols: number, ui: TabUi): RenderElement {
  const { Box, Text, Button } = els
  const err = view[TODO_ERROR_KEY] ?? ''
  const focus = view['settings:focus'] ?? ''
  // the Improve Other fields and the editor take letters too (the footer has no field of its own)
  const typing = focus === 'todo-q' || focus === 'improve-edit' || focus.startsWith('improve-other-')
  return (
    <Box flexDirection="column" gap={1}>
      {err !== '' && <Text color={ROLE.err.fg}>{`⚠ ${sanitizeText(err, Math.max(8, cols - 4))}`}</Text>}
      {TodoTab(els, todoPaneView(view), cols, ui)}
      <Box flexDirection="row" gap={1} paddingX={1}>
        <Button key="todo-key-a" hotkey={typing ? undefined : 'a'} label="add" plain={true} onPress={() => filesFocus($, 'todo-q')} />
        <Button key="todo-key-z" hotkey={typing ? undefined : 'z'} label="undo" plain={true} onPress={() => ui.todoAct?.('undo')} />
      </Box>
    </Box>
  )
}

let todosInFlight = false
let todosAgain = false

/** `mkdb.py todo list` for one project; one read in flight. A write that lands while a read is
 *  out asks for one more read after it (`again`), so the list never keeps pre-write rows. */
async function loadTodos($: EngineInterface, home: string, slug: string, again = false): Promise<void> {
  if (todosInFlight) { if (again) todosAgain = true; return }
  todosInFlight = true
  try {
    const r = await $.process.run(['python3', `${$.plugin.root}/scripts/mkdb.py`, '--home', home, 'todo', 'list', '--project', slug], { timeoutMs: 5_000 })
    const reply = JSON.parse(r.stdout) as { ok?: boolean; todos?: TodoItem[]; error?: string }
    if (r.exitCode !== 0 || reply.ok !== true || !Array.isArray(reply.todos)) throw new Error(reply.error ?? `todo list exit ${r.exitCode}`)
    const items = JSON.stringify([...reply.todos].sort((a, b) => b.id - a.id))
    await update($, viewStateAtom, prev => (prev[TODO_SLUG_KEY] !== slug ? prev : prev[TODO_ITEMS_KEY] === items && (prev[TODO_ERROR_KEY] ?? '') === '' ? prev : { ...prev, [TODO_ITEMS_KEY]: items, [TODO_ERROR_KEY]: '' }))
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    await update($, viewStateAtom, prev => ({ ...prev, [TODO_ERROR_KEY]: `todo list: ${msg}` })).catch(() => {})
  } finally {
    todosInFlight = false
    if (todosAgain) { todosAgain = false; void loadTodos($, home, slug) }
  }
}

/** The tick (and the switch to the tab): resolve the project from the shared root overview, then
 *  list. Only while the Todo tab is open; a missing overview database spawns nothing. */
async function refreshTodos($: EngineInterface, cwd: string, overview: RootOverview): Promise<void> {
  if ((await read($, dashTabAtom)) !== 'todo') return
  const home = overview.exists ? overview.home ?? '' : ''
  const dataDir = home === '' ? null : await resolveDataDir($, { cwd, home, requireExisting: true, store: true }).catch(() => null)
  const slug = home === '' ? '' : activeSlug(overview.doc, dataDir)
  await update($, viewStateAtom, prev => (prev[TODO_HOME_KEY] === home && prev[TODO_SLUG_KEY] === slug ? prev
    // a new project: the old rows, the undo row and the error belong to the old one, so a press cannot send their ids here
    : { ...prev, [TODO_HOME_KEY]: home, [TODO_SLUG_KEY]: slug, [TODO_ITEMS_KEY]: '', [TODO_UNDO_KEY]: '', [TODO_ERROR_KEY]: '' }))
  if (home !== '' && slug !== '') await loadTodos($, home, slug)
}

async function kickTodos($: EngineInterface): Promise<void> {
  const cwd = await filesCwd($)
  await refreshTodos($, cwd, await readRootOverview($)).catch(() => {})
}

/** A press: run `mkdb.py todo <verb>` with the user text as ONE argv element (`--text=<text>`,
 *  so a leading dash is not read as a flag), then reload the list. A failure keeps the list and
 *  shows an error line. */
async function todoAction($: EngineInterface, kind: string, arg?: string | number): Promise<void> {
  const view = await read($, viewStateAtom)
  const home = view[TODO_HOME_KEY] ?? ''
  const slug = view[TODO_SLUG_KEY] ?? ''
  let undo: TodoItem | null = null
  try { undo = JSON.parse(view[TODO_UNDO_KEY] ?? '') as TodoItem } catch { undo = null }
  const tail =
    kind === 'add' ? ['add', `--text=${String(arg ?? '')}`]
    : kind === 'toggle' || kind === 'delete' ? [kind, '--id', String(arg)]
    : kind === 'clear-done' ? ['clear-done']
    : kind === 'undo' && undo != null
      ? ['restore', '--id', String(undo.id), `--text=${undo.text}`, '--done', undo.done ? '1' : '0', ...(undo.created ? [`--created=${undo.created}`] : [])]
      : null
  if (tail == null) return
  const fail = (msg: string) => update($, viewStateAtom, prev => ({ ...prev, [TODO_ERROR_KEY]: `todo ${kind}: ${msg}` }))
  if (home === '' || slug === '') { await fail('no project'); return }
  try {
    const r = await $.process.run(['python3', `${$.plugin.root}/scripts/mkdb.py`, '--home', home, 'todo', tail[0]!, '--project', slug, ...tail.slice(1)], { timeoutMs: 10_000 })
    let reply: { ok?: boolean; todo?: TodoItem; error?: string } = {}
    try { reply = JSON.parse(r.stdout) as typeof reply } catch { reply = {} }
    if (r.exitCode !== 0 || reply.ok !== true) { await fail(reply.error ?? (r.stderr.trim() || `exit ${r.exitCode}`)); return }
    await update($, viewStateAtom, prev => ({
      ...prev,
      [TODO_ERROR_KEY]: '',
      ...(kind === 'delete' ? { [TODO_UNDO_KEY]: JSON.stringify(reply.todo ?? null) } : {}),
      ...(kind === 'undo' ? { [TODO_UNDO_KEY]: '' } : {}),
      ...(kind === 'add' ? { 'todo:draft': '' } : {}),
    }))
  } catch (e) {
    await fail(e instanceof Error ? e.message : String(e))
    return
  }
  await loadTodos($, home, slug, true)
}

// ── Improve: Claude Code's prompt line and the Improve tab share one proposal (improveAtom). Presses write it and
// ask the model; a draw only reads it. The prompt line is never touched by a failure or a Discard. ──

const IMPROVE_STORE_KEY = 'improveHistory'
const IMPROVE_KEEP = 20
const IMPROVE_SAVED_CHARS = 4000 // the store is one 4 MiB file shared with wake: a pasted wall of text must not fill it
let improveSeq = 0 // two Improve presses inside one clock tick still get two ids
const draftSending = new Set<string>() // drafted-message flows whose Send is in flight

/** the open improvement, changed only while it is still the one that asked */
const patchFlow = ($: EngineInterface, id: string, change: Partial<MkImproveFlow>): Promise<unknown> =>
  update($, improveAtom, s => (s.flow != null && s.flow.id === id ? { ...s, flow: { ...s.flow, ...change } } : s))

/** A step change decided on the CURRENT flow inside the update, so two quick presses cannot both pass: resolves
 *  the changed flow, or null when the flow is gone, is another one, or is not in a step of `from`. */
async function moveFlow($: EngineInterface, id: string, from: readonly MkImproveStep[], change: (f: MkImproveFlow) => Partial<MkImproveFlow>): Promise<MkImproveFlow | null> {
  let moved: MkImproveFlow | null = null
  await update($, improveAtom, s => {
    moved = s.flow != null && s.flow.id === id && from.includes(s.flow.step) ? { ...s.flow, ...change(s.flow) } : null
    return moved == null ? s : { ...s, flow: moved }
  })
  return moved
}

/** the card's note after a wizard rewrite: how many of the answers it used */
const wizardNote = (f: MkImproveFlow): string => {
  const used = answeredPairs(f).length
  return used > 0 ? `Built from your answers · ${String(used)} of ${String(f.questions.length)} used` : ''
}

/** the answers, with what is typed in the open Other field counted as that question's answer */
const answersWithDraft = (f: MkImproveFlow): MkImproveFlow['answers'] =>
  f.other != null && f.otherDraft.trim() !== '' ? { ...f.answers, [String(f.other)]: { kind: 'other' as const, text: f.otherDraft.trim() } } : f.answers

/** A reload (or a new session) drops the timer a working flow waits on, but the atom lives on: clear such a flow so
 *  Improve can be pressed again. The box text is not touched. */
async function resetImproveWork($: EngineInterface): Promise<void> {
  try {
    await update($, improveAtom, prev => (prev.flow != null && (prev.flow.step === 'asking' || prev.flow.step === 'writing') ? { ...prev, flow: null } : prev))
  } catch {
    // the atom could not be written: the Discard chip still clears it
  }
}

/** the model call failed or its answer was unreadable: one calm line and a Try again; nothing is changed or sent */
const failFlow = ($: EngineInterface, id: string, failedStep: 'questions' | 'rewrite'): Promise<unknown> =>
  update($, improveAtom, s => (s.flow != null && s.flow.id === id && (s.flow.step === 'asking' || s.flow.step === 'writing')
    ? { ...s, flow: { ...s.flow, step: 'error' as const, failedStep, error: IMPROVE_FAILED } }
    : s))

/** Model work outlives a press, so it starts on a timer (as the standalone Prompt Upgrade mod does), never as a dangling promise. */
function improveLater($: EngineInterface, id: string, failedStep: 'questions' | 'rewrite', fn: () => Promise<void>): void {
  $.clock.after(1, () => { void fn().catch(() => failFlow($, id, failedStep).catch(() => {})) })
}

/** the last 150 messages as one block of text; never throws: no context is better than no rewrite */
async function improveContext($: EngineInterface): Promise<string> {
  try {
    return contextBlock(await $.session.messages())
  } catch {
    return ''
  }
}

/** Run Wizard: ask the model for up to three questions; none that help leaves the rewrite on show standing */
async function improveAsk($: EngineInterface, id: string): Promise<void> {
  const f = (await read($, improveAtom)).flow
  if (f == null || f.id !== id || f.step !== 'asking') return
  let r
  try {
    r = await $.model.complete({ model: IMPROVE_MODEL, system: QUESTIONS_SYSTEM, prompt: questionsPrompt(f.original, await improveContext($)), maxTokens: 900, timeoutMs: 45_000 })
  } catch {
    await failFlow($, id, 'questions')
    return
  }
  const cur = (await read($, improveAtom)).flow
  if (cur == null || cur.id !== id || cur.step !== 'asking') return
  if (!r.isAnswered) { await failFlow($, id, 'questions'); return }
  const questions = readQuestions(r.text)
  await patchFlow($, id, questions.length === 0
    ? { step: 'review', note: 'No question would help. This rewrite stands.' }
    : { step: 'questions', questions, answers: {}, other: null, note: '' })
}

/** The rewrite: one model call with the recent conversation and any wizard answers; the result is saved in the list */
async function improveWrite($: EngineInterface, id: string): Promise<void> {
  const f = (await read($, improveAtom)).flow
  if (f == null || f.id !== id || f.step !== 'writing') return
  const mode = modeOf(f)
  let call
  if (mode === 'improve') {
    call = { system: REWRITE_SYSTEM, prompt: rewritePrompt(f.original, answeredPairs(f), await improveContext($)) }
  } else {
    // Answer / Question read the last DRAFT_MESSAGES messages; with none there is nothing to reply to
    // the latest exchange is sent whole and apart from the earlier rows, so the draft answers it, not an old message
    let ctx = { earlier: '', latest: '' }
    try { ctx = draftContext(await $.session.messages()) } catch { ctx = { earlier: '', latest: '' } }
    if (ctx.latest === '') {
      await update($, improveAtom, s => (s.flow != null && s.flow.id === id && s.flow.step === 'writing' ? { ...s, flow: { ...s.flow, step: 'error' as const, failedStep: 'rewrite' as const, error: DRAFT_EMPTY } } : s))
      return
    }
    call = { system: DRAFT_SYSTEM, prompt: draftPrompt(mode, f.original, ctx) }
  }
  let r
  try {
    r = await $.model.complete({ model: IMPROVE_MODEL, ...call, maxTokens: 2000, timeoutMs: 60_000 })
  } catch {
    await failFlow($, id, 'rewrite')
    return
  }
  const cur = (await read($, improveAtom)).flow
  if (cur == null || cur.id !== id || cur.step !== 'writing') return
  const out = r.isAnswered ? readRewrite(r.text) : null
  if (out == null) { await failFlow($, id, 'rewrite'); return }
  await patchFlow($, id, { step: 'review', upgraded: out.upgraded, added: out.added, draft: '', isEditing: false, note: mode === 'improve' ? wizardNote(f) : '' })
  void $.ui.scroll({ in: PANE_ID, to: 'end' }).catch(() => {}) // the card ends the content; docked, the pinned strip sits under it
  if (mode !== 'improve') return // a drafted message is not a saved improvement
  const entry: MkImproveEntry = { id, at: await $.clock.now(), original: f.original.slice(0, IMPROVE_SAVED_CHARS), upgraded: out.upgraded.slice(0, IMPROVE_SAVED_CHARS), added: out.added }
  const list = await update($, improveHistoryAtom, prev => [entry, ...prev.filter(x => x.id !== id)].slice(0, IMPROVE_KEEP))
  try { await $.store.set(IMPROVE_STORE_KEY, list) } catch { /* the list stays for this session; the next save tries again */ }
}

/** The saved list, once at session start (an unreadable or foreign value counts as none). */
async function loadImproveHistory($: EngineInterface): Promise<void> {
  try {
    const saved = await $.store.get(IMPROVE_STORE_KEY)
    if (!Array.isArray(saved)) return
    const ok: MkImproveEntry[] = []
    for (const x of saved as Record<string, unknown>[]) {
      if (x != null && typeof x === 'object' && typeof x.id === 'string' && typeof x.at === 'number' && typeof x.original === 'string' && typeof x.upgraded === 'string' && Array.isArray(x.added)) {
        ok.push({ id: x.id, at: x.at, original: x.original, upgraded: x.upgraded, added: x.added.filter((a): a is string => typeof a === 'string') })
      }
    }
    if (ok.length > 0) await update($, improveHistoryAtom, prev => (prev.length > 0 ? prev : ok.slice(0, IMPROVE_KEEP)))
  } catch {
    // no store here: the list lives for this session only
  }
}

/** One Improve press (footer or tab). `a` / `b` carry the question index, the option or the text. */
async function improveAction($: EngineInterface, kind: ImproveKind, a?: string | number, b?: string): Promise<void> {
  const text = typeof a === 'string' ? a : b ?? ''
  const s = await read($, improveAtom)
  const f = s.flow
  if (kind === 'menu') {
    await update($, improveAtom, prev => ({ ...prev, menu: prev.menu !== true }))
  } else if (kind === 'improve' || (kind === 'retry' && f != null && f.step === 'error' && f.original.trim() === '' && modeOf(f) === 'improve')) {
    if (f != null && (f.step === 'asking' || f.step === 'writing')) {
      await update($, improveAtom, prev => ({ ...prev, menu: false }))
      return
    }
    // the draft in Claude Code's own prompt line, read at the press (never rejects; '' with no box)
    const original = (await $.prompt.read()).text.trim()
    const id = `${String(await $.clock.now())}-${String(++improveSeq)}`
    if (original === '') {
      await update($, improveAtom, prev => ({ ...prev, menu: false, flow: { ...newImproveFlow(id, '', 'error'), error: IMPROVE_EMPTY } }))
      return
    }
    await update($, improveAtom, prev => ({ ...prev, menu: false, flow: newImproveFlow(id, original) }))
    improveLater($, id, 'rewrite', () => improveWrite($, id))
  } else if (kind === 'compose' && (a === 'answer' || a === 'question')) {
    // draft the person's next message from the conversation; what the prompt line holds is a hint, never required
    if (f != null && (f.step === 'asking' || f.step === 'writing')) {
      await update($, improveAtom, prev => ({ ...prev, menu: false }))
      return
    }
    const hint = (await $.prompt.read()).text.trim()
    const id = `${String(await $.clock.now())}-${String(++improveSeq)}`
    await update($, improveAtom, prev => ({ ...prev, menu: false, flow: newImproveFlow(id, hint, 'writing', a) }))
    improveLater($, id, 'rewrite', () => improveWrite($, id))
  } else if (kind === 'send') {
    // a drafted message goes in as the person's own words; a hint left in the prompt line is cleared with it
    if (f == null || modeOf(f) === 'improve' || !canAccept(f) || draftSending.has(f.id)) return
    draftSending.add(f.id) // a second press while the first submit is out must not send it twice
    let sent = false
    try { sent = (await $.prompt.submit({ text: finalText(f), asUser: true })).drop == null } catch { sent = false } finally { draftSending.delete(f.id) }
    if (!sent) {
      await patchFlow($, f.id, { note: DRAFT_SEND_FAILED })
      return
    }
    await update($, improveAtom, prev => (prev.flow != null && prev.flow.id === f.id ? { ...prev, flow: null } : prev))
    if (f.original !== '' && (await $.prompt.read()).text.trim() === f.original) {
      await $.prompt.fill({ text: '', mode: 'replace' }).catch(() => undefined)
    }
  } else if (kind === 'accept') {
    // the proposal goes into the prompt line over the draft; the text it replaced is kept for Undo
    if (f == null || !canAccept(f)) return
    const proposal = finalText(f)
    const before = await $.prompt.read()
    const filled = await $.prompt.fill({ text: proposal, mode: 'replace' })
    if (!filled.isFilled) {
      // a dialog holds the box: the proposal stays on show, so Accept can be tried again
      await patchFlow($, f.id, { note: IMPROVE_FILL_FAILED })
      return
    }
    await update($, improveAtom, prev => (prev.flow != null && prev.flow.id === f.id && canAccept(prev.flow)
      ? { ...prev, undo: before.text, written: filled.text, flow: null }
      : prev))
  } else if (kind === 'discard') {
    await update($, improveAtom, prev => ({ ...prev, flow: null }))
  } else if (kind === 'undo') {
    // only while the line still holds what Accept wrote: newer typing is never overwritten
    if (s.undo == null) return
    const box = await $.prompt.read()
    if (s.written != null && box.text !== s.written) {
      await update($, improveAtom, prev => ({ ...prev, undo: null, written: null }))
      return
    }
    const filled = await $.prompt.fill({ text: s.undo, mode: 'replace' })
    if (!filled.isFilled) return // the Undo chip stays for a retry
    await update($, improveAtom, prev => (prev.undo === s.undo ? { ...prev, undo: null, written: null } : prev))
  } else if (kind === 'open') {
    const saved = (await read($, improveHistoryAtom)).find(x => x.id === a)
    if (saved == null) return
    await update($, improveAtom, prev => (prev.flow != null ? prev : {
      ...prev,
      flow: { ...newImproveFlow(saved.id, saved.original, 'review'), upgraded: saved.upgraded, added: saved.added },
    }))
  } else if (f != null) {
    const id = f.id
    const ix = typeof a === 'number' ? String(a) : ''
    if (kind === 'wizard') {
      // Run Wizard on the proposal on show: the same card turns to the questions (improvements only)
      if (modeOf(f) === 'improve' && await moveFlow($, id, ['review'], cur => ({ upgraded: cur.isEditing && cur.draft.trim() !== '' ? cur.draft : cur.upgraded, step: 'asking', questions: [], answers: {}, other: null, otherDraft: '', isEditing: false, note: '' }))) {
        improveLater($, id, 'questions', () => improveAsk($, id))
      }
    } else if (kind === 'answer' && typeof a === 'number') {
      // merged into the CURRENT answers, so two quick picks both land; an Other field open on another question stays
      await moveFlow($, id, ['questions'], cur => ({ other: cur.other === a ? null : cur.other, otherDraft: cur.other === a ? '' : cur.otherDraft, answers: { ...cur.answers, [ix]: { kind: 'option', text: b ?? '' } } }))
    } else if (kind === 'other' && typeof a === 'number') {
      // opening one Other field keeps what was typed in the last as that question's answer
      await moveFlow($, id, ['questions'], cur => {
        const typed = cur.answers[ix]
        return { answers: answersWithDraft(cur), other: a, otherDraft: typed?.kind === 'other' ? typed.text : '' }
      })
    } else if (kind === 'other-draft' && typeof a === 'number') {
      await moveFlow($, id, ['questions'], cur => (cur.other === a ? { otherDraft: text } : {}))
    } else if (kind === 'other-save' && typeof a === 'number') {
      const t = (b ?? '').trim()
      await moveFlow($, id, ['questions'], cur => ({ other: null, otherDraft: '', answers: t === '' ? cur.answers : { ...cur.answers, [ix]: { kind: 'other', text: t } } }))
    } else if (kind === 'skip' && typeof a === 'number') {
      await moveFlow($, id, ['questions'], cur => ({ other: cur.other === a ? null : cur.other, otherDraft: cur.other === a ? '' : cur.otherDraft, answers: { ...cur.answers, [ix]: { kind: 'skip' } } }))
    } else if (kind === 'go') {
      // what is typed in an open Other field counts; the rest count as skipped; the rewrite runs with what was answered
      const next = await moveFlow($, id, ['questions'], cur => {
        const answers = { ...answersWithDraft(cur) }
        cur.questions.forEach((_, i) => { if (answers[String(i)] === undefined) answers[String(i)] = { kind: 'skip' } })
        return { answers, other: null, otherDraft: '', step: 'writing' }
      })
      if (next != null) improveLater($, id, 'rewrite', () => improveWrite($, id))
    } else if (kind === 'retry') {
      const next = await moveFlow($, id, ['error'], cur => ({ step: cur.failedStep === 'questions' ? 'asking' : 'writing', error: '' }))
      if (next != null) improveLater($, id, next.failedStep, () => (next.failedStep === 'questions' ? improveAsk($, id) : improveWrite($, id)))
    } else if (kind === 'edit' && f.step === 'review') {
      // leaving the editor keeps the draft as the proposal
      await moveFlow($, id, ['review'], cur => (cur.isEditing ? { isEditing: false, upgraded: cur.draft.trim() === '' ? cur.upgraded : cur.draft, note: '' } : { isEditing: true, draft: cur.upgraded, note: '' }))
    } else if (kind === 'edit-save' && f.step === 'review') {
      await moveFlow($, id, ['review'], cur => ({ isEditing: false, upgraded: text.trim() === '' ? cur.upgraded : text, note: '' }))
    } else if (kind === 'draft' && f.step === 'review') {
      await patchFlow($, id, { draft: text })
    } else if (kind === 'copy' && f.step === 'review') {
      let copied = false
      try { copied = (await $.ui.copy({ text: finalText(f) })).isCopied } catch { copied = false }
      await patchFlow($, id, { note: copied ? 'Copied.' : 'Could not copy. Select the text instead.' })
    }
  }
}

/** One root-store read per tick (L7.2): the Plan list and the Todo tab both need the
 *  root `overview --json` (numbers + linkage). Reading it once and handing the same
 *  document to both readers keeps a tick at one spawn, not two (C1: no second reader of
 *  the same data). `exists` is the fs probe of the root store so neither reader spawns
 *  mkdb for a store that is not there (which would create it).
 *
 *  A failure carries the neutral `kind`/`detail`, never a rendered line: the Plan list
 *  labels it with its own `plansErrorLine`. */
export type RootOverview = {
  home: string | null
  exists: boolean
  doc: unknown
  failure: { kind: 'exit' | 'json' | 'ok' | 'spawn' | 'timeout'; detail: string } | null
}

async function readRootOverview($: EngineInterface): Promise<RootOverview> {
  const home = await globalDirOf($, await homeDir($))
  const dbPath = home == null ? '' : await paths.pickDb(home, 'root', p => $.fs.exists(p).catch(() => false), p => $.fs.stat(p).then(st => st.size).catch(() => 0))
  let exists = false
  if (dbPath !== '') {
    try { exists = await $.fs.exists(dbPath) } catch { exists = false }
  }
  if (home == null || !exists) return { home, exists: false, doc: null, failure: null }
  let ran: { exitCode: number; stdout: string }
  try {
    ran = await $.process.run(['python3', `${$.plugin.root}/scripts/mkdb.py`, '--home', home, 'overview', '--json'], { timeoutMs: 5_000 })
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    return { home, exists, doc: null, failure: { kind: /timed out|timeout/i.test(msg) ? 'timeout' : 'spawn', detail: '' } }
  }
  if (ran.exitCode !== 0) return { home, exists, doc: null, failure: { kind: 'exit', detail: '' } }
  const stdout = ran.stdout ?? ''
  if (stdout.trim() === '') return { home, exists, doc: null, failure: { kind: 'json', detail: '' } }
  try {
    const parsed = JSON.parse(stdout) as { ok?: boolean; error?: string }
    if (parsed == null || typeof parsed !== 'object' || parsed.ok !== true) {
      return { home, exists, doc: null, failure: { kind: 'ok', detail: typeof parsed?.error === 'string' ? parsed.error : '' } }
    }
    return { home, exists, doc: parsed, failure: null }
  } catch {
    return { home, exists, doc: null, failure: { kind: 'json', detail: '' } }
  }
}

/** Never throws: a failing load keeps the last good snapshot and flags the error. */
async function refreshSafe($: EngineInterface, options: PluginOptions, cwd: string): Promise<void> {
  beginStoreTick() // one `mkdb.py projects --json` for the whole tick
  resetYamlPathMemo() // one store read for the yaml path per tick, not one per caller
  // The previous snapshot is read INSIDE the try: a refused read must not escape the tick,
  // and `endStoreTick()` in the finally must run whatever happens here.
  let prev: MkSnapshot = EMPTY_SNAPSHOT
  let readPrev = false // did the previous snapshot actually load? (a refused read keeps the atom untouched)
  try {
    prev = await read($, snapshotAtom)
    readPrev = true
    // The two first reads do not depend on each other: the run state and the root
    // overview start together, so the tick waits for the slower one, not both. Each
    // keeps its own failure: a rejected snapshot leaves the last good one flagged,
    // and the overview is still handed to the tab refreshers.
    const [snap, overview] = await Promise.all([
      loadSnapshot($, cwd).then(
        value => ({ value, error: null as string | null }),
        (err: unknown) => ({ value: null, error: String(err) }),
      ),
      readRootOverview($),
    ])
    if (snap.value == null) {
      try {
        await update($, snapshotAtom, () => ({ ...prev, error: `cannot read the run state (${String(snap.error).slice(0, 40)})` }))
      } catch {
        // even the atom write failed — nothing more to do
      }
    } else {
      const good = snap.value
      await applyStatus($, options, good)
      if (contentSig(prev) !== contentSig(good)) {
        await update($, snapshotAtom, () => good)
      }
    }
    // One root `overview --json` for the whole tick (C1): the Plan list and the
    // Todo tab read the same document, so it is spawned once here and handed on.
    // The refreshers now start together, so paths.ts's per-tick `projects --json` memo is
    // filled first: otherwise two concurrent resolvers would each spawn the table (P1.1).
    await resolveDataDir($, { cwd, home: await homeDir($), requireExisting: true, store: true }).catch(() => null)
    // The tab refreshers share nothing but that document (P3.4): they run together, so a
    // slow one neither delays the others nor the tick, and `allSettled` keeps one
    // rejection from stopping the rest. Each already handles its own failure; the
    // settled list is awaited so `endStoreTick()` still closes after them.
    const eventsTick = snap.value == null
      ? undefined
      // the store read costs a spawn: only for an open Events tab, and only with a run to read
      : (async () => {
          if ((await read($, dashTabAtom)) === 'events') await refreshEvents($, cwd, snap.value as MkSnapshot)
        })()
    // (the bus read is listed after the gated refreshers, so a test of one rejecting is
    // also a test that the read behind it still ran)
    await Promise.allSettled([
      refreshPlans($, cwd, overview),
      refreshTodos($, cwd, overview),
      refreshPending($, cwd),
      eventsTick,
    ])
  } catch (err) {
    // A tick whose previous snapshot could not even be read flags nothing: writing the
    // EMPTY_SNAPSHOT fallback over a good atom would lose data to a transport failure.
    if (readPrev) {
      try {
        await update($, snapshotAtom, () => ({ ...prev, error: `cannot read the run state (${String(err).slice(0, 40)})` }))
      } catch {
        // even the atom write failed — nothing more to do
      }
    }
  } finally {
    await dropStaleUndo($).catch(() => {})
    endStoreTick() // the answer stays for an outside-tick caller while it is fresh
  }
}

/** Undo stands only while the prompt line still holds what Accept wrote: one cheap read per tick, and only while
 *  an Undo is held. Typing since then drops it, so the chip never offers to overwrite newer text. */
async function dropStaleUndo($: EngineInterface): Promise<void> {
  const s = await read($, improveAtom)
  if (s.undo == null || s.written == null) return
  const box = await $.prompt.read()
  if (box.text === s.written) return
  await update($, improveAtom, prev => (prev.undo === s.undo ? { ...prev, undo: null, written: null } : prev))
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
    const dataDir = await resolveDataDir($, { cwd, home: await homeDir($), requireExisting: true, store: true })
    if (dataDir == null) return // first-run: the wizard writes the data dir
    // the pane's own snapshot, not a second loadSnapshot: the refresh timer owns
    // reading the run state, and startLoop always populates this before either call site
    const snap = await read($, snapshotAtom) as MkPaneSnapshot
    if (snap.run == null) return // only an active run owns a bus
    const script = `${$.plugin.root}/scripts/mk-bus.py`
    // Root bus (C6): a project with a store heartbeats the ONE bus under the
    // global dir (paths.globalDirOf: $MK_HOME, else $MATRIXKODER_HOME, else
    // ~/.matrixkoder). File mode (no mk.db) keeps the per-project bus.json —
    // paths.busDirOf is mkpaths.bus_dir's rule.
    const busFile = paths.busFile(await busDirFor($, dataDir, await globalDirOf($, await homeDir($))))
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

/** mkpaths.bus_dir for the pane: $MK_BUS_DIR, else store mode (the project DB
 *  exists: an exists check, never a read of the DB) or file mode. */
async function busDirFor($: EngineInterface, dataDir: string, globalDir: string | null): Promise<string> {
  const db = await paths.pickDb(dataDir, 'project', p => $.fs.exists(p).catch(() => false), p => $.fs.stat(p).then(st => st.size).catch(() => 0))
  const store = await $.fs.exists(db).catch(() => false)
  let busEnv: string | null = null
  try {
    busEnv = (await $.env.get('MK_BUS_DIR')) || null
  } catch {
    // env noun refused: the store/file rule
  }
  return paths.busDirOf(dataDir, store, globalDir, busEnv)
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
    const dataDir = await resolveDataDir($, { cwd, home, requireExisting: true, store: true })
    if (dataDir == null) return null
    const yaml = await readText($, await yamlPathFor($, dataDir, home))
    if (yaml == null) return null
    const providers = parseYamlProviders(yaml)
    return providers != null && providers.length > 0 ? providers : null
  } catch {
    return null // an unreadable yaml polls the default set, never blocks the poll
  }
}

// ---------- S3: the Settings tab's live layer (settings-io does the pure
// work; these closures hand it this module's $ — $ never crosses an import) ----------

let slotsInflight = false

/** The project's lane slots (`mk-lane.py list`) into view state `lanes:slots`, at most every
 *  30 s and only while the Lanes tab draws — a render never waits on it. */
async function loadLaneSlots($: EngineInterface): Promise<void> {
  if (slotsInflight) return
  slotsInflight = true
  try {
    const r = await $.process.run(['python3', `${$.plugin.root}/scripts/mk-lane.py`, 'list'], { timeoutMs: 5_000 })
    const parsed = JSON.parse(r.stdout ?? '') as { ok?: boolean; slots?: unknown }
    if (parsed.ok === true && Array.isArray(parsed.slots)) {
      await update($, viewStateAtom, prev => ({ ...prev, 'lanes:slots': JSON.stringify(parsed.slots), 'lanes:slots-at': String(Date.now()) }))
    }
  } catch {
    // no lane slots readable — the tab keeps what it had
  } finally {
    slotsInflight = false
  }
}

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
    const home = await homeDir($)
    const dataDir = cwd == null ? null : await resolveDataDir($, { cwd, home, requireExisting: true, store: true })
    // the LIVE yaml path: the C2 `<project>/project.yaml` once this project is
    // migrated, `<dataDir>/matrixkoder.yaml` before (paths.projectYamlPath is
    // mkpaths.project_yaml's rule — a data dir alone cannot tell them apart).
    const yamlPath = dataDir == null ? null : await yamlPathFor($, dataDir, home)
    let yamlExists = false
    if (yamlPath != null) {
      try {
        yamlExists = (await $.fs.stat(yamlPath)).kind === 'file'
      } catch {
        yamlExists = false
      }
    }
    const root = $.plugin.root
    const run: RunFn = (argv, init) => $.process.run(argv, init)
    let fields: YamlFieldIn[] = []
    let file = ''
    let loadError = ''
    if (!yamlExists) {
      loadError = `No ${yamlPath ?? 'matrixkoder.yaml'} — run /mk setup`
    } else if (dataDir != null) {
      try {
        if (!st.migrated) {
          await runMigrate(run, root, dataDir)
          await setViewKey($, 'settings:migrated', '1')
        }
        const listed = await runListFields(run, root, dataDir)
        fields = listed.fields
        file = listed.file
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
    // the live path `list --json` reported, so every scope note names the file it reads
    const view = buildSettingsView({ fields, file: file || yamlPath || '', configRows: cfg, globalDefaults: about.defaults, about })
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

function FirstRun(els: Els) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Text bold={true}>Setup</Text>
      <Text color={PALETTE.blue} bold={true}>⬡ Welcome to MatrixKoder!</Text>
      <Text>Configuration required for this project:</Text>
      <Text color={ROLE.warn.fg} bold={true}>[!] No project config yet (projects/&lt;slug&gt;/project.yaml)</Text>
      <Text color={PALETTE.blue} bold={true}>▶ Run /mk setup to launch the wizard</Text>
    </Box>
  )
}

/** No active run: the Overview's card layout with what there is — a RUN card saying so,
 *  the recent runs, and the live usage — not the old plain-text panel. */
function NoRun(els: Els, snap: MkSnapshot, cols: number) {
  const { Box, Text } = els
  const W = Math.max(1, cols - 4)
  const past = snap.runs.filter(r => r.status !== 'active').slice(-5).reverse()
  const usage = liveOverviewView(snap).usage
  return (
    // no ground of its own: the pane's Matrix ground and code rain show between the cards
    <Box flexDirection="column" gap={1}>
      {Card(els, {}, (
        <Box flexDirection="column">
          {Section(els, { label: 'Run', right: 'idle' })}
          <Text>No active orchestration run.</Text>
          <Text dimColor={true}>To start a multi-lane feature run, ask Claude to orchestrate it.</Text>
          <Text color={ROLE.live.fg} bold={true}>▶ New project? Run /mk setup</Text>
        </Box>
      ))}
      {past.length > 0 && Card(els, {}, (
        <Box flexDirection="column">
          {Section(els, { label: 'Recent runs', right: String(past.length) })}
          {past.map(r => (
            <Box key={r.id} flexDirection="row" gap={1}>
              <Text color={r.status === 'done' ? ROLE.ok.fg : ROLE.warn.fg}>{r.status === 'done' ? '✓' : '‖'}</Text>
              <Text bold={true}>{r.id}</Text>
              <Text dimColor={true} wrap="truncate-end">{(r.feature ?? '').slice(0, Math.max(1, W - r.id.length - 14))}</Text>
              <Box flexGrow={1} />
              {Chip(els, { label: r.status ?? '', role: r.status === 'done' ? 'ok' : 'idle' })}
            </Box>
          ))}
        </Box>
      ))}
      {usage.length > 0 && UsageCard(els, usage, W, cols < NARROW)}
    </Box>
  )
}

function elapsedMin(task: MkTask, nowMs: number): number | null {
  const t = Date.parse(task.updated ?? '')
  if (!Number.isFinite(t) || !nowMs) return null
  return Math.max(0, Math.round((nowMs - t) / 60000))
}

function statusWord(status: string, compact: boolean): string {
  return compact ? STATUS_SHORT[status] ?? status : status
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
  if (!compact) return `${d.toLocaleString('en', { month: 'short' })} ${d.getDate()}` // `Oct 20`, not the ambiguous `10-20`
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

const RAIN_MS = 1500
/** The Files walk + its two git runs: one per window on a quiet repo (a create, delete, rename or
 *  edit still shows within one window, so change detection is kept). */
const FILES_WALK_MS = 5000
/** when the rain last drew: the tick only redraws while a pane is actually showing it */
let lastRainDraw = 0

const RAIN_GLYPHS = 'ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789'
// three dim greens, head to tail: kept far under the text greens so the rain stays background
const RAIN_SHADES = ['#1c6b32', '#124a22', '#0b2e16'] as const

/** a cheap deterministic hash, so a column's drop is stable between redraws of the same tick */
const rainHash = (n: number): number => {
  let x = (n ^ 0x9e3779b9) >>> 0
  x = Math.imul(x ^ (x >>> 16), 0x85ebca6b) >>> 0
  x = Math.imul(x ^ (x >>> 13), 0xc2b2ae35) >>> 0
  return (x ^ (x >>> 16)) >>> 0
}

/** The rain's cells for one tick: shade per cell (-1 = empty) and the glyph grid. Pure. */
export type RainGrid = { grid: number[][]; glyph: string[][] }

/** Builds the rain grid for (cols, rows, tick). Pure: same inputs, same output. */
export function buildRainGrid(cols: number, rows: number, tick: number): RainGrid {
  // shade per cell (-1 = empty), then each row is drawn as runs of one shade
  const grid: number[][] = Array.from({ length: rows }, () => new Array<number>(cols).fill(-1))
  const glyph: string[][] = Array.from({ length: rows }, () => new Array<string>(cols).fill(' '))
  for (let c = 0; c < cols; c += 2) {
    const h = rainHash(c * 7919)
    if (h % 3 === 0) continue // a third of the columns stay dark
    const len = 4 + (h % 9)
    const period = rows + len + (h % 17)
    const head = (tick * (1 + (h % 2)) + (h % period)) % period
    for (let k = 0; k < len; k++) {
      const r = head - k
      if (r < 0 || r >= rows) continue
      grid[r]![c] = k === 0 ? 0 : k < len / 2 ? 1 : 2
      glyph[r]![c] = RAIN_GLYPHS[rainHash(c * 31 + r * 131 + tick) % RAIN_GLYPHS.length]!
    }
  }
  return { grid, glyph }
}

/** The latest grid only: two renders in one tick and size reuse it, a new tick or size builds a new
 *  one, and the cache never grows past this one entry. */
let rainGridCache: { cols: number; rows: number; tick: number; value: RainGrid } | null = null

/** buildRainGrid, once per (cols, rows, tick): a render is otherwise ~1,840 fresh cells. */
export function rainGridFor(cols: number, rows: number, tick: number): RainGrid {
  const hit = rainGridCache
  if (hit != null && hit.cols === cols && hit.rows === rows && hit.tick === tick) return hit.value
  const value = buildRainGrid(cols, rows, tick)
  rainGridCache = { cols, rows, tick, value }
  return value
}

/** Matrix code rain as an absolute layer behind the pane: one drop on every other column,
 *  falling one or two rows per rain tick, its head brightest and its tail fading. It moves only when
 *  the pane redraws: the RAIN_MS tick below, or any other redraw. */
function codeRain(els: Elements[keyof Elements], cols: number, rows: number, now: number): RenderElement {
  const { Box, Text } = els
  lastRainDraw = now
  const tick = Math.floor(now / RAIN_MS) // one step per rain redraw
  const { grid, glyph } = rainGridFor(cols, rows, tick)
  return (
    <Box key="rain" position="absolute" top={0} left={0} flexDirection="column">
      {grid.map((row, r) => {
        const runs: RenderElement[] = []
        let start = 0
        for (let c = 1; c <= cols; c++) {
          if (c < cols && row[c] === row[start]) continue
          const shade = row[start]!
          const text = glyph[r]!.slice(start, c).join('')
          runs.push(shade < 0
            ? <Text key={String(start)}>{text}</Text>
            : <Text key={String(start)} color={RAIN_SHADES[shade]}>{text}</Text>)
          start = c
        }
        return <Text key={`r${String(r)}`}>{runs}</Text>
      })}
    </Box>
  )
}

/** The Usage tab's live view: the snapshot's ledger rows (usageRows:
 *  `claude (all)`, `└` pool rows, ⊘ exhausted, n/a, hints) in the tab's QuotaRow shape.
 *  Jev routing has no recorded source yet, so that card is empty, never invented. */
const FLOOR_KEY = 'routing_preferences.reserve_floor'
/** the four reserve floors (% left) from the live Settings view; the schema defaults until it loads */
function reserveFloors(settings: SettingsView | null): FloorCtl[] {
  const rows = new Map((settings?.groups ?? []).flatMap(g => g.rows).map(f => [f.key, f]))
  const one = (who: 'claude' | 'others', win: 'five_hour' | 'weekly', dflt: number): FloorCtl['five'] => {
    const key = `${FLOOR_KEY}.${who}.${win}_left_pct`
    const f = rows.get(key)
    const v = Number(f?.value ?? dflt)
    return { key, left: Number.isFinite(v) ? v : dflt, min: f?.min ?? 0, max: f?.max ?? 90 }
  }
  return [
    { who: 'claude', label: 'claude', five: one('claude', 'five_hour', 40), week: one('claude', 'weekly', 30) },
    { who: 'others', label: 'others', five: one('others', 'five_hour', 10), week: one('others', 'weekly', 15) },
  ]
}

export function liveUsageView(snap: MkSnapshot, pollMin: number, settings: SettingsView | null = null): UsageView {
  const floors = reserveFloors(settings)
  // the bar's floor mark sits at the share used where the reserve starts: 100 − % left
  const floorFor = (agent: string, kind: '5h' | 'week'): FloorSetting => {
    const f = floors[agent === 'claude' ? 0 : 1]!
    return kind === '5h' ? f.five : f.week
  }
  type Win = { pool?: string; kind?: string; used_pct?: number; estimated?: boolean; pace?: { summary: string; will_last: boolean } }
  const winsOf = (agent: string, pool: string | null): Win[] =>
    ((snap.usage?.agents?.[agent] as { windows?: Win[] } | undefined)?.windows ?? []).filter(x => pool == null || x.pool === pool)
  // a monthly window (OpenCode Go) as a third bar; `est.` when CodexBar only estimates locally
  const monthOf = (agent: string, pool: string | null): QuotaRow['month'] => {
    const m = winsOf(agent, pool).find(x => x.kind === 'month' && typeof x.used_pct === 'number')
    return m == null ? undefined : { pct: m.used_pct!, used: `${Math.round(m.used_pct!)}%` }
  }
  const estOf = (agent: string, pool: string | null): boolean => winsOf(agent, pool).some(x => x.estimated === true)
  // CodexBar's pace forecast for one agent pool window (only Claude's session/weekly carry one today)
  const paceOf = (agent: string, pool: string | null, kind: '5h' | 'week'): Pick<Quota, 'pace' | 'paceBad'> => {
    const w = ((snap.usage?.agents?.[agent] as { windows?: Win[] } | undefined)?.windows ?? [])
      .find(x => x.kind === kind && x.pace != null && (pool == null || x.pool === pool))
    return w?.pace ? { pace: w.pace.summary, paceBad: !w.pace.will_last } : {}
  }
  const q = (pct: number | null, na: boolean, exhausted: boolean, pace: Pick<Quota, 'pace' | 'paceBad'> = {}, fs?: FloorSetting): QuotaRow['five'] =>
    exhausted ? { pct: 100, used: '⊘' } : pct == null ? (na ? { pct: 0, used: 'n/a' } : undefined) : { pct, used: `${Math.round(pct)}%`, floor: fs == null ? 80 : 100 - fs.left, floorSet: fs, ...pace }
  const rows: QuotaRow[] = []
  for (const r of usageRows(snap, false)) {
    if (r.hint != null) { rows.push({ name: r.label, nameTone: 'dim', note: r.hint, noteTone: 'dim' }); continue }
    if (r.header) { rows.push({ name: r.label }); continue }
    if (r.na && r.weekPct == null && r.sub.length === 0) { rows.push({ name: r.label.trim(), note: `n/a · sign in to ${r.name} in CodexBar`, noteTone: 'warning' }); continue }
    if (r.pct == null && r.weekPct == null && !r.na && r.sub.length === 0) { rows.push({ name: r.label.trim(), note: 'no quota source', noteTone: 'dim' }); continue }
    const [agent, pool] = r.name.includes(':') ? r.name.split(':') as [string, string] : [r.name, r.name === 'claude' ? 'default' : null]
    rows.push({
      name: r.label.trim(),
      ...(r.label.startsWith('  ') ? { nameTone: 'dim' as const, pool: true } : {}),
      five: q(r.pct, r.na, r.exhausted, paceOf(agent, pool, '5h'), floorFor(agent, '5h')),
      week: q(r.weekPct, false, false, paceOf(agent, pool, 'week'), floorFor(agent, 'week')),
      reset: resetStr(r.exhausted && r.exhaustedUntil ? r.exhaustedUntil : r.reset, snap.now, false).trim(),
      month: monthOf(agent, pool),
      ...(estOf(agent, pool) ? { est: true } : {}),
    })
    for (const p of r.sub) {
      rows.push(p.hint != null
        ? { name: `└ ${p.name}`, nameTone: 'dim', pool: true, note: p.hint, noteTone: 'dim' }
        : { name: `└ ${p.name}`, nameTone: 'dim', pool: true, five: q(p.pct, false, p.exhausted, paceOf(r.name, p.name, '5h'), floorFor(r.name, '5h')), week: q(p.weekPct, false, false, paceOf(r.name, p.name, 'week'), floorFor(r.name, 'week')), month: monthOf(r.name, p.name), ...(estOf(r.name, p.name) ? { est: true } : {}), reset: resetStr(p.exhausted && p.exhaustedUntil ? p.exhaustedUntil : p.reset, snap.now, false).trim() })
    }
  }
  return {
    ...USAGE_FIXTURE,
    mock: false,
    headerRight: '▮ reserve floor',
    rows,
    floors,
    actions: `usage.json · CodexBar · poll ${pollMin}m`,
    footerLeft: '',
    footerRight: '',
    decisionsRight: '',
    decisions: [],
    routingHint: 'Jev decisions show here once routing is recorded',
  }
}

// ---------- live views for Overview / Lanes / Events (same snapshot as liveUsageView) ----------

const hhmm = (iso: string | null | undefined): string => (iso ?? '').slice(11, 16)

/** local HH:MM of an ISO instant; '—' when it does not parse. */
function clockOf(iso: string | null | undefined): string {
  const d = new Date(iso ?? '')
  return Number.isNaN(d.getTime()) ? '—' : `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/** ` r2 14m`: the review/fix round and the age of a running task, '' when neither applies. */
function taskSuffix(t: MkTask, now: number): string {
  const round = t.round && (t.status === 'review' || t.status === 'fix') ? ` r${t.round}` : ''
  const mins = t.status === 'building' || t.status === 'verifying' || t.status === 'stalled' ? elapsedMin(t, now) : null
  return `${round}${mins != null ? ` ${mins}m` : ''}`
}

/** One entry per lane path (phases sharing a worktree share a row); no task carries a
 *  lane yet → the live tasks stand in. */
function laneGroups(run: MkRun): { slot: string; ids: string; status: string; rep: MkTask }[] {
  const out: { slot: string; ids: string; status: string; rep: MkTask }[] = []
  const add = (slot: string, tasks: MkTask[]): void => {
    const status = rollupLaneStatus(tasks)
    const rep = tasks.find(t => t.status === status) ?? tasks[0]
    if (rep == null) return
    const parents = tasks.filter(t => !t.parent)
    out.push({ slot, ids: (parents.length > 0 ? parents : tasks).map(t => t.id).join(' '), status, rep })
  }
  const lanes = run.tasks.filter(t => t.lane && LANE_LIVE.has(t.status))
  if (lanes.length > 0) {
    const by = new Map<string, MkTask[]>()
    for (const t of lanes) by.set(t.lane ?? '', [...(by.get(t.lane ?? '') ?? []), t])
    for (const [lane, tasks] of by) add(lane.split('/').filter(Boolean).at(-1) ?? lane, tasks)
  } else {
    for (const t of run.tasks.filter(t => LANE_FALLBACK.has(t.status))) add('·', [t])
  }
  return out
}

/** The usage rows' short names: a pool row `agy:gemini` drops its `agy:` prefix when the
 *  suffix is unique among the rows, else keeps the whole name (so two pools never clip alike). */
export function usageShorts<T extends { name: string }>(rows: readonly T[]): [T, string][] {
  const tail = (n: string): string => n.split(':').pop() ?? n
  return rows.map(r => [r, rows.filter(x => tail(x.name) === tail(r.name)).length === 1 ? tail(r.name) : r.name])
}

/** The Plan row's wide chip text (the glyph is the chip's): `build 14m`, `review r2`,
 *  `blocked P4` (the first dep not done), `quota 19:05` (the agent's reset), `W3` for a planned row. */
export function taskNote(t: MkTask, all: readonly MkTask[], snap: MkSnapshot, suffix: string): string {
  if (t.status === 'planned') return t.wave == null ? 'planned' : `W${t.wave}`
  if (t.status === 'blocked') {
    const dep = (t.deps ?? []).find(d => !DONE.has(all.find(x => x.id === d)?.status ?? '')) ?? (t.deps ?? [])[0]
    return dep == null ? 'blocked' : `blocked ${dep}`
  }
  if (t.status === 'quota') {
    const a = t.agent == null ? undefined : snap.usage?.agents?.[t.agent]
    const until = a?.exhausted_until ?? Object.values(a?.pools ?? {}).map(p => p.exhausted_until).find(u => u != null)
    return until == null ? 'quota' : `quota ${clockOf(until)}`
  }
  return `${t.status === 'building' ? 'build' : t.status}${suffix}`
}

/** The pane's last row on every tab but Settings: `2 live · 2 held · 1 waiting` left (waiting
 *  omitted at 0), the refresh interval right. Under a tight `w` the right drops `tick`, then
 *  goes, then the left shortens to `2l 2h 1w`, so the row never passes `w` cells. */
export function footerParts(live: number, held: number, waiting: number, w: number): { left: string; right: string } {
  const left = [`${live} live`, `${held} held`, ...(waiting > 0 ? [`${waiting} waiting`] : [])].join(' · ')
  const tick = REFRESH_MS / 1000
  for (const right of [`tick ${tick}s`, `${tick}s`, '']) {
    if (left.length + right.length + (right === '' ? 0 : 1) <= w) return { left, right }
  }
  return { left: [`${live}l`, `${held}h`, ...(waiting > 0 ? [`${waiting}w`] : [])].join(' ').slice(0, Math.max(1, w)), right: '' }
}

/** how many asks the pane owes an answer: the bus's pending asks, else the run's own waiting
 *  entry — what the Overview Waiting card draws */
export function openWaiting(asks: readonly unknown[], run: MkRun | null): number {
  return asks.length > 0 ? asks.length : run?.waiting ? 1 : 0
}

/** rows the pinned footer takes: the rule, the Improve chips, and the counts row while a run is on show */
export function pinnedFooterRows(snap: MkSnapshot, menu = false): number {
  return 2 + (snap.run != null ? 1 : 0) + (menu ? 3 : 0) // the open drop-up adds its three rows above the chips
}

function PaneFooter(els: Els, snap: MkSnapshot, waiting: number, cols: number, improve: ImproveView, ui: TabUi, card: boolean) {
  const { Box, Text } = els
  const groups = snap.run ? laneGroups(snap.run) : []
  const held = groups.filter(g => SIDE_STATE.has(g.status)).length
  const { left, right } = footerParts(groups.length - held, held, waiting, cols - 2) // paddingX 1 each side
  return (
    <Box flexDirection="column">
      <Text color={PALETTE.line}>{'╌'.repeat(Math.max(0, cols))}</Text>
      {ImproveFooter(els, improve, cols, ui, card)}
      {snap.run != null && (
        <Box flexDirection="row" justifyContent="space-between" gap={1} paddingX={1}>
          <Text dimColor={true}>{left}</Text>
          <Text dimColor={true}>{right}</Text>
        </Box>
      )}
    </Box>
  )
}

/** A model id in the Overview's lane rows: the family word, `-lite` kept (`gpt-6.1-sol` → `sol`,
 *  `gemini-3.8-flash-lite` → `flash-lite`, `claude-sonnet-4-5` → `sonnet`); unknown ids pass through. */
export function modelShort(model: string): string {
  const id = model.split('/').filter(Boolean).pop() ?? model
  return /(?:^|-)(sol|flash(?:-lite)?|sonnet|opus|haiku)(?:-|$)/i.exec(id)?.[1]?.toLowerCase() ?? id
}

/** The Overview's live view. No source for the lane slot of an un-laned run ('·').
 *  Waiting/usage/recent are empty when the snapshot has none. */
export function liveOverviewView(snap: MkSnapshot): OverviewView {
  const run = snap.run
  const tasks = run?.tasks ?? []
  const byWave = new Map<string, MkTask[]>()
  for (const t of tasks) {
    const key = t.wave == null ? 'W·' : `W${t.wave}`
    byWave.set(key, [...(byWave.get(key) ?? []), t])
  }
  const waves = [...byWave.entries()].sort((a, b) => a[0].localeCompare(b[0], undefined, { numeric: true })).map(([label, list]) => ({
    label,
    glyphs: list.map(t => (DONE.has(t.status) ? '✓' : STATUS_GLYPH[t.status] ?? '○')).join(''),
    tone: list.every(t => DONE.has(t.status)) ? 'done' as const : list.every(t => t.status === 'planned') ? 'planned' as const : 'active' as const,
  }))
  const w = run?.waiting
  const options = (w?.options ?? []).map(o => sanitizeText(o, 60))
  return {
    mock: false,
    run: {
      name: run?.feature ?? '',
      id: run?.id ?? '',
      short: run?.head_sha?.slice(0, 7) ?? '',
      stage: run?.stage ?? 0,
      stages: STAGE_TOTAL,
      stageWord: run ? stageWord(run.stage) : '',
      next: sanitizeText(run?.next, 120),
    },
    plan: { done: tasks.filter(t => DONE.has(t.status)).length, total: tasks.length },
    waves,
    tasks: tasks.filter(t => !DONE.has(t.status)).map(t => {
      const suffix = taskSuffix(t, snap.now)
      return { id: t.id, title: t.title, status: t.status, note: taskNote(t, tasks, snap, suffix), short: `${statusWord(t.status, true)}${suffix}` }
    }),
    lanes: (run ? laneGroups(run) : []).map(g => ({
      slot: g.slot,
      agent: g.rep.agent ?? 'agent',
      modelShort: modelShort(g.rep.model ?? ''),
      effort: g.rep.effort ?? '',
      status: g.status,
      short: statusWord(g.status, true),
    })),
    usage: usageShorts(usageRows(snap, false).filter(r => r.hint == null && !r.header && (r.pct != null || r.exhausted)))
      .map(([r, short]) => ({
        label: r.label.trim(),
        short,
        pct: Math.round(r.pct ?? 0),
        weekPct: r.weekPct == null ? null : Math.round(r.weekPct),
        reset: r.reset == null || r.exhausted ? null : resetStr(r.reset, snap.now, false),
        exhausted: r.exhausted,
        until: r.exhausted ? clockOf(r.exhaustedUntil) : null,
      })),
    waiting: {
      since: hhmm(w?.since),
      question: sanitizeText(w?.question, 160),
      options,
      rec: w?.rec == null ? -1 : (w.options ?? []).indexOf(w.rec),
      shortQuestion: sanitizeText(w?.question, 160),
      shortOptions: options,
    },
    recent: (run?.recent ?? []).slice(-3).reverse().map(ev => ({ t: hhmm(ev.t), text: sanitizeText(ev.text ?? '', 80) })),
  }
}

/** The Lanes tab's live view: one card per lane from run.tasks. No live source for the
 *  gate table, review counts, activity line, commit subject or pane id, so those stay
 *  empty; the stage strip is the task's place on LANE_PIPE. */
/** The lane slots last read by loadLaneSlots, typed for the Lanes tab. */
function laneSlotsOf(view: Record<string, string>): NonNullable<LanesView['slots']> {
  try {
    const raw = JSON.parse(view['lanes:slots'] ?? '[]') as unknown
    if (!Array.isArray(raw)) return []
    return raw.flatMap(x => {
      if (x == null || typeof x !== 'object') return []
      const o = x as Record<string, unknown>
      if (typeof o.slot !== 'number' || typeof o.path !== 'string') return []
      return [{ slot: o.slot, path: o.path, state: typeof o.state === 'string' ? o.state : '?', clean: o.clean === true, task: typeof o.task === 'string' ? o.task : null, branch: typeof o.branch === 'string' ? o.branch : null }]
    })
  } catch {
    return []
  }
}

/** the lane pipeline the stage strip walks, dispatch to merge */
const LANE_PIPE = ['dispatched', 'building', 'verifying', 'review', 'qa', 'merged'] as const as readonly string[]

export function liveLanesView(snap: MkSnapshot): LanesView {
  const groups = snap.run ? laneGroups(snap.run) : []
  const held = groups.filter(g => SIDE_STATE.has(g.status)).length
  const lanes: LaneData[] = groups.map(g => {
    // the stage strip: where the task sits on the lane pipeline (a fix round sits at review)
    const at = LANE_PIPE.indexOf(g.status === 'fix' ? 'review' : g.status)
    const reasons = (g.rep.open_reasons ?? []).map(r => r.kind).filter(Boolean)
    return {
      status: g.status,
      task: g.ids,
      title: g.rep.title,
      right: [g.rep.round != null && g.rep.round > 0 ? `r${String(g.rep.round)}` : '', taskSuffix(g.rep, snap.now).trim()].filter(Boolean).join(' · '),
      ...(at >= 0 ? { stage: { done: at, total: LANE_PIPE.length } } : {}),
      agent: [{ text: g.rep.agent ?? 'agent' }],
      agentId: g.rep.agent ?? 'agent',
      ...(g.rep.model ? { model: g.rep.model } : {}),
      ...(g.rep.effort ? { effort: g.rep.effort } : {}),
      ...(g.rep.last_commit ? { commit: { sha: g.rep.last_commit.slice(0, 7), subject: '' } } : {}),
      ...(reasons.length > 0 ? { note: [{ text: `waiting on ${reasons.join(', ')}`, tone: 'warning' as const }] } : {}),
      gate: [],
      actions: '',
    }
  })
  return {
    mock: false,
    summary: groups.length === 0 ? 'no live lanes' : `${groups.length} lanes · ${groups.length - held} live · ${held} held`,
    summaryRight: '',
    lanes,
    footerLeft: '',
    footerRight: '',
  }
}

let eventsInFlight = false
const EVENTS_LIST_KEY = 'events:list'
const EVENTS_BUS_KEY = 'events:bus'
const EVENTS_LANES_KEY = 'events:lanes'
/** raw store rows kept per run (acks included); the --since cursor reads only newer ones. */
const EVENTS_KEEP = EVENTS_MAX * 4
let eventsCache: { run: string; since: number; total: number; rows: Record<string, unknown>[] } | null = null
const LANE_BUCKETS = 24
const LANE_BUCKET_MS = 5 * 60_000 // 24 × 5 min: the card's "last 2h"
const LANE_SILENT_MS = 5 * 60_000
const LANE_EMPTY = '#0f4d22'

const ageOf = (ms: number): string =>
  ms < 60_000 ? `${Math.floor(ms / 1000)}s` : ms < 3_600_000 ? `${Math.floor(ms / 60_000)}m` : `${Math.floor(ms / 3_600_000)}h`

const metaOf = (row: Record<string, unknown>): Record<string, unknown> => {
  try {
    const meta = JSON.parse(typeof row.meta_json === 'string' ? row.meta_json : '') as unknown
    return meta != null && typeof meta === 'object' ? meta as Record<string, unknown> : {}
  } catch {
    return {}
  }
}

async function refreshEvents($: EngineInterface, cwd: string, snap: MkSnapshot): Promise<void> {
  if (eventsInFlight) return
  eventsInFlight = true
  // only a changed serialised value is written, so a quiet tick draws nothing
  const write = async (next: Record<string, string>): Promise<void> => {
    const prev = await read($, viewStateAtom)
    if (Object.keys(next).every(k => (prev[k] ?? '') === next[k])) return
    await update($, viewStateAtom, p => ({ ...p, ...next }))
  }
  try {
    const home = await homeDir($)
    const dataDir = await resolveDataDir($, { cwd, home, requireExisting: true, store: true }).catch(() => null)
    if (dataDir == null || snap.run == null) {
      eventsCache = null
      await write({ [EVENTS_LIST_KEY]: '', [EVENTS_BUS_KEY]: '', [EVENTS_LANES_KEY]: '' })
      return
    }
    const busDir = await busDirFor($, dataDir, await globalDirOf($, home))
    const now = Date.now()

    // 1. Bus status: up only while bus.json names a server whose heartbeat is fresh
    const busText = await readText($, paths.busFile(busDir)).catch(() => null)
    type RawBus = { port?: number; pid?: number; started?: number; owner_heartbeat?: number; projects?: Record<string, unknown> }
    let parsedBus = null as RawBus | null
    try {
      const parsed = JSON.parse(busText ?? '') as unknown
      if (parsed != null && typeof parsed === 'object') parsedBus = parsed as RawBus
    } catch {
      // missing, empty or corrupt bus.json: the bus reads down
    }
    const tokens = await $.fs.exists(join(busDir, 'bus-tokens.json')).catch(() => false)
    let ringQueue = 0
    try {
      const rq = JSON.parse((await readText($, join(busDir, 'ring-queue.json'))) ?? '[]') as unknown
      if (Array.isArray(rq)) ringQueue = rq.length
    } catch {
      // no readable ring queue: nothing pending
    }
    // a root bus heartbeats per project (projects map); file mode has owner_heartbeat only
    const beatMs = Math.max(Number(parsedBus?.owner_heartbeat) || 0, ...Object.values(parsedBus?.projects ?? {}).map(v => Number(v) || 0)) * 1000
    const startedMs = (Number(parsedBus?.started) || 0) * 1000
    const startHHMM = startedMs > 0 ? clockOf(new Date(startedMs).toISOString()) : '—'
    const upMins = Math.max(0, Math.floor((now - startedMs) / 60_000))
    const busView: BusViewData = parsedBus == null
      ? { ...BUS_UNKNOWN, files: { busJson: busText != null, tokens, ringQueue } }
      : {
        up: beatMs > 0 && now - beatMs <= 2 * BUS_INTERVAL_MS,
        lastAge: beatMs > 0 ? ageOf(Math.max(0, now - beatMs)) : '—',
        port: parsedBus.port ? `127.0.0.1:${parsedBus.port}` : '—',
        pid: typeof parsedBus.pid === 'number' ? parsedBus.pid : null,
        runningSince: startedMs > 0 ? `${startHHMM} · ${Math.floor(upMins / 60)}h ${upMins % 60}m` : '—',
        queue: { pending: ringQueue, rate: null, total: null },
        files: { busJson: true, tokens, ringQueue },
        rateStart: startHHMM,
        rateHistory: [],
      }

    // 2. Events: the --since cursor reads only rows past the last tick's newest id
    const runId = snap.run.id
    if (eventsCache?.run !== runId) eventsCache = { run: runId, since: 0, total: 0, rows: [] }
    const cache = eventsCache
    try {
      const ran = await $.process.run(
        ['python3', `${$.plugin.root}/scripts/mkdb.py`, '--data', dataDir, 'events', '--run', runId, '--chan', '', '--json',
          ...(cache.since > 0 ? ['--since', String(cache.since)] : [])],
        { timeoutMs: STORE_READ_MS },
      )
      if (ran.exitCode === 0) {
        const parsed = JSON.parse(ran.stdout) as { ok?: boolean; events?: Record<string, unknown>[] }
        if (parsed.ok && Array.isArray(parsed.events)) {
          for (const row of parsed.events) {
            if (typeof row.id === 'number' && row.id > cache.since) cache.since = row.id
            if (row.kind !== 'ack') cache.total += 1
          }
          cache.rows = [...cache.rows, ...parsed.events].slice(-EVENTS_KEEP)
        }
      }
    } catch {
      // subprocess safe catch: the cached rows stand
    }

    // mk-bus records an ack as its own kind:'ack' row (reply_to -> message, meta.ack_level):
    // fold it into its target, as mk-bus.py's read does, and keep it out of the list
    const ackOf = new Map<string, number>()
    for (const row of cache.rows) {
      if (row.kind !== 'ack' || typeof row.reply_to !== 'string') continue
      const level = metaOf(row).ack_level
      ackOf.set(row.reply_to, Math.max(ackOf.get(row.reply_to) ?? 0, typeof level === 'number' ? level : 1))
    }
    const items: EventItem[] = cache.rows.filter(row => row.kind !== 'ack').reverse().map((row, idx) => {
      const meta = metaOf(row)
      const isBus = row.chan === 'bus'
      const kind = typeof row.kind === 'string' ? row.kind : 'info'
      const fromId = typeof row.from_id === 'string' ? row.from_id : ''
      const toId = typeof row.to_id === 'string' ? row.to_id : ''
      const task = typeof row.task === 'string' ? row.task : ''
      const who = fromId && toId ? `${fromId}→${toId}` : (isBus ? fromId || 'orch' : 'run')
      const lane = task || (fromId.startsWith('p') ? fromId : toId.startsWith('p') ? toId : 'all')
      const text = typeof row.text === 'string' ? row.text : ''
      const id = typeof row.id === 'number' ? (isBus ? `m-${row.id}` : `r-${row.id}`) : `ev-${idx}`
      const t = typeof row.t === 'string' ? row.t : ''
      return {
        id,
        src: isBus ? 'bus' : 'run',
        t: hhmm(t),
        ts: Date.parse(t) || undefined,
        kind,
        type: kind,
        who,
        lane,
        text: sanitizeText(text, 120),
        ack: ackOf.get(id) ?? (meta.delivered === true ? 1 : 0),
        bad: meta.bad === true || kind.startsWith('ring-failed') || meta.delivered === false,
        reply_to: typeof row.reply_to === 'string' ? row.reply_to : undefined,
      }
    })

    // 3. Lane activity: 24 time buckets (oldest left), each the colour of its newest event
    const laneMap = new Map<string, { cells: string[]; newest: number }>()
    for (const ev of items) { // newest first
      if (!ev.lane || ev.lane === 'all' || ev.ts == null) continue
      const entry = laneMap.get(ev.lane) ?? { cells: Array<string>(LANE_BUCKETS).fill(LANE_EMPTY), newest: ev.ts }
      laneMap.set(ev.lane, entry)
      const slot = LANE_BUCKETS - 1 - Math.floor(Math.max(0, now - ev.ts) / LANE_BUCKET_MS)
      if (slot < 0 || entry.cells[slot] !== LANE_EMPTY) continue
      const kind = ev.kind ?? ''
      entry.cells[slot] = ev.bad || kind.startsWith('ring-') ? '#ff7a66'
        : kind === 'ask' || kind === 'nudge' ? '#e8b04a'
        : kind === 'answer' || kind === 'commit' ? '#19e35a'
        : kind === 'run' ? '#8bbf9a' : '#4fd1c5'
    }
    const lanesActivity: LaneActivityItem[] = Array.from(laneMap.entries()).map(([lane, { cells, newest }]) => {
      const age = Math.max(0, now - newest)
      const silent = age > LANE_SILENT_MS
      return { lane, cells, age: silent ? `${ageOf(age)} silent` : ageOf(age), silent }
    })

    await write({
      [EVENTS_BUS_KEY]: JSON.stringify(busView),
      [EVENTS_LIST_KEY]: JSON.stringify({ run: runId, total: cache.total, events: items.slice(0, EVENTS_MAX) }),
      [EVENTS_LANES_KEY]: JSON.stringify(lanesActivity),
    })
  } catch {
    // safe catch
  } finally {
    eventsInFlight = false
  }
}

const parseOr = <T,>(text: string | undefined): T | undefined => {
  try {
    return text ? JSON.parse(text) as T : undefined
  } catch {
    return undefined
  }
}

/** The Events tab's live view: the store's events for this run, else run.recent, newest
 *  first. The fixture is only the no-run (offline) view, labelled mock. */
export function liveEventsView(snap: MkSnapshot, viewState?: Record<string, string>): EventsView {
  if (snap.run == null) {
    return { ...EVENTS_FIXTURE, mock: true }
  }
  const bus = parseOr<BusViewData>(viewState?.[EVENTS_BUS_KEY]) ?? BUS_UNKNOWN
  const stored = parseOr<{ run?: string; total?: number; events?: EventItem[] }>(viewState?.[EVENTS_LIST_KEY])
  if (stored?.run === snap.run.id && Array.isArray(stored.events)) {
    return {
      mock: false,
      bus,
      events: stored.events,
      lanes: parseOr<LaneActivityItem[]>(viewState?.[EVENTS_LANES_KEY]) ?? [],
      earlierCount: Math.max(0, (stored.total ?? 0) - stored.events.length),
    }
  }
  const all = [...(snap.run?.recent ?? [])].reverse()
  return {
    mock: false,
    bus,
    events: all.slice(0, EVENTS_MAX).map(ev => ({
      id: `ev-${ev.t ?? ''}`,
      src: 'run' as const,
      t: hhmm(ev.t),
      ts: Date.parse(ev.t ?? '') || undefined,
      kind: ev.type ?? 'info',
      type: ev.type ?? 'info',
      who: 'run',
      lane: ev.task ?? 'all',
      text: sanitizeText(ev.text ?? '', 120),
    })),
    lanes: [],
    earlierCount: Math.max(0, all.length - EVENTS_MAX),
  }
}

