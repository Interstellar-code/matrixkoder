// MatrixKoder onboarding wizard (plan §7): a 10-step Pane (id matrixkoder-setup)
// driven by two $.state atoms — wizardStep and wizardDraft. Detection is
// read-only and runs once when /mk setup opens the pane; NOTHING is written
// until the review step's single confirm. The pure halves (parsers, gate
// proposals, YAML emitter) are in wizard-core.ts. API keys are never asked for
// or stored: step 4 only prints the command the user runs themselves.
import { atom, read, update } from 'claude-code'
import type { Elements as ElementTables, EngineInterface, On, PluginOptions, RenderChildren, RenderElement } from 'claude-code'
import type { MkWizardDraft } from '../types'
import { globalDirPath, join, parseJson } from './data'
import { listedDataDir, paths as mkPaths, projectYamlPath } from './paths'
import { parsePluginManifest } from './settings-io'
import {
  AGENT_NAMES, WIZARD_STEPS, addToGitignore, buildYaml, dataDirOf, EMPTY_DRAFT, emptyDraft, fallbackText, installModeOf, mergeModels, parseModels, parseProviders, parseVersion,
  parseWhich, proposeGates, providerChoices, slugOf,
} from './wizard-core'
import { StepsA, type WizardAct } from './wizard-steps-a'
import { StepsB } from './wizard-steps-b'
import { Card, Chip } from './tabs/kit'
import { PALETTE, ROLE } from './tabs/palette'

/** Surfaces that draw Input and Select (mobile does not): the wizard's steps need both. */
type Elements = ElementTables['terminal'] | ElementTables['desktop'] | ElementTables['vscode']
/** Chrome only — the brand block, the step card, the notice — draws on mobile too. */
type ChromeEls = ElementTables['terminal'] | ElementTables['desktop'] | ElementTables['vscode'] | ElementTables['mobile']

export const WIZARD_ID = 'matrixkoder-setup'
/** The pane opens at the slides' own width (the Row 3 artboards are 46 columns wide, contract C2). */
const COLUMNS = 46
const PROBE_MS = 5000
const LAST_STEP = WIZARD_STEPS.length

// atoms are file-local consts (the scan reads this file's own consts)
const stepAtom = atom({ plugin: 'matrixkoder', key: 'wizardStep' } as const, 1)
const draftAtom = atom({ plugin: 'matrixkoder', key: 'wizardDraft' } as const, EMPTY_DRAFT)

export function registerWizard(on: On, options: PluginOptions): void {
  // /mk setup | init: dashboard.tsx hands these down with next(e); anything else goes back down the chain
  on('command.run', { command: 'mk' }, async ($, e, next) => {
    const [arg = '', ...rest] = e.args.trim().split(/\s+/)
    const verb = arg.toLowerCase()
    if (verb !== 'setup' && verb !== 'init') return next(e)
    // B-14: `/mk init <name>` used to drop the name silently. It is the project slug's default now;
    // `setup` takes no name (it is the same wizard for a project that already has one).
    const name = verb === 'init' ? (rest[0] ?? '').trim() : ''
    return { text: await openWizard($, e.presentation, options, name) }
  })

  on('ui.render', { component: 'Pane', requestId: WIZARD_ID }, async ($, e, next) => {
    const chrome = $.ui.resolve(e)
    const { Box, Text } = chrome
    const step = await read($, stepAtom)
    const d = await read($, draftAtom)
    const cols = e.props.bodyColumns || COLUMNS
    // B-13: the mobile app draws no Input and no Select, so the ten steps cannot run there. Before
    // this, mobile fell through to `next(e)` and drew an EMPTY pane: the command had already claimed
    // the wizard was open. The notice below is the whole surface it gets, and it says where to go.
    if (e.surface === 'mobile') {
      return (
        <Box flexDirection="column" width={cols} gap={1}>
          {Header(chrome, d, cols, 'mobile')}
          {Card(chrome, { role: 'warn' }, <Text color={ROLE.warn.fg}>Open /mk setup on desktop to run the setup wizard: this app draws no text fields yet.</Text>)}
          <Text dimColor={true}>The wizard writes project.yaml, so it needs the docked pane.</Text>
        </Box>
      )
    }
    // the review preview is exactly what a replace writes: read the yaml the carry-over needs (readOpt: null on any failure)
    const data = dataDirOf(d)
    // the replace form diffs against the file on disk whichever way the keep/replace Select stands, so the
    // old text is read whenever there IS an old file (B-6); the create form gets null and draws no diff
    const oldYaml = step === LAST_STEP && d.existingYaml && data != null ? await readOpt($, await yamlPathOf($, d, data)) : null
    // the preview must be byte-identical to the write (B-6): one clock read for both
    const checkedAt = step === LAST_STEP ? new Date(await $.clock.now()).toISOString() : ''
    // the mobile surface already returned: this table is the terminal's, the desktop's or vscode's
    const els = $.ui.resolve(e) as Elements
    const inline = e.props.placement === 'inline'
    return (
      <Box flexDirection="column" width={cols} gap={1}>
        {/* SetupLoading is the brand block and the step card alone, with the detecting line inside it */}
        {Header(els, d, cols, statusOf(d, step, inline))}
        {inline && Card(els, { role: 'warn' }, <Text color={ROLE.warn.fg}>Run /tui fullscreen to dock this panel on the right.</Text>)}
        {!d.loaded
          ? (
            <>
              {StepCard(els, step, false, <Text dimColor={true}>Detecting agents, usage and gates…</Text>)}
              {FooterHints(els, d, step)}
            </>
          )
          : (
            <>
              {StepCard(els, step, true, Body(els, $, d, step, options, oldYaml, checkedAt))}
              {Nav(els, $, d, step)}
              {FooterHints(els, d, step)}
            </>
          )}
      </Box>
    )
  })
}

