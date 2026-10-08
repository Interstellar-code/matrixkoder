// Onboarding wizard tests (plan §7): detection pre-fill, step navigation,
// the review writes, keep-vs-replace, no API key anywhere, the inline
// fallback and /mk setup opening the pane. Mocked: process results, file
// contents and the clock only; the files are written through $.fs into an
// in-memory dict (testkit's mockFs).
import { test, expect, mock } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { MkWizardDraft } from '../types'
import { NOW, PROJECT_DIR, DATA_DIR, mockFs, paneProps, sessionStart, type Files } from './testkit'
import { addToGitignore, buildYaml, emptyDraft, inline, mergeModels, parseModels, parseProviders, parseWhich, proposeGates } from './wizard-core'

const HOME = '/home/mk'
const WIZARD = 'matrixkoder-setup'
const YAML = `${DATA_DIR}/matrixkoder.yaml`
const CONFIG = `${HOME}/.matrixkoder/config.json`
const PROJECTS = `${HOME}/.matrixkoder/projects.json`
const ISO_NOW = new Date(NOW).toISOString()

const PKG = JSON.stringify({
  scripts: { dev: 'vite', test: 'vitest run', typecheck: 'tsc --noEmit' },
  devDependencies: { vitest: '^2', eslint: '^9', prettier: '^3', typescript: '^5', react: '^19' },
})

/** repo fixtures: a pnpm + vitest project with a .gitignore */
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

