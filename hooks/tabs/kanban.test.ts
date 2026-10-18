// Kanban tab tests (plan mk-store, D8A, contract C4/C1): the render called
// directly with a stub element table so the assertions read the elements
// KanbanTab actually built — seven columns in fixture order, the red attention
// badge kept off the card line, needs-you and project filters, no overflow at
// 46 and 36, the empty store line, and readCards skipping when there is no
// store or the last read was slow on an odd tick.
import { test, expect } from 'claude-code/testing'
import type { Elements } from 'claude-code'
import {
  KanbanTab,
  KANBAN_NOW,
  applyFilters,
  cardLine,
  cardOfTask,
  columnOf,
  emptyKanbanView,
  isAttention,
  kanbanFixtureView,
  kanbanView,
  readCards,
  timeInStatus,
} from './kanban'
import type { RunFn } from './kanban'
import { KANBAN_AFTER, KANBAN_COLUMNS, KANBAN_FIXTURE } from './kanban.fixture'
import { ROLE } from './palette'

const ATTENTION_STATUSES = ['blocked', 'stalled', 'quota', 'provider'] as const
const ATTENTION_TASKS = ['P4', 'D5A', 'P6', 'F2'] as const

type N = { type: string; props: Record<string, unknown>; children: unknown[] }

const node = (v: unknown): N => v as N
const kids = (v: unknown): N[] => {
  const arr = (v as { children?: unknown[] }).children
  return Array.isArray(arr) ? (arr as unknown[]).map(node) : []
}
const text = (v: unknown): string => kids(v).map(c => (typeof c === 'string' ? c : text(c))).join('')

function allOf(root: unknown, type: string): N[] {
  const out: N[] = []
  const walk = (v: unknown): void => {
    if (typeof v !== 'object' || v === null) return
    const cur = node(v)
    if (cur.type === type) out.push(cur)
    kids(cur).forEach(walk)
  }
  walk(root)
  return out
}

const stubEls = (): Elements[keyof Elements] => {
  const make = (type: string) => (props: Record<string, unknown>, ...children: unknown[]) =>
    ({ type, props, children: children.length > 0 ? children : ((props.children as unknown[] | undefined) ?? []) }) as unknown
  return {
    Box: make('Box'),
    Text: make('Text'),
  } as unknown as Elements[keyof Elements]
}

type Drawn = { type?: string; props?: { flexDirection?: string; gap?: number }; children?: unknown[] }

function rowWidth(v: unknown): number {
  if (typeof v !== 'object' || v === null) return 0
  const cur = v as Drawn
  const cs = cur.children ?? []
  if (cur.type === 'Text') return text(cur).length
  if (cur.type === 'Box' && cur.props?.flexDirection === 'row') {
    const gap = typeof cur.props?.gap === 'number' ? cur.props.gap : 0
    return cs.reduce((sum: number, c) => sum + rowWidth(c), 0) + gap * Math.max(0, cs.length - 1)
  }
  return cs.reduce((max: number, c) => Math.max(max, rowWidth(c)), 0)
}

function rowWidths(root: unknown): number[] {
  const out: number[] = []
  const walk = (v: unknown): void => {
    if (typeof v !== 'object' || v === null) return
    const cur = v as Drawn
    if (cur.type === 'Text' || (cur.type === 'Box' && cur.props?.flexDirection === 'row')) out.push(rowWidth(cur))
    for (const c of cur.children ?? []) walk(c)
  }
  walk(root)
  return out
}

const headers = (root: unknown): string[] =>
  allOf(root, 'Text').filter(t => t.props.bold === true).map(t => text(t))

/** column section whose header starts with `name`, plus the card rows under it. */
function columnSection(root: unknown, name: string): N | undefined {
  return allOf(root, 'Box').find(box => {
    const head = kids(box).find(k => k.type === 'Text' && k.props.bold === true)
    return head != null && text(head).startsWith(`${name} `)
  })
}

