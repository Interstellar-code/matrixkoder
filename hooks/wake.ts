// MatrixKoder wake on lane events: polls events.jsonl and submits prompt on transition events
import type { EngineInterface, On, PluginOptions, Timer } from 'claude-code'

// $.state contract for wakeEventLines and wakeSentAt lives in types/index.d.ts

const WAKE_STATUSES = new Set(['verifying', 'blocked', 'quota', 'stalled', 'provider'])
const TASK_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const COOLDOWN_MS = 5 * 60 * 1000 // 5 minutes

let pendingBatch: Array<{ task: string; to: string }> = []
let debounceTimer: Timer | null = null
let pollTimer: Timer | null = null

function candidateDirs(from: string): string[] {
  const out: string[] = []
  let dir = from.replace(/\/+$/, '') || '/'
  for (;;) {
    out.push(dir)
    if (dir === '/') return out
    const cut = dir.lastIndexOf('/')
    dir = cut <= 0 ? '/' : dir.slice(0, cut)
  }
}

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

  for (const dir of candidateDirs(cwd)) {
    const gitPath = `${dir}/.git`
    let gitStat: { kind: string } | null = null
    try {
      gitStat = await $.fs.stat(gitPath)
    } catch {
      // not here
    }

    if (gitStat) {
      // Found nearest .git! Do not walk past it.
      if (gitStat.kind === 'file') {
        try {
          const gitContent = await $.fs.read(gitPath)
          const m = gitContent.match(/gitdir:\s*(.+)/)
          if (m) {
            let gitdir = m[1].trim()
            if (!gitdir.startsWith('/')) {
              gitdir = `${dir}/${gitdir}`
            }
            const commondirContent = await $.fs.read(`${gitdir}/commondir`).catch(() => '')
            if (commondirContent) {
              let common = commondirContent.trim()
              if (!common.startsWith('/')) {
                common = `${gitdir}/${common}`
              }
              const parts = common.split('/').filter(Boolean)
              if (parts[parts.length - 1] === '.git') {
                parts.pop()
                return `/${parts.join('/')}/.matrixkoder`
              }
            }
          }
        } catch {
          // unreadable git file
        }
      }
      return `${dir}/.matrixkoder`
    }
  }

  return `${cwd}/.matrixkoder`
}

async function flushBatch($: EngineInterface): Promise<void> {
  if (pendingBatch.length === 0) return
  let now = Date.now()
  try {
    now = await $.clock.now()
  } catch {
    // clock error
  }
  const summary = pendingBatch.map(p => `${p.task} → ${p.to}`).join(', ')
  const text = `MatrixKoder: ${summary}. Continue the run from its Next action.`

  try {
    await $.prompt.submit({ text })
    for (const p of pendingBatch) {
      const throttleKey = `${p.task}:${p.to}`
      await $.state
        .set({ plugin: 'matrixkoder', key: 'wakeSentAt', id: throttleKey } as const, now)
        .catch(() => {})
    }
    pendingBatch = []
  } catch {
    // Retain pendingBatch on submit failure for retry
  } finally {
    debounceTimer = null
  }
}

async function pollEvents($: EngineInterface): Promise<void> {
  try {
    const dataDir = await resolveDataDir($)
    const indexPath = `${dataDir}/runs/index.json`
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
      try {
        const ev = JSON.parse(lines[i])
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

    await $.state.set(stateRef, lines.length).catch(() => {})

    if (newlyDiscovered.length > 0) {
      for (const item of newlyDiscovered) {
        if (!pendingBatch.some(p => p.task === item.task && p.to === item.to)) {
          pendingBatch.push(item)
        }
      }

      if (!debounceTimer) {
        debounceTimer = $.clock.after(60000, () => {
          void flushBatch($)
        })
      }
    }
  } catch {
    // silent on fs/poll error
  }
}

export function registerWake(on: On, options: PluginOptions): void {
  if (!options.wake) return

  pendingBatch = []
  debounceTimer = null

  on('session.start', { cwd: /.*/ }, async ($, e, next) => {
    pollTimer?.cancel()
    pollTimer = $.clock.every(30000, () => {
      void pollEvents($)
    })
    return next(e)
  })
}
