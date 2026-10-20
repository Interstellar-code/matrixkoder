// Improve logic tests: the prompts, the parsers, the conversation block and the sizing helpers
// ported from the standalone Prompt Upgrade mod.
import { test, expect } from 'claude-code/testing'
import {
  FORMAT_RULE, answeredPairs, expandPaste, bigDigits, bigNumber, clip, contextBlock, draftContext, draftPrompt, extractJson, finalText, newImproveFlow, questionsPrompt,
  readQuestions, readRewrite, rewritePrompt, wordCount, wrap,
} from './improve-core'

const QUESTIONS = JSON.stringify({
  questions: [
    { question: 'Who is it for?', options: ['Customers', 'My team', 'Investors'] },
    { question: 'How long?', options: ['One page', 'Short'] },
  ],
})
const REWRITE = JSON.stringify({
  prompt: 'Write a one page update for my team about [the launch]. Done means they know the date.',
  added: ['Audience: my team', 'Length: one page', 'What done looks like'],
})

test('wordCount counts whitespace-separated words; blank is zero', () => {
  expect(wordCount('')).toBe(0)
  expect(wordCount('   \n ')).toBe(0)
  expect(wordCount('one')).toBe(1)
  expect(wordCount('  Check all\nthe   files ')).toBe(4)
})

test('extractJson reads an object out of fences and chatter, null when none parses', () => {
  expect(extractJson('```json\n{"a":1}\n```')).toEqual({ a: 1 })
  expect(extractJson('Sure! {"a":{"b":2}} hope that helps')).toEqual({ a: { b: 2 } })
  expect(extractJson('no braces')).toBeNull()
  expect(extractJson('{not json}')).toBeNull()
})

test('contextBlock keeps the newest 150 messages, 500 chars each, 24000 in all, and drops empty text', () => {
  const turn = (i: number) => ({ role: i % 2 === 0 ? 'user' : 'assistant', text: `message ${String(i)}` })
  const many = Array.from({ length: 200 }, (_, i) => turn(i))
  const block = contextBlock(many).split('\n')
  expect(block).toHaveLength(150)
  expect(block[0]).toBe('Person: message 50')
  expect(block[149]).toBe('Assistant: message 199')
  // the speaker label: the person's rows and the assistant's
  expect(contextBlock([{ role: 'user', text: 'hi' }, { role: 'assistant', text: 'hello' }])).toBe('Person: hi\nAssistant: hello')
  // each message cut to 500 chars with an ellipsis, whitespace folded
  const cut = contextBlock([{ role: 'user', text: `a   b\n${'x'.repeat(2000)}` }])
  expect(cut.startsWith('Person: a b x')).toBe(true)
  expect(cut.length).toBe('Person: '.length + 500)
  expect(cut.endsWith('…')).toBe(true)
  // empty or blank text, tool-only rows and other roles are left out
  expect(contextBlock([
    { role: 'user', text: '' }, { role: 'assistant', text: '   ' }, { role: 'system', text: 'x' }, { role: 'user', text: 'kept' },
  ])).toBe('Person: kept')
  expect(contextBlock([])).toBe('')
  // the whole block is capped, newest rows win
  const big = Array.from({ length: 150 }, (_, i) => ({ role: 'user', text: `${String(i).padStart(3, '0')}${'y'.repeat(497)}` }))
  const capped = contextBlock(big)
  expect(capped.length).toBeLessThanOrEqual(24000)
  expect(capped.split('\n').length).toBeLessThan(150)
  expect(capped.split('\n').at(-1)).toContain('149')
})

test('questionsPrompt: the conversation block only with context, the prompt always, JSON-only reply', () => {
  const none = questionsPrompt('write a launch email')
  expect(none).not.toContain('<conversation>')
  expect(none).toContain('<prompt>\nwrite a launch email\n</prompt>')
  expect(none).toContain('Ask up to 3 multiple-choice questions')
  expect(none).toContain('Reply with JSON only')
  const withCtx = questionsPrompt('fix it', 'Person: the build fails')
  expect(withCtx).toContain('<conversation>\nPerson: the build fails\n</conversation>')
  expect(withCtx.indexOf('<conversation>')).toBeLessThan(withCtx.indexOf('<prompt>'))
})

test('rewritePrompt: the conversation block only with context, the refer-back rule, never invent facts, answers or none', () => {
  const none = rewritePrompt('fix it', [])
  expect(none).not.toContain('<conversation>')
  expect(none).toContain('The person answered no questions.')
  expect(none).toContain('Never invent facts, names, numbers or prices')
  expect(none).toContain('[placeholder]')
  expect(none).toContain('If the prompt is already clear and complete, return it unchanged.')
  expect(none).toContain('"it", "that", "the same way", "again"')
  expect(none).toContain('say what it refers to, using only what the conversation actually shows')
  const ctx = rewritePrompt('do that again', [{ question: 'Who is it for?', answer: 'My team' }], 'Person: build the report\nAssistant: done')
  expect(ctx).toContain('<conversation>\nPerson: build the report\nAssistant: done\n</conversation>')
  expect(ctx).toContain('- Who is it for? My team')
  expect(ctx).not.toContain('answered no questions')
})