// ---------- the chrome every step shares (the Row 3 slides' header, card, nav and hint row) ----------

/** The slides' artboard is 46 columns wide; a pane opened at 46 draws them verbatim, a wider one pads. */
const SLIDE_COLUMNS = 46

/** The status strip's own words, as each slide spells them: `step N <Name>`, `detecting`,
 *  `inline, not fullscreen`, `step 10 Review · existing file`, `done`. */
function statusOf(d: MkWizardDraft, step: number, inline: boolean): string {
  if (d.phase === 'written') return 'done'
  if (!d.loaded) return 'detecting'
  if (inline) return 'inline, not fullscreen'
  const base = `step ${step} ${WIZARD_STEPS[step - 1] ?? ''}`
  return step === LAST_STEP && d.existingYaml ? `${base} · existing file` : base
}

/** The brand block (the dashboard's own: two-tone block, MATRIXKODER, the version chip) plus the
 *  SETUP chip under it, then the status strip line the slides carry at the top. */
function Header(els: ChromeEls, d: MkWizardDraft, cols: number, status: string): ReturnType<ChromeEls['Box']> {
  const { Box, Text } = els
  const version = d.version === '' ? '' : d.version.startsWith('v') ? d.version : `v${d.version}`
  return (
    <Box flexDirection="column" width={cols}>
      <Text dimColor={true}>{`herdr · claude code · /mk setup · ${Math.min(cols, SLIDE_COLUMNS)} cols · ${status}`}</Text>
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Box flexDirection="row" gap={1}>
          <Box flexDirection="row"><Text color={PALETTE.blue}>█</Text><Text color={PALETTE.grn}>█</Text></Box>
          <Text bold={true}>MATRIXKODER</Text>
        </Box>
        {version !== '' && Chip(els, { label: version, role: 'live', solid: true })}
      </Box>
      <Box flexDirection="row">{Chip(els, { label: 'SETUP', role: 'ok', solid: true })}</Box>
    </Box>
  )
}

/** The strip: ten cells, a dim cell per step behind, a solid cell per step done and the live one
 *  bright at the current step. Two cells per step keeps 46 columns from looking like a rule. */
function ProgressStrip(els: ChromeEls, step: number): ReturnType<ChromeEls['Text']> {
  const { Text } = els
  return (
    <Text>
      {WIZARD_STEPS.map((_, i) => (
        <Text key={`s${String(i)}`} color={i + 1 < step ? PALETTE.grn : i + 1 === step ? PALETTE.blue : PALETTE.line}>{i + 1 === step ? '█' : i + 1 < step ? '▀' : '▁'}</Text>
      ))}
    </Text>
  )
}

