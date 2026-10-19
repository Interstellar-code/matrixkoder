// Improve wiring in the pane: the footer Improve chip on every tab but Settings, the
// Improve tab between Todo and Settings, and the shared proposal. The model, the session, the
// prompt line and the store are mocked beneath the plugin; a real terminal draw is not covered here.
import { test, expect } from 'claude-code/testing'
import type { Engine, Mounted } from 'claude-code/testing'
import { mockFs, mockWorld, paneProps, sessionStart, worldFiles, worldFixtures } from './testkit'
import { IMPROVE_FAILED, IMPROVE_FILL_FAILED, wordCount } from './improve-core'

const usage = { input_tokens: 1, output_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }
const ROUGH = 'Check all the files with type errors.'
const BETTER = "Run tsc --noEmit and list every file with type errors. Give each error's file, line and cause. Don't edit anything yet."
const REWRITE = JSON.stringify({ prompt: BETTER, added: ['The check to run: tsc --noEmit', 'Output: file, line and cause', 'No edits yet'] })
const WIZARD_REWRITE = JSON.stringify({ prompt: 'Run tsc --noEmit for my team and list every file. [the folder]', added: ['Audience: my team'] })
const QUESTIONS = JSON.stringify({
  questions: [
    { question: 'Who is it for?', options: ['Customers', 'My team'] },
    { question: 'How long?', options: ['One page', 'Short'] },
  ],
})
const SAVED = [{ id: 'old-1', at: Date.parse('2026-10-17T09:00:00Z'), original: 'old rough prompt', upgraded: 'old better prompt', added: ['one detail'] }]

type Answer = string | null | 'throw'
type World = {
  /** what the store holds before the session starts */
  stored?: Record<string, unknown>
  /** the in-memory files the pane reads (the world's by default) */
  files?: Record<string, string>
  /** one answer per model call: the text, null for an API failure, 'throw' for a refused request */
  answer: (n: number, isQuestions: boolean) => Answer | Promise<Answer>
  submit?: 'drop' | 'throw'
  copy?: boolean
  /** the draft Claude Code's prompt line holds when the test mounts */
  draft?: string
}

/** the world beneath the pane: model, conversation, prompt box, clipboard, store */
function world(on: Parameters<typeof mockWorld>[0], w: World) {
  const log = { prompts: [] as string[], models: [] as string[], systems: [] as string[], calls: 0, submitted: [] as { text: string; asUser?: boolean }[], copied: [] as string[] }
  const clock = mockWorld(on)
  mockFs(on, w.files ?? worldFiles())
  // Claude Code's own prompt line, in memory: read resolves what it holds, fill writes it
  // (the real shapes: PromptBox `{ text, cursor }`, PromptFilled `{ isFilled, text, cursor }`)
  const box = { text: w.draft ?? '', refuses: false }
  const fills: { text: string; mode: string }[] = []
  on('prompt.read', () => ({ value: { text: box.text, cursor: box.text.length } }))
  // prompt.fill is an event: its handler answers the result itself, not a value wrapper
  on('prompt.fill', (_$, e) => {
    fills.push({ text: e.text, mode: e.mode })
    if (box.refuses) return { isFilled: false as const, refusal: 'dialog' as const }
    box.text = e.mode === 'append' ? box.text + e.text : e.text
    return { isFilled: true as const }
  })
  // the plugin's own store, in memory and readable by the test
  const stored = new Map<string, unknown>(Object.entries(w.stored ?? {}))
  on('store.get', (_$, e) => ({ value: stored.get(e.key) }))
  on('store.set', (_$, e) => {
    stored.set(e.key, e.value)
    return { value: undefined }
  })
  on('model.complete', async (_$, e) => {
    const n = log.calls++
    log.prompts.push(e.prompt)
    log.models.push(e.model ?? '')
    log.systems.push(typeof e.system === 'string' ? e.system : '')
    const out = await w.answer(n, e.prompt.includes('multiple-choice'))
    if (out === 'throw') throw new Error('request refused')
    return { value: out === null ? { isAnswered: false, reason: 'api-error', status: 529, error: 'overloaded', usage } : { isAnswered: true, text: out, usage } }
  })
  on('session.messages', () => ({
    value: [
      { role: 'user', text: 'build the weekly report', toolUses: [] },
      { role: 'assistant', text: 'Done: the report is in out/report.md', toolUses: [] },
      { role: 'user', text: '', toolUses: [] },
    ],
  }))
  on('prompt.submit', (_$, e) => {
    log.submitted.push({ text: e.text, asUser: (e.origin as { asUser?: boolean }).asUser })
    if (w.submit === 'throw') throw new Error('queue closed')
    return w.submit === 'drop' ? { drop: 'a Stop hook said no' } : { text: e.text }
  })
  on('ui.copy', (_$, e) => {
    log.copied.push(e.text)
    return { value: w.copy !== false ? { isCopied: true as const } : { isCopied: false as const, reason: 'no-clipboard' as const } }
  })
  const settle = async (n = 10) => { for (let i = 0; i < n; i++) await clock.advance(5) }
  return { log, clock, settle, stored, box, fills }
}

type Ui = Mounted<'terminal', 'Pane'>
const mount = ($: Engine, cols = 46): Promise<Ui> =>
  $.ui.mount({ plugin: 'matrixkoder', surface: 'terminal', component: 'Pane', requestId: 'matrixkoder', props: paneProps(cols) })

/** what Claude Code's prompt line holds: the world's box, as the person would see it */
const boxValue = (w: ReturnType<typeof world>): string => w.box.text
const type = (w: ReturnType<typeof world>, text: string): void => { w.box.text = text }
const has = async (ui: Ui, q: Parameters<Ui['find']>[0]): Promise<boolean> => (await ui.find(q)) !== undefined
/** a sentence the card wrapped over several rows, read whole */
const says = async (ui: Ui, sentence: string): Promise<boolean> =>
  (await ui.findAll({ type: 'Text' })).map(t => String(t.text)).join(' ').includes(sentence)

