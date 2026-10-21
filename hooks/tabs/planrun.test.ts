// Real-code tests for the Play/Pause prompt drafters (plan phase-task-play-pause,
// P1.1): the status table for canPlay / canPause, and the exact text of every
// prompt, pinned for rows of the captured `mkdb plans <id> --json` reply.
import { test, expect } from 'claude-code/testing'
import { canPause, canPlay, depsMet } from './planrun'
import { improveItemPrompt, pausePrompt, phasePrompt, taskPrompt } from './planrun'
import { PLAN_PROMPT_HEADING } from '../improve-core'
import { planFromReply } from './plans-map'
import detailFixture from './fixtures/plan-detail'

const view = planFromReply(detailFixture)
const phases = view.phases ?? []
const phaseOf = (id: string) => {
  const phase = phases.find(p => p.id === id)
  if (phase == null) throw new Error(`fixture has no phase ${id}`)
  return phase
}
const taskOf = (id: string) => {
  for (const phase of phases) {
    const task = phase.rows.find(r => r.id === id)
    if (task != null) return task
  }
  throw new Error(`fixture has no task ${id}`)
}

/** Every status the store can hold (mk-state.py STATUSES) plus the plan view's `ready`. */
const STATUSES = ['planned', 'routed', 'ready', 'dispatched', 'building', 'verifying', 'review', 'fix', 'rework', 'merged', 'qa', 'done', 'blocked', 'quota', 'stalled', 'provider']
/** Play offered, Pause offered, per status (with deps met). */
const TABLE: Record<string, readonly [boolean, boolean]> = {
  planned: [true, false], routed: [true, false], ready: [true, false],
  dispatched: [false, true], building: [false, true], verifying: [false, true],
  review: [false, true], fix: [false, true], qa: [false, true],
  rework: [true, false], blocked: [true, false], quota: [true, false],
  stalled: [true, false], provider: [true, false],
  merged: [false, false], done: [false, false],
}

test('canPlay / canPause: one row per status, deps met', () => {
  expect(Object.keys(TABLE).sort()).toEqual([...STATUSES].sort())
  for (const status of STATUSES) {
    const [play, pause] = TABLE[status] as readonly [boolean, boolean]
    expect(canPlay(status, true), `canPlay ${status}`).toBe(play)
    expect(canPause(status, true), `canPause ${status}`).toBe(pause)
  }
})

test('a status the store does not have offers neither button', () => {
  for (const status of ['', 'nonsense', 'archived', 'parked', 'held', 'on_hold']) {
    expect(canPlay(status, true), `canPlay ${status}`).toBe(false)
    expect(canPause(status, true), `canPause ${status}`).toBe(false)
  }
})

test('an unmet dependency hides both buttons', () => {
  expect(canPlay('planned', false)).toBe(false)
  expect(canPlay('routed', false)).toBe(false)
  expect(canPause('building', false)).toBe(false)
})

test('depsMet reads every dep in the loaded phases; no deps is met', () => {
  expect(depsMet([], phases)).toBe(true)
  expect(depsMet(undefined, phases)).toBe(true)
  expect(depsMet(['V1'], phases)).toBe(true) // V1 is merged
  expect(depsMet(['D1.5'], phases)).toBe(false) // review is not finished
  expect(depsMet(['R1', 'D1.5'], phases)).toBe(false)
  expect(depsMet(['nope'], phases)).toBe(false)
})

test('phasePrompt: the S1 phase of the captured reply, pinned', () => {
  expect(phasePrompt('settings-plans-db', phaseOf('S1'), phases)).toBe(
    'Sent from the MK dashboard (button press)\nStart phase S1 of plan settings-plans-db: "Settings mapping" (wave 4).\n'
    + '1. Needs first: V1 merged. If any is not merged or done, stop and tell me.\n'
    + '2. Tasks in order: S1.1 Scope-home map; S1.2 Dead keys and fixture drift.\n'
    + '3. Files this phase owns: scripts/mk-config.py, scripts/settings-drift.py, hooks/tabs/settings.tsx, hooks/tabs/settings.schema.ts, hooks/tabs/settings.fixture.ts, hooks/tabs/settings.test.ts, .matrixkoder/matrixkoder.yaml.',
  )
})

test('phasePrompt: a phase under way carries its goal and what is running', () => {
  const routed = { ...phaseOf('D1.5'), description: 'The store mirror keeps every field.', reviewer: 'sonnet' }
  const text = phasePrompt('settings-plans-db', routed, phases)
  expect(text).toContain('Goal: The store mirror keeps every field.')
  expect(text).toContain('Continue phase D1.5')
  expect(text).toContain('Running now, do not start twice: D1.5.1')
  expect(text).not.toContain('Route:')
})

test('taskPrompt: S1.1 of the captured reply, pinned', () => {
  expect(taskPrompt('settings-plans-db', taskOf('S1.1'), phases)).toBe(
    'Sent from the MK dashboard (button press)\nStart task S1.1 of plan settings-plans-db (phase S1): "Scope-home map".\n'
    + '1. Done means (acceptance S1.1.a1 onwards): C6 holds. The drift test passes. No storage moves: every reader keeps reading where it reads today.\n'
    + '2. Depends on: phase S1 needs V1 merged.\n'
    + '3. Files this task owns: scripts/mk-config.py, scripts/settings-drift.py, hooks/tabs/settings.schema.ts.\n'
    + '4. Run this one task, not the rest of S1.',
  )
})

