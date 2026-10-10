// MatrixKoder $.state contract (plan §3): the atoms the mod keeps, and the
// run.json / usage.json field subsets the dashboard reads. Declarations only —
// runtime defaults live in hooks/data.ts.

export type MkWaiting = {
  question: string
  options?: readonly string[]
  rec?: string | null
  since?: string | null
}

export type MkTask = {
  id: string
  title: string
  wave?: number | null
  deps?: readonly string[]
  /** parent task id (nested task); the parent's status rolls up from its children */
  parent?: string | null
  /** set by mk-state: has deps, not started, every dep merged/qa/done */
  ready?: boolean
  status: string
  agent?: string | null
  model?: string | null
  effort?: string | null
  lane?: string | null
  last_commit?: string | null
  round?: number
  updated?: string | null
}

export type MkEvent = {
  t?: string
  task?: string | null
  type?: string
  text?: string
}

export type MkRun = {
  id: string
  feature: string
  branch: string
  /** optional (design §7.2): absent → the pane shows the branch only */
  head_sha?: string | null
  status?: string
  stage: number
  next?: string | null
  waiting?: MkWaiting | null
  tasks: readonly MkTask[]
  recent?: readonly MkEvent[]
  updated?: string | null
}

export type MkRunIndexEntry = {
  id: string
  feature?: string
  status?: string
  started?: string
  ended?: string | null
}

export type MkWindow = { used_pct?: number | null; resets_at?: string | null } | null | undefined

export type MkPool = {
  five_hour?: MkWindow
  weekly?: MkWindow
  exhausted_until?: string | null
  /** set when this specific plan/provider could not be polled (OpenCode per-plan pools) */
  status?: string
  error?: string
}

export type MkAgentUsage = MkPool & {
  source?: string
  /** mk-usage.py's verdict: 'ok' | 'n/a' | 'error' (absent on older ledgers) */
  status?: string
  available_pools?: readonly string[]
  pools?: Readonly<Record<string, MkPool>>
}

export type MkUsage = { agents?: Readonly<Record<string, MkAgentUsage>> }

export type MkSnapshot = {
  /** $.clock.now() at load — renders compute remaining times against it */
  now: number
  dataDir: string | null
  run: MkRun | null
  runs: readonly MkRunIndexEntry[]
  usage: MkUsage | null
  /** the project yaml's available agents, display-ordered (herdr excluded); null = no yaml / no agents block → show every ledger agent */
  projectAgents: readonly string[] | null
  /** set when state files exist but cannot be read/parsed; the pane shows a ⚠ note */
  error?: string | null
}

export type MkWakePending = {
  /** run the events belong to */
  run: string
  /** events.jsonl line count to mark as seen once the batch is submitted */
  upTo: number
  items: readonly { readonly task: string; readonly to: string }[]
}

export type MkStatusMemory = {
  statuses: Readonly<Record<string, string>>
  /** task ids that were ready at the last snapshot (a flip to ready toasts once) */
  ready?: readonly string[]
  waiting: boolean
  loaded: boolean
  /** run id the statuses belong to; a change resets memory silently (no spurious toasts) */
  runId?: string | null
}

/** dashboard.tsx (S0.2): the /mk pane's tabs — 'old' is the pre-tab pane, the rest are the six mock tabs */
export type MkDashTab = 'old' | 'overview' | 'lanes' | 'plan' | 'usage' | 'events' | 'waiting' | 'settings'

declare module 'claude-code' {
  interface PluginState {
    matrixkoder: {
      snapshot: MkSnapshot
      taskStatuses: MkStatusMemory
      paneOpen: boolean
      /** dashboard.tsx: the /mk pane's selected tab ('old' = the pre-tab pane) */
      dashTab: MkDashTab
      /** dashboard.tsx: the tabs' local view state (folded phases, revealed
       *  options) — C12: only an onPress ever writes it, never a render */
      viewState: Record<string, string>
      /** wake.ts: events.jsonl lines already seen, per run id */
      wakeEventLines: { readonly byId: number }
      /** wake.ts: last wake submit timestamp per task:status id */
      wakeSentAt: { readonly byId: number }
      /** wake.ts: lane events found but not yet submitted; survives a reload */
      wakePending: MkWakePending
      /** wizard.tsx: current step, 1..10 */
      wizardStep: number
      /** wizard.tsx: detection + answers until the review step writes them */
      wizardDraft: MkWizardDraft
    }
  }
}

declare module 'claude-code' {
  interface EngineInterface {
    matrixkoder: {
      snapshot: MkSnapshot
      taskStatuses: MkStatusMemory
      paneOpen: boolean
    }
  }
}

// ---- onboarding wizard (hooks/wizard.tsx): draft answers, kept in $.state until the review step writes ----

export type MkWizardAgent = {
  found: boolean
  path: string | null
  /** the user's toggle; claude is always on */
  enabled: boolean
  /** `agy models` / `opencode models` first columns, capped */
  models: readonly string[]
}

export type MkWizardGates = {
  install: string
  test: string
  typecheck: string
  lint_changed: string
  format_changed: string
}

export type MkWizardDraft = {
  /** detection finished; false until openWizard's scan resolves */
  loaded: boolean
  /** main-checkout path of the repo (a linked worktree resolves to it); null outside git */
  repo: string | null
  slug: string
  home: string | null
  /** global dir: $MATRIXKODER_HOME, else ~/.matrixkoder */
  globalDir: string | null
  /** $MATRIXKODER_DATA when set: every reader uses it, so the wizard writes there */
  dataEnv: string | null
  installMode: 'dev' | 'marketplace'
  pluginRoot: string
  branch: string
  dataMode: 'repo' | 'home'
  /** add /.matrixkoder/ to <repo>/.gitignore (repo mode only) */
  gitignore: boolean
  gitignoreHas: boolean
  agents: Readonly<Record<string, MkWizardAgent>>
  codexbarVersion: string | null
  /** providers offered as toggles, and the ones switched on */
  providersAll: readonly string[]
  providersOn: readonly string[]
  gates: MkWizardGates
  gatesFrom: string
  banned: readonly string[]
  /** null = derive from the enabled agents */
  builderOrder: string | null
  maxLanes: string
  claudeFiveHour: string
  claudeWeekly: string
  preauthorised: string
  skipPermissions: boolean
  qaFrontend: boolean
  qaStart: string
  qaDevUrl: string
  qaThemes: string
  qaMockups: string
  qaTarget: 'local-main' | 'branch'
  autoOpen: boolean
  chip: boolean
  toasts: boolean
  pollMin: string
  existingYaml: boolean
  /** "replace?" confirm for an existing matrixkoder.yaml; default keep */
  replace: boolean
  phase: 'editing' | 'written' | 'error'
  written: readonly string[]
  error: string | null
  herdrPlugin: 'linked' | 'link' | 'skip'
  herdrLinkResult: { ok: boolean; text: string } | null
}
