// The Plan tab's "Checkout v2" mock world (dashboard-v2-p0, T1.2; V1.2 fold
// tree): artboard A's Plan view copied verbatim (C6, Main.dc.html L140-159)
// plus ONE documented addition — P0, a done task, because the plan's
// acceptance line demands merged (◈) and done (✓) draw differently and the
// artboard's waves hold only merged work — and, for V1.2's fold/expand,
// subtasks under P1 and the four W2 phases (Plan.dc.html's tree). The Narrow
// artboard's shorter titles/rights are its own words (Narrow.dc.html L116-129),
// not derived cuts.
import type { PlanView } from './plan'

export const PLAN_FIXTURE: PlanView = {
  mock: true,
  waves: [
    {
      name: 'W1',
      header: '✓ merged 16:52',
      tone: 'green',
      rows: [
        // the one fixture addition: a done task from the same world, so the
        // merged-vs-done marker split is real and testable
        { id: 'P0', title: 'Spike: checkout payloads', status: 'done', right: 'done', short: { title: 'Spike payloads', right: 'done' } },
        {
          id: 'P1', title: 'Cart schema migration', status: 'merged', right: 'codex · a41f9c2', short: { title: 'Cart schema', right: 'a41f9c2' },
          subs: [
            { id: 'P1.1', title: 'Schema migration + backfill', status: 'merged', right: 'r1' },
            { id: 'P1.2', title: 'Codex review r1 · 0 findings', status: 'done', right: '0 findings', rightRole: 'ok' },
          ],
        },
        { id: 'P2', title: 'Price engine', status: 'merged', right: 'opencode · 7c0e3d1', short: { title: 'Price engine', right: '7c0e3d1' } },
        { id: 'P3', title: 'Address API', status: 'merged', right: 'agy · c3b7a08', short: { title: 'Address API', right: 'c3b7a08' } },
      ],
    },
    {
      name: 'W2',
      header: '● 2 live · 2 held',
      tone: 'blue',
      rows: [
        {
          id: 'P4', title: 'Payment provider adapter', status: 'building', right: 'building 14m', short: { title: 'Payments', right: 'build' },
          subs: [
            { id: 'P4.1', title: 'R1 stripe adapter + webhook verify', status: 'building', right: 'r1' },
            { id: 'P4.2', title: 'Gates green: tests 412/2 · lint 0 · tsc ok', status: 'verified', right: 'r1' },
          ],
        },
        {
          id: 'P5', title: 'Checkout UI', status: 'review', right: 'review r2', short: { title: 'Checkout UI', right: 'review' },
          subs: [
            { id: 'P5.1', title: 'R1 findings: 1 blocker · 2 minor', status: 'fix', right: '2 findings', rightRole: 'warn' },
            { id: 'P5.2', title: 'R2 in review · opencode glm-5', status: 'review', right: 'r2' },
          ],
        },
        {
          id: 'P6', title: 'Tax rules', status: 'blocked', right: 'blocked · P4', short: { title: 'Tax rules', right: 'blocked' },
          subs: [
            { id: 'P6.1', title: 'Held: needs the P4 schema merge', status: 'blocked', right: 'held', rightRole: 'warn' },
          ],
        },
        {
          id: 'P7', title: 'Confirmation emails', status: 'quota', right: 'quota', short: { title: 'Emails', right: 'quota' },
          subs: [
            { id: 'P7.1', title: 'Held: agy 5h exhausted · reset 19:05', status: 'quota', right: '19:05', rightRole: 'warn' },
          ],
        },
      ],
    },
    {
      name: 'W3',
      header: '○ planned',
      tone: 'gray',
      shortHeader: '○',
      rows: [
        { id: 'P8', title: 'E2E checkout QA (Canary, 6 flows)', status: 'planned', right: 'after W2 merge', short: { title: 'E2E QA', right: '6 flows' } },
      ],
    },
  ],
  footer: ['Plan: .matrixkoder/plans/checkout-v2.md', 'briefs r1 written for P4–P7'],
}

