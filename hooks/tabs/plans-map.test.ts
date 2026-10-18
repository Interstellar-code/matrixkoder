// Mappers from captured mkdb plans JSON to PlansView / PlanView.
import { test, expect } from 'claude-code/testing'
import list from './fixtures/plans-list'
import detail from './fixtures/plan-detail'
import { laneAgent, naturalCompare, planFromReply, plansErrorLine, plansFromOverview, plansFromReply } from './plans-map'
import liveOverview from './fixtures/overview-live'
import type { PlansView } from './plans'

const detailReply = detail as unknown as { id: string; phases: unknown[] }

test('plansFromReply maps the captured list', () => {
  const mapped = plansFromReply(list)
  expect(mapped.plans[0]?.id).toBe('settings-plans-db')
  expect(mapped.plans[0]?.counts.tasks).toBe(14)
  expect(mapped.plans[0]?.counts.phases).toBe(0)
  expect(mapped.plans[0]?.mergesTo).toBe('main')
  expect(mapped.runs).toHaveLength(1)
  expect(mapped.runs[0]?.id).toBe('settings-plans-db-r1')
  const view: PlansView = { mock: false, runs: mapped.runs, plans: mapped.plans }
  expect(view.mock).toBe(false)
  expect('mock' in view.plans[0]!).toBe(false)
})

test('a live_run becomes one run row and counts sum', () => {
  const mapped = plansFromReply({
    ok: true,
    plans: [{
      id: 'p', title: 'T', status: 'running', created: 'c',
      runs: ['p-r1'], live_run: 'p-r1', counts: { building: 1 },
    }],
  })
  expect(mapped.runs).toHaveLength(1)
  expect(mapped.runs[0]?.id).toBe('p-r1')
  expect(mapped.plans[0]?.counts.tasks).toBe(1)
})

test('planFromReply maps the captured detail', () => {
  const view = planFromReply(detail)
  expect(view.mock).toBe(false)
  expect(view.phases?.length).toBe(detailReply.phases.length)
  expect(view.plan?.id).toBe(detailReply.id)
  expect(view.plan?.counts).toBe('32 tasks · 8 phases')
  expect(view.footer).toEqual(['8 phases · all folded by default', 'source project'])
  expect(view.phases?.[0]?.rows.length).toBe(3)
})

test('planFromReply carries phase-level routing onto the phase (V1.5)', () => {
  const view = planFromReply(detail)
  const d15 = view.phases?.find(p => p.id === 'D1.5')
  expect(d15?.slot).toBe(5)
  expect(d15?.pane).toBe('w1R:p1')
  expect(d15?.branch).toBe('mk/settings-plans-db-r1/d1.5')
  expect(d15?.route).toEqual({ agent: 'opencode', model: 'opencode/space-bunny-free', effort: 'medium' })
  const v14 = view.phases?.find(p => p.id === 'V1.4')
  expect(v14?.route).toEqual({ agent: 'opencode', model: 'zai-coding-plan/glm-5.3' })
})

test('a reply with no id is an empty view', () => {
  expect(planFromReply(null)).toEqual({ mock: false, footer: [] })
  expect(planFromReply({ ok: false })).toEqual({ mock: false, footer: [] })
})

test('plansErrorLine mirrors cardsErrorLine', () => {
  expect(plansErrorLine('timeout')).toBe('plans read timed out')
  expect(plansErrorLine('spawn')).toBe('plans read failed: cannot run mkdb')
  expect(plansErrorLine('json')).toBe('plans read failed: not JSON')
  expect(plansErrorLine('exit')).toBe('plans read failed: mkdb exited')
  expect(plansErrorLine('ok', '')).toBe('plans read failed')
  expect(plansErrorLine('ok', 'no store')).toBe('plans read failed: no store')
})

test('integration as a branch name maps to mergesTo', () => {
  const mapped = plansFromReply({ ok: true, plans: [{ id: 'p', title: 'T', status: 'draft', created: '', runs: [], live_run: null, counts: {}, integration: 'main' }] })
  expect(mapped.plans[0]?.mergesTo).toBe('main')
  const view = planFromReply({ ok: true, id: 'p', title: 'T', status: 'draft', integration: 'main', phases: [] })
  expect(view.plan?.mergesTo).toBe('main')
})

