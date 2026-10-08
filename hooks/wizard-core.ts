// MatrixKoder onboarding wizard (plan §7): the PURE halves — detection parsers,
// gate proposals, the hand-rolled YAML emitter and the file-content builders.
// No $ in here: wizard.tsx binds these to process/fs/state. Nothing here ever
// handles an API key: the z.ai key is entered by the user into CodexBar.
import type { MkWizardAgent, MkWizardDraft, MkWizardGates } from '../types'
import { basename } from './data'

export const AGENT_NAMES = ['claude', 'codex', 'opencode', 'agy', 'herdr'] as const
export const DEFAULT_BUILDER_ORDER = ['codex', 'opencode', 'agy', 'claude'] as const
/** codexbar lists ~60 providers; only these matter to the pool, plus whatever is already enabled */
export const RELEVANT_PROVIDERS = ['claude', 'codex', 'antigravity', 'zai', 'opencode', 'opencodego', 'ollama'] as const
export const DEFAULT_PROVIDERS = ['claude', 'codex', 'antigravity'] as const
export const WIZARD_STEPS = ['Install', 'Data', 'Agents', 'Usage', 'Gates', 'Routing', 'Autonomy', 'QA', 'Dashboard', 'Review'] as const
export const ZAI_KEY_CMD = `printf '%s' "$Z_AI_API_KEY" | codexbar config set-api-key --provider zai --stdin`
const MODEL_CAP = 12

export function emptyDraft(): MkWizardDraft {
  const agents: Record<string, MkWizardAgent> = {}
  for (const n of AGENT_NAMES) agents[n] = { found: false, path: null, enabled: false, models: [] }
  return {
    loaded: false, repo: null, slug: 'project', home: null, globalDir: null, dataEnv: null, installMode: 'dev', pluginRoot: '', branch: 'main',
    dataMode: 'repo', gitignore: true, gitignoreHas: false,
    agents, codexbarVersion: null, providersAll: [...DEFAULT_PROVIDERS], providersOn: [...DEFAULT_PROVIDERS],
    gates: { install: '', test: '', typecheck: '', lint_changed: '', format_changed: '' }, gatesFrom: '', banned: [],
    builderOrder: null, maxLanes: '3', claudeFiveHour: '40', claudeWeekly: '30',
    preauthorised: 'routing, failover, fix-loops, worktrees, lane-merges, push-non-main, open-pr',
    skipPermissions: true,
    qaFrontend: false, qaStart: '', qaDevUrl: '', qaThemes: '', qaMockups: '', qaTarget: 'local-main',
    autoOpen: true, chip: true, toasts: true, pollMin: '5',
    existingYaml: false, replace: false, phase: 'editing', written: [], error: null,
    herdrPlugin: 'skip', herdrLinkResult: null,
  }
}

export const EMPTY_DRAFT: MkWizardDraft = emptyDraft()

// ---------- detection parsers ----------

export function slugOf(path: string): string {
  const s = basename(path).toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '')
  return s === '' ? 'project' : s
}

/** `which a b c` stdout (found ones only, one path per line) → name → path. */
export function parseWhich(stdout: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of stdout.split('\n')) {
    const path = line.trim()
    if (path.startsWith('/')) out[basename(path)] = path
  }
  return out
}

/** `codexbar config providers` lines: `name: enabled|disabled ...`. */
export function parseProviders(stdout: string): { name: string; enabled: boolean }[] {
  const out: { name: string; enabled: boolean }[] = []
  for (const line of stdout.split('\n')) {
    const m = /^([a-z0-9_-]+):\s+(enabled|disabled)\b/.exec(line.trim())
    if (m?.[1] && m[2]) out.push({ name: m[1], enabled: m[2] === 'enabled' })
  }
  return out
}

