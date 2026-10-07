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
}

export type MkAgentUsage = MkPool & {
  source?: string
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
  /** set when state files exist but cannot be read/parsed; the pane shows a ⚠ note */
  error?: string | null
  /** /mk setup || /mk init asked for the first-run panel this session */
  setupHint?: boolean
}

export type MkStatusMemory = {
  statuses: Readonly<Record<string, string>>
  waiting: boolean
  loaded: boolean
  /** run id the statuses belong to; a change resets memory silently (no spurious toasts) */
  runId?: string | null
}

declare module 'claude-code' {
  interface PluginState {
    matrixkoder: {
      snapshot: MkSnapshot
      taskStatuses: MkStatusMemory
      paneOpen: boolean
      /** wake.ts: events.jsonl lines already seen, per run id */
      wakeEventLines: { readonly byId: number }
      /** wake.ts: last wake submit timestamp per task:status id */
      wakeSentAt: { readonly byId: number }
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
