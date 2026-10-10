// MatrixKoder wake on lane events: polls events.jsonl and submits prompt on transition events (strict types)
import type { EngineInterface, On, PluginOptions, Timer } from 'claude-code'
import type { MkWakePending } from '../types'
import { candidateDirs, globalDirPath, join, mainCheckout, parseGitdir, parseJson, projectDataDir } from './data'

// $.state contract for wakeEventLines and wakeSentAt lives in types/index.d.ts

const WAKE_STATUSES = new Set(['verifying', 'blocked', 'quota', 'stalled', 'provider'])
const TASK_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const COOLDOWN_MS = 5 * 60 * 1000 // 5 minutes
const HOLD_MS = 10 * 60 * 1000 // 10 minutes: a submit that threw may still have been queued
const CLAIM_DEAD_MS = 2 * 60 * 1000 // 2 minutes: a claim without a completed send is dead
const HOLD_GONE = -1 // a hold that already gave up: never blocks a later identical text

let debounceTimer: Timer | null = null
let retryNext = false // a submit failed or its text is on hold: the next poll tick re-checks the batch at once

/** A hot reload re-evaluates this module, so no module-level variable survives
 *  to the next evaluation: the live poll timer's cancel handle lives on
 *  globalThis (which a re-evaluation still reaches) under one wake-namespaced
 *  key — the same role data.ts's timerRegistry plays for the dashboard. */
const WAKE_TIMERS_KEY = '__matrixkoderWakeTimers'
type WakeTimers = { pollCancel?: () => void }

function wakeTimers(): WakeTimers {
  const g = globalThis as unknown as { [WAKE_TIMERS_KEY]?: WakeTimers }
  let timers = g[WAKE_TIMERS_KEY]
  if (timers == null) {
    timers = {}
    g[WAKE_TIMERS_KEY] = timers
  }
  return timers
}

type Pending = MkWakePending
const pendingRef = { plugin: 'matrixkoder', key: 'wakePending' } as const
const EMPTY_PENDING: Pending = { run: '', upTo: 0, items: [] }

/** Same rule as the dashboard and mk-state: MATRIXKODER_DATA, else the nearest .git (never above $HOME);
 *  a linked worktree resolves to its main checkout. Pure helpers are in data.ts ($ may not cross an import). */
async function resolveDataDir($: EngineInterface): Promise<string> {
  try {
    const envData = await $.env.get('MATRIXKODER_DATA')
    if (envData) return envData.trim()
  } catch {
    // env noun error
  }
  let cwd = '.'
  try {
    cwd = await $.session.cwd()
  } catch {
    // fallback
  }
  let home: string | undefined
  try {
    home = (await $.env.get('HOME')) || undefined
  } catch {
    // no HOME: the walk only stops at /
  }

  for (const dir of candidateDirs(cwd, home)) {
    const gitPath = join(dir, '.git')
    let kind: string | null = null
    try {
      kind = (await $.fs.stat(gitPath)).kind
    } catch {
      continue // no .git here — keep walking up
    }
    // nearest repo boundary: never use an ancestor's data
    let top = dir
    if (kind === 'file') {
      try {
        const gitdir = parseGitdir(await $.fs.read(gitPath))
        if (gitdir) {
          const gd = gitdir.startsWith('/') ? gitdir : join(dir, gitdir)
          const commondir = await $.fs.read(join(gd, 'commondir'))
          const main = mainCheckout(gd, commondir)
          if (main) top = main
        }
      } catch {
        // unreadable git file or no commondir: this checkout's own dir
      }
    }
    // the wizard's projects.json (home mode) names this repo's data dir; else <repo>/.matrixkoder
    try {
      let envHome: string | undefined
      try {
        envHome = (await $.env.get('MATRIXKODER_HOME')) || undefined
      } catch {
        // default global dir
      }
      const gdir = globalDirPath(envHome, home ?? null)
      if (gdir != null) {
        const listed = projectDataDir(parseJson(await $.fs.read(join(gdir, 'projects.json'))), [top], home ?? null)
        if (listed != null) return listed
      }
    } catch {
      // no projects.json
    }
    return join(top, '.matrixkoder')
  }

  return join(cwd, '.matrixkoder')
}

async function readPending($: EngineInterface): Promise<Pending> {
  const res = await $.state.get(pendingRef).catch(() => ({ value: undefined }))
  const v = res.value
  return v && Array.isArray(v.items) && typeof v.run === 'string' && typeof v.upTo === 'number' ? v : EMPTY_PENDING
}

