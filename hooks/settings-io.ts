// The Settings tab's live layer (plan settings-tab S3.1/S3.2, contracts C1–C3):
// every edge the tab needs, with the edges injected so tests stub them — `run`
// (the host spawn, same argv + timeout style as dashboard pollUsage),
// `configList`/`configSet` ($.config). Nothing here touches $ or reads atoms:
// dashboard.tsx owns the $ side, settings.tsx owns the render; this module is
// the pure middle (argv shapes, C1 JSON parsing, the live SettingsView
// builder, the ordered save). S1 (scripts/mk-config.py) is not merged into
// this lane, so every C1 behaviour below is coded against the contract text,
// never a real script: `list --json` / `get` / `set` / `migrate` print one
// JSON line on stdout; `set` to an unchanged value answers
// `{"ok":true,"changed":false}` with no write; writes back up (.bak) first,
// the .bak a one-step undo ("backup_of":"previous").
//
// Lives here because $ may not cross an import: dashboard imports these
// helpers and hands them its own $ closures; settings.tsx imports only the
// save-payload types plus the two pure helpers the render needs
// (tabsVisibilityToHidden for the payload, settingsInputKeys for the carried
// MEDIUM focus rule). The reverse import (this file reads SettingsView from
// settings.tsx) is type-only, so there is no runtime cycle.
import type {
  SettingsAbout,
  SettingsField,
  SettingsGroup,
  SettingsScope,
  SettingsScopeId,
  SettingsValue,
  SettingsView,
} from './tabs/settings'

// ---------- injected edges ----------

/** One host spawn: argv + a timeout, resolving the run's streams. */
export type RunFn = (
  argv: readonly string[],
  init?: { timeoutMs?: number },
) => Promise<{ exitCode: number; stdout: string; stderr: string }>

/** A $.config.list() row, narrowed to what the tab needs. */
export type ConfigRowLite = { key: string; value: SettingsValue; isLocked: boolean }

/** One $.config.set call: the full (prefixed) key and the value. */
export type ConfigSetFn = (key: string, value: SettingsValue) => Promise<{ deny?: string }>

// ---------- argv (always --data: from a lane mk-config.py would resolve the
// data dir to the main checkout, which already edited real yaml once) ----------

/** This plugin's config CLI, addressed from the plugin root like pollUsage. */
const MK_CONFIG_SCRIPT = 'scripts/mk-config.py'

/** The spawn timeout for a local yaml read/write (pollUsage takes 120 s for
 *  its network call; a local script answers far faster). */
const MK_CONFIG_TIMEOUT_MS = 60_000

/** `python3 <root>/scripts/mk-config.py --data <dataDir> <cmd...>` — the
 *  --data flag is unconditional, never left for the script to default. */
export function mkConfigArgv(pluginRoot: string, dataDir: string, ...cmd: string[]): string[] {
  return ['python3', `${pluginRoot}/${MK_CONFIG_SCRIPT}`, '--data', dataDir, ...cmd]
}

/** Run argv and take the one JSON line on stdout. The real CLI reports a
 *  refusal as exit 2 WITH the reason on stdout
 *  (`{"ok":false,"error":"repo.lanes.slots must be between 1 and 6: 99"}`, empty
 *  stderr) — so stdout is parsed before the exit code is looked at: a JSON
 *  `ok:false` throws its `error` text, a JSON `ok:true` stands whatever the
 *  exit. Only with no JSON on stdout does the exit code speak (`<cmd> exit N:
 *  <stderr>`); exit 0 with no JSON is `printed no JSON`. */
async function runJson(run: RunFn, argv: readonly string[]): Promise<unknown> {
  const r = await run(argv, { timeoutMs: MK_CONFIG_TIMEOUT_MS })
  const cmd = argv.find(a => a === 'migrate' || a === 'list' || a === 'get' || a === 'set') ?? argv[argv.length - 1] ?? '?'
  const parsed = tryJson(r.stdout)
  if (parsed !== undefined) {
    const ok = okOf(parsed)
    if (ok.ok) return parsed
    throw new Error(ok.error)
  }
  if (r.exitCode !== 0) {
    const err = r.stderr.trim()
    throw new Error(err !== '' ? `${cmd} exit ${r.exitCode}: ${err}` : `${cmd} exit ${r.exitCode}`)
  }
  throw new Error(`${cmd} printed no JSON`)
}

/** stdout's JSON value, or undefined when it holds none (empty or torn). */
function tryJson(stdout: string): unknown {
  try {
    return JSON.parse(stdout.trim()) as unknown
  } catch {
    return undefined
  }
}

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)

/** A C1 answer with ok:true (set's unchanged-value reply carries
 *  changed:false; it still counts as saved, never as an error). */
function okOf(ans: unknown): { ok: true } | { ok: false; error: string } {
  if (isRecord(ans) && ans.ok === true) return { ok: true }
  const err = isRecord(ans) ? ans.error : undefined
  return { ok: false, error: typeof err === 'string' && err !== '' ? err : 'not ok' }
}

