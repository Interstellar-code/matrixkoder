import { test, expect } from 'claude-code/testing'
import type { FsEntry } from 'claude-code'
import {
  SCAN_SKIP,
  SCAN_SKIP_PREFIX,
  walkRoot,
  treeRows,
  diffWalk,
  rootsOf,
  gitStatusMap,
  ignoredDirs,
  pickRoot,
  type Walk,
  type Row,
} from './filetree-data'

function makeEntry(name: string, kind: 'file' | 'dir' | 'other' = 'file', size = 100, mtimeMs = 1000, isLink = kind === 'other'): FsEntry {
  return { name, kind, size, mtimeMs, isLink }
}

test('Files F2.1: walkRoot caps at 2000 entries and 300 folder listings, keeping listings whole', async () => {
  // Test 1: 1000 empty folders directly under root
  // Root listing returns 1000 directories.
  // Each child folder returns 0 entries.
  // Total list calls should reach 300 (1 root + 299 children) and set capped = true.
  let callCount = 0
  const listMock = async (dir: string): Promise<FsEntry[]> => {
    callCount++
    if (dir === '/root') {
      const folders: FsEntry[] = []
      for (let i = 0; i < 1000; i++) {
        folders.push(makeEntry(`sub-${i}`, 'dir', 0, 0))
      }
      return folders
    }
    return []
  }

  const walk1 = await walkRoot('/root', listMock)
  expect(callCount).toBe(300)
  expect(walk1.capped).toBe(true)
  expect(walk1.missing).toBe(false)
  expect(walk1.dirs.get('/root')?.length).toBe(1000)

  // Test 2: one folder of 5000 entries is kept whole and capped is set
  let callCount2 = 0
  const list5000 = async (dir: string): Promise<FsEntry[]> => {
    callCount2++
    const entries: FsEntry[] = []
    for (let i = 0; i < 5000; i++) {
      entries.push(makeEntry(`file-${i}.txt`, 'file', 50, 1000))
    }
    return entries
  }

  const walk2 = await walkRoot('/big', list5000)
  expect(callCount2).toBe(1)
  expect(walk2.capped).toBe(true)
  expect(walk2.missing).toBe(false)
  expect(walk2.dirs.get('/big')?.length).toBe(5000)
})

test('Files F2.1: SCAN_SKIP and SCAN_SKIP_PREFIX folders and kind other are never listed, but dot folders like .github are walked', async () => {
  const expectedSkip = [
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
  ]
  expect(SCAN_SKIP).toEqual(expectedSkip)
  expect(SCAN_SKIP_PREFIX).toBe('.matrixkoder-')

  const listedDirs: string[] = []
  const treeMap: Record<string, FsEntry[]> = {
    '/repo': [
      makeEntry('.git', 'dir', 0, 0),
      makeEntry('node_modules', 'dir', 0, 0),
      makeEntry('.matrixkoder', 'dir', 0, 0),
      makeEntry('.matrixkoder-status.json', 'file', 10, 100),
      makeEntry('.matrixkoder-old-1', 'dir', 0, 0),
      makeEntry('dist', 'dir', 0, 0),
      makeEntry('.github', 'dir', 0, 0),
      makeEntry('symlink', 'other', 0, 0),
      makeEntry('src', 'dir', 0, 0),
    ],
    '/repo/.github': [
      makeEntry('workflows', 'dir', 0, 0),
    ],
    '/repo/.github/workflows': [
      makeEntry('ci.yml', 'file', 200, 1000),
    ],
    '/repo/src': [
      makeEntry('index.ts', 'file', 300, 1000),
    ],
  }

  const listMock = async (dir: string): Promise<FsEntry[]> => {
    listedDirs.push(dir)
    return treeMap[dir] ?? []
  }

  const walk = await walkRoot('/repo', listMock)
  expect(listedDirs).toContain('/repo')
  expect(listedDirs).toContain('/repo/.github')
  expect(listedDirs).toContain('/repo/.github/workflows')
  expect(listedDirs).toContain('/repo/src')

  // Real skip names and kind other are never entered/listed:
  expect(listedDirs).not.toContain('/repo/.git')
  expect(listedDirs).not.toContain('/repo/node_modules')
  expect(listedDirs).not.toContain('/repo/.matrixkoder')
  expect(listedDirs).not.toContain('/repo/.matrixkoder-old-1')
  expect(listedDirs).not.toContain('/repo/dist')
  expect(listedDirs).not.toContain('/repo/symlink')
  expect(walk.dirs.has('/repo')).toBe(true)
  expect(walk.dirs.has('/repo/.github')).toBe(true)
  expect(walk.dirs.has('/repo/src')).toBe(true)
})

