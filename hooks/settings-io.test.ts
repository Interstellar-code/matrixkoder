// Tests for the Settings tab's live layer (plan settings-tab S3.1/S3.2): the
// real settings-io.ts driven with stubbed edges only — a stub `run` answers
// canned C1 JSON lines (and records argv), stub config fns answer canned
// rows. The module itself is never stubbed.
import { test, expect } from 'claude-code/testing'
import {
  applySavedView, buildSettingsView, encodeSetValue, firstTagLine, loadYaml, matchGlobalKey, mkConfigArgv,
  parsePluginManifest, saveGlobals, saveYaml, savedEditKeys, settingsInputKeys,
  tabsVisibilityToHidden, topChangelogEntry,
} from './settings-io'
import type { ConfigRowLite, RunFn, YamlFieldIn } from './settings-io'
import { SETTINGS_FIXTURE, SETTINGS_LIST_FIXTURE } from './tabs/settings.fixture'
import { SETTINGS_SCHEMA } from './tabs/settings.schema'
import type { SettingsField } from './tabs/settings'

type Answer = { exitCode: number; stdout: string; stderr: string }

/** A stub run answering by the mk-config subcommand (argv after --data) and
 *  recording every argv it saw. */
function stubRun(byCmd: (cmd: string[]) => Answer): { run: RunFn; calls: string[][] } {
  const calls: string[][] = []
  const run: RunFn = async argv => {
    calls.push([...argv])
    return byCmd(argv.slice(4))
  }
  return { run, calls }
}

const line = (obj: unknown, exitCode = 0): Answer => ({ exitCode, stdout: `${JSON.stringify(obj)}\n`, stderr: '' })

/** Three C1-shaped rows across two scopes (child watch + project lanes). */
const FIELDS: YamlFieldIn[] = [
  { key: 'session.stall_min', scope: 'child', label: 'Stall after', type: 'num', value: 20, default: 15, min: 5, max: 60, unit: 'm', help: 'No change flags a lane.', editable: true },
  { key: 'session.max_wait_min', scope: 'child', label: 'Give up after', type: 'num', value: 120, default: 30, min: 10, max: 300, unit: 'm', help: 'Give up on a silent lane.', editable: true },
  { key: 'repo.lanes.slots', scope: 'project', label: 'Worktree slots', type: 'num', value: 3, default: 3, min: 1, max: 6, help: 'Reusable worktrees.', editable: true },
]

const LIST = { ok: true, file: '/tmp/x/.matrixkoder/matrixkoder.yaml', fields: FIELDS }

const CONFIG_ROWS: ConfigRowLite[] = [
  { key: 'matrixkoder.mode', value: 'orchestrator', isLocked: false },
  { key: 'matrixkoder.hiddenTabs', value: '', isLocked: false },
  { key: 'matrixkoder.chip', value: true, isLocked: true },
]

const ABOUT = { installed: '0.3.0', latest: '0.3.0', whatsNew: [{ kind: 'feat' as const, line: 'live settings' }] }

// ---------- S3.1: tab open ----------

test('S3.1 load: tab open runs exactly one migrate then one list --json, both with --data', async () => {
  const { run, calls } = stubRun(cmd => (cmd[0] === 'migrate' ? line({ ok: true, changed: [], notes: [] }) : line(LIST)))
  const out = await loadYaml(run, '/plug', '/data', false)
  expect(out.fields.map(f => f.key)).toEqual(['session.stall_min', 'session.max_wait_min', 'repo.lanes.slots'])
  expect(out.file).toBe('/tmp/x/.matrixkoder/matrixkoder.yaml')
  expect(calls).toHaveLength(2)
  expect(calls[0]?.slice(0, 2)).toEqual(['python3', '/plug/scripts/mk-config.py'])
  expect(calls[0]?.slice(2, 4)).toEqual(['--data', '/data'])
  expect(calls[0]?.[4]).toBe('migrate')
  expect(calls[1]?.slice(2)).toEqual(['--data', '/data', 'list', '--json'])
})

test('S3.1 load: an already-migrated session lists without migrating', async () => {
  const { run, calls } = stubRun(() => line(LIST))
  await loadYaml(run, '/plug', '/data', true)
  expect(calls).toHaveLength(1)
  expect(calls[0]?.slice(4)).toEqual(['list', '--json'])
})