// ---------- S3.1: tab open — migrate once per session, then list --json ----------

/** One C1 `list --json` field, as the CLI prints it (scope widens: unknown
 *  scopes are skipped by the view builder, never crash it). */
export type YamlFieldIn = {
  key: unknown
  scope: unknown
  /** C8: where the value lives (userConfig|yaml), beside the UI-tab `scope`. */
  home?: unknown
  /** C8: global (one value for the user) | project (per project). */
  level?: unknown
  label: unknown
  type: unknown
  value: unknown
  default: unknown
  min?: unknown
  max?: unknown
  unit?: unknown
  opts?: unknown
  /** The models `set <key>` accepts: the pool the Models picker offers. */
  pool?: unknown
  help: unknown
  editable: unknown
  why?: unknown
}

/** Load the yaml rows: `migrate` first (unless alreadyMigrated — once per
 *  session), then `list --json`. Both carry --data. Throws a short Error. */
export async function loadYaml(
  run: RunFn,
  pluginRoot: string,
  dataDir: string,
  alreadyMigrated: boolean,
): Promise<{ file: string; fields: YamlFieldIn[]; migrated: boolean }> {
  if (!alreadyMigrated) await runMigrate(run, pluginRoot, dataDir)
  const { file, fields } = await runListFields(run, pluginRoot, dataDir)
  return { file, fields, migrated: true }
}

/** One `migrate` run: throws the CLI's own `error` text on refusal. Split
 *  from the list so the caller can record `migrated` before listing (a
 *  failing list must not rerun the migrate). */
export async function runMigrate(run: RunFn, pluginRoot: string, dataDir: string): Promise<void> {
  try {
    const ok = okOf(await runJson(run, mkConfigArgv(pluginRoot, dataDir, 'migrate')))
    if (!ok.ok) throw new Error(ok.error)
  } catch (e) {
    const m = e instanceof Error ? e.message : String(e)
    throw new Error(m.startsWith('migrate') ? m : `migrate: ${m}`)
  }
}

/** One `list --json` run: throws when the answer carries no fields. */
export async function runListFields(
  run: RunFn,
  pluginRoot: string,
  dataDir: string,
): Promise<{ file: string; fields: YamlFieldIn[] }> {
  const ans = await runJson(run, mkConfigArgv(pluginRoot, dataDir, 'list', '--json'))
  if (!isRecord(ans) || ans.ok !== true || !Array.isArray(ans.fields)) {
    throw new Error('list answered no fields')
  }
  return {
    file: typeof ans.file === 'string' ? ans.file : '',
    fields: ans.fields.filter(isRecord).map(f => f as unknown as YamlFieldIn),
  }
}

// ---------- S3.1: save — one set per dirty key, in order, stop at first ok:false ----------

/** One dirty yaml row for the save: a list-typed row (or picks.*) whose edit
 *  is a comma string is encoded as a JSON array; an array edit as-is. */
export type DirtySet = { key: string; value: SettingsValue; list: boolean }

/** A comma-string edit as the JSON array C1's set takes for a list value. */
export function encodeSetValue(value: SettingsValue, list: boolean): string {
  if (Array.isArray(value)) return JSON.stringify([...value])
  if (list && typeof value === 'string') {
    return JSON.stringify(value.split(',').map(s => s.trim()).filter(s => s !== ''))
  }
  return JSON.stringify(value)
}

/** Save the dirty yaml rows in order: one `set` per key (VALUE is JSON);
 *  the first `ok:false` stops the rest and its error is the message. An
 *  `ok:true` reply counts as saved whether or not it changed anything (C1's
 *  unchanged-value reply carries changed:false with no write — still saved).
 *  Every write the CLI makes backs up (.bak) first; the message names that. */
export async function saveYaml(
  run: RunFn,
  pluginRoot: string,
  dataDir: string,
  dirty: readonly DirtySet[],
): Promise<{ saved: number; error?: string }> {
  let saved = 0
  for (const d of dirty) {
    let ans: unknown
    const formatErr = (err: string): string => (err.startsWith(d.key) ? err : `${d.key}: ${err}`)
    try {
      ans = await runJson(run, mkConfigArgv(pluginRoot, dataDir, 'set', d.key, encodeSetValue(d.value, d.list)))
    } catch (e) {
      return { saved, error: formatErr(e instanceof Error ? e.message : String(e)) }
    }
    const ok = okOf(ans)
    if (!ok.ok) return { saved, error: formatErr(ok.error) }
    saved++
  }
  return { saved }
}

// ---------- S3.1: global rows — suffix match, looked-up prefix, isLocked read-only ----------

/** One known userConfig row (C2's four additions plus the six that were
 *  already there): matched against $.config.list() by key SUFFIX — the full
 *  key (`<plugin>.<field>`) is looked up, never hard-coded. */
type GlobalRowDef = {
  readonly suffix: string
  readonly scope: SettingsScopeId
  readonly label: string
  readonly kind: 'bool' | 'num' | 'seg' | 'select' | 'text'
  readonly unit?: string
  readonly opts?: readonly (readonly [string, string])[]
  readonly help: string
}