test('Files F2.1: throwing listing leaves folder absent from dirs and root throw returns missing', async () => {
  const listMock = async (dir: string): Promise<FsEntry[]> => {
    if (dir === '/root/broken') {
      throw new Error('EACCES permission denied')
    }
    if (dir === '/root') {
      return [
        makeEntry('broken', 'dir', 0, 0),
        makeEntry('ok', 'dir', 0, 0),
      ]
    }
    if (dir === '/root/ok') {
      return [makeEntry('file.txt', 'file', 10, 1000)]
    }
    return []
  }

  const walk = await walkRoot('/root', listMock)
  expect(walk.missing).toBe(false)
  expect(walk.dirs.has('/root')).toBe(true)
  expect(walk.dirs.has('/root/ok')).toBe(true)
  // broken is ABSENT from dirs (never dirs.set(folder, []))
  expect(walk.dirs.has('/root/broken')).toBe(false)

  // Throw on root returns missing: true
  const listRootThrow = async (): Promise<FsEntry[]> => {
    throw new Error('ENOENT no such file or directory')
  }
  const rootWalk = await walkRoot('/nonexistent', listRootThrow)
  expect(rootWalk.missing).toBe(true)
  expect(rootWalk.dirs.size).toBe(0)
  expect(rootWalk.capped).toBe(false)
})

test('Files F2.1: folders come before files case-insensitive, .git is never a row, dot names flagged hidden, skipped folders and links show', () => {
  const dirs = new Map<string, FsEntry[]>()
  dirs.set('/repo', [
    makeEntry('zoo.txt', 'file'),
    makeEntry('Alpha', 'dir'),
    makeEntry('.git', 'dir'),
    makeEntry('.github', 'dir'),
    makeEntry('.omc', 'dir'),
    makeEntry('.matrixkoder-status.json', 'file'),
    makeEntry('beta', 'dir'),
    makeEntry('apple.txt', 'file'),
    makeEntry('symlink', 'other'),
  ])

  const walk: Walk = { dirs, capped: false, missing: false }
  const rows = treeRows(walk, '/repo', new Set<string>(), '', new Map())

  // .git is never a row
  expect(rows.some(r => r.name === '.git')).toBe(false)

  // folders come before files, both by name case-insensitive:
  // Folders: Alpha, .github, .omc, beta -> sorted: .github, .omc, Alpha, beta
  // Files/links: apple.txt, .matrixkoder-status.json, symlink, zoo.txt -> sorted: .matrixkoder-status.json, apple.txt, symlink, zoo.txt
  const names = rows.map(r => r.name)
  const dirNames = rows.filter(r => r.dir).map(r => r.name)
  const fileNames = rows.filter(r => !r.dir).map(r => r.name)

  expect(dirNames).toEqual(['.github', '.omc', 'Alpha', 'beta'])
  expect(fileNames).toEqual(['.matrixkoder-status.json', 'apple.txt', 'symlink', 'zoo.txt'])

  // First 4 rows are folders, last 4 are files/links
  expect(names).toEqual([...dirNames, ...fileNames])

  // Dot names flagged hidden
  const dotGithub = rows.find(r => r.name === '.github')!
  expect(dotGithub.hidden).toBe(true)
  const dotStatus = rows.find(r => r.name === '.matrixkoder-status.json')!
  expect(dotStatus.hidden).toBe(true)
  const alpha = rows.find(r => r.name === 'Alpha')!
  expect(alpha.hidden).toBe(false)

  // Skipped folder and link show as rows
  const omcRow = rows.find(r => r.name === '.omc')!
  expect(omcRow.dir).toBe(true)
  const linkRow = rows.find(r => r.name === 'symlink')!
  expect(linkRow.link).toBe(true)
  expect(linkRow.dir).toBe(false)
})