/** type a prompt in the prompt line, press Improve, let the timer-started model call finish */
async function improve(ui: Ui, w: ReturnType<typeof world>, text = ROUGH) {
  type(w, text)
  await ui.press({ key: 'improve-f-go' })
  await w.settle()
}

test('the footer: the solid Improve chip and two + tool slots sit between the dashed rule and the counts, on every tab but Settings, and no prompt field', { plugins: [worldFixtures] }, async ($, on) => {
  world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  for (const tab of ['overview', 'lanes', 'plan', 'usage', 'events', 'waiting', 'kanban', 'todo', 'improve']) {
    await ui.press({ key: `dash-tab-${tab}` })
    expect(await has(ui, { type: 'Input', key: 'improve-q' }), `no box of its own on ${tab}`).toBe(false)
    expect(await has(ui, { type: 'Button', key: 'improve-f-go' }), `the chip on ${tab}`).toBe(true)
  }
  await ui.press({ key: 'dash-tab-overview' })
  const order = (await ui.findAll({ type: 'Text' })).map(t => t.text)
  const at = (s: RegExp): number => order.findIndex(t => s.test(String(t)))
  expect(at(/^╌+$/)).toBeGreaterThan(-1)
  // the chips row is the footer's own: it sits between the dashed rule and the counts row, with no prompt row
  expect(at(/^╌+$/)).toBeLessThan(order.findIndex(t => String(t).includes('✦ Improve')))
  expect(order.findIndex(t => String(t).includes('✦ Improve'))).toBeLessThan(at(/ live · /))
  expect(order.filter(t => t === '┊ + tool ┊')).toHaveLength(2)
  expect((await ui.find({ type: 'Button', key: 'improve-f-go' }))?.text).toBe(' ✦ Improve ')
  expect(await has(ui, { text: '↶ Undo' }), 'no Undo before an accept').toBe(false)
  await ui.press({ key: 'dash-tab-settings' })
  expect(await has(ui, { type: 'Button', key: 'improve-f-go' }), 'Settings draws no footer').toBe(false)
})

test('with no active run the footer keeps Improve, without the counts row', { plugins: [worldFixtures] }, async ($, on) => {
  const files = worldFiles()
  const w = world(on, { answer: () => REWRITE, files })
  await $.session.start(sessionStart())
  delete files['/tmp/mk-proj/.matrixkoder/runs/wfv2/run.json'] // the run is archived
  await w.clock.advance(5000)
  const ui = await mount($)
  expect(await says(ui, 'No active orchestration run.')).toBe(true)
  expect(await has(ui, { type: 'Button', key: 'improve-f-go' })).toBe(true)
  expect(await has(ui, { text: / live · / }), 'no counts without a run').toBe(false)
  await improve(ui, w)
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' })).toBe(true)
})

test('the Improve tab sits between Todo and Settings with hotkey i; hiddenTabs hides it', { plugins: [worldFixtures] }, async ($, on) => {
  world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  const bar = (await ui.findAll({ type: 'Button' })).filter(b => String(b.props.key).startsWith('dash-tab-'))
  const keys = bar.map(b => String(b.props.key))
  expect(keys.indexOf('dash-tab-improve')).toBe(keys.indexOf('dash-tab-todo') + 1)
  expect(keys.indexOf('dash-tab-settings')).toBe(keys.indexOf('dash-tab-improve') + 1)
  expect(bar.find(b => b.props.key === 'dash-tab-improve')?.props.hotkey).toBe('i')
  expect(new Set(bar.map(b => b.props.hotkey)).size, 'every hotkey distinct').toBe(bar.length)
  await ui.press({ key: 'dash-tab-improve' })
  expect(await says(ui, 'Type a prompt in Claude Code’s prompt line and press ✦ Improve.')).toBe(true)
})

test('hiddenTabs improve hides the tab (key or label); defaultTab improve opens on it', { plugins: [worldFixtures], options: { hiddenTabs: 'Improve' } }, async ($, on) => {
  world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  expect(await has(ui, { type: 'Button', key: 'dash-tab-improve' })).toBe(false)
  expect(await has(ui, { type: 'Button', key: 'dash-tab-settings' })).toBe(true)
  expect(await has(ui, { type: 'Button', key: 'improve-f-go' }), 'the footer stays').toBe(true)
})

test('defaultTab improve opens the pane on the Improve tab', { plugins: [worldFixtures], options: { defaultTab: 'improve' } }, async ($, on) => {
  world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  expect(await says(ui, 'Type a prompt in Claude Code’s prompt line and press ✦ Improve.')).toBe(true)
})

test('Improve: one model call (haiku), the card shows the proposal and the word counts, the box is not touched', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  type(w, `  ${ROUGH}  `)
  await ui.press({ key: 'improve-f-go' })
  expect(await says(ui, 'Writing the new prompt…'), 'working shows a calm line').toBe(true)
  await w.settle()
  expect(w.log.calls, 'rewrite only, no questions').toBe(1)
  expect(w.log.models).toEqual(['haiku'])
  expect(w.log.prompts[0]).toContain(`<prompt>\n${ROUGH}\n</prompt>`)
  expect(w.log.prompts[0]).not.toContain('multiple-choice')
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' })).toBe(true)
  expect(await says(ui, `${String(wordCount(ROUGH))} → ${String(wordCount(BETTER))} words`)).toBe(true)
  const was = await ui.find({ type: 'Text', text: ROUGH })
  expect(was?.props).toMatchObject({ strikethrough: true })
  expect(await has(ui, { text: /Run tsc --noEmit and list every file/ })).toBe(true)
  expect(await has(ui, { type: 'Button', key: 'improve-f-accept' })).toBe(true)
  expect(await has(ui, { type: 'Button', key: 'improve-f-wizard' })).toBe(true)
  expect(await has(ui, { type: 'Button', key: 'improve-f-discard' })).toBe(true)
  expect(boxValue(w), 'the box keeps the person’s text').toBe(`  ${ROUGH}  `)
  expect(w.log.submitted, 'nothing is sent').toEqual([])
})

