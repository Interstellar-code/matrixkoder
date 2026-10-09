// Waiting tab fixture (plan dashboard-v2-p0b, V2.3, C6): the mockups' "Checkout v2"
// decision copied from the board Main's Waiting tab — the `! DECISION` chip over
// P6, the wrapped question and three options, each with its chip, its label, the
// board's short action word and its explanation. Module consts only — committed
// code never reads the mockups or lane-only .matrixkoder-* files.
//
// p0's options had no `action` word (the tab drew `[Confirm A] [Ask in chat]` as
// dim text); V2.3 makes them real ActionButtons, so each option carries the word
// its button shows — the mockup's chip form — and `actions` keeps only the hints
// the buttons do not replace.
import type { WaitingView } from './waiting'

export const WAITING_FIXTURE: Omit<WaitingView, 'mock'> = {
  header: 'P6 Tax rules',
  time: 'open since 17:41 · 3m',
  timeCompact: '17:41',
  question:
    "P6 reads the `tax_lines` table added by P4's migration (2b8e41d). P4 is still building, R1 at 14m, gates green so far. How should P6 proceed?",
  options: [
    {
      key: 'A',
      label: 'Hold P6 until P4 merges',
      action: 'Hold P6',
      recommended: true,
      explanation: 'No rebase churn. P4 expected to merge in ~20m. P6 brief stays r1.',
    },
    {
      key: 'B',
      label: 'Start P6 on current main, rebase after P4',
      action: 'Rebase later',
      explanation: 'Runs in parallel now. Conflict risk in db/migrations/; brief gets a rebase step.',
    },
    {
      key: 'C',
      label: "Fold P6 into P4's lane as R2",
      action: 'Fold',
      explanation: 'One agent owns schema + tax. Longer round, codex quota at 72%.',
    },
  ],
  actions: ['or type "mk: pick A"'],
  footerLeft: 'wake: off · reply in chat or here',
  footerRight: '17:45',
}
