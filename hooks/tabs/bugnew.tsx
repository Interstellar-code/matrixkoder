// The /mk pane's new-bug form (plan bugs-tab, UI.4; Bugs board BugNew at 46
// cols): `← Bugs ▸ New bug` with the next id chip, a card with the Title input,
// the Project (this repo, fixed), the Kind chips (one selected), Found in and
// the Severity chips, a Description card, a TASKS card with a `+ task` input and
// a SAVE TO card (store now: a row in this repo's bugs run and an event on the
// bus; draft: projects/<repo>/bugs/<id>.md) with the create / save-draft button.
// The draft lives in the pane's view state under `bugs:new:*` (title, kind,
// severity, found_in, description, tasks = one task per line, task = the pending
// `+ task` text, target = store|draft); `bugDraftOf` reads it. Pure render — no
// $, no state; inputs are drawn only when the element table has `Input`, and a
// submit only calls `ui.bugAct` (C1). `bugNewArgv` is the `mk-bug new` argv tail.
import type { Elements, RenderElement } from 'claude-code'
import { ActionButton, Card, Chip, Section, Segment } from './kit'
import { PALETTE } from './palette'
import { clip } from './plan'

type Els = Elements[keyof Elements]

export const BUG_KINDS = ['frontend', 'backend', 'cli', 'store', 'infra', 'docs', 'other'] as const
export const BUG_SEVERITIES = ['high', 'med', 'low'] as const
export type BugKind = typeof BUG_KINDS[number]
export type BugSeverity = typeof BUG_SEVERITIES[number]
export type BugTarget = 'store' | 'draft'

/** everything the form draws; the pane builds it with `bugDraftOf` */
export type BugDraft = {
  /** the next bug id, e.g. `B7` */
  readonly nextId: string
  /** this repo's slug (fixed) */
  readonly project: string
  readonly title: string
  readonly kind: BugKind
  readonly severity: BugSeverity
  readonly found_in: string
  readonly description: string
  /** the tasks added so far */
  readonly tasks: readonly string[]
  /** the `+ task` input's pending text */
  readonly task: string
  readonly target: BugTarget
}

/** C12: the tab's view-state handle; `bugAct` is the pane's submit callback */
export type TabUi = {
  get(key: string): string | undefined
  set(key: string, value: string): void
  bugAct?(kind: 'create' | 'save-draft'): void
}

const KEY = 'bugs:new:'

/** The draft out of the pane's view state (`get` reads a key): defaults kind other, severity med, target store. */
export function bugDraftOf(get: (key: string) => string | undefined, nextId: string, project: string): BugDraft {
  const kind = get(`${KEY}kind`) ?? ''
  const severity = get(`${KEY}severity`) ?? ''
  return {
    nextId,
    project,
    title: get(`${KEY}title`) ?? '',
    kind: (BUG_KINDS as readonly string[]).includes(kind) ? kind as BugKind : 'other',
    severity: (BUG_SEVERITIES as readonly string[]).includes(severity) ? severity as BugSeverity : 'med',
    found_in: get(`${KEY}found_in`) ?? '',
    description: get(`${KEY}description`) ?? '',
    tasks: (get(`${KEY}tasks`) ?? '').split('\n').filter(t => t.trim() !== ''),
    task: get(`${KEY}task`) ?? '',
    target: get(`${KEY}target`) === 'draft' ? 'draft' : 'store',
  }
}

/** The `mk-bug new` argv tail: one `--flag=<text>` argument per filled field (user text stays one argument,
 *  stored verbatim); an empty field is left out. The tasks go through `task add`, not here. */
export function bugNewArgv(draft: BugDraft): string[] {
  const out: string[] = []
  const add = (flag: string, value: string): void => { if (value.trim() !== '') out.push(`--${flag}=${value}`) }
  add('title', draft.title)
  add('kind', draft.kind)
  add('severity', draft.severity)
  add('found-in', draft.found_in)
  add('description', draft.description)
  return out
}

// drop control characters (the draft is user text headed for the terminal)
const clean = (s: string): string => s.replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')

