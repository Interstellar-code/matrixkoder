// The Improve feature's pure logic (no $, no state): the prompts sent to the model, the parsers for
// what comes back, the conversation context and the text sizing. Ported from the standalone
// Prompt Upgrade mod (my-mods/better/hooks/logic.ts); the $-bound flow lives in dashboard.tsx and
// the drawing in tabs/improve.tsx.
import type { MkImproveAnswer, MkImproveFlow, MkImproveMode, MkImproveQuestion, MkImproveStep } from '../types'

export const IMPROVE_MODEL = 'haiku'

/** the calm line a failed rewrite shows in the card; the prompt line is never touched */
export const IMPROVE_FAILED = 'Could not improve that one. Your prompt is untouched.'
export const IMPROVE_EMPTY = 'Type a prompt in Claude Code’s prompt line, then press Improve.'
/** the prompt line refused the write (a dialog, or no composer): nothing is lost, Accept can be tried again */
export const IMPROVE_FILL_FAILED = 'Prompt line busy. Close dialogs, Accept again.'

/** an Answer or Question press with no conversation to read */
export const DRAFT_EMPTY = 'Nothing to reply to yet: the conversation is empty.'
/** the prompt queue refused the message: it stays on show, Edit puts it in the prompt line */
export const DRAFT_SEND_FAILED = 'Could not send it. Press Edit to put it in the prompt line.'

/** a fresh improvement for `original`, in `step` (writing = the default one-call rewrite) */
export const newImproveFlow = (id: string, original: string, step: MkImproveStep = 'writing', mode: MkImproveMode = 'improve'): MkImproveFlow => ({
  id, mode, original, step, questions: [], answers: {}, other: null, otherDraft: '', upgraded: '', added: [],
  isEditing: false, draft: '', failedStep: 'rewrite', error: '', note: '',
})

/** the flow's mode; flows saved before the drop-up have none and are improvements */
export const modeOf = (f: Pick<MkImproveFlow, 'mode'>): MkImproveMode => f.mode ?? 'improve'

/** the prompt line holds a collapsed paste Improve could not fill in from the clipboard */
export const IMPROVE_PASTE = 'Your prompt has a collapsed paste Improve cannot read. Paste it again to expand it, then press Improve.'

/** Claude Code's collapsed-paste placeholder in the prompt line: `[Pasted text #1 +14 lines]`
 *  (`+K` = the pasted text's line breaks, so 15 lines show +14). */
