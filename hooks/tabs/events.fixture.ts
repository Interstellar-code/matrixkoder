// The Events tab's visual pass fixture (dashboard-v2-p0b, Events tab redesign):
// Matches the HTML visual pass ("Events tab · bus + run, kind filters, pinned asks, lane activity")
// with the complete 15-event feed, bus metrics, lane activity, and 24 earlier events.
import type { EventsView } from './events'

export const EVENTS_FIXTURE: EventsView = {
  mock: true,
  bus: {
    up: true,
    lastAge: '37s',
    port: '127.0.0.1:7421',
    pid: 48213,
    runningSince: '09:12 today · 8h 34m',
    queue: { pending: 0, rate: 14, total: 1842 },
    files: { busJson: true, tokens: true, ringQueue: 0 },
    rateStart: '09:12',
    rateHistory: Array.from({ length: 40 }, (_, i) => ({
      pct: [4, 9, 12, 14, 8, 3, 11][i % 7] ?? 5,
      color: i % 7 === 3 ? '#e8b04a' : [4, 9, 12, 14, 8, 3, 11][i % 7]! > 8 ? '#19e35a' : '#0f4d22',
    })),
  },
  events: [
    // the inbox rows below are REAL rows: written by scripts/mk-inbox.py in a temp home (list, import of a
    // template plan and of a not-a-plan file, archive, restore, delete --by lane-1) and read back with
    // `mkdb.py events --chan inbox --json` on 2026-10-21; ten of the fifteen, id i-<events.id>, newest first
    { id: 'i-15', src: 'inbox', t: '23:16', date: '2026-10-21T23:16:16+02:00', kind: 'deleted', who: 'lane-1', by: 'lane-1', chan: 'inbox', file: 'ponytail-notes.md', plan: 'mk-ponytail-hooks', fit: 'not_a_plan', text: 'deleted ponytail-notes.md; a copy is kept in .deleted/mk-ponytail-hooks-20261021-231616/ponytail-notes.md', short: { type: 'del', text: 'ponytail-notes.md' } },
    { id: 'i-14', src: 'inbox', t: '23:16', date: '2026-10-21T23:16:16+02:00', kind: 'restored', who: 'user', by: 'user', chan: 'inbox', file: 'wizards-redesign.md', plan: 'wizards-redesign', fit: 'partial', text: 'restored wizards-redesign.md', short: { type: 'rest', text: 'wizards-redesign.md' } },
    { id: 'i-13', src: 'inbox', t: '23:16', date: '2026-10-21T23:16:16+02:00', kind: 'archived', who: 'user', by: 'user', chan: 'inbox', file: 'wizards-redesign.md', plan: 'wizards-redesign', fit: 'partial', text: 'archived wizards-redesign.md', short: { type: 'arch', text: 'wizards-redesign.md' } },
    { id: 'i-12', src: 'inbox', t: '23:16', date: '2026-10-21T23:16:15+02:00', kind: 'import-failed', who: 'user', by: 'user', chan: 'inbox', file: 'ponytail-notes.md', plan: 'mk-ponytail-hooks', fit: 'not_a_plan', text: 'import failed at step 1: no phase or task headings', short: { type: 'failed', text: 'ponytail-notes.md' } },
    { id: 'i-11', src: 'inbox', t: '23:16', date: '2026-10-21T23:16:15+02:00', kind: 'import-step', who: 'user', by: 'user', chan: 'inbox', file: 'ponytail-notes.md', plan: 'mk-ponytail-hooks', fit: 'not_a_plan', text: 'import step 1 of 3: Parse the template', short: { type: 'step', text: 'ponytail-notes.md' } },
    { id: 'i-10', src: 'inbox', t: '23:16', date: '2026-10-21T23:16:15+02:00', kind: 'imported', who: 'user', by: 'user', chan: 'inbox', file: 'plan-inbox-and-parsing.md', plan: 'plan-inbox-and-parsing', fit: 'template', text: 'imported plan-inbox-and-parsing.md as plan-inbox-and-parsing', short: { type: 'import', text: 'plan-inbox-and-parsing.md' } },
    { id: 'i-8', src: 'inbox', t: '23:16', date: '2026-10-21T23:16:15+02:00', kind: 'import-step', who: 'user', by: 'user', chan: 'inbox', file: 'plan-inbox-and-parsing.md', plan: 'plan-inbox-and-parsing', fit: 'template', text: 'import step 2 of 3: Write the project', short: { type: 'step', text: 'plan-inbox-and-parsing.md' } },
    { id: 'i-6', src: 'inbox', t: '23:16', date: '2026-10-21T23:16:15+02:00', kind: 'checked', who: 'user', by: 'user', chan: 'inbox', file: 'wizards-redesign.md', plan: 'wizards-redesign', fit: 'partial', text: 'checked wizards-redesign.md: partial, 6 of 7 sections', short: { type: 'check', text: 'wizards-redesign.md' } },
    { id: 'i-5', src: 'inbox', t: '23:16', date: '2026-10-21T23:16:15+02:00', kind: 'found', who: 'user', by: 'user', chan: 'inbox', file: 'wizards-redesign.md', plan: 'wizards-redesign', fit: 'partial', text: 'found wizards-redesign.md', short: { type: 'found', text: 'wizards-redesign.md' } },
    { id: 'i-1', src: 'inbox', t: '23:16', date: '2026-10-21T23:16:15+02:00', kind: 'found', who: 'user', by: 'user', chan: 'inbox', file: 'plan-inbox-and-parsing.md', plan: 'plan-inbox-and-parsing', fit: 'template', text: 'found plan-inbox-and-parsing.md', short: { type: 'found', text: 'plan-inbox-and-parsing.md' } },
    { id: 'm-141', src: 'bus', t: '17:46', kind: 'ask', type: 'ask', who: 'p5→orch', lane: 'p5', ack: 1, text: 'review r2 touches checkout.ts, ok to widen scope?', short: { type: 'ask', text: 'review r2 touches checkout.ts' } },
    { id: 'm-140', src: 'bus', t: '17:45', kind: 'status', type: 'status', who: 'p4→orch', lane: 'p4', ack: 2, text: 'P4 building · 5/12 · tests 412/2', short: { type: 'stat', text: 'P4 building · 5/12' } },
    { id: 'r-88', src: 'run', t: '17:44', kind: 'review', type: 'review', who: 'run', lane: 'p5', ack: 0, text: 'P5 r2 sent to opencode glm-5 · r1: 1 blocker, 2 minor', short: { type: 'review', text: 'P5 r2 → glm-5' } },
    { id: 'm-139', src: 'bus', t: '17:44', kind: 'ring-failed', type: 'ring-failed', who: 'orch→p7', lane: 'p7', ack: 0, bad: true, text: 'pane wQ:p7 not found, nudge not typed', short: { type: 'fail', text: 'pane wQ:p7 not found' } },
    { id: 'm-138', src: 'bus', t: '17:43', kind: 'commit', type: 'commit', who: 'p5→orch', lane: 'p5', ack: 2, text: '9d17c4e fix(checkout): address r1 findings', short: { type: 'commit', text: 'P5 9d17c4e' } },
    { id: 'm-137', src: 'bus', t: '17:42', kind: 'ask', type: 'ask', who: 'p6→orch', lane: 'p6', ack: 1, text: 'P6 depends on P4 types, wait or stub?', short: { type: 'ask', text: 'P6 depends on P4' } },
    { id: 'r-87', src: 'run', t: '17:41', kind: 'blocked', type: 'blocked', who: 'run', lane: 'p6', ack: 0, text: 'P6 depends on P4 · waiting on you', short: { type: 'blocked', text: 'P6 · P4' } },
    { id: 'm-136', src: 'bus', t: '17:40', kind: 'quota', type: 'quota', who: 'p7→orch', lane: 'p7', ack: 2, text: 'agy 5h window exhausted, holding · reset 19:05', short: { type: 'quota', text: 'agy ⊘ 19:05' } },
    { id: 'm-135', src: 'bus', t: '17:39', kind: 'task', type: 'task', who: 'orch→p4', lane: 'p4', ack: 2, text: 'T P4 R1 brief-r1.md · connect, read, todo', short: { type: 'task', text: 'P4 R1 wQ:p4' } },
    { id: 'm-134', src: 'bus', t: '17:38', kind: 'ring', type: 'ring', who: 'orch→p4', lane: 'p4', ack: 2, text: 'typed: read your brief and start', short: { type: 'ring', text: 'typed: read brief' } },
    { id: 'r-81', src: 'run', t: '17:35', kind: 'lanes', type: 'lanes', who: 'run', lane: 'p4', ack: 0, text: 'worktrees p4 p5 p6 p7 created from mk-checkout-v2', short: { type: 'lanes', text: 'p4–p7' } },
    { id: 'r-77', src: 'run', t: '16:48', kind: 'review', type: 'review', who: 'run', lane: 'p3', ack: 0, text: 'P3 r1 approved · codex gpt-6.1-mini · 0 findings', short: { type: 'review', text: 'P3 approved' } },
  ],
  lanes: [
    {
      lane: 'p4',
      cells: ['#4fd1c5', '#4fd1c5', '#2c8a8a', '#8bbf9a', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22'],
      age: '1m',
    },
    {
      lane: 'p5',
      cells: ['#e8b04a', '#8bbf9a', '#19e35a', '#e8b04a', '#19e35a', '#4fd1c5', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22'],
      age: '37s',
    },
    {
      lane: 'p6',
      cells: ['#e8b04a', '#8bbf9a', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22'],
      age: '4m',
    },
    {
      lane: 'p7',
      cells: ['#ff7a66', '#5f9a70', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22', '#0f4d22'],
      age: '6m silent',
      silent: true,
    },
  ],
  earlierCount: 24,
}