/** Pure render: elements only, no $, no state. */
export function BugNewForm(els: Els, draft: BugDraft, cols: number, ui?: TabUi): RenderElement {
  const { Box, Text } = els
  const W = Math.max(1, cols - 4) // the Card's content width
  const set = (key: string, value: string): void => { ui?.set(`${KEY}${key}`, value) }
  // the engine refuses an Input without onSubmit: Enter in title / found in / description keeps the text and files
  // the bug (create or save draft, as the Save to card says) once it has a title
  const submitField = (key: string) => (v: string): void => {
    set(key, v)
    if ((key === 'title' ? v : draft.title).trim() !== '') ui?.bugAct?.(draft.target === 'draft' ? 'save-draft' : 'create')
  }
  const field = (key: string, value: string, placeholder: string, onSubmit: (v: string) => void = submitField(key)): RenderElement => {
    if ('Input' in els) {
      const { Input } = els as unknown as { Input: (props: Record<string, unknown>) => RenderElement }
      return <Box key={`bug-new-${key}`} flexGrow={1}>{Input({ key: `bug-new-in-${key}`, placeholder, value, onInput: (v: string) => { set(key, v) }, onSubmit })}</Box>
    }
    return <Text key={`bug-new-${key}`} dimColor={true}>{clip(value === '' ? placeholder : clean(value), Math.max(4, W - 11))}</Text>
  }
  const row = (label: string, body: RenderElement): RenderElement => (
    <Box key={`bug-new-row-${label}`} flexDirection="row" gap={1}>
      <Text dimColor={true}>{label.padEnd(8)}</Text>
      {body}
    </Box>
  )
  const addTask = (text: string): void => {
    if (text.trim() === '') return
    set('tasks', [...draft.tasks, text].join('\n'))
    set('task', '')
  }
  const target = (to: BugTarget, word: string, note: string): RenderElement => (
    <Box key={`bug-new-target-${to}`} flexDirection="column">
      {ActionButton(els, { key: `bug-new-target-${to}`, glyph: draft.target === to ? '(●)' : '( )', word, plain: true, tone: draft.target === to ? 'primary' : 'default', onPress: () => { set('target', to) } })}
      <Text dimColor={true} wrap="wrap">{`    ${note}`}</Text>
    </Box>
  )
  const canSave = draft.title.trim() !== ''
  const store = draft.target === 'store'

  return (
    <Box flexDirection="column" gap={1}>
      <Box flexDirection="row" gap={1}>
        {ActionButton(els, { key: 'bug-new-back', glyph: '←', word: 'Bugs', plain: true, tone: 'quiet', onPress: () => { ui?.set('bugs:sel', '') } })}
        <Text dimColor={true}>▸</Text>
        <Text bold={true}>New bug</Text>
        <Box flexGrow={1} />
        {Chip(els, { label: draft.nextId, role: 'live' })}
      </Box>

      {Card(els, {}, (
        <Box flexDirection="column">
          {row('title', field('title', draft.title, 'what is wrong'))}
          {row('project', <Text>{clean(draft.project)}</Text>)}
          <Box flexDirection="row" gap={1}>
            <Text dimColor={true}>{'kind'.padEnd(8)}</Text>
            <Box flexDirection="row" gap={1} flexWrap="wrap">
              {BUG_KINDS.map(k => Segment(els, { key: `bug-new-kind-${k}`, label: k, on: draft.kind === k, onPress: () => { set('kind', k) } }))}
            </Box>
          </Box>
          {row('found in', field('found_in', draft.found_in, 'version, commit or branch'))}
          <Box flexDirection="row" gap={1}>
            <Text dimColor={true}>{'severity'.padEnd(8)}</Text>
            {BUG_SEVERITIES.map(s => Segment(els, { key: `bug-new-sev-${s}`, label: s, on: draft.severity === s, onPress: () => { set('severity', s) } }))}
          </Box>
        </Box>
      ))}

      {Card(els, {}, (
        <Box flexDirection="column">
          {Section(els, { label: 'Description' })}
          {field('description', draft.description, 'what you saw, how to repeat it')}
        </Box>
      ))}

      {Card(els, {}, (
        <Box flexDirection="column">
          {Section(els, { label: 'Tasks', right: String(draft.tasks.length) })}
          {draft.tasks.length === 0 && <Text dimColor={true}>no tasks yet</Text>}
          {draft.tasks.map((t, i) => (
            <Box key={`bug-new-task-${String(i)}`} flexDirection="row" gap={1}>
              <Text dimColor={true}>{`${String(i + 1)}.`}</Text>
              <Text wrap="truncate-end">{clip(clean(t), Math.max(1, W - 12))}</Text>
              <Box flexGrow={1} />
              {ActionButton(els, { key: `bug-new-task-x${String(i)}`, glyph: '✕', word: 'del', plain: true, tone: 'quiet', onPress: () => { set('tasks', draft.tasks.filter((_, j) => j !== i).join('\n')) } })}
            </Box>
          ))}
          <Box flexDirection="row" gap={1}>
            <Text color={PALETTE.dim}>+ task</Text>
            {field('task', draft.task, 'type a task, Enter', addTask)}
          </Box>
        </Box>
      ))}

      {Card(els, {}, (
        <Box flexDirection="column">
          {Section(els, { label: 'Save to' })}
          {target('store', 'store now', `a row in ${clean(draft.project)}'s bugs run and an event on the bus`)}
          {target('draft', 'draft', `projects/${clean(draft.project)}/bugs/${draft.nextId}.md`)}
        </Box>
      ))}

      <Box flexDirection="row" gap={1}>
        {ActionButton(els, {
          key: store ? 'bug-new-create' : 'bug-new-save-draft',
          glyph: '+',
          word: store ? `Create ${draft.nextId}` : 'Save draft',
          tone: canSave ? 'primary' : 'disabled',
          primary: true,
          noHotkey: true,
          onPress: () => { if (canSave) ui?.bugAct?.(store ? 'create' : 'save-draft') },
        })}
        {ActionButton(els, { key: 'bug-new-esc', glyph: 'esc cancel', word: '', plain: true, tone: 'quiet', onPress: () => { ui?.set('bugs:sel', '') } })}
      </Box>
    </Box>
  )
}