/** `agy models` / `opencode models`: first whitespace column of each model-looking line. Model lists are parsed uncapped. */
export function parseModels(stdout: string): string[] {
  const out: string[] = []
  for (const line of stdout.split('\n')) {
    const id = line.trim().split(/\s+/)[0] ?? ''
    if (/^[A-Za-z0-9][A-Za-z0-9._/:-]*$/.test(id) && /[-/.]/.test(id)) out.push(id)
  }
  return out
}

/** Detection lists merged, deduped, capped.
 *  `zai-coding-plan/*` models are kept first, then `ollama-cloud/*`, then the rest. */
export function mergeModels(...lists: readonly (readonly string[])[]): string[] {
  const deduped = [...new Set(lists.flat())]
  const plan = deduped.filter(m => m.startsWith('zai-coding-plan/'))
  const ollama = deduped.filter(m => m.startsWith('ollama-cloud/'))
  const rest = deduped.filter(m => !m.startsWith('zai-coding-plan/') && !m.startsWith('ollama-cloud/'))
  const cap = Math.max(MODEL_CAP, plan.length + ollama.length)
  return [...plan, ...ollama, ...rest].slice(0, cap)
}

/** The Usage step's provider label: OpenCode plans are disambiguated; the stored value stays the CodexBar id. */
export function providerLabel(p: string): string {
  if (p === 'zai') return 'zai (OpenCode Z.AI plan)'
  if (p === 'opencode') return 'opencode (OpenCode Zen)'
  if (p === 'ollama') return 'ollama (OpenCode on Ollama Cloud)'
  if (p === 'opencodego') return 'opencodego (OpenCode Go)'
  return p
}

export function parseVersion(stdout: string): string | null {
  const m = /(\d+\.\d+(?:\.\d+)?)/.exec(stdout)
  return m?.[1] ?? null
}

export function providerChoices(listed: readonly { name: string; enabled: boolean }[]): { all: string[]; on: string[] } {
  const relevant = new Set<string>(RELEVANT_PROVIDERS)
  const shown = listed.filter(p => relevant.has(p.name) || p.enabled)
  return { all: shown.map(p => p.name), on: shown.filter(p => p.enabled && relevant.has(p.name)).map(p => p.name) }
}

/** Plugin root → install mode: a marketplace install lives in the plugins cache. */
export function installModeOf(root: string): 'dev' | 'marketplace' {
  return /\/plugins\/(cache|marketplaces)\//.test(root) ? 'marketplace' : 'dev'
}

// ---------- gates ----------

export type GateInputs = {
  pkg: unknown
  pyproject: string | null
  makefile: string | null
  /** lockfile names that exist at the repo root */
  locks: readonly string[]
  hasTsconfig: boolean
  /** other repo marker files that exist (fs.exists checks), e.g. 'Cargo.toml', '.claude-plugin/plugin.json' */
  markers: readonly string[]
}

function rec(v: unknown): Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

type Pm = { run: string; exec: string; install: string }
function pmOf(locks: readonly string[]): Pm {
  if (locks.includes('pnpm-lock.yaml')) return { run: 'pnpm run', exec: 'pnpm exec', install: 'pnpm install --frozen-lockfile --prefer-offline' }
  if (locks.includes('yarn.lock')) return { run: 'yarn', exec: 'yarn', install: 'yarn install --frozen-lockfile' }
  if (locks.includes('bun.lock') || locks.includes('bun.lockb')) return { run: 'bun run', exec: 'bunx', install: 'bun install --frozen-lockfile' }
  return { run: 'npm run', exec: 'npx', install: 'npm ci' }
}

