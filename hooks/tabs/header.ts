// The mock run header and the mock tab-bar counts (plan dashboard-v2-p0b,
// V0.4): an accessor over the six mock tabs' OWN fixture data, in a file of
// V0's own so the tab modules and their fixtures stay untouched for V1/V2
// (the brief's ownership line). Reading a fixture const is not editing it.
//
// The mockup boards carry the "Checkout v2" world in every tab's fixture; the
// Overview's fixture is the one with the run block, so the header and the plan
// count come from it and the rest from their own files.
import type { MkDashTab } from '../../types'
import { LANES_FIXTURE } from './lanes.fixture'
import { OVERVIEW_FIXTURE } from './overview.fixture'
import { WAITING_FIXTURE } from './waiting.fixture'

/** What the run header draws: the name, the stage chip's text, the ▶ Next line,
 *  the dim id line (dropped under NARROW) and whether this is a mock tab. */
export type RunHeaderData = {
  name: string
  stage: string
  next: string
  sub: string
  mock: boolean
}

/** The mock run header: the Overview fixture's run block (the same "Checkout
 *  v2" world every mock tab is drawn from). */
export function mockRunHeader(): RunHeaderData {
  const run = OVERVIEW_FIXTURE.run
  return {
    name: run.name,
    stage: `${run.stage}/${run.stages} ${run.stageWord}`,
    next: run.next,
    sub: `${run.id} · run ${run.short}`,
    mock: true,
  }
}

/** The Waiting tab's count: `!n` while decisions are open, nothing at all when
 *  none are (never `!0`). */
export function waitingCount(openDecisions: number): string {
  return openDecisions > 0 ? `!${openDecisions}` : ''
}

/** The tab bar's per-tab counts on a mock tab, from the same fixtures:
 *  Lanes = live lanes, Plan = done/total, Waiting = open decisions
 *  (Controls.dc.html's `l 4`, `p 3/8`, `w !1`). The other tabs carry none. */
export function mockTabCounts(): Record<MkDashTab, string> {
  const plan = OVERVIEW_FIXTURE.plan
  return {
    overview: '',
    lanes: String(LANES_FIXTURE.lanes.length),
    plan: `${plan.done}/${plan.total}`,
    usage: '',
    events: '',
    waiting: waitingCount(WAITING_FIXTURE.options.length > 0 ? 1 : 0),
    kanban: '',
    old: '',
    settings: '',
  }
}

/** The loop's stage names (skills/matrixkoder/SKILL.md's stage table), the
 *  same numbering the mockup's `6/12 Watch` uses: stage 6 is Watch. */
export const STAGE_WORDS: readonly string[] = [
  'Preflight', 'Plan', 'Route', 'Lanes', 'Brief', 'Dispatch', 'Watch',
  'Verify', 'Review', 'Fix', 'Merge', 'QA', 'Final',
]

/** The highest stage number (12), so a header can print `7/12`. */
export const STAGE_TOTAL = 12

/** A stage number's word; out-of-range numbers print nothing. */
export function stageWord(stage: number): string {
  return STAGE_WORDS[stage] ?? ''
}
