// Steps 6-10 of the setup wizard (plan wizards-redesign-r1, B1.3): each body is pinned to its
// Row 3 slide at the pane's own 46 columns — card headings, the controls' labels, the chips and
// the dim notes, in the order the slide draws them — plus the states the slides have no
// counterpart for (a branch that cannot be written, a poll that falls back to 5, a builder order
// naming an agent that is off) and the two review forms' whole-file / `-`/`+` previews.
//
// The harness is hooks/wizard.test.ts's own (world/repoFiles/mk/draftOf, copied because a test
// file's helpers are file-local); the probes are the canned real replies it records. Every
// assertion reads the drawn tree or the draft a press patched — no source text, no snapshot.
import { test, expect, mock } from 'claude-code/testing'
import type { Engine, Mounted } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { MkWizardDraft } from '../types'
import { NOW, PROJECT_DIR, DATA_DIR, mockFs, paneProps, sessionStart, type Files } from './testkit'

const HOME = '/home/mk'
const WIZARD = 'matrixkoder-setup'
const YAML = `${DATA_DIR}/matrixkoder.yaml`
const COLUMNS = 46

const PKG = JSON.stringify({
  scripts: { dev: 'vite', test: 'vitest run', typecheck: 'tsc --noEmit' },
  devDependencies: { vitest: '^2', eslint: '^9', prettier: '^3', typescript: '^5', react: '^19' },
})

/** the repo these tests drive: a pnpm + vitest project with a .gitignore */
function repoFiles(): Files {
  return {
    [`${PROJECT_DIR}/package.json`]: PKG,
    [`${PROJECT_DIR}/pnpm-lock.yaml`]: '',
    [`${PROJECT_DIR}/tsconfig.json`]: '{}',
    [`${PROJECT_DIR}/.gitignore`]: 'node_modules/\n',
  }
}

const PROCS: Record<string, string | null> = {
  'git rev-parse --path-format=absolute --git-common-dir': `${PROJECT_DIR}/.git\n`,
  'git symbolic-ref --short HEAD': 'main\n',
  'which claude codex opencode agy herdr': '/bin/claude\n/bin/codex\n/bin/agy\n/bin/herdr\n',
  'agy models': 'Fetching available models...\ngemini-3.8-flash-high\tGemini 3.8 Flash (High)\ngemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n',
  'codexbar --version': 'CodexBar 0.73.0\n',
  'codexbar config providers': 'codex: enabled default (Codex)\nopenai: disabled (OpenAI)\nclaude: enabled (Claude)\nzai: disabled (z.ai / GLM)\nantigravity: enabled (Antigravity)\n',
  'mkdir -p': '',
}

type World = { argv: string[][]; opened: string[]; closed: string[]; state: Map<string, { value: unknown; version: number }> }