test('readQuestions: at most three, two to four options, junk is no question', () => {
  const qs = readQuestions('```json\n' + QUESTIONS + '\n```')
  expect(qs).toHaveLength(2)
  expect(qs[0]?.options).toEqual(['Customers', 'My team', 'Investors'])
  const many = JSON.stringify({ questions: Array.from({ length: 6 }, (_, i) => ({ question: `q${String(i)}`, options: ['a', 'b', 'c', 'd', 'e'] })) })
  expect(readQuestions(many)).toHaveLength(3)
  expect(readQuestions(many)[0]?.options).toHaveLength(4)
  expect(readQuestions(JSON.stringify({ questions: [{ question: 'x?', options: ['only'] }] }))).toEqual([])
  expect(readQuestions(JSON.stringify({ questions: [{ question: 'x?', options: ['a', '  ', 3, 'b'] }] }))[0]?.options).toEqual(['a', 'b'])
  expect(readQuestions(JSON.stringify({ questions: [{ question: '', options: ['a', 'b'] }] }))).toEqual([])
  expect(readQuestions('sorry, no')).toEqual([])
  expect(readQuestions('{"questions":[]}')).toEqual([])
  expect(readQuestions('{"questions":"nope"}')).toEqual([])
  // long options and questions are cut
  const long = readQuestions(JSON.stringify({ questions: [{ question: 'q'.repeat(300), options: ['o'.repeat(100), 'b'] }] }))
  expect(long[0]?.question.length).toBe(160)
  expect(long[0]?.options[0]?.length).toBe(40)
})

test('readRewrite: the prompt and up to eight short notes; an empty or missing prompt is null', () => {
  expect(readRewrite(REWRITE)?.added).toHaveLength(3)
  expect(readRewrite('{"prompt":"  hi  "}')).toEqual({ upgraded: 'hi', added: [] })
  expect(readRewrite('{"prompt":""}')).toBeNull()
  expect(readRewrite('{"prompt":"   "}')).toBeNull()
  expect(readRewrite('{"added":["x"]}')).toBeNull()
  expect(readRewrite('plain words')).toBeNull()
  const notes = readRewrite(JSON.stringify({ prompt: 'p', added: Array.from({ length: 12 }, (_, i) => `n${String(i)}${'z'.repeat(200)}`) }))
  expect(notes?.added).toHaveLength(8)
  expect(notes?.added[0]?.length).toBe(90)
})

test('answeredPairs leaves out skipped and blank answers, in question order', () => {
  const questions = [{ question: 'A?', options: ['x', 'y'] }, { question: 'B?', options: ['x', 'y'] }, { question: 'C?', options: ['x', 'y'] }]
  expect(answeredPairs({ questions, answers: { '0': { kind: 'option', text: 'x' }, '1': { kind: 'skip' }, '2': { kind: 'other', text: '  mine ' } } }))
    .toEqual([{ question: 'A?', answer: 'x' }, { question: 'C?', answer: 'mine' }])
  expect(answeredPairs({ questions, answers: { '0': { kind: 'other', text: '   ' } } })).toEqual([])
  expect(answeredPairs({ questions, answers: {} })).toEqual([])
})

test('wrap never passes the width, cuts long words, keeps blank lines', () => {
  for (const w of [4, 20, 33, 80]) {
    for (const ln of wrap('A rather long sentence with a_very_long_unbroken_word_inside_it_that_goes_on\n\nnew paragraph', w)) expect(ln.length).toBeLessThanOrEqual(w)
  }
  expect(wrap('aa bb cc', 5)).toEqual(['aa bb', 'cc'])
  expect(wrap('a\n\nb', 10)).toEqual(['a', '', 'b'])
  expect(wrap('abcdefghij', 4)).toEqual(['abcd', 'efgh', 'ij'])
  expect(wrap('', 10)).toEqual([''])
  // a width under 4 is read as 4
  for (const ln of wrap('abcdefgh', 1)) expect(ln.length).toBeLessThanOrEqual(4)
})