const redBadges = (root: unknown): N[] =>
  allOf(root, 'Text').filter(t => t.props.color === ROLE.err.fg && text(t) === '! ')

test('every C4 column is drawn from the fixture in order and columnOf is the col field', () => {
  const view = kanbanFixtureView()
  const tree = KanbanTab(stubEls(), view, 46)
  const heads = headers(tree)
  for (const col of KANBAN_COLUMNS) {
    expect(heads.some(h => h.startsWith(`${col} `)), `header ${col}`).toBe(true)
  }
  const order = KANBAN_COLUMNS.map(col => heads.findIndex(h => h.startsWith(`${col} `)))
  expect(order.every((i, n) => i >= 0 && (n === 0 || i > order[n - 1]!)), 'column order').toBe(true)
  for (const card of KANBAN_FIXTURE) {
    expect(columnOf(card), card.task).toBe(card.col)
  }
  const d3a = KANBAN_FIXTURE.find(c => c.task === 'D3A')
  const p4 = KANBAN_FIXTURE.find(c => c.task === 'P4')
  expect(d3a).toBeTruthy()
  expect(p4).toBeTruthy()
  expect(columnOf(d3a!)).toBe('Building')
  expect(columnOf(p4!)).toBe('Building')
  expect(p4!.status).toBe('blocked')
  const building = columnSection(tree, 'Building')
  expect(building, 'Building section').toBeTruthy()
  expect(text(building!)).toContain('D3A')
  expect(text(building!)).toContain('P4')
})

test('attention cards carry a separate red "! " badge and isAttention is only those four', () => {
  const tree = KanbanTab(stubEls(), kanbanFixtureView(), 46)
  const rows = allOf(tree, 'Box').filter(b => b.props.flexDirection === 'row' && b.props.key != null)
  expect(rows.length).toBe(KANBAN_FIXTURE.length)
  for (const row of rows) {
    const key = String(row.props.key)
    const task = key.split('/')[2] ?? ''
    const card = KANBAN_FIXTURE.find(c => c.task === task)
    expect(card, key).toBeTruthy()
    const badges = kids(row).filter(k => k.type === 'Text' && k.props.color === ROLE.err.fg)
    const attention = ATTENTION_TASKS.includes(task as (typeof ATTENTION_TASKS)[number])
    if (attention) {
      expect(badges.length, task).toBe(1)
      expect(text(badges[0]!)).toBe('! ')
      expect(text(badges[0]!)).not.toContain('\n')
      const line = kids(row).filter(k => k.type === 'Text' && k.props.color !== ROLE.err.fg).map(text).join('')
      expect(line.startsWith('! '), `${task} badge folded into line`).toBe(false)
    } else {
      expect(badges.length, task).toBe(0)
    }
  }
  for (const card of KANBAN_FIXTURE) {
    const want = (ATTENTION_STATUSES as readonly string[]).includes(card.status)
    expect(isAttention(card.status), card.status).toBe(want)
  }
  expect(isAttention('building')).toBe(false)
  expect(isAttention('verifying')).toBe(false)
  expect(isAttention('done')).toBe(false)
  expect(redBadges(tree).length).toBe(4)
})

