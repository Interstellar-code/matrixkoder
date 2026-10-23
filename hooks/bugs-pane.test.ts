// Bugs tab wiring in the pane (plan bugs-tab, UI.1-UI.4): the tab button, the tick read of
// `mk-bug.py list` (+ `show`) only while the tab is open, and each press running the matching
// `mk-bug.py` argv against a stateful fake of the CLI keyed on argv.
import { test, expect } from 'claude-code/testing'
import type { TestBody } from 'claude-code/testing'
import { DATA_DIR, mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './testkit'

const HOME = '/tmp/mk-bugs-home'
const overview = { ok: true, projects: [{ slug: 'matrixkoder', repo: '/x/matrixkoder', data_dir: DATA_DIR }], plans: [] }
const reply = (body: unknown, exitCode = 0) => ({
  value: { exitCode, stdout: JSON.stringify(body), stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
})
type Task = { id: string; title: string; status: string; acceptance?: string; slot?: number }
type Bug = { id: string; title: string; status: string; task_status: string; severity: string; kind: string; source: string; tasks: Task[] }
const SEED = (): Bug[] => [
  { id: 'B2', title: 'kanban drops a card', status: 'fixing', task_status: 'building', severity: 'high', kind: 'frontend', source: '', tasks: [{ id: 'B2.1', title: 'map the status', status: 'building' }] },
  { id: 'B1', title: 'stale run in the header', status: 'open', task_status: 'planned', severity: 'med', kind: 'store', source: '', tasks: [] },
]

/** a fake `mk-bug.py`: list / show / new / draft / close / task add over `bugs`. */
async function world(on: Parameters<typeof mockWorld>[0], o: { hold?: boolean; fail?: string } = {}) {
  const calls: string[][] = []
  const bugs = SEED()
  const held: (() => void)[] = []
  const st = { hold: o.hold === true, next: 3 }
  const arg = (argv: readonly string[], flag: string): string => argv.find(a => a.startsWith(`${flag}=`))?.slice(flag.length + 1) ?? ''
  const row = (b: Bug) => ({
    id: b.id, title: b.title, status: b.status, task_status: b.task_status, severity: b.severity, kind: b.kind, area: '', found_in: '', labels: [],
    tasks: { done: b.tasks.filter(t => t.status === 'done').length, total: b.tasks.length }, lane: null, updated: '2026-10-07T17:00:00Z', created: '2026-10-07T16:00:00Z',
  })
  const full = (b: Bug) => ({ ...row(b), description: '', source: b.source, steps: '', expected: '', actual: '', links: [], assignee: '', wontfix: false })
  const clock = mockWorld(on, {
    env: { MK_HOME: HOME, MATRIXKODER_DATA: DATA_DIR },
    process: async argv => {
      if (argv.includes('overview')) { calls.push([...argv]); return reply(overview) }
      if (!String(argv[1] ?? '').endsWith('/scripts/mk-bug.py')) return undefined
      calls.push([...argv])
      const verb = argv[6]
      if (o.fail === verb) return reply({ ok: false, error: 'boom' }, 1)
      if (verb === 'list') {
        if (st.hold) await new Promise<void>(res => { held.push(res) })
        const counts = { open: 0, fixing: 0, needs: 0, fixed: 0, wontfix: 0 } as Record<string, number>
        for (const b of bugs) counts[b.status] = (counts[b.status] ?? 0) + 1
        return reply({ ok: true, project: 'matrixkoder', counts, bugs: bugs.map(row) })
      }
      const b = bugs.find(x => x.id === argv[7])
      if (verb === 'show') {
        if (b == null) return reply({ ok: false, error: 'no such bug' }, 1)
        const phase = { id: b.id, title: b.title, status: b.task_status, tasks: b.tasks.map(t => ({ ...t, parent: b.id, type: 'task', subtasks: [] })) }
        return reply({ ok: true, project: 'matrixkoder', bug: full(b), plan: { id: b.id, title: b.title, status: b.task_status, live_run: 'bugs', phases: [phase] }, events: [] })
      }
      if (verb === 'new') {
        const nb: Bug = { id: `B${String(st.next++)}`, title: arg(argv, '--title'), status: 'open', task_status: 'planned', severity: arg(argv, '--severity') || 'med', kind: arg(argv, '--kind') || 'other', source: arg(argv, '--source'), tasks: [] }
        bugs.unshift(nb)
        return reply({ ok: true, bug: full(nb) })
      }
      if (verb === 'draft') return reply({ ok: true, path: `matrixkoder/bugs/${arg(argv, '--title').toLowerCase().replace(/\W+/g, '-')}.md` })
      if (verb === 'close' && b != null) { b.status = 'fixed'; b.task_status = 'done'; return reply({ ok: true, bug: full(b) }) }
      if (verb === 'task' && argv[7] === 'add') {
        const parent = bugs.find(x => x.id === argv[8])
        const t = { id: `${argv[8] ?? ''}.${String((parent?.tasks.length ?? 0) + 1)}`, title: arg(argv, '--title'), status: 'planned' }
        parent?.tasks.push(t)
        return reply({ ok: true, task: t })
      }
      return reply({ ok: false, error: `fake: ${verb ?? ''}` }, 1)
    },
  })
  on('ui.focus', () => ({}))
  on('fs.exists', (_$, e) => ({ value: String(e.path).startsWith(`${HOME}/`) }))
  mockFs(on, worldFiles())
  const bugCalls = (verb?: string) => calls.filter(a => String(a[1] ?? '').endsWith('/scripts/mk-bug.py') && (verb == null || a[6] === verb))
  const settle = async (n = 20) => { for (let i = 0; i < n; i++) await clock.advance(0) }
  return { calls, bugs, held, st, clock, bugCalls, settle }
}

const HEAD = ['--home', HOME, '--project', 'matrixkoder']
type Engine = Parameters<TestBody>[0]
const mount = ($: Engine) =>
  $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })

test('UI.1: the bar draws b Bugs between Inbox and Todo; the list loads only while the tab is open, one read in flight', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on, { hold: true })
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  const bar = (await ui.findAll({ type: 'Button' })).filter(b => String(b.props.key).startsWith('dash-tab-'))
  const keys = bar.map(b => String(b.props.key))
  expect(keys.indexOf('dash-tab-bugs')).toBe(keys.indexOf('dash-tab-inbox') + 1)
  expect(keys.indexOf('dash-tab-todo')).toBe(keys.indexOf('dash-tab-bugs') + 1)
  expect(bar.find(b => b.props.key === 'dash-tab-bugs')?.props.hotkey).toBe('b')
  await w.clock.advance(5000)
  await w.settle()
  expect(w.bugCalls(), 'no read while another tab is open').toHaveLength(0)
  await ui.press({ key: 'dash-tab-bugs' })
  await w.settle()
  expect(w.bugCalls('list')).toHaveLength(1)
  expect(w.bugCalls('list')[0]!.slice(2)).toEqual([...HEAD, 'list'])
  for (let i = 0; i < 3; i++) { await w.clock.advance(5000); await w.settle() }
  expect(w.bugCalls('list'), 'still one: the first read is in flight').toHaveLength(1)
  while (w.held.length > 0) w.held.shift()?.()
  w.st.hold = false
  await w.settle()
  expect(await ui.find({ text: /matrixkoder · bugs run in the project db/ })).toBeTruthy()
  for (const c of ['○ 1 open', '◐ 1 fixing', '⚠ 0 needs', '✓ 0 fixed']) expect(await ui.find({ text: c }), c).toBeTruthy()
  expect(await ui.find({ type: 'Button', key: 'bugs-row-B2' }), 'open-first rows').toBeTruthy()
  await ui.press({ key: 'dash-tab-overview' })
  const n = w.bugCalls().length
  await w.clock.advance(5000)
  await w.settle()
  expect(w.bugCalls(), 'a closed tab reads nothing').toHaveLength(n)
})

test('UI.2: quick add sends one --title= element with the picked kind and severity, then reloads', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-bugs' })
  await w.settle()
  await ui.press({ key: 'bugs-q-kind-cli' })
  await ui.press({ key: 'bugs-q-sev-high' })
  const lists = w.bugCalls('list').length
  const text = '-x; rm -rf "$HOME"'
  await ui.input({ key: 'bugs-q', text })
  await w.settle()
  expect(w.bugCalls('new').at(-1)!.slice(2)).toEqual([...HEAD, 'new', `--title=${text}`, '--kind=cli', '--severity=high'])
  expect(w.bugCalls('list').length, 'the add reloads the list').toBeGreaterThan(lists)
  expect(await ui.find({ type: 'Button', key: 'bugs-row-B3' })).toBeTruthy()
  expect(await ui.find({ text: /\+ B3 filed/ })).toBeTruthy()
})