/** clock, env, process (argv → canned stdout, unknown → exit 1), fs and ui.open/close. `procs` overrides PROCS. */
function world(on: On, files: Files, procs: Record<string, string | null> = {}, env: Record<string, string> = {}, unreadable: readonly string[] = []): World {
  const w: World = { argv: [], opened: [], closed: [], state: new Map() }
  worldOf = w
  const table = { ...PROCS, ...procs }
  mock.clock(on, { now: NOW })
  mock.env(on, { HOME, ...env })
  mockFs(on, files, [`${PROJECT_DIR}/.git`])
  // `unreadable`: exists, but fs.read rejects (no content served), like an EACCES file
  on('fs.exists', (_$, e) => ({ value: e.path in files || unreadable.includes(e.path) }))
  on('session.start', (_$, _e) => ({ cwd: PROJECT_DIR }))
  on('prompt.compose', (_$, _e) => ({ sections: [] }))
  on('process.run', (_$, e) => {
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
  // observe the engine's own state layer (the tests read the draft back from here)
  on('state.set', async (_$, e, next) => {
    const r = await next(e)
    w.state.set(`${e.key}:${e.id ?? ''}`, { value: e.value, version: 0 })
    return r
  })
  on('ui.status', (_$, _e) => ({ value: undefined }))
  on('ui.toast', (_$, _e) => ({ value: undefined }))
  return w
}

const SURFACES = ['terminal', 'desktop'] as const

/** `/mk <args>` as the engine raises it: presentation stamped (fullscreen unless said otherwise). */
function mk($: Engine, args: string, isFullscreen = true) {
  return $.command.run({ command: 'mk', args, origin: { kind: 'composer' }, presentation: { isFullscreen, columns: 100 } })
}

let worldOf: World | null = null // the world the running test built (tests run one at a time)

function draftOf(): MkWizardDraft {
  const v = worldOf?.state.get('wizardDraft:')?.value
  if (v == null) throw new Error('no wizard draft')
  return v as MkWizardDraft
}
function stepOf(): unknown {
  return worldOf?.state.get('wizardStep:')?.value
}

// ---------- pure halves ----------

test('yaml emitter quotes what must be quoted and nothing else', () => {
  expect(inline('claude-sparing')).toBe('claude-sparing')
  expect(inline('pnpm vitest run {paths}')).toBe('"pnpm vitest run {paths}"')
  expect(inline('')).toBe('""')
  expect(inline('true')).toBe('"true"')
  expect(inline('3000')).toBe('"3000"')
  expect(inline('https://localhost:3000')).toBe('"https://localhost:3000"')
  expect(inline(['a', 'b c'])).toBe('[a, b c]')
  expect(inline({ available: true, models: [] })).toBe('{ available: true, models: [] }')
  expect(inline(null)).toBe('null')
})

test('parsers: which, providers, models', () => {
  expect(parseWhich('/bin/claude\n/usr/local/bin/agy\n')).toEqual({ claude: '/bin/claude', agy: '/usr/local/bin/agy' })
  expect(parseProviders('codex: enabled default (Codex)\nopenai: disabled (OpenAI)\nnoise')).toEqual([{ name: 'codex', enabled: true }, { name: 'openai', enabled: false }])
  expect(parseModels('Fetching available models...\ngemini-3.8-flash-high\tGemini\nopencode/big-pickle\n')).toEqual(['gemini-3.8-flash-high', 'opencode/big-pickle'])
  const manyModels = Array.from({ length: 20 }, (_, i) => `opencode/model-${i + 1}`).join('\n')
  expect(parseModels(manyModels).length).toBe(20)
  const merged = mergeModels(['opencode/a', 'opencode/b'], ['zai-coding-plan/p1', 'zai-coding-plan/p2'])
  expect(merged).toEqual(['zai-coding-plan/p1', 'zai-coding-plan/p2', 'opencode/a', 'opencode/b'])
})

test('gate proposals: pnpm+vitest, python, Makefile; never invents', () => {
  const js = proposeGates({ pkg: JSON.parse(PKG), pyproject: null, makefile: null, locks: ['pnpm-lock.yaml'], hasTsconfig: true, markers: [] })
  expect(js.gates).toEqual({
    install: 'pnpm install --frozen-lockfile --prefer-offline',
    test: 'pnpm exec vitest run {paths}',
    typecheck: 'pnpm run typecheck',
    lint_changed: 'pnpm exec eslint {files}',
    format_changed: 'pnpm exec prettier --write {files}',
  })
  expect(js.banned).toEqual(['pnpm run dev'])
  const py = proposeGates({ pkg: null, pyproject: '[tool.pytest]\n[tool.ruff]\n[tool.mypy]', makefile: null, locks: ['uv.lock'], hasTsconfig: false, markers: [] })
  expect(py.gates).toEqual({ install: 'uv sync', test: 'pytest {paths}', typecheck: 'mypy .', lint_changed: 'ruff check {files}', format_changed: 'ruff format {files}' })
  const mk = proposeGates({ pkg: null, pyproject: null, makefile: 'test:\n\tgo test\nlint:\n\tvet\n', locks: [], hasTsconfig: false, markers: [] })
  expect(mk.gates.test).toBe('make test')
  expect(mk.gates.typecheck).toBe('')
  expect(proposeGates({ pkg: null, pyproject: null, makefile: null, locks: [], hasTsconfig: false, markers: [] }).from).toBe('')
})

test('gate proposals: marker files (plugin, selftest, tsconfig-only, Cargo, go.mod)', () => {
  // this repo's shape: plugin.json + scripts/selftest.sh + tsconfig.json, no package.json
  const self = proposeGates({ pkg: null, pyproject: null, makefile: null, locks: [], hasTsconfig: true, markers: ['.claude-plugin/plugin.json', 'scripts/selftest.sh'] })
  expect(self.gates).toEqual({
    install: '', test: 'claude plugin test . && bash scripts/selftest.sh', typecheck: 'tsc -p . --noEmit',
    lint_changed: 'claude plugin validate .', format_changed: '',
  })
  expect(self.from).toBe('.claude-plugin/plugin.json + scripts/selftest.sh + tsconfig.json')
  const plugin = proposeGates({ pkg: null, pyproject: null, makefile: null, locks: [], hasTsconfig: false, markers: ['.claude-plugin/plugin.json'] })
  expect(plugin.gates.test).toBe('claude plugin test .')
  expect(plugin.gates.lint_changed).toBe('claude plugin validate .')
  expect(plugin.from).toBe('.claude-plugin/plugin.json')
  const selftest = proposeGates({ pkg: null, pyproject: null, makefile: null, locks: [], hasTsconfig: false, markers: ['scripts/selftest.sh'] })
  expect(selftest.gates.test).toBe('bash scripts/selftest.sh')
  expect(selftest.from).toBe('scripts/selftest.sh')
  const ts = proposeGates({ pkg: null, pyproject: null, makefile: null, locks: [], hasTsconfig: true, markers: [] })
  expect(ts.gates.typecheck).toBe('tsc -p . --noEmit')
  expect(ts.from).toBe('tsconfig.json')
  const cargo = proposeGates({ pkg: null, pyproject: null, makefile: null, locks: [], hasTsconfig: false, markers: ['Cargo.toml'] })
  expect(cargo.gates).toEqual({ install: '', test: 'cargo test', typecheck: '', lint_changed: 'cargo clippy', format_changed: 'cargo fmt' })
  const go = proposeGates({ pkg: null, pyproject: null, makefile: null, locks: [], hasTsconfig: false, markers: ['go.mod'] })
  expect(go.gates.test).toBe('go test ./...')
  expect(go.gates.lint_changed).toBe('go vet ./...')
  // a Makefile target still wins over a marker stack
  const mkCargo = proposeGates({ pkg: null, pyproject: null, makefile: 'test:\n\tcargo test\n', locks: [], hasTsconfig: false, markers: ['Cargo.toml'] })
  expect(mkCargo.gates.test).toBe('make test')
  expect(mkCargo.gates.lint_changed).toBe('cargo clippy')
  expect(mkCargo.from).toBe('Makefile + Cargo.toml')
  // package.json keeps its own typecheck; markers add nothing (a consulted marker still lands in from, like a Makefile)
  const pkgCargo = proposeGates({ pkg: JSON.parse(PKG), pyproject: null, makefile: null, locks: [], hasTsconfig: true, markers: ['Cargo.toml'] })
  expect(pkgCargo.gates.test).toBe('npx vitest run {paths}')
  expect(pkgCargo.gates.typecheck).toBe('npm run typecheck')
  expect(pkgCargo.from).toBe('package.json + Cargo.toml')
  // finding 3: selftest + Cargo gives cargo test && bash scripts/selftest.sh
  const cargoSelftest = proposeGates({ pkg: null, pyproject: null, makefile: null, locks: [], hasTsconfig: false, markers: ['scripts/selftest.sh', 'Cargo.toml'] })
  expect(cargoSelftest.gates.test).toBe('cargo test && bash scripts/selftest.sh')
  expect(cargoSelftest.gates.lint_changed).toBe('cargo clippy')
  expect(cargoSelftest.gates.format_changed).toBe('cargo fmt')
  expect(cargoSelftest.from).toBe('Cargo.toml + scripts/selftest.sh')
  // finding 4: package.json and Makefile test stay unchanged when selftest is present
  const pkgSelftest = proposeGates({ pkg: JSON.parse(PKG), pyproject: null, makefile: null, locks: [], hasTsconfig: true, markers: ['scripts/selftest.sh'] })
  expect(pkgSelftest.gates.test).toBe('npx vitest run {paths}')
  expect(pkgSelftest.from).toBe('package.json')
  const mkSelftest = proposeGates({ pkg: null, pyproject: null, makefile: 'test:\n\tgo test\nlint:\n\tvet\n', locks: [], hasTsconfig: false, markers: ['scripts/selftest.sh'] })
  expect(mkSelftest.gates.test).toBe('make test')
  expect(mkSelftest.from).toBe('Makefile')
})

test('gitignore append is idempotent', () => {
  expect(addToGitignore('node_modules/\n')).toBe('node_modules/\n/.matrixkoder/\n')
  expect(addToGitignore('node_modules/')).toBe('node_modules/\n/.matrixkoder/\n')
  expect(addToGitignore('/.matrixkoder/\n')).toBeNull()
  expect(addToGitignore(null)).toBe('/.matrixkoder/\n')
})

test('baselines stay null and gates are marked proposed', () => {
  const d = emptyDraft()
  d.gates.test = 'make test'
  const yaml = buildYaml(d, ISO_NOW)
  expect(yaml).toContain('baselines:')
  expect(yaml).toContain('  type_errors: null')
  expect(yaml).toContain('  test: make test   # proposed')
  expect(yaml).toContain('  install: ""')
})

// ---------- /mk setup and the pane ----------

test('/mk setup opens the wizard pane and detection pre-fills the draft', async ($, on) => {
  const files = repoFiles()
  const w = world(on, files)
  await $.session.start(sessionStart())
  const ran = await mk($, 'setup')
  expect(ran.text).toContain('setup opened')
  expect(w.opened).toContain(WIZARD)
  const d = draftOf()
  expect(d.loaded).toBe(true)
  expect(d.repo).toBe(PROJECT_DIR)
  expect(d.slug).toBe('mk-proj')
  expect(d.branch).toBe('main')
  expect(d.installMode).toBe('dev')
  expect(d.agents.codex).toMatchObject({ found: true, enabled: true })
  expect(d.agents.opencode).toMatchObject({ found: false, enabled: false })
  expect(d.agents.agy?.models).toEqual(['gemini-3.8-flash-high', 'gemini-3.8-flash-low'])
  expect(d.codexbarVersion).toBe('0.73.0')
  expect(d.providersOn).toEqual(['codex', 'claude', 'antigravity'])
  expect(d.providersAll).toEqual(['codex', 'claude', 'zai', 'antigravity'])
  expect(d.gates.test).toBe('pnpm exec vitest run {paths}')
  expect(d.gates.install).toBe('pnpm install --frozen-lockfile --prefer-offline')
  expect(d.gitignore).toBe(true)
  expect(d.qaFrontend).toBe(true)
  expect(d.existingYaml).toBe(false)
  // detection is read-only: no test suite, no write
  expect(w.argv.some(a => a.join(' ').includes('vitest'))).toBe(false)
  expect(Object.keys(files).some(p => p.includes('.matrixkoder'))).toBe(false)
  const init = await mk($, 'init feature-x')
  expect(init.text).toContain('setup opened')
})

test('detection proposes plugin/selftest/tsconfig gates for a repo shaped like this one', async ($, on) => {
  const files: Files = {
    [`${PROJECT_DIR}/.claude-plugin/plugin.json`]: '{}',
    [`${PROJECT_DIR}/scripts/selftest.sh`]: '#!/usr/bin/env bash\n',
    [`${PROJECT_DIR}/tsconfig.json`]: '{}',
  }
  world(on, files)
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const d = draftOf()
  expect(d.gates).toEqual({
    install: '', test: 'claude plugin test . && bash scripts/selftest.sh', typecheck: 'tsc -p . --noEmit',
    lint_changed: 'claude plugin validate .', format_changed: '',
  })
  expect(d.gatesFrom).toBe('.claude-plugin/plugin.json + scripts/selftest.sh + tsconfig.json')
})

test('the pane walks 10 steps forward and back on both surfaces', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const expectBody: [number, { text: RegExp } | { key: string }][] = [
    [1, { text: /Install mode/ }], [2, { key: 'wz-datamode' }], [3, { text: /Agents \(tick to enable\)/ }], [4, { text: /Providers \(tick to track\)/ }],
    [5, { text: /Proposed from package\.json/ }], [6, { key: 'wz-builderOrder' }], [7, { key: 'wz-preauthorised' }], [8, { key: 'wz-qaFrontend' }],
    [9, { key: 'wz-chip' }], [10, { text: /Will write/ }],
  ]
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'matrixkoder', surface, component: 'Pane', requestId: WIZARD, props: paneProps(64) })
    expect(await ui.find({ key: 'wz-back' }), `${surface} no back on step 1`).toBeUndefined()
    for (const [n, query] of expectBody) {
      expect(await ui.find({ text: new RegExp(`Step ${n}/10`) }), `${surface} step ${n} header`).toBeTruthy()
      expect(await ui.find(query), `${surface} step ${n} body`).toBeTruthy()
      if (n < 10) await ui.press({ key: 'wz-next' })
    }
    expect(await ui.find({ key: 'wz-next' }), `${surface} no next on the last step`).toBeUndefined()
    expect(stepOf()).toBe(10)
    await ui.press({ key: 'wz-back' })
    expect(stepOf()).toBe(9)
    for (let i = 0; i < 8; i++) await ui.press({ key: 'wz-back' })
    expect(stepOf(), `${surface} back to 1`).toBe(1)
    await ui.unmount()
  }
})

