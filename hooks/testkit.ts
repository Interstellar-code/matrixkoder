// Shared harness for the MatrixKoder hook tests. The inline fixtures plugins
// write every fixture through $.fs.write on session.start, ahead of the plugin
// under test — their register bodies reference only module consts (a closure
// over call arguments does not survive the engine's module scan). fs is served
// in memory; mockWorld bottoms every other noun the mod touches, with optional
// recorders for the chip and toasts.
import { mock } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { Plugin } from 'claude-code/testing'
import { paths } from './paths'

export type Files = Record<string, string>

export const PROJECT_DIR = '/tmp/mk-proj'
export const DATA_DIR = `${PROJECT_DIR}/.matrixkoder`
export const RUNS_INDEX = paths.runsIndex(DATA_DIR)
export const RUN_PATH = paths.runDoc(DATA_DIR, 'wfv2')
export const USAGE_PATH = paths.usage('/home/mk/.matrixkoder')
export const NOW = Date.parse('2026-10-07T18:00:00Z')

const RUN_INDEX = JSON.stringify([
  { id: 'wfv2', feature: 'workflows-v2', status: 'active', started: '2026-10-07T16:00:00+02:00', ended: null },
  { id: 'mk-p0', feature: 'mod spikes', status: 'done', started: '2026-10-01T10:00:00+02:00', ended: '2026-10-02T10:00:00+02:00' },
])

const iso = (offsetMs: number): string => new Date(NOW + offsetMs).toISOString()

const RUN_JSON = JSON.stringify({
  schema: 1,
  id: 'wfv2',
  feature: 'workflows-v2',
  branch: 'main',
  head_sha: '82252eb0d1f2',
  status: 'active',
  stage: 7,
  next: 'verify F6 gates in worktree-b',
  waiting: { question: 'F8 deletes rows — approve destructive step?', options: ['approve', 'abort'], rec: 'approve', since: iso(0) },
  started: iso(-7200_000),
  ended: null,
  updated: iso(-60_000),
  tasks: [
    { id: 'W0', title: 'wizard split', wave: 0, deps: [], status: 'merged', agent: 'claude', model: 'opus', effort: 'high', lane: null, last_commit: '62811f9', round: 1, rounds: [], updated: iso(-3600_000) },
    { id: 'F6', title: 'design+configure', wave: 1, deps: [], status: 'review', agent: 'codex', model: 'gpt-5', effort: 'high', lane: '../wt-f6', last_commit: null, round: 2, rounds: [], updated: iso(-300_000) },
    { id: 'F8', title: 'bulk delete', wave: 1, deps: [], status: 'building', agent: 'agy', model: 'flash', effort: 'high', lane: '../wt-f8', last_commit: null, round: 1, rounds: [], updated: iso(-120_000) },
    { id: 'FIX6', title: 'QA1 fixes', wave: 1, deps: [], status: 'stalled', agent: 'codex', model: 'gpt-5', effort: 'medium', lane: '../wt-fix6', last_commit: null, round: 2, rounds: [], updated: iso(-660_000) },
    { id: 'X1', title: 'design pass', wave: 2, deps: ['F6', 'F8'], status: 'planned', agent: null, model: null, effort: null, lane: null, last_commit: null, round: 0, rounds: [], updated: iso(-600_000) },
  ],
  recent: [
    { t: iso(-1200_000), run: 'wfv2', task: 'F6', type: 'verdict', text: 'F6 r2 gates 6196 ✓ · tsc 121 = base', from: null, to: null, by: 'orchestrator' },
  ],
})

const USAGE_JSON = JSON.stringify({
  agents: {
    claude: {
      source: 'codexbar',
      five_hour: { used_pct: 7, resets_at: iso(4 * 3600_000 + 3 * 60_000) },
      weekly: { used_pct: 36, resets_at: iso(90 * 3600_000) },
      exhausted_until: null,
      available_pools: ['default', 'fable'],
      pools: {
        default: { five_hour: { used_pct: 7, resets_at: iso(4 * 3600_000) }, weekly: { used_pct: 36, resets_at: iso(90 * 3600_000) }, exhausted_until: null },
        fable: { five_hour: { used_pct: 12, resets_at: iso(4 * 3600_000) }, weekly: { used_pct: 12, resets_at: iso(90 * 3600_000) }, exhausted_until: null },
      },
    },
    agy: {
      source: 'codexbar',
      five_hour: { used_pct: 54, resets_at: iso(3600_000 + 15 * 60_000) },
      weekly: { used_pct: 52, resets_at: iso(6 * 24 * 3600_000) },
      exhausted_until: null,
      available_pools: ['gemini'],
      pools: {
        gemini: { five_hour: { used_pct: 54, resets_at: iso(3600_000 + 15 * 60_000) }, weekly: { used_pct: 52, resets_at: iso(6 * 24 * 3600_000) }, exhausted_until: null },
        'claude-gpt': { five_hour: { used_pct: 100, resets_at: iso(3600_000) }, weekly: { used_pct: 100, resets_at: iso(6 * 24 * 3600_000) }, exhausted_until: iso(6 * 24 * 3600_000) },
      },
    },
    codex: { source: 'codexbar', exhausted_until: null, available_pools: ['default'], pools: { default: { five_hour: null, weekly: null, exhausted_until: null } } },
    opencode: {
      source: 'codexbar',
      five_hour: { used_pct: 100, resets_at: iso(18 * 3600_000 + 54 * 60_000) },
      weekly: null,
      exhausted_until: iso(18 * 3600_000 + 54 * 60_000),
      available_pools: [],
      pools: { default: { five_hour: { used_pct: 100, resets_at: iso(18 * 3600_000 + 54 * 60_000) }, weekly: null, exhausted_until: iso(18 * 3600_000 + 54 * 60_000) } },
    },
  },
})