test('needs-you keeps only attention cards, project beta keeps beta, and both filters compose', () => {
  const needs = kanbanFixtureView({ needsYou: true, project: null })
  const attention = KANBAN_FIXTURE.filter(c => c.attention === 1 || isAttention(c.status))
  expect(needs.cards.map(c => c.task).sort()).toEqual(attention.map(c => c.task).sort())
  expect(needs.cards.every(c => ATTENTION_TASKS.includes(c.task as (typeof ATTENTION_TASKS)[number]))).toBe(true)
  expect(applyFilters(KANBAN_FIXTURE, { needsYou: true, project: null }).map(c => c.task).sort())
    .toEqual([...ATTENTION_TASKS].sort())

  const beta = kanbanFixtureView({ needsYou: false, project: 'beta' })
  const betaCards = KANBAN_FIXTURE.filter(c => c.project === 'beta')
  expect(beta.cards.map(c => c.task).sort()).toEqual(betaCards.map(c => c.task).sort())
  expect(beta.cards.every(c => c.project === 'beta')).toBe(true)
  expect(applyFilters(KANBAN_FIXTURE, { needsYou: false, project: 'beta' }).every(c => c.project === 'beta')).toBe(true)

  const both = applyFilters(KANBAN_FIXTURE, { needsYou: true, project: 'beta' })
  expect(both.map(c => c.task).sort()).toEqual(['P4', 'P6'])
  expect(kanbanFixtureView({ needsYou: true, project: 'beta' }).cards.map(c => c.task).sort()).toEqual(['P4', 'P6'])

  const alpha = { ...KANBAN_FIXTURE[0]!, run: 'alpha-plan-r1', task: 'A1' }
  const betaPlan = { ...KANBAN_FIXTURE[1]!, run: 'beta-plan-r1', task: 'B1' }
  const planned = kanbanView([alpha, betaPlan], { needsYou: false, project: null, plan: 'alpha-plan' }, KANBAN_NOW)
  expect(planned.cards.map(c => c.task)).toEqual(['A1'])
  expect(planned.cards.every(c => c.run === 'alpha-plan-r1' || c.run.startsWith('alpha-plan-'))).toBe(true)

  const omitted = kanbanView(KANBAN_FIXTURE, { needsYou: false, project: null }, KANBAN_NOW)
  const planNull = kanbanView(KANBAN_FIXTURE, { needsYou: false, project: null, plan: null }, KANBAN_NOW)
  const planEmpty = kanbanView(KANBAN_FIXTURE, { needsYou: false, project: null, plan: '' }, KANBAN_NOW)
  const ids = (view: { cards: readonly { task: string }[]; projects: readonly string[] }) =>
    ({ tasks: view.cards.map(c => c.task), projects: view.projects })
  expect(ids(planNull)).toEqual(ids(omitted))
  expect(ids(planEmpty)).toEqual(ids(omitted))

  const tree = KanbanTab(stubEls(), needs, 46)
  expect(text(columnSection(tree, 'Building')!)).toContain('P4')
  expect(text(columnSection(tree, 'Building')!)).not.toContain('D3A')
})

test('46 and 36 cols never overflow and the red badge stays exactly "! "', () => {
  for (const cols of [46, 36]) {
    const tree = KanbanTab(stubEls(), kanbanFixtureView(), cols)
    for (const t of allOf(tree, 'Text')) {
      const shown = text(t)
      expect(shown.length, `text longer than ${cols}: ${shown}`).toBeLessThanOrEqual(cols)
    }
    for (const w of rowWidths(tree)) {
      expect(w, `row wider than ${cols}`).toBeLessThanOrEqual(cols)
    }
    const badges = redBadges(tree)
    expect(badges.length, `badges at ${cols}`).toBe(4)
    for (const badge of badges) {
      expect(text(badge)).toBe('! ')
      expect(text(badge).length).toBe(2)
      expect(text(badge)).not.toContain('\n')
    }
  }
  for (const card of KANBAN_FIXTURE) {
    for (const width of [46, 36, 20]) {
      expect(cardLine(card, KANBAN_NOW, width).length).toBeLessThanOrEqual(width)
    }
  }
})

test('emptyKanbanView shows "no store yet" and no column headers', () => {
  const tree = KanbanTab(stubEls(), emptyKanbanView(), 46)
  const texts = allOf(tree, 'Text').map(t => text(t))
  expect(texts).toContain('no store yet')
  expect(texts.filter(t => t === 'no store yet').length).toBe(1)
  for (const col of KANBAN_COLUMNS) {
    expect(texts.some(t => t.startsWith(col)), col).toBe(false)
  }
})