test('UI.4: a row opens its card, ↵ open reads show and draws the detail, ✓ fixed runs close', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-bugs' })
  await w.settle()
  await ui.press({ key: 'bugs-row-B2' })
  await ui.press({ key: 'bugs-pick-open' })
  await w.settle()
  expect(w.bugCalls('show').at(-1)!.slice(2)).toEqual([...HEAD, 'show', 'B2'])
  expect(await ui.find({ text: 'kanban drops a card' }), 'the detail title').toBeTruthy()
  expect(await ui.find({ type: 'Button', key: 'bug-task-B2.1' }), 'the task tree').toBeTruthy()
  await ui.press({ key: 'bug-act-fixed' })
  await w.settle()
  expect(w.bugCalls('close').at(-1)!.slice(2)).toEqual([...HEAD, 'close', 'B2'])
  expect(await ui.find({ type: 'Button', key: 'bug-act-fixed' }), 'a fixed bug offers no ✓ fixed').toBeUndefined()
  await ui.press({ key: 'bug-back' })
  await w.settle()
  expect(await ui.find({ text: /comes with lane dispatch/ }), 'the placeholder note is gone').toBeUndefined()
})

test('LANE 5: ▶ to lane, hand off, nudge and open pane submit their mk: line through the prompt path; nudge and open pane are off without a lane', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  const submitted: { text: string; asUser?: boolean }[] = []
  on('prompt.submit', (_$, e) => { submitted.push({ text: e.text, asUser: (e.origin as { asUser?: boolean }).asUser }); return { text: e.text } })
  w.bugs[0]!.tasks[0]!.acceptance = 'the card stays on its column'
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-bugs' })
  await w.settle()
  await ui.press({ key: 'bugs-row-B2' }) // the pick loads the bug's tasks at once
  await w.settle()
  expect(w.bugCalls('show').at(-1)!.slice(2)).toEqual([...HEAD, 'show', 'B2'])
  await ui.press({ key: 'bugs-pick-lane' })
  await w.settle()
  expect(submitted).toEqual([{ text: 'mk: dispatch bug B2.1', asUser: true }])
  await ui.press({ key: 'bugs-pick-open' })
  await w.settle()
  await ui.press({ key: 'bug-act-handoff' })
  await w.settle()
  expect(submitted.at(-1)).toEqual({ text: 'mk: hand off bug B2.1', asUser: true })
  expect(await ui.find({ text: /open pane, nudge: no lane/ })).toBeTruthy()
  const n = w.calls.length
  await ui.press({ key: 'bug-act-nudge' })
  await w.settle()
  expect(w.calls.length, 'an off nudge runs nothing').toBe(n)
  expect(submitted).toHaveLength(2)
  w.bugs[0]!.tasks[0]!.slot = 2 // the task gets a lane
  await w.clock.advance(5000)
  await w.settle()
  expect(await ui.find({ text: /no lane/ }), 'the lane turns nudge and open pane on').toBeUndefined()
  await ui.press({ key: 'bug-act-nudge' })
  await ui.press({ key: 'bug-act-open-pane' })
  await w.settle()
  expect(submitted.slice(2)).toEqual([{ text: 'mk: nudge bug B2.1', asUser: true }, { text: 'mk: open pane bug B2.1', asUser: true }])
  expect(w.bugCalls().every(a => a[6] === 'list' || a[6] === 'show'), 'the lane presses run no mk-bug verb').toBe(true)
})

test('UI.3: the kanban sub-tab draws the bug board and a card press opens the detail', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-bugs' })
  await w.settle()
  await ui.press({ key: 'bugs-sub-kanban' })
  await w.settle()
  expect(await ui.find({ text: /OPEN/ })).toBeTruthy()
  expect(await ui.find({ text: /FIXING/ })).toBeTruthy()
  expect(await ui.find({ text: /PLANNED/ }), 'never the plan words').toBeUndefined()
  await ui.press({ key: 'plankanban-card-B1' })
  await w.settle()
  expect(w.bugCalls('show').at(-1)!.slice(2)).toEqual([...HEAD, 'show', 'B1'])
  expect(await ui.find({ text: 'stale run in the header' })).toBeTruthy()
})