/** The step card: `⬡ <Step>` with the `STEP n/10` chip and the progress strip, then the step's body. */
function StepCard(els: ChromeEls, step: number, live: boolean, children: RenderChildren): RenderElement {
  const { Box, Text } = els
  return Card(els, live ? { role: 'live' } : {}, (
    <Box flexDirection="column" gap={1}>
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text color={ROLE.live.fg} bold={true}>{`⬡ ${WIZARD_STEPS[step - 1] ?? ''}`}</Text>
        {Chip(els, { label: `STEP ${step}/${LAST_STEP}`, role: 'accent' })}
      </Box>
      {ProgressStrip(els, step)}
      {children}
    </Box>
  ))
}

/** The footer hint row the slides carry under the nav: the keys the current step honours. */
/** The slides' hint rows verbatim (contract C2). They are not a summary of every key the pane honours:
 *  they are the words each artboard prints, which is what the render tests pin. */
const FOOTER: Record<number, readonly string[]> = {
  1: ['n next', 'c cancel'],
  2: ['n next', 'b back', 'c cancel'],
  3: ['1-4 toggle', 'n next', 'b back'],
  4: ['1-9 toggle', 'n next', 'b back'],
  5: ['tab next field', '↵ next step'],
  6: ['tab next field', '↵ next step'],
  7: ['tab next field', '↵ next step'],
  8: ['tab next field', '↵ next step'],
  9: ['tab next field', '↵ next step'],
  10: ['w write', 'b back', 'c cancel'],
}

function FooterHints(els: ChromeEls, d: MkWizardDraft, step: number): ReturnType<ChromeEls['Text']> {
  const { Text } = els
  const parts = !d.loaded ? ['esc close'] : d.phase === 'written' ? ['x close'] : (FOOTER[step] ?? [])
  return <Text dimColor={true}>{` ${parts.join(' · ')}`}</Text>
}

/** Opens the wizard pane (fullscreen) or answers with the detected defaults as text. Never throws. */
async function openWizard($: EngineInterface, presentation: { isFullscreen: boolean } | undefined, options: PluginOptions, name = ''): Promise<string> {
  // the engine stamps presentation on every command.run; the test harness leaves it off (treated as fullscreen)
  if (presentation?.isFullscreen === false) {
    return fallbackText(await detect($, options))
  }
  await update($, stepAtom, () => 1)
  await update($, draftAtom, () => emptyDraft())
  try {
    await $.ui.open({ id: WIZARD_ID, title: 'MatrixKoder setup', columns: COLUMNS, focus: true, closeOnEscape: true })
  } catch {
    return 'Could not open the MatrixKoder setup pane. Run /tui fullscreen, then /mk setup again.'
  }
  const draft = await detect($, options)
  // B-14: a name given to `/mk init` becomes the slug (the step-2 data dir and the root row follow it);
  // an empty or unusable name keeps detection's own slug rather than inventing one.
  const named = name === '' ? '' : slugOf(name)
  await update($, draftAtom, () => (named === '' ? draft : { ...draft, slug: named }))
  return `MatrixKoder setup opened — every step is pre-filled; nothing is written until the final confirm.${named === '' ? '' : ` Project slug: ${named}.`}`
}

// ---------- detection (read-only: version/which/models probes and file reads) ----------

async function run($: EngineInterface, argv: readonly string[], cwd?: string): Promise<{ ok: boolean; out: string }> {
  try {
    const r = await $.process.run(argv, { timeoutMs: PROBE_MS, ...(cwd ? { cwd } : {}) })
    return { ok: r.exitCode === 0, out: r.stdout }
  } catch {
    return { ok: false, out: '' } // missing binary or timeout
  }
}

async function readOpt($: EngineInterface, path: string): Promise<string | null> {
  try {
    return await $.fs.read(path)
  } catch {
    return null
  }
}

async function homeOf($: EngineInterface): Promise<string | null> {
  try {
    const v = await $.env.get('HOME')
    if (v) return v
  } catch {
    // env refused — ask the process
  }
  const r = await run($, ['printenv', 'HOME'])
  return r.ok && r.out.trim() !== '' ? r.out.trim() : null
}