const TAB_OPTS: readonly (readonly [string, string])[] = [
  ['overview', ''],
  ['lanes', ''],
  ['plan', ''],
  ['usage', ''],
  ['events', ''],
  ['waiting', ''],
  ['old', ''],
  ['settings', 'this tab'],
]

/** The ten global rows the tab draws (fixture wording, so the render keeps
 *  its shape against live values). */
export const GLOBAL_ROWS: readonly GlobalRowDef[] = [
  { suffix: 'mode', scope: 'mode', label: 'Operating mode', kind: 'seg', opts: [['solo', 'one session'], ['orchestrator', 'one repo'], ['hive', 'many repos']], help: 'Who builds: this session, one repo, or many.' },
  { suffix: 'defaultTab', scope: 'tabs', label: 'Open on', kind: 'select', opts: TAB_OPTS, help: 'The tab the pane opens on.' },
  { suffix: 'hiddenTabs', scope: 'tabs', label: 'Hidden tabs', kind: 'text', help: 'Comma list of tabs to hide (Settings never hidden).' },
  { suffix: 'raster', scope: 'tabs', label: 'Raster bars', kind: 'bool', help: 'Off = eighth-block fallback.' },
  { suffix: 'wake', scope: 'about', label: 'Wake on events', kind: 'bool', help: 'Submit a prompt when a lane needs attention.' },
  { suffix: 'guards', scope: 'about', label: 'Guards', kind: 'bool', help: 'Deny destructive git, banned commands.' },
  { suffix: 'usagePollMin', scope: 'about', label: 'Usage poll', kind: 'num', unit: 'm', help: 'Minutes between usage polls.' },
  { suffix: 'autoOpen', scope: 'about', label: 'Auto-open pane', kind: 'bool', help: 'Open the pane when a session starts.' },
  { suffix: 'chip', scope: 'about', label: 'Status chip', kind: 'bool', help: 'Run health chip under the prompt.' },
  { suffix: 'toasts', scope: 'about', label: 'Toasts', kind: 'bool', help: 'Toast on task status transitions.' },
]

/** The full $.config key for a suffix: the exact key when listed, else the
 *  first key ending in `.suffix` (shortest wins — deterministic when two
 *  plugins expose one). Null when no row carries the suffix. */
export function matchGlobalKey(listedKeys: readonly string[], suffix: string): string | null {
  if (listedKeys.includes(suffix)) return suffix
  const hits = listedKeys.filter(k => k.endsWith(`.${suffix}`)).sort((a, b) => a.length - b.length)
  return hits[0] ?? null
}

/** One dirty global row (suffix, not the full key — the prefix is looked up
 *  again at save time, so a relabelled config still writes). */
export type DirtyGlobal = { suffix: string; value: SettingsValue }

/** One dirty tabs-visibility bool: folded into a single hiddenTabs write. */
export type VisibilityEdit = { tab: string; on: boolean }

/** What the tab's save press hands the dashboard: yaml sets, global sets and
 *  visibility edits (hive edits ride along in view state and are cleared on
 *  success — they have no live home yet). */
export type SavePayload = {
  readonly sets: readonly DirtySet[]
  readonly globals: readonly DirtyGlobal[]
  readonly visibility: readonly VisibilityEdit[]
}

/** The seven hideable tabs (Settings never hides), in bar order. */
export const TAB_VISIBILITY: readonly { id: string; hotkey: string; label: string; help: string }[] = [
  { id: 'overview', hotkey: 'o', label: 'o Overview', help: 'The run at a glance.' },
  { id: 'lanes', hotkey: 'l', label: 'l Lanes', help: 'One row per builder lane.' },
  { id: 'plan', hotkey: 'p', label: 'p Plan', help: 'Waves and phases.' },
  { id: 'usage', hotkey: 'u', label: 'u Usage', help: 'Quota bars per agent.' },
  { id: 'events', hotkey: 'e', label: 'e Events', help: 'The run’s event log.' },
  { id: 'waiting', hotkey: 'w', label: 'w Waiting', help: 'Decisions open on you.' },
  { id: 'old', hotkey: '0', label: '0 Old', help: 'Retired later.' },
]

/** A hiddenTabs value as a set of tab ids (keys or labels, like the
 *  dashboard's hiddenTabs — unknown names ignored, Settings never hidden). */
function parseHiddenTabs(value: unknown): Set<string> {
  const out = new Set<string>()
  if (typeof value !== 'string') return out
  for (const part of value.split(',')) {
    const name = part.trim().toLowerCase()
    if (name === '') continue
    const tab = TAB_VISIBILITY.find(t => t.id === name || t.label.toLowerCase() === name)
    if (tab != null) out.add(tab.id)
  }
  return out
}

/** Visibility edits folded back into a hiddenTabs string (bar order, so the
 *  write is canonical). Pure — the tab and the tests share it. */