function armDebounce($: EngineInterface): void {
  if (debounceTimer) return
  debounceTimer = $.clock.after(60000, () => {
    void flushBatch($)
  })
}

/** The task's CURRENT status at flush time: run.json is authoritative, else the task's
 *  own task.json copy (state-schema v1). undefined = neither is readable — the caller
 *  keeps the item, a read error must not silently eat a wake. */
async function currentTaskStatus($: EngineInterface, dataDir: string, run: string, task: string): Promise<string | undefined> {
  const runObj = parseJson(await $.fs.read(`${dataDir}/runs/${run}/run.json`).catch(() => ''))
  const tasks = runObj != null && typeof runObj === 'object' && Array.isArray((runObj as { tasks?: unknown }).tasks)
    ? (runObj as { tasks: unknown[] }).tasks
    : null
  if (tasks != null) {
    for (const t of tasks) {
      if (t != null && typeof t === 'object') {
        const o = t as { id?: unknown; status?: unknown }
        if (o.id === task && typeof o.status === 'string') return o.status
      }
    }
  }
  const taskObj = parseJson(await $.fs.read(`${dataDir}/runs/${run}/tasks/${task}/task.json`).catch(() => ''))
  if (taskObj != null && typeof taskObj === 'object') {
    const status = (taskObj as { status?: unknown }).status
    if (typeof status === 'string') return status
  }
  return undefined
}

/** The identical-text hold sits in the wakeSentAt family under the id `hold:<text>` (a family
 *  id is the only computed key the engine lists; the task ids the cooldown uses cannot collide
 *  with the `hold:` prefix). It holds the time of the last submit attempt of that exact text:
 *  a submit that threw may still have been queued by a busy session, so the identical text is
 *  never resent within HOLD_MS; a negative value is a hold that already gave up and blocks nothing. */