async function detect($: EngineInterface, options: PluginOptions): Promise<MkWizardDraft> {
  const d = emptyDraft()
  d.home = await homeOf($)
  let dataEnv: string | undefined
  let homeEnv: string | undefined
  try {
    dataEnv = (await $.env.get('MATRIXKODER_DATA'))?.trim() || undefined
    homeEnv = (await $.env.get('MATRIXKODER_HOME'))?.trim() || undefined
  } catch {
    // env refused: as unset
  }
  d.dataEnv = dataEnv ?? null
  d.globalDir = globalDirPath(homeEnv, d.home)
  d.pluginRoot = $.plugin.root
  let forcedMode: 'dev' | 'marketplace' | null = null
  try {
    const m = (await $.env.get('MATRIXKODER_INSTALL_MODE'))?.trim()
    if (m === 'dev' || m === 'marketplace') forcedMode = m
  } catch {
    // env refused: no override
  }
  d.installMode = forcedMode ?? installModeOf(d.pluginRoot)
  // the header's version chip: plugin.json's own version, never a literal (the slides' v0.6.8 drifted)
  d.version = parsePluginManifest(await readOpt($, join(d.pluginRoot, '.claude-plugin', 'plugin.json')) ?? '').version
  if (d.version === 'unknown') d.version = ''
  d.autoOpen = options.autoOpen !== false
  d.chip = options.chip !== false
  d.toasts = options.toasts !== false
  if (typeof options.usagePollMin === 'number' && options.usagePollMin > 0) d.pollMin = String(Math.round(options.usagePollMin))

  // repo: the main checkout (a linked worktree resolves to it), as the data-dir rule does
  const common = await run($, ['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'])
  const gitDir = common.out.trim()
  d.repo = common.ok && gitDir.endsWith('/.git') ? gitDir.slice(0, -'/.git'.length) : null
  if (d.repo != null) {
    d.slug = slugOf(d.repo)
    const br = await run($, ['git', 'symbolic-ref', '--short', 'HEAD'], d.repo)
    if (br.ok && br.out.trim() !== '') d.branch = br.out.trim()
  }

  const agentRun = await run($, ['which', ...AGENT_NAMES])
  const paths = parseWhich(agentRun.out)
  const [agyModels, ocModels, ocPlanModels, ocOllamaModels, cbVersion, cbProviders, herdrList] = await Promise.all([
    paths.agy ? run($, ['agy', 'models']) : null,
    paths.opencode ? run($, ['opencode', 'models']) : null,
    // the bare list shows only opencode/* and opencode-go/*; the Z.AI plan models need their own probe
    paths.opencode ? run($, ['opencode', 'models', 'zai-coding-plan']) : null,
    // Ollama Cloud models are listed under their own plan
    paths.opencode ? run($, ['opencode', 'models', 'ollama-cloud']) : null,
    run($, ['codexbar', '--version']),
    run($, ['codexbar', 'config', 'providers']),
    paths.herdr ? run($, ['herdr', 'plugin', 'list']) : null,
  ])
  const models: Record<string, readonly string[]> = {
    agy: agyModels?.ok ? mergeModels(parseModels(agyModels.out)) : [],
    opencode: mergeModels(
      ocModels?.ok ? parseModels(ocModels.out) : [],
      ocPlanModels?.ok ? parseModels(ocPlanModels.out) : [],
      ocOllamaModels?.ok ? parseModels(ocOllamaModels.out) : [],
    ),
  }
  const agents: MkWizardDraft['agents'] = Object.fromEntries(
    AGENT_NAMES.map(n => [n, { found: n in paths, path: paths[n] ?? null, enabled: n in paths, models: models[n] ?? [] }]),
  )
  d.agents = agents
  const herdrLinked = herdrList?.ok ? herdrList.out.includes('matrixkoder') : false
  d.herdrPlugin = agents.herdr?.found ? (herdrLinked ? 'linked' : 'link') : 'skip'
  if (cbVersion.ok) {
    d.codexbarVersion = parseVersion(cbVersion.out) ?? 'unknown'
    const choices = providerChoices(parseProviders(cbProviders.out))
    if (choices.all.length > 0) {
      d.providersAll = choices.all
      d.providersOn = choices.on
    }
    // the opencode agent on the Z.AI plan tracks zai even before its CodexBar toggle is on
    const oc = agents.opencode
    if (oc?.enabled && oc.models.some(m => m.startsWith('zai-coding-plan/')) && d.providersAll.includes('zai') && !d.providersOn.includes('zai')) {
      d.providersOn = [...d.providersOn, 'zai']
    }
    // same for Ollama Cloud
    if (oc?.enabled && oc.models.some(m => m.startsWith('ollama-cloud/')) && d.providersAll.includes('ollama') && !d.providersOn.includes('ollama')) {
      d.providersOn = [...d.providersOn, 'ollama']
    }
  }

  if (d.repo != null) {
    const repo = d.repo
    const lockNames = ['pnpm-lock.yaml', 'yarn.lock', 'bun.lock', 'bun.lockb', 'package-lock.json', 'uv.lock', 'poetry.lock']
    const markerNames = ['.claude-plugin/plugin.json', 'scripts/selftest.sh', 'Cargo.toml', 'go.mod']
    const [pkg, pyproject, makefile, tsconfig, gitignore, locks, markerHits] = await Promise.all([
      readOpt($, join(repo, 'package.json')),
      readOpt($, join(repo, 'pyproject.toml')),
      readOpt($, join(repo, 'Makefile')),
      $.fs.exists(join(repo, 'tsconfig.json')).catch(() => false),
      readOpt($, join(repo, '.gitignore')),
      Promise.all(lockNames.map(n => $.fs.exists(join(repo, n)).catch(() => false))),
      Promise.all(markerNames.map(n => $.fs.exists(join(repo, n)).catch(() => false))),
    ])
    const proposal = proposeGates({
      pkg: pkg == null ? null : parseJson(pkg), pyproject, makefile, hasTsconfig: tsconfig,
      locks: lockNames.filter((_, i) => locks[i]),
      markers: markerNames.filter((_, i) => markerHits[i]),
    })
    d.gates = proposal.gates
    d.gatesFrom = proposal.from
    d.banned = proposal.banned
    d.gitignoreHas = addToGitignore(gitignore) === null
    d.gitignore = !d.gitignoreHas
    d.qaFrontend = pkg != null && /"(react|vue|svelte|next|vite|@tanstack\/react-start)"/.test(pkg)
  }
  // Default to home mode (`<globalDir>/projects/<slug>/`) for a repo that has neither a
  // data dir nor a project layout yet. A repo that already builds (a package.json, a
  // pyproject, a Makefile) keeps repo mode, and so does one that already has
  // `<repo>/.matrixkoder`. The write step registers the project in the root store through
  // `mk-state.py project register` (L10.7); <home>/projects.json is no longer written.
  if (d.dataEnv == null && d.repo != null && d.globalDir != null) {
    const repoData = await $.fs.exists(mkPaths.legacyDataDir(d.repo)).catch(() => false)
    const established = await $.fs.exists(join(d.repo, 'package.json')).catch(() => false)
      || await $.fs.exists(join(d.repo, 'pyproject.toml')).catch(() => false)
      || await $.fs.exists(join(d.repo, 'Makefile')).catch(() => false)
    if (!repoData && !established) d.dataMode = 'home'
  }
  // B-1: which folder and which file name this project's settings live in. The root store's own row for
  // this repo is the pane's rule (C1, `listedDataDir` + `projectDirFromStore`): it names the data dir, and a
  // MIGRATED row (a `db_path` set) names the C2 folder `<globalDir>/projects/<slug>`. With no row the mode
  // above decides. `projectYamlPath` then owns the file name for that folder, so the wizard reads and writes
  // the same paths `mk-config.py`, the dashboard and the Settings tab do.
  if (d.dataEnv == null && d.repo != null && d.globalDir != null) {
    d.storeDir = await listedDataDir($, [d.repo], d.home, { store: true }).catch(() => null)
  }
  const data = dataDirOf(d)
  const yamlPath = data == null ? null : await projectYamlPath($, data, { home: d.home, store: true })
  d.migrated = data != null && yamlPath === mkPaths.projectYaml(data, true)
  d.existingYaml = yamlPath != null && (await $.fs.exists(yamlPath).catch(() => false))
  d.loaded = true
  return d
}