/** clock, env, process (argv → canned stdout, unknown → exit 1), fs and ui.open/close — wizard.test.ts's world, narrowed to what steps 6-10 read. */
function world(on: On, files: Files, procs: Record<string, string | null> = {}, env: Record<string, string> = {}): World {
  const w: World = { argv: [], opened: [], closed: [], state: new Map() }
  worldOf = w
  const table = { ...PROCS, ...procs }
  mock.clock(on, { now: NOW })
  mock.env(on, { HOME, ...env })
  mockFs(on, files, [`${PROJECT_DIR}/.git`])
  on('fs.exists', (_$, e) => ({ value: e.path in files }))
  on('session.start', (_$, _e) => ({ cwd: PROJECT_DIR }))
  on('prompt.compose', (_$, _e) => ({ sections: [] }))
  on('process.run', async (_$, e) => {
    w.argv.push([...e.argv])
    // mv / cp act on the in-memory files, as the real commands would on disk
    if (e.argv[0] === 'mv' || e.argv[0] === 'cp') {
      const [src, dst] = [e.argv[e.argv.length - 2] ?? '', e.argv[e.argv.length - 1] ?? '']
      if (src in files) {
        files[dst] = files[src] ?? ''
        if (e.argv[0] === 'mv') delete files[src]
      }
      return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    // L10.7 / B-3: the root-table registration and the project store, with the replies the real scripts answer
    if (e.argv[1]?.endsWith('/scripts/mk-state.py') && e.argv[2] === 'project' && e.argv[3] === 'register') {
      return { value: { exitCode: 0, stdout: '{"ok": true}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    if (e.argv[1]?.endsWith('/scripts/mk-state.py') && e.argv[2] === 'sync') {
      return { value: { exitCode: 0, stdout: '{"ok": true, "store": "on", "project": "mk-proj", "copied": 0}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    const key = e.argv[0] === 'mkdir' ? 'mkdir -p' : e.argv.join(' ')
    const out = table[key]
    return { value: out == null ? { exitCode: 1, stdout: '', stderr: 'not found', isStdoutTruncated: false, isStderrTruncated: false } : { exitCode: 0, stdout: out, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('ui.open', (_$, e) => {
    w.opened.push(e.id)
    return { value: { isPlaced: true } }
  })
  on('ui.close', (_$, e) => {
    w.closed.push(e.id)
    return { value: undefined }
  })
  on('state.set', async (_$, e, next) => {
    const r = await next(e)
    w.state.set(`${e.key}:${e.id ?? ''}`, { value: e.value, version: 0 })
    return r
  })
  on('ui.status', (_$, _e) => ({ value: undefined }))
  on('ui.toast', (_$, _e) => ({ value: undefined }))
  return w
}

let worldOf: World | null = null // the world the running test built (tests run one at a time)

function draftOf(): MkWizardDraft {
  const v = worldOf?.state.get('wizardDraft:')?.value
  if (v == null) throw new Error('no wizard draft')
  return v as MkWizardDraft
}

/** `/mk setup` as the engine raises it (fullscreen: the pane opens). */
function mk($: Engine, args = 'setup') {
  return $.command.run({ command: 'mk', args, origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 100 } })
}

type Ui = Mounted<'terminal', 'Pane'>

/** The pane at the slides' 46 columns, on the step asked for (the wizard opens on 1). */
async function mountAt($: Engine, step: number): Promise<Ui> {
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(COLUMNS) })
  for (let i = 1; i < step; i++) await ui.press({ key: 'wz-next' })
  return ui
}

/** Every Text's whole shown text, in document order (the slide's rows). */
const texts = async (ui: Ui): Promise<string[]> => (await ui.findAll({ type: 'Text' })).map(t => String(t.text))

/** The body's rows in document order: a Text's text, an Input's label, a Select's label — the three
 *  things a slide row is drawn as (a heading, a dim label, or the control's own label). */
async function rows(ui: Ui): Promise<string[]> {
  const found = await ui.findAll({})
  return found
    .filter(el => el.type === 'Text' || el.type === 'Input' || el.type === 'Select')
    .map(el => (el.type === 'Text' ? String(el.text) : String(el.props.label ?? '')))
    .filter(r => r !== '')
}

/** `wanted` appear as rows, in this order (the slide's row order at 46 columns). */
async function pins(ui: Ui, wanted: readonly string[]): Promise<void> {
  const all = await rows(ui)
  let at = -1
  for (const row of wanted) {
    const found = all.findIndex((t, i) => i > at && t === row)
    expect(found, `${JSON.stringify(row)} drawn after ${at < 0 ? 'the start' : JSON.stringify(all[at])} — drew ${JSON.stringify(all)}`).toBeGreaterThan(at)
    at = found
  }
}

/** The preview card's drawn rows: everything between its right-hand title row ('exactly what is
 *  written' / 'diff vs your file') and its `↑↓ scroll` footer, in document order. Nothing else in
 *  the pane sits between those two rows, so this is the preview as the user reads it. */
async function previewRows(ui: Ui, title: string): Promise<string[]> {
  const all = await texts(ui)
  const start = all.indexOf(title)
  expect(start, `the preview card's title row ${JSON.stringify(title)}`).toBeGreaterThanOrEqual(0)
  const rest = all.slice(start + 1)
  const end = rest.findIndex(t => t.includes('↑↓ scroll'))
  expect(end, 'the preview card closes with its scroll footer').toBeGreaterThanOrEqual(0)
  return rest.slice(0, end)
}

// ---------- per step: the slide's rows, in its order, at 46 columns ----------

test('step 6 (Routing) is the slide: Builder order, Lanes, Claude reserve floor, Branch', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 6)
  await pins(ui, [
    'Builder order',
    'Builder order', // the slide's dim label under the card heading, drawn as the Input's own label
    'Lanes', 'Max build lanes',
    'Claude reserve floor', '5h left %', 'weekly left %', // one heading over both floors
    'Branch', 'Integration branch',
  ])
  // the four cards' controls, by key, and the branch field the doc drift list said was undocumented
  // (the derived order is the default filtered to the enabled agents: opencode is not installed here)
  expect((await ui.find({ key: 'wz-builderOrder' }))?.props.value).toBe('codex, agy, claude')
  expect((await ui.find({ key: 'wz-maxLanes' }))?.props.value).toBe('3')
  expect((await ui.find({ key: 'wz-claudeFiveHour' }))?.props.value).toBe('40')
  expect((await ui.find({ key: 'wz-claudeWeekly' }))?.props.value).toBe('30')
  expect((await ui.find({ key: 'wz-branch' }))?.props.value).toBe('main')
  // the lanes card offers 1..5 and the two floors the slide's own lists
  const lanes = await ui.find({ key: 'wz-maxLanes' })
  expect(JSON.stringify(lanes?.props)).toContain('{"value":"5"}')
  await ui.unmount()
})

test('step 7 (Autonomy) is the slide: Permissions with the worktree line, then Pre-authorised', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 7)
  await pins(ui, [
    'Permissions',
    'Builders skip permission prompts', // the slide's label WITHOUT the worktree clause
    'Own worktree only.', // the slide's own dim line, not a parenthetical in the label
    'Pre-authorised',
    'Pre-authorised',
  ])
  const all = await texts(ui)
  expect(all.some(t => t.includes('(own worktree only)')), 'the clause moved out of the label').toBe(false)
  expect((await ui.find({ key: 'wz-skipPermissions' }))?.props.value).toBe('yes')
  await ui.select({ key: 'wz-skipPermissions', value: 'no' })
  expect(draftOf().skipPermissions).toBe(false)
  await ui.unmount()
})

test('step 8 (QA) is the slide: Frontend, Run, Check, then the target with its dim line', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 8)
  await pins(ui, [
    'Frontend', 'Has a frontend',
    'Run', 'Dev URL', 'Start command (orchestrator only)',
    'Check', 'Themes (comma separated)', 'Mockups dir', // the two fields the doc omitted
    'QA integration target',
    'local-main = QA on the running dev server', // its own dim line, not part of an option label
  ])
  const target = await ui.find({ key: 'wz-qaTarget' })
  expect(JSON.stringify(target?.props), 'the options are the bare values the slide chips').not.toContain('QA on the running dev server')
  expect((await ui.find({ key: 'wz-qaThemes' }))?.props.label).toBe('Themes (comma separated)')
  expect((await ui.find({ key: 'wz-qaMockups' }))?.props.label).toBe('Mockups dir')
  await ui.select({ key: 'wz-qaTarget', value: 'branch' })
  expect(draftOf().qaTarget).toBe('branch')
  await ui.unmount()
})

test('step 9 (Dashboard) is the slide: Pane, Usage poll, Plugin settings today — and no Apply button', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 9)
  await pins(ui, [
    'Pane',
    'Auto-open (fullscreen, ≥144 cols)',
    'Status chip', 'Toasts',
    'Usage poll', 'Usage poll (minutes)',
    'Plugin settings today',
    "autoOpen on, chip on, toasts on, poll 5 min. These live in the plugin's own settings (claude plugin configure matrixkoder), not in the yaml.",
    'Saved with everything else at step 10 · Review lists them',
  ])
  // B-5: the prefs are written at the step-10 confirm, so this step has no button of its own
  expect(await ui.find({ key: 'wz-dash-apply' }), 'no Apply button on step 9').toBeUndefined()
  expect(await ui.find({ text: /Apply these settings/ })).toBeUndefined()
  await ui.unmount()
})