test('agent and provider toggles, and the z.ai key is a command, never a prompt', async ($, on) => {
  const w = world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'setup')
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'matrixkoder', surface, component: 'Pane', requestId: WIZARD, props: paneProps(64) })
    await ui.press({ key: 'wz-next' })
    await ui.press({ key: 'wz-next' }) // step 3
    expect(await ui.find({ text: /\[ \] opencode {2}not found/ })).toBeTruthy()
    expect(await ui.find({ text: /\[x\] codex/ })).toBeTruthy()
    await ui.press({ key: 'wz-agent-codex' })
    expect((draftOf()).agents.codex?.enabled).toBe(false)
    expect(await ui.find({ text: /\[ \] codex/ })).toBeTruthy()
    await ui.press({ key: 'wz-agent-codex' })
    await ui.press({ key: 'wz-next' }) // step 4
    await ui.press({ key: 'wz-prov-zai' })
    expect((draftOf()).providersOn).toContain('zai')
    expect(await ui.find({ text: /never asks for or stores API keys/ })).toBeTruthy()
    expect(await ui.find({ text: /set-api-key --provider zai --stdin/ })).toBeTruthy()
    await ui.press({ key: 'wz-prov-zai' })
    await ui.unmount()
    await mk($, 'setup') // fresh draft for the next surface
  }
  expect(w.argv.some(a => a.includes('set-api-key'))).toBe(false)
})

