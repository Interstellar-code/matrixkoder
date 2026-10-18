// The /mk pane's Files tab data layer (plan filetree-tab-ralplan, phase F2, task F2.1).
// Pure data transforms: walk a root breadth-first with entry and listing caps,
// turn listings into sorted tree rows with search filtering and activity rollup,
// diff walks for changed files, and list root options.
import type { FsEntry } from 'claude-code'

export type { FsEntry }

/** Folders skipped at any depth during walkRoot. */
export const SCAN_SKIP: readonly string[] = [
  '.omc',
  '.git',
  '.matrixkoder',
  'node_modules',
  'dist',
  'build',
  'target',
  '.venv',
  'venv',
  '__pycache__',
  '.next',
  '.turbo',
  '.cache',
  'coverage',
  '.ruff_cache',
  '.serena',
  '.omp',
  '.mypy_cache',
  '.pytest_cache',
] as const

/** Prefix of folders skipped during walk and files ignored during diffWalk. */
export const SCAN_SKIP_PREFIX = '.matrixkoder-'

const SCAN_SKIP_SET = new Set<string>(SCAN_SKIP)

export type Walk = {
  dirs: Map<string, FsEntry[]>
  capped: boolean
  missing: boolean
}

export type Row = {
  key: string
  path: string
  name: string
  depth: number
  dir: boolean
  open: boolean
  hidden: boolean
  link: boolean
  icon: 'read' | 'write' | null
  count: number
  note?: 'more' | 'empty' | 'unlisted'
}

export type RootItem = {
  id: string
  label: string
  path: string
}

function joinPath(dir: string, name: string): string {
  return dir.endsWith('/') ? `${dir}${name}` : `${dir}/${name}`
}

function getParent(p: string): string {
  const idx = p.lastIndexOf('/')
  return idx <= 0 ? '/' : p.slice(0, idx)
}

function compareEntries(a: FsEntry, b: FsEntry): number {
  const aDir = a.kind === 'dir'
  const bDir = b.kind === 'dir'
  if (aDir !== bDir) return aDir ? -1 : 1
  const cmp = a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })
  return cmp !== 0 ? cmp : a.name.localeCompare(b.name)
}

function folderActivity(
  folderPath: string,
  act: Map<string, 'read' | 'write'>,
): { icon: 'read' | 'write' | null; count: number } {
  const prefix = folderPath.endsWith('/') ? folderPath : `${folderPath}/`
  let hasWrite = false
  let hasRead = false
  let count = 0
  for (const [p, kind] of act) {
    if (p.startsWith(prefix)) {
      count++
      if (kind === 'write') hasWrite = true
      else if (kind === 'read') hasRead = true
    }
  }
  const icon = hasWrite ? 'write' : hasRead ? 'read' : null
  return { icon, count }
}

/**
 * Lists the root breadth-first through list function.
 * Stops listing at 2000 entries or 300 folder listings, whichever comes first.
 * Never enters folders in SCAN_SKIP, folders starting with SCAN_SKIP_PREFIX, or kind other entries.
 */
export async function walkRoot(
  root: string,
  list: (dir: string) => Promise<FsEntry[]>,
): Promise<Walk> {
  const dirs = new Map<string, FsEntry[]>()
  let listCalls = 0
  let totalEntries = 0
  let capped = false

  const queue: string[] = [root]

  while (queue.length > 0) {
    if (totalEntries >= 2000 || listCalls >= 300) {
      capped = true
      break
    }

    const current = queue.shift()!
    listCalls++
    let entries: FsEntry[]
    try {
      entries = await list(current)
    } catch {
      if (current === root) {
        return { dirs: new Map(), capped: false, missing: true }
      }
      if (listCalls >= 300) {
        capped = true
        break
      }
      continue
    }

    dirs.set(current, entries)
    totalEntries += entries.length

    if (totalEntries >= 2000 || listCalls >= 300) {
      capped = true
      break
    }

    for (const entry of entries) {
      if (entry.kind === 'dir') {
        if (!SCAN_SKIP_SET.has(entry.name) && !entry.name.startsWith(SCAN_SKIP_PREFIX)) {
          queue.push(joinPath(current, entry.name))
        }
      }
    }
  }

  return { dirs, capped, missing: false }
}