test('bigNumber is three rows of heavy glyphs, never wider than maxWidth; larger numbers show as 99, 999', () => {
  for (const max of [3, 7, 11, 23]) {
    const out = bigNumber(1234567, max)
    expect(out).toHaveLength(3)
    for (const r of out) expect([...r].length).toBeLessThanOrEqual(max)
  }
  expect(bigNumber(7, 3)).toEqual(['╺━┓', '  ┃', '  ╹'])
  expect(bigNumber(25, 7)).toEqual(['╺━┓ ┏━╸', '┏━┛ ┗━┓', '┗━╸ ╺━┛'])
  expect(bigNumber(1234, 7)[0]).toBe(bigNumber(99, 7)[0]) // 2 digits fit: 99
  expect(bigNumber(-5, 7)).toEqual(bigNumber(0, 7))
  expect(bigDigits(7)).toBe(2)
  expect(bigDigits(11)).toBe(3)
  expect(bigDigits(2)).toBe(0)
})

test('clip cuts with an ellipsis; newImproveFlow starts on the one-call rewrite; finalText follows the editor', () => {
  expect(clip('abc', 5)).toBe('abc')
  expect(clip('abcdefgh', 5)).toBe('abcd…')
  const f = newImproveFlow('1', 'fix it')
  expect(f).toMatchObject({ id: '1', original: 'fix it', step: 'writing', questions: [], answers: {}, isEditing: false })
  expect(newImproveFlow('2', 'x', 'review').step).toBe('review')
  expect(finalText({ isEditing: false, draft: 'd', upgraded: 'u' })).toBe('u')
  expect(finalText({ isEditing: true, draft: 'd', upgraded: 'u' })).toBe('d')
})

test('draftContext: the latest exchange is whole and apart; the oldest rows are dropped first; notices are stripped', () => {
  const rows = Array.from({ length: 120 }, (_, i) => ({ role: i % 2 === 0 ? 'user' : 'assistant', text: `row ${String(i)} ${'x'.repeat(3000)}` }))
  rows.push({ role: 'user', text: 'which file?' }, { role: 'assistant', text: 'The NEWEST answer: hooks/a.ts' }, { role: 'user', text: '<task-notification>done</task-notification>' })
  const { earlier, latest } = draftContext(rows)
  expect(latest).toBe('Person: which file?\nAssistant: The NEWEST answer: hooks/a.ts')
  expect(earlier).toContain('row 119 ')
  expect(earlier).not.toContain('row 0 ') // over the 24000 budget: the oldest go first
  expect(earlier.split('\n').every(r => r.length <= 'Assistant: '.length + 4000)).toBe(true)
  // only the newest 100 rows are read at all
  const window = draftContext(Array.from({ length: 150 }, (_, i) => ({ role: i % 2 === 0 ? 'user' : 'assistant', text: `r${String(i)}` })))
  expect(window.earlier).not.toContain('r49\n')
  expect(window.earlier.startsWith('Person: r50')).toBe(true)
  // a long latest assistant message keeps 4000 chars
  const long = draftContext([{ role: 'user', text: 'go' }, { role: 'assistant', text: 'y'.repeat(9000) }])
  expect(long.latest.split('\n')[1]?.length).toBe('Assistant: '.length + 4000)
  expect(draftContext([]).latest).toBe('')
})

test('wrap: a numbered or dashed point wraps under its own text; lines never pass the width', () => {
  const out = wrap('Do three things:\n1. Run the full test suite and report the counts\n- check the docs', 20)
  expect(out[0]).toBe('Do three things:')
  expect(out[1]).toBe('1. Run the full test')
  expect(out[2]).toBe('   suite and report')
  expect(out[3]).toBe('   the counts')
  expect(out[4]).toBe('- check the docs')
  expect(out.every(l => l.length <= 20)).toBe(true)
})

test('every written text is asked for the list layout: Improve rewrite, Answer and Question', () => {
  const ctx = { earlier: '', latest: 'Person: go\nAssistant: done' }
  for (const p of [rewritePrompt('rough', []), draftPrompt('answer', '', ctx), draftPrompt('question', '', ctx)]) {
    expect(p).toContain(FORMAT_RULE)
  }
  expect(FORMAT_RULE).toContain('numbered list')
})

test('expandPaste: one placeholder with a matching line count is filled; anything else is not guessed', () => {
  const clip = 'a\nb\nc\n'
  expect(expandPaste('see [Pasted text #3 +2 lines] now', clip)).toEqual({ text: 'see a\nb\nc now', ok: true })
  expect(expandPaste('no paste here', null)).toEqual({ text: 'no paste here', ok: true })
  expect(expandPaste('see [Pasted text #3 +5 lines]', clip).ok, 'count mismatch').toBe(false)
  expect(expandPaste('[Pasted text #1 +2 lines] [Pasted text #2 +2 lines]', clip).ok, 'two pastes').toBe(false)
  expect(expandPaste('[Pasted text #1 +2 lines]', null).ok, 'no clipboard').toBe(false)
  expect(expandPaste('[Pasted text #1 +2 lines]', '   ').ok, 'empty clipboard').toBe(false)
  expect(expandPaste('x [Pasted text #1] y', 'one line').text, 'no line count: filled').toBe('x one line y')
})
