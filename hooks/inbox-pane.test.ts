// Inbox tab wiring in the pane (plan inbox-and-parsing, phase WIRE, task WIRE.1): the tab row
// and hotkey n, the mk-inbox.py list loader (once on open, then on the tick, one read in
// flight), each card press running the matching mk-inbox.py argv, and the failed card state.
// The runner is injected the way the Todo tests do it: the plugin test runtime spawns nothing.
import { test, expect } from 'claude-code/testing'
import type { TestBody } from 'claude-code/testing'
import { DATA_DIR, mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './testkit'

const HOME = '/tmp/mk-inbox-home'
/** the REAL `mk-inbox.py list --json` reply, captured in a temp home holding
 *  scripts/fixtures/inbox-plan-template.md and scripts/fixtures/inbox-plan-not-a-plan.md (the
 *  two fixture copies; the `warnings` entry is this capture's own and is dropped below). */
const LIST_REPLY = {
  ok: true,
  files: [
    {
      id: 'mk-ponytail-hooks',
      path: 'notes-2026-10-20.md',
      title: 'Goal',
      fit: 'not_a_plan',
      sections_found: 0,
      sections_total: 7,
      missing: ['Summary', 'Goal', 'Design / context', 'Phases', 'Risks', 'Open questions', 'Log', 'header phases', 'tasks', 'acceptance'],
      age_s: 0,
      archived: false,
      note: 'no phase or task headings',
    },
    {
      id: 'plan-inbox-and-parsing',
      path: 'plan-inbox-and-parsing.md',
      title: 'one clean path from a plan document to the project store, with an Inbox tab',
      fit: 'template',
      sections_found: 7,
      sections_total: 7,
      missing: [],
      age_s: 0,
      archived: false,
      note: '',
    },
  ],
  counts: { inbox: 2, template: 1, fix: 1, archived: 0 },
}

/** what the list answers once the template file is imported (it leaves the Inbox) */
const LIST_AFTER_IMPORT = {
  ok: true,
  files: [LIST_REPLY.files[0]],
  counts: { inbox: 1, template: 0, fix: 1, archived: 0 },
}

const ok = (body: unknown) => ({
  value: { exitCode: 0, stdout: JSON.stringify(body), stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
})
const bad = (body: unknown) => ({
  value: { exitCode: 2, stdout: JSON.stringify(body), stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
})

type Opts = {
  /** the list answers this once, then the default */
  firstList?: unknown
  /** import answers a failure: the reply names step and reason */
  importFail?: { step: number; reason: string }
  /** hold the list open until released (one read in flight) */
  holdList?: boolean
  /** the list answers this every time */
  listReply?: unknown
  /** the host opener's argv, recorded */
  openers?: string[][]
  /** what every host opener exits with (default: `open` 0, `xdg-open` 1, so the order is proven) */
  openerExit?: number
}

/** A fake mk-inbox.py over one MK home: list, import (three progress lines), archive, restore,
 *  delete. Every argv is recorded; only the process runner and the clock are faked. */
async function world(on: Parameters<typeof mockWorld>[0], o: Opts = {}) {
  const calls: string[][] = []
  const optsOpeners = o.openers
  const held: (() => void)[] = []
  const st = { lists: 0, hold: o.holdList === true }
  const clock = mockWorld(on, {
    env: { MK_HOME: HOME, MATRIXKODER_DATA: DATA_DIR },
    process: async argv => {
      calls.push([...argv])
      if (argv.includes('overview')) {
        return ok({ ok: true, projects: [{ slug: 'matrixkoder', repo: '/x/matrixkoder', data_dir: DATA_DIR }], plans: [] })
      }
      // the import's own count read (the Plan tab's reader)
      // the real reply's shape: phases with their task rows, and a per-STATUS counts tally
      if (argv.includes('plans')) return ok({ ok: true, id: 'plan-inbox-and-parsing', phases: [{ id: 'WIRE', tasks: [{ id: 'WIRE.1' }] }, { id: 'UI', tasks: [{ id: 'UI.1' }, { id: 'UI.2' }] }], counts: {} })
      if (argv[0] === 'open' || argv[0] === 'xdg-open') {
        optsOpeners?.push([...argv])
        return { value: { exitCode: o.openerExit ?? (argv[0] === 'open' ? 0 : 1), stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      }
      const at = argv.findIndex(a => a.endsWith('/scripts/mk-inbox.py') || a.endsWith('/mk-inbox.py'))
      if (at < 0) return undefined
      const verb = argv[at + 1]
      if (verb === 'list') {
        st.lists += 1
        if (st.hold) await new Promise<void>(res => { held.push(res) })
        if (o.listReply !== undefined) return ok(o.listReply)
        const first = st.lists === 1 && o.firstList !== undefined
        return ok(first ? o.firstList : st.lists > 2 ? LIST_AFTER_IMPORT : LIST_REPLY)
      }
      if (verb === 'import') {
        if (o.importFail != null) return bad({ ok: false, errors: [`import failed at step ${String(o.importFail.step)}`], step: o.importFail.step, reason: o.importFail.reason })
        return {
          value: {
            exitCode: 0,
            stdout: [
              JSON.stringify({ progress: true, step: 1, of: 3, name: 'Parse the template' }),
              JSON.stringify({ progress: true, step: 2, of: 3, name: 'Write the project' }),
              JSON.stringify({ progress: true, step: 3, of: 3, name: 'Write the tasks' }),
              JSON.stringify({ ok: true, id: 'plan-inbox-and-parsing', plan: 'plan-inbox-and-parsing', items: 9, steps: 3 }),
            ].join('\n'),
            stderr: '',
            isStdoutTruncated: false,
            isStderrTruncated: false,
          },
        }
      }
      if (verb === 'archive' || verb === 'restore' || verb === 'delete') return ok({ ok: true })
      return undefined
    },
  })
  on('ui.focus', () => ({}))
  on('fs.exists', (_$, e) => ({ value: String(e.path).startsWith(`${HOME}/`) }))
  mockFs(on, worldFiles())
  const script = (a: readonly string[]): number => a.findIndex(x => x.endsWith('/mk-inbox.py'))
  const inboxCalls = (verb?: string) => calls.filter(a => script(a) >= 0 && (verb == null || a[script(a) + 1] === verb))
  const settle = async (n = 30): Promise<void> => { for (let i = 0; i < n; i++) await clock.advance(0) }
  return { calls, clock, held, st, inboxCalls, settle }
}

type Engine = Parameters<TestBody>[0]
const mount = (engine: Engine) => engine.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(46) })

const HEAD = { text: /✉ Inbox/ }

test('WIRE.1.a1: the bar draws n Inbox after Waiting and before Todo, and hiddenTabs hides it', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  const bar = (await ui.findAll({ type: 'Button' })).filter(b => String(b.props.key).startsWith('dash-tab-'))
  expect(bar.map(b => b.props.key)).toEqual([
    'dash-tab-overview', 'dash-tab-lanes', 'dash-tab-plan', 'dash-tab-usage', 'dash-tab-files', 'dash-tab-events', 'dash-tab-waiting', 'dash-tab-inbox', 'dash-tab-todo', 'dash-tab-improve', 'dash-tab-settings',
  ])
  const inbox = bar.find(b => b.props.key === 'dash-tab-inbox')
  expect(inbox?.props.hotkey).toBe('n')
  expect(inbox?.props.label).toBe('Inbox')
})

test('WIRE.1.a1: hiddenTabs inbox hides it (key or label), and Settings stays', { plugins: [worldFixtures], options: { hiddenTabs: 'inbox' } }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  expect(await ui.find({ type: 'Button', key: 'dash-tab-inbox' })).toBeUndefined()
  expect(await ui.find({ type: 'Button', key: 'dash-tab-settings' })).toBeTruthy()
})

test('WIRE.1.a1: hiddenTabs Inbox (label, capitalised) hides it too', { plugins: [worldFixtures], options: { hiddenTabs: 'Inbox,lanes' } }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  expect(await ui.find({ type: 'Button', key: 'dash-tab-inbox' })).toBeUndefined()
  expect(await ui.find({ type: 'Button', key: 'dash-tab-lanes' })).toBeUndefined()
  expect(await ui.find({ type: 'Button', key: 'dash-tab-settings' })).toBeTruthy()
})