test('Files F2.1: folded folders show no children, folders over 500 show first 500 plus more row, treeRows caps at 600 total rows', () => {
  const dirs = new Map<string, FsEntry[]>()
  const bigEntries: FsEntry[] = []
  for (let i = 0; i < 5000; i++) {
    bigEntries.push(makeEntry(`f-${String(i).padStart(4, '0')}.txt`, 'file'))
  }
  dirs.set('/repo', [
    makeEntry('folded', 'dir'),
    makeEntry('huge', 'dir'),
  ])
  dirs.set('/repo/folded', [
    makeEntry('child.txt', 'file'),
  ])
  dirs.set('/repo/huge', bigEntries)

  const walk: Walk = { dirs, capped: false, missing: false }

  // 1. Folded folder shows no children
  const rowsFolded = treeRows(walk, '/repo', new Set<string>(), '', new Map())
  expect(rowsFolded.map(r => r.name)).toEqual(['folded', 'huge'])
  expect(rowsFolded.some(r => r.name === 'child.txt')).toBe(false)

  // 2. Folder with >500 entries shows first 500 and one more row with count = 4500
  const rowsHuge = treeRows(walk, '/repo', new Set(['/repo/huge']), '', new Map())
  // 1 (folded) + 1 (huge) + 500 children + 1 (more) = 503 rows
  expect(rowsHuge.length).toBe(503)
  const moreRow = rowsHuge.find(r => r.key === 'files-more:/repo/huge')!
  expect(moreRow).toBeDefined()
  expect(moreRow.count).toBe(4500)
  expect(moreRow.note).toBe('more')

  // 3. treeRows caps at 600 rows in total, the last one a more row with the count left out
  const manyEntries: FsEntry[] = []
  for (let i = 0; i < 700; i++) {
    manyEntries.push(makeEntry(`file-${String(i).padStart(4, '0')}.txt`, 'file'))
  }
  const dirsCap = new Map<string, FsEntry[]>()
  dirsCap.set('/repo', [
    makeEntry('dir1', 'dir'),
    makeEntry('dir2', 'dir'),
  ])
  dirsCap.set('/repo/dir1', manyEntries.slice(0, 350))
  dirsCap.set('/repo/dir2', manyEntries.slice(350, 700))

  const walkCap: Walk = { dirs: dirsCap, capped: false, missing: false }
  const rowsCapped = treeRows(walkCap, '/repo', new Set(['/repo/dir1', '/repo/dir2']), '', new Map())
  // Total potential rows: 2 folders + 350 + 350 = 702 rows.
  // Capped at 600 rows total: first 599 rows + 1 more row with count = 702 - 599 = 103
  expect(rowsCapped.length).toBe(600)
  const lastRow = rowsCapped[599]!
  expect(lastRow.key).toBe('files-more-cap')
  expect(lastRow.note).toBe('more')
  expect(lastRow.count).toBe(702 - 599)
})

test('Files F2.1: search text keeps matching names and ancestor folders case-insensitive and shows them open, empty search keeps all', () => {
  const dirs = new Map<string, FsEntry[]>()
  dirs.set('/repo', [
    makeEntry('src', 'dir'),
    makeEntry('docs', 'dir'),
    makeEntry('package.json', 'file'),
  ])
  dirs.set('/repo/src', [
    makeEntry('components', 'dir'),
    makeEntry('utils.ts', 'file'),
  ])
  dirs.set('/repo/src/components', [
    makeEntry('Button.tsx', 'file'),
    makeEntry('Header.tsx', 'file'),
  ])
  dirs.set('/repo/docs', [
    makeEntry('readme.md', 'file'),
  ])

  const walk: Walk = { dirs, capped: false, missing: false }

  // Empty search keeps everything according to open set
  const rowsEmpty = treeRows(walk, '/repo', new Set<string>(), '', new Map())
  expect(rowsEmpty.map(r => r.name)).toEqual(['docs', 'src', 'package.json'])

  // Search 'button' keeps Button.tsx plus ancestor folders 'components' and 'src', showing them open
  const rowsSearch = treeRows(walk, '/repo', new Set<string>(), 'button', new Map())
  const names = rowsSearch.map(r => r.name)
  expect(names).toEqual(['src', 'components', 'Button.tsx'])

  const srcRow = rowsSearch.find(r => r.name === 'src')!
  expect(srcRow.open).toBe(true)
  const compRow = rowsSearch.find(r => r.name === 'components')!
  expect(compRow.open).toBe(true)

  // Sibling files not matching are omitted
  expect(names).not.toContain('utils.ts')
  expect(names).not.toContain('Header.tsx')
  expect(names).not.toContain('docs')
  expect(names).not.toContain('package.json')
})