test('the conversation reaches the model: the last messages as Person / Assistant rows, empty rows left out', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w, 'do that again')
  const prompt = w.log.prompts[0] ?? ''
  expect(prompt).toContain('<conversation>\nPerson: build the weekly report\nAssistant: Done: the report is in out/report.md\n</conversation>')
  expect(prompt.split('\n').filter(l => l.startsWith('Person:'))).toHaveLength(1)
  expect(prompt).toContain('<prompt>\ndo that again\n</prompt>')
  expect(prompt).toContain('say what it refers to, using only what the conversation actually shows')
  expect(prompt).toContain('Never invent facts')
})

test('Accept fills the box with the rewrite and shows Undo; Undo restores the original text exactly', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  const original = `  ${ROUGH}\nsecond line  `
  await improve(ui, w, original)
  await ui.press({ key: 'improve-f-accept' })
  expect(boxValue(w)).toBe(BETTER)
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' }), 'the card is gone').toBe(false)
  expect((await ui.find({ type: 'Button', key: 'improve-f-undo' }))?.text).toBe(' ↶ Undo ')
  await ui.press({ key: 'improve-f-undo' })
  expect(boxValue(w)).toBe(original)
  expect(await has(ui, { type: 'Button', key: 'improve-f-undo' }), 'Undo is spent').toBe(false)
  expect(w.log.submitted, 'accepting never sends').toEqual([])
})

test('Discard closes the card and leaves the box as it was', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  await ui.press({ key: 'improve-f-discard' })
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' })).toBe(false)
  expect(boxValue(w)).toBe(ROUGH)
  expect(await has(ui, { type: 'Button', key: 'improve-f-undo' })).toBe(false)
})

test('Run Wizard switches the same card to the questions; answers and Skip shape the new rewrite', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: (_n, isQuestions) => (isQuestions ? QUESTIONS : w2()) })
  let calls = 0
  const w2 = (): string => (calls++ === 0 ? REWRITE : WIZARD_REWRITE)
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  expect(w.log.calls).toBe(1)
  await ui.press({ key: 'improve-f-wizard' })
  expect(await says(ui, 'Reading your prompt and working out what to ask…')).toBe(true)
  await w.settle()
  expect(w.log.calls, 'the questions call').toBe(2)
  expect(w.log.prompts[1]).toContain('Ask up to 3 multiple-choice questions')
  expect(await has(ui, { text: '✦ WIZARD' }), 'the same card now asks').toBe(true)
  expect(await has(ui, { text: /1 Who is it for\?/ })).toBe(true)
  for (const b of ['improve-f-q0o0', 'improve-f-q0o1', 'improve-f-q0other', 'improve-f-q0skip', 'improve-f-q1o0']) expect(await has(ui, { type: 'Button', key: b }), b).toBe(true)
  await ui.press({ key: 'improve-f-q0o1' }) // My team
  expect(await has(ui, { text: '1 of 2 answered' })).toBe(true)
  await ui.press({ key: 'improve-f-q1skip' })
  expect(await has(ui, { text: '2 of 2 answered' })).toBe(true)
  expect(await has(ui, { type: 'Button', text: 'Upgrade now' })).toBe(true)
  await ui.press({ key: 'improve-f-upgrade' })
  await w.settle()
  expect(w.log.calls).toBe(3)
  expect(w.log.prompts[2]).toContain('- Who is it for? My team')
  expect(w.log.prompts[2]).not.toContain('How long?')
  expect(w.log.prompts[2]).not.toContain('The person answered no questions.')
  expect(await has(ui, { text: /for my team/ }), 'the new rewrite shows in the same card').toBe(true)
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' })).toBe(true)
  await ui.press({ key: 'improve-f-accept' })
  expect(boxValue(w)).toBe('Run tsc --noEmit for my team and list every file. [the folder]')
})

test('Run Wizard: Other (type it) takes the typed answer; Skip the rest, upgrade now skips what is left', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: (n, isQuestions) => (isQuestions ? QUESTIONS : n === 0 ? REWRITE : WIZARD_REWRITE) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  await ui.press({ key: 'improve-f-wizard' })
  await w.settle()
  await ui.press({ key: 'improve-f-q0other' })
  await ui.input({ key: 'improve-other-0', text: 'the board' })
  expect(await has(ui, { text: '✓ Other: the board' })).toBe(true)
  expect(await has(ui, { text: 'Skip the rest, upgrade now' })).toBe(true)
  await ui.press({ key: 'improve-f-upgrade' })
  await w.settle()
  expect(w.log.prompts[2]).toContain('- Who is it for? the board')
  expect(w.log.prompts[2]).not.toContain('How long?')
})

test('Run Wizard: when no question would help the rewrite stands, nothing is lost', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: (_n, isQuestions) => (isQuestions ? '{"questions":[]}' : REWRITE) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  await ui.press({ key: 'improve-f-wizard' })
  await w.settle()
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' })).toBe(true)
  expect(await says(ui, 'No question would help. This rewrite stands.')).toBe(true)
  expect(w.log.calls).toBe(2)
})

