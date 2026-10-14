// Real-code tests for the Settings tab (plan settings-tab S2.2): the real
// SettingsTab driven through a stub element table and a fake `ui` (a Map —
// structurally the TabUi the tab declares), the way plan.test.ts drives the
// fold. No $ and no engine mount: the tab is not wired into the dashboard
// yet (that is S2.3), so these tests press the closures the Buttons carry —
// C10: no press ever leaves the test. One test per scope (its signature
// rows, and the 36-col fit), plus the find filter, the dirty footer's
// save/discard, the Models 5th-pick refusal and name sort, the danger row's
// presence/absence, and the row-width + hotkey audits across every scope.
import { test, expect } from 'claude-code/testing'
import type { BoxProps, ElementChildren, Elements, RenderChildren, RenderElement, RenderNode, TextProps } from 'claude-code'
import { SettingsTab, settingsView } from './settings'

/** every node of `type` under `n`, in document order (plan.test.ts's walk). */
function nodesOf(n: unknown, type: string): { type: string; props: Record<string, unknown>; children: unknown[] }[] {
  const out: { type: string; props: Record<string, unknown>; children: unknown[] }[] = []
  const walk = (node: unknown): void => {
    if (typeof node !== 'object' || node === null) return
    const cur = node as { type?: string; props?: Record<string, unknown>; children?: unknown[] }
    if (cur.type === type) out.push({ type: cur.type, props: cur.props ?? {}, children: cur.children ?? [] })
    for (const c of cur.children ?? []) walk(c)
  }
  walk(n)
  return out
}

/** every drawn Text's own text under `n`, joined with newlines. */
const allText = (n: unknown): string =>
  nodesOf(n, 'Text').map(t => (t.children ?? []).map(c => (typeof c === 'string' ? c : '')).join('')).join('\n')

/** everything the tab drew, as one string: the Text runs plus the Button
 *  labels and the Input labels/values — the kit's rows put their labels and
 *  values inside those, not in Text runs. */
const drawnText = (n: unknown): string => [
  allText(n),
  ...buttonsOf(n).map(b => String(b.props.label ?? '')),
  ...nodesOf(n, 'Input').map(i => `${String(i.props.label ?? '')} ${String(i.props.value ?? '')}`),
].join('\n')

/** every Button in the drawn tree. */
const buttonsOf = (drawn: unknown): { props: Record<string, unknown>; children: unknown[] }[] => nodesOf(drawn, 'Button')

/** the Button under `drawn` carrying `key`, pressed (its onPress run). */
function press(drawn: unknown, key: string): void {
  const b = buttonsOf(drawn).find(x => x.props.key === key)
  expect(b, `the ${key} button`).toBeDefined()
  ;(b?.props.onPress as () => void)()
}

// ---------- the stub element table (plan.test.ts's, with the field elements
// keeping their props so a press/typing reads the closures off them)

const scalarProps = (props: object): Record<string, string | number | boolean> => {
  const out: Record<string, string | number | boolean> = {}
  for (const [k, v] of Object.entries(props)) {
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') out[k] = v
  }
  return out
}

const isNode = (k: RenderChildren): k is RenderNode =>
  typeof k === 'string' || (typeof k === 'object' && k !== null && !Array.isArray(k))

const kids = (c: RenderChildren | undefined): RenderNode[] | undefined => {
  if (c == null) return undefined
  const arr: readonly RenderChildren[] = Array.isArray(c) ? c : [c]
  return arr.filter(isNode)
}

const boxTag = (props: BoxProps & ElementChildren): RenderElement =>
  ({ type: 'Box', props: scalarProps(props), children: kids(props.children) })
const textTag = (props: TextProps & ElementChildren): RenderElement =>
  ({ type: 'Text', props: scalarProps(props), children: kids(props.children) })
const fieldTag = (type: string) => (props: Record<string, unknown>): RenderElement =>
  ({ type, props: { ...props }, children: [] }) as unknown as RenderElement

const stubEls: Elements[keyof Elements] = {
  Box: boxTag,
  Text: textTag,
  Button: (props: Record<string, unknown>) => ({ type: 'Button', props, children: [] }),
  Input: fieldTag('Input'),
  Select: fieldTag('Select'),
  Link: () => ({ type: 'Text', props: {}, children: [] }),
  Code: () => ({ type: 'Text', props: {}, children: [] }),
  Markdown: () => ({ type: 'Text', props: {}, children: [] }),
  Client: () => ({ type: 'Text', props: {}, children: [] }),
  Raster: (props: Record<string, unknown>) => ({ type: 'Raster', props: scalarProps(props), children: [] }),
  Image: () => ({ type: 'Text', props: {}, children: [] }),
} as unknown as Elements[keyof Elements]

/** the fake view state (a Map is structurally the tab's TabUi). */
type FakeUi = Map<string, string>

/** the tab drawn at `cols` with `ui`; `scope` pre-selects a scope the way the
 *  scope bar's press does (settings:scope). */
const draw = (ui: FakeUi, cols = 46, scope?: string): unknown => {
  if (scope != null) ui.set('settings:scope', scope)
  return SettingsTab(stubEls, settingsView(), cols, ui)
}

// ---------- row widths (plan.test.ts's discipline, with a Button counted as
// its drawn `[ label ]` / plain chrome and a field element as 0 — the engine
// lays those out): a row is a horizontal Box (or a Text); its width is the
// text of every Text descendant plus the gap columns between its children.

type DrawnNode = { type?: string; props?: { key?: string; flexDirection?: string; gap?: number; borderStyle?: string; label?: string; plain?: boolean }; children?: unknown[] }

function textWidth(node: unknown): number {
  if (typeof node === 'string') return node.length
  if (typeof node !== 'object' || node === null) return 0
  const n = node as DrawnNode
  if (n.type !== 'Text') return 0
  return (n.children ?? []).reduce((sum: number, c) => sum + textWidth(c), 0)
}

