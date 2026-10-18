// Wires every MatrixKoder hooks module (hooks.json loads only this file).
import type { Register } from 'claude-code'
import { registerGuards } from './guards'
import { registerDashboard } from './dashboard'
import { registerStatus } from './status'
import { registerResume } from './resume'
import { registerStopGuard } from './stop-guard'
import { registerTools } from './tools'
import { registerWake } from './wake'
import { registerWizard } from './wizard'
import { registerFileActivity } from './filetree-activity'

export const register: Register = (on, options) => {
  registerGuards(on, options)
  registerDashboard(on, options)
  registerStatus(on, options)
  registerResume(on, options)
  registerStopGuard(on, options)
  registerTools(on, options)
  registerWake(on, options)
  registerWizard(on, options)
  registerFileActivity(on)
}