async function flushBatch($: EngineInterface): Promise<void> {
  debounceTimer = null
  const pend = await readPending($)
  if (pend.items.length === 0) return
  // Claim the batch before submitting: swap it for the sending sentinel (the
  // same batch with empty items — MkWakePending carries no extra fields), so
  // a concurrent flush (a second poller after a reload, a retry) that reads
  // the full batch before this write still loses: both write a claim token,
  // but only the flush whose token echoes back owns the batch. A flush that
  // arrives later still — holding a stale full read — meets the lease: a
  // claimAt fresher than one poll tick (25 s) means another flush owns the
  // batch. The lease must stay under the 30 s tick so the retry loop (one
  // re-flush per tick after a held or failed submit) is never locked out.
  const claimRef = { plugin: 'matrixkoder', key: 'wakeSentAt', id: `claim:${pend.run}:${pend.upTo}` } as const
  const claimAtRef = { plugin: 'matrixkoder', key: 'wakeSentAt', id: `claimAt:${pend.run}:${pend.upTo}` } as const
  let now = Date.now()
  try {
    now = await $.clock.now()
  } catch {
    // clock error
  }
  const lease = await $.state.get(claimAtRef).catch(() => ({ value: undefined }))
  if (typeof lease.value === 'number' && now - lease.value < 25000) return // another flush owns this batch
  const token = Math.random()
  await $.state.set(claimRef, token).catch(() => {})
  await $.state.set(claimAtRef, now).catch(() => {})
  await $.state.set(pendingRef, { run: pend.run, upTo: pend.upTo, items: [] }).catch(() => {})
  const echo = await $.state.get(claimRef).catch(() => ({ value: undefined }))
  if (typeof echo.value === 'number' && echo.value !== token) return // another flush owns this batch
  // The same batch (same run, same lines — the same text) may already have
  // been submitted: a duplicate re-delivery (a retry, a second poller) never
  // repeats it within HOLD_MS or ever after; the submit that sent it consumed
  // these lines already.
  const sentRef = { plugin: 'matrixkoder', key: 'wakeSentAt', id: `sent:${pend.run}:${pend.upTo}` } as const
  const sent = await $.state.get(sentRef).catch(() => ({ value: undefined }))
  if (typeof sent.value === 'number') {
    await $.state.set({ plugin: 'matrixkoder', key: 'wakeEventLines', id: pend.run } as const, pend.upTo).catch(() => {})
    await $.state.set(pendingRef, EMPTY_PENDING).catch(() => {})
    return
  }
  // A later status change may already have reversed a transition while the batch waited:
  // re-read each task's current status and drop items that no longer hold it.
  const dataDir = await resolveDataDir($)
  const items: Array<{ task: string; to: string }> = []
  for (const p of pend.items) {
    const status = await currentTaskStatus($, dataDir, pend.run, p.task)
    if (status === undefined || status === p.to) items.push(p)
  }
  if (items.length === 0) {
    // every transition was reversed: nothing to submit, but the lines are consumed
    await $.state.set({ plugin: 'matrixkoder', key: 'wakeEventLines', id: pend.run } as const, pend.upTo).catch(() => {})
    await $.state.set(pendingRef, EMPTY_PENDING).catch(() => {})
    return
  }
  const summary = items.map(p => `${p.task} → ${p.to}`).join(', ')
  const text = `MatrixKoder: ${summary}. Continue the run from its Next action.`

  const held = await $.state
    .get({ plugin: 'matrixkoder', key: 'wakeSentAt', id: `hold:${text}` } as const)
    .catch(() => ({ value: undefined }))
  const heldAt = typeof held.value === 'number' ? held.value : undefined
  if (heldAt !== undefined && heldAt >= 0) {
    if (now - heldAt < HOLD_MS) {
      await $.state.set(pendingRef, pend).catch(() => {}) // un-claim: the batch is still owed
      retryNext = true // keep the batch and the old line count; the next poll tick re-checks the hold
      return
    }
    // the hold expired: give up the held batch. The held text is keyed by the text, and
    // the text is a function of the items, so identical text means identical items — there
    // is no "newer batch with the same text" to distinguish (R3-L1). The held upTo is the
    // line count of the held batch; the give-up sets wakeEventLines to max(current, heldUpTo)
    // so the cursor never rewinds past already-consumed lines (R3-M1).
    const heldUpToRes = await $.state
      .get({ plugin: 'matrixkoder', key: 'wakeSentAt', id: `heldUpTo:${text}` } as const)
      .catch(() => ({ value: undefined }))
    const heldUpTo = typeof heldUpToRes.value === 'number' ? heldUpToRes.value : pend.upTo
    await $.state.set({ plugin: 'matrixkoder', key: 'wakeSentAt', id: `hold:${text}` } as const, HOLD_GONE).catch(() => {})
    await $.state.set({ plugin: 'matrixkoder', key: 'wakeSentAt', id: `heldUpTo:${text}` } as const, 0).catch(() => {})
    // R3-M1: never rewind the cursor. For a thrown submit the cursor never advanced past
    // heldUpTo (the lines stayed unconsumed), so heldUpTo is the right value. For a
    // successful-submit hold the cursor has usually moved past heldUpTo (later batches
    // already consumed their own lines), and writing heldUpTo would rewind it — the next
    // poll would then rescan those lines, and any non-held transition that was already
    // sent would be re-woken past its 5-min cooldown. Read the current cursor and take the
    // max with heldUpTo.
    const cursorRef = { plugin: 'matrixkoder', key: 'wakeEventLines', id: pend.run } as const
    const cursorRes = await $.state.get(cursorRef).catch(() => ({ value: undefined }))
    const currentCursor = typeof cursorRes.value === 'number' ? cursorRes.value : 0
    const nextCursor = Math.max(currentCursor, heldUpTo)
    await $.state.set(cursorRef, nextCursor).catch(() => {})
    await $.state.set(pendingRef, EMPTY_PENDING).catch(() => {})
    retryNext = false
    return
  }

  try {
    await $.prompt.submit({ text })
  } catch {
    // the submit may still have been queued: hold the identical text instead of ever resending it
    // R2-M1: the hold records the HELD ITEMS (sorted task:to list) so a give-up can decide
    // "newer batch" by content — a fresh transition in pending that the held batch lacked
    // means the new batch must go out. Bumping upTo on a note/heartbeat line does not end
    // the hold (those events are not wake content; upTo is just the line cursor).
    await $.state.set({ plugin: 'matrixkoder', key: 'wakeSentAt', id: `hold:${text}` } as const, now).catch(() => {})
    await $.state.set({ plugin: 'matrixkoder', key: 'wakeSentAt', id: `heldUpTo:${text}` } as const, pend.upTo).catch(() => {})
    await $.state.set(pendingRef, pend).catch(() => {}) // un-claim: the batch is still owed
    retryNext = true // keep the batch and the old line count; the next poll tick re-checks the hold
    return
  }
  retryNext = false
  for (const p of items) {
    await $.state
      .set({ plugin: 'matrixkoder', key: 'wakeSentAt', id: `${p.task}:${p.to}` } as const, now)
      .catch(() => {})
  }
  // M4: the identical-text 10-min window — the same prompt text is not submitted again
  // within 10 min of a successful submit (a duplicate re-delivery from a later batch is
  // already covered by the per-batch `sent:` guard above; this is the wider window). The
  // held upTo lets the give-up set wakeEventLines without rewinding past already-consumed
  // lines (R3-M1).
  await $.state.set({ plugin: 'matrixkoder', key: 'wakeSentAt', id: `hold:${text}` } as const, now).catch(() => {})
  await $.state.set({ plugin: 'matrixkoder', key: 'wakeSentAt', id: `heldUpTo:${text}` } as const, pend.upTo).catch(() => {})
  await $.state.set(sentRef, now).catch(() => {}) // the duplicate guard for this exact batch
  await $.state.set({ plugin: 'matrixkoder', key: 'wakeEventLines', id: pend.run } as const, pend.upTo).catch(() => {})
  await $.state.set(pendingRef, EMPTY_PENDING).catch(() => {})
}

