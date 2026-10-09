// the /mk pane's Lanes tab (plan dashboard-v2-p0, S0 stub): T1/T2 replace the
// body with the artboard-A Lanes view from its fixture; the names below are
// the C1 contract the later phases build against.
import type { Elements } from 'claude-code'

type Els = Elements[keyof Elements]

/** T1/T2 fill this in; the stub carries only the mock flag (C1). */
export type LanesView = {
  mock: boolean
}

/** Phase 0 returns the fixture shape without data: the stub renders MOCK only. */
export function lanesView(): LanesView {
  return { mock: true }
}

/** Pure render: elements only, no $, no state (C1). */
export function LanesTab(els: Els, view: LanesView, cols: number) {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      <Text bold={true} color="warning">MOCK · fixture data · not this run</Text>
      <Text dimColor={true}>Lanes — mock coming in T1/T2</Text>
    </Box>
  )
}