test('step 10 (Review) is the slide: Will write, the whole file, then w Write these', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 10)
  await pins(ui, [
    'Will write',
    `create  ${DATA_DIR}/matrixkoder.yaml`,
    `create  ${DATA_DIR}/plans/ and runs/`,
    'register this repo in the root projects table',
    'set     4 dashboard prefs (plugin settings)', // B-5: the prefs are named here, they land with this write
    'matrixkoder.yaml · new file',
    'exactly what is written',
  ])
  expect((await previewRows(ui, 'exactly what is written')).length, 'the whole file is drawn').toBeGreaterThan(40)
  // `w Write these` is the nav row's button (the slide puts it between Back and Cancel), not a card's:
  // the card body draws no Button at all, and the nav holds the three keys in the slide's order
  expect(await ui.findAll({ type: 'Button' }), 'the body is cards and fields; every Button is the nav row').toHaveLength(3)
  expect((await ui.findAll({ type: 'Button' })).map(b => String(b.props.key))).toEqual(['wz-back', 'wz-write', 'wz-cancel'])
  expect((await ui.find({ key: 'wz-write' }))?.props.hotkey, 'pressed with w').toBe('w')
  expect(String((await ui.find({ key: 'wz-write' }))?.text), 'one `w`, the slide\'s own label').toBe('w Write these')
  await ui.unmount()
})