/** ── V1.5: the lane·agent tag ─────────────────────────────────────────── */

test('laneAgent: full, lane-only, agent-only, none', () => {
  expect(laneAgent({ slot: 3, route: { agent: 'opencode', model: 'zai-coding-plan/glm-5.3', effort: 'high' } })).toBe('L3 · opencode glm-5.3')
  expect(laneAgent({ slot: 5, route: { agent: 'opencode', model: 'opencode/space-bunny-free', effort: 'medium' } })).toBe('L5 · opencode space-bunny-free')
  expect(laneAgent({ slot: 2 })).toBe('L2')
  expect(laneAgent({ route: { agent: 'opencode', model: 'zai-coding-plan/glm-5.3' } })).toBe('opencode glm-5.3')
  expect(laneAgent({})).toBe('')
  expect(laneAgent({ slot: null, route: null })).toBe('')
  // a slot that is not a number, a route without an agent: parts just omit
  expect(laneAgent({ route: { model: 'provider/only' } })).toBe('only')
})

test('planFromReply carries the V1.5 routing fields onto tasks and subtasks', () => {
  const view = planFromReply({
    ok: true, id: 'p', title: 'T', status: 'running', phases: [{
      id: 'P1', title: 'Phase', status: 'building',
      tasks: [{
        id: 'T1', title: 'Task', status: 'building', type: 'task', round: 2, verdict: 'approve',
        slot: '3', pane: 'mk-3', branch: 'mk/p-r1/t1', updated: '2026-10-14T10:00:00+02:00',
        acceptance: 'the panel shows every field', owned: ['D1.5', 'V1.4'], deps: ['D1'],
        route: { agent: 'opencode', model: 'zai-coding-plan/glm-5.3', effort: 'high' },
        subtasks: [{ id: 'T1a', title: 'Sub', status: 'planned', slot: '4' }],
      }],
    }],
  })
  const row = view.phases?.[0]?.rows[0]
  expect(row?.slot).toBe(3) // the TEXT slot arrives as an int
  expect(row?.pane).toBe('mk-3')
  expect(row?.branch).toBe('mk/p-r1/t1')
  expect(row?.updated).toBe('2026-10-14T10:00:00+02:00')
  expect(row?.acceptance).toBe('the panel shows every field')
  expect(row?.owned).toEqual(['D1.5', 'V1.4'])
  expect(row?.deps).toEqual(['D1'])
  expect(row?.route).toEqual({ agent: 'opencode', model: 'zai-coding-plan/glm-5.3', effort: 'high' })
  expect(row?.type).toBe('task')
  expect(row?.round).toBe(2)
  expect(row?.verdict).toBe('approve')
  expect(row?.subs?.[0]?.slot).toBe(4)
  // a row without the fields keeps them absent (never null text downstream)
  const bare = planFromReply({ ok: true, id: 'p', phases: [{ id: 'P1', tasks: [{ id: 'T2', title: 't', status: 'planned' }] }] })
  const bareRow = bare.phases?.[0]?.rows[0]
  expect(bareRow?.slot).toBeUndefined()
  expect(bareRow?.route).toBeUndefined()
  expect(bareRow?.owned).toBeUndefined()
})

/** ── L7.2: the List shows only the ACTIVE project's plans ─────────────── */

// Two root `plan_summary` rows for two projects, each with its own store.
const TWO_PROJECTS = {
  ok: true,
  projects: [
    { slug: 'alpha', repo: '/srv/alpha', db_path: '/home/mk/projects/alpha/mk.db' },
    { slug: 'beta', repo: '/srv/beta', db_path: '/home/mk/projects/beta/mk.db' },
  ],
  plans: [
    { project: 'alpha', plan: 'alpha-plan', title: 'Alpha plan', status: 'running', counts: { building: 1 } },
    { project: 'beta', plan: 'beta-plan', title: 'Beta plan', status: 'draft', counts: {} },
  ],
}