test('a failing model call shows one calm line and Try again; the box is untouched and nothing is sent', { plugins: [worldFixtures] }, async ($, on) => {
  const answers: Answer[] = [null, 'throw', 'not json at all', REWRITE]
  const w = world(on, { answer: n => (n < answers.length ? answers[n] as Answer : REWRITE) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w) // 0: the API failed
  expect(await says(ui, IMPROVE_FAILED), 'api failure').toBe(true)
  expect(boxValue(w)).toBe(ROUGH)
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' })).toBe(false)
  await ui.press({ key: 'improve-f-retry' }) // 1: the request was refused (it rejects)
  await w.settle()
  expect(w.log.calls, 'second call made').toBe(2)
  expect(await says(ui, IMPROVE_FAILED), 'refused request').toBe(true)
  await ui.press({ key: 'improve-f-retry' }) // 2: an unreadable answer
  await w.settle()
  expect(await says(ui, IMPROVE_FAILED), 'unreadable answer').toBe(true)
  expect(boxValue(w)).toBe(ROUGH)
  await ui.press({ key: 'improve-f-retry' }) // 3: the rewrite
  await w.settle()
  expect(w.log.calls).toBe(4)
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' })).toBe(true)
  expect(await says(ui, IMPROVE_FAILED)).toBe(false)
  expect(w.log.submitted).toEqual([])
})

test('a failing question call shows the calm line; Try again asks again', { plugins: [worldFixtures] }, async ($, on) => {
  let asked = 0
  const w = world(on, { answer: (_n, isQuestions) => (isQuestions ? (asked++ === 0 ? null : QUESTIONS) : REWRITE) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  await ui.press({ key: 'improve-f-wizard' })
  await w.settle()
  expect(await says(ui, IMPROVE_FAILED)).toBe(true)
  await ui.press({ key: 'improve-f-retry' })
  await w.settle()
  expect(await has(ui, { text: '✦ WIZARD' })).toBe(true)
  expect(boxValue(w)).toBe(ROUGH)
})

test('Improve on an empty prompt line says so and asks nothing', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await ui.press({ key: 'improve-f-go' })
  await w.settle()
  expect(await says(ui, 'Type a prompt in Claude Code’s prompt line, then press Improve.')).toBe(true)
  expect(w.log.calls).toBe(0)
  // Try again with the prompt line filled now improves it
  type(w, ROUGH)
  await ui.press({ key: 'improve-f-retry' })
  await w.settle()
  expect(w.log.calls).toBe(1)
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' })).toBe(true)
})

test('no footer or tab field is drawn, so nothing of the Improve UI sends a prompt any more', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($, 80)
  await improve(ui, w)
  await ui.press({ key: 'dash-tab-improve' })
  expect(await has(ui, { type: 'Input', key: 'improve-q' }), 'no prompt field in the footer or the tab').toBe(false)
  await ui.press({ key: 'improve-t-accept' })
  expect(w.log.submitted, 'accepting never sends').toEqual([])
  expect(boxValue(w)).toBe(BETTER)
})

test('the Improve tab and the footer share one proposal: Accept in the tab does what Accept in the footer does', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($, 80)
  await improve(ui, w)
  await ui.press({ key: 'dash-tab-improve' })
  expect(await has(ui, { text: 'proposal ready' })).toBe(true)
  for (const k of ['improve-t-accept', 'improve-t-wizard', 'improve-t-edit', 'improve-t-copy', 'improve-t-discard']) expect(await has(ui, { type: 'Button', key: k }), k).toBe(true)
  expect(await has(ui, { text: 'WHAT I ADDED' }) || await has(ui, { text: '> WHAT I ADDED' })).toBe(true)
  expect(await has(ui, { text: '+' })).toBe(true)
  expect(await has(ui, { text: 'details added' })).toBe(true)
  expect(await has(ui, { type: 'Button', key: 'improve-f-accept' }), 'the footer card gives way to the tab').toBe(false)
  await ui.press({ key: 'improve-t-accept' })
  expect(boxValue(w)).toBe(BETTER)
  expect(await has(ui, { type: 'Button', key: 'improve-f-undo' })).toBe(true)
  // back on Overview the proposal is gone too: one state
  await ui.press({ key: 'dash-tab-overview' })
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' })).toBe(false)
  // the other way round: Improve in the footer on another tab, Discard in the tab
  await ui.press({ key: 'improve-f-undo' })
  await ui.press({ key: 'improve-f-go' })
  await w.settle()
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' })).toBe(true)
  await ui.press({ key: 'dash-tab-improve' })
  await ui.press({ key: 'improve-t-discard' })
  await ui.press({ key: 'dash-tab-overview' })
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' })).toBe(false)
  expect(boxValue(w)).toBe(ROUGH)
})

test('the tab: Edit shows the field, the draft is what Accept takes; Copy puts it on the clipboard', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($, 80)
  await improve(ui, w)
  await ui.press({ key: 'dash-tab-improve' })
  await ui.press({ key: 'improve-t-copy' })
  expect(w.log.copied).toEqual([BETTER])
  expect(await has(ui, { text: 'Copied.' })).toBe(true)
  await ui.press({ key: 'improve-t-edit' })
  expect(boxValue(w), 'editing never touches the footer box').toBe(ROUGH)
  expect((await ui.find({ type: 'Input', key: 'improve-edit' }))?.props.value).toBe(BETTER)
  await ui.input({ key: 'improve-edit', text: 'Run tsc and list the files.', kind: 'change' })
  await ui.press({ key: 'improve-t-accept' })
  expect(boxValue(w)).toBe('Run tsc and list the files.')
})

test('Copy that cannot reach the clipboard says so', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE, copy: false })
  await $.session.start(sessionStart())
  const ui = await mount($, 80)
  await improve(ui, w)
  await ui.press({ key: 'dash-tab-improve' })
  await ui.press({ key: 'improve-t-copy' })
  expect(await has(ui, { text: 'Could not copy. Select the text instead.' })).toBe(true)
})

test('the last improvements are kept in the store and listed in the tab; a click reopens its review', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE, stored: { improveHistory: SAVED } })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await w.settle()
  await ui.press({ key: 'dash-tab-improve' })
  expect(await has(ui, { text: 'old rough prompt' }), 'the saved list loads at session start').toBe(true)
  await improve(ui, w) // a new one joins the list, newest first
  const saved = w.stored.get('improveHistory') as { original: string }[]
  expect(saved.map(e => e.original)).toEqual([ROUGH, 'old rough prompt'])
  await ui.press({ key: 'improve-t-discard' })
  const rows = (await ui.findAll({ type: 'Button' })).filter(b => String(b.props.key).startsWith('improve-t-h'))
  expect(rows.map(b => String(b.props.label))).toEqual([expect.stringContaining('Check all the files'), expect.stringContaining('old rough prompt')])
  await ui.press({ key: String(rows[1]?.props.key) })
  expect(await has(ui, { text: 'proposal ready' })).toBe(true)
  expect(await has(ui, { text: 'old better prompt' })).toBe(true)
  expect(w.log.calls, 'reopening asks the model nothing').toBe(1)
  // accepting a reopened one fills the box; the text it held is kept for Undo
  await ui.press({ key: 'improve-t-accept' })
  expect(boxValue(w)).toBe('old better prompt')
  await ui.press({ key: 'improve-f-undo' })
  expect(boxValue(w)).toBe(ROUGH)
})

