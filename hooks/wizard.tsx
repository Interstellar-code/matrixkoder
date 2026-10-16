// MatrixKoder onboarding wizard (plan §7): a 10-step Pane (id matrixkoder-setup)
// driven by two $.state atoms — wizardStep and wizardDraft. Detection is
// read-only and runs once when /mk setup opens the pane; NOTHING is written
// until the review step's single confirm. The pure halves (parsers, gate
// proposals, YAML emitter) are in wizard-core.ts. API keys are never asked for
// or stored: step 4 only prints the command the user runs themselves.
import { atom, read, update } from 'claude-code'
import type { Elements as ElementTables, EngineInterface, On, PluginOptions } from 'claude-code'
import type { MkWizardDraft } from '../types'
import { globalDirPath, join, parseJson } from './data'
import {
  AGENT_NAMES, WIZARD_STEPS, ZAI_KEY_CMD, addToGitignore, buildYaml,
  builderOrderOf, dataDirOf, EMPTY_DRAFT, emptyDraft, fallbackText, installModeOf, mergeModels, parseModels, parseProviders, parseVersion,
  parseWhich, proposeGates, providerChoices, providerLabel, slugOf,
} from './wizard-core'

/** Surfaces that draw Input and Select (mobile does not). */
type Elements = ElementTables['terminal'] | ElementTables['desktop'] | ElementTables['vscode']

export const WIZARD_ID = 'matrixkoder-setup'
const COLUMNS = 64
const PROBE_MS = 5000
const PREVIEW_LINES = 40
const LAST_STEP = WIZARD_STEPS.length
const toggleable: readonly string[] = AGENT_NAMES.filter(n => n !== 'claude')

// atoms are file-local consts (the scan reads this file's own consts)
const stepAtom = atom({ plugin: 'matrixkoder', key: 'wizardStep' } as const, 1)
const draftAtom = atom({ plugin: 'matrixkoder', key: 'wizardDraft' } as const, EMPTY_DRAFT)

type StrField = 'maxLanes' | 'claudeFiveHour' | 'claudeWeekly' | 'branch' | 'preauthorised' | 'qaStart' | 'qaDevUrl' | 'qaThemes' | 'qaMockups' | 'pollMin'
type BoolField = 'gitignore' | 'skipPermissions' | 'qaFrontend' | 'autoOpen' | 'chip' | 'toasts' | 'replace'
type GateField = 'install' | 'test' | 'typecheck' | 'lint_changed' | 'format_changed'

const YES_NO = [{ value: 'yes', label: 'yes' }, { value: 'no', label: 'no' }] as const

export function registerWizard(on: On, options: PluginOptions): void {
  // /mk setup | init: dashboard.tsx hands these down with next(e); anything else goes back down the chain
  on('command.run', { command: 'mk' }, async ($, e, next) => {
    const arg = e.args.trim().split(/\s+/)[0]?.toLowerCase() ?? ''
    if (arg !== 'setup' && arg !== 'init') return next(e)
    return { text: await openWizard($, e.presentation, options) }
  })

  on('ui.render', { component: 'Pane', requestId: WIZARD_ID }, async ($, e, next) => {
    if (e.surface === 'mobile') return next(e) // no Input/Select there
    const els = $.ui.resolve(e)
    const { Box, Text } = els
    const step = await read($, stepAtom)
    const d = await read($, draftAtom)
    const cols = e.props.bodyColumns || COLUMNS
    const title = WIZARD_STEPS[step - 1] ?? ''
    // the review preview is exactly what a replace writes: read the yaml the carry-over needs (readOpt: null on any failure)
    const data = dataDirOf(d)
    const oldYaml = step === LAST_STEP && d.existingYaml && d.replace && data != null ? await readOpt($, join(data, 'matrixkoder.yaml')) : null
    return (
      <Box flexDirection="column" width={cols} gap={1}>
        {e.props.placement === 'inline' && (
          <Box borderStyle="round" borderColor="yellow" paddingX={1}>
            <Text color="yellow">Run /tui fullscreen to dock this panel on the right.</Text>
          </Box>
        )}
        <Box flexDirection="column">
          <Text color="cyan" bold={true}>⬡ MatrixKoder setup</Text>
          <Text dimColor={true}>{`Step ${step}/${LAST_STEP} · ${title}   ${WIZARD_STEPS.map((_, i) => (i + 1 < step ? '●' : i + 1 === step ? '◉' : '○')).join('')}`}</Text>
        </Box>
        {!d.loaded ? <Text dimColor={true}>Detecting agents, usage and gates…</Text> : Body(els, $, d, step, options, oldYaml)}
        {d.loaded && Nav(els, $, d, step)}
      </Box>
    )
  })
}