test('UI.2: the list sub-tab expands a card to its tasks and + task runs task add', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-bugs' })
  await w.settle()
  await ui.press({ key: 'bugs-sub-list' })
  await ui.press({ key: 'bugs-x-B2' })
  await w.settle()
  expect(w.bugCalls('show').at(-1)!.slice(2)).toEqual([...HEAD, 'show', 'B2'])
  expect(await ui.find({ text: /B2\.1 map the status/ })).toBeTruthy()
  await ui.press({ key: 'bugs-addtask-B2' })
  await ui.input({ key: 'bugs-task-q', text: 'add a test' })
  await w.settle()
  expect(w.bugCalls('task').at(-1)!.slice(2)).toEqual([...HEAD, 'task', 'add', 'B2', '--title=add a test'])
  expect(await ui.find({ text: /B2\.2 add a test/ })).toBeTruthy()
})

test('UI.4: an empty Enter opens the new-bug form; create runs new then task add per task line', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-bugs' })
  await w.settle()
  await ui.input({ key: 'bugs-q', text: '' })
  await w.settle()
  expect(await ui.find({ text: 'New bug' })).toBeTruthy()
  await ui.input({ key: 'bug-new-in-title', text: 'wake misses a lane', kind: 'change' })
  await ui.press({ key: 'bug-new-kind-backend' })
  await ui.input({ key: 'bug-new-in-task', text: 'repro it' })
  await w.settle()
  await ui.input({ key: 'bug-new-in-task', text: 'fix it' })
  await w.settle()
  await ui.press({ key: 'bug-new-create' })
  await w.settle()
  expect(w.bugCalls('new').at(-1)!.slice(2)).toEqual([...HEAD, 'new', '--title=wake misses a lane', '--kind=backend', '--severity=med'])
  expect(w.bugCalls('task').map(a => a.slice(6))).toEqual([['task', 'add', 'B3', '--title=repro it'], ['task', 'add', 'B3', '--title=fix it']])
  expect(await ui.find({ text: 'wake misses a lane' }), 'the new bug opens in the detail').toBeTruthy()
})

test('UI.2: s / k / a stand down while the quick-add field has the focus; a failing verb shows an error line', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on, { fail: 'close' })
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-bugs' })
  await w.settle()
  const hot = async () => (await ui.findAll({ type: 'Button' })).filter(b => String(b.props.key).startsWith('bugs-')).map(b => b.props.hotkey).filter((h): h is string => typeof h === 'string')
  expect(await hot()).toEqual(['s', 'k', 'a'])
  await $.ui.focus({ component: 'Pane', requestId: 'matrixkoder', element: 'bugs-q', origin: { kind: 'person' } })
  await ui.redraw(paneProps(46))
  expect(await hot()).toEqual([])
  await ui.press({ key: 'bugs-row-B1' })
  await ui.press({ key: 'bugs-pick-fixed' })
  await w.settle()
  expect(await ui.find({ text: /⚠ bugs close: boom/ })).toBeTruthy()
  expect(await ui.find({ type: 'Button', key: 'bugs-row-B1' }), 'the list stands').toBeTruthy()
})

