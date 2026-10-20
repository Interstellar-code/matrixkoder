// Todo tab wiring in the pane (plan todo-tab-r1, P3): the tab button, the tick read of
// `mkdb.py todo list`, and each press running the matching `mkdb.py todo` argv.
import { test, expect } from 'claude-code/testing'
import { DATA_DIR, mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './testkit'

const HOME = '/tmp/mk-todo-home'
const overviewOf = (slug: string | null) => ({ ok: true, projects: slug == null ? [] : [{ slug, repo: `/x/${slug}`, data_dir: DATA_DIR }], plans: [] })
const reply = (body: unknown, exitCode = 0) => ({
  value: { exitCode, stdout: typeof body === 'string' ? body : JSON.stringify(body), stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
})
type Row = { id: number; text: string; done: boolean; created: string }
const SEED: Row[] = [
  { id: 3, text: 'third open', done: false, created: '2026-10-18 09:00' },
  { id: 2, text: 'second done', done: true, created: '2026-10-17 09:00' },
  { id: 1, text: 'first open', done: false, created: '2026-10-16 09:00' },
]

/** a fake `mkdb.py todo` store: list / add / toggle / delete / restore / clear-done over `rows`. */
async function world(
  on: Parameters<typeof mockWorld>[0],
  o: { missing?: boolean; fail?: string; hold?: boolean; options?: Record<string, unknown> } = {},
) {
  const calls: string[][] = []
  const rows: Row[] = SEED.map(r => ({ ...r }))
  const held: (() => void)[] = []
  const st = { slug: 'matrixkoder' as string | null, hold: o.hold === true }
  const arg = (argv: readonly string[], flag: string): string => argv.find(a => a.startsWith(`${flag}=`))?.slice(flag.length + 1) ?? argv[argv.indexOf(flag) + 1] ?? ''
  const clock = mockWorld(on, {
    env: { MK_HOME: HOME, MATRIXKODER_DATA: DATA_DIR },
    process: async argv => {
      calls.push([...argv])
      if (argv.includes('overview')) return reply(overviewOf(st.slug))
      const at = argv.indexOf('todo')
      if (at < 0) return undefined
      const verb = argv[at + 1]
      if (o.fail === verb) return reply({ ok: false, error: 'boom' }, 2)
      const id = Number(arg(argv, '--id'))
      if (verb === 'list') {
        if (st.hold) await new Promise<void>(res => { held.push(res) })
        return reply({ ok: true, todos: rows.map(r => ({ ...r })), open: rows.filter(r => !r.done).length, done: rows.filter(r => r.done).length })
      }
      if (verb === 'add') {
        const row = { id: 4, text: arg(argv, '--text'), done: false, created: 'now' }
        rows.unshift(row)
        return reply({ ok: true, todo: row })
      }
      if (verb === 'toggle') { const r = rows.find(x => x.id === id); if (r) r.done = !r.done; return reply({ ok: true, todo: r }) }
      if (verb === 'delete') { const i = rows.findIndex(x => x.id === id); const [r] = rows.splice(i, 1); return reply({ ok: true, todo: r }) }
      if (verb === 'restore') {
        rows.push({ id, text: arg(argv, '--text'), done: arg(argv, '--done') === '1', created: arg(argv, '--created') })
        rows.sort((a, b) => b.id - a.id)
        return reply({ ok: true })
      }
      if (verb === 'clear-done') { const n = rows.filter(r => r.done).length; rows.splice(0, rows.length, ...rows.filter(r => !r.done)); return reply({ ok: true, cleared: n }) }
      return undefined
    },
  })
  on('ui.focus', () => ({}))
  on('fs.exists', (_$, e) => ({ value: o.missing === true ? false : String(e.path).startsWith(`${HOME}/`) }))
  mockFs(on, worldFiles())
  const todoCalls = (verb?: string) => calls.filter(a => a.includes('todo') && (verb == null || a[a.indexOf('todo') + 1] === verb))
  const settle = async (n = 20) => { for (let i = 0; i < n; i++) await clock.advance(0) }
  return { calls, rows, held, st, clock, todoCalls, settle }
}

const SAVED = { text: /saved in the overview db/ }

test('P3.1: the tab bar gains Todo (hotkey t) before Improve and Settings; pressing it draws the Todo header', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  const buttons = (await ui.findAll({ type: 'Button' })).filter(b => String(b.props.key).startsWith('dash-tab-'))
  expect(buttons.map(b => b.props.key)).toEqual([
    'dash-tab-overview', 'dash-tab-lanes', 'dash-tab-plan', 'dash-tab-usage', 'dash-tab-files', 'dash-tab-events', 'dash-tab-waiting', 'dash-tab-todo', 'dash-tab-improve', 'dash-tab-settings',
  ])
  expect(buttons.find(b => b.props.key === 'dash-tab-todo')?.props.hotkey).toBe('t')
  await ui.press({ key: 'dash-tab-todo' })
  await w.settle()
  expect(await ui.find(SAVED)).toBeTruthy()
  expect(await ui.find({ text: 'MOCK · fixture data · not this run' }), 'the live tab never draws the fixture').toBeUndefined()
})

test('P3.1: hiddenTabs todo hides it (key or label); defaultTab todo opens on it; Settings stays', { plugins: [worldFixtures], options: { hiddenTabs: 'todo' } }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ type: 'Button', key: 'dash-tab-todo' })).toBeUndefined()
  expect(await ui.find({ type: 'Button', key: 'dash-tab-settings' })).toBeTruthy()
})