test('plansFromOverview keeps only the active project\'s plans', () => {
  const all = plansFromOverview(TWO_PROJECTS)
  expect(all.plans.map(p => p.id)).toEqual(['alpha-plan', 'beta-plan']) // no activeDir: as before

  const active = plansFromOverview(TWO_PROJECTS, '/home/mk/projects/alpha')
  expect(active.plans.map(p => p.id)).toEqual(['alpha-plan'])
  expect(active.plans[0]?.title).toBe('Alpha plan')

  const other = plansFromOverview(TWO_PROJECTS, '/home/mk/projects/beta')
  expect(other.plans.map(p => p.id)).toEqual(['beta-plan'])
})

test('Z20.4a: an empty or missing activeDir keeps every plan (null, not an empty filter)', () => {
  expect(plansFromOverview(TWO_PROJECTS, null).plans.map(p => p.id)).toEqual(['alpha-plan', 'beta-plan'])
  // a reply whose projects lists nothing is a pre-projects root: no filter
  const noProjects = { ok: true, projects: [], plans: [{ project: 'alpha', plan: 'alpha-plan', title: 'A', status: 'running', counts: {} }] }
  expect(plansFromOverview(noProjects, '/home/mk/projects/alpha').plans.map(p => p.id)).toEqual(['alpha-plan'])
})

test('Z20.4b: a non-empty dir matching no project row empties the list, never keeps all', () => {
  // The old activeSlugs returned null here (0 matching rows), so the caller
  // kept FOREIGN plans in this project's List. The empty set stays a set and
  // the caller's `!wanted.has(project)` drops every row.
  const mismatched = plansFromOverview(TWO_PROJECTS, '/home/mk/projects/gamma')
  expect(mismatched.plans).toEqual([])
  expect(mismatched.runs).toEqual([])
  // the pre-migration data_dir shape mismatches the same way
  const legacy = {
    ok: true,
    projects: [
      { slug: 'alpha', repo: '/srv/alpha', data_dir: '/data/alpha' },
      { slug: 'beta', repo: '/srv/beta', data_dir: '/data/beta' },
    ],
    plans: [
      { project: 'alpha', plan: 'alpha-plan', title: 'Alpha plan', status: 'running', counts: {} },
      { project: 'beta', plan: 'beta-plan', title: 'Beta plan', status: 'draft', counts: {} },
    ],
  }
  expect(plansFromOverview(legacy, '/data/gamma').plans).toEqual([])
  // a whitespace-only dir trims to '' and is the null case, not an empty filter
  expect(plansFromOverview(TWO_PROJECTS, '   ').plans.map(p => p.id)).toEqual(['alpha-plan', 'beta-plan'])
})

test('plansFromOverview accepts an alias row sharing the active store', () => {
  // The live root carries the project name AND a legacy alias for ONE store.
  const aliased = {
    ok: true,
    projects: [
      { slug: 'alpha', repo: '/srv/alpha', db_path: '/home/mk/projects/alpha/mk.db' },
      { slug: 'a1b2c3', repo: '', db_path: '/home/mk/projects/alpha/mk.db' },
      { slug: 'beta', repo: '/srv/beta', db_path: '/home/mk/projects/beta/mk.db' },
    ],
    plans: [
      { project: 'alpha', plan: 'alpha-plan', title: 'Alpha plan', status: 'running', counts: { building: 1 } },
      { project: 'a1b2c3', plan: 'alias-plan', title: 'Alias plan', status: 'draft', counts: {} },
      { project: 'beta', plan: 'beta-plan', title: 'Beta plan', status: 'draft', counts: {} },
    ],
  }
  const active = plansFromOverview(aliased, '/home/mk/projects/alpha')
  expect(active.plans.map(p => p.id)).toEqual(['alpha-plan', 'alias-plan'])
  // a pre-migration row is read at its data_dir, so that dir matches too
  const legacy = {
    ok: true,
    projects: [
      { slug: 'alpha', repo: '/srv/alpha', data_dir: '/data/alpha' },
      { slug: 'beta', repo: '/srv/beta', data_dir: '/data/beta' },
    ],
    plans: [
      { project: 'alpha', plan: 'alpha-plan', title: 'Alpha plan', status: 'running', counts: {} },
      { project: 'beta', plan: 'beta-plan', title: 'Beta plan', status: 'draft', counts: {} },
    ],
  }
  expect(plansFromOverview(legacy, '/data/alpha').plans.map(p => p.id)).toEqual(['alpha-plan'])
})