function widthOf(node: unknown): number {
  if (typeof node === 'object' && node !== null) {
    const n = node as DrawnNode
    if (n.type === 'Text') return textWidth(n)
    if (n.type === 'Button') return String(n.props?.label ?? '').length + (n.props?.plain ? 2 : 4)
    if (n.type === 'Input' || n.type === 'Select') return 0
    const children = n.children ?? []
    if (n.type === 'Box' && n.props?.flexDirection === 'row') {
      const gap = typeof n.props.gap === 'number' ? n.props.gap : 0
      return children.reduce((sum: number, c) => sum + widthOf(c), 0) + gap * Math.max(0, children.length - 1)
    }
    return children.reduce((max: number, c) => Math.max(max, widthOf(c)), 0)
  }
  return 0
}

/** every row's width in the drawn tree: each Text and each horizontal Box,
 *  with whether it sits inside a kit Card (a round-bordered Box) and the
 *  row's text (Buttons count their label) so a failure names the row. */
function rowWidths(root: unknown): { width: number; inCard: boolean; text: string }[] {
  const out: { width: number; inCard: boolean; text: string }[] = []
  const preview = (node: unknown): string => {
    if (typeof node === 'string') return node
    if (typeof node !== 'object' || node === null) return ''
    const n = node as DrawnNode
    if (n.type === 'Button') return String(n.props?.label ?? '')
    return (n.children ?? []).map(preview).join('')
  }
  const walk = (node: unknown, inCard: boolean): void => {
    if (typeof node !== 'object' || node === null) return
    const n = node as DrawnNode
    const card = inCard || (n.type === 'Box' && n.props?.borderStyle === 'round' && !/^(dash-tab|set-scope)-/.test(String(n.props?.key ?? '')))
    if (n.type === 'Text' || (n.type === 'Box' && n.props?.flexDirection === 'row')) {
      out.push({ width: widthOf(node), inCard: card, text: preview(node) })
    }
    for (const c of n.children ?? []) walk(c, card)
  }
  walk(root, false)
  return out
}

/** a row inside a kit Card is budgeted for the Card's content (cols - 4); a
 *  row outside every Card owes only the pane's width (plan.test.ts's rule). */
const fitCheck = (drawn: unknown, cols: number): void => {
  const widths = rowWidths(drawn)
  expect(widths.length, `the tab drew rows at ${cols}`).toBeGreaterThan(0)
  const wide = widths.filter(w => (w.inCard ? w.width > cols - 4 : w.width > cols))
  expect(wide, `over-wide rows at ${cols} cols: ${JSON.stringify(wide)}`).toEqual([])
}

// ---------- Mode scope (the default)

test('Settings · Mode: scope bar 1-8, MOCK row, Solo/Orchestrator/Hive (no Hybrid), no danger row', () => {
  const ui: FakeUi = new Map()
  const drawn = draw(ui)
  const flat = drawnText(drawn)
  expect(flat.includes('MOCK · fixture data · not this run'), 'the MOCK row').toBe(true)
  expect(flat.includes('plugin.json · all projects'), 'the mode scope-note').toBe(true)
  // the scope bar: one pressable per scope, the digit its hotkey
  const scopeLabels = buttonsOf(drawn).map(b => String(b.props.label))
  for (const label of ['Project', 'Routing', 'Agents', 'Models', 'Child', 'Tabs', 'About']) { // the active Mode is tint text
    expect(scopeLabels, `the ${label} scope button`).toContain(label)
  }
  expect(buttonsOf(drawn).find(b => b.props.label === 'Project')?.props.hotkey).toBe('2')
  // the three bound modes; Hybrid is gone (the plan's departure)
  expect(flat.includes('OPERATING MODE'), 'the mode card header').toBe(true)
  for (const name of ['Solo', 'Orchestrator', 'Hive']) expect(flat.includes(name), `the ${name} mode card`).toBe(true)
  expect(flat.includes('Hybrid'), 'no Hybrid mode').toBe(false)
  expect(flat.includes('reset Mode to defaults'), 'no danger row on Mode').toBe(false)
  // pressing a mode card writes the edit; Hive then shows the checklist card.
  // L7.5: the hive rows came from the retired projects.json, so the fixture (and
  // the live view) draws none — the card renders, empty, with no stale entries.
  press(drawn, 'set-mode-solo')
  expect(ui.get('settings:edit:mode')).toBe('"solo"')
  press(drawn, 'set-mode-hive')
  expect(ui.get('settings:edit:mode')).toBe('"hive"')
  const hive = draw(ui)
  const hiveFlat = drawnText(hive)
  expect(hiveFlat.includes('HIVE · PROJECTS'), 'the hive checklist card').toBe(true)
  for (const p of ['matrixkoder', 'hermes-switchui', 'hermes-desktop']) {
    expect(hiveFlat.includes(p), `no stale ${p} entry (projects.json retired)`).toBe(false)
  }
  expect(hiveFlat.includes('mode = hive'), 'the card names its live source').toBe(true)

  fitCheck(drawn, 46)
  fitCheck(draw(ui, 36), 36)
})

// ---------- one test per row scope: signature rows + the 36-col fit

test('Settings · Project: lanes + QA rows, list row as comma-joined text, danger row', () => {
  const ui: FakeUi = new Map()
  const drawn = draw(ui, 46, 'project')
  expect(ui.get('settings:scope'), 'the scope press landed').toBe('project')
  const flat = drawnText(drawn)
  expect(flat.includes('project.yaml · this repo'), 'the project scope-note').toBe(true)
  for (const sig of ['Worktree slots', 'Reuse slots', 'Copy into slot', '.claude-plugin/types', 'Integration branch', 'Merge target', 'Builders skip perms']) {
    expect(flat.includes(sig), `the ${sig} row`).toBe(true)
  }
  expect(flat.includes('reset Project to defaults'), 'the danger row').toBe(true)
  fitCheck(drawn, 46)
  fitCheck(draw(ui, 36, 'project'), 36)
})