// ---------- B-6: the preview is exactly what is written ----------

test('B-6: the create preview draws every line of the file the write lands, with no cut line and no pending', async ($, on) => {
  const files = repoFiles()
  world(on, files)
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 10)
  const drawn = await previewRows(ui, 'exactly what is written')
  const all = await texts(ui)
  expect(all.some(t => /more lines/.test(t)), 'no `… N more lines` cut').toBe(false)
  expect(drawn.some(t => t.includes('pending')), 'no `checked_at: pending` placeholder').toBe(false)
  // every row is drawn the slide's way: the whole file, each line as an addition
  expect(drawn.every(t => t.startsWith('+ ')), 'each drawn row is the slide\'s `+ <line>`').toBe(true)
  // the footer counts what is drawn, and the count is the file's own
  const footer = all.find(t => t.startsWith('↑↓ scroll · all '))
  expect(footer).toBe(`↑↓ scroll · all ${String(drawn.length)} lines`)
  await ui.press({ key: 'wz-write' })
  const written = (files[YAML] ?? '').replace(/\n$/, '').split('\n')
  expect(drawn.length, 'the drawn rows are the written file, line for line').toBe(written.length)
  expect(drawn.map(t => t.slice(2)), 'and the same text, in the same order').toEqual(written)
  await ui.unmount()
})

test('B-6: the replace form draws `-`/`+` rows and a changed · unchanged count', async ($, on) => {
  // the file on disk is the one this wizard wrote a moment ago, with one gate edited in between: the
  // replace form must show exactly that one line as `-`/`+` and call the rest unchanged (the slide's
  // `1 line changed · 24 unchanged`), and it must draw the whole file, never a cut.
  const files = repoFiles()
  world(on, files)
  await $.session.start(sessionStart())
  await mk($)
  const first = await mountAt($, 10)
  await first.press({ key: 'wz-write' })
  await first.unmount()
  const written = files[YAML] ?? ''
  expect(written).toContain('  test: pnpm exec vitest run')
  files[YAML] = written.replace('  test: pnpm exec vitest run   # proposed', '  test: pnpm test')

  await mk($)
  const ui = await mountAt($, 10)
  // the default stays keep: the create form, not a diff
  expect(await ui.find({ text: 'What a replace changes' }), 'keep draws no diff').toBeUndefined()
  await ui.select({ key: 'wz-replace', value: 'replace' })
  expect(await ui.find({ text: 'What a replace changes' }), 'the replace form names what it changes').toBeTruthy()
  const diff = await previewRows(ui, 'diff vs your file')
  expect(diff.filter(t => t.startsWith('- ')), 'the one edited line, as a removal').toEqual(['-   test: pnpm test'])
  expect(diff.filter(t => t.startsWith('+ ')), 'and as the addition the form would write').toEqual(['+   test: pnpm exec vitest run   # proposed'])
  // every other line is drawn with the diff's own unchanged marker: a leading space
  expect(diff.filter(t => t.startsWith(' ')).length, 'the rest of the file is drawn unchanged').toBeGreaterThan(40)
  const footer = (await texts(ui)).find(t => /changed · \d+ unchanged · ↑↓ scroll$/.test(t))
  expect(footer, "the slide's changed · unchanged count").toBe('1 line changed · 66 unchanged · ↑↓ scroll')
  expect((await texts(ui)).some(t => /more lines/.test(t)), 'the diff is whole too').toBe(false)
  // whole and exact: the rows that survive the replace (unchanged and `+`) are the file the replace
  // writes, line for line — so the form never hides a line the user is about to lose
  const merged = diff.filter(t => !t.startsWith('- ')).map(t => t.slice(2))
  await ui.press({ key: 'wz-write' })
  const after = (files[YAML] ?? '').replace(/\n$/, '').split('\n')
  expect(merged.length, 'the diff carries every line of the new file, none cut').toBe(after.length)
  expect(merged, 'and the same text, in the same order').toEqual(after)
  await ui.unmount()
})