export function tabsVisibilityToHidden(current: unknown, edits: readonly VisibilityEdit[]): string {
  const hidden = parseHiddenTabs(current)
  for (const e of edits) {
    if (TAB_VISIBILITY.every(t => t.id !== e.tab)) continue
    if (e.on) hidden.delete(e.tab)
    else hidden.add(e.tab)
  }
  return TAB_VISIBILITY.map(t => t.id).filter(id => hidden.has(id)).join(',')
}

/** Save the global rows through $.config: the visibility edits fold into one
 *  hiddenTabs write (skipped when unchanged), then one set per dirty global
 *  in order. The fold reads the CURRENT hiddenTabs from the same fresh
 *  `$.config.list()` — never a stored view — so an outside change is not
 *  overwritten. A `{deny}` stops the rest and its reason is the message —
 *  shown, like the yaml error. An unknown suffix (nothing listed it) is an
 *  error. */
export async function saveGlobals(
  configList: () => Promise<ConfigRowLite[]>,
  configSet: ConfigSetFn,
  globals: readonly DirtyGlobal[],
  visibility: readonly VisibilityEdit[],
): Promise<{ saved: number; error?: string }> {
  let saved = 0
  const rows = await configList()
  const fullOf = (suffix: string): string | null =>
    matchGlobalKey(rows.map(r => r.key), suffix)
  if (visibility.length > 0) {
    const hiddenFull = fullOf('hiddenTabs')
    const hiddenRow = hiddenFull == null ? undefined : rows.find(r => r.key === hiddenFull)
    const next = tabsVisibilityToHidden(typeof hiddenRow?.value === 'string' ? hiddenRow.value : '', visibility)
    if (next !== (typeof hiddenRow?.value === 'string' ? hiddenRow.value : '')) {
      if (hiddenFull == null) return { saved, error: 'hiddenTabs: no such setting' }
      const out = await configSet(hiddenFull, next)
      if (out.deny != null) return { saved, error: out.deny }
      saved++
    }
  }
  for (const g of globals) {
    const full = fullOf(g.suffix)
    if (full == null) return { saved, error: `${g.suffix}: no such setting` }
    const out = await configSet(full, g.value)
    if (out.deny != null) return { saved, error: out.deny }
    saved++
  }
  return { saved }
}

// ---------- S3.2: About — version, latest tag, What's new, helpers ----------

/** plugin.json as installed: the version plus every userConfig default (the
 *  defaults the global rows draw their `default X` tails from). */
export function parsePluginManifest(text: string): { version: string; defaults: Record<string, SettingsValue> } {
  let v: unknown = null
  try {
    v = JSON.parse(text) as unknown
  } catch {
    return { version: 'unknown', defaults: {} }
  }
  const rec = isRecord(v) ? v : {}
  const version = typeof rec.version === 'string' && rec.version !== '' ? rec.version : 'unknown'
  const defaults: Record<string, SettingsValue> = {}
  const userConfig = isRecord(rec.userConfig) ? rec.userConfig : {}
  for (const [k, f] of Object.entries(userConfig)) {
    if (!isRecord(f) || !('default' in f)) continue
    const d = (f as Record<string, unknown>).default
    if (typeof d === 'string' || typeof d === 'number' || typeof d === 'boolean') defaults[k] = d
  }
  return { version, defaults }
}

/** The highest tag line: `git tag --sort=-v:refname` prints highest first —
 *  the first non-blank line, its leading `v` stripped. */
export function firstTagLine(stdout: string): string {
  for (const line of stdout.split('\n')) {
    const t = line.trim()
    if (t !== '') return t.replace(/^v/, '')
  }
  return ''
}

/** The top CHANGELOG.md entry's bullet lines: bullets under the first `## `
 *  heading until the next one. kind follows the `### ` section (Added → feat,
 *  Fixed → fix, anything else → feat). The `- ` marker is stripped; the line
 *  keeps its own scope marks. */
export function topChangelogEntry(text: string): SettingsAbout['whatsNew'] {
  const out: { kind: 'feat' | 'fix'; line: string }[] = []
  let inTop = false
  let kind: 'feat' | 'fix' = 'feat'
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (line.startsWith('## ')) {
      if (!inTop) inTop = true
      else break
      continue
    }
    if (!inTop) continue
    const section = /^###\s+(.+?)\s*$/.exec(line)?.[1]?.toLowerCase() ?? null
    if (section != null) {
      kind = section.startsWith('fix') ? 'fix' : 'feat'
      continue
    }
    const bullet = /^-\s+(.+?)\s*$/.exec(line)?.[1]
    if (bullet != null && bullet !== '') out.push({ kind, line: bullet })
  }
  return out
}

// ---------- the live SettingsView (same shape the fixture built for) ----------

/** The yaml rows' file label before the first live load (fixture / tests):
 *  the basename, not a repo-specific spelling. The live load replaces it with
 *  the real path `list --json` prints (`file`, L7.1). */
const YAML_FILE_LABEL = 'project.yaml'