test('readCards skips a missing root store, reads the overview then per-project, and skips a slow odd tick', async () => {
  let calls = 0
  const run: RunFn = async argv => {
    calls += 1
    expect(argv).toContain('--json')
    const homeAt = argv.indexOf('--home')
    if (homeAt >= 0) {
      expect(argv).toContain('overview')
      expect(argv[homeAt + 1]).toBe('/tmp/mk-home')
      return { stdout: JSON.stringify({ ok: true, projects: [{ slug: 'alpha', repo: '/r/alpha', data_dir: '/d/alpha', db_path: null }], plans: [] }), code: 0 }
    }
    expect(argv).toContain('run-snapshot')
    expect(argv).toContain('--data')
    const dataAt = argv.indexOf('--data')
    expect(argv[dataAt + 1]).toBe('/d/alpha')
    return { stdout: JSON.stringify({ ok: true, runs: [], active: 'r', waits: [], run: { id: 'r', plan: null, status: 'active', tasks: [{ id: 'D3A', title: 'Writer', status: 'building', parent: 'D3', updated: '2026-10-12T09:00:00+02:00' }] } }), code: 0 }
  }

  calls = 0
  const missing = await readCards(run, '/plugin', '/tmp/mk-home', false, 0, 0)
  expect(calls).toBe(0)
  expect(missing.empty).toBe(true)
  expect(missing.cards).toEqual([])

  calls = 0
  const fresh = await readCards(run, '/plugin', '/tmp/mk-home', true, 0, 0)
  expect(calls, 'one overview + one per linked project').toBe(2)
  expect(fresh.empty).toBe(false)
  expect(fresh.skipped).toBe(false)
  expect(fresh.cards).toHaveLength(1)
  expect(fresh.cards[0]?.task).toBe('D3A')
  expect(typeof fresh.ms).toBe('number')

  calls = 0
  const skipped = await readCards(run, '/plugin', '/tmp/mk-home', true, 1, 150)
  expect(calls).toBe(0)
  expect(skipped.skipped).toBe(true)
  expect(skipped.empty).toBe(false)

  calls = 0
  const even = await readCards(run, '/plugin', '/tmp/mk-home', true, 0, 150)
  expect(calls).toBe(2)
  expect(even.skipped).toBe(false)
  expect(typeof even.ms).toBe('number')
})

test('D3A is under Building before the fixture transition and under Verify after', () => {
  const before = KanbanTab(stubEls(), kanbanFixtureView({ needsYou: false, project: null }, KANBAN_FIXTURE), 46)
  const after = KanbanTab(stubEls(), kanbanFixtureView({ needsYou: false, project: null }, KANBAN_AFTER), 46)
  expect(text(columnSection(before, 'Building')!)).toContain('D3A')
  expect(text(columnSection(before, 'Verify')!)).not.toContain('D3A')
  expect(text(columnSection(after, 'Verify')!)).toContain('D3A')
  expect(text(columnSection(after, 'Building')!)).not.toContain('D3A')
  const moved = KANBAN_AFTER.find(c => c.task === 'D3A')
  expect(moved).toBeTruthy()
  expect(moved!.status).toBe('verifying')
  expect(moved!.col).toBe('Verify')
  expect(columnOf(moved!)).toBe('Verify')
})

test('timeInStatus of one hour before KANBAN_NOW is 1h', () => {
  expect(timeInStatus('2026-10-12T09:00:00+02:00', KANBAN_NOW)).toBe('1h')
})


/** Captured from a real corrupt mk.db: stdout empty, exit 1, this traceback on stderr. */
const CORRUPT_MKDB_STDERR = '    return _migrate(_connect(os.path.join(home, "mk.db")), version, ((1, ROOT_DDL),) + tuple(migrations))\n                    ~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n  File "/Users/rohits/Development/matrixkoder-mk-5/scripts/mkdb.py", line 144, in _connect\n    con.execute("PRAGMA journal_mode=WAL")\n    ~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^\nsqlite3.DatabaseError: file is not a database'