// ---------- state helpers ----------

const patch = ($: EngineInterface, p: Partial<MkWizardDraft> | ((cur: MkWizardDraft) => Partial<MkWizardDraft>)): Promise<unknown> =>
  update($, draftAtom, d => ({ ...d, ...(typeof p === 'function' ? p(d) : p) }))
const goto = ($: EngineInterface, step: number): Promise<unknown> =>
  (async () => {
    const target = Math.min(LAST_STEP, Math.max(1, step))
    const res = await update($, stepAtom, () => target)
    if (target === 5) {
      const d = await read($, draftAtom)
      if (d.gatesFrom === '') {
        try {
          await $.ui.focus({ requestId: WIZARD_ID, key: 'wz-gate-install' })
        } catch {
          // focus refused (the site does not hold the keys) — the Input's autoFocus still seats the ring on redraw
        }
      }
    }
    return res
  })()

/** The project's yaml path, migrated or not (B-1): a migrated project keeps `project.yaml` in its C2
 *  folder, everything older keeps `matrixkoder.yaml` in the data dir. `projectYamlPath` owns the rule
 *  (paths.ts, mkconfig's twin); this one wrapper only hands it the store flag the wizard already knows. */
async function yamlPathOf($: EngineInterface, d: MkWizardDraft, data: string): Promise<string> {
  return mkPaths.projectYaml(data, d.migrated)
}

