// MatrixKoder wake on lane events: polls events.jsonl and submits prompt on transition events
import type { EngineInterface, On, PluginOptions, StateFamily, Timer } from 'claude-code'

// $.state contract for wakeEventLines lives in types/index.d.ts

const WAKE_STATUSES = new Set(['verifying', 'blocked', 'quota', 'stalled', 'provider'])
const COOLDOWN_MS = 5 * 60 * 1000 // 5 minutes

let pendingBatch: Array<{ task: string; to: string }> = []
const lastSubmitted = new Map<string, number>()
let debounceTimer: Timer | null = null

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
      return `${dir}/.matrixkoder`
    } catch {
      // .git is not a file
    }

    try {
      const stat = await $.fs.stat(`${dir}/.matrixkoder`)
      if (stat.kind === 'dir') return `${dir}/.matrixkoder`
    } catch {
      // not here
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

  for (const p of pendingBatch) {
    lastSubmitted.set(`${p.task}:${p.to}`, now)
  }
  pendingBatch = []
  debounceTimer = null

  try {
    await $.prompt.submit({ text })
  } catch {
    // ignore submit failure if session is unready
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
    const stateRes = await $.state.get(stateRef).catch(() => ({ value: 0 }))
    const lastSeen = typeof stateRes.value === 'number' ? stateRes.value : 0

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
        if (ev.type === 'transition' && ev.to && WAKE_STATUSES.has(ev.to) && ev.task) {
          const throttleKey = `${ev.task}:${ev.to}`
          const lastTime = lastSubmitted.get(throttleKey)
          if (lastTime === undefined || now - lastTime >= COOLDOWN_MS) {
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

      debounceTimer?.cancel()
      debounceTimer = $.clock.after(60000, () => {
        void flushBatch($)
      })
    }
  } catch {
    // silent on fs/poll error
  }
}

export function registerWake(on: On, options: PluginOptions): void {
  if (!options.wake) return

  pendingBatch = []
  lastSubmitted.clear()
  debounceTimer = null

  on('session.start', { cwd: /.*/ }, async ($, e, next) => {
    $.clock.every(30000, () => {
      void pollEvents($)
    })
    return next(e)
  })
}