/** The fixture sources (served at /fixtures/*) plus the mutable dict view at
 *  the real locations; tests edit the real-location entries for transitions. */
export function worldFiles(): Files {
  return {
    ['/fixtures/index.json']: RUN_INDEX,
    ['/fixtures/run.json']: RUN_JSON,
    ['/fixtures/usage.json']: USAGE_JSON,
    [RUNS_INDEX]: RUN_INDEX,
    [RUN_PATH]: RUN_JSON,
    [USAGE_PATH]: USAGE_JSON,
  }
}

/** Copies the fixture sources the test's fs mock serves at /fixtures/* into their real
 *  locations via $.fs.write — every fixture reaches the mod through $.fs.write, while
 *  the bodies stay testkit consts (register sees literals only). */
export const worldFixtures: Plugin = {
  name: 'mk-fixtures',
  tier: 'prepend',
  register(on, _options) {
    // literals only: the engine's module scan drops closures over module consts (RUNS_INDEX etc.)
    return on('session.start', async ($, e, next) => {
      const copies: readonly [string, string][] = [
        ['/fixtures/index.json', '/tmp/mk-proj/.matrixkoder/runs/index.json'],
        ['/fixtures/run.json', '/tmp/mk-proj/.matrixkoder/runs/wfv2/run.json'],
        ['/fixtures/usage.json', '/home/mk/.matrixkoder/usage.json'],
      ]
      for (const [src, dst] of copies) await $.fs.write(dst, await $.fs.read(src))
      return next(e)
    })
  },
}

export function sessionStart(): { cwd: string; surface: 'terminal'; isInteractive: true } {
  return sessionStartIn(PROJECT_DIR)
}

export function sessionStartIn(cwd: string): { cwd: string; surface: 'terminal'; isInteractive: true } {
  return { cwd, surface: 'terminal', isInteractive: true }
}

/** A parsed JSON object, the shape every fixture mutator works on. */
export type Json = Record<string, unknown>

const parseObj = (text: string | undefined): Json => {
  const v: unknown = JSON.parse(text ?? '{}')
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Json) : {}
}

/** The run's tasks, narrowed to objects (a malformed entry is skipped, not fatal). */
export const tasksOf = (run: Json): Json[] =>
  Array.isArray(run.tasks) ? run.tasks.filter((t): t is Json => typeof t === 'object' && t !== null) : []

/** The usage ledger's agents map, narrowed IN PLACE (mutators delete entries). */
export const agentsOf = (usage: Json): Record<string, Json> => {
  const a = usage.agents
  return typeof a === 'object' && a !== null && !Array.isArray(a) ? (a as Record<string, Json>) : {}
}

/** One agent's usage entry, narrowed (undefined when absent). */
export const agentOf = (usage: Json, name: string): Json | undefined => agentsOf(usage)[name]

/** Rewrite the run fixture (source + dict) in one step; returns the parsed run. */
export function setRun(files: Files, mutate: (run: Json) => void): Json {
  const run = parseObj(files['/fixtures/run.json'])
  mutate(run)
  files['/fixtures/run.json'] = JSON.stringify(run)
  files[RUN_PATH] = JSON.stringify(run)
  return run
}

/** Rewrite the usage fixture (source + dict) in one step. */
export function setUsage(files: Files, mutate: (usage: Json) => void): void {
  const usage = parseObj(files['/fixtures/usage.json'])
  mutate(usage)
  files['/fixtures/usage.json'] = JSON.stringify(usage)
  files[USAGE_PATH] = JSON.stringify(usage)
}

/** prompt.compose's full input — the engine's compose dispatch crashes on an empty one. */
export function composeArgs() {
  return { model: 'sonnet', promptModel: 'sonnet', surfaces: ['terminal'] as const, tools: [], outputStyle: null, traits: [] }
}

/** In-memory fs: stat dir-hits for `dirs`, file-hits for `statFiles` (a worktree's
 *  `.git` is a FILE), reads from `files`, writes recorded into `files`. */
