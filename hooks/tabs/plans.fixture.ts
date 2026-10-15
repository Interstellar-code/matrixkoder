// Fixture for the Plan List sub-tab (V1.1). Source: the real plan
// `.matrixkoder/plans/settings-plans-db.md` (header status running, created
// 2026-10-13, runs settings-plans-db-r1, integration main, design the four
// plan files, phases R1 T1 D1 S1 V1 and their task ids) plus the real runs
// settings-plans-db-r1 and mk-bus-e2e-r1 with the statuses the PlanList.dc.html
// mockup draws. The extra plans (mk-bus-e2e running, checkout-v2 approved,
// hive-cards parked, workflows-v2 done, lane-nudge draft) exist so every C10
// status has a row; their titles are the plans those runs belong to.
import type { PlansView } from './plans'

export const PLANS_FIXTURE: PlansView = {
  mock: true,
  runs: [
    {
      id: 'settings-plans-db-r1',
      plan: 'settings-plans-db',
      title: 'Settings mapping, plans in the DB, Plan sub-tabs',
      stage: '7/12 Verify',
      counts: { done: 4, live: 3, planned: 1 },
      countsLine: '4 done · 3 live · 1 planned · 3 phases',
    },
    {
      id: 'mk-bus-e2e-r1',
      plan: 'mk-bus-e2e',
      title: 'full working SSE bus between main and sub sessions',
      stage: '8/12 Review',
      counts: { done: 15, live: 4, planned: 2 },
      countsLine: '15 merged · 4 review · 2 planned',
    },
  ],
  plans: [
    {
      id: 'settings-plans-db',
      title: 'Settings mapping, plans in the DB, Plan sub-tabs',
      status: 'running',
      created: '2026-10-13',
      runs: ['settings-plans-db-r1'],
      live_run: 'settings-plans-db-r1',
      counts: { tasks: 14, phases: 5, byStatus: { merged: 8, done: 2, planned: 3, building: 1 } },
      mergesTo: 'main',
      design: ['settings-tab', 'settings-wire', 'dashboard-v2', 'mk-store'],
    },
    {
      id: 'mk-bus-e2e',
      title: 'full working SSE bus between main and sub sessions',
      status: 'running',
      created: '2026-10-11',
      runs: ['mk-bus-e2e-r1'],
      live_run: 'mk-bus-e2e-r1',
      counts: { tasks: 8, phases: 3, byStatus: { merged: 4, building: 2, planned: 2 } },
    },
    {
      id: 'checkout-v2',
      title: 'Checkout v2',
      status: 'approved',
      created: '2026-10-09',
      runs: [],
      live_run: null,
      counts: { tasks: 9, phases: 3, byStatus: { done: 5, planned: 4 } },
      project: 'matrixkoder',
    },
    {
      id: 'hive-cards',
      title: 'Hive cards',
      status: 'parked',
      created: '2026-10-08',
      runs: ['hive-cards-r1'],
      live_run: null,
      counts: { tasks: 4, phases: 2, byStatus: { parked: 4 } },
    },
    {
      id: 'workflows-v2',
      title: 'Workflows v2',
      status: 'done',
      created: '2026-10-02',
      runs: ['workflows-v2-r1'],
      live_run: null,
      counts: { tasks: 6, phases: 2, byStatus: { done: 6 } },
    },
    {
      id: 'lane-nudge',
      title: 'Lane nudge',
      status: 'draft',
      created: '2026-10-12',
      runs: [],
      live_run: null,
      counts: { tasks: 2, phases: 1, byStatus: { planned: 2 } },
    },
    {
      id: 'orphan-run',
      title: 'A running plan whose run has closed',
      status: 'running',
      created: '2026-10-10',
      runs: [],
      live_run: null,
      counts: { tasks: 1, phases: 1, byStatus: { building: 1 } },
    },
  ],
}