async function pollEvents($: EngineInterface): Promise<void> {
  try {
    const dataDir = await resolveDataDir($)
    const indexPath = `${dataDir}/runs/index.json`
    let runs: unknown
    try {
      runs = JSON.parse(await $.fs.read(indexPath))
    } catch {
      runs = null
    }
    const active = Array.isArray(runs) ? (runs.find((r: { status?: string }) => r.status === 'active') as { id?: string } | undefined) : undefined
    // a batch found before a reload (or a held/failed submit) is still owed, but only to the run that is still active
    const owed = await readPending($)
    if (owed.items.length > 0 && (!active?.id || owed.run !== active.id)) {
      await $.state.set(pendingRef, EMPTY_PENDING).catch(() => {}) // a finished run must not wake the session
    } else if (owed.items.length > 0 && !debounceTimer) {
      if (retryNext) await flushBatch($)
      else armDebounce($)
    }
    if (!active?.id) return

    const eventsPath = `${dataDir}/runs/${active.id}/events.jsonl`
    const eventsRaw = await $.fs.read(eventsPath).catch(() => '')
    if (!eventsRaw) return

    const lines = eventsRaw.split('\n').filter(line => line.trim().length > 0)
    const stateRef = { plugin: 'matrixkoder', key: 'wakeEventLines', id: active.id } as const
    const stateRes = await $.state.get(stateRef).catch(() => ({ value: undefined }))

    if (typeof stateRes.value !== 'number') {
      // First enable or first time seeing this active run: baseline to current lines, do not wake for history
      await $.state.set(stateRef, lines.length).catch(() => {})
      return
    }

    const lastSeen = stateRes.value
    if (lines.length <= lastSeen) return

    let now = Date.now()
    try {
      now = await $.clock.now()
    } catch {
      // clock error
    }
    const newlyDiscovered: Array<{ task: string; to: string }> = []

    for (let i = lastSeen; i < lines.length; i++) {
      const line = lines[i]
      if (!line) continue
      try {
        const ev = JSON.parse(line)
        // Validate every value placed in prompt.submit:
        // task must be a string matching mk-state ID_RE; status must be in WAKE_STATUSES enum;
        // ignore orchestrator's own events to avoid self-wake loops.
        const isTransition = ev.type === 'transition'
        const isValidTask = typeof ev.task === 'string' && TASK_ID_RE.test(ev.task)
        const isValidTo = typeof ev.to === 'string' && WAKE_STATUSES.has(ev.to)
        const isNotOrchestrator = ev.by !== 'orchestrator'

        if (isTransition && isValidTask && isValidTo && isNotOrchestrator) {
          const throttleKey = `${ev.task}:${ev.to}`
          const sentRef = { plugin: 'matrixkoder', key: 'wakeSentAt', id: throttleKey } as const
          const sentRes = await $.state.get(sentRef).catch(() => ({ value: undefined }))
          const lastSent = typeof sentRes.value === 'number' ? sentRes.value : undefined

          if (lastSent === undefined || now - lastSent >= COOLDOWN_MS) {
            if (!newlyDiscovered.some(d => d.task === ev.task && d.to === ev.to)) {
              newlyDiscovered.push({ task: ev.task, to: ev.to })
            }
          }
        }
      } catch {
        // ignore corrupted/partial line
      }
    }

    const pend = await readPending($)
    if (pend.run !== '' && pend.items.length === 0) {
      // a flush holds this batch's claim (the sentinel kept run and upTo).
      // M1: a claim that is older than 2 minutes with no completed send is dead — the flush
      // died between the claim and the final write. The lease (25 s) only protects a batch
      // that still carries items, so the sentinel alone can stick forever. On a stale claim,
      // clear the sentinel and the claim keys, mark the retry-once flag, and let the next
      // tick re-discover the lines (the items are lost; the re-scan re-forms them with the
      // usual cooldown checks). A second stale-claim with the flag set gives up: lines are
      // consumed to wake-dead, not retried again.
      const claimAtRef = { plugin: 'matrixkoder', key: 'wakeSentAt', id: `claimAt:${pend.run}:${pend.upTo}` } as const
      const claimAtRes = await $.state.get(claimAtRef).catch(() => ({ value: undefined }))
      const claimAt = typeof claimAtRes.value === 'number' ? claimAtRes.value : undefined
      const stale = claimAt !== undefined && now - claimAt > CLAIM_DEAD_MS
      if (stale) {
        const staleFlagRef = { plugin: 'matrixkoder', key: 'wakeSentAt', id: `staleClaim:${pend.run}:${pend.upTo}` } as const
        const flagRes = await $.state.get(staleFlagRef).catch(() => ({ value: undefined }))
        const flagSet = typeof flagRes.value === 'number' && flagRes.value > 0
        if (flagSet) {
          // second-time stale: consume the lines, drop the sentinel, do not retry
          await $.state.set({ plugin: 'matrixkoder', key: 'wakeEventLines', id: pend.run } as const, pend.upTo).catch(() => {})
          await $.state.set(pendingRef, EMPTY_PENDING).catch(() => {})
          return
        }
        // first-time stale: clear the claim, drop the sentinel, and set the once-flag. Do NOT
        // reset wakeEventLines to 0 — it already holds the right resume point (the dead batch
        // never advanced it: only a successful submit moves it). Resetting to 0 re-scans the
        // whole run history and rewakes every transition that is still in a wake status
        // (the flush-time re-read only drops tasks that moved), producing a bogus prompt that
        // names tasks the held batch never carried.
        await $.state.set(claimAtRef, 0).catch(() => {})
        await $.state.set({ plugin: 'matrixkoder', key: 'wakeSentAt', id: `claim:${pend.run}:${pend.upTo}` } as const, 0).catch(() => {})
        await $.state.set(pendingRef, EMPTY_PENDING).catch(() => {})
        await $.state.set(staleFlagRef, 1).catch(() => {})
        return
      }
      // a flush holds this batch's claim (the sentinel kept run and upTo):
      // rewriting the batch now would resurrect it under the flush — the
      // lines stay unconsumed and re-enter the next tick's scan instead
      return
    }
    const items = [...pend.items]
    for (const item of newlyDiscovered) {
      if (!items.some(p => p.task === item.task && p.to === item.to)) items.push(item)
    }
    if (items.length === 0) {
      await $.state.set(stateRef, lines.length).catch(() => {}) // nothing to wake for: safe to move on
      return
    }
    // lines are NOT marked seen yet: that happens after the submit (flushBatch)
    await $.state.set(pendingRef, { run: active.id, upTo: lines.length, items }).catch(() => {})
    armDebounce($)
  } catch {
    // silent on fs/poll error
  }
}