test('Settings · Routing: policy, the four reserve-floor % rows with bars, reviewer selects, danger row', () => {
  const ui: FakeUi = new Map()
  const drawn = draw(ui, 46, 'routing')
  const flat = drawnText(drawn)
  expect(flat.includes('project.yaml · routing_preferences'), 'the routing scope-note').toBe(true)
  for (const sig of ['Policy', 'Builder order', 'opencode, claude', 'Max build lanes', 'Claude 5h', 'Claude week', 'Others 5h', 'Others week', 'Code reviewer', 'claude sonnet medium ▾', 'Visual reviewer']) {
    expect(flat.includes(sig), `the ${sig} row`).toBe(true)
  }
  // the four % rows draw the kit's SmoothBar (a track-background Text each)
  const bars = nodesOf(drawn, 'Text').filter(t => t.props.backgroundColor != null)
  expect(bars.length, 'four % bars at 46').toBeGreaterThanOrEqual(4)
  expect(flat.includes('reset Routing to defaults'), 'the danger row').toBe(true)
  fitCheck(drawn, 46)
  fitCheck(draw(ui, 36, 'routing'), 36)
})

test('Settings · seg words stay short when the live config sends long notes (S2b)', () => {
  const v = settingsView()
  const long = [['claude-sparing', 'Claude Code orchestrates; builders below'], ['balanced', 'Claude Code and builders share work'], ['quality', 'the strongest route wins, quota second']] as const
  const live = { ...v, groups: v.groups.map(g => ({ ...g, rows: g.rows.map(r => (r.key === 'routing_preferences.policy' ? { ...r, opts: long } : r)) })) }
  const ui: FakeUi = new Map()
  ui.set('settings:scope', 'routing')
  const flat = drawnText(SettingsTab(stubEls, live, 46, ui))
  for (const w of ['spare', 'bal', 'qual']) expect(flat.includes(w), `the ${w} button`).toBe(true)
  expect(flat.includes('orchestrates'), 'no note sentence in a button').toBe(false)
})

test('Settings · L7.1: a live view draws the REAL yaml path on every yaml scope note, never a hard-coded one', () => {
  // a view as buildSettingsView returns it once `list --json` answered with the
  // migrated C2 path: every yaml-backed scope-note carries that path (L7.1)
  const livePath = '/srv/mkhome/projects/matrixkoder/project.yaml'
  const v = settingsView()
  const live = { ...v, scopes: v.scopes.map(s => (['project', 'routing', 'agents', 'models', 'child'].includes(s.id) ? { ...s, file: livePath } : s)) }
  for (const id of ['project', 'routing', 'agents', 'models', 'child'] as const) {
    const ui: FakeUi = new Map([['settings:scope', id]])
    const flat = drawnText(SettingsTab(stubEls, live, 80, ui))
    expect(flat.includes(livePath), `the ${id} note names the real path`).toBe(true)
    expect(flat.includes('.matrixkoder/matrixkoder.yaml'), `no hard-coded path on ${id}`).toBe(false)
  }
  // and the fixture's own note is the shape label, not a repo-specific spelling
  const fixture = drawnText(SettingsTab(stubEls, settingsView(), 46, new Map([['settings:scope', 'project']])))
  expect(fixture.includes('.matrixkoder/matrixkoder.yaml'), 'no hard-coded path in the fixture').toBe(false)
  expect(fixture.includes('project.yaml · this repo'), 'the fixture shape label').toBe(true)
})

test('Settings · Models: an agent with no picks row is read-only and never edits (S2b)', () => {
  const v = settingsView()
  const ro = { ...v, picks: v.picks.map(p => (p.agent === 'claude' ? { ...p, editable: false } : p)) }
  const ui: FakeUi = new Map()
  ui.set('settings:scope', 'models')
  const drawn = SettingsTab(stubEls, ro, 46, ui)
  expect(drawnText(drawn).includes('read-only · no model list'), 'the read-only note').toBe(true)
  const btn = buttonsOf(drawn).find(b => String(b.props.key ?? '').startsWith('set-model-claude-'))
  ;(btn?.props.onPress as (() => void) | undefined)?.()
  expect([...ui.keys()].some(k => k === 'settings:edit:models.claude'), 'no edit written').toBe(false)
  // a stale edit from before the fix is neither counted nor saved
  ui.set('settings:edit:models.claude', JSON.stringify(['x']))
  const saved: unknown[] = []
  const ui2 = { get: (k: string) => ui.get(k), set: (k: string, v: string) => { ui.set(k, v) }, save: (pl: unknown) => { saved.push(pl) } }
  ui.set('settings:edit:mode', '')
  const again = SettingsTab(stubEls, ro, 46, ui2 as never)
  expect(drawnText(again).includes('unsaved'), 'a stale read-only edit is not dirty').toBe(false)
})

test('Settings · Agents: builder availability bools and the usage group', () => {
  const ui: FakeUi = new Map()
  const drawn = draw(ui, 46, 'agents')
  const flat = drawnText(drawn)
  expect(flat.includes('project.yaml · agents · usage'), 'the agents scope-note').toBe(true)
  for (const sig of ['OpenCode', 'Claude Code', 'herdr', 'Codex', 'AGY', 'codexbar', 'claude, opencodego, ollama']) {
    expect(flat.includes(sig), `the ${sig} row`).toBe(true)
  }
  expect(flat.includes('reset Agents to defaults'), 'the danger row').toBe(true)
  fitCheck(drawn, 46)
  fitCheck(draw(ui, 36, 'agents'), 36)
})

test('Settings · Child: builder sessions, watch and reviewer panes', () => {
  const ui: FakeUi = new Map()
  const drawn = draw(ui, 46, 'child')
  const flat = drawnText(drawn)
  expect(flat.includes('project.yaml · lanes.child'), 'the child scope-note').toBe(true)
  for (const sig of ['New round', 'new', 'Subagents', 'Subagent model', 'Stall after', 'Give up after', 'Split', 'Reuse reviewer pane']) {
    expect(flat.includes(sig), `the ${sig} row`).toBe(true)
  }
  expect(flat.includes('reset Child to defaults'), 'the danger row').toBe(true)
  fitCheck(drawn, 46)
  fitCheck(draw(ui, 36, 'child'), 36)
})