test('the list keeps twenty at most', { plugins: [worldFixtures], timeoutMs: 30000 }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  for (let i = 0; i < 22; i++) {
    await improve(ui, w, `prompt number ${String(i)}`)
    await ui.press({ key: 'improve-f-discard' })
  }
  const saved = w.stored.get('improveHistory') as { original: string }[]
  expect(saved).toHaveLength(20)
  expect(saved[0]?.original).toBe('prompt number 21')
})

test('F1: two Accept presses in a row leave Undo holding the typed original, and Undo restores it', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  // the second press may find the card gone; either way it must not change what Undo holds
  await Promise.allSettled([ui.press({ key: 'improve-f-accept' }), ui.press({ key: 'improve-f-accept' })])
  expect(boxValue(w)).toBe(BETTER)
  await ui.press({ key: 'improve-f-undo' })
  expect(boxValue(w), 'Undo gives back the typed original, not the rewrite').toBe(ROUGH)
})

test('F4: Improve reads Claude Code\u2019s prompt line at the press, and Accept writes the proposal back with replace', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE, draft: 'the person\u2019s own draft' })
  await $.session.start(sessionStart())
  const ui = await mount($)
  // the draft typed in the prompt line is what gets rewritten, with no field of ours involved
  await ui.press({ key: 'improve-f-go' })
  await w.settle()
  expect(w.log.prompts[0]).toContain('<prompt>\nthe person\u2019s own draft\n</prompt>')
  await ui.press({ key: 'improve-f-accept' })
  expect(w.fills, 'one replace over the draft').toEqual([{ text: BETTER, mode: 'replace' }])
  expect(boxValue(w)).toBe(BETTER)
})

test('review 2: what the person typed after the press is never sent, and Accept\u2019s Undo holds the text it replaced', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  type(w, 'a different draft now')
  await ui.press({ key: 'improve-f-accept' })
  expect(w.log.submitted, 'the Improve UI never sends').toEqual([])
  expect(boxValue(w)).toBe(BETTER)
  expect(w.fills, 'the proposal replaced what the line held').toEqual([{ text: BETTER, mode: 'replace' }])
  await ui.press({ key: 'improve-f-undo' })
  expect(boxValue(w), 'Undo gives back the text Accept replaced').toBe('a different draft now')
})

test('a fill the engine refuses (a dialog holds the box) keeps the proposal on show and says so', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  w.box.refuses = true
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  await ui.press({ key: 'improve-f-accept' })
  expect(boxValue(w), 'the prompt line is untouched').toBe(ROUGH)
  expect(await says(ui, IMPROVE_FILL_FAILED), 'the card says why').toBe(true)
  expect(await has(ui, { text: /Run tsc --noEmit and list every file/ }), 'the proposal stays').toBe(true)
  expect(await has(ui, { type: 'Button', key: 'improve-f-accept' }), 'Accept can be tried again').toBe(true)
  expect(await has(ui, { type: 'Button', key: 'improve-f-undo' }), 'no Undo without a write').toBe(false)
  // the dialog closes: the same Accept now lands
  w.box.refuses = false
  await ui.press({ key: 'improve-f-accept' })
  expect(boxValue(w)).toBe(BETTER)
  expect(await has(ui, { text: /IMPROVED PROMPT/ }), 'the card clears').toBe(false)
})

test('Undo is dropped once the person types over what Accept wrote, and never overwrites it', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  await ui.press({ key: 'improve-f-accept' })
  expect(await has(ui, { type: 'Button', key: 'improve-f-undo' })).toBe(true)
  type(w, `${BETTER} And keep it short.`)
  // the 5 s tick sees the line changed and drops Undo; the chip is gone and the text stays
  await w.clock.advance(5000)
  await ui.redraw(paneProps(46))
  expect(await has(ui, { type: 'Button', key: 'improve-f-undo' }), 'Undo was dropped').toBe(false)
  expect(boxValue(w)).toBe(`${BETTER} And keep it short.`)
})

test('Undo pressed right after typing, before any tick, never overwrites the new text', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  await ui.press({ key: 'improve-f-accept' })
  type(w, `${BETTER} And keep it short.`)
  await ui.press({ key: 'improve-f-undo' })
  expect(boxValue(w)).toBe(`${BETTER} And keep it short.`)
  expect(await has(ui, { type: 'Button', key: 'improve-f-undo' }), 'Undo was dropped').toBe(false)
})

test('review 3: a saved improvement is cut to 4000 characters each side, so one pasted wall cannot fill the store', { plugins: [worldFixtures] }, async ($, on) => {
  const big = 'x'.repeat(9000)
  const w = world(on, { answer: () => JSON.stringify({ prompt: big, added: ['a'] }) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w, big)
  const saved = w.stored.get('improveHistory') as { original: string; upgraded: string }[]
  expect(saved[0]?.original).toHaveLength(4000)
  expect(saved[0]?.upgraded).toHaveLength(4000)
})

test('review 6: Run Wizard keeps an edit in progress as the proposal, even when the question call then fails', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: (_n, isQuestions) => (isQuestions ? null : REWRITE) })
  await $.session.start(sessionStart())
  const ui = await mount($, 80)
  await improve(ui, w)
  await ui.press({ key: 'dash-tab-improve' })
  await ui.press({ key: 'improve-t-edit' })
  await ui.input({ key: 'improve-edit', text: 'my own edit', kind: 'change' })
  await ui.press({ key: 'improve-t-wizard' })
  await w.settle()
  expect(await has(ui, { text: IMPROVE_FAILED }), 'the question call failed').toBe(true)
  await ui.press({ key: 'improve-t-accept' })
  expect(boxValue(w), 'the edit, not the older rewrite, is what Accept takes').toBe('my own edit')
})