/** The eight scopes, in hotkey order. Every yaml-backed scope (project,
 *  routing, agents, models, child) carries YAML_FILE_LABEL as its `file`, so
 *  the live load can swap in the REAL path `list --json` prints (L7.1) and the
 *  scope-note line always names where the rows really live. `mode`, `tabs` and
 *  `about` are userConfig/plugin scopes and keep their own labels. */
const SCOPES: readonly SettingsScope[] = [
  { id: 'mode', label: 'Mode', file: 'plugin.json', note: 'all projects' },
  { id: 'project', label: 'Project', file: YAML_FILE_LABEL, note: 'this repo' },
  { id: 'routing', label: 'Routing', file: YAML_FILE_LABEL, note: 'routing_preferences' },
  { id: 'agents', label: 'Agents', file: YAML_FILE_LABEL, note: 'agents · usage' },
  { id: 'models', label: 'Models', file: YAML_FILE_LABEL, note: 'agents.*.models' },
  { id: 'child', label: 'Child', file: YAML_FILE_LABEL, note: 'lanes.child' },
  { id: 'tabs', label: 'Tabs', file: 'userConfig', note: 'dashboard' },
  { id: 'about', label: 'About', file: 'plugin.json', note: 'read-only' },
]

type GroupDef = { title: string; right?: string; prefixes: readonly string[] }

/** The artboard's groups as C1 key prefixes (the CLI's fields carry no group —
 *  the tab does). A field lands in the first group whose prefix it starts
 *  with; a field no group claims gets a trailing group named for its scope. */
const YAML_GROUPS: Record<'project' | 'routing' | 'agents' | 'child', readonly GroupDef[]> = {
  project: [
    { title: 'lanes', right: 'repo.lanes', prefixes: ['repo.lanes.', 'repo.integration_branch'] },
    { title: 'qa & autonomy', prefixes: ['qa.', 'autonomy_contract.'] },
  ],
  routing: [
    { title: 'policy', right: 'routing_preferences', prefixes: ['routing_preferences.policy', 'routing_preferences.builder_order', 'routing_preferences.max_build_lanes'] },
    { title: 'reserve floor', right: '% left', prefixes: ['routing_preferences.reserve_floor.'] },
    { title: 'review', prefixes: ['routing_preferences.reviewer', 'routing_preferences.visual_reviewer'] },
  ],
  agents: [
    { title: 'builders', right: 'agents.*.available', prefixes: ['agents.'] },
    { title: 'usage', right: 'usage', prefixes: ['usage.'] },
  ],
  child: [
    { title: 'builder sessions', right: 'session', prefixes: ['session.fresh_session', 'session.subagents', 'session.subagent_model'] },
    { title: 'watch', prefixes: ['session.stall_min', 'session.max_wait_min'] },
    { title: 'reviewer panes', prefixes: ['session.reviewer_split', 'session.reuse_reviewer'] },
  ],
}

const KNOWN_HELPERS: readonly { id: string; label: string; help: string }[] = [
  { id: 'herdr', label: 'herdr', help: 'Pane and worktree manager.' },
  { id: 'jev', label: 'Jev', help: 'Judgement gates for routing.' },
  { id: 'context_mode', label: 'context-mode', help: 'Context compression for big outputs.' },
  { id: 'codebase_memory', label: 'codebase-memory', help: 'Serena-style project memory.' },
  { id: 'canary', label: 'canary', help: 'Browser QA recordings.' },
]

/** The helper probe states the About scope draws: a probed value outside
 *  these still draws — the row falls back to text, never crashes. The
 *  fallback's note is empty so the kit's seg display-word translation keeps
 *  the value itself: a read-only row draws `not probed`, not a short word. */
const HELPER_OPTS: readonly (readonly [string, string])[] = [['ok', 'ok'], ['off', 'off'], ['not probed', '']]

const MODES: SettingsView['modes'] = [
  { id: 'solo', name: 'Solo', scope: 'one session', desc: 'MK without orchestration: dashboard, usage, guards, state. You build here.' },
  { id: 'orchestrator', name: 'Orchestrator', scope: 'one repo', desc: 'Full MK loop on this repo: plan, lanes, builders, reviews, merge.' },
  { id: 'hive', name: 'Hive', scope: 'many repos', desc: 'One MK coordinates several repos: shared quota, lanes and runs.' },
]

/** The agent cards the Models scope draws: who they are and where they run.
 *  Their model options are NOT listed here — each card's options are its
 *  `picks.<agent>` row's pool, the same list `set` accepts (below) — and
 *  whether a card draws at all is the live `agents.<id>.available` row, not a
 *  flag here: a flag can only contradict the Agents scope beside it. */
type AgentCard = { id: string; name: string; provider: string }

const AGENT_CARDS: readonly AgentCard[] = [
  { id: 'opencode', name: 'OpenCode', provider: 'opencode · opencode-go · ollama-cloud' },
  { id: 'claude', name: 'Claude Code', provider: 'anthropic subscription' },
  { id: 'agy', name: 'AGY', provider: 'google' },
  { id: 'codex', name: 'Codex', provider: 'openai' },
  { id: 'omp', name: 'omp', provider: 'pi' },
]

