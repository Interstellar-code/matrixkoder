// MatrixKoder paths — the ONE resolver (C1, L0): the MK home rule, the data-dir
// order, the project store path, the run folders and the bus location, in one
// module for every hook. Mirrors scripts/mkpaths.py (the Python half of C1).
//
// What lives here: the $-bound reads (env, HOME, the walk) and the path rules
// built on the pure primitives in data.ts (join, candidateDirs, globalDirPath,
// projectDirFromStore, projectDataDir, parseGitdir, mainCheckout). Those
// primitives themselves stay in data.ts — they are string math with no rule in
// them; every rule that turns them into a path is declared once, here, and
// dashboard.tsx / tools.ts / wake.ts import it instead of keeping a copy.
//
// The data-dir order is mk-state.py's: $MATRIXKODER_DATA → the root projects
// table (`mkdb.py projects --json`) → <home>/projects.json → <main
// checkout>/.matrixkoder; the walk stops BEFORE home (the global
// ~/.matrixkoder is never project data), and a linked worktree resolves to its
// main checkout. The MK home is $MK_HOME and $MATRIXKODER_HOME read as ONE
// setting (C16), as mkpaths.home() reads them on the Python side.
import { candidateDirs, globalDirPath, join, mainCheckout, normalizePath, parentOf, parseGitdir, parseJson, projectDataDir, projectDirFromStore, type MkProjectRow } from './data'
import type { EngineInterface } from 'claude-code'

/** `mkdb.py projects --json` answer: the root store's projects table. */
type ProjectsReply = { projects?: unknown }

function isProjectsReply(value: unknown): value is ProjectsReply {
  return typeof value === 'object' && value !== null && 'projects' in value
}

/** $MK_HOME and $MATRIXKODER_HOME are one setting (C16): either name sets the MK
 *  home. $MK_HOME wins when both are set, the same precedence `mkpaths.home()`
 *  applies. A disagreement is a config error: the Python side refuses it
 *  (`Fail`, exit 2, one JSON line); here the pane must keep rendering, so the
 *  documented precedence wins instead of throwing. */
export async function mkHomeEnv($: EngineInterface): Promise<string | undefined> {
  try {
    const mkHome = (await $.env.get('MK_HOME')) || undefined
    if (mkHome != null) return mkHome
  } catch {
    // env noun refused — the other name or the default
  }
  try {
    const legacy = (await $.env.get('MATRIXKODER_HOME')) || undefined
    if (legacy != null) return legacy
  } catch {
    // env noun refused — the default global dir
  }
  return undefined
}

/** The global dir ($MK_HOME or $MATRIXKODER_HOME, else ~/.matrixkoder): the root
 *  mk.db, projects.json, usage.json. null only when there is no HOME and no env
 *  name to spell "~" with. */
export async function globalDirOf($: EngineInterface, home: string | null): Promise<string | null> {
  return globalDirPath(await mkHomeEnv($), home)
}

/** $HOME from the engine, else `printenv HOME` (a UI hook must resolve a home
 *  even when the env noun is bare). */
export async function homeDir($: EngineInterface): Promise<string | null> {
  try {
    const viaEnv = await $.env.get('HOME')
    if (viaEnv) return viaEnv
  } catch {
    // env noun refused — fall through to the process
  }
  try {
    const ran = await $.process.run(['printenv', 'HOME'])
    const out = ran.stdout.trim()
    if (ran.exitCode === 0 && out) return out
  } catch {
    // no process either — the caller keeps its own fallback
  }
  return null
}

/** The session's cwd, '.' when the engine refuses to say. */
export async function sessionCwd($: EngineInterface): Promise<string> {
  try {
    const cwd = await $.session.cwd()
    return cwd || '.'
  } catch {
    return '.'
  }
}

/** The root store's projects table: the ONE `mkdb.py projects --json` spawn
 * `listedDataDir` and `projectYamlPath` share (C1 — no second reader of the same
 * table). null when the command fails or answers nothing; a caller then keeps
 * today's pre-migration paths. */