/** Opens the wizard pane (fullscreen) or answers with the detected defaults as text. Never throws. */
async function openWizard($: EngineInterface, presentation: { isFullscreen: boolean } | undefined, options: PluginOptions): Promise<string> {
  // the engine stamps presentation on every command.run; the test harness leaves it off (treated as fullscreen)
  if (presentation?.isFullscreen === false) {
    return fallbackText(await detect($, options))
  }
  await update($, stepAtom, () => 1)
  await update($, draftAtom, () => emptyDraft())
  try {
    await $.ui.open({ id: WIZARD_ID, title: 'MatrixKoder setup', columns: COLUMNS, focus: true })
  } catch {
    return 'Could not open the MatrixKoder setup pane. Run /tui fullscreen, then /mk setup again.'
  }
  const draft = await detect($, options)
  await update($, draftAtom, () => draft)
  return 'MatrixKoder setup opened — every step is pre-filled; nothing is written until the final confirm.'
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
    const repoData = await $.fs.exists(join(d.repo, '.matrixkoder')).catch(() => false)
    const established = await $.fs.exists(join(d.repo, 'package.json')).catch(() => false)
      || await $.fs.exists(join(d.repo, 'pyproject.toml')).catch(() => false)
      || await $.fs.exists(join(d.repo, 'Makefile')).catch(() => false)
    if (!repoData && !established) d.dataMode = 'home'
  }
  const data = dataDirOf(d)
  d.existingYaml = data != null && (await $.fs.exists(join(data, 'matrixkoder.yaml')).catch(() => false))
  d.loaded = true
  return d
}

// ---------- state helpers ----------

const patch = ($: EngineInterface, p: Partial<MkWizardDraft>): Promise<unknown> => update($, draftAtom, d => ({ ...d, ...p }))
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

async function refreshExisting($: EngineInterface, p: Partial<MkWizardDraft>): Promise<void> {
  const next = { ...(await read($, draftAtom)), ...p }
  const data = dataDirOf(next)
  const existing = data != null && (await $.fs.exists(join(data, 'matrixkoder.yaml')).catch(() => false))
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
    const yamlPath = join(data, 'matrixkoder.yaml')
    // detection is a snapshot: a yaml that appeared since goes back to the replace confirm, nothing is written
    const yamlExists = await $.fs.exists(yamlPath)
    if (yamlExists && !d.existingYaml) {
      await patch($, { existingYaml: true, replace: false, phase: 'error', error: 'matrixkoder.yaml appeared since detection — choose keep or replace, then write again' })
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
    let herdrLinkResult = d.herdrLinkResult
    if (d.herdrPlugin === 'link') {
      try {
        const link = await $.process.run(['herdr', 'plugin', 'link', `${d.pluginRoot}/herdr`], { timeoutMs: PROBE_MS })
        herdrLinkResult = { ok: link.exitCode === 0, text: link.exitCode === 0 ? 'herdr plugin linked' : `herdr plugin link failed: ${link.stderr.trim().slice(0, 120)}` }
      } catch (err) {
        herdrLinkResult = { ok: false, text: `herdr plugin link failed: ${String(err instanceof Error ? err.message : err).slice(0, 120)}` }
      }
    }
    await patch($, { phase: 'written', written, error: null, herdrLinkResult })
    // best-effort like the herdr link: a migration failure warns, never fails the wizard
    try {
      const mig = await $.process.run(['python3', join(d.pluginRoot, 'scripts', 'mk-config.py'), 'migrate', '--data', data], { timeoutMs: PROBE_MS })
      if (mig.exitCode !== 0) {
        await patch($, { error: `config migration skipped: ${mig.stderr.trim().slice(0, 120)}` })
      }
    } catch (err) {
      await patch($, { error: `config migration skipped: ${String(err instanceof Error ? err.message : err).slice(0, 120)}` })
    }
  } catch (err) {
    await patch($, { phase: 'error', error: String(err instanceof Error ? err.message : err).slice(0, 200) })
  }
}