test('review 12: while an Improve input has the focus the Todo a / z hotkeys stand down, like todo-q', { plugins: [worldFixtures] }, async ($, on) => {
  on('ui.focus', () => ({}))
  world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await ui.press({ key: 'dash-tab-todo' })
  const hotkeyOf = async (key: string): Promise<unknown> =>
    (await ui.find({ type: 'Button', key }) as { props: { hotkey?: string } } | undefined)?.props.hotkey
  expect(await hotkeyOf('todo-key-a'), 'armed before the box has the ring').toBe('a')
  await $.ui.focus({ component: 'Pane', requestId: 'matrixkoder', element: 'improve-edit', origin: { kind: 'person' } })
  await ui.redraw(paneProps(46))
  expect(await hotkeyOf('todo-key-a'), 'inert while an Improve input holds the ring').toBeUndefined()
  await $.ui.focus({ component: 'Pane', requestId: 'matrixkoder', element: 'improve-f-go', origin: { kind: 'person' } })
  await ui.redraw(paneProps(46))
  expect(await hotkeyOf('todo-key-a'), 'back once the ring leaves the box').toBe('a')
})

test('F4: Accept in the Improve tab writes the proposal into the prompt line too', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($, 80)
  await improve(ui, w)
  await ui.press({ key: 'dash-tab-improve' })
  await ui.press({ key: 'improve-t-accept' })
  expect(w.fills).toEqual([{ text: BETTER, mode: 'replace' }])
  expect(boxValue(w)).toBe(BETTER)
})

test('F2: what is typed in the wizard’s Other field survives a redraw, a refresh tick, a tab switch and a pick on another question; Enter commits it', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: (n, isQuestions) => (isQuestions ? QUESTIONS : n === 0 ? REWRITE : WIZARD_REWRITE) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  await ui.press({ key: 'improve-f-wizard' })
  await w.settle()
  await ui.press({ key: 'improve-f-q0other' })
  await ui.input({ key: 'improve-other-0', text: 'the boa', kind: 'change' })
  const typed = async (): Promise<string> => String((await ui.find({ type: 'Input', key: 'improve-other-0' }))?.props.value ?? '')
  expect(await typed()).toBe('the boa')
  await ui.redraw(paneProps(46))
  expect(await typed(), 'after a redraw').toBe('the boa')
  await ui.redraw(paneProps(36))
  expect(await typed(), 'after a resize').toBe('the boa')
  await w.clock.advance(5000)
  expect(await typed(), 'after the refresh tick').toBe('the boa')
  await ui.press({ key: 'dash-tab-lanes' })
  await ui.press({ key: 'dash-tab-overview' })
  expect(await typed(), 'after a tab switch').toBe('the boa')
  await ui.press({ key: 'improve-f-q1o1' }) // an answer on the OTHER question leaves the field and its text alone
  expect(await typed(), 'after a pick on another question').toBe('the boa')
  expect(await has(ui, { text: '1 of 2 answered' })).toBe(true)
  await ui.input({ key: 'improve-other-0', text: 'the board' }) // Enter commits it as the answer
  expect(await has(ui, { type: 'Input', key: 'improve-other-0' })).toBe(false)
  expect(await has(ui, { text: '✓ Other: the board' })).toBe(true)
  expect(await has(ui, { text: '2 of 2 answered' })).toBe(true)
})

test('F2: upgrading with a half-typed Other counts the text; opening another Other keeps the first one’s text as its answer', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: (n, isQuestions) => (isQuestions ? QUESTIONS : n === 0 ? REWRITE : WIZARD_REWRITE) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  await ui.press({ key: 'improve-f-wizard' })
  await w.settle()
  await ui.press({ key: 'improve-f-q0other' })
  await ui.input({ key: 'improve-other-0', text: 'the board', kind: 'change' })
  await ui.press({ key: 'improve-f-q1other' })
  expect(await has(ui, { text: '✓ Other: the board' }), 'the first text became its answer').toBe(true)
  await ui.input({ key: 'improve-other-1', text: 'two pages', kind: 'change' })
  await ui.press({ key: 'improve-f-upgrade' })
  await w.settle()
  expect(w.log.prompts[2]).toContain('- Who is it for? the board')
  expect(w.log.prompts[2]).toContain('- How long? two pages')
})