test('Files F2.1: folded folder carries strongest activity inside with write beating read and active file count, open carries none', () => {
  const dirs = new Map<string, FsEntry[]>()
  dirs.set('/repo', [
    makeEntry('src', 'dir'),
    makeEntry('test', 'dir'),
  ])
  dirs.set('/repo/src', [
    makeEntry('a.ts', 'file'),
    makeEntry('b.ts', 'file'),
  ])
  dirs.set('/repo/test', [
    makeEntry('t.ts', 'file'),
  ])

  const walk: Walk = { dirs, capped: false, missing: false }

  const act = new Map<string, 'read' | 'write'>([
    ['/repo/src/a.ts', 'read'],
    ['/repo/src/b.ts', 'write'],
    ['/repo/test/t.ts', 'read'],
  ])

  // With both folders folded:
  // /repo/src has 1 read, 1 write -> write beats read, icon = write, count = 2
  // /repo/test has 1 read -> icon = read, count = 1
  const rowsFolded = treeRows(walk, '/repo', new Set<string>(), '', act)
  const srcFolded = rowsFolded.find(r => r.path === '/repo/src')!
  expect(srcFolded.icon).toBe('write')
  expect(srcFolded.count).toBe(2)

  const testFolded = rowsFolded.find(r => r.path === '/repo/test')!
  expect(testFolded.icon).toBe('read')
  expect(testFolded.count).toBe(1)

  // With /repo/src open:
  // an open folder carries none (icon: null, count: 0)
  const rowsOpen = treeRows(walk, '/repo', new Set(['/repo/src']), '', act)
  const srcOpen = rowsOpen.find(r => r.path === '/repo/src')!
  expect(srcOpen.icon).toBe(null)
  expect(srcOpen.count).toBe(0)

  // File rows carry their direct activity
  const fileA = rowsOpen.find(r => r.path === '/repo/src/a.ts')!
  expect(fileA.icon).toBe('read')
  const fileB = rowsOpen.find(r => r.path === '/repo/src/b.ts')!
  expect(fileB.icon).toBe('write')
})

test('Files F2.1: diffWalk lists new files or files with grown mtimeMs, lists nothing with no prev, ignores .matrixkoder- names', () => {
  const prevDirs = new Map<string, FsEntry[]>()
  prevDirs.set('/repo', [
    makeEntry('unchanged.txt', 'file', 10, 1000),
    makeEntry('modified.txt', 'file', 20, 1000),
    makeEntry('.matrixkoder-status.json', 'file', 30, 1000),
  ])
  const prev: Walk = { dirs: prevDirs, capped: false, missing: false }

  const nextDirs = new Map<string, FsEntry[]>()
  nextDirs.set('/repo', [
    makeEntry('unchanged.txt', 'file', 10, 1000),
    makeEntry('modified.txt', 'file', 25, 2000), // mtimeMs grew
    makeEntry('created.txt', 'file', 50, 2000), // new file
    makeEntry('.matrixkoder-status.json', 'file', 35, 2000), // .matrixkoder- prefix
    makeEntry('.matrixkoder-new.json', 'file', 10, 2000), // .matrixkoder- prefix new
  ])
  const next: Walk = { dirs: nextDirs, capped: false, missing: false }

  // With no prev: lists nothing
  expect(diffWalk(undefined, next)).toEqual([])

  // With prev: lists created.txt and modified.txt; ignores unchanged and .matrixkoder- prefix
  const diff = diffWalk(prev, next)
  expect(diff.sort()).toEqual(['/repo/created.txt', '/repo/modified.txt'])
})

test('Files F2.1: diffWalk counts files only when parent folder was listed in both walks', () => {
  const prevDirs = new Map<string, FsEntry[]>()
  prevDirs.set('/repo', [
    makeEntry('root.txt', 'file', 10, 1000),
    makeEntry('sub', 'dir', 0, 0),
  ])
  // /repo/sub was NOT in prev.dirs (e.g. cut by cap or threw)

  const nextDirs = new Map<string, FsEntry[]>()
  nextDirs.set('/repo', [
    makeEntry('root.txt', 'file', 10, 1000),
    makeEntry('sub', 'dir', 0, 0),
  ])
  nextDirs.set('/repo/sub', [
    makeEntry('new-sub-file.txt', 'file', 100, 2000),
  ])

  const prev: Walk = { dirs: prevDirs, capped: false, missing: false }
  const next: Walk = { dirs: nextDirs, capped: false, missing: false }

  // Subfolder missing from previous walk adds NO writes
  const diff = diffWalk(prev, next)
  expect(diff).toEqual([])
})