test('S3.1 load: a refused migrate and a non-JSON list both throw', async () => {
  // refusals stubbed exactly like the real CLI: exit 2, the JSON on stdout, empty stderr
  const denied = stubRun(cmd => (cmd[0] === 'migrate' ? line({ ok: false, error: 'yaml torn' }, 2) : line(LIST)))
  await expect(loadYaml(denied.run, '/plug', '/data', false)).rejects.toThrow('migrate: yaml torn')
  expect(denied.calls).toHaveLength(1) // the list never ran after the refused migrate
  const garbage = stubRun(cmd => (cmd[0] === 'migrate' ? line({ ok: true }) : { exitCode: 0, stdout: 'not json\n', stderr: '' }))
  await expect(loadYaml(garbage.run, '/plug', '/data', false)).rejects.toThrow('no JSON')
})

test('MEDIUM 1: stdout JSON wins over the exit code — the refusal error, not `exit 2`', async () => {
  // exactly the real CLI's refusal shape: exit 2, JSON on stdout, empty stderr
  const refused = stubRun(() => ({
    exitCode: 2,
    stdout: '{"ok":false,"error":"repo.lanes.slots must be between 1 and 6: 99"}\n',
    stderr: '',
  }))
  const out = await saveYaml(refused.run, '/plug', '/data', [
    { key: 'repo.lanes.slots', value: 99, list: false },
    { key: 'repo.lanes.reuse', value: true, list: false },
  ])
  expect(out).toEqual({ saved: 0, error: 'repo.lanes.slots must be between 1 and 6: 99' })
  expect(refused.calls).toHaveLength(1) // the next key's set never ran
  // no stdout JSON: fall back to the exit code and stderr
  const bare = stubRun(() => ({ exitCode: 1, stdout: '', stderr: 'boom\n' }))
  expect(await saveYaml(bare.run, '/plug', '/data', [{ key: 'a', value: 1, list: false }])).toEqual({
    saved: 0,
    error: 'a: set exit 1: boom',
  })
})

// ---------- S3.1: save ----------

test('S3.1 save: 2 dirty yaml rows → exactly 2 set calls in order; the first ok:false stops the second', async () => {
  const seen: string[][] = []
  const { run } = stubRun(cmd => {
    seen.push(cmd)
    return cmd[1] === 'session.stall_min' ? line({ ok: false, error: 'below min 5' }, 2) : line({ ok: true })
  })
  const out = await saveYaml(run, '/plug', '/data', [
    { key: 'session.stall_min', value: 2, list: false },
    { key: 'session.max_wait_min', value: 60, list: false },
  ])
  expect(out).toEqual({ saved: 0, error: 'session.stall_min: below min 5' })
  expect(seen).toEqual([['set', 'session.stall_min', '2']]) // the second set never ran
})

test('S3.1 save: an unchanged-value reply (ok:true changed:false, no write) counts as saved', async () => {
  const { run, calls } = stubRun(() => line({ ok: true, changed: false }))
  const out = await saveYaml(run, '/plug', '/data', [{ key: 'session.stall_min', value: 20, list: false }])
  expect(out).toEqual({ saved: 1 })
  expect(calls[0]?.slice(4)).toEqual(['set', 'session.stall_min', '20'])
})

test('S3.1 save: list rows encode the comma string as a JSON array, scalars as JSON', () => {
  expect(encodeSetValue('a, b,, c ', true)).toBe('["a","b","c"]')
  expect(encodeSetValue('x', false)).toBe('"x"')
  expect(encodeSetValue(12, false)).toBe('12')
  expect(encodeSetValue(true, false)).toBe('true')
  expect(encodeSetValue(['a', 'b'], true)).toBe('["a","b"]')
})

// ---------- S3.1: globals ----------

test('S3.1 globals: suffix match finds the prefixed key — never hard-coded', () => {
  expect(matchGlobalKey(['matrixkoder.mode'], 'mode')).toBe('matrixkoder.mode')
  expect(matchGlobalKey(['mode'], 'mode')).toBe('mode') // an exact key wins over any prefix
  expect(matchGlobalKey(['matrixkoder.mode'], 'chip')).toBeNull()
  // a tie breaks deterministically (shortest key) rather than by list order
  expect(matchGlobalKey(['aa.mode', 'b.mode'], 'mode')).toBe('b.mode')
  expect(matchGlobalKey(['b.mode', 'aa.mode'], 'mode')).toBe('b.mode')
})