// ── Z18: live overview fixture tests ─────────────────────────────────────
// Captured: python3 scripts/mkdb.py overview --json (2026-10-15)
const LIVE = liveOverview as unknown as { ok: boolean; projects: Record<string, unknown>[]; plans: Record<string, unknown>[] }

// The real matrixkoder dataDir from the live overview's matrixkoder row.
const MATRIXKODER_DIR = '/Users/rohits/Development/matrixkoder/.matrixkoder'

test('Z18.1: storage-layout-r1 run card has non-zero counts and countsLine', () => {
  const { runs } = plansFromOverview(LIVE, MATRIXKODER_DIR)
  const run = runs.find(r => r.id === 'storage-layout-r1')
  expect(run).toBeDefined()
  expect(run!.counts.done).toBeGreaterThan(0)
  expect(run!.counts.live).toBeGreaterThan(0)
  expect(run!.countsLine).not.toBe('')
})

test('Z18.2: plan rows carry per-status counts', () => {
  const { plans } = plansFromOverview(LIVE, MATRIXKODER_DIR)
  const storage = plans.find(p => p.id === 'storage-layout')
  expect(storage).toBeDefined()
  expect(storage?.counts.byStatus?.merged).toBe(53)
  expect(storage?.counts.byStatus?.building).toBe(2)
  expect(storage?.counts.byStatus?.planned).toBe(30)
})

test('Z18.3: branch-selection absent from matrixkoder list, matrixkoder plans present', () => {
  const { plans } = plansFromOverview(LIVE, MATRIXKODER_DIR)
  const ids = plans.map(p => p.id)
  expect(ids).not.toContain('branch-selection')
  expect(ids).toContain('storage-layout')
  expect(ids).toContain('settings-plans-db')
  expect(ids).toContain('omp-lane-agent')
})
/** ── F.8: the raw task row's risk / critical / open_reasons pass through ──
 *  F.1 contract fixture: `planFromReply` → `taskOf` copies them onto the
 *  mapped PlanTask (found via the view's phases) when they are present, and
 *  leaves them ABSENT (undefined, not null / 0 / false) when the raw row
 *  carries none. */

test('F.8: planFromReply maps a task row\'s risk, critical and open_reasons onto the PlanTask', () => {
  const RAW_REASON = { kind: 'review', points: 3, source: 'rv', opened: '2026-10-15T10:00:00Z' }
  const view = planFromReply({
    ok: true,
    id: 'riskplan',
    title: 'T',
    status: 'running',
    phases: [{
      id: 'P1',
      title: '',
      status: 'building',
      tasks: [{
        id: 'T9', title: 'risky task', status: 'building', type: 'task', round: 0, verdict: null,
        risk: 4,
        critical: true,
        open_reasons: [RAW_REASON],
      }],
    }],
  })
  const row = view.phases?.find(p => p.id === 'P1')?.rows.find(t => t.id === 'T9')
  expect(row, 'the risky task is mapped').toBeDefined()
  expect(row?.risk, 'the raw `risk: 4` passes through').toBe(4)
  expect(row?.critical, 'the raw `critical: true` passes through').toBe(true)
  expect(row?.openReasons, 'the open_reasons list is landed as the same objects').toEqual([RAW_REASON])
  expect(row?.openReasons?.[0]?.kind).toBe('review')
  expect(row?.openReasons?.[0]?.points).toBe(3)
  expect(row?.openReasons?.[0]?.source).toBe('rv')
  expect(row?.openReasons?.[0]?.opened).toBe('2026-10-15T10:00:00Z')
})