function computeKeptPaths(
  walk: Walk,
  root: string,
  needle: string,
): { kept: Set<string>; openFolders: Set<string> } {
  const kept = new Set<string>()
  const openFolders = new Set<string>()

  function checkDir(dirPath: string) {
    const entries = walk.dirs.get(dirPath)
    if (!entries) return
    for (const entry of entries) {
      if (entry.name === '.git') continue
      const childPath = joinPath(dirPath, entry.name)
      const matches = entry.name.toLowerCase().includes(needle)
      if (matches) {
        kept.add(childPath)
        let cur = dirPath
        while (cur !== root && cur.startsWith(root)) {
          kept.add(cur)
          openFolders.add(cur)
          cur = getParent(cur)
        }
        openFolders.add(root)
      }
      if (entry.kind === 'dir' && walk.dirs.has(childPath)) {
        checkDir(childPath)
      }
    }
  }

  checkDir(root)
  return { kept, openFolders }
}

/**
 * Turns the walk, open set, search query, and activity into tree rows.
 * At most 500 entries per folder, at most 600 total rows.
 */
export function treeRows(
  walk: Walk,
  root: string,
  open: ReadonlySet<string>,
  q: string,
  act: Map<string, 'read' | 'write'>,
): Row[] {
  if (!walk.dirs.has(root)) {
    return []
  }

  const searchActive = q.length > 0 && q.trim().length > 0
  const needle = q.trim().toLowerCase()
  let kept = new Set<string>()
  let openFolders = new Set<string>()

  if (searchActive) {
    const searchRes = computeKeptPaths(walk, root, needle)
    kept = searchRes.kept
    openFolders = searchRes.openFolders
    if (kept.size === 0) {
      return []
    }
  }

  const rows: Row[] = []

  function renderDir(dirPath: string, depth: number) {
    if (!walk.dirs.has(dirPath)) {
      rows.push({
        key: `files-unlisted:${dirPath}`,
        path: dirPath,
        name: '(not listed)',
        depth,
        dir: false,
        open: false,
        hidden: false,
        link: false,
        icon: null,
        count: 0,
        note: 'unlisted',
      })
      return
    }

    let entries = walk.dirs.get(dirPath)!.filter(e => e.name !== '.git')
    if (searchActive) {
      entries = entries.filter(e => kept.has(joinPath(dirPath, e.name)))
    }

    if (entries.length === 0) {
      rows.push({
        key: `files-empty:${dirPath}`,
        path: dirPath,
        name: '(empty)',
        depth,
        dir: false,
        open: false,
        hidden: false,
        link: false,
        icon: null,
        count: 0,
        note: 'empty',
      })
      return
    }

    entries.sort(compareEntries)
    const total = entries.length
    const slice = entries.slice(0, 500)

    for (const entry of slice) {
      const childPath = joinPath(dirPath, entry.name)
      if (entry.kind === 'dir') {
        const isOpen = searchActive ? openFolders.has(childPath) : open.has(childPath)
        const { icon, count } = isOpen ? { icon: null, count: 0 } : folderActivity(childPath, act)
        rows.push({
          key: `files-row:${childPath}`,
          path: childPath,
          name: entry.name,
          depth,
          dir: true,
          open: isOpen,
          hidden: entry.name.startsWith('.'),
          link: false,
          icon,
          count,
        })
        if (isOpen) {
          renderDir(childPath, depth + 1)
        }
      } else {
        const isLink = entry.kind === 'other'
        const icon = act.get(childPath) ?? null
        rows.push({
          key: `files-row:${childPath}`,
          path: childPath,
          name: entry.name,
          depth,
          dir: false,
          open: false,
          hidden: entry.name.startsWith('.'),
          link: isLink,
          icon,
          count: 0,
        })
      }
    }

    if (total > 500) {
      rows.push({
        key: `files-more:${dirPath}`,
        path: dirPath,
        name: 'more',
        depth,
        dir: false,
        open: false,
        hidden: false,
        link: false,
        icon: null,
        count: total - 500,
        note: 'more',
      })
    }
  }

  // Root level rendering
  let rootEntries = walk.dirs.get(root)!.filter(e => e.name !== '.git')
  if (searchActive) {
    rootEntries = rootEntries.filter(e => kept.has(joinPath(root, e.name)))
  }

  if (rootEntries.length === 0) {
    if (!searchActive) {
      rows.push({
        key: `files-empty:${root}`,
        path: root,
        name: '(empty)',
        depth: 0,
        dir: false,
        open: false,
        hidden: false,
        link: false,
        icon: null,
        count: 0,
        note: 'empty',
      })
    }
  } else {
    rootEntries.sort(compareEntries)
    const total = rootEntries.length
    const slice = rootEntries.slice(0, 500)

    for (const entry of slice) {
      const childPath = joinPath(root, entry.name)
      if (entry.kind === 'dir') {
        const isOpen = searchActive ? openFolders.has(childPath) : open.has(childPath)
        const { icon, count } = isOpen ? { icon: null, count: 0 } : folderActivity(childPath, act)
        rows.push({
          key: `files-row:${childPath}`,
          path: childPath,
          name: entry.name,
          depth: 0,
          dir: true,
          open: isOpen,
          hidden: entry.name.startsWith('.'),
          link: false,
          icon,
          count,
        })
        if (isOpen) {
          renderDir(childPath, 1)
        }
      } else {
        const isLink = entry.kind === 'other'
        const icon = act.get(childPath) ?? null
        rows.push({
          key: `files-row:${childPath}`,
          path: childPath,
          name: entry.name,
          depth: 0,
          dir: false,
          open: false,
          hidden: entry.name.startsWith('.'),
          link: isLink,
          icon,
          count: 0,
        })
      }
    }

    if (total > 500) {
      rows.push({
        key: `files-more:${root}`,
        path: root,
        name: 'more',
        depth: 0,
        dir: false,
        open: false,
        hidden: false,
        link: false,
        icon: null,
        count: total - 500,
        note: 'more',
      })
    }
  }

  // 600 total row cap
  if (rows.length > 600) {
    const countLeftOut = rows.length - 599
    rows.length = 599
    rows.push({
      key: 'files-more-cap',
      path: root,
      name: 'more',
      depth: 0,
      dir: false,
      open: false,
      hidden: false,
      link: false,
      icon: null,
      count: countLeftOut,
      note: 'more',
    })
  }

  return rows
}