test('S3.1 globals: sets write in order with the looked-up prefix; a {deny} stops the rest and is shown', async () => {
  const rows: ConfigRowLite[] = [
    { key: 'matrixkoder.chip', value: true, isLocked: false },
    { key: 'matrixkoder.toasts', value: true, isLocked: false },
  ]
  const written: [string, unknown][] = []
  const out = await saveGlobals(
    async () => rows,
    async (key, value) => {
      written.push([key, value])
      return key.endsWith('.chip') ? { deny: 'locked by policy' } : {}
    },
    [{ suffix: 'chip', value: false }, { suffix: 'toasts', value: false }],
    [],
  )
  expect(out).toEqual({ saved: 0, error: 'locked by policy' })
  expect(written).toEqual([['matrixkoder.chip', false]]) // toasts never wrote
})

test('S3.1 globals: visibility edits fold into one hiddenTabs write; unchanged skips it', async () => {
  const rows: ConfigRowLite[] = [{ key: 'matrixkoder.hiddenTabs', value: '', isLocked: false }]
  const written: [string, unknown][] = []
  const set = async (key: string, value: ConfigRowLite['value']): Promise<{ deny?: string }> => {
    written.push([key, value])
    return {}
  }
  const out = await saveGlobals(async () => rows, set, [], [{ tab: 'lanes', on: false }])
  expect(out).toEqual({ saved: 1 })
  expect(written).toEqual([['matrixkoder.hiddenTabs', 'lanes']])
  // turning lanes back on from 'lanes' writes the empty list (the current
  // value comes from the fresh list, not the caller)
  const back = await saveGlobals(
    async () => [{ key: 'matrixkoder.hiddenTabs', value: 'lanes', isLocked: false }],
    set,
    [],
    [{ tab: 'lanes', on: true }],
  )
  expect(back).toEqual({ saved: 1 })
  expect(written[1]).toEqual(['matrixkoder.hiddenTabs', ''])
  // no edits at all: nothing writes
  written.length = 0
  expect(await saveGlobals(async () => rows, set, [], [])).toEqual({ saved: 0 })
  expect(written).toEqual([])
})

test('S3.1 globals: an unknown suffix is an error, nothing writes', async () => {
  const written: [string, unknown][] = []
  const out = await saveGlobals(
    async () => [],
    async (key, value) => {
      written.push([key, value])
      return {}
    },
    [{ suffix: 'nope', value: true }],
    [],
  )
  expect(out).toEqual({ saved: 0, error: 'nope: no such setting' })
  expect(written).toEqual([])
})

test('S3.1 globals: tabsVisibilityToHidden folds edits in bar order, labels count as keys', () => {
  expect(tabsVisibilityToHidden('', [{ tab: 'plan', on: false }, { tab: 'lanes', on: false }])).toBe('lanes,plan')
  expect(tabsVisibilityToHidden('lanes,plan', [{ tab: 'lanes', on: true }])).toBe('plan')
  expect(tabsVisibilityToHidden('', [{ tab: 'bogus', on: false }])).toBe('')
})

// ---------- the live view ----------

test('S3 view: yaml fields group by scope; picks overlay; unknown shapes never break it', () => {
  const view = buildSettingsView({
    fields: [
      ...FIELDS,
      { key: 'picks.opencode', scope: 'models', label: 'x', type: 'list', value: ['a', 'b'], default: [], help: '', editable: true },
      { key: 'mystery.future', scope: 'project', label: 'Future', type: 'bool', value: true, default: false, help: '', editable: true },
      { key: 'weird.scope', scope: 'nope', label: 'x', type: 'bool', value: true, default: false, help: '', editable: true },
      { key: 'weird.type', scope: 'child', label: 'x', type: 'slider', value: 1, default: 1, help: '', editable: true },
    ],
    file: '',
    configRows: CONFIG_ROWS,
    globalDefaults: { mode: 'orchestrator' },
    about: ABOUT,
  })
  expect(view.mock).toBe(false)
  const titles = view.groups.map(g => `${g.scope}/${g.title}`)
  expect(titles).toContain('child/watch') // stall_min + max_wait_min land together
  expect(titles).toContain('project/lanes')
  expect(titles).toContain('project/project') // the unrostered field gets a scope-named group
  expect(view.groups.flatMap(g => g.rows).some(r => r.key === 'weird.scope')).toBe(false)
  expect(view.groups.flatMap(g => g.rows).some(r => r.key === 'weird.type')).toBe(false)
  expect(view.picks.find(p => p.agent === 'opencode')?.models).toEqual(['a', 'b']) // the picks.* overlay
  expect(view.picks.find(p => p.agent === 'claude')?.models).toEqual([]) // no pool: no default offered
  // globals by suffix: mode matched matrixkoder.mode, chip locked read-only
  const mode = view.groups.flatMap(g => g.rows).find(r => r.key === 'mode')
  expect(mode?.value).toBe('orchestrator')
  expect(mode?.global).toBe(true)
  expect(mode?.default).toBe('orchestrator')
  const chip = view.groups.flatMap(g => g.rows).find(r => r.key === 'chip')
  expect(chip?.editable).toBe(false)
})