test('F.8: a task row with none of the three keys maps WITHOUT them — undefined, not null/0/false', () => {
  const view = planFromReply({
    ok: true,
    id: 'calmplan',
    title: 'T',
    status: 'draft',
    phases: [{ id: 'P1', title: '', status: 'planned', tasks: [{ id: 'T1', title: 'calm', status: 'planned' }] }],
  })
  const calm = view.phases?.[0]?.rows[0]
  expect(calm, 'the bare task is mapped').toBeDefined()
  expect(calm?.risk, 'risk stays absent (undefined, not 0)').toBeUndefined()
  expect(calm?.critical, 'critical stays absent (undefined, not false)').toBeUndefined()
  expect(calm?.openReasons, 'openReasons stays absent (undefined, not [])').toBeUndefined()
  // the keys are absent, never a made-up value
  expect('risk' in (calm ?? {})).toBe(false)
  expect('critical' in (calm ?? {})).toBe(false)
  expect('openReasons' in (calm ?? {})).toBe(false)
})

/** ── Z20: the run-card buckets lose no task ─────────────────────────────
 *  Z18's bucket table dropped every status it did not list (a counts map
 *  with ready/quota/etc. under-counted the run cards). ready and parked are
 *  planned, the needs-you statuses are live, and any status not in the
 *  table lands in planned — done+live+planned then equals the task total.
 */

const Z20_COUNTS = { merged: 3, ready: 4, blocked: 2, rework: 1, finishing: 2 } // 12 tasks

test('Z20.5: the run-card buckets keep every task — ready/helpless/unknown all count', () => {
  const { runs } = plansFromReply({
    ok: true,
    plans: [{ id: 'p', title: 'T', status: 'running', created: '', runs: ['p-r1'], live_run: 'p-r1', counts: Z20_COUNTS }],
  })
  expect(runs).toHaveLength(1)
  expect(runs[0]?.counts).toEqual({ done: 3, live: 3, planned: 6 })
  // nothing dropped: the buckets sum to the task total
  const c = runs[0]!.counts
  expect(c.done + c.live + c.planned).toBe(12)
})

test('Z20.5: the same totals come through the overview mapping', () => {
  const { runs } = plansFromOverview({
    ok: true,
    projects: [{ slug: 'alpha', repo: '/srv/alpha', db_path: '/home/mk/projects/alpha/mk.db' }],
    plans: [{ project: 'alpha', plan: 'p', title: 'T', status: 'running', counts: Z20_COUNTS, live_run: 'p-r1', stage: 's' }],
  })
  expect(runs).toHaveLength(1)
  const c = runs[0]!.counts
  expect(c.done).toBe(3)
  expect(c.live).toBe(3)
  expect(c.planned).toBe(6)
  expect(c.done + c.live + c.planned).toBe(12)
})

test('Z20.5: the plan row tally still shows the raw statuses', () => {
  const { plans } = plansFromReply({ ok: true, plans: [{ id: 'p', title: 'T', status: 'running', created: '', runs: [], live_run: null, counts: Z20_COUNTS }] })
  expect(plans[0]?.counts.byStatus).toEqual(Z20_COUNTS)
  expect(plans[0]?.counts.tasks).toBe(12)
})

test('planFromReply orders a phase\'s tasks and a task\'s subtasks by natural task id (Z26)', () => {
  const t = (id: string, subtasks: { id: string }[] = []) => ({ id, title: id, status: 'planned', subtasks })
  const view = planFromReply({
    id: 'p', phases: [{ id: 'F', tasks: [t('F.10'), t('F.2'), t('F.S'), t('F.4'),
      t('L10', [{ id: 'L10.12b' }, { id: 'L10.12a' }, { id: 'L10.13' }])] }],
  })
  const rows = view.phases?.[0]?.rows ?? []
  expect(rows.map(r => r.id)).toEqual(['F.2', 'F.4', 'F.10', 'F.S', 'L10'])
  expect(rows[4]?.subs?.map(s => s.id)).toEqual(['L10.12a', 'L10.12b', 'L10.13'])
})

