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
    { id: 'P4', title: 'Payment provider adapter', status: 'building', note: 'build 14m', short: 'build 14m' },
    { id: 'P5', title: 'Checkout UI', status: 'review', note: 'review r2', short: 'review' },
    { id: 'P6', title: 'Tax rules', status: 'blocked', note: 'blocked P4', short: 'blocked' },
    { id: 'P7', title: 'Confirmation emails', status: 'quota', note: 'quota 19:05', short: 'quota' },
    { id: 'P8', title: 'E2E checkout QA', status: 'planned', note: 'W3', short: 'plan' },
  ],
  lanes: [
    { slot: 'p4', agent: 'codex', modelShort: 'sol', effort: 'high', status: 'building', short: 'build' },
    { slot: 'p5', agent: 'claude', modelShort: 'sonnet', effort: 'medium', status: 'review', short: 'review' },
    { slot: 'p6', agent: 'opencode', modelShort: 'glm-5', effort: 'high', status: 'blocked', short: 'blocked' },
    { slot: 'p7', agent: 'agy', modelShort: 'flash', effort: 'medium', status: 'quota', short: 'quota' },
  ],
  usage: [
    { label: 'claude (all)', short: 'claude', pct: 38, weekPct: 61, reset: '2h10m', exhausted: false, until: null },
    { label: 'codex', short: 'codex', pct: 72, weekPct: 44, reset: '1h02m', exhausted: false, until: null },
    { label: 'agy', short: 'agy', pct: 100, weekPct: 58, reset: null, exhausted: true, until: '19:05' },
    { label: 'opencode go', short: 'opencode', pct: 41, weekPct: 23, reset: '3h44m', exhausted: false, until: null },
  ],
  waiting: {
    since: '17:41',
    question: "P6 needs P4's schema change, not merged. Hold P6 until P4 merges?",
    options: ['Hold P6', 'Rebase later', 'Fold'],
    rec: 0,
    // the Narrow board's waiting card: the short question and its three buttons
    shortQuestion: 'Hold P6 until P4 merges?',
    shortOptions: ['Hold', 'Start now', 'Fold in P4'],
  },
  recent: [
    { t: '17:44', text: 'P5 review r2 → glm-5' },
    { t: '17:41', text: 'P6 blocked · waiting on you' },
    { t: '17:38', text: 'P4 R1 dispatched · wQ:p4' },
  ],
}