test('P3.1: hiddenTabs Todo (label, capitalised) hides it too', { plugins: [worldFixtures], options: { hiddenTabs: 'Todo,settings' } }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find({ type: 'Button', key: 'dash-tab-todo' })).toBeUndefined()
  expect(await ui.find({ type: 'Button', key: 'dash-tab-settings' })).toBeTruthy()
})

test('P3.1: defaultTab todo opens the pane on the Todo tab', { plugins: [worldFixtures], options: { defaultTab: 'todo' } }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find(SAVED)).toBeTruthy()
})

test('P3.1: a hidden defaultTab todo falls back to Overview', { plugins: [worldFixtures], options: { defaultTab: 'todo', hiddenTabs: 'todo' } }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(await ui.find(SAVED)).toBeUndefined()
})

/** the bar's wrapped rows (the engine draws a plain Button `h: label` and joins with one cell) */
function barRows(buttons: readonly { props: Record<string, unknown> }[], cols: number): number[] {
  const rows: number[] = []
  let row = 0
  for (const b of buttons) {
    const w = `${String(b.props.hotkey ?? '')}: ${String(b.props.label ?? '')}`.length + 2 // paddingX 1 + marginRight 1 per chip
    row = row === 0 ? w : row + w > cols ? (rows.push(row), w) : row + w
  }
  if (row > 0) rows.push(row)
  return rows
}

test('P3.1: the ten-button bar wraps cleanly at 46 and 36 columns, every hotkey distinct', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  for (const cols of [46, 36] as const) {
    await ui.redraw(paneProps(cols))
    const bar = (await ui.findAll({ type: 'Button' })).filter(b => String(b.props.key).startsWith('dash-tab-'))
    expect(bar).toHaveLength(10)
    expect(bar.map(b => b.props.hotkey), `hotkeys at ${cols}`).toEqual(['o', 'l', 'p', 'u', 'f', 'e', 'w', 't', 'i', '9'])
    for (const row of barRows(bar, cols)) expect(row, `bar row at ${cols}`).toBeLessThanOrEqual(cols)
  }
})

test('P3.2: the tick lists the project once, one read in flight, and the rows appear', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on, { hold: true })
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  expect(w.todoCalls(), 'no read while another tab is open').toHaveLength(0)
  await ui.press({ key: 'dash-tab-todo' })
  await w.settle()
  expect(w.todoCalls('list')).toHaveLength(1)
  const argv = w.todoCalls('list')[0]!
  expect(argv[1]).toMatch(/scripts\/mkdb\.py$/)
  expect(argv.slice(2)).toEqual(['--home', HOME, 'todo', 'list', '--project', 'matrixkoder'])
  for (let i = 0; i < 3; i++) { await w.clock.advance(5000); await w.settle() }
  expect(w.todoCalls('list'), 'still one: the first read is in flight').toHaveLength(1)
  w.held.shift()?.()
  await w.settle()
  expect(await ui.find({ text: /third open/ })).toBeTruthy()
  expect(await ui.find({ text: /first open/ })).toBeTruthy()
  await w.clock.advance(5000)
  await w.settle()
  expect(w.todoCalls('list'), 'the next tick starts one new read').toHaveLength(2)
  while (w.held.length > 0) w.held.shift()?.()
  await w.settle()
  const before = w.todoCalls().length
  await ui.redraw(paneProps(36)) // a render starts no read
  await w.settle()
  expect(w.todoCalls()).toHaveLength(before)
})

test('P3.2: a missing overview database spawns nothing and draws an empty list, no error', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on, { missing: true })
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-todo' })
  await w.settle()
  await w.clock.advance(5000)
  await w.settle()
  expect(w.calls.filter(a => a.includes('todo') || a.includes('overview'))).toHaveLength(0)
  expect(await ui.find({ text: /nothing here/ })).toBeTruthy()
  expect(await ui.find({ text: /⚠/ })).toBeUndefined()
})

