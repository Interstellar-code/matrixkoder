// Lanes tab fixture (plan dashboard-v2-p0, T2.1, C6): the mockups' "Checkout v2"
// world copied as-is from artboard C (Lanes) and artboard B (Narrow, the compact
// rows). Module consts only — committed code never reads the mockups or
// lane-only .matrixkoder-* files.
import type { LanesView, Seg, Tone } from './lanes'

const s = (text: string, tone?: Tone, bold?: boolean): Seg => ({ text, tone, bold })

export const LANES_FIXTURE: Omit<LanesView, 'mock'> = {
  summary: '4 lanes · 2 live · 2 held',
  summaryRight: 'stall warn 8m · takeover 20m',
  lanes: [
    {
      status: 'building',
      statusTone: 'suggestion',
      task: 'P4',
      title: 'Payment provider adapter',
      right: 'R1 · 14m',
      rows: [
        [s('  agent  ', 'dim'), s('CODEX · gpt-6.1-sol · effort high · pane wQ:p4')],
        [s('  worktree  ', 'dim'), s('.worktrees/matrixkoder-worktree-p4 · branch mk/p4-payments', 'dim')],
        [s('  last commit  ', 'dim'), s('2b8e41d · 2m ago')],
        [s('  feat(payments): stripe adapter + webhook verify')],
        [s('  activity  ', 'dim'), s('pane active 40s ago · no stall', 'success')],
      ],
      gate: [
        { name: 'tests', lane: [s('412 pass / 2 known fail', 'success')], baseline: '410 / 2' },
        { name: 'lint', lane: [s('0 errors', 'success')], baseline: '0' },
        { name: 'typecheck', lane: [s('ok', 'success')], baseline: 'ok' },
        { name: 'review', lane: [s('not yet · after gates', 'dim')], baseline: '—' },
      ],
      gateOneLine: 'tests 412/2 · lint 0 · tsc ok',
      actions: '  [Open pane] [Nudge] [Takeover]',
      compact: {
        header: '● build 14m',
        statusTone: 'suggestion',
        right: 'P4 R1',
        rows: [
          { text: '  codex gpt-6.1-sol high' },
          { text: '  2b8e41d stripe adapter' },
          { text: '  tests 412/2 · lint 0 · tsc ok', tone: 'success' },
        ],
      },
    },
    {
      status: 'review',
      statusTone: 'suggestion',
      task: 'P5',
      title: 'Checkout UI',
      right: 'r2 · 31m total',
      rows: [
        [s('  agent  ', 'dim'), s('CLAUDE · sonnet · effort medium · pane wQ:p5')],
        [s('  reviewer  ', 'dim'), s('OPENCODE · glm-5 · separate lane, read-only')],
        [s('  worktree  ', 'dim'), s('.worktrees/matrixkoder-worktree-p5 · branch mk/p5-checkout-ui', 'dim')],
        [s('  last commit  ', 'dim'), s('9d17c4e · 7m ago')],
        [s('  fix(checkout): address r1 findings')],
        [s('  activity  ', 'dim'), s('! pane silent 6m · warn at 8m', 'warning')],
      ],
      gate: [
        { name: 'tests', lane: [s('418 pass / 2 known fail', 'success')], baseline: '410 / 2' },
        { name: 'lint', lane: [s('0 errors', 'success')], baseline: '0' },
        {
          name: 'review r1',
          lane: [s('1 blocker', 'error'), s(' · 2 minor', 'warning'), s(' · fixed in 9d17c4e')],
          baseline: '—',
        },
        { name: 'review r2', lane: [s('in progress · 4m')], baseline: '—' },
      ],
      gateOneLine: 'tests 418/2 · lint 0 · 1 blocker · 2 minor',
      actions: '  [Open pane] [Nudge] [Takeover]',
      compact: {
        header: '◐ review r2',
        statusTone: 'suggestion',
        right: 'silent 6m',
        rows: [
          { text: '  claude sonnet medium' },
          { text: '  9d17c4e fix r1 findings' },
          { text: '  1 blocker · 2 minor · 0 open' },
        ],
      },
    },
    {
      status: 'blocked',
      statusTone: 'warning',
      task: 'P6',
      title: 'Tax rules',
      right: 'not dispatched',
      rows: [
        [s('  route  ', 'dim'), s('OPENCODE · glm-5 · effort high · conf 0.77')],
        [s('  worktree  ', 'dim'), s('.worktrees/matrixkoder-worktree-p6 · created, empty', 'dim')],
        [s('  blocked by  ', 'dim'), s('P4 migration `tax_lines` not merged · decision open in Waiting', 'warning')],
      ],
      gate: [],
      gateOneLine: '',
      actions: '  [Go to Waiting] [Dispatch anyway]',
      compact: {
        header: '⊘ blocked',
        statusTone: 'warning',
        right: 'P6',
        rows: [{ text: '  opencode glm-5 · needs P4' }],
      },
    },
    {
      status: 'quota',
      statusTone: 'warning',
      task: 'P7',
      title: 'Confirmation emails',
      right: 'held 9m',
      rows: [
        [s('  route  ', 'dim'), s('AGY · gemini-3.8-flash · effort medium · conf 0.74')],
        [s('  quota  ', 'dim'), s('agy 5h window exhausted · resets 19:05 (1h23m)', 'warning')],
        [s('  fallback  ', 'dim'), s('Jev reroute candidates: opencode glm-5 high · codex gpt-6.1-mini medium')],
      ],
      gate: [],
      gateOneLine: '',
      actions: '  [Reroute] [Wait]',
      compact: {
        header: '⧗ quota',
        statusTone: 'warning',
        right: 'P7 · 19:05',
        rows: [{ text: '  agy gemini-3.8-flash' }],
      },
    },
  ],
  footerLeft: 'lane-watch bg · p4 p5 armed',
  footerRight: 'sweep 17:42 · next 17:43',
}