test('WIRE.1.a1: defaultTab inbox opens the pane on the Inbox tab', { plugins: [worldFixtures], options: { defaultTab: 'inbox' } }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await w.settle()
  expect(await ui.find(HEAD)).toBeTruthy()
  expect(w.inboxCalls('list').length, 'opening the saved tab loads the list').toBeGreaterThan(0)
})

test('WIRE.1.a2: the list runs once on open, not while the tab is closed, then on the tick', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  expect(w.inboxCalls('list'), 'no read while another tab is open').toHaveLength(0)
  await ui.press({ key: 'dash-tab-inbox' })
  await w.settle()
  expect(w.inboxCalls('list'), 'the open reads once, no tick waited for').toHaveLength(1)
  const argv = w.inboxCalls('list')[0]!
  expect(argv[0]).toBe('python3')
  expect(argv[1]!.endsWith('/scripts/mk-inbox.py')).toBe(true)
  expect(argv.slice(2, 4)).toEqual(['list', '--json'])
  expect(argv).toContain('--home')
  expect(argv[argv.indexOf('--home') + 1]).toBe(HOME)
  expect(argv[argv.indexOf('--data') + 1]).toBe(DATA_DIR)
  expect(await ui.find(HEAD)).toBeTruthy()
  expect(await ui.find({ text: /plan-inbox-and-parsing\.md/ }), 'the template card').toBeTruthy()
  expect(await ui.find({ text: /notes-2026-10-20\.md/ }), 'the not-a-plan card').toBeTruthy()
  expect(await ui.find({ text: 'MOCK · fixture data · not this run' }), 'never the fixture').toBeUndefined()
  await w.clock.advance(5000)
  await w.settle()
  expect(w.inboxCalls('list'), 'the tick reads again while the tab is open').toHaveLength(2)
  // closed: the tick stops reading
  await ui.press({ key: 'dash-tab-overview' })
  await w.settle()
  await w.clock.advance(5000)
  await w.settle()
  expect(w.inboxCalls('list'), 'a closed tab reads nothing').toHaveLength(2)
})

