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
import { paths as mkPaths } from './paths'
import {
  AGENT_NAMES, WIZARD_STEPS, addToGitignore, buildYaml, dataDirOf, EMPTY_DRAFT, emptyDraft, fallbackText, installModeOf, mergeModels, parseModels, parseProviders, parseVersion,
  parseWhich, proposeGates, providerChoices, slugOf,
} from './wizard-core'
import { StepsA, type WizardAct } from './wizard-steps-a'
import { StepsB } from './wizard-steps-b'
import { PALETTE, ROLE } from './tabs/palette'

/** Surfaces that draw Input and Select (mobile does not). */
type Elements = ElementTables['terminal'] | ElementTables['desktop'] | ElementTables['vscode']

export const WIZARD_ID = 'matrixkoder-setup'
const COLUMNS = 64
const PROBE_MS = 5000
const LAST_STEP = WIZARD_STEPS.length

// atoms are file-local consts (the scan reads this file's own consts)
const stepAtom = atom({ plugin: 'matrixkoder', key: 'wizardStep' } as const, 1)
const draftAtom = atom({ plugin: 'matrixkoder', key: 'wizardDraft' } as const, EMPTY_DRAFT)

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
    const oldYaml = step === LAST_STEP && d.existingYaml && d.replace && data != null ? await readOpt($, mkPaths.projectYaml(data)) : null
    return (
      <Box flexDirection="column" width={cols} gap={1}>
        {e.props.placement === 'inline' && (
          <Box borderStyle="round" borderColor="yellow" paddingX={1}>
            <Text color={ROLE.warn.fg}>Run /tui fullscreen to dock this panel on the right.</Text>
          </Box>
        )}
        <Box flexDirection="column">
          <Text color={PALETTE.blue} bold={true}>⬡ MatrixKoder setup</Text>
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
    const repoData = await $.fs.exists(mkPaths.legacyDataDir(d.repo)).catch(() => false)
    const established = await $.fs.exists(join(d.repo, 'package.json')).catch(() => false)
      || await $.fs.exists(join(d.repo, 'pyproject.toml')).catch(() => false)
      || await $.fs.exists(join(d.repo, 'Makefile')).catch(() => false)
    if (!repoData && !established) d.dataMode = 'home'
  }
  const data = dataDirOf(d)
  d.existingYaml = data != null && (await $.fs.exists(mkPaths.projectYaml(data)).catch(() => false))
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

async function refreshExisting($: EngineInterface, p: Partial<MkWizardDraft>): Promise<void> {
  const next = { ...(await read($, draftAtom)), ...p }
  const data = dataDirOf(next)
  const existing = data != null && (await $.fs.exists(mkPaths.projectYaml(data)).catch(() => false))
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
    const yamlPath = mkPaths.projectYaml(data)
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

// ---------- steps ----------

function Body(els: Elements, $: EngineInterface, d: MkWizardDraft, step: number, options: PluginOptions, oldYaml: string | null) {
  const act: WizardAct = {
    patch: p => patch($, p),
    refresh: p => refreshExisting($, p),
    write: () => writeAll($),
    applyPrefs: () => applyPrefs($),
  }
  return step <= 5 ? StepsA(els, d, act, step) : StepsB(els, d, act, step, options, oldYaml)
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