test('Files F2.1: row shape and unique key contract on fixture with empty, unlisted, 5000-entry folder and 600 cap', () => {
  const dirs = new Map<string, FsEntry[]>()
  const entries5000: FsEntry[] = []
  for (let i = 0; i < 5000; i++) {
    entries5000.push(makeEntry(`file-${String(i).padStart(4, '0')}.txt`, 'file'))
  }
  dirs.set('/repo', [
    makeEntry('empty-folder', 'dir'),
    makeEntry('folder-unlisted', 'dir'),
    makeEntry('huge-folder', 'dir'),
    makeEntry('z-extra-folder', 'dir'),
  ])
  dirs.set('/repo/empty-folder', [])
  // /repo/folder-unlisted is ABSENT from dirs (not walked / skipped)
  dirs.set('/repo/huge-folder', entries5000)
  const extraEntries: FsEntry[] = []
  for (let i = 0; i < 200; i++) {
    extraEntries.push(makeEntry(`extra-${i}.txt`, 'file'))
  }
  dirs.set('/repo/z-extra-folder', extraEntries)

  const walk: Walk = { dirs, capped: true, missing: false }
  const openSet = new Set(['/repo/empty-folder', '/repo/folder-unlisted', '/repo/huge-folder', '/repo/z-extra-folder'])

  const rows = treeRows(walk, '/repo', openSet, '', new Map())

  // Must hit the 600 total cap
  expect(rows.length).toBe(600)

  // Verify all required key formats are present
  const keys = rows.map(r => r.key)
  expect(keys).toContain('files-empty:/repo/empty-folder')
  expect(keys).toContain('files-unlisted:/repo/folder-unlisted')
  expect(keys).toContain('files-more:/repo/huge-folder')
  expect(keys).toContain('files-more-cap')
  expect(keys.some(k => k.startsWith('files-row:'))).toBe(true)

  // Verify all keys are strictly unique
  const uniqueKeys = new Set(keys)
  expect(uniqueKeys.size).toBe(rows.length)

  // Never equals files-q, files-s, or starts with dash-tab-
  for (const key of keys) {
    expect(key).not.toBe('files-q')
    expect(key).not.toBe('files-s')
    expect(key.startsWith('dash-tab-')).toBe(false)
  }

  // Check every row shape has all 10 required properties
  for (const row of rows) {
    expect(typeof row.key).toBe('string')
    expect(typeof row.path).toBe('string')
    expect(typeof row.name).toBe('string')
    expect(typeof row.depth).toBe('number')
    expect(row.depth).toBeGreaterThanOrEqual(0)
    expect(typeof row.dir).toBe('boolean')
    expect(typeof row.open).toBe('boolean')
    expect(typeof row.hidden).toBe('boolean')
    expect(typeof row.link).toBe('boolean')
    expect(row.icon === null || row.icon === 'read' || row.icon === 'write').toBe(true)
    expect(typeof row.count).toBe('number')
    if (row.note !== undefined) {
      expect(['more', 'empty', 'unlisted']).toContain(row.note)
    }
  }

  // Check specific note rows
  const emptyRow = rows.find(r => r.key === 'files-empty:/repo/empty-folder')!
  expect(emptyRow.note).toBe('empty')
  expect(emptyRow.depth).toBe(1)

  const unlistedRow = rows.find(r => r.key === 'files-unlisted:/repo/folder-unlisted')!
  expect(unlistedRow.note).toBe('unlisted')
  expect(unlistedRow.depth).toBe(1)

  const moreHuge = rows.find(r => r.key === 'files-more:/repo/huge-folder')!
  expect(moreHuge.note).toBe('more')
  expect(moreHuge.count).toBe(4500)
  expect(moreHuge.depth).toBe(1)

  const capRow = rows[599]!
  expect(capRow.key).toBe('files-more-cap')
  expect(capRow.note).toBe('more')
})

test('Files F2.1: rootsOf lists main checkout first with id main and label repo', () => {
  const roots = rootsOf('/path/to/main', [])
  expect(roots).toEqual([
    { id: 'main', label: 'main', path: '/path/to/main' },
  ])
})

