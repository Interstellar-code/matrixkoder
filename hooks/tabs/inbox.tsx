// The /mk pane's Inbox tab (plan inbox-and-parsing, phase UI, contracts C3/C6):
// plan files waiting to become projects — a header card, filter chips and one
// card per file with its template badge, per-card state and actions. A pure
// render (C10): no $, no state reads; a press only calls the `ui` callbacks the
// pane hands in (`inboxAct` for a file, `set` for the filter). The tab is
// registered in the dashboard by a later phase (WIRE).
import type { Elements, RenderElement } from 'claude-code'
import { ActionButton, Card, Chip, FilterChip, SmoothBar } from './kit'
import type { ActionSpec } from './kit'
import { PALETTE, ROLE } from './palette'
import type { PaletteRole } from './palette'
import { INBOX_FIXTURE } from './inbox.fixture'

type Els = Elements[keyof Elements]

/** how well a plan file follows the template (C2). */
export type InboxFit = 'template' | 'partial' | 'not_a_plan'
/** the filter chips: inbox = all not archived, template, fix = partial or not a plan, archived. */
export type InboxFilter = 'inbox' | 'template' | 'fix' | 'archived'
/** the import steps, drawn `N of 3`. */
export type InboxStep = 1 | 2 | 3

/** A card's UI state, drawn below the card text. */
export type InboxCardState =
  | { kind: 'idle' }
  | { kind: 'importing'; step: InboxStep }
  | { kind: 'done'; project: string; phases: number; tasks: number }
  | { kind: 'failed'; step: InboxStep; reason: string }
  | { kind: 'confirm' }

/** C3: one `mk-inbox.py list --json` entry, plus the card's UI state. */
export type InboxFile = {
  id: string
  path: string
  title: string
  fit: InboxFit
  sections_found: number
  sections_total: number
  missing: readonly string[]
  age_s: number
  archived: boolean
  state: InboxCardState
}

/** C6: everything the tab draws. `counts` is the C3 reply's counts. */
export type InboxView = {
  files: readonly InboxFile[]
  counts: { inbox: number; template: number; fix: number; archived: number }
  filter: InboxFilter
}

/** what a button asks for. `delete` opens the confirm state, `confirm-delete` / `cancel-delete` answer it. */
export type InboxActKind =
  | 'open' | 'import' | 'archive' | 'restore' | 'delete'
  | 'confirm-delete' | 'cancel-delete' | 'open-project' | 'retry' | 'dismiss'

/** C6: the callbacks the pane gives the tab. */
export type InboxUi = {
  set(key: string, value: string): void
  inboxAct?(kind: InboxActKind, id: string): void
}

/** the fixture view (four sample files). */
export function inboxView(): InboxView {
  return { ...INBOX_FIXTURE }
}

// drop control characters (a file name, title or reason is text headed for the terminal)
const clean = (s: string): string => s.replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')
const clip = (s: string, w: number): string => {
  const cells = Array.from(s)
  return cells.length <= w ? s : `${cells.slice(0, Math.max(1, w) - 1).join('')}…`
}
const len = (s: string): number => Array.from(s).length

/** greedy word wrap to `w` cells; a word longer than `w` is `…`-cut. */
function wrap(text: string, w: number): string[] {
  const lines: string[] = []
  let line = ''
  for (const word of clean(text).split(/\s+/).filter(x => x !== '')) {
    const next = line === '' ? word : `${line} ${word}`
    if (len(next) <= w) { line = next; continue }
    if (line !== '') lines.push(line)
    line = len(word) > w ? clip(word, w) : word
  }
  if (line !== '') lines.push(line)
  return lines
}

/** pack `parts` into lines of at most `w` cells, joined by ` · `, never splitting a part. */
function packParts(parts: readonly string[], w: number): string[] {
  const lines: string[] = []
  let line = ''
  for (const part of parts) {
    const next = line === '' ? part : `${line} · ${part}`
    if (len(next) <= w || line === '') { line = next; continue }
    lines.push(line)
    line = part
  }
  if (line !== '') lines.push(clip(line, w))
  return lines.map(l => clip(l, w))
}

/** `90s` → `now`, then `5m`, `2h`, `1d`, `3w`. */
export function inboxAge(ageS: number): string {
  const s = Math.max(0, Math.floor(ageS))
  if (s < 60) return 'now'
  if (s < 3600) return `${String(Math.floor(s / 60))}m`
  if (s < 86400) return `${String(Math.floor(s / 3600))}h`
  if (s < 604800) return `${String(Math.floor(s / 86400))}d`
  return `${String(Math.floor(s / 604800))}w`
}

const FIT: Record<InboxFit, { badge: string; role: PaletteRole }> = {
  template: { badge: '✓ TEMPLATE', role: 'ok' },
  partial: { badge: '◐ PARTIAL', role: 'warn' },
  not_a_plan: { badge: '✕ NOT A PLAN', role: 'err' },
}

