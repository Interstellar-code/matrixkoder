// Steps 1-5 of the setup wizard (plan wizards-redesign-r1, B1.2): each body is pinned to
// its Row 3 slide at the pane's own 46 columns — the card headings, the chips and the dim
// lines, in the order the slide draws them — plus one test per state the slides have no
// counterpart for (a missing repo, a $MATRIXKODER_DATA override, an ignored .gitignore,
// codexbar absent, nothing detected) and B-11 (an empty gate command is named, not blocked).
//
// The harness is hooks/wizard.test.ts's own (world/repoFiles/mk/draftOf), copied because a
// test file's helpers are file-local; the mocks are testkit's, the probes are the canned
// real replies wizard.test.ts records. Nothing here is a snapshot of source text: every
// assertion reads the drawn tree or the draft the step patched.
import { test, expect, mock } from 'claude-code/testing'
import type { Engine, Mounted } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { MkWizardDraft } from '../types'
import { NOW, PROJECT_DIR, DATA_DIR, mockFs, paneProps, sessionStart, type Files } from './testkit'

const HOME = '/home/mk'
const WIZARD = 'matrixkoder-setup'
const COLUMNS = 46

const PKG = JSON.stringify({
  scripts: { dev: 'vite', test: 'vitest run', typecheck: 'tsc --noEmit' },
  devDependencies: { vitest: '^2', eslint: '^9', prettier: '^3', typescript: '^5', react: '^19' },
})

/** the repo the steps 1-5 tests drive: a pnpm + vitest project with a .gitignore */
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

/** clock, env, process (argv → canned stdout, unknown → exit 1), fs and ui.open/close — wizard.test.ts's world, narrowed to what steps 1-5 read. */
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
    if (e.argv[0] === 'mv' || e.argv[0] === 'cp') {
      const [src, dst] = [e.argv[e.argv.length - 2] ?? '', e.argv[e.argv.length - 1] ?? '']
      if (src in files) {
        files[dst] = files[src] ?? ''
        if (e.argv[0] === 'mv') delete files[src]
      }
      return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
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

/** Every Text's whole shown text, in document order (the slides' rows). */
const texts = async (ui: Ui): Promise<string[]> => (await ui.findAll({ type: 'Text' })).map(t => String(t.text))

/** Every Button's label, in document order. */
const labels = async (ui: Ui): Promise<string[]> => (await ui.findAll({ type: 'Button' })).map(b => String(b.text))

/** The provider toggles' labels (the step-4 card's rows, never the nav row). */
const provLabels = async (ui: Ui): Promise<string[]> =>
  (await ui.findAll({ type: 'Button' })).filter(b => String(b.props.key).startsWith('wz-prov-')).map(b => String(b.text))

/** The agent toggles' labels (the step-3 cards' rows, never the nav row). */
const agentLabels = async (ui: Ui): Promise<string[]> =>
  (await ui.findAll({ type: 'Button' })).filter(b => String(b.props.key).startsWith('wz-agent-')).map(b => String(b.text))

/** `rows` appear as whole Texts, in this order (the slide's row order at 46 columns). */
async function pins(ui: Ui, rows: readonly string[]): Promise<void> {
  const all = await texts(ui)
  let at = -1
  for (const row of rows) {
    const found = all.findIndex((t, i) => i > at && t === row)
    expect(found, `${JSON.stringify(row)} drawn after ${at < 0 ? 'the start' : JSON.stringify(all[at])} — drew ${JSON.stringify(all)}`).toBeGreaterThan(at)
    at = found
  }
}

// ---------- per step: the slide's rows, in its order, at 46 columns ----------

test('step 1 (Install) is the slide: the Install mode card, the mode as a chip, the root and the info line', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 1)
  const d = draftOf()
  expect(d.installMode, 'a --plugin-dir install is dev').toBe('dev')
  await pins(ui, [
    'Install mode',
    ' dev ', // the chip's one cell of padding each side
    '(--plugin-dir)',
    d.pluginRoot,
    'Info only — nothing is stored.',
  ])
  // read-only: the slide asks nothing, so the step draws no field at all
  expect(await ui.findAll({ type: 'Input' })).toEqual([])
  expect(await ui.findAll({ type: 'Select' })).toEqual([])
  await ui.unmount()
})

