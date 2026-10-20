// The shared chrome of the setup wizard against the Row 3 slides: the brand
// header (46 columns, version chip from plugin.json), the step card for all ten
// steps, the nav row and footer hints exactly as the slides spell them, the
// loading and not-docked states, the mobile notice, the draft clearing on
// Cancel/Close (B-12) and `/mk init <name>`'s slug (B-14/B-2).
// Harness shape copied from wizard.test.ts: mocked process/fs/clock only.
import { test, expect, mock } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { MkWizardDraft } from '../types'
import { NOW, PROJECT_DIR, DATA_DIR, mockFs, paneProps, sessionStart, type Files } from './testkit'
import { WIZARD_STEPS } from './wizard-core'

const HOME = '/home/mk'
const WIZARD = 'matrixkoder-setup'

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

/** clock, env, process (argv → canned stdout, unknown → exit 1), fs and ui.open/close. */
function world(on: On, files: Files, procs: Record<string, string | null> = {}): World {
  const w: World = { argv: [], opened: [], closed: [], state: new Map() }
  worldOf = w
  const table = { ...PROCS, ...procs }
  mock.clock(on, { now: NOW })
  mock.env(on, { HOME })
  mockFs(on, files, [`${PROJECT_DIR}/.git`])
  on('fs.exists', (_$, e) => ({ value: e.path in files }))
  on('session.start', (_$, _e) => ({ cwd: PROJECT_DIR }))
  on('prompt.compose', (_$, _e) => ({ sections: [] }))
  on('process.run', async (_$, e) => {
    w.argv.push([...e.argv])
    if (e.argv[1]?.endsWith('/scripts/mk-state.py') && e.argv[2] === 'project' && e.argv[3] === 'register') {
      return { value: { exitCode: 0, stdout: '{"ok": true}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    if (e.argv[1]?.endsWith('/scripts/mk-state.py') && e.argv[2] === 'sync') {
      return { value: { exitCode: 0, stdout: '{"ok": true, "store": "on", "project": "mk-proj", "copied": 0}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    // B-4: mk-config migrate succeeds (the wizard reads only its exit code; a failure would warn)
    if (e.argv[1]?.endsWith('/scripts/mk-config.py') && e.argv[2] === 'migrate') {
      return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    // mv / cp act on the in-memory files, as the real commands would on disk (the yaml write uses tmp + mv)
    if (e.argv[0] === 'mv' || e.argv[0] === 'cp') {
      const src = e.argv[e.argv.length - 2] ?? ''
      const dst = e.argv[e.argv.length - 1] ?? ''
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
  // the step-10 write applies the four dashboard prefs (B-5): accept them, as the engine does
  on('config.set', (_$, e) => ({ value: e.value }))
  return w
}

/** `/mk <args>` as the engine raises it: presentation stamped (fullscreen). */
function mk($: Engine, args: string) {
  return $.command.run({ command: 'mk', args, origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 100 } })
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

type Walker = { find(q: { key: string }): Promise<unknown>; press(a: { key: string }): Promise<unknown> }

async function toReview(ui: Walker): Promise<void> {
  while (await ui.find({ key: 'wz-next' })) await ui.press({ key: 'wz-next' })
}

/** A repo whose plugin manifest lives at the plugin root: the engine names the root, so the mock
 *  matches the manifest by its suffix (the dashboard.test.ts:884 pattern). */
function withManifest(files: Files, version: string): Files {
  const manifest = JSON.stringify({ name: 'matrixkoder', version })
  const isManifest = (k: string | symbol): boolean => typeof k === 'string' && k.endsWith('/.claude-plugin/plugin.json')
  return new Proxy(files, {
    has: (f, k) => isManifest(k) || k in f,
    get: (f, k) => (isManifest(k) ? manifest : f[k as string]),
  })
}

/** mount the wizard pane for the running test's world */
function mount($: Engine, columns = 46, placement: 'dock' | 'inline' = 'dock') {
  return $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: WIZARD, props: paneProps(columns, placement) })
}

// ---------- the header (Row 3: brand block, SETUP chip, status strip) ----------

test('the header draws the wordmark, the SETUP chip and the 46-column status strip', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await mount($)
  expect(await ui.find({ text: 'MATRIXKODER' }), 'brand wordmark').toBeTruthy()
  expect(await ui.find({ text: 'SETUP' }), 'SETUP chip').toBeTruthy()
  expect(await ui.find({ text: /herdr · claude code · \/mk setup · 46 cols · step 1 Install/ }), 'status strip').toBeTruthy()
  await ui.unmount()
})

test('the version chip comes from plugin.json', async ($, on) => {
  world(on, withManifest(repoFiles(), '9.8.7'))
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await mount($)
  expect(await ui.find({ text: / v9\.8\.7 / }), 'version chip from the manifest').toBeTruthy()
  await ui.unmount()
})

test('with no manifest served the header draws no version chip', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await mount($)
  expect((await ui.findAll({ type: 'Text' })).some(t => / v\d/.test(String(t.text))), 'no manifest, no version chip').toBe(false)
  await ui.unmount()
})

// ---------- the step card: every step 1..10 ----------

test('the step card names the step and chips STEP n/10 for all ten steps', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await mount($)
  for (let i = 0; i < WIZARD_STEPS.length; i++) {
    const n = i + 1
    expect(await ui.find({ text: `⬡ ${WIZARD_STEPS[i] ?? ''}` }), `step ${n} card title`).toBeTruthy()
    expect(await ui.find({ text: `STEP ${n}/10` }), `step ${n} chip`).toBeTruthy()
    if (n < 10) await ui.press({ key: 'wz-next' })
  }
  await ui.unmount()
})

// ---------- the nav row and the footer hints, as the slides spell them ----------

/** What each slide's nav row draws: ← Back, Next →, Cancel (step 10 swaps Next for w Write these). */
const NAV: Record<number, { back: boolean; next: boolean }> = {
  1: { back: false, next: true },
  2: { back: true, next: true },
  3: { back: true, next: true },
  4: { back: true, next: true },
  5: { back: true, next: true },
  6: { back: true, next: true },
  7: { back: true, next: true },
  8: { back: true, next: true },
  9: { back: true, next: true },
  10: { back: true, next: false },
}

/** Each slide's footer hint row, verbatim: SetupInstall..SetupReview. */
const FOOTERS: Record<number, string> = {
  1: ' n next · c cancel',
  2: ' n next · b back · c cancel',
  5: ' tab next field · ↵ next step',
  6: ' tab next field · ↵ next step',
  7: ' tab next field · ↵ next step',
  8: ' tab next field · ↵ next step',
  9: ' tab next field · ↵ next step',
  10: ' w write · b back · c cancel',
}

test('the nav row and footer hints match the slides step by step', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await mount($)
  for (let n = 1; n <= 10; n++) {
    const nav = NAV[n]
    const hasBack = nav?.back === true
    const hasNext = nav?.next === true
    expect((await ui.find({ key: 'wz-back' })) !== undefined, `step ${n} ← Back ${hasBack ? 'drawn' : 'absent'}`).toBe(hasBack)
    expect((await ui.find({ text: '← Back' })) !== undefined, `step ${n} ← Back label`).toBe(hasBack)
    expect((await ui.find({ key: 'wz-next' })) !== undefined, `step ${n} Next → ${hasNext ? 'drawn' : 'absent'}`).toBe(hasNext)
    expect((await ui.find({ text: 'Next →' })) !== undefined, `step ${n} Next → label`).toBe(hasNext)
    // Cancel sits in the nav row on every unwritten step (SetupInstall has no Back, SetupReview no Next)
    expect(await ui.find({ key: 'wz-cancel' }), `step ${n} Cancel`).toBeTruthy()
    expect(await ui.find({ text: 'Cancel' }), `step ${n} Cancel label`).toBeTruthy()
    if (n === 3) {
      // the slide's footer: ` 1-4 toggle · n next · b back` (the chrome also appends `c cancel`)
      for (const hint of ['1-4 toggle', 'n next', 'b back']) expect(await ui.find({ text: hint }), `step 3 footer ${hint}`).toBeTruthy()
    } else if (n === 4) {
      // the slide's footer: ` 1-9 toggle · n next · b back` (the chrome also appends `c cancel`)
      for (const hint of ['1-9 toggle', 'n next', 'b back']) expect(await ui.find({ text: hint }), `step 4 footer ${hint}`).toBeTruthy()
    } else {
      expect(await ui.find({ text: FOOTERS[n] ?? '' }), `step ${n} footer row`).toBeTruthy()
    }
    if (n < 10) await ui.press({ key: 'wz-next' })
  }
  // step 10's nav row keeps Cancel and its card carries the write button the slide draws
  expect(await ui.find({ text: 'w Write these' }), 'step 10 w Write these').toBeTruthy()
  await ui.unmount()
})

test('the written state draws x Close and the x close hint', async ($, on) => {
  const files = repoFiles()
  world(on, files)
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await mount($)
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  const d = draftOf()
  expect(d.error, `write error: ${String(d.error)}`).toBeNull()
  expect(d.phase).toBe('written')
  expect(files[`${DATA_DIR}/matrixkoder.yaml`], 'the write landed').toContain('integration_branch')
  expect(await ui.find({ text: 'x Close' }), 'x Close label').toBeTruthy()
  expect(await ui.find({ text: ' x close' }), 'x close hint').toBeTruthy()
  expect(await ui.find({ key: 'wz-next' }), 'no Next after the write').toBeUndefined()
  expect(await ui.find({ key: 'wz-cancel' }), 'no Cancel after the write').toBeUndefined()
  await ui.unmount()
})

// ---------- SetupLoading ----------

test('SetupLoading holds the detecting line and draws no nav', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  // no /mk setup: the draft stays at emptyDraft(), so loaded is false
  const ui = await mount($)
  expect(await ui.find({ text: 'Detecting agents, usage and gates…' }), 'detecting line in the step card').toBeTruthy()
  expect(await ui.find({ text: /herdr · claude code · \/mk setup · 46 cols · detecting/ }), 'detecting status strip').toBeTruthy()
  expect(await ui.find({ key: 'wz-next' }), 'no Next while loading').toBeUndefined()
  expect(await ui.find({ key: 'wz-cancel' }), 'no Cancel while loading').toBeUndefined()
  expect(await ui.find({ key: 'wz-back' }), 'no Back while loading').toBeUndefined()
  await ui.unmount()
})

// ---------- SetupNotDocked ----------

test('SetupNotDocked keeps the inline hint and still draws the step body', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await mount($, 46, 'inline')
  expect(await ui.find({ text: 'Run /tui fullscreen to dock this panel on the right.' }), 'inline hint').toBeTruthy()
  expect(await ui.find({ text: 'Install mode' }), 'step 1 body still draws').toBeTruthy()
  expect(await ui.find({ key: 'wz-next' }), 'the step is still usable').toBeTruthy()
  await ui.redraw(paneProps(46, 'dock'))
  expect(await ui.find({ text: /\/tui fullscreen/ }), 'docked: hint gone').toBeUndefined()
  await ui.unmount()
})

// ---------- B-12: Cancel and Close clear the draft ----------

test('B-12: Cancel after a toggle restores the detected defaults and step 1', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await mount($)
  await ui.press({ key: 'wz-next' })
  await ui.press({ key: 'wz-next' }) // step 3
  expect(stepOf()).toBe(3)
  await ui.press({ key: 'wz-agent-codex' })
  expect((draftOf()).agents.codex?.enabled).toBe(false)
  await ui.press({ key: 'wz-cancel' })
  expect((draftOf()).loaded, 'the draft is dropped').toBe(false)
  expect(stepOf(), 'step is back to 1').toBe(1)
  await ui.unmount()

  await mk($, 'setup') // re-open: freshly detected, nothing resumed
  const again = await mount($)
  expect((draftOf()).loaded).toBe(true)
  expect((draftOf()).agents.codex?.enabled, 'the toggle is gone').toBe(true)
  expect(stepOf()).toBe(1)
  expect(await again.find({ text: 'STEP 1/10' })).toBeTruthy()
  await again.unmount()
})