test('P3.2: each press runs the matching mkdb todo argv (text one element) and then reloads', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-todo' })
  await w.settle()
  const head = ['--home', HOME, 'todo']
  const last = (verb: string): string[] => w.todoCalls(verb).at(-1)!.slice(2)
  const lists = () => w.todoCalls('list').length

  // add: the text is typed into the field; the text is ONE argv element, a leading dash and shell syntax included
  const text = '-x; rm -rf "$HOME" && echo \'hi\''
  let n = lists()
  await ui.input({ key: 'todo-q', text }) // typing then Enter submits the field
  await w.settle()
  expect(last('add')).toEqual([...head, 'add', '--project', 'matrixkoder', `--text=${text}`])
  expect(lists(), 'add reloads the list').toBe(n + 1)
  expect(await ui.find({ text: /rm -rf/ })).toBeTruthy()

  n = lists()
  await ui.press({ key: 'todo-t3' })
  await w.settle()
  expect(last('toggle')).toEqual([...head, 'toggle', '--project', 'matrixkoder', '--id', '3'])
  expect(lists()).toBe(n + 1)

  n = lists()
  await ui.press({ key: 'todo-x2' })
  await w.settle()
  expect(last('delete')).toEqual([...head, 'delete', '--project', 'matrixkoder', '--id', '2'])
  expect(lists()).toBe(n + 1)
  expect(await ui.find({ text: /deleted “second done”/ }), 'the undo line shows the removed row').toBeTruthy()

  n = lists()
  await ui.press({ key: 'todo-undo' })
  await w.settle()
  expect(last('restore')).toEqual([...head, 'restore', '--project', 'matrixkoder', '--id', '2', '--text=second done', '--done', '1', '--created=2026-10-17 09:00'])
  expect(lists()).toBe(n + 1)
  expect(await ui.find({ text: /deleted “/ }), 'the undo line is gone after the restore').toBeUndefined()

  n = lists()
  await ui.press({ key: 'todo-clear' })
  await w.settle()
  expect(last('clear-done')).toEqual([...head, 'clear-done', '--project', 'matrixkoder'])
  expect(lists()).toBe(n + 1)
})

test('P3.2: a failing verb shows an error line and keeps the list', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on, { fail: 'toggle' })
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-todo' })
  await w.settle()
  await ui.press({ key: 'todo-t3' })
  await w.settle()
  expect(await ui.find({ text: /⚠.*boom/ })).toBeTruthy()
  expect(await ui.find({ text: /third open/ }), 'the list stands').toBeTruthy()
})

test('P3.2: a focuses the add field, z undoes; both stand down while the field has focus; no duplicate hotkeys', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-todo' })
  await w.settle()
  const hot = async () => (await ui.findAll({ type: 'Button' })).map(b => b.props.hotkey).filter((h): h is string => typeof h === 'string')
  let keys = await hot()
  expect(keys).toContain('a')
  expect(keys).toContain('z')
  expect(new Set(keys).size, `no duplicate hotkey in ${keys.join('')}`).toBe(keys.length)
  await $.ui.focus({ component: 'Pane', requestId: 'matrixkoder', element: 'todo-q', origin: { kind: 'person' } })
  await ui.redraw(paneProps(46))
  keys = await hot()
  expect(keys).not.toContain('a')
  expect(keys).not.toContain('z')
  await ui.press({ key: 'todo-x2' })
  await w.settle()
  await $.ui.focus({ component: 'Pane', requestId: 'matrixkoder', element: 'todo-add', origin: { kind: 'person' } })
  await ui.redraw(paneProps(46))
  expect(await hot()).toContain('a')
  await ui.press({ key: 'todo-key-z' })
  await w.settle()
  expect(w.todoCalls('restore')).toHaveLength(1)
})

test('F1: a slug change clears the old rows, undo and error; a press before the reload sends no old id', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })
  await ui.press({ key: 'dash-tab-todo' })
  await w.settle()
  await ui.press({ key: 'todo-x2' }) // project A: a delete leaves an undo row
  await w.settle()
  expect(await ui.find({ text: /deleted “second done”/ })).toBeTruthy()
  for (const next of ['other', null] as const) {
    w.st.slug = next
    w.st.hold = true // the new project's list has not arrived
    await ui.press({ key: 'dash-tab-overview' })
    await ui.press({ key: 'dash-tab-todo' })
    await w.settle()
    expect(await ui.find({ text: /third open/ }), `rows cleared for ${String(next)}`).toBeUndefined()
    expect(await ui.find({ text: /deleted “/ }), `undo cleared for ${String(next)}`).toBeUndefined()
    expect(await ui.find({ text: /⚠/ })).toBeUndefined()
    const before = w.todoCalls().filter(a => !a.includes('list')).length
    await ui.press({ key: 'todo-key-z' })
    await w.settle()
    expect(w.todoCalls().filter(a => !a.includes('list')), 'no write with an old id').toHaveLength(before)
    while (w.held.length > 0) w.held.shift()?.()
    w.st.hold = false
    await w.settle()
    w.st.slug = 'matrixkoder'
    await ui.press({ key: 'dash-tab-overview' })
    await ui.press({ key: 'dash-tab-todo' })
    await w.settle()
  }
})