async function refreshExisting($: EngineInterface, p: Partial<MkWizardDraft>): Promise<void> {
  const next = { ...(await read($, draftAtom)), ...p }
  const data = dataDirOf(next)
  const existing = data != null && (await $.fs.exists(await yamlPathOf($, next, data)).catch(() => false))
  await patch($, { ...p, existingYaml: existing, replace: false })
}

// ---------- writing (step 10 confirm) ----------

/** Absent (false exists) is null; present-but-unreadable THROWS: never treat a failed read as an empty file. */
async function readExisting($: EngineInterface, path: string): Promise<string | null> {
  if (!(await $.fs.exists(path))) return null
  try {
    return await $.fs.read(path)
  } catch (err) {
    throw new Error(`cannot read ${path} (${String(err instanceof Error ? err.message : err).slice(0, 60)}) — not overwriting it`)
  }
}

/** Temp file in the same dir, then `mv -f` (a rename): a crash never leaves a truncated file. $.fs has no rename.
 *  ponytail: read-merge-write of the two global files is not locked; two wizards finishing in the same
 *  few milliseconds can lose one of them. Add a lock via mk-state if that ever happens. */
async function writeAtomic($: EngineInterface, path: string, text: string): Promise<void> {
  const tmp = `${path}.tmp`
  await $.fs.write(tmp, text)
  const mv = await $.process.run(['mv', '-f', tmp, path], { timeoutMs: PROBE_MS })
  if (mv.exitCode !== 0) throw new Error(`could not move ${tmp} into place: ${mv.stderr.trim().slice(0, 100)}`)
}

