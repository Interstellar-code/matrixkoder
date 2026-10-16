// Tests for hooks/paths.ts: the ONE MK path resolver (C1). The plugin test runtime reads
// no files and spawns no processes, and an engine-based probe cannot reach this module
// (the host only follows `$` into a function declared at the top of a hooks module — see
// PluginRegisterUses in the claude-code types). So the resolver is driven through a
// DOUBLE of the few nouns it touches, injected as its `$`: exactly the calls paths.ts
// makes, nothing else. A method it reaches but the double lacks throws a plain TypeError,
// so a drift in the module under test fails loudly instead of passing quietly. The rule
// itself is never mocked — env, fs, process and session answers are the test's own.
import { test, expect } from 'claude-code/testing'
import type { EngineInterface, ProcessRunResult } from 'claude-code'
import { globalDirOf, isMigratedFolder, paths, projectYamlPath, resolveDataDir } from './paths'

/** The calls paths.ts makes on `$`, and no others. */
type PathsDouble = {
  plugin: { name: string; root: string }
  env: { get: (name: string) => Promise<string | undefined> }
  fs: {
    stat: (path: string) => Promise<{ kind: string }>
    read: (path: string) => Promise<string>
  }
  session: { cwd: () => Promise<string> }
  process: { run: (argv: readonly string[]) => Promise<ProcessRunResult> }
}