// The real plan, for the V1.2 detail tests. Source: `.matrixkoder/plans/settings-plans-db.md`
// (status running, created 2026-10-13, runs settings-plans-db-r1, integration main, design
// settings-tab / settings-wire / dashboard-v2 / mk-store; phases R1 T1 D1 S1 V1 with their
// real task ids and titles). Statuses follow PlanDetail.dc.html: merged phases, S1 building,
// V1 in progress. R1 and T1 share wave 1, so each row shows `with` the other.
export const PLAN_REAL_FIXTURE: PlanView = {
  mock: true,
  plan: {
    id: 'settings-plans-db',
    title: 'Settings mapping, plans in the DB, Plan sub-tabs',
    status: 'running',
    statusChip: '● running · r1 · 7/12',
    created: '2026-10-13',
    runs: 'settings-plans-db-r1',
    mergesTo: 'main',
    counts: '14 tasks · 5 phases',
  },
  phases: [
    { id: 'R1', title: 'Self-tests never write a real DB', status: 'merged', header: '✓ merged', headerRole: 'ok', wave: 1,
      rows: [
        { id: 'R1.1', title: 'One home rule', status: 'merged', right: 'merged' },
        { id: 'R1.2', title: 'Write guard', status: 'merged', right: 'merged' },
        { id: 'R1.3', title: 'Register matrixkoder', status: 'done', right: 'done' },
      ], note: 'lane mk-3 · merged to main' },
    { id: 'T1', title: 'Plan template is the contract', status: 'merged', header: '✓ merged', headerRole: 'ok', wave: 1,
      rows: [
        { id: 'T1.1', title: 'templates/plan.md DB homes', status: 'merged', right: 'merged' },
        { id: 'T1.2', title: 'SKILL.md and plan check', status: 'merged', right: 'merged' },
      ] },
    { id: 'D1', title: 'Plans in the project DB', status: 'merged', header: '✓ merged 21:10', headerRole: 'ok', wave: 2,
      rows: [
        { id: 'D1.1', title: 'plans columns, mkdb plans query', status: 'merged', right: 'a41f9c2', last_commit: 'a41f9c2e5d1' },
        { id: 'D1.2', title: 'import, delete, pause', status: 'merged', right: '7c0e3d1', last_commit: '7c0e3d1b902' },
        { id: 'D1.3', title: 'template and schema docs', status: 'merged', right: 'c3b7a08', last_commit: 'c3b7a0844aa' },
        { id: 'D1.4', title: 'First real import', status: 'done', right: 'done' },
      ], note: 'lane mk-3 · merged to main' },
    { id: 'S1', title: 'Settings mapping and cleanup', status: 'building', header: '● building 22m', headerRole: 'live', wave: 3,
      description: 'Each setting is matched to the folder it belongs in. Keys nobody reads any more are removed, and the sample data is brought back in line.',
      rows: [
        { id: 'S1.1', title: 'scope-home map', status: 'merged', right: 'r1' },
        { id: 'S1.2', title: 'dead keys, fixture drift', status: 'building', right: 'building', round: 1,
          description: 'Finds settings keys that no screen reads and deletes them. Also refreshes the sample data so it matches the real reply.' },
      ], note: 'lane mk-4 · opencode · 2 subagents' },
    { id: 'V1', title: 'Plan tab sub-tabs', status: 'building', header: '2 findings', headerRole: 'warn', wave: 3,
      rows: [
        { id: 'V1.1', title: 'List with plan actions', status: 'merged', right: 'r1' },
        { id: 'V1.2', title: 'Detail by phase, folded', status: 'fix', right: '2 findings', findings: 2,
          subs: [{ id: 'V1.2a', title: 'fold state per phase', status: 'fix', right: '2 findings', rightRole: 'warn' }] },
        { id: 'V1.3', title: 'Kanban filter, wiring', status: 'building', right: 'building', round: 1 },
      ], note: 'lane mk-3 · after D1 · 3 subagents' },
  ],
  footer: ['5 phases · all folded by default', 'source project'],
}
