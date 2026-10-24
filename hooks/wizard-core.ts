// MatrixKoder onboarding wizard (plan §7): the PURE halves — detection parsers,
// gate proposals, the hand-rolled YAML emitter and the file-content builders.
// No $ in here: wizard.tsx binds these to process/fs/state. Nothing here ever
// handles an API key: the z.ai key is entered by the user into CodexBar.
import type { MkWizardAgent, MkWizardDraft, MkWizardGates } from '../types'
import { basename } from './data'
import { paths } from './paths'

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
    loaded: false, repo: null, slug: 'project', home: null, globalDir: null, dataEnv: null, installMode: 'dev', pluginRoot: '', version: '', branch: 'main',
    dataMode: 'repo', gitignore: true, gitignoreHas: false,
    agents, codexbarVersion: null, providersAll: [...DEFAULT_PROVIDERS], providersOn: [...DEFAULT_PROVIDERS],
    gates: { test: '', typecheck: '', lint: '', build: '' }, gatesFrom: '', banned: [],
    builderOrder: null, maxLanes: '3', claudeFiveHour: '40', claudeWeekly: '30',
    preauthorised: 'routing, failover, fix-loops, worktrees, lane-merges, push-non-main, open-pr',
    skipPermissions: true,
    qaFrontend: false, qaStart: '', qaDevUrl: '', qaThemes: '', qaMockups: '', qaTarget: 'local-main',
    autoOpen: true, chip: true, toasts: true, pollMin: '5',
    existingYaml: false, storeDir: null, migrated: false, replace: false, phase: 'editing', written: [], error: null,
    herdrPlugin: 'skip', herdrStaleLink: null, herdrLinkResult: null, prefsSaved: false,
  }
}

export const EMPTY_DRAFT: MkWizardDraft = emptyDraft()

// ---------- detection parsers ----------

export function slugOf(path: string): string {
  // mkdb.py's SLUG_RE (^[a-z0-9][a-z0-9-]{0,40}$): letters, digits and '-' only, 41 at most.
  // '_' and '.' used to survive, and `mk-state.py project register` refuses those AFTER the yaml is written (B-2).
  const s = basename(path).toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+/, '')
    .slice(0, 41)
    .replace(/-+$/, '')
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

/** The Usage step's provider label (the slides' short names): the stored value always stays the
 *  CodexBar id, and a provider this file has never heard of still gets a word back — the old
 *  fallback printed the bare id, so `openai` drew as `openai` and read as a second-class row (B-9). */
const PROVIDER_LABEL: Record<string, string> = {
  claude: 'Claude', codex: 'Codex', antigravity: 'Antigravity', openai: 'OpenAI',
  zai: 'Z.AI', opencode: 'OpenCode Zen', opencodego: 'OpenCode Go', ollama: 'Ollama',
}
export function providerLabel(p: string): string {
  return PROVIDER_LABEL[p] ?? p.charAt(0).toUpperCase() + p.slice(1)
}

export function parseVersion(stdout: string): string | null {
  const m = /(\d+\.\d+(?:\.\d+)?)/.exec(stdout)
  return m?.[1] ?? null
}

export function providerChoices(listed: readonly { name: string; enabled: boolean }[]): { all: string[]; on: string[] } {
  const relevant = new Set<string>(RELEVANT_PROVIDERS)
  const shown = listed.filter(p => relevant.has(p.name) || p.enabled)
  // every listed provider that CodexBar has ON is pre-selected, relevant or not: a listed-but-unticked
  // row with no reason was B-9 (`openai: enabled` drew as `[ ] openai`).
  return { all: shown.map(p => p.name), on: shown.filter(p => p.enabled).map(p => p.name) }
}

/** Plugin root → install mode: a marketplace install lives in the plugins cache. */
export function installModeOf(root: string): 'dev' | 'marketplace' {
  return /\/plugins\/(cache|marketplaces)\//.test(root) ? 'marketplace' : 'dev'
}

/** The matrixkoder line of `herdr plugin list`, and the local path it registers:
 *  `- matrixkoder (MatrixKoder) enabled [local:/path/to/herdr]`. Null when the list has no
 *  matrixkoder line, `path: null` when the line names a source that is not `local:` (a
 *  registry install has no path to compare). */
