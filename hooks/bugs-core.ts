// The Bugs tab's $-free logic (plan bugs-tab, UI.1/UI.4): the view-state keys,
// the `mk-bug.py` argv, the tick's list (+ show) read as a view-state patch, the
// press → verb mapping and the pane view. Every spawn goes through an injected
// RunFn: $ may not cross an import (readPlans / runPlanAction precedent).
import type { RunFn } from './settings-io'
import { bugDetailView } from './tabs/bugdetail'
import { bugDraftOf, bugNewArgv } from './tabs/bugnew'
import { bugKanbanView } from './tabs/bugs-kanban'
import { bugsFromList } from './tabs/bugs'
import type { BugsSub, BugsView } from './tabs/bugs'

/** the pane's own keys (the render reads its filter keys through `ui.get`) */
export const BUGS_KEYS = {
  home: 'bugs:home',
  slug: 'bugs:slug',
  /** the raw `list` reply (JSON) */
  list: 'bugs:list',
  /** the raw `show` reply (JSON) and the id it was read for */
  show: 'bugs:show',
  showFor: 'bugs:show-for',
  /** a press's error (the next good press clears it) and the tick read's (the next good read clears it) */
  error: 'bugs:error',
  readError: 'bugs:read-error',
  note: 'bugs:note',
  sub: 'bugs:sub',
  sel: 'bugs:sel',
  /** the expanded list card */
  open: 'bugs:open',
  /** the simple view's picked card (its ▶ to lane needs the bug's tasks) */
  pick: 'bugs:pick',
} as const

/** a press that writes one of these keys reloads at once (the detail should not wait for the tick) */
export const BUGS_RELOAD_ON = new Set<string>([BUGS_KEYS.sel, BUGS_KEYS.open, BUGS_KEYS.pick])

/** B<n>, B<n>.<m>, B<n>.<m>.<k>: an id that is never read as a flag */
export const isBugId = (id: string): boolean => /^B\d+(\.\d+){0,2}$/.test(id)

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)

export function bugArgv(root: string, home: string, slug: string, tail: readonly string[]): string[] {
  return ['python3', `${root}/scripts/mk-bug.py`, '--home', home, '--project', slug, ...tail]
}

/** One mk-bug call: its `{ok:true}` reply, or the error text (the reply's, else stderr / the exit). Never throws. */
export async function runBug(run: RunFn, root: string, home: string, slug: string, tail: readonly string[], timeoutMs = 30_000): Promise<{ reply: Record<string, unknown> } | { error: string }> {
  try {
    const r = await run(bugArgv(root, home, slug, tail), { timeoutMs })
    let reply: unknown = null
    try { reply = JSON.parse(r.stdout.trim().split('\n').at(-1) ?? '') } catch { reply = null }
    if (r.exitCode === 0 && isRecord(reply) && reply.ok === true) return { reply }
    const err = isRecord(reply) && typeof reply.error === 'string' ? reply.error : r.stderr.trim() || `exit ${String(r.exitCode)}`
    return { error: `${tail[0] ?? ''}: ${err}` }
  } catch (e) {
    return { error: `${tail[0] ?? ''}: ${e instanceof Error ? e.message : String(e)}` }
  }
}

/** The bug the tick also shows: the open detail, else the expanded list card, else the simple view's picked card. */
export function showTarget(view: Record<string, string>): string {
  const sel = view[BUGS_KEYS.sel] ?? ''
  if (sel !== '' && sel !== 'new') return sel
  const sub = view[BUGS_KEYS.sub] ?? ''
  if (sub === 'list') return view[BUGS_KEYS.open] ?? ''
  return sub === 'kanban' ? '' : view[BUGS_KEYS.pick] ?? ''
}

/** The tick's read: `list`, plus `show <id>` when a bug is open or expanded. The view-state patch to apply. */
export async function readBugs(run: RunFn, root: string, home: string, slug: string, showId: string): Promise<Record<string, string>> {
  const list = await runBug(run, root, home, slug, ['list'], 10_000)
  if ('error' in list) return { [BUGS_KEYS.readError]: `bugs ${list.error}` }
  const patch: Record<string, string> = { [BUGS_KEYS.list]: JSON.stringify(list.reply), [BUGS_KEYS.readError]: '' }
  if (isBugId(showId)) {
    const show = await runBug(run, root, home, slug, ['show', showId], 10_000)
    // a refused show (a deleted bug) draws the detail's `no such bug`
    patch[BUGS_KEYS.show] = 'error' in show ? JSON.stringify({ ok: false }) : JSON.stringify(show.reply)
    patch[BUGS_KEYS.showFor] = showId
  }
  return patch
}

const DRAFT_KEYS = ['title', 'kind', 'severity', 'found_in', 'description', 'tasks', 'task', 'target'].map(k => `bugs:new:${k}`)

/** A press → its mk-bug verbs, run in order; the view-state patch to apply (error or note, cleared fields).
 *  create = `new` then one `task add` per task line; save-draft = `draft` (the tasks one per line in one
 *  `--tasks=` argument), which closes the form and notes the returned path; fixed = `close`. The lane actions
 *  (to lane, hand off, nudge, open pane) never come here: the tab submits their `mk:` line to the orchestrator. */