test('opencode on the Z.AI plan: plan models merge in, zai is labelled and pre-selected', async ($, on) => {
  const bareModels = Array.from({ length: 16 }, (_, i) => `opencode/model-${i + 1}\tModel ${i + 1}`).join('\n')
  const procs: Record<string, string | null> = {
    'which claude codex opencode agy herdr': '/bin/claude\n/bin/codex\n/bin/opencode\n/bin/agy\n/bin/herdr\n',
    'opencode models': `Fetching…\n${bareModels}\n`,
    'opencode models zai-coding-plan': 'zai-coding-plan/glm-4.7\tGLM 4.7\nzai-coding-plan/glm-5.3\tGLM 5.3\n',
    'codexbar config providers': 'codex: enabled default (Codex)\nclaude: enabled (Claude)\nzai: disabled (z.ai / GLM)\nopencode: disabled (OpenCode Zen)\nantigravity: enabled (Antigravity)\n',
  }
  world(on, repoFiles(), procs)
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const d = draftOf()
  expect(d.agents.opencode).toMatchObject({ found: true, enabled: true })
  expect(d.agents.opencode?.models).toContain('zai-coding-plan/glm-4.7')
  expect(d.agents.opencode?.models).toContain('zai-coding-plan/glm-5.3')
  expect(d.agents.opencode?.models[0]).toBe('zai-coding-plan/glm-4.7')
  expect(d.agents.opencode?.models[1]).toBe('zai-coding-plan/glm-5.3')
  expect(d.agents.opencode?.models.length).toBe(12)
  expect(d.providersOn).toContain('zai') // pre-selected even though CodexBar lists zai disabled
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(72) })
  for (let i = 0; i < 3; i++) await ui.press({ key: 'wz-next' })
  expect(await ui.find({ text: /\[x\] zai \(OpenCode Z\.AI plan\)/ })).toBeTruthy()
  expect(await ui.find({ text: /\[ \] opencode \(OpenCode Zen\)/ })).toBeTruthy()
  // the stored value stays the CodexBar id
  await ui.press({ key: 'wz-prov-zai' })
  expect((draftOf()).providersOn).not.toContain('zai')
  await ui.press({ key: 'wz-prov-zai' })
  expect((draftOf()).providersOn).toContain('zai')
  await ui.unmount()
})