test('WIRE.1.a2: a slow read stays one in flight across two ticks', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on, { holdList: true })
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-inbox' })
  await w.settle(3)
  expect(w.inboxCalls('list'), 'the open read').toHaveLength(1)
  await w.clock.advance(5000) // a tick while the first read is still out
  await w.settle(3)
  expect(w.inboxCalls('list'), 'no second read while the first is out').toHaveLength(1)
  for (const release of w.held) release()
  await w.settle()
  expect(w.inboxCalls('list'), 'the held read lands and no second one was queued for it').toHaveLength(1)
  await w.clock.advance(5000) // the next tick reads again, now that nothing is in flight
  await w.settle()
  expect(w.inboxCalls('list'), 'the next tick reads once more').toHaveLength(2)
})

test('WIRE.1.a3: Import runs mk-inbox.py import <id>, shows the steps and reloads the list', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-inbox' })
  await w.settle()
  await ui.press({ key: 'inbox-import-plan-inbox-and-parsing' })
  await w.settle()
  const imports = w.inboxCalls('import')
  expect(imports).toHaveLength(1)
  const argv = imports[0]!
  expect(argv.slice(2, 4)).toEqual(['import', 'plan-inbox-and-parsing'])
  expect(w.inboxCalls('list').length, 'the list reloads after the write').toBeGreaterThan(1)
  // the card's done state: Imported as <plan>, with the plan's own phase and task counts
  expect(await ui.find({ text: /Imported as plan-inbox-and-parsing/ })).toBeTruthy()
  expect(await ui.find({ text: /2 phases · 3 tasks/ }), 'counted from the plan rows, not the status tally').toBeTruthy()
})