test('F7: two quick presses of Run Wizard, Upgrade now and Try again each start one model call', { plugins: [worldFixtures] }, async ($, on) => {
  let rewrites = 0
  const w = world(on, { answer: (_n, isQuestions) => (isQuestions ? QUESTIONS : rewrites++ === 0 ? REWRITE : WIZARD_REWRITE) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  expect(w.log.calls).toBe(1)
  await Promise.allSettled([ui.press({ key: 'improve-f-wizard' }), ui.press({ key: 'improve-f-wizard' })])
  await w.settle()
  expect(w.log.calls, 'one questions call').toBe(2)
  // two fast picks both land (merged into the current answers)
  await Promise.allSettled([ui.press({ key: 'improve-f-q0o0' }), ui.press({ key: 'improve-f-q1o1' })])
  expect(await has(ui, { text: '2 of 2 answered' })).toBe(true)
  await Promise.allSettled([ui.press({ key: 'improve-f-upgrade' }), ui.press({ key: 'improve-f-upgrade' })])
  await w.settle()
  expect(w.log.calls, 'one rewrite call').toBe(3)
  expect(w.log.prompts[2]).toContain('- Who is it for? Customers')
  expect(w.log.prompts[2]).toContain('- How long? Short')
})

test('F7: two quick presses of Try again start one model call', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: n => (n === 0 ? null : REWRITE) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  expect(await says(ui, IMPROVE_FAILED)).toBe(true)
  await Promise.allSettled([ui.press({ key: 'improve-f-retry' }), ui.press({ key: 'improve-f-retry' })])
  await w.settle()
  expect(w.log.calls, 'the first call and one retry').toBe(2)
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' })).toBe(true)
})

test('F8: a failed wizard call keeps the shown rewrite visible with the calm line, Try again and Accept', { plugins: [worldFixtures] }, async ($, on) => {
  let asked = 0
  const w = world(on, { answer: (_n, isQuestions) => (isQuestions ? (asked++ === 0 ? null : QUESTIONS) : REWRITE) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  await ui.press({ key: 'improve-f-wizard' })
  await w.settle()
  expect(await says(ui, IMPROVE_FAILED)).toBe(true)
  expect(await has(ui, { text: /Run tsc --noEmit and list every file/ }), 'the earlier rewrite is still on show').toBe(true)
  expect(await has(ui, { type: 'Button', key: 'improve-f-retry' })).toBe(true)
  expect(await has(ui, { type: 'Button', key: 'improve-f-accept' })).toBe(true)
  await ui.press({ key: 'dash-tab-improve' })
  expect(await has(ui, { text: /Run tsc --noEmit and list every file/ }), 'and on the tab').toBe(true)
  expect(await has(ui, { type: 'Button', key: 'improve-t-accept' })).toBe(true)
  await ui.press({ key: 'improve-t-accept' })
  expect(boxValue(w)).toBe(BETTER)
})

test('F8: an error before any rewrite has no Accept', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => null })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  expect(await has(ui, { type: 'Button', key: 'improve-f-retry' })).toBe(true)
  expect(await has(ui, { type: 'Button', key: 'improve-f-accept' })).toBe(false)
})

test('F13: a stale model answer that lands after Discard and a new Improve never overwrites the new flow', { plugins: [worldFixtures] }, async ($, on) => {
  let release: (text: string) => void = () => undefined
  const first = new Promise<string>(res => { release = res })
  const NEW = JSON.stringify({ prompt: 'The second prompt, improved.', added: ['detail'] })
  const OLD = JSON.stringify({ prompt: 'The FIRST prompt, improved.', added: ['stale'] })
  const w = world(on, { answer: n => (n === 0 ? first : NEW) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  type(w, 'first rough prompt')
  await ui.press({ key: 'improve-f-go' })
  await w.settle() // the first call is out and waiting
  expect(w.log.calls).toBe(1)
  await ui.press({ key: 'improve-f-discard' })
  type(w, 'second rough prompt')
  await ui.press({ key: 'improve-f-go' })
  release(OLD)
  await w.settle()
  expect(w.log.calls).toBe(2)
  expect(await has(ui, { text: /The second prompt, improved\./ }), 'the new flow shows its own rewrite').toBe(true)
  expect(await has(ui, { text: /FIRST prompt/ }), 'the stale answer was dropped').toBe(false)
  await ui.press({ key: 'dash-tab-improve' })
  await ui.press({ key: 'improve-t-discard' })
  expect((w.stored.get('improveHistory') as { original: string }[]).map(e => e.original), 'only the new one was saved').toEqual(['second rough prompt'])
})

test('F5: a flow left working by a lost timer is cleared at session start; the box text stays and Improve works again', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: () => REWRITE })
  await $.session.start(sessionStart())
  const ui = await mount($)
  type(w, ROUGH)
  await ui.press({ key: 'improve-f-go' }) // the model call waits on a timer that is never advanced: the worker is "lost"
  expect(await says(ui, 'Writing the new prompt…')).toBe(true)
  await $.session.start(sessionStart()) // the reloaded module starts a session
  expect(await says(ui, 'Writing the new prompt…'), 'no stuck flow').toBe(false)
  expect(boxValue(w), 'the box text is intact').toBe(ROUGH)
  await w.settle()
  expect(w.log.calls, 'the dropped worker asks nothing').toBe(0)
  await ui.press({ key: 'improve-f-go' })
  await w.settle()
  expect(w.log.calls).toBe(1)
  expect(await has(ui, { text: '✦ IMPROVED PROMPT' })).toBe(true)
})