test('a corrupt root store read (empty stdout, exit 1) is an error, not an empty board', async () => {
  expect(CORRUPT_MKDB_STDERR).toContain('sqlite3.DatabaseError: file is not a database')
  const run: RunFn = async argv => {
    expect(argv).toContain('overview')
    return { stdout: '', code: 1 }
  }
  const got = await readCards(run, '/plugin', '/tmp/mk-home', true, 0, 0)
  expect(got.empty).toBe(false)
  expect(got.error).toBe('cards read failed: mkdb exited')
  expect(got.cards).toEqual([])
  const view = { ...kanbanView(KANBAN_FIXTURE, { needsYou: false, project: null }, KANBAN_NOW), error: got.error, stale: true }
  const tree = KanbanTab(stubEls(), view, 46)
  const texts = allOf(tree, 'Text').map(t => text(t))
  expect(texts.some(t => t.includes('cards read failed: mkdb exited'))).toBe(true)
  expect(texts.some(t => t.includes('stale'))).toBe(true)
  expect(texts).not.toContain('no store yet')
  expect(text(columnSection(tree, 'Building')!)).toContain('D3A')
})

test('non-JSON overview stdout, {ok:false}, a thrown spawn and a timeout are errors', async () => {
  const cases: Array<{ name: string; run: RunFn; error: string }> = [
    {
      name: 'not json',
      run: async () => ({ stdout: 'not-json', code: 0 }),
      error: 'cards read failed: not JSON',
    },
    {
      name: 'ok false',
      run: async () => ({ stdout: JSON.stringify({ ok: false, error: 'locked' }), code: 0 }),
      error: 'cards read failed: locked',
    },
    {
      name: 'spawn throws',
      run: async () => { throw new Error('spawn python3 ENOENT') },
      error: 'cards read failed: cannot run mkdb',
    },
    {
      name: 'timeout',
      run: async () => { throw new Error('timed out after 5000ms') },
      error: 'cards read timed out',
    },
  ]
  for (const c of cases) {
    const got = await readCards(c.run, '/plugin', '/tmp/mk-home', true, 0, 0)
    expect(got.error, c.name).toBe(c.error)
    expect(got.empty, c.name).toBe(false)
    expect(got.cards, c.name).toEqual([])
  }
})

test('a per-project snapshot with 5000 open tasks parses and does not report an error', async () => {
  const tasks = Array.from({ length: 5000 }, (_, i) => ({ id: `T${String(i)}`, title: 'x'.repeat(40), status: 'building', parent: null, updated: '2026-10-12T09:00:00+02:00' }))
  const snap = JSON.stringify({ ok: true, runs: [], active: 'r', waits: [], run: { id: 'r', plan: null, status: 'active', tasks } })
  expect(snap.length).toBeGreaterThan(100_000)
  const run: RunFn = async argv => argv.includes('overview')
    ? { stdout: JSON.stringify({ ok: true, projects: [{ slug: 'alpha', repo: '/r/alpha', data_dir: '/d/alpha', db_path: null }], plans: [] }), code: 0 }
    : { stdout: snap, code: 0 }
  const got = await readCards(run, '/plugin', '/tmp/mk-home', true, 0, 0)
  expect(got.error).toBeNull()
  expect(got.cards).toHaveLength(5000)
  expect(got.empty).toBe(false)
})

