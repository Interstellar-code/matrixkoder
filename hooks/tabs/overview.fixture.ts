// The Overview tab's fixture (plan dashboard-v2-p0, T1.1): the "Checkout v2"
// artboard-A data verbatim (C6) — overview.tsx renders it in the pane's
// terminal voice and overviewView() stamps the mock flag on top.
import type { OverviewView } from './overview'

export const OVERVIEW_FIXTURE: Omit<OverviewView, 'mock'> = {
  run: {
    name: 'Checkout v2',
    id: 'mk-checkout-v2',
    short: '0f3a',
    stage: 6,
    stages: 12,
    stageWord: 'Watch',
    next: 'watching P4 R1 (lane-watch bg) · P5 review r2 in flight · then merge W2 into mk-checkout-v2',
  },
  plan: { done: 3, total: 8 },
  // the wave summary pills: glyph strings verbatim from the board
  waves: [
    { label: 'W1', glyphs: '✓✓✓', tone: 'done' },
    { label: 'W2', glyphs: '●◐!⊘', tone: 'active' },
    { label: 'W3', glyphs: '○', tone: 'planned' },
  ],
  // the Overview's short task list: `note` is the wide right column, `short`
  // the compact word (the board's narrow artboard)
  tasks: [
    { id: 'P4', title: 'Payment provider adapter', status: 'building', note: 'building 14m', short: 'build 14m' },
    { id: 'P5', title: 'Checkout UI', status: 'review', note: 'review r2', short: 'review' },
    { id: 'P6', title: 'Tax rules', status: 'blocked', note: 'blocked · needs P4', short: 'blocked' },
    { id: 'P7', title: 'Confirmation emails', status: 'quota', note: 'quota · agy reset 19:05', short: 'quota' },
    { id: 'P8', title: 'E2E checkout QA', status: 'planned', note: 'planned W3', short: 'plan' },
  ],
  lanes: [
    { slot: 'p4', agent: 'codex', model: 'gpt-6.1-sol', modelShort: 'sol', effort: 'high', status: 'building', note: 'building', short: 'build' },
    { slot: 'p5', agent: 'claude', model: 'sonnet', modelShort: 'sonnet', effort: 'medium', status: 'review', note: 'review r2', short: 'review' },
    { slot: 'p6', agent: 'opencode', model: 'glm-5', modelShort: 'glm-5', effort: 'high', status: 'blocked', note: 'blocked', short: 'blocked' },
    { slot: 'p7', agent: 'agy', model: 'gemini-3.8-flash', modelShort: 'flash', effort: 'medium', status: 'quota', note: 'quota', short: 'quota' },
  ],
  usage: [
    { label: 'claude (all)', short: 'claude', pct: 38, weekPct: 61, reset: '2h10m', exhausted: false, until: null },
    { label: 'codex', short: 'codex', pct: 72, weekPct: 44, reset: '1h02m', exhausted: false, until: null },
    { label: 'agy', short: 'agy', pct: 100, weekPct: 58, reset: null, exhausted: true, until: '19:05' },
    { label: 'opencode go', short: 'opencode', pct: 41, weekPct: 23, reset: '3h44m', exhausted: false, until: null },
  ],
  waiting: {
    since: '17:41',
    question: "P6 Tax rules depends on P4's schema change, not yet merged. Hold P6 until P4 merges?",
    options: ['Hold P6 (recommended)', 'Start on main, rebase later', 'Fold into P4 lane'],
    rec: 0,
  },
  recent: [
    { t: '17:44', text: 'P5 review r2 sent to opencode glm-5 (3 findings r1)' },
    { t: '17:41', text: 'P6 blocked: depends on P4 · waiting on you' },
    { t: '17:38', text: 'dispatched P4 R1, pane wQ:p4 · commit 2b8e41d' },
  ],
}
