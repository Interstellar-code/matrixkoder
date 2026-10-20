// The prompts a Play or Pause press drafts for one phase or task (plan
// phase-task-play-pause, P1.1): pure string work over the row the Plan tab
// already holds. No $, no state, no renderer — the pane hands the text to the
// Improve flow, which shows it for the person to accept.
import type { PlanPhase, PlanTask } from './plan'

/** A dependency counts as met when it is merged or done (the store's finished pair). */
const DEP_MET: Record<string, true> = { merged: true, done: true }

/** Play: work that has not started, and the states that need a retry or a continue. */
const PLAYABLE: Record<string, true> = { planned: true, routed: true, ready: true, blocked: true, stalled: true, quota: true, provider: true, rework: true }

/** Pause: work that is running right now (or waiting on the person). */
const PAUSABLE: Record<string, true> = { dispatched: true, building: true, verifying: true, review: true, fix: true, qa: true }

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

/** The lane and route words the row carries (`L3 · opencode glm-5.3`), '' when none. */
const laneWords = (row: { readonly slot?: number | null; readonly route?: PlanPhase['route'] }): string => {
  const lane = typeof row.slot === 'number' && Number.isFinite(row.slot) ? `L${row.slot}` : ''
  const route = row.route
  const agent = route == null ? '' : [route.agent ?? '', route.model ?? '', route.effort ?? ''].filter(p => p !== '').join(' ')
  return [lane, agent].filter(p => p !== '').join(' · ')
}

/** The line every prompt ends on. */
const LOOP = 'Run it through the MatrixKoder loop (round, brief, lane, dispatch, watch, verify, separate review). Do not edit plan statuses by hand.'

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
  if (phase.rows.length > 0) parts.push(`Tasks in order: ${taskOrder(phase)}.`)
  const owned = phase.owned ?? []
  if (owned.length > 0) parts.push(`Files this phase owns: ${owned.join(', ')}.`)
  const lane = laneWords(phase)
  const route = lane === '' ? 'none planned' : lane
  const reviewer = phase.reviewer != null && phase.reviewer !== '' ? phase.reviewer : 'none'
  parts.push(`Route: ${route}; reviewer: ${reviewer}.`)
  parts.push(LOOP)
  const wave = phase.wave != null ? ` (wave ${String(phase.wave)})` : ''
  return [`Start phase ${phase.id} of plan ${planId}: ${quote(phase.title)}${wave}.`, ...numbered(parts)].join('\n')
}

/** The phase a task id belongs to (`B1.1` → `B1`), or the phase that holds it. */
const phaseOf = (task: PlanTask, phases: readonly PlanPhase[]): PlanPhase | null =>
  phases.find(p => p.id === task.id) ?? phases.find(p => p.rows.some(r => r.id === task.id)) ?? phases.find(p => p.id === task.id.split('.')[0]) ?? null

/** The prompt for Play on a task: the task, why, what done means, its deps, its
 *  route, and the loop. Only fields the view holds. */
export function taskPrompt(planId: string, task: PlanTask, phases: readonly PlanPhase[]): string {
  const phase = phaseOf(task, phases)
  const parts: string[] = []
  if (task.description != null && task.description !== '') parts.push(`Why: ${task.description}`)
  const acceptance = task.acceptance != null && task.acceptance.trim() !== '' ? task.acceptance.trim() : ''
  if (acceptance !== '') parts.push(`Done means (acceptance ${task.id}.a1 onwards): ${acceptance}`)
  const needs = needsFirst(depStatuses(task.deps, phases))
  const phaseNeeds = phase == null ? '' : needsFirst(depStatuses(phase.deps, phases))
  const depParts = [needs !== '' ? `this task needs ${needs}` : '', phaseNeeds !== '' ? `phase ${phase?.id ?? ''} needs ${phaseNeeds}` : ''].filter(p => p !== '')
  if (depParts.length > 0) parts.push(`Depends on: ${depParts.join('; ')}.`)
  const lane = laneWords(task)
  if (lane !== '') parts.push(`Lane: ${lane}.`)
  const reviewer = task.reviewer != null && task.reviewer !== '' ? task.reviewer : ''
  if (reviewer !== '') parts.push(`Reviewer: ${reviewer}.`)
  const owned = task.owned ?? []
  if (owned.length > 0) parts.push(`Files this task owns: ${owned.join(', ')}.`)
  parts.push(`Run this one task, not the rest of ${phase?.id ?? 'the phase'}.`)
  parts.push(LOOP)
  const inPhase = phase != null && phase.id !== task.id ? ` (phase ${phase.id})` : ''
  return [`Start task ${task.id} of plan ${planId}${inPhase}: ${quote(task.title)}.`, ...numbered(parts)].join('\n')
}

/** The prompt for Pause on a phase or task: what to stop, what is left, and the
 *  loop. Pause asks; it stops nothing by itself. */
export function pausePrompt(planId: string, what: 'phase' | 'task', row: PlanPhase | PlanTask, phases: readonly PlanPhase[]): string {
  const status = row.status ?? ''
  const lane = laneWords(row) || 'no lane'
  const parts: string[] = []
  parts.push(`Stop handing out new work in ${row.id}; let nothing new start.`)
  parts.push(`Tell its builder to stop at the next safe point and report what is done.`)
  if (what === 'task') {
    const acceptance = row.acceptance != null && row.acceptance.trim() !== '' ? ` Not yet done: ${row.acceptance.trim()}` : ''
    parts.push(`Record the task's state on the run; do not set a status the store lacks.${acceptance}`)
  } else {
    const tasks = (row as PlanPhase).rows
    const left = tasks.map(t => `${t.id} ${titleOnly(t.title)}`)
    parts.push(left.length > 0
      ? `Record which tasks are still open (${left.join('; ')}); do not set a status the store lacks.`
      : 'Record the phase state on the run; do not set a status the store lacks.')
  }
  parts.push(`Tell me when it is quiet, and what "play" must restore.`)
  parts.push(`Run it through the MatrixKoder loop where work is needed. Do not edit plan statuses by hand; pause only asks, it stops no running builder.`)
  const needLine = needsFirst(depStatuses(row.deps, phases))
  return [
    `Pause ${what} ${row.id} of plan ${planId}: ${quote(row.title)}. Status ${status === '' ? 'unknown' : status}; lane ${lane}.${needLine === '' ? '' : ` Deps: ${needLine}.`}`,
    ...numbered(parts),
  ].join('\n')
}