test('step 2 (Data) is the slide: Repo, Data location (the mode as options) and .gitignore cards', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 2)
  await pins(ui, ['Repo', PROJECT_DIR, 'Data location', `Data dir: ${DATA_DIR}`, '.gitignore'])
  // the mode's two options are the slide's own words, and the answer lands in the draft
  const mode = await ui.find({ key: 'wz-datamode' })
  expect(mode, 'the data location is a choice').toBeTruthy()
  expect(JSON.stringify(mode?.props)).toContain('<repo>/.matrixkoder/ (default)')
  expect(JSON.stringify(mode?.props)).toContain(`${HOME}/.matrixkoder/projects/mk-proj/`)
  await ui.select({ key: 'wz-datamode', value: 'home' })
  expect(draftOf().dataMode).toBe('home')
  await ui.unmount()
})

test('step 3 (Agents) is the slide: one card per agent with its state chip, path and model count', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 3)
  // the slide's off state for agy: a press flips the row, its chip and nothing else
  await ui.press({ key: 'wz-agent-agy' })
  expect(draftOf().agents.agy?.enabled).toBe(false)
  // claude is always on and not a press; the rest are the slide's [x]/[ ] rows
  await pins(ui, [
    'Agents (tick to enable)',
    '[x] claude', ' always on ', '/bin/claude',
    ' enabled ', '/bin/codex',
    '[ ] opencode  not found',
    ' off ', '/bin/agy', '2 models: gemini-3.8-flash-high, gemini-3.8-flash-low',
    // the herdr plugin card: the slide's two options
    'herdr plugin',
  ])
  // the pressable agent rows: one per found agent, in the slide's order, each labelled with its hotkey digit
  // (opencode is absent here, so 2 is not drawn and agy keeps 3 — the digit is its index in the toggle list)
  expect(await agentLabels(ui)).toEqual(['1 [x] codex', '3 [ ] agy', '4 [x] herdr'])
  expect(await ui.find({ key: 'wz-herdrPlugin' }), 'herdr is on PATH, so the link is offered').toBeTruthy()
  await ui.unmount()
})

test('step 4 (Usage) is the slide: CodexBar, Providers with the short names, and the z.ai command block', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 4)
  await pins(ui, [
    'CodexBar', '✓ codexbar 0.73.0',
    'Providers (tick to track)',
    'z.ai key (OpenCode plan)',
    'The wizard never asks for or stores API keys. In your own terminal run:',
    `printf '%s' "$Z_AI_API_KEY" | codexbar config set-api-key --provider zai --stdin`,
  ])
  // the slide's capitalised short names, and a listed-but-unticked provider says why in one dim line
  expect(await provLabels(ui)).toEqual(['1 [x] Codex', '2 [x] Claude', '3 [ ] Z.AI', '4 [x] Antigravity'])
  expect(await texts(ui)).toContain('Z.AI: not tracked — its usage is not read; tick it to track.')
  await ui.press({ key: 'wz-prov-zai' })
  expect(draftOf().providersOn).toContain('zai')
  await ui.unmount()
})

test('step 5 (Gates) is the slide: Commands with the four fields, then Baselines and banned', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 5)
  await pins(ui, [
    'Commands',
    'Proposed from package.json. Test suites are not run.',
    'Baselines and banned',
    'Baselines (type errors, lint errors, failing tests): measured at first run.',
    'Banned (dev servers): pnpm run dev',
  ])
  const inputs = await ui.findAll({ type: 'Input' })
  expect(inputs.map(i => i.key)).toEqual(['wz-gate-test', 'wz-gate-typecheck', 'wz-gate-lint', 'wz-gate-build'])
  // the fixture repo has no build script, so build is the one empty gate and the only B-11 line
  expect(await texts(ui)).toContain('empty gates: build — written as "" (no command).')
  await ui.input({ key: 'wz-gate-lint', text: 'pnpm lint' })
  expect(draftOf().gates.lint).toBe('pnpm lint')
  await ui.unmount()
})

// ---------- the five states the slides have no counterpart for ----------

test('the states the slides lack: not a git repository is drawn inside the Repo card', async ($, on) => {
  // no `git rev-parse` reply → the repo is unknown, the code's own state
  world(on, {}, { 'git rev-parse --path-format=absolute --git-common-dir': null })
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 2)
  expect(draftOf().repo).toBeNull()
  const all = await texts(ui)
  expect(all, 'the card heading, then the state as its own row').toContain('Repo')
  expect(all, 'the state line is the row itself, not a prefix of another row').toContain('not a git repository')
  await ui.unmount()
})