test('Settings · Tabs: visibility bools, the C2 rows (defaultTab, hiddenTabs, raster), pane rows', () => {
  const ui: FakeUi = new Map()
  const drawn = draw(ui, 46, 'tabs')
  const flat = drawnText(drawn)
  expect(flat.includes('userConfig · dashboard'), 'the tabs scope-note').toBe(true)
  for (const sig of ['o Overview', '0 Old', 'Open on', 'old ▾', 'Hidden tabs', 'Raster bars']) {
    expect(flat.includes(sig), `the ${sig} row`).toBe(true)
  }
  expect(flat.includes('reset Tabs to defaults'), 'the danger row').toBe(true)
  fitCheck(drawn, 46)
  fitCheck(draw(ui, 36, 'tabs'), 36)
})

// ---------- Models scope: name sort, pick-order numbers, the 5th-pick refusal

test('Settings · Models: name-sorted rows, per-agent n/4 chips, pick-order numbers, 5th pick refused', () => {
  const ui: FakeUi = new Map()
  const drawn = draw(ui, 46, 'models')
  const flat = drawnText(drawn)
  expect(flat.includes('project.yaml · agents.*.models'), 'the models scope-note').toBe(true)
  // count chips: 3/4, 4/4, 0/4, 0/4
  for (const chip of [' 3/4 ', ' 4/4 ', ' 0/4 ']) expect(flat.includes(chip), `the ${chip.trim()} count chip`).toBe(true)
  expect(nodesOf(drawn, 'Text').filter(t => (t.children ?? []).join('') === ' 0/4 '), 'two empty agents').toHaveLength(2)
  // picked models show pick-order numbers: opencode 1-3, claude restarts at 1-4
  // (rows are name-sorted, so the numbers interleave; the multiset is what holds)
  const marks = nodesOf(drawn, 'Text')
    .map(t => (t.children ?? []).map(c => (typeof c === 'string' ? c : '')).join(''))
    .filter(t => /^ [1-4] $/.test(t))
  expect([...marks].sort(), `the pick-order numbers: ${marks.join('|')}`).toEqual([' 1 ', ' 1 ', ' 1 ', ' 2 ', ' 2 ', ' 3 ', ' 3 ', ' 4 '])
  // rows sorted by name (the departure: no ranks, no scores); long names are
  // clipped in the row, so each is sought by its shortest unique prefix
  const opencode = settingsView().agents.find(a => a.id === 'opencode')
  const names = (opencode?.models ?? []).map(m => m.name)
  const uniquePrefix = (name: string): string => {
    for (let n = 1; n <= name.length; n++) {
      const p = name.slice(0, n)
      if (names.every(o => o === name || !o.startsWith(p))) return p
    }
    return name
  }
  const expected = [...names].sort((a, b) => a.localeCompare(b))
  let at = -1
  for (const name of expected) {
    const i = flat.indexOf(uniquePrefix(name))
    expect(i, `the ${name} row drawn`).toBeGreaterThanOrEqual(0)
    expect(i, `${name} after the previous sorted name`).toBeGreaterThan(at)
    at = i
  }
  expect(flat.includes('#'), 'no rank numbers').toBe(false)
  expect(flat.includes('4 of 4 picked · untick one to swap'), 'the full card note at 46').toBe(true)
  // off agents (Codex, AGY) are dimmed out with the artboard's note
  expect(nodesOf(drawn, 'Text').filter(t => (t.children ?? []).join('') === 'agent off in Agents · not routed')).toHaveLength(2)
  // omp's pool is the allow-list: one model, one pick
  expect(flat.includes('manifest/auto'), 'the omp model').toBe(true)
  expect(flat.includes(' 1/4 '), 'the omp count chip').toBe(true)

  // the 5th pick: opencode has 3 — a 4th lands, a 5th is refused
  press(drawn, 'set-model-opencode-opencode-go/claude-haiku-5-5')
  expect(ui.get('settings:edit:models.opencode'), 'the 4th pick lands').toBe(JSON.stringify([
    'ollama-cloud/minimax-m3', 'opencode/step-5-preview-free', 'opencode/space-bunny-free', 'opencode-go/claude-haiku-5-5',
  ]))
  const full = draw(ui, 46, 'models')
  press(full, 'set-model-opencode-opencode/longcat-2.5-preview-free')
  expect(ui.get('settings:edit:models.opencode'), 'the 5th pick refused, edits unchanged').toBe(JSON.stringify([
    'ollama-cloud/minimax-m3', 'opencode/step-5-preview-free', 'opencode/space-bunny-free', 'opencode-go/claude-haiku-5-5',
  ]))
  // unticking a picked model renumbers (claude drops to 3)
  press(full, 'set-model-claude-claude-sonnet-5-5')
  expect(ui.get('settings:edit:models.claude'), 'an untick removes the pick').toBe(JSON.stringify([
    'claude-haiku-5-5', 'claude-opus-5-5', 'claude-fable-5-1',
  ]))
  expect(drawnText(full).includes('reset Models to defaults'), 'the danger row').toBe(true)
  fitCheck(drawn, 46)
  fitCheck(draw(new Map([['settings:scope', 'models']]), 36), 36)
})

// ---------- About scope: read-only rows, version card, c check, what's new


test('Settings · Models: an omp pick writes one picks.omp edit', () => {
  const ui: FakeUi = new Map()
  const drawn = draw(ui, 46, 'models')
  press(drawn, 'set-model-omp-manifest/auto')
  expect(ui.get('settings:edit:models.omp'), 'untick writes one edit').toBe(JSON.stringify([]))
})