export function herdrLinkOf(stdout: string): { path: string | null } | null {
  for (const line of stdout.split('\n')) {
    if (!/^(?:-\s+)?matrixkoder\b/.test(line.trim())) continue
    const m = /\[local:([^\]]+)\]/.exec(line)
    return { path: m?.[1]?.trim() ?? null }
  }
  return null
}

/** What the herdr plugin link stands at: `linked` when `herdr plugin list` names matrixkoder at this
 *  plugin's own `<root>/herdr`; `link` when it is absent, names another path (a stale link left by an
 *  earlier marketplace version — `stale` carries that path so the review can say what is being relinked),
 *  or the list cannot be read. Detection alone never writes; the write step relinks. */
export function herdrStateOf(stdout: string | null, root: string): { state: 'linked' | 'link'; stale: string | null } {
  if (stdout == null) return { state: 'link', stale: null }
  const link = herdrLinkOf(stdout)
  if (link == null) return { state: 'link', stale: null }
  const want = `${root.replace(/\/+$/, '')}/herdr`
  if (link.path == null || link.path === want) return { state: 'linked', stale: null }
  return { state: 'link', stale: link.path }
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

type Pm = { run: string; exec: string }
function pmOf(locks: readonly string[]): Pm {
  if (locks.includes('pnpm-lock.yaml')) return { run: 'pnpm run', exec: 'pnpm exec' }
  if (locks.includes('yarn.lock')) return { run: 'yarn', exec: 'yarn' }
  if (locks.includes('bun.lock') || locks.includes('bun.lockb')) return { run: 'bun run', exec: 'bunx' }
  return { run: 'npm run', exec: 'npx' }
}

/** Proposes the four gate commands from what the repo declares. Never runs anything.
 *  Installers and formatters are not gates (C3): they change files, so neither is proposed.
 *  The proposed commands are whole-repo runs: the old `{paths}`/`{files}` scope tokens were written
 *  literally by this emitter and expanded by nobody (B-18), so a proposed gate would have run a
 *  shell that met the braces and, if it ever ran, a command named `{files}`. */
export function proposeGates(inp: GateInputs): { gates: MkWizardGates; banned: string[]; from: string } {
  const gates: MkWizardGates = { test: '', typecheck: '', lint: '', build: '' }
  const banned: string[] = []
  const from: string[] = []
  const pkg = rec(inp.pkg)
  if (Object.keys(pkg).length > 0) {
    from.push('package.json')
    const pm = pmOf(inp.locks)
    const scripts = rec(pkg.scripts)
    const deps = { ...rec(pkg.dependencies), ...rec(pkg.devDependencies) }
    const script = (n: string): string => (typeof scripts[n] === 'string' ? (scripts[n] as string) : '')
    if (script('test') !== '' || 'vitest' in deps || 'jest' in deps) {
      gates.test = 'vitest' in deps || /vitest/.test(script('test')) ? `${pm.exec} vitest run`
        : 'jest' in deps || /jest/.test(script('test')) ? `${pm.exec} jest`
        : `${pm.run} test`
    }
    const tc = ['typecheck', 'type-check', 'check:types', 'tsc'].find(n => script(n) !== '')
    gates.typecheck = tc ? `${pm.run} ${tc}` : inp.hasTsconfig || 'typescript' in deps ? `${pm.exec} tsc --noEmit` : ''
    if ('eslint' in deps) gates.lint = `${pm.exec} eslint .`
    else if ('@biomejs/biome' in deps) gates.lint = `${pm.exec} biome check .`
    if (script('build') !== '') gates.build = `${pm.run} build`
    for (const n of ['dev', 'start']) if (script(n) !== '') banned.push(`${pm.run} ${n}`)
  }
  if (inp.pyproject != null) {
    from.push('pyproject.toml')
    const py = inp.pyproject
    if (gates.test === '' && /pytest/.test(py)) gates.test = 'pytest'
    if (gates.typecheck === '') gates.typecheck = /mypy/.test(py) ? 'mypy .' : /pyright/.test(py) ? 'pyright' : ''
    if (gates.lint === '' && /ruff/.test(py)) gates.lint = 'ruff check .'
  }
  if (inp.makefile != null) {
    from.push('Makefile')
    const has = (t: string): boolean => new RegExp(`^${t}:`, 'm').test(inp.makefile ?? '')
    if (gates.test === '' && has('test')) gates.test = 'make test'
    if (gates.typecheck === '' && has('typecheck')) gates.typecheck = 'make typecheck'
    if (gates.lint === '' && has('lint')) gates.lint = 'make lint'
    if (gates.build === '' && has('build')) gates.build = 'make build'
  }
  const hasProjectTest = gates.test !== ''
  // marker files fill what is still empty, so the blocks above win (a Makefile target beats Cargo)
  // detection order: plugin → Cargo → go → selftest
  const marker = (m: string): boolean => inp.markers.includes(m)
  if (marker('.claude-plugin/plugin.json')) {
    from.push('.claude-plugin/plugin.json')
    if (gates.test === '') gates.test = 'claude plugin test .'
    // the plugin schema has no whole-repo validate slot; every proposed gate is a whole-repo run
    if (gates.lint === '') gates.lint = 'claude plugin validate .'
  }
  if (marker('Cargo.toml')) {
    from.push('Cargo.toml')
    if (gates.test === '') gates.test = 'cargo test'
    if (gates.lint === '') gates.lint = 'cargo clippy'
    if (gates.build === '') gates.build = 'cargo build'
  }
  if (marker('go.mod')) {
    from.push('go.mod')
    if (gates.test === '') gates.test = 'go test ./...'
    if (gates.lint === '') gates.lint = 'go vet ./...'
    if (gates.build === '') gates.build = 'go build ./...'
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

/** The settings file's NAME (`project.yaml` or `matrixkoder.yaml`), asked of paths.ts rather than
 *  spelled: C1 keeps every store and config file name in one place, and the c1 gate fails on a literal
 *  in any other file. `migrated` is the draft's own flag (B-1). */
export function yamlNameOf(migrated: boolean): string {
  return basename(paths.projectYaml('', migrated))
}

export function dataDirOf(d: MkWizardDraft): string | null {
  if (d.dataEnv != null) return d.dataEnv // readers check $MATRIXKODER_DATA first
  // The root store's row for this repo wins: it names the folder the pane itself reads (mkpaths.data_dir,
  // data.ts projectDirFromStore), and for a MIGRATED project that is its C2 folder
  // `<globalDir>/projects/<slug>`. Writing anywhere else put the yaml beside a store nothing reads (B-1).
  if (d.storeDir != null) return d.storeDir
  if (d.dataMode === 'home') return d.globalDir == null ? null : paths.homeProjectDir(d.globalDir, d.slug)
  return d.repo == null ? null : paths.legacyDataDir(d.repo)
}

/** The session: keys a replace carries over — the Settings tab owns them between wizard runs. */
const SESSION_KEYS = new Set(['fresh_session', 'subagents', 'subagent_model', 'stall_min', 'max_wait_min', 'reviewer_split', 'reuse_reviewer'])

type YamlCarry = {
  session: Record<string, string | number | boolean>
  picks: Record<string, string[]>
  reviewer: string | null
  visualReviewer: string | null
}

/** `22   # idle minutes…` → `22`: cut at a `#` that starts a comment (whitespace before it, outside quotes). */
function stripYamlComment(v: string): string {
  let q: string | null = null
  for (let i = 0; i < v.length; i++) {
    const c = v[i]
    if (q != null) {
      if (c === q) q = null
    } else if (c === '"' || c === "'") q = c
    else if (c === '#' && (i === 0 || v[i - 1] === ' ' || v[i - 1] === '\t')) return v.slice(0, i)
  }
  return v
}

function unquoteScalar(v: string): string {
  const t = v.trim()
  return t.length >= 2 && ((t.startsWith('"') && t.endsWith('"')) || (t.startsWith("'") && t.endsWith("'"))) ? t.slice(1, -1) : t
}

const NUMERIC_KEYS = new Set(['stall_min', 'max_wait_min'])

/** The carried value for one session key, in the type that key actually has. Only the two numeric
 *  keys become numbers: everything else keeps its text, so a model id that looks like a number
 *  (`"007"`) stays `"007"` instead of turning into 7. */
function typedSessionScalar(key: string, v: string): string | number | boolean {
  if (NUMERIC_KEYS.has(key) && /^-?\d+$/.test(v)) return Number(v)
  if (key === 'subagents' || key === 'reuse_reviewer') {
    if (v === 'true') return true
    if (v === 'false') return false
  }
  return v
}

/** What an existing matrixkoder.yaml set in the three groups a replace must keep: the session:
 *  block, the picks lists and the two reviewer routes. Line-wise scan in this file's style —
 *  a malformed or absent block yields no override (defaults apply); never throws. */
function parseCarry(text: string | null | undefined): YamlCarry {
  const out: YamlCarry = { session: {}, picks: {}, reviewer: null, visualReviewer: null }
  if (text == null) return out
  let block = ''
  for (const raw of text.split('\n')) {
    const line = raw.replace(/\r$/, '')
    if (line.trim() === '' || line.trim().startsWith('#')) continue
    if (/^[A-Za-z_][\w-]*:/.test(line)) {
      block = line.slice(0, line.indexOf(':'))
      continue
    }
    const m = /^\s+([A-Za-z_][\w-]*):\s*(.*)$/.exec(line)
    if (m == null) continue
    const key = m[1] ?? ''
    const tail = m[2] ?? ''
    if (key === '') continue
    if (block === 'session' && SESSION_KEYS.has(key)) {
      const v = unquoteScalar(stripYamlComment(tail))
      if (v !== '') out.session[key] = typedSessionScalar(key, v)
    } else if (block === 'routing_preferences' && (key === 'reviewer' || key === 'visual_reviewer')) {
      const v = unquoteScalar(stripYamlComment(tail))
      if (v !== '') {
        if (key === 'reviewer') out.reviewer = v
        else out.visualReviewer = v
      }
    } else if (block === 'picks') {
      const v = stripYamlComment(tail).trim()
      if (v.startsWith('[') && v.endsWith(']')) out.picks[key] = splitList(v.slice(1, -1)).map(unquoteScalar)
    }
  }
  return out
}

/** The filled templates/matrixkoder.yaml. `checkedAt` is an ISO timestamp the caller reads from the clock.
 *  `existingYaml` is the text about to be replaced: its session/picks/reviewer values carry over. */
export function buildYaml(d: MkWizardDraft, checkedAt: string, existingYaml?: string | null): string {
  const carry = parseCarry(existingYaml)
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
  // the header names the file it is (B-1); the name itself comes from paths.ts (C1)
  L.push(`# <data>/${yamlNameOf(d.migrated)} — written by the MatrixKoder onboarding wizard after the user confirmed.`)
  L.push('# Re-checked for drift on every run; the user\'s explicit statements override any value.')
  // B-8: with no global dir to name in home mode, dataDirOf is null and the old `${dir ?? ''}/`
  // wrote the value `/` — a data dir of the filesystem root. The locks line below already spells
  // an unknown home as `~/.matrixkoder`; data_dir does the same instead of inventing a root.
  kv(0, 'data_dir', d.dataEnv != null || d.dataMode === 'home' ? `${dataDirOf(d) ?? paths.homeProjectDir('~/.matrixkoder', d.slug)}/` : '.matrixkoder/', 'project data dir')
  L.push('repo:')
  kv(2, 'integration_branch', d.branch, 'lanes merge here; one PR to main at the end')
  kv(2, 'lanes', { slots: 1, pattern: '../{repo}-mk-{n}', reuse: true }, 'reusable worktree slots; the cap is max_build_lanes')
  kv(2, 'conventions', [])
  L.push('gates:')
  kv(2, 'test', g.test, proposed(g.test))
  kv(2, 'typecheck', g.typecheck, proposed(g.typecheck))
  kv(2, 'lint', g.lint, proposed(g.lint))
  kv(2, 'build', g.build, proposed(g.build))
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
  kv(2, 'reviewer', carry.reviewer ?? 'claude sonnet medium', 'the route every review goes to')
  kv(2, 'visual_reviewer', carry.visualReviewer ?? 'claude haiku', 'the route a visual/UI review goes to')
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
  kv(2, 'providers', d.providersOn, 'add zai once its key is set in CodexBar')
  L.push('session:')
  kv(2, 'fresh_session', carry.session.fresh_session ?? 'new', 'what a fix or takeover round starts the agent with')
  kv(2, 'subagents', carry.session.subagents ?? true, 'a phase may hand its tasks to subagents')
  kv(2, 'subagent_model', carry.session.subagent_model ?? '', 'the model a subagent runs on (empty: the orchestrator\'s own)')
  kv(2, 'stall_min', carry.session.stall_min ?? 15, 'idle minutes before a working lane is called stalled')
  kv(2, 'max_wait_min', carry.session.max_wait_min ?? 30, 'minutes lane-watch waits before it gives up')
  kv(2, 'reviewer_split', carry.session.reviewer_split ?? 'right', 'where the reviewer runs while the builder waits')
  kv(2, 'reuse_reviewer', carry.session.reuse_reviewer ?? true, 'one reviewer session serves the phase\'s rounds')
  L.push('picks:')
  for (const n of AGENT_NAMES) {
    if (d.agents[n]?.found) kv(2, n, carry.picks[n] ?? [], 'this agent\'s models in preference order')
  }
  return `${L.join('\n')}\n`
}

function portOf(url: string): number | null {
  const m = /:(\d{2,5})(?:\/|$)/.exec(url)
  return m?.[1] ? Number(m[1]) : null
}

/** The .gitignore text with `/.matrixkoder/` appended, or null when already ignored. */
export function addToGitignore(text: string | null): string | null {
  const lines = (text ?? '').split('\n').map(l => l.trim())
  if (lines.some(l => l === '/.matrixkoder/' || l === '.matrixkoder/' || l === '/.matrixkoder' || l === '.matrixkoder')) return null
  const body = text ?? ''
  return `${body}${body === '' || body.endsWith('\n') ? '' : '\n'}/.matrixkoder/\n`
}

/** One row of the review's `-`/`+` diff (B-6, SetupReviewReplace). */
export type YamlDiffRow = { sign: '-' | '+' | ' '; text: string }

/** The review's replace form: line-wise `-`/`+` rows against the file on disk, plus how many lines the
 *  two share. A real line diff (LCS over lines, O(n·m) — the files are under ~200 lines), not a positional
 *  compare: one line inserted at the top used to mark every later line changed, which reads as a rewrite
 *  the write is not (B-19). A changed hunk draws its `-` rows before its `+` rows; unchanged lines keep
 *  their text and draw with a leading space. `changed` counts changed lines the way the count text reads
 *  (`N lines changed`): the larger of removed and added per hunk, so one replaced line is 1, not 2. */
export function yamlDiff(oldText: string | null, newText: string): { rows: YamlDiffRow[]; changed: number; unchanged: number } {
  const before = oldText == null || oldText === '' ? [] : oldText.replace(/\n$/, '').split('\n')
  const after = newText === '' ? [] : newText.replace(/\n$/, '').split('\n')
  const rows: YamlDiffRow[] = []
  let changed = 0
  let unchanged = 0
  const n = before.length
  const m = after.length
  // lcs[i][j] = the longest common subsequence of before[i..] and after[j..]
  const width = m + 1
  const lcs = new Uint32Array((n + 1) * width)
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i * width + j] = before[i] === after[j]
        ? (lcs[(i + 1) * width + j + 1] ?? 0) + 1
        : Math.max(lcs[(i + 1) * width + j] ?? 0, lcs[i * width + j + 1] ?? 0)
    }
  }
  const at = (i: number, j: number): number => lcs[i * width + j] ?? 0
  let removed: string[] = []
  let added: string[] = []
  const flush = (): void => {
    if (removed.length === 0 && added.length === 0) return
    for (const text of removed) rows.push({ sign: '-', text })
    for (const text of added) rows.push({ sign: '+', text })
    changed += Math.max(removed.length, added.length)
    removed = []
    added = []
  }
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (before[i] === after[j]) {
      flush()
      rows.push({ sign: ' ', text: before[i] ?? '' })
      unchanged++
      i++
      j++
    } else if (at(i, j + 1) >= at(i + 1, j)) {
      // a tie takes the insertion first, so an inserted line draws as one `+` row and nothing else moves
      added.push(after[j] ?? '')
      j++
    } else {
      removed.push(before[i] ?? '')
      i++
    }
  }
  while (i < n) removed.push(before[i++] ?? '')
  while (j < m) added.push(after[j++] ?? '')
  flush()
  return { rows, changed, unchanged }
}