function world(opts: {
  env?: Readonly<Record<string, string | undefined>>
  dirs?: readonly string[]                       // paths that stat as a dir (.git in a checkout)
  files?: Readonly<Record<string, string>>       // paths that exist as a file → their text
  cwd?: string
  runs?: (argv: readonly string[]) => ProcessRunResult   // per-call process.run answer
} = {}): EngineInterface {
  const dirs = opts.dirs ?? []
  const files = opts.files ?? {}
  const double: PathsDouble = {
    plugin: { name: 'matrixkoder', root: '/plugin' },
    env: { get: async (name) => opts.env?.[name] },
    fs: {
      stat: async (path) => {
        if (dirs.includes(path)) return { kind: 'dir' }
        if (path in files) return { kind: 'file' }
        throw new Error(`ENOENT: ${path}`)
      },
      read: async (path) => {
        if (!(path in files)) throw new Error(`ENOENT: ${path}`)
        return files[path] ?? ''
      },
    },
    session: { cwd: async () => opts.cwd ?? '/repo' },
    process: {
      run: async (argv) => opts.runs?.(argv) ?? {
        exitCode: 1,
        stdout: '',
        stderr: `no answer for ${argv.join(' ')}`,
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    },
  }
  return double as EngineInterface // DI seam: the double covers the whole surface the resolver touches
}

const plainRepo = (files: Readonly<Record<string, string>> = {}) =>
  world({ env: { HOME: '/home/mk' }, dirs: ['/repo/.git'], files, cwd: '/repo' })

test('$MATRIXKODER_DATA wins over the walk, trimmed', async () => {
  const $ = plainRepo()
  const envOverridden = world({ env: { HOME: '/home/mk', MATRIXKODER_DATA: ' /srv/data ' }, dirs: ['/repo/.git'], cwd: '/repo/sub' })
  expect(await resolveDataDir(envOverridden, { cwd: '/repo/sub', home: '/home/mk' })).toBe('/srv/data')
  expect(await resolveDataDir($, { cwd: '/repo/sub', home: '/home/mk' })).toBe('/repo/.matrixkoder')
})

test('the nearest .git above cwd names <repo>/.matrixkoder, and the walk stops before home', async () => {
  const $ = plainRepo()
  expect(await resolveDataDir($, { cwd: '/repo/sub/deep', home: '/home/mk' })).toBe('/repo/.matrixkoder')
  // a cwd under home with no repo above it: no data dir, never <home>/.matrixkoder
  const under = world({ env: { HOME: '/home/mk' }, cwd: '/home/mk/src' })
  expect(await resolveDataDir(under, { cwd: '/home/mk/src', home: '/home/mk' })).toBeNull()
})

test('a linked worktree resolves to the MAIN checkout data dir', async () => {
  const $ = world({
    env: { HOME: '/home/mk' },
    dirs: [],
    files: {
      '/wt-lane/.git': 'gitdir: /repo/.git/worktrees/lane\n',
      '/repo/.git/worktrees/lane/commondir': '../..\n',
    },
    cwd: '/wt-lane',
  })
  expect(await resolveDataDir($, { cwd: '/wt-lane', home: '/home/mk' })).toBe('/repo/.matrixkoder')
})

test('the root projects table names the data dir before projects.json', async () => {
  const storeHome = world({ env: { HOME: '/home/mk', MATRIXKODER_HOME: '/srv/mkhome' } })
  expect(await globalDirOf(storeHome, '/home/mk')).toBe('/srv/mkhome')
  const withRun = world({
    env: { HOME: '/home/mk' },
    dirs: ['/repo/.git'],
    files: { '/srv/mkhome/projects.json': JSON.stringify({ '/repo': { data_dir: 'from-json' } }) },
    cwd: '/repo',
    runs: () => ({
      exitCode: 0,
      stdout: JSON.stringify({ projects: [{ slug: 'matrixkoder', repo: '/repo', data_dir: '/srv/data' }] }),
      stderr: '',
      isStdoutTruncated: false,
      isStderrTruncated: false,
    }),
  })
  expect(await resolveDataDir(withRun, { cwd: '/repo', home: '/home/mk', store: true })).toBe('/srv/data')
})

test('a migrated project resolves to the C2 folder (dirname of the root db_path)', async () => {
  // C1 (L3): mk-migrate --switch sets projects.db_path; the same `mkdb.py projects --json` reply
  // carries it, so the root-store reader follows the switch with no second reader.
  const migrated = world({
    env: { HOME: '/home/mk', MATRIXKODER_HOME: '/srv/mkhome' },
    dirs: ['/repo/.git'],
    files: { '/srv/mkhome/projects.json': JSON.stringify({ '/repo': { data_dir: 'from-json' } }) },
    cwd: '/repo',
    runs: () => ({
      exitCode: 0,
      stdout: JSON.stringify({ projects: [{
        slug: 'matrixkoder', repo: '/repo', data_dir: '/repo/.matrixkoder',
        db_path: '/srv/mkhome/projects/matrixkoder/mk.db',
      }] }),
      stderr: '',
      isStdoutTruncated: false,
      isStderrTruncated: false,
    }),
  })
  expect(await resolveDataDir(migrated, { cwd: '/repo', home: '/home/mk', store: true }))
    .toBe('/srv/mkhome/projects/matrixkoder')
  // an unmigrated row (db_path empty) keeps today's data_dir
  const unmigrated = world({
    env: { HOME: '/home/mk', MATRIXKODER_HOME: '/srv/mkhome' },
    dirs: ['/repo/.git'],
    cwd: '/repo',
    runs: () => ({
      exitCode: 0,
      stdout: JSON.stringify({ projects: [{
        slug: 'matrixkoder', repo: '/repo', data_dir: '/srv/data', db_path: '',
      }] }),
      stderr: '',
      isStdoutTruncated: false,
      isStderrTruncated: false,
    }),
  })
  expect(await resolveDataDir(unmigrated, { cwd: '/repo', home: '/home/mk', store: true })).toBe('/srv/data')
})

test('the repo settings file is project.yaml on a migrated project, matrixkoder.yaml before', async () => {
  // L7.6: the same root-store reply mkpaths.project_yaml reads. A `db_path` set (C3's
  // migrated marker) makes the data dir the C2 project folder, and its yaml is
  // `<project>/project.yaml` — mk-config.project_yaml_path's answer, byte for byte.
  const c2 = '/srv/mkhome/projects/matrixkoder'
  const rows = (dbPath: string) => () => ({
    exitCode: 0,
    stdout: JSON.stringify({ projects: [{ slug: 'matrixkoder', repo: '/repo', data_dir: '/repo/.matrixkoder', db_path: dbPath }] }),
    stderr: '',
    isStdoutTruncated: false,
    isStderrTruncated: false,
  })
  const world1 = (dbPath: string) => world({
    env: { HOME: '/home/mk', MATRIXKODER_HOME: '/srv/mkhome' },
    dirs: ['/repo/.git'],
    cwd: '/repo',
    runs: rows(dbPath),
  })

  // the migrated project: db_path set → the C2 folder, and project.yaml there
  const migrated = world1(`${c2}/mk.db`)
  expect(await resolveDataDir(migrated, { cwd: '/repo', home: '/home/mk', store: true })).toBe(c2)
  expect(await projectYamlPath(migrated, c2, { home: '/home/mk', store: true })).toBe(`${c2}/project.yaml`)

  // pre-migration: db_path empty → today's <data>/matrixkoder.yaml, unchanged
  const legacy = world1('')
  expect(await projectYamlPath(legacy, '/srv/data', { home: '/home/mk', store: true })).toBe('/srv/data/matrixkoder.yaml')

  // a data dir no row names is not migrated (C2's rule, no guessing)
  expect(isMigratedFolder([{ db_path: `${c2}/mk.db` }], '/srv/data')).toBe(false)
  expect(isMigratedFolder([{ db_path: '' }], c2)).toBe(false)
  expect(isMigratedFolder([{ db_path: `${c2}/mk.db` }], `${c2}/`)).toBe(true)
  // store:false never asks the store — the pre-migration name stands
  expect(await projectYamlPath(migrated, c2, { home: '/home/mk' })).toBe(`${c2}/matrixkoder.yaml`)
})

test('projects.json is the fallback when the root-store command fails', async () => {
  const $ = plainRepo({ '/srv/mkhome/projects.json': JSON.stringify({ '/repo': { data_dir: '/srv/json-data' } }) })
  const failing = world({
    env: { HOME: '/home/mk', MATRIXKODER_HOME: '/srv/mkhome' },
    dirs: ['/repo/.git'],
    files: { '/srv/mkhome/projects.json': JSON.stringify({ '/repo': { data_dir: '/srv/json-data' } }) },
    cwd: '/repo',
  })
  expect(await resolveDataDir(failing, { cwd: '/repo', home: '/home/mk', store: true })).toBe('/srv/json-data')
})

test('requireExisting is the first-run answer: null when the data dir is not there', async () => {
  expect(await resolveDataDir(plainRepo(), { cwd: '/repo', home: '/home/mk', requireExisting: true })).toBeNull()
  const there = world({ env: { HOME: '/home/mk' }, dirs: ['/repo/.git', '/repo/.matrixkoder'], cwd: '/repo' })
  expect(await resolveDataDir(there, { cwd: '/repo', home: '/home/mk', requireExisting: true })).toBe('/repo/.matrixkoder')
})

test('$MK_HOME and $MATRIXKODER_HOME are ONE setting, MK_HOME first', async () => {
  const legacy = world({ env: { HOME: '/home/mk', MATRIXKODER_HOME: '/legacy-home' } })
  expect(await globalDirOf(legacy, '/home/mk')).toBe('/legacy-home')
  const both = world({ env: { HOME: '/home/mk', MK_HOME: '/mk-home', MATRIXKODER_HOME: '/legacy-home' } })
  expect(await globalDirOf(both, '/home/mk')).toBe('/mk-home') // the precedence mkpaths.home uses
  // a leading ~ in the env value means HOME, and no env name means <home>/.matrixkoder
  const tilde = world({ env: { HOME: '/home/mk', MATRIXKODER_HOME: '~/mkroot' } })
  expect(await globalDirOf(tilde, '/home/mk')).toBe('/home/mk/mkroot')
  const bare = world({ env: { HOME: '/home/mk' } })
  expect(await globalDirOf(bare, '/home/mk')).toBe('/home/mk/.matrixkoder')
  expect(await globalDirOf(world({ env: {} }), null)).toBeNull() // no HOME, no env name: nothing to spell
})

test('the data-dir and global-dir file names come from one place', async () => {
  expect(paths.runsIndex('/d')).toBe('/d/runs/index.json')
  expect(paths.runDoc('/d', 'r1')).toBe('/d/runs/r1/run.json')
  expect(paths.projectDb('/d')).toBe('/d/mk.db') // the legacy name: pickDb is the resolving form
  expect(paths.legacyDataDir('/r')).toBe('/r/.matrixkoder')
  expect(paths.homeProjectDir('/g', 'p')).toBe('/g/projects/p')
  expect(paths.planArchiveDir('p', 'a1')).toBe('~/.matrixkoder/projects/p/plans/a1/archive/')
  expect(paths.planArchiveDir('p', 'a1', '/g')).toBe('/g/projects/p/plans/a1/archive/')
  expect(paths.projectYaml('/d')).toBe('/d/matrixkoder.yaml')
  expect(paths.watchCursors('/d')).toBe('/d/watch-cursors.json')
  // L10.1: the same strings scripts/mkpaths.py --self-test asserts for the same home (this suite spawns no process)
  expect(paths.rootDb('/g')).toBe('/g/mk.db')
  expect(paths.dbCandidates('/g', 'root')).toEqual(['/g/mk-overview.db', '/g/mk.db'])
  expect(paths.dbCandidates('/d', 'project')).toEqual(['/d/mk-project.db', '/d/mk.db'])
  expect(paths.mkSettings('/g')).toBe('/g/mk-settings.yaml')
  expect(paths.inbox('/g')).toBe('/g/inbox')
  expect(paths.inbox('/g', 'p1')).toBe('/g/inbox/p1')
  for (const bad of ['..', '.', '', 'a/b', '../x', 'a\\b', 'a\0b']) expect(() => paths.inbox('/g', bad)).toThrow()
  expect(paths.projectsJson('/g')).toBe('/g/projects.json')
  expect(paths.usage('/g')).toBe('/g/usage.json')
  expect(paths.busFile(paths.busDirOf('/d', true, '/g'))).toBe('/g/bus.json') // store mode: the one bus per home
  expect(paths.busFile(paths.busDirOf('/d', false, '/g'))).toBe('/d/bus.json') // file mode: per-project
  expect(paths.busDirOf('/d', true, null)).toBe('/d') // no global dir: the per-project bus
})

// L10.13: mkpaths._resolve_db's four states, for both kinds (scripts/mkpaths.py --self-test asserts the same table)
test('pickDb: new name if it exists, else an existing mk.db, else the new name; both present = new wins', async () => {
  const has = (files: readonly string[]) => (p: string): boolean => files.includes(p)
  for (const [kind, fresh] of [['root', 'mk-overview.db'], ['project', 'mk-project.db']] as const) {
    expect(await paths.pickDb('/h', kind, has([])), `${kind} fresh`).toBe(`/h/${fresh}`)
    expect(await paths.pickDb('/h', kind, has(['/h/mk.db'])), `${kind} old only`).toBe('/h/mk.db')
    expect(await paths.pickDb('/h', kind, has([`/h/${fresh}`])), `${kind} new only`).toBe(`/h/${fresh}`)
    expect(await paths.pickDb('/h', kind, has(['/h/mk.db', `/h/${fresh}`])), `${kind} both`).toBe(`/h/${fresh}`)
  }
  // the other kind's new name is not this kind's store
  expect(await paths.pickDb('/h', 'root', has(['/h/mk-project.db']))).toBe('/h/mk-overview.db')
  // L10.X: an EMPTY new-name file is no store: with a size probe it never hides mk.db
  const sized = (sizes: Record<string, number>) => (p: string): number => sizes[p] ?? 0
  for (const [kind, fresh] of [['root', 'mk-overview.db'], ['project', 'mk-project.db']] as const) {
    const files = has(['/h/mk.db', `/h/${fresh}`])
    expect(await paths.pickDb('/h', kind, files, sized({ '/h/mk.db': 44, [`/h/${fresh}`]: 0 })), `${kind} empty new + mk.db`).toBe('/h/mk.db')
    expect(await paths.pickDb('/h', kind, files, sized({ '/h/mk.db': 44, [`/h/${fresh}`]: 4096 })), `${kind} real new + mk.db`).toBe(`/h/${fresh}`)
    expect(await paths.pickDb('/h', kind, has([`/h/${fresh}`]), sized({})), `${kind} empty new alone`).toBe(`/h/${fresh}`)
  }
  // an async probe, as $.fs.exists is
  expect(await paths.pickDb('/h', 'root', async p => p === '/h/mk.db')).toBe('/h/mk.db')
})