async function storeRows($: EngineInterface, home: string | null): Promise<MkProjectRow[] | null> {
  const gdir = await globalDirOf($, home)
  if (gdir == null) return null
  try {
    const ran = await $.process.run(
      ['python3', join($.plugin.root, 'scripts/mkdb.py'), 'projects', '--json', '--home', gdir],
      { timeoutMs: 5000 },
    )
    if (ran.exitCode !== 0) return null
    const parsed = parseJson(ran.stdout)
    const rows = isProjectsReply(parsed) ? parsed.projects : null
    return Array.isArray(rows) ? rows as MkProjectRow[] : null
  } catch {
    return null // no python, or the command failed
  }
}

/** C3's migrated marker for a data dir: a root row whose `db_path` names THIS
 * folder (dirname of db_path — the C2 project folder, what
 * `projectDirFromStore` hands the dashboard after a switch). Same test
 * `mk-config.project_yaml_path` makes with `project_slug_of` + `db_path_of`:
 * the slug's db_path folder is this path. Pure — the rows are the test's. */
export function isMigratedFolder(rows: readonly MkProjectRow[] | null | undefined, dataDir: string): boolean {
  if (!Array.isArray(rows)) return false
  const wanted = normalizePath(dataDir)
  return rows.some(row => {
    const dbPath = typeof row.db_path === 'string' ? row.db_path.trim() : ''
    return dbPath !== '' && normalizePath(parentOf(normalizePath(dbPath))) === wanted
  })
}

/** The repo settings file for a data dir (mkpaths.project_yaml): the C2
 * `<project>/project.yaml` once the project is MIGRATED, else
 * `<dataDir>/matrixkoder.yaml`. `migrated` is `isMigratedFolder`'s answer —
 * the caller owns the store read, this stays pure string math. */
export async function projectYamlPath(
  $: EngineInterface,
  dataDir: string,
  opts: { home?: string | null; store?: boolean } = {},
): Promise<string> {
  const migrated = opts.store === true && isMigratedFolder(await storeRows($, opts.home ?? await homeDir($)), dataDir)
  return paths.projectYaml(dataDir, migrated)
}
/** Root store first (`mkdb.py projects --json`), then projects.json. null = neither
 *  names this repo. The root-store read is opt-in (`store`): a caller whose tests
 *  count every process.run leaves it off. One place, every hook, no file-local
 *  copy — the engine reads each module's own consts, so the $-bound reads live
 *  here and the hooks import them. */
export async function listedDataDir($: EngineInterface, repoPaths: readonly string[], home: string | null, opts: { store?: boolean } = {}): Promise<string | null> {
  const gdir = await globalDirOf($, home)
  if (gdir == null) return null
  if (opts.store === true) {
    const fromStore = projectDirFromStore(await storeRows($, home), repoPaths, home)
    if (fromStore != null) return fromStore
  }
  try {
    return projectDataDir(parseJson(await $.fs.read(paths.projectsJson(gdir))), repoPaths, home)
  } catch {
    return null
  }
}

/** The data dir of this session (mk-state.py's resolve_data order). null when no
 *  repo above cwd owns one; `requireExisting` additionally demands the directory
 *  be there (the dashboard's first-run answer: no data dir yet). Without it the
 *  answer is the path a first `mk-state run init` would write to, which is what
 *  the tools and wake need. */
export async function resolveDataDir($: EngineInterface, opts: { cwd?: string; home?: string | null; store?: boolean; requireExisting?: boolean } = {}): Promise<string | null> {
  try {
    const envData = (await $.env.get('MATRIXKODER_DATA'))?.trim()
    if (envData) return envData
  } catch {
    // env noun refused — fall through to the walk
  }
  const cwd = opts.cwd ?? await sessionCwd($)
  const home = opts.home ?? null
  for (const dir of candidateDirs(cwd, home ?? undefined)) {
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
    // the root store names this repo's data dir; projects.json is the fallback; else <repo>/.matrixkoder
    const listed = await listedDataDir($, [top], home, { store: opts.store })
    const data = listed ?? join(top, '.matrixkoder')
    if (opts.requireExisting !== true) return data
    try {
      const s = await $.fs.stat(data)
      if (s.kind === 'dir') return data
    } catch {
      // repo without a data dir yet → first-run
    }
    return null
  }
  return null
}

