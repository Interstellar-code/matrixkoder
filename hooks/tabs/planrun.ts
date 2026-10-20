// The prompts a Play or Pause press drafts for one phase or task (plan
// phase-task-play-pause, P1.1): pure string work over the row the Plan tab
// already holds. No $, no state, no renderer — the pane hands the text to the
// Improve flow, which shows it for the person to accept.
import type { PlanPhase, PlanTask } from './plan'

/** A dependency counts as met when it is merged or done (the store's finished pair). */
const DEP_MET: Record<string, true> = { merged: true, done: true }

/** Play: work that has not started, and the states that need a retry or a continue. */
const PLAYABLE: Record<string, true> = { planned: true, routed: true, ready: true, blocked: true, stalled: true, quota: true, provider: true, rework: true, paused: true }

/** Pause: work that is running right now (or waiting on the person). */
const PAUSABLE: Record<string, true> = { dispatched: true, building: true, verifying: true, review: true, fix: true, qa: true }

/** A task that stopped on its own and needs a retry. */
const STOPPED: Record<string, true> = { blocked: true, stalled: true, quota: true, provider: true }

/** A task or phase the person held back. */
const HELD: Record<string, true> = { paused: true, parked: true, held: true, on_hold: true }

/** Play is offered only while every dependency is merged or done. */
export const canPlay = (status: string, depsMet: boolean): boolean => depsMet && PLAYABLE[status] === true

/** Pause is offered for a running item; a dependency has no bearing on it. */
export const canPause = (status: string, depsMet: boolean): boolean => depsMet && PAUSABLE[status] === true

/** A dependency met so far: its id and status as words. */
export type DepStatus = { readonly id: string; readonly status: string }

/** The dependencies of one row, looked up in the loaded phases (a dep may name a
 *  phase or a task; an id no phase holds counts as not met). `phases` only feeds
 *  this lookup and the prompt's "needs first" line. */
export function depStatuses(ids: readonly string[] | undefined, phases: readonly PlanPhase[]): DepStatus[] {
  const out: DepStatus[] = []
  for (const id of ids ?? []) {
    let status = ''
    for (const phase of phases) {
      if (phase.id === id) { status = phase.status ?? ''; break }
      const task = phase.rows.find(r => r.id === id)
      if (task != null) { status = task.status; break }
    }
    out.push({ id, status })
  }
  return out
}

/** True when every named dependency is merged or done; no deps is met. */
export const depsMet = (ids: readonly string[] | undefined, phases: readonly PlanPhase[]): boolean =>
  depStatuses(ids, phases).every(d => DEP_MET[d.status] === true)

/** A task's Play dependency gate: its own deps plus its phase's deps (a phase id or a
 *  task id both resolve through the loaded phases). Shared by the task row and the task
 *  detail panel so the two can never disagree. */
export const taskDepsMet = (task: PlanTask, phases: readonly PlanPhase[]): boolean => {
  const phase = phases.find(p => p.rows.some(r => r.id === task.id)) ?? phases.find(p => p.id === task.id.split('.')[0])
  return depsMet([...(task.deps ?? []), ...(phase?.deps ?? [])], phases)
}

/** The title only: store titles carry the description after the first `: `, `; ` or ` — `. */
const titleOnly = (text: string): string => {
  const m = /: |; | — /.exec(text)
  return (m == null ? text : text.slice(0, m.index)).trim()
}

const quote = (text: string): string => `"${titleOnly(text)}"`

/** `D1 merged and T1 merged`, or '' with no deps. */
const needsFirst = (deps: readonly DepStatus[]): string =>
  deps.length === 0 ? '' : deps.map(d => `${d.id} ${d.status === '' ? 'not in this plan' : d.status}`).join(' and ')

/** The heading line of every prompt a Play or Pause press drafts. */
const HEADING = 'Sent from the MK dashboard (button press)'

/** A numbered block: the parts present, numbered in order from 1. */
const numbered = (parts: readonly string[]): string[] => parts.map((part, i) => `${String(i + 1)}. ${part}`)

/** The tasks of a phase as the order line draws them: `B1.1 Header; B1.2 Steps`. */
const taskOrder = (phase: PlanPhase): string => phase.rows.map(r => `${r.id} ${titleOnly(r.title)}`).join('; ')

/** The prompt for Play on a phase: what it is, what it needs first, its tasks, its
 *  files, its route, and the loop. Only fields the view holds. */
export function phasePrompt(planId: string, phase: PlanPhase, phases: readonly PlanPhase[]): string {
  const parts: string[] = []
  if (phase.description != null && phase.description !== '') parts.push(`Goal: ${phase.description}`)
  const needs = needsFirst(depStatuses(phase.deps, phases))
  if (needs !== '') parts.push(`Needs first: ${needs}. If any is not merged or done, stop and tell me.`)
  const done = phase.rows.filter(r => DEP_MET[r.status] === true)
  const running = phase.rows.filter(r => PAUSABLE[r.status] === true)
  const todo = phase.rows.filter(r => DEP_MET[r.status] !== true && PAUSABLE[r.status] !== true)
  const list = (rows: readonly PlanTask[]): string => rows.map(r => `${r.id} ${titleOnly(r.title)}${r.status === '' ? '' : ` (${r.status})`}`).join('; ')
  const started = done.length > 0 || running.length > 0
  if (!started && phase.rows.length > 0) parts.push(`Tasks in order: ${taskOrder(phase)}.`)
  if (started) {
    if (done.length > 0) parts.push(`Already done, leave alone: ${list(done)}.`)
    if (running.length > 0) parts.push(`Running now, do not start twice: ${list(running)}.`)
    parts.push(todo.length > 0 ? `Still to do, in order: ${list(todo)}.` : 'Nothing left to start; check the running tasks and close the phase.')
  }
  const owned = phase.owned ?? []
  if (owned.length > 0) parts.push(`Files this phase owns: ${owned.join(', ')}.`)
  const wave = phase.wave != null ? ` (wave ${String(phase.wave)})` : ''
  const verb = HELD[phase.status ?? ''] === true ? 'Resume' : started ? 'Continue' : 'Start'
  return [HEADING, `${verb} phase ${phase.id} of plan ${planId}: ${quote(phase.title)}${wave}.`, ...numbered(parts)].join('\n')
}