async function writeAll($: EngineInterface): Promise<void> {
  const d = await read($, draftAtom)
  try {
    const data = dataDirOf(d)
    if (d.repo == null || data == null || d.home == null || d.globalDir == null) throw new Error('no git repository (or HOME) detected — run /mk setup from inside the project')
    const yamlPath = await yamlPathOf($, d, data)
    const yamlName = yamlPath.split('/').pop() ?? 'project.yaml'
    // detection is a snapshot: a yaml that appeared since goes back to the replace confirm, nothing is written
    const yamlExists = await $.fs.exists(yamlPath)
    if (yamlExists && !d.existingYaml) {
      await patch($, { existingYaml: true, replace: false, phase: 'error', error: `${yamlName} appeared since detection — choose keep or replace, then write again` })
      return
    }
    // read everything we merge into BEFORE the first write: an unreadable file aborts with nothing changed
    const gitignorePath = join(d.repo, '.gitignore')
    const wantGitignore = d.dataMode === 'repo' && d.dataEnv == null && d.gitignore
    const gitignoreNext = wantGitignore ? addToGitignore(await readExisting($, gitignorePath)) : null
    const globalDir = d.globalDir
    const at = new Date(await $.clock.now()).toISOString()
    const written: string[] = []
    const mk = await $.process.run(['mkdir', '-p', join(data, 'plans'), join(data, 'runs'), globalDir], { timeoutMs: PROBE_MS })
    if (mk.exitCode !== 0) throw new Error(`mkdir failed: ${mk.stderr.trim().slice(0, 120)}`)
    if (!yamlExists || d.replace) {
      if (yamlExists) {
        const bak = await $.process.run(['cp', '-f', yamlPath, `${yamlPath}.bak`], { timeoutMs: PROBE_MS })
        if (bak.exitCode !== 0) throw new Error(`could not back up ${yamlPath}: ${bak.stderr.trim().slice(0, 100)}`)
      }
      // a replace carries the old file's session/picks/reviewers; readExisting throws on an unreadable file, so nothing is written
      await writeAtomic($, yamlPath, buildYaml(d, at, yamlExists ? await readExisting($, yamlPath) : null))
      written.push(yamlPath)
    }
    if (gitignoreNext != null) {
      await writeAtomic($, gitignorePath, gitignoreNext)
      written.push(gitignorePath)
    }
    // L10.7: the root projects table is the registry; MK_HOME points the script at the wizard's home
    const reg = await $.process.run(
      ['python3', join(d.pluginRoot, 'scripts', 'mk-state.py'), 'project', 'register', '--slug', d.slug, '--repo', d.repo, '--data', data],
      { cwd: d.repo, env: { MK_HOME: globalDir, MATRIXKODER_HOME: globalDir }, timeoutMs: PROBE_MS })
    if (reg.exitCode !== 0) throw new Error(`could not register the project: ${(reg.stdout || reg.stderr).trim().slice(0, 120)}`)
    written.push(`${globalDir} root projects table`)
    // B-3: the project store is created through the script that owns it (`sync` opens mk-project.db via
    // mkdb.open_project and heals the root plan summary), never by writing SQL here. Registration goes first,
    // so the row the store is opened under already exists.
    const store = await $.process.run(
      ['python3', join(d.pluginRoot, 'scripts', 'mk-state.py'), 'sync', '--project', d.slug, '--data', data],
      { cwd: d.repo, env: { MK_HOME: globalDir, MATRIXKODER_HOME: globalDir }, timeoutMs: PROBE_MS })
    if (store.exitCode !== 0) throw new Error(`could not create the project store: ${(store.stdout || store.stderr).trim().slice(0, 120)}`)
    written.push(`${data} project store`)
    // B-5: the four dashboard prefs are written here with everything else, not at their own button on step 9.
    // A refusal is named on the post-write line and never undoes the files already on disk.
    const prefsError = await applyPrefs($)
    let herdrLinkResult = d.herdrLinkResult
    if (d.herdrPlugin === 'link') {
      try {
        const link = await $.process.run(['herdr', 'plugin', 'link', `${d.pluginRoot}/herdr`], { timeoutMs: PROBE_MS })
        herdrLinkResult = { ok: link.exitCode === 0, text: link.exitCode === 0 ? 'herdr plugin linked' : `herdr plugin link failed: ${link.stderr.trim().slice(0, 120)}` }
      } catch (err) {
        herdrLinkResult = { ok: false, text: `herdr plugin link failed: ${String(err instanceof Error ? err.message : err).slice(0, 120)}` }
      }
    }
    await patch($, { phase: 'written', written, herdrLinkResult, prefsSaved: prefsError == null, error: prefsError })
    // B-4: keep means keep. `mk-config migrate` REWRITES the yaml (it drops the now-dead `dashboard:`/
    // `poll_min` copies and appends the session/picks/routing blocks), so running it after the user chose
    // keep edited the very file they asked to leave alone. It now runs only when this write made the file.
    const wroteYaml = written.includes(yamlPath)
    if (!wroteYaml) return
    // best-effort like the herdr link: a migration failure warns, never fails the wizard. Both warnings
    // are kept: the prefs refusal and the skipped migration each get their words on the one line.
    let warnings = prefsError == null ? [] : [prefsError]
    try {
      const mig = await $.process.run(['python3', join(d.pluginRoot, 'scripts', 'mk-config.py'), 'migrate', '--data', data], { timeoutMs: PROBE_MS })
      if (mig.exitCode !== 0) warnings = [...warnings, `config migration skipped: ${mig.stderr.trim().slice(0, 120)}`]
    } catch (err) {
      warnings = [...warnings, `config migration skipped: ${String(err instanceof Error ? err.message : err).slice(0, 120)}`]
    }
    await patch($, { error: warnings.length === 0 ? null : warnings.join(' · ') })
  } catch (err) {
    await patch($, { phase: 'error', error: String(err instanceof Error ? err.message : err).slice(0, 200) })
  }
}