/** Every path a hook builds under the data dir and the global dir (C1). A new file
 *  name is added here once, not joined in three hooks — `runsIndex`,
 *  `runDoc`, `projectDb` and friends are the lockstep spots. */
export const paths = {
  projectsJson: (globalDir: string): string => join(globalDir, 'projects.json'),
  usage: (globalDir: string): string => join(globalDir, 'usage.json'),
  /** The root overview store: <home>/mk.db (mkpaths.root_db). The root probe reads this one. */
  rootDb: (home: string): string => join(home, 'mk.db'),
  /** The global settings file: <home>/mk-settings.yaml (mkpaths.mk_settings). */
  mkSettings: (home: string): string => join(home, 'mk-settings.yaml'),
  /** <home>/inbox, or <home>/inbox/<planId> (mkpaths.inbox_dir); a plan id with a separator or `..` is refused. */
  inbox: (home: string, planId?: string): string => {
    if (planId === undefined) return join(home, 'inbox')
    if (planId === '' || planId === '.' || planId === '..' || /[/\\\0]/.test(planId)) throw new Error(`bad plan id ${JSON.stringify(planId)}`)
    return join(home, 'inbox', planId)
  },
  /** The pre-migration data dir of a repo folder: <repo>/.matrixkoder. */
  legacyDataDir: (repo: string): string => join(repo, '.matrixkoder'),
  /** Where a deleted plan's file moves: <globalDir>/projects/<slug>/plans/<plan>/archive/. `globalDir` stays
   *  the display `~/.matrixkoder` when the caller has no real one. */
  planArchiveDir: (slug: string, planId: string, globalDir = '~/.matrixkoder'): string => `${globalDir}/projects/${slug}/plans/${planId}/archive/`,
  /** The project store: <data>/mk.db. Its presence is the store-mode probe. */
  projectDb: (dataDir: string): string => join(dataDir, 'mk.db'),
  /** The repo settings file: `<dataDir>/matrixkoder.yaml` today; the C2
   *  `<project>/project.yaml` for a migrated project (a root `projects.db_path`
   *  set — `projectYamlPath` supplies the flag, `isMigratedFolder` the rule). */
  projectYaml: (dataDir: string, migrated = false): string => join(dataDir, migrated ? 'project.yaml' : 'matrixkoder.yaml'),
  /** The run registry: <data>/runs/index.json. */
  runsIndex: (dataDir: string): string => join(dataDir, 'runs/index.json'),
  /** One run's document: <data>/runs/<run>/run.json. */
  runDoc: (dataDir: string, runId: string): string => join(dataDir, 'runs', runId, 'run.json'),
  /** One run's event log: <data>/runs/<run>/events.jsonl. */
  eventsDoc: (dataDir: string, runId: string): string => join(dataDir, 'runs', runId, 'events.jsonl'),
  /** The orchestrator's bus watch cursors: <data>/watch-cursors.json. */
  watchCursors: (dataDir: string): string => join(dataDir, 'watch-cursors.json'),
  /** C6 (mkpaths.bus_dir): store mode heartbeats the ONE bus under the MK home; file
   *  mode keeps the per-project bus.json in the data dir. `store` = <data>/mk.db
   *  exists; with no global dir to name, the per-project bus is the only answer. */
  busDirOf: (dataDir: string, store: boolean, globalDir: string | null): string => (store && globalDir != null ? globalDir : dataDir),
  /** <bus dir>/bus.json — pid, port, started, token (0600). */
  busFile: (busDir: string): string => join(busDir, 'bus.json'),
}