test('codexbar missing shows the install hint and still lets the user continue', async ($, on) => {
  world(on, repoFiles(), { 'codexbar --version': null, 'codexbar config providers': null })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  for (let i = 0; i < 3; i++) await ui.press({ key: 'wz-next' })
  expect(await ui.find({ text: /codexbar not found on PATH/ })).toBeTruthy()
  expect(await ui.find({ text: /Install CodexBar/ })).toBeTruthy()
  expect(await ui.find({ key: 'wz-next' })).toBeTruthy()
  expect((draftOf()).providersOn).toEqual(['claude', 'codex', 'antigravity'])
})

// ---------- review: the writes ----------

type Walker = { find(q: { key: string }): Promise<unknown>; press(a: { key: string }): Promise<unknown> }

async function toReview(ui: Walker): Promise<void> {
  while (await ui.find({ key: 'wz-next' })) await ui.press({ key: 'wz-next' })
}

test('review writes yaml, config.json, projects.json, .gitignore and the dirs; edits land in the yaml', async ($, on) => {
  const files = repoFiles()
  const w = world(on, files)
  await $.session.start(sessionStart())
  for (const surface of SURFACES) {
    for (const k of Object.keys(files).filter(p => p.includes('.matrixkoder'))) delete files[k]
    files[`${PROJECT_DIR}/.gitignore`] = 'node_modules/\n'
    await mk($, 'setup')
    const ui = await $.ui.mount({ plugin: 'matrixkoder', surface, component: 'Pane', requestId: WIZARD, props: paneProps(64) })
    // edits: a gate (step 5), max lanes (step 6), a pref (step 9)
    for (let i = 0; i < 4; i++) await ui.press({ key: 'wz-next' })
    await ui.input({ key: 'wz-gate-test', text: 'pnpm exec vitest run {paths} --bail' })
    await ui.press({ key: 'wz-next' })
    await ui.select({ key: 'wz-maxLanes', value: '2' })
    for (let i = 0; i < 3; i++) await ui.press({ key: 'wz-next' })
    await ui.select({ key: 'wz-toasts', value: 'no' })
    await ui.press({ key: 'wz-next' })
    expect(await ui.find({ text: /Will write/ })).toBeTruthy()
    expect(files[YAML], 'nothing is written before the confirm').toBeUndefined()
    expect(await ui.find({ text: /matrixkoder\.yaml preview/ })).toBeTruthy()
    await ui.press({ key: 'wz-write' })

    const yaml = files[YAML] ?? ''
    expect(yaml, surface).toContain('data_dir: .matrixkoder/')
    expect(yaml).toContain('  integration_branch: main')
    expect(yaml).toContain('  install: pnpm install --frozen-lockfile --prefer-offline   # proposed')
    expect(yaml).toContain('  test: "pnpm exec vitest run {paths} --bail"   # proposed')
    expect(yaml).toContain('  lint_changed: "pnpm exec eslint {files}"   # proposed')
    expect(yaml).toContain('  type_errors: null')
    expect(yaml).toContain('banned_commands: [pnpm run dev]')
    expect(yaml).toContain('  codex: { available: true, models: [] }')
    expect(yaml).toContain('  opencode: { available: false, models: [] }')
    expect(yaml).toContain('  agy: { available: true, models: [gemini-3.8-flash-high, gemini-3.8-flash-low] }')
    expect(yaml).toContain('  claude: { available: true }')
    expect(yaml).toContain(`  checked_at: "${ISO_NOW}"`)
    expect(yaml).toContain('  builder_order: [codex, agy, claude]')
    expect(yaml).toContain('  max_build_lanes: 2')
    expect(yaml).toContain('    claude: { five_hour_left_pct: 40, weekly_left_pct: 30 }')
    expect(yaml).toContain('  builder_skip_permissions: true')
    expect(yaml).toContain('  providers: [claude, codex, antigravity]'.replace('claude, codex', 'codex, claude'))
    expect(yaml).toContain('  toasts: false')
    expect(yaml).toContain('  chip: true')
    expect(yaml).toContain('  integration_target: local-main')

    const config = JSON.parse(files[CONFIG] ?? 'null')
    expect(config).toMatchObject({ schema: 1, dashboard: { auto_open: true, chip: true, toasts: false }, usage: { poll_min: 5 }, updated: ISO_NOW })
    const projects = JSON.parse(files[PROJECTS] ?? 'null')
    expect(projects).toEqual({ [PROJECT_DIR]: { data_dir: DATA_DIR, onboarded_at: ISO_NOW } })
    expect(files[`${PROJECT_DIR}/.gitignore`]).toBe('node_modules/\n/.matrixkoder/\n')
    expect(w.argv.some(a => a[0] === 'mkdir' && a.includes(`${DATA_DIR}/plans`) && a.includes(`${DATA_DIR}/runs`))).toBe(true)
    expect(await ui.find({ text: /MatrixKoder is set up/ })).toBeTruthy()
    await ui.press({ key: 'wz-close' })
    expect(w.closed).toContain(WIZARD)
    await ui.unmount()
  }
})