async function closeWizard($: EngineInterface): Promise<void> {
  try {
    await $.ui.close({ id: WIZARD_ID })
  } catch {
    // already closed
  }
}

/** Step 9's confirm: these prefs' one home is the plugin's userConfig, so $.config.set, not a file. */
async function applyPrefs($: EngineInterface): Promise<void> {
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
    await patch($, { error: null })
  } catch (err) {
    await patch($, { error: String(err instanceof Error ? err.message : err).slice(0, 200) })
  }
}

// ---------- field widgets ----------

function TextField(els: Elements, $: EngineInterface, d: MkWizardDraft, field: StrField, label: string, placeholder = '') {
  const { Input } = els
  const set = (v: string): Promise<unknown> => update($, draftAtom, cur => ({ ...cur, [field]: v }))
  return <Input key={`wz-${field}`} label={label} value={d[field]} placeholder={placeholder} onInput={v => set(v)} onSubmit={v => set(v)} />
}

function GateField(els: Elements, $: EngineInterface, d: MkWizardDraft, field: GateField, label: string, autoFocus = false) {
  const { Input } = els
  const set = (v: string): Promise<unknown> => update($, draftAtom, cur => ({ ...cur, gates: { ...cur.gates, [field]: v } }))
  return <Input key={`wz-gate-${field}`} label={label} value={d.gates[field]} placeholder="(none)" autoFocus={autoFocus || undefined} onInput={v => set(v)} onSubmit={v => set(v)} />
}

function YesNo(els: Elements, $: EngineInterface, d: MkWizardDraft, field: BoolField, label: string) {
  const { Select } = els
  return (
    <Select
      key={`wz-${field}`} label={label} options={YES_NO} value={d[field] ? 'yes' : 'no'}
      onSelect={v => update($, draftAtom, cur => ({ ...cur, [field]: v === 'yes' }))}
    />
  )
}

function Choice(els: Elements, $: EngineInterface, d: MkWizardDraft, field: StrField, label: string, values: readonly string[]) {
  const { Select } = els
  return (
    <Select
      key={`wz-${field}`} label={label} options={values.map(v => ({ value: v }))} value={d[field]}
      onSelect={v => update($, draftAtom, cur => ({ ...cur, [field]: v }))}
    />
  )
}

function HerdrLinkChoice(els: Elements, $: EngineInterface, d: MkWizardDraft) {
  const { Box, Text, Select } = els
  const a = d.agents.herdr
  const label = 'Link the MatrixKoder herdr plugin (herdr pushes lane events instead of polling)'
  const options = YES_NO.map(o => ({ value: o.value === 'yes' ? 'link' : 'skip', label: o.label }))
  if (!a?.found) return <Text dimColor={true}>herdr not found on PATH</Text>
  if (d.herdrPlugin === 'linked') return <Text color="green">✓ MatrixKoder herdr plugin is linked</Text>
  return (
    <Box flexDirection="column">
      <Select
        key="wz-herdrPlugin" label={label} options={options} value={d.herdrPlugin}
        onSelect={v => patch($, { herdrPlugin: v as 'link' | 'skip' })}
      />
    </Box>
  )
}

// ---------- steps ----------