test('F5: a wizard question flow (no worker) is left alone at session start', { plugins: [worldFixtures] }, async ($, on) => {
  const w = world(on, { answer: (_n, isQuestions) => (isQuestions ? QUESTIONS : REWRITE) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  await ui.press({ key: 'improve-f-wizard' })
  await w.settle()
  await $.session.start(sessionStart())
  expect(await has(ui, { text: '✦ WIZARD' })).toBe(true)
})

test('F3: wizard option chips of 40 characters fit inside the padded box at 36, 46 and 80 columns', { plugins: [worldFixtures], timeoutMs: 30000 }, async ($, on) => {
  const forty = 'o'.repeat(40)
  const q = JSON.stringify({ questions: [{ question: 'Pick one?', options: [forty, `${forty.slice(0, 39)}x`, 'b', 'c'] }] })
  const w = world(on, { answer: (_n, isQuestions) => (isQuestions ? q : REWRITE) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  await improve(ui, w)
  await ui.press({ key: 'improve-f-wizard' })
  await w.settle()
  await ui.press({ key: 'improve-f-q0o0' }) // a picked chip too
  for (const cols of [36, 46, 80] as const) {
    await ui.redraw(paneProps(cols))
    const rows = improveRows(await ui.drawn())
    const over = rows.filter(r => (r.inCard ? r.w > cols - 4 : r.w > cols))
    expect(over.map(r => `${String(r.w)}: ${r.text}`), `footer chips at ${cols}`).toEqual([])
    expect(rows.some(r => r.text.includes('o'.repeat(20))), 'the options were measured').toBe(true)
  }
  await ui.press({ key: 'dash-tab-improve' })
  for (const cols of [36, 46, 80] as const) {
    await ui.redraw(paneProps(cols))
    const over = improveRows(await ui.drawn()).filter(r => (r.inCard ? r.w > cols - 4 : r.w > cols))
    expect(over.map(r => `${String(r.w)}: ${r.text}`), `tab chips at ${cols}`).toEqual([])
  }
})

type Drawn = { type?: string; props?: Record<string, unknown>; children?: unknown[] }
/** the cells a Box's padding takes across: paddingX both sides, paddingLeft, paddingRight */
const padOf = (c: Drawn): number => {
  const num = (k: string): number => (typeof c.props?.[k] === 'number' ? (c.props[k] as number) : 0)
  return num('paddingX') * 2 + num('paddingLeft') + num('paddingRight')
}
const drawnWidth = (n: unknown): number => {
  if (typeof n === 'string') return n.length
  if (typeof n !== 'object' || n === null) return 0
  const c = n as Drawn
  if (c.type === 'Input') return 0 // the engine lays a field out in the room that is left
  if (c.type === 'Text' || c.type === 'Button') {
    if (c.type === 'Button' && (c.children ?? []).length === 0) return String(c.props?.label ?? '').length + (c.props?.plain === true ? 0 : 4)
    return (c.children ?? []).reduce((s: number, k) => s + drawnWidth(k), 0)
  }
  if (typeof c.props?.width === 'number') return c.props.width
  const pad = padOf(c)
  const border = c.props?.borderStyle === 'round' ? 2 : 0
  const ks = c.children ?? []
  if (c.type === 'Box' && c.props?.flexDirection === 'row') {
    return ks.reduce((s: number, k) => s + drawnWidth(k), 0) + (typeof c.props.gap === 'number' ? c.props.gap : 0) * Math.max(0, ks.length - 1) + pad + border
  }
  return ks.reduce((m: number, k) => Math.max(m, drawnWidth(k)), 0) + pad + border
}
/** the drawn rows of the Improve footer (the Box under the dashed rule) and tab (the Box headed `✦ Improve`) */
function improveRows(root: unknown): { w: number; inCard: boolean; text: string }[] {
  const out: { w: number; inCard: boolean; text: string }[] = []
  const textOf = (n: unknown): string => (typeof n === 'string' ? n : typeof n === 'object' && n !== null ? ((n as Drawn).children ?? []).map(textOf).join('') : '')
  const ownText = (n: unknown): string => (typeof n === 'object' && n !== null && (n as Drawn).type === 'Text' ? textOf(n) : '')
  const isFooter = (c: Drawn): boolean => c.type === 'Box' && /^╌+$/.test(ownText((c.children ?? [])[0]))
  const isTab = (c: Drawn): boolean => c.type === 'Box' && (c.children ?? []).some(k => typeof k === 'object' && k !== null && (k as Drawn).type === 'Box' && ((k as Drawn).children ?? []).some(t => ownText(t) === '✦ Improve'))
  // a row's width plus the padding of the Boxes around it (a Card's own padding is counted by its content limit)
  const measure = (n: unknown, inCard: boolean, inset: number): void => {
    if (typeof n !== 'object' || n === null) return
    const c = n as Drawn
    const isCard = c.type === 'Box' && c.props?.borderStyle === 'round'
    const card = inCard || isCard
    if (c.type === 'Text' || c.type === 'Button' || (c.type === 'Box' && c.props?.flexDirection === 'row')) out.push({ w: drawnWidth(c) + inset, inCard: card, text: textOf(c).slice(0, 40) })
    const next = c.type === 'Box' && !isCard ? inset + padOf(c) : inset
    c.children?.forEach(k => { measure(k, card, next) })
  }
  const find = (n: unknown): void => {
    if (typeof n !== 'object' || n === null) return
    const c = n as Drawn
    if (isFooter(c) || isTab(c)) { measure(c, false, 0); return }
    c.children?.forEach(find)
  }
  find(root)
  return out
}

test('every Improve row stays within the pane width at 36, 46 and 80 columns: footer card, questions, errors and the tab', { plugins: [worldFixtures], timeoutMs: 30000 }, async ($, on) => {
  const long = `${'extremely '.repeat(40)}a_very_long_unbroken_word_${'y'.repeat(120)}`
  const wide = JSON.stringify({ prompt: `${long} ${long}`, added: [long, 'short'] })
  const wideQuestions = JSON.stringify({ questions: [{ question: long, options: [long, 'My team', 'Customers', 'Investors'] }, { question: 'How long?', options: ['One page', 'Short'] }] })
  const w = world(on, { answer: (_n, isQuestions) => (isQuestions ? wideQuestions : wide) })
  await $.session.start(sessionStart())
  const ui = await mount($)
  const check = async (what: string): Promise<void> => {
    for (const cols of [36, 46, 80] as const) {
      await ui.redraw(paneProps(cols))
      const over = improveRows(await ui.drawn()).filter(r => (r.inCard ? r.w > cols - 4 : r.w > cols))
      expect(over.map(r => `${String(r.w)}: ${r.text}`), `${what} at ${cols}`).toEqual([])
    }
  }
  type(w, long)
  await ui.press({ key: 'improve-f-go' })
  await check('working')
  await w.settle()
  await check('card')
  await ui.press({ key: 'dash-tab-improve' })
  await check('tab review')
  await ui.press({ key: 'improve-t-edit' })
  await check('tab edit')
  await ui.press({ key: 'improve-t-edit' })
  await ui.press({ key: 'improve-t-wizard' })
  await w.settle()
  await check('tab questions')
  await ui.press({ key: 'dash-tab-overview' })
  await check('footer questions')
  await ui.press({ key: 'improve-f-q0other' })
  await ui.input({ key: 'improve-other-0', text: long })
  await check('footer answered')
  await ui.press({ key: 'improve-f-discard' })
  await check('footer idle')
  type(w, '')
  await ui.press({ key: 'improve-f-go' })
  await check('footer empty error')
  const rows = improveRows(await ui.drawn())
  expect(rows.length, 'the walk found the Improve rows').toBeGreaterThan(3)
})
