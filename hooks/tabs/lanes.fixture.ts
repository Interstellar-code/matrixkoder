// Lanes tab fixture (plan dashboard-v2-p0b, V2.1, C6): the mockups' "Checkout v2"
// world copied from the boards Main (Lanes, 46 cols) and Narrow (Lanes, 36 cols).
// Module consts only — committed code never reads the mockups or lane-only
// .matrixkoder-* files.
//
// The p0 boards' field names win over p0's: a lane carries what its Card draws
// — status, task, round/age, the stage strip's counts, the agent line (with the
// Narrow board's short form), the last commit, the gate table with each row's
// compact chip, the review counts, the activity line and its dim action text.
// The old free-form `rows`/`compact`/`gateOneLine` blobs went with the layout
// that flattened them.
import type { GateRow, LanesView, Seg, Tone } from './lanes'

const s = (text: string, tone?: Tone, bold?: boolean): Seg => ({ text, tone, bold })

/** a gate row with the Narrow board's chip (dropped when a gate has no chip:
 *  the review rounds are counts now, not rows). */
const gate = (name: string, laneText: string, tone: Tone | undefined, baseline: string, chip?: string): GateRow => ({
  name,
  lane: [s(laneText, tone)],
  baseline,
  short: chip == null ? undefined : { label: chip, role: 'ok' as const },
})

export const LANES_FIXTURE: Omit<LanesView, 'mock'> = {
  summary: '4 lanes · 2 live · 2 held',
  summaryRight: 'stall warn 8m · takeover 20m',
  lanes: [
    {
      status: 'building',
      task: 'P4',
      title: 'Payment provider adapter',
      right: 'R1 · 14m',
      stage: { done: 5, total: 12 },
      agent: [s('codex · gpt-6.1-sol · effort high · pane wQ:p4')],
      agentShort: 'codex · sol · high',
      commit: { sha: '2b8e41d', subject: 'feat(payments): stripe adapter' },
      gate: [
        gate('tests', '412 / 2 known', 'success', '410 / 2', 't 412/2'),
        gate('lint', '0 errors', 'success', '0', 'l 0'),
        gate('typecheck', 'ok', 'success', 'ok', 'tsc ✓'),
        gate('review', 'after gates', 'dim', '—'),
      ],
      activity: [s('● '), s('pane active 40s ago · no stall', 'success')],
      actions: '  [Open pane] [Nudge] [Takeover]',
    },
    {
      status: 'review',
      task: 'P5',
      title: 'Checkout UI',
      right: 'r2 · 31m',
      stage: { done: 7, total: 12 },
      agent: [s('claude · sonnet · effort medium · rev glm-5')],
      agentShort: 'claude · sonnet · med',
      commit: { sha: '9d17c4e', subject: 'fix(checkout): address r1 findings' },
      gate: [
        gate('tests', '418 / 2 known', 'success', '410 / 2', 't 418/2'),
        gate('lint', '0 errors', 'success', '0', 'l 0'),
        gate('typecheck', 'ok', 'success', 'ok', 'tsc ✓'),
        gate('review r2', 'in progress · 4m', undefined, '—'),
      ],
      reviews: { blocker: 1, minor: 2, open: 0 },
      activity: [s('◐ '), s('pane silent 6m · warn at 8m', 'warning')],
      actions: '  [Open pane] [Nudge] [Takeover]',
    },
    {
      status: 'blocked',
      task: 'P6',
      title: 'Tax rules',
      right: 'not dispatched',
      agent: [s('opencode · glm-5 · effort high · conf 0.77')],
      agentShort: 'opencode · glm-5 · high',
      note: [s('needs P4 `tax_lines` · decision open in Waiting', 'warning')],
      gate: [],
      activity: [s('! '), s('blocked before dispatch', 'warning')],
      actions: '  [Go to Waiting] [Dispatch anyway]',
    },
    {
      status: 'quota',
      task: 'P7',
      title: 'Confirmation emails',
      right: 'held 9m',
      agent: [s('agy · gemini-3.8-flash · effort medium · conf 0.74')],
      agentShort: 'agy · gemini-3.8-flash',
      note: [s('agy 5h exhausted · resets 19:05 · Jev reroute ready', 'warning')],
      gate: [],
      activity: [s('⊘ '), s('held for quota', 'warning')],
      actions: '  [Reroute] [Wait]',
    },
  ],
  footerLeft: 'lane-watch bg · p4 p5 armed',
  footerRight: 'sweep 17:42 · next 17:43',
}