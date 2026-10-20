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
import { resetStoreMemo } from './paths'

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

/** The same canned probes as PROCS, but with the repo at `repo` (a slug test drives a foreign folder). */
function ProcsFor(repo: string): Record<string, string | null> {
  return {
    ...PROCS,
    'git rev-parse --path-format=absolute --git-common-dir': `${repo}/.git\n`,
  }
}

type World = { argv: string[][]; opened: string[]; closed: string[]; state: Map<string, { value: unknown; version: number }> }

/** clock, env, process (argv → canned stdout, unknown → exit 1), fs and ui.open/close. `procs` overrides PROCS; `process` can override the result. */
function world(
  on: On,
  files: Files,
  procs: Record<string, string | null> = {},
  env: Record<string, string> = {},
  unreadable: readonly string[] = [],
  process?: (argv: readonly string[]) => void | { value: { exitCode: number; stdout: string; stderr: string; isStdoutTruncated: boolean; isStderrTruncated: boolean } } | Promise<void | { value: { exitCode: number; stdout: string; stderr: string; isStdoutTruncated: boolean; isStderrTruncated: boolean } }>,
): World {
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
  on('process.run', async (_$, e) => {
    w.argv.push([...e.argv])
    const override = await process?.(e.argv)
    if (override) return override
    // mv / cp act on the in-memory files, as the real commands would on disk
    if (e.argv[0] === 'mv' || e.argv[0] === 'cp') {
      const [src, dst] = [e.argv[e.argv.length - 2] ?? '', e.argv[e.argv.length - 1] ?? '']
      if (src in files) {
        files[dst] = files[src] ?? ''
        if (e.argv[0] === 'mv') delete files[src]
      }
      return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    // L10.7: `mk-state.py project register` (the root-table registration) succeeds unless a test overrides it
    if (e.argv[1]?.endsWith('/scripts/mk-state.py') && e.argv[2] === 'project' && e.argv[3] === 'register') {
      return { value: { exitCode: 0, stdout: '{"ok": true}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    // B-3: the project store is created by `mk-state.py sync` (real reply, captured from the script);
    // it writes <data>/mk-project.db, which the fake fs cannot show, so the reply is the witness
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
  const withOllama = mergeModels(['opencode/a'], ['ollama-cloud/x'], ['zai-coding-plan/p1'])
  expect(withOllama).toEqual(['zai-coding-plan/p1', 'ollama-cloud/x', 'opencode/a'])
})

test('gate proposals: pnpm+vitest, python, Makefile; never invents', () => {
  const js = proposeGates({ pkg: JSON.parse(PKG), pyproject: null, makefile: null, locks: ['pnpm-lock.yaml'], hasTsconfig: true, markers: [] })
  expect(js.gates).toEqual({
    install: 'pnpm install --frozen-lockfile --prefer-offline',
    test: 'pnpm exec vitest run',
    typecheck: 'pnpm run typecheck',
    lint_changed: 'pnpm exec eslint .',
    format_changed: 'pnpm exec prettier --write .',
  })
  expect(js.banned).toEqual(['pnpm run dev'])
  const py = proposeGates({ pkg: null, pyproject: '[tool.pytest]\n[tool.ruff]\n[tool.mypy]', makefile: null, locks: ['uv.lock'], hasTsconfig: false, markers: [] })
  expect(py.gates).toEqual({ install: 'uv sync', test: 'pytest', typecheck: 'mypy .', lint_changed: 'ruff check .', format_changed: 'ruff format .' })
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
  expect(pkgCargo.gates.test).toBe('npx vitest run')
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
  expect(pkgSelftest.gates.test).toBe('npx vitest run')
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

test('repo: emits the lanes block (reusable worktree slots), not worktree_pattern', () => {
  const yaml = buildYaml(emptyDraft(), ISO_NOW)
  expect(yaml).toContain('  lanes: { slots: 1, pattern: "../{repo}-mk-{n}", reuse: true }   # reusable worktree slots; the cap is max_build_lanes')
  expect(yaml).not.toContain('worktree_pattern')
  expect(yaml).not.toContain('-worktree-')
  expect(yaml).toContain('  integration_branch: main   # lanes merge here; one PR to main at the end\n' + '  lanes: { slots: 1,')
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
  expect(d.agents.herdr).toMatchObject({ found: true, enabled: true })
  expect(d.herdrPlugin).toBe('link')
  expect(d.agents.agy?.models).toEqual(['gemini-3.8-flash-high', 'gemini-3.8-flash-low'])
  expect(d.codexbarVersion).toBe('0.73.0')
  expect(d.providersOn).toEqual(['codex', 'claude', 'antigravity'])
  expect(d.providersAll).toEqual(['codex', 'claude', 'zai', 'antigravity'])
  expect(d.gates.test).toBe('pnpm exec vitest run')
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

test('detection picks home for a bare repo', async ($, on) => {
  world(on, {})
  await $.session.start(sessionStart())
  await mk($, 'setup')
  expect(draftOf().dataMode).toBe('home')
})

test('detection keeps repo mode when package.json exists', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'setup')
  expect(draftOf().dataMode).toBe('repo')
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
      expect(await ui.find({ text: new RegExp(`STEP ${n}/10`) }), `${surface} step ${n} header`).toBeTruthy()
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
  expect(await ui.find({ text: /\[x\] Z\.AI/ })).toBeTruthy()
  expect(await ui.find({ text: /\[ \] OpenCode Zen/ })).toBeTruthy()
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

test('review writes yaml, registers the project (no projects.json), .gitignore and the dirs; edits land in the yaml', async ($, on) => {
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
    expect(await ui.find({ text: /matrixkoder\.yaml · new file/ })).toBeTruthy()
    await ui.press({ key: 'wz-write' })

    const yaml = files[YAML] ?? ''
    expect(yaml, surface).toContain('data_dir: .matrixkoder/')
    expect(yaml).toContain('  integration_branch: main')
    expect(yaml).toContain('  install: pnpm install --frozen-lockfile --prefer-offline   # proposed')
    expect(yaml).toContain('  test: "pnpm exec vitest run {paths} --bail"   # proposed')
    expect(yaml).toContain('  lint_changed: pnpm exec eslint .   # proposed')
    expect(yaml).toContain('  type_errors: null')
    expect(yaml).toContain('banned_commands: [pnpm run dev]')
    expect(yaml).toContain('  codex: { available: true, models: [] }')
    expect(yaml).toContain('  opencode: { available: false, models: [] }')
    expect(yaml).toContain('  agy: { available: true, models: [gemini-3.8-flash-high, gemini-3.8-flash-low] }')
    expect(yaml).toContain('  claude: { available: true }')
    expect(yaml).toContain(`  checked_at: "${ISO_NOW}"`)
    expect(yaml).toContain('  builder_order: [codex, agy, claude]')
    expect(yaml).toContain('  max_build_lanes: 2')
    expect(yaml).toContain('  lanes: { slots: 1, pattern: "../{repo}-mk-{n}", reuse: true }')
    expect(yaml).not.toContain('worktree_pattern')
    expect(yaml).toContain('    claude: { five_hour_left_pct: 40, weekly_left_pct: 30 }')
    expect(yaml).toContain('  builder_skip_permissions: true')
    expect(yaml).toContain('  providers: [codex, claude, antigravity]')
    expect(yaml).toContain('  integration_target: local-main')
    expect(yaml).toContain('  reviewer: claude sonnet medium')
    expect(yaml).toContain('  visual_reviewer: claude haiku')
    expect(yaml).toContain('session:')
    expect(yaml).toContain('  fresh_session: new')
    expect(yaml).toContain('  subagents: true')
    expect(yaml).toContain('  stall_min: 15')
    expect(yaml).toContain('  max_wait_min: 30')
    expect(yaml).toContain('  reviewer_split: right')
    expect(yaml).toContain('  reuse_reviewer: true')
    expect(yaml).toContain('picks:')
    expect(yaml).toContain('  claude: []')
    expect(yaml).toContain('  codex: []')
    expect(yaml).toContain('  agy: []')
    expect(yaml).toContain('  herdr: []')
    expect(yaml, 'a missed agent gets no picks line').not.toContain('  opencode: []')
    expect(yaml, 'the dead dashboard copy is gone').not.toContain('dashboard:')
    expect(yaml, 'the dead poll_min copy is gone').not.toContain('poll_min:')

    expect(files[PROJECTS], 'projects.json is no longer written').toBeUndefined()
    const reg = w.argv.filter(a => a[2] === 'project' && a[3] === 'register').pop()
    expect(reg?.slice(4), surface).toEqual(['--slug', 'mk-proj', '--repo', PROJECT_DIR, '--data', DATA_DIR])
    expect(Object.keys(files).some(p => p.endsWith('/config.json')), 'no config.json anywhere in the fake home').toBe(false)
    expect(files[`${PROJECT_DIR}/.gitignore`]).toBe('node_modules/\n/.matrixkoder/\n')
    expect(w.argv.some(a => a[0] === 'mkdir' && a.includes(`${DATA_DIR}/plans`) && a.includes(`${DATA_DIR}/runs`))).toBe(true)
    expect(await ui.find({ text: /MatrixKoder is set up/ })).toBeTruthy()
    await ui.press({ key: 'wz-close' })
    expect(w.closed).toContain(WIZARD)
    await ui.unmount()
  }
})

test('a replace carries the old yaml\'s session, picks and reviewer values; unset keys keep defaults', async ($, on) => {
  const files = repoFiles()
  files[YAML] = [
    '# changed from the Settings tab since the last wizard run',
    'session:',
    '  stall_min: 22',
    'picks:',
    '  codex: [gpt-6.1, gpt-5.4]',
    'routing_preferences:',
    '  reviewer: codex gpt-6.1 high',
    '',
  ].join('\n')
  world(on, files)
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  await ui.select({ key: 'wz-replace', value: 'replace' })
  expect(await ui.find({ text: /replace +.*matrixkoder\.yaml/ })).toBeTruthy()
  await ui.press({ key: 'wz-write' })
  const yaml = files[YAML] ?? ''
  // carried: the three groups the old file set
  expect(yaml).toContain('  stall_min: 22')
  expect(yaml).toContain('  codex: [gpt-6.1, gpt-5.4]')
  expect(yaml).toContain('  reviewer: codex gpt-6.1 high')
  // never set by the old file: the schema defaults still land
  expect(yaml).toContain('  max_wait_min: 30')
  expect(yaml).toContain('  fresh_session: new')
  expect(yaml).toContain('  visual_reviewer: claude haiku')
  expect(yaml).toContain('  agy: []')
  // the wizard's own answers come from the draft, not the old file
  expect(yaml).toContain('  integration_branch: main')
  expect(yaml).not.toContain('# changed from the Settings tab')
  await ui.unmount()
})

test('a carried string key keeps its text: subagent_model "007" is not the number 7', async ($, on) => {
  const files = repoFiles()
  files[YAML] = ['session:', '  subagent_model: "007"', '  reuse_reviewer: false', ''].join('\n')
  world(on, files)
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  await ui.select({ key: 'wz-replace', value: 'replace' })
  await ui.press({ key: 'wz-write' })
  const yaml = files[YAML] ?? ''
  expect(yaml).toContain('  subagent_model: "007"   #')    // the text, quoted so yaml keeps it a string
  expect(yaml).not.toContain('  subagent_model: 7')
  expect(yaml).toContain('  reuse_reviewer: false')          // a real bool is still a bool
  await ui.unmount()
})

test('an existing projects.json is left alone (never read, never written); a failed registration aborts with the reason', async ($, on) => {
  const files = repoFiles()
  const old = JSON.stringify({ '/other/repo': { data_dir: '/other/repo/.matrixkoder', onboarded_at: 'x' } })
  files[PROJECTS] = old
  let fail = false
  const w = world(on, files, {}, {}, [], argv => {
    if (fail && argv[2] === 'project') return { value: { exitCode: 2, stdout: '{"ok": false, "error": "slug taken"}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect(files[PROJECTS]).toBe(old)
  expect(w.argv.some(a => a[2] === 'project' && a[3] === 'register')).toBe(true)
  expect(files[CONFIG], 'no config.json copy is written').toBeUndefined()

  // the registration fails: error shown with its reason, no "set up" claim
  fail = true
  await mk($, 'setup')
  await toReview(ui)
  await ui.select({ key: 'wz-replace', value: 'replace' })
  await ui.press({ key: 'wz-write' })
  expect(files[PROJECTS]).toBe(old)
  expect(await ui.find({ text: /could not register the project: .*slug taken/ })).toBeTruthy()
  expect(await ui.find({ text: /MatrixKoder is set up/ })).toBeUndefined()
})

test('an existing matrixkoder.yaml is kept by default and replaced only after the confirm', async ($, on) => {
  const files = repoFiles()
  files[YAML] = '# mine\ndata_dir: .matrixkoder/\n'
  const w = world(on, files)
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
    expect(w.argv.some(a => a[2] === 'project' && a[3] === 'register'), 'the project is still registered').toBe(true)
    expect(await ui.find({ text: /kept existing yaml \(unchanged\)/ })).toBeTruthy()
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
  const w = world(on, files)
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
  const reg = w.argv.filter(a => a[2] === 'project' && a[3] === 'register').pop()
  expect(reg?.slice(4)).toEqual(['--slug', 'mk-proj', '--repo', PROJECT_DIR, '--data', dir])
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
  expect(ran.text).toContain('test: pnpm exec vitest run')
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

test('a non-object config.json ([]) is left alone: it is no longer written or read', async ($, on) => {
  const files = repoFiles()
  files[CONFIG] = '[]'
  world(on, files)
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect(files[CONFIG]).toBe('[]')
  expect(await ui.find({ text: /✓ MatrixKoder is set up/ })).toBeTruthy()
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
  expect(w.argv.some(a => a[2] === 'project' && a[3] === 'register')).toBe(true)
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
  expect(moves).toEqual([YAML, GITIGNORE])
  for (const a of w.argv.filter(x => x[0] === 'mv')) expect(a[a.length - 2]).toBe(`${a[a.length - 1]}.tmp`)
  expect(Object.keys(files).filter(p => p.endsWith('.tmp')), 'no temp file is left').toEqual([])
  expect(files[`${YAML}.bak`]).toBe('# old\n')
  expect(files[YAML]).toContain('integration_branch')
})

test('$MATRIXKODER_DATA and $MATRIXKODER_HOME are honoured by the wizard', async ($, on) => {
  const files = repoFiles()
  const w = world(on, files, {}, { MATRIXKODER_DATA: '/srv/mk-data', MATRIXKODER_HOME: '/srv/mk-home' })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await ui.press({ key: 'wz-next' })
  expect(await ui.find({ text: /MATRIXKODER_DATA is set: data goes to \/srv\/mk-data/ })).toBeTruthy()
  expect(await ui.find({ key: 'wz-datamode' })).toBeUndefined()
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect(files['/srv/mk-data/matrixkoder.yaml']).toContain('data_dir: /srv/mk-data/')
  expect(files['/srv/mk-home/projects.json'], 'registered through mk-state, not a file').toBeUndefined()
  expect(w.argv.some(a => a[2] === 'project' && a[3] === 'register' && a.includes('/srv/mk-data'))).toBe(true)
  expect(files['/srv/mk-home/config.json'], 'no config.json anywhere, env home included').toBeUndefined()
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

// ---------- H2B: herdr plugin link and Ollama Cloud models ----------

test('wizard detects whether the MatrixKoder herdr plugin is linked', async ($, on) => {
  const files = repoFiles()
  world(on, files, { 'herdr plugin list': 'matrixkoder\n' })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  expect(draftOf().herdrPlugin).toBe('linked')
})

test('wizard links the herdr plugin on write when link, and only once', async ($, on) => {
  const files = repoFiles()
  const w = world(on, files, {}, {}, [], argv => {
    if (argv[0] === 'herdr' && argv[1] === 'plugin' && argv[2] === 'link') {
      return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
  })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  expect(draftOf().herdrPlugin).toBe('link')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  const links = w.argv.filter(a => a[0] === 'herdr' && a[1] === 'plugin' && a[2] === 'link')
  expect(links.length).toBe(1)
  expect(links[0]?.[3]).toBe(`${draftOf().pluginRoot}/herdr`)
  expect(await ui.find({ text: /✓ herdr plugin linked/ })).toBeTruthy()
})

test('wizard skips the herdr plugin link when skip or already linked', async ($, on) => {
  const files = repoFiles()
  const w = world(on, files, {}, {}, [], argv => {
    if (argv[0] === 'herdr' && argv[1] === 'plugin' && argv[2] === 'link') {
      return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
  })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  // navigate to step 3 and choose skip
  await ui.press({ key: 'wz-next' })
  await ui.press({ key: 'wz-next' })
  await ui.select({ key: 'wz-herdrPlugin', value: 'skip' })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect(w.argv.filter(a => a[0] === 'herdr' && a[1] === 'plugin' && a[2] === 'link').length).toBe(0)
})

test('wizard survives a failing herdr plugin link without failing the write', async ($, on) => {
  const files = repoFiles()
  const w = world(on, files, {}, {}, [], argv => {
    if (argv[0] === 'herdr' && argv[1] === 'plugin' && argv[2] === 'link') {
      return { value: { exitCode: 1, stdout: '', stderr: 'no herdr socket', isStdoutTruncated: false, isStderrTruncated: false } }
    }
  })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect(w.argv.filter(a => a[0] === 'herdr' && a[1] === 'plugin' && a[2] === 'link').length).toBe(1)
  expect(await ui.find({ text: /✓ MatrixKoder is set up/ })).toBeTruthy()
  expect(await ui.find({ text: /⚠ herdr plugin link failed/ })).toBeTruthy()
})

test('opencode on Ollama Cloud: models merge in, ollama is labelled and pre-selected', async ($, on) => {
  const bareModels = Array.from({ length: 16 }, (_, i) => `opencode/model-${i + 1}\tModel ${i + 1}`).join('\n')
  const procs: Record<string, string | null> = {
    'which claude codex opencode agy herdr': '/bin/claude\n/bin/codex\n/bin/opencode\n/bin/agy\n/bin/herdr\n',
    'opencode models': `Fetching…\n${bareModels}\n`,
    'opencode models ollama-cloud': 'ollama-cloud/llama-3.1\tLlama 3.1\nollama-cloud/qwen-2.5\tQwen 2.5\n',
    'codexbar config providers': 'codex: enabled default (Codex)\nclaude: enabled (Claude)\nzai: disabled (z.ai / GLM)\nopencode: disabled (OpenCode Zen)\nopencodego: disabled (OpenCode Go)\nollama: disabled (Ollama Cloud)\nantigravity: enabled (Antigravity)\n',
  }
  world(on, repoFiles(), procs)
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const d = draftOf()
  expect(d.agents.opencode).toMatchObject({ found: true, enabled: true })
  expect(d.agents.opencode?.models).toContain('ollama-cloud/llama-3.1')
  expect(d.agents.opencode?.models).toContain('ollama-cloud/qwen-2.5')
  expect(d.agents.opencode?.models[0]).toBe('ollama-cloud/llama-3.1')
  expect(d.providersOn).toContain('ollama')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(72) })
  for (let i = 0; i < 3; i++) await ui.press({ key: 'wz-next' })
  expect(await ui.find({ text: /\[x\] Ollama/ })).toBeTruthy()
  expect(await ui.find({ text: /\[ \] OpenCode Go/ })).toBeTruthy()
})

// ---------- H2B R2: linked-state Select, marketplace re-link note ----------

test('wizard does not render a herdr link Select and does not run herdr plugin link when the herdr plugin is already linked', async ($, on) => {
  const files = repoFiles()
  const w = world(on, files, { 'herdr plugin list': 'matrixkoder\n' })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  expect(draftOf().herdrPlugin).toBe('linked')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await ui.press({ key: 'wz-next' })
  await ui.press({ key: 'wz-next' })
  expect(await ui.find({ key: 'wz-herdrPlugin' })).toBeUndefined()
  expect(await ui.find({ text: /✓ MatrixKoder herdr plugin is linked/ })).toBeTruthy()
  await toReview(ui)
  expect(await ui.find({ text: /herdr plugin already linked/ })).toBeTruthy()
  await ui.press({ key: 'wz-write' })
  expect(w.argv.filter(a => a[0] === 'herdr' && a[1] === 'plugin' && a[2] === 'link').length).toBe(0)
  expect(await ui.find({ text: /✓ MatrixKoder is set up/ })).toBeTruthy()
})

test('wizard adds a marketplace re-link note to Review and the post-write view', async ($, on) => {
  const files = repoFiles()
  world(on, files, {}, { MATRIXKODER_INSTALL_MODE: 'marketplace' })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  expect((draftOf()).installMode).toBe('marketplace')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  expect(await ui.find({ text: /Re-run \/mk setup after each MatrixKoder update/ })).toBeTruthy()
  await ui.press({ key: 'wz-write' })
  expect(await ui.find({ text: /Re-run \/mk setup after each MatrixKoder update/ })).toBeTruthy()
})

// ---------- S1.3: dashboard prefs via $.config.set, config migration ----------

test('B-5: step 9 draws no Apply button; the four userConfig keys land with the step-10 write', async ($, on) => {
  const files = repoFiles()
  world(on, files)
  const sets: Array<{ key: string; value: unknown }> = []
  on('config.set', (_$, e) => {
    sets.push({ key: e.key, value: e.value })
    return { value: e.value }
  })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(46) })
  for (let i = 0; i < 8; i++) await ui.press({ key: 'wz-next' }) // step 9
  expect(await ui.find({ key: 'wz-dash-apply' }), 'no Apply button on step 9').toBeUndefined()
  await ui.select({ key: 'wz-toasts', value: 'no' })
  await ui.input({ key: 'wz-pollMin', text: '7' })
  expect(sets, 'rendering and editing never write').toEqual([])
  expect(files[YAML]).toBeUndefined()
  await toReview(ui)
  expect(sets, 'the review alone still writes nothing').toEqual([])
  await ui.press({ key: 'wz-write' })
  expect(sets).toEqual([
    { key: 'autoOpen', value: true },
    { key: 'chip', value: true },
    { key: 'toasts', value: false },
    { key: 'usagePollMin', value: 7 },
  ])
  expect(await ui.find({ text: /✗/ })).toBeUndefined()
  await ui.unmount()
})

test('B-5: a deny from $.config.set surfaces as the write error, and the files still land', async ($, on) => {
  const files = repoFiles()
  world(on, files)
  on('config.set', (_$, e) => (e.key === 'toasts' ? { deny: 'locked by policy' } : { value: e.value }))
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(46) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect(files[YAML], 'the yaml is written even when a pref is refused').toContain('integration_branch')
  // applyPrefs runs inside writeAll, so its refusal is the draft's error: the review keeps the write list up
  // and the post-write view's ⚠ line carries it — either way the text is on screen, never silent.
  expect(await ui.find({ text: /could not set toasts: locked by policy/ })).toBeTruthy()
  await ui.unmount()
})

// ---------- B-1/B-2/B-4: the migrated path, the slug, keep-versus-migrate ----------

/** `mkdb.py projects --json` as the real script answers it (captured), naming `dataDir`'s store. */
const projectsReply = (dataDir: string): string => JSON.stringify({
  ok: true,
  projects: [{
    slug: 'mk-proj', name: 'mk-proj', repo: PROJECT_DIR, data_dir: dataDir,
    mode: 'home', created: '2026-10-20T22:43:25+02:00', last_activity: '2026-10-20T22:43:25+02:00',
    db_path: `${dataDir}/mk-project.db`,
  }],
})

test('B-1: on a migrated project the review names and writes the C2 project.yaml, never matrixkoder.yaml', async ($, on) => {
  const files = repoFiles()
  // A REAL migrated row, captured from `mkdb.py projects --json`: db_path sits in the C2 project folder
  // `<home>/projects/<slug>`, NOT in the repo's data dir — so that folder is the data dir too.
  const c2 = `${HOME}/.matrixkoder/projects/mk-proj`
  const w = world(on, files, {}, {}, [], argv => {
    if (argv[1]?.endsWith('/scripts/mkdb.py') && argv[2] === 'projects') {
      return { value: { exitCode: 0, stdout: projectsReply(c2), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    return undefined
  })
  await $.session.start(sessionStart())
  // paths.ts memoises the root-store read for the rest of the process (a session-start reset does it in the
  // real pane); this test is the one that changes the answer, so it starts from a clean memo
  resetStoreMemo()
  await mk($, 'setup')
  expect(draftOf().migrated, 'the store row marks the project migrated').toBe(true)
  expect(draftOf().dataMode, 'a migrated project writes in its C2 folder').toBe('repo')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(46) })
  await toReview(ui)
  expect(await ui.find({ text: new RegExp(`${c2}/project\\.yaml`) }), 'the review names the C2 file').toBeTruthy()
  expect(await ui.find({ text: /matrixkoder\.yaml/ }), 'the pre-migration name is not offered').toBeUndefined()
  await ui.press({ key: 'wz-write' })
  expect(files[`${c2}/project.yaml`], 'the C2 file is the one written').toContain('integration_branch')
  expect(files[YAML], 'no second matrixkoder.yaml beside it').toBeUndefined()
  const reg = w.argv.filter(a => a[2] === 'project' && a[3] === 'register').pop()
  expect(reg?.slice(4)).toEqual(['--slug', 'mk-proj', '--repo', PROJECT_DIR, '--data', c2])
  await ui.unmount()
})

test('B-2: a repo folder name mk-state would refuse is normalised into a slug it accepts', async ($, on) => {
  // /tmp/my_project: the old slugOf kept `_`, and `project register` refused it AFTER the yaml was on disk
  const files: Files = Object.fromEntries(Object.entries(repoFiles()).map(([k, v]) => [k.replace(PROJECT_DIR, '/tmp/my_project'), v]))
  const w = world(on, files, ProcsFor('/tmp/my_project'))
  await $.session.start(sessionStart())
  await mk($, 'setup')
  expect(draftOf().slug).toBe('my-project')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(46) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  const reg = w.argv.filter(a => a[2] === 'project' && a[3] === 'register').pop()
  expect(reg?.[5], 'the slug handed to mk-state satisfies ^[a-z0-9][a-z0-9-]{0,40}$').toMatch(/^[a-z0-9][a-z0-9-]{0,40}$/)
  expect(await ui.find({ text: /MatrixKoder is set up/ })).toBeTruthy()
  await ui.unmount()
})

test('B-4: keep means keep — a kept yaml is never rewritten by mk-config migrate', async ($, on) => {
  const files = repoFiles()
  const kept = '# mine\ndata_dir: .matrixkoder/\n'
  files[YAML] = kept
  const w = world(on, files, {}, {}, [], argv => {
    if (argv[0] === 'python3' && argv.includes('migrate')) return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    return undefined
  })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(46) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect(files[YAML], 'the kept file is byte-identical').toBe(kept)
  expect(w.argv.some(a => a[0] === 'python3' && a.includes('migrate')), 'no migration ran against the kept yaml').toBe(false)
  await ui.unmount()

  // a replace DID write the file, so the migration still runs there
  await mk($, 'setup')
  const ui2 = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(46) })
  await toReview(ui2)
  await ui2.select({ key: 'wz-replace', value: 'replace' })
  await ui2.press({ key: 'wz-write' })
  expect(w.argv.some(a => a[0] === 'python3' && a.includes('migrate')), 'a replaced yaml is migrated').toBe(true)
  await ui2.unmount()
})

test('a successful write runs the mk-config migration once', async ($, on) => {
  const files = repoFiles()
  const w = world(on, files, {}, {}, [], argv => {
    if (argv[0] === 'python3') return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    return undefined
  })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  const migrates = w.argv.filter(a => a[0] === 'python3' && a.includes('migrate'))
  expect(migrates.length).toBe(1)
  expect(migrates[0]?.[1]).toBe(`${draftOf().pluginRoot}/scripts/mk-config.py`)
  expect(await ui.find({ text: /✓ MatrixKoder is set up/ })).toBeTruthy()
  expect(await ui.find({ text: /⚠ config migration/ })).toBeUndefined()
  await ui.unmount()
})

test('a failing migration warns but never fails the wizard', async ($, on) => {
  const files = repoFiles()
  world(on, files, {}, {}, [], argv => {
    if (argv[0] === 'python3' && argv.includes('migrate')) return { value: { exitCode: 1, stdout: '', stderr: 'python3: not available', isStdoutTruncated: false, isStderrTruncated: false } }
    return undefined
  })
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(64) })
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect(await ui.find({ text: /✓ MatrixKoder is set up/ })).toBeTruthy()
  expect(await ui.find({ text: /config migration skipped: python3: not available/ })).toBeTruthy()
  await ui.unmount()
})