function Body(els: Elements, $: EngineInterface, d: MkWizardDraft, step: number, options: PluginOptions, oldYaml: string | null) {
  const { Box, Text, Button, Select } = els
  switch (step) {
    case 1:
      return (
        <Box flexDirection="column">
          <Text>Install mode: <Text bold={true}>{d.installMode}</Text>{d.installMode === 'dev' ? ' (--plugin-dir)' : ' (marketplace)'}</Text>
          <Text dimColor={true}>{d.pluginRoot || 'plugin root unknown'}</Text>
          <Text dimColor={true}>Info only — nothing is stored.</Text>
        </Box>
      )
    case 2: {
      const dir = dataDirOf(d)
      return (
        <Box flexDirection="column" gap={1}>
          <Text>Repo: <Text bold={true}>{d.repo ?? 'not a git repository'}</Text></Text>
          {d.dataEnv != null
            ? <Text color="yellow">{`$MATRIXKODER_DATA is set: data goes to ${d.dataEnv}`}</Text>
            : (
              <Select
                key="wz-datamode" label="Data location" value={d.dataMode}
                options={[{ value: 'repo', label: '<repo>/.matrixkoder/ (default)' }, { value: 'home', label: `${d.globalDir ?? '~/.matrixkoder'}/projects/${d.slug}/` }]}
                onSelect={v => refreshExisting($, { dataMode: v === 'home' ? 'home' : 'repo' })}
              />
            )}
          {d.dataEnv == null && d.dataMode === 'repo' && (d.gitignoreHas
            ? <Text dimColor={true}>.gitignore already ignores /.matrixkoder/</Text>
            : YesNo(els, $, d, 'gitignore', 'Add /.matrixkoder/ to .gitignore'))}
          <Text dimColor={true}>Data dir: {dir ?? '—'}</Text>
        </Box>
      )
    }
    case 3:
      return (
        <Box flexDirection="column">
          <Text bold={true}>Agents (tick to enable)</Text>
          {AGENT_NAMES.map(n => {
            const a = d.agents[n]
            const digit = String(toggleable.indexOf(n) + 1) // hotkey 1..4
            if (a == null || !a.found) return <Text key={n} dimColor={true}>{`[ ] ${n}  not found`}</Text>
            if (n === 'claude') return <Text key={n}>{`[x] ${n}  ${a.path ?? ''}`}</Text>
            return (
              <Box key={n} flexDirection="column">
                <Button
                  key={`wz-agent-${n}`} hotkey={digit} label={`${digit} ${a.enabled ? '[x]' : '[ ]'} ${n}  ${a.path ?? ''}`}
                  onPress={() => update($, draftAtom, cur => ({ ...cur, agents: { ...cur.agents, [n]: { ...a, enabled: !a.enabled } } }))}
                />
                {a.models.length > 0 && <Text dimColor={true}>{`    ${a.models.length} models: ${a.models.slice(0, 3).join(', ')}${a.models.length > 3 ? ', …' : ''}`}</Text>}
              </Box>
            )
          })}
          {d.agents.herdr?.found && <Box flexDirection="column" marginTop={1}>{HerdrLinkChoice(els, $, d)}</Box>}
        </Box>
      )
    case 4:
      return (
        <Box flexDirection="column" gap={1}>
          {d.codexbarVersion == null
            ? (
              <Box flexDirection="column">
                <Text color="yellow">⚠ codexbar not found on PATH</Text>
                <Text dimColor={true}>Install CodexBar (github.com/steipete/CodexBar) and put its CLI on PATH; usage stays n/a until then. You can continue.</Text>
              </Box>
            )
            : <Text color="green">{`✓ codexbar ${d.codexbarVersion}`}</Text>}
          {d.codexbarVersion != null && (
            <Box flexDirection="column">
              <Text bold={true}>Providers (tick to track)</Text>
              {d.providersAll.map((p, i) => (
                <Button
                  key={`wz-prov-${p}`} {...(i < 9 ? { hotkey: String(i + 1) } : {})} label={`${i < 9 ? `${i + 1} ` : ''}${d.providersOn.includes(p) ? '[x]' : '[ ]'} ${providerLabel(p)}`}
                  onPress={() => update($, draftAtom, cur => ({ ...cur, providersOn: cur.providersOn.includes(p) ? cur.providersOn.filter(x => x !== p) : [...cur.providersOn, p] }))}
                />
              ))}
            </Box>
          )}
          <Box flexDirection="column">
            <Text bold={true}>z.ai key (OpenCode plan)</Text>
            <Text dimColor={true}>The wizard never asks for or stores API keys. In your own terminal run:</Text>
            <Text>{ZAI_KEY_CMD}</Text>
          </Box>
        </Box>
      )
    case 5: {
      // nothing detected: the first Input takes the initial focus so the step is typeable at once
      const gatesEmpty = d.gatesFrom === ''
      return (
        <Box flexDirection="column">
          <Text dimColor={true}>{d.gatesFrom !== '' ? `Proposed from ${d.gatesFrom}. Test suites are not run.` : 'Nothing detected — Tab to a field and type the command you use.'}</Text>
          {GateField(els, $, d, 'install', 'install', gatesEmpty)}
          {GateField(els, $, d, 'test', 'test')}
          {GateField(els, $, d, 'typecheck', 'typecheck')}
          {GateField(els, $, d, 'lint_changed', 'lint_changed')}
          {GateField(els, $, d, 'format_changed', 'format_changed')}
          <Text dimColor={true}>Baselines (type errors, lint errors, failing tests): measured at first run.</Text>
          {d.banned.length > 0 && <Text dimColor={true}>{`Banned (dev servers): ${d.banned.join(', ')}`}</Text>}
        </Box>
      )
    }
    case 6:
      return (
        Routing(els, $, d)
      )
    case 7:
      return (
        <Box flexDirection="column">
          {YesNo(els, $, d, 'skipPermissions', 'Builders skip permission prompts (own worktree only)')}
          {TextField(els, $, d, 'preauthorised', 'Pre-authorised')}
        </Box>
      )
    case 8:
      return (
        <Box flexDirection="column">
          {YesNo(els, $, d, 'qaFrontend', 'Has a frontend')}
          {TextField(els, $, d, 'qaDevUrl', 'Dev URL', 'https://localhost:3000')}
          {TextField(els, $, d, 'qaStart', 'Start command (orchestrator only)')}
          {TextField(els, $, d, 'qaThemes', 'Themes (comma separated)')}
          {TextField(els, $, d, 'qaMockups', 'Mockups dir')}
          <Select
            key="wz-qaTarget" label="QA integration target" value={d.qaTarget}
            options={[{ value: 'local-main', label: 'local-main (QA on the running dev server)' }, { value: 'branch', label: 'branch (separate build)' }]}
            onSelect={v => patch($, { qaTarget: v === 'branch' ? 'branch' : 'local-main' })}
          />
        </Box>
      )
    case 9:
      return (
        <Box flexDirection="column">
          {YesNo(els, $, d, 'autoOpen', 'Auto-open the dashboard (fullscreen, ≥144 cols)')}
          {YesNo(els, $, d, 'chip', 'Status chip')}
          {YesNo(els, $, d, 'toasts', 'Toasts')}
          {TextField(els, $, d, 'pollMin', 'Usage poll (minutes)')}
          <Text dimColor={true}>{`Plugin settings today: autoOpen ${options.autoOpen !== false ? 'on' : 'off'}, chip ${options.chip !== false ? 'on' : 'off'}, toasts ${options.toasts !== false ? 'on' : 'off'}.`}</Text>
          <Text dimColor={true}>These live in the plugin's own settings (claude plugin configure matrixkoder), not in the yaml.</Text>
          {d.error != null && <Text color="red">{`✗ ${d.error}`}</Text>}
          <Button key="wz-dash-apply" variant="primary" label="Apply these settings" onPress={() => void applyPrefs($)} />
        </Box>
      )
    default:
      return Review(els, $, d, oldYaml)
  }
}