/** One drift line: the key, what the existing file says, and what detection offers instead (B-15). */
export type DriftItem = { key: string; file: string; detected: string }

/** The four gates, in the emitted order, with the yaml key each one is written under. */
const DRIFT_GATES: readonly (readonly [keyof MkWizardGates, string])[] = [
  ['test', 'gates.test'],
  ['typecheck', 'gates.typecheck'],
  ['lint', 'gates.lint'],
  ['build', 'gates.build'],
]

/** The yaml keys the gates block used to carry, new name -> old name (C3). A file written before the
 *  rename is read through this map, so `lint_changed: ...` is the `lint` gate and not a missing one;
 *  the next write emits the new key. `install` and `format_changed` have no new name: they were never
 *  gates, so an old file's lines for them are ignored. */
const GATE_KEY_ALIASES: Readonly<Record<string, string>> = { lint: 'lint_changed' }

/** One scalar from a `name: value` line under a top-level block (`repo`, `gates`), or undefined when the
 *  block or the key is absent. A line-wise scan like parseCarry — no yaml library, never throws. */
function scalarUnder(text: string, block: string, key: string): string | undefined {
  let inside = false
  for (const raw of text.split('\n')) {
    const line = raw.replace(/\r$/, '')
    if (line.trim() === '' || line.trim().startsWith('#')) continue
    if (/^[A-Za-z_][\w-]*:/.test(line)) {
      inside = line.slice(0, line.indexOf(':')).trim() === block
      continue
    }
    if (!inside) continue
    const m = /^\s+([A-Za-z_][\w-]*):\s*(.*)$/.exec(line)
    if (m?.[1] === key) return unquoteScalar(stripYamlComment(m[2] ?? ''))
  }
  return undefined
}