test('projects.json and config.json are merged, not replaced; corrupt ones abort the write', async ($, on) => {
  const files = repoFiles()
  files[PROJECTS] = JSON.stringify({ '/other/repo': { data_dir: '/other/repo/.matrixkoder', onboarded_at: 'x' } })
  files[CONFIG] = JSON.stringify({ custom: 1, dashboard: { keep: true } })
  world(on, files)
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect(Object.keys(JSON.parse(files[PROJECTS] ?? '{}')).sort()).toEqual(['/other/repo', PROJECT_DIR])
  expect(JSON.parse(files[CONFIG] ?? '{}')).toMatchObject({ custom: 1, dashboard: { keep: true, toasts: true } })

  // corrupt projects.json: error shown, projects.json left alone, no "set up" claim
  files[PROJECTS] = '{not json'
  await mk($, 'setup')
  await toReview(ui)
  await ui.select({ key: 'wz-replace', value: 'replace' })
  await ui.press({ key: 'wz-write' })
  expect(files[PROJECTS]).toBe('{not json')
  expect(await ui.find({ text: /projects\.json is not valid JSON/ })).toBeTruthy()
  expect(await ui.find({ text: /MatrixKoder is set up/ })).toBeUndefined()
})

test('an existing matrixkoder.yaml is kept by default and replaced only after the confirm', async ($, on) => {
  const files = repoFiles()
  files[YAML] = '# mine\ndata_dir: .matrixkoder/\n'
  world(on, files)
  await $.session.start(sessionStart())
  for (const surface of SURFACES) {
    files[YAML] = '# mine\ndata_dir: .matrixkoder/\n'
    delete files[PROJECTS]
    await mk($, 'setup')
    expect((draftOf()).existingYaml).toBe(true)
    const ui = await $.ui.mount({ plugin: 'matrixkoder', surface, component: 'Pane', requestId: WIZARD, props: paneProps(64) })
    await toReview(ui)
    expect(await ui.find({ text: /keep +.*matrixkoder\.yaml/ }), `${surface} default is keep`).toBeTruthy()
    await ui.press({ key: 'wz-write' })
    expect(files[YAML], `${surface} kept`).toBe('# mine\ndata_dir: .matrixkoder/\n')
    expect(files[PROJECTS], 'the global files are still written').toBeDefined()
    expect(await ui.find({ text: /kept existing matrixkoder\.yaml/ })).toBeTruthy()
    await ui.unmount()

    await mk($, 'setup')
    const ui2 = await $.ui.mount({ plugin: 'matrixkoder', surface, component: 'Pane', requestId: WIZARD, props: paneProps(64) })
    await toReview(ui2)
    await ui2.select({ key: 'wz-replace', value: 'replace' })
    expect(await ui2.find({ text: /replace +.*matrixkoder\.yaml/ })).toBeTruthy()
    await ui2.press({ key: 'wz-write' })
    expect(files[YAML], `${surface} replaced`).toContain('integration_branch: main')
    expect(files[YAML]).not.toContain('# mine')
    await ui2.unmount()
  }
})

test('data location home: yaml goes under ~/.matrixkoder/projects/<slug>/ and no .gitignore edit', async ($, on) => {
  const files = repoFiles()
  world(on, files)
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await ui.press({ key: 'wz-next' }) // step 2
  await ui.select({ key: 'wz-datamode', value: 'home' })
  expect(await ui.find({ key: 'wz-gitignore' })).toBeUndefined()
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  const dir = `${HOME}/.matrixkoder/projects/mk-proj`
  expect(files[`${dir}/matrixkoder.yaml`]).toContain(`data_dir: ${dir}/`)
  expect(files[`${PROJECT_DIR}/.gitignore`]).toBe('node_modules/\n')
  expect(JSON.parse(files[PROJECTS] ?? '{}')[PROJECT_DIR]).toEqual({ data_dir: dir, onboarded_at: ISO_NOW })
})

test('no API key is read, asked for, run or written', async ($, on) => {
  const SECRET = 'sk-secret-abc123'
  const files = repoFiles()
  const w = world(on, files, {}, { Z_AI_API_KEY: SECRET, CUSTOM_API_KEY: SECRET, OPENAI_API_KEY: SECRET })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(80) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  const written = Object.entries(files).filter(([p]) => p.includes('.matrixkoder')).map(([, text]) => text).join('\n')
  expect(written).toContain('integration_branch')
  expect(written).not.toContain(SECRET)
  expect(written).not.toMatch(/api[_-]?key/i)
  expect(w.argv.some(a => a.includes('set-api-key') || a.some(x => x.includes(SECRET)))).toBe(false)
  expect(JSON.stringify(draftOf())).not.toContain(SECRET)
})

// ---------- inline fallback ----------

test('outside fullscreen /mk setup answers with text and tells the user to run /tui fullscreen', async ($, on) => {
  const files = repoFiles()
  const w = world(on, files)
  await $.session.start(sessionStart())
  const ran = await mk($, 'setup', false)
  expect(ran.text).toContain('/tui fullscreen')
  expect(ran.text).toContain('/mk setup')
  expect(ran.text).toContain('agents: claude, codex, agy, herdr (missing: opencode)')
  expect(ran.text).toContain('codexbar 0.73.0')
  expect(ran.text).toContain('test: pnpm exec vitest run {paths}')
  expect(ran.text).toContain('Nothing was written.')
  expect(w.opened).not.toContain(WIZARD)
  expect(Object.keys(files).some(p => p.includes('.matrixkoder'))).toBe(false)
})