test('taskPrompt: a routed task carries its deps and no lane or reviewer line', () => {
  const row = { ...taskOf('S1.2'), deps: [], slot: 3, route: { agent: 'opencode', model: 'manifest/auto', effort: 'high' }, reviewer: 'opus' }
  const text = taskPrompt('settings-plans-db', row, phases)
  expect(text).toContain('2. Depends on: phase S1 needs V1 merged.')
  expect(text).not.toContain('Lane:')
  expect(text).not.toContain('Reviewer:')
})

test('pausePrompt: the D1.5 phase of the captured reply, pinned', () => {
  expect(pausePrompt('settings-plans-db', 'phase', phaseOf('D1.5'), phases)).toBe(
    'Sent from the MK dashboard (button press)\nPause phase D1.5 of plan settings-plans-db: "Task fields survive the store mirror". Status review.\n'
    + '1. Stop handing out new work in D1.5; let nothing new start.\n'
    + '2. Tell its builder to stop at the next safe point and report what is done.\n'
    + '3. Set each task that is still open to paused and note what it was doing (D1.5.1 run init --plan copies acceptance, refs, owned, deps, type, route into run.json (review); D1.5.2 store mirror never blanks acceptance/owned/deps/route_json/type the file view lacks (review); D1.5.3 dispatch records agent, model, effort on the task (review); D1.5.4 mk-state task check (review); D1.5.5 backfill settings-plans-db-r1 task fields from the plan (review); D1.5.6 SKILL.md (review)).\n'
    + '4. Tell me when it is quiet. Play on a paused item puts it back to what it was doing.\n'
    + '5. Pause only asks; it stops no running builder.',
  )
})

test('pausePrompt: a task sets itself paused and names no lane', () => {
  const text = pausePrompt('settings-plans-db', 'task', taskOf('D1.5.1'), phases)
  expect(text.startsWith('Sent from the MK dashboard (button press)\nPause task D1.5.1 of plan settings-plans-db: "run init --plan copies acceptance, refs, owned, deps, type, route into run.json". Status review.')).toBe(true)
  expect(text).not.toContain('lane')
  expect(text).toContain('(mk-state task set D1.5.1 status=paused)')
  expect(text).toContain('5. Pause only asks; it stops no running builder.')
})

test('every prompt opens with the dashboard heading and names no loop, lane or reviewer', () => {
  for (const text of [
    phasePrompt('settings-plans-db', phaseOf('S1'), phases),
    taskPrompt('settings-plans-db', taskOf('S1.1'), phases),
    pausePrompt('settings-plans-db', 'phase', phaseOf('D1.5'), phases),
    pausePrompt('settings-plans-db', 'task', taskOf('D1.5.1'), phases),
  ]) {
    expect(text.startsWith('Sent from the MK dashboard (button press)\n')).toBe(true)
    expect(text).not.toContain('MatrixKoder loop')
    expect(text).not.toContain('Do not edit plan statuses by hand')
  }
})

// ---------- WIRE.3: the Improve prompt a draft row drafts ----------

test('WIRE.3.a2: the Improve prompt names the row data and mk-plan.py item set as the way to write it', () => {
  const phase = phaseOf('R1')
  const text = improveItemPrompt('settings-plans-db', 'phase', phase, phases)
  expect(text.startsWith(PLAN_PROMPT_HEADING), 'the Plan prompt heading').toBe(true)
  expect(text).toContain('Improve phase R1 of plan settings-plans-db (draft)')
  expect(text).toContain('"Self-tests never write a real DB"') // the title, cut at its first colon
  expect(text).toContain('What it says now: Every self-test now runs in a throwaway home folder.')
  expect(text).toContain('Files it owns now: scripts/*.py, scripts/selftest.sh, child-skills/mk-builder/scripts/.')
  expect(text).toContain('mk-plan.py item set settings-plans-db R1 --title "…" --description "…" --acceptance "…" --owned "<paths>" --deps "<ids>"')
  expect(text).toContain('Change only R1.')
})

test('WIRE.3.a2: a task Improve prompt carries its acceptance, owned files, deps and its phase id', () => {
  const task = taskOf('R1.1')
  const text = improveItemPrompt('settings-plans-db', 'task', task, phases)
  expect(text).toContain('Improve task R1.1 of plan settings-plans-db (draft)')
  expect(text).toContain('Done means now: C16 holds.')
  expect(text).toContain('Files it owns now:')
  expect(text).toContain('mk-plan.py item set settings-plans-db R1.1')
  expect(text).toContain('Change only R1.1.')
})

test('WIRE.3.a2: an empty row still names the row and the command, and never asks for a model call of its own', () => {
  const bare = { id: 'R9', title: 'a bare row', status: 'planned', right: '', rows: [] }
  const text = improveItemPrompt('p1', 'phase', bare, [])
  expect(text).toContain('Improve phase R9 of plan p1 (draft): "a bare row".')
  expect(text).not.toContain('What it says now:')
  expect(text).not.toContain('Files it owns now:')
  expect(text).toContain('mk-plan.py item set p1 R9')
})