/** The agent names an existing yaml lists under `agents:` (its keys, minus the `checked_at` stamp), or null
 *  when the file has no `agents:` block at all — an unreadable or older file must not read as "every agent
 *  removed". */
function agentsIn(text: string): string[] | null {
  let inside = false
  let seen = false
  const out: string[] = []
  for (const raw of text.split('\n')) {
    const line = raw.replace(/\r$/, '')
    if (line.trim() === '' || line.trim().startsWith('#')) continue
    if (/^[A-Za-z_][\w-]*:/.test(line)) {
      inside = line.slice(0, line.indexOf(':')).trim() === 'agents'
      seen = seen || inside
      continue
    }
    if (!inside) continue
    const m = /^\s+([A-Za-z_][\w-]*):/.exec(line)
    if (m?.[1] != null && m[1] !== 'checked_at') out.push(m[1])
  }
  return seen ? out : null
}

/** B-15: what detection now says that the project's own file does not (display only — nothing is written
 *  and nothing is changed). Compares the four gates, `repo.integration_branch` and the enabled agents.
 *  An absent existing file is no drift. A gate detected as `''` is "no command" and matches a file that
 *  says `""` too, so only a real difference is listed. A gate the file still holds under its old key
 *  (`lint_changed`) reads as the new one, so a pre-rename file drifts on its value, never on the name. */