/** Proposes the five gate commands from what the repo declares. Never runs anything. */
export function proposeGates(inp: GateInputs): { gates: MkWizardGates; banned: string[]; from: string } {
  const gates: MkWizardGates = { install: '', test: '', typecheck: '', lint_changed: '', format_changed: '' }
  const banned: string[] = []
  const from: string[] = []
  const pkg = rec(inp.pkg)
  if (Object.keys(pkg).length > 0) {
    from.push('package.json')
    const pm = pmOf(inp.locks)
    const scripts = rec(pkg.scripts)
    const deps = { ...rec(pkg.dependencies), ...rec(pkg.devDependencies) }
    const script = (n: string): string => (typeof scripts[n] === 'string' ? (scripts[n] as string) : '')
    gates.install = pm.install
    if (script('test') !== '' || 'vitest' in deps || 'jest' in deps) {
      gates.test = 'vitest' in deps || /vitest/.test(script('test')) ? `${pm.exec} vitest run {paths}`
        : 'jest' in deps || /jest/.test(script('test')) ? `${pm.exec} jest {paths}`
        : `${pm.run} test`
    }
    const tc = ['typecheck', 'type-check', 'check:types', 'tsc'].find(n => script(n) !== '')
    gates.typecheck = tc ? `${pm.run} ${tc}` : inp.hasTsconfig || 'typescript' in deps ? `${pm.exec} tsc --noEmit` : ''
    if ('eslint' in deps) gates.lint_changed = `${pm.exec} eslint {files}`
    else if ('@biomejs/biome' in deps) gates.lint_changed = `${pm.exec} biome check {files}`
    if ('prettier' in deps) gates.format_changed = `${pm.exec} prettier --write {files}`
    for (const n of ['dev', 'start']) if (script(n) !== '') banned.push(`${pm.run} ${n}`)
  }
  if (inp.pyproject != null) {
    from.push('pyproject.toml')
    const py = inp.pyproject
    if (gates.install === '') gates.install = inp.locks.includes('uv.lock') ? 'uv sync' : inp.locks.includes('poetry.lock') ? 'poetry install' : ''
    if (gates.test === '' && /pytest/.test(py)) gates.test = 'pytest {paths}'
    if (gates.typecheck === '') gates.typecheck = /mypy/.test(py) ? 'mypy .' : /pyright/.test(py) ? 'pyright' : ''
    if (gates.lint_changed === '' && /ruff/.test(py)) gates.lint_changed = 'ruff check {files}'
    if (gates.format_changed === '') gates.format_changed = /ruff/.test(py) ? 'ruff format {files}' : /black/.test(py) ? 'black {files}' : ''
  }
  if (inp.makefile != null) {
    from.push('Makefile')
    const has = (t: string): boolean => new RegExp(`^${t}:`, 'm').test(inp.makefile ?? '')
    if (gates.test === '' && has('test')) gates.test = 'make test'
    if (gates.typecheck === '' && has('typecheck')) gates.typecheck = 'make typecheck'
    if (gates.lint_changed === '' && has('lint')) gates.lint_changed = 'make lint'
    if (gates.format_changed === '') gates.format_changed = has('fmt') ? 'make fmt' : has('format') ? 'make format' : ''
  }
  const hasProjectTest = gates.test !== ''
  // marker files fill what is still empty, so the blocks above win (a Makefile target beats Cargo)
  // detection order: plugin → Cargo → go → selftest
  const marker = (m: string): boolean => inp.markers.includes(m)
  if (marker('.claude-plugin/plugin.json')) {
    from.push('.claude-plugin/plugin.json')
    if (gates.test === '') gates.test = 'claude plugin test .'
    // the plugin schema has no whole-repo validate slot; make lint already ignores {files} the same way
    if (gates.lint_changed === '') gates.lint_changed = 'claude plugin validate .'
  }
  if (marker('Cargo.toml')) {
    from.push('Cargo.toml')
    if (gates.test === '') gates.test = 'cargo test'
    if (gates.lint_changed === '') gates.lint_changed = 'cargo clippy'
    if (gates.format_changed === '') gates.format_changed = 'cargo fmt'
  }
  if (marker('go.mod')) {
    from.push('go.mod')
    if (gates.test === '') gates.test = 'go test ./...'
    if (gates.lint_changed === '') gates.lint_changed = 'go vet ./...'
  }
  if (marker('scripts/selftest.sh') && !hasProjectTest) {
    from.push('scripts/selftest.sh')
    gates.test = gates.test === '' ? 'bash scripts/selftest.sh' : `${gates.test} && bash scripts/selftest.sh`
  }
  if (inp.hasTsconfig && gates.typecheck === '') {
    from.push('tsconfig.json')
    gates.typecheck = 'tsc -p . --noEmit'
  }
  return { gates, banned, from: from.join(' + ') }
}

