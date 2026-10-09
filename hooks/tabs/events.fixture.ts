// The Events tab's "Checkout v2" fixture (dashboard-v2-p0, T1.3): artboard
// E's Timeline card (dashboard-v2-mockups/Waiting.dc.html) verbatim, newest
// first, plus the footer's earlier count. Pure data — the render, the cap and
// the type colours live in events.tsx; this file never imports them.
import type { EventsView } from './events'

/** the mockup's timeline rows, copied as-is (C6): the fuller artboard-E text.
 *  `short` is the Narrow artboard's own words for the same row (its type word
 *  and text); the two rows the Narrow board drops carry no short form and fall
 *  back to the wide one under NARROW. */
export const EVENTS_FIXTURE: EventsView = {
  mock: true,
  events: [
    { t: '17:44', type: 'review', text: 'P5 r2 sent to opencode glm-5 · r1: 1 blocker, 2 minor', short: { type: 'review', text: 'P5 r2 → glm-5' } },
    { t: '17:43', type: 'fix', text: 'P5 9d17c4e fix(checkout): address r1 findings', short: { type: 'fix', text: 'P5 9d17c4e' } },
    { t: '17:41', type: 'blocked', text: 'P6 depends on P4 · waiting on you', short: { type: 'blocked', text: 'P6 · P4' } },
    { t: '17:40', type: 'quota', text: 'agy 5h exhausted · reset 19:05 · P7 held', short: { type: 'quota', text: 'agy ⊘ 19:05' } },
    { t: '17:38', type: 'dispatched', text: 'P4 R1 · pane wQ:p4 · lane-watch armed', short: { type: 'disp', text: 'P4 R1 wQ:p4' } },
    { t: '17:37', type: 'route', text: 'P4 → codex gpt-6.1-sol high (0.81)', short: { type: 'route', text: 'P4 codex .81' } },
    { t: '17:37', type: 'brief', text: 'P4, P5, P6, P7 brief-r1.md written', short: { type: 'brief', text: 'P4 P5 r1' } },
    { t: '17:35', type: 'lanes', text: 'worktrees p4 p5 p6 p7 created from mk-checkout-v2', short: { type: 'lanes', text: 'p4–p7' } },
    { t: '17:33', type: 'stage', text: '5 Brief → 6 Watch', short: { type: 'stage', text: '5 → 6' } },
    { t: '16:52', type: 'merged', text: 'W1 (P1 P2 P3) into mk-checkout-v2 · 3 commits', short: { type: 'merged', text: 'W1' } },
    { t: '16:50', type: 'verified', text: 'P3 gates · tests 410/2 known · lint 0 · tsc ok' },
    { t: '16:48', type: 'review', text: 'P3 r1 approved · codex gpt-6.1-mini · 0 findings' },
  ],
  earlierCount: 24,
}