/** the third line's left side: what is missing, or how much was found. */
function fitNote(f: InboxFile): string {
  if (f.fit === 'not_a_plan') return 'no phase or task headings'
  if (f.missing.length > 0) return `missing: ${f.missing.map(clean).join(', ')}`
  return `${String(f.sections_found)} of ${String(f.sections_total)} sections found`
}

const STEP_NAME: Record<InboxStep, string> = { 1: 'Parse the template', 2: 'Write the project', 3: 'Write the tasks' }

const FILTERS: readonly { key: InboxFilter; label: string; glyph: string; role: PaletteRole }[] = [
  { key: 'inbox', label: 'Inbox', glyph: '', role: 'accent' },
  { key: 'template', label: 'Template', glyph: '✓', role: 'ok' },
  { key: 'fix', label: 'Needs fixing', glyph: '◐', role: 'warn' },
  { key: 'archived', label: 'Archived', glyph: '', role: 'idle' },
]

const EMPTY: Record<InboxFilter, { title: string; text: string }> = {
  inbox: { title: 'Inbox is empty', text: 'Drop a plan written from the template into inbox/ and it shows up here.' },
  template: { title: 'No clean files', text: 'Nothing here follows the template yet.' },
  fix: { title: 'Nothing to fix', text: 'Every file in the Inbox follows the template.' },
  archived: { title: 'Nothing archived', text: 'Archived plans stay here until you restore or delete them.' },
}

/** which files a filter shows: the Inbox is every file not archived; the others cut that, Archived is the archived. */
export function inboxShown(files: readonly InboxFile[], filter: InboxFilter): InboxFile[] {
  return files.filter(f => {
    if (filter === 'archived') return f.archived
    if (f.archived) return false
    return filter === 'inbox' || (filter === 'template' ? f.fit === 'template' : f.fit !== 'template')
  })
}