/** The pool a `picks.<agent>` row carries in the list fixture — the one list the
 *  picker and `mk-config.py set` both read, so the test never restates it. */
function fixturePool(agent: string): string[] {
  const row = SETTINGS_LIST_FIXTURE.find(f => f.key === `picks.${agent}`)
  return Array.isArray(row?.pool) ? (row?.pool as unknown[]).filter((x): x is string => typeof x === 'string') : []
}

test('W3.1 models: every option a picker shows is a model `set picks.<agent>` accepts', () => {
  const view = buildSettingsView({
    fields: [...SETTINGS_LIST_FIXTURE],
    file: '',
    configRows: [],
    globalDefaults: {},
    about: ABOUT,
  })
  // the offer IS the pool, per agent, in the pool's own order
  for (const card of view.agents) {
    expect(card.models.map(m => m.name)).toEqual(fixturePool(card.id))
  }
  expect(view.agents.map(a => a.id)).toEqual(['opencode', 'claude', 'agy', 'codex'])
  // the reported failure (2026-10-11): a model no pool lists is not offered, so `set` cannot
  // refuse it — the hard-coded catalogue offered exactly this one
  expect(fixturePool('opencode')).not.toContain('opencode/step-5-preview-free')
  for (const card of view.agents) {
    expect(card.models.map(m => m.name)).not.toContain('opencode/step-5-preview-free')
  }
})

test('W3.1 models: the defaults shown before a save are filtered to the pool', () => {
  const view = buildSettingsView({
    fields: [...SETTINGS_LIST_FIXTURE],
    file: '',
    configRows: [],
    globalDefaults: {},
    about: ABOUT,
  })
  expect(view.picks.find(p => p.agent === 'opencode')?.models).toEqual(
    ['opencode/space-bunny-free', 'ollama-cloud/minimax-m3'])
  // a row the answer carried no picks for falls back to the defaults, and a default outside
  // the pool is dropped: opencode's defaults are ollama-cloud/minimax-m3,
  // opencode/step-5-preview-free (in no pool) and opencode/space-bunny-free
  const noValue = SETTINGS_LIST_FIXTURE.map(f => (
    f.key === 'picks.opencode' ? { ...f, value: undefined } : f
  ))
  const draft = buildSettingsView({
    fields: noValue,
    file: '',
    configRows: [],
    globalDefaults: {},
    about: ABOUT,
  })
  expect(draft.picks.find(p => p.agent === 'opencode')?.models).toEqual(
    ['ollama-cloud/minimax-m3', 'opencode/space-bunny-free'])
  // every default an agent would show is one its pool lists (nothing can be refused on save)
  for (const p of draft.picks) {
    for (const m of p.models) expect(fixturePool(p.agent)).toContain(m)
  }
  expect(view.picks.find(p => p.agent === 'opencode')?.editable).toBe(true)
})

test('W3.1 models: a picks row with no pool offers nothing rather than a catalogue', () => {
  const view = buildSettingsView({
    fields: [{ key: 'picks.opencode', scope: 'models', label: 'x', type: 'list', value: [], default: [], help: '', editable: true }],
    file: '',
    configRows: [],
    globalDefaults: {},
    about: ABOUT,
  })
  expect(view.agents.find(a => a.id === 'opencode')?.models).toEqual([])
  expect(view.picks.find(p => p.agent === 'opencode')?.models).toEqual([])
  // and an agent the yaml lists no row for at all is read-only, as before
  expect(view.picks.find(p => p.agent === 'claude')?.editable).toBe(false)
})