test('two root rows sharing one store read it once, each task once, cards under the named slug', async () => {
  // L7.2 R2: the live root carries both the project's own slug and a legacy alias
  // for the SAME store (same db_path, same repo). Reading per root row would spawn
  // run-snapshot twice and push every task twice; the fix groups by store dir.
  const snapCalls: string[] = []
  const run: RunFn = async argv => {
    expect(argv).toContain('--json')
    if (argv.includes('--home')) {
      return {
        stdout: JSON.stringify({
          ok: true,
          projects: [
            { slug: 'alias-legacy', repo: '/r/matrixkoder', data_dir: null, db_path: '/d/matrixkoder/mk.db' },
            { slug: 'matrixkoder', repo: '/r/matrixkoder', data_dir: null, db_path: '/d/matrixkoder/mk.db' },
          ],
          plans: [],
        }),
        code: 0,
      }
    }
    expect(argv).toContain('run-snapshot')
    expect(argv).toContain('--data')
    const dataAt = argv.indexOf('--data')
    snapCalls.push(String(argv[dataAt + 1]))
    return {
      stdout: JSON.stringify({
        ok: true, runs: [], active: 'r', waits: [],
        run: { id: 'matrixkoder-r1', plan: null, status: 'active', tasks: [
          { id: 'D3A', title: 'Writer', status: 'building', parent: 'D3', updated: '2026-10-12T09:00:00+02:00' },
          { id: 'D4A', title: 'Reader', status: 'blocked', parent: 'D4', updated: '2026-10-12T09:00:00+02:00' },
        ] },
      }),
      code: 0,
    }
  }

  const got = await readCards(run, '/plugin', '/tmp/mk-home', true, 0, 0)
  expect(snapCalls, 'one run-snapshot per distinct store').toHaveLength(1)
  expect(snapCalls[0]).toBe('/d/matrixkoder')
  expect(got.error).toBeNull()
  expect(got.empty).toBe(false)
  expect(got.cards, 'each task exactly once').toHaveLength(2)
  expect(got.cards.map(c => c.task).sort()).toEqual(['D3A', 'D4A'])
  // The store's cards carry the NAMED row's slug (slug === basename(repo)), not the alias.
  expect(got.cards.find(c => c.task === 'D3A')?.project).toBe('matrixkoder')
  expect(got.cards.find(c => c.task === 'D4A')?.project).toBe('matrixkoder')
  expect(got.cards.every(c => c.project === 'matrixkoder')).toBe(true)
})

test('two distinct stores are each read once and both are returned under their own slugs', async () => {
  const snapCalls: string[] = []
  const run: RunFn = async argv => {
    if (argv.includes('--home')) {
      return {
        stdout: JSON.stringify({
          ok: true,
          projects: [
            { slug: 'alpha', repo: '/r/alpha', data_dir: null, db_path: '/d/alpha/mk.db' },
            { slug: 'beta', repo: '/r/beta', data_dir: null, db_path: '/d/beta/mk.db' },
          ],
          plans: [],
        }),
        code: 0,
      }
    }
    expect(argv).toContain('run-snapshot')
    const dataAt = argv.indexOf('--data')
    const dir = String(argv[dataAt + 1])
    snapCalls.push(dir)
    const name = dir.split('/').pop() ?? ''
    return {
      stdout: JSON.stringify({
        ok: true, runs: [], active: 'r', waits: [],
        run: { id: `${name}-r1`, plan: null, status: 'active', tasks: [
          { id: `${name.toUpperCase()}1`, title: 'x', status: 'building', parent: null, updated: '2026-10-12T09:00:00+02:00' },
        ] },
      }),
      code: 0,
    }
  }

  const got = await readCards(run, '/plugin', '/tmp/mk-home', true, 0, 0)
  expect(snapCalls.slice().sort(), 'one read per distinct store').toEqual(['/d/alpha', '/d/beta'])
  expect(got.error).toBeNull()
  expect(got.cards.map(c => c.task).sort()).toEqual(['ALPHA1', 'BETA1'])
  expect(got.cards.find(c => c.task === 'ALPHA1')?.project).toBe('alpha')
  expect(got.cards.find(c => c.task === 'BETA1')?.project).toBe('beta')
})
test('F.8: a rework task is a Fix-column card with attention', () => {
  // contract fixture until F.1 merges
  const card = cardOfTask('p', 'r', { id: 'X9', status: 'rework', title: 't' })
  expect(card.col, 'rework opens the Fix column, not Backlog').toBe('Fix')
  expect(card.attention, 'a rework card is an attention card').toBe(1)
})
