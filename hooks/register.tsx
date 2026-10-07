// Wires every MatrixKoder hooks module (hooks.json loads only this file).
import type { Register } from 'claude-code'
import { registerGuards } from './guards'
import { registerDashboard } from './dashboard'
import { registerStatus } from './status'
import { registerResume } from './resume'

export const register: Register = (on, options) => {
  registerGuards(on, options)
  registerDashboard(on, options)
  registerStatus(on, options)
  registerResume(on)
  // registerTools + registerWake (worktree mk-b) join here at merge
}
