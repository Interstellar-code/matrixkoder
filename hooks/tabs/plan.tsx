// the /mk pane's Plan tab (plan dashboard-v2-p0, S0 stub): T1/T2 replace the
// body with the artboard-A Plan view from its fixture; the names below are
// the C1 contract the later phases build against.
import type { Elements } from 'claude-code'

type Els = Elements[keyof Elements]

/** T1/T2 fill this in; the stub carries only the mock flag (C1). */
export type PlanView = {
  mock: boolean
}

/** Phase 0 returns the fixture shape without data: the stub renders MOCK only. */
export function planView(): PlanView {
  return { mock: true }
}

/** Pure render: elements only, no $, no state (C1). */
export function PlanTab(els: Els, view: PlanView, cols: number) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      <Text dimColor={true}>Plan — mock coming in T1/T2</Text>
    </Box>
  )
}