test('WIRE.1.a3: archive, restore and delete run their own argv; delete only asks first', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-inbox' })
  await w.settle()
  await ui.press({ key: 'inbox-archive-mk-ponytail-hooks' })
  await w.settle()
  expect(w.inboxCalls('archive')[0]?.slice(2, 5)).toEqual(['archive', 'mk-ponytail-hooks', '--home'])
  expect(w.inboxCalls('list').length, 'the list reloads').toBeGreaterThan(1)
  // delete asks on the card and spawns nothing until the confirm
  const before = w.inboxCalls().length
  await ui.press({ key: 'inbox-delete-plan-inbox-and-parsing' })
  await w.settle()
  expect(w.inboxCalls().length, 'the confirm press spawns nothing').toBe(before)
  expect(await ui.find({ type: 'Button', key: 'inbox-yes-plan-inbox-and-parsing' }), 'the confirm is on the card').toBeTruthy()
  expect(await ui.find({ type: 'Button', key: 'inbox-keep-plan-inbox-and-parsing' })).toBeTruthy()
  await ui.press({ key: 'inbox-yes-plan-inbox-and-parsing' })
  await w.settle()
  const del = w.inboxCalls('delete')[0]!
  expect(del.slice(2, 4)).toEqual(['delete', 'plan-inbox-and-parsing'])
  expect(del, 'a file still in the inbox is deleted without --archived').not.toContain('--archived')
})

test('WIRE.1.a3: a refused import draws the card failed state with the step and the reason', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on, { importFail: { step: 1, reason: 'no phase or task headings' } })
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-inbox' })
  await w.settle()
  await ui.press({ key: 'inbox-import-plan-inbox-and-parsing' })
  await w.settle()
  expect(await ui.find({ text: /Import failed at step 1/ })).toBeTruthy()
  expect(await ui.find({ text: /no phase or task headings/ })).toBeTruthy()
  expect(await ui.find({ type: 'Button', key: 'inbox-retry-plan-inbox-and-parsing' }), 'Retry on the card').toBeTruthy()
  expect(await ui.find({ text: /inbox import: import failed at step 1/ }), 'and the error line').toBeTruthy()
})

test('WIRE.1.a3: Open project switches to the Plan tab on that plan', { plugins: [worldFixtures] }, async ($, on) => {
  const w = await world(on)
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-inbox' })
  await w.settle()
  await ui.press({ key: 'inbox-import-plan-inbox-and-parsing' })
  await w.settle()
  await ui.press({ key: 'inbox-openproj-plan-inbox-and-parsing' })
  await w.settle()
  expect(await ui.find({ text: /no plans yet/ }), 'the Plan tab is the open one').toBeTruthy()
})