test('Settings · About: read-only plugin options + helper probes, version card, c check, what\'s new, no danger row', () => {
  const ui: FakeUi = new Map()
  const drawn = draw(ui, 46, 'about')
  const flat = drawnText(drawn)
  expect(flat.includes('plugin.json · read-only'), 'the about scope-note').toBe(true)
  for (const sig of ['Wake on events', 'Guards', 'Usage poll', 'herdr', 'Jev', 'context-mode', 'codebase-memory', 'canary']) {
    expect(flat.includes(sig), `the ${sig} row`).toBe(true)
  }
  // read-only rows carry no control buttons (nothing to press on them)
  for (const key of ['set-wake-tog', 'set-guards-tog', 'set-helpers.herdr-seg-ok']) {
    expect(buttonsOf(drawn).find(b => b.props.key === key), `no ${key} control`).toBeUndefined()
  }
  // the version card + what's new
  expect(flat.includes('VERSION'), 'the version card').toBe(true)
  expect(flat.includes('installed') && flat.includes('v0.2.0'), 'the installed version').toBe(true)
  expect(flat.includes('latest') && flat.includes('v0.3.0'), 'the latest version').toBe(true)
  expect(flat.includes('checked 10-09'), 'the stale check note').toBe(true)
  expect(flat.includes('WHAT\'S NEW · V0.3.0'), 'the what\'s-new card').toBe(true)
  expect(flat.includes('dashboard visual kit, bordered tabs'), 'a what\'s-new line').toBe(true)
  expect(flat.includes('reset About to defaults'), 'no danger row on About').toBe(false)
  // `c check` is view-state only: the note flips, nothing else moves
  press(drawn, 'set-about-check')
  expect(ui.get('settings:about:checked')).toBe('1')
  expect(drawnText(draw(ui, 46, 'about')).includes('checked now'), 'the fresh check note').toBe(true)
  fitCheck(drawn, 46)
  fitCheck(draw(ui, 36, 'about'), 36)
})

// ---------- the find filter

test('Settings · find: `f` focuses the field, typing filters rows by label+key with an N-matches note', () => {
  const ui: FakeUi = new Map()
  const drawn = draw(ui, 46, 'routing')
  const find = buttonsOf(drawn).find(b => b.props.key === 'set-find')
  expect(find?.props.hotkey).toBe('f')
  ;(find?.props.onPress as () => void)()
  expect(ui.get('settings:findfocus')).toBe('1')
  const again = draw(ui, 46, 'routing') // re-render: the closure reads fresh state
  press(again, 'set-find')
  expect(ui.get('settings:findfocus')).toBe('') // the press toggles

  const input = nodesOf(drawn, 'Input').find(i => i.props.key === 'set-find-input')
  expect(input, 'the find Input').toBeDefined()
  ;(input?.props.onInput as (v: string) => void)('claude')
  expect(ui.get('settings:find')).toBe('claude')
  const filtered = draw(ui, 46, 'routing')
  const flat = drawnText(filtered)
  expect(flat.includes('2 matches'), 'the match note').toBe(true)
  expect(flat.includes('Claude 5h') && flat.includes('Claude week'), 'the two matching rows').toBe(true)
  expect(flat.includes('Max build lanes'), 'a non-matching row hidden').toBe(false)
  expect(flat.includes('Code reviewer'), 'a key-only match on opts is not a row match').toBe(false)

  ;(input?.props.onInput as (v: string) => void)('')
  const cleared = draw(ui, 46, 'routing')
  expect(drawnText(cleared).includes('matches'), 'no note without a query').toBe(false)
  expect(drawnText(cleared).includes('Max build lanes'), 'every row back').toBe(true)
})

// ---------- the dirty footer: edits, save, discard

test('Settings · dirty footer: edits mark rows and count, s save / x discard clear them', () => {
  const ui: FakeUi = new Map()
  const drawn = draw(ui, 46, 'routing')
  expect(drawnText(drawn).includes('unsaved'), 'no footer clean').toBe(false)
  // a num step: the reserve floor's max is 90 > 60, so the step is 5
  press(drawn, 'set-routing_preferences.reserve_floor.claude.five_hour_left_pct-inc')
  expect(ui.get('settings:edit:routing_preferences.reserve_floor.claude.five_hour_left_pct')).toBe('45')
  let dirty = draw(ui, 46, 'routing')
  let flat = drawnText(dirty)
  expect(flat.includes('● 1 unsaved'), 'the count').toBe(true)
  expect(flat.includes('save'), 'the save action').toBe(true)
  // a second edit doubles the count (the seg press carries the display word
  // `qual`; the edit stores the real value)
  press(dirty, 'set-routing_preferences.policy-seg-qual')
  expect(ui.get('settings:edit:routing_preferences.policy')).toBe('"quality"')
  dirty = draw(ui, 46, 'routing')
  flat = drawnText(dirty)
  expect(flat.includes('● 2 unsaved'), 'two edits count').toBe(true)
  fitCheck(draw(ui, 36, 'routing'), 36) // the footer fits the compact pane too

  press(dirty, 'set-save')
  expect(ui.get('settings:edit:routing_preferences.reserve_floor.claude.five_hour_left_pct')).toBe('')
  expect(ui.get('settings:edit:routing_preferences.policy')).toBe('')
  expect(drawnText(draw(ui, 46, 'routing')).includes('unsaved'), 'save cleared the edits').toBe(false)

  press(draw(ui, 46, 'routing'), 'set-routing_preferences.reserve_floor.claude.weekly_left_pct-inc')
  expect(drawnText(draw(ui, 46, 'routing')).includes('● 1 unsaved'), 'dirty again').toBe(true)
  press(draw(ui, 46, 'routing'), 'set-discard')
  expect(drawnText(draw(ui, 46, 'routing')).includes('unsaved'), 'discard cleared the edits').toBe(false)
})

// ---------- row interactions through the kit: select, reset, toggle, choose, edit

