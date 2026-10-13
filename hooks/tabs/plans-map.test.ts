// Mappers from captured mkdb plans JSON to PlansView / PlanView.
import { test, expect } from 'claude-code/testing'
import list from './fixtures/plans-list'
import detail from './fixtures/plan-detail'
import { planFromReply, plansErrorLine, plansFromReply } from './plans-map'
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
  expect(view.plan?.counts).toBe('14 tasks · 5 phases')
  expect(view.footer).toEqual(['phases 5'])
  expect(view.phases?.[0]?.rows.length).toBe(3)
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