test('Files F2.1: rootsOf lists each slot in slot order with id slot-N and task id label or slot N', () => {
  const slots = [
    { slot: 3, path: '/worktrees/slot-3', task: 'F2.1' },
    { slot: 1, path: '/worktrees/slot-1', task: null },
    { slot: 2, path: '/worktrees/slot-2', task: 'F1.1' },
    { slot: 1, path: '/worktrees/slot-1-dup', task: 'ignored-dup' },
  ]
  const roots = rootsOf('/main', slots)

  expect(roots).toEqual([
    { id: 'main', label: 'main', path: '/main' },
    { id: 'slot-1', label: 'slot 1', path: '/worktrees/slot-1', idle: true },
    { id: 'slot-2', label: 'F1.1', path: '/worktrees/slot-2' },
    { id: 'slot-3', label: 'F2.1', path: '/worktrees/slot-3' },
  ])
})

test('Files F2.1: pickRoot returns chosen root or falls back to main checkout', () => {
  const roots = [
    { id: 'main', label: 'main', path: '/main' },
    { id: 'slot-1', label: 'slot 1', path: '/slot-1' },
    { id: 'slot-2', label: 'F2', path: '/slot-2' },
  ]

  expect(pickRoot(roots, 'slot-2')).toEqual({ id: 'slot-2', label: 'F2', path: '/slot-2' })
  expect(pickRoot(roots, 'slot-999')).toEqual({ id: 'main', label: 'main', path: '/main' })
  expect(pickRoot(roots, undefined)).toEqual({ id: 'main', label: 'main', path: '/main' })
  expect(pickRoot(roots, null)).toEqual({ id: 'main', label: 'main', path: '/main' })
  expect(pickRoot(roots, '')).toEqual({ id: 'main', label: 'main', path: '/main' })
})

test('Files columns: treeRows sorts by size and date, folders stay first, desc flips, size and date ride on the rows', () => {
  const walk: Walk = {
    dirs: new Map([['/r', [
      makeEntry('b.txt', 'file', 50, 3000),
      makeEntry('a.txt', 'file', 900, 1000),
      makeEntry('c.txt', 'file', 5, 2000),
      makeEntry('zdir', 'dir', 0, 500),
    ]]]),
    capped: false,
    missing: false,
  }
  const names = (by: 'name' | 'size' | 'date', desc: boolean): string[] =>
    treeRows(walk, '/r', new Set<string>(), '', new Map(), { by, desc }).map(r => r.name)
  expect(names('name', false)).toEqual(['zdir', 'a.txt', 'b.txt', 'c.txt'])
  expect(names('size', false)).toEqual(['zdir', 'c.txt', 'b.txt', 'a.txt'])
  expect(names('size', true)).toEqual(['zdir', 'a.txt', 'b.txt', 'c.txt'])
  expect(names('date', true)).toEqual(['zdir', 'b.txt', 'c.txt', 'a.txt'])
  const first = treeRows(walk, '/r', new Set<string>(), '', new Map()).find(r => r.name === 'a.txt')!
  expect(first.size).toBe(900)
  expect(first.mtimeMs).toBe(1_000_000) // 1000 is seconds, so it reads as 1e6 ms
})

test('Files git: gitStatusMap reads porcelain lines into one-letter marks, renames to the new path', () => {
  const m = gitStatusMap(' M hooks/a.ts\n?? notes/\nD  gone.txt\nA  new.ts\nR  old.ts -> moved.ts\n')
  expect(m.get('hooks/a.ts')).toBe('M')
  expect(m.get('notes/')).toBe('?')
  expect(m.get('gone.txt')).toBe('D')
  expect(m.get('new.ts')).toBe('A')
  expect(m.get('moved.ts')).toBe('R')
})

test('Files ignored: ignoredDirs reads git-ignored folders, walkRoot leaves them unlisted', async () => {
  const ign = ignoredDirs('out/\nsrc/gen/\nnotes.log\n', '/r')
  expect([...ign].sort()).toEqual(['/r/out', '/r/src/gen'])
  const listed: string[] = []
  const walk = await walkRoot('/r', async dir => {
    listed.push(dir)
    return dir === '/r' ? [makeEntry('out', 'dir'), makeEntry('src', 'dir')] : []
  }, ign)
  expect(listed.sort()).toEqual(['/r', '/r/src'])
  expect(walk.dirs.has('/r/out')).toBe(false)
})