test('W3.1 models: only the agents the yaml has switched on draw a card', () => {
  // the user report behind this: the Agents scope showed AGY on while its Models card
  // read "agent off in Agents · not routed", and Codex was off but still drew models
  const avail = (name: string, on: boolean): YamlFieldIn => ({
    key: `agents.${name}.available`, scope: 'agents', label: name, type: 'bool',
    value: on, default: false, help: 'detected at bootstrap', editable: false,
  })
  const view = buildSettingsView({
    fields: [...SETTINGS_LIST_FIXTURE, avail('agy', true), avail('codex', false)],
    file: '',
    configRows: [],
    globalDefaults: {},
    about: ABOUT,
  })
  expect(view.agents.map(a => a.id)).toEqual(['opencode', 'claude', 'agy'])
  // an agent that is on is never labelled off beside it
  expect(view.agents.every(a => a.off === false)).toBe(true)
  expect(view.agents.find(a => a.id === 'agy')?.models.map(m => m.name)).toEqual(fixturePool('agy'))
  // no availability row at all keeps the agent on (a yaml written before the row existed)
  const noRows = buildSettingsView({
    fields: [...SETTINGS_LIST_FIXTURE], file: '', configRows: [], globalDefaults: {}, about: ABOUT,
  })
  expect(noRows.agents.map(a => a.id)).toEqual(['opencode', 'claude', 'agy', 'codex'])
})

test('S3.2 about: version, latest tag, what is new, helpers fall back to not probed', () => {
  expect(parsePluginManifest(JSON.stringify({ version: '0.3.0', userConfig: { chip: { default: true }, mode: { default: 'x' } } }))).toEqual({
    version: '0.3.0',
    defaults: { chip: true, mode: 'x' },
  })
  expect(parsePluginManifest('torn').version).toBe('unknown')
  expect(firstTagLine('v0.3.0\nv0.2.0\n')).toBe('0.3.0')
  expect(firstTagLine('\n')).toBe('')
  const entry = topChangelogEntry(['# Changelog', '', '## [0.3.0] - 2026-10-10', '', '### Added', '- live settings', '', '### Fixed', '- a crash', '', '## [0.2.0] - old', '', '- stale line'].join('\n'))
  expect(entry).toEqual([
    { kind: 'feat', line: 'live settings' },
    { kind: 'fix', line: 'a crash' },
  ])
  // the view: absent helpers draw not probed, present ones their probe value
  const view = buildSettingsView({
    fields: [{ key: 'helpers.herdr', scope: 'child', label: 'x', type: 'text', value: 'ok', default: 'ok', help: '', editable: false }],
    file: '',
    configRows: [],
    globalDefaults: {},
    about: ABOUT,
  })
  const helpers = view.groups.find(g => g.title === 'helpers')?.rows ?? []
  expect(helpers.find(h => h.key === 'helpers.herdr')?.value).toBe('ok')
  expect(helpers.find(h => h.key === 'helpers.jev')?.value).toBe('not probed')
  expect(view.about).toMatchObject({ installed: '0.3.0', latest: '0.3.0' })
})

test('S3 keys: savedEditKeys covers fields, picks and hive; settingsInputKeys only real Inputs', () => {
  const view = buildSettingsView({ fields: FIELDS, file: '', configRows: CONFIG_ROWS, globalDefaults: {}, about: ABOUT })
  const keys = savedEditKeys(view)
  expect(keys).toContain('settings:edit:session.stall_min')
  expect(keys).toContain('settings:edit:models.opencode')
  expect(keys).toContain('settings:edit:hive.matrixkoder')
  const inputs = settingsInputKeys(view)
  expect(inputs.has('set-find-input')).toBe(true)
  expect(inputs.has('set-session.stall_min')).toBe(false) // a num row draws no Input
})

test('S3 argv: mkConfigArgv always carries --data with the dashboard data dir', () => {
  expect(mkConfigArgv('/plug', '/data', 'list', '--json')).toEqual(
    ['python3', '/plug/scripts/mk-config.py', '--data', '/data', 'list', '--json'])
})

test('S3 save: the stored view folds the saved values back, so saved rows read clean', () => {
  const view = buildSettingsView({ fields: FIELDS, file: '', configRows: CONFIG_ROWS, globalDefaults: {}, about: ABOUT })
  const kept = applySavedView(view, {
    sets: [{ key: 'session.stall_min', value: 25, list: false }],
    globals: [],
    visibility: [],
  })
  expect(kept.groups.flatMap(g => g.rows).find(r => r.key === 'session.stall_min')?.value).toBe(25)
  expect(kept.groups.flatMap(g => g.rows).find(r => r.key === 'session.max_wait_min')?.value).toBe(120)
})