function Routing(els: Elements, $: EngineInterface, d: MkWizardDraft) {
  const { Box, Input } = els
  const setOrder = (v: string): Promise<unknown> => update($, draftAtom, cur => ({ ...cur, builderOrder: v }))
  return (
    <Box flexDirection="column">
      <Input key="wz-builderOrder" label="Builder order" value={builderOrderOf(d).join(', ')} onInput={v => setOrder(v)} onSubmit={v => setOrder(v)} />
      {Choice(els, $, d, 'maxLanes', 'Max build lanes', ['1', '2', '3', '4', '5'])}
      {Choice(els, $, d, 'claudeFiveHour', 'Claude reserve floor, 5h left %', ['20', '40', '60'])}
      {Choice(els, $, d, 'claudeWeekly', 'Claude reserve floor, weekly left %', ['20', '30', '50'])}
      {TextField(els, $, d, 'branch', 'Integration branch')}
    </Box>
  )
}

function Review(els: Elements, $: EngineInterface, d: MkWizardDraft, oldYaml: string | null) {
  const { Box, Text, Button, Select } = els
  const data = dataDirOf(d)
  if (d.phase === 'written') {
    return (
      <Box flexDirection="column">
        <Text color="green" bold={true}>✓ MatrixKoder is set up</Text>
        {d.written.map(p => <Text key={p} dimColor={true}>{`wrote ${p}`}</Text>)}
        {d.existingYaml && !d.replace && <Text dimColor={true}>kept existing matrixkoder.yaml</Text>}
        {d.herdrLinkResult && (
          <Text color={d.herdrLinkResult.ok ? 'green' : 'yellow'}>
            {`${d.herdrLinkResult.ok ? '✓' : '⚠'} ${d.herdrLinkResult.text}`}
          </Text>
        )}
        {d.installMode === 'marketplace' && (
          <Text dimColor={true}>Re-run /mk setup after each MatrixKoder update: the herdr link points at this plugin version's folder.</Text>
        )}
        {d.error != null && <Text color="yellow">{`⚠ ${d.error}`}</Text>}
        <Text>Next: /mk to open the dashboard.</Text>
      </Box>
    )
  }
  const yaml = buildYaml(d, 'pending', d.existingYaml && d.replace ? oldYaml : null).split('\n')
  return (
    <Box flexDirection="column" gap={1}>
      {d.phase === 'error' && <Text color="red" bold={true}>{`✗ ${d.error ?? 'write failed'}`}</Text>}
      <Box flexDirection="column">
        <Text bold={true}>Will write</Text>
        <Text>{`${d.existingYaml && !d.replace ? 'keep   ' : d.existingYaml ? 'replace' : 'create '} ${data ?? '?'}/matrixkoder.yaml`}</Text>
        <Text>{`create  ${data ?? '?'}/plans/ and runs/`}</Text>
        {d.dataEnv == null && d.dataMode === 'repo' && d.gitignore && !d.gitignoreHas && <Text>add     /.matrixkoder/ to .gitignore</Text>}
        <Text>{`${d.globalDir ?? '~/.matrixkoder'}root projects table (register this repo)`}</Text>
        {d.agents.herdr?.found && d.herdrPlugin === 'link' && <Text>{`link    ${d.pluginRoot}/herdr in herdr`}</Text>}
        {d.agents.herdr?.found && d.herdrPlugin === 'linked' && <Text dimColor={true}>herdr plugin already linked</Text>}
        {d.installMode === 'marketplace' && <Text dimColor={true}>Re-run /mk setup after each MatrixKoder update: the herdr link points at this plugin version's folder.</Text>}
      </Box>
      {d.existingYaml && (
        <Select
          key="wz-replace" label="matrixkoder.yaml already exists" value={d.replace ? 'replace' : 'keep'}
          options={[{ value: 'keep', label: 'keep it (default)' }, { value: 'replace', label: 'replace it' }]}
          onSelect={v => patch($, { replace: v === 'replace' })}
        />
      )}
      <Box flexDirection="column">
        <Text bold={true}>matrixkoder.yaml preview</Text>
        {yaml.slice(0, PREVIEW_LINES).map((l, i) => <Text key={`y${i}`} dimColor={true}>{l}</Text>)}
        {yaml.length > PREVIEW_LINES && <Text dimColor={true}>{`… ${yaml.length - PREVIEW_LINES} more lines`}</Text>}
      </Box>
      <Button key="wz-write" variant="primary" hotkey="w" label="w Write these" onPress={() => writeAll($)} />
    </Box>
  )
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
  return (
    <Box flexDirection="row" gap={1}>
      {done
        ? <Button key="wz-close" variant="primary" hotkey="x" autoFocus={true} label="x Close" onPress={() => closeWizard($)} />
        : [
          step > 1 && <Button key="wz-back" {...hk('b')} label={lab('b', '← Back')} onPress={() => goto($, step - 1)} />,
          step < LAST_STEP && <Button key="wz-next" variant="primary" {...hk('n')} autoFocus={gatesEmpty ? undefined : true} label={lab('n', 'Next →')} onPress={() => goto($, step + 1)} />,
          <Button key="wz-cancel" role="dismiss" {...hk('c')} label={lab('c', 'Cancel')} onPress={() => closeWizard($)} />,
        ]}
    </Box>
  )
}