const DEFAULT_PICKS: readonly { agent: string; models: readonly string[] }[] = [
  { agent: 'opencode', models: ['ollama-cloud/minimax-m3', 'opencode/step-5-preview-free', 'opencode/space-bunny-free'] },
  { agent: 'claude', models: ['claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-opus-5-5', 'claude-fable-5-1'] },
  { agent: 'agy', models: [] },
  { agent: 'codex', models: [] },
  { agent: 'omp', models: ['manifest/auto'] },
]

/** The models an agent's picker may offer: exactly its picks row's `pool` —
 *  the list `mk-config.py set picks.<agent>` validates against. No row, no
 *  pool, no options: a card never falls back to a catalogue `set` would refuse. */
function poolOf(f: YamlFieldIn | undefined): readonly string[] {
  const raw = f?.pool
  if (!Array.isArray(raw)) return []
  return raw.filter((x): x is string => typeof x === 'string' && x !== '')
}

/** Whether this tab may route an agent: its live `agents.<id>.available` row, absent = yes (a yaml
 *  written before the row existed). The same reading `ModelsCards` takes for the `off` line, so the
 *  two scopes cannot disagree about one agent. */
function isAvailable(f: YamlFieldIn | undefined): boolean {
  return f == null || toSettingsValue(f.value) !== false
}

/** A C1 unknown as the row value the tab draws (scalars pass, string lists
 *  pass, anything else stringifies — the render never crashes on it). */
function toSettingsValue(v: unknown): SettingsValue {
  if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return v
  if (Array.isArray(v)) return v.filter((x): x is string => typeof x === 'string')
  return String(v)
}

const FIELD_TYPES = new Set(['bool', 'num', 'seg', 'select', 'text', 'list'])
const YAML_SCOPES: readonly string[] = ['project', 'routing', 'agents', 'child']

/** A C1 `[[value, note]]` pair list as the tab's opts (anything else → none). */
function toOpts(v: unknown): readonly (readonly [string, string])[] | undefined {
  if (!Array.isArray(v)) return undefined
  const out: (readonly [string, string])[] = []
  for (const o of v) {
    if (Array.isArray(o) && typeof o[0] === 'string') {
      out.push([o[0], typeof o[1] === 'string' ? o[1] : ''] as const)
    }
  }
  return out.length > 0 ? out : undefined
}

/** A raw C1 field as the tab's row (null when the shape is not a row the tab
 *  draws — wrong scope or type — so a backend addition never breaks the tab). */
function toRow(f: YamlFieldIn, scope: SettingsScopeId): SettingsField | null {
  if (typeof f.key !== 'string' || typeof f.label !== 'string' || typeof f.help !== 'string') return null
  if (typeof f.type !== 'string' || !FIELD_TYPES.has(f.type)) return null
  return {
    key: f.key,
    scope,
    label: f.label,
    type: f.type as SettingsField['type'],
    value: toSettingsValue(f.value),
    default: toSettingsValue(f.default),
    min: typeof f.min === 'number' ? f.min : undefined,
    max: typeof f.max === 'number' ? f.max : undefined,
    unit: typeof f.unit === 'string' ? f.unit : undefined,
    opts: toOpts(f.opts),
    help: f.help,
    editable: f.editable !== false,
    why: typeof f.why === 'string' ? f.why : undefined,
  }
}

/** Build the live view: yaml fields grouped into the artboard's groups,
 *  `picks.*` fields overlaid on the catalogue defaults, globals matched by
 *  suffix (isLocked → read-only), About from the probed data. Fields the
 *  roster does not know land in a trailing group named for their scope, so a
 *  schema addition still draws. mock is false — the MOCK row stays off.
 *
 *  `file` is the LIVE yaml path from `list --json` (L7.1: the Project/Routing/
 *  Agents rows name the real `<home>/projects/<slug>/project.yaml`, never a
 *  hard-coded spelling); empty keeps the shape label for the fixture and the
 *  pure tests. */