/** Closes the pane and DROPS the draft (B-12): Cancel and Close mean "start over", so the next
 *  `/mk setup` never resumes a half-filled set of answers the user walked away from. */
async function closeWizard($: EngineInterface): Promise<void> {
  try {
    await $.ui.close({ id: WIZARD_ID })
  } catch {
    // already closed
  }
  await update($, draftAtom, () => emptyDraft())
  await update($, stepAtom, () => 1)
}

/** The dashboard prefs' one home is the plugin's userConfig, so $.config.set, not a file. B-5 moved the
 *  call from its own step-9 button to the step-10 write; it answers the failure and lets the caller decide
 *  where the line goes, so a refused pref never wipes an error the write already set. */
async function applyPrefs($: EngineInterface): Promise<string | null> {
  const d = await read($, draftAtom)
  const poll = Number.parseInt(d.pollMin.trim(), 10)
  const values: Array<[string, boolean | number]> = [
    ['autoOpen', d.autoOpen],
    ['chip', d.chip],
    ['toasts', d.toasts],
    ['usagePollMin', Number.isFinite(poll) && poll >= 1 ? poll : 5],
  ]
  try {
    for (const [key, value] of values) {
      const res = await $.config.set({ key, value })
      if (res.deny != null) throw new Error(`could not set ${key}: ${res.deny}`)
    }
    return null
  } catch (err) {
    return String(err instanceof Error ? err.message : err).slice(0, 200)
  }
}

// ---------- steps ----------

function Body(els: Elements, $: EngineInterface, d: MkWizardDraft, step: number, options: PluginOptions, oldYaml: string | null, checkedAt: string) {
  const act: WizardAct = {
    patch: p => patch($, p),
    refresh: p => refreshExisting($, p),
    write: () => writeAll($),
    applyPrefs: async () => { await applyPrefs($) },
  }
  return step <= 5 ? StepsA(els, d, act, step) : StepsB(els, d, act, step, options, oldYaml, checkedAt)
}

/** Steps 5-9 hold text Inputs: a letter or digit hotkey could swallow typed characters ("pnpm" -> Next), so
 *  those steps draw no hotkeys at all. Next holds the initial focus (Enter); Tab walks to the rest.
 *  Exception: step 5 with nothing detected gives it to the first gate Input, so the step is typeable. */
const INPUT_STEPS = new Set([5, 6, 7, 8, 9])

function Nav(els: Elements, $: EngineInterface, d: MkWizardDraft, step: number) {
  const { Box, Button } = els
  const done = d.phase === 'written'
  const hk = (key: string): { hotkey: string } | Record<string, never> => (INPUT_STEPS.has(step) ? {} : { hotkey: key })
  const lab = (key: string, text: string): string => (INPUT_STEPS.has(step) ? text : `${key} ${text}`)
  const gatesEmpty = step === 5 && d.gatesFrom === ''
  const writeNow = step === LAST_STEP && d.phase !== 'written'
  return (
    <Box flexDirection="row" gap={1}>
      {done
        ? <Button key="wz-close" variant="primary" hotkey="x" autoFocus={true} label="x Close" onPress={() => closeWizard($)} />
        : [
          step > 1 && <Button key="wz-back" {...hk('b')} label={lab('b', '← Back')} onPress={() => goto($, step - 1)} />,
          // the slides put `w Write these` in the nav row on step 10, between Back and Cancel
          // `lab` adds the hotkey for a step with no Input; the label here is the slide's `w Write these`
          writeNow && <Button key="wz-write" variant="primary" hotkey="w" label={lab('w', 'Write these')} onPress={() => writeAll($)} />,
          step < LAST_STEP && <Button key="wz-next" variant="primary" {...hk('n')} autoFocus={gatesEmpty ? undefined : true} label={lab('n', 'Next →')} onPress={() => goto($, step + 1)} />,
          <Button key="wz-cancel" role="dismiss" {...hk('c')} label={lab('c', 'Cancel')} onPress={() => closeWizard($)} />,
        ]}
    </Box>
  )
}