export async function bugPressPatch(run: RunFn, root: string, view: Record<string, string>, kind: string, id = '', text = ''): Promise<Record<string, string>> {
  const home = view[BUGS_KEYS.home] ?? ''
  const slug = view[BUGS_KEYS.slug] ?? ''
  const note = (n: string): Record<string, string> => ({ [BUGS_KEYS.note]: n, [BUGS_KEYS.error]: '' })
  if (home === '' || slug === '') return { [BUGS_KEYS.error]: `bugs ${kind}: no project` }
  const fail = (e: string): Record<string, string> => ({ [BUGS_KEYS.error]: `bugs ${e}`, [BUGS_KEYS.note]: '' })
  const call = (tail: readonly string[]) => runBug(run, root, home, slug, tail)
  if (kind === 'fixed') {
    if (!isBugId(id)) return fail(`fixed: bad id ${id}`)
    const r = await call(['close', id])
    return 'error' in r ? fail(r.error) : note(`✓ ${id} fixed`)
  }
  if (kind === 'quick-add') {
    if (text.trim() === '') return {}
    const r = await call(['new', `--title=${text}`, `--kind=${view['bugs:q:kind'] || 'other'}`, `--severity=${view['bugs:q:sev'] || 'med'}`])
    if ('error' in r) return fail(r.error)
    const bug = isRecord(r.reply.bug) ? r.reply.bug : {}
    return { ...note(`+ ${String(bug.id ?? '')} filed`), 'bugs:q': '' }
  }
  if (kind === 'task-add') {
    if (!isBugId(id) || text.trim() === '') return {}
    const r = await call(['task', 'add', id, `--title=${text}`])
    return 'error' in r ? fail(r.error) : { ...note(''), 'bugs:task:q': '', 'bugs:addtask': '' }
  }
  if (kind === 'save-draft') {
    const draft = bugDraftOf(k => view[k], '', slug)
    if (draft.title.trim() === '') return {}
    const r = await call(['draft', ...bugNewArgv(draft), ...(draft.tasks.length > 0 ? [`--tasks=${draft.tasks.join('\n')}`] : [])])
    if ('error' in r) return fail(r.error)
    return { ...Object.fromEntries(DRAFT_KEYS.map(k => [k, ''])), 'settings:focus': '', ...note(`draft saved: ${String(r.reply.path ?? '')}`), [BUGS_KEYS.sel]: '' }
  }
  if (kind === 'create') {
    const draft = bugDraftOf(k => view[k], '', slug)
    if (draft.title.trim() === '') return {}
    const r = await call(['new', ...bugNewArgv(draft)])
    if ('error' in r) return fail(r.error)
    const newId = isRecord(r.reply.bug) && typeof r.reply.bug.id === 'string' ? r.reply.bug.id : ''
    const cleared = { ...Object.fromEntries(DRAFT_KEYS.map(k => [k, ''])), 'settings:focus': '' }
    for (const t of draft.tasks) {
      const tr = isBugId(newId) ? await call(['task', 'add', newId, `--title=${t}`]) : { error: 'task add: no bug id in the reply' }
      if ('error' in tr) return { ...cleared, ...fail(`${newId} filed, but ${tr.error}`), [BUGS_KEYS.sel]: newId }
    }
    return { ...cleared, ...note(`+ ${newId} filed`), [BUGS_KEYS.sel]: newId }
  }
  return {}
}

/** The todo → bug argv tail: the todo text is the title, verbatim, one argv element; the source names the todo. */
export function todoBugTail(todo: { id: number; text: string }): string[] {
  return ['new', `--title=${todo.text}`, `--source=todo #${String(todo.id)}`]
}

const parse = (text: string | undefined): unknown => { try { return JSON.parse(text ?? '') } catch { return null } }

/** The pane's view out of its view state. `now` is ISO (the age column). */
export function bugsViewOf(view: Record<string, string>, now: string): BugsView {
  const listReply = parse(view[BUGS_KEYS.list])
  const { counts, bugs } = bugsFromList(listReply)
  const project = isRecord(listReply) && typeof listReply.project === 'string' && listReply.project !== '' ? listReply.project : view[BUGS_KEYS.slug] ?? ''
  const sub = view[BUGS_KEYS.sub]
  const target = showTarget(view)
  const showRaw = view[BUGS_KEYS.show] ?? ''
  const detail = target !== '' && view[BUGS_KEYS.showFor] === target && showRaw !== '' ? bugDetailView(parse(showRaw), project) : null
  const max = bugs.reduce((m, b) => Math.max(m, Number(/^B(\d+)$/.exec(b.id)?.[1] ?? 0)), 0)
  return {
    project,
    loaded: isRecord(listReply) && listReply.ok === true,
    counts,
    bugs,
    sub: (sub === 'list' || sub === 'kanban' ? sub : 'simple') as BugsSub,
    sel: view[BUGS_KEYS.sel] ?? '',
    detail,
    kanban: bugKanbanView(listReply, now),
    // the next id is a guess from the list (a deleted bug's id is never reused, so the store may pick a later one)
    draft: bugDraftOf(k => view[k], `B${String(max + 1)}`, project),
    now,
    note: view[BUGS_KEYS.note] ?? '',
  }
}