test('Settings · rows: selection draws help + default + r reset; reset returns to default; controls write edits', () => {
  const ui: FakeUi = new Map()
  // the selected row's second line: help, `default X`, the r reset button
  ui.set('settings:scope', 'project')
  ui.set('settings:sel', 'repo.lanes.slots')
  const sel = draw(ui)
  const selFlat = drawnText(sel)
  expect(selFlat.includes('Reusable work'), 'the help line (it yields to the tail)').toBe(true)
  expect(selFlat.includes('default 3'), 'the default line').toBe(true)
  expect(buttonsOf(sel).find(b => b.props.label === 'r reset')?.props.hotkey).toBe('r')
  // a reset after a step returns the row to its default (no longer dirty)
  press(sel, 'set-repo.lanes.slots-inc')
  expect(drawnText(draw(ui)).includes('● 1 unsaved'), 'the step dirtied the row').toBe(true)
  press(draw(ui), 'set-repo.lanes.slots-reset')
  expect(ui.get('settings:edit:repo.lanes.slots')).toBe('') // reset to a default that equals the loaded value stores no edit (R3 LOW 4)
  expect(drawnText(draw(ui)).includes('unsaved'), 'reset to default is clean').toBe(false)

  // bool toggle, num steps, seg and select choices, text edit — every control
  // writes its JSON edit and closes an open list
  const drawn = draw(ui, 46, 'project')
  press(drawn, 'set-repo.lanes.reuse-tog')
  expect(ui.get('settings:edit:repo.lanes.reuse')).toBe('false')
  const branch = nodesOf(drawn, 'Input').find(i => i.props.value === 'main') // the Integration branch row (the label rides its press now)
  ;(branch?.props.onInput as (v: string) => void)('dev')
  expect(ui.get('settings:edit:repo.integration_branch')).toBe('"dev"')

  const routing = draw(ui, 46, 'routing')
  press(routing, 'set-routing_preferences.reviewer-sel')
  expect(ui.get('settings:open')).toBe('routing_preferences.reviewer')
  const open = draw(ui, 46, 'routing')
  expect(drawnText(open).includes('data-loss'), 'the option note in the open list').toBe(true)
  press(open, 'set-routing_preferences.reviewer-pick-claude opus high')
  expect(ui.get('settings:edit:routing_preferences.reviewer')).toBe('"claude opus high"')
  expect(ui.get('settings:open')).toBe('') // choosing closes the list
})

// ---------- the danger row: presence, absence, and what a press clears

test('Settings · danger: every scope except Mode and About; the press clears that scope\'s edits only', () => {
  const ui: FakeUi = new Map()
  const want: Record<string, string> = {
    project: 'reset Project to defaults',
    routing: 'reset Routing to defaults',
    agents: 'reset Agents to defaults',
    models: 'reset Models to defaults',
    child: 'reset Child to defaults',
    tabs: 'reset Tabs to defaults',
  }
  for (const id of ['mode', 'project', 'routing', 'agents', 'models', 'child', 'tabs', 'about']) {
    const flat = drawnText(draw(ui, 46, id))
    if (id in want) expect(flat.includes(want[id] ?? ''), `the danger row on ${id}`).toBe(true)
    else expect(flat.includes('reset'), `no danger row on ${id}`).toBe(false)
  }
  // a press clears its own scope's edits and leaves another scope's alone.
  // L7.5: agents.*.available is read-only now, so the Agents-scope edit uses the
  // usage.providers list row instead.
  const drawn = draw(ui, 46, 'routing')
  press(drawn, 'set-routing_preferences.reserve_floor.others.weekly_left_pct-inc')
  const agents = draw(ui, 46, 'agents')
  const providers = nodesOf(agents, 'Input').find(i => String(i.props.key) === 'set-usage.providers')
  expect(providers, 'the usage.providers Input').toBeDefined()
  ;(providers?.props.onInput as (v: string) => void)('claude')
  expect(ui.get('settings:edit:usage.providers')).toBe('"claude"')
  press(draw(ui, 46, 'routing'), 'set-danger')
  expect(ui.get('settings:edit:routing_preferences.reserve_floor.others.weekly_left_pct')).toBe('')
  expect(ui.get('settings:edit:usage.providers')).toBe('"claude"') // untouched, another scope
  // on Models the danger row also clears pick edits
  const models = draw(ui, 46, 'models')
  press(models, 'set-model-claude-claude-haiku-5-5')
  expect(ui.get('settings:edit:models.claude')).toBe(JSON.stringify(['claude-sonnet-5-5', 'claude-opus-5-5', 'claude-fable-5-1']))
  press(draw(ui, 46, 'models'), 'set-danger')
  expect(ui.get('settings:edit:models.claude')).toBe('')
})

// ---------- the audits: widths everywhere and the reserved hotkeys only

test('Settings · every scope fits 36 and 46 cols; hotkeys are only 1-8 and s/x/r/f/c — never /', () => {
  const allowed = new Set(['1', '2', '3', '4', '5', '6', '7', '8', 's', 'x', 'r', 'f', 'c'])
  for (const id of ['mode', 'project', 'routing', 'agents', 'models', 'child', 'tabs', 'about']) {
    for (const cols of [36, 46] as const) {
      const drawn = draw(new Map([['settings:scope', id]]), cols)
      fitCheck(drawn, cols)
      for (const b of buttonsOf(drawn)) {
        const hotkey = b.props.hotkey
        if (hotkey != null) expect(allowed.has(String(hotkey)), `the ${String(b.props.key)} hotkey ${String(hotkey)} on ${id} at ${cols}`).toBe(true)
        expect(String(hotkey), `no / hotkey on ${id}`).not.toBe('/')
      }
    }
  }
  // the busiest state also fits: a selection (on a short-default row — a long
  // select default would overflow the kit's own help-line budget), an open
  // select, edits, a filter
  const busy = new Map([
    ['settings:scope', 'routing'],
    ['settings:sel', 'routing_preferences.reserve_floor.claude.five_hour_left_pct'],
    ['settings:open', 'routing_preferences.reviewer'],
    ['settings:edit:routing_preferences.reviewer', '"claude opus high"'],
    ['settings:edit:routing_preferences.reserve_floor.claude.five_hour_left_pct', '85'],
    ['settings:find', 're'],
  ])
  for (const cols of [36, 46] as const) fitCheck(draw(busy, cols), cols)
  expect(drawnText(draw(busy, 46)).includes('● 2 unsaved'), 'the busy state is dirty').toBe(true)
})

