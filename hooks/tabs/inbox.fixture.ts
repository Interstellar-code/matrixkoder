// The Inbox tab's fixture (plan inbox-and-parsing, phase UI): four sample files
// mirroring a `mk-inbox.py list --json` reply (C3). Pure data plus two tiny
// helpers — the render lives in inbox.tsx.
import type { InboxFile, InboxView } from './inbox'

export const INBOX_FILES: readonly InboxFile[] = [
  {
    id: 'plan-inbox-and-parsing',
    path: 'plan-inbox-and-parsing.md',
    title: 'Plan Inbox and Parsing',
    fit: 'template',
    sections_found: 7,
    sections_total: 7,
    missing: [],
    age_s: 7200,
    archived: false,
    state: { kind: 'idle' },
  },
  {
    id: 'wizards-redesign',
    path: 'wizards-redesign.md',
    title: 'Wizards Redesign',
    fit: 'partial',
    sections_found: 5,
    sections_total: 7,
    missing: ['Tasks', 'Acceptance'],
    age_s: 86400,
    archived: false,
    state: { kind: 'idle' },
  },
  {
    id: 'notes-2026-10-20',
    path: 'notes-2026-10-20.md',
    title: 'Scratch notes',
    fit: 'not_a_plan',
    sections_found: 0,
    sections_total: 7,
    missing: [],
    age_s: 1814400,
    archived: false,
    state: { kind: 'idle' },
  },
  {
    id: 'playpause',
    path: 'playpause.md',
    title: 'Play and Pause',
    fit: 'template',
    sections_found: 7,
    sections_total: 7,
    missing: [],
    age_s: 1036800,
    archived: true,
    state: { kind: 'idle' },
  },
]

/** inbox = not archived; template / fix count only the not archived. */
export const INBOX_COUNTS = { inbox: 3, template: 1, fix: 2, archived: 1 }

export const INBOX_FIXTURE: InboxView = { files: INBOX_FILES, counts: INBOX_COUNTS, filter: 'inbox' }

export function inboxView(): InboxView {
  return { ...INBOX_FIXTURE }
}

/** The file with another card state (importing, done, failed, confirm). */
export function withState(file: InboxFile, state: InboxFile['state']): InboxFile {
  return { ...file, state }
}