/** The phase a task id belongs to (`B1.1` → `B1`), or the phase that holds it. */
const phaseOf = (task: PlanTask, phases: readonly PlanPhase[]): PlanPhase | null =>
  phases.find(p => p.id === task.id) ?? phases.find(p => p.rows.some(r => r.id === task.id)) ?? phases.find(p => p.id === task.id.split('.')[0]) ?? null

/** The prompt for Play on a task: the task, why, what done means, its deps, its
 *  route, and the loop. Only fields the view holds. */
export function taskPrompt(planId: string, task: PlanTask, phases: readonly PlanPhase[]): string {
  const phase = phaseOf(task, phases)
  const parts: string[] = []
  const status = task.status
  const where = [task.round != null ? `round r${String(task.round)}` : '', task.verdict != null && task.verdict !== '' ? `verdict ${task.verdict}` : '', task.findings != null && task.findings > 0 ? `${String(task.findings)} open findings` : '', task.last_commit != null && task.last_commit !== '' ? `last commit ${task.last_commit.slice(0, 7)}` : ''].filter(p => p !== '').join(', ')
  if (status === 'rework') parts.push(`It is in rework${where === '' ? '' : ` (${where})`}: fix the open review findings only; do not redo what passed.`)
  else if (STOPPED[status] === true) parts.push(`It stopped as ${status}${where === '' ? '' : ` (${where})`}: find out why, clear it, and re-dispatch on the same lane.`)
  else if (HELD[status] === true) parts.push(`It was paused${where === '' ? '' : ` (${where})`}: check its lane and branch for what is done, then carry on from there.`)
  if (task.description != null && task.description !== '') parts.push(`Why: ${task.description}`)
  const acceptance = task.acceptance != null && task.acceptance.trim() !== '' ? task.acceptance.trim() : ''
  if (acceptance !== '') parts.push(`Done means (acceptance ${task.id}.a1 onwards): ${acceptance}`)
  const needs = needsFirst(depStatuses(task.deps, phases))
  const phaseNeeds = phase == null ? '' : needsFirst(depStatuses(phase.deps, phases))
  const depParts = [needs !== '' ? `this task needs ${needs}` : '', phaseNeeds !== '' ? `phase ${phase?.id ?? ''} needs ${phaseNeeds}` : ''].filter(p => p !== '')
  if (depParts.length > 0) parts.push(`Depends on: ${depParts.join('; ')}.`)
  const owned = task.owned ?? []
  if (owned.length > 0) parts.push(`Files this task owns: ${owned.join(', ')}.`)
  parts.push(`Run this one task, not the rest of ${phase?.id ?? 'the phase'}.`)
  const verb = status === 'rework' ? 'Rework' : STOPPED[status] === true ? 'Retry' : HELD[status] === true ? 'Resume' : 'Start'
  const inPhase = phase != null && phase.id !== task.id ? ` (phase ${phase.id})` : ''
  return [HEADING, `${verb} task ${task.id} of plan ${planId}${inPhase}: ${quote(task.title)}.`, ...numbered(parts)].join('\n')
}

/** The prompt for Pause on a phase or task: what to stop, what is left, and the
 *  loop. Pause asks; it stops nothing by itself. */
export function pausePrompt(planId: string, what: 'phase' | 'task', row: PlanPhase | PlanTask, phases: readonly PlanPhase[]): string {
  const status = row.status ?? ''
  const parts: string[] = []
  parts.push(`Stop handing out new work in ${row.id}; let nothing new start.`)
  parts.push(`Tell its builder to stop at the next safe point and report what is done.`)
  if (what === 'task') {
    const acceptance = row.acceptance != null && row.acceptance.trim() !== '' ? ` Not yet done: ${row.acceptance.trim()}` : ''
    parts.push(`Set the task to paused (mk-state task set ${row.id} status=paused) and note what it was doing: ${status === '' ? 'unknown' : status}.${acceptance}`)
  } else {
    const tasks = (row as PlanPhase).rows
    const left = tasks.filter(t => DEP_MET[t.status] !== true).map(t => `${t.id} ${titleOnly(t.title)}${t.status === '' ? '' : ` (${t.status})`}`)
    parts.push(left.length > 0
      ? `Set each task that is still open to paused and note what it was doing (${left.join('; ')}).`
      : 'Note the phase state on the run.')
  }
  parts.push(`Tell me when it is quiet. Play on a paused item puts it back to what it was doing.`)
  parts.push(`Pause only asks; it stops no running builder.`)
  const needLine = needsFirst(depStatuses(row.deps, phases))
  return [
    HEADING,
    `Pause ${what} ${row.id} of plan ${planId}: ${quote(row.title)}. Status ${status === '' ? 'unknown' : status}.${needLine === '' ? '' : ` Deps: ${needLine}.`}`,
    ...numbered(parts),
  ].join('\n')
}