// ---------- YAML emitter (no library) ----------

const PLAIN = /^[A-Za-z0-9_./~][A-Za-z0-9_./~ =@+-]*$/
const AMBIGUOUS = /^(true|false|yes|no|on|off|null|~)$/i
/** YAML 1.1 plain scalars that read as numbers/dates/floats (0x1F, 1_000, 2026-10-08, .inf) */
const NUMBERISH = /^[-+]?(\.?\d|\.(inf|nan))/i

/** One scalar or flow collection on one line. Quoted (JSON string = valid YAML) unless provably plain. */
export function inline(v: unknown): string {
  if (v === null || v === undefined) return 'null'
  if (typeof v === 'boolean' || typeof v === 'number') return String(v)
  if (Array.isArray(v)) return v.length === 0 ? '[]' : `[${v.map(inline).join(', ')}]`
  if (typeof v === 'object') {
    const entries = Object.entries(v as Record<string, unknown>)
    return entries.length === 0 ? '{}' : `{ ${entries.map(([k, x]) => `${k}: ${inline(x)}`).join(', ')} }`
  }
  const s = String(v)
  if (s === '' || s.trim() !== s || AMBIGUOUS.test(s) || !PLAIN.test(s) || NUMBERISH.test(s) || s.includes(' #') || s.endsWith(':')) {
    return JSON.stringify(s).replace(/[\u0085\u2028\u2029]/g, c => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`)
  }
  return s
}

export function splitList(s: string): string[] {
  return s.split(/[,\n]/).map(x => x.trim()).filter(x => x !== '')
}

function toInt(s: string, fallback: number): number {
  const n = Number.parseInt(s.trim(), 10)
  return Number.isFinite(n) && n >= 0 ? n : fallback
}

export function enabledAgents(d: MkWizardDraft): string[] {
  return AGENT_NAMES.filter(n => d.agents[n]?.enabled)
}

/** The builder order as written: the user's edit, else the default filtered to the enabled agents. */
export function builderOrderOf(d: MkWizardDraft): string[] {
  if (d.builderOrder != null) return splitList(d.builderOrder)
  const on = new Set(enabledAgents(d))
  return DEFAULT_BUILDER_ORDER.filter(n => on.has(n))
}

export function dataDirOf(d: MkWizardDraft): string | null {
  if (d.dataEnv != null) return d.dataEnv // readers check $MATRIXKODER_DATA first
  if (d.dataMode === 'home') return d.globalDir == null ? null : `${d.globalDir}/projects/${d.slug}`
  return d.repo == null ? null : `${d.repo}/.matrixkoder`
}

/** The filled templates/matrixkoder.yaml. `checkedAt` is an ISO timestamp the caller reads from the clock. */
export function buildYaml(d: MkWizardDraft, checkedAt: string): string {
  const L: string[] = []
  const kv = (ind: number, key: string, val: unknown, note?: string): void => {
    L.push(`${' '.repeat(ind)}${key}: ${inline(val)}${note ? `   # ${note}` : ''}`)
  }
  const agent = (n: string): { available: boolean | null; models: readonly string[] } => {
    const a = d.agents[n]
    return { available: a == null || !a.found ? false : a.enabled, models: a?.models ?? [] }
  }
  const g = d.gates
  const proposed = (v: string): string | undefined => (v !== '' ? 'proposed' : undefined)
  L.push('# <data>/matrixkoder.yaml — written by the MatrixKoder onboarding wizard after the user confirmed.')
  L.push('# Re-checked for drift on every run; the user\'s explicit statements override any value.')
  kv(0, 'data_dir', d.dataEnv != null || d.dataMode === 'home' ? `${dataDirOf(d) ?? ''}/` : '.matrixkoder/', 'project data dir')
  L.push('repo:')
  kv(2, 'integration_branch', d.branch, 'lanes merge here; one PR to main at the end')
  kv(2, 'lanes', { slots: 1, pattern: '../{repo}-mk-{n}', reuse: true }, 'reusable worktree slots; the cap is max_build_lanes')
  kv(2, 'conventions', [])
  L.push('gates:')
  kv(2, 'install', g.install, proposed(g.install))
  kv(2, 'test', g.test, proposed(g.test))
  kv(2, 'typecheck', g.typecheck, proposed(g.typecheck))
  kv(2, 'lint_changed', g.lint_changed, proposed(g.lint_changed))
  kv(2, 'format_changed', g.format_changed, proposed(g.format_changed))
  L.push('baselines:                            # measured at first run, refreshed each run')
  kv(2, 'type_errors', null)
  kv(2, 'lint_errors', null)
  kv(2, 'known_failing_tests', [])
  kv(0, 'banned_commands', d.banned, d.banned.length > 0 ? 'proposed: dev servers' : undefined)
  L.push('qa:')
  kv(2, 'frontend', d.qaFrontend)
  kv(2, 'start', d.qaStart, 'orchestrator only')
  kv(2, 'integration_target', d.qaTarget, 'local-main = merge reviewed lanes into local main (no push) and QA on the running dev server')
  kv(2, 'dev_url', d.qaDevUrl)
  kv(2, 'port', portOf(d.qaDevUrl))
  kv(2, 'themes', splitList(d.qaThemes))
  kv(2, 'mockups', d.qaMockups)
  L.push('agents:                               # installed + logged in, verified at bootstrap')
  for (const n of ['codex', 'opencode', 'agy'] as const) {
    const a = agent(n)
    kv(2, n, a)
  }
  kv(2, 'claude', { available: true })
  kv(2, 'herdr', { available: agent('herdr').available })
  kv(2, 'checked_at', checkedAt)
  L.push('routing_preferences:')
  kv(2, 'policy', 'claude-sparing', 'Claude Code orchestrates; builders below')
  kv(2, 'builder_order', builderOrderOf(d))
  kv(2, 'effort_by_size', { trivial: 'low', standard: 'medium', complex: 'high', final_review: 'max' })
  L.push('  reserve_floor:')
  kv(4, 'claude', { five_hour_left_pct: toInt(d.claudeFiveHour, 40), weekly_left_pct: toInt(d.claudeWeekly, 30) })
  kv(4, 'others', { five_hour_left_pct: 10, weekly_left_pct: 15 })
  kv(2, 'max_build_lanes', toInt(d.maxLanes, 3))
  kv(2, 'reviewers', ['codex', 'agy-pro', 'claude-opus-if-jev'])
  L.push('autonomy_contract:')
  kv(2, 'pause_only_for', ['destructive-not-preauthorised', 'all-agents-below-floor', 'uncovered-scope-decision', 'secrets-or-user-config', 'lock-over-30min', 'fix-loop-exhausted', 'blocked-approval-dialog', 'jev-screen-block'])
  kv(2, 'preauthorised', splitList(d.preauthorised))
  kv(2, 'builder_skip_permissions', d.skipPermissions, 'builders only, in their own worktree')
  L.push('deploy:')
  kv(2, 'readme', '<data>/deploys/README.md', 'repo-specific checklist; always user-gated')
  L.push('locks:')
  kv(2, 'dir', `${d.globalDir ?? '~/.matrixkoder'}/locks`)
  L.push('usage:')
  kv(2, 'source', 'codexbar', 'one source for all agents; mk-usage.py -> ~/.matrixkoder/usage.json')
  kv(2, 'poll_min', Math.max(1, toInt(d.pollMin, 5)))
  kv(2, 'providers', d.providersOn, 'add zai once its key is set in CodexBar')
  L.push('dashboard:')
  kv(2, 'auto_open', d.autoOpen, 'only when fullscreen and >=144 cols')
  kv(2, 'chip', d.chip)
  kv(2, 'toasts', d.toasts)
  return `${L.join('\n')}\n`
}

function portOf(url: string): number | null {
  const m = /:(\d{2,5})(?:\/|$)/.exec(url)
  return m?.[1] ? Number(m[1]) : null
}

/** Global ~/.matrixkoder/config.json: merges the dashboard prefs into whatever is already there. */
export function buildConfigJson(existing: unknown, d: MkWizardDraft, at: string): string {
  const base = rec(existing)
  const out = {
    ...base,
    schema: 1,
    dashboard: { ...rec(base.dashboard), auto_open: d.autoOpen, chip: d.chip, toasts: d.toasts },
    usage: { ...rec(base.usage), poll_min: Math.max(1, toInt(d.pollMin, 5)) },
    updated: at,
  }
  return `${JSON.stringify(out, null, 2)}\n`
}

/** Upserts one repo into projects.json without touching the others. */
export function buildProjectsJson(existing: unknown, repo: string, dataDir: string, at: string): string {
  const out = { ...rec(existing), [repo]: { data_dir: dataDir, onboarded_at: at } }
  return `${JSON.stringify(out, null, 2)}\n`
}

/** The .gitignore text with `/.matrixkoder/` appended, or null when already ignored. */
export function addToGitignore(text: string | null): string | null {
  const lines = (text ?? '').split('\n').map(l => l.trim())
  if (lines.some(l => l === '/.matrixkoder/' || l === '.matrixkoder/' || l === '/.matrixkoder' || l === '.matrixkoder')) return null
  const body = text ?? ''
  return `${body}${body === '' || body.endsWith('\n') ? '' : '\n'}/.matrixkoder/\n`
}

/** Text answer for `/mk setup` outside fullscreen: the detected defaults, one command to continue. */
export function fallbackText(d: MkWizardDraft): string {
  const found = AGENT_NAMES.filter(n => d.agents[n]?.found)
  const missing = AGENT_NAMES.filter(n => !d.agents[n]?.found)
  const gates = Object.entries(d.gates).filter(([, v]) => v !== '').map(([k, v]) => `${k}: ${v}`)
  const herdrText = !d.agents.herdr?.found
    ? 'not found'
    : d.herdrPlugin === 'linked'
      ? 'plugin linked'
      : d.herdrPlugin === 'link'
        ? `run \`herdr plugin link ${d.pluginRoot}/herdr\` to enable event-driven lanes`
        : 'link skipped'
  return [
    'MatrixKoder setup needs the fullscreen pane. Run /tui fullscreen, then /mk setup again.',
    'Detected defaults:',
    `- install: ${d.installMode}`,
    `- data: ${d.dataEnv != null ? `${d.dataEnv}/ ($MATRIXKODER_DATA)` : d.dataMode === 'home' ? `${dataDirOf(d) ?? ''}/` : `${d.repo ?? '<repo>'}/.matrixkoder/ (add to .gitignore)`}`,
    `- agents: ${found.length > 0 ? found.join(', ') : 'none'}${missing.length > 0 ? ` (missing: ${missing.join(', ')})` : ''}`,
    `- usage: ${d.codexbarVersion != null ? `codexbar ${d.codexbarVersion}, providers ${d.providersOn.join(', ') || 'none'}` : 'codexbar not found'}`,
    `- gates: ${gates.length > 0 ? gates.join('; ') : 'none detected'}`,
    `- herdr: ${herdrText}`,
    'Nothing was written.',
  ].join('\n')
}