test('an inline-placed wizard pane carries the fullscreen hint', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'setup')
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'matrixkoder', surface, component: 'Pane', requestId: WIZARD, props: paneProps(64, 'inline') })
    expect(await ui.find({ text: /\/tui fullscreen/ }), surface).toBeTruthy()
    await ui.redraw(paneProps(64, 'dock'))
    expect(await ui.find({ text: /\/tui fullscreen/ }), surface).toBeUndefined()
    await ui.unmount()
  }
})

// ---------- keyboard: hotkeys and initial focus (live test: Enter/Tab never reached Next) ----------

test('hotkeys: unique and drawn on steps without text Inputs; none where an Input could lose typed letters', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const FIXED: Record<string, string> = { 'wz-next': 'n', 'wz-back': 'b', 'wz-cancel': 'c', 'wz-write': 'w', 'wz-close': 'x' }
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'matrixkoder', surface, component: 'Pane', requestId: WIZARD, props: paneProps(64) })
    for (let step = 1; step <= 10; step++) {
      const buttons = await ui.findAll({ type: 'Button' })
      const hasInput = (await ui.findAll({ type: 'Input' })).length > 0
      expect(hasInput, `${surface} step ${step} Input steps are exactly 5-9`).toBe(step >= 5 && step <= 9)
      const seen = new Set<string>()
      for (const b of buttons) {
        const hotkey = b.props.hotkey
        if (hasInput) {
          expect(hotkey, `${surface} step ${step} ${b.key}: a hotkey beside an Input could swallow typing`).toBeUndefined()
          continue
        }
        expect(typeof hotkey, `${surface} step ${step} ${b.key} has a hotkey`).toBe('string')
        expect(hotkey as string).toMatch(/^[a-z0-9]$/)
        expect(seen.has(hotkey as string), `${surface} step ${step} hotkey ${String(hotkey)} unique`).toBe(false)
        seen.add(hotkey as string)
        expect(b.text, `${surface} ${b.key} label shows its hotkey`).toContain(`${String(hotkey)} `)
        if (b.key != null && FIXED[b.key] != null) expect(hotkey).toBe(FIXED[b.key])
      }
      // initial focus: Next (Enter) on every step but the last; Write never takes it, so held Enter cannot confirm.
      // One exception (repoFiles here has detection, so it does not apply): step 5 with nothing detected
      // hands the initial focus to the first gate Input — covered by the test below.
      const focused = buttons.filter(b => b.props.autoFocus === true).map(b => b.key)
      expect(focused, `${surface} step ${step} initial focus`).toEqual(step < 10 ? ['wz-next'] : [])
      if (step < 10) {
        expect(await ui.find({ text: hasInput ? 'Next →' : 'n Next →' })).toBeTruthy()
        await ui.press({ key: 'wz-next' })
      }
    }
    expect(await ui.find({ text: 'w Write these' })).toBeTruthy()
    await ui.press({ key: 'wz-write' })
    const close = await ui.find({ key: 'wz-close' })
    expect(close?.props.hotkey).toBe('x')
    expect(close?.props.autoFocus).toBe(true)
    await ui.unmount()
    await mk($, 'setup')
  }
})

test('gates step with nothing detected: the first gate Input takes the focus, not Next; detected keeps Next', async ($, on) => {
  const files: Files = {}
  world(on, files)
  await $.session.start(sessionStart())
  await mk($, 'setup')
  expect((draftOf()).gatesFrom).toBe('')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  for (let i = 0; i < 4; i++) await ui.press({ key: 'wz-next' })
  expect(await ui.find({ text: /Nothing detected — Tab to a field and type the command you use\./ })).toBeTruthy()
  const inputs = await ui.findAll({ type: 'Input' })
  expect(inputs.map(i => i.key)).toEqual(['wz-gate-install', 'wz-gate-test', 'wz-gate-typecheck', 'wz-gate-lint_changed', 'wz-gate-format_changed'])
  expect(inputs[0]?.props.autoFocus, 'the first gate Input holds the initial focus').toBe(true)
  for (const i of inputs.slice(1)) expect(i.props.autoFocus).toBeUndefined()
  const focusedButtons = (await ui.findAll({ type: 'Button' })).filter(b => b.props.autoFocus === true)
  expect(focusedButtons, 'Next is not autoFocused when nothing was detected').toEqual([])
  await ui.input({ key: 'wz-gate-install', text: 'npm i' })
  expect((draftOf()).gates.install).toBe('npm i')
  await ui.unmount()
})

// ---------- r4: safe writes ----------

const GITIGNORE = `${PROJECT_DIR}/.gitignore`

test('an existing but unreadable .gitignore aborts the write and is never overwritten', async ($, on) => {
  const files = repoFiles()
  delete files[GITIGNORE] // exists (see world's `unreadable`) but cannot be read
  const w = world(on, files, {}, {}, [GITIGNORE])
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect(await ui.find({ text: /cannot read .*\.gitignore.*not overwriting/ })).toBeTruthy()
  expect(await ui.find({ text: /MatrixKoder is set up/ })).toBeUndefined()
  expect(files[GITIGNORE], 'the unreadable file was not replaced').toBeUndefined()
  expect(Object.keys(files).filter(p => p.includes('.matrixkoder') || p.endsWith('.tmp')), 'nothing at all was written').toEqual([])
  expect(w.argv.some(a => a[0] === 'mv' || a[0] === 'mkdir')).toBe(false)
})