// ---------- S3: the save press hands the ordered payload to the live wire ----------

/** a fake ui with the S3 live wires: save/recheck record their calls (kept
 *  import-free — only Map and the tab's own structural TabUi). */
const liveUiOf = (ui: FakeUi, seen: { saves: unknown[]; rechecks: number }) => ({
  get: (k: string): string | undefined => ui.get(k),
  set: (k: string, v: string): void => { ui.set(k, v) },
  save: (p: unknown): void => { seen.saves.push(p) },
  recheck: (): void => { seen.rechecks++ },
})

test('Settings · S3 save: s save hands the ordered payload to the live wire and keeps the edits; x discard writes nothing', () => {
  const ui: FakeUi = new Map()
  const seen = { saves: [] as unknown[], rechecks: 0 }
  const live = liveUiOf(ui, seen)
  ui.set('settings:scope', 'routing')
  press(SettingsTab(stubEls, settingsView(), 46, live), 'set-routing_preferences.reserve_floor.claude.five_hour_left_pct-inc')
  press(SettingsTab(stubEls, settingsView(), 46, live), 'set-routing_preferences.policy-seg-qual')
  const dirty = SettingsTab(stubEls, settingsView(), 46, live)
  expect(drawnText(dirty).includes('● 2 unsaved'), 'two edits count').toBe(true)
  press(dirty, 'set-save')
  // draw order (policy group before reserve floor), yaml rows, nothing else
  expect(seen.saves).toEqual([{
    sets: [
      { key: 'routing_preferences.policy', value: 'quality', list: false },
      { key: 'routing_preferences.reserve_floor.claude.five_hour_left_pct', value: 45, list: false },
    ],
    globals: [],
    visibility: [],
  }])
  // the tab keeps the edits: only the dashboard's success clears them
  expect(ui.get('settings:edit:routing_preferences.reserve_floor.claude.five_hour_left_pct')).toBe('45')
  press(SettingsTab(stubEls, settingsView(), 46, live), 'set-discard')
  expect(seen.saves).toHaveLength(1) // discard never saves
  expect(ui.get('settings:edit:routing_preferences.reserve_floor.claude.five_hour_left_pct')).toBe('')
})

test('Settings · S3 save: a global row routes to $.config by suffix; a locked row draws no control', () => {
  const base = settingsView()
  const view = {
    ...base,
    mock: false,
    groups: base.groups.map(g => g.scope === 'routing'
      ? {
        ...g,
        rows: g.rows.map(r => r.key === 'routing_preferences.policy'
          ? { ...r, global: true as const }
          : r.key === 'routing_preferences.reserve_floor.claude.five_hour_left_pct'
            ? { ...r, global: true as const, editable: false, why: 'Read-only: locked by policy' }
            : r),
      }
      : g),
  }
  const ui: FakeUi = new Map()
  const seen = { saves: [] as unknown[], rechecks: 0 }
  const live = liveUiOf(ui, seen)
  ui.set('settings:scope', 'routing')
  const drawn = SettingsTab(stubEls, view, 46, live)
  // the locked row offers no control to press (its label press selects only)
  const ctrls = buttonsOf(drawn).filter(b =>
    String(b.props.key).startsWith('set-routing_preferences.reserve_floor.claude.five_hour_left_pct-') &&
    !String(b.props.key).endsWith('-pick'))
  expect(ctrls).toEqual([])
  press(drawn, 'set-routing_preferences.policy-seg-qual')
  press(SettingsTab(stubEls, view, 46, live), 'set-save')
  expect(seen.saves).toEqual([{
    sets: [],
    globals: [{ suffix: 'routing_preferences.policy', value: 'quality' }],
    visibility: [],
  }])
})

// ---------- carried MEDIUM: hotkeys follow the real focus ring ----------

test('Settings · carried MEDIUM: hotkeys are inert while the find Input really has focus; typing s filters, never saves', () => {
  const ui: FakeUi = new Map()
  const seen = { saves: [] as unknown[], rechecks: 0 }
  const live = liveUiOf(ui, seen)
  ui.set('settings:scope', 'routing')
  ui.set('settings:sel', 'routing_preferences.reserve_floor.claude.five_hour_left_pct')
  ui.set('settings:edit:routing_preferences.reserve_floor.claude.five_hour_left_pct', '45') // dirty, so save/discard draw
  ui.set('settings:focus', 'set-find-input') // the dashboard's ui.focus hook records the ring here
  const drawn = SettingsTab(stubEls, settingsView(), 46, live)
  const hotkeys = new Map(buttonsOf(drawn).map(b => [b.props.key, b.props.hotkey]))
  expect(hotkeys.get('set-save'), 's inert').toBeUndefined()
  expect(hotkeys.get('set-discard'), 'x inert').toBeUndefined()
  expect(hotkeys.get('set-find'), 'f inert').toBeUndefined()
  expect(hotkeys.get('set-scope-project'), 'digits inert').toBeUndefined()
  expect(hotkeys.get('set-routing_preferences.reserve_floor.claude.five_hour_left_pct-reset'), 'r inert').toBeUndefined()
  // typing `s` reaches the focused Input alone (the engine's InputProps
  // contract): the filter takes it, no save runs, the edit stands
  const input = nodesOf(drawn, 'Input').find(i => i.props.key === 'set-find-input')
  expect(input, 'the find Input').toBeDefined()
  ;(input?.props.onInput as (v: string) => void)('s')
  expect(ui.get('settings:find')).toBe('s')
  expect(seen.saves).toEqual([])
  expect(ui.get('settings:edit:routing_preferences.reserve_floor.claude.five_hour_left_pct')).toBe('45')
  // the ring moves on: every hotkey is armed again
  ui.set('settings:focus', '')
  const back = SettingsTab(stubEls, settingsView(), 46, live)
  const rehot = new Map(buttonsOf(back).map(b => [b.props.key, b.props.hotkey]))
  expect(rehot.get('set-save')).toBe('s')
  expect(rehot.get('set-find')).toBe('f')
})