/** Pure render: elements only, no $, no state. */
export function InboxTab(els: Els, view: InboxView, cols: number, ui: InboxUi): RenderElement {
  const { Box, Text } = els
  const W = Math.max(1, cols - 4) // the Card's content width
  const act = (kind: InboxActKind, id: string): void => ui.inboxAct?.(kind, id)
  const shown = inboxShown(view.files, view.filter)
  const rule = <Text dimColor={true}>{'┄'.repeat(W)}</Text>
  const lines = (ls: readonly string[], key: string, extra: { color?: string } = {}): RenderElement[] =>
    ls.map((l, i) => <Text key={`${key}-${String(i)}`} dimColor={extra.color == null} color={extra.color}>{l}</Text>)
  const buttons = (specs: readonly ActionSpec[], key: string): RenderElement => (
    <Box key={key} flexDirection="row" gap={1} flexWrap="wrap">{specs.map(s => ActionButton(els, s))}</Box>
  )

  const header = Card(els, {}, (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between">
        <Text bold={true} color={ROLE.ok.fg}>✉ Inbox</Text>
        <Text>{`${String(view.counts.inbox)} waiting · ${String(view.counts.archived)} archived`}</Text>
      </Box>
      {lines(wrap('Plan documents written from the template, waiting to become projects. Folder: inbox/ in the MatrixKoder home.', W), 'inbox-about')}
      <Box flexDirection="row" gap={1} flexWrap="wrap">
        {FILTERS.map(f => FilterChip(els, {
          key: `inbox-chip-${f.key}`, label: `${f.label} ${String(view.counts[f.key])}`, glyph: f.glyph,
          color: ROLE[f.role].fg, tint: ROLE[f.role].tint, on: view.filter === f.key,
          onPress: () => ui.set('inbox:filter', f.key),
        }))}
      </Box>
      {lines(packParts(['✓ follows the template', '◐ partly', '✕ not a plan'], W), 'inbox-legend')}
    </Box>
  ))

  const fileCard = (f: InboxFile): RenderElement => {
    const fit = FIT[f.fit]
    const st = f.state
    const quiet = f.archived
    const role: PaletteRole | undefined = st.kind === 'importing' ? 'live' : quiet ? undefined : f.fit === 'partial' ? 'warn' : f.fit === 'not_a_plan' ? 'err' : undefined
    const badgeW = len(fit.badge) + 2
    const age = inboxAge(f.age_s)
    const name = clean(f.path)

    // the state block below the card text (UI.2): importing, done, failed, confirm
    let block: RenderElement[] = []
    if (st.kind === 'importing') {
      block = [
        <Box key="imp-step" flexDirection="row" justifyContent="space-between">
          <Text color={ROLE.live.fg}>{`▶ ${STEP_NAME[st.step]}`}</Text>
          <Text>{`${String(st.step)} of 3`}</Text>
        </Box>,
        <Box key="imp-bar">{SmoothBar(els, { pct: (st.step / 3) * 100, width: 20, role: 'live' })}</Box>,
        ...lines(wrap('runs in the background · you can leave this tab', W), 'imp-note'),
      ]
    } else if (st.kind === 'done') {
      const head = `✓ Imported as ${clean(st.project)}`
      const counts = `${String(st.phases)} phases · ${String(st.tasks)} tasks`
      block = [
        len(head) + 1 + len(counts) <= W
          ? (
            <Box key="done-head" flexDirection="row" justifyContent="space-between">
              <Text color={ROLE.ok.fg}>{head}</Text>
              <Text>{counts}</Text>
            </Box>
          )
          : (
            <Box key="done-head" flexDirection="column">
              <Text color={ROLE.ok.fg}>{clip(head, W)}</Text>
              <Text>{counts}</Text>
            </Box>
          ),
        ...lines(wrap('Written to the database. The file stays in the Inbox until you archive it.', W), 'done-note'),
        buttons([
          { key: `inbox-openproj-${f.id}`, glyph: '▤', word: 'Open project', tone: 'primary', onPress: () => act('open-project', f.id) },
          { key: `inbox-again-${f.id}`, glyph: '↻', word: 'Import again', onPress: () => act('import', f.id) },
        ], 'done-btns'),
      ]
    } else if (st.kind === 'failed') {
      block = [
        <Text key="fail-head" color={ROLE.err.fg}>{clip(`✕ Import failed at step ${String(st.step)}`, W)}</Text>,
        ...lines(wrap(st.reason, W), 'fail-why'),
        buttons([
          { key: `inbox-retry-${f.id}`, glyph: '↻', word: 'Retry', tone: 'primary', onPress: () => act('retry', f.id) },
          { key: `inbox-dismiss-${f.id}`, glyph: 'Dismiss', word: '', onPress: () => act('dismiss', f.id) },
        ], 'fail-btns'),
      ]
    } else if (st.kind === 'confirm') {
      block = [
        ...lines(wrap(`Delete ${name} from the Inbox? A copy is kept in inbox/.deleted/.`, W), 'conf-text', { color: ROLE.err.fg }),
        buttons([
          { key: `inbox-yes-${f.id}`, glyph: '✕', word: 'Yes, delete', tone: 'danger', onPress: () => act('confirm-delete', f.id) },
          { key: `inbox-keep-${f.id}`, glyph: 'Keep it', word: '', onPress: () => act('cancel-delete', f.id) },
        ], 'conf-btns'),
      ]
    }

    // the action row (3.5): idle, done and failed; not while importing or confirming
    const actions: ActionSpec[] = []
    if (st.kind === 'idle' || st.kind === 'done' || st.kind === 'failed') {
      actions.push({ key: `inbox-open-${f.id}`, glyph: '↗', word: 'Open', onPress: () => act('open', f.id) })
      if (!f.archived && st.kind !== 'done') actions.push({ key: `inbox-import-${f.id}`, glyph: '⇩', word: 'Import', tone: 'primary', onPress: () => act('import', f.id) })
      if (!f.archived) actions.push({ key: `inbox-archive-${f.id}`, glyph: 'Archive', word: '', onPress: () => act('archive', f.id) })
      if (f.archived) actions.push({ key: `inbox-restore-${f.id}`, glyph: '↶', word: 'Restore', onPress: () => act('restore', f.id) })
      actions.push({ key: `inbox-delete-${f.id}`, glyph: 'Delete', word: '', tone: 'quiet', onPress: () => act('delete', f.id) })
    }

    return (
      <Box key={`inbox-card-${f.id}`}>
        {Card(els, quiet && role == null ? { edge: PALETTE.line } : role == null ? { edge: PALETTE.line } : { role }, (
          <Box flexDirection="column">
            <Box flexDirection="row" justifyContent="space-between" gap={1}>
              <Text bold={true} color={quiet ? PALETTE.dim : ROLE.ok.fg}>{clip(name, Math.max(1, W - badgeW - 1))}</Text>
              {Chip(els, { label: fit.badge, role: quiet ? 'idle' : fit.role })}
            </Box>
            <Text dimColor={quiet}>{clip(clean(f.title), W)}</Text>
            <Box flexDirection="row" justifyContent="space-between" gap={1}>
              <Text dimColor={true} color={f.fit === 'template' ? undefined : ROLE[quiet ? 'idle' : fit.role].fg}>{clip(fitNote(f), Math.max(1, W - len(age) - 1))}</Text>
              <Text dimColor={true}>{age}</Text>
            </Box>
            {block.length > 0 && rule}
            {block}
            {actions.length > 0 && buttons(actions, 'actions')}
          </Box>
        ))}
      </Box>
    )
  }

  const empty = EMPTY[view.filter]
  return (
    <Box flexDirection="column">
      {header}
      {shown.length === 0
        ? Card(els, {}, (
          <Box flexDirection="column">
            <Text bold={true}>{clip(empty.title, W)}</Text>
            {lines(wrap(empty.text, W), 'inbox-empty')}
          </Box>
        ))
        : shown.map(fileCard)}
    </Box>
  )
}