test('WIRE.1.a4: an inbox row with no run is shown with source inbox, and the Events tab stays live', { plugins: [worldFixtures] }, async ($, on) => {
  // the world has NO active run; the inbox channel answers one row of a real mk-inbox.py session
  const files = worldFiles()
  delete files['/fixtures/index.json']
  mockFs(on, files)
  const rows = [
    { id: 9, run: null, task: null, t: '2026-10-21T23:16:15+02:00', chan: 'inbox', kind: 'imported', from_id: 'user', to_id: null, reply_to: null, text: 'imported plan-inbox-and-parsing.md as plan-inbox-and-parsing', artifact_id: null, meta_json: JSON.stringify({ file: 'plan-inbox-and-parsing.md', plan: 'plan-inbox-and-parsing', fit: 'template', by: 'user' }) },
  ]
  const seen: string[][] = []
  const clock = mockWorld(on, {
    process: argv => {
      if (!argv.some(a => a.endsWith('mkdb.py')) || !argv.includes('events')) return undefined
      seen.push([...argv])
      if (argv.includes('inbox')) return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, events: rows }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
      return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, events: [] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    },
  })
  await $.session.start(sessionStart())
  for (let i = 0; i < 10; i++) await clock.advance(0)
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-events' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  await clock.advance(5000)
  for (let i = 0; i < 10; i++) await clock.advance(0)
  const inboxReads = seen.filter(a => a.includes('inbox'))
  expect(inboxReads.length, 'the inbox channel is read with no run').toBeGreaterThan(0)
  const argv = inboxReads[0]!
  expect(argv.slice(argv.indexOf('events') + 1, argv.indexOf('events') + 4)).toEqual(['--chan', 'inbox', '--json'])
  expect(argv, 'no --run for the inbox channel').not.toContain('--run')
  await ui.redraw(paneProps(46))
  expect(await ui.find({ text: /MOCK · fixture data · not this run/ }), 'not the offline fixture').toBeUndefined()
  expect(await ui.find({ type: 'Button', key: 'ev-open-i-9' }), 'the inbox row is in the list').toBeTruthy()
  await ui.press({ key: 'ev-open-i-9' })
  for (let i = 0; i < 10; i++) await clock.advance(0)
  const drawn = await ui.drawn()
  const texts = JSON.stringify(drawn)
  expect(texts).toContain('plan-inbox-and-parsing')
})

test('WIRE.1.a2: the list reads the MK home’s inbox even when no root store exists yet', { plugins: [worldFixtures] }, async ($, on) => {
  // no root mk.db: the folder `mk-inbox.py` reads is <home>/inbox, which exists without a store
  const w = await world(on, { firstList: LIST_REPLY })
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-inbox' })
  await w.settle()
  expect(w.inboxCalls('list'), 'one read, no store needed').toHaveLength(1)
  expect(w.inboxCalls('list')[0]![w.inboxCalls('list')[0]!.indexOf('--home') + 1]).toBe(HOME)
  expect(await ui.find(HEAD)).toBeTruthy()
  expect(await ui.find({ text: /plan-inbox-and-parsing\.md/ })).toBeTruthy()
})

test('WIRE.1.a3: Open runs the host opener on the file’s own path (an archived one under archive/)', { plugins: [worldFixtures] }, async ($, on) => {
  const openers: string[][] = []
  const w = await world(on, { openers })
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-inbox' })
  await w.settle()
  await ui.press({ key: 'inbox-open-plan-inbox-and-parsing' })
  await w.settle()
  // `open` is the macOS opener and answers 0, so xdg-open is never tried for it
  expect(openers).toEqual([['open', `${HOME}/inbox/plan-inbox-and-parsing.md`]])
})

test('WIRE.1.a3: Open on an archived card addresses the file under inbox/archive/', { plugins: [worldFixtures] }, async ($, on) => {
  const openers: string[][] = []
  const archived = { ok: true, files: [{ ...LIST_REPLY.files[1], archived: true }], counts: { inbox: 0, template: 0, fix: 0, archived: 1 } }
  const w = await world(on, { openers, listReply: archived })
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-inbox' })
  await w.settle()
  await ui.press({ key: 'inbox-chip-archived' })
  await w.settle()
  await ui.press({ key: 'inbox-open-plan-inbox-and-parsing' })
  await w.settle()
  expect(openers).toEqual([['open', `${HOME}/inbox/archive/plan-inbox-and-parsing.md`]])
})

test('WIRE.1.a3: an Open whose openers both refuse draws the card’s failed state', { plugins: [worldFixtures] }, async ($, on) => {
  // both openers refuse: the loop tries each and then fails the card
  const openers: string[][] = []
  const w = await world(on, { openers, openerExit: 127 })
  await $.session.start(sessionStart())
  await w.settle()
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-inbox' })
  await w.settle()
  await ui.press({ key: 'inbox-open-plan-inbox-and-parsing' })
  await w.settle()
  expect(await ui.find({ text: /could not open plan-inbox-and-parsing\.md/ })).toBeTruthy()
})
