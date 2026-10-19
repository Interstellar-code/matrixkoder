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
  /** F.1 contract: the derived risk (int 1-5), its critical flag and the open
   *  reasons (each {kind, points, source, opened}). Optional — an old row has none. */
  risk?: number
  critical?: boolean
  open_reasons?: readonly { readonly kind: string; readonly points: number; readonly source: string; readonly opened: string }[]
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
  items: readonly { readonly task: string; readonly to: string; readonly askId?: string }[]
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

/** dashboard.tsx (S0.2): the /mk pane's tabs */
export type MkDashTab = 'overview' | 'lanes' | 'plan' | 'usage' | 'files' | 'events' | 'waiting' | 'kanban' | 'todo' | 'improve' | 'settings'

export type MkFileAct = {
  path: string
  kind: 'read' | 'write'
  at: number
}

// ---- Improve (hooks/improve-core.ts, hooks/tabs/improve.tsx): the footer prompt box and the Improve tab share one proposal ----

/** one multiple-choice question of the wizard: 2-4 short options */
export type MkImproveQuestion = { question: string; options: string[] }

/** the person's answer to one question; a skipped one is left out of the rewrite */
export type MkImproveAnswer =
  | { kind: 'option'; text: string }
  | { kind: 'other'; text: string }
  | { kind: 'skip' }

/** asking = the wizard's questions are being written, writing = the rewrite is, review = a proposal is on show */
export type MkImproveStep = 'asking' | 'questions' | 'writing' | 'review' | 'error'

/** one improvement from the press of Improve to its review */
export type MkImproveFlow = {
  id: string
  original: string
  step: MkImproveStep
  questions: MkImproveQuestion[]
  /** keyed by the question's index as a string */
  answers: Record<string, MkImproveAnswer>
  /** index of the question whose "Other" field is open, or null */
  other: number | null
  /** what is typed in the open "Other" field, kept here so a redraw never erases it */
  otherDraft: string
  upgraded: string
  /** one short note per detail the rewrite added */
  added: string[]
  isEditing: boolean
  draft: string
  /** which model call failed, when step is 'error' */
  failedStep: 'questions' | 'rewrite'
  error: string
  note: string
}

export type MkImprove = {
  /** the footer prompt box: the single source of what it holds */
  text: string
  /** the box text before the last Accept, for the Undo chip */
  undo: string | null
  flow: MkImproveFlow | null
}

/** one saved improvement (the last 20, also kept in $.store) */
export type MkImproveEntry = {
  id: string
  at: number
  original: string
  upgraded: string
  added: string[]
}

declare module 'claude-code' {
  interface PluginState {
    matrixkoder: {
      snapshot: MkSnapshot
      taskStatuses: MkStatusMemory
      paneOpen: boolean
      /** dashboard.tsx: the /mk pane's selected tab */
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
      fileActivity: readonly MkFileAct[]
      /** dashboard.tsx: the footer prompt box and the open improvement (shared by the footer card and the Improve tab) */
      improve: MkImprove
      /** dashboard.tsx: the last 20 improvements, newest first */
      improveHistory: readonly MkImproveEntry[]
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
