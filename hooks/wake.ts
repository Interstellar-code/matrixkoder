// MatrixKoder wake on lane events: polls events.jsonl and submits prompt on transition events (strict types)
import type { EngineInterface, On, PluginOptions, Timer } from 'claude-code'
import type { MkWakePending } from '../types'
import { candidateDirs, join, mainCheckout, parseGitdir } from './data'

// $.state contract for wakeEventLines and wakeSentAt lives in types/index.d.ts

const WAKE_STATUSES = new Set(['verifying', 'blocked', 'quota', 'stalled', 'provider'])
const TASK_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const COOLDOWN_MS = 5 * 60 * 1000 // 5 minutes

let debounceTimer: Timer | null = null
let pollTimer: Timer | null = null
let retryNext = false // a submit failed: the next poll tick retries at once

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
    if (kind === 'file') {
      try {
        const gitdir = parseGitdir(await $.fs.read(gitPath))
        if (gitdir) {
          const gd = gitdir.startsWith('/') ? gitdir : join(dir, gitdir)
          const commondir = await $.fs.read(join(gd, 'commondir'))
          const main = mainCheckout(gd, commondir)
          if (main) return join(main, '.matrixkoder')
        }
      } catch {
        // unreadable git file or no commondir: this checkout's own dir
      }
    }
    return join(dir, '.matrixkoder')
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

/** Submits the persisted batch. The seen-line count moves only after the submit resolved. */
async function flushBatch($: EngineInterface): Promise<void> {
  debounceTimer = null
  const pend = await readPending($)
  if (pend.items.length === 0) return
  let now = Date.now()
  try {
    now = await $.clock.now()
  } catch {
    // clock error
  }
  const summary = pend.items.map(p => `${p.task} → ${p.to}`).join(', ')
  const text = `MatrixKoder: ${summary}. Continue the run from its Next action.`

  try {
    await $.prompt.submit({ text })
  } catch {
    retryNext = true // keep the batch and the old line count; the next poll tick retries
    return
  }
  retryNext = false
  for (const p of pend.items) {
    await $.state
      .set({ plugin: 'matrixkoder', key: 'wakeSentAt', id: `${p.task}:${p.to}` } as const, now)
      .catch(() => {})
  }
  await $.state.set({ plugin: 'matrixkoder', key: 'wakeEventLines', id: pend.run } as const, pend.upTo).catch(() => {})
  await $.state.set(pendingRef, EMPTY_PENDING).catch(() => {})
}

async function pollEvents($: EngineInterface): Promise<void> {
  try {
    const dataDir = await resolveDataDir($)
    const indexPath = `${dataDir}/runs/index.json`
    // a batch found before a reload (or a failed submit) is still owed
    const owed = await readPending($)
    if (owed.items.length > 0 && !debounceTimer) {
      if (retryNext) await flushBatch($)
      else armDebounce($)
    }
    const indexRaw = await $.fs.read(indexPath)
    const runs = JSON.parse(indexRaw)
    if (!Array.isArray(runs)) return

    const active = runs.find((r: any) => r.status === 'active')
    if (!active || !active.id) return

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
    if (pend.items.length > 0 && pend.run !== active.id) return // the owed batch belongs to another run; flush it first
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

  debounceTimer = null
  retryNext = false

  on('session.start', { cwd: /.*/ }, async ($, e, next) => {
    pollTimer?.cancel()
    pollTimer = $.clock.every(30000, () => {
      void pollEvents($)
    })
    return next(e)
  })
}
