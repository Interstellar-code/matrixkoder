// Usage tab fixture (plan dashboard-v2-p0b, V2.2, C6): the mockups' "Checkout v2"
// world copied from the board Main's Usage tab — a Quota card (a name row, a 5h
// and a week bar per agent, the legend chips) and a Jev routing card in the
// board's one-line form. Module consts only — committed code never reads the
// mockups or lane-only .matrixkoder-* files.
//
// p0's rows carried a `tone` per bar and a `reset` per row; V2's bars take their
// colour from the gradient itself, so the tone went. The board's `pick` is the
// short route form, which is why each decision carries both.
import type { PaletteRole } from './palette'
import type { Decision, QuotaRow, UsageView } from './usage'

const quota = (pct: number, used: string, floor?: number) => ({ pct, used, floor })

/** a pool row under its agent (the board's `└ go`). */
const pool = (name: string, five: QuotaRow['five'], week: QuotaRow['week'], reset: string): QuotaRow => ({
  name,
  nameTone: 'dim',
  five,
  week,
  reset,
})

/** a routing decision in the board's one-line form. */
const decision = (task: string, pick: string, pickShort: string, conf: string, rule: string, candidates: string[]): Decision =>
  ({ task, pick, pickShort, conf, rule, candidates })

export const USAGE_FIXTURE: Omit<UsageView, 'mock'> = {
  headerLeft: 'Quota',
  headerRight: 'used · floor 20% left',
  rows: [
    { name: 'claude (all)', five: quota(38, '38%', 80), week: quota(61, '61%', 80), reset: '2h10m' },
    pool('└ fable', quota(9, '9%'), quota(17, '17%'), '10-13'),
    { name: 'codex', five: quota(72, '72%', 80), week: quota(44, '44%', 80), reset: '1h02m' },
    // the 5h window is spent: the name takes the err colour, the 5h bar is full red
    { name: 'agy', five: quota(100, '⊘'), week: quota(58, '58%', 80), reset: '19:05' },
    { name: 'opencode', note: 'provider pools below', noteTone: 'dim' },
    pool('└ go', quota(41, '41%', 80), quota(23, '23%', 80), '3h44m'),
    { name: '└ glm-5 (zai)', nameTone: 'dim', note: 'metered per token · no window · ok', noteTone: 'dim' },
    { name: '└ ollama', nameTone: 'dim', note: 'sign in at ollama.com', noteTone: 'warning' },
  ],
  legend: [
    { label: '<50%', role: 'ok' },
    { label: '50–80', role: 'warn' },
    { label: '>80 ⊘', role: 'err' },
    { label: '▄ floor', role: 'idle' },
  ],
  actions: 'usage.json 17:42 · CodexBar · poll 5m',
  decisionsHeader: 'Jev routing',
  decisionsRight: 'this run · 5 calls',
  decisions: [
    decision('P4', 'codex gpt-6.1-sol high', 'codex sol high', '0.81', 'top pick, no rule', [
      'codex gpt-6.1-sol high', 'claude sonnet medium', 'opencode glm-5 high', 'agy gemini-3.8-flash medium',
    ]),
    decision('P5', 'claude sonnet medium', 'claude sonnet med', '0.58', 'split haiku/sonnet → build path, one tier up', [
      'claude haiku high', 'claude sonnet medium', 'codex gpt-6.1-mini medium', 'opencode glm-5 medium',
    ]),
    decision('P6', 'opencode glm-5 high', 'opencode glm-5', '0.77', 'spare claude wins over accuracy here', [
      'opencode glm-5 high', 'codex gpt-6.1-mini high', 'claude sonnet medium',
    ]),
    decision('P7', 'agy gemini-3.8-flash medium', 'agy flash med', '0.74', 'agy exhausted since 17:40 · reroute pending', [
      'agy gemini-3.8-flash medium', 'opencode glm-5 low', 'claude haiku low',
    ]),
    decision('P5 review', 'opencode glm-5 high', 'opencode glm-5', '0.61', 'read-only, split → lower of top two', [
      'opencode glm-5 high', 'codex gpt-6.1-mini high', 'claude opus medium',
    ]),
  ],
  routingHint: '↵ candidates for the selected row',
  footerLeft: 'priorities: spare claude › accuracy › speed',
  footerRight: 'jev 5 calls · 0 escaped',
}