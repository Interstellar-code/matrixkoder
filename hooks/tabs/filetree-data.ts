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
  /** bytes (files) and last change; absent on note rows */
  size?: number
  mtimeMs?: number
}

/** The column the tree is sorted by and its direction; folders always list first. */
export type SortSpec = { by: 'name' | 'size' | 'date'; desc: boolean }

export type RootItem = {
  id: string
  label: string
  path: string
  /** a lane slot with no task: its chip is drawn dim */
  idle?: boolean
}

function joinPath(dir: string, name: string): string {
  return dir.endsWith('/') ? `${dir}${name}` : `${dir}/${name}`
}

function getParent(p: string): string {
  const idx = p.lastIndexOf('/')
  return idx <= 0 ? '/' : p.slice(0, idx)
}

/** Absolute paths of the git-ignored folders in `git ls-files --others --ignored --exclude-standard --directory` output. */
export function ignoredDirs(stdout: string, root: string): Set<string> {
  const out = new Set<string>()
  for (const line of stdout.split('\n')) {
    if (line.endsWith('/')) out.add(joinPath(root, line.slice(0, -1)))
  }
  return out
}

/** The host reports a folder's change time in seconds and a file's in milliseconds; anything below 1e11 is seconds. */
export function normMs(ms: number): number {
  return ms < 1e11 ? ms * 1000 : ms
}

/** `git status --porcelain` as { path relative to the root: one-letter mark }; an untracked folder keeps its trailing slash. */
export function gitStatusMap(stdout: string): Map<string, string> {
  const out = new Map<string, string>()
  for (const line of stdout.split('\n')) {
    if (line.length < 4) continue
    const xy = line.slice(0, 2)
    let path = line.slice(3)
    const arrow = path.indexOf(' -> ')
    if (arrow >= 0) path = path.slice(arrow + 4)
    path = path.replace(/^"|"$/g, '')
    const mark = xy === '??' ? '?' : xy.includes('D') ? 'D' : xy.includes('A') ? 'A' : xy.includes('R') ? 'R' : 'M'
    out.set(path, mark)
  }
  return out
}

function compareEntries(sort: SortSpec): (a: FsEntry, b: FsEntry) => number {
  const byName = (a: FsEntry, b: FsEntry): number => {
    const cmp = a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })
    return cmp !== 0 ? cmp : a.name.localeCompare(b.name)
  }
  return (a, b) => {
    const aDir = a.kind === 'dir'
    const bDir = b.kind === 'dir'
    if (aDir !== bDir) return aDir ? -1 : 1
    // ascending is A to Z, smallest first, oldest first; a folder has no size, so it falls back to its name
    const key = sort.by === 'date' ? normMs(a.mtimeMs) - normMs(b.mtimeMs) : sort.by === 'size' && !aDir ? a.size - b.size : 0
    return (key !== 0 ? key : byName(a, b)) * (sort.desc ? -1 : 1)
  }
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
  skip: ReadonlySet<string> = new Set(),
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
        if (!SCAN_SKIP_SET.has(entry.name) && !entry.name.startsWith(SCAN_SKIP_PREFIX) && !skip.has(joinPath(current, entry.name))) {
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
  sort: SortSpec = { by: 'name', desc: false },
): Row[] {
  const cmp = compareEntries(sort)
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
        name: '(not scanned)',
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

    entries.sort(cmp)
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
          size: entry.size,
          mtimeMs: normMs(entry.mtimeMs),
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
          size: entry.size,
          mtimeMs: normMs(entry.mtimeMs),
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
    rootEntries.sort(cmp)
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
          size: entry.size,
          mtimeMs: normMs(entry.mtimeMs),
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
          size: entry.size,
          mtimeMs: normMs(entry.mtimeMs),
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
 * Lists the main checkout first (id: 'main', label: 'main') and one root per lane slot in slot order.
 */
export function rootsOf(
  main: string,
  slots: readonly { slot: number; path: string; task: string | null }[],
): RootItem[] {
  const mainRoot: RootItem = { id: 'main', label: 'main', path: main }
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
      ...(s.task && s.task.trim() ? {} : { idle: true }),
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
  return { id: 'main', label: 'main', path: '' }
}
