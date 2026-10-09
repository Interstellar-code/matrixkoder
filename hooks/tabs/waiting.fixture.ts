// Waiting tab fixture (plan dashboard-v2-p0, T2.3, C6): the mockups' "Checkout v2"
// decision card copied as-is from artboard E (Waiting). Module consts only —
// committed code never reads the mockups or lane-only .matrixkoder-* files.
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
      recommended: true,
      explanation: 'No rebase churn. P4 expected to merge in ~20m. P6 brief stays r1.',
    },
    {
      key: 'B',
      label: 'Start P6 on current main, rebase after P4',
      explanation: 'Runs in parallel now. Conflict risk in db/migrations/; brief gets a rebase step.',
    },
    {
      key: 'C',
      label: "Fold P6 into P4's lane as R2",
      explanation: 'One agent owns schema + tax. Longer round, codex quota at 72%.',
    },
  ],
  actions: ['[Confirm A] [Ask in chat]', 'or type "mk: pick A"'],
  footerLeft: 'wake: off · reply in chat or here',
  footerRight: '17:45',
}