export function driftOf(existingYamlText: string | null | undefined, d: MkWizardDraft): DriftItem[] {
  if (existingYamlText == null || existingYamlText.trim() === '') return []
  const out: DriftItem[] = []
  for (const [key, label] of DRIFT_GATES) {
    const alias = GATE_KEY_ALIASES[key]
    const own = scalarUnder(existingYamlText, 'gates', key)
    const oldName = alias != null ? scalarUnder(existingYamlText, 'gates', alias) : undefined
    // an empty new-name value falls back to the old key too (``lint: ""`` beside `lint_changed: x` reads
    // x), which is what scripts/mk-config.py gates_of does — the two must agree on a hand-edited file
    const inFile = own == null || own.trim() === '' ? oldName ?? own : own
    if (inFile == null) continue
    const detected = d.gates[key].trim()
    // both empty is not a drift; an empty file value against a detected command (or the reverse) is
    const file = inFile.trim()
    if (file === detected) continue
    if (file === '' && detected === '') continue
    out.push({ key: label, file: file === '' ? '""' : file, detected: detected === '' ? '""' : detected })
  }
  const branch = scalarUnder(existingYamlText, 'repo', 'integration_branch')
  if (branch != null && branch.trim() !== d.branch.trim()) {
    out.push({ key: 'repo.integration_branch', file: branch.trim() === '' ? '""' : branch.trim(), detected: d.branch.trim() === '' ? '""' : d.branch.trim() })
  }
  const inFile = agentsIn(existingYamlText)
  if (inFile != null) {
    const on = enabledAgents(d)
    for (const n of on.filter(x => !inFile.includes(x))) out.push({ key: `agents.${n}`, file: 'absent', detected: 'enabled' })
    for (const n of inFile.filter(x => !on.includes(x))) out.push({ key: `agents.${n}`, file: 'listed', detected: 'not found' })
  }
  return out
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
