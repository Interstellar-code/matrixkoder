// The Plan tab's "Checkout v2" mock world (dashboard-v2-p0, T1.2): artboard
// A's Plan view copied verbatim (C6, Main.dc.html L140-159) plus ONE
// documented addition — P0, a done task, because the plan's acceptance line
// demands merged (◈) and done (✓) draw differently and the artboard's waves
// hold only merged work. The Narrow artboard's shorter titles/rights are its
// own words (Narrow.dc.html L116-129), not derived cuts.
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
        { id: 'P1', title: 'Cart schema migration', status: 'merged', right: 'codex · a41f9c2', short: { title: 'Cart schema', right: 'a41f9c2' } },
        { id: 'P2', title: 'Price engine', status: 'merged', right: 'opencode · 7c0e3d1', short: { title: 'Price engine', right: '7c0e3d1' } },
        { id: 'P3', title: 'Address API', status: 'merged', right: 'agy · c3b7a08', short: { title: 'Address API', right: 'c3b7a08' } },
      ],
    },
    {
      name: 'W2',
      header: '● 2 live · 2 held',
      tone: 'blue',
      rows: [
        { id: 'P4', title: 'Payment provider adapter', status: 'building', right: 'building 14m', short: { title: 'Payments', right: 'build' } },
        { id: 'P5', title: 'Checkout UI', status: 'review', right: 'review r2', short: { title: 'Checkout UI', right: 'review' } },
        { id: 'P6', title: 'Tax rules', status: 'blocked', right: 'blocked · P4', short: { title: 'Tax rules', right: 'blocked' } },
        { id: 'P7', title: 'Confirmation emails', status: 'quota', right: 'quota', short: { title: 'Emails', right: 'quota' } },
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
