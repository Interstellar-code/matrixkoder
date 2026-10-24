// The /mk pane's Plan tab task detail panel (plan settings-plans-db, V1.5):
// the full content of one task of the shown plan — a kit Card titled
// `<id> · <status>`: the whole title (wrapped, never cut), type, round,
// verdict, lane `L<slot>` + pane + branch, agent / model / effort, updated,
// deps, owned (one per line, capped at 8 with `+n more`), acceptance
// (wrapped) and the subtasks with their status glyphs. A field the row does
// not carry is simply left out — no `null` text. The footer dim line
// `esc close` is pressable (`onClose` clears `plan:task`): the pane cannot
// arm a raw Esc without a kit change, so the press is the close that works.
// Pure render — no $, no state, no scripts (C1).
import type { Elements, RenderElement } from 'claude-code'
import { ActionButton, Card, Chip, IconButton } from './kit'
import { PALETTE, ROLE } from './palette'
import { clip, planStatusGlyph, planStatusRole } from './plan'
import { routeText } from './plans-map'
import { canPause, canPlay } from './planrun'
import type { PlanPhase, PlanSub, PlanTask } from './plan'

type Els = Elements[keyof Elements]

/** The task row `id` names in the view's phases (V1.5 wiring): the first task
 *  row whose id matches, or — when the store rooted a round's lane task at
 *  phase level (D1.5, V1.4, V1.5 today; its routing lives on the phase node)
 *  — that phase as a task row with its children as subtasks. Null when the
 *  id names nothing. */
export function findPlanTask(phases: readonly PlanPhase[] | undefined, id: string): PlanTask | null {
  if (id === '') return null
  for (const phase of phases ?? []) {
    for (const row of phase.rows) {
      if (row.id === id) return row
    }
  }
  for (const phase of phases ?? []) {
    if (phase.id === id) {
      return {
        id: phase.id,
        title: phase.title,
        status: phase.status ?? 'planned',
        right: phase.status ?? '',
        ...(phase.description != null ? { description: phase.description } : {}),
        slot: phase.slot ?? null,
        pane: phase.pane ?? null,
        branch: phase.branch ?? null,
        updated: phase.updated ?? null,
        acceptance: phase.acceptance ?? null,
        owned: phase.owned,
        deps: phase.deps,
        route: phase.route ?? null,
        ...(phase.plannedRoute != null ? { plannedRoute: phase.plannedRoute } : {}),
        ...(phase.reviewer != null ? { reviewer: phase.reviewer } : {}),
        subs: phase.rows,
      }
    }
  }
  return null
}

/** One dim-key row (the panel's `kv` lines). The value wraps, never cuts. */
function fieldRow(els: Els, key: string, value: string): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="row" gap={1}>
      <Text dimColor={true}>{key}</Text>
      <Text wrap="wrap">{value}</Text>
    </Box>
  )
}

/** One subtask line: its status glyph in the role colour, `id title` cut to
 *  the card width (a wrapped title in a row ran past the border), and its
 *  status word dim on the right. `W` is the card's inner width. */
function subRow(els: Els, sub: PlanSub, W: number): RenderElement {
  const { Box, Text } = els
  return (
    <Box key={`plan-task-sub-${sub.id}`} flexDirection="row" gap={1}>
      <Text color={ROLE[planStatusRole(sub.status)].fg}>{planStatusGlyph(sub.status)}</Text>
      <Text wrap="truncate-end">{clip(`${sub.id} ${sub.title}`, Math.max(1, W - sub.status.length - 4))}</Text>
      <Box flexGrow={1} />
      <Text dimColor={true}>{sub.status}</Text>
    </Box>
  )
}

/** The V1.5 task detail panel. Pure render from one mapped task row; every
 *  missing field is left out. `onClose` is the caller's callback for the
 *  pressable `esc close` footer line. */
/** `onAct` drafts the row's Play/Pause prompt (the pane's Improve seed); `running` is the
 *  shown plan's own status, the one condition every Play/Pause button obeys; `depsMet`
 *  is the same dependency result the task row uses (absent: treated as met, today's
 *  behaviour). Pause never consults dependencies. */