for (const victim of ['config.json', 'projects.json'] as const) {
  test(`an unreadable existing ${victim} aborts before any write`, async ($, on) => {
    const files = repoFiles()
    world(on, files, {}, {}, [`${HOME}/.matrixkoder/${victim}`])
    await $.session.start(sessionStart())
    await mk($, 'setup')
    const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
    await toReview(ui)
    await ui.press({ key: 'wz-write' })
    expect(await ui.find({ text: new RegExp(`cannot read .*${victim.replace('.', '\\.')}`) })).toBeTruthy()
    expect(Object.keys(files).filter(p => p.includes('.matrixkoder') || p.endsWith('.tmp'))).toEqual([])
    expect(files[GITIGNORE]).toBe('node_modules/\n')
  })
}

test('a non-object config.json ([]) aborts instead of being replaced', async ($, on) => {
  const files = repoFiles()
  files[CONFIG] = '[]'
  world(on, files)
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect(files[CONFIG]).toBe('[]')
  expect(await ui.find({ text: /not a JSON object/ })).toBeTruthy()
})

test('a matrixkoder.yaml that appeared after detection goes back to the replace confirm and is not overwritten', async ($, on) => {
  const files = repoFiles()
  const w = world(on, files)
  await $.session.start(sessionStart())
  await mk($, 'setup')
  expect(draftOf().existingYaml).toBe(false)
  files[YAML] = '# created by someone else after detection\n'
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect(files[YAML]).toBe('# created by someone else after detection\n')
  expect(await ui.find({ text: /appeared since detection/ })).toBeTruthy()
  expect(await ui.find({ key: 'wz-replace' }), 'the replace confirm is now offered').toBeTruthy()
  expect(files[PROJECTS], 'nothing else was written').toBeUndefined()
  expect(w.argv.some(a => a[0] === 'mv')).toBe(false)
  // keep (default) then write again: the yaml stays, the rest is written
  await ui.press({ key: 'wz-write' })
  expect(files[YAML]).toBe('# created by someone else after detection\n')
  expect(files[PROJECTS]).toBeDefined()
  expect(await ui.find({ text: /MatrixKoder is set up/ })).toBeTruthy()
})

test('writes go through a .tmp file and mv; replacing a yaml keeps a .bak', async ($, on) => {
  const files = repoFiles()
  files[YAML] = '# old\n'
  const w = world(on, files)
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  await ui.select({ key: 'wz-replace', value: 'replace' })
  await ui.press({ key: 'wz-write' })
  const moves = w.argv.filter(a => a[0] === 'mv').map(a => a[a.length - 1])
  expect(moves).toEqual([YAML, GITIGNORE, CONFIG, PROJECTS])
  for (const a of w.argv.filter(x => x[0] === 'mv')) expect(a[a.length - 2]).toBe(`${a[a.length - 1]}.tmp`)
  expect(Object.keys(files).filter(p => p.endsWith('.tmp')), 'no temp file is left').toEqual([])
  expect(files[`${YAML}.bak`]).toBe('# old\n')
  expect(files[YAML]).toContain('integration_branch')
})

test('$MATRIXKODER_DATA and $MATRIXKODER_HOME are honoured by the wizard', async ($, on) => {
  const files = repoFiles()
  world(on, files, {}, { MATRIXKODER_DATA: '/srv/mk-data', MATRIXKODER_HOME: '/srv/mk-home' })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await ui.press({ key: 'wz-next' })
  expect(await ui.find({ text: /MATRIXKODER_DATA is set: data goes to \/srv\/mk-data/ })).toBeTruthy()
  expect(await ui.find({ key: 'wz-datamode' })).toBeUndefined()
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect(files['/srv/mk-data/matrixkoder.yaml']).toContain('data_dir: /srv/mk-data/')
  expect(JSON.parse(files['/srv/mk-home/projects.json'] ?? 'null')).toEqual({ [PROJECT_DIR]: { data_dir: '/srv/mk-data', onboarded_at: ISO_NOW } })
  expect(files['/srv/mk-home/config.json']).toBeDefined()
  expect(files[`${HOME}/.matrixkoder/projects.json`]).toBeUndefined()
  expect(files[GITIGNORE], 'env data dir lives outside the repo: no .gitignore edit').toBe('node_modules/\n')
})

test('yaml quoting: numberish, date-ish and NEL values are quoted', () => {
  for (const v of ['0x1F', '1_000', '2026-10-08', '.inf', '.nan', '-1.5', '12', '\u0085x']) {
    expect(inline(v).startsWith('"'), v).toBe(true)
  }
  expect(inline('\u0085x')).toBe('"\\u0085x"')
  expect(inline('.matrixkoder/')).toBe('.matrixkoder/')
  expect(inline('../x')).toBe('../x')
})