test('Settings · carried MEDIUM: a focused text-row Input swallows s the same way', () => {
  const ui: FakeUi = new Map()
  const seen = { saves: [] as unknown[], rechecks: 0 }
  const live = liveUiOf(ui, seen)
  ui.set('settings:scope', 'project')
  ui.set('settings:edit:repo.lanes.slots', '4') // dirty, so save draws
  ui.set('settings:focus', 'set-repo.integration_branch') // the text row's real Input key
  const drawn = SettingsTab(stubEls, settingsView(), 46, live)
  expect(new Map(buttonsOf(drawn).map(b => [b.props.key, b.props.hotkey])).get('set-save'), 's inert').toBeUndefined()
  const input = nodesOf(drawn, 'Input').find(i => i.props.value === 'main')
  expect(input, 'the branch Input').toBeDefined()
  ;(input?.props.onInput as (v: string) => void)('s')
  expect(ui.get('settings:edit:repo.integration_branch')).toBe('"s"')
  expect(seen.saves).toEqual([])
})

test('Settings · carried MEDIUM: a focused Button keeps its hotkey — only an Input swallows keys', () => {
  const ui: FakeUi = new Map()
  ui.set('settings:scope', 'routing')
  ui.set('settings:edit:routing_preferences.reserve_floor.claude.five_hour_left_pct', '45')
  ui.set('settings:focus', 'set-save') // a Button holds the ring, not an Input
  const hotkeys = new Map(buttonsOf(SettingsTab(stubEls, settingsView(), 46, ui)).map(b => [b.props.key, b.props.hotkey]))
  expect(hotkeys.get('set-save')).toBe('s')
})

test('Settings · carried MEDIUM: scope change, find toggle and save/discard clear a stale ring key, re-arming s', () => {
  const ui: FakeUi = new Map()
  const seen = { saves: [] as unknown[], rechecks: 0 }
  const live = liveUiOf(ui, seen)
  ui.set('settings:scope', 'routing')
  ui.set('settings:edit:routing_preferences.reserve_floor.claude.five_hour_left_pct', '45') // dirty, so save draws
  // a scope change unmounts the scope's Inputs with the ring key on them
  ui.set('settings:focus', 'set-find-input')
  press(SettingsTab(stubEls, settingsView(), 46, live), 'set-scope-project')
  expect(ui.get('settings:focus')).toBe('')
  // closing find drops it too (it was opened first, the way the f press does)
  ui.set('settings:scope', 'routing')
  ui.set('settings:findfocus', '1')
  ui.set('settings:focus', 'set-find-input')
  press(SettingsTab(stubEls, settingsView(), 46, live), 'set-find')
  expect(ui.get('settings:findfocus')).toBe('')
  expect(ui.get('settings:focus')).toBe('')
  // save and discard drop it as their footer unmounts
  for (const key of ['set-save', 'set-discard'] as const) {
    ui.set('settings:edit:routing_preferences.reserve_floor.claude.five_hour_left_pct', '45')
    ui.set('settings:focus', 'set-find-input')
    press(SettingsTab(stubEls, settingsView(), 46, live), key)
    expect(ui.get('settings:focus'), `${key} clears the ring key`).toBe('')
  }
  // cleared: `s` is armed on save again, and pressing it saves
  ui.set('settings:edit:routing_preferences.reserve_floor.claude.five_hour_left_pct', '45')
  const armed = SettingsTab(stubEls, settingsView(), 46, live)
  expect(new Map(buttonsOf(armed).map(b => [b.props.key, b.props.hotkey])).get('set-save')).toBe('s')
  press(armed, 'set-save')
  expect(seen.saves).toHaveLength(2) // the earlier loop's save + this one
})

// ---------- S3.2: About live at the render ----------

test('Settings · S3.2 about live: installed/latest versions, what is new, not probed, c re-runs the check, save messages draw', () => {
  const base = settingsView()
  const view = {
    ...base,
    mock: false,
    groups: [
      ...base.groups.filter(g => g.scope !== 'about'),
      {
        scope: 'about' as const,
        title: 'helpers',
        right: 'probe result',
        rows: [{
          key: 'helpers.jev',
          scope: 'about' as const,
          label: 'Jev',
          type: 'seg' as const,
          value: 'not probed',
          default: 'not probed',
          opts: [['ok', 'ok'], ['off', 'off'], ['not probed', '']] as readonly (readonly [string, string])[],
          help: 'Judgement gates.',
          editable: false,
          why: 'Read-only: probed at preflight',
        }],
      },
    ],
    about: { installed: '0.3.0', latest: '0.4.0', latestNote: '', commits: 1, whatsNew: [{ kind: 'feat' as const, line: 'live rows land' }] },
  }
  const ui: FakeUi = new Map()
  const seen = { saves: [] as unknown[], rechecks: 0 }
  const live = liveUiOf(ui, seen)
  ui.set('settings:scope', 'about')
  const flat = drawnText(SettingsTab(stubEls, view, 46, live))
  expect(flat.includes('v0.3.0'), 'installed version').toBe(true)
  expect(flat.includes('v0.4.0'), 'latest tag').toBe(true)
  expect(flat.includes('live rows land'), "what's new").toBe(true)
  expect(flat.includes('not probed'), 'absent helper').toBe(true)
  // `c` stamps the flag and re-runs the latest-tag check through the wire
  press(SettingsTab(stubEls, view, 46, live), 'set-about-check')
  expect(ui.get('settings:about:checked')).toBe('1')
  expect(seen.rechecks).toBe(1)
  // the save-result lines draw over the scope (success and error alike)
  ui.set('settings:save:msg', '✓ saved 2 · backups written first')
  expect(drawnText(SettingsTab(stubEls, view, 46, live)).includes('✓ saved 2 · backups written first'), 'the saved line').toBe(true)
  ui.set('settings:save:msg', 'session.stall_min: below min 5')
  expect(drawnText(SettingsTab(stubEls, view, 46, live)).includes('session.stall_min: below min 5'), 'the error line').toBe(true)
})
