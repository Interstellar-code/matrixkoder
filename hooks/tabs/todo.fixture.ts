// The Todo tab's fixture (plan todo-tab-r1, P2): the Todo.dc.html design's five
// sample todos, newest first. Pure data — the render lives in todo.tsx.
import type { TodoView } from './todo'

export const TODO_FIXTURE: TodoView = {
  mock: true,
  project: 'matrixkoder',
  todos: [
    { id: 5, text: 'Review the p5 plantask diff', done: false, created: '2026-10-18 09:12' },
    { id: 4, text: 'Cut v0.6.5 release and changelog', done: false, created: '2026-10-18 09:05' },
    { id: 3, text: 'Ask Z.AI about the coding plan quota', done: false, created: '2026-10-17 18:40' },
    { id: 2, text: 'Merge the p4 mkdb lane', done: true, created: '2026-10-17 16:22' },
    { id: 1, text: 'Update the Files tab docs', done: true, created: '2026-10-17 11:03' },
  ],
  filter: 'all',
  draft: '',
  undo: null,
}