/**
 * Reports files that are new or whose mtimeMs grew, but only inside folders both walks listed.
 * Never reports files starting with SCAN_SKIP_PREFIX.
 */
export function diffWalk(prev: Walk | undefined, next: Walk): string[] {
  if (!prev) return []
  const out: string[] = []

  for (const [dir, nextEntries] of next.dirs) {
    if (!prev.dirs.has(dir)) continue
    const prevEntries = prev.dirs.get(dir)!
    const prevMap = new Map<string, FsEntry>()
    for (const pe of prevEntries) {
      prevMap.set(pe.name, pe)
    }

    for (const ne of nextEntries) {
      if (ne.kind !== 'file') continue
      if (ne.name.startsWith(SCAN_SKIP_PREFIX)) continue
      const pe = prevMap.get(ne.name)
      if (!pe || ne.mtimeMs > pe.mtimeMs) {
        out.push(joinPath(dir, ne.name))
      }
    }
  }

  return out
}

/**
 * Lists the main checkout first (id: 'main', label: 'repo') and one root per lane slot in slot order.
 */
export function rootsOf(
  main: string,
  slots: readonly { slot: number; path: string; task: string | null }[],
): RootItem[] {
  const mainRoot: RootItem = { id: 'main', label: 'repo', path: main }
  const seenSlots = new Set<number>()
  const sorted = [...slots].sort((a, b) => a.slot - b.slot)
  const slotRoots: RootItem[] = []

  for (const s of sorted) {
    if (seenSlots.has(s.slot)) continue
    seenSlots.add(s.slot)
    const label = s.task && s.task.trim() ? s.task.trim() : `slot ${s.slot}`
    slotRoots.push({
      id: `slot-${s.slot}`,
      label,
      path: s.path,
    })
  }

  return [mainRoot, ...slotRoots]
}

/**
 * Returns chosen root, or the main checkout when chosen id is not in the list.
 */
export function pickRoot(
  roots: readonly RootItem[],
  chosen: string | undefined | null,
): RootItem {
  if (chosen) {
    const found = roots.find(r => r.id === chosen)
    if (found) return found
  }
  const main = roots.find(r => r.id === 'main')
  if (main) return main
  if (roots.length > 0 && roots[0]) return roots[0]
  return { id: 'main', label: 'repo', path: '' }
}