test('natural task id order without ICU: Z9 < Z10 < Z20, digits before text (Z27)', () => {
  const t = (id: string) => ({ id, title: id, status: 'planned', subtasks: [] })
  const view = planFromReply({ id: 'p', phases: [{ id: 'Z', tasks: [t('Z20'), t('Z9'), t('Z10'), t('Z2a'), t('Z')] }] })
  expect((view.phases?.[0]?.rows ?? []).map(r => r.id)).toEqual(['Z', 'Z2a', 'Z9', 'Z10', 'Z20'])
  expect(naturalCompare('F.10', 'F.S')).toBeLessThan(0)
})

test('description maps at plan, phase, task and subtask level; blank and non-string are absent', () => {
  const view = planFromReply({
    ok: true, id: 'p', title: 'T', status: 'running', description: '  Why this plan exists.  ',
    phases: [
      { id: 'P1', title: 'Phase', status: 'building', description: ' A phase.\n',
        tasks: [{ id: 'P1.1', title: 'Task', status: 'building', description: 'A task.',
          subtasks: [{ id: 'P1.1a', title: 'Sub', status: 'building', description: 'A subtask.' }] }] },
      { id: 'P2', title: 'Blank', status: 'planned', description: '   ',
        tasks: [{ id: 'P2.1', title: 'Empty', status: 'planned', description: '',
          subtasks: [{ id: 'P2.1a', title: 'Sub', status: 'planned', description: '\t' }] }] },
      { id: 'P3', title: 'Typed', status: 'planned', description: 7,
        tasks: [{ id: 'P3.1', title: 'Typed', status: 'planned', description: ['x'],
          subtasks: [{ id: 'P3.1a', title: 'Sub', status: 'planned', description: null }] }] },
    ],
  })
  expect(view.plan?.description).toBe('Why this plan exists.')
  expect(view.phases?.[0]?.description).toBe('A phase.')
  expect(view.phases?.[0]?.rows[0]?.description).toBe('A task.')
  expect(view.phases?.[0]?.rows[0]?.subs?.[0]?.description).toBe('A subtask.')
  for (const p of [view.phases?.[1], view.phases?.[2]]) {
    expect(p).not.toHaveProperty('description')
    expect(p?.rows[0]).not.toHaveProperty('description')
    expect(p?.rows[0]?.subs?.[0]).not.toHaveProperty('description')
  }
  for (const bad of ['', '  ', 5, null, {}]) {
    expect(planFromReply({ ok: true, id: 'p', description: bad }).plan).not.toHaveProperty('description')
  }
})

test('planned_route and reviewer map onto phase and task; route stays unset', () => {
  const view = planFromReply({
    ok: true, id: 'p', title: 'T', status: 'draft',
    phases: [{ id: 'P1', title: 'Phase', status: 'planned', planned_route: { agent: 'agy', model: 'gemini-3.8-flash-high' }, reviewer: 'claude sonnet',
      route: null, tasks: [{ id: 'P1.1', title: 'Task', status: 'planned', planned_route: { agent: 'agy', model: 'gemini-3.8-flash-high' }, reviewer: 'claude sonnet', route: null, subtasks: [] }] },
    { id: 'P2', title: 'Bare', status: 'planned', planned_route: {}, reviewer: '  ', tasks: [] }],
  })
  expect(view.phases?.[0]?.plannedRoute).toEqual({ agent: 'agy', model: 'gemini-3.8-flash-high' })
  expect(view.phases?.[0]?.reviewer).toBe('claude sonnet')
  expect(view.phases?.[0]?.rows[0]?.plannedRoute?.agent).toBe('agy')
  expect(view.phases?.[0]?.rows[0]?.reviewer).toBe('claude sonnet')
  expect(view.phases?.[0]?.rows[0]).not.toHaveProperty('route')
  expect(view.phases?.[1]).not.toHaveProperty('plannedRoute')
  expect(view.phases?.[1]).not.toHaveProperty('reviewer')
})

test('the captured detail carries the fixture descriptions', () => {
  const view = planFromReply(detail)
  expect(view.phases?.[0]?.description).toContain('throwaway home folder')
  expect(view.phases?.[0]?.rows[0]?.description).toContain('same folder')
  expect(view.phases?.[1]).not.toHaveProperty('description')
})