export function PlanTaskPanel(els: Els, row: PlanTask, cols: number, onClose?: () => void, onAct?: (kind: string) => void, running = false, depsMet = true): RenderElement {
  const { Box, Text } = els
  const owned = row.owned ?? []
  const shown = owned.slice(0, 8)
  const route = row.route
  return Card(els, { role: 'live' }, (
    <Box key={`plan-task-panel-${row.id}`} flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text bold={true}>{`${row.id} · ${row.status}`}</Text>
        <Box flexDirection="row" gap={1}>
          {running && onAct != null && canPlay(row.status, depsMet) && IconButton(els, { key: `plan-task-play-panel-${row.id}`, icon: 'play', labelled: true, onPress: () => { onAct(`play task ${row.id}`) } })}
          {running && onAct != null && canPause(row.status, true) && IconButton(els, { key: `plan-task-pause-panel-${row.id}`, icon: 'pause', labelled: true, onPress: () => { onAct(`pause task ${row.id}`) } })}
          {IconButton(els, { key: `plan-task-close-top-${row.id}`, icon: 'close', labelled: true, onPress: () => { onClose?.() } })}
        </Box>
      </Box>
      <Text color={PALETTE.mut} wrap="wrap">{row.title}</Text>
      {row.description != null && <Text color={PALETTE.mut} wrap="wrap">{row.description}</Text>}
      {row.last_commit != null && fieldRow(els, 'commit', row.last_commit.slice(0, 7))}
      {row.findings != null && row.findings > 0 && fieldRow(els, 'findings', String(row.findings))}
      {row.type != null && fieldRow(els, 'type', row.type)}
      {row.round != null && fieldRow(els, 'round', String(row.round))}
      {row.verdict != null && fieldRow(els, 'verdict', row.verdict)}
      {row.risk != null && row.risk >= 1 && row.risk <= 5 && (
        <Box flexDirection="row" gap={1}>
          <Text dimColor={true}>risk</Text>
          {Chip(els, { label: `R${row.risk}`, role: row.risk <= 2 ? 'ok' : row.risk >= 4 ? 'warn' : 'idle' })}
        </Box>
      )}
      {row.openReasons != null && row.openReasons.length > 0 && (
        <Box flexDirection="column">
          <Text dimColor={true}>open reasons</Text>
          {row.openReasons.map(reason => (
            <Box key={`plan-task-reason-${row.id}-${reason.kind}-${reason.opened}`} flexDirection="row" gap={1}>
              <Text dimColor={true}>·</Text>
              <Text wrap="wrap">{`${reason.kind} ${reason.points}`}</Text>
            </Box>
          ))}
        </Box>
      )}
      {row.slot != null && fieldRow(els, 'lane', `L${row.slot}`)}
      {row.pane != null && fieldRow(els, 'pane', row.pane)}
      {row.branch != null && fieldRow(els, 'branch', row.branch)}
      {route?.agent != null && fieldRow(els, 'agent', route.agent)}
      {route?.model != null && fieldRow(els, 'model', route.model)}
      {route?.effort != null && fieldRow(els, 'effort', route.effort)}
      {route == null && row.plannedRoute != null && fieldRow(els, 'planned agent', routeText(row.plannedRoute))}
      {row.reviewer != null && fieldRow(els, 'reviewer', row.reviewer)}
      {row.updated != null && fieldRow(els, 'updated', row.updated)}
      {row.deps != null && row.deps.length > 0 && fieldRow(els, 'deps', row.deps.join(' '))}
      {owned.length > 0 && (
        <Box flexDirection="column">
          <Text dimColor={true}>owned</Text>
          {shown.map(item => (
            <Box key={`plan-task-owned-${row.id}-${item}`} flexDirection="row" gap={1}>
              <Text dimColor={true}>·</Text>
              <Text wrap="wrap">{item}</Text>
            </Box>
          ))}
          {owned.length > 8 && <Text dimColor={true}>{`+${owned.length - 8} more`}</Text>}
        </Box>
      )}
      {row.acceptance != null && (
        <Box flexDirection="column">
          <Text dimColor={true}>acceptance</Text>
          <Text wrap="wrap">{row.acceptance}</Text>
        </Box>
      )}
      {row.subs != null && row.subs.length > 0 && (
        <Box flexDirection="column">
          <Text dimColor={true}>subtasks</Text>
          {row.subs.map(sub => subRow(els, sub, Math.max(1, cols - 4)))}
        </Box>
      )}
      {ActionButton(els, {
        key: `plan-task-close-${row.id}`,
        glyph: 'esc close',
        word: '',
        plain: true,
        tone: 'quiet',
        onPress: () => { onClose?.() },
      })}
    </Box>
  ))
}