test('UI.4: the Events loader reads run bugs too; its rows carry run bugs and the Bug segment shows only them', { plugins: [worldFixtures], timeoutMs: 15000 }, async ($, on) => {
  const runRows = [{ id: 11, run: 'wfv2', t: '2026-10-07T17:50:00+02:00', chan: 'state', kind: 'transition', to_id: 'building', task: 'F6', text: 'plan task moves', meta_json: '' }]
  const bugRows = [{ id: 12, run: 'bugs', t: '2026-10-07T17:51:00+02:00', chan: 'state', kind: 'transition', to_id: 'planned', task: 'B1', text: 'task added: pane crashes', meta_json: '' }]
  mockFs(on, worldFiles())
  const calls: string[][] = []
  const clock = mockWorld(on, {
    process: argv => {
      if (!(argv.some(a => a.endsWith('mkdb.py')) && argv.includes('events'))) return undefined
      calls.push([...argv])
      const rows = argv[argv.indexOf('--run') + 1] === 'bugs' ? bugRows : argv.includes('--run') ? runRows : []
      return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, events: rows }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    },
  })
  let stored: Record<string, string> = {}
  on('state.set', (_$, e, next) => {
    if (e.key === 'viewState' && e.value) stored = e.value as Record<string, string>
    return next(e)
  })
  await $.session.start(sessionStart())
  const ui = await $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(80) })
  await ui.press({ key: 'dash-tab-events' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  await clock.advance(5000)
  for (let i = 0; i < 10; i++) await clock.advance(0)
  expect(calls.some(a => a[a.indexOf('--run') + 1] === 'bugs'), 'the bugs run is read').toBe(true)
  const bugItems = JSON.parse(stored['events:bugs'] ?? '[]') as { id: string; run?: string }[]
  expect(bugItems.map(e => [e.id, e.run])).toEqual([['r-12', 'bugs']])
  expect(bugItems.map(e => (e as { src?: string }).src), 'a bug row carries its own source, not bus / run').toEqual(['bug'])
  const runItems = (JSON.parse(stored['events:list'] ?? '{}') as { events?: { id: string; run?: string }[] }).events ?? []
  expect(runItems.map(e => [e.id, e.run])).toEqual([['r-11', 'wfv2']])
  await ui.redraw(paneProps(80))
  await ui.press({ key: 'ev-src-bug' })
  expect(await ui.find({ text: /task added: pane crashes/ })).toBeTruthy()
  expect(await ui.find({ text: /plan task moves/ }), 'a plan run row is not a bug row').toBeUndefined()
  await ui.press({ key: 'ev-src-all' })
  expect(await ui.find({ text: /task added: pane crashes/ }), 'All still shows it').toBeTruthy()
  for (const k of ['bus', 'run']) {
    await ui.press({ key: `ev-src-${k}` })
    expect(await ui.find({ text: /task added: pane crashes/ }), `not under ${k}`).toBeUndefined()
  }
})

test('DRAFT: save draft runs mk-bug draft (one argv element per flag, tasks one per line), notes the path and closes the form', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-bugs' })
  await w.settle()
  await ui.input({ key: 'bugs-q', text: '' })
  await w.settle()
  await ui.input({ key: 'bug-new-in-title', text: 'wake misses a lane', kind: 'change' })
  await ui.press({ key: 'bug-new-kind-backend' })
  await ui.input({ key: 'bug-new-in-task', text: 'repro it' })
  await w.settle()
  await ui.input({ key: 'bug-new-in-task', text: 'fix it' })
  await w.settle()
  await ui.press({ key: 'bug-new-target-draft' })
  await w.settle()
  await ui.press({ key: 'bug-new-save-draft' })
  await w.settle()
  expect(w.bugCalls('draft').at(-1)!.slice(2)).toEqual([...HEAD, 'draft', '--title=wake misses a lane', '--kind=backend', '--severity=med', '--tasks=repro it\nfix it'])
  expect(w.bugCalls('new'), 'a draft files no bug').toHaveLength(0)
  expect(await ui.find({ text: /draft saved: matrixkoder\/bugs\/wake-misses-a-lane\.md/ })).toBeTruthy()
  expect(await ui.find({ text: 'New bug' }), 'the form closed').toBeUndefined()
})

test('DRAFT: a failing draft shows an error line and keeps the form and its text', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on, { fail: 'draft' })
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-bugs' })
  await w.settle()
  await ui.input({ key: 'bugs-q', text: '' })
  await w.settle()
  await ui.input({ key: 'bug-new-in-title', text: 'wake misses a lane', kind: 'change' })
  await ui.press({ key: 'bug-new-target-draft' })
  await w.settle()
  await ui.press({ key: 'bug-new-save-draft' })
  await w.settle()
  expect(await ui.find({ text: /⚠ bugs draft: boom/ })).toBeTruthy()
  expect(await ui.find({ text: 'New bug' }), 'the form stays').toBeTruthy()
})

test('UI.4: opening and closing the form drops a stale field focus, so s / k / a work again', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-bugs' })
  await w.settle()
  const hot = async () => (await ui.findAll({ type: 'Button' })).filter(b => String(b.props.key).startsWith('bugs-')).map(b => b.props.hotkey).filter((h): h is string => typeof h === 'string')
  await $.ui.focus({ component: 'Pane', requestId: 'matrixkoder', element: 'bugs-q', origin: { kind: 'person' } })
  await ui.redraw(paneProps(46))
  expect(await hot()).toEqual([])
  await ui.press({ key: 'bugs-new' })
  await w.settle()
  expect(await ui.find({ text: 'New bug' })).toBeTruthy()
  await ui.press({ key: 'bug-new-back' })
  await w.settle()
  await ui.redraw(paneProps(46))
  expect(await hot(), 'the focus the form left behind is cleared').toEqual(['s', 'k', 'a'])
})