export function mockFs(
  on: On,
  files: Files,
  dirs: readonly string[] = [DATA_DIR, `${PROJECT_DIR}/.git`],
  statFiles: readonly string[] = [],
): { reads: string[] } {
  servedFiles = files
  const reads: string[] = []
  on('fs.write', (_$, e) => {
    files[e.path] = e.text
    return { value: undefined }
  })
  on('fs.read', (_$, e, next) => {
    reads.push(e.path)
    if (e.path in files) return { value: files[e.path] ?? '' }
    return next(e)
  })
  on('fs.stat', (_$, e, next) => {
    if (dirs.includes(e.path)) return { value: { kind: 'dir', size: 0, mtimeMs: 0, isLink: false } }
    if (statFiles.includes(e.path)) return { value: { kind: 'file', size: 0, mtimeMs: 0, isLink: false } }
    return next(e)
  })
  return { reads }
}

/**
 * Bottoms every noun the mod touches besides fs: clock, env, cwd, process,
 * ui.open/status/toast, session.start and prompt.compose. `status`/`toast` tap
 * the chip and toast texts, `process` taps poll argv, `env` adds variables
 * (e.g. MATRIXKODER_DATA). Returns the mock clock (for advance()).
 */
/** The files dict of the latest mockFs call: the process.run handler may not call $.fs (host scan),
 *  so the default run-snapshot reply reads the same in-memory dict the fs mock serves. */
let servedFiles: Files = {}

/** The default `mkdb.py run-snapshot --json` reply: built from the in-memory index/run fixture
 *  files (what tests mutate via setRun) in the real RUN_SNAPSHOT shape. An unreadable index is
 *  "no store" (`store: false`); a missing or unparseable run doc is `run: null`. */
let snapshotSpawns = 0
/** How many default run-snapshot reads the pane made since mockWorld was set up (the old RUN_PATH read count). */
export const snapshotCount = (): number => snapshotSpawns

function storeReply(argv: readonly string[]) {
  snapshotSpawns++
  const dataDir = argv[argv.indexOf('--data') + 1] ?? DATA_DIR
  const read = (path: string): unknown => {
    try { return JSON.parse(servedFiles[path] ?? '') } catch { return undefined }
  }
  /** a run doc that is present but not JSON: the store named it, the pane cannot read it (⚠ note) */
  const readDoc = (path: string): unknown => (path in servedFiles ? read(path) ?? '<unreadable>' : null)
  const out = (stdout: string) => ({ exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false })
  const index = read(paths.runsIndex(dataDir))
  if (!Array.isArray(index)) return out(JSON.stringify({ ok: true, store: false, runs: [] }))
  const entries = index.filter((r): r is { id: string; status?: string; started?: string } => typeof r === 'object' && r !== null && typeof r.id === 'string')
  const head = entries.filter(r => r.status === 'active').sort((a, b) => Date.parse(b.started ?? '') - Date.parse(a.started ?? ''))[0]
  const run = head ? readDoc(paths.runDoc(dataDir, head.id)) : null
  return out(JSON.stringify({ ok: true, runs: entries, active: head?.id ?? null, run, waits: [], store: true }))
}

/** what mockWorld hands back: the mock clock (for advance()) */
export type MockWorld = ReturnType<typeof mock.clock>

export function mockWorld(
  on: On,
  opts: {
    autoOpen?: boolean
    status?: (text: string | undefined) => void
    toast?: (text: string) => void
    process?: (argv: readonly string[]) => void | { value: { exitCode: number; stdout: string; stderr: string; isStdoutTruncated: boolean; isStderrTruncated: boolean } } | Promise<void | { value: { exitCode: number; stdout: string; stderr: string; isStdoutTruncated: boolean; isStderrTruncated: boolean } }>
    open?: (id: string) => void
    env?: Record<string, string>
  } = {},
): MockWorld {
  snapshotSpawns = 0
  const clock = mock.clock(on, { now: NOW })
  mock.env(on, { HOME: '/home/mk', ...opts.env })
  // engine events (session.start, prompt.compose) are answered bare; noun
  // dispatches (fs.*, ui.*, process.run) take { value }
  on('session.start', (_$, _e) => ({ cwd: PROJECT_DIR }))
  on('prompt.compose', (_$, _e) => ({ sections: [] }))
  on('process.run', async (_$, e) => {
    const override = await opts.process?.(e.argv)
    if (override == null && e.argv.includes('run-snapshot')) return { value: storeReply(e.argv) }
    return override ?? { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('ui.open', (_$, e) => {
    opts.open?.(e.id)
    return { value: { isPlaced: true } }
  })
  on('ui.close', (_$, _e) => ({ value: undefined }))
  on('ui.status', (_$, e) => {
    opts.status?.(e.text)
    return { value: undefined }
  })
  on('ui.toast', (_$, e) => {
    opts.toast?.(e.text)
    return { value: undefined }
  })
  return clock
}

export function paneProps(bodyColumns: number, placement: 'dock' | 'inline' = 'dock') {
  return {
    title: 'MatrixKoder',
    isFocused: false,
    bodyColumns,
    placement,
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  }
}
