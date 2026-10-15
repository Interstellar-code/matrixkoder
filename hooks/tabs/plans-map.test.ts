// Mappers from captured mkdb plans JSON to PlansView / PlanView.
import { test, expect } from 'claude-code/testing'
import list from './fixtures/plans-list'
import detail from './fixtures/plan-detail'
import { laneAgent, planFromReply, plansErrorLine, plansFromOverview, plansFromReply } from './plans-map'
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
  expect(view.footer).toEqual(['phases 8'])
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

test('plansFromOverview keeps all plans when the active dir matches no root row', () => {
  const mapped = plansFromOverview(TWO_PROJECTS, '/home/mk/projects/gamma')
  expect(mapped.plans.map(p => p.id)).toEqual(['alpha-plan', 'beta-plan'])
  // an empty activeDir is the no-filter case too
  expect(plansFromOverview(TWO_PROJECTS, '').plans.map(p => p.id)).toEqual(['alpha-plan', 'beta-plan'])
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
