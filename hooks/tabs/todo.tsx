// The /mk pane's Todo tab (plan todo-tab-r1, P2, contracts T1/T2): a personal
// to-do list drawn from Todo.dc.html — header counts, the add row, filter chips,
// the rows card, the undo line, the count line and a key hint. A pure render
// (C10): no $, no state reads; a press only calls the `ui` callbacks the pane
// hands in (`todoAct` for the list, `set` for the filter and the draft).
import type { Elements, RenderElement } from 'claude-code'
import { ActionButton, Card, Segment } from './kit'
import { PALETTE, ROLE } from './palette'
import { TODO_FIXTURE } from './todo.fixture'

type Els = Elements[keyof Elements]

export type TodoItem = { id: number; text: string; done: boolean; created: string }
export type TodoFilter = 'all' | 'open' | 'done'

/** T1: everything the tab draws. Newest first (the pane sorts); counts are derived here. */
export type TodoView = {
  mock: boolean
  project: string
  todos: readonly TodoItem[]
  filter: TodoFilter
  draft: string
  undo: TodoItem | null
}

/** T2: the callbacks the pane gives the tab. */
export type TodoUi = {
  get(key: string): string | undefined
  set(key: string, value: string): void
  todoAct?(kind: 'add' | 'toggle' | 'delete' | 'undo' | 'clear-done', arg?: string | number): void
}

/** the fixture view (the design's five sample todos). */
export function todoView(): TodoView {
  return { ...TODO_FIXTURE }
}

// drop control characters (a todo is user text headed for the terminal), then `…`-cut to `w` cells
const clean = (s: string): string => s.replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')
const clip = (s: string, w: number): string => (s.length <= w ? s : `${s.slice(0, Math.max(1, w) - 1)}…`)

const FILTERS: readonly { key: TodoFilter; label: string }[] = [
  { key: 'all', label: 'All' }, { key: 'open', label: 'Open' }, { key: 'done', label: 'Done' },
]

/** Pure render: elements only, no $, no state. */
export function TodoTab(els: Els, view: TodoView, cols: number, ui: TodoUi): RenderElement {
  const { Box, Text } = els
  const W = Math.max(1, cols - 4) // the Card's content width
  const act = (kind: Parameters<NonNullable<TodoUi['todoAct']>>[0], arg?: string | number): void => ui.todoAct?.(kind, arg)
  const nDone = view.todos.filter(t => t.done).length
  const nOpen = view.todos.length - nDone
  const counts: Record<TodoFilter, number> = { all: view.todos.length, open: nOpen, done: nDone }
  const shown = view.todos.filter(t => view.filter === 'all' || (view.filter === 'done') === t.done)
  const emptyText = view.todos.length === 0 ? 'nothing here. type above, Enter.' : view.filter === 'done' ? 'nothing done yet' : 'all clear'
  const addDraft = (text: string): void => { if (text.trim() !== '') act('add', text) }

  const draft = clean(view.draft)
  let field: RenderElement
  if ('Input' in els) {
    const { Input } = els as unknown as { Input: (props: Record<string, unknown>) => RenderElement }
    field = Input({
      key: 'todo-q',
      placeholder: '+ type a todo, Enter',
      value: view.draft,
      onInput: (v: string) => ui.set('todo:draft', v),
      onSubmit: addDraft,
    })
  } else {
    field = <Text dimColor={true}>{clip(draft === '' ? 'press a, then type' : draft, Math.max(4, W - 11))}</Text>
  }

  const textW = Math.max(1, W - 10) // `[ ]` + gap + text + gap + `✕ del`
  const undoW = Math.max(1, cols - 2 - 10 - 1 - 10) // padding, `[ ↩ Undo ]`, gap, `deleted “”`
  return (
    <Box flexDirection="column">
      {view.mock && <Text bold={true} color={ROLE.warn.fg}>MOCK · fixture data · not this run</Text>}
      {Card(els, {}, (
        <Box flexDirection="column">
          <Box flexDirection="row" justifyContent="space-between">
            <Text bold={true} color={ROLE.ok.fg}>Todo</Text>
            <Text>{`● ${String(nOpen)} open · ✓ ${String(nDone)} done`}</Text>
          </Box>
          <Text dimColor={true}>{clip(`${clean(view.project)} · saved in the overview db`, W)}</Text>
          <Box flexDirection="row" gap={1}>
            {field}
            {ActionButton(els, { key: 'todo-add', glyph: '+', word: 'Add', tone: 'primary', onPress: () => addDraft(view.draft) })}
          </Box>
          <Box flexDirection="row" gap={1} flexWrap="wrap">
            {FILTERS.map(f => Segment(els, {
              key: `todo-f-${f.key}`, label: `${f.label} ${String(counts[f.key])}`, on: view.filter === f.key,
              onPress: () => ui.set('todo:filter', f.key),
            }))}
          </Box>
        </Box>
      ))}
      {Card(els, {}, (
        <Box flexDirection="column">
          {shown.length === 0 && <Text dimColor={true}>{emptyText}</Text>}
          {shown.map(t => (
            <Box key={`todo-row-${String(t.id)}`} flexDirection="row" gap={1}>
              {ActionButton(els, { key: `todo-t${String(t.id)}`, glyph: t.done ? '[✓]' : '[ ]', word: '', plain: true, tone: t.done ? 'quiet' : 'default', onPress: () => act('toggle', t.id) })}
              <Text dimColor={t.done} strikethrough={t.done}>{clip(clean(t.text), textW)}</Text>
              {ActionButton(els, { key: `todo-x${String(t.id)}`, glyph: '✕', word: 'del', plain: true, tone: 'quiet', onPress: () => act('delete', t.id) })}
            </Box>
          ))}
        </Box>
      ))}
      {view.undo != null && (
        <Box flexDirection="row" gap={1} paddingX={1}>
          <Text color={ROLE.warn.fg}>{`deleted “${clip(clean(view.undo.text), undoW)}”`}</Text>
          {ActionButton(els, { key: 'todo-undo', glyph: '↩', word: 'Undo', onPress: () => act('undo') })}
        </Box>
      )}
      <Box flexDirection="row" gap={1} paddingX={1}>
        <Text dimColor={true}>{`${String(shown.length)} ${shown.length === 1 ? 'todo' : 'todos'}`}</Text>
        {ActionButton(els, { key: 'todo-clear', glyph: '▾', word: 'Clear done', onPress: () => act('clear-done') })}
      </Box>
      <Text dimColor={true} color={PALETTE.dim}>{clip('✓ done · ✕ delete', cols)}</Text>
    </Box>
  )
}
