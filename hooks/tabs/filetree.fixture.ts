// Fixture views for Files tab tests (plan filetree-tab-ralplan, phase F3, task F3.1).
// Builds realistic FilesView objects using treeRows and real Row objects from filetree-data.
import type { FsEntry, RootItem, Row, Walk } from './filetree-data'
import { treeRows } from './filetree-data'
import type { FilesView } from './filetree'

function makeEntry(name: string, kind: 'file' | 'dir' | 'other' = 'file', size = 100, mtimeMs = 1000, isLink = kind === 'other'): FsEntry {
  return { name, kind, size, mtimeMs, isLink }
}

export const FIXTURE_ROOT = '/repo'

export const FIXTURE_ROOTS: readonly RootItem[] = [
  { id: 'main', label: 'main', path: '/repo' },
  { id: 'slot-1', label: 'P4', path: '/repo-wt/p4' },
  { id: 'slot-2', label: 'slot 2', path: '/repo-wt/slot-2' },
]

export function buildFixtureWalk(): Walk {
  const dirs = new Map<string, FsEntry[]>()
  dirs.set('/repo', [
    makeEntry('.github', 'dir'),
    makeEntry('.git', 'dir'),
    makeEntry('node_modules', 'dir'),
    makeEntry('.matrixkoder', 'dir'),
    makeEntry('.matrixkoder-status.json', 'file'),
    makeEntry('docs', 'dir'),
    makeEntry('hooks', 'dir'),
    makeEntry('scripts', 'dir'),
    makeEntry('empty-dir', 'dir'),
    makeEntry('link-to-readme', 'other'),
    makeEntry('README.md', 'file'),
    makeEntry('package.json', 'file'),
  ])
  dirs.set('/repo/docs', [
    makeEntry('architecture.md', 'file'),
    makeEntry('guide.md', 'file'),
  ])
  dirs.set('/repo/hooks', [
    makeEntry('dashboard.tsx', 'file'),
    makeEntry('tabs', 'dir'),
  ])
  dirs.set('/repo/hooks/tabs', [
    makeEntry('filetree.tsx', 'file'),
    makeEntry('kanban.tsx', 'file'),
  ])
  dirs.set('/repo/scripts', [
    makeEntry('mkdb.py', 'file'),
    makeEntry('test.sh', 'file'),
  ])
  dirs.set('/repo/empty-dir', [])
  // node_modules and .matrixkoder are skipped (absent from dirs)
  return { dirs, capped: false, missing: false }
}

export function filetreeFixtureView(overrides: Partial<FilesView> = {}): FilesView {
  const walk = buildFixtureWalk()
  const openSet = new Set(['/repo/docs', '/repo/hooks', '/repo/hooks/tabs', '/repo/scripts', '/repo/empty-dir'])
  const act = new Map<string, 'read' | 'write'>([
    ['/repo/scripts/mkdb.py', 'write'],
    ['/repo/hooks/dashboard.tsx', 'write'],
    ['/repo/docs/guide.md', 'read'],
    ['/repo/README.md', 'read'],
  ])
  const rows = treeRows(walk, '/repo', openSet, '', act)
  const dirsList = Array.from(walk.dirs.keys())

  return {
    roots: FIXTURE_ROOTS,
    root: 'main',
    rows,
    dirs: dirsList,
    nWrite: 2,
    nRead: 2,
    nFolders: 5,
    nFiles: 8,
    pulse: true,
    capped: false,
    note: null,
    q: '',
    sel: 'files-row:/repo/scripts/mkdb.py',
    ...overrides,
  }
}

/** Fixture with no dot-git entries in walk or view to verify no git/branch/commit words */
export function noGitFixtureView(): FilesView {
  const dirs = new Map<string, FsEntry[]>()
  dirs.set('/project', [
    makeEntry('src', 'dir'),
    makeEntry('README.md', 'file'),
  ])
  dirs.set('/project/src', [
    makeEntry('app.ts', 'file'),
  ])
  const walk: Walk = { dirs, capped: false, missing: false }
  const rows = treeRows(walk, '/project', new Set(['/project/src']), '', new Map())
  return {
    roots: [{ id: 'main', label: 'main', path: '/project' }],
    root: 'main',
    rows,
    dirs: ['/project', '/project/src'],
    nWrite: 0,
    nRead: 0,
    nFolders: 1,
    nFiles: 2,
    pulse: false,
    capped: false,
    note: null,
    q: '',
    sel: 'files-row:/project/src/app.ts',
  }
}

/** Fixture with long names to test clipping at 46 and 36 columns */
export function longNamesFixtureView(): FilesView {
  const longRoot = '/a/very/long/path/that/exceeds/the/normal/column/width/for/testing/purposes'
  const emptyFolder = `${longRoot}/an-empty-folder-with-a-very-long-name-indeed`
  const dirs = new Map<string, FsEntry[]>()
  dirs.set(longRoot, [
    makeEntry('an-extremely-long-file-name-that-definitely-exceeds-thirty-six-columns-in-length.typescript', 'file'),
    makeEntry('a-long-folder-name-that-is-very-long-indeed', 'dir'),
    makeEntry('an-empty-folder-with-a-very-long-name-indeed', 'dir'),
    makeEntry('node_modules', 'dir'),
  ])
  dirs.set(emptyFolder, [])
  const openSet = new Set([emptyFolder, `${longRoot}/node_modules`])
  const walk: Walk = { dirs, capped: true, missing: false }
  const rows = treeRows(walk, longRoot, openSet, '', new Map())
  return {
    roots: [{ id: 'main', label: 'main', path: longRoot }],
    root: 'main',
    rows,
    dirs: [longRoot, emptyFolder],
    nWrite: 14,
    nRead: 25,
    nFolders: 10,
    nFiles: 45,
    pulse: true,
    capped: true,
    note: 'missing root, fallback to repo',
    q: '',
    sel: `files-row:${longRoot}/an-extremely-long-file-name-that-definitely-exceeds-thirty-six-columns-in-length.typescript`,
  }
}