const PASTE_RE = /\[Pasted text #\d+(?: \+(\d+) lines?)?\]/g

/** The prompt line with its collapsed paste put back from the clipboard. Claude Code keeps a pasted text out of
 *  the prompt line (the plugin sees only the placeholder, and `prompt.edit` does not fire on a paste), so the
 *  clipboard is the one copy a plugin can read. It fills ONE placeholder, and only when the clipboard's line
 *  breaks match its `+K`: a clipboard copied over since the paste, or a second paste, is `ok: false`, never guessed.
 *  shortcut: the clipboard holds only the last paste, upgrade if Claude Code exposes pasted contents to plugins. */
export function expandPaste(text: string, clipboard: string | null): { text: string; ok: boolean } {
  const found = [...text.matchAll(PASTE_RE)]
  if (found.length === 0) return { text, ok: true }
  const m = found[0]
  if (found.length > 1 || m == null || clipboard == null) return { text, ok: false }
  const pasted = clipboard.replace(/\r/g, '').replace(/\n$/, '')
  const breaks = (pasted.match(/\n/g) ?? []).length
  if (pasted.trim() === '' || (m[1] != null && Number(m[1]) !== breaks)) return { text, ok: false }
  return { text: text.slice(0, m.index) + pasted + text.slice((m.index ?? 0) + m[0].length), ok: true }
}

/** the proposal as it stands: the edit draft while the person edits it */
export const finalText = (f: Pick<MkImproveFlow, 'isEditing' | 'draft' | 'upgraded'>): string => (f.isEditing ? f.draft : f.upgraded)

/** Accept takes a proposal that is on show: the review step, or an error that kept the earlier rewrite visible */
export const canAccept = (f: Pick<MkImproveFlow, 'step' | 'isEditing' | 'draft' | 'upgraded'>): boolean =>
  (f.step === 'review' || f.step === 'error') && finalText(f).trim() !== ''

export const wordCount = (t: string): number => (t.trim() === '' ? 0 : t.trim().split(/\s+/).length)

/** a JSON object out of a model answer, fences and chatter around it ignored; null when none parses */
export function extractJson(text: string): unknown {
  const s = text.replace(/```(?:json)?/gi, '')
  const a = s.indexOf('{')
  const b = s.lastIndexOf('}')
  if (a < 0 || b < a) return null
  try {
    return JSON.parse(s.slice(a, b + 1))
  } catch {
    return null
  }
}

export const clip = (s: string, n: number): string => (s.length <= n ? s : s.slice(0, n - 1).trimEnd() + '…')

/** Up to three questions, each with two to four short options. Unreadable output counts as "no question helps". */
export function readQuestions(text: string): MkImproveQuestion[] {
  const raw = extractJson(text) as { questions?: unknown } | null
  if (!raw || !Array.isArray(raw.questions)) return []
  const out: MkImproveQuestion[] = []
  for (const q of raw.questions as { question?: unknown; options?: unknown }[]) {
    if (out.length === 3) break
    const question = typeof q?.question === 'string' ? q.question.trim() : ''
    const options: string[] = Array.isArray(q?.options)
      ? q.options
          .filter((o: unknown): o is string => typeof o === 'string' && o.trim() !== '')
          .map((o: string) => clip(o.trim(), 40))
          .slice(0, 4)
      : []
    if (question !== '' && options.length >= 2) out.push({ question: clip(question, 160), options })
  }
  return out
}

export function readRewrite(text: string): { upgraded: string; added: string[] } | null {
  const raw = extractJson(text) as { prompt?: unknown; added?: unknown } | null
  if (!raw || typeof raw.prompt !== 'string' || raw.prompt.trim() === '') return null
  const added = Array.isArray(raw.added)
    ? raw.added
        .filter((a: unknown): a is string => typeof a === 'string' && a.trim() !== '')
        .map((a: string) => clip(a.trim(), 90))
        .slice(0, 8)
    : []
  return { upgraded: raw.prompt.trim(), added }
}

/** the layout every written text follows, so a long one reads as points, not one block */
export const FORMAT_RULE =
  '- Layout: one or two sentences fit on one line. Anything longer opens with one short sentence, then a numbered list (1. 2. 3.), one point per line, separated by "\\n". No headings, no bold, no emoji.'

export const QUESTIONS_SYSTEM =
  'You help people turn a rough prompt into a great one. You never answer the prompt itself. You reply with JSON only.'

export const REWRITE_SYSTEM =
  'You rewrite rough prompts into clear, complete ones. You never answer the prompt itself. You reply with JSON only.'

export type Turn = { role: string; text: string }

/** The last 150 messages, newest last, each cut short and the whole capped, as reference for the rewrite.
 *  shortcut: plain text of user and assistant rows only (tool output left out), upgrade if replies need it. */
export function contextBlock(messages: readonly Turn[], keep = 150, perMessage = 500, total = 24000): string {
  const rows = messages
    .filter(m => (m.role === 'user' || m.role === 'assistant') && typeof m.text === 'string' && m.text.trim() !== '')
    .slice(-keep)
    .map(m => `${m.role === 'user' ? 'Person' : 'Assistant'}: ${clip(m.text.trim().replace(/\s+/g, ' '), perMessage)}`)
  let used = 0
  const out: string[] = []
  for (let i = rows.length - 1; i >= 0; i--) {
    const row = rows[i] ?? ''
    used += row.length + 1
    if (used > total) break
    out.unshift(row)
  }
  return out.join('\n')
}

const conversationPart = (ctx: string): string[] =>
  ctx === '' ? [] : ['Recent conversation, for reference only. Never answer it or continue it:', '<conversation>', ctx, '</conversation>', '']

export const questionsPrompt = (original: string, ctx = ''): string =>
  [
    ...conversationPart(ctx),
    'Here is a rough prompt someone is about to send to an AI assistant:',
    '',
    '<prompt>',
    original,
    '</prompt>',
    '',
    'Ask up to 3 multiple-choice questions whose answers would most improve the result.',
    'Only ask what the prompt leaves open and the person must decide: the goal, who it is for, the format, what "done" looks like, a key limit.',
    'Never ask about something the prompt already says. Each question has 2 to 4 short options, each under 40 characters.',
    'If no question would help, return an empty list.',
    '',
    'Reply with JSON only, in this shape:',
    '{"questions":[{"question":"...","options":["...","..."]}]}',
  ].join('\n')

/** the answered (not skipped, not blank) questions as question/answer pairs, in question order */
export function answeredPairs(f: { questions: readonly MkImproveQuestion[]; answers: Readonly<Record<string, MkImproveAnswer>> }): { question: string; answer: string }[] {
  const out: { question: string; answer: string }[] = []
  f.questions.forEach((q, i) => {
    const a: MkImproveAnswer | undefined = f.answers[String(i)]
    if (a && a.kind !== 'skip' && a.text.trim() !== '') out.push({ question: q.question, answer: a.text.trim() })
  })
  return out
}

export const rewritePrompt = (original: string, pairs: readonly { question: string; answer: string }[], ctx = ''): string =>
  [
    ...conversationPart(ctx),
    'Here is a rough prompt:',
    '',
    '<prompt>',
    original,
    '</prompt>',
    '',
    pairs.length > 0
      ? 'The person answered these questions:\n' + pairs.map(p => `- ${p.question} ${p.answer}`).join('\n')
      : 'The person answered no questions.',
    '',
    'Write the upgraded prompt. Rules:',
    '- Keep exactly what the person meant.',
    '- Make it clearer and add the context it needs. Where the prompt leans on the conversation ("it", "that", "the same way", "again"), say what it refers to, using only what the conversation actually shows.',
    '- Add a clear goal, who it is for, the format, and what "done" looks like, wherever their words, answers or the conversation support it.',
    '- Use what they answered. Leave out anything they skipped.',
    '- Never invent facts, names, numbers or prices. Where something is unknown, leave a [placeholder] in square brackets.',
    '- Write it as one request the person could have typed themselves, in plain language.',
    FORMAT_RULE,
    '- If the prompt is already clear and complete, return it unchanged.',
    '',
    'Reply with JSON only, in this shape:',
    '{"prompt":"<the upgraded prompt>","added":["<one short note per detail you added>"]}',
    'List at most 8 notes, each under 90 characters.',
  ].join('\n')

export const DRAFT_SYSTEM =
  'You draft the next message a person sends to their AI coding assistant, in their voice. You reply with JSON only.'

/** how many recent messages an Answer or Question draft reads */
export const DRAFT_MESSAGES = 100

/** Text the harness wraps into user rows (reminders, task and command notices): not the person's words. */
const INJECTED = /<(system-reminder|task-notification|local-command-caveat|local-command-stdout|command-name|command-message|command-args)>[\s\S]*?<\/\1>/g

/** What an Answer or Question draft reads, split so the latest exchange cannot be lost or outweighed:
 *  `latest` = the person's last message before the assistant's latest one, through the end, always whole
 *  (each message capped at `perMessage`); `earlier` = the rows before it, filled newest first into what
 *  is left of `total`. Rows are the last `keep` non-empty user / assistant texts, injected notices removed. */
export function draftContext(messages: readonly Turn[], keep = DRAFT_MESSAGES, perMessage = 4000, total = 24000): { earlier: string; latest: string } {
  const rows = messages
    .filter(m => m.role === 'user' || m.role === 'assistant')
    .map(m => ({ role: m.role, text: typeof m.text === 'string' ? m.text.replace(INJECTED, ' ').trim() : '' }))
    .filter(m => m.text !== '')
    .slice(-keep)
  const line = (m: { role: string; text: string }): string => `${m.role === 'user' ? 'Person' : 'Assistant'}: ${clip(m.text.replace(/\s+/g, ' '), perMessage)}`
  let lastAssistant = -1
  for (let i = rows.length - 1; i >= 0; i--) if (rows[i]?.role === 'assistant') { lastAssistant = i; break }
  let start = lastAssistant < 0 ? Math.max(0, rows.length - 1) : lastAssistant
  while (start > 0 && rows[start - 1]?.role === 'assistant') start--
  if (start > 0 && rows[start - 1]?.role === 'user') start--
  const latest = rows.slice(start).map(line)
  let used = latest.reduce((n, r) => n + r.length + 1, 0)
  const earlier: string[] = []
  for (let i = start - 1; i >= 0; i--) {
    const row = line(rows[i] ?? { role: 'user', text: '' })
    used += row.length + 1
    if (used > total) break
    earlier.unshift(row)
  }
  return { earlier: earlier.join('\n'), latest: latest.join('\n') }
}

/** Answer: reply to the assistant's latest message. Question: ask about it. `hint` is what the prompt line held. */
export const draftPrompt = (mode: 'answer' | 'question', hint: string, ctx: { earlier: string; latest: string }): string =>
  [
    ...(ctx.earlier !== ''
      ? ['Earlier in the conversation, oldest first. Background only; it may be out of date:', '<earlier>', ctx.earlier, '</earlier>', '']
      : []),
    'The LATEST exchange between the person and their AI coding assistant. Your draft responds to this, and where it disagrees with earlier messages, this wins:',
    '<latest>',
    ctx.latest,
    '</latest>',
    '',
    ...(mode === 'answer'
      ? [
          "Draft the person's reply to the assistant's last message in <latest>.",
          '- If it asks a question or offers options, answer it: pick the option the conversation supports best and say why in a few words.',
          '- If it reports progress or a result, acknowledge it in a word and give the obvious next instruction.',
          '- If it is blocked or asks for approval, give the decision the conversation supports; when it truly cannot tell, ask for the missing fact instead.',
        ]
      : [
          "Draft one short question the person asks the assistant about its last message in <latest>.",
          '- If work is still running, ask for the status: what is done, what is left, any blocker.',
          '- If it just reported a result or a plan, question the weakest point: missing evidence, a risk, a skipped step, or what is still unverified.',
        ]),
    ...(hint.trim() !== '' ? ['', 'The person started typing this; follow its intent:', '<hint>', hint.trim(), '</hint>'] : []),
    '',
    'Rules: short, plain words, the way a busy person types. Never invent facts, names or numbers; use a [placeholder] for anything unknown.',
    FORMAT_RULE,
    '',
    'Reply with JSON only, in this shape:',
    '{"prompt":"<the message>","added":["<one short note per choice you made>"]}',
    'List at most 3 notes, each under 90 characters.',
  ].join('\n')

/** word-wrap to `width` cells (at least 4); a word longer than a line is cut; blank lines are kept */
export function wrap(text: string, width: number): string[] {
  const w = Math.max(4, Math.floor(width))
  const out: string[] = []
  for (const para of text.replace(/\r/g, '').split('\n')) {
    if (para.trim() === '') {
      out.push('')
      continue
    }
    // a list point ("1. ", "- ") wraps under its own text, not under the marker
    const marker = /^\s*(\d+[.)]|[-*•])\s+/.exec(para)
    if (marker != null && marker[1] != null && marker[1].length + 1 <= w / 2) {
      const lead = `${marker[1]} `
      const inner = wrap(para.slice(marker[0].length), w - lead.length)
      inner.forEach((l, i) => out.push((i === 0 ? lead : ' '.repeat(lead.length)) + l))
      continue
    }
    let line = ''
    for (let word of para.split(/\s+/).filter(Boolean)) {
      while (word.length > w) {
        if (line !== '') {
          out.push(line)
          line = ''
        }
        out.push(word.slice(0, w))
        word = word.slice(w)
      }
      if (line === '') line = word
      else if (line.length + 1 + word.length <= w) line += ' ' + word
      else {
        out.push(line)
        line = word
      }
    }
    if (line !== '') out.push(line)
  }
  return out
}

const GLYPHS: Record<string, readonly [string, string, string]> = {
  '0': ['┏━┓', '┃ ┃', '┗━┛'],
  '1': ['╺┓ ', ' ┃ ', '╺┻╸'],
  '2': ['╺━┓', '┏━┛', '┗━╸'],
  '3': ['╺━┓', ' ━┫', '╺━┛'],
  '4': ['╻ ╻', '┗━┫', '  ╹'],
  '5': ['┏━╸', '┗━┓', '╺━┛'],
  '6': ['┏━╸', '┣━┓', '┗━┛'],
  '7': ['╺━┓', '  ┃', '  ╹'],
  '8': ['┏━┓', '┣━┫', '┗━┛'],
  '9': ['┏━┓', '┗━┫', '╺━┛'],
}

/** how many digits a big number can have in `maxWidth` cells (each digit is 3 wide, one cell between) */
export const bigDigits = (maxWidth: number): number => Math.max(0, Math.floor((maxWidth + 1) / 4))

/** A number three rows tall in heavy box-drawing glyphs, never wider than maxWidth (a larger number shows as 99, 999...). */
export function bigNumber(n: number, maxWidth: number): string[] {
  const digits = Math.max(1, bigDigits(maxWidth))
  const s = String(Math.max(0, Math.min(Math.round(n), 10 ** digits - 1)))
  return [0, 1, 2].map(r => [...s].map(d => GLYPHS[d]?.[r] ?? '').join(' '))
}