// ---------- drift tests: schema and plugin.json userConfig ----------
//
// The bundled test runner runs in a sealed JS sandbox (no `process`, `Bun`,
// `require` or `fs`); it cannot run `python3 scripts/mk-config.py list` or
// read `.claude-plugin/plugin.json` from a test body. Instead, the
// committed `hooks/tabs/settings.schema.ts` is the reduced shape generated
// by `scripts/settings-drift.py --write` (a fresh data dir + a minimal
// agents block, the real `.claude-plugin/plugin.json`). Re-run
// `scripts/settings-drift.py --write` after editing either source, and
// `scripts/settings-drift.py --check` (or the script's --self-test) is the
// process-side guard that catches drift between the committed file and the
// live sources.

const SCHEMA = SETTINGS_SCHEMA.schema
const USER_CONFIG = SETTINGS_SCHEMA.userConfig

const YAML_SCOPES = new Set<SettingsField['scope']>(['project', 'routing', 'agents', 'child'])

/** Every yaml row the fixture draws (mutable, value-bearing, no models /
 *  tabs.* / about helpers — those scopes carry their own checks). */
function fixtureYamlRows(): SettingsField[] {
  return SETTINGS_FIXTURE.groups
    .filter(g => YAML_SCOPES.has(g.scope))
    .flatMap(g => g.rows)
    .filter(r => r.editable)
}

type SchemaRow = (typeof SETTINGS_SCHEMA)['schema'][number]

test('drift: editable YAML rows in SETTINGS_FIXTURE match the generated schema (both directions)', () => {
  const schemaMap = new Map<string, SchemaRow>(SCHEMA.map(f => [f.key as string, f]))
  const fixtureRows = fixtureYamlRows()
  expect(fixtureRows.length).toBeGreaterThan(0)

  // 1. fixture -> schema: every editable fixture row has a schema row,
  // and an editable schema row's default matches the fixture's
  for (const row of fixtureRows) {
    const schemaRow = schemaMap.get(row.key)
    expect(schemaRow, `key ${row.key} must exist in the generated schema`).toBeDefined()
    if (schemaRow && schemaRow.editable) {
      expect(row.default, `default for ${row.key} must match schema default`).toEqual(schemaRow.default)
    }
  }

  // 2. schema -> fixture: every editable schema row in a yaml scope has
  // a fixture row (a new editable schema row with no fixture row escapes
  // the tab and stays invisible — catch it here)
  const fixtureKeys = new Set(fixtureRows.map(r => r.key))
  for (const f of SCHEMA) {
    if (!f.editable) continue
    if (!YAML_SCOPES.has(f.scope as SettingsField['scope'])) continue
    expect(fixtureKeys.has(f.key as string), `schema key ${f.key} has no editable fixture row`).toBe(true)
  }
})

test('drift: global rows in SETTINGS_FIXTURE exactly match plugin.json userConfig keys', () => {
  // A userConfig key draws one fixture row in scope 'mode' / 'tabs' / 'about';
  // a fixture row in those scopes whose key is also a userConfig key draws
  // the userConfig default. The set is exact — a renamed key or a missing
  // row trips this test (the old `r.key in userConfig` filter skipped both).
  const globalScopes = new Set<SettingsField['scope']>(['mode', 'tabs', 'about'])
  const fixtureGlobalRows = SETTINGS_FIXTURE.groups
    .filter(g => globalScopes.has(g.scope))
    .flatMap(g => g.rows)
    .filter(r => r.key in USER_CONFIG)
  const fixtureGlobalKeys = new Set(fixtureGlobalRows.map(r => r.key))
  const userConfigKeys = new Set(Object.keys(USER_CONFIG))

  expect(fixtureGlobalRows.length).toBeGreaterThan(0)
  // exact set match: every userConfig key is drawn, no extra userConfig
  // keys are drawn, no fixture row is silently skipped
  expect(fixtureGlobalKeys).toEqual(userConfigKeys)
  for (const row of fixtureGlobalRows) {
    const expected = USER_CONFIG[row.key as keyof typeof USER_CONFIG]
    expect(row.default, `default for ${row.key} must match plugin.json userConfig default`).toEqual(expected)
  }
})