export function buildSettingsView(args: {
  fields: YamlFieldIn[]
  file: string
  configRows: ConfigRowLite[]
  globalDefaults: Record<string, SettingsValue>
  about: { installed: string; latest: string; whatsNew: SettingsAbout['whatsNew'] }
}): SettingsView {
  const { fields, configRows, globalDefaults, about } = args
  const yamlFile = args.file !== '' ? args.file : YAML_FILE_LABEL
  // the yaml-backed scope notes name the REAL path (L7.1); the others keep theirs.
  const scopes = SCOPES.map(s => (s.file === YAML_FILE_LABEL ? { ...s, file: yamlFile } : s))
  const byKey = new Map<string, YamlFieldIn>()
  for (const f of fields) {
    if (typeof f.key === 'string' && !byKey.has(f.key)) byKey.set(f.key, f)
  }

  // yaml rows, grouped (picks.* and the Models scope are built from the pool below, so
  // a models-scope field the roster cannot place is ignored rather than drawn twice)
  const grouped = new Map<string, SettingsField[]>()
  const pushRow = (scope: string, row: SettingsField): void => {
    const list = grouped.get(scope) ?? []
    list.push(row)
    grouped.set(scope, list)
  }
  for (const f of fields) {
    if (typeof f.key !== 'string') continue
    if (f.key.startsWith('picks.')) continue
    const scope = f.key === 'lanes.default_agent' ? 'child' : f.scope
    if (typeof scope !== 'string' || !YAML_SCOPES.includes(scope)) continue
    const row = toRow(f, scope as SettingsScopeId)
    if (row == null) continue
    let drawn = row
    if (row.key === 'lanes.default_agent') {
      const avail = fields
        .filter((a): a is YamlFieldIn & { key: string } => typeof a.key === 'string' && a.key.startsWith('agents.') && a.key.endsWith('.available') && a.value !== false)
        .map(a => a.key.slice('agents.'.length, -'.available'.length))
        .filter(id => id !== 'herdr') // herdr runs the panes; it is not a coding agent
      const ids = [...new Set(['omp', ...avail])]
      drawn = { ...row, type: 'select', opts: ids.map(id => [id, ''] as [string, string]) }
    }
    pushRow(scope, drawn)
  }
  const groups: SettingsGroup[] = []
  for (const [scope, defs] of Object.entries(YAML_GROUPS) as [keyof typeof YAML_GROUPS, readonly GroupDef[]][]) {
    const rows = grouped.get(scope) ?? []
    const rest = [...rows]
    for (const def of defs) {
      const mine = rest.filter(r => def.prefixes.some(p => r.key === p || r.key.startsWith(p)))
      if (mine.length === 0) continue
      for (const m of mine) rest.splice(rest.indexOf(m), 1)
      groups.push({ scope, title: def.title, ...(def.right != null ? { right: def.right } : {}), rows: mine })
    }
    if (rest.length > 0) {
      const label = SCOPES.find(s => s.id === scope)?.label ?? scope
      groups.push({ scope, title: label.toLowerCase(), rows: rest })
    }
  }

  // global rows: the looked-up full key's value, isLocked read-only
  const keys = configRows.map(r => r.key)
  const globalRow = (suffix: string): SettingsField | null => {
    const def = GLOBAL_ROWS.find(d => d.suffix === suffix)
    const full = def == null ? null : matchGlobalKey(keys, suffix)
    if (def == null || full == null) return null
    const row = configRows.find(r => r.key === full)
    if (row == null) return null
    return {
      key: suffix,
      scope: def.scope,
      label: def.label,
      type: def.kind,
      value: row.value,
      default: globalDefaults[suffix] ?? row.value,
      unit: def.unit,
      opts: def.opts,
      help: def.help,
      editable: !row.isLocked,
      ...(row.isLocked ? { why: 'Read-only: locked by policy' } : {}),
      global: true,
    }
  }
  const globalRows = (suffixes: readonly string[]): SettingsField[] => {
    const out: SettingsField[] = []
    for (const s of suffixes) {
      const r = globalRow(s)
      if (r != null) out.push(r)
    }
    return out
  }

  // tabs: visibility bools derived from the live hiddenTabs (they rewrite it),
  // then the three C2 globals. A locked hiddenTabs locks the bools too.
  const hiddenRow = configRows.find(r => r.key === matchGlobalKey(keys, 'hiddenTabs'))
  const hiddenLocked = hiddenRow?.isLocked === true
  const hidden = parseHiddenTabs(typeof hiddenRow?.value === 'string' ? hiddenRow.value : '')
  groups.push({
    scope: 'tabs',
    title: 'visible tabs',
    right: 'dashboard.tabs',
    rows: TAB_VISIBILITY.map(t => ({
      key: `tabs.${t.id}`,
      scope: 'tabs' as const,
      label: t.label,
      type: 'bool' as const,
      value: !hidden.has(t.id),
      default: true as const,
      help: t.help,
      editable: !hiddenLocked,
      ...(hiddenLocked ? { why: 'Read-only: locked by policy' } : {}),
    })),
  })
  const paneRows = globalRows(['defaultTab', 'hiddenTabs', 'raster'])
  if (paneRows.length > 0) groups.push({ scope: 'tabs', title: 'pane', right: 'dashboard', rows: paneRows })
  const modeRows = globalRows(['mode'])
  if (modeRows.length > 0) groups.push({ scope: 'mode', title: 'operating mode', right: 'radio', rows: modeRows })

  // about: the six userConfig rows, then the helper probes (absent → not probed)
  const pluginRows = globalRows(['wake', 'guards', 'usagePollMin', 'autoOpen', 'chip', 'toasts'])
  if (pluginRows.length > 0) groups.push({ scope: 'about', title: 'plugin options', right: 'userConfig', rows: pluginRows })
  groups.push({
    scope: 'about',
    title: 'helpers',
    right: 'probe result',
    rows: KNOWN_HELPERS.map(h => {
      const f = byKey.get(`helpers.${h.id}`)
      const v = typeof f?.value === 'string' ? f.value : 'not probed'
      return {
        key: `helpers.${h.id}`,
        scope: 'about' as const,
        label: h.label,
        type: 'seg' as const,
        value: v,
        default: v,
        opts: HELPER_OPTS,
        help: h.help,
        editable: false,
        why: 'Read-only: probed at preflight',
      }
    }),
  })

  // picks.* overlay the defaults shown before any save, filtered to the same pool the picker
  // offers — a default `set picks.<agent>` would refuse is not shown as a choice either
  const picks = DEFAULT_PICKS.map(p => {
    const f = byKey.get(`picks.${p.agent}`)
    const live = f != null && Array.isArray(f.value)
      ? (f.value as unknown[]).filter((x): x is string => typeof x === 'string').slice(0, 4)
      : null
    const pool = poolOf(f)
    return { agent: p.agent, models: live ?? p.models.filter(m => pool.includes(m)), editable: f != null }
  })

  return {
    mock: false,
    scopes,
    groups,
    modes: MODES,
    // L7.1: the hive card listed projects from the retired projects.json; the live
    // view has no hive source (the root store's projects are the DB's, not this tab's),
    // so it draws no stale entries — the card renders only when there is nothing to show.
    projects: [],
    agents: AGENT_CARDS
      .filter(c => isAvailable(byKey.get(`agents.${c.id}.available`)))
      .map(c => ({ ...c, off: false, models: poolOf(byKey.get(`picks.${c.id}`)).map(name => ({ name, note: '' })) })),
    picks,
    about: { installed: about.installed, latest: about.latest, latestNote: '', commits: about.whatsNew.length, whatsNew: about.whatsNew },
  }
}