test('the states the slides lack: $MATRIXKODER_DATA replaces the data-location choice with its warn line', async ($, on) => {
  world(on, repoFiles(), {}, { MATRIXKODER_DATA: '/srv/mk-data' })
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 2)
  await pins(ui, ['Data location', '$MATRIXKODER_DATA is set: data goes to /srv/mk-data', 'Data dir: /srv/mk-data'])
  expect(await ui.find({ key: 'wz-datamode' }), 'the environment decides, so there is no choice to make').toBeUndefined()
  // the .gitignore question goes with it: the data dir is outside the repo
  expect(await ui.find({ key: 'wz-gitignore' })).toBeUndefined()
  await ui.unmount()
})

test('the states the slides lack: an already-ignored .gitignore is confirmed, not asked again', async ($, on) => {
  const files = repoFiles()
  files[`${PROJECT_DIR}/.gitignore`] = 'node_modules/\n/.matrixkoder/\n'
  world(on, files)
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 2)
  expect(draftOf().gitignoreHas).toBe(true)
  await pins(ui, ['.gitignore', '.gitignore already ignores /.matrixkoder/'])
  expect(await ui.find({ key: 'wz-gitignore' }), 'nothing left to answer').toBeUndefined()
  await ui.unmount()
})

test('the states the slides lack: codexbar absent keeps its own card, with the install hint', async ($, on) => {
  world(on, repoFiles(), { 'codexbar --version': null, 'codexbar config providers': null })
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 4)
  await pins(ui, [
    'CodexBar',
    '⚠ codexbar not found on PATH',
    'Install CodexBar (github.com/steipete/CodexBar) and put its CLI on PATH; usage stays n/a until then. You can continue.',
  ])
  // no provider reply, so there is no list to tick — and the step still moves on
  expect(await provLabels(ui)).toEqual([])
  expect(await ui.find({ key: 'wz-next' })).toBeTruthy()
  await ui.unmount()
})

test('the states the slides lack: nothing detected says so in the Commands card', async ($, on) => {
  world(on, {})
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 5)
  expect(draftOf().gatesFrom).toBe('')
  await pins(ui, ['Commands', 'Nothing detected — Tab to a field and type the command you use.'])
  const inputs = await ui.findAll({ type: 'Input' })
  expect(inputs[0]?.props.autoFocus, 'the first field takes the focus, so the step is typeable at once').toBe(true)
  await ui.unmount()
})

// ---------- B-11: the gate fields ----------

test('B-11: an empty gate command is named in one dim line and never blocks Next', async ($, on) => {
  // this repo's own shape: plugin.json + selftest + tsconfig proposes no build command
  const files: Files = {
    [`${PROJECT_DIR}/.claude-plugin/plugin.json`]: '{}',
    [`${PROJECT_DIR}/scripts/selftest.sh`]: '#!/usr/bin/env bash\n',
    [`${PROJECT_DIR}/tsconfig.json`]: '{}',
  }
  world(on, files)
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 5)
  const d = draftOf()
  expect(d.gates.build).toBe('')
  await pins(ui, ['Commands', 'empty gates: build — written as "" (no command).'])
  // non-blocking: the slide draws no error state and Next still walks on
  expect(await ui.find({ key: 'wz-next' })).toBeTruthy()
  await ui.press({ key: 'wz-next' })
  expect(await ui.find({ text: /STEP 6\/10/ })).toBeTruthy()
  // typing the command clears its name from the line
  await ui.press({ key: 'wz-back' })
  await ui.input({ key: 'wz-gate-build', text: 'pnpm build' })
  expect((await texts(ui)).some(t => t.startsWith('empty gates:')), 'nothing empty any more').toBe(false)
  await ui.unmount()
})

test('B-11: an emptied gate is named again; none left draws no line', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($)
  const ui = await mountAt($, 5)
  // the fixture repo declares no build script, so build alone is named
  expect(await texts(ui)).toContain('empty gates: build — written as "" (no command).')
  await ui.input({ key: 'wz-gate-build', text: 'pnpm build' })
  expect((await texts(ui)).some(t => t.startsWith('empty gates:')), 'nothing empty any more').toBe(false)
  await ui.input({ key: 'wz-gate-typecheck', text: '' })
  expect(draftOf().gates.typecheck).toBe('')
  expect(await texts(ui)).toContain('empty gates: typecheck — written as "" (no command).')
  await ui.input({ key: 'wz-gate-typecheck', text: 'tsc --noEmit' })
  expect((await texts(ui)).some(t => t.startsWith('empty gates:')), 'nothing empty again').toBe(false)
  await ui.unmount()
})