export function registerWake(on: On, options: PluginOptions): void {
  if (!options.wake) return

  wakeTimers() // the shared registry must exist from registration on: a reload's re-registration reads it
  debounceTimer = null
  retryNext = false

  on('session.start', { cwd: /.*/ }, async ($, e, next) => {
    const timers = wakeTimers()
    timers.pollCancel?.() // the previous evaluation's poller (a reload) or this one's re-arm
    // The epoch is the reload guard the registry cannot give alone: $.state
    // survives a hot reload even where the reload lands in a fresh module
    // realm whose globalThis differs. Every registration bumps it; a poller
    // whose epoch is no longer current cancels itself before polling.
    const epochRef = { plugin: 'matrixkoder', key: 'wakeSentAt', id: 'pollEpoch' } as const
    const res = await $.state.get(epochRef).catch(() => ({ value: undefined }))
    const epoch = (typeof res.value === 'number' ? res.value : 0) + 1
    await $.state.set(epochRef, epoch).catch(() => {})
    const timer = $.clock.every(30000, () => {
      void (async () => {
        const cur = await $.state.get(epochRef).catch(() => ({ value: undefined }))
        // an unreadable epoch never cancels (a read error must not eat the
        // poller, the same rule currentTaskStatus follows); a newer one does
        if (typeof cur.value === 'number' && cur.value !== epoch) {
          timer.cancel() // a newer registration owns the poller now
          return
        }
        await pollEvents($)
      })()
    })
    timers.pollCancel = () => timer.cancel()
    return next(e)
  })
}