// ---------- view-state keys the dashboard clears on a successful save ----------

/** A successful save folded back into the stored view, so the rows keep
 *  showing what was just written (the edits clear, and dirty-tracking
 *  compares against the loaded value). Yaml sets land on their rows (a
 *  comma-string list edit becomes the array the row draws), globals on
 *  theirs, visibility edits recompute the tabs.* bools, picks.* sets the
 *  pick order. */
export function applySavedView(view: SettingsView, payload: SavePayload): SettingsView {
  const sets = new Map(payload.sets.map(s => [s.key, s.value]))
  const globals = new Map(payload.globals.map(g => [g.suffix, g.value]))
  const hiddenRow = view.groups.flatMap(g => g.rows).find(r => r.key === 'hiddenTabs')
  const hiddenNext = payload.visibility.length > 0
    ? new Set(tabsVisibilityToHidden(typeof hiddenRow?.value === 'string' ? hiddenRow.value : '', payload.visibility).split(',').filter(s => s !== ''))
    : null
  const rowValue = (r: SettingsField): SettingsValue => {
    const s = sets.get(r.key)
    if (s !== undefined) {
      if (r.type === 'list' && typeof s === 'string') {
        return s.split(',').map(x => x.trim()).filter(x => x !== '')
      }
      return s
    }
    if (r.global === true) {
      const g = globals.get(r.key)
      if (g !== undefined) return g
    }
    if (hiddenNext != null && r.scope === 'tabs' && r.key.startsWith('tabs.')) {
      return !hiddenNext.has(r.key.slice('tabs.'.length))
    }
    return r.value
  }
  return {
    ...view,
    groups: view.groups.map(g => ({ ...g, rows: g.rows.map(r => ({ ...r, value: rowValue(r) })) })),
    picks: view.picks.map(p => {
      const s = sets.get(`picks.${p.agent}`)
      return Array.isArray(s) ? { agent: p.agent, models: [...s] } : p
    }),
  }
}

/** Every `settings:edit:*` key a view can hold (fields, models picks, hive
 *  toggles): the dashboard deletes these after a clean save, leaving the
 *  footer to the `✓ saved N` line. */
export function savedEditKeys(view: SettingsView): string[] {
  const out: string[] = []
  for (const g of view.groups) for (const r of g.rows) out.push(`settings:edit:${r.key}`)
  for (const p of view.picks) out.push(`settings:edit:models.${p.agent}`)
  for (const pr of view.projects) out.push(`settings:edit:hive.${pr.name}`)
  return out
}

/** The element keys that draw a native Input on this view: the find field
 *  plus every editable text/list row's Input (`set-<key>` in kit.tsx). The
 *  carried MEDIUM rule keys off this set: while the engine's focus ring sits
 *  on one of these, every printable key already reaches the Input alone (the
 *  InputProps contract), so the tab drops its own letter/digit hotkeys too. */
export function settingsInputKeys(view: SettingsView): Set<string> {
  const out = new Set<string>(['set-find-input'])
  for (const g of view.groups) {
    for (const r of g.rows) {
      if ((r.type === 'text' || r.type === 'list') && r.editable) out.add(`set-${r.key}`)
    }
  }
  return out
}