test('B-12: x Close after a write drops the draft and the step', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await mount($)
  await toReview(ui)
  await ui.press({ key: 'wz-write' })
  expect((draftOf()).phase).toBe('written')
  await ui.press({ key: 'wz-close' })
  expect((draftOf()).loaded, 'the draft is dropped').toBe(false)
  expect(stepOf(), 'step is back to 1').toBe(1)
  await ui.unmount()

  await mk($, 'setup')
  const again = await mount($)
  expect((draftOf()).phase, 'no written state survives').toBe('editing')
  expect(stepOf()).toBe(1)
  expect(await again.find({ text: 'STEP 1/10' })).toBeTruthy()
  await again.unmount()
})

// ---------- B-13: mobile draws a notice, never an empty pane ----------

test('B-13: the mobile surface draws the desktop notice, text, and no Input', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'setup')
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'mobile', component: 'Pane', requestId: WIZARD, props: paneProps(46) })
  expect(await ui.find({ text: /\/mk setup on desktop/ }), 'the desktop notice').toBeTruthy()
  expect((await ui.findAll({ type: 'Text' })).length, 'not an empty pane').toBeGreaterThan(0)
  expect((await ui.findAll({ type: 'Input' })).length, 'mobile draws no Input').toBe(0)
  await ui.unmount()
})

// ---------- B-14/B-2: /mk init carries the name, slugOf normalises ----------

test('B-14/B-2: /mk init sets the slug, /mk setup keeps it, underscores become dashes', async ($, on) => {
  world(on, repoFiles())
  await $.session.start(sessionStart())
  await mk($, 'init feature-x')
  expect((draftOf()).slug).toBe('feature-x')
  await mk($, 'setup')
  expect((draftOf()).slug, 'setup takes no name').toBe('mk-proj')
  await mk($, 'init my_project')
  expect((draftOf()).slug, 'mkdb slug rule: no underscore').toBe('my-project')
})
