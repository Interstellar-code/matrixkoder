// The Improve tab's fixture: Chrome.dc.html's sample prompt and its rewrite, a proposal on show.
// Pure data — render lives in improve.tsx.
import type { ImproveView } from './improve'

export const IMPROVE_FIXTURE: ImproveView = {
  mock: true,
  text: 'Check all the files with type errors.',
  undo: null,
  history: [
    {
      id: '1', at: Date.parse('2026-10-18T09:12:00Z'), original: 'Check all the files with type errors.',
      upgraded: "Run tsc --noEmit and list every file with type errors. Give each error's file, line and cause, grouped by root cause. Don't edit anything yet.",
      added: ['The check to run: tsc --noEmit', 'Output: file, line and cause', 'Grouped by root cause', 'No edits yet'],
    },
  ],
  flow: {
    id: '1', original: 'Check all the files with type errors.', step: 'review', questions: [], answers: {}, other: null, otherDraft: '',
    upgraded: "Run tsc --noEmit and list every file with type errors. Give each error's file, line and cause, grouped by root cause. Don't edit anything yet.",
    added: ['The check to run: tsc --noEmit', 'Output: file, line and cause', 'Grouped by root cause', 'No edits yet'],
    isEditing: false, draft: '', failedStep: 'rewrite', error: '', note: '',
  },
}