// ---------- B-7: the builder-order field keeps what is typed ----------

test('B-7: typing `codex, agy` one character at a time keeps it, and a name that is not an agent is named', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 6)
  // opencode is not installed in this world, so it is not an enabled agent
  expect(draftOf().agents.opencode?.enabled).toBe(false)
  const typed = async (): Promise<string> => String((await ui.find({ key: 'wz-builderOrder' }))?.props.value ?? '')
  const target = 'codex, agy'
  for (let i = 1; i <= target.length; i++) {
    const sofar = target.slice(0, i)
    await ui.input({ key: 'wz-builderOrder', text: sofar, kind: 'change' })
    expect(await typed(), `after typing ${JSON.stringify(sofar)} the field still holds it`).toBe(sofar)
    expect(draftOf().builderOrder, 'and so does the draft').toBe(sofar)
  }
  expect(await typed()).toBe('codex, agy')
  // every name in it is an enabled agent here, so nothing is named
  expect((await texts(ui)).some(t => t.includes('not an enabled agent')), 'both names are enabled').toBe(false)
  // a name that is not an enabled agent is named in one dim line and still written as typed
  await ui.input({ key: 'wz-builderOrder', text: 'codex, opencode', kind: 'change' })
  expect(await texts(ui)).toContain('⚠ not an enabled agent: opencode — written as typed')
  expect(await typed()).toBe('codex, opencode')
  await ui.unmount()
})

// ---------- B-11 for steps 6-10: named, never blocking ----------

test('B-11: an empty or spaced integration branch is named in the Branch card', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 6)
  expect((await texts(ui)).some(t => t.includes('the integration branch')), 'a valid branch names nothing').toBe(false)
  await ui.input({ key: 'wz-branch', text: '', kind: 'change' })
  expect(draftOf().branch).toBe('')
  expect(await texts(ui)).toContain('the integration branch is empty — written as ""  (lanes merge here)')
  await ui.input({ key: 'wz-branch', text: 'feature/my lane', kind: 'change' })
  expect(await texts(ui)).toContain('the integration branch cannot hold spaces — written as "feature/my lane"')
  // non-blocking: Next still walks on with the invalid value
  expect(await ui.find({ key: 'wz-next' })).toBeTruthy()
  await ui.press({ key: 'wz-next' })
  expect(await ui.find({ text: /STEP 7\/10/ })).toBeTruthy()
  await ui.unmount()
})

test('B-11: a poll of 0 or abc is named as written 5, and the settings line says 5', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 9)
  expect((await texts(ui)).some(t => t.includes('written as 5')), 'a valid poll names nothing').toBe(false)
  await ui.input({ key: 'wz-pollMin', text: '0', kind: 'change' })
  expect(await texts(ui)).toContain('written as 5: "0" is not a whole number of minutes ≥ 1')
  expect((await texts(ui)).some(t => t.includes('poll 5 min.')), 'the settings line carries the fallback').toBe(true)
  await ui.input({ key: 'wz-pollMin', text: 'abc', kind: 'change' })
  expect(await texts(ui)).toContain('written as 5: "abc" is not a whole number of minutes ≥ 1')
  expect(await ui.find({ key: 'wz-next' }), 'still non-blocking').toBeTruthy()
  // a real interval is carried as itself: both lines stop naming the fallback
  await ui.input({ key: 'wz-pollMin', text: '7', kind: 'change' })
  expect((await texts(ui)).some(t => t.includes('written as 5')), 'a whole number ≥ 1 names nothing').toBe(false)
  expect((await texts(ui)).some(t => t.includes('poll 7 min.')), 'the settings line follows the field').toBe(true)
  await ui.unmount()
})
